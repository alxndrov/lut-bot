"""Отзывы покупателей в магазине: кнопка «⭐️ Отзывы покупателей» в каталоге.

Показываем только те, что админ опубликовал (handlers/reviews.py), и
только текст — без имени, ника и заказа: обезличенно. Фото с хорошим
отзывом присылают редко, поэтому вложения не показываем.
"""
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import database as db
from handlers.expenses import MSK

router = Router()

PAGE = 5
TEXT_LIMIT = 700
_MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
           "сентябрь", "октябрь", "ноябрь", "декабрь"]


def _month(ts: str | None) -> str:
    from datetime import datetime, timezone
    try:
        dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).astimezone(MSK)
    except (TypeError, ValueError):
        return ""
    return f"{_MONTHS[dt.month - 1]} {dt.year}"


def _clean_product(name: str | None) -> str:
    # Эмодзи в начале названия («🎤 Microphone…») в подписи лишние
    return (name or "").lstrip("🎤🔥🎬📦⭐️✨🎁 ").strip()


def review_text(r: dict) -> str:
    body = (r.get("body") or "").strip()
    if len(body) > TEXT_LIMIT:
        body = body[:TEXT_LIMIT].rstrip() + "…"
    sign = " · ".join(x for x in (_clean_product(r.get("product_name")), _month(r.get("created_at"))) if x)
    return f"«{escape(body)}»" + (f"\n<i>— {escape(sign)}</i>" if sign else "")


async def page(offset: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_published_reviews()
    rows = await db.get_published_reviews(PAGE, offset)
    if not rows:
        text = "⭐️ <b>Отзывы покупателей</b>\n\nСкоро здесь появятся отзывы 🤍"
    else:
        text = ("⭐️ <b>Отзывы покупателей</b>\n\n"
                + "\n\n".join(review_text(r) for r in rows))
    nav = []
    if offset:
        nav.append(InlineKeyboardButton(text="⬅️ Новее", callback_data=f"shoprev:{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav.append(InlineKeyboardButton(text="Ещё отзывы ➡️", callback_data=f"shoprev:{offset + PAGE}"))
    kb = ([nav] if nav else []) + [[InlineKeyboardButton(text="◀️ Каталог", callback_data="catalog")]]
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(F.data.startswith("shoprev:"))
async def cb_shop_reviews(callback: CallbackQuery):
    offset = int(callback.data.split(":", 1)[1] or 0)
    text, kb = await page(offset)
    await callback.answer()
    msg = callback.message
    # Из каталога (там фото-баннер) — новым сообщением; листание — правкой
    if msg.text and msg.text.startswith("⭐️ Отзывы покупателей"):
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)
