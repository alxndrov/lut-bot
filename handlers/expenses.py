"""
Расходы в админском боте (malimadmins): пластик, упаковка, поп-фильтр и что
угодно ещё. Вычитаются из чистой прибыли до дележа — материалы покупаются
из общего котла, значит и оплачивают их оба.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton,
)

import config
import database as db

router = Router()
logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))

# Единая навигация между разделами «Финансов» — одна и та же строка кнопок
# внизу каждого экрана (выручка/расчёт/расходы/СДЭК), чтобы всё было
# доступно из одной команды /finance, а не из четырёх разных.
_NAV_SECTIONS = [
    ("show", "💳 Выручка", "fin_show"),
    ("debt", "🤝 Расчёт", "fin_debt"),
    ("exp", "🧾 Расходы", "exp_show"),
    ("cdek", "🚚 СДЭК", "cdek_show"),
]


def _nav_row(current: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text=label if key != current else f"· {label} ·",
                             callback_data=cb)
        for key, label, cb in _NAV_SECTIONS
    ]


class ExpenseStates(StatesGroup):
    waiting_amount = State()
    waiting_category_name = State()


def _month_bounds(now: datetime | None = None) -> tuple[str, str]:
    """Границы текущего месяца в датах 'ГГГГ-ММ-ДД'."""
    now = now or datetime.now(MSK)
    first = now.replace(day=1)
    last_day = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d")


def _msk(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return (datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                + timedelta(hours=3)).strftime("%d.%m")
    except Exception:
        return ""


def _parse_amount(text: str) -> tuple[float | None, str]:
    """'1500 катушка PLA' -> (1500.0, 'катушка PLA'). Запятая = точка.

    Пробел внутри числа принимаем только как разделитель тысяч («2 300»),
    иначе «1500 2 катушки» слиплось бы в 15002.
    """
    number = r"\d{1,3}(?:[  ]\d{3})+|\d+"
    # «руб» проверяем раньше одиночной «р», иначе от «руб» останется «уб»
    m = re.match(rf"\s*({number})(?:[.,](\d{{1,2}}))?\s*(?:₽|руб\w*|р(?![а-яё]))?\s*(.*)",
                 text or "", flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return None, ""
    try:
        amount = float(re.sub(r"[  ]", "", m.group(1)) + "." + (m.group(2) or "0"))
    except ValueError:
        return None, ""
    return (amount if amount > 0 else None), m.group(3).strip()


async def _categories_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=name, callback_data=f"exp_cat:{name}")]
            for name in await db.get_expense_categories()]
    rows.append([InlineKeyboardButton(text="➕ Новая статья",
                                      callback_data="exp_newcat")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="exp_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("expense"))
async def cmd_expense(message: Message, state: FSMContext):
    """Внести расход."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await state.clear()
    await message.answer("🧾 <b>Куда потратили?</b>", parse_mode="HTML",
                         reply_markup=await _categories_keyboard())


@router.callback_query(F.data == "exp_cancel")
async def cb_expense_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_text("🧾 Внесение расхода отменено")
    except Exception:
        pass


@router.callback_query(F.data == "exp_newcat")
async def cb_expense_new_category(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExpenseStates.waiting_category_name)
    await callback.answer()
    await callback.message.answer("Как назвать статью расходов?")


@router.message(ExpenseStates.waiting_category_name)
async def on_category_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or len(name) > 40:
        await message.answer("Название нужно покороче — до 40 символов.")
        return
    await db.add_expense_category(name)
    await state.update_data(category=name)
    await state.set_state(ExpenseStates.waiting_amount)
    await message.answer(
        f"Статья «{name}» добавлена.\n\n"
        "Сколько потратили? Напишите сумму, можно с комментарием:\n"
        "<code>1500 катушка PLA</code>",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("exp_cat:"))
async def cb_expense_category(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split(":", 1)[1]
    await state.update_data(category=category)
    await state.set_state(ExpenseStates.waiting_amount)
    await callback.answer()
    try:
        await callback.message.edit_text(f"🧾 <b>{category}</b>", parse_mode="HTML")
    except Exception:
        pass
    await callback.message.answer(
        "Сколько потратили? Напишите сумму, можно с комментарием:\n"
        "<code>1500 катушка PLA</code>",
        parse_mode="HTML",
    )


@router.message(ExpenseStates.waiting_amount)
async def on_expense_amount(message: Message, state: FSMContext):
    amount, comment = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Не понял сумму. Напишите числом, например <code>1500</code> "
                             "или <code>1500 катушка PLA</code>.", parse_mode="HTML")
        return

    data = await state.get_data()
    category = data.get("category") or "Прочее"
    u = message.from_user
    name = f"@{u.username}" if u.username else (u.first_name or f"id:{u.id}")
    expense_id = await db.add_expense(category, amount, comment, u.id, name)
    await state.clear()

    tail = f"\n💬 {comment}" if comment else ""
    await message.answer(
        f"✅ Записал расход\n\n🧾 <b>{category}</b> — <b>{amount:,.2f} ₽</b>{tail}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"exp_del:{expense_id}")
        ]]),
    )


@router.callback_query(F.data.startswith("exp_del:"))
async def cb_expense_delete(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    expense_id = int(callback.data.split(":")[1])
    deleted = await db.delete_expense(expense_id)
    await callback.answer("Удалено 🗑" if deleted else "Этот расход уже удалён")
    try:
        await callback.message.edit_text("🗑 <s>Расход удалён</s>", parse_mode="HTML")
    except Exception:
        pass


async def _expenses_text() -> str:
    date_from, date_to = _month_bounds()
    summary = await db.get_expenses_summary(date_from, date_to)
    items = await db.get_expenses(date_from, date_to)

    if not items:
        return "🧾 <b>Расходы</b>\n\nВ этом месяце расходов ещё не было."

    month = datetime.now(MSK).strftime("%m.%Y")
    lines = [f"🧾 <b>Расходы за {month}: {summary['total']:,.2f} ₽</b>", ""]
    for row in summary["by_category"]:
        lines.append(f"  • {row['category']}: <b>{row['total']:,.2f} ₽</b> "
                     f"({row['cnt']} шт.)")
    lines.append("")
    lines.append("<b>Последние:</b>")
    for e in items[:10]:
        tail = f" — {e['comment']}" if e.get("comment") else ""
        who = f" · {e['user_name']}" if e.get("user_name") else ""
        lines.append(f"  {_msk(e['spent_at'])} {e['category']} "
                     f"{e['amount']:,.2f} ₽{tail}{who}")
    return "\n".join(lines)


def _expenses_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Внести расход", callback_data="exp_add")],
        [InlineKeyboardButton(text="🗑 Удалить расход", callback_data="exp_dellist")],
        _nav_row("exp"),
    ])


async def _show_expenses(message: Message, edit: bool = False):
    text = await _expenses_text()
    markup = _expenses_keyboard()
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("expenses"))
async def cmd_expenses(message: Message):
    """Расходы за текущий месяц: сумма, по статьям и последние записи."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await _show_expenses(message)


@router.callback_query(F.data == "exp_show")
async def cb_expenses_show(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _show_expenses(callback.message, edit=True)


@router.callback_query(F.data == "exp_add")
async def cb_expense_add(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await callback.message.answer("🧾 <b>Куда потратили?</b>", parse_mode="HTML",
                                  reply_markup=await _categories_keyboard())


@router.callback_query(F.data == "exp_dellist")
async def cb_expense_delete_list(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    date_from, date_to = _month_bounds()
    items = (await db.get_expenses(date_from, date_to))[:10]
    if not items:
        await callback.answer("Удалять нечего")
        return
    rows = [[InlineKeyboardButton(
        text=f"{_msk(e['spent_at'])} {e['category']} {e['amount']:,.0f} ₽",
        callback_data=f"exp_del:{e['id']}")] for e in items]
    rows.append([InlineKeyboardButton(text="◀️ Отмена", callback_data="exp_cancel")])
    await callback.answer()
    await callback.message.answer("Что удалить?",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
