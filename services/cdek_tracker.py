"""Следит за посылками в СДЭК и сообщает клиенту о прибытии в пункт выдачи.

Опрашиваем API, а не ждём вебхуков от СДЭК: заказов немного, а вебхуки
потребовали бы отдельной настройки на стороне СДЭК и публичного адреса.
"""
import asyncio
import logging
import time

from aiogram import Bot

import config
import database as db

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 30 * 60          # раз в полчаса
FIRST_DELAY = 90                  # даём боту спокойно подняться
COST_INTERVAL = 24 * 60 * 60      # суммы накладных сверяем раз в сутки
COST_DAYS = 60                    # …за последние два месяца (с возвратами)

# Посылка доехала и её можно забирать
ARRIVED_CODES = {
    "ACCEPTED_AT_PICK_UP_POINT",            # принят на склад до востребования
    "ACCEPTED_AT_RECIPIENT_CITY_WAREHOUSE",  # принят на склад доставки
}
# Уже получена — сообщать о прибытии поздно, просто закрываем отслеживание
FINAL_CODES = {"DELIVERED", "NOT_DELIVERED"}
KEEP_REMIND_HOURS = 48            # за сколько до конца бесплатного хранения напомнить


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


def _hours_left(stamp_utc: str | None) -> float | None:
    from datetime import datetime, timezone
    try:
        until = datetime.strptime(stamp_utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (until - datetime.now(timezone.utc)).total_seconds() / 3600


def _msk(stamp_utc: str) -> str:
    from datetime import datetime, timedelta
    d = datetime.strptime(stamp_utc, "%Y-%m-%d %H:%M:%S") + timedelta(hours=3)
    return d.strftime("%d.%m")


async def _who(order: dict) -> str:
    user = await db.get_user(order["user_id"]) or {}
    name = f"@{user['username']}" if user.get("username") else f"id:{order['user_id']}"
    return name + (f" ({order['recipient_name']})" if order.get("recipient_name") else "")


async def _notify_admins(text: str):
    from handlers.prodamus_webhook import _send_notify
    await _send_notify(None, text)


async def _send_client(bot: Bot, order: dict, text: str):
    try:
        await bot.send_message(order["user_id"], text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.error(f"CDEK: не написать клиенту {order['user_id']}: {e}")
        return False


async def _remind_keep(bot: Bot, order: dict, keep: str):
    """За 2 дня до конца бесплатного хранения — клиенту и админам."""
    num = order.get("order_code") or ""
    pvz = pvz_from_delivery(order.get("summary"))
    text = (f"⏳ <b>Заказ {num} ждёт в пункте выдачи до {_msk(keep)}</b>\n\n"
            "Осталось 2 дня бесплатного хранения — потом СДЭК отправит посылку обратно.\n")
    if pvz:
        text += f"\n📍 {pvz}"
    if order.get("cdek_number"):
        text += f"\nТрек-номер: <code>{order['cdek_number']}</code>"
    text += "\n\nДля получения понадобится паспорт или код из приложения СДЭК."
    sent = await _send_client(bot, order, text)
    await _notify_admins(
        f"⏳ <b>{num}</b> — {await _who(order)}: посылка лежит в пункте выдачи, бесплатное "
        f"хранение до {_msk(keep)}. " + ("Клиенту напомнил." if sent else
                                         "⚠️ Клиенту написать не удалось — напомните сами."))
    await db.set_order_keep_reminded(order["id"], keep)
    logger.info(f"CDEK: {num} — напомнил о сроке хранения до {keep}")


async def _not_picked_up(bot: Bot, client, order: dict, codes):
    """Посылку не забрали — СДЭК везёт её обратно: клиенту, админам,
    и стоимость обратной накладной — в «Отложено на СДЭК»."""
    num = order.get("order_code") or ""
    await _save_return_cost(client, order, codes)
    back = (await db.get_order(order["id"]) or {}).get("cdek_return_cost")
    sent = await _send_client(bot, order, (
        f"↩️ <b>Заказ {num} не забрали из пункта выдачи</b>\n\n"
        "Срок хранения закончился, и СДЭК везёт посылку обратно к нам. "
        "Если заказ ещё нужен — напишите в поддержку (/help), договоримся о повторной отправке."))
    await _notify_admins(
        f"↩️ <b>{num}</b> — {await _who(order)}: посылку не забрали, она едет обратно.\n"
        + (f"Обратная накладная: {back:.2f} ₽ — учтена в «Отложено на СДЭК»." if back is not None
           else "Стоимость обратной накладной бот добавит в «Отложено на СДЭК», когда СДЭК её начислит.")
        + ("" if sent else "\n⚠️ Клиенту написать не удалось."))


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
        await _save_cost(order, info.get("total_sum"))
        codes = set(info.get("status_codes") or [])
        final = next((c for c in sorted(FINAL_CODES) if c in codes), None)

        if final:
            # Посылку забрали (или она уходит обратно) — сообщать о прибытии
            # уже поздно; запоминаем итог для этапа «Получен» и снимаем с опроса
            when = _utc((info.get("status_dates") or {}).get(final))
            await db.set_order_final(order["id"], final, when)
            await _after_final(bot, order, final, when)
            if final == "NOT_DELIVERED":
                await _not_picked_up(bot, client, order, codes)

        elif order.get("arrived_notified_at"):
            # О прибытии уже сообщили — ждём, когда заберут. Срок хранения
            # СДЭК может продлить — держим свежим
            keep = _utc(info.get("keep_free_until"))
            if keep and keep != order.get("keep_until"):
                await db.set_order_keep_until(order["id"], keep)
            keep = keep or order.get("keep_until")
            left = _hours_left(keep)
            arrived_ago = _hours_left(order.get("arrived_notified_at"))
            # Сообщение о прибытии само говорит срок — сразу за ним не напоминаем
            if (keep and keep != order.get("keep_reminded_for") and left is not None
                    and 0 < left <= KEEP_REMIND_HOURS
                    and (arrived_ago is None or arrived_ago <= -12)):
                await _remind_keep(bot, order, keep)

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


async def _save_cost(order: dict, total) -> bool:
    """Запоминает, сколько СДЭК начислил за накладную, если сумма изменилась."""
    if total is None:
        return False
    total = round(float(total), 2)
    if order.get("cdek_actual_cost") is not None and abs(float(order["cdek_actual_cost"]) - total) < 0.005:
        return False
    await db.set_order_cdek_cost(order["id"], total)
    logger.info(f"CDEK: накладная {order.get('order_code')} — начислено {total:.2f} ₽ "
                f"(было {order.get('cdek_actual_cost')})")
    return True


async def _save_return_cost(client, order: dict, codes) -> bool:
    """Посылку не забрали — СДЭК везёт её обратно отдельной накладной и
    выставляет её в том же счёте. Запоминаем её сумму."""
    if "NOT_DELIVERED" not in set(codes or []):
        return False
    back = await client.get_return_cost(order["cdek_uuid"])
    total = (back or {}).get("total")
    if total is None:
        return False
    total = round(float(total), 2)
    if order.get("cdek_return_cost") is not None and abs(float(order["cdek_return_cost"]) - total) < 0.005:
        return False
    await db.set_order_cdek_return_cost(order["id"], total)
    logger.info(f"CDEK: обратная накладная по {order.get('order_code')} — {total:.2f} ₽")
    return True


async def refresh_costs(days: int | None = COST_DAYS) -> int:
    """Сверяет с API СДЭК суммы накладных за последние days дней (None — все).
    Нужна и после вручения: хранение в пункте выдачи СДЭК дописывает в
    накладную к выдаче, а опрос статусов вручённые уже не смотрит."""
    from services.cdek_accounts import client_for_order
    changed = 0
    for order in await db.get_orders_cdek_cost_check(days):
        client = client_for_order(order)
        if not client:
            continue
        info = await client.get_order_info(order["cdek_uuid"])
        if not info:
            continue
        if await _save_cost(order, info.get("total_sum")):
            changed += 1
        if await _save_return_cost(client, order, info.get("status_codes")):
            changed += 1
        await asyncio.sleep(0.5)
    return changed


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
    last_costs = 0.0
    while True:
        try:
            await _check_once(bot)
        except Exception:
            logger.exception("CDEK-трекинг: ошибка обхода заказов")
        if time.monotonic() - last_costs >= COST_INTERVAL or not last_costs:
            try:
                changed = await refresh_costs()
                logger.info(f"CDEK: суммы накладных сверены, изменилось {changed}")
                last_costs = time.monotonic()
            except Exception:
                logger.exception("CDEK: сверка сумм накладных не удалась")
        await asyncio.sleep(CHECK_INTERVAL)
