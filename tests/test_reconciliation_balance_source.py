from services.reconciliation_balance_source import (
    build_balance_source,
    load_reconciliation_balance_source,
    build_uploaded_ledger_balances,
    load_uploaded_ledger_balances,
    build_cashflow_differences,
    build_cashflow_reconciliation,
)

import copy
import hashlib
import json
from decimal import Decimal
import pytest


@pytest.fixture
def independent_year_boundary_case():
    """Two separately transcribed books; expected amounts never use the engine."""
    import pandas as pd
    journal = pd.DataFrame([
        ['BEFORE', '2025-12-14', 'B1', 20, 0], ['BEFORE', '2025-12-14', 'AR', 0, 20],
        ['RECEIPT', '2025-12-15', 'B1', 97.25, 0], ['RECEIPT', '2025-12-15', 'AR', 0, 100],
        ['RECEIPT', '2025-12-15', 'FEE', 2.75, 0],
        ['TRANSFER', '2025-12-20', 'B1', 0, 40], ['TRANSFER', '2025-12-20', 'C1', 40, 0],
        ['MIXED', '2025-12-31', 'B1', 0, 10.25], ['MIXED', '2025-12-31', 'C1', 10, 0],
        ['MIXED', '2025-12-31', 'FEE', .25, 0],
        ['REVERSAL', '2026-01-01', 'B1', 0, 38.90], ['REVERSAL', '2026-01-01', 'AR', 40, 0],
        ['REVERSAL', '2026-01-01', 'FEE', 0, 1.10],
        ['PAYMENT', '2026-01-05', 'C1', 0, 25.50], ['PAYMENT', '2026-01-05', 'AP', 25.50, 0],
        ['REFUND', '2026-01-20', 'C1', 5.10, 0], ['REFUND', '2026-01-20', 'AP', 0, 5.10],
        ['REPEATS', '2026-01-25', 'C1', 3.33, 0], ['REPEATS', '2026-01-25', 'C1', 3.33, 0],
        ['REPEATS', '2026-01-25', 'AR', 0, 6.66],
        ['NEGATIVE', '2026-01-30', 'B1', 0, -12.34], ['NEGATIVE', '2026-01-30', 'EXP', -12.34, 0],
        ['AFTER', '2026-02-11', 'B1', 9.99, 0], ['AFTER', '2026-02-11', 'AR', 0, 9.99],
    ], columns=['voucher_id', 'date', 'account_code', 'debit', 'credit'])
    journal['account_name'] = journal['account_code']
    journal['voucher_detail'] = journal['voucher_id']
    coa = pd.DataFrame([dict(account_code=code, account_title=code, category=category)
        for code, category in [('B1', ''), ('B0', ''), ('C1', ''), ('AR', 'Collections'),
                               ('FEE', 'Charges'), ('AP', 'Suppliers'), ('EXP', 'Operating')]])
    # Independent ledger transcription includes activity outside the selected dates.
    ledger_entries = [
        ('B1', '2025-12-14', '20', '0'), ('B1', '2025-12-15', '97.25', '0'),
        ('B1', '2025-12-20', '0', '40'), ('C1', '2025-12-20', '40', '0'),
        ('B1', '2025-12-31', '0', '10.25'), ('C1', '2025-12-31', '10', '0'),
        ('B1', '2026-01-01', '0', '38.90'), ('C1', '2026-01-05', '0', '25.50'),
        ('C1', '2026-01-20', '5.10', '0'), ('C1', '2026-01-25', '3.33', '0'),
        ('C1', '2026-01-25', '3.33', '0'), ('B1', '2026-01-30', '12.34', '0'),
        ('B1', '2026-02-11', '9.99', '0'),
    ]
    rows = [_canonical(i, day, debit, credit, code) for i, (code, day, debit, credit)
            in enumerate(ledger_entries, 1)]
    uploads = [_uploaded(rows, opening=opening, start='2025-12-01', end='2026-02-28',
                         identity=i, code=code) for i, (code, opening)
               in enumerate([('B1', '-50'), ('B0', '0'), ('C1', '0')], 1)]
    return dict(journal=journal, coa=coa, rows=rows, uploads=uploads,
        accounts={'B1': 'Bank', 'B0': 'Bank', 'C1': 'Cash'},
        start='2025-12-15', end='2026-02-10',
        roles={'customer_receipt': ['Collections'], 'bank_charge': ['Charges']},
        # Hand calculation: -30 + 97 - 40.30 = 26.70; transfers cancel combined.
        expected={'bank': [('47.00', '-30', '17'), ('-26.56', '17', '-9.56'), ('0', '-9.56', '-9.56')],
                  'cash': [('50', '0', '50'), ('-13.74', '50', '36.26'), ('0', '36.26', '36.26')],
                  'combined': [('97', '-30', '67'), ('-40.30', '67', '26.70'), ('0', '26.70', '26.70')]})


@pytest.mark.parametrize('presentation', ['net', 'gross'])
@pytest.mark.parametrize('grouping', ['month', 'year'])
def test_independent_books_prove_year_boundary_carry_and_keep_allocation_review(
        independent_year_boundary_case, presentation, grouping):
    from services.ledger_group_summary import build_cashflow_summary
    case = independent_year_boundary_case
    original = copy.deepcopy([case['rows'], case['uploads']])
    summary = build_cashflow_summary(case['journal'], case['coa'], ['C1'], ['B1', 'B0'],
        case['start'], case['end'], group_by=grouping, receipt_presentation=presentation,
        cashflow_role_categories=case['roles'], vendor_account_codes=['AP'])
    flows = {kind: {r['period_key']: r['explained_movement'] for r in summary['treasury_movement_proof']
                    if r['treasury_type'] == kind} for kind in ('bank', 'cash', 'combined')}
    result = build_uploaded_ledger_balances(case['uploads'], case['rows'], case['accounts'],
        summary['periods'], flows['combined'], treasury_movements={kind: flows[kind] for kind in ('bank', 'cash')})
    assert result['status'] == 'Matched'
    for kind in ('bank', 'cash', 'combined'):
        evidence = result if kind == 'combined' else result['treasury_breakdown'][kind]
        expected = case['expected'][kind]
        if grouping == 'year':
            expected = [expected[0], (str(Decimal(expected[1][0]) + Decimal(expected[2][0])), expected[1][1], expected[2][2])]
        for period, (movement, opening, closing) in zip(evidence['periods'], expected, strict=True):
            for field, amount in [('cashflow_net_movement', movement), ('ledger_movement_total', movement),
                                  ('opening_total', opening), ('calculated_opening', opening),
                                  ('closing_total', closing), ('calculated_closing', closing)]:
                assert Decimal(str(period[field])).quantize(Decimal('.01')) == Decimal(amount).quantize(Decimal('.01'))
            assert period['variance'] == pytest.approx(0, abs=1e-9)
        assert evidence['overall']['closing_total'] == pytest.approx(float(expected[-1][2]))
        assert evidence['overall']['opening_total'] == float(expected[0][1])
    # An arithmetic match must never certify an ambiguous category allocation.
    assert summary['treasury_reconciliation']['status'] == 'Review required'
    assert summary['treasury_reconciliation']['source_status'] == 'Matched'
    assert all(r['allocation_status'] == 'Review required' for r in summary['treasury_movement_proof']
               if r['period_key'] == summary['periods'][0]['key'])
    dormant = next(a for a in result['periods'][0]['accounts'] if a['account_code'] == 'B0')
    assert dormant['opening_balance'] == dormant['closing_balance'] == dormant['ledger_movement'] == 0
    assert [case['rows'], case['uploads']] == original


def _rows():
    return [
        {"account_code": "B1", "period_month": "2026-01", "opening_balance": 1000, "actual_closing_balance": 1100},
        {"account_code": "B1", "period_month": "2026-02", "opening_balance": 1000, "actual_closing_balance": 900},
        {"account_code": "B2", "period_month": "2026-01", "opening_balance": 0, "actual_closing_balance": 50},
        {"account_code": "B2", "period_month": "2026-02", "opening_balance": 0, "actual_closing_balance": 0},
    ]


def test_balance_source_returns_engine_maps_and_preserves_zero():
    source = build_balance_source(_rows(), ["B1", "B2"], "2026-01-01", "2026-02-28")

    assert source.ready
    assert source.months == ("2026-01", "2026-02")
    assert source.opening_balances == {"B1": 1000.0, "B2": 0.0}
    assert source.actual_closing_balances["B2"]["2026-02"] == 0.0


def test_balance_source_reports_missing_accounts_months_and_not_zero_substitution():
    source = build_balance_source(
        [{"account_code": "B1", "period_month": "2026-01", "opening_balance": 1000, "actual_closing_balance": 1100}],
        ["B1", "B2"],
        "2026-01-01",
        "2026-02-28",
    )

    assert not source.ready
    codes = {item["code"] for item in source.blocking_diagnostics()}
    assert "missing_closing_balance" in codes
    assert "missing_opening_balance" in codes
    assert "B2" not in source.opening_balances


def test_balance_source_rejects_duplicate_and_inconsistent_values():
    rows = _rows()
    rows.append({"account_code": "B1", "period_month": "2026-01", "opening_balance": 1000, "actual_closing_balance": 1100})
    rows[1]["opening_balance"] = 999
    source = build_balance_source(rows, ["B1", "B2"], "2026-01", "2026-02")

    codes = {item["code"] for item in source.blocking_diagnostics()}
    assert "duplicate_balance_month" in codes
    assert "inconsistent_opening_balance" in codes


def test_balance_source_loader_uses_client_and_group_selector():
    calls = []

    def query(sql, params=None):
        calls.append((sql, params))
        return _rows()

    source = load_reconciliation_balance_source(
        7,
        "Bank",
        ["B1", "B2"],
        "2026-01-01",
        "2026-02-28",
        query_fn=query,
    )

    assert source.ready
    assert calls[0][1] == (7, "Bank")
    assert "report_balance_inputs" in calls[0][0]


PERIOD = {'key': '2026-01', 'start': '2026-01-01', 'end': '2026-01-31', 'label': 'Jan-26'}


def _canonical(identity=1, day='2026-01-05', debit='50', credit='0', code='B1'):
    return dict(id=identity, version=1, extract_date=day, debit=debit, credit=credit,
                source_fingerprint=f'source-{identity}', account_code=code, template_id=11)


def _uploaded(rows, opening='150', start='2026-01-01', end='2026-01-31', identity=1,
              code='B1', source='inferred_from_transaction'):
    inside = sorted((r for r in rows if r['account_code'] == code and start <= str(r['extract_date'])[:10] <= end), key=lambda r: r['id'])
    movement = sum((Decimal(str(r['debit'] or 0)) - Decimal(str(r['credit'] or 0)) for r in inside), Decimal(0))
    fingerprint = hashlib.md5(','.join(f"{r['id']}:{r['version']}:{r['source_fingerprint']}" for r in inside).encode()).hexdigest()
    return dict(id=identity, template_id=11, ledger_account_code=code, ledger_account_name='Bank Alpha',
                ledger_period_start=start, ledger_period_end=end, configuration_current=True,
                ledger_evidence={'opening': None, 'closing': None,
                                 'balance_check': {'source_mode': 'replace_range', 'status': 'consistent',
                                                   'opening_source': source, 'opening': opening,
                                                   'closing': str(Decimal(opening) + movement),
                                                   'canonical_fingerprint': fingerprint}})


def _resolve(uploads=None, rows=None, periods=None, flows=None, accounts=None):
    rows = [_canonical()] if rows is None else rows
    uploads = [_uploaded(rows)] if uploads is None else uploads
    return build_uploaded_ledger_balances(uploads, rows, accounts or {'B1': 'Bank'},
                                         periods or [PERIOD], {'2026-01': 50} if flows is None else flows)


def _difference_source(ledger=None, transactions=None):
    ledger = [dict(_canonical(), date='2026-01-05', voucher_id='V1')] if ledger is None else ledger
    transactions = [dict(id=100, account_code='B1', voucher_id='V1', date='2026-01-05', debit='50', credit='0')] if transactions is None else transactions
    upload = _uploaded(ledger)
    upload['comparison_roles_available'] = True
    return dict(uploads=[upload], ledger_rows=ledger, transaction_rows=transactions)


@pytest.fixture
def horizontal_reconciliation_case():
    """Independent small working paper: gross receipts 900, payments 350."""
    import pandas as pd
    journal = pd.DataFrame([
        ['RECEIPT', '2026-01-05', 'B1', 950, 0],
        ['RECEIPT', '2026-01-05', 'AR', 0, 1000],
        ['RECEIPT', '2026-01-05', 'FEE', 30, 0],
        ['RECEIPT', '2026-01-05', 'WHT', 20, 0],
        ['TRANSFER', '2026-01-06', 'B1', 0, 200],
        ['TRANSFER', '2026-01-06', 'C1', 200, 0],
        ['REFUND', '2026-01-07', 'B1', 0, 100],
        ['REFUND', '2026-01-07', 'AR', 100, 0],
        ['PAYMENT', '2026-01-08', 'B1', 0, 300],
        ['PAYMENT', '2026-01-08', 'AP', 300, 0],
    ], columns=['voucher_id', 'date', 'account_code', 'debit', 'credit'])
    coa = pd.DataFrame([dict(account_code=code, category=category) for code, category in
        [('B1', ''), ('C1', ''), ('AR', 'Customers'), ('FEE', 'Charges'), ('WHT', 'Tax'), ('AP', 'Suppliers')]])
    accounts = {'B1': 'Bank', 'C1': 'Cash'}
    transactions = [dict(row, id=i+100, upload_id=1, source_fingerprint=str(i)) for i, row in enumerate(journal.to_dict('records'))]
    ledger = [dict(_canonical(i+1, row['date'], str(row['debit']), str(row['credit']), row['account_code']),
                   date=row['date'], voucher_id=row['voucher_id'])
              for i, row in enumerate(transactions) if row['account_code'] in accounts]
    def uploads(rows):
        return [dict(_uploaded(rows, opening=opening, start='2026-01-01', end='2026-02-28', identity=i,
                               code=code), comparison_roles_available=True)
                for i, (code, opening) in enumerate([('B1', '8000'), ('C1', '2000')], 1)]
    return dict(journal=journal, coa=coa, accounts=accounts, roles={'customer_receipt': ['Customers'],
        'bank_charge': ['Charges'], 'withholding_tax': ['Tax']},
        source=dict(uploads=uploads(ledger), ledger_rows=ledger, transaction_rows=transactions), refresh_uploads=uploads)


def _horizontal_summary(case, presentation='gross', grouping='month', end='2026-01-31'):
    from services.ledger_group_summary import build_cashflow_summary
    return build_cashflow_summary(case['journal'], case['coa'], ['C1'], ['B1'], '2026-01-01', end,
        receipt_presentation=presentation, cashflow_role_categories=case['roles'], group_by=grouping,
        include_reconciliation_evidence=True)


@pytest.mark.parametrize('presentation,expected', [('gross', (900, 350)), ('net', (850, 300))])
@pytest.mark.parametrize('view', ['combined', 'bank', 'cash', 'split'])
def test_horizontal_bridge_matches_independent_receipts_payments_and_balances(horizontal_reconciliation_case, presentation, expected, view):
    case = horizontal_reconciliation_case
    before = copy.deepcopy(case['source'])
    result = build_cashflow_reconciliation(_horizontal_summary(case, presentation), case['source'], case['accounts'], view)
    for kind, part in result['views'].items():
        p = part['periods'][0]
        assert part['amount_status'] == 'Matched'
        assert part['posting_comparison']['posting_status'] == 'Matched'
        assert p['receipts']['cashflow'] == (0 if kind == 'cash' else expected[0])
        assert p['payments']['cashflow'] == (0 if kind == 'cash' else expected[1])
        assert p['receipts']['difference'] == p['payments']['difference'] == 0
        assert p['balance_proof']['difference'] == p['balance_proof']['continuous_difference'] == 0
        assert p['balance_proof']['ledger_closing'] == {'combined':10550, 'bank':8350, 'cash':2200}[kind]
        assert part['overall_balance_proof']['calculated_closing'] == part['overall_balance_proof']['ledger_closing']
        assert part['overall_balance_proof']['difference'] == 0
        assert part['status'] == 'Review required'  # Extraction completeness remains uncertified.
    if 'combined' in result['views']:
        part = result['views']['combined']
        assert part['overall']['receipts']['ledger'] == 1150
        assert part['overall']['payments']['ledger'] == 600
        assert sum(r['total']['receipts'] for r in part['adjustments']) == expected[0] - 1150
        assert sum(r['total']['payments'] for r in part['adjustments']) == expected[1] - 600
    assert case['source'] == before
    assert not result['complete']


@pytest.mark.parametrize('presentation', ['net', 'gross'])
def test_horizontal_technical_offset_uses_original_gross_sides(horizontal_reconciliation_case, presentation):
    case = horizontal_reconciliation_case
    case['journal'].loc[0, ['debit','credit']] = [1000,50]
    case['source']['transaction_rows'][0].update(debit=1000, credit=50)
    case['source']['ledger_rows'][0].update(debit='1000', credit='50')
    case['source']['uploads'] = case['refresh_uploads'](case['source']['ledger_rows'])
    result = build_cashflow_reconciliation(_horizontal_summary(case,presentation),case['source'],case['accounts'])['views']['combined']
    assert result['amount_status'] == 'Matched'
    assert result['overall']['receipts']['ledger'] == 1200
    assert result['overall']['payments']['ledger'] == 650
    offsets = next(r for r in result['adjustments'] if r['reason']=='treasury_offsets')
    assert offsets['total']=={'receipts':-50,'payments':-50}


@pytest.mark.parametrize('outflow', ['0','25'])
def test_horizontal_source_only_postings_remain_separate_gross_differences(horizontal_reconciliation_case,outflow):
    case=horizontal_reconciliation_case
    case['source']['ledger_rows'].append(dict(_canonical(99,debit='25',credit=outflow),date='2026-01-05',voucher_id='EXTRA'))
    case['source']['uploads']=case['refresh_uploads'](case['source']['ledger_rows'])
    part=build_cashflow_reconciliation(_horizontal_summary(case),case['source'],case['accounts'])['views']['combined']
    assert part['amount_status']=='Differences'
    assert part['overall']['receipts']['difference']==25
    assert part['overall']['payments']['difference']==float(outflow)
    assert part['posting_comparison']['posting_status']=='Differences'
    assert all(e['voucher_id']!='EXTRA' for entries in build_cashflow_reconciliation(
        _horizontal_summary(case),case['source'],case['accounts'])['_support_entries'].values() for e in entries)


def test_horizontal_missing_ledger_never_becomes_zero(horizontal_reconciliation_case):
    case=horizontal_reconciliation_case
    case['source']['uploads']=[u for u in case['source']['uploads'] if u['ledger_account_code']!='C1']
    result=build_cashflow_reconciliation(_horizontal_summary(case),case['source'],case['accounts'],'split')
    assert result['views']['bank']['amount_status']=='Matched'
    for view in ('combined','cash'):
        assert result['views'][view]['amount_status']=='Unavailable'
        assert result['views'][view]['overall']['receipts']['ledger'] is None


@pytest.mark.parametrize('grouping',['day','fortnight','month','quarter','half_year','year'])
def test_horizontal_empty_subperiods_preserve_zero_and_total(horizontal_reconciliation_case,grouping):
    case=horizontal_reconciliation_case
    result=build_cashflow_reconciliation(_horizontal_summary(case,grouping=grouping,end='2026-02-28'),case['source'],case['accounts'])
    part=result['views']['combined']
    assert part['amount_status']=='Matched'
    assert part['overall']['receipts']['cashflow']==900
    assert part['overall']['payments']['cashflow']==350
    assert all(p['receipts']['difference']==p['payments']['difference']==0 for p in part['periods'])


@pytest.mark.parametrize('presentation,receipts,payments',[('net',77.35,20.65),('gross',79,22.30)])
@pytest.mark.parametrize('grouping',['month','year'])
def test_horizontal_independent_year_boundary_mixed_refunds_and_negative_sides(
        independent_year_boundary_case,presentation,receipts,payments,grouping):
    from services.ledger_group_summary import build_cashflow_summary
    case=independent_year_boundary_case
    transaction=case['journal'].to_dict('records')
    treasury=[r for r in transaction if r['account_code'] in case['accounts']]
    ledger=[dict(row,date=row['extract_date'],voucher_id=original['voucher_id'])
            for row,original in zip(case['rows'],treasury,strict=True)]
    source=dict(ledger_rows=ledger,transaction_rows=[dict(r,id=i+100) for i,r in enumerate(transaction)],
        uploads=[dict(u,comparison_roles_available=True) for u in case['uploads']])
    summary=build_cashflow_summary(case['journal'],case['coa'],['C1'],['B1','B0'],case['start'],case['end'],
        receipt_presentation=presentation,cashflow_role_categories=case['roles'],group_by=grouping,
        include_reconciliation_evidence=True)
    result=build_cashflow_reconciliation(summary,source,case['accounts'],'split')
    combined=result['views']['combined']
    assert combined['overall']['receipts']['cashflow']==receipts
    assert combined['overall']['payments']['cashflow']==payments
    for view,part in result['views'].items():
        assert part['amount_status']=='Matched',(view,part['periods'])
        assert part['posting_comparison']['posting_status']=='Matched'
        assert part['allocation_status']=='Review required'
        assert all(p['balance_proof']['difference']==p['balance_proof']['continuous_difference']==0 for p in part['periods'])
        assert part['balance_controls']['overall']['closing_total']==pytest.approx(float(case['expected'][view][-1][2]))


@pytest.mark.parametrize('invalid',['stale','roles','blank_amount','bad_date'])
def test_horizontal_invalid_evidence_is_not_a_matched_zero(horizontal_reconciliation_case,invalid):
    case=horizontal_reconciliation_case
    if invalid=='stale':case['source']['uploads'][0]['configuration_current']=False
    if invalid=='roles':case['source']['uploads'][0]['comparison_roles_available']=False
    if invalid=='blank_amount':case['source']['ledger_rows'][0].update(debit=None,credit=None)
    if invalid=='bad_date':case['source']['ledger_rows'][0]['date']='bad'
    part=build_cashflow_reconciliation(_horizontal_summary(case),case['source'],case['accounts'])['views']['combined']
    assert part['amount_status']=='Unavailable'
    assert part['overall']['receipts']['ledger'] is None
    assert part['overall']['payments']['difference'] is None


def test_horizontal_selected_total_rounds_original_precision_after_aggregation(horizontal_reconciliation_case):
    import pandas as pd
    case=horizontal_reconciliation_case
    case['journal']=pd.DataFrame([['A','2026-01-05','B1',.0049,0],['A','2026-01-05','AR',0,.0049],
        ['B','2026-02-05','B1',.0049,0],['B','2026-02-05','AR',0,.0049]],
        columns=['voucher_id','date','account_code','debit','credit'])
    transactions=[dict(row,id=i+100) for i,row in enumerate(case['journal'].to_dict('records'))]
    ledger=[dict(_canonical(i+1,row['date'],str(row['debit']),str(row['credit']),'B1'),
        date=row['date'],voucher_id=row['voucher_id']) for i,row in enumerate(transactions) if row['account_code']=='B1']
    case['source']=dict(ledger_rows=ledger,transaction_rows=transactions,uploads=case['refresh_uploads'](ledger))
    part=build_cashflow_reconciliation(_horizontal_summary(case,end='2026-02-28'),case['source'],case['accounts'])['views']['combined']
    assert [p['receipts']['ledger'] for p in part['periods']]==[0,0]
    assert part['overall']['receipts']['ledger']==part['overall']['receipts']['cashflow']==.01
    assert part['overall']['receipts']['difference']==0
    assert part['overall_balance_proof']['difference']==0


@pytest.mark.parametrize('change,expected', [('none', 'matched'), ('amount', 'changed'), ('date', 'changed'),
    ('repeat', 'changed'), ('ledger_only', 'ledger_only'), ('transaction_only', 'transaction_only'), ('blank', 'unresolved')])
def test_differences_compare_occurrences_without_guessing_pairs(change, expected):
    source = _difference_source()
    if change == 'amount': source['transaction_rows'][0]['debit'] = '49'
    if change == 'date': source['transaction_rows'][0]['date'] = '2026-02-01'
    if change == 'repeat': source['transaction_rows'].append(dict(source['transaction_rows'][0], id=101))
    if change == 'ledger_only': source['transaction_rows'] = []
    if change == 'transaction_only': source = _difference_source(ledger=[])
    if change == 'blank': source['transaction_rows'][0]['voucher_id'] = ''
    before = copy.deepcopy(source)
    result = build_cashflow_differences(source, {'B1': 'Bank'}, '2026-01-01', '2026-01-31')
    assert expected in result['counts']
    assert not result['complete']  # Matching stored postings cannot certify extraction completeness.
    assert result['transaction_coverage_status'] == 'Not independently certified'
    if change == 'date':
        row = result['findings'][0]
        assert row['difference_fields'] == ['dates']
        assert row['transaction_movement'] == 0
        assert row['transaction_lines'][0]['in_period'] is False
    if change == 'repeat':
        assert result['findings'][0]['matched_occurrences'] == 1
        assert result['findings'][0]['transaction_line_count'] == 2
    assert source == before


def test_differences_equal_net_cannot_hide_missing_offsetting_postings():
    ledger = [dict(_canonical(1), date='2026-01-05', voucher_id='V1'),
              dict(_canonical(2, debit='0', credit='50'), date='2026-01-05', voucher_id='V1')]
    result = build_cashflow_differences(_difference_source(ledger, []), {'B1': 'Bank'}, '2026-01-01', '2026-01-31')
    assert result['status'] == 'Differences'
    assert result['findings'][0]['movement_difference'] == 0
    assert result['findings'][0]['ledger_line_count'] == 2


@pytest.mark.parametrize('reason', ['configuration', 'roles', 'partial', 'missing', 'amount', 'date'])
def test_differences_insufficient_evidence_never_becomes_a_matched_zero(reason):
    source = _difference_source()
    if reason == 'configuration': source['uploads'][0]['configuration_current'] = False
    if reason == 'roles': source['uploads'][0]['comparison_roles_available'] = False
    if reason == 'partial': source['uploads'][0]['ledger_evidence']['balance_check']['source_mode'] = 'partial_extract'
    if reason == 'missing': source['uploads'] = []
    if reason == 'amount': source['transaction_rows'][0]['debit'] = 'NaN'
    if reason == 'date': source['transaction_rows'][0]['date'] = 'bad date'
    result = build_cashflow_differences(source, {'B1': 'Bank'}, '2026-01-01', '2026-01-31')
    assert result['status'] in ['Unavailable', 'Review required']
    assert not result['posting_comparison_complete']
    assert result['account_totals'][0]['movement_difference'] is None


def test_differences_keep_complete_counterparts_and_signed_erp_representations():
    source = _difference_source()
    source['transaction_rows'][0].update(debit='0', credit='-50')
    source['transaction_rows'].append(dict(id=101, account_code='AR', voucher_id='V1', date='2026-01-05', debit=0, credit=50))
    result = build_cashflow_differences(source, {'B1': 'Bank'}, '2026-01-01', '2026-01-31')
    assert result['status'] == 'Review required' and result['posting_status'] == 'Matched'
    assert result['findings'][0]['transaction_line_count'] == 1
    assert len(result['findings'][0]['voucher_lines']) == 2
    assert result['findings'][0]['transaction_lines'][0]['credit'] == '-50'
    assert 'ledger_evidence' not in result['ledger_sources'][0]
    assert 'not certified ERP extraction times' in result['timestamp_note']


def test_differences_do_not_collapse_reused_references_across_accounts_or_years():
    ledger = [dict(_canonical(1, day='2025-12-31', debit='10'),date='2025-12-31',voucher_id='V'),
              dict(_canonical(2, day='2026-01-01', debit='20'),date='2026-01-01',voucher_id='V')]
    source = _difference_source(ledger, [dict(r,id=r['id']+100) for r in ledger])
    source['uploads'][0] = _uploaded(ledger,start='2025-12-01',end='2026-01-31')
    source['uploads'][0]['comparison_roles_available']=True
    second = dict(_canonical(3,code='C1',debit='0',credit='7'),date='2026-01-05',voucher_id='V')
    source['ledger_rows'].append(second)
    source['transaction_rows'].append(dict(second,id=103,credit='8'))
    upload=_uploaded(source['ledger_rows'],code='C1',identity=2,opening='0')
    upload['comparison_roles_available']=True; source['uploads'].append(upload)
    result=build_cashflow_differences(source,{'B1':'Bank','C1':'Cash'},'2025-12-31','2026-01-05')
    assert result['counts']=={'matched':1,'unavailable':1}  # C1 lacks December coverage.
    bank=next(r for r in result['findings'] if r['account_code']=='B1')
    assert bank['ledger_line_count']==bank['transaction_line_count']==2
    assert bank['ledger_movement']==30
    assert len(bank['voucher_lines'])==3


@pytest.mark.parametrize('blank', [None, ''])
def test_differences_accept_a_blank_nonposting_side_but_not_two_missing_amounts(blank):
    source=_difference_source()
    source['transaction_rows'][0]['credit']=blank
    assert build_cashflow_differences(source,{'B1':'Bank'},'2026-01-01','2026-01-31')['posting_status']=='Matched'
    source['transaction_rows'][0]['debit']=blank
    result=build_cashflow_differences(source,{'B1':'Bank'},'2026-01-01','2026-01-31')
    assert result['status']=='Review required'
    assert result['findings'][0]['movement_difference'] is None


def test_uploaded_balances_infer_opening_and_keep_source_and_cashflow_arithmetic_separate():
    result = _resolve()
    period = result['periods'][0]
    account = period['accounts'][0]
    assert (account['opening_balance'], account['closing_balance']) == (150, 200)
    assert account['ledger_movement'] == 50
    assert period['calculated_closing'] == 200
    assert period['variance'] == 0
    assert result['status'] == 'Matched'
    assert len(result['evidence_fingerprint']) == 64
    difference = _resolve(flows={'2026-01': 40})
    assert difference['periods'][0]['closing_total'] == 200
    assert difference['periods'][0]['calculated_closing'] == 190
    assert difference['periods'][0]['variance'] == 10
    assert difference['status'] == 'Variance'


@pytest.mark.parametrize('opening', ['0', '-200', '100.15'])
def test_uploaded_balances_preserve_zero_credit_and_decimal_openings(opening):
    rows = [_canonical(debit='0.20', credit='0.10')]
    result = _resolve([_uploaded(rows, opening=opening)], rows, flows={'2026-01': Decimal('0.10')})
    account = result['periods'][0]['accounts'][0]
    assert account['opening_balance'] == float(opening)
    assert account['closing_balance'] == float(Decimal(opening) + Decimal('0.10'))
    assert result['status'] == 'Matched'


def test_uploaded_period_boundaries_exclude_prior_movement_from_selected_period():
    rows = [_canonical(1, '2026-01-01', '20'), _canonical(2, '2026-01-10', '50'),
            _canonical(3, '2026-01-20', '0', '30'), _canonical(4, '2026-01-31', '10')]
    periods = [dict(PERIOD, start='2026-01-10', end='2026-01-20')]
    result = _resolve([_uploaded(rows, opening='100')], rows, periods, {'2026-01': 20})
    account = result['periods'][0]['accounts'][0]
    assert (account['opening_balance'], account['closing_balance'], account['ledger_movement']) == (120, 140, 20)


def test_uploaded_boundary_carry_handles_inactive_days_and_empty_complete_reports():
    rows = [_canonical(day='2026-01-10')]
    periods = [dict(PERIOD, start='2026-01-11', end='2026-01-25')]
    result = _resolve([_uploaded(rows)], rows, periods, {'2026-01': 0})
    account = result['periods'][0]['accounts'][0]
    assert account['opening_balance'] == account['closing_balance'] == 200
    empty = _resolve([_uploaded([], opening='-10', source='inferred_from_closing')], [], flows={'2026-01': 0})
    assert empty['periods'][0]['closing_total'] == -10
    assert empty['status'] == 'Matched'


def test_uploaded_legitimate_repeated_lines_are_counted_once_per_canonical_occurrence():
    rows = [_canonical(1), _canonical(2)]
    result = _resolve([_uploaded(rows)], rows, flows={'2026-01': 100})
    assert result['periods'][0]['ledger_movement_total'] == 100
    assert result['periods'][0]['closing_total'] == 250


def test_uploaded_proof_version_matches_postgresql_coalesce_without_treating_zero_as_missing():
    rows = [_canonical()]
    rows[0]['version'] = 0
    assert _resolve([_uploaded(rows)], rows)['ready']


@pytest.mark.parametrize('mode,status', [('partial_extract', 'partial_or_incomplete'),
                                       ('replace_range', 'partial_or_incomplete'), ('replace_range', 'unordered')])
def test_uploaded_partial_or_ambiguous_order_cannot_prove_coverage(mode, status):
    rows = [_canonical()]
    upload = _uploaded(rows)
    upload['ledger_evidence']['balance_check'].update(source_mode=mode, status=status)
    result = _resolve([upload], rows)
    assert not result['ready']
    assert result['periods'][0]['opening_total'] is None
    assert result['periods'][0]['calculated_closing'] is None


def test_uploaded_gap_does_not_connect_reports_even_with_a_later_independent_opening():
    rows = [_canonical()]
    uploads = [_uploaded(rows, end='2026-01-10'),
               _uploaded(rows, start='2026-01-12', identity=2, opening='200')]
    result = _resolve(uploads, rows)
    assert not result['ready']
    assert any(d['code'] == 'ledger_coverage_gap' for d in result['diagnostics'])
    assert _resolve(uploads, rows, [dict(PERIOD, start='2026-01-12')], {'2026-01': 0})['ready']


def test_uploaded_contiguous_history_recomputes_stale_carried_balances_after_replacement():
    rows = [_canonical(1, '2026-01-05', '20'), _canonical(2, '2026-01-20', '10')]
    first = _uploaded(rows, end='2026-01-15')
    later = _uploaded(rows, start='2026-01-16', identity=2, opening='170', source='previous_coverage')
    rows[0].update(debit='30', version=2, source_fingerprint='changed')
    fresh = _uploaded(rows, end='2026-01-15', identity=3)
    later['ledger_evidence']['continuity_stale'] = True
    result = _resolve([first, later, fresh], rows, flows={'2026-01': 40})
    assert result['ready']
    assert result['periods'][0]['closing_total'] == 190
    assert result['periods'][0]['accounts'][0]['provenance']['opening']['anchor_upload_ids'] == [3]
    assert result['status'] == 'Matched'


def test_uploaded_overlapping_partial_replacement_can_reconstruct_backwards_without_double_counting():
    rows = [_canonical(1, '2026-01-05', '20'), _canonical(2, '2026-01-20', '10')]
    old = _uploaded(rows, opening='100')
    rows[1].update(debit='30', version=2, source_fingerprint='changed')
    fresh = _uploaded(rows, start='2026-01-15', end='2026-01-25', identity=2, opening='120')
    result = _resolve([old, fresh], rows)
    assert result['ready']
    assert result['periods'][0]['opening_total'] == 100
    assert result['periods'][0]['closing_total'] == 150
    assert result['periods'][0]['accounts'][0]['provenance']['opening']['anchor_upload_ids'] == [2]


@pytest.mark.parametrize('mutation', ['changed', 'deleted', 'added', 'missing_fingerprint'])
def test_uploaded_stale_or_missing_proof_cannot_support_a_cached_opening(mutation):
    rows = [_canonical()]
    upload = _uploaded(rows)
    if mutation == 'changed':
        rows[0].update(version=2, source_fingerprint='changed', debit='60')
    elif mutation == 'deleted':
        rows.clear()
    elif mutation == 'added':
        rows.append(_canonical(2))
    else:
        rows[0]['source_fingerprint'] = None
    result = _resolve([upload], rows)
    assert result['status'] == 'Unavailable'
    assert result['periods'][0]['closing_total'] is None
    assert any(d['code'] == 'ledger_proof_recomputed' for d in result['diagnostics'])


def test_uploaded_newer_complete_evidence_supersedes_old_opening_when_transactions_are_unchanged():
    rows = [_canonical()]
    old = _uploaded(rows, opening='100')
    fresh = _uploaded(rows, opening='150', identity=2)
    result = _resolve([old, fresh], rows)
    assert result['ready']
    assert result['periods'][0]['opening_total'] == 150


def test_uploaded_conflicting_contiguous_independent_anchors_are_not_silently_paired():
    rows = [_canonical()]
    uploads = [_uploaded(rows, end='2026-01-15'),
               _uploaded(rows, start='2026-01-16', identity=2, opening='999')]
    result = _resolve(uploads, rows)
    assert not result['ready']
    assert any(d['code'] == 'conflicting_ledger_anchors' for d in result['diagnostics'])


def test_uploaded_source_differences_cannot_be_hidden_by_an_earlier_good_anchor():
    rows = [_canonical()]
    first = _uploaded(rows, end='2026-01-15')
    disputed = _uploaded(rows, start='2026-01-16', identity=2, opening='200')
    disputed['ledger_evidence']['balance_check']['status'] = 'differences'
    result = _resolve([first, disputed], rows)
    assert not result['ready']
    assert result['periods'][0]['closing_total'] is None
    assert any(d['code'] == 'ledger_source_differences' for d in result['diagnostics'])


@pytest.mark.parametrize('change', ['configuration', 'bad_json', 'bad_opening', 'bad_amount', 'negative_debit', 'bad_date', 'missing_both_amounts'])
def test_uploaded_invalid_evidence_is_unavailable_instead_of_zero_or_an_exception(change):
    rows = [_canonical()]
    upload = _uploaded(rows)
    if change == 'configuration': upload['configuration_current'] = False
    elif change == 'bad_json': upload['ledger_evidence'] = '{invalid'
    elif change == 'bad_opening': upload['ledger_evidence']['balance_check']['opening'] = 'NaN'
    elif change == 'bad_amount': rows[0]['debit'] = 'NaN'
    elif change == 'negative_debit': rows[0]['debit'] = '-10'
    elif change == 'bad_date': rows[0]['extract_date'] = None
    else: rows[0].update(debit=None, credit=None)
    result = _resolve([upload], rows)
    assert result['status'] == 'Unavailable'
    assert not result['ready']


def test_uploaded_missing_account_blocks_totals_but_keeps_available_individual_balances():
    result = _resolve(accounts={'B1': 'Bank', 'C1': 'Cash'})
    period = result['periods'][0]
    assert period['accounts'][0]['closing_balance'] == 200
    assert period['accounts'][1]['closing_balance'] is None
    assert period['closing_total'] is None
    assert period['opening_total'] is None
    assert result['overall']['closing_total'] is None


def test_uploaded_known_opening_survives_a_report_end_beyond_available_ledger_coverage():
    rows = [_canonical()]
    result = _resolve([_uploaded(rows, end='2026-01-20')], rows)
    period = result['periods'][0]
    assert period['opening_total'] == 150
    assert period['closing_total'] is None
    assert period['calculated_closing'] == 200
    assert period['accounts'][0]['status'] == 'Partial'
    assert not result['ready']
    assert result['status'] == 'Unavailable'


def test_uploaded_known_closing_survives_a_report_start_before_available_ledger_coverage():
    rows = [_canonical(day='2026-01-20')]
    result = _resolve([_uploaded(rows, start='2026-01-10')], rows)
    period = result['periods'][0]
    assert period['opening_total'] is None
    assert period['closing_total'] == 200
    assert period['calculated_closing'] is None
    assert not result['ready']


def test_uploaded_separately_known_boundaries_do_not_certify_a_gap_as_complete():
    rows = [_canonical()]
    uploads = [_uploaded(rows, end='2026-01-10'), _uploaded(rows, start='2026-01-12', identity=2, opening='200')]
    result = _resolve(uploads, rows)
    period = result['periods'][0]
    assert period['opening_total'] == 150
    assert period['closing_total'] == 200
    assert period['ledger_movement_total'] is None
    assert period['status'] == result['status'] == 'Unavailable'
    assert not period['complete']


def test_uploaded_accounts_and_templates_cannot_cross_contaminate_each_other():
    rows = [_canonical(), _canonical(2, debit='90', code='OTHER')]
    upload = _uploaded(rows)
    other = _uploaded(rows, identity=2, code='OTHER', opening='9999')
    assert _resolve([upload, other], rows)['periods'][0]['closing_total'] == 200
    duplicate = copy.deepcopy(upload)
    duplicate.update(id=3, template_id=12)
    result = _resolve([upload, duplicate], rows)
    assert not result['ready']
    assert any(d['code'] == 'ambiguous_ledger_templates' for d in result['diagnostics'])


def test_uploaded_overall_uses_first_opening_and_last_closing_not_sum_of_openings():
    rows = [_canonical(), _canonical(2, '2026-02-10', '25')]
    upload = _uploaded(rows, end='2026-02-28')
    periods = [PERIOD, dict(PERIOD, key='2026-02', start='2026-02-01', end='2026-02-28')]
    result = _resolve([upload], rows, periods, {'2026-01': 50, '2026-02': 25})
    assert result['overall'] == dict(complete=True, opening_total=150, closing_total=225,
                                     cashflow_net_movement=75, calculated_closing=225, variance=0)
    assert result['periods'][1]['opening_total'] == result['periods'][0]['closing_total']


def test_uploaded_cashflow_variance_carries_forward_instead_of_resetting_to_ledger_opening():
    rows = [_canonical(), _canonical(2, '2026-02-10', '25')]
    upload = _uploaded(rows, end='2026-02-28')
    periods = [PERIOD, dict(PERIOD, key='2026-02', start='2026-02-01', end='2026-02-28')]
    result = _resolve([upload], rows, periods, {'2026-01': 40, '2026-02': 25})
    first, second = result['periods']
    assert first['calculated_opening'] == 150
    assert first['calculated_closing'] == second['calculated_opening'] == 190
    assert second['opening_total'] == 200
    assert second['calculated_closing'] == 215
    assert first['variance'] == second['variance'] == result['overall']['variance'] == 10


def test_uploaded_unknown_cashflow_does_not_become_zero_or_restart_running_calculations():
    rows = [_canonical(), _canonical(2, '2026-02-10', '25')]
    periods = [PERIOD, dict(PERIOD, key='2026-02', start='2026-02-01', end='2026-02-28')]
    result = _resolve([_uploaded(rows, end='2026-02-28')], rows, periods, {'2026-01': None, '2026-02': 25})
    assert result['coverage_complete']
    assert not result['ready']
    assert result['periods'][1]['opening_total'] == 200
    assert result['periods'][1]['calculated_opening'] is None
    assert result['periods'][1]['calculated_closing'] is None


def test_uploaded_missing_old_opening_can_be_reconstructed_after_contiguous_history_becomes_available():
    rows = [_canonical(1, '2026-01-05'), _canonical(2, '2026-01-20', '10')]
    first = _uploaded(rows, end='2026-01-15', identity=2)
    later = _uploaded(rows, start='2026-01-16', identity=1, opening='200', source='unavailable')
    later['ledger_evidence']['balance_check'].update(status='opening_unavailable', opening=None, closing=None)
    result = _resolve([first, later], rows, flows={'2026-01': 60})
    assert result['ready']
    assert result['periods'][0]['closing_total'] == 210


@pytest.mark.parametrize('grouping', ['day', 'fortnight', 'month', 'quarter', 'half_year', 'year'])
def test_uploaded_balances_reuse_actual_cashflow_engine_period_descriptors(grouping):
    import pandas as pd
    from services.ledger_group_summary import build_cashflow_summary
    dates = ['2026-02-10', '2026-03-15', '2026-04-18']
    journal = []
    rows = []
    for identity, day in enumerate(dates, 1):
        rows.append(_canonical(identity, day, '10'))
        journal.extend([
            dict(voucher_id=str(identity), date=day, account_code='B1', account_name='Bank', debit=10, credit=0, voucher_detail='Receipt'),
            dict(voucher_id=str(identity), date=day, account_code='AR', account_name='Customer', debit=0, credit=10, voucher_detail='Receipt'),
        ])
    coa = pd.DataFrame([dict(account_code='AR', account_name='Customer', category='Customers')])
    summary = build_cashflow_summary(pd.DataFrame(journal), coa, [], ['B1'], dates[0], dates[-1], group_by=grouping)
    flows = {p['key']: float(summary['category_totals'][p['key']].sum()) for p in summary['periods']}
    result = _resolve([_uploaded(rows, start='2026-02-01', end='2026-04-30')], rows, summary['periods'], flows)
    assert result['ready']
    assert result['status'] == 'Matched'
    assert result['overall']['opening_total'] == 150
    assert result['overall']['closing_total'] == 180
    assert result['overall']['calculated_closing'] == result['periods'][-1]['calculated_closing']
    assert [(p['start'], p['end']) for p in result['periods']] == [(p['start'], p['end']) for p in summary['periods']]


def test_uploaded_evidence_fingerprint_changes_without_modifying_inputs():
    rows = [_canonical()]
    uploads = [_uploaded(rows)]
    original = copy.deepcopy([uploads, rows])
    result = _resolve(uploads, rows)
    assert [uploads, rows] == original
    assert _resolve(uploads, rows)['evidence_fingerprint'] == result['evidence_fingerprint']
    uploads[0]['ledger_account_name'] = 'Renamed bank'
    assert _resolve(uploads, rows)['evidence_fingerprint'] != result['evidence_fingerprint']
    assert _resolve(uploads, rows, flows={'2026-01': 49})['evidence_fingerprint'] != result['evidence_fingerprint']


def test_uploaded_loader_scopes_queries_and_checks_exact_upload_configuration():
    rows = [_canonical()]
    upload = _uploaded(rows)
    roles = {'debit': 1, 'credit': 2}
    columns = [{'id': 1, 'column_name': 'Debit Amount', 'data_type': 'Numeric'},
               {'id': 2, 'column_name': 'Credit Amount', 'data_type': 'Numeric'}]
    upload.update(ledger_columns=json.dumps(roles), columns=columns)
    upload['ledger_evidence']['balance_check']['configuration'] = hashlib.sha256(json.dumps([roles, columns], sort_keys=True).encode()).hexdigest()
    upload['ledger_evidence'] = json.dumps(upload['ledger_evidence'])
    calls = []
    def query(sql, params):
        calls.append((sql, params))
        return [upload] if len(calls) == 1 else rows
    result = load_uploaded_ledger_balances(7, {'B1': 'Bank'}, [PERIOD], {'2026-01': 50}, query_fn=query)
    assert result['ready']
    assert len(calls) == 2
    assert calls[0][1] == (7, ['B1'])
    assert calls[1][1] == (7, ['B1'], [11])
    assert 'cd.is_deleted = FALSE' in calls[1][0]
    assert 'u.client_id = cd.client_id' in calls[1][0]
    assert "u.status = 'Imported'" in calls[1][0]
    assert 'row_data' not in result
    columns[0]['column_name'] = 'Changed after validation'
    calls.clear()
    result = load_uploaded_ledger_balances(7, {'B1': 'Bank'}, [PERIOD], {'2026-01': 50}, query_fn=query)
    assert not result['ready']
    assert len(calls) == 1


def test_split_balances_use_the_same_arithmetic_and_keep_signed_types_separate():
    rows = [_canonical(), _canonical(2, debit='0', credit='20', code='C1')]
    uploads = [_uploaded(rows), _uploaded(rows, code='C1', opening='100', identity=2)]
    result = build_uploaded_ledger_balances(uploads, rows, {'B1': 'Bank', 'C1': 'Cash'}, [PERIOD], {'2026-01': 30},
        treasury_movements={'bank': {'2026-01': 50}, 'cash': {'2026-01': -20}})
    bank, cash = (result['treasury_breakdown'][key] for key in ('bank', 'cash'))
    assert bank['overall']['closing_total'] == bank['overall']['calculated_closing'] == 200
    assert cash['overall']['closing_total'] == cash['overall']['calculated_closing'] == 80
    assert bank['overall']['closing_total'] + cash['overall']['closing_total'] == result['overall']['closing_total']
    assert cash['periods'][0]['cashflow_net_movement'] == -20
    assert bank['status'] == cash['status'] == result['status'] == 'Matched'
    assert [a['account_code'] for a in bank['periods'][0]['accounts']] == ['B1']
    assert [a['account_code'] for a in cash['periods'][0]['accounts']] == ['C1']


def test_split_missing_cash_ledger_does_not_hide_available_bank_or_invent_a_combined_total():
    rows = [_canonical()]
    result = build_uploaded_ledger_balances([_uploaded(rows)], rows, {'B1': 'Bank', 'C1': 'Cash'}, [PERIOD], {'2026-01': 50},
        account_names={'B1': 'Current Bank', 'C1': 'Cash Account'},
        treasury_movements={'bank': {'2026-01': 50}, 'cash': {'2026-01': 0}})
    assert result['overall']['closing_total'] is None
    assert result['treasury_breakdown']['bank']['overall']['closing_total'] == 200
    assert result['treasury_breakdown']['cash']['overall']['closing_total'] is None
    accounts = result['periods'][0]['accounts']
    assert accounts[0]['account_name'] == 'Bank Alpha'  # Uploaded historical name wins.
    assert accounts[1]['account_name'] == 'Cash Account'  # Missing ledger still has a genuine name.


def test_split_explicitly_unconfigured_type_is_zero_not_a_missing_uploaded_account():
    rows = [_canonical()]
    result = build_uploaded_ledger_balances([_uploaded(rows)], rows, {'B1': 'Bank'}, [PERIOD], {'2026-01': 50},
        treasury_movements={'bank': {'2026-01': 50}, 'cash': {'2026-01': 0}})
    cash = result['treasury_breakdown']['cash']
    assert cash['status'] == 'Not applicable'
    assert cash['overall']['opening_total'] == cash['overall']['closing_total'] == 0
    assert cash['periods'][0]['accounts'] == []
    assert cash['diagnostics'] == []


def test_split_cash_variance_carries_to_later_columns_without_affecting_bank():
    rows = [_canonical(), _canonical(2, credit='0', debit='20', code='C1'),
            _canonical(3, '2026-02-10', '10', code='C1')]
    uploads = [_uploaded(rows, end='2026-02-28'), _uploaded(rows, code='C1', opening='0', identity=2, end='2026-02-28')]
    periods = [PERIOD, dict(PERIOD, key='2026-02', start='2026-02-01', end='2026-02-28')]
    result = build_uploaded_ledger_balances(uploads, rows, {'B1': 'Bank', 'C1': 'Cash'}, periods,
        {'2026-01': 65, '2026-02': 10}, treasury_movements={
            'bank': {'2026-01': 50, '2026-02': 0}, 'cash': {'2026-01': 15, '2026-02': 10}})
    bank, cash = (result['treasury_breakdown'][key] for key in ('bank', 'cash'))
    assert bank['status'] == 'Matched'
    assert cash['periods'][0]['variance'] == cash['periods'][1]['variance'] == 5
    assert cash['periods'][1]['calculated_opening'] == 15
    assert cash['overall']['variance'] == result['overall']['variance'] == 5
