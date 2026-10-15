import os
import tempfile
import unittest

import aiosqlite

import database as db
from services import npd, npd_receipts


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class NpdReceiptsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "test.db")
        await db.init_db()
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute("INSERT INTO products(id, name, description, price, category) "
                               "VALUES (5, 'MALIMABI LUT', '-', 399, 'digital')")
            await conn.commit()
        self.calls = []
        self.fail = None
        self.orig = {n: getattr(npd, n) for n in ("create_receipt", "cancel_receipt", "print_url")}

        async def create(name, amount, when=None):
            if self.fail:
                raise self.fail
            self.calls.append(("create", name, amount))
            return f"uuid{len(self.calls)}"

        async def cancel(uuid, comment=npd.CANCEL_REFUND):
            self.calls.append(("cancel", uuid, comment))

        async def url(uuid):
            return f"https://lknpd/{uuid}"
        npd.create_receipt, npd.cancel_receipt, npd.print_url = create, cancel, url
        self.notes = []

        async def notify(text):
            self.notes.append(text)
        self.orig_notify = npd_receipts._notify_admins
        npd_receipts._notify_admins = notify

    async def asyncTearDown(self):
        for n, f in self.orig.items():
            setattr(npd, n, f)
        npd_receipts._notify_admins = self.orig_notify
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def test_issue_sends_link_once(self):
        bot = FakeBot()
        name = await npd_receipts.receipt_name("d", 5)
        self.assertEqual(name, "MALIMABI LUT")
        await npd_receipts.issue_for_payment(bot, "yk-1", 7, name, 399.0, "2026-10-14T09:00:00.000Z")
        await npd_receipts.issue_for_payment(bot, "yk-1", 7, name, 399.0, None)   # повтор вебхука
        self.assertEqual(self.calls, [("create", "MALIMABI LUT", 399.0)])
        self.assertIn("Чек об оплате", bot.sent[0][1])
        self.assertEqual((await db.get_npd_receipt("yk-1"))["status"], "issued")

    async def test_not_connected_then_retry(self):
        bot = FakeBot()
        self.fail = npd.NpdNotConnected("не подключён")
        await npd_receipts.issue_for_payment(bot, "yk-2", 7, "Курс", 4990.0, None)
        self.assertEqual((await db.get_npd_receipt("yk-2"))["status"], "failed")
        self.assertIn("/nalog", self.notes[0])
        self.fail = None
        done, left = await npd_receipts.retry_failed(bot)
        self.assertEqual((done, left), (1, 0))

    async def test_retry_after_timeout_adopts_existing_receipt(self):
        bot = FakeBot()
        self.fail = npd.NpdTimeout(npd.TIMEOUT_MSG)
        await npd_receipts.issue_for_payment(bot, "yk-5", 7, "Курс", 4990.0, "2026-10-15T07:10:57Z")
        self.fail = None

        async def incomes(since, until):   # налоговая чек всё-таки создала
            return [{"approvedReceiptUuid": "real1", "totalAmount": 4990, "name": "Курс",
                     "operationTime": "2026-10-15T10:10:57+03:00", "cancellationInfo": None}]
        orig = npd.list_incomes
        npd.list_incomes = incomes
        try:
            self.assertEqual(await npd_receipts.retry_failed(bot), (1, 0))
        finally:
            npd.list_incomes = orig
        self.assertEqual(self.calls, [])   # второй чек не создавали
        self.assertEqual((await db.get_npd_receipt("yk-5"))["receipt_uuid"], "real1")

    async def test_cancel_on_refund(self):
        bot = FakeBot()
        await npd_receipts.issue_for_payment(bot, "yk-3", 7, "Курс", 2990.0, None)
        self.assertTrue(await npd_receipts.cancel_for_refund("yk-3"))
        self.assertFalse(await npd_receipts.cancel_for_refund("yk-3"))   # уже отменён
        self.assertFalse(await npd_receipts.cancel_for_refund("yk-404"))
        self.assertEqual(self.calls[-1], ("cancel", "uuid1", "Возврат средств"))


class SaleMessageTest(NpdReceiptsTest):
    async def test_sale_message_gets_receipt_line(self):
        from unittest import mock
        edits = []

        class FakeAdminBot:
            def __init__(self, token):
                self.session = mock.AsyncMock()

            async def edit_message_text(self, text, chat_id, message_id, **kw):
                edits.append((chat_id, message_id, text))
        await db.add_sale_message("yk-9", 111, 5, "🎓 <b>Курс оплачен</b>")
        with mock.patch.object(npd_receipts, "Bot", FakeAdminBot):
            await npd_receipts.issue_for_payment(FakeBot(), "yk-9", 7, "Курс", 4990.0, None)
        self.assertEqual(edits[-1][:2], (111, 5))
        self.assertIn("Курс оплачен", edits[-1][2])
        self.assertIn("отправлен клиенту", edits[-1][2])

        class BlockedBot(FakeBot):
            async def send_message(self, chat_id, text, **kwargs):
                raise RuntimeError("bot was blocked by the user")
        await db.add_sale_message("yk-10", 111, 6, "💰 Новая покупка")
        with mock.patch.object(npd_receipts, "Bot", FakeAdminBot):
            await npd_receipts.issue_for_payment(BlockedBot(), "yk-10", 7, "LUT", 399.0, None)
        self.assertIn("не доставлен", edits[-1][2])


class ProdamusWatchTest(NpdReceiptsTest):
    async def test_missing_and_schedule(self):
        async with aiosqlite.connect(db.DB_PATH) as conn:
            for pid, amount, at in (("01a-1", 399, "2026-10-09 15:24:00"),   # чек есть
                                    ("01a-2", 999, "2026-10-09 10:16:00"),   # чека нет
                                    ("yk-3", 4990, "2026-10-09 11:00:00")):  # ЮKassa — не наше
                await conn.execute("INSERT INTO purchases(user_id, username, product_id, "
                                   "telegram_payment_id, amount, created_at) VALUES (7, 'u', 5, ?, ?, ?)",
                                   (pid, amount, at))
            await conn.commit()
        await db.set_setting(npd_receipts.S_PRODAMUS_FROM, "2026-10-08")

        async def incomes(since, until):
            return [{"totalAmount": 399, "operationTime": "2026-10-09T15:24:30+03:00",
                     "cancellationInfo": None}]
        orig = npd.list_incomes
        npd.list_incomes = incomes
        try:
            missing = await npd_receipts.prodamus_missing()
            self.assertEqual([g["payment_id"] for g in missing], ["01a-2"])
            await db.set_setting(npd_receipts.S_PRODAMUS_NEXT, "2099-01-01")
            self.assertIsNone(await npd_receipts.check_prodamus())   # ещё не пора
            self.assertEqual(len(await npd_receipts.check_prodamus(force=True)), 1)
            self.assertIn("999 ₽", self.notes[-1])
        finally:
            npd.list_incomes = orig


class PhoneTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(npd.normalize_phone("+7 (912) 345-67-89"), "79123456789")
        self.assertEqual(npd.normalize_phone("89123456789"), "79123456789")
        self.assertIsNone(npd.normalize_phone("12345"))


if __name__ == "__main__":
    unittest.main()
