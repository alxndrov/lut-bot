import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

import database as db
from services import cdek_accounts as accounts, cdek_tracker as tracker
from handlers import order_actions, prodamus_webhook as webhook


class CdekContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "test.db")
        await db.init_db()
        self.old = SimpleNamespace(get_order_info=AsyncMock(), get_barcode_pdf=AsyncMock(),
                                   find_order_by_number=AsyncMock(return_value=None),
                                   create_order=AsyncMock(return_value=None))
        self.new = SimpleNamespace(get_order_info=AsyncMock(), get_barcode_pdf=AsyncMock(),
                                   find_order_by_number=AsyncMock(return_value=None),
                                   create_order=AsyncMock(return_value=None))
        self.registry = patch.multiple(accounts, CURRENT_CONTRACT="new", OLD_CONTRACT="old",
                                       CDEK_CLIENT=self.new, OLD_CDEK_CLIENT=self.old)
        self.registry.start()

    async def asyncTearDown(self):
        self.registry.stop()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def make_order(self, contract=None, uuid=None):
        order_id = await db.create_order(1, 1, None, "test", order_code="test")
        if contract:
            await db.bind_order_cdek_contract(order_id, contract)
        if uuid:
            await db.set_order_cdek(order_id, uuid)
        return await db.get_order(order_id)

    async def test_migration_keeps_old_orders_and_restarts_do_not_rebind_new(self):
        old = await self.make_order(uuid="old-uuid")
        pending = await self.make_order()
        # Воссоздаём схему до перехода на два договора.
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute("ALTER TABLE orders DROP COLUMN cdek_contract")
            await conn.commit()
        await db.init_db(existing_cdek_contract="old")
        for order in (old, pending):
            self.assertEqual((await db.get_order(order["id"]))["cdek_contract"], "old")
        new = await self.make_order()
        await db.init_db(existing_cdek_contract="old")
        self.assertIsNone((await db.get_order(new["id"]))["cdek_contract"])
        bound = await db.bind_order_cdek_contract(new["id"], "new")
        self.assertIs(accounts.client_for_order(bound), self.new)
        rebound = await db.bind_order_cdek_contract(old["id"], "new")
        self.assertIs(accounts.client_for_order(rebound), self.old)

    async def test_unknown_or_unbound_shipment_never_uses_new_contract(self):
        with self.assertLogs(accounts.logger, level="ERROR"):
            self.assertIsNone(accounts.client_for_order({"cdek_contract": "missing"}))
            self.assertIsNone(accounts.client_for_order({"cdek_uuid": "orphan"}))
        with patch.object(accounts, "OLD_CDEK_CLIENT", None):
            self.assertIsNone(accounts.client_for_order({"cdek_contract": "old"}))

    async def test_tracker_queries_each_contract(self):
        old = await self.make_order("old", "old-uuid")
        new = await self.make_order("new", "new-uuid")
        self.old.get_order_info.return_value = {"status_codes": ["DELIVERED"]}
        self.new.get_order_info.return_value = {"status_codes": ["DELIVERED"]}
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(tracker.asyncio, "sleep", AsyncMock()):
            await tracker._check_once(bot)
        self.old.get_order_info.assert_awaited_once_with("old-uuid")
        self.new.get_order_info.assert_awaited_once_with("new-uuid")
        for order in (old, new):
            self.assertIsNotNone((await db.get_order(order["id"]))["arrived_notified_at"])
        bot.send_message.assert_not_awaited()

    async def test_old_barcode_uses_old_contract(self):
        order = await self.make_order("old", "old-uuid")
        self.old.get_barcode_pdf.return_value = b"pdf"
        callback = SimpleNamespace(message=SimpleNamespace(reply_document=AsyncMock(
            return_value=SimpleNamespace(document=SimpleNamespace(file_id="cached")))))
        await order_actions._deliver_barcode(callback, order, "order", "caption")
        self.old.get_barcode_pdf.assert_awaited_once()
        self.new.get_barcode_pdf.assert_not_awaited()
        self.assertEqual((await db.get_order(order["id"]))["cdek_barcode_file_id"], "cached")

    async def test_retry_after_timeout_keeps_original_contract(self):
        order = await self.make_order("old")
        pending = {"pvz_code": "MSK1", "recipient_name": "Test", "recipient_phone": "+79990000000"}
        result = await webhook._create_cdek_order(
            order["id"], pending, [1], {1: {"id": 1, "name": "Test", "price": 100}},
            "test", notify_fail=False, adopt_existing=True,
        )
        self.assertFalse(result)
        self.old.find_order_by_number.assert_awaited_once_with("test")
        self.old.create_order.assert_awaited_once()
        self.new.create_order.assert_not_awaited()
        self.assertEqual((await db.get_order(order["id"]))["cdek_contract"], "old")

    async def test_new_order_is_bound_before_network_request(self):
        order = await self.make_order()

        async def timeout(**kwargs):
            saved = await db.get_order(order["id"])
            self.assertEqual(saved["cdek_contract"], "new")
            return None

        self.new.create_order.side_effect = timeout
        result = await webhook._create_cdek_order(
            order["id"], {"pvz_code": "MSK1", "recipient_name": "Test", "recipient_phone": "123"},
            [1], {1: {"id": 1, "name": "Test", "price": 100}}, "test", notify_fail=False,
        )
        self.assertFalse(result)
        self.new.create_order.assert_awaited_once()
        self.old.create_order.assert_not_awaited()
