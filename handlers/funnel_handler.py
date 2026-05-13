"""
Пользовательская часть воронки лид-магнита.
Кнопка callback_data="funnel:start:{funnel_id}" запускает воронку.
"""
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery

import database as db

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("funnel:start:"))
async def cb_funnel_start(callback: CallbackQuery):
    funnel_id = callback.data.split(":", 2)[2]
    user_id = callback.from_user.id

    funnel = await db.get_funnel(funnel_id)
    if not funnel or not funnel["active"]:
        await callback.answer("Недоступно.", show_alert=True)
        return

    # Если уже купил — воронка не нужна
    if funnel.get("product_id"):
        purchase = await db.get_purchase(user_id, funnel["product_id"])
        if purchase:
            await callback.answer("У тебя уже есть доступ! 🎉", show_alert=True)
            return

    # Берём источник пользователя (ref из /start) и передаём в воронку
    user_ref = await db.get_user_ref(user_id)
    await db.enqueue_funnel(user_id, funnel_id, source=user_ref)
    await callback.answer()

    # Первый шаг отправляется мгновенно — воркер подхватит его через ≤30 сек,
    # но можно и сразу для лучшего UX:
    steps = await db.get_funnel_steps(funnel_id)
    first = next((s for s in steps if s["step"] == 0), None)
    if first:
        try:
            await callback.message.answer(first["text"], parse_mode="HTML")
            # Помечаем нулевой шаг как отправленный, чтобы воркер не дублировал
            async with __import__("aiosqlite").connect(db.DB_PATH) as conn:
                await conn.execute(
                    """UPDATE funnel_queue SET sent = 1
                       WHERE user_id = ? AND funnel_id = ? AND step = 0
                         AND sent = 0 AND cancelled = 0""",
                    (user_id, funnel_id),
                )
                await conn.commit()
        except Exception as e:
            logger.error(f"funnel first step error: {e}")
