"""
/help — поддержка в основном боте: вопрос клиента уходит всем админам
в malimadmins, ответ приходит обратно сюда же (см. handlers/support_admin.py).
"""
import logging

from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, InlineKeyboardMarkup, InlineKeyboardButton,
                           BufferedInputFile)

import config
import database as db

router = Router()
logger = logging.getLogger(__name__)


class SupportState(StatesGroup):
    waiting_text = State()


# Медиа между двумя ботами: copy_message тут не работает — админский бот не
# видит чат клиента («chat not found»), а file_id у каждого бота свой.
# Поэтому качаем файл основным ботом и заливаем админским.
_MEDIA = (
    ("photo", "send_photo", "photo.jpg"),
    ("video", "send_video", "video.mp4"),
    ("voice", "send_voice", "voice.ogg"),
    ("video_note", "send_video", "video.mp4"),
    ("animation", "send_animation", "animation.mp4"),
    ("audio", "send_audio", "audio.mp3"),
    ("document", "send_document", None),
)


async def relay_media(message: Message, to_bot: Bot, chat_id: int):
    """Пересылает вложение клиента админскому боту. Возвращает Message или None."""
    for attr, method, default_name in _MEDIA:
        obj = getattr(message, attr, None)
        if not obj:
            continue
        obj = obj[-1] if isinstance(obj, (list, tuple)) else obj      # photo — список размеров
        try:
            file = await message.bot.get_file(obj.file_id)
            buf = await message.bot.download_file(file.file_path)
            name = getattr(obj, "file_name", None) or default_name or "file"
            data = BufferedInputFile(buf.read(), filename=name)
            return await getattr(to_bot, method)(chat_id, data,
                                                 caption=message.caption or None)
        except Exception as e:
            logger.error(f"relay_media ({attr}): {e}")
            return await to_bot.send_message(
                chat_id, f"⚠️ Вложение ({attr}) переслать не удалось: {e}")
    return None


async def notify_admins(message: Message, header: str, user_id: int,
                        bot_token: str | None = None) -> bool:
    """Шапка + само сообщение клиента всем админам в malimadmins.

    Каждое отправленное сообщение регистрируем в support_messages —
    ответом (Reply) на любое из них админ отвечает клиенту.
    """
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответить", callback_data=f"sup_reply:{user_id}")
    ]])
    ok = False
    bot = Bot(token=bot_token or config.WAITLIST_BOT_TOKEN)
    try:
        for admin_id in config.ADMIN_IDS:
            try:
                if message.text:
                    sent = await bot.send_message(admin_id, f"{header}\n\n{message.text}",
                                                  parse_mode="HTML", reply_markup=kb)
                    await db.add_support_message(admin_id, sent.message_id, user_id)
                else:
                    # Фото/голос/документ: шапка отдельно, следом вложение
                    sent = await bot.send_message(admin_id, header, parse_mode="HTML",
                                                  reply_markup=kb)
                    await db.add_support_message(admin_id, sent.message_id, user_id)
                    copy = await relay_media(message, bot, admin_id)
                    if copy:
                        await db.add_support_message(admin_id, copy.message_id, user_id)
                ok = True
            except Exception as e:
                logger.error(f"support: notify admin {admin_id} failed: {e}")
    finally:
        await bot.session.close()
    return ok


def client_header(message: Message, title: str, extra: str = "") -> str:
    user = message.from_user
    username = f"@{user.username}" if user.username else f"id:{user.id}"
    header = (f"{title}\n"
              f"👤 {user.first_name or '—'} {username} (id:{user.id})")
    if extra:
        header += f"\n{extra}"
    # Подпись к фото/видео — тоже текст сообщения, показываем её в шапке
    if not message.text and message.caption:
        header += f"\n\n{message.caption}"
    return header


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
    ok = await notify_admins(message, client_header(message, "🆘 <b>Вопрос в поддержку</b>"),
                             message.from_user.id)
    if not ok:
        await message.answer("⚠️ Не получилось отправить, попробуйте ещё раз чуть позже.")
        return
    await message.answer("✅ Вопрос отправлен! Как только ответят — пришлю сюда.")


# --- Свободное сообщение клиента ---
# Человеку неоткуда знать про /help: он просто пишет в бот — в ответ на
# просьбу об отзыве или продолжая начатую переписку с поддержкой. Раньше
# такие сообщения не ловил никто и они пропадали. Роутер подключается
# ПОСЛЕДНИМ (см. bot.py), чтобы не перехватывать чужие сценарии.
review_router = Router()


@review_router.message(F.text | F.photo | F.video | F.voice | F.document
                       | F.video_note | F.animation | F.audio)
async def on_free_message(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return                                   # человек внутри другого сценария
    if message.text and message.text.startswith("/"):
        return

    user_id = message.from_user.id
    push = await db.recent_review_push(user_id)
    support_at = await db.recent_support_contact(user_id)
    # Было и то и другое — считаем продолжением того, что случилось позже
    if push and support_at and str(push["send_at"]) > support_at:
        support_at = None

    if support_at:
        header = client_header(message, "🆘 <b>Сообщение в поддержку</b>")
        reply_text = "✅ Передал команде — ответят здесь же."
    elif push:
        header = client_header(message, "⭐️ <b>Отзыв о покупке</b>",
                               f"🛍 {push['product_name']}")
        reply_text = "Спасибо за отзыв! 🙏 Передал команде."
    else:
        return

    if await notify_admins(message, header, user_id):
        await message.answer(reply_text)
    else:
        await message.answer("⚠️ Не получилось отправить, попробуйте ещё раз чуть позже.")
