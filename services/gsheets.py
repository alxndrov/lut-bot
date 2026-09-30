"""Доступ к Google Таблице сервисным аккаунтом и запросы на пересверку
финансового листа.

Файл с ключом лежит на сервере, таблица расшарена на почту этого аккаунта.
Ничего публично не открывается. Отдельного листа с заказами больше нет —
в таблице только финансы (services.finance_sheet).
"""
import logging

import config

logger = logging.getLogger(__name__)


class SheetsError(Exception):
    """Понятная человеку ошибка выгрузки."""


def _open_book():
    """Открывает таблицу сервисным аккаунтом. Блокирующая функция (gspread
    синхронный) — вызывать через to_thread. Общая для всех выгрузок."""
    if not config.GOOGLE_SHEET_ID:
        raise SheetsError("не задан GOOGLE_SHEET_ID")
    if not config.GOOGLE_CREDENTIALS_FILE:
        raise SheetsError("не задан GOOGLE_CREDENTIALS_FILE")

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        raise SheetsError(f"не установлены библиотеки: {e}")

    try:
        creds = Credentials.from_service_account_file(
            config.GOOGLE_CREDENTIALS_FILE,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        client = gspread.authorize(creds)
        client.set_timeout((10, 60))
        return client.open_by_key(config.GOOGLE_SHEET_ID)
    except FileNotFoundError:
        raise SheetsError(f"файл ключа не найден: {config.GOOGLE_CREDENTIALS_FILE}")
    except Exception as e:
        name = type(e).__name__
        if "SpreadsheetNotFound" in name or "PermissionError" in name or "403" in str(e):
            raise SheetsError(
                "нет доступа к таблице. Откройте её и дайте права «Редактор» "
                "сервисному аккаунту (почта из файла ключа)."
            )
        raise SheetsError(f"{name}: {e}")


def request_sync():
    """Заказ изменился — пересверить финансовый лист."""
    request_finance_sync()


# Financial events request reconciliation from the durable SQLite source.
# Keep these signatures for existing handlers; never race to append a row.
def request_finance_sync():
    from services.finance_sheet import request_finance_sync as request
    request()


def request_finance_append(order_code: str, date_msk: str, amount: float, delivery_cost: float,
                           comment: str = "", goods_type: str | None = None,
                           printer: str | None = None, printer_positions: int | None = None):
    request_finance_sync()


def request_expense_append(code: str, date_msk: str, amount: float, comment: str,
                           kind: str = "Расход"):
    request_finance_sync()


def request_finance_printer_update(order_code: str, printer: str, positions: int):
    request_finance_sync()
