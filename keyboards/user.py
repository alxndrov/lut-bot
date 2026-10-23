from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import config
from services.prodamus import build_payment_url


def catalog_keyboard(products: list[dict], is_admin: bool = False,
                     reviews: int = 0) -> InlineKeyboardMarkup:
    buttons = []
    for p in products:
        hidden = not p.get("active", 1)
        prefix = "🙈 " if hidden else ""
        buttons.append([InlineKeyboardButton(
            text=f"{prefix}{p['name']} — {p.get('price_label') or p['price']} ₽",
            callback_data=f"product:{p['id']}"
        )])
    if reviews:   # опубликованные админом отзывы — см. handlers/shop_reviews.py
        buttons.append([InlineKeyboardButton(text=f"⭐️ Отзывы покупателей ({reviews})",
                                             callback_data="shoprev:0")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def product_keyboard(product: dict, user_id: int = 0,
                     effective_price: int = 0, is_admin: bool = False) -> InlineKeyboardMarkup:
    pid = product["id"]
    category = product.get("category", "digital")
    price = effective_price or product["price"]

    if category == "physical":
        # Оформление заказа стартует прямо из карточки товара — отдельная
        # кнопка «Оформить заказ» больше не нужна.
        buy_button = None
    elif category == "waitlist":
        buy_button = InlineKeyboardButton(text="📋 Записаться в список", callback_data=f"waitlist:{pid}")
    elif config.PRODAMUS_SHOP_URL and user_id:
        url = build_payment_url(
            shop_url=config.PRODAMUS_SHOP_URL,
            product_name=product["name"],
            price=price,
            user_id=user_id,
            product_id=pid,
            order_type="d",
            secret=config.PRODAMUS_SECRET,
            notification_url=config.PRODAMUS_WEBHOOK_URL,
        )
        buy_button = InlineKeyboardButton(text=f"💳 Российской картой или СБП · {price} ₽", url=url)
    else:
        buy_button = InlineKeyboardButton(text="💳 Купить", callback_data=f"buy:{pid}")

    rows = [[buy_button]] if buy_button else []
    # Зарубежная карта — lava.top Миши, в долларах (email спросит наша страница)
    if (category in ("digital", "infobiz") and user_id and config.LAVA_ENABLED
            and config.PAY_BASE_URL):
        from services.paylinks import link
        rows.append([InlineKeyboardButton(
            text="🌍 Картой зарубежного банка · в $",
            url=link(f"d_{user_id}_{pid}", price, product["name"], via="lava"))])
    if category in ("digital", "infobiz") and config.PRODAMUS_SHOP_URL and user_id:
        rows.append([InlineKeyboardButton(
            text="🔄 Я оплатил — получить товар",
            callback_data=f"check_payment:{pid}",
        )])

    if is_admin:
        rows.append([
            InlineKeyboardButton(text="✅ Симулировать успешную оплату", callback_data=f"sim_success:{pid}"),
        ])
        rows.append([
            InlineKeyboardButton(text="❌ Симулировать неуспешную оплату", callback_data=f"sim_fail:{pid}"),
        ])

    rows.append([InlineKeyboardButton(text="◀️ Назад к каталогу", callback_data="catalog")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К каталогу", callback_data="catalog")],
    ])
