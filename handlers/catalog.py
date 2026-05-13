from aiogram import Router, F
from aiogram.types import CallbackQuery

import config
from keyboards.user import catalog_keyboard, product_keyboard
import database as db
from handlers.start import show_catalog

router = Router()


@router.callback_query(F.data == "catalog")
async def cb_catalog(callback: CallbackQuery):
    await show_catalog(callback, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("product:"))
async def cb_product(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    product = await db.get_product(product_id)

    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    admin = callback.from_user.id in config.ADMIN_IDS
    if not product.get("active") and not admin:
        await callback.answer("Товар недоступен.", show_alert=True)
        return

    category = product.get("category", "digital")
    purchase_count = await db.get_product_purchase_count(product_id)
    effective_price = await db.get_effective_price(product)

    if category == "waitlist":
        category_label = "📋 Список ожидания"
        file_status = "📋 Запишитесь и мы сообщим о появлении"
    elif category == "physical":
        category_label = ""
        file_status = "🚚 Доставка впоследствии будет осуществляться удобным вам способом"
    elif category == "infobiz":
        category_label = ""
        has_file = bool(product.get("file_id"))
        has_instruction = bool(product.get("instruction_file_id"))
        has_video = bool(product.get("video_url"))
        if not has_file:
            file_status = "⚠️ Файл ещё не загружен"
        else:
            parts = ["материалы курса"]
            if has_instruction:
                parts.append("инструкцию")
            if has_video:
                parts.append("видео-урок")
            file_status = "📥 После оплаты вы получите: " + ", ".join(parts)
    else:
        category_label = "📦 Цифровой товар"
        has_file = bool(product.get("file_id"))
        has_instruction = bool(product.get("instruction_file_id"))
        has_video = bool(product.get("video_url"))
        if not has_file:
            file_status = "⚠️ Файл ещё не загружен"
        else:
            parts = ["файл с пресетом"]
            if has_instruction:
                parts.append("подробную инструкцию в PDF")
            if has_video:
                parts.append("ссылку на видео-урок")
            file_status = "📥 После оплаты вы получите: " + ", ".join(parts)

    label_line = f"{category_label}\n" if category_label else ""

    # Счётчик для инфобиз (если включён)
    counter_line = ""
    if category == "infobiz" and product.get("counter_visible"):
        counter_line = f"🔥 Уже купили: <b>{purchase_count}</b>\n"

    text = (
        f"<b>{product['name']}</b>\n\n"
        f"<i>{product['description']}</i>\n\n"
        f"{counter_line}"
        f"💰 Цена: <b>{effective_price} ₽</b>\n"
        f"{label_line}"
        f"{file_status}"
    )

    kb = product_keyboard(product, user_id=callback.from_user.id,
                          effective_price=effective_price, is_admin=admin)

    if product.get("photo_id"):
        await callback.message.delete()
        await callback.message.answer_photo(
            photo=product["photo_id"],
            caption=text,
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=kb,
        )

    await callback.answer()
