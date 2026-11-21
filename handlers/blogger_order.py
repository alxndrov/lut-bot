"""Бесплатный заказ для блогера.

Админ в malimadmins: /blogger @ник — бот создаёт личную ссылку
t.me/<бот>?start=blog_<token> и присылает готовый текст для блогера (его
пересылают или копируют блогеру). /blogger без аргументов — список
приглашений с кнопкой отмены.

Блогер открывает ссылку (или просто пишет /start с этого ника) — бот
привязывает приглашение к его аккаунту и предлагает выбрать товар. Дальше
обычный путь клиента: опрос по товару, СДЭК, получатель — только без
кнопки «добавить ещё» (ровно одна позиция) и без оплаты: в конце кнопка
«🎁 Оформить бесплатно». Заказ проводится как подарок другу
(provision_payment gift=True): без строки покупки и выручки, накладную
СДЭК оплачиваем мы; в карточке и финансовой таблице — «Блогер @ник».

Приглашение одноразовое: гасится до выдачи, второй заказ по нему не
пройдёт. Ссылку чужой аккаунт открыть не может — только ник из приглашения.
"""
import html
import json
import logging
import secrets

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import database as db

router = Router()         # основной бот: блогер
admin_router = Router()   # malimadmins: /blogger
logger = logging.getLogger(__name__)

PREFIX = "blog_"
NICK_RE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")

INVITE_TEXT = (
    "Привет! Это MALIMABI 🤍\n\n"
    "Дарим вам один любой товар из нашего магазина — бесплатно, доставку СДЭК "
    "тоже берём на себя.\n\n"
    "Откройте ссылку, выберите товар и оформите заказ — оплачивать ничего не нужно:\n"
    "{link}\n\n"
    "Ссылка личная и сработает только с аккаунта @{nick}."
)
OFFER_TEXT = (
    "🎁 <b>Подарок от MALIMABI</b>\n\n"
    "Выберите один любой товар — он бесплатно, доставку СДЭК тоже берём на себя. "
    "Дальше те же вопросы по товару и адрес доставки, оплачивать ничего не нужно 👇"
)


def _nick(inv: dict) -> str:
    return f"@{inv['username']}"


async def _notify(text: str) -> None:
    from handlers.prodamus_webhook import _send_notify
    await _send_notify(None, text)


async def invite_link(token: str) -> str:
    bot = Bot(token=config.BOT_TOKEN)
    try:
        username = (await bot.get_me()).username
    finally:
        await bot.session.close()
    return f"https://t.me/{username}?start={PREFIX}{token}"


# ── Админ: /blogger ────────────────────────────────────────────────────────────

def _status(inv: dict) -> str:
    if inv["cancelled_at"]:
        return "❌ отменено"
    if inv["used_at"]:
        return f"✅ заказал{' — ' + inv['order_code'] if inv['order_code'] else ''}"
    if inv["user_id"]:
        return "👀 открыл, ещё не заказал"
    return "⏳ ждёт"


@admin_router.message(Command("blogger"))
async def cmd_blogger(message: Message, command: CommandObject):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    arg = (command.args or "").strip()
    if not arg:
        await _list(message)
        return
    nick = arg.split()[0].lstrip("@").lower()
    if len(nick) < 4 or not set(nick) <= NICK_RE_CHARS:
        await message.answer("Формат: <code>/blogger @ник</code> — ник блогера в Telegram.",
                             parse_mode="HTML")
        return
    inv = await db.open_blogger_invite_by_username(nick)
    fresh = inv is None
    if fresh:
        inv = await db.create_blogger_invite(secrets.token_urlsafe(9), nick, message.from_user.id)
        logger.info(f"blogger: {message.from_user.id} создал приглашение {inv['id']} для @{nick}")
    link = await invite_link(inv["token"])
    await message.answer(
        ("🎬 <b>Приглашение для блогера готово</b>" if fresh else
         "🎬 <b>У этого ника уже есть приглашение</b> — вот оно же") +
        f"\n{_nick(inv)} · один бесплатный товар, доставка за наш счёт.\n\n"
        "Текст для блогера — следующим сообщением: перешлите или скопируйте его. "
        "Ссылка сработает только с этого ника. Список и отмена — /blogger",
        parse_mode="HTML")
    await message.answer(INVITE_TEXT.format(link=link, nick=inv["username"]),
                         disable_web_page_preview=True)


async def _list(message: Message):
    invites = await db.list_blogger_invites()
    if not invites:
        await message.answer("Приглашений блогерам пока нет.\nСоздать: <code>/blogger @ник</code>",
                             parse_mode="HTML")
        return
    lines = ["🎬 <b>Приглашения блогерам</b>"]
    rows = []
    for inv in invites:
        lines.append(f"• {html.escape(_nick(inv))} — {_status(inv)}")
        if not inv["used_at"] and not inv["cancelled_at"]:
            rows.append([InlineKeyboardButton(text=f"❌ Отменить {_nick(inv)}",
                                              callback_data=f"blg:cancel:{inv['id']}")])
    lines.append("\nСоздать: <code>/blogger @ник</code>")
    await message.answer("\n".join(lines), parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)


@admin_router.callback_query(F.data.startswith("blg:cancel:"))
async def cb_cancel(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    inv_id = int(callback.data.split(":")[2])
    if await db.cancel_blogger_invite(inv_id):
        inv = await db.get_blogger_invite_by_id(inv_id)
        logger.info(f"blogger: {callback.from_user.id} отменил приглашение {inv_id}")
        await callback.answer("Отменено")
        await callback.message.answer(f"❌ Приглашение {html.escape(_nick(inv))} отменено — "
                                      "ссылка больше не работает.")
    else:
        await callback.answer("Уже отменено или по нему уже заказали.", show_alert=True)


# ── Блогер: вход по ссылке ─────────────────────────────────────────────────────

async def claim(message: Message, user, state: FSMContext, token: str) -> None:
    """Блогер открыл ссылку-приглашение (согласие с политикой уже есть)."""
    inv = await db.get_blogger_invite(token)
    await _open(message, user, state, inv)


async def claim_by_username(message: Message, user, state: FSMContext) -> bool:
    """/start без ссылки: есть приглашение на ник — показываем его. False — нет."""
    username = getattr(user, "username", None)
    if not isinstance(username, str) or not username:
        return False
    inv = await db.open_blogger_invite_by_username(username)
    if not inv or (inv["user_id"] and inv["user_id"] != user.id):
        return False
    await _open(message, user, state, inv)
    return True


async def _open(message: Message, user, state: FSMContext, inv: dict | None) -> None:
    if not inv or inv["cancelled_at"]:
        await message.answer("Это приглашение не работает. Напишите в /help — разберёмся.")
        return
    if inv["user_id"] and inv["user_id"] != user.id:
        await message.answer("Это приглашение уже открыто с другого аккаунта. "
                             "Если это ошибка — напишите в /help.")
        return
    if inv["used_at"]:
        code = f" {inv['order_code']}" if inv["order_code"] else ""
        await message.answer(f"Заказ по этому приглашению уже оформлен{code} 🤍 "
                             "Вопросы — в /help.")
        return
    if not inv["user_id"]:
        if (user.username or "").lower() != inv["username"]:
            await message.answer(
                f"🎁 Это приглашение для аккаунта @{inv['username']}. Откройте ссылку с него — "
                "или напишите в /help, если ник сменился.")
            await _notify(f"⚠️ Приглашение блогера {html.escape(_nick(inv))} пытался открыть "
                          f"{html.escape(user.first_name or '')} "
                          f"{'@' + user.username if user.username else ''} (id:{user.id}) — "
                          "не тот ник, заказ не открыт.")
            return
        if not await db.bind_blogger_invite(inv["id"], user.id):
            await message.answer("Это приглашение уже не работает. Напишите в /help.")
            return
        logger.info(f"blogger: приглашение {inv['id']} открыл {user.id}")
        await _notify(f"🎬 Блогер {html.escape(_nick(inv))} открыл приглашение — выбирает товар.")
    if state is not None:
        await state.clear()
    await _show_products(message, inv)


async def _show_products(message: Message, inv: dict) -> None:
    from handlers.friend_order import _orderable_products
    products = await _orderable_products()
    if not products:
        await message.answer("Сейчас в магазине нечего выбрать 😔 Напишите в /help — подберём.")
        return
    rows = [[InlineKeyboardButton(text=p["name"], callback_data=f"blg:pick:{inv['id']}:{p['id']}")]
            for p in products]
    await message.answer(OFFER_TEXT, parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _my_open_invite(inv_id: int, user_id: int) -> dict | None:
    inv = await db.get_blogger_invite_by_id(inv_id)
    if (not inv or inv["user_id"] != user_id or inv["used_at"] or inv["cancelled_at"]):
        return None
    return inv


@router.callback_query(F.data.startswith("blg:pick:"))
async def cb_pick(callback: CallbackQuery, state: FSMContext):
    _, _, inv_id, pid = callback.data.split(":")
    inv = await _my_open_invite(int(inv_id), callback.from_user.id)
    if not inv:
        await callback.answer("Это приглашение уже не действует.", show_alert=True)
        return
    from handlers.friend_order import _orderable_products
    product = next((p for p in await _orderable_products() if p["id"] == int(pid)), None)
    if not product:
        await callback.answer("Этого товара сейчас нет — выберите другой.", show_alert=True)
        return
    await callback.answer()
    from handlers.brief_handler import start_order_flow
    await callback.message.answer(f"🎁 Ваш подарок: <b>{html.escape(product['name'])}</b>",
                                  parse_mode="HTML")
    await start_order_flow(callback.message, state, product)
    # Метку ставим после старта: start_order_flow очищает состояние. По ней
    # опрос не предлагает «ещё один товар», а в конце вместо оплаты — подарок
    if await state.get_state():
        await state.update_data(blogger_invite=inv["id"], blogger=_nick(inv))


# ── Итог: бесплатное оформление ────────────────────────────────────────────────

def is_blogger(data: dict) -> bool:
    return bool(data.get("blogger_invite"))


async def send_confirm(message: Message, user_id: int, product_id: int, data: dict,
                       goods_line: str, delivery_lines: str) -> None:
    """Подтверждение подарочного заказа вместо кнопок оплаты.
    pending_deliveries уже сохранён (с меткой blogger)."""
    text = (f"📋 <b>Подарочный заказ</b>\n\n{goods_line}\n{delivery_lines}\n\n"
            "<b>К оплате: 0 ₽</b> 🎁")
    await message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(
            text="🎁 Оформить бесплатно",
            callback_data=f"blg:ok:{data['blogger_invite']}:{product_id}")]]))


_BUSY: set[int] = set()


@router.callback_query(F.data.startswith("blg:ok:"))
async def cb_ok(callback: CallbackQuery, bot: Bot):
    _, _, inv_id, pid = callback.data.split(":")
    inv_id, pid, uid = int(inv_id), int(pid), callback.from_user.id
    if inv_id in _BUSY:
        await callback.answer("Уже оформляю…")
        return
    _BUSY.add(inv_id)
    try:
        inv = await _my_open_invite(inv_id, uid)
        if not inv:
            await callback.answer("Заказ по этому приглашению уже оформлен или оно не действует.",
                                  show_alert=True)
            return
        pending = await db.get_pending_order(uid, pid)
        if not pending or pending.get("blogger") != _nick(inv):
            await callback.answer("Этот заказ устарел — выберите товар заново по ссылке.",
                                  show_alert=True)
            return
        rounds = json.loads(pending.get("survey_json") or "[]")
        products = db.unpack_round_products(pending.get("round_products_json"), rounds, pid)
        if len(rounds) > 1 or len(products or [pid]) != 1:
            await callback.answer("В подарочном заказе может быть только один товар.",
                                  show_alert=True)
            return
        if not await db.use_blogger_invite(inv_id, uid):
            await callback.answer("Заказ по этому приглашению уже оформлен.", show_alert=True)
            return
        await callback.answer("Оформляю…")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        payment_id = f"blogger:{inv_id}"
        try:
            from handlers.prodamus_webhook import provision_payment
            ok = await provision_payment(bot, uid, pid, "p", payment_id, 0, gift=True)
        except Exception as e:
            logger.exception(f"blogger: заказ по приглашению {inv_id} упал: {e}")
            ok = False
        order_id = await db.get_order_id_by_payment(payment_id)
        if not ok or not order_id:
            await db.unuse_blogger_invite(inv_id)
            await callback.message.answer("Не получилось оформить заказ 😔 Мы уже в курсе — "
                                          "напишем вам. Или напишите в /help.")
            await _notify(f"🔴 Заказ блогера {html.escape(_nick(inv))} не оформился — "
                          "проверьте логи. Приглашение снова действует.")
            return
        order = await db.get_order(order_id)
        await db.set_blogger_order_code(inv_id, order.get("order_code") or str(order_id))
        logger.info(f"blogger: приглашение {inv_id} → заказ {order.get('order_code')}")
    finally:
        _BUSY.discard(inv_id)
