"""Сообщения об открытии уроков: кому, когда и без дублей."""
import os
import tempfile
import unittest
from datetime import datetime

import config
import database as db
from handlers import course_open
from services import course as svc


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class CourseOpenTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        await db.grant_course_access(1, ["shooting", "editing"], None)
        await db.grant_course_access(2, ["editing"], None)
        await db.grant_course_access(3, ["shooting"], None, test=True)   # тест — не шлём

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def test_due_wave_windows(self):
        day = config.COURSE_OPEN_AT
        at = lambda h, m=0: datetime.fromisoformat(f"{day}T{h:02d}:{m:02d}").replace(tzinfo=svc.MSK)
        self.assertIsNone(course_open.due_wave(at(0) - (at(1) - at(0))))
        self.assertEqual(course_open.due_wave(at(0)), "open")
        self.assertEqual(course_open.due_wave(at(5, 59)), "open")
        self.assertIsNone(course_open.due_wave(at(6)))
        self.assertEqual(course_open.due_wave(at(10)), "morning")
        self.assertIsNone(course_open.due_wave(at(20)))

    async def test_waves_once_and_morning_only_not_started(self):
        bot = FakeBot()
        self.assertEqual(await course_open.send_wave(bot, "open"), (2, 0))
        self.assertEqual(sorted(u for u, _ in bot.sent), [1, 2])
        self.assertEqual(await course_open.send_wave(bot, "open"), (0, 0))     # без дублей
        self.assertIn("Оба раздела", dict(bot.sent)[1])
        self.assertNotIn("Пятый урок", dict(bot.sent)[2])                      # у монтажа нет

        await db.record_lesson_view(1, "shooting", 1)                          # 1-й уже смотрит
        bot.sent.clear()
        self.assertEqual(await course_open.send_wave(bot, "morning"), (1, 0))
        self.assertEqual([u for u, _ in bot.sent], [2])
        self.assertIn("знакомимся с Premiere", bot.sent[0][1])


if __name__ == "__main__":
    unittest.main()
