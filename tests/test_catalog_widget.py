import json
import os
import tempfile
import unittest
from unittest import mock

import aiosqlite

import config
import database as db
from handlers import catalog_widget as cw
from handlers.order_widget import _fits


class CatalogWidgetTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO products(id, name, description, price, category) "
                            "VALUES (5, '🎤 Микрофон', 'Строка без точки\nВторая', 999, 'physical')")
            await c.execute("INSERT INTO products(id, name, description, price, category) "
                            "VALUES (6, 'LUT', 'Сочный лут.', 399, 'digital')")
            await c.commit()
        self.calls = []

        async def api(bot, method, **params):
            self.calls.append((method, params))
            return {"message_id": 1}
        p = mock.patch("handlers.catalog_widget.api", api)
        p.start(); self.addCleanup(p.stop)

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    async def test_items_and_modes(self):
        it = await cw._item(await db.get_product(5))
        self.assertEqual((it["e"], it["n"]), ("🎤", "Микрофон"))
        self.assertEqual(it["d"], "Строка без точки. Вторая.")
        self.assertEqual(it["b"], "🛒 Оформить заказ")
        lut = await cw._item(await db.get_product(6))
        self.assertEqual(lut["b"], "💳 Купить · 399 ₽")

        admin, buyer = config.ADMIN_IDS[0], 555
        self.assertTrue(await cw.send(mock.Mock(), admin, admin))     # по умолчанию — админам
        self.assertFalse(await cw.send(mock.Mock(), buyer, buyer))
        await db.set_setting(cw.SETTING, "all")
        self.assertTrue(await cw.send(mock.Mock(), buyer, buyer))
        params = self.calls[-1][1]
        self.assertTrue(params["allow_queries"])
        self.assertEqual(params["reply_markup"]["inline_keyboard"][-1][0]["callback_data"],
                         "info:sellers")

    def test_long_descriptions_still_fit(self):
        import hashlib
        items = [{"i": j, "e": "📦", "n": f"Товар {j}", "s": "", "p": "1 ₽", "g": "", "b": "Купить",
                  "o": 0, "h": 0, "x": "",
                  "d": " ".join(hashlib.sha256(bytes([j, k])).hexdigest() for k in range(4))}
                 for j in range(12)]
        page = cw.build_html(items, False)
        self.assertTrue(_fits(page))
        for j in range(12):
            self.assertIn(f"Товар {j}", page)

    async def test_tap_opens_product_via_cb_product(self):
        got = {}

        async def cb_product(tap, state):
            got["data"], got["msg"], got["state"] = tap.data, tap.message, state
            await tap.answer()
        q = {"id": "q1", "from": {"id": config.ADMIN_IDS[0], "is_bot": False, "first_name": "Д"},
             "chat": {"id": 42, "type": "private"}, "message_id": 7, "data": "pr:6"}
        with mock.patch("handlers.catalog.cb_product", cb_product):
            await cw.handle_query(mock.Mock(id=1), q, storage=mock.Mock())
        self.assertEqual(got["data"], "product:6")
        self.assertEqual((got["msg"].chat.id, got["msg"].message_id), (42, 7))
        self.assertTrue(got["msg"].photo)            # карточку пришлют новым сообщением
        self.assertEqual(got["state"].key.user_id, config.ADMIN_IDS[0])
        self.assertEqual(self.calls[-1][0], "answerWidgetQuery")

    async def test_tap_without_consent_asks_policy(self):
        q = {"id": "q2", "from": {"id": 777, "is_bot": False, "first_name": "К"},
             "chat": {"id": 777, "type": "private"}, "message_id": 7, "data": "pr:6"}
        ask = mock.AsyncMock()
        with mock.patch.object(config, "POLICY_REQUIRED", True), \
                mock.patch("handlers.start.ask_policy", ask), \
                mock.patch("handlers.catalog.cb_product", mock.AsyncMock()) as cb:
            await cw.handle_query(mock.Mock(id=1), q, storage=mock.Mock())
        ask.assert_awaited()
        cb.assert_not_awaited()
        self.assertIn("согласие", json.loads(self.calls[-1][1]["data"])["t"])


if __name__ == "__main__":
    unittest.main()
