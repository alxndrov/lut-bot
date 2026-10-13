"""
ЮKassa API v3: создать платёж и узнать его статус.

Платёж создаётся в момент перехода клиента по нашей ссылке (/pay/…):
ЮKassa отдаёт confirmation_url, туда и отправляем. Наш order_num едет в
metadata и возвращается в статусе платежа — по нему выдаём товар той же
provision_payment, что и после Prodamus.

Уведомления ЮKassa не подписаны, поэтому статус всегда берём запросом
к API (get_payment), а не из тела уведомления.
"""
import logging
import uuid

import aiohttp

import config

logger = logging.getLogger(__name__)

API = "https://api.yookassa.ru/v3"
TIMEOUT = aiohttp.ClientTimeout(total=15)


class YooKassaError(Exception):
    pass


def credentials(kind: str) -> tuple[str, str]:
    """kind: 'p' — магазин физтоваров, иначе основной."""
    if kind == "p":
        return config.YOOKASSA_SHOP_ID_PHYSICAL, config.YOOKASSA_SECRET_KEY_PHYSICAL
    return config.YOOKASSA_SHOP_ID, config.YOOKASSA_SECRET_KEY


async def _request(method: str, path: str, kind: str, json: dict | None = None) -> dict:
    shop_id, secret = credentials(kind)
    if not (shop_id and secret):
        raise YooKassaError("ЮKassa не настроена")
    headers = {"Idempotence-Key": str(uuid.uuid4())} if method == "POST" else {}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        async with s.request(method, API + path, json=json, headers=headers,
                             auth=aiohttp.BasicAuth(shop_id, secret)) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise YooKassaError(f"{r.status}: {data.get('description') or data}")
            return data


async def create_payment(kind: str, order_num: str, amount: int, description: str,
                         return_url: str) -> dict:
    """Новый платёж; в ответе id и confirmation.confirmation_url."""
    return await _request("POST", "/payments", kind, {
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        # Описание видит клиент на странице оплаты; больше 128 символов ЮKassa не берёт
        "description": description[:128],
        "metadata": {"order_num": order_num},
    })


async def get_payment(kind: str, payment_id: str) -> dict:
    return await _request("GET", f"/payments/{payment_id}", kind)
