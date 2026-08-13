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


# --- Финансовая книга: кэшфлоу (приходы + расходы), по строке на операцию,
# лист дописывается, не перезаписывается ---

FINANCE_HEADERS = [
    "Дата заказа", "Номер операции", "Сумма", "Комиссия Prodamus",
    "Налог", "Отложено на СДЭК", "К выплате", "Тип", "Товар", "Печатал",
    "Комментарий",
]

# Старые названия столбцов -> новые. Переименовываем ячейку шапки на
# месте (см. _header_columns), а не дописываем новый столбец в конец —
# иначе старые и новые записи расползлись бы по разным колонкам.
_HEADER_RENAMES = {
    "Номер заказа": "Номер операции",
    "Оплата": "Сумма",
}


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


def _ensure_rows(sheet, need_rows: int):
    """Расширяет лист, если целевая строка выходит за текущий размер грида.

    append_row/append_rows растили лист сами по мере добавления строк;
    запись по фиксированному диапазону (batch_update) — нет, поэтому
    растим заранее, с запасом, чтобы не дёргать это на каждой строке."""
    if need_rows > sheet.row_count:
        sheet.add_rows(need_rows - sheet.row_count + 50)


def _ensure_cols(sheet, need_cols: int):
    """Аналогично _ensure_rows, но по столбцам — нужно, когда шапке
    дописывают недостающие названия (см. _header_columns)."""
    if need_cols > sheet.col_count:
        sheet.add_cols(need_cols - sheet.col_count + 10)


def _header_columns(sheet, headers: list[str]) -> dict[str, int]:
    """Название столбца из шапки листа -> номер столбца (с 1).

    Запись идёт по названию столбца, а не по фиксированной позиции A-G:
    если в листе руками вставили новый столбец (даже в середине — до
    существующих), шапка сдвинется, но бот всё равно попадёт в нужный
    по названию столбец, а не затрёт чужие данные/формулы и не собьёт
    условное форматирование, привязанное к конкретным буквам.

    Названий из headers, которых в шапке ещё нет (например, «Тип» и
    «Комментарий» — их не было в листе до появления кэшфлоу), бот сам
    дописывает в конец шапки, а не падает с ошибкой: так лист сам
    донастраивается под новые возможности бота, ничего вручную двигать
    не нужно.

    Старые названия (см. _HEADER_RENAMES) переименовываются в текущей
    позиции столбца — иначе они бы считались «отсутствующими» и бот
    дописал бы для них новый столбец в конец, расколов старые и новые
    записи по разным колонкам.
    """
    header_row = sheet.row_values(1)
    renamed = []
    for i, name in enumerate(header_row):
        new_name = _HEADER_RENAMES.get(name)
        if new_name and new_name not in header_row:
            header_row[i] = new_name
            renamed.append({"range": f"{_col_letter(i + 1)}1", "values": [[new_name]]})
    if renamed:
        sheet.batch_update(renamed, value_input_option="USER_ENTERED")
        logger.info(f"gsheets: переименованы столбцы шапки: {', '.join(r['values'][0][0] for r in renamed)}")

    cols = {name: i + 1 for i, name in enumerate(header_row) if name}
    missing = [h for h in headers if h not in cols]
    if missing:
        next_col = len(header_row) + 1
        _ensure_cols(sheet, next_col + len(missing) - 1)
        new_cells = []
        for h in missing:
            cols[h] = next_col
            new_cells.append({"range": f"{_col_letter(next_col)}1", "values": [[h]]})
            next_col += 1
        sheet.batch_update(new_cells, value_input_option="USER_ENTERED")
        logger.info(f"gsheets: в шапку листа дописаны столбцы: {', '.join(missing)}")
    return cols


def _row_cells(header_cols: dict[str, int], row_num: int, *, date_msk: str, code: str,
               amount: float, kind: str, comment: str = "",
               delivery_cost: float | None = None, goods_type: str | None = None,
               printer: str | None = None) -> list[dict]:
    """Ячейки одной строки кэшфлоу — привязаны к столбцу по названию из
    header_cols (см. _header_columns), не по букве A-G.

    kind — 'Приход' (оплата физ- или цифрового товара) или 'Расход'
    (трата из /expenses, выплата партнёру). Комиссия/Налог/К выплате
    считаются формулой только для приходов (delivery_cost задан) — у
    расходов эти столбцы просто не трогаем, их сумма никуда не идёт.

    goods_type — 'Физический'/'Цифровой' (только у приходов). printer —
    ники (@ник через запятую, если заказ печатали несколько человек) тех,
    кто печатал физтовар; в момент оплаты ещё не известен — простановка
    задним числом см. update_finance_printer."""
    def letter(name: str) -> str:
        return _col_letter(header_cols[name])

    values = {
        "Дата заказа": date_msk,
        "Номер операции": code,
        "Сумма": amount,
        "Тип": kind,
        "Комментарий": comment,
    }
    if goods_type is not None:
        values["Товар"] = goods_type
    if printer is not None:
        values["Печатал"] = printer
    if delivery_cost is not None:
        pay_cell = f"{letter('Сумма')}{row_num}"
        fee_cell = f"{letter('Комиссия Prodamus')}{row_num}"
        npd_cell = f"{letter('Налог')}{row_num}"
        cdek_cell = f"{letter('Отложено на СДЭК')}{row_num}"
        values["Комиссия Prodamus"] = _pct_formula(pay_cell, config.PRODAMUS_FEE_PERCENT)
        values["Налог"] = _pct_formula(pay_cell, config.NPD_PERCENT)
        values["Отложено на СДЭК"] = delivery_cost
        values["К выплате"] = f"={pay_cell}-{fee_cell}-{npd_cell}-{cdek_cell}"

    return [
        {"range": f"{letter(name)}{row_num}", "values": [[value]]}
        for name, value in values.items()
    ]


def _open_finance_sheet():
    """Открывает (создаёт при необходимости, с шапкой) финансовый лист.
    Возвращает (sheet, header_cols) — header_cols см. _header_columns."""
    book = _open_book()
    title = config.GOOGLE_SHEET_FINANCE_TAB
    try:
        sheet = book.worksheet(title)
    except Exception:
        sheet = book.add_worksheet(title=title, rows=200, cols=len(FINANCE_HEADERS))
        sheet.update(values=[FINANCE_HEADERS], range_name="A1")
        sheet.freeze(rows=1)
    header_cols = _header_columns(sheet, FINANCE_HEADERS)
    return sheet, header_cols


def _append_cashflow_row(code: str, date_msk: str, amount: float, kind: str,
                         comment: str = "", delivery_cost: float | None = None,
                         goods_type: str | None = None, printer: str | None = None) -> None:
    """Дописывает одну строку (приход или расход) в первую свободную
    строку финансового листа. Общая часть append_payment_row/append_expense_row.

    Блокирующая функция (gspread синхронный) — вызывать через to_thread.
    """
    try:
        sheet, header_cols = _open_finance_sheet()
        row = len(sheet.get_all_values()) + 1
        _ensure_rows(sheet, row)
        cells = _row_cells(header_cols, row, date_msk=date_msk, code=code, amount=amount,
                           kind=kind, comment=comment, delivery_cost=delivery_cost,
                           goods_type=goods_type, printer=printer)
        sheet.batch_update(cells, value_input_option="USER_ENTERED")
        logger.info(f"gsheets: записана строка {code!r} ({kind}) в финансовый лист (строка {row})")
    except SheetsError:
        raise
    except Exception as e:
        raise SheetsError(f"не удалось записать строку: {type(e).__name__}: {e}")


def append_payment_row(order_code: str, date_msk: str, amount: float, delivery_cost: float,
                       comment: str = "", goods_type: str | None = None,
                       printer: str | None = None) -> None:
    """Дописывает приход — оплату физ- или цифрового товара. Комиссия/
    Налог/К выплате — формулами, которые бот сам копирует в новую строку
    (по тем же ставкам, что в остальных расчётах бота).

    Блокирующая функция (gspread синхронный) — вызывать через to_thread.
    """
    _append_cashflow_row(order_code, date_msk, amount, "Приход", comment, delivery_cost,
                         goods_type, printer)


def append_expense_row(code: str, date_msk: str, amount: float, comment: str,
                       kind: str = "Расход") -> None:
    """Дописывает расход — трату из /expenses или выплату партнёру — в тот
    же финансовый лист. Без формул комиссии/налога/СДЭК: сумма расхода
    просто пишется как есть, дальше пользователь считает сам.

    Блокирующая функция (gspread синхронный) — вызывать через to_thread.
    """
    _append_cashflow_row(code, date_msk, amount, kind, comment, delivery_cost=None)


def request_finance_append(order_code: str, date_msk: str, amount: float, delivery_cost: float,
                           comment: str = "", goods_type: str | None = None,
                           printer: str | None = None):
    """Просит дописать приход в финансовый лист. Вызывать неблокирующе.

    В отличие от request_sync — без задержки: одна строка добавляется
    одним лёгким запросом, откладывать и схлопывать с другими нечего.
    """
    if not config.GSHEETS_ENABLED:
        return
    try:
        asyncio.create_task(_finance_append(order_code, date_msk, amount, delivery_cost,
                                            comment, goods_type, printer))
    except RuntimeError:
        logger.warning("gsheets: request_finance_append вне event loop, пропускаю")


async def _finance_append(order_code: str, date_msk: str, amount: float, delivery_cost: float,
                          comment: str = "", goods_type: str | None = None,
                          printer: str | None = None):
    try:
        await asyncio.to_thread(append_payment_row, order_code, date_msk, amount,
                                delivery_cost, comment, goods_type, printer)
    except SheetsError as e:
        logger.error(f"gsheets: запись в финансовый лист не удалась ({order_code}) — {e}")
    except Exception:
        logger.exception(f"gsheets: неожиданная ошибка записи в финансовый лист ({order_code})")


def request_expense_append(code: str, date_msk: str, amount: float, comment: str,
                           kind: str = "Расход"):
    """Просит дописать расход в финансовый лист. Вызывать неблокирующе."""
    if not config.GSHEETS_ENABLED:
        return
    try:
        asyncio.create_task(_expense_append(code, date_msk, amount, comment, kind))
    except RuntimeError:
        logger.warning("gsheets: request_expense_append вне event loop, пропускаю")


async def _expense_append(code: str, date_msk: str, amount: float, comment: str,
                          kind: str = "Расход"):
    try:
        await asyncio.to_thread(append_expense_row, code, date_msk, amount, comment, kind)
    except SheetsError as e:
        logger.error(f"gsheets: запись расхода в финансовый лист не удалась ({code}) — {e}")
    except Exception:
        logger.exception(f"gsheets: неожиданная ошибка записи расхода в финансовый лист ({code})")


def backfill_finance_rows(rows: list[dict]) -> tuple[int, int]:
    """Дописывает в финансовый лист операции из rows (см.
    db.get_cashflow_export_rows), которых там ещё нет — и приходы
    (физ-/цифровые товары), и расходы (траты, выплаты партнёрам).

    Идемпотентно: сверяет по коду операции (столбец «Номер операции», по
    названию из шапки — см. _header_columns) с уже вписанными строками —
    повторный запуск (или операции, попавшие туда «живой» записью до
    бэкафилла) не задвоит их. Возвращает (добавлено, уже было).

    Все ячейки всех новых строк пишутся ОДНИМ батч-запросом, а не по
    одной — иначе на полусотне операций легко упереться в лимит запросов
    Google API. Каждая ячейка привязана к столбцу по названию из шапки
    (см. _row_cells), не по фиксированной букве A-G.
    Блокирующая функция (gspread синхронный) — вызывать через to_thread.
    """
    sheet, header_cols = _open_finance_sheet()
    order_col = header_cols["Номер операции"]
    existing_codes = set(sheet.col_values(order_col)[1:])  # без шапки
    row_num = len(sheet.get_all_values()) + 1

    from services.payout import delivery_out

    cells = []
    added = 0
    skipped = 0
    for r in rows:
        code = r["code"]
        if not code or code in existing_codes:
            skipped += 1
            continue
        delivery_cost = None
        if r.get("delivery_cost") is not None:
            # Заказы до появления точного счёта СДЭК в purchases.delivery_cost —
            # оцениваем той же формулой, что и «Взаиморасчёт»/«Касса»
            # (delivery_legacy > 0 только когда delivery_cost так и не сохранился)
            delivery_cost = delivery_out(float(r["delivery_cost"]), float(r["delivery_legacy"]),
                                         config.PRODAMUS_FEE_PERCENT)
        cells += _row_cells(header_cols, row_num, date_msk=_msk(r["created_at"]), code=code,
                            amount=float(r["amount"]), kind=r["kind"],
                            comment=r.get("comment", ""), delivery_cost=delivery_cost,
                            goods_type=r.get("goods_type"), printer=r.get("printer"))
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


def update_finance_printer(order_code: str, printer: str) -> None:
    """Проставляет «Печатал» в уже существующей строке заказа.

    В момент оплаты (когда строка появляется в листе) печатающий ещё не
    известен — заказ может провисеть в очереди на печать сколько угодно,
    а до этого его ещё и передать другому админу. Поэтому колонку
    заполняем отдельно, когда заказ реально отмечен распечатанным (см.
    request_sync в handlers/order_actions.py).

    Тихо ничего не делает, если строки заказа в листе ещё нет (например,
    если бэкафилл ещё не запускали) — это не ошибка, а несинхронизированное
    состояние, разрешится следующим бэкафиллом.
    Блокирующая функция (gspread синхронный) — вызывать через to_thread.
    """
    sheet, header_cols = _open_finance_sheet()
    order_col = header_cols["Номер операции"]
    codes = sheet.col_values(order_col)
    try:
        row_num = codes.index(order_code) + 1  # +1: col_values 0-based, строки — с 1
    except ValueError:
        logger.warning(f"gsheets: заказ {order_code!r} не найден в финансовом листе "
                       f"для отметки печати")
        return
    printer_col = _col_letter(header_cols["Печатал"])
    sheet.update(values=[[printer]], range_name=f"{printer_col}{row_num}")
    logger.info(f"gsheets: заказ {order_code!r} — печатал {printer!r}")


def request_finance_printer_update(order_code: str, printer: str):
    """Просит проставить «Печатал» в финансовом листе. Вызывать неблокирующе."""
    if not config.GSHEETS_ENABLED:
        return
    try:
        asyncio.create_task(_finance_printer_update(order_code, printer))
    except RuntimeError:
        logger.warning("gsheets: request_finance_printer_update вне event loop, пропускаю")


async def _finance_printer_update(order_code: str, printer: str):
    try:
        await asyncio.to_thread(update_finance_printer, order_code, printer)
    except SheetsError as e:
        logger.error(f"gsheets: не проставить печатал ({order_code}) — {e}")
    except Exception:
        logger.exception(f"gsheets: неожиданная ошибка отметки печати ({order_code})")
