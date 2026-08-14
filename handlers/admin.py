import asyncio
import json
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram import Bot
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton)
from datetime import datetime, timedelta, timezone

import config
from config import ADMIN_IDS
from keyboards.admin import (
    admin_menu_keyboard, admin_products_keyboard,
    admin_product_keyboard, admin_back_keyboard,
    product_back_keyboard,
    catalog_menu_back_keyboard, funnel_back_keyboard,
    confirm_delete_keyboard, category_keyboard,
    stats_keyboard, stats_back_keyboard,
    order_analytics_products_keyboard, order_analytics_back_keyboard, shipping_keyboard,
    funnels_keyboard, funnel_keyboard,
    product_submenu_content, product_submenu_media,
    product_submenu_infobiz, product_submenu_marketing,
    product_submenu_survey,
    catalog_submenu_keyboard,
    pending_orders_keyboard, pending_order_keyboard,
)
import logging
logger = logging.getLogger(__name__)
import database as db

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


class SetReviewPush(StatesGroup):
    waiting_delay = State()
    waiting_text = State()


class SurveyAddQuestion(StatesGroup):
    waiting_text = State()


class SurveyEditQuestion(StatesGroup):
    waiting_text = State()


class SurveyRepeat(StatesGroup):
    waiting_text = State()


class SurveyDelivery(StatesGroup):
    waiting_text = State()


class PackageSetup(StatesGroup):
    """Пошаговый ввод габаритов посылки."""
    weight = State()
    length = State()
    width = State()
    height = State()


class SurveyRouting(StatesGroup):
    waiting_text = State()


class SurveyPaid(StatesGroup):
    waiting_text = State()


# --- Фильтр: только для админов ---

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def admin_only(callback: CallbackQuery) -> bool:
    """Проверка для callback-хэндлеров: отвечает и возвращает False если не админ."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer()
        return False
    return True


# --- Команда /admin ---

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🛠 Панель администратора", reply_markup=admin_menu_keyboard())


# --- Меню ---

@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.clear()
    await callback.message.edit_text("🛠 Панель администратора", reply_markup=admin_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:menu_catalog")
async def cb_menu_catalog(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    from keyboards.admin import catalog_submenu_keyboard
    await callback.message.edit_text("🛍 Каталог", reply_markup=catalog_submenu_keyboard())
    await callback.answer()


# --- Незавершённые заказы (опрос заполнен, оплаты не было) ---

@router.callback_query(F.data == "admin:pending")
async def cb_pending_orders(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    orders = await db.get_pending_orders()
    if not orders:
        text = ("🛒 <b>Незавершённые заказы</b>\n\n"
                "<i>Пусто — все, кто заполнил заказ, оплатили.</i>")
    else:
        text = (f"🛒 <b>Незавершённые заказы</b> — <b>{len(orders)}</b>\n\n"
                "Клиенты заполнили заказ, но не оплатили. "
                "Нажми на строку, чтобы посмотреть ответы и контакты.")
        if len(orders) > 30:
            text += f"\n\n<i>Показаны последние 30 из {len(orders)}.</i>"
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=pending_orders_keyboard(orders)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:pending_view:"))
async def cb_pending_view(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    parts = callback.data.split(":")   # admin : pending_view : user_id : product_id
    uid, pid = int(parts[2]), int(parts[3])
    order = await db.get_pending_order(uid, pid)
    if not order:
        await callback.answer("Заказ уже не в списке.", show_alert=True)
        return
    product = await db.get_product(pid)
    rounds = []
    if order.get("survey_json"):
        try:
            rounds = json.loads(order["survey_json"])
        except Exception:
            pass

    user_row = None
    try:
        import aiosqlite
        async with aiosqlite.connect(db.DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT username, first_name FROM users WHERE user_id = ?", (uid,)
            ) as cur:
                user_row = await cur.fetchone()
    except Exception:
        pass
    username_str = f"@{user_row['username']}" if user_row and user_row["username"] else f"id:{uid}"
    first_name = (user_row["first_name"] if user_row else None) or "—"

    from handlers.prodamus_webhook import _format_order
    # Сумма, посчитанная при оформлении (товар + доставка). У старых заказов
    # её нет — считаем по цене товара, как раньше.
    price = _pending_amount(order, product)
    ts = order.get("created_at") or ""            # "YYYY-MM-DD HH:MM:SS"
    when = f"{ts[8:10]}.{ts[5:7]}.{ts[0:4]} {ts[11:16]}" if len(ts) >= 16 else ts
    text = _format_order(
        first_name, username_str, (product or {}).get("name", "—"),
        price, order.get("delivery_str") or "не указан", rounds, when,
    ).replace("🧾 <b>Новый заказ</b>", "🛒 <b>Незавершённый заказ</b> (не оплачен)")

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=pending_order_keyboard(uid, pid)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:pending_del:"))
async def cb_pending_del(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    parts = callback.data.split(":")
    uid, pid = int(parts[2]), int(parts[3])
    await db.delete_pending_delivery(uid, pid)
    await callback.answer("Убрано из списка")
    await cb_pending_orders(callback)


# --- Выгрузка заказов в Google Таблицу ---

@router.callback_query(F.data == "admin:gsheet_open")
async def cb_gsheet_open(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    from keyboards.admin import gsheet_keyboard
    from services.gsheets import SYNC_DELAY

    url = f"https://docs.google.com/spreadsheets/d/{config.GOOGLE_SHEET_ID}"
    orders = await db.get_orders_export()
    await callback.message.edit_text(
        "📊 <b>Заказы в Google Таблице</b>\n\n"
        f"Лист «{config.GOOGLE_SHEET_TAB}», заказов: <b>{len(orders)}</b>.\n\n"
        "Таблица обновляется сама: после каждой оплаты, когда приходит трек СДЭК "
        f"и когда заказ берут в работу или отправляют (через ~{int(SYNC_DELAY)} сек).\n\n"
        "Кнопка ниже нужна, только если хочется обновить прямо сейчас.",
        parse_mode="HTML",
        reply_markup=gsheet_keyboard(url),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:gsheet_sync")
async def cb_gsheet_sync(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    from services.gsheets import sync_orders, SheetsError

    await callback.answer("Выгружаю…")
    orders = await db.get_orders_export()
    if not orders:
        await callback.message.answer("Заказов пока нет — выгружать нечего.")
        return

    try:
        # gspread синхронный, в отдельном потоке — иначе подвиснет весь бот
        url = await asyncio.to_thread(sync_orders, orders)
    except SheetsError as e:
        logger.error(f"gsheet_sync: {e}")
        await callback.message.answer(
            f"❌ Не удалось обновить таблицу: {e}", parse_mode="HTML"
        )
        return
    except Exception as e:
        logger.exception("gsheet_sync: неожиданная ошибка")
        await callback.message.answer(f"❌ Ошибка выгрузки: {type(e).__name__}: {e}")
        return

    await callback.message.answer(
        f"✅ Таблица обновлена — заказов: <b>{len(orders)}</b>\n\n"
        f'<a href="{url}">Открыть таблицу</a>',
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# --- Габариты посылки (влияют на тариф СДЭК и накладную) ---

# Шаги мастера: состояние, подпись, единица, разумный предел
PACKAGE_STEPS = [
    (PackageSetup.weight, "вес", "г", 1, 100_000),
    (PackageSetup.length, "длину", "см", 1, 300),
    (PackageSetup.width, "ширину", "см", 1, 300),
    (PackageSetup.height, "высоту", "см", 1, 300),
]


@router.callback_query(F.data.startswith("admin:pkg:"))
async def cb_package(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    from keyboards.admin import package_keyboard
    await state.clear()
    pid = int(callback.data.split(":")[2])
    product = await db.get_product(pid)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    pkg = db.product_package(product)
    own = bool(product.get("pkg_weight"))
    src = "заданы для этого товара" if own else "общие из настроек сервера"
    await callback.message.edit_text(
        f"📐 <b>Габариты посылки</b> — {product['name']}\n\n"
        f"Сейчас ({src}):\n"
        f"• Вес: <b>{pkg['weight']} г</b>\n"
        f"• Длина: <b>{pkg['length']} см</b>\n"
        f"• Ширина: <b>{pkg['width']} см</b>\n"
        f"• Высота: <b>{pkg['height']} см</b>\n\n"
        "По ним считается стоимость доставки для покупателя и создаётся накладная СДЭК. "
        "Если реальные размеры больше, СДЭК пересчитает цену при приёмке — "
        "разницу придётся доплачивать вам.\n\n"
        "Указывайте размеры <b>коробки в упаковке</b>, а не самого товара.",
        parse_mode="HTML",
        reply_markup=package_keyboard(pid, own),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:pkg_reset:"))
async def cb_package_reset(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    pid = int(callback.data.split(":")[2])
    await db.set_product_package(pid, None, None, None, None)
    await state.clear()
    await callback.answer("Сброшено к общим значениям")
    callback.data = f"admin:pkg:{pid}"
    await cb_package(callback, state)


@router.callback_query(F.data.startswith("admin:pkg_start:"))
async def cb_package_start(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    pid = int(callback.data.split(":")[2])
    await state.set_state(PackageSetup.weight)
    await state.update_data(product_id=pid)
    await callback.message.answer(
        "Шаг 1 из 4. Пришлите <b>вес</b> посылки в граммах.\n"
        "<i>Например: 500 или 1200</i>",
        parse_mode="HTML",
    )
    await callback.answer()


async def _package_step(message: Message, state: FSMContext, index: int):
    """Разбирает число текущего шага и переходит к следующему."""
    _, label, unit, lo, hi = PACKAGE_STEPS[index]
    raw = (message.text or "").strip().replace(",", ".")
    try:
        value = int(round(float(raw)))
    except ValueError:
        await message.answer(f"Нужно число. Пришлите {label} в {unit}.")
        return
    if not lo <= value <= hi:
        await message.answer(f"Похоже на ошибку: допустимо от {lo} до {hi} {unit}.")
        return

    key = ("weight", "length", "width", "height")[index]
    await state.update_data(**{key: value})

    if index + 1 < len(PACKAGE_STEPS):
        nxt_state, nxt_label, nxt_unit, _, _ = PACKAGE_STEPS[index + 1]
        await state.set_state(nxt_state)
        await message.answer(
            f"Шаг {index + 2} из 4. Пришлите <b>{nxt_label}</b> в {nxt_unit}.",
            parse_mode="HTML",
        )
        return

    # Последний шаг — сохраняем
    data = await state.get_data()
    pid = data["product_id"]
    await db.set_product_package(pid, data["weight"], data["length"],
                                 data["width"], data["height"])
    await state.clear()
    product = await db.get_product(pid)
    questions = await db.get_product_questions(pid)
    await message.answer(
        f"✅ Габариты сохранены: <b>{data['weight']} г</b>, "
        f"{data['length']}×{data['width']}×{data['height']} см.\n\n"
        "Теперь доставка для этого товара считается по ним.",
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product, await db.get_product_purchase_count(pid)),
    )


@router.message(PackageSetup.weight)
async def fsm_package_weight(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await _package_step(message, state, 0)


@router.message(PackageSetup.length)
async def fsm_package_length(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await _package_step(message, state, 1)


@router.message(PackageSetup.width)
async def fsm_package_width(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await _package_step(message, state, 2)


@router.message(PackageSetup.height)
async def fsm_package_height(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await _package_step(message, state, 3)


# --- Пропущенные оплаты (вебхук мог не дойти, пока бот перезапускался) ---

# Заказ моложе этого возраста — клиент, скорее всего, ещё на странице оплаты
MISSED_MIN_AGE_MINUTES = 15


def _pending_amount(order: dict, product: dict | None) -> int:
    """Сумма заказа. У заказов, оформленных до появления колонки amount,
    её нет — считаем по цене товара и числу позиций."""
    amount = order.get("amount") or 0
    if amount:
        return int(amount)
    rounds = []
    if order.get("survey_json"):
        try:
            rounds = json.loads(order["survey_json"])
        except Exception:
            pass
    return (product or {}).get("price", 0) * max(1, len(rounds))


@router.callback_query(F.data.startswith("admin:paylink:"))
async def cb_pending_paylink(callback: CallbackQuery, bot: Bot):
    """Новая ссылка на оплату для незавершённого заказа.

    Нужна, когда у клиента ссылка не открылась (например, Prodamus сменил
    платформу) — прежнюю пересоздать нельзя, а заново проходить опрос
    клиента заставлять не хочется: сумма и состав заказа уже посчитаны.

    Клиенту сама не уходит: ссылка отдаётся админу, он решает, писать ли
    и что написать. Отправить одной кнопкой можно тут же.
    """
    if not await admin_only(callback):
        return
    parts = callback.data.split(":")
    uid, pid = int(parts[2]), int(parts[3])

    order = await db.get_pending_order(uid, pid)
    if not order:
        await callback.answer("Заказ уже не в списке.", show_alert=True)
        return
    product = await db.get_product(pid)
    amount = _pending_amount(order, product)
    if not amount:
        await callback.answer("У заказа не посчитана сумма — ссылку не собрать.",
                              show_alert=True)
        return

    await callback.answer("Создаю ссылку…")
    from services.prodamus import build_payment_url
    url = build_payment_url(
        shop_url=config.PRODAMUS_SHOP_URL_PHYSICAL,
        product_name=await _pending_payment_name(order, product),
        price=amount, user_id=uid, product_id=pid, order_type="p",
        secret=config.PRODAMUS_SECRET_PHYSICAL,
        notification_url=config.PRODAMUS_WEBHOOK_URL_PHYSICAL,
    )

    await callback.message.answer(
        f"🔗 <b>Новая ссылка на оплату</b> — <b>{amount} ₽</b>\n"
        f"{url}\n\n"
        f"Клиенту пока не отправлена — скопируйте её или отправьте кнопкой ниже.",
        parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📨 Отправить клиенту",
                                 callback_data=f"admin:paysend:{uid}:{pid}:{amount}")
        ]]),
    )


@router.callback_query(F.data.startswith("admin:paysend:"))
async def cb_pending_paysend(callback: CallbackQuery, bot: Bot):
    """Отправляет клиенту ссылку, созданную кнопкой выше — уже по решению админа.

    Ссылку не пересобираем из callback_data (она длинная и там не помещается),
    а создаём заново: у Prodamus каждая ссылка одноразово-независима, лишняя
    несозданная оплата ничему не мешает.
    """
    if not await admin_only(callback):
        return
    parts = callback.data.split(":")
    uid, pid, amount = int(parts[2]), int(parts[3]), int(parts[4])

    order = await db.get_pending_order(uid, pid)
    if not order:
        await callback.answer("Заказ уже не в списке.", show_alert=True)
        return

    await callback.answer("Отправляю…")
    from services.prodamus import build_payment_url
    url = build_payment_url(
        shop_url=config.PRODAMUS_SHOP_URL_PHYSICAL,
        product_name=await _pending_payment_name(order, await db.get_product(pid)),
        price=amount, user_id=uid, product_id=pid, order_type="p",
        secret=config.PRODAMUS_SECRET_PHYSICAL,
        notification_url=config.PRODAMUS_WEBHOOK_URL_PHYSICAL,
    )
    try:
        await bot.send_message(uid, (
            "🔗 Вот новая ссылка на оплату вашего заказа — прежняя могла не открыться.\n"
            "Состав заказа и сумма прежние, заново ничего заполнять не нужно."
        ), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=url)
        ]]))
    except Exception as e:
        logger.warning(f"paylink: не отправил клиенту {uid}: {e}")
        await callback.message.answer(
            "⚠️ Клиенту отправить не удалось — возможно, он заблокировал бота.")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ Отправил клиенту новую ссылку на оплату.")


async def _pending_payment_name(order: dict, product: dict | None) -> str:
    """Название позиции для ссылки — как при оформлении (см. delivery.py)."""
    from handlers.delivery import _goods_payment_name
    try:
        rounds = json.loads(order.get("survey_json") or "[]")
    except Exception:
        rounds = []
    round_products = db.unpack_round_products(
        order.get("round_products_json"), rounds, order["product_id"])
    counts: dict[int, int] = {}
    for rp in round_products or [order["product_id"]]:
        counts[rp] = counts.get(rp, 0) + 1
    items = []
    for rp, qty in counts.items():
        p = await db.get_product(rp) or product
        if p:
            items.append((p, qty))
    return _goods_payment_name(items) if items else (product or {}).get("name", "Заказ")


@router.callback_query(F.data == "admin:missed")
async def cb_missed_payments(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    from keyboards.admin import missed_orders_keyboard

    orders = await db.get_pending_orders()
    # created_at пишется как CURRENT_TIMESTAMP — это UTC, сравниваем с UTC
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    suspicious = []
    for o in orders:
        ts = o.get("created_at") or ""
        try:
            created = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if (now - created).total_seconds() >= MISSED_MIN_AGE_MINUTES * 60:
            suspicious.append(o)

    head = "🔁 <b>Пропущенные оплаты</b>\n\n"
    if not suspicious:
        text = head + (
            "<i>Подозрительных заказов нет.</i>\n\n"
            f"Сюда попадают заказы старше {MISSED_MIN_AGE_MINUTES} минут, по которым "
            "не пришло уведомление об оплате."
        )
    else:
        text = head + (
            f"Заказов под вопросом: <b>{len(suspicious)}</b>\n\n"
            "Клиент дошёл до кнопки оплаты, но уведомление так и не пришло. "
            "Либо он не заплатил, либо вебхук потерялся, пока бот перезапускался.\n\n"
            "<b>Как проверить наверняка:</b> кабинет Prodamus → «Список платежей» → "
            "найти заказ. Если оплата там есть — нажми в блоке URL-уведомлений значок "
            "обновления, Prodamus пришлёт вебхук заново и заказ проведётся сам.\n\n"
            "Если так не выходит — открой заказ здесь и проведи его кнопкой вручную."
        )
        if len(suspicious) > 30:
            text += f"\n\n<i>Показаны первые 30 из {len(suspicious)}.</i>"

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=missed_orders_keyboard(suspicious)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:force_paid:"))
async def cb_force_paid(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    from keyboards.admin import force_paid_confirm_keyboard
    parts = callback.data.split(":")
    uid, pid = int(parts[2]), int(parts[3])

    order = await db.get_pending_order(uid, pid)
    if not order:
        await callback.answer("Заказ уже не в списке.", show_alert=True)
        return
    product = await db.get_product(pid)
    amount = _pending_amount(order, product)

    await callback.message.edit_text(
        "⚠️ <b>Провести заказ вручную?</b>\n\n"
        f"Товар: <b>{(product or {}).get('name', '—')}</b>\n"
        f"Сумма: <b>{amount} ₽</b>\n\n"
        "Бот оформит заказ так же, как после обычной оплаты: запишет покупку, "
        "пришлёт вам заявку, отправит клиенту сообщение об успешной оплате "
        "и выдаст товар, если он цифровой.\n\n"
        "<b>Нажимайте только если оплата действительно есть в кабинете Prodamus.</b> "
        "Деньги при этом не списываются — бот лишь считает заказ оплаченным.",
        parse_mode="HTML",
        reply_markup=force_paid_confirm_keyboard(uid, pid),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:force_paid_yes:"))
async def cb_force_paid_yes(callback: CallbackQuery, bot: Bot):
    if not await admin_only(callback):
        return
    from handlers.prodamus_webhook import provision_payment
    parts = callback.data.split(":")
    uid, pid = int(parts[2]), int(parts[3])

    order = await db.get_pending_order(uid, pid)
    if not order:
        await callback.answer("Заказ уже не в списке.", show_alert=True)
        return

    product = await db.get_product(pid)
    amount = _pending_amount(order, product)
    # Тот же сквозной идентификатор, что и в платёжной ссылке: если вебхук
    # всё-таки придёт позже, повторной выдачи не будет — сработает идемпотентность
    order_num = f"manual_p_{uid}_{pid}"

    await callback.answer("Провожу заказ…")
    try:
        result = await provision_payment(bot, uid, pid, "p", order_num, amount)
    except Exception as e:
        logger.error(f"force_paid: не удалось провести заказ {uid}/{pid}: {e}")
        await callback.message.answer(
            f"❌ Не удалось провести заказ: {e}\nЗаказ остался в списке."
        )
        return

    if result is False:
        await callback.message.answer("❌ Товар не найден — заказ не проведён.")
        return
    if result is None:
        await callback.message.answer("ℹ️ Этот заказ уже был проведён раньше.")
        return

    logger.info(f"force_paid: админ {callback.from_user.id} провёл заказ {uid}/{pid} на {amount} ₽")
    await callback.message.answer(
        f"✅ Заказ проведён на <b>{amount} ₽</b>.\n"
        f"Товар: {(product or {}).get('name', '—')}\n"
        "Клиенту отправлено подтверждение, заявка ушла в админский чат.",
        parse_mode="HTML",
    )
    await cb_pending_orders(callback)


@router.callback_query(F.data == "admin:menu_marketing")
async def cb_menu_marketing(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    from keyboards.admin import marketing_submenu_keyboard
    await callback.message.edit_text("📣 Маркетинг", reply_markup=marketing_submenu_keyboard())
    await callback.answer()


# --- Список товаров ---

@router.callback_query(F.data == "admin:products")
async def cb_admin_products(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    products = await db.get_all_products(active_only=False)
    if not products:
        await callback.message.edit_text(
            "Товаров нет.", reply_markup=catalog_menu_back_keyboard()
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
        await callback.answer()
        return
    product_id = int(callback.data.split(":")[2])
    product = await db.get_product(product_id)
    if not product:
        await callback.answer("Не найден.", show_alert=True)
        return

    purchase_count = await db.get_product_purchase_count(product_id)
    text = await _product_card_text(product, purchase_count)
    kb = admin_product_keyboard(product, purchase_count)

    try:
        if callback.message.photo:
            await callback.message.delete()
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
        else:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"cb_admin_product edit error: {e}")
        try:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
        except Exception as e2:
            logger.error(f"cb_admin_product fallback error: {e2}")

    await callback.answer()


# --- Добавить товар ---

@router.callback_query(F.data == "admin:add_product")
async def cb_add_product(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await state.set_state(AddProduct.name)
    await callback.message.edit_text(
        "Введи <b>название</b> товара:",
        parse_mode="HTML",
        reply_markup=catalog_menu_back_keyboard(),
    )
    await callback.answer()


@router.message(AddProduct.name)
async def fsm_product_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(AddProduct.description)
    await message.answer(
        "Введи <b>описание</b> товара.\n\n"
        "Отправь <code>-</code> — чтобы оставить товар <b>без описания</b>.",
        parse_mode="HTML",
    )


@router.message(AddProduct.description)
async def fsm_product_description(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    desc = (message.text or message.caption or "").strip()
    await state.update_data(description="" if desc == "-" else desc)
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
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(EditProduct.name)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "Введи новое <b>название</b> товара:",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(product_id),
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
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(EditProduct.description)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "Введи новое <b>описание</b> товара.\n\n"
        "Отправь <code>-</code> — чтобы <b>убрать описание</b> "
        "(в карточке останется только название и цена).",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(product_id),
    )
    await callback.answer()


@router.message(EditProduct.description)
async def fsm_edit_description(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    text = (message.text or message.caption or "").strip()
    # «-» очищает описание
    cleared = text == "-"
    await db.update_product_description(product_id, "" if cleared else text)
    await state.clear()
    product = await db.get_product(product_id)
    await message.answer(
        "✅ Описание убрано." if cleared else "✅ Описание обновлено.",
        reply_markup=admin_product_keyboard(product),
    )


# --- Редактировать цену ---

@router.callback_query(F.data.startswith("admin:edit_price:"))
async def cb_edit_price(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(EditProduct.price)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "Введи новую <b>цену</b> в рублях (только число):",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(product_id),
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
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(UploadFile.waiting_file)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "📎 Отправь файл (документ) для этого товара:",
        reply_markup=product_back_keyboard(product_id),
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
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(UploadPhoto.waiting_photo)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "🖼 Отправь фото для этого товара:",
        reply_markup=product_back_keyboard(product_id),
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
    if not await admin_only(callback):
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
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    await callback.message.edit_text(
        "Ты уверен, что хочешь удалить этот товар?",
        reply_markup=confirm_delete_keyboard(product_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:confirm_delete:"))
async def cb_confirm_delete(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    await db.delete_product(product_id)
    await callback.answer("Удалено.", show_alert=True)
    products = await db.get_all_products(active_only=False)
    await callback.message.edit_text(
        "📦 Все товары:",
        reply_markup=admin_products_keyboard(products) if products else catalog_menu_back_keyboard(),
    )


# --- Загрузить инструкцию (PDF) ---

@router.callback_query(F.data.startswith("admin:upload_instruction:"))
async def cb_upload_instruction(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(UploadInstruction.waiting_file)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "📄 Отправь файл инструкции — PDF или картинку (JPG, PNG):",
        reply_markup=product_back_keyboard(product_id),
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
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(SetVideo.waiting_url)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "🎬 Отправь ссылку на видео-урок (YouTube, Vimeo и т.д.):\n\n"
        "Чтобы <b>удалить</b> текущую ссылку — отправь <code>-</code>",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(product_id),
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
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    await callback.message.edit_text(
        "Выберите категорию товара:",
        reply_markup=category_keyboard(product_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:category:"))
async def cb_category_select(callback: CallbackQuery):
    if not await admin_only(callback):
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
    if not await admin_only(callback):
        return
    await state.set_state(UploadBanner.waiting_photo)
    await callback.message.edit_text(
        "🖼 Отправь картинку, которая будет показываться при открытии каталога.\n\n"
        "Чтобы <b>удалить</b> баннер — отправь <code>-</code>",
        parse_mode="HTML",
        reply_markup=catalog_menu_back_keyboard(),
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
        reply_markup=catalog_submenu_keyboard(),
    )


@router.message(UploadBanner.waiting_photo, F.text == "-")
async def fsm_remove_banner(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await db.set_setting("catalog_banner_file_id", "")
    await state.clear()
    await message.answer(
        "✅ Баннер каталога удалён.",
        reply_markup=catalog_submenu_keyboard(),
    )


# --- Инфобиз: счётчик и динамическая цена ---

@router.callback_query(F.data.startswith("admin:toggle_counter:"))
async def cb_toggle_counter(callback: CallbackQuery):
    if not await admin_only(callback):
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
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(SetPriceTrigger.waiting_count)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "🔢 Введи количество покупок, после которого цена изменится:\n\n"
        "<i>Например: 30</i>",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(product_id),
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
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    await state.set_state(SetPriceAfterTrigger.waiting_price)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        "💲 Введи новую цену (в рублях), которая будет после триггера:\n\n"
        "<i>Например: 5000</i>",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(product_id),
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
    if not await admin_only(callback):
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
        reply_markup=product_back_keyboard(product_id),
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
    if not await admin_only(callback):
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
        reply_markup=product_back_keyboard(product_id),
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
        reply_markup=product_back_keyboard(data["product_id"]),
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
    if not await admin_only(callback):
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
    if not await admin_only(callback):
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

        f"📋 <b>Список ожидания:</b> {s['waitlist_total']} чел.\n"
        f"🎯 <b>Конверсия</b> (старт → покупка): <b>{s['conversion']}%</b>\n\n"

        f"📦 <b>По товарам</b> (всё время)\n"
    )
    by_product = await db.get_sales_by_product()
    if by_product:
        for p in by_product:
            text += f"  {p['name']} — <b>{p['count']}</b> шт · <b>{p['revenue']:,} ₽</b>\n".replace(",", " ")
    else:
        text += "  <i>продаж пока нет</i>\n"

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=stats_keyboard()
    )
    await callback.answer()


_MONTH_NAMES = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
]


@router.callback_query(F.data == "admin:stats_export")
async def cb_stats_export(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer("Готовлю файл…")
    import csv, io
    from aiogram.types import BufferedInputFile

    rows = await db.get_purchases_export()
    buf = io.StringIO()
    buf.write("﻿")  # BOM — чтобы кириллица корректно открывалась в Excel
    w = csv.writer(buf, delimiter=";")
    w.writerow(["ID", "Дата (UTC)", "user_id", "username", "Товар", "Сумма ₽", "Номер платежа"])
    total = 0
    for r in rows:
        total += r["amount"] or 0
        w.writerow([r["id"], r["created_at"], r["user_id"],
                    r["username"] or "", r["product"], r["amount"], r["telegram_payment_id"] or ""])
    w.writerow([])
    w.writerow(["", "", "", "", "ИТОГО", total, f"{len(rows)} шт"])

    data = buf.getvalue().encode("utf-8")
    now = datetime.now(MSK).strftime("%Y-%m-%d_%H-%M")
    file = BufferedInputFile(data, filename=f"sales_{now}.csv")
    await callback.message.answer_document(
        file,
        caption=(f"📥 Выгрузка продаж\n"
                 f"Всего: <b>{len(rows)}</b> покупок на <b>{total:,} ₽</b>".replace(",", " ")),
        parse_mode="HTML",
    )


def _msk_dt(ts: str | None):
    """'2026-07-27 12:45:14' (UTC) → datetime по Москве."""
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S") + timedelta(hours=3)
    except Exception:
        return None


def _days_ago(ts: str | None) -> str:
    dt = _msk_dt(ts)
    if not dt:
        return ""
    days = (datetime.utcnow() + timedelta(hours=3) - dt).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "1 день"
    if days < 5:
        return f"{days} дня"
    return f"{days} дн."


def _client(o: dict) -> str:
    return f"@{o['username']}" if o.get("username") else (o.get("first_name") or f"id:{o['user_id']}")


@router.callback_query(F.data.startswith("admin:shipping"))
async def cb_shipping(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    parts = callback.data.split(":")
    mode = parts[2] if len(parts) > 2 else "unshipped"

    orders = await db.get_orders()
    unshipped = [o for o in orders if not o.get("shipped_at")]
    shipped = [o for o in orders if o.get("shipped_at")]

    lines = [
        "🚚 <b>Отправка заказов</b>\n",
        f"Всего оплаченных заказов: <b>{len(orders)}</b>",
        f"📦 Отправлено: <b>{len(shipped)}</b>   ⏳ Не отправлено: <b>{len(unshipped)}</b>\n",
    ]

    rows = unshipped if mode == "unshipped" else shipped
    title = "⏳ <b>Ждут отправки</b>" if mode == "unshipped" else "📦 <b>Отправленные</b>"
    lines.append(title)

    if not rows:
        lines.append("<i>— пусто —</i>")
    for o in rows[:30]:
        num = o.get("order_code") or o.get("prodamus_order_id") or f"#{o['id']}"
        created = _msk_dt(o.get("created_at"))
        created_s = created.strftime("%d.%m %H:%M") if created else "—"
        lines.append(f"\n<b>№{num}</b> · {o.get('product_name') or 'товар удалён'}")
        info = f"   👤 {_client(o)} · 🧑‍🔧 {o.get('assignee_name') or 'не взят'}"
        lines.append(info)
        if mode == "unshipped":
            lines.append(f"   🕒 оплачен {created_s} · ждёт {_days_ago(o.get('created_at'))}")
        else:
            sh = _msk_dt(o.get("shipped_at"))
            sh_s = sh.strftime("%d.%m %H:%M") if sh else "—"
            lines.append(f"   📦 отправлен {sh_s} · {o.get('shipped_by_name') or ''}")
    if len(rows) > 30:
        lines.append(f"\n<i>…и ещё {len(rows) - 30}</i>")

    try:
        await callback.message.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=shipping_keyboard(mode)
        )
        await callback.answer()
    except Exception:
        await callback.answer("Актуально ✅")


@router.callback_query(F.data == "admin:order_analytics")
async def cb_order_analytics(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    products = await db.get_all_products(active_only=False)
    physical = [p for p in products if p.get("category") == "physical"]
    if not physical:
        await callback.answer("Нет физических товаров.", show_alert=True)
        return
    await callback.message.edit_text(
        "🎤 <b>Аналитика заказов</b>\n\nВыбери товар — покажу разбивку по ответам "
        "(цвет, текстура и т.д.) и кто печатал.",
        parse_mode="HTML",
        reply_markup=order_analytics_products_keyboard(physical),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:oa:"))
async def cb_oa_product(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    from collections import Counter
    pid = int(callback.data.split(":")[2])
    product = await db.get_product(pid)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    orders = await db.get_orders_for_product(pid)
    questions = await db.get_product_questions(pid)

    q_counts = {q["text"]: Counter() for q in questions}
    assignee = Counter()
    total_orders = len(orders)
    total_positions = 0
    shipped_n = 0

    for o in orders:
        rounds = []
        if o.get("rounds_json"):
            try:
                rounds = json.loads(o["rounds_json"])
            except Exception:
                pass
        for answers in rounds:
            total_positions += 1
            for a in answers:
                q = a.get("q")
                v = (a.get("text") or "").strip() or "📷/файл"
                if q in q_counts:
                    q_counts[q][v] += 1
        assignee[o.get("assignee_name") or "не взято"] += 1
        if o.get("shipped_at"):
            shipped_n += 1

    if total_orders == 0:
        await callback.message.edit_text(
            f"🎤 <b>{product['name']}</b>\n\n<i>Заказов пока нет — аналитика появится "
            "после первых оплат.</i>",
            parse_mode="HTML",
            reply_markup=order_analytics_back_keyboard(),
        )
        await callback.answer()
        return

    lines = [
        f"🎤 <b>{product['name']}</b> — аналитика\n",
        f"Заказов: <b>{total_orders}</b>  |  позиций: <b>{total_positions}</b>",
        f"📦 Отправлено: <b>{shipped_n}</b>  |  ⏳ ждут отправки: "
        f"<b>{total_orders - shipped_n}</b>\n",
    ]
    for q in questions:
        cnt = q_counts[q["text"]]
        if not cnt:
            continue
        qshort = q["text"] if len(q["text"]) <= 60 else q["text"][:59] + "…"
        parts = " · ".join(f"<b>{v}</b> — {n}" for v, n in cnt.most_common())
        lines.append(f"• {qshort}\n   {parts}")
    lines.append("\n🖨 <b>Кто печатал</b>")
    for name, n in assignee.most_common():
        lines.append(f"   {name} — <b>{n}</b>")

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=order_analytics_back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:stats_monthly")
async def cb_stats_monthly(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer()

    rows = await db.get_monthly_earnings_breakdown(months=12)

    if not rows:
        await callback.message.edit_text(
            "📅 <b>Заработок по месяцам</b>\n\nДанных пока нет.",
            parse_mode="HTML",
            reply_markup=stats_back_keyboard(),
        )
        return

    fee_pct = config.PRODAMUS_FEE_PERCENT

    lines = ["📅 <b>Заработок по месяцам</b>\n"]
    from services.daily_report import _money, _expenses_for
    import calendar as _cal
    for r in rows:
        delivery = float(r["delivery"] or 0)
        gross = float(r["total"])
        y, mth = int(r["year"]), int(r["month"])
        # Расходы за месяц уходят из общей прибыли до дележа
        spent = (await _expenses_for(
            f"{y:04d}-{mth:02d}-01",
            f"{y:04d}-{mth:02d}-{_cal.monthrange(y, mth)[1]:02d}"))["total"]
        net_after_tax = _money(
            gross, delivery, fee_pct, spent,
            delivery_cost=float(r["delivery_cost"] or 0),
            delivery_legacy=float(r["delivery_legacy"] or 0),
        )["net"]
        misha = net_after_tax * 0.80
        danya = net_after_tax * 0.20

        month_name = _MONTH_NAMES[int(r["month"])]
        year = r["year"]

        delivery_note = f" (в т.ч. доставка {delivery:,.0f} ₽)" if delivery else ""
        spent_note = f"\n  🧾 Расходы: −{spent:,.0f} ₽" if spent else ""
        lines.append(
            f"<b>{month_name} {year}</b> — {r['count']} прод. — {gross:,.0f} ₽{delivery_note}"
            f"{spent_note}\n"
            f"  Миша: <b>{misha:,.2f} ₽</b>  |  Даня: <b>{danya:,.2f} ₽</b>"
        )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=stats_back_keyboard(),
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
    if not await admin_only(callback):
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
    if not await admin_only(callback):
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
    if not await admin_only(callback):
        return
    await state.set_state(CreateFunnel.waiting_name)
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    await callback.message.edit_text(
        "Введи название воронки (например: <code>Разбор профиля</code>):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад к воронкам", callback_data="admin:funnels")],
        ]),
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
    if not await admin_only(callback):
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
        parse_mode="HTML", reply_markup=funnel_back_keyboard(funnel_id)
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
    data = await state.get_data()
    await state.set_state(FunnelAddStep.waiting_text)
    await message.answer(
        "✍️ Теперь введи текст сообщения (поддерживается HTML):",
        reply_markup=funnel_back_keyboard(data["funnel_id"])
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
    if not await admin_only(callback):
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
        parse_mode="HTML", reply_markup=funnel_back_keyboard(funnel_id)
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
    await message.answer("✍️ Введи новый текст сообщения:", reply_markup=funnel_back_keyboard(data["funnel_id"]))


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
    if not await admin_only(callback):
        return
    funnel_id = callback.data.split(":", 2)[2]
    await state.set_state(FunnelSetProduct.waiting_product_id)
    await state.update_data(funnel_id=funnel_id)
    products = await db.get_all_products(active_only=False)
    products_text = "\n".join(f"  <code>{p['id']}</code> — {p['name']}" for p in products)
    await callback.message.edit_text(
        f"Введи ID товара, покупка которого отменяет воронку:\n\n{products_text}\n\n"
        "Отправь <code>0</code> чтобы убрать привязку.",
        parse_mode="HTML", reply_markup=funnel_back_keyboard(funnel_id)
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
    if not await admin_only(callback):
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


@router.callback_query(F.data == "admin:noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


# --- Подменю карточки товара ---

@router.callback_query(F.data.startswith("admin:psub:"))
async def cb_product_submenu(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    parts = callback.data.split(":")   # admin : psub : sub : pid
    sub = parts[2]
    pid = int(parts[3])
    product = await db.get_product(pid)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    purchase_count = await db.get_product_purchase_count(pid)

    titles = {
        "content": "✏️ Контент",
        "media": "📎 Медиафайлы",
        "infobiz": "📚 Инфобиз",
        "marketing": "💌 Маркетинг",
    }
    if sub == "content":
        kb = product_submenu_content(product)
    elif sub == "media":
        kb = product_submenu_media(product)
    elif sub == "infobiz":
        kb = product_submenu_infobiz(product, purchase_count)
    elif sub == "marketing":
        kb = product_submenu_marketing(product)
    elif sub == "survey":
        questions = await db.get_product_questions(pid)
        kb = product_submenu_survey(product, questions)
    else:
        await callback.answer()
        return

    if sub == "survey":
        body = _survey_body(product, questions)
    else:
        body = f"<b>{product['name']}</b> — {titles.get(sub, sub)}"

    try:
        await callback.message.edit_text(body, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"cb_product_submenu error: {e}")
        await callback.message.answer(body, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


def _survey_body(product: dict, questions: list[dict]) -> str:
    n = len(questions)
    tail = (f"Сейчас вопросов: <b>{n}</b>. Порядок — стрелками."
            if n else "<i>Вопросов пока нет — добавьте первый.</i>")
    repeat = product.get("survey_repeat_text")
    repeat_line = (f"\n\n🔁 Доп. позиция: <i>{repeat}</i>" if repeat else "")
    if config.CDEK_ENABLED:
        delivery_line = (
            "\n\n🚚 Доставка: <b>СДЭК до пункта выдачи</b> — после опроса бот сам спросит "
            "город, покажет ближайшие ПВЗ и посчитает стоимость."
        )
    else:
        delivery_line = ""
    return (
        f"<b>{product['name']}</b> — 📋 Опрос\n\n"
        "Вопросы задаются покупателю по одному, сразу после открытия карточки товара. "
        "Ответы (текст и фото) собираются и приходят вам в чат.\n\n"
        f"{tail}{repeat_line}{delivery_line}"
    )


async def _render_survey(callback: CallbackQuery, pid: int):
    product = await db.get_product(pid)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    questions = await db.get_product_questions(pid)
    kb = product_submenu_survey(product, questions)
    body = _survey_body(product, questions)
    try:
        await callback.message.edit_text(body, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(body, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("admin:survey_add:"))
async def cb_survey_add(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    pid = int(callback.data.split(":")[2])
    await state.set_state(SurveyAddQuestion.waiting_text)
    await state.update_data(product_id=pid)
    await callback.message.edit_text(
        "✍️ Введи текст вопроса, который бот задаст покупателю.\n\n"
        "Можно прислать <b>фото с подписью</b> — тогда бот покажет это фото вместе с вопросом.",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(pid),
    )
    await callback.answer()


# Альбом (несколько фото) приходит отдельными сообщениями — собираем их вместе
_album_buf: dict = {}


async def _album_collect(message: Message, state: FSMContext, kind: str):
    mgid = message.media_group_id
    buf = _album_buf.setdefault(mgid, {"msgs": [], "task": None})
    buf["msgs"].append(message)
    if buf["task"] is None:
        buf["task"] = asyncio.create_task(_album_flush(mgid, state, kind))


async def _album_flush(mgid: str, state: FSMContext, kind: str):
    await asyncio.sleep(1.3)
    buf = _album_buf.pop(mgid, None)
    if not buf or not buf["msgs"]:
        return
    msgs = buf["msgs"]
    cur = await state.get_state()
    if kind == "add" and cur == SurveyAddQuestion.waiting_text.state:
        await _save_survey_add(msgs, state, msgs[0])
    elif kind == "edit" and cur == SurveyEditQuestion.waiting_text.state:
        await _save_survey_edit(msgs, state, msgs[0])


def _extract_qa(msgs: list) -> tuple[str, list]:
    """Из сообщения(-ий) достаёт текст вопроса и список file_id фото."""
    photo_ids, text = [], ""
    for m in msgs:
        if m.photo:
            photo_ids.append(m.photo[-1].file_id)
        cap = m.caption or m.text
        if cap and not text:
            text = cap.strip()
    return text, photo_ids


def _pic_suffix(photo_ids: list) -> str:
    if len(photo_ids) > 1:
        return f" 📷×{len(photo_ids)}"
    return " 📷" if photo_ids else ""


async def _save_survey_add(msgs: list, state: FSMContext, target: Message):
    text, photo_ids = _extract_qa(msgs)
    if not text:
        hint = ("Пришли фото с подписью-вопросом." if photo_ids
                else "Введи текст вопроса (можно фото с подписью).")
        await target.answer(f"Вопрос не может быть пустым. {hint}")
        return
    data = await state.get_data()
    pid = data["product_id"]
    await db.add_product_question(pid, text, photo_ids=photo_ids)
    await state.clear()
    product = await db.get_product(pid)
    questions = await db.get_product_questions(pid)
    await target.answer(
        f"✅ Вопрос добавлен{_pic_suffix(photo_ids)} (всего: {len(questions)}).",
        reply_markup=product_submenu_survey(product, questions),
    )


@router.message(SurveyAddQuestion.waiting_text)
async def fsm_survey_add(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.media_group_id:
        await _album_collect(message, state, "add")
        return
    await _save_survey_add([message], state, message)


@router.callback_query(F.data.startswith("admin:survey_edit:"))
async def cb_survey_edit(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    parts = callback.data.split(":")   # admin : survey_edit : qid : pid
    qid, pid = int(parts[2]), int(parts[3])
    q = await db.get_product_question(qid)
    if not q:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return
    has_photo = "\n📷 <i>к вопросу прикреплено фото</i>" if q.get("photo_id") else ""
    await state.set_state(SurveyEditQuestion.waiting_text)
    await state.update_data(question_id=qid, product_id=pid)
    await callback.message.edit_text(
        f"✏️ <b>Редактирование вопроса</b>\n\n"
        f"Сейчас: <i>{q['text']}</i>{has_photo}\n\n"
        "Пришли новый текст вопроса.\n"
        "• Пришлёшь <b>фото с подписью</b> — заменю и текст, и фото.\n"
        "• Пришлёшь только текст — обновлю текст, фото останется прежним.",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(pid),
    )
    await callback.answer()


async def _save_survey_edit(msgs: list, state: FSMContext, target: Message):
    text, photo_ids = _extract_qa(msgs)
    if not text:
        await target.answer("Вопрос не может быть пустым. Пришли новый текст (можно фото с подписью).")
        return
    data = await state.get_data()
    qid, pid = data["question_id"], data["product_id"]
    # фото меняем только если прислали новые (иначе текст правим, фото сохраняем)
    await db.update_product_question(qid, text, photo_ids=photo_ids, change_photos=bool(photo_ids))
    await state.clear()
    product = await db.get_product(pid)
    questions = await db.get_product_questions(pid)
    await target.answer(
        f"✅ Вопрос обновлён{_pic_suffix(photo_ids)}.",
        reply_markup=product_submenu_survey(product, questions),
    )


@router.message(SurveyEditQuestion.waiting_text)
async def fsm_survey_edit(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.media_group_id:
        await _album_collect(message, state, "edit")
        return
    await _save_survey_edit([message], state, message)


@router.callback_query(F.data.startswith("admin:survey_router:"))
async def cb_survey_router(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    parts = callback.data.split(":")   # admin : survey_router : qid : pid
    qid, pid = int(parts[2]), int(parts[3])
    on = await db.toggle_router_question(pid, qid)
    await _render_survey(callback, pid)
    await callback.answer("🎨 Это вопрос-цвет (по нему определяем, кто печатает)" if on
                          else "Снял пометку вопроса-цвета")


@router.callback_query(F.data.startswith("admin:survey_routing:"))
async def cb_survey_routing(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    pid = int(callback.data.split(":")[2])
    product = await db.get_product(pid)
    current = product.get("order_routing_text") if product else None
    questions = await db.get_product_questions(pid)
    has_router = any(q.get("is_router") for q in questions)
    now = f"\n\nСейчас:\n<code>{current}</code>" if current else "\n\n<i>Пока не задано.</i>"
    warn = "" if has_router else ("\n\n⚠️ Сначала пометьте вопрос-цвет кнопкой 🎨 "
                                  "в списке вопросов — по нему определяется номер цвета.")
    await state.set_state(SurveyRouting.waiting_text)
    await state.update_data(product_id=pid)
    await callback.message.edit_text(
        "🖨 <b>Кто печатает — по номеру цвета</b>\n\n"
        "Пришли распределение по строкам «Имя: номера». Пример:\n"
        "<code>Даня: 1, 2, 3, 13\nПартнёр: 4, 5, 6, 12</code>\n\n"
        "В заказе появится строка «🖨 Печатает: …» по номеру цвета из ответа клиента."
        f"{now}{warn}\n\n"
        "Отправь <code>-</code> — чтобы отключить.",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(pid),
    )
    await callback.answer()


@router.message(SurveyRouting.waiting_text)
async def fsm_survey_routing(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or message.caption or "").strip()
    data = await state.get_data()
    pid = data["product_id"]
    if text == "-":
        await db.set_order_routing_text(pid, None)
        note = "🖨 Распределение отключено."
    elif not text:
        await message.answer("Пусто. Пришли строки «Имя: номера» или <code>-</code>.", parse_mode="HTML")
        return
    else:
        await db.set_order_routing_text(pid, text)
        note = "✅ Распределение сохранено."
    await state.clear()
    product = await db.get_product(pid)
    await message.answer(note, reply_markup=admin_product_keyboard(product))


@router.callback_query(F.data.startswith("admin:survey_paid:"))
async def cb_survey_paid(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    from handlers.prodamus_webhook import DEFAULT_POST_PAYMENT
    pid = int(callback.data.split(":")[2])
    product = await db.get_product(pid)
    current = product.get("post_payment_text") if product else None
    shown = current or DEFAULT_POST_PAYMENT
    await state.set_state(SurveyPaid.waiting_text)
    await state.update_data(product_id=pid)
    await callback.message.edit_text(
        "💬 <b>Сообщение клиенту после оплаты</b>\n\n"
        f"Сейчас{' (по умолчанию)' if not current else ''}:\n<code>{shown}</code>\n\n"
        "Пришли новый текст. Можно вставить <code>{order}</code> — вместо него подставится "
        "номер заказа.\n\n"
        "Отправь <code>-</code> — вернуть текст по умолчанию.",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(pid),
    )
    await callback.answer()


@router.message(SurveyPaid.waiting_text)
async def fsm_survey_paid(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or message.caption or "").strip()
    data = await state.get_data()
    pid = data["product_id"]
    if text == "-":
        await db.set_post_payment_text(pid, None)
        note = "💬 Вернул текст после оплаты по умолчанию."
    elif not text:
        await message.answer("Пусто. Пришли текст или <code>-</code> для сброса.", parse_mode="HTML")
        return
    else:
        await db.set_post_payment_text(pid, text)
        note = "✅ Сообщение после оплаты сохранено."
    await state.clear()
    product = await db.get_product(pid)
    await message.answer(note, reply_markup=admin_product_keyboard(product))


@router.callback_query(F.data.startswith("admin:survey_del:"))
async def cb_survey_del(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    parts = callback.data.split(":")   # admin : survey_del : qid : pid
    qid, pid = int(parts[2]), int(parts[3])
    await db.delete_product_question(qid)
    await _render_survey(callback, pid)
    await callback.answer("Удалено")


@router.callback_query(F.data.startswith("admin:survey_up:"))
async def cb_survey_up(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    parts = callback.data.split(":")
    qid, pid = int(parts[2]), int(parts[3])
    await db.move_product_question(qid, -1)
    await _render_survey(callback, pid)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:survey_down:"))
async def cb_survey_down(callback: CallbackQuery):
    if not await admin_only(callback):
        return
    parts = callback.data.split(":")
    qid, pid = int(parts[2]), int(parts[3])
    await db.move_product_question(qid, 1)
    await _render_survey(callback, pid)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:survey_repeat:"))
async def cb_survey_repeat(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    pid = int(callback.data.split(":")[2])
    product = await db.get_product(pid)
    current = product.get("survey_repeat_text") if product else None
    now = f"\n\nСейчас: <i>{current}</i>" if current else "\n\n<i>Пока не задан.</i>"
    await state.set_state(SurveyRepeat.waiting_text)
    await state.update_data(product_id=pid)
    await callback.message.edit_text(
        "🔁 <b>Вопрос про дополнительную позицию</b>\n\n"
        "Задаётся в конце опроса. Если клиент нажмёт «Добавить ещё» — опрос "
        "пройдёт заново для следующей позиции, а к оплате добавится ещё одна цена."
        f"{now}\n\n"
        "Пришли текст вопроса (например: <i>Вам нужен один микрофон или добавим ещё?</i>).\n"
        "Отправь <code>-</code> — чтобы отключить.",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(pid),
    )
    await callback.answer()


@router.message(SurveyRepeat.waiting_text)
async def fsm_survey_repeat(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or message.caption or "").strip()
    data = await state.get_data()
    pid = data["product_id"]
    if text == "-":
        await db.set_survey_repeat_text(pid, None)
        note = "🔁 Доп. позиция отключена."
    elif not text:
        await message.answer("Пусто. Пришли текст вопроса или <code>-</code> для отключения.",
                             parse_mode="HTML")
        return
    else:
        await db.set_survey_repeat_text(pid, text)
        note = "✅ Сохранено."
    await state.clear()
    product = await db.get_product(pid)
    questions = await db.get_product_questions(pid)
    await message.answer(note, reply_markup=product_submenu_survey(product, questions))


@router.callback_query(F.data.startswith("admin:survey_delivery:"))
async def cb_survey_delivery(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    pid = int(callback.data.split(":")[2])
    product = await db.get_product(pid)
    current = product.get("survey_delivery_text") if product else None
    now = f"\n\nСейчас:\n<i>{current}</i>" if current else "\n\n<i>Пока не задан.</i>"
    await state.set_state(SurveyDelivery.waiting_text)
    await state.update_data(product_id=pid)

    if config.CDEK_ENABLED:
        head = (
            "🚚 <b>Доставка: работает СДЭК, до пункта выдачи</b>\n\n"
            "После опроса бот сам спрашивает город, ищет ближайшие ПВЗ по геолокации "
            "либо по введённой улице, считает стоимость по тарифу СДЭК и добавляет её "
            "к сумме заказа. Адрес и код ПВЗ приходят вам вместе с заявкой.\n\n"
            "Курьерская доставка до двери отключена.\n\n"
            f"Отправка из города: <b>{config.CDEK_FROM_CITY}</b>\n"
            f"Тариф: <b>{config.CDEK_TARIFF_PVZ}</b> (склад — склад)\n\n"
            "<b>Текстовый блок ниже сейчас не используется.</b> Он включится сам, "
            "только если отключить СДЭК в настройках сервера — держите его как запасной."
            f"{now}\n\n"
            "Изменить запасной текст — пришлите его сообщением.\n"
            "Отправьте <code>-</code>, чтобы стереть."
        )
    else:
        head = (
            "🚚 <b>Блок про доставку</b>\n\n"
            "Задаётся один раз в самом конце опроса (после всех позиций). "
            "Клиент присылает свои данные одним сообщением, они сохраняются как адрес "
            "доставки и приходят вам вместе с заявкой."
            f"{now}\n\n"
            "Пришли текст блока (например, с просьбой указать ФИО, телефон и ПВЗ).\n"
            "Отправь <code>-</code> — чтобы отключить."
        )

    await callback.message.edit_text(
        head,
        parse_mode="HTML",
        reply_markup=product_back_keyboard(pid),
    )
    await callback.answer()


@router.message(SurveyDelivery.waiting_text)
async def fsm_survey_delivery(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or message.caption or "").strip()
    data = await state.get_data()
    pid = data["product_id"]
    if text == "-":
        await db.set_survey_delivery_text(pid, None)
        note = "🚚 Блок доставки отключён."
    elif not text:
        await message.answer("Пусто. Пришли текст блока или <code>-</code> для отключения.",
                             parse_mode="HTML")
        return
    else:
        await db.set_survey_delivery_text(pid, text)
        note = "✅ Сохранено."
    await state.clear()
    product = await db.get_product(pid)
    await message.answer(note, reply_markup=admin_product_keyboard(product))


# --- Пуш отзыва ---

@router.callback_query(F.data.startswith("admin:set_review_push:"))
async def cb_set_review_push(callback: CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    product_id = int(callback.data.split(":")[2])
    product = await db.get_product(product_id)

    delay = product.get("review_push_delay")
    text = product.get("review_push_text")

    if delay:
        if delay < 3600:
            delay_str = f"{delay // 60} мин"
        elif delay < 86400:
            delay_str = f"{delay // 3600} ч"
        else:
            delay_str = f"{delay // 86400} д"
        current = f"\n\nСейчас: через <b>{delay_str}</b>\nТекст: {text or '<i>по умолчанию</i>'}"
    else:
        current = "\n\n<i>Пуш не настроен</i>"

    await state.set_state(SetReviewPush.waiting_delay)
    await state.update_data(product_id=product_id)
    await callback.message.edit_text(
        f"⭐ <b>Пуш с просьбой оставить отзыв</b>{current}\n\n"
        "Через сколько времени после покупки отправить сообщение?\n"
        "<i>Примеры: <code>30m</code> — 30 минут, <code>2h</code> — 2 часа, "
        "<code>3d</code> — 3 дня</i>\n\n"
        "Отправь <code>0</code> — чтобы отключить пуш.",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(product_id),
    )
    await callback.answer()


@router.message(SetReviewPush.waiting_delay)
async def fsm_review_push_delay(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    raw = message.text.strip().lower()
    data = await state.get_data()

    if raw == "0":
        await db.set_review_push(data["product_id"], None, None)
        await state.clear()
        product = await db.get_product(data["product_id"])
        purchase_count = await db.get_product_purchase_count(data["product_id"])
        await message.answer(
            "✅ Пуш отзыва отключён.",
            reply_markup=admin_product_keyboard(product, purchase_count),
        )
        return

    # Парсим: 30m / 2h / 3d / число (как минуты)
    try:
        if raw.endswith("m"):
            seconds = int(raw[:-1]) * 60
        elif raw.endswith("h"):
            seconds = int(raw[:-1]) * 3600
        elif raw.endswith("d"):
            seconds = int(raw[:-1]) * 86400
        else:
            seconds = int(raw) * 60  # просто число — как минуты
        if seconds <= 0:
            raise ValueError
    except (ValueError, IndexError):
        await message.answer(
            "Не понял формат. Попробуй: <code>30m</code>, <code>2h</code>, <code>3d</code>",
            parse_mode="HTML",
        )
        return

    await state.update_data(delay_seconds=seconds)
    await state.set_state(SetReviewPush.waiting_text)

    if seconds < 3600:
        delay_label = f"{seconds // 60} мин"
    elif seconds < 86400:
        delay_label = f"{seconds // 3600} ч"
    else:
        delay_label = f"{seconds // 86400} д"

    await message.answer(
        f"⏱ Задержка: <b>{delay_label}</b>\n\n"
        "Теперь введи текст сообщения, которое получит покупатель.\n"
        "Или отправь <code>-</code> — чтобы использовать стандартный текст.",
        parse_mode="HTML",
        reply_markup=product_back_keyboard(data["product_id"]),
    )


@router.message(SetReviewPush.waiting_text)
async def fsm_review_push_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    product_id = data["product_id"]
    seconds = data["delay_seconds"]
    text = None if message.text.strip() == "-" else message.text.strip()

    await db.set_review_push(product_id, seconds, text)
    await state.clear()

    if seconds < 3600:
        delay_label = f"{seconds // 60} мин"
    elif seconds < 86400:
        delay_label = f"{seconds // 3600} ч"
    else:
        delay_label = f"{seconds // 86400} д"

    product = await db.get_product(product_id)
    purchase_count = await db.get_product_purchase_count(product_id)
    await message.answer(
        f"✅ Пуш отзыва настроен!\n\n"
        f"Через <b>{delay_label}</b> после покупки покупатель получит сообщение с просьбой оставить отзыв.",
        parse_mode="HTML",
        reply_markup=admin_product_keyboard(product, purchase_count),
    )


@router.callback_query(F.data.startswith("admin:funnel_analytics:"))
async def cb_funnel_analytics(callback: CallbackQuery):
    if not await admin_only(callback):
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
