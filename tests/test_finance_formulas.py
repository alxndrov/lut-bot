"""Formula output must use real cashflow fields without executing source text."""
import unittest
from unittest.mock import patch
from services import finance_sheet as fs


class FormulaTests(unittest.TestCase):
    def test_explicit_formula_type_does_not_execute_comments(self):
        self.assertEqual(fs.cell(fs.Formula('=SUM(A1:A3)')),
                         {'userEnteredValue': {'formulaValue': '=SUM(A1:A3)'}})
        self.assertEqual(fs.cell('=SUM(A1:A3)'),
                         {'userEnteredValue': {'stringValue': '=SUM(A1:A3)'}})

    def test_formulas_follow_reordered_columns_and_exclude_noncash_payouts(self):
        headers = list(reversed(fs.HEADERS))
        cols = {h: i for i, h in enumerate(headers)}
        snapshot = {'monthly': [[name] + [0] * 9 for name in fs.MONTHS],
                    'tax_months': [{'month': '2026-08'}, {'month': '2026-09'}]}
        with patch.object(fs, 'dashboard', return_value=[[''] * 10 for _ in range(50)]):
            rows = fs.formula_dashboard(snapshot, 0, {}, cols)
        gross = rows[2][1].expression
        self.assertIn("'cashflow'!O2:O", gross)  # amount follows its header
        self.assertIn('"Приход"', gross)
        self.assertIn('"Сверено"', gross)
        self.assertNotIn('Денежная операция', gross)  # includes noncash sales
        self.assertIn('Денежная операция', rows[8][1].expression)
        self.assertIn('payout-*', rows[8][1].expression)
        reserve = rows[6][1].expression
        self.assertEqual(reserve.count('MAX(0;'), 2)  # no cross-month debt netting
        self.assertIn('2026-08', reserve)
        self.assertIn('2026-09', reserve)
        self.assertIn('$B$19', rows[21][1].expression)
        self.assertIn('DATE(2026;09;02)', rows[21][8].expression)
        self.assertEqual(rows[33][1], fs.Formula('=ROUND(SUM(B22:B33);2)'))


if __name__ == '__main__':
    unittest.main()
