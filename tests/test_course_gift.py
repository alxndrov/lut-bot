import os
import tempfile
import unittest
from unittest import mock

import aiosqlite

import config
import database as db
from handlers import course_gift


class CourseGiftTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        await db.upsert_user(user_id=1, username="buyer", first_name="Вика", ref=None)
        await db.upsert_user(user_id=2, username="friend", first_name="Аня", ref=None)
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO course_options (id, product_id, slug, title, sections, price, "
                            "price_full) VALUES (3, 1, 'bundle', 'Пакет', 'shooting,editing', 4990, 9990)")
            await c.execute("INSERT INTO purchases (id, user_id, product_id, amount, course_option_id, "
                            "telegram_payment_id) VALUES (218, 1, 1, 4990, 3, 'paw-x')")
            await c.commit()
        await db.grant_course_access(1, ["shooting", "editing"], 218)
        self.notes = []

        async def notify(bot, text, **kw):
            self.notes.append(text)
        p = mock.patch("handlers.prodamus_webhook._send_notify", notify)
        p.start(); self.addCleanup(p.stop)
        p = mock.patch("handlers.course.send_course", mock.AsyncMock())
        p.start(); self.addCleanup(p.stop)

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    async def _make_gift(self, args="@buyer"):
        msg = mock.AsyncMock()
        msg.from_user.id = config.ADMIN_IDS[0]
        with mock.patch.object(course_gift, "gift_link",
                               mock.AsyncMock(side_effect=lambda t: f"https://t.me/b?start=gift_{t}")):
            await course_gift.cmd_gift(msg, mock.Mock(args=args))
        text = msg.answer.await_args.args[0]
        return text.split("start=gift_")[1].split()[0]

    async def test_gift_moves_access_once(self):
        token = await self._make_gift()
        self.assertEqual(await db.get_user_course_sections(1), set())   # придержан
        self.assertEqual(token, await self._make_gift())                # повтор — та же ссылка
        friend = mock.AsyncMock()
        await course_gift.claim(friend, 2, token)
        self.assertEqual(await db.get_user_course_sections(2), {"shooting", "editing"})
        friend.bot.send_message.assert_awaited()                         # покупателю — «получен»
        self.assertTrue(any("Подарок получен" in n for n in self.notes))
        # второй раз по той же ссылке другой человек не получит
        stranger = mock.AsyncMock()
        await course_gift.claim(stranger, 5, token)
        self.assertIn("уже получен", stranger.answer.await_args.args[0])
        self.assertEqual(await db.get_user_course_sections(5), set())
        # покупка в учёте осталась за покупателем
        self.assertEqual((await db.last_course_purchase(1))["id"], 218)

    async def test_named_gift_only_for_that_username(self):
        token = await self._make_gift("@buyer @Friend")
        stranger = mock.AsyncMock()
        await course_gift.claim(stranger, 5, token)                      # чужой — не открывает
        self.assertIn("другому человеку", stranger.answer.await_args.args[0])
        self.assertEqual(await db.get_user_course_sections(5), set())
        await course_gift.claim(mock.AsyncMock(), 2, token)              # @friend — открывает
        self.assertEqual(await db.get_user_course_sections(2), {"shooting", "editing"})

    async def test_free_gift_from_admin(self):
        admin = config.ADMIN_IDS[0]
        await db.upsert_user(user_id=admin, username="misha", first_name="Миша", ref=None)
        self.assertEqual(course_gift.parse_username(" https://t.me/Friend "), "friend")
        self.assertIsNone(course_gift.parse_username("привет"))
        state = mock.AsyncMock()
        state.get_data.return_value = {"kind": "editing"}
        msg = mock.AsyncMock()
        with mock.patch.object(course_gift, "gift_link",
                               mock.AsyncMock(side_effect=lambda t: f"https://t.me/b?start=gift_{t}")):
            await course_gift._make_free_gift(msg, admin, state, "friend")
        card, gift_msg = [c.args[0] for c in msg.answer.await_args_list]
        self.assertIn("только для @friend", card)
        self.assertIn("монтаж", gift_msg)
        token = gift_msg.split("start=gift_")[1].split()[0]
        stranger = mock.AsyncMock()
        await course_gift.claim(stranger, 5, token)                      # чужой ник — нет
        self.assertEqual(await db.get_user_course_sections(5), set())
        friend = mock.AsyncMock()
        await course_gift.claim(friend, 2, token)
        self.assertEqual(await db.get_user_course_sections(2), {"editing"})
        friend.bot.send_message.assert_not_awaited()                     # «дарителю» не пишем
        self.assertTrue(any("Подарок от @misha получен" in n for n in self.notes))
        self.assertIn("подарили", friend.answer.await_args_list[0].args[0])
        gifts = await db.get_free_gifts()
        self.assertEqual([(g["claimed_by"], g["buyer_id"]) for g in gifts], [(2, admin)])
        # покупок и денег не появилось; получен — не отменить
        self.assertEqual((await db.last_course_purchase(2)), None)
        self.assertFalse(await db.delete_unclaimed_gift(token))

    async def test_free_gift_open_and_cancel(self):
        state = mock.AsyncMock()
        state.get_data.return_value = {"kind": "bundle"}
        msg = mock.AsyncMock()
        with mock.patch.object(course_gift, "gift_link",
                               mock.AsyncMock(side_effect=lambda t: f"https://t.me/b?start=gift_{t}")):
            await course_gift._make_free_gift(msg, config.ADMIN_IDS[0], state, None)
        card, gift_msg = [c.args[0] for c in msg.answer.await_args_list]
        self.assertIn("свободный", card)
        token = gift_msg.split("start=gift_")[1].split()[0]
        self.assertTrue(await db.delete_unclaimed_gift(token))
        late = mock.AsyncMock()
        await course_gift.claim(late, 2, token)
        self.assertIn("не работает", late.answer.await_args.args[0])
        self.assertEqual(await db.get_user_course_sections(2), set())


if __name__ == "__main__":
    unittest.main()
