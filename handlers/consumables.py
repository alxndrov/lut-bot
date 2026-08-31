"""
Расходники под физические товары (malimadmins): коробки и поп-фильтры.

У каждого админа свой остаток — Даня и Миша печатают и отправляют из
разных мест, поэтому запасы не общие. Списываются автоматически при
отметке заказа «отправлен» и возвращаются при снятии отметки — привязка
к отправке, а не к печати или оплате, потому что именно тогда товар
реально уходит в коробку. Списывается со счёта того, кто нажал кнопку
(он и паковал), возвращается тому, кто изначально отправил.
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
CONSUMABLE_NAMES: dict[str, str] = {"box": "Коробка", "pop_filter": "Поп-фильтр"}

LOW_THRESHOLD = 5


class StockStates(StatesGroup):
    waiting_qty = State()


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


async def _queue_need_by_admin() -> tuple[dict[int, dict[str, int]], dict[str, int]]:
    """Сколько каждого расходника нужно на ещё не отправленные заказы.

    Считаем по тому, за кем сейчас числится заказ (assignee) — он и будет
    паковать/отправлять. У заказов без исполнителя пока непонятно, чей
    это расход — они уходят во второй, «нераспределённый» счётчик.
    Списание происходит при отправке заказа целиком, поэтому в очередь
    идут ВСЕ позиции неотправленных заказов, а не только напечатанные.
    Импорт внутри функции — иначе цикл импортов с order_actions.
    """
    from handlers.order_actions import _order_positions

    orders = await db.get_orders(only_unshipped=True)
    need: dict[int, dict[str, int]] = {}
    unassigned: dict[str, int] = {}
    for order in orders:
        total = _order_positions(order)
        round_products = _positions_products(order, total)
        assignee = order.get("assignee_id")
        for pos in range(1, total + 1):
            pid = round_products[pos - 1] if pos - 1 < len(round_products) else order["product_id"]
            for key in CONSUMABLE_RULES.get(pid, []):
                if assignee:
                    need.setdefault(assignee, {})
                    need[assignee][key] = need[assignee].get(key, 0) + 1
                else:
                    unassigned[key] = unassigned.get(key, 0) + 1
    return need, unassigned


async def apply_stock_delta(user_id: int, order: dict, positions: set[int], sign: int) -> set[str]:
    """Списывает (sign=-1) или возвращает (sign=+1) расходники user_id под positions.

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
            await db.adjust_consumable(user_id, key, sign)
            touched.add(key)
    return touched


async def stock_note(user_id: int, key: str) -> str | None:
    """Текст предупреждения по одному расходнику этого админа, если остаток низкий/недостаточный."""
    items = {c["key"]: c for c in await db.get_consumables(user_id)}
    c = items.get(key)
    if not c:
        return None
    need_by_admin, _ = await _queue_need_by_admin()
    need = need_by_admin.get(user_id, {}).get(key, 0)
    qty = c["qty"]
    if qty < need:
        return (f"🚨 <b>{c['name']}</b> у вас: осталось {qty} шт., а на вашу очередь нужно "
                f"{need} — не хватает {need - qty}! Закажите срочно.")
    if qty <= LOW_THRESHOLD:
        return f"⚠️ <b>{c['name']}</b> у вас: осталось {qty} шт. — пора заказать ещё."
    return None


async def _stock_text() -> str:
    from handlers.order_actions import _ensure_admin_names, _ADMIN_NAMES
    await _ensure_admin_names()

    need_by_admin, unassigned = await _queue_need_by_admin()
    lines = ["📦 <b>Расходники</b>", ""]
    # Показываем только тех, у кого расходники реально есть или на кого
    # висит очередь: кто не печатает, тому и склад ни к чему.
    for uid in config.ADMIN_IDS:
        stock = await db.get_consumables(uid)
        need = need_by_admin.get(uid, {})
        if not stock and not need:
            continue
        lines.append(f"<b>{_ADMIN_NAMES.get(uid, f'id:{uid}')}</b>")
        for c in stock:
            key, name, qty = c["key"], c["name"], c["qty"]
            n = need.get(key, 0)
            if qty < n:
                lines.append(f"  🚨 {name}: {qty} шт. — не хватает {n - qty} шт. на очередь "
                            f"(нужно {n})!")
            elif qty <= LOW_THRESHOLD:
                lines.append(f"  ⚠️ {name}: {qty} шт. — заканчивается (нужно под очередь: {n})")
            else:
                lines.append(f"  ✅ {name}: {qty} шт. (нужно под очередь: {n})")
        for key, n in need.items():
            if n and not any(c["key"] == key for c in stock):
                lines.append(f"  🚨 {CONSUMABLE_NAMES.get(key, key)}: запаса нет, "
                            f"а на очередь нужно {n} шт.!")
        lines.append("")
    if unassigned:
        lines.append("❔ <b>Не распределено</b> (заказ ещё никто не взял в работу):")
        for key, n in unassigned.items():
            lines.append(f"  {CONSUMABLE_NAMES.get(key, key)}: понадобится ещё {n} шт.")
    return "\n".join(lines).rstrip()


def _stock_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"➕ Пополнить свой запас: {name}",
                                  callback_data=f"stk_add:{key}")]
            for key, name in CONSUMABLE_NAMES.items()]
    rows += _nav_row("stk")
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_stock(message: Message, edit: bool = False):
    text = await _stock_text()
    markup = _stock_keyboard()
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
    name = CONSUMABLE_NAMES.get(key)
    if not name:
        await callback.answer("Такого расходника нет")
        return
    await state.update_data(key=key)
    await state.set_state(StockStates.waiting_qty)
    await callback.answer()
    await callback.message.answer(
        f"Сколько «{name}» привезли/добавить к вашему остатку? Напишите числом.")


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
    new_qty = await db.adjust_consumable(message.from_user.id, key, n)
    if new_qty is None:
        await message.answer("Такого расходника нет.")
        return
    await message.answer(f"✅ Ваш остаток обновлён: {new_qty} шт.")
    await _show_stock(message)
