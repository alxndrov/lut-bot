"""
Обработчик ссылок от победителей бонусного разбора.
Если пользователь является победителем бонуса и присылает ссылку —
бот сохраняет её и уведомляет Мишу.
"""
import logging
import re
from aiogram import Router, F, Bot
from aiogram.types import Message

import config
import database as db

router = Router()
logger = logging.getLogger(__name__)

URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)


@router.message(F.text)
async def handle_possible_video_link(message: Message, bot: Bot):
    user_id = message.from_user.id

    # Ищем URL в сообщении
    urls = URL_RE.findall(message.text or "")
    if not urls:
        return

    # Проверяем — есть ли у пользователя активный бонус хоть по одному продукту
    bonuses = await db.get_bonus_winners_for_any_product(user_id)
    if not bonuses:
        return  # Не победитель — игнорируем (другие хендлеры разберутся)

    link = urls[0].rstrip(".,)")  # берём первую ссылку, чистим пунктуацию

    # Если у победителя несколько продуктов с бонусом — берём первый без ссылки
    target = next((b for b in bonuses if not b["video_link"]), None)
    if target is None:
        # Уже прислал ссылку — обновляем (позволяем переотправить)
        target = bonuses[0]

    await db.save_bonus_video_link(user_id, target["product_id"], link)

    username_str = f"@{message.from_user.username}" if message.from_user.username \
        else f"id:{user_id}"
    first_name = message.from_user.first_name or "—"

    await message.answer(
        "✅ Ссылка принята! Я посмотрю ролик и напишу тебе разбор в личку 🎬"
    )

    # Уведомляем всех админов
    notify_text = (
        f"🎬 <b>Новая ссылка на разбор</b>\n\n"
        f"👤 {first_name} {username_str}\n"
        f"🛍 {target['product_name']}\n"
        f"🔗 {link}\n\n"
        f"Напиши разбор напрямую в личку пользователю."
    )
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, notify_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"bonus notify admin {admin_id}: {e}")

    logger.info(f"bonus review link saved: user {user_id}, link={link}")
