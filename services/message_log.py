"""Лог всех входящих сообщений: кто написал, что и разобрал ли это кто-нибудь.

Нужен, потому что потерянное сообщение восстановить нечем: Telegram
историю ботам не отдаёт, и если сообщение съел чужой хендлер, следов не
остаётся вообще. Теперь след остаётся всегда — в журнале systemd.
"""
import logging

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update

logger = logging.getLogger("messages")

_KINDS = ("photo", "video", "voice", "video_note", "animation", "audio",
          "document", "sticker", "contact", "location")
_LIMIT = 300


def _describe(msg) -> str:
    if msg.text:
        return f"text {msg.text[:_LIMIT]!r}"
    kind = next((k for k in _KINDS if getattr(msg, k, None)), "—")
    return f"{kind}" + (f" caption {msg.caption[:_LIMIT]!r}" if msg.caption else "")


class MessageLogMiddleware(BaseMiddleware):
    """Пишет строку на каждое входящее сообщение и помечает, дошло ли оно.

    NOT HANDLED — сообщение не разобрал ни один хендлер: человек написал
    в пустоту. Это и есть сигнал, что где-то дырка.
    """

    def __init__(self, tag: str):
        self.tag = tag

    async def __call__(self, handler, event: Update, data):
        msg = getattr(event, "message", None)
        if msg is None:
            return await handler(event, data)

        result = UNHANDLED
        try:
            result = await handler(event, data)
            return result
        finally:
            u = msg.from_user
            who = f"@{u.username}" if u and u.username else (u.first_name if u else "—")
            state = data.get("raw_state")
            mark = "NOT HANDLED" if result is UNHANDLED else "ok"
            logger.info(f"[{self.tag}] {who} (id:{u.id if u else '?'}) "
                        f"state={state} {mark} | {_describe(msg)}")
