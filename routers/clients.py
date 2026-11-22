# ==============================================
# 🏢 CLIENT MANAGEMENT ROUTER
# ==============================================

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from typing import Optional, List
from database import execute_query_pg, execute_write_pg, log_audit
from datetime import datetime
from routers.auth import require_admin
from services.storage_cleanup import (
    CleanupConfirmationError,
    DataPurgePreviewStaleError,
    DataPurgeProtectedError,
    PERMANENT_DATA_DELETE_CONFIRMATION,
    apply_client_data_purge,
    get_client_data_purge_preview,
)

router = APIRouter()

# ---------- DATA MODELS ----------
class ClientCreate(BaseModel):
    client_code: str
    client_name: str
    industry: Optional[str] = None
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None

class ClientUpdate(BaseModel):
    client_code: str
    client_name: str
    industry: Optional[str] = None
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    is_active: bool = True

class ClientResponse(BaseModel):
    id: int
    client_code: str
    client_name: str
    industry: Optional[str]
    contact_person: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    is_active: bool
    created_at: str


class ClientDataPurgeRequest(BaseModel):
    preview_hash: str
    confirmation: str
    template_id: Optional[int] = None
    include_linked_verification: bool = False

# ---------- HELPER ----------
def row_to_client(row: dict) -> dict:
    created_at = row["created_at"]
    return {
        "id": row["id"],
        "client_code": row["client_code"],
        "client_name": row["client_name"],
        "industry": row.get("industry"),
        "contact_person": row.get("contact_person"),
        "email": row.get("email"),
        "phone": row.get("phone"),
        "is_active": bool(row["is_active"]),
        # created_at is TIMESTAMPTZ on Postgres (a real datetime, not a
        # string) - ClientResponse.created_at is typed str, so normalize here.
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at
    }

# ---------- ENDPOINTS ----------

@router.get("/api/clients", response_model=List[ClientResponse])
def get_clients(
    response: Response,
    include_inactive: bool = False,
    search: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    Get clients, optionally filtered by is_active/search and paginated.
    - include_inactive=False (default): active clients only.
    - search: case-insensitive substring match against client_code/client_name/industry.
    - limit/offset: when limit is omitted (every caller before Fast Data Handling Phase 11 of 13,
      including the Admin Manage Clients page's reference-data cache), the full matching list is
      returned exactly as before, no X-Total-Count header. When limit is set, the result is
      paginated and X-Total-Count carries the total matching row count (Client Cards' Prev/Next
      pagination is the only caller that sets it today).
    """
    where_clauses = []
    params = []
    if not include_inactive:
        where_clauses.append("is_active = TRUE")
    if search:
        where_clauses.append("(client_code ILIKE %s OR client_name ILIKE %s OR industry ILIKE %s)")
        like = f"%{search}%"
        params.extend([like, like, like])
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    if limit is not None:
        total = execute_query_pg("SELECT COUNT(*) as cnt FROM clients" + where_sql, tuple(params))[0]["cnt"]
        response.headers["X-Total-Count"] = str(total)
        rows = execute_query_pg(
            "SELECT * FROM clients" + where_sql + " ORDER BY client_name LIMIT %s OFFSET %s",
            tuple(params) + (limit, offset)
        )
    else:
        rows = execute_query_pg("SELECT * FROM clients" + where_sql + " ORDER BY client_name", tuple(params))
    return [row_to_client(r) for r in rows]

@router.get("/api/clients/{client_id}", response_model=ClientResponse)
def get_client(client_id: int, session=Depends(require_admin)):
    rows = execute_query_pg("SELECT * FROM clients WHERE id = %s", (client_id,))
    if not rows:
        raise HTTPException(404, "Client not found")
    return row_to_client(rows[0])

@router.post("/api/clients", response_model=dict)
def create_client(client: ClientCreate, session=Depends(require_admin)):
    # Check for duplicate client_code
    existing = execute_query_pg("SELECT id FROM clients WHERE client_code = %s", (client.client_code,))
    if existing:
        raise HTTPException(400, f"Client code '{client.client_code}' already exists")

    client_id = execute_write_pg(
        """
        INSERT INTO clients (client_code, client_name, industry, contact_person, email, phone)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (client.client_code, client.client_name, client.industry,
         client.contact_person, client.email, client.phone)
    )
    log_audit("Admin", "Create", "Clients", f"Created client: {client.client_code} - {client.client_name}")
    return {"message": f"Client '{client.client_name}' created successfully", "client_id": client_id}

@router.put("/api/clients/{client_id}", response_model=dict)
def update_client(client_id: int, client: ClientUpdate, session=Depends(require_admin)):
    existing = execute_query_pg("SELECT id FROM clients WHERE id = %s", (client_id,))
    if not existing:
        raise HTTPException(404, "Client not found")

    # Check duplicate client_code for other clients
    dup = execute_query_pg("SELECT id FROM clients WHERE client_code = %s AND id != %s", (client.client_code, client_id))
    if dup:
        raise HTTPException(400, f"Client code '{client.client_code}' is already used by another client")

    execute_write_pg(
        """
        UPDATE clients
        SET client_code = %s, client_name = %s, industry = %s, contact_person = %s, email = %s, phone = %s, is_active = %s
        WHERE id = %s
        """,
        (client.client_code, client.client_name, client.industry,
         client.contact_person, client.email, client.phone,
         client.is_active, client_id)
    )
    log_audit("Admin", "Update", "Clients", f"Updated client ID {client_id}")
    return {"message": f"Client updated successfully"}

@router.delete("/api/clients/{client_id}", response_model=dict)
def delete_client(client_id: int, session=Depends(require_admin)):
    """
    Soft-delete a client (set is_active = FALSE).
    """
    existing = execute_query_pg("SELECT client_name FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not existing:
        raise HTTPException(404, "Active client not found")
    execute_write_pg("UPDATE clients SET is_active = FALSE WHERE id = %s", (client_id,))
    log_audit("Admin", "Delete", "Clients", f"Soft-deleted client: {existing[0]['client_name']} (ID: {client_id})")
    return {"message": f"Client deleted (soft-delete) successfully"}


@router.get("/api/clients/{client_id}/data-purge-preview")
def client_data_purge_preview(
    client_id: int,
    template_id: Optional[int] = None,
    include_linked_verification: bool = False,
    session=Depends(require_admin),
):
    """Return an exact, read-only preview for permanent client-data deletion."""
    try:
        return get_client_data_purge_preview(
            client_id, template_id, include_linked_verification
        )
    except DataPurgeProtectedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/api/clients/{client_id}/data-purge")
def client_data_purge(client_id: int, payload: ClientDataPurgeRequest, session=Depends(require_admin)):
    """Apply a freshly previewed permanent source-data deletion."""
    try:
        return apply_client_data_purge(
            preview_hash=payload.preview_hash,
            operator=session.get("username", "Unknown administrator"),
            confirmation=payload.confirmation,
            client_id=client_id,
            template_id=payload.template_id,
            include_linked_verification=payload.include_linked_verification,
        )
    except CleanupConfirmationError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{exc}. Required phrase: {PERMANENT_DATA_DELETE_CONFIRMATION}",
        ) from exc
    except DataPurgePreviewStaleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DataPurgeProtectedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

@router.post("/api/clients/{client_id}/restore", response_model=dict)
def restore_client(client_id: int, session=Depends(require_admin)):
    """
    Restore a soft-deleted client.
    """
    existing = execute_query_pg("SELECT client_name FROM clients WHERE id = %s AND is_active = FALSE", (client_id,))
    if not existing:
        raise HTTPException(404, "Inactive client not found")
    execute_write_pg("UPDATE clients SET is_active = TRUE WHERE id = %s", (client_id,))
    log_audit("Admin", "Restore", "Clients", f"Restored client: {existing[0]['client_name']} (ID: {client_id})")
    return {"message": f"Client restored successfully"}
