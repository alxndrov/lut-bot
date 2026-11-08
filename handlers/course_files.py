"""/coursefiles — загрузка файлов курса и сводка по видео уроков (клиентский бот, только админы).

Админ пишет /coursefiles в личке с клиентским ботом — на 30 минут включается
приём файлов: всё, что он пришлёт документом, бот раскладывает по урокам и
материалам по имени файла (services/course_files.py) и через несколько секунд
тишины присылает сводку. Файлы именно сюда, а не в malimadmins: file_id
привязан к боту, а ученикам файлы присылает клиентский бот.

Файл с незнакомым именем бот не теряет, а спрашивает кнопками, куда положить:
урок или группа «Материалов» (можно завести новую). Кнопка «✏️ Изменить файлы»
под сводкой — урок → файл → переименовать, перенести, заменить новой версией,
поднять выше, удалить.

/coursefiles - имя_файла — убрать файл; /coursefiles off — выключить приём.
"""
import asyncio
import itertools
import logging
import time
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

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
WAIT_TTL = 15 * 60
# Кнопки ссылаются на файлы короткими номерами (в callback_data влезает 64 байта):
# номер → ("key", ключ загруженного файла) | ("new", {file_id, file_name, size}) — ещё не разложен
_refs: dict[str, tuple[str, object]] = {}
_seq = itertools.count(1)
_wait: dict[int, tuple[str, str, float]] = {}       # admin id → (title | group, номер, до когда)
_new_group: dict[str, str] = {}                     # номер файла → название новой группы
_replace: dict[int, tuple[str, float]] = {}         # admin id → (ключ, до когда): следующий файл — новая версия


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


def _where(data: dict, key: str) -> str:
    f = data[key]
    if f.get("place") == "lesson":
        return f"{svc.SECTION_SHORT[f['section']]} {f['lesson']}"
    return f"Материалы · {cf.groups(data).get(f.get('group'), ('', f.get('group') or '?'))[1]}"


async def files_status() -> list[str]:
    data = await cf.all_files()
    lines = ["📎 <b>Файлы курса</b>"]
    for slug, program in svc.PROGRAM.items():
        for i, title in enumerate(program["lessons"], 1):
            here = cf.keys_in(data, f"{slug}:{i}")
            if here:
                lines.append(f"{svc.SECTION_SHORT[slug]} {i}: " + ", ".join(escape(data[k]["title"]) for k in here))
    for group, (_, gtitle) in cf.groups(data).items():
        here = cf.keys_in(data, f"mat:{group}")
        if here:
            lines.append(f"Материалы · {escape(gtitle)}: " + ", ".join(escape(data[k]["title"]) for k in here))
    missing = cf.expected_missing(data)
    if missing:
        lines.append(f"\nЕщё не загружены ({len(missing)}): " + "; ".join(escape(m) for m in missing))
    else:
        lines.append("\nВсе известные файлы на месте ✅")
    return lines


def _clear(uid: int) -> None:
    _wait.pop(uid, None)
    _replace.pop(uid, None)


def _key_for_name(data: dict, name: str) -> str:
    info = cf.recognize(name)
    if info:
        return info["key"]
    stem = cf.new_key({}, name)
    return stem if stem in data else next((k for k, f in data.items() if f.get("file_name") == name), "")


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
    _clear(uid)
    if args.startswith("- "):
        key = _key_for_name(await cf.all_files(), args[2:].strip())
        ok = key and await cf.remove(key)
        await message.answer("Убрал ✅" if ok else "Такого файла нет среди загруженных.")
        return
    _mode[uid] = time.time() + MODE_TTL
    text = ["📥 <b>Жду файлы курса — 30 минут</b>",
            "Перетащи сюда PDF, PNG, лут, Playhead, Library — что угодно — <b>файлами</b> (без сжатия). "
            "Знакомые разложу по урокам сам, по именам; про незнакомые спрошу, куда положить. "
            "Видео сюда не надо — оно в Bunny.",
            "Перезалить файл — пришли его ещё раз с тем же именем. Переименовать, перенести, "
            "удалить или поменять порядок — «✏️ Изменить файлы» внизу.", ""]
    text += await files_status()
    text += [""] + await video_status()
    await message.answer("\n".join(text), parse_mode="HTML", reply_markup=_kb(
        [("✏️ Изменить файлы и материалы", "cf:menu")]))


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
    target = _replace.pop(uid, None)
    if target and target[1] > time.time():
        # «🔄 Заменить файл» в карточке — новая версия, имя может быть любым
        if await cf.replace_file(target[0], doc.file_id, name, doc.file_size or 0):
            logger.info(f"coursefiles: {name} → {target[0]} (замена)")
            text, kb = await _card(_ref("key", target[0]), "🔄 <b>Заменил файл</b>")
            await message.answer(text, parse_mode="HTML", reply_markup=kb)
            return
    info = cf.recognize(name)
    if not info:
        ref = _ref("new", {"file_id": doc.file_id, "file_name": name, "size": doc.file_size or 0})
        await message.answer(f"📄 <code>{escape(name)}</code> — куда положить?", parse_mode="HTML",
                             reply_markup=_places_kb(f"cf:to:{ref}:", [("Не добавлять", f"cf:drop:{ref}")]))
        return
    replaced = await cf.save(info, doc.file_id, name, doc.file_size or 0)
    data = await cf.all_files()
    where = _where(data, info["key"]) if info["key"] in data else ""
    logger.info(f"coursefiles: {name} → {info['key']} ({where})")
    _queue(bot, uid, f"{'🔄' if replaced else '✅'} {escape(where)} · {escape(data[info['key']]['title'])}"
                     f" <i>({escape(name)})</i>")


@router.message(F.photo | F.video, _receiving)
async def on_compressed(message: Message, bot: Bot):
    _queue(bot, message.from_user.id,
           "⚠️ Пришло сжатой картинкой/видео — имени файла нет. Отправь <b>файлом</b> "
           "(в окне отправки сними «Сжать изображения»).")


# ── Редактор файлов: урок → файл → действие ──────────────────────────────────

def _kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=c) for t, c in row]
                                                 for row in rows if row])


def _ref(kind: str, value) -> str:
    if kind == "key":
        for r, (k, v) in _refs.items():
            if k == "key" and v == value:
                return r
    r = str(next(_seq))
    _refs[r] = (kind, value)
    return r


def _places_kb(prefix: str, extra: list[tuple[str, str]], counts: dict | None = None) -> InlineKeyboardMarkup:
    """Уроки обоих разделов и «Материалы»; callback — prefix + «editing:3» или «mat»."""
    def label(text: str, place: str) -> str:
        return f"{text} · {counts[place]}" if counts and counts.get(place) else text
    rows = []
    for slug, program in svc.PROGRAM.items():
        buttons = [(label(f"{svc.SECTION_SHORT[slug]} {i}", f"{slug}:{i}"), f"{prefix}{slug}:{i}")
                   for i in range(1, len(program["lessons"]) + 1)]
        rows += [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([(label("📦 Материалы", "mat"), f"{prefix}mat")])
    rows.append(extra)
    return _kb(*rows)


def _lesson_title(slug: str, n: int) -> str:
    return f"{svc.SECTION_SHORT[slug]} {n} · {svc.PROGRAM[slug]['lessons'][n - 1]}"


async def _card(ref: str, head: str = "") -> tuple[str, InlineKeyboardMarkup]:
    data = await cf.all_files()
    kind, key = _refs.get(ref, ("", ""))
    if kind != "key" or key not in data:
        return "Этого файла больше нет.", _kb([("← К урокам", "cf:menu")])
    f = data[key]
    keys = cf.keys_in(data, cf.place_of(f))
    if f.get("place") == "lesson":
        where, back = _lesson_title(f["section"], f["lesson"]), f"cf:pl:{f['section']}:{f['lesson']}"
    else:
        where, back = _where(data, key), "cf:pl:mat"
    lines = ([head] if head else []) + [f"📄 <b>{escape(f['title'])}</b>"]
    if f.get("note"):
        lines.append(f"<i>{escape(f['note'])}</i>")
    lines += [f"Где: {escape(where)} — {keys.index(key) + 1}-й из {len(keys)}",
              f"Файл: <code>{escape(f.get('file_name') or '')}</code>, {cf.size_text(f.get('size') or 0)}"]
    if f.get("place") == "material" and f.get("sections"):
        lines.append("Видят: " + " и ".join(svc.SECTION_SHORT[s] for s in f["sections"]).lower().capitalize())
    return "\n".join(lines), _kb(
        [("✏️ Название", f"cf:ren:{ref}"), ("📁 Перенести", f"cf:mv:{ref}")],
        [("🔄 Заменить файл", f"cf:rep:{ref}"), ("⬆️ Выше", f"cf:up:{ref}")],
        [("📤 Прислать мне", f"cf:send:{ref}"), ("🗑 Удалить", f"cf:del:{ref}")],
        [("← Назад", back)])


async def _place_list(place: str, head: str = "") -> tuple[str, InlineKeyboardMarkup]:
    data = await cf.all_files()
    rows = []
    if place == "mat":
        lines = ["📦 <b>Материалы</b>"]
        for group, (_, gtitle) in cf.groups(data).items():
            keys = cf.keys_in(data, f"mat:{group}")
            if keys:
                lines.append(f"\n<b>{escape(gtitle)}</b>")
                lines += [f"{i}. {escape(data[k]['title'])}" for i, k in enumerate(keys, 1)]
                rows += [[(f"{gtitle} · {data[k]['title']}"[:60], f"cf:card:{_ref('key', k)}")] for k in keys]
    else:
        slug, _, n = place.partition(":")
        keys = cf.keys_in(data, place)
        lines = [f"📚 <b>{escape(_lesson_title(slug, int(n)))}</b>"]
        lines += [f"{i}. {escape(data[k]['title'])}" for i, k in enumerate(keys, 1)] or ["Файлов нет."]
        rows += [[(f"{i}. {data[k]['title']}"[:60], f"cf:card:{_ref('key', k)}")] for i, k in enumerate(keys, 1)]
    lines.append("\nНажми на файл, чтобы изменить. Добавить новый — просто пришли его сюда файлом.")
    rows.append([("← К урокам", "cf:menu")])
    return "\n".join(([head] if head else []) + lines), _kb(*rows)


async def _menu() -> tuple[str, InlineKeyboardMarkup]:
    data = await cf.all_files()
    counts: dict[str, int] = {}
    for f in data.values():
        place = "mat" if f.get("place") == "material" else cf.place_of(f)
        counts[place] = counts.get(place, 0) + 1
    return ("✏️ <b>Файлы курса</b>\nВыбери урок или материалы (цифра — сколько файлов). "
            "Новый файл — просто пришли его сюда, спрошу, куда положить."), \
        _places_kb("cf:pl:", [], counts)


async def _show(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


def _admin_cb(callback: CallbackQuery) -> bool:
    return bool(callback.data and callback.data.startswith("cf:")
                and callback.from_user and callback.from_user.id in config.ADMIN_IDS)


async def _put(callback: CallbackQuery, ref: str, place: dict):
    """Положить новый файл или перенести загруженный — и показать карточку."""
    kind, value = _refs.get(ref, ("", None))
    if kind == "new":
        title = value["file_name"].rsplit(".", 1)[0] if "." in value["file_name"] else value["file_name"]
        key = await cf.add(place, title.strip() or "Файл", value["file_id"], value["file_name"], value["size"])
        _refs[ref] = ("key", key)
        logger.info(f"coursefiles: {value['file_name']} → {key} (вручную)")
        head = "✅ <b>Добавил.</b> Название взял из имени файла — поправь, если надо."
    elif kind == "key" and await cf.move(value, place):
        head = "✅ <b>Перенёс</b> — встал последним."
    else:
        await _show(callback, "Кнопка устарела — открой /coursefiles заново.", _kb())
        return
    await _show(callback, *(await _card(ref, head)))


@router.callback_query(_admin_cb)
async def on_editor(callback: CallbackQuery, bot: Bot):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    action, ref = parts[1], parts[2] if len(parts) > 2 else ""
    if action not in ("ren", "rep"):
        _clear(uid)
    _mode[uid] = time.time() + MODE_TTL   # пока в редакторе — файлы принимаем
    kind, value = _refs.get(ref, ("", None))
    if ref and not kind and action not in ("pl",):
        await callback.answer("Кнопка устарела — открой /coursefiles заново", show_alert=True)
        return
    await callback.answer()
    if action == "menu":
        await _show(callback, *(await _menu()))
    elif action == "pl":
        await _show(callback, *(await _place_list(":".join(parts[2:]))))
    elif action == "card":
        await _show(callback, *(await _card(ref)))
    elif action == "ren":
        data = await cf.all_files()
        f = data.get(value) if kind == "key" else None
        if not f:
            return await _show(callback, "Этого файла больше нет.", _kb([("← К урокам", "cf:menu")]))
        _wait[uid] = ("title", ref, time.time() + WAIT_TTL)
        text = f"✏️ Пришли новое название для «{escape(f['title'])}» — как его увидят ученики."
        if f.get("place") == "material":
            text += ("\n\nВторой строкой можно прислать подпись под названием"
                     + (f" (сейчас: «{escape(f['note'])}»)" if f.get("note") else "")
                     + ". Без второй строки подпись не меняю, «-» второй строкой — убрать подпись.")
        await _show(callback, text, _kb([("Отмена", f"cf:card:{ref}")]))
    elif action == "mv":
        await _show(callback, "📁 Куда перенести?",
                    _places_kb(f"cf:to:{ref}:", [("Отмена", f"cf:card:{ref}")]))
    elif action == "to":
        dest = parts[3:]
        if dest == ["mat"]:
            data = await cf.all_files()
            rows = [[(f"{badge} · {gtitle}", f"cf:grp:{ref}:{i}")]
                    for i, (badge, gtitle) in enumerate(cf.groups(data).values())]
            rows.append([("➕ Новая группа", f"cf:grp:{ref}:new")])
            rows.append([("Отмена", f"cf:card:{ref}" if kind == "key" else f"cf:drop:{ref}")])
            await _show(callback, "📦 В какую группу «Материалов»?", _kb(*rows))
        else:
            slug, n = dest[0], int(dest[1])
            await _put(callback, ref, cf.lesson_place(slug, n))
    elif action == "grp":
        if parts[3] == "new":
            _wait[uid] = ("group", ref, time.time() + WAIT_TTL)
            await _show(callback, "➕ Как назвать новую группу? Например: «Пресеты» или «Шаблоны».",
                        _kb([("Отмена", f"cf:card:{ref}" if kind == "key" else f"cf:drop:{ref}")]))
            return
        data = await cf.all_files()
        group = list(cf.groups(data))[int(parts[3])]
        await _put(callback, ref, cf.material_place(data, group))
    elif action == "sec":
        title = _new_group.pop(ref, "")
        if not title:
            return await _show(callback, "Кнопка устарела — открой /coursefiles заново.", _kb())
        secs = ["shooting", "editing"] if parts[3] == "both" else [parts[3]]
        data = await cf.all_files()
        await _put(callback, ref, cf.material_place(data, f"g{int(time.time())}", title, secs))
    elif action == "up":
        moved = await cf.move_up(value)
        await _show(callback, *(await _card(ref, "⬆️ <b>Поднял</b>" if moved else "Уже первый.")))
    elif action == "rep":
        data = await cf.all_files()
        if value not in data:
            return await _show(callback, "Этого файла больше нет.", _kb([("← К урокам", "cf:menu")]))
        _replace[uid] = (value, time.time() + WAIT_TTL)
        await _show(callback, f"🔄 Пришли новую версию «{escape(data[value]['title'])}» файлом — заменю. "
                              "Имя файла может быть любым, название и место останутся.",
                    _kb([("Отмена", f"cf:card:{ref}")]))
    elif action == "send":
        f = (await cf.all_files()).get(value)
        if f:
            await bot.send_document(uid, f["file_id"], caption=f["title"])
    elif action == "del":
        f = (await cf.all_files()).get(value)
        if f:
            await _show(callback, f"🗑 Удалить «{escape(f['title'])}»? Ученики перестанут его видеть.",
                        _kb([("🗑 Да, удалить", f"cf:rm:{ref}"), ("Отмена", f"cf:card:{ref}")]))
    elif action == "rm":
        data = await cf.all_files()
        f = data.get(value)
        if f and await cf.remove(value):
            logger.info(f"coursefiles: удалён {value}")
            place = f"{f['section']}:{f['lesson']}" if f.get("place") == "lesson" else "mat"
            await _show(callback, *(await _place_list(place, f"🗑 Удалил «{escape(f['title'])}»\n")))
    elif action == "drop":
        _refs.pop(ref, None)
        _new_group.pop(ref, None)
        await _show(callback, "Не добавил.", _kb())


def _waiting(message: Message) -> bool:
    uid = message.from_user.id if message.from_user else 0
    w = _wait.get(uid)
    return bool(message.chat.type == "private" and uid in config.ADMIN_IDS and w and w[2] > time.time()
                and message.text and not message.text.startswith("/"))


@router.message(F.text, _waiting)
async def on_text(message: Message):
    uid = message.from_user.id
    what, ref, _ = _wait.pop(uid)
    kind, value = _refs.get(ref, ("", None))
    lines = [x.strip() for x in message.text.strip().split("\n")]
    if what == "group":
        if not lines[0]:
            return
        _new_group[ref] = lines[0][:40]
        cancel = f"cf:card:{ref}" if kind == "key" else f"cf:drop:{ref}"
        await message.answer(f"Группа «{escape(lines[0][:40])}». Кому её показывать?", parse_mode="HTML",
                             reply_markup=_kb([("Съёмке", f"cf:sec:{ref}:shooting"),
                                               ("Монтажу", f"cf:sec:{ref}:editing"),
                                               ("Всем", f"cf:sec:{ref}:both")], [("Отмена", cancel)]))
        return
    if kind != "key" or not lines[0]:
        return
    fields: dict = {"title": lines[0][:80]}
    f = (await cf.all_files()).get(value)
    if f and f.get("place") == "material" and len(lines) > 1 and lines[1]:
        fields["note"] = None if lines[1] == "-" else lines[1][:80]
    await cf.update(value, **fields)
    text, kb = await _card(ref, "✏️ <b>Переименовал</b>")
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
