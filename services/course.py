"""Курс Shot on iPhone + Cut by MALIMABI: цены по дате, разделы, initData.

Один товар (категория infobiz, в интерфейсе «Курс»), внутри — опции из
таблицы course_options: съёмка, монтаж, пакет. Опция открывает разделы
(course_access), пакет ещё и ставит в очередь на личный разбор.
"""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import config

MSK = timezone(timedelta(hours=3))

SECTIONS = {
    "shooting": "Shot on iPhone — съёмка",
    "editing": "Cut by MALIMABI — монтаж",
}
SECTION_SHORT = {"shooting": "Съёмка", "editing": "Монтаж"}

# Программа для мини-приложения — по оферте (раздел 7.1). Видео и длительности
# появятся на этапе 2 (видеохостинг); пока у уроков только названия.
PROGRAM = {
    "shooting": {
        "name": "Shot on iPhone",
        "lead": "Настраиваем камеру, собираем сет и снимаем себя без команды.",
        "lessons": [
            "Blackmagic Camera",
            "Оборудование и съёмка себя",
            "Линзы",
            "Как снимать под лут",
            "Мои приёмы и движение камеры",
        ],
    },
    "editing": {
        "name": "Cut by MALIMABI",
        "lead": "Монтаж в Premiere Pro: от первого проекта до финального рендера.",
        "lessons": [
            "Старт и настройка проекта",
            "Монтаж под музыку и саунд-дизайн",
            "Нейросети для переходов и динамики",
            "Цвет и film look",
            "Работа с текстом",
            "Чёткость и мастер-экспорт",
            "Финальный рендер в Topaz",
        ],
    },
}

# Вкладка «Материалы». sections — кому положено (хватает любого из разделов);
# url пустой — файл ещё не загружен, в приложении «откроется с уроками».
MATERIALS = [
    {"kind": "file", "badge": "LUT", "title": "Авторский лут",
     "note": ".cube · для Blackmagic и Premiere", "sections": ["shooting", "editing"], "url": ""},
    {"kind": "file", "badge": "PH", "title": "Playhead",
     "note": "Трекер времени · Mac и Windows", "sections": ["editing"], "url": ""},
    {"kind": "file", "badge": "BIN", "title": "Bin",
     "note": "Порядок в файлах · Mac", "sections": ["editing"], "url": ""},
    {"kind": "pdf", "title": "Баланс белого", "sections": ["shooting"], "url": ""},
    {"kind": "pdf", "title": "Выдержка", "sections": ["shooting"], "url": ""},
    {"kind": "pdf", "title": "ISO без шума", "sections": ["shooting"], "url": ""},
    {"kind": "pdf", "title": "False color", "sections": ["shooting"], "url": ""},
]


def _msk_date_start(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=MSK)


def now_msk() -> datetime:
    return datetime.now(MSK)


def is_full_price(now: datetime | None = None) -> bool:
    return (now or now_msk()) >= _msk_date_start(config.COURSE_FULL_PRICE_FROM)


def is_open(now: datetime | None = None) -> bool:
    return (now or now_msk()) >= _msk_date_start(config.COURSE_OPEN_AT)


def days_to_open(now: datetime | None = None) -> int:
    """Сколько дней до открытия уроков (0 — уже открыты; в последний день — 1)."""
    left = _msk_date_start(config.COURSE_OPEN_AT) - (now or now_msk())
    return 0 if left.total_seconds() <= 0 else left.days + 1


def price_stage(now: datetime | None = None) -> str:
    """presale — до COURSE_MID_PRICE_FROM, mid — до COURSE_FULL_PRICE_FROM, full — дальше."""
    now = now or now_msk()
    if now >= _msk_date_start(config.COURSE_FULL_PRICE_FROM):
        return "full"
    if now >= _msk_date_start(config.COURSE_MID_PRICE_FROM):
        return "mid"
    return "presale"


def mid_price(option: dict) -> int:
    """Середина между предпродажей и полной ценой, до десятков рублей."""
    return int(round((option["price"] + option["price_full"]) / 20) * 10)


def price_for(option: dict, now: datetime | None = None) -> int:
    stage = price_stage(now)
    if stage == "full":
        return int(option["price_full"])
    if stage == "mid":
        return mid_price(option)
    return int(option["price"])


def option_sections(option: dict) -> list[str]:
    return [s for s in (option.get("sections") or "").split(",") if s]


def _date_text(date_str: str) -> str:
    d = _msk_date_start(date_str)
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
    return f"{d.day} {months[d.month - 1]}"


def open_date_text() -> str:
    return _date_text(config.COURSE_OPEN_AT)


def presale_end_text() -> str:
    """До какого числа действуют цены предпродажи (день смены цен не включается)."""
    return _date_text(config.COURSE_MID_PRICE_FROM)


def full_price_from_text() -> str:
    return _date_text(config.COURSE_FULL_PRICE_FROM)


def price_until_text(now: datetime | None = None) -> str | None:
    """До какого числа действует текущая цена; None — цена уже полная."""
    stage = price_stage(now)
    if stage == "presale":
        return presale_end_text()
    if stage == "mid":
        return full_price_from_text()
    return None


def price_schedule_text() -> str:
    """Строка под ценами: что будет дальше. Пусто — цены уже полные."""
    stage = price_stage()
    if stage == "presale":
        return (f"Цены предпродажи действуют до {presale_end_text()}. "
                f"С {presale_end_text()} — дороже, с {full_price_from_text()} — полные.")
    if stage == "mid":
        return f"С {full_price_from_text()} — полные цены."
    return ""


def rub(amount: int | float) -> str:
    return f"{int(amount):,}".replace(",", " ") + " ₽"


def available_options(options: list[dict], owned: set[str]) -> list[dict]:
    """Что ещё имеет смысл купить: опции, которые откроют хотя бы один новый
    раздел. Купившему один раздел пакет не предлагаем — только второй раздел."""
    result = []
    for o in options:
        secs = set(option_sections(o))
        if not secs or secs <= owned:
            continue
        if owned and len(secs) > 1:
            continue
        result.append(o)
    return result


def verify_init_data(init_data: str, bot_token: str | None = None,
                     max_age: int | None = None, now: float | None = None) -> dict | None:
    """Проверяет initData мини-приложения по алгоритму Telegram.

    secret = HMAC_SHA256(key="WebAppData", msg=bot_token);
    hash   = HMAC_SHA256(key=secret, msg=data_check_string),
    где data_check_string — все поля, кроме hash, «key=value», по алфавиту,
    через \\n. Плюс свежесть auth_date. Возвращает user (dict) или None.
    """
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", "")
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", (bot_token or config.BOT_TOKEN).encode(),
                      hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    max_age = config.WEBAPP_AUTH_MAX_AGE if max_age is None else max_age
    if not auth_date or (now or time.time()) - auth_date > max_age:
        return None
    try:
        user = json.loads(pairs.get("user", ""))
    except ValueError:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user
