"""Entry point and compatibility for old admin-menu messages.

The Telegram command menu is the single top-level navigation.
"""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import config

router = Router()

_NAV_HINT = 'Выберите нужный раздел через кнопку «Меню» внизу чата.'


@router.message(Command('start', 'admin', 'menu'))
async def menu(message: Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await state.clear()
    await message.answer(_NAV_HINT)


@router.callback_query(F.data == 'ops_menu')
async def menu_callback(callback: CallbackQuery, state: FSMContext):
    """Old messages remain usable without reopening the removed menu."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer('Только для администраторов', show_alert=True)
        return
    await state.clear()
    await callback.answer(_NAV_HINT, show_alert=True)
