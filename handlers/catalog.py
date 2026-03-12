from aiogram import Router, F
from aiogram.types import CallbackQuery

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

    file_status = "📥 После оплаты файл сразу придёт в этот чат" if product.get("file_id") else "⚠️ Файл ещё не загружен"
    text = (
        f"<b>{product['name']}</b>\n\n"
        f"{product['description']}\n\n"
        f"💰 Цена: <b>{product['price']} ₽</b>\n"
        f"{file_status}"
    )

    if product.get("photo_id"):
        await callback.message.delete()
        await callback.message.answer_photo(
            photo=product["photo_id"],
            caption=text,
            parse_mode="HTML",
            reply_markup=product_keyboard(product_id),
        )
    else:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=product_keyboard(product_id),
        )

    await callback.answer()
