"""Админский бот (malimadmins) — только для ADMIN_IDS.

Права проверяет и каждый обработчик сам, но кнопок без своей проверки
десятки, и одна забытая проверка открыла бы чужому человеку заказы,
финансы или выдачу курса. Этот фильтр стоит на входе: апдейт не от
админа дальше не идёт.
"""
import logging

from aiogram import BaseMiddleware

import config

logger = logging.getLogger(__name__)


def sender_id(event, data) -> int | None:
    user = data.get("event_from_user")
    if user:
        return user.id
    # widget_query aiogram не знает — отправитель лежит в сыром апдейте
    q = (getattr(event, "model_extra", None) or {}).get("widget_query") or {}
    sender = q.get("from") or {}
    return sender.get("id") if isinstance(sender, dict) else None


class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        uid = sender_id(event, data)
        if uid in config.ADMIN_IDS:
            return await handler(event, data)
        logger.warning(f"админский бот: апдейт не от админа (id:{uid}) — пропущен")
        return None
