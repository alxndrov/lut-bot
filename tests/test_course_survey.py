"""Опрос покупателей курса: группы, кнопки, ответ текстом, вопрос о публикации."""
import os
import tempfile
import unittest
from unittest import mock

import aiosqlite

import database as db
from handlers import course_survey as cs


def _user(uid=5, name="Аня <3"):
    return mock.Mock(id=uid, first_name=name, username="anya")


def _msg(text, uid=5, content_type="text"):
    m = mock.AsyncMock()
    m.text, m.caption, m.content_type = text, None, content_type
    m.from_user = _user(uid)
    return m


def _cb(data, uid=5):
    c = mock.AsyncMock()
    c.data = data
    c.from_user = _user(uid)
    c.message = mock.AsyncMock()
    return c


class SurveyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        await db.upsert_user(5, "anya", "Аня <3")
        self.real_show = cs._show
        self.show = mock.patch.object(cs, "_show", mock.AsyncMock()).start()
        self.bot = mock.AsyncMock()

    def _card(self):
        """Карточка, которую увидели бы админы после последнего события."""
        s, who = self.show.await_args.args[:2]
        return cs.render_card(s, who)

    async def asyncTearDown(self):
        mock.patch.stopall()
        db.DB_PATH = self.old
        self.tmp.cleanup()

    async def _send(self, grp):
        return await cs.send(self.bot, 5, grp)

    def test_groups(self):
        self.assertEqual([cs.group_for(n) for n in (0, 1, 3, 4, 12)], [3, 2, 2, 1, 1])

    async def test_intro_escapes_name_and_has_buttons(self):
        sid = await self._send(1)
        text = self.bot.send_message.await_args.args[1]
        self.assertIn("Привет, Аня &lt;3! Это Миша", text)
        kb = self.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual([b.text for b in kb[0] + kb[1]], [str(n) for n in range(1, 11)])
        self.assertEqual(kb[0][0].callback_data, f"svy:{sid}:r:1")

    async def test_group1_button_then_text_then_publish(self):
        sid = await self._send(1)
        cb = _cb(f"svy:{sid}:r:9")
        await cs.on_button(cb)
        self.assertEqual(cb.message.answer.await_args.args[0], cs.RATED_HIGH)
        msg = _msg("Всё зашло, не хватило цвета")
        self.assertTrue(await cs.on_reply(msg))
        texts = [c.args[0] for c in msg.answer.await_args_list]
        self.assertIn("Спасибо, Аня &lt;3! 🙏 Это правда ценно.", texts[0])
        self.assertEqual(texts[1], cs.PUBLISH_Q)
        s = await cs._get(sid)
        self.assertEqual((s["rating"], s["stage"], s["answer"]), (9, "publish", "Всё зашло, не хватило цвета"))
        # следующее сообщение — уже не опрос, уходит обычной поддержке
        self.assertFalse(await cs.on_reply(_msg("и ещё")))
        cb = _cb(f"svy:{sid}:p:0")
        await cs.on_button(cb)
        self.assertEqual(cb.message.answer.await_args.args[0], cs.PUBLISH_NO)
        s = await cs._get(sid)
        self.assertEqual((s["publish"], s["stage"]), (0, "done"))

    async def test_one_card_collects_everything(self):
        sid = await self._send(2)
        await cs.on_button(_cb(f"svy:{sid}:c:broken"))
        self.assertIn("❓ Что мешает продолжить?\n→ 🚨 <b>Что-то не работает</b>", self._card())
        await cs.on_reply(_msg("Видео <не> грузится"))
        card = self._card()
        self.assertIn("→ 🚨 <b>Что-то не работает</b>", card)
        self.assertIn("❓ <b>Что не работает и с какого устройства?</b>\n💬 Видео &lt;не&gt; грузится", card)
        # группа 1: оценка 9, текст, «Нет» — одна карточка, публикация тихой правкой
        sid = await self._send(1)
        await cs.on_button(_cb(f"svy:{sid}:r:9"))
        await cs.on_reply(_msg("Лутов"))
        card = self._card()
        self.assertIn("→ ⭐️ <b>9</b> из 10", card)
        self.assertIn("❓ <b>Что зашло больше всего и чего не хватило?</b>\n💬 Лутов", card)
        self.assertIn("→ ⏳ пока не ответил(а)", card)
        await cs.on_button(_cb(f"svy:{sid}:p:0"))
        self.assertEqual(self.show.await_args.kwargs.get("resend"), False)
        self.assertIn("❓ Можно указать имя при публикации отзыва?\n→ 🙈 Нет, без имени", self._card())
        # сразу текстом с оценкой — ответ на первый вопрос
        sid = await self._send(1)
        await cs.on_reply(_msg("8/10, всё супер"))
        card = self._card()
        self.assertIn("❓ <b>Как тебе курс? Оценка 1–10 и что зашло, чего не хватило</b>", card)
        self.assertIn("Оценка в тексте: <b>8</b>", card)
        self.assertNotIn("→ ⭐️", card)

    async def test_show_resends_and_deletes_old_then_edits(self):
        sid = await self._send(2)
        s = await cs._get(sid)
        s.update(reason="time")
        fake = mock.AsyncMock()
        fake.send_message.side_effect = [mock.Mock(message_id=m) for m in (100, 200, 300, 400)]
        with mock.patch.object(cs, "Bot", return_value=fake), \
             mock.patch.object(cs.config, "ADMIN_IDS", [11, 22]), \
             mock.patch.object(cs.db, "add_support_message", mock.AsyncMock()) as reg:
            await self.real_show(s, "who")
            self.assertEqual(await cs._card_ids(sid), {11: 100, 22: 200})
            fake.delete_message.assert_not_awaited()
            await self.real_show(s, "who")                       # новое событие — заново
            self.assertEqual(await cs._card_ids(sid), {11: 300, 22: 400})
            self.assertEqual([c.args for c in fake.delete_message.await_args_list],
                             [(11, 100), (22, 200)])
            await self.real_show(s, "who", resend=False)          # тихая правка
            self.assertEqual(fake.edit_message_text.await_count, 2)
            self.assertEqual(fake.send_message.await_count, 4)
            self.assertEqual(reg.await_count, 4)            # Reply на карточку → клиенту

    async def test_low_rating_reply(self):
        sid = await self._send(1)
        cb = _cb(f"svy:{sid}:r:5")
        await cs.on_button(cb)
        self.assertEqual(cb.message.answer.await_args.args[0], cs.RATED_LOW)

    async def test_rating_as_text(self):
        sid = await self._send(1)
        msg = _msg("8")
        self.assertTrue(await cs.on_reply(msg))
        self.assertEqual(msg.answer.await_args.args[0], cs.RATED_HIGH)
        self.assertEqual((await cs._get(sid))["stage"], "await")
        self.assertTrue(await cs.on_reply(_msg("Круто, 9/10, но монтаж сложный")))
        s = await cs._get(sid)
        self.assertEqual(s["rating"], 8)          # первая оценка не перетирается
        sid2 = await self._send(1)
        await cs.on_reply(_msg("Оценю на 7 из 10: мало про цвет"))
        self.assertEqual((await cs._get(sid2))["rating"], 7)

    async def test_group2_choice_then_text(self):
        sid = await self._send(2)
        cb = _cb(f"svy:{sid}:c:hard")
        await cs.on_button(cb)
        self.assertIn("На каком уроке стало сложно", cb.message.answer.await_args.args[0])
        msg = _msg("Урок про Premiere")
        self.assertTrue(await cs.on_reply(msg))
        self.assertIn("Получил, посмотрю", msg.answer.await_args.args[0])
        s = await cs._get(sid)
        self.assertEqual((s["reason"], s["stage"]), ("hard", "done"))

    async def test_group3_soon_closes(self):
        sid = await self._send(3)
        await cs.on_button(_cb(f"svy:{sid}:c:soon"))
        self.assertEqual((await cs._get(sid))["stage"], "done")
        self.assertFalse(await cs.on_reply(_msg("ок")))

    async def test_foreign_user_cannot_press(self):
        sid = await self._send(1)
        cb = _cb(f"svy:{sid}:r:10", uid=6)
        await cs.on_button(cb)
        cb.message.answer.assert_not_awaited()
        self.assertIsNone((await cs._get(sid))["rating"])

    async def test_test_run_goes_one_group_at_a_time(self):
        sid = await cs.send(self.bot, 5, 2, test=True)
        await cs.on_button(_cb(f"svy:{sid}:c:time"))
        msg = _msg("Работа")
        await cs.on_reply(msg)
        sent = [c.args[1] for c in msg.bot.send_message.await_args_list]
        self.assertIn("🧪 Дальше — группа 3", sent[0])
        self.assertIn("уроки курса у тебя пока не открыты", sent[1])
        # старые тестовые опросы закрываются, их кнопки больше не работают
        self.assertEqual(await cs.cancel_open_tests(5), 1)
        cb = _cb(f"svy:{sid + 1}:c:soon")
        await cs.on_button(cb)
        cb.answer.assert_awaited_with("Этот опрос уже закрыт.")
        cb.message.answer.assert_not_awaited()

    async def test_no_survey_no_catch(self):
        self.assertFalse(await cs.on_reply(_msg("привет")))

    async def test_audience(self):
        async with aiosqlite.connect(db.DB_PATH) as conn:
            for uid in (5, 6, 7):
                await conn.execute("INSERT INTO course_access (user_id, section) VALUES (?, 'shooting')", (uid,))
            await conn.execute("INSERT INTO course_access (user_id, section, test) VALUES (8, 'shooting', 1)")
            for n in range(1, 5):
                await conn.execute("INSERT INTO course_lesson_views (user_id, section, lesson) VALUES (5, 'shooting', ?)", (n,))
            await conn.execute("INSERT INTO course_lesson_views (user_id, section, lesson) VALUES (6, 'shooting', 1)")
            await conn.commit()
        g = await cs.audience()
        self.assertEqual({k: sorted(r["user_id"] for r in v) for k, v in g.items()},
                         {1: [5], 2: [6], 3: [7]})


if __name__ == "__main__":
    unittest.main()
