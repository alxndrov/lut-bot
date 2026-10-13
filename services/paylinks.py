"""
Наши ссылки на оплату: https://app.malimabi.com/pay/<токен>.

В токене — всё, чтобы собрать оплату в любом из двух мест: номер заказа
(order_num), сумма и название. Он подписан ключом от токена бота, поэтому
сумму в ссылке не подменить — как и подпись в ссылке Prodamus. Хранить
ничего не нужно: build_payment_url остаётся синхронной и вызывается
прямо при отрисовке кнопок.

via — куда вести независимо от настройки: 'yk' у запасной ссылки.
"""
import base64
import hashlib
import hmac
import json

import config

_KEY = hashlib.sha256(b"paylink:" + config.BOT_TOKEN.encode()).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sig(body: str) -> str:
    return _b64(hmac.new(_KEY, body.encode(), hashlib.sha256).digest()[:15])


def make_token(order_num: str, amount: int, name: str, via: str = "") -> str:
    body = _b64(json.dumps([order_num, int(amount), name, via],
                           ensure_ascii=False, separators=(",", ":")).encode())
    return f"{body}.{_sig(body)}"


def parse_token(token: str) -> dict | None:
    """{order_num, amount, name, via, kind, user_id} или None, если подпись не та."""
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sig(body)):
            return None
        order_num, amount, name, via = json.loads(_unb64(body))
        kind, user_id, _ = order_num.split("_", 2)
        return {"order_num": order_num, "amount": int(amount), "name": name,
                "via": via, "kind": kind, "user_id": int(user_id)}
    except Exception:
        return None


def link(order_num: str, amount: int, name: str, via: str = "") -> str:
    return f"{config.PAY_BASE_URL}/pay/{make_token(order_num, amount, name, via)}"
