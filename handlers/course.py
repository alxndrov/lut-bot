"""Курс Shot on iPhone + Cut by MALIMABI в клиентском боте.

Экран покупки (кодовое слово, /course, ссылка ?start=course, карточка в
каталоге), выдача доступа после оплаты и сервер мини-приложения:
статическая страница /app/ и /api/course/me, который отдаёт доступы
только по проверенному initData.
"""
import html
import logging
import mimetypes
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    MenuButtonCommands, MenuButtonWebApp, Message, WebAppInfo,
)
from aiohttp import web

import config
import database as db
from services.sellers import seller_line
from services import course as svc
from services.prodamus import build_payment_url

router = Router()
logger = logging.getLogger(__name__)

KEYWORDS_SETTING = "course_keywords"
CAPTION_LIMIT = 1024
WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"


# ── Экран покупки ──────────────────────────────────────────────────────────────

def _payment_url(option: dict, user_id: int, price: int) -> str:
    return build_payment_url(
        shop_url=config.PRODAMUS_SHOP_URL,
        product_name=option["title"],
        price=price,
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


async def sync_menu_button(bot: Bot):
    """Кнопка «Курс» у поля ввода. Пока курс скрыт в каталоге (до анонса) —
    только у админов, после включения товара — у всех."""
    if not config.MINIAPP_URL:
        return
    app_menu = MenuButtonWebApp(text="Курс", web_app=WebAppInfo(url=config.MINIAPP_URL))
    product = await db.get_course_product()
    public = bool(product and product.get("active"))
    try:
        await bot.set_chat_menu_button(menu_button=app_menu if public else MenuButtonCommands())
    except Exception as e:
        logger.warning(f"Не удалось поставить кнопку приложения: {e}")
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.set_chat_menu_button(chat_id=admin_id, menu_button=app_menu)
        except Exception as e:  # админ ещё не запускал клиентский бот
            logger.info(f"Кнопка приложения админу {admin_id} не поставлена: {e}")


async def set_admin_commands(bot: Bot, commands: list[BotCommand]):
    """Админам в меню команд — ещё /app (приложение курса)."""
    admin_commands = commands + [BotCommand(command="app", description="📱 Приложение курса")]
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logger.info(f"Команды админу {admin_id} не поставлены: {e}")


async def course_screen(user_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    product = await db.get_course_product()
    if not product:
        return None
    admin = user_id in config.ADMIN_IDS
    if not product.get("active") and not admin:
        return None
    options = await db.get_course_options(product["id"])
    owned = await db.get_user_course_sections(user_id)
    paid = await db.get_user_course_paid(user_id)
    has_call = bool(await db.get_user_course_call(user_id))
    full = svc.is_full_price()
    bundle = svc.bundle_option(options)
    reviews_left = max(0, config.BUNDLE_REVIEW_LIMIT - await db.count_bundle_reviews())

    lines = [f"<b>{product['name']}</b>"]
    desc = (product.get("description") or "").strip()
    if desc and desc != "-":
        lines += ["", desc]
    lines.append("")
    for o in options:
        price = svc.rub(svc.price_for(o))
        later = ("" if full or o["price_full"] <= svc.price_for(o)
                 else f" <s>{svc.rub(o['price_full'])}</s>")
        if svc.is_call_option(o):
            topup = svc.price_for_user(o, paid)
            mark = (" ✓ куплено" if has_call else
                    f" · тебе доплата {svc.rub(topup)}" if owned and topup < svc.price_for(o) else "")
        elif set(svc.option_sections(o)) <= owned:
            mark = " ✓ куплено"
        else:
            topup = svc.price_for_user(o, paid, bundle)
            mark = (f" · тебе {svc.rub(topup)}" if owned and len(svc.option_sections(o)) == 1
                    and topup < svc.price_for(o) else "")
        lines.append(f"• <b>{o['title']}</b> — {price}{later}{mark}")
    schedule = svc.price_schedule_text()
    if schedule:
        lines += ["", schedule]
    if reviews_left and not owned:
        lines += ["", f"🎁 <b>Подарок первым {config.BUNDLE_REVIEW_LIMIT} покупателям пакета</b>\n"
                      f"{config.COURSE_REVIEWER} лично посмотрит твой ролик и запишет видеоразбор: "
                      f"съёмка, цвет, монтаж. Свободно мест: <b>{reviews_left} из "
                      f"{config.BUNDLE_REVIEW_LIMIT}</b>."]
    if owned:
        names = ", ".join(svc.SECTION_SHORT[s] for s in svc.SECTIONS if s in owned)
        when = ("Уроки уже в приложении." if svc.is_open()
                else f"Уроки откроются {svc.open_date_text()} — пришлём сообщение.")
        lines += ["", f"У тебя открыто: <b>{names}</b>. {when}"]
        if bundle and len(owned) == 1:
            lines.append("Второй раздел — по цене пакета минус то, что ты уже заплатил.")
    lines += ["", seller_line("course")]
    if config.COURSE_OFFER_URL:
        lines.append(f'Оплачивая, ты принимаешь <a href="{config.COURSE_OFFER_URL}">оферту</a>.')
    if admin and not product.get("active"):
        lines += ["", "<i>🔐 Курс скрыт — экран видят только админы.</i>"]

    rows = []
    if config.PRODAMUS_SHOP_URL:
        for o in svc.available_options(options, owned, has_call):
            price = svc.price_for_user(o, paid, bundle)
            if not price:
                continue
            if owned and svc.is_call_option(o):
                label = "Перейти на VIP (созвон с Мишей)"
            else:
                label = "Докупить " + o["title"] if owned else o["title"]
            rows.append([InlineKeyboardButton(
                text=f"💳 {label} · {svc.rub(price)}", url=_payment_url(o, user_id, price))])
        if rows and config.LAVA_ENABLED and config.PAY_BASE_URL:
            rows.append([InlineKeyboardButton(text="🌍 Картой зарубежного банка (в $)",
                                              callback_data="course:foreign")])
        if rows:
            rows.append([InlineKeyboardButton(text="🔄 Я оплатил", callback_data="course:check")])
    button = app_button()
    if button:
        rows.append([button])
    if admin:
        rows.append([InlineKeyboardButton(text="🧪 Тестовая оплата", callback_data="course:test")])
    rows.append([InlineKeyboardButton(text="◀️ Каталог", callback_data="catalog")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "course:foreign")
async def cb_course_foreign(callback: CallbackQuery):
    """Те же варианты, что на экране курса, но оплата зарубежной картой —
    lava.top Миши в долларах (курс ЦБ + 5%, см. services/lava.py)."""
    from services import lava, paylinks
    user_id = callback.from_user.id
    product = await db.get_course_product()
    if not product:
        await callback.answer()
        return
    options = await db.get_course_options(product["id"])
    owned = await db.get_user_course_sections(user_id)
    paid = await db.get_user_course_paid(user_id)
    has_call = bool(await db.get_user_course_call(user_id))
    bundle = svc.bundle_option(options)
    try:
        rate = await lava.usd_rate()
    except lava.LavaError:
        await callback.answer("Оплата зарубежной картой временно недоступна", show_alert=True)
        return
    rows = []
    for o in svc.available_options(options, owned, has_call):
        price = svc.price_for_user(o, paid, bundle)
        if not price:
            continue
        label = ("Перейти на VIP" if owned and svc.is_call_option(o)
                 else ("Докупить " if owned else "") + o["title"])
        rows.append([InlineKeyboardButton(
            text=f"🌍 {label} · ${lava.usd_price(price, rate)}",
            url=paylinks.link(f"c_{user_id}_{o['id']}", price, o["title"], via="lava"))])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="course:show")])
    await callback.answer()
    await callback.message.answer(
        "🌍 <b>Оплата картой зарубежного банка</b>\n\n"
        "Цены в долларах — по курсу ЦБ плюс комиссия за зарубежную оплату. "
        "На странице оплаты укажите email: туда придёт подтверждение.\n\n"
        "<i>Pay by a non-Russian card in USD. Enter your email on the payment page.</i>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def send_course(message: Message, user_id: int) -> bool:
    screen = await course_screen(user_id)
    if not screen:
        await message.answer("Курс скоро появится — следите за анонсом 🤍")
        return False
    text, kb = screen
    await db.record_course_view(user_id, "bot")
    product = await db.get_course_product()
    # Подпись к фото у Telegram — не больше 1024 символов (без учёта HTML-тегов).
    # Длинный экран: фото отдельно, текст с кнопками — следующим сообщением.
    fits = len(html.unescape(re.sub(r"<[^>]+>", "", text))) <= CAPTION_LIMIT
    if product and product.get("photo_id") and fits:
        await message.answer_photo(product["photo_id"], caption=text, reply_markup=kb,
                                   parse_mode="HTML")
    else:
        if product and product.get("photo_id"):
            await message.answer_photo(product["photo_id"])
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


@router.message(Command("app"))
async def cmd_app(message: Message):
    """Открыть мини-приложение. До анонса — только админам."""
    product = await db.get_course_product()
    admin = message.from_user.id in config.ADMIN_IDS
    if not admin and not (product and product.get("active")):
        await send_course(message, message.from_user.id)
        return
    button = app_button()
    if not button:
        await message.answer("Адрес приложения не задан (MINIAPP_URL в .env).")
        return
    hidden = admin and not (product and product.get("active"))
    await message.answer(
        "📱 Приложение курса" + ("\n\n<i>🔐 Курс скрыт — у покупателей кнопки «Курс» "
                                "пока нет, приложение открывают только админы.</i>" if hidden else ""),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]), parse_mode="HTML")


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
    purchase_id = None
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
        if svc.is_call_option(option):
            extra += f"\n📞 Созвон VIP — до {svc.date_text(svc.call_expires_at())}"
        again = ""
        if owned_before:
            again = ("\n⬆️ Переход на VIP — доплата" if svc.is_call_option(option)
                     else "\n♻️ Докупка второго раздела")
        await notify(bot, f"🎓 <b>Курс оплачен</b>\n\n👤 {user.get('first_name') or '—'} {who}\n"
                          f"🛍 {option['title']}\n💵 {amount} ₽{again}{extra}", sale=payment_id)

    call_until = None
    if svc.is_call_option(option):
        expires = svc.call_expires_at()
        await db.add_course_call(user_id, purchase_id, expires, test)
        call_until = svc.date_text(expires)

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
            f"{config.COURSE_REVIEWER} запишет тебе видеоразбор твоего ролика (до 3 минут). "
            f"Ролик пришлёшь во вкладке «Разбор» приложения после старта курса."
            + ("\n\n🧪 В тесте место в очереди не занимается." if test else ""))
    if call_until:
        await bot.send_message(
            user_id,
            "📞 В VIP входит личный созвон с Мишей на 1 час. Когда его провести — "
            f"решаешь сам: до курса или после, но не позднее {call_until}. "
            "Чтобы договориться о времени, нажми «Договориться о созвоне» во вкладке "
            "«Разбор» приложения курса.")
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
    from handlers import course_orders
    user_id = user["id"]
    admin = user_id in config.ADMIN_IDS
    owned = await db.get_user_course_sections(user_id)
    test_owned = await db.get_user_test_course_sections(user_id)
    product = await db.get_course_product()
    options = await db.get_course_options(product["id"]) if product else []
    # Админ может посмотреть приложение «как после старта» до открытия
    is_open = svc.is_open() or (admin and preview_open)
    bundle = svc.bundle_option(options)
    paid = await db.get_user_course_paid(user_id)
    # Ролики уроков (Kinescope с DRM) — ссылка с токеном только на открытые уроки
    from services import kinescope
    videos = await kinescope.videos()
    watched = await db.get_lesson_views(user_id)
    sections = []
    for slug, title in svc.SECTIONS.items():
        program = svc.PROGRAM[slug]
        option = next((o for o in options if svc.option_sections(o) == [slug]), None)
        has = slug in owned
        sections.append({
            "slug": slug, "title": title, "name": program["name"], "lead": program["lead"],
            "owned": has, "test": slug in test_owned,
            # Докупка второго раздела — по цене пакета минус заплаченное,
            # зачёркнутой показываем обычную цену раздела
            "price": svc.price_for_user(option, paid, bundle) if option else None,
            "price_full": (svc.price_for(option) if paid else option["price_full"])
                          if option else None,
            "lessons": [{"n": i + 1, "title": t, "open": has and is_open,
                         "watched": i + 1 in watched.get(slug, []),
                         "video": kinescope.embed_url(videos[slug][str(i + 1)], user_id)
                                  if has and is_open and str(i + 1) in videos.get(slug, {}) else ""}
                        for i, t in enumerate(program["lessons"])],
        })
    vip = next((o for o in options if svc.is_call_option(o)), None)
    call = await db.get_user_course_call(user_id)
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
    review_order = await db.get_user_course_order(user_id, "review")
    call_order = await db.get_user_course_order(user_id, "call")
    return {
        "first_name": user.get("first_name") or "",
        "admin": admin,
        "open": is_open,
        "preview": is_open and not svc.is_open(),
        "open_date": svc.open_date_text(),
        "days_left": svc.days_to_open(),
        "full_price": svc.is_full_price(),
        "price_stage": svc.price_stage(),
        "price_until": svc.price_until_text(),
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
            "sent": bool(review_order),
            # open — ролик у Миши, done — разбор отправлен
            "status": review_order["status"] if review_order else None,
            "due": svc.date_text(review_order["due_at"]) if review_order else None,
            "link": review_order["link"] if review_order else None,
        },
        "vip": {"title": vip["title"], "upgrade": bool(owned),
                "price": svc.price_for_user(vip, paid)}
               if vip and not call else None,
        "call": {
            "until": svc.date_text(call["expires_at"]),
            "expired": svc.now_msk().date().isoformat() > call["expires_at"] and not call_order,
            "requested": bool(call_order),
            "scheduled": course_orders.call_time_text(call_order)
                         if call_order and call_order.get("scheduled_at") else None,
            "where": config.COURSE_CALL_WHERE,
            "done": bool(call_order and call_order["status"] == "done"),
            "missed": bool(call_order and call_order["status"] == "missed"),
            "test": bool(call["test"]),
        } if call else None,
        "options": [{"id": o["id"], "slug": o["slug"], "title": o["title"]} for o in options]
                   if admin else [],
        "buy_url": f"https://t.me/{bot_username}?start=course" if bot_username else "",
    }


async def api_me(request: web.Request) -> web.Response:
    user, body = await _app_user(request)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)
    await db.record_course_view(user["id"], "app")
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
    """Ролик на разбор → заказ по курсу (см. handlers/course_orders.py)."""
    user, body = await _app_user(request)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)
    state = await _app_state(user, "", bool(body.get("preview_open")))
    review = state["review"]
    if not review["eligible"]:
        return web.json_response({"error": "Разбор — для первых покупателей пакета."}, status=403)
    if not state["open"]:
        return web.json_response({"error": f"Заявки откроются {state['open_date']}."}, status=403)
    if review["status"] == "done":
        return web.json_response({"error": "Разбор уже отправлен — он в чате с ботом."},
                                 status=400)
    link = str(body.get("link") or "").strip()[:500]
    question = str(body.get("question") or "").strip()[:2000]
    if not link.lower().startswith(("http://", "https://")):
        return web.json_response({"error": "Нужна ссылка на видео — начинается с https://"},
                                 status=400)
    from handlers import course_orders
    await course_orders.submit_review(user, link, question, review["test"])
    return web.json_response(await _app_state(user, request.app.get("bot_username") or "",
                                              bool(body.get("preview_open"))))


async def api_call(request: web.Request) -> web.Response:
    """VIP: «Договориться о созвоне» → заказ по курсу, админы пишут клиенту сами."""
    user, body = await _app_user(request)
    if not user:
        return web.json_response({"error": "unauthorized"}, status=401)
    call = await db.get_user_course_call(user["id"])
    if not call:
        return web.json_response({"error": "Созвон входит в пакет VIP."}, status=403)
    order = await db.get_user_course_order(user["id"], "call")
    if order and order["status"] in ("done", "missed"):
        return web.json_response({"error": "Созвон уже прошёл."}, status=400)
    if svc.now_msk().date().isoformat() > call["expires_at"] and not order:
        return web.json_response({"error": "Срок созвона истёк."}, status=400)
    await db.mark_course_call_requested(call["id"])
    from handlers import course_orders
    await course_orders.request_call(user, call)
    return web.json_response(await _app_state(user, request.app.get("bot_username") or "",
                                              bool(body.get("preview_open"))))


async def api_log(request: web.Request) -> web.Response:
    """Сбой загрузки мини-приложения у пользователя (см. __appLog в index.html)."""
    try:
        raw = (await request.read())[:2000].decode("utf-8", "replace")
    except Exception:
        raw = ""
    logger.warning(f"miniapp: {raw}")
    return web.Response(status=204)


async def app_index(request: web.Request) -> web.Response:
    # Страницу — всегда целиком и без кэша. FileResponse отвечал Telegram
    # Desktop на Range-запрос куском (206) новой версии поверх закэшированной
    # старой — получалась склейка, и приложение висло на «Загружаем…»
    return web.Response(body=(WEBAPP_DIR / "index.html").read_bytes(),
                        content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store"})


async def app_static(request: web.Request) -> web.Response:
    # Статику — тоже целиком (200), без Range: Telegram Desktop (WebView2)
    # запрашивал telegram-web-app.js кусками (206), скрипт не догружался и
    # приложение висло на белом экране. Скрипты и стили — без кэша, шрифты кэшируем.
    root = (WEBAPP_DIR / "static").resolve()
    path = (root / request.match_info["path"]).resolve()
    if root not in path.parents or not path.is_file():
        raise web.HTTPNotFound()
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    cache = "public, max-age=604800" if path.suffix in (".woff2", ".woff", ".ttf") else "no-store"
    return web.Response(body=path.read_bytes(), content_type=ctype,
                        headers={"Cache-Control": cache, "Accept-Ranges": "none"})


def setup_web(app: web.Application, bot_username: str = ""):
    app["bot_username"] = bot_username
    app.router.add_get("/app", lambda r: web.HTTPFound("/app/"))
    app.router.add_get("/app/", app_index)
    app.router.add_get("/app/static/{path:.+}", app_static)
    app.router.add_post("/api/course/me", api_me)
    app.router.add_post("/api/course/log", api_log)
    app.router.add_post("/api/course/test", api_test)
    app.router.add_post("/api/course/review", api_review)
    app.router.add_post("/api/course/call", api_call)
