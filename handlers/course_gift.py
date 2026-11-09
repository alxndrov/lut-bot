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

Подарок от нас (без покупки): /gift без аргументов → съёмка / монтаж / пакет →
@ник получателя или «свободный» → бот присылает готовое сообщение со ссылкой,
его пересылают получателю. В course_gifts такой подарок — purchase_id = 0,
buyer_id — админ, который его создал; в продажах и финансах его нет,
в /course — отдельной строкой. Неполученный можно отменить кнопкой.
"""
import re
import logging
import secrets
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import database as db
from services import course as svc

router = Router()   # админский бот
logger = logging.getLogger(__name__)

PREFIX = "gift_"
COURSE = "«Shot on iPhone + Cut by MALIMABI»"
# Что можно подарить от нас: ключ кнопки → разделы
FREE_KINDS = {"shooting": ["shooting"], "editing": ["editing"], "bundle": ["shooting", "editing"]}
FREE_TITLES = {"shooting": "🎬 Съёмка", "editing": "✂️ Монтаж", "bundle": "📦 Пакет: съёмка + монтаж"}


class FreeGift(StatesGroup):
    recipient = State()


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
            "🎁 <b>Подарить курс от нас</b> — без оплаты, в продажи не попадёт. Что дарим?\n\n"
            "<i>Передарить чужую покупку:</i>\n"
            "• /gift @покупатель — курс получит тот, кто первым откроет ссылку;\n"
            "• /gift @покупатель @получатель — только для этого ника.\n"
            "Доступ покупателя придержу и дам ссылку.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t, callback_data=f"fgift:kind:{k}")]
                for k, t in FREE_TITLES.items()]))
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
        free = gift["purchase_id"] == 0
        await db.grant_course_access(user_id, sections, gift["purchase_id"] or None)
        logger.info(f"gift: {token} получил {user_id} (покупатель {gift['buyer_id']})")
        recipient = await db.get_user(user_id)
        buyer = await db.get_user(gift["buyer_id"])
        from handlers.prodamus_webhook import _send_notify
        sale = None if gift["buyer_id"] else await db.get_manual_sale_by_gift(token)
        if free:
            await _send_notify(None, f"🎁 Подарок от {escape(_who(buyer, gift['buyer_id']))} получен: "
                                     f"{_sections_text(sections)} — открыто "
                                     f"{escape(_who(recipient, user_id))}.")
        elif sale:
            await _send_notify(None, f"🎓 Ручная продажа b2b-{sale['id']}"
                                     + (f" ({escape(sale['buyer_name'])})" if sale.get("buyer_name") else "")
                                     + f": ученик открыл курс — {escape(_who(recipient, user_id))}.")
        else:
            await _send_notify(None, f"🎁 Подарок получен: курс открыт {escape(_who(recipient, user_id))} "
                                     f"(подарил(а) {escape(_who(buyer, gift['buyer_id']))}).")
        if gift["buyer_id"] and gift["buyer_id"] != user_id and not free:
            try:
                await message.bot.send_message(
                    gift["buyer_id"], "🎁 Ваш подарок получен — курс открыт получателю. Спасибо! 🤍")
            except Exception as e:
                logger.info(f"gift: покупателю {gift['buyer_id']} не написать: {e}")
    await message.answer("🎁 Тебе подарили курс «Shot on iPhone + Cut by MALIMABI»! Он уже твой 👇"
                         if gift["buyer_id"] else
                         "🎓 Доступ к курсу «Shot on iPhone + Cut by MALIMABI» открыт — он уже твой 👇")
    await send_course(message, user_id)


# ── Подарок от нас (без покупки) ───────────────────────────────────────────────

def _sections_text(sections: list[str]) -> str:
    return " + ".join(svc.SECTION_SHORT.get(x, x) for x in sections).lower()


def _gift_text(kind: str) -> str:
    """Сообщение получателю — админ пересылает или копирует его."""
    what = {"shooting": "к разделу «Shot on iPhone» — съёмка",
            "editing": "к разделу «Cut by MALIMABI» — монтаж",
            "bundle": "ко всему курсу — съёмка и монтаж"}[kind]
    return f"🎁 Дарю тебе доступ к онлайн-курсу {COURSE} {what} 🤍"


def parse_username(text: str) -> str | None:
    m = re.fullmatch(r"\s*(?:https?://)?(?:t\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,31})\s*", text or "")
    return m.group(1).lower() if m else None


@router.callback_query(F.data.startswith("fgift:kind:"))
async def cb_free_kind(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    kind = callback.data.rsplit(":", 1)[1]
    if kind not in FREE_KINDS:
        await callback.answer()
        return
    await state.set_state(FreeGift.recipient)
    await state.update_data(kind=kind)
    await callback.answer()
    await callback.message.answer(
        f"{FREE_TITLES[kind]} в подарок. Для кого?\n\n"
        "Пришли @ник получателя — курс откроется только ему.\n"
        "Или нажми «Свободный подарок» — получит тот, кто первым откроет ссылку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Свободный подарок", callback_data="fgift:open")],
            [InlineKeyboardButton(text="Отмена", callback_data="fgift:stop")]]))


@router.callback_query(F.data == "fgift:stop")
async def cb_free_stop(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data == "fgift:open", StateFilter(FreeGift.recipient))
async def cb_free_open(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _make_free_gift(callback.message, callback.from_user.id, state, None)


@router.message(StateFilter(FreeGift.recipient), F.text, ~F.text.startswith("/"))
async def msg_free_recipient(message: Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    username = parse_username(message.text)
    if not username:
        await message.answer("Не похоже на ник. Пришли @ник получателя (например, @malimabi) "
                             "или нажми «Свободный подарок» выше.")
        return
    await _make_free_gift(message, message.from_user.id, state, username)


async def _make_free_gift(message: Message, admin_id: int, state: FSMContext, username: str | None):
    kind = (await state.get_data()).get("kind")
    await state.clear()
    if kind not in FREE_KINDS:
        await message.answer("Начни заново: /gift")
        return
    sections = FREE_KINDS[kind]
    token = secrets.token_urlsafe(9)
    await db.create_course_gift(token, 0, admin_id, sections, username)
    link = await gift_link(token)
    logger.info(f"gift: подарок от {admin_id} — {kind}, {'@' + username if username else 'свободный'}, {token}")
    await message.answer(
        f"🎁 <b>Подарок готов</b> — {FREE_TITLES[kind]}, "
        + (f"только для @{escape(username)}." if username else
           "свободный: получит тот, кто первым откроет ссылку.")
        + "\n\nСообщение ниже перешли получателю или скопируй его текст. Ссылка одноразовая; "
          "когда курс откроют — придёт уведомление.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="❌ Отменить подарок", callback_data=f"fgift:cancel:{token}")]]))
    await message.answer(f"{_gift_text(kind)}\n\nОткрой ссылку и нажми «Старт» — "
                         f"курс сразу появится в боте:\n{link}",
                         disable_web_page_preview=True)


@router.callback_query(F.data.startswith("fgift:cancel:"))
async def cb_free_cancel(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    token = callback.data.split(":", 2)[2]
    gift = await db.get_course_gift(token)
    if gift and gift["purchase_id"] == 0 and await db.delete_unclaimed_gift(token):
        await callback.answer("Подарок отменён — ссылка больше не работает.", show_alert=True)
        try:
            await callback.message.edit_text(callback.message.html_text + "\n\n❌ <b>Отменён</b>",
                                             parse_mode="HTML")
        except Exception:
            pass
    else:
        await callback.answer("Подарок уже получен — отменить нельзя." if gift else
                              "Этого подарка уже нет.", show_alert=True)
