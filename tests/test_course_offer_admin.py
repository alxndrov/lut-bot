"""/offer в админ-боте, страница предложения, события в «Базу»."""
import os
import re
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import aiosqlite
from aiohttp import web

import config
import database as db
from handlers import course_offer, pay_router
from services import course as svc, leads_sheet, paylinks


class OfferCommandTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        await db.upsert_user(user_id=5, username="dof0miin", first_name="Дофа", ref=None)
        await db.upsert_user(user_id=6, username="newbie", first_name="Новый", ref=None)
        await db.accept_policy(5)
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("INSERT INTO products(id, name, description, price, category) "
                            "VALUES (1, 'Курс', '', 0, 'infobiz')")
            for row in [(1, "shooting", "Съёмка", "shooting", 2990, 3990, 4990, 0),
                        (3, "bundle", "Пакет", "shooting,editing", 4990, 6990, 8990, 0),
                        (4, "vip", "VIP", "shooting,editing", 12490, 12490, 12490, 1)]:
                await c.execute("INSERT INTO course_options (id, product_id, slug, title, sections, "
                                "price, price_mid, price_full, call_bonus) "
                                "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)", row)
            await c.commit()
        p = mock.patch.object(svc, "now_msk",
                              return_value=datetime(2026, 11, 17, 13, 0, tzinfo=svc.MSK))
        p.start(); self.addCleanup(p.stop)
        for k, v in dict(POLICY_REQUIRED=True, LAVA_ENABLED=True, LEADS_SHEET_ID="",
                         PAY_BASE_URL="https://app.malimabi.com").items():
            p = mock.patch.object(config, k, v)
            p.start(); self.addCleanup(p.stop)

    async def asyncTearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    async def _cmd(self, args):
        msg = mock.AsyncMock()
        msg.from_user.id = config.ADMIN_IDS[0]
        await course_offer.cmd_offer(msg, mock.Mock(args=args))
        return msg.answer.await_args.args[0]

    async def _only_offer(self):
        offers = await db.get_active_course_offers("2026-11-17")
        self.assertEqual(len(offers), 1)
        return offers[0]

    def test_parse_until(self):
        today = datetime(2026, 11, 17).date()
        self.assertEqual(course_offer.parse_until("20.11", today).isoformat(), "2026-11-20")
        self.assertEqual(course_offer.parse_until("05.01", today).isoformat(), "2027-01-05")
        self.assertEqual(course_offer.parse_until("20.11.26", today).isoformat(), "2026-11-20")
        self.assertEqual(course_offer.parse_until("2026-11-30", today).isoformat(), "2026-11-30")
        self.assertIsNone(course_offer.parse_until("31.02", today))
        self.assertIsNone(course_offer.parse_until("завтра", today))

    async def test_offer_command_replies_with_pay_link(self):
        text = await self._cmd("@dof0miin vip 11190 20.11")
        offer = await self._only_offer()
        self.assertEqual((offer["user_id"], offer["option_id"], offer["price"], offer["valid_until"]),
                         (5, 4, 11190, "2026-11-20"))
        link = paylinks.parse_token(re.search(r"https://\S+/pay/(\S+)", text)[1])
        self.assertEqual((link["order_num"], link["amount"], link["via"]),
                         ("c_5_4", 11190, f"offer:{offer['token']}"))
        self.assertIn("https://app.malimabi.com/pay/", text)
        self.assertNotIn("t.me/", text)
        self.assertNotIn("Согласий", text)
        self.assertIn("Согласий", await self._cmd("@newbie package 7990 30.11"))

    async def _cmd_all(self, args):
        """Все ответы бота: [(текст, kwargs), ...]."""
        msg = mock.AsyncMock()
        msg.from_user.id = config.ADMIN_IDS[0]
        await course_offer.cmd_offer(msg, mock.Mock(args=args))
        return [(c.args[0], c.kwargs) for c in msg.answer.await_args_list]

    async def test_template_gives_ready_text_for_dm(self):
        replies = await self._cmd_all(
            "@dof0miin vip 11190 20.11\n"
            "Привет!\n\nДля тебя {тариф} за {цена} вместо {старая_цена} — до {срок}.\n"
            "\nОплатить: {ссылка}\n")
        self.assertEqual(len(replies), 2)
        service, (client, kw) = replies[0][0], replies[1]
        offer = await self._only_offer()
        link = re.search(r"https://\S+/pay/\S+", service)[0]
        self.assertEqual(client,
                         "Привет!\n\nДля тебя пакет с созвоном за 11 190 ₽ вместо 12 490 ₽ — "
                         f"до 20 ноября.\n\nОплатить: {link}\n")
        self.assertEqual(paylinks.parse_token(link.rsplit("/pay/", 1)[1])["via"],
                         f"offer:{offer['token']}")
        self.assertIsNone(kw["parse_mode"])
        self.assertFalse(kw["protect_content"])
        self.assertNotIn("⚠️", service)
        self.assertIn(f"/offer_cancel {offer['token']}", service)

    async def test_template_warnings(self):
        replies = await self._cmd_all("@dof0miin package 7990 30.11\nТебе {тариф}: {цена}, {скидка} {скидка}")
        service, client = replies[0][0], replies[1][0]
        self.assertEqual(client, "Тебе пакет: съёмка + монтаж: 7 990 ₽, {скидка} {скидка}")
        self.assertIn("нет {ссылка}", service)
        self.assertIn("Не знаю метки {скидка} — оставил как есть", service)
        self.assertEqual(len(await db.get_active_course_offers("2026-11-17")), 1)   # создано

    async def test_no_template_works_as_before(self):
        replies = await self._cmd_all("@dof0miin shooting 3990 20.11")
        self.assertEqual(len(replies), 1)
        replies = await self._cmd_all("@dof0miin shooting 3900 21.11\n   \n")
        self.assertEqual(len(replies), 1)
        self.assertIn("3 900 ₽", replies[0][0])   # предложение создано, второго сообщения нет

    def test_render_template(self):
        text, unknown = course_offer.render_template("{срок}\n\n{x}{срок}{}", {"срок": "20 ноября"})
        self.assertEqual(text, "20 ноября\n\n{x}20 ноября{}")
        self.assertEqual(unknown, ["{x}", "{}"])

    async def test_offer_command_rejects_bad_input(self):
        self.assertIn("Не знаю тариф", await self._cmd("@dof0miin gold 100 20.11"))
        self.assertIn("дороже", await self._cmd("@dof0miin shooting 5990 20.11"))
        self.assertIn("Не понял дату", await self._cmd("@dof0miin shooting 3990 2026-11-01"))
        self.assertIn("Не нашёл", await self._cmd("@nobody shooting 3990 20.11"))
        self.assertIn("Формат", await self._cmd("@dof0miin shooting"))
        await db.add_course_call(5, None, "2027-01-17", False)
        self.assertIn("уже есть", await self._cmd("@dof0miin vip 11190 20.11"))
        self.assertEqual(await db.get_active_course_offers("2026-11-17"), [])

    async def test_offers_list_and_cancel(self):
        await self._cmd("@dof0miin vip 11190 20.11")
        offer = await self._only_offer()
        msg = mock.AsyncMock()
        msg.from_user.id = config.ADMIN_IDS[0]
        await course_offer.cmd_offers(msg)
        self.assertIn("@dof0miin", msg.answer.await_args.args[0])
        self.assertIn("не открывал", msg.answer.await_args.args[0])
        await course_offer.cmd_offer_cancel(msg, mock.Mock(args=offer["token"]))
        self.assertIn("Отменил", msg.answer.await_args.args[0])
        problem = await course_offer._check(await db.get_course_offer(offer["token"]), 5)
        self.assertIn("закончился", problem)
        self.assertEqual(await db.get_active_course_offers("2026-11-17"), [])

    async def _page(self, user="@dof0miin"):
        await self._cmd(f"{user} vip 11190 20.11")
        offer = (await db.get_active_course_offers("2026-11-17"))[-1]
        option = await db.get_course_option(4)
        link = paylinks.parse_token(course_offer.pay_link(offer, option).rsplit("/pay/", 1)[1])
        req = mock.Mock(app={"bot": mock.AsyncMock()}, method="GET")
        try:
            return offer, await pay_router._offer_pay(req, link, "malimabibot")
        except web.HTTPFound as e:
            return offer, e.location

    async def test_page_shows_offer_when_consents_given(self):
        offer, resp = await self._page()
        self.assertEqual(resp.status, 200)
        self.assertIn("12 490 ₽</s>", resp.text)
        self.assertIn("11 190 ₽", resp.text)
        self.assertIn("20 ноября", resp.text)
        vias = [paylinks.parse_token(t)["via"] for t in re.findall(r'/pay/([^"]+)"', resp.text)]
        self.assertEqual(vias, [f"offer:{offer['token']}:paw", f"offer:{offer['token']}:lava"])
        kinds = [e["kind"] for e in await db.get_course_offer_events(offer["token"])]
        self.assertEqual(kinds, ["open"])

    async def test_page_sends_to_bot_without_consents(self):
        offer, where = await self._page("@newbie")
        self.assertEqual(where, f"https://t.me/malimabibot?start=offer_{offer['token']}")
        self.assertEqual(await db.get_course_offer_events(offer["token"]), [])

    async def test_claim_in_bot_gives_both_payment_methods(self):
        await self._cmd("@dof0miin vip 11190 20.11")
        offer = await self._only_offer()
        msg = mock.AsyncMock()
        await course_offer.claim(msg, 5, offer["token"])
        kb = msg.answer.await_args.kwargs["reply_markup"].inline_keyboard
        vias = [paylinks.parse_token(r[0].url.rsplit("/pay/", 1)[1])["via"] for r in kb[:2]]
        self.assertEqual(vias, [f"offer:{offer['token']}:paw", f"offer:{offer['token']}:lava"])
        self.assertIn("<s>12 490 ₽</s>", msg.answer.await_args.args[0])

    async def test_pay_and_purchase_events_go_to_sheet(self):
        await self._cmd("@dof0miin vip 11190 20.11")
        offer = await self._only_offer()
        option = await db.get_course_option(4)
        link = paylinks.parse_token(course_offer.pay_link(offer, option, "paw").rsplit("/pay/", 1)[1])
        req = mock.Mock(app={"bot": mock.AsyncMock()}, method="GET")
        with mock.patch.object(config, "PAYANYWAY_ENABLED", True), \
             mock.patch.object(pay_router, "_payanyway_url",
                               mock.AsyncMock(return_value="https://paw/y")):
            with self.assertRaises(web.HTTPFound):
                await pay_router._offer_pay(req, link, "malimabibot")
        await course_offer.on_purchase(5, 4, 11190)
        await course_offer.on_purchase(6, 4, 12490)          # без предложения — ничего
        lines = []
        with mock.patch.object(config, "LEADS_SHEET_ID", "x"), \
             mock.patch.object(leads_sheet, "append_note",
                               side_effect=lambda uid, u, n, line: lines.append((uid, u, line)) or True):
            await course_offer.flush_events()
        self.assertEqual([l[:2] for l in lines], [(5, "dof0miin"), (5, "dof0miin")])
        self.assertIn("перешёл к оплате — карта РФ / СБП: VIP, 11 190 ₽ до 20.11", lines[0][2])
        self.assertIn("купил: VIP, 11 190 ₽ до 20.11 (11 190 ₽)", lines[1][2])
        self.assertEqual(await db.get_course_offer_events(unsynced=True), [])

    async def test_sheet_error_keeps_event_for_retry(self):
        await self._cmd("@dof0miin vip 11190 20.11")
        offer = await self._only_offer()
        await db.add_course_offer_event(offer["token"], 5, "open")
        with mock.patch.object(config, "LEADS_SHEET_ID", "x"), \
             mock.patch.object(leads_sheet, "append_note", side_effect=RuntimeError("quota")):
            await course_offer.flush_events()
        self.assertEqual(len(await db.get_course_offer_events(unsynced=True)), 1)


class NotePlanTest(unittest.TestCase):
    def test_appends_to_existing_row_or_takes_empty_one(self):
        cells = {"B": "=HYPERLINK(...)", "C": "Дофа", "D": "9"}
        sheet = [["1", "@a", "А", "7", "", "", "", "", "", "", "", "было"],
                 ["2", "@d", "Дофа", "5"],
                 ["3"]]
        self.assertEqual(leads_sheet.note_plan(sheet, 5, "новое", cells),
                         [{"range": "L3", "values": [["'новое"]]}])
        self.assertEqual(leads_sheet.note_plan(sheet, 7, "ещё", cells),
                         [{"range": "L2", "values": [["'было\nещё"]]}])
        self.assertEqual(leads_sheet.note_plan(sheet, 9, "первое", cells),
                         [{"range": "B4:D4", "values": [["=HYPERLINK(...)", "'Дофа", "9"]]},
                          {"range": "L4", "values": [["'первое"]]}])
