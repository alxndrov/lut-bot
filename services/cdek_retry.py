"""Повторная попытка завести накладную СДЭК.

Накладная создаётся один раз, сразу после оплаты. Если в эту минуту у
СДЭК сбой (31.08.2026 их API час отдавал 504), заказ остаётся без
отправления, и замечает это только человек по сообщению в админский бот.
Воркер дотягивает такие заказы сам, когда СДЭК оживёт.

Перед каждой попыткой заявка ищется по нашему номеру заказа: на таймауте
она могла всё-таки создаться, и второй накладной на ту же посылку быть
не должно.
"""
import asyncio
import json
import logging

import config
import database as db

logger = logging.getLogger(__name__)

RETRY_EVERY = 10 * 60          # как часто проверяем
FIRST_DELAY = 3 * 60           # даём отработать первой попытке из вебхука
MAX_AGE_DAYS = 5               # старше — уже наверняка завели руками


async def _orders_without_waybill() -> list[dict]:
    """Неотправленные заказы, у которых нет заявки в СДЭК, но есть чем её завести."""
    orders = await db.get_orders(only_unshipped=True)
    out = []
    for o in orders:
        if o.get("cdek_uuid") or not o.get("order_code"):
            continue
        if not (o.get("pvz_code") and o.get("recipient_name") and o.get("recipient_phone")):
            continue
        out.append(o)
    return out


async def retry_once(bot) -> int:
    """Один проход. Возвращает, сколько накладных удалось завести."""
    from handlers.prodamus_webhook import _create_cdek_order

    pending_orders = await _orders_without_waybill()
    if not pending_orders:
        return 0

    done = 0
    for o in pending_orders:
        round_products = db.unpack_round_products(
            o.get("round_products_json"), json.loads(o.get("rounds_json") or "[]"),
            o.get("product_id"))
        products_by_id = {}
        for pid in set(round_products):
            p = await db.get_product(pid)
            if p:
                products_by_id[pid] = p
        pending = {
            "pvz_code": o.get("pvz_code"),
            "recipient_name": o.get("recipient_name"),
            "recipient_phone": o.get("recipient_phone"),
        }
        ok = await _create_cdek_order(
            o["id"], pending, round_products, products_by_id, o["order_code"],
            bot=bot, client_id=o.get("user_id"),
            notify_fail=False,       # о неудаче уже сообщили в первый раз
            adopt_existing=True,     # вдруг заявка всё-таки создалась
        )
        if ok:
            done += 1
            logger.info(f"cdek_retry: {o['order_code']} — накладная заведена повторной попыткой")
            from handlers.prodamus_webhook import _send_notify
            await _send_notify(None, (
                f"✅ Заказ <code>{o['order_code']}</code>: накладная СДЭК всё-таки "
                f"создана — вручную заводить не нужно."
            ))
        else:
            logger.info(f"cdek_retry: {o['order_code']} — пока не выходит, попробую позже")
    return done


async def cdek_retry_worker(bot):
    """Фоновый цикл: раз в RETRY_EVERY добираем заказы без накладной."""
    if not config.CDEK_AUTO_ORDER:
        logger.info("cdek_retry: автосоздание накладных выключено, воркер не нужен")
        return
    logger.info("cdek_retry_worker: запущен")
    await asyncio.sleep(FIRST_DELAY)
    while True:
        try:
            await retry_once(bot)
        except Exception as e:
            logger.error(f"cdek_retry_worker: {type(e).__name__}: {e}")
        await asyncio.sleep(RETRY_EVERY)
