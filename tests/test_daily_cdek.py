import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import daily_report as report


class WeeklyCdekTests(unittest.IsolatedAsyncioTestCase):
    async def test_monday_in_moscow_uses_previous_full_week(self):
        with patch.object(report.db, 'get_cdek_accrued', new_callable=AsyncMock) as accrued:
            accrued.return_value = {'total': 1234.56, 'count': 3}
            text = await report._weekly_cdek_reminder(
                datetime(2026, 9, 13, 21, tzinfo=timezone.utc))
            accrued.assert_awaited_once_with('2026-09-07', '2026-09-13')
            self.assertIn('1,234.56 ₽', text)
            self.assertIn('оплатить', text)

    async def test_other_days_do_not_query_or_remind(self):
        with patch.object(report.db, 'get_cdek_accrued', new_callable=AsyncMock) as accrued:
            for day in range(15, 21):
                self.assertIsNone(await report._weekly_cdek_reminder(
                    datetime(2026, 9, day, 9, tzinfo=report.MSK)))
            accrued.assert_not_awaited()

    async def test_year_boundary_and_zero_amount(self):
        with patch.object(report.db, 'get_cdek_accrued', new_callable=AsyncMock) as accrued:
            accrued.return_value = {'total': 0, 'count': 0}
            text = await report._weekly_cdek_reminder(
                datetime(2027, 1, 4, tzinfo=report.MSK))
            accrued.assert_awaited_once_with('2026-12-28', '2027-01-03')
            self.assertIn('0.00 ₽', text)

    async def test_database_error_keeps_reminder_without_fake_zero(self):
        with patch.object(report.db, 'get_cdek_accrued', side_effect=RuntimeError('test')):
            with self.assertLogs(report.logger, level='ERROR'):
                text = await report._weekly_cdek_reminder(
                    datetime(2026, 9, 14, tzinfo=report.MSK))
            self.assertIn('недоступна', text)
            self.assertNotIn('0.00', text)

    async def test_reminder_is_separate_message_to_each_admin(self):
        notify = SimpleNamespace(send_message=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
        with patch.object(report, 'Bot', return_value=notify), \
             patch.object(report.config, 'ADMIN_IDS', [1, 2]), \
             patch.object(report, '_weekly_cdek_reminder', AsyncMock(return_value='reminder')), \
             patch.object(report.db, 'get_purchases_report', AsyncMock(return_value={'count': 0, 'total': 0, 'by_product': []})), \
             patch.object(report, '_expenses_for', AsyncMock(return_value={})), \
             patch('services.payout.split', AsyncMock(return_value={})), \
             patch.object(report, '_cdek_balance_line', AsyncMock(return_value='')):
            await report.send_daily_report(None)
        self.assertEqual(notify.send_message.await_count, 4)
        calls = notify.send_message.await_args_list
        for offset, admin in [(0, 1), (2, 2)]:
            self.assertIn('Отчёт за', calls[offset].args[1])
            self.assertEqual(calls[offset + 1].args, (admin, 'reminder'))
        notify.session.close.assert_awaited_once()
