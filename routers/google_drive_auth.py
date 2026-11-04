"""Administrator-only Google Drive reconnection and token storage."""

import base64
import hashlib
import json
import os
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException, Request as WebRequest
from fastapi.responses import RedirectResponse

from routers.auth import require_admin
from services.drive_archive import ArchiveError, DriveArchive, save_refresh_token


router = APIRouter()
_SCOPE = 'https://www.googleapis.com/auth/drive'
_STATE_COOKIE = 'drive_oauth_state'
_VERIFIER_COOKIE = 'drive_oauth_verifier'
_COOKIE_PATH = '/api/google-drive'
_SETTINGS_URL = '/static/settings.html'
_COOKIE_MAX_AGE = 600


def _oauth_config():
    client_id = os.environ.get('DRIVE_CLIENT_ID', '').strip()
    client_secret = os.environ.get('DRIVE_CLIENT_SECRET', '').strip()
    redirect_uri = os.environ.get('DRIVE_OAUTH_REDIRECT_URI', '').strip()
    folder_id = os.environ.get('DRIVE_ARCHIVE_FOLDER_ID', '').strip()
    if not all((client_id, client_secret, redirect_uri, folder_id)):
        raise HTTPException(
            503,
            'Google Drive reconnect is not configured. The app needs its OAuth web client, callback address, and archive folder.',
        )
    parsed = urlsplit(redirect_uri)
    local_http = parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1')
    if (parsed.scheme != 'https' and not local_http) or not parsed.netloc or parsed.query or parsed.fragment:
        raise HTTPException(503, 'DRIVE_OAUTH_REDIRECT_URI must be an HTTPS callback address (HTTP localhost is allowed for local development).')
    if not parsed.path.endswith('/api/google-drive/callback'):
        raise HTTPException(503, 'The Google OAuth callback address must end in /api/google-drive/callback.')
    return client_id, client_secret, redirect_uri


def _secure_cookie(request):
    return request.url.scheme == 'https' or bool(os.environ.get('RENDER'))


def _clear_oauth_cookies(response, request):
    for name in (_STATE_COOKIE, _VERIFIER_COOKIE):
        response.delete_cookie(name, path=_COOKIE_PATH, httponly=True,
                               secure=_secure_cookie(request), samesite='lax')


def _settings_redirect(result):
    return RedirectResponse(_SETTINGS_URL + '?drive_oauth=' + result, status_code=303)


@router.get('/api/google-drive/status')
def google_drive_status(session=Depends(require_admin)):
    """Check Drive access with a read-only archive-folder request."""
    try:
        drive = DriveArchive()
        drive.verify_archive_folder()
    except ArchiveError as error:
        return {'connected': False, 'message': str(error), 'code': error.code}
    except Exception:
        return {'connected': False, 'message': 'Google Drive connection could not be checked. Reconnect it, then try again.'}
    return {'connected': True, 'message': 'Google Drive is connected and the archive folder is writable.'}


@router.get('/api/google-drive/connect')
def begin_google_drive_reconnect(request: WebRequest, session=Depends(require_admin)):
    """Start the Google OAuth flow; the user approves access on Google's page."""
    client_id, _, redirect_uri = _oauth_config()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode('ascii')
    query = urlencode({
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': _SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    })
    response = RedirectResponse('https://accounts.google.com/o/oauth2/v2/auth?' + query, status_code=302)
    for name, value in ((_STATE_COOKIE, state), (_VERIFIER_COOKIE, verifier)):
        response.set_cookie(name, value, max_age=_COOKIE_MAX_AGE, path=_COOKIE_PATH,
                            httponly=True, secure=_secure_cookie(request), samesite='lax')
    return response


def _exchange_code(code, verifier):
    client_id, client_secret, redirect_uri = _oauth_config()
    payload = urlencode({
        'code': code,
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': redirect_uri,
        'grant_type': 'authorization_code',
        'code_verifier': verifier,
    }).encode('utf-8')
    request = Request('https://oauth2.googleapis.com/token', data=payload,
                      headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urlopen(request, timeout=45) as response:
            token_response = json.load(response)
    except HTTPError as error:
        raise HTTPException(502, 'Google did not accept the authorization. Check the OAuth client and callback settings, then reconnect.') from error
    except (URLError, OSError, ValueError, TypeError):
        raise HTTPException(502, 'Google could not complete the authorization. Check the connection and try again.') from None
    refresh_token = token_response.get('refresh_token') if isinstance(token_response, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise HTTPException(502, 'Google did not return a new saved permission. Start Connect Google Drive again and approve access.')
    return refresh_token


@router.get('/api/google-drive/callback')
def complete_google_drive_reconnect(request: WebRequest, code: str | None = None,
                                   state: str | None = None, error: str | None = None,
                                   session=Depends(require_admin)):
    """Validate the browser-bound state, verify folder access, and save ciphertext."""
    response = _settings_redirect('failed')
    try:
        expected_state = request.cookies.get(_STATE_COOKIE, '')
        verifier = request.cookies.get(_VERIFIER_COOKIE, '')
        if not expected_state or not verifier or not state or not secrets.compare_digest(expected_state, state):
            raise HTTPException(400, 'Google authorization could not be matched to this browser. Start again from Settings.')
        if error:
            response = _settings_redirect('cancelled' if error == 'access_denied' else 'failed')
        elif not code:
            raise HTTPException(400, 'Google did not return an authorization code. Start again from Settings.')
        else:
            refresh_token = _exchange_code(code, verifier)
            drive = DriveArchive(refresh_token_override=refresh_token)
            drive.verify_archive_folder()
            save_refresh_token(refresh_token, drive.client_secret)
            response = _settings_redirect('connected')
    except HTTPException:
        response = _settings_redirect('failed')
    except ArchiveError as error:
        response = _settings_redirect('failed')
    except Exception:
        response = _settings_redirect('failed')
    _clear_oauth_cookies(response, request)
    return response
