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
"""
import json
import re
import unicodedata

import database as db

S_FILES = "course_files"

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
    "shot_05_dvizhenie": ("shooting", 5, "Приёмы и движение камеры"),
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
    "lut": (["shooting", "editing"], "lut", "Авторский лут", ".cube · для Blackmagic и Premiere"),
    "playhead_mac_arm": (["editing"], "playhead", "Mac на M1–M4", "Apple Silicon · .dmg"),
    "playhead_mac_intel": (["editing"], "playhead", "Mac на Intel", ".dmg"),
    "playhead_windows": (["editing"], "playhead", "Windows", ".exe"),
    "playhead_guide": (["editing"], "playhead", "Как установить", "Инструкция PDF"),
    "library": (["editing"], "library", "MALIMABI Library", "Библиотека для монтажа · .zip"),
    "quest": (["editing"], "quest", "Квест «Собери свой ролик»", "Задание PDF"),
}

# Порядок и подписи групп во вкладке «Материалы»
MATERIAL_GROUPS = {
    "lut": ("LUT", "Авторский лут"),
    "playhead": ("PH", "Playhead"),
    "library": ("LIB", "MALIMABI Library"),
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


async def save(info: dict, file_id: str, file_name: str, size: int) -> bool:
    """Запомнить файл. True — заменили уже загруженный."""
    data = await all_files()
    replaced = info["key"] in data
    data[info["key"]] = info | {"file_id": file_id, "file_name": file_name, "size": size}
    await db.set_setting(S_FILES, json.dumps(data, ensure_ascii=False))
    return replaced


async def remove(key: str) -> bool:
    data = await all_files()
    if key not in data:
        return False
    del data[key]
    await db.set_setting(S_FILES, json.dumps(data, ensure_ascii=False))
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
