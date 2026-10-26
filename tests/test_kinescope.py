import base64
import os
import tempfile
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import database as db
from handlers import kinescope_drm
from services import course as svc
from services import kinescope

VID = "7127f2d7-0e96-40d0-9a03-2e987c096466"


class TokenTest(unittest.TestCase):
    def test_roundtrip_tamper_expiry(self):
        t = kinescope.make_token(42, now=1000)
        self.assertEqual(kinescope.verify_token(t, now=1001), 42)
        payload, sig = t.split(".")
        self.assertIsNone(kinescope.verify_token(payload + "." + sig[:-2] + "AA", now=1001))
        self.assertIsNone(kinescope.verify_token("42", now=1001))          # голый user_id — нет
        self.assertIsNone(kinescope.verify_token(t, now=1000 + kinescope.TOKEN_TTL + 1))

    def test_embed_has_token_and_watermark(self):
        url = kinescope.embed_url({"embed": "https://kinescope.io/embed/abc"}, 42)
        self.assertTrue(url.startswith("https://kinescope.io/embed/abc?drmauthtoken="))
        self.assertIn("watermark=ID+42", url)


class DrmBackendTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "t.db")
        await db.init_db()
        await kinescope.set_video("editing", 2, {"id": VID, "embed": "https://kinescope.io/embed/x"})
        await db.grant_course_access(7, ["editing"], 1)
        app = web.Application()
        kinescope_drm.setup_web(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.auth = "Basic " + base64.b64encode(
            f"{kinescope.DRM_USER}:{kinescope.drm_password()}".encode()).decode()

    async def asyncTearDown(self):
        await self.client.close()
        db.DB_PATH = self.old
        self.tmp.cleanup()

    async def _ask(self, user_id, video=VID, auth=None):
        r = await self.client.post(kinescope.DRM_PATH, headers={"Authorization": auth or self.auth},
                                   json={"id": video, "type": "vod", "ip": "1.2.3.0",
                                         "token": kinescope.make_token(user_id) if user_id else ""})
        return r.status

    async def test_rules(self):
        with mock.patch.object(svc, "is_open", return_value=True):
            self.assertEqual(await self._ask(7), 200)                       # купил монтаж
            self.assertEqual(await db.get_lesson_views(7), {"editing": [2]})
            self.assertEqual(await self._ask(8), 403)                       # не покупал
            self.assertEqual(await self._ask(None), 403)                    # без токена
            self.assertEqual(await self._ask(7, video="00000000-0000-0000-0000-000000000000"), 403)
            self.assertEqual(await self._ask(7, auth="Basic eDp5"), 403)    # не Kinescope
        with mock.patch.object(svc, "is_open", return_value=False):
            self.assertEqual(await self._ask(7), 403)                       # курс ещё не стартовал


if __name__ == "__main__":
    unittest.main()
