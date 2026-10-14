"""Выставить счёт за курс — только админам (основной бот).

Админ выбирает клиента (@ник, ID или пересланное сообщение) и вариант курса:
съёмка, монтаж, пакет или VIP. Бот собирает ссылку на оплату Prodamus
(order_num c_{клиент}_{опция} — та же, что у кнопок на экране курса, поэтому
после оплаты доступ выдаётся сам) и показывает её админу. Клиенту — только
кнопкой «📨 Отправить клиенту», как у незавершённых заказов физтоваров.
Сумму можно поменять («✏️ Другая сумма») — например, скидка.

Тот же выбор варианта открывается из списка «Смотрели курс, не купили».
"""
import logging
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import database as db
from services import course as svc
from services.prodamus import build_payment_url

router = Router()
logger = logging.getLogger(__name__)


class CourseInvoice(StatesGroup):
    client = State()   # ждём @ник, ID или пересланное сообщение
    amount = State()   # ждём другую сумму


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def _who(user: dict) -> str:
    return f"@{user['username']}" if user.get("username") else (user.get("first_name") or f"id:{user['user_id']}")


def payment_url(option: dict, user_id: int, price: int) -> str:
    return build_payment_url(
        shop_url=config.PRODAMUS_SHOP_URL, product_name=option["title"], price=price,
        user_id=user_id, product_id=option["id"], order_type="c",
        secret=config.PRODAMUS_SECRET, notification_url=config.PRODAMUS_WEBHOOK_URL,
    )


async def find_client(message: Message) -> dict | None:
    """Клиент из пересланного сообщения, @ника или ID — только тот, кто уже
    запускал бота: иначе ни ссылку ему не отправить, ни доступ после оплаты."""
    origin = getattr(message, "forward_origin", None)
    sender = getattr(origin, "sender_user", None) or getattr(message, "forward_from", None)
    if sender:
        return await db.get_user(sender.id)
    text = (message.text or "").strip()
    if text.lstrip("-").isdigit():
        return await db.get_user(int(text))
    if text.startswith("@") or text.replace("_", "").isalnum():
        return await db.get_user_by_username(text.lstrip("@"))
    return None


async def options_screen(user_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    user = await db.get_user(user_id)
    product = await db.get_course_product()
    if not user or not product:
        return None
    options = await db.get_course_options(product["id"])
    owned = await db.get_user_course_sections(user_id)
    paid = await db.get_user_course_paid(user_id)
    has_call = bool(await db.get_user_course_call(user_id))
    lines = [f"🧾 <b>Счёт за курс</b> — {escape(_who(user))}"]
    if owned:
        names = ", ".join(svc.SECTION_SHORT[s] for s in svc.SECTIONS if s in owned)
        lines.append(f"Уже есть: {names}" + (f", оплачено {svc.rub(paid)}" if paid else ""))
    lines += ["", "Выбери вариант — цена текущая, на следующем шаге её можно поменять:"]
    rows = []
    for o in options:
        is_call = svc.is_call_option(o)
        done = has_call if is_call else set(svc.option_sections(o)) <= owned
        if done:
            continue
        price = svc.price_for_user(o, paid, svc.bundle_option(options))
        label = ("Переход на VIP" if is_call and owned else o["title"])
        rows.append([InlineKeyboardButton(text=f"{label} · {svc.rub(price)}",
                                          callback_data=f"cinv:opt:{user_id}:{o['id']}")])
    if not rows:
        lines.append("<i>У клиента уже есть всё — выставлять нечего.</i>")
    rows.append([InlineKeyboardButton(text="◀️ В меню", callback_data="admin:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _link_message(target: Message, user_id: int, option_id: int, price: int):
    user = await db.get_user(user_id)
    option = await db.get_course_option(option_id)
    url = payment_url(option, user_id, price)
    await target.answer(
        f"🔗 <b>Ссылка на оплату</b> — {escape(option['title'])}, <b>{svc.rub(price)}</b>\n"
        f"Клиент: {escape(_who(user))}\n\n{url}\n\n"
        "Клиенту пока не отправлена — скопируй её или отправь кнопкой ниже. "
        "После оплаты доступ откроется сам.",
        parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📨 Отправить клиенту",
                                  callback_data=f"cinv:send:{user_id}:{option_id}:{price}")],
            [InlineKeyboardButton(text="✏️ Другая сумма",
                                  callback_data=f"cinv:amount:{user_id}:{option_id}")],
        ]))


# ── Вход ───────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:cinv")
async def cb_start(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(CourseInvoice.client)
    await callback.answer()
    await callback.message.answer(
        "🧾 <b>Счёт за курс</b>\n\nКому? Пришли @ник или ID клиента — или перешли сюда "
        "любое его сообщение. Клиент должен хотя бы раз запускать бота.\n\nОтмена — /cancel",
        parse_mode="HTML")


@router.callback_query(F.data.startswith("admin:cinv_user:"))
async def cb_for_user(callback: CallbackQuery, state: FSMContext):
    """Из списка «Смотрели курс, не купили» — сразу к выбору варианта."""
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    screen = await options_screen(int(callback.data.split(":")[2]))
    await callback.answer()
    if not screen:
        await callback.message.answer("Клиент не найден или курс не заведён.")
        return
    await callback.message.answer(screen[0], parse_mode="HTML", reply_markup=screen[1])


@router.message(CourseInvoice.client)
async def msg_client(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    if (message.text or "").strip() == "/cancel":
        await state.clear()
        await message.answer("Ок, отменил.")
        return
    user = await find_client(message)
    if not user:
        await message.answer(
            "Не нашёл такого клиента среди тех, кто запускал бота. Проверь @ник или ID. "
            "Если пересылаешь сообщение, а клиент скрыл аккаунт в настройках пересылки, "
            "пришли его ID или @ник. Отмена — /cancel")
        return
    await state.clear()
    text, kb = await options_screen(user["user_id"])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Вариант, сумма, отправка ───────────────────────────────────────────────────

@router.callback_query(F.data.startswith("cinv:opt:"))
async def cb_option(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, uid, oid = callback.data.split(":")
    option = await db.get_course_option(int(oid))
    if not option:
        await callback.answer("Вариант не найден.", show_alert=True)
        return
    options = await db.get_course_options(option["product_id"])
    price = svc.price_for_user(option, await db.get_user_course_paid(int(uid)),
                               svc.bundle_option(options))
    await callback.answer()
    await _link_message(callback.message, int(uid), int(oid), price)


@router.callback_query(F.data.startswith("cinv:amount:"))
async def cb_amount(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, uid, oid = callback.data.split(":")
    await state.set_state(CourseInvoice.amount)
    await state.update_data(user_id=int(uid), option_id=int(oid))
    await callback.answer()
    await callback.message.answer("Какую сумму выставить? Число в рублях, например 3990. "
                                  "Отмена — /cancel")


@router.message(CourseInvoice.amount)
async def msg_amount(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    text = (message.text or "").replace(" ", "").replace("₽", "").strip()
    if text == "/cancel":
        await state.clear()
        await message.answer("Ок, сумму не меняю.")
        return
    if not text.isdigit() or not 1 <= int(text) <= 1_000_000:
        await message.answer("Нужно число в рублях, например 3990.")
        return
    data = await state.get_data()
    await state.clear()
    await _link_message(message, data["user_id"], data["option_id"], int(text))


@router.callback_query(F.data.startswith("cinv:send:"))
async def cb_send(callback: CallbackQuery):
    """Ссылку не тащим через callback_data (длинная) — собираем заново с той же
    суммой: у Prodamus каждая ссылка независима."""
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, uid, oid, price = callback.data.split(":")
    uid, oid, price = int(uid), int(oid), int(price)
    option = await db.get_course_option(oid)
    url = payment_url(option, uid, price)
    offer = (f'\n\nОплачивая, ты принимаешь <a href="{config.COURSE_OFFER_URL}">оферту</a>.'
             if config.COURSE_OFFER_URL else "")
    try:
        await callback.bot.send_message(
            uid, f"🧾 <b>Счёт за курс</b>\n{escape(option['title'])} — <b>{svc.rub(price)}</b>\n\n"
                 f"После оплаты доступ откроется сам, и я пришлю сообщение.{offer}",
            parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Оплатить {svc.rub(price)}", url=url)],
                [InlineKeyboardButton(text="🔄 Я оплатил", callback_data="course:check")],
            ]))
    except Exception as e:
        logger.warning(f"course invoice: не отправил клиенту {uid}: {e}")
        await callback.answer()
        await callback.message.answer("⚠️ Клиенту отправить не удалось — возможно, он "
                                      "заблокировал бота. Скопируй ссылку и отправь сам.")
        return
    logger.info(f"course invoice: {callback.from_user.id} → {uid}: {option['slug']} {price} ₽")
    await callback.answer("Отправлено ✅")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Счёт отправлен клиенту: {escape(option['title'])}, "
                                  f"{svc.rub(price)}.", parse_mode="HTML")
