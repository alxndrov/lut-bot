from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Товары", callback_data="admin:products")],
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin:add_product")],
        [InlineKeyboardButton(text="🖼 Баннер каталога", callback_data="admin:upload_banner")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="💳 Финансы", callback_data="admin:finance")],
        [InlineKeyboardButton(text="🤝 Взаиморасчёт", callback_data="admin:debt")],
        [InlineKeyboardButton(text="🔀 Воронки", callback_data="admin:funnels")],
    ])


def funnels_keyboard(funnels: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=f"{'✅' if f['active'] else '⏸'} {f['name']}",
            callback_data=f"admin:funnel:{f['id']}",
        )]
        for f in funnels
    ]
    buttons.append([InlineKeyboardButton(text="➕ Создать воронку", callback_data="admin:funnel_create")])
    buttons.append([InlineKeyboardButton(text="◀️ В меню", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def funnel_keyboard(funnel: dict, steps: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for s in steps:
        delay_label = _delay_label(s["delay_seconds"])
        buttons.append([InlineKeyboardButton(
            text=f"✏️ Шаг {s['step'] + 1} ({delay_label})",
            callback_data=f"admin:funnel_edit_step:{funnel['id']}:{s['step']}",
        )])
    buttons.append([InlineKeyboardButton(
        text="➕ Добавить шаг", callback_data=f"admin:funnel_add_step:{funnel['id']}"
    )])
    buttons.append([InlineKeyboardButton(
        text="🔗 Товар для проверки покупки", callback_data=f"admin:funnel_set_product:{funnel['id']}"
    )])
    buttons.append([InlineKeyboardButton(
        text="📋 Скопировать кнопку", callback_data=f"admin:funnel_copy_btn:{funnel['id']}"
    )])
    buttons.append([InlineKeyboardButton(
        text="📊 Аналитика источников", callback_data=f"admin:funnel_analytics:{funnel['id']}"
    )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:funnels")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _bonus_button_label(product: dict) -> str:
    limit = product.get("bonus_limit")
    if limit:
        return f"🎁 Бонус-разбор: первые {limit} чел."
    return "🎁 Бонус-разбор для первых N покупателей"


def _delay_label(seconds: int) -> str:
    if seconds == 0:
        return "сразу"
    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч"
    return f"{seconds // 86400} д"


def finance_keyboard(has_unsettled: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    if has_unsettled:
        buttons.append([InlineKeyboardButton(text="✅ Мы в расчёте", callback_data="admin:settle")])
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:finance")])
    buttons.append([InlineKeyboardButton(text="◀️ В меню", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def debt_keyboard(has_debt: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    if has_debt:
        buttons.append([InlineKeyboardButton(text="✅ Расчёт произведён", callback_data="admin:settle_debt")])
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:debt")])
    buttons.append([InlineKeyboardButton(text="◀️ В меню", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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


def admin_product_keyboard(product: dict, purchase_count: int = 0) -> InlineKeyboardMarkup:
    pid = product["id"]
    category = product.get("category", "digital")

    # --- Категория ---
    cat_icons = {"physical": "🚚", "waitlist": "📋", "infobiz": "📚", "digital": "📦"}
    cat_names = {"physical": "Физический", "waitlist": "Ожидание",
                 "infobiz": "Инфобиз", "digital": "Цифровой"}
    category_text = f"{cat_icons.get(category, '📦')} {cat_names.get(category, 'Цифровой')}"

    # --- Название (показываем текущее, обрезаем) ---
    name = product.get("name", "")
    name_label = f"✏️ {name[:18]}…" if len(name) > 18 else f"✏️ {name}"

    # --- Файл ---
    if product.get("file_name"):
        file_label = f"📎 Файл: {product['file_name']}"
    else:
        file_label = "📎 Файл: не загружен ⚠️"

    # --- Фото ---
    photo_label = "🖼 Фото: загружено ✅" if product.get("photo_id") else "🖼 Фото: нет"

    # --- Инструкция ---
    instr_name = product.get("instruction_file_name") or "не загружена"
    instr_label = f"📄 Инструкция: {instr_name}"

    # --- Видео ---
    video = product.get("video_url")
    video_label = f"🎬 Видео: {video[:30]}…" if video and len(video) > 30 \
        else f"🎬 Видео: {video}" if video else "🎬 Видео: не указано"

    # --- Триггер цены ---
    trigger = product.get("price_trigger")
    after = product.get("price_after_trigger")
    trigger_label = f"🔢 Триггер: {trigger} покупок" if trigger else "🔢 Триггер цены: не задан"
    after_label = f"💲 После: {after} ₽" if after else "💲 Цена после: не задана"

    # --- Счётчик ---
    counter_label = (
        f"{'👁 Счётчик виден' if product.get('counter_visible') else '🙈 Счётчик скрыт'}"
        f"  ({purchase_count} продаж)"
    )

    # --- Канал ---
    channel = product.get("channel_id")
    channel_label = f"📢 Канал: {channel}" if channel else "📢 Канал: не задан ⚠️"

    # --- Бонус ---
    bonus_limit = product.get("bonus_limit")
    if bonus_limit:
        remaining = max(bonus_limit - purchase_count, 0)
        bonus_label = f"🎁 Разбор: первые {bonus_limit} ({remaining} осталось)"
    else:
        bonus_label = "🎁 Разбор для первых N: не задан"

    # --- Активность / публикация ---
    toggle_text = "👁 Скрыть из каталога" if product["active"] else "✅ Показать в каталоге"

    buttons = [
        # Название + описание
        [
            InlineKeyboardButton(text=name_label, callback_data=f"admin:edit_name:{pid}"),
            InlineKeyboardButton(text="✏️ Описание", callback_data=f"admin:edit_desc:{pid}"),
        ],
        # Цена
        [InlineKeyboardButton(
            text=f"💰 Цена: {product['price']} ₽  (нажми чтобы изменить)",
            callback_data=f"admin:edit_price:{pid}",
        )],
        # Категория
        [InlineKeyboardButton(text=category_text, callback_data=f"admin:set_category:{pid}")],
        # Файл
        [InlineKeyboardButton(text=file_label, callback_data=f"admin:upload_file:{pid}")],
        # Фото
        [InlineKeyboardButton(text=photo_label, callback_data=f"admin:upload_photo:{pid}")],
        # Инструкция + видео (цифровой и инфобиз)
        *([
            [InlineKeyboardButton(text=instr_label, callback_data=f"admin:upload_instruction:{pid}")],
            [InlineKeyboardButton(text=video_label, callback_data=f"admin:set_video:{pid}")],
        ] if category in ("digital", "infobiz") else []),
        # Инфобиз-специфичные поля
        *([
            [
                InlineKeyboardButton(text=trigger_label, callback_data=f"admin:set_price_trigger:{pid}"),
                InlineKeyboardButton(text=after_label, callback_data=f"admin:set_price_after:{pid}"),
            ],
            [InlineKeyboardButton(text=counter_label, callback_data=f"admin:toggle_counter:{pid}")],
            [InlineKeyboardButton(text=channel_label, callback_data=f"admin:set_channel:{pid}")],
            [
                InlineKeyboardButton(text=bonus_label, callback_data=f"admin:set_bonus:{pid}"),
                InlineKeyboardButton(text="📋 Список разборов", callback_data=f"admin:bonus_reviews:{pid}"),
            ],
        ] if category == "infobiz" else []),
        # Статус + удаление
        [InlineKeyboardButton(text=toggle_text, callback_data=f"admin:toggle:{pid}")],
        [InlineKeyboardButton(text="🗑 Удалить товар", callback_data=f"admin:delete:{pid}")],
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="admin:products")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def category_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Цифровой", callback_data=f"admin:category:digital:{product_id}")],
        [InlineKeyboardButton(text="🚚 Физический", callback_data=f"admin:category:physical:{product_id}")],
        [InlineKeyboardButton(text="📋 Список ожидания", callback_data=f"admin:category:waitlist:{product_id}")],
        [InlineKeyboardButton(text="📚 Инфобиз", callback_data=f"admin:category:infobiz:{product_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:product:{product_id}")],
    ])


def stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Заработок по месяцам", callback_data="admin:stats_monthly")],
        [InlineKeyboardButton(text="◀️ В меню", callback_data="admin:menu")],
    ])


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
