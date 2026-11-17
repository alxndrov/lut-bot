import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

import database as db
from services import cdek_tracker as tracker


class CdekActualCostTests(unittest.IsolatedAsyncioTestCase):
    """«Отложено на СДЭК» — по сумме, которую начислил сам СДЭК (17.11.2026:
    октябрьский счёт 5 144,45 сошёлся с API СДЭК копейка в копейку, а оценка
    бота с НДС давала 5 336,08)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "test.db")
        await db.init_db()

    async def asyncTearDown(self):
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def make_order(self, pay_id: str, rows: list[tuple[float, float]]) -> int:
        """Заказ с накладной и строками purchases (delivery_amount, delivery_cost) —
        у заказа из разных товаров строк несколько, доставка в первой."""
        order_id = await db.create_order(1, 10, pay_id, "test", order_code=pay_id)
        await db.set_order_cdek(order_id, f"uuid-{pay_id}")
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute("UPDATE orders SET shipped_at = '2026-10-20 10:00:00' WHERE id = ?",
                               (order_id,))
            for amount, cost in rows:
                await conn.execute(
                    "INSERT INTO purchases (user_id, product_id, telegram_payment_id, amount, "
                    "delivery_amount, delivery_cost, created_at) VALUES (1, 10, ?, 1000, ?, ?, "
                    "'2026-10-19 10:00:00')", (pay_id, amount, cost))
            await conn.commit()
        return order_id

    async def test_estimate_until_cdek_reports_then_actual(self):
        order_id = await self.make_order("p1", [(235, 216.67)])
        self.assertAlmostEqual((await db.get_cdek_accrued())["total"], 216.67)
        await db.set_order_cdek_cost(order_id, 202.49)
        accrued = await db.get_cdek_accrued("2026-10-01", "2026-10-31")
        self.assertAlmostEqual(accrued["total"], 202.49)
        self.assertEqual(accrued["count"], 1)

    async def test_mixed_order_counts_waybill_once(self):
        order_id = await self.make_order("p2", [(235, 216.67), (0, 0)])
        await db.set_order_cdek_cost(order_id, 358.12)   # с хранением 150 ₽
        accrued = await db.get_cdek_accrued()
        self.assertAlmostEqual(accrued["total"], 358.12)
        self.assertEqual(accrued["count"], 1)
        self.assertAlmostEqual((await db.get_cdek_account())["due"], 358.12)

    async def test_refresh_saves_changed_sum_only(self):
        order_id = await self.make_order("p3", [(235, 216.67)])
        client = SimpleNamespace(get_order_info=AsyncMock(return_value={"total_sum": 202.49}))
        with patch("services.cdek_accounts.client_for_order", return_value=client), \
             patch.object(tracker.asyncio, "sleep", AsyncMock()):
            self.assertEqual(await tracker.refresh_costs(None), 1)
            self.assertEqual(await tracker.refresh_costs(None), 0)
            client.get_order_info.return_value = {"total_sum": 352.49}
            self.assertEqual(await tracker.refresh_costs(None), 1)
        self.assertAlmostEqual((await db.get_order(order_id))["cdek_actual_cost"], 352.49)

    async def test_no_sum_from_cdek_keeps_estimate(self):
        await self.make_order("p4", [(235, 216.67)])
        client = SimpleNamespace(get_order_info=AsyncMock(return_value={"total_sum": None}))
        with patch("services.cdek_accounts.client_for_order", return_value=client), \
             patch.object(tracker.asyncio, "sleep", AsyncMock()):
            self.assertEqual(await tracker.refresh_costs(None), 0)
        self.assertAlmostEqual((await db.get_cdek_accrued())["total"], 216.67)

    async def test_return_waybill_counted_once(self):
        order_id = await self.make_order("p5", [(300, 275.51)])
        client = SimpleNamespace(
            get_order_info=AsyncMock(return_value={"total_sum": 275.51,
                                                   "status_codes": ["NOT_DELIVERED"]}),
            get_return_cost=AsyncMock(return_value={"total": 160.5}))
        with patch("services.cdek_accounts.client_for_order", return_value=client), \
             patch.object(tracker.asyncio, "sleep", AsyncMock()):
            await tracker.refresh_costs(None)
        self.assertAlmostEqual((await db.get_cdek_accrued())["total"], 436.01)
        # Если обратную доставку уже записали в возврат — второй раз не считаем
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute("INSERT INTO refunds (order_id, amount, paid, return_delivery) "
                               "VALUES (?, 1000, 1000, 160.5)", (order_id,))
            await conn.commit()
        self.assertAlmostEqual((await db.get_cdek_accrued())["total"], 436.01)

    async def test_act_adjustment_in_total_and_its_month(self):
        await self.make_order("p6", [(235, 216.67)])
        await db.add_cdek_adjustment("2026-10-25 09:00:00", 100.5, "акт сверки")
        self.assertAlmostEqual((await db.get_cdek_accrued())["total"], 317.17)
        self.assertAlmostEqual((await db.get_cdek_accrued("2026-10-25", "2026-10-25"))["total"], 100.5)
        months = {r["month"]: r["total"] for r in await db.get_cdek_accrued_by_month()}
        self.assertAlmostEqual(months["2026-10"], 317.17)
