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
их возвращают долей при расчёте; «Ушло на расходы» в «Финансах» тоже
считает только траты со счёта проекта, так что «у Дани + у Миши» = «Должно
сейчас быть на счету». То же самое формулами — лист «Взаиморасчёт».
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
                  - pending + moved["danya"]),
        # Комиссию PayAnyWay сервис берёт при выводе: с денег Миши вычитаем
        # только уже взятую, остальное — его резерв (ниже)
        "misha": (m["gross"] - (m["fee"] - m.get("paw_fee", 0)) - m.get("withdraw_fee", 0)
                  - npd_paid.get("misha", 0) - from_whose.get("misha", 0) + moved["misha"]),
    }
    paw_left = await db.paw_balance("payanyway")
    paw_fee = (max(paw_left * config.PAYANYWAY_FEE_PERCENT / 100, config.PAYANYWAY_FEE_MIN)
               if paw_left >= 0.01 else 0.0)
    months = await tax_account.months()
    tax = {"danya": sum(r["accrued"] - r["paid"] for r in months),
           "misha": inc["misha_tax"] - npd_paid.get("misha", 0)}
    cdek_due = cdek["due"]
    share = {"danya": s["partner_left"], "misha": s["owner_left"]}
    reserve = {"danya": tax["danya"] + cdek_due, "misha": tax["misha"] + paw_fee}
    # Как на листе: свободные деньги — то, что уже на руках после резервов;
    # деньги в пути от Prodamus/ЮKassa выровняет следующий расчёт (решение Дани)
    pos = {who: held[who] - reserve[who] - share[who] for who in ("danya", "misha")}
    spare = pos
    transfer = None
    for src, dst in (("misha", "danya"), ("danya", "misha")):
        if spare[src] >= 1 and pos[dst] <= -1:
            transfer = {"from": src, "to": dst, "amount": _r(min(spare[src], -pos[dst]))}
    return {
        "since": last["settled_at"] if last else None, "split": s, "now": now_utc,
        "held": {k: _r(v) for k, v in held.items()}, "pending": _r(pending),
        "tax": {k: _r(v) for k, v in tax.items()}, "cdek_due": _r(cdek_due),
        "paw_fee": _r(paw_fee),
        "reserve": {k: _r(v) for k, v in reserve.items()},
        "share": {k: _r(v) for k, v in share.items()},
        "reimb": {"danya": _r(s.get("reimb_partner", 0)), "misha": _r(s.get("reimb_owner", 0))},
        "pos": {k: _r(v) for k, v in pos.items()}, "transfer": transfer,
        "on_accounts": _r(held["danya"] + held["misha"]),
        "unallocated": _r(pos["danya"] + pos["misha"]),
        "paw_balance": _r(await db.paw_balance("payanyway") + await db.paw_balance("lava")),
    }


def _rub(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


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
        f"• Миша: налог {_rub(st['tax'][m])}"
        + (f", комиссия PayAnyWay при выводе {_rub(st['paw_fee'])}" if st["paw_fee"] >= 0.01 else ""),
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


def plan(st: dict) -> tuple[dict, dict, dict]:
    """Что будет после перевода: (на руках, забирает себе, переносится долгом).
    Себе — доля, но не больше, чем остаётся сверх резервов."""
    held = dict(st["held"])
    t = st["transfer"]
    if t:
        held[t["from"]] -= t["amount"]
        held[t["to"]] += t["amount"]
    paid, carry = {}, {}
    for who in ("danya", "misha"):
        can = max(0.0, held[who] - st["reserve"][who])
        paid[who] = _r(max(0.0, min(st["share"][who], can)))
        carry[who] = _r(max(0.0, st["share"][who] - paid[who]))
    return held, paid, carry


def who_is(user_id: int) -> str:
    return "danya" if user_id == config.PARTNER_ID else "misha"


GENITIVE = {"danya": "Дани", "misha": "Миши"}


def my_text(st: dict, me: str) -> str:
    """«Что мне сделать для взаиморасчёта» — то же, что text(), но от лица me."""
    other = "misha" if me == "danya" else "danya"
    held, paid, carry = plan(st)
    t = st["transfer"]
    lines = [f"🙋 <b>{NAMES[me]}, что тебе сделать для взаиморасчёта</b>",
             f"<i>{st['now'].astimezone(MSK).strftime('%d.%m.%Y %H:%M')}, доли {_since(st)}</i>", "",
             f"Сейчас у тебя денег проекта: <b>{_rub(st['held'][me])}</b>"
             + (f" (+ {_rub(st['pending'])} ещё в пути от Prodamus/ЮKassa — тратить их пока нельзя)"
                if me == "danya" and st["pending"] >= 0.01 else "")
             + (f", из них на балансе PayAnyWay/lava.top {_rub(st['paw_balance'])}"
                if me == "misha" and st["paw_balance"] >= 0.01 else ""), ""]
    step = 1
    if t and t["from"] == me:
        lines.append(f"{step}️⃣ <b>Отправь {DATIVE[other]} {_rub(t['amount'])}</b>")
        step += 1
    elif t:
        lines.append(f"{step}️⃣ <b>Запроси у {GENITIVE[other]} {_rub(t['amount'])}</b> — "
                     f"это его лишние деньги проекта сверх резервов и доли")
        step += 1
    else:
        lines.append("Переводить друг другу ничего не нужно.")
    lines.append(f"{step}️⃣ <b>Забери себе {_rub(paid[me])}</b> — твоя доля прибыли"
                 + (f" (в т.ч. возмещение личных трат {_rub(st['reimb'][me])})" if st["reimb"][me] >= 0.01 else ""))
    if carry[me] >= 1:   # копейки округления не показываем
        lines.append(f"   Ещё {_rub(carry[me])} твоей доли пока не из чего взять — перейдёт в следующий расчёт.")
    step += 1
    res = []
    if me == "danya":
        res.append(f"налог {_rub(st['tax'][me])}")
        if st["cdek_due"] >= 0.01:
            res.append(f"СДЭК {_rub(st['cdek_due'])}")
    else:
        res.append(f"налог {_rub(st['tax'][me])}")
        if st["paw_fee"] >= 0.01:
            res.append(f"будущая комиссия PayAnyWay при выводе {_rub(st['paw_fee'])}")
    lines.append(f"{step}️⃣ <b>Оставь в резерве {_rub(st['reserve'][me])}</b> — " + ", ".join(res))
    rest = _r(held[me] - st["reserve"][me] - paid[me])
    lines += ["", f"После этого у тебя останется денег проекта {_rub(st['reserve'][me] + max(rest, 0.0))}"
              + (f" (резерв + {_rub(rest)} свободных)" if rest >= 1 else " — ровно резерв") + "."]
    if me == "misha" and st["paw_balance"] >= 0.01:
        lines.append("Долю бери с баланса PayAnyWay выводом — запиши его кнопкой «💸 Вывод с PayAnyWay» в /payments.")
    lines += ["", "Когда переведёте — любой из вас жмёт «✅ Перевели — зафиксировать расчёт»."]
    return "\n".join(lines)


async def settle(user_id: int, user_name: str) -> dict:
    """«Перевели»: записывает перевод между нами, выплаты обоим их долей из их
    же денег (не больше, чем у них есть сверх резервов) и сам расчёт —
    недоплаченное переносится долгом в следующий."""
    st = await state()
    now = datetime.now(timezone.utc)
    paid_at = (now - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    comment = f"Взаиморасчёт {now.astimezone(MSK).strftime('%d.%m.%Y')}"
    t = st["transfer"]
    if t:
        await db.add_partner_transfer(t["from"], t["to"], t["amount"], comment, user_id, user_name,
                                      paid_at)
    _, paid, carry = plan(st)
    for who in ("danya", "misha"):
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
# Формулы: свободные деньги — из «Финансов» (строки «После резервов у …» по
# подписи), доли — по cashflow. Значения бот пишет только для того, чего нет
# в cashflow: даты последнего расчёта и перенесённого с него долга.
# «Финансы» берут отсюда только итог — B19 (строка «Взаиморасчёт»).

def _col_letter(i: int) -> str:
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def sheet_values(cols: dict[str, int], last: dict | None, cashflow: str = "cashflow") -> list[list]:
    """Лист «Взаиморасчёт» (решение Дани 28.10.2026): свободные деньги каждого
    берутся из «Финансов» («После резервов у …»), здесь — только доли с
    последнего расчёта и кто кому переводит. Справа (E:F) — прибыль за период,
    от которой считаются доли. cols — {колонка cashflow: номер}."""
    from services.finance_sheet import Formula
    c = {name: f"{cashflow}!${_col_letter(i)}:${_col_letter(i)}" for name, i in cols.items()}
    A, B, C, D = c["Дата операции"], c["Номер операции"], c["Тип"], c["Сумма"]
    H, J, M, R = c["К выплате"], c["Товар"], c["Получатель"], c["Кто внёс расход"]
    U = c["На что расход"]
    fin = "'Финансы'"

    def f(expr):
        return Formula("=" + expr)

    def both(tpl):   # Миша (B) и Даня (C)
        return [f(tpl.format(who='"Миша"')), f(tpl.format(who='"Даня"'))]

    def fin_row(label):   # строка «Финансов» по подписи — Даня переставляет строки
        return f'IFERROR(INDEX({fin}!B:B;MATCH("{label}";{fin}!A:A;0));0)'

    # Проценты целыми: в русской локали таблицы дробь пишется через запятую
    digital = round(config.PARTNER_DIGITAL_PERCENT)      # доля Дани с цифры
    goods = round(config.PARTNER_GOODS_PERCENT_NEW)      # доля Дани с физтоваров
    general = round(config.PARTNER_GENERAL_PERCENT)      # доля Дани в общих расходах
    if last:
        t = datetime.strptime(last["settled_at"][:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
        t = t.replace(tzinfo=timezone.utc).astimezone(MSK)
        since = f(f"DATE({t.year};{t.month};{t.day})+TIME({t.hour};{t.minute};{t.second})")
    else:
        since = 0
    carry_m = float(last.get("carry_owner") or 0) if last else 0.0
    carry_d = float(last.get("carry_partner") or 0) if last else 0.0
    after = f'">"&$B$2'
    def exp(purpose):
        return f(f'SUMIFS({D};{B};"exp-*";{U};"{purpose}";{A};{after})')

    rows = [
        ["ВЗАИМОРАСЧЁТ", "", "", "", "ПРИБЫЛЬ С ПОСЛЕДНЕГО РАСЧЁТА", ""],
        ["Последний расчёт", since, "", "", "Чистыми с цифры",
         f(f'SUMIFS({H};{J};"Цифровой";{C};"<>Расход";{A};{after})')],
        ["Перенос с прошлого расчёта", carry_m, carry_d, "", "Чистыми с физтоваров",
         f(f'SUMIFS({H};{J};"<>Цифровой";{C};"<>Расход";{A};{after})')],
        ["", "", "", "", "Расходы на цифру", exp("Цифра")],
        ["", "Миша", "Даня", "", "Расходы на физтовары", exp("Физтовары")],              # 5
        ["Свободные деньги на руках", f(fin_row("После резервов у Миши")),
         f(fin_row("После резервов у Дани")), "", "Общие расходы", exp("Общее")],
        [],
        ["ДОЛЯ С ПОСЛЕДНЕГО РАСЧЁТА"],                                                # 8
        [f"Цифра минус её расходы {100 - digital}/{digital}",
         f(f"(F2-F4)*{100 - digital}/100"), f(f"(F2-F4)*{digital}/100")],
        [f"Физтовары минус их расходы {100 - goods}/{goods}",
         f(f"(F3-F5)*{100 - goods}/100"), f(f"(F3-F5)*{goods}/100")],
        [f"− общие расходы {100 - general}/{general}",
         f(f"-F6*{100 - general}/100"), f(f"-F6*{general}/100")],
        ["+ возмещение трат, оплаченных лично",
         *both(f'SUMIFS({D};{B};"exp-*";{R};{{who}};{A};{after})')],
        ["− уже выплачено после расчёта",
         *both(f'SUMIFS({D};{B};"payout-*";{M};{{who}};{A};{after})')],
        ["+ перенос", f("B3"), f("C3")],
        ["= Доля к выплате", f("SUM(B9:B11)+B12-B13+B14"), f("SUM(C9:C11)+C12-C13+C14")],  # 15
        [],
        ["Лишнее (+) / не хватает (−)", f("B6-B15"), f("C6-C15")],                   # 17
        [],
        ["КТО КОМУ ПЕРЕВОДИТ",                                                        # 19
         f('IF(AND(B17>=1;C17<=-1);"Миша переводит Дане "&TEXT(MIN(B17;-C17);"#,##0.00")&" ₽";'
           'IF(AND(C17>=1;B17<=-1);"Даня переводит Мише "&TEXT(MIN(C17;-B17);"#,##0.00")&" ₽";'
           '"Переводить друг другу ничего не нужно"))'), "",
         "Потом каждый забирает свою долю из своих денег"],
    ]
    return rows


def finance_formulas(cols: dict[str, int], cashflow: str = "cashflow") -> dict[str, str]:
    """Формулы строк «Финансов», которые считают деньги и резервы по людям
    прямо по cashflow (по подписи строки). «Ушло на СДЭК» и «Ещё не перевёл/
    перевела» берутся из соседних строк «Финансов» тоже по подписи."""
    c = {name: f"{cashflow}!${_col_letter(i)}:${_col_letter(i)}" for name, i in cols.items()}
    B, C, D, E = c["Номер операции"], c["Тип"], c["Сумма"], c["Комиссия Prodamus"]
    F, M, S, T = c["Налог"], c["Получатель"], c["Платёжная система"], c["У кого деньги"]

    def row(label):
        return f'IFERROR(INDEX(B:B;MATCH("{label}";A:A;0));0)'

    def held(who):
        return (f'SUMIFS({D};{C};"<>Расход";{T};"{who}")'
                f'-SUMIFS({E};{C};"<>Расход";{T};"{who}";{S};"<>PayAnyWay")'
                f'-SUMIFS({E};{B};"pawout-*";{T};"{who}")'
                f'-SUMIFS({D};{B};"npd-*";{T};"{who}")'
                f'-SUMIFS({D};{B};"exp-*";{T};"{who}")'
                f'-SUMIFS({D};{B};"payout-*";{T};"{who}")'
                f'+SUMIFS({D};{B};"transfer-*";{M};"{who}")'
                f'-SUMIFS({D};{B};"transfer-*";{T};"{who}")')

    def tax(who):
        return (f'SUMIFS({F};{C};"<>Расход";{T};"{who}")-SUMIFS({D};{B};"npd-*";{T};"{who}")')

    paw_bal = f'(SUMIFS({D};{S};"PayAnyWay";{C};"<>Расход")-SUMIF({B};"pawout-*";{D}))'
    paw_pct = round(config.PAYANYWAY_FEE_PERCENT * 10)   # 4,5% → 45/1000
    return {
        "  из них у Дани": "=ROUND(" + held("Даня") + f'-{row("Ушло на СДЭК")}'
                          f'-{row("Ещё не перевёл Prodamus")}-{row("Ещё не перевела ЮКасса")};2)',
        "  из них у Миши": "=ROUND(" + held("Миша") + ";2)",
        "Ещё отложено на НПД у Дани": "=ROUND(" + tax("Даня") + ";2)",
        "Ещё отложено на НПД у Миши": "=ROUND(" + tax("Миша") + ";2)",
        "Ещё возьмёт комиссию PayAnyWay у Миши":
            f"=IF({paw_bal}>=0,01;ROUND(MAX({paw_bal}*{paw_pct}/1000;{round(config.PAYANYWAY_FEE_MIN)});2);0)",
        "Взаиморасчёт": "='Взаиморасчёт'!B19",
    }


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
