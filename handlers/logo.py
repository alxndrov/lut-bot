"""Ручная конвертация логотипа в SVG в админском боте.

В заказе SVG приходит сам, но логотип часто прилетает и мимо заказа:
клиент прислал файл позже, переслали из личной переписки, старый заказ.
Поэтому любой файл, отправленный админом в бота, тоже конвертируем.
"""
import logging

from aiogram import Router, F
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, Message

import config
from services.logo_svg import to_svg, is_vector

router = Router()
logger = logging.getLogger(__name__)


@router.message(F.photo | F.document)
async def on_logo_file(message: Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_IDS:
        return UNHANDLED
    if await state.get_state() is not None:
        return UNHANDLED                     # админ внутри другого сценария

    obj = message.photo[-1] if message.photo else message.document
    name = getattr(obj, "file_name", None) or "logo.jpg"
    if is_vector(name):
        await message.reply("Это уже вектор — конвертировать нечего.")
        return

    note = await message.reply("⏳ Перевожу в SVG…")
    try:
        f = await message.bot.get_file(obj.file_id)
        data = (await message.bot.download_file(f.file_path)).read()
        result = await to_svg(data, name)
    except Exception as e:
        logger.error(f"logo svg (ручной, {name}): {e}")
        result = None

    try:
        await note.delete()
    except Exception:
        pass

    if not result:
        await message.reply("⚠️ Не получилось: не картинка или фон не отделился "
                            "от рисунка. Пришлите файл с однотонным фоном "
                            "или без фона.")
        return
    svg, svg_name = result
    await message.reply_document(BufferedInputFile(svg, filename=svg_name),
                                 caption="🖤 Логотип в SVG — всё, кроме фона, чёрным")
