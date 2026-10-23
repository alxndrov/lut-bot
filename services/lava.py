"""lava.top — оплата цифровых товаров картой зарубежного банка (с 23.10.2026).

Кабинет и продукты — Миши (самозанятый Барышов М. П.): «SHOT ON IPHONE + CUT»
для курса и «MALIMABI LUT APPLE LOG» для LUT, оба с ценой «по запросу API».
Бот создаёт счёт (POST /api/v3/invoice) с суммой в USD и email покупателя,
lava.top присылает вебхук payment.success, статус бот перепроверяет запросом
счёта. Чеков lava.top не делает — их пробивает бот в «Мой налог» Миши.

Цена в USD (решение Дани 23.10.2026): рублёвая цена ÷ курс ЦБ × (1 + 5%) на
комиссию, вверх до целого доллара, не меньше $5 (минимум lava.top).
Документация: https://developers.lava.top/ru, спецификация — gate.lava.top/docs.
"""
import json
import math
import re
from datetime import datetime, timedelta, timezone

import aiohttp

import config
import database as db

API = "https://gate.lava.top"
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
TIMEOUT = aiohttp.ClientTimeout(total=30)
MIN_USD = 5
S_RATE = "lava_usd_rate"          # {"date": "YYYY-MM-DD", "rate": 81.1255}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
MSK = timezone(timedelta(hours=3))


class LavaError(Exception):
    pass


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match((email or "").strip())) and len(email) <= 254


async def usd_rate() -> float:
    """Курс ЦБ на сегодня (кэш на день). Нет связи — последний известный."""
    today = datetime.now(MSK).date().isoformat()
    cached = await db.get_setting(S_RATE)
    if cached:
        c = json.loads(cached)
        if c.get("date") == today:
            return float(c["rate"])
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(CBR_URL) as r:
                data = json.loads(await r.text())
        rate = float(data["Valute"]["USD"]["Value"])
        await db.set_setting(S_RATE, json.dumps({"date": today, "rate": rate}))
        return rate
    except Exception as e:
        if cached:
            return float(json.loads(cached)["rate"])
        raise LavaError(f"нет курса ЦБ: {e}") from e


def usd_price(rub: float, rate: float) -> int:
    return max(MIN_USD, math.ceil(rub / rate * (1 + config.LAVA_MARKUP_PERCENT / 100)))


def offer_for(kind: str) -> str:
    """Курс (c) — продукт курса, остальная цифра — LUT."""
    return config.LAVA_OFFER_COURSE if kind == "c" else config.LAVA_OFFER_LUT


async def _request(method: str, path: str, body: dict | None = None) -> dict:
    headers = {"X-Api-Key": config.LAVA_API_KEY, "Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        async with s.request(method, API + path, json=body, headers=headers) as r:
            text = await r.text()
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = {"error": text[:300]}
            if r.status >= 400:
                raise LavaError(f"{r.status}: {data.get('error') or data}")
            return data


async def create_invoice(kind: str, order_num: str, usd: int, email: str,
                         return_url: str = "") -> dict:
    body = {"email": email.strip(), "offerId": offer_for(kind), "currency": "USD",
            "amount": usd, "buyerLanguage": "EN", "clientUtm": {"utm_content": order_num}}
    if return_url:
        body.update(successful_return_url=return_url, failure_return_url=return_url,
                    cancel_return_url=return_url)
    data = await _request("POST", "/api/v3/invoice", body)
    if not data.get("id") or not data.get("paymentUrl"):
        raise LavaError(f"lava.top не вернула счёт: {data}")
    return data


async def get_invoice(invoice_id: str) -> dict:
    return await _request("GET", f"/api/v2/invoices/{invoice_id}")
