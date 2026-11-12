"""«Мой заказ» — виджет заказа для клиента в стиле мини-приложения MALIMABI.

Стиль — из webapp/index.html (макет Миши): чёрный фон, карточки #141414,
красный акцент #e10600, заголовки широким жирным шрифтом капсом.

Шрифт заголовков целиком в виджет не влезает (лимит — 4 КБ HTML после
zlib, а внешние файлы виджету запрещены). Поэтому в каждую страницу
встраивается подмножество шрифта — только буквы, которые есть в её
заголовках: латиница и цифры из Anybody, кириллица из Unbounded, как в
приложении. Если Telegram шрифт не примет — заголовки системным жирным.

Пока это пробник: шлём только админам командой /mywidget <номер заказа>.
"""
import base64
import html
import io
import json
import logging
import re
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

# (заголовок, пояснение) по этапам; дата этапа — из заказа
STAGES = [
    ("created_at", "Оплачен", "В работе",
     "Заказ оплачен и стоит в очереди на изготовление."),
    ("printed_at", "Готов", "Готов",
     "Заказ изготовлен и скоро уедет в СДЭК."),
    ("shipped_at", "Отправлен", "В пути",
     "Посылка едет в пункт выдачи. Когда приедет — пришлю сообщение."),
    ("arrived_notified_at", "В ПВЗ", "Можно забирать",
     "Понадобится паспорт или код из приложения СДЭК."),
]


# ── шрифт заголовков ────────────────────────────────────────────────

@lru_cache(maxsize=64)
def _subset(file: str, chars: str) -> str:
    """woff2 c нужными буквами в base64; пусто — если букв нет или не вышло."""
    if not chars.strip():
        return ""
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
        font = TTFont(str(FONTS / file))
        cmap = font.getBestCmap()
        chars = "".join(c for c in chars if ord(c) in cmap)
        if not chars.strip():
            return ""
        opts = subset.Options()
        opts.flavor = "woff2"
        opts.layout_features = []          # кернинг и лигатуры заголовку не нужны
        opts.name_IDs = []
        opts.notdef_outline = False
        sub = subset.Subsetter(opts)
        sub.populate(text=chars)
        sub.subset(font)
        out = io.BytesIO()
        font.flavor = "woff2"
        font.save(out)
        return base64.b64encode(out.getvalue()).decode()
    except Exception as e:
        logger.warning(f"Шрифт {file}: подмножество не собрать: {e}")
        return ""


def font_css(text: str) -> str:
    """@font-face с буквами из text: Anybody (латиница, цифры) + Unbounded (кириллица)."""
    chars = "".join(sorted(set(text.upper())))
    css = ""
    for name, file in (("A", "anybody-latin.woff2"), ("U", "unbounded-cyrillic.woff2")):
        b64 = _subset(file, chars)
        if b64:
            css += (f"@font-face{{font-family:{name};font-weight:900;"
                    f"src:url(data:font/woff2;base64,{b64}) format('woff2')}}")
    return css


# ── что показываем ──────────────────────────────────────────────────

def _short_code(order: dict) -> str:
    code = order.get("order_code") or ""
    m = re.search(r"(\d+)$", code)
    return m.group(1) if m else str(order["id"])


def _date(ts: str | None) -> str:
    # В базе UTC; для «12.11» сдвиг на 3 часа почти никогда не важен,
    # но вечерние отметки иначе съезжают на день назад
    if not ts:
        return ""
    from datetime import datetime, timedelta
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
    out = []
    for p in parts:
        if not out or out[-1] != p:
            out.append(p)
    return ", ".join(out).replace("помещение", "пом.")


async def order_data(order: dict) -> dict:
    stage = 0
    for i, (field, *_rest) in enumerate(STAGES):
        if order.get(field):
            stage = i
    product = await db.get_product(order["product_id"]) or {}
    name = re.sub(r"^[^\wА-Яа-яЁё]+", "", product.get("name") or "").strip()
    where, who = _delivery(order.get("summary") or "")
    return {
        "n": _short_code(order),
        "k": stage,
        "s": [[s[1], _date(order.get(s[0]))] for s in STAGES],
        "h": STAGES[stage][2],
        "l": STAGES[stage][3],
        "p": name,
        "q": [_answer(a) for a in _answers(order.get("summary") or "")],
        "t": _total(order.get("summary") or ""),
        "d": _address(where),
        "r": who.split(",")[0],
        "c": order.get("cdek_number") or "",
    }


_CSS = """*{box-sizing:border-box;margin:0}
body{background:#0a0a0a;color:#f2f2f2;font:14px/1.35 -apple-system,system-ui,'Segoe UI',Roboto,sans-serif;padding:14px 14px 8px}
.D{font-family:A,U,'Arial Black',sans-serif;font-weight:900;text-transform:uppercase;line-height:.95;letter-spacing:-.01em}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.eb{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#7a7a7a}.eb.r{color:#e10600}
.pill{border:1px solid #262626;border-radius:99px;padding:3px 11px;font-size:13px;font-weight:600}
h1{font-size:26px}
.ld{color:#a3a3a3;margin-top:6px;font-size:13px}
.st{display:flex;gap:4px;margin-top:12px}.st div{flex:1}
.st i{display:block;height:3px;border-radius:2px;background:#262626}.st .on i{background:#e10600}
.st b{display:block;font-size:11px;font-weight:600;margin-top:6px;color:#7a7a7a}.st .on b{color:#f2f2f2}
.st small{font-size:11px;color:#7a7a7a}
.cd{background:#141414;border:1px solid #262626;border-radius:18px;padding:11px 14px;margin-top:10px}
.cd .eb{margin-bottom:4px}.pn{display:flex;justify-content:space-between;gap:10px;font-weight:700;font-size:15px;margin-bottom:4px}
.ad{font-size:13px;padding:2px 0 6px}
.rw{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-top:1px solid #1f1f1f;font-size:13px}
.rw span{color:#7a7a7a}.rw b{font-weight:600;text-align:right}
.tr{display:flex;align-items:center;justify-content:space-between;gap:10px;padding-top:6px;border-top:1px solid #1f1f1f}
.tr code{font:600 15px ui-monospace,Menlo,monospace;letter-spacing:.02em}
.bt{border-radius:99px;padding:8px 14px;font-weight:600;font-size:13px;background:#f2f2f2;color:#0a0a0a;cursor:pointer;white-space:nowrap}
.sw{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;border:1px solid #3a3a3a;vertical-align:0}
.pn span+span{white-space:nowrap}.mo{cursor:pointer}.mo span,.mo b{color:#a3a3a3}
#m{text-align:center;color:#7a7a7a;font-size:12px;min-height:16px;margin-top:8px}"""

# Кнопки — div: формы в виджете вырезаются, onclick работает на любом элементе
_JS = r"""var W=window.TelegramWidget,F=0;
function e(x){return String(x).replace(/[&<>"]/g,function(c){return"&#"+c.charCodeAt(0)+";"})}
function R(){var o=D,h='<div class="top"><span class="eb r">Заказ №'+e(o.n)+'</span><span class="pill">MALIMABI</span></div>';
h+='<h1 class="D">'+e(o.h)+'</h1><p class="ld">'+e(o.l)+'</p><div class="st">';
o.s.forEach(function(s,i){h+='<div class="'+(i<=o.k?"on":"")+'"><i></i><b>'+e(s[0])+'</b><small>'+(i<=o.k?e(s[1]):"&nbsp;")+'</small></div>'});
h+='</div><div class="cd"><div class="eb">Что в заказе</div><div class="pn"><span>'+e(o.p)+'</span><span>'+e(o.t)+'</span></div>';
var q=o.q,many=q.length>4;if(many&&!F)q=q.slice(0,3);
q.forEach(function(a){h+='<div class="rw"><span>'+e(a[0])+'</span><b'+(a[2]&&!a[3]?' style="color:'+a[2]+'"':'')+'>'+(a[2]?'<i class="sw" style="background:'+a[2]+'"></i>':'')+e(a[1])+'</b></div>'});
if(many)h+='<div class="rw mo" onclick="F=!F;R()"><span>'+(F?'Свернуть':'Ещё '+(o.q.length-3))+'</span><b>'+(F?'▴':'▾')+'</b></div>';
h+='</div>';
if((o.d||o.c)&&!F){h+='<div class="cd"><div class="eb">Пункт выдачи СДЭК</div>';
if(o.d)h+='<div class="ad">'+e(o.d)+(o.r?'<br><span style="color:#7a7a7a">'+e(o.r)+'</span>':'')+'</div>';
if(o.c)h+='<div class="tr"><div><div class="eb">Трек СДЭК</div><code>'+e(o.c)+'</code></div><div class="bt" onclick="C()">Скопировать</div></div>';
h+='</div>'}
document.getElementById("L").innerHTML=h+'<div id="m"></div>'}
function C(){try{W.copyText(D.c);W.HapticFeedback.notificationOccurred("success")}catch(_){}document.getElementById("m").textContent="Трек-номер скопирован"}
R();if(W&&W.ready)W.ready();"""


def build_html(data: dict, with_font: bool = True) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    css = _CSS
    if with_font:
        fc = font_css(data["h"])      # широкий шрифт — только у заголовка
        if fc and _fits(f"<style>{fc}{_CSS}</style><div id=L></div><script>var D={payload};{_JS}</script>"):
            css = fc + _CSS
    return f"<style>{css}</style><div id=\"L\"></div><script>var D={payload};{_JS}</script>"


async def send(bot, chat_id: int, order_id: int, with_font: bool = True) -> int | None:
    order = await db.get_order(order_id)
    if not order:
        return None
    data = await order_data(order)
    page = build_html(data, with_font)
    markup = None
    if data["c"]:
        markup = {"inline_keyboard": [[{
            "text": "📍 Отследить в СДЭК",
            "url": f"https://www.cdek.ru/ru/tracking?order_id={data['c']}"}]]}
    try:
        sent = await api(bot, "sendWidget", chat_id=chat_id, html=page, reply_markup=markup)
    except Exception as e:
        logger.warning(f"sendWidget «Мой заказ» {order_id} → {chat_id}: {e}")
        return None
    return sent["message_id"]


@router.message(Command("mywidget"))
async def cmd_mywidget(message: Message, command: CommandObject):
    """Пробник: /mywidget <id заказа> [nofont] — только админам."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    args = (command.args or "").split()
    if not args or not args[0].isdigit():
        await message.answer("Формат: /mywidget <id заказа> [nofont]")
        return
    mid = await send(message.bot, message.chat.id, int(args[0]),
                     with_font="nofont" not in args)
    if not mid:
        await message.answer("Telegram не принял виджет — подробности в логе.")
