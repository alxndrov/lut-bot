"""
Обработчик ТЗ для физических товаров.
Пользователь заполняет свободный текст — он уходит в админский чат.
"""
import logging
from datetime import datetime

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import config
import database as db
from keyboards.user import back_to_catalog_keyboard

router = Router()
logger = logging.getLogger(__name__)


class BriefForm(StatesGroup):
    waiting_brief = State()


@router.callback_query(F.data.startswith("brief:"))
async def cb_brief_start(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])
    product = await db.get_product(product_id)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    await state.update_data(product_id=product_id)
    await state.set_state(BriefForm.waiting_brief)

    await callback.message.answer(
        f"📋 <b>{product['name']}</b>\n\n"
        "Опишите ваш запрос: что именно вам нужно, пожелания, размеры, цвет, контакты — "
        "всё, что поможет нам подготовить предложение.\n\n"
        "Можно написать всё одним сообщением или прикрепить фото.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(BriefForm.waiting_brief)
async def fsm_receive_brief(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    product_id = data["product_id"]
    product = await db.get_product(product_id)
    await state.clear()

    user = message.from_user
    username_str = f"@{user.username}" if user.username else f"id:{user.id}"
    first_name = user.first_name or "—"
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    product_name = product["name"] if product else f"id:{product_id}"

    notify_text = (
        f"📋 <b>Новое ТЗ</b>\n\n"
        f"👤 {first_name} {username_str}\n"
        f"🛍 {product_name}\n"
        f"🕐 {now}\n\n"
        f"<b>Сообщение:</b>\n{message.text or '(без текста)'}"
    )

    # Пересылаем в админ-чат через notify-бот
    try:
        notify_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
        for admin_id in config.ADMIN_IDS:
            try:
                await notify_bot.send_message(admin_id, notify_text, parse_mode="HTML")
                # Если есть фото/файл — пересылаем оригинал
                if message.photo or message.document:
                    await message.forward(chat_id=admin_id)
            except Exception as e:
                logger.error(f"Brief notify to {admin_id} failed: {e}")
        await notify_bot.session.close()
    except Exception as e:
        logger.error(f"Failed to send brief notification: {e}")

    await message.answer(
        "✅ Ваш запрос получен! Мы свяжемся с вами в ближайшее время.",
        reply_markup=back_to_catalog_keyboard(),
    )
