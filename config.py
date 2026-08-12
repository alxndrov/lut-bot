import os
import logging
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

# СДЭК. Без account/password модуль не подключается, и адрес доставки
# собирается по-старому — свободным текстом.
CDEK_ACCOUNT: str = os.getenv("CDEK_ACCOUNT", "")
CDEK_SECURE_PASSWORD: str = os.getenv("CDEK_SECURE_PASSWORD", "")
CDEK_FROM_CITY: str = os.getenv("CDEK_FROM_CITY", "Москва")
CDEK_TEST_MODE: bool = os.getenv("CDEK_TEST_MODE", "false").lower() in ("1", "true", "yes")
# Доставка только до пункта выдачи. 136 — склад-склад, тариф «интернет-магазин».
# При обычном договоре (не ИМ) заменить на 234.
CDEK_TARIFF_PVZ: int = int(os.getenv("CDEK_TARIFF_PVZ", "136"))
# Включён ли автоматический расчёт доставки. Когда True — текстовый блок
# доставки в опросе не используется, адрес собирается по шагам через СДЭК.
CDEK_ENABLED: bool = bool(CDEK_ACCOUNT and CDEK_SECURE_PASSWORD)

# Код ПВЗ СДЭК, куда вы сдаёте посылки (например, MSK123). Без него заказы
# в СДЭК не создаются — бот только считает доставку, а заказ заводится вручную.
CDEK_SHIPMENT_POINT: str = os.getenv("CDEK_SHIPMENT_POINT", "")
# Габариты и вес посылки по умолчанию (вес в граммах, размеры в см)
CDEK_PACKAGE_WEIGHT: int = int(os.getenv("CDEK_PACKAGE_WEIGHT", "500"))
CDEK_PACKAGE_LENGTH: int = int(os.getenv("CDEK_PACKAGE_LENGTH", "20"))
CDEK_PACKAGE_WIDTH: int = int(os.getenv("CDEK_PACKAGE_WIDTH", "15"))
CDEK_PACKAGE_HEIGHT: int = int(os.getenv("CDEK_PACKAGE_HEIGHT", "5"))
# Создавать ли заказ в СДЭК автоматически после оплаты
CDEK_AUTO_ORDER: bool = CDEK_ENABLED and bool(CDEK_SHIPMENT_POINT)
# Формат наклейки ШК-места: A4, A5, A6 или A7. A6 (105×148 мм) — под рулон
# термоэтикеток 100×150 мм. Если ваш принтер под 75×100 мм, ставьте A7.
CDEK_BARCODE_FORMAT: str = os.getenv("CDEK_BARCODE_FORMAT", "A6").upper()
# НДС в счёте СДЭК. Калькулятор тарифов отдаёт цену БЕЗ него, а в акт он
# попадает — без этой поправки каждая посылка обходится нам на 7% дороже,
# чем мы взяли с клиента.
CDEK_VAT_PERCENT: float = float(os.getenv("CDEK_VAT_PERCENT", "7"))
# «Дополнительный сбор за объявленную стоимость» — страховка, процент от
# цены товара, которую мы объявляем в накладной. В калькулятор не входит.
CDEK_INSURANCE_PERCENT: float = float(os.getenv("CDEK_INSURANCE_PERCENT", "0.75"))
# НПД самозанятого. Платится со ВСЕЙ принятой суммы, включая доставку,
# поэтому цену доставки для клиента поднимаем и на него тоже.
NPD_PERCENT: float = float(os.getenv("NPD_PERCENT", "4"))

WAITLIST_BOT_TOKEN: str = os.getenv("WAITLIST_BOT_TOKEN", "")
WAITLIST_CHAT_ID: int = int(os.getenv("WAITLIST_CHAT_ID", "0") or "0") or (ADMIN_IDS[0] if ADMIN_IDS else 0)

# Prodamus
PRODAMUS_SECRET: str = os.getenv("PRODAMUS_SECRET", "")
PRODAMUS_SHOP_URL: str = os.getenv("PRODAMUS_SHOP_URL", "")
PRODAMUS_WEBHOOK_PORT: int = int(os.getenv("PRODAMUS_WEBHOOK_PORT", "8080"))
# Куда Prodamus должен слать webhook об оплате. Передаётся в ссылке (urlNotification),
# поэтому не зависит от настроек кабинета Prodamus и переживает переезд сервера.
PRODAMUS_WEBHOOK_URL: str = os.getenv("PRODAMUS_WEBHOOK_URL", "")

# Отдельный магазин Prodamus для ФИЗИЧЕСКИХ товаров (order_type="p").
# Если не заданы — используются реквизиты основного (цифрового) магазина.
# У физического магазина свой секрет и своя ссылка уведомления:
#   PRODAMUS_WEBHOOK_URL_PHYSICAL должен указывать на /prodamus/webhook/physical
PRODAMUS_SHOP_URL_PHYSICAL: str = os.getenv("PRODAMUS_SHOP_URL_PHYSICAL", "") or PRODAMUS_SHOP_URL
PRODAMUS_SECRET_PHYSICAL: str = os.getenv("PRODAMUS_SECRET_PHYSICAL", "") or PRODAMUS_SECRET
PRODAMUS_WEBHOOK_URL_PHYSICAL: str = os.getenv("PRODAMUS_WEBHOOK_URL_PHYSICAL", "") or PRODAMUS_WEBHOOK_URL

PRODAMUS_FEE_PERCENT: float = float(os.getenv("PRODAMUS_FEE_PERCENT", "3.5"))

# Условия с партнёром. Физтовары (микрофоны): Даня получает процент от
# стоимости товара плюс фиксированную сумму за каждую напечатанную им ручку,
# Миша — весь остаток прибыли. Цифровые товары делятся по-старому.
PARTNER_ID: int = int(os.getenv("PARTNER_ID", "140657458"))          # @alxndrov
PARTNER_NAME: str = os.getenv("PARTNER_NAME", "Даня")
OWNER_NAME: str = os.getenv("OWNER_NAME", "Миша")
PARTNER_GOODS_PERCENT: float = float(os.getenv("PARTNER_GOODS_PERCENT", "10"))
PARTNER_PRINT_FEE: int = int(os.getenv("PARTNER_PRINT_FEE", "200"))
PARTNER_DIGITAL_PERCENT: float = float(os.getenv("PARTNER_DIGITAL_PERCENT", "20"))

# Согласие на обработку персональных данных и оферта. Пока ссылки не заданы,
# согласие не спрашивается — бот работает как раньше.
PRIVACY_POLICY_URL: str = os.getenv("PRIVACY_POLICY_URL", "")
OFFER_URL: str = os.getenv("OFFER_URL", "")
POLICY_REQUIRED: bool = bool(PRIVACY_POLICY_URL)

# Google Таблица с заказами. Пока не заданы — кнопка выгрузки не показывается.
GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS_FILE: str = os.getenv("GOOGLE_CREDENTIALS_FILE", "google-service-account.json")
GOOGLE_SHEET_TAB: str = os.getenv("GOOGLE_SHEET_TAB", "Заказы")
GSHEETS_ENABLED: bool = bool(GOOGLE_SHEET_ID)
DAILY_REPORT_HOUR_MSK: int = int(os.getenv("DAILY_REPORT_HOUR_MSK", "9"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")
if not ADMIN_IDS:
    raise ValueError("ADMIN_IDS не задан в .env")
if not PRODAMUS_SECRET:
    logging.warning("PRODAMUS_SECRET не задан в .env — подпись вебхуков не будет проверяться")
if not PRODAMUS_SHOP_URL:
    logging.warning("PRODAMUS_SHOP_URL не задан в .env — платежи будут недоступны")
