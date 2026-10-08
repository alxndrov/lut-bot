import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from urllib.parse import urlencode

import aiosqlite

import config
import database as db
from handlers import course as course_handler
from services import course as svc

TOKEN = "123:abc"


def _sign(user_id=7, auth_date=1_000_000, token=TOKEN):
    data = {"auth_date": str(auth_date), "user": json.dumps({"id": user_id})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


class InitDataTest(unittest.TestCase):
    def test_valid(self):
        user = svc.verify_init_data(_sign(), TOKEN, max_age=60, now=1_000_030)
        self.assertEqual(user["id"], 7)

    def test_wrong_token_expired_tampered(self):
        self.assertIsNone(svc.verify_init_data(_sign(token="1:x"), TOKEN, 60, 1_000_030))
        self.assertIsNone(svc.verify_init_data(_sign(), TOKEN, 60, 1_000_100))
        self.assertIsNone(svc.verify_init_data(_sign().replace("7", "8"), TOKEN, 60, 1_000_030))
        self.assertIsNone(svc.verify_init_data("", TOKEN, 60, 1_000_030))


class PricingTest(unittest.TestCase):
    opt = {"price": 4490, "price_full": 5990, "sections": "shooting,editing"}

    def test_cascade_switches_at_msk_midnight(self):
        utc = timezone.utc
        for name, value in (("COURSE_MID_PRICE_FROM", "2026-11-01"),
                            ("COURSE_FULL_PRICE_FROM", "2026-11-08")):
            self.addCleanup(setattr, config, name, getattr(config, name))
            setattr(config, name, value)
        price = lambda *dt: svc.price_for(self.opt, datetime(*dt, tzinfo=utc))
        self.assertEqual(price(2026, 10, 31, 20, 59), 4490)
        self.assertEqual(price(2026, 10, 31, 21, 0), 5240)   # 1 ноября по Москве
        self.assertEqual(price(2026, 11, 7, 20, 59), 5240)
        self.assertEqual(price(2026, 11, 7, 21, 0), 5990)    # 8 ноября по Москве

    def test_mid_price_rounds_to_tens(self):
        self.assertEqual(svc.mid_price({"price": 2490, "price_full": 3490}), 2990)
        self.assertEqual(svc.mid_price({"price": 2490, "price_full": 3495}), 2990)

    def test_available_options(self):
        opts = [{"slug": "shooting", "sections": "shooting"},
                {"slug": "editing", "sections": "editing"},
                {"slug": "bundle", "sections": "shooting,editing"}]
        slugs = lambda owned: [o["slug"] for o in svc.available_options(opts, owned)]
        self.assertEqual(slugs(set()), ["shooting", "editing", "bundle"])
        self.assertEqual(slugs({"shooting"}), ["editing"])
        self.assertEqual(slugs({"shooting", "editing"}), [])


class OpenDateTest(unittest.TestCase):
    def test_days_to_open(self):
        utc = timezone.utc
        old, config.COURSE_OPEN_AT = config.COURSE_OPEN_AT, "2026-11-01"
        self.addCleanup(setattr, config, "COURSE_OPEN_AT", old)
        # Старт 1 ноября 00:00 МСК = 31 октября 21:00 UTC
        self.assertEqual(svc.days_to_open(datetime(2026, 10, 7, 7, 0, tzinfo=utc)), 25)
        self.assertEqual(svc.days_to_open(datetime(2026, 10, 31, 20, 0, tzinfo=utc)), 1)
        self.assertEqual(svc.days_to_open(datetime(2026, 10, 31, 21, 0, tzinfo=utc)), 0)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class TestPaymentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "test.db")
        await db.init_db()
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute("INSERT INTO products(id, name, description, price, category) "
                               "VALUES (14, 'Курс', '-', 0, 'infobiz')")
            await conn.commit()
        self.shoot = await db.add_course_option(14, "shooting", "Съёмка", "shooting", 2490, 3490, 1)
        await db.add_course_option(14, "editing", "Монтаж", "editing", 2990, 3990, 2)
        self.bundle = await db.add_course_option(14, "bundle", "Пакет", "shooting,editing",
                                                 4490, 5990, 3, review_bonus=True)

    async def asyncTearDown(self):
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def _count(self, table):
        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                return (await cur.fetchone())[0]

    async def test_test_payment_grants_access_without_purchase(self):
        bot = FakeBot()
        ok = await course_handler.provision(bot, 7, self.bundle, "", 0, None, test=True)
        self.assertTrue(ok)
        self.assertEqual(await db.get_user_course_sections(7), {"shooting", "editing"})
        self.assertEqual(await db.get_user_test_course_sections(7), {"shooting", "editing"})
        self.assertEqual(await self._count("purchases"), 0)
        self.assertEqual(await self._count("bundle_review_queue"), 0)
        self.assertIn("Тестовая оплата", bot.sent[0][1])

        self.assertEqual(await db.clear_test_course_access(7), 2)
        self.assertEqual(await db.get_user_course_sections(7), set())

    async def test_real_payment_overrides_test_and_survives_reset(self):
        await db.grant_course_access(7, ["shooting"], None, test=True)
        purchase_id = await db.add_course_purchase(7, None, 14, self.shoot, "p1", 2490)
        await db.grant_course_access(7, ["shooting"], purchase_id)
        await db.grant_course_access(7, ["editing"], None, test=True)
        # Тестовая выдача поверх настоящей её не перетирает
        await db.grant_course_access(7, ["shooting"], None, test=True)
        self.assertEqual(await db.get_user_test_course_sections(7), {"editing"})
        await db.clear_test_course_access(7)
        self.assertEqual(await db.get_user_course_sections(7), {"shooting"})

    async def test_app_state_hides_admin_tools_and_preview_from_buyers(self):
        user = {"id": 7, "first_name": "Аня"}
        state = await course_handler._app_state(user, "bot", preview_open=True)
        self.assertFalse(state["admin"])
        self.assertEqual(state["options"], [])
        self.assertEqual(state["open"], svc.is_open())
        self.assertEqual(state["materials"], [])
        self.assertFalse(state["review"]["eligible"])

        admin = {"id": config.ADMIN_IDS[0], "first_name": "Даня"}
        await db.grant_course_access(admin["id"], ["shooting", "editing"], None, test=True)
        state = await course_handler._app_state(admin, "bot", preview_open=True)
        self.assertTrue(state["open"])
        self.assertEqual(len(state["options"]), 3)
        self.assertTrue(state["review"]["eligible"] and state["review"]["test"])
        self.assertTrue(all(l["open"] for s in state["sections"] for l in s["lessons"]))


if __name__ == "__main__":
    unittest.main()
