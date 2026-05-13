import os
import logging
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

YANDEX_DELIVERY_TOKEN: str = os.getenv("YANDEX_DELIVERY_TOKEN", "")
YANDEX_WAREHOUSE_ID: str = os.getenv("YANDEX_WAREHOUSE_ID", "")
WAITLIST_BOT_TOKEN: str = os.getenv("WAITLIST_BOT_TOKEN", "")
WAITLIST_CHAT_ID: int = int(os.getenv("WAITLIST_CHAT_ID", "0") or "0") or (ADMIN_IDS[0] if ADMIN_IDS else 0)

# Prodamus
PRODAMUS_SECRET: str = os.getenv("PRODAMUS_SECRET", "")
PRODAMUS_SHOP_URL: str = os.getenv("PRODAMUS_SHOP_URL", "")
PRODAMUS_WEBHOOK_PORT: int = int(os.getenv("PRODAMUS_WEBHOOK_PORT", "8080"))

PRODAMUS_FEE_PERCENT: float = float(os.getenv("PRODAMUS_FEE_PERCENT", "3.5"))
DAILY_REPORT_HOUR_MSK: int = int(os.getenv("DAILY_REPORT_HOUR_MSK", "9"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")
if not ADMIN_IDS:
    raise ValueError("ADMIN_IDS не задан в .env")
if not PRODAMUS_SECRET:
    logging.warning("PRODAMUS_SECRET не задан в .env — подпись вебхуков не будет проверяться")
if not PRODAMUS_SHOP_URL:
    logging.warning("PRODAMUS_SHOP_URL не задан в .env — платежи будут недоступны")
