"""Личная скидка на курс.

Ссылка t.me/<бот>?start=offer_<token> (создаёт scripts/make_course_offer.py)
показывает один вариант курса по цене из course_offers — только тому
Telegram-аккаунту, для которого она сделана, и только до valid_until
включительно (по Москве). Остальным и после срока ссылка скидку не даёт
и ведёт на обычный экран курса.

Оплата — тем же заказом c_{клиент}_{вариант}, что и с экрана курса, поэтому
доступ после оплаты выдаётся сам. Ссылку на оплату можно дать и сразу —
pay_link(): https://app.malimabi.com/pay/<токен>; при переходе по ней
handlers/pay_router._offer_pay снова проверяет срок и сумму, пишет клик
в pay_clicks, а о первом переходе сообщает админам.
"""
import logging
from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import database as db
from services import course as svc
from services.sellers import seller_line

router = Router()
logger = logging.getLogger(__name__)

PREFIX = "offer_"


def _until_date(offer: dict):
    return datetime.strptime(offer["valid_until"], "%Y-%m-%d").date()


async def _check(offer: dict | None, user_id: int, now: datetime | None = None) -> str | None:
    """Почему скидка не действует (текст клиенту) или None — действует."""
    if not offer:
        return "Эта ссылка не работает."
    if offer["user_id"] != user_id:
        return "Это личное предложение для другого человека."
    if (now or svc.now_msk()).date() > _until_date(offer):
        return "Срок этого предложения закончился."
    option = await db.get_course_option(offer["option_id"])
    if not option:
        return "Этот вариант курса больше недоступен."
    if set(svc.option_sections(option)) <= await db.get_user_course_sections(user_id):
        return "Этот раздел у тебя уже есть 🤍"
    return None


def pay_link(offer: dict, option: dict, method: str = "paw") -> str:
    """https://app.malimabi.com/pay/<токен> — переход проверяет срок и сумму
    (handlers/pay_router._offer_pay), фиксирует клик и ведёт в PayAnyWay
    (method='paw') или lava.top ('lava')."""
    from services import paylinks
    return paylinks.link(f"c_{offer['user_id']}_{offer['option_id']}", offer["price"],
                         option["title"], via=f"offer:{offer['token']}:{method}")


def _date_text(offer: dict) -> str:
    return svc.date_text(offer["valid_until"])


async def claim(message: Message, user_id: int, token: str) -> None:
    """Клиент открыл ссылку (согласие уже есть)."""
    from handlers.course import send_course
    offer = await db.get_course_offer(token)
    problem = await _check(offer, user_id)
    if problem:
        logger.info(f"offer: {token} у {user_id} не сработал: {problem}")
        await message.answer(problem + " Вот курс по обычной цене 👇")
        await send_course(message, user_id)
        return
    option = await db.get_course_option(offer["option_id"])
    full = svc.price_for(option)
    old = f"<s>{svc.rub(full)}</s> " if full > offer["price"] else ""
    await db.record_course_view(user_id, "offer")
    lines = [
        "🤍 <b>Личное предложение</b>",
        "",
        f"<b>{escape(option['title'])}</b> — {old}<b>{svc.rub(offer['price'])}</b>",
        f"Действует до {_date_text(offer)} включительно и только для тебя.",
        "",
        seller_line("course"),
    ]
    if config.COURSE_OFFER_URL:
        lines.append(f'Оплачивая, ты принимаешь <a href="{config.COURSE_OFFER_URL}">оферту</a>.')
    await message.answer(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {svc.rub(offer['price'])}",
                                  callback_data=f"course:offer:{token}")],
            [InlineKeyboardButton(text="📖 Что в курсе", callback_data="course:show")],
            [InlineKeyboardButton(text="🔄 Я оплатил", callback_data="course:check")],
        ]))
    logger.info(f"offer: {token} открыл {user_id}")


@router.callback_query(F.data.startswith("course:offer:"))
async def cb_pay(callback: CallbackQuery):
    token = callback.data.split(":", 2)[2]
    offer = await db.get_course_offer(token)
    problem = await _check(offer, callback.from_user.id)
    if problem:
        await callback.answer(problem, show_alert=True)
        return
    option = await db.get_course_option(offer["option_id"])
    rows = [[InlineKeyboardButton(text="💳 Карта РФ или СБП", url=pay_link(offer, option))]]
    if config.LAVA_ENABLED:
        rows.append([InlineKeyboardButton(text="🌍 Иностранный банк",
                                          url=pay_link(offer, option, "lava"))])
    await callback.answer()
    await callback.message.answer("Выберите способ оплаты:",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
