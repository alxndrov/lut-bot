"""
Ответы поддержки (malimadmins): админ жмёт «Ответить» под вопросом
клиента и пишет ответ — он уходит клиенту в основной бот
(см. handlers/support.py).
"""
import logging

from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ForceReply, Message

import config
import database as db
from handlers.support import relay_media

router = Router()
logger = logging.getLogger(__name__)


class SupportReplyState(StatesGroup):
    waiting_answer = State()


@router.callback_query(F.data.startswith("sup_reply:"))
async def cb_support_reply(callback: CallbackQuery, state: FSMContext):
    """Кнопка «Ответить»: следующее сообщение админа уходит клиенту.

    Раньше рассчитывали только на Reply к подсказке. Но на ForceReply
    полагаться нельзя: ответ легко отправить и без цитаты, тогда его не
    ловил никто и он молча пропадал. Поэтому запоминаем, кому отвечаем.
    """
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    user_id = int(callback.data.split(":", 1)[1])
    await callback.answer()
    await state.set_state(SupportReplyState.waiting_answer)
    await state.update_data(support_user_id=user_id)
    prompt = await callback.message.answer(
        "✍️ Напишите ответ клиенту — он придёт ему в основной бот.\n"
        "Отменить — /cancel.",
        reply_markup=ForceReply(),
    )
    # Reply на подсказку тоже работает — через support_messages
    await db.add_support_message(prompt.chat.id, prompt.message_id, user_id)


@router.message(Command("cancel"), SupportReplyState.waiting_answer)
async def cmd_cancel_reply(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Ответ отменён.")


async def _support_reply_filter(message: Message):
    """Реплай админа именно на нашу пересланную копию вопроса.

    Возвращает False на любой другой reply (например, внутри /expense) —
    тогда aiogram передаёт апдейт дальше, другим хендлерам этого бота.
    """
    if not message.reply_to_message or message.from_user.id not in config.ADMIN_IDS:
        return False
    user_id = await db.get_support_user(message.chat.id, message.reply_to_message.message_id)
    if user_id is None:
        return False
    return {"support_user_id": user_id}


async def _send_to_client(message: Message, support_user_id: int) -> bool:
    header = ("💬 <b>Ответ поддержки</b>:" if message.text else
              "💬 <b>Ответ поддержки</b>")
    hint = "\n\n<i>Можно ответить прямо здесь — я передам.</i>"

    main_bot = Bot(token=config.BOT_TOKEN)
    try:
        if message.text:
            await main_bot.send_message(support_user_id,
                                        f"{header}\n\n{message.text}{hint}",
                                        parse_mode="HTML")
        else:
            # copy_message тут не работает: основной бот не видит чат админа
            # с админским ботом. Качаем файл и заливаем заново (см. relay_media).
            await main_bot.send_message(support_user_id, header + hint, parse_mode="HTML")
            await relay_media(message, main_bot, support_user_id)
    except Exception as e:
        logger.error(f"support: reply to {support_user_id} failed: {e}")
        await message.reply("⚠️ Не удалось отправить клиенту (заблокировал бота?).")
        return False
    finally:
        await main_bot.session.close()

    # Свой же ответ тоже кладём в переписку: на него можно ответить Reply,
    # и по нему видно, что разговор с клиентом ещё живой
    await db.add_support_message(message.chat.id, message.message_id, support_user_id)
    await message.reply("✅ Отправлено клиенту.")
    logger.info(f"support: ответ админа {message.from_user.id} → клиенту {support_user_id}")
    return True


# Команду ответом не считаем — иначе /myorders уедет клиенту
@router.message(SupportReplyState.waiting_answer, ~(F.text & F.text.startswith("/")))
async def on_reply_after_button(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    user_id = data.get("support_user_id")
    if not user_id:
        return
    await _send_to_client(message, user_id)


@router.message(_support_reply_filter)
async def on_support_reply(message: Message, support_user_id: int):
    await _send_to_client(message, support_user_id)
