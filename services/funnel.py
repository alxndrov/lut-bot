"""
Фоновый воркер воронки лид-магнита.
Каждые 30 секунд проверяет очередь и отправляет сообщения вовремя.
"""
import asyncio
import logging

from aiogram import Bot

import config
import database as db

logger = logging.getLogger(__name__)


async def funnel_worker(bot: Bot):
    logger.info("funnel_worker started")
    while True:
        try:
            due = await db.get_due_funnel_messages()
            held = 0
            for msg in due:
                # Рекламные сообщения — только с согласия на обработку данных.
                # Сообщение не отменяем: примет согласие — воронка продолжится.
                if config.POLICY_REQUIRED and not await db.is_policy_accepted(msg["user_id"]):
                    held += 1
                    continue
                try:
                    await bot.send_message(
                        msg["user_id"],
                        msg["text"],
                        parse_mode="HTML",
                    )
                    await db.mark_funnel_message_sent(msg["id"])
                    logger.info(
                        f"funnel: sent step {msg['step']} "
                        f"of '{msg['funnel_id']}' to user {msg['user_id']}"
                    )
                except Exception as e:
                    logger.error(
                        f"funnel: failed to send to user {msg['user_id']}: {e}"
                    )
                    # Не помечаем как sent — попробуем снова через 30 сек
            if held:
                logger.info(f"funnel: придержано {held} сообщений — нет согласия")
        except Exception as e:
            logger.error(f"funnel_worker error: {e}")

        await asyncio.sleep(30)
