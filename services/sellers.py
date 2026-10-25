"""Продавцы по единой оферте (редакция от 24.10.2026): цифровые товары и курс
продаёт Миша (PayAnyWay, lava.top), физические товары (изделия 3D-печати) — Даня
(Prodamus). Продавец показывается покупателю перед оплатой и на экране
«ℹ️ Продавцы и условия» — этого требует PayAnyWay (проверка магазина)."""

MISHA = {"name": "Барышов Михаил Павлович", "short": "Барышов М. П.", "inn": "745312244278"}
DANYA = {"name": "Александров Даниил Константинович", "short": "Александров Д. К.",
         "inn": "745114732883"}
CONTACT = "mikhail.baryshov@gmail.com"


def seller_for(category: str) -> dict:
    return DANYA if category == "physical" else MISHA


def seller_line(category: str) -> str:
    s = seller_for(category)
    return f"🧾 Продавец: самозанятый {s['short']}, ИНН {s['inn']}"
