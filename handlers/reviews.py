"""
Отзывы клиентов списком (malimadmins): /reviews.

Отзыв приходит в чат отдельным сообщением и там же тонет. Здесь он
лежит целиком: кто, про какой заказ, когда и что написал. Вложение
пересылается по кнопке — его хранит основной бот (см. handlers/support.py).
"""
import logging

from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

import config
import database as db
from handlers.expenses import MSK, _nav_row

router = Router()
logger = logging.getLogger(__name__)

PAGE = 5                      # больше в одно сообщение не влезает по длине
TEXT_LIMIT = 600

_KIND_WORDS = {
    "photo": "фото", "video": "видео", "voice": "голосовое",
    "video_note": "кружок", "animation": "гифка", "audio": "аудио",
    "document": "файл",
}


def _msk(ts: str | None) -> str:
    from datetime import datetime, timezone
    if not ts:
        return ""
    try:
        dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(ts)[:16]
    return dt.replace(tzinfo=timezone.utc).astimezone(MSK).strftime("%d.%m %H:%M")


def _num(code: str | None) -> str:
    import re
    nums = re.findall(r"\d+", code or "")
    return nums[-1] if nums else ""


def _one(r: dict, index: int) -> str:
    who = f"@{r['username']}" if r.get("username") else (r.get("first_name") or f"id:{r['user_id']}")
    head = f"<b>{index}.</b> {who}"
    num = _num(r.get("order_code"))
    if num:
        head += f" · заказ №{num}"
    if r.get("product_name"):
        head += f" · {r['product_name']}"
    head += f" · {_msk(r.get('created_at'))}"

    body = (r.get("text") or "").strip()
    if len(body) > TEXT_LIMIT:
        body = body[:TEXT_LIMIT].rstrip() + "…"
    kind = _KIND_WORDS.get(r.get("media_kind") or "")
    if kind and not body:
        body = f"<i>без текста, только {kind}</i>"
    elif kind:
        body += f"\n<i>+ {kind}</i>"
    return f"{head}\n{body or '<i>пусто</i>'}"


async def _page_text(offset: int) -> tuple[str, int, list[dict]]:
    total = await db.count_reviews()
    rows = await db.get_reviews(limit=PAGE, offset=offset)
    if not rows:
        return "⭐️ <b>Отзывы</b>\n\nПока ни одного.", total, rows
    shown_to = offset + len(rows)
    head = f"⭐️ <b>Отзывы</b> — всего {total}\nПоказываю {offset + 1}–{shown_to}"
    body = "\n\n".join(_one(r, offset + i + 1) for i, r in enumerate(rows))
    return f"{head}\n\n{body}", total, rows


def _keyboard(offset: int, total: int, rows: list[dict]) -> InlineKeyboardMarkup:
    kb: list[list[InlineKeyboardButton]] = []
    media = [r for r in rows if r.get("file_id")]
    if media:
        kb.append([InlineKeyboardButton(
            text=f"📎 Прислать вложения ({len(media)})",
            callback_data=f"rev_media:{offset}")])
    nav = []
    if offset:
        nav.append(InlineKeyboardButton(text="⬅️ Новее",
                                        callback_data=f"rev_page:{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav.append(InlineKeyboardButton(text="Старее ➡️",
                                        callback_data=f"rev_page:{offset + PAGE}"))
    if nav:
        kb.append(nav)
    kb += _nav_row("rev")
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def _show(message: Message, offset: int = 0, edit: bool = False):
    text, total, rows = await _page_text(offset)
    markup = _keyboard(offset, total, rows)
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup,
                                    disable_web_page_preview=True)
            return
        except Exception:
            pass
    await message.answer(text, parse_mode="HTML", reply_markup=markup,
                         disable_web_page_preview=True)


@router.message(Command("reviews"))
async def cmd_reviews(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await _show(message)


@router.callback_query(F.data == "rev_show")
async def cb_reviews_show(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    await callback.answer()
    await _show(callback.message, 0, edit=True)


@router.callback_query(F.data.startswith("rev_page:"))
async def cb_reviews_page(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    await callback.answer()
    await _show(callback.message, int(callback.data.split(":", 1)[1]), edit=True)


@router.callback_query(F.data.startswith("rev_media:"))
async def cb_reviews_media(callback: CallbackQuery):
    """Досылает фото/видео отзывов этой страницы.

    Файл лежит у основного бота — качаем им и заливаем админским: file_id
    одного бота для другого недействителен (та же история, что в relay_media).
    """
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Только для администраторов.", show_alert=True)
        return
    offset = int(callback.data.split(":", 1)[1])
    rows = [r for r in await db.get_reviews(limit=PAGE, offset=offset) if r.get("file_id")]
    if not rows:
        await callback.answer("Вложений на этой странице нет.")
        return
    await callback.answer("Досылаю…")

    main_bot = Bot(token=config.BOT_TOKEN)
    try:
        for r in rows:
            who = f"@{r['username']}" if r.get("username") else (r.get("first_name") or "—")
            caption = f"⭐️ {who}" + (f" · заказ №{_num(r.get('order_code'))}"
                                     if r.get("order_code") else "")
            try:
                f = await main_bot.get_file(r["file_id"])
                data = (await main_bot.download_file(f.file_path)).read()
                name = (f.file_path or "file").split("/")[-1]
                await callback.message.answer_document(
                    BufferedInputFile(data, filename=name), caption=caption)
            except Exception as e:
                logger.error(f"review media {r['id']}: {type(e).__name__}: {e}")
                await callback.message.answer(
                    f"⚠️ Вложение отзыва {who} переслать не удалось: {e}")
    finally:
        await main_bot.session.close()
