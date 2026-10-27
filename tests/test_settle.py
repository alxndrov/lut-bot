import os
import tempfile
import unittest
from unittest import mock

import aiosqlite

import config
import database as db
from services import settle


class SettleTest(unittest.IsolatedAsyncioTestCase):
    """Взаиморасчёт как в Splitwise: деньги у обоих, один перевод, доли — из своих."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO products (id, name, description, price, category) "
                            "VALUES (1, 'LUT', '-', 1000, 'digital')")
            # Даня: оплата через Prodamus; Миша: две оплаты через PayAnyWay
            for code, amount in (("pd-1", 10000), ("paw-1", 20000), ("paw-2", 20000)):
                await c.execute("INSERT INTO purchases (user_id, product_id, telegram_payment_id, amount, "
                                "created_at) VALUES (7, 1, ?, ?, '2026-10-20 10:00:00')", (code, amount))
            await c.commit()
        for k, v in dict(EARLY_PAYOUTS=0.0, PRODAMUS_FEE_PERCENT=4.0, PAYANYWAY_FEE_PERCENT=4.0,
                         NPD_PERCENT=4.0).items():
            p = mock.patch.object(config, k, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("services.gsheets.request_expense_append", lambda *a, **k: None)
        p.start(); self.addCleanup(p.stop)
        p = mock.patch("handlers.finance._pending_gross", mock.AsyncMock(return_value=0.0))
        p.start(); self.addCleanup(p.stop)

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    async def test_split_hold_and_settle(self):
        st = await settle.state()
        self.assertAlmostEqual(st["held"]["danya"], 9600)          # 10 000 − 4% комиссии
        self.assertAlmostEqual(st["held"]["misha"], 38400)         # 40 000 − 4%
        self.assertAlmostEqual(st["tax"]["danya"], 400)            # 4% с его чеков
        self.assertAlmostEqual(st["tax"]["misha"], 1600)           # 4% с чеков Миши
        # цифра 80/20: чистыми 50 000 − 2 000 комиссии − 2 000 налога = 46 000
        self.assertAlmostEqual(st["share"]["misha"] + st["share"]["danya"], 46000, places=1)
        self.assertAlmostEqual(st["share"]["danya"], 9200, places=1)
        # у Миши лишнее: 38 400 − 1 600 налога − 36 800 доли = 0; у Дани: 9 600 − 400 − 9 200 = 0
        self.assertIsNone(st["transfer"])

        # Миша заранее взял себе 5 000 из своих: денег у него меньше, но и доля к выплате тоже
        await db.add_payout("Миша", 5000, "аванс", source="misha")
        st = await settle.state()
        self.assertIsNone(st["transfer"])

        # Даня заплатил 3 000 со своего счёта за материалы: прибыль меньше, у Дани меньше денег
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO expenses (spent_at, category, amount, paid_by) "
                            "VALUES ('2026-10-21 10:00:00', 'Материалы', 3000, 'Проект')")
            await c.commit()
        st = await settle.state()
        self.assertEqual(st["transfer"]["from"], "misha")
        self.assertAlmostEqual(st["transfer"]["amount"], 1800, places=1)  # расходы делятся 60/40

        res = await settle.settle(1, "test")
        self.assertAlmostEqual(res["paid"]["danya"] + res["paid"]["misha"],
                               st["share"]["danya"] + st["share"]["misha"], places=1)
        async with aiosqlite.connect(db.DB_PATH) as c:
            sources = {r[0]: r[1] for r in await c.execute_fetchall(
                "SELECT recipient, source FROM payouts WHERE comment LIKE 'Взаиморасчёт%'")}
        self.assertEqual(sources, {"Даня": "danya", "Миша": "misha"})
        after = await settle.state()
        self.assertIsNone(after["transfer"])
        self.assertAlmostEqual(after["share"]["danya"], 0, places=1)
        self.assertAlmostEqual(after["share"]["misha"], 0, places=1)
        # резервы остались у тех, кто платит
        self.assertAlmostEqual(after["held"]["danya"], after["reserve"]["danya"], places=1)
        self.assertAlmostEqual(after["held"]["misha"], after["reserve"]["misha"], places=1)


if __name__ == "__main__":
    unittest.main()
