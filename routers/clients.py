# ==============================================
# 🏢 CLIENT MANAGEMENT ROUTER
# ==============================================

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from database import execute_query, execute_write, log_audit
from datetime import datetime

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

# ---------- HELPER ----------
def row_to_client(row: dict) -> dict:
    return {
        "id": row["id"],
        "client_code": row["client_code"],
        "client_name": row["client_name"],
        "industry": row.get("industry"),
        "contact_person": row.get("contact_person"),
        "email": row.get("email"),
        "phone": row.get("phone"),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"]
    }

# ---------- ENDPOINTS ----------

@router.get("/api/clients", response_model=List[ClientResponse])
def get_clients(include_inactive: bool = False):
    """
    Get all clients. By default, only active clients are returned.
    Set include_inactive=True to get all.
    """
    query = "SELECT * FROM clients"
    if not include_inactive:
        query += " WHERE is_active = 1"
    query += " ORDER BY client_name"
    rows = execute_query(query)
    return [row_to_client(r) for r in rows]

@router.get("/api/clients/{client_id}", response_model=ClientResponse)
def get_client(client_id: int):
    rows = execute_query("SELECT * FROM clients WHERE id = ?", (client_id,))
    if not rows:
        raise HTTPException(404, "Client not found")
    return row_to_client(rows[0])

@router.post("/api/clients", response_model=dict)
def create_client(client: ClientCreate):
    # Check for duplicate client_code
    existing = execute_query("SELECT id FROM clients WHERE client_code = ?", (client.client_code,))
    if existing:
        raise HTTPException(400, f"Client code '{client.client_code}' already exists")
    
    client_id = execute_write(
        """
        INSERT INTO clients (client_code, client_name, industry, contact_person, email, phone)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (client.client_code, client.client_name, client.industry,
         client.contact_person, client.email, client.phone)
    )
    log_audit("Admin", "Create", "Clients", f"Created client: {client.client_code} - {client.client_name}")
    return {"message": f"Client '{client.client_name}' created successfully", "client_id": client_id}

@router.put("/api/clients/{client_id}", response_model=dict)
def update_client(client_id: int, client: ClientUpdate):
    existing = execute_query("SELECT id FROM clients WHERE id = ?", (client_id,))
    if not existing:
        raise HTTPException(404, "Client not found")
    
    # Check duplicate client_code for other clients
    dup = execute_query("SELECT id FROM clients WHERE client_code = ? AND id != ?", (client.client_code, client_id))
    if dup:
        raise HTTPException(400, f"Client code '{client.client_code}' is already used by another client")
    
    execute_write(
        """
        UPDATE clients
        SET client_code = ?, client_name = ?, industry = ?, contact_person = ?, email = ?, phone = ?, is_active = ?
        WHERE id = ?
        """,
        (client.client_code, client.client_name, client.industry,
         client.contact_person, client.email, client.phone,
         1 if client.is_active else 0, client_id)
    )
    log_audit("Admin", "Update", "Clients", f"Updated client ID {client_id}")
    return {"message": f"Client updated successfully"}

@router.delete("/api/clients/{client_id}", response_model=dict)
def delete_client(client_id: int):
    """
    Soft-delete a client (set is_active = 0).
    """
    existing = execute_query("SELECT client_name FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not existing:
        raise HTTPException(404, "Active client not found")
    execute_write("UPDATE clients SET is_active = 0 WHERE id = ?", (client_id,))
    log_audit("Admin", "Delete", "Clients", f"Soft-deleted client: {existing[0]['client_name']} (ID: {client_id})")
    return {"message": f"Client deleted (soft-delete) successfully"}

@router.post("/api/clients/{client_id}/restore", response_model=dict)
def restore_client(client_id: int):
    """
    Restore a soft-deleted client.
    """
    existing = execute_query("SELECT client_name FROM clients WHERE id = ? AND is_active = 0", (client_id,))
    if not existing:
        raise HTTPException(404, "Inactive client not found")
    execute_write("UPDATE clients SET is_active = 1 WHERE id = ?", (client_id,))
    log_audit("Admin", "Restore", "Clients", f"Restored client: {existing[0]['client_name']} (ID: {client_id})")
    return {"message": f"Client restored successfully"}