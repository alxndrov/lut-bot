"""/coursefiles — загрузка файлов курса и сводка по видео уроков (клиентский бот, только админы).

Админ пишет /coursefiles в личке с клиентским ботом — на 30 минут включается
приём файлов: всё, что он пришлёт документом, бот раскладывает по урокам и
материалам по имени файла (services/course_files.py) и через несколько секунд
тишины присылает сводку. Файлы именно сюда, а не в malimadmins: file_id
привязан к боту, а ученикам файлы присылает клиентский бот.

/coursefiles - имя_файла — убрать файл; /coursefiles off — выключить приём.
"""
import asyncio
import logging
import time
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import config
from services import bunny
from services import course as svc
from services import course_files as cf

router = Router()
logger = logging.getLogger(__name__)

MODE_TTL = 30 * 60
SUMMARY_DELAY = 4

_mode: dict[int, float] = {}            # admin id → до какого времени принимаем файлы
_batch: dict[int, list[str]] = {}        # admin id → строки сводки текущей пачки
_tasks: dict[int, asyncio.Task] = {}


def _receiving(message: Message) -> bool:
    uid = message.from_user.id if message.from_user else 0
    return (message.chat.type == "private" and uid in config.ADMIN_IDS
            and _mode.get(uid, 0) > time.time())


async def video_status() -> list[str]:
    lines = ["🎬 <b>Видео уроков (Bunny)</b>"]
    if not bunny.configured():
        return lines + ["⚠️ Нет ключей Bunny в .env."]
    try:
        videos = await bunny.videos(force=True)
    except bunny.BunnyError as e:
        return lines + [f"⚠️ {escape(str(e))}"]
    for slug, program in svc.PROGRAM.items():
        lines.append(f"\n<b>{escape(program['name'])}</b>")
        for i, title in enumerate(program["lessons"], 1):
            v = videos.get(slug, {}).get(str(i))
            if not v:
                mark = "— нет ролика"
            elif bunny.ready(v):
                mark = f"✅ {bunny.duration_text(v['length'])}"
            else:
                st = bunny.STATUS.get(v["status"], str(v["status"]))
                mark = f"⏳ {st}" + (f" {v['progress']}%" if v["status"] in (2, 3) else "")
            if v and v.get("duplicates"):
                mark += f" (в Bunny ещё {v['duplicates']} с тем же номером — беру последний)"
            lines.append(f"{i}. {escape(title)} {mark}")
    lines.append("\nУрок узнаю по названию ролика: <code>SHOT 01 …</code>, <code>CUT 3 …</code>.")
    return lines


async def files_status() -> list[str]:
    data = await cf.all_files()
    lines = ["📎 <b>Файлы курса</b>"]
    for slug, program in svc.PROGRAM.items():
        for i, title in enumerate(program["lessons"], 1):
            here = sorted((k for k, f in data.items()
                           if f.get("place") == "lesson" and f["section"] == slug and f["lesson"] == i),
                          key=cf.lesson_order)
            if here:
                lines.append(f"{svc.SECTION_SHORT[slug]} {i}: " + ", ".join(escape(data[k]["title"]) for k in here))
    mats = sorted((k for k, f in data.items() if f.get("place") == "material"), key=cf.material_order)
    if mats:
        lines.append("Материалы: " + ", ".join(
            escape(data[k]["title"] if cf.MATERIAL_SLOTS[k][1] != "playhead" else f"Playhead · {data[k]['title']}")
            for k in mats))
    missing = cf.expected_missing(data)
    if missing:
        lines.append(f"\nЕщё не загружены ({len(missing)}): " + "; ".join(escape(m) for m in missing))
    else:
        lines.append("\nВсе известные файлы на месте ✅")
    return lines


@router.message(Command("coursefiles"), F.chat.type == "private")
async def cmd_coursefiles(message: Message, command: CommandObject):
    uid = message.from_user.id
    if uid not in config.ADMIN_IDS:
        return
    args = (command.args or "").strip()
    if args == "off":
        _mode.pop(uid, None)
        await message.answer("Приём файлов выключен.")
        return
    if args.startswith("- "):
        info = cf.recognize(args[2:].strip())
        ok = info and await cf.remove(info["key"])
        await message.answer("Убрал ✅" if ok else "Такого файла нет среди загруженных.")
        return
    _mode[uid] = time.time() + MODE_TTL
    text = ["📥 <b>Жду файлы курса — 30 минут</b>",
            "Перетащи сюда все PDF, PNG, лут, Playhead и Library <b>файлами</b> (без сжатия). "
            "Разложу по урокам сам, по именам файлов. Видео сюда не надо — оно в Bunny.",
            "Перезалить файл — просто пришли его ещё раз с тем же именем.", ""]
    text += await files_status()
    text += [""] + await video_status()
    await message.answer("\n".join(text), parse_mode="HTML")


async def _summary_later(bot: Bot, uid: int):
    await asyncio.sleep(SUMMARY_DELAY)
    lines = _batch.pop(uid, [])
    _tasks.pop(uid, None)
    if not lines:
        return
    text = ["📎 <b>Принял файлы</b>"] + lines + [""] + await files_status()
    try:
        await bot.send_message(uid, "\n".join(text), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"coursefiles: сводка не отправлена: {e}")


def _queue(bot: Bot, uid: int, line: str):
    _batch.setdefault(uid, []).append(line)
    old = _tasks.get(uid)
    if old and not old.done():
        old.cancel()
    _tasks[uid] = asyncio.create_task(_summary_later(bot, uid))


@router.message(F.document, _receiving)
async def on_document(message: Message, bot: Bot):
    uid = message.from_user.id
    _mode[uid] = time.time() + MODE_TTL
    doc = message.document
    name = doc.file_name or ""
    info = cf.recognize(name)
    if not info:
        _queue(bot, uid, f"❓ <code>{escape(name)}</code> — не понял, куда это. Напиши Claude, добавим правило.")
        return
    replaced = await cf.save(info, doc.file_id, name, doc.file_size or 0)
    where = (f"{svc.SECTION_SHORT[info['section']]} {info['lesson']}" if info["place"] == "lesson"
             else "Материалы")
    logger.info(f"coursefiles: {name} → {info['key']} ({where})")
    _queue(bot, uid, f"{'🔄' if replaced else '✅'} {escape(where)} · {escape(info['title'])}"
                     f" <i>({escape(name)})</i>")


@router.message(F.photo | F.video, _receiving)
async def on_compressed(message: Message, bot: Bot):
    _queue(bot, message.from_user.id,
           "⚠️ Пришло сжатой картинкой/видео — имени файла нет. Отправь <b>файлом</b> "
           "(в окне отправки сними «Сжать изображения»).")
