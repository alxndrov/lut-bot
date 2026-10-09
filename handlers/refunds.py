"""
Возврат денег покупателю — кнопка «💸 Провести возврат» в карточке заказа
(админский бот malimadmins).

У Prodamus нет API для возвратов: заявку создают руками в кабинете
(«Список платежей» → платёж → возврат). Поэтому бот ведёт по шагам —
полный или частичный, причина, обратная доставка, расходники — даёт
готовые данные для кабинета и записывает возврат, только когда админ
подтвердил, что заявка в Prodamus оформлена. После этого:
  • бот раз в день напоминает отменить чек в «Мой налог», пока не
    нажмут «Чек отменён»;
  • возврат уменьшает выручку, налог и чистую прибыль периода, в котором
    оформлен (см. db.get_period_revenue, tax_account.RECEIPTS);
  • на листе операций появляется строка refund-N, на листе «Возвраты» —
    подробности;
  • клиенту уходит короткое сообщение от основного бота;
  • просьба об отзыве по этому заказу отменяется.

Обратная доставка и расходники могут быть ещё неизвестны (посылка в
пути) — тогда их можно указать позже кнопками в карточке.
"""
import html
import logging
import re

from aiogram import Bot, Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton,
)

import config
import database as db
from services.tax_account import parse_amount

router = Router()
logger = logging.getLogger(__name__)

PRODAMUS_MIN_REFUND = 100      # меньше Prodamus не возвращает


class RefundStates(StatesGroup):
    waiting_amount = State()
    waiting_reason = State()
    waiting_delivery = State()


def _kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def _rub(x: float) -> str:
    s = f"{x:,.2f}"
    s = s[:-3] if s.endswith(".00") else s
    return s.replace(",", " ").replace(".", ",") + " ₽"


def _num(order: dict) -> str:
    from handlers.order_actions import _order_num
    return _order_num(order)


def _actor(callback: CallbackQuery) -> str:
    from handlers.order_actions import _actor_name
    return _actor_name(callback)


def _consumables_label(order: dict) -> str:
    """«Коробка ×1, Поп-фильтр ×1» — что вернётся на склад с этим заказом."""
    from handlers import consumables
    from handlers.order_actions import _order_positions
    total = _order_positions(order)
    products = consumables._positions_products(order, total)
    counts: dict[str, int] = {}
    for pos in range(total):
        pid = products[pos] if pos < len(products) else order["product_id"]
        for key in consumables.CONSUMABLE_RULES.get(pid, []):
            counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{consumables.CONSUMABLE_NAMES.get(k, k)} ×{n}" for k, n in counts.items())


async def _refresh_cards(bot: Bot, order_id: int):
    from handlers.order_actions import _sync_order_messages
    order = await db.get_order(order_id)
    if order:
        await _sync_order_messages(bot, order)


def _sync_finance():
    from services.finance_sheet import request_finance_sync
    request_finance_sync()


async def _load(callback: CallbackQuery) -> dict | None:
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return None
    order = await db.get_order(int(callback.data.split(":")[2]))
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
    return order


# ── Шаг 1: полный или частичный ─────────────────────────────────────────

@router.callback_query(F.data.startswith("rf:start:"))
async def cb_start(callback: CallbackQuery, state: FSMContext):
    order = await _load(callback)
    if not order:
        return
    if order.get("refunded_at"):
        await callback.answer("По этому заказу возврат уже оформлен.", show_alert=True)
        return
    paid = await db.order_paid_amount(order)
    if paid <= 0:
        await callback.answer("По этому заказу не было оплаты — возвращать нечего.",
                              show_alert=True)
        return
    await state.clear()
    await state.update_data(oid=order["id"], paid=paid)
    await callback.answer()
    await callback.message.answer(
        f"💸 <b>Возврат по заказу №{_num(order)}</b>\n"
        f"Клиент заплатил: <b>{_rub(paid)}</b>\n\n"
        f"Возвращаем всё или часть?",
        parse_mode="HTML",
        reply_to_message_id=callback.message.message_id,
        reply_markup=_kb([(f"Полный — {_rub(paid)}", f"rf:full:{order['id']}")],
                         [("Частичный", f"rf:part:{order['id']}")],
                         [("❌ Отмена", f"rf:cancel:{order['id']}")]),
    )


def _same_order(data: dict, callback: CallbackQuery) -> bool:
    return data.get("oid") == int(callback.data.split(":")[2])


@router.callback_query(F.data.startswith("rf:cancel:"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    if _same_order(await state.get_data(), callback):
        await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_text("❌ Возврат не оформлен — в учёте ничего не изменилось.")


@router.callback_query(F.data.startswith("rf:full:"))
async def cb_full(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not _same_order(data, callback):
        await callback.answer("Начните заново кнопкой в карточке заказа.", show_alert=True)
        return
    await state.update_data(amount=data["paid"], full=True)
    await callback.answer()
    await _ask_reason(callback.message, state, edit=True)


@router.callback_query(F.data.startswith("rf:part:"))
async def cb_part(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not _same_order(data, callback):
        await callback.answer("Начните заново кнопкой в карточке заказа.", show_alert=True)
        return
    await state.set_state(RefundStates.waiting_amount)
    await callback.answer()
    await callback.message.edit_text(
        f"Сколько вернуть? Напишите сумму от {PRODAMUS_MIN_REFUND} до "
        f"{_rub(data['paid'])} — меньше {PRODAMUS_MIN_REFUND} ₽ Prodamus не возвращает.\n\n"
        f"/cancel — отменить")


@router.message(RefundStates.waiting_amount, F.text)
async def on_amount(message: Message, state: FSMContext):
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Возврат не оформлен.")
        return
    data = await state.get_data()
    try:
        amount = parse_amount(message.text)
    except ValueError as e:
        await message.answer(str(e))
        return
    if not PRODAMUS_MIN_REFUND <= amount <= data["paid"]:
        await message.answer(f"Нужна сумма от {PRODAMUS_MIN_REFUND} до {_rub(data['paid'])}.")
        return
    await state.update_data(amount=amount, full=abs(amount - data["paid"]) < 0.005)
    await _ask_reason(message, state)


# ── Шаг 2: причина ──────────────────────────────────────────────────────

async def _ask_reason(message: Message, state: FSMContext, edit: bool = False):
    await state.set_state(RefundStates.waiting_reason)
    text = ("Почему возврат? Напишите коротко — причина попадёт в карточку заказа "
            "и в таблицу.\n\n/cancel — отменить")
    if edit:
        await message.edit_text(text)
    else:
        await message.answer(text)


@router.message(RefundStates.waiting_amount)
@router.message(RefundStates.waiting_reason, ~F.text)
async def on_not_text(message: Message):
    await message.answer("Нужен текст. /cancel — отменить")


@router.message(RefundStates.waiting_reason, F.text)
async def on_reason(message: Message, state: FSMContext, bot: Bot):
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Возврат не оформлен.")
        return
    await state.update_data(reason=message.text.strip()[:500])
    await state.set_state(None)
    data = await state.get_data()
    order = await db.get_order(data["oid"])
    if not order.get("shipped_at"):
        # Посылка не уходила — обратной доставки и расходников нет
        await state.update_data(delivery=0.0, stock=None)
        await _show_confirm(message, state)
        return
    await _ask_delivery(message, state, order, flow=True)


# ── Шаг 3: обратная доставка ────────────────────────────────────────────

async def _ask_delivery(message: Message, state: FSMContext, order: dict, flow: bool):
    """Ищем обратную накладную в СДЭК; нет — спрашиваем сумму."""
    found = None
    if order.get("cdek_uuid"):
        from services.cdek_accounts import client_for_order
        client = client_for_order(order)
        if client:
            found = await client.get_return_cost(order["cdek_uuid"])
    p = "rf:dl" if flow else "rf:dL"
    oid = order["id"]
    rows = []
    if found and found.get("total"):
        await state.update_data(cdek_back=found["total"])
        text = (f"🚚 СДЭК завёл обратную накладную {found['cdek_number']}"
                + (f" ({found['status']})" if found.get("status") else "")
                + f": <b>{_rub(found['total'])}</b>.\n\nУчесть эту сумму как обратную доставку?")
        rows.append([(f"✅ Да, {_rub(found['total'])}", f"{p}:{oid}:cdek")])
    else:
        text = ("🚚 Обратной накладной к этому заказу в СДЭК нет — похоже, клиент "
                "отправляет посылку отдельно.\n\nСколько стоит обратная доставка для нас? "
                "Напишите сумму")
    text += "\n\nИли:"
    rows += [[("0 — платит клиент", f"{p}:{oid}:0")],
             [("Пока неизвестно — укажу позже", f"{p}:{oid}:later")]]
    if flow:
        rows.append([("❌ Отмена", f"rf:cancel:{oid}")])
    await state.update_data(oid=oid, delivery_flow=flow)
    await state.set_state(RefundStates.waiting_delivery)
    await message.answer(text, parse_mode="HTML", reply_markup=_kb(*rows))


async def _delivery_chosen(message: Message, state: FSMContext, value: float | None,
                           edit: bool, actor: str):
    data = await state.get_data()
    await state.set_state(None)
    if data.get("delivery_flow"):
        await state.update_data(delivery=value)
        order = await db.get_order(data["oid"])
        await _ask_stock(message, state, order, flow=True, edit=edit)
        return
    # Карточка уже с возвратом — просто дописываем
    await state.clear()
    if value is None:
        text = "Хорошо, укажете позже — кнопка останется в карточке."
    elif await db.set_refund_field(data["oid"], "return_delivery", value):
        _sync_finance()
        await _refresh_cards(message.bot, data["oid"])
        logger.info(f"refund: заказ {data['oid']} — обратная доставка {value} ({actor})")
        text = f"✅ Обратная доставка учтена: {_rub(value)}."
    else:
        text = "Обратная доставка по этому возврату уже указана."
    if edit:
        await message.edit_text(text)
    else:
        await message.answer(text)


@router.callback_query(F.data.startswith("rf:dl:") | F.data.startswith("rf:dL:"))
async def cb_delivery(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not _same_order(data, callback):
        await callback.answer("Начните заново кнопкой в карточке заказа.", show_alert=True)
        return
    choice = callback.data.split(":")[3]
    value = {"cdek": data.get("cdek_back"), "0": 0.0, "later": None}[choice]
    await callback.answer()
    await _delivery_chosen(callback.message, state, value, edit=True, actor=_actor(callback))


@router.message(RefundStates.waiting_delivery, F.text)
async def on_delivery(message: Message, state: FSMContext):
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.")
        return
    try:
        value = parse_amount(message.text)
    except ValueError as e:
        await message.answer(str(e))
        return
    u = message.from_user
    await _delivery_chosen(message, state, value, edit=False,
                           actor=f"@{u.username}" if u.username else str(u.id))


@router.callback_query(F.data.startswith("rf:back:"))
async def cb_delivery_later(callback: CallbackQuery, state: FSMContext):
    """Кнопка в карточке: обратную доставку указали «позже» — указываем."""
    order = await _load(callback)
    if not order:
        return
    if order.get("refund_return_delivery") is not None:
        await callback.answer("Обратная доставка уже указана.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _ask_delivery(callback.message, state, order, flow=False)


# ── Шаг 4: расходники ───────────────────────────────────────────────────

async def _ask_stock(message: Message, state: FSMContext, order: dict, flow: bool,
                     edit: bool = False):
    label = _consumables_label(order)
    if not label:
        await state.update_data(stock=False)
        if flow:
            await _show_confirm(message, state, edit=edit)
        return
    p = "rf:st" if flow else "rf:sT"
    oid = order["id"]
    rows = [[("✅ Да, вернуть на склад", f"{p}:{oid}:1")],
            [("Нет — испорчены/не вернулись", f"{p}:{oid}:0")]]
    if flow:
        rows += [[("Решу, когда посылка приедет", f"{p}:{oid}:later")],
                 [("❌ Отмена", f"rf:cancel:{oid}")]]
    await state.update_data(oid=oid)
    text = f"📦 Вернуть на склад расходники из заказа: {label}?"
    if edit:
        await message.edit_text(text, reply_markup=_kb(*rows))
    else:
        await message.answer(text, reply_markup=_kb(*rows))


async def _return_stock(user_id: int, order: dict):
    from handlers import consumables
    from handlers.order_actions import _order_positions
    total = _order_positions(order)
    await consumables.apply_stock_delta(user_id, order, set(range(1, total + 1)), +1)


@router.callback_query(F.data.startswith("rf:st:"))
async def cb_stock_flow(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not _same_order(data, callback):
        await callback.answer("Начните заново кнопкой в карточке заказа.", show_alert=True)
        return
    choice = callback.data.split(":")[3]
    await state.update_data(stock={"1": True, "0": False, "later": None}[choice])
    await callback.answer()
    await _show_confirm(callback.message, state, edit=True)


@router.callback_query(F.data.startswith("rf:stock:"))
async def cb_stock_later(callback: CallbackQuery, state: FSMContext):
    """Кнопка в карточке: посылка приехала — решаем про расходники."""
    order = await _load(callback)
    if not order:
        return
    if order.get("refund_stock_returned") is not None:
        await callback.answer("Про расходники уже решено.", show_alert=True)
        return
    await callback.answer()
    await _ask_stock(callback.message, state, order, flow=False)


@router.callback_query(F.data.startswith("rf:sT:"))
async def cb_stock_set(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return
    back = callback.data.split(":")[3] == "1"
    if not await db.set_refund_field(order["id"], "stock_returned", int(back)):
        await callback.answer("Про расходники уже решено.", show_alert=True)
        return
    if back:
        await _return_stock(callback.from_user.id, order)
    logger.info(f"refund: заказ {order['id']} — расходники на склад: {back} ({_actor(callback)})")
    await callback.answer("Готово")
    await callback.message.edit_text(
        f"✅ Расходники вернулись на склад: {_consumables_label(order)}." if back
        else "Хорошо, расходники на склад не возвращаем.")
    await _refresh_cards(callback.bot, order["id"])


# ── Шаг 5: оформить в Prodamus и подтвердить ────────────────────────────

async def _show_confirm(message: Message, state: FSMContext, edit: bool = False):
    data = await state.get_data()
    order = await db.get_order(data["oid"])
    amount, delivery, stock = data["amount"], data.get("delivery"), data.get("stock")
    lines = [
        f"💸 <b>Возврат по заказу №{_num(order)}</b>",
        f"Сумма: <b>{_rub(amount)}</b> ({'полный' if data['full'] else 'частичный'}"
        f" из {_rub(data['paid'])})",
        f"Причина: {html.escape(data['reason'])}",
    ]
    if order.get("shipped_at"):
        lines.append("Обратная доставка: " + ("укажу позже" if delivery is None else _rub(delivery)))
        if _consumables_label(order):
            lines.append("Расходники на склад: " + (
                "решу, когда приедет" if stock is None else ("да" if stock else "нет")))
    else:
        lines.append("Заказ ещё не отправлен — он уйдёт из очереди заказов." if data["full"]
                     else "Заказ ещё не отправлен — останется в очереди.")
    created = order.get("created_at") or ""
    lines += [
        "",
        "<b>Теперь оформите возврат в кабинете Prodamus:</b>",
        f"1. «Список платежей» → платёж <code>{html.escape(order.get('prodamus_order_id') or '')}</code>"
        + (f" от {created[8:10]}.{created[5:7]}" if created else "")
        + f" на {_rub(data['paid'])}",
        f"2. «Возврат» → сумма <b>{_rub(amount)}</b>",
        "3. Способ — «Удержать из следующих поступлений». Если за 10 дней "
        "поступлений не хватит, Prodamus заявку отменит — тогда переводом по реквизитам.",
        "",
        "Когда заявка создана — нажмите кнопку ниже. До этого в учёте ничего не меняется.",
    ]
    kb = _kb([("✅ Возврат в Prodamus оформлен", f"rf:done:{order['id']}")],
             [("❌ Отмена", f"rf:cancel:{order['id']}")])
    if edit:
        await message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


async def _notify_client(order: dict, amount: float) -> bool:
    text = (f"Возврат по заказу №{_num(order)} оформлен: <b>{_rub(amount)}</b> вернутся "
            f"на карту, с которой вы платили. Обычно это 1–5 рабочих дней, но иногда "
            f"банку нужно больше времени.\n\nЕсли будут вопросы — просто напишите сюда.")
    main_bot = Bot(token=config.BOT_TOKEN)
    try:
        await main_bot.send_message(order["user_id"], text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning(f"refund: не сообщил клиенту {order['user_id']}: {e}")
        return False
    finally:
        await main_bot.session.close()


async def _drop_review_push(order: dict):
    """Просить отзыв у человека, которому вернули деньги, — странно."""
    push = await db.pending_review_push(order["user_id"])
    if push and push.get("order_id") == order["id"]:
        await db.drop_review_push(push["id"])


@router.callback_query(F.data.startswith("rf:done:"))
async def cb_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not _same_order(data, callback) or "reason" not in data:
        await callback.answer("Начните заново кнопкой в карточке заказа.", show_alert=True)
        return
    order = await db.get_order(data["oid"])
    actor = _actor(callback)
    refund_id = await db.add_refund(
        order["id"], data["amount"], data["paid"], data["full"], data["reason"],
        data.get("delivery"), data.get("stock"), callback.from_user.id, actor)
    await state.clear()
    if refund_id is None:
        await callback.answer("Возврат по этому заказу уже записан.", show_alert=True)
        return
    await callback.answer("Записано ✅")
    logger.info(f"refund: заказ {order['id']} — возврат {data['amount']} "
                f"({'полный' if data['full'] else 'частичный'}), {actor}")

    if data.get("stock"):
        await _return_stock(callback.from_user.id, order)
    await _drop_review_push(order)
    notified = await _notify_client(order, data["amount"])
    if notified:
        await db.set_refund_field(order["id"], "client_notified", 1)
    _sync_finance()
    await _refresh_cards(callback.bot, order["id"])

    refund = next(r for r in await db.get_refunds() if r["order_id"] == order["id"])
    lines = [
        f"✅ <b>Возврат по заказу №{_num(order)} записан: {_rub(data['amount'])}</b>",
        "",
        "• Выручка, налог и прибыль текущего периода уменьшены — делёж 60/40 "
        "считается уже с учётом возврата",
        "• В таблице: строка на листе операций и запись на листе «Возвраты»",
        "• Клиенту отправлено сообщение о возврате" if notified
        else "• ⚠️ Клиенту написать не удалось (заблокировал бота?) — напишите сами",
        "",
        receipt_text(refund),
    ]
    if order.get("shipped_at") and data.get("delivery") is None:
        lines.append("<i>Обратную доставку укажите кнопкой в карточке, когда станет известна.</i>")
    if order.get("shipped_at") and data.get("stock") is None and _consumables_label(order):
        lines.append("<i>Когда посылка приедет — решите про расходники кнопкой в карточке.</i>")
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML",
                                     reply_markup=_receipt_kb(order["id"]))


# ── Чек в «Мой налог» ───────────────────────────────────────────────────
# Prodamus деньги вернёт, а чек о доходе в «Мой налог» сам не отменит —
# без этого налог заплатим с денег, которых у нас уже нет. Напоминаем
# раз в день, пока не нажмут «Чек отменён».

REMIND_HOUR_MSK = 10
REMIND_CHECK_EVERY = 30 * 60


def _msk_day(ts: str | None) -> str:
    from datetime import datetime, timedelta
    if not ts:
        return ""
    return (datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S") + timedelta(hours=3)).strftime("%d.%m")


def receipt_text(refund: dict) -> str:
    num = (re.findall(r"\d+", refund.get("order_code") or "") or [str(refund["order_id"])])[-1]
    tax = refund["amount"] * config.NPD_PERCENT / 100
    text = (f"🧾 <b>Отмените чек в «Мой налог»</b> по возврату №{num}:\n"
            f"«Мой налог» → Продажи → чек от {_msk_day(refund.get('order_created_at'))} "
            f"на {_rub(refund['paid'])} → «Аннулировать» → причина «Возврат средств».")
    if not refund.get("full"):
        text += (f"\nВозврат частичный — после этого пробейте новый чек на "
                 f"{_rub(refund['paid'] - refund['amount'])}: эта часть осталась у нас.")
    text += f"\n<i>В учёте бота налог уже уменьшен на {_rub(tax)}.</i>"
    return text


def _receipt_kb(order_id: int) -> InlineKeyboardMarkup:
    return _kb([("✅ Чек отменён", f"rf:rcpt:{order_id}")])


@router.callback_query(F.data.startswith("rf:rcpt:") | F.data.startswith("rf:rcptc:"))
async def cb_receipt(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return
    if not await db.mark_receipt_cancelled(order["id"]):
        await callback.answer("Уже отмечено.", show_alert=True)
        return
    logger.info(f"refund: заказ {order['id']} — чек в «Мой налог» отменён ({_actor(callback)})")
    await callback.answer("Отмечено ✅ Больше не напоминаю")
    if callback.data.startswith("rf:rcpt:"):
        # Кнопка в напоминании — само напоминание закрываем; в карточке
        # текст не трогаем, её перерисует _refresh_cards
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.reply("✅ Чек отменён — больше не напоминаю.")
    _sync_finance()
    await _refresh_cards(callback.bot, order["id"])


async def remind_receipts(bot: Bot):
    from datetime import datetime, timedelta, timezone
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    if now_msk.hour < REMIND_HOUR_MSK:
        return
    today = now_msk.strftime("%d.%m")
    for r in await db.get_refunds():
        if r.get("receipt_cancelled_at"):
            continue
        # В день возврата напоминание уже было в сообщении об оформлении
        if _msk_day(r["created_at"]) == today or _msk_day(r.get("receipt_reminded_at")) == today:
            continue
        uid = r["created_by_id"] if r.get("created_by_id") in config.ADMIN_IDS else config.PARTNER_ID
        try:
            await bot.send_message(
                uid, "⏰ Напоминание\n" + receipt_text(r), parse_mode="HTML",
                reply_markup=_receipt_kb(r["order_id"]),
                reply_to_message_id=await db.order_card_in_chat(r["order_id"], uid),
                allow_sending_without_reply=True)
        except Exception as e:
            logger.warning(f"refund: не напомнил про чек по заказу {r['order_id']}: {e}")
            continue
        await db.mark_receipt_reminded(r["id"])
        logger.info(f"refund: напомнил {uid} отменить чек по заказу {r['order_id']}")


async def receipt_reminder_loop(bot: Bot):
    import asyncio
    while True:
        try:
            await remind_receipts(bot)
        except Exception:
            logger.exception("refund: напоминание про чек не сработало")
        await asyncio.sleep(REMIND_CHECK_EVERY)


# ── Карточка заказа ─────────────────────────────────────────────────────

def refund_line(order: dict) -> str:
    """Строка о возврате в карточке заказа."""
    if not order.get("refunded_at"):
        return ""
    from handlers.order_actions import _msk
    kind = "полный" if order.get("refund_full") else "частичный"
    text = f"\n💸 <b>Возврат:</b> {_rub(order['refund_amount'])} ({kind}) · {_msk(order['refunded_at'])}"
    if order.get("refund_reason"):
        text += f"\n    <i>{html.escape(order['refund_reason'])}</i>"
    return text


def refund_rows(order: dict) -> list[list[InlineKeyboardButton]]:
    """Кнопки возврата: оформить, а после — дописать то, что было неизвестно."""
    oid = order["id"]
    if order.get("repeat_of_order_id") or str(order.get("prodamus_order_id") or "").startswith("gift:"):
        return []      # бесплатный повтор или подарок другу — денег за них не платили
    if not order.get("refunded_at"):
        return [[InlineKeyboardButton(text="💸 Провести возврат", callback_data=f"rf:start:{oid}")]]
    rows = []
    if not order.get("refund_receipt_cancelled_at"):
        rows.append([InlineKeyboardButton(text="🧾 Чек в «Мой налог» отменён",
                                          callback_data=f"rf:rcptc:{oid}")])
    if order.get("shipped_at") and order.get("refund_return_delivery") is None:
        rows.append([InlineKeyboardButton(text="🚚 Указать обратную доставку",
                                          callback_data=f"rf:back:{oid}")])
    if (order.get("shipped_at") and order.get("refund_stock_returned") is None
            and _consumables_label(order)):
        rows.append([InlineKeyboardButton(text="📦 Посылка вернулась — расходники на склад?",
                                          callback_data=f"rf:stock:{oid}")])
    return rows
