import os
import tempfile
import unittest
from unittest.mock import patch

import aiosqlite
import config
import database as db
from services import leads_sheet as svc


class LeadsSheetTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, 'DB_PATH', os.path.join(self.tmp.name, 'test.db'))
        self.patch.start()
        self.patch2 = patch.object(svc, 'DB_PATH', db.DB_PATH)
        self.patch2.start()
        await db.init_db()
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO products(id,name,description,price,category) "
                            "VALUES (10,'Микрофон','',999,'physical'), (6,'LUT','',399,'digital')")
            for uid, name in ((1, 'anna'), (2, None), (3, 'boris'), (4, 'ncomonetasupport')):
                await c.execute("INSERT INTO users(user_id,username,first_name,first_seen) "
                                "VALUES (?,?,?,'2026-11-01 10:00:00')", (uid, name, f'U{uid}'))
            await c.execute("INSERT INTO course_views(user_id,first_at,last_at,views,source) "
                            "VALUES (1,'2026-11-02 10:00:00','2026-11-03 22:30:00',2,'app'),"
                            "(4,'2026-11-02 10:00:00','2026-11-02 10:00:00',1,'bot')")
            await c.execute("INSERT INTO pending_deliveries(user_id,product_id,delivery_str,created_at,amount) "
                            "VALUES (2,10,'x','2026-11-05 10:00:00',1351)")
            await c.execute("INSERT INTO pay_clicks(order_num,user_id,amount,name,provider,clicked_at) "
                            "VALUES ('p_2_10',2,1351,'Микрофон','prodamus','2026-11-06 10:00:00')")
            # boris купил LUT — его просмотр карточки LUT не интерес
            await c.execute("INSERT INTO product_views(user_id,product_id,first_at,last_at) "
                            "VALUES (3,6,'2026-11-04 10:00:00','2026-11-04 10:00:00')")
            await c.execute("INSERT INTO purchases(user_id,product_id,telegram_payment_id,amount,created_at) "
                            "VALUES (3,6,'x',399,'2026-10-01 10:00:00')")
            await c.commit()

    async def asyncTearDown(self):
        self.patch2.stop()
        self.patch.stop()
        self.tmp.cleanup()

    async def test_build_picks_furthest_stage_and_skips_buyers(self):
        leads = svc.build(await svc.collect())
        self.assertEqual(set(leads), {1, 2, 3})
        self.assertFalse(leads[3]['open'])
        anna = leads[1]['cells']
        self.assertEqual(anna['B'], '=HYPERLINK("https://t.me/anna";"@anna")')
        self.assertEqual((anna['E'], anna['G'], anna['H']), ('Курс', 'Открыл курс', '04.11.2026'))
        two = leads[2]['cells']
        self.assertEqual((two['B'], two['E'], two['G']), ('', 'Физтовар', 'Перешёл к оплате'))
        self.assertEqual(two['F'], 'оплата: Микрофон, 1 351 ₽')   # анкета того же товара не дублируется

    async def test_plan_adds_updates_marks_bought_and_respects_deleted_rows(self):
        leads = svc.build(await svc.collect())
        updates, remember, forget, missing = svc.plan([], leads, set())
        self.assertEqual(sorted(remember), [1, 2])
        self.assertEqual(updates[0]['range'], 'B2:J2')            # свежий (2, 06.11) сверху
        self.assertEqual(updates[0]['values'][0][-1], "'Не связывались")
        # Строка уже такая же — ничего не пишем; Даня удалил строку — не возвращаем
        row = ['1'] + svc._shown(leads[2]['cells']) + ['Думает']
        updates, *_ = svc.plan([row], leads, {1, 2})
        self.assertEqual(updates, [])
        # Оплатил: анкета удалилась, клик оплачен — статус «Купил», заметки не трогаем
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO leads_sheet(user_id,added_at) VALUES (2,'2026-11-07 10:00:00')")
            await c.execute("DELETE FROM pending_deliveries")
            await c.execute("UPDATE pay_clicks SET paid_at='2026-11-08 10:00:00'")
            await c.execute("INSERT INTO purchases(user_id,product_id,telegram_payment_id,amount,created_at) "
                            "VALUES (2,10,'y',1351,'2026-11-08 10:00:00')")
            await c.commit()
        leads = svc.build(await svc.collect())
        updates, *_ = svc.plan([row], leads, {1, 2})
        self.assertEqual(updates, [{'range': 'J2', 'values': [['Купил']]}])

    async def test_forgotten_user_row_is_cleared(self):
        await db.forget_user(1)
        leads = svc.build(await svc.collect())
        row = ['1', '@anna', 'U1', '1', 'Курс', 'x', 'Открыл курс', '04.11.2026', '01.11.2026',
               'Ответили', 'шаг', '', '', 'заметка']
        updates, remember, forget, _ = svc.plan([row], leads, {1, 2})
        self.assertEqual(forget, [1])
        self.assertEqual(updates, [{'range': 'B2:N2', 'values': [[''] * 13]}])

    async def test_product_view_recorded_and_forgotten(self):
        await db.record_product_view(9, 10)
        await db.record_product_view(9, 10)
        async with aiosqlite.connect(db.DB_PATH) as c:
            self.assertEqual(await (await c.execute(
                'SELECT views FROM product_views WHERE user_id=9')).fetchone(), (2,))
        await db.forget_user(9)
        async with aiosqlite.connect(db.DB_PATH) as c:
            self.assertIsNone(await (await c.execute(
                'SELECT 1 FROM product_views WHERE user_id=9')).fetchone())


if __name__ == '__main__':
    unittest.main()
