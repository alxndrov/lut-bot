import os
import tempfile
import unittest

import aiosqlite

import database as db
from handlers import reviews, shop_reviews


class ShopReviewsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO products(id, name, description, price, category) "
                            "VALUES (13, '🎤 Microphone Handled Kit', '-', 1234, 'physical')")
            await c.commit()

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    async def test_only_published_and_anonymous(self):
        a = await db.add_review(7, "ivan_secret", "Иван", None, 13, "Отличный кейс, <b>спасибо</b>!",
                                created_at="2026-10-01 10:00:00")
        await db.add_review(8, "petya", "Петя", None, 13, "не показывать")
        await db.add_review(9, "foto", "Фото", None, 13, None, "photo", "f1")
        self.assertEqual(await db.count_published_reviews(), 0)
        await db.set_review_published(a, True)
        text, kb = await shop_reviews.page(0)
        self.assertIn("Отличный кейс", text)
        self.assertIn("&lt;b&gt;", text)                 # HTML клиента не исполняется
        self.assertIn("Microphone Handled Kit · октябрь 2026", text)
        for leak in ("ivan_secret", "Иван", "не показывать"):
            self.assertNotIn(leak, text)
        # Правка текста для магазина
        await db.set_review_public_text(a, "Отличный кейс!")
        text, _ = await shop_reviews.page(0)
        self.assertIn("«Отличный кейс!»", text)
        await db.set_review_published(a, False)
        self.assertEqual(await db.count_published_reviews(), 0)

    def test_publish_row(self):
        self.assertEqual(reviews.publish_row(5, False)[0].callback_data, "rvpub:5")
        self.assertEqual([b.callback_data for b in reviews.publish_row(5, True)],
                         ["rvunpub:5", "rvedit:5"])


if __name__ == "__main__":
    unittest.main()
