from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from config import ADMIN_IDS
from keyboards.user import catalog_keyboard, back_to_catalog_keyboard
from keyboards.admin import admin_menu_keyboard
import database as db

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    products = await db.get_all_products()

    if not products:
        await message.answer(
            "👋 Привет! Пока товаров нет, скоро появятся."
        )
        return

    await message.answer(
        "👋 Привет! Выбери товар из каталога:",
        reply_markup=catalog_keyboard(products),
    )


async def show_catalog(message_or_callback, edit=False):
    products = await db.get_all_products()

    if not products:
        text = "😔 Товаров пока нет."
        kb = None
    else:
        text = "📦 Каталог товаров:"
        kb = catalog_keyboard(products)

    if edit:
        await message_or_callback.message.edit_text(text, reply_markup=kb)
    else:
        await message_or_callback.answer(text, reply_markup=kb)
