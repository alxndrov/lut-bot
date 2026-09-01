"""
/help — поддержка в основном боте: вопрос клиента уходит всем админам
в malimadmins, ответ приходит обратно сюда же (см. handlers/support_admin.py).
"""
import logging
import re

from aiogram import Router, Bot, F
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, InlineKeyboardMarkup, InlineKeyboardButton,
                           BufferedInputFile)

import config
import database as db

router = Router()
logger = logging.getLogger(__name__)


class SupportState(StatesGroup):
    waiting_text = State()


# Медиа между двумя ботами: copy_message тут не работает — админский бот не
# видит чат клиента («chat not found»), а file_id у каждого бота свой.
# Поэтому качаем файл основным ботом и заливаем админским.
_MEDIA = (
    ("photo", "send_photo", "photo.jpg"),
    ("video", "send_video", "video.mp4"),
    ("voice", "send_voice", "voice.ogg"),
    ("video_note", "send_video", "video.mp4"),
    ("animation", "send_animation", "animation.mp4"),
    ("audio", "send_audio", "audio.mp3"),
    ("document", "send_document", None),
)


async def relay_media(message: Message, to_bot: Bot, chat_id: int):
    """Пересылает вложение клиента админскому боту. Возвращает Message или None."""
    for attr, method, default_name in _MEDIA:
        obj = getattr(message, attr, None)
        if not obj:
            continue
        obj = obj[-1] if isinstance(obj, (list, tuple)) else obj      # photo — список размеров
        try:
            file = await message.bot.get_file(obj.file_id)
            buf = await message.bot.download_file(file.file_path)
            name = getattr(obj, "file_name", None) or default_name or "file"
            data = BufferedInputFile(buf.read(), filename=name)
            return await getattr(to_bot, method)(chat_id, data,
                                                 caption=message.caption or None)
        except Exception as e:
            logger.error(f"relay_media ({attr}): {e}")
            return await to_bot.send_message(
                chat_id, f"⚠️ Вложение ({attr}) переслать не удалось: {e}")
    return None


async def notify_admins(message: Message, header: str, user_id: int,
                        bot_token: str | None = None, order: dict | None = None) -> bool:
    """Шапка + само сообщение клиента всем админам в malimadmins.

    Если у клиента есть заказ, вопрос уходит ОТВЕТОМ на карточку этого
    заказа: спрашивают почти всегда про заказ, и искать его вручную по
    нику — лишняя работа. Карточка у каждого админа своя, поэтому
    message_id ищем по его чату.

    Каждое отправленное сообщение регистрируем в support_messages —
    ответом (Reply) на любое из них админ отвечает клиенту.
    """
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответить", callback_data=f"sup_reply:{user_id}")
    ]])
    ok = False
    bot = Bot(token=bot_token or config.WAITLIST_BOT_TOKEN)
    try:
        for admin_id in config.ADMIN_IDS:
            try:
                card = await db.order_card_in_chat(order["id"], admin_id) if order else None
                if message.text:
                    sent = await bot.send_message(admin_id, f"{header}\n\n{message.text}",
                                                  parse_mode="HTML", reply_markup=kb,
                                                  reply_to_message_id=card)
                    await db.add_support_message(admin_id, sent.message_id, user_id)
                else:
                    # Фото/голос/документ: шапка отдельно, следом вложение
                    sent = await bot.send_message(admin_id, header, parse_mode="HTML",
                                                  reply_markup=kb, reply_to_message_id=card)
                    await db.add_support_message(admin_id, sent.message_id, user_id)
                    copy = await relay_media(message, bot, admin_id)
                    if copy:
                        await db.add_support_message(admin_id, copy.message_id, user_id)
                ok = True
            except Exception as e:
                # Карточку могли удалить — тогда шлём без привязки, лишь бы дошло
                logger.error(f"support: notify admin {admin_id} failed: {e}")
                try:
                    text = f"{header}\n\n{message.text}" if message.text else header
                    sent = await bot.send_message(admin_id, text, parse_mode="HTML",
                                                  reply_markup=kb)
                    await db.add_support_message(admin_id, sent.message_id, user_id)
                    if not message.text:
                        copy = await relay_media(message, bot, admin_id)
                        if copy:
                            await db.add_support_message(admin_id, copy.message_id, user_id)
                    ok = True
                except Exception as e2:
                    logger.error(f"support: и без карточки не ушло админу {admin_id}: {e2}")
    finally:
        await bot.session.close()
    return ok


def order_line(order: dict | None) -> str:
    """Строка о заказе для шапки: номер, этап, трек."""
    if not order:
        return ""
    nums = re.findall(r"\d+", order.get("order_code") or "")
    num = nums[-1] if nums else str(order.get("id"))
    stage = "отправлен" if order.get("shipped_at") else "в работе"
    line = f"🧾 Заказ №{num} ({stage})"
    if order.get("cdek_number"):
        line += f" · СДЭК {order['cdek_number']}"
    return line


async def order_hint(user_id: int) -> tuple[dict | None, str]:
    """Последний заказ клиента и строка о нём для шапки вопроса."""
    order = await db.last_order_of_user(user_id)
    return order, order_line(order)


def client_header(message: Message, title: str, extra: str = "") -> str:
    user = message.from_user
    username = f"@{user.username}" if user.username else f"id:{user.id}"
    header = (f"{title}\n"
              f"👤 {user.first_name or '—'} {username} (id:{user.id})")
    if extra:
        header += f"\n{extra}"
    # Подпись к фото/видео — тоже текст сообщения, показываем её в шапке
    if not message.text and message.caption:
        header += f"\n\n{message.caption}"
    return header


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await state.set_state(SupportState.waiting_text)
    await message.answer(
        "🆘 <b>Поддержка</b>\n\n"
        "Напишите ваш вопрос одним сообщением — я передам его команде, "
        "и вам ответят прямо здесь.\n\n"
        "Чтобы отменить — /cancel.",
        parse_mode="HTML",
    )


@router.message(Command("cancel"), SupportState.waiting_text)
async def cmd_support_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено.")


# Команду не считаем вопросом в поддержку — см. тот же случай в feedback.py
@router.message(SupportState.waiting_text, ~(F.text & F.text.startswith("/")))
async def on_support_message(message: Message, state: FSMContext):
    await state.clear()
    order, hint = await order_hint(message.from_user.id)
    ok = await notify_admins(
        message, client_header(message, "🆘 <b>Вопрос в поддержку</b>", hint),
        message.from_user.id, order=order)
    if not ok:
        await message.answer("⚠️ Не получилось отправить, попробуйте ещё раз чуть позже.")
        return
    await message.answer("✅ Вопрос отправлен! Как только ответят — пришлю сюда.")


# --- Свободное сообщение клиента ---
# Человеку неоткуда знать про /help: он просто пишет в бот — в ответ на
# просьбу об отзыве или продолжая начатую переписку с поддержкой. Раньше
# такие сообщения не ловил никто и они пропадали. Роутер подключается
# ПОСЛЕДНИМ (см. bot.py), чтобы не перехватывать чужие сценарии.
review_router = Router()


@review_router.message(F.text | F.photo | F.video | F.voice | F.document
                       | F.video_note | F.animation | F.audio)
async def on_free_message(message: Message, state: FSMContext):
    # UNHANDLED, а не голый return: так в логе видно, что сообщение не
    # разобрал никто (см. services/message_log.py)
    if await state.get_state() is not None:
        return UNHANDLED                         # человек внутри другого сценария
    if message.text and message.text.startswith("/"):
        return UNHANDLED

    user_id = message.from_user.id
    push = await db.recent_review_push(user_id)
    support_at = await db.recent_support_contact(user_id)
    # Было и то и другое — считаем продолжением того, что случилось позже
    if push and support_at and str(push["send_at"]) > support_at:
        support_at = None

    order, hint = await order_hint(user_id)
    # Отзыв пишут и не дожидаясь просьбы: заказ уже пришёл, человеку есть
    # что сказать. Раньше такое сообщение не подхватывал никто.
    early = None
    if not push and not support_at and order and order.get("shipped_at"):
        early = await db.pending_review_push(user_id)

    if support_at:
        header = client_header(message, "🆘 <b>Сообщение в поддержку</b>", hint)
        reply_text = "✅ Передал команде — ответят здесь же."
    elif push or early:
        p = push or early
        # Отзыв — про тот заказ, по которому просили, а не про самый свежий:
        # у постоянного клиента их несколько, и привязка к последнему врёт
        if p.get("order_id"):
            asked = await db.get_order(p["order_id"])
            if asked:
                order, hint = asked, order_line(asked)
        header = client_header(message, "⭐️ <b>Отзыв о покупке</b>",
                               f"🛍 {p['product_name']}" + (f"\n{hint}" if hint else ""))
        reply_text = "Спасибо за отзыв! 🙏 Передал команде."
    elif order:
        # Клиент с заказом просто пишет в бота — это вопрос, а не пустота
        header = client_header(message, "🆘 <b>Сообщение в поддержку</b>", hint)
        reply_text = "✅ Передал команде — ответят здесь же."
    else:
        return UNHANDLED

    if await notify_admins(message, header, user_id, order=order):
        if early:
            # Отзыв уже есть — просить о нём ещё раз незачем
            await db.drop_review_push(early["id"])
            logger.info(f"review: {user_id} написал сам, просьбу {early['id']} отменяю")
        await message.answer(reply_text)
    else:
        await message.answer("⚠️ Не получилось отправить, попробуйте ещё раз чуть позже.")
