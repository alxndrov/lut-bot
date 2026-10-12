"""Заказы по курсу — то, что мы должны клиенту исполнить (как заказы пластика).

🎬 Разбор — первые покупатели пакета присылают ролик во вкладке «Разбор»
   мини-приложения. Миша отвечает на карточку ссылкой на видеоразбор (или
   небольшим видео) — бот отправляет его клиенту и закрывает заказ.
📞 Созвон — покупатель VIP жмёт «Договориться о созвоне». Договорились
   в личке — админ жмёт «📅 Назначить время», бот подтверждает клиенту
   и напоминает обоим за день и за час. Потом — «Проведён» / «Не состоялся».

Карточки приходят в malimadmins обоим админам; кнопки на них обновляются
у всех сразу. /courseorders — что в работе. Номер заказа — «К-{id}».
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo,
)

import config
import database as db
from services import course as svc

router = Router()
logger = logging.getLogger(__name__)

MSK = svc.MSK
REVIEW_DAYS = 14            # разбор готовится в течение 14 дней (оферта, п. 7.7)
VIDEO_DAYS_AFTER_OPEN = 30  # ролик присылают в течение 30 дней после старта
RELAY_LIMIT = 20 * 1024 * 1024   # больше этого бот скачать файл не может


class SetCallTime(StatesGroup):
    waiting = State()


# ── Тексты ─────────────────────────────────────────────────────────────────────

def number(order: dict) -> str:
    return f"К-{order['id']}"


def _who(order: dict) -> str:
    name = escape(order.get("first_name") or "—")
    nick = f" @{escape(order['username'])}" if order.get("username") else ""
    return f'{name}{nick} · <a href="tg://user?id={order["user_id"]}">написать</a>'


def _due_mark(order: dict) -> str:
    if order["status"] not in ("open", "scheduled"):
        return ""
    left = (datetime.strptime(order["due_at"], "%Y-%m-%d").date() - svc.now_msk().date()).days
    if left < 0:
        return " 🔴 просрочен"
    if left <= 3:
        return " 🟡"
    return ""


def call_time_text(order: dict) -> str:
    """scheduled_at хранится по Москве: 'YYYY-MM-DD HH:MM'."""
    dt = datetime.strptime(order["scheduled_at"], "%Y-%m-%d %H:%M")
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
    return f"{dt.day} {months[dt.month - 1]} в {dt:%H:%M} МСК"


def card_text(order: dict) -> str:
    test = " 🧪 ТЕСТ" if order["test"] else ""
    due = svc.date_text(order["due_at"])
    if order["kind"] == "review":
        lines = [f"🎬 <b>Заказ {number(order)} · Разбор</b>{test}", "",
                 f"👤 {_who(order)}",
                 f"🔗 {escape(order.get('link') or '—')}"]
        if order.get("question"):
            lines.append(f"❓ {escape(order['question'])}")
        lines.append(f"⏳ Сделать до {due}{_due_mark(order)}")
        if order["status"] == "open":
            lines += ["", "<i>Ответь на эту карточку ссылкой на видеоразбор (или видео "
                          "до 20 МБ) — бот отправит его клиенту и закроет заказ.</i>"]
    else:
        lines = [f"📞 <b>Заказ {number(order)} · Созвон 1 час</b>{test}", "",
                 f"👤 {_who(order)}",
                 f"⏳ Провести до {due}{_due_mark(order)}"]
        if order.get("scheduled_at"):
            lines.append(f"📅 {call_time_text(order)}")
        elif order["status"] == "open":
            lines += ["", "<i>Напиши клиенту, договоритесь о времени — и нажми "
                          "«Назначить время».</i>"]
    status = {
        "done": "✅ Разбор отправлен" if order["kind"] == "review" else "✅ Созвон проведён",
        "missed": "❌ Не состоялся",
        "cancelled": "🚫 Отменён",
    }.get(order["status"])
    if status:
        when = svc.msk_text(order.get("done_at"))
        by = f" · {escape(order['done_by'])}" if order.get("done_by") else ""
        how = {"bot": " (через бот)", "self": " (отправил сам)"}.get(order.get("result") or "", "")
        lines += ["", f"<b>{status}</b>{how} {when}{by}"]
    return "\n".join(lines)


def card_keyboard(order: dict) -> InlineKeyboardMarkup | None:
    oid = order["id"]
    rows = []
    if order["kind"] == "review":
        if order["status"] == "open":
            rows.append([InlineKeyboardButton(text="✅ Отправил сам",
                                              callback_data=f"corder_self:{oid}")])
    else:
        if order["status"] == "open":
            rows.append([InlineKeyboardButton(text="📅 Назначить время",
                                              callback_data=f"corder_time:{oid}")])
        elif order["status"] == "scheduled":
            rows.append([InlineKeyboardButton(text="✅ Созвон проведён",
                                              callback_data=f"corder_held:{oid}")])
            rows.append([InlineKeyboardButton(text="📅 Перенести",
                                              callback_data=f"corder_time:{oid}"),
                         InlineKeyboardButton(text="❌ Не состоялся",
                                              callback_data=f"corder_missed:{oid}")])
    if order["status"] in ("done", "missed"):
        rows.append([InlineKeyboardButton(text="↩️ Вернуть в работу",
                                          callback_data=f"corder_reopen:{oid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# ── Отправка и синхронизация карточек ──────────────────────────────────────────

def _admin_bot() -> Bot:
    return Bot(token=config.WAITLIST_BOT_TOKEN)


async def send_cards(order_id: int, note: str = ""):
    """Новая карточка всем админам (из основного бота — через админский)."""
    order = await db.get_course_order(order_id)
    bot = _admin_bot()
    try:
        for admin_id in config.ADMIN_IDS:
            try:
                msg = await bot.send_message(admin_id, (note + "\n\n" if note else "") + card_text(order),
                                             parse_mode="HTML", reply_markup=card_keyboard(order),
                                             disable_web_page_preview=True)
                await db.add_course_order_message(order_id, admin_id, msg.message_id)
            except Exception as e:
                logger.error(f"course order {order_id}: карточка админу {admin_id}: {e}")
    finally:
        await bot.session.close()


async def sync_cards(bot: Bot, order_id: int):
    """Перерисовать все копии карточки (bot — админский)."""
    order = await db.get_course_order(order_id)
    for chat_id, message_id in await db.get_course_order_messages(order_id):
        try:
            await bot.edit_message_text(card_text(order), chat_id=chat_id, message_id=message_id,
                                        parse_mode="HTML", reply_markup=card_keyboard(order),
                                        disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e):
                logger.info(f"course order {order_id}: карточка {chat_id}/{message_id}: {e}")


async def _tell_admins(text: str, order_id: int | None = None):
    """Короткое сообщение админам — ответом на карточку заказа, если она есть."""
    bot = _admin_bot()
    cards = dict(await db.get_course_order_messages(order_id)) if order_id else {}
    try:
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML",
                                       reply_to_message_id=cards.get(admin_id),
                                       allow_sending_without_reply=True)
            except Exception as e:
                logger.error(f"course orders: админу {admin_id}: {e}")
    finally:
        await bot.session.close()


async def _tell_client(user_id: int, text: str, app_button: bool = False) -> bool:
    bot = Bot(token=config.BOT_TOKEN)
    kb = None
    if app_button and config.MINIAPP_URL:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="📱 Приложение курса", web_app=WebAppInfo(url=config.MINIAPP_URL))]])
    try:
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb,
                               disable_web_page_preview=False)
        return True
    except Exception as e:
        logger.error(f"course orders: клиенту {user_id}: {e}")
        return False
    finally:
        await bot.session.close()


# ── Создание (вызывается из API мини-приложения) ───────────────────────────────

async def submit_review(user: dict, link: str, question: str, test: bool) -> dict:
    """Клиент прислал ролик. Пока разбор не отправлен — повторная отправка
    заменяет ролик в том же заказе."""
    current = await db.get_user_course_order(user["id"], "review")
    if current and current["status"] == "open":
        await db.update_course_order(current["id"], link=link, question=question)
        bot = _admin_bot()
        try:
            await sync_cards(bot, current["id"])
        finally:
            await bot.session.close()
        await _tell_admins(f"🔄 Клиент заменил ролик в заказе {number(current)}:\n"
                           f"🔗 {escape(link)}", current["id"])
        return await db.get_course_order(current["id"])
    due = (svc.now_msk().date() + timedelta(days=REVIEW_DAYS)).isoformat()
    oid = await db.create_course_order("review", user["id"], due, test, link, question)
    await send_cards(oid, "🆕 <b>Новый заказ по курсу</b>")
    return await db.get_course_order(oid)


async def request_call(user: dict, call: dict) -> dict:
    """VIP нажал «Договориться о созвоне». Повторное нажатие — напоминание
    админам по тому же заказу, новый заказ не создаётся."""
    current = await db.get_user_course_order(user["id"], "call")
    if current and current["status"] in ("open", "scheduled"):
        await _tell_admins(f"🔔 Клиент ещё раз просит договориться о созвоне — "
                           f"заказ {number(current)}", current["id"])
        return current
    oid = await db.create_course_order("call", user["id"], call["expires_at"], bool(call["test"]))
    await send_cards(oid, "🆕 <b>Новый заказ по курсу</b>")
    return await db.get_course_order(oid)


# ── Кнопки на карточке ─────────────────────────────────────────────────────────

def _actor(callback_or_message) -> str:
    u = callback_or_message.from_user
    return f"@{u.username}" if u.username else (u.first_name or str(u.id))


async def _load(callback: CallbackQuery) -> dict | None:
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return None
    order = await db.get_course_order(int(callback.data.split(":")[1]))
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
    return order


async def _finish(order: dict, status: str, actor: str, result: str | None = None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.update_course_order(order["id"], status=status, done_at=now, done_by=actor,
                                 result=result)


@router.callback_query(F.data.startswith("corder_self:"))
async def cb_sent_self(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return
    if order["status"] != "open":
        await callback.answer("Заказ уже закрыт.")
        return
    await _finish(order, "done", _actor(callback), "self")
    await callback.answer("Отмечено: разбор отправлен ✅")
    await sync_cards(callback.bot, order["id"])


@router.callback_query(F.data.startswith("corder_held:"))
async def cb_held(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return
    await _finish(order, "done", _actor(callback))
    await callback.answer("Созвон проведён ✅")
    await sync_cards(callback.bot, order["id"])


@router.callback_query(F.data.startswith("corder_missed:"))
async def cb_missed(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return
    await _finish(order, "missed", _actor(callback))
    await callback.answer("Отмечено: не состоялся")
    await sync_cards(callback.bot, order["id"])


@router.callback_query(F.data.startswith("corder_reopen:"))
async def cb_reopen(callback: CallbackQuery):
    order = await _load(callback)
    if not order:
        return
    status = "scheduled" if order["kind"] == "call" and order.get("scheduled_at") else "open"
    await db.update_course_order(order["id"], status=status, done_at=None, done_by=None,
                                 result=None)
    await callback.answer("Вернул в работу")
    await sync_cards(callback.bot, order["id"])


@router.callback_query(F.data.startswith("corder_time:"))
async def cb_time(callback: CallbackQuery, state: FSMContext):
    order = await _load(callback)
    if not order:
        return
    await state.set_state(SetCallTime.waiting)
    await state.update_data(order_id=order["id"])
    await callback.answer()
    await callback.message.answer(
        f"📅 Когда созвон по заказу {number(order)}? Напиши дату и время по Москве, "
        f"например <code>14.11 19:00</code>.\nКрайний срок — {svc.date_text(order['due_at'])}. "
        f"Передумал — /cancel.", parse_mode="HTML")


def parse_call_time(text: str, now: datetime) -> datetime | None:
    """'14.11 19:00', '14.11.2026 19:00', '14.11 в 19:00' → datetime по Москве."""
    m = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\D+(\d{1,2})[:.](\d{2})", text)
    if not m:
        return None
    day, month, year, hour, minute = m.groups()
    year = int(year) + (2000 if year and len(year) == 2 else 0) if year else now.year
    try:
        dt = datetime(year, int(month), int(day), int(hour), int(minute), tzinfo=MSK)
    except ValueError:
        return None
    if not m.group(3) and dt < now - timedelta(days=1):
        dt = dt.replace(year=dt.year + 1)
    return dt


@router.message(Command("cancel"), StateFilter(SetCallTime.waiting))
async def cmd_cancel_time(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, время не меняю.")


@router.message(StateFilter(SetCallTime.waiting), F.text)
async def msg_call_time(message: Message, state: FSMContext):
    order = await db.get_course_order((await state.get_data()).get("order_id") or 0)
    if not order:
        await state.clear()
        return
    when = parse_call_time(message.text, svc.now_msk())
    if not when:
        await message.answer("Не понял время. Напиши так: <code>14.11 19:00</code>",
                             parse_mode="HTML")
        return
    if when < svc.now_msk():
        await message.answer("Это время уже прошло. Напиши другое.")
        return
    late = when.date().isoformat() > order["due_at"]
    await state.clear()
    moved = bool(order.get("scheduled_at"))
    await db.update_course_order(order["id"], status="scheduled",
                                 scheduled_at=when.strftime("%Y-%m-%d %H:%M"),
                                 reminded_day_at=None, reminded_hour_at=None)
    order = await db.get_course_order(order["id"])
    await sync_cards(message.bot, order["id"])
    sent = await _tell_client(
        order["user_id"],
        f"📞 Созвон с Мишей {'перенесли на' if moved else '—'} <b>{call_time_text(order)}</b>, "
        f"{escape(config.COURSE_CALL_WHERE)}. Длительность — до часа.\n\n"
        "Напомню за день и за час. Если планы поменяются — просто напиши сюда.")
    await message.answer(
        f"✅ Созвон {number(order)}: {call_time_text(order)}."
        + (" Клиенту отправил подтверждение." if sent else
           " ⚠️ Клиенту написать не удалось — предупреди его сам.")
        + ("\n⚠️ Это позже срока по оферте — убедись, что клиент согласен." if late else ""))


# ── Разбор: ответ на карточку ссылкой или видео ────────────────────────────────

async def _review_reply_filter(message: Message):
    if not message.reply_to_message or message.from_user.id not in config.ADMIN_IDS:
        return False
    oid = await db.course_order_by_card(message.chat.id, message.reply_to_message.message_id)
    if not oid:
        return False
    order = await db.get_course_order(oid)
    if not order or order["kind"] != "review":
        return False
    return {"course_order": order}


@router.message(_review_reply_filter)
async def msg_review_result(message: Message, course_order: dict):
    order = course_order
    if order["status"] != "open":
        await message.reply("Этот разбор уже закрыт. Если нужно отправить ещё раз — "
                            "нажми «↩️ Вернуть в работу» и ответь снова.")
        return
    video = message.video or message.document
    link = re.search(r"https?://\S+", message.text or message.caption or "")
    intro = "🎬 <b>Миша записал тебе видеоразбор</b> 🤍"
    main_bot = Bot(token=config.BOT_TOKEN)
    try:
        if video:
            if (video.file_size or 0) > RELAY_LIMIT:
                await message.reply("⚠️ Файл больше 20 МБ — бот не может его переслать. "
                                    "Загрузи видео в Kinescope или на Диск и ответь на "
                                    "карточку ссылкой.")
                return
            from handlers.support import relay_media
            await main_bot.send_message(order["user_id"], intro, parse_mode="HTML")
            await relay_media(message, main_bot, order["user_id"])
        elif link:
            extra = (message.text or "").strip()
            await main_bot.send_message(order["user_id"], f"{intro}\n\n{escape(extra)}",
                                        parse_mode="HTML")
        else:
            await message.reply("Пришли ссылку на видеоразбор (или видео до 20 МБ) "
                                "ответом на карточку.")
            return
    except Exception as e:
        logger.error(f"course order {order['id']}: разбор клиенту: {e}")
        await message.reply("⚠️ Не удалось отправить клиенту (заблокировал бота?). "
                            "Отправь сам и нажми «✅ Отправил сам».")
        return
    finally:
        await main_bot.session.close()
    await _finish(order, "done", _actor(message), "bot")
    await sync_cards(message.bot, order["id"])
    await message.reply(f"✅ Разбор отправлен клиенту, заказ {number(order)} закрыт.")


# ── /courseorders ──────────────────────────────────────────────────────────────

async def waiting_summary() -> tuple[int, int]:
    """Сколько ещё впереди: место на разбор есть, ролик не прислан;
    созвон VIP положен, но не запрошен (и срок не вышел)."""
    today = svc.now_msk().date().isoformat()
    no_video = 0
    for uid in await db.get_bundle_review_holders():
        if not await db.get_user_course_order(uid, "review"):
            no_video += 1
    no_call = 0
    for call in await db.get_course_calls():
        if call["test"] or call["done_at"] or call["expires_at"] < today:
            continue
        if not await db.get_user_course_order(call["user_id"], "call"):
            no_call += 1
    return no_video, no_call


@router.message(Command("courseorders"))
async def cmd_course_orders(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    orders = await db.get_open_course_orders()
    no_video, no_call = await waiting_summary()
    head = [f"📋 <b>Заказы по курсу в работе: {len(orders)}</b>"]
    if no_video or no_call:
        head.append(f"Впереди: ждём ролик — {no_video}, созвон не запрошен — {no_call}")
    await message.answer("\n".join(head), parse_mode="HTML")
    for order in orders:
        line = (f"{'🎬' if order['kind'] == 'review' else '📞'} <b>{number(order)}</b> · "
                f"{escape(order.get('first_name') or '—')} · до {svc.date_text(order['due_at'])}"
                f"{_due_mark(order)}")
        if order.get("scheduled_at"):
            line += f" · 📅 {call_time_text(order)}"
        card = dict(await db.get_course_order_messages(order["id"])).get(message.chat.id)
        if card:
            await message.answer(line, parse_mode="HTML", reply_to_message_id=card,
                                 allow_sending_without_reply=True)
        else:
            msg = await message.answer(card_text(order), parse_mode="HTML",
                                       reply_markup=card_keyboard(order),
                                       disable_web_page_preview=True)
            await db.add_course_order_message(order["id"], message.chat.id, msg.message_id)
    if not orders:
        await message.answer("Всё исполнено 🎉")


async def report_line() -> str:
    """Строка для четверговой сводки."""
    orders = [o for o in await db.get_open_course_orders() if not o["test"]]
    late = sum(1 for o in orders if _due_mark(o).startswith(" 🔴"))
    no_video, no_call = await waiting_summary()
    if not (orders or no_video or no_call):
        return ""
    return (f"📋 Заказы по курсу: в работе <b>{len(orders)}</b>"
            + (f", просрочено <b>{late}</b> 🔴" if late else "")
            + f" · ждём ролик — {no_video}, созвон не запрошен — {no_call}. /courseorders")


# ── Напоминания ────────────────────────────────────────────────────────────────

async def reminder_tick(now: datetime | None = None):
    now = now or svc.now_msk()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Созвоны: за сутки и за час — клиенту и админам
    for order in await db.get_open_course_orders():
        if order["kind"] != "call" or order["status"] != "scheduled" or not order["scheduled_at"]:
            continue
        when = datetime.strptime(order["scheduled_at"], "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
        left = when - now
        if left <= timedelta(0):
            continue
        if left <= timedelta(hours=1, minutes=5) and not order["reminded_hour_at"]:
            await db.update_course_order(order["id"], reminded_hour_at=stamp, reminded_day_at=stamp)
            await _tell_client(order["user_id"],
                               f"⏰ Через час созвон с Мишей — {call_time_text(order)}, "
                               f"{escape(config.COURSE_CALL_WHERE)}.")
            await _tell_admins(f"⏰ Через час созвон {number(order)} с "
                               f"{escape(order.get('first_name') or 'клиентом')}", order["id"])
        elif left <= timedelta(hours=24) and not order["reminded_day_at"]:
            await db.update_course_order(order["id"], reminded_day_at=stamp)
            await _tell_client(order["user_id"],
                               f"📞 Напоминаю: завтра созвон с Мишей — {call_time_text(order)}, "
                               f"{escape(config.COURSE_CALL_WHERE)}.")
            await _tell_admins(f"📅 Завтра созвон {number(order)} — {call_time_text(order)}",
                               order["id"])

    if not svc.is_open(now):
        return
    today = now.date()
    # Ролик на разбор: за 7 дней до конца срока
    video_deadline = (svc._msk_date_start(config.COURSE_OPEN_AT).date()
                      + timedelta(days=VIDEO_DAYS_AFTER_OPEN))
    if timedelta(0) <= video_deadline - today <= timedelta(days=7):
        for uid in await db.get_bundle_review_holders():
            if await db.get_user_course_order(uid, "review"):
                continue
            if await db.mark_course_nudge(uid, "review_video"):
                await _tell_client(
                    uid, f"🎬 Напоминаю про подарок: Миша ждёт твой ролик на разбор (до 3 минут). "
                         f"Прислать можно до {svc.date_text(video_deadline.isoformat())} — "
                         f"во вкладке «Разбор» приложения курса.", app_button=True)
    # Созвон VIP не запрошен: за 14 дней до конца срока
    for call in await db.get_course_calls():
        if call["test"] or call["done_at"]:
            continue
        expires = datetime.strptime(call["expires_at"], "%Y-%m-%d").date()
        if not timedelta(0) <= expires - today <= timedelta(days=14):
            continue
        if await db.get_user_course_order(call["user_id"], "call"):
            continue
        if await db.mark_course_nudge(call["user_id"], "call_request"):
            await _tell_client(
                call["user_id"], f"📞 Твой созвон с Мишей (VIP) ещё не назначен — "
                                 f"его нужно провести до {svc.date_text(call['expires_at'])}. "
                                 f"Нажми «Договориться о созвоне» во вкладке «Разбор».",
                app_button=True)


async def reminder_loop():
    while True:
        try:
            await reminder_tick()
        except Exception:
            logger.exception("course orders: reminder tick failed")
        await asyncio.sleep(300)
