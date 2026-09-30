"""Reconcile the financial workbook with SQLite; one atomic Google write.

SQLite is the durable queue: startup and periodic reconciliation recover missed
notifications, restarts and failed writes. User-added columns are never written.
"""
import asyncio
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import fcntl
import logging
import math
from pathlib import Path
import re

import aiosqlite
import config
import database as db
from services import payout, tax_account
from services.gsheets import SheetsError, _open_book

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))
SUMMARY_TAB = 'Финансы'
SETTINGS_TAB = 'Настройки финансов'
HEADERS = [
    'Дата операции', 'Номер операции', 'Тип', 'Сумма', 'Комиссия Prodamus',
    'Налог', 'Отложено на СДЭК', 'К выплате', 'Комментарий', 'Товар',
    'Печатал', 'Позиций напечатал Даня', 'Получатель', 'Статус синхронизации',
    'Движение денег', 'Налоговый месяц', 'База процента партнёра', 'Кто внёс расход',
]
RENAMES = {'Дата заказа': 'Дата операции', 'Номер заказа': 'Номер операции', 'Оплата': 'Сумма'}
MONTHS = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
          'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']
PERIOD = 300
RETRY = 60
_wake = asyncio.Event()
_lock = asyncio.Lock()


def money(value):
    return float(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def msk_date(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(
        tzinfo=timezone.utc).astimezone(MSK)


def serial_date(value):
    if len(value) == 10:
        return (datetime.fromisoformat(value) - datetime(1899, 12, 30)).days
    return (msk_date(value).replace(tzinfo=None) - datetime(1899, 12, 30)).total_seconds() / 86400


def request_finance_sync():
    if config.GSHEETS_ENABLED:
        _wake.set()


@contextmanager
def process_lock():
    """Also serialize manual reconciliation against the running bot."""
    path = Path(db.DB_PATH).resolve().with_suffix('.finance-sync.lock')
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SheetsError('финансовая сверка уже выполняется')
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def allocate_tax(rows, months):
    """Allocate corrected monthly NPD to receipts, preserving exact cents."""
    groups = defaultdict(list)
    for row in rows:
        if row['kind'] == 'Приход':
            groups[msk_date(row['created_at']).strftime('%Y-%m')].append(row)
    for month in months:
        group = groups.get(month['month'], [])
        gross = sum(float(r['amount']) for r in group)
        if abs(gross - month['gross']) > .005:
            raise SheetsError(f"выручка выгрузки за {month['month']} не совпала с базой")
        if not gross:
            for row in group:
                row['tax'] = 0.0
            continue
        positive = [r for r in group if r['amount'] != 0]
        for row in group:
            row['tax'] = money(Decimal(str(row['amount'])) * Decimal(str(month['accrued']))
                               / Decimal(str(gross)))
        # Deterministic rounding residual; a free replacement must keep zero tax.
        positive[-1]['tax'] = money(positive[-1]['tax'] + month['accrued']
                                    - sum(r['tax'] for r in group))


async def _build_snapshot(year, now, monthly_split=True):
    rows = await db.get_cashflow_export_rows(config.ADMIN_IDS, config.PARTNER_ID)
    counts = Counter(str(r['code']) for r in rows)
    if any(not r['code'] for r in rows) or any(n != 1 for n in counts.values()):
        raise SheetsError('пустые или повторяющиеся коды операций в базе')
    from services.bank_reconciliation import load
    bank, noncash = await load()
    for row in rows:
        row['noncash'] = noncash.get(str(row['code']), '')
    months = await tax_account.months()
    allocate_tax(rows, months)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute("SELECT id, COALESCE(tax_month, strftime('%Y-%m', datetime(paid_at, '+3 hours'))) FROM npd_payments") as cur:
            tax_months = {f'npd-{i}': m for i, m in await cur.fetchall()}
        async with conn.execute("""SELECT COALESCE(o.order_code, o.prodamus_order_id),
            COALESCE(SUM(p.delivery_amount), 0) FROM orders o LEFT JOIN purchases p
            ON p.telegram_payment_id=o.prodamus_order_id GROUP BY o.id""") as cur:
            delivery_customer = dict(await cur.fetchall())
    assessment = {m['month']: m for m in months}
    for row in rows:
        row['tax_month'] = tax_months.get(row['code'], '')
        if row['kind'] == 'Приход':
            m = assessment.get(msk_date(row['created_at']).strftime('%Y-%m'), {})
            rate = m.get('accrued', 0) / m['gross'] if m.get('gross') else 0
            goods = float(row['amount']) - delivery_customer.get(row['code'], 0)
            row['partner_base'] = goods * (1 - config.PRODAMUS_FEE_PERCENT / 100 - rate) - float(row.get('delivery_legacy') or 0) * rate
    payments = await db.get_payouts_for_finance_export()
    recipients = {f"payout-{p['id']}": p['recipient'] for p in payments}
    for row in rows:
        row['recipient'] = recipients.get(row['code'], '')
        if row.get('delivery_cost') is not None:
            row['delivery'] = payout.delivery_out(float(row['delivery_cost']),
                                                  float(row['delivery_legacy']),
                                                  config.PRODAMUS_FEE_PERCENT)
    payouts = await db.get_payouts_summary()
    cdek = await db.get_cdek_account()
    tax_paid = await db.get_npd_payments_summary()
    expenses = await db.get_expenses_summary('1900-01-01', '2100-12-31')
    gross = sum(float(r['amount']) for r in rows if r['kind'] == 'Приход')
    fee = gross * config.PRODAMUS_FEE_PERCENT / 100
    # Share the bot's remittance calendar, including its documented limitations.
    from handlers.finance import _pending_gross
    pending_gross = await _pending_gross('1900-01-01 00:00:00',
                                       now.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), now)
    pending = pending_gross * (1 - config.PRODAMUS_FEE_PERCENT / 100)
    active_months = {msk_date(r['created_at']).month for r in rows
                     if msk_date(r['created_at']).year == year}
    monthly = []
    for month in range(1, 13):
        if monthly_split and month in active_months:
            s = await payout.split(*payout.month_bounds(year, month))
            monthly.append([MONTHS[month - 1], *[money(s[k]) for k in
                ('gross', 'fee', 'npd', 'delivery_out')],
                money(s['net'] + s['expenses']), money(s['expenses']),
                money(s['net']), money(s['partner']),
                money(money(s['net']) - money(s['partner']))])
        else:
            monthly.append([MONTHS[month - 1]] + [0] * 9)
    return {
        'rows': rows, 'year': year, 'monthly': monthly, 'now': now.isoformat(),
        'tax_months': months, 'bank': bank, 'fee_percent': config.PRODAMUS_FEE_PERCENT,
        'gross': money(gross), 'fee': money(fee), 'pending': money(pending),
        'arrived': money(gross - fee - pending), 'cdek_paid': money(cdek['paid']),
        'cdek_reserve': money(max(0, cdek['due'])),
        'cdek_overpaid': money(max(0, -cdek['due'])),
        'tax_paid': money(tax_paid['total']),
        'tax_reserve': money(sum(max(0, m['accrued'] - m['paid']) for m in months)),
        'tax_overpaid': money(sum(max(0, m['paid'] - m['accrued']) for m in months)),
        'expenses': money(expenses['total']), 'payouts': payouts,
    }


async def build_snapshot(year, now=None, monthly_split=True):
    now = now or datetime.now(timezone.utc)
    # A connection kept open observes commits made by any other connection.
    # Retry if any source changed while the helpers were reading it.
    async with aiosqlite.connect(db.DB_PATH) as observer:
        async def version():
            async with observer.execute('PRAGMA data_version') as cur:
                return (await cur.fetchone())[0]
        for _ in range(3):
            before = await version()
            snapshot = await _build_snapshot(year, now, monthly_split)
            if await version() == before:
                return snapshot
        raise SheetsError('база менялась во время сверки; повторю автоматически')


async def free_cash(now=None) -> dict:
    """Сколько денег на счету свободно после всех резервов — по той же
    формуле, что строка «После резервов на счету» листа «Финансы»: всё
    принятое минус комиссия, то, что Prodamus ещё не перевёл, оплаченные
    СДЭК/НПД/расходы, все выплаты (включая ранние) и минус ещё не
    оплаченные СДЭК и НПД. Из этого и только из этого можно платить доли."""
    now = now or datetime.now(timezone.utc)
    snap = await build_snapshot(now.year, now, monthly_split=False)
    on_account = money(snap['arrived'] - snap['cdek_paid'] - snap['tax_paid']
                       - snap['expenses'] - snap['payouts']['total'] - config.EARLY_PAYOUTS)
    return {'on_account': on_account, 'pending': snap['pending'],
            'cdek_reserve': snap['cdek_reserve'], 'tax_reserve': snap['tax_reserve'],
            'free': money(on_account - snap['cdek_reserve'] - snap['tax_reserve'])}


@dataclass(frozen=True)
class Formula:
    expression: str


def cell(value):
    if isinstance(value, Formula):
        return {'userEnteredValue': {'formulaValue': value.expression}}
    if value is None or value == '':
        return {}
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise SheetsError('нечисловое значение в финансовом расчёте')
        return {'userEnteredValue': {'numberValue': value}}
    # Text is always literal, including comments beginning with '='.
    return {'userEnteredValue': {'stringValue': str(value)}}


def write_cells(sheet_id, row, column, values):
    return {'updateCells': {
        'start': {'sheetId': sheet_id, 'rowIndex': row, 'columnIndex': column},
        'rows': [{'values': [cell(v) for v in line]} for line in values],
        'fields': 'userEnteredValue',
    }}


def header_columns(header):
    header = [RENAMES.get(h, h) for h in header]
    duplicates = [h for h, n in Counter(header).items() if h in HEADERS and n > 1]
    if duplicates:
        raise SheetsError('повторяющиеся колонки: ' + ', '.join(duplicates))
    for name in HEADERS:
        if name not in header:
            header.append(name)
    return header, {h: i for i, h in enumerate(header) if h}


def operation_values(row):
    income = row['kind'] == 'Приход'
    amount = float(row['amount'])
    fee = amount * config.PRODAMUS_FEE_PERCENT / 100 if income else None
    tax = row.get('tax', 0) if income else None
    delivery = row.get('delivery', 0) if income else None
    return dict(zip(HEADERS, [
        serial_date(row['created_at']), str(row['code']), row['kind'], amount,
        fee, tax, delivery, amount - fee - tax - delivery if income else None,
        row.get('comment', ''), row.get('goods_type', ''), row.get('printer', ''),
        row.get('partner_positions') if income else None,
        row.get('recipient', ''), 'Сверено',
        'Взаимозачёт — без движения денег' if row.get('noncash') else 'Денежная операция',
        row.get('tax_month', ''), row.get('partner_base') if income else None, row.get('paid_by', ''),
    ]))


def cashflow_plan(sheet_id, existing, rows):
    """Upsert by code; keep row positions and any user-added columns."""
    header, cols = header_columns(list(existing[0]) if existing else [])
    requests = []
    if not existing or header != existing[0]:
        requests.append(write_cells(sheet_id, 0, 0, [header]))
    expected = {str(r['code']): r for r in rows}
    seen = set()
    stats = {'added': 0, 'updated': 0, 'cancelled': 0, 'duplicates': 0, 'manual': 0}

    def at(line, name):
        i = cols[name]
        return line[i] if i < len(line) else ''

    def changes(index, current, desired):
        count = 0
        for name, value in desired.items():
            actual = at(current, name)
            if (actual if actual is not None else '') != (value if value is not None else ''):
                requests.append(write_cells(sheet_id, index, cols[name], [[value]]))
                count += 1
        return count

    for index, current in enumerate(existing[1:], 1):
        code = str(at(current, 'Номер операции')).strip()
        if not code:
            if any(current):
                stats['manual'] += 1
            continue
        if code in expected and code not in seen:
            stats['updated'] += bool(changes(index, current, operation_values(expected[code])))
            seen.add(code)
        elif code in expected or re.fullmatch(r'(exp|payout|cdek|npd)-\d+', code) or at(current, 'Статус синхронизации') in ('Сверено', 'Отменено', 'Дубликат'):
            duplicate = code in expected
            desired = {h: None for h in HEADERS if h not in
                       ('Номер операции', 'Дата операции', 'Комментарий')}
            desired['Статус синхронизации'] = 'Дубликат' if duplicate else 'Отменено'
            changes(index, current, desired)
            stats['duplicates' if duplicate else 'cancelled'] += 1
        else:
            changes(index, current, {'Статус синхронизации': 'Ручная запись — вне сводки'})
            stats['manual'] += 1
    next_row = max(1, len(existing))
    for code, row in expected.items():
        if code not in seen:
            changes(next_row, [], operation_values(row))
            next_row += 1
            stats['added'] += 1
    return requests, header, next_row, stats


def dashboard(snapshot, legacy, stats):
    from services.bank_reconciliation import projected_cash
    projected = projected_cash(snapshot)
    bank = snapshot.get('bank')
    snapshot = dict(snapshot)
    if projected:
        snapshot.update({k: money(projected[k]) for k in ('arrived', 'cdek_paid', 'tax_paid', 'expenses')})
        snapshot['payouts'] = {'total': money(projected['total_payouts']), 'by_recipient': projected['payouts']}
    payouts = snapshot['payouts']
    partner = money(payouts['by_recipient'].get(config.PARTNER_NAME, 0))
    owner = money(payouts['by_recipient'].get(config.OWNER_NAME, 0))
    other = money(payouts['total'] - partner - owner)
    balance = money(snapshot['arrived'] - snapshot['cdek_paid'] - snapshot['tax_paid']
                    - snapshot['expenses'] - payouts['total'] - (0 if bank else legacy))
    free = money(balance - snapshot['cdek_reserve'] - snapshot['tax_reserve'])
    values = [
        ['MALIMABI — ФИНАНСЫ'],
        ['Движение денег — по выпискам и изменениям после сверки' if bank else 'Движение денег за всё время'],
        ['Пришло денег', snapshot['arrived'], 'Переводы Prodamus по выпискам; новые поступления после сверки — оценка по боту' if bank else 'Поступления на счета после комиссии Prodamus; по расчётному календарю выплат'],
        ['Ушло на СДЭК', snapshot['cdek_paid'], 'Оплаты по выпискам и последующие изменения бота' if bank else 'Оплаченные счета'],
        ['Ещё отложено на СДЭК', snapshot['cdek_reserve'], 'Начислено за доставки минус оплачено; часть баланса'],
        ['Ушло на НПД', snapshot['tax_paid'], 'Оплаты в периоде представленных выписок и последующие изменения бота' if bank else 'Фактические оплаты налога'],
        ['Ещё отложено на НПД', snapshot['tax_reserve'], 'Неоплаченный налог по месяцам с учётом исправлений; часть баланса'],
        ['Ушло на расходы', snapshot['expenses'], 'Банковские списания и возмещения; расходы вне выписок показаны ниже' if bank else 'Все расходы, внесённые в боте'],
        ['Ушло на выплаты Дане', partner, 'Денежные выплаты; взаимозачёты исключены' if bank else 'Фактически записанные выплаты'],
        ['Ушло на выплаты Мише', owner, 'Денежные выплаты; взаимозачёты исключены' if bank else 'Фактически записанные выплаты'],
        ['Баланс Альфы по выписке' if bank else 'Остаток по записям бота', bank['accounts'][0]['closing'] if bank else balance, 'Подтверждено на ' + bank['as_of'] + '; другой счёт и расчёт после сверки — в блоке ниже' if bank else 'Не подтверждён остатком на счетах; включает резервы СДЭК и НПД'],
        ['После резервов на Альфе' if bank else 'По учёту после резервов', money(bank['accounts'][0]['closing'] - snapshot['cdek_reserve'] - snapshot['tax_reserve']) if bank else free, 'Остаток Альфы на дату выписки минус все текущие резервы проекта' if bank else 'Расчёт по базе; доступные деньги требуют сверки с фактическим остатком'],
        ['Ещё не перевёл Prodamus', snapshot['pending'], 'После комиссии; оценка по календарю без праздников и банковской сверки'],
        ['Комиссия Prodamus', snapshot['fee'], 'Уже вычтена из поступлений; повторно из баланса не вычитается'],
        ['Ранее выведено', legacy, 'До периода выписок, справочно: повторно из банковского баланса не вычитается' if bank else 'Историческая корректировка; отдельно от выплат Дане и Мише'],
        ['Принято от покупателей', snapshot['gross'], 'За всё время по боту, включая неденежные взаимозачёты; не равно банковским поступлениям'],
        ['Прочие выплаты', other, 'Выплаты получателям, отличным от Дани и Миши'],
        [],
        ['Год', snapshot['year'], 'Можно изменить год; помесячный блок обновится в течение 5 минут'],
        ['ПО МЕСЯЦАМ — НАЧИСЛЕННАЯ ПРИБЫЛЬ И ДОЛИ'],
        ['Месяц', 'Выручка', 'Комиссия', 'Налог НПД', 'Начислено СДЭК',
         'После вычетов', 'Расходы', 'Чистая прибыль', 'Даня', 'Миша'],
        *snapshot['monthly'],
        ['ИТОГО ЗА ГОД', *[money(sum(r[i] for r in snapshot['monthly'])) for i in range(1, 10)]],
        [],
        ['Последняя успешная сверка', datetime.fromisoformat(snapshot['now']).astimezone(MSK).strftime('%d.%m.%Y %H:%M:%S МСК')],
        ['Состояние', f"По базе: {len(snapshot['rows'])} операций",
         f"{'Есть сверка выписок; охват указан ниже.' if bank else 'С банком не сверено.'} Вне сводки: ручных {stats['manual']}, отменённых {stats['cancelled']}, дублей {stats['duplicates']}"],
        ['Правила расчёта', 'Совпадают с ботом', 'Исправленный НПД; доли 60/40 с 02.09.2026. Выплаты долей не уменьшают прибыль.'],
        ['Переплата СДЭК', snapshot['cdek_overpaid'], 'Показывается отдельно, резерв не уходит в минус'],
        ['Переплата НПД', snapshot['tax_overpaid'], 'По налоговым месяцам; не скрывает долг другого месяца'],
    ]
    if bank:
        values += [
            [],
            ['БАНКОВСКАЯ СВЕРКА', bank['as_of']],
            ['Альфа — остаток по PDF', bank['accounts'][0]['closing'], 'Подтверждён выпиской на ' + bank['as_of']],
            ['Другой счёт — обороты CSV', bank['accounts'][1]['net'], '27.07–17.08.2026. Начальный и конечный остатки в файле не указаны; это разница оборотов.'],
            ['Prodamus — по скриншоту', bank['pending'], 'Отдельно от банковских счетов; ' + bank['as_of']],
            ['Свободно на Альфе после резервов', money(bank['accounts'][0]['closing'] - snapshot['cdek_reserve'] - snapshot['tax_reserve']), 'Если все текущие резервы хранятся на Альфе; остаток банка на дату выписки'],
            ['Взаимозачёты Мише', bank['noncash_payouts'], 'Учитываются в долге партнёру, но не списываются с банковских счетов'],
            ['Расходы вне выписок', bank['unmatched_expense'], 'Красный PLA от 20.08 — есть в боте, нет в представленных банковских операциях'],
            ['Разница возмещения 31.08', bank['reimbursement_difference'], 'Расходы в боте 4 095 ₽, банковский перевод 4 085 ₽; прибыль не изменена'],
            ['Расчётный остаток по двум счетам', balance, 'Полностью подтверждённый текущий итог требует начального остатка CSV и операций после 17.08'],
        ]
    return [r + [''] * (10 - len(r)) for r in values]


def formula_dashboard(snapshot, legacy, stats, cols):
    """Aggregates are Sheets formulas; source amounts and bank facts remain inputs."""
    from gspread.utils import rowcol_to_a1
    tab = "'" + config.GOOGLE_SHEET_FINANCE_TAB.replace("'", "''") + "'!"
    def ref(name):
        letter = rowcol_to_a1(1, cols[name] + 1)[:-1]
        return f'{tab}{letter}2:{letter}'
    def quoted(value):
        return '"' + str(value).replace('"', '""') + '"'
    def total(column='Сумма', kind='Приход', filters=()):
        args = [ref(column), ref('Статус синхронизации'), '"Сверено"', ref('Тип'), quoted(kind)]
        for name, condition in filters:
            args.extend([ref(name), condition])
        return 'SUMIFS(' + ';'.join(args) + ')'
    def expense(prefix, extra=()):
        return total(kind='Расход', filters=(('Номер операции', quoted(prefix + '-*')), *extra))
    values = dashboard(snapshot, legacy, stats)
    def put(row, expression, label=None, note=None):
        values[row-1][1] = Formula('=' + expression)
        if label is not None:
            values[row-1][0] = label
        if note is not None:
            values[row-1][2] = note
    cash = (('Движение денег', '"Денежная операция"'),)
    values[1][0] = 'Движение денег — суммы операций cashflow'
    put(3, total(), note='Сумма всех приходов cashflow до комиссии, включая взаимозачёты. Это выручка, а не переводы на банк.')
    put(4, expense('cdek'), note='Сумма оплат СДЭК в cashflow')
    put(5, f'ROUND(MAX(0;{total("Отложено на СДЭК")}-B4);2)')
    put(6, expense('npd'), note='Все оплаты НПД в cashflow, включая периоды до банковских выписок')
    put(8, expense('exp'), note='Все расходы cashflow, включая расходы без подтверждения в представленных выписках')
    put(9, expense('payout', (*cash, ('Получатель', quoted(config.PARTNER_NAME)))))
    put(10, expense('payout', (*cash, ('Получатель', quoted(config.OWNER_NAME)))))
    put(14, f'ROUND({total("Комиссия Prodamus")};2)', note='Комиссия со всех приходов; в строке «Пришло денег» ещё не вычтена')
    put(15, "'Настройки финансов'!B2")
    put(17, expense('payout', cash) + '-B9-B10')
    # Keep each tax month's debt separate: prepayments must not mask another month's debt.
    debts, credits = [], []
    for month in snapshot.get('tax_months', []):
        y, m = map(int, month['month'].split('-'))
        period = (('Дата операции', f'">="&DATE({y};{m};1)'), ('Дата операции', f'"<"&DATE({y};{m+1};1)'))
        accrued = total('Налог', filters=period)
        paid = expense('npd', (('Налоговый месяц', quoted(month['month'])),))
        debts.append(f'MAX(0;{accrued}-{paid})')
        credits.append(f'MAX(0;{paid}-{accrued})')
    put(7, 'ROUND(' + ('+'.join(debts) or '0') + ';2)')
    put(40, 'ROUND(' + ('+'.join(credits) or '0') + ';2)')
    put(39, f'ROUND(MAX(0;B4-{total("Отложено на СДЭК")});2)')
    date = ref('Дата операции')
    pending = (f'SUMPRODUCT(({ref("Тип")}="Приход")*({ref("Статус синхронизации")}="Сверено")*'
               f'({ref("Движение денег")}="Денежная операция")*'
               f'(INT({date})+CHOOSE(WEEKDAY({date};2);2;2;2;4;3;2;2)>TODAY())*'
               f'({ref("Сумма")}-{ref("Комиссия Prodamus")}))')
    put(13, f'ROUND({pending};2)')
    if snapshot.get('bank'):
        put(11, "'Банковская сверка'!B2", note='Подтверждённый остаток Альфы на дату выписки. Не является результатом вычитания строк cashflow.')
        put(12, 'ROUND(B11-B5-B7;2)')
        put(16, 'SUMIF(\'Банковская сверка\'!F12:F;"Поступление Prodamus";\'Банковская сверка\'!D12:D)',
            'Пришло на банки по выпискам', 'Только переводы Prodamus в представленных выписках; другой период и сумма после комиссии')
        for row, source in [(43,2),(44,3),(45,5),(48,7),(49,8)]:
            put(row, f"'Банковская сверка'!B{source}")
        personal_owner = [r for r in snapshot['rows'] if r.get('paid_by') == config.OWNER_NAME]
        if personal_owner:
            put(48, expense('exp', (('Кто внёс расход', quoted(config.OWNER_NAME)),)),
                'Миша оплатил лично — расходы',
                'PLA 1 184 ₽ оплатил Миша. Возмещение не подтверждено: проверить состав прежних выплат до повторного перевода. Это не установленный долг.')
        put(46, 'ROUND(B43-B5-B7;2)')
        put(47, expense('payout', (('Движение денег', '"Взаимозачёт — без движения денег"'), ('Получатель', quoted(config.OWNER_NAME)))))
        put(50, "ROUND(SUM('Банковская сверка'!D12:D)-SUM('Банковская сверка'!E12:E);2)",
            'Разница оборотов двух выписок', 'Не текущий остаток: в CSV отсутствуют начальный остаток и операции после 17.08')
    else:
        put(16, f'ROUND({total(filters=cash)}-{total("Комиссия Prodamus", filters=cash)}-B13;2)', 'На счета по расчётному календарю')
        put(11, 'ROUND(B16-B4-B6-B8-B9-B10-B15-B17;2)')
        put(12, 'ROUND(B11-B5-B7;2)')
    values[18][2] = 'Измените год — формулы пересчитают помесячный блок сразу'
    cut = 'DATE(' + config.NEW_SPLIT_FROM.replace('-', ';') + ')'
    def pct(value):
        return str(value).replace('.', ',') + '%'
    for m in range(1, 13):
        r = 21 + m
        period = (('Дата операции', f'">="&DATE($B$19;{m};1)'), ('Дата операции', f'"<"&DATE($B$19;{m+1};1)'))
        expressions = [total(c, filters=period) for c in ('Сумма', 'Комиссия Prodamus', 'Налог', 'Отложено на СДЭК')]
        expressions += [f'B{r}-C{r}-D{r}-E{r}', expense('exp', period), f'F{r}-G{r}']
        old = (*period, ('Дата операции', f'"<"&{cut}'))
        new = (*period, ('Дата операции', f'">="&{cut}'))
        old_phys = total('База процента партнёра', filters=(*old, ('Товар', '"Физический"')))
        old_digital = total('База процента партнёра', filters=(*old, ('Товар', '"Цифровой"')))
        prints = total('Позиций напечатал Даня', filters=old)
        new_net = total('К выплате', filters=new) + '-' + expense('exp', new)
        expressions += [f'{old_phys}*{pct(config.PARTNER_GOODS_PERCENT)}+{old_digital}*{pct(config.PARTNER_DIGITAL_PERCENT)}+{prints}*{config.PARTNER_PRINT_FEE}+({new_net})*{pct(config.PARTNER_GOODS_PERCENT_NEW)}', f'H{r}-I{r}']
        values[r-1][1:] = [Formula(f'=ROUND({e};2)') for e in expressions]
    for col in range(1,10):
        letter = rowcol_to_a1(1, col+1)[:-1]
        values[33][col] = Formula(f'=ROUND(SUM({letter}22:{letter}33);2)')
    return values


def load_workbook():
    """Only cashflow is bot-owned; the user owns summary formulas and layout.

    No dependency on summary cell positions, year selector, or deleted helper
    sheets. The year below is only used by the internal reference calculation.
    """
    book = _open_book()
    metadata = book.fetch_sheet_metadata()
    props = {s['properties']['title']: s['properties'] for s in metadata['sheets']}
    title = config.GOOGLE_SHEET_FINANCE_TAB
    values = {title: (book.worksheet(title).get_all_values(value_render_option='UNFORMATTED_VALUE')
                     if title in props else [])}
    return book, props, values, datetime.now(MSK).year, 0, True


def formatting(sid, header_row=0, summary=False):
    dark = {'red': .10, 'green': .17, 'blue': .24}
    white = {'red': 1, 'green': 1, 'blue': 1}
    def format_range(r0, r1, c0, c1, fmt):
        return {'repeatCell': {'range': {'sheetId': sid, 'startRowIndex': r0, 'endRowIndex': r1,
                                       'startColumnIndex': c0, 'endColumnIndex': c1},
                               'cell': {'userEnteredFormat': fmt}, 'fields': 'userEnteredFormat'}}
    requests = [format_range(0, 1, 0, 10 if summary else len(HEADERS),
                            {'backgroundColor': dark, 'textFormat': {'bold': True, 'foregroundColor': white}})]
    if summary:
        requests.insert(0, format_range(0, 40, 0, 10, {
            'backgroundColor': white, 'textFormat': {'fontFamily': 'Arial', 'fontSize': 11,
            'foregroundColor': dark}, 'verticalAlignment': 'MIDDLE', 'wrapStrategy': 'CLIP',
            'numberFormat': {'type': 'NUMBER', 'pattern': '0.#########'},
        }))
        requests += [

            format_range(20, 21, 0, 10, {'backgroundColor': dark, 'textFormat': {'bold': True, 'foregroundColor': white}, 'wrapStrategy': 'WRAP'}),
            format_range(2, 17, 1, 2, {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0.00" ₽"'}}),
            format_range(21, 34, 1, 10, {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0.00" ₽"'}}),
            format_range(38, 40, 1, 2, {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0.00" ₽"'}}),
            format_range(18, 19, 1, 2, {'numberFormat': {'type': 'NUMBER', 'pattern': '0'}, 'backgroundColor': {'red': 1, 'green': .95, 'blue': .8}}),
        ]
        for row in (10, 11, 33):
            requests.append(format_range(row, row + 1, 0, 10 if row == 33 else 2,
                {'backgroundColor': {'red': .86, 'green': .94, 'blue': .89}, 'textFormat': {'bold': True},
                 'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0.00" ₽"'}}))
        # Notes span C:J in the top block; monthly data keep individual columns.
        for row in list(range(2, 17)) + [18, 36, 37, 38, 39]:
            requests.append({'mergeCells': {'range': {'sheetId': sid, 'startRowIndex': row,
                             'endRowIndex': row + 1, 'startColumnIndex': 2, 'endColumnIndex': 10}, 'mergeType': 'MERGE_ALL'}})
        for c0, c1, width in [(0, 1, 285), (1, 2, 180), (2, 10, 120)]:
            requests.append({'updateDimensionProperties': {'range': {'sheetId': sid, 'dimension': 'COLUMNS', 'startIndex': c0, 'endIndex': c1},
                                                           'properties': {'pixelSize': width}, 'fields': 'pixelSize'}})
        requests.append({'updateSheetProperties': {'properties': {'sheetId': sid, 'gridProperties': {'frozenRowCount': 2}}, 'fields': 'gridProperties.frozenRowCount'}})
    return requests


def publish(book, props, values, snapshot, legacy, modern, *, preserve_summary=True):
    requests = []
    used_ids = {p['sheetId'] for p in props.values()}
    def ensure_sheet(title, rows, cols):
        if title not in props:
            sid = max(used_ids, default=0) + 1
            used_ids.add(sid)
            props[title] = {'sheetId': sid, 'gridProperties': {'rowCount': max(200, rows), 'columnCount': max(15, cols)}}
            requests.append({'addSheet': {'properties': {'title': title, **props[title]}}})
        p = props[title]
        grid = p['gridProperties']
        if grid['rowCount'] < rows or grid['columnCount'] < cols:
            requests.append({'updateSheetProperties': {'properties': {'sheetId': p['sheetId'], 'gridProperties': {
                'rowCount': max(grid['rowCount'], rows + 50), 'columnCount': max(grid['columnCount'], cols)}},
                'fields': 'gridProperties.rowCount,gridProperties.columnCount'}})
        return p['sheetId']

    title = config.GOOGLE_SHEET_FINANCE_TAB
    existing = values[title]
    header, cols = header_columns(existing[0] if existing else [])
    cash_id = ensure_sheet(title, len(existing) + len(snapshot['rows']) + 1, len(header))
    changes, header, end, stats = cashflow_plan(cash_id, existing, snapshot['rows'])
    requests.extend(changes)
    for name in ('Дата операции', 'Сумма', 'Комиссия Prodamus', 'Налог', 'Отложено на СДЭК', 'К выплате'):
        fmt = {'type': 'DATE_TIME', 'pattern': 'dd.mm.yyyy hh:mm'} if name == 'Дата операции' else {'type': 'NUMBER', 'pattern': '#,##0.00'}
        requests.append({'repeatCell': {'range': {'sheetId': cash_id, 'startRowIndex': 1, 'endRowIndex': end,
            'startColumnIndex': cols[name], 'endColumnIndex': cols[name] + 1}, 'cell': {'userEnteredFormat': {'numberFormat': fmt}},
            'fields': 'userEnteredFormat.numberFormat'}})
    requests.append({'updateSheetProperties': {'properties': {'sheetId': cash_id, 'gridProperties': {'frozenRowCount': 1}}, 'fields': 'gridProperties.frozenRowCount'}})
    if preserve_summary:
        # Ordinary synchronisation must never rebuild the user-owned summary,
        # formatting, helper sheets or formulas, including during backfill.
        book.batch_update({'requests': requests})
        return stats
    summary_id = ensure_sheet(SUMMARY_TAB, 55, 10)
    if not modern:
        requests.append({'unmergeCells': {'range': {'sheetId': summary_id, 'startRowIndex': 0, 'endRowIndex': 40, 'startColumnIndex': 0, 'endColumnIndex': props[SUMMARY_TAB]['gridProperties']['columnCount']}}})
    requests.append(write_cells(summary_id, 0, 0, formula_dashboard(snapshot, legacy, stats, cols)))
    if not modern:
        # Rebuild formatting only on migration, so later user sizing is preserved.
        requests.extend(formatting(summary_id, summary=True))
        requests.extend(formatting(cash_id))
    if snapshot.get('bank'):
        bank = snapshot['bank']
        bank_rows = [
            ['БАНКОВСКАЯ СВЕРКА', bank['as_of']],
            ['Альфа: подтверждённый остаток', bank['accounts'][0]['closing']],
            ['CSV: поступления минус списания', bank['accounts'][1]['net']],
            ['Охват CSV', '27.07–17.08.2026; начальный/конечный остаток отсутствуют'],
            ['Prodamus по скриншоту', bank['pending']],
            ['Взаимозачёты Мише', bank['noncash_payouts'], 'Не являются банковским расходом'],
            ['Расход без банковского списания', bank['unmatched_expense'], 'Красный PLA, 20.08; сохранён в прибыли бота'],
            ['Разница возмещения материалов', bank['reimbursement_difference'], '31.08: расходы 4 095 ₽, перевод 4 085 ₽'],
            ['Транзит 14.08', 1402, 'Пополнение и списание одной суммы; итог ноль, не выручка и не дивиденды'],
            [],
            ['Дата', 'Счёт', 'Код банка', 'Поступление', 'Списание', 'Категория', 'Операции бота'],
        ]
        bank_rows += [[r['date'], r['account'], r['bank_code'], max(0, r['amount']),
                       max(0, -r['amount']), r['category'], ', '.join(r['codes'])]
                      for r in bank['ledger']]
        bank_id = ensure_sheet('Банковская сверка', len(bank_rows), 7)
        requests.append(write_cells(bank_id, 0, 0, [r + [''] * (7-len(r)) for r in bank_rows]))
        requests.append({'updateSheetProperties': {'properties': {'sheetId': bank_id,
                         'gridProperties': {'frozenRowCount': 11}}, 'fields': 'gridProperties.frozenRowCount'}})
        for sid, start_row, end_row, start_col, end_col in (
            (bank_id, 11, len(bank_rows), 3, 5), (summary_id, 42, 50, 1, 2),
        ):
            requests.append({'repeatCell': {'range': {'sheetId': sid, 'startRowIndex': start_row,
                'endRowIndex': end_row, 'startColumnIndex': start_col, 'endColumnIndex': end_col},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0.00" ₽"'}}},
                'fields': 'userEnteredFormat.numberFormat'}})

    # One spreadsheets.batchUpdate: cashflow, dashboard and success time commit
    # together. A failed request cannot publish a new success marker alone.
    book.batch_update({'requests': requests})
    return stats


async def sync_finance():
    async with _lock:
        with process_lock():
            book, props, values, year, legacy, modern = await asyncio.to_thread(load_workbook)
            snapshot = await build_snapshot(year)
            write = asyncio.create_task(asyncio.to_thread(
                publish, book, props, values, snapshot, legacy, modern))
            try:
                stats = await asyncio.shield(write)
            except asyncio.CancelledError:
                # A to_thread write keeps running after cancellation. Keep both
                # locks until it finishes, so a second writer cannot overtake it.
                try:
                    await write
                finally:
                    raise
            logger.info('finance_sheet: сверено %s операций, %s', len(snapshot['rows']), stats)
            return stats


async def finance_sync_loop():
    if not config.GSHEETS_ENABLED:
        return
    while True:
        # Clear before syncing: events arriving during the sync remain pending.
        _wake.clear()
        delay = PERIOD
        try:
            await sync_finance()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('finance_sheet: сверка не удалась; повтор через %s с', RETRY)
            delay = RETRY
        try:
            await asyncio.wait_for(_wake.wait(), timeout=delay)
            await asyncio.sleep(5)  # Coalesce bursts, including events during sync.
        except asyncio.TimeoutError:
            pass
