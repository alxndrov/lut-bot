"""Оплата в админском боте (malimadmins): /payments.

Куда ведут ссылки (ЮKassa или Prodamus с запасной ЮKassa), сводка за сутки,
проверочный платёж на 1 ₽ и рассылка новых ссылок тем, у кого на руках
остались старые — прямые на Prodamus, которые переключение не затрагивает.
"""
import asyncio
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import database as db
from handlers.pay_router import PROVIDER_SETTING, current_provider
from services import paylinks, yookassa

router = Router()
router.message.filter(F.from_user.id.in_(config.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))
logger = logging.getLogger(__name__)

RECENT_DAYS = 30   # незавершённые заказы для рассылки — не старше


async def _screen() -> tuple[str, InlineKeyboardMarkup]:
    provider = await current_provider()
    s = await db.pay_stats(24)
    lines = ["💳 <b>Оплата</b>", ""]
    if provider == "yookassa":
        lines.append("Ссылки ведут в <b>ЮKassa</b> — все кнопки «Оплатить» в боте, "
                     "и новые, и уже отправленные.")
    else:
        lines.append("Ссылки ведут в <b>Prodamus</b>. Если через "
                     f"{config.PAY_BACKUP_DELAY_MIN} мин после перехода оплаты нет, "
                     "бот спросит клиента и даст запасную ссылку ЮKassa"
                     + ("." if config.YOOKASSA_ENABLED else " — когда ЮKassa будет подключена."))
    lines.append("")
    lines.append("ЮKassa: " + ("✅ подключена" if config.YOOKASSA_ENABLED else
                               "❌ не подключена — нужны YOOKASSA_SHOP_ID и "
                               "YOOKASSA_SECRET_KEY в .env"))
    if not config.PAY_BASE_URL:
        lines.append("⚠️ PAY_BASE_URL не задан — ссылки идут прямо в Prodamus, "
                     "переключение не работает.")
    lines += ["", f"За сутки: переходов к оплате {s['clicks']}, из них оплачено {s['paid']}, "
                  f"запасных ссылок {s['backups']}; через ЮKassa — {s['yk_count']} "
                  f"на {s['yk_sum']} ₽."]

    rows = []
    if config.YOOKASSA_ENABLED:
        rows.append([InlineKeyboardButton(
            text="↩️ Вернуть Prodamus (+ запасная ЮKassa)" if provider == "yookassa"
            else "🔀 Все ссылки — в ЮKassa",
            callback_data="pay:mode:" + ("prodamus" if provider == "yookassa" else "yookassa"))])
        rows.append([InlineKeyboardButton(text="🧪 Проверить ЮKassa (платёж на 1 ₽)",
                                          callback_data="pay:test")])
    rows.append([InlineKeyboardButton(text="📨 Разослать новые ссылки",
                                      callback_data="pay:bc")])
    rows.append([InlineKeyboardButton(text="📣 Оплата работает + зарубежные карты",
                                      callback_data="pay:fbc")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("payments"))
async def cmd_payments(message: Message):
    text, kb = await _screen()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("pay:mode:"))
async def cb_mode(callback: CallbackQuery):
    mode = callback.data.split(":")[2]
    await db.set_setting(PROVIDER_SETTING, mode)
    logger.info(f"payments: {callback.from_user.id} переключил оплату на {mode}")
    await callback.answer("ЮKassa" if mode == "yookassa" else "Prodamus")
    text, kb = await _screen()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "pay:test")
async def cb_test(callback: CallbackQuery):
    """Настоящий платёж на 1 ₽ — проверить ключи и страницу ЮKassa целиком.
    После оплаты придёт «тестовый платёж прошёл», товар не выдаётся."""
    await callback.answer()
    url = paylinks.link(f"t_{callback.from_user.id}_0", 1, "Проверка оплаты MALIMABI",
                        via="yk")
    await callback.message.answer(
        "🧪 Ссылка на настоящий платёж в 1 ₽. Оплатите — через несколько секунд "
        "сюда придёт подтверждение. Так проверяются и ключи, и страница оплаты, "
        "и то, что бот узнаёт об оплате.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💳 Оплатить 1 ₽", url=url)]]))


# ── Рассылка новых ссылок ──────────────────────────────────────────────────────

ORDER_TEXT = (
    "Привет! 👋\n"
    "Вы оформляли заказ «{name}», но до оплаты дело не дошло. Возможно, "
    "мешала страница оплаты — простите, если так 🙏\n\n"
    "Мы подключили новый способ оплаты: карта, SberPay или T-Pay. Заказ сохранился — "
    "ответы, пункт выдачи и сумма, {amount} ₽.\n\n"
    "Если он ещё актуален, оплатить можно кнопкой ниже. А если передумали, "
    "просто не обращайте внимания на это сообщение 🙂"
)
COURSE_TEXT = (
    "Привет! 👋\n"
    "Вы заглядывали в курс, но так его и не купили. Если дело было в оплате — "
    "простите 🙏 Мы подключили новый способ: карта, SberPay или T-Pay.\n\n"
    "Если курс ещё интересен, открыть его можно кнопкой ниже 🙂"
)


# Рассылка «оплата работает, принимаем зарубежные карты» (17.10.2026: Prodamus
# подключил зарубежные карты; кто из неоплативших хотел платить ими — неизвестно,
# поэтому пишем всем, универсально)
ORDER_FOREIGN_TEXT = (
    "Привет! 👋\n"
    "Оплата в боте работает в штатном режиме — а ещё теперь можно оплатить "
    "зарубежной картой (Visa, Mastercard).\n\n"
    "Ваш заказ «{name}» сохранился — ответы, пункт выдачи и сумма, {amount} ₽. "
    "Если он ещё актуален, оплатить можно кнопкой ниже. А если передумали, "
    "просто не обращайте внимания на это сообщение 🙂"
)
COURSE_FOREIGN_TEXT = (
    "Привет! 👋\n"
    "Вы заглядывали в курс «Shot on iPhone + Cut by MALIMABI». Оплата работает "
    "в штатном режиме — а ещё теперь можно оплатить зарубежной картой "
    "(Visa, Mastercard).\n\n"
    "Если курс ещё интересен, открыть его можно кнопкой ниже 🙂"
)


async def _recent_orders() -> list[dict]:
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    return [o for o in await db.get_pending_orders()
            if (o.get("created_at") or "") >= since and not o.get("friend_by")
            and (o.get("amount") or 0) > 0]


async def _course_lookers() -> list[dict]:
    return await db.get_course_lookers(exclude=config.ADMIN_IDS)


@router.callback_query(F.data == "pay:bc")
async def cb_broadcast(callback: CallbackQuery):
    await callback.answer()
    orders, lookers = await _recent_orders(), await _course_lookers()
    sample = ORDER_TEXT.format(name="Microphone Handled Kit", amount=1351)
    await callback.message.answer(
        "📨 <b>Новые ссылки на оплату</b>\n\n"
        "У кого остались старые ссылки, те ведут прямо в Prodamus — переключение "
        "на них не действует. Можно разослать новые, они ведут туда, куда сейчас "
        "выбрано в /payments.\n\n"
        f"• Незавершённые заказы за {RECENT_DAYS} дней: <b>{len(orders)}</b> — "
        "у каждого своя кнопка оплаты с его суммой и «Есть вопрос».\n"
        f"• Смотрели курс и не купили: <b>{len(lookers)}</b> — кнопка «Открыть курс».\n\n"
        f"<b>Текст для заказов:</b>\n<blockquote>{escape(sample)}</blockquote>\n"
        f"<b>Текст для курса:</b>\n<blockquote>{escape(COURSE_TEXT)}</blockquote>\n"
        "Отправка — после кнопки ниже, по одному разу каждому.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📨 Заказам ({len(orders)})",
                                  callback_data="pay:bcgo:orders")],
            [InlineKeyboardButton(text=f"📨 Курсу ({len(lookers)})",
                                  callback_data="pay:bcgo:course")],
        ]))


@router.callback_query(F.data == "pay:fbc")
async def cb_foreign_broadcast(callback: CallbackQuery):
    await callback.answer()
    orders, lookers = await _recent_orders(), await _course_lookers()
    sample = ORDER_FOREIGN_TEXT.format(name="Microphone Handled Kit", amount=1351)
    await callback.message.answer(
        "📣 <b>Оплата работает + зарубежные карты</b>\n\n"
        f"• Незавершённые заказы за {RECENT_DAYS} дней: <b>{len(orders)}</b> — кнопки "
        "«Оплатить» (куда выбрано в /payments), «Оплатить зарубежной картой» (всегда "
        "Prodamus) и «Есть вопрос».\n"
        f"• Смотрели курс и не купили: <b>{len(lookers)}</b> — кнопка «Открыть курс».\n\n"
        f"<b>Текст для заказов:</b>\n<blockquote>{escape(sample)}</blockquote>\n"
        f"<b>Текст для курса:</b>\n<blockquote>{escape(COURSE_FOREIGN_TEXT)}</blockquote>\n"
        "⚠️ Перед отправкой убедись, что зарубежные карты включены в обоих магазинах "
        "Prodamus: физтовары — malimabi-store, курс — baryshovv.\n\n"
        "Отправка — после кнопки ниже, по одному разу каждому.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📣 Заказам ({len(orders)})",
                                  callback_data="pay:bcgo:forders")],
            [InlineKeyboardButton(text=f"📣 Курсу ({len(lookers)})",
                                  callback_data="pay:bcgo:fcourse")],
        ]))


@router.callback_query(F.data.startswith("pay:bcgo:"))
async def cb_broadcast_go(callback: CallbackQuery):
    audience = callback.data.split(":")[2]
    await callback.answer("Отправляю…")
    await callback.message.edit_reply_markup(reply_markup=None)
    if audience == "orders":
        sent, failed = await _send_orders()
    elif audience == "forders":
        sent, failed = await _send_orders(ORDER_FOREIGN_TEXT, foreign=True)
    elif audience == "fcourse":
        sent, failed = await _send_course(COURSE_FOREIGN_TEXT)
    else:
        sent, failed = await _send_course()
    await callback.message.answer(
        f"✅ Отправлено: {sent}" + (f", не дошло: {failed} (бот заблокирован)" if failed else ""))


async def _send_orders(text: str = ORDER_TEXT, foreign: bool = False) -> tuple[int, int]:
    from handlers.admin import _pending_payment_name
    main_bot = Bot(token=config.BOT_TOKEN)
    sent = failed = 0
    try:
        for o in await _recent_orders():
            product = await db.get_product(o["product_id"])
            name = await _pending_payment_name(o, product)
            amount = int(o["amount"])
            order_num = f"p_{o['user_id']}_{o['product_id']}"
            url = paylinks.link(order_num, amount, name)
            rows = [[InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=url)]]
            if foreign:   # зарубежные карты принимает только Prodamus
                rows.append([InlineKeyboardButton(
                    text="🌍 Оплатить зарубежной картой",
                    url=paylinks.link(order_num, amount, name, via="pd"))])
            rows.append([InlineKeyboardButton(text="💬 Есть вопрос",
                                              callback_data=f"pn:ask:{o['product_id']}")])
            try:
                await main_bot.send_message(
                    o["user_id"], text.format(name=name, amount=amount),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                await db.mark_pending_nudged(o["user_id"], o["product_id"])
                sent += 1
            except Exception as e:
                logger.warning(f"payments: рассылка заказу {o['user_id']}: {e}")
                failed += 1
            await asyncio.sleep(0.1)
    finally:
        await main_bot.session.close()
    return sent, failed


async def _send_course(text: str = COURSE_TEXT) -> tuple[int, int]:
    main_bot = Bot(token=config.BOT_TOKEN)
    sent = failed = 0
    try:
        for v in await _course_lookers():
            try:
                await main_bot.send_message(
                    v["user_id"], text,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🎬 Открыть курс", callback_data="course:show")]]))
                sent += 1
            except Exception as e:
                logger.warning(f"payments: рассылка курсу {v['user_id']}: {e}")
                failed += 1
            await asyncio.sleep(0.1)
    finally:
        await main_bot.session.close()
    return sent, failed
