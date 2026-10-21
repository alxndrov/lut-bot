"""
Отзывы клиентов списком (malimadmins): /reviews.

Модерация для магазина: отзыв с текстом можно показать покупателям
(«⭐️ Показать в магазине» — под уведомлением об отзыве и звёздочки в
/reviews) и поправить текст перед показом — например, убрать имя.
Покупатели видят его обезличенно (handlers/shop_reviews.py).

Отзыв приходит в чат отдельным сообщением и там же тонет. Здесь он
лежит целиком: кто, про какой заказ, когда и что написал. Вложение
пересылается по кнопке — его хранит основной бот (см. handlers/support.py).
"""
import logging

from aiogram import Router, Bot, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

import config
import database as db
from handlers.expenses import MSK

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
    if r.get("published"):
        head += " · ⭐️ <b>в магазине</b>"
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
    # ☆ N — показать отзыв N в магазине, ⭐️ N — уже показан (нажать — убрать)
    toggles = [InlineKeyboardButton(
        text=f"{'⭐️' if r.get('published') else '☆'} {offset + i + 1}",
        callback_data=f"rvtog:{r['id']}:{offset}")
        for i, r in enumerate(rows) if (r.get("text") or "").strip()]
    if toggles:
        kb.append(toggles)
    edits = [InlineKeyboardButton(text=f"✏️ {offset + i + 1}", callback_data=f"rvedit:{r['id']}")
             for i, r in enumerate(rows) if r.get("published")]
    if edits:
        kb.append(edits)
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
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def _show(message: Message, offset: int = 0, edit: bool = False):
    text, total, rows = await _page_text(offset)
    if any((r.get("text") or "").strip() for r in rows):
        text += ("\n\n<i>☆ N — показать отзыв в магазине (без имени), ⭐️ N — уже там, "
                 "нажми, чтобы убрать. ✏️ N — поправить текст для магазина.</i>")
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


# ── Отзывы в магазине: модерация ──────────────────────────────────────────────

class ReviewEdit(StatesGroup):
    text = State()


def publish_row(review_id: int, published: bool) -> list[InlineKeyboardButton]:
    if published:
        return [InlineKeyboardButton(text="✅ В магазине — убрать", callback_data=f"rvunpub:{review_id}"),
                InlineKeyboardButton(text="✏️ Поправить текст", callback_data=f"rvedit:{review_id}")]
    return [InlineKeyboardButton(text="⭐️ Показать в магазине", callback_data=f"rvpub:{review_id}")]


async def _preview(review_id: int) -> str:
    from handlers.shop_reviews import review_text
    r = await db.get_review(review_id) or {}
    body = (r.get("public_text") or "").strip() or r.get("text") or ""
    return review_text({"body": body, "product_name": r.get("product_name"),
                        "created_at": r.get("created_at")})


async def _set_published(callback: CallbackQuery, review_id: int, published: bool) -> dict | None:
    r = await db.get_review(review_id)
    if not r or not (r.get("text") or "").strip():
        await callback.answer("Отзыв без текста — в магазин не попадёт.", show_alert=True)
        return None
    await db.set_review_published(review_id, published)
    logger.info(f"reviews: отзыв {review_id} {'показан' if published else 'убран'} "
                f"({callback.from_user.id})")
    return r


@router.callback_query(F.data.regexp(r"^rvu?n?pub:\d+$"))
async def cb_publish(callback: CallbackQuery):
    """Кнопка под уведомлением об отзыве: показать / убрать из магазина."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    action, rid = callback.data.split(":")
    published = action == "rvpub"
    r = await _set_published(callback, int(rid), published)
    if not r:
        return
    await callback.answer("Отзыв в магазине ⭐️" if published else "Убрал из магазина")
    try:
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Ответить", callback_data=f"sup_reply:{r['user_id']}")],
            publish_row(int(rid), published)]))
    except Exception:
        pass
    if published:
        await callback.message.answer("Так его увидят покупатели:\n\n" + await _preview(int(rid))
                                      + "\n\nЕсли в тексте имя или лишнее — «✏️ Поправить текст».",
                                      parse_mode="HTML")


@router.callback_query(F.data.startswith("rvtog:"))
async def cb_toggle(callback: CallbackQuery):
    """Звёздочки в /reviews."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    _, rid, offset = callback.data.split(":")
    r = await db.get_review(int(rid))
    if not r:
        await callback.answer("Отзыв не найден.")
        return
    published = not r.get("published")
    if not await _set_published(callback, int(rid), published):
        return
    await callback.answer("Отзыв в магазине ⭐️" if published else "Убрал из магазина")
    await _show(callback.message, int(offset), edit=True)


@router.callback_query(F.data.startswith("rvedit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    rid = int(callback.data.split(":")[1])
    r = await db.get_review(rid)
    if not r:
        await callback.answer("Отзыв не найден.")
        return
    await state.set_state(ReviewEdit.text)
    await state.update_data(review_id=rid)
    await callback.answer()
    current = (r.get("public_text") or "").strip() or r.get("text") or ""
    await callback.message.answer(
        "✏️ Пришли текст отзыва, как его показать в магазине (имя и лишнее можно убрать). "
        "Сейчас:\n\n<code>" + current.replace("<", "&lt;") + "</code>\n\n"
        "Вернуть исходный текст — «-». Отмена — /cancel.", parse_mode="HTML")


@router.message(Command("cancel"), StateFilter(ReviewEdit.text))
async def cmd_edit_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Ок, текст не меняю.")


@router.message(StateFilter(ReviewEdit.text), F.text)
async def msg_edit(message: Message, state: FSMContext):
    rid = (await state.get_data()).get("review_id")
    await state.clear()
    text = message.text.strip()
    await db.set_review_public_text(rid, None if text == "-" else text)
    await message.answer("✅ Сохранил. В магазине будет так:\n\n" + await _preview(rid),
                         parse_mode="HTML")
