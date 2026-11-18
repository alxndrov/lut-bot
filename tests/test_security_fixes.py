"""Экранирование имён в карточках, фильтр админ-бота, журнал мини-приложения."""
import asyncio
import unittest
from unittest import mock

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Update

import config
from handlers import course
from handlers.prodamus_webhook import _format_order
from services.admin_guard import AdminOnlyMiddleware


class FormatOrderEscapeTest(unittest.TestCase):
    def test_client_text_is_escaped(self):
        text = _format_order("Аня <3", "@anya", "Кит", 1000,
                             "СДЭК: ул. <b>Ленина</b> & Co", [[{"q": "Цвет", "text": "<i>розовый"}]],
                             "19.11.2026 10:00")
        self.assertIn("Аня &lt;3", text)
        self.assertIn("ул. &lt;b&gt;Ленина&lt;/b&gt; &amp; Co", text)
        self.assertIn("&lt;i&gt;розовый", text)
        self.assertNotIn("<i>", text)


def _update(uid: int) -> Update:
    return Update.model_validate({
        "update_id": 1,
        "message": {"message_id": 1, "date": 0, "text": "/finance",
                    "chat": {"id": uid, "type": "private"},
                    "from": {"id": uid, "is_bot": False, "first_name": "x"}},
    })


class AdminGuardTest(unittest.IsolatedAsyncioTestCase):
    async def _feed(self, uid: int) -> list:
        seen = []
        dp = Dispatcher()
        dp.update.outer_middleware(AdminOnlyMiddleware())
        r = Router()

        @r.message()
        async def any_message(message):
            seen.append(message.from_user.id)

        dp.include_router(r)
        bot = Bot(token="42:TEST")
        with mock.patch.object(config, "ADMIN_IDS", [111]):
            await dp.feed_update(bot, _update(uid))
        await bot.session.close()
        return seen

    async def test_admin_passes(self):
        self.assertEqual(await self._feed(111), [111])

    async def test_stranger_dropped(self):
        self.assertEqual(await self._feed(222), [])

    def test_widget_query_sender(self):
        from services.admin_guard import sender_id
        ev = mock.Mock(model_extra={"widget_query": {"from": {"id": 111}}})
        self.assertEqual(sender_id(ev, {}), 111)
        self.assertIsNone(sender_id(mock.Mock(model_extra={}), {}))


class MiniappLogTest(unittest.TestCase):
    def test_line_is_single_and_strips_init_data(self):
        raw = ('{"kind":"error","msg":"boom @ https://app.malimabi.com/app/#tgWebAppData=query_id%3DAAH'
               '&hash=abc\\n2026-11-19 [INFO] fake line","t":5,"evil":"x"}').encode()
        line = course._log_line(raw)
        self.assertNotIn("\n", line)
        self.assertNotIn("query_id", line)
        self.assertNotIn("evil", line)
        self.assertIn("kind=error", line)

    def test_not_json(self):
        self.assertIn("msg=hello", course._log_line(b"hello\r\nworld"))

    def test_rate_limit(self):
        course._log_hits.clear()
        ok = [course._log_allowed("1.2.3.4", 100.0 + i) for i in range(15)]
        self.assertEqual(ok.count(True), course._LOG_PER_IP)
        self.assertTrue(course._log_allowed("5.6.7.8", 120.0))
        self.assertTrue(course._log_allowed("1.2.3.4", 100.0 + course._LOG_WINDOW + 20))


if __name__ == "__main__":
    unittest.main()
