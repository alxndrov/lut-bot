"""
Каталог основного бота интерактивным сообщением (HTML-виджет Bot API 10.4).

Карточки товаров списком: название, коротко о товаре, цена и метка
(курс, лут, 3D-печать, в наличии). Нажатие раскрывает товар — описание,
что осталось в наличии — и кнопку «Купить» / «Оформить заказ». Она шлёт
боту widget_query «pr:<id>», а дальше всё как раньше: тот же
catalog.cb_product (карточка с фото, оплата, опрос для физтовара) —
ему подсовываем заглушку CallbackQuery и сообщение-виджет вместо каталога.
«⭐️ Отзывы» и «ℹ️ Продавцы и условия» — обычные кнопки под виджетом.

Кому показываем — настройка catalog_widget (/catalogwidget у админа):
admins — только админам (по умолчанию, проба), all — всем, off — никому.
Без виджета, и если Telegram его не принял, — прежний каталог кнопками.
Старые приложения Telegram виджет не покажут — поэтому не спешим с all.

Фото товаров в виджет не влезают (HTML не больше 4096 байт в сжатом
виде, картинки — только data:), фото — в карточке товара после выбора.
"""
import html
import json
import logging
import re

from aiogram import BaseMiddleware, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, Message, User)

import config
import database as db
from handlers.order_widget import api, _cut, _fits

router = Router()
logger = logging.getLogger(__name__)

SETTING = "catalog_widget"
MODES = ("admins", "all", "off")


async def mode() -> str:
    m = await db.get_setting(SETTING)
    return m if m in MODES else "admins"


async def enabled_for(user_id: int) -> bool:
    m = await mode()
    return m == "all" or (m == "admins" and user_id in config.ADMIN_IDS)


# ── что показываем ────────────────────────────────────────────────────

def _first_sentence(text: str, n: int = 70) -> str:
    """Подзаголовок карточки: первое предложение, если оно не обрубок."""
    text = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return _cut(m.group(1) if m and len(m.group(1)) >= 30 else text, n)


async def _item(p: dict) -> dict:
    """Товар для виджета."""
    cat = p.get("category") or "digital"
    price = await db.get_effective_price(p)
    price_text = f"{p.get('price_label') or price} ₽"
    desc = (p.get("description") or "").strip()
    desc = "" if desc == "-" else desc
    # Строки описания — в предложения: в одну строку «передатчиков Есть» слипается
    desc = " ".join(ln if ln[-1] in ".!?…:" else ln + "."
                    for ln in (x.strip() for x in desc.splitlines()) if ln)
    name = p["name"]
    icon = ""
    m = re.match(r"^([^\w\s«\"(]+)\s*", name)
    if m:
        icon, name = m.group(1), name[m.end():]
    tag, extra, btn, off = "", "", "", 0
    if await db.get_course_options(p["id"]):
        tag, btn = "Курс", "Подробнее о курсе"
        icon = icon or "🎬"
    elif cat == "waitlist":
        tag, btn = "Скоро", "Записаться в список"
    elif cat == "physical":
        if db.is_stock_product(p):
            left = [v for v in await db.get_product_variants(p["id"]) if v["stock"] > 0]
            n = sum(v["stock"] for v in left)
            tag = f"В наличии: {n} шт" if n else "Всё разобрали"
            extra = ", ".join(v["name"] for v in left[:6])
            btn, off = ("Выбрать", 0) if n else ("Нет в наличии", 1)
        else:
            tag, btn = "3D-печать на заказ", "Оформить заказ"
        icon = icon or "📦"
    else:
        tag, btn = ("Курс" if cat == "infobiz" else "Цифровой товар"), f"Купить · {price} ₽"
        icon = icon or "✨"
    return {"i": p["id"], "e": icon, "n": _cut(name, 48), "s": _first_sentence(desc),
            "d": _cut(desc, 260), "x": _cut(extra, 120), "p": price_text, "g": tag,
            "b": btn, "o": off, "h": 0 if p.get("active", 1) else 1}


# Стиль — как у мини-приложения (webapp/index.html, макет Миши) и у «Моего
# заказа»: чёрный фон, карточки #141414, красный #e10600, заголовок и номера
# товаров фирменным шрифтом — контурами в SVG (шрифты в виджете запрещены).
# Список — как уроки в приложении: номер, название капсом, метка, цена.
_CSS = """*{box-sizing:border-box;margin:0}
body{background:#0a0a0a;color:#f2f2f2;font:14px/1.35 -apple-system,system-ui,'Segoe UI',Roboto,sans-serif;padding:12px 14px 6px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.eb{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#7a7a7a}.eb.r{color:#e10600}
.pill{border:1px solid #262626;border-radius:99px;padding:3px 11px;font-size:13px;font-weight:600}
.h1{color:#f2f2f2}
.ls{margin-top:12px;border-top:1px solid #1f1f1f}
.it{border-bottom:1px solid #1f1f1f;cursor:pointer}
.rw{display:flex;align-items:center;gap:12px;padding:11px 0}
.nm{width:34px;flex:none;color:#3a3a3a}.nt{font-weight:900;font-size:19px;letter-spacing:-.02em}.on .nm{color:#e10600}
.m{flex:1;min-width:0}.m b{display:block;font-weight:600;font-size:15px;line-height:1.25}
.m small{display:block;color:#7a7a7a;font-size:13px;margin-top:3px}.m small i{font-style:normal;color:#e10600}
.m em{font-style:normal;font-weight:700;color:#f2f2f2}
.cd{padding:0 0 12px 46px;margin-top:-3px}
.d{color:#a3a3a3;font-size:13px;display:-webkit-box;-webkit-line-clamp:6;-webkit-box-orient:vertical;overflow:hidden}
.tg .rw{padding:8px 0}.tg .d{-webkit-line-clamp:4}.x{color:#7a7a7a;font-size:12px;margin-top:6px}
.d+.bt,.x+.bt{margin-top:10px}.bt{border-radius:99px;padding:10px 16px;font-weight:600;font-size:14px;text-align:center;background:#e10600;color:#fff}
.bt.o{background:#262626;color:#7a7a7a}
#t{font-size:12px;text-align:center;color:#7a7a7a;min-height:16px;margin-top:8px}"""

# Раскрыт товар — подписи остальных прячем: пузырь не выше 600 px
_JS = r"""var W=window.TelegramWidget,sel=0,busy=0,skip=0;
function e(x){return String(x).replace(/[&<>"]/g,function(c){return"&#"+c.charCodeAt(0)+";"})}
function H(f,a){try{W.HapticFeedback[f](a)}catch(_){}}
function R(){var h="";D.forEach(function(p,j){var on=p.i==sel;
h+='<div class="it'+(on?' on':'')+'" onclick="T('+p.i+')"><div class="rw"><div class="nm">'+N[j]+'</div><div class="m"><b>'+e(p.n)+'</b>';
if(!sel||on)h+='<small><em>'+e(p.p)+'</em> · '+(p.h?'<i>скрыт</i> · ':'')+e(p.g)+'</small>';h+='</div></div>';
if(on){h+='<div class="cd">';if(p.d)h+='<div class="d">'+e(p.d)+'</div>';if(p.x)h+='<div class="x">'+e(p.x)+'</div>';
h+='<div class="bt'+(p.o?' o':'')+'" onclick="A('+p.i+','+p.o+')">'+e(p.b)+'</div></div>'}h+='</div>'});
var L=document.getElementById("L");L.className="ls"+(D.length>5?" tg":"");L.innerHTML=h}
function T(i){if(skip){skip=0;return}sel=sel==i?0:i;M("");H("selectionChanged");R()}
function M(t){document.getElementById("t").textContent=t}
// Кнопка внутри карточки: нажатие всплывёт до товара — его не сворачиваем
function A(i,o){skip=1;setTimeout(function(){skip=0},0);if(o||!W||busy)return;busy=1;M("Открываю…");H("impactOccurred","light");
W.request("pr:"+i).then(function(a){busy=0;var r={};try{r=JSON.parse(a.result)}catch(_){}M(r.t||"")},function(er){busy=0;M("Не вышло: "+er.code+". Попробуйте ещё раз")})}
if(W&&W.ready)W.ready();R();"""


def _count(n: int) -> str:
    word = "товар" if n % 10 == 1 and n % 100 != 11 else (
        "товара" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "товаров")
    return f"{n} {word}"


def build_html(items: list[dict], admin: bool) -> str:
    from handlers.client_order_widget import headline_svg
    svg_nums = [headline_svg(f"{j:02d}", size=20) for j in range(1, len(items) + 1)]
    txt_nums = [f'<span class="nt">{j:02d}</span>' for j in range(1, len(items) + 1)]
    head = headline_svg("Каталог", size=34, max_em=12)

    def page(data, nums):
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        n = json.dumps(nums, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        note = _count(len(data)) + (" · со скрытыми" if admin else "")
        return (f"<style>{_CSS}</style><div class=\"top\"><span class=\"eb r\">MALIMABI</span>"
                f"<span class=\"pill\">{note}</span></div><div class=\"h1\">{head}</div>"
                f"<div class=\"ls\" id=\"L\"></div><div id=\"t\">Нажмите на товар — откроются подробности</div>"
                f"<script>var D={payload},N={n};{_JS}</script>")
    # Номера фирменным шрифтом — если влезают; описания важнее, поэтому
    # сначала номера системным жирным, и только потом режем описания
    out = page(items, svg_nums)
    if _fits(out):
        return out
    data = list(items)
    for n in (None, 180, 120, 60, 0):
        if n is not None:
            data = [dict(it, d=_cut(it["d"], n) if n else "", x=_cut(it["x"], n)) for it in data]
        out = page(data, txt_nums)
        if _fits(out):
            break
    return out


def _keyboard(reviews: int) -> InlineKeyboardMarkup:
    rows = []
    if reviews:
        rows.append([InlineKeyboardButton(text=f"⭐️ Отзывы покупателей ({reviews})",
                                          callback_data="shoprev:0")])
    rows.append([InlineKeyboardButton(text="ℹ️ Продавцы и условия", callback_data="info:sellers")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send(bot, chat_id: int, user_id: int) -> bool:
    """Каталог виджетом. False — не наш случай или Telegram не принял."""
    if not await enabled_for(user_id):
        return False
    admin = user_id in config.ADMIN_IDS
    products = await db.get_catalog_products(include_hidden=admin)
    if not products:
        return False
    items = [await _item(p) for p in products]
    try:
        await api(bot, "sendWidget", chat_id=chat_id, html=build_html(items, admin),
                  allow_queries=True,
                  reply_markup=_keyboard(await db.count_published_reviews()).model_dump(
                      exclude_none=True))
        return True
    except Exception as e:
        logger.warning(f"catalog sendWidget: {e}")
        return False


@router.message(Command("catalogwidget"))
async def cmd_catalog_widget(message: Message, command: CommandObject):
    """/catalogwidget admins|all|off — кому показывать каталог виджетом."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    arg = (command.args or "").strip().lower()
    if arg in MODES:
        await db.set_setting(SETTING, arg)
    m = await mode()
    await message.answer(
        f"📦 Каталог виджетом: <b>{ {'admins': 'только админам', 'all': 'всем', 'off': 'выключен'}[m] }</b>\n\n"
        "/catalogwidget admins — только админам\n/catalogwidget all — всем покупателям\n"
        "/catalogwidget off — выключить\n\n"
        "<i>Старые версии Telegram виджет не показывают — покупатель увидит "
        "«сообщение не поддерживается».</i>", parse_mode="HTML")


# ── нажатия в виджете ─────────────────────────────────────────────────

class _Tap:
    """Заглушка CallbackQuery для catalog.cb_product."""

    def __init__(self, bot, user: User, data: str, message: Message):
        self.bot, self.from_user, self.data, self.message = bot, user, data, message
        self.id = "widget"
        self.note = ""

    async def answer(self, text: str | None = None, show_alert: bool = False, **_):
        if text:
            self.note = text


def _widget_message(bot, chat: dict, message_id: int) -> Message:
    """Сообщение-виджет как aiogram Message. Помечаем его «фото», чтобы
    cb_product не правил его текст (у виджета текста нет), а удалил
    и прислал карточку новым сообщением — как с каталогом-баннером."""
    return Message.model_validate({
        "message_id": message_id, "date": 0, "chat": chat,
        "photo": [{"file_id": "widget", "file_unique_id": "widget", "width": 1, "height": 1}],
    }, context={"bot": bot})


async def handle_query(bot, q: dict, storage):
    note = ""
    try:
        user = User.model_validate(q["from"])
        kind, _, rest = (q.get("data") or "").partition(":")
        chat = q.get("chat")
        if kind == "cat" and chat:
            # «Открыть каталог» из «Моего заказа» (handlers/client_order_widget.py)
            from handlers.start import show_catalog
            await show_catalog(_widget_message(bot, chat, q["message_id"]), user_id=user.id)
        elif kind != "pr" or not rest.isdigit() or not chat:
            note = "Обновите каталог: /catalog"
        else:
            msg = _widget_message(bot, chat, q["message_id"])
            if (config.POLICY_REQUIRED and user.id not in config.ADMIN_IDS
                    and await db.policy_status(user.id) != "ok"):
                # Как PolicyGate для обычных кнопок: сначала согласие
                from handlers.start import ask_policy
                await ask_policy(msg, renewed=await db.policy_status(user.id) == "old")
                note = "Сначала подтвердите согласие — сообщением ниже"
            else:
                from handlers.catalog import cb_product
                state = FSMContext(storage=storage, key=StorageKey(
                    bot_id=bot.id, chat_id=chat["id"], user_id=user.id))
                tap = _Tap(bot, user, f"product:{rest}", msg)
                await cb_product(tap, state)
                note = tap.note
    except Exception as e:
        logger.exception(f"catalog widget_query {q.get('data')}: {e}")
        note = "Не получилось — откройте /catalog ещё раз"
    try:
        await api(bot, "answerWidgetQuery", widget_query_id=q["id"],
                  data=json.dumps({"t": _cut(note, 200)}, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"answerWidgetQuery: {e}")   # виджет уже удалён — это нормально


class WidgetQueryMiddleware(BaseMiddleware):
    """widget_query основного бота — до того, как aiogram его выкинет."""

    async def __call__(self, handler, event, data):
        q = (getattr(event, "model_extra", None) or {}).get("widget_query")
        if q is None:
            return await handler(event, data)
        await handle_query(data["bot"], q, data["fsm_storage"])
        return True
