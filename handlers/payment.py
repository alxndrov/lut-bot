from aiogram import Router, F, Bot
from aiogram.types import (
    CallbackQuery, LabeledPrice, PreCheckoutQuery,
    Message, ContentType
)

from config import PROVIDER_TOKEN
from keyboards.user import back_to_catalog_keyboard
import database as db

router = Router()


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery, bot: Bot):
    product_id = int(callback.data.split(":")[1])
    product = await db.get_product(product_id)

    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    if not product.get("file_id"):
        await callback.answer("Файл ещё не загружен. Попробуй позже.", show_alert=True)
        return

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=product["name"],
        description=product["description"],
        payload=f"product:{product['id']}",
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label=product["name"], amount=product["price"] * 100)],
        # Цена в копейках (умножаем на 100)
        start_parameter=f"buy_{product['id']}",
    )
    await callback.answer()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    # Проверяем что товар ещё существует и файл на месте
    payload = query.invoice_payload  # "product:ID"
    try:
        product_id = int(payload.split(":")[1])
        product = await db.get_product(product_id)
        if product and product.get("file_id"):
            await query.answer(ok=True)
            return
    except Exception:
        pass
    await query.answer(ok=False, error_message="Товар недоступен. Попробуй позже.")


@router.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: Message, bot: Bot):
    payment = message.successful_payment
    payload = payment.invoice_payload  # "product:ID"

    try:
        product_id = int(payload.split(":")[1])
    except Exception:
        await message.answer("Оплата прошла, но произошла ошибка. Напишите в поддержку.")
        return

    product = await db.get_product(product_id)
    if not product or not product.get("file_id"):
        await message.answer("Оплата прошла, но файл недоступен. Напишите в поддержку.")
        return

    # Сохраняем покупку
    await db.add_purchase(
        user_id=message.from_user.id,
        username=message.from_user.username,
        product_id=product_id,
        telegram_payment_id=payment.telegram_payment_charge_id,
        amount=payment.total_amount // 100,
    )

    # Отправляем файл
    await message.answer("✅ Оплата прошла! Вот твой файл:")
    await bot.send_document(
        chat_id=message.from_user.id,
        document=product["file_id"],
        caption=f"<b>{product['name']}</b>\n\nСпасибо за покупку! 🎉",
        parse_mode="HTML",
        reply_markup=back_to_catalog_keyboard(),
    )
