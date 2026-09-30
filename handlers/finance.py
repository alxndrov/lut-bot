"""Финансовое меню: отдельные экраны выручки, кассы, налогов и расчётов."""
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
from handlers.expenses import MSK, _msk, _parse_amount
from services import payout
from services.daily_report import fetch_payments_summary
from services import tax_account

router = Router()
logger = logging.getLogger(__name__)

_NPD_RATE = config.NPD_PERCENT / 100


class CashStates(StatesGroup):
    waiting_payout = State()
    waiting_npd = State()


def _dative(name: str) -> str:
    """Грубое склонение в дательный падеж для обычных русских имён вида
    Миша/Даня/Саша (то, что реально бывает в OWNER_NAME/PARTNER_NAME):
    конечную «а»/«я» меняем на «е». Имя другой формы возвращаем как есть —
    лучше без падежа, чем с неправильным."""
    if name and name[-1] in "ая":
        return name[:-1] + "е"
    return name


def _fmt_period(label: str, s: dict) -> str:
    if s["count"] == 0:
        return f"{label}\n  Продаж не было\n"
    return (
        f"{label}\n"
        f"  Продаж: <b>{s['count']}</b>  |  Брутто: <b>{s['gross']:,.2f} ₽</b>\n"
        f"  Комиссия ({s['fee_pct']}%): −{s['fee']:,.2f} ₽  |  НДС: {s['vat']:,.2f} ₽\n"
        f"  Чистыми: <b>{s['net']:,.2f} ₽</b>\n"
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
    lines.append(f"НПД с учётом исправлений: −{s['npd']:,.2f} ₽")
    if s["delivery"]:
        lines.append(f"Доставка в СДЭК: −{s['delivery_out']:,.2f} ₽")
    if s["expenses"]:
        lines.append(f"Расходы: −{s['expenses']:,.2f} ₽")
    lines += [
        "─" * 30,
        f"Чистыми: <b>{s['net']:,.2f} ₽</b>",
        "",
    ]
    # Если часть доли уже отдали внутри периода — показываем остаток, иначе
    # цифра выглядит как долг, которого на самом деле уже нет
    for name, share, reimb, paid, left in (
        (config.OWNER_NAME, s["owner"], s.get("reimb_owner", 0), s.get("paid_owner", 0),
         s.get("owner_left", s["owner"])),
        (config.PARTNER_NAME, s["partner"], s.get("reimb_partner", 0), s.get("paid_partner", 0),
         s.get("partner_left", s["partner"])),
    ):
        if paid or reimb:
            lines.append(f"👤 {name}: <b>{left:,.2f} ₽</b>  ← осталось выплатить")
            detail = f"доля {share:,.2f}"
            if reimb:
                detail += f" + возместить личные траты {reimb:,.2f}"
            if paid:
                detail += f" − уже выплачено {paid:,.2f}"
            lines.append(f"    <i>{detail}</i>")
        else:
            lines.append(f"👤 {name}: <b>{share:,.2f} ₽</b>")
    parts = payout.share_parts(s)
    if parts:
        lines.append(f"    <i>{' · '.join(parts)}</i>")
    return "\n".join(lines)


# Правило поддержки Prodamus (дословно): деньги переводятся на 2-й рабочий
# день после поступления, кроме выходных/праздников — но чт/пт/сб все
# уходят одним рейсом в ближайший понедельник, а вс — во вторник. Праздники
# не учитываем (нет календаря под рукой) — тут возможна погрешность в
# несколько дней в году, некритично для сверки с банком.
_PRODAMUS_PAYOUT_DELAY_DAYS = {
    0: 2,  # Понедельник → среда
    1: 2,  # Вторник → четверг
    2: 2,  # Среда → пятница
    3: 4,  # Четверг → понедельник
    4: 3,  # Пятница → понедельник
    5: 2,  # Суббота → понедельник
    6: 2,  # Воскресенье → вторник
}


def _prodamus_arrived(created_at_utc: str, now_msk_date) -> bool:
    """Дошли ли до счёта деньги за эту покупку — по правилу Prodamus выше.
    created_at хранится в UTC, день недели считаем по МСК."""
    try:
        receipt_utc = datetime.strptime(created_at_utc[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return True  # не смогли разобрать дату — не пугаем зря
    receipt_msk_date = (receipt_utc + timedelta(hours=3)).date()
    delay = _PRODAMUS_PAYOUT_DELAY_DAYS[receipt_msk_date.weekday()]
    return now_msk_date >= receipt_msk_date + timedelta(days=delay)


async def _pending_gross(dt_from: str, dt_to: str, now_utc: datetime) -> float:
    """Сколько из «Принято» ещё не перевёл на счёт Prodamus — точно, по их
    правилу, а не «плюс-минус 2 суток»."""
    purchases = await db.get_purchases_in_range(dt_from, dt_to)
    now_msk_date = (now_utc + timedelta(hours=3)).date()
    return sum(float(p["amount"]) for p in purchases
              if not _prodamus_arrived(p["created_at"], now_msk_date))


async def _debt_caveats(s: dict, pending_gross: float, cdek_paid: dict, npd_paid: dict) -> str:
    """Оговорки к цифрам «Взаиморасчёта»: часть «Принято» ещё не на счету
    (Prodamus переводит не мгновенно — 2-й рабочий день после оплаты), а
    часть начисленного СДЭК/НПД — ещё не оплачена по факту. Не ошибка,
    а просто разное время."""
    lines = []
    if pending_gross:
        lines.append(f"<i>⏳ ≈{pending_gross:,.2f} ₽ из «Принято» выше ещё не на счету — "
                     f"Prodamus переводит на 2-й рабочий день после оплаты</i>")

    not_paid_cdek = s["delivery_out"] - float(cdek_paid["total"])
    not_paid_npd = s["npd"] - float(npd_paid["total"])
    if not_paid_cdek > 0.5 or not_paid_npd > 0.5:
        lines.append(
            f"<i>Начислено выше, но не оплачено по факту: СДЭК {max(not_paid_cdek, 0):,.2f} ₽"
            f" · НПД {max(not_paid_npd, 0):,.2f} ₽ — резерв на счету, пока не пришёл "
            f"счёт СДЭК и не наступил срок уплаты налога.</i>"
        )
    return "\n".join(lines)


async def _debt_block(s: dict | None, last_settlement: dict | None,
                      dt_from: str, dt_to: str, now_utc: datetime) -> tuple[str, dict, dict, float]:
    """«Взаиморасчёт» + оговорки к его цифрам одним текстом. Заодно
    отдаёт cdek_paid/npd_paid/pending_gross, чтобы «Касса» не считала их
    повторно."""
    text = _fmt_debt_screen(s, last_settlement)
    cdek_paid = npd_paid = {"total": 0, "count": 0}
    pending_gross = 0.0
    if s and s["count"] > 0:
        cdek_paid = await db.get_cdek_payments_summary(dt_from, dt_to)
        npd_paid = await db.get_npd_payments_summary(dt_from, dt_to)
        pending_gross = await _pending_gross(dt_from, dt_to, now_utc)
        caveats = await _debt_caveats(s, pending_gross, cdek_paid, npd_paid)
        if caveats:
            text += "\n" + caveats
    return text, cdek_paid, npd_paid, pending_gross


async def _free_to_payout(dt_from: str, dt_to: str, now_utc: datetime) -> tuple[float, float, float, float]:
    """Сколько можно выплатить прямо сейчас, ничего не сломав.

    Резерв на СДЭК и начисленный НПД физически лежат на том же счету, что и
    прибыль, — их легко выплатить как долю и потом не найти денег на счёт
    СДЭК (так и вышло в августе 2026). Поэтому «свободно» — это ожидаемый
    остаток на счету МИНУС то, что уже начислено, но ещё не оплачено.

    Возвращает (свободно, остаток_на_счету, долг_СДЭК, долг_НПД).
    """
    from services import payout as payout_svc
    s = await payout_svc.split(dt_from, dt_to)
    if not s or not s["count"]:
        return 0.0, 0.0, 0.0, 0.0
    npd_paid_for_period = await db.get_npd_payments_summary_by_tax_month(dt_from, dt_to)
    payouts = await db.get_payouts_summary(dt_from, dt_to)
    pending_net = (await _pending_gross(dt_from, dt_to, now_utc)) * (1 - s["fee_pct"] / 100)

    cash = (s["gross"] - s["fee"] - s["expenses"]
            - payouts["total"] - pending_net)
    owe_cdek = max(0.0, s["delivery_out"])
    owe_npd = max(0.0, s["npd"] - float(npd_paid_for_period["total"]))
    return cash - owe_cdek - owe_npd, cash, owe_cdek, owe_npd


async def _cash_block_text(s: dict, dt_from: str, dt_to: str, cdek_paid: dict,
                           npd_paid: dict, pending_gross: float) -> str:
    """«Взаиморасчёт» выше — начисление: сколько ДОЛЖНО уйти на налог, СДЭК
    и доли партнёров. Здесь — сколько реально ушло (по вашим отметкам),
    сколько Prodamus реально перевёл (см. pending_gross) и сколько поэтому
    сколько свободно по текущему периоду после резервов."""
    payouts = await db.get_payouts_summary(dt_from, dt_to)
    # Комиссия с ещё не переведённой части — плоская оценка по общей ставке
    # периода, точнее взять неоткуда (Prodamus не отдаёт комиссию по заказу)
    pending_net = pending_gross * (1 - s["fee_pct"] / 100)

    npd_paid_for_period = await db.get_npd_payments_summary_by_tax_month(dt_from, dt_to)
    cash = (s["gross"] - s["fee"] - s["expenses"] - payouts["total"] - pending_net)

    lines = [
        "💰 <b>Касса</b> — деньги периода после фактических расходов",
        "─" * 30,
    ]
    if pending_net:
        lines.append(f"Ещё не перевёл Prodamus: −{pending_net:,.2f} ₽")
    lines += [
        f"Оплачено СДЭК по факту: −{float(cdek_paid['total']):,.2f} ₽"
        + (f" ({int(cdek_paid['count'])} плат.)" if cdek_paid["count"] else ""),
        f"Оплачено НПД по факту: −{float(npd_paid['total']):,.2f} ₽"
        + (f" ({int(npd_paid['count'])} плат.)" if npd_paid["count"] else ""),
    ]
    if payouts["total"]:
        by = " · ".join(f"{name} {amt:,.2f} ₽" for name, amt in payouts["by_recipient"].items())
        lines.append(f"Выплачено партнёрам: −{payouts['total']:,.2f} ₽ ({by})")
    owe_cdek = max(0.0, s["delivery_out"])
    owe_npd = max(0.0, s["npd"] - float(npd_paid_for_period["total"]))
    free = cash - owe_cdek - owe_npd
    lines += [
        "─" * 30,
        f"💰 <b>Деньги периода до резервов: {cash:,.2f} ₽</b>",
        "",
        f"🔒 Из них зарезервировано: СДЭК {owe_cdek:,.2f} ₽ · НПД {owe_npd:,.2f} ₽",
        f"✅ <b>Свободно к выплате: {free:,.2f} ₽</b>" if free > 0
        else f"⛔️ <b>Свободных денег нет: {free:,.2f} ₽</b> — на счету не хватает "
             f"даже на СДЭК и налог, выплаты лучше приостановить",
    ]
    return "\n".join(lines)


async def _finance_pulse_text() -> str:
    """Отдельный экран принятых платежей за несколько периодов."""
    now_utc = datetime.now(timezone.utc)
    today_from = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    def iso(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    tasks = [
        fetch_payments_summary(iso(today_from), iso(now_utc)),
        fetch_payments_summary(iso(now_utc - timedelta(days=7)), iso(now_utc)),
        fetch_payments_summary(iso(now_utc - timedelta(days=30)), iso(now_utc)),
        fetch_payments_summary("2020-01-01T00:00:00.000Z", iso(now_utc)),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    def safe(r):
        if isinstance(r, Exception):
            logger.error(f"finance pulse error: {r}")
            return None
        return r

    today_s, week_s, month_s, all_s = [safe(r) for r in results]

    now_msk = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    lines = [f"💳 <b>Финансы</b>  <i>· {now_msk} МСК</i>\n"]
    if today_s:
        lines.append(_fmt_period("📅 <b>Сегодня</b>", today_s))
    if week_s:
        lines.append(_fmt_period("📅 <b>7 дней</b>", week_s))
    if month_s:
        lines.append(_fmt_period("📅 <b>30 дней</b>", month_s))
    if all_s:
        lines.append(_fmt_period("📊 <b>Всё время</b>", all_s))
    if any(isinstance(r, Exception) for r in results):
        lines.append("⚠️ Часть данных не загрузилась — проверь логи")
    return "\n".join(lines).rstrip()


async def _settle_split() -> tuple[dict | None, dict | None, str, str, datetime]:
    """Раскладка payout.split за период с прошлого расчёта + сами границы —
    общая для главного экрана «Финансы» и подменю «Взаиморасчёт»."""
    last_settlement = await db.get_last_settlement()
    dt_from_iso = last_settlement["settled_at"] if last_settlement else "2020-01-01T00:00:00.000Z"
    now_utc = datetime.now(timezone.utc)
    dt_to_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    dt_from, dt_to = dt_from_iso[:19].replace("T", " "), dt_to_iso[:19].replace("T", " ")

    try:
        from services import payout as payout_svc
        s = await payout_svc.split(dt_from, dt_to)
    except Exception as e:
        logger.error(f"settle split fetch error: {e}")
        s = None

    return s, last_settlement, dt_from, dt_to, now_utc


def _finance_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Принятые платежи", callback_data="fin_revenue")],
        [InlineKeyboardButton(text="🤝 Взаиморасчёт", callback_data="fin_settle_menu"),
         InlineKeyboardButton(text="💰 Касса", callback_data="fin_cash")],
        [InlineKeyboardButton(text="🧾 НПД", callback_data="cash_log"),
         InlineKeyboardButton(text="🚚 СДЭК", callback_data="cdek_show")],
        [InlineKeyboardButton(text="🧾 Расходы", callback_data="exp_show"),
         InlineKeyboardButton(text="📦 Расходники", callback_data="stk_show")],
        [InlineKeyboardButton(text="📊 Сводки", callback_data="fin_reports")],
    ])


def _back_to_finance_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="fin_show")
    ]])


def _settle_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Мы в расчёте", callback_data="fin_settle")],
        [InlineKeyboardButton(text=f"👤 Выплата {_dative(config.OWNER_NAME)}", callback_data="payout:owner")],
        [InlineKeyboardButton(text=f"👤 Выплата {_dative(config.PARTNER_NAME)}", callback_data="payout:partner")],
        [InlineKeyboardButton(text="📋 История взаиморасчётов", callback_data="settle_log")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="fin_show")],
    ])


async def _admin_only(callback: CallbackQuery) -> bool:
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return False
    return True


async def _show_finance(message: Message):
    text = "💳 <b>Финансы</b>\n\nВыберите раздел:"
    markup = _finance_keyboard()
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("finance"))
async def cmd_finance(message: Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await state.clear()
    await message.answer("💳 <b>Финансы</b>\n\nВыберите раздел:",
                         parse_mode="HTML", reply_markup=_finance_keyboard())


@router.callback_query(F.data == "fin_show")
async def cb_fin_show(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    await state.clear()
    await callback.answer()
    await _show_finance(callback.message)


@router.callback_query(F.data == "fin_settle_menu")
async def cb_settle_menu(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    await state.clear()
    await callback.answer()
    s, last, start, end, now = await _settle_split()
    text, *_ = await _debt_block(s, last, start, end, now)
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_settle_menu_keyboard(),
    )


@router.callback_query(F.data == "fin_settle")
async def cb_fin_settle(callback: CallbackQuery):
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
        logger.error(f"settle error: {e}")
        await callback.message.edit_text(
            "❌ Не удалось получить данные.", reply_markup=_settle_menu_keyboard()
        )
        return

    # «Мы в расчёте» = обоим перевели их остаток за период. Записываем эти
    # переводы выплатами — иначе они есть только в банковской выписке, и
    # сверка счёта не сходится. Время ставим на секунду раньше расчёта,
    # чтобы выплата попала в закрываемый период, а не в следующий.
    u = callback.from_user
    name = f"@{u.username}" if u.username else (u.first_name or f"id:{u.id}")
    paid_at = (now_utc - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    now_msk = datetime.now(MSK)
    comment = f"Мы в расчёте {now_msk.strftime('%d.%m.%Y')}"
    from services.gsheets import request_expense_append
    share_lines, overpaid = [], []
    for recipient, left, reimb in (
        (config.OWNER_NAME, s["owner_left"], s.get("reimb_owner", 0)),
        (config.PARTNER_NAME, s["partner_left"], s.get("reimb_partner", 0)),
    ):
        if left >= 0.01:
            left = round(left, 2)
            payout_id = await db.add_payout(recipient, left, comment, u.id, name, paid_at=paid_at)
            request_expense_append(f"payout-{payout_id}", now_msk.strftime("%d.%m.%Y %H:%M"),
                                   left, f"Выплата {recipient}: {comment}")
            note = f", в т.ч. возмещение личных трат {reimb:,.2f} ₽" if reimb else ""
            share_lines.append(f"👤 {recipient}: <b>{left:,.2f} ₽</b> — записал выплату{note}")
        elif left <= -0.01:
            overpaid.append(f"⚠️ {recipient} за период получил на <b>{-left:,.2f} ₽</b> больше доли")
        else:
            share_lines.append(f"👤 {recipient}: уже всё выплачено")

    await db.add_settlement(gross=s["gross"], fee=s["fee"], net=s["net"], count=s["count"])

    text = (
        f"✅ <b>Расчёт зафиксирован</b> — {now_msk.strftime('%d.%m.%Y %H:%M')} МСК\n\n"
        f"Продаж: <b>{s['count']}</b>  |  Принято: <b>{s['gross']:,.2f} ₽</b>\n"
        f"Чистыми: <b>{s['net']:,.2f} ₽</b>\n\n"
        + "\n".join(share_lines + overpaid)
    )
    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=_settle_menu_keyboard())


@router.callback_query(F.data.startswith("npd_del:"))
async def cb_npd_delete(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    payment_id = int(callback.data.split(":")[1])
    deleted = await db.delete_npd_payment(payment_id)
    from services.gsheets import request_finance_sync
    request_finance_sync()
    await callback.answer("Удалено 🗑" if deleted else "Эта запись уже удалена")
    try:
        await callback.message.edit_text(
            "🗑 <s>Оплата НПД удалена</s>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="cash_log")
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
    await callback.message.edit_text(
        f"👤 <b>Сколько выплатили {_dative(recipient)}?</b>\n\nНапишите сумму, можно с комментарием:\n"
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

    # Сколько можно отдать, не залезая в резерв на СДЭК и налог
    _, _, dt_from, dt_to, now_utc = await _settle_split()
    free, _cash, owe_cdek, owe_npd = await _free_to_payout(dt_from, dt_to, now_utc)

    payout_id = await db.add_payout(recipient, amount, comment, u.id, name)
    await state.clear()

    warn = ""
    if amount > free:
        warn = (f"\n\n⚠️ <b>Выплата больше свободных денег.</b>\n"
                f"Свободно было: <b>{free:,.2f} ₽</b> — это остаток на счету за вычетом "
                f"неоплаченного СДЭК ({owe_cdek:,.2f} ₽) и налога ({owe_npd:,.2f} ₽).\n"
                f"После этой выплаты на обязательные платежи не хватает "
                f"<b>{amount - free:,.2f} ₽</b>. Записал, но имейте в виду.")

    from services.gsheets import request_expense_append
    sheet_comment = f"Выплата {recipient}" + (f": {comment}" if comment else "")
    request_expense_append(f"payout-{payout_id}", datetime.now(MSK).strftime("%d.%m.%Y %H:%M"),
                           amount, sheet_comment)

    tail = f"\n💬 {comment}" if comment else ""
    await message.answer(
        f"✅ Записал выплату\n\n👤 <b>{recipient}: {amount:,.2f} ₽</b>{tail}{warn}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"payout_del:{payout_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="fin_settle_menu")],
        ]),
    )


@router.callback_query(F.data.startswith("payout_del:"))
async def cb_payout_delete(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    payout_id = int(callback.data.split(":")[1])
    deleted = await db.delete_payout(payout_id)
    from services.gsheets import request_finance_sync
    request_finance_sync()
    await callback.answer("Удалено 🗑" if deleted else "Эта запись уже удалена")
    try:
        await callback.message.edit_text(
            "🗑 <s>Выплата удалена</s>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="fin_settle_menu")
            ]]),
        )
    except Exception:
        pass


async def _cash_log_render(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    months = await tax_account.months()
    page = max(0, min(page, max(0, (len(months) - 1) // 6)))
    lines = ["🧾 <b>НПД</b>", "\n<b>По месяцам:</b>"]
    rows = []
    for r in months[page * 6:(page + 1) * 6]:
        key = r['month']
        label = f"{key[5:]}.{key[:4]}"
        paid = r['paid'] > 0 and r['paid'] >= r['accrued'] - 0.005
        mark = "✅" if paid else "◻️"
        manual = " · вручную" if r['manual'] else " · авторасчёт"
        lines.append(f"{mark} {label}: <b>{r['accrued']:,.2f} ₽</b>{manual}\n"
                     f"Оплачено: {r['paid']:,.2f} ₽")
        rows.append([InlineKeyboardButton(text=f"{mark} {label} · {r['accrued']:,.2f} ₽", callback_data=f"npd_detail:{key}")])
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="⬅️ Новее", callback_data=f"npd_page:{page-1}"))
    if (page + 1) * 6 < len(months):
        nav.append(InlineKeyboardButton(text="Старее ➡️", callback_data=f"npd_page:{page+1}"))
    if nav:
        rows.append(nav)
    if not months:
        lines.append("Пока нет начислений.")
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="fin_show")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query((F.data == "cash_log") | F.data.startswith("npd_page:"))
async def cb_cash_log(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    await state.clear()
    await callback.answer()
    page = int(callback.data.split(":")[1]) if ":" in callback.data else 0
    text, markup = await _cash_log_render(page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data.startswith("npd_edit:"))
async def cb_npd_edit(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    month = callback.data.split(":", 1)[1]
    row = await tax_account.assessment(month)
    if not row or (row['paid'] > 0 and row['paid'] >= row['accrued'] - 0.005):
        await callback.answer("Месяц уже оплачен или не найден", show_alert=True)
        return
    await state.set_state(CashStates.waiting_npd)
    await state.update_data(npd_month=month)
    await callback.answer()
    await callback.message.edit_text(
        f"Введите полную сумму НПД за {month[5:]}.{month[:4]} в рублях.\n"
        f"Сейчас: {row['accrued']:,.2f} ₽. Уже оплачено: {row['paid']:,.2f} ₽.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Отмена", callback_data="cash_log")]]))


@router.message(CashStates.waiting_npd)
async def on_npd_amount(message: Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    try:
        amount = tax_account.parse_amount(message.text or "")
    except ValueError as e:
        await message.answer(str(e))
        return
    data = await state.get_data()
    try:
        await tax_account.edit(data['npd_month'], amount, message.from_user.id)
    except ValueError as e:
        await message.answer(str(e))
        return
    from services.gsheets import request_finance_sync
    request_finance_sync()
    await state.clear()
    text, markup = await _cash_log_render()
    await message.answer("✅ Сумма сохранена.\n\n" + text, parse_mode="HTML", reply_markup=markup)


def _msk_dt(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return (datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                + timedelta(hours=3)).strftime("%d.%m %H:%M")
    except Exception:
        return ""


def _npd_detail_lines(month: str, purchases: list[dict], assessment: dict | None) -> list[str]:
    year, mm = month.split("-", 1) if "-" in month else ("", month)
    total = sum(float(p["amount"]) for p in purchases)
    accrued = total * _NPD_RATE
    current = float(assessment["accrued"]) if assessment else accrued
    paid = float(assessment["paid"]) if assessment else 0.0
    source = "вручную" if assessment and assessment["manual"] else "авторасчёт"

    lines = [
        f"🧾 <b>НПД за {mm}.{year}</b>",
        f"Продаж: <b>{len(purchases)}</b> на <b>{total:,.2f} ₽</b>",
        f"Сумма НПД: <b>{current:,.2f} ₽</b> · {source}",
        f"Оплачено: <b>{paid:,.2f} ₽</b>",
    ]
    if assessment and assessment["manual"] and abs(current - accrued) >= 0.005:
        lines.append(f"Автооценка бота: {accrued:,.2f} ₽")
    return lines


@router.callback_query(F.data.startswith("npd_detail:"))
async def cb_npd_detail(callback: CallbackQuery):
    """Покупки за месяц по отдельности — сверить с чеками в «Мой налог»,
    когда начисленное в боте не сходится с приложением."""
    if not await _admin_only(callback):
        return
    month = callback.data.split(":", 1)[1]
    purchases = await db.get_purchases_by_month(month)
    assessment = await tax_account.assessment(month)
    lines = _npd_detail_lines(month, purchases, assessment)

    rows = []
    if assessment:
        paid = assessment['paid'] > 0 and assessment['paid'] >= assessment['accrued'] - 0.005
        if not paid:
            actions = [InlineKeyboardButton(text="✏️ Исправить сумму", callback_data=f"npd_edit:{month}")]
            if assessment['accrued'] > assessment['paid']:
                actions.append(InlineKeyboardButton(text="✅ Отметить оплаченным", callback_data=f"npd_markpaid:{month}"))
            rows.append(actions)
        payments = await db.get_npd_payments(limit=20, tax_month=month)
        if payments:
            lines.append("\n<b>Оплаты за этот месяц:</b>")
            for p in payments:
                lines.append(f"{_msk(p['paid_at'])}: {p['amount']:,.2f} ₽")
                rows.append([InlineKeyboardButton(text=f"🗑 Удалить оплату {_msk(p['paid_at'])} · {p['amount']:,.2f} ₽",
                                                  callback_data=f"npd_del:{p['id']}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="cash_log")])

    await callback.answer()
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("npd_markpaid:"))
async def cb_npd_markpaid(callback: CallbackQuery):
    """Одной кнопкой: записать оплату НПД за месяц на всю начисленную сумму —
    когда налог за этот месяц точно уже закрыт и сверять по копейке не нужно."""
    if not await _admin_only(callback):
        return
    month = callback.data.split(":", 1)[1]
    u = callback.from_user
    name = f"@{u.username}" if u.username else (u.first_name or f"id:{u.id}")
    try:
        payment_id, accrued = await tax_account.mark_paid(month, u.id, name)
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    if payment_id is None:
        await callback.answer("Этот месяц уже закрыт", show_alert=True)
        return
    year, mm = month.split("-")

    from services.gsheets import request_expense_append
    request_expense_append(f"npd-{payment_id}", datetime.now(MSK).strftime("%d.%m.%Y %H:%M"),
                           accrued, f"Налог НПД за {mm}.{year}")
    await callback.answer(f"Отмечено: {mm}.{year} оплачен ✅")
    text, markup = await _cash_log_render()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)


async def _settle_log_render() -> tuple[str, InlineKeyboardMarkup]:
    settlements = await db.get_settlements(limit=10)
    payouts = await db.get_payouts(limit=10)

    lines = ["📋 <b>История взаиморасчётов</b>"]
    if settlements:
        lines.append("\n<b>Расчёты («Мы в расчёте»):</b>")
        for s in settlements:
            lines.append(f"  {_msk(s['settled_at'])} — {int(s['count'])} продаж, "
                         f"принято {float(s['gross']):,.2f} ₽, чисто {float(s['net']):,.2f} ₽")
    if payouts:
        lines.append("\n<b>Выплаты:</b>")
        for p in payouts:
            comment = f" — {p['comment']}" if p.get("comment") else ""
            lines.append(f"  {_msk(p['paid_at'])} 👤 {p['recipient']}: "
                         f"{float(p['amount']):,.2f} ₽{comment}")
    if not settlements and not payouts:
        lines.append("\nПока пусто.")

    rows = [[InlineKeyboardButton(
        text=f"🗑 {p['recipient']} {_msk(p['paid_at'])} {float(p['amount']):,.0f} ₽",
        callback_data=f"payout_del:{p['id']}")] for p in payouts[:5]]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="fin_settle_menu")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "settle_log")
async def cb_settle_log(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    await callback.answer()
    text, markup = await _settle_log_render()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("gsheets_backfill"))
async def cmd_gsheets_backfill(message: Message):
    """Полная сверка: новые операции, изменения, отмены и сводка."""
    if message.from_user.id not in config.ADMIN_IDS:
        return
    if not config.GSHEETS_ENABLED:
        await message.answer("Google Таблица не настроена.")
        return
    m = await message.answer("⏳ Сверяю финансовую таблицу с базой…")
    try:
        from services.finance_sheet import sync_finance
        stats = await sync_finance()
    except Exception:
        logger.exception("gsheets_backfill: сверка не удалась")
        await m.edit_text("❌ Сверка не завершена. Автоматическая сверка повторит попытку.")
        return
    await m.edit_text(
        f"✅ Таблица сверена. Добавлено: {stats['added']}, обновлено: {stats['updated']}.\n"
        f"Исключено из расчёта: отменённых {stats['cancelled']}, дублей {stats['duplicates']}."
    )


@router.callback_query(F.data == "fin_revenue")
async def cb_fin_revenue(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(await _finance_pulse_text(), parse_mode="HTML",
                                     reply_markup=_back_to_finance_keyboard())


@router.callback_query(F.data == "fin_cash")
async def cb_fin_cash(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    await state.clear()
    await callback.answer()
    _, _, start, end, now = await _settle_split()
    s = await payout.split(start, end)
    text = await _cash_block_text(s, start, end,
        await db.get_cdek_payments_summary(start, end),
        await db.get_npd_payments_summary(start, end),
        await _pending_gross(start, end, now))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_back_to_finance_keyboard())


@router.callback_query((F.data == 'fin_reports') | F.data.startswith('fin_reports_page:'))
async def cb_fin_reports(callback: CallbackQuery, state: FSMContext):
    if not await _admin_only(callback):
        return
    await state.clear()
    await callback.answer()
    from services import finance_reports as reports
    page = int(callback.data.split(':')[1]) if ':' in callback.data else 0
    saved = await reports.history(page)
    rows = [[InlineKeyboardButton(text=reports.period_label(data), callback_data=f'fin_report:{rid}')]
            for rid, data in saved[:10]]
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text='⬅️ Новее', callback_data=f'fin_reports_page:{page-1}'))
    if len(saved) > 10:
        nav.append(InlineKeyboardButton(text='Старее ➡️', callback_data=f'fin_reports_page:{page+1}'))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text='◀️ Назад', callback_data='fin_show')])
    text = (f'📊 Сводки создаются 15-го и в последний день месяца в {config.DAILY_REPORT_HOUR_MSK:02d}:00 МСК.\n'
            'Каждая — за период после предыдущей сводки.')
    if not saved:
        text += '\n\nСохранённых сводок пока нет.'
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith('fin_report:'))
async def cb_fin_report(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    from services import finance_reports as reports
    rid = int(callback.data.split(':')[1])
    data = await reports.get(rid)
    if not data:
        await callback.answer('Сводка не найдена', show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(reports.render(data), parse_mode='HTML', reply_markup=reports.keyboard(rid))


@router.callback_query(F.data.startswith('fin_xlsx:'))
async def cb_fin_xlsx(callback: CallbackQuery):
    if not await _admin_only(callback):
        return
    from aiogram.types import BufferedInputFile
    from services import finance_reports as reports
    rid = int(callback.data.split(':')[1])
    data = await reports.get(rid)
    if not data:
        await callback.answer('Сводка не найдена', show_alert=True)
        return
    await callback.answer()
    content = await asyncio.to_thread(reports.xlsx, data)
    await callback.message.answer_document(BufferedInputFile(content, filename=f'finance-{data["end"][:10]}-{rid}.xlsx'))
