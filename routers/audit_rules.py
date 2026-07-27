# ==============================================
# 📐 AUDIT RULES ROUTER
# ==============================================
# Phase 2 of 9 added Documents CRUD; Phase 3 of 9 added Cross-checks CRUD; Phase 4 of 9
# added Categories CRUD - all three reusable entity types (Documents / Cross-checks /
# Categories) in the Audit Rules module now have CRUD. Categories is the entity a
# voucher will eventually reference (wired up in Phase 9).

from fastapi import APIRouter, HTTPException
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
        SELECT d.id as document_id, d.name
        FROM audit_rule_category_documents cd
        JOIN audit_rule_documents d ON cd.document_id = d.id
        WHERE cd.category_id = ?
        ORDER BY cd.sort_order
        """,
        (category_id,)
    )
    return [{"document_id": r["document_id"], "name": r["name"]} for r in rows]

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
        SELECT d.id as document_id, d.name, d.kb_note, d.template_id, d.risk_rating
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
        "documents": get_category_documents(category_id),
        "cross_checks": get_category_cross_checks(category_id),
        "used_by_vouchers_count": get_category_used_by_vouchers_count(category_id)
    }

def save_category_documents(category_id: int, document_ids: List[int]):
    """Replace a Category's Document list (delete-then-reinsert)."""
    execute_write("DELETE FROM audit_rule_category_documents WHERE category_id = ?", (category_id,))
    for idx, document_id in enumerate(document_ids):
        execute_write(
            "INSERT INTO audit_rule_category_documents (category_id, document_id, sort_order) VALUES (?, ?, ?)",
            (category_id, document_id, idx)
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

    execute_write(
        "UPDATE audit_rule_categories SET name = ?, kb_overview = ? WHERE id = ?",
        (category.name, category.kb_overview, category_id)
    )
    save_category_documents(category_id, category.document_ids)
    save_category_cross_checks(category_id, category.cross_check_ids)

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
