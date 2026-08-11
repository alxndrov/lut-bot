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
    """{id админа: сколько позиций он напечатал} по списку заказов.

    Кто печатал позицию: отметка о печати, иначе разметка «кто печатает»,
    иначе тот, за кем заказ. Для заказов, где ничего не известно, позиции
    не засчитываются никому — лучше пропустить, чем начислить не тому.
    """
    credits: dict[int, int] = {}
    names: dict[str, int | None] = {}

    async def resolve(name: str | None) -> int | None:
        if not name:
            return None
        if name not in names:
            admin = await db.get_admin_by_username(name, config.ADMIN_IDS)
            names[name] = admin["user_id"] if admin else None
        return names[name]

    for order in orders:
        # У отгруженных заказов разметки в JSON нет — читаем её из карточки
        whos = db.order_routing(order) or db.parse_routing_line(order.get("summary") or "")
        total = len(whos) or int(order.get("quantity") or 1)
        prints = await db.get_order_prints(order["id"])
        by_position: dict[int, int] = {}
        for p in prints:
            positions = db.print_positions(p) or set(range(1, total + 1))
            for pos in positions:
                by_position.setdefault(pos, p["user_id"])

        for pos in range(1, total + 1):
            uid = by_position.get(pos)
            if uid is None and pos <= len(whos):
                uid = await resolve(whos[pos - 1])
            if uid is None:
                uid = order.get("printed_by_id") or order.get("assignee_id")
            if uid:
                credits[uid] = credits.get(uid, 0) + 1
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

    # Доля Дани: процент со стоимости товара (доставка — транзит, в неё
    # не входит) плюс оплата печати, а с цифровых — прежние 20%
    digital_net = digital * (1 - fee_rate - NPD_RATE)
    partner = (physical * config.PARTNER_GOODS_PERCENT / 100
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
        "partner_goods": physical * config.PARTNER_GOODS_PERCENT / 100,
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
