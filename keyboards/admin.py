from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import config


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Каталог", callback_data="admin:menu_catalog")],
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📣 Маркетинг", callback_data="admin:menu_marketing")],
        [InlineKeyboardButton(text="💰 Финансы", callback_data="admin:menu_finance")],
    ])


def catalog_submenu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Все товары", callback_data="admin:products")],
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin:add_product")],
        [InlineKeyboardButton(text="🖼 Баннер каталога", callback_data="admin:upload_banner")],
        [InlineKeyboardButton(text="🛒 Незавершённые заказы", callback_data="admin:pending")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")],
    ])


def pending_orders_keyboard(orders: list[dict]) -> InlineKeyboardMarkup:
    """Список незавершённых заказов (опрос заполнен, оплаты нет)."""
    rows = []
    for o in orders[:30]:
        who = f"@{o['username']}" if o.get("username") else (o.get("first_name") or f"id:{o['user_id']}")
        ts = o.get("created_at") or ""            # "YYYY-MM-DD HH:MM:SS"
        day = f"{ts[8:10]}.{ts[5:7]} {ts[11:16]}" if len(ts) >= 16 else ts
        rows.append([InlineKeyboardButton(
            text=f"{day} · {who} · {o.get('product_name') or '—'}",
            callback_data=f"admin:pending_view:{o['user_id']}:{o['product_id']}",
        )])
    rows.append([InlineKeyboardButton(text="🔁 Проверить пропущенные оплаты",
                                      callback_data="admin:missed")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:pending")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu_catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def missed_orders_keyboard(orders: list[dict]) -> InlineKeyboardMarkup:
    """Заказы, по которым оплата могла потеряться (бот был недоступен)."""
    rows = []
    for o in orders[:30]:
        who = f"@{o['username']}" if o.get("username") else (o.get("first_name") or f"id:{o['user_id']}")
        ts = o.get("created_at") or ""
        day = f"{ts[8:10]}.{ts[5:7]} {ts[11:16]}" if len(ts) >= 16 else ts
        amount = o.get("amount") or 0
        sum_str = f" · {amount} ₽" if amount else ""
        rows.append([InlineKeyboardButton(
            text=f"{day} · {who}{sum_str}",
            callback_data=f"admin:pending_view:{o['user_id']}:{o['product_id']}",
        )])
    rows.append([InlineKeyboardButton(text="🔄 Проверить снова", callback_data="admin:missed")])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="admin:pending")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pending_order_keyboard(user_id: int, product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оплата пришла — провести заказ",
                              callback_data=f"admin:force_paid:{user_id}:{product_id}")],
        [InlineKeyboardButton(text="🗑 Убрать из списка",
                              callback_data=f"admin:pending_del:{user_id}:{product_id}")],
        [InlineKeyboardButton(text="◀️ К списку", callback_data="admin:pending")],
    ])


def force_paid_confirm_keyboard(user_id: int, product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, провести заказ",
                              callback_data=f"admin:force_paid_yes:{user_id}:{product_id}")],
        [InlineKeyboardButton(text="◀️ Отмена",
                              callback_data=f"admin:pending_view:{user_id}:{product_id}")],
    ])


def marketing_submenu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔀 Воронки", callback_data="admin:funnels")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")],
    ])


def finance_submenu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Финансы", callback_data="admin:finance")],
        [InlineKeyboardButton(text="🤝 Взаиморасчёт", callback_data="admin:debt")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")],
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
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu_marketing")])
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
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu_finance")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def debt_keyboard(has_debt: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    if has_debt:
        buttons.append([InlineKeyboardButton(text="✅ Расчёт произведён", callback_data="admin:settle_debt")])
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:debt")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu_finance")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_products_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for p in products:
        status = "✅" if p["active"] else "❌"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {p['name']} — {p['price']} ₽",
            callback_data=f"admin:product:{p['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu_catalog")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _section(title: str) -> InlineKeyboardButton:
    """Кнопка-заголовок раздела (ничего не делает при нажатии)."""
    return InlineKeyboardButton(text=title, callback_data="admin:noop")


def _back_to_product(pid: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:product:{pid}")


def admin_product_keyboard(product: dict, purchase_count: int = 0) -> InlineKeyboardMarkup:
    """Главный экран карточки товара — только категории-подменю."""
    pid = product["id"]
    category = product.get("category", "digital")

    # Статусы для превью на кнопках
    file_ok = "✅" if product.get("file_id") else "⚠️"
    photo_ok = "✅" if product.get("photo_id") else "—"
    if product["active"]:
        toggle_icon = "👁"
        toggle_text = "Виден в каталоге"
    else:
        toggle_icon = "🙈"
        toggle_text = "Скрыт от каталога"

    rp = "✅" if product.get("review_push_delay") else "—"

    buttons = [
        [InlineKeyboardButton(text="✏️ Контент", callback_data=f"admin:psub:content:{pid}")],
        [InlineKeyboardButton(text=f"📎 Медиафайлы  {file_ok} файл · {photo_ok} фото",
                              callback_data=f"admin:psub:media:{pid}")],
        *([[InlineKeyboardButton(text="📚 Инфобиз", callback_data=f"admin:psub:infobiz:{pid}")]]
          if category == "infobiz" else []),
        *([[InlineKeyboardButton(text="📋 Опрос", callback_data=f"admin:psub:survey:{pid}")]]
          if category == "physical" else []),
        *([[InlineKeyboardButton(text=_package_label(product),
                                 callback_data=f"admin:pkg:{pid}")]]
          if category == "physical" else []),
        [InlineKeyboardButton(text=f"💌 Маркетинг  {rp} пуш отзыва",
                              callback_data=f"admin:psub:marketing:{pid}")],
        [InlineKeyboardButton(text=f"{toggle_icon} {toggle_text}",
                              callback_data=f"admin:toggle:{pid}"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin:delete:{pid}")],
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="admin:products")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _package_label(product: dict) -> str:
    """Подпись кнопки габаритов: свои значения или общие из .env."""
    if product.get("pkg_weight"):
        w = product["pkg_weight"]
        w_str = f"{w} г" if w < 1000 else f"{w / 1000:g} кг"
        dims = "×".join(str(product.get(k) or "?")
                        for k in ("pkg_length", "pkg_width", "pkg_height"))
        return f"📐 Габариты: {w_str}, {dims} см"
    return "📐 Габариты: по умолчанию"


def package_keyboard(product_id: int, has_own: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📐 Задать габариты",
                                  callback_data=f"admin:pkg_start:{product_id}")]]
    if has_own:
        rows.append([InlineKeyboardButton(text="↩️ Сбросить к общим",
                                          callback_data=f"admin:pkg_reset:{product_id}")])
    rows.append([_back_to_product(product_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_submenu_content(product: dict) -> InlineKeyboardMarkup:
    pid = product["id"]
    name = product.get("name", "")
    name_label = f"✏️ {name[:20]}…" if len(name) > 20 else f"✏️ {name}"
    cat_icons = {"physical": "🚚", "waitlist": "📋", "infobiz": "📚", "digital": "📦"}
    cat_names = {"physical": "Физический", "waitlist": "Ожидание",
                 "infobiz": "Инфобиз", "digital": "Цифровой"}
    cat = product.get("category", "digital")
    cat_text = f"{cat_icons.get(cat, '📦')} {cat_names.get(cat, 'Цифровой')}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name_label, callback_data=f"admin:edit_name:{pid}"),
         InlineKeyboardButton(text="✏️ Описание", callback_data=f"admin:edit_desc:{pid}")],
        [InlineKeyboardButton(text=f"💰 Цена: {product['price']} ₽", callback_data=f"admin:edit_price:{pid}")],
        [InlineKeyboardButton(text=cat_text, callback_data=f"admin:set_category:{pid}")],
        [_back_to_product(pid)],
    ])


def product_submenu_media(product: dict) -> InlineKeyboardMarkup:
    pid = product["id"]
    cat = product.get("category", "digital")
    file_label = f"📎 {product['file_name']}" if product.get("file_name") else "📎 Файл не загружен ⚠️"
    photo_label = "🖼 Фото ✅" if product.get("photo_id") else "🖼 Фото: нет"
    instr_label = f"📄 {product['instruction_file_name']}" if product.get("instruction_file_name") \
        else "📄 Инструкция не загружена"
    video = product.get("video_url")
    video_label = f"🎬 {video[:25]}…" if video and len(video) > 25 else f"🎬 {video}" if video \
        else "🎬 Видео не указано"
    rows = [
        [InlineKeyboardButton(text=file_label, callback_data=f"admin:upload_file:{pid}"),
         InlineKeyboardButton(text=photo_label, callback_data=f"admin:upload_photo:{pid}")],
    ]
    if cat in ("digital", "infobiz"):
        rows.append([
            InlineKeyboardButton(text=instr_label, callback_data=f"admin:upload_instruction:{pid}"),
            InlineKeyboardButton(text=video_label, callback_data=f"admin:set_video:{pid}"),
        ])
    rows.append([_back_to_product(pid)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_submenu_infobiz(product: dict, purchase_count: int = 0) -> InlineKeyboardMarkup:
    pid = product["id"]
    trigger = product.get("price_trigger")
    after = product.get("price_after_trigger")
    trigger_label = f"🔢 Триггер: {trigger} шт." if trigger else "🔢 Триггер: не задан"
    after_label = f"💲 После: {after} ₽" if after else "💲 Цена после: —"
    counter_icon = "👁" if product.get("counter_visible") else "🙈"
    counter_label = f"{counter_icon} Счётчик ({purchase_count} продаж)"
    channel = product.get("channel_id")
    channel_label = f"📢 Канал: {channel}" if channel else "📢 Канал: не задан ⚠️"
    bonus_limit = product.get("bonus_limit")
    if bonus_limit:
        remaining = max(bonus_limit - purchase_count, 0)
        bonus_label = f"🎁 Разбор: первые {bonus_limit} ({remaining} осталось)"
    else:
        bonus_label = "🎁 Бонус-разбор: не задан"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=trigger_label, callback_data=f"admin:set_price_trigger:{pid}"),
         InlineKeyboardButton(text=after_label, callback_data=f"admin:set_price_after:{pid}")],
        [InlineKeyboardButton(text=counter_label, callback_data=f"admin:toggle_counter:{pid}")],
        [InlineKeyboardButton(text=channel_label, callback_data=f"admin:set_channel:{pid}")],
        [InlineKeyboardButton(text=bonus_label, callback_data=f"admin:set_bonus:{pid}"),
         InlineKeyboardButton(text="📋 Разборы", callback_data=f"admin:bonus_reviews:{pid}")],
        [_back_to_product(pid)],
    ])


def product_submenu_marketing(product: dict) -> InlineKeyboardMarkup:
    pid = product["id"]
    rp_delay = product.get("review_push_delay")
    if rp_delay:
        if rp_delay < 3600:
            rp_str = f"{rp_delay // 60} мин"
        elif rp_delay < 86400:
            rp_str = f"{rp_delay // 3600} ч"
        else:
            rp_str = f"{rp_delay // 86400} д"
        review_label = f"⭐ Пуш отзыва: через {rp_str} ✅"
    else:
        review_label = "⭐ Пуш отзыва: не настроен"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=review_label, callback_data=f"admin:set_review_push:{pid}")],
        [_back_to_product(pid)],
    ])


def product_submenu_survey(product: dict, questions: list[dict]) -> InlineKeyboardMarkup:
    """Экран настройки опроса: список вопросов с управлением + добавление."""
    pid = product["id"]
    rows = []
    total = len(questions)
    for i, q in enumerate(questions):
        qid = q["id"]
        short = q["text"] if len(q["text"]) <= 28 else q["text"][:27] + "…"
        pic = "📷 " if q.get("photo_id") else ""
        router = "🎨 " if q.get("is_router") else ""
        # заголовок-вопрос (клик просто обновляет экран) + строка управления
        rows.append([InlineKeyboardButton(
            text=f"{i + 1}. {router}{pic}{short}", callback_data=f"admin:psub:survey:{pid}",
        )])
        controls = [
            InlineKeyboardButton(text="✏️", callback_data=f"admin:survey_edit:{qid}:{pid}"),
            InlineKeyboardButton(text="🎨" if not q.get("is_router") else "🎨✅",
                                 callback_data=f"admin:survey_router:{qid}:{pid}"),
        ]
        if i > 0:
            controls.append(InlineKeyboardButton(text="⬆️", callback_data=f"admin:survey_up:{qid}:{pid}"))
        if i < total - 1:
            controls.append(InlineKeyboardButton(text="⬇️", callback_data=f"admin:survey_down:{qid}:{pid}"))
        controls.append(InlineKeyboardButton(text="🗑", callback_data=f"admin:survey_del:{qid}:{pid}"))
        rows.append(controls)
    rows.append([InlineKeyboardButton(text="➕ Добавить вопрос", callback_data=f"admin:survey_add:{pid}")])
    repeat_on = bool(product.get("survey_repeat_text"))
    repeat_label = "🔁 Доп. позиция: ✅ вкл" if repeat_on else "🔁 Доп. позиция: не задана"
    rows.append([InlineKeyboardButton(text=repeat_label, callback_data=f"admin:survey_repeat:{pid}")])
    delivery_on = bool(product.get("survey_delivery_text"))
    if config.CDEK_ENABLED:
        # Расчёт СДЭК подменяет текстовый блок — показываем, что работает на самом деле
        delivery_label = "🚚 Доставка: СДЭК до ПВЗ, расчёт автоматом"
    else:
        delivery_label = "🚚 Доставка: ✅ вкл" if delivery_on else "🚚 Доставка: не задана"
    rows.append([InlineKeyboardButton(text=delivery_label, callback_data=f"admin:survey_delivery:{pid}")])
    routing_on = bool(product.get("order_routing_text"))
    routing_label = "🖨 Кто печатает: ✅ задано" if routing_on else "🖨 Кто печатает: не задано"
    rows.append([InlineKeyboardButton(text=routing_label, callback_data=f"admin:survey_routing:{pid}")])
    paid_on = bool(product.get("post_payment_text"))
    paid_label = "💬 После оплаты: ✅ своё" if paid_on else "💬 После оплаты: по умолчанию"
    rows.append([InlineKeyboardButton(text=paid_label, callback_data=f"admin:survey_paid:{pid}")])
    rows.append([_back_to_product(pid)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Цифровой", callback_data=f"admin:category:digital:{product_id}")],
        [InlineKeyboardButton(text="🚚 Физический", callback_data=f"admin:category:physical:{product_id}")],
        [InlineKeyboardButton(text="📋 Список ожидания", callback_data=f"admin:category:waitlist:{product_id}")],
        [InlineKeyboardButton(text="📚 Инфобиз", callback_data=f"admin:category:infobiz:{product_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:product:{product_id}")],
    ])


def stats_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:stats")],
    ])


def stats_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🎤 Аналитика заказов", callback_data="admin:order_analytics")],
        [InlineKeyboardButton(text="🚚 Отправка заказов", callback_data="admin:shipping")],
        [InlineKeyboardButton(text="📅 Заработок по месяцам", callback_data="admin:stats_monthly")],
    ]
    if config.GSHEETS_ENABLED:
        rows.append([InlineKeyboardButton(text="📊 Заказы в Google Таблице",
                                          callback_data="admin:gsheet_open")])
    rows += [
        [InlineKeyboardButton(text="📥 Выгрузить в файл", callback_data="admin:stats_export")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gsheet_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Открыть таблицу", url=url)],
        [InlineKeyboardButton(text="🔄 Обновить сейчас", callback_data="admin:gsheet_sync")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:stats")],
    ])


def order_analytics_products_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=p["name"], callback_data=f"admin:oa:{p['id']}")]
            for p in products]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:stats")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shipping_keyboard(mode: str = "unshipped") -> InlineKeyboardMarkup:
    """Экран отправок: переключение между списками."""
    other = ("📦 Показать отправленные", "admin:shipping:shipped") if mode == "unshipped" \
        else ("⏳ Показать неотправленные", "admin:shipping:unshipped")
    rows = [
        [InlineKeyboardButton(text=other[0], callback_data=other[1])],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin:shipping:{mode}")],
    ]
    if config.GSHEETS_ENABLED:
        rows.append([InlineKeyboardButton(text="📊 Обновить Google Таблицу",
                                          callback_data="admin:gsheet_sync")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:stats")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_analytics_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К товарам", callback_data="admin:order_analytics")],
    ])


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ В меню", callback_data="admin:menu")],
    ])


def product_back_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к товару", callback_data=f"admin:product:{product_id}")],
    ])


def finance_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu_finance")],
    ])


def catalog_menu_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu_catalog")],
    ])


def funnel_back_keyboard(funnel_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к воронке", callback_data=f"admin:funnel:{funnel_id}")],
    ])


def confirm_delete_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin:confirm_delete:{product_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:product:{product_id}"),
        ]
    ])
