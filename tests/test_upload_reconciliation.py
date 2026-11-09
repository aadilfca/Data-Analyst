"""Source controls and SQL aggregation, without application/live database writes."""

from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZipFile
import pandas as pd
import pytest
from openpyxl import Workbook

from services import upload_reconciliation as controls
from routers import import_export as upload
from routers import data as databank


COLUMNS = [
    {'id': 1, 'column_name': 'PO reference', 'data_type': 'Text'},
    {'id': 2, 'column_name': 'Document', 'data_type': 'Text', 'reconciliation_role': 'document_id'},
    {'id': 3, 'column_name': 'Date', 'data_type': 'Date'},
    {'id': 4, 'column_name': 'Debit', 'data_type': 'Numeric', 'reconciliation_role': 'footing'},
    {'id': 5, 'column_name': 'Credit', 'data_type': 'Numeric', 'reconciliation_role': 'footing'},
]
MAPPING = {c['column_name']: c['column_name'] for c in COLUMNS}


def rows():
    return [
        {'PO reference': 'PO1', 'Document': 'A', 'Date': '01/01/2026', 'Debit': '0.10', 'Credit': 0},
        {'PO reference': 'PO1', 'Document': 'A', 'Date': '01/01/2026', 'Debit': '0.20', 'Credit': 0},
        {'PO reference': 'PO1', 'Document': 'B', 'Date': '01/01/2026', 'Debit': 0, 'Credit': '0.30'},
        {'PO reference': 'TOTAL', 'Document': None, 'Date': None, 'Debit': '0.30', 'Credit': '0.30'},
    ]


def request(**changes):
    return upload.ImportRequest(client_id=1, template_id=1, file_id=1,
        mappings=[upload.ColumnMapping(template_column_id=c['id'], template_column_name=c['column_name'],
                                       excel_column_name=c['column_name']) for c in COLUMNS], **changes)


def test_source_footer_excluded_and_configured_document_pivot_preserves_every_occurrence():
    frame = pd.DataFrame(rows())
    token = upload._source_row_numbers.set({})
    try:
        detail, diagnostics = upload._prepare_source_rows(frame, request(control_total_row=5, control_total_confirmed=True), COLUMNS, MAPPING)
        proof = upload._source_reconciliation(frame, detail, request(control_total_row=5, control_total_confirmed=True), COLUMNS, MAPPING)
        assert len(detail) == 3
        assert [upload._ledger_row_number(row, 0) for row in detail] == [2, 3, 4]
        assert diagnostics['excluded_rows'][0]['kind'] == 'confirmed_total'
        assert proof['status'] == 'matched'
        assert proof['full_sheet'] == {'row_count': 3, 'document_count': 2, 'missing_document_rows': 0,
                                       'footing_totals': {'Debit': '0.30', 'Credit': '0.30'}}
        # Two lines of A remain separate, and PO1 is not the document pivot.
        partial = upload._approved_reconciliation(proof, detail[:2], COLUMNS, MAPPING, request(), None)
        assert partial['approved_detail']['row_count'] == 2
        assert partial['approved_detail']['document_count'] == 1
        assert Decimal(partial['approved_detail']['footing_totals']['Credit']) == 0
        assert partial['scope']['complete_period'] is False
        assert partial['omitted_detail_rows'] == 1
        assert partial['full_sheet']['row_count'] == 3
    finally:
        upload._source_row_numbers.reset(token)


@pytest.mark.parametrize('row,confirmed', [(None, False), (5, False), (1, True), (6, True), (True, True)])
def test_missing_or_invalid_footer_choice_blocks(row, confirmed):
    with pytest.raises(ValueError, match='Confirm a valid'):
        controls.selected_control_row(rows(), COLUMNS, MAPPING, row, confirmed)


def test_neighboring_transaction_fields_do_not_disqualify_confirmed_footer_row():
    # The user-selected row is checked vertically through mapped Footing cells.
    # Its document, date and reference cells do not decide whether it is a total.
    assert controls.selected_control_row(rows(), COLUMNS, MAPPING, 2, True) == 2


def test_unlabelled_total_and_total_in_middle_are_explicit_choices_not_lost_transactions():
    source = rows()
    source[-1]['PO reference'] = ''
    source.insert(1, source.pop())
    assert controls.selected_control_row(source, COLUMNS, MAPPING, 3, True) == 3
    detail, _ = upload._prepare_source_rows(pd.DataFrame(source), request(control_total_row=3, control_total_confirmed=True), COLUMNS, MAPPING)
    assert len(detail) == 3


@pytest.mark.parametrize('value', [None, '', '=SUM(D2:D4)', '#DIV/0!', True, 'NaN', 'Infinity'])
def test_required_footer_must_have_real_saved_numeric_results(value):
    source = rows()
    source[-1]['Debit'] = value
    with pytest.raises(ValueError):
        controls.selected_control_row(source, COLUMNS, MAPPING, 5, True)


def test_missing_footing_mapping_cannot_be_bypassed_with_partial_extract():
    with pytest.raises(ValueError, match='Map the reconciliation field'):
        controls.selected_control_row(rows(), COLUMNS, {**MAPPING, 'Credit': None}, 5, True)


@pytest.mark.parametrize('excel_total,status,difference', [
    ('0.300000000000000001', 'rounding_tolerated', '-0.000000000000000001'),
    ('1.31', 'mismatch', '-1.01'),
])
def test_footing_variance_preserves_exact_values_and_only_blocks_over_limit(excel_total,status,difference):
    source = rows()
    source[-1]['Debit'] = excel_total
    original = [dict(row) for row in source]
    proof = controls.reconcile_source(source[-1], source[:-1], COLUMNS, MAPPING, 5, lambda row, fallback: fallback)
    assert proof['status'] == status
    assert Decimal(proof['checks'][0]['difference']) == Decimal(difference)
    assert proof['checks'][1]['status'] == 'matched'
    assert source == original
    if status == 'mismatch':
        response = upload._reconciliation_failure(proof, 3)
        assert response['can_import'] is False
        assert response['errors'][0]['row_number'] == 5
        assert len(response['errors'][0]['errors']) == 1


def test_exact_decimal_sum_exceeds_default_decimal_context_without_rounding():
    source = rows()[:2]
    source[0]['Debit'], source[1]['Debit'] = '1000000000000000000000000000000.01', '0.02'
    summary = controls.summarize_rows(source, COLUMNS, MAPPING)
    assert summary['footing_totals']['Debit'] == '1000000000000000000000000000000.03'


def test_exact_comparison_fingerprint_normalizes_numeric_encodings_without_rounding():
    columns = [{'column_name': 'Amount', 'data_type': 'Numeric'}]
    assert upload._exact_comparison_fingerprint({'Amount': 120}, columns) == \
        upload._exact_comparison_fingerprint({'Amount': 120.0}, columns) == \
        upload._exact_comparison_fingerprint({'Amount': '120.000'}, columns)
    assert upload._exact_comparison_fingerprint({'Amount': ''}, columns) != \
        upload._exact_comparison_fingerprint({'Amount': 0}, columns)
    assert upload._exact_comparison_fingerprint({'Amount': 'not-a-number'}, columns) == \
        upload._exact_comparison_fingerprint({'Amount': 'not-a-number'}, columns)
    assert upload._exact_comparison_fingerprint({'Amount': 'not-a-number'}, columns) != \
        upload._exact_comparison_fingerprint({'Amount': 'another-value'}, columns)
    assert upload._exact_comparison_fingerprint({'Amount': '120.0000001'}, columns) != \
        upload._exact_comparison_fingerprint({'Amount': '120'}, columns)
    assert upload._exact_comparison_fingerprint({'Amount': '1.00000000000000000000000000001'}, columns) != \
        upload._exact_comparison_fingerprint({'Amount': '1'}, columns)


def test_no_footing_template_retains_optional_controls_and_counts_repeated_lines():
    columns = [c for c in COLUMNS if c['data_type'] != 'Numeric']
    assert controls.selected_control_row(rows(), columns, MAPPING, None, False) is None
    summary = controls.summarize_rows(rows()[:3] * 2, columns, MAPPING)
    assert summary['row_count'] == 6 and summary['document_count'] == 2
    assert summary['footing_totals'] == {}
    with pytest.raises(ValueError, match='settings changed'):
        controls.selected_control_row(rows(), columns, MAPPING, 5, True)


@pytest.mark.parametrize('field,value', [('control_total_row', True), ('control_total_row', 2.5),
                                        ('control_total_row', '5'), ('control_total_confirmed', 'true')])
def test_request_choice_types_are_strict(field, value):
    with pytest.raises(ValueError):
        request(**{field: value})


def test_xlsx_missing_formula_cache_is_blocking_even_in_excluded_source_rows(tmp_path):
    path = tmp_path / 'source.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.title = 'Sheet1'
    sheet.append(list(MAPPING))
    for row in rows():
        sheet.append([row.get(name) for name in MAPPING])
    sheet.cell(2, 4, '=1/10')
    sheet.cell(5, 4, '=SUM(D2:D4)')
    book.save(path)
    frame = pd.read_excel(path)
    errors = upload._workbook_input_errors(path, request(excluded_rows=[2]), COLUMNS, MAPPING, frame)
    assert {e['row_number'] for e in errors} == {2, 5}
    assert all(e['errors'][0]['error_type'] == 'formula_result_missing' for e in errors)


def test_saved_excel_formula_result_reconciles_without_recalculating_or_modifying_source(tmp_path):
    path = tmp_path / 'calculated.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.title = 'Sheet1'
    sheet.append(list(MAPPING))
    for row in rows():
        sheet.append([row.get(name) for name in MAPPING])
    sheet.cell(5, 4, '=SUM(D2:D4)')
    buffer = BytesIO()
    book.save(buffer)
    # Simulate Excel's saved numeric cache; this is an isolated test workbook.
    with ZipFile(buffer) as original, ZipFile(path, 'w') as calculated:
        for info in original.infolist():
            content = original.read(info.filename)
            if info.filename == 'xl/worksheets/sheet1.xml':
                root = ElementTree.fromstring(content)
                ns = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                root.find(".//x:c[@r='D5']/x:v", ns).text = '0.3'
                content = ElementTree.tostring(root)
            calculated.writestr(info, content)
    before = path.read_bytes()
    req = request(control_total_row=5, control_total_confirmed=True)
    frame = pd.read_excel(path)
    assert not upload._workbook_input_errors(path, req, COLUMNS, MAPPING, frame)
    detail, _ = upload._prepare_source_rows(frame, req, COLUMNS, MAPPING)
    assert upload._source_reconciliation(frame, detail, req, COLUMNS, MAPPING)['status'] == 'matched'
    assert path.read_bytes() == before


def test_app_cell_edits_cannot_change_footing_controlled_source():
    req = request(control_total_row=5, control_total_confirmed=True,
                  edited_cells=[{'row_number': 2, 'excel_column': 'Debit', 'value': '999'}])
    frame = pd.DataFrame(rows())
    detail, _ = upload._prepare_source_rows(frame, req, COLUMNS, MAPPING)
    with pytest.raises(upload.HTTPException, match='Correct Footing-controlled uploads in Excel'):
        upload._source_reconciliation(frame, detail, req, COLUMNS, MAPPING)


def test_sql_controls_are_one_parameterized_scope_query_and_return_only_aggregates():
    calls = []
    columns = [dict(c, column_name="Document'); DROP TABLE client_data; --") if c['id'] == 2 else c for c in COLUMNS]
    def query(sql, params):
        calls.append((sql, params))
        return [{'row_count': 116, 'document_count': 29, 'missing_document_rows': 0,
                 'total_0': Decimal('77108960.00'), 'total_1': Decimal('77108960.00'), 'invalid_0': 0, 'invalid_1': 0}]
    summary = controls.aggregate_stored_controls(query, columns, 5, 9, '2026-01-01', '2026-01-01', ledger_account_code='100-1')
    assert summary['row_count'] == 116 and summary['document_count'] == 29
    assert summary['footing_totals'] == {'Debit': '77108960.00', 'Credit': '77108960.00'}
    sql, params = calls[0]
    assert len(calls) == 1
    assert 'DROP TABLE' not in sql and columns[1]['column_name'] in params
    assert (5, 9, '2026-01-01', '2026-01-01', '100-1') == params[6:11]
    assert params[1] == params[3] == params[5] == controls.SOURCE_WHITESPACE
    assert 'cd.is_deleted = FALSE' in sql and "cd.change_detection_status != 'deleted'" in sql
    assert 'ledger_account_code = %s' in sql and 'COUNT(DISTINCT document_id)' in sql
    assert sql.count('%s') == len(params)


def test_sql_invalid_values_or_reversed_dates_cannot_claim_reconciliation():
    def query(*args):
        return [{'row_count': 1, 'document_count': 1, 'missing_document_rows': 0,
                 'total_0': 0, 'total_1': 0, 'invalid_0': 1, 'invalid_1': 0}]
    with pytest.raises(ValueError, match='invalid amounts'):
        controls.aggregate_stored_controls(query, COLUMNS, 1, 1, '2026-01-01', '2026-01-01')
    with pytest.raises(ValueError, match='Start date'):
        controls.aggregate_stored_controls(query, COLUMNS, 1, 1, '2026-02-01', '2026-01-01')


def verification_inputs():
    summary = {'row_count': 116, 'document_count': 29, 'missing_document_rows': 0,
               'footing_totals': {'Debit': '77108960.01', 'Credit': '77108960.01'}}
    source = {'approved_detail': summary}
    scope = {'client_id': 5, 'template_id': 1, 'complete_period': True,
             'date_range': {'start': '2026-01-01', 'end': '2026-01-01'}}
    return source, scope, summary


def test_stored_controls_compare_exact_counts_and_each_footing_independently():
    source, scope, stored = verification_inputs()
    result = controls.stored_reconciliation(source, COLUMNS, scope, stored)
    assert result['status'] == 'matched'
    assert result['document_column'] == 'Document'
    altered = {**stored, 'footing_totals': {**stored['footing_totals'], 'Credit': '77108960.010000000001'}}
    mismatch = controls.stored_reconciliation(source, COLUMNS, scope, altered)
    assert mismatch['status'] == 'mismatch'
    assert Decimal(mismatch['rows'][-1]['difference']) == Decimal('0.000000000001')
    assert mismatch['rows'][-2]['matches'] is True


@pytest.mark.parametrize('metric,value', [('row_count', 117), ('document_count', 28)])
def test_matching_amounts_cannot_hide_different_row_or_document_counts(metric, value):
    source, scope, stored = verification_inputs()
    result = controls.stored_reconciliation(source, COLUMNS, scope, {**stored, metric: value})
    assert result['status'] == 'mismatch'


def test_missing_values_partial_scopes_and_absent_source_are_never_certified():
    source, scope, stored = verification_inputs()
    assert controls.stored_reconciliation(source, COLUMNS, {**scope, 'complete_period': False}, stored)['status'] == 'not_comparable'
    assert controls.stored_reconciliation(source, COLUMNS, scope, {**stored, 'missing_document_rows': 1})['status'] == 'incomplete'
    assert controls.stored_reconciliation(None, COLUMNS, scope, stored)['status'] == 'unavailable'
    assert controls.stored_reconciliation(source, COLUMNS, scope, unavailable=True)['status'] == 'unavailable'


def test_no_footing_configuration_reports_count_controls_without_inventing_zero_totals():
    source, scope, stored = verification_inputs()
    result = controls.stored_reconciliation(source, [c for c in COLUMNS if c['data_type'] != 'Numeric'], scope, stored)
    assert result['status'] == 'matched' and not result['footing_configured']
    assert len(result['rows']) == 2
    assert 'not configured' in result['message']


def test_missing_date_scope_never_runs_an_unbounded_database_aggregate():
    source, scope, stored = verification_inputs()
    def query(*args):
        pytest.fail('Missing period must not query all current data')
    assert controls.verify_stored_controls(query, source, COLUMNS, {**scope, 'date_range': None})['status'] == 'unavailable'


@pytest.fixture
def preview_metrics_backend(monkeypatch):
    state = {'calls': [], 'snapshot': False, 'snapshots': 0, 'columns': COLUMNS,
             'total': 116, 'invalid': False}
    def snapshot(callback):
        state['snapshots'] += 1
        state['snapshot'] = True
        try: return callback(object())
        finally: state['snapshot'] = False
    def query(sql, params=None, **kwargs):
        state['calls'].append((sql, params, state['snapshot']))
        if 'FROM clients' in sql: return [{'id': 5}]
        if 'FROM templates' in sql: return [{'id': 9, 'template_name': 'Report', 'data_purpose': 'account_ledger'}]
        if 'FROM template_column_mappings' in sql: return state['columns']
        if 'FROM audit_rule_documents' in sql: return [{'id': 3, 'template_id': 9}]
        if 'FROM uploaded_files' in sql and 'FROM client_data' not in sql: return [{'id': 99}]
        if sql.startswith('WITH source AS'):
            return [{'row_count': state['total'], 'document_count': 29 if state['total'] else 0,
                     'missing_document_rows': 0, 'total_0': Decimal('77108960') if state['total'] else Decimal(0),
                     'total_1': Decimal('77108960') if state['total'] else Decimal(0),
                     'invalid_0': 1 if state['invalid'] else 0, 'invalid_1': 0}]
        if 'COUNT(*) as total' in sql: return [{'total': state['total']}]
        if 'sunday_count' in sql: return [{'sunday_count': 0}]
        if 'LIMIT %s OFFSET %s' in sql:
            return [{'id': 1, 'row_data': {'Document': 'A', 'Debit': 1}, 'extract_amount': 1,
                     'extract_date': '2026-01-01', 'data_quality_flag': None, 'rate_source': None}]
        raise AssertionError(sql)
    monkeypatch.setattr(databank.database, 'run_read_snapshot_pg', snapshot)
    monkeypatch.setattr(databank, 'execute_query_pg', query)
    monkeypatch.setattr(databank, 'attach_history_flags', lambda rows: None)
    monkeypatch.setattr(databank, 'attach_assignment_flags', lambda rows: None)
    def read(**kwargs):
        defaults = dict(client_id=5, template_id=9, date_start='2026-01-01', date_end='2026-01-31',
            page=1, limit=25, search=None, amount_min=None, amount_max=None, sunday_only=False,
            data_quality=None, document_id=None, ledger_account_code=None, include_metrics=True, perf=False)
        return databank.get_preview_rows(**{**defaults, **kwargs})
    state['read'] = read
    return state


def test_preview_metrics_cover_full_filtered_scope_in_same_snapshot_without_extra_count(preview_metrics_backend):
    state = preview_metrics_backend
    result = state['read'](search="O'Reilly", amount_min=1.5, amount_max=900, sunday_only=True,
                           data_quality='complete', ledger_account_code='100-1', page=4, limit=25)
    assert result['total_rows'] == result['preview_metrics']['row_count'] == 116
    assert result['preview_metrics']['document_count'] == 29
    assert result['preview_metrics']['footing_totals'] == {'Debit': '77108960', 'Credit': '77108960'}
    aggregates = [(sql, params) for sql, params, _ in state['calls'] if sql.startswith('WITH source AS')]
    assert len(aggregates) == 1
    sql, params = aggregates[0]
    assert 'LIMIT' not in sql and 'OFFSET' not in sql
    assert "O'Reilly" not in sql and "%O'Reilly%" in params
    for fragment in ('ledger_account_code = %s', 'cd.extract_amount >= %s', 'cd.extract_amount <= %s',
                     'EXTRACT(DOW FROM cd.extract_date) = 0', 'cd.data_quality_flag IS NULL'):
        assert fragment in sql
    assert not any('COUNT(*) as total' in sql for sql, _, _ in state['calls'])
    assert state['snapshots'] == 1 and all(in_snapshot for _, _, in_snapshot in state['calls'])
    assert result['preview_metrics']['scope']['ledger_account_code'] == '100-1'


def test_preview_zero_results_and_absent_footing_are_explicit(preview_metrics_backend):
    state = preview_metrics_backend
    state['total'] = 0
    empty = state['read']()
    assert empty['rows'] == [] and empty['preview_metrics']['document_count'] == 0
    assert empty['preview_metrics']['footing_totals'] == {'Debit': '0', 'Credit': '0'}
    state['columns'] = [c for c in COLUMNS if c['data_type'] != 'Numeric']
    unconfigured = state['read']()
    assert unconfigured['preview_metrics']['footing_totals'] == {}
    assert unconfigured['preview_metrics']['footing_configured'] is False


def test_invalid_stored_amount_keeps_rows_but_never_shows_a_false_zero_footing(preview_metrics_backend):
    state = preview_metrics_backend
    state['invalid'] = True
    result = state['read']()
    assert result['total_rows'] == 116 and len(result['rows']) == 1
    assert result['preview_metrics']['status'] == 'unavailable'
    assert 'footing_totals' not in result['preview_metrics']


def test_metrics_opt_out_preserves_ordinary_preview_callers(preview_metrics_backend):
    state = preview_metrics_backend
    result = state['read'](include_metrics=False)
    assert 'preview_metrics' not in result and 'column_order' not in result
    assert state['snapshots'] == 0
    assert not any(sql.startswith('WITH source AS') for sql, _, _ in state['calls'])


def test_preview_http_query_enables_one_snapshot_and_exact_json_totals(preview_metrics_backend):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(databank.router)
    with TestClient(app) as client:
        response = client.get('/api/data/preview', params={
            'client_id': 5, 'template_id': 9, 'date_start': '2026-01-01',
            'date_end': '2026-01-31', 'include_metrics': 'true', 'limit': 25})
    assert response.status_code == 200
    assert response.json()['preview_metrics']['footing_totals']['Debit'] == '77108960'
    assert preview_metrics_backend['snapshots'] == 1


def test_restricted_document_preview_does_not_expose_hidden_footing_metadata(preview_metrics_backend, monkeypatch):
    monkeypatch.setattr(databank, 'build_verification_field_policy', lambda *args: {
        'configured': True, 'core_fields': [{'available': True, 'column_name': 'Document'}], 'optional_fields': []})
    state = preview_metrics_backend
    result = state['read'](document_id=3)
    assert result['preview_metrics']['status'] == 'unavailable'
    assert result['column_order'] == ['Document']
    assert 'footing_totals' not in result['preview_metrics']
    assert not any(sql.startswith('WITH source AS') for sql, _, _ in state['calls'])
