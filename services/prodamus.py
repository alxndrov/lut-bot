r"""
Интеграция с Prodamus по официальной документации.

Алгоритм подписи:
  1. products[0][name] и т.п. → вложенный список products:[{...}]
  2. Рекурсивно сортируем ключи, все значения → строки
  3. json.dumps(ensure_ascii=False, separators=(',',':'))
  4. Экранируем / → \/
  5. HMAC-SHA256(secret, json_string)

Подпись платёжной ссылки  → параметр URL signature
Подпись входящего вебхука → HTTP-заголовок Sign

order_id в URL → order_num в вебхуке (наш сквозной идентификатор).
"""
import json
import hmac
import hashlib
import logging
import re
from urllib.parse import quote

logger = logging.getLogger(__name__)


# ---------- Подпись ----------

def _nest_products(flat: dict) -> dict:
    """
    Превращает плоские products[0][name] и т.п.
    в вложенную структуру {'products': [{'name':..., 'price':...}]}.
    """
    result = {}
    products: dict[int, dict] = {}

    for key, value in flat.items():
        m = re.match(r'^products\[(\d+)\]\[(\w+)\]$', key)
        if m:
            idx, field = int(m.group(1)), m.group(2)
            products.setdefault(idx, {})[field] = value
        else:
            result[key] = value

    if products:
        result['products'] = [products[i] for i in sorted(products)]

    return result


def _sort_dict(d):
    """Рекурсивно сортирует ключи, значения → строки."""
    if isinstance(d, dict):
        return {k: _sort_dict(v) for k, v in sorted(d.items())}
    if isinstance(d, list):
        return [_sort_dict(i) for i in d]
    return str(d)


def make_signature(data: dict, secret: str) -> str:
    """Вычисляет подпись по алгоритму Prodamus."""
    nested = _nest_products(data)
    sorted_data = _sort_dict(nested)
    json_str = json.dumps(sorted_data, ensure_ascii=False, separators=(",", ":"))
    json_str = json_str.replace("/", "\\/")
    return hmac.new(secret.encode(), json_str.encode(), hashlib.sha256).hexdigest()


def verify_webhook(post_data: dict, sign_header: str, secret: str) -> bool:
    """
    Проверяет подпись входящего вебхука.
    sign_header — значение HTTP-заголовка Sign.
    """
    if not sign_header:
        return False
    data = {k: v for k, v in post_data.items() if k != "sign"}
    expected = make_signature(data, secret)
    return hmac.compare_digest(expected.lower(), sign_header.lower())


# ---------- URL-билдер ----------

def _urlencode(params: dict) -> str:
    """urlencode — скобки кодируются как %5B%5D (как PHP http_build_query)."""
    return "&".join(
        f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
        for k, v in params.items()
    )


def build_payment_url(
    shop_url: str,
    product_name: str,
    price: int,
    user_id: int,
    product_id: int,
    order_type: str = "d",
    secret: str = "",
    notification_url: str = "",
) -> str:
    """
    Строит подписанную платёжную ссылку.
    - Цена зафиксирована подписью signature — покупатель не может её изменить.
    - order_id = '{order_type}_{user_id}_{product_id}' вернётся в вебхуке как order_num.
    - notification_url → параметр urlNotification: адрес, куда Prodamus пришлёт webhook.
      Задаётся в самой ссылке (участвует в подписи), поэтому не зависит от настроек
      кабинета Prodamus и переживает переезд на другой сервер.

    Работает и на новой платформе (Prodamus.Pay): магазин сам отвечает 302
    на link.payform.ru/?orderId=<uuid>, создав заказ. Ломает её только
    эмодзи в названии товара — их снимает clean_product_name.
    """
    params = _payment_params(product_name, price, user_id, product_id,
                             order_type, secret, notification_url, do="pay")
    return shop_url.rstrip("/") + "/?" + _urlencode(params)


def clean_product_name(name: str) -> str:
    """Убирает из названия эмодзи — новая платформа Prodamus их не принимает.

    Названия товаров у нас с эмодзи («🎤 Microphone Handled Kit»), а
    Prodamus.Pay на таком названии отвечает 400 Bad Request, и клиент
    видит ошибку вместо оплаты. Ломаются только символы вне BMP
    (эмодзи, кодовая точка > U+FFFF) — «✅», «×», «—», ««»» проходят,
    поэтому вырезаем именно их, не трогая остальное.
    """
    out = "".join(ch for ch in (name or "") if ord(ch) <= 0xFFFF)
    return re.sub(r"\s+", " ", out).strip() or "Заказ"


def _payment_params(product_name: str, price: int, user_id: int, product_id: int,
                    order_type: str, secret: str, notification_url: str,
                    do: str) -> dict:
    params = {
        "do": do,
        "order_id": f"{order_type}_{user_id}_{product_id}",
        "products[0][name]": clean_product_name(product_name),
        "products[0][price]": str(price),
        "products[0][quantity]": "1",
    }
    if notification_url:
        params["urlNotification"] = notification_url
    if secret:
        params["signature"] = make_signature(params, secret)
    return params
