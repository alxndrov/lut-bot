"""
Финансы в админском боте (malimadmins): выручка по Prodamus и взаиморасчёт
с Даней.

Раньше жило в основном боте (malimabibot) под кнопкой «Финансы» в панели
/admin — доступ туда и так был только у ADMIN_IDS, но крутилось это в
клиентском боте вперемешку с каталогом. Перенесено сюда, чтобы все
управленческие команды (заказы, расходы, СДЭК, финансы) были в одном месте.

История расчётов не переезжает — это та же таблица settlements в той же
БД, что и раньше: какой бот её читает и пишет, не имеет значения.
"""
import asyncio
import logging
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
from handlers.expenses import MSK, _nav_row, _msk, _parse_amount
from services.daily_report import fetch_payments_summary

router = Router()
logger = logging.getLogger(__name__)

_SHARES = [("Миша", 0.80), ("Даня", 0.20)]
_NPD_RATE = config.NPD_PERCENT / 100


class CashStates(StatesGroup):
    waiting_npd = State()
    waiting_payout = State()


def _fmt_period_with_shares(label: str, s: dict) -> str:
    if s["count"] == 0:
        return f"{label}\n  Продаж не было\n"
    net_after_tax = s["net"] - s["net"] * _NPD_RATE
    shares = "  ".join(
        f"{name}: <b>{net_after_tax * pct:,.2f} ₽</b>"
        for name, pct in _SHARES
    )
    return (
        f"{label}\n"
        f"  Продаж: <b>{s['count']}</b>  |  Брутто: <b>{s['gross']:,.2f} ₽</b>\n"
        f"  Комиссия ({s['fee_pct']}%): −{s['fee']:,.2f} ₽\n"
        f"  НПД {config.NPD_PERCENT:g}%: −{s['net'] * _NPD_RATE:,.2f} ₽\n"
        f"  Чистыми: <b>{net_after_tax:,.2f} ₽</b>\n"
        f"  {shares}\n"
    )


def _fmt_period(label: str, s: dict) -> str:
    if s["count"] == 0:
        return f"{label}\n  Продаж не было\n"
    return (
        f"{label}\n"
        f"  Продаж: <b>{s['count']}</b>  |  Брутто: <b>{s['gross']:,.2f} ₽</b>\n"
        f"  Комиссия ({s['fee_pct']}%): −{s['fee']:,.2f} ₽  |  НДС: {s['vat']:,.2f} ₽\n"
        f"  Чистыми: <b>{s['net']:,.2f} ₽</b>\n"
    )


def _fmt_payout_block(s: dict, label: str) -> str:
    if not s or s["count"] == 0:
        return ""
    tax = s["gross"] * _NPD_RATE
    net_after_tax = s["net"] - tax
    shares = "\n".join(
        f"  {name} ({int(pct*100)}%): <b>{net_after_tax * pct:,.2f} ₽</b>"
        for name, pct in _SHARES
    )
    return (
        f"{'─' * 30}\n"
        f"💰 <b>К получению</b> ({label}): <b>{s['net']:,.2f} ₽</b>\n"
        f"📋 НПД {config.NPD_PERCENT:g}% (самозанятость): −{tax:,.2f} ₽\n"
        f"💵 После налога: <b>{net_after_tax:,.2f} ₽</b>\n"
        f"{shares}\n"
        f"{'─' * 30}"
    )


def _fmt_debt_screen(s: dict | None, last_settlement: dict | None) -> str:
    """s — раскладка из services.payout.split за период с прошлого расчёта."""
    if last_settlement:
        period_text = f"с <b>{last_settlement['settled_at'][:10]}</b>"
    else:
        period_text = "за <b>всё время</b>"

    if not s or s["count"] == 0:
        return (
            f"🤝 <b>Взаиморасчёт</b>\n\n"
            f"Продаж {period_text} не было.\n"
            f"К выплате: <b>0 ₽</b>"
        )

    lines = [
        f"🤝 <b>Взаиморасчёт</b> ({period_text})",
        "─" * 30,
        f"Продаж: <b>{s['count']}</b>  |  Принято: <b>{s['gross']:,.2f} ₽</b>",
    ]
    if s["physical"]:
        lines.append(f"  товар: {s['physical']:,.2f} ₽")
    if s["digital"]:
        lines.append(f"  цифра: {s['digital']:,.2f} ₽")
    if s["delivery"]:
        lines.append(f"  доставка: {s['delivery']:,.2f} ₽")
    lines.append(f"Комиссия {s['fee_pct']}%: −{s['fee']:,.2f} ₽")
    lines.append(f"НПД {config.NPD_PERCENT:g}%: −{s['npd']:,.2f} ₽")
    if s["delivery"]:
        lines.append(f"Доставка в СДЭК: −{s['delivery_out']:,.2f} ₽")
    if s["expenses"]:
        lines.append(f"Расходы: −{s['expenses']:,.2f} ₽")
    lines += [
        "─" * 30,
        f"Чистыми: <b>{s['net']:,.2f} ₽</b>",
        "",
        f"👤 {config.OWNER_NAME}: <b>{s['owner']:,.2f} ₽</b>  ← к выплате",
        f"👤 {config.PARTNER_NAME}: <b>{s['partner']:,.2f} ₽</b>",
        f"    <i>{config.PARTNER_GOODS_PERCENT:g}% с товара {s['partner_goods']:,.2f}"
        f" · печать {s['printed']} × {config.PARTNER_PRINT_FEE} ₽ = {s['partner_print']:,.2f}"
        f" · цифра {config.PARTNER_DIGITAL_PERCENT:g}% = {s['partner_digital']:,.2f}</i>",
    ]
    return "\n".join(lines)


def _finance_keyboard(has_unsettled: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_unsettled:
        rows.append([InlineKeyboardButton(text="✅ Мы в расчёте", callback_data="fin_settle")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="fin_show")])
    rows += _nav_row("show")
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _debt_keyboard(has_debt: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_debt:
        rows.append([InlineKeyboardButton(text="✅ Расчёт произведён", callback_data="fin_settle_debt")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="fin_debt")])
    rows += _nav_row("debt")
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _admin_only(callback: CallbackQuery) -> bool:
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return False
    return True


async def _finance_text() -> tuple[str, bool]:
    """Текст экрана «Финансы» и флаг — есть ли продажи, не сведённые в расчёт."""
    now_utc = datetime.now(timezone.utc)
    today_from = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    today_to = now_utc

    def iso(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    last_settlement = await db.get_last_settlement()

    tasks = [
        fetch_payments_summary(iso(today_from), iso(today_to)),
        fetch_payments_summary(iso(now_utc - timedelta(days=7)), iso(today_to)),
        fetch_payments_summary(iso(now_utc - timedelta(days=30)), iso(today_to)),
        fetch_payments_summary("2020-01-01T00:00:00.000Z", iso(today_to)),
    ]
    if last_settlement:
        tasks.append(
            fetch_payments_summary(last_settlement["settled_at"], iso(today_to))
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    def safe(r):
        if isinstance(r, Exception):
            logger.error(f"finance fetch error: {r}")
            return None
        return r

    today_s, week_s, month_s, all_s = [safe(r) for r in results[:4]]
    since_s = safe(results[4]) if last_settlement else None

    now_msk = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    lines = [f"💳 <b>Финансы (Prodamus)</b>\n<i>обновлено {now_msk} МСК</i>\n"]

    if today_s:
        lines.append(_fmt_period("📅 <b>За сегодня</b>", today_s))
    if week_s:
        lines.append(_fmt_period("📅 <b>За 7 дней</b>", week_s))
    if month_s:
        lines.append(_fmt_period("📅 <b>За 30 дней</b>", month_s))
    if all_s:
        lines.append(_fmt_period_with_shares("📊 <b>За всё время</b>", all_s))

    if last_settlement:
        settled_at = last_settlement["settled_at"][:10]
        if since_s:
            lines.append(_fmt_period(f"🤝 <b>С расчёта {settled_at}</b>", since_s))
        has_unsettled = bool(since_s and since_s["count"] > 0)
        payout_s, payout_label = since_s, f"с {settled_at}"
    else:
        lines.append("🤝 <b>Расчётов ещё не было</b>\n")
        has_unsettled = bool(all_s and all_s["count"] > 0)
        payout_s, payout_label = all_s, "всё время"

    payout_block = _fmt_payout_block(payout_s, payout_label)
    if payout_block:
        lines.append(payout_block)

    if any(isinstance(r, Exception) for r in results):
        lines.append("\n⚠️ Часть данных не загрузилась — проверь логи")

    return "\n".join(lines), has_unsettled


async def _show_finance(message: Message):
    text, has_unsettled = await _finance_text()
    try:
        await message.edit_text(text, parse_mode="HTML",
                                reply_markup=_finance_keyboard(has_unsettled))
    except Exception:
        await message.answer(text, parse_mode="HTML",
                             reply_markup=_finance_keyboard(has_unsettled))


async def _show_debt(message: Message):
    last_settlement = await db.get_last_settlement()
    dt_from = last_settlement["settled_at"] if last_settlement else "2020-01-01T00:00:00.000Z"
    now_utc = datetime.now(timezone.utc)
    dt_to = now_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")

    try:
        from services import payout
        s = await payout.split(dt_from[:19].replace("T", " "), dt_to[:19].replace("T", " "))
    except Exception as e:
        logger.error(f"debt fetch error: {e}")
        s = None

    text = _fmt_debt_screen(s, last_settlement)
    has_debt = bool(s and s["count"] > 0)
    try:
        await message.edit_text(text, parse_mode="HTML",
                                reply_markup=_debt_keyboard(has_debt))
    except Exception:
        await message.answer(text, parse_mode="HTML",
                             reply_markup=_debt_keyboard(has_debt))


@router.message(Command("finance"))
async def cmd_finance(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    m = await message.answer("⏳ Загружаю данные…")
    await _show_finance(m)


@router.callback_query(F.data == "fin_show")
async def cb_fin_show(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    await callback.answer()
    await callback.message.edit_text("⏳ Загружаю данные…")
    await _show_finance(callback.message)


@router.callback_query(F.data == "fin_settle")
async def cb_fin_settle(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    await callback.answer()
    await callback.message.edit_text("⏳ Фиксирую расчёт…")

    now_utc = datetime.now(timezone.utc)
    last = await db.get_last_settlement()
    dt_from = last["settled_at"] if last else "2020-01-01T00:00:00.000Z"

    try:
        s = await fetch_payments_summary(dt_from, now_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z"))
    except Exception as e:
        logger.error(f"settle fetch error: {e}")
        await callback.message.edit_text(
            "❌ Не удалось получить данные из БД.", reply_markup=_finance_keyboard(False)
        )
        return

    await db.add_settlement(gross=s["gross"], fee=s["fee"], net=s["net"], count=s["count"])

    tax = s["gross"] * _NPD_RATE
    net_after_tax = s["net"] - tax
    now_msk = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    text = (
        f"✅ <b>Расчёт зафиксирован</b> — {now_msk} МСК\n\n"
        f"Продаж с прошлого расчёта: <b>{s['count']}</b>\n"
        f"Брутто: <b>{s['gross']:,.2f} ₽</b>\n"
        f"Комиссия: −{s['fee']:,.2f} ₽\n"
        f"Получено: <b>{s['net']:,.2f} ₽</b>\n"
        f"НПД {config.NPD_PERCENT:g}%: −{tax:,.2f} ₽\n"
        f"После налога: <b>{net_after_tax:,.2f} ₽</b>"
    )
    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=_finance_keyboard(False))


@router.message(Command("debt"))
async def cmd_debt(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await _show_debt(message)


@router.callback_query(F.data == "fin_debt")
async def cb_fin_debt(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    await callback.answer()
    await _show_debt(callback.message)


@router.callback_query(F.data == "fin_settle_debt")
async def cb_fin_settle_debt(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    await callback.answer()
    await callback.message.edit_text("⏳ Фиксирую расчёт…")

    last = await db.get_last_settlement()
    dt_from = last["settled_at"] if last else "2020-01-01T00:00:00.000Z"
    now_utc = datetime.now(timezone.utc)
    dt_to = now_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")

    try:
        from services import payout
        s = await payout.split(dt_from[:19].replace("T", " "), dt_to[:19].replace("T", " "))
    except Exception as e:
        logger.error(f"settle_debt error: {e}")
        await callback.message.edit_text(
            "❌ Не удалось получить данные.", reply_markup=_debt_keyboard(False)
        )
        return

    await db.add_settlement(gross=s["gross"], fee=s["fee"], net=s["net"], count=s["count"])

    now_msk = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    text = (
        f"✅ <b>Расчёт зафиксирован</b> — {now_msk} МСК\n\n"
        f"Продаж: <b>{s['count']}</b>  |  Принято: <b>{s['gross']:,.2f} ₽</b>\n"
        f"Чистыми: <b>{s['net']:,.2f} ₽</b>\n"
        f"{config.OWNER_NAME}: <b>{s['owner']:,.2f} ₽</b>\n"
        f"{config.PARTNER_NAME}: <b>{s['partner']:,.2f} ₽</b> "
        f"(печать {s['printed']} шт.)"
    )
    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=_debt_keyboard(False))


# ── Кассовый остаток ──────────────────────────────────────────────────
# «Взаиморасчёт» выше — начисление: сколько ДОЛЖНО уйти на налог, СДЭК и
# доли партнёров. Здесь — сколько реально ушло (по вашим отметкам) и
# сколько поэтому реально должно быть на счету прямо сейчас. Эти два
# экрана специально разные: один для честного дележа прибыли, другой —
# для сверки с банком.

def _cash_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💸 Заплатил НПД", callback_data="npd_pay")],
        [InlineKeyboardButton(text=f"👤 Выплатил {config.OWNER_NAME}", callback_data="payout:owner"),
         InlineKeyboardButton(text=f"👤 Выплатил {config.PARTNER_NAME}", callback_data="payout:partner")],
        [InlineKeyboardButton(text="📋 История", callback_data="cash_log")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="fin_cash")],
    ]
    rows += _nav_row("cash")
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _cash_text() -> str:
    last_settlement = await db.get_last_settlement()
    now_utc = datetime.now(timezone.utc)
    dt_from_iso = last_settlement["settled_at"] if last_settlement else "2020-01-01T00:00:00.000Z"
    dt_to_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    dt_from, dt_to = dt_from_iso[:19].replace("T", " "), dt_to_iso[:19].replace("T", " ")
    period_text = f"с {last_settlement['settled_at'][:10]}" if last_settlement else "за всё время"

    try:
        from services import payout as payout_svc
        s = await payout_svc.split(dt_from, dt_to)
    except Exception as e:
        logger.error(f"cash fetch error: {e}")
        return "❌ Не удалось посчитать кассу — проверь логи"

    if s["count"] == 0:
        return f"💰 <b>Кассовый остаток</b> ({period_text})\n\nПродаж не было."

    cdek_paid = await db.get_cdek_payments_summary(dt_from, dt_to)
    npd_paid = await db.get_npd_payments_summary(dt_from, dt_to)
    payouts = await db.get_payouts_summary(dt_from, dt_to)

    cash = (s["gross"] - s["fee"] - s["expenses"]
            - float(cdek_paid["total"]) - float(npd_paid["total"]) - payouts["total"])

    # Эвристика, не факт: Prodamus переводит деньги на счёт не мгновенно,
    # а с задержкой в 1-2 дня — заказы за это время бот уже посчитал
    # в «Принято», а по факту они могут быть ещё не на счету.
    recent_from_dt = max(datetime.strptime(dt_from, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
                         now_utc - timedelta(days=2))
    recent = await db.get_period_revenue(recent_from_dt.strftime("%Y-%m-%d %H:%M:%S"), dt_to)
    recent_gross = float(recent["physical"]) + float(recent["digital"]) + float(recent["delivery"])

    lines = [
        f"💰 <b>Кассовый остаток</b> ({period_text})",
        "<i>реальные деньги на счету, не доля прибыли — для дележа см. «Взаиморасчёт»</i>",
        "─" * 30,
        f"Принято: <b>{s['gross']:,.2f} ₽</b> ({s['count']} продаж)",
        f"Комиссия Prodamus (оценка {s['fee_pct']}%): −{s['fee']:,.2f} ₽",
        f"Расходы: −{s['expenses']:,.2f} ₽",
        f"Оплачено СДЭК по факту: −{float(cdek_paid['total']):,.2f} ₽"
        + (f" ({int(cdek_paid['count'])} плат.)" if cdek_paid["count"] else ""),
        f"Оплачено НПД по факту: −{float(npd_paid['total']):,.2f} ₽"
        + (f" ({int(npd_paid['count'])} плат.)" if npd_paid["count"] else ""),
    ]
    if payouts["total"]:
        by = " · ".join(f"{name} {amt:,.2f} ₽" for name, amt in payouts["by_recipient"].items())
        lines.append(f"Выплачено партнёрам: −{payouts['total']:,.2f} ₽ ({by})")
    lines += [
        "─" * 30,
        f"💰 <b>Ожидаемый остаток на счету: {cash:,.2f} ₽</b>",
        "\nСверьте с выпиской банка — если не сходится, чего-то не хватает в записях выше.",
    ]
    if recent_gross:
        lines.append(f"\n<i>⏳ ≈{recent_gross:,.2f} ₽ из «Принято» — заказы последних 2 суток, "
                     f"Prodamus мог их ещё не перевести на счёт</i>")
    not_paid_cdek = s["delivery_out"] - float(cdek_paid["total"])
    not_paid_npd = s["npd"] - float(npd_paid["total"])
    if not_paid_cdek > 0.5 or not_paid_npd > 0.5:
        lines.append(
            f"\n<i>Начислено, но не оплачено по факту: СДЭК {max(not_paid_cdek, 0):,.2f} ₽"
            f" · НПД {max(not_paid_npd, 0):,.2f} ₽ — эти деньги должны остаться "
            f"на счету до прихода счёта СДЭК и срока уплаты налога.</i>"
        )
    return "\n".join(lines)


async def _show_cash(message: Message):
    text = await _cash_text()
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=_cash_keyboard())
    except Exception:
        await message.answer(text, parse_mode="HTML", reply_markup=_cash_keyboard())


@router.callback_query(F.data == "fin_cash")
async def cb_fin_cash(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    await state.clear()
    await callback.answer()
    await _show_cash(callback.message)


@router.callback_query(F.data == "npd_pay")
async def cb_npd_pay(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    await state.set_state(CashStates.waiting_npd)
    await callback.answer()
    await callback.message.answer(
        "💸 <b>Сколько заплатили НПД?</b>\n\nНапишите сумму, можно с комментарием:\n"
        "<code>1500 за июль</code>",
        parse_mode="HTML",
    )


@router.message(CashStates.waiting_npd)
async def on_npd_amount(message: Message, state: FSMContext):
    amount, comment = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Не понял сумму. Напишите числом, например <code>1500</code>.",
                             parse_mode="HTML")
        return
    u = message.from_user
    name = f"@{u.username}" if u.username else (u.first_name or f"id:{u.id}")
    payment_id = await db.add_npd_payment(amount, comment, u.id, name)
    await state.clear()

    tail = f"\n💬 {comment}" if comment else ""
    await message.answer(
        f"✅ Записал оплату НПД\n\n💸 <b>{amount:,.2f} ₽</b>{tail}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"npd_del:{payment_id}")],
            [InlineKeyboardButton(text="💰 Касса", callback_data="fin_cash")],
        ]),
    )


@router.callback_query(F.data.startswith("npd_del:"))
async def cb_npd_delete(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    payment_id = int(callback.data.split(":")[1])
    deleted = await db.delete_npd_payment(payment_id)
    await callback.answer("Удалено 🗑" if deleted else "Эта запись уже удалена")
    try:
        await callback.message.edit_text(
            "🗑 <s>Оплата НПД удалена</s>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="💰 Касса", callback_data="fin_cash")
            ]]),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("payout:"))
async def cb_payout_start(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    which = callback.data.split(":", 1)[1]
    recipient = config.OWNER_NAME if which == "owner" else config.PARTNER_NAME
    await state.set_state(CashStates.waiting_payout)
    await state.update_data(recipient=recipient)
    await callback.answer()
    await callback.message.answer(
        f"👤 <b>Сколько выплатили {recipient}?</b>\n\nНапишите сумму, можно с комментарием:\n"
        "<code>15000 за июль</code>",
        parse_mode="HTML",
    )


@router.message(CashStates.waiting_payout)
async def on_payout_amount(message: Message, state: FSMContext):
    amount, comment = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Не понял сумму. Напишите числом, например <code>15000</code>.",
                             parse_mode="HTML")
        return
    data = await state.get_data()
    recipient = data.get("recipient") or config.OWNER_NAME
    u = message.from_user
    name = f"@{u.username}" if u.username else (u.first_name or f"id:{u.id}")
    payout_id = await db.add_payout(recipient, amount, comment, u.id, name)
    await state.clear()

    tail = f"\n💬 {comment}" if comment else ""
    await message.answer(
        f"✅ Записал выплату\n\n👤 <b>{recipient}: {amount:,.2f} ₽</b>{tail}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"payout_del:{payout_id}")],
            [InlineKeyboardButton(text="💰 Касса", callback_data="fin_cash")],
        ]),
    )


@router.callback_query(F.data.startswith("payout_del:"))
async def cb_payout_delete(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    payout_id = int(callback.data.split(":")[1])
    deleted = await db.delete_payout(payout_id)
    await callback.answer("Удалено 🗑" if deleted else "Эта запись уже удалена")
    try:
        await callback.message.edit_text(
            "🗑 <s>Выплата удалена</s>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="💰 Касса", callback_data="fin_cash")
            ]]),
        )
    except Exception:
        pass


async def _cash_log_render() -> tuple[str, InlineKeyboardMarkup]:
    npd = await db.get_npd_payments(limit=10)
    payouts = await db.get_payouts(limit=10)
    revenue_by_month = await db.get_revenue_by_month(limit=6)
    npd_paid_by_month = {r["month"]: r for r in await db.get_npd_payments_by_month(limit=12)}

    lines = ["📋 <b>История кассы</b>"]
    unpaid_months = []  # (month_key, mm.yyyy) — для кнопок «Детали»/«Оплачен» ниже

    if revenue_by_month:
        lines.append("\n<b>НПД по месяцам:</b>")
        for row in revenue_by_month:
            month_key = row["month"] or "____-__"
            year, month = month_key.split("-")
            accrued = float(row["gross"]) * _NPD_RATE
            paid_row = npd_paid_by_month.get(month_key)
            paid = float(paid_row["total"]) if paid_row else 0.0
            mark = "✅" if paid >= accrued - 0.5 else ("◻️" if paid == 0 else "⏳")
            lines.append(f"  {mark} {month}.{year}: начислено <b>{accrued:,.2f} ₽</b>, "
                         f"оплачено {paid:,.2f} ₽")
            if mark != "✅":
                unpaid_months.append((month_key, f"{month}.{year}"))

    if npd:
        lines.append("\n<b>НПД:</b>")
        for p in npd:
            comment = f" — {p['comment']}" if p.get("comment") else ""
            who = f" · {p['user_name']}" if p.get("user_name") else ""
            lines.append(f"  {_msk(p['paid_at'])} 💸 {float(p['amount']):,.2f} ₽{comment}{who}")
    if payouts:
        lines.append("\n<b>Выплаты партнёрам:</b>")
        for p in payouts:
            comment = f" — {p['comment']}" if p.get("comment") else ""
            lines.append(f"  {_msk(p['paid_at'])} 👤 {p['recipient']}: "
                         f"{float(p['amount']):,.2f} ₽{comment}")
    if not npd and not payouts:
        lines.append("\nПока пусто.")

    rows = [[InlineKeyboardButton(text=f"🔍 Детали {label}", callback_data=f"npd_detail:{key}"),
             InlineKeyboardButton(text=f"✅ {label} оплачен", callback_data=f"npd_markpaid:{key}")]
            for key, label in unpaid_months]
    rows += [[InlineKeyboardButton(
        text=f"🗑 НПД {_msk(p['paid_at'])} {float(p['amount']):,.0f} ₽",
        callback_data=f"npd_del:{p['id']}")] for p in npd[:5]]
    rows += [[InlineKeyboardButton(
        text=f"🗑 {p['recipient']} {_msk(p['paid_at'])} {float(p['amount']):,.0f} ₽",
        callback_data=f"payout_del:{p['id']}")] for p in payouts[:5]]
    rows.append([InlineKeyboardButton(text="◀️ К кассе", callback_data="fin_cash")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "cash_log")
async def cb_cash_log(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    await callback.answer()
    text, markup = await _cash_log_render()
    await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)


def _msk_dt(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return (datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                + timedelta(hours=3)).strftime("%d.%m %H:%M")
    except Exception:
        return ""


@router.callback_query(F.data.startswith("npd_detail:"))
async def cb_npd_detail(callback: CallbackQuery):
    """Покупки за месяц по отдельности — сверить с чеками в «Мой налог»,
    когда начисленное в боте не сходится с приложением."""
    if not await _admin_only(callback):
        return
    month = callback.data.split(":", 1)[1]
    purchases = await db.get_purchases_by_month(month)
    year, mm = month.split("-", 1) if "-" in month else ("", month)
    total = sum(float(p["amount"]) for p in purchases)
    accrued = total * _NPD_RATE

    lines = [
        f"🔍 <b>Покупки за {mm}.{year}</b> — {len(purchases)} шт. на <b>{total:,.2f} ₽</b>",
        f"НПД с них (оценка бота): <b>{accrued:,.2f} ₽</b>",
        "<i>время в МСК — сверяйте с датой чека в «Мой налог», не с датой в интерфейсе Prodamus</i>",
        "",
    ]
    for p in purchases:
        buyer = (f"@{p['username']}" if p.get("username")
                else (p.get("first_name") or f"id:{p.get('user_id')}"))
        name = p.get("product_name") or "—"
        lines.append(f"  {_msk_dt(p['created_at'])} · {float(p['amount']):,.2f} ₽ · {name} · {buyer}")
    if not purchases:
        lines.append("  Пусто.")

    await callback.answer()
    await callback.message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ К истории", callback_data="cash_log")
        ]]),
    )


@router.callback_query(F.data.startswith("npd_markpaid:"))
async def cb_npd_markpaid(callback: CallbackQuery):
    """Одной кнопкой: записать оплату НПД за месяц на всю начисленную сумму —
    когда налог за этот месяц точно уже закрыт и сверять по копейке не нужно."""
    if not await _admin_only(callback):
        return
    month = callback.data.split(":", 1)[1]
    revenue = await db.get_revenue_by_month(limit=24)
    row = next((r for r in revenue if r["month"] == month), None)
    if not row:
        await callback.answer("За этот месяц продаж не найдено.", show_alert=True)
        return
    accrued = float(row["gross"]) * _NPD_RATE
    if accrued <= 0:
        await callback.answer("Начислять нечего.", show_alert=True)
        return

    u = callback.from_user
    name = f"@{u.username}" if u.username else (u.first_name or f"id:{u.id}")
    year, mm = month.split("-", 1) if "-" in month else ("", month)
    await db.add_npd_payment(accrued, f"начислено за {mm}.{year}, отмечено оплаченным",
                             u.id, name, paid_at=f"{month}-28 12:00:00")
    await callback.answer(f"Отмечено: {mm}.{year} оплачен ✅")
    text, markup = await _cash_log_render()
    await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
