"""Administrator-only storage cleanup preview API."""

from fastapi import APIRouter, Depends, HTTPException

from routers.auth import require_admin
from services.storage_cleanup import get_storage_cleanup_preview


router = APIRouter()


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
