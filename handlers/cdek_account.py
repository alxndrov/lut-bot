"""
Счёт СДЭК в админском боте (malimadmins): сколько денег занесли на договор,
сколько с него списалось по накладным и что осталось.

Это не /expense. Доставку оплачивает клиент, и в дележ прибыли она уже
входит транзитом (services.payout.delivery_out) — второй раз вычитать её
нельзя. Здесь считается только сам счёт СДЭК: пополнения вносим руками,
списания бот берёт из заказов, по которым заведена накладная.
"""
import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton,
)

import config
import database as db
from handlers.expenses import MSK, _month_bounds, _msk, _parse_amount

router = Router()
logger = logging.getLogger(__name__)


class CdekStates(StatesGroup):
    waiting_topup = State()
    waiting_writeoff = State()


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def _shipments(n: int) -> str:
    return f"{n} {_plural(n, 'отправка', 'отправки', 'отправок')}"


def _rub(value: float) -> str:
    """Сумма со знаком минус как в отчётах — «−», а не дефис."""
    return f"{value:,.2f}".replace("-", "−")


def _account_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Пополнил счёт", callback_data="cdek_add")],
        [InlineKeyboardButton(text="➖ Списать вручную", callback_data="cdek_spend")],
        [InlineKeyboardButton(text="📋 История", callback_data="cdek_log")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="cdek_show")],
    ])


async def _account_text() -> str:
    """Экран счёта: остаток, сколько внесли и на что ушло."""
    date_from, date_to = _month_bounds()
    a = await db.get_cdek_account(date_from, date_to)
    month = datetime.now(MSK).strftime("%m.%Y")

    if not a["entries"] and not a["orders_count"]:
        return (
            "🚚 <b>Счёт СДЭК</b>\n\n"
            "Движений по счёту пока нет.\n\n"
            "Нажмите «Пополнил счёт», когда занесёте деньги на договор — "
            "дальше бот сам будет вычитать стоимость каждой накладной "
            "и показывать остаток."
        )

    lines = [
        "🚚 <b>Счёт СДЭК</b>",
        "",
        f"💰 Сейчас на счету: <b>{_rub(a['balance'])} ₽</b>",
        "",
        f"➕ Внесено всего: <b>{a['topups']:,.2f} ₽</b>",
        f"➖ Потрачено всего: <b>{a['spent']:,.2f} ₽</b>",
    ]
    if a["orders_spent"]:
        lines.append(f"     • накладные: {a['orders_spent']:,.2f} ₽ "
                     f"({_shipments(a['orders_count'])})")
    if a["manual_spent"]:
        lines.append(f"     • вручную: {a['manual_spent']:,.2f} ₽")

    lines.append("")
    if a["period_spent"]:
        tail = (f" ({_shipments(a['period_orders_count'])})"
                if a["period_orders_count"] else "")
        lines.append(f"📅 За {month} ушло: <b>{a['period_spent']:,.2f} ₽</b>{tail}")
    else:
        lines.append(f"📅 За {month} со счёта ещё ничего не ушло")

    if not a["topups"]:
        lines.append("\n⚠️ Пополнений не вносили — остаток считать не из чего. "
                     "Внесите, сколько занесли на счёт СДЭК.")
    elif a["balance"] < 0:
        lines.append("\n⚠️ Счёт в минусе: либо пополнение не внесли, "
                     "либо списаний оказалось больше, чем денег.")
    elif a["balance"] < config.CDEK_LOW_BALANCE:
        lines.append("\n⚠️ Денег на счету мало — пора пополнять.")

    lines.append("\n<i>Доставку оплачивает клиент, в дележ прибыли она "
                 "уже входит транзитом — этот счёт на расчёт не влияет.</i>")
    return "\n".join(lines)


async def _show_account(message: Message, edit: bool = False):
    text = await _account_text()
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML",
                                    reply_markup=_account_keyboard())
            return
        except Exception:
            pass
    await message.answer(text, parse_mode="HTML",
                         reply_markup=_account_keyboard())


async def _admin_only(callback: CallbackQuery) -> bool:
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return False
    return True


@router.message(Command("cdek"))
async def cmd_cdek(message: Message, state: FSMContext):
    """Счёт СДЭК: остаток, пополнения и траты."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await state.clear()
    await _show_account(message)


@router.callback_query(F.data == "cdek_show")
async def cb_cdek_show(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    await state.clear()
    await callback.answer()
    await _show_account(callback.message, edit=True)


@router.callback_query(F.data == "cdek_cancel")
async def cb_cdek_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_text("🚚 Отменил")
    except Exception:
        pass


@router.callback_query(F.data == "cdek_add")
async def cb_cdek_add(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    await state.set_state(CdekStates.waiting_topup)
    await callback.answer()
    await callback.message.answer(
        "➕ <b>Пополнение счёта СДЭК</b>\n\n"
        "Сколько занесли? Можно с комментарием:\n"
        "<code>10000 с карты Миши</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="cdek_cancel")
        ]]),
    )


@router.callback_query(F.data == "cdek_spend")
async def cb_cdek_spend(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    await state.set_state(CdekStates.waiting_writeoff)
    await callback.answer()
    await callback.message.answer(
        "➖ <b>Списание со счёта СДЭК</b>\n\n"
        "Так вносят то, чего бот не видит: накладную из личного кабинета, "
        "возврат, доплату за габариты. Наши накладные считаются "
        "автоматически — их вносить не нужно.\n\n"
        "Сколько списать? Можно с комментарием:\n"
        "<code>350 возврат посылки</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="cdek_cancel")
        ]]),
    )


async def _save_entry(message: Message, state: FSMContext, sign: int):
    amount, comment = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Не понял сумму. Напишите числом, например "
                             "<code>10000</code> или <code>10000 с карты Миши</code>.",
                             parse_mode="HTML")
        return

    u = message.from_user
    name = f"@{u.username}" if u.username else (u.first_name or f"id:{u.id}")
    entry_id = await db.add_cdek_entry(sign * amount, comment, u.id, name)
    await state.clear()

    a = await db.get_cdek_account()
    head = "✅ Записал пополнение" if sign > 0 else "✅ Записал списание"
    tail = f"\n💬 {comment}" if comment else ""
    await message.answer(
        f"{head}\n\n🚚 <b>{'+' if sign > 0 else '−'}{amount:,.2f} ₽</b>{tail}\n\n"
        f"💰 На счету СДЭК: <b>{_rub(a['balance'])} ₽</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить",
                                  callback_data=f"cdek_del:{entry_id}")],
            [InlineKeyboardButton(text="🚚 Счёт СДЭК", callback_data="cdek_show")],
        ]),
    )


@router.message(CdekStates.waiting_topup)
async def on_topup_amount(message: Message, state: FSMContext):
    await _save_entry(message, state, sign=1)


@router.message(CdekStates.waiting_writeoff)
async def on_writeoff_amount(message: Message, state: FSMContext):
    await _save_entry(message, state, sign=-1)


@router.callback_query(F.data.startswith("cdek_del:"))
async def cb_cdek_delete(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    entry_id = int(callback.data.split(":")[1])
    deleted = await db.delete_cdek_entry(entry_id)
    await callback.answer("Удалено 🗑" if deleted else "Эта запись уже удалена")
    try:
        await callback.message.edit_text(
            "🗑 <s>Запись по счёту СДЭК удалена</s>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚚 Счёт СДЭК", callback_data="cdek_show")
            ]]),
        )
    except Exception:
        pass


@router.callback_query(F.data == "cdek_log")
async def cb_cdek_log(callback: CallbackQuery):
    """История: ручные движения (их можно удалить) и траты по месяцам."""
    if not await _admin_only(callback):
        return
    entries = await db.get_cdek_entries(limit=15)
    by_month = await db.get_cdek_spent_by_month(limit=6)

    lines = ["📋 <b>История счёта СДЭК</b>"]
    if entries:
        lines.append("\n<b>Вносили руками:</b>")
        for e in entries:
            amount = float(e["amount"])
            sign = "➕" if amount > 0 else "➖"
            comment = f" — {e['comment']}" if e.get("comment") else ""
            who = f" · {e['user_name']}" if e.get("user_name") else ""
            lines.append(f"  {_msk(e['created_at'])} {sign} "
                         f"{abs(amount):,.2f} ₽{comment}{who}")
    if by_month:
        lines.append("\n<b>Ушло по накладным:</b>")
        for row in by_month:
            year, month = (row["month"] or "____-__").split("-")
            lines.append(f"  {month}.{year}: <b>{float(row['total']):,.2f} ₽</b> "
                         f"({_shipments(int(row['count']))})")
    if not entries and not by_month:
        lines.append("\nПока пусто.")

    rows = [[InlineKeyboardButton(
        text=f"🗑 {_msk(e['created_at'])} "
             f"{'+' if float(e['amount']) > 0 else '−'}{abs(float(e['amount'])):,.0f} ₽",
        callback_data=f"cdek_del:{e['id']}")] for e in entries[:10]]
    rows.append([InlineKeyboardButton(text="◀️ К счёту", callback_data="cdek_show")])

    await callback.answer()
    await callback.message.answer("\n".join(lines), parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
