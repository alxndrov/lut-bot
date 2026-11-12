import os
import tempfile
import unittest
from unittest import mock

import config
import database as db
from handlers import order_actions as oa


class ShipNotifyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        self.bot = mock.Mock()
        self.bot.send_message = mock.AsyncMock(return_value=mock.Mock(message_id=77))
        self.bot.delete_message = mock.AsyncMock()
        self.bot.session.close = mock.AsyncMock()
        p = mock.patch("handlers.order_actions.Bot", return_value=self.bot)
        p.start(); self.addCleanup(p.stop)

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def test_text(self):
        with mock.patch.object(config, "CDEK_AUTO_ORDER", True):
            t = oa.shipped_client_text({"order_code": "M-12", "cdek_number": "1234567890",
                                        "cdek_uuid": "u"})
        self.assertIn("Заказ M-12 отправлен", t)
        self.assertIn("order_id=1234567890", t)
        self.assertIn("пункт выдачи", t)
        bare = oa.shipped_client_text({"order_code": "M-13"})
        self.assertNotIn("Трек", bare)
        self.assertTrue(bare.endswith("отправлен!</b>"))

    async def test_send_and_retract(self):
        order = {"id": 5, "user_id": 900, "order_code": "M-5"}
        await oa._notify_client_shipped(order)
        self.assertEqual(self.bot.send_message.await_args.args[0], 900)
        await oa._retract_client_shipped(order)
        self.bot.delete_message.assert_awaited_with(900, 77)
        self.bot.delete_message.reset_mock()
        await oa._retract_client_shipped(order)       # второй раз удалять нечего
        self.bot.delete_message.assert_not_awaited()

    async def test_friend_and_pickup_skip(self):
        await oa._notify_client_shipped({"id": 6, "user_id": 1, "friend_by": 2})
        await oa._notify_client_shipped({"id": 7, "user_id": 1, "pickup": 1})
        self.bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
