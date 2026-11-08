"""Файлы курса (шпаргалки PDF, шаблоны, лут, Playhead, Library) — в Telegram.

Админ шлёт файлы клиентскому боту после /coursefiles, бот узнаёт их по
имени файла и запоминает file_id. Ученик жмёт файл в мини-приложении — бот
присылает его в чат (file_id не зависит от размера, так что и 90 МБ Playhead
уходят без скачивания на сервер). file_id привязан к боту: файлы, отправленные
админскому боту (malimadmins), клиентский бот прислать не сможет.

Где файл: lesson (урок раздела) или material (вкладка «Материалы»). Хранение —
настройка course_files: {ключ: {...}}, ключ — имя файла в нижнем регистре,
у материалов с версиями (Library, Playhead) — постоянный ключ слота, чтобы
новая версия заменяла старую.

Что не узнали по имени, админ раскладывает сам кнопками (handlers/course_files.py):
урок или группа материалов, название, порядок. Ручные правки (название, место,
порядок) остаются, когда тот же файл присылают заново новой версией.
"""
import asyncio
import json
import re
import time
import unicodedata

import database as db

S_FILES = "course_files"
# Файлы пачкой приходят почти одновременно — без блокировки параллельные
# «прочитал → дописал → сохранил» затирали друг друга (07.11 из 26 осталось 10)
_lock = asyncio.Lock()

# Шпаргалки и шаблоны уроков: имя файла (без расширения) → (раздел, урок, название)
LESSON_FILES = {
    "00_nastroyki_blackmagic": ("shooting", 1, "Настройки Blackmagic Camera"),
    "01_balans_belogo": ("shooting", 1, "Баланс белого"),
    "02_vyderzhka": ("shooting", 1, "Выдержка"),
    "03_iso": ("shooting", 1, "ISO без шума"),
    "04_false_color": ("shooting", 1, "False color"),
    "shot_02_oborudovanie": ("shooting", 2, "Оборудование"),
    "shot_03_linzy": ("shooting", 3, "Линзы"),
    "shot_04_pod_lut": ("shooting", 4, "Съёмка под лут"),
    "shot_05_priemy": ("shooting", 5, "Мои приёмы"),
    "shot_05_cheklist": ("shooting", 5, "Чек-лист съёмки"),
    "shot_05_zum_stabilizaciya": ("shooting", 5, "Зум и стабилизация"),
    "cut_01_english": ("editing", 1, "Premiere по-английски"),
    "cut_01_hotkeys": ("editing", 1, "Горячие клавиши"),
    "cut_02_otbor": ("editing", 2, "Отбор"),
    "cut_03_razbor": ("editing", 3, "Разбор монтажа"),
    "cut_04_zvuk": ("editing", 4, "Музыка и саунд-дизайн"),
    "cut_05_cvet": ("editing", 5, "Цвет"),
    "cut_06_neyronka": ("editing", 6, "Нейросети"),
    "cut_07_eksport": ("editing", 7, "Экспорт и Topaz"),
    "cut_07_seyfzony": ("editing", 7, "Безопасные зоны"),
    "seyfzona_all_1080x1920": ("editing", 7, "Сейф-зона · все платформы"),
    "seyfzona_instagram_reels_1080x1920": ("editing", 7, "Сейф-зона · Instagram Reels"),
    "seyfzona_telegram_stories_1080x1920": ("editing", 7, "Сейф-зона · Telegram Stories"),
    "seyfzona_tiktok_1080x1920": ("editing", 7, "Сейф-зона · TikTok"),
    "seyfzona_vk_klipy_1080x1920": ("editing", 7, "Сейф-зона · VK Клипы"),
    "seyfzona_youtube_shorts_1080x1920": ("editing", 7, "Сейф-зона · YouTube Shorts"),
}

# Материалы: слот → (разделы, кому положено; группа во вкладке; название; пояснение)
MATERIAL_SLOTS = {
    "lut": (["shooting", "editing"], "lut", "MALIMABI LUT", ".cube · для Blackmagic Camera и Premiere"),
    "playhead_mac_arm": (["editing"], "playhead", "Mac на M1–M4", "Apple Silicon · .dmg"),
    "playhead_mac_intel": (["editing"], "playhead", "Mac на Intel", ".dmg"),
    "playhead_windows": (["editing"], "playhead", "Windows", ".exe"),
    "playhead_guide": (["editing"], "playhead", "Как установить", "Инструкция PDF"),
    "library": (["editing"], "library", "MALIMABI Library", "Панель для Premiere с файлами MALIMABI · Mac и Windows"),
    "quest": (["editing"], "quest", "Квест «Собери свой ролик»", "Задание PDF"),
}

# Порядок и подписи групп во вкладке «Материалы»
MATERIAL_GROUPS = {
    "lut": ("LUT", "Авторский лут"),
    "playhead": ("PH", "Playhead"),
    "library": ("LIB", "Library"),
    "quest": ("Q", "Квест"),
}

_MATERIAL_RULES = [
    (re.compile(r"\.cube$"), "lut"),
    (re.compile(r"^playhead.*(arm64|apple).*\.dmg$"), "playhead_mac_arm"),
    (re.compile(r"^playhead.*intel.*\.dmg$"), "playhead_mac_intel"),
    (re.compile(r"^playhead.*\.(exe|msi)$"), "playhead_windows"),
    (re.compile(r"^playhead.*(ustanovka|install).*\.pdf$"), "playhead_guide"),
    (re.compile(r"^malimabi[_ -]?library.*\.zip$"), "library"),
    (re.compile(r"^quest.*\.pdf$"), "quest"),
]


def _norm(name: str) -> str:
    return unicodedata.normalize("NFC", (name or "").strip()).lower()


def recognize(file_name: str) -> dict | None:
    """Куда относится файл по имени; None — не узнали."""
    name = _norm(file_name)
    stem = name.rsplit(".", 1)[0]
    if stem in LESSON_FILES:
        section, n, title = LESSON_FILES[stem]
        return {"key": stem, "place": "lesson", "section": section, "lesson": n, "title": title}
    for rule, slot in _MATERIAL_RULES:
        if rule.search(name):
            sections, group, title, note = MATERIAL_SLOTS[slot]
            return {"key": slot, "place": "material", "sections": sections, "group": group,
                    "title": title, "note": note}
    return None


async def all_files() -> dict:
    raw = await db.get_setting(S_FILES)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def place_of(f: dict) -> str:
    """Где лежит файл: «editing:3» — урок, «mat:playhead» — группа материалов."""
    if f.get("place") == "lesson":
        return f"{f['section']}:{f['lesson']}"
    return f"mat:{f.get('group') or ''}"


def _rank(data: dict, key: str) -> tuple:
    f = data[key]
    default = lesson_order(key) if f.get("place") == "lesson" else material_order(key)
    return (f.get("order", default), f.get("added", 0), key)


def keys_in(data: dict, place: str) -> list[str]:
    """Файлы одного урока или одной группы материалов — в порядке показа."""
    return sorted((k for k, f in data.items() if place_of(f) == place), key=lambda k: _rank(data, k))


def _put_last(data: dict, key: str) -> None:
    others = [k for k in keys_in(data, place_of(data[key])) if k != key]
    data[key]["order"] = (_rank(data, others[-1])[0] + 1) if others else 0


def groups(data: dict) -> dict[str, tuple[str, str]]:
    """Группы материалов по порядку: известные, потом созданные админом. {ключ: (значок, название)}"""
    out = dict(MATERIAL_GROUPS)
    for f in sorted(data.values(), key=lambda f: f.get("added", 0)):
        g = f.get("group")
        if f.get("place") == "material" and g and g not in out:
            out[g] = (f.get("group_badge") or "•", f.get("group_title") or g)
    return out


def group_sections(data: dict, group: str) -> list[str]:
    """Кому положены материалы группы: как у файлов в ней, иначе как у известных слотов."""
    secs = {s for f in data.values() if f.get("place") == "material" and f.get("group") == group
            for s in f.get("sections") or []}
    secs = secs or {s for secs_, g, _, _ in MATERIAL_SLOTS.values() if g == group for s in secs_}
    return [s for s in ("shooting", "editing") if s in secs] or ["shooting", "editing"]


def badge_for(title: str) -> str:
    words = re.findall(r"\w+", title)
    if len(words) > 1:
        return "".join(w[0] for w in words[:3]).upper()
    return (words[0][:3] if words else "•").upper()


def new_key(data: dict, file_name: str) -> str:
    stem = re.sub(r"\s+", "_", _norm(file_name).rsplit(".", 1)[0])[:60] or "file"
    key, n = stem, 2
    while key in data:
        key, n = f"{stem}_{n}", n + 1
    return key


def lesson_place(section: str, lesson: int) -> dict:
    return {"place": "lesson", "section": section, "lesson": lesson}


def material_place(data: dict, group: str, title: str = "", sections: list[str] | None = None) -> dict:
    """Место в группе материалов; title и sections — для новой группы."""
    place = {"place": "material", "group": group, "sections": sections or group_sections(data, group)}
    if not title:
        # в существующую созданную админом группу: подпись группы — как у соседей
        same = next((f for f in data.values() if f.get("group") == group and f.get("group_title")), None)
        title = same["group_title"] if same else ""
    if title and group not in MATERIAL_GROUPS:
        place |= {"group_title": title, "group_badge": badge_for(title)}
    return place


async def _write(data: dict) -> None:
    await db.set_setting(S_FILES, json.dumps(data, ensure_ascii=False))


async def save(info: dict, file_id: str, file_name: str, size: int) -> bool:
    """Запомнить файл. True — заменили уже загруженный: название, место и порядок
    после ручных правок остаются как были, меняется только сам файл."""
    file = {"file_id": file_id, "file_name": file_name, "size": size}
    async with _lock:
        data = await all_files()
        replaced = info["key"] in data
        if replaced:
            data[info["key"]] |= file
        else:
            data[info["key"]] = info | file | {"added": time.time()}
            # известные файлы стоят в порядке списка выше, пока порядок не меняли руками
            if any("order" in data[k] for k in keys_in(data, place_of(data[info["key"]]))):
                _put_last(data, info["key"])
        await _write(data)
    return replaced


async def add(place: dict, title: str, file_id: str, file_name: str, size: int) -> str:
    """Новый файл, разложенный админом вручную. Возвращает ключ."""
    async with _lock:
        data = await all_files()
        key = new_key(data, file_name)
        data[key] = place | {"key": key, "title": title, "file_id": file_id, "file_name": file_name,
                             "size": size, "added": time.time()}
        _put_last(data, key)
        await _write(data)
    return key


async def replace_file(key: str, file_id: str, file_name: str, size: int) -> bool:
    async with _lock:
        data = await all_files()
        if key not in data:
            return False
        data[key] |= {"file_id": file_id, "file_name": file_name, "size": size}
        await _write(data)
    return True


async def update(key: str, **fields) -> bool:
    """Название, пояснение. None — убрать поле."""
    async with _lock:
        data = await all_files()
        if key not in data:
            return False
        for k, v in fields.items():
            if v is None:
                data[key].pop(k, None)
            else:
                data[key][k] = v
        await _write(data)
    return True


_PLACE_FIELDS = ("place", "section", "lesson", "group", "sections", "group_title", "group_badge", "order")


async def move(key: str, place: dict) -> bool:
    """Перенести файл в другой урок или группу — встаёт последним."""
    async with _lock:
        data = await all_files()
        if key not in data:
            return False
        f = {k: v for k, v in data[key].items() if k not in _PLACE_FIELDS}
        if place.get("place") == "lesson":
            f.pop("note", None)
        data[key] = f | place
        _put_last(data, key)
        await _write(data)
    return True


async def move_up(key: str) -> bool:
    """На одну позицию выше внутри урока или группы. False — уже первый."""
    async with _lock:
        data = await all_files()
        if key not in data:
            return False
        keys = keys_in(data, place_of(data[key]))
        i = keys.index(key)
        if i == 0:
            return False
        keys[i - 1], keys[i] = keys[i], keys[i - 1]
        for n, k in enumerate(keys):
            data[k]["order"] = n
        await _write(data)
    return True


async def remove(key: str) -> bool:
    async with _lock:
        data = await all_files()
        if key not in data:
            return False
        del data[key]
        await _write(data)
    return True


def lesson_order(key: str) -> int:
    keys = list(LESSON_FILES)
    return keys.index(key) if key in keys else len(keys)


def material_order(key: str) -> int:
    keys = list(MATERIAL_SLOTS)
    return keys.index(key) if key in keys else len(keys)


def expected_missing(data: dict) -> list[str]:
    """Каких известных файлов ещё нет (квест — по готовности, не напоминаем)."""
    short = {"shooting": "Съёмка", "editing": "Монтаж"}
    missing = [f"{short[s]} {n} · {t}" for k, (s, n, t) in LESSON_FILES.items() if k not in data]
    missing += [MATERIAL_SLOTS[k][2] + (f" ({MATERIAL_SLOTS[k][3]})" if MATERIAL_SLOTS[k][1] == "playhead" else "")
                for k in MATERIAL_SLOTS if k not in data and k != "quest"]
    return missing


def size_text(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.0f} МБ"
    return f"{max(1, size // 1024)} КБ"
