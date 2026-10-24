import os
import tempfile
import unittest

import aiosqlite

import database as db


class ForgetTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        await db.upsert_user(user_id=7, username="anna", first_name="Анна", ref="src")
        await db.accept_policy(7)
        await db.set_user_email(7, "a@b.ru")
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO purchases (user_id, username, product_id, telegram_payment_id, "
                            "amount) VALUES (7, 'anna', 1, 'paw-1', 399)")
            await c.execute("INSERT INTO orders (user_id, product_id, summary, recipient_name, "
                            "recipient_phone, created_at, arrived_notified_at) VALUES "
                            "(7, 2, 'Рукоятка', 'Анна Иванова', '79990000000', "
                            "datetime('now', '-90 days'), NULL)")
            await c.execute("INSERT INTO reviews (user_id, username, first_name, product_id, text) "
                            "VALUES (7, 'anna', 'Анна', 1, 'супер')")
            await c.commit()

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    async def _one(self, sql):
        async with aiosqlite.connect(db.DB_PATH) as c:
            return await (await c.execute(sql)).fetchone()

    async def test_order_in_transit_blocks(self):
        self.assertEqual(await db.orders_in_progress(7), [])   # старый заказ — не мешает
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO orders (user_id, product_id, summary) VALUES (7, 2, 'новый')")
            await c.commit()
        self.assertEqual(len(await db.orders_in_progress(7)), 1)

    async def test_forget_keeps_only_purchases(self):
        await db.forget_user(7)
        self.assertEqual(await db.policy_status(7), "none")
        self.assertIsNone(await db.get_user_email(7))
        self.assertIsNone(await self._one("SELECT 1 FROM users WHERE user_id = 7"))
        self.assertIsNone(await self._one("SELECT 1 FROM reviews WHERE user_id = 7"))
        self.assertEqual(await self._one("SELECT recipient_name, recipient_phone FROM orders"),
                         (None, None))
        # покупка осталась для налогового учёта, но без username
        self.assertEqual(await self._one("SELECT amount, username FROM purchases"), (399, None))
        # вернулся и снова согласился — всё работает
        await db.accept_policy(7)
        self.assertEqual(await db.policy_status(7), "ok")


if __name__ == "__main__":
    unittest.main()
