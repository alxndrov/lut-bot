"""Курс в подарок.

Админ в malimadmins: /gift @покупатель [@получатель] (покупатель — ник или
Telegram ID) — доступ покупателя к разделам его последней покупки курса
придерживается, а бот выдаёт ссылку t.me/<бот>?start=gift_<token>. Покупатель
пересылает её получателю в нужный день. Получатель открывает ссылку, даёт
согласие (галочки на /start) — и разделы этой покупки открываются ему, а
покупателю и админам приходит сообщение. Покупка в учёте остаётся за
покупателем (он платил).

Два вида подарка: открытый (без @получателя) — курс получит тот, кто первым
откроет ссылку; с ником — только человек с этим username, остальным ссылка
не откроет курс и не «сгорит».

Бот не может написать человеку первым, поэтому подарок — только ссылкой.
"""
import logging
import secrets
from html import escape

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import config
import database as db
from services import course as svc

router = Router()   # админский бот
logger = logging.getLogger(__name__)

PREFIX = "gift_"


def _who(user: dict | None, user_id: int) -> str:
    if not user_id:   # продажа вне бота (/sale) — покупателя в боте нет
        return "оплата переводом"
    if user and user.get("username"):
        return "@" + user["username"]
    return (user or {}).get("first_name") or f"id {user_id}"


async def gift_link(token: str) -> str:
    bot = Bot(token=config.BOT_TOKEN)
    try:
        username = (await bot.get_me()).username
    finally:
        await bot.session.close()
    return f"https://t.me/{username}?start={PREFIX}{token}"


@router.message(Command("gift"))
async def cmd_gift(message: Message, command: CommandObject):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    args = (command.args or "").split()
    if not args or len(args) > 2:
        await message.answer(
            "🎁 Курс в подарок:\n"
            "• /gift @покупатель — открытый подарок: курс получит тот, кто первым откроет ссылку;\n"
            "• /gift @покупатель @получатель — только для этого ника.\n\n"
            "Покупателя можно указать и Telegram ID. Доступ покупателя придержу и дам ссылку.")
        return
    arg = args[0].lstrip("@")
    recipient = args[1].lstrip("@").lower() if len(args) == 2 else None
    buyer = await (db.get_user(int(arg)) if arg.isdigit() else db.get_user_by_username(arg))
    buyer_id = buyer["user_id"] if buyer else (int(arg) if arg.isdigit() else None)
    purchase = await db.last_course_purchase(buyer_id) if buyer_id else None
    if not purchase:
        await message.answer(f"Не нашёл покупку курса у {escape(arg)}.")
        return
    sections = await db.purchase_sections(purchase["id"])
    if not sections:
        await message.answer("У этой покупки нет разделов курса — нечего дарить.")
        return
    gift = await db.get_open_gift_for_purchase(purchase["id"])
    if gift:   # ссылка уже есть — та же, вид подарка меняем на указанный сейчас
        await db.set_gift_recipient(gift["token"], recipient)
    else:
        gift = {"token": secrets.token_urlsafe(9)}
        await db.create_course_gift(gift["token"], purchase["id"], buyer_id, sections, recipient)
    # Придерживаем доступ покупателя к разделам этой покупки
    await db.revoke_course_access(buyer_id, sections)
    link = await gift_link(gift["token"])
    names = " + ".join(svc.SECTION_SHORT.get(s, s) for s in sections)
    logger.info(f"gift: покупка {purchase['id']} ({buyer_id}) → ссылка {gift['token']}")
    await message.answer(
        f"🎁 <b>Подарок готов</b>\n{escape(purchase['title'])} ({names}) — покупка "
        f"{_who(buyer, buyer_id)}, {purchase['amount']} ₽. Доступ покупателя придержан.\n\n"
        f"Ссылка для получателя (одноразовая):\n{link}\n\n"
        "Покупатель пересылает её получателю в нужный день. "
        + (f"Курс откроется только @{escape(recipient)} — у других ссылка не сработает."
           if recipient else
           "Подарок открытый: кто первым откроет ссылку и даст согласие — тому и откроется курс."),
        parse_mode="HTML", disable_web_page_preview=True)


async def claim(message: Message, user_id: int, token: str) -> None:
    """Получатель открыл подарочную ссылку (согласие уже есть)."""
    from handlers.course import send_course
    gift = await db.get_course_gift(token)
    if not gift:
        await message.answer("Эта подарочная ссылка не работает. Напиши в /help — разберёмся.")
        return
    if gift["claimed_by"] and gift["claimed_by"] != user_id:
        await message.answer("Этот подарок уже получен. Если это ошибка — напиши в /help.")
        return
    if not gift["claimed_by"] and gift.get("recipient"):
        username = ((await db.get_user(user_id)) or {}).get("username") or ""
        if username.lower() != gift["recipient"]:
            await message.answer("🎁 Этот подарок предназначен другому человеку. Если он для тебя — "
                                 "попроси дарителя написать нам в /help, поправим.")
            return
    if not gift["claimed_by"]:
        if not await db.claim_course_gift(token, user_id):
            await message.answer("Этот подарок уже получен. Если это ошибка — напиши в /help.")
            return
        sections = gift["sections"].split(",")
        await db.grant_course_access(user_id, sections, gift["purchase_id"])
        logger.info(f"gift: {token} получил {user_id} (покупатель {gift['buyer_id']})")
        recipient = await db.get_user(user_id)
        buyer = await db.get_user(gift["buyer_id"])
        from handlers.prodamus_webhook import _send_notify
        await _send_notify(None, f"🎁 Подарок получен: курс открыт {escape(_who(recipient, user_id))} "
                                 + (f"(подарил(а) {escape(_who(buyer, gift['buyer_id']))})."
                                    if gift["buyer_id"] else "(продажа вне бота, /sale)."))
        if gift["buyer_id"] and gift["buyer_id"] != user_id:
            try:
                await message.bot.send_message(
                    gift["buyer_id"], "🎁 Ваш подарок получен — курс открыт получателю. Спасибо! 🤍")
            except Exception as e:
                logger.info(f"gift: покупателю {gift['buyer_id']} не написать: {e}")
    await message.answer("🎁 Тебе подарили курс «Shot on iPhone + Cut by MALIMABI»! Он уже твой 👇")
    await send_course(message, user_id)
