"""Личная скидка на курс: ссылка на оплату app.malimabi.com/pay/<токен>
(и экран скидки в боте t.me/<бот>?start=offer_<token>).

Работает только у указанного аккаунта (по Telegram ID, ник берём для поиска)
и до --until включительно по Москве (по умолчанию — до конца текущего месяца).
Подробности — handlers/course_offer.py.

Обычно удобнее /offer в админ-боте; скрипт — то же самое из консоли сервера.
Запуск на сервере из корня репозитория:
    venv/bin/python -m scripts.make_course_offer @ник shooting --percent 10
    venv/bin/python -m scripts.make_course_offer 123456 bundle --price 7990 --until 2026-12-15
Цена со скидкой в процентах считается от текущей цены и округляется вниз до десятков.
"""
import argparse
import asyncio
import calendar
import secrets
from datetime import datetime

from aiogram import Bot

import config
import database as db
from handlers.course_offer import PREFIX, pay_link
from services import course as svc


def month_end(now: datetime) -> str:
    return now.replace(day=calendar.monthrange(now.year, now.month)[1]).strftime("%Y-%m-%d")


def discounted(price: int, percent: float) -> int:
    return int(price * (100 - percent) / 100) // 10 * 10


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("user", help="@ник или Telegram ID — человек должен хоть раз запускать бота")
    ap.add_argument("option", help="slug варианта: shooting, editing, bundle, vip")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--percent", type=float)
    group.add_argument("--price", type=int)
    ap.add_argument("--until", help="последний день, YYYY-MM-DD; по умолчанию конец месяца")
    ap.add_argument("--note", help="заметка для себя: откуда скидка")
    args = ap.parse_args()

    raw = args.user.lstrip("@").removeprefix("https://t.me/")
    user = await (db.get_user(int(raw)) if raw.isdigit() else db.get_user_by_username(raw))
    if not user:
        raise SystemExit(f"Не нашёл {args.user} среди тех, кто запускал бота")
    option = next((o for o in await db.get_course_options() if o["slug"] == args.option), None)
    if not option:
        raise SystemExit(f"Нет варианта курса {args.option}")
    full = svc.price_for(option)
    price = args.price if args.price else discounted(full, args.percent)
    until = args.until or month_end(svc.now_msk())
    datetime.strptime(until, "%Y-%m-%d")
    if not 1 <= price <= full:
        raise SystemExit(f"Странная цена {price} (текущая {full})")

    token = secrets.token_urlsafe(9)
    await db.create_course_offer(token, option["id"], price, user["user_id"], until, args.note)
    offer = await db.get_course_offer(token)
    bot = Bot(token=config.BOT_TOKEN)
    try:
        username = (await bot.get_me()).username
    finally:
        await bot.session.close()
    who = f"@{user['username']}" if user.get("username") else user["user_id"]
    print(f"{option['title']}: {svc.rub(full)} → {svc.rub(price)} для {who} "
          f"(id {user['user_id']}) до {until} включительно")
    print("Ссылка (страница предложения и выбор оплаты):", pay_link(offer, option))
    print("Сразу карта РФ / СБП:", pay_link(offer, option, "paw"))
    print("То же предложение в боте:", f"https://t.me/{username}?start={PREFIX}{token}")


if __name__ == "__main__":
    asyncio.run(main())
