import asyncio
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram import Bot
from aiogram.types import Message, CallbackQuery
from datetime import datetime, timedelta, timezone

import config
from config import ADMIN_IDS
from keyboards.admin import (
    admin_menu_keyboard, admin_products_keyboard,
    admin_product_keyboard, admin_back_keyboard,
    confirm_delete_keyboard, category_keyboard,
    finance_keyboard, debt_keyboard, stats_keyboard,
    funnels_keyboard, funnel_keyboard,
)
import logging
logger = logging.getLogger(__name__)
import database as db
from services.daily_report import fetch_payments_summary

MSK = timezone(timedelta(hours=3))

router = Router()


# --- FSM States ---

class AddProduct(StatesGroup):
    name = State()
    description = State()
    price = State()


class EditProduct(StatesGroup):
    name = State()
    description = State()
    price = State()


class UploadFile(StatesGroup):
    waiting_file = State()
    product_id = State()


class UploadPhoto(StatesGroup):
    waiting_photo = State()
    product_id = State()


class UploadInstruction(StatesGroup):
    waiting_file = State()


class SetVideo(StatesGroup):
    waiting_url = State()


class UploadBanner(StatesGroup):
    waiting_photo = State()


class SetPriceTrigger(StatesGroup):
    waiting_count = State()


class SetPriceAfterTrigger(StatesGroup):
    waiting_price = State()


class SetChannelId(StatesGroup):
    waiting_id = State()


class CreateFunnel(StatesGroup):
    waiting_name = State()


class FunnelAddStep(StatesGroup):
    waiting_delay = State()
    waiting_text = State()


class FunnelEditStep(StatesGroup):
    waiting_delay = State()
    waiting_text = State()


class FunnelSetProduct(StatesGroup):
    waiting_product_id = State()


class SetBonusLimit(StatesGroup):
    waiting_limit = State()
    waiting_text = State()


# --- Фильтр: только для админов ---

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# --- Команда /admin ---

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🛠 Панель администратора", reply_markup=admin_menu_keyboard())


# --- Меню ---

@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.message.edit_text("🛠 Панель администратора", reply_markup=admin_menu_keyboard())
    await callback.answer()


# --- Список товаров ---

@router.callback_query(F.data == "admin:products")
async def cb_admin_products(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    products = await db.get_all_products(active_only=False)
    if not products:
        await callback.message.edit_text(
            "Товаров нет.", reply_markup=admin_back_keyboard()
        )
    else:
        await callback.message.edit_text(
            "📦 Все товары:", reply_markup=admin_products_keyboard(products)
        )
    await callback.answer()


# --- Карточка товара ---

async def _product_card_text(product: dict, purchase_count: int = 0) -> str:
    file_info = f"📎 {product['file_name']}" if product.get("file_name") else "📎 Файл не загружен"
    photo_info = "🖼 Фото есть" if product.get("photo_id") else "🖼 Фото нет"
    status = "✅ Активен" if product["active"] else "❌ Скрыт"
    cat = product.get("category", "digital")
    if cat == "physical":
        cat_label = "🚚 Физический"
    elif cat == "waitlist":
        cat_label = "📋 Список ожидания"
    elif cat == "infobiz":
        cat_label = "📚 Инфобиз"
    else:
        cat_label = "📦 Цифровой"

    text = (
        f"<b>{product['name']}</b>\n"
        f"Цена: {product['price']} ₽\n"
        f"Статус: {status}\n"
        f"Категория: {cat_label}\n"
        f"{file_info}\n"
        f"{photo_info}\n"
    )

    if cat == "infobiz":
        trigger = product.get("price_trigger")
        after = product.get("price_after_trigger")
        effective = after if (trigger and after and purchase_count >= trigger) else product["price"]
        text += (
            f"\n📊 Покупок: <b>{purchase_count}</b>\n"
            f"💰 Текущая цена: <b>{effective} ₽</b>\n"
        )
        if trigger and after:
            text += f"⚡ Триггер: после {trigger} покупок → {after} ₽\n"

        bonus_limit = product.get("bonus_limit")
        if bonus_limit:
            remaining = max(bonus_limit - purchase_count, 0)
            text += f"🎁 Разбор: первые {bonus_limit} чел. ({remaining} мест осталось)\n"

    text += f"\n{product['description']}"
    return text


@router.callback_query(F.data.startswith("admin:product:"))
async def cb_admin_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    product = await db.get_product(product_id)
    if not product:
        await callback.answer("Не найден.", show_alert=True)
        return

    purchase_count = await db.get_product_purchase_count(product_id)
    text = await _product_card_text(product, purchase_count)

    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=admin_product_keyboard(product, purchase_count)
        )
    except Exception:
        pass
    await callback.answer()


# --- Добавить товар ---

@router.callback_query(F.data == "admin:add_product")
async def cb_add_product(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AddProduct.name)
    await callback.message.edit_text(
        "Введи <b>название</b> товара:",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(AddProduct.name)
async def fsm_product_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(AddProduct.description)
    await message.answer("Введи <b>описание</b> товара:", parse_mode="HTML")


@router.message(AddProduct.description)
async def fsm_product_description(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(description=message.text.strip())
    await state.set_state(AddProduct.price)
    await message.answer("Введи <b>цену</b> в рублях (только число):", parse_mode="HTML")


@router.message(AddProduct.price)
async def fsm_product_price(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи корректную цену (целое число больше 0).")
        return

    data = await state.get_data()
    product_id = await db.add_product(data["name"], data["description"], price)
    await state.clear()

    product = await db.get_product(product_id)
    await message.answer(
        f"✅ Товар <b>{product['name']}</b> создан!\n\n"
        f"Теперь загрузи файл через карточку товара.",
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product),
    )


# --- Редактировать название ---

@router.callback_query(F.data.startswith("admin:edit_name:"))
async def cb_edit_name(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(EditProduct.name)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "Введи новое <b>название</b> товара:",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(EditProduct.name)
async def fsm_edit_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    await db.update_product_name(product_id, message.text.strip())
    await state.clear()
    product = await db.get_product(product_id)
    await message.answer(
        f"✅ Название обновлено: <b>{product['name']}</b>",
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product),
    )


# --- Редактировать описание ---

@router.callback_query(F.data.startswith("admin:edit_desc:"))
async def cb_edit_desc(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(EditProduct.description)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "Введи новое <b>описание</b> товара:",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(EditProduct.description)
async def fsm_edit_description(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    await db.update_product_description(product_id, message.text.strip())
    await state.clear()
    product = await db.get_product(product_id)
    await message.answer(
        "✅ Описание обновлено.",
        reply_markup=admin_product_keyboard(product),
    )


# --- Редактировать цену ---

@router.callback_query(F.data.startswith("admin:edit_price:"))
async def cb_edit_price(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(EditProduct.price)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "Введи новую <b>цену</b> в рублях (только число):",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(EditProduct.price)
async def fsm_edit_price(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи корректную цену (целое число больше 0).")
        return
    data = await state.get_data()
    product_id = data["product_id"]
    await db.update_product_price(product_id, price)
    await state.clear()
    product = await db.get_product(product_id)
    await message.answer(
        f"✅ Цена обновлена: <b>{product['price']} ₽</b>",
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product),
    )


# --- Загрузить файл ---

@router.callback_query(F.data.startswith("admin:upload_file:"))
async def cb_upload_file(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(UploadFile.waiting_file)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "📎 Отправь файл (документ) для этого товара:",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(UploadFile.waiting_file, F.document)
async def fsm_receive_file(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    file_id = message.document.file_id
    file_name = message.document.file_name or "file"

    await db.set_product_file(product_id, file_id, file_name)
    await state.clear()

    product = await db.get_product(product_id)
    await message.answer(
        f"✅ Файл <b>{file_name}</b> прикреплён к товару.",
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product),
    )


# --- Загрузить фото ---

@router.callback_query(F.data.startswith("admin:upload_photo:"))
async def cb_upload_photo(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(UploadPhoto.waiting_photo)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "🖼 Отправь фото для этого товара:",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(UploadPhoto.waiting_photo, F.photo)
async def fsm_receive_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    photo_id = message.photo[-1].file_id  # берём наибольшее разрешение

    await db.set_product_photo(product_id, photo_id)
    await state.clear()

    product = await db.get_product(product_id)
    await message.answer(
        "✅ Фото прикреплено.",
        reply_markup=admin_product_keyboard(product),
    )


# --- Переключить активность ---

@router.callback_query(F.data.startswith("admin:toggle:"))
async def cb_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    product = await db.get_product(product_id)
    if not product:
        await callback.answer("Не найден.", show_alert=True)
        return

    new_active = not product["active"]
    await db.update_product_active(product_id, new_active)
    product = await db.get_product(product_id)

    status = "показан" if new_active else "скрыт"
    await callback.answer(f"Товар {status}.")

    # Обновляем карточку через общую функцию
    purchase_count = await db.get_product_purchase_count(product["id"])
    text = await _product_card_text(product, purchase_count)
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=admin_product_keyboard(product, purchase_count)
        )
    except Exception:
        pass


# --- Удалить товар ---

@router.callback_query(F.data.startswith("admin:delete:"))
async def cb_delete(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await callback.message.edit_text(
        "Ты уверен, что хочешь удалить этот товар?",
        reply_markup=confirm_delete_keyboard(product_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:confirm_delete:"))
async def cb_confirm_delete(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await db.delete_product(product_id)
    await callback.answer("Удалено.", show_alert=True)
    products = await db.get_all_products(active_only=False)
    await callback.message.edit_text(
        "📦 Все товары:",
        reply_markup=admin_products_keyboard(products) if products else admin_back_keyboard(),
    )


# --- Загрузить инструкцию (PDF) ---

@router.callback_query(F.data.startswith("admin:upload_instruction:"))
async def cb_upload_instruction(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(UploadInstruction.waiting_file)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "📄 Отправь файл инструкции — PDF или картинку (JPG, PNG):",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(UploadInstruction.waiting_file, F.document)
async def fsm_receive_instruction_doc(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    file_id = message.document.file_id
    file_name = message.document.file_name or "instruction.pdf"

    await db.set_product_instruction(product_id, file_id, file_name, file_type="document")
    await state.clear()

    product = await db.get_product(product_id)
    await message.answer(
        f"✅ Инструкция <b>{file_name}</b> прикреплена к товару.",
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product),
    )


@router.message(UploadInstruction.waiting_file, F.photo)
async def fsm_receive_instruction_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    file_id = message.photo[-1].file_id

    await db.set_product_instruction(product_id, file_id, "instruction.jpg", file_type="photo")
    await state.clear()

    product = await db.get_product(product_id)
    await message.answer(
        "✅ Картинка-инструкция прикреплена к товару.",
        reply_markup=admin_product_keyboard(product),
    )


# --- Установить ссылку на видео ---

@router.callback_query(F.data.startswith("admin:set_video:"))
async def cb_set_video(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(SetVideo.waiting_url)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "🎬 Отправь ссылку на видео-урок (YouTube, Vimeo и т.д.):\n\n"
        "Чтобы <b>удалить</b> текущую ссылку — отправь <code>-</code>",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(SetVideo.waiting_url)
async def fsm_receive_video_url(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    url = message.text.strip()
    if url == "-":
        url = ""

    await db.set_product_video_url(product_id, url)
    await state.clear()

    product = await db.get_product(product_id)
    text = "✅ Ссылка на видео удалена." if not url else f"✅ Ссылка на видео сохранена:\n{url}"
    await message.answer(text, reply_markup=admin_product_keyboard(product))


# --- Ссылка Prodamus ---

# --- Выбор категории ---

@router.callback_query(F.data.startswith("admin:set_category:"))
async def cb_set_category(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await callback.message.edit_text(
        "Выберите категорию товара:",
        reply_markup=category_keyboard(product_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:category:"))
async def cb_category_select(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split(":")
    category = parts[2]
    product_id = int(parts[3])
    await db.update_product_category(product_id, category)
    product = await db.get_product(product_id)

    labels = {"physical": "🚚 Физический", "waitlist": "📋 Список ожидания",
              "infobiz": "📚 Инфобиз", "digital": "📦 Цифровой"}
    cat_label = labels.get(category, "📦 Цифровой")

    purchase_count = await db.get_product_purchase_count(product_id)
    text = await _product_card_text(product, purchase_count)
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=admin_product_keyboard(product, purchase_count)
        )
    except Exception:
        pass
    await callback.answer(f"Категория изменена на {cat_label}")


# --- Баннер каталога ---

@router.callback_query(F.data == "admin:upload_banner")
async def cb_upload_banner(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(UploadBanner.waiting_photo)
    await callback.message.edit_text(
        "🖼 Отправь картинку, которая будет показываться при открытии каталога.\n\n"
        "Чтобы <b>удалить</b> баннер — отправь <code>-</code>",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(UploadBanner.waiting_photo, F.photo)
async def fsm_receive_banner(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    file_id = message.photo[-1].file_id
    await db.set_setting("catalog_banner_file_id", file_id)
    await state.clear()
    await message.answer(
        "✅ Баннер каталога обновлён! Теперь он будет показываться при открытии каталога.",
        reply_markup=admin_menu_keyboard(),
    )


@router.message(UploadBanner.waiting_photo, F.text == "-")
async def fsm_remove_banner(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await db.set_setting("catalog_banner_file_id", "")
    await state.clear()
    await message.answer(
        "✅ Баннер каталога удалён.",
        reply_markup=admin_menu_keyboard(),
    )


# --- Инфобиз: счётчик и динамическая цена ---

@router.callback_query(F.data.startswith("admin:toggle_counter:"))
async def cb_toggle_counter(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    product = await db.get_product(product_id)
    if not product:
        await callback.answer("Не найден.", show_alert=True)
        return
    new_visible = not product.get("counter_visible", False)
    await db.set_infobiz_counter_visible(product_id, new_visible)
    product = await db.get_product(product_id)
    purchase_count = await db.get_product_purchase_count(product_id)
    text = await _product_card_text(product, purchase_count)
    label = "виден" if new_visible else "скрыт"
    await callback.answer(f"Счётчик теперь {label}")
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=admin_product_keyboard(product, purchase_count)
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin:set_price_trigger:"))
async def cb_set_price_trigger(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(SetPriceTrigger.waiting_count)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "🔢 Введи количество покупок, после которого цена изменится:\n\n"
        "<i>Например: 30</i>",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(SetPriceTrigger.waiting_count)
async def fsm_price_trigger(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        count = int(message.text.strip())
        if count <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое число больше 0.")
        return
    data = await state.get_data()
    product_id = data["product_id"]
    await db.set_infobiz_price_trigger(product_id, count)
    await state.clear()
    product = await db.get_product(product_id)
    purchase_count = await db.get_product_purchase_count(product_id)
    await message.answer(
        f"✅ Триггер установлен: после <b>{count}</b> покупок цена изменится.",
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product, purchase_count),
    )


@router.callback_query(F.data.startswith("admin:set_price_after:"))
async def cb_set_price_after(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(SetPriceAfterTrigger.waiting_price)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "💲 Введи новую цену (в рублях), которая будет после триггера:\n\n"
        "<i>Например: 5000</i>",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(SetPriceAfterTrigger.waiting_price)
async def fsm_price_after_trigger(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое число больше 0.")
        return
    data = await state.get_data()
    product_id = data["product_id"]
    await db.set_infobiz_price_after_trigger(product_id, price)
    await state.clear()
    product = await db.get_product(product_id)
    purchase_count = await db.get_product_purchase_count(product_id)
    await message.answer(
        f"✅ Цена после триггера установлена: <b>{price} ₽</b>",
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product, purchase_count),
    )


@router.callback_query(F.data.startswith("admin:set_channel:"))
async def cb_set_channel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(SetChannelId.waiting_id)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "📢 <b>Закрытый канал для доступа после оплаты</b>\n\n"
        "Введи ID канала или его @username.\n\n"
        "<b>Как узнать ID:</b> добавь @userinfobot в канал, он напишет ID вида <code>-1001234567890</code>\n\n"
        "⚠️ Убедись, что бот является администратором канала с правом приглашать участников.\n\n"
        "Чтобы <b>убрать</b> канал — отправь <code>-</code>",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(SetChannelId.waiting_id)
async def fsm_set_channel_id(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    raw = message.text.strip()
    channel_id = None if raw == "-" else raw

    invite_link = None
    if channel_id:
        try:
            link = await bot.create_chat_invite_link(
                chat_id=channel_id,
                creates_join_request=True,
                name="Infobiz access",
            )
            invite_link = link.invite_link
        except Exception as e:
            logger.error(f"create_chat_invite_link error: {e}")
            await message.answer(
                f"⚠️ Канал сохранён, но ссылку создать не удалось.\n"
                f"Убедись, что бот — администратор канала.\n\nОшибка: <code>{e}</code>",
                parse_mode="HTML",
            )

    await db.set_product_channel_id(product_id, channel_id, invite_link)
    await state.clear()

    product = await db.get_product(product_id)
    purchase_count = await db.get_product_purchase_count(product_id)
    label = f"<code>{channel_id}</code>" if channel_id else "удалён"
    link_info = f"\n🔗 Ссылка: <code>{invite_link}</code>" if invite_link else ""
    await message.answer(
        f"✅ Канал {label} сохранён.{link_info}",
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product, purchase_count),
    )


# --- Инфобиз: бонус для первых N покупателей ---

@router.callback_query(F.data.startswith("admin:set_bonus:"))
async def cb_set_bonus(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    product = await db.get_product(product_id)
    current = ""
    if product.get("bonus_limit"):
        current = f"\n\n<i>Сейчас: первые <b>{product['bonus_limit']}</b> чел.</i>"
    await state.set_state(SetBonusLimit.waiting_limit)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        f"🎁 <b>Бонус-разбор для первых покупателей</b>{current}\n\n"
        "Сколько первых покупателей получат персональный разбор?\n"
        "<i>Например: <code>30</code></i>\n\n"
        "Отправь <code>0</code> — чтобы отключить.",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )
    await callback.answer()


@router.message(SetBonusLimit.waiting_limit)
async def fsm_bonus_limit(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        limit = int(message.text.strip())
        if limit < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое число ≥ 0.")
        return

    data = await state.get_data()
    product_id = data["product_id"]

    if limit == 0:
        await db.set_infobiz_bonus(product_id, None)
        await state.clear()
        product = await db.get_product(product_id)
        purchase_count = await db.get_product_purchase_count(product_id)
        await message.answer(
            "✅ Бонус-разбор отключён.",
            reply_markup=admin_product_keyboard(product, purchase_count),
        )
        return

    await state.update_data(bonus_limit=limit)
    await state.set_state(SetBonusLimit.waiting_text)
    await message.answer(
        f"✅ Лимит: первые <b>{limit}</b> покупателей получат разбор.\n\n"
        "Теперь введи текст, который получит победитель сразу после покупки.\n"
        "Или отправь <code>-</code> чтобы использовать стандартный текст.",
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )


@router.message(SetBonusLimit.waiting_text)
async def fsm_bonus_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    limit = data["bonus_limit"]
    text = None if message.text.strip() == "-" else message.text.strip()

    await db.set_infobiz_bonus(product_id, limit, text)
    await state.clear()

    product = await db.get_product(product_id)
    purchase_count = await db.get_product_purchase_count(product_id)
    await message.answer(
        f"✅ Бонус-разбор настроен!\n\n"
        f"Первые <b>{limit}</b> покупателей получат предложение прислать ссылку на ролик.",
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product, purchase_count),
    )


@router.callback_query(F.data.startswith("admin:bonus_reviews:"))
async def cb_bonus_reviews(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    product = await db.get_product(product_id)
    winners = await db.get_bonus_reviews_for_product(product_id)

    if not winners:
        await callback.answer("Победителей пока нет.", show_alert=True)
        return

    lines = [f"📋 <b>Бонусные разборы — {product['name']}</b>\n"]
    for i, w in enumerate(winners, 1):
        username_str = f"@{w['username']}" if w["username"] else f"id:{w['user_id']}"
        name = w.get("first_name") or "—"
        link_str = f"\n   🔗 <a href='{w['video_link']}'>{w['video_link'][:60]}</a>" \
            if w.get("video_link") else "\n   ⏳ Ссылка ещё не прислана"
        lines.append(f"{i}. {name} {username_str}{link_str}")

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:product:{product_id}")]
    ])
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    await callback.answer()


# --- Статистика ---

@router.callback_query(F.data == "admin:stats")
async def cb_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    s = await db.get_stats()
    text = (
        f"📊 <b>Статистика</b>\n\n"

        f"👥 <b>Аудитория</b>\n"
        f"  Всего пользователей: <b>{s['total_users']}</b>\n"
        f"  Запусков /start: <b>{s['total_launches']}</b>\n"
        f"  Новых сегодня: <b>{s['new_today']}</b>  |  "
        f"за 7 дней: <b>{s['new_week']}</b>  |  "
        f"за 30 дней: <b>{s['new_month']}</b>\n\n"

        f"💰 <b>Продажи (всё время)</b>\n"
        f"  Заказов: <b>{s['total_orders']}</b>  |  "
        f"Покупателей: <b>{s['unique_buyers']}</b>\n"
        f"  Выручка: <b>{s['total_revenue']} ₽</b>  |  "
        f"Средний чек: <b>{s['avg_order']} ₽</b>\n\n"

        f"📅 <b>За сегодня</b>\n"
        f"  Заказов: <b>{s['orders_today']}</b>  |  "
        f"Выручка: <b>{s['revenue_today']} ₽</b>\n\n"

        f"📅 <b>За 7 дней</b>\n"
        f"  Заказов: <b>{s['orders_week']}</b>  |  "
        f"Выручка: <b>{s['revenue_week']} ₽</b>\n\n"

        f"📅 <b>За 30 дней</b>\n"
        f"  Заказов: <b>{s['orders_month']}</b>  |  "
        f"Выручка: <b>{s['revenue_month']} ₽</b>\n\n"

        f"🏆 <b>Топ товар:</b> {s['top_product']}\n"
        f"📋 <b>Список ожидания:</b> {s['waitlist_total']} чел.\n"
        f"🎯 <b>Конверсия</b> (старт → покупка): <b>{s['conversion']}%</b>"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=stats_keyboard()
    )
    await callback.answer()


_MONTH_NAMES = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
]


@router.callback_query(F.data == "admin:stats_monthly")
async def cb_stats_monthly(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()

    rows = await db.get_monthly_earnings_breakdown(months=12)

    if not rows:
        await callback.message.edit_text(
            "📅 <b>Заработок по месяцам</b>\n\nДанных пока нет.",
            parse_mode="HTML",
            reply_markup=admin_back_keyboard(),
        )
        return

    fee_pct = config.PRODAMUS_FEE_PERCENT

    lines = ["📅 <b>Заработок по месяцам</b>\n"]
    for r in rows:
        gross = float(r["total"])
        fee = gross * fee_pct / 100
        net = gross - fee
        net_after_tax = net * (1 - 0.04)
        misha = net_after_tax * 0.80
        danya = net_after_tax * 0.20

        month_name = _MONTH_NAMES[int(r["month"])]
        year = r["year"]

        lines.append(
            f"<b>{month_name} {year}</b> — {r['count']} прод. — {gross:,.0f} ₽\n"
            f"  Миша: <b>{misha:,.2f} ₽</b>  |  Даня: <b>{danya:,.2f} ₽</b>"
        )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=admin_back_keyboard(),
    )


# --- Финансы (Prodamus) ---

_SHARES = [("Миша", 0.80), ("Даня", 0.20)]
_NPD_RATE = 0.04


def _fmt_period_with_shares(label: str, s: dict) -> str:
    if s["count"] == 0:
        return f"{label}\n  Продаж не было\n"
    net_after_tax = s["net"] - s["net"] * _NPD_RATE
    shares = "  ".join(
        f"{name}: <b>{net_after_tax * pct:,.2f} ₽</b>"
        for name, pct in _SHARES
    )
    return (
        f"{label}\n"
        f"  Продаж: <b>{s['count']}</b>  |  Брутто: <b>{s['gross']:,.2f} ₽</b>\n"
        f"  Комиссия ({s['fee_pct']}%): −{s['fee']:,.2f} ₽\n"
        f"  НПД 4%: −{s['net'] * _NPD_RATE:,.2f} ₽\n"
        f"  Чистыми: <b>{net_after_tax:,.2f} ₽</b>\n"
        f"  {shares}\n"
    )


def _fmt_period(label: str, s: dict) -> str:
    if s["count"] == 0:
        return f"{label}\n  Продаж не было\n"
    return (
        f"{label}\n"
        f"  Продаж: <b>{s['count']}</b>  |  Брутто: <b>{s['gross']:,.2f} ₽</b>\n"
        f"  Комиссия ({s['fee_pct']}%): −{s['fee']:,.2f} ₽  |  НДС: {s['vat']:,.2f} ₽\n"
        f"  Чистыми: <b>{s['net']:,.2f} ₽</b>\n"
    )


def _fmt_payout_block(s: dict, label: str) -> str:
    if not s or s["count"] == 0:
        return ""
    tax = s["gross"] * _NPD_RATE
    net_after_tax = s["net"] - tax
    shares = "\n".join(
        f"  {name} ({int(pct*100)}%): <b>{net_after_tax * pct:,.2f} ₽</b>"
        for name, pct in _SHARES
    )
    return (
        f"{'─' * 30}\n"
        f"💰 <b>К получению</b> ({label}): <b>{s['net']:,.2f} ₽</b>\n"
        f"📋 НПД 4% (самозанятость): −{tax:,.2f} ₽\n"
        f"💵 После налога: <b>{net_after_tax:,.2f} ₽</b>\n"
        f"{shares}\n"
        f"{'─' * 30}"
    )


def _make_range(days_ago: int) -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    dt_from = (now_utc - timedelta(days=days_ago)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return dt_from.strftime("%Y-%m-%dT%H:%M:%S.000Z"), now_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")


@router.callback_query(F.data == "admin:finance")
async def cb_finance(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    await callback.message.edit_text("⏳ Загружаю данные…")

    now_utc = datetime.now(timezone.utc)
    today_from = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    today_to = now_utc

    def iso(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    last_settlement = await db.get_last_settlement()

    tasks = [
        fetch_payments_summary(iso(today_from), iso(today_to)),
        fetch_payments_summary(iso(now_utc - timedelta(days=7)), iso(today_to)),
        fetch_payments_summary(iso(now_utc - timedelta(days=30)), iso(today_to)),
        fetch_payments_summary("2020-01-01T00:00:00.000Z", iso(today_to)),
    ]
    if last_settlement:
        tasks.append(
            fetch_payments_summary(last_settlement["settled_at"], iso(today_to))
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    def safe(r):
        if isinstance(r, Exception):
            logger.error(f"finance fetch error: {r}")
            return None
        return r

    today_s, week_s, month_s, all_s = [safe(r) for r in results[:4]]
    since_s = safe(results[4]) if last_settlement else None

    now_msk = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")

    lines = [f"💳 <b>Финансы (Prodamus)</b>\n<i>обновлено {now_msk} МСК</i>\n"]

    if today_s:
        lines.append(_fmt_period("📅 <b>За сегодня</b>", today_s))
    if week_s:
        lines.append(_fmt_period("📅 <b>За 7 дней</b>", week_s))
    if month_s:
        lines.append(_fmt_period("📅 <b>За 30 дней</b>", month_s))
    if all_s:
        lines.append(_fmt_period_with_shares("📊 <b>За всё время</b>", all_s))

    if last_settlement:
        settled_at = last_settlement["settled_at"][:10]
        if since_s:
            lines.append(_fmt_period(f"🤝 <b>С расчёта {settled_at}</b>", since_s))
        has_unsettled = since_s and since_s["count"] > 0
        payout_s, payout_label = since_s, f"с {settled_at}"
    else:
        lines.append("🤝 <b>Расчётов ещё не было</b>\n")
        has_unsettled = all_s and all_s["count"] > 0
        payout_s, payout_label = all_s, "всё время"

    payout_block = _fmt_payout_block(payout_s, payout_label)
    if payout_block:
        lines.append(payout_block)

    if any(isinstance(r, Exception) for r in results):
        lines.append("\n⚠️ Часть данных не загрузилась — проверь логи")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=finance_keyboard(has_unsettled=bool(has_unsettled)),
    )


@router.callback_query(F.data == "admin:settle")
async def cb_settle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    await callback.message.edit_text("⏳ Фиксирую расчёт…")

    now_utc = datetime.now(timezone.utc)
    last = await db.get_last_settlement()
    dt_from = last["settled_at"] if last else "2020-01-01T00:00:00.000Z"

    try:
        s = await fetch_payments_summary(dt_from, now_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z"))
    except Exception as e:
        logger.error(f"settle fetch error: {e}")
        await callback.message.edit_text(
            "❌ Не удалось получить данные из БД.", reply_markup=finance_keyboard(False)
        )
        return

    await db.add_settlement(
        gross=s["gross"], fee=s["fee"], net=s["net"], count=s["count"]
    )

    tax = s["gross"] * _NPD_RATE
    net_after_tax = s["net"] - tax
    now_msk = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    text = (
        f"✅ <b>Расчёт зафиксирован</b> — {now_msk} МСК\n\n"
        f"Продаж с прошлого расчёта: <b>{s['count']}</b>\n"
        f"Брутто: <b>{s['gross']:,.2f} ₽</b>\n"
        f"Комиссия: −{s['fee']:,.2f} ₽\n"
        f"Получено: <b>{s['net']:,.2f} ₽</b>\n"
        f"НПД 4%: −{tax:,.2f} ₽\n"
        f"После налога: <b>{net_after_tax:,.2f} ₽</b>"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=finance_keyboard(has_unsettled=False)
    )


# --- Долг Мише ---

def _calc_debt(s: dict) -> float:
    """Считает долг Мише: (net - НПД 4%) * 80%"""
    net_after_tax = s["net"] - s["net"] * _NPD_RATE
    return round(net_after_tax * 0.80, 2)


def _fmt_debt_screen(s: dict | None, last_settlement: dict | None) -> str:
    if last_settlement:
        since_label = last_settlement["settled_at"][:10]
        period_text = f"с <b>{since_label}</b>"
    else:
        period_text = "за <b>всё время</b>"

    if not s or s["count"] == 0:
        return (
            f"🤝 <b>Взаиморасчёт</b>\n\n"
            f"Продаж {period_text} не было.\n"
            f"К выплате: <b>0 ₽</b>"
        )

    debt = _calc_debt(s)
    net_after_tax = s["net"] - s["net"] * _NPD_RATE
    my_share = round(net_after_tax * 0.20, 2)

    return (
        f"🤝 <b>Взаиморасчёт</b> ({period_text})\n"
        f"{'─' * 30}\n"
        f"Продаж: <b>{s['count']}</b>  |  Брутто: <b>{s['gross']:,.2f} ₽</b>\n"
        f"Комиссия: −{s['fee']:,.2f} ₽\n"
        f"НПД 4%: −{s['net'] * _NPD_RATE:,.2f} ₽\n"
        f"{'─' * 30}\n"
        f"Чистыми: <b>{net_after_tax:,.2f} ₽</b>\n\n"
        f"👤 Миша (80%): <b>{debt:,.2f} ₽</b>  ← к выплате\n"
        f"👤 Даня (20%): <b>{my_share:,.2f} ₽</b>"
    )


@router.callback_query(F.data == "admin:debt")
async def cb_debt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()

    last_settlement = await db.get_last_settlement()
    dt_from = last_settlement["settled_at"] if last_settlement else "2020-01-01T00:00:00.000Z"
    now_utc = datetime.now(timezone.utc)
    dt_to = now_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")

    try:
        s = await fetch_payments_summary(dt_from, dt_to)
    except Exception as e:
        logger.error(f"debt fetch error: {e}")
        s = None

    text = _fmt_debt_screen(s, last_settlement)
    has_debt = bool(s and s["count"] > 0)

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=debt_keyboard(has_debt=has_debt)
    )


@router.callback_query(F.data == "admin:settle_debt")
async def cb_settle_debt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.answer()
    await callback.message.edit_text("⏳ Фиксирую расчёт…")

    last = await db.get_last_settlement()
    dt_from = last["settled_at"] if last else "2020-01-01T00:00:00.000Z"
    now_utc = datetime.now(timezone.utc)
    dt_to = now_utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")

    try:
        s = await fetch_payments_summary(dt_from, dt_to)
    except Exception as e:
        logger.error(f"settle_debt error: {e}")
        await callback.message.edit_text(
            "❌ Не удалось получить данные.", reply_markup=debt_keyboard(False)
        )
        return

    await db.add_settlement(gross=s["gross"], fee=s["fee"], net=s["net"], count=s["count"])

    debt = _calc_debt(s)
    now_msk = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    text = (
        f"✅ <b>Расчёт с Мишей зафиксирован</b> — {now_msk} МСК\n\n"
        f"Продаж: <b>{s['count']}</b>  |  Брутто: <b>{s['gross']:,.2f} ₽</b>\n"
        f"Переведено Мише: <b>{debt:,.2f} ₽</b>"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=debt_keyboard(has_debt=False)
    )


# --- Воронки лид-магнита ---

def _funnel_card_text(funnel: dict, steps: list[dict], stats: dict) -> str:
    from keyboards.admin import _delay_label
    product_info = f"Товар #{funnel['product_id']}" if funnel.get("product_id") else "не задан"
    steps_text = "\n".join(
        f"  Шаг {s['step'] + 1} | {_delay_label(s['delay_seconds'])}: "
        f"{s['text'][:50]}{'…' if len(s['text']) > 50 else ''}"
        for s in steps
    ) or "  Шагов нет"
    conv = stats.get("conversion_rate", 0)
    return (
        f"🔀 <b>{funnel['name']}</b>\n\n"
        f"📌 Товар для проверки: {product_info}\n"
        f"👥 Всего в воронке: <b>{stats['total']}</b>  |  🔄 Активных: <b>{stats['active']}</b>\n"
        f"✅ Купили: <b>{stats['converted']}</b>  |  📈 Конверсия: <b>{conv}%</b>\n\n"
        f"<b>Шаги:</b>\n{steps_text}\n\n"
        f"<b>Кнопка запуска (callback_data):</b>\n"
        f"<code>funnel:start:{funnel['id']}</code>\n\n"
        f"Нажми «📋 Скопировать кнопку» чтобы получить рабочие deep link'и."
    )


@router.callback_query(F.data == "admin:funnels")
async def cb_funnels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    funnels = await db.get_all_funnels()
    text = "🔀 <b>Воронки лид-магнита</b>\n\nВыбери воронку для настройки или создай новую."
    if not funnels:
        text += "\n\n<i>Воронок ещё нет.</i>"
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=funnels_keyboard(funnels)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:funnel:"))
async def cb_funnel_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    funnel_id = callback.data.split(":", 2)[2]
    funnel = await db.get_funnel(funnel_id)
    if not funnel:
        await callback.answer("Не найдено.", show_alert=True)
        return
    steps = await db.get_funnel_steps(funnel_id)
    analytics = await db.get_funnel_analytics(funnel_id)
    # Совместимость с _funnel_card_text (ожидает поля total/active/converted/conversion_rate)
    stats = {
        "total": analytics["total_enrolled"],
        "active": analytics["active"],
        "converted": analytics["converted"],
        "conversion_rate": analytics["conversion_rate"],
    }
    await callback.message.edit_text(
        _funnel_card_text(funnel, steps, stats),
        parse_mode="HTML",
        reply_markup=funnel_keyboard(funnel, steps),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:funnel_create")
async def cb_funnel_create(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(CreateFunnel.waiting_name)
    await callback.message.edit_text(
        "Введи название воронки (например: <code>Разбор профиля</code>):",
        parse_mode="HTML", reply_markup=admin_back_keyboard()
    )
    await callback.answer()


@router.message(CreateFunnel.waiting_name)
async def fsm_create_funnel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    name = message.text.strip()
    funnel_id = name.lower().replace(" ", "_")[:32]
    await db.create_funnel(funnel_id, name)
    await state.clear()
    funnel = await db.get_funnel(funnel_id)
    await message.answer(
        f"✅ Воронка <b>{name}</b> создана!\n\n"
        f"Теперь добавь шаги — нажми «Добавить шаг».",
        parse_mode="HTML",
        reply_markup=funnel_keyboard(funnel, []),
    )


@router.callback_query(F.data.startswith("admin:funnel_add_step:"))
async def cb_funnel_add_step(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    funnel_id = callback.data.split(":", 2)[2]
    await state.set_state(FunnelAddStep.waiting_delay)
    await state.update_data(funnel_id=funnel_id)
    await callback.message.edit_text(
        "⏱ Через сколько секунд отправить это сообщение?\n\n"
        "Примеры:\n"
        "• <code>0</code> — сразу\n"
        "• <code>60</code> — через 1 минуту\n"
        "• <code>86400</code> — через 24 часа",
        parse_mode="HTML", reply_markup=admin_back_keyboard()
    )
    await callback.answer()


@router.message(FunnelAddStep.waiting_delay)
async def fsm_funnel_step_delay(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        delay = int(message.text.strip())
        if delay < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое число ≥ 0.")
        return
    await state.update_data(delay=delay)
    await state.set_state(FunnelAddStep.waiting_text)
    await message.answer(
        "✍️ Теперь введи текст сообщения (поддерживается HTML):",
        reply_markup=admin_back_keyboard()
    )


@router.message(FunnelAddStep.waiting_text, F.text)
async def fsm_funnel_step_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    funnel_id = data["funnel_id"]
    delay = data["delay"]
    steps = await db.get_funnel_steps(funnel_id)
    next_step = len(steps)
    await db.upsert_funnel_step(funnel_id, next_step, delay, message.text)
    await state.clear()
    funnel = await db.get_funnel(funnel_id)
    steps = await db.get_funnel_steps(funnel_id)
    stats = await db.get_funnel_stats(funnel_id)
    from keyboards.admin import _delay_label
    await message.answer(
        f"✅ Шаг {next_step + 1} добавлен ({_delay_label(delay)}).",
        reply_markup=funnel_keyboard(funnel, steps),
    )


@router.callback_query(F.data.startswith("admin:funnel_edit_step:"))
async def cb_funnel_edit_step(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    _, _, funnel_id, step_str = callback.data.split(":", 3)
    step = int(step_str)
    await state.set_state(FunnelEditStep.waiting_delay)
    await state.update_data(funnel_id=funnel_id, step=step)
    steps = await db.get_funnel_steps(funnel_id)
    current = next((s for s in steps if s["step"] == step), None)
    current_info = f"\nТекущая задержка: {current['delay_seconds']} сек\nТекущий текст: {current['text'][:100]}" if current else ""
    await callback.message.edit_text(
        f"✏️ Редактирование шага {step + 1}{current_info}\n\n"
        "Введи новую задержку в секундах (или <code>-</code> чтобы оставить прежнюю):",
        parse_mode="HTML", reply_markup=admin_back_keyboard()
    )
    await callback.answer()


@router.message(FunnelEditStep.waiting_delay)
async def fsm_edit_step_delay(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    if message.text.strip() == "-":
        steps = await db.get_funnel_steps(data["funnel_id"])
        current = next((s for s in steps if s["step"] == data["step"]), None)
        delay = current["delay_seconds"] if current else 0
    else:
        try:
            delay = int(message.text.strip())
        except ValueError:
            await message.answer("Введи число или <code>-</code>.", parse_mode="HTML")
            return
    await state.update_data(delay=delay)
    await state.set_state(FunnelEditStep.waiting_text)
    await message.answer("✍️ Введи новый текст сообщения:", reply_markup=admin_back_keyboard())


@router.message(FunnelEditStep.waiting_text, F.text)
async def fsm_edit_step_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    await db.upsert_funnel_step(data["funnel_id"], data["step"], data["delay"], message.text)
    await state.clear()
    funnel = await db.get_funnel(data["funnel_id"])
    steps = await db.get_funnel_steps(data["funnel_id"])
    stats = await db.get_funnel_stats(data["funnel_id"])
    await message.answer(
        "✅ Шаг обновлён.",
        reply_markup=funnel_keyboard(funnel, steps),
    )


@router.callback_query(F.data.startswith("admin:funnel_set_product:"))
async def cb_funnel_set_product(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    funnel_id = callback.data.split(":", 2)[2]
    await state.set_state(FunnelSetProduct.waiting_product_id)
    await state.update_data(funnel_id=funnel_id)
    products = await db.get_all_products(active_only=False)
    products_text = "\n".join(f"  <code>{p['id']}</code> — {p['name']}" for p in products)
    await callback.message.edit_text(
        f"Введи ID товара, покупка которого отменяет воронку:\n\n{products_text}\n\n"
        "Отправь <code>0</code> чтобы убрать привязку.",
        parse_mode="HTML", reply_markup=admin_back_keyboard()
    )
    await callback.answer()


@router.message(FunnelSetProduct.waiting_product_id)
async def fsm_funnel_set_product(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    try:
        pid = int(message.text.strip())
    except ValueError:
        await message.answer("Введи число.")
        return
    await db.update_funnel_product(data["funnel_id"], pid if pid > 0 else None)
    await state.clear()
    funnel = await db.get_funnel(data["funnel_id"])
    steps = await db.get_funnel_steps(data["funnel_id"])
    stats = await db.get_funnel_stats(data["funnel_id"])
    await message.answer(
        f"✅ Товар {'привязан' if pid > 0 else 'отвязан'}.",
        reply_markup=funnel_keyboard(funnel, steps),
    )


@router.callback_query(F.data.startswith("admin:funnel_copy_btn:"))
async def cb_funnel_copy_btn(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    funnel_id = callback.data.split(":", 2)[2]
    await callback.answer()

    funnel = await db.get_funnel(funnel_id)
    slug = funnel.get("slug") if funnel else None

    bot_info = await callback.bot.get_me()
    bot_username = bot_info.username

    if not slug:
        await callback.message.answer(
            "⚠️ У этой воронки нет slug. Перезапусти бота или пересоздай воронку.",
            parse_mode="HTML",
        )
        return

    await callback.message.answer(
        f"<b>Deep link (автозапуск воронки):</b>\n"
        f"<code>https://t.me/{bot_username}?start=lm_{slug}</code>\n\n"
        f"<b>С источником (для Stories / Reels / поста):</b>\n"
        f"<code>https://t.me/{bot_username}?start=lm_{slug}_src_reels</code>\n"
        f"<code>https://t.me/{bot_username}?start=lm_{slug}_src_stories</code>\n"
        f"<code>https://t.me/{bot_username}?start=lm_{slug}_src_bio</code>\n\n"
        f"<b>Кнопка в боте (callback_data):</b>\n"
        f"<code>funnel:start:{funnel_id}</code>\n\n"
        f"💡 Замени <code>reels</code>/<code>stories</code>/<code>bio</code> на любое название — "
        f"оно появится в аналитике источников.",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("admin:funnel_analytics:"))
async def cb_funnel_analytics(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    funnel_id = callback.data.split(":", 2)[2]
    await callback.answer()

    funnel = await db.get_funnel(funnel_id)
    if not funnel:
        await callback.answer("Не найдено.", show_alert=True)
        return

    analytics = await db.get_funnel_analytics(funnel_id)

    lines = [
        f"📊 <b>Аналитика: {funnel['name']}</b>\n",
        f"👥 Вошли в воронку: <b>{analytics['total_enrolled']}</b>",
        f"🔄 Сейчас активны: <b>{analytics['active']}</b>",
        f"✅ Купили: <b>{analytics['converted']}</b>",
        f"💤 Прошли всю воронку без покупки: <b>{analytics['dropped']}</b>",
        f"📈 Конверсия (ЛМ → покупка): <b>{analytics['conversion_rate']}%</b>",
    ]

    if analytics["by_source"]:
        lines.append("\n<b>По источникам:</b>")
        for s in analytics["by_source"]:
            bar_filled = int(s["conversion_rate"] / 5)  # каждые 5% = 1 блок
            bar = "█" * bar_filled + "░" * (20 - bar_filled)
            lines.append(
                f"\n🔗 <b>{s['source']}</b>\n"
                f"  Вошли: {s['enrolled']}  |  Купили: {s['converted']}  |  "
                f"Конв: <b>{s['conversion_rate']}%</b>\n"
                f"  <code>{bar}</code>"
            )
    else:
        lines.append("\n<i>Данных по источникам пока нет.</i>")

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:funnel:{funnel_id}")]
    ])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
