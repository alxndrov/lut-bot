import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
PROVIDER_TOKEN: str = os.getenv("PROVIDER_TOKEN", "")  # от @BotFather → Payments
ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")
if not PROVIDER_TOKEN:
    raise ValueError("PROVIDER_TOKEN не задан в .env")
if not ADMIN_IDS:
    raise ValueError("ADMIN_IDS не задан в .env")
