import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import aiosqlite

import config
import database as db
from handlers import course_offer
from scripts.make_course_offer import discounted, month_end
from services import course as svc, paylinks


class CourseOfferTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        await db.upsert_user(user_id=1, username="lesha", first_name="Лёша", ref=None)
        await db.upsert_user(user_id=2, username="other", first_name="Другой", ref=None)
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO course_options (id, product_id, slug, title, sections, price, "
                            "price_mid, price_full) VALUES (1, 1, 'shooting', 'Съёмка', 'shooting', "
                            "2990, 3990, 4990)")
            await c.commit()
        await db.create_course_offer("tok", 1, 4490, 1, "2026-11-30")
        p = mock.patch("handlers.course.send_course", mock.AsyncMock())
        self.send_course = p.start(); self.addCleanup(p.stop)

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def _at(self, s):
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=svc.MSK)

    async def test_only_owner_until_last_day(self):
        offer = await db.get_course_offer("tok")
        self.assertIsNone(await course_offer._check(offer, 1, self._at("2026-11-30 23:59")))
        self.assertIn("другого", await course_offer._check(offer, 2, self._at("2026-11-17 12:00")))
        self.assertIn("закончился", await course_offer._check(offer, 1, self._at("2026-12-01 00:00")))
        self.assertIn("не работает", await course_offer._check(None, 1))

    async def test_already_owned(self):
        await db.grant_course_access(1, ["shooting"], None)
        offer = await db.get_course_offer("tok")
        self.assertIn("уже есть", await course_offer._check(offer, 1, self._at("2026-11-20 12:00")))

    async def test_claim_shows_price_or_falls_back(self):
        msg = mock.AsyncMock()
        with mock.patch.object(svc, "now_msk", return_value=self._at("2026-11-20 12:00")):
            await course_offer.claim(msg, 1, "tok")
        text = msg.answer.await_args.args[0]
        self.assertIn("4 490 ₽", text)
        self.assertIn("<s>4 990 ₽</s>", text)
        kb = msg.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "course:offer:tok")
        stranger = mock.AsyncMock()
        await course_offer.claim(stranger, 2, "tok")
        self.assertIn("другого", stranger.answer.await_args.args[0])
        self.send_course.assert_awaited()

    async def test_pay_button_rechecks(self):
        cb = mock.AsyncMock()
        cb.data = "course:offer:tok"
        cb.from_user.id = 2
        await course_offer.cb_pay(cb)
        self.assertTrue(cb.answer.await_args.kwargs.get("show_alert"))
        cb.message.answer.assert_not_awaited()
        cb = mock.AsyncMock()
        cb.data = "course:offer:tok"
        cb.from_user.id = 1
        with mock.patch.object(svc, "now_msk", return_value=self._at("2026-11-20 12:00")), \
             mock.patch.object(config, "LAVA_ENABLED", False):
            await course_offer.cb_pay(cb)
        url = cb.message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url
        link = paylinks.parse_token(url.rsplit("/pay/", 1)[1])
        self.assertEqual((link["order_num"], link["amount"], link["via"]),
                         ("c_1_1", 4490, "offer:tok:paw"))

    async def _open(self, token_via="offer:tok:paw", amount=4490, order="c_1_1",
                    now="2026-11-20 12:00"):
        from aiohttp import web
        from handlers import pay_router
        link = paylinks.parse_token(paylinks.make_token(order, amount, "Съёмка", token_via))
        req = mock.Mock(app={"bot": mock.AsyncMock()})
        notify = mock.AsyncMock()
        with mock.patch.object(svc, "now_msk", return_value=self._at(now)), \
             mock.patch.object(config, "PAYANYWAY_ENABLED", True), \
             mock.patch.object(pay_router, "_notify", notify), \
             mock.patch.object(pay_router, "_payanyway_url",
                               mock.AsyncMock(return_value="https://paw/x")):
            try:
                resp = await pay_router._offer_pay(req, link, "malimabibot")
            except web.HTTPFound as e:
                return e.location, notify
        return resp.status, notify

    async def test_pay_link_checks_offer_and_records_click(self):
        where, notify = await self._open()
        self.assertEqual(where, "https://paw/x")
        self.assertIn("Открыл ссылку со скидкой", notify.await_args.args[1])
        self.assertEqual((await db.get_pay_click("c_1_1"))["amount"], 4490)
        where, notify = await self._open()                       # второй клик — без уведомления
        self.assertEqual(where, "https://paw/x")
        notify.assert_not_awaited()
        self.assertEqual((await self._open(now="2026-12-01 00:01"))[0], 410)   # срок вышел
        self.assertEqual((await self._open(amount=100))[0], 410)              # чужая сумма
        self.assertEqual((await self._open(order="c_2_1"))[0], 410)           # другой человек
        self.assertEqual((await self._open(token_via="offer:nope:paw"))[0], 410)

    def test_helpers(self):
        self.assertEqual(discounted(4990, 10), 4490)
        self.assertEqual(month_end(self._at("2026-11-17 12:00")), "2026-11-30")
        self.assertEqual(month_end(self._at("2027-02-03 12:00")), "2027-02-28")
