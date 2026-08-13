"""
Расходники под физические товары (malimadmins): коробки и поп-фильтры.

Списываются автоматически при отметке заказа «отправлен» и возвращаются
при снятии отметки — привязка к отправке, а не к печати или оплате,
потому что именно тогда товар реально уходит в коробку.
"""
import json
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

import config
import database as db
from handlers.expenses import _nav_row

router = Router()
logger = logging.getLogger(__name__)

# Сколько позиций каждого товара расходует каждый расходник.
# Коробка общая на микрофоны и кейсы, поп-фильтр — только на микрофоны.
CONSUMABLE_RULES: dict[int, list[str]] = {
    config.MIC_PRODUCT_ID: ["box", "pop_filter"],
    config.CASE_PRODUCT_ID: ["box"],
}

LOW_THRESHOLD = 5


class StockStates(StatesGroup):
    waiting_qty = State()


async def _queue_need() -> dict[str, int]:
    """Сколько каждого расходника нужно на ещё не отправленные заказы.

    Списание происходит при отправке заказа целиком, поэтому в очередь
    идут ВСЕ позиции неотправленных заказов — независимо от того,
    напечатаны они уже или нет. Импорт внутри функции, чтобы не ловить
    цикл импортов (order_actions зовёт этот модуль обратно при отправке).
    """
    from handlers.order_actions import _order_positions

    orders = await db.get_orders(only_unshipped=True)
    need: dict[str, int] = {}
    for order in orders:
        total = _order_positions(order)
        round_products = _positions_products(order, total)
        for pos in range(1, total + 1):
            pid = round_products[pos - 1] if pos - 1 < len(round_products) else order["product_id"]
            for key in CONSUMABLE_RULES.get(pid, []):
                need[key] = need.get(key, 0) + 1
    return need


def _positions_products(order: dict, total: int) -> list[int]:
    """Товар каждой позиции заказа — как считает сам бот (см. unpack_round_products)."""
    try:
        rounds = json.loads(order.get("rounds_json") or "[]")
    except Exception:
        rounds = []
    round_products = db.unpack_round_products(
        order.get("round_products_json"), rounds, order["product_id"])
    if not round_products:
        round_products = [order["product_id"]] * total
    return round_products


async def apply_stock_delta(order: dict, positions: set[int], sign: int) -> set[str]:
    """Списывает (sign=-1) или возвращает (sign=+1) расходники под positions.

    Возвращает ключи задетых расходников — по ним потом проверяем остаток.
    """
    if not positions:
        return set()
    from handlers.order_actions import _order_positions

    round_products = _positions_products(order, _order_positions(order))
    touched: set[str] = set()
    for pos in positions:
        pid = round_products[pos - 1] if pos - 1 < len(round_products) else order["product_id"]
        for key in CONSUMABLE_RULES.get(pid, []):
            await db.adjust_consumable(key, sign)
            touched.add(key)
    return touched


async def stock_note(key: str) -> str | None:
    """Текст предупреждения по одному расходнику, если остаток низкий/недостаточный."""
    items = {c["key"]: c for c in await db.get_consumables()}
    c = items.get(key)
    if not c:
        return None
    need = (await _queue_need()).get(key, 0)
    qty = c["qty"]
    if qty < need:
        return (f"🚨 <b>{c['name']}</b>: осталось {qty} шт., а на заказы в очереди нужно "
                f"{need} — не хватает {need - qty}! Закажите срочно.")
    if qty <= LOW_THRESHOLD:
        return f"⚠️ <b>{c['name']}</b>: осталось {qty} шт. — пора заказать ещё."
    return None


async def _stock_text() -> str:
    items = await db.get_consumables()
    need = await _queue_need()
    lines = ["📦 <b>Расходники</b>", ""]
    for c in items:
        key, name, qty = c["key"], c["name"], c["qty"]
        n = need.get(key, 0)
        if qty < n:
            lines.append(f"🚨 <b>{name}:</b> {qty} шт. — не хватает {n - qty} шт. на очередь "
                        f"(нужно {n})!")
        elif qty <= LOW_THRESHOLD:
            lines.append(f"⚠️ <b>{name}:</b> {qty} шт. — заканчивается, нужно под очередь: {n}")
        else:
            lines.append(f"✅ <b>{name}:</b> {qty} шт. (нужно под очередь: {n})")
    return "\n".join(lines)


def _stock_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"➕ Пополнить: {c['name']}",
                                  callback_data=f"stk_add:{c['key']}")]
            for c in items]
    rows += _nav_row("stk")
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_stock(message: Message, edit: bool = False):
    text = await _stock_text()
    markup = _stock_keyboard(await db.get_consumables())
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("stock"))
async def cmd_stock(message: Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await state.clear()
    await _show_stock(message)


@router.callback_query(F.data == "stk_show")
async def cb_stock_show(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _show_stock(callback.message, edit=True)


@router.callback_query(F.data.startswith("stk_add:"))
async def cb_stock_add(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    items = {c["key"]: c for c in await db.get_consumables()}
    c = items.get(key)
    if not c:
        await callback.answer("Такого расходника нет")
        return
    await state.update_data(key=key)
    await state.set_state(StockStates.waiting_qty)
    await callback.answer()
    await callback.message.answer(
        f"Сколько «{c['name']}» привезли/добавить к остатку? Напишите числом.")


@router.message(StockStates.waiting_qty)
async def on_stock_qty(message: Message, state: FSMContext):
    try:
        n = int((message.text or "").strip())
    except ValueError:
        await message.answer("Не понял число. Напишите просто количество, например 20.")
        return
    if n == 0:
        await message.answer("Ноль добавлять незачем — напишите ненулевое число.")
        return
    data = await state.get_data()
    key = data.get("key")
    await state.clear()
    new_qty = await db.adjust_consumable(key, n)
    if new_qty is None:
        await message.answer("Такого расходника уже нет.")
        return
    await message.answer(f"✅ Остаток обновлён: {new_qty} шт.")
    await _show_stock(message)
