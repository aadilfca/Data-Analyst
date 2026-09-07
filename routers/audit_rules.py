# ==============================================
# 📐 AUDIT RULES ROUTER
# ==============================================
# Phase 2 of 9 added Documents CRUD; Phase 3 of 9 added Cross-checks CRUD; Phase 4 of 9
# added Categories CRUD - all three reusable entity types (Documents / Cross-checks /
# Categories) in the Audit Rules module now have CRUD. Categories is the entity a
# voucher will eventually reference (wired up in Phase 9).

import json
import time
import threading
from fastapi import APIRouter, HTTPException, Body, Query, Response, Depends
from pydantic import BaseModel
from psycopg2.extras import Json
import database
from typing import Any, Optional, List, Dict
from database import (
    execute_query_pg,
    execute_write_pg,
    log_audit,
    bump_persisted_audit_rules_config_version,
    get_persisted_audit_rules_config_version,
    record_audit_rules_config_change,
    pg_transaction,
)

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
_audit_rules_config_version_source = None
_audit_rules_cache: Dict[str, tuple] = {}  # key -> (expires_at, config_version, value)
_verification_rulebook_cache: Dict[str, tuple] = {}  # key -> (expires_at, config_version, value)
_audit_rules_cache_lock = threading.Lock()

def get_audit_rules_config_version() -> int:
    """Return the durable version for stable Audit Rules configuration."""
    global _audit_rules_config_version, _audit_rules_config_version_source
    with _audit_rules_cache_lock:
        source = "postgres" if database.is_postgres_schema_ready() else "sqlite"
        if _audit_rules_config_version_source != source:
            persisted = get_persisted_audit_rules_config_version()
            if persisted is not None:
                _audit_rules_config_version = persisted
            _audit_rules_config_version_source = source
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

def invalidate_audit_rules_cache(change=None):
    """
    Clear the entire cache. Called from every Documents/Cross-checks/Categories write
    endpoint below (create/update/delete/restore, plus the two per-field toggle endpoints) -
    see the section comment above for why a full clear, not a scoped one, is correct here.
    """
    with _audit_rules_cache_lock:
        global _audit_rules_config_version, _audit_rules_config_version_source
        persisted = bump_persisted_audit_rules_config_version()
        if persisted is None:
            _audit_rules_config_version += 1
        else:
            _audit_rules_config_version = int(persisted)
        _audit_rules_config_version_source = (
            "postgres" if database.is_postgres_schema_ready() else "sqlite"
        )
        _audit_rules_cache.clear()
        _verification_rulebook_cache.clear()
        change = change or {
            "entity_type": "Audit Rules configuration",
            "entity_name": None,
            "action": "Updated",
        }
        record_audit_rules_config_change(
            _audit_rules_config_version,
            change.get("entity_type", "Audit Rules configuration"),
            change.get("entity_id"),
            change.get("entity_name"),
            change.get("action", "Updated"),
            change.get("changed_by", "System"),
        )

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


def _parse_category_validation_rules(value: Any) -> dict:
    if not value:
        return {"procedure_evidence": {}, "mathematical_cross_checks": []}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    if not isinstance(value, dict):
        value = {}
    procedure_evidence = value.get("procedure_evidence")
    if not isinstance(procedure_evidence, dict):
        procedure_evidence = {}
    mathematical_cross_checks = value.get("mathematical_cross_checks")
    if not isinstance(mathematical_cross_checks, list):
        mathematical_cross_checks = []
    return {
        "procedure_evidence": procedure_evidence,
        "mathematical_cross_checks": mathematical_cross_checks,
    }


def _build_selected_verification_fields(
    template_id: Optional[int], stored_policy: Any, mappings_by_template: Dict[int, List[dict]]
) -> List[dict]:
    """Return the exact Template columns the future Procedure window should project."""
    if template_id is None:
        return []
    columns = mappings_by_template.get(template_id, [])
    parsed = _parse_verification_field_policy(stored_policy)
    if parsed is None:
        return [
            {
                "column_id": column["column_id"],
                "column_name": column.get("column_name"),
                "data_type": column.get("data_type"),
                "selection": "default",
                "sort_order": index,
            }
            for index, column in enumerate(columns)
        ]

    columns_by_id = {int(column["column_id"]): column for column in columns}
    selected = []
    core_ids = parsed.get("core") or {}
    for key, column_id in core_ids.items():
        column = columns_by_id.get(column_id)
        if column is not None:
            selected.append({
                "column_id": column["column_id"],
                "column_name": column.get("column_name"),
                "data_type": column.get("data_type"),
                "selection": "core",
                "key": key,
                "sort_order": len(selected),
            })
    for optional in parsed.get("optional") or []:
        column = columns_by_id.get(optional.get("column_id"))
        if column is not None:
            selected.append({
                "column_id": column["column_id"],
                "column_name": column.get("column_name"),
                "data_type": column.get("data_type"),
                "selection": "optional",
                "risk_level": optional.get("risk_level"),
                "sort_order": len(selected),
            })
    return selected


def _build_procedure_evidence_contract(
    validation_rules: dict,
    document_by_id: Dict[int, dict],
    roles_by_document: Dict[int, List[dict]],
    specimens_by_key: Dict[tuple, List[dict]],
) -> dict:
    """Resolve saved Category checks to the Document/role data needed by Verification later."""
    evidence = validation_rules.get("procedure_evidence") or {}
    raw_checks = evidence.get("checks") if isinstance(evidence, dict) else []
    if not isinstance(raw_checks, list):
        raw_checks = []
    checks = []
    for raw_check in raw_checks:
        if not isinstance(raw_check, dict):
            continue
        check = dict(raw_check)
        document_id = raw_check.get("document_id")
        try:
            document_id = int(document_id) if document_id is not None else None
        except (TypeError, ValueError):
            document_id = None
        document = document_by_id.get(document_id)
        role_name = next(
            (raw_check.get(key) for key in ("role", "role_name", "signature") if raw_check.get(key)),
            None,
        )
        selected_detail = raw_check.get("sub_value") or raw_check.get("detail") or raw_check.get("selected_detail")
        if document is not None:
            available_roles = roles_by_document.get(document_id, [])
            if not role_name and selected_detail:
                detail_text = str(selected_detail).casefold()
                role_name = next(
                    (
                        role["role_name"]
                        for role in available_roles
                        if str(role["role_name"]).casefold() == detail_text
                        or any(
                            str(specimen.get("expected_signatory_name", "")).casefold() == detail_text
                            for specimen in specimens_by_key.get((document_id, role["role_name"]), [])
                        )
                    ),
                    None,
                )
            configured_signatories = []
            if role_name:
                configured_signatories = [
                    specimen
                    for specimen in specimens_by_key.get((document_id, role_name), [])
                ]
            check.update({
                "document_id": document_id,
                "document_name": document.get("name"),
                "document_type": document.get("document_type") or "linked_template",
                "template_id": document.get("template_id"),
                "available_roles": available_roles,
                "role_name": role_name,
                "configured_signatories": configured_signatories,
            })
        else:
            check.update({
                "document_id": document_id,
                "document_name": None,
                "document_type": None,
                "template_id": None,
                "available_roles": [],
                "role_name": role_name,
                "configured_signatories": [],
            })
        check["selected_detail"] = selected_detail
        check["risk"] = raw_check.get("risk") or raw_check.get("risk_rating") or "Medium"
        checks.append(check)
    return {**evidence, "checks": checks}


def _build_cross_check_matrix_contract(
    check_id: int,
    document_ids: List[int],
    document_by_id: Dict[int, dict],
    matrix_fields_by_check: Dict[int, List[dict]],
    matrix_cells_by_field: Dict[int, Dict[str, Optional[str]]],
) -> dict:
    documents = [
        {
            "document_id": document_id,
            "name": document_by_id[document_id].get("name") if document_id in document_by_id else None,
            "template_id": document_by_id[document_id].get("template_id") if document_id in document_by_id else None,
            "document_type": document_by_id[document_id].get("document_type") or "linked_template" if document_id in document_by_id else "linked_template",
            "field_options": _cross_check_field_specs_from_document_row({"id": document_id, **document_by_id[document_id]}) if document_id in document_by_id else [],
            "sort_order": sort_order,
        }
        for sort_order, document_id in enumerate(document_ids)
    ]
    fields = []
    for field in matrix_fields_by_check.get(check_id, []):
        field_id = field["id"]
        fields.append({
            "id": field_id,
            "sort_order": field.get("sort_order", 0),
            "cells": matrix_cells_by_field.get(field_id, {}),
            "operator": field.get("operator") or "=",
            "tolerance": float(field.get("tolerance") or 0),
            "missing_data_behavior": field.get("missing_data_behavior") or "not_configured",
            "blocking": bool(field.get("blocking")),
            "risk": field.get("risk") or "Medium",
        })
    return {"documents": documents, "fields": fields}


def compile_verification_rulebook(client_id: int) -> dict:
    """Compile active Audit Rules into a Verification-facing, read-only rulebook.

    Voucher evidence and calculated results are deliberately outside this stable configuration
    read model. The returned object is a fresh compilation; use
    ``load_client_verification_rulebook`` for the version-aware cached loader.
    """
    warnings: List[dict] = []

    categories = execute_query_pg(
        "SELECT id, name, kb_overview, evidence_type, matrix_version, validation_rules_json "
        "FROM audit_rule_categories WHERE is_active = TRUE ORDER BY id"
    )
    documents = execute_query_pg(
        "SELECT id, name, kb_note, template_id, risk_rating, transaction_level_approval, "
        "document_type, verification_field_policy, is_active FROM audit_rule_documents "
        "WHERE is_active = TRUE ORDER BY id"
    )
    document_ids = [row["id"] for row in documents]
    category_ids = [row["id"] for row in categories]

    roles = execute_query_pg(
        "SELECT document_id, role_name, required_authority, sort_order "
        "FROM audit_rule_document_auth_roles ORDER BY document_id, sort_order"
    )
    # Cross-check fields are a mirror of the active Master Columns registry.  The legacy
    # audit_rule_document_fields table is retained for compatibility, but it is no longer the
    # source of truth because a manually saved list cannot include Master Columns added later.
    master_columns = execute_query_pg(
        "SELECT id, column_name FROM master_columns "
        "WHERE is_active = TRUE ORDER BY category, column_name"
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
    matrix_fields = execute_query_pg(
        "SELECT id, cross_check_id, sort_order, operator, tolerance, missing_data_behavior, "
        "blocking, risk FROM audit_rule_cross_check_fields "
        "ORDER BY cross_check_id, sort_order, id"
    )
    matrix_cells = execute_query_pg(
        "SELECT field_id, document_id, column_name FROM audit_rule_cross_check_cells "
        "ORDER BY field_id, document_id"
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
        "SELECT id, template_name AS name FROM templates WHERE is_active = TRUE ORDER BY id"
    )
    mappings = execute_query_pg(
        "SELECT p.template_id, p.column_id, m.column_name, m.data_type, p.sort_order "
        "FROM template_column_mappings p "
        "JOIN master_columns m ON p.column_id = m.id "
        "WHERE m.is_active = TRUE "
        "ORDER BY p.template_id, p.sort_order"
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
    category_document_ids: Dict[int, set] = {category_id: set() for category_id in category_ids}
    cross_docs_by_id: Dict[int, List[int]] = {check_id: [] for check_id in cross_check_ids}
    matrix_fields_by_check: Dict[int, List[dict]] = {check_id: [] for check_id in cross_check_ids}
    matrix_cells_by_field: Dict[int, Dict[str, Optional[str]]] = {}
    checks_by_category: Dict[int, List[int]] = {category_id: [] for category_id in category_ids}
    specimens_by_key: Dict[tuple, List[dict]] = {}

    for row in roles:
        if row["document_id"] in roles_by_document:
            roles_by_document[row["document_id"]].append({
                "role_name": row["role_name"],
                "required_authority": row.get("required_authority")
            })
    mirrored_fields = [
        {"field_name": row["column_name"], "master_column_id": row["id"]}
        for row in master_columns
    ]
    for document in documents:
        if (document.get("document_type") or "linked_template") != "physical_bill":
            fields_by_document[document["id"]] = [field.copy() for field in mirrored_fields]
    for row in requirements:
        if row["category_id"] in required_by_category:
            required_by_category[row["category_id"]].append(row)
            category_document_ids[row["category_id"]].add(row["document_id"])
            if row["document_id"] not in document_by_id:
                warnings.append({"code": "inactive_document_requirement", "category_id": row["category_id"], "document_id": row["document_id"]})
    for row in cross_check_documents:
        if row["cross_check_id"] in cross_docs_by_id:
            cross_docs_by_id[row["cross_check_id"]].append(row["document_id"])
            if row["document_id"] not in document_by_id:
                warnings.append({"code": "inactive_cross_check_document", "cross_check_id": row["cross_check_id"], "document_id": row["document_id"]})
    for row in matrix_fields:
        if row["cross_check_id"] in matrix_fields_by_check:
            matrix_fields_by_check[row["cross_check_id"]].append(row)
            matrix_cells_by_field[row["id"]] = {}
    for row in matrix_cells:
        matrix_cells_by_field.setdefault(row["field_id"], {})[str(row["document_id"])] = row.get("column_name")
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
            "column_id": row["column_id"], "column_name": row.get("column_name"),
            "data_type": row.get("data_type"), "sort_order": row["sort_order"]
        })

    def build_document(document_id: int, required: bool) -> dict:
        row = document_by_id[document_id]
        template_id = row.get("template_id")
        form_fields = get_document_form_fields(document_id) \
            if (row.get("document_type") or "linked_template") == "form" else []
        if form_fields:
            row["form_fields"] = form_fields
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
            "is_active": bool(row.get("is_active", True)),
            "required": bool(required), "transaction_level_approval": bool(row.get("transaction_level_approval")),
            "risk_rating": row.get("risk_rating"),
            "fields": fields_by_document.get(document_id, []),
            "form_fields": form_fields,
            "verification_field_policy": _parse_verification_field_policy(row.get("verification_field_policy")),
            "verification_fields": _build_selected_verification_fields(
                template_id, row.get("verification_field_policy"), mappings_by_template
            ),
            "authority_roles": roles_by_document.get(document_id, []),
            "signatories": [
                {"role_name": role_name, **specimen}
                for (specimen_document_id, role_name), role_specimens in specimens_by_key.items()
                if specimen_document_id == document_id
                for specimen in role_specimens
            ],
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
            missing_dependencies = [
                document_id for document_id in doc_ids
                if document_id not in category_document_ids[category_id]
            ]
            inactive_dependencies = [
                document_id for document_id in doc_ids
                if document_id not in document_by_id
            ]
            if missing_dependencies or inactive_dependencies:
                warnings.append({
                    "code": "cross_check_dependency_invalid",
                    "category_id": category_id,
                    "cross_check_id": check_id,
                    "missing_document_ids": missing_dependencies,
                    "inactive_document_ids": inactive_dependencies,
                })
                continue
            matrix = _build_cross_check_matrix_contract(
                check_id, doc_ids, document_by_id, matrix_fields_by_check, matrix_cells_by_field
            )
            profile_cross_checks.append({
                "cross_check_id": check_id, "name": check["name"], "compare_field": check["compare_field"],
                "comparison_type": check["comparison_type"],
                "compare_to_voucher_line": bool(check.get("compare_to_voucher_line")),
                "document_ids": doc_ids,
                "document_chain": matrix["documents"],
                "matrix": matrix,
            })
            if not all(doc_id in document_by_id for doc_id in doc_ids):
                warnings.append({"code": "cross_check_has_inactive_document", "category_id": category_id, "cross_check_id": check_id})
        validation_rules = _parse_category_validation_rules(category.get("validation_rules_json"))
        category_profiles.append({
            "category_id": category_id, "name": category["name"], "kb_overview": category.get("kb_overview"),
            "matrix_version": category.get("matrix_version", 1), "evidence_type": category.get("evidence_type"),
            "documents": profile_documents, "cross_checks": profile_cross_checks,
            "validation_rules": validation_rules,
            "procedure_evidence": _build_procedure_evidence_contract(
                validation_rules, document_by_id, roles_by_document, specimens_by_key
            ),
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
        "templates": [{
            "id": row["id"],
            "name": row.get("name"),
            "mapping_column_ids": [m["column_id"] for m in mappings_by_template.get(row["id"], [])],
            "columns": [
                {
                    "column_id": m["column_id"],
                    "column_name": m.get("column_name"),
                    "data_type": m.get("data_type"),
                    "sort_order": m["sort_order"],
                }
                for m in mappings_by_template.get(row["id"], [])
            ],
        } for row in templates],
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
    # Document evidence type: linked_template uses Data Bank rows, form uses a configured
    # manual form, and physical_bill is retained only for the legacy migration path.
    # Optional/None rather than a literal default here so
    # resolve_document_type() below stays the single place the default lives.
    document_type: Optional[str] = None
    form_fields: List[Dict[str, Any]] = []
    # Phase 2: selected verification fields for the linked Template. The server validates this
    # against the active Template mapping before persisting it.
    verification_field_policy: Optional[Dict[str, Any]] = None

class DocumentUpdate(BaseModel):
    name: str
    kb_note: Optional[str] = None
    template_id: Optional[int] = None
    risk_rating: Optional[str] = None
    auth_roles: List[str] = []
    auth_role_authorities: Dict[str, Optional[str]] = {}
    cross_check_fields: List[str] = []
    document_type: Optional[str] = None
    # None means the caller did not manage Form fields; preserve the saved definition. An
    # explicit [] remains the intentional way for the Audit Rules UI to clear all fields.
    form_fields: Optional[List[Dict[str, Any]]] = None
    verification_field_policy: Optional[Dict[str, Any]] = None

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

FORM_FIELD_TYPES = ('text', 'date', 'numeric')
FORM_FIELD_SCOPES = ('header', 'item')

def normalize_form_fields(document_type: str, template_id: Optional[int], raw_fields: List[Dict[str, Any]]) -> List[dict]:
    """Validate the reusable Form field contract and return stable ordered rows."""
    if document_type != 'form':
        if raw_fields:
            raise HTTPException(400, "form_fields are only valid for Form documents")
        return []
    fields = []
    seen_names = set()
    template_columns = set()
    if template_id:
        template_rows = execute_query_pg(
            "SELECT mc.column_name FROM template_column_mappings tcm "
            "JOIN master_columns mc ON mc.id = tcm.column_id "
            "WHERE tcm.template_id = %s AND mc.is_active = TRUE",
            (template_id,)
        )
        template_columns = {row['column_name'].strip().casefold() for row in template_rows}
    for index, raw in enumerate(raw_fields or []):
        name = str(raw.get('field_name') or raw.get('column_name') or '').strip()
        field_type = str(raw.get('field_type') or raw.get('column_type') or 'text').strip().lower()
        field_scope = str(raw.get('field_scope') or 'header').strip().lower()
        linked_column = str(raw.get('linked_column') or '').strip() or None
        if not name:
            raise HTTPException(400, f"Form field {index + 1} needs a column name")
        if name.casefold() in seen_names:
            raise HTTPException(400, f"Form field names must be unique: '{name}'")
        if field_type not in FORM_FIELD_TYPES:
            raise HTTPException(400, f"Invalid Form field type '{field_type}'")
        if field_scope not in FORM_FIELD_SCOPES:
            raise HTTPException(400, f"Invalid Form field scope '{field_scope}'")
        if linked_column and linked_column.casefold() not in template_columns:
            raise HTTPException(400, f"Linked Column '{linked_column}' is not an active column on the selected Template")
        seen_names.add(name.casefold())
        fields.append({'field_name': name, 'field_type': field_type, 'field_scope': field_scope,
                       'linked_column': linked_column, 'sort_order': index})
    return fields

def get_document_form_fields(document_id: int) -> List[dict]:
    rows = execute_query_pg(
        "SELECT id, field_name, field_type, field_scope, linked_column, sort_order "
        "FROM audit_rule_document_fields WHERE document_id = %s ORDER BY sort_order, id",
        (document_id,)
    )
    return [{
        'id': row['id'], 'field_name': row['field_name'], 'field_type': row.get('field_type') or 'text',
        'field_scope': row.get('field_scope') or 'header', 'linked_column': row.get('linked_column'),
        'sort_order': row['sort_order']
    } for row in rows]

def save_document_form_fields(document_id: int, fields: List[dict]):
    execute_write_pg("DELETE FROM audit_rule_document_fields WHERE document_id = %s", (document_id,))
    for field in fields:
        execute_write_pg(
            "INSERT INTO audit_rule_document_fields (document_id, field_name, sort_order, field_type, field_scope, linked_column) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (document_id, field['field_name'], field['sort_order'], field['field_type'],
             field['field_scope'], field['linked_column'])
        )

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

def get_active_master_columns() -> List[dict]:
    """Return the live Master Columns registry used by cross-check field mirrors."""
    return execute_query_pg(
        "SELECT id, column_name FROM master_columns "
        "WHERE is_active = TRUE ORDER BY category, column_name"
    )


# Phase 2: Verification Data Fields ---------------------------------------------------------
# These are the six transaction fields used by the inventory-purchase verification workflow.
# Their labels and aliases are stable product vocabulary; the actual column IDs remain
# Template-scoped and are always resolved from the active mapping table.
VERIFICATION_CORE_FIELDS = (
    ("vendor", "Vendor", ("vendor", "vendor name", "supplier", "supplier name")),
    ("item", "Item", ("item", "item name", "item description", "description")),
    ("unit", "Unit of measurement", ("unit", "unit of measurement", "uom")),
    ("quantity", "Quantity", ("quantity", "qty")),
    ("rate", "Rate", ("rate", "unit rate")),
    ("amount", "Amount", ("amount", "total amount", "line amount", "net amount")),
)
VERIFICATION_RISK_LEVELS = ("Low", "Medium", "High")


def _verification_name_key(value: Any) -> str:
    """Normalize a Master Column name for conservative alias matching."""
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _parse_verification_field_policy(value: Any) -> Optional[dict]:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _get_verification_template_columns(template_id: Optional[int]) -> List[dict]:
    if template_id is None:
        return []
    return execute_query_pg(
        "SELECT m.id AS column_id, m.column_name, m.data_type, p.sort_order "
        "FROM template_column_mappings p "
        "JOIN master_columns m ON m.id = p.column_id "
        "WHERE p.template_id = %s AND m.is_active = TRUE "
        "ORDER BY p.sort_order, m.id",
        (template_id,)
    )


def validate_and_normalize_verification_field_policy(
    template_id: Optional[int], raw_policy: Optional[dict]
) -> Optional[dict]:
    """Validate the saved field selection and return a small stable JSON contract."""
    if raw_policy is None:
        return None
    if not isinstance(raw_policy, dict):
        raise HTTPException(400, "Verification field policy must be an object")
    if template_id is None:
        raise HTTPException(400, "A linked Template is required for verification fields")

    columns = _get_verification_template_columns(template_id)
    by_id = {int(row["column_id"]): row for row in columns}
    if not by_id:
        raise HTTPException(400, "The selected Template has no active mapped columns")

    raw_core = raw_policy.get("core") or {}
    if not isinstance(raw_core, dict):
        raise HTTPException(400, "Verification core fields must be an object")
    core_ids = {}
    for key, _label, _aliases in VERIFICATION_CORE_FIELDS:
        value = raw_core.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(400, f"Core verification field '{key}' must use a column ID")
        if value not in by_id:
            raise HTTPException(400, f"Core verification field '{key}' is not active in this Template")
        core_ids[key] = value
    if len(set(core_ids.values())) != len(core_ids):
        raise HTTPException(400, "Each core verification field must use a different Template column")

    raw_optional = raw_policy.get("optional") or []
    if not isinstance(raw_optional, list):
        raise HTTPException(400, "Optional verification fields must be a list")
    optional = []
    optional_ids = set()
    for index, entry in enumerate(raw_optional):
        if not isinstance(entry, dict):
            raise HTTPException(400, f"Optional verification field {index + 1} must be an object")
        column_id = entry.get("column_id")
        if isinstance(column_id, bool) or not isinstance(column_id, int) or column_id not in by_id:
            raise HTTPException(400, f"Optional verification field {index + 1} is not active in this Template")
        if column_id in optional_ids or column_id in core_ids.values():
            raise HTTPException(400, "Verification fields cannot be duplicated")
        risk_level = entry.get("risk_level") or "Medium"
        if risk_level not in VERIFICATION_RISK_LEVELS:
            raise HTTPException(400, f"Invalid optional verification risk level '{risk_level}'")
        sort_order = entry.get("sort_order", index)
        if isinstance(sort_order, bool) or not isinstance(sort_order, int):
            raise HTTPException(400, "Optional verification field order must be an integer")
        optional_ids.add(column_id)
        optional.append({"column_id": column_id, "risk_level": risk_level, "sort_order": sort_order})
    optional.sort(key=lambda entry: (entry["sort_order"], entry["column_id"]))
    for index, entry in enumerate(optional):
        entry["sort_order"] = index
    return {"version": 1, "core": core_ids, "optional": optional}


def build_verification_field_policy(template_id: Optional[int], stored_policy: Any) -> dict:
    """Build the UI/read model, including safe defaults and unavailable-field notices."""
    columns = _get_verification_template_columns(template_id)
    parsed = _parse_verification_field_policy(stored_policy)
    by_id = {int(row["column_id"]): row for row in columns}
    normalized_by_name = {_verification_name_key(row["column_name"]): row for row in columns}
    raw_core = (parsed or {}).get("core") or {}
    selected_core = {}
    unavailable = []

    for key, label, aliases in VERIFICATION_CORE_FIELDS:
        column = None
        configured_id = raw_core.get(key)
        if isinstance(configured_id, int) and not isinstance(configured_id, bool):
            column = by_id.get(configured_id)
            if column is None:
                unavailable.append({"kind": "core", "key": key, "label": label, "column_id": configured_id})
        if column is None and configured_id is None:
            for alias in aliases:
                column = normalized_by_name.get(_verification_name_key(alias))
                if column:
                    break
        if column:
            selected_core[key] = int(column["column_id"])

    used_ids = set(selected_core.values())
    optional = []
    for entry in ((parsed or {}).get("optional") or []):
        if not isinstance(entry, dict):
            continue
        column_id = entry.get("column_id")
        if not isinstance(column_id, int) or isinstance(column_id, bool):
            continue
        column = by_id.get(column_id)
        if column is None:
            unavailable.append({"kind": "optional", "column_id": column_id})
            continue
        if column_id in used_ids or any(item["column_id"] == column_id for item in optional):
            continue
        optional.append({
            "column_id": column_id,
            "column_name": column["column_name"],
            "data_type": column.get("data_type"),
            "risk_level": entry.get("risk_level") if entry.get("risk_level") in VERIFICATION_RISK_LEVELS else "Medium",
            "sort_order": entry.get("sort_order", len(optional)),
        })
    optional.sort(key=lambda entry: (entry["sort_order"], entry["column_id"]))

    core_fields = []
    for key, label, aliases in VERIFICATION_CORE_FIELDS:
        column_id = selected_core.get(key)
        column = by_id.get(column_id) if column_id is not None else None
        core_fields.append({
            "key": key,
            "label": label,
            "aliases": list(aliases),
            "column_id": column_id,
            "column_name": column.get("column_name") if column else None,
            "data_type": column.get("data_type") if column else None,
            "available": bool(column),
        })
    return {
        "version": 1,
        "template_id": template_id,
        "configured": parsed is not None,
        "template_columns": [
            {"column_id": int(row["column_id"]), "column_name": row["column_name"],
             "data_type": row.get("data_type"), "sort_order": row.get("sort_order", 0)}
            for row in columns
        ],
        "core_fields": core_fields,
        "optional_fields": optional,
        "unavailable_fields": unavailable,
        "selected_column_ids": [field["column_id"] for field in core_fields if field["column_id"] is not None]
            + [field["column_id"] for field in optional],
    }


def get_document_cross_check_fields(document_id: int, document_type: Optional[str] = None) -> List[str]:
    """Return the field names that Cross-checks may use for a Document.

    Form fields are the authority for Form Documents; legacy Physical Bills expose their fixed
    manual fields. Linked-template Documents retain the active Master Columns mirror for the
    older flat Cross-check contract.
    """
    return [field["field_name"] for field in get_document_cross_check_field_specs(document_id, document_type)]


def get_document_cross_check_field_specs(
    document_id: int, document_type: Optional[str] = None
) -> List[dict]:
    """Return stable Cross-check field metadata for a Document.

    A Form field keeps its saved Form name as the Cross-check value and exposes ``linked_column``
    only as source metadata. The evaluator reads the saved Form value by field name; it never
    substitutes the linked-template source row for auditor-entered Form evidence.
    """
    if document_type is None:
        rows = execute_query_pg(
            "SELECT document_type FROM audit_rule_documents WHERE id = %s",
            (document_id,)
        )
        document_type = (rows[0].get("document_type") if rows else None) or "linked_template"
    if document_type == "form":
        rows = execute_query_pg(
            "SELECT field_name, field_type, linked_column FROM audit_rule_document_fields "
            "WHERE document_id = %s ORDER BY sort_order, id",
            (document_id,)
        )
        return [
            {
                "field_name": row["field_name"],
                "data_type": row.get("field_type") or "text",
                "linked_column": row.get("linked_column"),
                "source": "form",
            }
            for row in rows
        ]
    if document_type == "physical_bill":
        return [
            {"field_name": field_name, "data_type": "numeric" if field_name in ("Quantity", "Rate", "Amount") else "text", "linked_column": None, "source": "legacy_physical_bill"}
            for field_name in ("Bill No", "Bill Date", "Vendor Name", "Quantity", "Rate", "Amount")
        ]
    return [
        {"field_name": row["column_name"], "data_type": row.get("data_type") or "text", "linked_column": None, "source": "master_column"}
        for row in get_active_master_columns()
    ]


def _cross_check_field_specs_from_document_row(row: dict) -> List[dict]:
    document_type = row.get("document_type") or "linked_template"
    if document_type == "form":
        return [
            {
                "field_name": field["field_name"],
                "data_type": field.get("field_type") or "text",
                "linked_column": field.get("linked_column"),
                "source": "form",
            }
            for field in row.get("form_fields") or []
        ]
    if document_type == "physical_bill":
        return get_document_cross_check_field_specs(row["id"], document_type)
    return []

def get_document_used_by_count(document_id: int) -> int:
    rows = execute_query_pg(
        "SELECT COUNT(*) as cnt FROM audit_rule_category_documents WHERE document_id = %s",
        (document_id,)
    )
    return rows[0]["cnt"] if rows else 0

def row_to_document(row: dict) -> dict:
    document_id = row["id"]
    document_type = row.get("document_type") or 'linked_template'
    form_fields = get_document_form_fields(document_id) if document_type == 'form' else []
    return {
        "id": document_id,
        "name": row["name"],
        "kb_note": row.get("kb_note"),
        "template_id": row.get("template_id"),
        "risk_rating": row.get("risk_rating"),
        # Physical Bill evidence type (Phase 2 of 8). The `or` fallback is defensive only -
        # Phase 1's ALTER backfilled every existing row to 'linked_template'.
        "document_type": document_type,
        "is_active": bool(row["is_active"]),
        "auth_roles": get_document_auth_roles(document_id),
        "auth_role_authorities": get_document_auth_role_authorities(document_id),
        "cross_check_fields": get_document_cross_check_fields(document_id, document_type),
        "cross_check_field_specs": get_document_cross_check_field_specs(document_id, document_type),
        "form_fields": form_fields,
        "verification_field_policy": build_verification_field_policy(
            row.get("template_id"), row.get("verification_field_policy")
        ),
        "used_by_categories_count": get_document_used_by_count(document_id)
    }

def row_to_document_list(row: dict, mirrored_fields: Optional[List[str]] = None) -> dict:
    """
    List shape used by both the Documents rail and the Cross-check document picker. The field
    mirror is included here so the picker can calculate common fields without fetching every
    Document detail record separately. Other detail-only fields remain trimmed.
    """
    document_type = row.get("document_type") or "linked_template"
    return {
        "id": row["id"],
        "name": row["name"],
        "is_active": bool(row["is_active"]),
        "document_type": document_type,
        "template_id": row.get("template_id"),
        "cross_check_fields": [] if document_type == "physical_bill" else (mirrored_fields or [])
    }

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
DOCUMENT_TYPES = ('linked_template', 'form', 'physical_bill')

def resolve_document_type(document_type: Optional[str]) -> str:
    """
    Resolve a Document's type, raising HTTPException(400) on an unrecognised value.

    Both an omitted field and an explicit null resolve to 'linked_template' - deliberately a
    resolver rather than a bare validator (mirroring resolve_rate_source() in
    routers/templates.py) so create/update each get one already-disciplined value instead of
    repeating the fallback.

    'linked_template' is the safe default: it's what all existing Documents already are, so an
    API caller that doesn't know about this field yet (static/audit_rules.js until Phase 4)
    keeps working unchanged. Category Evidence Type is now legacy read-only metadata; the active
    document and Procedure/Evidence configuration supplies the verification policy.
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
    Compatibility hook for the old per-Document field persistence path.

    Cross-check-eligible fields now come directly from active Master Columns, so persisting a
    snapshot here would reintroduce the original bug whenever a new column is added. Existing
    audit_rule_document_fields rows are deliberately left intact for older data and migration
    compatibility, but no longer participate in display, validation, or Rulebook compilation.
    """
    return None

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

    is_system rows (e.g. a Cross-check's own lazily-created shortfall-note placeholder, see
    routers/verification.py's _get_or_create_cross_check_shortfall_document_id()) are excluded
    unconditionally, even with include_inactive=True - they're never a real evidence requirement
    and must never appear in this list.
    """
    cache_key = f"documents:list:{include_inactive}:{limit}:{offset}"
    cached = cache_get(cache_key)
    if cached is not None:
        result, total = cached
        if total is not None:
            response.headers["X-Total-Count"] = str(total)
        return result

    where = " WHERE (is_system IS NOT TRUE)" if include_inactive \
        else " WHERE (is_system IS NOT TRUE) AND is_active = TRUE"
    base_query = "SELECT * FROM audit_rule_documents" + where
    count_query = "SELECT COUNT(*) as cnt FROM audit_rule_documents" + where
    rows, total = fetch_paginated_rows(base_query, count_query, "ORDER BY name", limit, offset)
    if total is not None:
        response.headers["X-Total-Count"] = str(total)
    mirrored_fields = [row["column_name"] for row in get_active_master_columns()]
    result = [row_to_document_list(r, mirrored_fields) for r in rows]
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
    form_fields = normalize_form_fields(document_type, template_id, document.form_fields)
    verification_field_policy = validate_and_normalize_verification_field_policy(
        template_id, document.verification_field_policy
    )
    document_id = execute_write_pg(
        "INSERT INTO audit_rule_documents "
        "(name, kb_note, template_id, risk_rating, document_type, verification_field_policy) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (document.name, document.kb_note, template_id, document.risk_rating, document_type,
         Json(verification_field_policy) if verification_field_policy is not None else None)
    )
    save_document_auth_roles(document_id, document.auth_roles, document.auth_role_authorities)
    save_document_form_fields(document_id, form_fields)
    save_document_cross_check_fields(document_id, cross_check_fields)

    log_audit("Admin", "Create", "AuditRules", f"Created Document: {document.name}")
    invalidate_audit_rules_cache({"entity_type": "Document", "entity_id": document_id,
                                  "entity_name": document.name, "action": "Created"})
    return {"message": f"Document '{document.name}' created successfully", "document_id": document_id}

@router.put("/api/audit-rules/documents/{document_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def update_document(document_id: int, document: DocumentUpdate):
    existing = execute_query_pg(
        "SELECT id, template_id, document_type, verification_field_policy FROM audit_rule_documents WHERE id = %s",
        (document_id,)
    )
    if not existing:
        raise HTTPException(404, "Document not found")
    document_type = resolve_document_type(document.document_type)
    template_id, cross_check_fields = apply_document_type_constraints(
        document_type, document.template_id, document.cross_check_fields
    )
    validate_document_template_id(template_id)
    validate_document_risk_rating(document.risk_rating)
    raw_form_fields = document.form_fields
    if document_type == 'form' and raw_form_fields is None:
        # Secondary document updates (for example adding a signatory) should not silently
        # erase a Form definition merely because that action does not edit Form fields.
        raw_form_fields = get_document_form_fields(document_id)
    form_fields = normalize_form_fields(document_type, template_id, raw_form_fields or [])

    # Omitted policy means preserve the current policy for API compatibility. The Audit Rules
    # UI sends the field explicitly when the administrator changes it.
    verification_field_policy = None
    policy_sql = ""
    policy_params = []
    if document.verification_field_policy is not None:
        verification_field_policy = validate_and_normalize_verification_field_policy(
            template_id, document.verification_field_policy
        )
        policy_sql = ", verification_field_policy = %s"
        policy_params.append(Json(verification_field_policy))
    elif template_id != existing[0].get("template_id"):
        # A changed Template cannot safely inherit column IDs from the old Template.
        policy_sql = ", verification_field_policy = NULL"

    execute_write_pg(
        "UPDATE audit_rule_documents SET name = %s, kb_note = %s, template_id = %s, "
        "risk_rating = %s, document_type = %s" + policy_sql + " WHERE id = %s",
        (document.name, document.kb_note, template_id, document.risk_rating, document_type,
         *policy_params, document_id)
    )
    save_document_auth_roles(document_id, document.auth_roles, document.auth_role_authorities)
    save_document_form_fields(document_id, form_fields)
    # save_document_cross_check_fields() is still called unconditionally (one write path), but
    # receives an empty list for a physical_bill, so nothing is persisted for it.
    save_document_cross_check_fields(document_id, cross_check_fields)

    log_audit("Admin", "Update", "AuditRules", f"Updated Document ID {document_id}")
    invalidate_audit_rules_cache({"entity_type": "Document", "entity_id": document_id,
                                  "entity_name": document.name, "action": "Updated"})
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
    invalidate_audit_rules_cache({"entity_type": "Document", "entity_id": document_id,
                                  "entity_name": existing[0]['name'], "action": "Deleted"})
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
    invalidate_audit_rules_cache({"entity_type": "Document", "entity_id": document_id,
                                  "entity_name": existing[0]['name'], "action": "Restored"})
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
    # Cross-check per-document field names (Phase 2 of 4): optional per-Document override of
    # compare_field, keyed by document_id. A document_id absent from this map (or mapped to
    # None / "") stores NULL on its junction row, which means "fall back to the shared
    # compare_field above" (Phase 1's backfill contract). An older client that sends only
    # document_ids keeps today's behaviour: every junction row NULL -> every Document uses
    # the shared field.
    document_fields: Dict[int, Optional[str]] = {}
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
    document_fields: Dict[int, Optional[str]] = {}
    compare_to_voucher_line: bool = False

# ---------- HELPERS ----------
def normalize_compare_field(value: Optional[str]) -> Optional[str]:
    """Trim a compare_field override; treat blank as "not set" (NULL / use the shared field)."""
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def get_cross_check_documents(cross_check_id: int, shared_compare_field: Optional[str] = None) -> List[dict]:
    rows = execute_query_pg(
        """
        SELECT d.id as document_id, d.name, ccd.compare_field
        FROM audit_rule_cross_check_documents ccd
        JOIN audit_rule_documents d ON ccd.document_id = d.id
        WHERE ccd.cross_check_id = %s
        ORDER BY ccd.sort_order
        """,
        (cross_check_id,)
    )
    result = []
    for r in rows:
        own_field = normalize_compare_field(r.get("compare_field"))
        result.append({
            "document_id": r["document_id"],
            "name": r["name"],
            # own_field is this Document's per-row override (NULL -> None); effective_compare_field
            # resolves the fallback so the frontend (Phase 3) never has to re-derive it.
            "compare_field": own_field,
            "effective_compare_field": own_field or shared_compare_field,
        })
    return result

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
        "documents": get_cross_check_documents(cross_check_id, row["compare_field"]),
        "used_by_categories_count": get_cross_check_used_by_count(cross_check_id)
    }

def row_to_cross_check_list(row: dict, documents: Optional[List[dict]] = None) -> dict:
    """
    Lean shape for the Cross-checks LIST endpoint - it includes one set-based dependency summary
    for the Category picker, while avoiding the detail view's per-row used-by count query.
    """
    return {
        "id": row["id"],
        "name": row["name"],
        "is_active": bool(row["is_active"]),
        "documents": documents or [],
    }

def get_cross_check_document_summaries(cross_check_ids: List[int]) -> Dict[int, List[dict]]:
    """Load ordered Cross-check dependencies for a paginated list in one query."""
    documents_by_cross_check = {cross_check_id: [] for cross_check_id in cross_check_ids}
    if not cross_check_ids:
        return documents_by_cross_check

    placeholders = ",".join("%s" for _ in cross_check_ids)
    rows = execute_query_pg(
        f"""
        SELECT ccd.cross_check_id, d.id AS document_id, d.name, d.is_active, ccd.sort_order
        FROM audit_rule_cross_check_documents ccd
        JOIN audit_rule_documents d ON d.id = ccd.document_id
        WHERE ccd.cross_check_id IN ({placeholders})
        ORDER BY ccd.cross_check_id, ccd.sort_order
        """,
        tuple(cross_check_ids),
    )
    for row in rows:
        documents_by_cross_check[row["cross_check_id"]].append({
            "document_id": row["document_id"],
            "name": row["name"],
            "is_active": bool(row["is_active"]),
            "sort_order": row["sort_order"],
        })
    return documents_by_cross_check

def save_cross_check_documents(cross_check_id: int, document_ids: List[int],
                               document_fields: Optional[Dict[int, Optional[str]]] = None):
    """Replace a Cross-check's ordered document list (delete-then-reinsert).

    document_fields maps a document_id to its own compare_field override; a document absent
    from the map (or mapped to blank/None) stores NULL, i.e. "use the Cross-check's shared
    compare_field" (Phase 1's backfill contract).
    """
    field_map = document_fields or {}
    execute_write_pg("DELETE FROM audit_rule_cross_check_documents WHERE cross_check_id = %s", (cross_check_id,))
    for idx, document_id in enumerate(document_ids):
        own_field = normalize_compare_field(field_map.get(document_id))
        execute_write_pg(
            "INSERT INTO audit_rule_cross_check_documents (cross_check_id, document_id, sort_order, compare_field) "
            "VALUES (%s, %s, %s, %s)",
            (cross_check_id, document_id, idx, own_field)
        )

def validate_cross_check_payload(compare_field: str, comparison_type: str, document_ids: List[int],
                                 compare_to_voucher_line: bool = False,
                                 document_fields: Optional[Dict[int, Optional[str]]] = None):
    """
    Raises HTTPException(400) if comparison_type isn't one of the fixed set, or if a
    Document's effective compared field isn't cross-check-eligible on that Document, or if
    compare_to_voucher_line is set with no Document to act as the anchor (Phase 5 of 10 -
    the anchor is the last document in document_ids, resolved live at evaluation time, never
    picked here).

    Cross-check per-document field names (Phase 2 of 4): each Document's *effective* field is
    its own override from document_fields if set, otherwise the shared compare_field. There is
    no longer a single-name check across every Document - fields are matched by chain position
    / comparison intent, not by identical name.

    SQLite foreign keys aren't enforced on this app's connections (see get_db()), so
    Document existence is also checked here rather than left to the database.
    """
    if comparison_type not in COMPARISON_TYPES:
        raise HTTPException(400, f"Invalid comparison type '{comparison_type}'. Must be one of: {', '.join(COMPARISON_TYPES)}")

    if compare_to_voucher_line and not document_ids:
        raise HTTPException(400, "compare_to_voucher_line requires at least one Document (used as the anchor)")

    field_map = document_fields or {}
    for document_id in document_ids:
        doc_rows = execute_query_pg(
            "SELECT name, document_type FROM audit_rule_documents WHERE id = %s",
            (document_id,)
        )
        if not doc_rows:
            raise HTTPException(400, f"Document ID {document_id} not found")
        effective_field = normalize_compare_field(field_map.get(document_id)) or compare_field
        doc_fields = get_document_cross_check_fields(document_id, doc_rows[0].get("document_type"))
        if effective_field not in doc_fields:
            raise HTTPException(
                400,
                f"Field '{effective_field}' is not a cross-check-eligible field on Document '{doc_rows[0]['name']}' (ID {document_id})"
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
    cache_key = f"cross_checks:list:v2:{include_inactive}:{limit}:{offset}"
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
    documents_by_cross_check = get_cross_check_document_summaries([row["id"] for row in rows])
    result = [row_to_cross_check_list(r, documents_by_cross_check.get(r["id"], [])) for r in rows]
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
        cross_check.compare_to_voucher_line, cross_check.document_fields
    )

    cross_check_id = execute_write_pg(
        "INSERT INTO audit_rule_cross_checks (name, compare_field, comparison_type, compare_to_voucher_line) VALUES (%s, %s, %s, %s) RETURNING id",
        (cross_check.name, cross_check.compare_field, cross_check.comparison_type, cross_check.compare_to_voucher_line)
    )
    save_cross_check_documents(cross_check_id, cross_check.document_ids, cross_check.document_fields)

    log_audit("Admin", "Create", "AuditRules", f"Created Cross-check: {cross_check.name}")
    invalidate_audit_rules_cache({"entity_type": "Cross-check", "entity_id": cross_check_id,
                                  "entity_name": cross_check.name, "action": "Created"})
    return {"message": f"Cross-check '{cross_check.name}' created successfully", "cross_check_id": cross_check_id}

@router.put("/api/audit-rules/cross-checks/{cross_check_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def update_cross_check(cross_check_id: int, cross_check: CrossCheckUpdate):
    existing = execute_query_pg("SELECT id FROM audit_rule_cross_checks WHERE id = %s", (cross_check_id,))
    if not existing:
        raise HTTPException(404, "Cross-check not found")

    validate_cross_check_payload(
        cross_check.compare_field, cross_check.comparison_type, cross_check.document_ids,
        cross_check.compare_to_voucher_line, cross_check.document_fields
    )

    execute_write_pg(
        "UPDATE audit_rule_cross_checks SET name = %s, compare_field = %s, comparison_type = %s, compare_to_voucher_line = %s WHERE id = %s",
        (cross_check.name, cross_check.compare_field, cross_check.comparison_type, cross_check.compare_to_voucher_line, cross_check_id)
    )
    save_cross_check_documents(cross_check_id, cross_check.document_ids, cross_check.document_fields)

    log_audit("Admin", "Update", "AuditRules", f"Updated Cross-check ID {cross_check_id}")
    invalidate_audit_rules_cache({"entity_type": "Cross-check", "entity_id": cross_check_id,
                                  "entity_name": cross_check.name, "action": "Updated"})
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
    invalidate_audit_rules_cache({"entity_type": "Cross-check", "entity_id": cross_check_id,
                                  "entity_name": existing[0]['name'], "action": "Deleted"})
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
    invalidate_audit_rules_cache({"entity_type": "Cross-check", "entity_id": cross_check_id,
                                  "entity_name": existing[0]['name'], "action": "Restored"})
    return {"message": "Cross-check restored successfully"}


# ==============================================
# CROSS-CHECK MATRIX (document x field)
# ==============================================
# Cross-Check Matrix Redesign, Phase 2 of 5. A Cross-check is becoming a matrix: the Documents
# (audit_rule_cross_check_documents) are the columns, audit_rule_cross_check_fields are the rows
# (no label - id + sort_order only), and audit_rule_cross_check_cells hold, per (row x Document),
# the name of a column picked from that Document's linked template. A missing cell / NULL
# column_name means "not applicable for this Document in this row".
#
# This phase is purely additive - the old flat Cross-check CRUD above (shared compare_field +
# ordered document list) and the verification.py evaluation engine that consumes it are left
# untouched. The old compare_field columns are dropped in a later phase, once a phase migrates
# the engine to read this matrix.

# ---------- DATA MODELS ----------
class CrossCheckFieldOrder(BaseModel):
    field_ids: List[int]

class CrossCheckCellUpdate(BaseModel):
    # None / "" clears the cell (deletes the row) = "not applicable". A non-empty string must be
    # a real column name on the Document's linked template (validated server-side).
    column_name: Optional[str] = None


# Suggestion 2: a matrix row is the reusable comparison rule. These values are validated here
# rather than in the browser so a Category can safely display the same definition everywhere.
MATRIX_OPERATORS = ('=', '>=', '<=', '>', '<', '!=')
MATRIX_MISSING_DATA_BEHAVIORS = ('not_configured', 'not_linked', 'missing', 'shortfall')
MATRIX_RISK_RATINGS = ('Low', 'Medium', 'High')


class CrossCheckConditionUpdate(BaseModel):
    operator: str = '='
    tolerance: float = 0
    missing_data_behavior: str = 'not_configured'
    blocking: bool = False
    risk: str = 'Medium'

# ---------- HELPERS ----------
def get_document_template_columns(document_id: int) -> List[dict]:
    """Return the linked-template column list for a Document (empty if it has no template).

    Reuses templates.py's own cached column-source (get_cached_template_columns) rather than
    re-querying template_column_mappings here - imported locally to keep the router import graph
    acyclic at startup (templates.py already imports audit_rules.py the same way).
    """
    from routers.templates import get_cached_template_columns

    rows = execute_query_pg(
        "SELECT template_id FROM audit_rule_documents WHERE id = %s", (document_id,)
    )
    if not rows:
        return []
    template_id = rows[0].get("template_id")
    if template_id is None:
        return []
    return get_cached_template_columns(template_id)


def get_cross_check_matrix(cross_check_id: int) -> dict:
    """Assemble the full matrix for a Cross-check: ordered document columns + ordered field rows,
    each row carrying its per-Document cell selections. Raises HTTPException(404) if the
    Cross-check does not exist."""
    cc = execute_query_pg(
        "SELECT id FROM audit_rule_cross_checks WHERE id = %s", (cross_check_id,)
    )
    if not cc:
        raise HTTPException(404, "Cross-check not found")

    documents = execute_query_pg(
        """
        SELECT ccd.document_id, d.name, d.template_id, d.document_type, ccd.sort_order
        FROM audit_rule_cross_check_documents ccd
        JOIN audit_rule_documents d ON d.id = ccd.document_id
        WHERE ccd.cross_check_id = %s
        ORDER BY ccd.sort_order, ccd.document_id
        """,
        (cross_check_id,),
    )
    field_rows = execute_query_pg(
        "SELECT id, sort_order, operator, tolerance, missing_data_behavior, blocking, risk "
        "FROM audit_rule_cross_check_fields "
        "WHERE cross_check_id = %s ORDER BY sort_order, id",
        (cross_check_id,),
    )
    field_ids = [r["id"] for r in field_rows]
    cells_by_field: Dict[int, Dict[str, Optional[str]]] = {fid: {} for fid in field_ids}
    if field_ids:
        placeholders = ",".join("%s" for _ in field_ids)
        cell_rows = execute_query_pg(
            f"SELECT field_id, document_id, column_name FROM audit_rule_cross_check_cells "
            f"WHERE field_id IN ({placeholders})",
            tuple(field_ids),
        )
        for c in cell_rows:
            cells_by_field.setdefault(c["field_id"], {})[str(c["document_id"])] = c["column_name"]

    return {
        "cross_check_id": cross_check_id,
        "documents": [
            {
                "document_id": d["document_id"],
                "name": d["name"],
                "template_id": d.get("template_id"),
                "document_type": d.get("document_type") or "linked_template",
                "field_options": (
                    get_document_cross_check_field_specs(d["document_id"], "form")
                    if d.get("document_type") == "form"
                    else get_document_cross_check_field_specs(d["document_id"], "physical_bill")
                    if d.get("document_type") == "physical_bill"
                    else []
                ),
                "sort_order": d["sort_order"],
            }
            for d in documents
        ],
        "fields": [
            {
                "id": r["id"],
                "sort_order": r["sort_order"],
                "cells": cells_by_field.get(r["id"], {}),
                "operator": r.get("operator") or '=',
                "tolerance": float(r.get("tolerance") or 0),
                "missing_data_behavior": r.get("missing_data_behavior") or 'not_configured',
                "blocking": bool(r.get("blocking") or 0),
                "risk": r.get("risk") or 'Medium',
            }
            for r in field_rows
        ],
    }


def validate_cross_check_condition(condition: CrossCheckConditionUpdate):
    if condition.operator not in MATRIX_OPERATORS:
        raise HTTPException(400, f"Invalid matrix operator '{condition.operator}'")
    if condition.tolerance < 0:
        raise HTTPException(400, "Matrix tolerance cannot be negative")
    if condition.missing_data_behavior not in MATRIX_MISSING_DATA_BEHAVIORS:
        raise HTTPException(400, f"Invalid matrix missing-data behavior '{condition.missing_data_behavior}'")
    if condition.risk not in MATRIX_RISK_RATINGS:
        raise HTTPException(400, f"Invalid matrix risk rating '{condition.risk}'")


def validate_cross_check_cell(field_id: int, document_id: int, column_name: Optional[str]):
    """Raises HTTPException if the field row is unknown, the Document isn't a column of the field
    row's parent Cross-check, or the selected field isn't available for that Document's type."""
    field_rows = execute_query_pg(
        "SELECT cross_check_id FROM audit_rule_cross_check_fields WHERE id = %s", (field_id,)
    )
    if not field_rows:
        raise HTTPException(404, f"Cross-check field row {field_id} not found")
    cross_check_id = field_rows[0]["cross_check_id"]

    doc_col = execute_query_pg(
        "SELECT 1 FROM audit_rule_cross_check_documents WHERE cross_check_id = %s AND document_id = %s",
        (cross_check_id, document_id),
    )
    if not doc_col:
        raise HTTPException(
            400, f"Document {document_id} is not a column of Cross-check {cross_check_id}"
        )

    normalized = normalize_compare_field(column_name)
    if normalized is None:
        return
    document_rows = execute_query_pg(
        "SELECT document_type FROM audit_rule_documents WHERE id = %s",
        (document_id,),
    )
    document_type = (document_rows[0].get("document_type") if document_rows else None) or "linked_template"
    # A Form's optional template supplies prefill values; its own fields remain the matrix operands.
    if document_type in ("form", "physical_bill"):
        valid_names = {
            field["field_name"]
            for field in get_document_cross_check_field_specs(document_id, document_type)
        }
    else:
        valid_names = {c["column_name"] for c in get_document_template_columns(document_id)}
    if not valid_names:
        raise HTTPException(
            400, f"Document {document_id} has no configured Cross-check fields"
        )
    if normalized not in valid_names:
        raise HTTPException(
            400, f"Field '{normalized}' is not available on Document {document_id}"
        )

# ---------- ENDPOINTS ----------

@router.get("/api/audit-rules/cross-checks/{cross_check_id}/matrix")
def get_cross_check_matrix_endpoint(cross_check_id: int):
    cache_key = f"cross_checks:matrix:{cross_check_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    result = get_cross_check_matrix(cross_check_id)
    cache_set(cache_key, result)
    return result

@router.post("/api/audit-rules/cross-checks/{cross_check_id}/fields",
             dependencies=[Depends(ensure_no_active_verification_sessions)])
def add_cross_check_field(cross_check_id: int):
    """Append a new reusable comparison row to a Cross-check's matrix."""
    existing = execute_query_pg("SELECT name FROM audit_rule_cross_checks WHERE id = %s", (cross_check_id,))
    if not existing:
        raise HTTPException(404, "Cross-check not found")
    max_row = execute_query_pg(
        "SELECT MAX(sort_order) AS max_order FROM audit_rule_cross_check_fields WHERE cross_check_id = %s",
        (cross_check_id,),
    )
    next_order = (max_row[0]["max_order"] + 1) if max_row and max_row[0]["max_order"] is not None else 0
    field_id = execute_write_pg(
        "INSERT INTO audit_rule_cross_check_fields (cross_check_id, sort_order) VALUES (%s, %s) RETURNING id",
        (cross_check_id, next_order),
    )
    log_audit("Admin", "Update", "AuditRules", f"Added Cross-check field row (Cross-check ID {cross_check_id})")
    invalidate_audit_rules_cache({"entity_type": "Cross-check", "entity_id": cross_check_id,
                                  "entity_name": existing[0]["name"], "action": "Updated"})
    return {
        "field_id": field_id,
        "operator": '=',
        "tolerance": 0,
        "missing_data_behavior": 'not_configured',
        "blocking": False,
        "risk": 'Medium',
    }

@router.delete("/api/audit-rules/cross-check-fields/{field_id}",
               dependencies=[Depends(ensure_no_active_verification_sessions)])
def delete_cross_check_field(field_id: int):
    """Delete a field row and its cells."""
    rows = execute_query_pg("SELECT cross_check_id FROM audit_rule_cross_check_fields WHERE id = %s", (field_id,))
    if not rows:
        raise HTTPException(404, "Cross-check field row not found")
    cross_check_id = rows[0]["cross_check_id"]
    # FKs aren't enforced on this app's connections (see get_db()), so clear cells explicitly.
    # Postgres would cascade anyway; the explicit delete keeps both backends identical.
    execute_write_pg("DELETE FROM audit_rule_cross_check_cells WHERE field_id = %s", (field_id,))
    execute_write_pg("DELETE FROM audit_rule_cross_check_fields WHERE id = %s", (field_id,))
    log_audit("Admin", "Update", "AuditRules", f"Deleted Cross-check field row {field_id} (Cross-check ID {cross_check_id})")
    invalidate_audit_rules_cache({"entity_type": "Cross-check", "entity_id": cross_check_id,
                                  "entity_name": None, "action": "Updated"})
    return {"message": "Cross-check field row deleted"}

@router.put("/api/audit-rules/cross-checks/{cross_check_id}/field-order",
            dependencies=[Depends(ensure_no_active_verification_sessions)])
def reorder_cross_check_fields(cross_check_id: int, order: CrossCheckFieldOrder):
    """Rewrite the display order of a Cross-check's field rows. field_ids must be exactly this
    Cross-check's current set of field-row ids (any permutation)."""
    existing = execute_query_pg("SELECT name FROM audit_rule_cross_checks WHERE id = %s", (cross_check_id,))
    if not existing:
        raise HTTPException(404, "Cross-check not found")
    current = execute_query_pg(
        "SELECT id FROM audit_rule_cross_check_fields WHERE cross_check_id = %s", (cross_check_id,)
    )
    current_ids = {r["id"] for r in current}
    if set(order.field_ids) != current_ids or len(order.field_ids) != len(current_ids):
        raise HTTPException(400, "field_ids must be exactly this Cross-check's current field-row ids")
    for idx, field_id in enumerate(order.field_ids):
        execute_write_pg(
            "UPDATE audit_rule_cross_check_fields SET sort_order = %s WHERE id = %s",
            (idx, field_id),
        )
    log_audit("Admin", "Update", "AuditRules", f"Reordered Cross-check field rows (Cross-check ID {cross_check_id})")
    invalidate_audit_rules_cache({"entity_type": "Cross-check", "entity_id": cross_check_id,
                                  "entity_name": existing[0]["name"], "action": "Updated"})
    return {"message": "Cross-check field rows reordered"}

@router.put("/api/audit-rules/cross-check-fields/{field_id}/cells/{document_id}",
            dependencies=[Depends(ensure_no_active_verification_sessions)])
def set_cross_check_cell(field_id: int, document_id: int, cell: CrossCheckCellUpdate):
    """Set a field available for the Document's type, or clear an empty/None cell."""
    validate_cross_check_cell(field_id, document_id, cell.column_name)
    normalized = normalize_compare_field(cell.column_name)
    if normalized is None:
        execute_write_pg(
            "DELETE FROM audit_rule_cross_check_cells WHERE field_id = %s AND document_id = %s",
            (field_id, document_id),
        )
        message = "Cross-check cell cleared"
    else:
        execute_write_pg(
            "INSERT INTO audit_rule_cross_check_cells (field_id, document_id, column_name) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (field_id, document_id) DO UPDATE SET column_name = EXCLUDED.column_name",
            (field_id, document_id, normalized),
        )
        message = "Cross-check cell set"
    log_audit("Admin", "Update", "AuditRules", f"Set Cross-check cell (field {field_id}, document {document_id})")
    invalidate_audit_rules_cache({"entity_type": "Cross-check", "entity_id": None,
                                  "entity_name": None, "action": "Updated"})
    return {"message": message}


@router.put("/api/audit-rules/cross-check-fields/{field_id}/condition",
            dependencies=[Depends(ensure_no_active_verification_sessions)])
def set_cross_check_condition(field_id: int, condition: CrossCheckConditionUpdate):
    """Persist the complete reusable condition owned by one Cross-check matrix row."""
    validate_cross_check_condition(condition)
    rows = execute_query_pg(
        "SELECT cross_check_id FROM audit_rule_cross_check_fields WHERE id = %s", (field_id,)
    )
    if not rows:
        raise HTTPException(404, "Cross-check field row not found")
    cross_check_id = rows[0]["cross_check_id"]
    execute_write_pg(
        "UPDATE audit_rule_cross_check_fields SET operator = %s, tolerance = %s, "
        "missing_data_behavior = %s, blocking = %s, risk = %s WHERE id = %s",
        (condition.operator, condition.tolerance, condition.missing_data_behavior,
         condition.blocking, condition.risk, field_id),
    )
    log_audit("Admin", "Update", "AuditRules", f"Updated Cross-check condition row {field_id}")
    invalidate_audit_rules_cache({"entity_type": "Cross-check", "entity_id": cross_check_id,
                                  "entity_name": None, "action": "Updated"})
    return {"message": "Cross-check condition updated"}


# ---------- COLUMN AXIS + META (Cross-Check Matrix Redesign, Phase 5 of 5) ----------
# The matrix UI persists each edit immediately. The row/cell endpoints above already cover the
# field axis; these two cover what the old flat CRUD (create/update_cross_check) can't do
# cleanly for a matrix cross-check:
#   /documents - set the column axis (audit_rule_cross_check_documents) on its own, without the
#                old model's required compare_field / per-document overrides.
#   /meta      - save just Name / Comparison Type / voucher-line flag, leaving compare_field
#                (a deferred-drop column) untouched.
# create_cross_check() is still used to make a new matrix cross-check - the frontend sends a
# filler compare_field with document_ids=[] (validation loop skipped), then builds the matrix
# via these + the row/cell endpoints.

class CrossCheckDocuments(BaseModel):
    document_ids: List[int]

class CrossCheckMeta(BaseModel):
    name: str
    comparison_type: str
    compare_to_voucher_line: bool = False

def save_cross_check_column_axis(cross_check_id: int, document_ids: List[int]):
    """Replace a Cross-check's document columns (delete-then-reinsert), then drop any matrix cell
    left pointing at a document that's no longer a column. Distinct from save_cross_check_documents()
    which also writes the old model's compare_field / document_fields - this one is matrix-only."""
    execute_write_pg("DELETE FROM audit_rule_cross_check_documents WHERE cross_check_id = %s", (cross_check_id,))
    for idx, document_id in enumerate(document_ids):
        execute_write_pg(
            "INSERT INTO audit_rule_cross_check_documents (cross_check_id, document_id, sort_order, compare_field) "
            "VALUES (%s, %s, %s, NULL)",
            (cross_check_id, document_id, idx),
        )
    # Orphan-cell cleanup: a cell keys off (field_id, document_id); if the document is gone from
    # the column axis its cells are dead. FKs aren't enforced on this app's SQLite connections.
    if document_ids:
        placeholders = ",".join("%s" for _ in document_ids)
        execute_write_pg(
            f"DELETE FROM audit_rule_cross_check_cells WHERE field_id IN "
            f"(SELECT id FROM audit_rule_cross_check_fields WHERE cross_check_id = %s) "
            f"AND document_id NOT IN ({placeholders})",
            (cross_check_id, *document_ids),
        )
    else:
        execute_write_pg(
            "DELETE FROM audit_rule_cross_check_cells WHERE field_id IN "
            "(SELECT id FROM audit_rule_cross_check_fields WHERE cross_check_id = %s)",
            (cross_check_id,),
        )

@router.put("/api/audit-rules/cross-checks/{cross_check_id}/documents",
            dependencies=[Depends(ensure_no_active_verification_sessions)])
def set_cross_check_documents(cross_check_id: int, payload: CrossCheckDocuments):
    """Replace a Cross-check's document columns (the matrix's column axis)."""
    existing = execute_query_pg("SELECT name FROM audit_rule_cross_checks WHERE id = %s", (cross_check_id,))
    if not existing:
        raise HTTPException(404, "Cross-check not found")
    for document_id in payload.document_ids:
        doc = execute_query_pg("SELECT id FROM audit_rule_documents WHERE id = %s", (document_id,))
        if not doc:
            raise HTTPException(400, f"Document ID {document_id} not found")
    save_cross_check_column_axis(cross_check_id, payload.document_ids)
    log_audit("Admin", "Update", "AuditRules", f"Set Cross-check document columns (Cross-check ID {cross_check_id})")
    invalidate_audit_rules_cache({"entity_type": "Cross-check", "entity_id": cross_check_id,
                                  "entity_name": existing[0]["name"], "action": "Updated"})
    return {"message": "Cross-check document columns updated"}

@router.put("/api/audit-rules/cross-checks/{cross_check_id}/meta",
            dependencies=[Depends(ensure_no_active_verification_sessions)])
def update_cross_check_meta(cross_check_id: int, meta: CrossCheckMeta):
    """Save a Cross-check's Name / Comparison Type / voucher-line flag only. Leaves compare_field
    and the document/field axes untouched (those persist through their own endpoints)."""
    existing = execute_query_pg("SELECT id FROM audit_rule_cross_checks WHERE id = %s", (cross_check_id,))
    if not existing:
        raise HTTPException(404, "Cross-check not found")
    if meta.comparison_type not in COMPARISON_TYPES:
        raise HTTPException(400, f"Invalid comparison type '{meta.comparison_type}'. Must be one of: {', '.join(COMPARISON_TYPES)}")
    execute_write_pg(
        "UPDATE audit_rule_cross_checks SET name = %s, comparison_type = %s, compare_to_voucher_line = %s WHERE id = %s",
        (meta.name, meta.comparison_type, meta.compare_to_voucher_line, cross_check_id),
    )
    log_audit("Admin", "Update", "AuditRules", f"Updated Cross-check meta (ID {cross_check_id})")
    invalidate_audit_rules_cache({"entity_type": "Cross-check", "entity_id": cross_check_id,
                                  "entity_name": meta.name, "action": "Updated"})
    return {"message": "Cross-check updated successfully"}


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


class PhysicalBillMigrationPreviewRequest(BaseModel):
    source_document_id: int
    target_document_id: int
    field_mapping: Dict[str, int] = {}


class PhysicalBillMigrationApplyRequest(PhysicalBillMigrationPreviewRequest):
    confirm: bool = False


LEGACY_PHYSICAL_BILL_FIELDS = (
    "Bill No", "Bill Date", "Vendor Name", "Item Name", "Quantity", "Rate", "Amount"
)

LEGACY_PHYSICAL_BILL_FIELD_TYPES = {
    "Bill No": "text",
    "Bill Date": "date",
    "Vendor Name": "text",
    "Item Name": "text",
    "Quantity": "numeric",
    "Rate": "numeric",
    "Amount": "numeric",
}

class CategoryValidationRules(BaseModel):
    procedure_evidence: Dict[str, Any] = {}
    # Legacy Category-owned comparisons are retained for compatibility while Cross-checks are
    # moved to their canonical reusable definitions. Omitted means "preserve existing legacy
    # data" during a Procedure/Evidence-only save.
    mathematical_cross_checks: Optional[List[Dict[str, Any]]] = None

def get_category_validation_rules(category_id: int) -> dict:
    rows = execute_query_pg("SELECT validation_rules_json FROM audit_rule_categories WHERE id = %s", (category_id,))
    if not rows:
        raise HTTPException(404, "Category not found")
    raw = rows[0].get("validation_rules_json")
    if not raw:
        return {"procedure_evidence": {}, "mathematical_cross_checks": []}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        value = {}
    return _parse_category_validation_rules(value)

def validate_category_validation_rules(rules: CategoryValidationRules, category_id: Optional[int] = None):
    allowed_missing = {"not_configured", "not_linked", "missing", "shortfall"}
    allowed_conditions = {"required_document", "prepared_by", "checked_by", "approved_by", "signature"}
    allowed_risks = {"Low", "Medium", "High"}
    category_document_ids = None
    if category_id is not None:
        category_document_ids = {
            row["document_id"]
            for row in execute_query_pg(
                "SELECT document_id FROM audit_rule_category_documents WHERE category_id = %s",
                (category_id,)
            )
        }
    for index, rule in enumerate((rules.procedure_evidence or {}).get("checks", [])):
        if not isinstance(rule, dict) or not rule.get("document_id") or rule.get("condition") not in allowed_conditions:
            raise HTTPException(400, f"Procedure/Evidence row {index + 1} needs a document and valid condition")
        if rule.get("risk") not in allowed_risks:
            raise HTTPException(400, f"Procedure/Evidence row {index + 1} needs a valid risk level")
        detail = rule.get("sub_value") or rule.get("detail") or rule.get("selected_detail")
        if rule.get("condition") != "required_document" and not detail:
            raise HTTPException(400, f"Procedure/Evidence row {index + 1} needs a role or signatory detail")
        try:
            document_id = int(rule["document_id"])
        except (TypeError, ValueError):
            raise HTTPException(400, f"Procedure/Evidence row {index + 1} must use a valid Document ID")
        if category_document_ids is not None and document_id not in category_document_ids:
            raise HTTPException(
                400,
                f"Procedure/Evidence row {index + 1} uses Document ID {document_id}, "
                "which is not included in this Category's Applicable Documents"
            )
        rows = execute_query_pg("SELECT id FROM audit_rule_documents WHERE id = %s AND is_active = TRUE", (document_id,))
        if not rows:
            raise HTTPException(400, f"Procedure/Evidence row {index + 1} must use an active document")
    # The current Category UI intentionally omits this legacy field. Pydantic represents the
    # omission as None, while the save endpoint separately preserves any older stored rules.
    for index, rule in enumerate(rules.mathematical_cross_checks or []):
        required = ("left_document_id", "left_field", "right_document_id", "right_field", "operator")
        if any(not rule.get(key) for key in required):
            raise HTTPException(400, f"Mathematical Cross-check row {index + 1} needs two documents, two fields, and an operator")
        document_ids = {rule["left_document_id"], rule["right_document_id"]}
        for document_id in document_ids:
            rows = execute_query_pg("SELECT id FROM audit_rule_documents WHERE id = %s AND is_active = TRUE AND document_type = 'linked_template'", (document_id,))
            if not rows:
                raise HTTPException(400, f"Mathematical Cross-check row {index + 1} must use active Template documents")
        if rule.get("missing_data_behavior") and rule["missing_data_behavior"] not in allowed_missing:
            raise HTTPException(400, f"Mathematical Cross-check row {index + 1} has an invalid missing-data behavior")

# ---------- HELPERS ----------
def get_physical_bill_migration_inventory() -> dict:
    """Return a read-only inventory for the controlled Physical Bill-to-Form migration."""
    documents = execute_query_pg(
        """
        SELECT id, name, document_type, is_active
        FROM audit_rule_documents
        WHERE document_type IN ('physical_bill', 'form')
        ORDER BY document_type, name, id
        """
    )
    category_rows = execute_query_pg(
        """
        SELECT cd.document_id, cd.category_id, c.name AS category_name, cd.is_required
        FROM audit_rule_category_documents cd
        JOIN audit_rule_categories c ON c.id = cd.category_id
        JOIN audit_rule_documents d ON d.id = cd.document_id
        WHERE d.document_type = 'physical_bill'
        ORDER BY cd.document_id, c.name, c.id
        """
    )
    bill_rows = execute_query_pg(
        """
        SELECT document_id, COUNT(*) AS bill_count,
               COUNT(DISTINCT voucher_line_id) AS line_count
        FROM voucher_line_physical_bills
        GROUP BY document_id
        """
    )
    cross_check_rows = execute_query_pg(
        """
        SELECT ccd.document_id, COUNT(DISTINCT ccd.cross_check_id) AS cross_check_count
        FROM audit_rule_cross_check_documents ccd
        JOIN audit_rule_documents d ON d.id = ccd.document_id
        WHERE d.document_type = 'physical_bill'
        GROUP BY ccd.document_id
        """
    )
    form_field_rows = execute_query_pg(
        """
        SELECT f.document_id, f.id, f.field_name, f.field_type, f.field_scope, f.sort_order
        FROM audit_rule_document_fields f
        JOIN audit_rule_documents d ON d.id = f.document_id
        WHERE d.document_type = 'form'
        ORDER BY f.document_id, f.sort_order, f.id
        """
    )

    categories_by_document = {}
    for row in category_rows:
        categories_by_document.setdefault(row["document_id"], []).append({
            "category_id": row["category_id"],
            "category_name": row["category_name"],
            "is_required": bool(row["is_required"]),
        })
    bills_by_document = {
        row["document_id"]: {
            "bill_count": int(row["bill_count"]),
            "line_count": int(row["line_count"]),
        }
        for row in bill_rows
    }
    cross_checks_by_document = {
        row["document_id"]: int(row["cross_check_count"])
        for row in cross_check_rows
    }
    fields_by_document = {}
    for row in form_field_rows:
        fields_by_document.setdefault(row["document_id"], []).append({
            "field_id": row["id"],
            "field_name": row["field_name"],
            "field_type": row["field_type"],
            "field_scope": row["field_scope"],
            "sort_order": row["sort_order"],
        })

    inventory = []
    for document in documents:
        document_id = document["id"]
        inventory.append({
            "document_id": document_id,
            "name": document["name"],
            "document_type": document["document_type"],
            "is_active": bool(document["is_active"]),
            "categories": categories_by_document.get(document_id, []),
            "legacy_data": bills_by_document.get(document_id, {"bill_count": 0, "line_count": 0}),
            "cross_check_count": cross_checks_by_document.get(document_id, 0),
            "fields": fields_by_document.get(document_id, []),
        })

    return {
        "legacy_fields": list(LEGACY_PHYSICAL_BILL_FIELDS),
        "documents": inventory,
        "warnings": [
            "This inventory is read-only. No legacy rows or Category references are changed.",
            "A migration requires an explicit source Document, target Form, and field mapping.",
        ],
    }


@router.get("/api/audit-rules/physical-bill-migration/inventory")
def physical_bill_migration_inventory():
    return get_physical_bill_migration_inventory()


def _get_physical_bill_migration_reference_plan(source_document_id: int, target_document_id: int) -> dict:
    """Return legacy references and any target collisions without changing data."""
    categories = execute_query_pg(
        """
        SELECT source.id, source.category_id, source.is_required,
               target.id AS target_id
        FROM audit_rule_category_documents source
        LEFT JOIN audit_rule_category_documents target
          ON target.category_id = source.category_id
         AND target.document_id = %s
        WHERE source.document_id = %s
        ORDER BY source.category_id, source.id
        """,
        (target_document_id, source_document_id),
    )
    cross_checks = execute_query_pg(
        """
        SELECT source.id, source.cross_check_id, source.sort_order, source.compare_field,
               target.id AS target_id
        FROM audit_rule_cross_check_documents source
        LEFT JOIN audit_rule_cross_check_documents target
          ON target.cross_check_id = source.cross_check_id
         AND target.document_id = %s
        WHERE source.document_id = %s
        ORDER BY source.cross_check_id, source.id
        """,
        (target_document_id, source_document_id),
    )
    procedure_checks = execute_query_pg(
        """
        SELECT source.id, source.voucher_line_id, source.role_name,
               target.id AS target_id
        FROM voucher_line_procedure_checks source
        LEFT JOIN voucher_line_procedure_checks target
          ON target.voucher_line_id = source.voucher_line_id
         AND target.document_id = %s
         AND target.role_name = source.role_name
        WHERE source.document_id = %s
        ORDER BY source.voucher_line_id, source.id
        """,
        (target_document_id, source_document_id),
    )
    observations = execute_query_pg(
        """
        SELECT source.id, source.voucher_line_id, source.observation_text,
               source.synced, source.last_synced_text, source.updated_by,
               target.id AS target_id
        FROM voucher_line_observations source
        LEFT JOIN voucher_line_observations target
          ON target.voucher_line_id = source.voucher_line_id
         AND target.document_id = %s
        WHERE source.document_id = %s
        ORDER BY source.voucher_line_id, source.id
        """,
        (target_document_id, source_document_id),
    )
    return {
        "categories": categories,
        "cross_checks": cross_checks,
        "procedure_checks": procedure_checks,
        "observations": observations,
    }


def _physical_bill_migration_mapping(request: PhysicalBillMigrationPreviewRequest,
                                      target_fields: List[dict], blockers: List[str]) -> dict:
    """Validate the explicit legacy-field-to-Form-field mapping."""
    fields_by_id = {int(row["id"]): row for row in target_fields}
    field_mapping = request.field_mapping or {}
    required_scopes = {
        "Bill No": "header", "Bill Date": "header", "Vendor Name": "header",
        "Item Name": "item", "Quantity": "item", "Rate": "item", "Amount": "item",
    }
    resolved_mapping = {}
    used_field_ids = {}
    for legacy_name in LEGACY_PHYSICAL_BILL_FIELDS:
        field_id = field_mapping.get(legacy_name)
        field = fields_by_id.get(field_id)
        if not field:
            blockers.append(f"Missing target Form field mapping for '{legacy_name}'.")
            continue
        expected_scope = required_scopes[legacy_name]
        if (field.get("field_scope") or "header") != expected_scope:
            blockers.append(
                f"Target field '{field['field_name']}' for '{legacy_name}' must use "
                f"{expected_scope} scope."
            )
        expected_type = LEGACY_PHYSICAL_BILL_FIELD_TYPES[legacy_name]
        actual_type = field.get("field_type") or "text"
        if actual_type != expected_type:
            blockers.append(
                f"Target field '{field['field_name']}' for '{legacy_name}' must use "
                f"{expected_type} type."
            )
        if field_id in used_field_ids:
            blockers.append(
                f"Target Form field '{field['field_name']}' is mapped more than once "
                f"('{used_field_ids[field_id]}' and '{legacy_name}')."
            )
        else:
            used_field_ids[field_id] = legacy_name
        resolved_mapping[legacy_name] = {
            "field_id": field["id"],
            "field_name": field["field_name"],
            "field_type": actual_type,
            "field_scope": field.get("field_scope") or "header",
        }
    return resolved_mapping


def _add_physical_bill_reference_blockers(reference_plan: dict, blockers: List[str]):
    """Reject ambiguous merges instead of silently combining existing target data."""
    for row in reference_plan["categories"]:
        if row.get("target_id") is not None:
            blockers.append(
                f"Category {row['category_id']} already contains the target Form; "
                "remove the duplicate reference before migration."
            )
    for row in reference_plan["cross_checks"]:
        if row.get("target_id") is not None:
            blockers.append(
                f"Cross-check {row['cross_check_id']} already contains the target Form; "
                "remove the duplicate reference before migration."
            )
    for row in reference_plan["procedure_checks"]:
        if row.get("target_id") is not None:
            blockers.append(
                f"Voucher line {row['voucher_line_id']} already has a target Form "
                f"procedure check for role '{row['role_name']}'."
            )
    for row in reference_plan["observations"]:
        if row.get("target_id") is not None:
            blockers.append(
                f"Voucher line {row['voucher_line_id']} already has a target Form observation."
            )


@router.post("/api/audit-rules/physical-bill-migration/preview")
def preview_physical_bill_migration(request: PhysicalBillMigrationPreviewRequest):
    documents = execute_query_pg(
        """
        SELECT id, name, document_type, is_active
        FROM audit_rule_documents
        WHERE id IN (%s, %s)
        """,
        (request.source_document_id, request.target_document_id),
    )
    by_id = {row["id"]: row for row in documents}
    source = by_id.get(request.source_document_id)
    target = by_id.get(request.target_document_id)
    blockers = []
    if not source or source["document_type"] != "physical_bill":
        blockers.append("Source Document must be an active legacy Physical Bill Document.")
    elif not source["is_active"]:
        blockers.append("Source Physical Bill Document is inactive.")
    if not target or target["document_type"] != "form":
        blockers.append("Target Document must be an active Form Document.")
    elif not target["is_active"]:
        blockers.append("Target Form Document is inactive.")

    target_fields = execute_query_pg(
        """
        SELECT id, field_name, field_type, field_scope, sort_order
        FROM audit_rule_document_fields
        WHERE document_id = %s
        ORDER BY sort_order, id
        """,
        (request.target_document_id,),
    )
    resolved_mapping = _physical_bill_migration_mapping(request, target_fields, blockers)

    bill_count_rows = execute_query_pg(
        "SELECT COUNT(*) AS bill_count FROM voucher_line_physical_bills WHERE document_id = %s",
        (request.source_document_id,),
    )
    existing_form_rows = execute_query_pg(
        "SELECT COUNT(*) AS entry_count FROM voucher_line_form_entries WHERE document_id = %s",
        (request.target_document_id,),
    )
    existing_target_entry_count = int((existing_form_rows or [{"entry_count": 0}])[0]["entry_count"])
    if existing_target_entry_count:
        blockers.append(
            "Target Form already contains evidence entries; migration will not merge or overwrite them."
        )
    reference_plan = _get_physical_bill_migration_reference_plan(
        request.source_document_id, request.target_document_id
    )
    _add_physical_bill_reference_blockers(reference_plan, blockers)
    return {
        "ready": not blockers,
        "source_document": source,
        "target_document": target,
        "field_mapping": resolved_mapping,
        "legacy_bill_count": int((bill_count_rows or [{"bill_count": 0}])[0]["bill_count"]),
        "existing_target_entry_count": existing_target_entry_count,
        "reference_counts": {key: len(rows) for key, rows in reference_plan.items()},
        "reference_plan": reference_plan,
        "blockers": blockers,
        "writes_performed": False,
    }


def _insert_migrated_form_value(entry_id: int, field: dict, value: Any):
    """Insert one legacy value using the target Form field's typed storage column."""
    if value in (None, ""):
        return
    field_type = field["field_type"]
    value_text = value if field_type == "text" else None
    value_date = value if field_type == "date" else None
    value_numeric = value if field_type == "numeric" else None
    execute_write_pg(
        "INSERT INTO voucher_line_form_values "
        "(entry_id, field_id, value_text, value_date, value_numeric, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
        (entry_id, field["field_id"], value_text, value_date, value_numeric),
    )


def _insert_migrated_form_entry(voucher_line_id: int, document_id: int, row_scope: str,
                                sort_order: int, values: List[tuple]):
    """Insert one Form entry and its already-validated field/value pairs."""
    entry_id = execute_write_pg(
        "INSERT INTO voucher_line_form_entries "
        "(voucher_line_id, document_id, row_scope, sort_order, updated_at) "
        "VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) RETURNING id",
        (voucher_line_id, document_id, row_scope, sort_order),
    )
    for field, value in values:
        _insert_migrated_form_value(entry_id, field, value)
    return entry_id


def _apply_physical_bill_migration(request: PhysicalBillMigrationApplyRequest, plan: dict) -> dict:
    """Copy legacy bills and retarget references inside the caller's transaction."""
    field_map = plan["field_mapping"]
    bill_rows = execute_query_pg(
        """
        SELECT id, voucher_line_id, bill_no, bill_date, vendor_name, sort_order
        FROM voucher_line_physical_bills
        WHERE document_id = %s
        ORDER BY voucher_line_id, sort_order, id
        """,
        (request.source_document_id,),
    )
    bill_ids = [row["id"] for row in bill_rows]
    item_rows = []
    if bill_ids:
        placeholders = ",".join("%s" for _ in bill_ids)
        item_rows = execute_query_pg(
            f"""
            SELECT bill_id, item_name, quantity, rate, amount, sort_order
            FROM voucher_line_physical_bill_items
            WHERE bill_id IN ({placeholders})
            ORDER BY bill_id, sort_order, id
            """,
            tuple(bill_ids),
        )
    items_by_bill = {}
    for row in item_rows:
        items_by_bill.setdefault(row["bill_id"], []).append(row)

    migrated_entry_count = 0
    for bill in bill_rows:
        _insert_migrated_form_entry(
            bill["voucher_line_id"], request.target_document_id, "header",
            int(bill.get("sort_order") or 0),
            [
                (field_map["Bill No"], bill.get("bill_no")),
                (field_map["Bill Date"], bill.get("bill_date")),
                (field_map["Vendor Name"], bill.get("vendor_name")),
            ],
        )
        migrated_entry_count += 1
        for item in items_by_bill.get(bill["id"], []):
            _insert_migrated_form_entry(
                bill["voucher_line_id"], request.target_document_id, "item",
                int(item.get("sort_order") or 0),
                [
                    (field_map["Item Name"], item.get("item_name")),
                    (field_map["Quantity"], item.get("quantity")),
                    (field_map["Rate"], item.get("rate")),
                    (field_map["Amount"], item.get("amount")),
                ],
            )
            migrated_entry_count += 1

    references = plan["reference_plan"]
    for row in references["categories"]:
        execute_write_pg(
            "UPDATE audit_rule_category_documents SET document_id = %s WHERE id = %s",
            (request.target_document_id, row["id"]),
        )
    for row in references["cross_checks"]:
        execute_write_pg(
            "UPDATE audit_rule_cross_check_cells SET document_id = %s "
            "WHERE document_id = %s AND field_id IN "
            "(SELECT id FROM audit_rule_cross_check_fields WHERE cross_check_id = %s)",
            (request.target_document_id, request.source_document_id, row["cross_check_id"]),
        )
        execute_write_pg(
            "UPDATE audit_rule_cross_check_documents SET document_id = %s WHERE id = %s",
            (request.target_document_id, row["id"]),
        )
    for row in references["procedure_checks"]:
        execute_write_pg(
            "UPDATE voucher_line_procedure_checks SET document_id = %s WHERE id = %s",
            (request.target_document_id, row["id"]),
        )
    for row in references["observations"]:
        execute_write_pg(
            "UPDATE voucher_line_observations SET document_id = %s WHERE id = %s",
            (request.target_document_id, row["id"]),
        )
    return {
        "migrated_bill_count": len(bill_rows),
        "migrated_form_entry_count": migrated_entry_count,
        "migrated_reference_counts": {key: len(rows) for key, rows in references.items()},
    }


@router.post(
    "/api/audit-rules/physical-bill-migration/apply",
    dependencies=[Depends(ensure_no_active_verification_sessions)],
)
def apply_physical_bill_migration(request: PhysicalBillMigrationApplyRequest):
    if not request.confirm:
        raise HTTPException(400, "Set confirm=true only after reviewing the migration preview.")

    with pg_transaction():
        plan = preview_physical_bill_migration(request)
        if not plan["ready"]:
            raise HTTPException(409, {
                "message": "Physical Bill migration is blocked; no writes were performed.",
                "preview": plan,
            })
        result = _apply_physical_bill_migration(request, plan)

    invalidate_audit_rules_cache({
        "entity_type": "Document",
        "entity_id": request.target_document_id,
        "action": "Physical Bill migration applied",
    })
    log_audit(
        "Admin", "Migrate", "AuditRules",
        f"Migrated legacy Physical Bill Document {request.source_document_id} "
        f"to Form Document {request.target_document_id}.",
    )
    return {
        "message": "Physical Bill migration applied successfully.",
        "source_document_id": request.source_document_id,
        "target_document_id": request.target_document_id,
        "legacy_rows_preserved": True,
        "writes_performed": True,
        **result,
    }


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
        SELECT d.id as document_id, d.name, d.kb_note, d.template_id, d.risk_rating, d.transaction_level_approval, d.document_type, d.is_active
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
            "is_active": bool(r["is_active"]),
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
            "document_type": r["document_type"] or 'linked_template',
            "form_fields": get_document_form_fields(r["document_id"])
                if (r["document_type"] or 'linked_template') == 'form' else []
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
        # Legacy Category Evidence Type is returned raw, including NULL, for migration/audit
        # visibility only. It is no longer authored or used as an active policy.
        "evidence_type": row.get("evidence_type"),
        # Validation Rules (Phase 2 of 7): bumped by bump_category_matrix_version() whenever
        # this Category's document list or is_required flags change.
        "matrix_version": row.get("matrix_version", 1),
        "documents": get_category_documents(category_id),
        "cross_checks": get_category_cross_checks(category_id),
        "validation_rules": get_category_validation_rules(category_id),
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

@router.get("/api/audit-rules/categories/{category_id}/validation-rules")
def get_category_validation_rules_endpoint(category_id: int):
    return get_category_validation_rules(category_id)

@router.put("/api/audit-rules/categories/{category_id}/validation-rules", dependencies=[Depends(ensure_no_active_verification_sessions)])
def update_category_validation_rules(category_id: int, rules: CategoryValidationRules):
    existing = execute_query_pg("SELECT id FROM audit_rule_categories WHERE id = %s", (category_id,))
    if not existing:
        raise HTTPException(404, "Category not found")
    validate_category_validation_rules(rules, category_id=category_id)
    payload_data = rules.dict(exclude_none=True)
    if rules.mathematical_cross_checks is None:
        # Do not erase pre-existing Category-owned comparisons merely because the new UI no
        # longer edits them. They remain visibly legacy until the administrator recreates them
        # as reusable Cross-check definitions.
        payload_data["mathematical_cross_checks"] = get_category_validation_rules(category_id).get(
            "mathematical_cross_checks", []
        )
    payload = Json(payload_data)
    execute_write_pg("UPDATE audit_rule_categories SET validation_rules_json = %s WHERE id = %s", (payload, category_id))
    log_audit("Admin", "Update", "AuditRules", f"Updated validation rules for Category ID {category_id}")
    invalidate_audit_rules_cache({"entity_type": "Category", "entity_id": category_id, "action": "Updated"})
    return get_category_validation_rules(category_id)

@router.post("/api/audit-rules/categories", dependencies=[Depends(ensure_no_active_verification_sessions)])
def create_category(category: CategoryCreate):
    validate_category_payload(category.document_ids, category.cross_check_ids)

    category_id = execute_write_pg(
        "INSERT INTO audit_rule_categories (name, kb_overview) VALUES (%s, %s) RETURNING id",
        (category.name, category.kb_overview)
    )
    save_category_documents(category_id, category.document_ids)
    save_category_cross_checks(category_id, category.cross_check_ids)

    log_audit("Admin", "Create", "AuditRules", f"Created Category: {category.name}")
    invalidate_audit_rules_cache({"entity_type": "Category", "entity_id": category_id,
                                  "entity_name": category.name, "action": "Created"})
    return {"message": f"Category '{category.name}' created successfully", "category_id": category_id}

@router.put("/api/audit-rules/categories/{category_id}", dependencies=[Depends(ensure_no_active_verification_sessions)])
def update_category(category_id: int, category: CategoryUpdate):
    existing = execute_query_pg("SELECT id FROM audit_rule_categories WHERE id = %s", (category_id,))
    if not existing:
        raise HTTPException(404, "Category not found")

    validate_category_payload(category.document_ids, category.cross_check_ids)

    # Validation Rules (Phase 2 of 7): capture the document set before it's replaced, so a
    # real membership change (add/remove - reordering alone doesn't count) can trigger a
    # matrix_version bump below. Cross-check changes deliberately do not bump the version.
    old_document_ids = set(get_category_document_requireds(category_id).keys())

    execute_write_pg(
        "UPDATE audit_rule_categories SET name = %s, kb_overview = %s WHERE id = %s",
        (category.name, category.kb_overview, category_id)
    )
    save_category_documents(category_id, category.document_ids)
    save_category_cross_checks(category_id, category.cross_check_ids)

    if old_document_ids != set(category.document_ids):
        bump_category_matrix_version(category_id)

    log_audit("Admin", "Update", "AuditRules", f"Updated Category ID {category_id}")
    invalidate_audit_rules_cache({"entity_type": "Category", "entity_id": category_id,
                                  "entity_name": category.name, "action": "Updated"})
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
    invalidate_audit_rules_cache({"entity_type": "Category", "entity_id": category_id,
                                  "entity_name": existing[0]['name'], "action": "Deleted"})
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
    invalidate_audit_rules_cache({"entity_type": "Category", "entity_id": category_id,
                                  "entity_name": existing[0]['name'], "action": "Restored"})
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

    existing = execute_query_pg("SELECT id FROM audit_rule_categories WHERE id = %s", (category_id,))
    if not existing:
        raise HTTPException(404, "Category not found")

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
    invalidate_audit_rules_cache({"entity_type": "Voucher-level Check", "entity_id": voucher_check_id,
                                  "entity_name": voucher_check.name, "action": "Created"})
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
    invalidate_audit_rules_cache({"entity_type": "Voucher-level Check", "entity_id": voucher_check_id,
                                  "entity_name": voucher_check.name, "action": "Updated"})
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
    invalidate_audit_rules_cache({"entity_type": "Voucher-level Check", "entity_id": voucher_check_id,
                                  "entity_name": existing[0]['name'], "action": "Deleted"})
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
    invalidate_audit_rules_cache({"entity_type": "Voucher-level Check", "entity_id": voucher_check_id,
                                  "entity_name": existing[0]['name'], "action": "Restored"})
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
    invalidate_audit_rules_cache({"entity_type": "Voucher-level Check", "entity_name": "Voucher-level Checks",
                                  "action": "Reordered"})
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
