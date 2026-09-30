import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite
import config
import database as db
from services import finance_sheet as fs, payout


def receipt(code='p1', amount=1000, when='2026-09-10 12:00:00'):
    return {'code': code, 'amount': amount, 'created_at': when, 'kind': 'Приход',
            'tax': 40, 'delivery': 100, 'goods_type': 'Физический',
            'comment': '=not_a_formula', 'partner_positions': 0}


def apply_updates(grid, requests):
    """Emulate only value writes; assert other columns are never erased."""
    for request in requests:
        update = request.get('updateCells')
        if not update:
            continue
        start = update['start']
        for offset, row in enumerate(update['rows']):
            r = start['rowIndex'] + offset
            while len(grid) <= r:
                grid.append([])
            for j, item in enumerate(row['values'], start['columnIndex']):
                while len(grid[r]) <= j:
                    grid[r].append('')
                value = item.get('userEnteredValue', {})
                grid[r][j] = next(iter(value.values()), '')
    return grid


class ReconciliationTests(unittest.TestCase):
    def test_load_does_not_require_summary_positions_or_settings(self):
        book = Mock()
        book.fetch_sheet_metadata.return_value = {'sheets': [
            {'properties': {'title': 'Финансы', 'sheetId': 42}},
            {'properties': {'title': config.GOOGLE_SHEET_FINANCE_TAB, 'sheetId': 7}},
        ]}
        book.worksheet.return_value.get_all_values.return_value = [fs.HEADERS]
        with patch.object(fs, '_open_book', return_value=book):
            loaded, props, values, year, legacy, modern = fs.load_workbook()
        book.worksheet.assert_called_once_with(config.GOOGLE_SHEET_FINANCE_TAB)
        self.assertEqual(values, {config.GOOGLE_SHEET_FINANCE_TAB: [fs.HEADERS]})
        self.assertEqual(legacy, 0)

    def test_idempotent_upsert_reordered_columns_and_user_notes(self):
        headers = ['Моя заметка', 'Номер операции', 'Сумма', 'Дата заказа']
        grid = [headers, ['keep', 'p1', 12, 'old date']]
        changes, _, _, stats = fs.cashflow_plan(1, grid, [receipt(), receipt('p2')])
        self.assertEqual((stats['updated'], stats['added']), (1, 1))
        apply_updates(grid, changes)
        self.assertEqual(grid[1][0], 'keep')
        self.assertEqual(grid[2][0], '')
        changes, _, _, stats = fs.cashflow_plan(1, grid, [receipt(), receipt('p2')])
        self.assertEqual(changes, [])
        self.assertEqual((stats['updated'], stats['added']), (0, 0))

    def test_deleted_and_duplicate_rows_do_not_contribute_money(self):
        grid = [list(fs.HEADERS) + ['Моя заметка']]
        for code in ['p1', 'p1', 'exp-1', 'my-manual']:
            values = fs.operation_values(receipt(code))
            if code == 'my-manual':
                values['Статус синхронизации'] = ''
            grid.append([values[k] for k in fs.HEADERS] + ['keep'])
        changes, _, _, stats = fs.cashflow_plan(1, grid, [receipt()])
        apply_updates(grid, changes)
        cols = {h: i for i, h in enumerate(grid[0])}
        for index, status in [(2, 'Дубликат'), (3, 'Отменено')]:
            self.assertEqual(grid[index][cols['Сумма']], '')
            self.assertEqual(grid[index][cols['Статус синхронизации']], status)
            self.assertEqual(grid[index][-1], 'keep')
        # Unrecognised user row is preserved and explicitly excluded.
        self.assertEqual(grid[4][cols['Сумма']], 1000)
        self.assertEqual(stats['manual'], 1)
        self.assertEqual(stats['duplicates'], 1)
        self.assertEqual(stats['cancelled'], 1)

    def test_duplicate_header_rejected_and_text_is_literal(self):
        with self.assertRaises(fs.SheetsError):
            fs.header_columns(['Сумма', 'Оплата'])
        self.assertEqual(fs.cell('=1+1'), {'userEnteredValue': {'stringValue': '=1+1'}})
        self.assertEqual(fs.cell(0), {'userEnteredValue': {'numberValue': 0}})

    def test_corrected_tax_rounding_and_moscow_boundary(self):
        rows = [receipt(str(i), 100, '2026-08-31 21:00:00') for i in range(3)]
        rows.append(receipt('free', 0, '2026-08-31 21:00:00'))
        fs.allocate_tax(rows, [{'month': '2026-09', 'gross': 300, 'accrued': 1}])
        self.assertEqual([r['tax'] for r in rows], [.33, .33, .34, 0])
        self.assertEqual(fs.msk_date(rows[0]['created_at']).month, 9)
        with self.assertRaises(fs.SheetsError):
            fs.allocate_tax(rows, [{'month': '2026-09', 'gross': 301, 'accrued': 1}])

    def test_balance_deducts_payments_once_and_reserves_only_from_free(self):
        snapshot = dict(arrived=1000, cdek_paid=100, cdek_reserve=200,
                        tax_paid=40, tax_reserve=20, expenses=50, pending=500,
                        fee=30, gross=1530, payouts={'total': 300, 'by_recipient': {
                            config.PARTNER_NAME: 100, config.OWNER_NAME: 200}},
                        year=2026, monthly=[[m]+[0]*9 for m in fs.MONTHS],
                        now='2026-09-25T12:00:00+00:00', rows=[],
                        cdek_overpaid=0, tax_overpaid=0)
        result = fs.dashboard(snapshot, 10, {'manual': 0, 'cancelled': 0, 'duplicates': 0})
        values = {r[0]: r[1] for r in result}
        self.assertEqual(values['Остаток по записям бота'], 500)
        self.assertEqual(values['По учёту после резервов'], 280)
        self.assertEqual(values['Ушло на выплаты Дане'], 100)
        self.assertEqual(values['Ушло на выплаты Мише'], 200)

    def test_cross_process_lock_prevents_overlapping_sync(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', str(Path(tmp)/'db')):
            with fs.process_lock():
                with self.assertRaises(fs.SheetsError):
                    with fs.process_lock():
                        self.fail('a second writer acquired the lock')


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, 'DB_PATH', str(Path(self.tmp.name)/'test.db'))
        self.patch.start()
        await db.init_db()
        async with aiosqlite.connect(db.DB_PATH) as con:
            await con.execute("INSERT INTO products(id,name,description,price,category) VALUES(1,'Digital','',1000,'digital')")
            await con.execute("INSERT INTO purchases(user_id, product_id, telegram_payment_id, amount,created_at) VALUES(1,1,'d1',1000,'2026-09-10 10:00:00')")
            await con.execute("INSERT INTO npd_assessments(month,amount) VALUES('2026-09',20)")
            await con.execute("INSERT INTO expenses(category,amount,spent_at) VALUES('Материалы',100,'2026-09-10 10:00:00')")
            await con.execute("INSERT INTO npd_payments(amount,tax_month,paid_at) VALUES(10,'2026-09','2026-09-11 10:00:00')")
            await con.execute("INSERT INTO cdek_payments(amount,paid_at) VALUES(50,'2026-09-11 10:00:00')")
            await con.execute("INSERT INTO payouts(recipient,amount,paid_at) VALUES(?,100,'2026-09-11 10:00:00')",(config.PARTNER_NAME,))
            await con.commit()
        self.now = datetime(2026,9,25,tzinfo=timezone.utc)

    async def asyncTearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    async def test_snapshot_uses_shared_profit_and_tax_calculation(self):
        s = await fs.build_snapshot(2026, self.now)
        share = await payout.split('2026-09-01', '2026-09-30')
        self.assertEqual(s['gross'], 1000)
        self.assertEqual(s['tax_paid'], 10)
        self.assertEqual(s['tax_reserve'], 10)
        self.assertEqual(s['cdek_reserve'], 0)
        self.assertEqual(s['cdek_overpaid'], 50)
        self.assertEqual(s['monthly'][8][7], fs.money(share['net']))
        self.assertEqual(s['monthly'][8][8], fs.money(share['partner']))
        for row in s['monthly']:
            self.assertEqual(fs.money(row[8] + row[9]), row[7])
        self.assertEqual(s['rows'][0]['tax'], 20)
        self.assertEqual(s['pending'], 0)

    async def test_share_rounding_assigns_residual_without_creating_a_cent(self):
        share = await payout.split('2026-09-01', '2026-09-30')
        share.update(net=1.014, partner=.205, owner=.809)
        with patch.object(payout, 'split', new=AsyncMock(return_value=share)):
            snapshot = await fs.build_snapshot(2026, self.now)
        row = snapshot['monthly'][8]
        self.assertEqual(row[7:10], [1.01, .21, .8])

    async def test_source_change_during_read_retries(self):
        original = fs._build_snapshot
        calls = []
        async def mutate_once(year, now):
            result = await original(year, now)
            if not calls:
                await db.add_expense('Материалы', 10)
            calls.append(1)
            return result
        with patch.object(fs, '_build_snapshot', side_effect=mutate_once):
            result = await fs.build_snapshot(2026, self.now)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['expenses'], 110)

    async def test_expense_payer_is_separate_from_record_author(self):
        expense_id = await db.add_expense('Пластик', 1184, 'PLA', 999, 'author', paid_by='Миша')
        rows = await db.get_cashflow_export_rows(config.ADMIN_IDS, config.PARTNER_ID)
        row = next(r for r in rows if r['code'] == f'exp-{expense_id}')
        self.assertEqual(row['paid_by'], 'Миша')
        self.assertEqual(row['amount'], 1184)
        self.assertEqual(fs.operation_values(row)['Кто внёс расход'], 'Миша')
        self.assertEqual(fs.operation_values(row)['Тип'], 'Расход')

    async def test_publish_is_one_atomic_request_and_idempotent(self):
        snapshot = await fs.build_snapshot(2026, self.now)
        props = {config.GOOGLE_SHEET_FINANCE_TAB: {'sheetId': 1, 'gridProperties': {'rowCount': 200, 'columnCount': 20}},
                 fs.SUMMARY_TAB: {'sheetId': 2, 'gridProperties': {'rowCount': 200, 'columnCount': 10}}}
        values = {config.GOOGLE_SHEET_FINANCE_TAB: [], fs.SUMMARY_TAB: []}
        book = Mock()
        stats = fs.publish(book, props, values, snapshot, 0, True, preserve_summary=False)
        book.batch_update.assert_called_once()
        requests = book.batch_update.call_args.args[0]['requests']
        self.assertEqual(stats['added'], 5)
        cash = [r for r in requests if r.get('updateCells',{}).get('start',{}).get('sheetId') == 1]
        grid = apply_updates([], cash)
        changes, _, _, second = fs.cashflow_plan(1, grid, snapshot['rows'])
        self.assertEqual(changes, [])
        self.assertEqual(second['added'], 0)
        self.assertTrue(any(r.get('updateCells',{}).get('start',{}).get('sheetId') == 2 for r in requests))

    async def test_default_publish_only_touches_cashflow(self):
        snapshot = await fs.build_snapshot(2026, self.now)
        props = {config.GOOGLE_SHEET_FINANCE_TAB: {'sheetId': 1, 'gridProperties': {'rowCount': 300, 'columnCount': 25}},
                 fs.SUMMARY_TAB: {'sheetId': 2, 'gridProperties': {'rowCount': 30, 'columnCount': 10}}}
        book = Mock()
        fs.publish(book, props, {config.GOOGLE_SHEET_FINANCE_TAB: []}, snapshot, 0, True)
        requests = book.batch_update.call_args.args[0]['requests']
        self.assertTrue(requests)
        for request in requests:
            self.assertNotIn('addSheet', request)
            body = next(iter(request.values()))
            target = body.get('start', body.get('range', body.get('properties', {})))
            self.assertEqual(target.get('sheetId'), 1, request)

    async def test_worker_retries_failure_and_keeps_events_during_sync(self):
        wake = asyncio.Event()
        attempts = []
        delays = []
        async def sync():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError('temporary network error')
            wake.set()
        async def wait(awaitable, timeout):
            awaitable.close()
            delays.append(timeout)
            if len(delays) == 1:
                raise asyncio.TimeoutError()
            self.assertTrue(wake.is_set())
            raise asyncio.CancelledError()
        with patch.object(config, 'GSHEETS_ENABLED', True), patch.object(fs, '_wake', wake), \
             patch.object(fs, 'sync_finance', side_effect=sync), \
             patch.object(fs.asyncio, 'wait_for', side_effect=wait):
            with self.assertLogs(fs.logger, level='ERROR'):
                with self.assertRaises(asyncio.CancelledError):
                    await fs.finance_sync_loop()
        self.assertEqual(delays, [fs.RETRY, fs.PERIOD])
        self.assertEqual(len(attempts), 2)
