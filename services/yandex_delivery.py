import aiohttp
import logging
from typing import Optional

YADEL_BASE_URL = "https://b2b-authproxy.taxi.yandex.net"
logger = logging.getLogger(__name__)


class YandexDeliveryClient:
    def __init__(self, token: str, warehouse_id: str):
        self.token = token
        self.warehouse_id = warehouse_id

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def get_geo_id(self, city: str) -> Optional[int]:
        """Определяет geo_id города по названию."""
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"{YADEL_BASE_URL}/api/b2b/platform/location/detect",
                    json={"location": city},
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                data = await resp.json()
                variants = data.get("variants", [])
                if variants:
                    return variants[0].get("geo_id")
        except Exception as e:
            logger.error(f"YaDel get_geo_id error: {e}")
        return None

    async def get_pickup_points(self, geo_id: int, limit: int = 5) -> list:
        """Получает список ПВЗ в городе."""
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"{YADEL_BASE_URL}/api/b2b/platform/pickup-points/list",
                    json={
                        "geo_id": geo_id,
                        "type": "pickup_point",
                        "payment_methods": ["already_paid"],
                    },
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                data = await resp.json()
                points = data.get("points", [])
                return points[:limit]
        except Exception as e:
            logger.error(f"YaDel get_pickup_points error: {e}")
        return []

    async def calculate_price(
        self,
        destination_address: str,
        tariff: str = "time_interval",
        weight_grams: int = 500,
    ) -> Optional[dict]:
        """Рассчитывает стоимость доставки.
        tariff: 'time_interval' (курьер) или 'self_pickup' (ПВЗ)
        """
        try:
            body = {
                "source": {"platform_station_id": self.warehouse_id},
                "destination": {"address": destination_address},
                "tariff": tariff,
                "total_weight": weight_grams,
                "total_assessed_price": 10000,
                "client_price": 10000,
                "payment_method": "already_paid",
                "places": [
                    {
                        "physical_dims": {
                            "weight_gross": weight_grams,
                            "dx": 20,
                            "dy": 15,
                            "dz": 5,
                        }
                    }
                ],
            }
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"{YADEL_BASE_URL}/api/b2b/platform/pricing-calculator",
                    json=body,
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                data = await resp.json()
                return data
        except Exception as e:
            logger.error(f"YaDel calculate_price error: {e}")
        return None
