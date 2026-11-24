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
import secrets

import config

_KEY = hashlib.sha256(b"paylink:" + config.BOT_TOKEN.encode()).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def link_code(nbytes: int = 9) -> str:
    """Случайный код для ссылки (t.me/<бот>?start=…_<код>, /pay/…). Не кончается
    на «_» или «-»: Telegram и iOS не включают их в ссылку, и код приходит обрезанным."""
    while True:
        code = secrets.token_urlsafe(nbytes)
        if code[-1] not in "-_":
            return code


def _sig(body: str) -> str:
    return _b64(hmac.new(_KEY, body.encode(), hashlib.sha256).digest()[:15])


def make_token(order_num: str, amount: int, name: str, via: str = "",
               manual: bool = False) -> str:
    """manual — ссылку админ отправляет клиенту сам, в личке: бот по такому
    заказу клиенту ничего не напоминает (pay_router._backup_allowed)."""
    data = [order_num, int(amount), name, via] + ([1] if manual else [])
    body = _b64(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
    return f"{body}.{_sig(body)}"


def parse_token(token: str) -> dict | None:
    """{order_num, amount, name, via, manual, kind, user_id} или None, если
    подпись не та. Старые ссылки — без manual (False)."""
    try:
        body, sig = token.split(".", 1)
        want = _sig(body)
        # Telegram и iOS не включают в ссылку «_» или «-» в самом конце — подпись
        # приходит без последнего символа. Подлинность так же проверена: остальные
        # 19 символов подписи без ключа не подобрать
        if want[-1] in "-_" and len(sig) == len(want) - 1:
            want = want[:-1]
        if not hmac.compare_digest(sig, want):
            return None
        data = json.loads(_unb64(body))
        order_num, amount, name, via = data[:4]
        kind, user_id, _ = order_num.split("_", 2)
        return {"order_num": order_num, "amount": int(amount), "name": name,
                "via": via, "manual": len(data) > 4 and bool(data[4]),
                "kind": kind, "user_id": int(user_id)}
    except Exception:
        return None


def link(order_num: str, amount: int, name: str, via: str = "", manual: bool = False) -> str:
    return f"{config.PAY_BASE_URL}/pay/{make_token(order_num, amount, name, via, manual)}"
