"""Бесплатный заказ для блогера: приглашение, вход по нику, один товар, оформление."""
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
from handlers import blogger_order as bo
from handlers import brief_handler, delivery, prodamus_webhook

ADMIN = config.ADMIN_IDS[0] if config.ADMIN_IDS else 1


def _user(uid=5, username="Blogger_One", first_name="Аня"):
    return mock.Mock(id=uid, username=username, first_name=first_name)


def _msg(uid=ADMIN):
    m = mock.AsyncMock()
    m.from_user = _user(uid, "admin")
    return m


def _cb(data, uid=5):
    c = mock.AsyncMock()
    c.data = data
    c.from_user = _user(uid)
    c.message = mock.AsyncMock()
    return c


class BloggerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        await db.upsert_user(5, "Blogger_One", "Аня")
        self.notify = mock.patch.object(bo, "_notify", mock.AsyncMock()).start()
        mock.patch.object(bo, "invite_link",
                          mock.AsyncMock(side_effect=lambda t: f"https://t.me/b?start=blog_{t}")).start()
        mock.patch.object(config, "ADMIN_IDS", [ADMIN]).start()
        self.state = FSMContext(storage=MemoryStorage(),
                                key=StorageKey(bot_id=1, chat_id=5, user_id=5))

    async def asyncTearDown(self):
        mock.patch.stopall()
        db.DB_PATH = self.old
        self.tmp.cleanup()

    async def _invite(self, nick="@Blogger_One"):
        m = _msg()
        await bo.cmd_blogger(m, mock.Mock(args=nick))
        return m, await db.open_blogger_invite_by_username(nick)

    async def test_command_gives_ready_text_once_per_nick(self):
        m, inv = await self._invite()
        texts = [c.args[0] for c in m.answer.await_args_list]
        self.assertEqual(len(texts), 2)
        self.assertIn("Дарим вам один любой товар", texts[1])
        self.assertIn(f"start=blog_{inv['token']}", texts[1])
        self.assertIn("@blogger_one", texts[1])
        m2, inv2 = await self._invite("blogger_one")
        self.assertEqual(inv2["id"], inv["id"])           # тот же, не второй
        self.assertIn("уже есть приглашение", m2.answer.await_args_list[0].args[0])

    async def test_bad_nick(self):
        m = _msg()
        await bo.cmd_blogger(m, mock.Mock(args="@a b"))
        self.assertIn("Формат", m.answer.await_args.args[0])
        self.assertEqual(await db.list_blogger_invites(), [])

    async def test_only_that_nick_can_open(self):
        _, inv = await self._invite()
        stranger = mock.AsyncMock()
        await bo.claim(stranger, _user(6, "other"), self.state, inv["token"])
        self.assertIn("для аккаунта @blogger_one", stranger.answer.await_args.args[0])
        self.notify.assert_awaited()
        self.assertIsNone((await db.get_blogger_invite(inv["token"]))["user_id"])
        m = mock.AsyncMock()
        with mock.patch("handlers.friend_order._orderable_products",
                        mock.AsyncMock(return_value=[{"id": 10, "name": "Кит"}])):
            await bo.claim(m, _user(), self.state, inv["token"])
        kb = m.answer.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(kb[0][0].callback_data, f"blg:pick:{inv['id']}:10")
        self.assertEqual((await db.get_blogger_invite(inv["token"]))["user_id"], 5)
        # привязано — с другого аккаунта уже не открыть, даже по нику
        again = mock.AsyncMock()
        await bo.claim(again, _user(6, "blogger_one"), self.state, inv["token"])
        self.assertIn("с другого аккаунта", again.answer.await_args.args[0])

    async def test_plain_start_finds_invite_by_nick(self):
        await self._invite()
        m = mock.AsyncMock()
        with mock.patch("handlers.friend_order._orderable_products",
                        mock.AsyncMock(return_value=[{"id": 10, "name": "Кит"}])):
            self.assertTrue(await bo.claim_by_username(m, _user(), self.state))
            self.assertFalse(await bo.claim_by_username(mock.AsyncMock(), _user(7, "nobody"),
                                                        self.state))

    async def test_pick_marks_state_and_round_done_skips_repeat(self):
        _, inv = await self._invite()
        await db.bind_blogger_invite(inv["id"], 5)

        async def fake_flow(target, state, product):
            await state.clear()
            await state.set_state(brief_handler.SurveyForm.answering)
            await state.update_data(product_id=product["id"], rounds=[[]],
                                    round_products=[product["id"]])

        with mock.patch("handlers.friend_order._orderable_products",
                        mock.AsyncMock(return_value=[{"id": 10, "name": "Кит"}])), \
             mock.patch.object(brief_handler, "start_order_flow", fake_flow):
            await bo.cb_pick(_cb(f"blg:pick:{inv['id']}:10"), self.state)
        data = await self.state.get_data()
        self.assertEqual((data["blogger_invite"], data["blogger"]), (inv["id"], "@blogger_one"))
        # у товара настроено «ещё один» — блогеру не предлагаем
        product = {"id": 10, "survey_repeat_text": "Ещё один?", "survey_delivery_text": None}
        with mock.patch.object(db, "get_product", mock.AsyncMock(return_value=product)), \
             mock.patch.object(brief_handler, "_finish_survey", mock.AsyncMock()) as fin:
            target = mock.AsyncMock()
            await brief_handler._round_done(target, self.state, mock.Mock(), _user())
        fin.assert_awaited()
        target.answer.assert_not_awaited()

    async def test_pick_needs_own_open_invite(self):
        _, inv = await self._invite()
        cb = _cb(f"blg:pick:{inv['id']}:10")            # ещё не привязано к нему
        await bo.cb_pick(cb, self.state)
        cb.answer.assert_awaited_with("Это приглашение уже не действует.", show_alert=True)

    async def test_delivery_keeps_blogger_mark(self):
        await self.state.update_data(blogger_invite=3, blogger="@x")
        with mock.patch.object(delivery, "CDEK_CLIENT", object()):
            self.assertTrue(await delivery.start_delivery_flow(
                mock.AsyncMock(), self.state, 10, quantity=1, rounds=[[]], round_products=[10]))
        data = await self.state.get_data()
        self.assertEqual((data["blogger_invite"], data["blogger"]), (3, "@x"))

    async def _ready(self, products=(10,)):
        _, inv = await self._invite()
        await db.bind_blogger_invite(inv["id"], 5)
        await db.save_pending_delivery(
            5, 10, "СДЭК: Москва", survey_json=json.dumps([[]] * len(products)), amount=0,
            round_products_json=json.dumps(list(products)), blogger="@blogger_one")
        return inv

    async def test_free_checkout_once(self):
        inv = await self._ready()
        calls = []

        async def provision(bot, uid, pid, kind, payment_id, amount, gift=False):
            calls.append((uid, pid, kind, payment_id, amount, gift))
            async with aiosqlite.connect(db.DB_PATH) as conn:
                await conn.execute("INSERT INTO orders (user_id, product_id, prodamus_order_id, "
                                   "summary, order_code) VALUES (?, ?, ?, '', 'malimabi-store-077')",
                                   (uid, pid, payment_id))
                await conn.commit()
            return True

        with mock.patch.object(prodamus_webhook, "provision_payment", provision):
            await bo.cb_ok(_cb(f"blg:ok:{inv['id']}:10"), mock.Mock())
            second = _cb(f"blg:ok:{inv['id']}:10")
            await bo.cb_ok(second, mock.Mock())
        self.assertEqual(calls, [(5, 10, "p", f"blogger:{inv['id']}", 0, True)])
        second.answer.assert_awaited_with(
            "Заказ по этому приглашению уже оформлен или оно не действует.", show_alert=True)
        fresh = await db.get_blogger_invite(inv["token"])
        self.assertEqual(fresh["order_code"], "malimabi-store-077")
        self.assertIn("заказал — malimabi-store-077", bo._status(fresh))

    async def test_two_items_refused(self):
        inv = await self._ready(products=(10, 10))
        cb = _cb(f"blg:ok:{inv['id']}:10")
        with mock.patch.object(prodamus_webhook, "provision_payment", mock.AsyncMock()) as prov:
            await bo.cb_ok(cb, mock.Mock())
        prov.assert_not_awaited()
        cb.answer.assert_awaited_with("В подарочном заказе может быть только один товар.",
                                      show_alert=True)
        self.assertIsNone((await db.get_blogger_invite(inv["token"]))["used_at"])

    async def test_failed_provision_returns_invite(self):
        inv = await self._ready()
        cb = _cb(f"blg:ok:{inv['id']}:10")
        with mock.patch.object(prodamus_webhook, "provision_payment",
                               mock.AsyncMock(side_effect=RuntimeError("x"))):
            await bo.cb_ok(cb, mock.Mock())
        self.assertIsNone((await db.get_blogger_invite(inv["token"]))["used_at"])
        self.assertIn("Не получилось", cb.message.answer.await_args.args[0])

    async def test_provision_labels_blogger_order(self):
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute("INSERT INTO products (id, name, description, price, category) "
                               "VALUES (10, 'Кит', '', 999, 'physical')")
            await conn.commit()
        await db.save_pending_delivery(5, 10, "СДЭК: Москва", survey_json="[[]]", amount=0,
                                       round_products_json="[10]", delivery_cost=350.0,
                                       blogger="@blogger_one")
        bot = mock.AsyncMock()
        pw = prodamus_webhook
        with mock.patch.object(pw, "_send_order_notify", mock.AsyncMock()) as notify, \
             mock.patch.object(pw, "_default_whos", mock.AsyncMock(return_value=None)), \
             mock.patch.object(pw, "_printer_admins", mock.AsyncMock(return_value=[])), \
             mock.patch("services.gsheets.request_finance_append") as fin, \
             mock.patch("services.gsheets.request_sync"), \
             mock.patch("handlers.client_order_widget.sync", mock.AsyncMock()), \
             mock.patch.object(pw.config, "CDEK_AUTO_ORDER", False):
            self.assertTrue(await pw.provision_payment(bot, 5, 10, "p", "blogger:1", 0, gift=True))
        summary = notify.await_args.args[1]
        self.assertIn("🎬 Блогер @blogger_one — бесплатно", summary)
        self.assertEqual(fin.call_args.kwargs["comment"], "Блогер @blogger_one: Кит")
        self.assertEqual(fin.call_args.args[2], 0)
        self.assertIn("Подарочный заказ оформлен", bot.send_message.await_args_list[0].args[1])
        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute("SELECT COUNT(*) FROM purchases") as cur:
                self.assertEqual((await cur.fetchone())[0], 0)   # не выручка

    async def test_cancel(self):
        _, inv = await self._invite()
        cb = _cb(f"blg:cancel:{inv['id']}", uid=ADMIN)
        await bo.cb_cancel(cb)
        self.assertIsNotNone((await db.get_blogger_invite(inv["token"]))["cancelled_at"])
        m = mock.AsyncMock()
        await bo.claim(m, _user(), self.state, inv["token"])
        self.assertIn("не работает", m.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
