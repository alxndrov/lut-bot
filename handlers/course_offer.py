"""Личное предложение на курс: свой тариф по своей цене до своей даты.

Админ в админ-боте: /offer @ник ТАРИФ ЦЕНА ДО_ДАТЫ (тарифы shooting, editing,
package, vip; дата — «20.11», действует до 23:59 МСК этого дня). Бот отвечает
ссылкой https://app.malimabi.com/pay/<токен> (services/paylinks, via
offer:<код>). /offers — активные предложения, /offer_cancel <код> — отменить.
Со второй строки /offer — шаблон текста клиенту с метками {ссылка}, {цена},
{старая_цена}, {срок}, {тариф}: бот вторым сообщением присылает готовый текст
без разметки — скопировать в личку (render_template).

Ссылка открывает страницу предложения (offer_page): тариф, цена, старая цена
зачёркнутой, срок и выбор «Карта РФ или СБП» → PayAnyWay, «Иностранный банк» →
lava.top (та же ссылка с via offer:<код>:paw|lava, см. pay_router._offer_pay).
Если человек ещё не давал согласия (или их надо обновить), страница отправляет
его в бот — t.me/<бот>?start=offer_<код>: там галочки, а после них то же
предложение кнопками (claim). Давал — сразу к оплате.

Номер заказа c_{клиент}_{вариант}, как с экрана курса: чек, учёт и доступ —
как при обычной покупке, и доступ получит только тот, для кого предложение.
В боте чужой человек, а везде — истёкшее или отменённое предложение ведут
к обычным ценам.

События (открыл предложение, перешёл к оплате, купил) — по одному каждого
вида на предложение — пишутся в course_offer_events и дописываются в колонку
«Заметки» (L) этого человека в таблице «База» (services/leads_sheet);
что не записалось — повторяется вместе с выгрузкой «Базы» раз в 15 минут.

Ту же ссылку делает и scripts/make_course_offer.py (с сервера, скидкой в %).
"""
import asyncio
import logging
import re
from datetime import date, datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import database as db
from services import course as svc
from services import paylinks
from services.sellers import seller_line

router = Router()        # клиентский бот: ?start=offer_<код> и кнопка «Оплатить»
admin_router = Router()  # админ-бот: /offer, /offers, /offer_cancel
logger = logging.getLogger(__name__)

PREFIX = "offer_"
TARIFFS = {"shooting": "shooting", "съёмка": "shooting", "съемка": "shooting",
           "editing": "editing", "монтаж": "editing",
           "package": "bundle", "bundle": "bundle", "пакет": "bundle",
           "vip": "vip"}
EVENTS = {"open": "открыл предложение", "pay_paw": "перешёл к оплате — карта РФ / СБП",
          "pay_lava": "перешёл к оплате — иностранный банк", "buy": "купил"}
_tasks: set = set()
_flush_lock = asyncio.Lock()


# ── Проверки и ссылки ─────────────────────────────────────────────────────────

def _until_date(offer: dict) -> date:
    return datetime.strptime(offer["valid_until"], "%Y-%m-%d").date()


async def _check(offer: dict | None, user_id: int, now: datetime | None = None) -> str | None:
    """Почему предложение не действует (текст клиенту) или None — действует."""
    if not offer:
        return "Эта ссылка не работает."
    if offer["user_id"] != user_id:
        return "Это личное предложение для другого человека."
    if offer.get("cancelled_at") or (now or svc.now_msk()).date() > _until_date(offer):
        return "Срок этого предложения закончился."
    option = await db.get_course_option(offer["option_id"])
    if not option:
        return "Этот вариант курса больше недоступен."
    if svc.is_call_option(option):
        if await db.get_user_course_call(user_id):
            return "Этот тариф у тебя уже есть 🤍"
    elif set(svc.option_sections(option)) <= await db.get_user_course_sections(user_id):
        return "Этот тариф у тебя уже есть 🤍"
    return None


def pay_link(offer: dict, option: dict, method: str = "") -> str:
    """https://app.malimabi.com/pay/<токен>. Без method — страница предложения,
    'paw' — сразу PayAnyWay, 'lava' — lava.top."""
    from services import paylinks
    via = f"offer:{offer['token']}" + (f":{method}" if method else "")
    return paylinks.link(f"c_{offer['user_id']}_{offer['option_id']}", offer["price"],
                         option["title"], via=via)


def _date_text(offer: dict) -> str:
    return svc.date_text(offer["valid_until"])


def _who(user: dict | None, user_id: int) -> str:
    if user and user.get("username"):
        return "@" + user["username"]
    return (user or {}).get("first_name") or f"id {user_id}"


# ── События → таблица «База» ───────────────────────────────────────────────────

async def log_event(offer: dict, kind: str, detail: str = "") -> None:
    """Запомнить событие (одно каждого вида на предложение) и записать в «Базу»."""
    try:
        if not await db.add_course_offer_event(offer["token"], offer["user_id"], kind, detail):
            return
    except Exception as e:
        logger.warning(f"offer: событие {kind} по {offer['token']} не сохранено: {e}")
        return
    if not config.LEADS_SHEET_ID:
        return
    task = asyncio.create_task(flush_events())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _event_line(event: dict, offer: dict | None, option: dict | None) -> str:
    at = (datetime.fromisoformat(event["created_at"]).replace(tzinfo=timezone.utc)
          .astimezone(svc.MSK).strftime("%d.%m %H:%M"))
    what = ""
    if offer and option:
        what = (f": {option['title']}, {svc.rub(offer['price'])} до "
                f"{datetime.strptime(offer['valid_until'], '%Y-%m-%d').strftime('%d.%m')}")
    extra = f" ({event['detail']})" if event.get("detail") else ""
    return f"{at} личное предложение — {EVENTS.get(event['kind'], event['kind'])}{what}{extra}"


async def flush_events() -> None:
    """Дописать незаписанные события в «Заметки» (L). Ошибка — повторим позже."""
    if not config.LEADS_SHEET_ID:
        return
    from services import leads_sheet
    async with _flush_lock:
        for event in await db.get_course_offer_events(unsynced=True):
            offer = await db.get_course_offer(event["token"])
            option = await db.get_course_option(offer["option_id"]) if offer else None
            user = await db.get_user(event["user_id"]) or {}
            try:
                ok = await asyncio.to_thread(leads_sheet.append_note, event["user_id"],
                                             user.get("username"), user.get("first_name"),
                                             _event_line(event, offer, option))
            except Exception as e:
                logger.warning(f"offer: событие {event['id']} не записано в «Базу»: {e}")
                return
            if not ok:
                logger.warning("offer: в «Базе» нет свободной строки для заметки")
                return
            await db.mark_course_offer_event_synced(event["id"])


async def on_purchase(user_id: int, option_id: int, amount: int) -> None:
    """Из handlers/course.provision: оплата тарифа, на который было предложение."""
    offer = await db.latest_course_offer(user_id, option_id)
    if offer:
        await log_event(offer, "buy", svc.rub(amount))


# ── Клиентский бот ─────────────────────────────────────────────────────────────

async def claim(message: Message, user_id: int, token: str) -> None:
    """Открыл t.me/<бот>?start=offer_<код> (согласие уже есть)."""
    from handlers.course import send_course
    offer = await db.get_course_offer(token)
    problem = await _check(offer, user_id)
    if problem:
        logger.info(f"offer: {token} у {user_id} не сработал: {problem}")
        await message.answer(problem + " Вот курс по обычной цене 👇")
        await send_course(message, user_id)
        return
    option = await db.get_course_option(offer["option_id"])
    full = svc.price_for(option)
    old = f"<s>{svc.rub(full)}</s> " if full > offer["price"] else ""
    await db.record_course_view(user_id, "offer")
    await log_event(offer, "open", "в боте")
    lines = [
        "🤍 <b>Личное предложение</b>",
        "",
        f"<b>{escape(option['title'])}</b> — {old}<b>{svc.rub(offer['price'])}</b>",
        f"Действует до {_date_text(offer)} включительно (23:59 МСК) и только для тебя.",
        "",
        seller_line("course"),
    ]
    if config.COURSE_OFFER_URL:
        lines.append(f'Оплачивая, ты принимаешь <a href="{config.COURSE_OFFER_URL}">оферту</a>.')
    rows = [[InlineKeyboardButton(text="💳 Карта РФ или СБП", url=pay_link(offer, option, "paw"))]]
    if config.LAVA_ENABLED:
        rows.append([InlineKeyboardButton(text="🌍 Иностранный банк",
                                          url=pay_link(offer, option, "lava"))])
    rows += [[InlineKeyboardButton(text="📖 Что в курсе", callback_data="course:show")],
             [InlineKeyboardButton(text="🔄 Я оплатил", callback_data="course:check")]]
    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    logger.info(f"offer: {token} открыл {user_id} в боте")


@router.callback_query(F.data.startswith("course:offer:"))
async def cb_pay(callback: CallbackQuery):
    """Кнопка «Оплатить» из сообщений до 17.11 — те же способы оплаты."""
    token = callback.data.split(":", 2)[2]
    offer = await db.get_course_offer(token)
    problem = await _check(offer, callback.from_user.id)
    if problem:
        await callback.answer(problem, show_alert=True)
        return
    option = await db.get_course_option(offer["option_id"])
    rows = [[InlineKeyboardButton(text="💳 Карта РФ или СБП", url=pay_link(offer, option, "paw"))]]
    if config.LAVA_ENABLED:
        rows.append([InlineKeyboardButton(text="🌍 Иностранный банк",
                                          url=pay_link(offer, option, "lava"))])
    await callback.answer()
    await callback.message.answer("Выберите способ оплаты:",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# ── Страница предложения (app.malimabi.com/pay/…) ──────────────────────────────

def _button(url: str, text: str, dark: bool = True) -> str:
    style = ("background:#111;color:#fff" if dark else "background:#fff;color:#111;border:1px solid #111")
    return (f'<a href="{escape(url)}" style="display:block;text-align:center;text-decoration:none;'
            f'margin-top:12px;padding:14px;border-radius:10px;{style}">{escape(text)}</a>')


async def offer_page(request, offer: dict, link: dict, bot_username: str):
    """Страница по ссылке без способа оплаты. Согласий нет — в бот за ними."""
    from aiohttp import web
    user_id = offer["user_id"]
    if config.POLICY_REQUIRED and bot_username and await db.policy_status(user_id) != "ok":
        logger.info(f"offer: {offer['token']} — у {user_id} нет согласий, веду в бот")
        raise web.HTTPFound(f"https://t.me/{bot_username}?start={PREFIX}{offer['token']}")
    option = await db.get_course_option(offer["option_id"])
    full = svc.price_for(option)
    await log_event(offer, "open", "страница оплаты")
    old = (f'<s style="color:#888">{escape(svc.rub(full))}</s> ' if full > offer["price"] else "")
    docs = ""
    if config.COURSE_OFFER_URL:
        docs = (f'<p><small>Оплачивая, вы принимаете <a href="{escape(config.COURSE_OFFER_URL)}">'
                f"оферту</a>.</small></p>")
    buttons = _button(pay_link(offer, option, "paw"), "💳 Карта РФ или СБП")
    if config.LAVA_ENABLED:
        buttons += _button(pay_link(offer, option, "lava"), "🌍 Иностранный банк", dark=False)
    back = (f'<p style="margin-top:24px"><a href="https://t.me/{escape(bot_username)}">'
            f"Вернуться в бот</a></p>" if bot_username else "")
    return web.Response(content_type="text/html", text=(
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Личное предложение</title>"
        '<body style="font:17px/1.5 system-ui,sans-serif;max-width:420px;margin:8vh auto;padding:0 16px">'
        "<p style=\"color:#888;margin:0\">🤍 Личное предложение</p>"
        f"<h2 style=\"margin:6px 0 4px\">{escape(option['title'])}</h2>"
        f"<p style=\"font-size:24px;margin:8px 0\">{old}<b>{escape(svc.rub(offer['price']))}</b></p>"
        f"<p>Действует до {escape(_date_text(offer))} включительно, 23:59 МСК.</p>"
        f"{buttons}"
        f"<p><small>{escape(seller_line('course'))}</small></p>{docs}{back}</body>"))


# ── Админ-бот: /offer, /offers, /offer_cancel ──────────────────────────────────

USAGE = ("Формат: <code>/offer @ник ТАРИФ ЦЕНА ДО_ДАТЫ</code>\n"
         "Например: <code>/offer @dof0miin vip 11190 20.11</code>\n\n"
         "Тарифы: shooting, editing, package, vip. Действует до 23:59 МСК этой даты.\n\n"
         "Со следующей строки можно написать текст клиенту — бот вернёт его отдельным "
         "сообщением с подставленными {ссылка}, {цена}, {старая_цена}, {срок}, {тариф}.")

# {тариф} в тексте клиенту — по-русски, со строчной (он идёт внутри фразы)
TARIFF_NAMES = {"vip": "пакет с созвоном", "bundle": "пакет: съёмка + монтаж",
                "shooting": "съёмка", "editing": "монтаж"}
_LABEL = re.compile(r"\{([^{}\n]*)\}")


def render_template(text: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """Подставляет {метки}. Неизвестные оставляет как есть и возвращает списком
    (по одной, в порядке появления). Переносы и пустые строки не трогает."""
    unknown: list[str] = []

    def sub(m: re.Match) -> str:
        if m[1] in values:
            return values[m[1]]
        if m[0] not in unknown:
            unknown.append(m[0])
        return m[0]

    return _LABEL.sub(sub, text), unknown


def parse_until(text: str, today: date) -> date | None:
    """«20.11», «20.11.2026», «20.11.26», «2026-11-20». Без года — ближайшее
    такое число начиная с сегодня."""
    text = text.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return datetime.strptime(text, "%Y-%m-%d").date()
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?", text)
        if not m:
            return None
        day, month, year = int(m[1]), int(m[2]), m[3]
        if year:
            return date(int(year) + (2000 if len(year) == 2 else 0), month, day)
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


async def _find_user(arg: str) -> tuple[int | None, dict | None]:
    raw = re.sub(r"^(?:https?://)?(?:t\.me/)?@?", "", arg.strip()).rstrip("/")
    if raw.isdigit():
        return int(raw), await db.get_user(int(raw))
    user = await db.get_user_by_username(raw) if raw else None
    return (user["user_id"], user) if user else (None, None)


async def create_offer(user_id: int, option: dict, price: int, until: date,
                       note: str | None = None) -> dict:
    token = paylinks.link_code()
    await db.create_course_offer(token, option["id"], price, user_id, until.isoformat(), note)
    return await db.get_course_offer(token)


@admin_router.message(Command("offer"))
async def cmd_offer(message: Message, command: CommandObject):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    # Первая строка — параметры, всё после неё — шаблон текста клиенту
    head, _, template = (command.args or "").partition("\n")
    args = head.split()
    if len(args) != 4:
        await message.answer(USAGE, parse_mode="HTML")
        return
    who, tariff, price_s, until_s = args
    slug = TARIFFS.get(tariff.lower())
    product = await db.get_course_product()
    options = await db.get_course_options(product["id"]) if product else []
    option = next((o for o in options if o["slug"] == slug), None)
    if not option:
        await message.answer(f"Не знаю тариф «{escape(tariff)}».\n\n{USAGE}", parse_mode="HTML")
        return
    price_s = price_s.replace("₽", "")
    if not price_s.isdigit() or int(price_s) < 1:
        await message.answer(f"Цена — число в рублях, например 11190.\n\n{USAGE}", parse_mode="HTML")
        return
    price, full = int(price_s), svc.price_for(option)
    if price > full:
        await message.answer(f"{svc.rub(price)} дороже обычной цены {escape(option['title'])} — "
                             f"{svc.rub(full)}. Проверь цену.")
        return
    today = svc.now_msk().date()
    until = parse_until(until_s, today)
    if not until or until < today:
        await message.answer(f"Не понял дату «{escape(until_s)}» или она уже прошла.\n\n{USAGE}",
                             parse_mode="HTML")
        return
    user_id, user = await _find_user(who)
    if not user_id:
        await message.answer(f"Не нашёл {escape(who)} среди тех, кто запускал бота. Пусть человек "
                             "нажмёт «Старт» в боте — или пришли его Telegram ID вместо ника.")
        return
    offer = await create_offer(user_id, option, price, until, f"/offer от {message.from_user.id}")
    if await _check(offer, user_id):
        await db.cancel_course_offer(offer["token"])
        await message.answer(f"У {escape(_who(user, user_id))} уже есть «{escape(option['title'])}» — "
                             "предложение не создал.")
        return
    logger.info(f"offer: {message.from_user.id} создал {offer['token']} для {user_id}: "
                f"{option['slug']} {price} ₽ до {until}")
    consent = ("" if not config.POLICY_REQUIRED or await db.policy_status(user_id) == "ok" else
               "\n\nСогласий с документами у человека ещё нет — ссылка сначала откроет бот "
               "с галочками, потом это предложение.")
    link = pay_link(offer, option)
    client_text, warnings = None, []
    if template.strip():
        client_text, unknown = render_template(template, {
            "ссылка": link, "цена": svc.rub(price), "старая_цена": svc.rub(full),
            "срок": svc.date_text(until.isoformat()),
            "тариф": TARIFF_NAMES.get(option["slug"], option["title"])})
        if "{ссылка}" not in template:
            warnings.append("В тексте клиенту нет {ссылка} — ссылку вставь сам (она выше).")
        if unknown:
            warnings.append(f"Не знаю метки {', '.join(unknown)} — оставил как есть. "
                            "Есть: {ссылка}, {цена}, {старая_цена}, {срок}, {тариф}.")
    await message.answer(
        f"🤍 <b>Личное предложение</b> для {escape(_who(user, user_id))}\n"
        f"{escape(option['title'])} — <b>{svc.rub(price)}</b>"
        + (f" вместо {svc.rub(full)}" if price < full else "")
        + f"\nДо {svc.date_text(until.isoformat())} включительно, 23:59 МСК. "
          f"Код <code>{offer['token']}</code>{consent}\n\n"
          f"{link}\n\n"
          f"Отменить: <code>/offer_cancel {offer['token']}</code> · все: /offers"
        + "".join(f"\n\n⚠️ {escape(w)}" for w in warnings),
        parse_mode="HTML", disable_web_page_preview=True)
    if client_text:
        # Только текст клиенту — без разметки и пометок, чтобы скопировать в личку
        await message.answer(client_text, parse_mode=None, entities=None,
                             disable_web_page_preview=True, protect_content=False)


@admin_router.message(Command("offers"))
async def cmd_offers(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    offers = await db.get_active_course_offers(svc.now_msk().date().isoformat())
    if not offers:
        await message.answer("Активных личных предложений нет. Создать: /offer")
        return
    lines = ["🤍 <b>Активные личные предложения</b>"]
    for o in offers:
        kinds = {e["kind"] for e in await db.get_course_offer_events(o["token"])}
        state = ("купил" if "buy" in kinds else "перешёл к оплате"
                 if kinds & {"pay_paw", "pay_lava"} else "открыл" if "open" in kinds else "не открывал")
        lines.append(f"\n<b>{escape(_who(o, o['user_id']))}</b> — {escape(o['title'] or '?')}, "
                     f"{svc.rub(o['price'])}, до {datetime.strptime(o['valid_until'], '%Y-%m-%d'):%d.%m}"
                     f" · {state}\n<code>/offer_cancel {o['token']}</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@admin_router.message(Command("offer_cancel"))
async def cmd_offer_cancel(message: Message, command: CommandObject):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    token = (command.args or "").strip().removeprefix(PREFIX)
    if not token:
        await message.answer("Формат: <code>/offer_cancel КОД</code> — коды в /offers.",
                             parse_mode="HTML")
        return
    if await db.cancel_course_offer(token):
        logger.info(f"offer: {message.from_user.id} отменил {token}")
        await message.answer(f"Отменил предложение <code>{escape(token)}</code> — по ссылке теперь "
                             "обычные цены.", parse_mode="HTML")
    else:
        await message.answer("Такого действующего предложения нет — коды в /offers.")
