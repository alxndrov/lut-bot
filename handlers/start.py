import logging
from aiogram import BaseMiddleware, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)
from datetime import datetime

import config
from config import ADMIN_IDS
from keyboards.user import catalog_keyboard, back_to_catalog_keyboard
from keyboards.admin import admin_menu_keyboard
import database as db
from services.sellers import CONTACT, DANYA, MISHA

router = Router()
logger = logging.getLogger(__name__)


OFFER_BOX = "Принимаю оферту"
CONSENT_BOX = "Даю согласие на обработку персональных данных"
ON, OFF = "✅", "⬜️"


def policy_keyboard(offer_on: bool = False, consent_on: bool = False) -> InlineKeyboardMarkup:
    """Две отдельные галочки: оферта и согласие на обработку ПДн. Согласие по
    ч. 1 ст. 9 152-ФЗ (с 01.09.2025) оформляется отдельно от других документов,
    поэтому одной кнопкой «принимаю всё» его давать нельзя."""
    rows = []
    if config.OFFER_URL:
        rows.append([InlineKeyboardButton(text=f"{ON if offer_on else OFF} {OFFER_BOX}",
                                          callback_data="policy:t:offer")])
    rows.append([InlineKeyboardButton(text=f"{ON if consent_on else OFF} {CONSENT_BOX}",
                                      callback_data="policy:t:consent")])
    rows.append([InlineKeyboardButton(text="Дальше →", callback_data="policy:accept")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _boxes(markup: InlineKeyboardMarkup | None) -> dict | None:
    """Какие галочки отмечены — читаем из кнопок самого сообщения.
    None — старое сообщение без галочек (одна кнопка «Принимаю»)."""
    state = {}
    for row in (markup.inline_keyboard if markup else []):
        for b in row:
            if b.callback_data in ("policy:t:offer", "policy:t:consent"):
                state[b.callback_data.split(":")[2]] = b.text.startswith(ON)
    return state or None


async def ask_policy(message: Message, renewed: bool = False):
    """Первый экран для нового пользователя: без согласия дальше не пускаем.
    renewed — человек давал согласие прежнему оператору: просим новое."""
    items = []
    if config.OFFER_URL:
        items.append(f'• <a href="{config.OFFER_URL}">публичная оферта</a>')
    items.append(f'• <a href="{config.CONSENT_URL}">согласие на обработку персональных данных</a>')
    if renewed:
        text = ("Мы обновили документы: теперь у магазина два продавца и оператора "
                "персональных данных (указаны ниже).")
    else:
        text = ("Привет! Это бот MALIMABI: LUT для видео, онлайн-курс по съёмке и монтажу "
                "и изделия 3D-печати для съёмки: рукоятки для микрофонов, кейсы.")
    text += ("\n\nЧуть-чуть формальностей, без них никак. Отметь, пожалуйста, "
             + ("оба пункта" if len(items) > 1 else "пункт") + ":\n" + "\n".join(items))
    if config.PRIVACY_POLICY_URL != config.CONSENT_URL:
        text += (f'\n\nКак мы обрабатываем и защищаем данные — в '
                 f'<a href="{config.PRIVACY_POLICY_URL}">политике обработки персональных данных</a>.')
    text += ("\n\n<i>Продавцы: самозанятые " + MISHA["short"] + f" (ИНН {MISHA['inn']}) — "
             "цифровые товары и курс, " + DANYA["short"] + f" (ИНН {DANYA['inn']}) — изделия "
             f"3D-печати. Связь: {CONTACT}</i>")
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True,
                         reply_markup=policy_keyboard())


class PolicyGate(BaseMiddleware):
    """Без действующего согласия кнопки не работают — сначала экран согласия.
    Кто давал согласие по прежним документам (db.POLICY_RECONSENT_SETTING —
    дата, с которой согласие новое), видит «Мы обновили документы»; кто не
    давал или удалил данные через /forget — обычный экран."""

    async def __call__(self, handler, event: CallbackQuery, data):
        if (config.POLICY_REQUIRED and not (event.data or "").startswith(("policy:", "forget:"))
                and event.from_user.id not in ADMIN_IDS):
            status = await db.policy_status(event.from_user.id)
            if status != "ok":   # 'old' — давал прежнему оператору, 'none' — не давал или /forget
                await event.answer()
                await ask_policy(event.message, renewed=status == "old")
                return None
        return await handler(event, data)


async def ensure_policy(message: Message, payload: str | None = None) -> bool:
    """Без согласия на обработку данных дальше не пускаем: спрашиваем его,
    а payload откроется после «Принимаю». True — согласие уже есть."""
    await db.upsert_user(user_id=message.from_user.id, username=message.from_user.username,
                         first_name=message.from_user.first_name or "", ref=None)
    if not config.POLICY_REQUIRED:
        return True
    status = await db.policy_status(message.from_user.id)
    if status == "ok":
        return True
    await db.set_pending_payload(message.from_user.id, payload)
    await ask_policy(message, renewed=status == "old")
    return False


async def _start_funnel(user_id: int, payload: str | None):
    """Запускает воронку по deep-link payload (после согласия)."""
    if not payload or not payload.startswith("lm_"):
        return
    rest = payload[3:]
    if "_src_" in rest:
        funnel_id, source = rest.split("_src_", 1)
    else:
        funnel_id, source = rest, payload

    funnel = await db.get_funnel_by_slug(funnel_id) or await db.get_funnel(funnel_id)
    if not (funnel and funnel["active"]):
        logger.warning(f"deep link lm_{funnel_id}: воронка не найдена или неактивна")
        return
    if funnel.get("product_id") and await db.get_purchase(user_id, funnel["product_id"]):
        return
    await db.enqueue_funnel(user_id, funnel["id"], source=source)
    logger.info(f"funnel auto-start: user {user_id} → funnel {funnel['id']} source={source}")


@router.callback_query(F.data.startswith("policy:t:"))
async def cb_policy_toggle(callback: CallbackQuery):
    state = _boxes(callback.message.reply_markup) or {}
    which = callback.data.split(":")[2]
    state[which] = not state.get(which, False)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=policy_keyboard(
            offer_on=state.get("offer", False), consent_on=state.get("consent", False)))
    except Exception:
        pass


@router.callback_query(F.data == "policy:accept")
async def cb_policy_accept(callback: CallbackQuery):
    state = _boxes(callback.message.reply_markup)
    if state is None:   # старый экран с одной кнопкой — показываем новый, с галочками
        await callback.answer()
        await ask_policy(callback.message, renewed=await db.policy_status(callback.from_user.id) == "old")
        return
    if not all(state.values()):
        await callback.answer("Отметь, пожалуйста, "
                              + ("оба пункта" if len(state) > 1 else "пункт") + " 🙏",
                              show_alert=True)
        return
    payload = await db.accept_policy(callback.from_user.id)
    await callback.answer("Спасибо!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if payload == "course":
        from handlers.course import send_course
        await send_course(callback.message, callback.from_user.id)
        return
    if payload and payload.startswith("gift_"):
        from handlers import course_gift
        await course_gift.claim(callback.message, callback.from_user.id, payload[5:])
        return
    await _start_funnel(callback.from_user.id, payload)
    await show_catalog(callback.message, user_id=callback.from_user.id)


@router.message(CommandStart())
async def cmd_start(message: Message):
    # Парсим deep link: /start PAYLOAD
    # Форматы:
    #   src_reels_24apr       → просто отслеживание источника
    #   lm_FUNNELID           → запуск воронки (+ источник = lm_FUNNELID)
    #   lm_FUNNELID_src_NAME  → запуск воронки с явным названием источника
    args = message.text.split(None, 1)
    payload = args[1].strip() if len(args) > 1 else None
    ref = payload  # сохраняем как источник (first-touch)

    # Определяем: нужно ли автозапустить воронку
    funnel_to_start: str | None = None
    funnel_source: str | None = ref
    if payload and payload.startswith("lm_"):
        # lm_FUNNELID или lm_FUNNELID_src_NAME
        rest = payload[3:]  # убираем "lm_"
        if "_src_" in rest:
            funnel_id_part, src_part = rest.split("_src_", 1)
            funnel_to_start = funnel_id_part
            funnel_source = src_part
        else:
            funnel_to_start = rest
            funnel_source = payload  # весь payload как источник

    await db.upsert_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name or "",
        ref=ref,
    )

    # Без согласия на обработку данных ничего не показываем и воронку
    # не запускаем — payload дождётся принятия
    status = await db.policy_status(message.from_user.id) if config.POLICY_REQUIRED else "ok"
    if status != "ok":
        await db.set_pending_payload(message.from_user.id, payload)
        await ask_policy(message, renewed=status == "old")
        return

    if payload == "course":
        from handlers.course import send_course
        await send_course(message, message.from_user.id)
        return

    if payload and payload.startswith("gift_"):   # курс в подарок — handlers/course_gift.py
        from handlers import course_gift
        await course_gift.claim(message, message.from_user.id, payload[5:])
        return

    if funnel_to_start:
        await _start_funnel(message.from_user.id, payload)

    admin = message.from_user.id in ADMIN_IDS
    products = await db.get_catalog_products(include_hidden=admin)
    banner_file_id = await db.get_setting("catalog_banner_file_id")

    if not products:
        await message.answer("👋 Привет! Пока товаров нет, скоро появятся.")
        return

    admin_hint = "\n\n<i>🔐 Вы видите скрытые товары, т.к. вы администратор</i>" if admin else ""
    text = "👋 Привет! Выбери товар из каталога:" + admin_hint
    kb = catalog_keyboard(products, is_admin=admin,
                          reviews=await db.count_published_reviews())

    if banner_file_id:
        await message.answer_photo(banner_file_id, caption=text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("catalog"))
async def cmd_catalog(message: Message):
    await show_catalog(message)


@router.message(Command("mypurchases"))
async def cmd_mypurchases(message: Message):
    purchases = await db.get_user_purchases(message.from_user.id)

    if not purchases:
        await message.answer(
            "🛍 У тебя пока нет покупок.\n\n"
            "Загляни в /catalog!",
        )
        return

    lines = []
    for i, p in enumerate(purchases, 1):
        try:
            dt = datetime.strptime(p["created_at"], "%Y-%m-%d %H:%M:%S")
            date_str = dt.strftime("%d.%m.%Y")
        except Exception:
            date_str = p["created_at"][:10]

        category = p.get("category", "digital")
        icon = "🚚" if category == "physical" else "📦"
        lines.append(f"{i}. {icon} <b>{p['product_name']}</b> — {p['amount']} ₽  <i>{date_str}</i>")

    text = "🧾 <b>Ваши покупки:</b>\n\n" + "\n".join(lines)
    await message.answer(text, parse_mode="HTML", reply_markup=back_to_catalog_keyboard())


async def show_catalog(message_or_callback, edit=False, user_id: int | None = None):
    if user_id is None:
        user_id = getattr(
            getattr(message_or_callback, "from_user", None) or
            getattr(getattr(message_or_callback, "message", None), "from_user", None),
            "id", 0,
        )
    import config as _config
    admin = user_id in _config.ADMIN_IDS
    products = await db.get_catalog_products(include_hidden=admin)
    banner_file_id = await db.get_setting("catalog_banner_file_id")

    if not products:
        text = "😔 Товаров пока нет."
        kb = None
    else:
        admin_hint = "\n\n<i>🔐 Вы видите скрытые товары, т.к. вы администратор</i>" if admin else ""
        text = "📦 Каталог товаров:" + admin_hint
        kb = catalog_keyboard(products, is_admin=admin,
                              reviews=await db.count_published_reviews())

    if edit:
        msg = message_or_callback.message
        try:
            await msg.delete()
        except Exception:
            pass
        if banner_file_id:
            await msg.answer_photo(banner_file_id, caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        if banner_file_id:
            await message_or_callback.answer_photo(banner_file_id, caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await message_or_callback.answer(text, reply_markup=kb, parse_mode="HTML")
