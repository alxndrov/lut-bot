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


def _msk_date_start(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=MSK)


def now_msk() -> datetime:
    return datetime.now(MSK)


def is_full_price(now: datetime | None = None) -> bool:
    return (now or now_msk()) >= _msk_date_start(config.COURSE_FULL_PRICE_FROM)


def is_open(now: datetime | None = None) -> bool:
    return (now or now_msk()) >= _msk_date_start(config.COURSE_OPEN_AT)


def price_for(option: dict, now: datetime | None = None) -> int:
    return int(option["price_full"] if is_full_price(now) else option["price"])


def option_sections(option: dict) -> list[str]:
    return [s for s in (option.get("sections") or "").split(",") if s]


def open_date_text() -> str:
    d = _msk_date_start(config.COURSE_OPEN_AT)
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
    return f"{d.day} {months[d.month - 1]}"


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
