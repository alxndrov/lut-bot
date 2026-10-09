"""
Заказ для друга — только для админов (основной бот).

Админ выбирает товар и проходит тот же опрос, что и клиент, а на шаге
доставки выбирает: 🏠 Самовывоз (без накладной СДЭК) или 🚚 СДЭК как
обычно. Дальше — ссылка на оплату, которую админ пересылает другу, или
🎁 подарок без оплаты.

Самовывоз есть только здесь. Флаг friend_by в состоянии ставит лишь
админский обработчик, и каждый шаг заново проверяет, что жмёт админ —
клиент до самовывоза дойти не может.
"""
import html
import json
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import database as db
from services.prodamus import build_payment_url

router = Router()
logger = logging.getLogger(__name__)


class FriendOrder(StatesGroup):
    method = State()        # самовывоз или СДЭК
    pickup_name = State()   # имя друга для самовывоза


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def _actor(user) -> str:
    return f"@{user.username}" if user.username else (user.first_name or f"id:{user.id}")


async def _orderable_products() -> list[dict]:
    """Физтовары, которые можно оформить опросом (или выбором из наличия)."""
    out = []
    for p in await db.get_all_products(active_only=True):
        if p.get("category") != "physical":
            continue
        if db.is_stock_product(p) or await db.get_product_questions(p["id"]):
            out.append(p)
    return out


# ── Вход: выбор товара ─────────────────────────────────────────────────────────

async def _show_products(message: Message):
    products = await _orderable_products()
    if not products:
        await message.answer("Нет физтоваров с опросом — оформить нечего.")
        return
    rows = [[InlineKeyboardButton(text=f"{p['name']} — {p['price']} ₽",
                                  callback_data=f"friend:pick:{p['id']}")] for p in products]
    await message.answer(
        "🤝 <b>Заказ для друга</b>\n\n"
        "Выбери товар — дальше тот же опрос, что у клиента. На шаге доставки "
        "будет самовывоз или СДЭК, в конце — ссылка на оплату или подарок.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("friend"))
async def cmd_friend(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await _show_products(message)


@router.callback_query(F.data == "admin:friend")
async def cb_friend_menu(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.answer()
    await _show_products(callback.message)


@router.callback_query(F.data.startswith("friend:pick:"))
async def cb_friend_pick(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    product = await db.get_product(int(callback.data.split(":")[2]))
    if not product or product.get("category") != "physical":
        await callback.answer("Товар не найден.", show_alert=True)
        return
    await callback.answer()
    from handlers.brief_handler import start_order_flow
    await callback.message.answer(f"🤝 Заказ для друга: <b>{html.escape(product['name'])}</b>",
                                  parse_mode="HTML")
    await start_order_flow(callback.message, state, product)
    # Метку ставим после старта опроса: start_order_flow очищает состояние
    if await state.get_state():
        await state.update_data(friend_by=_actor(callback.from_user))


# ── Шаг доставки: самовывоз или СДЭК ───────────────────────────────────────────

async def ask_receive_method(target: Message, state: FSMContext):
    """Вызывается из start_delivery_flow вместо вопроса о городе."""
    await state.set_state(FriendOrder.method)
    await target.answer(
        "🤝 Как друг получит заказ?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Самовывоз", callback_data="friend:pickup")],
            [InlineKeyboardButton(text="🚚 СДЭК", callback_data="friend:cdek")],
        ]))


@router.callback_query(FriendOrder.method, F.data == "friend:cdek")
async def cb_friend_cdek(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    from handlers.delivery import DeliveryOrder
    await state.set_state(DeliveryOrder.waiting_city)
    await callback.answer()
    await callback.message.answer("🚚 Введи <b>город</b> друга — рассчитаю доставку:",
                                  parse_mode="HTML")


@router.callback_query(FriendOrder.method, F.data == "friend:pickup")
async def cb_friend_pickup(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(FriendOrder.pickup_name)
    await callback.answer()
    await callback.message.answer("🏠 Самовывоз. Как зовут друга? Имя будет в карточке заказа.")


@router.message(FriendOrder.pickup_name)
async def fsm_pickup_name(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        await state.clear()
        return
    name = (message.text or "").strip()
    if not name or name.startswith("/"):
        await message.answer("Напиши имя друга текстом.")
        return
    data = await state.get_data()
    product = await db.get_product(data["product_id"])
    round_products = data.get("round_products") or [product["id"]]
    from handlers.delivery import _goods_breakdown, _goods_line, _goods_payment_name
    products_by_id = {pid: await db.get_product(pid) for pid in set(round_products)}
    goods_items = _goods_breakdown(round_products, products_by_id)
    total = sum(p["price"] * qty for p, qty in goods_items)
    rounds = data.get("rounds") or []
    friend_by = data.get("friend_by") or _actor(message.from_user)

    await db.save_pending_delivery(
        message.from_user.id, product["id"],
        f"🏠 Самовывоз\nПолучатель: {name}",
        survey_json=json.dumps(rounds, ensure_ascii=False) if rounds else None,
        amount=total, recipient_name=name,
        round_products_json=json.dumps(round_products, ensure_ascii=False),
        pickup=True, friend_by=friend_by,
    )
    await state.clear()
    text = (f"📋 <b>Заказ для друга</b>\n\n{_goods_line(goods_items)}\n"
            f"Получение: 🏠 самовывоз\nПолучатель: {html.escape(name)}\n\n"
            f"<b>Итого: {total} ₽</b>")
    await send_friend_confirm(message, text, product["id"], message.from_user.id, total,
                              _goods_payment_name(goods_items))


# ── Оплата или подарок ─────────────────────────────────────────────────────────

async def send_friend_confirm(message: Message, text: str, product_id: int, admin_id: int,
                              total: int, payment_name: str):
    """Итог заказа для друга: ссылка на оплату (переслать другу) или подарок."""
    rows = []
    url = None
    if config.PRODAMUS_SHOP_URL_PHYSICAL and total > 0:
        url = build_payment_url(
            shop_url=config.PRODAMUS_SHOP_URL_PHYSICAL,
            product_name=payment_name,
            price=total,
            user_id=admin_id,
            product_id=product_id,
            order_type="p",
            secret=config.PRODAMUS_SECRET_PHYSICAL,
            notification_url=config.PRODAMUS_WEBHOOK_URL_PHYSICAL,
        )
        rows.append([InlineKeyboardButton(text=f"💳 Открыть оплату {total} ₽", url=url)])
    rows.append([InlineKeyboardButton(text="🎁 Подарить — без оплаты",
                                      callback_data=f"friend:gift:{product_id}")])
    tail = ("\n\nПерешли другу ссылку на оплату ниже — после оплаты заказ сам уйдёт "
            "в работу, а тебе придёт подтверждение.\n\n"
            f"<code>{html.escape(url)}</code>" if url else
            "\n\n⚠️ Оплата ссылкой сейчас недоступна — можно только подарить.")
    await message.answer(text + tail, parse_mode="HTML", disable_web_page_preview=True,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("friend:gift:"))
async def cb_friend_gift(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    product_id = int(callback.data.split(":")[2])
    pending = await db.get_pending_order(callback.from_user.id, product_id)
    if not pending or not pending.get("friend_by"):
        await callback.answer("Этот заказ уже оформлен или устарел.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        "🎁 Подарить заказ без оплаты? Он сразу уйдёт в работу"
        + ("." if pending.get("pickup") else ", накладную СДЭК оплачиваем мы."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, подарить",
                                 callback_data=f"friend:giftok:{product_id}"),
            InlineKeyboardButton(text="Отмена", callback_data="friend:giftno"),
        ]]))


@router.callback_query(F.data == "friend:giftno")
async def cb_friend_gift_no(callback: CallbackQuery):
    await callback.answer("Ок, не дарим")
    try:
        await callback.message.delete()
    except Exception:
        pass


# Двойное нажатие «Да, подарить» не должно создать два заказа
_GIFT_BUSY: set[tuple[int, int]] = set()


@router.callback_query(F.data.startswith("friend:giftok:"))
async def cb_friend_gift_ok(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    admin_id = callback.from_user.id
    product_id = int(callback.data.split(":")[2])
    key = (admin_id, product_id)
    if key in _GIFT_BUSY:
        await callback.answer("Уже оформляю…")
        return
    _GIFT_BUSY.add(key)
    try:
        pending = await db.get_pending_order(admin_id, product_id)
        if not pending or not pending.get("friend_by"):
            await callback.answer("Этот заказ уже оформлен или устарел.", show_alert=True)
            return
        await callback.answer("Оформляю подарок…")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        from handlers.prodamus_webhook import provision_payment
        await provision_payment(bot, admin_id, product_id, "p",
                                f"gift:{admin_id}:{product_id}:{int(time.time())}", 0,
                                gift=True)
    finally:
        _GIFT_BUSY.discard(key)
