# ==============================================
# 📐 AUDIT RULES ROUTER
# ==============================================
# Phase 2 of 9 added Documents CRUD; Phase 3 of 9 added Cross-checks CRUD; Phase 4 of 9
# added Categories CRUD - all three reusable entity types (Documents / Cross-checks /
# Categories) in the Audit Rules module now have CRUD. Categories is the entity a
# voucher will eventually reference (wired up in Phase 9).

import time
import threading
from fastapi import APIRouter, HTTPException, Body, Query, Response, Depends
from pydantic import BaseModel
from typing import Any, Optional, List, Dict
from database import execute_query_pg, execute_write_pg, log_audit

router = APIRouter()

def ensure_no_active_verification_sessions():
    """Prevent changes while any authenticated user is actively inside Verification."""
    active = execute_query_pg(
        "SELECT 1 FROM verification_presence "
        "WHERE ended_at IS NULL AND last_heartbeat > CURRENT_TIMESTAMP - INTERVAL '90 seconds' LIMIT 1"
    )
    if active:
        raise HTTPException(
            409,
            "Audit Rules cannot be changed while a user is actively inside Verification. "
            "Leave Verification first, then retry."
        )

# ==============================================
# TTL CACHE (Modal Caching Phase 3, sub-phase 1 of 3)
# ==============================================
# In-memory, process-level cache for Categories/Documents/guidance reads - sub-phase 2 wires
# the GET endpoints below, and verification.py's guidance/category-for-account lookups, to
# read through this. Single-instance Render deployment, so no Redis is needed. TTL is a
# safety net only; the real invalidation mechanism is explicit, on every write to
# Documents/Cross-checks/Categories (all three feed guidance content) via
# invalidate_audit_rules_cache() below. None of these three entities are client-scoped (no
# client_id column on audit_rule_documents/audit_rule_cross_checks/audit_rule_categories),
# so a write from any one client's session affects every client's cached reads equally -
# invalidation always clears the whole cache rather than a per-client slice, regardless of
# how sub-phase 2 keys its own cache entries.
AUDIT_RULES_CACHE_TTL_SECONDS = 600  # 10 min safety net

_audit_rules_config_version = 1
_audit_rules_cache: Dict[str, tuple] = {}  # key -> (expires_at, config_version, value)
_verification_rulebook_cache: Dict[str, tuple] = {}  # key -> (expires_at, config_version, value)
_audit_rules_cache_lock = threading.Lock()

def get_audit_rules_config_version() -> int:
    """Return the current process-local version for stable Audit Rules configuration."""
    with _audit_rules_cache_lock:
        return _audit_rules_config_version

def cache_get(key: str):
    """Return the cached value for key, or None if absent/expired."""
    with _audit_rules_cache_lock:
        entry = _audit_rules_cache.get(key)
        if entry is None:
            return None
        expires_at, config_version, value = entry
        if time.monotonic() > expires_at or config_version != _audit_rules_config_version:
            del _audit_rules_cache[key]
            return None
        return value

def cache_set(key: str, value, ttl: float = AUDIT_RULES_CACHE_TTL_SECONDS):
    """Store value under key with the given TTL (seconds)."""
    with _audit_rules_cache_lock:
        _audit_rules_cache[key] = (time.monotonic() + ttl, _audit_rules_config_version, value)

def invalidate_audit_rules_cache():
    """
    Clear the entire cache. Called from every Documents/Cross-checks/Categories write
    endpoint below (create/update/delete/restore, plus the two per-field toggle endpoints) -
    see the section comment above for why a full clear, not a scoped one, is correct here.
    """
    with _audit_rules_cache_lock:
        global _audit_rules_config_version
        _audit_rules_config_version += 1
        _audit_rules_cache.clear()
        _verification_rulebook_cache.clear()

def category_for_account_cache_key(client_id: int, account_code: str) -> str:
    """Key shape for the client-scoped category-for-account cache (Modal Caching Phase 3 extension)."""
    return f"category_for_account:{client_id}:{account_code}"

def invalidate_category_for_account_cache(client_id: int):
    """
    Clear only this client's category-for-account cache entries (key prefix
    "category_for_account:{client_id}:"), unlike invalidate_audit_rules_cache()'s
    full clear - this lookup is genuinely per-client, unlike Categories/Documents/
    Cross-checks. Called by TB upload and category-assignment (smart-fill/force-apply)
    writes after any change that could affect this client's resolved categories -
    invalidate-all-for-that-client, not a surgical per-account clear.
    """
    prefix = f"category_for_account:{client_id}:"
    with _audit_rules_cache_lock:
        keys_to_delete = [k for k in _audit_rules_cache if k.startswith(prefix)]
        for k in keys_to_delete:
            del _audit_rules_cache[k]
        rulebook_prefix = f"client_rulebook:{client_id}:"
        rulebook_keys = [k for k in _verification_rulebook_cache if k.startswith(rulebook_prefix)]
        for k in rulebook_keys:
            del _verification_rulebook_cache[k]


def compile_verification_rulebook(client_id: int) -> dict:
    """Compile active Audit Rules into a Verification-facing, read-only rulebook.

    Voucher evidence and calculated results are deliberately outside this stable configuration
    read model. The returned object is a fresh compilation; use
    ``load_client_verification_rulebook`` for the version-aware cached loader.
    """
    warnings: List[dict] = []

    categories = execute_query_pg(
        "SELECT id, name, kb_overview, evidence_type, matrix_version "
        "FROM audit_rule_categories WHERE is_active = TRUE ORDER BY id"
    )
    documents = execute_query_pg(
        "SELECT id, name, kb_note, template_id, risk_rating, transaction_level_approval, "
        "document_type FROM audit_rule_documents WHERE is_active = TRUE ORDER BY id"
    )
    document_ids = [row["id"] for row in documents]
    category_ids = [row["id"] for row in categories]

    roles = execute_query_pg(
        "SELECT document_id, role_name, required_authority, sort_order "
        "FROM audit_rule_document_auth_roles ORDER BY document_id, sort_order"
    )
    fields = execute_query_pg(
        "SELECT document_id, field_name, sort_order, master_column_id "
        "FROM audit_rule_document_fields ORDER BY document_id, sort_order"
    )
    requirements = execute_query_pg(
        "SELECT category_id, document_id, is_required, sort_order "
        "FROM audit_rule_category_documents ORDER BY category_id, sort_order"
    )
    cross_checks = execute_query_pg(
        "SELECT id, name, compare_field, comparison_type, compare_to_voucher_line "
        "FROM audit_rule_cross_checks WHERE is_active = TRUE ORDER BY id"
    )
    cross_check_ids = [row["id"] for row in cross_checks]
    cross_check_documents = execute_query_pg(
        "SELECT cross_check_id, document_id, sort_order "
        "FROM audit_rule_cross_check_documents ORDER BY cross_check_id, sort_order"
    )
    category_cross_checks = execute_query_pg(
        "SELECT category_id, cross_check_id, sort_order "
        "FROM audit_rule_category_cross_checks ORDER BY category_id, sort_order"
    )
    voucher_checks = execute_query_pg(
        "SELECT id, name, authority, sort_order FROM audit_rule_voucher_checks "
        "WHERE is_active = TRUE ORDER BY sort_order, id"
    )
    templates = execute_query_pg(
        "SELECT id, name FROM templates WHERE is_active = TRUE ORDER BY id"
    )
    mappings = execute_query_pg(
        "SELECT template_id, column_id, sort_order FROM template_column_mappings "
        "ORDER BY template_id, sort_order"
    )
    specimens = execute_query_pg(
        "SELECT document_id, role_name, expected_signatory_name, authority "
        "FROM signature_specimens WHERE client_id = %s AND is_active = TRUE "
        "ORDER BY document_id, role_name, id",
        (client_id,)
    )

    document_by_id = {row["id"]: row for row in documents}
    category_by_id = {row["id"]: row for row in categories}
    roles_by_document: Dict[int, List[dict]] = {doc_id: [] for doc_id in document_ids}
    fields_by_document: Dict[int, List[dict]] = {doc_id: [] for doc_id in document_ids}
    required_by_category: Dict[int, List[dict]] = {category_id: [] for category_id in category_ids}
    cross_docs_by_id: Dict[int, List[int]] = {check_id: [] for check_id in cross_check_ids}
    checks_by_category: Dict[int, List[int]] = {category_id: [] for category_id in category_ids}
    specimens_by_key: Dict[tuple, List[dict]] = {}

    for row in roles:
        if row["document_id"] in roles_by_document:
            roles_by_document[row["document_id"]].append({
                "role_name": row["role_name"],
                "required_authority": row.get("required_authority")
            })
    for row in fields:
        if row["document_id"] in fields_by_document:
            fields_by_document[row["document_id"]].append({
                "field_name": row["field_name"],
                "master_column_id": row.get("master_column_id")
            })
    for row in requirements:
        if row["category_id"] in required_by_category:
            required_by_category[row["category_id"]].append(row)
            if row["document_id"] not in document_by_id:
                warnings.append({"code": "inactive_document_requirement", "category_id": row["category_id"], "document_id": row["document_id"]})
    for row in cross_check_documents:
        if row["cross_check_id"] in cross_docs_by_id:
            cross_docs_by_id[row["cross_check_id"]].append(row["document_id"])
            if row["document_id"] not in document_by_id:
                warnings.append({"code": "inactive_cross_check_document", "cross_check_id": row["cross_check_id"], "document_id": row["document_id"]})
    for row in category_cross_checks:
        if row["category_id"] in checks_by_category and row["cross_check_id"] in cross_check_ids:
            checks_by_category[row["category_id"]].append(row["cross_check_id"])
    for row in specimens:
        specimens_by_key.setdefault((row["document_id"], row["role_name"]), []).append({
            "expected_signatory_name": row["expected_signatory_name"],
            "authority": row.get("authority")
        })

    template_ids = {row["id"] for row in templates}
    mappings_by_template: Dict[int, List[dict]] = {}
    for row in mappings:
        mappings_by_template.setdefault(row["template_id"], []).append({
            "column_id": row["column_id"], "sort_order": row["sort_order"]
        })

    def build_document(document_id: int, required: bool) -> dict:
        row = document_by_id[document_id]
        template_id = row.get("template_id")
        if template_id is not None and template_id not in template_ids:
            warnings.append({"code": "inactive_template", "document_id": document_id, "template_id": template_id})
        signature_requirements = []
        for role in roles_by_document.get(document_id, []):
            if role.get("required_authority"):
                signature_requirements.append({
                    "role_name": role["role_name"],
                    "required_authority": role["required_authority"],
                    "specimens": specimens_by_key.get((document_id, role["role_name"]), [])
                })
        return {
            "document_id": document_id, "name": row["name"], "kb_note": row.get("kb_note"),
            "document_type": row.get("document_type") or "linked_template", "template_id": template_id,
            "required": bool(required), "transaction_level_approval": bool(row.get("transaction_level_approval")),
            "risk_rating": row.get("risk_rating"),
            "fields": fields_by_document.get(document_id, []),
            "authority_roles": roles_by_document.get(document_id, []),
            "signature_requirements": signature_requirements,
            "template_mapping_column_ids": [m["column_id"] for m in mappings_by_template.get(template_id, [])]
        }

    cross_check_by_id = {row["id"]: row for row in cross_checks}
    category_profiles = []
    for category in categories:
        category_id = category["id"]
        profile_documents = []
        for requirement in required_by_category[category_id]:
            if requirement["document_id"] in document_by_id:
                profile_documents.append(build_document(requirement["document_id"], requirement["is_required"]))
        profile_cross_checks = []
        for check_id in checks_by_category[category_id]:
            check = cross_check_by_id[check_id]
            doc_ids = cross_docs_by_id.get(check_id, [])
            profile_cross_checks.append({
                "cross_check_id": check_id, "name": check["name"], "compare_field": check["compare_field"],
                "comparison_type": check["comparison_type"],
                "compare_to_voucher_line": bool(check.get("compare_to_voucher_line")),
                "document_ids": doc_ids
            })
            if not all(doc_id in document_by_id for doc_id in doc_ids):
                warnings.append({"code": "cross_check_has_inactive_document", "category_id": category_id, "cross_check_id": check_id})
        category_profiles.append({
            "category_id": category_id, "name": category["name"], "kb_overview": category.get("kb_overview"),
            "matrix_version": category.get("matrix_version", 1), "evidence_type": category.get("evidence_type"),
            "documents": profile_documents, "cross_checks": profile_cross_checks,
            "working_paper_gates": {"approval": {}, "supporting_documents": {}, "account_head_classification": {}, "observation": {}}
        })

    category_name_by_id = {row["id"]: row["name"] for row in categories}
    account_index = load_client_account_category_index(client_id)
    for account_code, value in account_index.items():
        if value["category_id"] is not None and value["category_id"] not in category_by_id:
            warnings.append({"code": "inactive_account_category", "account_code": account_code, "category_id": value["category_id"]})

    return {
        "global_version": get_audit_rules_config_version(),
        "client_id": client_id,
        "account_category_index": account_index,
        "categories": category_profiles,
        "voucher_checks": [dict(row) for row in voucher_checks],
        "templates": [{"id": row["id"], "name": row.get("name"), "mapping_column_ids": [m["column_id"] for m in mappings_by_template.get(row["id"], [])]} for row in templates],
        "warnings": warnings
    }


def load_client_verification_rulebook(client_id: int) -> dict:
    """Return one versioned, client-specific compiled Rulebook from the process cache."""
    version = get_audit_rules_config_version()
    key = f"client_rulebook:{client_id}:{version}"
    with _audit_rules_cache_lock:
        entry = _verification_rulebook_cache.get(key)
        if entry is not None:
            expires_at, cached_version, value = entry
            if time.monotonic() <= expires_at and cached_version == version:
                return value
            _verification_rulebook_cache.pop(key, None)

    rulebook = compile_verification_rulebook(client_id)
    rulebook["global_version"] = version
    with _audit_rules_cache_lock:
        _verification_rulebook_cache[key] = (
            time.monotonic() + AUDIT_RULES_CACHE_TTL_SECONDS,
            version,
            rulebook,
        )
    return rulebook


def load_client_account_category_index(client_id: int, account_codes: Optional[List[str]] = None) -> Dict[str, dict]:
    """Load category resolution data once, preserving Trial Balance-first precedence.

    The latest parsed Trial Balance is authoritative for codes it contains with an active
    Category; the persistent assignment table is the fallback for codes absent from that upload.
    The returned mapping includes every requested code when ``account_codes`` is supplied.
    """
    requested = {str(code) for code in account_codes} if account_codes is not None else None
    result = {code: {"category_id": None, "category_name": None} for code in (requested or set())}
    code_filter = ""
    params: List[Any] = [client_id]
    if requested:
        placeholders = ",".join("%s" for _ in requested)
        code_filter = f" AND a.account_code IN ({placeholders})"

    upload_rows = execute_query_pg(
        "SELECT id FROM trial_balance_uploads WHERE client_id = %s AND status = 'parsed' "
        "ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC LIMIT 1",
        (client_id,)
    )
    if upload_rows:
        tb_params = [upload_rows[0]["id"]] + list(requested or [])
        tb_filter = code_filter.replace("a.account_code", "a.account_code") if requested else ""
        rows = execute_query_pg(
            "SELECT a.account_code, a.category_id, c.name AS category_name "
            "FROM trial_balance_accounts a "
            "JOIN audit_rule_categories c ON c.id = a.category_id AND c.is_active = TRUE "
            "WHERE a.upload_id = %s" + tb_filter,
            tuple(tb_params)
        )
        for row in rows:
            result[str(row["account_code"])] = {"category_id": row["category_id"], "category_name": row["category_name"]}

    remaining = [code for code, value in result.items() if value["category_id"] is None]
    if requested is None:
        assignment_filter = ""
        assignment_params = [client_id]
    else:
        if not remaining:
            return result
        assignment_filter = " AND aca.account_code IN (" + ",".join("%s" for _ in remaining) + ")"
        assignment_params = [client_id] + remaining
    assignment_rows = execute_query_pg(
        "SELECT aca.account_code, aca.category_id, arc.name AS category_name "
        "FROM account_category_assignments aca "
        "LEFT JOIN audit_rule_categories arc ON arc.id = aca.category_id AND arc.is_active = TRUE "
        "WHERE aca.client_id = %s" + assignment_filter,
        tuple(assignment_params)
    )
    for row in assignment_rows:
        code = str(row["account_code"])
        if requested is None or code in result:
            result[code] = {"category_id": row.get("category_id"), "category_name": row.get("category_name")}
    return result

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
    rows = execute_query_pg(
        "SELECT role_name FROM audit_rule_document_auth_roles WHERE document_id = %s ORDER BY sort_order",
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
    rows = execute_query_pg(
        "SELECT role_name, required_authority FROM audit_rule_document_auth_roles WHERE document_id = %s ORDER BY sort_order",
        (document_id,)
    )
    return {r["role_name"]: r["required_authority"] for r in rows if r["required_authority"]}

def get_document_auth_roles_batch(document_ids: List[int]) -> Dict[int, List[str]]:
    """Batched form of get_document_auth_roles() - one IN(...) query for many document_ids."""
    if not document_ids:
        return {}
    ph = ",".join("%s" for _ in document_ids)
    rows = execute_query_pg(
        f"SELECT document_id, role_name FROM audit_rule_document_auth_roles "
        f"WHERE document_id IN ({ph}) ORDER BY document_id, sort_order",
        tuple(document_ids)
    )
    result: Dict[int, List[str]] = {did: [] for did in document_ids}
    for r in rows:
        result[r["document_id"]].append(r["role_name"])
    return result

def get_document_auth_role_authorities_batch(document_ids: List[int]) -> Dict[int, dict]:
    """Batched form of get_document_auth_role_authorities() - one IN(...) query for many document_ids."""
    if not document_ids:
        return {}
    ph = ",".join("%s" for _ in document_ids)
    rows = execute_query_pg(
        f"SELECT document_id, role_name, required_authority FROM audit_rule_document_auth_roles "
        f"WHERE document_id IN ({ph}) ORDER BY document_id, sort_order",
        tuple(document_ids)
    )
    result: Dict[int, dict] = {did: {} for did in document_ids}
    for r in rows:
        if r["required_authority"]:
            result[r["document_id"]][r["role_name"]] = r["required_authority"]
    return result

def get_required_documents_with_authority(category_id: int) -> List[dict]:
    """
    Category's documents flagged is_required=1 (Automated cross-document validation engine,
    Phase 4 of 10), each with its Approved-by-role(s)' Required Authority (only ever set for a
    role matching is_approval_role() - see save_document_auth_roles()). Used by
    routers/verification.py's compute_and_persist_cleared_status() for the Document-level
    Approved-by authority gate (new, confirmed requirement).
    """
    rows = execute_query_pg(
        """
        SELECT d.id as document_id, d.name
        FROM audit_rule_category_documents cd
        JOIN audit_rule_documents d ON cd.document_id = d.id
        WHERE cd.category_id = %s AND cd.is_required = TRUE AND d.is_active = TRUE
        """,
        (category_id,)
    )
    doc_ids = [r["document_id"] for r in rows]
    authorities_by_doc = get_document_auth_role_authorities_batch(doc_ids)
    result = []
    for r in rows:
        authorities = authorities_by_doc.get(r["document_id"], {})
        if authorities:
            result.append({"document_id": r["document_id"], "name": r["name"], "required_authorities": authorities})
    return result

def has_matching_active_specimen(client_id: int, document_id: int, role_name: str, required_authority: str) -> bool:
    """
    True if an ACTIVE Signature Specimen exists for this client+document+role whose Authority
    exactly matches (case/whitespace-insensitive) required_authority.
    """
    rows = execute_query_pg(
        """
        SELECT id FROM signature_specimens
        WHERE client_id = %s AND document_id = %s AND role_name = %s AND is_active = TRUE
          AND TRIM(LOWER(authority)) = TRIM(LOWER(%s))
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
    docs = get_required_documents_with_authority(category_id)
    required_pairs = [
        (doc["document_id"], role_name, required_authority)
        for doc in docs
        for role_name, required_authority in doc["required_authorities"].items()
    ]
    if not required_pairs:
        return True

    # Batched: one query for every required document's active specimens instead of one
    # has_matching_active_specimen() query per (document x role) pair - this gate runs on every
    # save_line_items() call, so the per-pair query loop was a direct contributor to that call's
    # total DB round-trip count. .strip().lower() reproduces the original per-pair query's
    # TRIM(LOWER(...)) = TRIM(LOWER(...)) comparison exactly.
    doc_ids = list({pair[0] for pair in required_pairs})
    placeholders = ",".join("%s" for _ in doc_ids)
    specimens = execute_query_pg(
        f"""
        SELECT document_id, role_name, authority
        FROM signature_specimens
        WHERE client_id = %s AND document_id IN ({placeholders}) AND is_active = TRUE
        """,
        (client_id, *doc_ids)
    )
    specimen_keys = {
        (s["document_id"], s["role_name"], (s["authority"] or "").strip().lower())
        for s in specimens
    }
    for document_id, role_name, required_authority in required_pairs:
        if (document_id, role_name, (required_authority or "").strip().lower()) not in specimen_keys:
            return False
    return True

def get_document_cross_check_fields(document_id: int) -> List[str]:
    rows = execute_query_pg(
        "SELECT field_name FROM audit_rule_document_fields WHERE document_id = %s ORDER BY sort_order",
        (document_id,)
    )
    return [r["field_name"] for r in rows]

def get_document_used_by_count(document_id: int) -> int:
    rows = execute_query_pg(
        "SELECT COUNT(*) as cnt FROM audit_rule_category_documents WHERE document_id = %s",
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
        "is_active": bool(row["is_active"]),
        "auth_roles": get_document_auth_roles(document_id),
        "auth_role_authorities": get_document_auth_role_authorities(document_id),
        "cross_check_fields": get_document_cross_check_fields(document_id),
        "used_by_categories_count": get_document_used_by_count(document_id)
    }

def row_to_document_list(row: dict) -> dict:
    """
    Lean shape for the Documents LIST endpoint (Column trimming, Phase 4 of 13) -
    renderDocumentList() (static/audit_rules.js) only reads id/name/is_active per row, so the
    list endpoint no longer pays for row_to_document()'s 4 extra per-row queries
    (auth_roles/auth_role_authorities/cross_check_fields/used_by_categories_count) that only
    the detail view (selectDocument(), via the single-item GET) actually needs.
    """
    return {"id": row["id"], "name": row["name"], "is_active": bool(row["is_active"])}

def validate_document_template_id(template_id: Optional[int]):
    """Raises HTTPException(400) if template_id is set but doesn't reference an active template."""
    if template_id is None:
        return
    rows = execute_query_pg("SELECT id FROM templates WHERE id = %s AND is_active = TRUE", (template_id,))
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
    execute_write_pg("DELETE FROM audit_rule_document_auth_roles WHERE document_id = %s", (document_id,))
    for idx, role_name in enumerate(auth_roles):
        required_authority = required_authority_map.get(role_name) if is_approval_role(role_name) else None
        execute_write_pg(
            "INSERT INTO audit_rule_document_auth_roles (document_id, role_name, sort_order, required_authority) VALUES (%s, %s, %s, %s)",
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
    rows = execute_query_pg(
        """
        SELECT mc.id FROM master_columns mc
        JOIN template_column_mappings tcm ON tcm.column_id = mc.id
        WHERE tcm.template_id = %s AND LOWER(TRIM(mc.column_name)) = LOWER(TRIM(%s))
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
    doc_rows = execute_query_pg("SELECT template_id FROM audit_rule_documents WHERE id = %s", (document_id,))
    template_id = doc_rows[0]["template_id"] if doc_rows else None
    execute_write_pg("DELETE FROM audit_rule_document_fields WHERE document_id = %s", (document_id,))
    for idx, field_name in enumerate(cross_check_fields):
        master_column_id = resolve_field_name_to_master_column_id(template_id, field_name)
        execute_write_pg(
            "INSERT INTO audit_rule_document_fields (document_id, field_name, sort_order, master_column_id) VALUES (%s, %s, %s, %s)",
            (document_id, field_name, idx, master_column_id)
        )

# ---------- ENDPOINTS ----------

def fetch_paginated_rows(base_query: str, count_query: str, order_by: str, limit: Optional[int], offset: int):
    """
    Shared pagination helper (Fast Data Handling, Phase 10 of 13) for the three list
    endpoints below. limit=None (every existing caller before that phase) runs the query
    unpaginated, exactly as before - fully backward compatible for callers that need the
    full list (pickers/caches, see the frontend call-site inventory). limit set applies
    LIMIT/OFFSET and returns the full unpaginated total so the frontend's "Load more"
    button knows when to stop.

    Returns (rows, total) - total is None when limit is None. Header-setting was moved out
    to each caller (Modal Caching Phase 3, sub-phase 2) so a cache hit and a cache miss can
    both set X-Total-Count identically from a cached (rows, total) tuple, rather than this
    helper being the only place able to set it.
    """
    if limit is None:
        return execute_query_pg(base_query + " " + order_by), None
    total = execute_query_pg(count_query)[0]["cnt"]
    rows = execute_query_pg(base_query + " " + order_by + " LIMIT %s OFFSET %s", (limit, offset))
    return rows, total

@router.get("/api/audit-rules/documents")
def get_documents(response: Response, include_inactive: bool = False, limit: Optional[int] = Query(None, ge=1, le=200), offset: int = Query(0, ge=0)):
    """
    Get all Documents. By default, only active Documents are returned.
    Set include_inactive=True to get all. Optional limit/offset paginate the result
    (Fast Data Handling, Phase 10 of 13) - see fetch_paginated_rows() above. Read-side
    cache wiring (Modal Caching Phase 3, sub-phase 2) - see cache_get()/cache_set() above.
    """
    cache_key = f"documents:list:{include_inactive}:{limit}:{offset}"
    cached = cache_get(cache_key)
    if cached is not None:
        result, total = cached
        if total is not None:
            response.headers["X-Total-Count"] = str(total)
        return result

    where = "" if include_inactive else " WHERE is_active = TRUE"
    base_query = "SELECT * FROM audit_rule_documents" + where
    count_query = "SELECT COUNT(*) as cnt FROM audit_rule_documents" + where
    rows, total = fetch_paginated_rows(base_query, count_query, "ORDER BY name", limit, offset)
    if total is not None:
        response.headers["X-Total-Count"] = str(total)
    result = [row_to_document_list(r) for r in rows]
    cache_set(cache_key, (result, total))
    return result

@router.get("/api/audit-rules/documents/{document_id}")
def get_document(document_id: int):
    cache_key = f"documents:{document_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    rows = execute_query_pg("SELECT * FROM audit_rule_documents WHERE id = %s", (document_id,))
    if not rows:
        raise HTTPException(404, "Document not found")
    result = row_to_document(rows[0])
    cache_set(cache_key, result)
    return result

@router.post("/api/audit-rules/documents", dependencies=[Depends(ensure_no_active_verification_sessions)])
def create_document(document: DocumentCreate):
    document_type = resolve_document_type(document.document_type)
    template_id, cross_check_fields = apply_document_type_constraints(
        document_type, document.template_id, document.cross_check_fields
    )
    validate_document_template_id(template_id)
    validate_document_risk_rating(document.risk_rating)
    document_id = execute_write_pg(
        "INSERT INTO audit_rule_documents (name, kb_note, template_id, risk_rating, document_type) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (document.name, document.kb_note, template_id, document.risk_rating, document_type)
    )
    save_document_auth_roles(document_id, document.auth_roles, document.auth_role_authorities)
    save_document_cross_check_fields(document_id, cross_check_fields)

    log_audit("Admin", "Create", "AuditRules", f"Created Document: {document.name}")
    invalidate_audit_rules_cache()
    return {"message": f"Document '{document.name}' created successfully", "document_id": document_id}

@router.put("/api/audit-rules/documents/{document_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def update_document(document_id: int, document: DocumentUpdate):
    existing = execute_query_pg("SELECT id FROM audit_rule_documents WHERE id = %s", (document_id,))
    if not existing:
        raise HTTPException(404, "Document not found")
    document_type = resolve_document_type(document.document_type)
    template_id, cross_check_fields = apply_document_type_constraints(
        document_type, document.template_id, document.cross_check_fields
    )
    validate_document_template_id(template_id)
    validate_document_risk_rating(document.risk_rating)

    execute_write_pg(
        "UPDATE audit_rule_documents SET name = %s, kb_note = %s, template_id = %s, risk_rating = %s, document_type = %s WHERE id = %s",
        (document.name, document.kb_note, template_id, document.risk_rating, document_type, document_id)
    )
    save_document_auth_roles(document_id, document.auth_roles, document.auth_role_authorities)
    # save_document_cross_check_fields() is still called unconditionally (one write path), but
    # receives an empty list for a physical_bill, so nothing is persisted for it.
    save_document_cross_check_fields(document_id, cross_check_fields)

    log_audit("Admin", "Update", "AuditRules", f"Updated Document ID {document_id}")
    invalidate_audit_rules_cache()
    return {"message": "Document updated successfully"}

@router.delete("/api/audit-rules/documents/{document_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def delete_document(document_id: int):
    """
    Soft-delete a Document (set is_active = FALSE).
    """
    existing = execute_query_pg("SELECT name FROM audit_rule_documents WHERE id = %s AND is_active = TRUE", (document_id,))
    if not existing:
        raise HTTPException(404, "Active Document not found")
    execute_write_pg("UPDATE audit_rule_documents SET is_active = FALSE WHERE id = %s", (document_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Soft-deleted Document: {existing[0]['name']} (ID: {document_id})")
    invalidate_audit_rules_cache()
    return {"message": "Document deleted (soft-delete) successfully"}

@router.post("/api/audit-rules/documents/{document_id}/restore", dependencies=[Depends(ensure_no_active_verification_sessions)])
def restore_document(document_id: int):
    """
    Restore a soft-deleted Document.
    """
    existing = execute_query_pg("SELECT name FROM audit_rule_documents WHERE id = %s AND is_active = FALSE", (document_id,))
    if not existing:
        raise HTTPException(404, "Inactive Document not found")
    execute_write_pg("UPDATE audit_rule_documents SET is_active = TRUE WHERE id = %s", (document_id,))
    log_audit("Admin", "Restore", "AuditRules", f"Restored Document: {existing[0]['name']} (ID: {document_id})")
    invalidate_audit_rules_cache()
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
    rows = execute_query_pg(
        """
        SELECT d.id as document_id, d.name
        FROM audit_rule_cross_check_documents ccd
        JOIN audit_rule_documents d ON ccd.document_id = d.id
        WHERE ccd.cross_check_id = %s
        ORDER BY ccd.sort_order
        """,
        (cross_check_id,)
    )
    return [{"document_id": r["document_id"], "name": r["name"]} for r in rows]

def get_cross_check_used_by_count(cross_check_id: int) -> int:
    rows = execute_query_pg(
        "SELECT COUNT(*) as cnt FROM audit_rule_category_cross_checks WHERE cross_check_id = %s",
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
        "documents": get_cross_check_documents(cross_check_id),
        "used_by_categories_count": get_cross_check_used_by_count(cross_check_id)
    }

def row_to_cross_check_list(row: dict) -> dict:
    """
    Lean shape for the Cross-checks LIST endpoint (Column trimming, Phase 4 of 13) -
    renderCrossCheckList() (static/audit_rules.js) only reads id/name/is_active per row, so the
    list endpoint no longer pays for row_to_cross_check()'s per-row documents JOIN +
    used_by_categories_count query that only the detail view (selectCrossCheck(), via the
    single-item GET) actually needs.
    """
    return {"id": row["id"], "name": row["name"], "is_active": bool(row["is_active"])}

def save_cross_check_documents(cross_check_id: int, document_ids: List[int]):
    """Replace a Cross-check's ordered document list (delete-then-reinsert)."""
    execute_write_pg("DELETE FROM audit_rule_cross_check_documents WHERE cross_check_id = %s", (cross_check_id,))
    for idx, document_id in enumerate(document_ids):
        execute_write_pg(
            "INSERT INTO audit_rule_cross_check_documents (cross_check_id, document_id, sort_order) VALUES (%s, %s, %s)",
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
        doc_rows = execute_query_pg("SELECT name FROM audit_rule_documents WHERE id = %s", (document_id,))
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
def get_cross_checks(response: Response, include_inactive: bool = False, limit: Optional[int] = Query(None, ge=1, le=200), offset: int = Query(0, ge=0)):
    """
    Get all Cross-checks. By default, only active Cross-checks are returned.
    Set include_inactive=True to get all. Optional limit/offset paginate the result
    (Fast Data Handling, Phase 10 of 13) - see fetch_paginated_rows() above. Read-side
    cache wiring (Modal Caching Phase 3, sub-phase 2) - see cache_get()/cache_set() above.
    """
    cache_key = f"cross_checks:list:{include_inactive}:{limit}:{offset}"
    cached = cache_get(cache_key)
    if cached is not None:
        result, total = cached
        if total is not None:
            response.headers["X-Total-Count"] = str(total)
        return result

    where = "" if include_inactive else " WHERE is_active = TRUE"
    base_query = "SELECT * FROM audit_rule_cross_checks" + where
    count_query = "SELECT COUNT(*) as cnt FROM audit_rule_cross_checks" + where
    rows, total = fetch_paginated_rows(base_query, count_query, "ORDER BY name", limit, offset)
    if total is not None:
        response.headers["X-Total-Count"] = str(total)
    result = [row_to_cross_check_list(r) for r in rows]
    cache_set(cache_key, (result, total))
    return result

@router.get("/api/audit-rules/cross-checks/{cross_check_id}")
def get_cross_check(cross_check_id: int):
    cache_key = f"cross_checks:{cross_check_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    rows = execute_query_pg("SELECT * FROM audit_rule_cross_checks WHERE id = %s", (cross_check_id,))
    if not rows:
        raise HTTPException(404, "Cross-check not found")
    result = row_to_cross_check(rows[0])
    cache_set(cache_key, result)
    return result

@router.post("/api/audit-rules/cross-checks", dependencies=[Depends(ensure_no_active_verification_sessions)])
def create_cross_check(cross_check: CrossCheckCreate):
    validate_cross_check_payload(
        cross_check.compare_field, cross_check.comparison_type, cross_check.document_ids,
        cross_check.compare_to_voucher_line
    )

    cross_check_id = execute_write_pg(
        "INSERT INTO audit_rule_cross_checks (name, compare_field, comparison_type, compare_to_voucher_line) VALUES (%s, %s, %s, %s) RETURNING id",
        (cross_check.name, cross_check.compare_field, cross_check.comparison_type, cross_check.compare_to_voucher_line)
    )
    save_cross_check_documents(cross_check_id, cross_check.document_ids)

    log_audit("Admin", "Create", "AuditRules", f"Created Cross-check: {cross_check.name}")
    invalidate_audit_rules_cache()
    return {"message": f"Cross-check '{cross_check.name}' created successfully", "cross_check_id": cross_check_id}

@router.put("/api/audit-rules/cross-checks/{cross_check_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def update_cross_check(cross_check_id: int, cross_check: CrossCheckUpdate):
    existing = execute_query_pg("SELECT id FROM audit_rule_cross_checks WHERE id = %s", (cross_check_id,))
    if not existing:
        raise HTTPException(404, "Cross-check not found")

    validate_cross_check_payload(
        cross_check.compare_field, cross_check.comparison_type, cross_check.document_ids,
        cross_check.compare_to_voucher_line
    )

    execute_write_pg(
        "UPDATE audit_rule_cross_checks SET name = %s, compare_field = %s, comparison_type = %s, compare_to_voucher_line = %s WHERE id = %s",
        (cross_check.name, cross_check.compare_field, cross_check.comparison_type, cross_check.compare_to_voucher_line, cross_check_id)
    )
    save_cross_check_documents(cross_check_id, cross_check.document_ids)

    log_audit("Admin", "Update", "AuditRules", f"Updated Cross-check ID {cross_check_id}")
    invalidate_audit_rules_cache()
    return {"message": "Cross-check updated successfully"}

@router.delete("/api/audit-rules/cross-checks/{cross_check_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def delete_cross_check(cross_check_id: int):
    """
    Soft-delete a Cross-check (set is_active = FALSE).
    """
    existing = execute_query_pg("SELECT name FROM audit_rule_cross_checks WHERE id = %s AND is_active = TRUE", (cross_check_id,))
    if not existing:
        raise HTTPException(404, "Active Cross-check not found")
    execute_write_pg("UPDATE audit_rule_cross_checks SET is_active = FALSE WHERE id = %s", (cross_check_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Soft-deleted Cross-check: {existing[0]['name']} (ID: {cross_check_id})")
    invalidate_audit_rules_cache()
    return {"message": "Cross-check deleted (soft-delete) successfully"}

@router.post("/api/audit-rules/cross-checks/{cross_check_id}/restore", dependencies=[Depends(ensure_no_active_verification_sessions)])
def restore_cross_check(cross_check_id: int):
    """
    Restore a soft-deleted Cross-check.
    """
    existing = execute_query_pg("SELECT name FROM audit_rule_cross_checks WHERE id = %s AND is_active = FALSE", (cross_check_id,))
    if not existing:
        raise HTTPException(404, "Inactive Cross-check not found")
    execute_write_pg("UPDATE audit_rule_cross_checks SET is_active = TRUE WHERE id = %s", (cross_check_id,))
    log_audit("Admin", "Restore", "AuditRules", f"Restored Cross-check: {existing[0]['name']} (ID: {cross_check_id})")
    invalidate_audit_rules_cache()
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
    rows = execute_query_pg(
        """
        SELECT d.id as document_id, d.name, cd.is_required, d.is_active, d.document_type
        FROM audit_rule_category_documents cd
        JOIN audit_rule_documents d ON cd.document_id = d.id
        WHERE cd.category_id = %s
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
    rows = execute_query_pg(
        "SELECT document_id, is_required FROM audit_rule_category_documents WHERE category_id = %s",
        (category_id,)
    )
    return {r["document_id"]: r["is_required"] for r in rows}

def get_category_cross_checks(category_id: int) -> List[dict]:
    rows = execute_query_pg(
        """
        SELECT c.id as cross_check_id, c.name
        FROM audit_rule_category_cross_checks cc
        JOIN audit_rule_cross_checks c ON cc.cross_check_id = c.id
        WHERE cc.category_id = %s
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
    rows = execute_query_pg(
        """
        SELECT COUNT(*) as cnt
        FROM audit_rule_category_cross_checks cc
        JOIN audit_rule_cross_checks c ON cc.cross_check_id = c.id
        WHERE cc.category_id = %s AND c.is_active = TRUE
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
    rows = execute_query_pg(
        """
        SELECT d.id as document_id, d.name, d.kb_note, d.template_id, d.risk_rating, d.transaction_level_approval, d.document_type
        FROM audit_rule_category_documents cd
        JOIN audit_rule_documents d ON cd.document_id = d.id
        WHERE cd.category_id = %s AND d.is_active = TRUE
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
    rows = execute_query_pg(
        """
        SELECT c.id as cross_check_id, c.name, c.compare_field, c.comparison_type
        FROM audit_rule_category_cross_checks cc
        JOIN audit_rule_cross_checks c ON cc.cross_check_id = c.id
        WHERE cc.category_id = %s AND c.is_active = TRUE
        ORDER BY cc.sort_order
        """,
        (category_id,)
    )
    result = []
    for r in rows:
        doc_rows = execute_query_pg(
            """
            SELECT d.name
            FROM audit_rule_cross_check_documents ccd
            JOIN audit_rule_documents d ON ccd.document_id = d.id
            WHERE ccd.cross_check_id = %s
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
    execute_write_pg("DELETE FROM audit_rule_category_documents WHERE category_id = %s", (category_id,))
    for idx, document_id in enumerate(document_ids):
        is_required = existing_requireds.get(document_id, True)
        execute_write_pg(
            "INSERT INTO audit_rule_category_documents (category_id, document_id, sort_order, is_required) VALUES (%s, %s, %s, %s)",
            (category_id, document_id, idx, is_required)
        )

def bump_category_matrix_version(category_id: int):
    """
    Increment audit_rule_categories.matrix_version (Validation Rules, Phase 2 of 7) - called
    automatically whenever a Category's document list or is_required flags change, never
    as a separate manual action. Lets a later phase's verification_details.matrix_version_applied
    detect a stale classification without silently reclassifying an already-Cleared voucher.
    """
    execute_write_pg(
        "UPDATE audit_rule_categories SET matrix_version = matrix_version + 1 WHERE id = %s",
        (category_id,)
    )

def save_category_cross_checks(category_id: int, cross_check_ids: List[int]):
    """Replace a Category's Cross-check list (delete-then-reinsert)."""
    execute_write_pg("DELETE FROM audit_rule_category_cross_checks WHERE category_id = %s", (category_id,))
    for idx, cross_check_id in enumerate(cross_check_ids):
        execute_write_pg(
            "INSERT INTO audit_rule_category_cross_checks (category_id, cross_check_id, sort_order) VALUES (%s, %s, %s)",
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
        rows = execute_query_pg("SELECT id FROM audit_rule_documents WHERE id = %s", (document_id,))
        if not rows:
            raise HTTPException(400, f"Document ID {document_id} not found")
        doc_id_set.add(document_id)

    for cross_check_id in cross_check_ids:
        cc_rows = execute_query_pg("SELECT name FROM audit_rule_cross_checks WHERE id = %s", (cross_check_id,))
        if not cc_rows:
            raise HTTPException(400, f"Cross-check ID {cross_check_id} not found")
        cc_doc_rows = execute_query_pg(
            "SELECT document_id FROM audit_rule_cross_check_documents WHERE cross_check_id = %s",
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
def get_categories(response: Response, include_inactive: bool = False, limit: Optional[int] = Query(None, ge=1, le=200), offset: int = Query(0, ge=0)):
    """
    Get all Categories. By default, only active Categories are returned.
    Set include_inactive=True to get all. Optional limit/offset paginate the result
    (Fast Data Handling, Phase 10 of 13) - see fetch_paginated_rows() above. Read-side
    cache wiring (Modal Caching Phase 3, sub-phase 2) - see cache_get()/cache_set() above.
    """
    cache_key = f"categories:list:{include_inactive}:{limit}:{offset}"
    cached = cache_get(cache_key)
    if cached is not None:
        result, total = cached
        if total is not None:
            response.headers["X-Total-Count"] = str(total)
        return result

    where = "" if include_inactive else " WHERE is_active = TRUE"
    base_query = "SELECT * FROM audit_rule_categories" + where
    count_query = "SELECT COUNT(*) as cnt FROM audit_rule_categories" + where
    rows, total = fetch_paginated_rows(base_query, count_query, "ORDER BY name", limit, offset)
    if total is not None:
        response.headers["X-Total-Count"] = str(total)
    result = [row_to_category(r) for r in rows]
    cache_set(cache_key, (result, total))
    return result

@router.get("/api/audit-rules/categories/{category_id}")
def get_category(category_id: int):
    cache_key = f"categories:{category_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    rows = execute_query_pg("SELECT * FROM audit_rule_categories WHERE id = %s", (category_id,))
    if not rows:
        raise HTTPException(404, "Category not found")
    result = row_to_category(rows[0])
    cache_set(cache_key, result)
    return result

@router.post("/api/audit-rules/categories", dependencies=[Depends(ensure_no_active_verification_sessions)])
def create_category(category: CategoryCreate):
    validate_category_evidence_type(category.evidence_type)
    validate_category_payload(category.document_ids, category.cross_check_ids)

    category_id = execute_write_pg(
        "INSERT INTO audit_rule_categories (name, kb_overview, evidence_type) VALUES (%s, %s, %s) RETURNING id",
        (category.name, category.kb_overview, category.evidence_type)
    )
    save_category_documents(category_id, category.document_ids)
    save_category_cross_checks(category_id, category.cross_check_ids)

    log_audit("Admin", "Create", "AuditRules", f"Created Category: {category.name}")
    invalidate_audit_rules_cache()
    return {"message": f"Category '{category.name}' created successfully", "category_id": category_id}

@router.put("/api/audit-rules/categories/{category_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def update_category(category_id: int, category: CategoryUpdate):
    existing = execute_query_pg("SELECT id FROM audit_rule_categories WHERE id = %s", (category_id,))
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

    execute_write_pg(
        "UPDATE audit_rule_categories SET name = %s, kb_overview = %s, evidence_type = %s WHERE id = %s",
        (category.name, category.kb_overview, category.evidence_type, category_id)
    )
    save_category_documents(category_id, category.document_ids)
    save_category_cross_checks(category_id, category.cross_check_ids)

    if old_document_ids != set(category.document_ids):
        bump_category_matrix_version(category_id)

    log_audit("Admin", "Update", "AuditRules", f"Updated Category ID {category_id}")
    invalidate_audit_rules_cache()
    return {"message": "Category updated successfully"}

@router.delete("/api/audit-rules/categories/{category_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def delete_category(category_id: int):
    """
    Soft-delete a Category (set is_active = FALSE).
    """
    existing = execute_query_pg("SELECT name FROM audit_rule_categories WHERE id = %s AND is_active = TRUE", (category_id,))
    if not existing:
        raise HTTPException(404, "Active Category not found")
    execute_write_pg("UPDATE audit_rule_categories SET is_active = FALSE WHERE id = %s", (category_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Soft-deleted Category: {existing[0]['name']} (ID: {category_id})")
    invalidate_audit_rules_cache()
    return {"message": "Category deleted (soft-delete) successfully"}

@router.post("/api/audit-rules/categories/{category_id}/restore", dependencies=[Depends(ensure_no_active_verification_sessions)])
def restore_category(category_id: int):
    """
    Restore a soft-deleted Category.
    """
    existing = execute_query_pg("SELECT name FROM audit_rule_categories WHERE id = %s AND is_active = FALSE", (category_id,))
    if not existing:
        raise HTTPException(404, "Inactive Category not found")
    execute_write_pg("UPDATE audit_rule_categories SET is_active = TRUE WHERE id = %s", (category_id,))
    log_audit("Admin", "Restore", "AuditRules", f"Restored Category: {existing[0]['name']} (ID: {category_id})")
    invalidate_audit_rules_cache()
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
    # Read-side cache wiring (Modal Caching Phase 3, sub-phase 2) - see cache_get()/cache_set() above.
    cache_key = f"categories:{category_id}:cleared-requirements"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    existing = execute_query_pg("SELECT id, evidence_type FROM audit_rule_categories WHERE id = %s", (category_id,))
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

    result = {
        "category_id": category_id,
        "evidence_type": existing[0]["evidence_type"],
        "voucher_level_checks_required": True,
        "account_head_required": True,
        "cross_checks_required": get_category_active_cross_checks_count(category_id) > 0,
        "required_documents": required_documents,
    }
    cache_set(cache_key, result)
    return result


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
    rows = execute_query_pg(
        "SELECT role_name FROM audit_rule_voucher_check_auth_roles WHERE voucher_check_id = %s ORDER BY sort_order",
        (voucher_check_id,)
    )
    return [r["role_name"] for r in rows]

def save_voucher_check_auth_roles(voucher_check_id: int, auth_roles: List[str]):
    """Replace a voucher check's authorisation-roles list (delete-then-reinsert)."""
    execute_write_pg("DELETE FROM audit_rule_voucher_check_auth_roles WHERE voucher_check_id = %s", (voucher_check_id,))
    for idx, role_name in enumerate(auth_roles):
        execute_write_pg(
            "INSERT INTO audit_rule_voucher_check_auth_roles (voucher_check_id, role_name, sort_order) VALUES (%s, %s, %s)",
            (voucher_check_id, role_name, idx)
        )

def row_to_voucher_check(row: dict) -> dict:
    voucher_check_id = row["id"]
    return {
        "id": voucher_check_id,
        "name": row["name"],
        "authority": row.get("authority"),
        "is_active": bool(row["is_active"]),
    }

def get_next_voucher_check_sort_order() -> int:
    """
    Next sort_order value to append a new check at the end of the list. Computed across all
    rows (active or not) so a soft-deleted row's slot isn't reused unexpectedly.
    """
    rows = execute_query_pg("SELECT COALESCE(MAX(sort_order), -1) as max_order FROM audit_rule_voucher_checks")
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
        query += " WHERE is_active = TRUE"
    query += " ORDER BY sort_order"
    rows = execute_query_pg(query)
    return [row_to_voucher_check(r) for r in rows]

@router.get("/api/audit-rules/voucher-checks/{voucher_check_id}")
def get_voucher_check(voucher_check_id: int):
    rows = execute_query_pg("SELECT * FROM audit_rule_voucher_checks WHERE id = %s", (voucher_check_id,))
    if not rows:
        raise HTTPException(404, "Voucher check not found")
    return row_to_voucher_check(rows[0])

@router.post("/api/audit-rules/voucher-checks", dependencies=[Depends(ensure_no_active_verification_sessions)])
def create_voucher_check(voucher_check: VoucherCheckCreate):
    sort_order = get_next_voucher_check_sort_order()
    voucher_check_id = execute_write_pg(
        "INSERT INTO audit_rule_voucher_checks (name, sort_order, authority) VALUES (%s, %s, %s) RETURNING id",
        (voucher_check.name, sort_order, voucher_check.authority)
    )

    log_audit("Admin", "Create", "AuditRules", f"Created voucher check: {voucher_check.name}")
    invalidate_audit_rules_cache()
    return {"message": f"Voucher check '{voucher_check.name}' created successfully", "voucher_check_id": voucher_check_id}

@router.put("/api/audit-rules/voucher-checks/{voucher_check_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def update_voucher_check(voucher_check_id: int, voucher_check: VoucherCheckUpdate):
    existing = execute_query_pg("SELECT id FROM audit_rule_voucher_checks WHERE id = %s", (voucher_check_id,))
    if not existing:
        raise HTTPException(404, "Voucher check not found")

    execute_write_pg(
        "UPDATE audit_rule_voucher_checks SET name = %s, authority = %s WHERE id = %s",
        (voucher_check.name, voucher_check.authority, voucher_check_id)
    )

    log_audit("Admin", "Update", "AuditRules", f"Updated voucher check ID {voucher_check_id}")
    invalidate_audit_rules_cache()
    return {"message": "Voucher check updated successfully"}

@router.delete("/api/audit-rules/voucher-checks/{voucher_check_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def delete_voucher_check(voucher_check_id: int):
    """
    Soft-delete a voucher check (set is_active = FALSE).
    """
    existing = execute_query_pg("SELECT name FROM audit_rule_voucher_checks WHERE id = %s AND is_active = TRUE", (voucher_check_id,))
    if not existing:
        raise HTTPException(404, "Active voucher check not found")
    execute_write_pg("UPDATE audit_rule_voucher_checks SET is_active = FALSE WHERE id = %s", (voucher_check_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Soft-deleted voucher check: {existing[0]['name']} (ID: {voucher_check_id})")
    invalidate_audit_rules_cache()
    return {"message": "Voucher check deleted (soft-delete) successfully"}

@router.post("/api/audit-rules/voucher-checks/{voucher_check_id}/restore", dependencies=[Depends(ensure_no_active_verification_sessions)])
def restore_voucher_check(voucher_check_id: int):
    """
    Restore a soft-deleted voucher check.
    """
    existing = execute_query_pg("SELECT name FROM audit_rule_voucher_checks WHERE id = %s AND is_active = FALSE", (voucher_check_id,))
    if not existing:
        raise HTTPException(404, "Inactive voucher check not found")
    execute_write_pg("UPDATE audit_rule_voucher_checks SET is_active = TRUE WHERE id = %s", (voucher_check_id,))
    log_audit("Admin", "Restore", "AuditRules", f"Restored voucher check: {existing[0]['name']} (ID: {voucher_check_id})")
    invalidate_audit_rules_cache()
    return {"message": "Voucher check restored successfully"}

@router.post("/api/audit-rules/voucher-checks/reorder", dependencies=[Depends(ensure_no_active_verification_sessions)])
def reorder_voucher_checks(check_ids: List[int] = Body(...)):
    """Update sort_order for all voucher checks, same pattern as reorder_template_columns()."""
    for idx, check_id in enumerate(check_ids):
        execute_write_pg(
            "UPDATE audit_rule_voucher_checks SET sort_order = %s WHERE id = %s",
            (idx, check_id)
        )
    log_audit("Admin", "Reorder", "AuditRules", "Reordered voucher checks")
    invalidate_audit_rules_cache()
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
    rows = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not rows:
        raise HTTPException(400, f"Client ID {client_id} not found or inactive")

def validate_signature_specimen_payload(document_id: int, role_name: str):
    """
    Raises HTTPException(400) if document_id doesn't exist, or role_name isn't one of that
    Document's configured auth roles - mirrors validate_cross_check_payload()'s compare_field
    check against a Document's cross_check_fields.
    """
    doc_rows = execute_query_pg("SELECT name FROM audit_rule_documents WHERE id = %s", (document_id,))
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
        "SELECT id FROM signature_specimens WHERE client_id = %s AND document_id = %s AND role_name = %s "
        "AND is_active = TRUE AND TRIM(LOWER(expected_signatory_name)) = TRIM(LOWER(%s))"
    )
    params = [client_id, document_id, role_name, expected_signatory_name]
    if exclude_id is not None:
        query += " AND id != %s"
        params.append(exclude_id)
    existing = execute_query_pg(query, tuple(params))
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
    query = SIGNATURE_SPECIMEN_SELECT + " WHERE s.client_id = %s"
    params = [client_id]
    if document_id is not None:
        query += " AND s.document_id = %s"
        params.append(document_id)
    if not include_inactive:
        query += " AND s.is_active = TRUE"
    query += " ORDER BY d.name, s.role_name"
    rows = execute_query_pg(query, tuple(params))
    return [row_to_signature_specimen(r) for r in rows]

@router.get("/api/audit-rules/signature-specimens/{specimen_id}")
def get_signature_specimen(specimen_id: int):
    rows = execute_query_pg(SIGNATURE_SPECIMEN_SELECT + " WHERE s.id = %s", (specimen_id,))
    if not rows:
        raise HTTPException(404, "Signature specimen not found")
    return row_to_signature_specimen(rows[0])

@router.post("/api/audit-rules/signature-specimens", dependencies=[Depends(ensure_no_active_verification_sessions)])
def create_signature_specimen(specimen: SignatureSpecimenCreate):
    validate_client_id(specimen.client_id)
    validate_signature_specimen_payload(specimen.document_id, specimen.role_name)
    validate_no_duplicate_active_signatory(specimen.client_id, specimen.document_id, specimen.role_name, specimen.expected_signatory_name)

    specimen_id = execute_write_pg(
        "INSERT INTO signature_specimens (client_id, document_id, role_name, expected_signatory_name, authority) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (specimen.client_id, specimen.document_id, specimen.role_name, specimen.expected_signatory_name, specimen.authority)
    )

    log_audit(
        "Admin", "Create", "AuditRules",
        f"Created signature specimen for client {specimen.client_id}, document {specimen.document_id}, role '{specimen.role_name}'"
    )
    invalidate_audit_rules_cache()
    return {"message": "Signature specimen created successfully", "specimen_id": specimen_id}

@router.put("/api/audit-rules/signature-specimens/{specimen_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def update_signature_specimen(specimen_id: int, specimen: SignatureSpecimenUpdate):
    existing = execute_query_pg("SELECT id FROM signature_specimens WHERE id = %s", (specimen_id,))
    if not existing:
        raise HTTPException(404, "Signature specimen not found")

    validate_client_id(specimen.client_id)
    validate_signature_specimen_payload(specimen.document_id, specimen.role_name)
    validate_no_duplicate_active_signatory(specimen.client_id, specimen.document_id, specimen.role_name, specimen.expected_signatory_name, exclude_id=specimen_id)

    execute_write_pg(
        "UPDATE signature_specimens SET client_id = %s, document_id = %s, role_name = %s, "
        "expected_signatory_name = %s, authority = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (specimen.client_id, specimen.document_id, specimen.role_name, specimen.expected_signatory_name, specimen.authority, specimen_id)
    )

    log_audit("Admin", "Update", "AuditRules", f"Updated signature specimen ID {specimen_id}")
    invalidate_audit_rules_cache()
    return {"message": "Signature specimen updated successfully"}

@router.delete("/api/audit-rules/signature-specimens/{specimen_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def delete_signature_specimen(specimen_id: int):
    """
    Soft-delete a signature specimen (set is_active = FALSE). This is the normal "Delete" action -
    for a genuine data-entry mistake needing permanent removal, use the separate hard-delete
    endpoint below.
    """
    existing = execute_query_pg(
        "SELECT expected_signatory_name FROM signature_specimens WHERE id = %s AND is_active = TRUE",
        (specimen_id,)
    )
    if not existing:
        raise HTTPException(404, "Active signature specimen not found")
    execute_write_pg("UPDATE signature_specimens SET is_active = FALSE WHERE id = %s", (specimen_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Soft-deleted signature specimen ID {specimen_id} ({existing[0]['expected_signatory_name']})")
    invalidate_audit_rules_cache()
    return {"message": "Signature specimen deleted (soft-delete) successfully"}

@router.post("/api/audit-rules/signature-specimens/{specimen_id}/restore", dependencies=[Depends(ensure_no_active_verification_sessions)])
def restore_signature_specimen(specimen_id: int):
    """
    Restore a soft-deleted signature specimen.
    """
    existing = execute_query_pg(
        "SELECT expected_signatory_name FROM signature_specimens WHERE id = %s AND is_active = FALSE",
        (specimen_id,)
    )
    if not existing:
        raise HTTPException(404, "Inactive signature specimen not found")
    execute_write_pg("UPDATE signature_specimens SET is_active = TRUE WHERE id = %s", (specimen_id,))
    log_audit("Admin", "Restore", "AuditRules", f"Restored signature specimen ID {specimen_id} ({existing[0]['expected_signatory_name']})")
    invalidate_audit_rules_cache()
    return {"message": "Signature specimen restored successfully"}

@router.delete("/api/audit-rules/signature-specimens/{specimen_id}/hard-delete", dependencies=[Depends(ensure_no_active_verification_sessions)])
def hard_delete_signature_specimen(specimen_id: int):
    """
    Permanent hard delete - deliberately a separate, low-visibility endpoint (not the normal
    Delete action above) for correcting genuine data-entry mistakes only, not routine personnel
    changes. Works regardless of current is_active state (an active or already-deactivated row
    can both be hard-deleted).
    """
    existing = execute_query_pg("SELECT expected_signatory_name FROM signature_specimens WHERE id = %s", (specimen_id,))
    if not existing:
        raise HTTPException(404, "Signature specimen not found")
    execute_write_pg("DELETE FROM signature_specimens WHERE id = %s", (specimen_id,))
    log_audit("Admin", "Delete", "AuditRules", f"Hard-deleted (permanent) signature specimen ID {specimen_id} ({existing[0]['expected_signatory_name']})")
    invalidate_audit_rules_cache()
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

@router.put("/api/audit-rules/categories/{category_id}/documents/{document_id}/required", dependencies=[Depends(ensure_no_active_verification_sessions)])
def update_category_document_required(category_id: int, document_id: int, payload: CategoryDocumentRequiredUpdate):
    existing = execute_query_pg(
        "SELECT is_required FROM audit_rule_category_documents WHERE category_id = %s AND document_id = %s",
        (category_id, document_id)
    )
    if not existing:
        raise HTTPException(404, "This Document is not part of this Category")

    new_value = payload.is_required
    changed = bool(existing[0]["is_required"]) != payload.is_required

    execute_write_pg(
        "UPDATE audit_rule_category_documents SET is_required = %s WHERE category_id = %s AND document_id = %s",
        (new_value, category_id, document_id)
    )
    if changed:
        bump_category_matrix_version(category_id)

    log_audit(
        "Admin", "Update", "AuditRules",
        f"Set is_required={payload.is_required} for Document ID {document_id} in Category ID {category_id}"
    )
    invalidate_audit_rules_cache()
    return {"category_id": category_id, "document_id": document_id, "is_required": payload.is_required}


# ---------- PER-DOCUMENT TRANSACTION-LEVEL-APPROVAL FLAG (new, confirmed requirement) ----------
# A dedicated toggle endpoint, same single-field-toggle-on-an-existing-row convention as the
# Category-Document is_required toggle above - but scoped to the Document itself (no
# category_id), since this flag lives directly on audit_rule_documents, not a Category
# pairing. No matrix_version bump: that concept only applies to a Category's own document/
# requirement state.
class DocumentTransactionLevelApprovalUpdate(BaseModel):
    transaction_level_approval: bool

@router.put("/api/audit-rules/documents/{document_id}/transaction-level-approval", dependencies=[Depends(ensure_no_active_verification_sessions)])
def update_document_transaction_level_approval(document_id: int, payload: DocumentTransactionLevelApprovalUpdate):
    existing = execute_query_pg("SELECT id FROM audit_rule_documents WHERE id = %s", (document_id,))
    if not existing:
        raise HTTPException(404, "Document not found")

    new_value = payload.transaction_level_approval
    execute_write_pg(
        "UPDATE audit_rule_documents SET transaction_level_approval = %s WHERE id = %s",
        (new_value, document_id)
    )

    log_audit(
        "Admin", "Update", "AuditRules",
        f"Set transaction_level_approval={payload.transaction_level_approval} for Document ID {document_id}"
    )
    invalidate_audit_rules_cache()
    return {"document_id": document_id, "transaction_level_approval": payload.transaction_level_approval}
