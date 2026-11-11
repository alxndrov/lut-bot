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
LIST_HEAD = {"work": "📋 Заказы в работе", "sent": "📦 Отправленные"}


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


def _answers(summary: str) -> list[list[str]]:
    """Ответы клиента из текста заказа: [[вопрос коротко, ответ], …]."""
    out = []
    for m in re.finditer(r"<b>\d+\.\s*(.*?)</b>\n(.*?)(?=\n<b>|\n\n|$)", summary or "", re.S):
        q = _plain(m.group(1)).split("\n")[0].rstrip("?").strip()
        q = re.sub(r"^(Выберите|Напишите|Укажите)\s+", "", q)
        q = re.split(r"\s+(?:и напишите|—|-|\()", q)[0]
        out.append([_cut(q[:1].upper() + q[1:], 34), _cut(_plain(m.group(2)), 60)])
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
    return {"q": _answers(order.get("summary") or ""), "c": client,
            "d": where, "r": who, "k": order.get("cdek_number") or ""}


def _fit_details(det: dict, limit: int = ANSWER_LIMIT - 40) -> str:
    """Детали в JSON не длиннее ответа виджету: режем ответы клиента."""
    n = 60
    while True:
        out = json.dumps(det, ensure_ascii=False, separators=(",", ":"))
        if len(out.encode()) <= limit or n < 10:
            return out
        n -= 10
        det = dict(det, q=[[q, _cut(a, n)] for q, a in det["q"]],
                   r=_cut(det["r"], n), d=_cut(det["d"], n + 20))


def _static(order: dict) -> dict:
    """Неизменная часть заказа — зашивается в HTML виджета."""
    from handlers import order_actions as oa
    from handlers.refunds import _rub
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
        "t": _cut(title, 40),
        "m": _plain(money.group(1)) if money else "",
        "h": shipped,
        "f": refund,
        "z": 1 if order.get("refund_full") else 0,   # полный возврат — заказ отменён
        "b": 1 if order.get("cdek_uuid") else 0,     # есть накладная — есть и штрихкод
        # Слова этапов: печать или сборка, отправка или выдача
        "w": [w["icon"], w["do"], w["done"].capitalize(), sw["icon"], sw["btn"],
              sw["did"], sw["do"]],
        # По позициям: значок, «Распечатал»/«Собрал», товар
        "p": [[oa._words(order, [p])["icon"], oa._words(order, [p])["did"],
               oa._products_label(order, [p], total).strip(" ()")]
              for p in range(1, total + 1)],
    }


async def _state(order_ids: list[int]) -> str:
    """Изменяемая часть: «id:маска печати по позициям:отправлен;…»."""
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
        parts.append(f"{oid}:{mask}:{1 if order.get('shipped_at') else 0}")
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
#t{font-size:13px;text-align:center;color:var(--tg-theme-hint-color);min-height:17px}"""

# Пузырь виджета не выше 600 px и сам не прокручивается — поэтому заказы
# вкладками по номерам сверху (по 6 на страницу, листаются ‹ ›), а ниже —
# один выбранный заказ целиком. Детали заказов, не влезших в HTML,
# подгружаются запросом «d:id» при выборе вкладки.
# Кнопки — div, а не <button>: формы в профиле виджета вырезаются,
# а onclick на любом элементе работает
_JS = r"""var W=window.TelegramWidget,P=6,sel=D[0].i,busy=0,X={};
D.forEach(function(o){if(o.x)X[o.i]=o.x});
function e(x){return String(x).replace(/[&<>"]/g,function(c){return"&#"+c.charCodeAt(0)+";"})}
function st(){var m={};String((W&&W.chatData)||"").split(";").forEach(function(x){var p=x.split(":");if(p[0])m[p[0]]={m:p[1]||"",s:p[2]=="1"}});return m}
function B(t,d,g){return'<div class="b'+(g?' g':'')+'" onclick="A(\''+d+'\')">'+e(t)+'</div>'}
function R(){var S=st(),h="",o,ix=0;
function Y(o){var x=S[o.i]||{m:"",s:false};x.d=x.m&&x.m.indexOf("0")<0;x.k=x.s?2:x.d?1:0;x.z=o.z&&!x.s;x.c=x.z?"💸":x.s?"✅":x.d?o.w[3]:o.w[0];return x}
D.forEach(function(q,j){if(q.i==sel){o=q;ix=j}});
if(D.length>1){var pg=Math.floor(ix/P),np=Math.ceil(D.length/P);h+='<div class="tb">';
if(np>1)h+='<div class="tab" onclick="G('+(pg+np-1)%np+')">‹</div>';
D.slice(pg*P,pg*P+P).forEach(function(o){h+='<div class="tab'+(o.i==sel?' on':'')+'" onclick="T('+o.i+')">'+Y(o).c+' №'+e(o.n)+'</div>'});
if(np>1)h+='<div class="tab" onclick="G('+(pg+1)%np+')">›</div>';h+='</div>'}
var x=Y(o),w=o.w,v=X[o.i],ch=x.z?"💸 Отменён":x.s?"✅ "+w[5]:x.d?w[3]+" "+w[6]:w[0]+" "+w[1];
h+='<div class="o"><div class="hd"><div><div class="n">№'+e(o.n)+' · '+e(o.m)+'</div><div class="s">'+e(o.t)+'</div></div><div class="ch">'+e(ch)+'</div></div>';
h+='<div class="bar"><i class="on"></i><i class="'+(x.k>0?"on":"")+'"></i><i class="'+(x.k>1?"on":"")+'"></i></div><div class="lb"><span>Оплачен</span><span>'+e(w[2])+'</span><span>'+e(w[5])+'</span></div>';
h+='<div class="dt">';if(x.s&&o.h)h+='<div><span>'+e(w[5])+':</span> '+e(o.h)+'</div>';if(o.f)h+='<div>'+e(o.f)+'</div>';
if(!v)h+='<div class="s">Загружаю детали…</div>';else if(v.e)h+='<div class="s">Детали не загрузились — нажми на номер ещё раз</div>';else{
v.q.forEach(function(a){h+='<div><span>'+e(a[0])+':</span> '+e(a[1])+'</div>'});
h+='<div><span>Клиент:</span> '+e(v.c)+'</div>';if(v.d)h+='<div><span>Куда:</span> '+e(v.d)+'</div>';if(v.r)h+='<div><span>Кому:</span> '+e(v.r)+'</div>';
if(v.k)h+='<div onclick="C(\''+v.k+'\')"><span>СДЭК:</span> '+e(v.k)+' ⧉</div>'}h+='</div>';
var pb="",gb="";function K(t,d,g){if(g)gb+=B(t,d,1);else pb+=B(t,d)}
if(x.s)K("↩️ Отменить «"+w[5].toLowerCase()+"»","x:"+o.i,1);else if(!x.z){var n=o.p.length;
o.p.forEach(function(p,j){var nm=n>1?" поз."+(j+1)+(p[2]?" ("+p[2]+")":""):"";
x.m.charAt(j)=="1"?K("↩️ Снять «"+p[1].toLowerCase()+"»"+nm,"u:"+o.i+":"+(j+1),1):K(p[0]+" "+p[1]+nm,"p:"+o.i+":"+(j+1))});
if(x.d&&o.b)K("🏷 Штрихкод СДЭК","b:"+o.i,1);if(x.d)K(w[4],"s:"+o.i)}
K(x.s?"📄 Повтор, возврат":"📄 Карточка","c:"+o.i,1);h+='<div class="r">'+gb+'</div>'+pb+'</div>';
document.getElementById("L").innerHTML=h}
function H(f,a){try{W.HapticFeedback[f](a)}catch(_){}}
function L(i){if(!W)return;W.request("d:"+i).then(function(a){try{X[i]=JSON.parse(a.result)}catch(_){X[i]={e:1}}R()},function(er){X[i]={e:1};M("Не вышло: "+er.code);R()})}
function T(i){sel=i;M("");H("selectionChanged");if(!X[i]||X[i].e)L(i);R()}
function G(p){T(D[p*P].i)}
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


def build_html(orders: list[dict], mode: str = "work", total: int | None = None) -> tuple[str, int]:
    """HTML виджета и сколько заказов в него влезло.

    Сначала выкидываем детали с конца списка (их подгрузит запрос),
    и только если не влезает и без них — сами заказы.
    """
    data = [dict(_static(o), x=_details(o)) for o in orders]
    total = total or len(orders)

    def head():
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
    page, fit = build_html(orders, mode, total)
    ids = [o["id"] for o in orders[:fit]]
    try:
        sent = await api(message.bot, "sendWidget", chat_id=message.chat.id, html=page,
                         chat_data=await _state(ids), allow_queries=True)
    except Exception as e:
        logger.warning(f"sendWidget {mode}: {e}")
        return False
    await db.add_order_widget(message.chat.id, sent["message_id"], ids)
    await db.replace_admin_command_messages(batch, message.chat.id, message.from_user.id,
                                            [sent["message_id"]])
    return True


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
                  chat_data=await _state(w["order_ids"]))
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
        await handle_query(data["bot"], q)
        return True
