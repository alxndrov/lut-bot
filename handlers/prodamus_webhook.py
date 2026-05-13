"""
aiohttp-обработчик вебхуков от Prodamus.
Подпись — в HTTP-заголовке Sign.
order_id в URL → order_num в вебхуке (сквозной идентификатор).
"""
import logging
from datetime import datetime, timezone

from aiohttp import web
from aiogram import Bot

import config
import database as db
from services.prodamus import verify_webhook

logger = logging.getLogger(__name__)


async def handle_webhook(request: web.Request) -> web.Response:
    bot: Bot = request.app["bot"]

    try:
        raw = dict(await request.post())
    except Exception as e:
        logger.error(f"prodamus webhook: ошибка разбора тела: {e}")
        return web.Response(text="bad request", status=400)

    logger.info(f"prodamus webhook: headers.Sign={request.headers.get('Sign')!r}, body={raw}")

    # Проверяем подпись из заголовка Sign
    if config.PRODAMUS_SECRET:
        sign_header = request.headers.get("Sign", "")
        if not verify_webhook(dict(raw), sign_header, config.PRODAMUS_SECRET):
            logger.warning("prodamus webhook: неверная подпись")
            return web.Response(text="invalid sign", status=403)

    # Неуспешный платёж — уведомляем покупателя если знаем его user_id
    if raw.get("payment_status") != "success":
        order_num = raw.get("order_num", "")
        if order_num:
            try:
                parts = order_num.split("_", 2)
                user_id = int(parts[1])
                await bot.send_message(
                    user_id,
                    "😔 К сожалению, оплата не прошла.\n\n"
                    "Попробуйте ещё раз — вернитесь в каталог и нажмите «Купить».",
                    reply_markup=_back_to_catalog_keyboard(),
                )
            except Exception:
                pass
        return web.Response(text="ok")

    # Сквозной параметр order_num: "{order_type}_{user_id}_{product_id}"
    order_num = raw.get("order_num", "")
    if not order_num:
        logger.warning(
            f"prodamus webhook: нет order_num — ручной платёж из дашборда? "
            f"(order_id={raw.get('order_id')}, payment_init={raw.get('payment_init')})"
        )
        return web.Response(text="ok")

    try:
        parts = order_num.split("_", 2)
        order_type = parts[0]        # "d" или "p"
        user_id = int(parts[1])
        product_id = int(parts[2])
    except Exception:
        logger.error(f"prodamus webhook: неверный order_num={order_num!r}")
        return web.Response(text="bad params", status=400)

    prodamus_order_id = raw.get("order_id", "")
    try:
        amount = int(float(raw.get("sum") or raw.get("payment_sum") or 0))
    except Exception:
        amount = 0

    product = await db.get_product(product_id)
    if not product:
        logger.error(f"prodamus webhook: товар {product_id} не найден")
        return web.Response(text="product not found", status=404)

    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    # Имя/юзернейм из таблицы users
    username, first_name = None, "—"
    try:
        import aiosqlite
        async with aiosqlite.connect(db.DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT username, first_name FROM users WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    username = row["username"]
                    first_name = row["first_name"] or "—"
    except Exception as e:
        logger.warning(f"prodamus webhook: не удалось получить данные юзера: {e}")

    username_str = f"@{username}" if username else f"id:{user_id}"

    if order_type == "d":
        category = product.get("category", "digital")

        purchase_num = await db.add_purchase(
            user_id=user_id, username=username, product_id=product_id,
            telegram_payment_id=prodamus_order_id, amount=amount,
        )
        await db.cancel_funnel_for_user(user_id, product_id)

        await _send_notify(bot, (
            f"💰 <b>Новая покупка</b>\n\n"
            f"👤 {first_name} {username_str}\n"
            f"🛍 {product['name']}\n"
            f"💵 {amount} ₽\n"
            f"🕐 {now}"
        ))

        await bot.send_message(user_id, "✅ Оплата прошла! Спасибо за покупку 🎉")

        if category == "infobiz":
            await _deliver_infobiz(bot, user_id, product, purchase_num,
                                   username=username, first_name=first_name)
        else:
            await _deliver_digital(bot, user_id, product)

    elif order_type == "p":
        delivery_info = await db.get_pending_delivery(user_id, product_id) or "не указан"
        await db.delete_pending_delivery(user_id, product_id)

        await db.add_purchase(
            user_id=user_id, username=username, product_id=product_id,
            telegram_payment_id=prodamus_order_id, amount=amount,
        )

        await _send_notify(bot, (
            f"💰 <b>Новый заказ (физический товар)</b>\n\n"
            f"👤 {first_name} {username_str}\n"
            f"🛍 {product['name']}\n"
            f"💵 {amount} ₽\n"
            f"📦 Доставка: {delivery_info}\n"
            f"🕐 {now}"
        ))

        await bot.send_message(
            user_id,
            "✅ Оплата прошла! Ваш заказ принят.\n\n"
            "В ближайшее время мы свяжемся с вами для уточнения деталей доставки.",
        )
    else:
        logger.error(f"prodamus webhook: неизвестный order_type={order_type!r}")

    return web.Response(text="ok")


async def _deliver_digital(bot: Bot, user_id: int, product: dict):
    """Отправляет файл цифрового товара."""
    if not product.get("file_id"):
        await bot.send_message(user_id, "Файл недоступен. Напишите в поддержку.")
        return

    await bot.send_document(
        chat_id=user_id,
        document=product["file_id"],
        caption=f"<b>{product['name']}</b> — пресет",
        parse_mode="HTML",
    )

    if product.get("instruction_file_id"):
        if product.get("instruction_type") == "photo":
            await bot.send_photo(
                chat_id=user_id, photo=product["instruction_file_id"],
                caption="📄 <b>Инструкция по применению</b>", parse_mode="HTML",
            )
        else:
            await bot.send_document(
                chat_id=user_id, document=product["instruction_file_id"],
                caption="📄 <b>Инструкция по применению</b>", parse_mode="HTML",
            )

    if product.get("video_url"):
        await bot.send_message(
            user_id, f"🎬 <b>Видео-урок:</b> {product['video_url']}", parse_mode="HTML",
        )

    await bot.send_message(user_id, "Удачи! 🌟")


async def _deliver_infobiz(bot: Bot, user_id: int, product: dict, purchase_num: int = 0,
                           username: str | None = None, first_name: str = "",
                           test_mode: bool = False):
    """Выдаёт ссылку с заявкой на вступление — бот одобрит только этого пользователя."""
    channel_id = product.get("channel_id")
    invite_link = product.get("channel_invite_link")

    if channel_id and invite_link:
        if not test_mode:
            # Записываем право доступа — бот одобрит заявку в channel_access handler
            await db.grant_channel_access(user_id, str(channel_id))
        await bot.send_message(
            user_id,
            f"{'🧪 [ТЕСТ] ' if test_mode else ''}"
            f"🔐 <b>Доступ к закрытому каналу</b>\n\n"
            f"Нажми на ссылку ниже и отправь заявку на вступление — "
            f"бот одобрит её автоматически:\n\n"
            f"{invite_link}\n\n"
            f"Ссылку можно использовать повторно (если выйдешь и захочешь вернуться).",
            parse_mode="HTML",
        )
    else:
        logger.warning(f"infobiz product {product['id']}: channel_id или invite_link не заданы")
        await bot.send_message(
            user_id,
            f"{'🧪 [ТЕСТ] ' if test_mode else ''}"
            "Оплата прошла! Доступ к каналу будет выдан в ближайшее время.",
        )

    # Бонус для первых N покупателей — персональный разбор
    bonus_limit = product.get("bonus_limit")
    if bonus_limit and purchase_num and purchase_num <= bonus_limit:
        if not test_mode:
            # Записываем победителя только при реальной покупке
            await db.add_bonus_winner(
                user_id=user_id,
                username=username,
                first_name=first_name,
                product_id=product["id"],
            )
        bonus_text = product.get("bonus_text") or (
            f"🎁 <b>Ты попал в число первых {bonus_limit} покупателей!</b>\n\n"
            f"Ты получаешь персональный разбор своего ролика 🎬\n\n"
            f"Пришли ссылку на свой ролик в любое удобное время — "
            f"Миша посмотрит и напишет тебе разбор в личку."
        )
        prefix = "🧪 [ТЕСТ] — бонус сработал:\n\n" if test_mode else ""
        await bot.send_message(user_id, prefix + bonus_text, parse_mode="HTML")
        logger.info(
            f"infobiz bonus {'(test) ' if test_mode else ''}user {user_id} "
            f"winner #{purchase_num}/{bonus_limit}, product {product['id']}"
        )

    # Дополнительные материалы (если есть)
    if product.get("file_id"):
        await bot.send_document(
            chat_id=user_id,
            document=product["file_id"],
            caption=f"<b>{product['name']}</b> — материалы",
            parse_mode="HTML",
        )

    if product.get("instruction_file_id"):
        if product.get("instruction_type") == "photo":
            await bot.send_photo(
                chat_id=user_id, photo=product["instruction_file_id"],
                caption="📄 <b>Инструкция</b>", parse_mode="HTML",
            )
        else:
            await bot.send_document(
                chat_id=user_id, document=product["instruction_file_id"],
                caption="📄 <b>Инструкция</b>", parse_mode="HTML",
            )

    if product.get("video_url"):
        await bot.send_message(
            user_id, f"🎬 <b>Видео-урок:</b> {product['video_url']}", parse_mode="HTML",
        )


async def _send_notify(bot: Bot, text: str):
    try:
        notify_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
        for admin_id in config.ADMIN_IDS:
            try:
                await notify_bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Notify admin {admin_id} failed: {e}")
        await notify_bot.session.close()
    except Exception as e:
        logger.error(f"Failed to create notify bot: {e}")


def _back_to_catalog_keyboard():
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В каталог", callback_data="catalog")]
    ])


def create_app(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/prodamus/webhook", handle_webhook)
    return app
