"""PayAnyWay (НКО «МОНЕТА») — основная оплата с 23.10.2026.

Платежи принимает самозанятый Барышов Михаил Павлович (бизнес-счёт
PAYANYWAY_MNT_ID); чеки в «Мой налог» делает сам PayAnyWay.Self — бот их
по этим оплатам не пробивает.

Ссылка на оплату — форма assistant.htm с подписью MNT_SIGNATURE. Об оплате
PayAnyWay сообщает на Pay URL (задаётся в кабинете: «Мой магазин» →
https://app.malimabi.com/payanyway/notify), мы проверяем подпись и сумму и
отвечаем XML с MNT_RESULT_CODE=200. Документация:
https://docs.moneta.ru/assistant/v1/
"""
import hashlib
import secrets
from urllib.parse import urlencode

import config


class PayAnyWayError(Exception):
    pass


def amount_str(amount: float) -> str:
    """В подписи сумма всегда с двумя знаками после точки: 1351.00."""
    return f"{float(amount):.2f}"


def _md5(*parts) -> str:
    return hashlib.md5("".join(str(p) for p in parts).encode()).hexdigest()


def new_transaction_id(order_num: str) -> str:
    """Номер оплаты в PayAnyWay: заказ + случайный хвост (повторная попытка
    после смены цены — новый номер, у PayAnyWay номер оплачивается один раз)."""
    return f"{order_num}-{secrets.token_hex(3)}"


def form_url(transaction_id: str, amount: float, description: str, subscriber_id: str = "",
             return_url: str = "") -> str:
    test = "1" if config.PAYANYWAY_TEST_MODE else "0"
    params = {
        "MNT_ID": config.PAYANYWAY_MNT_ID,
        "MNT_TRANSACTION_ID": transaction_id,
        "MNT_AMOUNT": amount_str(amount),
        "MNT_CURRENCY_CODE": "RUB",
        "MNT_TEST_MODE": test,
        "MNT_DESCRIPTION": description[:500],
        "MNT_SUBSCRIBER_ID": subscriber_id,
        "MNT_SIGNATURE": _md5(config.PAYANYWAY_MNT_ID, transaction_id, amount_str(amount), "RUB",
                              subscriber_id, test, config.PAYANYWAY_SECRET),
    }
    if return_url:
        # Работает, если в кабинете разрешено «Можно переопределять настройки в URL»
        params.update(MNT_SUCCESS_URL=return_url, MNT_FAIL_URL=return_url,
                      MNT_RETURN_URL=return_url)
    return f"{config.PAYANYWAY_URL}?{urlencode(params)}"


def notification_valid(p: dict) -> bool:
    """Подпись уведомления об оплате (Pay URL)."""
    expected = _md5(p.get("MNT_ID", ""), p.get("MNT_TRANSACTION_ID", ""),
                    p.get("MNT_OPERATION_ID", ""), p.get("MNT_AMOUNT", ""),
                    p.get("MNT_CURRENCY_CODE", ""), p.get("MNT_SUBSCRIBER_ID", ""),
                    p.get("MNT_TEST_MODE", ""), config.PAYANYWAY_SECRET)
    return secrets.compare_digest(expected, (p.get("MNT_SIGNATURE") or "").lower())


def response_xml(transaction_id: str, code: int) -> str:
    """Ответ на уведомление: 200 — принято, 500 — не повторять (подпись не та и т. п.)."""
    sig = _md5(code, config.PAYANYWAY_MNT_ID, transaction_id, config.PAYANYWAY_SECRET)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f"<MNT_RESPONSE><MNT_ID>{config.PAYANYWAY_MNT_ID}</MNT_ID>"
            f"<MNT_TRANSACTION_ID>{transaction_id}</MNT_TRANSACTION_ID>"
            f"<MNT_RESULT_CODE>{code}</MNT_RESULT_CODE>"
            f"<MNT_SIGNATURE>{sig}</MNT_SIGNATURE></MNT_RESPONSE>")
