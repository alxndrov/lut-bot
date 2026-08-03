from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

import config
from keyboards.user import catalog_keyboard, product_keyboard
import database as db
from handlers.start import show_catalog
from handlers.brief_handler import start_order_flow

router = Router()


@router.callback_query(F.data == "catalog")
async def cb_catalog(callback: CallbackQuery, state: FSMContext):
    # Возврат в каталог отменяет незавершённое оформление заказа
    await state.clear()
    await show_catalog(callback, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("product:"))
async def cb_product(callback: CallbackQuery, state: FSMContext):
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
        file_status = "📋 Запишись — я сообщу тебе, как только появится"
    elif category == "physical":
        category_label = ""
        # Заказ оформляется прямо здесь — сразу объясняем, что будет дальше
        if await db.get_product_questions(product_id):
            file_status = (
                "🛒 Для оформления заказа ответьте на несколько вопросов ниже — "
                "это займёт минуту.\nОтвет можно писать текстом или прикреплять фото."
            )
        else:
            file_status = "🛒 Для оформления заказа опишите свой запрос в сообщении ниже 👇"
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

    # Описание можно не заполнять — тогда его блок не выводим вовсе
    desc = (product.get("description") or "").strip()
    desc_block = f"<i>{desc}</i>\n\n" if desc and desc != "-" else ""

    text = (
        f"<b>{product['name']}</b>\n\n"
        f"{desc_block}"
        f"{counter_line}"
        f"💰 Цена: <b>{effective_price} ₽</b>\n"
        f"{label_line}"
        f"{file_status}"
    ).rstrip()

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
    elif callback.message.photo:
        # Текущее сообщение — фото (каталог с баннером), edit_text невозможен
        await callback.message.delete()
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=kb,
        )

    await callback.answer()

    # Физтовар: оформление стартует сразу за карточкой, без промежуточного шага
    if category == "physical":
        await start_order_flow(callback.message, state, product)
