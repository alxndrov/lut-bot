import time
import aiohttp
import logging
from typing import Optional

# Боевой контур и песочница СДЭК. Песочница принимает только тестовые
# аккаунт/пароль из документации и считает тарифы по фиктивным данным.
CDEK_PROD_URL = "https://api.cdek.ru/v2"
CDEK_TEST_URL = "https://api.edu.cdek.ru/v2"

logger = logging.getLogger(__name__)


def _split_items(items: list[dict], places: int) -> list[list[dict]]:
    """Раскладывает товары по коробкам: по одной штуке в место.

    Если штук меньше, чем мест, лишние места не создаём — накладная
    должна совпадать с тем, что реально сдаём в пункт приёма.
    """
    units = []
    for it in items:
        for _ in range(max(1, int(it.get("amount") or 1))):
            units.append({**it, "amount": 1})
    if not units:
        return [[]]
    places = max(1, min(places, len(units)))
    if places == 1:
        return [items]                     # одно место — товары как есть
    chunks: list[list[dict]] = [[] for _ in range(places)]
    for i, unit in enumerate(units):
        chunks[i % places].append(unit)
    return chunks


class CDEKClient:
    def __init__(self, client_id: str, client_secret: str,
                 from_city: str = "", test_mode: bool = False):
        self.client_id = client_id
        self.client_secret = client_secret
        self.from_city = from_city
        self.base_url = CDEK_TEST_URL if test_mode else CDEK_PROD_URL
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._from_city_code: Optional[int] = None

    async def get_token(self) -> Optional[str]:
        """Возвращает токен, переиспользуя его до истечения срока."""
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"{self.base_url}/oauth/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    timeout=aiohttp.ClientTimeout(total=10)
                )
                data = await resp.json()
                token = data.get("access_token")
                if not token:
                    logger.error(f"CDEK get_token: нет access_token в ответе: {data}")
                    return None
                self._token = token
                # Запас в 60 сек, чтобы не попасть на истечение в момент запроса
                self._token_expires_at = time.monotonic() + int(data.get("expires_in", 3600)) - 60
                return self._token
        except Exception as e:
            logger.error(f"CDEK get_token error: {e}")
            return None

    async def get_city_code(self, city_name: str) -> Optional[int]:
        token = await self.get_token()
        if not token:
            return None
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    f"{self.base_url}/location/cities",
                    params={"city": city_name, "size": 1, "country_codes": "RU"},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10)
                )
                data = await resp.json()
                if isinstance(data, list) and data:
                    return data[0].get("code")
        except Exception as e:
            logger.error(f"CDEK get_city_code error: {e}")
        return None

    async def get_from_city_code(self) -> Optional[int]:
        """Код города отправления из конфига, считается один раз."""
        if self._from_city_code is not None:
            return self._from_city_code
        if not self.from_city:
            return None
        self._from_city_code = await self.get_city_code(self.from_city)
        return self._from_city_code

    async def calculate_tariff(
        self,
        to_city_code: int,
        tariff_code: int,
        from_city_code: Optional[int] = None,
        weight: int = 500,
        length: int = 20,
        width: int = 15,
        height: int = 5,
        places: int = 1,
    ) -> Optional[dict]:
        """Считает доставку. Возвращает {'cost': int, 'days_min': int, 'days_max': int}.

        weight — вес ОДНОГО места. places — сколько таких коробок едет.
        На цену число мест не влияет (считается по суммарному весу), но
        расчёт должен совпадать с тем, что уйдёт в накладную.
        """
        token = await self.get_token()
        if not token:
            return None
        if from_city_code is None:
            from_city_code = await self.get_from_city_code()
        if from_city_code is None:
            logger.error("CDEK calculate_tariff: не определён город отправления")
            return None
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"{self.base_url}/calculator/tariff",
                    json={
                        "tariff_code": tariff_code,
                        "from_location": {"code": from_city_code},
                        "to_location": {"code": to_city_code},
                        "packages": [{"weight": weight, "length": length,
                                      "width": width, "height": height}
                                     for _ in range(max(1, places))],
                    },
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10)
                )
                data = await resp.json()
                if not isinstance(data, dict) or data.get("delivery_sum") is None:
                    logger.error(f"CDEK calculate_tariff: неожиданный ответ: {data}")
                    return None
                return {
                    "cost": int(round(float(data["delivery_sum"]))),
                    "days_min": data.get("period_min"),
                    "days_max": data.get("period_max"),
                }
        except Exception as e:
            logger.error(f"CDEK calculate_tariff error: {e}")
        return None

    async def create_order(
        self,
        number: str,
        shipment_point: str,
        delivery_point: str,
        recipient_name: str,
        recipient_phone: str,
        items: list[dict],
        tariff_code: int,
        weight: int = 500,
        length: int = 20,
        width: int = 15,
        height: int = 5,
        places: int = 1,
    ) -> Optional[str]:
        """Заводит заказ в СДЭК. Возвращает uuid заявки или None.

        Заказ создаётся асинхронно: СДЭК отвечает 202 и uuid, а результат
        (успех и трек-номер) забирается отдельно через get_order_info.
        items: [{'name','ware_key','cost','amount','weight'}]
        weight/length/width/height — габариты ОДНОГО места, places — сколько
        коробок реально сдаём: на каждую СДЭК печатает свою наклейку.
        """
        token = await self.get_token()
        if not token:
            return None
        body = {
            "type": 1,                      # заказ интернет-магазина
            "number": number,
            "tariff_code": tariff_code,
            "shipment_point": shipment_point,
            "delivery_point": delivery_point,
            "recipient": {
                "name": recipient_name,
                "phones": [{"number": recipient_phone}],
            },
            "packages": [{
                "number": str(i + 1),
                "weight": weight,
                "length": length,
                "width": width,
                "height": height,
                "items": [{
                    "name": it["name"][:255],
                    "ware_key": str(it["ware_key"])[:20],
                    # payment — наложенный платёж; заказ уже оплачен, значит 0
                    "payment": {"value": 0},
                    "cost": it["cost"],
                    "amount": it["amount"],
                    "weight": it.get("weight", weight),
                } for it in chunk],
            } for i, chunk in enumerate(_split_items(items, max(1, places)))],
        }
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"{self.base_url}/orders",
                    json=body,
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=30)
                )
                data = await resp.json()
                uuid = (data.get("entity") or {}).get("uuid")
                if not uuid:
                    logger.error(f"CDEK create_order: нет uuid в ответе: {data}")
                    return None
                return uuid
        except Exception as e:
            logger.error(f"CDEK create_order error: {e}")
        return None

    async def get_order_info(self, uuid: str) -> Optional[dict]:
        """Статус заявки. Возвращает {'state', 'cdek_number', 'errors'}."""
        token = await self.get_token()
        if not token:
            return None
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    f"{self.base_url}/orders/{uuid}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=20)
                )
                data = await resp.json()
                entity = data.get("entity") or {}
                requests_ = data.get("requests") or []
                create_req = next(
                    (r for r in requests_ if r.get("type") == "CREATE"),
                    requests_[0] if requests_ else {},
                )
                statuses = entity.get("statuses") or []
                return {
                    "state": create_req.get("state"),
                    "cdek_number": entity.get("cdek_number"),
                    "errors": create_req.get("errors") or [],
                    # Последний статус — первый в списке СДЭК
                    "status_code": statuses[0].get("code") if statuses else None,
                    "status_name": statuses[0].get("name") if statuses else None,
                    "status_codes": [s.get("code") for s in statuses],
                }
        except Exception as e:
            logger.error(f"CDEK get_order_info error: {e}")
        return None

    async def get_pvz(self, city_code: int, limit: int = 1000) -> list:
        """Пункты выдачи в городе.

        Возвращает [{'code', 'name', 'address', 'work_time', 'lat', 'lon'}].
        Координаты нужны, чтобы отсортировать пункты по расстоянию до клиента.
        """
        token = await self.get_token()
        if not token:
            return []
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    f"{self.base_url}/deliverypoints",
                    params={
                        "city_code": city_code,
                        "type": "PVZ",
                        "country_code": "RU",
                        "size": limit,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=30)
                )
                data = await resp.json()
                if not isinstance(data, list):
                    logger.error(f"CDEK get_pvz: неожиданный ответ: {data}")
                    return []
                points = []
                for p in data[:limit]:
                    loc = p.get("location", {})
                    points.append({
                        "code": p.get("code", ""),
                        "name": p.get("name") or p.get("code", "ПВЗ"),
                        "address": loc.get("address_full") or loc.get("address", "адрес не указан"),
                        # короткий адрес и город — для поиска пункта в Яндекс.Картах
                        "address_short": loc.get("address", ""),
                        "city": loc.get("city", ""),
                        "work_time": (p.get("work_time") or "").strip(),
                        "lat": loc.get("latitude"),
                        "lon": loc.get("longitude"),
                    })
                return points
        except Exception as e:
            logger.error(f"CDEK get_pvz error: {e}")
        return []
