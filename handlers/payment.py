import logging

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, Message,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import config
from keyboards.user import back_to_catalog_keyboard
import database as db

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery):
    """Фолбэк — срабатывает только если PRODAMUS_SHOP_URL не задан."""
    await callback.answer("Оплата временно недоступна. Напиши мне в личку.", show_alert=True)


@router.callback_query(F.data.startswith("check_payment:"))
async def cb_check_payment(callback: CallbackQuery, bot: Bot):
    product_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    product = await db.get_product(product_id)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    purchase = await db.get_purchase(user_id, product_id)
    if not purchase:
        await callback.answer(
            "Оплата не найдена. Если ты только что оплатил — подожди минуту и попробуй снова.",
            show_alert=True,
        )
        return

    await callback.answer("✅ Оплата найдена! Отправляю товар…")

    if not product.get("file_id"):
        await callback.message.answer("Файл недоступен. Напиши мне в личку.")
        return

    await bot.send_document(
        chat_id=user_id,
        document=product["file_id"],
        caption=f"<b>{product['name']}</b> — пресет",
        parse_mode="HTML",
    )

    if product.get("instruction_file_id"):
        if product.get("instruction_type") == "photo":
            await bot.send_photo(
                chat_id=user_id,
                photo=product["instruction_file_id"],
                caption="📄 <b>Инструкция по применению</b>",
                parse_mode="HTML",
            )
        else:
            await bot.send_document(
                chat_id=user_id,
                document=product["instruction_file_id"],
                caption="📄 <b>Инструкция по применению</b>",
                parse_mode="HTML",
            )

    if product.get("video_url"):
        await callback.message.answer(
            f"🎬 <b>Видео-урок:</b> {product['video_url']}",
            parse_mode="HTML",
        )

    await callback.message.answer("Удачи! 🌟", reply_markup=back_to_catalog_keyboard())


# ── Тест покупки (только для админов) ────────────────────────────────────────

class SimState(StatesGroup):
    waiting_num = State()


def _infobiz_preview(product: dict, purchase_num: int, purchase_count: int) -> str:
    """Что сработает при данном номере покупки."""
    lines = [
        f"🧪 <b>Тест: успешная оплата — {product['name']}</b>\n",
        f"📊 Реальных покупок: <b>{purchase_count}</b>",
        f"🔢 Симулируем покупку №<b>{purchase_num}</b>\n",
        "<b>Что получит покупатель:</b>",
    ]

    if product.get("file_id"):
        lines.append("  📎 Файл с материалами — ✅")
    else:
        lines.append("  📎 Файл — <b>не загружен</b> ⚠️")

    if product.get("channel_id") and product.get("channel_invite_link"):
        lines.append("  📢 Приглашение в закрытый канал — ✅")
    else:
        lines.append("  📢 Канал — <b>не настроен</b> ⚠️")

    trigger = product.get("price_trigger")
    after = product.get("price_after_trigger")
    if trigger and after:
        if purchase_num > trigger:
            lines.append(f"  💲 Цена уже <b>{after} ₽</b> (триггер {trigger} пройден)")
        else:
            lines.append(
                f"  💲 Цена <b>{product['price']} ₽</b> "
                f"(триггер {trigger} ещё не пройден, осталось {trigger - purchase_num + 1})"
            )

    bonus_limit = product.get("bonus_limit")
    if bonus_limit:
        if purchase_num <= bonus_limit:
            lines.append(
                f"  🎁 Бонус-разбор — <b>сработает</b> ✅ "
                f"(покупка #{purchase_num} из {bonus_limit})"
            )
        else:
            lines.append(
                f"  🎁 Бонус-разбор — <b>не сработает</b> "
                f"(лимит {bonus_limit} исчерпан)"
            )

    lines.append("\n<i>В базу ничего не запишется.</i>")
    return "\n".join(lines)


def _sim_confirm_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Запустить тест", callback_data=f"sim_run:{product_id}")],
        [InlineKeyboardButton(text="🔢 Изменить номер", callback_data=f"sim_success:{product_id}")],
        [InlineKeyboardButton(text="◀️ Назад к товару", callback_data=f"admin:product:{product_id}")],
    ])


@router.callback_query(F.data.startswith("sim_success:"))
async def cb_sim_success(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if callback.from_user.id not in config.ADMIN_IDS:
        return
    product_id = int(callback.data.split(":")[1])
    product = await db.get_product(product_id)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    # Для инфобиза — спрашиваем номер покупки
    if product.get("category") == "infobiz":
        purchase_count = await db.get_product_purchase_count(product_id)
        await state.set_state(SimState.waiting_num)
        await state.update_data(product_id=product_id)
        await callback.message.answer(
            f"🔢 Введи номер покупки для симуляции.\n\n"
            f"Реальных покупок сейчас: <b>{purchase_count}</b>\n\n"
            f"<i>Примеры: <code>1</code> — первый покупатель, "
            f"<code>{purchase_count + 1}</code> — следующий после реальных</i>",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    # Для остальных категорий — сразу запускаем
    await callback.answer("🧪 Симулирую…")
    await _run_sim_success(callback.message, bot, product, callback.from_user)


@router.message(SimState.waiting_num)
async def fsm_sim_num(message: Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    try:
        num = int(message.text.strip())
        if num < 1:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое число ≥ 1.")
        return

    data = await state.get_data()
    product_id = data["product_id"]
    await state.update_data(purchase_num=num)
    await state.set_state(SimState.waiting_num)  # остаёмся в состоянии до подтверждения

    product = await db.get_product(product_id)
    purchase_count = await db.get_product_purchase_count(product_id)

    await state.update_data(confirmed=False)
    await message.answer(
        _infobiz_preview(product, num, purchase_count),
        parse_mode="HTML",
        reply_markup=_sim_confirm_keyboard(product_id),
    )


@router.callback_query(F.data.startswith("sim_run:"), SimState.waiting_num)
async def cb_sim_run(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if callback.from_user.id not in config.ADMIN_IDS:
        return
    data = await state.get_data()
    product_id = data["product_id"]
    purchase_num = data.get("purchase_num", 1)
    product = await db.get_product(product_id)

    await state.clear()
    await callback.answer("🧪 Запускаю…")
    await _run_sim_success(
        callback.message, bot, product, callback.from_user,
        purchase_num=purchase_num,
    )


async def _run_sim_success(message, bot: Bot, product: dict, user, purchase_num: int = None):
    from handlers.prodamus_webhook import _deliver_digital, _deliver_infobiz
    category = product.get("category", "digital")
    user_id = user.id

    await message.answer(
        "🧪 <b>Тест: успешная оплата</b>\n\nНиже — то, что получит покупатель 👇",
        parse_mode="HTML",
    )

    try:
        if category == "infobiz":
            if purchase_num is None:
                purchase_num = await db.get_product_purchase_count(product["id"]) + 1
            await _deliver_infobiz(
                bot, user_id, product, purchase_num,
                username=user.username,
                first_name=user.first_name or "",
                test_mode=True,
            )
        elif category == "physical":
            # Ровно то же сообщение, что уходит после настоящей оплаты:
            # свой текст товара либо стандартный, с подставленным номером
            from handlers.prodamus_webhook import DEFAULT_POST_PAYMENT
            paid_text = product.get("post_payment_text") or DEFAULT_POST_PAYMENT
            paid_text = paid_text.replace("{order}", await db.peek_order_code())
            await bot.send_message(user_id, paid_text)
        else:
            await _deliver_digital(bot, user_id, product)
    except Exception as e:
        logger.error(f"sim_success error: {e}")
        await message.answer(f"⚠️ Ошибка: <code>{e}</code>", parse_mode="HTML")
        return

    await message.answer(
        "✅ <b>Тест завершён.</b> В базу ничего не записано.",
        parse_mode="HTML",
        reply_markup=back_to_catalog_keyboard(),
    )


@router.callback_query(F.data.startswith("sim_fail:"))
async def cb_sim_fail(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        return
    await callback.answer("❌ Симулирую неуспешную оплату…")
    await callback.message.answer(
        "🧪 <b>Тест: неуспешная оплата</b>\n\nПокупатель получит это сообщение 👇",
        parse_mode="HTML",
    )
    # Тот же текст и та же кнопка, что шлёт вебхук при отказе оплаты
    from handlers.prodamus_webhook import _back_to_catalog_keyboard
    await callback.message.answer(
        "😔 К сожалению, оплата не прошла.\n\n"
        "Попробуйте ещё раз — вернитесь в каталог и нажмите «Купить».",
        reply_markup=_back_to_catalog_keyboard(),
    )
