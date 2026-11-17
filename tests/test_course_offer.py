import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import aiosqlite

import database as db
from handlers import course_offer
from scripts.make_course_offer import discounted, month_end
from services import course as svc


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
             mock.patch("handlers.pay_router.payment_method_keyboard") as kb:
            await course_offer.cb_pay(cb)
        kb.assert_called_once_with("c_1_1", 4490, "Съёмка", "c")

    def test_helpers(self):
        self.assertEqual(discounted(4990, 10), 4490)
        self.assertEqual(month_end(self._at("2026-11-17 12:00")), "2026-11-30")
        self.assertEqual(month_end(self._at("2027-02-03 12:00")), "2027-02-28")
