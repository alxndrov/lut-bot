import aiohttp
import logging
from typing import Optional

CDEK_BASE_URL = "https://api.edu.cdek.ru/v2"
logger = logging.getLogger(__name__)


class CDEKClient:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None

    async def get_token(self) -> Optional[str]:
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"{CDEK_BASE_URL}/oauth/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    timeout=aiohttp.ClientTimeout(total=10)
                )
                data = await resp.json()
                self._token = data.get("access_token")
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
                    f"{CDEK_BASE_URL}/location/cities",
                    params={"city": city_name, "size": 1},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10)
                )
                data = await resp.json()
                if data and isinstance(data, list):
                    return data[0].get("code")
        except Exception as e:
            logger.error(f"CDEK get_city_code error: {e}")
        return None

    async def calculate_tariff(
        self,
        from_city_code: int,
        to_city_code: int,
        tariff_code: int = 368,
        weight: int = 500,
    ) -> Optional[dict]:
        token = await self.get_token()
        if not token:
            return None
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"{CDEK_BASE_URL}/calculator/tariff",
                    json={
                        "tariff_code": tariff_code,
                        "from_location": {"code": from_city_code},
                        "to_location": {"code": to_city_code},
                        "packages": [{"weight": weight, "length": 20, "width": 15, "height": 5}],
                    },
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10)
                )
                return await resp.json()
        except Exception as e:
            logger.error(f"CDEK calculate_tariff error: {e}")
        return None

    async def get_pvz(self, city_code: int, limit: int = 5) -> list:
        token = await self.get_token()
        if not token:
            return []
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    f"{CDEK_BASE_URL}/deliverypoints",
                    params={"city_code": city_code, "type": "PVZ", "size": limit},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10)
                )
                data = await resp.json()
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"CDEK get_pvz error: {e}")
        return []
