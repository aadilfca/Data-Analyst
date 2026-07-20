# ==============================================
# 📐 AUDIT RULES ROUTER
# ==============================================
# Phase 2 of 9 added Documents CRUD; Phase 3 of 9 added Cross-checks CRUD - the first
# two of the three reusable entity types (Documents / Cross-checks / Categories) in
# the Audit Rules module. Categories get their own CRUD in Phase 4; its junction
# tables exist from Phase 1 but stay unused until then.

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


# ==============================================
# CROSS-CHECKS
# ==============================================
# Phase 3 of 9: the second reusable entity type. A Cross-check references an ordered
# list of Documents and compares one field across them using a fixed comparison type.

# Mirrors the CHECK constraint on audit_rule_cross_checks.comparison_type - validated here
# too so a bad value returns a clean 400, not a raw SQLite CHECK-constraint 500.
COMPARISON_TYPES = ('All equal', 'Sequential increasing', 'Sequential decreasing', 'Within tolerance %')

# ---------- DATA MODELS ----------
class CrossCheckCreate(BaseModel):
    name: str
    compare_field: str
    comparison_type: str
    document_ids: List[int] = []

class CrossCheckUpdate(BaseModel):
    name: str
    compare_field: str
    comparison_type: str
    document_ids: List[int] = []

# ---------- HELPERS ----------
def get_cross_check_documents(cross_check_id: int) -> List[dict]:
    rows = execute_query(
        """
        SELECT d.id as document_id, d.name
        FROM audit_rule_cross_check_documents ccd
        JOIN audit_rule_documents d ON ccd.document_id = d.id
        WHERE ccd.cross_check_id = ?
        ORDER BY ccd.sort_order
        """,
        (cross_check_id,)
    )
    return [{"document_id": r["document_id"], "name": r["name"]} for r in rows]

def get_cross_check_used_by_count(cross_check_id: int) -> int:
    rows = execute_query(
        "SELECT COUNT(*) as cnt FROM audit_rule_category_cross_checks WHERE cross_check_id = ?",
        (cross_check_id,)
    )
    return rows[0]["cnt"] if rows else 0

def row_to_cross_check(row: dict) -> dict:
    cross_check_id = row["id"]
    return {
        "id": cross_check_id,
        "name": row["name"],
        "compare_field": row["compare_field"],
        "comparison_type": row["comparison_type"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "documents": get_cross_check_documents(cross_check_id),
        "used_by_categories_count": get_cross_check_used_by_count(cross_check_id)
    }

def save_cross_check_documents(cross_check_id: int, document_ids: List[int]):
    """Replace a Cross-check's ordered document list (delete-then-reinsert)."""
    execute_write("DELETE FROM audit_rule_cross_check_documents WHERE cross_check_id = ?", (cross_check_id,))
    for idx, document_id in enumerate(document_ids):
        execute_write(
            "INSERT INTO audit_rule_cross_check_documents (cross_check_id, document_id, sort_order) VALUES (?, ?, ?)",
            (cross_check_id, document_id, idx)
        )

def validate_cross_check_payload(compare_field: str, comparison_type: str, document_ids: List[int]):
    """
    Raises HTTPException(400) if comparison_type isn't one of the fixed set, or if
    compare_field isn't a cross-check-eligible field on every selected Document.
    SQLite foreign keys aren't enforced on this app's connections (see get_db()), so
    Document existence is also checked here rather than left to the database.
    """
    if comparison_type not in COMPARISON_TYPES:
        raise HTTPException(400, f"Invalid comparison type '{comparison_type}'. Must be one of: {', '.join(COMPARISON_TYPES)}")

    for document_id in document_ids:
        doc_rows = execute_query("SELECT name FROM audit_rule_documents WHERE id = ?", (document_id,))
        if not doc_rows:
            raise HTTPException(400, f"Document ID {document_id} not found")
        doc_fields = get_document_cross_check_fields(document_id)
        if compare_field not in doc_fields:
            raise HTTPException(
                400,
                f"Field '{compare_field}' is not a cross-check-eligible field on Document '{doc_rows[0]['name']}' (ID {document_id})"
            )

# ---------- ENDPOINTS ----------

@router.get("/api/audit-rules/cross-checks")
def get_cross_checks(include_inactive: bool = False):
    """
    Get all Cross-checks. By default, only active Cross-checks are returned.
    Set include_inactive=True to get all.
    """
    query = "SELECT * FROM audit_rule_cross_checks"
    if not include_inactive:
        query += " WHERE is_active = 1"
    query += " ORDER BY name"
    rows = execute_query(query)
    return [row_to_cross_check(r) for r in rows]

@router.get("/api/audit-rules/cross-checks/{cross_check_id}")
def get_cross_check(cross_check_id: int):
    rows = execute_query("SELECT * FROM audit_rule_cross_checks WHERE id = ?", (cross_check_id,))
    if not rows:
        raise HTTPException(404, "Cross-check not found")
    return row_to_cross_check(rows[0])

@router.post("/api/audit-rules/cross-checks")
def create_cross_check(cross_check: CrossCheckCreate):
    validate_cross_check_payload(cross_check.compare_field, cross_check.comparison_type, cross_check.document_ids)

    cross_check_id = execute_write(
        "INSERT INTO audit_rule_cross_checks (name, compare_field, comparison_type) VALUES (?, ?, ?)",
        (cross_check.name, cross_check.compare_field, cross_check.comparison_type)
    )
    save_cross_check_documents(cross_check_id, cross_check.document_ids)

    log_audit("Admin", "Create", "AuditRules", f"Created Cross-check: {cross_check.name}")
    return {"message": f"Cross-check '{cross_check.name}' created successfully", "cross_check_id": cross_check_id}

@router.put("/api/audit-rules/cross-checks/{cross_check_id}")
def update_cross_check(cross_check_id: int, cross_check: CrossCheckUpdate):
    existing = execute_query("SELECT id FROM audit_rule_cross_checks WHERE id = ?", (cross_check_id,))
    if not existing:
        raise HTTPException(404, "Cross-check not found")

    validate_cross_check_payload(cross_check.compare_field, cross_check.comparison_type, cross_check.document_ids)

    execute_write(
        "UPDATE audit_rule_cross_checks SET name = ?, compare_field = ?, comparison_type = ? WHERE id = ?",
        (cross_check.name, cross_check.compare_field, cross_check.comparison_type, cross_check_id)
    )
    save_cross_check_documents(cross_check_id, cross_check.document_ids)

    log_audit("Admin", "Update", "AuditRules", f"Updated Cross-check ID {cross_check_id}")
    return {"message": "Cross-check updated successfully"}

@router.delete("/api/audit-rules/cross-checks/{cross_check_id}")
def delete_cross_check(cross_check_id: int):
    """
    Soft-delete a Cross-check (set is_active = 0).
    """
    existing = execute_query("SELECT name FROM audit_rule_cross_checks WHERE id = ? AND is_active = 1", (cross_check_id,))
    if not existing:
        raise HTTPException(404, "Active Cross-check not found")
    execute_write("UPDATE audit_rule_cross_checks SET is_active = 0 WHERE id = ?", (cross_check_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Soft-deleted Cross-check: {existing[0]['name']} (ID: {cross_check_id})")
    return {"message": "Cross-check deleted (soft-delete) successfully"}

@router.post("/api/audit-rules/cross-checks/{cross_check_id}/restore")
def restore_cross_check(cross_check_id: int):
    """
    Restore a soft-deleted Cross-check.
    """
    existing = execute_query("SELECT name FROM audit_rule_cross_checks WHERE id = ? AND is_active = 0", (cross_check_id,))
    if not existing:
        raise HTTPException(404, "Inactive Cross-check not found")
    execute_write("UPDATE audit_rule_cross_checks SET is_active = 1 WHERE id = ?", (cross_check_id,))
    log_audit("Admin", "Restore", "AuditRules", f"Restored Cross-check: {existing[0]['name']} (ID: {cross_check_id})")
    return {"message": "Cross-check restored successfully"}
