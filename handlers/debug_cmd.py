"""
Отладка заказа прямо в админском боте (malimadmins): /debug <код или id>.

Раньше для этого был отдельный HTTPS-эндпоинт (handlers/debug_api.py) —
убрали его: держать открытый порт наружу ради разовой проверки того,
почему заказ не распределился на печать, лишняя возня. Здесь то же самое,
но без сервера, сертификатов и токенов — только SELECT-запросы.
"""
import json
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import config
import database as db
from handlers.order_actions import _msk

router = Router()
logger = logging.getLogger(__name__)


async def _find_order(token: str) -> dict | None:
    orders = await db.get_orders()
    return next(
        (o for o in orders
         if o.get("order_code") == token or str(o["id"]) == token),
        None,
    )


@router.message(Command("debug"))
async def cmd_debug(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return

    arg = (message.text or "").split(maxsplit=1)
    token = arg[1].strip() if len(arg) > 1 else ""

    if not token:
        orders = (await db.get_orders())[:10]
        if not orders:
            await message.answer("Заказов пока нет.")
            return
        lines = ["🐞 <b>Последние заказы</b> — пришлите код или id:\n"]
        for o in orders:
            code = o.get("order_code") or f"#{o['id']}"
            buyer = (f"@{o['username']}" if o.get("username")
                    else (o.get("first_name") or f"id:{o.get('user_id')}"))
            lines.append(f"  <code>{code}</code> (id {o['id']}) — {buyer} · {_msk(o.get('created_at'))}")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    order = await _find_order(token)
    if not order:
        await message.answer(f"Заказ «{token}» не найден.")
        return

    try:
        rounds = json.loads(order.get("rounds_json") or "[]")
    except Exception:
        rounds = []
    round_products = db.unpack_round_products(
        order.get("round_products_json"), rounds, order["product_id"])

    buyer = (f"@{order['username']}" if order.get("username")
            else (order.get("first_name") or f"id:{order.get('user_id')}"))
    code = order.get("order_code") or f"#{order['id']}"

    lines = [
        f"🐞 <b>Заказ {code}</b> (id {order['id']})",
        f"Покупатель: {buyer} · оплачен {_msk(order.get('created_at'))}",
    ]
    if order.get("assignee_name"):
        lines.append(f"Исполнитель: <b>{order['assignee_name']}</b> (id {order.get('assignee_id')})")
    else:
        lines.append("Исполнитель: <i>не назначен</i>")

    routing = db.order_routing(order)
    lines.append(f"routing_json: <code>{routing or '[]'}</code>")

    products_cache: dict[int, dict] = {}
    for i, pid in enumerate(round_products, 1):
        if pid not in products_cache:
            products_cache[pid] = await db.get_product(pid)
        product = products_cache[pid]
        who = routing[i - 1] if i - 1 <= len(routing) - 1 else None

        lines.append(f"\n<b>Поз.{i}</b> — {product['name'] if product else f'товар #{pid}'}"
                     + (f" → сейчас печатает: <b>{who}</b>" if who else " → сейчас: не определён"))

    if not round_products:
        lines.append("\nПозиций не нашлось — round_products пуст.")

    await message.answer("\n".join(lines), parse_mode="HTML")
