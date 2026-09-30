"""Bank statement facts and dated cash checkpoints, separate from profit.

A checkpoint is an observed bank position, not an invented expense. Future
changes are estimates from the bot until another statement is reconciled.
"""
import json
import aiosqlite
import database as db


async def load():
    async with aiosqlite.connect(db.DB_PATH) as con:
        async with con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bank_reconciliations'") as cur:
            if not await cur.fetchone():
                return None, {}
        async with con.execute('SELECT data_json FROM bank_reconciliations ORDER BY id DESC LIMIT 1') as cur:
            row = await cur.fetchone()
        async with con.execute('SELECT code, reason FROM cashflow_noncash') as cur:
            noncash = dict(await cur.fetchall())
    return json.loads(row[0]) if row else None, noncash


def cash_components(snapshot):
    """Cash-only source totals; offsets still belong to profit/partner debt."""
    noncash_income = sum(float(r['amount']) * (1 - snapshot['fee_percent'] / 100)
                         for r in snapshot['rows']
                         if r['kind'] == 'Приход' and r.get('noncash'))
    noncash_payouts = {}
    for r in snapshot['rows']:
        if r['code'].startswith('payout-') and r.get('noncash'):
            recipient = r.get('recipient', '')
            noncash_payouts[recipient] = noncash_payouts.get(recipient, 0) + float(r['amount'])
    paid = {name: amount - noncash_payouts.get(name, 0)
            for name, amount in snapshot['payouts']['by_recipient'].items()}
    return {'arrived': snapshot['arrived'] - noncash_income,
            'cdek_paid': snapshot['cdek_paid'], 'tax_paid': snapshot['tax_paid'],
            'expenses': snapshot['expenses'], 'payouts': paid}


def projected_cash(snapshot):
    """Use bank facts through the checkpoint, source deltas only afterwards."""
    bank = snapshot.get('bank')
    if not bank:
        return None
    current = cash_components(snapshot)
    baseline = bank['baseline']
    result = {k: bank['cash'][k] + current[k] - baseline[k]
              for k in ('arrived', 'cdek_paid', 'tax_paid', 'expenses')}
    names = set(bank['cash']['payouts']) | set(current['payouts']) | set(baseline['payouts'])
    result['payouts'] = {name: bank['cash']['payouts'].get(name, 0)
                        + current['payouts'].get(name, 0) - baseline['payouts'].get(name, 0)
                        for name in names}
    result['total_payouts'] = sum(result['payouts'].values())
    result['balance'] = (result['arrived'] - result['cdek_paid'] - result['tax_paid']
                         - result['expenses'] - result['total_payouts'])
    return result
