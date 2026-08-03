from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
import logging

import config
from config import ADMIN_IDS

logger = logging.getLogger(__name__)

router = Router()


class FeedbackState(StatesGroup):
    waiting_text = State()


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext):
    await state.set_state(FeedbackState.waiting_text)
    await message.answer(
        "✍️ Напиши свой отзыв или вопрос — я лично прочитаю.\n\n"
        "Отправь /cancel чтобы отменить."
    )


@router.message(Command("cancel"), FeedbackState.waiting_text)
async def cmd_cancel_feedback(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено.")


@router.message(FeedbackState.waiting_text)
async def receive_feedback(message: Message, state: FSMContext, bot: Bot):
    await state.clear()

    user = message.from_user
    username = f"@{user.username}" if user.username else f"id:{user.id}"

    notify_text = (
        f"💬 <b>Обратная связь</b>\n\n"
        f"👤 {user.first_name} {username}\n\n"
        f"📝 {message.text}"
    )

    try:
        notify_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
        for admin_id in ADMIN_IDS:
            try:
                await notify_bot.send_message(admin_id, notify_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Feedback notify to {admin_id} error: {e}")
        await notify_bot.session.close()
    except Exception as e:
        logger.error(f"Feedback notify bot error: {e}")

    await message.answer(
        "✅ Спасибо! Я обязательно прочитаю 🙏"
    )
