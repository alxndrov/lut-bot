"""
/orders и /sentorders в админском боте — интерактивным сообщением
(HTML-виджет Bot API 10.4).

Один виджет на весь список: вкладки с номерами заказов сверху (страницами
по 6), ниже — выбранный заказ целиком: этапы полосой, ответы клиента,
доставка, кнопки «Распечатал» / «🏷 Штрихкод СДЭК» / «Заказ отправил» /
отмена отметок и «📄 Карточка заказа» (за повтором и возвратом).

Как это устроено
- Разметка и неизменное (номер, товар, ответы, доставка) зашиты в HTML
  при отправке; изменяемое — отметки печати и отправки — лежит в chat_data
  сообщения строкой «id:маска:отправлен;…» (не больше 1024 байт).
- HTML у Telegram не больше 4096 байт в сжатом виде — это 5–6 заказов
  с деталями. Детали остальных виджет подгружает запросом при выборе
  вкладки («d:141» → ответ с деталями, до 1024 байт).
- Кнопка в виджете шлёт боту widget_query («p:141:1»). Мы прогоняем его
  через те же обработчики, что и обычные кнопки карточки (order_actions),
  подсунув им вместо CallbackQuery заглушку, — права, учёт расходников,
  пуш об отзыве и синхронизация с таблицей те же самые.
- Состояние перерисовывается editMessageWidgetData — и после нажатия
  в виджете, и когда заказ отметили кнопкой в обычной карточке
  (order_actions._sync_order_messages → refresh).

aiogram 3.15 про виджеты не знает: методы зовём напрямую, а widget_query
ловим в outer-middleware на уровне Update (неизвестный тип апдейта
aiogram иначе молча пропускает).

Ограничения Telegram: виджет — только в личке с ботом, переслать нельзя,
пузырь не выше 600 px и сам не прокручивается. Если Telegram виджет
не принял — показываем прежний текстовый список (он же /orderslist,
/sentlist).
"""
import html
import json
import logging
import re
import zlib

import aiohttp
from aiogram import BaseMiddleware, Router
from aiogram.filters import Command
from aiogram.types import Message, User

import config
import database as db

router = Router()
logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
HTML_ZLIB_LIMIT = 4000      # у Telegram 4096 байт после zlib -9, держим запас
ANSWER_LIMIT = 1024         # ответ на запрос виджета
LIST_HEAD = {"work": "📋 Заказы в работе", "sent": "📦 Отправленные", "new": ""}


async def api(bot, method: str, **params):
    """Вызов метода Bot API, которого нет в aiogram."""
    body = {k: v for k, v in params.items() if v is not None}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post(API.format(token=bot.token, method=method), json=body) as r:
            data = await r.json(content_type=None)
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or str(data))
    return data.get("result")


# ── что показываем ────────────────────────────────────────────────────

def _plain(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _cut(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


# Цвета пластика словами — как подписывает их services/answer_labels.py
# (нынешняя палитра и старая, до 01.10.2026). Ответ в «Цвет …» красим
# в сам цвет; слишком тёмный или светлый для любой темы — с обводкой.
COLORS = {
    "черный": "#1f1f1f", "чёрный": "#1f1f1f", "розовый блестящий": "#ff8fcf",
    "розовый": "#ff7eb3", "красный": "#e53935", "оливковый": "#8a8f2e",
    "синий глубокий": "#173b8f", "синий": "#2563eb", "фиолетовый с блёстками": "#a259d9",
    "фиолетовый": "#8e44ad", "белый с точками": "#f4f4f4", "белый": "#ffffff",
    "голубой": "#4fc3f7", "оранжевый": "#ff8a1f", "серый глубокий": "#4b4b4b",
    "серый": "#8c8c8c", "жёлтый": "#f5c518", "желтый": "#f5c518",
    "богатый зелёный": "#0b6e3a", "зелёный": "#2e9e44", "зеленый": "#2e9e44",
    "бежевый темнее": "#b89b72", "бежевый": "#d9c3a0", "золотой": "#d4a017",
}


def _color(question: str, answer: str) -> tuple[str, int] | None:
    """(#цвет, нужна ли обводка) для ответа на вопрос про цвет."""
    if "цвет" not in question.lower():
        return None
    low = answer.lower()
    name = max((c for c in COLORS if c in low), key=len, default=None)
    if not name:
        return None
    hx = COLORS[name]
    r, g, b = (int(hx[i:i + 2], 16) for i in (1, 3, 5))
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return hx, int(lum < 0.2 or lum > 0.85)


def _answers(summary: str) -> list[list]:
    """Ответы клиента из текста заказа: [[вопрос коротко, ответ(, цвет, обводка)], …]."""
    out = []
    for m in re.finditer(r"<b>\d+\.\s*(.*?)</b>\n(.*?)(?=\n<b>|\n\n|$)", summary or "", re.S):
        q = _plain(m.group(1)).split("\n")[0].rstrip("?").strip()
        q = re.sub(r"^(Выберите|Напишите|Укажите)\s+", "", q)
        q = re.split(r"\s+(?:и напишите|—|-|\()", q)[0]
        a = _plain(m.group(2))
        item = [_cut(q[:1].upper() + q[1:], 34), _cut(a, 60)]
        col = _color(q, a)
        if col:
            item += list(col)
        out.append(item)
    return out


def _delivery(summary: str) -> tuple[str, str]:
    """(куда, получатель) из блока «🚚 Доставка»."""
    m = re.search(r"🚚 <b>Доставка:</b>\n(.*?)(?:\n(Получатель:.*?))?(?:\n|$)", summary or "")
    if not m:
        return "", ""
    where = _plain(m.group(1))
    where = re.sub(r"\s*\(код ПВЗ:[^)]*\)", "", where)
    where = re.sub(r"\b\d{6},\s*", "", where).replace("пункт выдачи: ", "ПВЗ, ")
    parts = [p for p in where.split(", ") if not re.fullmatch(r"\d{6}|Россия", p)
             and not re.search(r"област|край|республик|округ", p, re.I)]
    who = _plain(m.group(2) or "").replace("Получатель: ", "")
    return _cut(", ".join(parts).replace("ПВЗ, ", "ПВЗ "), 80), _cut(who, 60)


def _details(order: dict) -> dict:
    """Ответы клиента и доставка — то, что раскрывается под заказом."""
    client = (f"@{order['username']}" if order.get("username")
              else order.get("first_name") or f"id:{order.get('user_id')}")
    where, who = _delivery(order.get("summary") or "")
    det = {"q": _answers(order.get("summary") or ""), "c": client,
           "d": where, "r": who, "k": order.get("cdek_number") or ""}
    if order.get("receipt_line"):   # чек «Мой налог» за оплату ЮKassa
        det["rc"] = _cut(_plain(order["receipt_line"]), 120)
    return det


def _fit_details(det: dict, limit: int = ANSWER_LIMIT - 40) -> str:
    """Детали в JSON не длиннее ответа виджету: режем ответы клиента."""
    n = 60
    while True:
        out = json.dumps(det, ensure_ascii=False, separators=(",", ":"))
        if len(out.encode()) <= limit or n < 10:
            return out
        n -= 10
        det = dict(det, q=[[it[0], _cut(it[1], n), *it[2:]] for it in det["q"]],
                   r=_cut(det["r"], n), d=_cut(det["d"], n + 20))


def _static(order: dict, has_card: bool = False) -> dict:
    """Неизменная часть заказа — зашивается в HTML виджета.

    has_card — у заказа есть прежняя карточка-сообщение в этом чате:
    тогда кнопка «📄 Карточка», иначе повтор и возврат — своей кнопкой."""
    from handlers import order_actions as oa
    from handlers.refunds import _rub, refund_rows
    total = oa._order_positions(order)
    w, sw = oa._words(order), oa._ship(order)
    money = re.search(r"💵\s*([^\n]+)", order.get("summary") or "")
    title = re.sub(r"^\W+\s*", "", order.get("product_name") or "")
    shipped = ""
    if order.get("shipped_at"):
        shipped = " · ".join(x for x in (oa._msk(order["shipped_at"]),
                                         order.get("shipped_by_name")) if x)
    refund = ""
    if order.get("refunded_at"):
        refund = f"💸 Возврат {_rub(order['refund_amount'])}"
    return {
        "i": order["id"],
        "n": oa._order_num(order),
        "t": _cut(("🔁 Повтор · " if order.get("repeat_of_order_id") else "") + title, 40),
        "m": _plain(money.group(1)) if money else "",
        "h": shipped,
        "f": refund,
        "z": 1 if order.get("refund_full") else 0,   # полный возврат — заказ отменён
        "b": 1 if order.get("cdek_uuid") else 0,     # есть накладная — есть и штрихкод
        "cd": int(has_card),
        "rf": int(bool(refund_rows(order))),         # есть что делать с возвратом
        # Слова этапов: печать или сборка, отправка или выдача
        "w": [w["icon"], w["do"], w["done"].capitalize(), sw["icon"], sw["btn"],
              sw["did"], sw["do"]],
        # По позициям: значок, «Распечатал»/«Собрал», товар
        "p": [[oa._words(order, [p])["icon"], oa._words(order, [p])["did"],
               oa._products_label(order, [p], total).strip(" ()")]
              for p in range(1, total + 1)],
    }


async def _full_order(order_id: int) -> dict | None:
    """Заказ с ником клиента и товаром — как в db.get_orders (get_order их не даёт)."""
    order = await db.get_order(order_id)
    if not order:
        return None
    u = await db.get_user(order["user_id"]) or {}
    p = await db.get_product(order["product_id"]) or {}
    return dict(order, username=u.get("username"), first_name=u.get("first_name"),
                product_name=order.get("product_name") or p.get("name"))


async def _chat_data(order_ids: list[int]) -> str:
    """chat_data виджета. У виджета одного заказа — JSON со свежими полями:
    трек, возврат, чек и отметка об отправке появляются уже после отправки."""
    state = await _state(order_ids)
    if len(order_ids) != 1:
        return state
    order = await _full_order(order_ids[0])
    if not order:
        return state
    st = _static(order)
    fresh = {k: st[k] for k in ("h", "f", "z", "b", "rf")}
    base = {"s": state, "o": {str(order["id"]): fresh}}
    room = ANSWER_LIMIT - len(json.dumps(base, ensure_ascii=False).encode()) - 8
    fresh["x"] = json.loads(_fit_details(_details(order), room))
    return json.dumps(base, ensure_ascii=False, separators=(",", ":"))


async def _state(order_ids: list[int]) -> str:
    """Изменяемая часть: «id:маска печати по позициям:отправлен:накладная;…»."""
    from handlers import order_actions as oa
    await oa._ensure_admin_names()
    parts = []
    for oid in order_ids:
        order = await db.get_order(oid)
        if not order:
            continue
        prints = await db.get_order_prints(oid)
        printed = oa._print_map(order, prints)
        total = oa._order_positions(order)
        mask = "".join("1" if p in printed else "0" for p in range(1, total + 1))
        parts.append(f"{oid}:{mask}:{1 if order.get('shipped_at') else 0}"
                     f":{1 if order.get('cdek_uuid') else 0}")
    return ";".join(parts)


_CSS = """*{box-sizing:border-box;margin:0}
body{font:15px/1.35 system-ui,sans-serif;color:var(--tg-theme-text-color);padding:10px 10px 6px}
h3{font-size:16px;margin:0 2px 8px}
.tb{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.tab{padding:6px 10px;border-radius:10px;font-size:14px;background:var(--tg-theme-secondary-bg-color)}
.tab.on{background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color)}
.o{background:var(--tg-theme-secondary-bg-color);border-radius:14px;padding:10px 12px;margin-bottom:8px}
.hd{display:flex;justify-content:space-between;align-items:center;gap:8px}
.n{font-weight:600}.s{font-size:13px;color:var(--tg-theme-hint-color)}
.ch{font-size:12px;padding:3px 8px;border-radius:10px;background:var(--tg-theme-bg-color);white-space:nowrap}
.bar{display:flex;gap:4px;margin:10px 0 3px}
.bar i{flex:1;height:4px;border-radius:2px;background:var(--tg-theme-hint-color);opacity:.25}
.bar i.on{background:var(--tg-theme-button-color);opacity:1}
.lb{display:flex;justify-content:space-between;font-size:12px;color:var(--tg-theme-hint-color)}
.dt{margin-top:8px;font-size:13px}.dt div{margin-top:3px}
.dt span{color:var(--tg-theme-hint-color)}
.b{display:block;width:100%;margin-top:8px;padding:9px;border:0;border-radius:10px;font:inherit;font-weight:600;text-align:center;background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color)}
.b.g{background:var(--tg-theme-bg-color);color:var(--tg-theme-link-color);font-weight:400}
.r{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.r .b{flex:1 1 40%;margin:0;padding:8px 6px;font-size:13px}
.cl{cursor:pointer}.cl:hover{text-decoration:underline}
.cv{font-weight:600}.cv.ol{text-shadow:0 0 1px var(--tg-theme-hint-color),0 0 1px var(--tg-theme-hint-color)}
#t{font-size:13px;text-align:center;color:var(--tg-theme-hint-color);min-height:17px}"""

# Пузырь виджета не выше 600 px и сам не прокручивается — поэтому заказы
# вкладками по номерам сверху (по 6 на страницу, листаются ‹ ›; на первой
# странице нет «‹», на последней — «›»), а ниже — один выбранный заказ
# целиком; повторное нажатие на его номер сворачивает. Детали заказов,
# не влезших в HTML, подгружаются запросом «d:id» при выборе вкладки.
# «Куда» и «Кому» скрыты, пока не нажмут на «Клиент:».
# chat_data — строка состояний «id:маска:отправлен:накладная;…» или JSON
# {"s": та же строка, "o": {id: свежие поля заказа}} — у виджета одного
# заказа (новый заказ), где трек, возврат и чек появляются позже.
# Кнопки — div, а не <button>: формы в профиле виджета вырезаются,
# а onclick на любом элементе работает
_JS = r"""var W=window.TelegramWidget,P=6,sel=D[0].i,pg=0,busy=0,X={},CL=0;
D.forEach(function(o){if(o.x)X[o.i]=o.x});
function e(x){return String(x).replace(/[&<>"]/g,function(c){return"&#"+c.charCodeAt(0)+";"})}
function st(){var m={},c=String((W&&W.chatData)||"");
if(c.charAt(0)=="{"){try{var j=JSON.parse(c);c=j.s||"";if(j.o)D.forEach(function(o){var u=j.o[o.i];if(u){for(var k in u)if(k!="x")o[k]=u[k];if(u.x)X[o.i]=u.x}})}catch(_){c=""}}
c.split(";").forEach(function(x){var p=x.split(":");if(p[0])m[p[0]]={m:p[1]||"",s:p[2]=="1",b:p[3]=="1"}});return m}
function B(t,d,g){return'<div class="b'+(g?' g':'')+'" onclick="A(\''+d+'\')">'+e(t)+'</div>'}
function R(){var S=st(),h="",o;
function Y(o){var x=S[o.i]||{m:"",s:false,b:false};x.d=x.m&&x.m.indexOf("0")<0;x.k=x.s?2:x.d?1:0;x.z=o.z&&!x.s;x.c=x.z?"💸":x.s?"✅":x.d?o.w[3]:o.w[0];return x}
D.forEach(function(q){if(q.i==sel)o=q});
if(D.length>1){var np=Math.ceil(D.length/P);h+='<div class="tb">';
if(pg>0)h+='<div class="tab" onclick="G('+(pg-1)+')">‹</div>';
D.slice(pg*P,pg*P+P).forEach(function(o){h+='<div class="tab'+(o.i==sel?' on':'')+'" onclick="T('+o.i+')">'+Y(o).c+' №'+e(o.n)+'</div>'});
if(pg<np-1)h+='<div class="tab" onclick="G('+(pg+1)+')">›</div>';h+='</div>'}
if(o){var x=Y(o),w=o.w,v=X[o.i],ch=x.z?"💸 Отменён":x.s?"✅ "+w[5]:x.d?w[3]+" "+w[6]:w[0]+" "+w[1];
h+='<div class="o"><div class="hd"><div><div class="n">№'+e(o.n)+' · '+e(o.m)+'</div><div class="s">'+e(o.t)+'</div></div><div class="ch">'+e(ch)+'</div></div>';
h+='<div class="bar"><i class="on"></i><i class="'+(x.k>0?"on":"")+'"></i><i class="'+(x.k>1?"on":"")+'"></i></div><div class="lb"><span>Оплачен</span><span>'+e(w[2])+'</span><span>'+e(w[5])+'</span></div>';
h+='<div class="dt">';if(x.s&&o.h)h+='<div><span>'+e(w[5])+':</span> '+e(o.h)+'</div>';if(o.f)h+='<div>'+e(o.f)+'</div>';
if(!v)h+='<div class="s">Загружаю детали…</div>';else if(v.e)h+='<div class="s">Детали не загрузились — нажми на номер ещё раз</div>';else{
v.q.forEach(function(a){h+='<div><span>'+e(a[0])+':</span> '+(a[2]?'<b class="cv'+(a[3]?' ol':'')+'" style="color:'+a[2]+'">'+e(a[1])+'</b>':e(a[1]))+'</div>'});
h+='<div><span class="cl" onclick="Q()">Клиент:</span> '+e(v.c)+'</div>';if(CL){if(v.d)h+='<div><span>Куда:</span> '+e(v.d)+'</div>';if(v.r)h+='<div><span>Кому:</span> '+e(v.r)+'</div>'}
if(v.k)h+='<div onclick="C(\''+v.k+'\')"><span>СДЭК:</span> '+e(v.k)+' ⧉</div>';if(v.rc)h+='<div>'+e(v.rc)+'</div>'}h+='</div>';
var pb="",gb="";function K(t,d,g){if(g)gb+=B(t,d,1);else pb+=B(t,d)}
if(x.s)K("↩️ Отменить «"+w[5].toLowerCase()+"»","x:"+o.i,1);else if(!x.z){var n=o.p.length;
o.p.forEach(function(p,j){var nm=n>1?" поз."+(j+1)+(p[2]?" ("+p[2]+")":""):"";
x.m.charAt(j)=="1"?K("↩️ Снять «"+p[1].toLowerCase()+"»"+nm,"u:"+o.i+":"+(j+1),1):K(p[0]+" "+p[1]+nm,"p:"+o.i+":"+(j+1))});
if(x.d&&(x.b||o.b))K("🏷 Штрихкод СДЭК","b:"+o.i,1);if(x.d)K(w[4],"s:"+o.i)}
if(o.cd)K(x.s?"📄 Повтор, возврат":"📄 Карточка","c:"+o.i,1);else if(o.rf||x.s)K(x.s?(o.rf?"🔁 Повтор · 💸 Возврат":"🔁 Повтор"):"💸 Возврат","m:"+o.i,1);
h+=(gb?'<div class="r">'+gb+'</div>':'')+pb+'</div>'}
document.getElementById("L").innerHTML=h}
function H(f,a){try{W.HapticFeedback[f](a)}catch(_){}}
function L(i){if(!W)return;W.request("d:"+i).then(function(a){try{X[i]=JSON.parse(a.result)}catch(_){X[i]={e:1}}R()},function(er){X[i]={e:1};M("Не вышло: "+er.code);R()})}
function T(i){M("");H("selectionChanged");if(sel==i){sel=0;R();return}sel=i;if(!X[i]||X[i].e)L(i);R()}
function G(p){pg=p;M("");R()}
function Q(){CL=!CL;R()}
function M(t){document.getElementById("t").textContent=t}
function C(k){W&&W.copyText(k);M("Трек скопирован")}
function A(d){if(!W||busy)return;busy=1;M("…");W.request(d).then(function(a){busy=0;var r={};try{r=JSON.parse(a.result)}catch(_){}
M(r.t||"");H("notificationOccurred",r.a?"warning":"success")},function(er){busy=0;M("Не вышло: "+er.code)})}
if(W){W.onEvent("chatDataChanged",R);W.ready&&W.ready()}R();"""


def _page(data: list[dict], head: str) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (f"<style>{_CSS}</style><h3>{html.escape(head)}</h3><div id=\"L\"></div>"
            f"<div id=\"t\"></div><script>var D={payload};{_JS}</script>")


def _fits(page: str) -> bool:
    return len(zlib.compress(page.encode(), 9)) <= HTML_ZLIB_LIMIT


def build_html(orders: list[dict], mode: str = "work", total: int | None = None,
               cards: set | None = None) -> tuple[str, int]:
    """HTML виджета и сколько заказов в него влезло.

    Сначала выкидываем детали с конца списка (их подгрузит запрос),
    и только если не влезает и без них — сами заказы.
    cards — заказы, у которых в этом чате есть прежняя карточка.
    """
    cards = cards or set()
    data = [dict(_static(o, o["id"] in cards), x=_details(o)) for o in orders]
    total = total or len(orders)

    def head():
        if mode == "new":
            return "🧾 Новый заказ" if not orders[0].get("repeat_of_order_id") else "🔁 Повтор заказа"
        h = f"{LIST_HEAD[mode]}: {total}"
        if len(data) < total:
            h += f" (тут {len(data)})"
        return h

    for keep in range(len(data), 0, -1):
        for j, item in enumerate(data):
            if j >= keep:
                item.pop("x", None)
        page = _page(data, head())
        if _fits(page):
            return page, len(data)
    while len(data) > 1:
        data.pop()
        page = _page(data, head())
        if _fits(page):
            return page, len(data)
    return _page(data, head()), 1


# ── команды ───────────────────────────────────────────────────────────

async def _send_list(message: Message, orders: list[dict], mode: str, batch: str,
                     total: int | None = None) -> bool:
    """Шлёт виджет со списком вместо прошлого. False — Telegram не принял."""
    page, fit = build_html(orders, mode, total, await _real_cards(orders, message.chat.id))
    ids = [o["id"] for o in orders[:fit]]
    try:
        sent = await api(message.bot, "sendWidget", chat_id=message.chat.id, html=page,
                         chat_data=await _chat_data(ids), allow_queries=True)
    except Exception as e:
        logger.warning(f"sendWidget {mode}: {e}")
        return False
    await db.add_order_widget(message.chat.id, sent["message_id"], ids)
    await db.replace_admin_command_messages(batch, message.chat.id, message.from_user.id,
                                            [sent["message_id"]])
    return True


async def _real_cards(orders: list[dict], chat_id: int) -> set[int]:
    """Заказы, у которых в этом чате есть карточка-сообщение (не виджет)."""
    out = set()
    for o in orders:
        if any(m["chat_id"] == chat_id for m in await db.get_order_messages(o["id"])):
            out.add(o["id"])
    return out


async def send_new_order(bot, chat_id: int, order_id: int) -> int | None:
    """Новый заказ — виджетом одного заказа. message_id или None (не принял)."""
    order = await _full_order(order_id)
    if not order:
        return None
    from handlers import order_actions as oa
    await oa._ensure_admin_names()
    page, _ = build_html([order], "new")
    try:
        sent = await api(bot, "sendWidget", chat_id=chat_id, html=page,
                         chat_data=await _chat_data([order_id]), allow_queries=True)
    except Exception as e:
        logger.warning(f"sendWidget new order {order_id} → {chat_id}: {e}")
        return None
    await db.add_order_widget(chat_id, sent["message_id"], [order_id])
    return sent["message_id"]


async def _clear_previous(message: Message, batch: str):
    """Прошлый список в этом чате убираем: живёт один актуальный."""
    from handlers import order_actions as oa
    for msg_id in await db.get_admin_command_messages(batch, message.chat.id,
                                                       message.from_user.id):
        await db.delete_order_widget(message.chat.id, msg_id)
    await oa._delete_previous_command_batch(message, batch)


# /myorders, /allorders, /worders — прежние имена, разосланы в переписке
@router.message(Command("orders", "myorders", "allorders", "worders"))
async def cmd_orders(message: Message):
    """Заказы в работе — одним виджетом, старые сверху."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    from handlers import order_actions as oa
    await oa._ensure_admin_names()
    todo = await db.get_orders(only_unshipped=True)
    if not todo:
        return await oa.cmd_orders_list(message)
    todo.sort(key=lambda o: o.get("created_at") or "")
    await _clear_previous(message, "orders")
    if not await _send_list(message, todo, "work", "orders"):
        await oa.cmd_orders_list(message)


@router.message(Command("sentorders"))
async def cmd_sent_orders(message: Message):
    """Отправленные — свежие первыми, последние SENT_LIMIT."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    from handlers import order_actions as oa
    await oa._ensure_admin_names()
    orders = [o for o in await db.get_orders() if o.get("shipped_at")]
    if not orders:
        await message.answer("📦 Отправленных заказов пока нет")
        return
    orders.sort(key=lambda o: o.get("shipped_at") or "", reverse=True)
    await _clear_previous(message, "sentorders")
    if not await _send_list(message, orders[:SENT_LIMIT], "sent", "sentorders",
                            total=len(orders)):
        await oa.cmd_sent_orders_list(message)


SENT_LIMIT = 24     # четыре страницы вкладок


# ── нажатия в виджете ─────────────────────────────────────────────────

class _Tap:
    """Заглушка CallbackQuery для обработчиков order_actions:
    им нужны data, from_user, bot и answer — ответ мы забираем себе."""

    def __init__(self, bot, user: User, data: str, message=None):
        self.bot, self.from_user, self.data = bot, user, data
        self.message = message
        self.note, self.alert = "", False

    async def answer(self, text: str | None = None, show_alert: bool = False, **_):
        self.note, self.alert = text or "", show_alert


class _WidgetChat:
    """Вместо callback.message: штрихкод СДЭК обработчик присылает
    «ответом на сообщение» — у нас это ответ на сам виджет."""

    def __init__(self, bot, chat_id: int, message_id: int | None):
        self.bot, self.chat_id, self.message_id = bot, chat_id, message_id

    async def reply_document(self, document, caption: str | None = None, **_):
        return await self.bot.send_document(
            self.chat_id, document, caption=caption, reply_to_message_id=self.message_id,
            allow_sending_without_reply=True)

    async def answer(self, text: str, **kw):
        return await self.bot.send_message(self.chat_id, text, **kw)


_ACTIONS = {"p": "order_printed", "u": "order_unprint",
            "s": "order_shipped", "x": "order_unship", "b": "order_barcode"}


async def _show_card(bot, chat_id: int, widget_msg: int | None, order_id: int) -> str:
    """«📄 Карточка заказа»: ссылка ответом на карточку в этом чате,
    а если её тут нет — свежая карточка со всеми кнопками."""
    from handlers import order_actions as oa
    order = await db.get_order(order_id)
    if not order:
        return "Заказ не найден."
    card = next((m["message_id"] for m in await db.get_order_messages(order_id)
                 if m["chat_id"] == chat_id), None)
    num = oa._order_num(order)
    if card:
        try:
            sent = await bot.send_message(chat_id, f"📄 Карточка заказа №{num} ↑",
                                          reply_to_message_id=card)
            await db.add_admin_command_message("orders", chat_id, chat_id, sent.message_id)
            return "Карточка — сообщением ниже, нажми на цитату"
        except Exception:
            pass   # карточку удалили — пришлём новую
    prints = await db.get_order_prints(order_id)
    sent = await bot.send_message(chat_id, oa._order_text(order, prints), parse_mode="HTML",
                                  reply_markup=oa._order_keyboard(order, chat_id, prints))
    await db.add_order_message(order_id, chat_id, sent.message_id)
    return "Карточка — сообщением ниже"


async def _show_more(bot, chat_id: int, widget_msg: int | None, order_id: int) -> str:
    """Повтор и возврат у заказа без карточки: кнопки отдельным сообщением
    ответом на виджет — эти действия в несколько шагов, в виджет не влезают."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from handlers import order_actions as oa
    from handlers.refunds import refund_rows
    order = await db.get_order(order_id)
    if not order:
        return "Заказ не найден."
    rows = []
    if order.get("shipped_at"):
        rows.append([InlineKeyboardButton(text="🔁 Повторить заказ",
                                          callback_data=f"order_repeat:{order_id}")])
    rows += refund_rows(order)
    if not rows:
        return "Тут нечего делать: возврат и повтор недоступны."
    await bot.send_message(chat_id, f"Заказ №{oa._order_num(order)}: повтор и возврат",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                           reply_to_message_id=widget_msg, allow_sending_without_reply=True)
    return "Кнопки — сообщением ниже"


async def handle_query(bot, q: dict):
    note, alert, raw = "", False, None
    try:
        user = User.model_validate(q["from"])
        kind, _, rest = (q.get("data") or "").partition(":")
        chat, msg_id = (q.get("chat") or {}).get("id"), q.get("message_id")
        if user.id not in config.ADMIN_IDS:
            note, alert = "Только для администраторов.", True
        elif kind == "d":
            order = await db.get_order(int(rest))
            if order:   # get_order без ника клиента — добираем
                u = await db.get_user(order["user_id"]) or {}
                order = dict(order, username=u.get("username"), first_name=u.get("first_name"))
            raw = _fit_details(_details(order)) if order else '{"e":1}'
        elif kind == "c" and chat:
            note = await _show_card(bot, chat, msg_id, int(rest))
        elif kind == "m" and chat:
            note = await _show_more(bot, chat, msg_id, int(rest))
        elif kind not in _ACTIONS:
            note, alert = "Неизвестная кнопка — пришли /orders заново.", True
        else:
            from handlers import order_actions as oa
            handler = {"p": oa.cb_order_printed, "u": oa.cb_order_unprint,
                       "s": oa.cb_order_shipped, "x": oa.cb_order_unship,
                       "b": oa.cb_order_barcode}[kind]
            tap = _Tap(bot, user, f"{_ACTIONS[kind]}:{rest}",
                       _WidgetChat(bot, chat, msg_id) if chat else None)
            await handler(tap)
            note, alert = tap.note, tap.alert
            # Обработчик мог ответить отказом, ничего не меняя, — тогда
            # синхронизации не было, а виджет мог отстать: подтягиваем
            if chat and msg_id and kind != "b":
                await _redraw(bot, chat, msg_id)
    except Exception as e:
        logger.exception(f"widget_query {q.get('data')}: {e}")
        note, alert = "Ошибка, посмотри в карточке заказа.", True
    if raw is None:
        raw = json.dumps({"t": _cut(note, 200), "a": int(alert)}, ensure_ascii=False)
    try:
        await api(bot, "answerWidgetQuery", widget_query_id=q["id"], data=raw)
    except Exception as e:
        logger.warning(f"answerWidgetQuery: {e}")


async def _redraw(bot, chat_id: int, message_id: int):
    w = await db.get_order_widget(chat_id, message_id)
    if not w:
        return
    try:
        await api(bot, "editMessageWidgetData", chat_id=chat_id, message_id=message_id,
                  chat_data=await _chat_data(w["order_ids"]))
    except Exception as e:
        err = str(e)
        if "not found" in err or "can't be edited" in err:
            await db.delete_order_widget(chat_id, message_id)
        elif "not modified" not in err:
            logger.warning(f"order widget {chat_id}/{message_id}: {err}")


async def refresh(bot, order_id: int):
    """Перерисовать все виджеты, где есть этот заказ."""
    for w in await db.get_order_widgets_with(order_id):
        await _redraw(bot, w["chat_id"], w["message_id"])


class WidgetQueryMiddleware(BaseMiddleware):
    """Перехватывает widget_query до того, как aiogram выкинет его как
    неизвестный тип апдейта."""

    async def __call__(self, handler, event, data):
        q = (getattr(event, "model_extra", None) or {}).get("widget_query")
        if q is None:
            return await handler(event, data)
        if str(q.get("data") or "").startswith("fin:"):   # сводка /finance
            from handlers import finance_widget
            await finance_widget.handle_query(data["bot"], q)
        else:
            await handle_query(data["bot"], q)
        return True
