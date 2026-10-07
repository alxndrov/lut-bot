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
        lines += ["", f"Цены предпродажи действуют до {svc.presale_end_text()}."]
    if reviews_left and not owned:
        lines.append(f"🎁 Первым {config.BUNDLE_REVIEW_LIMIT}, кто возьмёт пакет, — личный "
                     f"разбор видео от {config.COURSE_REVIEWER}. Осталось мест: <b>{reviews_left}</b>.")
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
    if admin:
        rows.append([InlineKeyboardButton(text="🧪 Тестовая оплата", callback_data="course:test")])
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
                    notify, test: bool = False) -> bool | None:
    """Проводит оплату курса. None — платёж уже проведён, False — нет опции.

    test=True — «как будто оплатил» из админки: тот же доступ и те же
    сообщения покупателю, но без записи покупки, денег в финансах,
    места в очереди на разбор и уведомления партнёрам."""
    option = await db.get_course_option(option_id)
    if not option:
        logger.error(f"course: опция {option_id} не найдена (платёж {payment_id})")
        return False
    user = await db.get_user(user_id) or {}
    username = user.get("username")
    sections = svc.option_sections(option)
    owned_before = await db.get_user_course_sections(user_id)
    position = None
    if test:
        await db.grant_course_access(user_id, sections, None, test=True)
        if option.get("review_bonus"):
            taken = await db.count_bundle_reviews()
            position = taken + 1 if taken < config.BUNDLE_REVIEW_LIMIT else None
    else:
        purchase_id = await db.add_course_purchase(user_id, username, option["product_id"],
                                                   option_id, payment_id, amount)
        if purchase_id is None:
            logger.info(f"course: платёж {payment_id} уже проведён — пропускаю")
            return None
        await db.grant_course_access(user_id, sections, purchase_id)
        if option.get("review_bonus"):
            position = await db.reserve_bundle_review(user_id, purchase_id,
                                                      config.BUNDLE_REVIEW_LIMIT)

        from services.gsheets import request_finance_append
        paid_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
        request_finance_append(payment_id, paid_msk, amount, 0.0,
                               comment=f"Курс: {option['title']}", goods_type="Цифровой")

        who = f"@{username}" if username else f"id:{user_id}"
        extra = f"\n🎁 Разбор: место {position} из {config.BUNDLE_REVIEW_LIMIT}" if position else ""
        again = "\n♻️ Докупка второго раздела" if owned_before else ""
        await notify(bot, f"🎓 <b>Курс оплачен</b>\n\n👤 {user.get('first_name') or '—'} {who}\n"
                          f"🛍 {option['title']}\n💵 {amount} ₽{again}{extra}")

    mark = "🧪 <b>Тестовая оплата</b> — денег не было, в финансы не попадёт.\n\n" if test else ""
    if svc.is_open():
        text = "Оплата прошла 🤍 Уроки уже в приложении — открывай по кнопке ниже."
    else:
        text = (f"Оплата прошла 🤍 Уроки откроются {svc.open_date_text()} — пришлём сообщение. "
                f"Приложение курса уже можно открыть по кнопке ниже.")
    button = app_button()
    kb = InlineKeyboardMarkup(inline_keyboard=[[button]]) if button else None
    await bot.send_message(user_id, mark + text, reply_markup=kb, parse_mode="HTML")
    if position:
        await bot.send_message(
            user_id,
            f"Ты среди первых {config.BUNDLE_REVIEW_LIMIT}, кто взял пакет, — значит, "
            f"{config.COURSE_REVIEWER} лично разберёт твоё видео. Подробности пришлём после старта курса."
            + ("\n\n🧪 В тесте место в очереди не занимается." if test else ""))
    logger.info(f"course: user {user_id} {'ТЕСТ ' if test else ''}оплатил {option['slug']} "
                f"({amount} ₽, {payment_id}), разделы {sections}, разбор {position}")
    return True


# ── Тестовая оплата (только админы) ────────────────────────────────────────────

def _test_keyboard(options: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🧪 Как будто оплатил: {o['title']}",
                                  callback_data=f"course:testpay:{o['id']}")] for o in options]
    rows.append([InlineKeyboardButton(text="♻️ Сбросить тестовый доступ",
                                      callback_data="course:testreset")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="course:show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "course:test")
async def cb_test_menu(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    product = await db.get_course_product()
    options = await db.get_course_options(product["id"]) if product else []
    await callback.answer()
    await callback.message.answer(
        "🧪 <b>Тестовая оплата курса</b>\n\n"
        "Выдаёт доступ к разделам и присылает те же сообщения, что и после настоящей "
        "оплаты. Денег, записи о покупке, строки в финансах и места на разбор нет; "
        "партнёрам уведомление не уходит.\n\n"
        "«Сбросить» убирает только тестовый доступ — настоящие покупки остаются.",
        reply_markup=_test_keyboard(options), parse_mode="HTML")


@router.callback_query(F.data.startswith("course:testpay:"))
async def cb_test_pay(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    option_id = int(callback.data.rsplit(":", 1)[1])
    await callback.answer("🧪 Провожу тестовую оплату…")
    await provision(callback.bot, callback.from_user.id, option_id, "", 0, None, test=True)


@router.callback_query(F.data == "course:testreset")
async def cb_test_reset(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    removed = await db.clear_test_course_access(callback.from_user.id)
    await callback.answer(f"Тестовый доступ снят ({removed})." if removed
                          else "Тестового доступа не было.", show_alert=True)


# ── Мини-приложение ────────────────────────────────────────────────────────────

async def _app_user(request: web.Request) -> tuple[dict | None, dict]:
    try:
        body = await request.json()
    except Exception:
        return None, {}
    if not isinstance(body, dict):
        return None, {}
    return svc.verify_init_data(str(body.get("initData") or "")), body


async def _app_state(user: dict, bot_username: str, preview_open: bool = False) -> dict:
    user_id = user["id"]
    admin = user_id in config.ADMIN_IDS
    owned = await db.get_user_course_sections(user_id)
    test_owned = await db.get_user_test_course_sections(user_id)
    product = await db.get_course_product()
    options = await db.get_course_options(product["id"]) if product else []
    # Админ может посмотреть приложение «как после старта» до открытия
    is_open = svc.is_open() or (admin and preview_open)
    sections = []
    for slug, title in svc.SECTIONS.items():
        program = svc.PROGRAM[slug]
        option = next((o for o in options if svc.option_sections(o) == [slug]), None)
        has = slug in owned
        sections.append({
            "slug": slug, "title": title, "name": program["name"], "lead": program["lead"],
            "owned": has, "test": slug in test_owned,
            "price": svc.price_for(option) if option else None,
            "price_full": option["price_full"] if option else None,
            "lessons": [{"n": i + 1, "title": t, "open": has and is_open}
                        for i, t in enumerate(program["lessons"])],
        })
    bundle = next((o for o in options if len(svc.option_sections(o)) > 1), None)
    materials = []
    for m in svc.MATERIALS:
        has = bool(set(m["sections"]) & owned)
        if not has:
            continue
        materials.append({k: m.get(k) for k in ("kind", "badge", "title", "note")}
                         | {"url": m["url"] if is_open else ""})

    position = await db.get_bundle_review_position(user_id)
    taken = await db.count_bundle_reviews()
    review_test = admin and {"shooting", "editing"} <= test_owned
    last = await db.get_last_course_review_request(user_id)
    return {
        "first_name": user.get("first_name") or "",
        "admin": admin,
        "open": is_open,
        "preview": is_open and not svc.is_open(),
        "open_date": svc.open_date_text(),
        "days_left": svc.days_to_open(),
        "full_price": svc.is_full_price(),
        "presale_end": svc.presale_end_text(),
        "sections": sections,
        "bundle": {"title": bundle["title"], "price": svc.price_for(bundle),
                   "price_full": bundle["price_full"]} if bundle else None,
        "materials": materials,
        "review": {
            "eligible": bool(position) or review_test,
            "test": review_test and not position,
            "position": position,
            "limit": config.BUNDLE_REVIEW_LIMIT,
            "left": max(0, config.BUNDLE_REVIEW_LIMIT - taken),
            "reviewer": config.COURSE_REVIEWER,
            "sent": bool(last),
        },
        "options": [{"id": o["id"], "slug": o["slug"], "title": o["title"]} for o in options]
                   if admin else [],
        "buy_url": f"https://t.me/{bot_username}?start=course" if bot_username else "",
    }


async def api_me(request: web.Request) -> web.Response:
    user, body = await _app_user(request)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response(await _app_state(user, request.app.get("bot_username") or "",
                                              bool(body.get("preview_open"))))


async def api_test(request: web.Request) -> web.Response:
    """Тестовая оплата из мини-приложения — только админам."""
    user, body = await _app_user(request)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)
    if user["id"] not in config.ADMIN_IDS:
        return web.json_response({"error": "forbidden"}, status=403)
    action = body.get("action")
    if action == "pay":
        try:
            option_id = int(body.get("option_id"))
        except (TypeError, ValueError):
            return web.json_response({"error": "bad option"}, status=400)
        ok = await provision(request.app["bot"], user["id"], option_id, "", 0, None, test=True)
        if ok is False:
            return web.json_response({"error": "bad option"}, status=400)
    elif action == "reset":
        await db.clear_test_course_access(user["id"])
    else:
        return web.json_response({"error": "bad action"}, status=400)
    return web.json_response(await _app_state(user, request.app.get("bot_username") or "",
                                              bool(body.get("preview_open"))))


async def api_review(request: web.Request) -> web.Response:
    """Заявка на личный разбор: ссылка на видео и вопрос — партнёрам в админ-бот."""
    user, body = await _app_user(request)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)
    state = await _app_state(user, "", bool(body.get("preview_open")))
    review = state["review"]
    if not review["eligible"]:
        return web.json_response({"error": "Разбор — для первых покупателей пакета."}, status=403)
    if not state["open"]:
        return web.json_response({"error": f"Заявки откроются {state['open_date']}."}, status=403)
    link = str(body.get("link") or "").strip()[:500]
    question = str(body.get("question") or "").strip()[:2000]
    if not link.lower().startswith(("http://", "https://")):
        return web.json_response({"error": "Нужна ссылка на видео — начинается с https://"},
                                 status=400)
    test = review["test"]
    await db.add_course_review_request(user["id"], link, question, test)
    from handlers.prodamus_webhook import _send_notify
    from html import escape
    who = f"@{user['username']}" if user.get("username") else f"id:{user['id']}"
    place = f"место {review['position']} из {review['limit']}" if review["position"] else "тест"
    await _send_notify(None, (
        f"{'🧪 ТЕСТ · ' if test else ''}🎬 <b>Заявка на разбор</b> ({place})\n\n"
        f"👤 {escape(user.get('first_name') or '—')} {escape(who)}\n"
        f"🔗 {escape(link)}\n"
        + (f"❓ {escape(question)}" if question else "")))
    state["review"]["sent"] = True
    return web.json_response(state)


async def app_index(request: web.Request) -> web.Response:
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def setup_web(app: web.Application, bot_username: str = ""):
    app["bot_username"] = bot_username
    app.router.add_get("/app", lambda r: web.HTTPFound("/app/"))
    app.router.add_get("/app/", app_index)
    if (WEBAPP_DIR / "static").is_dir():
        app.router.add_static("/app/static/", WEBAPP_DIR / "static", show_index=False)
    app.router.add_post("/api/course/me", api_me)
    app.router.add_post("/api/course/test", api_test)
    app.router.add_post("/api/course/review", api_review)
