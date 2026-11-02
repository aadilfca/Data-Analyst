"""Isolated replacement/archive acceptance: no live database, Drive or imports."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import io
import json
import sqlite3
import zipfile

import pytest
from fastapi import HTTPException

import database
from routers import import_export as upload
from services import upload_lifecycle as lifecycle, storage_cleanup, drive_archive


COLUMNS = [{'id': i, 'column_name': name, 'data_type': kind} for i, (name, kind) in enumerate([
    ('Voucher ID', 'Text'), ('Transaction Date', 'Date'), ('Account Detail', 'Text'),
    ('Voucher Detail', 'Text'), ('Debit Amount', 'Numeric'), ('Credit Amount', 'Numeric'),
], 1)]


def row(voucher='V1', day='15/04/2026', description='Supplies', amount=10):
    return {'Voucher ID': voucher, 'Transaction Date': day, 'Account Detail': 'A',
            'Voucher Detail': description, 'Debit Amount': amount, 'Credit Amount': 0}


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
        self.jobs, self.plans, self.audits = {}, {}, []
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
        before = deepcopy((self.rows, self.jobs, self.plans, self.file, self.audits, self.source_writes, self.metadata_writes)) if self.depth == 0 else None
        self.depth += 1
        try:
            yield self
        except BaseException:
            if before is not None:
                self.rows, self.jobs, self.plans, self.file, self.audits, self.source_writes, self.metadata_writes = before
            raise
        finally:
            self.depth -= 1

    def query(self, sql, params=None, **kwargs):
        params = params or ()
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
            if 'drive_file_id = ' in sql:
                job['drive_file_id'] = job.get('drive_file_id') or params[0]
            elif "status = 'verified'" in sql:
                job.update(status='verified', archive_sha256=params[0], archive_path=params[1], archive_state_hash=params[2])
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
        elif 'UPDATE client_data SET source_row_number' in sql:
            for number, source, key, client, template in values:
                target = next(r for r in self.rows if r['id'] == key and r['client_id'] == client and r['template_id'] == template)
                target.update(source_row_number=number, source_order_upload_id=source)
                self.metadata_writes += 1
        elif 'UPDATE client_data' in sql:
            for value in values:
                target = next(r for r in self.rows if r['id'] == value[-1])
                target.update(row_data=deepcopy(value[0].adapted), upload_id=value[1], extract_date=value[6], source_identity=value[7],
                              source_row_number=value[9], source_order_upload_id=value[10])
                self.source_writes += 1


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    import pandas as pd
    state = {'store': None, 'incoming': [], 'failure': False, 'after_archive': None, 'transfers': 0}
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
        mappings=[upload.ColumnMapping(template_column_id=c['id'], template_column_name=c['column_name'],
                                       excel_column_name=c['column_name']) for c in COLUMNS], **changes)


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
    assert result['deleted_count'] == result['imported_count'] == 1
    assert {r['id'] for r in state['store'].rows if r['row_data']['Voucher Detail'] == 'Keep sibling'} == {1, 3}
    assert validation['change_detection']['changed_voucher_count'] == 1
    with zipfile.ZipFile(next(iter(state['store'].jobs.values()))['archive_path']) as archive:
        assert len(json.loads(archive.read('rows.json'))) == 3
        from openpyxl import load_workbook
        report = load_workbook(io.BytesIO(archive.read('Upload observation report.xlsx')))
        assert report['Previous vouchers'].max_row == report['Incoming vouchers'].max_row == 8


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


def test_protected_evidence_blocks_transaction_and_keeps_verified_archive(pipeline):
    state = pipeline
    state['store'] = Store([row(description='Old')]); state['incoming'] = [row(description='New')]
    state['store'].protected = True
    before = deepcopy(state['store'].rows)
    validation = upload.validate_only(request())
    with pytest.raises(HTTPException) as error:
        upload.validate_and_import(request(validation_plan_id=validation['validation_plan_id']))
    assert error.value.status_code == 409 and state['store'].rows == before
    assert next(iter(state['store'].jobs.values()))['status'] == 'verified'


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
    assert result['imported_count'] == result['deleted_count'] == 1
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
    with pytest.raises(drive_archive.ArchiveError, match='not configured'):
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
    assert result['deleted_count'] == result['imported_count'] == 1


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
