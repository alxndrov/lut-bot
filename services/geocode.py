"""Геокодер адресов на OpenStreetMap (Nominatim).

Нужен, чтобы клиент мог вместо геолокации просто написать улицу и дом —
мы превращаем это в координаты и сортируем ПВЗ по расстоянию.
Ключ не требуется; по правилам сервиса обязателен User-Agent.
"""
import aiohttp
import logging
from typing import Optional

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "lut-bot/1.0 (Telegram shop bot; delivery point search)"

logger = logging.getLogger(__name__)


async def geocode(query: str, city: str = "") -> Optional[tuple[float, float]]:
    """Возвращает (широта, долгота) или None, если адрес не найден."""
    query = (query or "").strip()
    if not query:
        return None
    # Город подставляем сам, если клиент его не написал
    if city and city.lower() not in query.lower():
        query = f"{city}, {query}"
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                NOMINATIM_URL,
                params={
                    "q": query,
                    "format": "json",
                    "limit": 1,
                    "accept-language": "ru",
                    "countrycodes": "ru",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=12),
            )
            if resp.status != 200:
                logger.warning(f"geocode: HTTP {resp.status} для запроса {query!r}")
                return None
            data = await resp.json()
            if isinstance(data, list) and data:
                return float(data[0]["lat"]), float(data[0]["lon"])
            logger.info(f"geocode: адрес не найден: {query!r}")
    except Exception as e:
        logger.error(f"geocode error: {e}")
    return None
