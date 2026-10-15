"""«Мой налог» (lknpd.nalog.ru): чеки самозанятого за оплаты через ЮKassa.

Prodamus пробивает чеки сам, а ЮKassa для самозанятых перестала это делать
с 29.12.2025 — поэтому чек за платёж ЮKassa бот регистрирует сам.

Официального API у «Мой налог» нет — это API веб-кабинета lknpd.nalog.ru
(тем же пользуются библиотеки shoman4eg/moy-nalog, nalogo и др.). Может
измениться без предупреждения — тогда чек не создастся, админам придёт
сообщение, а бот будет повторять попытку (services/npd_receipts.py).

Вход — один раз по телефону и коду из СМС (/nalog в malimadmins), пароль
не нужен и нигде не хранится. В настройках бота лежит refresh-токен
и id «устройства»; по ним бот сам получает короткий токен доступа.
"""
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

import asyncio

import aiohttp

import config
import database as db

try:
    from python_socks import ProxyError as _ProxyError
except ImportError:
    _ProxyError = OSError

logger = logging.getLogger(__name__)

API = "https://lknpd.nalog.ru/api"
# Сервер слабый и под нагрузкой бывает медленным, налоговая тоже — 20 с не хватало
TIMEOUT = aiohttp.ClientTimeout(total=60)
HEADERS = {"Referrer": "https://lknpd.nalog.ru/auth/login",
           "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
MSK = timezone(timedelta(hours=3))

S_REFRESH, S_DEVICE, S_INN = "npd_refresh_token", "npd_device_id", "npd_inn"

CANCEL_REFUND = "Возврат средств"
CANCEL_MISTAKE = "Чек сформирован ошибочно"

_access: dict = {"token": None, "until": 0.0}


class NpdError(Exception):
    pass


class NpdNotConnected(NpdError):
    pass


TIMEOUT_MSG = "«Мой налог» не ответил вовремя — повторю"


class NpdTimeout(NpdError):
    """Ответа не дождались: запрос мог и пройти (чек создан)."""


async def _device_id() -> str:
    dev = await db.get_setting(S_DEVICE)
    if not dev:
        dev = secrets.token_urlsafe(16)[:21].lower()
        await db.set_setting(S_DEVICE, dev)
    return dev


async def _device_info() -> dict:
    return {"sourceType": "WEB", "sourceDeviceId": await _device_id(), "appVersion": "1.0.0",
            "metaDetails": {"userAgent": HEADERS["User-Agent"]}}


def _session() -> aiohttp.ClientSession:
    """Сессия к налоговой. NPD_PROXY=socks5://… — туннель с домашнего компа
    (имена резолвит та сторона: DNS Oracle налоговую не видит), http://… — обычный прокси."""
    proxy = config.NPD_PROXY
    if proxy.startswith("socks"):
        from aiohttp_socks import ProxyConnector
        return aiohttp.ClientSession(timeout=TIMEOUT,
                                     connector=ProxyConnector.from_url(proxy, rdns=True))
    return aiohttp.ClientSession(timeout=TIMEOUT, proxy=proxy or None)


async def _post(path: str, payload: dict, token: str | None = None, version: str = "v1") -> dict:
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with _session() as s:
            async with s.post(f"{API}/{version}{path}", json=payload, headers=headers) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = {"message": (await r.text())[:300]}
                if r.status >= 400:
                    raise NpdError(f"{r.status}: {(data or {}).get('message') or data}")
                return data or {}
    except (aiohttp.ClientConnectionError, TimeoutError, OSError, _ProxyError) as e:
        # Сервер за границей: налоговая режет зарубежные IP (и даже DNS)
        if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
            # Туннель живой, просто налоговая (или сервер) долго отвечает —
            # чек при этом мог и создаться: retry сперва ищет его (npd_receipts)
            raise NpdTimeout(TIMEOUT_MSG) from e
        hint = ("туннель с домашнего компа не на связи (комп выключен или без интернета?)"
                if config.NPD_PROXY.startswith("socks") else
                "прокси NPD_PROXY не отвечает" if config.NPD_PROXY else
                "налоговая не пускает зарубежные IP — нужен российский прокси (NPD_PROXY)")
        raise NpdError(f"нет связи с «Мой налог»: {hint} ({type(e).__name__})") from e


async def _save_login(data: dict):
    if not data.get("refreshToken"):
        raise NpdError("«Мой налог» не вернул токен")
    await db.set_setting(S_REFRESH, data["refreshToken"])
    inn = (data.get("profile") or {}).get("inn")
    if inn:
        await db.set_setting(S_INN, str(inn))
    _access["token"] = data.get("token")
    _access["until"] = time.time() + 600


# ── Вход по СМС ────────────────────────────────────────────────────────────────

def normalize_phone(text: str) -> str | None:
    digits = "".join(c for c in text if c.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return None


async def start_sms(phone: str) -> str:
    """Шлёт СМС с кодом, возвращает challengeToken."""
    data = await _post("/auth/challenge/sms/start",
                       {"phone": phone, "requireTpToBeActive": True}, version="v2")
    return data["challengeToken"]


async def verify_sms(phone: str, challenge: str, code: str) -> str:
    data = await _post("/auth/challenge/sms/verify",
                       {"phone": phone, "code": code.strip(), "challengeToken": challenge,
                        "deviceInfo": await _device_info()})
    await _save_login(data)
    return await db.get_setting(S_INN) or ""


async def disconnect():
    await db.set_setting(S_REFRESH, "")
    _access["token"] = None


async def connected() -> bool:
    return bool(await db.get_setting(S_REFRESH))


async def _token() -> str:
    if _access["token"] and time.time() < _access["until"]:
        return _access["token"]
    refresh = await db.get_setting(S_REFRESH)
    if not refresh:
        raise NpdNotConnected("«Мой налог» не подключён — /nalog в malimadmins")
    try:
        data = await _post("/auth/token", {"deviceInfo": await _device_info(),
                                           "refreshToken": refresh})
    except NpdError as e:
        if str(e).startswith(("401", "403")):
            raise NpdNotConnected(f"Вход в «Мой налог» слетел ({e}) — подключи заново: /nalog")
        raise
    if data.get("refreshToken"):
        await db.set_setting(S_REFRESH, data["refreshToken"])
    _access["token"] = data["token"]
    # Токен живёт около часа; обновляем с запасом
    _access["until"] = time.time() + 30 * 60
    return _access["token"]


def _iso(moment: datetime) -> str:
    return moment.astimezone(MSK).isoformat(timespec="seconds")


async def create_receipt(name: str, amount: float, when: datetime | None = None) -> str:
    """Регистрирует доход от физлица. Возвращает uuid чека."""
    now = datetime.now(MSK)
    data = await _post("/income", {
        "operationTime": _iso(when or now),
        "requestTime": _iso(now),
        "services": [{"name": name[:256], "amount": round(float(amount), 2), "quantity": 1}],
        "totalAmount": f"{float(amount):.2f}",
        "client": {"contactPhone": None, "displayName": None,
                   "incomeType": "FROM_INDIVIDUAL", "inn": None},
        "paymentType": "CASH",
        "ignoreMaxTotalIncomeRestriction": False,
    }, token=await _token())
    uuid = data.get("approvedReceiptUuid")
    if not uuid:
        raise NpdError(f"«Мой налог» не вернул номер чека: {data}")
    return uuid


async def cancel_receipt(uuid: str, comment: str = CANCEL_REFUND):
    now = datetime.now(MSK)
    await _post("/cancel", {"operationTime": _iso(now), "requestTime": _iso(now),
                            "comment": comment, "receiptUuid": uuid, "partnerCode": None},
                token=await _token())


async def list_incomes(since: datetime, until: datetime) -> list[dict]:
    """Все чеки за период (и наши, и от партнёров вроде Продамуса)."""
    token = await _token()
    headers = dict(HEADERS, Authorization=f"Bearer {token}")
    result, offset = [], 0
    try:
        async with _session() as s:
            while True:
                params = {"from": _iso(since), "to": _iso(until), "offset": offset,
                          "sortBy": "operation_time:asc", "limit": 50}
                async with s.get(f"{API}/v1/incomes", params=params, headers=headers) as r:
                    data = await r.json(content_type=None)
                    if r.status >= 400:
                        raise NpdError(f"{r.status}: {(data or {}).get('message') or data}")
                result += data.get("content") or []
                if not data.get("hasMore"):
                    return result
                offset += 50
    except (aiohttp.ClientConnectionError, TimeoutError, OSError, _ProxyError) as e:
        raise NpdError(f"нет связи с «Мой налог» ({type(e).__name__})") from e


async def print_url(uuid: str) -> str:
    inn = await db.get_setting(S_INN) or ""
    return f"{API}/v1/receipt/{inn}/{uuid}/print"
