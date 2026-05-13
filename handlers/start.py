import logging
from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from datetime import datetime

from config import ADMIN_IDS
from keyboards.user import catalog_keyboard, back_to_catalog_keyboard
from keyboards.admin import admin_menu_keyboard
import database as db

router = Router()
logger = logging.getLogger(__name__)


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

    # Автозапуск воронки через deep link
    if funnel_to_start:
        # Ищем по slug (ASCII-safe), fallback — по id (старые воронки)
        funnel = await db.get_funnel_by_slug(funnel_to_start)
        if funnel is None:
            funnel = await db.get_funnel(funnel_to_start)

        if funnel and funnel["active"]:
            already_bought = False
            if funnel.get("product_id"):
                purchase = await db.get_purchase(message.from_user.id, funnel["product_id"])
                already_bought = bool(purchase)

            if not already_bought:
                await db.enqueue_funnel(
                    message.from_user.id, funnel["id"], source=funnel_source
                )
                logger.info(
                    f"funnel auto-start: user {message.from_user.id} → "
                    f"funnel {funnel['id']} slug={funnel_to_start} source={funnel_source}"
                )
        else:
            logger.warning(f"deep link lm_{funnel_to_start}: воронка не найдена или неактивна")

    products = await db.get_all_products()
    banner_file_id = await db.get_setting("catalog_banner_file_id")

    if not products:
        await message.answer("👋 Привет! Пока товаров нет, скоро появятся.")
        return

    text = "👋 Привет! Выбери товар из каталога:"
    kb = catalog_keyboard(products)

    if banner_file_id:
        await message.answer_photo(banner_file_id, caption=text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@router.message(Command("catalog"))
async def cmd_catalog(message: Message):
    await show_catalog(message)


@router.message(Command("mypurchases"))
async def cmd_mypurchases(message: Message):
    purchases = await db.get_user_purchases(message.from_user.id)

    if not purchases:
        await message.answer(
            "🛍 У вас пока нет покупок.\n\n"
            "Загляните в /catalog!",
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


async def show_catalog(message_or_callback, edit=False):
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
        text = "📦 Каталог товаров:" + (" <i>(включая скрытые)</i>" if admin else "")
        kb = catalog_keyboard(products, is_admin=admin)

    if edit:
        msg = message_or_callback.message
        try:
            await msg.delete()
        except Exception:
            pass
        if banner_file_id:
            await msg.answer_photo(banner_file_id, caption=text, reply_markup=kb)
        else:
            await msg.answer(text, reply_markup=kb)
    else:
        if banner_file_id:
            await message_or_callback.answer_photo(banner_file_id, caption=text, reply_markup=kb)
        else:
            await message_or_callback.answer(text, reply_markup=kb)
