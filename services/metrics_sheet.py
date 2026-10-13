"""Таблица «Метрики»: сводка по дням и по источникам трафика.

Данные берутся из SQLite (users, course_views, purchases), лист целиком
перезаписывается раз в час. Ролики площадок добавляются отдельно.
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import aiosqlite

import config
from database import DB_PATH

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))
DAYS_TAB = 'По дням'
SOURCES_TAB = 'Источники'
PERIOD = 3600

DAYS_HEADERS = ['Дата', 'Новых в боте', 'Открыли курс', 'Оплат товаров', 'Выручка товаров, ₽',
                'Оплат курса', 'Выручка курса, ₽', 'Выручка всего, ₽']
SOURCES_HEADERS = ['Источник', 'Площадка', 'Пришло', 'Открыли курс', 'Купили товар',
                   'Купили курс', 'Выручка, ₽', 'Конверсия в покупку, %']
PLATFORMS = {'tt': 'TikTok', 'ig': 'Instagram', 'yt': 'YouTube', 'vk': 'VK', 'tg': 'Telegram',
             'reels': 'Instagram', 'stories': 'Instagram'}


def _day(value: str) -> str:
    """UTC-время из SQLite → дата по Москве."""
    dt = datetime.fromisoformat(value).replace(tzinfo=timezone.utc).astimezone(MSK)
    return dt.strftime('%Y-%m-%d')


def source_of(ref: str | None) -> str:
    """Метка источника из start-параметра: src_tt_13okt, lm_<id>_src_reels → tt_13okt, reels."""
    if not ref:
        return 'без метки'
    if '_src_' in ref:
        return ref.split('_src_', 1)[1]
    if ref.startswith('src_'):
        return ref[4:]
    return ref


def platform_of(source: str) -> str:
    return PLATFORMS.get(source.split('_', 1)[0].lower(), '')


async def collect():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        users = [dict(r) for r in await (await db.execute(
            'SELECT user_id, first_seen, ref FROM users')).fetchall()]
        views = [dict(r) for r in await (await db.execute(
            'SELECT user_id, first_at FROM course_views')).fetchall()]
        buys = [dict(r) for r in await (await db.execute(
            'SELECT user_id, amount, created_at, course_option_id FROM purchases')).fetchall()]
    return users, views, buys


def build(users, views, buys):
    days = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    src = defaultdict(lambda: {'new': set(), 'view': set(), 'goods': set(), 'course': set(), 'sum': 0})
    ref_of = {u['user_id']: source_of(u['ref']) for u in users}
    for u in users:
        days[_day(u['first_seen'])][0] += 1
        src[ref_of[u['user_id']]]['new'].add(u['user_id'])
    for v in views:
        days[_day(v['first_at'])][1] += 1
        src[ref_of.get(v['user_id'], 'без метки')]['view'].add(v['user_id'])
    for b in buys:
        d = days[_day(b['created_at'])]
        s = src[ref_of.get(b['user_id'], 'без метки')]
        if b['course_option_id'] is not None:
            d[4] += 1
            d[5] += b['amount']
            s['course'].add(b['user_id'])
        else:
            d[2] += 1
            d[3] += b['amount']
            s['goods'].add(b['user_id'])
        s['sum'] += b['amount']
    day_rows = [[k, v[0], v[1], v[2], v[3], v[4], v[5], v[3] + v[5]]
                for k, v in sorted(days.items(), reverse=True)]
    src_rows = []
    for name, s in sorted(src.items(), key=lambda kv: (-len(kv[1]['new']), kv[0])):
        buyers = len(s['goods'] | s['course'])
        conv = round(100 * buyers / len(s['new']), 1) if s['new'] else 0
        src_rows.append([name, platform_of(name), len(s['new']), len(s['view']),
                         len(s['goods']), len(s['course']), s['sum'], conv])
    return day_rows, src_rows


def publish(day_rows, src_rows):
    from services.gsheets import SheetsError
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        config.GOOGLE_CREDENTIALS_FILE, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    client = gspread.authorize(creds)
    client.set_timeout((10, 60))
    book = client.open_by_key(config.METRICS_SHEET_ID)
    titles = {w.title: w for w in book.worksheets()}
    for title, headers, rows in ((DAYS_TAB, DAYS_HEADERS, day_rows), (SOURCES_TAB, SOURCES_HEADERS, src_rows)):
        ws = titles.get(title) or book.add_worksheet(title, rows=1000, cols=len(headers))
        ws.clear()
        ws.update([headers] + rows, 'A1', value_input_option='USER_ENTERED')
        ws.freeze(rows=1)
        ws.format('1:1', {'textFormat': {'bold': True}})
    stale = titles.get('Лист1')
    if stale and len(book.worksheets()) > 1 and not stale.get_all_values():
        book.del_worksheet(stale)


async def sync_metrics():
    day_rows, src_rows = build(*await collect())
    await asyncio.to_thread(publish, day_rows, src_rows)
    logger.info('Метрики: %d дней, %d источников', len(day_rows), len(src_rows))


async def metrics_sync_loop():
    if not config.METRICS_SHEET_ID:
        return
    while True:
        try:
            await sync_metrics()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Метрики: выгрузка не удалась')
        await asyncio.sleep(PERIOD)
