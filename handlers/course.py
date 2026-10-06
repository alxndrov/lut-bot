"""Курс Shot on iPhone + Cut by MALIMABI в клиентском боте.

Экран покупки (кодовое слово, /course, ссылка ?start=course, карточка в
каталоге), выдача доступа после оплаты и сервер мини-приложения:
статическая страница /app/ и /api/course/me, который отдаёт доступы
только по проверенному initData.
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo,
)
from aiohttp import web

import config
import database as db
from services import course as svc
from services.prodamus import build_payment_url

router = Router()
logger = logging.getLogger(__name__)

KEYWORDS_SETTING = "course_keywords"
WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"


# ── Экран покупки ──────────────────────────────────────────────────────────────

def _payment_url(option: dict, user_id: int) -> str:
    return build_payment_url(
        shop_url=config.PRODAMUS_SHOP_URL,
        product_name=option["title"],
        price=svc.price_for(option),
        user_id=user_id,
        product_id=option["id"],
        order_type="c",
        secret=config.PRODAMUS_SECRET,
        notification_url=config.PRODAMUS_WEBHOOK_URL,
    )


def app_button(text: str = "📱 Открыть приложение курса") -> InlineKeyboardButton | None:
    if not config.MINIAPP_URL:
        return None
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=config.MINIAPP_URL))


async def course_screen(user_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    product = await db.get_course_product()
    if not product:
        return None
    admin = user_id in config.ADMIN_IDS
    if not product.get("active") and not admin:
        return None
    options = await db.get_course_options(product["id"])
    owned = await db.get_user_course_sections(user_id)
    full = svc.is_full_price()
    reviews_left = max(0, config.BUNDLE_REVIEW_LIMIT - await db.count_bundle_reviews())

    lines = [f"<b>{product['name']}</b>"]
    desc = (product.get("description") or "").strip()
    if desc and desc != "-":
        lines += ["", desc]
    lines.append("")
    for o in options:
        price = svc.rub(svc.price_for(o))
        later = "" if full else f" <s>{svc.rub(o['price_full'])}</s>"
        mark = " ✓ куплено" if set(svc.option_sections(o)) <= owned else ""
        lines.append(f"• <b>{o['title']}</b> — {price}{later}{mark}")
    if not full:
        lines += ["", f"Цены предпродажи действуют до {svc.open_date_text()}."]
    if reviews_left and not owned:
        lines.append(f"🎁 Первым {config.BUNDLE_REVIEW_LIMIT}, кто возьмёт пакет, — личный "
                     f"разбор видео от Миши. Осталось мест: <b>{reviews_left}</b>.")
    if owned:
        names = ", ".join(svc.SECTION_SHORT[s] for s in svc.SECTIONS if s in owned)
        when = ("Уроки уже в приложении." if svc.is_open()
                else f"Уроки откроются {svc.open_date_text()} — пришлём сообщение.")
        lines += ["", f"У тебя открыто: <b>{names}</b>. {when}"]
    if config.COURSE_OFFER_URL:
        lines += ["", f'Оплачивая, ты принимаешь <a href="{config.COURSE_OFFER_URL}">оферту</a>.']
    if admin and not product.get("active"):
        lines += ["", "<i>🔐 Курс скрыт — экран видят только админы.</i>"]

    rows = []
    if config.PRODAMUS_SHOP_URL:
        for o in svc.available_options(options, owned):
            label = "Докупить " + o["title"] if owned else o["title"]
            rows.append([InlineKeyboardButton(
                text=f"💳 {label} · {svc.rub(svc.price_for(o))}", url=_payment_url(o, user_id))])
        if rows:
            rows.append([InlineKeyboardButton(text="🔄 Я оплатил", callback_data="course:check")])
    button = app_button()
    if button:
        rows.append([button])
    rows.append([InlineKeyboardButton(text="◀️ Каталог", callback_data="catalog")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def send_course(message: Message, user_id: int) -> bool:
    screen = await course_screen(user_id)
    if not screen:
        await message.answer("Курс скоро появится — следите за анонсом 🤍")
        return False
    text, kb = screen
    product = await db.get_course_product()
    if product and product.get("photo_id"):
        await message.answer_photo(product["photo_id"], caption=text, reply_markup=kb,
                                   parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML",
                             disable_web_page_preview=True)
    return True


async def _keywords() -> list[str]:
    raw = await db.get_setting(KEYWORDS_SETTING) or ""
    return [w.strip().lower() for w in raw.split(",") if w.strip()]


async def _is_keyword(message: Message) -> bool:
    text = (message.text or "").strip().lower()
    return bool(text) and text in await _keywords()


@router.message(StateFilter(None), F.text, _is_keyword)
async def msg_keyword(message: Message):
    from handlers.start import ensure_policy
    if not await ensure_policy(message, "course"):
        return
    await send_course(message, message.from_user.id)


@router.message(Command("course"))
async def cmd_course(message: Message):
    from handlers.start import ensure_policy
    if not await ensure_policy(message, "course"):
        return
    await send_course(message, message.from_user.id)


@router.callback_query(F.data == "course:show")
async def cb_course(callback: CallbackQuery):
    await callback.answer()
    await send_course(callback.message, callback.from_user.id)


@router.callback_query(F.data == "course:check")
async def cb_check(callback: CallbackQuery):
    owned = await db.get_user_course_sections(callback.from_user.id)
    if not owned:
        await callback.answer("Оплата пока не пришла. Если оплатил только что — "
                              "подожди минуту и нажми ещё раз.", show_alert=True)
        return
    await callback.answer("Оплата есть ✓")
    await send_course(callback.message, callback.from_user.id)


# ── Выдача после оплаты ────────────────────────────────────────────────────────

async def provision(bot: Bot, user_id: int, option_id: int, payment_id: str, amount: int,
                    notify) -> bool | None:
    """Проводит оплату курса. None — платёж уже проведён, False — нет опции."""
    option = await db.get_course_option(option_id)
    if not option:
        logger.error(f"course: опция {option_id} не найдена (платёж {payment_id})")
        return False
    user = await db.get_user(user_id) or {}
    username = user.get("username")
    purchase_id = await db.add_course_purchase(user_id, username, option["product_id"],
                                               option_id, payment_id, amount)
    if purchase_id is None:
        logger.info(f"course: платёж {payment_id} уже проведён — пропускаю")
        return None
    sections = svc.option_sections(option)
    owned_before = await db.get_user_course_sections(user_id)
    await db.grant_course_access(user_id, sections, purchase_id)
    position = None
    if option.get("review_bonus"):
        position = await db.reserve_bundle_review(user_id, purchase_id, config.BUNDLE_REVIEW_LIMIT)

    from services.gsheets import request_finance_append
    paid_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    request_finance_append(payment_id, paid_msk, amount, 0.0,
                           comment=f"Курс: {option['title']}", goods_type="Цифровой")

    who = f"@{username}" if username else f"id:{user_id}"
    extra = f"\n🎁 Разбор: место {position} из {config.BUNDLE_REVIEW_LIMIT}" if position else ""
    again = "\n♻️ Докупка второго раздела" if owned_before else ""
    await notify(bot, f"🎓 <b>Курс оплачен</b>\n\n👤 {user.get('first_name') or '—'} {who}\n"
                      f"🛍 {option['title']}\n💵 {amount} ₽{again}{extra}")

    if svc.is_open():
        text = "Оплата прошла 🤍 Уроки уже в приложении — открывай по кнопке ниже."
    else:
        text = (f"Оплата прошла 🤍 Уроки откроются {svc.open_date_text()} — пришлём сообщение. "
                f"Приложение курса уже можно открыть по кнопке ниже.")
    button = app_button()
    kb = InlineKeyboardMarkup(inline_keyboard=[[button]]) if button else None
    await bot.send_message(user_id, text, reply_markup=kb)
    if position:
        await bot.send_message(
            user_id,
            f"Ты среди первых {config.BUNDLE_REVIEW_LIMIT}, кто взял пакет, — значит, Миша "
            f"лично разберёт твоё видео. Подробности пришлём после старта курса.")
    logger.info(f"course: user {user_id} оплатил {option['slug']} ({amount} ₽, {payment_id}), "
                f"разделы {sections}, разбор {position}")
    return True


# ── Мини-приложение ────────────────────────────────────────────────────────────

async def api_me(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    user = svc.verify_init_data(str(body.get("initData") or ""))
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)
    owned = await db.get_user_course_sections(user["id"])
    product = await db.get_course_product()
    options = await db.get_course_options(product["id"]) if product else []
    bot_username = request.app.get("bot_username") or ""
    is_open = svc.is_open()
    sections = []
    for slug, title in svc.SECTIONS.items():
        cheapest = min((svc.price_for(o) for o in options
                        if svc.option_sections(o) == [slug]), default=None)
        sections.append({
            "slug": slug, "title": title, "owned": slug in owned,
            "price": cheapest,
            # Уроки появятся на этапе 2 — и только купившим после открытия
            "lessons": [],
        })
    return web.json_response({
        "first_name": user.get("first_name") or "",
        "open": is_open,
        "open_date": svc.open_date_text(),
        "sections": sections,
        "buy_url": f"https://t.me/{bot_username}?start=course" if bot_username else "",
    })


async def app_index(request: web.Request) -> web.Response:
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def setup_web(app: web.Application, bot_username: str = ""):
    app["bot_username"] = bot_username
    app.router.add_get("/app", lambda r: web.HTTPFound("/app/"))
    app.router.add_get("/app/", app_index)
    if (WEBAPP_DIR / "static").is_dir():
        app.router.add_static("/app/static/", WEBAPP_DIR / "static", show_index=False)
    app.router.add_post("/api/course/me", api_me)
