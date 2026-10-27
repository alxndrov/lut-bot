import os
import tempfile
import unittest
from unittest import mock

import aiosqlite

import database as db
from services import npd


class ManualSaleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO course_options (id, product_id, slug, title, sections, price, "
                            "price_full) VALUES (3, 1, 'bundle', 'Пакет', 'shooting,editing', 4990, 9990)")
            await c.commit()
        self.option = await db.get_course_option(3)

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    async def _one(self, sql, *args):
        async with aiosqlite.connect(db.DB_PATH) as c:
            return await (await c.execute(sql, args)).fetchone()

    async def test_sale_recorded_without_fee(self):
        sale = await db.add_manual_sale(1, self.option, 4990, "legal", "7700000000", "ООО Ромашка",
                                        "misha", "self", 1)
        code = f"b2b-{sale['id']}"
        self.assertEqual(await self._one("SELECT amount, user_id, course_option_id FROM purchases "
                                         "WHERE telegram_payment_id = ?", code), (4990, 0, 3))
        # без комиссии, в листе — «Перевод»
        self.assertEqual((await db.get_yk_fee_by_code())[code], 0)
        self.assertEqual((await db.get_payment_providers())[code], "Перевод")
        self.assertEqual(sale["wallet"], "misha")

    async def test_money_to_danya_is_plain_income(self):
        sale = await db.add_manual_sale(1, self.option, 4990, "person", None, None, "danya", "bot", 1)
        from handlers.finance import _pending_gross   # не ждём перевода от Prodamus
        from datetime import datetime, timezone
        self.assertEqual(await _pending_gross("1900-01-01 00:00:00", "2100-01-01 00:00:00",
                                              datetime.now(timezone.utc)), 0)

    async def test_physical_sale_without_order_reaches_cashflow(self):
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO products (id, name, description, price, category) "
                            "VALUES (2, 'Кейс', '-', 1699, 'physical')")
            await c.commit()
        sale = await db.add_manual_sale(2, None, 1699, "person", None, None, "danya", "self", 1)
        rows = {r["code"]: r for r in await db.get_cashflow_export_rows()}
        row = rows[f"b2b-{sale['id']}"]
        self.assertEqual((row["amount"], row["goods_type"]), (1699, "Физический"))


class LegalReceiptTest(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_from_legal_entity(self):
        sent = {}

        async def fake_post(path, payload, token=None, version="v1"):
            sent.update(payload)
            return {"approvedReceiptUuid": "u1"}
        with mock.patch.object(npd, "_post", fake_post), \
             mock.patch.object(npd, "_token", mock.AsyncMock(return_value="t")):
            self.assertEqual(await npd.create_receipt("Курс", 4990, account="misha",
                                                      client_inn="7700000000",
                                                      client_name="ООО Ромашка"), "u1")
        self.assertEqual(sent["client"], {"contactPhone": None, "displayName": "ООО Ромашка",
                                          "incomeType": "FROM_LEGAL_ENTITY", "inn": "7700000000"})
        self.assertEqual(sent["paymentType"], "ACCOUNT")


if __name__ == "__main__":
    unittest.main()
