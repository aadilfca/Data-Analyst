"""Manual local PostgreSQL snapshots; no source writes, row selection or restore API."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import random
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs
import zipfile

from psycopg2.extensions import parse_dsn

from services.drive_archive import ArchiveError, DriveArchive


_PROJECT = Path(__file__).resolve().parents[1]
_OPERATION = re.compile(r'^[0-9a-f]{32}$')
_DRIVE_SETTINGS = ('DRIVE_CLIENT_ID', 'DRIVE_CLIENT_SECRET', 'DRIVE_REFRESH_TOKEN', 'DRIVE_ARCHIVE_FOLDER_ID')
_MAX_DUMP_BYTES = 256 * 1024 * 1024  # Reviewed manual-backup size limit; transfer memory is chunk-bounded.
_TIMEOUT = 900
_DRIVE_CHUNK = 256 * 1024


class BackupError(Exception):
    def __init__(self, detail, *, status=503, backup=None, retry_same=False, code='backup_error'):
        super().__init__(detail)
        self.status, self.detail = status, detail
        self.backup, self.retry_same = backup, retry_same
        self.code = code


def local_runtime():
    # Explicitly fail closed on hosted deployments, even if a flag is set there.
    return platform.system() == 'Windows' and not os.environ.get('RENDER')


def _enabled():
    return local_runtime() and os.environ.get('LOCAL_DATABASE_BACKUP_ENABLED', '').lower() == 'true'


def _root(create=False):
    raw = os.environ.get('UPLOAD_ARCHIVE_DIR', '').strip()
    if not raw or not Path(raw).is_absolute():
        raise BackupError('A protected archive folder outside the application must be configured.')
    parent = Path(raw).expanduser().resolve()
    if parent == _PROJECT or _PROJECT in parent.parents:
        raise BackupError('The backup folder must be outside the application checkout.')
    root = parent / 'full-database-backups'
    if root.is_symlink() or root.resolve().parent != parent:
        raise BackupError('The backup folder location is unsafe.')
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _tools():
    directory = os.environ.get('DATABASE_BACKUP_BIN_DIR', '').strip()
    tools = []
    for name in ('pg_dump', 'pg_restore'):
        if directory:
            if not Path(directory).is_absolute():
                raise BackupError('Configure an absolute PostgreSQL tools folder.')
            path = Path(directory) / (name + ('.exe' if os.name == 'nt' else ''))
        else:
            found = shutil.which(name)
            path = Path(found) if found else None
        if path is None or not path.is_file():
            raise BackupError('PostgreSQL backup tools are not configured on this computer.')
        tools.append(path.resolve())
    if tools[0].parent != tools[1].parent:
        raise BackupError('Use pg_dump and pg_restore from the same PostgreSQL tools folder.')
    return tools


def _connection_env():
    raw = os.environ.get('DATABASE_BACKUP_URL') or os.environ.get('DATABASE_URL')
    try:
        params = parse_dsn(raw or '')
    except Exception:
        raise BackupError('A valid backup database connection must be configured.') from None
    if not all(params.get(key) for key in ('host', 'dbname', 'user')):
        raise BackupError('A complete backup database connection must be configured.')
    if params.get('port') == '6543' and params['host'].endswith('.pooler.supabase.com'):
        raise BackupError('Configure a direct or session-mode connection for database backups.')
    mapping = {'host': 'PGHOST', 'hostaddr': 'PGHOSTADDR', 'port': 'PGPORT', 'dbname': 'PGDATABASE',
               'user': 'PGUSER', 'password': 'PGPASSWORD', 'sslmode': 'PGSSLMODE',
               'sslrootcert': 'PGSSLROOTCERT', 'sslcert': 'PGSSLCERT', 'sslkey': 'PGSSLKEY',
               'sslcrl': 'PGSSLCRL', 'channel_binding': 'PGCHANNELBINDING',
               'connect_timeout': 'PGCONNECT_TIMEOUT', 'application_name': 'PGAPPNAME'}
    if set(params) - set(mapping):
        raise BackupError('The backup connection contains unsupported options; review it with the administrator.')
    local = params['host'] in ('127.0.0.1', 'localhost', '::1')
    if not local and params.get('sslmode', 'require') not in ('require', 'verify-ca', 'verify-full'):
        raise BackupError('Remote database backups require an encrypted database connection.')
    env = _process_env()
    env.update({mapping[key]: value for key, value in params.items()})
    env['PGSSLMODE'] = params.get('sslmode', 'prefer' if local else 'require')
    env['PGCONNECT_TIMEOUT'] = '15'
    env['PGAPPNAME'] = 'internal-audit-manual-backup'
    env['PGOPTIONS'] = '-c default_transaction_read_only=on -c statement_timeout=900000 -c lock_timeout=10000'
    return env


def _process_env():
    # Native tools need system runtime variables, not app/Drive credentials.
    secrets = {'DATABASE_URL', 'DATABASE_BACKUP_URL', *_DRIVE_SETTINGS}
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith('PG') and key.upper() not in secrets}


def configuration():
    requirements = []
    if not _enabled():
        requirements.append('Full database backup is not enabled on this computer.')
    for check in (_root, _tools, _connection_env):
        try:
            check()
        except BackupError as error:
            requirements.append(error.detail)
    if not all(os.environ.get(key, '').strip() for key in _DRIVE_SETTINGS):
        requirements.append('The app Google Drive archive connection must be configured.')
    return {'ready': not requirements, 'requirements': requirements}


@contextmanager
def _exclusive(root):
    # Same OS-held locking pattern as reconciliation exports; no stale-lock deletion.
    handle = (root / '.backup.lock').open('a+b')
    acquired = False
    try:
        if handle.tell() == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise BackupError('A database backup is already running. Check its status before retrying.', status=409, retry_same=True) from None
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _directory(root, operation):
    if not _OPERATION.fullmatch(operation):
        raise BackupError('Invalid backup reference.', status=400)
    directory = root / ('backup-' + operation)
    if directory.is_symlink() or directory.resolve().parent != root.resolve():
        raise BackupError('Unsafe backup location.', status=409)
    return directory


def _save(directory, receipt):
    descriptor, name = tempfile.mkstemp(prefix='.receipt-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(receipt, output, sort_keys=True)
            output.flush(); os.fsync(output.fileno())
        os.replace(name, directory / 'receipt.json')
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _read(directory, operation, owner):
    path = directory / 'receipt.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32768:
        raise BackupError('This incomplete backup cannot be reused. Create a new backup; existing files will be retained.', status=409)
    try:
        receipt = json.loads(path.read_text(encoding='utf-8'))
        if receipt['operation_id'] != operation or receipt['owner'] != str(owner):
            raise ValueError()
        filename = receipt['file_name']
        if not re.fullmatch(r'Database-\d{8}T\d{6}Z-' + operation + r'\.zip', filename):
            raise ValueError()
        if (directory / filename).is_symlink():
            raise ValueError()
        if receipt['state'] not in ('creating', 'incomplete', 'local_verified', 'complete'):
            raise ValueError()
        if receipt['state'] in ('local_verified', 'complete'):
            if not isinstance(receipt['bytes'], int) or receipt['bytes'] < 5 or not re.fullmatch(r'[0-9a-f]{64}', receipt['sha256']):
                raise ValueError()
        if receipt.get('drive_file_id') and not re.fullmatch(r'[A-Za-z0-9_-]+', receipt['drive_file_id']):
            raise ValueError()
        if receipt['state'] == 'complete' and not receipt.get('drive_file_id'):
            raise ValueError()
        return receipt
    except (KeyError, ValueError, TypeError):
        raise BackupError('Backup receipt is unavailable for this administrator.', status=404) from None


def _public(directory, receipt):
    return {key: receipt.get(key) for key in ('operation_id', 'created_at', 'verified_at', 'state', 'file_name', 'bytes', 'sha256', 'error', 'error_code')} | {
        'local_path': str(directory / receipt['file_name']),
        'drive_verified': receipt['state'] == 'complete',
        'drive_url': ('https://drive.google.com/file/d/' + receipt['drive_file_id'] + '/view') if receipt['state'] == 'complete' else None,
        'progress': _progress(directory, receipt),
    }


_STAGES = {
    'export': (0, 'Saving database data'),
    'native_check': (1, 'Checking the database file'),
    'package': (2, 'Preparing the dated backup ZIP'),
    'drive': (3, 'Copying to Drive and comparing both copies'),
    'complete': (4, 'Both backup copies verified'),
}


def _progress(directory, receipt):
    stage = receipt.get('stage', 'complete' if receipt['state'] == 'complete' else 'export')
    completed, label = _STAGES.get(stage, _STAGES['export'])
    size = receipt.get('bytes') or 0
    if stage in ('export', 'native_check'):
        partial = directory / 'database.partial'
        if partial.is_file(): size = partial.stat().st_size
    elif stage == 'package':
        target = directory / receipt['file_name']
        if target.is_file(): size = target.stat().st_size
    note = label
    if stage == 'drive':
        note = {'upload': 'Uploading small pieces to Drive; confirmed progress is saved',
                'verify': 'Downloading the Drive copy in pieces and checking its full checksum',
                'complete': 'Drive contents checked; confirming private access'}.get(receipt.get('drive_action'), label)
    log = directory / 'native-progress.tmp'
    if stage == 'export' and log.is_file() and not log.is_symlink():
        try:
            with log.open('rb') as source:
                source.seek(max(0, log.stat().st_size - 8192))
                tail = source.read(8192).decode('utf-8', errors='replace')
            # Only business labels derived from known tool messages reach the UI;
            # never expose SQL, connection details, arbitrary stderr or row values.
            tables = re.findall(r'dumping contents of table "([A-Za-z0-9_.]+)"', tail)
            if tables:
                note = 'Saving transaction rows' if tables[-1] == 'public.client_data' else 'Saving database setup and other records'
        except OSError:
            pass
    try:
        finish = datetime.fromisoformat(receipt['verified_at']) if receipt['state'] == 'complete' and receipt.get('verified_at') else datetime.now(timezone.utc)
        elapsed = max(0, int((finish - datetime.fromisoformat(receipt.get('attempt_started_at') or receipt['created_at'])).total_seconds()))
    except (KeyError, ValueError, TypeError):
        elapsed = 0
    return {'stage': stage, 'completed_steps': completed, 'total_steps': 4,
            'label': label, 'note': note, 'local_bytes': size, 'elapsed_seconds': elapsed,
            'transfer': {'action': receipt.get('drive_action'), 'uploaded_bytes': receipt.get('uploaded_bytes', 0),
                         'verified_bytes': receipt.get('verified_bytes', 0), 'total_bytes': receipt.get('bytes') or 0}}


def _error_chain(error):
    seen = set()
    while error and id(error) not in seen:
        seen.add(id(error)); yield error
        reason = error.reason if isinstance(error, URLError) and isinstance(error.reason, BaseException) else None
        error = reason or error.__cause__  # An old, handled 404 context is not the transfer cause.


def _http_reason(error):
    if hasattr(error, '_backup_reason'): return error._backup_reason
    reason = ''
    try:
        value = json.loads(error.read(8192)).get('error', {})
        if isinstance(value, str): reason = value
        elif value.get('errors'): reason = value['errors'][0].get('reason', '')
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        pass
    error._backup_reason = reason if isinstance(reason, str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', reason) else ''
    return error._backup_reason


def _transient_drive(error):
    http_error = next((item for item in _error_chain(error) if isinstance(item, HTTPError)), None)
    if http_error is not None:
        return http_error.code in (408, 429, 500, 502, 503, 504) or http_error.code == 403 and _http_reason(http_error) in ('rateLimitExceeded', 'userRateLimitExceeded')
    return any(isinstance(item, (URLError, OSError, http.client.HTTPException)) for item in _error_chain(error))


def _drive_call(drive, action, *, tries=3):
    """Bounded retries for reads/status probes; chunk writes use tries=1."""
    failures, refreshed = 0, False
    while True:
        try:
            return action()
        except HTTPError as error:
            if error.code == 401 and not refreshed:
                error.close(); drive.token = None; refreshed = True
                continue
            failures += 1
            if failures >= tries or not _transient_drive(error): raise
            error.close()
        except (ArchiveError, OSError, http.client.HTTPException) as error:
            failures += 1
            if failures >= tries or not _transient_drive(error): raise
        time.sleep(min(2 ** (failures - 1), 4) + random.random() / 4)


def _drive_failure(error, action):
    if isinstance(error, BackupError): return error
    error = next((item for item in _error_chain(error) if isinstance(item, HTTPError)), error)
    label = 'upload' if action == 'upload' else 'download/check' if action == 'verify' else 'connection'
    if isinstance(error, HTTPError):
        reason = _http_reason(error)
        detail = f'Google Drive rejected the {label} (HTTP {error.code}' + (f'; {reason}' if reason else '') + ').'
        code = f'drive_http_{error.code}' + ('_' + reason if reason else '')
        if reason == 'invalid_grant': detail = 'The app Google Drive authorization has expired or been revoked; reconnect its Drive account.'
        elif reason == 'storageQuotaExceeded': detail = 'Google Drive storage is full.'
        error.close()
    elif any(isinstance(item, TimeoutError) for item in _error_chain(error)):
        detail = f'The Drive {label} connection timed out.'
        code = 'drive_' + label.replace('/', '_') + '_timeout'
    else:
        detail = f'The Drive {label} connection was interrupted.'
        code = 'drive_connection_interrupted'
    return BackupError(detail + ' The local ZIP is safe. Retry the Drive copy to continue the same backup.', code=code, retry_same=True)


def _session_url(value):
    parsed = urlsplit(value or '')
    if parsed.scheme != 'https' or parsed.netloc != 'www.googleapis.com' or parsed.path != '/upload/drive/v3/files' or not parse_qs(parsed.query).get('upload_id'):
        raise BackupError('Drive returned an invalid upload-session location.', code='drive_session_invalid', retry_same=True)
    return value


def _upload_response(drive, url, data, headers, *, method='PUT', tries=1):
    def send():
        try:
            with drive._open(url, data=data, method=method, headers=headers) as response:
                return response.status, response.headers
        except HTTPError as error:
            if error.code == 308:
                acknowledged_headers = error.headers; error.close()
                return 308, acknowledged_headers
            raise
    return _drive_call(drive, send, tries=tries)


def _acknowledged(status_code, headers, total):
    if status_code in (200, 201): return total
    if status_code != 308:
        raise BackupError('Drive returned an unexpected upload response.', code='drive_protocol_error', retry_same=True)
    value = headers.get('Range')
    if not value: return 0
    match = re.fullmatch(r'bytes=0-(\d+)', value)
    if not match or not 0 < int(match[1]) + 1 <= total:
        raise BackupError('Drive returned an invalid upload position.', code='drive_position_invalid', retry_same=True)
    return int(match[1]) + 1


def _remote_matches(drive, receipt):
    try:
        metadata = _drive_call(drive, lambda: drive._metadata(receipt['drive_file_id']))
    except HTTPError as error:
        if error.code == 404:
            error.close(); return False
        raise
    try: remote_size = int(metadata.get('size', -1))
    except (ValueError, TypeError): remote_size = -1
    if (metadata.get('trashed') or drive.folder_id not in metadata.get('parents', [])
            or metadata.get('appProperties', {}).get('operation_id') != receipt['operation_id']
            or metadata.get('appProperties', {}).get('sha256') != receipt['sha256']
            or remote_size != receipt['bytes']):
        raise BackupError('The existing Drive file does not match this backup. It was not overwritten.', code='drive_identity_mismatch')
    return True


def _transfer_full_backup(drive, directory, receipt):
    """Backup-only resumable protocol. Shared transaction archive code is untouched.

    See Google Drive manage-uploads (308/Range/status recovery) and manage-downloads.
    Session URLs are private receipt fields and are never included in API responses.
    """
    total = receipt['bytes']; path = directory / receipt['file_name']
    receipt.update(drive_action='upload', verified_bytes=0)
    _save(directory, receipt)
    try:
        exists = _remote_matches(drive, receipt)
        if not exists:
            for restart in range(2):
                if not receipt.get('drive_upload_session'):
                    metadata = {'id': receipt['drive_file_id'], 'name': path.name, 'parents': [drive.folder_id],
                                'mimeType': 'application/zip', 'appProperties': {'operation_id': receipt['operation_id'], 'sha256': receipt['sha256']}}
                    try:
                        code, headers = _upload_response(drive,
                            'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true',
                            json.dumps(metadata).encode(), {'Content-Type': 'application/json',
                                'X-Upload-Content-Type': 'application/zip', 'X-Upload-Content-Length': str(total)}, method='POST', tries=3)
                    except HTTPError as error:
                        if error.code == 409 and _remote_matches(drive, receipt): break
                        raise
                    if code not in (200, 201):
                        raise BackupError('Drive did not start an upload session.', code='drive_session_failed', retry_same=True)
                    receipt['drive_upload_session'] = _session_url(headers.get('Location'))
                    receipt['uploaded_bytes'] = 0; _save(directory, receipt)
                session = _session_url(receipt['drive_upload_session'])
                try:
                    code, headers = _upload_response(drive, session, b'', {'Content-Length': '0', 'Content-Range': f'bytes */{total}'}, tries=3)
                    offset = _acknowledged(code, headers, total)
                    stalled = 0
                    with path.open('rb') as source:
                        while offset < total:
                            source.seek(offset); chunk = source.read(min(_DRIVE_CHUNK, total - offset))
                            if not chunk:
                                raise BackupError('The local ZIP changed during its transfer.', code='local_file_changed')
                            previous = offset; end = offset + len(chunk) - 1
                            try:
                                code, headers = _upload_response(drive, session, chunk, {'Content-Type': 'application/zip',
                                    'Content-Length': str(len(chunk)), 'Content-Range': f'bytes {offset}-{end}/{total}'})
                            except (ArchiveError, OSError, http.client.HTTPException) as error:
                                if not _transient_drive(error): raise
                                if stalled >= 2: raise
                                time.sleep(min(2 ** stalled, 4) + random.random() / 4)
                                # Never guess whether an uncertain write succeeded.
                                code, headers = _upload_response(drive, session, b'', {'Content-Length': '0', 'Content-Range': f'bytes */{total}'}, tries=3)
                            offset = _acknowledged(code, headers, total)
                            if offset > end + 1:
                                raise BackupError('Drive acknowledged bytes beyond the sent chunk.', code='drive_position_invalid', retry_same=True)
                            stalled = stalled + 1 if offset <= previous else 0
                            if stalled >= 3:
                                raise BackupError('Drive is not accepting the next upload chunk. Retry the saved backup later.', code='drive_upload_stalled', retry_same=True)
                            receipt['uploaded_bytes'] = offset; _save(directory, receipt)
                    receipt.pop('drive_upload_session', None)
                    _save(directory, receipt)
                    break
                except HTTPError as error:
                    if error.code not in (404, 410) or restart: raise
                    error.close()
                    # An expired session may have finished before its last response was lost.
                    if _remote_matches(drive, receipt): break
                    receipt.pop('drive_upload_session', None); receipt['uploaded_bytes'] = 0
                    _save(directory, receipt)
            if not _remote_matches(drive, receipt):
                raise BackupError('Drive has not committed the complete backup file.', code='drive_upload_uncommitted', retry_same=True)
        receipt.update(uploaded_bytes=total, drive_action='verify', verified_bytes=0)
        receipt.pop('drive_upload_session', None); _save(directory, receipt)
        checksum = hashlib.sha256()
        for offset in range(0, total, _DRIVE_CHUNK):
            end = min(total, offset + _DRIVE_CHUNK) - 1
            def read_range():
                with drive._open('https://www.googleapis.com/drive/v3/files/' + receipt['drive_file_id'] + '?alt=media&supportsAllDrives=true',
                                 headers={'Range': f'bytes={offset}-{end}', 'Accept-Encoding': 'identity'}) as response:
                    if response.status != 206 or response.headers.get('Content-Range') != f'bytes {offset}-{end}/{total}':
                        raise BackupError('Drive did not return the requested verification range.', code='drive_download_range_invalid', retry_same=True)
                    data = response.read(end - offset + 2)
                    if len(data) != end - offset + 1:
                        raise http.client.IncompleteRead(data)
                    return data
            checksum.update(_drive_call(drive, read_range))
            receipt['verified_bytes'] = end + 1; _save(directory, receipt)
        if checksum.hexdigest() != receipt['sha256']:
            raise BackupError('The downloaded Drive copy does not match the local ZIP. Keep the local copy and review before proceeding.', code='drive_checksum_mismatch')
        receipt['drive_action'] = 'complete'; _save(directory, receipt)
    except (ArchiveError, OSError, http.client.HTTPException) as error:
        raise _drive_failure(error, receipt.get('drive_action')) from None


def status(owner, operation=None):
    result = configuration()
    if operation:
        root = _root()
        directory = _directory(root, operation)
        if directory.exists():
            receipt = _read(directory, operation, owner)
            active = False
            if receipt['state'] != 'complete':
                try:
                    with _exclusive(root):
                        # A crashed process leaves its files intact but releases its
                        # OS lock. Report an interrupted attempt without deleting it.
                        if receipt['state'] == 'creating':
                            receipt = {**receipt, 'state': 'incomplete'}
                except BackupError as error:
                    if error.status != 409:
                        raise
                    active = True
            result['backup'] = {**_public(directory, receipt), 'running': active}
    return result


def _native_failure(raw):
    text = raw.decode('utf-8', errors='replace').casefold() if isinstance(raw, bytes) else str(raw).casefold()
    # Known error categories preserve useful evidence without storing server SQL
    # or potentially confidential values in logs/receipts.
    categories = [
        ('idle-in-transaction timeout', 'database_idle_timeout', 'PostgreSQL closed the backup connection because its transaction was idle too long.'),
        ('statement timeout', 'database_query_timeout', 'PostgreSQL stopped a backup query because it timed out.'),
        ('permission denied', 'database_permission', 'PostgreSQL denied read access to a required database object.'),
        ('server version mismatch', 'tool_version_mismatch', 'The PostgreSQL backup tool version does not support the source database version.'),
        ('ssl syscall error: eof detected', 'database_ssl_eof', 'The database connection ended unexpectedly during the backup (SSL EOF).'),
        ('ssl error', 'database_ssl_error', 'The database connection reported an SSL error during the backup.'),
        ('ssl connection', 'database_ssl_error', 'The SSL database connection was interrupted during the backup.'),
        ('server closed the connection', 'database_connection_lost', 'The database server closed the connection during the backup.'),
        ('connection to server was lost', 'database_connection_lost', 'The database connection was lost during the backup.'),
        ('could not receive data', 'database_connection_lost', 'The backup tool could not receive data from the database connection.'),
        ('no space left', 'local_disk_full', 'The local archive disk ran out of space.'),
    ]
    for signal, code, detail in categories:
        if signal in text: return BackupError(detail + ' Existing working data was not replaced.', code=code)
    return BackupError('The PostgreSQL backup tool could not complete. Check its version, connection and access; current database data was not changed.', code='native_tool_failed')


def _safe_native_diagnostic(raw, env):
    text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
    for key, value in sorted(env.items(), key=lambda pair: len(pair[1]), reverse=True):
        if value and (key in ('PGPASSWORD', 'PGSSLPASSWORD') or key in ('PGHOST', 'PGUSER', 'PGDATABASE') and len(value) > 3):
            text = text.replace(value, '[redacted]')
    text = re.sub(r'(?i)postgres(?:ql)?://\S+', '[connection redacted]', text)
    text = re.sub(r'(?i)password\s*=\s*\S+', 'password=[redacted]', text)
    # Source SQL can be very large; retain tool/server messages only, bounded.
    lines = [line for line in text.splitlines() if not re.search(r'(?i)(command was:|query was:|^\s*(COPY|SELECT|INSERT|UPDATE)\s)', line)]
    return '\n'.join(lines)[-4000:]


def _native(args, env, *, log_path=None):
    output = log_path.open('w+b') if log_path else tempfile.TemporaryFile()
    try:
        subprocess.run([str(arg) for arg in args], env=env, check=True, timeout=_TIMEOUT,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=output,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise BackupError('The backup tool timed out after 15 minutes. Current database data was not changed; create a new backup.', code='native_timeout') from None
    except subprocess.CalledProcessError as error:
        output.flush(); output.seek(0, 2); size = output.tell()
        output.seek(max(0, size - 16384))
        raw = output.read(16384) or error.stderr or b''
        if log_path:
            try:
                # This operation's bounded, credential-redacted diagnostic stays
                # in protected external storage, never an app/DB error log.
                with log_path.with_name('native-error.txt').open('x', encoding='utf-8') as diagnostic:
                    diagnostic.write(_safe_native_diagnostic(raw, env))
            except OSError:
                pass
        raise _native_failure(raw) from None
    except OSError:
        raise BackupError('The PostgreSQL backup tool could not be started on this computer.', code='native_start_failed') from None
    finally:
        output.close()
        if log_path:
            try: log_path.unlink()
            except OSError: pass


def _sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def create_backup(operation, owner):
    config = configuration()
    if not config['ready']:
        raise BackupError(' '.join(config['requirements']))
    root = _root(create=True)
    directory = _directory(root, operation)
    with _exclusive(root):
        retrying = directory.exists()
        if directory.exists():
            receipt = _read(directory, operation, owner)
            if receipt['state'] not in ('local_verified', 'complete'):
                raise BackupError('This backup did not finish creating its local file. Create a new backup; existing files are retained.', status=409)
        else:
            if shutil.disk_usage(root).free < 1024 * 1024 * 1024:
                raise BackupError('At least 1 GiB of free local archive space is required before starting a backup.')
            now = datetime.now(timezone.utc)
            receipt = {'version': 1, 'operation_id': operation, 'owner': str(owner),
                       'created_at': now.isoformat(), 'attempt_started_at': now.isoformat(), 'state': 'creating', 'stage': 'export',
                       'file_name': 'Database-' + now.strftime('%Y%m%dT%H%M%SZ') + '-' + operation + '.zip'}
            if os.name == 'nt' and len(str(directory / receipt['file_name'])) >= 240:
                raise BackupError('Use a shorter protected archive folder path, such as D:/Audit Archives, before starting a full backup.')
            directory.mkdir(mode=0o700)  # Unique operation directory; never reuse an old output.
            _save(directory, receipt)
            dump, restore = _tools()
            partial = directory / 'database.partial'
            try:
                # No --table/schema filters, no row matching or transformations; pg_dump
                # takes its own consistent read-only database snapshot. Secrets stay in env.
                _native([dump, '--format=custom', '--verbose', '--no-password', '--lock-wait-timeout=10000', '--file', partial], _connection_env(), log_path=directory / 'native-progress.tmp')
                if not partial.is_file() or partial.stat().st_size < 5:
                    raise BackupError('The backup tool did not produce a database archive.')
                with partial.open('rb') as source:
                    if source.read(5) != b'PGDMP':
                        raise BackupError('The backup file format is invalid.')
                # Read/decompress the entire archive into the null device, never into a DB.
                receipt['stage'] = 'native_check'; _save(directory, receipt)
                clean_env = _process_env()
                _native([restore, '--file', os.devnull, partial], clean_env, log_path=directory / 'native-progress.tmp')
                receipt['stage'] = 'package'; _save(directory, receipt)
                target = directory / receipt['file_name']
                manifest = {'format': 'postgresql-custom', 'version': 1,
                            'operation_id': operation, 'created_at': receipt['created_at'],
                            'dump_file': 'database.dump', 'dump_bytes': partial.stat().st_size,
                            'dump_sha256': _sha256(partial),
                            'not_included': ['server-global roles', 'source workbooks', 'Storage object bytes', 'environment credentials']}
                # Exclusive creation protects prior files on every filesystem, including FAT.
                # The existing Drive transport stores ZIPs. Wrap the already-compressed
                # native dump without recompressing or interpreting any transaction row.
                with target.open('xb') as output:
                    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
                        archive.write(partial, 'database.dump')
                        archive.writestr('manifest.json', json.dumps(manifest, sort_keys=True))
                    output.flush(); os.fsync(output.fileno())
                with zipfile.ZipFile(target) as archive:
                    restored_hash = hashlib.sha256()
                    with archive.open('database.dump') as source:
                        while chunk := source.read(1024 * 1024):
                            restored_hash.update(chunk)
                    if restored_hash.hexdigest() != manifest['dump_sha256'] or json.loads(archive.read('manifest.json')) != manifest:
                        raise BackupError('The local backup package could not be verified.')
                receipt.update(state='local_verified', bytes=target.stat().st_size, sha256=_sha256(target))
                _save(directory, receipt)
            except Exception as error:
                receipt['state'] = 'incomplete'
                receipt['error'] = error.detail if isinstance(error, BackupError) else 'The local backup file could not be completed. Existing data and files were retained.'
                receipt['error_code'] = error.code if isinstance(error, BackupError) else 'local_file_failed'
                _save(directory, receipt)
                if isinstance(error, BackupError): error.backup = _public(directory, receipt)
                raise
            try:
                partial.unlink()  # Only this operation's successfully published temporary file.
            except OSError:
                pass  # A valid full copy remains usable even if temporary cleanup fails.
        target = directory / receipt['file_name']
        if not target.is_file() or target.stat().st_size != receipt['bytes'] or _sha256(target) != receipt['sha256']:
            raise BackupError('The local backup has changed. Existing files were retained; create a new backup.', status=409)
        if receipt['state'] == 'complete':
            return _public(directory, receipt)
        if receipt['bytes'] > _MAX_DUMP_BYTES:
            raise BackupError('The full local backup exceeds the safe Drive transfer limit (256 MiB). No rows were omitted; the local file is retained for administrator review.', backup=_public(directory, receipt))
        try:
            receipt['stage'] = 'drive'
            if retrying: receipt['attempt_started_at'] = datetime.now(timezone.utc).isoformat()
            receipt.pop('error', None); receipt.pop('error_code', None)
            _save(directory, receipt)
            drive = DriveArchive()
            # Full snapshots can contain user/security tables. Only an unshared
            # My Drive folder/file is accepted; this action never edits sharing.
            folder = _drive_call(drive, lambda: drive._json('files/' + drive.folder_id + '?fields=shared,driveId&supportsAllDrives=true'))
            if folder.get('shared') is not False or folder.get('driveId'):
                raise BackupError('The local backup is saved. Full database backups require a private, unshared My Drive folder; review the folder access before retrying.', backup=_public(directory, receipt), retry_same=True, code='drive_folder_access')
            if not receipt.get('drive_file_id'):
                receipt['drive_file_id'] = _drive_call(drive, drive.allocate_id)
                _save(directory, receipt)
            _transfer_full_backup(drive, directory, receipt)
            remote = _drive_call(drive, lambda: drive._json('files/' + receipt['drive_file_id'] + '?fields=shared,driveId&supportsAllDrives=true'))
            if remote.get('shared') is not False or remote.get('driveId'):
                raise BackupError('The local backup is saved, but Drive file access needs review before completion. Keep the backup private, then retry.', backup=_public(directory, receipt), retry_same=True, code='drive_file_access')
        except BackupError as error:
            receipt.update(error=error.detail, error_code=error.code)
            _save(directory, receipt)
            error.backup = _public(directory, receipt)
            raise
        except (ArchiveError, OSError, http.client.HTTPException) as error:
            failure = _drive_failure(error, receipt.get('drive_action'))
            receipt.update(error=failure.detail, error_code=failure.code)
            _save(directory, receipt)
            failure.backup = _public(directory, receipt)
            raise failure from None
        receipt.update(state='complete', stage='complete', verified_at=datetime.now(timezone.utc).isoformat())
        _save(directory, receipt)
        return _public(directory, receipt)
