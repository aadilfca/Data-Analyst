import os
import tempfile
import unittest
from unittest.mock import patch

import pytest

import database
from database import execute_query, execute_write, init_db
from routers.import_export import (
    ColumnMapping,
    ImportRequest,
    _build_validation_plan_payload,
    _build_upload_storage_path,
    _canonical_source_identity,
    _canonical_source_identities,
    _is_duplicate_upload_noop,
    _strict_duplicate_row_errors,
    _validation_request_signature,
    build_change_report,
    compare_transactions,
    count_new_and_existing_rows,
)


@pytest.mark.parametrize('action', ['new', 'changed'])
@pytest.mark.parametrize('date_kind', ['native', 'text', 'serial'])
def test_validated_calendar_date_survives_json_plan_and_actual_import_batches(action, date_kind):
    """Native Excel dates must not be reinterpreted by the text-date picker."""
    import json
    from contextlib import ExitStack, nullcontext
    from datetime import datetime
    from routers import import_export as uploads

    columns = [{'id': i, 'column_name': name, 'data_type': kind} for i, (name, kind) in enumerate([
        ('Voucher ID', 'Text'), ('Transaction Date', 'Date'), ('Account Detail', 'Text'),
        ('Voucher Detail', 'Text'), ('Debit Amount', 'Numeric'), ('Credit Amount', 'Numeric'),
    ], 1)]
    source_date = {'native': datetime(2026, 4, 1), 'text': '04-01-2026', 'serial': 46113}[date_kind]
    source = {'Voucher ID': 'V1', 'Transaction Date': source_date, 'Account Detail': 'A',
              'Voucher Detail': 'Corrected supplies', 'Debit Amount': 100.0, 'Credit Amount': 0.0}
    old = {**source, 'Transaction Date': '01-04-2026', 'Voucher Detail': 'Old supplies'}
    existing = [{'id': 8, 'row_data': old, 'version': 1, 'source_identity': 'original-8',
                 'extract_date': '2026-04-01'}] if action == 'changed' else []
    mapping = {c['column_name']: c['column_name'] for c in columns}
    with patch.object(uploads, 'execute_query_pg', return_value=existing):
        changes = uploads.compare_transactions(5, 1, columns, [source], mapping, {},
                                               date_delimiter='-', date_order='MDY')
    payload = uploads._build_validation_plan_payload(changes, columns, 1, 1, [], 0, None, '-', 'MDY')
    plan = json.loads(json.dumps(payload, default=str))
    assert plan[action][0]['validated_date'] == '2026-04-01'
    request = ImportRequest(client_id=5, template_id=1, file_id=99, mappings=[], validation_plan_id='date-plan')
    captured, classifications = [], []

    def query(sql, params=None, **kwargs):
        if 'FROM templates' in sql:
            return [{'id': 1, 'template_name': 'Transactions', 'rate_source': None}]
        if 'FROM uploaded_files' in sql:
            return [{'file_path': 'unused.xlsx', 'file_name': 'unused.xlsx', 'status': 'Validated'}]
        if 'FROM clients' in sql:
            return [{'id': 5}]
        if 'FROM template_column_mappings' in sql:
            return columns
        if ('FROM client_data' in sql and 'cd.id = ANY' in sql) or 'FOR UPDATE' in sql:
            return existing
        return []

    def insert(conn, sql, rows, **kwargs):
        captured.append(('insert', rows))
        return [101]

    def batch(conn, sql, rows):
        if 'UPDATE client_data' in sql:
            captured.append(('update', rows))

    with ExitStack() as stack:
        for name, replacement in {
            'execute_query_pg': query, 'execute_write_pg': lambda *a: None,
            '_load_validation_plan': lambda *a: {'plan_json': plan},
            'pg_transaction': lambda: nullcontext(object()),
            '_check_upload_storage_budget': lambda *a: None,
            '_assert_active_identity_collisions': lambda *a: None,
            'load_treasury_mapping_postgres': lambda *a: {},
            'execute_batch_insert_with_ids_pg': insert, 'execute_batch_pg': batch,
            'prepare_lifecycle': lambda *a: None,  # Archive transport is covered by lifecycle integration tests.
            'upsert_classification_rows_postgres': lambda conn, rows, *a, **kw: classifications.extend(rows),
            'resolve_upload_occurrences_postgres': lambda *a, **kw: {'eligible': True},
            '_persist_upload_summary': lambda *a, **kw: None, 'log_audit': lambda *a: None,
        }.items():
            stack.enter_context(patch.object(uploads, name, replacement))
        try:
            result = uploads._import_validation_plan(request)
        finally:
            uploads.IMPORT_PROGRESS.pop(99, None)
    assert result['status'] == 'success'
    assert len(captured) == len(classifications) == 1
    operation, rows = captured[0]
    assert rows[0][5 if operation == 'insert' else 6] == '2026-04-01'
    assert classifications[0]['extract_date'] == '2026-04-01'
    if date_kind == 'native':
        assert classifications[0]['row_data']['Transaction Date'] == '01/04/2026'
    if action == 'changed':
        assert result['change_detection']['report'][0]['changed_fields'] == ['Voucher Detail']


@pytest.fixture
def upload_formatting_page():
    """Real upload scripts with local assets and intercepted APIs; no server or DB."""
    from pathlib import Path
    from urllib.parse import urlparse
    from playwright.sync_api import sync_playwright
    root = Path(__file__).resolve().parents[1]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(request):
            path = urlparse(request.request.url).path
            if path == '/static/client_context.js':
                request.fulfill(content_type='application/javascript', body='''
                    const API_BASE = '';
                    window.isClientSelected = () => true;
                    window.getSelectedClientId = () => 5;
                    window.getSelectedClientName = () => 'Test client';
                    window.getSelectedClientCode = () => 'TEST';
                ''')
            elif path in ['/static/upload.html', '/static/upload.js', '/static/style.css', '/static/vendor/xlsx.full.min.js', '/static/loading_ui.js']:
                request.fulfill(path=str(root / path.lstrip('/')))
            elif path == '/api/templates':
                request.fulfill(json=[])
            elif path in ['/api/upload/validate', '/api/upload/import']:
                request.fulfill(status=409, json={'detail': "Voucher '<b>V1</b>', account 'A': ambiguous repeated lines"})
            else:
                request.fulfill(body='', content_type='text/css' if path.endswith('.css') else 'text/plain')

        page.route('**/*', route)
        page.goto('http://upload.test/static/upload.html', wait_until='load')
        page.wait_for_function("typeof formatUploadPreviewCell === 'function'")
        yield page
        assert errors == []
        browser.close()


def test_upload_grids_show_serial_dates_and_preserve_raw_values_and_text_order(upload_formatting_page):
    page = upload_formatting_page
    result = page.evaluate('''() => {
        currentMappings = [
            {column_id: 1, column_name: 'Voucher', data_type: 'Text', excel_column: 'Voucher'},
            {column_id: 2, column_name: 'Transaction Date', data_type: 'Date', excel_column: 'Date'},
            {column_id: 3, column_name: 'Amount', data_type: 'Numeric', excel_column: 'Amount'}
        ];
        const sheet = XLSX.utils.aoa_to_sheet([
            ['Voucher', 'Date', 'Amount'], ['V1', 46113, 46113], ['V2', '01/04/2026', 0], ['V3', '', 10]
        ]);
        sheet.B2.z = 'mm-dd-yy';
        const workbook = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(workbook, sheet, 'Transactions');
        workbookData = XLSX.write(workbook, {bookType: 'xlsx', type: 'array'});
        currentSheet = 'Transactions';
        renderRowExclusionGrid();
        showPreview();
        return {raw: rowExclusionAllRows[0][1], date: formatUploadPreviewCell(46113, 'Date'),
            amount: formatUploadPreviewCell(46113, 'Amount'), text: formatUploadPreviewCell('01/04/2026', 'Date')};
    }''')
    assert result == {'raw': 46113, 'date': '01/04/2026', 'amount': 46113, 'text': '01/04/2026'}
    assert page.locator('#rowExclusionBody tr').first.locator('td').nth(3).inner_text() == '01/04/2026'
    assert page.locator('#previewSheetBody tr').first.locator('td').nth(2).inner_text() == '01/04/2026'
    assert page.locator('#previewSheetBody tr').nth(1).locator('td').nth(2).inner_text() == '01/04/2026'
    # A different workbook epoch must use Excel's own decoding, independent of timezone.
    assert page.evaluate("() => { uploadWorkbookDate1904 = true; return formatUploadPreviewCell(44651, 'Date'); }") == '01/04/2026'


def test_validation_conflict_is_visible_as_text_and_discards_stale_plan(upload_formatting_page):
    page = upload_formatting_page
    page.evaluate('''() => {
        fileId = 99; selectedTemplateId = 1; currentSheet = 'Transactions';
        comparisonModeDropdown.value = 'correction';
        excelHeaders = ['Voucher'];
        currentMappings = [{column_id: 1, column_name: 'Voucher', data_type: 'Text', excel_column: 'Voucher', is_matched: true}];
        renderMapping(currentMappings);
        leftPanel.style.display = 'block'; mappingPanel.style.display = 'block';
        modeActions.style.display = 'block';
        validationPlanId = 'stale-plan'; validationStatus = 'idle';
        importBtn.disabled = false;
    }''')
    page.locator('#importBtn').click()
    page.wait_for_function("validationStatus === 'invalid'")
    assert "Voucher '<b>V1</b>', account 'A'" in page.locator('#statusMessage').inner_text()
    assert page.locator('#statusMessage b').count() == 0
    assert page.evaluate('validationPlanId') is None
    assert page.locator('#importBtn').inner_text() == 'Validate'
    # The Import handler must surface the same ordinary FastAPI detail safely.
    page.evaluate("async () => { validationPlanId = 'stale-plan'; await performImport({file_id: 99}); }")
    assert "Import error: Voucher '<b>V1</b>'" in page.locator('#statusMessage').inner_text()
    assert page.locator('#statusMessage b').count() == 0
    assert page.evaluate('validationPlanId') is None
    assert page.evaluate("uploadFailureMessage({error: {http_detail: 'Legacy conflict'}}, 'Fallback')") == 'Legacy conflict'
    assert page.evaluate("uploadFailureMessage({detail: [{msg: 'Required mapping'}]}, 'Fallback')") == 'Required mapping'


def test_complete_report_requires_coverage_and_sends_exact_dates(upload_formatting_page):
    page = upload_formatting_page
    sent = []
    page.route('**/api/upload/validate', lambda route: (
        sent.append(route.request.post_data_json),
        route.fulfill(json={'total_rows': 1, 'can_import': True, 'validation_plan_id': 'ready-plan',
                            'change_detection': {'new_count': 1, 'missing_count': 1}}),
    ))
    page.evaluate('''() => {
        fileId = 99; selectedTemplateId = 1; currentSheet = 'Transactions';
        excelHeaders = ['Voucher'];
        currentMappings = [{column_id: 1, column_name: 'Voucher', data_type: 'Text', excel_column: 'Voucher', is_matched: true}];
        renderMapping(currentMappings);
        leftPanel.style.display = 'block'; mappingPanel.style.display = 'block';
        modeActions.style.display = 'block'; importBtn.disabled = false;
    }''')
    page.locator('#importBtn').click()
    assert 'coverage start/end' in page.locator('#statusMessage').inner_text()
    assert sent == []
    page.locator('#validationErrorCloseBtn').click()
    page.locator('#uploadCoverageStart').fill('2026-01-01')
    page.locator('#uploadCoverageEnd').fill('2026-10-25')
    page.locator('#uploadConfirmEmpty').check()
    page.locator('#importBtn').click()
    page.wait_for_function("validationStatus === 'valid'")
    assert len(sent) == 1
    assert {k: sent[0][k] for k in ['comparison_mode', 'coverage_start', 'coverage_end', 'confirm_empty_period']} == {
        'comparison_mode': 'replace_range', 'coverage_start': '2026-01-01', 'coverage_end': '2026-10-25', 'confirm_empty_period': True,
    }
    page.locator('#uploadCoverageEnd').fill('2026-10-26')
    page.locator('#uploadCoverageEnd').dispatch_event('change')
    assert page.evaluate('validationPlanId') is None
    page.locator('#comparisonModeDropdown').select_option('correction')
    assert page.locator('#uploadCoverageFields').is_hidden()
    page.locator('#comparisonModeDropdown').select_option('replace_range')
    assert page.locator('#uploadCoverageFields').is_visible()


def test_replacement_popup_cancel_escape_and_confirm_only_send_on_approval(upload_formatting_page):
    page = upload_formatting_page
    imports = []
    page.route('**/api/upload/import', lambda route: (
        imports.append(route.request.post_data_json), route.fulfill(status=503, json={'detail': 'Archive unavailable'}),
    ))
    page.route('**/api/upload/progress/99', lambda route: route.fulfill(json={'status': 'archiving'}))
    for choice in ['cancel', 'escape', 'confirm']:
        page.evaluate('''() => {
            lastChangePreview = {unchanged_count: 10, new_count: 2, changed_count: 0, missing_count: 3};
            validationPlanId = 'ready-plan'; validationStatus = 'valid';
            window.reviewFinished = false;
            performImport({file_id: 99, comparison_mode: 'replace_range', coverage_start: '2026-04-01',
                coverage_end: '2026-04-30', validation_plan_id: 'ready-plan'}).then(() => window.reviewFinished = true);
        }''')
        assert page.locator('#uploadReplacementReview').is_visible()
        assert '2026-04-01 through 2026-04-30' in page.locator('#uploadReplacementScope').inner_text()
        assert '3 superseded/removed' in page.locator('#uploadReplacementCounts').inner_text()
        assert imports == []
        if choice == 'escape':
            page.keyboard.press('Escape')
        else:
            page.locator('#uploadReplacement' + choice.title()).click()
        page.wait_for_function('window.reviewFinished')
        assert page.locator('#uploadReplacementReview').is_hidden()
    assert len(imports) == 1
    assert imports[0]['validation_plan_id'] == 'ready-plan'
    assert 'Archive unavailable' in page.locator('#statusMessage').inner_text()
    assert page.evaluate('validationPlanId') == 'ready-plan'


def test_review_identifies_date_corrections_safely_and_explains_empty_checkbox(upload_formatting_page):
    page = upload_formatting_page
    assert 'This report contains no transactions—clear the declared period' in page.locator('#uploadConfirmEmpty').locator('..').text_content()
    page.evaluate('''() => {
        lastChangePreview = {unchanged_count: 0, new_count: 2, missing_count: 2,
            changed_voucher_count: 1, removed_voucher_count: 0,
            date_moves: [{voucher_id: '<b>V1</b>', previous_dates: ['2026-03-31'], incoming_dates: ['2026-04-15']}]};
        window.dateReviewClosed = false;
        reviewUploadReplacement({coverage_start: '2026-04-01', coverage_end: '2026-04-30'})
            .then(() => window.dateReviewClosed = true);
    }''')
    assert page.locator('#uploadReplacementDateMoves').is_visible()
    assert '<b>V1</b>: 2026-03-31 → 2026-04-15' in page.locator('#uploadReplacementDateMoves').inner_text()
    assert page.locator('#uploadReplacementDateMoves b').count() == 0
    assert '1 changed vouchers' in page.locator('#uploadReplacementCounts').inner_text()
    page.locator('#uploadReplacementCancel').click()
    page.wait_for_function('window.dateReviewClosed')


class UploadValidationCountTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        database.DB_NAME = os.path.join(self.tmpdir.name, "test.db")
        init_db()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_same_name_uploads_use_distinct_storage_paths(self):
        first_path = _build_upload_storage_path('All voucher one fine.xlsx')
        second_path = _build_upload_storage_path('All voucher one fine.xlsx')

        self.assertNotEqual(first_path, second_path)
        self.assertEqual(os.path.basename(first_path).split('_', 1)[1], 'All voucher one fine.xlsx')
        self.assertEqual(os.path.basename(second_path).split('_', 1)[1], 'All voucher one fine.xlsx')

    def test_counts_existing_and_new_rows(self):
        execute_write(
            "INSERT INTO client_data (client_id, template_id, upload_id, row_data, created_by, version) VALUES (?, ?, ?, ?, ?, ?)",
            (1, 1, 1, '{"Voucher":"ABC","Amount":100}', "Analyst", 1),
        )

        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        mapping_dict = {
            1: MappingStub("Voucher"),
            2: MappingStub("Amount"),
        }

        rows = [
            {"Voucher": "ABC", "Amount": 100},
            {"Voucher": "XYZ", "Amount": 200},
        ]

        # The count helper reads Postgres in production, while the fixture row above
        # is intentionally created in isolated SQLite. Stub only that external read
        # so the comparison logic itself remains under test.
        with patch(
            "routers.import_export.execute_query_pg",
            return_value=[{"row_data": {"Voucher": "ABC", "Amount": 100}}],
        ):
            new_count, existing_count = count_new_and_existing_rows(
                1, 1, rows, template_cols, mapping_dict
            )

        self.assertEqual(new_count, 1)
        self.assertEqual(existing_count, 1)


    def test_uses_set_based_counts_when_postgres_returns_counts(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        rows = [{"Voucher": "ABC", "Amount": 100}, {"Voucher": "XYZ", "Amount": 200}]
        mapping_dict = {1: MappingStub("Voucher"), 2: MappingStub("Amount")}
        with patch(
            "routers.import_export.execute_query_pg",
            return_value=[{"new_count": 1, "existing_count": 1}],
        ) as query:
            counts = count_new_and_existing_rows(1, 1, rows, template_cols, mapping_dict)

        self.assertEqual(counts, (1, 1))
        query.assert_called_once()

    def test_current_row_comparison_classifies_unchanged_changed_and_added(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        mapping_dict = {1: MappingStub("Voucher"), 2: MappingStub("Amount")}
        existing = [
            {"id": 10, "row_data": {"Voucher": "UNCHANGED", "Amount": 100}, "version": 4,
             "extract_date": None, "validation_status": "Verified", "verification_detail_id": 1},
            {"id": 11, "row_data": {"Voucher": "CHANGED", "Amount": 100}, "version": 2,
             "extract_date": None, "validation_status": "Pending", "verification_detail_id": None},
        ]
        incoming = [
            {"Voucher": "UNCHANGED", "Amount": 100},
            {"Voucher": "CHANGED", "Amount": 125},
            {"Voucher": "ADDED", "Amount": 50},
        ]

        with patch("routers.import_export.execute_query_pg", return_value=existing):
            result = compare_transactions(
                1, 1, template_cols, incoming, {"Voucher": "Voucher", "Amount": "Amount"},
                mapping_dict,
            )

        self.assertEqual(result["unchanged_count"], 1)
        self.assertEqual(result["changed_count"], 1)
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["missing_count"], 0)
        self.assertEqual(result["unchanged"], [10])
        self.assertEqual(result["changed"][0]["client_data_id"], 11)

    def test_identical_reimport_is_idempotent_and_creates_no_actions(self):
        template_cols = [
            {'id': 1, 'column_name': 'Voucher', 'data_type': 'Text'},
            {'id': 2, 'column_name': 'Amount', 'data_type': 'Numeric'},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        mapping_dict = {
            1: MappingStub('Voucher'),
            2: MappingStub('Amount'),
        }
        existing = [{
            'id': 19,
            'row_data': {'Voucher': 'UNCHANGED', 'Amount': 100},
            'version': 3,
            'extract_date': None,
            'validation_status': 'Verified',
            'verification_detail_id': 4,
        }]

        with patch('routers.import_export.execute_query_pg', return_value=existing):
            result = compare_transactions(
                1,
                1,
                template_cols,
                [{'Voucher': 'UNCHANGED', 'Amount': 100}],
                {'Voucher': 'Voucher', 'Amount': 'Amount'},
                mapping_dict,
            )

        self.assertEqual(result['unchanged_count'], 1)
        self.assertEqual(result['changed_count'], 0)
        self.assertEqual(result['new_count'], 0)
        self.assertEqual(result['missing_count'], 0)
        self.assertEqual(result['unchanged'], [19])
        self.assertEqual(result['changed'], [])
        self.assertEqual(result['new'], [])
        self.assertEqual(result['missing'], [])

    def test_changed_row_keeps_existing_canonical_identity(self):
        template_cols = [
            {'id': 1, 'column_name': 'Voucher', 'data_type': 'Text'},
            {'id': 2, 'column_name': 'Amount', 'data_type': 'Numeric'},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        existing = [{
            'id': 27,
            'row_data': {'Voucher': 'V-1', 'Amount': 100},
            'source_identity': 'stable-identity',
            'version': 2,
            'extract_date': None,
            'validation_status': 'Pending',
            'verification_detail_id': None,
        }]
        with patch('routers.import_export.execute_query_pg', return_value=existing):
            result = compare_transactions(
                1,
                1,
                template_cols,
                [{'Voucher': 'V-1', 'Amount': 125}],
                {'Voucher': 'Voucher', 'Amount': 'Amount'},
                {1: MappingStub('Voucher'), 2: MappingStub('Amount')},
            )

        self.assertEqual(result['changed'][0]['source_identity'], 'stable-identity')

    def test_same_source_file_with_no_delta_is_not_importable(self):
        self.assertTrue(_is_duplicate_upload_noop(
            {"id": 397},
            {"new_count": 0, "changed_count": 0, "missing_count": 0},
        ))

    def test_canonical_source_identity_is_repeatable_and_line_stable(self):
        template_cols = [
            {'id': 1, 'column_name': 'Voucher ID', 'data_type': 'Text'},
            {'id': 2, 'column_name': 'Line No', 'data_type': 'Text'},
            {'id': 3, 'column_name': 'Amount', 'data_type': 'Numeric'},
        ]
        original = {'Voucher ID': 'V-1', 'Line No': '2', 'Amount': 100}
        corrected = {'Voucher ID': 'V-1', 'Line No': '2', 'Amount': 125}

        self.assertEqual(
            _canonical_source_identity(template_cols, original, 7),
            _canonical_source_identity(template_cols, original, 7),
        )
        self.assertEqual(
            _canonical_source_identity(template_cols, original, 7),
            _canonical_source_identity(template_cols, corrected, 7),
        )

    def test_multi_line_document_ids_are_compared_without_duplicate_rejection(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher ID", "data_type": "Text"},
            {"id": 2, "column_name": "Account Name", "data_type": "Text"},
            {"id": 3, "column_name": "Debit Amount", "data_type": "Numeric"},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        mapping_dict = {
            1: MappingStub("Voucher ID"),
            2: MappingStub("Account Name"),
            3: MappingStub("Debit Amount"),
        }
        existing = [
            {"id": 30, "row_data": {"Voucher ID": "CPV-0126-100", "Account Name": "Expense", "Debit Amount": 100},
             "version": 1, "extract_date": None, "validation_status": "Pending", "verification_detail_id": None},
            {"id": 31, "row_data": {"Voucher ID": "CPV-0126-100", "Account Name": "Cash", "Debit Amount": 0},
             "version": 1, "extract_date": None, "validation_status": "Pending", "verification_detail_id": None},
        ]
        incoming = [
            {"Voucher ID": "CPV-0126-100", "Account Name": "Expense", "Debit Amount": 125},
            {"Voucher ID": "CPV-0126-100", "Account Name": "Cash", "Debit Amount": 0},
        ]

        with patch("routers.import_export.execute_query_pg", return_value=existing):
            result = compare_transactions(
                1, 1, template_cols, incoming,
                {"Voucher ID": "Voucher ID", "Account Name": "Account Name", "Debit Amount": "Debit Amount"},
                mapping_dict,
            )

        self.assertEqual(result["unchanged_count"], 1)
        self.assertEqual(result["changed_count"], 1)
        self.assertEqual(result["new_count"], 0)
        self.assertEqual(result["missing_count"], 0)
        self.assertEqual(result["unchanged"], [31])
        self.assertEqual(result["changed"][0]["client_data_id"], 30)

    def test_repeated_no_line_key_rows_are_compared_as_separate_occurrences(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher ID", "data_type": "Text"},
            {"id": 2, "column_name": "Account", "data_type": "Text"},
            {"id": 3, "column_name": "Amount", "data_type": "Numeric"},
        ]
        row = {"Voucher ID": "V-1", "Account": "Cash", "Amount": 100}
        existing = [{
            "id": 41,
            "row_data": row,
            "document_group_id": None,
            "source_identity": _canonical_source_identity(template_cols, row, 1),
            "version": 1,
            "extract_date": None,
            "validation_status": "Pending",
            "verification_detail_id": None,
        }]

        with patch("routers.import_export.execute_query_pg", return_value=existing):
            result = compare_transactions(
                1,
                1,
                template_cols,
                [row, row],
                {"Voucher ID": "Voucher ID", "Account": "Account", "Amount": "Amount"},
                {},
            )

        self.assertEqual(result["unchanged_count"], 1)
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(len(result["new_source_identities"]), 1)
        self.assertNotEqual(
            result["new_source_identities"][0],
            _canonical_source_identity(template_cols, row, 1),
        )

    def test_complete_replacement_removes_only_missing_lines_inside_detected_date_range(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
            {"id": 3, "column_name": "Date", "data_type": "Date"},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        mapping_dict = {
            1: MappingStub("Voucher"),
            2: MappingStub("Amount"),
            3: MappingStub("Date"),
        }
        existing = [
            {"id": 20, "row_data": {"Voucher": "KEEP", "Amount": 100, "Date": "01/01/2026"},
             "version": 1, "extract_date": "2026-01-01", "validation_status": "Pending", "verification_detail_id": None},
            {"id": 21, "row_data": {"Voucher": "REMOVE", "Amount": 200, "Date": "02/01/2026"},
             "version": 1, "extract_date": "2026-01-02", "validation_status": "Pending", "verification_detail_id": None},
            {"id": 22, "row_data": {"Voucher": "OUTSIDE", "Amount": 300, "Date": "03/01/2026"},
             "version": 1, "extract_date": "2026-01-03", "validation_status": "Pending", "verification_detail_id": None},
        ]

        # The scoped SQL returns the first two rows; OUTSIDE remains excluded.
        with patch("routers.import_export.execute_query_pg", side_effect=lambda sql, *a, **kw: existing[1:2] if 'SELECT cd.id FROM' in sql else existing[:2]):
            result = compare_transactions(
                1, 1, template_cols,
                [
                    {"Voucher": "KEEP", "Amount": 100, "Date": "01/01/2026"},
                    {"Voucher": "NEW", "Amount": 150, "Date": "02/01/2026"},
                ],
                {"Voucher": "Voucher", "Amount": "Amount", "Date": "Date"},
                mapping_dict,
                comparison_mode="replace_range",
                coverage_start="2026-01-01", coverage_end="2026-01-02",
                date_delimiter="/",
                date_order="DMY",
            )

        self.assertEqual(result["date_range"], {"start": "2026-01-01", "end": "2026-01-02"})
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["missing_count"], 1)
        self.assertEqual(result["missing"][0]["client_data_id"], 21)

    def test_validation_plan_keeps_only_actionable_rows_and_fingerprints(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
        ]
        change_detection = {
            "comparison_mode": "correction",
            "date_range": None,
            "unchanged_count": 7,
            "changed_count": 1,
            "new_count": 1,
            "missing_count": 1,
            "duplicate_group_count": 2,
            "duplicate_row_count": 4,
            "duplicate_skipped_count": 2,
            "duplicate_row_numbers": [8, 9, 12, 13],
            "duplicate_skipped_row_numbers": [9, 13],
            "changed": [{
                "client_data_id": 10,
                "old_row_data": {"Voucher": "CHANGED", "Amount": 100},
                "new_row_data": {"Voucher": "CHANGED", "Amount": 125},
                "old_version": 3,
                "verification_detail_id": 99,
            }],
            "new": [{"Voucher": "ADDED", "Amount": 50}],
            "missing": [{
                "client_data_id": 11,
                "old_row_data": {"Voucher": "REMOVED", "Amount": 75},
                "old_version": 2,
                "verification_detail_id": None,
            }],
        }

        payload = _build_validation_plan_payload(
            change_detection,
            template_cols,
            1,
            total_rows=10,
            rejected_rows=[],
            force_included_count=0,
            rate_source=None,
            date_delimiter="/",
            date_order="DMY",
        )

        self.assertEqual(payload["unchanged_count"], 7)
        self.assertEqual(len(payload["changed"]), 1)
        self.assertEqual(len(payload["new"]), 1)
        self.assertEqual(len(payload["missing"]), 1)
        self.assertNotIn("old_row_data", payload["changed"][0])
        self.assertNotIn("old_row_data", payload["missing"][0])
        self.assertTrue(payload["changed"][0]["expected_old_fingerprint"])
        self.assertEqual(payload["new"][0]["row_data"]["Voucher"], "ADDED")
        self.assertEqual(payload["duplicate_group_count"], 2)
        self.assertEqual(payload["duplicate_row_count"], 4)
        self.assertEqual(payload["duplicate_skipped_count"], 2)
        self.assertEqual(payload["duplicate_skipped_row_numbers"], [9, 13])

    def test_validation_signature_ignores_plan_id(self):
        base = {
            "template_id": 1,
            "file_id": 2,
            "client_id": 3,
            "mappings": [
                ColumnMapping(
                    template_column_id=1,
                    template_column_name="Voucher",
                    excel_column_name="Voucher",
                )
            ],
            "comparison_mode": "correction",
        }
        first = ImportRequest(**base, validation_plan_id="plan-one")
        second = ImportRequest(**base, validation_plan_id="plan-two")

        self.assertEqual(
            _validation_request_signature(first),
            _validation_request_signature(second),
        )

    def test_change_report_keeps_old_and_new_values_only_for_changed_fields(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
            {"id": 3, "column_name": "Narration", "data_type": "Text"},
        ]
        report = build_change_report(
            {
                "changed": [{
                    "client_data_id": 10,
                    "old_row_data": {"Voucher": "CPV-1", "Amount": 100, "Narration": "old"},
                    "new_row_data": {"Voucher": "CPV-1", "Amount": 125, "Narration": "new"},
                }],
                "new": [],
                "missing": [],
            },
            template_cols,
            1,
        )

        self.assertEqual(report[0]["changed_fields"], ["Amount", "Narration"])
        self.assertEqual(report[0]["old_values"], {"Amount": 100, "Narration": "old"})
        self.assertEqual(report[0]["new_values"], {"Amount": 125, "Narration": "new"})
        self.assertNotIn("old_row_data", report[0])
        self.assertNotIn("new_row_data", report[0])

    def test_uploaded_files_has_only_compact_upload_summary_columns(self):
        columns = {
            row["name"]
            for row in execute_query("PRAGMA table_info(uploaded_files)")
        }

        self.assertTrue({
            "template_id",
            "uploaded_by",
            "comparison_mode",
            "date_range_start",
            "date_range_end",
            "summary_total_count",
            "summary_added_count",
            "summary_changed_count",
            "summary_removed_count",
            "summary_unchanged_count",
            "summary_updated_at",
        }.issubset(columns))
        self.assertNotIn("change_report_json", columns)

    def test_client_data_has_canonical_identity_derived_columns_and_indexes(self):
        columns = {
            row["name"]
            for row in execute_query("PRAGMA table_info(client_data)")
        }
        self.assertTrue({
            "source_identity",
            "source_fingerprint",
            "derived_voucher_id",
            "derived_account_code",
            "derived_transaction_date",
            "derived_treasury_type",
            "derived_classification_status",
        }.issubset(columns))

        indexes = {
            row["name"]
            for row in execute_query("PRAGMA index_list(client_data)")
        }
        self.assertTrue({
            "idx_client_data_active_identity",
            "idx_client_data_active_identity_unique",
            "idx_client_data_active_derived_date",
            "idx_client_data_active_derived_classification",
        }.issubset(indexes))

    def test_uploaded_files_persists_compact_source_file_hash(self):
        columns = {
            row["name"]
            for row in execute_query("PRAGMA table_info(uploaded_files)")
        }

        self.assertIn("file_sha256", columns)
        indexes = {
            row["name"]
            for row in execute_query("PRAGMA index_list(uploaded_files)")
        }
        self.assertIn("idx_uploaded_files_source_hash_scope", indexes)

    def test_strict_gate_allows_exact_duplicate_lines_without_line_key(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher ID", "data_type": "Text"},
            {"id": 2, "column_name": "Account", "data_type": "Text"},
            {"id": 3, "column_name": "Amount", "data_type": "Numeric"},
        ]
        rows = [
            {"Voucher": "V-1", "Ledger": "Cash", "Amount": 100},
            {"Voucher": "V-1", "Ledger": "Cash", "Amount": 100},
        ]

        errors = _strict_duplicate_row_errors(
            rows,
            {"Voucher ID": "Voucher", "Account": "Ledger", "Amount": "Amount"},
            template_cols,
            1,
            None,
            0,
        )

        self.assertEqual(errors, [])

    def test_repeated_lines_without_line_key_receive_distinct_identities(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher ID", "data_type": "Text"},
            {"id": 2, "column_name": "Account", "data_type": "Text"},
            {"id": 3, "column_name": "Amount", "data_type": "Numeric"},
        ]
        rows = [
            {"Voucher ID": "V-1", "Account": "Cash", "Amount": 100},
            {"Voucher ID": "V-1", "Account": "Cash", "Amount": 100},
        ]

        identities = _canonical_source_identities(template_cols, rows, 1)

        self.assertEqual(len(set(identities)), 2)

    def test_strict_gate_allows_same_line_content_in_different_documents(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher ID", "data_type": "Text"},
            {"id": 2, "column_name": "Account", "data_type": "Text"},
            {"id": 3, "column_name": "Amount", "data_type": "Numeric"},
        ]
        rows = [
            {"Voucher": "V-1", "Ledger": "Cash", "Amount": 100},
            {"Voucher": "V-2", "Ledger": "Cash", "Amount": 100},
        ]

        errors = _strict_duplicate_row_errors(
            rows,
            {"Voucher ID": "Voucher", "Account": "Ledger", "Amount": "Amount"},
            template_cols,
            1,
            None,
            0,
        )

        self.assertEqual(errors, [])

def test_batch_audit_retains_only_changed_fields_and_no_insert_copy():
    from routers.import_export import _audit_row_params
    old = {"Voucher": "V1", "Amount": 100, "Detail": "Unchanged"}
    new = {**old, "Amount": 120}
    inserted = _audit_row_params(1, "INSERT", None, new)
    assert inserted[3:] == (None, None)
    updated = _audit_row_params(1, "UPDATE", old, new)
    assert updated[3].adapted == {"Amount": 100}
    assert updated[4].adapted == {"Amount": 120}
    deleted = _audit_row_params(1, "DELETE", old, None)
    assert set(deleted[3].adapted) == {"_row_fingerprint"}


def test_storage_budget_counts_allocated_bytes_and_projected_growth():
    from database import get_database_storage_budget_pg
    with patch("database.execute_query_pg", return_value=[{"database_bytes": 300_000_000}]):
        budget = get_database_storage_budget_pg(100_000_000)
    assert budget["warning"] is True
    assert budget["blocked"] is True
    assert budget["projected_bytes"] == 400_000_000


def test_import_storage_guard_blocks_before_writes():
    import pytest
    from fastapi import HTTPException
    from routers.import_export import _check_upload_storage_budget
    with patch("routers.import_export.get_database_storage_budget_pg", return_value={"blocked": True}), pytest.raises(HTTPException) as error:
        _check_upload_storage_budget(1)
    assert error.value.status_code == 507


def test_ready_validation_plan_replaces_same_client_template_across_files():
    from contextlib import nullcontext
    from types import SimpleNamespace
    from routers.import_export import _create_validation_plan
    writes = []
    with patch("routers.import_export.pg_transaction", return_value=nullcontext()), \
            patch("routers.import_export.execute_query_pg"), \
            patch("routers.import_export._check_upload_storage_budget"), \
            patch("routers.import_export._validation_file_sha256", return_value="hash"), \
            patch("routers.import_export._validation_request_signature", return_value="settings"), \
            patch("routers.import_export.execute_write_pg", side_effect=lambda sql, params=None: writes.append((sql, params))):
        _create_validation_plan(SimpleNamespace(client_id=5, template_id=1, file_id=99), "unused", {"new": []})
    assert writes[0][1] == (5, 1)
    assert "client_id = %s AND template_id = %s" in writes[0][0]
    assert "INSERT INTO upload_validation_plans" in writes[1][0]


def test_postgres_classification_updates_only_canonical_rows_with_typed_dates():
    from copy import deepcopy
    from unittest.mock import MagicMock
    from services.cash_bank_classification import upsert_classification_rows_postgres

    source = [
        {"id": 1, "client_id": 5, "template_id": 1, "upload_id": 459,
         "row_data": {"Voucher ID": "V1", "Account Code": "100",
                      "Date": "2026-01-05", "Debit": 90, "Credit": 0}},
        {"id": 2, "client_id": 5, "template_id": 1, "upload_id": 459,
         "row_data": {"Voucher ID": "V1", "Account Code": "200",
                      "Date": "2026-01-05", "Debit": 0, "Credit": 90}},
        {"id": 3, "client_id": 5, "template_id": 1, "upload_id": 459,
         "row_data": {"Voucher ID": "V2", "Account Code": "200",
                      "Date": None, "Debit": 0, "Credit": 0}},
    ]
    original = deepcopy(source)
    conn = MagicMock()
    with patch("services.cash_bank_classification.execute_values") as batch:
        result = upsert_classification_rows_postgres(conn, source, {"100": {"bank"}}, 2)
    assert source == original
    batch.assert_called_once()
    sql, values = batch.call_args.args[1:3]
    assert "UPDATE client_data AS target" in sql
    assert "NULLIF(incoming.derived_transaction_date, '')::date" in sql
    assert "cash_bank_transaction_classifications" not in sql
    assert [row[4] for row in values] == ["2026-01-05", "2026-01-05", None]
    assert [(row[7], row[8]) for row in values] == [(90, 0), (0, 90), (0, 0)]
    assert [row["classification_status"] for row in result] == ["bank", "non_treasury", "non_treasury"]
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()
    statements = [call.args[0] for call in conn.cursor.return_value.__enter__.return_value.execute.call_args_list]
    assert all("cash_bank_transaction_classifications" not in sql for sql in statements)


@pytest.mark.parametrize("import_path", ["direct", "validation_plan"])
@pytest.mark.parametrize("failure_stage", ["classification", "occurrence"])
def test_import_failure_after_source_writes_rolls_back_whole_upload(import_path, failure_stage):
    """Both route branches must retain the outer transaction after raw-row batches."""
    from contextlib import ExitStack
    from copy import deepcopy
    from unittest.mock import MagicMock
    import pandas as pd
    import psycopg2
    from routers import import_export as uploads

    columns = [{"id": i, "column_name": name, "data_type": kind} for i, (name, kind) in enumerate(
        [("Voucher", "Text"), ("Line ID", "Text"), ("Amount", "Numeric")], 1)]
    rows = [{"Voucher": "V1", "Line ID": line, "Amount": 100} for line in ("A", "B")]
    identities = _canonical_source_identities(columns, rows, 1)
    changes = {"new": rows, "new_source_identities": identities, "changed": [], "missing": [],
               "unchanged": [], "new_count": 2, "changed_count": 0, "missing_count": 0,
               "unchanged_count": 0, "comparison_mode": "correction", "date_range": None}
    plan = {"new": [{"row_data": row, "source_identity": identity}
                    for row, identity in zip(rows, identities)], "total_rows": 2}
    request = ImportRequest(client_id=5, template_id=1, file_id=99,
        validation_plan_id="plan" if import_path == "validation_plan" else None,
        mappings=[ColumnMapping(template_column_id=c["id"], template_column_name=c["column_name"],
                               excel_column_name=c["column_name"]) for c in columns])
    original = [{"Voucher": "EXISTING", "Amount": 50}]
    committed, pending, staged_counts = deepcopy(original), [], []
    conn = MagicMock()
    conn.closed = 0
    conn.rollback.side_effect = pending.clear
    conn.commit.side_effect = lambda: committed.extend(pending)
    pool = MagicMock()
    pool.getconn.return_value = conn

    def read(sql, params=None):
        if "FROM templates" in sql:
            return [{"id": 1, "template_name": "Transactions", "rate_source": None}]
        if "FROM uploaded_files" in sql:
            return [{"file_path": "isolated.xlsx", "file_name": "isolated.xlsx", "status": "Pending"}]
        if "FROM clients" in sql:
            return [{"id": 5}]
        if "FROM template_column_mappings" in sql:
            return columns
        return []

    def insert(active, sql, values, **kwargs):
        assert active is conn and database._pg_transaction.get() is conn
        pending.extend(value[3].adapted for value in values)
        staged_counts.append(len(pending))
        return [10, 11]

    def fail(*args, **kwargs):
        assert pending == rows
        assert database._pg_transaction.get() is conn
        conn.commit.assert_not_called()
        raise psycopg2.OperationalError("SSL error: unexpected eof while reading")

    replacements = {
        "execute_query_pg": read, "_ensure_uploaded_file_hash": lambda *a: "hash",
        "_find_prior_import_with_same_hash": lambda *a: None, "get_rate_precision": lambda: 0,
        "_load_validation_plan": lambda *a: {"plan_json": plan},
        "compare_transactions": lambda **kw: deepcopy(changes),
        "_check_upload_storage_budget": MagicMock(),
        "load_treasury_mapping_postgres": lambda *a: {},
        "execute_batch_insert_with_ids_pg": insert, "execute_batch_pg": MagicMock(),
        "upsert_classification_rows_postgres": fail if failure_stage == "classification" else MagicMock(),
        "resolve_upload_occurrences_postgres": fail if failure_stage == "occurrence" else MagicMock(),
        "log_audit": MagicMock(), "_persist_upload_summary": MagicMock(),
    }
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(database, "get_pg_pool", return_value=pool))
            stack.enter_context(patch.object(uploads.pd, "read_excel", return_value=pd.DataFrame(rows)))
            for name, value in replacements.items():
                stack.enter_context(patch.object(uploads, name, value))
            with pytest.raises(psycopg2.OperationalError):
                uploads.validate_and_import(request)
        assert staged_counts == [2]
        assert committed == original and pending == []
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        pool.getconn.assert_called_once()
        assert database._pg_transaction.get() is None
        replacements["_persist_upload_summary"].assert_not_called()
    finally:
        uploads.IMPORT_PROGRESS.pop(99, None)


@pytest.mark.parametrize("endpoint", ["validate_only", "validate_and_import"])
def test_failed_comparison_cannot_create_a_plan_or_write_transactions(endpoint):
    from contextlib import ExitStack
    from unittest.mock import MagicMock
    import pandas as pd
    import psycopg2
    from routers import import_export as uploads

    columns = [{"id": 1, "column_name": "Voucher", "data_type": "Text"},
               {"id": 2, "column_name": "Amount", "data_type": "Numeric"}]
    request = ImportRequest(client_id=5, template_id=1, file_id=99, mappings=[
        ColumnMapping(template_column_id=c['id'], template_column_name=c['column_name'],
                      excel_column_name=c['column_name']) for c in columns])

    def read(sql, params=None):
        if "FROM templates" in sql:
            return [{"id": 1, "template_name": "Transactions", "rate_source": None}]
        if "FROM uploaded_files" in sql:
            return [{"file_path": "isolated.xlsx", "file_name": "isolated.xlsx", "status": "Pending"}]
        if "FROM clients" in sql:
            return [{"id": 5}]
        if "FROM template_column_mappings" in sql:
            return columns
        return []

    guards = {name: MagicMock() for name in ["_create_validation_plan", "execute_write_pg",
        "execute_batch_pg", "execute_batch_insert_with_ids_pg", "_persist_upload_summary", "pg_transaction"]}
    with ExitStack() as stack:
        for name, mock in guards.items():
            stack.enter_context(patch.object(uploads, name, mock))
        stack.enter_context(patch.object(uploads, "execute_query_pg", side_effect=read))
        stack.enter_context(patch.object(uploads, "_ensure_uploaded_file_hash", return_value="hash"))
        stack.enter_context(patch.object(uploads, "_find_prior_import_with_same_hash", return_value=None))
        stack.enter_context(patch.object(uploads, "get_rate_precision", return_value=0))
        stack.enter_context(patch.object(uploads.pd, "read_excel", return_value=pd.DataFrame([
            {"Voucher": "V1", "Amount": 100}])))
        stack.enter_context(patch.object(uploads, "compare_transactions", side_effect=psycopg2.OperationalError(
            "SSL error: unexpected eof while reading")))
        with pytest.raises(psycopg2.OperationalError):
            getattr(uploads, endpoint)(request)
    for mock in guards.values():
        mock.assert_not_called()


class _PrecisionAuditCursor:
    """Small PostgreSQL protocol fixture: no SQLite or remote connection."""
    def __init__(self, conn, name=None):
        self.conn, self.name, self.position, self.sql = conn, name, 0, ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.conn.statements.append(sql)
        assert sql.lstrip().startswith(("SELECT", "SET TRANSACTION"))

    def fetchall(self):
        if "AS repair_schema_guard" in self.sql:
            return [{"repair_schema_guard": {"columns": ["id", "row_data", "extract_amount"]}}]
        if "pg_database_size" in self.sql:
            return [{"database_bytes": 257_330_323, "table_total_bytes": 189_186_048,
                     "table_bytes": 112_902_144, "index_bytes": 76_283_904,
                     "transaction_read_only": self.conn.read_only, "transaction_isolation": "repeatable read"}]
        if "information_schema.columns" in self.sql:
            return [{"data_type": "real", "udt_name": "float4", "is_nullable": "YES", "column_default": None}]
        if "FROM templates t" in self.sql:
            return self.conn.mappings
        if "FROM pg_depend" in self.sql:
            return self.conn.dependencies
        if "FROM pg_index" in self.sql:
            return [{"index_name": "idx_client_data_active_identity_unique", "indisprimary": False,
                     "indisunique": True, "indisvalid": True, "indisready": True,
                     "definition": "CREATE UNIQUE INDEX active_identity ON client_data ...", "index_bytes": 100}]
        if "AS duplicate_groups" in self.sql:
            return [{"duplicate_groups": 0}]
        if "UNION ALL" in self.sql:
            return [{"table_name": "cashflow_prepared_lines", "rows": 0}]
        raise AssertionError("Unexpected audit SQL")

    def fetchmany(self, size):
        self.conn.fetch_sizes.append(size)
        if len(self.conn.fetch_sizes) == self.conn.disconnect_fetch:
            import psycopg2
            raise psycopg2.OperationalError("SSL error: unexpected eof while reading")
        page = self.conn.rows[self.position:self.position + size]
        self.position += len(page)
        return page


class _PrecisionAuditConnection:
    def __init__(self, rows, mappings=None, disconnect_fetch=None, read_only="on", dependencies=None):
        self.rows = rows
        self.mappings = mappings if mappings is not None else [
            {"template_id": 1, "template_name": "Transactions", "template_active": True,
             "column_id": 1, "column_name": "Amount", "data_type": "Numeric", "sort_order": 1}]
        self.disconnect_fetch = disconnect_fetch
        self.read_only = read_only
        self.dependencies = dependencies or []
        self.statements, self.fetch_sizes = [], []
        self.closed, self.commits, self.rollbacks = 0, 0, 0

    def cursor(self, cursor_factory=None, name=None):
        return _PrecisionAuditCursor(self, name)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _precision_row(row_id, amount, stored, *, client_id=5, template_id=1, retired=False, binary=None):
    return {"id": row_id, "client_id": client_id, "template_id": template_id,
            "row_data": {"Amount": amount}, "extract_amount": stored,
            "binary_stored_amount": stored if binary is None else binary,
            "is_deleted": retired, "change_detection_status": "deleted" if retired else "active",
            "source_identity": f"source-{row_id}", "raw_fingerprint": str(row_id),
            "protected_fingerprint": f"protected-{row_id}"}


def _run_precision_fixture(*connections, page_size=2):
    from unittest.mock import MagicMock
    from backfill_extract_amount import audit_postgres
    pool = MagicMock()
    pool.getconn.side_effect = connections
    with patch.object(database, "get_pg_pool", return_value=pool):
        result = audit_postgres(page_size)
    return result, pool


def test_precision_audit_streams_all_datasets_and_retired_rows_without_writing():
    from copy import deepcopy
    mappings = [{"template_id": n, "template_name": f"T{n}", "template_active": n == 1,
                 "column_id": n, "column_name": "Amount", "data_type": "Numeric", "sort_order": 1}
                for n in (1, 2)]
    rows = [_precision_row(1, 349677.57, 349678), _precision_row(2, 100, 100),
            _precision_row(3, 349677.57, 349678, client_id=6, template_id=2, retired=True),
            _precision_row(4, 0, None)]
    original = deepcopy(rows)
    conn = _PrecisionAuditConnection(rows, mappings)
    result, pool = _run_precision_fixture(conn)
    assert result['totals']['rows_scanned'] == 4
    assert result['totals']['different_amounts'] == 2
    assert result['totals']['retired_rows'] == 1
    assert result['totals']['no_source_amount'] == 1
    assert len(result['datasets']) == 2
    assert conn.fetch_sizes == [2, 2, 2]
    assert rows == original and conn.commits == 0 and conn.rollbacks == 1
    assert database._pg_transaction.get() is None
    assert conn.statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    pool.getconn.assert_called_once()
    assert not result['live_apply_available']
    assert not result['backup_restore_verified'] and not result['safe_peak_verified']
    assert result['repair_preparation']['direct_rewrite_exceeds_400mb_allowance']
    assert result['repair_preparation']['only_allowed_field_change'] == 'extract_amount'
    assert all('row_data' not in example for item in result['datasets'] for example in item['examples'])


def test_precision_audit_distinguishes_read_differences_from_binary_float_noise():
    rows = [_precision_row(1, 0.1, 0.1, binary=0.10000000149011612),
            _precision_row(2, -246157.1, -246157), _precision_row(3, 25.50, None)]
    result, _ = _run_precision_fixture(_PrecisionAuditConnection(rows))
    assert result['totals']['different_amounts'] == 1
    assert result['totals']['matching_amounts'] == 1
    assert result['totals']['missing_amounts'] == 1
    assert result['totals']['binary_storage_difference_rows'] == 2


@pytest.mark.parametrize('page_size', [1, 2, 7])
def test_precision_audit_digests_and_counts_are_independent_of_page_boundaries(page_size):
    rows = [_precision_row(n, 10.25, 10.25) for n in range(1, 8)]
    result, _ = _run_precision_fixture(_PrecisionAuditConnection(rows), page_size=page_size)
    baseline, _ = _run_precision_fixture(_PrecisionAuditConnection(rows), page_size=3)
    assert result['datasets'] == baseline['datasets']
    assert result['totals']['rows_scanned'] == 7  # Identical-looking lines remain separate.


def test_precision_audit_blocks_invalid_json_nonfinite_and_unsupported_source_precision():
    rows = [_precision_row(1, 'bad', None), _precision_row(2, float('inf'), 1),
            _precision_row(3, 9007199254740993, 9007199254740992), _precision_row(4, 1, 1)]
    rows[3]['row_data'] = 'not JSON'
    result, _ = _run_precision_fixture(_PrecisionAuditConnection(rows))
    assert result['totals']['invalid_source_rows'] == 3
    assert result['totals']['unsupported_source_precision_rows'] == 1
    assert result['source_or_integrity_blockers']
    assert not result['live_apply_available']


def test_precision_audit_discards_partial_counts_when_read_disconnects():
    dead = _PrecisionAuditConnection([_precision_row(1, 1, 2), _precision_row(2, 1, 2)], disconnect_fetch=2)
    good = _PrecisionAuditConnection([_precision_row(3, 100, 100)])
    result, pool = _run_precision_fixture(dead, good, page_size=1)
    assert result['totals']['rows_scanned'] == 1
    assert result['totals']['different_amounts'] == 0
    assert pool.getconn.call_count == 2
    assert any(call.args == (dead,) and call.kwargs == {'close': True} for call in pool.putconn.call_args_list)
    assert database._pg_transaction.get() is None


def test_precision_audit_second_disconnect_and_bad_snapshot_fail_without_results():
    import psycopg2
    with pytest.raises(psycopg2.OperationalError):
        _run_precision_fixture(*[_PrecisionAuditConnection([], disconnect_fetch=1) for _ in range(2)])
    assert database._pg_transaction.get() is None
    with pytest.raises(RuntimeError, match='enforced read-only'):
        _run_precision_fixture(_PrecisionAuditConnection([], read_only='off'))
    assert database._pg_transaction.get() is None


def test_precision_expected_amount_keeps_existing_selection_and_zero_rules():
    from backfill_extract_amount import expected_helper_amount
    columns = [{'id': 1, 'column_name': 'Quantity', 'data_type': 'Numeric'},
               {'id': 2, 'column_name': 'Amount', 'data_type': 'Numeric'}]
    assert expected_helper_amount({'Quantity': 25, 'Amount': '123.45'}, columns) == 123.45
    assert expected_helper_amount({'Quantity': 25, 'Amount': 0}, columns) == 25
    movement = [{'id': n, 'column_name': name, 'data_type': 'Numeric'}
                for n, name in enumerate(['Debit Amount', 'Credit Amount'], 1)]
    assert expected_helper_amount({'Debit Amount': 0, 'Credit Amount': 123.45}, movement) == 123.45
    assert expected_helper_amount({'Debit Amount': 0, 'Credit Amount': 0}, movement) is None


def test_precision_cli_rejects_postgres_run_before_borrowing_or_writing():
    import backfill_extract_amount as repair
    with patch.object(database, 'get_pg_pool') as pool, patch.object(repair, 'scan') as legacy:
        with pytest.raises(SystemExit) as error:
            repair.main(['--postgres', '--run'])
    assert error.value.code == 2
    pool.assert_not_called()
    legacy.assert_not_called()


def test_precision_cli_never_overwrites_reports_or_writes_partial_failed_audits(tmp_path):
    import psycopg2
    import backfill_extract_amount as repair
    existing = tmp_path / 'existing.json'
    existing.write_text('KEEP')
    with patch.object(repair, 'audit_postgres') as audit:
        with pytest.raises(SystemExit):
            repair.main(['--postgres', '--output', str(existing)])
    assert existing.read_text() == 'KEEP'
    audit.assert_not_called()
    target = tmp_path / 'new.json'
    with patch.object(repair, 'audit_postgres', side_effect=psycopg2.OperationalError('read failed')):
        with pytest.raises(psycopg2.OperationalError):
            repair.main(['--postgres', '--output', str(target)])
    assert not target.exists()


def test_precision_cli_preserves_legacy_sqlite_selection_and_read_only_postgres(tmp_path):
    import json
    import backfill_extract_amount as repair
    legacy_report = {'templates': [], 'grand_total': {}}
    with patch.object(repair, 'scan', return_value=legacy_report) as legacy, patch.object(repair, 'print_report'):
        repair.main([])
        legacy.assert_called_once_with(dry_run=True)
    with patch.object(repair, 'scan', return_value=legacy_report) as legacy, patch.object(repair, 'print_report'):
        repair.main(['--run'])
        legacy.assert_called_once_with(dry_run=False)
    report = {'read_only': True, 'live_apply_available': False}
    target = tmp_path / 'audit.json'
    with patch.object(repair, 'scan') as legacy, patch.object(repair, 'audit_postgres', return_value=report):
        repair.main(['--postgres', '--output', str(target)])
    legacy.assert_not_called()
    assert json.loads(target.read_text()) == report


class _RepairCursor(_PrecisionAuditCursor):
    """Transactional PostgreSQL protocol model, never an SQL compatibility test."""
    rowcount = 0

    def execute(self, sql, params=None):
        from copy import deepcopy
        import psycopg2
        self.params = params
        self.sql = sql
        self.conn.statements.append(sql)
        if self.conn.fail_sql and self.conn.fail_sql in sql:
            self.conn.fail_sql = None
            raise psycopg2.OperationalError("SSL error: unexpected eof while reading")
        if sql.startswith("SET TRANSACTION"):
            self.conn.snapshot = deepcopy((self.conn.rows, self.conn.columns))
        elif sql.startswith(("SELECT", "SET LOCAL", "LOCK TABLE")):
            pass
        elif sql.startswith("ALTER TABLE public.client_data ADD COLUMN"):
            self.conn.columns['extract_amount_repair'] = {
                'name': 'extract_amount_repair', 'type': 'double precision', 'not_null': False,
                'default_value': None, 'comment': None}
            for row in self.conn.rows:
                row['extract_amount_repair'] = None
        elif sql.startswith("COMMENT ON COLUMN"):
            self.conn.columns['extract_amount_repair']['comment'] = params[0]
        elif sql.startswith("UPDATE public.client_data SET extract_amount_repair"):
            amount, row_id, _ = params
            row = next(row for row in self.conn.rows if row['id'] == row_id)
            self.rowcount = int(row.get('extract_amount_repair') != amount)
            if self.rowcount:
                row['extract_amount_repair'] = amount
                self.conn.allocated += self.conn.growth_per_update
        elif sql.startswith("ALTER TABLE public.client_data RENAME COLUMN"):
            old, new = sql.split()[-3], sql.split()[-1]
            self.conn.columns[new] = self.conn.columns.pop(old)
            self.conn.columns[new]['name'] = new
            for row in self.conn.rows:
                row[new] = row.pop(old)
        else:
            raise AssertionError("Unexpected rehearsal SQL: " + sql)

    def fetchone(self):
        assert self.sql == "SELECT current_database(), inet_server_addr()::text"
        return ('precision_rehearsal_test', '127.0.0.1')

    def fetchall(self):
        from copy import deepcopy
        if self.sql.startswith('SELECT a.attname AS name'):
            return deepcopy(list(self.conn.columns.values()))
        if self.sql.startswith('SELECT id,template_id,row_data'):
            # Parameters are captured by the connection through this cursor.
            return deepcopy([row for row in self.conn.rows if row['id'] > self.params[0]][:self.params[1]])
        if 'AS repair_schema_guard' in self.sql:
            return [{'repair_schema_guard': {'columns': [
                [name, 'amount-helper' if name == 'extract_amount' else col['type'], col['not_null'], col['default_value']]
                for name, col in sorted(self.conn.columns.items())
                if name not in {'extract_amount_repair', 'extract_amount_previous'}],
                'extra': self.conn.schema_extra}}]
        if 'information_schema.columns' in self.sql:
            col = self.conn.columns['extract_amount']
            return [{'data_type': col['type'], 'udt_name': 'float4' if col['type'] == 'real' else 'float8',
                     'is_nullable': 'NO' if col['not_null'] else 'YES', 'column_default': col['default_value']}]
        result = super().fetchall()
        if 'pg_database_size' in self.sql:
            result[0]['database_bytes'] = self.conn.allocated
        return result

    def fetchmany(self, size):
        from copy import deepcopy
        import backfill_extract_amount as repair
        page = deepcopy(self.conn.rows[self.position:self.position+size])
        self.position += len(page)
        for row in page:
            column = 'extract_amount_previous' if 'cd.extract_amount_previous AS' in self.sql else 'extract_amount'
            stored = row[column]
            protected = {key: value for key, value in row.items() if key not in {
                'extract_amount', 'extract_amount_previous', 'extract_amount_repair',
                'binary_stored_amount', 'raw_fingerprint', 'protected_fingerprint'}}
            row['extract_amount'] = row['binary_stored_amount'] = stored
            row['raw_fingerprint'] = repair._digest_records(row['row_data'])
            row['protected_fingerprint'] = repair._digest_records(protected)
            if 'AS candidate_amount' in self.sql:
                row['candidate_amount'] = row.get('extract_amount_repair')
        return page

class _RepairConnection(_PrecisionAuditConnection):
    autocommit = False

    def __init__(self, rows):
        from copy import deepcopy
        super().__init__(deepcopy(rows))
        self.columns = {'extract_amount': {'name': 'extract_amount', 'type': 'real', 'not_null': False,
                                          'default_value': None, 'comment': None}}
        self.snapshot = None
        self.fail_sql = None
        self.commit_failure = False
        self.allocated, self.growth_per_update = 257_330_323, 128
        self.schema_extra = None
        self.bulk_calls = []

    def cursor(self, cursor_factory=None, name=None):
        return _RepairCursor(self, name)

    def get_transaction_status(self):
        return 0 if self.snapshot is None else 2

    def get_dsn_parameters(self):
        return {'host': 'localhost', 'dbname': 'precision_rehearsal_test'}

    def commit(self):
        self.snapshot = None
        self.commits += 1
        if self.commit_failure:
            import psycopg2
            self.commit_failure = False
            raise psycopg2.OperationalError('SSL error: unexpected eof while reading')

    def rollback(self):
        if self.snapshot is not None:
            self.rows, self.columns = self.snapshot
        self.snapshot = None
        self.rollbacks += 1
        self.read_only = 'off'


def _repair_fixture():
    from backfill_extract_amount import _audit_postgres_snapshot, build_repair_plan
    conn = _RepairConnection([_precision_row(1, 349677.57, 349678),
                              _precision_row(2, 349677.57, 349678), _precision_row(3, 0, None)])
    plan = build_repair_plan(_audit_postgres_snapshot(conn, 2))
    return conn, plan


def _repair_step(conn, plan, action, **kwargs):
    from backfill_extract_amount import rehearsal_step_pg
    def bulk(cursor, statement, values, *, template, page_size):
        assert page_size == len(values) <= 2000
        assert template == '(%s::bigint,%s::double precision)'
        assert 'FROM (VALUES %s)' in statement
        conn.bulk_calls.append(len(values))
        updates = 0
        for row_id, amount in values:
            cursor.execute('UPDATE public.client_data SET extract_amount_repair=%s WHERE id=%s AND extract_amount_repair IS DISTINCT FROM %s',
                           (amount, row_id, amount))
            updates += cursor.rowcount
        cursor.rowcount = updates
    with patch.object(database, 'get_pg_pool', side_effect=AssertionError('No live pool in rehearsal tests')), \
            patch('backfill_extract_amount.execute_values', side_effect=bulk):
        return rehearsal_step_pg(conn, plan, action, **kwargs)


def test_repair_repeated_zero_lines_resume_switch_and_verified_rollback():
    from copy import deepcopy
    conn, plan = _repair_fixture()
    original = deepcopy(conn.rows)
    _repair_step(conn, plan, 'prepare')
    first = _repair_step(conn, plan, 'batch', batch_size=1)
    assert first['rows_updated'] == 1 and first['after_id'] == 1
    assert [row['extract_amount'] for row in conn.rows] == [349678, 349678, None]
    with pytest.raises(RuntimeError, match='incomplete'):
        _repair_step(conn, plan, 'switch')
    # Restart from zero after interruption: the committed first row is a no-op.
    resumed = _repair_step(conn, plan, 'batch')
    assert resumed['rows_scanned'] == 3 and resumed['rows_updated'] == 1
    assert _repair_step(conn, plan, 'batch', after_id=3)['scan_complete']
    switched = _repair_step(conn, plan, 'switch')
    assert switched['state'] == 'switched' and not switched['physical_peak_proven']
    assert [row['extract_amount'] for row in conn.rows] == [349677.57, 349677.57, None]
    assert _repair_step(conn, plan, 'status')['state'] == 'switched'
    assert _repair_step(conn, plan, 'switch')['state'] == 'switched'  # Lost final acknowledgement is recoverable.
    assert _repair_step(conn, plan, 'rollback')['state'] == 'staging'
    for before, after in zip(original, conn.rows):
        assert {key: after[key] for key in before} == before
    assert len(conn.rows) == 3 and len({row['source_identity'] for row in conn.rows}) == 3


def test_repair_batch_failure_and_lost_commit_ack_require_inspection_without_replay():
    import psycopg2
    conn, plan = _repair_fixture()
    _repair_step(conn, plan, 'prepare')
    conn.fail_sql = 'UPDATE public.client_data'
    with pytest.raises(psycopg2.OperationalError):
        _repair_step(conn, plan, 'batch')
    assert all(row['extract_amount_repair'] is None for row in conn.rows)
    conn.commit_failure = True
    with pytest.raises(psycopg2.OperationalError):
        _repair_step(conn, plan, 'batch')
    updates = sum('UPDATE public.client_data' in sql for sql in conn.statements)
    assert _repair_step(conn, plan, 'status')['state'] == 'staging'
    assert sum('UPDATE public.client_data' in sql for sql in conn.statements) == updates
    assert _repair_step(conn, plan, 'batch')['rows_updated'] == 0


def test_repair_staging_uses_one_bounded_statement_and_skips_empty_batches():
    conn, plan = _repair_fixture()
    _repair_step(conn, plan, 'prepare')
    assert _repair_step(conn, plan, 'batch')['rows_updated'] == 2
    assert conn.bulk_calls == [3]  # Repeated and zero lines share one bounded call.
    assert _repair_step(conn, plan, 'batch')['rows_updated'] == 0
    assert conn.bulk_calls == [3, 3]
    assert _repair_step(conn, plan, 'batch', after_id=3)['scan_complete']
    assert conn.bulk_calls == [3, 3]


@pytest.mark.parametrize('change', ['raw', 'helper', 'mapping', 'schema', 'identity', 'insert', 'dependency'])
def test_repair_refuses_switch_when_bound_source_or_configuration_changes(change):
    conn, plan = _repair_fixture()
    _repair_step(conn, plan, 'prepare')
    _repair_step(conn, plan, 'batch')
    if change == 'raw':
        conn.rows[0]['row_data']['Amount'] = 10
    elif change == 'helper':
        conn.rows[0]['extract_amount'] = 11
    elif change == 'mapping':
        conn.mappings[0]['column_name'] = 'Different'
    elif change == 'schema':
        conn.schema_extra = 'new trigger'
    elif change == 'identity':
        conn.rows[0]['source_identity'] = 'changed'
    elif change == 'insert':
        conn.rows.append(_precision_row(4, 50, 50))
    else:
        conn.dependencies.append({'dependent_object': 'new view', 'deptype': 'n'})
    with pytest.raises(RuntimeError, match='changed since'):
        _repair_step(conn, plan, 'switch')
    assert 'extract_amount_previous' not in conn.columns


def test_repair_second_rename_failure_rolls_back_entire_switch():
    import psycopg2
    conn, plan = _repair_fixture()
    _repair_step(conn, plan, 'prepare')
    _repair_step(conn, plan, 'batch')
    conn.fail_sql = 'RENAME COLUMN extract_amount_repair TO extract_amount'
    with pytest.raises(psycopg2.OperationalError):
        _repair_step(conn, plan, 'switch')
    assert conn.columns['extract_amount']['type'] == 'real'
    assert 'extract_amount_previous' not in conn.columns
    assert _repair_step(conn, plan, 'switch')['state'] == 'switched'


def test_repair_lost_switch_acknowledgement_can_be_inspected_and_repeated_safely():
    import psycopg2
    conn, plan = _repair_fixture()
    _repair_step(conn, plan, 'prepare')
    _repair_step(conn, plan, 'batch')
    conn.commit_failure = True
    with pytest.raises(psycopg2.OperationalError):
        _repair_step(conn, plan, 'switch')
    renames = sum('RENAME COLUMN' in sql for sql in conn.statements)
    assert renames == 2
    assert _repair_step(conn, plan, 'status')['state'] == 'switched'
    assert _repair_step(conn, plan, 'switch')['state'] == 'switched'
    assert sum('RENAME COLUMN' in sql for sql in conn.statements) == renames


def test_repair_bad_second_source_rejects_entire_batch_before_writing():
    conn, plan = _repair_fixture()
    _repair_step(conn, plan, 'prepare')
    conn.rows[1]['row_data']['Amount'] = 'invalid'
    before = len(conn.statements)
    with pytest.raises(ValueError, match='numeric source'):
        _repair_step(conn, plan, 'batch')
    assert all(row['extract_amount_repair'] is None for row in conn.rows)
    assert conn.columns['extract_amount']['type'] == 'real'
    assert not any('UPDATE public.client_data' in statement for statement in conn.statements[before:])


def test_repair_status_is_available_even_when_storage_blocks_more_writes():
    conn, plan = _repair_fixture()
    _repair_step(conn, plan, 'prepare')
    conn.allocated = 399_000_000
    assert _repair_step(conn, plan, 'status')['state'] == 'staging'
    with pytest.raises(RuntimeError, match='headroom'):
        _repair_step(conn, plan, 'batch')


def test_repair_rejects_corrupt_staging_and_mapping_changes_before_more_batches():
    conn, plan = _repair_fixture()
    _repair_step(conn, plan, 'prepare')
    _repair_step(conn, plan, 'batch')
    conn.rows[0]['extract_amount_repair'] = 1
    with pytest.raises(RuntimeError, match='incorrect'):
        _repair_step(conn, plan, 'switch')
    assert conn.columns['extract_amount']['type'] == 'real'
    conn.mappings[0]['column_name'] = 'Changed'
    with pytest.raises(RuntimeError, match='mappings changed'):
        _repair_step(conn, plan, 'batch')


def test_repair_plan_requires_review_of_user_triggers_and_complete_new_assessment():
    import backfill_extract_amount as repair
    conn = _RepairConnection([_precision_row(1, 10.25, 10)])
    assessment = repair._audit_postgres_snapshot(conn, 2)
    assessment['repair_schema_guard']['triggers'] = ['user_trigger']
    with pytest.raises(ValueError, match='side-effect'):
        repair.build_repair_plan(assessment)
    del assessment['repair_schema_guard']
    with pytest.raises(ValueError, match='fresh read-only'):
        repair.build_repair_plan(assessment)


def test_repair_storage_refuses_before_writes_and_reports_retained_allocation_after_failure():
    conn, plan = _repair_fixture()
    conn.allocated = 380_000_000
    with pytest.raises(RuntimeError, match='headroom'):
        _repair_step(conn, plan, 'prepare')
    assert 'extract_amount_repair' not in conn.columns
    conn.allocated = 257_330_323
    _repair_step(conn, plan, 'prepare')
    conn.growth_per_update = 2_000_000
    with pytest.raises(RuntimeError, match='rollback may retain'):
        _repair_step(conn, plan, 'batch', batch_size=1, batch_growth_allowance_bytes=1_000_000)
    assert all(row['extract_amount_repair'] is None for row in conn.rows)
    assert conn.allocated == 259_330_323  # Rolling back values does not promise storage shrinkage.


def test_repair_refuses_unowned_stage_modified_plan_and_bad_cli_combinations(tmp_path):
    import backfill_extract_amount as repair
    conn, plan = _repair_fixture()
    plan['storage_limit_bytes'] = 500_000_000
    with pytest.raises(ValueError, match='modified'):
        _repair_step(conn, plan, 'prepare')
    conn, plan = _repair_fixture()
    conn.columns['extract_amount_repair'] = {**conn.columns['extract_amount'],
                                            'name': 'extract_amount_repair', 'type': 'double precision'}
    with pytest.raises(RuntimeError, match='unowned'):
        _repair_step(conn, plan, 'prepare')
    for arguments in (['--repair-plan', 'any.json', '--run'], ['--repair-plan', 'any.json', '--postgres']):
        with patch.object(database, 'get_pg_pool') as pool, pytest.raises(SystemExit):
            repair.main(arguments)
        pool.assert_not_called()


def test_offline_repair_plan_cannot_assert_backup_restore_or_storage_proof(tmp_path):
    import json
    import backfill_extract_amount as repair
    conn = _RepairConnection([_precision_row(1, 123.45, 123)])
    report = repair._audit_postgres_snapshot(conn, 2)
    source, target = tmp_path / 'assessment.json', tmp_path / 'plan.json'
    source.write_text(json.dumps(report))
    with patch.object(database, 'get_pg_pool', side_effect=AssertionError('Offline plan borrowed pool')):
        plan = repair.main(['--repair-plan', str(source), '--output', str(target)])
    assert plan == json.loads(target.read_text())
    assert not plan['live_apply_available'] and not plan['backup_restore_verified']
    assert not plan['cashflow_parity_verified'] and plan['measured_peak_bytes'] is None
    assert 'row_data' not in target.read_text()
    assert 'schema, data and sequence' in plan['backup_requirements'][0]


@pytest.mark.parametrize('values', [(True, 1, 1), (1, -1, 1), (1, 1.5, 1), (1, 1, '20')])
def test_maintenance_storage_budget_rejects_invalid_allowances(values):
    with pytest.raises(ValueError):
        database.maintenance_storage_budget(*values)


def test_repair_binding_allows_restored_database_name_but_preserves_permission_checks():
    from copy import deepcopy
    import backfill_extract_amount as repair
    conn = _RepairConnection([_precision_row(1, 123.45, 123)])
    assessment = repair._audit_postgres_snapshot(conn, 2)
    grant = {'grantor': 'postgres', 'grantee': 'authenticated', 'table_catalog': 'postgres',
             'table_schema': 'public', 'table_name': 'client_data', 'column_name': 'extract_amount',
             'privilege_type': 'SELECT', 'is_grantable': 'NO'}
    assessment['repair_schema_guard']['grants'] = [grant]
    restored = deepcopy(assessment)
    restored['repair_schema_guard']['grants'][0]['table_catalog'] = 'precision_rehearsal_restore'
    assert repair._repair_binding(assessment) == repair._repair_binding(restored)
    assert assessment['repair_schema_guard']['grants'][0] == grant
    for key, value in [('grantor', 'other'), ('grantee', 'anon'), ('table_schema', 'other'),
                       ('table_name', 'other'), ('column_name', 'other'),
                       ('privilege_type', 'UPDATE'), ('is_grantable', 'YES')]:
        changed = deepcopy(restored)
        changed['repair_schema_guard']['grants'][0][key] = value
        assert repair._repair_binding(assessment) != repair._repair_binding(changed), key
    old_plan = repair.build_repair_plan(assessment)
    old_plan['format_version'] = 1
    old_plan['plan_sha256'] = repair._digest_records({k:v for k,v in old_plan.items() if k != 'plan_sha256'})
    with pytest.raises(ValueError, match='modified'):
        repair._validate_repair_plan(old_plan)


def test_maintenance_storage_budget_reserves_recovery_below_locked_ceiling():
    assert not database.maintenance_storage_budget(370_000_000, 8_000_000, 20_000_000)['blocked']
    assert database.maintenance_storage_budget(372_000_000, 8_000_000, 20_000_000)['blocked']


if __name__ == "__main__":
    unittest.main()


@pytest.fixture
def source_upload_metadata():
    """Client-neutral voucher export, with cheque columns deliberately unmapped."""
    from routers import import_export as uploads
    columns = [{'id': i, 'column_name': name, 'data_type': kind} for i, (name, kind) in enumerate([
        ('Voucher ID', 'Text'), ('Transaction Date', 'Date'), ('Account Detail', 'Text'),
        ('Account Name', 'Text'), ('Voucher Detail', 'Text'),
        ('Debit Amount', 'Numeric'), ('Credit Amount', 'Numeric'),
    ], 1)]
    mapping = dict(zip([c['column_name'] for c in columns],
                       ['Voucher ID', 'Date', 'Account Code', 'Account Name', 'Voucher Detail', 'Debit', 'Credit']))
    request = ImportRequest(client_id=5, template_id=1, file_id=99, sheet_name='Vouchers',
        comparison_mode='replace_range', coverage_start='2026-01-01', coverage_end='2026-10-31',
        date_delimiter='-', date_order='DMY', mappings=[
            ColumnMapping(template_column_id=c['id'], template_column_name=c['column_name'],
                          excel_column_name=mapping[c['column_name']]) for c in columns])
    row = {'Voucher ID': 'V1', 'Date': '01-04-2026', 'Account Code': 'A', 'Account Name': 'Bank',
           'Voucher Detail': 'Goods', 'Cheque #': '', 'Cheque Date': '', 'Debit': 10.01, 'Credit': 0}
    return uploads, columns, mapping, request, row


def test_unmapped_footer_blank_rows_multiplicity_and_original_positions(source_upload_metadata):
    import pandas as pd
    uploads, columns, mapping, request, row = source_upload_metadata
    footer = {'Cheque Date': 'TOTAL', 'Debit': 20.01, 'Credit': 0}
    token = uploads._source_row_numbers.set({})
    try:
        rows, controls = uploads._prepare_source_rows(pd.DataFrame([row, {}, row, footer]), request, columns, mapping)
        assert len(rows) == 2 and rows[0] == rows[1]  # Legitimate identical lines survive.
        assert [uploads._ledger_row_number(r, 0) for r in rows] == [2, 4]
        assert all('Cheque Date' not in r for r in rows)
        assert [(r['row_number'], r['kind']) for r in controls['excluded_rows']] == [(3, 'blank'), (5, 'total')]
        assert len(controls['warnings']) == 1
        assert '20.02' in controls['warnings'][0]['message'] and '0.01' in controls['warnings'][0]['message']
        assert uploads.filter_excluded_rows(rows, [4]) == [rows[0]]
        assert uploads.filter_excluded_rows(rows, [3, 5]) == rows
    finally:
        uploads._source_row_numbers.reset(token)
    assert uploads._source_row_numbers.get() is None


@pytest.mark.parametrize('change', [
    {'Voucher ID': 'TOTAL'}, {'Voucher Detail': 'Grand Total'},
    {'Voucher ID': 'TOTAL-SUPPLIES-1'},
    {'Voucher ID': '', 'Cheque Date': 'TOTAL'},  # Still carries date/account evidence.
    {'Voucher ID': '', 'Date': '', 'Account Code': '', 'Account Name': '',
     'Voucher Detail': 'Actual incomplete entry', 'Cheque Date': 'TOTAL'},
])
def test_total_word_never_hides_transaction_evidence(source_upload_metadata, change):
    import pandas as pd
    uploads, columns, mapping, request, row = source_upload_metadata
    actual = {**row, **change}
    rows, controls = uploads._prepare_source_rows(pd.DataFrame([actual]), request, columns, mapping)
    assert len(rows) == 1 and controls['excluded_count'] == 0


@pytest.mark.parametrize('endpoint', ['validate_only', 'validate_and_import'])
@pytest.mark.parametrize('invalid', [False, True])
def test_source_controls_shared_by_validation_and_direct_import(source_upload_metadata, endpoint, invalid):
    from contextlib import ExitStack
    from unittest.mock import MagicMock
    import pandas as pd
    uploads, columns, mapping, request, row = source_upload_metadata
    if endpoint == 'validate_and_import':
        request.comparison_mode = 'correction'
    source = [row, {}, {**row, 'Debit': 'bad' if invalid else 10.01},
              {'Cheque Date': 'TOTAL', 'Debit': 20.02, 'Credit': 0}]
    def read(sql, params=None):
        if 'FROM templates' in sql:
            return [{'id': 1, 'template_name': 'Transactions', 'rate_source': None}]
        if 'FROM uploaded_files' in sql:
            return [{'file_path': 'isolated.xlsx', 'file_name': 'isolated.xlsx', 'status': 'Pending'}]
        if 'FROM clients' in sql:
            return [{'id': 5}]
        if 'FROM template_column_mappings' in sql:
            return columns
        return []
    compare = MagicMock(side_effect=RuntimeError('comparison reached'))
    guards = {name: MagicMock() for name in ['_create_validation_plan', 'execute_write_pg',
        'execute_batch_pg', 'execute_batch_insert_with_ids_pg', '_persist_upload_summary', 'pg_transaction']}
    with ExitStack() as stack:
        replacements = {**guards, 'execute_query_pg': read, '_ensure_uploaded_file_hash': lambda *a: 'hash',
            '_find_prior_import_with_same_hash': lambda *a: None, 'get_rate_precision': lambda: 0,
            'get_database_storage_budget_pg': lambda *a: {'warning': False, 'blocked': False},
            'compare_transactions': compare}
        for name, value in replacements.items():
            stack.enter_context(patch.object(uploads, name, value))
        stack.enter_context(patch.object(uploads.pd, 'read_excel', return_value=pd.DataFrame(source)))
        if invalid:
            result = getattr(uploads, endpoint)(request)
            assert result['status'] == 'validation_failed'
            assert result['errors'][0]['row_number'] == 4
            assert result['source_diagnostics']['excluded_count'] == 2
            compare.assert_not_called()
        else:
            with pytest.raises(RuntimeError, match='comparison reached'):
                getattr(uploads, endpoint)(request)
            assert len(compare.call_args.kwargs['incoming_rows']) == 2
    for guard in guards.values():
        guard.assert_not_called()
    assert uploads._source_row_numbers.get() is None


def test_complete_missing_voucher_is_rejected_with_exact_row_and_no_plan(source_upload_metadata):
    from contextlib import ExitStack
    from unittest.mock import MagicMock
    import pandas as pd
    uploads, columns, mapping, request, row = source_upload_metadata
    source = pd.DataFrame([row, {}, {**row, 'Voucher ID': ''}, {'Cheque Date': 'TOTAL', 'Debit': 20.02}])
    def read(sql, params=None):
        if 'FROM templates' in sql: return [{'id': 1, 'rate_source': None}]
        if 'FROM uploaded_files' in sql: return [{'file_path': 'isolated.xlsx', 'status': 'Pending'}]
        if 'FROM clients' in sql: return [{'id': 5}]
        if 'FROM template_column_mappings' in sql: return columns
        return []
    blocked = {name: MagicMock() for name in ['compare_transactions', '_create_validation_plan', 'execute_write_pg', '_persist_upload_summary']}
    with ExitStack() as stack:
        for name, value in {**blocked, 'execute_query_pg': read, '_ensure_uploaded_file_hash': lambda *a: 'hash',
                '_find_prior_import_with_same_hash': lambda *a: None, 'get_rate_precision': lambda: 0, 'get_database_storage_budget_pg': lambda *a: {'warning': False, 'blocked': False}}.items():
            stack.enter_context(patch.object(uploads, name, value))
        stack.enter_context(patch.object(uploads.pd, 'read_excel', return_value=source))
        result = uploads.validate_only(request)
    assert result['has_errors'] is True and result['can_import'] is False
    assert result['rejected_rows'][0]['row_number'] == 4
    assert result['rejected_rows'][0]['reasons'][0]['role'] == 'document_id'
    for guard in blocked.values(): guard.assert_not_called()


def test_issue_popup_safe_text_paging_focus_and_rejections(upload_formatting_page):
    page = upload_formatting_page
    page.evaluate("""() => showValidationErrors({error_count: 0, rejected_count: 102, total_rows: 102,
        rejected_rows: Array.from({length: 102}, (_, i) => ({row_number: i + 2,
            row_data: {'Voucher': '<img src=x onerror=alert(1)>'},
            reasons: [{column: '<b>Date</b>', message: 'Required <script>source</script>'}]}))})""")
    overlay = page.locator('#validationErrorOverlay')
    assert overlay.locator('[role="dialog"]').is_visible()
    assert overlay.locator('tbody tr').count() == 50
    assert overlay.locator('img, script, b').count() == 0
    assert '<b>Date</b>' in overlay.inner_text()
    overlay.get_by_role('button', name='Next').click()
    overlay.get_by_role('button', name='Next').click()
    assert overlay.locator('tbody tr').count() == 2
    assert '101–102 of 102' in overlay.inner_text()
    page.keyboard.press('Escape')
    assert overlay.count() == 0


@pytest.mark.parametrize('kind', ['network', 'html', 'incomplete', 'expired', 'archive', 'structured'])
def test_import_failures_explain_stage_and_preserve_safe_retry(upload_formatting_page, kind):
    page = upload_formatting_page
    def respond(route):
        if kind == 'network': route.abort('connectionfailed')
        elif kind == 'html': route.fulfill(status=502, body='<html>Proxy failure</html>', content_type='text/html')
        elif kind == 'incomplete': route.fulfill(json={})
        elif kind == 'expired': route.fulfill(status=401, json={'detail': 'Not authenticated'})
        elif kind == 'archive': route.fulfill(status=400, json={'detail': 'Drive archive verification failed; no data was changed.'})
        else: route.fulfill(status=400, json={'detail': {'message': 'Source row rejected', 'rejected_rows': [
            {'row_number': 64881, 'reasons': [{'column': 'Voucher ID', 'message': 'Missing voucher'}]}]}})
    page.route('**/api/upload/import', respond)
    page.evaluate("""async () => { validationPlanId = 'ready-plan'; validationStatus = 'valid';
        await performImport({file_id: 99, validation_plan_id: 'ready-plan'}); }""")
    overlay = page.locator('#validationErrorOverlay')
    assert 'Stage: Import' in overlay.inner_text()
    assert page.evaluate('validationPlanId') == 'ready-plan'
    if kind in ['network', 'html', 'incomplete']:
        assert 'Import outcome unconfirmed' in overlay.inner_text()
        assert 'receipt' in overlay.inner_text()
        assert 'No data was changed' not in overlay.inner_text()
    elif kind == 'expired':
        assert 'Sign in again' in overlay.inner_text()
    elif kind == 'archive':
        assert 'Drive archive verification failed' in overlay.inner_text()
    else:
        assert '64881' in overlay.inner_text() and 'Missing voucher' in overlay.inner_text()


def test_source_warning_separates_controls_and_discrepancy(upload_formatting_page):
    page = upload_formatting_page
    page.evaluate("""() => showSourceDiagnostics({source_diagnostics: {excluded_count: 1,
        excluded_rows: [{row_number: 64881, kind: 'total', label: 'TOTAL'}],
        warnings: [{row_number: 64881, column: 'Debit Amount', message: 'Difference 0.01. Amounts were not altered.'}]}})""")
    assert page.locator('#validationErrorOverlay [data-severity="warning"]').count() == 1
    assert '64881' in page.locator('#validationErrorOverlay').inner_text()
    assert 'Amounts were not altered' in page.locator('#validationErrorOverlay').inner_text()


@pytest.mark.parametrize('stage', ['templates', 'columns', 'mapping', 'dates', 'upload', 'saved'])
def test_each_setup_stage_surfaces_server_detail(upload_formatting_page, stage):
    page = upload_formatting_page
    endpoint = {'templates': '/api/templates', 'columns': '/api/templates/1/upload-columns',
        'mapping': '/api/upload/auto-map', 'dates': '/api/upload/date-preview',
        'upload': '/api/import-excel', 'saved': '/api/upload/saved-mapping?client_id=5&template_id=1'}[stage]
    page.route('**' + endpoint, lambda route: route.fulfill(status=400, json={'detail': 'Specific source issue <b>keep text</b>'}))
    page.evaluate("""() => {
        fileId = 99; selectedTemplateId = 1; currentSheet = 'Transactions';
        currentMappings = [{column_id: 1, column_name: 'Transaction Date', data_type: 'Date', excel_column: 'Date'}];
    }""")
    actions = {'templates': 'loadTemplates()', 'columns': 'selectTemplate(1)',
        'mapping': 'autoMapColumns()', 'dates': 'refreshDateFormatPreview()',
        'upload': "(async () => { selectedFile = new File(['test'], 'test.xlsx'); await uploadFileToServer(selectedFile); })()",
        'saved': 'applySavedMapping()'}
    page.evaluate('async () => { await ' + actions[stage] + '; }')
    overlay = page.locator('#validationErrorOverlay')
    assert 'Specific source issue <b>keep text</b>' in overlay.inner_text()
    assert overlay.locator('b').count() == 0


def test_corrupt_local_workbook_uses_file_reading_popup_and_clears_old_plan(upload_formatting_page):
    page = upload_formatting_page
    page.evaluate("""async () => {
        fileId = 88; validationPlanId = 'old-plan'; validationStatus = 'valid';
        await handleFile(new File([new Uint8Array([0x50, 0x4b, 3, 4, 0])], 'damaged.xlsx',
            {type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));
    }""")
    page.locator('#validationErrorOverlay').wait_for()
    assert 'File reading' in page.locator('#validationErrorOverlay').inner_text()
    assert page.evaluate('fileId') is None
    assert page.evaluate('validationPlanId') is None


def test_progress_failure_is_advisory_once_and_does_not_cancel_import(upload_formatting_page):
    page = upload_formatting_page
    page.route('**/api/upload/import-status/99', lambda route: route.fulfill(status=503, json={'detail': 'Progress unavailable'}))
    page.evaluate('() => pollImportProgress(99)')
    overlay = page.locator('#validationErrorOverlay')
    overlay.wait_for()
    assert 'Stage: Import progress' in overlay.inner_text()
    assert 'still running' in overlay.inner_text()
    page.locator('#validationErrorCloseBtn').click()
    # Let two more real polling cycles run; the advisory dialog must stay closed.
    page.wait_for_timeout(2200)
    assert overlay.count() == 0
    page.evaluate('() => { clearInterval(importProgressPollInterval); importProgressPollInterval = null; }')


@pytest.mark.parametrize('label_header', ['Cheque Date', 'Voucher ID', 'Date', 'Account Code', 'Voucher Detail'])
def test_structurally_empty_total_can_be_labelled_in_any_source_column(source_upload_metadata, label_header):
    import pandas as pd
    uploads, columns, mapping, request, row = source_upload_metadata
    token = uploads._source_row_numbers.set({})
    try:
        rows, controls = uploads._prepare_source_rows(pd.DataFrame([row, {label_header: 'Grand Total:', 'Debit': 10.01, 'Credit': 0}]), request, columns, mapping)
        assert len(rows) == 1 and controls['excluded_count'] == 1
        assert controls['warnings'] == []
    finally:
        uploads._source_row_numbers.reset(token)


def test_import_summary_explains_line_preservation_correctly(upload_formatting_page):
    page = upload_formatting_page
    page.evaluate("() => showImportSummary({status: 'success', total_rows: 685, imported_count: 685})")
    summary = page.locator('#importSummaryOverlay').inner_text()
    assert 'Every valid line is preserved' in summary
    assert 'one transaction per voucher ID' not in summary
