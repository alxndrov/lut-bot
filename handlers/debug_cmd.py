"""
Отладка заказа прямо в админском боте (malimadmins): /debug <код или id>.

Раньше для этого был отдельный HTTPS-эндпоинт (handlers/debug_api.py) —
убрали его: держать открытый порт наружу ради разовой проверки того,
почему заказ не распределился на печать, лишняя возня. Здесь то же самое,
но без сервера, сертификатов и токенов — только SELECT-запросы и разбор
той же логики роутинга, что использует handlers/prodamus_webhook.py.
"""
import json
import logging
import re

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import config
import database as db
from handlers.order_actions import _msk
from handlers.prodamus_webhook import _parse_routing, routing_lookup

router = Router()
logger = logging.getLogger(__name__)


async def _find_order(token: str) -> dict | None:
    orders = await db.get_orders()
    return next(
        (o for o in orders
         if o.get("order_code") == token or str(o["id"]) == token),
        None,
    )


async def _routing_diagnosis(pid: int, product: dict | None, answers: list) -> list[str]:
    """Почему позиция ушла тому или иному человеку — по шагам."""
    lines = []
    routing_text = product.get("order_routing_text") if product else None
    if not routing_text:
        lines.append("  распределение по товару не настроено — позиция ничья")
        return lines
    lines.append(f"  правило: <code>{routing_text}</code>")

    questions = await db.get_product_questions(pid)
    router_q = next((q for q in questions if q.get("is_router")), None)
    if not router_q:
        lines.append("  ⚠️ ни один вопрос не отмечен как вопрос-распределитель")
        return lines
    lines.append(f"  вопрос-распределитель: «{router_q['text']}»")

    from handlers.prodamus_webhook import find_answer
    answer = find_answer(answers, router_q["text"])
    if answer is None:
        lines.append("  ⚠️ в ответах клиента нет ответа на этот вопрос "
                     "(текст вопроса изменился после заказа?)")
        return lines
    lines.append(f"  ответ клиента: «{answer}»")

    rmap = _parse_routing(routing_text)
    nums = re.findall(r"\d+", answer or "")
    if not nums and "*" not in rmap:
        lines.append("  ⚠️ в ответе нет числа — номер не извлечь")
        return lines

    num = nums[0] if nums else None
    name = routing_lookup(rmap, num)
    if not name:
        lines.append(f"  ⚠️ номер «{num}» не встречается в правиле выше — позиция ничья")
        return lines
    if num and rmap.get(num):
        lines.append(f"  по номеру «{num}» должен печатать: <b>{name}</b>")
    else:
        lines.append(f"  по правилу «*» (всё остальное) должен печатать: <b>{name}</b>")

    admin = await db.get_admin_by_username(name, config.ADMIN_IDS)
    if not admin:
        lines.append(f"  ⚠️ «{name}» не похоже ни на чей @username среди админов — "
                     "человек либо не писал боту, либо в правиле имя, а не ник")
    else:
        admin_label = f"@{admin['username']}" if admin.get("username") else str(admin["user_id"])
        lines.append(f"  ✅ распознан как {admin_label} (id {admin['user_id']})")
    return lines


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
        answers = rounds[i - 1] if i - 1 < len(rounds) else []
        who = routing[i - 1] if i - 1 <= len(routing) - 1 else None

        lines.append(f"\n<b>Поз.{i}</b> — {product['name'] if product else f'товар #{pid}'}"
                     + (f" → сейчас печатает: <b>{who}</b>" if who else " → сейчас: не определён"))
        lines += await _routing_diagnosis(pid, product, answers)

    if not round_products:
        lines.append("\nПозиций не нашлось — round_products пуст.")

    await message.answer("\n".join(lines), parse_mode="HTML")
