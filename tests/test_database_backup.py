"""Full-backup safety tests. App DB pools and real Drive are never used."""
import json
import io
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import zipfile
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import database
from routers import database_backup as api
from routers.auth import get_current_user
from services import database_backup as backup
from services.drive_archive import ArchiveError


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail('App database pool must never be used by backup tests')
    monkeypatch.setattr(database, 'get_pg_pool', forbidden)
    monkeypatch.setattr(backup.platform, 'system', lambda: 'Windows')
    monkeypatch.delenv('RENDER', raising=False)
    monkeypatch.setenv('LOCAL_DATABASE_BACKUP_ENABLED', 'true')
    monkeypatch.setenv('UPLOAD_ARCHIVE_DIR', str(tmp_path / 'protected'))
    monkeypatch.setenv('DATABASE_URL', 'postgresql://fixture:fixture-password@127.0.0.1:55439/backup_fixture?sslmode=disable')
    monkeypatch.delenv('DATABASE_BACKUP_URL', raising=False)
    for name in backup._DRIVE_SETTINGS:
        monkeypatch.setenv(name, 'fixture-' + name.lower())
    tools = tmp_path / 'tools'
    tools.mkdir()
    for name in ('pg_dump', 'pg_restore'):
        (tools / (name + ('.exe' if os.name == 'nt' else ''))).write_bytes(b'fixture tool only')
    monkeypatch.setenv('DATABASE_BACKUP_BIN_DIR', str(tools))
    monkeypatch.setattr(backup.shutil, 'disk_usage', lambda path: SimpleNamespace(free=2 * 1024**3))
    state = SimpleNamespace(calls=[], uploads=[], allocations=0, fail_drive=False, native_failure=None,
                            folder_shared=False, file_shared=False,
                            content=b'PGDMP' + b'same-voucher\t123.4500\n' * 685)

    def native(args, env, **kwargs):
        state.calls.append((list(map(str, args)), dict(env)))
        if state.native_failure:
            raise backup.BackupError(state.native_failure)
        if Path(args[0]).stem == 'pg_dump':
            Path(args[args.index('--file') + 1]).write_bytes(state.content)
    monkeypatch.setattr(backup, '_native', native)

    class FakeDrive:
        folder_id = 'fixture-folder'

        def _json(self, path):
            return {'shared': state.folder_shared if path.startswith('files/fixture-folder?') else state.file_shared}

        def allocate_id(self):
            state.allocations += 1
            return 'fixture-remote-' + str(state.allocations)

        def upload_and_verify(self, remote, path, operation, sha):
            assert backup._sha256(path) == sha
            state.uploads.append((remote, operation, path.read_bytes()))
            if state.fail_drive:
                raise ArchiveError('fixture failure')
    monkeypatch.setattr(backup, 'DriveArchive', FakeDrive)
    state.real_transfer = backup._transfer_full_backup
    def transfer(drive, directory, receipt):
        drive.upload_and_verify(receipt['drive_file_id'], directory / receipt['file_name'], receipt['operation_id'], receipt['sha256'])
    monkeypatch.setattr(backup, '_transfer_full_backup', transfer)
    state.sleeps = []
    monkeypatch.setattr(backup.time, 'sleep', lambda delay: state.sleeps.append(delay))
    monkeypatch.setattr(backup.random, 'random', lambda: 0)
    return state


class _Response:
    def __init__(self, status, headers=None, data=b''):
        self.status, self.headers, self.body = status, headers or {}, io.BytesIO(data)
    def __enter__(self): return self
    def __exit__(self, *args): self.body.close()
    def read(self, length=-1): return self.body.read(length)


class _DriveServer:
    """In-memory Drive protocol, including urllib's HTTPError for 308."""
    folder_id = 'fixture-folder'
    token = 'fixture-token'
    def __init__(self, receipt, content):
        self.receipt, self.content = receipt, content
        self.received = bytearray(); self.exists = False
        self.session = 'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=private-fixture-session'
        self.starts = []; self.ranges = []; self.initiations = 0; self.probes = 0
        self.lose_ack = False; self.lose_final_ack = False; self.fail_writes = 0; self.expire = False
        self.corrupt_download = False; self.bad_range = False; self.bad_ack = False
        self.reject = None; self.unauthorized = False; self.bad_metadata = False; self.download_retry = False

    def _metadata(self, file_id):
        if not self.exists: raise HTTPError('https://www.googleapis.com/file', 404, 'missing', {}, io.BytesIO())
        return {'size': str(len(self.content)), 'parents': [self.folder_id], 'trashed': False,
                'appProperties': {'operation_id': self.receipt['operation_id'], 'sha256': 'bad' if self.bad_metadata else self.receipt['sha256']}}

    def _json(self, path):
        return {'shared': False}

    def _open(self, url, data=None, method=None, headers=None):
        headers = headers or {}
        if method == 'POST':
            self.initiations += 1
            self.received.clear()
            assert json.loads(data)['id'] == self.receipt['drive_file_id']
            return _Response(200, {'Location': self.session})
        if method == 'PUT':
            assert url == self.session
            if data == b'':
                self.probes += 1
                if self.expire:
                    self.expire = False
                    raise HTTPError(url, 404, 'expired', {}, io.BytesIO())
                if self.exists: return _Response(200)
                ack = {'Range': f'bytes=0-{len(self.received)-1}'} if self.received else {}
                raise HTTPError(url, 308, 'Resume Incomplete', ack, io.BytesIO())
            if self.reject:
                status, reason = self.reject
                raise HTTPError(url, status, 'fixture rejection', {}, io.BytesIO(json.dumps({'error': {'errors': [{'reason': reason}]}}).encode()))
            if self.unauthorized:
                self.unauthorized = False
                raise HTTPError(url, 401, 'expired access token', {}, io.BytesIO())
            start = int(headers['Content-Range'].split(' ')[1].split('-')[0]); self.starts.append(start)
            assert start == len(self.received) and len(data) <= backup._DRIVE_CHUNK
            if self.fail_writes:
                self.fail_writes -= 1
                raise URLError(TimeoutError('The write operation timed out'))
            self.received.extend(data)
            self.exists = len(self.received) == len(self.content)
            if self.lose_ack or self.lose_final_ack and self.exists:
                self.lose_ack = False; self.lose_final_ack = False
                raise URLError(TimeoutError('The write operation timed out'))
            if self.bad_ack: return _Response(308, {'Range': f'bytes=0-{len(self.content)-1}'})
            if self.exists: return _Response(200)
            raise HTTPError(url, 308, 'Resume Incomplete', {'Range': f'bytes=0-{len(self.received)-1}'}, io.BytesIO())
        assert 'alt=media' in url
        start, end = map(int, headers['Range'].removeprefix('bytes=').split('-'))
        self.ranges.append((start, end))
        if self.download_retry:
            self.download_retry = False
            raise URLError(TimeoutError('The read operation timed out'))
        value = self.content[start:end+1]
        if self.corrupt_download and start == 0: value = bytes([value[0] ^ 1]) + value[1:]
        return _Response(206, {'Content-Range': 'invalid' if self.bad_range else f'bytes {start}-{end}/{len(self.content)}'}, value)


def _transfer_fixture(isolated):
    from datetime import datetime, timezone
    root = backup._root(create=True); directory = backup._directory(root, 'd' * 32); directory.mkdir()
    content = bytes(range(256)) * 2200 + b'final bytes'
    filename = 'Database-20261025T120000Z-' + 'd' * 32 + '.zip'
    path = directory / filename; path.write_bytes(content)
    receipt = {'version': 1, 'owner': '7', 'operation_id': 'd' * 32, 'file_name': filename,
               'created_at': datetime.now(timezone.utc).isoformat(), 'state': 'local_verified', 'stage': 'drive',
               'bytes': len(content), 'sha256': backup._sha256(path), 'drive_file_id': 'fixed-backup-file'}
    backup._save(directory, receipt)
    return directory, receipt, _DriveServer(receipt, content)


def test_resumable_upload_and_download_are_bounded_and_exact(isolated):
    directory, receipt, drive = _transfer_fixture(isolated)
    isolated.real_transfer(drive, directory, receipt)
    assert bytes(drive.received) == drive.content and drive.initiations == 1
    assert drive.starts == [0, backup._DRIVE_CHUNK, 2 * backup._DRIVE_CHUNK]
    assert len(drive.ranges) == 3 and receipt['verified_bytes'] == receipt['bytes']
    assert all(end - start + 1 <= backup._DRIVE_CHUNK for start, end in drive.ranges)
    assert not isolated.calls and not isolated.uploads  # No dump or multipart sender.
    assert 'drive_upload_session' not in receipt


def test_lost_chunk_ack_queries_server_before_continuing(isolated):
    directory, receipt, drive = _transfer_fixture(isolated); drive.lose_ack = True
    isolated.real_transfer(drive, directory, receipt)
    assert drive.probes >= 2
    assert drive.starts == [0, backup._DRIVE_CHUNK, 2 * backup._DRIVE_CHUNK]
    assert bytes(drive.received) == drive.content


def test_interrupted_process_resumes_existing_session_without_new_file(isolated):
    directory, receipt, drive = _transfer_fixture(isolated)
    drive.received.extend(drive.content[:backup._DRIVE_CHUNK])
    receipt.update(drive_upload_session=drive.session, uploaded_bytes=backup._DRIVE_CHUNK)
    public = backup._public(directory, receipt)
    assert 'upload_id' not in json.dumps(public) and drive.session not in json.dumps(public)
    isolated.real_transfer(drive, directory, receipt)
    assert drive.initiations == 0 and drive.starts[0] == backup._DRIVE_CHUNK


def test_lost_final_upload_ack_probes_completion_without_duplicate_file(isolated):
    directory, receipt, drive = _transfer_fixture(isolated); drive.lose_final_ack = True
    isolated.real_transfer(drive, directory, receipt)
    assert drive.initiations == 1 and len(drive.starts) == 3
    assert receipt['verified_bytes'] == receipt['bytes'] and drive.probes == 2


def test_expired_session_restarts_with_same_reserved_file_id(isolated):
    directory, receipt, drive = _transfer_fixture(isolated)
    drive.received.extend(drive.content[:backup._DRIVE_CHUNK])
    receipt['drive_upload_session'] = drive.session; drive.expire = True
    isolated.real_transfer(drive, directory, receipt)
    assert drive.initiations == 1 and receipt['drive_file_id'] == 'fixed-backup-file'


def test_repeated_write_timeout_stops_with_checkpoint_and_precise_error(isolated):
    directory, receipt, drive = _transfer_fixture(isolated); drive.fail_writes = 10
    with pytest.raises(backup.BackupError) as caught:
        isolated.real_transfer(drive, directory, receipt)
    assert caught.value.code == 'drive_upload_timeout'
    assert caught.value.retry_same and len(drive.starts) == 3
    assert receipt['drive_upload_session'] == drive.session
    assert (directory / receipt['file_name']).read_bytes() == drive.content


def test_existing_remote_copy_only_downloads_and_verifies(isolated):
    directory, receipt, drive = _transfer_fixture(isolated); drive.exists = True
    isolated.real_transfer(drive, directory, receipt)
    assert drive.initiations == 0 and not drive.starts and len(drive.ranges) == 3


@pytest.mark.parametrize('defect', ['bad_range', 'corrupt_download', 'bad_metadata', 'bad_ack'])
def test_transfer_rejects_wrong_ranges_data_identity_or_ack(isolated, defect):
    directory, receipt, drive = _transfer_fixture(isolated)
    setattr(drive, defect, True)
    if defect == 'bad_metadata': drive.exists = True
    with pytest.raises(backup.BackupError): isolated.real_transfer(drive, directory, receipt)
    assert receipt['state'] == 'local_verified'


def test_download_retry_does_not_hash_a_range_twice(isolated):
    directory, receipt, drive = _transfer_fixture(isolated)
    drive.exists = True; drive.download_retry = True
    isolated.real_transfer(drive, directory, receipt)
    assert drive.ranges[:2] == [(0, backup._DRIVE_CHUNK-1)] * 2
    assert receipt['verified_bytes'] == receipt['bytes']


def test_access_token_refresh_retries_unauthorized_chunk_only_once(isolated):
    directory, receipt, drive = _transfer_fixture(isolated); drive.unauthorized = True
    isolated.real_transfer(drive, directory, receipt)
    assert drive.token is None and bytes(drive.received) == drive.content


def test_storage_quota_failure_does_not_loop_and_reports_reason(isolated):
    directory, receipt, drive = _transfer_fixture(isolated); drive.reject = (403, 'storageQuotaExceeded')
    with pytest.raises(backup.BackupError) as caught: isolated.real_transfer(drive, directory, receipt)
    assert caught.value.code == 'drive_http_403_storageQuotaExceeded'
    assert 'storage is full' in caught.value.detail and not isolated.sleeps


def test_saved_upload_url_cannot_send_credentials_to_another_host(isolated):
    directory, receipt, drive = _transfer_fixture(isolated)
    receipt['drive_upload_session'] = 'https://untrusted.invalid/upload?upload_id=secret'
    with pytest.raises(backup.BackupError, match='invalid upload-session'):
        isolated.real_transfer(drive, directory, receipt)
    assert not drive.starts and drive.probes == 0


def test_full_export_preserves_all_bytes_and_uses_no_row_filters(isolated):
    result = backup.create_backup('a' * 32, '7')
    assert result['state'] == 'complete' and result['drive_verified']
    with zipfile.ZipFile(result['local_path']) as archive:
        assert archive.read('database.dump') == isolated.content
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['dump_bytes'] == len(isolated.content)
        assert 'fixture-password' not in json.dumps(manifest)
    with zipfile.ZipFile(io.BytesIO(isolated.uploads[0][2])) as archive:
        assert archive.read('database.dump').count(b'same-voucher') == 685
    assert len(isolated.calls) == 2
    dump, dump_env = isolated.calls[0]
    restore, restore_env = isolated.calls[1]
    assert '--format=custom' in dump and '--no-password' in dump
    assert not any(arg.startswith(('--table', '--schema', '--exclude', '--data-only', '--schema-only')) for arg in dump)
    assert 'fixture-password' not in ' '.join(dump)
    assert dump_env['PGPASSWORD'] == 'fixture-password'
    assert 'default_transaction_read_only=on' in dump_env['PGOPTIONS']
    assert 'DATABASE_URL' not in dump_env and 'DRIVE_REFRESH_TOKEN' not in dump_env
    assert restore[1:3] == ['--file', os.devnull]
    assert not any(key.startswith('PG') for key in restore_env)
    receipt = json.loads((Path(result['local_path']).parent / 'receipt.json').read_text())
    assert receipt['owner'] == '7'
    assert isolated.allocations == 1


def test_new_versions_and_lost_response_replay_never_overwrite(isolated):
    first = backup.create_backup('a' * 32, '7')
    first_bytes = Path(first['local_path']).read_bytes()
    assert backup.create_backup('a' * 32, '7') == first
    assert len(isolated.calls) == 2 and len(isolated.uploads) == 1
    second = backup.create_backup('b' * 32, '7')
    assert first['local_path'] != second['local_path']
    assert Path(first['local_path']).read_bytes() == first_bytes
    assert len(isolated.calls) == 4


def test_drive_failure_retry_reuses_local_dump_and_remote_id(isolated):
    isolated.fail_drive = True
    with pytest.raises(backup.BackupError) as caught:
        backup.create_backup('a' * 32, '7')
    assert caught.value.retry_same and caught.value.backup['state'] == 'local_verified'
    assert backup.status('7', 'a' * 32)['backup']['error_code'] == 'drive_connection_interrupted'
    saved = Path(caught.value.backup['local_path']).read_bytes()
    isolated.fail_drive = False
    result = backup.create_backup('a' * 32, '7')
    assert result['state'] == 'complete'
    assert result['error'] is None
    assert len(isolated.calls) == 2 and isolated.allocations == 1
    assert isolated.uploads[0][:2] == isolated.uploads[1][:2]
    assert Path(result['local_path']).read_bytes() == saved


def test_saved_backup_retry_runs_real_resumable_protocol_without_new_dump(isolated, monkeypatch):
    isolated.fail_drive = True
    with pytest.raises(backup.BackupError) as caught:
        backup.create_backup('a' * 32, '7')
    path = Path(caught.value.backup['local_path'])
    receipt = json.loads((path.parent / 'receipt.json').read_text())
    server = _DriveServer(receipt, path.read_bytes())
    monkeypatch.setattr(backup, 'DriveArchive', lambda: server)
    monkeypatch.setattr(backup, '_transfer_full_backup', isolated.real_transfer)
    result = backup.create_backup('a' * 32, '7')
    assert result['state'] == 'complete' and result['drive_verified']
    assert len(isolated.calls) == 2 and isolated.allocations == 1
    assert server.initiations == 1 and bytes(server.received) == path.read_bytes()
    assert backup.create_backup('a' * 32, '7') == result
    assert server.initiations == 1 and len(isolated.calls) == 2


def test_shared_drive_folder_blocks_upload_and_keeps_local_backup(isolated):
    isolated.folder_shared = True
    with pytest.raises(backup.BackupError, match='private, unshared') as caught:
        backup.create_backup('a' * 32, '7')
    assert caught.value.backup['state'] == 'local_verified'
    assert not isolated.uploads and isolated.allocations == 0


def test_shared_remote_file_cannot_be_marked_complete(isolated):
    isolated.file_shared = True
    with pytest.raises(backup.BackupError, match='file access needs review'):
        backup.create_backup('a' * 32, '7')
    assert backup.status('7', 'a' * 32)['backup']['state'] == 'local_verified'


def test_tampered_local_copy_is_retained_and_never_uploaded(isolated):
    result = backup.create_backup('a' * 32, '7')
    path = Path(result['local_path']); path.write_bytes(b'changed')
    with pytest.raises(backup.BackupError, match='local backup has changed'):
        backup.create_backup('a' * 32, '7')
    assert path.read_bytes() == b'changed'
    assert len(isolated.uploads) == 1


def test_failed_dump_never_becomes_a_verified_backup(isolated):
    isolated.native_failure = 'fixture dump failed'
    with pytest.raises(backup.BackupError):
        backup.create_backup('a' * 32, '7')
    assert not isolated.uploads
    assert backup.status('7', 'a' * 32)['backup']['state'] == 'incomplete'
    with pytest.raises(backup.BackupError, match='did not finish'):
        backup.create_backup('a' * 32, '7')
    assert len(isolated.calls) == 1


def test_status_distinguishes_running_and_interrupted_creation_without_deleting(isolated):
    root = backup._root(create=True)
    directory = backup._directory(root, 'a' * 32); directory.mkdir()
    receipt = {'operation_id': 'a' * 32, 'owner': '7', 'state': 'creating',
               'file_name': 'Database-20261025T120000Z-' + 'a' * 32 + '.zip'}
    backup._save(directory, receipt)
    original = (directory / 'receipt.json').read_bytes()
    with backup._exclusive(root):
        assert backup.status('7', 'a' * 32)['backup']['state'] == 'creating'
    assert backup.status('7', 'a' * 32)['backup']['state'] == 'incomplete'
    assert (directory / 'receipt.json').read_bytes() == original
    assert not isolated.calls


def test_unreadable_dump_never_reaches_drive(isolated, monkeypatch):
    initial = backup._native
    def fail_restore(args, env, **kwargs):
        if Path(args[0]).stem == 'pg_restore':
            raise backup.BackupError('Unreadable fixture archive')
        return initial(args, env, **kwargs)
    monkeypatch.setattr(backup, '_native', fail_restore)
    with pytest.raises(backup.BackupError):
        backup.create_backup('a' * 32, '7')
    assert not isolated.uploads
    assert backup.status('7', 'a' * 32)['backup']['state'] == 'incomplete'


def test_size_limit_retains_entire_local_copy_without_filtering(isolated, monkeypatch):
    monkeypatch.setattr(backup, '_MAX_DUMP_BYTES', 10)
    with pytest.raises(backup.BackupError, match='No rows were omitted') as caught:
        backup.create_backup('a' * 32, '7')
    with zipfile.ZipFile(caught.value.backup['local_path']) as archive:
        assert archive.read('database.dump') == isolated.content
    assert not isolated.uploads and not caught.value.retry_same


def test_existing_output_collision_is_never_overwritten(isolated, monkeypatch):
    initial = backup._native
    sentinel = b'old-backup-do-not-touch'
    collision = []
    def native(args, env, **kwargs):
        initial(args, env, **kwargs)
        if Path(args[0]).stem == 'pg_dump':
            partial = Path(args[args.index('--file') + 1])
            receipt = json.loads((partial.parent / 'receipt.json').read_text())
            target = partial.parent / receipt['file_name']
            target.write_bytes(sentinel); collision.append(target)
    monkeypatch.setattr(backup, '_native', native)
    with pytest.raises(FileExistsError):
        backup.create_backup('a' * 32, '7')
    assert collision[0].read_bytes() == sentinel and not isolated.uploads


def test_cross_process_lock_blocks_duplicate_dump(isolated):
    root = backup._root(create=True)
    with backup._exclusive(root):
        with pytest.raises(backup.BackupError) as caught:
            backup.create_backup('a' * 32, '7')
    assert caught.value.status == 409 and not isolated.calls


@pytest.mark.parametrize('operation', ['../outside', 'A' * 32, 'a' * 31, 'a' * 33])
def test_invalid_backup_reference_is_rejected(isolated, operation):
    with pytest.raises(backup.BackupError):
        backup.create_backup(operation, '7')
    assert not isolated.calls


def test_receipt_cannot_be_reused_by_different_admin(isolated):
    backup.create_backup('a' * 32, '7')
    with pytest.raises(backup.BackupError) as caught:
        backup.create_backup('a' * 32, '8')
    assert caught.value.status == 404 and len(isolated.calls) == 2


def test_corrupt_receipt_cannot_choose_an_arbitrary_file(isolated):
    result = backup.create_backup('a' * 32, '7')
    path = Path(result['local_path']).parent / 'receipt.json'
    receipt = json.loads(path.read_text()); receipt['file_name'] = '../outside.dump'
    path.write_text(json.dumps(receipt))
    with pytest.raises(backup.BackupError):
        backup.status('7', 'a' * 32)


@pytest.mark.parametrize('setting,value', [('RENDER', 'true'), ('LOCAL_DATABASE_BACKUP_ENABLED', 'false')])
def test_hosted_or_disabled_feature_never_runs_tools(isolated, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises(backup.BackupError):
        backup.create_backup('a' * 32, '7')
    assert not isolated.calls


def test_non_windows_runtime_never_runs_tools(isolated, monkeypatch):
    monkeypatch.setattr(backup.platform, 'system', lambda: 'Linux')
    assert not backup.configuration()['ready']
    with pytest.raises(backup.BackupError): backup.create_backup('a' * 32, '7')
    assert not isolated.calls


def test_archive_cannot_be_inside_checkout(isolated, monkeypatch):
    path = backup._PROJECT / 'must-not-be-created'
    monkeypatch.setenv('UPLOAD_ARCHIVE_DIR', str(path))
    with pytest.raises(backup.BackupError, match='outside'):
        backup.create_backup('a' * 32, '7')
    assert not path.exists() and not isolated.calls


def test_low_disk_stops_before_dump(isolated, monkeypatch):
    monkeypatch.setattr(backup.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1024))
    with pytest.raises(backup.BackupError, match='free local archive space'):
        backup.create_backup('a' * 32, '7')
    assert not isolated.calls


@pytest.mark.skipif(os.name != 'nt', reason='Windows path limit guard')
def test_long_archive_path_stops_before_native_export(isolated, monkeypatch, tmp_path):
    parent = tmp_path / ('x' * max(1, 150 - len(str(tmp_path))))
    monkeypatch.setenv('UPLOAD_ARCHIVE_DIR', str(parent))
    with pytest.raises(backup.BackupError, match='shorter protected archive folder'):
        backup.create_backup('a' * 32, '7')
    assert not isolated.calls


@pytest.mark.parametrize('key', ['DATABASE_BACKUP_BIN_DIR', 'DRIVE_REFRESH_TOKEN'])
def test_missing_requirements_stop_before_dump(isolated, monkeypatch, key):
    monkeypatch.setenv(key, 'not-a-valid-tool-folder' if key == 'DATABASE_BACKUP_BIN_DIR' else '')
    with pytest.raises(backup.BackupError): backup.create_backup('a' * 32, '7')
    assert not isolated.calls


@pytest.mark.parametrize('url', [
    'not-a-connection',
    'postgresql://user:secret@region.pooler.supabase.com:6543/postgres',
    'postgresql://user:secret@remote.invalid/postgres?sslmode=disable',
    'postgresql://user:secret@remote.invalid/postgres?options=bad',
])
def test_unsafe_or_incomplete_backup_connections_are_rejected(isolated, monkeypatch, url):
    monkeypatch.setenv('DATABASE_BACKUP_URL', url)
    with pytest.raises(backup.BackupError): backup.create_backup('a' * 32, '7')
    assert not isolated.calls


def test_dedicated_connection_does_not_modify_app_connection(isolated, monkeypatch):
    original = os.environ['DATABASE_URL']
    monkeypatch.setenv('DATABASE_BACKUP_URL', 'postgresql://reader:new-secret@remote.invalid:5432/postgres?sslmode=require')
    env = backup._connection_env()
    assert env['PGPASSWORD'] == 'new-secret' and env['PGPORT'] == '5432'
    assert os.environ['DATABASE_URL'] == original


@pytest.mark.parametrize('error', [subprocess.CalledProcessError(1, ['fixture'], stderr=b'fixture-password private'), subprocess.TimeoutExpired(['fixture'], 1)])
def test_native_failures_do_not_expose_secrets(isolated, monkeypatch, error):
    # Call the real wrapper rather than the fixture's successful native stand-in.
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location('backup_native_wrapper', backup.__file__)
    module = module_from_spec(spec); spec.loader.exec_module(module)
    def fail(*args, **kwargs): raise error
    monkeypatch.setattr(module.subprocess, 'run', fail)
    with pytest.raises(module.BackupError) as caught:
        module._native(['fixture'], {})
    assert 'fixture-password' not in str(caught.value) and 'private' not in str(caught.value)


@pytest.mark.parametrize('message,code', [
    ('SSL SYSCALL error: EOF detected', 'database_ssl_eof'),
    ('FATAL: terminating connection due to idle-in-transaction timeout', 'database_idle_timeout'),
    ('ERROR: canceling statement due to statement timeout', 'database_query_timeout'),
    ('ERROR: permission denied for table private_values', 'database_permission'),
])
def test_known_native_failure_is_classified_without_raw_sql_or_credentials(message, code):
    error = backup._native_failure((message + '\nCommand was: COPY private_values WITH fixture-password').encode())
    assert error.code == code
    assert 'fixture-password' not in error.detail and 'private_values' not in error.detail and 'COPY' not in error.detail


def test_native_diagnostic_redacts_connection_credentials_and_drops_source_sql():
    raw = b'pg_dump: error: connection to private-host failed for user private-user password=fixture-secret\nCommand was: COPY confidential_data TO stdout\npostgresql://user:secret@private-host/postgres'
    value = backup._safe_native_diagnostic(raw, {'PGPASSWORD': 'fixture-secret', 'PGHOST': 'private-host', 'PGUSER': 'private-user'})
    assert 'fixture-secret' not in value and 'private-host' not in value and 'private-user' not in value
    assert 'COPY' not in value and 'user:secret' not in value


def test_failed_export_retains_safe_cause_and_stage(isolated, monkeypatch):
    def fail(args, env, **kwargs):
        Path(args[args.index('--file') + 1]).write_bytes(b'PGDMPpartial')
        raise backup._native_failure(b'pg_dump: error: SSL SYSCALL error: EOF detected')
    monkeypatch.setattr(backup, '_native', fail)
    with pytest.raises(backup.BackupError) as caught:
        backup.create_backup('a' * 32, '7')
    value = caught.value.backup
    assert value['error_code'] == 'database_ssl_eof'
    assert value['progress']['stage'] == 'export' and value['progress']['local_bytes'] == 12
    assert value['state'] == 'incomplete' and not isolated.uploads
    assert backup.status('7', 'a' * 32)['backup']['error_code'] == 'database_ssl_eof'


def test_progress_only_exposes_business_label_and_local_size(isolated):
    from datetime import datetime, timezone
    root = backup._root(create=True); directory = backup._directory(root, 'a' * 32); directory.mkdir()
    (directory / 'database.partial').write_bytes(b'x' * 1024)
    (directory / 'native-progress.tmp').write_text('fixture-password\npg_dump: dumping contents of table "public.client_data"\n')
    receipt = {'state': 'creating', 'stage': 'export', 'created_at': datetime.now(timezone.utc).isoformat()}
    progress = backup._progress(directory, receipt)
    assert progress['note'] == 'Saving transaction rows' and progress['local_bytes'] == 1024
    assert 'fixture-password' not in json.dumps(progress)


@pytest.fixture
def client(isolated):
    app = FastAPI(); app.include_router(api.router)
    session = {'role': 'admin', 'user_id': 7}
    app.dependency_overrides[get_current_user] = lambda: session
    @app.middleware('http')
    async def fixture_peer(request, call_next):
        request.scope['client'] = (request.headers.get('x-fixture-peer', '127.0.0.1'), 40000)
        return await call_next(request)
    with TestClient(app, base_url='http://localhost') as value:
        yield value, session, app


def test_api_uses_admin_guard_and_has_no_restore_endpoint(client, isolated):
    http, session, _ = client
    assert http.get('/api/database-backup/status').json()['ready']
    assert http.post('/api/database-backup/create', json={'operation_id': 'a' * 32}).json()['drive_verified']
    session['role'] = 'analyst'
    assert http.get('/api/database-backup/status').status_code == 403
    assert http.post('/api/database-backup/create', json={'operation_id': 'b' * 32}).status_code == 403
    assert http.post('/api/database-backup/restore').status_code == 404


def test_unauthenticated_api_does_not_start_dump(client, isolated):
    http, _, app = client
    app.dependency_overrides.clear()
    assert http.get('/api/database-backup/status').status_code == 401
    assert not isolated.calls


@pytest.mark.parametrize('headers', [{'x-fixture-peer': '192.0.2.1'}, {'host': 'untrusted.invalid'}, {'origin': 'https://untrusted.invalid'}])
def test_remote_host_and_cross_origin_posts_are_blocked(client, isolated, headers):
    http, _, _ = client
    response = http.post('/api/database-backup/create', json={'operation_id': 'a' * 32}, headers=headers)
    assert response.status_code in (403, 404) and not isolated.calls


def test_hosted_api_is_unavailable_even_with_enabled_flag(client, isolated, monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    assert client[0].get('/api/database-backup/status').status_code == 404
    assert not isolated.calls


def test_api_reports_preserved_local_copy_for_deliberate_retry(client, isolated):
    isolated.fail_drive = True
    response = client[0].post('/api/database-backup/create', json={'operation_id': 'a' * 32})
    assert response.status_code == 503
    assert response.json()['retry_same'] and response.json()['backup']['state'] == 'local_verified'


@pytest.fixture
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        yield browser
        browser.close()


def _screen(browser, *, role='admin', mode='success', width=1366):
    page = browser.new_page(viewport={'width': width, 'height': 900})
    state = {'posts': [], 'lost': False, 'stage': 0, 'pending_route': None, 'drive_action': 'upload'}
    static = Path(__file__).resolve().parents[1] / 'static'
    def result(operation, complete=True):
        return {'operation_id': operation, 'state': 'complete' if complete else 'local_verified',
                'file_name': 'Database-20261025T120000Z-' + operation + '.zip',
                'local_path': 'D:\\Protected Audit Archives\\full-database-backups\\backup-' + operation + '\\Database-test.zip',
                'created_at': '2026-10-25T12:00:00+00:00', 'drive_verified': complete,
                'verified_at': '2026-10-25T12:01:00+00:00' if complete else None,
                'bytes': 1048576,
                'progress': {'stage': 'complete' if complete else 'drive', 'completed_steps': 4 if complete else 3,
                             'label': 'Both backup copies verified' if complete else 'Copying to Drive and comparing both copies',
                             'local_bytes': 1048576, 'elapsed_seconds': 60},
                'drive_url': 'https://drive.google.com/file/d/fixture-id/view' if complete else None}
    def serve(route):
        url = urlsplit(route.request.url); path = url.path
        if path == '/static/client_context.js':
            route.fulfill(content_type='text/javascript', body="const API_BASE='';window.clientContextReady=Promise.resolve();window.isAdminUser=()=>" + ('true' if role == 'admin' else 'false') + ';')
        elif path.startswith('/static/'):
            file = static / path.removeprefix('/static/')
            types = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css'}
            route.fulfill(content_type=types.get(file.suffix, 'text/plain'), body=file.read_bytes())
        elif path == '/api/database-backup/status':
            value = {'ready': True, 'requirements': []}
            if mode == 'lost' and state['posts']:
                value['backup'] = result(state['posts'][0])
            if mode == 'progress' and state['posts']:
                stages = [('export', 'Saving database data'), ('native_check', 'Checking the database file'),
                          ('package', 'Preparing the dated backup ZIP'), ('drive', 'Copying to Drive and comparing both copies')]
                index = state['stage']; stage, label = stages[index]
                value['backup'] = result(state['posts'][0], False)
                value['backup'].update(state='creating' if index < 3 else 'local_verified', bytes=None if index < 3 else 1048576, running=True,
                    progress={'stage': stage, 'completed_steps': index, 'label': label,
                              'note': 'Saving transaction rows' if index == 0 else label,
                              'local_bytes': 12582912, 'elapsed_seconds': 120,
                              'transfer': {'action': state['drive_action'], 'uploaded_bytes': 524288,
                                           'verified_bytes': 262144, 'total_bytes': 1048576}})
            route.fulfill(json=value)
        elif path == '/api/database-backup/create':
            operation = route.request.post_data_json['operation_id']; state['posts'].append(operation)
            if mode == 'lost':
                route.abort('failed')
            elif mode == 'progress':
                state['pending_route'] = route
            elif mode == 'native-failure':
                value = result(operation, False)
                value.update(state='incomplete', bytes=None, error='The database connection ended unexpectedly during the backup (SSL EOF).', error_code='database_ssl_eof',
                             progress={'stage': 'export', 'completed_steps': 0, 'label': 'Saving database data', 'local_bytes': 5893458, 'elapsed_seconds': 182})
                route.fulfill(status=503, json={'detail': value['error'], 'backup': value, 'retry_same': False})
            elif mode == 'drive-failure' and len(state['posts']) == 1:
                route.fulfill(status=503, json={'detail': 'The local backup is saved. Retry the Drive copy.', 'retry_same': True, 'backup': result(operation, False)})
            else:
                route.fulfill(json=result(operation))
        elif path in ('/api/columns', '/api/templates'):
            route.fulfill(json=[])
        else:
            route.fulfill(status=404, body='fixture not found')
    page.route('http://backup.test/**', serve)
    page.goto('http://backup.test/static/setup.html')
    return page, state


@pytest.mark.parametrize('width', [1366, 390])
def test_backup_screen_success_and_mobile_layout(browser, tmp_path, width):
    page, state = _screen(browser, width=width)
    try:
        button = page.locator('#createFullDatabaseBackupBtn'); button.wait_for(state='visible')
        button.click()
        page.wait_for_function("document.getElementById('fullDatabaseBackupStatus').dataset.kind==='success'")
        assert len(state['posts']) == 1
        assert page.locator('#fullDatabaseBackupDriveLink').get_attribute('href').endswith('/fixture-id/view')
        assert page.locator('#fullDatabaseBackupDriveLink').get_attribute('rel') == 'noopener noreferrer'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('#exportBackupBtn').is_visible() and page.locator('#importBackupBtn').is_visible()
        page.locator('#fullDatabaseBackup').scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / ('backup-' + str(width) + '.png')))
    finally: page.close()


def test_backup_screen_drive_retry_reuses_same_reference(browser):
    page, state = _screen(browser, mode='drive-failure')
    try:
        page.locator('#createFullDatabaseBackupBtn').click()
        page.get_by_role('button', name='Retry Drive copy', exact=True).click()
        page.wait_for_function("document.getElementById('fullDatabaseBackupStatus').dataset.kind==='success'")
        assert len(state['posts']) == 2 and state['posts'][0] == state['posts'][1]
    finally: page.close()


@pytest.mark.parametrize('recovery', ['button', 'reload'])
def test_backup_screen_lost_response_checks_status_without_second_post(browser, recovery):
    page, state = _screen(browser, mode='lost')
    try:
        page.locator('#createFullDatabaseBackupBtn').click()
        page.wait_for_function("document.getElementById('fullDatabaseBackupStatus').textContent.includes('interrupted')")
        if recovery == 'reload': page.reload()
        else: page.get_by_role('button', name='Check backup status', exact=True).click()
        page.wait_for_function("document.getElementById('fullDatabaseBackupStatus').dataset.kind==='success'")
        assert len(state['posts']) == 1
    finally: page.close()


def test_backup_screen_hidden_from_non_admin(browser):
    page, state = _screen(browser, role='analyst')
    try:
        assert page.locator('#fullDatabaseBackup').is_hidden() and not state['posts']
    finally: page.close()


@pytest.mark.parametrize('width', [1366, 390])
def test_backup_screen_reports_real_stages_while_post_is_pending(browser, tmp_path, width):
    page, state = _screen(browser, mode='progress', width=width)
    try:
        page.locator('#createFullDatabaseBackupBtn').click()
        page.wait_for_function("document.getElementById('fullDatabaseBackupMetrics').textContent.includes('12.0 MiB')")
        assert page.locator('#fullDatabaseBackupSpinner').is_visible()
        assert page.locator('#fullDatabaseBackupBar').get_attribute('aria-valuenow') == '0'
        assert page.locator('#fullDatabaseBackupActivity').inner_text() == 'Saving transaction rows'
        page.locator('#fullDatabaseBackupProgress').scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / ('backup-progress-' + str(width) + '.png')))
        for step in (1, 2, 3):
            state['stage'] = step
            page.wait_for_function('n => document.getElementById("fullDatabaseBackupBar").getAttribute("aria-valuenow") === String(n)', arg=step)
        assert page.locator('#fullDatabaseBackupTransferText').inner_text() == 'Drive upload: 0.5 / 1.0 MiB (50%)'
        assert page.locator('#fullDatabaseBackupTransferBar').get_attribute('aria-valuenow') == '50'
        state['drive_action'] = 'verify'
        page.wait_for_function("document.getElementById('fullDatabaseBackupTransferText').textContent.includes('Drive download check')")
        assert page.locator('#fullDatabaseBackupTransferBar').get_attribute('aria-valuenow') == '25'
        page.locator('#fullDatabaseBackupProgress').scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / ('backup-transfer-' + str(width) + '.png')))
        assert len(state['posts']) == 1
        operation = state['posts'][0]
        state['pending_route'].fulfill(json={
            'operation_id': operation, 'state': 'complete', 'drive_verified': True,
            'file_name': 'Database-test.zip', 'local_path': 'D:\\Protected Archives\\Database-test.zip',
            'drive_url': 'https://drive.google.com/file/d/fixture-id/view',
            'progress': {'stage': 'complete', 'completed_steps': 4, 'label': 'Both backup copies verified'},
        })
        page.wait_for_function("document.getElementById('fullDatabaseBackupBar').getAttribute('aria-valuenow')==='4'")
        assert page.locator('#fullDatabaseBackupSpinner').is_hidden()
        assert page.locator('#fullDatabaseBackupTransfer').is_hidden()
    finally: page.close()


def test_backup_screen_native_failure_shows_cause_without_false_completion(browser):
    page, state = _screen(browser, mode='native-failure')
    try:
        page.locator('#createFullDatabaseBackupBtn').click()
        page.wait_for_function("document.getElementById('fullDatabaseBackupStatus').textContent.includes('SSL EOF')")
        assert page.locator('#fullDatabaseBackupBar').get_attribute('aria-valuenow') == '0'
        assert page.locator('#fullDatabaseBackupSpinner').is_hidden()
        assert page.locator('#fullDatabaseBackupDetails').is_hidden()
        assert page.locator('#fullDatabaseBackupDriveLink').is_hidden()
        assert len(state['posts']) == 1
        assert 'Import replaces existing' in page.locator('body').inner_text()
    finally: page.close()


@pytest.mark.skipif(not os.environ.get('TEST_POSTGRES_BIN_DIR'), reason='Optional isolated native-tool fixture; no live DB/Drive')
def test_native_dump_keeps_every_synthetic_voucher_line(isolated, monkeypatch, tmp_path):
    """Temporary loopback PostgreSQL source only; does not perform a DB restore."""
    import psycopg2
    tools = Path(os.environ['TEST_POSTGRES_BIN_DIR']).resolve()
    suffix = '.exe' if os.name == 'nt' else ''
    clean = backup._process_env()
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
    cluster = tmp_path / 'fixture-cluster'
    def tool(name, *args):
        # File-backed output avoids Windows descendants retaining PIPE handles
        # after pg_ctl exits; the server's own log stays inside the fixture.
        with tempfile.TemporaryFile() as output:
            return subprocess.run([str(tools / (name + suffix)), *map(str, args)], env=clean, check=True,
                                  stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, timeout=90,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    tool('initdb', '-D', cluster, '-U', 'fixture', '-A', 'trust', '--no-locale', '--encoding=UTF8')
    archive_work = tempfile.TemporaryDirectory(prefix='auditbk-')
    monkeypatch.setenv('UPLOAD_ARCHIVE_DIR', archive_work.name)
    try:
        tool('pg_ctl', '-D', cluster, '-l', tmp_path / 'fixture-server.log', '-o', f'-h 127.0.0.1 -p {port} -F', '-w', 'start')
        conn = psycopg2.connect(host='127.0.0.1', port=port, user='fixture', dbname='postgres', sslmode='disable', connect_timeout=5)
        try:
            conn.autocommit = True
            with conn.cursor() as cur: cur.execute('CREATE DATABASE backup_fixture')
        finally: conn.close()
        source = psycopg2.connect(host='127.0.0.1', port=port, user='fixture', dbname='backup_fixture', sslmode='disable', connect_timeout=5)
        try:
            with source, source.cursor() as cur:
                cur.execute("SELECT current_database(), host(inet_server_addr())")
                assert cur.fetchone() == ('backup_fixture', '127.0.0.1')
                cur.execute('CREATE TABLE voucher_lines (id integer PRIMARY KEY, voucher text, amount numeric, notes text)')
                cur.executemany('INSERT INTO voucher_lines VALUES (%s,%s,%s,%s)', [(i, 'TEST-VOUCHER', '123.4500', 'same repeated line') for i in range(685)])
                cur.execute('CREATE TABLE independent_settings (name text)')
                cur.execute("INSERT INTO independent_settings VALUES ('KEEP-SETTINGS')")
                cur.execute('CREATE TABLE exact_repeats (voucher text, amount numeric)')
                cur.executemany('INSERT INTO exact_repeats VALUES (%s,%s)', [('TEST-REPEAT', '1.00')] * 3)
                cur.execute('SELECT id, voucher, amount::text, notes FROM voucher_lines ORDER BY id')
                before = cur.fetchall()
            monkeypatch.setenv('DATABASE_BACKUP_BIN_DIR', str(tools))
            monkeypatch.setenv('DATABASE_URL', f'postgresql://fixture@127.0.0.1:{port}/backup_fixture?sslmode=disable')
            # Restore the real native wrapper; Drive stays fake from isolated fixture.
            from importlib.util import spec_from_file_location, module_from_spec
            spec = spec_from_file_location('backup_real_native', backup.__file__)
            module = module_from_spec(spec); spec.loader.exec_module(module)
            monkeypatch.setattr(backup, '_native', module._native)
            result = backup.create_backup('c' * 32, '7')
            script = tmp_path / 'fixture-readback.sql'
            native_dump = tmp_path / 'fixture-readback.dump'
            with zipfile.ZipFile(result['local_path']) as archive:
                native_dump.write_bytes(archive.read('database.dump'))
            tool('pg_restore', '--file', script, native_dump)
            text = script.read_text(encoding='utf-8')
            assert text.count('\tTEST-VOUCHER\t123.4500\tsame repeated line') == 685
            assert 'KEEP-SETTINGS' in text
            assert text.count('TEST-REPEAT\t1.00') == 3
            with source, source.cursor() as cur:
                cur.execute('SELECT count(*), sum(amount) FROM voucher_lines')
                count, total = cur.fetchone()
                assert count == 685 and str(total) == '84563.2500'
                cur.execute('SELECT id, voucher, amount::text, notes FROM voucher_lines ORDER BY id')
                assert cur.fetchall() == before
            assert result['drive_verified']
        finally: source.close()
    finally:
        if (cluster / 'postmaster.pid').exists():
            tool('pg_ctl', '-D', cluster, '-m', 'fast', '-w', 'stop')
        archive_work.cleanup()
