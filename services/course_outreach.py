"""Persistent reply routing for course outreach sent by the main bot."""
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from html import escape

import aiosqlite
import config

import database as db

SHEET_ID = "1Fcq5ponkcpAcWW6h7VuKSDhbFmdrMKqaaAxuNPcKeHU"
CAMPAIGN = "course_20261105"
RECIPIENT = 140657458
DEADLINE = datetime(2026, 11, 8, tzinfo=timezone(timedelta(hours=3)))
LOG = logging.getLogger(__name__)
EVENTS = {"sent": "Отправлено", "failed": "Ошибка отправки", "offer": "Нажал «Купить по спеццене»",
          "option": "Выбрал раздел", "question": "Нажал «Есть вопрос»", "reply": "Ответил",
          "pay": "Перешёл к оплате", "purchase": "Купил курс",
          "neutral_reply": "Сообщение — тема не определена",
          "product_review": "Отзыв о физтоваре — не курс"}


async def remember_reply(user_id, message_id, topic):
    await db.set_setting(f'reply_topic:{user_id}:{message_id}', topic)


async def reply_topic(message, context, explicit_course=False, order=None):
    """A broadcast receipt alone is not evidence that a reply is about it."""
    import re
    replied = getattr(message, 'reply_to_message', None)
    if replied:
        topic = await db.get_setting(f'reply_topic:{message.from_user.id}:{replied.message_id}')
        if topic:
            return topic
        if replied.message_id == context.get('message_id'):
            return 'course'
    if explicit_course:
        return 'course'
    text = (message.text or message.caption or '').lower()
    # Only an explicit product + review wording + an actual shipped order is
    # enough to treat a free message as a product review. Other cases stay neutral.
    physical = re.search(r'микрофон|чехол|рукоятк|кейс|3d.?печат', text)
    course = re.search(r'курс|урок|обучени', text)
    praise = re.search(r'спасибо|понрав|круто|кайф|качеств|отзыв|довол', text)
    if order and order.get('shipped_at') and physical and praise and not course:
        return 'review'
    if course and not physical:
        return 'course'
    return 'neutral'


async def init():
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute('''CREATE TABLE IF NOT EXISTS course_campaign (
            user_id INTEGER PRIMARY KEY, sheet_row INTEGER NOT NULL, prices TEXT NOT NULL,
            first_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            sent_at TEXT, message_id INTEGER)''')
        await c.execute('''CREATE TABLE IF NOT EXISTS course_campaign_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, unique_key TEXT UNIQUE,
            synced INTEGER NOT NULL DEFAULT 0)''')
        await c.commit()


async def offer(user_id):
    async with aiosqlite.connect(db.DB_PATH) as c:
        c.row_factory = aiosqlite.Row
        row = await (await c.execute('SELECT * FROM course_campaign WHERE user_id=?', (user_id,))).fetchone()
    return dict(row) if row else None


async def event(user_id, kind, detail='', key=None):
    if not await offer(user_id):
        return
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute('INSERT OR IGNORE INTO course_campaign_events(user_id,kind,detail,unique_key) VALUES(?,?,?,?)',
                        (user_id, kind, str(detail), key))
        await c.commit()


def active(now=None):
    return (now or datetime.now(timezone.utc)) < DEADLINE


async def payment_allowed(link):
    if not active() or link['kind'] != 'c':
        return False
    row = await offer(link['user_id'])
    if not row or row['state'] != 'sent':
        return False
    option_id = link['order_num'].split('_')[2]
    return json.loads(row['prices']).get(option_id) == link['amount']


def keyboard():
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🎬 Купить по спеццене', callback_data='outreach:offer')],
        [InlineKeyboardButton(text='💬 Есть вопрос', callback_data='course:question')]])


async def options_screen(callback):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    row = await offer(callback.from_user.id)
    if not row or row['state'] != 'sent' or not active():
        await callback.answer('Предложение завершилось. Актуальные цены — в разделе «Курс».', show_alert=True)
        return
    if await db.get_user_course_sections(callback.from_user.id):
        await callback.answer('Курс уже приобретён. Откройте раздел «Курс».', show_alert=True)
        return
    await event(callback.from_user.id, 'offer')
    prices = json.loads(row['prices'])
    options = await db.get_course_options()
    buttons = [[InlineKeyboardButton(text=f"{o['title']} · {prices[str(o['id'])]} ₽",
                 callback_data=f"outreach:option:{o['id']}")] for o in options if str(o['id']) in prices]
    await callback.answer()
    sent = await callback.message.answer('Ваша цена до 7 ноября, 23:59 МСК. Выберите раздел или пакет:',
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await remember_reply(callback.from_user.id, sent.message_id, 'course')


async def option_screen(callback):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from services import paylinks
    row = await offer(callback.from_user.id)
    oid = callback.data.rsplit(':', 1)[1]
    price = json.loads(row['prices']).get(oid) if row else None
    if not row or row['state'] != 'sent' or not active() or price is None:
        await callback.answer('Предложение недоступно. Актуальные цены — в разделе «Курс».', show_alert=True)
        return
    if await db.get_user_course_sections(callback.from_user.id):
        await callback.answer('Курс уже приобретён.', show_alert=True)
        return
    option = await db.get_course_option(int(oid))
    await event(callback.from_user.id, 'option', option['title'])
    order = f"c_{callback.from_user.id}_{oid}"
    buttons = []
    if config.PAYANYWAY_ENABLED:
        buttons.append([InlineKeyboardButton(text='💳 Карта РФ или СБП',
            url=paylinks.link(order, price, option['title'], via='offer_paw'))])
    if config.LAVA_ENABLED:
        buttons.append([InlineKeyboardButton(text='🌍 Иностранный банк',
            url=paylinks.link(order, price, option['title'], via='offer_lava'))])
    await callback.answer()
    sent = await callback.message.answer(f"{escape(option['title'])} — <b>{price} ₽</b>.\nВыберите способ оплаты:",
                                  parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await remember_reply(callback.from_user.id, sent.message_id, 'course')


def _book():
    import gspread
    client = gspread.service_account(filename=config.GOOGLE_CREDENTIALS_FILE)
    client.set_timeout((10, 30))
    return client.open_by_key(SHEET_ID)


def _sync(events, people):
    book = _book()
    journal = book.worksheet('События')
    # Durable event IDs make retries safe if Sheets accepted a write before timeout.
    ids = set(journal.col_values(7)[1:])
    rows = []
    for e in events:
        p = people[e['user_id']]
        stamp = datetime.fromisoformat(e['created_at']).replace(tzinfo=timezone.utc).astimezone(DEADLINE.tzinfo)
        if str(e['id']) not in ids:
            rows.append([stamp.strftime('%d.%m.%Y %H:%M:%S'), str(e['user_id']),
                         '@'+p['username'] if p.get('username') else '', p.get('first_name') or '',
                         EVENTS[e['kind']], e['detail'], str(e['id'])])
    if rows:
        journal.append_rows(rows, value_input_option='RAW')
    # The six-column user layout stays intact. Update only campaign status/contact/replies.
    sheet = book.worksheet('Ответы')
    existing = sheet.get('D2:F501')
    updates = []
    for uid in {e['user_id'] for e in events}:
        p = people[uid]
        n = p['sheet_row']
        subset = [e for e in events if e['user_id'] == uid]
        updates.append({'range':f'D{n}', 'values':[[EVENTS[subset[-1]['kind']]]]})
        sent = next((e for e in subset if e['kind'] == 'sent'), None)
        if sent:
            updates.append({'range':f'E{n}', 'values':[[datetime.fromisoformat(sent['created_at']).replace(tzinfo=timezone.utc).astimezone(DEADLINE.tzinfo).strftime('%d.%m.%Y %H:%M')]]})
        replies = [e['detail'] for e in subset if e['kind'] == 'reply']
        if replies:
            old = existing[n-2][2] if n-2 < len(existing) and len(existing[n-2]) > 2 else ''
            for reply in replies:
                if reply not in old:
                    old += ('\n\n' if old else '') + reply
            updates.append({'range':f'F{n}', 'values':[[old]]})
    if updates:
        sheet.batch_update(updates, value_input_option='RAW')


async def sync_once():
    async with aiosqlite.connect(db.DB_PATH) as c:
        c.row_factory = aiosqlite.Row
        purchases = await (await c.execute('''SELECT p.* FROM purchases p JOIN course_campaign x ON x.user_id=p.user_id
            WHERE x.sent_at IS NOT NULL AND p.created_at>=x.sent_at AND p.course_option_id IS NOT NULL''')).fetchall()
        for p in purchases:
            await c.execute('INSERT OR IGNORE INTO course_campaign_events(user_id,kind,detail,created_at,unique_key) VALUES(?,?,?,?,?)',
                (p['user_id'],'purchase',f"{p['amount']} ₽; платёж {p['telegram_payment_id']}",p['created_at'],f"purchase:{p['id']}"))
        await c.commit()
        events = [dict(r) for r in await (await c.execute('SELECT * FROM course_campaign_events WHERE synced=0 ORDER BY id LIMIT 300')).fetchall()]
        people = {r['user_id']:dict(r) for r in await (await c.execute('SELECT x.*,u.username,u.first_name FROM course_campaign x LEFT JOIN users u ON u.user_id=x.user_id')).fetchall()}
    if events:
        await asyncio.to_thread(_sync, events, people)
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.executemany('UPDATE course_campaign_events SET synced=1 WHERE id=?',[(e['id'],) for e in events])
            await c.commit()
    return len(events)


async def sync_loop():
    while True:
        try:
            await sync_once()
        except Exception:
            LOG.exception('course outreach: sync failed; durable events retained for retry')
        await asyncio.sleep(20)


async def register(user_id: int, message_id: int, recipient_id: int):
    await db.set_setting(f"course_outreach:{user_id}", json.dumps({
        "message_id": message_id, "recipient_id": recipient_id,
    }))


async def context(user_id: int) -> dict | None:
    raw = await db.get_setting(f"course_outreach:{user_id}")
    return json.loads(raw) if raw else None


async def send(bot, user_id: int, recipient_id: int, text: str, reply_markup):
    # Register first: a fast reply must not arrive before routing is persisted.
    await register(user_id, 0, recipient_id)
    message = await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=reply_markup)
    await register(user_id, message.message_id, recipient_id)
    return message
