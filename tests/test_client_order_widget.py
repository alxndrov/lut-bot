import os
import tempfile
import unittest
from unittest import mock

import aiosqlite

import database as db
from handlers import client_order_widget as cw
from handlers.order_widget import _fits

SUMMARY = ("🧾 <b>Новый заказ</b> <code>malimabi-store-77</code>\n\n💵 1380 ₽\n\n"
           "<b>1. Модель вашего микрофона?</b>\n2 (DJI Mic Mini)\n"
           "<b>2. Выберите цвет для микрофона и напишите его номер</b>\n1 (черный)\n\n"
           "🚚 <b>Доставка:</b>\nСДЭК, пункт выдачи: 400000, Россия, Волгоград, Волгоград, ул. Елецкая, 21 (код ПВЗ: X)\n"
           "Получатель: Шин Виктория, +7900")


class ClientOrderWidgetTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO products(id, name, description, price, category) "
                            "VALUES (10, '🎤 Microphone Handled Kit', '', 999, 'physical')")
            await c.execute("INSERT INTO orders(id, user_id, product_id, summary, order_code, "
                            "created_at, printed_at, shipped_at, cdek_number) VALUES "
                            "(1, 5, 10, ?, 'malimabi-store-77', '2026-11-08 10:00:00', "
                            "'2026-11-11 08:00:00', '2026-11-11 21:30:00', '10345883523')",
                            (SUMMARY,))
            await c.commit()

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    async def test_data_and_page(self):
        d = await cw.order_data(await db.get_order(1))
        self.assertEqual((d["n"], d["k"], d["h"]), ("77", 2, "В пути"))
        self.assertEqual(d["s"][2][1], "12.11")            # 21:30 UTC — уже 12-е по Москве
        self.assertEqual(len(d["o"]), 1)
        pos = d["o"][0]
        self.assertEqual(pos["p"], "Microphone Handled Kit")
        self.assertEqual(pos["q"][0][0], "Модель")
        self.assertEqual(pos["q"][1][3], 1)                # чёрный — кружком, текст белым
        self.assertTrue(d["d"].startswith("Волгоград, ул. Елецкая"))
        page = cw.build_html(d)
        self.assertNotIn("@font-face", page)               # Telegram его вырезает
        self.assertIn("<svg", page)
        self.assertIn("order_id=10345883523", page)
        self.assertTrue(_fits(page))

    async def test_positions_as_tabs(self):
        two = SUMMARY.replace("💵 1380 ₽\n\n", "💵 2242 ₽\n\n— <b>Позиция 1</b> —\n").replace(
            "1 (черный)\n\n", "1 (черный)\n\n— <b>Позиция 2</b> —\n"
            "<b>1. Модель вашего микрофона?</b>\n2 (DJI Mic Mini)\n"
            "<b>2. Выберите цвет для микрофона и напишите его номер</b>\n3 (красный)\n\n")
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("UPDATE orders SET summary = ? WHERE id = 1", (two,))
            await c.commit()
        d = await cw.order_data(await db.get_order(1))
        self.assertEqual([p["l"] for p in d["o"]], ["🎤 Черный", "🎤 Красный"])

    async def test_delivered_by_cdek(self):
        from services import cdek_tracker
        client = mock.Mock()
        client.get_order_info = mock.AsyncMock(return_value={
            "status_codes": ["DELIVERED", "ACCEPTED_AT_PICK_UP_POINT", "CREATED"],
            "status_dates": {"DELIVERED": "2026-11-13T14:05:00+0300"}})
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("UPDATE orders SET cdek_uuid = 'u' WHERE id = 1")
            await c.commit()
        bot = mock.Mock(send_message=mock.AsyncMock())
        with mock.patch("services.cdek_accounts.client_for_order", return_value=client), \
                mock.patch("database.get_orders_tracking",
                           mock.AsyncMock(return_value=[await db.get_order(1)])):
            await cdek_tracker._check_once(bot)
        bot.send_message.assert_not_awaited()              # уже забрали — о прибытии поздно
        o = await db.get_order(1)
        self.assertEqual((o["cdek_final"], o["delivered_at"]), ("DELIVERED", "2026-11-13 11:05:00"))
        d = await cw.order_data(o)
        self.assertEqual((d["k"], d["h"], d["s"][4][1], d["z"]), (4, "Получен", "13.11", 1))

    async def test_lifecycle_resend_and_review(self):
        """Прибытие: виджет приходит заново со звуком и сроком хранения, старый
        удаляется (или сворачивается); получение — отзыв через 2 дня."""
        from services import cdek_tracker
        calls = []

        async def api(bot, method, **params):
            calls.append((method, params))
            if method == "deleteMessage":
                raise RuntimeError("message can't be deleted")
            return {"message_id": 100 + len(calls)}
        await db.set_setting(cw.SETTING, "all")
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("UPDATE orders SET cdek_uuid = 'u', shipped_at = NULL WHERE id = 1")
            await c.execute("UPDATE products SET review_push_delay = 432000 WHERE id = 10")
            await c.commit()
        bot = mock.Mock(send_message=mock.AsyncMock())
        with mock.patch("handlers.client_order_widget.api", api):
            self.assertTrue(await cw.sync(1, bot))                    # после оплаты/печати
            self.assertFalse(await cw.sync(1, bot))                   # этап тот же — молчим
            self.assertTrue(calls[0][1]["disable_notification"])      # «Готов» — без звука
            first = (await db.get_client_widget(1))["message_id"]

            client = mock.Mock(get_order_info=mock.AsyncMock(return_value={
                "status_codes": ["ACCEPTED_AT_PICK_UP_POINT"],
                "keep_free_until": "2026-11-19T20:59:59Z"}))
            with mock.patch("services.cdek_accounts.client_for_order", return_value=client), \
                    mock.patch("database.get_orders_tracking",
                               mock.AsyncMock(return_value=[await db.get_order(1)])):
                await cdek_tracker._check_once(bot)
            bot.send_message.assert_not_awaited()                     # текст не нужен — виджет
            send = [p for m, p in calls if m == "sendWidget"][-1]
            self.assertIsNone(send.get("disable_notification"))       # прибытие — со звуком
            self.assertIn("19.11", send["html"])
            edit = [p for m, p in calls if m == "editMessageWidgetData"][-1]
            self.assertEqual((edit["message_id"], edit["chat_data"]), (first, '{"x":1}'))

            client.get_order_info.return_value = {
                "status_codes": ["DELIVERED", "ACCEPTED_AT_PICK_UP_POINT"],
                "status_dates": {"DELIVERED": "2026-11-13T10:00:00+0000"}}
            with mock.patch("services.cdek_accounts.client_for_order", return_value=client), \
                    mock.patch("database.get_orders_tracking",
                               mock.AsyncMock(return_value=[await db.get_order(1)])):
                await cdek_tracker._check_once(bot)
        self.assertEqual((await db.get_client_widget(1))["stage"], 4)
        async with aiosqlite.connect(db.DB_PATH) as c:
            rows = await (await c.execute(
                "SELECT product_id, send_at FROM review_push_queue WHERE order_id = 1")).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0][1], "2026-11-15 10:00:00")

    async def test_off_for_strangers_by_default(self):
        self.assertFalse(await cw.sync(1, mock.Mock()))              # по умолчанию — только админам

if __name__ == "__main__":
    unittest.main()
