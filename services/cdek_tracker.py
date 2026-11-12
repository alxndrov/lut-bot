"""Следит за посылками в СДЭК и сообщает клиенту о прибытии в пункт выдачи.

Опрашиваем API, а не ждём вебхуков от СДЭК: заказов немного, а вебхуки
потребовали бы отдельной настройки на стороне СДЭК и публичного адреса.
"""
import asyncio
import logging

from aiogram import Bot

import config
import database as db

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 30 * 60          # раз в полчаса
FIRST_DELAY = 90                  # даём боту спокойно подняться

# Посылка доехала и её можно забирать
ARRIVED_CODES = {
    "ACCEPTED_AT_PICK_UP_POINT",            # принят на склад до востребования
    "ACCEPTED_AT_RECIPIENT_CITY_WAREHOUSE",  # принят на склад доставки
}
# Уже получена — сообщать о прибытии поздно, просто закрываем отслеживание
FINAL_CODES = {"DELIVERED", "NOT_DELIVERED"}


def _utc(stamp: str | None) -> str | None:
    """«2026-11-12T14:05:00+0300» от СДЭК → «2026-11-12 11:05:00» UTC, как в базе."""
    if not stamp:
        return None
    from datetime import datetime, timezone
    try:
        d = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def pvz_from_delivery(delivery_str: str | None) -> str:
    """Адрес пункта выдачи из строки доставки, без служебных пометок."""
    for line in (delivery_str or "").splitlines():
        if "пункт выдачи" in line.lower():
            return line.split(":", 1)[-1].split("(код ПВЗ")[0].strip()
    return ""


def _arrived_text(order: dict) -> str:
    num = order.get("order_code") or order.get("prodamus_order_id") or ""
    text = f"📬 <b>Заказ {num} приехал в пункт выдачи!</b>\n\n"
    pvz = pvz_from_delivery(order.get("summary"))
    if pvz:
        text += f"📍 {pvz}\n"
    if order.get("cdek_number"):
        text += f"Трек-номер: <code>{order['cdek_number']}</code>\n"
    text += (
        "\nДля получения понадобится паспорт или код из приложения СДЭК.\n"
        "Посылка хранится в пункте выдачи несколько дней — "
        "точный срок подскажут в СДЭК."
    )
    return text


async def _check_once(bot: Bot):
    from services.cdek_accounts import client_for_order

    orders = await db.get_orders_tracking()
    if not orders:
        return

    for order in orders:
        client = client_for_order(order)
        if not client:
            continue
        info = await client.get_order_info(order["cdek_uuid"])
        if not info:
            continue
        codes = set(info.get("status_codes") or [])
        final = next((c for c in sorted(FINAL_CODES) if c in codes), None)

        if final:
            # Посылку забрали (или она уходит обратно) — сообщать о прибытии
            # уже поздно; запоминаем итог для этапа «Получен» и снимаем с опроса
            when = _utc((info.get("status_dates") or {}).get(final))
            await db.set_order_final(order["id"], final, when)
            await _after_final(bot, order, final, when)

        elif order.get("arrived_notified_at"):
            # О прибытии уже сообщили — ждём, когда заберут. Срок хранения
            # СДЭК может продлить — держим свежим
            keep = _utc(info.get("keep_free_until"))
            if keep and keep != order.get("keep_until"):
                await db.set_order_keep_until(order["id"], keep)

        elif codes & ARRIVED_CODES:
            # Отмечаем сразу и в любом случае, иначе будем слать каждые полчаса
            await db.set_order_keep_until(order["id"], _utc(info.get("keep_free_until")))
            await db.set_order_arrived(order["id"])
            # «Мой заказ» приходит заново — непрочитанным, со сроком хранения;
            # кому виджет не включён (или не принял) — прежний текст
            from handlers import client_order_widget
            try:
                widget = await client_order_widget.sync(order["id"], bot)
            except Exception as e:
                logger.warning(f"CDEK: виджет заказа {order['id']}: {e}")
                widget = False
            if not widget:
                try:
                    await bot.send_message(order["user_id"], _arrived_text(order),
                                           parse_mode="HTML")
                except Exception as e:
                    logger.error(f"CDEK: не сообщить о прибытии клиенту "
                                 f"{order['user_id']}: {e}")
            logger.info(f"CDEK: заказ {order.get('order_code')} приехал в ПВЗ, "
                        f"клиент {order['user_id']} уведомлён {'виджетом' if widget else 'текстом'}")

        await asyncio.sleep(0.5)   # не частим с запросами к СДЭК


async def _after_final(bot: Bot, order: dict, final: str, when: str | None):
    """Вручена — просьба об отзыве через 2 дня; не вручена — пуш снимаем.
    «Мой заказ» переходит в «Получен» / «Не получен» без звука."""
    import json
    if not order.get("friend_by"):
        try:
            rounds = json.loads(order.get("rounds_json") or "[]")
        except Exception:
            rounds = []
        pids = db.unpack_round_products(order.get("round_products_json"), rounds,
                                        order["product_id"]) or [order["product_id"]]
        wanted = []
        for pid in dict.fromkeys(pids):
            product = await db.get_product(pid)
            if product and product.get("review_push_delay"):   # товар просит отзывы
                wanted.append(pid)
        await db.review_push_after_delivery(order, wanted, when,
                                            cancel=final != "DELIVERED")
    from handlers import client_order_widget
    try:
        await client_order_widget.sync(order["id"], bot)
    except Exception as e:
        logger.warning(f"CDEK: виджет заказа {order['id']}: {e}")


async def cdek_tracking_worker(bot: Bot):
    if not config.CDEK_AUTO_ORDER:
        logger.info("CDEK-трекинг выключен: не настроено автосоздание заказов")
        return
    await asyncio.sleep(FIRST_DELAY)
    logger.info("CDEK-трекинг запущен")
    while True:
        try:
            await _check_once(bot)
        except Exception:
            logger.exception("CDEK-трекинг: ошибка обхода заказов")
        await asyncio.sleep(CHECK_INTERVAL)
