"""Взаиморасчёт Дани и Миши — как в Splitwise (решение Дани 27.10.2026).

Деньги проекта лежат у обоих: у Дани — Альфа (Prodamus, ЮKassa, переводы
ему), у Миши — PayAnyWay, lava.top и переводы ему. Для каждого:

  у него на руках = принятое его каналами − комиссия − его налог оплаченный
                    − (у Дани) СДЭК, расходы проекта, возвраты, ранние выплаты
                    − выплаты, сделанные из его денег ± переводы между нами;
  ему нужно оставить = резервы, которые платит он (свой НПД к уплате; у Дани
                    ещё СДЭК) + его доля прибыли к выплате (payout.split с
                    прошлого расчёта, с возмещением личных трат и переносом);
  позиция = на руках − нужно оставить. Плюс — лишние деньги, минус — не хватает.

Итог расчёта — один перевод от того, у кого лишнее, тому, кому не хватает,
после чего каждый забирает свою долю из своих денег (выплаты пишутся с
source = чьими деньгами). Резервы остаются у тех, кто будет платить.

Траты личными деньгами (expenses.paid_by = имя) деньги проекта не трогают —
их возвращают долей при расчёте, поэтому «у Дани + у Миши» больше «Должно
быть на счету» листа ровно на такие траты: лист вычитает все расходы.
Деньги, которые Prodamus/ЮKassa ещё не перевели, — Данины, но в пути: в
позиции они считаются, а платить из них нельзя.
"""
from datetime import datetime, timedelta, timezone

import config
import database as db

MSK = timezone(timedelta(hours=3))
NAMES = {"danya": config.PARTNER_NAME, "misha": config.OWNER_NAME}
WHO = {"partner": "danya", "owner": "misha"}   # как в payout.split


def _r(x: float) -> float:
    return round(x + 0.0, 2)


async def state() -> dict:
    from handlers.finance import _pending_gross, _settle_split
    from services import tax_account
    s, last, dt_from, dt_to, now_utc = await _settle_split()
    if s is None:
        raise RuntimeError("не удалось посчитать доли (payout.split)")
    inc = await db.income_by_holder()
    npd_paid = await db.npd_paid_by()
    from_whose = await db.payouts_by_source()
    exp = await db.expenses_by_payer()
    cdek = await db.get_cdek_account()
    moved = await db.transfers_net()
    pending = (await _pending_gross("1900-01-01 00:00:00", now_utc.strftime("%Y-%m-%d %H:%M:%S"),
                                    now_utc)) * (1 - config.PRODAMUS_FEE_PERCENT / 100)

    d, m = inc["danya"], inc["misha"]
    held = {
        "danya": (d["gross"] - d["fee"] - d.get("refunds", 0) - cdek["paid"]
                  - npd_paid.get("danya", 0) - exp["project"] - from_whose.get("danya", 0)
                  - config.EARLY_PAYOUTS - pending + moved["danya"]),
        "misha": (m["gross"] - m["fee"] - npd_paid.get("misha", 0)
                  - from_whose.get("misha", 0) + moved["misha"]),
    }
    months = await tax_account.months()
    tax = {"danya": sum(r["accrued"] - r["paid"] for r in months),
           "misha": inc["misha_tax"] - npd_paid.get("misha", 0)}
    cdek_due = cdek["due"]
    share = {"danya": s["partner_left"], "misha": s["owner_left"]}
    reserve = {"danya": tax["danya"] + cdek_due, "misha": tax["misha"]}
    pos = {"danya": held["danya"] + pending - reserve["danya"] - share["danya"],
           "misha": held["misha"] - reserve["misha"] - share["misha"]}
    # Сколько реально можно отдать: у Дани — без денег в пути
    spare = {"danya": pos["danya"] - pending, "misha": pos["misha"]}
    transfer = None
    for src, dst in (("misha", "danya"), ("danya", "misha")):
        if spare[src] >= 1 and pos[dst] <= -1:
            transfer = {"from": src, "to": dst, "amount": _r(min(spare[src], -pos[dst]))}
    personal = sum(v for k, v in exp.items() if k != "project")
    return {
        "since": last["settled_at"] if last else None, "split": s, "now": now_utc,
        "held": {k: _r(v) for k, v in held.items()}, "pending": _r(pending),
        "tax": {k: _r(v) for k, v in tax.items()}, "cdek_due": _r(cdek_due),
        "reserve": {k: _r(v) for k, v in reserve.items()},
        "share": {k: _r(v) for k, v in share.items()},
        "reimb": {"danya": _r(s.get("reimb_partner", 0)), "misha": _r(s.get("reimb_owner", 0))},
        "pos": {k: _r(v) for k, v in pos.items()}, "transfer": transfer,
        "personal": _r(personal),
        "on_accounts": _r(held["danya"] + held["misha"] - personal),
        "unallocated": _r(pos["danya"] + pos["misha"]),
        "paw_balance": _r(await db.paw_balance("payanyway") + await db.paw_balance("lava")),
    }


def _rub(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ") + " ₽"


def _since(st: dict) -> str:
    if not st["since"]:
        return "с начала"
    t = datetime.strptime(st["since"][:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    return "с " + (t.replace(tzinfo=timezone.utc).astimezone(MSK)).strftime("%d.%m")


DATIVE = {"danya": "Дане", "misha": "Мише"}


def verdict(st: dict) -> str:
    t = st["transfer"]
    if not t:
        return "Переводить друг другу ничего не нужно"
    return f"{NAMES[t['from']]} переводит {DATIVE[t['to']]} {_rub(t['amount'])}"


def text(st: dict) -> str:
    d, m = "danya", "misha"
    lines = [
        f"🤝 <b>Взаиморасчёт</b> — {st['now'].astimezone(MSK).strftime('%d.%m.%Y %H:%M')}", "",
        f"💼 <b>Деньги проекта у нас: {_rub(st['held'][d] + st['held'][m])}</b>",
        f"• у Дани: {_rub(st['held'][d])}" + (f" (+ {_rub(st['pending'])} ещё в пути от Prodamus/ЮKassa)"
                                             if st["pending"] >= 0.01 else ""),
        f"• у Миши: {_rub(st['held'][m])}" + (f" (из них на балансе PayAnyWay/lava.top {_rub(st['paw_balance'])})"
                                             if st["paw_balance"] >= 0.01 else ""),
        "",
        "🔒 <b>Резервы — остаются у того, кто платит</b>",
        f"• Даня: налог {_rub(st['tax'][d])}, СДЭК {_rub(st['cdek_due'])}",
        f"• Миша: налог {_rub(st['tax'][m])}",
        "",
        f"💰 <b>Доли к выплате {_since(st)}</b>",
        f"• Даня: {_rub(st['share'][d])}" + (f" (в т.ч. возмещение личных трат {_rub(st['reimb'][d])})"
                                            if st["reimb"][d] >= 0.01 else ""),
        f"• Миша: {_rub(st['share'][m])}" + (f" (в т.ч. возмещение личных трат {_rub(st['reimb'][m])})"
                                            if st["reimb"][m] >= 0.01 else ""),
        "",
        f"➡️ <b>{verdict(st)}</b>",
        "Потом каждый забирает свою долю из своих денег, резервы остаются на месте.",
    ]
    if abs(st["unallocated"] + st["pending"]) >= 1 and abs(st["unallocated"]) >= 1:
        lines += ["", f"<i>Вне долей и резервов: {_rub(st['unallocated'])} — "
                      + ("свободные деньги, их никто не забирает." if st["unallocated"] > 0 else
                         "не хватает: часть долей ждёт денег в пути, остаток перейдёт в следующий расчёт.")
                      + "</i>"]
    return "\n".join(lines)


async def settle(user_id: int, user_name: str) -> dict:
    """«Перевели»: записывает перевод между нами, выплаты обоим их долей из их
    же денег (не больше, чем у них есть сверх резервов) и сам расчёт —
    недоплаченное переносится долгом в следующий."""
    st = await state()
    now = datetime.now(timezone.utc)
    paid_at = (now - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    comment = f"Взаиморасчёт {now.astimezone(MSK).strftime('%d.%m.%Y')}"
    held = dict(st["held"])
    t = st["transfer"]
    if t:
        await db.add_partner_transfer(t["from"], t["to"], t["amount"], comment, user_id, user_name,
                                      paid_at)
        held[t["from"]] -= t["amount"]
        held[t["to"]] += t["amount"]
    paid, carry = {}, {}
    for who in ("danya", "misha"):
        can = max(0.0, held[who] - st["reserve"][who])
        paid[who] = _r(max(0.0, min(st["share"][who], can)))
        carry[who] = _r(max(0.0, st["share"][who] - paid[who]))
        if paid[who] >= 0.01:
            payout_id = await db.add_payout(NAMES[who], paid[who], comment, user_id, user_name,
                                            paid_at=paid_at, source=who)
            from services.gsheets import request_expense_append
            request_expense_append(f"payout-{payout_id}", now.astimezone(MSK).strftime("%d.%m.%Y %H:%M"),
                                   paid[who], f"Выплата {NAMES[who]}: {comment}")
    s = st["split"]
    await db.add_settlement(gross=s["gross"], fee=s["fee"], net=s["net"], count=s["count"],
                            carry_owner=carry["misha"], carry_partner=carry["danya"])
    return {"state": st, "paid": paid, "carry": carry}


def settled_text(res: dict) -> str:
    st = res["state"]
    lines = [f"✅ <b>Расчёт зафиксирован</b>", "", f"➡️ {verdict(st)}"]
    for who in ("danya", "misha"):
        line = f"👤 {NAMES[who]} забирает {_rub(res['paid'][who])} из своих"
        if res["carry"][who] >= 0.01:
            line += f"; ещё {_rub(res['carry'][who])} — в следующий расчёт"
        lines.append(line)
    lines += ["", f"🔒 Остаются в резерве: у Дани {_rub(st['reserve']['danya'])}, "
                  f"у Миши {_rub(st['reserve']['misha'])}"]
    return "\n".join(lines)


# ── Лист «Взаиморасчёт» ───────────────────────────────────────────────────────

def sheet_values(st: dict, transfers: list[dict]) -> list[list]:
    d, m = "danya", "misha"
    rows = [
        ["ВЗАИМОРАСЧЁТ ДАНИ И МИШИ", "", "Обновляет бот, руками не править"],
        ["Деньги проекта у нас", st["held"][d] + st["held"][m],
         "= «Должно быть на счету» + траты, оплаченные лично (их вернут долей)"],
        ["  из них у Дани", st["held"][d], "Альфа: Prodamus, ЮKassa, переводы Дане"],
        ["  из них у Миши", st["held"][m], "PayAnyWay, lava.top, переводы Мише"],
        ["  ещё в пути к Дане", st["pending"], "Prodamus/ЮKassa ещё не перевели"],
        ["  оплачено лично (вернём долей)", st["personal"], ""],
        [],
        ["РЕЗЕРВЫ (остаются у того, кто платит)", "Даня", "Миша"],
        ["Налог НПД к уплате", st["tax"][d], st["tax"][m]],
        ["СДЭК к оплате", st["cdek_due"], 0],
        [],
        [f"ДОЛИ К ВЫПЛАТЕ ({_since(st)})", "Даня", "Миша"],
        ["Доля прибыли (с возмещениями и переносом)", st["share"][d], st["share"][m]],
        [],
        ["ПОЗИЦИЯ", "Даня", "Миша"],
        ["На руках", st["held"][d] + st["pending"], st["held"][m]],
        ["− резервы", st["reserve"][d], st["reserve"][m]],
        ["− доля к выплате", st["share"][d], st["share"][m]],
        ["= лишнее (+) / не хватает (−)", st["pos"][d], st["pos"][m]],
        [],
        ["ИТОГ", verdict(st)],
        [],
        ["ПЕРЕВОДЫ МЕЖДУ НАМИ", "Сумма", "Комментарий"],
    ]
    for t in transfers:
        when = datetime.strptime(str(t["paid_at"])[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).astimezone(MSK).strftime("%d.%m.%Y")
        rows.append([f"{when}: {NAMES[t['from_who']]} → {NAMES[t['to_who']]}", t["amount"],
                     t.get("comment") or ""])
    return rows


# ── Напоминание 1-го и 16-го ───────────────────────────────────────────────────

S_NOTIFIED = "settle_notified_on"   # дата МСК, за которую уже напомнили
DAYS = (1, 16)
HOUR = 10


async def remind_once(now_msk: datetime) -> bool:
    """1-го и 16-го в 10:00 МСК — итог взаиморасчёта админам с кнопкой
    «Перевели». Один раз в день, даже после перезапуска бота."""
    today = now_msk.date().isoformat()
    if now_msk.day not in DAYS or now_msk.hour < HOUR or await db.get_setting(S_NOTIFIED) == today:
        return False
    await db.set_setting(S_NOTIFIED, today)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from services.npd_receipts import _notify_admins_kb
    st = await state()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Перевели — зафиксировать", callback_data="fin_settle")],
        [InlineKeyboardButton(text="🤝 Открыть взаиморасчёт", callback_data="fin_settle_menu")]])
    await _notify_admins_kb("📅 <b>Пора рассчитаться</b>\n\n" + text(st), kb)
    return True


async def remind_loop():
    import asyncio
    import logging
    while True:
        try:
            await remind_once(datetime.now(MSK))
        except Exception:
            logging.getLogger(__name__).exception("settle: напоминание не отправилось")
        await asyncio.sleep(600)
