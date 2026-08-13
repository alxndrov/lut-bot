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


def _pct_formula(cell: str, pct: float) -> str:
    """'C6', 3.8 -> '=C6*3,8%'. Запятая, не точка: таблица на русской
    локали, с точкой дробный множитель в формуле не распознаётся числом
    (Google Sheets тогда либо ругается, либо тихо считает неверно)."""
    return f"={cell}*{pct:g}%".replace(".", ",")


def _col_letter(col: int) -> str:
    """Номер столбца (с 1) в букву(ы): 1 -> 'A', 27 -> 'AA'."""
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _header_columns(sheet, headers: list[str]) -> dict[str, int]:
    """Название столбца из шапки листа -> номер столбца (с 1).

    Запись идёт по названию столбца, а не по фиксированной позиции A-G:
    если в листе руками вставили новый столбец (даже в середине — до
    существующих), шапка сдвинется, но бот всё равно попадёт в нужный
    по названию столбец, а не затрёт чужие данные/формулы и не собьёт
    условное форматирование, привязанное к конкретным буквам.
    """
    header_row = sheet.row_values(1)
    cols = {name: i + 1 for i, name in enumerate(header_row) if name}
    missing = [h for h in headers if h not in cols]
    if missing:
        raise SheetsError(
            f"в листе «{config.GOOGLE_SHEET_FINANCE_TAB}» не найден столбец «{missing[0]}» — "
            "шапку переименовали? верните исходный текст заголовка."
        )
    return cols


def _finance_row_cells(header_cols: dict[str, int], row_num: int, date_msk: str,
                        order_code: str, amount: float, delivery_cost: float) -> list[dict]:
    """Ячейки одной строки финансового листа — каждая привязана к столбцу
    по названию из header_cols (см. _header_columns), не по букве A-G."""
    def letter(name: str) -> str:
        return _col_letter(header_cols[name])

    pay_cell = f"{letter('Оплата')}{row_num}"
    fee_cell = f"{letter('Комиссия Prodamus')}{row_num}"
    npd_cell = f"{letter('Налог')}{row_num}"
    cdek_cell = f"{letter('Отложено на СДЭК')}{row_num}"

    values = {
        "Дата заказа": date_msk,
        "Номер заказа": order_code,
        "Оплата": amount,
        "Комиссия Prodamus": _pct_formula(pay_cell, config.PRODAMUS_FEE_PERCENT),
        "Налог": _pct_formula(pay_cell, config.NPD_PERCENT),
        "Отложено на СДЭК": delivery_cost,
        "К выплате": f"={pay_cell}-{fee_cell}-{npd_cell}-{cdek_cell}",
    }
    return [
        {"range": f"{letter(name)}{row_num}", "values": [[value]]}
        for name, value in values.items()
    ]


def _ensure_rows(sheet, need_rows: int):
    """Расширяет лист, если целевая строка выходит за текущий размер грида.

    append_row/append_rows растили лист сами по мере добавления строк;
    запись по фиксированному диапазону (batch_update) — нет, поэтому
    растим заранее, с запасом, чтобы не дёргать это на каждой строке."""
    if need_rows > sheet.row_count:
        sheet.add_rows(need_rows - sheet.row_count + 50)


def append_payment_row(order_code: str, date_msk: str, amount: float, delivery_cost: float) -> None:
    """Дописывает одну строку в финансовый лист (GOOGLE_SHEET_FINANCE_TAB) —
    в отличие от sync_orders, ничего не перезаписывает: остальные строки
    и вписанные туда формулы/пометки не трогаются.

    Комиссия/Налог/К выплате — формулами, которые бот сам копирует в новую
    строку (по тем же ставкам, что в остальных расчётах бота), чтобы не
    зависеть от того, подхватит ли Google Sheets автозаполнение сам.

    Каждое значение пишется в столбец по названию из шапки (см.
    _header_columns), а не в фиксированную букву — если в листе вручную
    добавили столбец, запись всё равно попадёт куда нужно.

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

        header_cols = _header_columns(sheet, FINANCE_HEADERS)
        row = len(sheet.get_all_values()) + 1
        _ensure_rows(sheet, row)
        cells = _finance_row_cells(header_cols, row, date_msk, order_code, amount, delivery_cost)
        sheet.batch_update(cells, value_input_option="USER_ENTERED")
        logger.info(f"gsheets: записан заказ {order_code} в финансовый лист (строка {row})")
    except SheetsError:
        raise
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


def backfill_finance_rows(rows: list[dict]) -> tuple[int, int]:
    """Дописывает в финансовый лист заказы из rows (см.
    db.get_orders_for_finance_export), которых там ещё нет.

    Идемпотентно: сверяет по номеру заказа (столбец «Номер заказа», по
    названию из шапки — см. _header_columns) с уже вписанными строками —
    повторный запуск (или заказы, попавшие туда «живой» записью до
    бэкафилла) не задвоит их. Возвращает (добавлено, уже было).

    Все ячейки всех новых строк пишутся ОДНИМ батч-запросом, а не по
    одной — иначе на полусотне заказов легко упереться в лимит запросов
    Google API. Каждая ячейка привязана к столбцу по названию из шапки
    (см. _finance_row_cells), не по фиксированной букве A-G.
    Блокирующая функция (gspread синхронный) — вызывать через to_thread.
    """
    book = _open_book()
    title = config.GOOGLE_SHEET_FINANCE_TAB
    try:
        sheet = book.worksheet(title)
    except Exception:
        sheet = book.add_worksheet(title=title, rows=max(200, len(rows) + 10),
                                   cols=len(FINANCE_HEADERS))
        sheet.update(values=[FINANCE_HEADERS], range_name="A1")
        sheet.freeze(rows=1)

    header_cols = _header_columns(sheet, FINANCE_HEADERS)
    order_col = header_cols["Номер заказа"]
    existing_codes = set(sheet.col_values(order_col)[1:])  # без шапки
    row_num = len(sheet.get_all_values()) + 1

    from services.payout import delivery_out

    cells = []
    added = 0
    skipped = 0
    for r in rows:
        code = r["order_code"]
        if not code or code in existing_codes:
            skipped += 1
            continue
        # Заказы до появления точного счёта СДЭК в purchases.delivery_cost —
        # оцениваем той же формулой, что и «Взаиморасчёт»/«Касса»
        # (delivery_legacy > 0 только когда delivery_cost так и не сохранился)
        delivery_cost = delivery_out(float(r["delivery_cost"]), float(r["delivery_legacy"]),
                                     config.PRODAMUS_FEE_PERCENT)
        cells += _finance_row_cells(header_cols, row_num, _msk(r["created_at"]), code,
                                    float(r["amount"]), delivery_cost)
        row_num += 1
        added += 1

    if cells:
        _ensure_rows(sheet, row_num - 1)
        try:
            sheet.batch_update(cells, value_input_option="USER_ENTERED")
        except Exception as e:
            raise SheetsError(f"не удалось записать строки: {type(e).__name__}: {e}")

    return added, skipped


def request_finance_backfill(rows: list[dict]) -> "asyncio.Task":
    """Как backfill_finance_rows, но асинхронно (для вызова из хендлера
    команды бота — не блокируя обработку остальных апдейтов)."""
    return asyncio.create_task(asyncio.to_thread(backfill_finance_rows, rows))
