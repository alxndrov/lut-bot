import json
import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import aiosqlite
import database as db
from services import course_outreach as svc


class OutreachTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        self.patch.start()
        await db.init_db()
        await svc.init()
        await db.upsert_user(7, 'test', 'Test', None)
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute('INSERT INTO course_campaign(user_id,sheet_row,prices,first_at,state,sent_at) VALUES(?,?,?,?,?,?)',
                (7, 2, json.dumps({'3': 4990}), '2026-10-20 12:00:00', 'sent', '2026-11-05 10:00:00'))
            await c.commit()

    async def asyncTearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    async def test_offer_bound_to_user_price_kind_and_deadline(self):
        link = {'kind':'c', 'user_id':7, 'order_num':'c_7_3', 'amount':4990}
        with patch.object(svc, 'active', return_value=True):
            self.assertTrue(await svc.payment_allowed(link))
            for overrides in ({'amount':1}, {'user_id':8}, {'kind':'p'}):
                self.assertFalse(await svc.payment_allowed(dict(link, **overrides)))
        with patch.object(svc, 'active', return_value=False):
            self.assertFalse(await svc.payment_allowed(link))
        self.assertTrue(svc.active(svc.DEADLINE-timedelta(seconds=1)))
        self.assertFalse(svc.active(svc.DEADLINE))

    async def test_events_retry_and_purchase_discovery(self):
        await svc.event(7, 'reply', 'Question', 'reply:7:1')
        await svc.event(7, 'reply', 'Question', 'reply:7:1')
        await svc.event(8, 'reply', 'Not in campaign')
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute('INSERT INTO purchases(user_id,product_id,telegram_payment_id,amount,course_option_id,created_at) VALUES(7,1,?,4990,3,?)',
                ('paw-test', '2026-11-05 11:00:00'))
            await c.commit()
        with patch.object(svc, '_sync', side_effect=RuntimeError('timeout')):
            with self.assertRaises(RuntimeError):
                await svc.sync_once()
        with patch.object(svc, '_sync') as sync:
            self.assertEqual(await svc.sync_once(), 2)
            self.assertEqual([e['kind'] for e in sync.call_args.args[0]], ['reply','purchase'])
            self.assertEqual(await svc.sync_once(), 0)

    async def test_expired_offer_never_creates_provider_payment(self):
        from handlers import pay_router
        from services import paylinks
        req = type('Request', (), {'app':{'bot_username':'test'}, 'match_info':{
            'token':paylinks.make_token('c_7_3',4990,'Course','offer_paw')}})()
        with patch.object(svc, 'active', return_value=False), patch.object(pay_router, '_payanyway_url', AsyncMock()) as provider:
            response = await pay_router.handle_pay(req)
            self.assertEqual(response.status, 410)
            provider.assert_not_awaited()

    async def test_reply_topic_requires_evidence(self):
        def msg(text, reply=None):
            return SimpleNamespace(text=text, caption=None, from_user=SimpleNamespace(id=7),
                                   reply_to_message=reply)
        ctx = {'message_id': 10}
        shipped = {'shipped_at': '2026-10-24'}
        self.assertEqual(await svc.reply_topic(msg('тут оставить или чатик какой-то есть'), ctx), 'neutral')
        review = msg('чехол для микрофона в видео выглядит очень круто, спасибо')
        self.assertEqual(await svc.reply_topic(review, ctx, order=shipped), 'review')
        self.assertEqual(await svc.reply_topic(review, ctx), 'neutral')
        self.assertEqual(await svc.reply_topic(msg('Что входит в курс?'), ctx), 'course')
        self.assertEqual(await svc.reply_topic(msg('Хочу курс и чехол'), ctx, order=shipped), 'neutral')
        self.assertEqual(await svc.reply_topic(msg('дорого', SimpleNamespace(message_id=10)), ctx), 'course')
        self.assertEqual(await svc.reply_topic(msg('дорого'), ctx, explicit_course=True), 'course')
        await svc.remember_reply(7, 20, 'review')
        self.assertEqual(await svc.reply_topic(msg('спасибо', SimpleNamespace(message_id=20)),
                                               ctx, explicit_course=True), 'review')

    async def test_review_routed_and_saved_without_course_answer(self):
        from handlers import support
        message = SimpleNamespace(from_user=SimpleNamespace(id=7), text='Чехол для микрофона — кайф!',
            caption=None, reply_to_message=None, chat=SimpleNamespace(id=7), message_id=30,
            answer=AsyncMock())
        order = {'id': 139, 'product_id': 10, 'shipped_at': '2026-10-24'}
        with patch.object(support, 'order_hint', AsyncMock(return_value=(order, 'order'))), \
             patch.object(db, 'recent_review_push', AsyncMock(return_value=None)), \
             patch.object(db, 'pending_review_push', AsyncMock(return_value=None)), \
             patch.object(support, '_save_review', AsyncMock(return_value=12)) as save, \
             patch.object(support, 'client_header', side_effect=lambda m,title,hint: title), \
             patch.object(support, 'notify_admins', AsyncMock(return_value=True)) as notify, \
             patch.object(support.config, 'ADMIN_IDS', [svc.RECIPIENT]):
            await support._relay_course_reply(message, {'recipient_id':svc.RECIPIENT})
            save.assert_awaited_once_with(message, order, 10)
            self.assertIn('Отзыв о покупке', notify.call_args.args[1])
            self.assertEqual(notify.call_args.kwargs['recipient_ids'], [svc.RECIPIENT])
        async with aiosqlite.connect(db.DB_PATH) as c:
            rows = await (await c.execute('SELECT kind FROM course_campaign_events')).fetchall()
        self.assertEqual(rows, [('product_review',)])
