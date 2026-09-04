"""
Дележ прибыли между Мишей и Даней.

Условия разные по линейкам и по времени:
  • физтовары до 02.09.2026 — Даня получает 10% от стоимости товара (без
    доставки) плюс 200 ₽ за каждую напечатанную им ручку;
  • физтовары с 02.09.2026 — печатает всё Даня, отдельной платы за печать
    нет, прибыль с таких заказов делится 60/40 в пользу Миши;
  • цифровые товары (пресеты) — по-старому 80/20.

Граница по дате заказа, а не по дате расчёта: период может её пересекать,
и тогда каждая половина считается по своим условиям. Задним числом старые
заказы не переигрываем — расчёты по ним уже проведены.

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


def split_moment() -> str:
    """Момент перехода на новые условия в UTC — как хранится created_at."""
    return _bounds(config.NEW_SPLIT_FROM, config.NEW_SPLIT_FROM)[0]


def _before(moment: str) -> str:
    """Секунда перед моментом — верхняя граница «старого» куска периода."""
    return (datetime.strptime(moment, "%Y-%m-%d %H:%M:%S")
            - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")


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

    # Печать оплачивается отдельно только по заказам до перехода
    cut = split_moment()
    old_orders = [o for o in await _orders_in_period(dt_from, dt_to)
                  if (o.get("created_at") or "") < cut]
    printed_paid = (await _print_credits(old_orders)).get(config.PARTNER_ID, 0)

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

    # Физтовары делим на до и после перехода: условия по ним разные, а
    # период расчёта может лежать по обе стороны границы
    if dt_from < cut <= dt_to:
        old_goods = await db.get_period_revenue(dt_from, _before(cut))
        physical_old = float(old_goods["physical"])
        legacy_old = float(old_goods["delivery_legacy"])
    elif dt_to < cut:
        physical_old, legacy_old = physical, float(goods["delivery_legacy"])
    else:
        physical_old, legacy_old = 0.0, 0.0

    net_of = lambda v, legacy: v * (1 - fee_rate - NPD_RATE) - legacy * NPD_RATE
    physical_net_old = net_of(physical_old, legacy_old)
    physical_net_new = physical_net - physical_net_old

    partner_goods = physical_net_old * config.PARTNER_GOODS_PERCENT / 100
    partner_goods_new = physical_net_new * config.PARTNER_GOODS_PERCENT_NEW / 100
    partner_print = printed_paid * config.PARTNER_PRINT_FEE
    partner_digital = digital_net * config.PARTNER_DIGITAL_PERCENT / 100
    partner = partner_goods + partner_goods_new + partner_print + partner_digital

    # Уже выплаченное внутри периода: доля начисляется за весь период, но
    # часть её могли отдать раньше расчёта — частичной выплатой. Без этого
    # «Взаиморасчёт» повторно показывал бы уже отданные деньги как долг.
    paid = (await db.get_payouts_summary(dt_from, dt_to))["by_recipient"]
    paid_partner = float(paid.get(config.PARTNER_NAME, 0))
    paid_owner = float(paid.get(config.OWNER_NAME, 0))

    return {
        "gross": gross, "physical": physical, "digital": digital,
        "delivery": delivery, "count": int(goods["count"]),
        "fee": fee, "fee_pct": fee_pct, "npd": npd,
        "delivery_out": out, "expenses": expenses,
        "net": net,
        "printed": printed, "print_credits": credits,
        "printed_paid": printed_paid,
        "physical_old": physical_old, "physical_new": physical - physical_old,
        "partner": partner, "owner": net - partner,
        "paid_partner": paid_partner, "paid_owner": paid_owner,
        "partner_left": partner - paid_partner,
        "owner_left": net - partner - paid_owner,
        "partner_goods": partner_goods,
        "partner_goods_new": partner_goods_new,
        "partner_print": partner_print,
        "partner_digital": partner_digital,
    }


def share_parts(s: dict) -> list[str]:
    """Из чего сложилась доля Дани — только ненулевые слагаемые."""
    parts = []
    if s.get("partner_goods"):
        parts.append(f"{config.PARTNER_GOODS_PERCENT:g}% с товара до "
                     f"{_split_date_ru()} = {s['partner_goods']:,.2f}")
    if s.get("partner_print"):
        parts.append(f"печать {s.get('printed_paid', s['printed'])} × "
                     f"{config.PARTNER_PRINT_FEE} ₽ = {s['partner_print']:,.2f}")
    if s.get("partner_goods_new"):
        parts.append(f"{config.PARTNER_GOODS_PERCENT_NEW:g}% с товара с "
                     f"{_split_date_ru()} = {s['partner_goods_new']:,.2f}")
    if s.get("partner_digital"):
        parts.append(f"цифра {config.PARTNER_DIGITAL_PERCENT:g}% "
                     f"= {s['partner_digital']:,.2f}")
    return parts


def _split_date_ru() -> str:
    d = datetime.strptime(config.NEW_SPLIT_FROM, "%Y-%m-%d")
    return d.strftime("%d.%m")


def format_shares(s: dict) -> list[str]:
    """Строки с долями и расшифровкой, чем они набраны."""
    lines = [
        f"  {config.OWNER_NAME}: <b>{s['owner']:,.2f} ₽</b>",
        f"  {config.PARTNER_NAME}: <b>{s['partner']:,.2f} ₽</b>",
    ]
    parts = share_parts(s)
    if parts:
        lines.append(f"    <i>{' · '.join(parts)}</i>")
    return lines


def month_bounds(year: int, month: int) -> tuple[str, str]:
    import calendar
    return (f"{year:04d}-{month:02d}-01",
            f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}")
