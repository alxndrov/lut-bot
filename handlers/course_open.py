"""Сообщения покупателям об открытии уроков (оферта 7.4: «Об открытии Покупатель
получает сообщение в Боте»). Решение Дани 07.11.2026 — две волны:

- open — в момент открытия (COURSE_OPEN_AT, 00:00 по Москве) всем купившим;
- morning — в 10:00 по Москве тем, кто ещё не запустил ни одного урока.

Каждому — один раз на волну (таблица course_open_notices), так что перезапуск
бота не продублирует. Если бот лежал и проспал окно волны (open — до 06:00,
morning — до 20:00), волну пропускаем, а не шлём ночью или поздно вечером.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

import config
import database as db
from services import course as svc

logger = logging.getLogger(__name__)

# волна → (сдвиг от открытия, сколько после этого ещё можно слать, только не начавшим)
WAVES = {
    "open": (timedelta(0), timedelta(hours=6), False),
    "morning": (timedelta(hours=10), timedelta(hours=10), True),
}


def _sections_text(sections: set[str]) -> str:
    if {"shooting", "editing"} <= sections:
        return "Оба раздела — съёмка и монтаж — уже ждут тебя в приложении."
    name = svc.SECTION_SHORT.get(next(iter(sections)), "")
    return f"Раздел «{name}» уже ждёт тебя в приложении."


def message_text(wave: str, sections: set[str]) -> str:
    if wave == "open":
        lines = ["🎬 <b>Уроки открыты!</b>", "",
                 "Курс Shot on iPhone + Cut by MALIMABI стартовал. " + _sections_text(sections), "",
                 "Под каждым уроком — шпаргалки, в «Материалах» — авторский лут"
                 + (", Playhead и Library." if "editing" in sections else ".")]
    else:
        lines = ["☀️ Доброе утро! Уроки курса уже открыты.", "",
                 _sections_text(sections) + " Начни с первого урока — "
                 + ("настроим Blackmagic Camera." if "shooting" in sections else "знакомимся с Premiere.")]
    if "shooting" in sections:
        lines += ["", "Пятый урок съёмки — «Приёмы и движение камеры» — появится чуть позже."]
    lines += ["", "Доступ без ограничения срока — смотри в своём темпе."]
    return "\n".join(lines)


def _keyboard() -> InlineKeyboardMarkup | None:
    from handlers.course import app_button
    button = app_button("📱 Открыть уроки")
    return InlineKeyboardMarkup(inline_keyboard=[[button]]) if button else None


def due_wave(now: datetime) -> str | None:
    opened = svc._msk_date_start(config.COURSE_OPEN_AT)
    for wave, (offset, window, _) in WAVES.items():
        if opened + offset <= now < opened + offset + window:
            return wave
    return None


async def send_wave(bot: Bot, wave: str) -> tuple[int, int]:
    only_not_started = WAVES[wave][2]
    sent = failed = 0
    kb = _keyboard()
    for user_id, sections in await db.get_course_open_recipients(wave, only_not_started):
        ok = True
        for _ in range(3):
            try:
                await bot.send_message(user_id, message_text(wave, sections), parse_mode="HTML",
                                       reply_markup=kb)
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                ok = False     # бот заблокирован — не повторяем
                break
            except Exception as e:
                logger.warning(f"course open {wave}: {user_id} не получил: {e}")
                ok = False
                break
        await db.mark_course_open_notice(user_id, wave, ok)
        sent += ok
        failed += not ok
        await asyncio.sleep(0.1)
    if sent or failed:
        logger.info(f"course open {wave}: отправлено {sent}, не дошло {failed}")
    return sent, failed


async def open_notice_loop(bot: Bot):
    while True:
        try:
            wave = due_wave(svc.now_msk())
            if wave:
                await send_wave(bot, wave)
        except Exception:
            logger.exception("course open: волна не отправилась")
        await asyncio.sleep(30)
