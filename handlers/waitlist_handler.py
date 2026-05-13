from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from datetime import datetime
import logging

import database as db
import config
from keyboards.user import back_to_catalog_keyboard

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("waitlist:"))
async def cb_waitlist(callback: CallbackQuery, bot: Bot):
    product_id = int(callback.data.split(":")[1])
    product = await db.get_product(product_id)

    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    user = callback.from_user
    username = f"@{user.username}" if user.username else f"id:{user.id}"
    first_name = user.first_name or ""

    # Check if already in waitlist
    existing = await db.get_waitlist_entry(user.id, product_id)
    if existing:
        await callback.answer("Вы уже в списке ожидания для этого товара!", show_alert=True)
        return

    # Save to DB
    await db.add_waitlist_entry(user.id, user.username, first_name, product_id)

    # Send notification
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    notify_text = (
        f"📋 <b>Новая заявка в список ожидания</b>\n\n"
        f"👤 Пользователь: {first_name} {username}\n"
        f"🛍 Товар: <b>{product['name']}</b>\n"
        f"🕐 Время: {now}"
    )

    # Отправляем через notify-бот всем админам
    try:
        notify_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
        for admin_id in config.ADMIN_IDS:
            try:
                await notify_bot.send_message(admin_id, notify_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Waitlist notify to {admin_id} error: {e}")
        await notify_bot.session.close()
    except Exception as e:
        logger.error(f"Failed to create notify bot: {e}")

    # Confirm to user
    await callback.message.answer(
        f"✅ Вы записаны в список ожидания для <b>{product['name']}</b>!\n\n"
        "Мы уведомим вас, когда товар появится.",
        parse_mode="HTML",
        reply_markup=back_to_catalog_keyboard(),
    )
    await callback.answer()
