"""Exact upload coverage, recoverable archives and compact commit receipts."""

from collections import defaultdict, deque
from datetime import date
import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
import zipfile

from fastapi import HTTPException
from database import execute_query_pg
from services.drive_archive import ArchiveError, DriveArchive, archive_directory
from services.upload_observation_report import build_observation_report


SOURCE_ROW_HASH_SQL = "encode(sha256(convert_to(jsonb_build_array(cd.id, COALESCE(cd.version, 1), cd.row_data, cd.extract_date, cd.source_identity)::text, 'UTF8')), 'hex')"
UPLOAD_BACKUP_SETTING = 'development_allow_unbacked_uploads'


def drive_archiving_enabled():
    """Drive remains the default; local development can opt into verified ZIPs only."""
    value = os.environ.get('UPLOAD_ARCHIVE_TO_DRIVE', 'true').strip().casefold()
    return value not in {'0', 'false', 'no', 'off', 'disabled'}


def unbacked_uploads_allowed():
    """Read the administrator's shared choice without a stale local cache."""
    rows = execute_query_pg('SELECT value FROM app_settings WHERE key = %s', (UPLOAD_BACKUP_SETTING,))
    return bool(rows and rows[0].get('value') == 'true')


def check_backup_policy(request):
    if getattr(request, 'backup_enabled', None) is False and not unbacked_uploads_allowed():
        raise HTTPException(403, 'This server requires upload backups. Turn backup on and validate again.')


def unbacked_receipt_matches(prepared, receipt):
    """Never disguise an intentionally unbacked import as a verified archive."""
    scope = receipt.get('scope_json') or {}
    if isinstance(scope, str):
        scope = json.loads(scope)
    return (prepared.get('archive_mode') == 'none' and unbacked_uploads_allowed()
            and receipt.get('status') == 'unbacked'
            and scope.get('backup_mode') == 'none'
            and receipt.get('archive_state_hash') == prepared.get('archive_state_hash')
            and not receipt.get('drive_file_id') and not receipt.get('archive_sha256'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def declared_coverage(start, end):
    try:
        if not start or not end or date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end or start > end:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, 'Select the complete report coverage start/end as YYYY-MM-DD; coverage is never inferred from surviving transactions.')
    return {'start': start, 'end': end}


def scope_sql(scope):
    sql = 'cd.client_id = %s AND cd.template_id = %s AND cd.is_deleted = FALSE'
    params = [scope['client_id'], scope['template_id']]
    if scope.get('start'):
        sql += ' AND (cd.extract_date BETWEEN %s::date AND %s::date'
        params.extend([scope['start'], scope['end']])
        if scope.get('document_groups'):
            sql += ' OR cd.document_group_id = ANY(%s)'
            params.append(scope['document_groups'])
        sql += ')'
    if scope.get('account_code'):
        sql += ' AND EXISTS (SELECT 1 FROM uploaded_files lu WHERE lu.id = cd.upload_id AND lu.ledger_account_code = %s)'
        params.append(scope['account_code'])
    return sql, params


def source_fingerprint(rows):
    if rows and all('scope_row_hash' in row for row in rows):
        return hashlib.sha256(''.join(r['scope_row_hash'] for r in sorted(rows, key=lambda r: r['id'])).encode()).hexdigest()
    def source(row):
        raw = row.get('row_data')
        if isinstance(raw, str):
            raw = json.loads(raw)
        return [row['id'], row.get('version', 1), raw, str(row.get('extract_date') or '')[:10], row.get('source_identity')]
    return digest([source(r) for r in sorted(rows, key=lambda r: r['id'])])


def match_unchanged(incoming, existing, exact, equivalent):
    """Consume occurrences once, reserving all exact matches before equivalents."""
    available = set(range(len(existing)))
    unmatched = list(incoming)
    matched = []
    for key in (exact, equivalent):
        buckets = defaultdict(deque)
        for index in sorted(available):
            buckets[key(existing[index], True)].append(index)
        remaining = []
        for record in unmatched:
            bucket = buckets[key(record, False)]
            if bucket:
                index = bucket.popleft()
                available.remove(index)
                matched.append(existing[index])
            else:
                remaining.append(record)
        unmatched = remaining
    return matched, unmatched, [existing[i] for i in sorted(available)]


def get_receipt(request, query, signature):
    rows = query('SELECT status, request_signature, result_json FROM upload_lifecycle_runs WHERE operation_id = %s AND client_id = %s AND template_id = %s AND file_id = %s',
                 (request.validation_plan_id, request.client_id, request.template_id, request.file_id))
    if rows and rows[0]['status'] == 'applied':
        if rows[0]['request_signature'] != signature:
            raise HTTPException(409, 'The completed import belongs to different upload settings.')
        result = rows[0]['result_json']
        result = json.loads(result) if isinstance(result, str) else result
        return {**result, 'recovered_commit': True}


def check_scope(lifecycle, query):
    where, params = scope_sql(lifecycle['scope'])
    if lifecycle.get('fingerprint_method') == 'postgres':
        rows = query("SELECT encode(sha256(convert_to(COALESCE(string_agg(" + SOURCE_ROW_HASH_SQL
                     + ", '' ORDER BY cd.id), ''), 'UTF8')), 'hex') AS scope_fingerprint FROM client_data cd WHERE " + where, tuple(params))
        if not rows or rows[0]['scope_fingerprint'] != lifecycle['source_fingerprint']:
            raise HTTPException(409, 'Data inside the selected coverage changed after validation. Validate again; no replacement was applied.')
        return
    rows = query('SELECT cd.id, cd.version, cd.row_data, cd.extract_date, cd.source_identity FROM client_data cd WHERE ' + where + ' ORDER BY cd.id', tuple(params))
    if source_fingerprint(rows) != lifecycle['source_fingerprint']:
        raise HTTPException(409, 'Data inside the selected coverage changed after validation. Validate again; no replacement was applied.')


def _archive_state(ids, query):
    # Include original derived fields and compact row audit events for recovery.
    rows = query('SELECT cd.* FROM client_data cd WHERE cd.id = ANY(%s) ORDER BY cd.id', (ids,))
    audits = query('SELECT * FROM data_audit_log WHERE client_data_id = ANY(%s) ORDER BY id', (ids,))
    return {'rows': rows, 'audit_events': audits}


def prepare_lifecycle(request, payload, query, write, transaction, signature):
    check_backup_policy(request)
    lifecycle = payload.get('lifecycle')
    if not lifecycle:
        if payload.get('changed') or payload.get('missing'):
            raise HTTPException(409, 'This older validation plan has no archive safeguards. Validate the file again.')
        return None
    check_scope(lifecycle, query)
    operation = request.validation_plan_id
    if not re.fullmatch(r'[a-f0-9]{32}', operation or ''):
        raise HTTPException(409, 'Invalid upload operation. Validate the file again.')
    if payload.get('comparison_mode') == 'replace_range' and 'voucher_observations' not in payload:
        raise HTTPException(409, 'Validate again to prepare the complete-voucher observation report.')
    lifecycle = dict(lifecycle)
    if getattr(request, 'backup_enabled', None) is False:
        lifecycle['backup_mode'] = 'none'
    with transaction():
        write('''INSERT INTO upload_lifecycle_runs (operation_id, file_id, client_id, template_id, request_signature, status, scope_json)
                 VALUES (%s, %s, %s, %s, %s, 'archiving', %s) ON CONFLICT (operation_id) DO NOTHING''',
              (operation, request.file_id, request.client_id, request.template_id, signature, json.dumps(lifecycle, sort_keys=True)))
    ids = sorted({item['client_data_id'] for kind in ('changed', 'missing') for item in payload.get(kind, [])})
    ids = sorted(set(ids) | {key for observation in payload.get('voucher_observations', []) for key in observation['old_ids']})
    prepared = {'operation_id': operation, 'ids': ids, 'lifecycle': lifecycle}
    if not ids:
        return prepared
    state = _archive_state(ids, query)
    if {r['id'] for r in state['rows']} != set(ids):
        raise HTTPException(409, 'Source records changed before archiving; validate again.')
    uploads = sorted({r['upload_id'] for r in state['rows'] if r.get('upload_id') is not None})
    state['uploads'] = query('SELECT * FROM uploaded_files WHERE id = ANY(%s) ORDER BY id', (uploads,)) if uploads else []
    prepared['archive_state_hash'] = digest({'rows': state['rows'], 'audit_events': state['audit_events']})
    if getattr(request, 'backup_enabled', None) is False:
        # Only a fingerprint and explicit receipt survive; no ZIP or Drive I/O.
        with transaction():
            write("UPDATE upload_lifecycle_runs SET status = 'unbacked', archive_state_hash = %s WHERE operation_id = %s AND status <> 'applied'",
                  (prepared['archive_state_hash'], operation))
        prepared['archive_mode'] = 'none'
        return prepared
    state['manifest'] = {'format': 1, 'operation_id': operation, 'scope': lifecycle['scope'],
                         'source_fingerprint': lifecycle['source_fingerprint'],
                         'archive_state_hash': prepared['archive_state_hash'], 'template_columns': payload.get('template_columns', [])}
    observations = payload.get('voucher_observations', [])
    if observations:
        state['manifest']['format'] = 2
        state['observations'] = observations
    temporary_archive = None
    try:
        # Render Free uses scratch space only while creating the verified Drive
        # copy. Local development retains its configured additional ZIP copy.
        if (getattr(request, 'backup_enabled', None) is True
                and os.environ.get('RENDER') == 'true'
                and not os.environ.get('UPLOAD_ARCHIVE_DIR', '').strip()):
            temporary_archive = tempfile.TemporaryDirectory(prefix='audit-upload-archive-')
            root = Path(temporary_archive.name)
        else:
            root = archive_directory()
        scope = lifecycle['scope']
        filename = f"Client-{request.client_id}_Template-{request.template_id}_{scope.get('start') or 'corrections'}_{scope.get('end') or 'current'}_{operation}.zip"
        path = root / filename
        descriptor, temporary = tempfile.mkstemp(dir=root, suffix='.zip')
        try:
            with os.fdopen(descriptor, 'wb') as output, zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                if observations:
                    info = zipfile.ZipInfo('Upload observation report.xlsx', date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, build_observation_report(observations, state['rows'],
                        payload.get('template_columns', []), lifecycle['scope'], payload.get('archive_labels')))
                for name, value in sorted(state.items()):
                    info = zipfile.ZipInfo(name + '.json', date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode())
            checksum = hashlib.sha256(Path(temporary).read_bytes()).hexdigest()
            if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise ArchiveError('Existing recovery archive differs from current source data; validate again.')
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        if getattr(request, 'backup_enabled', None) is not True and not drive_archiving_enabled():
            proof = {'drive_file_id': None, 'sha256': checksum, 'local_path': str(path)}
            with transaction():
                write("""UPDATE upload_lifecycle_runs
                          SET status = 'verified', drive_file_id = NULL, archive_sha256 = %s,
                              archive_path = %s, archive_state_hash = %s
                        WHERE operation_id = %s AND status <> 'applied'""",
                      (checksum, str(path), prepared['archive_state_hash'], operation))
            prepared['archive'] = proof
            prepared['archive_mode'] = 'local'
            return prepared
        client = DriveArchive()
        saved = query('SELECT drive_file_id FROM upload_lifecycle_runs WHERE operation_id = %s', (operation,))
        remote_id = saved[0].get('drive_file_id') if saved else None
        if not remote_id:
            allocated = client.allocate_id()
            with transaction():
                write('UPDATE upload_lifecycle_runs SET drive_file_id = %s WHERE operation_id = %s AND drive_file_id IS NULL', (allocated, operation))
            remote_id = query('SELECT drive_file_id FROM upload_lifecycle_runs WHERE operation_id = %s', (operation,))[0]['drive_file_id']
        proof = client.upload_and_verify(remote_id, path, operation, checksum)
        with transaction():
            write("UPDATE upload_lifecycle_runs SET status = 'verified', archive_sha256 = %s, archive_path = %s, archive_state_hash = %s WHERE operation_id = %s AND status <> 'applied'",
                  (checksum, str(path), prepared['archive_state_hash'], operation))
        prepared['archive'] = proof
        return prepared
    except (ArchiveError, OSError) as error:
        raise HTTPException(503, str(error) if isinstance(error, ArchiveError) else 'External archive storage is unavailable. Current source data was not replaced.') from error
    finally:
        if temporary_archive is not None:
            temporary_archive.cleanup()


def guard_lifecycle(prepared, query):
    if not prepared:
        return
    # Briefly exclude all source writers, including legacy mutation paths. No
    # Drive I/O occurs while these locks are held; normal report reads continue.
    query('LOCK TABLE client_data IN SHARE ROW EXCLUSIVE MODE')
    query('LOCK TABLE data_audit_log IN SHARE ROW EXCLUSIVE MODE')
    rows = query(
        'SELECT status, archive_sha256, archive_path, drive_file_id, archive_state_hash, scope_json '
        'FROM upload_lifecycle_runs WHERE operation_id = %s FOR UPDATE',
        (prepared['operation_id'],),
    )
    if rows and rows[0]['status'] == 'applied':
        raise HTTPException(409, 'This import already completed. Retry to retrieve its saved result.')
    check_scope(prepared['lifecycle'], query)
    if prepared['ids']:
        if prepared.get('archive_mode') == 'local':
            proof = prepared.get('archive') or {}
            try:
                local_checksum = hashlib.sha256(Path(proof.get('local_path', '')).read_bytes()).hexdigest()
            except (OSError, TypeError, ValueError):
                local_checksum = None
            receipt = rows[0] if rows else {}
            if (local_checksum != proof.get('sha256')
                    or local_checksum != receipt.get('archive_sha256')
                    or str(proof.get('local_path') or '') != str(receipt.get('archive_path') or '')
                    or receipt.get('drive_file_id') is not None):
                raise HTTPException(409, 'The protected local recovery archive is missing or changed; validate again.')
        state = _archive_state(prepared['ids'], query)
        if digest(state) != prepared['archive_state_hash']:
            raise HTTPException(409, 'Archived source or audit data changed. Validate again before replacing it.')
        if prepared.get('archive_mode') == 'none':
            if not rows or not unbacked_receipt_matches(prepared, rows[0]):
                raise HTTPException(409, 'Development backup permission changed; validate again with backups enabled.')
        elif not rows or rows[0]['status'] != 'verified':
            raise HTTPException(409, 'A verified archive is required before replacement.')


def finish_lifecycle(prepared, result, write):
    if prepared:
        compact = {key: result[key] for key in ('status', 'message', 'total_rows', 'imported_count', 'updated_count', 'deleted_count')}
        compact.update({key: result[key] for key in ('backup_mode', 'archive_verified') if key in result})
        write("UPDATE upload_lifecycle_runs SET status = 'applied', result_json = %s WHERE operation_id = %s",
              (json.dumps(compact), prepared['operation_id']))
