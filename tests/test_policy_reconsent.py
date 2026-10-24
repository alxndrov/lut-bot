import os
import tempfile
import unittest

import aiosqlite

import database as db


class ReconsentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        for uid in (1, 2, 3):
            await db.upsert_user(user_id=uid, username=None, first_name="", ref=None)
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("UPDATE users SET policy_accepted_at = '2026-09-01 10:00:00' WHERE user_id = 1")
            await c.execute("UPDATE users SET policy_accepted_at = '2026-10-21 10:00:00' WHERE user_id = 2")
            await c.commit()

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    async def test_status(self):
        # Пока смена не включена — прежнее согласие действует
        self.assertEqual(await db.policy_status(1), "ok")
        await db.set_setting(db.POLICY_RECONSENT_SETTING, "2026-10-20 00:00:00")
        self.assertEqual(await db.policy_status(1), "old")    # давал прежнему оператору
        self.assertEqual(await db.policy_status(2), "ok")     # уже согласился с новой
        self.assertEqual(await db.policy_status(3), "none")   # новый — как обычно
        await db.accept_policy(1)
        self.assertEqual(await db.policy_status(1), "ok")


class CheckboxConsentTest(ReconsentTest):
    """Оферта и согласие на ПДн — две отдельные галочки (ч. 1 ст. 9 152-ФЗ)."""

    def _callback(self, data, markup):
        from unittest import mock
        cb = mock.AsyncMock()
        cb.data, cb.from_user.id = data, 3
        cb.message.reply_markup = markup
        return cb

    async def test_both_boxes_required(self):
        from unittest import mock
        import config
        from handlers import start
        with mock.patch.object(config, "OFFER_URL", "https://offer"):
            kb = start.policy_keyboard()
            self.assertEqual(start._boxes(kb), {"offer": False, "consent": False})
            # «Дальше» без галочек — не пускаем
            cb = self._callback("policy:accept", kb)
            with mock.patch.object(start, "show_catalog", mock.AsyncMock()) as catalog:
                await start.cb_policy_accept(cb)
                catalog.assert_not_awaited()
            self.assertEqual(await db.policy_status(3), "none")
            # отмечаем только согласие — всё ещё нельзя
            cb = self._callback("policy:t:consent", kb)
            await start.cb_policy_toggle(cb)
            kb = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"]
            self.assertEqual(start._boxes(kb), {"offer": False, "consent": True})
            await start.cb_policy_accept(self._callback("policy:accept", kb))
            self.assertEqual(await db.policy_status(3), "none")
            # отмечаем оферту — пускаем
            cb = self._callback("policy:t:offer", kb)
            await start.cb_policy_toggle(cb)
            kb = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"]
            with mock.patch.object(start, "show_catalog", mock.AsyncMock()) as catalog:
                await start.cb_policy_accept(self._callback("policy:accept", kb))
                catalog.assert_awaited_once()
            self.assertEqual(await db.policy_status(3), "ok")


if __name__ == "__main__":
    unittest.main()
