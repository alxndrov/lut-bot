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


if __name__ == "__main__":
    unittest.main()
