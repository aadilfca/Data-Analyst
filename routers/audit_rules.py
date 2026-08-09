# ==============================================
# 📐 AUDIT RULES ROUTER
# ==============================================
# Phase 2 of 9 added Documents CRUD; Phase 3 of 9 added Cross-checks CRUD; Phase 4 of 9
# added Categories CRUD - all three reusable entity types (Documents / Cross-checks /
# Categories) in the Audit Rules module now have CRUD. Categories is the entity a
# voucher will eventually reference (wired up in Phase 9).

from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel
from typing import Optional, List, Dict
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
    # Required Authority (new, confirmed requirement): role_name -> required authority text,
    # additive alongside auth_roles so that field's existing List[str] contract (read by
    # static/verification.js and signature-specimen role validation) stays untouched. Only
    # ever persisted for a role matching is_approval_role() below - see save_document_auth_roles().
    auth_role_authorities: Dict[str, Optional[str]] = {}
    cross_check_fields: List[str] = []
    # Physical Bill evidence type (Phase 2 of 8): how this Document's evidence is satisfied -
    # 'linked_template' (a linked Data Bank row, or ticked procedure roles when there's no
    # template) or 'physical_bill' (manually-keyed Bill No/Date/Amount/Vendor name, captured
    # per voucher line in Phase 5). Optional/None rather than a literal default here so
    # resolve_document_type() below stays the single place the default lives.
    document_type: Optional[str] = None

class DocumentUpdate(BaseModel):
    name: str
    kb_note: Optional[str] = None
    template_id: Optional[int] = None
    risk_rating: Optional[str] = None
    auth_roles: List[str] = []
    auth_role_authorities: Dict[str, Optional[str]] = {}
    cross_check_fields: List[str] = []
    document_type: Optional[str] = None

# ---------- HELPERS ----------
def get_document_auth_roles(document_id: int) -> List[str]:
    rows = execute_query(
        "SELECT role_name FROM audit_rule_document_auth_roles WHERE document_id = ? ORDER BY sort_order",
        (document_id,)
    )
    return [r["role_name"] for r in rows]

def is_approval_role(role_name: str) -> bool:
    """
    Matches an "Approved by"-type role by substring, not an exact string - role names are
    free text (e.g. "Approved By:", "Approved by"). Mirrors routers/verification.py's
    compute_and_persist_cleared_status() approval-role check ('approv' in role_name.lower()),
    duplicated here per this codebase's established per-router cross-cutting-check convention.
    """
    return 'approv' in (role_name or '').lower()

def get_document_auth_role_authorities(document_id: int) -> dict:
    """Return {role_name: required_authority} for a Document's roles that have one set."""
    rows = execute_query(
        "SELECT role_name, required_authority FROM audit_rule_document_auth_roles WHERE document_id = ? ORDER BY sort_order",
        (document_id,)
    )
    return {r["role_name"]: r["required_authority"] for r in rows if r["required_authority"]}

def get_required_documents_with_authority(category_id: int) -> List[dict]:
    """
    Category's documents flagged is_required=1 (Automated cross-document validation engine,
    Phase 4 of 10), each with its Approved-by-role(s)' Required Authority (only ever set for a
    role matching is_approval_role() - see save_document_auth_roles()). Used by
    routers/verification.py's compute_and_persist_cleared_status() for the Document-level
    Approved-by authority gate (new, confirmed requirement).
    """
    rows = execute_query(
        """
        SELECT d.id as document_id, d.name
        FROM audit_rule_category_documents cd
        JOIN audit_rule_documents d ON cd.document_id = d.id
        WHERE cd.category_id = ? AND cd.is_required = 1 AND d.is_active = 1
        """,
        (category_id,)
    )
    result = []
    for r in rows:
        authorities = get_document_auth_role_authorities(r["document_id"])
        if authorities:
            result.append({"document_id": r["document_id"], "name": r["name"], "required_authorities": authorities})
    return result

def has_matching_active_specimen(client_id: int, document_id: int, role_name: str, required_authority: str) -> bool:
    """
    True if an ACTIVE Signature Specimen exists for this client+document+role whose Authority
    exactly matches (case/whitespace-insensitive) required_authority.
    """
    rows = execute_query(
        """
        SELECT id FROM signature_specimens
        WHERE client_id = ? AND document_id = ? AND role_name = ? AND is_active = 1
          AND TRIM(LOWER(authority)) = TRIM(LOWER(?))
        """,
        (client_id, document_id, role_name, required_authority)
    )
    return bool(rows)

def check_approved_by_authority_gate(client_id: int, category_id: int) -> bool:
    """
    Document-level Approved-by authority gate (new, confirmed requirement) for Cleared/Query
    computation - replaces the retired Category-level approval_required substring-match gate.
    For every Document required by this Category whose Approved-by role has a Required
    Authority set, requires at least one ACTIVE Signature Specimen on file for that
    client+document+role whose Authority exactly matches. This is an existence check ("a
    qualified signatory is on file"), not a match against a specific recorded signer - nothing
    in this app records who actually signed a particular voucher line's document (confirmed
    with the user). A Document/role with no Required Authority set imposes no requirement;
    vacuously True if no required Document has any Required Authority configured at all.
    """
    for doc in get_required_documents_with_authority(category_id):
        for role_name, required_authority in doc["required_authorities"].items():
            if not has_matching_active_specimen(client_id, doc["document_id"], role_name, required_authority):
                return False
    return True

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
        # Physical Bill evidence type (Phase 2 of 8). The `or` fallback is defensive only -
        # Phase 1's ALTER backfilled every existing row to 'linked_template'.
        "document_type": row.get("document_type") or 'linked_template',
        "transaction_level_approval": bool(row.get("transaction_level_approval") or 0),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "auth_roles": get_document_auth_roles(document_id),
        "auth_role_authorities": get_document_auth_role_authorities(document_id),
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

# Physical Bill evidence type (Phase 2 of 8). Validated here rather than by a DB CHECK: Phase 1
# could only attach a real CHECK to the CREATE TABLE, since a post-hoc ALTER can't retrofit one
# without a full table rebuild - so on an already-initialized database this is the only
# enforcement. Same accepted trade-off, and same code-level-validator shape, as RISK_RATINGS above.
DOCUMENT_TYPES = ('linked_template', 'physical_bill')

def resolve_document_type(document_type: Optional[str]) -> str:
    """
    Resolve a Document's type, raising HTTPException(400) on an unrecognised value.

    Both an omitted field and an explicit null resolve to 'linked_template' - deliberately a
    resolver rather than a bare validator (mirroring resolve_rate_source() in
    routers/templates.py) so create/update each get one already-disciplined value instead of
    repeating the fallback.

    'linked_template' is the safe default: it's what all existing Documents already are, so an
    API caller that doesn't know about this field yet (static/audit_rules.js until Phase 4)
    keeps working unchanged. Confirmed with the user - forcing an explicit choice at create time
    belongs in Phase 4, alongside the picker that makes the choice possible; the "no silent
    assumption" rule stays on Categories' evidence_type, which is what actually drives the gate.
    """
    if document_type is None:
        return 'linked_template'
    if document_type not in DOCUMENT_TYPES:
        raise HTTPException(400, f"Invalid document type '{document_type}'. Must be one of: {', '.join(DOCUMENT_TYPES)}")
    return document_type

def apply_document_type_constraints(document_type: str, template_id: Optional[int], cross_check_fields: List[str]):
    """
    Return the (template_id, cross_check_fields) a Document of this type is actually allowed to
    have. A 'physical_bill' has neither: its field set (Bill No / Date / Amount / Vendor name) is
    fixed by type rather than drawn from a Data Bank template, so there's no template to link and
    no field list to configure.

    Both are forced rather than rejected - the same discipline-server-side convention as
    resolve_rate_source() (routers/templates.py) forcing NULL for an ineligible template, and
    save_document_auth_roles() dropping required_authority on a non-approval role. Forcing
    template_id is safety-critical, not cosmetic: compute_and_persist_cleared_status()'s check #1
    (routers/verification.py) branches on template_id, so a physical_bill that carried one would
    be routed down the template-linked branch and demand a linked Data Bank evidence row that can
    never exist for a physical bill.

    auth_roles are deliberately NOT stripped - a physical_bill still supports them (confirmed
    against check #1's actual logic, not assumed: a template-less Document is currently satisfied
    only by having every configured role ticked, so stripping roles would make one permanently
    unsatisfiable until Phase 6 intercepts this type).
    """
    if document_type == 'physical_bill':
        return None, []
    return template_id, cross_check_fields

def save_document_auth_roles(document_id: int, auth_roles: List[str], required_authority_map: Optional[Dict[str, Optional[str]]] = None):
    """
    Replace a Document's authorisation-roles list (delete-then-reinsert). required_authority_map
    (role_name -> text) is only ever persisted for a role matching is_approval_role() - a value
    supplied for any other role is silently dropped, enforcing "only applies to Approved by"
    server-side rather than relying on the UI alone.
    """
    required_authority_map = required_authority_map or {}
    execute_write("DELETE FROM audit_rule_document_auth_roles WHERE document_id = ?", (document_id,))
    for idx, role_name in enumerate(auth_roles):
        required_authority = required_authority_map.get(role_name) if is_approval_role(role_name) else None
        execute_write(
            "INSERT INTO audit_rule_document_auth_roles (document_id, role_name, sort_order, required_authority) VALUES (?, ?, ?, ?)",
            (document_id, role_name, idx, required_authority)
        )

def resolve_field_name_to_master_column_id(template_id: Optional[int], field_name: str) -> Optional[int]:
    """
    Best-effort match a free-text field name to a master column mapped onto the given
    template (case-insensitive/trimmed) - mirrors database.py's own startup migration
    matching logic (duplicated here per this codebase's established per-router
    cross-cutting-lookup convention, not cross-imported). Returns None (never raises) if
    template_id is unset or no same-named column is mapped onto that template.
    """
    if not template_id:
        return None
    rows = execute_query(
        """
        SELECT mc.id FROM master_columns mc
        JOIN template_column_mappings tcm ON tcm.column_id = mc.id
        WHERE tcm.template_id = ? AND LOWER(TRIM(mc.column_name)) = LOWER(TRIM(?))
        """,
        (template_id, field_name)
    )
    return rows[0]["id"] if rows else None

def save_document_cross_check_fields(document_id: int, cross_check_fields: List[str]):
    """
    Replace a Document's cross-check-eligible fields list (delete-then-reinsert). Each
    field_name is also best-effort resolved to a master_column_id scoped to the Document's
    own linked template (ID-based Cross-check-eligible Fields, Phase 1 of 4) - an unmatched
    name is stored with master_column_id NULL, flagged for manual re-selection once a later
    phase's dropdown UI lands, never silently dropped. field_name itself is unchanged/still
    authoritative for the API's existing List[str] contract.
    """
    doc_rows = execute_query("SELECT template_id FROM audit_rule_documents WHERE id = ?", (document_id,))
    template_id = doc_rows[0]["template_id"] if doc_rows else None
    execute_write("DELETE FROM audit_rule_document_fields WHERE document_id = ?", (document_id,))
    for idx, field_name in enumerate(cross_check_fields):
        master_column_id = resolve_field_name_to_master_column_id(template_id, field_name)
        execute_write(
            "INSERT INTO audit_rule_document_fields (document_id, field_name, sort_order, master_column_id) VALUES (?, ?, ?, ?)",
            (document_id, field_name, idx, master_column_id)
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
    document_type = resolve_document_type(document.document_type)
    template_id, cross_check_fields = apply_document_type_constraints(
        document_type, document.template_id, document.cross_check_fields
    )
    validate_document_template_id(template_id)
    validate_document_risk_rating(document.risk_rating)
    document_id = execute_write(
        "INSERT INTO audit_rule_documents (name, kb_note, template_id, risk_rating, document_type) VALUES (?, ?, ?, ?, ?)",
        (document.name, document.kb_note, template_id, document.risk_rating, document_type)
    )
    save_document_auth_roles(document_id, document.auth_roles, document.auth_role_authorities)
    save_document_cross_check_fields(document_id, cross_check_fields)

    log_audit("Admin", "Create", "AuditRules", f"Created Document: {document.name}")
    return {"message": f"Document '{document.name}' created successfully", "document_id": document_id}

@router.put("/api/audit-rules/documents/{document_id}")
def update_document(document_id: int, document: DocumentUpdate):
    existing = execute_query("SELECT id FROM audit_rule_documents WHERE id = ?", (document_id,))
    if not existing:
        raise HTTPException(404, "Document not found")
    document_type = resolve_document_type(document.document_type)
    template_id, cross_check_fields = apply_document_type_constraints(
        document_type, document.template_id, document.cross_check_fields
    )
    validate_document_template_id(template_id)
    validate_document_risk_rating(document.risk_rating)

    execute_write(
        "UPDATE audit_rule_documents SET name = ?, kb_note = ?, template_id = ?, risk_rating = ?, document_type = ? WHERE id = ?",
        (document.name, document.kb_note, template_id, document.risk_rating, document_type, document_id)
    )
    save_document_auth_roles(document_id, document.auth_roles, document.auth_role_authorities)
    # save_document_cross_check_fields() is still called unconditionally (one write path), but
    # receives an empty list for a physical_bill, so nothing is persisted for it.
    save_document_cross_check_fields(document_id, cross_check_fields)

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
# too so a bad value returns a clean 400, not a raw SQLite CHECK-constraint 500. Extended
# from 4 to 6 values (Automated cross-document validation engine, Phase 1 of 10 schema /
# Phase 5 of 10 this UI) - 'Must be equal' and 'May be less, never more' added, none of the
# original 4 deprecated.
COMPARISON_TYPES = (
    'All equal', 'Sequential increasing', 'Sequential decreasing', 'Within tolerance %',
    'Must be equal', 'May be less, never more'
)

# ---------- DATA MODELS ----------
class CrossCheckCreate(BaseModel):
    name: str
    compare_field: str
    comparison_type: str
    document_ids: List[int] = []
    # Phase 5 of 10: marks this Cross-check as also comparing its anchor document (the last
    # document in document_ids, resolved live at evaluation time - Phase 6's job, not
    # configured here) against the voucher line's own amount. Config-only this phase; nothing
    # reads it yet.
    compare_to_voucher_line: bool = False

class CrossCheckUpdate(BaseModel):
    name: str
    compare_field: str
    comparison_type: str
    document_ids: List[int] = []
    compare_to_voucher_line: bool = False

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
        "compare_to_voucher_line": bool(row.get("compare_to_voucher_line") or 0),
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

def validate_cross_check_payload(compare_field: str, comparison_type: str, document_ids: List[int], compare_to_voucher_line: bool = False):
    """
    Raises HTTPException(400) if comparison_type isn't one of the fixed set, or if
    compare_field isn't a cross-check-eligible field on every selected Document, or if
    compare_to_voucher_line is set with no Document to act as the anchor (Phase 5 of 10 -
    the anchor is the last document in document_ids, resolved live at evaluation time, never
    picked here).
    SQLite foreign keys aren't enforced on this app's connections (see get_db()), so
    Document existence is also checked here rather than left to the database.
    """
    if comparison_type not in COMPARISON_TYPES:
        raise HTTPException(400, f"Invalid comparison type '{comparison_type}'. Must be one of: {', '.join(COMPARISON_TYPES)}")

    if compare_to_voucher_line and not document_ids:
        raise HTTPException(400, "compare_to_voucher_line requires at least one Document (used as the anchor)")

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
    validate_cross_check_payload(
        cross_check.compare_field, cross_check.comparison_type, cross_check.document_ids,
        cross_check.compare_to_voucher_line
    )

    cross_check_id = execute_write(
        "INSERT INTO audit_rule_cross_checks (name, compare_field, comparison_type, compare_to_voucher_line) VALUES (?, ?, ?, ?)",
        (cross_check.name, cross_check.compare_field, cross_check.comparison_type, int(cross_check.compare_to_voucher_line))
    )
    save_cross_check_documents(cross_check_id, cross_check.document_ids)

    log_audit("Admin", "Create", "AuditRules", f"Created Cross-check: {cross_check.name}")
    return {"message": f"Cross-check '{cross_check.name}' created successfully", "cross_check_id": cross_check_id}

@router.put("/api/audit-rules/cross-checks/{cross_check_id}")
def update_cross_check(cross_check_id: int, cross_check: CrossCheckUpdate):
    existing = execute_query("SELECT id FROM audit_rule_cross_checks WHERE id = ?", (cross_check_id,))
    if not existing:
        raise HTTPException(404, "Cross-check not found")

    validate_cross_check_payload(
        cross_check.compare_field, cross_check.comparison_type, cross_check.document_ids,
        cross_check.compare_to_voucher_line
    )

    execute_write(
        "UPDATE audit_rule_cross_checks SET name = ?, compare_field = ?, comparison_type = ?, compare_to_voucher_line = ? WHERE id = ?",
        (cross_check.name, cross_check.compare_field, cross_check.comparison_type, int(cross_check.compare_to_voucher_line), cross_check_id)
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
    # Physical Bill evidence type (Phase 3 of 8): what kind of evidence this Category requires -
    # 'billable' (a Physical Bill document, whose Amount Phase 6 hard-matches against the voucher
    # line amount) or 'non_billable' (today's procedure-role gate). MANDATORY - see
    # validate_category_evidence_type() below for why there's no default here.
    #
    # Typed Optional even though it's required, deliberately: declaring it `str` would make
    # Pydantic reject a missing value with a 422 and a schema-shaped error body, whereas every
    # other validation failure in this file is an explicit HTTPException(400) carrying a plain
    # sentence. The validator produces that clean 400 instead.
    evidence_type: Optional[str] = None

class CategoryUpdate(BaseModel):
    name: str
    kb_overview: Optional[str] = None
    document_ids: List[int] = []
    cross_check_ids: List[int] = []
    evidence_type: Optional[str] = None

# Physical Bill evidence type (Phase 3 of 8). Validated in code for the same reason as
# DOCUMENT_TYPES above - Phase 1 could only attach a real CHECK to the CREATE TABLE, so on an
# already-initialized database this is the only enforcement.
EVIDENCE_TYPES = ('billable', 'non_billable')

def validate_category_evidence_type(evidence_type: Optional[str]):
    """
    Raises HTTPException(400) if evidence_type is missing OR unrecognised.

    Deliberately a validator, not a resolver - the opposite of resolve_document_type() above,
    which defaults a missing value to 'linked_template'. There is no safe default here:
    evidence_type is what drives Phase 6's Cleared/Query gate, so a Category that reached that
    gate unclassified would misclassify real vouchers. document_type could default safely only
    because it affects no gate yet. Confirmed with the user, including that this applies on
    UPDATE as well as CREATE - a half-enforced rule would leave exactly the hole this exists to
    close, and requiring the choice on edit doubles as the backfill route for the existing
    unclassified Categories.

    Two distinct messages so a caller can tell "you forgot it" from "that isn't a valid value".
    """
    if evidence_type is None:
        raise HTTPException(400, f"evidence_type is required. Must be one of: {', '.join(EVIDENCE_TYPES)}")
    if evidence_type not in EVIDENCE_TYPES:
        raise HTTPException(400, f"Invalid evidence type '{evidence_type}'. Must be one of: {', '.join(EVIDENCE_TYPES)}")

# ---------- HELPERS ----------
def get_category_documents(category_id: int) -> List[dict]:
    rows = execute_query(
        """
        SELECT d.id as document_id, d.name, cd.is_required, d.is_active, d.document_type
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
            # Automated cross-document validation engine, Phase 4 of 10: whether this
            # Document must be present for the Category to be Cleared. Not yet read by
            # compute_and_persist_cleared_status() - that wiring is Phase 7's job, deliberately
            # deferred so this can be configured in advance with no effect on live vouchers yet.
            "is_required": bool(r["is_required"]),
            # Inactive-document indicator (new, confirmed fix): the JOIN above is unfiltered on
            # d.is_active, so a soft-deleted attached Document's real name already comes through
            # here - this flag lets the frontend show it greyed/"Inactive" instead of falling
            # back to a bare "Document #{id}" placeholder, without removing it from the list or
            # auto-detaching it from the Category.
            "is_active": bool(r["is_active"]),
            # Physical Bill evidence type (Phase 3 of 8): lets a caller tell a physical-bill
            # requirement from a linked-template one. Purely additive - row_to_category() and
            # compute_and_persist_cleared_status()'s check #1 (routers/verification.py) both read
            # this helper by key. Exposed here rather than via a separate query in
            # get_category_cleared_requirements() because Phase 6 needs it at exactly this call
            # site anyway, to intercept a physical_bill before check #1's template_id branch.
            "document_type": r["document_type"] or 'linked_template'
        }
        for r in rows
    ]

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

def get_category_active_cross_checks_count(category_id: int) -> int:
    """
    Count of active Cross-checks attached to a Category - a soft-deleted Cross-check that's
    still attached doesn't count as 'required' (mirrors what the evaluation engine in
    routers/verification.py actually evaluates).
    """
    rows = execute_query(
        """
        SELECT COUNT(*) as cnt
        FROM audit_rule_category_cross_checks cc
        JOIN audit_rule_cross_checks c ON cc.cross_check_id = c.id
        WHERE cc.category_id = ? AND c.is_active = 1
        """,
        (category_id,)
    )
    return rows[0]["cnt"] if rows else 0

# Phase 9: fuller Document/Cross-check shapes for verification.js's audit-guidance panel and
# Supporting column - the Categories CRUD response only needs id/name (above), but the
# verification module needs template_id/kb_note/roles/fields per Document and
# compare_field/comparison_type/document-names per Cross-check. New, additive functions -
# get_category_documents()/get_category_cross_checks() above (used by the Categories CRUD
# response) are untouched.
def get_category_guidance_documents(category_id: int) -> List[dict]:
    rows = execute_query(
        """
        SELECT d.id as document_id, d.name, d.kb_note, d.template_id, d.risk_rating, d.transaction_level_approval, d.document_type
        FROM audit_rule_category_documents cd
        JOIN audit_rule_documents d ON cd.document_id = d.id
        WHERE cd.category_id = ? AND d.is_active = 1
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
            "cross_check_fields": get_document_cross_check_fields(r["document_id"]),
            # Physical Bill evidence type, Phase 7 of 8: lets the Verification frontend tell a
            # physical_bill Document apart from an ordinary no-template one when rendering the
            # procedure window's document rail - get_category_documents() above already exposes
            # this same field for the Category admin display; this is the guidance-endpoint
            # sibling Verification itself actually reads.
            "document_type": r["document_type"] or 'linked_template'
        }
        for r in rows
    ]

def get_category_guidance_cross_checks(category_id: int) -> List[dict]:
    rows = execute_query(
        """
        SELECT c.id as cross_check_id, c.name, c.compare_field, c.comparison_type
        FROM audit_rule_category_cross_checks cc
        JOIN audit_rule_cross_checks c ON cc.cross_check_id = c.id
        WHERE cc.category_id = ? AND c.is_active = 1
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
        # Physical Bill evidence type (Phase 3 of 8). Returned raw, NULL included - an existing
        # Category predating this field stays visibly unclassified rather than being silently
        # presented as one of the two values.
        "evidence_type": row.get("evidence_type"),
        # Validation Rules (Phase 2 of 7): bumped by bump_category_matrix_version() whenever
        # this Category's document list or is_required flags change.
        "matrix_version": row.get("matrix_version", 1),
        "documents": get_category_documents(category_id),
        "cross_checks": get_category_cross_checks(category_id),
        "used_by_vouchers_count": get_category_used_by_vouchers_count(category_id)
    }

def save_category_documents(category_id: int, document_ids: List[int]):
    """
    Replace a Category's Document list (delete-then-reinsert). Carries each surviving
    document's is_required value forward across the delete-then-reinsert (Cross-document
    validation engine, Phase 4 of 10) - a newly-added document defaults to 1, matching that
    column's own DB default (TRUE), so a plain document-list edit never silently makes
    anything optional. (approval_required's own carry-forward, Validation Rules Phase 2 of
    7, was removed alongside the column - confirmed requirement, 2026-08-02.)
    """
    existing_requireds = get_category_document_requireds(category_id)
    execute_write("DELETE FROM audit_rule_category_documents WHERE category_id = ?", (category_id,))
    for idx, document_id in enumerate(document_ids):
        is_required = existing_requireds.get(document_id, 1)
        execute_write(
            "INSERT INTO audit_rule_category_documents (category_id, document_id, sort_order, is_required) VALUES (?, ?, ?, ?)",
            (category_id, document_id, idx, is_required)
        )

def bump_category_matrix_version(category_id: int):
    """
    Increment audit_rule_categories.matrix_version (Validation Rules, Phase 2 of 7) - called
    automatically whenever a Category's document list or is_required flags change, never
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
    validate_category_evidence_type(category.evidence_type)
    validate_category_payload(category.document_ids, category.cross_check_ids)

    category_id = execute_write(
        "INSERT INTO audit_rule_categories (name, kb_overview, evidence_type) VALUES (?, ?, ?)",
        (category.name, category.kb_overview, category.evidence_type)
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

    # Enforced on update as well as create (confirmed with the user) - see
    # validate_category_evidence_type(). An existing Category still carrying NULL therefore can't
    # be edited until an evidence type is chosen, which is the intended backfill route.
    validate_category_evidence_type(category.evidence_type)
    validate_category_payload(category.document_ids, category.cross_check_ids)

    # Validation Rules (Phase 2 of 7): capture the document set before it's replaced, so a
    # real membership change (add/remove - reordering alone doesn't count) can trigger a
    # matrix_version bump below. Cross-check changes deliberately do not bump the version.
    old_document_ids = set(get_category_document_requireds(category_id).keys())

    execute_write(
        "UPDATE audit_rule_categories SET name = ?, kb_overview = ?, evidence_type = ? WHERE id = ?",
        (category.name, category.kb_overview, category.evidence_type, category_id)
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

@router.get("/api/audit-rules/categories/{category_id}/cleared-requirements")
def get_category_cleared_requirements(category_id: int):
    """
    Read-only 'what does Cleared require for this Category' summary (new, confirmed
    requirement) - reflects live backend state, no manual toggles. Reuses
    get_category_documents() (already filters/exposes is_required + is_active per document,
    the same data compute_and_persist_cleared_status()'s check #1 in routers/verification.py
    reads) and get_category_active_cross_checks_count() above. Voucher-level checks and
    Account Head are global gates (not category-specific), always required.
    """
    existing = execute_query("SELECT id, evidence_type FROM audit_rule_categories WHERE id = ?", (category_id,))
    if not existing:
        raise HTTPException(404, "Category not found")

    # Physical Bill evidence type (Phase 3 of 8): report-only, confirmed with the user. This
    # echoes the Category's evidence_type and labels each required document with its own
    # document_type, but does NOT change which documents are reported as required - that's
    # Phase 6's gate logic. This endpoint's contract is to mirror what the gate actually reads;
    # deriving its own requirement rule ahead of the gate is how a summary and the real gate end
    # up disagreeing.
    required_documents = [
        {
            "document_id": d["document_id"],
            "name": d["name"],
            "is_active": d["is_active"],
            "document_type": d["document_type"],
        }
        for d in get_category_documents(category_id)
        if d["is_required"]
    ]

    return {
        "category_id": category_id,
        "evidence_type": existing[0]["evidence_type"],
        "voucher_level_checks_required": True,
        "account_head_required": True,
        "cross_checks_required": get_category_active_cross_checks_count(category_id) > 0,
        "required_documents": required_documents,
    }


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
    # Authority/Designation (new, confirmed requirement): free-text, applies uniformly to every
    # check - no role-specific gating. Informational only, not matched against anything yet.
    authority: Optional[str] = None

class VoucherCheckUpdate(BaseModel):
    name: str
    authority: Optional[str] = None

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
        "authority": row.get("authority"),
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
        "INSERT INTO audit_rule_voucher_checks (name, sort_order, authority) VALUES (?, ?, ?)",
        (voucher_check.name, sort_order, voucher_check.authority)
    )

    log_audit("Admin", "Create", "AuditRules", f"Created voucher check: {voucher_check.name}")
    return {"message": f"Voucher check '{voucher_check.name}' created successfully", "voucher_check_id": voucher_check_id}

@router.put("/api/audit-rules/voucher-checks/{voucher_check_id}")
def update_voucher_check(voucher_check_id: int, voucher_check: VoucherCheckUpdate):
    existing = execute_query("SELECT id FROM audit_rule_voucher_checks WHERE id = ?", (voucher_check_id,))
    if not existing:
        raise HTTPException(404, "Voucher check not found")

    execute_write(
        "UPDATE audit_rule_voucher_checks SET name = ?, authority = ? WHERE id = ?",
        (voucher_check.name, voucher_check.authority, voucher_check_id)
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
# staff, not a global list). Soft-delete conversion (new, confirmed 4-phase requirement),
# Phase 1 of 4: signature_specimens now has is_active (database.py upgrade block above), so
# this entity follows the same soft-delete/restore pattern as Documents/Cross-checks/
# Categories/Voucher Checks - the normal "Delete" action deactivates; a separate, low-visibility
# hard-delete endpoint remains for correcting genuine data-entry mistakes only. Relax uniqueness
# constraint, Phase 2 of 4: the old table-level UNIQUE(client_id, document_id, role_name)
# constraint (database.py) is gone - it used to block a replacement specimen once any row, even
# a deactivated one, existed for the same triple, which broke routine personnel turnover.
# validate_no_duplicate_active_signatory() below is now the sole enforcement: only one ACTIVE
# specimen per client+document+role at a time, but multiple inactive/historical rows and
# multiple concurrently-active rows for genuinely different people (e.g. two people both
# authorised to sign as "Store Manager") are both allowed.

# ---------- DATA MODELS ----------
class SignatureSpecimenCreate(BaseModel):
    client_id: int
    document_id: int
    role_name: str
    expected_signatory_name: str
    # Authority (new, confirmed requirement): free-text authority level for this signatory,
    # captured alongside expected_signatory_name at add-time. Unconditional - not gated to an
    # approval-type role. A later phase compares this against a Document's Required Authority.
    authority: Optional[str] = None

class SignatureSpecimenUpdate(BaseModel):
    client_id: int
    document_id: int
    role_name: str
    expected_signatory_name: str
    authority: Optional[str] = None

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

def validate_no_duplicate_active_signatory(client_id: int, document_id: int, role_name: str, expected_signatory_name: str, exclude_id: Optional[int] = None):
    """
    Raises HTTPException(400) if an ACTIVE signature specimen already exists for this
    client+document+role with the SAME person's name (Relax uniqueness constraint, Phase 2 of
    4 - replaces the old DB-level UNIQUE(client_id, document_id, role_name) constraint, which
    blocked even a deactivated row's slot from being reused, and would have also blocked
    genuinely different people). This check is scoped to name too (case-insensitive/trimmed)
    - it exists to catch an accidental literal duplicate entry for the same person, not to
    limit a role to one active person: multiple concurrently-active rows for genuinely
    different people at the same client+document+role are allowed by design (e.g. two people
    both authorised to sign as "Store Manager"), per the confirmed requirement. exclude_id lets
    an update to a specimen's own row skip checking itself.
    """
    query = (
        "SELECT id FROM signature_specimens WHERE client_id = ? AND document_id = ? AND role_name = ? "
        "AND is_active = 1 AND TRIM(LOWER(expected_signatory_name)) = TRIM(LOWER(?))"
    )
    params = [client_id, document_id, role_name, expected_signatory_name]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    existing = execute_query(query, tuple(params))
    if existing:
        raise HTTPException(
            400,
            f"An active signature specimen for '{expected_signatory_name}' in this client/document/role already exists (ID {existing[0]['id']})"
        )

def row_to_signature_specimen(row: dict) -> dict:
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "document_id": row["document_id"],
        "document_name": row.get("document_name"),
        "role_name": row["role_name"],
        "expected_signatory_name": row["expected_signatory_name"],
        "authority": row.get("authority"),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

SIGNATURE_SPECIMEN_SELECT = """
    SELECT s.id, s.client_id, s.document_id, d.name as document_name, s.role_name,
           s.expected_signatory_name, s.authority, s.is_active, s.created_at, s.updated_at
    FROM signature_specimens s
    JOIN audit_rule_documents d ON s.document_id = d.id
"""

# ---------- ENDPOINTS ----------

@router.get("/api/audit-rules/signature-specimens")
def get_signature_specimens(client_id: int, document_id: Optional[int] = None, include_inactive: bool = False):
    """
    Get all signature specimens for a client, optionally filtered to one Document.
    client_id is required - specimens are always client-scoped. By default, only active
    specimens are returned. Set include_inactive=True to get all (Soft-delete conversion,
    Phase 1 of 4).
    """
    query = SIGNATURE_SPECIMEN_SELECT + " WHERE s.client_id = ?"
    params = [client_id]
    if document_id is not None:
        query += " AND s.document_id = ?"
        params.append(document_id)
    if not include_inactive:
        query += " AND s.is_active = 1"
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
    validate_no_duplicate_active_signatory(specimen.client_id, specimen.document_id, specimen.role_name, specimen.expected_signatory_name)

    specimen_id = execute_write(
        "INSERT INTO signature_specimens (client_id, document_id, role_name, expected_signatory_name, authority) VALUES (?, ?, ?, ?, ?)",
        (specimen.client_id, specimen.document_id, specimen.role_name, specimen.expected_signatory_name, specimen.authority)
    )

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
    validate_no_duplicate_active_signatory(specimen.client_id, specimen.document_id, specimen.role_name, specimen.expected_signatory_name, exclude_id=specimen_id)

    execute_write(
        "UPDATE signature_specimens SET client_id = ?, document_id = ?, role_name = ?, "
        "expected_signatory_name = ?, authority = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (specimen.client_id, specimen.document_id, specimen.role_name, specimen.expected_signatory_name, specimen.authority, specimen_id)
    )

    log_audit("Admin", "Update", "AuditRules", f"Updated signature specimen ID {specimen_id}")
    return {"message": "Signature specimen updated successfully"}

@router.delete("/api/audit-rules/signature-specimens/{specimen_id}")
def delete_signature_specimen(specimen_id: int):
    """
    Soft-delete a signature specimen (set is_active = 0). This is the normal "Delete" action -
    for a genuine data-entry mistake needing permanent removal, use the separate hard-delete
    endpoint below.
    """
    existing = execute_query(
        "SELECT expected_signatory_name FROM signature_specimens WHERE id = ? AND is_active = 1",
        (specimen_id,)
    )
    if not existing:
        raise HTTPException(404, "Active signature specimen not found")
    execute_write("UPDATE signature_specimens SET is_active = 0 WHERE id = ?", (specimen_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Soft-deleted signature specimen ID {specimen_id} ({existing[0]['expected_signatory_name']})")
    return {"message": "Signature specimen deleted (soft-delete) successfully"}

@router.post("/api/audit-rules/signature-specimens/{specimen_id}/restore")
def restore_signature_specimen(specimen_id: int):
    """
    Restore a soft-deleted signature specimen.
    """
    existing = execute_query(
        "SELECT expected_signatory_name FROM signature_specimens WHERE id = ? AND is_active = 0",
        (specimen_id,)
    )
    if not existing:
        raise HTTPException(404, "Inactive signature specimen not found")
    execute_write("UPDATE signature_specimens SET is_active = 1 WHERE id = ?", (specimen_id,))
    log_audit("Admin", "Restore", "AuditRules", f"Restored signature specimen ID {specimen_id} ({existing[0]['expected_signatory_name']})")
    return {"message": "Signature specimen restored successfully"}

@router.delete("/api/audit-rules/signature-specimens/{specimen_id}/hard-delete")
def hard_delete_signature_specimen(specimen_id: int):
    """
    Permanent hard delete - deliberately a separate, low-visibility endpoint (not the normal
    Delete action above) for correcting genuine data-entry mistakes only, not routine personnel
    changes. Works regardless of current is_active state (an active or already-deactivated row
    can both be hard-deleted).
    """
    existing = execute_query("SELECT expected_signatory_name FROM signature_specimens WHERE id = ?", (specimen_id,))
    if not existing:
        raise HTTPException(404, "Signature specimen not found")
    execute_write("DELETE FROM signature_specimens WHERE id = ?", (specimen_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Hard-deleted (permanent) signature specimen ID {specimen_id} ({existing[0]['expected_signatory_name']})")
    return {"message": "Signature specimen permanently deleted"}


# ---------- PER-DOCUMENT IS-REQUIRED FLAG (Automated cross-document validation engine, Phase 4 of 10) ----------
# A dedicated toggle endpoint rather than folding into CategoryCreate/CategoryUpdate's
# document_ids, keeping that already-live payload shape untouched. Bumping matrix_version on
# an actual change is part of the same "what does this Category's matrix require" concept.
# (The former Category-Document approval_required flag/endpoint this once mirrored was
# hard-removed - confirmed requirement, 2026-08-02 - approval requirement now lives solely at
# Document level via the "Approved by" role's Required Authority field.)
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
# A dedicated toggle endpoint, same single-field-toggle-on-an-existing-row convention as the
# Category-Document is_required toggle above - but scoped to the Document itself (no
# category_id), since this flag lives directly on audit_rule_documents, not a Category
# pairing. No matrix_version bump: that concept only applies to a Category's own document/
# requirement state.
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
