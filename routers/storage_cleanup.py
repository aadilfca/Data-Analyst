"""Administrator-only storage cleanup preview and apply API."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from routers.auth import require_admin
from services.storage_cleanup import (
    CleanupConfirmationError,
    CleanupPreviewStaleError,
    IndexMaintenanceBlockedError,
    apply_storage_cleanup,
    apply_index_maintenance,
    apply_validation_plan_cleanup,
    compact_storage,
    get_storage_cleanup_preview,
)


router = APIRouter()


class StorageCleanupApplyRequest(BaseModel):
    preview_hash: str
    confirmation: str


class StorageCompactionRequest(BaseModel):
    preview_hash: str
    confirmation: str
    full: bool = False


class StorageIndexMaintenanceRequest(BaseModel):
    preview_hash: str
    index_name: str
    confirmation: str
    operation: str = "drop"


class ValidationPlanCleanupRequest(BaseModel):
    preview_hash: str
    confirmation: str


@router.get("/api/storage-cleanup/preview")
def storage_cleanup_preview(session=Depends(require_admin)):
    """Return storage usage plus canonical-data protections and legacy-copy candidates."""
    try:
        return get_storage_cleanup_preview()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Storage preview is temporarily unavailable because database evidence could not be read; no cleanup or optimization action was performed.",
        ) from exc


@router.post("/api/storage-cleanup/apply")
def storage_cleanup_apply(payload: StorageCleanupApplyRequest, session=Depends(require_admin)):
    """Apply approved log, retired-data, and legacy Cashflows-copy cleanup safely."""
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


@router.post("/api/storage-cleanup/validation-plans/apply")
def storage_validation_plan_cleanup(
    payload: ValidationPlanCleanupRequest,
    session=Depends(require_admin),
):
    """Remove only completed validation plans, preserving audit and canonical data."""
    try:
        return apply_validation_plan_cleanup(
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
            detail="Validation-plan cleanup failed and was rolled back; no partial cleanup was committed.",
        ) from exc


@router.post("/api/storage-cleanup/compact")
def storage_cleanup_compact(payload: StorageCompactionRequest, session=Depends(require_admin)):
    """Compact allow-listed tables only after a fresh preview and confirmation."""
    try:
        return compact_storage(
            preview_hash=payload.preview_hash,
            operator=session.get("username", "Unknown administrator"),
            confirmation=payload.confirmation,
            full=payload.full,
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
            detail="Storage compaction failed; no rows were deleted.",
        ) from exc


@router.post("/api/storage-cleanup/index-maintenance")
def storage_cleanup_index_maintenance(
    payload: StorageIndexMaintenanceRequest,
    session=Depends(require_admin),
):
    """Remove one evidence-approved non-integrity index after fresh review."""
    try:
        return apply_index_maintenance(
            preview_hash=payload.preview_hash,
            index_name=payload.index_name,
            operator=session.get("username", "Unknown administrator"),
            confirmation=payload.confirmation,
            operation=payload.operation,
        )
    except CleanupConfirmationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (CleanupPreviewStaleError, IndexMaintenanceBlockedError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Index maintenance failed; review the database state before retrying.",
        ) from exc
