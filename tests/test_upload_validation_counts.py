import hashlib
import os
import sqlite3
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
    _comparison_line_key,
    _row_fingerprint_token,
    _strict_duplicate_row_errors,
    _validation_request_signature,
    apply_rate_derivation,
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
    assert rows[0][-2 if operation == 'insert' else -3] == 2
    assert rows[0][-1 if operation == 'insert' else -2] == 99


@pytest.fixture
def source_order_store(monkeypatch):
    """Real metadata updates in an owned database, with an outside client sentinel."""
    import sqlite3, json
    from types import SimpleNamespace
    from routers import import_export as uploads
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE client_data (id INTEGER PRIMARY KEY,client_id INTEGER,template_id INTEGER,
        row_data TEXT,version INTEGER,is_deleted INTEGER,change_detection_status TEXT,
        source_row_number INTEGER,source_order_upload_id INTEGER)''')
    columns = [{'id':1,'column_name':'Voucher','data_type':'Text'},
               {'id':2,'column_name':'Amount','data_type':'Numeric'}]
    existing = []
    # Original source has V9 first; the previous importer inserted V1 first.
    for key, raw in [(10,{'Voucher':'V1','Amount':20}), (11,{'Voucher':'V9','Amount':10}),
                     (12,{'Voucher':'V1','Amount':20})]:
        conn.execute('INSERT INTO client_data VALUES (?,?,?,?,1,0,\'active\',NULL,NULL)',(key,5,7,json.dumps(raw)))
        existing.append({'id':key,'row_data':raw,'version':1,'extract_date':None})
    conn.execute("INSERT INTO client_data VALUES (999,6,7,'{}',1,0,'active',77,1)")
    conn.commit()
    calls = []
    def query(sql, params=None, **kw):
        if 'AS scope_row_hash' in sql:
            return existing
        ids, client, template = params
        executable = sql.replace('id = ANY(%s)', 'id IN ('+','.join('?' for _ in ids)+')')
        executable = executable.replace('%s','?').replace(' FOR UPDATE','')
        return [dict(r) for r in conn.execute(executable,(*ids,client,template))]
    def batch(active, sql, rows):
        assert active is conn
        calls.append((sql,rows))
        conn.executemany(sql.replace('%s','?'),rows)
    monkeypatch.setattr(uploads,'execute_query_pg',query)
    monkeypatch.setattr(uploads,'execute_batch_pg',batch)
    source=[{'Voucher':'V9','Amount':10},{'Voucher':'V1','Amount':20},{'Voucher':'V1','Amount':20}]
    yield SimpleNamespace(conn=conn,existing=existing,source=source,columns=columns,calls=calls,uploads=uploads)
    conn.close()


def test_unchanged_reupload_restores_positions_without_replacing_ids_values_or_repeated_lines(source_order_store):
    db=source_order_store; uploads=db.uploads
    mapping={c['column_name']:c['column_name'] for c in db.columns}
    changes=uploads.compare_transactions(5,7,db.columns,db.source,mapping,{})
    assert changes['new_count']==changes['changed_count']==changes['missing_count']==0
    assert changes['unchanged_count']==3 and changes['source_order_refresh_required']
    assert not uploads._is_duplicate_upload_noop({'id':20},changes)
    plan=uploads._build_validation_plan_payload(changes,db.columns,7,3,[],0,None,'/','DMY')
    assert plan['new']==plan['changed']==plan['missing']==[]
    assert all('row_data' not in a for a in plan['source_order'])
    request=uploads.ImportRequest(client_id=5,template_id=7,file_id=30,mappings=[])
    before=[tuple(r) for r in db.conn.execute('SELECT id,client_id,template_id,row_data,version FROM client_data ORDER BY id')]
    with db.conn:
        uploads._apply_source_order_updates(db.conn,request,plan['source_order'])
    assert [r[0] for r in db.conn.execute('SELECT id FROM client_data WHERE client_id=5 ORDER BY source_order_upload_id,source_row_number,id')] == [11,10,12]
    assert [tuple(r) for r in db.conn.execute('SELECT id,client_id,template_id,row_data,version FROM client_data ORDER BY id')]==before
    assert tuple(db.conn.execute('SELECT source_row_number,source_order_upload_id FROM client_data WHERE id=999').fetchone())==(77,1)
    for old in db.existing:
        values=db.conn.execute('SELECT source_row_number,source_order_upload_id FROM client_data WHERE id=?',(old['id'],)).fetchone()
        old.update(dict(values))
    repeat=uploads.compare_transactions(5,7,db.columns,db.source,mapping,{})
    assert not repeat['source_order_refresh_required']
    assert uploads._is_duplicate_upload_noop({'id':30},repeat)
    assert len(db.calls)==1


@pytest.mark.parametrize('change',['amount','version','position','client','missing','duplicate_plan'])
def test_stale_or_foreign_order_plan_cannot_write_any_positions(source_order_store,change):
    from fastapi import HTTPException
    db=source_order_store; uploads=db.uploads
    changes=uploads.compare_transactions(5,7,db.columns,db.source,{c['column_name']:c['column_name'] for c in db.columns},{})
    actions=changes['source_order']
    if change=='amount': db.conn.execute('UPDATE client_data SET row_data=\'{"Voucher":"V9","Amount":999}\' WHERE id=11')
    if change=='version': db.conn.execute('UPDATE client_data SET version=2 WHERE id=11')
    if change=='position': db.conn.execute('UPDATE client_data SET source_row_number=42 WHERE id=11')
    if change=='client': db.conn.execute('UPDATE client_data SET client_id=6 WHERE id=11')
    if change=='missing': db.conn.execute('DELETE FROM client_data WHERE id=11')
    if change=='duplicate_plan': actions=actions+[actions[0]]
    db.conn.commit()
    before=[tuple(r) for r in db.conn.execute('SELECT * FROM client_data ORDER BY id')]
    with pytest.raises(HTTPException) as error, db.conn:
        uploads._apply_source_order_updates(db.conn,uploads.ImportRequest(client_id=5,template_id=7,file_id=30,mappings=[]),actions)
    assert error.value.status_code==409
    assert [tuple(r) for r in db.conn.execute('SELECT * FROM client_data ORDER BY id')]==before
    assert db.calls==[]


def test_metadata_only_plan_import_keeps_financial_counts_zero_and_recovers_its_receipt(source_order_store,monkeypatch):
    import json
    from contextlib import contextmanager
    db=source_order_store; uploads=db.uploads
    changes=uploads.compare_transactions(5,7,db.columns,db.source,{c['column_name']:c['column_name'] for c in db.columns},{})
    payload=uploads._build_validation_plan_payload(changes,db.columns,7,3,[],0,None,'/','DMY')
    request=uploads.ImportRequest(client_id=5,template_id=7,file_id=30,mappings=[],validation_plan_id='a'*32)
    state={'status':'Validated','receipt':None}
    actual_query=uploads.execute_query_pg
    def query(sql,params=None,**kwargs):
        if 'FROM templates' in sql: return [{'id':7,'template_name':'Transactions','rate_source':None}]
        if 'FROM uploaded_files' in sql: return [{'file_path':'unused.xlsx','file_name':'report.xlsx','status':state['status']}]
        if 'FROM clients' in sql: return [{'id':5,'client_name':'Test client'}]
        if 'FROM template_column_mappings' in sql: return db.columns
        if 'FROM upload_validation_plans' in sql: return [{'status':'ready','expires_at':None}]
        if 'FROM upload_lifecycle_runs' in sql:
            return [{'status':'applied','request_signature':uploads._validation_request_signature(request),'result_json':state['receipt']}] if state['receipt'] else []
        if 'FROM client_data' in sql: return actual_query(sql,params,**kwargs)
        return []
    def write(sql,params=None):
        if "SET status = 'Imported'" in sql: state['status']='Imported'
        if "SET status = 'applied'" in sql: state['receipt']=json.loads(params[0])
    @contextmanager
    def transaction():
        with db.conn: yield db.conn
    for name,replacement in {
        'execute_query_pg':query,'execute_write_pg':write,'pg_transaction':transaction,
        '_load_validation_plan':lambda *a:{'plan_json':payload},
        'prepare_lifecycle':lambda *a:{'operation_id':request.validation_plan_id,'ids':[],'lifecycle':{}},
        'guard_lifecycle':lambda *a:None,'_check_upload_storage_budget':lambda *a:None,
        'load_treasury_mapping_postgres':lambda *a:{},
        'resolve_upload_occurrences_postgres':lambda *a,**kw:{'eligible':True},
        'invalidate_cashflow_summary_read_models':lambda *a,**kw:None,
        '_prepare_cashflow_snapshot_background':lambda *a:None,
        '_persist_upload_summary':lambda *a,**kw:None,'log_audit':lambda *a:None,
        'execute_batch_insert_with_ids_pg':lambda *a,**kw:pytest.fail('Unchanged rows must not be inserted'),
    }.items(): monkeypatch.setattr(uploads,name,replacement)
    try:
        result=uploads._import_validation_plan(request)
        assert result['status']=='success' and result['source_order_refreshed']
        assert result['imported_count']==result['updated_count']==result['deleted_count']==0
        assert len(db.calls)==1 and state['receipt']['total_rows']==3
        retry=uploads._import_validation_plan(request)
        assert retry['recovered_commit'] and retry['imported_count']==0
        assert len(db.calls)==1
    finally: uploads.IMPORT_PROGRESS.pop(30,None)


def test_source_positions_roll_back_with_a_later_import_failure(source_order_store):
    db=source_order_store; uploads=db.uploads
    changes=uploads.compare_transactions(5,7,db.columns,db.source,{c['column_name']:c['column_name'] for c in db.columns},{})
    before=[tuple(r) for r in db.conn.execute('SELECT * FROM client_data ORDER BY id')]
    with pytest.raises(RuntimeError,match='later stage'),db.conn:
        uploads._apply_source_order_updates(db.conn,uploads.ImportRequest(client_id=5,template_id=7,file_id=30,mappings=[]),changes['source_order'])
        raise RuntimeError('later stage failed')
    assert [tuple(r) for r in db.conn.execute('SELECT * FROM client_data ORDER BY id')]==before


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
        assert 'Deleted — absent from this complete report' in page.locator('#uploadReplacementCounts').inner_text()
        assert '3' in page.locator('#uploadReplacementCounts').inner_text()
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
    assert 'Empty report — clear this period' in page.locator('#uploadConfirmEmpty').locator('..').text_content()
    page.evaluate('''() => {
        lastChangePreview = {unchanged_count: 0, new_count: 2, missing_count: 2,
            changed_voucher_count: 1, removed_voucher_count: 0,
            date_moves: [{voucher_id: '<b>V1</b>', previous_dates: ['2026-03-31'], incoming_dates: ['2026-04-15']}]};
        window.dateReviewClosed = false;
        reviewUploadReplacement({coverage_start: '2026-04-01', coverage_end: '2026-04-30'})
            .then(() => window.dateReviewClosed = true);
    }''')
    assert page.locator('#uploadReplacementDateMoves').is_visible()
    assert page.locator('#uploadReplacementDateMoveRows tr').inner_text() == '<b>V1</b>\t2026-03-31\t2026-04-15'
    assert page.locator('#uploadReplacementDateMoves b').count() == 0
    assert page.locator('#uploadReplacementChangedVouchers').inner_text() == '1'
    page.locator('#uploadReplacementCancel').click()
    page.wait_for_function('window.dateReviewClosed')


class UploadValidationCountTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        database.DB_NAME = os.path.join(self.tmpdir.name, "test.db")
        init_db()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_saved_mapping_comparison_setting_is_nullable_on_fresh_sqlite(self):
        conn = sqlite3.connect(database.DB_NAME)
        try:
            columns = {row[1]: row for row in conn.execute('PRAGMA table_info(saved_column_mappings)')}
        finally:
            conn.close()
        self.assertIn('compare_for_changes', columns)
        self.assertEqual(columns['compare_for_changes'][3], 0)
        self.assertIsNone(columns['compare_for_changes'][4])

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

    def test_numeric_format_equivalence_keeps_the_stored_row_unchanged(self):
        template_cols = [
            {'id': 1, 'column_name': 'Voucher', 'data_type': 'Text'},
            {'id': 2, 'column_name': 'Amount', 'data_type': 'Numeric'},
        ]
        old_row = {'Voucher': 'V-1', 'Amount': 70000}
        self.assertEqual(
            _canonical_source_identity(template_cols, old_row, 1),
            _canonical_source_identity(template_cols, {'Voucher': 'V-1', 'Amount': 70000.0}, 1),
        )
        existing = [{
            'id': 41, 'row_data': old_row, 'version': 3,
            'extract_date': None, 'validation_status': 'Verified',
            'verification_detail_id': 7, 'source_identity': 'stable-voucher-line',
            'source_row_number': 2, 'source_order_upload_id': 90,
        }]

        with patch('routers.import_export.execute_query_pg', return_value=existing):
            result = compare_transactions(
                1, 1, template_cols,
                [{'Voucher': 'V-1', 'Amount': 70000.0}],
                {'Voucher': 'Voucher', 'Amount': 'Amount'}, {},
            )

        self.assertEqual(result['unchanged_count'], 1)
        self.assertEqual(result['unchanged'], [41])
        self.assertEqual(result['changed_count'], 0)
        self.assertEqual(result['new_count'], 0)
        self.assertEqual(result['missing_count'], 0)
        self.assertEqual(existing[0]['row_data'], {'Voucher': 'V-1', 'Amount': 70000})
        self.assertEqual(existing[0]['version'], 3)
        self.assertEqual(existing[0]['source_identity'], 'stable-voucher-line')

    def test_numeric_comparison_keeps_real_cent_changes_and_blank_distinct_from_zero(self):
        template_cols = [
            {'id': 1, 'column_name': 'Voucher', 'data_type': 'Text'},
            {'id': 2, 'column_name': 'Amount', 'data_type': 'Numeric'},
        ]

        def compare(old_amount, new_amount):
            existing = [{
                'id': 42, 'row_data': {'Voucher': 'V-1', 'Amount': old_amount},
                'version': 1, 'extract_date': None,
                'validation_status': 'Pending', 'verification_detail_id': None,
                'source_identity': 'stable-voucher-line',
                'source_row_number': 2, 'source_order_upload_id': 90,
            }]
            with patch('routers.import_export.execute_query_pg', return_value=existing):
                return compare_transactions(
                    1, 1, template_cols,
                    [{'Voucher': 'V-1', 'Amount': new_amount}],
                    {'Voucher': 'Voucher', 'Amount': 'Amount'}, {},
                )

        cent_change = compare(70000, 70000.01)
        self.assertEqual(cent_change['changed_count'], 1)
        self.assertEqual(cent_change['changed'][0]['changed_fields'], ['Amount'])

        for blank in (None, ''):
            with self.subTest(blank=blank):
                blank_to_zero = compare(blank, 0)
                self.assertEqual(blank_to_zero['changed_count'], 1)
                self.assertEqual(blank_to_zero['changed'][0]['changed_fields'], ['Amount'])

    def test_text_leading_zero_changes_are_not_numeric_normalized(self):
        template_cols = [
            {'id': 1, 'column_name': 'Voucher', 'data_type': 'Text'},
            {'id': 2, 'column_name': 'Reference', 'data_type': 'Text'},
            {'id': 3, 'column_name': 'Amount', 'data_type': 'Numeric'},
        ]
        existing = [{
            'id': 43, 'row_data': {'Voucher': 'V-1', 'Reference': '0007', 'Amount': 12},
            'version': 1, 'extract_date': None,
            'validation_status': 'Pending', 'verification_detail_id': None,
            'source_row_number': 2, 'source_order_upload_id': 90,
        }]

        with patch('routers.import_export.execute_query_pg', return_value=existing):
            result = compare_transactions(
                1, 1, template_cols,
                [{'Voucher': 'V-1', 'Reference': '7', 'Amount': 12.0}],
                {'Voucher': 'Voucher', 'Reference': 'Reference', 'Amount': 'Amount'}, {},
            )

        self.assertEqual(result['changed_count'], 1)
        self.assertEqual(result['changed'][0]['client_data_id'], 43)
        self.assertEqual(result['changed'][0]['changed_fields'], ['Reference'])

    def test_repeated_numeric_equivalent_lines_keep_each_existing_occurrence(self):
        template_cols = [
            {'id': 1, 'column_name': 'Voucher', 'data_type': 'Text'},
            {'id': 2, 'column_name': 'Amount', 'data_type': 'Numeric'},
        ]
        old_row = {'Voucher': 'V-1', 'Amount': 70000}
        existing = [{
            'id': row_id, 'row_data': dict(old_row), 'version': 1,
            'extract_date': None, 'validation_status': 'Pending',
            'verification_detail_id': None, 'source_identity': f'line-{row_id}',
            'source_row_number': row_id - 49, 'source_order_upload_id': 90,
        } for row_id in (50, 51)]

        with patch('routers.import_export.execute_query_pg', return_value=existing):
            result = compare_transactions(
                1, 1, template_cols,
                [{'Voucher': 'V-1', 'Amount': 70000.0}, {'Voucher': 'V-1', 'Amount': 70000.00}],
                {'Voucher': 'Voucher', 'Amount': 'Amount'}, {},
            )

        self.assertEqual(result['unchanged_count'], 2)
        self.assertEqual(result['unchanged'], [50, 51])
        self.assertEqual(result['changed_count'], 0)
        self.assertEqual(result['new_count'], 0)
        self.assertEqual(result['missing_count'], 0)
        self.assertEqual([row['source_identity'] for row in existing], ['line-50', 'line-51'])

    def test_unique_descriptive_anchors_pair_numeric_changes_across_upload_templates(self):
        cases = [
            (
                'Purchase Order',
                [
                    {'id': 1, 'column_name': 'PO No', 'data_type': 'Text'},
                    {'id': 2, 'column_name': 'Item Code', 'data_type': 'Text'},
                    {'id': 3, 'column_name': 'Rate', 'data_type': 'Numeric'},
                    {'id': 4, 'column_name': 'Amount', 'data_type': 'Numeric'},
                ],
                'PO No', 'Item Code', 'Rate',
            ),
            (
                'Store Receiving',
                [
                    {'id': 1, 'column_name': 'GRN No', 'data_type': 'Text'},
                    {'id': 2, 'column_name': 'Item Code', 'data_type': 'Text'},
                    {'id': 3, 'column_name': 'Rate', 'data_type': 'Numeric'},
                    {'id': 4, 'column_name': 'Amount', 'data_type': 'Numeric'},
                ],
                'GRN No', 'Item Code', 'Rate',
            ),
            (
                'Transaction Report',
                [
                    {'id': 1, 'column_name': 'Voucher', 'data_type': 'Text'},
                    {'id': 2, 'column_name': 'Account Code', 'data_type': 'Text'},
                    {'id': 3, 'column_name': 'Narration', 'data_type': 'Text'},
                    {'id': 4, 'column_name': 'Debit Amount', 'data_type': 'Numeric'},
                    {'id': 5, 'column_name': 'Credit Amount', 'data_type': 'Numeric'},
                ],
                'Voucher', 'Account Code', 'Debit Amount',
            ),
        ]

        for template_name, template_cols, document_field, anchor_field, amount_field in cases:
            with self.subTest(template=template_name):
                old_rows = [
                    {document_field: 'DOC-1', anchor_field: f'LINE-{i}', amount_field: 10 + i}
                    for i in range(3)
                ]
                new_rows = [
                    {**old, amount_field: old[amount_field] + 0.25}
                    for old in old_rows
                ]
                existing = [{
                    'id': 70 + i, 'row_data': old, 'version': 1,
                    'extract_date': None, 'validation_status': 'Pending',
                    'verification_detail_id': None,
                    'source_identity': f'{template_name}-{i}',
                    'source_row_number': i + 2, 'source_order_upload_id': 90,
                } for i, old in enumerate(old_rows)]
                mappings = {field: field for row in new_rows for field in row}

                with patch('routers.import_export.execute_query_pg', return_value=existing):
                    result = compare_transactions(
                        1, 1, template_cols, new_rows, mappings, {},
                    )

                self.assertEqual(result['changed_count'], 3)
                self.assertEqual(result['new_count'], 0)
                self.assertEqual(result['missing_count'], 0)
                self.assertEqual(
                    [item['changed_fields'] for item in result['changed']],
                    [[amount_field], [amount_field], [amount_field]],
                )

    def test_duplicate_descriptive_anchors_are_not_guessed_as_line_changes(self):
        template_cols = [
            {'id': 1, 'column_name': 'PO No', 'data_type': 'Text'},
            {'id': 2, 'column_name': 'Item Code', 'data_type': 'Text'},
            {'id': 3, 'column_name': 'Rate', 'data_type': 'Numeric'},
        ]
        old_rows = [
            {'PO No': 'PO-1', 'Item Code': 'SAME', 'Rate': 10},
            {'PO No': 'PO-1', 'Item Code': 'SAME', 'Rate': 20},
        ]
        new_rows = [
            {'PO No': 'PO-1', 'Item Code': 'SAME', 'Rate': 10.5},
            {'PO No': 'PO-1', 'Item Code': 'SAME', 'Rate': 20.5},
        ]
        existing = [{
            'id': row_id, 'row_data': old, 'version': 1,
            'extract_date': None, 'validation_status': 'Pending',
            'verification_detail_id': None, 'source_identity': f'line-{row_id}',
            'source_row_number': row_id, 'source_order_upload_id': 90,
        } for row_id, old in zip((80, 81), old_rows)]

        from fastapi import HTTPException
        with patch('routers.import_export.execute_query_pg', return_value=existing):
            with self.assertRaises(HTTPException) as error:
                compare_transactions(
                    1, 1, template_cols, new_rows,
                    {'PO No': 'PO No', 'Item Code': 'Item Code', 'Rate': 'Rate'}, {},
                )

        self.assertEqual(error.exception.status_code, 409)

    def test_history_api_displays_unique_numeric_replacement_as_changed(self):
        from routers import data as data_router

        columns = [
            {'column_name': 'PO No', 'data_type': 'Text'},
            {'column_name': 'Item Code', 'data_type': 'Text'},
            {'column_name': 'Rate', 'data_type': 'Numeric'},
        ]
        old_row = {'PO No': 'PO-1', 'Item Code': 'ITEM-1', 'Rate': 58}
        replacement_row = {'PO No': 'PO-1', 'Item Code': 'ITEM-1', 'Rate': 58.4}
        old_history = {
            'id': 90, 'client_data_id': 40, 'action': 'deleted',
            'changed_at': None, 'changed_by': 'Analyst',
            'source_upload_id': 10, 'replacing_upload_id': 11,
            'source_identity': 'old-identity', 'row_data': old_row,
            'changed_fields': None, 'ledger_account_code': None,
            'ledger_account_name': None,
        }

        def execute(sql, params=None, **_kwargs):
            if 'FROM clients' in sql:
                return [{'id': 1}]
            if 'FROM templates' in sql:
                return [{'id': 3, 'template_name': 'Purchase Order', 'data_purpose': 'standard'}]
            if 'COUNT(*) AS total' in sql:
                return [{'total': 1}]
            if 'SELECT h.id AS history_id' in sql:
                return [{
                    'history_id': 90,
                    'replacement_client_data_id': 41,
                    'replacement_row_data': replacement_row,
                }]
            if 'SELECT h.id, h.client_data_id, h.action' in sql:
                return [dict(old_history)]
            raise AssertionError(f'Unexpected history query: {sql}')

        with patch.object(data_router, 'get_template_columns_ordered', return_value=columns), \
             patch.object(data_router, 'execute_query_pg', side_effect=execute):
            result = data_router.get_client_data_history(
                client_id=1, template_id=3, page=1, limit=25,
                action='changed', ledger_account_code=None,
            )

        self.assertEqual(result['total'], 1)
        self.assertEqual(result['rows'][0]['action'], 'deleted')
        self.assertEqual(result['rows'][0]['display_action'], 'changed')
        self.assertEqual(result['rows'][0]['replacement_client_data_id'], 41)
        self.assertEqual(result['rows'][0]['changed_fields'], {
            'Rate': {'old_value': 58, 'new_value': 58.4},
        })

    def test_new_zero_line_does_not_collide_with_existing_blank_line_identity(self):
        template_cols = [
            {'id': 1, 'column_name': 'Voucher', 'data_type': 'Text'},
            {'id': 2, 'column_name': 'Amount', 'data_type': 'Numeric'},
        ]
        old_row = {'Voucher': 'V-1', 'Amount': None}
        legacy_identity_input = (
            f"{_comparison_line_key(template_cols, old_row, 1)}"
            f"|fingerprint:{_row_fingerprint_token(old_row)}"
        )
        legacy_identity = hashlib.sha256(legacy_identity_input.encode('utf-8')).hexdigest()
        existing = [{
            'id': 60, 'row_data': old_row, 'version': 1, 'extract_date': None,
            'validation_status': 'Pending', 'verification_detail_id': None,
            'source_identity': legacy_identity, 'source_row_number': 2,
            'source_order_upload_id': 90,
        }]

        with patch('routers.import_export.execute_query_pg', return_value=existing):
            result = compare_transactions(
                1, 1, template_cols,
                [{'Voucher': 'V-1', 'Amount': 0}, {'Voucher': 'V-1', 'Amount': None}],
                {'Voucher': 'Voucher', 'Amount': 'Amount'}, {},
            )

        self.assertEqual(result['unchanged'], [60])
        self.assertEqual(result['unchanged_count'], 1)
        self.assertEqual(result['new_count'], 1)
        self.assertEqual(result['changed_count'], 0)
        self.assertEqual(result['missing_count'], 0)
        self.assertNotEqual(result['new_source_identities'][0], legacy_identity)
        self.assertEqual(existing[0]['source_identity'], legacy_identity)

    def test_imported_rates_stay_exact_while_derived_rates_use_configured_precision(self):
        template_cols = [
            {'id': 1, 'column_name': 'Rate', 'data_type': 'Numeric'},
            {'id': 2, 'column_name': 'Quantity', 'data_type': 'Numeric'},
            {'id': 3, 'column_name': 'Amount', 'data_type': 'Numeric'},
        ]
        imported = {'Rate': 732.5, 'Quantity': 1, 'Amount': 732.5}
        apply_rate_derivation(imported, template_cols, 'imported', 0)
        self.assertEqual(imported['Rate'], 732.5)

        derived = {'Rate': None, 'Quantity': 3, 'Amount': 100}
        apply_rate_derivation(derived, template_cols, 'derived', 2)
        self.assertEqual(derived['Rate'], 33.33)

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
            "source_row_number",
            "source_order_upload_id",
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


def _arrange_ledger_upload_page(page, accounts=None, *, show_mapping=True):
    accounts = accounts if accounts is not None else [
        {'account_code':'A-1','account_name':'Bank <A>'},
        {'account_code':'B-2','account_name':'Cash B'},
        {'account_code':'EXP-1','account_name':'Rent expense'}]
    columns = [{'id':i+1,'column_name':name,'data_type':kind} for i,(name,kind) in enumerate([
        ('Voucher ID','Text'),('Transaction Date','Date'),('Voucher Detail','Text'),
        ('Debit Amount','Numeric'),('Credit Amount','Numeric'),('Balance','Text')])]
    page.route('**/api/templates/7/upload-columns',lambda route:route.fulfill(json={
        'columns':columns,'total_columns':6,'data_purpose':'account_ledger',
        'ledger_columns':dict(zip(['voucher','date','description','debit','credit','balance'],range(1,7)))}))
    page.route('**/api/upload/ledger-accounts?client_id=5',lambda route:route.fulfill(json={'accounts':accounts}))
    page.evaluate("async () => { templateDropdown.appendChild(new Option('Account Ledger','7')); templateDropdown.value='7'; await selectTemplate(7); }")
    if show_mapping:
        # The field belongs to the ordinary mapping/action panel, never a header.
        page.evaluate("() => { leftPanel.style.display='block'; mappingPanel.style.display='block'; modeActions.style.display='flex'; }")
    return columns


def _arrange_staged_ledger_mapping(page, columns):
    page.evaluate("""columns => {
        fileId=99; currentSheet='Ledger';
        excelHeaders=columns.map(c=>c.column_name);
        currentMappings=columns.map(c=>({column_id:c.id,column_name:c.column_name,data_type:c.data_type,excel_column:c.column_name,is_matched:true}));
        renderMapping(currentMappings); leftPanel.style.display='block'; mappingPanel.style.display='block';
        modeActions.style.display='block'; importBtn.disabled=false; setDateFormatGroupVisible(true);
    }""",columns)


def test_ledger_balance_compare_setting_defaults_off_and_can_be_enabled(upload_formatting_page):
    page = upload_formatting_page
    columns = _arrange_ledger_upload_page(page)
    _arrange_staged_ledger_mapping(page, columns)
    balance = page.locator('.mapping-table tbody tr').filter(has_text='Balance')
    balance_checkbox = balance.locator('input[data-compare-column]')
    assert not balance_checkbox.is_checked()
    assert page.locator('.mapping-table tbody tr input[data-compare-column]').count() == 6
    assert page.locator('.mapping-table tbody tr input[data-compare-column]').nth(0).is_checked()
    balance_checkbox.check()
    assert page.evaluate("() => currentMappings.find(item => item.column_name === 'Balance').compare_for_changes")


def _select_upload_ledger_account(page, code):
    page.locator('#ledgerAccountSelect').fill(code)
    page.locator('#ledgerAccountOptions [role="option"]').filter(has_text=code).click()


def test_ledger_account_and_existing_coverage_fields_reach_validate_and_invalidate_plan(upload_formatting_page):
    page=upload_formatting_page
    columns=_arrange_ledger_upload_page(page)
    assert page.locator('#ledgerUploadContext').is_visible()
    page.locator('#ledgerAccountSelect').focus()
    assert page.get_by_role('option', name='Bank <A> — A-1', exact=True).is_visible()
    assert page.locator('#ledgerUploadContext a, #ledgerUploadContext img').count()==0
    _select_upload_ledger_account(page, 'A-1')
    _arrange_staged_ledger_mapping(page,columns)
    page.locator('#uploadCoverageStart').fill('2026-01-01')
    page.locator('#uploadCoverageEnd').fill('2026-01-31')
    sent=[]
    page.route('**/api/upload/validate',lambda route:(sent.append(route.request.post_data_json),route.fulfill(json={
        'can_import':True,'validation_plan_id':'ledger-plan','total_rows':1,'has_errors':False,
        'ledger_evidence':{'opening':'0','closing':'-10','control_rows':[{'row_number':2},{'row_number':4}]}})))
    page.locator('#importBtn').click()
    page.wait_for_function("validationStatus==='valid'")
    assert sent[0]['ledger_account_code']=='A-1'
    assert sent[0]['coverage_start']==sent[0]['ledger_period_start']=='2026-01-01'
    assert sent[0]['coverage_end']==sent[0]['ledger_period_end']=='2026-01-31'
    assert sent[0]['force_include_rows']==[]
    assert 'Opening control: 0' in page.locator('#ledgerEvidenceSummary').inner_text()
    _select_upload_ledger_account(page, 'B-2')
    assert page.evaluate('validationPlanId') is None
    assert page.locator('#ledgerEvidenceSummary').is_hidden()
    page.locator('#comparisonModeDropdown').select_option('correction')
    assert page.locator('#uploadCoverageFields').is_visible()
    assert page.locator('#uploadEmptyConfirmLabel').is_hidden()
    page.locator('#importBtn').click()
    page.wait_for_function("validationStatus==='valid'")
    assert sent[1]['coverage_start'] is None and sent[1]['ledger_period_start']=='2026-01-01'
    page.route('**/api/upload/date-preview',lambda route:route.fulfill(json={'samples':[]}))
    page.locator('#dateFormatTriggerBtn').click()
    page.locator('#dateOrderDropdown').select_option('MDY')
    assert page.evaluate('validationPlanId') is None


@pytest.mark.parametrize('accounts', [[],None])
def test_missing_or_unselected_ledger_account_blocks_validate_and_offers_action(upload_formatting_page, accounts):
    page=upload_formatting_page
    columns=_arrange_ledger_upload_page(page,accounts)
    _arrange_staged_ledger_mapping(page,columns)
    sent=[]
    page.route('**/api/upload/validate',lambda route:(sent.append(True),route.fulfill(json={})))
    page.locator('#importBtn').click()
    assert 'Select a current Trial Balance posting account' in page.locator('#validationErrorOverlay').inner_text()
    assert sent==[]
    if accounts==[]:
        assert 'No posting accounts found' in page.locator('#ledgerAccountHelp').inner_text()
        assert page.locator('#ledgerAccountSelect').is_disabled()


def test_ledger_context_is_in_review_and_summary_and_rejected_rows_cannot_be_overridden(upload_formatting_page):
    page=upload_formatting_page
    _arrange_ledger_upload_page(page)
    _select_upload_ledger_account(page, 'A-1')
    page.evaluate("""() => {
        uploadCoverageStart.value='2026-01-01';uploadCoverageEnd.value='2026-01-31';
        setRejectedRows([{row_number:3,reasons:[{column:'Transaction Date',message:'Date is blank'}]}]);
        reviewUploadReplacement({ledger_account_code:'A-1',coverage_start:'2026-01-01',coverage_end:'2026-01-31'});
    }""")
    assert 'Bank <A> (A-1)' in page.locator('#uploadReplacementScope').inner_text()
    page.locator('#uploadReplacementCancel').click()
    assert page.locator('#rowRejectionList input[type="checkbox"]').count()==0
    page.evaluate("() => showImportSummary({status:'success',total_rows:1,imported_count:1})")
    assert 'Bank <A> (A-1)' in page.locator('#importSummaryOverlay').inner_text()
    assert page.locator('#importSummaryOverlay b').count()==0


def test_client_mismatch_blocks_ledger_validation_and_session_restore(upload_formatting_page):
    page=upload_formatting_page
    columns=_arrange_ledger_upload_page(page)
    _select_upload_ledger_account(page, 'A-1')
    _arrange_staged_ledger_mapping(page,columns)
    page.evaluate('() => { window.getSelectedClientId=()=>6; }')
    page.locator('#importBtn').click()
    assert 'Select a current Trial Balance posting account' in page.locator('#validationErrorOverlay').inner_text()
    result=page.evaluate("""async () => {
        sessionStorage.setItem(UPLOAD_SESSION_STORAGE_KEY,JSON.stringify({clientId:5,templateId:7,ledgerAccountCode:'A-1'}));
        await restoreUploadSessionState(); return sessionStorage.getItem(UPLOAD_SESSION_STORAGE_KEY);
    }""")
    assert result is None


def test_standard_template_switch_hides_ledger_context_and_sends_no_account(upload_formatting_page):
    page=upload_formatting_page
    _arrange_ledger_upload_page(page)
    _select_upload_ledger_account(page, 'A-1')
    page.evaluate("() => { const note=document.getElementById('ledgerEvidenceSummary'); note.textContent='Old balance'; note.hidden=false; }")
    page.route('**/api/templates/8/upload-columns',lambda route:route.fulfill(json={
        'columns':[{'id':1,'column_name':'Voucher','data_type':'Text'}],'total_columns':1,'data_purpose':'standard'}))
    page.evaluate("async () => { templateDropdown.appendChild(new Option('Ordinary','8')); await selectTemplate(8); }")
    assert page.locator('#ledgerUploadContext').is_hidden()
    assert page.locator('#ledgerEvidenceSummary').is_hidden()
    assert page.evaluate('ledgerSelectedAccountCode')==''
    assert page.evaluate('validationPlanId') is None
    _arrange_staged_ledger_mapping(page,[{'id':1,'column_name':'Voucher','data_type':'Text'}])
    page.locator('#comparisonModeDropdown').select_option('correction')
    assert page.locator('#uploadCoverageFields').is_hidden()
    sent=[]
    page.route('**/api/upload/validate',lambda route:(sent.append(route.request.post_data_json),route.fulfill(json={'has_errors':False,'can_import':False})))
    page.locator('#importBtn').click()
    page.wait_for_function("validationStatus==='ignored'")
    assert sent[0]['ledger_account_code'] is None and sent[0]['ledger_period_start'] is None


def test_validation_response_for_previous_account_is_discarded(upload_formatting_page):
    page=upload_formatting_page
    columns=_arrange_ledger_upload_page(page)
    _select_upload_ledger_account(page, 'A-1')
    _arrange_staged_ledger_mapping(page,columns)
    page.locator('#uploadCoverageStart').fill('2026-01-01')
    page.locator('#uploadCoverageEnd').fill('2026-01-31')
    waiting=[]
    page.route('**/api/upload/validate',lambda route:waiting.append(route))
    page.locator('#importBtn').click()
    page.wait_for_function('uploadActionInFlight')
    _select_upload_ledger_account(page, 'B-2')
    assert waiting
    waiting[0].fulfill(json={'has_errors':False,'can_import':True,'validation_plan_id':'stale-A-plan'})
    page.locator('#validationErrorOverlay').wait_for()
    assert 'settings changed during validation' in page.locator('#validationErrorOverlay').inner_text()
    assert page.evaluate('validationPlanId') is None
    assert page.evaluate('ledgerSelectedAccountCode')=='B-2'


def test_import_summary_uses_submitted_account_even_if_selection_changes(upload_formatting_page):
    page=upload_formatting_page
    _arrange_ledger_upload_page(page)
    _select_upload_ledger_account(page, 'A-1')
    requests=[]
    page.route('**/api/upload/import',lambda route:requests.append(route))
    page.evaluate("""() => {
        performImport({file_id:99,ledger_account_code:'A-1',ledger_period_start:'2026-01-01',ledger_period_end:'2026-01-31'});
    }""")
    _select_upload_ledger_account(page, 'B-2')
    assert requests
    requests[0].fulfill(json={'status':'success','total_rows':1,'imported_count':1})
    page.locator('#importSummaryOverlay').wait_for()
    text=page.locator('#importSummaryOverlay').inner_text()
    assert 'Bank <A> (A-1)' in text and 'Cash B (B-2)' not in text


def test_validated_ledger_controls_are_labelled_separately_from_junk_rows(upload_formatting_page):
    page=upload_formatting_page
    columns=_arrange_ledger_upload_page(page)
    _select_upload_ledger_account(page, 'A-1')
    _arrange_staged_ledger_mapping(page,columns)
    page.evaluate("""() => {
        const workbook=XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(workbook,XLSX.utils.aoa_to_sheet([excelHeaders,
            ['','','Opening Balance',0,0,'100 DR'],['V1','15/01/2026','Payment',0,10,'90 DR'],
            ['','','Closing Balance',0,0,'90 DR']]),'Ledger');
        workbookData=XLSX.write(workbook,{type:'array',bookType:'xlsx'});renderRowExclusionGrid();
    }""")
    page.locator('#uploadCoverageStart').fill('2026-01-01')
    page.locator('#uploadCoverageEnd').fill('2026-01-31')
    page.route('**/api/upload/validate',lambda route:route.fulfill(json={'has_errors':False,'can_import':True,
        'validation_plan_id':'control-plan','ledger_evidence':{'opening':'100','closing':'90','control_rows':[{'row_number':2},{'row_number':4}]}}))
    page.locator('#importBtn').click()
    page.wait_for_function("validationStatus==='valid'")
    assert page.locator('#rowExclusionBody tr.row-flagged').count()==0
    assert 'not a transaction' in page.locator('#rowExclusionBody tr').first.locator('.row-exclusion-rownum-col').get_attribute('title')
    assert 'source rows selected; balance controls are recorded separately' in page.locator('#rowExclusionCounter').inner_text()


def test_ledger_uses_standard_mapping_with_one_account_field_above_validate(upload_formatting_page):
    page = upload_formatting_page
    columns = _arrange_ledger_upload_page(page, show_mapping=False)
    assert page.locator('#ledgerUploadContext').is_hidden()
    _arrange_staged_ledger_mapping(page, columns)
    assert page.locator('#mappingPanel .section-title').inner_text().lower().startswith('🔗 column mapping')
    assert page.locator('#mappingBody tr').count() == 6
    assert page.locator('#ledgerUploadContext select, #ledgerUploadContext input').count() == 1
    assert page.locator('#ledgerAccountSelect').get_attribute('required') is not None
    assert page.locator('#ledgerUploadContext h2, #ledgerAccountSearch, #reloadLedgerAccountsBtn').count() == 0
    assert page.locator('#ledgerAccountHelp').is_hidden()
    page.locator('#ledgerAccountSelect').focus()
    assert page.get_by_role('option', name='Rent expense — EXP-1', exact=True).is_visible()
    assert page.evaluate("() => document.getElementById('ledgerUploadContext').nextElementSibling.contains(importBtn)")
    assert page.locator('#ledgerAccountSelect').bounding_box()['y'] < page.locator('#importBtn').bounding_box()['y']


def test_non_cash_bank_account_reaches_upload_validation(upload_formatting_page):
    page = upload_formatting_page
    columns = _arrange_ledger_upload_page(page)
    _arrange_staged_ledger_mapping(page, columns)
    _select_upload_ledger_account(page, 'EXP-1')
    page.locator('#uploadCoverageStart').fill('2026-01-01')
    page.locator('#uploadCoverageEnd').fill('2026-01-31')
    sent = []
    page.route('**/api/upload/validate', lambda route: (
        sent.append(route.request.post_data_json), route.fulfill(json={'has_errors':False, 'can_import':False})))
    page.locator('#importBtn').click()
    page.wait_for_function("validationStatus==='ignored'")
    assert sent[0]['ledger_account_code'] == 'EXP-1'


def test_upload_ledger_search_uses_names_codes_and_explicit_keyboard_selection(upload_formatting_page):
    page = upload_formatting_page
    _arrange_ledger_upload_page(page)
    field = page.locator('#ledgerAccountSelect')
    field.fill('rEnT')
    assert page.locator('#ledgerAccountOptions').get_by_role('option').all_text_contents() == ['Rent expense — EXP-1']
    assert page.evaluate('ledgerSelectedAccountCode') == ''
    field.press('Enter')
    assert field.input_value() == 'Rent expense — EXP-1'
    assert page.evaluate('ledgerSelectedAccountCode') == 'EXP-1'
    assert field.get_attribute('aria-expanded') == 'false'
    field.fill('b-2')
    assert page.evaluate('ledgerSelectedAccountCode') == ''
    field.press('ArrowDown')
    assert field.get_attribute('aria-activedescendant') == page.locator('#ledgerAccountOptions').get_by_role('option').get_attribute('id')
    field.press('Enter')
    assert field.input_value() == 'Cash B — B-2'
    # Opening/cancelling a search preserves an unchanged confirmed choice.
    page.get_by_role('button', name='Show ledger accounts', exact=True).click()
    field.press('Escape')
    assert page.locator('#ledgerAccountOptions').is_hidden()
    assert page.evaluate('ledgerSelectedAccountCode') == 'B-2'
    field.press('ArrowUp')
    field.press('Enter')
    assert page.evaluate('ledgerSelectedAccountCode') == 'EXP-1'


def test_editing_ledger_search_invalidates_plan_and_unmatched_text_never_reaches_validate(upload_formatting_page):
    page = upload_formatting_page
    columns = _arrange_ledger_upload_page(page)
    _arrange_staged_ledger_mapping(page, columns)
    _select_upload_ledger_account(page, 'A-1')
    page.evaluate("() => { validationStatus='valid'; validationPlanId='old-account-plan'; }")
    field = page.locator('#ledgerAccountSelect')
    field.fill('missing account')
    assert page.evaluate('validationPlanId') is None
    assert page.evaluate('ledgerSelectedAccountCode') == ''
    assert 'No matching accounts' in page.locator('#ledgerAccountOptions').inner_text()
    field.press('Enter')
    sent = []
    page.route('**/api/upload/validate', lambda route: (sent.append(True), route.fulfill(json={})))
    page.locator('#importBtn').click()
    assert sent == []
    assert 'Select a current Trial Balance posting account' in page.locator('#validationErrorOverlay').inner_text()


def test_large_upload_account_list_is_bounded_and_duplicate_names_remain_distinct(upload_formatting_page):
    page = upload_formatting_page
    _arrange_ledger_upload_page(page, [
        {'account_code': f'ACC-{index:04}', 'account_name': 'Shared name'} for index in range(1000)])
    field = page.locator('#ledgerAccountSelect')
    field.focus()
    assert page.locator('#ledgerAccountOptions').get_by_role('option').count() == 100
    assert 'Type more to narrow' in page.locator('#ledgerAccountOptions').inner_text()
    field.fill('ACC-0999')
    assert page.locator('#ledgerAccountOptions').get_by_role('option').all_text_contents() == ['Shared name — ACC-0999']
    field.press('Enter')
    assert page.evaluate('ledgerSelectedAccountCode') == 'ACC-0999'
    # Session restoration uses the selected identity, not the search text.
    page.evaluate("async () => { await loadLedgerUploadAccounts('ACC-0999'); }")
    assert field.input_value() == 'Shared name — ACC-0999'
    assert page.evaluate('ledgerSelectedAccountCode') == 'ACC-0999'


@pytest.mark.parametrize('width', [1280, 390])
def test_upload_account_dropdown_fits_viewport_and_source_labels_are_safe(upload_formatting_page, tmp_path, width):
    page = upload_formatting_page
    page.set_viewport_size({'width': width, 'height': 800})
    columns = _arrange_ledger_upload_page(page)
    _arrange_staged_ledger_mapping(page, columns)
    field = page.locator('#ledgerAccountSelect')
    field.scroll_into_view_if_needed()
    field.focus()
    options = page.locator('#ledgerAccountOptions')
    assert options.is_visible()
    assert options.locator('img, a, script, b').count() == 0
    assert 'Bank <A>' in options.inner_text()
    assert page.evaluate("""() => {
        const list=document.getElementById('ledgerAccountOptions');
        const box=list.getBoundingClientRect();
        const hit=document.elementFromPoint(box.x+box.width/2, box.y+box.height/2);
        return box.left>=0 && box.right<=innerWidth && box.top>=0 && box.bottom<=innerHeight && list.contains(hit);
    }""")
    page.screenshot(path=str(tmp_path / f'upload-account-{width}.png'))
    page.get_by_role('option', name='Bank <A> — A-1', exact=True).click()
    assert field.input_value() == 'Bank <A> — A-1'
    page.locator('#resetBtn').focus()
    assert options.is_hidden()


@pytest.mark.parametrize('outcome', ['success', 'server_error', 'malformed', 'network'])
def test_account_loading_feedback_finishes_on_success_or_failure(upload_formatting_page, outcome):
    page = upload_formatting_page
    _arrange_ledger_upload_page(page)
    waiting = []
    page.route('**/api/upload/ledger-accounts?client_id=5', lambda route: waiting.append(route))
    page.evaluate("() => { window.accountLoadPromise = loadLedgerUploadAccounts(); }")
    page.wait_for_function("ledgerAccountSelect.getAttribute('aria-busy')==='true'")
    assert page.locator('#ledgerAccountSelect').is_disabled()
    assert page.locator('#ledgerUploadContext .ui-field-spinner').count() == 1
    assert 'Loading accounts' in page.locator('#ledgerAccountHelp').inner_text()
    assert page.evaluate("document.documentElement.style.cursor") == 'progress'
    assert waiting
    if outcome == 'success':
        waiting[0].fulfill(json={'accounts':[{'account_code':'SUP-1','account_name':'Supplier'}]})
    elif outcome == 'server_error':
        waiting[0].fulfill(status=503, json={'detail':'Trial Balance unavailable'})
    elif outcome == 'malformed':
        waiting[0].fulfill(json={'accounts':None})
    else:
        waiting[0].abort()
    page.evaluate('() => window.accountLoadPromise')
    assert page.locator('#ledgerUploadContext .ui-field-spinner').count() == 0
    assert page.locator('#ledgerAccountSelect').get_attribute('aria-busy') is None
    assert page.evaluate('document.documentElement.style.cursor') == ''
    if outcome == 'success':
        assert page.locator('#ledgerAccountSelect').is_enabled()
        assert page.locator('#ledgerAccountHelp').is_hidden()
    else:
        assert page.locator('#ledgerAccountSelect').is_disabled()
        assert 'Select the template again to retry' in page.locator('#ledgerAccountHelp').inner_text()
        assert page.locator('#validationErrorOverlay').is_visible()


def test_template_switch_clears_busy_state_and_discards_delayed_accounts(upload_formatting_page):
    page = upload_formatting_page
    _arrange_ledger_upload_page(page)
    waiting = []
    page.route('**/api/upload/ledger-accounts?client_id=5', lambda route: waiting.append(route))
    page.evaluate("() => { window.accountLoadPromise = loadLedgerUploadAccounts(); }")
    page.wait_for_function("ledgerAccountSelect.getAttribute('aria-busy')==='true'")
    page.route('**/api/templates/8/upload-columns', lambda route: route.fulfill(json={
        'columns':[{'id':1,'column_name':'Voucher','data_type':'Text'}],'total_columns':1,'data_purpose':'standard'}))
    page.evaluate("async () => { templateDropdown.appendChild(new Option('Ordinary','8')); await selectTemplate(8); }")
    assert page.locator('#ledgerUploadContext').is_hidden()
    assert page.evaluate('document.documentElement.style.cursor') == ''
    assert page.locator('#ledgerUploadContext .ui-field-spinner').count() == 0
    waiting[0].fulfill(json={'accounts':[{'account_code':'STALE','account_name':'Old client account'}]})
    page.evaluate('() => window.accountLoadPromise')
    assert page.evaluate('ledgerAccountChoices') == []
    assert page.evaluate('ledgerSelectedAccountCode') == ''


@pytest.mark.parametrize('contents,reason', [
    (b'<html><frameset><frame src="ledger_files/sheet001.htm"></frameset></html>', 'separate linked files'),
    ('<html><frameset><frame src="sheet.htm"></frameset></html>'.encode('utf-16'), 'separate linked files'),
    (b'<html><table><tr><td>Balance</td></tr></table></html>', 'HTML web-page export'),
    (b'', 'empty'), (b'not an excel workbook', 'could not be read'),
])
def test_invalid_workbook_uploads_are_actionable_and_never_staged(contents, reason):
    import asyncio
    from io import BytesIO
    from fastapi import HTTPException, UploadFile
    from routers import import_export as uploads
    with patch.object(uploads, 'execute_write_pg') as write, patch.object(uploads, '_build_upload_storage_path') as path:
        with pytest.raises(HTTPException) as error:
            asyncio.run(uploads.import_excel(UploadFile(file=BytesIO(contents), filename='source.xls'), session={'username':'Tester'}))
        assert error.value.status_code == 400 and reason in error.value.detail
        write.assert_not_called()
        path.assert_not_called()


@pytest.mark.parametrize('book_type,filename', [('xlsx','SOURCE.XLSX'), ('biff8','SOURCE.XLS')])
def test_real_excel_formats_and_text_balances_survive_staging(upload_formatting_page, tmp_path, book_type, filename):
    import asyncio, base64
    from concurrent.futures import ThreadPoolExecutor
    from io import BytesIO
    from fastapi import UploadFile
    from routers import import_export as uploads
    # Use the app's existing SheetJS writer solely to make isolated reader fixtures.
    encoded = upload_formatting_page.evaluate("""bookType => {
        const wb=XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb,XLSX.utils.aoa_to_sheet([
            ['Voucher','Debit','Credit','Balance','Note'],['V1',50,0,'200 Dr','<table>source text</table>']]),'Ledger');
        return XLSX.write(wb,{type:'base64',bookType});
    }""", book_type)
    contents = base64.b64decode(encoded)
    path = tmp_path / filename
    with patch.object(uploads, '_build_upload_storage_path', return_value=str(path)), \
         patch.object(uploads, 'execute_write_pg', return_value=99) as write, patch.object(uploads, 'log_audit'):
        # Playwright owns the main thread's loop; run this async handler separately.
        with ThreadPoolExecutor(max_workers=1) as worker:
            result = worker.submit(asyncio.run, uploads.import_excel(UploadFile(file=BytesIO(contents), filename=filename), session={'username':'Tester'})).result()
    assert result['file_id'] == 99 and result['record_count'] == 1
    assert path.read_bytes() == contents
    parsed = uploads._read_standalone_workbook(contents)
    assert parsed.iloc[0]['Balance'] == '200 Dr' and parsed.iloc[0]['Debit'] == 50
    assert write.call_count == 1


def test_global_unknown_upload_popup_shows_reason_and_support_reference_as_text(upload_formatting_page):
    page = upload_formatting_page
    page.route('**/api/import-excel', lambda route: route.fulfill(status=500, json={'success':False,'error':{
        'error_code':'ERR-UNK-001','error_id':123,
        'user_message':"An unknown error occurred. We don't yet have specific information about this error.",
        'technical_details':{'message':'Excel format cannot be determined <b>source</b>', 'file':'PRIVATE_PATH', 'line':100}}}))
    page.evaluate("async () => { selectedFile = new File(['test'], 'test.xls'); await uploadFileToServer(selectedFile); }")
    text = page.locator('#validationErrorOverlay').inner_text()
    assert 'Excel format cannot be determined <b>source</b>' in text and 'reference 123' in text
    assert 'PRIVATE_PATH' not in text and page.locator('#validationErrorOverlay b').count() == 0


@pytest.mark.parametrize('opening,closing,source', [('150','200','inferred_from_transaction'), ('0','50','erp_opening'), (None,None,'unavailable')])
def test_validation_explains_calculated_balance_provenance_without_inventing_zero(upload_formatting_page, opening, closing, source):
    page = upload_formatting_page
    columns = _arrange_ledger_upload_page(page)
    _arrange_staged_ledger_mapping(page, columns)
    _select_upload_ledger_account(page, 'EXP-1')
    page.locator('#uploadCoverageStart').fill('2026-01-01')
    page.locator('#uploadCoverageEnd').fill('2026-01-31')
    page.route('**/api/upload/validate', lambda route: route.fulfill(json={
        'has_errors':False,'can_import':True,'validation_plan_id':'balance-plan',
        'ledger_evidence_refresh':opening == '150',
        'ledger_evidence':{'opening':None,'closing':None,'control_rows':[],
            'balance_check':{'opening':opening,'closing':closing,'opening_source':source,
                'difference_count':0,'status':'consistent' if opening is not None else 'opening_unavailable'}}}))
    page.locator('#importBtn').click()
    page.wait_for_function("validationStatus==='valid'")
    text = page.locator('#ledgerEvidenceSummary').inner_text()
    if opening is None:
        assert 'Calculated opening: Unavailable' in text and 'Balance comparison: unavailable' in text
        assert 'Balance differences: 0' not in text
    else:
        assert 'Calculated opening: ' + opening + ' Dr' in text
        assert 'Calculated closing: ' + closing + ' Dr' in text
        assert ('inferred from transactions' if source.startswith('inferred') else 'ERP opening') in text
    if opening == '150':
        assert page.locator('#importBtn').inner_text() == 'Refresh balance evidence'


def test_ledger_continuation_review_keeps_raw_notes_and_shows_parent_without_junk_warning(upload_formatting_page):
    page = upload_formatting_page
    columns = _arrange_ledger_upload_page(page)
    _arrange_staged_ledger_mapping(page, columns)
    _select_upload_ledger_account(page, 'A-1')
    page.evaluate("""() => {
        const book = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(book, XLSX.utils.aoa_to_sheet([excelHeaders,
            ['V1','15/01/2026','Original description',50,0,'200 Dr'],
            ['','','Bill #: <b>123</b>','','','']]), 'Ledger');
        workbookData = XLSX.write(book,{type:'array',bookType:'xlsx'});
        renderRowExclusionGrid();
    }""")
    page.locator('#uploadCoverageStart').fill('2026-01-01')
    page.locator('#uploadCoverageEnd').fill('2026-01-31')
    page.route('**/api/upload/validate', lambda route: route.fulfill(json={
        'has_errors':False, 'can_import':True, 'validation_plan_id':'notes-plan',
        'ledger_evidence':{'opening':None, 'closing':None, 'control_rows':[],
            'continuation_rows':[{'row_number':3, 'parent_row_number':2, 'voucher':'V1 <b>source</b>'}]}}))
    page.locator('#importBtn').click()
    page.wait_for_function("validationStatus==='valid'")
    assert '1 continuation note(s) attached' in page.locator('#ledgerEvidenceSummary').inner_text()
    assert page.locator('#rowExclusionBody tr.row-flagged').count() == 0
    detail = page.locator('#rowExclusionBody tr').nth(1)
    assert 'Bill #: <b>123</b>' in detail.inner_text() and detail.locator('b').count() == 0
    title = detail.locator('.row-exclusion-rownum-col').get_attribute('title')
    assert 'attached to Excel row 2' in title and 'voucher V1 <b>source</b>' in title
    page.evaluate('resetValidationState()')
    assert page.evaluate('ledgerRecognisedNoteRows.size') == 0


def test_ledger_coverage_popup_shows_specific_row_voucher_and_both_ranges(upload_formatting_page):
    page = upload_formatting_page
    columns = _arrange_ledger_upload_page(page)
    _arrange_staged_ledger_mapping(page, columns)
    _select_upload_ledger_account(page, 'A-1')
    page.locator('#uploadCoverageStart').fill('2026-01-01')
    page.locator('#uploadCoverageEnd').fill('2026-10-19')
    message = "Ledger transaction at Excel row 2, voucher 'JV-1225-100', dated 2025-12-31 is outside the declared coverage period 2026-01-01 to 2026-10-19."
    page.route('**/api/upload/validate', lambda route: route.fulfill(status=400, json={'detail':message}))
    page.locator('#importBtn').click()
    assert message in page.locator('#validationErrorOverlay').inner_text()
    assert page.evaluate('validationPlanId') is None


@pytest.mark.parametrize('state', ['locked_preview', 'missing_select', 'empty_mapping', 'wrong_column'])
def test_inactive_mapping_uses_issue_popup_and_never_sends_partial_validation_or_import(upload_formatting_page, state):
    page = upload_formatting_page
    columns = _arrange_ledger_upload_page(page)
    _arrange_staged_ledger_mapping(page, columns)
    _select_upload_ledger_account(page, 'A-1')
    requests = []
    page.on('request', lambda r: requests.append(r.url) if '/api/upload/validate' in r.url or '/api/upload/import' in r.url else None)
    page.evaluate("""state => {
        validationStatus='valid'; validationPlanId='retain-receipt-plan';
        if (state==='locked_preview') renderRestorePreview({mappings:currentMappings,sheetName:'Ledger'});
        if (state==='missing_select') mappingBody.querySelector('select').remove();
        if (state==='empty_mapping') mappingBody.innerHTML='';
        if (state==='wrong_column') mappingBody.querySelector('select').dataset.columnId='9999';
    }""", state)
    page.locator('#importBtn').click()
    page.wait_for_function('uploadActionInFlight === false')
    assert 'wait for column mapping to finish' in page.locator('#validationErrorOverlay').inner_text()
    assert 'Column mapping' in page.locator('#validationErrorOverlay').inner_text()
    assert not requests
    assert page.evaluate('validationPlanId') == 'retain-receipt-plan'
    assert page.evaluate('document.documentElement.style.cursor') == ''
    page.keyboard.press('Escape')
    # The same action works once a real interactive mapping replaces the preview.
    _arrange_staged_ledger_mapping(page, columns)
    page.evaluate('clearRestorePreview();resetValidationState()')
    page.locator('#uploadCoverageStart').fill('2026-01-01')
    page.locator('#uploadCoverageEnd').fill('2026-01-31')
    page.route('**/api/upload/validate', lambda route: route.fulfill(json={'has_errors':False,'can_import':False}))
    page.locator('#importBtn').click()
    page.wait_for_function("validationStatus === 'ignored'")
    assert len(requests) == 1
