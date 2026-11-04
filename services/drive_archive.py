"""Verified external upload archives; OAuth credentials stay in server settings."""

import hashlib
import base64
import hmac
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from database import execute_query_pg, execute_write_pg


class ArchiveError(ValueError):
    """Archiving did not complete; current source data must remain untouched."""

    def __init__(self, message, *, code=None, outcome=None):
        super().__init__(message)
        self.code = code
        self.outcome = outcome


_TOKEN_KEY_CONTEXT = b'internal-audit-app/google-drive-refresh-token/v1'


def _token_cipher(client_secret=None):
    secret = client_secret if client_secret is not None else os.environ.get('DRIVE_CLIENT_SECRET', '')
    secret = str(secret or '').strip()
    if not secret:
        raise ArchiveError('Configure the Google Drive OAuth client secret before reconnecting.')
    key = base64.urlsafe_b64encode(hmac.new(secret.encode(), _TOKEN_KEY_CONTEXT, hashlib.sha256).digest())
    return Fernet(key)


def encrypt_refresh_token(refresh_token, client_secret=None):
    if not isinstance(refresh_token, str) or not refresh_token.strip() or len(refresh_token) > 4096:
        raise ArchiveError('Google returned an invalid Drive authorization token.')
    return _token_cipher(client_secret).encrypt(refresh_token.encode()).decode('ascii')


def decrypt_refresh_token(encrypted_token, client_secret=None):
    try:
        return _token_cipher(client_secret).decrypt(str(encrypted_token).encode('ascii')).decode('utf-8')
    except (InvalidToken, UnicodeError, ValueError, TypeError):
        raise ArchiveError(
            'The saved Google Drive permission cannot be unlocked. Reconnect Google Drive in Settings.',
            code='drive_auth_reconnect_required', outcome='not_applied') from None


def load_saved_refresh_token(client_secret=None):
    rows = execute_query_pg(
        'SELECT encrypted_refresh_token FROM google_drive_credentials WHERE id = 1')
    if not rows:
        return None
    return decrypt_refresh_token(rows[0].get('encrypted_refresh_token'), client_secret)


def save_refresh_token(refresh_token, client_secret=None):
    encrypted = encrypt_refresh_token(refresh_token, client_secret)
    execute_write_pg('''INSERT INTO google_drive_credentials
        (id, encrypted_refresh_token, updated_at) VALUES (1, %s, now())
        ON CONFLICT (id) DO UPDATE SET
            encrypted_refresh_token = EXCLUDED.encrypted_refresh_token,
            updated_at = now()''', (encrypted,))


def archive_directory():
    configured = os.environ.get('UPLOAD_ARCHIVE_DIR', '').strip()
    if not configured:
        raise ArchiveError('Configure UPLOAD_ARCHIVE_DIR and the app Google Drive connection before replacing existing data.')
    root = Path(configured).expanduser().resolve()
    project = Path(__file__).resolve().parents[1]
    if root == project or project in root.parents:
        raise ArchiveError('UPLOAD_ARCHIVE_DIR must be a protected directory outside the application checkout.')
    root.mkdir(parents=True, exist_ok=True)
    return root


class DriveArchive:
    """Small Drive v3 client using Python's standard HTTPS/OAuth facilities.

    Preallocated IDs make a repeated create safe after a lost response. Every
    archive is downloaded and SHA-256 verified before database writes are allowed.
    """

    def __init__(self, refresh_token_override=None):
        self.client_id = os.environ.get('DRIVE_CLIENT_ID', '').strip()
        self.client_secret = os.environ.get('DRIVE_CLIENT_SECRET', '').strip()
        self.folder_id = os.environ.get('DRIVE_ARCHIVE_FOLDER_ID', '').strip()
        if not self.client_id or not self.client_secret or not self.folder_id:
            raise ArchiveError('Configure DRIVE_CLIENT_ID, DRIVE_CLIENT_SECRET and DRIVE_ARCHIVE_FOLDER_ID for the app Google Drive connection.')
        legacy_refresh_token = os.environ.get('DRIVE_REFRESH_TOKEN', '').strip()
        if refresh_token_override is not None:
            self.refresh_token = str(refresh_token_override).strip()
        else:
            try:
                self.refresh_token = load_saved_refresh_token(self.client_secret) or legacy_refresh_token
            except ArchiveError:
                raise
            except Exception as error:
                if not legacy_refresh_token:
                    raise ArchiveError('The saved Google Drive connection could not be loaded. Check the database and reconnect in Settings.') from error
                # Preserve legacy local/development setup while the shared database is unavailable.
                self.refresh_token = legacy_refresh_token
        if not self.refresh_token:
            raise ArchiveError('The running app Google Drive connection is not configured. Reconnect Google Drive in Settings.')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', self.folder_id):
            raise ArchiveError('DRIVE_ARCHIVE_FOLDER_ID must be a Drive folder ID.')
        self.token = None

    def verify_archive_folder(self):
        folder = self._json('files/' + self.folder_id + '?' + urlencode({
            'fields': 'id,mimeType,trashed,capabilities(canAddChildren)', 'supportsAllDrives': 'true'}))
        if (folder.get('trashed') or folder.get('mimeType') != 'application/vnd.google-apps.folder'
                or not folder.get('capabilities', {}).get('canAddChildren')):
            raise ArchiveError('The configured archive folder is unavailable or not writable.')
        return True

    def _open(self, url, data=None, method=None, headers=None, *, authenticated=True):
        try:
            if authenticated and self.token is None:
                payload = urlencode({'client_id': self.client_id, 'client_secret': self.client_secret,
                                     'refresh_token': self.refresh_token, 'grant_type': 'refresh_token'}).encode()
                with self._open('https://oauth2.googleapis.com/token', payload,
                                headers={'Content-Type': 'application/x-www-form-urlencoded'}, authenticated=False) as response:
                    self.token = json.load(response)['access_token']
            request_headers = dict(headers or {})
            if authenticated:
                request_headers['Authorization'] = 'Bearer ' + self.token
            return urlopen(Request(url, data=data, method=method, headers=request_headers), timeout=45)
        except HTTPError as error:
            if url == 'https://oauth2.googleapis.com/token':
                try:
                    response = json.loads(error.read().decode('utf-8', 'replace'))
                except (AttributeError, ValueError, TypeError):
                    response = {}
                if response.get('error') == 'invalid_grant':
                    raise ArchiveError(
                        'The app Google Drive authorization has expired or was revoked. Reconnect Google Drive in Settings, then validate and retry. No app data was changed.',
                        code='drive_auth_reconnect_required', outcome='not_applied') from error
                if response.get('error') == 'invalid_client':
                    raise ArchiveError(
                        'The app Google Drive client ID or secret was rejected. Correct the Drive connection settings, then validate and retry. No app data was changed.',
                        code='drive_client_config_invalid', outcome='not_applied') from error
            raise
        except ArchiveError:
            raise
        except (URLError, OSError, KeyError, ValueError, TypeError) as error:
            raise ArchiveError('Google Drive could not be reached or authenticated. Current data has not been replaced.') from error

    def _json(self, path, data=None, method=None):
        try:
            with self._open('https://www.googleapis.com/drive/v3/' + path, data, method,
                            {'Content-Type': 'application/json'}) as response:
                return json.load(response)
        except (ValueError, KeyError, TypeError) as error:
            raise ArchiveError('Google Drive returned an invalid archive response.') from error

    def allocate_id(self):
        try:
            self.verify_archive_folder()
            allocated = self._json('files/generateIds?' + urlencode({'count': 1, 'space': 'drive'}))
            identifiers = allocated.get('ids') if isinstance(allocated, dict) else None
            if not identifiers or not isinstance(identifiers[0], str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identifiers[0]):
                raise ArchiveError('Google Drive did not allocate a valid archive file ID.')
            return identifiers[0]
        except HTTPError as error:
            raise ArchiveError(f'Google Drive folder/access check failed (HTTP {error.code}).') from error

    @staticmethod
    def _drive_url(file_id):
        return 'https://drive.google.com/drive/folders/' + file_id

    def _find_child_folder(self, parent_id, name):
        escaped = name.replace('\\', '\\\\').replace("'", "\\'")
        query = "name = '" + escaped + "' and mimeType = 'application/vnd.google-apps.folder' and '" + parent_id + "' in parents and trashed = false"
        result = self._json('files?' + urlencode({
            'q': query, 'pageSize': 100, 'fields': 'files(id,name,mimeType,parents,webViewLink,trashed)',
            'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'}))
        folders = result.get('files', []) if isinstance(result, dict) else []
        return next((folder for folder in folders if not folder.get('trashed')
                     and folder.get('mimeType') == 'application/vnd.google-apps.folder'
                     and parent_id in folder.get('parents', [])), None)

    def _ensure_child_folder(self, parent_id, name):
        folder = self._find_child_folder(parent_id, name)
        if folder:
            return folder
        try:
            folder = self._json('files?supportsAllDrives=true', json.dumps({
                'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]
            }).encode(), 'POST')
        except HTTPError as error:
            # A concurrent retry may have created the same dated folder.
            if error.code != 409:
                raise
            folder = self._find_child_folder(parent_id, name)
        if not isinstance(folder, dict) or not re.fullmatch(r'[A-Za-z0-9_-]+', folder.get('id', '')):
            folder = self._find_child_folder(parent_id, name)
        if not folder:
            raise ArchiveError('Google Drive could not create the dated change-report folder.')
        folder.setdefault('webViewLink', self._drive_url(folder['id']))
        return folder

    def upload_change_report(self, content, filename, client_name, import_date, operation_id):
        """Store one deterministic XLSX in the client's dated Drive folder and verify it."""
        if not re.fullmatch(r'[a-f0-9]{32}', operation_id or ''):
            raise ArchiveError('Invalid change-report operation ID.')
        if not isinstance(content, bytes) or not content:
            raise ArchiveError('The upload change report is empty.')
        if not re.fullmatch(r'[A-Za-z0-9._ -]{1,120}\.xlsx', filename):
            raise ArchiveError('Invalid change-report filename.')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', import_date):
            raise ArchiveError('Invalid change-report date.')
        checksum = hashlib.sha256(content).hexdigest()
        try:
            folder = self._json('files/' + self.folder_id + '?' + urlencode({
                'fields': 'id,mimeType,trashed,capabilities(canAddChildren)', 'supportsAllDrives': 'true'}))
            if (folder.get('trashed') or folder.get('mimeType') != 'application/vnd.google-apps.folder'
                    or not folder.get('capabilities', {}).get('canAddChildren')):
                raise ArchiveError('The configured archive folder is unavailable or not writable.')
            parent = self._ensure_child_folder(self.folder_id, 'Upload Change Reports')
            client_folder = self._ensure_child_folder(parent['id'], client_name)
            year_folder = self._ensure_child_folder(client_folder['id'], import_date[:4])
            day_folder = self._ensure_child_folder(year_folder['id'], import_date)
            escaped = filename.replace('\\', '\\\\').replace("'", "\\'")
            query = "name = '" + escaped + "' and '" + day_folder['id'] + "' in parents and trashed = false"
            listing = self._json('files?' + urlencode({
                'q': query, 'pageSize': 100,
                'fields': 'files(id,name,mimeType,parents,webViewLink,size,trashed,appProperties)',
                'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'}))
            matches = listing.get('files', []) if isinstance(listing, dict) else []
            existing = next((item for item in matches if not item.get('trashed')
                             and day_folder['id'] in item.get('parents', [])), None)
            if existing:
                file_id = existing.get('id')
                metadata = self._metadata(file_id)
                if (metadata.get('appProperties', {}).get('operation_id') != operation_id
                        or metadata.get('appProperties', {}).get('sha256') != checksum
                        or int(metadata.get('size', -1)) != len(content)):
                    raise ArchiveError('An existing report has the same name but different import evidence.')
            else:
                allocated = self._json('files/generateIds?' + urlencode({'count': 1, 'space': 'drive'}))
                identifiers = allocated.get('ids') if isinstance(allocated, dict) else None
                if not identifiers or not re.fullmatch(r'[A-Za-z0-9_-]+', identifiers[0]):
                    raise ArchiveError('Google Drive did not allocate a change-report file ID.')
                file_id = identifiers[0]
                boundary = 'audit_report_' + operation_id
                metadata = {'id': file_id, 'name': filename, 'parents': [day_folder['id']],
                            'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                            'appProperties': {'operation_id': operation_id, 'sha256': checksum}}
                body = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode()
                        + json.dumps(metadata).encode() + f'\r\n--{boundary}\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n'.encode()
                        + content + f'\r\n--{boundary}--\r\n'.encode())
                try:
                    with self._open('https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true',
                                    body, headers={'Content-Type': 'multipart/related; boundary=' + boundary}):
                        pass
                except HTTPError as conflict:
                    if conflict.code != 409:
                        raise
                metadata = self._metadata(file_id)
            if (metadata.get('trashed') or day_folder['id'] not in metadata.get('parents', [])
                    or metadata.get('name') != filename
                    or metadata.get('mimeType') != 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                    or metadata.get('appProperties', {}).get('operation_id') != operation_id
                    or metadata.get('appProperties', {}).get('sha256') != checksum
                    or int(metadata.get('size', -1)) != len(content)):
                raise ArchiveError('Google Drive change-report metadata does not match this import.')
            digest, length = hashlib.sha256(), 0
            with self._open('https://www.googleapis.com/drive/v3/files/' + file_id + '?alt=media&supportsAllDrives=true') as response:
                while chunk := response.read(1024 * 1024):
                    length += len(chunk)
                    if length > len(content):
                        raise ArchiveError('Google Drive change-report size verification failed.')
                    digest.update(chunk)
            if length != len(content) or digest.hexdigest() != checksum:
                raise ArchiveError('Google Drive change-report checksum verification failed.')
            file_metadata = self._metadata(file_id)
            file_url = file_metadata.get('webViewLink') or 'https://drive.google.com/file/d/' + file_id + '/view'
            return {'file_id': file_id, 'file_url': file_url,
                    'folder_url': day_folder.get('webViewLink') or self._drive_url(day_folder['id']),
                    'folder_path': f'Upload Change Reports/{client_name}/{import_date[:4]}/{import_date}',
                    'sha256': checksum, 'filename': filename}
        except HTTPError as error:
            raise ArchiveError(f'Google Drive change-report transfer failed (HTTP {error.code}).') from error

    def upload_and_verify(self, file_id, path, operation_id, expected_sha256):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', file_id):
            raise ArchiveError('Invalid archive file ID.')
        content = Path(path).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ArchiveError('The local archive changed; current data has not been replaced.')
        try:
            try:
                metadata = self._metadata(file_id)
            except HTTPError as error:
                if error.code != 404:
                    raise
                boundary = 'audit_archive_' + operation_id
                metadata = {'id': file_id, 'name': Path(path).name, 'parents': [self.folder_id],
                            'mimeType': 'application/zip',
                            'appProperties': {'operation_id': operation_id, 'sha256': expected_sha256}}
                body = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode()
                        + json.dumps(metadata).encode() + f'\r\n--{boundary}\r\nContent-Type: application/zip\r\n\r\n'.encode()
                        + content + f'\r\n--{boundary}--\r\n'.encode())
                try:
                    with self._open('https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true',
                                    body, headers={'Content-Type': 'multipart/related; boundary=' + boundary}):
                        pass
                except HTTPError as conflict:
                    if conflict.code != 409:
                        raise
                metadata = self._metadata(file_id)
            if (metadata.get('trashed') or self.folder_id not in metadata.get('parents', [])
                    or metadata.get('appProperties', {}).get('operation_id') != operation_id
                    or metadata.get('appProperties', {}).get('sha256') != expected_sha256
                    or int(metadata.get('size', -1)) != len(content)):
                raise ArchiveError('Google Drive archive metadata does not match this replacement.')
            checksum, length = hashlib.sha256(), 0
            with self._open('https://www.googleapis.com/drive/v3/files/' + file_id + '?alt=media&supportsAllDrives=true') as response:
                while chunk := response.read(1024 * 1024):
                    length += len(chunk)
                    if length > len(content):
                        raise ArchiveError('Google Drive archive size verification failed.')
                    checksum.update(chunk)
            if length != len(content) or checksum.hexdigest() != expected_sha256:
                raise ArchiveError('Google Drive archive checksum verification failed.')
            return {'drive_file_id': file_id, 'sha256': expected_sha256, 'local_path': str(path)}
        except HTTPError as error:
            raise ArchiveError(f'Google Drive archive transfer failed (HTTP {error.code}); retry the same import after restoring access.') from error

    def _metadata(self, file_id):
        return self._json('files/' + file_id + '?' + urlencode({
            'fields': 'id,name,mimeType,size,parents,trashed,webViewLink,appProperties', 'supportsAllDrives': 'true'}))
