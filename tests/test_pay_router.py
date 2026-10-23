import os
import tempfile
import unittest
from unittest import mock

import aiosqlite
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import config
import database as db
from handlers import pay_router, prodamus_webhook
from services import paylinks, prodamus


class TokenTest(unittest.TestCase):
    def test_roundtrip_and_tamper(self):
        token = paylinks.make_token("c_7_3", 2990, "Съёмка 🎬", via="yk")
        link = paylinks.parse_token(token)
        self.assertEqual((link["order_num"], link["amount"], link["name"], link["via"],
                          link["kind"], link["user_id"]),
                         ("c_7_3", 2990, "Съёмка 🎬", "yk", "c", 7))
        body, sig = token.split(".")
        forged = paylinks.make_token("c_7_3", 1, "Съёмка 🎬", via="yk").split(".")[0]
        self.assertIsNone(paylinks.parse_token(f"{forged}.{sig}"))
        self.assertIsNone(paylinks.parse_token("garbage"))

    def test_build_payment_url_goes_through_our_domain(self):
        with mock.patch.object(config, "PAY_BASE_URL", "https://app.example"):
            url = prodamus.build_payment_url("https://shop", "Kit", 1351, 5, 10, "p", "s", "n")
        self.assertTrue(url.startswith("https://app.example/pay/"))
        link = paylinks.parse_token(url.rsplit("/", 1)[1])
        self.assertEqual((link["order_num"], link["amount"]), ("p_5_10", 1351))
        with mock.patch.object(config, "PAY_BASE_URL", ""):
            url = prodamus.build_payment_url("https://shop", "Kit", 1351, 5, 10, "p", "s", "n")
        self.assertTrue(url.startswith("https://shop/?do=pay"))

    def test_prodamus_url_picks_shop_by_order_type(self):
        with mock.patch.multiple(config, PRODAMUS_SHOP_URL="https://digital",
                                 PRODAMUS_SHOP_URL_PHYSICAL="https://physical"):
            self.assertTrue(prodamus.prodamus_url("p_1_10", 100, "Kit").startswith("https://physical/"))
            self.assertTrue(prodamus.prodamus_url("c_1_3", 100, "Kit").startswith("https://digital/"))


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "test.db")
        await db.init_db()

    async def asyncTearDown(self):
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def _age(self, order_num, minutes, column="clicked_at"):
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute(f"UPDATE pay_clicks SET {column} = datetime('now', ?) "
                               "WHERE order_num = ?", (f"-{minutes} minutes", order_num))
            await conn.commit()


class ClicksTest(_DbCase):
    async def test_backup_due_after_delay_once(self):
        await db.record_pay_click("c_7_3", 7, 2990, "Съёмка", "prodamus")
        self.assertEqual(await db.due_pay_backups(3), [])
        await self._age("c_7_3", 4)
        self.assertEqual([r["order_num"] for r in await db.due_pay_backups(3)], ["c_7_3"])
        self.assertTrue(await db.claim_pay_backup("c_7_3"))
        self.assertFalse(await db.claim_pay_backup("c_7_3"))
        self.assertEqual(await db.due_pay_backups(3), [])

    async def test_paid_or_yookassa_clicks_get_no_backup(self):
        await db.record_pay_click("c_7_3", 7, 2990, "Съёмка", "prodamus")
        await db.record_pay_click("c_8_3", 8, 2990, "Съёмка", "yookassa")
        await db.mark_pay_paid("c_7_3", "123")
        await self._age("c_7_3", 4)
        await self._age("c_8_3", 4)
        self.assertEqual(await db.due_pay_backups(3), [])

    async def test_new_click_after_payment_is_new_attempt(self):
        await db.record_pay_click("p_7_10", 7, 1351, "Kit", "prodamus")
        await db.claim_pay_backup("p_7_10")
        await db.mark_pay_paid("p_7_10", "123")
        await db.record_pay_click("p_7_10", 7, 1400, "Kit", "prodamus")
        click = await db.get_pay_click("p_7_10")
        self.assertIsNone(click["paid_at"])
        self.assertIsNone(click["backup_sent_at"])
        self.assertEqual((click["amount"], click["clicks"]), (1400, 2))


class DoublePaymentTest(_DbCase):
    async def test_second_payment_after_first_is_not_provisioned(self):
        bot = mock.AsyncMock()
        await db.record_pay_click("c_7_3", 7, 2990, "Съёмка", "prodamus")
        await self._age("c_7_3", 5)
        inner = mock.AsyncMock(return_value=True)
        notify = mock.AsyncMock()
        with mock.patch.object(prodamus_webhook, "_provision_payment", inner), \
             mock.patch.object(prodamus_webhook, "_send_notify", notify):
            self.assertTrue(await prodamus_webhook.provision_payment(bot, 7, 3, "c", "pr-1", 2990))
            self.assertIsNone(await prodamus_webhook.provision_payment(bot, 7, 3, "c", "yk-2", 2990))
        self.assertEqual(inner.await_count, 1)
        self.assertIn("оплачен дважды", notify.await_args.args[1])
        self.assertEqual((await db.get_pay_click("c_7_3"))["paid_payment_id"], "pr-1")

    async def test_without_our_link_works_as_before(self):
        inner = mock.AsyncMock(return_value=True)
        with mock.patch.object(prodamus_webhook, "_provision_payment", inner):
            await prodamus_webhook.provision_payment(mock.AsyncMock(), 7, 3, "c", "a", 1)
            await prodamus_webhook.provision_payment(mock.AsyncMock(), 7, 3, "c", "b", 1)
        self.assertEqual(inner.await_count, 2)


class PayRouteTest(_DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        app = web.Application()
        app["bot"] = mock.AsyncMock()
        pay_router.setup_web(app, "testbot")
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        for k, v in dict(PRODAMUS_SHOP_URL="https://digital", YOOKASSA_ENABLED=True,
                         YOOKASSA_SHOP_ID="1", YOOKASSA_SECRET_KEY="k",
                         PAYANYWAY_ENABLED=False).items():
            p = mock.patch.object(config, k, v)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.client.close()
        await super().asyncTearDown()

    async def _go(self, via=""):
        token = paylinks.make_token("c_7_3", 2990, "Съёмка", via)
        return await self.client.get(f"/pay/{token}", allow_redirects=False)

    async def test_prodamus_mode_redirects_and_records_click(self):
        r = await self._go()
        self.assertEqual(r.status, 302)
        self.assertTrue(r.headers["Location"].startswith("https://digital/?do=pay"))
        self.assertEqual((await db.get_pay_click("c_7_3"))["provider"], "prodamus")

    async def test_yookassa_mode_and_backup_link_create_one_payment(self):
        await db.set_setting(pay_router.PROVIDER_SETTING, "yookassa")
        create = mock.AsyncMock(return_value={
            "id": "yk1", "confirmation": {"confirmation_url": "https://yoomoney/pay/yk1"}})
        with mock.patch.object(pay_router.yookassa, "create_payment", create):
            r1 = await self._go()
            r2 = await self._go(via="yk")
        self.assertEqual(r1.headers["Location"], "https://yoomoney/pay/yk1")
        self.assertEqual(r2.headers["Location"], "https://yoomoney/pay/yk1")
        self.assertEqual(create.await_count, 1)   # повторный переход — тот же платёж
        self.assertEqual(create.await_args.args[:3], ("c", "c_7_3", 2990))
        self.assertEqual((await db.get_pay_click("c_7_3"))["provider"], "yookassa")

    async def test_foreign_card_link_goes_to_prodamus_even_in_yookassa_mode(self):
        await db.set_setting(pay_router.PROVIDER_SETTING, "yookassa")
        r = await self._go(via="pd")
        self.assertEqual(r.status, 302)
        self.assertTrue(r.headers["Location"].startswith("https://digital/?do=pay"))

    async def test_yookassa_failure_falls_back_to_prodamus(self):
        await db.set_setting(pay_router.PROVIDER_SETTING, "yookassa")
        create = mock.AsyncMock(side_effect=pay_router.yookassa.YooKassaError("401"))
        with mock.patch.object(pay_router.yookassa, "create_payment", create), \
             mock.patch.object(pay_router, "_alert", mock.AsyncMock()):
            r = await self._go()
        self.assertTrue(r.headers["Location"].startswith("https://digital/"))

    async def test_bad_token(self):
        r = await self.client.get("/pay/abc.def", allow_redirects=False)
        self.assertEqual(r.status, 400)

    async def test_succeeded_payment_is_provisioned_once(self):
        await db.add_yk_payment("yk1", "c_7_3", "c", 2990, "https://x")
        row = await db.get_yk_payment("yk1")
        paid = {"status": "succeeded", "amount": {"value": "2990.00"},
                "income_amount": {"value": "2885.35"}, "metadata": {"order_num": "c_7_3"}}
        provision = mock.AsyncMock(return_value=True)
        from services import npd_receipts
        issue = mock.AsyncMock()   # иначе тест шлёт настоящим админам «чек не пробит»
        with mock.patch.object(pay_router.yookassa, "get_payment", mock.AsyncMock(return_value=paid)), \
             mock.patch.object(prodamus_webhook, "provision_payment", provision), \
             mock.patch.object(npd_receipts, "issue_for_payment", issue):
            await pay_router.check_yk_payment(mock.AsyncMock(), row)
            await pay_router.check_yk_payment(mock.AsyncMock(), row)
        provision.assert_awaited_once()
        issue.assert_awaited_once()
        self.assertEqual(provision.await_args.args[1:], (7, 3, "c", "yk-yk1", 2990))
        self.assertEqual((await db.get_yk_payment("yk1"))["income"], 2885.35)
        self.assertEqual(await db.yk_payments_to_check(), [])


if __name__ == "__main__":
    unittest.main()


class PayAnyWaySignatureTest(unittest.TestCase):
    """Примеры из документации: https://docs.moneta.ru/assistant/v1/"""
    def test_doc_examples(self):
        import hashlib
        from services import payanyway
        with mock.patch.multiple(config, PAYANYWAY_MNT_ID="54600817", PAYANYWAY_SECRET="QWERTY",
                                 PAYANYWAY_TEST_MODE=False):
            url = payanyway.form_url("FF790ABCD", 120.25, "Тест")
            self.assertIn("MNT_SIGNATURE=" + hashlib.md5(b"54600817FF790ABCD120.25RUB0QWERTY").hexdigest(), url)
            self.assertIn(hashlib.md5(b"20054600817FF790ABCDQWERTY").hexdigest(),
                          payanyway.response_xml("FF790ABCD", 200))


class PayAnyWayFlowTest(_DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot = mock.AsyncMock()
        app = web.Application()
        app["bot"] = self.bot
        pay_router.setup_web(app, "testbot")
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        for k, v in dict(PAYANYWAY_MNT_ID="34903086", PAYANYWAY_SECRET="code", PAYANYWAY_ENABLED=True,
                         PAYANYWAY_TEST_MODE=False, PRODAMUS_SHOP_URL="https://digital").items():
            p = mock.patch.object(config, k, v)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.client.close()
        await super().asyncTearDown()

    def _notify_params(self, txn, amount, op="777"):
        import hashlib
        p = {"MNT_ID": "34903086", "MNT_TRANSACTION_ID": txn, "MNT_OPERATION_ID": op,
             "MNT_AMOUNT": amount, "MNT_CURRENCY_CODE": "RUB", "MNT_SUBSCRIBER_ID": "7",
             "MNT_TEST_MODE": "0"}
        p["MNT_SIGNATURE"] = hashlib.md5("".join([*p.values(), "code"]).encode()).hexdigest()
        return p

    async def test_link_notify_provision_once(self):
        token = paylinks.make_token("c_7_3", 2990, "Съёмка")
        r = await self.client.get(f"/pay/{token}", allow_redirects=False)
        self.assertEqual(r.status, 302)
        self.assertTrue(r.headers["Location"].startswith("https://www.payanyway.ru/assistant.htm?MNT_ID=34903086"))
        r2 = await self.client.get(f"/pay/{token}", allow_redirects=False)
        self.assertEqual(r.headers["Location"], r2.headers["Location"])   # тот же номер оплаты
        async with aiosqlite.connect(db.DB_PATH) as conn:
            txn = (await (await conn.execute("SELECT id FROM paw_payments")).fetchone())[0]
        provision = mock.AsyncMock(return_value=True)
        with mock.patch.object(prodamus_webhook, "provision_payment", provision), \
             mock.patch.object(prodamus_webhook, "_send_notify", mock.AsyncMock()):
            bad = dict(self._notify_params(txn, "2990.00"), MNT_SIGNATURE="00")
            self.assertEqual(await (await self.client.post("/payanyway/notify", data=bad)).text(), "FAIL")
            cheap = await self.client.post("/payanyway/notify", data=self._notify_params(txn, "1.00"))
            self.assertIn("<MNT_RESULT_CODE>200", await cheap.text())
            provision.assert_not_awaited()                                   # сумма не та
            for _ in range(2):                                               # повтор уведомления
                ok = await self.client.post("/payanyway/notify", data=self._notify_params(txn, "2990.00"))
                self.assertIn("<MNT_RESULT_CODE>200", await ok.text())
        provision.assert_awaited_once()
        self.assertEqual(provision.await_args.args[1:], (7, 3, "c", f"paw-{txn}", 2990))

    async def test_foreign_card_still_prodamus(self):
        token = paylinks.make_token("c_7_3", 2990, "Съёмка", via="pd")
        r = await self.client.get(f"/pay/{token}", allow_redirects=False)
        self.assertTrue(r.headers["Location"].startswith("https://digital/?do=pay"))
