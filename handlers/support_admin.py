"""
Ответы поддержки (malimadmins): админ отвечает Reply на пересланный
вопрос клиента — ответ уходит клиенту в основной бот (см. handlers/support.py).
"""
import logging

from aiogram import Router, Bot, F
from aiogram.types import CallbackQuery, ForceReply, Message

import config
import database as db

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("sup_reply:"))
async def cb_support_reply(callback: CallbackQuery):
    """Кнопка «Ответить»: подсовываем поле ответа, дальше — обычный Reply.

    Отдельным сообщением с ForceReply, а не ответом на саму карточку
    вопроса: так поле ввода открывается сразу и только у нажавшего.
    """
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    user_id = int(callback.data.split(":", 1)[1])
    await callback.answer()
    prompt = await callback.message.answer(
        "✍️ Напишите ответ клиенту — он придёт ему в основной бот.",
        reply_markup=ForceReply(selective=True),
    )
    # Тот же механизм, что и у обычного Reply: ответ на это сообщение
    # находит клиента через support_messages
    await db.add_support_message(prompt.chat.id, prompt.message_id, user_id)


async def _support_reply_filter(message: Message):
    """Реплай админа именно на нашу пересланную копию вопроса.

    Возвращает False на любой другой reply (например, внутри /expense) —
    тогда aiogram передаёт апдейт дальше, другим хендлерам этого бота.
    """
    if not message.reply_to_message or message.from_user.id not in config.ADMIN_IDS:
        return False
    user_id = await db.get_support_user(message.chat.id, message.reply_to_message.message_id)
    if user_id is None:
        return False
    return {"support_user_id": user_id}


@router.message(_support_reply_filter)
async def on_support_reply(message: Message, support_user_id: int):
    header = "💬 <b>Ответ поддержки</b>:"

    main_bot = Bot(token=config.BOT_TOKEN)
    try:
        if message.text:
            await main_bot.send_message(support_user_id, f"{header}\n\n{message.text}",
                                        parse_mode="HTML")
        else:
            await main_bot.send_message(support_user_id, header, parse_mode="HTML")
            await main_bot.copy_message(support_user_id, message.chat.id, message.message_id)
    except Exception as e:
        logger.error(f"support: reply to {support_user_id} failed: {e}")
        await message.reply("⚠️ Не удалось отправить клиенту (заблокировал бота?).")
        return
    finally:
        await main_bot.session.close()

    await message.reply("✅ Отправлено клиенту.")
