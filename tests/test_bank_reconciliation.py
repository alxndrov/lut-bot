import copy
import unittest
from services.bank_reconciliation import cash_components, projected_cash
from services.finance_sheet import dashboard, MONTHS, money


class BankCashTests(unittest.TestCase):
    def snapshot(self):
        snapshot = dict(arrived=2000, cdek_paid=100, cdek_reserve=200,
                        tax_paid=40, tax_reserve=20, expenses=50, pending=500,
                        fee=30, fee_percent=0, gross=2500,
                        payouts={'total': 500, 'by_recipient': {'Даня': 100, 'Миша': 400}},
                        year=2026, monthly=[[m]+[0]*9 for m in MONTHS],
                        now='2026-09-25T12:00:00+00:00',
                        rows=[{'code':'order-1','kind':'Приход','amount':200,'noncash':'offset'},
                              {'code':'payout-1','kind':'Расход','amount':200,
                               'recipient':'Миша','noncash':'offset'}],
                        cdek_overpaid=0, tax_overpaid=0)
        snapshot['bank'] = {'as_of':'2026-09-25', 'pending':500, 'noncash_payouts':200,
                            'accounts':[{'closing':1000}, {'net':50}],
                            'unmatched_expense':1184, 'reimbursement_difference':10,
                            'baseline':cash_components(snapshot),
                            'cash':{'arrived':1540, 'cdek_paid':100, 'tax_paid':40,
                                    'expenses':50, 'payouts':{'Даня':100,'Миша':200}}}
        return snapshot

    def test_offsets_do_not_leave_bank_but_still_reduce_partner_debt(self):
        s = self.snapshot()
        cash = cash_components(s)
        self.assertEqual(cash['arrived'],1800)
        self.assertEqual(cash['payouts']['Миша'],200)
        self.assertEqual(s['payouts']['by_recipient']['Миша'],400)
        self.assertEqual(projected_cash(s)['balance'],1050)

    def test_future_cash_deltas_do_not_reapply_historical_expenses(self):
        s = self.snapshot()
        s['arrived'] += 100
        s['expenses'] += 20
        s['payouts']['by_recipient']['Даня'] += 10
        result = projected_cash(s)
        self.assertEqual(result['balance'],1120)
        self.assertEqual(result['expenses'],70)

    def test_checkpoint_does_not_subtract_legacy_withdrawals_twice(self):
        s = self.snapshot()
        rows = dashboard(s,8967.44,{'manual':0,'cancelled':0,'duplicates':0})
        data = {r[0]:r[1] for r in rows}
        self.assertEqual(data['Баланс Альфы по выписке'],1000)
        self.assertEqual(data['Расчётный остаток по двум счетам'],1050)
        self.assertEqual(data['После резервов на Альфе'],780)
        self.assertEqual(data['Ранее выведено'],8967.44)

    def test_extra_noncash_payout_cannot_change_bank_balance(self):
        s = self.snapshot()
        before = projected_cash(s)['balance']
        s['payouts']['by_recipient']['Миша'] += 300
        s['rows'].append({'code':'payout-2','kind':'Расход','amount':300,'recipient':'Миша','noncash':'offset'})
        self.assertEqual(projected_cash(s)['balance'], before)
