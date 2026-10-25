"""Administrator-only local backup action. Deliberately has no restore endpoint."""
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from routers.auth import require_admin
from services import database_backup as backup

router = APIRouter(prefix='/api/database-backup')


class BackupRequest(BaseModel):
    operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')


def _local_request(request, *, mutation=False):
    host = request.url.hostname
    try:
        peer_is_local = ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        peer_is_local = False
    if not backup.local_runtime() or not peer_is_local or host not in ('localhost', '127.0.0.1', '::1'):
        raise HTTPException(404, 'Local backup is unavailable here.')
    if mutation:
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'Open this backup action from the local application.')


def _owner(session):
    return str(session['user_id'])


@router.get('/status')
def backup_status(request: Request, operation_id: str | None = None, session=Depends(require_admin)):
    _local_request(request)
    try:
        return backup.status(_owner(session), operation_id)
    except backup.BackupError as error:
        raise HTTPException(error.status, error.detail) from None
    except OSError:
        raise HTTPException(503, 'Local backup storage is unavailable.') from None
    except Exception:
        raise HTTPException(503, 'Backup status could not be read. Existing files were retained.') from None


@router.post('/create')
def create_database_backup(payload: BackupRequest, request: Request, session=Depends(require_admin)):
    _local_request(request, mutation=True)
    try:
        return backup.create_backup(payload.operation_id, _owner(session))
    except backup.BackupError as error:
        return JSONResponse(status_code=error.status, content={
            'detail': error.detail, 'backup': error.backup, 'retry_same': error.retry_same,
        })
    except OSError:
        return JSONResponse(status_code=503, content={
            'detail': 'Local backup storage is unavailable. Existing database data and backup files were not replaced.',
            'retry_same': True,
        })
    except Exception:
        # Avoid global error logging of native/credential details; uncertain POSTs
        # retain their reference so a caller checks status before creating another.
        return JSONResponse(status_code=503, content={
            'detail': 'The backup could not be completed. Check its status before retrying; existing files were retained.',
            'retry_same': True,
        })
