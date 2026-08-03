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
    from handlers.delivery import CDEK_CLIENT
    if not CDEK_CLIENT:
        return

    orders = await db.get_orders_tracking()
    if not orders:
        return

    for order in orders:
        info = await CDEK_CLIENT.get_order_info(order["cdek_uuid"])
        if not info:
            continue
        codes = set(info.get("status_codes") or [])

        if codes & ARRIVED_CODES:
            try:
                await bot.send_message(order["user_id"], _arrived_text(order),
                                       parse_mode="HTML")
                logger.info(
                    f"CDEK: заказ {order.get('order_code')} приехал в ПВЗ, "
                    f"клиент {order['user_id']} уведомлён"
                )
            except Exception as e:
                logger.error(f"CDEK: не сообщить о прибытии клиенту "
                             f"{order['user_id']}: {e}")
            # Отмечаем в любом случае, иначе будем слать каждые полчаса
            await db.set_order_arrived(order["id"])

        elif codes & FINAL_CODES:
            # Посылку уже забрали — снимаем с отслеживания молча
            await db.set_order_arrived(order["id"])

        await asyncio.sleep(0.5)   # не частим с запросами к СДЭК


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
