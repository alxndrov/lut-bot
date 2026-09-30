import os
import tempfile
import unittest

import aiosqlite

import database as db
from handlers.order_actions import _replacement_summary


class OrderRepeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "test.db")
        await db.init_db()

        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO products(id, name, description, price, category) "
                "VALUES (1, 'Ручка', 'test', 1000, 'physical')"
            )
            await conn.execute(
                "INSERT INTO users(user_id, username, first_name) "
                "VALUES (10, 'buyer', 'Покупатель')"
            )
            await conn.commit()

        await db.add_purchase(
            10, "buyer", 1, "pay-1", 1300,
            delivery_amount=300, delivery_cost=250.0,
        )
        self.source_id = await db.create_order(
            10, 1, "pay-1",
            "🧾 <b>Новый заказ</b> <code>malimabi-store-001</code>\n"
            "💵 1300 ₽\n🚚 <b>Доставка:</b> ПВЗ",
            rounds_json='[[{"q":"Цвет","text":"1"}]]',
            order_code="malimabi-store-001",
            recipient_name="Иван Иванов", recipient_phone="+79990000000",
            pvz_code="MSK1", round_products_json="[1]",
        )
        await db.set_order_routing(self.source_id, ["@printer"])

    async def asyncTearDown(self):
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def test_repeat_is_not_a_purchase_and_charges_delivery_to_business(self):
        source = await db.get_order(self.source_id)
        cost = await db.get_order_delivery_cost(self.source_id)
        self.assertEqual(cost, 250.0)

        summary = _replacement_summary(source, "malimabi-store-002")
        repeat_id, created = await db.create_replacement_order(
            self.source_id, "malimabi-store-002", 99, "@admin", summary, cost,
        )
        self.assertTrue(created)

        repeat = await db.get_order(repeat_id)
        self.assertEqual(repeat["repeat_of_order_id"], self.source_id)
        self.assertEqual(repeat["recipient_phone"], "+79990000000")
        self.assertEqual(repeat["covered_delivery_cost"], 250.0)
        self.assertEqual(db.order_routing(repeat), ["@printer"])
        self.assertIsNone(await db.get_purchase_by_payment_id("repeat:malimabi-store-002"))
        self.assertIn("клиент повторно не платит", repeat["summary"])
        self.assertIn("возврат товара не требуется", repeat["summary"])

        totals = await db.get_period_revenue("2000-01-01", "2100-01-01")
        self.assertEqual(totals["count"], 1)
        self.assertEqual(totals["physical"], 1000)
        self.assertEqual(totals["delivery_cost"], 500.0)

        finance = {r["order_code"]: r for r in await db.get_orders_for_finance_export()}
        self.assertEqual(finance["malimabi-store-002"]["amount"], 0)
        self.assertEqual(finance["malimabi-store-002"]["delivery_cost"], 250.0)
        self.assertIn("Повтор", finance["malimabi-store-002"]["comment"])

        await db.set_order_cdek(repeat_id, "cdek-uuid", "track")
        accrued = await db.get_cdek_accrued("2000-01-01", "2100-01-01")
        self.assertEqual(accrued["count"], 1)
        self.assertEqual(accrued["cost"], 250.0)

    async def test_double_click_is_idempotent_but_repeat_can_be_repeated(self):
        first_id, created = await db.create_replacement_order(
            self.source_id, "malimabi-store-002", 99, "@admin", "repeat", 250,
        )
        self.assertTrue(created)
        same_id, created_again = await db.create_replacement_order(
            self.source_id, "malimabi-store-003", 99, "@admin", "repeat", 250,
        )
        self.assertFalse(created_again)
        self.assertEqual(same_id, first_id)

        second_id, second_created = await db.create_replacement_order(
            first_id, "malimabi-store-004", 99, "@admin", "repeat 2", 250,
        )
        self.assertTrue(second_created)
        self.assertNotEqual(second_id, first_id)


if __name__ == "__main__":
    unittest.main()
