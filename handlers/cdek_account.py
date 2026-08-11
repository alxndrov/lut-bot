"""
Счёт СДЭК в админском боте (malimadmins).

СДЭК работает постоплатой: сначала возит посылки, потом выставляет счёт.
Деньги на него клиент уже отдал вместе с заказом, поэтому по каждой
отправке на СДЭК «откладывается» её стоимость по накладной (тариф +
страховка + НДС — она сохранена в purchases.delivery_cost). Когда счёт
приходит и мы его оплачиваем, отложенное уменьшается на сумму оплаты.

Это не /expense: доставку оплачивает клиент, и в дележ прибыли она уже
входит транзитом (services.payout.delivery_out). Вторым расходом её
вычитать нельзя — здесь только учёт расчётов со СДЭК.
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
    waiting_payment = State()


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def _shipments(n: int) -> str:
    return f"{n} {_plural(n, 'отправка', 'отправки', 'отправок')}"


def _bills(n: int) -> str:
    return f"{n} {_plural(n, 'счёт', 'счёта', 'счетов')}"


def _rub(value: float) -> str:
    """Сумма со знаком минус как в отчётах — «−», а не дефис."""
    return f"{value:,.2f}".replace("-", "−")


def _account_keyboard(due: float = 0.0) -> InlineKeyboardMarkup:
    """Кнопка оплаты сразу с суммой — обычно счёт закрывают целиком."""
    pay = "✅ Оплатил счёт" if due <= 0 else f"✅ Оплатил счёт — {_rub(due)} ₽"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=pay, callback_data="cdek_pay")],
        [InlineKeyboardButton(text="📋 История", callback_data="cdek_log")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="cdek_show")],
    ])


async def _account() -> dict:
    """Состояние по СДЭК вместе с суммами за текущий месяц."""
    return await db.get_cdek_account(*_month_bounds())


def _account_text(a: dict) -> str:
    """Главный экран: сколько отложено на СДЭК и из чего это сложилось."""
    month = datetime.now(MSK).strftime("%m.%Y")

    if not a["accrued_count"] and not a["paid_count"]:
        return (
            "🚚 <b>СДЭК</b>\n\n"
            "Отправок пока не было — и счёт выставлять не за что.\n\n"
            "Как только уйдёт первая посылка, здесь появится сумма, "
            "которую СДЭК выставит к оплате."
        )

    lines = ["🚚 <b>СДЭК</b>", ""]

    if a["due"] > 0:
        lines += [
            f"💰 Отложено на СДЭК: <b>{_rub(a['due'])} ₽</b>",
            "<i>набежало по накладным и ещё не оплачено — "
            "на эту сумму придёт счёт</i>",
        ]
    elif a["due"] < 0:
        lines += [
            f"💰 Переплата: <b>{_rub(-a['due'])} ₽</b>",
            "<i>оплатили вперёд — следующие отправки спишутся из неё</i>",
        ]
    else:
        lines.append("✅ <b>Всё оплачено</b> — неоплаченных накладных нет")

    lines += [
        "",
        f"📦 Всего по накладным: <b>{_rub(a['accrued'])} ₽</b>"
        + (f" · {_shipments(a['accrued_count'])}" if a["accrued_count"] else ""),
        f"✅ Оплачено счетов: <b>{_rub(a['paid'])} ₽</b>"
        + (f" · {_bills(a['paid_count'])}" if a["paid_count"] else ""),
        "",
    ]

    if a["period_accrued"]:
        lines.append(f"📅 За {month} набежало: <b>{_rub(a['period_accrued'])} ₽</b> "
                     f"({_shipments(a['period_accrued_count'])})")
    else:
        lines.append(f"📅 За {month} отправок ещё не было")
    if a["period_paid"]:
        lines.append(f"📅 За {month} оплачено: <b>{_rub(a['period_paid'])} ₽</b>")

    if a["accrued_legacy"]:
        lines.append("\n⚠️ По отправкам до августа 2026 счёт СДЭК не сохранялся — "
                     "они посчитаны по той же формуле, что и в отчётах, "
                     "то есть примерно.")
    if not a["paid_count"] and a["due"] > 0:
        lines.append("\n💡 Если счета за эти отправки уже оплачены — нажмите "
                     "«Оплатил счёт» и закройте остаток одной кнопкой.")

    lines.append("\n<i>Доставку оплачивает клиент, в дележ прибыли она "
                 "уже входит транзитом — эти деньги на расчёт не влияют.</i>")
    return "\n".join(lines)


async def _show_account(message: Message, edit: bool = False):
    a = await _account()
    text = _account_text(a)
    markup = _account_keyboard(a["due"])
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


async def _admin_only(callback: CallbackQuery) -> bool:
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return False
    return True


@router.message(Command("cdek"))
async def cmd_cdek(message: Message, state: FSMContext):
    """Сколько денег отложено на СДЭК и что уже оплачено."""
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


@router.callback_query(F.data == "cdek_pay")
async def cb_cdek_pay(callback: CallbackQuery, state: FSMContext):
    """Оплата счёта: весь остаток одной кнопкой или своя сумма."""
    if not await _admin_only(callback):
        return
    a = await db.get_cdek_account()
    await state.set_state(CdekStates.waiting_payment)
    await callback.answer()

    rows = []
    if a["due"] > 0:
        rows.append([InlineKeyboardButton(
            text=f"✅ Весь остаток — {_rub(a['due'])} ₽",
            callback_data="cdek_pay_all")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cdek_cancel")])

    hint = (f"Сейчас не оплачено <b>{_rub(a['due'])} ₽</b>.\n\n"
            if a["due"] > 0 else "")
    await callback.message.answer(
        f"✅ <b>Оплата счёта СДЭК</b>\n\n{hint}"
        "Нажмите кнопку, если оплатили весь остаток, "
        "или напишите сумму счёта — можно с комментарием:\n"
        "<code>3500 счёт за июль</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _save_payment(message: Message, amount: float, comment: str,
                        user, state: FSMContext):
    name = f"@{user.username}" if user.username else (user.first_name or f"id:{user.id}")
    payment_id = await db.add_cdek_payment(amount, comment, user.id, name)
    await state.clear()

    a = await db.get_cdek_account()
    if a["due"] > 0:
        rest = f"💰 Осталось отложено: <b>{_rub(a['due'])} ₽</b>"
    elif a["due"] < 0:
        rest = f"💰 Переплата: <b>{_rub(-a['due'])} ₽</b>"
    else:
        rest = "✅ Со СДЭК в расчёте — неоплаченных накладных нет"

    tail = f"\n💬 {comment}" if comment else ""
    await message.answer(
        f"✅ Записал оплату счёта\n\n🚚 <b>{_rub(amount)} ₽</b>{tail}\n\n{rest}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить",
                                  callback_data=f"cdek_del:{payment_id}")],
            [InlineKeyboardButton(text="🚚 СДЭК", callback_data="cdek_show")],
        ]),
    )


@router.callback_query(F.data == "cdek_pay_all")
async def cb_cdek_pay_all(callback: CallbackQuery, state: FSMContext):
    """Оплатить весь остаток. Сумму берём заново — она могла измениться."""
    if not await _admin_only(callback):
        return
    a = await db.get_cdek_account()
    if a["due"] <= 0:
        await state.clear()
        await callback.answer("Оплачивать нечего — всё уже закрыто", show_alert=True)
        return
    await callback.answer()
    await _save_payment(callback.message, a["due"], "весь остаток",
                        callback.from_user, state)


@router.message(CdekStates.waiting_payment)
async def on_payment_amount(message: Message, state: FSMContext):
    amount, comment = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Не понял сумму. Напишите числом, например "
                             "<code>3500</code> или <code>3500 счёт за июль</code>.",
                             parse_mode="HTML")
        return
    await _save_payment(message, amount, comment, message.from_user, state)


@router.callback_query(F.data.startswith("cdek_del:"))
async def cb_cdek_delete(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    payment_id = int(callback.data.split(":")[1])
    deleted = await db.delete_cdek_payment(payment_id)
    await callback.answer("Удалено 🗑" if deleted else "Эта оплата уже удалена")
    try:
        await callback.message.edit_text(
            "🗑 <s>Оплата счёта удалена</s>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚚 СДЭК", callback_data="cdek_show")
            ]]),
        )
    except Exception:
        pass


@router.callback_query(F.data == "cdek_log")
async def cb_cdek_log(callback: CallbackQuery):
    """История: оплаченные счета (их можно удалить) и накладные по месяцам."""
    if not await _admin_only(callback):
        return
    payments = await db.get_cdek_payments(limit=15)
    by_month = await db.get_cdek_accrued_by_month(limit=6)

    lines = ["📋 <b>История расчётов со СДЭК</b>"]
    if payments:
        lines.append("\n<b>Оплаченные счета:</b>")
        for p in payments:
            comment = f" — {p['comment']}" if p.get("comment") else ""
            who = f" · {p['user_name']}" if p.get("user_name") else ""
            lines.append(f"  {_msk(p['paid_at'])} ✅ "
                         f"{_rub(float(p['amount']))} ₽{comment}{who}")
    else:
        lines.append("\nСчета ещё не оплачивали.")
    if by_month:
        lines.append("\n<b>Набежало по накладным:</b>")
        for row in by_month:
            year, month = (row["month"] or "____-__").split("-")
            about = " ≈" if row["legacy"] else ""
            lines.append(f"  {month}.{year}:{about} <b>{_rub(float(row['total']))} ₽</b> "
                         f"({_shipments(int(row['count']))})")
        if any(r["legacy"] for r in by_month):
            lines.append("  <i>≈ — счёт СДЭК за те месяцы не сохранялся, "
                         "сумма расчётная</i>")

    rows = [[InlineKeyboardButton(
        text=f"🗑 {_msk(p['paid_at'])} {float(p['amount']):,.0f} ₽",
        callback_data=f"cdek_del:{p['id']}")] for p in payments[:10]]
    rows.append([InlineKeyboardButton(text="◀️ К счёту", callback_data="cdek_show")])

    await callback.answer()
    await callback.message.answer("\n".join(lines), parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
