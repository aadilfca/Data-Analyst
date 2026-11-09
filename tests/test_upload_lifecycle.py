"""Isolated replacement/archive acceptance: no live database, Drive or imports."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import sqlite3
import zipfile

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import database
from routers import import_export as upload
from routers import google_drive_auth as drive_auth
from routers.auth import require_admin
from services import upload_lifecycle as lifecycle, storage_cleanup, drive_archive


COLUMNS = [{'id': i, 'column_name': name, 'data_type': kind,
            'reconciliation_role': 'document_date' if name == 'Transaction Date' else None}
           for i, (name, kind) in enumerate([
    ('Voucher ID', 'Text'), ('Transaction Date', 'Date'), ('Account Detail', 'Text'),
    ('Voucher Detail', 'Text'), ('Debit Amount', 'Numeric'), ('Credit Amount', 'Numeric'),
], 1)]


@pytest.fixture(autouse=True)
def no_saved_drive_credential_queries(monkeypatch):
    """Keep Drive tests isolated from the app's real PostgreSQL database."""
    monkeypatch.setattr(drive_archive, 'load_saved_refresh_token', lambda *_: None)


def row(voucher='V1', day='15/04/2026', description='Supplies', amount=10):
    return {'Voucher ID': voucher, 'Transaction Date': day, 'Account Detail': 'A',
            'Voucher Detail': description, 'Debit Amount': amount, 'Credit Amount': 0}


def _google_drive_auth_client():
    app = FastAPI()
    app.include_router(drive_auth.router)
    app.dependency_overrides[require_admin] = lambda: {'role': 'admin'}
    return TestClient(app, follow_redirects=False)


def _configure_google_drive_oauth(monkeypatch):
    monkeypatch.setenv('DRIVE_CLIENT_ID', 'web-client-id')
    monkeypatch.setenv('DRIVE_CLIENT_SECRET', 'web-client-secret')
    monkeypatch.setenv('DRIVE_OAUTH_REDIRECT_URI', 'https://example.test/api/google-drive/callback')
    monkeypatch.setenv('DRIVE_ARCHIVE_FOLDER_ID', 'folder-id')


def test_google_drive_reconnect_uses_state_pkce_and_clears_cookies_on_cancel(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    _configure_google_drive_oauth(monkeypatch)
    client = _google_drive_auth_client()
    started = client.get('/api/google-drive/connect')
    query = parse_qs(urlsplit(started.headers['location']).query)
    assert query['client_id'] == ['web-client-id']
    assert query['redirect_uri'] == ['https://example.test/api/google-drive/callback']
    assert query['access_type'] == ['offline'] and query['prompt'] == ['consent']
    assert query['code_challenge_method'] == ['S256']
    assert client.cookies.get('drive_oauth_state') == query['state'][0]
    assert client.cookies.get('drive_oauth_verifier')

    cancelled = client.get('/api/google-drive/callback', params={
        'state': query['state'][0], 'error': 'access_denied'})
    assert cancelled.status_code == 303
    assert 'drive_oauth=cancelled' in cancelled.headers['location']
    assert all('Max-Age=0' in value for value in cancelled.headers.get_list('set-cookie'))


def test_google_drive_reconnect_verifies_folder_before_saving_token(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    _configure_google_drive_oauth(monkeypatch)
    saved = []

    class VerifiedDrive:
        client_secret = 'web-client-secret'
        def __init__(self, refresh_token_override=None):
            assert refresh_token_override == 'new-refresh-token'
        def verify_archive_folder(self):
            saved.append('verified')

    monkeypatch.setattr(drive_auth, '_exchange_code', lambda code, verifier: 'new-refresh-token')
    monkeypatch.setattr(drive_auth, 'DriveArchive', VerifiedDrive)
    monkeypatch.setattr(drive_auth, 'save_refresh_token', lambda token, secret: saved.append((token, secret)))
    client = _google_drive_auth_client()
    started = client.get('/api/google-drive/connect')
    query = parse_qs(urlsplit(started.headers['location']).query)
    callback = client.get('/api/google-drive/callback', params={
        'state': query['state'][0], 'code': 'one-time-code'})
    assert callback.status_code == 303 and 'drive_oauth=connected' in callback.headers['location']
    assert saved == ['verified', ('new-refresh-token', 'web-client-secret')]
    assert 'new-refresh-token' not in callback.headers['location']


def test_google_drive_refresh_token_is_encrypted_and_secret_bound():
    encrypted = drive_archive.encrypt_refresh_token('sample-token', 'secret-a')
    assert encrypted != 'sample-token'
    assert drive_archive.decrypt_refresh_token(encrypted, 'secret-a') == 'sample-token'
    with pytest.raises(drive_archive.ArchiveError, match='cannot be unlocked'):
        drive_archive.decrypt_refresh_token(encrypted, 'secret-b')


def test_saved_change_report_lists_informational_values_without_transaction_change():
    from openpyxl import load_workbook
    from services.upload_observation_report import build_change_report

    report = build_change_report({
        'new': [], 'changed': [], 'missing': [],
        'informational_updates': [{
            'client_data_id': 42,
            'field_updates': {'Balance': '80 DR'},
            'old_field_values': {'Balance': '100 DR'},
        }],
    }, [{'column_name': 'Voucher ID', 'data_type': 'Text'},
        {'column_name': 'Balance', 'data_type': 'Text'}],
       {'client_id': 5, 'template_id': 1, 'start': '2026-10-01', 'end': '2026-10-31'})
    workbook = load_workbook(io.BytesIO(report), data_only=False)

    assert 'Informational refreshes' in workbook.sheetnames
    rows = list(workbook['Informational refreshes'].values)
    assert rows[1] == (42, 'Balance', '100 DR', '80 DR')
    assert workbook['Import summary']['B9'].value == 1


def test_upload_staging_path_is_absolute_and_uses_configured_directory(monkeypatch, tmp_path):
    monkeypatch.setenv('UPLOAD_STAGING_DIR', str(tmp_path))

    path = upload._build_upload_storage_path('meezan bank.xlsx')

    assert upload.os.path.dirname(path) == str(tmp_path)
    assert upload.os.path.isabs(path)
    assert upload.os.path.basename(path).endswith('_meezan bank.xlsx')


def test_missing_staged_file_is_reported_even_when_hash_is_already_saved(tmp_path):
    missing_path = str(tmp_path / 'missing.xlsx')

    with pytest.raises(HTTPException) as error:
        upload._ensure_uploaded_file_hash(99, missing_path, 'previously-saved-hash')

    assert error.value.status_code == 410
    assert error.value.detail['code'] == 'staged_file_missing'


class Store:
    """Transaction-aware adapter executing the real validation/import orchestration."""

    def __init__(self, rows):
        self.rows = []
        self.jobs, self.plans, self.audits, self.history = {}, {}, [], []
        self.columns = deepcopy(COLUMNS)
        self.file = {'id': 99, 'file_path': 'isolated.xlsx', 'file_name': 'isolated.xlsx', 'status': 'Pending'}
        self.depth = 0
        self.source_writes = 0
        self.classified_dates = []
        self.metadata_writes = 0
        self.protected = False
        for index, raw in enumerate(rows, 1):
            self.add(index, raw)

    def add(self, index, raw, client=5, template=1):
        occurrence = 1 + sum(r['row_data'] == raw for r in self.rows)
        identity = upload._canonical_source_identity(COLUMNS, raw, 1, occurrence_index=occurrence)
        self.rows.append({'id': index, 'client_id': client, 'template_id': template, 'upload_id': 1,
                          'row_data': deepcopy(raw), 'version': 1, 'source_identity': identity,
                          'document_group_id': '1-' + raw['Voucher ID'], 'is_deleted': False,
                          'source_row_number': index + 1, 'source_order_upload_id': 1,
                          'extract_date': datetime.strptime(raw['Transaction Date'], '%d/%m/%Y').strftime('%Y-%m-%d')})

    @contextmanager
    def transaction(self):
        before = deepcopy((self.rows, self.jobs, self.plans, self.file, self.audits, self.history, self.source_writes, self.metadata_writes)) if self.depth == 0 else None
        self.depth += 1
        try:
            yield self
        except BaseException:
            if before is not None:
                self.rows, self.jobs, self.plans, self.file, self.audits, self.history, self.source_writes, self.metadata_writes = before
            raise
        finally:
            self.depth -= 1

    def query(self, sql, params=None, **kwargs):
        params = params or ()
        if sql.startswith(('SAVEPOINT ', 'RELEASE SAVEPOINT ', 'ROLLBACK TO SAVEPOINT ')):
            return []
        if sql.startswith('WITH source AS (SELECT NULLIF(BTRIM(cd.row_data'):
            self.aggregate_calls = getattr(self, 'aggregate_calls', 0) + 1
            self.aggregate_after_writes = self.source_writes
            if getattr(self, 'aggregate_error', False):
                raise RuntimeError('Simulated aggregate failure')
            footings = [c['column_name'] for c in self.columns if c.get('reconciliation_role') == 'footing']
            key, *names = params[:2 * (len(footings) + 1):2]
            position = 2 * (len(footings) + 1)
            client_id, template_id, start, end = params[position:position + 4]
            records = [r for r in self.rows if r['client_id'] == client_id and r['template_id'] == template_id
                       and not r.get('is_deleted') and start <= r['extract_date'] <= end]
            ids = [str(r['row_data'][key]).strip() if r['row_data'].get(key) is not None else '' for r in records]
            aggregate = {'row_count': len(records), 'document_count': len(set(ids) - {''}),
                         'missing_document_rows': ids.count('')}
            for i, name in enumerate(names):
                total, invalid = Decimal(0), 0
                for record in records:
                    try:
                        total += Decimal(str(record['row_data'].get(name) or 0))
                    except InvalidOperation:
                        invalid += 1
                aggregate.update({f'total_{i}': total, f'invalid_{i}': invalid})
            aggregate.update(getattr(self, 'aggregate_override', {}))
            return [aggregate]
        if 'to_regclass' in sql:
            return [{'table_name': None}]
        if 'LOCK TABLE' in sql or 'pg_advisory' in sql:
            return []
        if 'FROM templates' in sql:
            return [{'id': 1, 'template_name': 'Transactions', 'rate_source': None, 'data_purpose': 'standard'}]
        if 'FROM template_column_mappings' in sql:
            return deepcopy(self.columns)
        if 'FROM clients' in sql:
            return [{'id': 5}]
        if 'FROM app_settings' in sql:
            return []
        if 'FROM upload_lifecycle_runs' in sql:
            job = self.jobs.get(params[0])
            return [deepcopy(job)] if job else []
        if 'FROM upload_validation_plans' in sql:
            plan = self.plans.get(params[0])
            return [deepcopy(plan)] if plan else []
        if 'FROM data_audit_log' in sql:
            return [deepcopy(a) for a in self.audits if a['client_data_id'] in params[0]]
        if 'FROM uploaded_files' in sql or 'FROM public.uploaded_files' in sql:
            if 'NOT EXISTS' in sql:
                return []
            if 'file_sha256 = ' in sql:
                return []
            if 'id = ANY' in sql:
                return [{'id': 1, 'client_id': 5, 'template_id': 1, 'file_name': 'old.xlsx'}]
            return [deepcopy(self.file)]
        if 'FROM client_data' in sql or 'FROM public.client_data' in sql:
            rows = [deepcopy(r) for r in self.rows]
            if 'cd.id = ANY' in sql or ' AND id = ANY' in sql or 'WHERE id = ANY' in sql:
                selected_ids = next(value for value in params if isinstance(value, list))
                return [r for r in rows if r['id'] in selected_ids]
            if 'client_id = %s' in sql:
                rows = [r for r in rows if r['client_id'] == params[0] and r['template_id'] == params[1]]
            if 'OR cd.document_group_id = ANY' in sql:
                return [r for r in rows if params[2] <= r['extract_date'] <= params[3]
                        or r['document_group_id'] in params[4]]
            if 'cd.document_group_id = ANY' in sql:
                if len(params) == 3:
                    return [r for r in rows if r['document_group_id'] in params[2]]
                return [r for r in rows if r['document_group_id'] in params[2]
                        and not params[3] <= r['extract_date'] <= params[4]]
            if 'source_identity = ANY' in sql:
                return [r for r in rows if r['source_identity'] in params[2]
                        and (len(params) < 4 or r['id'] not in params[3])]
            if 'BETWEEN %s::date' in sql:
                rows = [r for r in rows if params[2] <= r['extract_date'] <= params[3]]
            return rows
        raise AssertionError('Unexpected read: ' + sql)

    def write(self, sql, params=None):
        params = params or ()
        if 'INSERT INTO upload_lifecycle_runs' in sql:
            self.jobs.setdefault(params[0], {'operation_id': params[0], 'file_id': params[1],
                'client_id': params[2], 'template_id': params[3], 'request_signature': params[4],
                'status': 'archiving', 'scope_json': params[5], 'drive_file_id': None})
        elif 'UPDATE upload_lifecycle_runs' in sql:
            job = self.jobs[params[-1]]
            if "status = 'unbacked'" in sql:
                job.update(status='unbacked', archive_state_hash=params[0])
            elif 'drive_file_id = ' in sql:
                job['drive_file_id'] = job.get('drive_file_id') or params[0]
            elif "status = 'verified'" in sql:
                if len(params) == 4:
                    job.update(status='verified', archive_sha256=params[0], archive_path=params[1], archive_state_hash=params[2])
                else:
                    job.update(status='verified', archive_state_hash=params[0])
            elif "status = 'applied'" in sql:
                job.update(status='applied', result_json=params[0])
        elif 'INSERT INTO upload_validation_plans' in sql:
            payload = params[6].adapted
            self.plans[params[0]] = {'plan_id': params[0], 'file_id': params[1], 'client_id': params[2],
                'template_id': params[3], 'file_sha256': params[4], 'request_signature': params[5],
                'plan_json': json.loads(json.dumps(payload, default=str)), 'status': 'ready',
                'expires_at': datetime.now() + timedelta(minutes=30)}
        elif 'DELETE FROM upload_validation_plans' in sql or 'DELETE FROM public.upload_validation_plans' in sql:
            if params and isinstance(params[0], str):
                self.plans.pop(params[0], None)
            else:
                self.plans.clear()
        elif 'UPDATE uploaded_files' in sql:
            if "status = 'Imported'" in sql:
                self.file['status'] = 'Imported'
            elif "status = %s" in sql:
                pass
        elif 'DELETE FROM public.client_data' in sql:
            self.rows = [r for r in self.rows if r['id'] not in params[0]]
            self.source_writes += 1
        elif 'DELETE FROM public.data_audit_log' in sql:
            self.audits = [r for r in self.audits if r['client_data_id'] not in params[0]]

    def insert(self, conn, sql, values, **kwargs):
        assert conn is self and self.depth > 0
        ids = []
        for value in values:
            assert not any(r['client_id'] == value[0] and r['template_id'] == value[1]
                           and r['source_identity'] == value[11] for r in self.rows)
            index = max([r['id'] for r in self.rows] + [10000]) + 1
            self.rows.append({'id': index, 'client_id': value[0], 'template_id': value[1],
                              'upload_id': value[2], 'row_data': deepcopy(value[3].adapted), 'version': 1,
                              'extract_date': value[5], 'source_identity': value[11],
                              'source_row_number': value[13], 'source_order_upload_id': value[14],
                              'document_group_id': value[8], 'is_deleted': False})
            self.source_writes += 1
            ids.append(index)
        return ids

    def batch(self, conn, sql, values):
        assert self.depth > 0
        if 'INSERT INTO data_audit_log' in sql:
            for value in values:
                self.audits.append({'id': len(self.audits) + 1, 'client_data_id': value[0], 'action': value[2]})
        elif 'INSERT INTO client_data_history' in sql:
            for value in values:
                self.history.append({
                    'client_id': value[0], 'template_id': value[1], 'client_data_id': value[2],
                    'action': value[3], 'source_upload_id': value[5],
                    'replacing_upload_id': value[6], 'source_identity': value[7],
                    'row_data': deepcopy(value[8].adapted),
                    'changed_fields': deepcopy(value[9].adapted) if value[9] else None,
                    'ledger_account_code': value[10], 'ledger_account_name': value[11],
                })
        elif 'UPDATE client_data SET source_row_number' in sql:
            for number, source, key, client, template in values:
                target = next(r for r in self.rows if r['id'] == key and r['client_id'] == client and r['template_id'] == template)
                target.update(source_row_number=number, source_order_upload_id=source)
                self.metadata_writes += 1
        elif 'SET row_data = %s, upload_id = %s, source_identity = %s' in sql:
            for value in values:
                target = next(r for r in self.rows if r['id'] == value[-1])
                target.update(row_data=deepcopy(value[0].adapted), upload_id=value[1],
                              source_identity=value[2], source_fingerprint=value[3],
                              source_row_number=value[4], source_order_upload_id=value[5])
                self.source_writes += 1
        elif 'UPDATE client_data' in sql:
            for value in values:
                target = next(r for r in self.rows if r['id'] == value[-1])
                target.update(row_data=deepcopy(value[0].adapted), upload_id=value[1], extract_date=value[6], source_identity=value[7],
                              source_row_number=value[9], source_order_upload_id=value[10])
                self.source_writes += 1


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    import pandas as pd
    state = {'store': None, 'incoming': [], 'failure': False, 'after_archive': None, 'transfers': 0, 'reports': 0,
             'allow_without_backup': False}
    monkeypatch.setattr(lifecycle, 'execute_query_pg', lambda *args, **kwargs:
                        [{'value': 'true' if state['allow_without_backup'] else 'false'}])
    monkeypatch.setattr(lifecycle, 'unbacked_uploads_allowed', lambda query=None: state['allow_without_backup'])
    monkeypatch.setenv('UPLOAD_ARCHIVE_DIR', str(tmp_path / 'archives'))
    monkeypatch.setenv('UPLOAD_ARCHIVE_TO_DRIVE', 'true')

    class Drive:
        def allocate_id(self):
            return 'remote'

        def upload_and_verify(self, file_id, path, operation, expected):
            state['transfers'] += 1
            assert state['store'].source_writes == 0
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
            if state['failure']:
                raise drive_archive.ArchiveError('Archive unavailable')
            if state['after_archive']:
                state['after_archive'](state['store'])
            return {'drive_file_id': file_id, 'sha256': expected, 'local_path': str(path)}

        def upload_change_report(self, content, filename, client_name, import_date, operation_id):
            state['reports'] += 1
            assert content.startswith(b'PK') and state['store'].source_writes == 0
            if state['failure']:
                raise drive_archive.ArchiveError('Change report unavailable')
            return {'file_id': 'report', 'file_url': 'https://drive.google.com/file/d/report/view',
                    'folder_url': 'https://drive.google.com/drive/folders/day',
                    'folder_path': f'Upload Change Reports/{client_name}/{import_date[:4]}/{import_date}',
                    'filename': filename}

    monkeypatch.setattr(lifecycle, 'DriveArchive', Drive)
    for module in (upload, storage_cleanup):
        monkeypatch.setattr(module, 'execute_query_pg', lambda *a, **kw: state['store'].query(*a, **kw))
        monkeypatch.setattr(module, 'execute_write_pg', lambda *a, **kw: state['store'].write(*a, **kw))
        monkeypatch.setattr(module, 'pg_transaction', lambda: state['store'].transaction())
    monkeypatch.setattr(storage_cleanup, '_count', lambda *a: int(state['store'].protected))
    monkeypatch.setattr(upload.pd, 'read_excel', lambda *a, **kw: pd.DataFrame(state['incoming'], columns=[c['column_name'] for c in COLUMNS]))
    # This adapter supplies synthetic rows; real workbook metadata has separate tests.
    monkeypatch.setattr(upload, '_workbook_input_errors', lambda *a: [])
    require_staged_upload = upload._require_staged_upload
    monkeypatch.setattr(upload, '_require_staged_upload', lambda path: path if path == 'isolated.xlsx' else require_staged_upload(path))
    monkeypatch.setattr(upload, '_ensure_uploaded_file_hash', lambda *a: 'hash')
    monkeypatch.setattr(upload, '_validation_file_sha256', lambda *a: 'hash')
    monkeypatch.setattr(upload, 'get_rate_precision', lambda: 0)
    monkeypatch.setattr(upload, 'get_database_storage_budget_pg', lambda *a: {'blocked': False, 'warning': False})
    monkeypatch.setattr(upload, 'execute_batch_insert_with_ids_pg', lambda *a, **kw: state['store'].insert(*a, **kw))
    monkeypatch.setattr(upload, 'execute_batch_pg', lambda *a, **kw: state['store'].batch(*a, **kw))
    monkeypatch.setattr(upload, 'load_treasury_mapping_postgres', lambda *a: {})
    monkeypatch.setattr(upload, 'upsert_classification_rows_postgres', lambda conn, rows, *a, **kw: state['store'].classified_dates.extend(r['extract_date'] for r in rows))
    monkeypatch.setattr(upload, 'resolve_upload_occurrences_postgres', lambda *a, **kw: {'eligible': True})
    monkeypatch.setattr(upload, 'log_audit', lambda *a: None)
    yield state
    upload.IMPORT_PROGRESS.pop(99, None)


def request(**changes):
    return upload.ImportRequest(client_id=5, template_id=1, file_id=99, comparison_mode=changes.pop('comparison_mode', 'replace_range'),
        coverage_start=changes.pop('coverage_start', '2026-04-01'), coverage_end=changes.pop('coverage_end', '2026-04-30'),
        confirm_report_coverage=changes.pop('confirm_report_coverage', True),
        mappings=[upload.ColumnMapping(template_column_id=c['id'], template_column_name=c['column_name'],
                                       excel_column_name=c['column_name']) for c in COLUMNS], **changes)


def test_complete_report_requires_explicit_full_coverage_confirmation():
    columns = deepcopy(COLUMNS)
    mapping = {column['column_name']: column['column_name'] for column in columns}
    with pytest.raises(HTTPException, match='Confirm that the coverage dates'):
        upload._validate_complete_report_inputs(request(confirm_report_coverage=False), columns, mapping)
    assert upload._validate_complete_report_inputs(request(), columns, mapping)[2]['column_name'] == 'Transaction Date'


def test_document_date_suggestions_use_only_transaction_rows_and_shared_date_parser():
    rows = [
        {'Transaction Date': '03/04/2026'},
        {'Transaction Date': datetime(2026, 4, 8)},
        {'Transaction Date': 'not a date'},
    ]
    suggestion = upload._suggest_document_date_range(rows, 'Transaction Date', 'Transaction Date', '/', 'DMY')
    assert suggestion == {'start': '2026-04-03', 'end': '2026-04-08',
                          'transaction_dates': 2, 'column': 'Transaction Date'}


def test_date_preview_suggests_transaction_dates_and_skips_report_total(pipeline):
    state = pipeline
    state['store'] = Store([])
    state['incoming'] = [row('V1', '08/04/2026'), row('V2', '03/04/2026'),
        {'Voucher ID': 'TOTAL', 'Transaction Date': None, 'Account Detail': None,
         'Voucher Detail': 'TOTAL', 'Debit Amount': 20, 'Credit Amount': 0}]
    request = upload.DatePreviewRequest(
        file_id=99, template_id=1,
        date_mappings=[upload.DatePreviewMapping(template_column_name='Transaction Date',
                                                 excel_column_name='Transaction Date')],
        column_mappings=[upload.DatePreviewMapping(template_column_name=c['column_name'],
                                                   excel_column_name=c['column_name']) for c in COLUMNS],
        date_delimiter='/', date_order='DMY',
    )
    result = upload.preview_date_format(request)
    assert result['date_suggestion'] == {
        'start': '2026-04-03', 'end': '2026-04-08', 'transaction_dates': 2,
        'column': 'Transaction Date', 'status': 'ready'
    }


def validate_then_import(state, **options):
    result = upload.validate_only(request(**options))
    assert result['can_import']
    return upload.validate_and_import(request(validation_plan_id=result['validation_plan_id'], **options)), result


def test_actual_685_line_shape_replaces_without_line_ids_and_retains_one_current_set(pipeline):
    state = pipeline
    state['incoming'] = [row(voucher='BPV-0426-248', description=f'Advance Paid : ADVE-{i}', amount=i % 63 + 1) for i in range(685)]
    state['store'] = Store([{**r, 'Voucher Detail': r['Voucher Detail'] + f'\nTag: {i}'} for i, r in enumerate(state['incoming'])])
    state['store'].add(900, row('KEEP-OTHER-MONTH', '01/05/2026'))
    state['store'].add(901, row('OTHER-CLIENT'), client=6)
    before = deepcopy(state['store'].rows)
    result, validation = validate_then_import(state)
    assert validation['change_detection']['missing_count'] == 685
    assert result['imported_count'] == result['deleted_count'] == 685
    assert result['archive_verified']
    assert len(state['store'].rows) == 687
    assert state['store'].rows[:2] == before[-2:]
    assert len({r['source_identity'] for r in state['store'].rows}) == 687
    job = next(iter(state['store'].jobs.values()))
    assert job['status'] == 'applied'
    with zipfile.ZipFile(job['archive_path']) as archive:
        archived = json.loads(archive.read('rows.json'))
        assert len(archived) == 685
        assert all('Tag:' in r['row_data']['Voucher Detail'] for r in archived)
    # Lost response: same plan returns a receipt, without another transfer/write.
    writes = state['store'].source_writes
    retry = upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert retry['recovered_commit'] and state['transfers'] == 1
    assert state['store'].source_writes == writes


@pytest.mark.parametrize('start,end', [('2026-04-15', '2026-04-15'), ('2026-04-10', '2026-04-20'), ('2026-01-01', '2026-10-25')])
def test_daily_weekly_and_cumulative_coverage_preserves_other_periods(pipeline, start, end):
    state = pipeline
    state['store'] = Store([row('OLD'), row('OUTSIDE', '01/12/2026')])
    state['incoming'] = [row('NEW')]
    result, _ = validate_then_import(state, coverage_start=start, coverage_end=end)
    assert result['deleted_count'] == result['imported_count'] == 1
    assert {r['row_data']['Voucher ID'] for r in state['store'].rows} == {'NEW', 'OUTSIDE'}


def test_deleted_first_last_days_and_empty_period_use_declared_boundaries(pipeline):
    state = pipeline
    state['store'] = Store([row('FIRST', '01/04/2026'), row('KEEP'), row('LAST', '30/04/2026'), row('OUTSIDE', '01/05/2026')])
    state['incoming'] = [row('KEEP')]
    result, _ = validate_then_import(state)
    assert result['deleted_count'] == 2
    assert {r['row_data']['Voucher ID'] for r in state['store'].rows} == {'KEEP', 'OUTSIDE'}


def test_empty_period_requires_explicit_confirmation_then_archives_and_clears(pipeline):
    state = pipeline
    state['store'] = Store([row()])
    with pytest.raises(HTTPException, match='empty'):
        upload.validate_only(request())
    result, _ = validate_then_import(state, confirm_empty_period=True)
    assert result['deleted_count'] == 1 and state['store'].rows == []


def test_repeated_unchanged_source_is_noop_without_archive_or_plan(pipeline):
    state = pipeline
    state['incoming'] = [row(), row()]
    state['store'] = Store(state['incoming'])
    result = upload.validate_only(request())
    assert not result['can_import'] and result['change_detection']['unchanged_count'] == 2
    assert not state['store'].jobs and not state['transfers']


def test_99_unchanged_complete_report_rows_are_not_written_again(pipeline):
    state = pipeline
    state['incoming'] = [row(voucher=f'V-{index:03}') for index in range(1, 100)]
    state['store'] = Store(state['incoming'])
    before = deepcopy(state['store'].rows)

    validation = upload.validate_only(request())

    changes = validation['change_detection']
    assert not validation['can_import']
    assert changes['unchanged_count'] == 99
    assert changes['new_count'] == changes['changed_count'] == changes['missing_count'] == 0
    assert state['store'].rows == before
    assert state['store'].source_writes == 0
    assert state['store'].history == []
    assert not state['store'].jobs and not state['transfers']


def test_unchecked_balance_refreshes_only_after_confirmed_import_without_history(pipeline, monkeypatch):
    import sys
    columns = deepcopy(COLUMNS) + [{'id': 7, 'column_name': 'Balance', 'data_type': 'Text', 'compare_for_changes': False}]
    monkeypatch.setattr(sys.modules[__name__], 'COLUMNS', columns)
    state = pipeline
    old = {**row(), 'Balance': '100 DR'}
    state['store'] = Store([old])
    state['incoming'] = [{**old, 'Balance': '80 DR'}]

    validation_request = request()
    validation_request.mappings[-1].compare_for_changes = True  # Cannot override the template.
    validation = upload.validate_only(validation_request)

    assert validation['can_import']
    assert validation['change_detection']['unchanged_count'] == 1
    assert validation['change_detection']['informational_update_count'] == 1
    assert validation['change_detection']['changed_count'] == 0
    assert state['store'].rows[0]['row_data']['Balance'] == '100 DR'
    assert state['store'].source_writes == 0 and state['store'].history == []

    import_request = request(validation_plan_id=validation['validation_plan_id'])
    import_request.mappings[-1].compare_for_changes = True
    result = upload.validate_and_import(import_request)

    assert result['updated_count'] == 0
    assert result['informational_updated_count'] == 1
    assert result['change_detection']['unchanged_count'] == 1
    assert result['change_detection']['changed_count'] == 0
    assert len(state['store'].rows) == 1
    assert state['store'].rows[0]['id'] == 1
    assert state['store'].rows[0]['row_data']['Balance'] == '80 DR'
    assert state['store'].history == []


def test_legacy_source_order_refresh_keeps_values_ids_and_receipt_without_financial_archive(pipeline):
    state=pipeline
    state['store']=Store([row('V1'),row('V1'),row('V9'),row('OUTSIDE','01/05/2026')])
    for saved in state['store'].rows[:3]:
        saved.update(source_row_number=None,source_order_upload_id=None)
    before=[(r['id'],deepcopy(r['row_data']),r['version'],r['upload_id']) for r in state['store'].rows]
    outside=deepcopy(state['store'].rows[-1])
    state['incoming']=[row('V9'),row('V1'),row('V1')]
    result,validation=validate_then_import(state)
    assert result['imported_count']==result['updated_count']==result['deleted_count']==0
    assert result['source_order_refreshed'] and 'row order restored' in result['message']
    assert state['store'].source_writes==state['transfers']==0
    assert state['store'].metadata_writes==3
    assert [(r['id'],r['row_data'],r['version'],r['upload_id']) for r in state['store'].rows]==before
    assert state['store'].rows[-1]==outside
    ordered=sorted(state['store'].rows[:3],key=lambda r:r['source_row_number'])
    assert [r['id'] for r in ordered]==[3,1,2]
    retry=upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert retry['recovered_commit'] and 'row order restored' in retry['message']
    assert state['store'].metadata_writes==3 and state['transfers']==0


def test_changed_voucher_archives_unchanged_sibling_lines_and_keeps_their_ids(pipeline):
    state = pipeline
    original = [row(description='Keep sibling'), row(description='Old detail'), row(description='Keep sibling')]
    state['store'] = Store(original)
    state['incoming'] = [original[2], row(description='Revised detail'), original[0]]
    result, validation = validate_then_import(state)
    assert result['updated_count'] == 1
    assert result['deleted_count'] == result['imported_count'] == 0
    assert {r['id'] for r in state['store'].rows if r['row_data']['Voucher Detail'] == 'Keep sibling'} == {1, 3}
    assert len(state['store'].history) == 1
    assert state['store'].history[0]['action'] == 'changed'
    assert state['store'].history[0]['row_data']['Voucher Detail'] == 'Old detail'
    assert state['store'].history[0]['changed_fields']['Voucher Detail'] == {
        'old_value': 'Old detail', 'new_value': 'Revised detail'
    }
    assert validation['change_detection']['changed_voucher_count'] == 1
    with zipfile.ZipFile(next(iter(state['store'].jobs.values()))['archive_path']) as archive:
        assert len(json.loads(archive.read('rows.json'))) == 3
        from openpyxl import load_workbook
        report = load_workbook(io.BytesIO(archive.read('Upload observation report.xlsx')))
        assert report['Previous vouchers'].max_row == report['Incoming vouchers'].max_row == 8


def test_amount_correction_does_not_reclassify_other_voucher_with_equivalent_numeric_encodings(pipeline, monkeypatch):
    import sys

    columns = deepcopy(COLUMNS)
    columns.insert(2, {'id': 7, 'column_name': 'Receive Date', 'data_type': 'Date'})
    monkeypatch.setattr(sys.modules[__name__], 'COLUMNS', columns)
    state = pipeline
    changed = {**row(voucher='PO-CHANGE', amount=2825), 'Receive Date': '11/02/2026'}
    unchanged = {**row(voucher='PO-UNCHANGED', amount=120), 'Receive Date': '11/02/2026'}
    state['store'] = Store([changed, unchanged])
    unchanged_id = next(item['id'] for item in state['store'].rows
                        if item['row_data']['Voucher ID'] == 'PO-UNCHANGED')
    state['incoming'] = [
        {**changed, 'Debit Amount': 2825.5},
        {**unchanged, 'Debit Amount': 120.0, 'Credit Amount': 0.0},
    ]

    result, validation = validate_then_import(state)

    changes = validation['change_detection']
    assert changes['changed_count'] == 1
    assert changes['unchanged_count'] == 1
    assert changes['new_count'] == changes['missing_count'] == 0
    assert result['updated_count'] == 1
    unchanged_after = [item for item in state['store'].rows
                       if item['row_data']['Voucher ID'] == 'PO-UNCHANGED']
    assert len(unchanged_after) == 1
    assert unchanged_after[0]['id'] == unchanged_id
    assert unchanged_after[0]['row_data']['Debit Amount'] == 120
    assert len(state['store'].history) == 1
    assert state['store'].history[0]['row_data']['Voucher ID'] == 'PO-CHANGE'


def test_changed_and_removed_rows_are_saved_to_independent_history(pipeline):
    state = pipeline
    state['store'] = Store([row(description='Old detail')])
    state['incoming'] = [row(description='Revised detail')]

    result, _ = validate_then_import(state, comparison_mode='correction')
    assert result['updated_count'] == 1 and result['deleted_count'] == 0
    assert [(item['action'], item['client_data_id'], item['row_data']['Voucher Detail'])
            for item in state['store'].history] == [('changed', 1, 'Old detail')]
    assert state['store'].history[0]['changed_fields']['Voucher Detail'] == {
        'old_value': 'Old detail', 'new_value': 'Revised detail'
    }

    state['store'] = Store([row('REMOVE', description='Old deleted')])
    state['incoming'] = []
    result, _ = validate_then_import(state, confirm_empty_period=True)
    assert result['deleted_count'] == 1
    assert [(item['action'], item['client_data_id'], item['row_data']['Voucher ID'])
            for item in state['store'].history] == [('deleted', 1, 'REMOVE')]
    assert state['store'].history[0]['changed_fields'] is None
    assert all(item['client_id'] == 5 and item['template_id'] == 1
               and item['source_upload_id'] == 1
               and item['replacing_upload_id'] == 99 for item in state['store'].history)


def test_history_snapshots_retain_ledger_account_context():
    from types import SimpleNamespace
    token = upload._ledger_context.set({
        'account_code': '50-95-0003', 'account_name': 'Electricity Bills'
    })
    try:
        params = upload._history_row_params(
            SimpleNamespace(client_id=5, template_id=2, file_id=99),
            {
                'client_data_id': 40,
                'old_row_data': {'Voucher ID': 'V1', 'Debit': 10},
                'new_row_data': {'Voucher ID': 'V1', 'Debit': 12},
                'changed_fields': ['Debit'],
            },
            'changed',
        )
    finally:
        upload._ledger_context.reset(token)

    assert params[9].adapted == {'Debit': {'old_value': 10, 'new_value': 12}}
    assert params[10:12] == ('50-95-0003', 'Electricity Bills')


def test_history_write_rolls_back_with_failed_import(pipeline, monkeypatch):
    state = pipeline
    state['store'] = Store([row(description='Old detail')])
    state['incoming'] = [row(description='Revised detail')]
    before = deepcopy(state['store'].rows)

    def fail_after_history(*args, **kwargs):
        raise RuntimeError('Derived write failed')

    monkeypatch.setattr(upload, 'upsert_classification_rows_postgres', fail_after_history)
    with pytest.raises(RuntimeError, match='Derived write failed'):
        validate_then_import(state)

    assert state['store'].rows == before
    assert state['store'].history == []


def test_reordered_duplicate_looking_lines_remain_a_noop(pipeline):
    state = pipeline
    source = [row(description='Same'), row(description='Other'), row(description='Same')]
    state['store'] = Store(source)
    state['incoming'] = list(reversed(source))
    result = upload.validate_only(request())
    assert not result['can_import'] and result['change_detection']['unchanged_count'] == 3
    assert state['store'].source_writes == state['transfers'] == 0


def test_extended_voucher_with_one_added_line_archives_the_complete_previous_voucher(pipeline):
    state = pipeline
    state['store'] = Store([row(description='Retained')])
    state['incoming'] = [row(description='Retained'), row(description='Additional line')]
    result, _ = validate_then_import(state)
    assert result['imported_count'] == 1 and result['deleted_count'] == 0
    assert len(state['store'].rows) == 2 and state['transfers'] == 1


def test_empty_confirmation_is_rejected_for_a_populated_report(pipeline):
    pipeline['store'] = Store([row()])
    pipeline['incoming'] = [row()]
    with pytest.raises(HTTPException, match='Uncheck'):
        upload.validate_only(request(confirm_empty_period=True))
    assert pipeline['store'].source_writes == 0


def test_absent_voucher_with_lines_on_both_sides_of_boundary_cannot_be_partly_removed(pipeline):
    pipeline['store'] = Store([row('ABSENT'), row('ABSENT', day='31/03/2026')])
    pipeline['incoming'] = []
    with pytest.raises(HTTPException, match='lines outside this period'):
        upload.validate_only(request(confirm_empty_period=True))
    assert pipeline['store'].source_writes == 0


@pytest.mark.parametrize('mutation', ['changed_sibling', 'outside_same_voucher', 'outside_other_voucher'])
def test_archive_guards_unchanged_siblings_and_date_moves_without_blocking_unrelated_dates(pipeline, mutation):
    state = pipeline
    state['store'] = Store([row(day='31/03/2026', description='Old')])
    state['incoming'] = [row(description='New')]
    validation = upload.validate_only(request())
    def mutate(store):
        if mutation == 'changed_sibling':
            store.rows[0]['row_data']['Voucher Detail'] = 'Concurrent change'
        elif mutation == 'outside_same_voucher':
            store.add(55, row(day='01/03/2026', description='Concurrent sibling'))
        else:
            store.add(55, row('OTHER', day='01/03/2026'))
    state['after_archive'] = mutate
    if mutation == 'outside_other_voucher':
        result = upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
        assert result['status'] == 'success' and len(state['store'].rows) == 2
    else:
        with pytest.raises(HTTPException) as error:
            upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
        assert error.value.status_code == 409 and state['store'].source_writes == 0


def test_archive_failure_leaves_source_unchanged_and_same_plan_can_retry(pipeline):
    state = pipeline
    state['store'] = Store([row(description='Old')]); state['incoming'] = [row(description='New')]
    validation = upload.validate_only(request())
    before = deepcopy(state['store'].rows)
    state['failure'] = True
    with pytest.raises(HTTPException) as error:
        upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert error.value.status_code == 503
    assert state['store'].rows == before and state['store'].source_writes == 0
    state['failure'] = False
    result = upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert result['archive_verified'] and len(state['store'].rows) == 1
    assert len(state['store'].jobs) == 1


def test_new_only_import_saves_a_verified_excel_report_and_returns_its_location(pipeline):
    state = pipeline
    state['store'] = Store([])
    state['incoming'] = [row(voucher='NEW-1')]
    validation = upload.validate_only(request())
    assert validation['can_import']
    result = upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert result['status'] == 'success'
    assert result['backup_mode'] == 'drive' and not result['archive_verified']
    assert result['change_report']['file_url'] == 'https://drive.google.com/file/d/report/view'
    assert result['change_report']['folder_path'].startswith('Upload Change Reports/')
    assert state['reports'] == 1 and state['transfers'] == 0
    retry = upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert retry['recovered_commit'] and retry['change_report'] == result['change_report']


def test_change_report_failure_blocks_new_only_import(pipeline):
    state = pipeline
    state['store'] = Store([])
    state['incoming'] = [row(voucher='NEW-1')]
    validation = upload.validate_only(request())
    state['failure'] = True
    with pytest.raises(HTTPException) as error:
        upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert error.value.status_code == 503
    assert state['store'].source_writes == 0 and len(state['store'].rows) == 0


def test_backup_policy_shows_drive_address_only_for_a_complete_connection(monkeypatch):
    monkeypatch.setattr(upload, 'execute_query_pg', lambda *args, **kwargs: [{'value': 'false'}])
    for name in ['DRIVE_CLIENT_ID', 'DRIVE_CLIENT_SECRET', 'DRIVE_REFRESH_TOKEN', 'DRIVE_ARCHIVE_FOLDER_ID']:
        monkeypatch.setenv(name, 'configured')
    monkeypatch.setenv('DRIVE_ARCHIVE_FOLDER_ID', 'folder_123')
    response = upload.upload_backup_policy(session={'role': 'admin'})
    assert response == {'allow_without_backup': False,
                        'archive_folder_url': 'https://drive.google.com/drive/folders/folder_123',
                        'drive_connection_ready': True}
    monkeypatch.delenv('DRIVE_REFRESH_TOKEN')
    response = upload.upload_backup_policy(session={'role': 'admin'})
    assert response['archive_folder_url'] == 'https://drive.google.com/drive/folders/folder_123'
    assert response['drive_connection_ready'] is False


def test_revoked_refresh_token_has_a_clear_reconnect_message_without_secrets(monkeypatch):
    from urllib.error import HTTPError
    for name in ['DRIVE_CLIENT_ID', 'DRIVE_CLIENT_SECRET', 'DRIVE_REFRESH_TOKEN', 'DRIVE_ARCHIVE_FOLDER_ID']:
        monkeypatch.setenv(name, 'configured')
    def rejected(url, *args, **kwargs):
        assert url.full_url == 'https://oauth2.googleapis.com/token'
        raise HTTPError(url.full_url, 400, 'Bad Request', {},
                        io.BytesIO(b'{"error":"invalid_grant","error_description":"Token has been expired or revoked."}'))
    monkeypatch.setattr(drive_archive, 'urlopen', rejected)
    client = drive_archive.DriveArchive()
    with pytest.raises(drive_archive.ArchiveError) as error:
        client._open('https://www.googleapis.com/drive/v3/files')
    assert error.value.code == 'drive_auth_reconnect_required'
    assert error.value.outcome == 'not_applied'
    assert 'Reconnect Google Drive in Settings' in str(error.value)
    assert 'No app data was changed' in str(error.value)


def test_revoked_refresh_token_import_response_says_no_data_was_applied(pipeline, monkeypatch):
    state = pipeline
    state['store'] = Store([])
    state['incoming'] = [row(voucher='NEW-1')]
    validation = upload.validate_only(request())

    class ExpiredDrive:
        def upload_change_report(self, *args, **kwargs):
            raise drive_archive.ArchiveError('Authorization expired',
                code='drive_auth_reconnect_required', outcome='not_applied')

    monkeypatch.setattr(lifecycle, 'DriveArchive', ExpiredDrive)
    with pytest.raises(HTTPException) as error:
        upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert error.value.status_code == 503
    assert error.value.detail == {'message': 'Authorization expired',
        'code': 'drive_auth_reconnect_required', 'outcome': 'not_applied'}
    assert state['store'].source_writes == 0 and state['store'].rows == []


@pytest.mark.parametrize('client_choice', [None, False, True])
def test_global_backup_on_applies_even_if_client_asks_to_skip(pipeline, client_choice):
    pipeline['allow_without_backup'] = False
    pipeline['store'] = Store([row(description='Old')])
    pipeline['incoming'] = [row(description='New')]
    result, validation = validate_then_import(pipeline, backup_enabled=client_choice)
    assert validation['backup_enabled'] is True
    assert result['archive_verified'] and pipeline['transfers'] == 1


@pytest.fixture
def backup_settings_api(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import settings, auth
    state = {'value': None, 'actor': 'admin', 'writes': 0}
    def read(sql, params):
        assert params == (lifecycle.UPLOAD_BACKUP_SETTING,)
        return [] if state['value'] is None else [{'value': state['value']}]
    def write(sql, params):
        assert 'ON CONFLICT (key)' in sql and params[0] == lifecycle.UPLOAD_BACKUP_SETTING
        state['value'] = params[1]
        state['writes'] += 1
    def actor():
        if state['actor'] is None:
            raise HTTPException(401, 'Not authenticated')
        return {'role': state['actor'], 'username': 'Isolated test'}
    monkeypatch.setattr(lifecycle, 'execute_query_pg', read)
    monkeypatch.setattr(settings, 'execute_write_pg', write)
    monkeypatch.setattr(settings, 'log_audit', lambda *args: None)
    app = FastAPI()
    app.include_router(settings.router)
    app.dependency_overrides[auth.get_current_user] = actor
    with TestClient(app) as client:
        yield client, state


def test_admin_backup_setting_is_shared_persistent_and_overrides_old_environment(backup_settings_api, monkeypatch):
    client, state = backup_settings_api
    monkeypatch.setenv('DEVELOPMENT_ALLOW_UNBACKED_UPLOADS', 'true')
    path = '/api/settings/upload-backups'
    assert client.get(path).json() == {'allow_without_backup': False}
    for allowed in (True, False):
        response = client.post(path, json={'allow_without_backup': allowed})
        assert response.status_code == 200
        assert client.get(path).json() == {'allow_without_backup': allowed}
        assert lifecycle.unbacked_uploads_allowed() is allowed
    assert state['writes'] == 2
    assert client.post(path, json={'allow_without_backup': 'true'}).status_code == 422
    assert state['writes'] == 2


@pytest.mark.parametrize('actor,status', [(None, 401), ('analyst', 403)])
def test_backup_setting_rejects_nonadministrator_reads_and_writes(backup_settings_api, actor, status):
    client, state = backup_settings_api
    state['actor'] = actor
    path = '/api/settings/upload-backups'
    assert client.get(path).status_code == status
    assert client.post(path, json={'allow_without_backup': True}).status_code == status
    # The old generic Settings endpoint must not offer an alternate write path.
    assert client.post('/api/settings/' + lifecycle.UPLOAD_BACKUP_SETTING,
                       json={'value': 'true'}).status_code == 403
    assert state['writes'] == 0


@pytest.mark.parametrize('protected', [False, True])
@pytest.mark.parametrize('client_choice', [None, False, True])
def test_development_replacement_skips_archives_and_retains_changed_rows_in_history(pipeline, monkeypatch, protected, client_choice):
    pipeline['allow_without_backup'] = True
    state = pipeline
    state['store'] = Store([row(description='Old')])
    state['store'].protected = protected
    state['incoming'] = [row(description='New')]
    monkeypatch.setattr(lifecycle, 'archive_directory', lambda: pytest.fail('No local archive allowed'))
    monkeypatch.setattr(lifecycle, 'DriveArchive', lambda: pytest.fail('No Drive calls allowed'))
    before = deepcopy(state['store'].rows)
    validation = upload.validate_only(request(backup_enabled=client_choice))
    assert validation['backup_enabled'] is False
    importing = request(backup_enabled=client_choice, validation_plan_id=validation['validation_plan_id'])
    result = upload.validate_and_import(importing)
    assert result['status'] == 'success' and result['backup_mode'] == 'none'
    assert result['updated_count'] == 1 and result['imported_count'] == result['deleted_count'] == 0
    assert not result['archive_verified'] and state['transfers'] == 0
    assert len(state['store'].rows) == 1 and state['store'].rows[0]['id'] == before[0]['id']
    assert state['store'].rows[0]['row_data']['Voucher Detail'] == 'New'
    assert len(state['store'].history) == 1
    assert state['store'].history[0]['row_data']['Voucher Detail'] == 'Old'
    recovered = upload.validate_and_import(importing)
    assert recovered['recovered_commit'] and recovered['backup_mode'] == 'none'
    assert all(not job.get('archive_path') and not job.get('drive_file_id') for job in state['store'].jobs.values())


@pytest.mark.parametrize('initial', [True, False])
def test_global_backup_setting_is_bound_to_validation_in_both_directions(pipeline, initial):
    pipeline['allow_without_backup'] = initial
    state = pipeline
    state['store'] = Store([row(description='Old')]); state['incoming'] = [row(description='New')]
    validation = upload.validate_only(request())
    pipeline['allow_without_backup'] = not initial
    with pytest.raises(HTTPException) as error:
        upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert error.value.status_code == 409 and state['store'].source_writes == 0


def test_render_backup_on_verifies_drive_without_persistent_local_directory(pipeline, monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    monkeypatch.delenv('UPLOAD_ARCHIVE_DIR')
    monkeypatch.setenv('UPLOAD_ARCHIVE_TO_DRIVE', 'false')
    state = pipeline
    state['store'] = Store([row(description='Old')]); state['incoming'] = [row(description='New')]
    result, _ = validate_then_import(state, backup_enabled=True)
    assert result['archive_verified'] and state['transfers'] == 1
    assert result['backup_mode'] == 'drive'
    assert not __import__('pathlib').Path(next(iter(state['store'].jobs.values()))['archive_path']).exists()


@pytest.mark.parametrize('change', ['source', 'permission'])
def test_unbacked_guard_rejects_changes_before_source_writes(pipeline, monkeypatch, change):
    pipeline['allow_without_backup'] = True
    state = pipeline
    state['store'] = Store([row(description='Old')]); state['incoming'] = [row(description='New')]
    guard = upload.guard_lifecycle
    def changed_before_guard(prepared, query):
        if change == 'source':
            state['store'].rows[0]['row_data']['Voucher Detail'] = 'Concurrent update'
        else:
            pipeline['allow_without_backup'] = False
        return guard(prepared, query)
    monkeypatch.setattr(upload, 'guard_lifecycle', changed_before_guard)
    validation = upload.validate_only(request(backup_enabled=False))
    with pytest.raises(HTTPException) as error:
        upload.validate_and_import(request(backup_enabled=False, validation_plan_id=validation['validation_plan_id']))
    assert error.value.status_code == 409 and state['store'].source_writes == 0


def test_unbacked_write_failure_rolls_back_and_same_plan_can_retry(pipeline, monkeypatch):
    pipeline['allow_without_backup'] = True
    state = pipeline
    state['store'] = Store([row(description='Old')]); state['incoming'] = [row(description='New')]
    before = deepcopy(state['store'].rows)
    validation = upload.validate_only(request(backup_enabled=False))
    importing = request(backup_enabled=False, validation_plan_id=validation['validation_plan_id'])
    original = upload.upsert_classification_rows_postgres
    def interrupted(*args, **kwargs):
        assert state['store'].source_writes > 0
        raise RuntimeError('Interrupted write')
    monkeypatch.setattr(upload, 'upsert_classification_rows_postgres', interrupted)
    with pytest.raises(RuntimeError, match='Interrupted write'):
        upload.validate_and_import(importing)
    assert state['store'].rows == before and state['store'].source_writes == 0
    monkeypatch.setattr(upload, 'upsert_classification_rows_postgres', original)
    assert upload.validate_and_import(importing)['backup_mode'] == 'none'


@pytest.mark.parametrize('mutation', ['append', 'edit', 'audit', 'configuration'])
def test_changes_during_archive_reject_stale_plan_without_source_replacement(pipeline, mutation):
    state = pipeline
    state['store'] = Store([row(description='Old')]); state['incoming'] = [row(description='New')]
    validation = upload.validate_only(request())
    def mutate(store):
        if mutation == 'append': store.add(100, row('CONCURRENT'))
        elif mutation == 'edit': store.rows[0]['row_data']['Voucher Detail'] = 'Concurrent edit'
        elif mutation == 'audit': store.audits.append({'id': 1, 'client_data_id': 1, 'action': 'Concurrent audit'})
        else: store.columns[0]['column_name'] = 'Renamed'
    state['after_archive'] = mutate
    with pytest.raises(HTTPException) as error:
        upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert error.value.status_code == 409
    assert state['store'].source_writes == 0
    assert next(iter(state['store'].jobs.values()))['status'] == 'verified'


def test_changed_row_with_protected_evidence_keeps_old_values_in_history(pipeline):
    state = pipeline
    state['store'] = Store([row(description='Old')]); state['incoming'] = [row(description='New')]
    state['store'].protected = True
    validation = upload.validate_only(request())
    result = upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert result['updated_count'] == 1 and result['imported_count'] == result['deleted_count'] == 0
    assert state['store'].rows[0]['id'] == 1
    assert state['store'].rows[0]['row_data']['Voucher Detail'] == 'New'
    assert state['store'].history[0]['row_data']['Voucher Detail'] == 'Old'
    assert next(iter(state['store'].jobs.values()))['status'] == 'applied'


@pytest.mark.parametrize('options', [{'excluded_rows': [2]}, {'force_include_rows': [2]}, {'coverage_end': '2026-04-10'}])
def test_incomplete_or_outside_source_cannot_become_deletions(pipeline, options):
    state = pipeline
    state['store'] = Store([row()]); state['incoming'] = [row()]
    with pytest.raises(HTTPException): upload.validate_only(request(**options))
    assert state['store'].source_writes == 0 and not state['store'].jobs


def test_shifted_voucher_date_follows_same_id_and_retains_other_outside_vouchers(pipeline):
    state = pipeline
    state['store'] = Store([row(day='31/03/2026'), row('OUTSIDE', day='31/03/2026')])
    state['incoming'] = [row()]
    result, validation = validate_then_import(state)
    assert result['updated_count'] == 1
    assert result['imported_count'] == result['deleted_count'] == 0
    assert len(state['store'].rows) == 2
    assert {(r['row_data']['Voucher ID'], r['extract_date']) for r in state['store'].rows} == {
        ('V1', '2026-04-15'), ('OUTSIDE', '2026-03-31')}
    assert state['store'].plans == {}
    with zipfile.ZipFile(next(iter(state['store'].jobs.values()))['archive_path']) as archive:
        assert 'Upload observation report.xlsx' in archive.namelist()
        observations = json.loads(archive.read('observations.json'))
        assert observations[0]['old_ids'] == [1]
        assert 'Transaction Date' in observations[0]['changed_fields']


@pytest.mark.parametrize('change', ['superseded', 'expired', 'removed'])
def test_plan_superseded_or_expired_during_archive_never_applies(pipeline, change):
    state = pipeline
    state['store'] = Store([row(description='Old')])
    state['incoming'] = [row(description='New')]
    before = deepcopy(state['store'].rows)

    def invalidate(store):
        if change == 'removed':
            store.plans.clear()
        else:
            plan = next(iter(store.plans.values()))
            if change == 'expired':
                plan['expires_at'] = datetime.now() - timedelta(seconds=1)
            else:
                plan['status'] = 'superseded'

    state['after_archive'] = invalidate
    with pytest.raises(HTTPException) as error:
        validate_then_import(state)
    assert error.value.status_code == 409
    assert 'superseded' in error.value.detail
    assert state['store'].rows == before and state['store'].source_writes == 0


def test_partial_correction_archives_changed_row_and_retains_absent_rows(pipeline):
    state = pipeline
    state['store'] = Store([row(description='Old'), row('ABSENT')])
    state['incoming'] = [row(description='New')]
    result, _ = validate_then_import(state, comparison_mode='correction')
    assert result['updated_count'] == 1 and result['deleted_count'] == 0
    assert len(state['store'].rows) == 2
    assert any(r['row_data']['Voucher ID'] == 'ABSENT' for r in state['store'].rows)
    assert state['transfers'] == 1


def test_ledger_archive_scope_filters_only_selected_account():
    sql, params = lifecycle.scope_sql({'client_id': 5, 'template_id': 2,
        'start': '2026-01-01', 'end': '2026-10-25', 'account_code': 'BANK-A'})
    assert params == [5, 2, '2026-01-01', '2026-10-25', 'BANK-A']
    assert 'lu.ledger_account_code = %s' in sql and 'lu.id = cd.upload_id' in sql


def test_schema_is_idempotent_and_contains_metadata_only():
    with sqlite3.connect(':memory:') as conn:
        database._ensure_upload_lifecycle_schema(conn)
        database._ensure_upload_lifecycle_schema(conn)
        columns = {r[1] for r in conn.execute('PRAGMA table_info(upload_lifecycle_runs)')}
    assert {'operation_id', 'archive_sha256', 'result_json'} <= columns
    assert not {'row_data', 'old_row_data', 'plan_json'} & columns


def test_google_drive_downloading_wrong_bytes_never_verifies(monkeypatch, tmp_path):
    for name in ['DRIVE_CLIENT_ID', 'DRIVE_CLIENT_SECRET', 'DRIVE_REFRESH_TOKEN', 'DRIVE_ARCHIVE_FOLDER_ID']:
        monkeypatch.setenv(name, 'configured')
    path = tmp_path / 'archive.zip'; path.write_bytes(b'original')
    checksum = hashlib.sha256(b'original').hexdigest()
    client = drive_archive.DriveArchive()
    monkeypatch.setattr(client, '_metadata', lambda *a: {'parents': ['configured'], 'size': 8,
        'appProperties': {'operation_id': 'operation', 'sha256': checksum}})
    monkeypatch.setattr(client, '_open', lambda *a, **kw: io.BytesIO(b'corrupt!'))
    with pytest.raises(drive_archive.ArchiveError, match='checksum'):
        client.upload_and_verify('remote', path, 'operation', checksum)


def test_app_connection_is_required_and_local_archive_cannot_live_in_checkout(monkeypatch):
    for name in ['DRIVE_CLIENT_ID', 'DRIVE_CLIENT_SECRET', 'DRIVE_REFRESH_TOKEN', 'DRIVE_ARCHIVE_FOLDER_ID']:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(drive_archive.ArchiveError, match='Configure DRIVE_CLIENT_ID'):
        drive_archive.DriveArchive()
    monkeypatch.setenv('UPLOAD_ARCHIVE_DIR', str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'uploads'))
    with pytest.raises(drive_archive.ArchiveError, match='outside'):
        drive_archive.archive_directory()


def test_database_failure_after_source_writes_rolls_back_and_can_retry(pipeline, monkeypatch):
    state = pipeline
    state['store'] = Store([row(description='Old')]); state['incoming'] = [row(description='New')]
    before = deepcopy(state['store'].rows)
    validation = upload.validate_only(request())
    def fail(*a, **kw):
        assert state['store'].source_writes > 0
        raise RuntimeError('Interrupted classification')
    monkeypatch.setattr(upload, 'upsert_classification_rows_postgres', fail)
    with pytest.raises(RuntimeError, match='Interrupted'):
        upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert state['store'].rows == before and state['store'].source_writes == 0
    assert next(iter(state['store'].jobs.values()))['status'] == 'verified'
    monkeypatch.setattr(upload, 'upsert_classification_rows_postgres', lambda *a, **kw: None)
    result = upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert result['updated_count'] == 1
    assert result['deleted_count'] == result['imported_count'] == 0


def test_postgres_scope_guard_uses_small_server_aggregate_instead_of_returning_rows():
    records = [{'id': 2, 'scope_row_hash': 'b' * 64}, {'id': 1, 'scope_row_hash': 'a' * 64}]
    expected = hashlib.sha256(('a' * 64 + 'b' * 64).encode()).hexdigest()
    assert lifecycle.source_fingerprint(records) == expected
    calls = []
    def query(sql, params):
        calls.append((sql, params))
        return [{'scope_fingerprint': expected}]
    lifecycle.check_scope({'scope': {'client_id': 5, 'template_id': 1, 'start': '2026-01-01', 'end': '2026-10-25'},
        'source_fingerprint': expected, 'fingerprint_method': 'postgres'}, query)
    assert 'ORDER BY cd.id' in calls[0][0] and 'string_agg' in calls[0][0]
    assert calls[0][1] == (5, 1, '2026-01-01', '2026-10-25')


def test_google_drive_create_and_retry_verify_identical_remote_file(monkeypatch, tmp_path):
    from urllib.error import HTTPError
    for name in ['DRIVE_CLIENT_ID', 'DRIVE_CLIENT_SECRET', 'DRIVE_REFRESH_TOKEN', 'DRIVE_ARCHIVE_FOLDER_ID']:
        monkeypatch.setenv(name, 'configured')
    path = tmp_path / 'archive.zip'; path.write_bytes(b'archive bytes')
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    client = drive_archive.DriveArchive()
    calls, created = [], []
    def metadata(*a):
        if not created:
            raise HTTPError('https://example.invalid', 404, 'Not found', {}, None)
        return {'parents': ['configured'], 'size': len(path.read_bytes()),
                'appProperties': {'operation_id': 'operation', 'sha256': checksum}}
    def network(url, data=None, *a, **kw):
        calls.append(url)
        if 'upload/drive' in url:
            assert path.read_bytes() in data and b'"id": "remote"' in data
            created.append('remote')
            return io.BytesIO(b'{}')
        return io.BytesIO(path.read_bytes())
    monkeypatch.setattr(client, '_metadata', metadata)
    monkeypatch.setattr(client, '_open', network)
    for _ in range(2):
        proof = client.upload_and_verify('remote', path, 'operation', checksum)
        assert proof['sha256'] == checksum
    assert sum('upload/drive' in url for url in calls) == 1


def test_google_drive_dated_change_report_is_verified_and_retry_reuses_file(monkeypatch):
    from urllib.parse import parse_qs
    for name in ['DRIVE_CLIENT_ID', 'DRIVE_CLIENT_SECRET', 'DRIVE_REFRESH_TOKEN', 'DRIVE_ARCHIVE_FOLDER_ID']:
        monkeypatch.setenv(name, 'configured')
    client = drive_archive.DriveArchive()
    content = b'PK sample workbook bytes'
    folders = {'configured': {'id': 'configured', 'name': 'Internal Audit Upload Archives',
        'mimeType': 'application/vnd.google-apps.folder', 'parents': [], 'capabilities': {'canAddChildren': True}}}
    files = {}
    next_id = iter(['folder-1', 'folder-2', 'folder-3', 'folder-4', 'report-1'])
    upload_calls = []

    def json_api(path, data=None, method=None):
        if path.startswith('files/configured?'):
            return folders['configured']
        if path.startswith('files/generateIds?'):
            return {'ids': [next(next_id)]}
        if path.startswith('files?supportsAllDrives=true'):
            values = json.loads(data)
            identifier = next(next_id)
            folder = {'id': identifier, **values, 'webViewLink': f'https://drive.google.com/drive/folders/{identifier}'}
            folders[identifier] = folder
            return folder
        if path.startswith('files?'):
            query = parse_qs(path.split('?', 1)[1]).get('q', [''])[0]
            parent = next((key for key in folders if f"'{key}' in parents" in query), None)
            name = next((folder for folder in folders.values()
                         if folder.get('name') in query and parent in folder.get('parents', [])), None)
            report = next((item for item in files.values()
                           if item['name'] in query and parent in item['parents']), None)
            return {'files': [report] if report else [name] if name else []}
        if path.startswith('files/report-1?'):
            return files['report-1']
        raise AssertionError(path)

    def metadata(identifier):
        return files[identifier]

    def network(url, data=None, *args, **kwargs):
        if '/upload/drive/v3/files?' in url:
            upload_calls.append(url)
            identifier = 'report-1'
            parent = next(value for value in folders if value.startswith('folder-4'))
            files[identifier] = {'id': identifier,
                'name': '143000-Account-Ledger-' + ('a' * 32) + '.xlsx',
                'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'parents': [parent], 'size': len(content), 'trashed': False,
                'webViewLink': 'https://drive.google.com/file/d/report-1/view',
                'appProperties': {'operation_id': 'a' * 32, 'sha256': hashlib.sha256(content).hexdigest()}}
            return io.BytesIO(b'{}')
        if '/files/report-1?alt=media' in url:
            return io.BytesIO(content)
        raise AssertionError(url)

    monkeypatch.setattr(client, '_json', json_api)
    monkeypatch.setattr(client, '_metadata', metadata)
    monkeypatch.setattr(client, '_open', network)
    for _ in range(2):
        result = client.upload_change_report(content, '143000-Account-Ledger-' + ('a' * 32) + '.xlsx',
            'Leather-Crop', '2026-11-03', 'a' * 32)
        assert result['file_url'] == 'https://drive.google.com/file/d/report-1/view'
        assert result['folder_path'] == 'Upload Change Reports/Leather-Crop/2026/2026-11-03'
    assert len(upload_calls) == 1


@pytest.mark.parametrize('field,value', [('parents', ['other-folder']), ('size', 999), ('trashed', True),
                                       ('appProperties', {'operation_id': 'different-operation'})])
def test_google_drive_wrong_scope_or_archive_metadata_blocks_verification(monkeypatch, tmp_path, field, value):
    for name in ['DRIVE_CLIENT_ID', 'DRIVE_CLIENT_SECRET', 'DRIVE_REFRESH_TOKEN', 'DRIVE_ARCHIVE_FOLDER_ID']:
        monkeypatch.setenv(name, 'configured')
    path = tmp_path / 'archive.zip'; path.write_bytes(b'original')
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata = {'parents': ['configured'], 'size': 8,
                'appProperties': {'operation_id': 'operation', 'sha256': checksum}, field: value}
    client = drive_archive.DriveArchive()
    monkeypatch.setattr(client, '_metadata', lambda *a: metadata)
    monkeypatch.setattr(client, '_open', lambda *a, **kw: pytest.fail('Mismatched metadata must stop before content access'))
    with pytest.raises(drive_archive.ArchiveError, match='metadata'):
        client.upload_and_verify('remote', path, 'operation', checksum)


def controlled_source(state, details=None):
    """Configure Footing on an isolated template, never on shared master columns."""
    state['store'] = Store([])
    for column in state['store'].columns:
        if column['column_name'] == 'Voucher ID':
            column['reconciliation_role'] = 'document_id'
        elif column['column_name'] in ('Debit Amount', 'Credit Amount'):
            column['reconciliation_role'] = 'footing'
    details = details if details is not None else [row('V1'), row('V1'), row('V2', amount=15)]
    state['incoming'] = details + [{'Voucher ID': None, 'Transaction Date': None, 'Account Detail': None,
        'Voucher Detail': 'TOTAL', 'Debit Amount': sum(r['Debit Amount'] for r in details),
        'Credit Amount': sum(r['Credit Amount'] for r in details)}]
    return {'control_total_row': len(details) + 2, 'control_total_confirmed': True}


def test_footing_plan_import_preserves_repeated_lines_and_compact_controls(pipeline):
    choices = controlled_source(pipeline)
    result, validation = validate_then_import(pipeline, **choices)
    assert validation['total_rows'] == result['total_rows'] == 3
    assert result['imported_count'] == 3
    proof = result['source_reconciliation']
    assert proof['status'] == 'matched'
    assert proof['full_sheet']['document_count'] == 2
    assert proof['approved_detail']['footing_totals']['Debit Amount'] == '35'
    assert len(pipeline['store'].rows) == 3
    assert sum(r['row_data']['Voucher ID'] == 'V1' for r in pipeline['store'].rows) == 2
    writes = pipeline['store'].source_writes
    retry = upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id'], **choices))
    assert retry['recovered_commit']
    assert retry['imported_count'] == 3
    assert pipeline['store'].source_writes == writes


def test_subcent_footing_difference_is_auto_accepted_without_rounding(pipeline):
    choices = controlled_source(pipeline)
    pipeline['incoming'][-1]['Debit Amount'] = Decimal('35.0000000064')
    result, validation = validate_then_import(pipeline, **choices)
    control = result['source_reconciliation']
    debit = next(item for item in control['checks'] if item['column'] == 'Debit Amount')
    assert validation['can_import']
    assert control['status'] == 'rounding_tolerated'
    assert debit['difference'] == '-0.0000000064'
    assert debit['excel_total'] == '35.0000000064'
    assert debit['detail_total'] == '35'
    assert control['approved_detail']['footing_totals']['Debit Amount'] == '35'
    assert result['imported_count'] == 3 and pipeline['store'].source_writes == 3


@pytest.mark.parametrize(('difference', 'expected_status'), [
    ('0.009999999', 'rounding_tolerated'),
    ('0.01', 'confirmation_required'),
    ('-0.01', 'confirmation_required'),
    ('1', 'confirmation_required'),
    ('-1', 'confirmation_required'),
    ('1.0000001', 'mismatch'),
])
def test_footing_variance_policy_boundaries(pipeline, difference, expected_status):
    choices = controlled_source(pipeline)
    pipeline['incoming'][-1]['Debit Amount'] = Decimal('35') - Decimal(difference)
    validation = upload.validate_only(request(**choices))
    if expected_status == 'confirmation_required':
        assert validation['status'] == 'footing_confirmation_required'
        assert validation['footing_variance_confirmation_required']
        assert not validation['has_errors'] and not validation['can_import']
        assert validation['source_reconciliation']['status'] == expected_status
        assert not pipeline['store'].plans and pipeline['store'].source_writes == 0
    elif expected_status == 'mismatch':
        assert validation['status'] == 'validation_failed'
        assert validation['source_reconciliation']['status'] == expected_status
        assert not pipeline['store'].plans and pipeline['store'].source_writes == 0
    else:
        assert validation['can_import']
        assert validation['source_reconciliation']['status'] == expected_status


def test_footing_variance_requires_acknowledgement_and_binds_it_to_import_plan(pipeline):
    choices = controlled_source(pipeline)
    pipeline['incoming'][-1]['Debit Amount'] = Decimal('34')
    pending = upload.validate_only(request(**choices))
    assert pending['status'] == 'footing_confirmation_required'
    assert not pipeline['store'].plans and pipeline['store'].source_writes == 0

    confirmed = upload.validate_only(request(**choices, confirm_footing_variance=True))
    assert confirmed['can_import'] and confirmed['source_reconciliation']['status'] == 'confirmed_variance'
    assert confirmed['source_reconciliation']['tolerance_policy']['variance_confirmed']
    with pytest.raises(HTTPException) as error:
        upload.validate_and_import(request(validation_plan_id=confirmed['validation_plan_id'], **choices))
    assert error.value.status_code == 409
    assert pipeline['store'].source_writes == 0

    result = upload.validate_and_import(request(validation_plan_id=confirmed['validation_plan_id'],
                                                confirm_footing_variance=True, **choices))
    assert result['imported_count'] == 3
    assert result['source_reconciliation']['checks'][0]['difference'] == '1'
    assert result['source_reconciliation']['status'] == 'confirmed_variance'


def test_mismatching_source_footing_cannot_create_plan_or_write_transactions(pipeline):
    choices = controlled_source(pipeline)
    pipeline['incoming'][-1]['Debit Amount'] += Decimal('1.01')
    validation = upload.validate_only(request(**choices))
    assert validation['status'] == 'validation_failed'
    assert validation['source_reconciliation']['status'] == 'mismatch'
    assert not pipeline['store'].plans and pipeline['store'].source_writes == 0


def test_partial_exclusions_compare_footer_with_full_sheet_and_retain_approved_subset(pipeline):
    choices = controlled_source(pipeline)
    result, validation = validate_then_import(pipeline, comparison_mode='correction', excluded_rows=[3], **choices)
    proof = validation['source_reconciliation']
    assert proof['full_sheet']['row_count'] == 3
    assert proof['full_sheet']['footing_totals']['Debit Amount'] == '35'
    assert proof['approved_detail']['row_count'] == 2
    assert proof['approved_detail']['footing_totals']['Debit Amount'] == '25'
    assert proof['scope']['complete_period'] is False
    assert result['imported_count'] == 2


@pytest.mark.parametrize('change', ['row', 'confirmation', 'selection', 'dates', 'template', 'missing_proof'])
def test_footing_plan_rejects_stale_choices_or_missing_proof_before_writes(pipeline, change):
    choices = controlled_source(pipeline)
    validation = upload.validate_only(request(**choices))
    assert validation['can_import']
    options = {**choices, 'validation_plan_id': validation['validation_plan_id']}
    if change == 'row': options['control_total_row'] -= 1
    if change == 'confirmation': options['control_total_confirmed'] = False
    if change == 'selection': options['excluded_rows'] = [2]
    if change == 'dates': options['coverage_end'] = '2026-04-29'
    if change == 'template': pipeline['store'].columns[-1]['reconciliation_role'] = None
    if change == 'missing_proof': pipeline['store'].plans[validation['validation_plan_id']]['plan_json'].pop('source_reconciliation')
    with pytest.raises(HTTPException) as error:
        upload.validate_and_import(request(**options))
    assert error.value.status_code == 409
    assert pipeline['store'].source_writes == 0


def test_direct_import_and_source_edits_cannot_bypass_footing_validation(pipeline):
    choices = controlled_source(pipeline)
    with pytest.raises(HTTPException, match='control totals first'):
        upload.validate_and_import(request(comparison_mode='correction', skip_validation=True, **choices))
    assert pipeline['store'].source_writes == 0


def test_complete_footing_report_still_rejects_excluded_detail_or_out_of_period_rows(pipeline):
    choices = controlled_source(pipeline)
    for options in ({'excluded_rows': [2]}, {'coverage_start': '2026-04-16'}):
        with pytest.raises(HTTPException):
            upload.validate_only(request(**choices, **options))
    assert pipeline['store'].source_writes == 0


def test_empty_footing_report_requires_confirmed_zero_source_total(pipeline):
    choices = controlled_source(pipeline, [])
    pipeline['store'].add(1, row())
    result, validation = validate_then_import(pipeline, confirm_empty_period=True, **choices)
    assert result['deleted_count'] == 1 and not pipeline['store'].rows
    assert validation['source_reconciliation']['approved_detail']['row_count'] == 0


def test_post_import_sql_totals_follow_writes_and_are_recovered_without_reimport(pipeline):
    choices = controlled_source(pipeline)
    pipeline['store'].add(91, row('OUTSIDE', '01/05/2026', amount=999))
    pipeline['store'].add(92, row('OTHER-CLIENT', amount=888), client=6)
    result, validation = validate_then_import(pipeline, **choices)
    proof = result['reconciliation']
    assert proof['status'] == 'matched'
    assert [item['stored'] for item in proof['rows']] == [3, 2, '35', '0']
    assert pipeline['store'].aggregate_calls == 1
    assert pipeline['store'].aggregate_after_writes == 3
    pipeline['store'].rows[0]['row_data']['Debit Amount'] = 1000
    retry = upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id'], **choices))
    assert retry['reconciliation'] == proof and retry['recovered_commit']
    assert pipeline['store'].aggregate_calls == 1
    assert pipeline['store'].source_writes == 3


@pytest.mark.parametrize('problem,status', [('query', 'unavailable'), ('difference', 'mismatch')])
def test_post_import_query_failure_or_difference_is_not_reported_as_failed_commit(pipeline, problem, status):
    choices = controlled_source(pipeline)
    if problem == 'query': pipeline['store'].aggregate_error = True
    else: pipeline['store'].aggregate_override = {'total_0': Decimal('36')}
    result, validation = validate_then_import(pipeline, **choices)
    assert result['status'] == 'success' and result['imported_count'] == 3
    assert result['reconciliation']['status'] == status
    assert json.loads(next(iter(pipeline['store'].jobs.values()))['result_json'])['reconciliation'] == result['reconciliation']
    assert len(pipeline['store'].rows) == 3


def test_unchanged_repeat_verifies_sql_without_creating_plan_or_writing_detail(pipeline):
    choices = controlled_source(pipeline)
    for i, detail in enumerate(pipeline['incoming'][:-1], 1):
        pipeline['store'].add(i, detail)
    before = deepcopy(pipeline['store'].rows)
    result = upload.validate_only(request(**choices))
    assert not result['can_import'] and not result['validation_plan_id']
    assert result['reconciliation']['status'] == 'matched'
    assert pipeline['store'].aggregate_calls == 1
    assert pipeline['store'].rows == before and pipeline['store'].source_writes == 0


def test_partial_import_never_certifies_whole_period_even_when_counts_match(pipeline):
    choices = controlled_source(pipeline)
    result, _ = validate_then_import(pipeline, comparison_mode='correction', **choices)
    assert result['reconciliation']['status'] == 'not_comparable'
    assert all(item['matches'] is None for item in result['reconciliation']['rows'])


def test_concurrent_noop_change_with_identical_counts_and_totals_cannot_be_certified(pipeline, monkeypatch):
    choices = controlled_source(pipeline)
    for i, detail in enumerate(pipeline['incoming'][:-1], 1): pipeline['store'].add(i, detail)
    verify = upload._verify_import_controls
    def changed_before_check(*args, **kwargs):
        pipeline['store'].rows[0]['row_data']['Voucher Detail'] = 'Concurrent correction'
        return verify(*args, **kwargs)
    monkeypatch.setattr(upload, '_verify_import_controls', changed_before_check)
    result = upload.validate_only(request(**choices))
    assert not result['can_import']
    assert result['reconciliation']['status'] == 'unavailable'
    assert pipeline['store'].source_writes == 0


def test_failure_after_verification_rolls_back_both_rows_and_reconciliation_receipt(pipeline, monkeypatch):
    choices = controlled_source(pipeline)
    validation = upload.validate_only(request(**choices))
    finish = upload.finish_lifecycle
    def fail_before_commit(*args, **kwargs):
        finish(*args, **kwargs)
        raise RuntimeError('Simulated failure after receipt write')
    monkeypatch.setattr(upload, 'finish_lifecycle', fail_before_commit)
    with pytest.raises(RuntimeError, match='after receipt write'):
        upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id'], **choices))
    assert pipeline['store'].aggregate_calls == 1
    assert not pipeline['store'].rows and pipeline['store'].source_writes == 0
    assert next(iter(pipeline['store'].jobs.values()))['status'] != 'applied'
