# ==============================================
# 📐 AUDIT RULES ROUTER
# ==============================================
# Phase 2 of 9 added Documents CRUD; Phase 3 of 9 added Cross-checks CRUD; Phase 4 of 9
# added Categories CRUD - all three reusable entity types (Documents / Cross-checks /
# Categories) in the Audit Rules module now have CRUD. Categories is the entity a
# voucher will eventually reference (wired up in Phase 9).

import sqlite3
from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel
from typing import Optional, List
from database import execute_query, execute_write, log_audit

router = APIRouter()

# ---------- DATA MODELS ----------
class DocumentCreate(BaseModel):
    name: str
    kb_note: Optional[str] = None
    # Phase 9: which Data Bank template this Document's supporting evidence should be
    # searched in (verification.js's Supporting column needs this per-Document).
    template_id: Optional[int] = None
    # Audit Risk indicator (Supporting Documents modal redesign, final phase): this Document's
    # risk rating, surfaced on the main voucher table's Risk column.
    risk_rating: Optional[str] = None
    auth_roles: List[str] = []
    cross_check_fields: List[str] = []

class DocumentUpdate(BaseModel):
    name: str
    kb_note: Optional[str] = None
    template_id: Optional[int] = None
    risk_rating: Optional[str] = None
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
        "template_id": row.get("template_id"),
        "risk_rating": row.get("risk_rating"),
        "transaction_level_approval": bool(row.get("transaction_level_approval") or 0),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "auth_roles": get_document_auth_roles(document_id),
        "cross_check_fields": get_document_cross_check_fields(document_id),
        "used_by_categories_count": get_document_used_by_count(document_id)
    }

def validate_document_template_id(template_id: Optional[int]):
    """Raises HTTPException(400) if template_id is set but doesn't reference an active template."""
    if template_id is None:
        return
    rows = execute_query("SELECT id FROM templates WHERE id = ? AND is_active = 1", (template_id,))
    if not rows:
        raise HTTPException(400, f"Template ID {template_id} not found or inactive")

# Fixed rating scale for a Document's Audit Risk indicator (Supporting Documents modal redesign,
# final phase) - validated here since risk_rating has no DB-level CHECK constraint (see database.py).
RISK_RATINGS = ('Low', 'Medium', 'High')

def validate_document_risk_rating(risk_rating: Optional[str]):
    """Raises HTTPException(400) if risk_rating is set but isn't one of the fixed RISK_RATINGS."""
    if risk_rating is None:
        return
    if risk_rating not in RISK_RATINGS:
        raise HTTPException(400, f"Invalid risk rating '{risk_rating}'. Must be one of: {', '.join(RISK_RATINGS)}")

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
    validate_document_template_id(document.template_id)
    validate_document_risk_rating(document.risk_rating)
    document_id = execute_write(
        "INSERT INTO audit_rule_documents (name, kb_note, template_id, risk_rating) VALUES (?, ?, ?, ?)",
        (document.name, document.kb_note, document.template_id, document.risk_rating)
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
    validate_document_template_id(document.template_id)
    validate_document_risk_rating(document.risk_rating)

    execute_write(
        "UPDATE audit_rule_documents SET name = ?, kb_note = ?, template_id = ?, risk_rating = ? WHERE id = ?",
        (document.name, document.kb_note, document.template_id, document.risk_rating, document_id)
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


# ==============================================
# CATEGORIES
# ==============================================
# Phase 4 of 9: the third and final reusable entity type. A Category assembles a list
# of Documents and a list of Cross-checks by reference. This is the entity a voucher
# will eventually reference (wired up in Phase 9).

# ---------- DATA MODELS ----------
class CategoryCreate(BaseModel):
    name: str
    kb_overview: Optional[str] = None
    document_ids: List[int] = []
    cross_check_ids: List[int] = []

class CategoryUpdate(BaseModel):
    name: str
    kb_overview: Optional[str] = None
    document_ids: List[int] = []
    cross_check_ids: List[int] = []

# ---------- HELPERS ----------
def get_category_documents(category_id: int) -> List[dict]:
    rows = execute_query(
        """
        SELECT d.id as document_id, d.name, cd.approval_required, cd.is_required
        FROM audit_rule_category_documents cd
        JOIN audit_rule_documents d ON cd.document_id = d.id
        WHERE cd.category_id = ?
        ORDER BY cd.sort_order
        """,
        (category_id,)
    )
    return [
        {
            "document_id": r["document_id"],
            "name": r["name"],
            "approval_required": bool(r["approval_required"]),
            # Automated cross-document validation engine, Phase 4 of 10: whether this
            # Document must be present for the Category to be Cleared. Not yet read by
            # compute_and_persist_cleared_status() - that wiring is Phase 7's job, deliberately
            # deferred so this can be configured in advance with no effect on live vouchers yet.
            "is_required": bool(r["is_required"])
        }
        for r in rows
    ]

def get_category_document_approvals(category_id: int) -> dict:
    """Return {document_id: approval_required (0/1)} for a Category's current document list."""
    rows = execute_query(
        "SELECT document_id, approval_required FROM audit_rule_category_documents WHERE category_id = ?",
        (category_id,)
    )
    return {r["document_id"]: r["approval_required"] for r in rows}

def get_category_document_requireds(category_id: int) -> dict:
    """Return {document_id: is_required (0/1)} for a Category's current document list."""
    rows = execute_query(
        "SELECT document_id, is_required FROM audit_rule_category_documents WHERE category_id = ?",
        (category_id,)
    )
    return {r["document_id"]: r["is_required"] for r in rows}

def get_category_cross_checks(category_id: int) -> List[dict]:
    rows = execute_query(
        """
        SELECT c.id as cross_check_id, c.name
        FROM audit_rule_category_cross_checks cc
        JOIN audit_rule_cross_checks c ON cc.cross_check_id = c.id
        WHERE cc.category_id = ?
        ORDER BY cc.sort_order
        """,
        (category_id,)
    )
    return [{"cross_check_id": r["cross_check_id"], "name": r["name"]} for r in rows]

# Phase 9: fuller Document/Cross-check shapes for verification.js's audit-guidance panel and
# Supporting column - the Categories CRUD response only needs id/name (above), but the
# verification module needs template_id/kb_note/roles/fields per Document and
# compare_field/comparison_type/document-names per Cross-check. New, additive functions -
# get_category_documents()/get_category_cross_checks() above (used by the Categories CRUD
# response) are untouched.
def get_category_guidance_documents(category_id: int) -> List[dict]:
    rows = execute_query(
        """
        SELECT d.id as document_id, d.name, d.kb_note, d.template_id, d.risk_rating, d.transaction_level_approval
        FROM audit_rule_category_documents cd
        JOIN audit_rule_documents d ON cd.document_id = d.id
        WHERE cd.category_id = ?
        ORDER BY cd.sort_order
        """,
        (category_id,)
    )
    return [
        {
            "id": r["document_id"],
            "name": r["name"],
            "kb_note": r["kb_note"],
            "template_id": r["template_id"],
            "risk_rating": r["risk_rating"],
            "transaction_level_approval": bool(r["transaction_level_approval"]),
            "auth_roles": get_document_auth_roles(r["document_id"]),
            "cross_check_fields": get_document_cross_check_fields(r["document_id"])
        }
        for r in rows
    ]

def get_category_guidance_cross_checks(category_id: int) -> List[dict]:
    rows = execute_query(
        """
        SELECT c.id as cross_check_id, c.name, c.compare_field, c.comparison_type
        FROM audit_rule_category_cross_checks cc
        JOIN audit_rule_cross_checks c ON cc.cross_check_id = c.id
        WHERE cc.category_id = ?
        ORDER BY cc.sort_order
        """,
        (category_id,)
    )
    result = []
    for r in rows:
        doc_rows = execute_query(
            """
            SELECT d.name
            FROM audit_rule_cross_check_documents ccd
            JOIN audit_rule_documents d ON ccd.document_id = d.id
            WHERE ccd.cross_check_id = ?
            ORDER BY ccd.sort_order
            """,
            (r["cross_check_id"],)
        )
        result.append({
            "id": r["cross_check_id"],
            "name": r["name"],
            "compare_field": r["compare_field"],
            "comparison_type": r["comparison_type"],
            "documents": [dr["name"] for dr in doc_rows]
        })
    return result

def get_category_used_by_vouchers_count(category_id: int) -> int:
    """
    Placeholder - no voucher/client_data column references a Category yet (that link is
    Phase 9's job). Returns 0 unconditionally so the response shape is stable now and the
    frontend (Phase 7) doesn't need a later API contract change; replace with a real
    COUNT(*) query once Phase 9 adds the voucher-side category reference.
    """
    return 0

def row_to_category(row: dict) -> dict:
    category_id = row["id"]
    return {
        "id": category_id,
        "name": row["name"],
        "kb_overview": row.get("kb_overview"),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        # Validation Rules (Phase 2 of 7): bumped by bump_category_matrix_version() whenever
        # this Category's document list or approval-required flags change.
        "matrix_version": row.get("matrix_version", 1),
        "documents": get_category_documents(category_id),
        "cross_checks": get_category_cross_checks(category_id),
        "used_by_vouchers_count": get_category_used_by_vouchers_count(category_id)
    }

def save_category_documents(category_id: int, document_ids: List[int]):
    """
    Replace a Category's Document list (delete-then-reinsert). Carries each surviving
    document's approval_required value forward across the delete-then-reinsert (Validation
    Rules, Phase 2 of 7) - without this, any ordinary document-list edit would silently
    reset every document's approval flag back to 0. A newly-added document defaults to 0,
    matching the column's own DB default. Same carry-forward now applies to is_required
    (Phase 4 of 10) - a newly-added document defaults to 1, matching that column's own DB
    default (TRUE), so a plain document-list edit never silently makes anything optional.
    """
    existing_approvals = get_category_document_approvals(category_id)
    existing_requireds = get_category_document_requireds(category_id)
    execute_write("DELETE FROM audit_rule_category_documents WHERE category_id = ?", (category_id,))
    for idx, document_id in enumerate(document_ids):
        approval_required = existing_approvals.get(document_id, 0)
        is_required = existing_requireds.get(document_id, 1)
        execute_write(
            "INSERT INTO audit_rule_category_documents (category_id, document_id, sort_order, approval_required, is_required) VALUES (?, ?, ?, ?, ?)",
            (category_id, document_id, idx, approval_required, is_required)
        )

def bump_category_matrix_version(category_id: int):
    """
    Increment audit_rule_categories.matrix_version (Validation Rules, Phase 2 of 7) - called
    automatically whenever a Category's document list or approval-required flags change, never
    as a separate manual action. Lets a later phase's verification_details.matrix_version_applied
    detect a stale classification without silently reclassifying an already-Cleared voucher.
    """
    execute_write(
        "UPDATE audit_rule_categories SET matrix_version = matrix_version + 1 WHERE id = ?",
        (category_id,)
    )

def save_category_cross_checks(category_id: int, cross_check_ids: List[int]):
    """Replace a Category's Cross-check list (delete-then-reinsert)."""
    execute_write("DELETE FROM audit_rule_category_cross_checks WHERE category_id = ?", (category_id,))
    for idx, cross_check_id in enumerate(cross_check_ids):
        execute_write(
            "INSERT INTO audit_rule_category_cross_checks (category_id, cross_check_id, sort_order) VALUES (?, ?, ?)",
            (category_id, cross_check_id, idx)
        )

def validate_category_payload(document_ids: List[int], cross_check_ids: List[int]):
    """
    Raises HTTPException(400) if any document_id/cross_check_id doesn't exist (SQLite
    foreign keys aren't enforced on this app's connections, see get_db()), or if any
    selected Cross-check references a Document that isn't also in this Category's own
    document_ids list.
    """
    doc_id_set = set()
    for document_id in document_ids:
        rows = execute_query("SELECT id FROM audit_rule_documents WHERE id = ?", (document_id,))
        if not rows:
            raise HTTPException(400, f"Document ID {document_id} not found")
        doc_id_set.add(document_id)

    for cross_check_id in cross_check_ids:
        cc_rows = execute_query("SELECT name FROM audit_rule_cross_checks WHERE id = ?", (cross_check_id,))
        if not cc_rows:
            raise HTTPException(400, f"Cross-check ID {cross_check_id} not found")
        cc_doc_rows = execute_query(
            "SELECT document_id FROM audit_rule_cross_check_documents WHERE cross_check_id = ?",
            (cross_check_id,)
        )
        cc_doc_ids = [r["document_id"] for r in cc_doc_rows]
        missing = [d for d in cc_doc_ids if d not in doc_id_set]
        if missing:
            raise HTTPException(
                400,
                f"Cross-check '{cc_rows[0]['name']}' (ID {cross_check_id}) references Document ID(s) "
                f"{missing} that are not included in this Category's Document list"
            )

# ---------- ENDPOINTS ----------

@router.get("/api/audit-rules/categories")
def get_categories(include_inactive: bool = False):
    """
    Get all Categories. By default, only active Categories are returned.
    Set include_inactive=True to get all.
    """
    query = "SELECT * FROM audit_rule_categories"
    if not include_inactive:
        query += " WHERE is_active = 1"
    query += " ORDER BY name"
    rows = execute_query(query)
    return [row_to_category(r) for r in rows]

@router.get("/api/audit-rules/categories/{category_id}")
def get_category(category_id: int):
    rows = execute_query("SELECT * FROM audit_rule_categories WHERE id = ?", (category_id,))
    if not rows:
        raise HTTPException(404, "Category not found")
    return row_to_category(rows[0])

@router.post("/api/audit-rules/categories")
def create_category(category: CategoryCreate):
    validate_category_payload(category.document_ids, category.cross_check_ids)

    category_id = execute_write(
        "INSERT INTO audit_rule_categories (name, kb_overview) VALUES (?, ?)",
        (category.name, category.kb_overview)
    )
    save_category_documents(category_id, category.document_ids)
    save_category_cross_checks(category_id, category.cross_check_ids)

    log_audit("Admin", "Create", "AuditRules", f"Created Category: {category.name}")
    return {"message": f"Category '{category.name}' created successfully", "category_id": category_id}

@router.put("/api/audit-rules/categories/{category_id}")
def update_category(category_id: int, category: CategoryUpdate):
    existing = execute_query("SELECT id FROM audit_rule_categories WHERE id = ?", (category_id,))
    if not existing:
        raise HTTPException(404, "Category not found")

    validate_category_payload(category.document_ids, category.cross_check_ids)

    # Validation Rules (Phase 2 of 7): capture the document set before it's replaced, so a
    # real membership change (add/remove - reordering alone doesn't count) can trigger a
    # matrix_version bump below. Cross-check changes deliberately do not bump the version.
    old_document_ids = set(get_category_document_approvals(category_id).keys())

    execute_write(
        "UPDATE audit_rule_categories SET name = ?, kb_overview = ? WHERE id = ?",
        (category.name, category.kb_overview, category_id)
    )
    save_category_documents(category_id, category.document_ids)
    save_category_cross_checks(category_id, category.cross_check_ids)

    if old_document_ids != set(category.document_ids):
        bump_category_matrix_version(category_id)

    log_audit("Admin", "Update", "AuditRules", f"Updated Category ID {category_id}")
    return {"message": "Category updated successfully"}

@router.delete("/api/audit-rules/categories/{category_id}")
def delete_category(category_id: int):
    """
    Soft-delete a Category (set is_active = 0).
    """
    existing = execute_query("SELECT name FROM audit_rule_categories WHERE id = ? AND is_active = 1", (category_id,))
    if not existing:
        raise HTTPException(404, "Active Category not found")
    execute_write("UPDATE audit_rule_categories SET is_active = 0 WHERE id = ?", (category_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Soft-deleted Category: {existing[0]['name']} (ID: {category_id})")
    return {"message": "Category deleted (soft-delete) successfully"}

@router.post("/api/audit-rules/categories/{category_id}/restore")
def restore_category(category_id: int):
    """
    Restore a soft-deleted Category.
    """
    existing = execute_query("SELECT name FROM audit_rule_categories WHERE id = ? AND is_active = 0", (category_id,))
    if not existing:
        raise HTTPException(404, "Inactive Category not found")
    execute_write("UPDATE audit_rule_categories SET is_active = 1 WHERE id = ?", (category_id,))
    log_audit("Admin", "Restore", "AuditRules", f"Restored Category: {existing[0]['name']} (ID: {category_id})")
    return {"message": "Category restored successfully"}


# ==============================================
# VOUCHER CHECKS
# ==============================================
# Generic Voucher-Level Checks (new, confirmed 6-phase requirement), Phase 2 of 6: CRUD for
# check definitions, replacing the hardcoded Prepared/Checked/Approved-By concept
# (voucher_approvals, untouched by this phase) with a data-driven, extensible list of global
# checks. Mirrors Documents' 6-endpoint soft-delete/restore CRUD pattern exactly, plus a
# reorder endpoint (this is the first entity in this file with a user-orderable sort_order on
# the entity itself, rather than only on its child rows) modeled on
# routers/templates.py's POST /api/templates/{template_id}/reorder.

# ---------- DATA MODELS ----------
class VoucherCheckCreate(BaseModel):
    name: str

class VoucherCheckUpdate(BaseModel):
    name: str

# ---------- HELPERS ----------
# Confirmed requirement: Voucher-level Checks are now name-only (no Authorisation Roles
# field). audit_rule_voucher_check_auth_roles is left in place, unread/unwritten by this
# section going forward (non-destructive-schema convention) - these two helpers are kept
# defined but no longer called from the CRUD endpoints below.
def get_voucher_check_auth_roles(voucher_check_id: int) -> List[str]:
    rows = execute_query(
        "SELECT role_name FROM audit_rule_voucher_check_auth_roles WHERE voucher_check_id = ? ORDER BY sort_order",
        (voucher_check_id,)
    )
    return [r["role_name"] for r in rows]

def save_voucher_check_auth_roles(voucher_check_id: int, auth_roles: List[str]):
    """Replace a voucher check's authorisation-roles list (delete-then-reinsert)."""
    execute_write("DELETE FROM audit_rule_voucher_check_auth_roles WHERE voucher_check_id = ?", (voucher_check_id,))
    for idx, role_name in enumerate(auth_roles):
        execute_write(
            "INSERT INTO audit_rule_voucher_check_auth_roles (voucher_check_id, role_name, sort_order) VALUES (?, ?, ?)",
            (voucher_check_id, role_name, idx)
        )

def row_to_voucher_check(row: dict) -> dict:
    voucher_check_id = row["id"]
    return {
        "id": voucher_check_id,
        "name": row["name"],
        "sort_order": row["sort_order"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
    }

def get_next_voucher_check_sort_order() -> int:
    """
    Next sort_order value to append a new check at the end of the list. Computed across all
    rows (active or not) so a soft-deleted row's slot isn't reused unexpectedly.
    """
    rows = execute_query("SELECT COALESCE(MAX(sort_order), -1) as max_order FROM audit_rule_voucher_checks")
    return rows[0]["max_order"] + 1

# ---------- ENDPOINTS ----------

@router.get("/api/audit-rules/voucher-checks")
def get_voucher_checks(include_inactive: bool = False):
    """
    Get all voucher checks, ordered by sort_order (a user-orderable list, unlike
    Documents/Cross-checks/Categories which order alphabetically). By default, only active
    checks are returned. Set include_inactive=True to get all.
    """
    query = "SELECT * FROM audit_rule_voucher_checks"
    if not include_inactive:
        query += " WHERE is_active = 1"
    query += " ORDER BY sort_order"
    rows = execute_query(query)
    return [row_to_voucher_check(r) for r in rows]

@router.get("/api/audit-rules/voucher-checks/{voucher_check_id}")
def get_voucher_check(voucher_check_id: int):
    rows = execute_query("SELECT * FROM audit_rule_voucher_checks WHERE id = ?", (voucher_check_id,))
    if not rows:
        raise HTTPException(404, "Voucher check not found")
    return row_to_voucher_check(rows[0])

@router.post("/api/audit-rules/voucher-checks")
def create_voucher_check(voucher_check: VoucherCheckCreate):
    sort_order = get_next_voucher_check_sort_order()
    voucher_check_id = execute_write(
        "INSERT INTO audit_rule_voucher_checks (name, sort_order) VALUES (?, ?)",
        (voucher_check.name, sort_order)
    )

    log_audit("Admin", "Create", "AuditRules", f"Created voucher check: {voucher_check.name}")
    return {"message": f"Voucher check '{voucher_check.name}' created successfully", "voucher_check_id": voucher_check_id}

@router.put("/api/audit-rules/voucher-checks/{voucher_check_id}")
def update_voucher_check(voucher_check_id: int, voucher_check: VoucherCheckUpdate):
    existing = execute_query("SELECT id FROM audit_rule_voucher_checks WHERE id = ?", (voucher_check_id,))
    if not existing:
        raise HTTPException(404, "Voucher check not found")

    execute_write(
        "UPDATE audit_rule_voucher_checks SET name = ? WHERE id = ?",
        (voucher_check.name, voucher_check_id)
    )

    log_audit("Admin", "Update", "AuditRules", f"Updated voucher check ID {voucher_check_id}")
    return {"message": "Voucher check updated successfully"}

@router.delete("/api/audit-rules/voucher-checks/{voucher_check_id}")
def delete_voucher_check(voucher_check_id: int):
    """
    Soft-delete a voucher check (set is_active = 0).
    """
    existing = execute_query("SELECT name FROM audit_rule_voucher_checks WHERE id = ? AND is_active = 1", (voucher_check_id,))
    if not existing:
        raise HTTPException(404, "Active voucher check not found")
    execute_write("UPDATE audit_rule_voucher_checks SET is_active = 0 WHERE id = ?", (voucher_check_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Soft-deleted voucher check: {existing[0]['name']} (ID: {voucher_check_id})")
    return {"message": "Voucher check deleted (soft-delete) successfully"}

@router.post("/api/audit-rules/voucher-checks/{voucher_check_id}/restore")
def restore_voucher_check(voucher_check_id: int):
    """
    Restore a soft-deleted voucher check.
    """
    existing = execute_query("SELECT name FROM audit_rule_voucher_checks WHERE id = ? AND is_active = 0", (voucher_check_id,))
    if not existing:
        raise HTTPException(404, "Inactive voucher check not found")
    execute_write("UPDATE audit_rule_voucher_checks SET is_active = 1 WHERE id = ?", (voucher_check_id,))
    log_audit("Admin", "Restore", "AuditRules", f"Restored voucher check: {existing[0]['name']} (ID: {voucher_check_id})")
    return {"message": "Voucher check restored successfully"}

@router.post("/api/audit-rules/voucher-checks/reorder")
def reorder_voucher_checks(check_ids: List[int] = Body(...)):
    """Update sort_order for all voucher checks, same pattern as reorder_template_columns()."""
    for idx, check_id in enumerate(check_ids):
        execute_write(
            "UPDATE audit_rule_voucher_checks SET sort_order = ? WHERE id = ?",
            (idx, check_id)
        )
    log_audit("Admin", "Reorder", "AuditRules", "Reordered voucher checks")
    return {"message": "Order updated successfully"}


# ==============================================
# SIGNATURE SPECIMENS
# ==============================================
# Validation Rules (Phase 2 of 7): expected signatory name per role/document type, name-only,
# no image handling, per the locked assumption. Always scoped to a client (a client's own
# staff, not a global list). The signature_specimens table (Phase 1 schema) has no is_active
# column, so unlike Documents/Cross-checks/Categories above this is a plain 5-endpoint CRUD
# with a real hard delete, not the six-endpoint soft-delete/restore pattern - a deliberate
# deviation, not an oversight, matching the "simple name-list management" scope of this phase.

# ---------- DATA MODELS ----------
class SignatureSpecimenCreate(BaseModel):
    client_id: int
    document_id: int
    role_name: str
    expected_signatory_name: str

class SignatureSpecimenUpdate(BaseModel):
    client_id: int
    document_id: int
    role_name: str
    expected_signatory_name: str

# ---------- HELPERS ----------
def validate_client_id(client_id: int):
    """Raises HTTPException(400) if client_id doesn't reference an active client."""
    rows = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not rows:
        raise HTTPException(400, f"Client ID {client_id} not found or inactive")

def validate_signature_specimen_payload(document_id: int, role_name: str):
    """
    Raises HTTPException(400) if document_id doesn't exist, or role_name isn't one of that
    Document's configured auth roles - mirrors validate_cross_check_payload()'s compare_field
    check against a Document's cross_check_fields.
    """
    doc_rows = execute_query("SELECT name FROM audit_rule_documents WHERE id = ?", (document_id,))
    if not doc_rows:
        raise HTTPException(400, f"Document ID {document_id} not found")
    auth_roles = get_document_auth_roles(document_id)
    if role_name not in auth_roles:
        raise HTTPException(
            400,
            f"Role '{role_name}' is not a configured authorisation role on Document '{doc_rows[0]['name']}' (ID {document_id})"
        )

def row_to_signature_specimen(row: dict) -> dict:
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "document_id": row["document_id"],
        "document_name": row.get("document_name"),
        "role_name": row["role_name"],
        "expected_signatory_name": row["expected_signatory_name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

SIGNATURE_SPECIMEN_SELECT = """
    SELECT s.id, s.client_id, s.document_id, d.name as document_name, s.role_name,
           s.expected_signatory_name, s.created_at, s.updated_at
    FROM signature_specimens s
    JOIN audit_rule_documents d ON s.document_id = d.id
"""

# ---------- ENDPOINTS ----------

@router.get("/api/audit-rules/signature-specimens")
def get_signature_specimens(client_id: int, document_id: Optional[int] = None):
    """
    Get all signature specimens for a client, optionally filtered to one Document.
    client_id is required - specimens are always client-scoped.
    """
    query = SIGNATURE_SPECIMEN_SELECT + " WHERE s.client_id = ?"
    params = [client_id]
    if document_id is not None:
        query += " AND s.document_id = ?"
        params.append(document_id)
    query += " ORDER BY d.name, s.role_name"
    rows = execute_query(query, tuple(params))
    return [row_to_signature_specimen(r) for r in rows]

@router.get("/api/audit-rules/signature-specimens/{specimen_id}")
def get_signature_specimen(specimen_id: int):
    rows = execute_query(SIGNATURE_SPECIMEN_SELECT + " WHERE s.id = ?", (specimen_id,))
    if not rows:
        raise HTTPException(404, "Signature specimen not found")
    return row_to_signature_specimen(rows[0])

@router.post("/api/audit-rules/signature-specimens")
def create_signature_specimen(specimen: SignatureSpecimenCreate):
    validate_client_id(specimen.client_id)
    validate_signature_specimen_payload(specimen.document_id, specimen.role_name)

    try:
        specimen_id = execute_write(
            "INSERT INTO signature_specimens (client_id, document_id, role_name, expected_signatory_name) VALUES (?, ?, ?, ?)",
            (specimen.client_id, specimen.document_id, specimen.role_name, specimen.expected_signatory_name)
        )
    except sqlite3.IntegrityError:
        raise HTTPException(400, "A signature specimen for this client/document/role already exists")

    log_audit(
        "Admin", "Create", "AuditRules",
        f"Created signature specimen for client {specimen.client_id}, document {specimen.document_id}, role '{specimen.role_name}'"
    )
    return {"message": "Signature specimen created successfully", "specimen_id": specimen_id}

@router.put("/api/audit-rules/signature-specimens/{specimen_id}")
def update_signature_specimen(specimen_id: int, specimen: SignatureSpecimenUpdate):
    existing = execute_query("SELECT id FROM signature_specimens WHERE id = ?", (specimen_id,))
    if not existing:
        raise HTTPException(404, "Signature specimen not found")

    validate_client_id(specimen.client_id)
    validate_signature_specimen_payload(specimen.document_id, specimen.role_name)

    try:
        execute_write(
            "UPDATE signature_specimens SET client_id = ?, document_id = ?, role_name = ?, "
            "expected_signatory_name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (specimen.client_id, specimen.document_id, specimen.role_name, specimen.expected_signatory_name, specimen_id)
        )
    except sqlite3.IntegrityError:
        raise HTTPException(400, "A signature specimen for this client/document/role already exists")

    log_audit("Admin", "Update", "AuditRules", f"Updated signature specimen ID {specimen_id}")
    return {"message": "Signature specimen updated successfully"}

@router.delete("/api/audit-rules/signature-specimens/{specimen_id}")
def delete_signature_specimen(specimen_id: int):
    """
    Hard delete - no is_active column exists on signature_specimens (Phase 1 schema), so
    unlike Documents/Cross-checks/Categories there is no soft-delete/restore for this entity.
    """
    existing = execute_query("SELECT expected_signatory_name FROM signature_specimens WHERE id = ?", (specimen_id,))
    if not existing:
        raise HTTPException(404, "Signature specimen not found")
    execute_write("DELETE FROM signature_specimens WHERE id = ?", (specimen_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Deleted signature specimen ID {specimen_id} ({existing[0]['expected_signatory_name']})")
    return {"message": "Signature specimen deleted successfully"}


# ---------- PER-DOCUMENT APPROVAL-REQUIRED FLAG (Validation Rules, Phase 2 of 7) ----------
# A dedicated toggle endpoint rather than folding approval state into CategoryCreate/
# CategoryUpdate's existing document_ids: List[int] - that field's shape stays exactly as the
# already-live Categories page (Phase 7 of 9) sends it today, so this addition doesn't disturb
# an unrelated, already-working flow. Mirrors this app's existing single-field-toggle-on-an-
# existing-row convention (e.g. PUT /api/trial-balance/accounts/{id}/override).
class CategoryDocumentApprovalUpdate(BaseModel):
    approval_required: bool

@router.put("/api/audit-rules/categories/{category_id}/documents/{document_id}/approval")
def update_category_document_approval(category_id: int, document_id: int, payload: CategoryDocumentApprovalUpdate):
    existing = execute_query(
        "SELECT approval_required FROM audit_rule_category_documents WHERE category_id = ? AND document_id = ?",
        (category_id, document_id)
    )
    if not existing:
        raise HTTPException(404, "This Document is not part of this Category")

    new_value = 1 if payload.approval_required else 0
    changed = bool(existing[0]["approval_required"]) != payload.approval_required

    execute_write(
        "UPDATE audit_rule_category_documents SET approval_required = ? WHERE category_id = ? AND document_id = ?",
        (new_value, category_id, document_id)
    )
    if changed:
        bump_category_matrix_version(category_id)

    log_audit(
        "Admin", "Update", "AuditRules",
        f"Set approval_required={payload.approval_required} for Document ID {document_id} in Category ID {category_id}"
    )
    return {"category_id": category_id, "document_id": document_id, "approval_required": payload.approval_required}


# ---------- PER-DOCUMENT IS-REQUIRED FLAG (Automated cross-document validation engine, Phase 4 of 10) ----------
# Same dedicated-toggle convention as the approval_required endpoint directly above - a
# separate endpoint rather than folding into CategoryCreate/CategoryUpdate's document_ids,
# for the same reason (keeps that already-live payload shape untouched). Bumping
# matrix_version on an actual change mirrors the approval_required toggle too: is_required is
# part of the same "what does this Category's matrix require" concept, even though nothing
# reads it yet (Phase 7 wires it into compute_and_persist_cleared_status()).
class CategoryDocumentRequiredUpdate(BaseModel):
    is_required: bool

@router.put("/api/audit-rules/categories/{category_id}/documents/{document_id}/required")
def update_category_document_required(category_id: int, document_id: int, payload: CategoryDocumentRequiredUpdate):
    existing = execute_query(
        "SELECT is_required FROM audit_rule_category_documents WHERE category_id = ? AND document_id = ?",
        (category_id, document_id)
    )
    if not existing:
        raise HTTPException(404, "This Document is not part of this Category")

    new_value = 1 if payload.is_required else 0
    changed = bool(existing[0]["is_required"]) != payload.is_required

    execute_write(
        "UPDATE audit_rule_category_documents SET is_required = ? WHERE category_id = ? AND document_id = ?",
        (new_value, category_id, document_id)
    )
    if changed:
        bump_category_matrix_version(category_id)

    log_audit(
        "Admin", "Update", "AuditRules",
        f"Set is_required={payload.is_required} for Document ID {document_id} in Category ID {category_id}"
    )
    return {"category_id": category_id, "document_id": document_id, "is_required": payload.is_required}


# ---------- PER-DOCUMENT TRANSACTION-LEVEL-APPROVAL FLAG (new, confirmed requirement) ----------
# A dedicated toggle endpoint, same convention as the Category-Document approval_required
# toggle above - but scoped to the Document itself (no category_id), since this flag lives
# directly on audit_rule_documents, not a Category pairing. No matrix_version bump: that
# concept only applies to a Category's own document/approval-required state.
class DocumentTransactionLevelApprovalUpdate(BaseModel):
    transaction_level_approval: bool

@router.put("/api/audit-rules/documents/{document_id}/transaction-level-approval")
def update_document_transaction_level_approval(document_id: int, payload: DocumentTransactionLevelApprovalUpdate):
    existing = execute_query("SELECT id FROM audit_rule_documents WHERE id = ?", (document_id,))
    if not existing:
        raise HTTPException(404, "Document not found")

    new_value = 1 if payload.transaction_level_approval else 0
    execute_write(
        "UPDATE audit_rule_documents SET transaction_level_approval = ? WHERE id = ?",
        (new_value, document_id)
    )

    log_audit(
        "Admin", "Update", "AuditRules",
        f"Set transaction_level_approval={payload.transaction_level_approval} for Document ID {document_id}"
    )
    return {"document_id": document_id, "transaction_level_approval": payload.transaction_level_approval}
