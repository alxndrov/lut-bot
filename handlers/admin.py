from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from config import ADMIN_IDS
from keyboards.admin import (
    admin_menu_keyboard, admin_products_keyboard,
    admin_product_keyboard, admin_back_keyboard,
    confirm_delete_keyboard,
)
import database as db

router = Router()


# --- FSM States ---

class AddProduct(StatesGroup):
    name = State()
    description = State()
    price = State()


class UploadFile(StatesGroup):
    waiting_file = State()
    product_id = State()


class UploadPhoto(StatesGroup):
    waiting_photo = State()
    product_id = State()


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

@router.callback_query(F.data.startswith("admin:product:"))
async def cb_admin_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    product_id = int(callback.data.split(":")[2])
    product = await db.get_product(product_id)
    if not product:
        await callback.answer("Не найден.", show_alert=True)
        return

    file_info = f"📎 {product['file_name']}" if product.get("file_name") else "📎 Файл не загружен"
    photo_info = "🖼 Фото есть" if product.get("photo_id") else "🖼 Фото нет"
    status = "✅ Активен" if product["active"] else "❌ Скрыт"

    text = (
        f"<b>{product['name']}</b>\n"
        f"Цена: {product['price']} ₽\n"
        f"Статус: {status}\n"
        f"{file_info}\n"
        f"{photo_info}\n\n"
        f"{product['description']}"
    )

    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=admin_product_keyboard(product)
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

    # Обновляем карточку
    file_info = f"📎 {product['file_name']}" if product.get("file_name") else "📎 Файл не загружен"
    photo_info = "🖼 Фото есть" if product.get("photo_id") else "🖼 Фото нет"
    status_text = "✅ Активен" if product["active"] else "❌ Скрыт"

    text = (
        f"<b>{product['name']}</b>\n"
        f"Цена: {product['price']} ₽\n"
        f"Статус: {status_text}\n"
        f"{file_info}\n"
        f"{photo_info}\n\n"
        f"{product['description']}"
    )
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=admin_product_keyboard(product)
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


# --- Статистика ---

@router.callback_query(F.data == "admin:stats")
async def cb_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    stats = await db.get_stats()
    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"Всего заказов: <b>{stats['total_orders']}</b>\n"
        f"Уникальных покупателей: <b>{stats['unique_buyers']}</b>\n"
        f"Выручка: <b>{stats['total_revenue']} ₽</b>"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=admin_back_keyboard()
    )
    await callback.answer()
