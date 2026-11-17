"""Переход по ссылке /pay/ — уведомление админам; превью мессенджеров не считаются."""
import os
import tempfile
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import config
import database as db
from handlers import pay_router
from services import paylinks

IPHONE_TG = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Mobile/15E148 Telegram-iOS")
TG_PREVIEW = "TelegramBot (like TwitterBot)"


class PayClickNoticeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        await db.upsert_user(294789073, "iamdariadrn", "daria")
        self.token = paylinks.make_token("p_294789073_10", 1693, "🎤 Microphone Handled Kit", "pd")

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def _req(self, ua, method="GET", ip="5.6.7.8"):
        app = web.Application()
        app["bot"] = mock.AsyncMock()
        app["bot_username"] = "malimabibot"
        req = make_mocked_request(method, f"/pay/{self.token}", app=app,
                                  match_info={"token": self.token},
                                  headers={"User-Agent": ua, "X-Forwarded-For": f"{ip}, 10.0.0.1"})
        return req

    async def _open(self, ua, **kw):
        notify = mock.AsyncMock()
        with mock.patch.object(pay_router, "_notify", notify), \
             mock.patch.object(config, "PRODAMUS_SHOP_URL_PHYSICAL", "https://shop.example"), \
             mock.patch.object(pay_router, "prodamus_url", return_value="https://prodamus/x"):
            try:
                resp = await pay_router.handle_pay(self._req(ua, **kw))
            except web.HTTPFound as e:
                resp = e
            for task in list(pay_router._bg):
                await task
        return resp, notify

    async def test_every_click_is_reported_with_device_and_ip(self):
        resp, notify = await self._open(IPHONE_TG)
        self.assertEqual(resp.location, "https://prodamus/x")
        text = notify.await_args.args[1]
        self.assertIn("Переход по ссылке на оплату</b> — 1-й", text)
        self.assertIn("🎤 Microphone Handled Kit — 1693 ₽ · Prodamus (зарубежная карта)", text)
        self.assertIn("Ссылка для @iamdariadrn", text)
        self.assertIn("iPhone · Telegram · IP 5.6.7.8", text)
        _, notify = await self._open(IPHONE_TG, ip="9.9.9.9")
        self.assertIn("— 2-й", notify.await_args.args[1])
        self.assertIn("IP 9.9.9.9", notify.await_args.args[1])

    async def test_messenger_preview_is_not_a_click(self):
        resp, notify = await self._open(TG_PREVIEW)
        self.assertEqual(resp.status, 200)          # страница-заглушка, без перехода в оплату
        notify.assert_not_awaited()
        self.assertIsNone(await db.get_pay_click("p_294789073_10"))

    def test_device(self):
        self.assertEqual(pay_router._device(IPHONE_TG), "iPhone · Telegram")
        self.assertEqual(pay_router._device(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"), "Windows · Chrome")


if __name__ == "__main__":
    unittest.main()
