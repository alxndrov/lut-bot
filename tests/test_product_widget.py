import json
import os
import tempfile
import unittest
from unittest import mock

import aiosqlite
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import config
import database as db
from handlers import brief_handler as bh
from handlers import product_widget as pw
from handlers.order_widget import _fits

QUESTIONS = [
    "Модель вашего микрофона?\n\nЕсли на картинке нет вашей модели — напишите текстом",
    "Выберите цвет для микрофона и напишите его номер\n(поп-фильтр только черный)",
    "Ручка — сделать ее с текстурой (1) или гладкой (2) ?",
    "Нужно ли добавить логотип?\nМы печатаем логотип на нижнем краю ручки (на дне)",
]


class ProductWidgetTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO products(id, name, description, price, category) "
                            "VALUES (10, '🎤 Microphone Handled Kit', '', 999, 'physical')")
            for i, t in enumerate(QUESTIONS, 1):
                await c.execute("INSERT INTO product_questions(product_id, position, text) "
                                "VALUES (10, ?, ?)", (i, t))
            await c.commit()
        self.calls = []

        async def api(bot, method, **params):
            self.calls.append((method, params))
            return {"message_id": 7}
        p = mock.patch("handlers.product_widget.api", api)
        p.start(); self.addCleanup(p.stop)

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def test_sections(self):
        kinds = [pw.section({"text": t})["t"] for t in QUESTIONS]
        self.assertEqual(kinds, ["Модель", "Цвет", "Ручка", "Логотип"])   # не «Ручка» у логотипа
        qs = [{"text": t} for t in QUESTIONS]
        self.assertEqual(pw.answers_from(qs, [1, 9, 1, 0]),
                         {0: "2", 1: "10", 2: "2", 3: "нет"})
        self.assertEqual(pw.answers_from(qs, [3, 0, 0, 1]), {1: "1", 2: "1"})  # «Другая», «С логотипом»
        self.assertIsNone(pw.answers_from(qs, [0, 0]))
        self.assertIsNone(pw.answers_from(qs, [0, 99, 0, 0]))

    async def test_widget_then_chat_for_logo(self):
        admin = config.ADMIN_IDS[0]
        storage = MemoryStorage()
        bot = mock.Mock(id=1)
        state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=admin, user_id=admin))
        target = mock.Mock(bot=bot, chat=mock.Mock(id=admin))
        target.answer = mock.AsyncMock()
        target.answer_photo = mock.AsyncMock()
        await bh.start_order_flow(target, state, await db.get_product(10))
        self.assertEqual(await state.get_state(), bh.SurveyForm.widget.state)
        page = self.calls[-1][1]["html"]
        self.assertTrue(_fits(page))

        q = {"id": "q", "from": {"id": admin, "is_bot": False, "first_name": "Д"},
             "chat": {"id": admin, "type": "private"}, "message_id": 7, "data": "sv:10:0.2.0.1"}
        with mock.patch("handlers.brief_handler._round_done", mock.AsyncMock()) as done, \
                mock.patch("handlers.brief_handler._ask_question", mock.AsyncMock()) as ask:
            await pw.handle_query(bot, q, storage)
            # Логотип файлом — спрашиваем в чате, остальное уже записано
            self.assertEqual(await state.get_state(), bh.SurveyForm.answering.state)
            data = await state.get_data()
            self.assertEqual([a["text"] for a in data["rounds"][-1]], ["1", "3", "1"])
            self.assertEqual(data["q_index"], 3)
            self.assertIn("логотип", ask.await_args.args[1]["text"])
            done.assert_not_awaited()
            answer = json.loads([p for m, p in self.calls if m == "answerWidgetQuery"][-1]["data"])
            self.assertIn("Готово", answer["t"])
            lock = [p for m, p in self.calls if m == "editMessageWidgetData"][-1]
            self.assertEqual(lock["chat_data"], '{"d":1}')

            # Повторное нажатие — опрос уже не ждёт виджет
            await pw.handle_query(bot, q, storage)
            answer = json.loads([p for m, p in self.calls if m == "answerWidgetQuery"][-1]["data"])
            self.assertEqual(answer["a"], 1)

    async def test_strangers_get_chat_questions(self):
        storage = MemoryStorage()
        state = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=555, user_id=555))
        target = mock.Mock(bot=mock.Mock(), chat=mock.Mock(id=555))
        target.answer = mock.AsyncMock()
        target.answer_photo = mock.AsyncMock()
        await bh.start_order_flow(target, state, await db.get_product(10))
        self.assertEqual(await state.get_state(), bh.SurveyForm.answering.state)
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
