"""One approved campaign; durable claim per recipient prevents accidental resend.

Run from repository root: venv/bin/python -m scripts.send_course_offer --send
An interrupted 'sending' row must be investigated manually before retrying.
"""
import asyncio
import json
import sys
from datetime import datetime, timezone

import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest

import config
import database as db
from services import course as course_svc
from services import course_outreach as svc


def text(prices, options, early):
    heading = ('Вернём для тебя цену первого знакомства с курсом 🎬' if early else
               'Твоя цена на курс действует до 7 ноября 🎬')
    intro = ('Ты интересовался курсом ещё до повышения цены. Если покупку отложил — сейчас можно присоединиться на прежних условиях:' if early else
             'Ты уже интересовался курсом. До 7 ноября включительно можно присоединиться по той стоимости, которую ты видел при первом знакомстве:')
    lines = []
    for o in options:
        p = prices[str(o['id'])]
        current = course_svc.price_for(o)
        suffix = f' вместо {current:,} ₽'.replace(',', ' ') if current > p else ''
        lines.append(f"<b>{o['title']} — {p:,} ₽</b>{suffix}".replace(',', ' '))
    return (f'<b>{heading}</b>\n\n{intro}\n\n' + '\n'.join(lines) +
        '\n\nВыбери то, что хочется прокачать: съёмку, монтаж или весь путь от первого кадра до готового ролика.\n\n'
        '<b>Спеццена действует до 7 ноября, 23:59 по Москве.</b> С 8 ноября откроются уроки и начнут действовать полные цены: '
        'съёмка — <b>4 990 ₽</b>, монтаж — <b>5 990 ₽</b>, пакет — <b>8 990 ₽</b>.\n\n'
        'Не уверен, какой раздел подойдёт? Нажми «Есть вопрос» или просто напиши сюда — поможем выбрать.')


async def main():
    if sys.argv[1:] != ['--send'] or not svc.active():
        raise RuntimeError('Use --send only for the approved campaign before its deadline')
    await svc.init()
    sheet_rows = await asyncio.to_thread(lambda: svc._book().worksheet('Ответы').get('A2:F501'))
    by_contact = {str(r[1]).lower().rstrip('/'): (i+2, r) for i,r in enumerate(sheet_rows) if len(r)>1}
    options = [o for o in await db.get_course_options() if o['slug'] in ('shooting','editing','bundle')]
    if len(options) != 3:
        raise RuntimeError('Unexpected course options; inspect before sending')
    async with aiosqlite.connect(db.DB_PATH) as c:
        c.row_factory=aiosqlite.Row
        leads = [dict(r) for r in await (await c.execute('''SELECT v.*,u.username FROM course_views v JOIN users u ON u.user_id=v.user_id
            WHERE NOT EXISTS(SELECT 1 FROM course_access a WHERE a.user_id=v.user_id AND a.revoked_at IS NULL)
            AND NOT EXISTS(SELECT 1 FROM purchases p WHERE p.user_id=v.user_id AND p.course_option_id IS NOT NULL)
            ORDER BY v.last_at DESC''')).fetchall()]
        for p in leads:
            if p['user_id'] in config.ADMIN_IDS or (p['username'] or '').lower()=='ncomonetasupport':
                continue
            key = f"https://t.me/{p['username']}".lower() if p['username'] else f"telegram id {p['user_id']}"
            if key not in by_contact:
                continue
            n, row = by_contact[key]
            if len(row)>3 and row[3].strip().lower() in ('не связываться','отписался'):
                continue
            first=datetime.fromisoformat(p['first_at']).replace(tzinfo=timezone.utc)
            prices={str(o['id']):course_svc.price_for(o,first) for o in options}
            await c.execute('INSERT OR IGNORE INTO course_campaign(user_id,sheet_row,prices,first_at) VALUES(?,?,?,?)',
                            (p['user_id'],n,json.dumps(prices),p['first_at']))
        await c.commit()
        targets=[dict(r) for r in await (await c.execute("SELECT * FROM course_campaign WHERE state='pending' ORDER BY sheet_row")).fetchall()]
    print(f'Pending recipients: {len(targets)}',flush=True)
    async with Bot(token=config.BOT_TOKEN) as bot:
        for p in targets:
            uid=p['user_id']
            # Recheck immediately before sending, since purchases may happen during the batch.
            if await db.get_user_course_sections(uid) or await db.get_user_course_paid(uid):
                async with aiosqlite.connect(db.DB_PATH) as c:
                    await c.execute("UPDATE course_campaign SET state='skipped_buyer' WHERE user_id=?",(uid,)); await c.commit()
                continue
            async with aiosqlite.connect(db.DB_PATH) as c:
                claimed=await c.execute("UPDATE course_campaign SET state='sending' WHERE user_id=? AND state='pending'",(uid,)); await c.commit()
                if not claimed.rowcount:
                    continue
            first=datetime.fromisoformat(p['first_at']).replace(tzinfo=timezone.utc)
            content=text(json.loads(p['prices']),options,course_svc.price_stage(first)=='presale')
            try:
                while True:
                    try:
                        sent=await svc.send(bot,uid,svc.RECIPIENT,content,svc.keyboard())
                        break
                    except TelegramRetryAfter as e:
                        await asyncio.sleep(e.retry_after+1)
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                async with aiosqlite.connect(db.DB_PATH) as c:
                    await c.execute("UPDATE course_campaign SET state='failed' WHERE user_id=?",(uid,)); await c.commit()
                await svc.event(uid,'failed',str(e),f'failed:{uid}')
                print(f'Failed recipient {uid}: {type(e).__name__}',flush=True)
                continue
            except Exception:
                # Ambiguous network failure: don't resend a message that Telegram may have accepted.
                await svc.event(uid,'failed','Неизвестен результат отправки; нужна ручная проверка',f'uncertain:{uid}')
                raise
            async with aiosqlite.connect(db.DB_PATH) as c:
                await c.execute("UPDATE course_campaign SET state='sent',sent_at=CURRENT_TIMESTAMP,message_id=? WHERE user_id=?",(sent.message_id,uid)); await c.commit()
            await svc.event(uid,'sent',f'Цены: {p["prices"]}; сообщение {sent.message_id}',f'sent:{uid}')
            print(f'Sent recipient {uid}',flush=True)
            await asyncio.sleep(.15)
    print('Campaign sending completed',flush=True)


if __name__ == '__main__':
    asyncio.run(main())
