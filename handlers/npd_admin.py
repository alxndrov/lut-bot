"""/nalog в malimadmins — чеки «Мой налог» за оплаты ЮKassa.

Статус подключения, сколько чеков пробито и что ждёт, подключение по СМС
(телефон и код вводит сам админ — пароль от «Мой налог» не нужен),
ручной повтор непробитых.
"""
import logging
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import database as db
from services import npd, npd_receipts

router = Router()
logger = logging.getLogger(__name__)


class NpdLogin(StatesGroup):
    phone = State()
    code = State()


def _admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


async def status_screen() -> tuple[str, InlineKeyboardMarkup]:
    rows = await db.get_npd_receipts()
    month = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m")
    issued = [r for r in rows if r["status"] == "issued"]
    waiting = [r for r in rows if r["status"] in ("pending", "failed")]
    ok = await npd.connected()
    inn = await db.get_setting(npd.S_INN) or ""
    lines = ["🧾 <b>Чеки «Мой налог» за оплаты ЮKassa</b>", "",
             f"Подключение: {'✅ ИНН ' + inn if ok else '❌ не подключено'}",
             f"Пробито: {len(issued)}, в этом месяце — "
             f"{sum(1 for r in issued if (r['issued_at'] or '').startswith(month))}"]
    if waiting:
        lines += ["", f"⏳ <b>Ждут: {len(waiting)}</b>"]
        for r in waiting[:10]:
            lines.append(f"• {escape(r['name'])} — {r['amount']:.0f} ₽"
                         + (f" · {escape((r['error'] or '')[:80])}" if r.get("error") else ""))
    lines += ["", "<i>Чеки по оплатам Prodamus делает Prodamus — здесь их нет.</i>"]
    kb = [[InlineKeyboardButton(text="📱 Подключить по СМС" if not ok else "🔁 Переподключить",
                                callback_data="npd:login")]]
    if waiting:
        kb.append([InlineKeyboardButton(text="🧾 Пробить ждущие сейчас", callback_data="npd:retry")])
    if ok:
        kb.append([InlineKeyboardButton(text="Отключить", callback_data="npd:off")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


@router.message(Command("nalog"))
async def cmd_nalog(message: Message, state: FSMContext):
    if not _admin(message.from_user.id):
        return
    await state.clear()
    text, kb = await status_screen()
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "npd:login")
async def cb_login(callback: CallbackQuery, state: FSMContext):
    if not _admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(NpdLogin.phone)
    await callback.answer()
    await callback.message.answer(
        "📱 Номер телефона, к которому привязан «Мой налог» (самозанятость продавца). "
        "Налоговая пришлёт на него СМС с кодом.\n\nОтмена — /cancel")


@router.message(Command("cancel"), StateFilter(NpdLogin.phone, NpdLogin.code))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, подключение отменено.")


@router.message(StateFilter(NpdLogin.phone), F.text)
async def msg_phone(message: Message, state: FSMContext):
    phone = npd.normalize_phone(message.text)
    if not phone:
        await message.answer("Не похоже на номер. Пример: +7 912 345-67-89")
        return
    try:
        challenge = await npd.start_sms(phone)
    except Exception as e:
        await message.answer(f"⚠️ Налоговая не отправила СМС: {escape(str(e)[:200])}",
                             parse_mode="HTML")
        return
    await state.set_state(NpdLogin.code)
    await state.update_data(phone=phone, challenge=challenge)
    await message.answer("Код из СМС от «Мой налог»?")


@router.message(StateFilter(NpdLogin.code), F.text)
async def msg_code(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    try:
        inn = await npd.verify_sms(data["phone"], data["challenge"], message.text)
    except Exception as e:
        await message.answer(f"⚠️ Не подошло: {escape(str(e)[:200])}\nПопробуй ещё раз "
                             f"или /cancel и начни сначала.", parse_mode="HTML")
        return
    await state.clear()
    # Код больше не нужен — убираем его из чата
    try:
        await message.delete()
    except Exception:
        pass
    logger.info(f"npd: подключён ИНН {inn} ({message.from_user.id})")
    await message.answer(f"✅ «Мой налог» подключён (ИНН {inn}). Чеки за оплаты ЮKassa "
                         "теперь пробиваются сами.")
    main_bot = Bot(token=config.BOT_TOKEN)
    try:
        done, left = await npd_receipts.retry_failed(main_bot)
    finally:
        await main_bot.session.close()
    if done or left:
        await message.answer(f"Пробил ждавшие чеки: {done}" + (f", осталось {left}" if left else ""))


@router.callback_query(F.data == "npd:retry")
async def cb_retry(callback: CallbackQuery):
    if not _admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer("Пробиваю…")
    main_bot = Bot(token=config.BOT_TOKEN)
    try:
        done, left = await npd_receipts.retry_failed(main_bot)
    finally:
        await main_bot.session.close()
    await callback.message.answer(f"🧾 Пробито: {done}" + (f", осталось {left} — причина в /nalog"
                                                           if left else ""))


@router.callback_query(F.data == "npd:off")
async def cb_off(callback: CallbackQuery):
    if not _admin(callback.from_user.id):
        await callback.answer()
        return
    await npd.disconnect()
    await callback.answer("Отключено")
    text, kb = await status_screen()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "npd:prodamus_issue")
async def cb_prodamus_issue(callback: CallbackQuery):
    """Продамус не выдал чеки — пробиваем их сами (кнопка из сверки)."""
    if not _admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer("Пробиваю…")
    await callback.message.edit_reply_markup(reply_markup=None)
    main_bot = Bot(token=config.BOT_TOKEN)
    try:
        done, failed = await npd_receipts.issue_prodamus_missing(main_bot)
    finally:
        await main_bot.session.close()
    await db.set_setting(npd_receipts.S_PRODAMUS_ASK, "")
    await callback.message.answer(
        f"🧾 Пробито чеков: {done}" + (f", не вышло: {failed} — бот повторит сам, статус в /nalog"
                                       if failed else ". Клиентам отправлены ссылки."))
