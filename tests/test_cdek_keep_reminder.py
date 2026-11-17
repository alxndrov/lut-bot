import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

import database as db
from services import cdek_tracker as tracker


def _utc(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _cdek(stamp: str) -> str:
    return stamp.replace(" ", "T") + "+0000"


class KeepReminderTests(unittest.IsolatedAsyncioTestCase):
    """За 2 дня до конца хранения — напоминание; не забрали — «едет обратно»
    клиенту и админам, обратная накладная в учёт (Даня, 17.11.2026)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "test.db")
        await db.init_db()
        self.order_id = await db.create_order(5, 10, "pay", "x", order_code="malimabi-store-200")
        await db.set_order_cdek(self.order_id, "uuid-200")
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute("UPDATE orders SET arrived_notified_at = ? WHERE id = ?",
                               (_utc(-72), self.order_id))
            await conn.commit()
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.admins = AsyncMock()
        self.client = SimpleNamespace(get_order_info=AsyncMock(), get_return_cost=AsyncMock())
        self.patches = [patch("services.cdek_accounts.client_for_order", return_value=self.client),
                        patch.object(tracker, "_notify_admins", self.admins),
                        patch.object(tracker, "_after_final", AsyncMock()),
                        patch.object(tracker.asyncio, "sleep", AsyncMock())]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def info(self, keep_hours: float, codes=("ACCEPTED_AT_PICK_UP_POINT",)):
        return {"status_codes": list(codes), "status_dates": {},
                "keep_free_until": _cdek(_utc(keep_hours)), "total_sum": 202.49}

    async def test_reminds_once_two_days_before(self):
        self.client.get_order_info.return_value = self.info(30)
        await tracker._check_once(self.bot)
        await tracker._check_once(self.bot)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn("ждёт в пункте выдачи", self.bot.send_message.await_args.args[1])
        self.assertEqual(self.admins.await_count, 1)

    async def test_no_reminder_while_more_than_two_days_left(self):
        self.client.get_order_info.return_value = self.info(80)
        await tracker._check_once(self.bot)
        self.bot.send_message.assert_not_awaited()
        self.admins.assert_not_awaited()

    async def test_not_picked_up_notifies_and_counts_return(self):
        self.client.get_order_info.return_value = self.info(-5, codes=("NOT_DELIVERED",))
        self.client.get_return_cost.return_value = {"total": 160.5}
        await tracker._check_once(self.bot)
        self.assertIn("не забрали", self.bot.send_message.await_args.args[1])
        self.assertIn("160.50", self.admins.await_args.args[0])
        order = await db.get_order(self.order_id)
        self.assertEqual(order["cdek_final"], "NOT_DELIVERED")
        self.assertAlmostEqual(order["cdek_return_cost"], 160.5)
