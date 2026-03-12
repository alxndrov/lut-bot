from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def catalog_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=f"{p['name']} — {p['price']} ₽",
            callback_data=f"product:{p['id']}"
        )]
        for p in products
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def product_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить", callback_data=f"buy:{product_id}")],
        [InlineKeyboardButton(text="◀️ Назад к каталогу", callback_data="catalog")],
    ])


def back_to_catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К каталогу", callback_data="catalog")],
    ])
