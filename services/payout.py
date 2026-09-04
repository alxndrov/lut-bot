"""
Дележ прибыли между Мишей и Даней.

Условия разные по линейкам и по времени:
  • физтовары до 02.09.2026 — Даня получает 10% от стоимости товара (без
    доставки) плюс 200 ₽ за каждую напечатанную им ручку;
  • цифровые товары (пресеты) — 80/20.

С 02.09.2026 бизнес общий: вся ЧИСТАЯ прибыль по заказам этой эпохи
делится 60/40 в пользу Миши. Чистая — уже за вычетом комиссии Prodamus,
НПД, доставки в СДЭК и расходов на материалы: расходы теперь несут оба
в той же пропорции, а не один Миша. Отдельной платы за печать нет.

Граница проходит по дате заказа (у расходов — по дате траты), а не по
дате расчёта: период может её пересекать, и тогда каждая половина
считается по своим условиям. Задним числом старые заказы не
переигрываем — расчёты по ним уже проведены.
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


_EMPTY_ERA = {"gross": 0.0, "physical": 0.0, "digital": 0.0, "delivery": 0.0,
              "count": 0, "fee": 0.0, "npd": 0.0, "out": 0.0, "expenses": 0.0,
              "net": 0.0, "legacy": 0.0}


async def era_totals(dt_from: str, dt_to: str, fee_rate: float, fee_pct: float) -> dict:
    """Деньги за кусок периода: выручка, вычеты и чистая прибыль.

    Считается по каждую сторону границы условий отдельно: доли разные, а
    вычеты (комиссия, налог, доставка, материалы) относятся к своей эпохе
    и делятся по её правилам.
    """
    if dt_from > dt_to:
        return dict(_EMPTY_ERA)
    goods = await db.get_period_revenue(dt_from, dt_to)
    physical = float(goods["physical"])
    digital = float(goods["digital"])
    delivery = float(goods["delivery"])
    legacy = float(goods["delivery_legacy"])
    gross = physical + digital + delivery
    expenses = (await db.get_expenses_summary(dt_from, dt_to))["total"]
    fee = gross * fee_rate
    npd = gross * NPD_RATE
    out = delivery_out(float(goods["delivery_cost"]), legacy, fee_pct)
    return {"gross": gross, "physical": physical, "digital": digital,
            "delivery": delivery, "count": int(goods["count"]),
            "fee": fee, "npd": npd, "out": out, "expenses": expenses,
            "net": gross - fee - npd - out - expenses, "legacy": legacy}


async def split(dt_from: str, dt_to: str, fee_pct: float | None = None) -> dict:
    """Полная раскладка денег и долей за период.

    Границы — даты или моменты времени, включительно с обеих сторон.
    Возвращает суммы, вычеты, число напечатанных ручек и доли обоих.
    """
    dt_from, dt_to = _bounds(dt_from, dt_to)
    fee_pct = config.PRODAMUS_FEE_PERCENT if fee_pct is None else fee_pct
    fee_rate = fee_pct / 100

    cut = split_moment()
    was = await era_totals(dt_from, min(dt_to, _before(cut)), fee_rate, fee_pct)
    now = await era_totals(max(dt_from, cut), dt_to, fee_rate, fee_pct)

    physical = was["physical"] + now["physical"]
    digital = was["digital"] + now["digital"]
    delivery = was["delivery"] + now["delivery"]
    gross = was["gross"] + now["gross"]
    fee = was["fee"] + now["fee"]
    npd = was["npd"] + now["npd"]
    out = was["out"] + now["out"]
    expenses = was["expenses"] + now["expenses"]
    net = was["net"] + now["net"]

    credits = await _print_credits(await _orders_in_period(dt_from, dt_to))
    printed = credits.get(config.PARTNER_ID, 0)
    # Печать оплачивается отдельно только по заказам старой эпохи
    old_orders = [o for o in await _orders_in_period(dt_from, dt_to)
                  if (o.get("created_at") or "") < cut]
    printed_paid = (await _print_credits(old_orders)).get(config.PARTNER_ID, 0)

    # Старая эпоха: процент не с «грязной» стоимости товара, а с чистой,
    # после вычета комиссии Prodamus и налога НПД (доставка — транзит, в
    # неё не входит). Выведено из того, что реально остаётся «К выплате»
    # по каждому заказу в финансовом листе: (товар + доставка) минус
    # комиссия, налог и отложенное на СДЭК — доставка при этом гасит сама
    # себя, кроме старых заказов (до точного счёта СДЭК), где наценка на
    # доставку не покрывала налог — оттуда и поправка на legacy.
    # Материалы в этой эпохе целиком на Мише: они уменьшают его остаток.
    physical_net_was = (was["physical"] * (1 - fee_rate - NPD_RATE)
                        - was["legacy"] * NPD_RATE)
    digital_net_was = was["digital"] * (1 - fee_rate - NPD_RATE)

    partner_goods = physical_net_was * config.PARTNER_GOODS_PERCENT / 100
    partner_print = printed_paid * config.PARTNER_PRINT_FEE
    partner_digital = digital_net_was * config.PARTNER_DIGITAL_PERCENT / 100
    # Новая эпоха: бизнес общий — делим чистую прибыль целиком, вместе с
    # расходами на материалы, а не только процент с товара
    partner_new = now["net"] * config.PARTNER_GOODS_PERCENT_NEW / 100
    partner = partner_goods + partner_print + partner_digital + partner_new

    # Уже выплаченное внутри периода: доля начисляется за весь период, но
    # часть её могли отдать раньше расчёта — частичной выплатой. Без этого
    # «Взаиморасчёт» повторно показывал бы уже отданные деньги как долг.
    paid = (await db.get_payouts_summary(dt_from, dt_to))["by_recipient"]
    paid_partner = float(paid.get(config.PARTNER_NAME, 0))
    paid_owner = float(paid.get(config.OWNER_NAME, 0))

    return {
        "gross": gross, "physical": physical, "digital": digital,
        "delivery": delivery, "count": was["count"] + now["count"],
        "fee": fee, "fee_pct": fee_pct, "npd": npd,
        "delivery_out": out, "expenses": expenses,
        "net": net,
        "printed": printed, "print_credits": credits,
        "printed_paid": printed_paid,
        "net_was": was["net"], "net_now": now["net"],
        "physical_was": was["physical"], "physical_now": now["physical"],
        "expenses_was": was["expenses"], "expenses_now": now["expenses"],
        "partner": partner, "owner": net - partner,
        "paid_partner": paid_partner, "paid_owner": paid_owner,
        "partner_left": partner - paid_partner,
        "owner_left": net - partner - paid_owner,
        "partner_goods": partner_goods,
        "partner_new": partner_new,
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
    if s.get("partner_digital"):
        parts.append(f"цифра {config.PARTNER_DIGITAL_PERCENT:g}% "
                     f"= {s['partner_digital']:,.2f}")
    if s.get("partner_new"):
        parts.append(f"{config.PARTNER_GOODS_PERCENT_NEW:g}% чистой прибыли с "
                     f"{_split_date_ru()} = {s['partner_new']:,.2f}")
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
