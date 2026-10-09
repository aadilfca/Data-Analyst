"""Administrator-only storage cleanup preview and apply API."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from routers.auth import require_admin
from services.storage_cleanup import (
    CleanupConfirmationError,
    CleanupPreviewStaleError,
    apply_storage_cleanup,
    get_storage_cleanup_preview,
)


router = APIRouter()


class StorageCleanupApplyRequest(BaseModel):
    preview_hash: str
    confirmation: str


@router.get("/api/storage-cleanup/preview")
def storage_cleanup_preview(session=Depends(require_admin)):
    """Return aggregate storage usage and protected/removable candidates."""
    try:
        return get_storage_cleanup_preview()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Storage preview is temporarily unavailable; no cleanup was performed.",
        ) from exc


@router.post("/api/storage-cleanup/apply")
def storage_cleanup_apply(payload: StorageCleanupApplyRequest, session=Depends(require_admin)):
    """Reject destructive cleanup while the backup/redesign gate is active."""
    raise HTTPException(status_code=423, detail='Storage cleanup is temporarily locked pending a verified database backup and current-only Cashflows redesign.')
    try:
        return apply_storage_cleanup(
            preview_hash=payload.preview_hash,
            operator=session.get("username", "Unknown administrator"),
            confirmation=payload.confirmation,
        )
    except CleanupConfirmationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CleanupPreviewStaleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Storage cleanup failed and was rolled back; no partial cleanup was committed.",
        ) from exc
