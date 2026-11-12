"""«Мой заказ» — виджет заказа для клиента в стиле мини-приложения MALIMABI.

Стиль — из webapp/index.html (макет Миши): чёрный фон, карточки #141414,
красный акцент #e10600, заголовки широким жирным шрифтом капсом.

Шрифты в виджете запрещены (@font-face Telegram вырезает — на компьютере
пока нет, на телефоне да). Поэтому заголовок рисуется контурами букв в
inline SVG: буквы берём из тех же шрифтов, что у приложения — латиницу и
цифры из Anybody, кириллицу из Unbounded (fontTools есть в venv вместе с
weasyprint).

Позиции заказа (несколько товаров или экземпляров) — вкладками; подпись
вкладки — то, чем позиции отличаются: товар, цвет или модель.

Этапы: оплачен → готов → в пути → в пункте выдачи → получен. Последние
два — по статусам СДЭК (services/cdek_tracker.py).

Пока это пробник: шлём только админам командой /mywidget <id заказа>.
"""
import html
import json
import logging
import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import config
import database as db
from handlers.order_widget import api, _answers, _delivery, _fits

logger = logging.getLogger(__name__)
router = Router()

FONTS = Path(__file__).resolve().parent.parent / "webapp" / "static" / "fonts"
TRACK_URL = "https://www.cdek.ru/ru/tracking?order_id={}"

# (поле с датой, подпись на полосе, заголовок, пояснение)
STAGES = [
    ("created_at", "Оплачен", "В работе",
     "Заказ оплачен и стоит в очереди на изготовление."),
    ("printed_at", "Готов", "Готов",
     "Заказ изготовлен и скоро уедет в СДЭК."),
    ("shipped_at", "В пути", "В пути",
     "Посылка едет в пункт выдачи. Когда приедет — пришлю сообщение."),
    ("arrived_notified_at", "В ПВЗ", "Можно забирать",
     "Понадобится паспорт или код из приложения СДЭК."),
    ("delivered_at", "Получен", "Получен",
     "Спасибо за заказ! Будем рады отзыву."),
]
NOT_DELIVERED = ("Не получен", "Посылка не вручена и уходит обратно. Напишите нам — разберёмся.")


# ── заголовок контурами букв ────────────────────────────────────────

@lru_cache(maxsize=1)
def _fonts():
    from fontTools.ttLib import TTFont
    return [TTFont(str(FONTS / f)) for f in ("anybody-latin.woff2", "unbounded-cyrillic.woff2")]


def _glyph(ch: str):
    for font in _fonts():
        name = font.getBestCmap().get(ord(ch))
        if name:
            return font, name
    return None, None


def _advance(ch: str) -> float:
    """Ширина буквы в долях кегля (с трекингом −0.01 em, как в приложении)."""
    font, name = _glyph(ch)
    if not font:
        return 0.3
    return font["hmtx"][name][0] / font["head"].unitsPerEm - 0.01


def _wrap(text: str, max_em: float) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        trial = f"{lines[-1]} {word}" if lines else word
        if lines and sum(map(_advance, trial)) <= max_em:
            lines[-1] = trial
        else:
            lines.append(word)
    return lines or [""]


@lru_cache(maxsize=32)
def headline_svg(text: str, size: int = 26, max_em: float = 10.0, color: str = "#f2f2f2") -> str:
    """Заголовок капсом как SVG-контуры. Координаты — в сотых кегля: на 26 px
    это четверть пикселя, а цифр в пути вдвое меньше, чем в тысячных."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    cap, lead, em = 75, 95, 100              # высота прописных Unbounded, межстрочный
    lines = _wrap(text.upper(), max_em)
    parts, width = [], 0
    for i, line in enumerate(lines):
        x, base = 0.0, cap + i * lead
        for ch in line:
            font, name = _glyph(ch)
            if font:
                k = em / font["head"].unitsPerEm
                gs = font.getGlyphSet()
                pen = SVGPathPen(gs, ntos=lambda v: str(round(v)))
                gs[name].draw(TransformPen(pen, (k, 0, 0, -k, x, base)))
                parts.append(pen.getCommands())
            x += _advance(ch) * em
        width = max(width, x)
    height = cap + (len(lines) - 1) * lead
    w, h = round(width), round(height)
    return (f'<svg viewBox="0 0 {w} {h}" width="{w * size // em}" height="{h * size // em}" '
            f'style="display:block;overflow:visible;max-width:100%;height:auto" role="img" '
            f'aria-label="{html.escape(text)}"><path fill="{color}" d="{"".join(parts)}"/></svg>')


# ── что показываем ──────────────────────────────────────────────────

def _short_code(order: dict) -> str:
    m = re.search(r"(\d+)$", order.get("order_code") or "")
    return m.group(1) if m else str(order["id"])


def _date(ts: str | None) -> str:
    """Дата этапа по Москве: в базе UTC, вечерние отметки иначе съезжают на день."""
    if not ts:
        return ""
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "")) + timedelta(hours=3)
    except ValueError:
        return ""
    return d.strftime("%d.%m")


def _total(summary: str) -> str:
    m = re.search(r"💵\s*([\d\s]+)\s*₽", summary or "")
    return f"{m.group(1).strip()} ₽" if m else ""


# Вопросы опроса длинные — в виджете клиенту хватит одного слова
LABELS = [("модел", "Модель"), ("цвет", "Цвет"), ("ручк", "Ручка"), ("логотип", "Логотип"),
          ("текстур", "Текстура"), ("гравир", "Гравировка")]


def _label(q: str) -> str:
    low = q.lower()
    for key, short in LABELS:
        if key in low:
            return short
    return q


def _answer(a: list) -> list:
    """[вопрос, ответ(, цвет, тёмный)]: тёмный цвет на чёрном фоне не виден —
    тогда ответ белым, а цвет показывает кружок с рамкой."""
    out = [_label(a[0]), a[1]]
    if len(a) > 2:
        r, g, b = (int(a[2][i:i + 2], 16) for i in (1, 3, 5))
        out += [a[2], int((0.299 * r + 0.587 * g + 0.114 * b) / 255 < 0.25)]
    return out


def _address(where: str) -> str:
    """«СДЭК, Москва, Москва, пер. …» → «Москва, пер. …»."""
    parts = [p for p in where.replace("ПВЗ ", "").split(", ") if p and p != "СДЭК"]
    out: list[str] = []
    for p in parts:
        if not out or out[-1] != p:
            out.append(p)
    return ", ".join(out).replace("помещение", "пом.")


def _pick(answers: list, short: str) -> str:
    """Ответ на вопрос «Цвет»/«Модель» коротко: «3 (красный)» → «красный»."""
    for a in answers:
        if a[0] == short:
            m = re.search(r"\(([^)]+)\)", a[1])
            return (m.group(1) if m else a[1]).strip()
    return ""


async def _positions(order: dict) -> list[dict]:
    """Позиции заказа: [{"l": подпись вкладки, "p": товар, "q": ответы}]."""
    summary = order.get("summary") or ""
    chunks = re.split(r"\n—\s*<b>Позиция \d+</b>\s*—\n", summary)
    try:
        rounds = json.loads(order.get("rounds_json") or "[]")
    except Exception:
        rounds = []
    pids = db.unpack_round_products(order.get("round_products_json"), rounds,
                                    order["product_id"]) or [order["product_id"]]
    # Перед «— Позиция 1 —» идёт шапка заказа без ответов — её пропускаем
    if len(chunks) > 1 and not _answers(chunks[0]):
        chunks = chunks[1:]
    out = []
    for i, chunk in enumerate(chunks):
        pid = pids[i] if i < len(pids) else order["product_id"]
        product = await db.get_product(pid) or {}
        name = re.sub(r"^[^\wА-Яа-яЁё]+", "", product.get("name") or "").strip()
        out.append({"p": name, "q": [_answer(a) for a in _answers(chunk)]})
    if len(out) > 1:
        # Подпись вкладки — то, чем позиции различаются
        for key in ("p", "Цвет", "Модель"):
            vals = [p["p"] if key == "p" else _pick(p["q"], key) for p in out]
            if all(vals) and len(set(vals)) > 1:
                break
        else:
            vals = [""] * len(out)
        dup = len(set(vals)) < len(vals)
        for i, (p, v) in enumerate(zip(out, vals), 1):
            v = v[:1].upper() + v[1:]
            p["l"] = (f"{i} · {v}" if dup else v) if v else f"Позиция {i}"
    return out


async def order_data(order: dict) -> dict:
    stage = 0
    for i, st in enumerate(STAGES):
        if order.get(st[0]):
            stage = i
    head, lead = STAGES[stage][2], STAGES[stage][3]
    if order.get("cdek_final") == "NOT_DELIVERED":
        head, lead = NOT_DELIVERED
    where, who = _delivery(order.get("summary") or "")
    return {
        "n": _short_code(order),
        "k": stage,
        "s": [[st[1], _date(order.get(st[0]))] for st in STAGES],
        "h": head,
        "l": lead,
        "o": await _positions(order),
        "t": _total(order.get("summary") or ""),
        "d": _address(where),
        "r": who.split(",")[0],
        "c": order.get("cdek_number") or "",
    }


_CSS = """*{box-sizing:border-box;margin:0}
body{background:#0a0a0a;color:#f2f2f2;font:14px/1.35 -apple-system,system-ui,'Segoe UI',Roboto,sans-serif;padding:12px 14px 6px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.eb{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#7a7a7a}.eb.r{color:#e10600}
.pill{border:1px solid #262626;border-radius:99px;padding:3px 11px;font-size:13px;font-weight:600}
.ld{color:#a3a3a3;margin-top:7px;font-size:13px}
.st{display:flex;gap:3px;margin-top:10px}.st div{flex:1;min-width:0}
.st i{display:block;height:3px;border-radius:2px;background:#262626}.st .on i{background:#e10600}
.st b{display:block;font-size:11px;font-weight:600;margin-top:5px;color:#7a7a7a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.st .on b{color:#f2f2f2}
.st small{font-size:11px;color:#7a7a7a}
.cd{background:#141414;border:1px solid #262626;border-radius:18px;padding:10px 14px;margin-top:9px}
.hd{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px}.hd b{font-weight:700;white-space:nowrap}
.tb{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 4px}
.tab{border:1px solid #262626;border-radius:99px;padding:4px 11px;font-size:13px;color:#a3a3a3;cursor:pointer}
.tab.on{background:#f2f2f2;border-color:#f2f2f2;color:#0a0a0a;font-weight:600}
.pn{font-weight:700;font-size:15px;margin:2px 0 3px}
.rw{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-top:1px solid #1f1f1f;font-size:13px}
.rw span{color:#7a7a7a}.rw b{font-weight:600;text-align:right}
.sw{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;border:1px solid #3a3a3a}
.mo{cursor:pointer}.mo span,.mo b{color:#a3a3a3}
.ad{font-size:13px;padding:1px 0 6px}.ad span{color:#7a7a7a}
.tr{display:flex;align-items:center;justify-content:space-between;gap:10px;padding-top:6px;border-top:1px solid #1f1f1f}
.tr div{font-weight:600;font-size:15px;letter-spacing:.02em;min-width:0;overflow:hidden;text-overflow:ellipsis}.tr .bt{flex:none}
.bt{border-radius:99px;padding:8px 16px;font-weight:600;font-size:13px;background:#e10600;color:#fff;cursor:pointer;white-space:nowrap}"""

# Кнопки — div: формы в виджете вырезаются, onclick работает на любом элементе.
# Ссылка открывается через TelegramWidget.openLink (Telegram спросит подтверждение)
_JS = r"""var W=window.TelegramWidget,F=0,P=0;
function e(x){return String(x).replace(/[&<>"]/g,function(c){return"&#"+c.charCodeAt(0)+";"})}
function H(f,a){try{W.HapticFeedback[f](a)}catch(_){}}
function R(){var o=D,h='<div class="top"><span class="eb r">Заказ №'+e(o.n)+'</span><span class="pill">MALIMABI</span></div>'+S;
h+='<p class="ld">'+e(o.l)+'</p><div class="st">';
o.s.forEach(function(s,i){h+='<div class="'+(i<=o.k?"on":"")+'"><i></i><b>'+e(s[0])+'</b><small>'+(i<=o.k?e(s[1]):"&nbsp;")+'</small></div>'});
var p=o.o[P],q=p.q,many=q.length>4;if(many&&!F)q=q.slice(0,3);
h+='</div><div class="cd"><div class="hd"><span class="eb">Что в заказе</span><b>'+e(o.t)+'</b></div>';
if(o.o.length>1){h+='<div class="tb">';o.o.forEach(function(x,i){h+='<div class="tab'+(i==P?' on':'')+'" onclick="T('+i+')">'+e(x.l)+'</div>'});h+='</div>'}
h+='<div class="pn">'+e(p.p)+'</div>';
q.forEach(function(a){h+='<div class="rw"><span>'+e(a[0])+'</span><b'+(a[2]&&!a[3]?' style="color:'+a[2]+'"':'')+'>'+(a[2]?'<i class="sw" style="background:'+a[2]+'"></i>':'')+e(a[1])+'</b></div>'});
if(many)h+='<div class="rw mo" onclick="F=!F;R()"><span>'+(F?'Свернуть':'Ещё '+(p.q.length-3))+'</span><b>'+(F?'▴':'▾')+'</b></div>';
h+='</div>';
if((o.d||o.c)&&!F){h+='<div class="cd"><div class="eb">Пункт выдачи СДЭК</div>';
if(o.d)h+='<div class="ad">'+e(o.d)+(o.r?'<br><span>'+e(o.r)+'</span>':'')+'</div>';
if(o.c)h+='<div class="tr"><div>'+e(o.c)+'</div><div class="bt" onclick="O()">Отследить</div></div>';
h+='</div>'}
document.getElementById("L").innerHTML=h}
function T(i){if(i!=P){P=i;F=0;H("selectionChanged");R()}}
function O(){H("impactOccurred","light");try{W.openLink(U)}catch(_){}}
R();if(W&&W.ready)W.ready();"""


def build_html(data: dict) -> str:
    def page(d: dict) -> str:
        payload = json.dumps(d, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        svg = headline_svg(d["h"]).replace("</", "<\\/")
        url = json.dumps(TRACK_URL.format(d["c"]) if d["c"] else "")
        return (f"<style>{_CSS}</style><div id=\"L\"></div><script>var D={payload},"
                f"S='{svg}',U={url};{_JS}</script>")
    out = page(data)
    # Не влезло — укорачиваем ответы, а не выкидываем позиции
    n = 40
    while not _fits(out) and n >= 10:
        data = dict(data, o=[dict(p, q=[[a[0], a[1][:n], *a[2:]] for a in p["q"]])
                             for p in data["o"]])
        out, n = page(data), n - 10
    return out


async def send(bot, chat_id: int, order_id: int) -> int | None:
    order = await db.get_order(order_id)
    if not order:
        return None
    page = build_html(await order_data(order))
    try:
        sent = await api(bot, "sendWidget", chat_id=chat_id, html=page)
    except Exception as e:
        logger.warning(f"sendWidget «Мой заказ» {order_id} → {chat_id}: {e}")
        return None
    return sent["message_id"]


@router.message(Command("mywidget"))
async def cmd_mywidget(message: Message, command: CommandObject):
    """Пробник: /mywidget <id заказа> — только админам."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /mywidget <id заказа>")
        return
    if not await send(message.bot, message.chat.id, int(arg)):
        await message.answer("Telegram не принял виджет — подробности в логе.")
