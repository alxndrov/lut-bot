"""Видео уроков в Kinescope с DRM и нашим авторизационным бэкендом.

Плеер урока в мини-приложении встраивается с параметром drmauthtoken —
подписанным токеном «пользователь + срок». Когда зритель жмёт «play», Kinescope
спрашивает POST /kinescope/drm (handlers/kinescope_drm.py): мы проверяем подпись,
что ролик — урок курса и что у пользователя открыт его раздел, и отвечаем
200 или 403. Без 200 сервер лицензий не отдаст ключ — зашифрованное видео не
проиграется ни по ссылке, ни после скачивания.

Ключи не хранятся отдельно: подпись токена и пароль Basic Auth для Kinescope
выводятся из BOT_TOKEN. Соответствие «урок → ролик» — настройка
kinescope_videos, её заполняет админ командой /kinescope в malimadmins.
Документация: https://docs.kinescope.ru/instrukcii-dlya-razrabotchikov/avtorizatsionnyy-bekend-dlya-kontrolya-dostupa-k-video/
"""
import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlencode

import aiohttp

import config
import database as db

API = "https://api.kinescope.io/v1"
TIMEOUT = aiohttp.ClientTimeout(total=20)
S_VIDEOS = "kinescope_videos"   # {"shooting": {"1": {"id": uuid, "embed": url, "title": ...}}}
TOKEN_TTL = 12 * 3600            # урок открыт в приложении — токена хватает на полдня
DRM_USER = "malimabi"
DRM_PATH = "/kinescope/drm"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class KinescopeError(Exception):
    pass


def _key(purpose: str) -> bytes:
    return hmac.new(config.BOT_TOKEN.encode(), f"kinescope:{purpose}".encode(),
                    hashlib.sha256).digest()


def drm_password() -> str:
    return _key("basic").hex()[:32]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_token(user_id: int, now: float | None = None) -> str:
    payload = _b64(json.dumps({"u": user_id, "e": int((now or time.time()) + TOKEN_TTL)},
                              separators=(",", ":")).encode())
    sig = _b64(hmac.new(_key("token"), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_token(token: str, now: float | None = None) -> int | None:
    """user_id из токена или None (подделан, испорчен, просрочен)."""
    try:
        payload, sig = (token or "").split(".")
        good = _b64(hmac.new(_key("token"), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data["e"] < (now or time.time()):
            return None
        return int(data["u"])
    except Exception:
        return None


# ── Уроки ↔ ролики ─────────────────────────────────────────────────────────────

async def videos() -> dict:
    raw = await db.get_setting(S_VIDEOS)
    return json.loads(raw) if raw else {}


async def set_video(section: str, n: int, video: dict | None):
    data = await videos()
    lessons = data.setdefault(section, {})
    if video:
        lessons[str(n)] = video
    else:
        lessons.pop(str(n), None)
    await db.set_setting(S_VIDEOS, json.dumps(data, ensure_ascii=False))


async def lesson_by_video(video_id: str) -> tuple[str, int] | None:
    for section, lessons in (await videos()).items():
        for n, v in lessons.items():
            if v.get("id", "").lower() == (video_id or "").lower():
                return section, int(n)
    return None


def embed_url(video: dict, user_id: int) -> str:
    """drmauthtoken — для нашего бэкенда; watermark — динамический водяной знак
    с Telegram ID зрителя (оферта, п. 7.9.3): по записи экрана видно, чей доступ."""
    return (f"{video['embed']}?" + urlencode({"drmauthtoken": make_token(user_id),
                                              "watermark": f"ID {user_id}"}))


# ── API Kinescope ──────────────────────────────────────────────────────────────

async def _api(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    if not config.KINESCOPE_API_TOKEN:
        raise KinescopeError("нет KINESCOPE_API_TOKEN в .env")
    headers = {"Authorization": f"Bearer {config.KINESCOPE_API_TOKEN}"}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        async with s.request(method, API + path, json=body, params=params, headers=headers) as r:
            text = await r.text()
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = {"error": text[:300]}
            if r.status >= 400:
                raise KinescopeError(f"{r.status}: {data.get('error') or data}")
            return data


async def find_video(ref: str) -> dict:
    """Ролик по UUID или по ссылке (kinescope.io/<id> или /embed/<id>):
    {"id": UUID — его присылает Kinescope в запросе лицензии,
     "embed": ссылка для iframe, "title": название}."""
    ref = ref.strip()
    m = UUID_RE.search(ref)
    if m:
        v = (await _api("GET", f"/videos/{m.group(0)}")).get("data") or {}
    else:
        short = ref.rstrip("/").split("/")[-1].split("?")[0]
        v, page = None, 1
        while v is None:
            data = await _api("GET", "/videos", params={"page": page, "per_page": 100})
            items = data.get("data") or []
            v = next((x for x in items if short and short in
                      f"{x.get('embed_link', '')} {x.get('play_link', '')}"), None)
            if v is None and (len(items) < 100 or page >= 20):
                raise KinescopeError(f"ролик {short} не найден в Kinescope")
            page += 1
    if not v.get("id"):
        raise KinescopeError("Kinescope не вернул ролик")
    embed = v.get("embed_link") or f"https://kinescope.io/embed/{v['id']}"
    return {"id": v["id"], "embed": embed.split("?")[0], "title": v.get("title") or "",
            "project_id": v.get("project_id") or ""}


async def setup_drm_auth(project_id: str, url: str, strict: bool) -> dict:
    """Подключает наш эндпоинт как авторизационный бэкенд проекта."""
    return await _api("PUT", f"/drm/auth/{project_id}",
                      {"url": url, "username": DRM_USER, "password": drm_password(),
                       "strict": strict})


async def get_drm_auth(project_id: str) -> dict:
    return await _api("GET", f"/drm/auth/{project_id}")
