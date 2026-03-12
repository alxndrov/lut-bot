from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Товары", callback_data="admin:products")],
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin:add_product")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
    ])


def admin_products_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for p in products:
        status = "✅" if p["active"] else "❌"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {p['name']} — {p['price']} ₽",
            callback_data=f"admin:product:{p['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_product_keyboard(product: dict) -> InlineKeyboardMarkup:
    pid = product["id"]
    toggle_text = "❌ Скрыть" if product["active"] else "✅ Показать"
    buttons = [
        [InlineKeyboardButton(text="📎 Загрузить файл", callback_data=f"admin:upload_file:{pid}")],
        [InlineKeyboardButton(text="🖼 Загрузить фото", callback_data=f"admin:upload_photo:{pid}")],
        [InlineKeyboardButton(text=toggle_text, callback_data=f"admin:toggle:{pid}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin:delete:{pid}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:products")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В меню", callback_data="admin:menu")],
    ])


def confirm_delete_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin:confirm_delete:{product_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:product:{product_id}"),
        ]
    ])
