import json
import os
import tempfile
import unittest
from unittest import mock

import aiosqlite

import config
import database as db
from handlers import order_widget


SUMMARY = ("🧾 <b>Новый заказ</b> <code>malimabi-store-007</code>\n\n👤 Вика @buyer\n"
           "🛍 🎤 Microphone Kit\n💵 1380 ₽\n\n"
           "<b>1. Модель вашего микрофона?\n\nЕсли нет — текстом</b>\n2 (DJI Mic Mini)\n"
           "<b>2. Выберите цвет для микрофона и напишите его номер</b>\n1 (черный)\n\n"
           "🚚 <b>Доставка:</b>\nСДЭК, пункт выдачи: 400120, Россия, Волгоградская область, "
           "Волгоград, ул. Елецкая, 21 (код ПВЗ: VLG32)\nПолучатель: Иван, +79990000000")


class OrderWidgetTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO products(id, name, description, price, category) "
                            "VALUES (1, 'Ручка', 't', 1000, 'physical')")
            await c.execute("INSERT INTO users(user_id, username, first_name) VALUES (10, 'buyer', 'Вика')")
            await c.commit()
        self.oid = await db.create_order(10, 1, "pay-1", SUMMARY, rounds_json='[[{"q":"Цвет","text":"1"}]]',
                                         order_code="malimabi-store-007", round_products_json="[1]")
        self.calls = []

        async def api(bot, method, **params):
            self.calls.append((method, params))
            return {"message_id": 500} if method == "sendWidget" else True
        for target in ("handlers.order_widget.api",):
            p = mock.patch(target, api)
            p.start(); self.addCleanup(p.stop)
        for target in ("handlers.order_actions.request_sync",
                       "handlers.order_actions.request_finance_printer_update"):
            p = mock.patch(target, mock.Mock())
            p.start(); self.addCleanup(p.stop)

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def test_static_parts_from_summary(self):
        order = {"id": 1, "order_code": "malimabi-store-007", "summary": SUMMARY,
                 "product_name": "🎤 Microphone Kit", "username": "buyer", "product_id": 1,
                 "rounds_json": "[[]]", "cdek_number": "123"}
        s = order_widget._static(order)
        self.assertEqual(s["n"], "007")
        self.assertEqual(s["m"], "1380 ₽")
        self.assertEqual(s["q"], [["Модель вашего микрофона", "2 (DJI Mic Mini)"],
                                  ["Цвет для микрофона", "1 (черный)"]])
        self.assertEqual(s["d"], "СДЭК, ПВЗ Волгоград, ул. Елецкая, 21")
        self.assertEqual(s["r"], "Иван, +79990000000")

    async def test_send_and_tap_marks_printed_and_redraws(self):
        admin = config.ADMIN_IDS[0]
        msg = mock.AsyncMock()
        msg.from_user.id = admin
        msg.chat.id = admin
        await order_widget.cmd_worders(msg)
        method, params = self.calls[0]
        self.assertEqual(method, "sendWidget")
        self.assertTrue(params["allow_queries"])
        self.assertEqual(params["chat_data"], f"{self.oid}:0:0")
        self.calls.clear()

        bot = mock.AsyncMock()
        await order_widget.handle_query(bot, {
            "id": "q1", "from": {"id": admin, "is_bot": False, "first_name": "Даня",
                                 "username": "alxndrov"},
            "chat": {"id": admin, "type": "private"}, "message_id": 500,
            "data": f"p:{self.oid}:1"})

        order = await db.get_order(self.oid)
        self.assertTrue(order["printed_at"])
        edits = [p for m, p in self.calls if m == "editMessageWidgetData"]
        self.assertEqual(edits[-1]["chat_data"], f"{self.oid}:1:0")
        answer = [p for m, p in self.calls if m == "answerWidgetQuery"][0]
        self.assertEqual(answer["widget_query_id"], "q1")
        self.assertIn("Отмечено", json.loads(answer["data"])["t"])

    async def test_barcode_is_sent_as_reply_to_widget(self):
        admin = config.ADMIN_IDS[0]
        await db.set_order_barcode_file(self.oid, "FILE1")
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("UPDATE orders SET cdek_uuid = 'u1' WHERE id = ?", (self.oid,))
            await c.commit()
        self.assertEqual(order_widget._static(await db.get_order(self.oid))["b"], 1)
        bot = mock.AsyncMock()
        await order_widget.handle_query(bot, {
            "id": "q3", "from": {"id": admin, "is_bot": False, "first_name": "Даня"},
            "chat": {"id": admin, "type": "private"}, "message_id": 500,
            "data": f"b:{self.oid}"})
        args, kw = bot.send_document.await_args
        self.assertEqual(args[:2], (admin, "FILE1"))
        self.assertEqual(kw["reply_to_message_id"], 500)
        self.assertEqual(self.calls[-1][0], "answerWidgetQuery")

    async def test_stranger_is_refused(self):
        await order_widget.handle_query(mock.AsyncMock(), {
            "id": "q2", "from": {"id": 999, "is_bot": False, "first_name": "X"},
            "data": f"s:{self.oid}"})
        answer = json.loads(self.calls[-1][1]["data"])
        self.assertEqual(answer["a"], 1)
        self.assertIsNone((await db.get_order(self.oid))["shipped_at"])


if __name__ == "__main__":
    unittest.main()
