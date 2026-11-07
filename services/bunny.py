"""Видео уроков в Bunny Stream.

Ролики Миша (или Даня) загружает прямо в библиотеку Bunny, сервер их не
хранит. Урок узнаём по названию ролика: «SHOT 01 BLACK MAGIC.mp4» — урок 1
съёмки, «CUT 3 razbor montaje.mp4» — урок 3 монтажа. Если у урока несколько
роликов (перезалили), берём последний готовый.

Плеер встраивается iframe-ом с подписанной ссылкой (Embed view token
authentication): sha256(ключ + id ролика + срок). Без подписи и с чужого
сайта (Allowed domains) ролик не играет. Список роликов кэшируем на пару
минут, чтобы не дёргать API на каждое открытие приложения.
"""
import asyncio
import hashlib
import logging
import re
import time

import aiohttp

import config

logger = logging.getLogger(__name__)

API = "https://video.bunnycdn.com"
EMBED = "https://iframe.mediadelivery.net/embed"
TIMEOUT = aiohttp.ClientTimeout(total=15)
CACHE_TTL = 120
TOKEN_TTL = 6 * 3600     # ссылка живёт полдня рабочего дня — урок успеют досмотреть
FINISHED = 4             # статус Bunny: закодирован, можно смотреть
STATUS = {0: "создан", 1: "загружен", 2: "обрабатывается", 3: "кодируется",
          4: "готов", 5: "ошибка", 6: "ошибка загрузки"}

TITLE_RE = re.compile(r"^\s*(shot|cut)[\s_-]*0*(\d{1,2})\b", re.I)
SECTION_BY_PREFIX = {"shot": "shooting", "cut": "editing"}

_cache: dict = {"at": 0.0, "videos": None}


class BunnyError(Exception):
    pass


def configured() -> bool:
    return bool(config.BUNNY_LIBRARY_ID and config.BUNNY_API_KEY)


def lesson_of(title: str) -> tuple[str, int] | None:
    m = TITLE_RE.match(title or "")
    if not m:
        return None
    return SECTION_BY_PREFIX[m.group(1).lower()], int(m.group(2))


async def fetch_all() -> list[dict]:
    if not configured():
        raise BunnyError("нет BUNNY_LIBRARY_ID / BUNNY_API_KEY в .env")
    items: list[dict] = []
    page = 1
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        while True:
            url = f"{API}/library/{config.BUNNY_LIBRARY_ID}/videos"
            async with session.get(url, params={"page": page, "itemsPerPage": 100, "orderBy": "date"},
                                   headers={"AccessKey": config.BUNNY_API_KEY,
                                            "accept": "application/json"}) as r:
                if r.status != 200:
                    raise BunnyError(f"API Bunny ответил {r.status}: {(await r.text())[:200]}")
                data = await r.json()
            items += data.get("items") or []
            if len(items) >= int(data.get("totalItems") or 0) or not data.get("items"):
                return items
            page += 1


def chapters_of(v: dict) -> list[dict]:
    """Главы ролика (их ставят в Bunny: Chapters) — [{"start": сек, "title"}] по порядку."""
    out = []
    for c in v.get("chapters") or []:
        title = str(c.get("title") or "").strip()
        try:
            start = max(0, int(c.get("start") or 0))
        except (TypeError, ValueError):
            continue
        if title:
            out.append({"start": start, "title": title})
    return sorted(out, key=lambda c: c["start"])


def time_text(seconds: int) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _pick(items: list[dict]) -> dict:
    """{раздел: {"N": ролик}} — по уроку лучший ролик: готовый и самый свежий."""
    out: dict[str, dict[str, dict]] = {}
    for v in items:
        lesson = lesson_of(v.get("title") or "")
        if not lesson:
            continue
        section, n = lesson
        video = {"id": v["guid"], "title": v.get("title") or "", "status": int(v.get("status") or 0),
                 "progress": int(v.get("encodeProgress") or 0), "length": int(v.get("length") or 0),
                 "width": int(v.get("width") or 0), "height": int(v.get("height") or 0),
                 "created": v.get("dateUploaded") or "", "chapters": chapters_of(v)}
        cur = out.setdefault(section, {}).get(str(n))
        better = (cur is None
                  or (video["status"] == FINISHED) > (cur["status"] == FINISHED)
                  or ((video["status"] == FINISHED) == (cur["status"] == FINISHED)
                      and video["created"] > cur["created"]))
        if better:
            if cur is not None:
                video["duplicates"] = cur.get("duplicates", 0) + 1
            out[section][str(n)] = video
        else:
            cur["duplicates"] = cur.get("duplicates", 0) + 1
    return out


async def _load_chapters(picked: dict) -> None:
    """Главы есть только в карточке ролика (в общем списке Bunny отдаёт пусто) —
    дочитываем по уроку. Не вышло — урок просто без содержания."""
    targets = [v for lessons in picked.values() for v in lessons.values() if v["status"] == FINISHED]
    if not targets:
        return
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async def one(video):
            url = f"{API}/library/{config.BUNNY_LIBRARY_ID}/videos/{video['id']}"
            try:
                async with session.get(url, headers={"AccessKey": config.BUNNY_API_KEY,
                                                     "accept": "application/json"}) as r:
                    if r.status == 200:
                        video["chapters"] = chapters_of(await r.json())
            except (aiohttp.ClientError, TimeoutError, ValueError) as e:
                logger.warning(f"bunny: главы {video['title']} не получены: {e}")
        await asyncio.gather(*(one(v) for v in targets))


async def videos(force: bool = False) -> dict:
    """Ролики уроков; при сбое API — последний удачный список (или пусто)."""
    if not configured():
        return {}
    if not force and _cache["videos"] is not None and time.time() - _cache["at"] < CACHE_TTL:
        return _cache["videos"]
    try:
        picked = _pick(await fetch_all())
        await _load_chapters(picked)
        _cache["videos"] = picked
        _cache["at"] = time.time()
    except (BunnyError, aiohttp.ClientError, TimeoutError) as e:
        logger.warning(f"bunny: список роликов не получен: {e}")
        if force:
            raise BunnyError(str(e)) from e
        _cache["at"] = time.time() - CACHE_TTL + 30   # повторим через полминуты
    return _cache["videos"] or {}


def ready(video: dict | None) -> bool:
    return bool(video) and video["status"] == FINISHED


def embed_url(video_id: str, now: float | None = None) -> str:
    expires = int((now or time.time()) + TOKEN_TTL)
    params = "autoplay=false&preload=true&responsive=true"
    if config.BUNNY_TOKEN_KEY:
        token = hashlib.sha256(f"{config.BUNNY_TOKEN_KEY}{video_id}{expires}".encode()).hexdigest()
        params = f"token={token}&expires={expires}&" + params
    return f"{EMBED}/{config.BUNNY_LIBRARY_ID}/{video_id}?{params}"


def duration_text(seconds: int) -> str:
    if not seconds:
        return ""
    m = round(seconds / 60)
    return f"{m} мин" if m else f"{seconds} сек"
