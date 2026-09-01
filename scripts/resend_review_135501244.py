"""
Разовый скрипт: повторно отправляет в админский чат конкретный отзыв клиента
(Stel @stelrum, id:135501244, кейс Hollyland Lark M2) уже в новом формате —
с номером заказа и реплаем на карточку заказа у каждого админа.

Запускать на сервере, где лежит реальная база (database.py -> DB_PATH) и
доступен config.WAITLIST_BOT_TOKEN. После использования можно удалить.

    python3 scripts/resend_review_135501244.py
"""
import asyncio

import config
import database as db
from handlers.support import notify_admins, client_header
from aiogram.types import Message

USER_ID = 135501244
USERNAME = "stelrum"
FIRST_NAME = "Stel"
PRODUCT_NAME_HINT = "Hollyland Lark M2"
REVIEW_TEXT = (
    "Привет, все гуд, тупанул толь то что у тебя на канале капсом было "
    "прописано на крышке, я подумал что у меня будет так же автоматом и "
    "не стал прописывать капсом"
)


class _FakeUser:
    def __init__(self, user_id, username, first_name):
        self.id = user_id
        self.username = username
        self.first_name = first_name


class _FakeMessage:
    """Заглушка вместо aiogram.Message — notify_admins/client_header читают
    только .from_user, .text и .caption."""
    def __init__(self, user, text):
        self.from_user = user
        self.text = text
        self.caption = None


async def find_order() -> dict | None:
    orders = [o for o in await db.get_orders()
              if o.get("user_id") == USER_ID
              and PRODUCT_NAME_HINT.lower() in (o.get("product_name") or "").lower()]
    if not orders:
        return None
    orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)
    return orders[0]


async def main():
    order = await find_order()
    if not order:
        print("Заказ не найден — проверь USER_ID / PRODUCT_NAME_HINT.")
        return

    extra = f"🛍 {order['product_name']} · №{db.order_number_digits(order)}"
    user = _FakeUser(USER_ID, USERNAME, FIRST_NAME)
    fake_message = _FakeMessage(user, REVIEW_TEXT)
    header = client_header(fake_message, "⭐️ <b>Отзыв о покупке</b>", extra)

    ok = await notify_admins(fake_message, header, USER_ID,
                             bot_token=config.WAITLIST_BOT_TOKEN, order_id=order["id"])
    print("Отправлено" if ok else "Не удалось отправить — смотри лог выше.")


if __name__ == "__main__":
    asyncio.run(main())
