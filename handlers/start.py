import logging
from aiogram import Router, F
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

router = Router()
logger = logging.getLogger(__name__)


def policy_keyboard() -> InlineKeyboardMarkup:
    """Одна кнопка — сами документы даны ссылками в тексте."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принимаю", callback_data="policy:accept")]
    ])


async def ask_policy(message: Message):
    """Первый экран для нового пользователя: без согласия дальше не пускаем."""
    policy = (f'<a href="{config.PRIVACY_POLICY_URL}">'
              "согласие на обработку персональных данных</a>")
    offer = (f' и <a href="{config.OFFER_URL}">оферту</a>'
             if config.OFFER_URL else "")
    await message.answer(
        "Привет!\n\n"
        f"Прежде чем я отправлю тебе всю информацию, пожалуйста, прими {policy}{offer}.",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=policy_keyboard(),
    )


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


@router.callback_query(F.data == "policy:accept")
async def cb_policy_accept(callback: CallbackQuery):
    payload = await db.accept_policy(callback.from_user.id)
    await callback.answer("Спасибо!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
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
    if config.POLICY_REQUIRED and not await db.is_policy_accepted(message.from_user.id):
        await db.set_pending_payload(message.from_user.id, payload)
        await ask_policy(message)
        return

    if funnel_to_start:
        await _start_funnel(message.from_user.id, payload)

    admin = message.from_user.id in ADMIN_IDS
    products = await db.get_all_products(active_only=not admin)
    banner_file_id = await db.get_setting("catalog_banner_file_id")

    if not products:
        await message.answer("👋 Привет! Пока товаров нет, скоро появятся.")
        return

    admin_hint = "\n\n<i>🔐 Вы видите скрытые товары, т.к. вы администратор</i>" if admin else ""
    text = "👋 Привет! Выбери товар из каталога:" + admin_hint
    kb = catalog_keyboard(products, is_admin=admin)

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
    products = await db.get_all_products(active_only=not admin)
    banner_file_id = await db.get_setting("catalog_banner_file_id")

    if not products:
        text = "😔 Товаров пока нет."
        kb = None
    else:
        admin_hint = "\n\n<i>🔐 Вы видите скрытые товары, т.к. вы администратор</i>" if admin else ""
        text = "📦 Каталог товаров:" + admin_hint
        kb = catalog_keyboard(products, is_admin=admin)

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
