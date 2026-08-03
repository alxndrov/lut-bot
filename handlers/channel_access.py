"""
Обработчик заявок на вступление в закрытый канал (ChatJoinRequest).
Одобряет только тех, кто есть в таблице channel_access (т.е. оплатил).
"""
import logging
import aiosqlite
from aiogram import Router
from aiogram.types import ChatJoinRequest, InlineKeyboardMarkup, InlineKeyboardButton

import config
import database as db
from services.prodamus import build_payment_url

router = Router()
logger = logging.getLogger(__name__)


async def _get_product_by_channel(channel_id: str) -> dict | None:
    """Находит инфобиз-товар по channel_id."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM products WHERE channel_id = ? AND active = 1 LIMIT 1",
            (channel_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


@router.chat_join_request()
async def handle_join_request(request: ChatJoinRequest):
    user_id = request.from_user.id
    channel_id = str(request.chat.id)

    has_access = await db.check_channel_access(user_id, channel_id)

    if has_access:
        await request.approve()
        logger.info(f"channel_access: одобрен user {user_id} в канал {channel_id}")
        return

    await request.decline()
    logger.info(f"channel_access: отклонён user {user_id} в канал {channel_id}")

    try:
        product = await _get_product_by_channel(channel_id)

        if product and config.PRODAMUS_SHOP_URL:
            effective_price = await db.get_effective_price(product)
            pay_url = build_payment_url(
                shop_url=config.PRODAMUS_SHOP_URL,
                product_name=product["name"],
                price=effective_price,
                user_id=user_id,
                product_id=product["id"],
                order_type="d",
                secret=config.PRODAMUS_SECRET,
                notification_url=config.PRODAMUS_WEBHOOK_URL,
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"💳 Купить за {effective_price} ₽",
                    url=pay_url,
                )],
            ])
            text = (
                f"Привет! Этот канал доступен только после покупки "
                f"<b>{product['name']}</b>.\n\n"
                f"Оформи заказ — и ссылка для вступления придёт автоматически 👇"
            )
        else:
            keyboard = None
            text = (
                "Привет! Этот канал доступен только после покупки.\n\n"
                "Если ты уже оплатил — напиши нам, разберёмся 🙌"
            )

        await request.bot.send_message(
            user_id, text, parse_mode="HTML", reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"channel_access: ошибка отправки сообщения user {user_id}: {e}")
