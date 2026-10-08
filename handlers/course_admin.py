"""Курс в админском боте: кто купил, цены, кодовое слово.

/course            — сводка и последние покупки, кнопка CSV со всеми
/courseprice slug предпродажа полная — цены опции (shooting/editing/bundle)
/courseword слово[, слово2] — кодовые слова; без аргумента — показать,
                     «/courseword -» — выключить
"""
import csv
import io

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

import config
import database as db
from handlers.course import KEYWORDS_SETTING
from services import course as svc
from services.finance_sheet import msk_date

router = Router()
router.message.filter(F.from_user.id.in_(config.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))

LIST_LIMIT = 40


def _who(b: dict) -> str:
    return f"@{b['username']}" if b.get("username") else (b.get("first_name") or str(b["user_id"]))


@router.message(Command("course"))
async def cmd_course(message: Message):
    buyers = await db.get_course_buyers()
    options = await db.get_course_options()
    if not options:
        await message.answer("Курс ещё не заведён.")
        return
    product = await db.get_course_product()
    price_kind = {"presale": f"предпродажа (до {svc.presale_end_text()})",
                  "mid": f"средние (до {svc.full_price_from_text()})",
                  "full": "полные"}[svc.price_stage()]
    lines = [f"🎓 <b>Курс</b> — {'в продаже' if product and product.get('active') else 'скрыт'}, "
             f"цены: {price_kind}", ""]
    for o in options:
        mine = [b for b in buyers if b["slug"] == o["slug"]]
        lines.append(f"• {o['title']} ({o['slug']}): {svc.rub(svc.price_for(o))} — "
                     f"{len(mine)} шт, {svc.rub(sum(b['amount'] for b in mine))}")
    taken = sum(1 for b in buyers if b["review_position"])
    lines += [f"Всего: {len(buyers)} покупок, {svc.rub(sum(b['amount'] for b in buyers))}",
              f"🎁 Разборы: {taken} из {config.BUNDLE_REVIEW_LIMIT}", ""]
    words = await db.get_setting(KEYWORDS_SETTING) or ""
    lines.append(f"Кодовое слово: {words or 'не задано (/courseword)'}")
    if buyers:
        lines += ["", f"<b>Последние покупки</b>{' (все — в CSV)' if len(buyers) > LIST_LIMIT else ''}:"]
        for b in buyers[::-1][:LIST_LIMIT]:
            pos = f" · разбор #{b['review_position']}" if b["review_position"] else ""
            when = msk_date(b["created_at"]).strftime("%d.%m %H:%M")
            lines.append(f"{when} {_who(b)} — {b['title']}, {svc.rub(b['amount'])}{pos}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📄 CSV", callback_data="courseadm:csv")]]) if buyers else None
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "courseadm:csv")
async def cb_csv(callback: CallbackQuery):
    buyers = await db.get_course_buyers()
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Дата (МСК)", "Опция", "Сумма", "Имя", "Telegram", "Telegram ID",
                "Место на разбор", "Открытые разделы", "Платёж Prodamus"])
    for b in buyers:
        w.writerow([msk_date(b["created_at"]).strftime("%d.%m.%Y %H:%M"), b["title"], b["amount"],
                    b.get("first_name") or "", f"@{b['username']}" if b.get("username") else "",
                    b["user_id"], b["review_position"] or "",
                    ", ".join(svc.SECTION_SHORT.get(s, s) for s in (b.get("sections") or "").split(",") if s),
                    b.get("telegram_payment_id") or ""])
    data = out.getvalue().encode("utf-8-sig")   # BOM — чтобы Excel понял кириллицу
    await callback.answer()
    await callback.message.answer_document(BufferedInputFile(data, filename="course_buyers.csv"))


@router.message(Command("courseprice"))
async def cmd_price(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        options = await db.get_course_options()
        await message.answer(
            "Формат: <code>/courseprice slug предпродажа полная</code>\n\n" + "\n".join(
                f"{o['slug']}: {o['price']} / {o['price_full']}" for o in options),
            parse_mode="HTML")
        return
    if not await db.set_course_option_prices(parts[0], int(parts[1]), int(parts[2])):
        await message.answer(f"Нет опции «{parts[0]}».")
        return
    await message.answer(f"✅ {parts[0]}: предпродажа {svc.rub(int(parts[1]))}, "
                         f"полная {svc.rub(int(parts[2]))}.")


@router.message(Command("courseword"))
async def cmd_word(message: Message, command: CommandObject):
    args = (command.args or "").strip()
    if not args:
        words = await db.get_setting(KEYWORDS_SETTING) or ""
        await message.answer(f"Кодовые слова: {words or 'не заданы'}\n\n"
                             "Задать: /courseword слово, слово2\nВыключить: /courseword -")
        return
    value = "" if args == "-" else ", ".join(w.strip() for w in args.split(",") if w.strip())
    await db.set_setting(KEYWORDS_SETTING, value)
    await message.answer(f"✅ Кодовые слова: {value}" if value else "✅ Кодовые слова выключены.")
