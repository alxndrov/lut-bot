"""
«Заказы в работе» интерактивным сообщением — HTML-виджет Bot API 10.4.

/worders в админском боте присылает один виджет со всеми неотправленными
заказами: этапы полосой, детали по нажатию на заказ и кнопки
«Распечатал» / «Заказ отправил» прямо в сообщении. Пробная замена /orders.

Как это устроено
- Разметка и неизменное (номер, товар, ответы, доставка) зашиты в HTML
  при отправке; изменяемое — отметки печати и отправки — лежит в chat_data
  сообщения строкой «id:маска:отправлен;…» (не больше 1024 байт).
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

Ограничения Telegram: виджет — только в личке с ботом, переслать нельзя;
HTML не больше 4096 байт в сжатом виде — заказы, что не влезли,
остаются в /orders.
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


def _static(order: dict) -> dict:
    """Неизменная часть заказа — зашивается в HTML виджета."""
    from handlers import order_actions as oa
    total = oa._order_positions(order)
    w, sw = oa._words(order), oa._ship(order)
    money = re.search(r"💵\s*([^\n]+)", order.get("summary") or "")
    client = (f"@{order['username']}" if order.get("username")
              else order.get("first_name") or f"id:{order.get('user_id')}")
    where, who = _delivery(order.get("summary") or "")
    title = re.sub(r"^\W+\s*", "", order.get("product_name") or "")
    return {
        "i": order["id"],
        "n": oa._order_num(order),
        "t": _cut(title, 40),
        "m": _plain(money.group(1)) if money else "",
        "c": client,
        "q": _answers(order.get("summary") or ""),
        "d": where, "r": who,
        "k": order.get("cdek_number") or "",
        "b": 1 if order.get("cdek_uuid") else 0,   # есть накладная — есть и штрихкод
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
#t{font-size:13px;text-align:center;color:var(--tg-theme-hint-color);min-height:17px}"""

# Пузырь виджета не выше 600 px и сам не прокручивается — поэтому заказы
# вкладками по номерам сверху, а ниже — один выбранный заказ целиком.
# Кнопки — div, а не <button>: формы в профиле виджета вырезаются,
# а onclick на любом элементе работает
_JS = r"""var W=window.TelegramWidget,sel=D[0].i,busy=0;
function e(x){return String(x).replace(/[&<>"]/g,function(c){return"&#"+c.charCodeAt(0)+";"})}
function st(){var m={};String((W&&W.chatData)||"").split(";").forEach(function(x){var p=x.split(":");if(p[0])m[p[0]]={m:p[1]||"",s:p[2]=="1"}});return m}
function B(t,d,g){return'<div class="b'+(g?' g':'')+'" onclick="A(\''+d+'\')">'+e(t)+'</div>'}
function R(){var S=st(),h="",o;
function X(o){var x=S[o.i]||{m:"",s:false};x.d=x.m&&x.m.indexOf("0")<0;x.k=x.s?2:x.d?1:0;x.c=x.s?"✅":x.d?o.w[3]:o.w[0];return x}
if(D.length>1){h+='<div class="tb">';D.forEach(function(o){h+='<div class="tab'+(o.i==sel?' on':'')+'" onclick="T('+o.i+')">'+X(o).c+' №'+e(o.n)+'</div>'});h+='</div>'}
D.forEach(function(q){if(q.i==sel)o=q});var x=X(o),w=o.w,
ch=x.s?"✅ "+w[5]:x.d?w[3]+" "+w[6]:w[0]+" "+w[1];
h+='<div class="o"><div class="hd"><div><div class="n">№'+e(o.n)+' · '+e(o.m)+'</div><div class="s">'+e(o.t)+'</div></div><div class="ch">'+e(ch)+'</div></div>';
h+='<div class="bar"><i class="on"></i><i class="'+(x.k>0?"on":"")+'"></i><i class="'+(x.k>1?"on":"")+'"></i></div><div class="lb"><span>Оплачен</span><span>'+e(w[2])+'</span><span>'+e(w[5])+'</span></div>';
h+='<div class="dt">';o.q.forEach(function(a){h+='<div><span>'+e(a[0])+':</span> '+e(a[1])+'</div>'});
h+='<div><span>Клиент:</span> '+e(o.c)+'</div>';if(o.d)h+='<div><span>Куда:</span> '+e(o.d)+'</div>';if(o.r)h+='<div><span>Кому:</span> '+e(o.r)+'</div>';
if(o.k)h+='<div onclick="C(\''+o.k+'\')"><span>СДЭК:</span> '+e(o.k)+' ⧉</div>';h+='</div>';
if(x.s)h+=B("↩️ Отменить «"+w[5].toLowerCase()+"»","x:"+o.i,1);else{var n=o.p.length;
o.p.forEach(function(p,j){var nm=n>1?" поз."+(j+1)+(p[2]?" ("+p[2]+")":""):"";
h+=x.m.charAt(j)=="1"?B("↩️ Снять «"+p[1].toLowerCase()+"»"+nm,"u:"+o.i+":"+(j+1),1):B(p[0]+" "+p[1]+nm,"p:"+o.i+":"+(j+1))});
if(x.d&&o.b)h+=B("🏷 Штрихкод СДЭК","b:"+o.i,1);if(x.d)h+=B(w[4],"s:"+o.i)}h+='</div>';
document.getElementById("L").innerHTML=h}
function H(f,a){try{W.HapticFeedback[f](a)}catch(_){}}
function T(i){sel=i;M("");H("selectionChanged");R()}
function M(t){document.getElementById("t").textContent=t}
function C(k){W&&W.copyText(k);M("Трек скопирован")}
function A(d){if(!W||busy)return;busy=1;M("…");W.request(d).then(function(a){busy=0;var r={};try{r=JSON.parse(a.result)}catch(_){}
M(r.t||"");H("notificationOccurred",r.a?"warning":"success")},function(er){busy=0;M("Не вышло: "+er.code)})}
if(W){W.onEvent("chatDataChanged",R);W.ready&&W.ready()}R();"""


def build_html(orders: list[dict]) -> tuple[str, int]:
    """HTML виджета и сколько заказов в него влезло (лимит Telegram по размеру)."""
    data = [_static(o) for o in orders]
    while True:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        head = f"📋 Заказы в работе: {len(orders)}"
        if len(data) < len(orders):
            head += f" (тут {len(data)}, остальные — /orders)"
        page = (f"<style>{_CSS}</style><h3>{head}</h3><div id=\"L\"></div><div id=\"t\"></div>"
                f"<script>var D={payload};{_JS}</script>")
        if len(zlib.compress(page.encode(), 9)) <= HTML_ZLIB_LIMIT or len(data) <= 1:
            return page, len(data)
        data.pop()


# ── команда ───────────────────────────────────────────────────────────

@router.message(Command("worders"))
async def cmd_worders(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    from handlers import order_actions as oa
    await oa._ensure_admin_names()
    todo = await db.get_orders(only_unshipped=True)
    todo = [o for o in todo if not o.get("refund_full")]
    todo.sort(key=lambda o: o.get("created_at") or "")
    if not todo:
        await message.answer("📋 Заказов в работе нет — всё разослано 🎉")
        return

    # Прошлый виджет убираем: в чате живёт один актуальный список
    for msg_id in await db.get_admin_command_messages("worders", message.chat.id,
                                                       message.from_user.id):
        try:
            await message.bot.delete_message(message.chat.id, msg_id)
        except Exception:
            pass
        await db.delete_order_widget(message.chat.id, msg_id)

    page, fit = build_html(todo)
    ids = [o["id"] for o in todo[:fit]]
    try:
        sent = await api(message.bot, "sendWidget", chat_id=message.chat.id, html=page,
                         chat_data=await _state(ids), allow_queries=True)
    except Exception as e:
        logger.warning(f"sendWidget: {e}")
        await message.answer(f"⚠️ Telegram не принял виджет: {html.escape(str(e))}",
                             parse_mode="HTML")
        return
    await db.add_order_widget(message.chat.id, sent["message_id"], ids)
    await db.replace_admin_command_messages("worders", message.chat.id,
                                            message.from_user.id, [sent["message_id"]])
    try:
        await message.delete()
    except Exception:
        pass


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


async def handle_query(bot, q: dict):
    note, alert = "", False
    try:
        user = User.model_validate(q["from"])
        kind, _, rest = (q.get("data") or "").partition(":")
        if user.id not in config.ADMIN_IDS:
            note, alert = "Только для администраторов.", True
        elif kind not in _ACTIONS:
            note, alert = "Неизвестная кнопка — пришли /worders заново.", True
        else:
            from handlers import order_actions as oa
            handler = {"p": oa.cb_order_printed, "u": oa.cb_order_unprint,
                       "s": oa.cb_order_shipped, "x": oa.cb_order_unship,
                       "b": oa.cb_order_barcode}[kind]
            chat, msg_id = (q.get("chat") or {}).get("id"), q.get("message_id")
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
    try:
        await api(bot, "answerWidgetQuery", widget_query_id=q["id"],
                  data=json.dumps({"t": _cut(note, 200), "a": int(alert)}, ensure_ascii=False))
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
