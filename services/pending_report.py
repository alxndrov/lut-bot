"""Раз в неделю, по четвергам в 09:00 МСК — сводка по незавершённым заказам.

Сколько за неделю добавилось заказов, которые клиенты заполнили, но не
оплатили. Приходит Дане в админский бот. Неделя — от прошлого четверга
09:00 до этого, конец не включён.

Незавершённый заказ после оплаты из списка удаляется, поэтому в сводку
попадают только те, что так и остались неоплаченными.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html import escape

import aiosqlite

import config
import database as db

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
WEEKDAY = 3                    # четверг (понедельник = 0)
LIST_LIMIT = 15


def last_boundary(now: datetime) -> datetime:
    """Последний четверг 09:00 МСК, не позже now."""
    now = now.astimezone(MSK)
    day = now.replace(hour=config.DAILY_REPORT_HOUR_MSK, minute=0, second=0, microsecond=0)
    day -= timedelta(days=(day.weekday() - WEEKDAY) % 7)
    if day > now:
        day -= timedelta(days=7)
    return day


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def build(start: datetime, end: datetime) -> str:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """SELECT pd.created_at, pd.amount, pd.nudged_at, pd.user_id,
                      pr.name AS product_name, u.username, u.first_name
               FROM pending_deliveries pd
               LEFT JOIN products pr ON pr.id = pd.product_id
               LEFT JOIN users u ON u.user_id = pd.user_id
               WHERE pd.created_at >= ? AND pd.created_at < ?
               ORDER BY pd.created_at""", (_utc(start), _utc(end))) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with conn.execute("SELECT COUNT(*) FROM pending_deliveries") as cur:
            total_now = (await cur.fetchone())[0]

    period = f"{start.strftime('%d.%m')} – {end.strftime('%d.%m')}"
    lines = ["🛒 <b>Незавершённые заказы за неделю</b>", period, ""]
    if not rows:
        lines.append("Неоплаченных заказов за неделю не добавилось 🎉")
    else:
        money = sum(int(r["amount"] or 0) for r in rows)
        lines.append(f"Добавилось неоплаченных: <b>{len(rows)}</b>"
                     + (f" на {money:,} ₽".replace(",", " ") if money else ""))
        lines.append("")
        for r in rows[:LIST_LIMIT]:
            when = datetime.strptime(r["created_at"][:19], "%Y-%m-%d %H:%M:%S") \
                .replace(tzinfo=timezone.utc).astimezone(MSK).strftime("%d.%m")
            who = f"@{r['username']}" if r.get("username") else (r.get("first_name") or f"id:{r['user_id']}")
            amount = f" · {int(r['amount'])} ₽" if r.get("amount") else ""
            asked = " 📨" if r.get("nudged_at") else ""
            lines.append(f"• {when} {escape(who)} · {escape(r.get('product_name') or '—')}{amount}{asked}")
        if len(rows) > LIST_LIMIT:
            lines.append(f"…и ещё {len(rows) - LIST_LIMIT}")
    lines += ["", f"Всего в списке незавершённых сейчас: {total_now}",
              "<i>📨 — уже спрашивали. Спросить клиента: админка основного бота → "
              "🛒 Незавершённые заказы.</i>"]
    return "\n".join(lines)


async def tick(bot, now: datetime) -> None:
    boundary = last_boundary(now)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute("SELECT last_at FROM pending_report_schedule WHERE id=1") as cur:
            row = await cur.fetchone()
        if row is None:
            # Первый запуск: прошлую неделю задним числом не шлём, ждём четверга
            await conn.execute("INSERT INTO pending_report_schedule(id, last_at) VALUES (1, ?)",
                               (boundary.isoformat(),))
            await conn.commit()
            return
    if datetime.fromisoformat(row[0]) >= boundary:
        return
    # После простоя шлём только последнюю неделю — пачка старых никому не нужна
    text = await build(boundary - timedelta(days=7), boundary)
    try:
        await bot.send_message(config.PARTNER_ID, text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"pending_report: не отправилась: {type(e).__name__}: {e}")
        return                                  # попробуем на следующем тике
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("UPDATE pending_report_schedule SET last_at=? WHERE id=1",
                           (boundary.isoformat(),))
        await conn.commit()
    logger.info(f"pending_report: отправлена сводка за неделю до {boundary:%d.%m}")


async def pending_report_loop(bot) -> None:
    logger.info("pending_report_loop: запущен (четверг, "
                f"{config.DAILY_REPORT_HOUR_MSK}:00 МСК)")
    while True:
        try:
            await tick(bot, datetime.now(MSK))
        except Exception:
            logger.exception("pending_report: tick failed")
        await asyncio.sleep(60)
