"""
/help — поддержка в основном боте: вопрос клиента уходит всем админам
в malimadmins, ответ приходит обратно сюда же (см. handlers/support_admin.py).
"""
import logging

from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

import config
import database as db

router = Router()
logger = logging.getLogger(__name__)


class SupportState(StatesGroup):
    waiting_text = State()


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await state.set_state(SupportState.waiting_text)
    await message.answer(
        "🆘 <b>Поддержка</b>\n\n"
        "Напишите ваш вопрос одним сообщением — я передам его команде, "
        "и вам ответят прямо здесь.\n\n"
        "Чтобы отменить — /cancel.",
        parse_mode="HTML",
    )


@router.message(Command("cancel"), SupportState.waiting_text)
async def cmd_support_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено.")


# Команду не считаем вопросом в поддержку — см. тот же случай в feedback.py
@router.message(SupportState.waiting_text, ~(F.text & F.text.startswith("/")))
async def on_support_message(message: Message, state: FSMContext):
    await state.clear()

    user = message.from_user
    username = f"@{user.username}" if user.username else f"id:{user.id}"
    header = (f"🆘 <b>Вопрос в поддержку</b>\n"
             f"👤 {user.first_name or '—'} {username} (id:{user.id})")

    # Кнопка «Ответить» — иначе единственный способ ответить это Reply
    # вручную, и его легко не заметить
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответить", callback_data=f"sup_reply:{user.id}")
    ]])
    try:
        support_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
        try:
            for admin_id in config.ADMIN_IDS:
                try:
                    if message.text:
                        sent = await support_bot.send_message(
                            admin_id, f"{header}\n\n{message.text}",
                            parse_mode="HTML", reply_markup=kb)
                        await db.add_support_message(admin_id, sent.message_id, user.id)
                    else:
                        # Фото/голос/документ и т.п. — шапка отдельно, дальше копия как есть
                        sent_header = await support_bot.send_message(
                            admin_id, header, parse_mode="HTML", reply_markup=kb)
                        await db.add_support_message(admin_id, sent_header.message_id, user.id)
                        sent_copy = await support_bot.copy_message(
                            admin_id, message.chat.id, message.message_id)
                        await db.add_support_message(admin_id, sent_copy.message_id, user.id)
                except Exception as e:
                    logger.error(f"support: notify admin {admin_id} failed: {e}")
        finally:
            await support_bot.session.close()
    except Exception as e:
        logger.error(f"support: bot error: {e}")
        await message.answer("⚠️ Не получилось отправить, попробуйте ещё раз чуть позже.")
        return

    await message.answer("✅ Вопрос отправлен! Как только ответят — пришлю сюда.")


# --- Ответ на просьбу об отзыве ---
# Пуш зовёт написать отзыв прямо в бот, но до сих пор такие сообщения не
# ловил ни один хендлер — они просто пропадали. Роутер подключается
# ПОСЛЕДНИМ (см. bot.py), чтобы не перехватывать чужие сценарии.
review_router = Router()


@review_router.message(F.text | F.photo | F.video | F.voice | F.document)
async def on_review_reply(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return                                   # человек внутри другого сценария
    if message.text and message.text.startswith("/"):
        return
    push = await db.recent_review_push(message.from_user.id)
    if not push:
        return

    user = message.from_user
    username = f"@{user.username}" if user.username else f"id:{user.id}"
    header = (f"⭐️ <b>Отзыв о покупке</b>\n"
             f"👤 {user.first_name or '—'} {username} (id:{user.id})\n"
             f"🛍 {push['product_name']}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответить", callback_data=f"sup_reply:{user.id}")
    ]])
    bot = Bot(token=config.WAITLIST_BOT_TOKEN)
    try:
        for admin_id in config.ADMIN_IDS:
            try:
                if message.text:
                    sent = await bot.send_message(admin_id, f"{header}\n\n{message.text}",
                                                  parse_mode="HTML", reply_markup=kb)
                    await db.add_support_message(admin_id, sent.message_id, user.id)
                else:
                    sent = await bot.send_message(admin_id, header, parse_mode="HTML",
                                                  reply_markup=kb)
                    await db.add_support_message(admin_id, sent.message_id, user.id)
                    copy = await bot.copy_message(admin_id, message.chat.id, message.message_id)
                    await db.add_support_message(admin_id, copy.message_id, user.id)
            except Exception as e:
                logger.error(f"review reply → admin {admin_id}: {e}")
    finally:
        await bot.session.close()
    await message.answer("Спасибо за отзыв! 🙏 Передал команде.")
