# ==============================================
# 📐 AUDIT RULES ROUTER
# ==============================================
# Phase 2 of 9: Documents CRUD - the first of the three reusable entity types
# (Documents / Cross-checks / Categories) in the Audit Rules module. Cross-checks
# and Categories get their own CRUD in later phases; their junction tables exist
# from Phase 1 but stay unused until then.

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from database import execute_query, execute_write, log_audit

router = APIRouter()

# ---------- DATA MODELS ----------
class DocumentCreate(BaseModel):
    name: str
    kb_note: Optional[str] = None
    auth_roles: List[str] = []
    cross_check_fields: List[str] = []

class DocumentUpdate(BaseModel):
    name: str
    kb_note: Optional[str] = None
    auth_roles: List[str] = []
    cross_check_fields: List[str] = []

# ---------- HELPERS ----------
def get_document_auth_roles(document_id: int) -> List[str]:
    rows = execute_query(
        "SELECT role_name FROM audit_rule_document_auth_roles WHERE document_id = ? ORDER BY sort_order",
        (document_id,)
    )
    return [r["role_name"] for r in rows]

def get_document_cross_check_fields(document_id: int) -> List[str]:
    rows = execute_query(
        "SELECT field_name FROM audit_rule_document_fields WHERE document_id = ? ORDER BY sort_order",
        (document_id,)
    )
    return [r["field_name"] for r in rows]

def get_document_used_by_count(document_id: int) -> int:
    rows = execute_query(
        "SELECT COUNT(*) as cnt FROM audit_rule_category_documents WHERE document_id = ?",
        (document_id,)
    )
    return rows[0]["cnt"] if rows else 0

def row_to_document(row: dict) -> dict:
    document_id = row["id"]
    return {
        "id": document_id,
        "name": row["name"],
        "kb_note": row.get("kb_note"),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "auth_roles": get_document_auth_roles(document_id),
        "cross_check_fields": get_document_cross_check_fields(document_id),
        "used_by_categories_count": get_document_used_by_count(document_id)
    }

def save_document_auth_roles(document_id: int, auth_roles: List[str]):
    """Replace a Document's authorisation-roles list (delete-then-reinsert)."""
    execute_write("DELETE FROM audit_rule_document_auth_roles WHERE document_id = ?", (document_id,))
    for idx, role_name in enumerate(auth_roles):
        execute_write(
            "INSERT INTO audit_rule_document_auth_roles (document_id, role_name, sort_order) VALUES (?, ?, ?)",
            (document_id, role_name, idx)
        )

def save_document_cross_check_fields(document_id: int, cross_check_fields: List[str]):
    """Replace a Document's cross-check-eligible fields list (delete-then-reinsert)."""
    execute_write("DELETE FROM audit_rule_document_fields WHERE document_id = ?", (document_id,))
    for idx, field_name in enumerate(cross_check_fields):
        execute_write(
            "INSERT INTO audit_rule_document_fields (document_id, field_name, sort_order) VALUES (?, ?, ?)",
            (document_id, field_name, idx)
        )

# ---------- ENDPOINTS ----------

@router.get("/api/audit-rules/documents")
def get_documents(include_inactive: bool = False):
    """
    Get all Documents. By default, only active Documents are returned.
    Set include_inactive=True to get all.
    """
    query = "SELECT * FROM audit_rule_documents"
    if not include_inactive:
        query += " WHERE is_active = 1"
    query += " ORDER BY name"
    rows = execute_query(query)
    return [row_to_document(r) for r in rows]

@router.get("/api/audit-rules/documents/{document_id}")
def get_document(document_id: int):
    rows = execute_query("SELECT * FROM audit_rule_documents WHERE id = ?", (document_id,))
    if not rows:
        raise HTTPException(404, "Document not found")
    return row_to_document(rows[0])

@router.post("/api/audit-rules/documents")
def create_document(document: DocumentCreate):
    document_id = execute_write(
        "INSERT INTO audit_rule_documents (name, kb_note) VALUES (?, ?)",
        (document.name, document.kb_note)
    )
    save_document_auth_roles(document_id, document.auth_roles)
    save_document_cross_check_fields(document_id, document.cross_check_fields)

    log_audit("Admin", "Create", "AuditRules", f"Created Document: {document.name}")
    return {"message": f"Document '{document.name}' created successfully", "document_id": document_id}

@router.put("/api/audit-rules/documents/{document_id}")
def update_document(document_id: int, document: DocumentUpdate):
    existing = execute_query("SELECT id FROM audit_rule_documents WHERE id = ?", (document_id,))
    if not existing:
        raise HTTPException(404, "Document not found")

    execute_write(
        "UPDATE audit_rule_documents SET name = ?, kb_note = ? WHERE id = ?",
        (document.name, document.kb_note, document_id)
    )
    save_document_auth_roles(document_id, document.auth_roles)
    save_document_cross_check_fields(document_id, document.cross_check_fields)

    log_audit("Admin", "Update", "AuditRules", f"Updated Document ID {document_id}")
    return {"message": "Document updated successfully"}

@router.delete("/api/audit-rules/documents/{document_id}")
def delete_document(document_id: int):
    """
    Soft-delete a Document (set is_active = 0).
    """
    existing = execute_query("SELECT name FROM audit_rule_documents WHERE id = ? AND is_active = 1", (document_id,))
    if not existing:
        raise HTTPException(404, "Active Document not found")
    execute_write("UPDATE audit_rule_documents SET is_active = 0 WHERE id = ?", (document_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Soft-deleted Document: {existing[0]['name']} (ID: {document_id})")
    return {"message": "Document deleted (soft-delete) successfully"}

@router.post("/api/audit-rules/documents/{document_id}/restore")
def restore_document(document_id: int):
    """
    Restore a soft-deleted Document.
    """
    existing = execute_query("SELECT name FROM audit_rule_documents WHERE id = ? AND is_active = 0", (document_id,))
    if not existing:
        raise HTTPException(404, "Inactive Document not found")
    execute_write("UPDATE audit_rule_documents SET is_active = 1 WHERE id = ?", (document_id,))
    log_audit("Admin", "Restore", "AuditRules", f"Restored Document: {existing[0]['name']} (ID: {document_id})")
    return {"message": "Document restored successfully"}
