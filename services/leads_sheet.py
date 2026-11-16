"""«Миша Стор база заинтересованных»: кто интересовался и не купил.

Даня пишет этим людям лично; таблица — его рабочий список. Бот раз в 15
минут добавляет новых людей и обновляет то, что знает сам:

    B Telegram, C Имя, D Telegram ID, E Интерес, F Что именно,
    G Где остановился, H Последняя активность, I В боте с

Колонки Дани — J Статус, K Следующий шаг, L Когда написать, M Первое
сообщение, N Ответ и заметки — бот пишет только один раз, при добавлении
строки (J и пометка о рассылке 05.11 в N), и ещё ставит J = «Купил», когда
человек оплатил то, чем интересовался. A — формула номера, её не трогаем.

Источники (время в SQLite — UTC, в таблицу пишется дата по Москве):
- course_views — открыл экран курса в боте или приложении;
- product_views — открыл карточку товара;
- pending_deliveries — заполнил анкету доставки физтовара и не оплатил;
- pay_clicks без paid_at — открыл нашу ссылку оплаты и не заплатил.
Интерес закрыт, если человек купил это (курс — любой раздел или доступ;
товар — покупка этого товара когда угодно).

Строку находим по Telegram ID (D): ник меняется, ID нет. Кого бот добавил,
помним в leads_sheet — если Даня удалил строку, бот её не вернёт. Если
человек удалил данные (/forget), бот очищает его строку.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

import config
from database import DB_PATH

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))
TAB = 'База'
PERIOD = 900
LAST_ROW = 1000          # до этой строки в таблице заданы формат, списки и формула №
SKIP_USERNAMES = {'ncomonetasupport'}   # аккаунт поддержки, не покупатель

COURSE, PHYSICAL, DIGITAL, OTHER = 'Курс', 'Физтовар', 'Цифровой товар', 'Не определено'
CATEGORY = {'physical': PHYSICAL, 'digital': DIGITAL, 'infobiz': COURSE}
PAY_KIND = {'c': COURSE, 'p': PHYSICAL, 'd': DIGITAL}
# Чем дальше человек дошёл, тем выше этап; по нему выбирается «Интерес»
STAGES = {'Открыл товар': 1, 'Открыл курс': 1, 'Заполнил анкету доставки': 2, 'Перешёл к оплате': 3}
BOUGHT = 'Купил'
NEW = 'Не связывались'


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _date(value: datetime) -> str:
    return value.astimezone(MSK).strftime('%d.%m.%Y')


def _rub(amount) -> str:
    return f'{int(amount):,} ₽'.replace(',', ' ')


def _text(value: str) -> str:
    """Апостроф — чтобы Sheets не принял имя вроде «=…» или «+7…» за формулу/число."""
    return "'" + value if value else ''


async def collect() -> dict:
    """Всё нужное из базы одним заходом."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async def rows(sql, *args):
            return [dict(r) for r in await (await db.execute(sql, args)).fetchall()]

        await db.execute('''CREATE TABLE IF NOT EXISTS leads_sheet (
            user_id INTEGER PRIMARY KEY, added_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
        has_campaign = bool(await rows(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='course_campaign'"))
        return {
            'users': {u['user_id']: u for u in await rows(
                'SELECT user_id, username, first_name, first_seen FROM users')},
            'products': {p['id']: p for p in await rows(
                '''SELECT p.id, p.name, p.category,
                          EXISTS(SELECT 1 FROM course_options o WHERE o.product_id = p.id) AS is_course
                   FROM products p''')},
            'course_views': await rows('SELECT * FROM course_views'),
            'product_views': await rows('SELECT * FROM product_views'),
            'pending': await rows(
                'SELECT user_id, product_id, created_at, amount FROM pending_deliveries'),
            'clicks': await rows(
                'SELECT user_id, order_num, amount, name, clicked_at FROM pay_clicks '
                'WHERE paid_at IS NULL'),
            'purchases': await rows(
                'SELECT user_id, product_id, course_option_id, amount, created_at FROM purchases'),
            'access': {r['user_id'] for r in await rows(
                'SELECT DISTINCT user_id FROM course_access WHERE revoked_at IS NULL')},
            'campaign': {r['user_id']: r for r in await rows(
                '''SELECT x.user_id, x.state,
                          EXISTS(SELECT 1 FROM course_campaign_events e WHERE e.user_id = x.user_id
                                 AND e.kind IN ('reply', 'question')) AS answered
                   FROM course_campaign x''')} if has_campaign else {},
            'tracked': {r['user_id']: r['added_at'] for r in await rows(
                'SELECT user_id, added_at FROM leads_sheet')},
        }


def build(data: dict) -> dict[int, dict]:
    """Человек → что о нём писать. open=False — всё, чем интересовался, уже купил."""
    users, products = data['users'], data['products']
    signals: dict[int, list[dict]] = {}

    def add(uid, interest, stage, what, at, product_id=None):
        signals.setdefault(uid, []).append({'interest': interest, 'stage': stage, 'what': what,
                                            'at': _utc(at), 'product_id': product_id})

    for v in data['course_views']:
        where = 'приложение' if v.get('source') == 'app' else 'бот'
        add(v['user_id'], COURSE, 'Открыл курс', f"экран курса ({where}), раз: {v['views']}",
            v['last_at'])
    for v in data['product_views']:
        p = products.get(v['product_id'])
        if p and not p['is_course']:
            add(v['user_id'], CATEGORY.get(p['category'], OTHER), 'Открыл товар', f"карточка: {p['name']}",
                v['last_at'], p['id'])
    for d in data['pending']:
        p = products.get(d['product_id'])
        add(d['user_id'], PHYSICAL, 'Заполнил анкету доставки',
            f"анкета доставки: {p['name'] if p else 'товар'}, {_rub(d['amount'] or 0)}",
            d['created_at'], d['product_id'])
    for c in data['clicks']:
        kind, _, rest = (c['order_num'] or '').partition('_')
        if kind not in PAY_KIND:
            continue
        product_id = None
        if kind != 'c':
            tail = rest.split('_')[-1]
            product_id = int(tail) if tail.isdigit() else None
        add(c['user_id'], PAY_KIND[kind], 'Перешёл к оплате',
            f"оплата: {c['name']}, {_rub(c['amount'] or 0)}", c['clicked_at'], product_id)

    course_buyers = set(data['access'])
    bought_products, purchases = set(), {}
    for p in data['purchases']:
        purchases.setdefault(p['user_id'], []).append(_utc(p['created_at']))
        if p['course_option_id'] is not None:
            course_buyers.add(p['user_id'])
        elif p['product_id'] is not None:
            bought_products.add((p['user_id'], p['product_id']))

    tracked = data['tracked']
    # Кого бот уже добавил, оцениваем и без сигналов: после оплаты анкета
    # доставки удаляется, а клик по оплате получает paid_at — сигнал пропадает
    for uid in tracked:
        signals.setdefault(uid, [])

    leads = {}
    for uid, items in signals.items():
        user = users.get(uid)
        if (not user or uid in config.ADMIN_IDS
                or (user.get('username') or '').lower() in SKIP_USERNAMES):
            continue   # нет в users — человек удалил данные (/forget)
        open_items = [s for s in items if not (
            uid in course_buyers if s['interest'] == COURSE
            else (uid, s['product_id']) in bought_products)]
        # Покупка после того, как человек попал в таблицу (или после первого интереса)
        since = _utc(tracked[uid]) if uid in tracked else min(s['at'] for s in items)
        lead = {'user_id': uid, 'open': bool(open_items),
                'bought': any(t >= since for t in purchases.get(uid, []))}
        if open_items:
            main = max(open_items, key=lambda s: (STAGES[s['stage']], s['at']))
            lead['last'] = max(s['at'] for s in open_items)
            # Один товар — одна запись, по самому дальнему шагу; у курса видно и экран, и оплату
            seen, what = set(), []
            for s in sorted(open_items, key=lambda s: (-STAGES[s['stage']], -s['at'].timestamp())):
                key = s['what'] if s['product_id'] is None else s['product_id']
                if key not in seen:
                    seen.add(key)
                    what.append(s['what'])
            name = user.get('username')
            lead['cells'] = {
                'B': f'=HYPERLINK("https://t.me/{name}";"@{name}")' if name else '',
                'C': user.get('first_name') or '',
                'D': str(uid),
                'E': main['interest'],
                'F': '; '.join(what),
                'G': main['stage'],
                'H': _date(lead['last']),
                'I': _date(_utc(user['first_seen'])) if user.get('first_seen') else '',
            }
            camp = data['campaign'].get(uid)
            lead['status'], lead['note'] = NEW, ''
            if camp and camp['state'] == 'sent':
                lead['status'] = 'Ответили' if camp['answered'] else 'Написали'
                lead['note'] = ('05.11 бот отправил спеццену на курс до 07.11 (рассылка). '
                                'Переписка — в таблице «База возражений по курсу».')
            elif camp and camp['state'] == 'failed':
                lead['note'] = 'Рассылка по курсу 05.11 не дошла: Telegram не принял сообщение.'
        leads[uid] = lead
    return leads


def _shown(cells: dict) -> list[str]:
    """Как ячейки B–I выглядят в таблице — чтобы не переписывать то, что не поменялось."""
    b = cells['B']
    shown_b = b.rsplit(';"', 1)[1].rstrip('")') if b else ''
    return [shown_b] + [cells[c] for c in 'CDEFGHI']


def _row_cells(cells: dict) -> list[str]:
    return [cells['B'], _text(cells['C']), cells['D'], _text(cells['E']), _text(cells['F']),
            _text(cells['G']), cells['H'], cells['I']]


def plan(sheet: list[list[str]], leads: dict[int, dict], tracked: set[int]):
    """sheet — значения A2:N как их показывает таблица. Возвращает
    (правки для batch_update, кого запомнить, кого забыть, сколько строк не хватило)."""
    rows = [list(r) + [''] * (14 - len(r)) for r in sheet]
    by_id, empty = {}, []
    for i, r in enumerate(rows):
        n = i + 2
        if r[3].strip().isdigit():
            by_id.setdefault(int(r[3]), n)
        elif not any(c.strip() for c in r[1:]):
            empty.append(n)
    empty += list(range(len(rows) + 2, LAST_ROW + 1))

    updates, remember, forget = [], [], []
    # Новые — свежие сверху: при первой заливке список сразу по давности
    for uid, lead in sorted(leads.items(), key=lambda kv: -kv[1]['last'].timestamp()
                            if 'last' in kv[1] else 0):
        n = by_id.get(uid)
        if n:
            r = rows[n - 2]
            if lead['open'] and r[1:9] != _shown(lead['cells']):
                updates.append({'range': f'B{n}:I{n}', 'values': [_row_cells(lead['cells'])]})
            elif not lead['open'] and lead['bought'] and r[9] != BOUGHT:
                updates.append({'range': f'J{n}', 'values': [[BOUGHT]]})
            if uid not in tracked:
                remember.append(uid)   # Даня внёс сам — теперь тоже помним
        elif lead['open'] and uid not in tracked:
            if not empty:
                continue
            n = empty.pop(0)
            updates.append({'range': f'B{n}:J{n}',
                            'values': [_row_cells(lead['cells']) + [_text(lead['status'])]]})
            if lead['note']:
                updates.append({'range': f'N{n}', 'values': [[_text(lead['note'])]]})
            remember.append(uid)
    # /forget: человека больше нет в базе — убираем его строку целиком
    for uid in tracked:
        n = by_id.get(uid)
        if uid not in leads and n:
            updates.append({'range': f'B{n}:N{n}', 'values': [[''] * 13]})
            forget.append(uid)
    missing = sum(1 for uid, l in leads.items()
                  if l['open'] and uid not in by_id and uid not in tracked and uid not in remember)
    return updates, remember, forget, missing


def _sheet():
    import gspread
    client = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    client.set_timeout((10, 60))
    return client.open_by_key(config.LEADS_SHEET_ID).worksheet(TAB)


def _publish(leads, tracked):
    ws = _sheet()
    current = ws.get(f'A2:N{LAST_ROW}')
    updates, remember, forget, missing = plan(current, leads, tracked)
    if updates:
        ws.batch_update(updates, value_input_option='USER_ENTERED')
    return len(updates), remember, forget, missing


async def sync_once():
    data = await collect()
    leads = build(data)
    # Кто из добавленных не попал в leads — удалил данные (/forget): plan очистит строку
    written, remember, forget, missing = await asyncio.to_thread(
        _publish, leads, set(data['tracked']))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany('INSERT OR IGNORE INTO leads_sheet(user_id) VALUES (?)',
                             [(u,) for u in remember])
        await db.executemany('DELETE FROM leads_sheet WHERE user_id = ?', [(u,) for u in forget])
        await db.commit()
    if written or missing:
        logger.info('Заинтересованные: правок %d, новых %d, удалено %d, не хватило строк %d',
                    written, len([u for u in remember if u not in data['tracked']]), len(forget), missing)
    if missing:
        logger.warning('Заинтересованные: в таблице кончились строки до %d — добавьте строки с форматом', LAST_ROW)
    return written


async def leads_sync_loop():
    if not config.LEADS_SHEET_ID:
        return
    while True:
        try:
            await sync_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Заинтересованные: выгрузка в таблицу не удалась')
        await asyncio.sleep(PERIOD)
