import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import daily_report as report


class DailyReportCdekTests(unittest.IsolatedAsyncioTestCase):
    async def test_monday_sends_only_the_report_no_cdek_reminder(self):
        """Еженедельное напоминание «оплатить счёт СДЭК» убрано (Даня, 17.11.2026):
        в понедельник каждому админу — только отчёт."""
        self.assertFalse(hasattr(report, '_weekly_cdek_reminder'))
        notify = SimpleNamespace(send_message=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
        monday = datetime(2026, 11, 16, 10, tzinfo=report.MSK)
        fake_dt = SimpleNamespace(now=lambda tz=None: monday)
        with patch.object(report, 'Bot', return_value=notify), \
             patch.object(report, 'datetime', fake_dt), \
             patch.object(report.config, 'ADMIN_IDS', [1, 2]), \
             patch.object(report.db, 'get_purchases_report', AsyncMock(return_value={'count': 0, 'total': 0, 'by_product': []})), \
             patch.object(report, '_expenses_for', AsyncMock(return_value={})), \
             patch('services.payout.split', AsyncMock(return_value={})), \
             patch.object(report, '_cdek_balance_line', AsyncMock(return_value='')):
            await report.send_daily_report(None)
        calls = notify.send_message.await_args_list
        self.assertEqual([c.args[0] for c in calls], [1, 2])
        for c in calls:
            self.assertIn('Отчёт за', c.args[1])
        notify.session.close.assert_awaited_once()
