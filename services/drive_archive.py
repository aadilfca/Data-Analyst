"""Verified external upload archives; OAuth credentials stay in server settings."""

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ArchiveError(ValueError):
    """Archiving did not complete; current source data must remain untouched."""


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

    def __init__(self):
        names = ('DRIVE_CLIENT_ID', 'DRIVE_CLIENT_SECRET', 'DRIVE_REFRESH_TOKEN', 'DRIVE_ARCHIVE_FOLDER_ID')
        values = [os.environ.get(name, '').strip() for name in names]
        if not all(values):
            raise ArchiveError('The running app Google Drive connection is not configured. Configure DRIVE_CLIENT_ID, DRIVE_CLIENT_SECRET, DRIVE_REFRESH_TOKEN and DRIVE_ARCHIVE_FOLDER_ID.')
        self.client_id, self.client_secret, self.refresh_token, self.folder_id = values
        if not re.fullmatch(r'[A-Za-z0-9_-]+', self.folder_id):
            raise ArchiveError('DRIVE_ARCHIVE_FOLDER_ID must be a Drive folder ID.')
        self.token = None

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
        except HTTPError:
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
            folder = self._json('files/' + self.folder_id + '?' + urlencode({'fields': 'id,mimeType,trashed,capabilities(canAddChildren)', 'supportsAllDrives': 'true'}))
            if (folder.get('trashed') or folder.get('mimeType') != 'application/vnd.google-apps.folder'
                    or not folder.get('capabilities', {}).get('canAddChildren')):
                raise ArchiveError('The configured archive folder is unavailable or not writable.')
            allocated = self._json('files/generateIds?' + urlencode({'count': 1, 'space': 'drive'}))
            identifiers = allocated.get('ids') if isinstance(allocated, dict) else None
            if not identifiers or not isinstance(identifiers[0], str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identifiers[0]):
                raise ArchiveError('Google Drive did not allocate a valid archive file ID.')
            return identifiers[0]
        except HTTPError as error:
            raise ArchiveError(f'Google Drive folder/access check failed (HTTP {error.code}).') from error

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
            'fields': 'id,size,parents,trashed,appProperties', 'supportsAllDrives': 'true'}))
