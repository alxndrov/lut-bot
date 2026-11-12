import os
import tempfile
import unittest

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
        self.assertEqual(d["p"], "Microphone Handled Kit")
        self.assertEqual(d["q"][0][0], "Модель")
        self.assertEqual(d["q"][1][3], 1)                  # чёрный — кружком, текст белым
        self.assertTrue(d["d"].startswith("Волгоград, ул. Елецкая"))
        page = cw.build_html(d)
        self.assertIn("@font-face", page)
        self.assertTrue(_fits(page))
        self.assertNotIn("@font-face", cw.build_html(d, with_font=False))


if __name__ == "__main__":
    unittest.main()
