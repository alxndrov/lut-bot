from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, Message,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
import json
import logging
import math
import re
from urllib.parse import quote

import config
import database as db
from services.prodamus import build_payment_url
from services.geocode import geocode
from keyboards.user import back_to_catalog_keyboard

router = Router()
logger = logging.getLogger(__name__)

CDEK_CLIENT = None
if config.CDEK_ACCOUNT and config.CDEK_SECURE_PASSWORD:
    from services.cdek import CDEKClient
    CDEK_CLIENT = CDEKClient(
        client_id=config.CDEK_ACCOUNT,
        client_secret=config.CDEK_SECURE_PASSWORD,
        from_city=config.CDEK_FROM_CITY,
        test_mode=config.CDEK_TEST_MODE,
    )


PVZ_PAGE_SIZE = 5
# Сколько пунктов оставляем клиенту на выбор после сортировки по расстоянию
PVZ_KEEP = 30


class DeliveryOrder(StatesGroup):
    waiting_city = State()
    waiting_geo = State()
    waiting_pvz_choice = State()
    waiting_name = State()
    waiting_phone = State()


def _normalize_phone(raw: str) -> str | None:
    """Приводит телефон к виду +7XXXXXXXXXX. None — если не похоже на номер."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return "+" + digits


def _distance_km(lat1, lon1, lat2, lon2) -> float | None:
    """Расстояние по прямой между двумя точками, км."""
    try:
        r = 6371.0
        p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
        dp = math.radians(float(lat2) - float(lat1))
        dl = math.radians(float(lon2) - float(lon1))
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    except (TypeError, ValueError):
        return None


def _yandex_maps_url(pvz: dict) -> str:
    """Ссылка на пункт СДЭК в Яндекс.Картах.

    Ищем организацию по названию и адресу с центром на координатах пункта —
    так открывается карточка самого СДЭК (часы, телефон, фото), а не просто
    точка на доме. Если адреса нет, откатываемся к карточке места.
    """
    lat, lon = pvz.get("lat"), pvz.get("lon")
    point = f"{lon},{lat}"
    addr = pvz.get("address_short") or ""
    city = pvz.get("city") or ""

    if addr:
        query = " ".join(x for x in ("СДЭК", city, addr) if x)
        return (f"https://yandex.ru/maps/?ll={point}&z=17"
                f"&text={quote(query)}")

    return (f"https://yandex.ru/maps/?ll={point}&z=18&mode=whatshere"
            f"&whatshere%5Bpoint%5D={point}&whatshere%5Bzoom%5D=18")


def _with_fee(amount: int) -> int:
    """Добавляет к стоимости доставки комиссию Prodamus, округляя вверх до рубля.

    Считаем «в обратную сторону»: чтобы после удержания комиссии на руках
    осталась ровно стоимость СДЭК, клиент должен заплатить amount/(1-ставка).
    Клиенту про комиссию не сообщаем — он видит просто цену доставки.
    """
    fee = config.PRODAMUS_FEE_PERCENT
    if amount <= 0 or fee <= 0 or fee >= 100:
        return amount
    return math.ceil(amount / (1 - fee / 100))


def _fmt_distance(km: float | None) -> str:
    if km is None:
        return ""
    if km < 1:
        return f"{int(km * 1000)} м"
    if km < 10:
        return f"{km:.1f} км"
    return f"{int(km)} км"


SKIP_GEO_TEXT = "Показать список без геолокации"


def geo_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
            [KeyboardButton(text=SKIP_GEO_TEXT)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def _apply_point(state: FSMContext, lat: float, lon: float) -> int:
    """Сортирует пункты по расстоянию от точки и оставляет ближайшие.

    Полный список пунктов города держим нетронутым в pvz_all — иначе
    повторный поиск по другому адресу сортировал бы только прошлую выборку.
    """
    data = await state.get_data()
    points = list(data.get("pvz_all", []))
    for p in points:
        p["distance"] = _distance_km(lat, lon, p.get("lat"), p.get("lon"))
    points.sort(key=lambda p: (p["distance"] is None, p["distance"] or 0))
    view = points[:PVZ_KEEP]
    await state.update_data(pvz_view=view)
    return len(view)


async def start_delivery_flow(target: Message, state: FSMContext, product_id: int,
                              quantity: int = 1, rounds: list | None = None) -> bool:
    """Запускает расчёт доставки СДЭК после заполнения опроса.

    Возвращает False, если СДЭК не настроен — тогда вызывающий код
    продолжает по старому сценарию (адрес свободным текстом).
    """
    if not CDEK_CLIENT:
        return False
    await state.clear()
    await state.set_state(DeliveryOrder.waiting_city)
    await state.update_data(
        product_id=product_id,
        quantity=max(1, quantity),
        rounds=rounds or [],
    )
    await target.answer(
        "🚚 Осталось оформить доставку СДЭК.\n\n"
        "Введи свой <b>город</b> — рассчитаю стоимость:",
        parse_mode="HTML",
    )
    return True


@router.callback_query(F.data.startswith("buy_physical:"))
async def cb_buy_physical(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])
    product = await db.get_product(product_id)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    await state.set_state(DeliveryOrder.waiting_city)
    await state.update_data(product_id=product_id)
    await callback.message.answer(
        f"📦 <b>{product['name']}</b>\n\n"
        "Введи свой <b>город</b> для расчёта доставки:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(DeliveryOrder.waiting_city)
async def fsm_delivery_city(message: Message, state: FSMContext):
    city = message.text.strip()
    await state.update_data(city=city)

    if CDEK_CLIENT:
        await message.answer("⏳ Определяю город...")
        city_code = await CDEK_CLIENT.get_city_code(city)
        if not city_code:
            await message.answer(
                "❌ Город не найден в СДЭК. Попробуй иначе, например: "
                "<b>Москва</b>, <b>Санкт-Петербург</b>, <b>Казань</b>.",
                parse_mode="HTML",
            )
            return
        await state.update_data(cdek_city_code=city_code)

    await _offer_pvz(message, state)


async def _offer_pvz(message: Message, state: FSMContext):
    """Загружает пункты выдачи города и предлагает найти ближайший."""
    data = await state.get_data()

    points = []
    if CDEK_CLIENT and data.get("cdek_city_code"):
        await message.answer("⏳ Загружаю пункты выдачи...")
        points = await CDEK_CLIENT.get_pvz(data["cdek_city_code"])

    if not points:
        # Без пунктов выдачи заказ не оформить — СДЭК везёт только до ПВЗ
        await state.set_state(DeliveryOrder.waiting_city)
        await message.answer(
            "❌ В этом городе нет пунктов выдачи СДЭК — доставить туда не получится.\n\n"
            "Попробуй ближайший крупный город: напиши его название.",
        )
        return

    await state.update_data(pvz_all=points, pvz_total=len(points))
    await state.set_state(DeliveryOrder.waiting_geo)
    await message.answer(
        f"📦 В городе <b>{data.get('city', '')}</b> найдено пунктов выдачи СДЭК: "
        f"<b>{len(points)}</b>.\n\n"
        "Чтобы показать ближайшие, отправь геолокацию кнопкой ниже "
        "или просто напиши улицу и дом — например, <i>Баумана 5</i>.",
        parse_mode="HTML",
        reply_markup=geo_request_keyboard(),
    )


@router.message(DeliveryOrder.waiting_geo, F.location)
async def fsm_pvz_geo(message: Message, state: FSMContext):
    """Пришла геолокация — сортируем пункты по расстоянию."""
    await _apply_point(state, message.location.latitude, message.location.longitude)
    await message.answer("📍 Нашёл ближайшие к тебе:", reply_markup=ReplyKeyboardRemove())
    await _show_pvz_page(message, state, page=0)


@router.message(DeliveryOrder.waiting_geo)
async def fsm_pvz_geo_text(message: Message, state: FSMContext):
    """Вместо геолокации — введённый адрес либо отказ от поиска."""
    text = (message.text or "").strip()
    data = await state.get_data()

    if not text or text == SKIP_GEO_TEXT:
        await state.update_data(pvz_view=data.get("pvz_all", [])[:PVZ_KEEP])
        await message.answer("Хорошо, вот список:", reply_markup=ReplyKeyboardRemove())
        await _show_pvz_page(message, state, page=0)
        return

    await message.answer("⏳ Ищу адрес...", reply_markup=ReplyKeyboardRemove())
    point = await geocode(text, data.get("city", ""))
    if not point:
        await state.update_data(pvz_view=data.get("pvz_all", [])[:PVZ_KEEP])
        await message.answer(
            "❌ Не нашёл такой адрес. Попробуй в формате <i>улица дом</i> — "
            "например, <i>Баумана 5</i>. А пока вот список пунктов:",
            parse_mode="HTML",
        )
        await _show_pvz_page(message, state, page=0)
        return

    await _apply_point(state, *point)
    await message.answer(f"📍 Ближайшие к адресу «{text}»:")
    await _show_pvz_page(message, state, page=0)


async def _show_pvz_page(message: Message, state: FSMContext, page: int = 0):
    data = await state.get_data()
    points = data.get("pvz_view") or data.get("pvz_all", [])[:PVZ_KEEP]
    total = len(points)
    start = page * PVZ_PAGE_SIZE
    chunk = points[start:start + PVZ_PAGE_SIZE]

    if not chunk:
        # Пролистали до конца — возвращаем к началу списка
        if page > 0:
            await _show_pvz_page(message, state, page=0)
        return

    lines = []
    for i, p in enumerate(chunk, start + 1):
        dist = p.get("distance")
        dist_str = f" — {_fmt_distance(dist)} от тебя" if dist is not None else ""
        work = f"\n    🕐 {p['work_time']}" if p.get("work_time") else ""
        lines.append(f"<b>{i}.</b> 📍 {p['address']}{dist_str}{work}")

    rows = [[
        InlineKeyboardButton(text=str(i), callback_data=f"delivery:pvz_pick:{i - 1}")
        for i in range(start + 1, start + len(chunk) + 1)
    ]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"delivery:pvz_page:{page - 1}"))
    if start + PVZ_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Ещё ▶️", callback_data=f"delivery:pvz_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="catalog")])

    city_total = data.get("pvz_total", total)
    shown = f"{start + 1}–{start + len(chunk)} из {total}"
    if city_total > total:
        shown += f", ближайшие из {city_total} в городе"

    await state.update_data(pvz_page=page)
    await state.set_state(DeliveryOrder.waiting_pvz_choice)
    await message.answer(
        f"📦 <b>Пункты выдачи СДЭК</b> ({shown})\n\n"
        + "\n\n".join(lines)
        + "\n\nВыбери номер кнопкой ниже. Не подходит — напиши другую улицу и дом, "
          "поищу рядом с ними:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("delivery:pvz_page:"), DeliveryOrder.waiting_pvz_choice)
async def cb_pvz_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[2])
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _show_pvz_page(callback.message, state, page=page)


@router.callback_query(F.data.startswith("delivery:pvz_pick:"), DeliveryOrder.waiting_pvz_choice)
async def cb_pvz_pick(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    points = data.get("pvz_view") or data.get("pvz_all", [])[:PVZ_KEEP]
    idx = int(callback.data.split(":")[2])
    if not 0 <= idx < len(points):
        await callback.answer("Пункт не найден, напиши адрес текстом.", show_alert=True)
        return

    pvz = points[idx]
    await callback.answer()
    await state.update_data(
        address=pvz["address"],
        pvz_code=pvz.get("code", ""),
        full_address_override=pvz["address"],
    )

    # Карточка с картой — клиент видит, где именно находится пункт.
    # Кнопкой уводим в Яндекс.Карты: встроенная карточка Telegram открывается
    # в картах системы (на Android это Google), а нам нужны Яндекс.
    if pvz.get("lat") and pvz.get("lon"):
        try:
            await callback.message.answer("✅ Записал, заказ приедет сюда:")
            await callback.message.answer_venue(
                latitude=float(pvz["lat"]),
                longitude=float(pvz["lon"]),
                title=f"СДЭК {pvz.get('code', '')}".strip(),
                address=pvz["address"],
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🗺 Открыть в Яндекс.Картах",
                        url=_yandex_maps_url(pvz),
                    )
                ]]),
            )
        except Exception as e:
            logger.warning(f"CDEK: не удалось отправить точку на карте: {e}")

    await _ask_recipient_name(callback.message, state)


def phone_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить свой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def _ask_recipient_name(message: Message, state: FSMContext):
    """ФИО и телефон обязательны для накладной СДЭК — без них посылку не выдадут."""
    await state.set_state(DeliveryOrder.waiting_name)
    await message.answer(
        "👤 Напиши <b>ФИО получателя</b> полностью — как в паспорте.\n\n"
        "По нему выдадут посылку в пункте выдачи — либо по коду из приложения СДЭК, "
        "если у тебя есть CDEK ID.",
        parse_mode="HTML",
    )


@router.message(DeliveryOrder.waiting_name)
async def fsm_recipient_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 3 or not re.search(r"[А-Яа-яЁёA-Za-z]", name):
        await message.answer("Не похоже на ФИО. Напиши фамилию, имя и отчество.")
        return
    await state.update_data(recipient_name=name)
    await state.set_state(DeliveryOrder.waiting_phone)
    await message.answer(
        "📞 Теперь <b>телефон получателя</b> — на него СДЭК пришлёт СМС, "
        "когда посылка приедет в пункт выдачи.\n\n"
        "Например: <code>+7 900 123-45-67</code>",
        parse_mode="HTML",
        reply_markup=phone_request_keyboard(),
    )


@router.message(DeliveryOrder.waiting_phone, F.contact)
async def fsm_recipient_phone_contact(message: Message, state: FSMContext):
    await _accept_phone(message, state, message.contact.phone_number)


@router.message(DeliveryOrder.waiting_phone)
async def fsm_recipient_phone(message: Message, state: FSMContext):
    await _accept_phone(message, state, message.text or "")


async def _accept_phone(message: Message, state: FSMContext, raw: str):
    phone = _normalize_phone(raw)
    if not phone:
        await message.answer(
            "Не похоже на номер. Напиши в формате <code>+7 900 123-45-67</code> "
            "или нажми кнопку ниже.",
            parse_mode="HTML",
            reply_markup=phone_request_keyboard(),
        )
        return
    await state.update_data(recipient_phone=phone)
    await message.answer(f"Записал: {phone}", reply_markup=ReplyKeyboardRemove())
    await _calculate_and_confirm(message, state, message.from_user.id)


@router.message(DeliveryOrder.waiting_pvz_choice)
async def fsm_pvz_choice(message: Message, state: FSMContext):
    """Клиент пишет адрес прямо на экране со списком — ищем пункты рядом."""
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши улицу и дом — например, <i>Баумана 5</i>.",
                             parse_mode="HTML")
        return

    data = await state.get_data()
    await message.answer("⏳ Ищу адрес...")
    point = await geocode(text, data.get("city", ""))
    if point:
        await _apply_point(state, *point)
        await message.answer(f"📍 Ближайшие к адресу «{text}»:")
        await _show_pvz_page(message, state, page=0)
        return

    # Адрес не распознан. Дальше не пускаем: пункт выдачи должен быть выбран
    # из списка СДЭК — по произвольному тексту накладную не создать.
    data = await state.get_data()
    await message.answer(
        "❌ Не нашёл такой адрес. Попробуй в формате <i>улица дом</i> — "
        "например, <i>Светланская 29</i>.\n\n"
        "<b>Пункт выдачи нужно выбрать из списка кнопкой</b> — "
        "только так я смогу оформить доставку.",
        parse_mode="HTML",
    )
    await _show_pvz_page(message, state, page=data.get("pvz_page", 0))


async def _calculate_and_confirm(message: Message, state: FSMContext, user_id: int):
    """Считает доставку и показывает итог сразу с кнопкой оплаты.

    user_id передаём явно: в ветке с выбором ПВЗ сюда приходит сообщение бота,
    и message.from_user — это бот, а не покупатель.
    """
    data = await state.get_data()
    product = await db.get_product(data["product_id"])

    delivery_cost = 0
    delivery_info = ""
    calc_failed = False
    city = data.get("city", "")
    addr = data.get("address", "не указан")
    # Адрес ПВЗ из СДЭК уже содержит город и индекс — город второй раз не клеим
    full_address = data.get("full_address_override") or (f"{city}, {addr}" if city else addr)

    if CDEK_CLIENT and data.get("cdek_city_code"):
        tariff_code = config.CDEK_TARIFF_PVZ
        pkg = db.product_package(product)
        result = await CDEK_CLIENT.calculate_tariff(
            to_city_code=data["cdek_city_code"],
            tariff_code=tariff_code,
            weight=pkg["weight"],
            length=pkg["length"], width=pkg["width"], height=pkg["height"],
            places=max(1, data.get("quantity", 1)),
        )
        if result:
            delivery_cost = _with_fee(result["cost"])
            logger.info(
                f"CDEK: тариф {result['cost']} ₽ -> к оплате {delivery_cost} ₽ "
                f"(комиссия Prodamus {config.PRODAMUS_FEE_PERCENT}%)"
            )
            days_min, days_max = result.get("days_min"), result.get("days_max")
            if days_min and days_max:
                delivery_info = f"{days_min}–{days_max} дн."
            elif days_max:
                delivery_info = f"до {days_max} дн."
        else:
            # Тариф недоступен по направлению или СДЭК не ответил. Показывать
            # «0 ₽» нельзя — клиент прочитает это как бесплатную доставку.
            calc_failed = True
            logger.warning(
                f"CDEK: расчёт не удался, город={city} код={data.get('cdek_city_code')} "
                f"тариф={tariff_code}"
            )

    delivery_type_str = "СДЭК, пункт выдачи" if CDEK_CLIENT else "Пункт выдачи"

    quantity = max(1, data.get("quantity", 1))
    goods_sum = product["price"] * quantity
    total = goods_sum + delivery_cost

    qty_str = f" ×{quantity}" if quantity > 1 else ""
    if calc_failed:
        delivery_line = (
            f"Доставка ({delivery_type_str}): рассчитаю отдельно и напишу — "
            f"оплачивается при получении"
        )
    else:
        delivery_line = f"Доставка ({delivery_type_str}): {delivery_cost} ₽ {delivery_info}"
    name = data.get("recipient_name", "")
    phone = data.get("recipient_phone", "")
    recipient_line = f"Получатель: {name}, {phone}\n" if name else ""

    text = (
        f"📋 <b>Подтверждение заказа</b>\n\n"
        f"Товар: {product['name']}{qty_str} — {goods_sum} ₽\n"
        f"{delivery_line}\n"
        f"Адрес: {full_address}\n"
        f"{recipient_line}\n"
        f"<b>Итого: {total} ₽</b>\n\n"
        "После оплаты я свяжусь с тобой для уточнения деталей доставки."
    )

    # Адрес и ответы опроса — в БД до оплаты; вебхук заберёт их после
    delivery_str = f"{delivery_type_str}: {full_address}"
    pvz_code = data.get("pvz_code")
    if pvz_code:
        delivery_str += f" (код ПВЗ: {pvz_code})"
    if name:
        delivery_str += f"\nПолучатель: {name}, {phone}"
    rounds = data.get("rounds") or []
    await db.save_pending_delivery(
        user_id, product["id"], delivery_str,
        survey_json=json.dumps(rounds, ensure_ascii=False) if rounds else None,
        amount=total,
        recipient_name=name or None,
        recipient_phone=phone or None,
        pvz_code=pvz_code or None,
        delivery_amount=delivery_cost,
    )

    if not config.PRODAMUS_SHOP_URL_PHYSICAL:
        await state.clear()
        await message.answer(
            text + "\n\n⚠️ Оплата временно недоступна — напиши мне в личку.",
            parse_mode="HTML",
        )
        return

    # Физические товары — отдельный магазин Prodamus. Ссылка сразу в кнопке
    # подтверждения, чтобы не заставлять клиента жать «Оплатить» дважды.
    product_name = product["name"] if quantity == 1 else f"{product['name']} ×{quantity}"
    url = build_payment_url(
        shop_url=config.PRODAMUS_SHOP_URL_PHYSICAL,
        product_name=product_name,
        price=total,
        user_id=user_id,
        product_id=product["id"],
        order_type="p",
        secret=config.PRODAMUS_SECRET_PHYSICAL,
        notification_url=config.PRODAMUS_WEBHOOK_URL_PHYSICAL,
    )

    await state.clear()
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {total} ₽", url=url)],
        ]),
    )
