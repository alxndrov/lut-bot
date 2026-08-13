"""Выгрузка заказов в Google Таблицу.

Работает через сервисный аккаунт Google: файл с ключом лежит на сервере,
таблица расшарена на почту этого аккаунта. Ничего публично не открывается.

Таблица перезаписывается целиком — так выгрузка идемпотентна: сколько раз
ни нажми кнопку, результат один и тот же, дублей не появится.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))

HEADERS = [
    "Номер заказа", "Взял", "Статус", "Telegram", "Отправлен",
]


class SheetsError(Exception):
    """Понятная человеку ошибка выгрузки."""


def _msk(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(ts)


def _telegram(o: dict) -> str:
    return f"@{o['username']}" if o.get("username") else f"id:{o.get('user_id')}"


def _status(o: dict) -> str:
    if o.get("shipped_at"):
        return "Отправлен"
    if o.get("printed_at"):
        return "Распечатан"
    if o.get("assignee_name"):
        return "В работе"
    return "Новый"


def build_rows(orders: list[dict]) -> list[list]:
    rows = []
    for o in orders:
        rows.append([
            o.get("order_code") or o.get("prodamus_order_id") or f"#{o.get('id')}",
            o.get("assignee_name") or "",
            _status(o),
            _telegram(o),
            _msk(o.get("shipped_at")),
        ])
    return rows


# Индекс колонки «Статус» (0-based) → буква для формул условного форматирования
_STATUS_COL = HEADERS.index("Статус")
_STATUS_LETTER = chr(ord("A") + _STATUS_COL)

GREEN = {"red": 0.85, "green": 0.94, "blue": 0.83}   # отправлен
YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.80}   # ещё не отправлен


def _apply_formatting(book, sheet):
    """Красит строки по статусу: отправленные — зелёные, остальные — жёлтые.

    Делается условным форматированием, а не заливкой ячеек: правило живёт
    в самой таблице, поэтому цвет верный даже если статус поправить руками.
    Правила пересоздаются, чтобы не копились дубли при каждой выгрузке.
    """
    sid = sheet.id
    requests = []

    # Сносим прежние правила этого листа (удаляем с конца, индексы сдвигаются)
    try:
        meta = book.fetch_sheet_metadata()
        for s in meta.get("sheets", []):
            if s.get("properties", {}).get("sheetId") != sid:
                continue
            existing = s.get("conditionalFormats", []) or []
            for i in range(len(existing) - 1, -1, -1):
                requests.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": i}})
    except Exception as e:
        logger.warning(f"gsheets: не удалось прочитать прежние правила: {e}")

    data_range = {
        "sheetId": sid,
        "startRowIndex": 1,          # без шапки
        "startColumnIndex": 0,
        "endColumnIndex": len(HEADERS),
    }

    def rule(formula: str, color: dict) -> dict:
        return {"addConditionalFormatRule": {
            "rule": {
                "ranges": [data_range],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                  "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": color},
                },
            },
            "index": 0,
        }}

    # Выигрывает первое подходящее правило, а каждое добавление с index=0
    # встаёт в начало списка. Поэтому добавляем в обратном порядке: жёлтое
    # первым, зелёное вторым — в итоге зелёное окажется наверху и победит
    # для отправленных заказов.
    requests.append(rule(f'=$A2<>""', YELLOW))
    requests.append(rule(f'=${_STATUS_LETTER}2="Отправлен"', GREEN))

    # Шапку — жирной, чтобы не терялась на цветном фоне
    requests.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
        "fields": "userEnteredFormat.textFormat.bold",
    }})

    book.batch_update({"requests": requests})


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


def sync_orders(orders: list[dict]) -> str:
    """Перезаписывает лист заказами. Возвращает ссылку на таблицу.

    Блокирующая функция (gspread синхронный) — вызывать через to_thread.
    """
    book = _open_book()

    try:
        title = config.GOOGLE_SHEET_TAB
        try:
            sheet = book.worksheet(title)
        except Exception:
            sheet = book.add_worksheet(title=title, rows=200, cols=len(HEADERS))

        rows = build_rows(orders)
        sheet.clear()
        sheet.update(values=[HEADERS] + rows, range_name="A1")
        sheet.freeze(rows=1)
        _apply_formatting(book, sheet)
        logger.info(f"gsheets: выгружено заказов {len(rows)}")
    except Exception as e:
        raise SheetsError(f"не удалось записать данные: {type(e).__name__}: {e}")

    return f"https://docs.google.com/spreadsheets/d/{config.GOOGLE_SHEET_ID}"


# --- Автообновление ---

# Отложенная выгрузка: несколько событий подряд (оплата, трек СДЭК, отправка)
# схлопываются в одну запись. Иначе на серии заказов упрёмся в лимиты Google.
_pending_task: asyncio.Task | None = None
SYNC_DELAY = 15.0


def request_sync(delay: float = SYNC_DELAY):
    """Просит обновить таблицу через delay секунд. Вызывать неблокирующе.

    Если выгрузка уже запланирована — ничего не делаем: когда она сработает,
    то возьмёт из базы актуальные данные, включая это изменение.
    """
    global _pending_task
    if not config.GSHEETS_ENABLED:
        return
    if _pending_task and not _pending_task.done():
        return
    try:
        _pending_task = asyncio.create_task(_delayed_sync(delay))
    except RuntimeError:
        # нет запущенного цикла событий — значит, вызвали вне бота
        logger.warning("gsheets: request_sync вне event loop, пропускаю")


async def _delayed_sync(delay: float):
    import database as db
    try:
        await asyncio.sleep(delay)
        orders = await db.get_orders_export()
        if not orders:
            return
        await asyncio.to_thread(sync_orders, orders)
    except SheetsError as e:
        logger.error(f"gsheets: автовыгрузка не удалась — {e}")
    except Exception:
        logger.exception("gsheets: неожиданная ошибка автовыгрузки")


# --- Финансовая книга: по строке на заказ, дописывается, не перезаписывается ---

FINANCE_HEADERS = [
    "Дата заказа", "Номер заказа", "Оплата", "Комиссия Prodamus",
    "Налог", "Отложено на СДЭК", "К выплате",
]


def append_payment_row(order_code: str, date_msk: str, amount: float, delivery_cost: float) -> None:
    """Дописывает одну строку в финансовый лист (GOOGLE_SHEET_FINANCE_TAB) —
    в отличие от sync_orders, ничего не перезаписывает: остальные строки
    и вписанные туда формулы/пометки не трогаются.

    Комиссия/Налог/К выплате — формулами, которые бот сам копирует в новую
    строку (по тем же ставкам, что в остальных расчётах бота), чтобы не
    зависеть от того, подхватит ли Google Sheets автозаполнение сам.

    Блокирующая функция (gspread синхронный) — вызывать через to_thread.
    """
    book = _open_book()

    try:
        title = config.GOOGLE_SHEET_FINANCE_TAB
        try:
            sheet = book.worksheet(title)
        except Exception:
            sheet = book.add_worksheet(title=title, rows=200, cols=len(FINANCE_HEADERS))
            sheet.update(values=[FINANCE_HEADERS], range_name="A1")
            sheet.freeze(rows=1)

        row = len(sheet.get_all_values()) + 1
        fee_formula = f"=C{row}*{config.PRODAMUS_FEE_PERCENT:g}%"
        npd_formula = f"=C{row}*{config.NPD_PERCENT:g}%"
        payout_formula = f"=C{row}-D{row}-E{row}-F{row}"
        sheet.append_row(
            [date_msk, order_code, amount, fee_formula, npd_formula, delivery_cost, payout_formula],
            value_input_option="USER_ENTERED",
        )
        logger.info(f"gsheets: записан заказ {order_code} в финансовый лист (строка {row})")
    except Exception as e:
        raise SheetsError(f"не удалось записать строку: {type(e).__name__}: {e}")


def request_finance_append(order_code: str, date_msk: str, amount: float, delivery_cost: float):
    """Просит дописать заказ в финансовый лист. Вызывать неблокирующе.

    В отличие от request_sync — без задержки: одна строка добавляется
    одним лёгким запросом, откладывать и схлопывать с другими нечего.
    """
    if not config.GSHEETS_ENABLED:
        return
    try:
        asyncio.create_task(_finance_append(order_code, date_msk, amount, delivery_cost))
    except RuntimeError:
        logger.warning("gsheets: request_finance_append вне event loop, пропускаю")


async def _finance_append(order_code: str, date_msk: str, amount: float, delivery_cost: float):
    try:
        await asyncio.to_thread(append_payment_row, order_code, date_msk, amount, delivery_cost)
    except SheetsError as e:
        logger.error(f"gsheets: запись в финансовый лист не удалась ({order_code}) — {e}")
    except Exception:
        logger.exception(f"gsheets: неожиданная ошибка записи в финансовый лист ({order_code})")
