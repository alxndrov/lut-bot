from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, Message,
    InlineKeyboardMarkup, InlineKeyboardButton
)
import logging

import config
import database as db
from services.prodamus import build_payment_url
from keyboards.user import back_to_catalog_keyboard

router = Router()
logger = logging.getLogger(__name__)

YADEL_CLIENT = None
if config.YANDEX_DELIVERY_TOKEN and config.YANDEX_WAREHOUSE_ID:
    from services.yandex_delivery import YandexDeliveryClient
    YADEL_CLIENT = YandexDeliveryClient(
        token=config.YANDEX_DELIVERY_TOKEN,
        warehouse_id=config.YANDEX_WAREHOUSE_ID,
    )


class DeliveryOrder(StatesGroup):
    waiting_city = State()
    waiting_delivery_type = State()
    waiting_address = State()
    waiting_pvz_choice = State()
    confirming = State()


def delivery_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚚 Курьер до двери", callback_data="delivery:courier")],
        [InlineKeyboardButton(text="📦 Пункт выдачи", callback_data="delivery:pvz")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="catalog")],
    ])


def confirm_keyboard(total: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Оплатить {total} ₽", callback_data="delivery:pay")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="catalog")],
    ])


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
        "Введите ваш <b>город</b> для расчёта доставки:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(DeliveryOrder.waiting_city)
async def fsm_delivery_city(message: Message, state: FSMContext):
    city = message.text.strip()
    geo_id = None

    if YADEL_CLIENT:
        await message.answer("⏳ Определяю город...")
        geo_id = await YADEL_CLIENT.get_geo_id(city)
        if not geo_id:
            await message.answer(
                "❌ Город не найден. Попробуйте иначе, например: "
                "<b>Москва</b>, <b>Санкт-Петербург</b>, <b>Казань</b>.",
                parse_mode="HTML",
            )
            return

    await state.update_data(city=city, geo_id=geo_id)
    await state.set_state(DeliveryOrder.waiting_delivery_type)
    await message.answer(
        f"🏙 Город: <b>{city}</b>\n\nВыберите способ доставки:",
        parse_mode="HTML",
        reply_markup=delivery_type_keyboard(),
    )


@router.callback_query(F.data == "delivery:courier", DeliveryOrder.waiting_delivery_type)
async def cb_delivery_courier(callback: CallbackQuery, state: FSMContext):
    await state.update_data(delivery_type="courier", tariff="time_interval")
    await state.set_state(DeliveryOrder.waiting_address)
    await callback.message.answer("Введите адрес доставки (улица, дом, квартира):")
    await callback.answer()


@router.callback_query(F.data == "delivery:pvz", DeliveryOrder.waiting_delivery_type)
async def cb_delivery_pvz(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(delivery_type="pvz", tariff="self_pickup")

    if YADEL_CLIENT and data.get("geo_id"):
        pvz_list = await YADEL_CLIENT.get_pickup_points(data["geo_id"])
        if pvz_list:
            lines = []
            pvz_data = []
            for i, p in enumerate(pvz_list, 1):
                addr_obj = p.get("address", {})
                addr = addr_obj.get("full_address") or addr_obj.get("street", "адрес не указан")
                name = p.get("name", f"ПВЗ {i}")
                lines.append(f"{i}. <b>{name}</b>\n   📍 {addr}")
                pvz_data.append({"name": name, "address": addr, "id": p.get("id", "")})
            await state.update_data(pvz_list=pvz_data)
            await state.set_state(DeliveryOrder.waiting_pvz_choice)
            await callback.message.answer(
                "📦 Ближайшие пункты выдачи Яндекс Доставки:\n\n" + "\n\n".join(lines) +
                "\n\nВведите адрес нужного пункта или скопируйте из списка:",
                parse_mode="HTML",
            )
            await callback.answer()
            return

    await state.set_state(DeliveryOrder.waiting_address)
    await callback.message.answer("Введите адрес пункта выдачи:")
    await callback.answer()


@router.message(DeliveryOrder.waiting_pvz_choice)
async def fsm_pvz_choice(message: Message, state: FSMContext):
    await state.update_data(address=message.text.strip())
    await _calculate_and_confirm(message, state)


@router.message(DeliveryOrder.waiting_address)
async def fsm_delivery_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text.strip())
    await _calculate_and_confirm(message, state)


async def _calculate_and_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    product = await db.get_product(data["product_id"])

    delivery_cost = 0
    delivery_info = ""
    city = data.get("city", "")
    addr = data.get("address", "не указан")
    full_address = f"{city}, {addr}" if city else addr

    if YADEL_CLIENT:
        await message.answer("⏳ Рассчитываю стоимость доставки...")
        tariff = data.get("tariff", "time_interval")
        result = await YADEL_CLIENT.calculate_price(
            destination_address=full_address,
            tariff=tariff,
        )
        if result and "pricing_total" in result:
            # pricing_total приходит как "225.70 RUB"
            try:
                price_str = result["pricing_total"].split()[0]
                delivery_cost = int(float(price_str))
                days = result.get("delivery_days")
                if days:
                    delivery_info = f"до {days} дн."
            except Exception:
                pass

    delivery_type_str = "Курьер" if data.get("delivery_type") == "courier" else "Пункт выдачи"
    total = product["price"] + delivery_cost

    await state.update_data(
        delivery_cost=delivery_cost,
        total=total,
        delivery_type_str=delivery_type_str,
        addr=addr,
        full_address=full_address,
    )
    await state.set_state(DeliveryOrder.confirming)

    text = (
        f"📋 <b>Подтверждение заказа</b>\n\n"
        f"Товар: {product['name']} — {product['price']} ₽\n"
        f"Доставка ({delivery_type_str}): {delivery_cost} ₽ {delivery_info}\n"
        f"Адрес: {full_address}\n\n"
        f"<b>Итого: {total} ₽</b>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=confirm_keyboard(total))


@router.callback_query(F.data == "delivery:pay", DeliveryOrder.confirming)
async def cb_delivery_pay(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    product = await db.get_product(data["product_id"])

    total = data.get("total", product["price"])
    full_address = data.get("full_address", "")
    delivery_type_str = data.get("delivery_type_str", "")

    delivery_str = f"{delivery_type_str}: {full_address}"

    # Сохраняем адрес в БД — вебхук заберёт его после оплаты
    await db.save_pending_delivery(callback.from_user.id, product["id"], delivery_str)

    if not config.PRODAMUS_SHOP_URL:
        await callback.message.answer("Оплата временно недоступна. Напишите в поддержку.")
        return

    url = build_payment_url(
        shop_url=config.PRODAMUS_SHOP_URL,
        product_name=product["name"],
        price=total,
        user_id=callback.from_user.id,
        product_id=product["id"],
        order_type="p",
        secret=config.PRODAMUS_SECRET,
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {total} ₽", url=url)],
    ])

    await callback.message.answer(
        "Нажмите кнопку ниже, чтобы перейти к оплате. "
        "После успешной оплаты мы свяжемся с вами по доставке.",
        reply_markup=keyboard,
    )
    await state.clear()
    await callback.answer()
