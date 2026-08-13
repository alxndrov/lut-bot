"""
Дележ прибыли между Мишей и Даней.

Условия разные по линейкам:
  • физтовары (микрофоны) — Даня получает 10% от стоимости товара (без
    доставки) плюс 200 ₽ за каждую напечатанную им ручку, Миша — остаток;
  • цифровые товары (пресеты) — по-старому 80/20.

Из общей прибыли до дележа уходят комиссия Prodamus, НПД, доставка в СДЭК
и расходы на материалы.
"""
import logging
from datetime import datetime, timedelta

import config
import database as db

logger = logging.getLogger(__name__)

NPD_RATE = config.NPD_PERCENT / 100


def delivery_out(delivery_cost: float, delivery_legacy: float,
                 fee_pct: float) -> float:
    """Сколько из принятой за доставку суммы реально ушло в СДЭК.

    По новым заказам счёт СДЭК (тариф + страховка + НДС) сохранён в
    purchases.delivery_cost — берём его как есть. У заказов до августа 2026
    колонки нет: там наценка покрывала только комиссию Prodamus, значит в
    СДЭК уходило ровно то, что осталось от delivery_amount после неё.
    """
    return delivery_cost + delivery_legacy * (1 - fee_pct / 100)


async def _print_credits(orders: list[dict]) -> dict[int, int]:
    """{id админа: сколько позиций он напечатал} по списку заказов —
    сумма db.order_print_credits (см. её докстринг) по каждому заказу."""
    credits: dict[int, int] = {}
    for order in orders:
        prints = await db.get_order_prints(order["id"])
        per_order = await db.order_print_credits(order, prints, config.ADMIN_IDS)
        for uid, n in per_order.items():
            credits[uid] = credits.get(uid, 0) + n
    return credits


_bounds = db.period_bounds      # правило одно на все выборки за период


async def _orders_in_period(dt_from: str, dt_to: str) -> list[dict]:
    """Заказы за период — по ним считаем, кто сколько ручек напечатал."""
    orders = await db.get_orders()
    return [o for o in orders
            if dt_from <= (o.get("created_at") or "") <= dt_to]


async def split(dt_from: str, dt_to: str, fee_pct: float | None = None) -> dict:
    """Полная раскладка денег и долей за период.

    Границы — даты или моменты времени, включительно с обеих сторон.
    Возвращает суммы, вычеты, число напечатанных ручек и доли обоих.
    """
    dt_from, dt_to = _bounds(dt_from, dt_to)
    fee_pct = config.PRODAMUS_FEE_PERCENT if fee_pct is None else fee_pct
    fee_rate = fee_pct / 100

    goods = await db.get_period_revenue(dt_from, dt_to)
    physical = float(goods["physical"])
    digital = float(goods["digital"])
    delivery = float(goods["delivery"])
    gross = physical + digital + delivery

    expenses = (await db.get_expenses_summary(dt_from, dt_to))["total"]

    fee = gross * fee_rate
    npd = gross * NPD_RATE
    out = delivery_out(float(goods["delivery_cost"]),
                       float(goods["delivery_legacy"]), fee_pct)
    net = gross - fee - npd - out - expenses

    credits = await _print_credits(await _orders_in_period(dt_from, dt_to))
    printed = credits.get(config.PARTNER_ID, 0)

    # Доля Дани — процент не с «грязной» стоимости товара, а с чистой,
    # после вычета комиссии Prodamus и налога НПД (доставка — транзит, в
    # неё не входит; она в этот же расчёт входит нулём — см. ниже). Для
    # цифры так было и раньше (digital_net), для физтоваров — тоже самое,
    # выведено из того, что реально остаётся «К выплате» по каждому
    # заказу в финансовом листе: (goods+доставка) минус комиссия, налог
    # и отложенное на СДЭК — доставка при этом гасит сама себя, кроме
    # старых заказов (до точного счёта СДЭК), где наценка на доставку не
    # покрывала налог — оттуда и поправка на delivery_legacy.
    digital_net = digital * (1 - fee_rate - NPD_RATE)
    physical_net = physical * (1 - fee_rate - NPD_RATE) - float(goods["delivery_legacy"]) * NPD_RATE
    partner = (physical_net * config.PARTNER_GOODS_PERCENT / 100
               + printed * config.PARTNER_PRINT_FEE
               + digital_net * config.PARTNER_DIGITAL_PERCENT / 100)

    return {
        "gross": gross, "physical": physical, "digital": digital,
        "delivery": delivery, "count": int(goods["count"]),
        "fee": fee, "fee_pct": fee_pct, "npd": npd,
        "delivery_out": out, "expenses": expenses,
        "net": net,
        "printed": printed, "print_credits": credits,
        "partner": partner, "owner": net - partner,
        "partner_goods": physical_net * config.PARTNER_GOODS_PERCENT / 100,
        "partner_print": printed * config.PARTNER_PRINT_FEE,
        "partner_digital": digital_net * config.PARTNER_DIGITAL_PERCENT / 100,
    }


def format_shares(s: dict) -> list[str]:
    """Строки с долями и расшифровкой, чем они набраны."""
    parts = []
    if s["partner_goods"]:
        parts.append(f"{config.PARTNER_GOODS_PERCENT:g}% с товара "
                     f"{s['partner_goods']:,.2f}")
    if s["partner_print"]:
        parts.append(f"печать {s['printed']} × {config.PARTNER_PRINT_FEE} ₽ "
                     f"= {s['partner_print']:,.2f}")
    if s["partner_digital"]:
        parts.append(f"цифра {config.PARTNER_DIGITAL_PERCENT:g}% "
                     f"= {s['partner_digital']:,.2f}")
    lines = [
        f"  {config.OWNER_NAME}: <b>{s['owner']:,.2f} ₽</b>",
        f"  {config.PARTNER_NAME}: <b>{s['partner']:,.2f} ₽</b>",
    ]
    if parts:
        lines.append(f"    <i>{' · '.join(parts)}</i>")
    return lines


def month_bounds(year: int, month: int) -> tuple[str, str]:
    import calendar
    return (f"{year:04d}-{month:02d}-01",
            f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}")
