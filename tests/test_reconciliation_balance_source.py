from services.reconciliation_balance_source import (
    build_balance_source,
    load_reconciliation_balance_source,
    build_uploaded_ledger_balances,
    load_uploaded_ledger_balances,
)

import copy
import hashlib
import json
from decimal import Decimal
import pytest


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
