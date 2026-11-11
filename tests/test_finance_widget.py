import asyncio
import json
import unittest
from unittest import mock

import config
from handlers import finance_widget as fw


SNAP = {"ts": "11.11 19:01", "p": [[1, 399, 17.95, 0, 365.09]] * 4,
        "s": {"since": "30.10", "n": 16, "nt": 46978.93, "ol": 39279.02, "pl": 9875.83},
        "c": {"oa": 6000.49, "cr": 4525.79, "tr": 6329, "pd": 0, "fr": -4854.3}}


class FinanceWidgetTest(unittest.IsolatedAsyncioTestCase):
    def test_data_fits_chat_data(self):
        self.assertLessEqual(len(fw._data(SNAP).encode()), 1024)
        huge = dict(SNAP, s=dict(SNAP["s"], pad="я" * 400))
        self.assertEqual(len(json.loads(fw._data(huge))["p"]), 3)   # «всё время» жертвуем

    async def test_open_sends_at_once_and_fills_later(self):
        calls = []

        async def api(bot, method, **params):
            calls.append((method, params))
            return {"message_id": 9}
        msg = mock.AsyncMock()
        msg.chat.id = msg.from_user.id = config.ADMIN_IDS[0]
        snap = mock.AsyncMock(return_value=SNAP)
        with mock.patch.object(fw, "api", api), mock.patch.object(fw, "snapshot", snap), \
                mock.patch("database.get_admin_command_messages", mock.AsyncMock(return_value=[])), \
                mock.patch("database.clear_admin_command_messages", mock.AsyncMock()), \
                mock.patch("database.replace_admin_command_messages", mock.AsyncMock()):
            self.assertTrue(await fw.send(msg))
            self.assertEqual(calls[0][1]["chat_data"], "{}")       # без расчётов
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.assertEqual(calls[-1][0], "editMessageWidgetData")

    async def test_refresh_redraws_and_answers(self):
        calls = []

        async def api(bot, method, **params):
            calls.append((method, params))
            return True
        with mock.patch.object(fw, "api", api), \
                mock.patch.object(fw, "snapshot", mock.AsyncMock(return_value=SNAP)):
            await fw.handle_query(mock.Mock(), {
                "id": "q1", "from": {"id": config.ADMIN_IDS[0]}, "data": "fin:r",
                "chat": {"id": 1}, "message_id": 9})
        self.assertEqual(calls[0][0], "editMessageWidgetData")
        self.assertEqual(json.loads(calls[0][1]["chat_data"])["ts"], "11.11 19:01")
        self.assertEqual(json.loads(calls[1][1]["data"])["t"], "Обновлено")


if __name__ == "__main__":
    unittest.main()
