"""Kinescope: авторизационный бэкенд DRM и команда /kinescope для админов.

POST /kinescope/drm — Kinescope спрашивает при каждом запросе лицензии
(старт ролика): можно ли этому зрителю смотреть этот ролик. Отвечаем 200
или 403, тело Kinescope не читает. Проверяем Basic Auth (логин и пароль мы
сами передали Kinescope при подключении), подпись токена из drmauthtoken,
что ролик — урок курса, раздел открыт пользователю и курс стартовал (админам
— всегда, для проверки). Разрешённый старт записываем как просмотр урока.

/kinescope в malimadmins — какие уроки уже с роликами; /kinescope shooting 1
<ссылка или UUID ролика> — привязать ролик к уроку (бот найдёт его через API
Kinescope); /kinescope shooting 1 - — отвязать; /kinescope drm <ID проекта>
[strict] — подключить наш бэкенд к проекту Kinescope.
"""
import base64
import hmac
import logging
from html import escape

from aiohttp import web
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import config
import database as db
from services import course as svc
from services import kinescope

router = Router()   # админский бот
logger = logging.getLogger(__name__)

def _deny() -> web.Response:
    return web.Response(status=403)


def _basic_ok(request: web.Request) -> bool:
    expected = "Basic " + base64.b64encode(
        f"{kinescope.DRM_USER}:{kinescope.drm_password()}".encode()).decode()
    return hmac.compare_digest(request.headers.get("Authorization", ""), expected)


async def handle_drm(request: web.Request) -> web.Response:
    if not _basic_ok(request):
        logger.warning("kinescope drm: неверный Basic Auth")
        return _deny()
    try:
        body = await request.json()
    except Exception:
        return _deny()
    user_id = kinescope.verify_token(str(body.get("token") or ""))
    lesson = await kinescope.lesson_by_video(str(body.get("id") or ""))
    if not user_id or not lesson:
        logger.info(f"kinescope drm: отказ — токен {'ок' if user_id else 'плохой'}, "
                    f"ролик {body.get('id')} {'урок' if lesson else 'не урок'}")
        return _deny()
    section, n = lesson
    admin = user_id in config.ADMIN_IDS
    if not admin and not (svc.is_open() and section in await db.get_user_course_sections(user_id)):
        logger.info(f"kinescope drm: отказ {user_id} — {section} {n}")
        return _deny()
    await db.record_lesson_view(user_id, section, n)
    logger.info(f"kinescope drm: {user_id} смотрит {section} {n}")
    return web.Response(status=200)


def setup_web(app: web.Application):
    app.router.add_post(kinescope.DRM_PATH, handle_drm)


# ── /kinescope в malimadmins ───────────────────────────────────────────────────

async def _status() -> str:
    data = await kinescope.videos()
    lines = ["🎬 <b>Видео уроков (Kinescope)</b>", ""]
    for slug, program in svc.PROGRAM.items():
        lines.append(f"<b>{escape(program['name'])}</b>")
        for i, title in enumerate(program["lessons"], 1):
            v = data.get(slug, {}).get(str(i))
            lines.append(f"{i}. {escape(title)} — " + (f"✅ {escape(v.get('title') or v['id'])}"
                                                      if v else "❌ нет ролика"))
        lines.append("")
    lines += ["Привязать: <code>/kinescope shooting 1 ссылка</code> (раздел shooting или "
              "editing, номер урока, ссылка на ролик или его UUID).",
              "Отвязать: <code>/kinescope shooting 1 -</code>",
              "Подключить DRM-проверку к проекту: <code>/kinescope drm ID_проекта</code>"]
    if not config.KINESCOPE_API_TOKEN:
        lines += ["", "⚠️ Нет KINESCOPE_API_TOKEN в .env — привязка и подключение не сработают."]
    return "\n".join(lines)


@router.message(Command("kinescope"))
async def cmd_kinescope(message: Message, command: CommandObject):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    args = (command.args or "").split()
    try:
        if args and args[0] == "drm" and len(args) in (2, 3):
            url = config.PAY_BASE_URL.rstrip("/") + kinescope.DRM_PATH
            strict = len(args) == 3 and args[2] == "strict"
            await kinescope.setup_drm_auth(args[1], url, strict)
            current = await kinescope.get_drm_auth(args[1])
            logger.info(f"kinescope: DRM-проверка подключена к проекту {args[1]} (strict={strict})")
            await message.answer(f"✅ Проверка доступа подключена к проекту.\nАдрес: {escape(url)}\n"
                                 f"Строгий режим: {'да' if strict else 'нет'}\n"
                                 f"<code>{escape(str(current)[:300])}</code>", parse_mode="HTML")
            return
        if len(args) == 3 and args[0] in svc.PROGRAM and args[1].isdigit():
            section, n = args[0], int(args[1])
            if not 1 <= n <= len(svc.PROGRAM[section]["lessons"]):
                await message.answer("Нет такого урока.")
                return
            if args[2] == "-":
                await kinescope.set_video(section, n, None)
            else:
                video = await kinescope.find_video(args[2])
                await kinescope.set_video(section, n, video)
                logger.info(f"kinescope: {section} {n} → {video['id']}")
    except kinescope.KinescopeError as e:
        await message.answer(f"⚠️ Kinescope: {escape(str(e))}", parse_mode="HTML")
        return
    await message.answer(await _status(), parse_mode="HTML", disable_web_page_preview=True)
