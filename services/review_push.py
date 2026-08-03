"""
Фоновый воркер: отправляет пуши с просьбой оставить отзыв после покупки.
Проверяет очередь раз в минуту.
"""
import asyncio
import logging

from aiogram import Bot

import database as db

logger = logging.getLogger(__name__)

DEFAULT_TEXT = (
    "⭐ Привет!\n\n"
    "Как тебе покупка? Если всё понравилось — буду очень рад отзыву, "
    "это очень помогает развитию 🙏\n\n"
    "Можешь написать прямо сюда — я передам."
)


async def review_push_worker(bot: Bot):
    """Запускать как asyncio.create_task()."""
    logger.info("review_push_worker: started")
    while True:
        try:
            due = await db.get_due_review_pushes()
            for push in due:
                text = push.get("review_push_text") or DEFAULT_TEXT
                try:
                    await bot.send_message(push["user_id"], text)
                    logger.info(
                        f"review_push sent: user={push['user_id']} "
                        f"product={push['product_id']} push_id={push['id']}"
                    )
                except Exception as e:
                    logger.warning(f"review_push send error (push {push['id']}): {e}")
                finally:
                    await db.mark_review_push_sent(push["id"])
        except Exception as e:
            logger.error(f"review_push_worker error: {e}")
        await asyncio.sleep(60)
