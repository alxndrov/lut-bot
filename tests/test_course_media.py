"""Видео уроков (Bunny) и файлы курса (через бота)."""
import asyncio
import hashlib
import os
import tempfile
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import config
import database as db
from handlers import course as course_handler
from services import bunny
from services import course as svc
from services import course_files as cf

# Имена файлов из папок курса (07.11.2026)
NAMES = """cut_01_english.pdf cut_01_hotkeys.pdf cut_02_otbor.pdf cut_03_razbor.pdf cut_04_zvuk.pdf
cut_05_cvet.pdf cut_06_neyronka.pdf cut_07_eksport.pdf cut_07_seyfzony.pdf
seyfzona_all_1080x1920.png seyfzona_instagram_reels_1080x1920.png seyfzona_telegram_stories_1080x1920.png
seyfzona_tiktok_1080x1920.png seyfzona_vk_klipy_1080x1920.png seyfzona_youtube_shorts_1080x1920.png
quest_01_otbor_sborka.pdf MALIMABI_Library_1.1.0.zip playhead_ustanovka.pdf
Playhead-2.3.7-macOS-Apple-Silicon-arm64.dmg Playhead-2.3.7-macOS-Intel.dmg Playhead-2.3.7-Windows-Setup.exe
00_nastroyki_blackmagic.pdf 01_balans_belogo.pdf 02_vyderzhka.pdf 03_iso.pdf 04_false_color.pdf
shot_02_oborudovanie.pdf shot_03_linzy.pdf shot_04_pod_lut.pdf shot_05_dvizhenie.pdf""".split()


class RecognizeTest(unittest.TestCase):
    def test_all_course_files_recognized(self):
        for name in NAMES + ["MALIMABI LUT.cube"]:
            self.assertIsNotNone(cf.recognize(name), name)

    def test_places(self):
        self.assertEqual(cf.recognize("02_vyderzhka.pdf")["section"], "shooting")
        self.assertEqual(cf.recognize("02_vyderzhka.pdf")["lesson"], 1)
        self.assertEqual((cf.recognize("seyfzona_tiktok_1080x1920.png")["section"],
                          cf.recognize("seyfzona_tiktok_1080x1920.png")["lesson"]), ("editing", 7))
        self.assertEqual(cf.recognize("Playhead-2.3.7-macOS-Apple-Silicon-arm64.dmg")["key"], "playhead_mac_arm")
        self.assertEqual(cf.recognize("Playhead-2.3.7-macOS-Intel.dmg")["key"], "playhead_mac_intel")
        self.assertEqual(cf.recognize("MALIMABI_Library_1.2.0.zip")["key"], "library")   # новая версия — тот же слот
        self.assertEqual(cf.recognize("MALIMABI LUT.cube")["sections"], ["shooting", "editing"])
        self.assertIsNone(cf.recognize("cut_old.pdf"))

    def test_every_lesson_file_points_to_real_lesson(self):
        for key, (section, n, _) in cf.LESSON_FILES.items():
            self.assertLessEqual(n, len(svc.PROGRAM[section]["lessons"]), key)


class BunnyTest(unittest.TestCase):
    def test_lesson_by_title(self):
        self.assertEqual(bunny.lesson_of("SHOT 01 BLACK MAGIC.mp4"), ("shooting", 1))
        self.assertEqual(bunny.lesson_of("CUT 1 WELCOME PRmp4.mp4"), ("editing", 1))
        self.assertEqual(bunny.lesson_of("CUT 02 OTBOR.mp4"), ("editing", 2))
        self.assertEqual(bunny.lesson_of("cut 3 razbor montaje.mp4"), ("editing", 3))
        self.assertIsNone(bunny.lesson_of("trailer.mp4"))

    def test_pick_prefers_finished_then_newest(self):
        items = [
            {"guid": "a", "title": "CUT 4 SOUND.mp4", "status": 4, "dateUploaded": "2026-11-07T10:00"},
            {"guid": "b", "title": "CUT 4 SOUND v2.mp4", "status": 3, "dateUploaded": "2026-11-07T12:00"},
            {"guid": "c", "title": "SHOT 2 STUFF.mp4", "status": 4, "dateUploaded": "2026-11-07T10:00"},
            {"guid": "d", "title": "SHOT 02 STUFF.mp4", "status": 4, "dateUploaded": "2026-11-07T11:00"},
        ]
        picked = bunny._pick(items)
        self.assertEqual(picked["editing"]["4"]["id"], "a")
        self.assertEqual(picked["shooting"]["2"]["id"], "d")
        self.assertEqual(picked["shooting"]["2"]["duplicates"], 1)

    def test_chapters_sorted_and_cleaned(self):
        picked = bunny._pick([{"guid": "a", "title": "CUT 5 TSVET.mp4", "status": 4, "chapters": [
            {"title": "Film look", "start": 610, "end": 900}, {"title": " Вступление ", "start": 0, "end": 30},
            {"title": "", "start": 50}, {"title": "Битый", "start": "x"}]}])
        self.assertEqual(picked["editing"]["5"]["chapters"],
                         [{"start": 0, "title": "Вступление"}, {"start": 610, "title": "Film look"}])
        self.assertEqual(bunny.time_text(610), "10:10")
        self.assertEqual(bunny.time_text(3725), "1:02:05")

    def test_embed_url_signed(self):
        with mock.patch.object(config, "BUNNY_LIBRARY_ID", "77"), \
             mock.patch.object(config, "BUNNY_TOKEN_KEY", "key"):
            url = bunny.embed_url("vid", now=1000)
        expires = 1000 + bunny.TOKEN_TTL
        token = hashlib.sha256(f"keyvid{expires}".encode()).hexdigest()
        self.assertTrue(url.startswith("https://iframe.mediadelivery.net/embed/77/vid?"))
        self.assertIn(f"token={token}&expires={expires}", url)


class FakeBot:
    def __init__(self):
        self.docs = []

    async def send_document(self, chat_id, file_id, **kwargs):
        self.docs.append((chat_id, file_id, kwargs.get("caption")))


class AppTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        self._videos = bunny.videos

        async def fake_videos(force=False):
            return {"editing": {"2": {"id": "v2", "status": 4, "length": 2101, "width": 1920, "height": 1080,
                                      "chapters": [{"start": 0, "title": "Старт"}, {"start": 95, "title": "Отбор"}]},
                                "3": {"id": "v3", "status": 3, "progress": 40, "length": 0, "width": 0, "height": 0}}}
        bunny.videos = fake_videos
        for name in ("cut_07_eksport.pdf", "seyfzona_tiktok_1080x1920.png", "MALIMABI LUT.cube",
                     "Playhead-2.3.7-Windows-Setup.exe", "02_vyderzhka.pdf"):
            await cf.save(cf.recognize(name), "fid-" + name, name, 1000)
        self.bot = FakeBot()
        app = web.Application()
        app["bot"] = self.bot
        course_handler.setup_web(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.patches = [mock.patch.object(course_handler.svc, "verify_init_data",
                                          lambda s: {"id": int(s)} if s else None),
                        mock.patch.object(svc, "is_open", lambda now=None: True)]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.client.close()
        bunny.videos = self._videos
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def post(self, path, user, **body):
        r = await self.client.post(path, json={"initData": str(user)} | body)
        return r.status, await r.json()

    async def test_state_lessons_and_materials(self):
        await db.grant_course_access(5, ["editing"], None)
        state = await course_handler._app_state({"id": 5}, "bot")
        editing = next(s for s in state["sections"] if s["slug"] == "editing")
        l2, l3, l7 = editing["lessons"][1], editing["lessons"][2], editing["lessons"][6]
        self.assertTrue(l2["video"].startswith("https://iframe.mediadelivery.net/embed/"))
        self.assertEqual(l2["duration"], "35 мин")
        self.assertEqual(l2["chapters"], [{"start": 0, "time": "0:00", "title": "Старт"},
                                          {"start": 95, "time": "1:35", "title": "Отбор"}])
        self.assertEqual(l3["video"], "")
        self.assertEqual(l3["chapters"], [])
        self.assertTrue(l3["processing"])
        self.assertEqual([f["title"] for f in l7["files"]], ["Экспорт и Topaz", "Сейф-зона · TikTok"])
        shooting = next(s for s in state["sections"] if s["slug"] == "shooting")
        self.assertEqual(shooting["lessons"][0]["files"], [])      # раздел не куплен
        groups = {g["group"]: g for g in state["materials"]}
        self.assertEqual(set(groups), {"lut", "playhead"})           # Library и квест не загружены
        keys = [it["key"] for it in groups["playhead"]["items"]]
        self.assertEqual(keys, ["playhead_windows"])                  # показываем только загруженное
        self.assertEqual(state["wm"], "ID 5")

    async def test_review_tab_only_for_review_or_vip_and_faq_for_all(self):
        await db.grant_course_access(5, ["shooting"], None)
        state = await course_handler._app_state({"id": 5}, "bot")
        self.assertFalse(state["review_tab"])
        self.assertTrue(state["faq"] and state["faq"][0]["q"].startswith("Где взять Premiere"))
        groups = [g["title"] for g in state["materials"]]
        self.assertEqual(groups, ["Авторский лут"])

    async def test_file_sending_checks_access(self):
        await db.grant_course_access(5, ["shooting"], None)
        status, _ = await self.post("/api/course/file", 5, key="cut_07_eksport")
        self.assertEqual(status, 403)
        status, body = await self.post("/api/course/file", 5, key="lut")
        self.assertEqual((status, body["sent"]), (200, 1))
        self.assertEqual(self.bot.docs[-1][:2], (5, "fid-MALIMABI LUT.cube"))
        await db.grant_course_access(6, ["editing"], None)
        status, body = await self.post("/api/course/file", 6, lesson="editing:7")
        self.assertEqual((status, body["sent"]), (200, 2))
        status, _ = await self.post("/api/course/file", 6, key="nope")
        self.assertEqual(status, 404)

    async def test_parallel_saves_keep_everything(self):
        infos = [(cf.recognize(n), n) for n in NAMES]
        await asyncio.gather(*(cf.save(i, "fid-" + n, n, 1) for i, n in infos))
        data = await cf.all_files()
        self.assertTrue(all(i["key"] in data for i, _ in infos))

    async def test_manual_files_order_rename_and_groups(self):
        await db.grant_course_access(5, ["editing"], None)
        # незнакомый файл — в урок вручную, встаёт последним
        key = await cf.add(cf.lesson_place("editing", 7), "Чек-лист экспорта", "fid-x", "Check List.pdf", 10)
        self.assertEqual(key, "check_list")
        data = await cf.all_files()
        self.assertEqual(cf.keys_in(data, "editing:7"), ["cut_07_eksport", "seyfzona_tiktok_1080x1920", key])
        self.assertTrue(await cf.move_up(key))
        self.assertFalse(await cf.move_up("cut_07_eksport"))
        data = await cf.all_files()
        self.assertEqual(cf.keys_in(data, "editing:7"), ["cut_07_eksport", key, "seyfzona_tiktok_1080x1920"])
        # переименовали знакомый файл — новая версия с тем же именем название не сбрасывает
        await cf.update("cut_07_eksport", title="Экспорт")
        await cf.save(cf.recognize("cut_07_eksport.pdf"), "fid-new", "cut_07_eksport.pdf", 5)
        data = await cf.all_files()
        self.assertEqual((data["cut_07_eksport"]["title"], data["cut_07_eksport"]["file_id"]), ("Экспорт", "fid-new"))
        # новая группа материалов — только монтажу; перенос урока в неё
        await cf.move(key, cf.material_place(data, "g1", "Пресеты цвета", ["editing"]))
        await cf.update(key, note="для Premiere")
        state = await course_handler._app_state({"id": 5}, "bot")
        l7 = next(s for s in state["sections"] if s["slug"] == "editing")["lessons"][6]
        self.assertEqual([f["title"] for f in l7["files"]], ["Экспорт", "Сейф-зона · TikTok"])
        g = state["materials"][-1]
        self.assertEqual((g["group"], g["badge"], g["title"]), ("g1", "ПЦ", "Пресеты цвета"))
        self.assertEqual(g["items"], [{"key": key, "title": "Чек-лист экспорта", "note": "для Premiere"}])
        await db.grant_course_access(6, ["shooting"], None)
        state = await course_handler._app_state({"id": 6}, "bot")
        self.assertNotIn("g1", [g["group"] for g in state["materials"]])
        status, _ = await self.post("/api/course/file", 6, key=key)
        self.assertEqual(status, 403)
        # перенос в существующую группу берёт её подпись; удаление
        data = await cf.all_files()
        place = cf.material_place(data, "g1")
        self.assertEqual((place["group_title"], place["sections"]), ("Пресеты цвета", ["editing"]))
        self.assertEqual(cf.material_place(data, "lut")["sections"], ["shooting", "editing"])
        self.assertTrue(await cf.remove(key))
        self.assertNotIn("g1", cf.groups(await cf.all_files()))

    async def test_view_recorded_only_for_owner(self):
        await db.grant_course_access(5, ["editing"], None)
        status, _ = await self.post("/api/course/view", 5, section="shooting", lesson=1)
        self.assertEqual(status, 403)
        status, _ = await self.post("/api/course/view", 5, section="editing", lesson=2)
        self.assertEqual(status, 200)
        self.assertEqual(await db.get_lesson_views(5), {"editing": [2]})
        status, _ = await self.post("/api/course/view", 5, section="editing", lesson=99)
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
