"""Экран месяца НПД: Даня и Миша отдельно + что начислила налоговая."""
import unittest
from unittest import mock

import config
from handlers import finance
from services import npd

# Как пришло из «Мой налог» (раздел «Налоги») 17.11.2026
RECORDS = [
    {"taxPeriodId": 202609, "taxAmount": -80.16, "chargeDate": "2026-11-10", "type": "REDUCED_CORRECTION"},
    {"taxPeriodId": 202610, "taxAmount": 6334.52, "chargeDate": "2026-11-10", "type": "ACCRUED"},
    {"taxPeriodId": 202609, "taxAmount": 1826.36, "chargeDate": "2026-10-10", "type": "ACCRUED"},
]


class MonthChargesTest(unittest.TestCase):
    def test_october_due_includes_september_correction(self):
        ch = npd.month_charges(RECORDS, "2026-10")
        self.assertEqual(ch["accrued"], 6334.52)
        self.assertEqual(ch["corrections"], -80.16)
        self.assertEqual(ch["due"], 6254.36)
        self.assertEqual(ch["later"], [])

    def test_september_shows_later_correction(self):
        ch = npd.month_charges(RECORDS, "2026-09")
        self.assertEqual(ch["due"], 1826.36)
        self.assertEqual(ch["later"], [("2026-11-10", -80.16)])

    def test_not_charged_yet(self):
        self.assertIsNone(npd.month_charges(RECORDS, "2026-11"))


class NpdDetailLinesTest(unittest.TestCase):
    def test_split_by_person(self):
        purchases = [
            {"amount": 1000, "who": "danya", "tax": 40.0},
            {"amount": 4990, "who": "danya", "tax": 299.4},     # юрлицо, 6%
            {"amount": 399, "who": "misha", "tax": 15.96},
        ]
        assessment = {"accrued": 6254.36, "paid": 6254.36, "manual": 1}
        with mock.patch.object(config, "NPD_PERCENT", 4.0), \
             mock.patch.object(finance, "_NPD_RATE", 0.04):
            text = "\n".join(finance._npd_detail_lines(
                "2026-10", purchases, assessment, refunds=500,
                charges={"danya": npd.month_charges(RECORDS, "2026-10"), "misha": None}))
        danya, misha = text.split(f"👤 <b>{config.OWNER_NAME}</b>")
        self.assertIn("продаж: <b>2</b> на <b>5,990.00 ₽</b>, возвраты −500.00 ₽", danya)
        self.assertIn("Оценка бота: 319.40 ₽", danya)          # 40 + 299,40 − 4% с возврата
        self.assertIn("начислено <b>6,334.52 ₽</b>", danya)
        self.assertIn("к оплате <b>6,254.36 ₽</b>", danya)
        self.assertIn("В боте: <b>6,254.36 ₽</b> · вручную", danya)
        self.assertIn("продаж: <b>1</b> на <b>399.00 ₽</b>", misha)
        self.assertIn("Оценка бота: 15.96 ₽", misha)
        self.assertIn("ещё не начисляла", misha)
        self.assertNotIn("В боте", misha)

    def test_connection_error_is_shown(self):
        text = "\n".join(finance._npd_detail_lines(
            "2026-10", [{"amount": 100, "who": "danya", "tax": 4.0}], None,
            charges={"danya": "«Мой налог» не ответил"}))
        self.assertIn("Налоговая: не узнать — «Мой налог» не ответил", text)


if __name__ == "__main__":
    unittest.main()
