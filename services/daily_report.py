"""
Ночной и месячный отчёты о продажах — отправляются админам автоматически.
Данные берутся из локальной БД.
"""
import asyncio
import calendar
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot

import config
import database as db

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
_SHARES = [("Миша", 0.80), ("Даня", 0.20)]


async def fetch_payments_summary(dt_from: str, dt_to: str) -> dict:
    """
    Возвращает {count, gross, net, fee, vat, fee_pct} за диапазон UTC ISO datetime.
    Данные берутся из локальной БД (таблица purchases).
    """
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM purchases
               WHERE datetime(created_at) >= datetime(?)
                 AND datetime(created_at) <= datetime(?)""",
            (dt_from, dt_to),
        ) as cur:
            row = await cur.fetchone()
            count = int(row[0] or 0)
            gross = float(row[1] or 0)

    fee_pct = config.PRODAMUS_FEE_PERCENT
    fee = gross * fee_pct / 100
    net = gross - fee
    return {
        "count": count,
        "gross": gross,
        "net": net,
        "fee": fee,
        "vat": 0.0,          # не выделяем отдельно
        "fee_pct": round(fee_pct, 2),
    }


def _build_report(date_str: str, data: dict) -> str:
    count = data["count"]
    total_gross = float(data["total"])
    by_product = data["by_product"]

    if not count:
        return (
            f"📊 <b>Отчёт за {date_str}</b>\n\n"
            "Продаж за этот день не было."
        )

    fee_pct = config.PRODAMUS_FEE_PERCENT
    total_fee = total_gross * fee_pct / 100
    total_net = total_gross - total_fee

    npd_tax = total_net * 0.04
    net_after_tax = total_net - npd_tax

    share_lines = [
        f"  {name} ({int(pct * 100)}%): <b>{net_after_tax * pct:,.2f} ₽</b>"
        for name, pct in _SHARES
    ]

    lines = [
        f"📊 <b>Отчёт за {date_str}</b>\n",
        f"🛍 Продаж: <b>{count}</b>",
        f"💰 Выручка брутто: <b>{total_gross:,.2f} ₽</b>",
        "",
        f"🏦 Комиссия Prodamus ({fee_pct}%): <b>−{total_fee:,.2f} ₽</b>",
        "",
        f"✅ Итого к получению: <b>{total_net:,.2f} ₽</b>",
        f"📋 НПД 4% (самозанятость): −{npd_tax:,.2f} ₽",
        f"💵 После налога: <b>{net_after_tax:,.2f} ₽</b>",
        "",
        "💰 <b>Расчёт:</b>",
    ] + share_lines

    if len(by_product) > 1 or (len(by_product) == 1 and count > 1):
        lines.append("")
        lines.append("📦 <b>По товарам:</b>")
        for row in by_product:
            name = row["name"] or "Без названия"
            pct = round(float(row["subtotal"]) / total_gross * 100, 1) if total_gross else 0
            lines.append(
                f"  • {name}: {row['cnt']} шт. — {float(row['subtotal']):,.2f} ₽ ({pct}%)"
            )

    return "\n".join(lines)


async def send_daily_report(bot: Bot):
    yesterday = (datetime.now(MSK) - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        data = await db.get_purchases_report(yesterday)
    except Exception as e:
        logger.error(f"daily_report: DB error: {e}")
        data = {"count": 0, "total": 0, "by_product": []}

    text = _build_report(yesterday, data)

    notify_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
    try:
        for admin_id in config.ADMIN_IDS:
            try:
                await notify_bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"daily_report: send to admin {admin_id} failed: {e}")
    finally:
        await notify_bot.session.close()


async def _seconds_until_next_report() -> float:
    now = datetime.now(MSK)
    target = now.replace(
        hour=config.DAILY_REPORT_HOUR_MSK, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


_MONTH_RU = [
    "", "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"
]
_MONTH_RU_GEN = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]


def _build_monthly_report(year: int, month: int, data: dict) -> str:
    count = data["count"]
    total_gross = data["total"]
    by_product = data["by_product"]
    month_label = f"{_MONTH_RU[month].capitalize()} {year}"

    if not count:
        return f"📅 <b>Месячный отчёт — {month_label}</b>\n\nПродаж в этом месяце не было."

    fee_pct = config.PRODAMUS_FEE_PERCENT
    total_fee = total_gross * fee_pct / 100
    total_net = total_gross - total_fee
    npd_tax = total_net * 0.04
    net_after_tax = total_net - npd_tax

    lines = [
        f"📅 <b>Месячный отчёт — {month_label}</b>",
        "",
        f"🛍 Продаж: <b>{count}</b>  |  Покупателей: <b>{data['unique_buyers']}</b>",
        f"👥 Новых пользователей: <b>{data['new_users']}</b>",
        f"💳 Средний чек: <b>{data['avg_order']:,.2f} ₽</b>",
    ]

    if data.get("best_day"):
        bd = data["best_day"]
        day_parts = bd["day"].split("-")
        best_label = f"{int(day_parts[2])} {_MONTH_RU_GEN[int(day_parts[1])]}"
        lines.append(f"🏆 Лучший день: <b>{best_label}</b> — {float(bd['day_total']):,.2f} ₽")

    lines += [
        "",
        f"💰 Выручка брутто: <b>{total_gross:,.2f} ₽</b>",
        f"🏦 Комиссия Prodamus ({fee_pct}%): −{total_fee:,.2f} ₽",
        f"📋 НПД 4%: −{npd_tax:,.2f} ₽",
        "─" * 30,
        f"💵 Чистыми: <b>{net_after_tax:,.2f} ₽</b>",
    ]

    # Доли
    for name, pct in _SHARES:
        lines.append(f"  {name} ({int(pct * 100)}%): <b>{net_after_tax * pct:,.2f} ₽</b>")

    # По товарам
    if by_product:
        lines.append("")
        lines.append("📦 <b>По товарам:</b>")
        for p in by_product:
            name = p["name"] or "Без названия"
            pct_of_gross = round(float(p["subtotal"]) / total_gross * 100, 1) if total_gross else 0
            lines.append(
                f"  • {name}: {p['cnt']} шт. — {float(p['subtotal']):,.2f} ₽ ({pct_of_gross}%)"
            )

    return "\n".join(lines)


async def send_monthly_report(bot: Bot):
    now_msk = datetime.now(MSK)
    # Отчёт за текущий месяц (отправляем в последний день)
    year, month = now_msk.year, now_msk.month

    try:
        data = await db.get_monthly_purchases_report(year, month)
    except Exception as e:
        logger.error(f"monthly_report: DB error: {e}")
        return

    text = _build_monthly_report(year, month, data)

    notify_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
    try:
        for admin_id in config.ADMIN_IDS:
            try:
                await notify_bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"monthly_report: send to admin {admin_id} failed: {e}")
    finally:
        await notify_bot.session.close()


async def _seconds_until_monthly_report() -> float:
    """Считает секунды до последнего дня месяца в DAILY_REPORT_HOUR_MSK по МСК."""
    now = datetime.now(MSK)
    last_day = calendar.monthrange(now.year, now.month)[1]
    target = now.replace(
        day=last_day,
        hour=config.DAILY_REPORT_HOUR_MSK,
        minute=0, second=0, microsecond=0,
    )
    if target <= now:
        # Переходим к последнему дню следующего месяца
        if now.month == 12:
            next_year, next_month = now.year + 1, 1
        else:
            next_year, next_month = now.year, now.month + 1
        last_day_next = calendar.monthrange(next_year, next_month)[1]
        target = now.replace(
            year=next_year, month=next_month, day=last_day_next,
            hour=config.DAILY_REPORT_HOUR_MSK,
            minute=0, second=0, microsecond=0,
        )
    return (target - now).total_seconds()


async def monthly_report_loop(bot: Bot):
    logger.info("monthly_report_loop started")
    while True:
        wait = await _seconds_until_monthly_report()
        now = datetime.now(MSK)
        last_day = calendar.monthrange(now.year, now.month)[1]
        logger.info(
            f"monthly_report: next report on {last_day:02d}.{now.month:02d} "
            f"in {wait / 3600:.1f}h"
        )
        await asyncio.sleep(wait)
        try:
            await send_monthly_report(bot)
        except Exception as e:
            logger.error(f"monthly_report: failed: {e}")


async def daily_report_loop(bot: Bot):
    logger.info(
        f"daily_report_loop started, will send at {config.DAILY_REPORT_HOUR_MSK}:00 MSK"
    )
    while True:
        wait = await _seconds_until_next_report()
        logger.info(f"daily_report: next report in {wait / 3600:.1f}h")
        await asyncio.sleep(wait)
        try:
            await send_daily_report(bot)
        except Exception as e:
            logger.error(f"daily_report: failed: {e}")
