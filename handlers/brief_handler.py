"""
Обработчик ТЗ для физических товаров.
Пользователь заполняет свободный текст — он уходит в админский чат.
"""
import json
import logging
from datetime import datetime

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, Message,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto,
)

import config
import database as db
from services.prodamus import build_payment_url
from keyboards.user import back_to_catalog_keyboard
from handlers.delivery import start_delivery_flow, _goods_breakdown, _goods_payment_name


def _payment_keyboard(product: dict, user_id: int, quantity: int = 1,
                      total: int | None = None, name: str | None = None) -> InlineKeyboardMarkup | None:
    """Кнопка оплаты физического товара после заполнения ТЗ (order_type='p').
    quantity — число позиций (клиент мог добавить ещё через повтор опроса).
    total/name — переопределение суммы/названия для смешанного заказа
    (несколько разных товаров), иначе считаются по одному product."""
    # Физические товары — отдельный магазин Prodamus
    if not (config.PRODAMUS_SHOP_URL_PHYSICAL and user_id and product):
        return None
    quantity = max(1, quantity)
    if total is None:
        total = product["price"] * quantity
    if name is None:
        name = product["name"] if quantity == 1 else f"{product['name']} ×{quantity}"
    url = build_payment_url(
        shop_url=config.PRODAMUS_SHOP_URL_PHYSICAL,
        product_name=name,
        price=total,
        user_id=user_id,
        product_id=product["id"],
        order_type="p",
        secret=config.PRODAMUS_SECRET_PHYSICAL,
        notification_url=config.PRODAMUS_WEBHOOK_URL_PHYSICAL,
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {total} ₽", url=url)],
    ])


async def _finish_with_payment(message: Message, product: dict, user_id: int,
                               thanks: str, quantity: int = 1,
                               total: int | None = None, name: str | None = None):
    """Завершает ТЗ: благодарит и, если возможно, даёт ссылку на оплату."""
    pay_kb = _payment_keyboard(product, user_id, quantity, total=total, name=name)
    qty_note = f"\nПозиций в заказе: <b>{quantity}</b>." if quantity > 1 else ""
    if pay_kb:
        await message.answer(
            f"{thanks}{qty_note}\n\n"
            "Осталось оплатить заказ — нажми кнопку ниже 👇\n"
            "После оплаты в течение нескольких рабочих дней я отправлю вам заказ.",
            parse_mode="HTML",
            reply_markup=pay_kb,
        )
    else:
        await message.answer(
            f"{thanks} Я свяжусь с тобой в ближайшее время.",
            reply_markup=back_to_catalog_keyboard(),
        )

router = Router()
logger = logging.getLogger(__name__)


class BriefForm(StatesGroup):
    waiting_brief = State()


class SurveyForm(StatesGroup):
    answering = State()
    repeat_choice = State()
    picking_product = State()
    picking_variant = State()
    delivery = State()


# Подпись ответа «какой экземпляр из наличия выбран» в опросе и в заказе
VARIANT_Q = "Вариант из наличия"


def _repeat_keyboard(has_delivery: bool = False, has_other: bool = False) -> InlineKeyboardMarkup:
    # Следующий шаг после «Достаточно»: доставка (если настроена) или сразу оплата
    done_text = "✅ Достаточно, к доставке" if has_delivery else "✅ Достаточно, к оплате"
    rows = [[InlineKeyboardButton(text="➕ Ещё один такой же", callback_data="survey_more:add")]]
    if has_other:
        rows.append([InlineKeyboardButton(text="🆕 Добавить другой товар", callback_data="survey_more:other")])
    rows.append([InlineKeyboardButton(text=done_text, callback_data="survey_more:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _other_physical_products(exclude_ids) -> list[dict]:
    """Активные физтовары, ещё не добавленные в этот заказ (без распроданных)."""
    exclude_ids = set(exclude_ids)
    products = await db.get_all_products(active_only=True)
    result = []
    for p in products:
        if p.get("category") != "physical" or p["id"] in exclude_ids:
            continue
        if await db.get_product_variants(p["id"]) and not await db.get_product_variants(
                p["id"], in_stock_only=True):
            continue
        result.append(p)
    return result


async def _available_variants(product_id: int, rounds: list) -> list[dict]:
    """Варианты в наличии за вычетом уже выбранных в этом же заказе."""
    taken: dict[int, int] = {}
    for answers in rounds:
        for a in answers:
            if a.get("variant_id"):
                taken[a["variant_id"]] = taken.get(a["variant_id"], 0) + 1
    return [v for v in await db.get_product_variants(product_id, in_stock_only=True)
            if v["stock"] > taken.get(v["id"], 0)]


def _variant_keyboard(variants: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=v["name"], callback_data=f"survey_variant:{v['id']}")]
            for v in variants]
    rows.append([InlineKeyboardButton(text="◀️ К каталогу", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _start_round(target: Message, state: FSMContext, product_id: int) -> bool:
    """Первый шаг раунда: выбор варианта из наличия (если у товара они есть),
    иначе первый вопрос. False — у товара есть варианты, но все разобраны."""
    if await db.get_product_variants(product_id):
        data = await state.get_data()
        variants = await _available_variants(product_id, data.get("rounds") or [])
        if not variants:
            return False
        await state.set_state(SurveyForm.picking_variant)
        await target.answer("Выберите вариант из наличия 👇",
                            reply_markup=_variant_keyboard(variants))
        return True
    questions = await db.get_product_questions(product_id)
    if questions:
        await state.set_state(SurveyForm.answering)
        await _ask_question(target, questions[0], 1, len(questions))
    return True


def _product_picker_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{p['name']} — {p['price']} ₽",
                                  callback_data=f"survey_pick:{p['id']}")]
            for p in products]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="survey_pick:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ask_question(message: Message, question: dict, num: int, total: int):
    caption = f"<b>Вопрос {num}/{total}:</b>\n{question['text']}"
    photos = db.question_photos(question)
    if len(photos) > 1:
        # альбом: подпись-вопрос на первом фото
        media = [InputMediaPhoto(media=photos[0], caption=caption, parse_mode="HTML")]
        media += [InputMediaPhoto(media=p) for p in photos[1:10]]
        await message.answer_media_group(media)
    elif len(photos) == 1:
        await message.answer_photo(photos[0], caption=caption, parse_mode="HTML")
    else:
        await message.answer(caption, parse_mode="HTML")


async def start_order_flow(target: Message, state: FSMContext, product: dict):
    """Запускает оформление заказа: опрос по вопросам или свободное ТЗ.

    Вызывается прямо из карточки товара, поэтому название и цену повторно
    не выводим — они уже в карточке над этим сообщением.
    """
    product_id = product["id"]
    questions = await db.get_product_questions(product_id)
    has_variants = bool(await db.get_product_variants(product_id))
    await state.clear()

    # Настроен опрос (или выбор из наличия) — задаём вопросы по одному
    if questions or has_variants:
        await state.update_data(product_id=product_id, q_index=0, rounds=[[]],
                                round_products=[product_id], cur_product_id=product_id)
        if not await _start_round(target, state, product_id):
            await state.clear()
            await target.answer("😔 Всё разобрали — в наличии ничего не осталось.",
                                reply_markup=back_to_catalog_keyboard())
        return

    # Опрос не настроен — свободное ТЗ (как было)
    await state.set_state(BriefForm.waiting_brief)
    await state.update_data(product_id=product_id)
    await target.answer(
        "Опиши свой запрос: что именно тебе нужно, пожелания, размеры, цвет, контакты — "
        "всё, что поможет мне подготовить предложение.\n\n"
        "Можно написать всё одним сообщением или прикрепить фото.",
    )


@router.callback_query(F.data.startswith("brief:"))
async def cb_brief_start(callback: CallbackQuery, state: FSMContext):
    """Оставлен для старых сообщений с кнопкой «Оформить заказ» в истории чата."""
    product_id = int(callback.data.split(":")[1])
    product = await db.get_product(product_id)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    await start_order_flow(callback.message, state, product)
    await callback.answer()


@router.message(SurveyForm.answering)
async def fsm_survey_answer(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    anchor_id = data["product_id"]
    # Товар ТЕКУЩЕГО раунда: тот же анкорный, либо другой — если выбран
    # через «Добавить другой товар»
    product_id = data.get("cur_product_id", anchor_id)
    idx = data.get("q_index", 0)
    rounds = data.get("rounds") or [[]]

    questions = await db.get_product_questions(product_id)
    if not questions or idx >= len(questions):
        await state.clear()
        await message.answer(
            "Что-то пошло не так с опросом. Попробуй ещё раз из каталога.",
            reply_markup=back_to_catalog_keyboard(),
        )
        return

    text = (message.text or message.caption or "").strip()
    photo_id = message.photo[-1].file_id if message.photo else None
    doc_id = message.document.file_id if message.document else None

    if not text and not photo_id and not doc_id:
        await message.answer("Пусто 🙂 Напиши ответ текстом или пришли фото.")
        return

    rounds[-1].append({
        "q": questions[idx]["text"],
        "text": text,
        "photo": photo_id,
        "doc": doc_id,
    })
    idx += 1

    # Ещё есть вопросы в текущем круге
    if idx < len(questions):
        await state.update_data(q_index=idx, rounds=rounds)
        await _ask_question(message, questions[idx], idx + 1, len(questions))
        return

    await state.update_data(rounds=rounds)
    await _round_done(message, state, bot, message.from_user)


async def _round_done(target: Message, state: FSMContext, bot: Bot, user):
    """Круг вопросов пройден — предложить добавить ещё позицию (если настроено).
    Решает и говорит анкорный товар — так формулировка не меняется
    посреди заказа из-за другого товара в последнем раунде."""
    data = await state.get_data()
    anchor_id = data["product_id"]
    rounds = data.get("rounds") or [[]]
    anchor_product = await db.get_product(anchor_id)
    repeat_text = anchor_product.get("survey_repeat_text") if anchor_product else None
    if repeat_text:
        await state.set_state(SurveyForm.repeat_choice)
        has_delivery = bool(anchor_product.get("survey_delivery_text")) if anchor_product else False
        round_products = data.get("round_products") or [anchor_id] * len(rounds)
        has_other = bool(await _other_physical_products(round_products))
        await target.answer(repeat_text, reply_markup=_repeat_keyboard(has_delivery, has_other))
        return

    await _finish_survey(target, state, bot, user, anchor_id, rounds)


@router.callback_query(SurveyForm.picking_variant, F.data.startswith("survey_variant:"))
async def cb_survey_variant(callback: CallbackQuery, state: FSMContext, bot: Bot):
    variant_id = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    product_id = data.get("cur_product_id", data["product_id"])
    rounds = data.get("rounds") or [[]]

    # Пока клиент думал, вариант мог уйти другому покупателю
    variants = await _available_variants(product_id, rounds)
    variant = next((v for v in variants if v["id"] == variant_id), None)
    if not variant:
        await callback.answer("Этот вариант уже разобрали 😔", show_alert=True)
        if variants:
            await callback.message.answer("Выберите вариант из наличия 👇",
                                          reply_markup=_variant_keyboard(variants))
        else:
            await state.clear()
            await callback.message.answer("😔 Всё разобрали — в наличии ничего не осталось.",
                                          reply_markup=back_to_catalog_keyboard())
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(f"✅ {variant['name']}")

    rounds[-1].append({"q": VARIANT_Q, "text": variant["name"], "variant_id": variant["id"],
                       "photo": None, "doc": None})
    await state.update_data(rounds=rounds, q_index=0)
    questions = await db.get_product_questions(product_id)
    if questions:
        await state.set_state(SurveyForm.answering)
        await _ask_question(callback.message, questions[0], 1, len(questions))
        return
    await _round_done(callback.message, state, bot, callback.from_user)


@router.message(SurveyForm.picking_variant)
async def fsm_variant_freetext(message: Message, state: FSMContext):
    data = await state.get_data()
    product_id = data.get("cur_product_id", data["product_id"])
    variants = await _available_variants(product_id, data.get("rounds") or [])
    if not variants:
        await state.clear()
        await message.answer("😔 Всё разобрали — в наличии ничего не осталось.",
                             reply_markup=back_to_catalog_keyboard())
        return
    await message.answer("Выберите вариант кнопкой 👇", reply_markup=_variant_keyboard(variants))


async def _sold_out_back(target: Message, state: FSMContext):
    """Повтор позиции не удался — все варианты разобраны: снимаем пустой раунд
    и возвращаем клиента к выбору «ещё / достаточно»."""
    data = await state.get_data()
    anchor_id = data["product_id"]
    rounds = (data.get("rounds") or [[]])[:-1] or [[]]
    round_products = (data.get("round_products") or [anchor_id])[:len(rounds)]
    anchor_product = await db.get_product(anchor_id)
    has_delivery = bool(anchor_product.get("survey_delivery_text")) if anchor_product else False
    has_other = bool(await _other_physical_products(round_products))
    await state.set_state(SurveyForm.repeat_choice)
    await state.update_data(rounds=rounds, round_products=round_products,
                            cur_product_id=round_products[-1])
    await target.answer("😔 Больше вариантов в наличии не осталось.",
                        reply_markup=_repeat_keyboard(has_delivery, has_other))


@router.callback_query(SurveyForm.repeat_choice, F.data.startswith("survey_more:"))
async def cb_survey_more(callback: CallbackQuery, state: FSMContext, bot: Bot):
    action = callback.data.split(":")[1]
    data = await state.get_data()
    anchor_id = data["product_id"]
    rounds = data.get("rounds") or [[]]
    round_products = data.get("round_products") or [anchor_id] * len(rounds)
    await callback.answer()

    if action == "add":
        # Повторяем ТОТ товар, чей раунд отвечали последним
        cur_id = round_products[-1] if round_products else anchor_id
        rounds.append([])
        round_products.append(cur_id)
        await state.update_data(rounds=rounds, round_products=round_products,
                                q_index=0, cur_product_id=cur_id)
        if not await _available_variants(cur_id, rounds) and await db.get_product_variants(cur_id):
            await _sold_out_back(callback.message, state)
            return
        await callback.message.answer(
            f"➕ <b>Позиция {len(rounds)}</b> — ответь на те же вопросы ещё раз.",
            parse_mode="HTML",
        )
        await _start_round(callback.message, state, cur_id)
        return

    if action == "other":
        others = await _other_physical_products(round_products)
        if not others:
            await callback.answer("Других физических товаров пока нет.", show_alert=True)
            return
        await state.set_state(SurveyForm.picking_product)
        await callback.message.answer(
            "Выбери товар, который добавить в заказ:",
            reply_markup=_product_picker_keyboard(others),
        )
        return

    # «Достаточно»
    await _finish_survey(callback.message, state, bot, callback.from_user, anchor_id, rounds)


@router.callback_query(SurveyForm.picking_product, F.data.startswith("survey_pick:"))
async def cb_survey_pick_product(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    choice = callback.data.split(":", 1)[1]
    data = await state.get_data()
    anchor_id = data["product_id"]
    rounds = data.get("rounds") or [[]]
    round_products = data.get("round_products") or [anchor_id] * len(rounds)

    if choice == "cancel":
        anchor_product = await db.get_product(anchor_id)
        has_delivery = bool(anchor_product.get("survey_delivery_text")) if anchor_product else False
        has_other = bool(await _other_physical_products(round_products))
        await state.set_state(SurveyForm.repeat_choice)
        await callback.message.answer("Хорошо, остаёмся здесь 👇",
                                      reply_markup=_repeat_keyboard(has_delivery, has_other))
        return

    new_id = int(choice)
    new_product = await db.get_product(new_id)
    if not new_product:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    rounds.append([])
    round_products.append(new_id)
    await state.update_data(rounds=rounds, round_products=round_products,
                            q_index=0, cur_product_id=new_id)
    questions = await db.get_product_questions(new_id)
    if await db.get_product_variants(new_id):
        if not await _available_variants(new_id, rounds):
            await _sold_out_back(callback.message, state)
            return
        await callback.message.answer(
            f"🆕 <b>Позиция {len(rounds)}: {new_product['name']}</b>",
            parse_mode="HTML",
        )
        await _start_round(callback.message, state, new_id)
        return
    await callback.message.answer(
        f"🆕 <b>Позиция {len(rounds)}: {new_product['name']}</b> — ответь на вопросы этого товара.",
        parse_mode="HTML",
    )
    if questions:
        await state.set_state(SurveyForm.answering)
        await _ask_question(callback.message, questions[0], 1, len(questions))
        return

    # У добавленного товара нет своего опроса — сразу возвращаемся к выбору
    rounds[-1] = []
    anchor_product = await db.get_product(anchor_id)
    has_delivery = bool(anchor_product.get("survey_delivery_text")) if anchor_product else False
    has_other = bool(await _other_physical_products(round_products))
    await state.set_state(SurveyForm.repeat_choice)
    await state.update_data(rounds=rounds, round_products=round_products)
    await callback.message.answer(
        f"У «{new_product['name']}» не настроен опрос — добавил без вопросов.",
        reply_markup=_repeat_keyboard(has_delivery, has_other),
    )


@router.message(SurveyForm.repeat_choice)
async def fsm_repeat_freetext(message: Message, state: FSMContext, bot: Bot):
    """Если вместо кнопки клиент пишет текстом — понимаем «ещё» / «достаточно»."""
    t = (message.text or "").strip().lower()
    add_words = ("ещё", "еще", "добав", "да", "+", "друг")
    done_words = ("один", "хватит", "достаточно", "нет", "всё", "все", "не надо")
    data = await state.get_data()
    anchor_id = data["product_id"]
    rounds = data.get("rounds") or [[]]
    round_products = data.get("round_products") or [anchor_id] * len(rounds)

    if any(w in t for w in add_words):
        cur_id = round_products[-1] if round_products else anchor_id
        rounds.append([])
        round_products.append(cur_id)
        await state.update_data(rounds=rounds, round_products=round_products,
                                q_index=0, cur_product_id=cur_id)
        if not await _available_variants(cur_id, rounds) and await db.get_product_variants(cur_id):
            await _sold_out_back(message, state)
            return
        await message.answer(
            f"➕ <b>Позиция {len(rounds)}</b> — ответь на те же вопросы ещё раз.",
            parse_mode="HTML",
        )
        await _start_round(message, state, cur_id)
    elif any(w in t for w in done_words):
        await _finish_survey(message, state, bot, message.from_user, anchor_id, rounds)
    else:
        anchor_product = await db.get_product(anchor_id)
        has_delivery = bool(anchor_product.get("survey_delivery_text")) if anchor_product else False
        has_other = bool(await _other_physical_products(round_products))
        await message.answer("Выбери кнопкой 👇", reply_markup=_repeat_keyboard(has_delivery, has_other))


async def _finish_survey(target: Message, state: FSMContext, bot: Bot,
                         user, product_id: int, rounds: list):
    """Позиции собраны. Если настроен блок доставки — спрашиваем его,
    иначе сразу завершаем заказ."""
    product = await db.get_product(product_id)
    data = await state.get_data()
    round_products = data.get("round_products") or [product_id] * len(rounds)

    # Если настроена служба доставки (СДЭК / Яндекс) — считаем стоимость,
    # адрес собирается по шагам в handlers/delivery.py
    if await start_delivery_flow(target, state, product_id,
                                 quantity=len(rounds), rounds=rounds,
                                 round_products=round_products):
        return

    delivery_text = product.get("survey_delivery_text") if product else None
    if delivery_text:
        await state.set_state(SurveyForm.delivery)
        await state.update_data(product_id=product_id, rounds=rounds,
                                round_products=round_products)
        await target.answer(delivery_text)
        return
    await _complete_order(target, state, bot, user, product_id, rounds, None)


@router.message(SurveyForm.delivery)
async def fsm_survey_delivery_answer(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    product_id = data["product_id"]
    rounds = data.get("rounds") or [[]]
    delivery_str = (message.text or message.caption or "").strip()
    if not delivery_str:
        await message.answer(
            "Пожалуйста, напиши данные текстом: ФИО, телефон и адрес ПВЗ."
        )
        return
    await _complete_order(message, state, bot, message.from_user, product_id, rounds, delivery_str)


async def _complete_order(target: Message, state: FSMContext, bot: Bot,
                          user, product_id: int, rounds: list, delivery_str: str | None):
    data = await state.get_data()
    round_products = data.get("round_products") or [product_id] * len(rounds)
    await state.clear()
    product = await db.get_product(product_id)

    products_by_id = {pid: await db.get_product(pid) for pid in set(round_products)}
    products_by_id.setdefault(product_id, product)
    goods_items = _goods_breakdown(round_products, products_by_id)
    total = sum(p["price"] * qty for p, qty in goods_items)
    payment_name = _goods_payment_name(goods_items)

    # Ответы и адрес сохраняем до оплаты — уведомление «Новый заказ» уйдёт
    # админу из вебхука, строго после успешной оплаты.
    await db.save_pending_delivery(
        user.id, product_id,
        delivery_str or "не указан",
        survey_json=json.dumps(rounds, ensure_ascii=False),
        amount=total,
        round_products_json=json.dumps(round_products, ensure_ascii=False),
    )
    await _finish_with_payment(
        target, product, user.id,
        thanks="✅ Заказ оформлен!",
        quantity=len(rounds),
        total=total,
        name=payment_name,
    )


@router.message(BriefForm.waiting_brief)
async def fsm_receive_brief(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    product_id = data["product_id"]
    product = await db.get_product(product_id)
    await state.clear()

    user = message.from_user
    username_str = f"@{user.username}" if user.username else f"id:{user.id}"
    first_name = user.first_name or "—"
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    product_name = product["name"] if product else f"id:{product_id}"

    notify_text = (
        f"📋 <b>Новое ТЗ</b>\n\n"
        f"👤 {first_name} {username_str}\n"
        f"🛍 {product_name}\n"
        f"🕐 {now}\n\n"
        f"<b>Сообщение:</b>\n{message.text or '(без текста)'}"
    )

    # Пересылаем в админ-чат через notify-бот
    try:
        notify_bot = Bot(token=config.WAITLIST_BOT_TOKEN)
        for admin_id in config.ADMIN_IDS:
            try:
                await notify_bot.send_message(admin_id, notify_text, parse_mode="HTML")
                # Если есть фото/файл — пересылаем оригинал
                if message.photo or message.document:
                    await message.forward(chat_id=admin_id)
            except Exception as e:
                logger.error(f"Brief notify to {admin_id} failed: {e}")
        await notify_bot.session.close()
    except Exception as e:
        logger.error(f"Failed to send brief notification: {e}")

    await _finish_with_payment(
        message, product, user.id,
        thanks="✅ Запрос получен!",
    )
