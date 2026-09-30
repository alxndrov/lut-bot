"""Monthly NPD assessments, with atomic edits and payment acknowledgement."""
import math
import re
from datetime import datetime

import aiosqlite
import config
import database as db


def parse_amount(text: str) -> float:
    text = text.strip()
    if not re.fullmatch(r'(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d{1,2})?', text):
        raise ValueError('Введите только сумму, например 1250,50. Можно 0.')
    amount = float(text.replace(' ', '').replace('\u00a0', '').replace(',', '.'))
    if not math.isfinite(amount):
        raise ValueError('Некорректная сумма')
    return amount


def validate_month(month: str):
    if datetime.strptime(month, '%Y-%m').strftime('%Y-%m') != month:
        raise ValueError('Некорректный месяц')


async def _months(conn):
    conn.row_factory = aiosqlite.Row
    async with conn.execute("""
        WITH revenue AS (
            SELECT strftime('%Y-%m', datetime(created_at, '+3 hours')) month,
                   SUM(amount) gross FROM purchases GROUP BY month
        ), paid AS (
            SELECT COALESCE(tax_month, strftime('%Y-%m', datetime(paid_at, '+3 hours'))) month,
                   SUM(amount) paid FROM npd_payments GROUP BY month
        ), months AS (
            SELECT month FROM revenue UNION SELECT month FROM paid
            UNION SELECT month FROM npd_assessments
        )
        SELECT months.month, COALESCE(gross, 0) gross,
               COALESCE(a.amount, ROUND(COALESCE(gross, 0) * ?, 2)) accrued,
               COALESCE(paid.paid, 0) paid, a.amount IS NOT NULL manual
        FROM months LEFT JOIN revenue USING(month) LEFT JOIN paid USING(month)
        LEFT JOIN npd_assessments a USING(month)
        WHERE months.month IS NOT NULL ORDER BY months.month DESC
    """, (config.NPD_PERCENT / 100,)) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def months():
    async with aiosqlite.connect(db.DB_PATH) as conn:
        return await _months(conn)


async def assessment(month):
    validate_month(month)
    return next((r for r in await months() if r['month'] == month), None)


async def edit(month: str, amount: float, user_id: int):
    validate_month(month)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError('Введите неотрицательную сумму')
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute('BEGIN IMMEDIATE')
        row = next((r for r in await _months(conn) if r['month'] == month), None)
        if not row:
            raise ValueError('Месяц не найден')
        if row['paid'] > 0 and row['paid'] >= row['accrued'] - 0.005:
            raise ValueError('Этот месяц уже оплачен')
        if amount < row['paid']:
            raise ValueError('Сумма не может быть меньше уже оплаченной части')
        await conn.execute('''INSERT INTO npd_assessments(month, amount, updated_by)
            VALUES (?, ?, ?) ON CONFLICT(month) DO UPDATE SET amount=excluded.amount,
            updated_by=excluded.updated_by, updated_at=CURRENT_TIMESTAMP''',
            (month, round(amount, 2), user_id))
        await conn.commit()


async def mark_paid(month: str, user_id: int, user_name: str):
    """Pay only outstanding balance. Serialization prevents duplicate clicks."""
    validate_month(month)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute('BEGIN IMMEDIATE')
        row = next((r for r in await _months(conn) if r['month'] == month), None)
        if not row:
            raise ValueError('Месяц не найден')
        amount = round(max(0, row['accrued'] - row['paid']), 2)
        if not amount:
            return None, 0
        cur = await conn.execute('''INSERT INTO npd_payments
            (amount, comment, user_id, user_name, tax_month) VALUES (?, ?, ?, ?, ?)''',
            (amount, f'НПД за {month}', user_id, user_name, month))
        await conn.commit()
        return cur.lastrowid, amount


async def accrued_for_period(dt_from: str, dt_to: str, component: str = "total") -> float:
    """Allocate a corrected month across receipts proportionally to revenue.

    This preserves the monthly total and avoids charging the entire correction
    twice when consecutive reports divide a month.
    """
    dt_from, dt_to = db.period_bounds(dt_from, dt_to)
    # Fixed expressions only; no user-provided SQL.
    base = {
        "total": "p.amount",
        "physical": "CASE WHEN pr.category='physical' THEN p.amount-COALESCE(p.delivery_amount, 0) ELSE 0 END",
        "digital": "CASE WHEN pr.category='physical' THEN 0 ELSE p.amount-COALESCE(p.delivery_amount, 0) END",
        "legacy": "CASE WHEN COALESCE(p.delivery_cost, 0)=0 THEN COALESCE(p.delivery_amount, 0) ELSE 0 END",
    }[component]
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(f'''
            WITH monthly AS (
                SELECT strftime('%Y-%m', datetime(created_at, '+3 hours')) month,
                       SUM(amount) gross FROM purchases GROUP BY month
            )
            SELECT COALESCE(SUM(({base}) * CASE
                WHEN a.amount IS NOT NULL AND m.gross > 0 THEN a.amount / m.gross
                ELSE ? END), 0)
            FROM purchases p
            LEFT JOIN products pr ON pr.id=p.product_id
            JOIN monthly m ON m.month = strftime('%Y-%m', datetime(p.created_at, '+3 hours'))
            LEFT JOIN npd_assessments a ON a.month = m.month
            WHERE datetime(p.created_at) BETWEEN datetime(?) AND datetime(?)
        ''', (config.NPD_PERCENT / 100, dt_from, dt_to)) as cur:
            return float((await cur.fetchone())[0])
