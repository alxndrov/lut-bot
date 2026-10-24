"""/forget — пользователь сам удаляет свои данные из бота (ст. 21 152-ФЗ:
отзыв согласия и удаление). Что удаляется и что остаётся — database.forget_user.
Пока заказ может быть в пути, данные получателя не удаляем: без них СДЭК не
выдаст посылку."""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import database as db
from services.sellers import CONTACT

router = Router()
logger = logging.getLogger(__name__)

CONFIRM_TEXT = (
    "🗑 <b>Удалить мои данные</b>\n\n"
    "Удалим всё, что бот знает о тебе: имя и username, email, данные получателя заказов, "
    "переписку с поддержкой, отзывы, ссылки на видео, неоплаченные корзины и рассылки. "
    "Согласие на обработку данных будет отозвано.\n\n"
    "<b>Останутся</b> только сведения о покупках — сумма, дата, товар и номер платежа: "
    "закон обязывает хранить их 5 лет для налогового учёта.\n\n"
    "⚠️ Бот перестанет тебе писать, а оплаченные товары и курс станут недоступны, "
    "пока ты снова не дашь согласие через /start — тогда доступ к оплаченному вернётся.\n\n"
    "Удалить?"
)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить мои данные", callback_data="forget:yes")],
        [InlineKeyboardButton(text="Отмена", callback_data="forget:no")],
    ])


@router.message(Command("forget"))
async def cmd_forget(message: Message, state: FSMContext):
    await state.clear()
    orders = await db.orders_in_progress(message.from_user.id)
    if orders:
        await message.answer(
            f"У тебя есть заказ в пути (№ {', '.join(map(str, orders))}) — пока он не получен, "
            "удалить данные получателя нельзя: без них служба доставки не выдаст посылку. "
            f"Вызови /forget после получения или напиши на {CONTACT}.")
        return
    await message.answer(CONFIRM_TEXT, parse_mode="HTML", reply_markup=confirm_keyboard())


@router.callback_query(F.data == "forget:no")
async def cb_forget_no(callback: CallbackQuery):
    await callback.answer("Ничего не удалили")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data == "forget:yes")
async def cb_forget_yes(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if await db.orders_in_progress(user_id):   # заказ мог появиться после /forget
        await callback.answer("Есть заказ в пути — удалить данные пока нельзя.", show_alert=True)
        return
    await state.clear()
    await db.forget_user(user_id)
    logger.info(f"forget: пользователь {user_id} удалил свои данные")
    await callback.answer()
    try:
        await callback.message.edit_text(
            "✅ Готово: твои данные удалены, согласие отозвано. Бот больше не будет тебе писать.\n\n"
            "Если захочешь вернуться — нажми /start.")
    except Exception:
        pass
    from handlers.prodamus_webhook import _send_notify
    from services import gsheets
    await _send_notify(None, f"🗑 Пользователь <code>{user_id}</code> удалил свои данные (/forget). "
                             "В учёте остались только покупки. Если его данные есть где-то ещё "
                             "(переписка, свои таблицы) — удалите и там.")
    gsheets.request_finance_sync()   # лист «Курс» — без имени и username
