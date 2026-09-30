"""Выбор договора СДЭК по сохранённой привязке заказа."""
import hashlib
import logging

import config
from services.cdek import CDEKClient

logger = logging.getLogger(__name__)


def contract_key(account: str) -> str:
    # В базе хранится отпечаток аккаунта, сами ключи остаются только в .env.
    return hashlib.sha256(account.encode()).hexdigest() if account else ""


def _client(account: str, password: str) -> CDEKClient | None:
    if not account or not password:
        return None
    return CDEKClient(account, password, config.CDEK_FROM_CITY, config.CDEK_TEST_MODE)


CURRENT_CONTRACT = contract_key(config.CDEK_ACCOUNT)
OLD_CONTRACT = contract_key(config.CDEK_ACCOUNT_OLD)
CDEK_CLIENT = _client(config.CDEK_ACCOUNT, config.CDEK_SECURE_PASSWORD)
OLD_CDEK_CLIENT = _client(config.CDEK_ACCOUNT_OLD, config.CDEK_SECURE_PASSWORD_OLD)


def client_for_order(order: dict) -> CDEKClient | None:
    key = order.get("cdek_contract")
    if not key:
        # Накладную без привязки нельзя отправлять на произвольный договор.
        if order.get("cdek_uuid"):
            logger.error("CDEK: у накладной заказа %s нет договора", order.get("id"))
            return None
        return CDEK_CLIENT
    if key == CURRENT_CONTRACT:
        return CDEK_CLIENT
    if key == OLD_CONTRACT:
        return OLD_CDEK_CLIENT
    logger.error("CDEK: нет ключей договора для заказа %s", order.get("id"))
    return None
