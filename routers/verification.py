# ==============================================
# ✅ VERIFICATION MODULE – PHASES 2, 3, 4
# Cashbook Identification, Session Start,
# Verification Workflow, Audited Cashbook & Export
# ==============================================

from fastapi import APIRouter, HTTPException, Query, Body, Depends
from fastapi.responses import StreamingResponse
from database import execute_query_pg, execute_write_pg, execute_batch_pg, execute_batch_insert_with_ids_pg, log_audit, log_data_audit_pg, get_raw_conn_pg, release_raw_conn_pg, get_client_voucher_prefixes_pg, pg_transaction
from database import start_db_metrics, get_db_metrics, stop_db_metrics
from psycopg2.extras import Json
from datetime import datetime, timezone
import json
import math
import re
import time
from typing import List, Optional, Dict, Any
import io
from functools import wraps
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
from rapidfuzz import fuzz, utils as fuzz_utils
# Reused for voucher/line grouping in get_next_transaction (Phase 1) — same document-key
# logic already used by import's compare_transactions, applied at query time.
from routers.import_export import get_document_group_id
from routers.templates import get_cached_template_columns
# Create Vendor Bill from GRN (Phase 1): reused as-is rather than duplicated, since this
# feature must build a client_data row that behaves identically to a real import - same
# rationale as the cross-router imports below.
from routers.import_export import (
    find_named_column, get_rate_precision, reorder_row_data_by_template,
    extract_columns_from_row, get_critical_field_columns, compute_data_quality_flag,
    DEFAULT_DATE_DELIMITER, DEFAULT_DATE_ORDER,
)
# Phase 9 (Audit Rules): audit guidance now sources from Audit Rules Categories instead of
# the old hardcoded audit_procedure_library table — same cross-router import pattern as above.
# cache_get/cache_set (Modal Caching Phase 3, sub-phase 2): get_audit_guidance() below reads
# through the same process-level cache audit_rules.py's own GET endpoints use — guidance
# content is keyed by Category name/id, not client_id, since Categories aren't client-scoped
# (see cache_get()/cache_set()'s own docstring in audit_rules.py for why).
from routers.audit_rules import get_category_guidance_documents, get_category_guidance_cross_checks, get_category_documents, check_approved_by_authority_gate, get_document_auth_roles_batch, cache_get, cache_set, category_for_account_cache_key, get_audit_rules_config_version, load_client_account_category_index, load_client_verification_rulebook
from database import get_audit_rules_config_changes_since
from routers.auth import get_current_user
# "Why is this line Query?" gate breakdown only (see compute_and_persist_cleared_status()'s
# detail mode) — the pass/fail answer itself still comes from check_approved_by_authority_gate()
# above, so the explanation can never disagree with the real gate.
from routers.audit_rules import get_required_documents_with_authority, has_matching_active_specimen
# Check #1's no-template-Document satisfaction rule (2026-08-08 fix) needs a Document's full
# role list, not just the ones with a Required Authority set (get_document_auth_role_authorities
# above only returns the latter) - already used identically by static/verification.js's own
# isProcedureDocumentComplete() no-template branch.
from routers.audit_rules import get_document_auth_roles

router = APIRouter()

# ==============================================
# HELPER FUNCTIONS
# ==============================================

# Postgres cutover: checked_at/updated_at columns are TIMESTAMPTZ now, not SQLite TEXT
# CURRENT_TIMESTAMP strings - psycopg2 returns real (tz-aware) datetime objects. The
# `(r['checked_at'] or '') > (existing['checked_at'] or '')` "sorts correctly as a string"
# pattern used throughout this file to find the most-recently-touched sibling row breaks
# under Postgres: comparing a real datetime against the '' fallback (or two rows where one
# is None and the other a datetime) raises TypeError. This sentinel/helper pair replaces the
# `or ''` fallback with a real minimum datetime, so comparisons stay well-typed either way.
_MIN_TS = datetime.min.replace(tzinfo=timezone.utc)

def _ts_sort_key(value):
    return value if value is not None else _MIN_TS

def match_voucher_category(voucher_id: str, client_prefixes: dict) -> Optional[str]:
    """
    Case-insensitive "starts with" match of voucher_id against the client's configured
    per-category prefixes. Returns one of 'Cash Payment'/'Cash Receipt'/'Bank Payment'/
    'Bank Receipt'/'Journal', or None if nothing matches (including a client with no
    prefixes configured for any category).
    """
    if not voucher_id:
        return None
    voucher_id_lower = voucher_id.strip().lower()
    for category, prefixes in client_prefixes.items():
        for prefix in prefixes:
            if voucher_id_lower.startswith(prefix.strip().lower()):
                return category
    return None

def voucher_type_label(category: str) -> Optional[str]:
    """
    Map a client_voucher_prefixes category directly to its voucher type label
    (CPV/CRV/BPV/BRV/JV). The 5-way category scheme already encodes Payment vs Receipt
    (Cash Payment/Cash Receipt/Bank Payment/Bank Receipt/Journal), so no separate
    debit/credit-side input is needed here anymore.
    """
    return {
        'Cash Payment': 'CPV',
        'Cash Receipt': 'CRV',
        'Bank Payment': 'BPV',
        'Bank Receipt': 'BRV',
        'Journal': 'JV',
    }.get(category)

def get_column_value(row_data: dict, aliases: list) -> Optional[str]:
    """
    Try to get a value from row_data using a list of possible column name aliases.
    Returns the first non-None value found.
    """
    for alias in aliases:
        if alias in row_data:
            val = row_data[alias]
            if val is not None and val != '':
                return val
    return None

# Validation Rules Phase 4 (status engine): same account-code alias list as
# verification.js's VOUCHER_LINE_ACCOUNT_CODE_ALIASES, ported here so the backend can
# resolve a line's Category the same way the frontend's Type cell already does (via
# GET /api/verification/category-for-account) without a network round-trip.
ACCOUNT_CODE_ALIASES = ['Account Detail', 'Account Details', 'Account Code', 'Account No', 'Account Number', 'Code', 'Ledger Code', 'AccountID', 'Account Head', 'Ledger Detail']

def get_account_code_value(row_data: dict) -> Optional[str]:
    """
    get_column_value(row_data, ACCOUNT_CODE_ALIASES), coerced to str (Postgres cutover fix).
    An account code column is typically declared Text, but a purely-numeric code (e.g. "5001")
    often comes back from the Excel import as a raw Python int/float in row_data (JSONB doesn't
    force a declared column's Python type). Every caller here immediately binds this value into
    a query comparing it against a real TEXT column (trial_balance_accounts.account_code,
    account_category_assignments.account_code) - Postgres raises "operator does not exist: text
    = integer" for a bare int/float param there, whereas SQLite's dynamic typing silently
    tolerated the mismatch. str() once here, at the single resolution point, is simpler and
    safer than fixing it at every call site.
    """
    value = get_column_value(row_data, ACCOUNT_CODE_ALIASES)
    return str(value) if value is not None else None

# Audit Workbook Phase 1 (backend rollup endpoint): same alias list as verification.js's
# VOUCHER_LINE_DESCRIPTION_ALIASES, ported here so a voucher row's description can be resolved
# server-side the same way the frontend already does for the voucher table.
DESCRIPTION_ALIASES = ['Description', 'Voucher Detail', 'Narration', 'Remarks', 'Particulars']

# Automated cross-document validation engine, Phase 8 of 10 (fixed): each Cross-check gets its
# own real, hidden audit_rule_documents row for its shortfall observations, created lazily the
# first time it's needed and remembered on audit_rule_cross_checks.shortfall_document_id - one row
# per (voucher_line_id, cross_check_id), same as before, still via voucher_line_observations'
# existing UNIQUE(voucher_line_id, document_id) dedupe key.
#
# Superseded design, kept here as a comment since the failure mode is instructive: this used to be
# a fixed negative-ID sentinel (CROSS_CHECK_SHORTFALL_DOC_ID_BASE - cross_check_id) that was never
# a real row, on the assumption (true for SQLite, which never enables PRAGMA foreign_keys) that a
# non-existent id is safe at the DB level. Postgres's real FK on
# voucher_line_observations.document_id rejects it outright - _write_shortfall_observations()'s
# own try/except swallowed that error locally, but couldn't undo Postgres's resulting "transaction
# aborted" state, so every later statement in the same shared Save-and-Verify transaction failed
# too. Dormant since Phase 8 was written (no Category had a real active Cross-check until the
# cross-check matrix redesign); confirmed root cause of the "Verify & Next Line" 500 on
# CPV-0826-180, 2026-09-02.
def _get_or_create_cross_check_shortfall_document_id(cross_check_id: int) -> int:
    rows = execute_query_pg(
        "SELECT shortfall_document_id FROM audit_rule_cross_checks WHERE id = %s",
        (cross_check_id,)
    )
    if rows and rows[0]['shortfall_document_id'] is not None:
        return rows[0]['shortfall_document_id']

    # Lazily create the one real Document row this Cross-check will reuse from now on. is_active
    # = FALSE so it's naturally excluded from every existing "WHERE is_active = TRUE" document
    # query (Documents tab, Rulebook compiler, evidence-linking pickers) with no further change
    # needed there; is_system = TRUE is the explicit "hidden system row" flag, also used by
    # sync_line_observations_to_voucher()/sync_voucher_observations_to_voucher() below to label
    # this entry instead of the old numeric-range test.
    new_id = execute_write_pg(
        """
        INSERT INTO audit_rule_documents (name, is_active, is_system, document_type)
        VALUES (%s, FALSE, TRUE, 'linked_template')
        RETURNING id
        """,
        (f"[System] Cross-check shortfall note (cross_check_id={cross_check_id})",)
    )
    execute_write_pg(
        "UPDATE audit_rule_cross_checks SET shortfall_document_id = %s WHERE id = %s",
        (new_id, cross_check_id)
    )
    return new_id

def _has_pending_account_head_note(voucher_line_id: int, sibling_ids: Optional[List[int]] = None) -> bool:
    """
    True if this voucher line (or any sibling session's copy of it - a cashbook reopen creates
    a fresh verification_details row with no true session-resume, see
    _resolve_sibling_voucher_line_ids()) currently has a non-blank "no suitable account exists"
    Account Head note recorded in voucher_line_observations under the reserved sentinel
    document_id = ACCOUNT_HEAD_OBSERVATION_DOC_ID (both defined later in this file - resolved
    at call time, same forward-reference this function's own caller already relies on for
    _resolve_sibling_voucher_line_ids()).

    The "Recommended Account Head" field itself (static/verification.js's accountHeadCache) has
    no backend persistence at all - this sentinel observation note is the ONLY real signal that
    exists server-side for "an auditor flagged this line as needing a new account head with no
    Trial Balance match" (Account Head Recommendation reposition + search-select + no-match
    note fix). Mirrors get_line_observations()'s sibling-resolution + updated_at dedup, narrowed
    to just this one document_id.

    Returns False (not blocking) for an unresolvable voucher line - by the time
    compute_and_persist_cleared_status() would call this, it has already confirmed the line
    exists via its own lookup, so this case is defensive, not a real path.

    sibling_ids: pass the already-resolved list to skip the redundant re-resolve
    (compute_and_persist_cleared_status() already has it) - resolved here only if omitted.
    """
    if sibling_ids is None:
        sibling_ids = _resolve_sibling_voucher_line_ids(voucher_line_id)
    if not sibling_ids:
        return False
    placeholders = ",".join("%s" for _ in sibling_ids)
    rows = execute_query_pg(
        f"""
        SELECT observation_text, updated_at FROM voucher_line_observations
        WHERE voucher_line_id IN ({placeholders}) AND document_id = %s
        """,
        tuple(sibling_ids) + (ACCOUNT_HEAD_OBSERVATION_DOC_ID,)
    )
    if not rows:
        return False
    latest = max(rows, key=lambda r: _ts_sort_key(r['updated_at']))
    return bool((latest['observation_text'] or '').strip())


def _format_shortfall_observation_text(rule_result: dict) -> str:
    """
    One specific, auditable sentence per shortfall pair in a single Cross-check's result (a
    chain can produce more than one shortfall step), joined with '; ' - e.g. "Quantity Match:
    Store Receiving Quantity (95) is less than Purchase Order Quantity (100) - shortfall of 5".
    The Cross-check's own name is embedded directly in the text (not looked up again later), so
    sync_line_observations_to_voucher()'s rollup needs no new JOIN to label this correctly.
    """
    field = rule_result['compare_field']
    name = rule_result['name']
    parts = []
    for s in rule_result['shortfalls']:
        parts.append(
            f"{name}: {s['to_document']} {field} ({s['to_value']:g}) is less than "
            f"{s['from_document']} {field} ({s['from_value']:g}) — shortfall of {s['difference']:g}"
        )
    return "; ".join(parts)


def _write_shortfall_observations(voucher_line_id: int, cross_check_result: dict) -> bool:
    """
    Auto-log a minor observation for every currently-shortfall Cross-check result (Automated
    cross-document validation engine, Phase 8 of 10), reusing voucher_line_observations exactly
    as-is - same upsert shape save_line_observation() already uses for a real Document, one row
    per (voucher_line_id, sentinel document_id) via
    _get_or_create_cross_check_shortfall_document_id().

    Self-correcting, not append-only: every rule in cross_check_result['results'] (i.e. every
    active Cross-check on the Category Phase 7 already resolved) has its sentinel row written
    on EVERY call - shortfall text when currently "shortfall", or '' otherwise (resolved back to
    passing, or now a hard fail instead - either way, no longer accurately described as a
    shortfall). A blank entry contributes no line to the rollup (sync_line_observations_to_
    voucher()'s existing "blank text = no block" rule, confirmed 2026-07-25), so a resolved
    shortfall disappears from the voucher's Observations field on the very next recompute with
    no separate "resolved/superseded" flag needed.

    Deliberately out of scope, flagged not silently handled: a Cross-check later deactivated or
    removed from its Category stops appearing in cross_check_result entirely, so its old
    shortfall row is NOT proactively cleared by this function - it's left as a historical entry,
    matching this file's established append-only-by-default philosophy for observations
    elsewhere.

    Returns True iff at least one Cross-check was evaluated (i.e. results was non-empty) - the
    caller uses this to skip an unnecessary re-sync call when there was nothing to write (a
    Category with zero active Cross-checks). Never raises - a failure here must not block
    Cleared/Query computation, matching every other helper's contract in this file.
    """
    try:
        results = cross_check_result.get('results') or []
        for rule_result in results:
            sentinel_id = _get_or_create_cross_check_shortfall_document_id(rule_result['cross_check_id'])
            text = _format_shortfall_observation_text(rule_result) if rule_result['status'] == 'shortfall' else ''
            execute_write_pg(
                """
                INSERT INTO voucher_line_observations (voucher_line_id, document_id, observation_text, synced, updated_by, updated_at)
                VALUES (%s, %s, %s, FALSE, 'System', CURRENT_TIMESTAMP)
                ON CONFLICT(voucher_line_id, document_id)
                DO UPDATE SET observation_text = excluded.observation_text, synced = FALSE, updated_by = excluded.updated_by, updated_at = CURRENT_TIMESTAMP
                """,
                (voucher_line_id, sentinel_id, text)
            )
        return len(results) > 0
    except Exception as exc:
        print(f"⚠️ _write_shortfall_observations failed for line {voucher_line_id}: {exc}")
        return False


def _clear_status_gate(key: str, label: str, ok: bool, detail: str = "", not_applicable: bool = False) -> dict:
    """
    One row of compute_and_persist_cleared_status()'s optional gate breakdown ("Why is this line
    Query?"). Labels are plain English for the auditor, never internal variable names.
    """
    return {
        "key": key,
        "label": label,
        "status": "not_applicable" if not_applicable else ("pass" if ok else "fail"),
        "detail": detail,
    }


def compute_and_persist_cleared_status(voucher_line_id: int, detail: bool = False,
                                       persist: bool = True,
                                       sibling_ids: Optional[List[int]] = None,
                                       rulebook: Optional[dict] = None) -> Optional[dict]:
    """
    Validation Rules Phase 4: compute this line's Cleared/Query status. Automated
    cross-document validation engine Phase 7 added a 5th (cross-document evaluation) and 6th
    (Account Head note) condition - see those sections below. Persists to
    verification_details.cleared_status/matrix_version_applied. Called from save_line_items() -
    the same chokepoint every Save/Next/Prev/Validate/Complete/Exit path already runs per line
    (mirrors sync_line_observations_to_voucher()).

    Returns the computed {"cleared_status", "matrix_version_applied"} dict, or None if the
    line/its account code/its Category can't be resolved - cleared_status is left exactly
    as it was (NULL for a never-computed line) rather than written to, since "not yet
    classified" is a normal state (a line with no resolved Category can't even open
    Supporting Documents today, per the existing Verification workflow convention).

    Two optional params, both defaulting to today's exact behaviour so the existing
    save_line_items() call site is untouched ("Why is this line Query?" fix):

      detail=True  - additionally returns a "gates" list (one entry per gate, via
                     _clear_status_gate()) explaining WHICH gate failed and why, and turns the
                     three early "can't classify this line" returns from a bare None into a
                     {"cleared_status": None, "reason": "..."} dict so a caller can tell those
                     apart from a genuinely missing line. Affects nothing else - the gate
                     booleans, all_ok, and the persisted value are computed identically either
                     way.
      persist=False - skips the final UPDATE and the shortfall-observation write/re-sync, so a
                     read-only caller (GET /api/verification/line_status_reason) can ask "why?"
                     without a GET request writing to the database.

      sibling_ids   - optional pre-resolved _resolve_sibling_voucher_line_ids() result. When
                     omitted (every caller today), resolved here exactly as before. Passing it
                     lets _save_line_items_impl() share one resolve across this function and
                     the sync_line_observations_to_voucher() call it makes right before this
                     one, instead of each re-deriving it independently.
    """
    gates: List[dict] = []
    line = execute_query_pg(
        """
        SELECT vd.id, vd.session_id, vd.transaction_ref, cd.row_data, cd.extract_amount, vs.client_id,
               vd.category_id_applied, vd.category_name_applied
        FROM verification_details vd
        JOIN client_data cd ON cd.id = vd.client_data_id
        JOIN verification_sessions vs ON vs.id = vd.session_id
        WHERE vd.id = %s
        """,
        (voucher_line_id,)
    )
    if not line:
        # detail mode returns None here too - "no such voucher line" is the one case a caller
        # must be able to tell apart from "line exists but can't be classified" (the two
        # reason-carrying returns below), since only this one is a 404.
        return None
    row = line[0]
    client_id = row['client_id']
    session_rulebook = rulebook or load_verification_rulebook_for_session(row['session_id'])
    transaction_ref = row['transaction_ref']
    row_data = row['row_data']
    if isinstance(row_data, str):
        try:
            row_data = json.loads(row_data)
        except Exception:
            row_data = {}
    row_data = row_data or {}
    # Physical Bill evidence type, Phase 6 of 8: same coercion helper
    # evaluate_cross_checks_for_line()'s own compare_to_voucher_line block already uses against
    # this exact column, so a physical bill's Amount is compared to the voucher line the same way.
    voucher_amount = _cross_check_numeric(row['extract_amount'])

    account_code = get_account_code_value(row_data)
    if not account_code:
        if detail:
            return {"cleared_status": None, "matrix_version_applied": None, "gates": [],
                    "reason": "This line has no account code, so no Category could be resolved - "
                              "Cleared/Query is not computed for it."}
        return None

    # Freeze Category on voucher lines: once a line has been successfully classified/persisted
    # once, category_id_applied is set and permanently governs it from then on - a later TB
    # re-upload/deletion/reassignment must not retroactively reinterpret already-audited work.
    # Only a not-yet-frozen line resolves live (TB-first, assignments-fallback, unchanged below).
    if row['category_id_applied'] is not None:
        # matrix_version is still re-checked live for the frozen category - a separate, already-
        # solved staleness problem via matrix_version_applied, orthogonal to category identity.
        # A since-deactivated category still resolves here, same "still a real, displayable
        # match" convention as get_category_for_account()'s own docstring.
        category_id = row['category_id_applied']
        category_name = row['category_name_applied']
        mv_rows = execute_query_pg(
            "SELECT matrix_version FROM audit_rule_categories WHERE id = %s", (category_id,)
        )
        matrix_version = mv_rows[0]['matrix_version'] if mv_rows else None
    else:
        # Category resolution mirrors get_category_for_account() (2026-07-30 fix): prefer the
        # client's latest parsed Trial Balance's own category_id for this account_code - that's
        # what Assign Categories itself shows/edits, and account_category_assignments (below) is
        # a persisted mirror that can drift stale relative to it. The live TB wins whenever the
        # account is present in it; the assignments table is used only as a fallback for an
        # account not in the latest TB. Applied here too so the status engine can't disagree
        # with what the Verification UI's Type column is already showing for the same line.
        cat_rows = None
        upload_rows = execute_query_pg(
            """
            SELECT id FROM trial_balance_uploads
            WHERE client_id = %s AND status = 'parsed'
            ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC
            LIMIT 1
            """,
            (client_id,)
        )
        if upload_rows:
            tb_rows = execute_query_pg(
                """
                SELECT a.category_id, c.name AS category_name, c.matrix_version
                FROM trial_balance_accounts a
                LEFT JOIN audit_rule_categories c ON c.id = a.category_id AND c.is_active = TRUE
                WHERE a.upload_id = %s AND a.account_code = %s
                """,
                (upload_rows[0]['id'], account_code)
            )
            if tb_rows and tb_rows[0]['category_name']:
                cat_rows = tb_rows

        if cat_rows is None:
            assignment_rows = execute_query_pg(
                """
                SELECT aca.category_id, arc.name AS category_name, arc.matrix_version
                FROM account_category_assignments aca
                LEFT JOIN audit_rule_categories arc ON arc.id = aca.category_id AND arc.is_active = TRUE
                WHERE aca.client_id = %s AND aca.account_code = %s
                """,
                (client_id, account_code)
            )
            if assignment_rows and assignment_rows[0]['category_name']:
                cat_rows = assignment_rows

        if not cat_rows or not cat_rows[0]['category_id'] or not cat_rows[0]['category_name']:
            if detail:
                return {"cleared_status": None, "matrix_version_applied": None, "gates": [],
                        "reason": f"Account code {account_code} has no active Category assigned, so "
                                  "Cleared/Query is not computed for this line. Assign one under "
                                  "Audit Rules → Assign Categories."}
            return None

        category_id = cat_rows[0]['category_id']
        matrix_version = cat_rows[0]['matrix_version']
        category_name = cat_rows[0]['category_name']

    if sibling_ids is None:
        sibling_ids = _resolve_sibling_voucher_line_ids(voucher_line_id) or [voucher_line_id]
    placeholders = ",".join("%s" for _ in sibling_ids)

    # 1. Document requirement check: Category's required documents, each satisfied one of two
    # ways depending on whether it has a linked Data Bank template.
    # get_category_documents() is deliberately unfiltered on is_active (it also backs the
    # Category detail admin display, which shows an inactive attached document greyed rather
    # than hiding it) - filtered to active documents here, the actual gating use, so a
    # deactivated Document can never permanently block Cleared status for a line that never
    # got a chance to link evidence against it before it was deactivated (Inactive items
    # excluded from selection contexts, Phase 5 of 5: it has already been removed from every
    # selection surface - Audit Rules pickers, Verification's Supporting Documents tab list -
    # so requiring it here would be an unfulfillable requirement, not a real gate).
    category_profile = next((c for c in (session_rulebook or {}).get('categories', [])
                             if c.get('category_id') == category_id), None)
    required_docs = [
        {**doc, "document_id": doc.get("document_id", doc.get("id")), "is_active": True}
        for doc in (category_profile or {}).get("documents", [])
    ]
    required_doc_ids = {d['document_id'] for d in required_docs if d['is_active']}
    # Physical Bill evidence type, Phase 6 of 8: per-document type, keyed off the Document's own
    # document_type - NOT the Category's evidence_type. This intercepts a physical_bill Document
    # unconditionally, before it can fall into the no-template role-tick branch below (the trap
    # flagged since Phase 2/3: a physical_bill Document has no template_id, so without this it
    # would silently resolve "satisfied" via role ticks alone, with no bill data ever checked).
    required_doc_types = {d['document_id']: d['document_type'] for d in required_docs if d['is_active']}

    linked_rows = execute_query_pg(
        f"""
        SELECT DISTINCT document_id FROM voucher_line_documents
        WHERE voucher_line_id IN ({placeholders}) AND status = 'active'
        """,
        tuple(sibling_ids)
    )
    linked_doc_ids = {r['document_id'] for r in linked_rows}

    # Confirmed fix (2026-08-08): a required Document with NO linked template has no
    # evidence-linking step in the Supporting Documents modal at all (Supporting Documents modal
    # redesign, Phase 1-3's "no-template Documents" design) - it can never appear in
    # voucher_line_documents, so testing it against linked_doc_ids alone made it permanently
    # unsatisfiable regardless of how complete it actually was. Its real completion signal is the
    # Audit Procedures role checklist (voucher_line_procedure_checks), exactly as
    # static/verification.js's own isProcedureDocumentComplete() already defines for its
    # no-template branch (roles.length > 0 && every role ticked - zero configured roles is
    # deliberately unsatisfiable too, mirroring that same rule, not a new inconsistency).
    required_template_ids = {}
    if required_doc_ids:
        doc_ph = ",".join("%s" for _ in required_doc_ids)
        template_rows = execute_query_pg(
            f"SELECT id, template_id FROM audit_rule_documents WHERE id IN ({doc_ph})",
            tuple(required_doc_ids)
        )
        required_template_ids = {r['id']: r['template_id'] for r in template_rows}

    # Physical Bill evidence type, Phase 6 of 8: a physical_bill Document never goes through the
    # role-tick path (its own dedicated check below handles it), regardless of what its auth_roles
    # happen to be configured as.
    no_template_required_ids = {
        did for did in required_doc_ids
        if not required_template_ids.get(did) and required_doc_types.get(did) != 'physical_bill'
    }

    # role_ticks_by_doc: {document_id: {role_name: is_checked}} - only fetched when at least one
    # required document is template-less, and only for those documents' rows. Same "most
    # recently checked/unchecked sibling row wins" dedup get_procedure_checks() already uses, so
    # this can't disagree with what the Supporting Documents modal itself shows for the same line.
    role_ticks_by_doc: Dict[int, Dict[str, bool]] = {}
    if no_template_required_ids:
        nt_ph = ",".join("%s" for _ in no_template_required_ids)
        tick_rows = execute_query_pg(
            f"""
            SELECT document_id, role_name, is_checked, checked_at
            FROM voucher_line_procedure_checks
            WHERE voucher_line_id IN ({placeholders}) AND document_id IN ({nt_ph})
            """,
            tuple(sibling_ids) + tuple(no_template_required_ids)
        )
        latest_tick: Dict[tuple, dict] = {}
        for r in tick_rows:
            key = (r['document_id'], r['role_name'])
            existing = latest_tick.get(key)
            if existing is None or _ts_sort_key(r['checked_at']) > _ts_sort_key(existing['checked_at']):
                latest_tick[key] = r
        for (did, role_name), r in latest_tick.items():
            role_ticks_by_doc.setdefault(did, {})[role_name] = bool(r['is_checked'])

    no_template_roles_by_doc = get_document_auth_roles_batch(list(no_template_required_ids))

    def _no_template_doc_roles(document_id: int) -> List[str]:
        return no_template_roles_by_doc.get(document_id, [])

    def _no_template_doc_satisfied(document_id: int) -> bool:
        roles = _no_template_doc_roles(document_id)
        if not roles:
            return False
        ticks = role_ticks_by_doc.get(document_id, {})
        return all(ticks.get(role) for role in roles)

    # Physical Bill evidence type, Phase 6 of 8 (generalized in Multi-Bill/Multi-Line Physical
    # Bill, Phase 2 of 6): fetch every physical bill recorded for this line (sibling-session-
    # resolved, same as every other voucher_line_* read in this file) once, only when at least
    # one required document actually needs one - zero extra query otherwise. A document can now
    # have several bills, so bills_by_doc groups into a LIST per document_id rather than
    # assuming one row.
    physical_bill_required_ids = {did for did, dt in required_doc_types.items() if dt == 'physical_bill'}
    bills_by_doc: Dict[int, list] = {}
    if physical_bill_required_ids:
        for b in _get_voucher_line_physical_bills(voucher_line_id, sibling_ids=sibling_ids)['bills']:
            bills_by_doc.setdefault(b['document_id'], []).append(b)

    def _physical_bill_satisfied(document_id: int) -> bool:
        """
        Delegates to the shared _evaluate_physical_bill_completion() (defined further down this
        file, near _cross_check_values_equal) - the hard-lock, no-partial-credit rule: every
        bill's Bill No/Date/Vendor non-blank and at least one item, every item's amount
        resolvable, and the GRAND TOTAL across every item in every bill for this document
        exactly equal (float-safety epsilon only, no business rounding tolerance) to the
        voucher line's own extract_amount.
        """
        doc_bills = [
            {"bill_no": b['bill_no'], "bill_date": b['bill_date'], "vendor_name": b['vendor_name'],
             "items": [item['amount'] for item in b['items']]}
            for b in bills_by_doc.get(document_id, [])
        ]
        return _evaluate_physical_bill_completion(doc_bills, voucher_amount)

    satisfied_doc_ids = set()
    for did in required_doc_ids:
        if required_doc_types.get(did) == 'physical_bill':
            if _physical_bill_satisfied(did):
                satisfied_doc_ids.add(did)
        elif required_template_ids.get(did):
            if did in linked_doc_ids:
                satisfied_doc_ids.add(did)
        elif _no_template_doc_satisfied(did):
            satisfied_doc_ids.add(did)

    doc_requirement_met = required_doc_ids.issubset(satisfied_doc_ids)

    if detail:
        outstanding_parts = []
        for d in required_docs:
            did = d['document_id']
            if not d['is_active'] or did in satisfied_doc_ids:
                continue
            if required_doc_types.get(did) == 'physical_bill':
                doc_bills = bills_by_doc.get(did) or []
                if not doc_bills:
                    outstanding_parts.append(f"{d['name']} (no physical bill entered)")
                elif any(
                    not (b['bill_no'] or '').strip() or not (b['bill_date'] or '').strip()
                    or not (b['vendor_name'] or '').strip() or not b['items']
                    or any(_cross_check_numeric(item['amount']) is None for item in b['items'])
                    for b in doc_bills
                ):
                    outstanding_parts.append(f"{d['name']} (physical bill missing Bill No/Date/Vendor or has an incomplete item row)")
                else:
                    grand_total = sum(_cross_check_numeric(item['amount']) for b in doc_bills for item in b['items'])
                    outstanding_parts.append(
                        f"{d['name']} (physical bill grand total {grand_total} does not match "
                        f"voucher amount {row['extract_amount']})"
                    )
            elif required_template_ids.get(did):
                outstanding_parts.append(f"{d['name']} (not linked)")
            else:
                roles = _no_template_doc_roles(did)
                if not roles:
                    outstanding_parts.append(f"{d['name']} (no roles configured)")
                else:
                    ticks = role_ticks_by_doc.get(did, {})
                    done = sum(1 for role in roles if ticks.get(role))
                    outstanding_parts.append(f"{d['name']} ({done} of {len(roles)} required roles ticked)")
        gates.append(_clear_status_gate(
            "documents", "Required documents linked", doc_requirement_met,
            detail=("Outstanding: " + "; ".join(outstanding_parts)) if outstanding_parts
                   else f"All {len(required_doc_ids)} required document(s) are complete.",
            not_applicable=(not required_doc_ids)
        ))

    # 2. Voucher-level checks gate: independent of Category/documents, always required.
    # Generic Voucher-Level Checks (Phase 5 cutover) - reads voucher_check_status/
    # audit_rule_voucher_checks (keyed client_id+transaction_ref, not session-scoped, same
    # rationale as the old voucher_approvals table it replaces) instead of the retired
    # voucher_approvals-based Prepared/Checked/Approved-By fields. Passes only if every
    # currently-active check has been ticked - a check with no voucher_check_status row yet
    # counts as unticked (fails safe, same "no record = normal not-yet-done state, but that
    # state doesn't pass the gate" convention used elsewhere in this function). No transaction_ref
    # = fail.
    voucher_checks_ok = False
    active_checks = []
    ticked_check_ids = set()
    if transaction_ref:
        # `name` is selected purely for detail mode's plain-English gate text - the gate logic
        # itself still only ever compares ids, exactly as before.
        active_checks = (session_rulebook or {}).get("voucher_checks", [])
        active_check_ids = {c['id'] for c in active_checks}
        ticked_rows = execute_query_pg(
            "SELECT check_id FROM voucher_check_status WHERE client_id = %s AND transaction_ref = %s AND state = 'done'",
            (client_id, transaction_ref)
        )
        ticked_check_ids = {r['check_id'] for r in ticked_rows}
        # Vacuously true when zero checks are configured (nothing required = nothing to satisfy),
        # same as an empty required-documents set already behaves in check #1 above.
        voucher_checks_ok = active_check_ids.issubset(ticked_check_ids)

    if detail:
        if not transaction_ref:
            vc_detail = "This line has no voucher reference, so the voucher-level checks can't be read."
        else:
            untick = [c['name'] for c in active_checks if c['id'] not in ticked_check_ids]
            vc_detail = ("(voucher-wide) Not yet marked done: " + ", ".join(untick)) if untick \
                else "(voucher-wide) Every voucher-level check is marked done."
        gates.append(_clear_status_gate(
            "voucher_checks", "Voucher-level checks complete", voucher_checks_ok,
            detail=vc_detail,
            not_applicable=(bool(transaction_ref) and not active_checks)
        ))

    # 3. Signature-exception check: defaults OK, fails only on a confirmed flagged mismatch.
    sig_rows = execute_query_pg(
        f"""
        SELECT COUNT(*) as cnt FROM voucher_line_documents
        WHERE voucher_line_id IN ({placeholders}) AND status = 'active' AND signature_exception = TRUE
        """,
        tuple(sibling_ids)
    )
    signature_ok = (sig_rows[0]['cnt'] == 0)

    if detail:
        gates.append(_clear_status_gate(
            "signature", "No signature exceptions", signature_ok,
            detail="No linked evidence row is flagged with a signature exception."
                   if signature_ok
                   else f"{sig_rows[0]['cnt']} linked evidence row(s) are flagged with a signature exception."
        ))

    # 4. Cross-document validation engine (Phase 6): any HARD-FAIL rule result blocks Cleared.
    # category_id is passed through explicitly - already resolved above, via this exact same
    # account_category_assignments lookup - so the engine's own (different) Category-resolution
    # fallback never runs from this call site and the two can never disagree. Shortfalls (soft -
    # Phase 8's observation-logging concern) and not_evaluated (Phase 6's own "missing data is
    # inconclusive, not a fail" contract) do NOT gate here - only an actual "fail" does.
    # With zero active Cross-checks configured (true of every category today),
    # evaluate_cross_checks_for_line() returns an empty results list, so this is a no-op until
    # a Cross-check is actually configured and activated. Never raises, per Phase 6's contract.
    cross_check_result = evaluate_cross_checks_for_line(
        voucher_line_id, category_id=category_id, sibling_ids=sibling_ids,
        voucher_amount=voucher_amount, rulebook=session_rulebook
    )
    cross_checks_ok = not any(r['status'] == 'fail' for r in cross_check_result['results'])

    if detail:
        cc_results = cross_check_result['results']
        failed = [r for r in cc_results if r['status'] == 'fail']
        # Shortfalls and not_evaluated deliberately do NOT gate (Phase 6/8 contract) - reported
        # here as non-blocking notes so the auditor isn't left thinking they caused the Query.
        soft = [r for r in cc_results if r['status'] in ('shortfall', 'not_evaluated')]
        if failed:
            cc_detail = "Failed: " + "; ".join(f"{r['name']} — {r['message']}" for r in failed)
        elif cc_results:
            cc_detail = "All cross-document checks passed."
        else:
            cc_detail = "No cross-document checks are configured for this Category."
        if soft:
            cc_detail += " Not blocking: " + "; ".join(
                f"{r['name']} ({r['status'].replace('_', ' ')}) — {r['message']}" for r in soft
            )
        gates.append(_clear_status_gate(
            "cross_checks", "Cross-document checks passed", cross_checks_ok,
            detail=cc_detail, not_applicable=(not cc_results)
        ))

    # Phase 8 of 10: auto-log/self-clear a shortfall observation from the SAME cross_check_result
    # just computed above - never re-runs the engine. Purely a side effect (does not affect
    # cross_checks_ok/all_ok) - see _write_shortfall_observations()'s own docstring. Re-syncs
    # immediately (only if something was actually written) rather than waiting for save_line_
    # items()'s own earlier sync_line_observations_to_voucher() call, which already ran BEFORE
    # this function - without this, a freshly-appearing shortfall would be one save-cycle stale
    # in the Observations column, the same class of bug already fixed once for manual
    # observations (2026-07-25, "roll-up doesn't fire until an unrelated later action").
    # persist=False (the read-only "why is this Query?" endpoint) skips this entirely - a GET
    # must not write, and this block is the function's only write other than the UPDATE below.
    if persist and _write_shortfall_observations(voucher_line_id, cross_check_result):
        sync_line_observations_to_voucher(voucher_line_id, sibling_ids=sibling_ids)

    # 5. Recommended Account Head must be empty. Unconditional - applies to every voucher line
    # regardless of whether a Category resolved above (unlike checks 1/4), per the confirmed
    # Discuss-mode reconciliation. See _has_pending_account_head_note() for what this actually
    # reads - the "Recommended Account Head" UI field itself has no backend persistence; the
    # only real signal is the sentinel Account Head observation note.
    account_head_ok = not _has_pending_account_head_note(voucher_line_id, sibling_ids=sibling_ids)

    if detail:
        gates.append(_clear_status_gate(
            "account_head", "No pending Account Head note", account_head_ok,
            detail="No 'no suitable account exists' note is recorded for this line."
                   if account_head_ok
                   else "A 'no suitable account exists' Account Head note is recorded for this "
                        "line — clear it once a proper account head is assigned."
        ))

    # (Check #2 used to be a per-document approval-required gate reading Category-level
    # approval_required + a substring-match on ticked procedure-check role names - hard-removed
    # alongside the audit_rule_category_documents.approval_required column, confirmed
    # requirement, 2026-08-02. Approval requirement now lives solely at Document level via the
    # "Approved by" role's Required Authority field (audit_rule_document_auth_roles.
    # required_authority) - stripped entirely here since it was built on the substring-match
    # heuristic being retired; now replaced by check #6 below.)

    # 6. Document-level Approved-by authority gate (new, confirmed requirement): replaces the
    # retired Category-level approval_required substring-match gate. See
    # check_approved_by_authority_gate()'s own docstring - this is an existence check ("a
    # qualified signatory is on file"), not a match against a specific recorded signer, since
    # nothing in this app records who actually signed a particular voucher's document.
    approval_authority_ok = check_approved_by_authority_gate(client_id, category_id)

    if detail:
        # Re-walks the same data check_approved_by_authority_gate() walks, purely to name WHICH
        # document/role is missing a specimen. The pass/fail answer above is NOT recomputed from
        # this - it still comes from the gate function itself, so the two can't drift apart.
        auth_docs = get_required_documents_with_authority(category_id)
        missing_auth = []
        for doc in auth_docs:
            for role_name, required_authority in doc['required_authorities'].items():
                if not has_matching_active_specimen(client_id, doc['document_id'], role_name, required_authority):
                    missing_auth.append(f"{doc['name']} / {role_name} (needs '{required_authority}')")
        gates.append(_clear_status_gate(
            "approval_authority", "Approved-by authority on file", approval_authority_ok,
            detail=("No active signature specimen on file for: " + "; ".join(missing_auth))
                   if missing_auth
                   else ("A qualified signatory is on file for every required document."
                         if auth_docs
                         else "No required document has a Required Authority configured."),
            not_applicable=(not auth_docs)
        ))

    all_ok = (
        doc_requirement_met and voucher_checks_ok and signature_ok
        and cross_checks_ok and account_head_ok and approval_authority_ok
    )
    cleared_status = "Cleared" if all_ok else "Query"

    if persist:
        if row['category_id_applied'] is None:
            # First successful persist for this line - freeze the category now, alongside the
            # existing cleared_status/matrix_version_applied write. Every later save leaves
            # category_id_applied/category_name_applied untouched (see the else branch).
            execute_write_pg(
                "UPDATE verification_details SET cleared_status = %s, matrix_version_applied = %s, "
                "category_id_applied = %s, category_name_applied = %s WHERE id = %s",
                (cleared_status, matrix_version, category_id, category_name, voucher_line_id)
            )
        else:
            execute_write_pg(
                "UPDATE verification_details SET cleared_status = %s, matrix_version_applied = %s WHERE id = %s",
                (cleared_status, matrix_version, voucher_line_id)
            )

    result = {"cleared_status": cleared_status, "matrix_version_applied": matrix_version,
              "category_id_applied": category_id, "category_name_applied": category_name}
    if detail:
        # Failing gates first, then not_applicable, then passing - a stable sort, so gates stay
        # in their original evaluation order within each band.
        rank = {"fail": 0, "not_applicable": 1, "pass": 2}
        result["gates"] = sorted(gates, key=lambda g: rank.get(g['status'], 3))
        result["working_paper_gates"] = _build_working_paper_gates(result["gates"])
        result["reason"] = None
    return result

def extract_cashbook_name(row_data: dict, client_prefixes: dict) -> Optional[str]:
    # Define aliases for required columns
    voucher_aliases = ['Voucher ID', 'Voucher No', 'Voucher Number', 'Voucher', 'Invoice No', 'Invoice Number']
    account_aliases = ['Account Name', 'Account', 'Account Detail', 'Account Code', 'Ledger', 'Head']
    debit_aliases = ['Debit', 'Debit Amount', 'Dr', 'Dr Amount', 'Debit (Dr)', 'Dr. Amount']
    credit_aliases = ['Credit', 'Credit Amount', 'Cr', 'Cr Amount', 'Credit (Cr)', 'Cr. Amount']

    voucher_id = get_column_value(row_data, voucher_aliases)
    account_name = get_column_value(row_data, account_aliases)
    debit_str = get_column_value(row_data, debit_aliases)
    credit_str = get_column_value(row_data, credit_aliases)

    if not voucher_id or not account_name:
        return None

    try:
        debit = float(debit_str) if debit_str else 0.0
        credit = float(credit_str) if credit_str else 0.0
    except:
        return None

    category = match_voucher_category(voucher_id, client_prefixes)
    # Type-specific side match: the 5-way category already tells us Payment vs Receipt, so
    # (unlike the old 3-way Cash/Bank/Journal scheme) discovery no longer needs to already
    # know the cashbook name to determine which side is correct - a Payment-type voucher's
    # cashbook leg is always the credit side, a Receipt-type voucher's is always the debit
    # side. This is what fixes expense/vendor accounts (the opposite leg of the same voucher)
    # from being wrongly returned as cashbook candidates.
    if category in ('Cash Payment', 'Bank Payment') and credit > 0:
        return account_name
    if category in ('Cash Receipt', 'Bank Receipt') and debit > 0:
        return account_name
    return None

def is_cashbook_leg_row(row: dict, category: Optional[str]) -> bool:
    """
    True only if `row` is structurally the cashbook/bankbook leg of its OWN voucher category
    (Payment-type -> credit side, Receipt-type -> debit side), mirroring extract_cashbook_name()'s
    side rule. Regression found during final Bank Verification testing: `cashbook_row` lookups in
    start_verification_session()/preview_cashbook() previously matched on account_name alone,
    with no side check - a voucher whose OTHER leg happened to share a name with an unrelated
    cashbook (e.g. a real Bank Payment transfer voucher debiting "CASH IN HAND" while crediting a
    bank account) could wrongly be pulled into that unrelated cashbook's transaction list. This
    can only manifest once a client has both Cash and Bank prefixes configured (impossible before
    Bank Verification existed), which is why it was dormant until now.
    """
    if category in ('Cash Payment', 'Bank Payment'):
        return row['credit'] > 0
    if category in ('Cash Receipt', 'Bank Receipt'):
        return row['debit'] > 0
    return False

def get_row_data(client_data_id: int) -> dict:
    rows = execute_query_pg("SELECT row_data FROM client_data WHERE id = %s", (client_data_id,))
    if not rows:
        return {}
    rd = rows[0]['row_data']
    if isinstance(rd, str):
        try:
            return json.loads(rd)
        except:
            return {}
    return rd if isinstance(rd, dict) else {}


def looks_like_cashbook_row(raw_value: Any) -> bool:
    if isinstance(raw_value, dict):
        return True
    if not isinstance(raw_value, str):
        return False
    lowered = raw_value.lower()
    return any(token in lowered for token in ['voucher id', 'voucher no', 'account name', 'debit', 'credit'])


def parse_row_data(raw_value: Any) -> Optional[Dict[str, Any]]:
    if not looks_like_cashbook_row(raw_value):
        return None
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None

def get_template_columns(template_id: int) -> list:
    """Fetch a template's active columns ordered by sort_order (lowest first), for document_group_id computation."""
    return get_cached_template_columns(template_id)

def get_document_field_value(client_data_id: int, field_name: str) -> Optional[Any]:
    """
    Resolves a named field's value (e.g. "Quantity", "Rate") from a linked evidence row's
    row_data, matching field_name against that row's own template's real column list
    case-insensitively (via get_template_columns()) before reading row_data - mirrors
    static/verification.js's getRowFieldCaseInsensitive(), server-side, but additionally
    validates the field actually exists on the template rather than trusting whatever keys
    happen to be in row_data. Additive to, not a replacement for, extract_amount.

    Used by Phase 6's chain-aware evaluation engine (Cross-Document Validation Engine),
    called once per (evidence row, field name) pair during a chain walk - must never raise,
    since a missing client_data row, a row with no template_id, or a field name that doesn't
    match any of that template's columns are all normal "nothing to compare" outcomes there,
    not errors. Returns None in all such cases.
    """
    if not client_data_id or not field_name or not field_name.strip():
        return None

    rows = execute_query_pg(
        "SELECT row_data, template_id FROM client_data WHERE id = %s", (client_data_id,)
    )
    if not rows:
        return None

    template_id = rows[0]['template_id']
    if not template_id:
        return None

    row_data = rows[0]['row_data']
    if isinstance(row_data, str):
        try:
            row_data = json.loads(row_data)
        except Exception:
            return None
    if not isinstance(row_data, dict):
        return None

    columns = get_template_columns(template_id)
    target = field_name.strip().lower()
    matched_column_name = next(
        (c['column_name'] for c in columns if c['column_name'].strip().lower() == target),
        None
    )
    if matched_column_name is None:
        return None

    # row_data keys are written from template column names at import time, so they should
    # already match exactly - resolved case-insensitively anyway for parity with
    # getRowFieldCaseInsensitive()'s own tolerance, and as a defensive fallback.
    row_key = next(
        (k for k in row_data.keys() if k.strip().lower() == matched_column_name.strip().lower()),
        None
    )
    if row_key is None:
        return None

    value = row_data[row_key]
    return value if value not in (None, '') else None


def flatten_procedure_data(proc_data: dict) -> dict:
    """Extract standard procedure fields from JSON."""
    if not proc_data:
        return {}
    return {
        "purchase_demand": proc_data.get("purchase_demand", False),
        "purchase_order": proc_data.get("purchase_order", False),
        "gate_pass": proc_data.get("gate_pass", False),
        "store_receiving": proc_data.get("store_receiving", False),
        "vendor_bill": proc_data.get("vendor_bill", False),
        "observation": proc_data.get("observation", ""),
        "comments": proc_data.get("comments", ""),
    }

# ==============================================
# EXISTING ENDPOINT (stats)
# ==============================================

@router.get("/api/verification/stats")
def get_verification_stats():
    try:
        total = execute_query_pg("SELECT COUNT(*) as count FROM verifications")[0]['count']
        pending = execute_query_pg("SELECT COUNT(*) as count FROM verifications WHERE status = 'Pending'")[0]['count']
        approved = execute_query_pg("SELECT COUNT(*) as count FROM verifications WHERE status = 'Approved'")[0]['count']
    except:
        total = pending = approved = 0
    return {"total": total, "pending": pending, "approved": approved, "rejected": 0}

# ==============================================
# PHASE 2 ENDPOINTS
# ==============================================

@router.get("/api/verification/cashbooks")
def get_cashbooks(
    client_id: int = Query(..., description="Client ID"),
    date_start: str = Query(..., description="Start date (YYYY-MM-DD)"),
    date_end: str = Query(..., description="End date (YYYY-MM-DD)"),
    debug: bool = Query(False, description="If true, return detailed debug info"),
    # Bank Verification (Phase 8): restricts candidate accounts to just the Cash or Bank
    # categories, so a client with both Cash and Bank prefixes configured doesn't get them
    # mixed into one undifferentiated list. Omitted keeps today's unfiltered behavior.
    category_group: Optional[str] = Query(None, pattern="^(cash|bank)$", description="'cash' or 'bank' - restricts discovery to that category group")
):
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")
    try:
        datetime.fromisoformat(date_start)
        datetime.fromisoformat(date_end)
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")
    if date_start > date_end:
        raise HTTPException(400, "Start date must be before or equal to end date.")

    client_prefixes = get_client_voucher_prefixes_pg(client_id)
    if category_group == 'cash':
        client_prefixes = {k: v for k, v in client_prefixes.items() if k in ('Cash Payment', 'Cash Receipt')}
    elif category_group == 'bank':
        client_prefixes = {k: v for k, v in client_prefixes.items() if k in ('Bank Payment', 'Bank Receipt')}

    rows = execute_query_pg(
        """
        SELECT id, row_data
        FROM client_data
        WHERE client_id = %s AND is_deleted = FALSE AND change_detection_status != 'deleted'
          AND extract_date BETWEEN %s AND %s
        ORDER BY extract_date
        """,
        (client_id, date_start, date_end)
    )

    cashbooks = set()
    debug_info = []
    for row in rows:
        try:
            row_data = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
            if not isinstance(row_data, dict):
                continue
            voucher_id = row_data.get('Voucher ID')
            cb_name = extract_cashbook_name(row_data, client_prefixes)
            if debug:
                debug_info.append({
                    "row_id": row['id'],
                    "voucher_id": voucher_id,
                    "category": match_voucher_category(voucher_id, client_prefixes),
                    "account_name": row_data.get('Account Name'),
                    "debit": row_data.get('Debit'),
                    "credit": row_data.get('Credit'),
                    "extracted_cashbook": cb_name
                })
            if cb_name:
                cashbooks.add(cb_name)
        except Exception as e:
            if debug:
                debug_info.append({"row_id": row['id'], "error": str(e)})
            continue

    if debug:
        return {
            "cashbooks": sorted(list(cashbooks)),
            "count": len(cashbooks),
            "debug": {
                "total_rows": len(rows),
                "patterns_used": client_prefixes,
                "rows": debug_info
            }
        }
    return {"cashbooks": sorted(list(cashbooks)), "count": len(cashbooks)}

@router.get("/api/verification/latest_cashbook_date")
def get_latest_cashbook_date(
    client_id: int = Query(..., description="Client ID"),
    category_group: Optional[str] = Query(None, pattern="^(cash|bank)$", description="'cash' or 'bank' - restricts discovery to that category group")
):
    """
    Returns the extract_date of the most recent client_data row that actually
    resolves to a cashbook/bankbook leg (via extract_cashbook_name(), the same
    function get_cashbooks() uses) - lets the Start Verification popup default
    its search range to the real latest month with matching data, instead of
    "today's calendar month" (which fails whenever the client's latest upload
    isn't in the current month). Deliberately not a plain MAX(extract_date)
    over all client_data - that would default to a month that has data for an
    unrelated template (e.g. Purchase Order) but no actual cashbook rows.
    """
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    client_prefixes = get_client_voucher_prefixes_pg(client_id)
    if category_group == 'cash':
        client_prefixes = {k: v for k, v in client_prefixes.items() if k in ('Cash Payment', 'Cash Receipt')}
    elif category_group == 'bank':
        client_prefixes = {k: v for k, v in client_prefixes.items() if k in ('Bank Payment', 'Bank Receipt')}

    # Fetch the small date list first, then retrieve JSON only for one date at a time.
    # The previous query transferred every client_data.row_data value to Python before
    # discovering the newest matching cashbook row.
    date_rows = execute_query_pg(
        """
        SELECT DISTINCT extract_date
        FROM client_data
        WHERE client_id = %s AND is_deleted = FALSE AND change_detection_status != 'deleted'
        ORDER BY extract_date DESC
        """,
        (client_id,)
    )

    for date_row in date_rows:
        candidate_date = date_row['extract_date']
        rows = execute_query_pg(
            """
            SELECT row_data
            FROM client_data
            WHERE client_id = %s AND is_deleted = FALSE AND change_detection_status != 'deleted'
              AND extract_date = %s
            """,
            (client_id, candidate_date)
        )
        for row in rows:
            try:
                row_data = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
                if not isinstance(row_data, dict):
                    continue
                if extract_cashbook_name(row_data, client_prefixes):
                    return {"latest_date": candidate_date}
            except Exception:
                continue

    return {"latest_date": None}

@router.post("/api/verification/presence/enter")
def enter_verification_presence(session_id: int = Body(..., embed=True), user_session=Depends(get_current_user)):
    """Acquire or refresh this authenticated user's active Verification presence."""
    # Validate and acquire/refresh in one database round trip. The INSERT ... SELECT keeps the
    # previous 404 behavior: no row is written when the session is missing or cancelled.
    entered = execute_write_pg(
        """INSERT INTO verification_presence
           (verification_session_id, session_token, user_id, last_heartbeat, ended_at)
           SELECT vs.id, %s, %s, CURRENT_TIMESTAMP, NULL
           FROM verification_sessions vs
           WHERE vs.id = %s AND vs.status != 'Cancelled'
           ON CONFLICT (verification_session_id, session_token)
           DO UPDATE SET last_heartbeat = CURRENT_TIMESTAMP, ended_at = NULL
           RETURNING verification_session_id""",
        (user_session["token"], user_session["user_id"], session_id)
    )
    if not entered:
        raise HTTPException(404, "Verification session not found or cancelled.")
    return {"active": True, "session_id": session_id}


@router.post("/api/verification/presence/heartbeat")
def heartbeat_verification_presence(session_id: int = Body(..., embed=True), user_session=Depends(get_current_user)):
    updated = execute_write_pg(
        "UPDATE verification_presence SET last_heartbeat = CURRENT_TIMESTAMP, ended_at = NULL "
        "WHERE verification_session_id = %s AND session_token = %s",
        (session_id, user_session["token"])
    )
    return {"active": bool(updated), "session_id": session_id}


@router.post("/api/verification/presence/leave")
def leave_verification_presence(session_id: int = Body(..., embed=True), user_session=Depends(get_current_user)):
    execute_write_pg(
        "UPDATE verification_presence SET ended_at = CURRENT_TIMESTAMP "
        "WHERE verification_session_id = %s AND session_token = %s",
        (session_id, user_session["token"])
    )
    return {"active": False, "session_id": session_id}


@router.post("/api/verification/start_session")
def start_verification_session(
    client_id: int = Query(..., description="Client ID"),
    cashbook_name: str = Query(..., description="Cashbook name (as extracted)"),
    period_start: str = Query(..., description="Start date (YYYY-MM-DD)"),
    period_end: str = Query(..., description="End date (YYYY-MM-DD)"),
    created_by: str = "Analyst",
    # Distinguishes a Cash Verification session from a Bank Verification session (Phases
    # 7-8 add the dashboard entry points/UI that will pass 'bank' - no caller does yet).
    session_type: str = Query('cash', pattern="^(cash|bank)$", description="'cash' or 'bank'")
):
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")
    try:
        datetime.fromisoformat(period_start)
        datetime.fromisoformat(period_end)
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")
    if period_start > period_end:
        raise HTTPException(400, "Start date must be before or equal to end date.")
    
    client_prefixes = get_client_voucher_prefixes_pg(client_id)
    rows = execute_query_pg(
        """
        SELECT id, row_data, extract_date, extract_amount
        FROM client_data
        WHERE client_id = %s AND is_deleted = FALSE AND change_detection_status != 'deleted'
          AND extract_date BETWEEN %s AND %s
        ORDER BY extract_date
        """,
        (client_id, period_start, period_end)
    )

    # Aliases for flexible column matching
    voucher_aliases = ['Voucher ID', 'Voucher No', 'Voucher Number', 'Voucher', 'Invoice No', 'Invoice Number']
    account_aliases = ['Account Name', 'Account', 'Account Detail', 'Account Code', 'Ledger', 'Head']
    debit_aliases = ['Debit', 'Debit Amount', 'Dr', 'Dr Amount', 'Debit (Dr)', 'Dr. Amount']
    credit_aliases = ['Credit', 'Credit Amount', 'Cr', 'Cr Amount', 'Credit (Cr)', 'Cr. Amount']

    def get_col_value(row_data, aliases):
        for alias in aliases:
            if alias in row_data:
                val = row_data[alias]
                if val is not None and val != '':
                    return val
        return None

    vouchers = {}
    for row in rows:
        try:
            rd = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
            if not isinstance(rd, dict):
                continue
            voucher_id = get_col_value(rd, voucher_aliases)
            if not voucher_id:
                continue
            category = match_voucher_category(voucher_id, client_prefixes)
            if category not in ('Cash Payment', 'Cash Receipt', 'Bank Payment', 'Bank Receipt'):
                continue
            debit_str = get_col_value(rd, debit_aliases)
            credit_str = get_col_value(rd, credit_aliases)
            debit = float(debit_str) if debit_str else 0.0
            credit = float(credit_str) if credit_str else 0.0
            account_name = get_col_value(rd, account_aliases)
            if not account_name:
                continue
            if voucher_id not in vouchers:
                vouchers[voucher_id] = {
                    'category': category,
                    'rows': []
                }
            vouchers[voucher_id]['rows'].append({
                'row_id': row['id'],
                'row_data': rd,
                'debit': debit,
                'credit': credit,
                'account_name': account_name,
                'extract_date': row['extract_date'],
                'extract_amount': row['extract_amount']
            })
        except:
            continue

    transactions_to_verify = []
    for vid, data in vouchers.items():
        # The 5-way category already implies Payment vs Receipt, so vtype comes straight from
        # it - no need to re-derive it from the cashbook row's debit/credit sign anymore.
        # cashbook_row is still looked up as a sanity check that the named cashbook actually
        # appears among this voucher's legs - must also be the structurally correct side for
        # this voucher's OWN category (see is_cashbook_leg_row()), not just a name match, or a
        # voucher whose other leg happens to share a name with an unrelated cashbook can leak in.
        cashbook_row = next((r for r in data['rows'] if r['account_name'] == cashbook_name and is_cashbook_leg_row(r, data['category'])), None)
        if cashbook_row is None:
            continue
        vtype = voucher_type_label(data['category'])
        if vtype is None:
            continue
        for row in data['rows']:
            if vtype in ('CPV', 'BPV') and row['debit'] > 0 and row['credit'] == 0:
                transactions_to_verify.append({
                    'client_data_id': row['row_id'],
                    'row_data': row['row_data'],
                    'voucher_id': vid,
                    'extract_date': row['extract_date'],
                    'extract_amount': row['extract_amount']
                })
            elif vtype in ('CRV', 'BRV') and row['credit'] > 0 and row['debit'] == 0:
                transactions_to_verify.append({
                    'client_data_id': row['row_id'],
                    'row_data': row['row_data'],
                    'voucher_id': vid,
                    'extract_date': row['extract_date'],
                    'extract_amount': row['extract_amount']
                })
    
    if not transactions_to_verify:
        raise HTTPException(400, f"No transactions found for cashbook '{cashbook_name}' in the given period.")
    
        # Insert session header
    rulebook_version = get_audit_rules_config_version()
    session_id = execute_write_pg(
        """
        INSERT INTO verification_sessions (client_id, cashbook_name, period_start, period_end, created_by, status, total_transactions, session_type, rulebook_version)
        VALUES (%s, %s, %s, %s, %s, 'In Progress', %s, %s, %s)
        RETURNING id
        """,
        (client_id, cashbook_name, period_start, period_end, created_by, len(transactions_to_verify), session_type, rulebook_version)
    )

    # Prepare parameters for all details in one batch
    detail_params = []
    for trans in transactions_to_verify:
        detail_params.append((session_id, trans['client_data_id'], trans['voucher_id']))

    # Insert all details in a single round trip via execute_values(), not one round trip per row
    conn = get_raw_conn_pg()
    try:
        execute_batch_insert_with_ids_pg(
            conn,
            """
            INSERT INTO verification_details (session_id, client_data_id, transaction_ref, status, validation_status)
            VALUES %s RETURNING id
            """,
            detail_params,
            template="(%s, %s, %s, 'Pending', 'Pending')"
        )
    finally:
        release_raw_conn_pg(conn)

    # Bug fix ("Cleared" reverting to "Pending" on module switch/restart): start_verification_session()
    # has no "resume an existing session" logic (see _resolve_sibling_voucher_line_ids()'s own
    # docstring) - every re-open of the same cashbook/period inserts a brand-new verification_details
    # row per line, which starts with cleared_status = NULL ("Pending") and previously stayed that
    # way until the NEXT explicit save touched that specific new row - looking exactly like a real
    # Cleared status had been lost (and, since nothing ever re-saved it, surviving a restart too).
    # Seeded here via a single UPDATE that copies cleared_status/matrix_version_applied straight from
    # each line's most recent prior sibling session that had a real (non-NULL) value - not a live
    # recompute (confirmed with the user: a real recompute via compute_and_persist_cleared_status()
    # costs ~150ms/line, which would add tens of seconds to Start Verification for a large cashbook).
    # A copy can go stale if the Category's requirements changed since that prior computation, but
    # this introduces no new class of staleness - compute_and_persist_cleared_status() already never
    # proactively reclassifies an already-Cleared line when its matrix_version changes; this just
    # extends that same existing tolerance across a session restart. The next real save on this line
    # still recomputes it fresh, exactly as before.
    execute_write_pg(
        """
        UPDATE verification_details
        SET cleared_status = (
                SELECT vd2.cleared_status FROM verification_details vd2
                WHERE vd2.client_data_id = verification_details.client_data_id
                  AND vd2.session_id != %s AND vd2.cleared_status IS NOT NULL
                ORDER BY vd2.id DESC LIMIT 1
            ),
            matrix_version_applied = (
                SELECT vd2.matrix_version_applied FROM verification_details vd2
                WHERE vd2.client_data_id = verification_details.client_data_id
                  AND vd2.session_id != %s AND vd2.cleared_status IS NOT NULL
                ORDER BY vd2.id DESC LIMIT 1
            )
        WHERE session_id = %s
          AND EXISTS (
                SELECT 1 FROM verification_details vd2
                WHERE vd2.client_data_id = verification_details.client_data_id
                  AND vd2.session_id != %s AND vd2.cleared_status IS NOT NULL
            )
        """,
        (session_id, session_id, session_id, session_id)
    )

    log_audit(created_by, "Start Verification Session", "Verification", f"Session {session_id}: {cashbook_name}, {len(transactions_to_verify)} transactions")
    
    return {
        "session_id": session_id,
        "cashbook_name": cashbook_name,
        "total_transactions": len(transactions_to_verify),
        "message": f"Verification session started with {len(transactions_to_verify)} transactions."
    }

# ==============================================
# PHASE 3 ENDPOINTS
# ==============================================

def _build_verification_line(d: dict) -> dict:
    """Build the per-line payload (unchanged shape from the pre-Phase-1 single-row response)."""
    row_data = get_row_data(d['client_data_id'])
    line_items = []
    if d['line_items']:
        try:
            line_items = json.loads(d['line_items']) if isinstance(d['line_items'], str) else d['line_items']
        except:
            line_items = []
    procedure_data = {}
    if d['procedure_data']:
        try:
            procedure_data = json.loads(d['procedure_data']) if isinstance(d['procedure_data'], str) else d['procedure_data']
        except:
            procedure_data = {}

    transaction_type = procedure_data.get('transaction_type', '')

    # --- Phase 3: Fetch change_detection info for the transaction ---
    change_info = execute_query_pg(
        """
        SELECT cd.change_detection_status, cd.version,
               dal.old_row_data
        FROM client_data cd
        LEFT JOIN data_audit_log dal ON dal.client_data_id = cd.id
            AND dal.action = 'UPDATE'
            AND dal.id = (
                SELECT MAX(id) FROM data_audit_log
                WHERE client_data_id = cd.id AND action = 'UPDATE'
            )
        WHERE cd.id = %s
        """,
        (d['client_data_id'],)
    )
    change_detection_status = None
    old_row_data = None
    current_version = 1
    if change_info:
        ci = change_info[0]
        current_version = ci.get('version') or 1
        change_detection_status = ci.get('change_detection_status')
        old_raw = ci.get('old_row_data')
        if old_raw:
            try:
                old_row_data = json.loads(old_raw) if isinstance(old_raw, str) else old_raw
            except:
                pass

    return {
        "detail_id": d['id'],
        "client_data_id": d['client_data_id'],
        "voucher_id": d['transaction_ref'],
        "row_data": row_data,
        "line_items": line_items,
        "procedure_data": procedure_data,
        "validation_status": d['validation_status'],
        "validation_message": d['validation_message'],
        "transaction_type": transaction_type,
        # Phase 3: change detection data for the verification card
        "change_detection_status": change_detection_status,
        "old_row_data": old_row_data,
        "version": current_version,
        # Confirmed fix, 2026-07-25: the rolled-up Observation text for this line, previously
        # never returned to the frontend at all (the OBSERVATIONS column in the voucher table
        # was a placeholder with no data source) - see renderVoucherLines() in verification.js.
        "observations": d.get('observations'),
        # Validation Rules Phase 5: Cleared/Query status computed and persisted by
        # compute_and_persist_cleared_status() (Phase 4) - null until a line has been saved at
        # least once with a resolvable Category, same "not yet computed" convention as elsewhere.
        "cleared_status": d.get('cleared_status'),
        "matrix_version_applied": d.get('matrix_version_applied'),
        "category_id_applied": d.get('category_id_applied'),
        "category_name_applied": d.get('category_name_applied')
    }

def _fetch_session_details_grouped(session_id: int) -> list:
    """
    Fetch all verification_details for a session (joined with client_data),
    each annotated with its computed '_doc_group_id' (Phase A: shared by
    get_next_transaction and get_voucher_nav so both group vouchers the same
    way). Ordered by client_data_id so line/voucher order matches the
    physical row order. Does not filter by status.
    """
    details = execute_query_pg(
        """
        SELECT vd.id, vd.client_data_id, vd.transaction_ref, vd.line_items, vd.procedure_data,
               vd.validation_status, vd.validation_message, vd.status, vd.observations,
               vd.cleared_status, vd.matrix_version_applied,
               vd.category_id_applied, vd.category_name_applied,
               cd.template_id, cd.row_data, cd.extract_date
        FROM verification_details vd
        JOIN client_data cd ON vd.client_data_id = cd.id
        WHERE vd.session_id = %s AND vd.range_status = 'active'
        ORDER BY cd.id ASC
        """,
        (session_id,)
    )

    # Compute document_group_id for every detail row, caching template columns per template_id
    template_cols_cache: Dict[int, list] = {}

    def doc_group_id_for(d: dict) -> str:
        tpl_id = d['template_id']
        if tpl_id not in template_cols_cache:
            template_cols_cache[tpl_id] = get_template_columns(tpl_id)
        rd = json.loads(d['row_data']) if isinstance(d['row_data'], str) else d['row_data']
        return get_document_group_id(template_cols_cache[tpl_id], rd, tpl_id)

    for d in details:
        d['_doc_group_id'] = doc_group_id_for(d)

    return details


@router.get("/api/verification/next/{session_id}")
def get_next_transaction(session_id: int):
    """
    Phase 1 (voucher/line grouping): returns the next voucher — header + ordered
    array of its lines — instead of a single client_data row. Lines are grouped
    using the same document-key logic as import's compare_transactions
    (get_document_group_id), computed at query time (not persisted).
    """
    session = execute_query_pg("SELECT id FROM verification_sessions WHERE id = %s AND status != 'Cancelled'", (session_id,))
    if not session:
        raise HTTPException(404, "Verification session not found or cancelled.")

    details = _fetch_session_details_grouped(session_id)

    pending = [d for d in details if d['status'] == 'Pending']
    if not pending:
        return {"message": "All transactions in this session have been verified.", "completed": True}

    # Anchor = earliest pending detail row (by verification_details.id, same tie-break as before)
    anchor = min(pending, key=lambda d: d['id'])
    anchor_group_id = anchor['_doc_group_id']
    voucher_rows = [d for d in details if d['_doc_group_id'] == anchor_group_id]

    lines = [_build_verification_line(d) for d in voucher_rows]

    return {
        "voucher_id": anchor['transaction_ref'],
        "voucher_date": anchor['extract_date'],
        "document_group_id": anchor_group_id,
        "line_count": len(lines),
        "lines": lines
    }


def _compute_voucher_nav_result(details: list, current_detail_id: int, direction: str) -> dict:
    """
    Shared nav computation used by both get_voucher_nav() and verify_voucher_checks() (the
    "Verify & Next/Close" action) - factored out so the two don't duplicate the ordered-group-id/
    boundary logic. `details` is the already-fetched _fetch_session_details_grouped() result for
    one session. Raises HTTPException(404) if current_detail_id isn't in `details`.
    """
    ordered_group_ids = []
    seen = set()
    for d in details:
        gid = d['_doc_group_id']
        if gid not in seen:
            seen.add(gid)
            ordered_group_ids.append(gid)
    total_vouchers = len(ordered_group_ids)

    current = next((d for d in details if d['id'] == current_detail_id), None)
    if current is None:
        raise HTTPException(404, "Verification detail not found in this session.")

    current_index = ordered_group_ids.index(current['_doc_group_id'])
    target_index = current_index - 1 if direction == "prev" else current_index + 1

    if target_index < 0 or target_index >= total_vouchers:
        return {
            "boundary": True,
            "message": "No previous voucher." if direction == "prev" else "No next voucher.",
            "voucher_index": current_index + 1,
            "total_vouchers": total_vouchers
        }

    target_group_id = ordered_group_ids[target_index]
    voucher_rows = [d for d in details if d['_doc_group_id'] == target_group_id]
    anchor = voucher_rows[0]
    lines = [_build_verification_line(d) for d in voucher_rows]

    return {
        "boundary": False,
        "voucher_id": anchor['transaction_ref'],
        "voucher_date": anchor['extract_date'],
        "document_group_id": target_group_id,
        "line_count": len(lines),
        "lines": lines,
        "voucher_index": target_index + 1,
        "total_vouchers": total_vouchers
    }


@router.get("/api/verification/voucher_nav/{session_id}")
def get_voucher_nav(
    session_id: int,
    current_detail_id: int = Query(..., description="detail_id of any line in the currently displayed voucher"),
    direction: str = Query(..., pattern="^(next|prev)$", description="'next' or 'prev'")
):
    """
    Phase A: sequential voucher-to-voucher navigation, independent of
    verification status (unlike /next, which always jumps to the next
    Pending voucher). Vouchers are ordered by each group's earliest
    client_data_id — the same physical row order /next already uses for
    lines within a voucher.
    """
    session = execute_query_pg("SELECT id FROM verification_sessions WHERE id = %s AND status != 'Cancelled'", (session_id,))
    if not session:
        raise HTTPException(404, "Verification session not found or cancelled.")

    details = _fetch_session_details_grouped(session_id)
    return _compute_voucher_nav_result(details, current_detail_id, direction)


@router.post("/api/verification/verify_voucher_checks")
def verify_voucher_checks(
    session_id: int = Body(..., embed=True),
    current_detail_id: int = Body(..., embed=True, description="detail_id of any line in the currently displayed voucher"),
    perf: bool = Query(False)
):
    """
    Voucher-level Checks simplified to name-only (Phase 2 of 4): the "Verify & Next/Close"
    action. This is the ONLY trigger that pushes unresolved ("not done") voucher-level checks
    into the voucher's Observation field - ordinary Prev/Next/close-icon saves keep auto-saving
    each check's tick state (via save_voucher_check) but never themselves change what's recorded
    in Observations. Re-running this (e.g. re-verifying an already-closed voucher) correctly
    re-syncs: a since-fixed check's line is dropped, a still-unresolved one stays/updates -
    not a one-time write - because it always recomputes flagged_in_observations fresh from the
    live state and sync_line_observations_to_voucher() always rebuilds the full field from that
    flag rather than appending.

    Backend safety net: rejects with 400 if any active check is still 'untouched' for this
    voucher, mirroring the button-disable condition Phase 3's frontend will also enforce
    client-side.
    """
    perf_token = start_db_metrics() if perf else None
    perf_started = time.perf_counter()
    timings = {} if perf else None
    try:
        result = _verify_voucher_checks_impl(session_id, current_detail_id, perf_timings=timings)
        if perf and isinstance(result, dict):
            metrics = get_db_metrics()
            result["_perf"] = {
                "components": timings,
                "db_calls": metrics["calls"],
                "db_reads": metrics["reads"],
                "db_writes": metrics["writes"],
                "db_ms": round(metrics["db_time"] * 1000, 1),
            }
        return result
    finally:
        if perf_token is not None:
            metrics = get_db_metrics()
            print(json.dumps({"perf": "verify_voucher_checks", "session_id": session_id,
                              "detail_id": current_detail_id,
                              "total_ms": round((time.perf_counter() - perf_started) * 1000, 1),
                              "observation_sync_ms": timings.get("observation_sync_ms", 0),
                              "db_calls": metrics["calls"], "db_reads": metrics["reads"],
                              "db_writes": metrics["writes"],
                              "db_ms": round(metrics["db_time"] * 1000, 1)}))
            stop_db_metrics(perf_token)


def _verify_voucher_checks_impl(session_id: int, current_detail_id: int, perf_timings=None, rulebook=None):
    session = execute_query_pg("SELECT id, client_id FROM verification_sessions WHERE id = %s AND status != 'Cancelled'", (session_id,))
    if not session:
        raise HTTPException(404, "Verification session not found or cancelled.")
    client_id = session[0]['client_id']
    session_rulebook = rulebook or load_verification_rulebook_for_session(session_id)

    details = _fetch_session_details_grouped(session_id)
    current = next((d for d in details if d['id'] == current_detail_id), None)
    if current is None:
        raise HTTPException(404, "Verification detail not found in this session.")

    transaction_ref = current['transaction_ref']
    voucher_rows = [d for d in details if d['_doc_group_id'] == current['_doc_group_id']]

    active_checks = (session_rulebook or {}).get("voucher_checks", [])
    status_rows = execute_query_pg(
        "SELECT check_id, state FROM voucher_check_status WHERE client_id = %s AND transaction_ref = %s",
        (client_id, transaction_ref)
    )
    state_by_check_id = {r['check_id']: r['state'] for r in status_rows}

    untouched = [c for c in active_checks if state_by_check_id.get(c['id'], 'untouched') == 'untouched']
    if untouched:
        names = ", ".join(c['name'] for c in untouched)
        raise HTTPException(400, f"Cannot verify: {len(untouched)} check(s) still not marked ({names}).")

    not_done_ids = {c['id'] for c in active_checks if state_by_check_id.get(c['id']) == 'not_done'}
    # Batched (was one UPDATE per active check): every active check's flagged_in_observations
    # is purely a function of not_done_ids membership, so this partitions into at most 2
    # set-based UPDATEs instead of N.
    done_ids = [c['id'] for c in active_checks if c['id'] not in not_done_ids]
    if not_done_ids:
        placeholders = ",".join("%s" for _ in not_done_ids)
        execute_write_pg(
            f"UPDATE voucher_check_status SET flagged_in_observations = TRUE "
            f"WHERE client_id = %s AND transaction_ref = %s AND check_id IN ({placeholders})",
            (client_id, transaction_ref, *not_done_ids)
        )
    if done_ids:
        placeholders = ",".join("%s" for _ in done_ids)
        execute_write_pg(
            f"UPDATE voucher_check_status SET flagged_in_observations = FALSE "
            f"WHERE client_id = %s AND transaction_ref = %s AND check_id IN ({placeholders})",
            (client_id, transaction_ref, *done_ids)
        )

    # Not batched, deliberately: sync_line_observations_to_voucher() isn't a single write per
    # line - it resolves sibling ids, runs its own SELECTs/dedup, and writes both
    # verification_details and voucher_line_observations per line, with its own try/except so
    # one line's failure never blocks another's. Collapsing this across lines would mean
    # rewriting a shared function (also called per-line elsewhere in this file) with detailed
    # correctness invariants documented in its own docstring - not worth the risk for what's
    # normally just a handful of lines per voucher.
    sync_started = time.perf_counter()
    voucher_sibling_ids = _resolve_sibling_voucher_line_ids(current_detail_id) or [current_detail_id]
    sync_voucher_observations_to_voucher([row['id'] for row in voucher_rows], voucher_sibling_ids)
    if perf_timings is not None:
        perf_timings["observation_sync_ms"] = round((time.perf_counter() - sync_started) * 1000, 1)

    result = _compute_voucher_nav_result(details, current_detail_id, "next")
    result["verified"] = True
    result["not_done_count"] = len(not_done_ids)
    return result

@router.post("/api/verification/save_line_items")
def save_line_items(
    detail_id: int = Body(..., embed=True),
    line_items: List[Dict[str, Any]] = Body(..., embed=True),
    transaction_type: Optional[str] = Body(None, embed=True),
    procedure_data: Optional[Dict[str, Any]] = Body(None, embed=True),
    perf: bool = Query(False)
):
    perf_token = start_db_metrics() if perf else None
    started = time.perf_counter()
    timings = {} if perf else None
    try:
        return _save_line_items_impl(detail_id, line_items, transaction_type, procedure_data, timings)
    finally:
        if perf_token is not None:
            metrics = get_db_metrics()
            print(json.dumps({"perf": "save_line_items", "detail_id": detail_id,
                              "total_ms": round((time.perf_counter() - started) * 1000, 1),
                              "observation_sync_ms": timings.get("observation_sync_ms", 0),
                              "status_engine_ms": timings.get("status_engine_ms", 0),
                              "db_calls": metrics["calls"], "db_reads": metrics["reads"],
                              "db_writes": metrics["writes"],
                              "db_ms": round(metrics["db_time"] * 1000, 1)}))
            stop_db_metrics(perf_token)


def _save_line_items_impl(detail_id, line_items, transaction_type, procedure_data,
                          perf_timings=None, rulebook=None, compute_status=True,
                          sibling_ids=None):
    detail = execute_query_pg(
        "SELECT id, status FROM verification_details WHERE id = %s",
        (detail_id,)
    )
    if not detail:
        raise HTTPException(404, "Verification detail not found.")
    if detail[0]['status'] != 'Pending':
        raise HTTPException(400, "Transaction is already verified or skipped.")

    # Resolved once here and threaded through both calls below (and everything they in turn
    # call) instead of each re-deriving the same answer independently - see
    # _resolve_sibling_voucher_line_ids()'s own docstring for what this list represents.
    sibling_ids = sibling_ids if sibling_ids is not None else (
        _resolve_sibling_voucher_line_ids(detail_id) or [detail_id]
    )

    if not isinstance(line_items, list):
        raise HTTPException(400, "line_items must be a list.")
    for item in line_items:
        if not isinstance(item, dict):
            raise HTTPException(400, "Each line item must be a dict.")
        required_keys = ['vendor', 'unit', 'qty', 'rate', 'calculated_amount']
        for key in required_keys:
            if key not in item:
                raise HTTPException(400, f"Each line item must have '{key}' field.")
        try:
            float(item['qty'])
            float(item['rate'])
            float(item['calculated_amount'])
        except:
            raise HTTPException(400, "Qty, Rate, and Calculated Amount must be numeric.")
    
    # line_items/procedure_data are JSONB columns - written via Json(...) so
    # psycopg2/Postgres own the actual encoding, not a pre-encoded JSON string.
    updates = {"line_items": Json(line_items)}
    if transaction_type is not None:
        if procedure_data is not None:
            proc = procedure_data.copy()
            proc['transaction_type'] = transaction_type
            updates["procedure_data"] = Json(proc)
        else:
            updates["procedure_data"] = Json({"transaction_type": transaction_type})
    elif procedure_data is not None:
        updates["procedure_data"] = Json(procedure_data)
    
    set_clauses = []
    params = []
    for key, value in updates.items():
        set_clauses.append(f"{key} = %s")
        params.append(value)
    params.append(detail_id)
    sql = f"UPDATE verification_details SET {', '.join(set_clauses)} WHERE id = %s"
    execute_write_pg(sql, params)

    # Phase 6: roll any unsynced per-document observation entries into this line's own
    # Observation field. save_line_items() is the single chokepoint every Save/Next/Prev/
    # Validate/Complete/Exit path already calls per line, so this is the correct place to sync.
    sync_started = time.perf_counter()
    sync_line_observations_to_voucher(detail_id, sibling_ids=sibling_ids)
    if perf_timings is not None:
        perf_timings["observation_sync_ms"] = round((time.perf_counter() - sync_started) * 1000, 1)

    # Validation Rules Phase 4: recompute Cleared/Query status on every standalone save, same
    # chokepoint as the observation sync directly above. The grouped Save/Verify endpoint passes
    # compute_status=False because it deliberately recomputes once after ALL supplied procedure,
    # voucher-check, observation, and Physical Bill state has been persisted below; calculating
    # here first would be both incomplete and duplicated work in the same transaction.
    status_result = None
    if compute_status:
        status_started = time.perf_counter()
        status_result = compute_and_persist_cleared_status(
            detail_id, sibling_ids=sibling_ids, rulebook=rulebook
        )
        if perf_timings is not None:
            perf_timings["status_engine_ms"] = round((time.perf_counter() - status_started) * 1000, 1)

    log_audit("Analyst", "Save Line Items", "Verification", f"Detail {detail_id} line items saved.")
    response = {"message": "Line items and procedure data saved successfully."}
    if status_result:
        response["cleared_status"] = status_result["cleared_status"]
        response["matrix_version_applied"] = status_result["matrix_version_applied"]
        response["category_id_applied"] = status_result["category_id_applied"]
        response["category_name_applied"] = status_result["category_name_applied"]
    return response


@router.post("/api/verification/save_and_verify_voucher")
def save_and_verify_voucher(
    session_id: int = Body(..., embed=True),
    current_detail_id: int = Body(..., embed=True),
    detail_id: int = Body(..., embed=True),
    line_items: List[Dict[str, Any]] = Body(..., embed=True),
    transaction_type: Optional[str] = Body(None, embed=True),
    procedure_data: Optional[Dict[str, Any]] = Body(None, embed=True),
):
    """Atomically persist the current line and verify its voucher."""
    if detail_id != current_detail_id:
        raise HTTPException(400, "The saved line and verified line must match.")
    with pg_transaction():
        _save_line_items_impl(detail_id, line_items, transaction_type, procedure_data)
        return _verify_voucher_checks_impl(session_id, current_detail_id)


def _bundle_perf_diagnostics(fn):
    """Add opt-in total/database timings without changing the save transaction."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not kwargs.get("perf", False):
            return fn(*args, **kwargs)
        token = start_db_metrics()
        started = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
            metrics = get_db_metrics()
            if isinstance(result, dict):
                result["_perf"] = {
                    "total_ms": round((time.perf_counter() - started) * 1000, 1),
                    "db_calls": metrics["calls"],
                    "db_reads": metrics["reads"],
                    "db_writes": metrics["writes"],
                    "db_ms": round(metrics["db_time"] * 1000, 1),
                }
            return result
        finally:
            metrics = get_db_metrics()
            print(json.dumps({"perf": "save_and_verify_voucher_bundle",
                              "total_ms": round((time.perf_counter() - started) * 1000, 1),
                              "components": result.get("_perf_components", {}) if isinstance(result, dict) else {},
                              "db_calls": metrics["calls"], "db_reads": metrics["reads"],
                              "db_writes": metrics["writes"],
                              "db_ms": round(metrics["db_time"] * 1000, 1)}))
            stop_db_metrics(token)
    return wrapped


@router.post("/api/verification/save_and_verify_voucher_bundle")
@_bundle_perf_diagnostics
def save_and_verify_voucher_bundle(
    session_id: int = Body(..., embed=True),
    current_detail_id: int = Body(..., embed=True),
    lines: List[Dict[str, Any]] = Body(..., embed=True),
    verify: bool = Body(True, embed=True),
    observations: Optional[List[Dict[str, Any]]] = Body(None, embed=True),
    account_head_notes: Optional[List[Dict[str, Any]]] = Body(None, embed=True),
    physical_bills: Optional[List[Dict[str, Any]]] = Body(None, embed=True),
    form_entries: Optional[List[Dict[str, Any]]] = Body(None, embed=True),
    procedure_checks: Optional[List[Dict[str, Any]]] = Body(None, embed=True),
    voucher_checks: Optional[List[Dict[str, Any]]] = Body(None, embed=True),
    perf: bool = Query(False),
):
    """Atomically save all supplied voucher lines and tick state, then verify once.

    Observations and Physical Bills are intentionally not accepted until their pending modal
    state can be serialized without losing fields; that is the next 4B sub-phase.
    """
    procedure_checks = procedure_checks if isinstance(procedure_checks, list) else []
    voucher_checks = voucher_checks if isinstance(voucher_checks, list) else []
    observations = observations if isinstance(observations, list) else []
    account_head_notes = account_head_notes if isinstance(account_head_notes, list) else []
    physical_bills = physical_bills if isinstance(physical_bills, list) else []
    form_entries = form_entries if isinstance(form_entries, list) else []
    if not lines:
        raise HTTPException(400, "At least one voucher line is required.")
    if str(current_detail_id) not in {str(line.get('detail_id')) for line in lines}:
        raise HTTPException(400, "The current detail must be included in lines.")
    timings = {} if perf else None
    line_statuses = {
        row['id']: row['status']
        for row in execute_query_pg(
            "SELECT id, status FROM verification_details WHERE id = ANY(%s)",
            ([line.get('detail_id') for line in lines],)
        )
    }
    if line_statuses.get(current_detail_id) != 'Pending':
        raise HTTPException(400, "The current voucher line is already verified or skipped.")
    eligible_line_ids = {
        line_id for line_id, status in line_statuses.items() if status == 'Pending'
    }
    if verify:
        session = execute_query_pg(
            "SELECT client_id FROM verification_sessions WHERE id = %s AND status != 'Cancelled'",
            (session_id,)
        )
        if not session:
            raise HTTPException(404, "Verification session not found or cancelled.")
        session_rulebook = load_verification_rulebook_for_session(session_id)
        active_checks = (session_rulebook or {}).get("voucher_checks", [])
        submitted_states = {str(c.get('check_id')): c.get('state') for c in voucher_checks}
        untouched = [c for c in active_checks if submitted_states.get(str(c['id']), 'untouched') == 'untouched']
        if untouched:
            names = ", ".join(c['name'] for c in untouched)
            raise HTTPException(400, f"Cannot verify: {len(untouched)} check(s) still not marked ({names}).")
    else:
        session_rulebook = None
    with pg_transaction():
        save_started = time.perf_counter()
        save_components = {"observation_sync_ms": 0.0, "status_engine_ms": 0.0}
        sibling_ids_by_line = {}
        for line in lines:
            line_id = line.get('detail_id')
            if line_id in eligible_line_ids:
                sibling_ids_by_line[line_id] = (
                    _resolve_sibling_voucher_line_ids(line_id) or [line_id]
                )
        for line in lines:
            line_id = line.get('detail_id')
            if line_id not in eligible_line_ids:
                continue
            line_timings = {} if perf else None
            _save_line_items_impl(
                line_id, line.get('line_items', []),
                line.get('transaction_type'), line.get('procedure_data'), line_timings,
                session_rulebook, compute_status=False,
                sibling_ids=sibling_ids_by_line[line_id]
            )
            if perf:
                for key in save_components:
                    save_components[key] += line_timings.get(key, 0)
        if perf:
            timings["line_saves_ms"] = round((time.perf_counter() - save_started) * 1000, 1)
            timings.update({key: round(value, 1) for key, value in save_components.items()})
        procedure_started = time.perf_counter()
        for entry in observations:
            execute_write_pg(
                """INSERT INTO voucher_line_observations
                   (voucher_line_id, document_id, observation_text, synced, updated_by, updated_at)
                   VALUES (%s, %s, %s, FALSE, %s, CURRENT_TIMESTAMP)
                   ON CONFLICT(voucher_line_id, document_id) DO UPDATE SET
                   observation_text = excluded.observation_text, synced = FALSE,
                   updated_by = excluded.updated_by, updated_at = CURRENT_TIMESTAMP""",
                (entry['voucher_line_id'], entry['document_id'], entry.get('observation_text', ''), 'Analyst')
            )
        for entry in account_head_notes:
            execute_write_pg(
                """INSERT INTO voucher_line_observations
                   (voucher_line_id, document_id, observation_text, synced, updated_by, updated_at)
                   VALUES (%s, %s, %s, FALSE, %s, CURRENT_TIMESTAMP)
                   ON CONFLICT(voucher_line_id, document_id) DO UPDATE SET
                   observation_text = excluded.observation_text, synced = FALSE,
                   updated_by = excluded.updated_by, updated_at = CURRENT_TIMESTAMP""",
                (entry['voucher_line_id'], ACCOUNT_HEAD_OBSERVATION_DOC_ID,
                 entry.get('observation_text', ''), 'Analyst')
            )
        for entry in physical_bills:
            line_id = entry['voucher_line_id']; document_id = entry['document_id']
            if not execute_query_pg("SELECT id FROM verification_details WHERE id = %s", (line_id,)):
                raise HTTPException(404, "Voucher line not found.")
            execute_write_pg("DELETE FROM voucher_line_physical_bill_items WHERE bill_id IN (SELECT id FROM voucher_line_physical_bills WHERE voucher_line_id = %s AND document_id = %s)", (line_id, document_id))
            execute_write_pg("DELETE FROM voucher_line_physical_bills WHERE voucher_line_id = %s AND document_id = %s", (line_id, document_id))
            for sort_order, bill in enumerate(entry.get('bills') or []):
                if not any([bill.get('bill_no'), bill.get('bill_date'), bill.get('vendor_name'), bill.get('items')]):
                    continue
                bill_id = execute_write_pg("""INSERT INTO voucher_line_physical_bills
                    (voucher_line_id, document_id, bill_no, bill_date, vendor_name, sort_order, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP) RETURNING id""",
                    (line_id, document_id, (bill.get('bill_no') or '').strip() or None,
                     bill.get('bill_date') or None, (bill.get('vendor_name') or '').strip() or None, sort_order))
                for item_order, item in enumerate(bill.get('items') or []):
                    qty = item.get('quantity'); rate = item.get('rate')
                    qty = None if qty in (None, '') else float(qty); rate = None if rate in (None, '') else float(rate)
                    amount = round(qty * rate, get_rate_precision()) if qty is not None and rate is not None else None
                    execute_write_pg("""INSERT INTO voucher_line_physical_bill_items
                        (bill_id, item_name, quantity, rate, amount, sort_order, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)""",
                        (bill_id, (item.get('item_name') or '').strip() or None, qty, rate, amount, item_order))
        for entry in form_entries:
            line_id = entry.get('voucher_line_id')
            document_id = entry.get('document_id')
            if line_id in eligible_line_ids:
                _replace_form_entries(line_id, document_id, entry.get('entries') or [])
        if observations or account_head_notes:
            for line in lines:
                sibling_ids = _resolve_sibling_voucher_line_ids(line['detail_id'])
                sync_line_observations_to_voucher(line['detail_id'], sibling_ids=sibling_ids)
        if procedure_checks:
            set_procedure_checks_batch(procedure_checks)
        if perf:
            timings["procedure_checks_ms"] = round((time.perf_counter() - procedure_started) * 1000, 1)
        voucher_started = time.perf_counter()
        if voucher_checks:
            session = execute_query_pg(
                "SELECT client_id FROM verification_sessions WHERE id = %s AND status != 'Cancelled'",
                (session_id,)
            )
            if not session:
                raise HTTPException(404, "Verification session not found or cancelled.")
            save_voucher_checks_batch(session[0]['client_id'],
                                      next(line.get('transaction_ref') for line in lines if str(line.get('detail_id')) == str(current_detail_id)),
                                      voucher_checks)
        if perf:
            timings["voucher_checks_ms"] = round((time.perf_counter() - voucher_started) * 1000, 1)

        # Procedure and voucher ticks are written after _save_line_items_impl(). Recompute only
        # after all supplied support data is persisted; otherwise deselecting a required tick can
        # leave a previously Cleared line visibly Cleared until a later save.
        status_results = {}
        status_started = time.perf_counter()
        for line in lines:
            line_id = line.get('detail_id')
            if line_id not in eligible_line_ids:
                continue
            status_result = compute_and_persist_cleared_status(
                line_id, sibling_ids=sibling_ids_by_line[line_id], rulebook=session_rulebook
            )
            if status_result:
                status_results[line_id] = status_result
        if perf:
            timings["post_support_status_engine_ms"] = round(
                (time.perf_counter() - status_started) * 1000, 1
            )
        verify_started = time.perf_counter()
        result = _verify_voucher_checks_impl(session_id, current_detail_id, timings, session_rulebook) if verify else {"saved": True}
        current_status = status_results.get(current_detail_id)
        result["line_statuses"] = {
            str(detail_id): value["cleared_status"]
            for detail_id, value in status_results.items()
        }
        if current_status:
            result["cleared_status"] = current_status["cleared_status"]
            result["matrix_version_applied"] = current_status["matrix_version_applied"]
        if perf:
            timings["verify_ms"] = round((time.perf_counter() - verify_started) * 1000, 1)
            result["_perf_components"] = timings
        return result

@router.post("/api/verification/validate")
def validate_transaction(
    detail_id: int = Body(..., embed=True),
    inventory_flag: bool = Body(False, embed=True)
):
    detail = execute_query_pg(
        """
        SELECT vd.id, vd.client_data_id, vd.line_items, vd.status, cd.extract_amount
        FROM verification_details vd
        JOIN client_data cd ON vd.client_data_id = cd.id
        WHERE vd.id = %s
        """,
        (detail_id,)
    )
    if not detail:
        raise HTTPException(404, "Verification detail not found.")
    d = detail[0]
    if d.get('status', 'Pending') != 'Pending':
        raise HTTPException(400, "Transaction is already verified or skipped.")
    line_items = []
    if d['line_items']:
        try:
            line_items = json.loads(d['line_items']) if isinstance(d['line_items'], str) else d['line_items']
        except:
            line_items = []
    if not line_items:
        return {
            "validation_status": "Failed",
            "message": "No line items found. Please add line items before validation."
        }
    total_line_amount = sum(float(item.get('calculated_amount', 0)) for item in line_items)
    transaction_amount = float(d['extract_amount'] or 0)
    tolerance = 0.01
    passed = abs(total_line_amount - transaction_amount) <= tolerance
    validation_status = "Passed" if passed else "Failed"
    if passed:
        message = f"Validation passed: Sum of line items ({total_line_amount:.2f}) matches transaction amount ({transaction_amount:.2f})."
    else:
        message = f"Validation failed: Sum of line items ({total_line_amount:.2f}) does not match transaction amount ({transaction_amount:.2f}). Difference: {total_line_amount - transaction_amount:.2f}"
    execute_write_pg(
        """
        UPDATE verification_details
        SET validation_status = %s, validation_message = %s
        WHERE id = %s
        """,
        (validation_status, message, detail_id)
    )
    log_audit("Analyst", "Validate Transaction", "Verification", f"Detail {detail_id}: {validation_status}")
    return {
        "validation_status": validation_status,
        "validation_message": message,
        "total_line_amount": total_line_amount,
        "transaction_amount": transaction_amount,
        "passed": passed
    }
@router.post("/api/verification/complete")
def complete_transaction(
    detail_id: int = Body(..., embed=True),
    verified_by: str = "Analyst"
):
    detail = execute_query_pg(
        "SELECT id, validation_status, status FROM verification_details WHERE id = %s",
        (detail_id,)
    )
    if not detail:
        raise HTTPException(404, "Verification detail not found.")
    d = detail[0]
    # Use .get() with a fallback to handle missing or NULL status
    if d.get('status', 'Pending') != 'Pending':
        raise HTTPException(400, "Transaction is already verified or skipped.")
    if d['validation_status'] != 'Passed':
        raise HTTPException(400, "Transaction validation has not passed. Please validate first.")
    
    # verified_at is TIMESTAMPTZ - pass a real tz-aware datetime object, not an isoformat string.
    now = datetime.now(timezone.utc)
    execute_write_pg(
        """
        UPDATE verification_details
        SET status = 'Verified', verified_by = %s, verified_at = %s
        WHERE id = %s
        """,
        (verified_by, now, detail_id)
    )
    
    session_id = execute_query_pg(
        "SELECT session_id FROM verification_details WHERE id = %s",
        (detail_id,)
    )[0]['session_id']
    execute_write_pg(
        """
        UPDATE verification_sessions
        SET verified_count = verified_count + 1
        WHERE id = %s
        """,
        (session_id,)
    )
    
    log_audit(verified_by, "Complete Verification", "Verification", f"Detail {detail_id} marked as verified.")
    return {"message": "Transaction marked as verified.", "detail_id": detail_id}

@router.get("/api/verification/supporting")
def get_supporting_documents(
    client_id: int = Query(..., description="Client ID"),
    search: str = Query(..., description="Search term (e.g., PO number, vendor name)"),
    template_id: int = Query(..., description="Template ID to filter (e.g., Purchase Orders)"),
    limit: int = Query(50, description="Max rows to return")
):
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive.")
    template = execute_query_pg("SELECT id FROM templates WHERE id = %s AND is_active = TRUE", (template_id,))
    if not template:
        raise HTTPException(404, "Template not found or inactive.")
    
    query = """
        SELECT id, row_data, extract_amount, extract_date
        FROM client_data
        WHERE client_id = %s AND template_id = %s AND is_deleted = FALSE
          AND EXISTS (
              SELECT 1 FROM jsonb_each_text(row_data)
              WHERE LOWER(value) LIKE LOWER(%s)
          )
        ORDER BY extract_date DESC
        LIMIT %s
    """
    rows = execute_query_pg(query, (client_id, template_id, f"%{search}%", limit))
    
    result = []
    for row in rows:
        rd = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
        result.append({
            "id": row['id'],
            "row_data": rd,
            "extract_amount": row['extract_amount'],
            "extract_date": row['extract_date']
        })
    return {"supporting_documents": result, "count": len(result)}

# ==============================================
# CASHBOOK PREVIEW (No session creation)
# ==============================================

@router.get("/api/verification/preview_cashbook")
def preview_cashbook(
    client_id: int = Query(..., description="Client ID"),
    cashbook_name: str = Query(..., description="Cashbook name"),
    date_start: str = Query(..., description="Start date (YYYY-MM-DD)"),
    date_end: str = Query(..., description="End date (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=200, description="Rows per page (max 200)")
):
    """
    Get paginated transactions for a specific cashbook without creating a session.
    Uses the same logic as start_verification_session but returns only the rows.
    """
    # Validate client
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")
    
    # Validate dates
    try:
        datetime.fromisoformat(date_start)
        datetime.fromisoformat(date_end)
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")
    if date_start > date_end:
        raise HTTPException(400, "Start date must be before or equal to end date.")
    
    client_prefixes = get_client_voucher_prefixes_pg(client_id)

    # Fetch rows
    rows = execute_query_pg(
        """
        SELECT id, row_data, extract_date, extract_amount
        FROM client_data
        WHERE client_id = %s AND is_deleted = FALSE AND change_detection_status != 'deleted'
          AND extract_date BETWEEN %s AND %s
        ORDER BY extract_date
        """,
        (client_id, date_start, date_end)
    )

    # Group by voucher ID and filter cashbook
    vouchers = {}
    voucher_aliases = ['Voucher ID', 'Voucher No', 'Voucher Number', 'Voucher', 'Invoice No', 'Invoice Number']
    account_aliases = ['Account Name', 'Account', 'Account Detail', 'Account Code', 'Ledger', 'Head']
    debit_aliases = ['Debit', 'Debit Amount', 'Dr', 'Dr Amount', 'Debit (Dr)', 'Dr. Amount']
    credit_aliases = ['Credit', 'Credit Amount', 'Cr', 'Cr Amount', 'Credit (Cr)', 'Cr. Amount']

    def get_col_value(row_data, aliases):
        for alias in aliases:
            if alias in row_data:
                val = row_data[alias]
                if val is not None and val != '':
                    return val
        return None

    # Cashbook screen Phase 3 of 3, FINAL: literal row_data key names (as they actually appear
    # on this row, not a hardcoded guess) that matched debit_aliases/credit_aliases - lets the
    # frontend strip exactly the right column(s) when collapsing Debit/Credit into one signed
    # Amount column, without needing its own separate alias list that could drift from this one.
    def find_debit_credit_keys(row_data, debit_aliases, credit_aliases):
        dc_aliases = set(debit_aliases) | set(credit_aliases)
        return [k for k in row_data.keys() if k in dc_aliases]

    for row in rows:
        try:
            rd = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
            if not isinstance(rd, dict):
                continue
            voucher_id = get_col_value(rd, voucher_aliases)
            if not voucher_id:
                continue
            category = match_voucher_category(voucher_id, client_prefixes)
            if category not in ('Cash Payment', 'Cash Receipt', 'Bank Payment', 'Bank Receipt'):
                continue
            debit_str = get_col_value(rd, debit_aliases)
            credit_str = get_col_value(rd, credit_aliases)
            debit = float(debit_str) if debit_str else 0.0
            credit = float(credit_str) if credit_str else 0.0
            account_name = get_col_value(rd, account_aliases)
            if not account_name:
                continue
            if voucher_id not in vouchers:
                vouchers[voucher_id] = {
                    'category': category,
                    'rows': []
                }
            vouchers[voucher_id]['rows'].append({
                'row_id': row['id'],
                'row_data': rd,
                'debit': debit,
                'credit': credit,
                'account_name': account_name,
                'extract_date': row['extract_date'],
                'extract_amount': row['extract_amount']
            })
        except:
            continue

    # Filter to only the selected cashbook and collect transaction rows
    transactions = []
    for vid, data in vouchers.items():
        # Same category-implies-type derivation as start_verification_session - cashbook_row
        # is looked up only as a sanity check that the named cashbook actually appears among
        # this voucher's legs (on the structurally correct side, see is_cashbook_leg_row()),
        # not to derive vtype anymore.
        cashbook_row = next((r for r in data['rows'] if r['account_name'] == cashbook_name and is_cashbook_leg_row(r, data['category'])), None)
        if cashbook_row is None:
            continue
        vtype = voucher_type_label(data['category'])
        if vtype is None:
            continue
        for row in data['rows']:
            if vtype in ('CPV', 'BPV') and row['debit'] > 0 and row['credit'] == 0:
                transactions.append({
                    'client_data_id': row['row_id'],
                    'row_data': row['row_data'],
                    'voucher_id': vid,
                    'extract_date': row['extract_date'],
                    'extract_amount': row['extract_amount'],
                    # Signed Amount (Cashbook Phase 3): positive for a Payment (CPV/BPV, debit
                    # side), negative for a Receipt (CRV/BRV, credit side) - same vtype/side
                    # condition already gating this branch, just applying a sign to the value.
                    'amount': row['debit'],
                    'debit_credit_keys': find_debit_credit_keys(row['row_data'], debit_aliases, credit_aliases)
                })
            elif vtype in ('CRV', 'BRV') and row['credit'] > 0 and row['debit'] == 0:
                transactions.append({
                    'client_data_id': row['row_id'],
                    'row_data': row['row_data'],
                    'voucher_id': vid,
                    'extract_date': row['extract_date'],
                    'extract_amount': row['extract_amount'],
                    'amount': -row['credit'],
                    'debit_credit_keys': find_debit_credit_keys(row['row_data'], debit_aliases, credit_aliases)
                })

    # Status column (Cashbook screen Phase 2 of 3): computed over the FULL (unpaginated)
    # transactions list, not just the current page, so a voucher whose lines straddle two
    # pages still gets one consistent status. This is line-level filtering identical to
    # start_verification_session()'s own row-building, so the client_data_ids collected here
    # are exactly the set that would become verification_details rows for this cashbook - the
    # lookup below reuses that fact rather than re-deriving it.
    voucher_status_map: Dict[str, str] = {}
    all_client_data_ids = [t['client_data_id'] for t in transactions]
    if all_client_data_ids:
        placeholders = ",".join("%s" for _ in all_client_data_ids)
        vd_rows = execute_query_pg(
            f"""
            SELECT client_data_id, cleared_status, id
            FROM verification_details
            WHERE client_data_id IN ({placeholders}) AND range_status = 'active'
            ORDER BY id ASC
            """,
            tuple(all_client_data_ids)
        )
        # Dedup per client_data_id, keeping the highest id (most recently created session's
        # row) - this table has no updated_at, so id order is the best available proxy for
        # recency. A client_data_id absent from this dict was never captured into any
        # session's verification_details at all (distinct from a row that exists with a NULL
        # cleared_status - see the empty-list handling below).
        latest_status_by_cdid: Dict[int, Optional[str]] = {}
        for r in vd_rows:
            latest_status_by_cdid[r['client_data_id']] = r['cleared_status']

        # Group by voucher_id, using only lines that actually have a verification_details row.
        statuses_by_voucher: Dict[str, list] = {}
        for t in transactions:
            cdid = t['client_data_id']
            if cdid in latest_status_by_cdid:
                statuses_by_voucher.setdefault(t['voucher_id'], []).append(latest_status_by_cdid[cdid])
            else:
                statuses_by_voucher.setdefault(t['voucher_id'], [])

        for vid, statuses in statuses_by_voucher.items():
            # _rollup_voucher_status([]) would wrongly return 'Cleared' (Python's all() on an
            # empty list is True) - a voucher with zero verification_details rows across any of
            # its lines has simply never been touched, a distinct case from _rollup_voucher_status()'s
            # own 'Pending' (which means a row exists but isn't yet Cleared/Query).
            voucher_status_map[vid] = _rollup_voucher_status(statuses) if statuses else 'Not Started'

    # Pagination
    total = len(transactions)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    paginated = transactions[start_idx:end_idx]

    # Build response rows with row_data as dict
    result_rows = []
    for trans in paginated:
        rd = json.loads(trans['row_data']) if isinstance(trans['row_data'], str) else trans['row_data']
        result_rows.append({
            'row_data': rd,
            'extract_date': trans['extract_date'],
            'extract_amount': trans['extract_amount'],
            'status': voucher_status_map.get(trans['voucher_id'], 'Not Started'),
            'amount': trans['amount'],
            'debit_credit_keys': trans['debit_credit_keys']
        })

    return {
        'total_rows': total,
        'page': page,
        'limit': limit,
        'rows': result_rows
    }
# ==============================================
# PHASE 4 ENDPOINTS – Audited Cashbook & Export
# ==============================================

@router.get("/api/verification/audited_cashbook/{session_id}")
def get_audited_cashbook(session_id: int):
    """
    Retrieve the audited cashbook for a session.
    Includes all transactions with their verification details (line items, procedure status, etc.)
    Returns session header and list of transactions.
    """
    # Get session info
    session = execute_query_pg(
        """
        SELECT id, client_id, cashbook_name, period_start, period_end, 
               total_transactions, verified_count, status, created_by, created_at
        FROM verification_sessions
        WHERE id = %s AND status != 'Cancelled'
        """,
        (session_id,)
    )
    if not session:
        raise HTTPException(404, "Verification session not found or cancelled.")
    
    s = session[0]
    # Get all details
    details = execute_query_pg(
        """
        SELECT vd.id, vd.client_data_id, vd.transaction_ref, vd.line_items, vd.procedure_data,
               vd.validation_status, vd.validation_message, vd.status as verification_status,
               vd.comments, vd.observations, vd.verified_by, vd.verified_at,
               vd.cleared_status, vd.matrix_version_applied,
               vd.category_id_applied, vd.category_name_applied,
               cd.row_data, cd.extract_amount, cd.extract_date
        FROM verification_details vd
        JOIN client_data cd ON vd.client_data_id = cd.id
        WHERE vd.session_id = %s AND vd.range_status = 'active'
        ORDER BY vd.id
        """,
        (session_id,)
    )
    
    transactions = []
    for d in details:
        # Parse row_data
        rd = json.loads(d['row_data']) if isinstance(d['row_data'], str) else d['row_data']
        # Parse line_items
        line_items = []
        if d['line_items']:
            try:
                line_items = json.loads(d['line_items']) if isinstance(d['line_items'], str) else d['line_items']
            except:
                line_items = []
        # Parse procedure_data
        proc = {}
        if d['procedure_data']:
            try:
                proc = json.loads(d['procedure_data']) if isinstance(d['procedure_data'], str) else d['procedure_data']
            except:
                proc = {}
        # Flatten procedure flags
        proc_flags = flatten_procedure_data(proc)
        
        # Build transaction entry
        trans = {
            "detail_id": d['id'],
            "client_data_id": d['client_data_id'],
            "voucher_id": d['transaction_ref'],
            "row_data": rd,
            "extract_amount": d['extract_amount'],
            "extract_date": d['extract_date'],
            "line_items": line_items,
            "procedure_flags": proc_flags,
            "observations": d['observations'] or "",
            "comments": d['comments'] or "",
            "validation_status": d['validation_status'],
            "validation_message": d['validation_message'],
            "verification_status": d['verification_status'],
            "verified_by": d['verified_by'],
            "verified_at": d['verified_at'],
            # Confirmed fix, 2026-08-09: previously never selected/returned here, so any line
            # rendered via this list (hard-refresh restore-to-voucher, Prev navigation, the 🔍
            # popover — all route through navigateToTransaction(), which renders directly from
            # this endpoint's data) showed the Pending pill even for an already-Cleared line,
            # since cleared_status was undefined rather than the persisted value. Mirrors
            # _build_verification_line()'s own d.get('cleared_status')/d.get('matrix_version_applied')
            # — same already-computed columns, no change to how cleared_status is computed.
            "cleared_status": d['cleared_status'],
            "matrix_version_applied": d['matrix_version_applied'],
            # Freeze Category on voucher lines: frozen once this line has been saved with a
            # resolvable Category - the frontend prefers these over a live category-for-account
            # lookup when present, so an already-audited line can't drift with a later TB change.
            "category_id_applied": d['category_id_applied'],
            "category_name_applied": d['category_name_applied']
        }
        transactions.append(trans)
    
    return {
        "session": {
            "id": s['id'],
            "client_id": s['client_id'],
            "cashbook_name": s['cashbook_name'],
            "period_start": s['period_start'],
            "period_end": s['period_end'],
            "total_transactions": s['total_transactions'],
            "verified_count": s['verified_count'],
            "status": s['status'],
            "created_by": s['created_by'],
            "created_at": s['created_at']
        },
        "transactions": transactions
    }
@router.get("/api/verification/session/{session_id}/index")
def get_verification_session_index(session_id: int):
    """Return compact session identity/status rows for future lightweight loading."""
    session = execute_query_pg(
        """SELECT id, client_id, cashbook_name, period_start, period_end, total_transactions,
                  verified_count, status, created_by, created_at
           FROM verification_sessions
           WHERE id = %s AND status != 'Cancelled'""", (session_id,)
    )
    if not session:
        raise HTTPException(404, "Verification session not found or cancelled.")
    rows = execute_query_pg(
        """SELECT vd.id AS detail_id, vd.client_data_id, vd.transaction_ref AS voucher_id,
                  vd.validation_status, vd.status AS verification_status,
                  vd.cleared_status, vd.matrix_version_applied,
                  cd.extract_amount, cd.extract_date
           FROM verification_details vd JOIN client_data cd ON cd.id = vd.client_data_id
           WHERE vd.session_id = %s AND vd.range_status = 'active' ORDER BY vd.id""", (session_id,)
    )
    s = session[0]
    return {"session": s, "transactions": rows}


@router.get("/api/verification/session/{session_id}/rulebook")
def get_verification_session_rulebook(session_id: int):
    """Return the stable Rulebook captured by a Verification session.

    Legacy sessions without a captured version remain readable using the current Rulebook. A
    version mismatch is rejected so a session never silently evaluates changed stable rules.
    Voucher evidence and calculated results are deliberately not included here.
    """
    rulebook = load_verification_rulebook_for_session(session_id)
    return {"session_id": session_id, "rulebook": rulebook}


@router.post("/api/verification/session/{session_id}/rulebook/rebase")
def rebase_verification_session_rulebook(session_id: int, user_session=Depends(get_current_user)):
    """Explicitly move a resumable session to the current Rulebook version.

    This changes only the session's stable-rule reference. Verification details, evidence,
    observations, and saved conclusions remain untouched so recovery cannot discard work.
    """
    sessions = execute_query_pg(
        "SELECT client_id, rulebook_version FROM verification_sessions "
        "WHERE id = %s AND status != 'Cancelled'",
        (session_id,)
    )
    if not sessions:
        raise HTTPException(404, "Verification session not found or cancelled.")

    current_version = get_audit_rules_config_version()
    if sessions[0].get("rulebook_version") == current_version:
        return {
            "session_id": session_id,
            "rulebook_version": current_version,
            "recovered": False,
            "message": "Verification session already uses the current Rulebook.",
        }

    updated = execute_write_pg(
        "UPDATE verification_sessions SET rulebook_version = %s "
        "WHERE id = %s AND status != 'Cancelled' RETURNING id",
        (current_version, session_id)
    )
    if updated is None:
        raise HTTPException(404, "Verification session not found or cancelled.")
    return {
        "session_id": session_id,
        "rulebook_version": current_version,
        "recovered": True,
        "message": "Verification session is ready to resume with the current Rulebook.",
    }


def load_verification_rulebook_for_session(session_id: int) -> dict:
    """Load a session's stable Rulebook while enforcing its captured version."""
    sessions = execute_query_pg(
        "SELECT client_id, rulebook_version FROM verification_sessions "
        "WHERE id = %s AND status != 'Cancelled'",
        (session_id,)
    )
    if not sessions:
        raise HTTPException(404, "Verification session not found or cancelled.")

    session_version = sessions[0].get("rulebook_version")
    current_version = get_audit_rules_config_version()
    if session_version is not None and session_version != current_version:
        changes = get_audit_rules_config_changes_since(session_version)
        if not changes:
            changes = [{
                "config_version": current_version,
                "entity_type": "Audit Rules configuration",
                "entity_id": None,
                "entity_name": None,
                "action": "Updated",
                "changed_by": "System",
                "changed_at": None,
            }]
        raise HTTPException(
            409,
            {
                "code": "RULEBOOK_VERSION_MISMATCH",
                "message": "This Verification session uses an older Rulebook version.",
                "session_id": session_id,
                "session_version": session_version,
                "current_version": current_version,
                "changes": changes,
                "recovery": {
                    "method": "POST",
                    "path": f"/api/verification/session/{session_id}/rulebook/rebase",
                    "preserves_work": True,
                },
            }
        )

    return load_client_verification_rulebook(sessions[0]["client_id"])


@router.get("/api/verification/session/{session_id}/progress")
def get_session_progress(session_id: int):
    """
    Lightweight endpoint to get only the progress counts for a session.
    Returns total_transactions and verified_count.
    """
    session = execute_query_pg(
        """
        SELECT total_transactions, verified_count
        FROM verification_sessions
        WHERE id = %s AND status != 'Cancelled'
        """,
        (session_id,)
    )
    if not session:
        raise HTTPException(404, "Session not found or cancelled.")
    return {
        "total_transactions": session[0]['total_transactions'],
        "verified_count": session[0]['verified_count']
    }


def _rollup_voucher_status(cleared_statuses: list) -> str:
    """
    Shared voucher-level Cleared/Query/Pending rollup rule, factored out of get_voucher_stats()
    (Audit Workbook Phase 1) so it and get_audit_workbook() can never silently drift apart.

    Rollup rule: all lines cleared_status == 'Cleared' -> Cleared; else any line
    cleared_status == 'Query' -> Query (Query takes priority over Pending); else -> Pending
    (covers NULL/unclassified).
    """
    if all(s == 'Cleared' for s in cleared_statuses):
        return 'Cleared'
    elif any(s == 'Query' for s in cleared_statuses):
        return 'Query'
    else:
        return 'Pending'


@router.get("/api/verification/session/{session_id}/voucher_stats")
def get_voucher_stats(session_id: int):
    """
    Voucher-wise Cleared/Query/Pending counts for a session's whole cashbook (not just the
    currently open voucher) - powers the header's Total/Cleared/Query/Pending stat (new,
    confirmed 3-phase requirement, this is Phase 1/backend), replacing the old line-based
    "Verified" counter. Groups by transaction_ref, matching the same grouping
    computeVoucherStats() (static/verification.js) already uses for "Total Vouchers" - not
    the _doc_group_id grouping _fetch_session_details_grouped()/get_next_transaction() use for
    line navigation - so this endpoint's `total` agrees with the existing header number exactly.

    cleared + query + pending always equals total. See _rollup_voucher_status() for the rule.
    """
    session = execute_query_pg(
        "SELECT id FROM verification_sessions WHERE id = %s AND status != 'Cancelled'",
        (session_id,)
    )
    if not session:
        raise HTTPException(404, "Session not found or cancelled.")

    rows = execute_query_pg(
        "SELECT transaction_ref, cleared_status FROM verification_details WHERE session_id = %s AND range_status = 'active'",
        (session_id,)
    )

    lines_by_voucher: Dict[str, list] = {}
    for r in rows:
        ref = r['transaction_ref']
        if not ref:
            continue
        lines_by_voucher.setdefault(ref, []).append(r['cleared_status'])

    cleared = query = pending = 0
    for statuses in lines_by_voucher.values():
        status = _rollup_voucher_status(statuses)
        if status == 'Cleared':
            cleared += 1
        elif status == 'Query':
            query += 1
        else:
            pending += 1

    total = len(lines_by_voucher)
    return {"total": total, "cleared": cleared, "query": query, "pending": pending}


def _audit_workbook_resolve_categories(client_id: int, account_codes: set) -> Dict[str, Optional[int]]:
    """
    Batched version of compute_and_persist_cleared_status()'s per-line Category resolution
    (TB-first, account_category_assignments-fallback) - resolved once for every distinct account
    code in a session's line set, instead of once per line, so get_audit_workbook() doesn't run
    N separate lookups for a 300-line session. Same rule, same precedence: the client's latest
    parsed Trial Balance wins whenever the code is present there with a non-null active category;
    account_category_assignments is used only as a fallback for a code the TB doesn't resolve.

    Returns {account_code: category_id or None} - every requested code is present in the result.
    """
    return {
        code: value["category_id"]
        for code, value in load_client_account_category_index(client_id, list(account_codes)).items()
    }


def _build_working_paper_gates(gates: List[dict]) -> List[dict]:
    """Group detailed status checks into the four auditor-facing workpaper gates."""
    groups = [
        ("approval", "Approval", {"voucher_checks", "approval_authority"}),
        ("supporting_documents", "Supporting Documents", {"documents", "signature", "cross_checks"}),
        ("account_head_classification", "Account-head Classification", {"account_head"}),
    ]
    result = []
    for key, label, detail_keys in groups:
        checks = [gate for gate in gates if gate["key"] in detail_keys]
        statuses = {gate["status"] for gate in checks}
        status = "fail" if "fail" in statuses else ("not_applicable" if checks and statuses == {"not_applicable"} else "pass")
        result.append({"key": key, "label": label, "status": status, "checks": checks})
    result.append({"key": "observation", "label": "Observation", "status": "pass", "checks": [], "detail": "Observation text is recorded separately and does not create a new blocking rule."})
    return result


def _audit_workbook_resolve_account_names(client_id: int, account_codes: set) -> Dict[str, Optional[str]]:
    """
    Same "latest parsed Trial Balance" lookup pattern as _audit_workbook_resolve_categories()
    (and get_category_for_account()) - batched once per distinct account code in a session's
    line set, not once per line. No account_category_assignments-style fallback: that table
    carries no name column, only category_id, so a code absent from the latest TB simply
    resolves to None.

    Returns {account_code: account_name or None}, every requested code present in the result.
    """
    resolved: Dict[str, Optional[str]] = {code: None for code in account_codes}
    if not account_codes:
        return resolved

    codes = list(account_codes)
    placeholders = ",".join("%s" for _ in codes)

    upload_rows = execute_query_pg(
        """
        SELECT id FROM trial_balance_uploads
        WHERE client_id = %s AND status = 'parsed'
        ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC
        LIMIT 1
        """,
        (client_id,)
    )
    if upload_rows:
        tb_rows = execute_query_pg(
            f"""
            SELECT account_code, account_name
            FROM trial_balance_accounts
            WHERE upload_id = %s AND account_code IN ({placeholders})
            """,
            tuple([upload_rows[0]['id']] + codes)
        )
        for r in tb_rows:
            resolved[r['account_code']] = r['account_name']

    return resolved


def _audit_workbook_resolve_category_names(category_ids: set) -> Dict[int, Optional[str]]:
    """
    Batched category_id -> name lookup for get_audit_workbook() (Procedures-pill wiring phase) -
    same batching style as _audit_workbook_resolve_account_names(). The endpoint already resolves
    category_id internally (_audit_workbook_resolve_categories()) to compute the procedures field,
    but never surfaced the name - a real requirement for a caller (Audit Workbook's own procedure
    modal adapter) that needs to call GET /api/verification/audit_guidance?transaction_type=<name>,
    which is keyed by name, not id.

    Returns {category_id: name or None} for every id passed in - a soft-deleted category still
    resolves to its name here (display-context convention, matches get_category_for_account()).
    """
    resolved: Dict[int, Optional[str]] = {cid: None for cid in category_ids}
    if not category_ids:
        return resolved
    ids = list(category_ids)
    placeholders = ",".join("%s" for _ in ids)
    rows = execute_query_pg(
        f"SELECT id, name FROM audit_rule_categories WHERE id IN ({placeholders})",
        tuple(ids)
    )
    for r in rows:
        resolved[r['id']] = r['name']
    return resolved


@router.get("/api/verification/session/{session_id}/audit_workbook")
def get_audit_workbook(session_id: int):
    """
    Audit Workbook Phase 1 of 5 (per-voucher, original) / Phase 1 of a confirmed 3-phase
    line-grain restructure (this version): read-only rows for a session's Audit Workbook list
    view, now one row per ACCOUNTING LINE grouped under its voucher, rather than one row per
    voucher - a voucher's Sr number, date, and rollup amount/status live on the voucher GROUP
    object, not repeated per line. Same session scope/grouping as get_voucher_stats()
    (transaction_ref, not the _doc_group_id line-navigation grouping), so this endpoint's
    per-voucher status still agrees with that one exactly.

    Per line:
    - account_code / description: resolved via ACCOUNT_CODE_ALIASES / DESCRIPTION_ALIASES
      against that line's own row_data (same aliases used elsewhere in this file).
    - account_name: resolved from the client's latest parsed Trial Balance by account_code -
      same "latest parsed upload" lookup pattern as get_category_for_account()/
      _audit_workbook_resolve_categories(). None when the code isn't in the latest TB.
    - category_name (Procedures-pill wiring phase): the line's resolved Category NAME (the
      category_id already resolved internally to compute `procedures` below, just also
      surfaced by name here) - None when the account code doesn't resolve to a Category. Needed
      by Audit Workbook's own procedure-modal adapter, which must supply a Category name (not
      id) to open the shared modal and to call GET /api/verification/audit_guidance.
    - amount: client_data.extract_amount (already the resolved Debit/Credit-side value - see
      the 2026-08-09 extract_amount backfill fix).
    - status: this line's own cleared_status verbatim, None mapped to "Pending" - the same
      three-way vocabulary _rollup_voucher_status() already establishes for "not yet computed".
    - procedures: {"done", "total"} over the line's resolved Category's active attached
      Documents, using the SAME per-document-type "done" rule static/verification.js's
      isDocDone() applies for the main grid's Procedures badge (physical_bill exact-amount
      match / template-linked evidence presence / no-template role ticks, each additionally
      gated on voucher-level checks when transaction_level_approval is set) - so this column
      can never disagree with what the auditor already sees for the same line in Verification.
      {"done": 0, "total": 0} for a line with no resolved Category.
    - observations: {"count", "entries": [{document_id, document_name, text}]} - every
      voucher_line_observations entry for the line with non-blank text, same filter
      updateObservationsCellForLine()'s nonEmpty already applies.

    Per voucher group: sr (1-based, first-appearance/chronological order), voucher_no, date
    (first line's extract_date), line_count, amount (sum of its lines' amount), status
    (_rollup_voucher_status() over its lines' cleared_status) - amount/status here ARE the
    subtotal/rollup values a later frontend phase renders as a subtotal row, only when
    line_count > 1; deliberately not duplicated under a separate "subtotal" key.

    Every lookup below is batched once for the whole session (not per line) - a session can have
    300+ lines, and this mirrors get_voucher_line_documents()/get_procedure_checks()/
    get_voucher_line_physical_bills()/get_line_observations()'s own sibling-resolution scope
    (client_data_id, not the per-session voucher_line_id), just computed for every line in the
    session in one pass instead of resolved one line at a time.

    Top-level response also carries client_id (Procedures-pill wiring phase) - already loaded
    below for the session lookup, just also returned. Needed by Audit Workbook's own procedure-
    modal adapter for client-scoped calls inside the modal (Trial Balance leaf-account lookup,
    Data Bank search/browse) - the session's real client, not whatever the global navbar client
    picker happens to have selected at the time, which can silently disagree.
    """
    session = execute_query_pg(
        "SELECT id, client_id FROM verification_sessions WHERE id = %s AND status != 'Cancelled'",
        (session_id,)
    )
    if not session:
        raise HTTPException(404, "Session not found or cancelled.")
    client_id = session[0]['client_id']

    rows = execute_query_pg(
        """
        SELECT vd.id AS detail_id, vd.transaction_ref, vd.cleared_status,
               vd.category_id_applied, vd.category_name_applied,
               cd.id AS client_data_id, cd.extract_amount, cd.extract_date, cd.row_data
        FROM verification_details vd
        JOIN client_data cd ON vd.client_data_id = cd.id
        WHERE vd.session_id = %s AND vd.range_status = 'active'
        ORDER BY cd.id ASC
        """,
        (session_id,)
    )

    if not rows:
        return {"total_vouchers": 0, "total_lines": 0, "client_id": client_id, "vouchers": []}

    # Parse row_data / resolve account_code+description once per line, up front - everything
    # below keys off account_code (category resolution) or client_data_id (the sibling-scope
    # every per-line lookup below is batched against).
    parsed_lines = []
    account_codes = set()
    client_data_ids = set()
    transaction_refs = set()
    for r in rows:
        row_data = r['row_data']
        try:
            row_data = json.loads(row_data) if isinstance(row_data, str) else (row_data or {})
        except Exception:
            row_data = {}
        account_code = get_account_code_value(row_data)
        description = get_column_value(row_data, DESCRIPTION_ALIASES)
        parsed_lines.append({
            "detail_id": r['detail_id'],
            "transaction_ref": r['transaction_ref'],
            "cleared_status": r['cleared_status'],
            "category_id_applied": r['category_id_applied'],
            "category_name_applied": r['category_name_applied'],
            "client_data_id": r['client_data_id'],
            "extract_amount": r['extract_amount'] or 0,
            "extract_date": r['extract_date'],
            "account_code": account_code,
            "description": description,
        })
        if account_code:
            account_codes.add(account_code)
        client_data_ids.add(r['client_data_id'])
        if r['transaction_ref']:
            transaction_refs.add(r['transaction_ref'])

    category_id_by_account_code = _audit_workbook_resolve_categories(client_id, account_codes)
    account_name_by_code = _audit_workbook_resolve_account_names(client_id, account_codes)

    # guidance_docs_by_category: fetched once per DISTINCT category_id actually in use this
    # session, not once per line.
    distinct_category_ids = {cid for cid in category_id_by_account_code.values() if cid}
    guidance_docs_by_category: Dict[int, list] = {
        cid: get_category_guidance_documents(cid) for cid in distinct_category_ids
    }
    category_name_by_id = _audit_workbook_resolve_category_names(distinct_category_ids)

    cd_placeholders = ",".join("%s" for _ in client_data_ids)

    # Linked evidence: {client_data_id: {document_id, ...}}
    linked_doc_ids_by_cd: Dict[int, set] = {}
    linked_rows = execute_query_pg(
        f"""
        SELECT vd2.client_data_id, vld.document_id
        FROM voucher_line_documents vld
        JOIN verification_details vd2 ON vd2.id = vld.voucher_line_id
        WHERE vd2.client_data_id IN ({cd_placeholders}) AND vld.status = 'active'
        """,
        tuple(client_data_ids)
    )
    for r in linked_rows:
        linked_doc_ids_by_cd.setdefault(r['client_data_id'], set()).add(r['document_id'])

    # Procedure-check ticks: {client_data_id: {document_id: {role_name: bool}}}, deduped by
    # latest checked_at per (client_data_id, document_id, role_name) - same rule
    # compute_and_persist_cleared_status()'s role_ticks_by_doc already uses per line.
    role_ticks_by_cd: Dict[int, Dict[int, Dict[str, bool]]] = {}
    tick_rows = execute_query_pg(
        f"""
        SELECT vd2.client_data_id, vpc.document_id, vpc.role_name, vpc.is_checked, vpc.checked_at
        FROM voucher_line_procedure_checks vpc
        JOIN verification_details vd2 ON vd2.id = vpc.voucher_line_id
        WHERE vd2.client_data_id IN ({cd_placeholders})
        """,
        tuple(client_data_ids)
    )
    latest_tick: Dict[tuple, dict] = {}
    for r in tick_rows:
        key = (r['client_data_id'], r['document_id'], r['role_name'])
        existing = latest_tick.get(key)
        if existing is None or _ts_sort_key(r['checked_at']) > _ts_sort_key(existing['checked_at']):
            latest_tick[key] = r
    for (cd_id, doc_id, role_name), r in latest_tick.items():
        role_ticks_by_cd.setdefault(cd_id, {}).setdefault(doc_id, {})[role_name] = bool(r['is_checked'])

    # Physical bills (Multi-Bill/Multi-Line Physical Bill, Phase 2 of 6): {client_data_id:
    # {document_id: [bill, ...]}}, each bill a lightweight {"bill_no", "bill_date",
    # "vendor_name", "items": [<raw amount>, ...]} dict ready for _evaluate_physical_bill_
    # completion(). A document can now have several bills, so this dedups at the whole-bill-SET
    # level, not per-row: the winning voucher_line_id per (client_data_id, document_id) is
    # whichever sibling session's bills for that document have the max(updated_at) among the
    # header rows - same "latest-touched bill SET wins" rule get_voucher_line_physical_bills()
    # uses - then only that session's bills/items are kept, never a mix of two sessions' bills
    # under one document.
    bills_by_cd: Dict[int, Dict[int, list]] = {}
    header_rows = execute_query_pg(
        f"""
        SELECT vd2.client_data_id, vpb.id, vpb.voucher_line_id, vpb.document_id,
               vpb.bill_no, vpb.bill_date, vpb.vendor_name, vpb.updated_at
        FROM voucher_line_physical_bills vpb
        JOIN verification_details vd2 ON vd2.id = vpb.voucher_line_id
        WHERE vd2.client_data_id IN ({cd_placeholders})
        """,
        tuple(client_data_ids)
    )
    winning_session_by_key: Dict[tuple, tuple] = {}
    for r in header_rows:
        key = (r['client_data_id'], r['document_id'])
        candidate = (_ts_sort_key(r['updated_at']), r['voucher_line_id'])
        existing = winning_session_by_key.get(key)
        if existing is None or candidate > existing:
            winning_session_by_key[key] = candidate

    winning_bill_ids = []
    bill_shell_by_id: Dict[int, dict] = {}
    for r in header_rows:
        key = (r['client_data_id'], r['document_id'])
        if r['voucher_line_id'] != winning_session_by_key[key][1]:
            continue
        shell = {"bill_no": r['bill_no'], "bill_date": r['bill_date'], "vendor_name": r['vendor_name'], "items": []}
        bill_shell_by_id[r['id']] = shell
        winning_bill_ids.append(r['id'])
        bills_by_cd.setdefault(r['client_data_id'], {}).setdefault(r['document_id'], []).append(shell)

    if winning_bill_ids:
        bid_placeholders = ",".join("%s" for _ in winning_bill_ids)
        item_rows = execute_query_pg(
            f"SELECT bill_id, amount FROM voucher_line_physical_bill_items WHERE bill_id IN ({bid_placeholders})",
            tuple(winning_bill_ids)
        )
        for r in item_rows:
            bill_shell_by_id[r['bill_id']]['items'].append(r['amount'])

    # transaction_level_approval gate: {transaction_ref: bool}, vacuously True for every ref
    # when zero voucher-level checks are configured - mirrors check #2's voucher_checks_ok.
    voucher_checks_ok_by_ref: Dict[str, bool] = {}
    if transaction_refs:
        active_checks = execute_query_pg("SELECT id FROM audit_rule_voucher_checks WHERE is_active = TRUE")
        active_check_ids = {c['id'] for c in active_checks}
        if not active_check_ids:
            voucher_checks_ok_by_ref = {ref: True for ref in transaction_refs}
        else:
            ref_placeholders = ",".join("%s" for _ in transaction_refs)
            ticked_rows = execute_query_pg(
                f"""
                SELECT transaction_ref, check_id FROM voucher_check_status
                WHERE client_id = %s AND state = 'done' AND transaction_ref IN ({ref_placeholders})
                """,
                tuple([client_id] + list(transaction_refs))
            )
            ticked_by_ref: Dict[str, set] = {}
            for r in ticked_rows:
                ticked_by_ref.setdefault(r['transaction_ref'], set()).add(r['check_id'])
            voucher_checks_ok_by_ref = {
                ref: active_check_ids.issubset(ticked_by_ref.get(ref, set()))
                for ref in transaction_refs
            }

    # Observations: {client_data_id: [{document_id, document_name, text}]}, deduped by latest
    # updated_at per (client_data_id, document_id), non-blank text only - same rule
    # updateObservationsCellForLine()'s nonEmpty filter already applies.
    observations_by_cd: Dict[int, list] = {}
    obs_rows = execute_query_pg(
        f"""
        SELECT vd2.client_data_id, vlo.document_id, vlo.observation_text, vlo.updated_at,
               ard.name AS document_name
        FROM voucher_line_observations vlo
        JOIN verification_details vd2 ON vd2.id = vlo.voucher_line_id
        LEFT JOIN audit_rule_documents ard ON ard.id = vlo.document_id
        WHERE vd2.client_data_id IN ({cd_placeholders})
        """,
        tuple(client_data_ids)
    )
    latest_obs: Dict[tuple, dict] = {}
    for r in obs_rows:
        key = (r['client_data_id'], r['document_id'])
        existing = latest_obs.get(key)
        if existing is None or _ts_sort_key(r['updated_at']) > _ts_sort_key(existing['updated_at']):
            latest_obs[key] = r
    for (cd_id, doc_id), r in sorted(latest_obs.items(), key=lambda kv: kv[0][1]):
        text = (r['observation_text'] or '').strip()
        if not text:
            continue
        observations_by_cd.setdefault(cd_id, []).append({
            "document_id": doc_id,
            "document_name": r['document_name'],
            "text": text,
        })

    def _compute_line_procedures(client_data_id: int, category_id: Optional[int],
                                  transaction_ref: Optional[str], line_amount) -> dict:
        if not category_id:
            return {"done": 0, "total": 0}
        docs = guidance_docs_by_category.get(category_id) or []
        if not docs:
            return {"done": 0, "total": 0}
        linked = linked_doc_ids_by_cd.get(client_data_id, set())
        ticks = role_ticks_by_cd.get(client_data_id, {})
        bills = bills_by_cd.get(client_data_id, {})
        voucher_amount = _cross_check_numeric(line_amount)

        done = 0
        for doc in docs:
            if doc['document_type'] == 'physical_bill':
                base = _evaluate_physical_bill_completion(bills.get(doc['id'], []), voucher_amount)
            elif doc['template_id']:
                base = doc['id'] in linked
            else:
                roles = doc['auth_roles'] or []
                doc_ticks = ticks.get(doc['id'], {})
                base = bool(roles) and all(doc_ticks.get(role) for role in roles)
            if base and doc['transaction_level_approval']:
                base = voucher_checks_ok_by_ref.get(transaction_ref, True)
            if base:
                done += 1
        return {"done": done, "total": len(docs)}

    groups: Dict[str, dict] = {}
    sr_counter = 0
    total_lines = 0
    for pl in parsed_lines:
        ref = pl['transaction_ref']
        if not ref:
            continue
        if ref not in groups:
            sr_counter += 1
            groups[ref] = {
                "sr": sr_counter,
                "voucher_no": ref,
                "date": pl['extract_date'],
                "line_count": 0,
                "amount": 0.0,
                "_cleared_statuses": [],
                "lines": [],
            }
        g = groups[ref]
        # Freeze Category on voucher lines: a frozen line's category_id_applied always wins over
        # the batched live resolution above, so this view can't disagree with an already-audited
        # line's frozen category. category_name_applied is used directly rather than
        # category_name_by_id.get(category_id) - a frozen category might not be in
        # distinct_category_ids (derived only from LIVE category_id_by_account_code values,
        # above), which would otherwise wrongly resolve the name to None.
        if pl['category_id_applied'] is not None:
            category_id = pl['category_id_applied']
            category_name = pl['category_name_applied']
        else:
            category_id = category_id_by_account_code.get(pl['account_code']) if pl['account_code'] else None
            category_name = category_name_by_id.get(category_id) if category_id else None
        line_status = pl['cleared_status'] or "Pending"
        entries = observations_by_cd.get(pl['client_data_id'], [])
        g["lines"].append({
            "detail_id": pl['detail_id'],
            "client_data_id": pl['client_data_id'],
            "account_code": pl['account_code'],
            "account_name": account_name_by_code.get(pl['account_code']) if pl['account_code'] else None,
            "category_name": category_name,
            "description": pl['description'],
            "amount": pl['extract_amount'],
            "status": line_status,
            "procedures": _compute_line_procedures(
                pl['client_data_id'], category_id, ref, pl['extract_amount']
            ),
            "observations": {"count": len(entries), "entries": entries},
        })
        g["line_count"] += 1
        g["amount"] += pl['extract_amount']
        g["_cleared_statuses"].append(pl['cleared_status'])
        total_lines += 1

    vouchers = []
    for g in sorted(groups.values(), key=lambda v: v['sr']):
        g["status"] = _rollup_voucher_status(g.pop("_cleared_statuses"))
        vouchers.append(g)

    return {"total_vouchers": len(vouchers), "total_lines": total_lines, "client_id": client_id, "vouchers": vouchers}


@router.post("/api/verification/session/{session_id}/reconcile_range")
def reconcile_session_range(
    session_id: int,
    period_start: str = Query(..., description="New start date (YYYY-MM-DD)"),
    period_end: str = Query(..., description="New end date (YYYY-MM-DD)"),
    created_by: str = "Analyst"
):
    """
    Re-resolves an already-active session's voucher list against a new date range (Place 2's
    date-range picker, edited mid-session) - a reconcile, not a new session start. Nothing is
    ever deleted: a voucher that falls out of the new range has its verification_details
    row(s) soft-excluded (range_status='excluded', excluded_at stamped) rather than removed,
    so its line_items/procedure_data/cleared_status survive intact if the range is later
    widened back to include it again (reactivation restores the exact same row, never a
    duplicate). A voucher newly in range that was never in this session before gets a fresh
    Pending row, exactly like start_verification_session() creates on a first session start.

    range_status is what actually keeps an excluded row out of the active voucher list -
    _fetch_session_details_grouped()/get_audited_cashbook()/get_voucher_stats() all filter to
    range_status='active', so this endpoint only needs to flip that flag, not touch those
    endpoints' own logic further.
    """
    session = execute_query_pg(
        "SELECT id, client_id, cashbook_name FROM verification_sessions WHERE id = %s AND status != 'Cancelled'",
        (session_id,)
    )
    if not session:
        raise HTTPException(404, "Verification session not found or cancelled.")
    s = session[0]
    client_id = s['client_id']
    cashbook_name = s['cashbook_name']

    try:
        datetime.fromisoformat(period_start)
        datetime.fromisoformat(period_end)
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")
    if period_start > period_end:
        raise HTTPException(400, "Start date must be before or equal to end date.")

    # --- Re-run the same voucher-discovery logic as start_verification_session(), against the
    # session's existing client_id/cashbook_name but the new date range. Deliberately duplicated
    # rather than shared via a helper, to keep this addition purely additive - see the plan note.
    client_prefixes = get_client_voucher_prefixes_pg(client_id)
    rows = execute_query_pg(
        """
        SELECT id, row_data, extract_date, extract_amount
        FROM client_data
        WHERE client_id = %s AND is_deleted = FALSE AND change_detection_status != 'deleted'
          AND extract_date BETWEEN %s AND %s
        ORDER BY extract_date
        """,
        (client_id, period_start, period_end)
    )

    voucher_aliases = ['Voucher ID', 'Voucher No', 'Voucher Number', 'Voucher', 'Invoice No', 'Invoice Number']
    account_aliases = ['Account Name', 'Account', 'Account Detail', 'Account Code', 'Ledger', 'Head']
    debit_aliases = ['Debit', 'Debit Amount', 'Dr', 'Dr Amount', 'Debit (Dr)', 'Dr. Amount']
    credit_aliases = ['Credit', 'Credit Amount', 'Cr', 'Cr Amount', 'Credit (Cr)', 'Cr. Amount']

    def get_col_value(row_data, aliases):
        for alias in aliases:
            if alias in row_data:
                val = row_data[alias]
                if val is not None and val != '':
                    return val
        return None

    vouchers = {}
    for row in rows:
        try:
            rd = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
            if not isinstance(rd, dict):
                continue
            voucher_id = get_col_value(rd, voucher_aliases)
            if not voucher_id:
                continue
            category = match_voucher_category(voucher_id, client_prefixes)
            if category not in ('Cash Payment', 'Cash Receipt', 'Bank Payment', 'Bank Receipt'):
                continue
            debit_str = get_col_value(rd, debit_aliases)
            credit_str = get_col_value(rd, credit_aliases)
            debit = float(debit_str) if debit_str else 0.0
            credit = float(credit_str) if credit_str else 0.0
            account_name = get_col_value(rd, account_aliases)
            if not account_name:
                continue
            if voucher_id not in vouchers:
                vouchers[voucher_id] = {'category': category, 'rows': []}
            vouchers[voucher_id]['rows'].append({
                'row_id': row['id'],
                'account_name': account_name,
                'debit': debit,
                'credit': credit,
            })
        except:
            continue

    target: Dict[int, str] = {}  # client_data_id -> transaction_ref
    for vid, data in vouchers.items():
        cashbook_row = next((r for r in data['rows'] if r['account_name'] == cashbook_name and is_cashbook_leg_row(r, data['category'])), None)
        if cashbook_row is None:
            continue
        vtype = voucher_type_label(data['category'])
        if vtype is None:
            continue
        for row in data['rows']:
            if vtype in ('CPV', 'BPV') and row['debit'] > 0 and row['credit'] == 0:
                target[row['row_id']] = vid
            elif vtype in ('CRV', 'BRV') and row['credit'] > 0 and row['debit'] == 0:
                target[row['row_id']] = vid

    target_ids = set(target.keys())

    # --- Existing verification_details rows for this session, regardless of range_status.
    existing = execute_query_pg(
        "SELECT id, client_data_id, range_status FROM verification_details WHERE session_id = %s",
        (session_id,)
    )
    existing_by_client_data_id = {r['client_data_id']: r for r in existing}
    existing_ids_all = set(existing_by_client_data_id.keys())

    new_client_data_ids = target_ids - existing_ids_all
    reactivate_ids = [
        cdid for cdid in (target_ids & existing_ids_all)
        if existing_by_client_data_id[cdid]['range_status'] == 'excluded'
    ]
    exclude_detail_rows = [
        r for r in existing
        if r['range_status'] == 'active' and r['client_data_id'] not in target_ids
    ]

    # --- New rows: insert, then seed cleared_status from a prior sibling session exactly like
    # start_verification_session() does - scoped to only the freshly inserted rows, since (unlike
    # a brand-new session) this session may already have active rows whose own cleared_status
    # must not be clobbered by a sibling-session copy.
    added_count = 0
    if new_client_data_ids:
        detail_params = [(session_id, cdid, target[cdid]) for cdid in new_client_data_ids]
        conn = get_raw_conn_pg()
        try:
            cur = conn.cursor()
            cur.executemany(
                """
                INSERT INTO verification_details (session_id, client_data_id, transaction_ref, status, validation_status, range_status)
                VALUES (%s, %s, %s, 'Pending', 'Pending', 'active')
                """,
                detail_params
            )
            conn.commit()
        finally:
            release_raw_conn_pg(conn)
        added_count = len(new_client_data_ids)

        placeholders = ",".join(["%s"] * len(new_client_data_ids))
        new_rows = execute_query_pg(
            f"""
            SELECT id FROM verification_details
            WHERE session_id = %s AND range_status = 'active' AND client_data_id IN ({placeholders})
            """,
            (session_id, *new_client_data_ids)
        )
        new_detail_ids = [r['id'] for r in new_rows]
        if new_detail_ids:
            id_placeholders = ",".join(["%s"] * len(new_detail_ids))
            execute_write_pg(
                f"""
                UPDATE verification_details
                SET cleared_status = (
                        SELECT vd2.cleared_status FROM verification_details vd2
                        WHERE vd2.client_data_id = verification_details.client_data_id
                          AND vd2.session_id != %s AND vd2.cleared_status IS NOT NULL
                        ORDER BY vd2.id DESC LIMIT 1
                    ),
                    matrix_version_applied = (
                        SELECT vd2.matrix_version_applied FROM verification_details vd2
                        WHERE vd2.client_data_id = verification_details.client_data_id
                          AND vd2.session_id != %s AND vd2.cleared_status IS NOT NULL
                        ORDER BY vd2.id DESC LIMIT 1
                    )
                WHERE id IN ({id_placeholders})
                  AND EXISTS (
                        SELECT 1 FROM verification_details vd2
                        WHERE vd2.client_data_id = verification_details.client_data_id
                          AND vd2.session_id != %s AND vd2.cleared_status IS NOT NULL
                    )
                """,
                (session_id, session_id, *new_detail_ids, session_id)
            )

    # --- Reactivate rows that fell out of range before and are now back in.
    reactivated_count = 0
    if reactivate_ids:
        placeholders = ",".join(["%s"] * len(reactivate_ids))
        execute_write_pg(
            f"""
            UPDATE verification_details
            SET range_status = 'active', excluded_at = NULL
            WHERE session_id = %s AND client_data_id IN ({placeholders})
            """,
            (session_id, *reactivate_ids)
        )
        reactivated_count = len(reactivate_ids)

    # --- Exclude rows that are no longer in range.
    excluded_detail_ids = [r['id'] for r in exclude_detail_rows]
    if excluded_detail_ids:
        placeholders = ",".join(["%s"] * len(excluded_detail_ids))
        execute_write_pg(
            f"""
            UPDATE verification_details
            SET range_status = 'excluded', excluded_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
            """,
            tuple(excluded_detail_ids)
        )

    total_active = execute_query_pg(
        "SELECT COUNT(*) as c FROM verification_details WHERE session_id = %s AND range_status = 'active'",
        (session_id,)
    )[0]['c']

    execute_write_pg(
        "UPDATE verification_sessions SET period_start = %s, period_end = %s, total_transactions = %s WHERE id = %s",
        (period_start, period_end, total_active, session_id)
    )

    log_audit(
        created_by, "Reconcile Session Range", "Verification",
        f"Session {session_id}: range set to {period_start}..{period_end} - "
        f"{added_count} added, {reactivated_count} reactivated, {len(excluded_detail_ids)} excluded, {total_active} active"
    )

    return {
        "session_id": session_id,
        "period_start": period_start,
        "period_end": period_end,
        "added_count": added_count,
        "reactivated_count": reactivated_count,
        "excluded_count": len(excluded_detail_ids),
        "total_active": total_active,
        "excluded_detail_ids": excluded_detail_ids
    }


@router.get("/api/verification/print_sheet/{session_id}")
def get_print_data(
    session_id: int,
    sheet_type: str = Query("verification", description="Type: 'verification' or 'observation'")
):
    """
    Returns data specifically formatted for printing.
    For 'verification': full sheet with all transactions and details.
    For 'observation': only transactions with observations or comments.
    """
    # Get audited data
    audited = get_audited_cashbook(session_id)
    transactions = audited["transactions"]
    session_info = audited["session"]
    
    # Filter if observation sheet
    if sheet_type == "observation":
        transactions = [t for t in transactions if t.get('observations') or t.get('comments')]
    
    return {
        "session_info": session_info,
        "transactions": transactions,
        "sheet_type": sheet_type,
        "total_count": len(transactions)
    }

@router.get("/api/verification/export_excel/{session_id}")
def export_audited_cashbook_excel(session_id: int):
    """
    Export the audited cashbook to an Excel file.
    """
    # Get data
    audited = get_audited_cashbook(session_id)
    session_info = audited["session"]
    transactions = audited["transactions"]
    
    if not transactions:
        raise HTTPException(400, "No transactions to export.")
    
    # Prepare data for DataFrame
    rows = []
    for t in transactions:
        # Extract common fields from row_data
        rd = t.get('row_data', {})
        row = {
            "Voucher ID": t.get('voucher_id', ''),
            "Date": t.get('extract_date', ''),
            "Description": rd.get('Description', ''),
            "Account Code": rd.get('Account Code', ''),
            "Account Name": rd.get('Account Name', ''),
            "Amount": t.get('extract_amount', 0),
            "Vendor": "",
            "Unit": "",
            "Qty": "",
            "Rate": "",
            "Calculated Amount": "",
            "Purchase Demand": "",
            "Purchase Order": "",
            "Gate Pass": "",
            "Store Receiving": "",
            "Vendor Bill": "",
            "Observations": t.get('observations', ''),
            "Comments": t.get('comments', ''),
            "Validation Status": t.get('validation_status', ''),
            "Verification Status": t.get('verification_status', ''),
            "Verified By": t.get('verified_by', ''),
            # verified_at is TIMESTAMPTZ (tz-aware) now - Excel/openpyxl rejects a tz-aware
            # datetime outright, so it's formatted to a plain string here rather than passed
            # through raw.
            "Verified At": t['verified_at'].strftime('%d/%m/%Y %H:%M') if t.get('verified_at') else ''
        }
        # If there are line items, we could expand them; but for simplicity we'll put the first line item's data
        line_items = t.get('line_items', [])
        if line_items:
            first = line_items[0]
            row.update({
                "Vendor": first.get('vendor', ''),
                "Unit": first.get('unit', ''),
                "Qty": first.get('qty', ''),
                "Rate": first.get('rate', ''),
                "Calculated Amount": first.get('calculated_amount', '')
            })
        # Procedure flags
        proc = t.get('procedure_flags', {})
        row.update({
            "Purchase Demand": "✅" if proc.get('purchase_demand') else "",
            "Purchase Order": "✅" if proc.get('purchase_order') else "",
            "Gate Pass": "✅" if proc.get('gate_pass') else "",
            "Store Receiving": "✅" if proc.get('store_receiving') else "",
            "Vendor Bill": "✅" if proc.get('vendor_bill') else "",
        })
        rows.append(row)
    
    df = pd.DataFrame(rows)
    
    # Create Excel in memory
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Audited Cashbook', index=False)
        # Format headers
        workbook = writer.book
        worksheet = writer.sheets['Audited Cashbook']
        # Bold headers
        for cell in worksheet[1]:
            cell.font = Font(bold=True)
        # Auto-adjust column widths
        for col in worksheet.columns:
            max_length = 0
            column = col[0].column_letter
            for cell in col:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            worksheet.column_dimensions[column].width = adjusted_width
    
    output.seek(0)
    filename = f"Audited_Cashbook_{session_info['cashbook_name']}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ==============================================
# AUDIT GUIDANCE LOOKUP (Phase 9: sourced from Audit Rules Categories)
# ==============================================

@router.get("/api/verification/audit_guidance")
def get_audit_guidance(
    # Same query param name/URL as before this phase (kept for frontend-contract stability) -
    # the value is now a Category name, matched against the live Type dropdown built from
    # GET /api/audit-rules/categories, not a fixed transaction_type string against the old
    # audit_procedure_library table (left in place, unused - see database.py's migration
    # note and 2. Python App/CLAUDE.md's Conventions entry for this phase).
    transaction_type: str = Query(..., description="Category name to look up (must match the Type dropdown's values)")
):
    """
    Look up audit guidance (KB overview, attached Documents, attached Cross-checks) for a
    Category. Cross-checks are surfaced as read-only guidance text only in this phase -
    automated comparison against real voucher data is a separate, deferred requirement.

    Read-side cache wiring (Modal Caching Phase 3, sub-phase 2): checks the shared
    process-level cache (audit_rules.py's cache_get/cache_set) before hitting Postgres,
    keyed on transaction_type (the Category name) - global, not per-client, since Category
    guidance content doesn't vary by client. Invalidated on any Category/Document/
    Cross-check write via audit_rules.py's existing invalidate_audit_rules_cache() hooks.
    """
    cache_key = f"guidance:{transaction_type}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    rows = execute_query_pg(
        "SELECT id, kb_overview, matrix_version FROM audit_rule_categories WHERE name = %s AND is_active = TRUE",
        (transaction_type,)
    )
    if not rows:
        raise HTTPException(404, f"No audit guidance found for category '{transaction_type}'.")
    category_id = rows[0]['id']
    result = {
        "transaction_type": transaction_type,
        "category_id": category_id,
        "kb_overview": rows[0]['kb_overview'],
        # Session-level ref-data caching, Phase 2 of 4: exposed so the frontend cache
        # (auditGuidanceCache, verification.js) can carry this Category's version alongside
        # its cached guidance - see that file's fetchAuditGuidanceData() for how it's used.
        "matrix_version": rows[0]['matrix_version'],
        "documents": get_category_guidance_documents(category_id),
        "cross_checks": get_category_guidance_cross_checks(category_id)
    }
    cache_set(cache_key, result)
    return result

# ==============================================
# VERIFICATION ↔ ASSIGN CATEGORIES LINK (Phase 3 of 5)
# Read-only lookup against Phase 1's persistent account_category_assignments
# table (client_id + account_code -> category_id), written only by Assign
# Categories (routers/trial_balance.py's assign_trial_balance_category()).
# Verification never writes to this table - this endpoint is the read side
# a later phase will use to auto-fill the per-line Type dropdown.
# ==============================================

@router.get("/api/verification/category-for-account")
def get_category_for_account(
    client_id: int = Query(..., description="Client ID"),
    account_code: str = Query(..., description="Account code to look up")
):
    """
    Look up the Audit Rules Category assigned (via Assign Categories) to a
    given client + account code. Returns a clean "no match" shape rather than
    a 404 when nothing is assigned - not having a mapping yet is a normal,
    expected state, not an error.

    This is a read-only display lookup (feeds Verification's per-line Type
    auto-fill), not a selection UI - so a Category since deactivated is still
    returned as a real match rather than degraded to "no match". Freezing a
    since-deactivated assignment's visibility this way mirrors the same
    "display contexts keep showing an already-referenced inactive item"
    convention used elsewhere in this codebase (e.g. the Supporting Documents
    modal's locked tab for an already-referenced inactive Document).

    Read-side cache wiring (category-for-account cache, Phase 3): goes through the same
    process-level cache get_audit_guidance() reads through, but keyed per-client via
    category_for_account_cache_key() - unlike guidance, this lookup's data is genuinely
    per-client. Invalidated by routers/trial_balance.py on TB upload and category assignment.

    The cache is consulted BEFORE the client-existence check (F3a fix): on this
    Render<->Supabase deployment every DB call pays a large cross-region latency floor, so
    running the existence SELECT first meant a cache hit still cost a full round-trip - most
    of what the cache was built to avoid. A cached entry can only have been written by a
    prior call that already passed the existence check, and the TTL +
    invalidate_category_for_account_cache() hooks bound staleness, so skipping the check on a
    hit is safe. A cache miss still runs the existence check exactly as before.
    """
    cache_key = category_for_account_cache_key(client_id, account_code)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    # Prefer the client's current (latest parsed) Trial Balance's own category_id for this
    # account_code - this is what Assign Categories itself shows/edits, and is the "correct
    # type from the client's Trial Balance" a user expects here. account_category_assignments
    # (below) is a persisted mirror meant to carry a categorization forward across TB
    # re-uploads, kept in sync by upsert_account_category_assignment() - but it's a separate
    # cached copy, not the live TB, and confirmed (2026-07-30) to be able to drift stale
    # relative to it. So the live TB wins whenever the account is present in it; the
    # assignments table is used only as a fallback for an account not in the latest TB.
    upload_rows = execute_query_pg(
        """
        SELECT id FROM trial_balance_uploads
        WHERE client_id = %s AND status = 'parsed'
        ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC
        LIMIT 1
        """,
        (client_id,)
    )
    if upload_rows:
        tb_rows = execute_query_pg(
            """
            SELECT a.category_id, c.name AS category_name
            FROM trial_balance_accounts a
            LEFT JOIN audit_rule_categories c ON c.id = a.category_id
            WHERE a.upload_id = %s AND a.account_code = %s
            """,
            (upload_rows[0]['id'], account_code)
        )
        if tb_rows and tb_rows[0]['category_name']:
            result = {
                "matched": True,
                "category_id": tb_rows[0]['category_id'],
                "category_name": tb_rows[0]['category_name']
            }
            cache_set(cache_key, result)
            return result

    # LEFT JOIN means category_name comes back NULL for every genuine
    # "no match" case in one query: no assignment row at all, or an
    # assignment explicitly cleared to NULL. A category's own is_active
    # state is deliberately NOT folded into this join - a since-deactivated
    # category is still a real, displayable match here (see docstring).
    rows = execute_query_pg(
        """
        SELECT aca.category_id, arc.name AS category_name
        FROM account_category_assignments aca
        LEFT JOIN audit_rule_categories arc
            ON arc.id = aca.category_id
        WHERE aca.client_id = %s AND aca.account_code = %s
        """,
        (client_id, account_code)
    )

    if not rows or not rows[0]['category_name']:
        result = {"matched": False, "category_id": None, "category_name": None}
        cache_set(cache_key, result)
        return result

    result = {
        "matched": True,
        "category_id": rows[0]['category_id'],
        "category_name": rows[0]['category_name']
    }
    cache_set(cache_key, result)
    return result

# ==============================================
# ACCOUNT HEAD RECOMMENDATION (Validation Rules, Phase 6 of 7)
# Fuzzy-matches a voucher line's description against the client's leaf
# Trial Balance accounts (by Name, via rapidfuzz) to recommend an existing
# Chart-of-Accounts head instead of creating a new one. Separate, name-based
# lookup - does not touch the account-code-based Assign Categories logic.
# Recommends only; never creates accounts.
# ==============================================

# Starting judgement call for this phase - tunable, not fixed forever.
ACCOUNT_HEAD_MATCH_THRESHOLD = 55

# Account Head Recommendation follow-up (reposition + TB search-select + no-match note):
# reserved sentinel document_id for the Voucher Line tab's "no suitable TB account" note - reuses
# voucher_line_observations/sync_line_observations_to_voucher() without a real audit_rule_documents
# row (this app never enables PRAGMA foreign_keys=ON, so a non-existent id is safe at the DB
# level). Must match ACCOUNT_HEAD_OBSERVATION_DOC_ID in static/verification.js.
ACCOUNT_HEAD_OBSERVATION_DOC_ID = -1

_ACCOUNT_HEAD_STOPWORDS = {
    'the', 'and', 'for', 'from', 'with', 'this', 'that', 'account', 'accounts'
}


def _account_head_keyword_match(description: str, account_name: str):
    """Longest shared significant word (len>=4, stopwords skipped) between the
    two strings, or None if there's no such overlap. Rule-based reason text,
    not a scoring input."""
    desc_words = {w for w in re.findall(r'[a-zA-Z]+', description.lower())
                  if len(w) >= 4 and w not in _ACCOUNT_HEAD_STOPWORDS}
    name_words = {w for w in re.findall(r'[a-zA-Z]+', (account_name or '').lower())
                  if len(w) >= 4 and w not in _ACCOUNT_HEAD_STOPWORDS}
    shared = desc_words & name_words
    if not shared:
        return None
    return max(shared, key=len)


@router.get("/api/verification/suggest-account-head")
def suggest_account_head(
    client_id: int = Query(..., description="Client ID"),
    description: str = Query(..., description="Voucher line description to match"),
    category: str = Query(None, description="Category (Account Type) name, used only for the alignment bonus/reason")
):
    """
    Recommend up to 2 existing leaf Trial Balance accounts (by Name) as a
    Chart-of-Accounts head for a voucher line, using rapidfuzz fuzzy matching
    plus rule-based reason strings. Returns a clean "no match" shape (not an
    error) when nothing clears ACCOUNT_HEAD_MATCH_THRESHOLD - matches
    category-for-account's "absence is a normal outcome" convention. This
    only recommends; it never creates a Chart-of-Accounts head.
    """
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    no_match_response = {"matched": False, "message": "No match — new head likely needed", "suggestions": []}

    upload_rows = execute_query_pg(
        """
        SELECT id FROM trial_balance_uploads
        WHERE client_id = %s AND status = 'parsed'
        ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC
        LIMIT 1
        """,
        (client_id,)
    )
    if not upload_rows:
        return no_match_response

    accounts = execute_query_pg(
        """
        SELECT a.account_code, a.account_name, a.category_id, c.name AS category_name
        FROM trial_balance_accounts a
        LEFT JOIN audit_rule_categories c ON a.category_id = c.id AND c.is_active = TRUE
        WHERE a.upload_id = %s AND a.classification = 'leaf'
        """,
        (upload_rows[0]['id'],)
    )
    if not accounts:
        return no_match_response

    scored = []
    for acc in accounts:
        # WRatio (rapidfuzz's blended general-purpose scorer) + default_process
        # (case/punctuation-insensitive) - account names are stored ALL CAPS
        # while descriptions use natural case, so a case-sensitive scorer
        # silently sinks every real match.
        score = fuzz.WRatio(description, acc['account_name'] or '', processor=fuzz_utils.default_process)
        reasons = []

        if category and acc['category_name'] and acc['category_name'].strip().lower() == category.strip().lower():
            score = min(100, score + 10)
            reasons.append(f"Category alignment: linked to Category '{acc['category_name']}'")

        keyword = _account_head_keyword_match(description, acc['account_name'])
        if keyword:
            reasons.append(f"Matched keyword: '{keyword}'")

        reasons.append("High confidence match" if score >= 80 else "Medium confidence match")

        scored.append({
            "account_code": acc['account_code'],
            "account_name": acc['account_name'],
            "score": round(score),
            "reasons": reasons
        })

    scored.sort(key=lambda s: s['score'], reverse=True)
    top = scored[:2]

    if not top or top[0]['score'] < ACCOUNT_HEAD_MATCH_THRESHOLD:
        return no_match_response

    top = [s for s in top if s['score'] >= ACCOUNT_HEAD_MATCH_THRESHOLD]
    return {"matched": True, "suggestions": top}

# ==============================================
# GENERIC VOUCHER-LEVEL CHECKS (new 6-phase requirement, Phase 4 of 6; cutover Phase 5 of 6)
# Read/write for voucher_check_status (database.py, Phase 1) against the check definitions
# configured in Audit Rules (audit_rule_voucher_checks / audit_rule_voucher_check_auth_roles,
# Phase 2). Keyed by client_id+transaction_ref - same rationale as the old voucher_approvals
# table (mandatory, always-required gate independent of Category/documents; survives a
# cashbook being reopened in a new verification session since it isn't session_id-scoped).
# Phase 5 cut compute_and_persist_cleared_status()'s voucher-level gate over to this table and
# retired the old GET/POST voucher_approval / save_voucher_approval endpoints that used to read
# voucher_approvals directly - that table itself is left in place (schema untouched), just no
# longer read/written by this router.
# ==============================================

VOUCHER_CHECK_STATES = ('untouched', 'done', 'not_done')

@router.get("/api/verification/voucher_checks")
def get_voucher_checks_status(
    client_id: int = Query(..., description="Client ID"),
    transaction_ref: str = Query(..., description="Voucher number (verification_details.transaction_ref)")
):
    """
    Return every active audit_rule_voucher_checks definition, ordered by sort_order, merged
    with this voucher's current 3-state status. A check with no voucher_check_status row yet
    returns state='untouched' - no record yet is a normal state, same convention as
    get_category_for_account(). Voucher-level Checks simplified to name-only (Phase 2 of 4):
    auth_roles and performed_by_name are both gone - Phase 1 already dropped the role concept
    from Audit Rules entirely, and this model has no "performed by" field anymore either.
    """
    checks = execute_query_pg(
        "SELECT id, name, sort_order FROM audit_rule_voucher_checks WHERE is_active = TRUE ORDER BY sort_order"
    )
    status_rows = execute_query_pg(
        "SELECT check_id, state FROM voucher_check_status WHERE client_id = %s AND transaction_ref = %s",
        (client_id, transaction_ref)
    )
    status_by_check_id = {r['check_id']: r for r in status_rows}

    result = []
    for check in checks:
        status = status_by_check_id.get(check['id'])
        result.append({
            "id": check['id'],
            "name": check['name'],
            "sort_order": check['sort_order'],
            "state": status['state'] if status else 'untouched'
        })
    return result


@router.post("/api/verification/save_voucher_check")
def save_voucher_check_status(
    client_id: int = Body(..., embed=True),
    transaction_ref: str = Body(..., embed=True),
    check_id: int = Body(..., embed=True),
    state: str = Body(..., embed=True, description="'untouched' | 'done' | 'not_done'")
):
    """
    Upsert one voucher's 3-state status for one check. ON CONFLICT DO UPDATE, matching
    voucher_check_status's own UNIQUE(client_id, transaction_ref, check_id) constraint - same
    per-item-tick upsert shape as set_procedure_check(), not voucher_approvals' select-then-branch,
    since this is a repeated single-field toggle rather than a single multi-field record.
    Only ever writes `state` - `flagged_in_observations` is untouched here, it only ever changes
    inside verify_voucher_checks() (the "Verify & Next/Close" action), per the confirmed
    requirement that ordinary ticking must not itself alter the voucher's Observation field.
    """
    if state not in VOUCHER_CHECK_STATES:
        raise HTTPException(400, f"Invalid state '{state}' - must be one of {VOUCHER_CHECK_STATES}.")

    check = execute_query_pg(
        "SELECT id FROM audit_rule_voucher_checks WHERE id = %s AND is_active = TRUE",
        (check_id,)
    )
    if not check:
        raise HTTPException(400, f"Check {check_id} is not a configured active voucher-level check.")

    execute_write_pg(
        """
        INSERT INTO voucher_check_status (client_id, transaction_ref, check_id, state, updated_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT(client_id, transaction_ref, check_id)
        DO UPDATE SET state = excluded.state, updated_at = CURRENT_TIMESTAMP
        """,
        (client_id, transaction_ref, check_id, state)
    )
    log_audit("Analyst", "Save Voucher Check", "Verification",
              f"Client {client_id}, voucher '{transaction_ref}', check {check_id} set to state={state}.")
    return {
        "client_id": client_id,
        "transaction_ref": transaction_ref,
        "check_id": check_id,
        "state": state
    }


@router.post("/api/verification/save_voucher_checks_batch")
def save_voucher_checks_batch(
    client_id: int = Body(..., embed=True),
    transaction_ref: str = Body(..., embed=True),
    checks: List[Dict[str, Any]] = Body(..., embed=True),
):
    """Atomically upsert several voucher-level check states in one request."""
    if not isinstance(checks, list):
        raise HTTPException(400, "checks must be a list.")
    with pg_transaction():
        active = execute_query_pg(
            "SELECT id FROM audit_rule_voucher_checks WHERE is_active = TRUE"
        )
        active_ids = {row['id'] for row in active}
        for check in checks:
            if check.get('check_id') not in active_ids:
                raise HTTPException(400, f"Check {check.get('check_id')} is not active.")
            if check.get('state') not in VOUCHER_CHECK_STATES:
                raise HTTPException(400, f"Invalid state '{check.get('state')}'.")
        for check in checks:
            execute_write_pg(
                """INSERT INTO voucher_check_status
                   (client_id, transaction_ref, check_id, state, updated_at)
                   VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                   ON CONFLICT(client_id, transaction_ref, check_id)
                   DO UPDATE SET state = excluded.state, updated_at = CURRENT_TIMESTAMP""",
                (client_id, transaction_ref, check['check_id'], check['state'])
            )
    return {"client_id": client_id, "transaction_ref": transaction_ref, "checks": checks}

# ==============================================
# VOUCHER LINE SUPPORTING DOCUMENTS (new 6-phase requirement, Phase 1 of 6)
# Data layer for linking a Data Bank row (client_data) to a voucher line as
# supporting evidence for a specific Document "slot" (audit_rule_documents).
# "Once linked, never assignable again elsewhere" is enforced by a partial
# unique index on client_data_id scoped to status='active' (database.py) -
# every query below that reads links must filter status='active', since an
# unassigned (soft-deleted, see unassign_supporting_document further down)
# row must never be treated as a live link.
# ==============================================

def _resolve_sibling_voucher_line_ids(voucher_line_id: int) -> Optional[List[int]]:
    """
    Bug fix (Voucher Supporting-Document Linking follow-up, Phase 3 of 4): start_verification_session()
    has no "resume an existing session" logic - every time a cashbook is opened and verification is
    (re)started, a brand-new verification_sessions row and a brand-new set of verification_details
    rows are inserted for every voucher/line, even ones already worked on in an earlier session. So
    the SAME real accounting line (one client_data row) gets a DIFFERENT verification_details.id -
    the "voucher_line_id"/"uid" used throughout this file - every time it's re-verified.

    voucher_line_documents/voucher_line_procedure_checks/voucher_line_observations all key their
    data off that per-session id, which silently orphaned any assignment/tick/observation made in
    an earlier session the moment a new session was started for the same cashbook - Browse still
    reported the row as "Assigned" (it resolves the assignment's own stored voucher_line_id back to
    a still-valid transaction_ref, from whichever old session it belongs to), but Linked Evidence
    (filtered by the CURRENT session's voucher_line_id only) could never find it.

    Returns every verification_details.id (across ALL sessions, past and present) that shares the
    same underlying client_data_id as voucher_line_id - i.e. every copy of this exact accounting
    line - so a caller can query "WHERE voucher_line_id IN (...)" instead of "= %s" and see
    assignments/ticks/observations regardless of which session they were made under. Returns None
    if voucher_line_id itself doesn't exist (caller 404s, same as before this fix). Writes are NOT
    affected by this helper - they still target whichever voucher_line_id the caller passes in,
    exactly as before.
    """
    line = execute_query_pg("SELECT client_data_id FROM verification_details WHERE id = %s", (voucher_line_id,))
    if not line:
        return None
    siblings = execute_query_pg(
        "SELECT id FROM verification_details WHERE client_data_id = %s",
        (line[0]['client_data_id'],)
    )
    return [s['id'] for s in siblings]


@router.post("/api/verification/assign_document")
def assign_supporting_document(
    voucher_line_id: int = Body(..., embed=True, description="verification_details.id (the voucher line 'uid')"),
    client_data_id: int = Body(..., embed=True, description="client_data.id of the Data Bank row being linked as evidence"),
    document_id: int = Body(..., embed=True, description="audit_rule_documents.id - which evidence slot this fulfills"),
    linked_by: str = Body("Analyst", embed=True)
):
    """
    Link a Data Bank row to a voucher line as supporting evidence for one Document slot.
    Rejects if the client_data row is already linked to any voucher line (the
    UNIQUE(client_data_id) constraint is the ultimate backstop, but this check gives a
    clear 400 naming the existing voucher instead of a raw DB-constraint error).
    """
    line = execute_query_pg("SELECT id FROM verification_details WHERE id = %s", (voucher_line_id,))
    if not line:
        raise HTTPException(404, "Voucher line not found.")

    row = execute_query_pg("SELECT id FROM client_data WHERE id = %s AND is_deleted = FALSE", (client_data_id,))
    if not row:
        raise HTTPException(404, "Data Bank row not found or has been deleted.")

    doc = execute_query_pg("SELECT id FROM audit_rule_documents WHERE id = %s AND is_active = TRUE", (document_id,))
    if not doc:
        raise HTTPException(404, "Document not found or inactive.")

    existing = execute_query_pg(
        """
        SELECT vld.id, vld.voucher_line_id, vd.transaction_ref
        FROM voucher_line_documents vld
        JOIN verification_details vd ON vd.id = vld.voucher_line_id
        WHERE vld.client_data_id = %s AND vld.status = 'active'
        """,
        (client_data_id,)
    )
    if existing:
        raise HTTPException(
            400,
            f"This Data Bank row is already assigned to voucher '{existing[0]['transaction_ref']}' (line {existing[0]['voucher_line_id']})."
        )

    assignment_id = execute_write_pg(
        """
        INSERT INTO voucher_line_documents (voucher_line_id, client_data_id, document_id, linked_by)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (voucher_line_id, client_data_id, document_id, linked_by)
    )

    log_audit(linked_by, "Assign Supporting Document", "Verification",
               f"Linked client_data {client_data_id} to voucher line {voucher_line_id} (document {document_id}).")

    linked_row = execute_query_pg(
        "SELECT id, voucher_line_id, client_data_id, document_id, linked_by, linked_at FROM voucher_line_documents WHERE id = %s",
        (assignment_id,)
    )[0]
    return dict(linked_row)


@router.get("/api/verification/voucher_line_documents/{voucher_line_id}")
def get_voucher_line_documents(voucher_line_id: int):
    """
    List every ACTIVE document currently assigned to a voucher line (status='active' -
    an unassigned/soft-deleted link is never returned here), enriched with the
    Document's name and the linked client_data row's data - so a later phase's
    selected-lines table can render without a second round-trip.

    Bug fix (Phase 3 of 4, see _resolve_sibling_voucher_line_ids()): now looks up assignments
    across every verification_details.id that shares this line's own client_data_id, not just
    voucher_line_id itself - so evidence linked in an earlier verification session still shows
    up when the same voucher line is reopened in a new one. No dedup needed here - the partial
    unique index on client_data_id already guarantees one evidence row can never be actively
    linked under more than one sibling at a time.
    """
    sibling_ids = _resolve_sibling_voucher_line_ids(voucher_line_id)
    if sibling_ids is None:
        raise HTTPException(404, "Voucher line not found.")

    placeholders = ",".join("%s" for _ in sibling_ids)
    rows = execute_query_pg(
        f"""
        SELECT vld.id, vld.voucher_line_id, vld.client_data_id, vld.document_id,
               vld.linked_by, vld.linked_at, vld.signature_exception,
               ard.name AS document_name, ard.is_active AS document_is_active,
               cd.row_data, cd.extract_date, cd.extract_amount, cd.rate_source
        FROM voucher_line_documents vld
        JOIN audit_rule_documents ard ON ard.id = vld.document_id
        JOIN client_data cd ON cd.id = vld.client_data_id
        WHERE vld.voucher_line_id IN ({placeholders}) AND vld.status = 'active'
        ORDER BY vld.linked_at ASC
        """,
        tuple(sibling_ids)
    )

    result = []
    for r in rows:
        row_data = json.loads(r['row_data']) if isinstance(r['row_data'], str) else r['row_data']
        result.append({
            "id": r['id'],
            "voucher_line_id": r['voucher_line_id'],
            "client_data_id": r['client_data_id'],
            "document_id": r['document_id'],
            "document_name": r['document_name'],
            # Inactive-document handling: lets the frontend detect/lock a tab whose
            # Document was deactivated after evidence was already linked to it.
            "is_active": bool(r['document_is_active']),
            "linked_by": r['linked_by'],
            "linked_at": r['linked_at'],
            # Validation Rules Phase 5: default OK (0/False) - only ever set to True on a
            # confirmed signature mismatch, per Phase 1's locked assumption.
            "signature_exception": bool(r['signature_exception']),
            "row_data": row_data,
            "extract_date": r['extract_date'],
            "extract_amount": r['extract_amount'],
            # Rate-derivation requirement, Phase 10 (FINAL): per-row provenance for the Rate
            # column (imported/derived/None), read straight from client_data — see the matching
            # comment in routers/data.py's GET /api/data/preview.
            "rate_source": r['rate_source']
        })
    return {"voucher_line_id": voucher_line_id, "documents": result, "count": len(result)}


@router.post("/api/verification/set_signature_exception")
def set_signature_exception(
    assignment_id: int = Body(..., embed=True, description="voucher_line_documents.id of the linked evidence row"),
    signature_exception: bool = Body(..., embed=True)
):
    """
    Validation Rules Phase 5: flag/clear a signature-exception on one linked evidence row.
    Defaults OK - the auditor ticks this only to record a confirmed mismatch against the
    document's signature_specimens entry (Phase 1). Simple update-and-return, mirroring
    toggle_procedure_check()'s shape.
    """
    row = execute_query_pg(
        "SELECT id, voucher_line_id FROM voucher_line_documents WHERE id = %s AND status = 'active'",
        (assignment_id,)
    )
    if not row:
        raise HTTPException(404, "Linked evidence row not found or no longer active.")

    execute_write_pg(
        "UPDATE voucher_line_documents SET signature_exception = %s WHERE id = %s",
        (signature_exception, assignment_id)
    )
    log_audit("Analyst", "Set Signature Exception", "Verification",
              f"Assignment {assignment_id}: signature_exception={signature_exception}.")
    return {"id": assignment_id, "voucher_line_id": row[0]['voucher_line_id'], "signature_exception": signature_exception}


@router.post("/api/verification/unassign_document")
def unassign_supporting_document(
    assignment_id: int = Body(..., embed=True, description="voucher_line_documents.id of the link to unassign")
):
    """
    Soft-deletes a voucher_line_documents link: flips status to 'unassigned' and stamps
    unassigned_at, rather than deleting the row - preserves linked_by/linked_at as a
    provenance trail. Once unassigned, the underlying client_data row is free to be
    assigned again (to this or any other voucher line) because every read query in this
    file (and get_assignment_status_map, used by routers/data.py's preview flags) filters
    to status='active' only - the freed row is treated as unassigned automatically, with
    no other code path needing to change. Observation text for the tab is untouched here;
    that is a deliberate choice, not an oversight (see the confirmed requirement).
    404s on an unknown id or one that's already unassigned (nothing to undo twice).
    """
    link = execute_query_pg(
        "SELECT id, voucher_line_id, client_data_id, document_id, status FROM voucher_line_documents WHERE id = %s",
        (assignment_id,)
    )
    if not link:
        raise HTTPException(404, "Supporting document link not found.")
    if link[0]['status'] != 'active':
        raise HTTPException(404, "This supporting document link has already been unassigned.")

    execute_write_pg(
        "UPDATE voucher_line_documents SET status = 'unassigned', unassigned_at = CURRENT_TIMESTAMP WHERE id = %s",
        (assignment_id,)
    )

    log_audit("Analyst", "Unassign Supporting Document", "Verification",
               f"Unlinked client_data {link[0]['client_data_id']} from voucher line {link[0]['voucher_line_id']} (document {link[0]['document_id']}).")

    return {
        "id": assignment_id,
        "voucher_line_id": link[0]['voucher_line_id'],
        "client_data_id": link[0]['client_data_id'],
        "document_id": link[0]['document_id'],
        "status": "unassigned"
    }


# ==============================================
# VOUCHER LINE PROCEDURE CHECKS (Phase 5 of 6: audit procedures checklist)
# Persists the tick state of each Document's auth-role checklist (e.g. "Prepared by",
# "Approved by") per voucher line, rendered under that Document tab's Linked Evidence
# table in the Supporting Documents modal. No validation of what a tick "means" - rules
# are deferred to a future phase, per the confirmed brief.
# ==============================================

@router.get("/api/verification/procedure_checks/{voucher_line_id}")
def get_procedure_checks(voucher_line_id: int):
    """
    Batch fetch every ticked/unticked role recorded for a voucher line, across all
    documents - one call per Supporting Documents modal open, mirroring how
    voucher_line_documents/{id} is already fetched once there. A role with no row is
    simply absent from the response; the frontend treats absence as unchecked.

    Bug fix (Phase 3 of 4, see _resolve_sibling_voucher_line_ids()): now looks up ticks across
    every verification_details.id that shares this line's own client_data_id, not just
    voucher_line_id itself, so a tick made in an earlier verification session still shows up
    when the same voucher line is reopened in a new one. Unlike voucher_line_documents (which
    can never have two sibling lines both claiming the same evidence row), two different
    sibling sessions COULD each have their own row for the same (document_id, role_name) - the
    most recently checked/unchecked one wins (deduped in Python below by checked_at).
    """
    sibling_ids = _resolve_sibling_voucher_line_ids(voucher_line_id)
    if sibling_ids is None:
        raise HTTPException(404, "Voucher line not found.")

    placeholders = ",".join("%s" for _ in sibling_ids)
    rows = execute_query_pg(
        f"""
        SELECT vlpc.document_id, vlpc.role_name, vlpc.is_checked, vlpc.checked_by, vlpc.checked_at,
               ard.name AS document_name, ard.is_active AS document_is_active
        FROM voucher_line_procedure_checks vlpc
        LEFT JOIN audit_rule_documents ard ON ard.id = vlpc.document_id
        WHERE vlpc.voucher_line_id IN ({placeholders})
        """,
        tuple(sibling_ids)
    )
    # Dedup by (document_id, role_name), keeping whichever sibling's row was checked/unchecked
    # most recently - checked_at is a real TIMESTAMPTZ value now (Postgres), compared via
    # _ts_sort_key() so None/real-datetime rows sort correctly against each other.
    latest_by_key: Dict[tuple, dict] = {}
    for r in rows:
        key = (r['document_id'], r['role_name'])
        existing = latest_by_key.get(key)
        if existing is None or _ts_sort_key(r['checked_at']) > _ts_sort_key(existing['checked_at']):
            latest_by_key[key] = r

    checks = [
        {
            "document_id": r['document_id'],
            "role_name": r['role_name'],
            "is_checked": bool(r['is_checked']),
            "checked_by": r['checked_by'],
            "checked_at": r['checked_at'],
            "document_name": r['document_name'],
            # Inactive-document handling: LEFT JOIN so a document_id is never dropped;
            # None (not False) is preserved when there's no matching audit_rule_documents
            # row at all, distinguishing "no real document" from "real, deactivated".
            "is_active": r['document_is_active'] if r['document_is_active'] is None else bool(r['document_is_active'])
        }
        for r in latest_by_key.values()
    ]
    return {"voucher_line_id": voucher_line_id, "checks": checks}


@router.post("/api/verification/procedure_checks")
def set_procedure_check(
    voucher_line_id: int = Body(..., embed=True, description="verification_details.id (the voucher line 'uid')"),
    document_id: int = Body(..., embed=True, description="audit_rule_documents.id this role belongs to"),
    role_name: str = Body(..., embed=True, description="Must match an existing audit_rule_document_auth_roles.role_name for this document"),
    is_checked: bool = Body(..., embed=True),
    checked_by: str = Body("Analyst", embed=True)
):
    """
    Toggle one auth-role checkbox for a voucher line's Document tab. Upserts by
    (voucher_line_id, document_id, role_name) so re-ticking the same role updates the
    existing row instead of creating a duplicate.
    """
    line = execute_query_pg("SELECT id FROM verification_details WHERE id = %s", (voucher_line_id,))
    if not line:
        raise HTTPException(404, "Voucher line not found.")

    doc = execute_query_pg("SELECT id FROM audit_rule_documents WHERE id = %s AND is_active = TRUE", (document_id,))
    if not doc:
        raise HTTPException(404, "Document not found or inactive.")

    role = execute_query_pg(
        "SELECT id FROM audit_rule_document_auth_roles WHERE document_id = %s AND role_name = %s",
        (document_id, role_name)
    )
    if not role:
        raise HTTPException(400, f"'{role_name}' is not a configured auth role for this document.")

    execute_write_pg(
        """
        INSERT INTO voucher_line_procedure_checks (voucher_line_id, document_id, role_name, is_checked, checked_by, checked_at)
        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT(voucher_line_id, document_id, role_name)
        DO UPDATE SET is_checked = excluded.is_checked, checked_by = excluded.checked_by, checked_at = CURRENT_TIMESTAMP
        """,
        (voucher_line_id, document_id, role_name, is_checked, checked_by)
    )

    log_audit(checked_by, "Set Audit Procedure Check", "Verification",
               f"Set '{role_name}' ({document_id}) to {is_checked} for voucher line {voucher_line_id}.")

    updated = execute_query_pg(
        """
        SELECT voucher_line_id, document_id, role_name, is_checked, checked_by, checked_at
        FROM voucher_line_procedure_checks
        WHERE voucher_line_id = %s AND document_id = %s AND role_name = %s
        """,
        (voucher_line_id, document_id, role_name)
    )[0]
    return {
        "voucher_line_id": updated['voucher_line_id'],
        "document_id": updated['document_id'],
        "role_name": updated['role_name'],
        "is_checked": bool(updated['is_checked']),
        "checked_by": updated['checked_by'],
        "checked_at": updated['checked_at']
    }


@router.post("/api/verification/procedure_checks_batch")
def set_procedure_checks_batch(
    checks: List[Dict[str, Any]] = Body(..., embed=True),
):
    """Atomically upsert several procedure-role ticks in one request."""
    if not isinstance(checks, list):
        raise HTTPException(400, "checks must be a list.")
    with pg_transaction():
        # Validate the complete batch with set-based reads. The previous loop performed up to
        # three remote round trips per check, which is expensive when Save/Verify submits many
        # document-role ticks over the remote database connection.
        line_ids = {check.get('voucher_line_id') for check in checks}
        document_ids = {check.get('document_id') for check in checks}
        line_rows = execute_query_pg(
            "SELECT id FROM verification_details WHERE id = ANY(%s)",
            (list(line_ids),)
        ) if line_ids else []
        if len({row['id'] for row in line_rows}) != len(line_ids):
            raise HTTPException(404, "Voucher line not found.")
        document_rows = execute_query_pg(
            "SELECT id FROM audit_rule_documents WHERE id = ANY(%s) AND is_active = TRUE",
            (list(document_ids),)
        ) if document_ids else []
        if len({row['id'] for row in document_rows}) != len(document_ids):
            raise HTTPException(404, "Document not found or inactive.")
        role_pairs = {(check.get('document_id'), check.get('role_name')) for check in checks}
        role_clauses = ["(document_id = %s AND role_name = %s)"] * len(role_pairs)
        role_params = [value for pair in role_pairs for value in pair]
        role_rows = execute_query_pg(
            f"SELECT document_id, role_name FROM audit_rule_document_auth_roles WHERE {' OR '.join(role_clauses)}",
            role_params
        ) if role_pairs else []
        if not role_pairs.issubset({(row['document_id'], row['role_name']) for row in role_rows}):
            raise HTTPException(400, "Role is not configured for this document.")
        for check in checks:
            execute_write_pg(
                """INSERT INTO voucher_line_procedure_checks
                   (voucher_line_id, document_id, role_name, is_checked, checked_by, checked_at)
                   VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                   ON CONFLICT(voucher_line_id, document_id, role_name)
                   DO UPDATE SET is_checked = excluded.is_checked,
                                 checked_by = excluded.checked_by, checked_at = CURRENT_TIMESTAMP""",
                (check['voucher_line_id'], check['document_id'], check['role_name'],
                 bool(check.get('is_checked')), check.get('checked_by', 'Analyst'))
            )
    return {"checks": checks}


# ==============================================
# VOUCHER LINE OBSERVATIONS (Phase 6 of 6, FINAL: observation flow)
# Each Document tab in the Supporting Documents modal gets its own observation text field,
# saved as a tagged, per-document entry against the voucher line (voucher_line_observations,
# database.py). Entries are upserted in place (one per voucher_line_id+document_id) and
# unconditionally marked unsynced on every explicit save; sync_line_observations_to_voucher()
# (called from save_line_items(), the single chokepoint every Save/Next/Prev/Validate/
# Complete/Exit path already runs per line) appends any unsynced, non-blank entries into
# that line's own verification_details.observations field - labelled by document name,
# never overwriting whatever text (auto or manual) was already there - then marks them
# synced so the same text is never appended twice. Fixed 2026-07-25 to resolve sibling
# voucher_line_ids first (via _resolve_sibling_voucher_line_ids()) rather than only the exact
# id passed in - otherwise an observation saved under an orphaned earlier session (see that
# helper's docstring) was permanently stranded and never rolled up. See sync function docstring.
# ==============================================

def sync_line_observations_to_voucher(voucher_line_id: int, sibling_ids: Optional[List[int]] = None) -> None:
    """
    Rebuild the line's own verification_details.observations field from scratch, from the
    CURRENT state of every document's own observation entry for this line - exactly one
    labelled line per document/procedure, never more. A no-op if nothing to build. Never
    raises - a failure here should not block the save_line_items()/save_line_observation()
    call it's invoked from.

    Bug fix (confirmed against live data, 2026-07-25): start_verification_session() has no
    session-resume logic, so the same real accounting line gets a brand-new
    verification_details.id (a new "sibling") every time its cashbook is reopened. Now uses
    _resolve_sibling_voucher_line_ids() - the same helper already used by every read path in
    this feature (get_voucher_line_documents/get_procedure_checks/get_line_observations) - so
    an observation saved under an earlier sibling's id is still picked up when rebuilding.

    Bug fix (confirmed against live data, 2026-07-25): this function used to APPEND one block
    per save, so saving the same procedure's box repeatedly (whether editing it or re-saving
    unchanged text) kept stacking up duplicate/superseded lines in the voucher's Observation
    field - e.g. three separate "[Purchase Demand]: ..." lines for what was really just one box
    edited three times. Rebuilding the ENTIRE field from source data on every call, rather than
    appending, is what actually guarantees "at most one line per procedure" as an invariant -
    a find/replace on the stored text was considered and rejected as more fragile (opaque
    string-splicing vs. deriving the whole field fresh from the one table that's the real
    source of truth). Every voucher_line_observations row for this line (across all siblings,
    not just unsynced ones - considering only "pending" rows would silently drop an untouched
    procedure's line from the rebuilt text) is read, deduped one-per-document (keeping
    whichever sibling's row was updated most recently - same tie-break get_line_observations()
    already uses), and any document with blank/cleared text is dropped entirely. `synced`/
    `last_synced_text` are still written per entry for consistency/audit visibility, but under
    this rebuild design they no longer gate any behaviour - every entry is considered every
    time, regardless of synced state.

    Voucher-level Checks simplified to name-only (Phase 2 of 4): the rebuilt text is now
    <voucher-check "not done" blocks> followed by <per-document blocks> - the check blocks are
    derived purely from voucher_check_status.flagged_in_observations, a flag written ONLY by
    verify_voucher_checks() (the "Verify & Next/Close" action). This function itself never
    changes that flag - it just re-renders whatever it currently says, so an ordinary line save
    (Prev/Next/close icon, which all funnel through this same function) re-stamps the same
    check blocks without altering which ones are present. The early "nothing to build" exit is
    gone for this reason: a line can now have check blocks to write even with zero document
    observation entries.
    """
    try:
        # sibling_ids: pass the already-resolved list (save_line_items()'s chain shares one
        # resolve across this, compute_and_persist_cleared_status(), etc.) to skip a re-resolve.
        if sibling_ids is None:
            sibling_ids = _resolve_sibling_voucher_line_ids(voucher_line_id)
        if not sibling_ids:
            # voucher_line_id itself doesn't exist - nothing to do (shouldn't happen, since
            # save_line_items()/save_line_observation() already validated it exists).
            return

        placeholders = ",".join("%s" for _ in sibling_ids)
        # LEFT JOIN (not INNER) so the reserved ACCOUNT_HEAD_OBSERVATION_DOC_ID sentinel entry -
        # which has no matching audit_rule_documents row by design - isn't silently dropped from
        # the rollup; its document_name comes back NULL and is handled in the block-building loop
        # below instead.
        all_entries = execute_query_pg(
            f"""
            SELECT vlo.id, vlo.document_id, vlo.observation_text, vlo.updated_at,
                   ard.name AS document_name, ard.is_system AS is_system
            FROM voucher_line_observations vlo
            LEFT JOIN audit_rule_documents ard ON ard.id = vlo.document_id
            WHERE vlo.voucher_line_id IN ({placeholders})
            """,
            tuple(sibling_ids)
        )

        # Dedup to one row per document_id, keeping whichever sibling's entry is most recently
        # updated - same convention get_line_observations() already uses for this identical
        # cross-session scenario (two different sessions can each carry their own entry for the
        # same document).
        latest_by_doc: Dict[int, dict] = {}
        for entry in all_entries:
            existing = latest_by_doc.get(entry['document_id'])
            if existing is None or _ts_sort_key(entry['updated_at']) > _ts_sort_key(existing['updated_at']):
                latest_by_doc[entry['document_id']] = entry

        # One labelled block per document that currently has non-blank text - a blank/cleared
        # observation simply contributes no line, so re-clearing a box removes its line entirely
        # on the next rebuild rather than leaving a stale one behind.
        def _format_block(entry: dict) -> str:
            # The sentinel entry stores the intern's raw note text (unwrapped, so re-opening the
            # modal shows exactly what was typed) - the fully-worded finding is only assembled
            # here, at rollup time.
            if entry['document_id'] == ACCOUNT_HEAD_OBSERVATION_DOC_ID:
                return (f"[Account Head Recommendation]: New account head recommended: "
                        f"{entry['observation_text'].strip()} — no match in Trial Balance")
            # Automated cross-document validation engine, Phase 8 of 10: a shortfall sentinel is
            # a real, hidden Document row (ard.is_system) - the Cross-check's own name is already
            # embedded in the stored text itself (_format_shortfall_observation_text()), so this
            # label stays generic rather than showing the sentinel's own internal document_name.
            if entry.get('is_system'):
                return f"[Cross-check Shortfall]: {entry['observation_text'].strip()}"
            return f"[{entry['document_name']}]: {entry['observation_text'].strip()}"

        doc_blocks = [
            _format_block(entry)
            for entry in sorted(latest_by_doc.values(), key=lambda e: e['document_id'])
            if entry['observation_text'] and entry['observation_text'].strip()
        ]

        # Voucher-check blocks: derived from this line's own (client_id, transaction_ref), not
        # from sibling ids - voucher_check_status is deliberately keyed session-independently
        # (see that table's own schema comment), so a plain lookup on the line itself is enough.
        line_row = execute_query_pg(
            """
            SELECT vd.transaction_ref, vs.client_id
            FROM verification_details vd
            JOIN verification_sessions vs ON vs.id = vd.session_id
            WHERE vd.id = %s
            """,
            (voucher_line_id,)
        )
        check_blocks: List[str] = []
        if line_row:
            check_rows = execute_query_pg(
                """
                SELECT arvc.name
                FROM voucher_check_status vcs
                JOIN audit_rule_voucher_checks arvc ON arvc.id = vcs.check_id
                WHERE vcs.client_id = %s AND vcs.transaction_ref = %s
                  AND arvc.is_active = TRUE AND vcs.flagged_in_observations = TRUE
                ORDER BY arvc.sort_order
                """,
                (line_row[0]['client_id'], line_row[0]['transaction_ref'])
            )
            check_blocks = [f"{r['name']} marked not done" for r in check_rows]

        blocks = check_blocks + doc_blocks
        new_text = "\n".join(blocks) if blocks else None

        execute_write_pg(
            "UPDATE verification_details SET observations = %s WHERE id = %s",
            (new_text, voucher_line_id)
        )

        # Bookkeeping only under this rebuild design (see docstring) - kept accurate for
        # consistency/audit visibility, not because anything still reads it to gate behaviour.
        # Batched via execute_batch_pg() (one connection, one pipelined round-trip) rather than
        # one execute_write_pg() call per entry - each of those borrowed/released its own pooled
        # connection and round-tripped separately, which is what made this loop a real contributor
        # to save_line_items()'s total DB round-trip count (see routers/import_export.py's own
        # get_raw_conn_pg()/execute_batch_pg()/release_raw_conn_pg() usage for the same pattern).
        if all_entries:
            conn = get_raw_conn_pg()
            try:
                execute_batch_pg(
                    conn,
                    "UPDATE voucher_line_observations SET synced = TRUE, last_synced_text = %s WHERE id = %s",
                    [(entry['observation_text'], entry['id']) for entry in all_entries]
                )
            finally:
                release_raw_conn_pg(conn)
    except Exception as exc:
        # Deliberately swallow - observation roll-up must never block a line save.
        print(f"⚠️ sync_line_observations_to_voucher failed for line {voucher_line_id}: {exc}")


def sync_voucher_observations_to_voucher(voucher_line_ids: List[int], sibling_ids: List[int]) -> None:
    """Rebuild every line in one voucher using set-based observation reads and writes."""
    if not voucher_line_ids:
        return
    try:
        line_ph = ",".join("%s" for _ in voucher_line_ids)
        sibling_ph = ",".join("%s" for _ in sibling_ids)
        rows = execute_query_pg(
            f"""SELECT vd.id AS line_id, vd.client_data_id, vlo.id, vlo.document_id,
                       vlo.observation_text, vlo.updated_at, ard.name AS document_name,
                       ard.is_system AS is_system
                FROM verification_details vd
                LEFT JOIN verification_details sibling ON sibling.client_data_id=vd.client_data_id
                LEFT JOIN voucher_line_observations vlo ON vlo.voucher_line_id=sibling.id
                LEFT JOIN audit_rule_documents ard ON ard.id=vlo.document_id
                WHERE vd.id IN ({line_ph})""",
            tuple(voucher_line_ids),
        )
        check_rows = execute_query_pg(
            """SELECT vcs.client_id, vcs.transaction_ref, arvc.name
               FROM voucher_check_status vcs JOIN audit_rule_voucher_checks arvc ON arvc.id=vcs.check_id
               WHERE arvc.is_active=TRUE AND vcs.flagged_in_observations=TRUE"""
        )
        checks = {}
        for r in check_rows:
            checks.setdefault((r['client_id'], r['transaction_ref']), []).append(f"{r['name']} marked not done")
        line_meta = execute_query_pg(
            f"""SELECT vd.id, vd.transaction_ref, vs.client_id
                FROM verification_details vd JOIN verification_sessions vs ON vs.id=vd.session_id
                WHERE vd.id IN ({line_ph})""",
            tuple(voucher_line_ids),
        )
        meta = {r['id']: r for r in line_meta}
        latest = {}
        synced_ids = []
        for r in rows:
            if r['id'] is None:
                continue
            synced_ids.append(r['id'])
            key = (r['line_id'], r['document_id'])
            if key not in latest or _ts_sort_key(r['updated_at']) > _ts_sort_key(latest[key]['updated_at']):
                latest[key] = r
        updates = []
        for line_id in voucher_line_ids:
            blocks = list(checks.get((meta[line_id]['client_id'], meta[line_id]['transaction_ref']), [])) if line_id in meta else []
            for (owner, _), entry in sorted(latest.items(), key=lambda item: (item[0][0], item[0][1])):
                if owner != line_id or not (entry['observation_text'] or '').strip():
                    continue
                if entry['document_id'] == ACCOUNT_HEAD_OBSERVATION_DOC_ID:
                    blocks.append(f"[Account Head Recommendation]: New account head recommended: {entry['observation_text'].strip()} — no match in Trial Balance")
                elif entry.get('is_system'):
                    blocks.append(f"[Cross-check Shortfall]: {entry['observation_text'].strip()}")
                else:
                    blocks.append(f"[{entry['document_name']}]: {entry['observation_text'].strip()}")
            updates.append(("\n".join(blocks) if blocks else None, line_id))
        conn = get_raw_conn_pg()
        try:
            execute_batch_pg(conn, "UPDATE verification_details SET observations=%s WHERE id=%s", updates)
            if synced_ids:
                execute_batch_pg(conn, "UPDATE voucher_line_observations SET synced=TRUE, last_synced_text=observation_text WHERE id=%s", [(i,) for i in synced_ids])
        finally:
            release_raw_conn_pg(conn)
    except Exception as exc:
        print(f"⚠️ sync_voucher_observations_to_voucher failed: {exc}")


@router.get("/api/verification/line_observations/{voucher_line_id}")
def get_line_observations(voucher_line_id: int):
    """
    Batch fetch every observation entry recorded for a voucher line, across all documents -
    one call per Supporting Documents modal open, mirroring how voucher_line_documents/{id}
    and procedure_checks/{id} are already fetched once there.

    Bug fix (Phase 3 of 4, see _resolve_sibling_voucher_line_ids()): now looks up entries
    across every verification_details.id that shares this line's own client_data_id, not just
    voucher_line_id itself, so an observation saved in an earlier verification session still
    shows up when the same voucher line is reopened in a new one. Two sibling sessions could
    each have their own entry for the same document_id - the most recently updated one wins
    (deduped in Python below by updated_at). This is a display-only read fix - sync_line_
    observations_to_voucher() (called from save_line_items()) still only ever rolls an entry
    into ITS OWN voucher_line_id's verification_details.observations field, unaffected.
    """
    sibling_ids = _resolve_sibling_voucher_line_ids(voucher_line_id)
    if sibling_ids is None:
        raise HTTPException(404, "Voucher line not found.")

    placeholders = ",".join("%s" for _ in sibling_ids)
    rows = execute_query_pg(
        f"""
        SELECT vlo.document_id, vlo.observation_text, vlo.synced, vlo.last_synced_text,
               vlo.updated_by, vlo.updated_at,
               ard.name AS document_name, ard.is_active AS document_is_active
        FROM voucher_line_observations vlo
        LEFT JOIN audit_rule_documents ard ON ard.id = vlo.document_id
        WHERE vlo.voucher_line_id IN ({placeholders})
        """,
        tuple(sibling_ids)
    )
    # Dedup by document_id, keeping whichever sibling's entry was updated most recently -
    # updated_at is a real TIMESTAMPTZ value now (Postgres), compared via _ts_sort_key().
    latest_by_doc: Dict[int, dict] = {}
    for r in rows:
        existing = latest_by_doc.get(r['document_id'])
        if existing is None or _ts_sort_key(r['updated_at']) > _ts_sort_key(existing['updated_at']):
            latest_by_doc[r['document_id']] = r

    entries = [
        {
            "document_id": r['document_id'],
            "observation_text": r['observation_text'] or '',
            "synced": bool(r['synced']),
            "last_synced_text": r['last_synced_text'],
            "updated_by": r['updated_by'],
            "updated_at": r['updated_at'],
            # Inactive-document handling: LEFT JOIN so the reserved ACCOUNT_HEAD_OBSERVATION_DOC_ID
            # sentinel (still not a real audit_rule_documents row) is never dropped - its
            # document_name/is_active come back None, never coerced to False, so the frontend can
            # tell "not a real document" apart from "real, deactivated". A cross-check-shortfall
            # entry is different since the note-save fix: it now has a real (but is_system=TRUE,
            # is_active=FALSE) Document row, so this comes back as a real name and
            # is_active: false rather than None/None - inert today, since nothing in the frontend
            # renders this sentinel as a Document tab (it's never attached to any Category).
            "document_name": r['document_name'],
            "is_active": r['document_is_active'] if r['document_is_active'] is None else bool(r['document_is_active'])
        }
        for r in latest_by_doc.values()
    ]
    return {"voucher_line_id": voucher_line_id, "entries": entries}


@router.post("/api/verification/save_line_observation")
def save_line_observation(
    voucher_line_id: int = Body(..., embed=True, description="verification_details.id (the voucher line 'uid')"),
    document_id: int = Body(..., embed=True, description="audit_rule_documents.id this observation belongs to"),
    observation_text: str = Body("", embed=True),
    updated_by: str = Body("Analyst", embed=True)
):
    """
    Save (upsert) one Document tab's observation text for a voucher line, then immediately roll
    it (and any other pending sibling entries) into the voucher line's own Observation field.

    Bug fix (confirmed against live data, 2026-07-25): this endpoint previously only ever marked
    the entry unsynced and relied entirely on some LATER, unrelated action (save_line_items() -
    i.e. Next/Prev/Validate/Complete/Exit) to eventually roll it up. Live-tracing a fresh save
    showed the write itself always worked correctly, but the roll-up could sit pending
    indefinitely if the auditor saved an observation and never happened to trigger another line
    save afterward - from the auditor's point of view, clicking "Save Observation" looks like a
    complete action, so the roll-up should not depend on some separate, later click. Now calls
    sync_line_observations_to_voucher() directly, right after the upsert - that helper is already
    idempotent (a no-op if nothing is pending) and marks entries synced as it goes, so a later
    save_line_items() call for the same line will correctly find nothing left to do; no double-
    append risk. Confirmed acceptable side effect: sync_line_observations_to_voucher() has no
    line-status gate (unlike save_line_items()), so an observation can now roll up even after the
    line has already been validated/completed - reasonable, since this only ever touches the
    observations field, not validation state.
    """
    line = execute_query_pg("SELECT id FROM verification_details WHERE id = %s", (voucher_line_id,))
    if not line:
        raise HTTPException(404, "Voucher line not found.")

    # ACCOUNT_HEAD_OBSERVATION_DOC_ID is a reserved sentinel, not a real audit_rule_documents row -
    # skip the existence/active check for it (see the constant's definition above).
    if document_id != ACCOUNT_HEAD_OBSERVATION_DOC_ID:
        doc = execute_query_pg("SELECT id FROM audit_rule_documents WHERE id = %s AND is_active = TRUE", (document_id,))
        if not doc:
            raise HTTPException(404, "Document not found or inactive.")

    execute_write_pg(
        """
        INSERT INTO voucher_line_observations (voucher_line_id, document_id, observation_text, synced, updated_by, updated_at)
        VALUES (%s, %s, %s, FALSE, %s, CURRENT_TIMESTAMP)
        ON CONFLICT(voucher_line_id, document_id)
        DO UPDATE SET observation_text = excluded.observation_text, synced = FALSE, updated_by = excluded.updated_by, updated_at = CURRENT_TIMESTAMP
        """,
        (voucher_line_id, document_id, observation_text, updated_by)
    )

    log_audit(updated_by, "Save Line Observation", "Verification",
               f"Saved observation for document {document_id} on voucher line {voucher_line_id}.")

    # Roll up immediately rather than waiting for the next save_line_items() call - see
    # docstring above. Runs before the final SELECT below so the response's own "synced" field
    # correctly reflects the roll-up that just happened, not a stale pre-sync value.
    sync_line_observations_to_voucher(voucher_line_id)

    updated = execute_query_pg(
        """
        SELECT voucher_line_id, document_id, observation_text, synced, updated_by, updated_at
        FROM voucher_line_observations
        WHERE voucher_line_id = %s AND document_id = %s
        """,
        (voucher_line_id, document_id)
    )[0]

    # Confirmed fix, 2026-07-25: also return the line's own current rolled-up Observation text
    # (post-sync, since the call above already ran) so the frontend can update the voucher
    # table's OBSERVATIONS cell immediately, without a full voucher reload.
    voucher_row = execute_query_pg(
        "SELECT observations FROM verification_details WHERE id = %s",
        (voucher_line_id,)
    )
    voucher_observations = voucher_row[0]['observations'] if voucher_row else None

    return {
        "voucher_line_id": updated['voucher_line_id'],
        "document_id": updated['document_id'],
        "observation_text": updated['observation_text'] or '',
        "synced": bool(updated['synced']),
        "updated_by": updated['updated_by'],
        "updated_at": updated['updated_at'],
        "voucher_observations": voucher_observations
    }


# ==============================================
# VOUCHER LINE PHYSICAL BILLS
# Physical Bill evidence type, Phase 5 of 8: save/read the manually-keyed Bill No/Date/
# Amount/Vendor name captured against a voucher line for a Billable Category's "physical_bill"
# Document(s). Plain CRUD only - no Amount-vs-voucher-line matching/gate logic here, that's
# Phase 6's job. Mirrors the voucher_line_observations endpoints directly above exactly:
# same Body(..., embed=True) param style (no Pydantic model - none exist anywhere in this
# file), same ON CONFLICT(...) DO UPDATE upsert (the table has a real
# UNIQUE(voucher_line_id, document_id) constraint, same shape as voucher_line_observations'
# own), and - critically - the same _resolve_sibling_voucher_line_ids() read pattern, per the
# schema's own comment in database.py: reopening a cashbook creates fresh verification_details
# rows, and skipping sibling resolution is the exact orphaning bug this module has already
# fixed three times for voucher_line_documents/_observations/_procedure_checks.
# ==============================================

# ---------- POPULATE PHYSICAL BILL FROM GRN/ERP VENDOR BILL (new, confirmed 4-phase
# requirement, Phase 2 of 4: backend preview + populate) ----------
# Lets an intern pre-fill a Physical Bill entry from an existing Store Receiving (GRN) or
# Vendor Bill (ERP) client_data row instead of typing Vendor Name/Bill No/Quantity/Rate by
# hand. source_type is supplied explicitly by the caller - this code never guesses which
# template a row belongs to, it only confirms the claim is correct. Amount is always
# computed (Qty x Rate) at save time, never trusted from a source column - same rule the
# now-removed "Create Vendor Bill from GRN" feature's resolver used to establish (Phase 4
# of 4 cleanup removed that superseded code entirely, see CLAUDE.md's Status log). Date is
# never resolved here - it stays a manual field, unaffected by this feature.
PHYSICAL_BILL_SOURCE_TYPES = ('grn', 'vendor_bill')

# Expected templates.template_name (case-insensitive/trimmed) for each source_type - same
# 'store receiving'/'vendor bill' comparison convention the now-removed "Create Vendor Bill
# from GRN" feature used to follow.
PHYSICAL_BILL_SOURCE_TEMPLATE_NAMES = {
    'grn': 'store receiving',
    'vendor_bill': 'vendor bill',
}

# Which column holds the source row's own bill number, per source type - a GRN's own
# "Bill no" field vs. the Vendor Bill template's own "vendor Bill no" field (the same column
# name the old resolver's target side already confirmed exists on that template).
PHYSICAL_BILL_SOURCE_BILL_NO_FIELD = {
    'grn': 'Bill no',
    'vendor_bill': 'vendor Bill no',
}


def resolve_physical_bill_fields_from_source(source_client_data_id: int, source_type: str) -> dict:
    """
    Read-only resolution of Vendor Name/Bill No/Quantity/Rate from a source GRN (Store
    Receiving) or ERP Vendor Bill client_data row, for populating a Physical Bill entry.
    No writes. Amount is deliberately NOT resolved here (computed at save time from
    Qty x Rate in save_physical_bill()); bill_date is never resolved here either - always
    left to manual entry.
    """
    if source_type not in PHYSICAL_BILL_SOURCE_TYPES:
        raise HTTPException(400, f"Invalid source_type '{source_type}'. Must be one of: {', '.join(PHYSICAL_BILL_SOURCE_TYPES)}")

    source_rows = execute_query_pg(
        "SELECT id, client_id, template_id, row_data, is_deleted FROM client_data WHERE id = %s",
        (source_client_data_id,)
    )
    if not source_rows:
        raise HTTPException(404, "Source row not found.")
    source_row = source_rows[0]
    if source_row['is_deleted']:
        raise HTTPException(400, "Source row has been deleted.")

    source_template = execute_query_pg("SELECT id, template_name FROM templates WHERE id = %s", (source_row['template_id'],))
    expected_name = PHYSICAL_BILL_SOURCE_TEMPLATE_NAMES[source_type]
    if not source_template or source_template[0]['template_name'].strip().lower() != expected_name:
        actual_name = source_template[0]['template_name'] if source_template else 'Unknown'
        raise HTTPException(400, f"Source row's template is '{actual_name}', expected '{expected_name.title()}' for source_type '{source_type}'.")

    try:
        source_data = json.loads(source_row['row_data']) if isinstance(source_row['row_data'], str) else (source_row['row_data'] or {})
    except Exception:
        source_data = {}

    source_cols = get_template_columns(source_row['template_id'])

    def _resolve_value(field_name):
        col = find_named_column(source_cols, field_name)
        if not col:
            return None, f"column '{field_name}' not configured on this template"
        value = source_data.get(col['column_name'])
        if value in (None, ''):
            return None, "blank on source row"
        return value, None

    vendor_name, vendor_skip_reason = _resolve_value('Vendor')
    bill_no, bill_no_skip_reason = _resolve_value(PHYSICAL_BILL_SOURCE_BILL_NO_FIELD[source_type])

    qty_raw, qty_skip_reason = _resolve_value('Quantity')
    rate_raw, rate_skip_reason = _resolve_value('Rate')
    try:
        quantity = float(qty_raw) if qty_raw is not None else None
    except (TypeError, ValueError):
        quantity = None
        qty_skip_reason = "non-numeric on source row"
    try:
        rate = float(rate_raw) if rate_raw is not None else None
    except (TypeError, ValueError):
        rate = None
        rate_skip_reason = "non-numeric on source row"

    amount_preview = None
    if quantity is not None and rate is not None:
        amount_preview = round(quantity * rate, get_rate_precision())

    fields_resolved = []
    fields_skipped = []
    for label, value, reason in (
        ("Vendor Name", vendor_name, vendor_skip_reason),
        ("Bill No", bill_no, bill_no_skip_reason),
        ("Quantity", quantity, qty_skip_reason),
        ("Rate", rate, rate_skip_reason),
    ):
        if value is not None:
            fields_resolved.append(label)
        else:
            fields_skipped.append({"field": label, "reason": reason or "not available"})

    return {
        "source_client_data_id": source_row['id'],
        "source_client_id": source_row['client_id'],
        "source_type": source_type,
        "source_template_id": source_row['template_id'],
        "source_template_name": source_template[0]['template_name'],
        "vendor_name": vendor_name,
        "bill_no": bill_no,
        "quantity": quantity,
        "rate": rate,
        "amount_preview": amount_preview,
        "fields_resolved": fields_resolved,
        "fields_skipped": fields_skipped,
    }


@router.get("/api/verification/physical_bill_source_preview/{client_data_id}")
def preview_physical_bill_from_source(client_data_id: int, source_type: str):
    """
    Read-only preview of what populating a Physical Bill entry from this GRN/ERP Vendor
    Bill row would produce - no writes. source_type ('grn'/'vendor_bill') is supplied
    explicitly by the caller, since this endpoint doesn't try to guess which template a
    row belongs to.
    """
    return resolve_physical_bill_fields_from_source(client_data_id, source_type)


def _fetch_physical_bill_items(bill_ids: List[int]) -> Dict[int, list]:
    """
    Batch-fetch every item row for the given bill ids in one query, grouped by bill_id and
    ordered sort_order/id. Returns {} without querying for an empty bill_ids list - both
    callers below already handle the "no bills" case upstream, but this stays defensive since
    an empty IN (...) is invalid SQL.
    """
    if not bill_ids:
        return {}
    placeholders = ",".join("%s" for _ in bill_ids)
    rows = execute_query_pg(
        f"""
        SELECT id, bill_id, item_name, quantity, rate, amount,
               source_document_row_id, source_template, sort_order, created_at, updated_at
        FROM voucher_line_physical_bill_items
        WHERE bill_id IN ({placeholders})
        ORDER BY bill_id, sort_order, id
        """,
        tuple(bill_ids)
    )
    items_by_bill: Dict[int, list] = {}
    for r in rows:
        items_by_bill.setdefault(r['bill_id'], []).append({
            "id": r['id'],
            "item_name": r['item_name'],
            "quantity": r['quantity'],
            "rate": r['rate'],
            "amount": r['amount'],
            "source_document_row_id": r['source_document_row_id'],
            "source_template": r['source_template'],
            "sort_order": r['sort_order'],
            "created_at": r['created_at'],
            "updated_at": r['updated_at'],
        })
    return items_by_bill


def _shape_physical_bill(header_row: dict, items: list) -> dict:
    """
    Builds one nested bill dict from a header row (bill_no/bill_date/vendor_name/... joined to
    audit_rule_documents) plus its already-fetched items list - the single place both
    save_physical_bill()'s response and get_voucher_line_physical_bills() build a bill dict, so
    their shapes stay byte-identical. bill_total is the sum of this bill's item amounts, or
    None if the bill has no items yet or any item's amount isn't resolvable (an incomplete
    bill has no meaningful subtotal) - mirrors _evaluate_physical_bill_completion()'s own
    "any unresolvable item amount blocks the total" rule, just surfaced for display here rather
    than as a pass/fail gate.
    """
    bill_total = None
    if items:
        numeric_amounts = [_cross_check_numeric(item['amount']) for item in items]
        if all(a is not None for a in numeric_amounts):
            bill_total = sum(numeric_amounts)
    document_is_active = header_row['document_is_active']
    return {
        "id": header_row['id'],
        "voucher_line_id": header_row['voucher_line_id'],
        "document_id": header_row['document_id'],
        "bill_no": header_row['bill_no'],
        "bill_date": header_row['bill_date'],
        "vendor_name": header_row['vendor_name'],
        "sort_order": header_row['sort_order'],
        "created_at": header_row['created_at'],
        "updated_at": header_row['updated_at'],
        "document_name": header_row['document_name'],
        "is_active": document_is_active if document_is_active is None else bool(document_is_active),
        "items": items,
        "bill_total": bill_total,
    }


def _fetch_physical_bills_for_document(voucher_line_id: int, document_id: int) -> dict:
    """
    Direct (non-sibling-resolved) read of every bill currently stored for exactly this
    (voucher_line_id, document_id) pair - used as save_physical_bill()'s response so a caller
    sees exactly what it just wrote, regardless of whether a sibling session might also have
    bills for the same document. get_voucher_line_physical_bills() below is the sibling-
    resolved batch read used everywhere else (Supporting Documents modal, Cleared/Query gate).
    """
    header_rows = execute_query_pg(
        """
        SELECT vpb.id, vpb.voucher_line_id, vpb.document_id, vpb.bill_no, vpb.bill_date,
               vpb.vendor_name, vpb.sort_order, vpb.created_at, vpb.updated_at,
               ard.name AS document_name, ard.is_active AS document_is_active
        FROM voucher_line_physical_bills vpb
        LEFT JOIN audit_rule_documents ard ON ard.id = vpb.document_id
        WHERE vpb.voucher_line_id = %s AND vpb.document_id = %s
        ORDER BY vpb.sort_order, vpb.id
        """,
        (voucher_line_id, document_id)
    )
    items_by_bill = _fetch_physical_bill_items([r['id'] for r in header_rows])
    bills = [_shape_physical_bill(r, items_by_bill.get(r['id'], [])) for r in header_rows]
    return {"voucher_line_id": voucher_line_id, "document_id": document_id, "bills": bills}


def _form_value_for_storage(field_type: str, value: Any, field_name: str):
    """Return one typed Form value as (text, date, numeric), or all None for blank input."""
    if value is None or value == '':
        return None, None, None
    if field_type == 'text':
        text = str(value).strip()
        return (text, None, None) if text else (None, None, None)
    if field_type == 'date':
        raw = str(value).strip()
        try:
            parsed = datetime.strptime(raw, '%Y-%m-%d').date()
        except (TypeError, ValueError):
            raise HTTPException(400, f"Form field '{field_name}' must be a valid date (YYYY-MM-DD).")
        return None, parsed, None
    if field_type == 'numeric':
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"Form field '{field_name}' must be numeric.")
        if not math.isfinite(numeric):
            raise HTTPException(400, f"Form field '{field_name}' must be numeric.")
        return None, None, numeric
    raise HTTPException(400, f"Unsupported Form field type '{field_type}'.")


def _replace_form_entries(voucher_line_id: int, document_id: int, raw_entries: Any) -> int:
    """Full-replace the manual evidence rows for one Form Document and voucher line."""
    line = execute_query_pg("SELECT id FROM verification_details WHERE id = %s", (voucher_line_id,))
    if not line:
        raise HTTPException(404, "Voucher line not found.")
    document = execute_query_pg(
        "SELECT id FROM audit_rule_documents WHERE id = %s AND is_active = TRUE AND document_type = 'form'",
        (document_id,)
    )
    if not document:
        raise HTTPException(404, "Form document not found or inactive.")
    if not isinstance(raw_entries, list):
        raise HTTPException(400, "entries must be a list.")

    field_rows = execute_query_pg(
        "SELECT id, field_name, field_type, field_scope FROM audit_rule_document_fields "
        "WHERE document_id = %s ORDER BY sort_order, id",
        (document_id,)
    )
    fields = {int(row['id']): row for row in field_rows}
    clean_entries = []
    for entry_index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            raise HTTPException(400, f"Form entry {entry_index + 1} must be an object.")
        row_scope = raw_entry.get('row_scope') or 'header'
        if row_scope not in ('header', 'item'):
            raise HTTPException(400, f"Form entry {entry_index + 1} has an invalid row scope.")
        try:
            sort_order = int(raw_entry.get('sort_order', entry_index))
        except (TypeError, ValueError):
            raise HTTPException(400, f"Form entry {entry_index + 1} has an invalid row order.")

        raw_values = raw_entry.get('values') or []
        if isinstance(raw_values, dict):
            raw_values = [{'field_id': key, 'value': value} for key, value in raw_values.items()]
        if not isinstance(raw_values, list):
            raise HTTPException(400, f"Form entry {entry_index + 1} values must be a list.")
        clean_values = []
        seen_fields = set()
        for value_index, raw_value in enumerate(raw_values):
            if not isinstance(raw_value, dict):
                raise HTTPException(400, f"Form entry {entry_index + 1}, value {value_index + 1} must be an object.")
            try:
                field_id = int(raw_value.get('field_id'))
            except (TypeError, ValueError):
                raise HTTPException(400, f"Form entry {entry_index + 1} has an invalid field.")
            field = fields.get(field_id)
            if not field:
                raise HTTPException(400, f"Field {field_id} does not belong to this Form document.")
            if field_id in seen_fields:
                raise HTTPException(400, f"Form field '{field['field_name']}' was supplied more than once.")
            if (field.get('field_scope') or 'header') != row_scope:
                raise HTTPException(400, f"Form field '{field['field_name']}' belongs to the {field.get('field_scope') or 'header'} section.")
            seen_fields.add(field_id)
            text_value, date_value, numeric_value = _form_value_for_storage(
                field.get('field_type') or 'text', raw_value.get('value'), field['field_name']
            )
            if text_value is not None or date_value is not None or numeric_value is not None:
                clean_values.append({
                    'field_id': field_id, 'value_text': text_value,
                    'value_date': date_value, 'value_numeric': numeric_value
                })
        if clean_values:
            clean_entries.append({'row_scope': row_scope, 'sort_order': sort_order, 'values': clean_values})

    execute_write_pg(
        "DELETE FROM voucher_line_form_entries WHERE voucher_line_id = %s AND document_id = %s",
        (voucher_line_id, document_id)
    )
    saved_count = 0
    for entry in clean_entries:
        entry_id = execute_write_pg(
            "INSERT INTO voucher_line_form_entries "
            "(voucher_line_id, document_id, row_scope, sort_order, updated_at) "
            "VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) RETURNING id",
            (voucher_line_id, document_id, entry['row_scope'], entry['sort_order'])
        )
        for value in entry['values']:
            execute_write_pg(
                "INSERT INTO voucher_line_form_values "
                "(entry_id, field_id, value_text, value_date, value_numeric, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
                (entry_id, value['field_id'], value['value_text'], value['value_date'], value['value_numeric'])
            )
        saved_count += 1
    return saved_count


def _shape_form_entries(rows: List[dict]) -> List[dict]:
    entries = {}
    for row in rows:
        entry_id = row['entry_id']
        entry = entries.setdefault(entry_id, {
            'id': entry_id, 'voucher_line_id': row['voucher_line_id'],
            'document_id': row['document_id'], 'row_scope': row['row_scope'],
            'sort_order': row['sort_order'], 'values': []
        })
        value = row['value_text']
        if (row.get('field_type') or 'text') == 'date':
            date_value = row.get('value_date')
            value = date_value.isoformat() if hasattr(date_value, 'isoformat') else date_value
        elif (row.get('field_type') or 'text') == 'numeric':
            value = float(row['value_numeric']) if row.get('value_numeric') is not None else None
        if value is not None:
            entry['values'].append({'field_id': row['field_id'], 'field_name': row['field_name'], 'value': value})
    return sorted(entries.values(), key=lambda entry: (entry['sort_order'], entry['id']))


def _fetch_form_entries_for_line(voucher_line_id: int, document_id: int) -> dict:
    rows = execute_query_pg(
        """SELECT e.id AS entry_id, e.voucher_line_id, e.document_id, e.row_scope, e.sort_order,
                  f.id AS field_id, f.field_name, f.field_type, v.value_text, v.value_date, v.value_numeric
           FROM voucher_line_form_entries e
           JOIN voucher_line_form_values v ON v.entry_id = e.id
           JOIN audit_rule_document_fields f ON f.id = v.field_id
           WHERE e.voucher_line_id = %s AND e.document_id = %s
           ORDER BY e.sort_order, e.id, f.sort_order, f.id""",
        (voucher_line_id, document_id)
    )
    return {'voucher_line_id': voucher_line_id, 'document_id': document_id,
            'entries': _shape_form_entries(rows)}


@router.post("/api/verification/save_form_entries")
def save_form_entries(
    voucher_line_id: int = Body(..., embed=True),
    document_id: int = Body(..., embed=True),
    entries: List[Dict[str, Any]] = Body(..., embed=True),
):
    """Full-replace manually entered Form evidence for one voucher line/document pair."""
    with pg_transaction():
        _replace_form_entries(voucher_line_id, document_id, entries)
        saved = _fetch_form_entries_for_line(voucher_line_id, document_id)
    return saved


@router.post("/api/verification/save_physical_bill")
def save_physical_bill(
    voucher_line_id: int = Body(..., embed=True, description="verification_details.id (the voucher line 'uid')"),
    document_id: int = Body(..., embed=True, description="audit_rule_documents.id (a physical_bill Document this bill belongs to)"),
    bills: List[Dict[str, Any]] = Body(..., embed=True, description="Full replacement bill set for this document - see docstring"),
    updated_by: str = Body("Analyst", embed=True)
):
    """
    Multi-Bill/Multi-Line Physical Bill, Phase 2 of 6: full-replace upsert of a Document's
    ENTIRE bill set for a voucher line - a repeat save always replaces every existing bill and
    item for (voucher_line_id, document_id) with exactly what's sent, never merges. Each
    `bills[]` entry: {bill_no, bill_date, vendor_name, items: [{item_name, quantity, rate,
    source_document_row_id, source_template}, ...]}. Supersedes the old one-bill-per-Document
    flat shape - Phase 1's schema already dropped the UNIQUE(voucher_line_id, document_id)
    constraint that used to limit a Document to exactly one bill.

    Writes always target the literal voucher_line_id passed in, never fanned out across
    sibling sessions - _resolve_sibling_voucher_line_ids()'s own documented contract (writes
    are unaffected by it, only reads are), same as every other voucher_line_* table.

    Per item: quantity/rate are optional (_parse_optional_float, same convention as before);
    amount is ALWAYS derived as Qty x Rate (rounded to get_rate_precision()) when both are
    given, else None - an item no longer accepts a standalone manual Amount the way the old
    single-bill row did, per the confirmed "Amount (derived)" field design. source_template, if
    given, is validated against PHYSICAL_BILL_SOURCE_TYPES.

    Per bill: bill_no/vendor_name are optional free text (blank stored as NULL, never
    rejected - same relaxed-validation fix carried over from before: the auto-save-on-exit
    flow must never fail just because a field is still blank mid-entry); bill_date, if given,
    must be a valid ISO date (YYYY-MM-DD).

    A bill that is entirely blank (no bill_no/date/vendor AND every item itself entirely
    blank) is silently dropped rather than persisted - keeps an unused trailing "Add Bill"/
    "Add line" UI placeholder from becoming a permanent phantom row on every auto-save tick.
    An item that is entirely blank within an otherwise real bill is dropped the same way (a
    trailing auto-added blank item row someone never filled in).

    No check here that the grand total matches the voucher line's own amount - that's the
    Cleared/Query gate's job (_evaluate_physical_bill_completion()), not this endpoint's.
    """
    line = execute_query_pg("SELECT id FROM verification_details WHERE id = %s", (voucher_line_id,))
    if not line:
        raise HTTPException(404, "Voucher line not found.")

    doc = execute_query_pg("SELECT id FROM audit_rule_documents WHERE id = %s AND is_active = TRUE", (document_id,))
    if not doc:
        raise HTTPException(404, "Document not found or inactive.")

    if not isinstance(bills, list):
        raise HTTPException(400, "bills must be a list.")

    def _parse_optional_float(value, field_label):
        if value in (None, ''):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{field_label} must be numeric.")

    def _is_blank_item(raw_item: dict) -> bool:
        return not (raw_item.get('item_name') or '').strip() \
            and raw_item.get('quantity') in (None, '') \
            and raw_item.get('rate') in (None, '') \
            and raw_item.get('source_document_row_id') in (None, '') \
            and not (raw_item.get('source_template') or '')

    cleaned_bills = []
    for bill in bills:
        if not isinstance(bill, dict):
            raise HTTPException(400, "Each bill must be a dict.")
        raw_items = bill.get('items') or []
        if not isinstance(raw_items, list):
            raise HTTPException(400, "Each bill's items must be a list.")

        parsed_items = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise HTTPException(400, "Each item must be a dict.")
            if _is_blank_item(raw_item):
                continue
            quantity = _parse_optional_float(raw_item.get('quantity'), "Quantity")
            rate = _parse_optional_float(raw_item.get('rate'), "Rate")
            amount = round(quantity * rate, get_rate_precision()) if (quantity is not None and rate is not None) else None
            source_template = raw_item.get('source_template') or None
            if source_template is not None and source_template not in PHYSICAL_BILL_SOURCE_TYPES:
                raise HTTPException(400, f"Invalid source_template '{source_template}'. Must be one of: {', '.join(PHYSICAL_BILL_SOURCE_TYPES)} or omitted.")
            parsed_items.append({
                "item_name": (raw_item.get('item_name') or '').strip() or None,
                "quantity": quantity,
                "rate": rate,
                "amount": amount,
                "source_document_row_id": raw_item.get('source_document_row_id') or None,
                "source_template": source_template,
            })

        bill_no = (bill.get('bill_no') or '').strip() or None
        vendor_name = (bill.get('vendor_name') or '').strip() or None
        bill_date = (bill.get('bill_date') or '').strip() or None
        if bill_date is not None:
            try:
                datetime.fromisoformat(bill_date)
            except (TypeError, ValueError):
                raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")

        if not bill_no and not bill_date and not vendor_name and not parsed_items:
            continue

        cleaned_bills.append({
            "bill_no": bill_no,
            "bill_date": bill_date,
            "vendor_name": vendor_name,
            "items": parsed_items,
        })

    conn = get_raw_conn_pg()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM voucher_line_physical_bills WHERE voucher_line_id = %s AND document_id = %s",
            (voucher_line_id, document_id)
        )
        existing_bill_ids = [r[0] for r in cur.fetchall()]
        if existing_bill_ids:
            item_placeholders = ",".join("%s" for _ in existing_bill_ids)
            cur.execute(
                f"DELETE FROM voucher_line_physical_bill_items WHERE bill_id IN ({item_placeholders})",
                existing_bill_ids
            )
        cur.execute(
            "DELETE FROM voucher_line_physical_bills WHERE voucher_line_id = %s AND document_id = %s",
            (voucher_line_id, document_id)
        )

        for bill_sort, bill in enumerate(cleaned_bills):
            # RETURNING id + fetchone() (Postgres cutover fix) - psycopg2 cursors have no
            # SQLite-style lastrowid attribute.
            cur.execute(
                """
                INSERT INTO voucher_line_physical_bills
                    (voucher_line_id, document_id, bill_no, bill_date, vendor_name, sort_order, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                RETURNING id
                """,
                (voucher_line_id, document_id, bill['bill_no'], bill['bill_date'], bill['vendor_name'], bill_sort)
            )
            bill_id = cur.fetchone()[0]
            for item_sort, item in enumerate(bill['items']):
                cur.execute(
                    """
                    INSERT INTO voucher_line_physical_bill_items
                        (bill_id, item_name, quantity, rate, amount, source_document_row_id,
                         source_template, sort_order, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    """,
                    (bill_id, item['item_name'], item['quantity'], item['rate'], item['amount'],
                     item['source_document_row_id'], item['source_template'], item_sort)
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_raw_conn_pg(conn)

    log_audit(updated_by, "Save Physical Bill", "Verification",
               f"Saved {len(cleaned_bills)} physical bill(s) for document {document_id} on voucher line {voucher_line_id}.")

    return _fetch_physical_bills_for_document(voucher_line_id, document_id)


def _get_voucher_line_physical_bills(voucher_line_id: int, sibling_ids: Optional[List[int]] = None):
    """
    Batch fetch every Physical Bill entry recorded for a voucher line, across all documents -
    one call per Supporting Documents modal open, mirroring get_line_observations()/
    get_voucher_line_documents(). Nested bill->items shape (Multi-Bill/Multi-Line Physical
    Bill, Phase 2 of 6) - a document can now have several bills, each with several items.

    Looks up across every verification_details.id that shares this line's own client_data_id
    (_resolve_sibling_voucher_line_ids()), not just voucher_line_id itself, so bills saved in
    an earlier verification session still show up when the same voucher line is reopened in a
    new one. Two sibling sessions could each have their own bill SET for the same document_id
    (the old UNIQUE constraint that prevented more than one bill within a single session is
    gone as of Phase 1, and never applied across sessions anyway) - dedup is now "latest-
    touched bill SET wins per document_id", generalized from the old "latest-updated row
    wins": the winning voucher_line_id for a document_id is whichever sibling session's bills
    for that document have the max(updated_at), and every bill returned for that document
    comes from that one winning session - never a mix of bills from two different sibling
    sessions under the same document.

    LEFT JOIN (not INNER) so a bill against a since-deactivated Document still surfaces with
    is_active: false rather than disappearing - matches this module's established
    "already-referenced inactive item stays visible, greyed" convention.
    """
    sibling_ids = sibling_ids if sibling_ids is not None else _resolve_sibling_voucher_line_ids(voucher_line_id)
    if sibling_ids is None:
        raise HTTPException(404, "Voucher line not found.")

    placeholders = ",".join("%s" for _ in sibling_ids)
    header_rows = execute_query_pg(
        f"""
        SELECT vpb.id, vpb.voucher_line_id, vpb.document_id, vpb.bill_no, vpb.bill_date,
               vpb.vendor_name, vpb.sort_order, vpb.created_at, vpb.updated_at,
               ard.name AS document_name, ard.is_active AS document_is_active
        FROM voucher_line_physical_bills vpb
        LEFT JOIN audit_rule_documents ard ON ard.id = vpb.document_id
        WHERE vpb.voucher_line_id IN ({placeholders})
        ORDER BY vpb.sort_order, vpb.id
        """,
        tuple(sibling_ids)
    )

    winning_session_by_doc: Dict[int, tuple] = {}
    for r in header_rows:
        candidate = (_ts_sort_key(r['updated_at']), r['voucher_line_id'])
        existing = winning_session_by_doc.get(r['document_id'])
        if existing is None or candidate > existing:
            winning_session_by_doc[r['document_id']] = candidate

    winning_rows = [
        r for r in header_rows
        if r['voucher_line_id'] == winning_session_by_doc[r['document_id']][1]
    ]

    items_by_bill = _fetch_physical_bill_items([r['id'] for r in winning_rows])
    bills = [_shape_physical_bill(r, items_by_bill.get(r['id'], [])) for r in winning_rows]
    return {"voucher_line_id": voucher_line_id, "bills": bills, "count": len(bills)}


@router.get("/api/verification/physical_bills/{voucher_line_id}")
def get_voucher_line_physical_bills(voucher_line_id: int):
    return _get_voucher_line_physical_bills(voucher_line_id)


@router.get("/api/verification/voucher_support_data/{voucher_line_id}")
def get_voucher_support_data(voucher_line_id: int, perf: bool = Query(False)):
    """Fetch one voucher's supporting data with set-based reads (one query per source)."""
    started = time.perf_counter()
    perf_token = start_db_metrics() if perf else None
    try:
        anchor = execute_query_pg(
            """
            SELECT vd.session_id, vd.transaction_ref, vs.client_id
            FROM verification_details vd
            JOIN verification_sessions vs ON vs.id = vd.session_id
            WHERE vd.id = %s
            """,
            (voucher_line_id,),
        )
        if not anchor:
            raise HTTPException(404, "Voucher line not found.")
        lines_rows = execute_query_pg(
            "SELECT id FROM verification_details WHERE session_id = %s AND transaction_ref = %s ORDER BY id",
            (anchor[0]['session_id'], anchor[0]['transaction_ref']),
        )
        ids = [r['id'] for r in lines_rows]
        ph = ",".join("%s" for _ in ids)
        sibling_rows = execute_query_pg(
            f"""SELECT id, client_data_id FROM verification_details
                WHERE id IN ({ph}) OR client_data_id IN
                (SELECT client_data_id FROM verification_details WHERE id IN ({ph}))""",
            tuple(ids + ids),
        ) if ids else []
        sibling_by_line = {r['id']: r['client_data_id'] for r in sibling_rows}
        cd_ids = list(dict.fromkeys(sibling_by_line.values()))
        cph = ",".join("%s" for _ in cd_ids)

        links = execute_query_pg(f"""SELECT vld.id, vld.voucher_line_id, vld.client_data_id,
            vd.client_data_id AS voucher_line_client_data_id,
            vld.document_id, vld.linked_by, vld.linked_at, vld.signature_exception,
            ard.name AS document_name, ard.is_active AS document_is_active, cd.row_data,
            cd.extract_date, cd.extract_amount, cd.rate_source
            FROM voucher_line_documents vld JOIN verification_details vd ON vd.id=vld.voucher_line_id
            JOIN client_data cd ON cd.id=vld.client_data_id LEFT JOIN audit_rule_documents ard ON ard.id=vld.document_id
            WHERE vd.client_data_id IN ({cph}) AND vld.status='active' ORDER BY vld.linked_at ASC""", tuple(cd_ids)) if cd_ids else []
        checks = execute_query_pg(f"""SELECT vd.client_data_id, vlpc.document_id, vlpc.role_name,
            vlpc.is_checked, vlpc.checked_by, vlpc.checked_at, ard.name AS document_name, ard.is_active AS document_is_active
            FROM voucher_line_procedure_checks vlpc JOIN verification_details vd ON vd.id=vlpc.voucher_line_id
            LEFT JOIN audit_rule_documents ard ON ard.id=vlpc.document_id WHERE vd.client_data_id IN ({cph})""", tuple(cd_ids)) if cd_ids else []
        obs = execute_query_pg(f"""SELECT vd.client_data_id, vlo.document_id, vlo.observation_text, vlo.synced,
            vlo.last_synced_text, vlo.updated_by, vlo.updated_at, ard.name AS document_name, ard.is_active AS document_is_active
            FROM voucher_line_observations vlo JOIN verification_details vd ON vd.id=vlo.voucher_line_id
            LEFT JOIN audit_rule_documents ard ON ard.id=vlo.document_id WHERE vd.client_data_id IN ({cph})""", tuple(cd_ids)) if cd_ids else []
        bills = execute_query_pg(f"""SELECT vd.client_data_id, vpb.id, vpb.voucher_line_id, vpb.document_id,
            vpb.bill_no, vpb.bill_date, vpb.vendor_name, vpb.sort_order, vpb.created_at, vpb.updated_at,
            ard.name AS document_name, ard.is_active AS document_is_active
            FROM voucher_line_physical_bills vpb JOIN verification_details vd ON vd.id=vpb.voucher_line_id
            LEFT JOIN audit_rule_documents ard ON ard.id=vpb.document_id WHERE vd.client_data_id IN ({cph})
            ORDER BY vpb.sort_order, vpb.id""", tuple(cd_ids)) if cd_ids else []
        bill_ids = [r['id'] for r in bills]
        items = execute_query_pg(f"SELECT id, bill_id, item_name, quantity, rate, amount, source_document_row_id, source_template, sort_order, created_at, updated_at FROM voucher_line_physical_bill_items WHERE bill_id IN ({','.join('%s' for _ in bill_ids)}) ORDER BY bill_id, sort_order, id", tuple(bill_ids)) if bill_ids else []
        forms = execute_query_pg(f"""SELECT vd.client_data_id, e.id AS entry_id, e.voucher_line_id,
            e.document_id, e.row_scope, e.sort_order, e.updated_at AS entry_updated_at,
            f.id AS field_id, f.field_name, f.field_type, v.value_text, v.value_date, v.value_numeric,
            ard.is_active AS document_is_active
            FROM voucher_line_form_entries e
            JOIN verification_details vd ON vd.id=e.voucher_line_id
            JOIN voucher_line_form_values v ON v.entry_id=e.id
            JOIN audit_rule_document_fields f ON f.id=v.field_id
            LEFT JOIN audit_rule_documents ard ON ard.id=e.document_id
            WHERE vd.client_data_id IN ({cph})
            ORDER BY e.document_id, e.sort_order, e.id, f.sort_order, f.id""", tuple(cd_ids)) if cd_ids else []

        latest_check = {}
        for r in checks:
            k=(r['client_data_id'],r['document_id'],r['role_name'])
            if k not in latest_check or _ts_sort_key(r['checked_at']) > _ts_sort_key(latest_check[k]['checked_at']): latest_check[k]=r
        latest_obs = {}
        for r in obs:
            k=(r['client_data_id'],r['document_id'])
            if k not in latest_obs or _ts_sort_key(r['updated_at']) > _ts_sort_key(latest_obs[k]['updated_at']): latest_obs[k]=r
        winning={}
        for r in bills:
            k=(r['client_data_id'],r['document_id']); candidate=(_ts_sort_key(r['updated_at']),r['voucher_line_id'])
            if k not in winning or candidate > winning[k]: winning[k]=candidate
        item_map={}
        for r in items:
            item_map.setdefault(r['bill_id'],[]).append({"id":r['id'],"item_name":r['item_name'],"quantity":r['quantity'],"rate":r['rate'],"amount":r['amount'],"source_document_row_id":r['source_document_row_id'],"source_template":r['source_template'],"sort_order":r['sort_order'],"created_at":r['created_at'],"updated_at":r['updated_at']})
        link_map={}; check_map={}; obs_map={}; bill_map={}; form_map={}; form_winning={}
        for r in links:
            d=json.loads(r['row_data']) if isinstance(r['row_data'],str) else r['row_data']
            # Group by the voucher line's stable identity. client_data_id in the payload remains
            # the linked evidence row's id; using that id as the map key made links disappear
            # after the procedure modal reopened.
            link_map.setdefault(r['voucher_line_client_data_id'],[]).append({"id":r['id'],"voucher_line_id":r['voucher_line_id'],"client_data_id":r['client_data_id'],"document_id":r['document_id'],"document_name":r['document_name'],"is_active":bool(r['document_is_active']),"linked_by":r['linked_by'],"linked_at":r['linked_at'],"signature_exception":bool(r['signature_exception']),"row_data":d,"extract_date":r['extract_date'],"extract_amount":r['extract_amount'],"rate_source":r['rate_source']})
        for r in latest_check.values(): check_map.setdefault(r['client_data_id'],[]).append({"document_id":r['document_id'],"role_name":r['role_name'],"is_checked":bool(r['is_checked']),"checked_by":r['checked_by'],"checked_at":r['checked_at'],"document_name":r['document_name'],"is_active":r['document_is_active'] if r['document_is_active'] is None else bool(r['document_is_active'])})
        for r in latest_obs.values(): obs_map.setdefault(r['client_data_id'],[]).append({"document_id":r['document_id'],"observation_text":r['observation_text'] or '',"synced":bool(r['synced']),"last_synced_text":r['last_synced_text'],"updated_by":r['updated_by'],"updated_at":r['updated_at'],"document_name":r['document_name'],"is_active":r['document_is_active'] if r['document_is_active'] is None else bool(r['document_is_active'])})
        for r in bills:
            if (_ts_sort_key(r['updated_at']),r['voucher_line_id']) == winning[(r['client_data_id'],r['document_id'])]: bill_map.setdefault(r['client_data_id'],[]).append(_shape_physical_bill(r,item_map.get(r['id'],[])))
        for r in forms:
            key = (r['client_data_id'], r['document_id'])
            candidate = (_ts_sort_key(r['entry_updated_at']), r['voucher_line_id'])
            if key not in form_winning or candidate > form_winning[key]:
                form_winning[key] = candidate
        winning_form_rows = [
            r for r in forms
            if (_ts_sort_key(r['entry_updated_at']), r['voucher_line_id']) == form_winning[(r['client_data_id'], r['document_id'])]
        ]
        form_rows_by_line_doc = {}
        for r in winning_form_rows:
            form_rows_by_line_doc.setdefault((r['client_data_id'], r['document_id']), []).append(r)
        for (client_data_id, document_id), rows in form_rows_by_line_doc.items():
            form_map.setdefault(client_data_id, []).append({
                'document_id': document_id,
                'is_active': rows[0]['document_is_active'] if rows[0]['document_is_active'] is None else bool(rows[0]['document_is_active']),
                'entries': _shape_form_entries(rows)
            })
        result={str(r['id']):{"linked_documents":{"voucher_line_id":r['id'],"documents":link_map.get(sibling_by_line[r['id']],[]),"count":len(link_map.get(sibling_by_line[r['id']],[]))},"procedure_checks":{"voucher_line_id":r['id'],"checks":check_map.get(sibling_by_line[r['id']],[])},"observations":{"voucher_line_id":r['id'],"entries":obs_map.get(sibling_by_line[r['id']],[])},"physical_bills":{"voucher_line_id":r['id'],"bills":bill_map.get(sibling_by_line[r['id']],[]),"count":len(bill_map.get(sibling_by_line[r['id']],[]))},"form_entries":{"voucher_line_id":r['id'],"documents":form_map.get(sibling_by_line[r['id']],[])}} for r in lines_rows}
        response={"voucher_line_id":voucher_line_id,"session_id":anchor[0]['session_id'],
                  "transaction_ref":anchor[0]['transaction_ref'],
                  "client_id":anchor[0].get('client_id'),"lines":result}
        if perf:
            response['_perf']={"total_ms":round((time.perf_counter()-started)*1000,1),**get_db_metrics()}
        return response
    finally:
        if perf_token is not None: stop_db_metrics(perf_token)


def _resolve_line_srno(session_id: int, target_voucher_line_id: int, session_details_cache: Dict[int, list]) -> int:
    """
    Voucher Supporting-Document Linking follow-up, Phase 2 of 4: backend equivalent of Phase 1's
    client-side Sr no. (renderVoucherLines() in verification.js, which numbers a voucher's lines
    1..N in the order they're rendered). Reuses _fetch_session_details_grouped() - the exact same
    grouping (_doc_group_id) and ordering (cd.id ASC) get_next_transaction()/get_voucher_nav()
    already build their lines[] arrays from - so this can never disagree with what Phase 1 shows
    for a line belonging to the currently-open voucher, and can also resolve a Sr no. for a line
    in a DIFFERENT voucher the frontend has no line-order data for at all.

    session_details_cache is shared across a whole get_assignment_status_map() call so a session
    with multiple matched rows only pays for one query, not one per row.

    Returns None (not 0) if the target line can't be found in its own session - a defensive
    fallback (e.g. a race where the line was since removed), not an expected outcome.
    """
    if session_id not in session_details_cache:
        session_details_cache[session_id] = _fetch_session_details_grouped(session_id)
    details = session_details_cache[session_id]

    target = next((d for d in details if d['id'] == target_voucher_line_id), None)
    if target is None:
        return None

    group_members = [d for d in details if d['_doc_group_id'] == target['_doc_group_id']]
    for idx, member in enumerate(group_members):
        if member['id'] == target_voucher_line_id:
            return idx + 1
    return None


def get_assignment_status_map(client_data_ids: List[int]) -> Dict[int, dict]:
    """
    Batch lookup of voucher_line_documents assignment status for a list of client_data ids -
    the same join get_document_assignment_status() used inline before Phase 2, now extracted
    so routers/data.py's GET /api/data/preview (Phase 2) can call it directly (no HTTP
    round-trip, no duplicated SQL) rather than only being reachable via this router's own
    endpoint. Returns {client_data_id: {"voucher_line_id": ..., "voucher_id": ..., "document_id": ...,
    "document_name": ..., "line_srno": ...}} - only for ids that are actually assigned; callers
    should treat any id missing from the returned dict as not assigned.

    document_id/document_name are included (bug fix) because a row can be assigned to a given
    voucher line under one Document "slot" (e.g. Purchase Order) while a different Document tab
    on that SAME voucher line (e.g. Vendor Bill, if it shares the same template_id) still shows
    that row in its own search/browse results. Without document_id, a caller can only tell
    "assigned to this voucher line" - not "assigned to this voucher line UNDER THIS DOCUMENT" -
    which previously caused the Browse modal (voucher_line match only) and the Linked Evidence
    table (voucher_line + document_id match) to disagree about the same row for the same line.

    line_srno (Voucher Supporting-Document Linking follow-up, Phase 2 of 4) is the assigned line's
    1-based position within its own voucher - see _resolve_line_srno() - so Browse can show
    "{voucher_id} [{line_srno}]" instead of the voucher number alone, making the assignment
    visibly verifiable against the voucher's own line order.

    voucher_line_client_data_id (Phase 3 of 4 bug fix) is the STABLE client_data_id of the
    assigned row's own voucher_line_id (vd.client_data_id, already joined) - since a new
    verification session recreates verification_details rows from scratch for every voucher,
    the assignment's own voucher_line_id is only meaningful within whichever session it was
    made under, so it can no longer be compared directly against a "currently open" line's
    voucher_line_id to decide "is this the same line" (see _resolve_sibling_voucher_line_ids()
    for the read-side of this same fix). Callers should compare voucher_line_client_data_id
    against the currently open line's own client_data_id instead.
    """
    if not client_data_ids:
        return {}
    placeholders = ",".join("%s" for _ in client_data_ids)
    rows = execute_query_pg(
        f"""
        SELECT vld.client_data_id, vld.voucher_line_id, vd.transaction_ref, vd.session_id,
               vd.client_data_id AS voucher_line_client_data_id,
               vld.document_id, ard.name AS document_name
        FROM voucher_line_documents vld
        JOIN verification_details vd ON vd.id = vld.voucher_line_id
        JOIN audit_rule_documents ard ON ard.id = vld.document_id
        WHERE vld.client_data_id IN ({placeholders}) AND vld.status = 'active'
        """,
        tuple(client_data_ids)
    )
    # Shared across every matched row so a session with several assigned lines only has its
    # details fetched/grouped once, not once per row - see _resolve_line_srno()'s own docstring.
    session_details_cache: Dict[int, list] = {}
    return {
        r['client_data_id']: {
            "voucher_line_id": r['voucher_line_id'],
            "voucher_id": r['transaction_ref'],
            "document_id": r['document_id'],
            "document_name": r['document_name'],
            "line_srno": _resolve_line_srno(r['session_id'], r['voucher_line_id'], session_details_cache),
            "voucher_line_client_data_id": r['voucher_line_client_data_id']
        }
        for r in rows
    }


@router.get("/api/verification/document_assignment_status")
def get_document_assignment_status(
    client_data_ids: str = Query(..., description="Comma-separated list of client_data.id values to check")
):
    """
    Batch lookup for search/preview contexts (e.g. Phase 2's Data Bank search): for each
    requested client_data id, reports whether it's already linked to a voucher line and,
    if so, which one - so an already-assigned row can be shown read-only ("assigned to
    voucher X") instead of being offered for assignment again. IDs that don't parse as
    integers are ignored; an id with no assignment simply comes back assigned: false.
    """
    try:
        ids = [int(x) for x in client_data_ids.split(",") if x.strip() != ""]
    except ValueError:
        raise HTTPException(400, "client_data_ids must be a comma-separated list of integers.")

    if not ids:
        return {"statuses": []}

    assigned_map = get_assignment_status_map(ids)

    statuses = []
    for cid in ids:
        match = assigned_map.get(cid)
        if match:
            statuses.append({
                "client_data_id": cid,
                "assigned": True,
                "voucher_id": match['voucher_id'],
                "voucher_line_id": match['voucher_line_id']
            })
        else:
            statuses.append({
                "client_data_id": cid,
                "assigned": False,
                "voucher_id": None,
                "voucher_line_id": None
            })
    return {"statuses": statuses}

# ==============================================
# 🔗 CHAIN-AWARE CROSS-DOCUMENT EVALUATION ENGINE
# ==============================================
# Automated cross-document validation engine, Phase 6 of 10.
#
# Given a voucher line, walks its Category's configured Cross-checks and produces a
# structured pass / fail / shortfall result per rule. Uses Phase 2's
# get_document_field_value() for field extraction and Phases 1/5's schema
# (comparison_type's 6 values, compare_to_voucher_line, the ordered
# audit_rule_cross_check_documents junction that IS the chain).
#
# "Chain-aware" means: only the documents ACTUALLY LINKED to this specific voucher line
# participate. A configured-but-unlinked document (e.g. an optional Purchase Demand a
# particular client doesn't raise) is skipped entirely - never a failure. The anchor is
# resolved live as the last linked document in chain order, never configured statically,
# so different clients running different-length chains each get a correct anchor.
#
# This phase WRITES NOTHING and DECIDES NOTHING. It has no endpoint and no call site - it is
# deliberately dead code until Phase 7, exactly as get_document_field_value() was until this
# phase. Phase 7 consumes the result for Cleared/Query, Phase 8 for shortfall observations,
# Phase 9 for the Data Table segment's pass/fail UI.
#
# Per Phase 2's contract, nothing here may raise on missing/unparseable data - an
# unevaluable rule returns a clear "not_evaluated" result rather than breaking the chain walk.

# Tolerance for the 'Within tolerance %' rule. audit_rule_cross_checks has NO per-rule
# tolerance column (confirmed against the Phase 1 schema), so this is a single global
# starting judgement call, not a fixed constant - same tunable-placeholder convention as
# ACCOUNT_HEAD_MATCH_THRESHOLD above. A later phase adding a real per-rule column should
# read that instead of this and leave this as the fallback default.
CROSS_CHECK_DEFAULT_TOLERANCE_PCT = 5.0

# Float-noise guard. Values come from Excel imports via JSON, so 100 and 100.0000001 must
# compare equal; a real audit difference is never this small.
CROSS_CHECK_FLOAT_EPSILON = 0.005

# The six configured comparison types (mirrors routers/audit_rules.py's COMPARISON_TYPES and
# the DB CHECK constraint). Kept as literals here rather than imported so this engine can't
# be silently broken by an unrelated edit to that tuple's ordering.
CC_ALL_EQUAL = 'All equal'
CC_MUST_BE_EQUAL = 'Must be equal'
CC_SEQ_INCREASING = 'Sequential increasing'
CC_SEQ_DECREASING = 'Sequential decreasing'
CC_WITHIN_TOLERANCE = 'Within tolerance %'
CC_MAY_BE_LESS = 'May be less, never more'


def _cross_check_numeric(value: Any) -> Optional[float]:
    """
    Coerce a raw row_data value into a float for comparison. Handles the shapes an Excel
    import realistically produces: real numbers, numeric strings, thousands separators, a
    leading currency symbol, surrounding whitespace. Returns None for anything unparseable
    (including None/'' and genuinely non-numeric text like a document number) - the caller
    treats None as "this document contributes nothing", not as zero.

    Never raises.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # A bool is an int subclass in Python; comparing True as 1.0 would be nonsense here.
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return None

    text = str(value).replace(',', '').strip()
    if not text:
        return None

    # Whole-string match only: an optional non-numeric prefix (currency symbol/label, e.g.
    # "Rs. 250", "$1200"), exactly one number, then an optional non-numeric suffix (unit, e.g.
    # "100 kg", "12.5%"). Requiring a FULL match is the point - a progressive strip-the-edges
    # approach silently turns a document number like "PD-0626-1866" into -626.0, fabricating a
    # comparison value out of an identifier. Anything containing a second run of digits fails
    # this match and correctly returns None ("not a number I can compare").
    # The prefix is LAZY and the number accepts a bare leading decimal point, so ".5" parses as
    # 0.5 rather than the prefix greedily swallowing the "." and leaving 5 - a silent 10x error.
    match = re.fullmatch(r'[^\d\-]*?(-?(?:\d+(?:\.\d+)?|\.\d+))\s*[^\d]*', text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _cross_check_values_equal(a: float, b: float) -> bool:
    """Equality within CROSS_CHECK_FLOAT_EPSILON - see that constant for why."""
    return abs(a - b) <= CROSS_CHECK_FLOAT_EPSILON


def _evaluate_physical_bill_completion(bills: List[dict], voucher_amount: Optional[float]) -> bool:
    """
    Shared hard-lock check for one physical_bill Document's requirement (Multi-Bill/Multi-Line
    Physical Bill, Phase 2 of 6) - used both by compute_and_persist_cleared_status()'s per-line
    gate and get_audit_workbook()'s pre-batched Procedures-pill computation, so the two can never
    disagree. No partial credit: every bill in `bills` must have non-blank bill_no/bill_date/
    vendor_name and at least one item; every item must resolve to a numeric amount
    (_cross_check_numeric); the SUM of every item's amount across every bill must exactly equal
    `voucher_amount` (_cross_check_values_equal - float-safety epsilon only, no rounding
    tolerance). One incomplete bill or item fails the whole document.

    `bills` is a list of {"bill_no", "bill_date", "vendor_name", "items": [<raw amount>, ...]}
    dicts - callers adapt their own row shape (a real nested bill dict, or a lighter dict built
    from a batch join) into this shape before calling; this function only ever reads those 4 keys.
    """
    if not bills or voucher_amount is None:
        return False
    grand_total = 0.0
    for bill in bills:
        if not (bill.get('bill_no') or '').strip():
            return False
        bill_date = bill.get('bill_date')
        # Postgres returns DATE columns as datetime.date, while older/imported values may be
        # strings. Normalize both forms before applying the existing non-blank rule.
        if bill_date is None or not str(bill_date).strip():
            return False
        if not (bill.get('vendor_name') or '').strip():
            return False
        items = bill.get('items') or []
        if not items:
            return False
        for raw_amount in items:
            item_amount = _cross_check_numeric(raw_amount)
            if item_amount is None:
                return False
            grand_total += item_amount
    return _cross_check_values_equal(grand_total, voucher_amount)


def _rulebook_cross_check_configs(category_id: int, rulebook: dict) -> Optional[List[dict]]:
    """Adapt one compiled Category profile to the evaluator's stable internal shape."""
    profile = next(
        (category for category in (rulebook or {}).get('categories', [])
         if category.get('category_id') == category_id),
        None,
    )
    if profile is None:
        return None

    configs = []
    for check in profile.get('cross_checks', []):
        matrix = check.get('matrix') or {}
        matrix_documents = matrix.get('documents') or check.get('document_chain') or []
        configs.append({
            "id": check.get('cross_check_id', check.get('id')),
            "name": check.get('name'),
            "compare_field": check.get('compare_field'),
            "comparison_type": check.get('comparison_type'),
            "compare_to_voucher_line": bool(check.get('compare_to_voucher_line')),
            "documents": [
                {
                    "document_id": document.get('document_id'),
                    "document_name": document.get('name') or document.get('document_name'),
                    "sort_order": document.get('sort_order', index),
                }
                for index, document in enumerate(matrix_documents)
            ],
            "matrix_fields": matrix.get('fields') or [],
        })
    return configs


def _get_category_cross_check_configs(category_id: int, rulebook: Optional[dict] = None) -> List[dict]:
    """
    Every ACTIVE Cross-check attached to a Category, with its ordered Document chain.

    Read directly here rather than via routers/audit_rules.py's
    get_category_guidance_cross_checks() - that helper returns document NAMES only (no ids)
    and doesn't expose compare_to_voucher_line, so it can't drive an evaluation. Matches this
    codebase's documented convention of duplicating a small cross-cutting lookup per-router
    rather than reshaping another router's already-live helper for one caller.

    documents[] order is audit_rule_cross_check_documents.sort_order - that ordering IS the
    chain, and the whole engine's direction logic depends on it. The rule rows and all document
    chains are fetched in two set-based reads on a cache miss; do not reintroduce one document
    query per Cross-check here.
    """
    if rulebook is not None:
        compiled = _rulebook_cross_check_configs(category_id, rulebook)
        if compiled is not None:
            return compiled

    cache_key = f"cross_check_configs:{category_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    rule_rows = execute_query_pg(
        f"""
        SELECT c.id, c.name, c.compare_field, c.comparison_type, c.compare_to_voucher_line
        FROM audit_rule_category_cross_checks cc
        JOIN audit_rule_cross_checks c ON c.id = cc.cross_check_id
        WHERE cc.category_id = %s AND c.is_active = TRUE
          AND NOT EXISTS (
              SELECT 1
              FROM
                  audit_rule_cross_check_documents invalid_ccd
              LEFT JOIN audit_rule_category_documents category_doc
                ON category_doc.category_id = {int(category_id)}
               AND category_doc.document_id = invalid_ccd.document_id
              LEFT JOIN audit_rule_documents dependency_doc
                ON dependency_doc.id = invalid_ccd.document_id
              WHERE invalid_ccd.cross_check_id = c.id
                AND (category_doc.document_id IS NULL OR dependency_doc.is_active IS NOT TRUE)
          )
        ORDER BY cc.sort_order
        """,
        (category_id,)
    )

    cross_check_ids = [r['id'] for r in rule_rows]
    documents_by_cross_check = {cross_check_id: [] for cross_check_id in cross_check_ids}
    if cross_check_ids:
        placeholders = ",".join("%s" for _ in cross_check_ids)
        doc_rows = execute_query_pg(
            f"""
            SELECT ccd.cross_check_id, ccd.document_id, ccd.sort_order, d.name AS document_name
            FROM audit_rule_cross_check_documents ccd
            JOIN audit_rule_documents d ON d.id = ccd.document_id
            WHERE ccd.cross_check_id IN ({placeholders})
            ORDER BY ccd.cross_check_id, ccd.sort_order
            """,
            tuple(cross_check_ids)
        )
        for d in doc_rows:
            documents_by_cross_check[d['cross_check_id']].append(d)

    configs = []
    for r in rule_rows:
        doc_rows = documents_by_cross_check[r['id']]
        configs.append({
            "id": r['id'],
            "name": r['name'],
            "compare_field": r['compare_field'],
            "comparison_type": r['comparison_type'],
            "compare_to_voucher_line": bool(r['compare_to_voucher_line'] or 0),
            "documents": [
                {
                    "document_id": d['document_id'],
                    "document_name": d['document_name'],
                    "sort_order": d['sort_order'],
                }
                for d in doc_rows
            ],
        })
    cache_set(cache_key, configs)
    return configs


def _get_linked_evidence_by_document(voucher_line_id: int, sibling_ids: Optional[List[int]] = None) -> Dict[int, List[int]]:
    """
    {document_id: [client_data_id, ...]} for every ACTIVELY linked evidence row on this
    voucher line. Resolves sibling verification_details ids first (see
    _resolve_sibling_voucher_line_ids()) so evidence linked in an earlier verification
    session of the same cashbook still counts - same convention
    compute_and_persist_cleared_status() and get_voucher_line_documents() already follow.
    Pass an already-resolved sibling_ids to skip that re-resolve.

    Returns {} for an unknown voucher line rather than raising.
    """
    if sibling_ids is None:
        sibling_ids = _resolve_sibling_voucher_line_ids(voucher_line_id)
    if not sibling_ids:
        return {}

    placeholders = ",".join("%s" for _ in sibling_ids)
    rows = execute_query_pg(
        f"""
        SELECT document_id, client_data_id
        FROM voucher_line_documents
        WHERE voucher_line_id IN ({placeholders}) AND status = 'active'
        ORDER BY linked_at ASC
        """,
        tuple(sibling_ids)
    )

    linked: Dict[int, List[int]] = {}
    for r in rows:
        linked.setdefault(r['document_id'], []).append(r['client_data_id'])
    return linked


def _sum_document_field(client_data_ids: List[int], field_name: str,
                        physical_bill_values: Optional[dict] = None):
    """
    Sum one named field across every evidence row linked for a single Document, via Phase 2's
    get_document_field_value() (unchanged). Summing (rather than taking one row) matches the
    Linked Evidence table's existing totals-row behaviour - a Purchase Order split across
    three lines of 40/30/30 must compare as 100, not as one arbitrary line.

    Returns (total, contributing_row_count). (None, 0) means no row yielded a usable numeric
    value - the caller's "cannot evaluate this document" signal, deliberately distinct from a
    genuine total of 0.
    """
    if physical_bill_values is not None:
        return physical_bill_values.get(field_name), physical_bill_values.get('_row_count', 0)
    total = None
    count = 0
    for client_data_id in client_data_ids or []:
        raw = get_document_field_value(client_data_id, field_name)
        numeric = _cross_check_numeric(raw)
        if numeric is None:
            continue
        total = numeric if total is None else total + numeric
        count += 1
    return total, count


def _matrix_condition_matches(left: float, right: float, operator: str, tolerance: float) -> bool:
    tolerance = max(float(tolerance or 0), 0)
    if operator == '=':
        return abs(left - right) <= tolerance + CROSS_CHECK_FLOAT_EPSILON
    if operator == '!=':
        return abs(left - right) > tolerance + CROSS_CHECK_FLOAT_EPSILON
    if operator == '>=':
        return left >= right - tolerance - CROSS_CHECK_FLOAT_EPSILON
    if operator == '<=':
        return left <= right + tolerance + CROSS_CHECK_FLOAT_EPSILON
    if operator == '>':
        return left > right + tolerance + CROSS_CHECK_FLOAT_EPSILON
    if operator == '<':
        return left < right - tolerance - CROSS_CHECK_FLOAT_EPSILON
    return False


def _evaluate_matrix_cross_check(config: dict, linked_by_document: Dict[int, List[int]],
                                 voucher_amount: Optional[float],
                                 physical_bill_values: Optional[Dict[int, dict]] = None) -> dict:
    """Evaluate the reusable Cross-check matrix while returning the legacy result shape."""
    documents = config.get('documents') or []
    matrix_fields = config.get('matrix_fields') or []
    chain = [{
        "document_id": document['document_id'],
        "document_name": document['document_name'],
        "sort_order": document['sort_order'],
        "linked": bool(linked_by_document.get(document['document_id'])),
        "value": None,
        "row_count": 0,
    } for document in documents]
    chain_by_id = {entry['document_id']: entry for entry in chain}
    matrix_rows = []
    failures = []
    shortfalls = []
    has_not_evaluated = False

    for field in matrix_fields:
        cells = field.get('cells') or {}
        configured_cells = []
        missing_cells = []
        for document in documents:
            column_name = cells.get(str(document['document_id']))
            if not column_name:
                continue
            physical_values = (physical_bill_values or {}).get(document['document_id'])
            if physical_values is None:
                # Keep the original two-argument call shape for ordinary template documents;
                # this also preserves compatibility with isolated unit-test doubles.
                value, row_count = _sum_document_field(
                    linked_by_document.get(document['document_id'], []), column_name
                )
            else:
                value, row_count = _sum_document_field(
                    linked_by_document.get(document['document_id'], []), column_name, physical_values
                )
            cell = {
                "document_id": document['document_id'],
                "document_name": document['document_name'],
                "field_name": column_name,
                "value": value,
                "row_count": row_count,
                "linked": bool(linked_by_document.get(document['document_id'])),
            }
            configured_cells.append(cell)
            if value is None:
                missing_cells.append(cell)
            elif chain_by_id[document['document_id']]['value'] is None:
                chain_by_id[document['document_id']]['value'] = value
                chain_by_id[document['document_id']]['row_count'] = row_count

        row_result = {
            "field_id": field.get('id'),
            "sort_order": field.get('sort_order', 0),
            "operator": field.get('operator') or '=',
            "tolerance": float(field.get('tolerance') or 0),
            "missing_data_behavior": field.get('missing_data_behavior') or 'not_configured',
            "blocking": bool(field.get('blocking')),
            "risk": field.get('risk') or 'Medium',
            "cells": cells,
            "values": configured_cells,
            "status": 'not_evaluated',
            "message": '',
        }
        if len(configured_cells) < 2:
            has_not_evaluated = True
            row_result['message'] = 'At least two document fields are required.'
        elif missing_cells:
            behavior = row_result['missing_data_behavior']
            message = 'One or more selected fields are missing or not linked.'
            if behavior in ('not_configured', 'not_linked'):
                has_not_evaluated = True
                row_result['message'] = message
            elif row_result['blocking']:
                failures.append(f"Matrix row {row_result['sort_order'] + 1}: {message}")
                row_result['status'] = 'fail'
                row_result['message'] = message
            else:
                shortfalls.append({
                    "row_id": row_result['field_id'],
                    "message": message,
                    "risk": row_result['risk'],
                })
                row_result['status'] = 'shortfall'
                row_result['message'] = message
        else:
            comparisons = []
            for left, right in zip(configured_cells, configured_cells[1:]):
                matches = _matrix_condition_matches(
                    left['value'], right['value'], row_result['operator'], row_result['tolerance']
                )
                comparisons.append((left, right, matches))
            failed_comparisons = [comparison for comparison in comparisons if not comparison[2]]
            if failed_comparisons:
                left, right, _ = failed_comparisons[0]
                message = (
                    f"{left['document_name']} {left['field_name']} {left['value']:g} "
                    f"{row_result['operator']} {right['document_name']} {right['field_name']} "
                    f"{right['value']:g} condition failed."
                )
                row_result['message'] = message
                if row_result['blocking']:
                    failures.append(f"Matrix row {row_result['sort_order'] + 1}: {message}")
                    row_result['status'] = 'fail'
                else:
                    shortfalls.append({
                        "row_id": row_result['field_id'],
                        "message": message,
                        "risk": row_result['risk'],
                    })
                    row_result['status'] = 'shortfall'
            else:
                row_result['status'] = 'pass'
                row_result['message'] = 'Condition passed.'
        matrix_rows.append(row_result)

    anchor = next((entry for entry in reversed(chain) if entry['value'] is not None), None)
    result = {
        "cross_check_id": config['id'],
        "name": config['name'],
        "compare_field": None,
        "comparison_type": 'matrix',
        "compare_to_voucher_line": bool(config.get('compare_to_voucher_line')),
        "status": 'not_evaluated',
        "message": '',
        "anchor_document_id": anchor['document_id'] if anchor else None,
        "anchor_document_name": anchor['document_name'] if anchor else None,
        "chain": chain,
        "matrix_rows": matrix_rows,
        "voucher_line_comparison": None,
        "failures": failures,
        "shortfalls": shortfalls,
        "risk": max((row['risk'] for row in matrix_rows), key=lambda value: {'Low': 1, 'Medium': 2, 'High': 3}.get(value, 0), default=None),
    }
    if config.get('compare_to_voucher_line') and anchor is not None:
        if voucher_amount is not None:
            match = _cross_check_values_equal(anchor['value'], voucher_amount)
            result['voucher_line_comparison'] = {
                "anchor_value": anchor['value'], "voucher_amount": voucher_amount, "match": match,
            }
            if not match:
                result['failures'].append(
                    f"{anchor['document_name']} does not equal the voucher line amount {voucher_amount:g}"
                )
        else:
            result['voucher_line_comparison'] = {
                "anchor_value": anchor['value'], "voucher_amount": None, "match": None,
            }

    if result['failures']:
        result['status'] = 'fail'
        result['message'] = '; '.join(result['failures'])
    elif result['shortfalls']:
        result['status'] = 'shortfall'
        result['message'] = '; '.join(item['message'] for item in result['shortfalls'])
    elif has_not_evaluated or not matrix_rows:
        result['status'] = 'not_evaluated'
        result['message'] = 'The matrix does not have enough complete data to evaluate.'
    else:
        result['status'] = 'pass'
        result['message'] = 'All matrix conditions passed.'
    return result


def _evaluate_single_cross_check(config: dict, linked_by_document: Dict[int, List[int]],
                                 voucher_amount: Optional[float],
                                 physical_bill_values: Optional[Dict[int, dict]] = None) -> dict:
    """
    Evaluate one Cross-check against one voucher line's linked evidence. Pure - reads no
    global state, writes nothing.

    Status precedence is fail > shortfall > pass > not_evaluated: an excess anywhere in the
    chain outranks a shortfall elsewhere, because an excess is a hard control failure while a
    shortfall is a reportable observation (Phase 8) rather than a rejection.
    """
    if config.get('matrix_fields'):
        return _evaluate_matrix_cross_check(config, linked_by_document, voucher_amount, physical_bill_values)

    compare_field = config['compare_field']
    comparison_type = config['comparison_type']

    # --- Build the chain: every configured document in order, linked ones carrying values. ---
    chain = []
    for doc in config['documents']:
        evidence_ids = linked_by_document.get(doc['document_id'], [])
        is_linked = len(evidence_ids) > 0
        value, row_count = (None, 0)
        if is_linked:
            value, row_count = _sum_document_field(evidence_ids, compare_field)
        chain.append({
            "document_id": doc['document_id'],
            "document_name": doc['document_name'],
            "sort_order": doc['sort_order'],
            "linked": is_linked,
            "value": value,
            "row_count": row_count,
        })

    # --- Anchor: the LAST linked document in chain order, resolved live. A configured-but-
    # unlinked trailing document must not become the anchor, which is exactly why this walks
    # backwards over `linked` rather than just taking chain[-1]. ---
    anchor = next((c for c in reversed(chain) if c['linked']), None)

    result = {
        "cross_check_id": config['id'],
        "name": config['name'],
        "compare_field": compare_field,
        "comparison_type": comparison_type,
        "compare_to_voucher_line": config['compare_to_voucher_line'],
        "status": "not_evaluated",
        "message": "",
        "anchor_document_id": anchor['document_id'] if anchor else None,
        "anchor_document_name": anchor['document_name'] if anchor else None,
        "chain": chain,
        "voucher_line_comparison": None,
        "failures": [],
        "shortfalls": [],
    }

    # Only linked documents with an extractable value participate in the comparison. An
    # unlinked document is skipped silently (chain-aware behaviour); a linked document whose
    # field can't be extracted is skipped too, but is still visible in `chain` with value None
    # so Phase 9's UI can show why.
    comparable = [c for c in chain if c['linked'] and c['value'] is not None]

    failures: List[str] = []
    shortfalls: List[dict] = []

    if len(comparable) >= 2:
        if comparison_type in (CC_ALL_EQUAL, CC_MUST_BE_EQUAL):
            baseline = comparable[0]
            for other in comparable[1:]:
                if not _cross_check_values_equal(baseline['value'], other['value']):
                    failures.append(
                        f"{other['document_name']} {compare_field} {other['value']:g} "
                        f"does not equal {baseline['document_name']} {baseline['value']:g}"
                    )

        elif comparison_type == CC_SEQ_INCREASING:
            for earlier, later in zip(comparable, comparable[1:]):
                if not (later['value'] > earlier['value'] + CROSS_CHECK_FLOAT_EPSILON):
                    failures.append(
                        f"{later['document_name']} {compare_field} {later['value']:g} is not greater "
                        f"than {earlier['document_name']} {earlier['value']:g}"
                    )

        elif comparison_type == CC_SEQ_DECREASING:
            for earlier, later in zip(comparable, comparable[1:]):
                if not (later['value'] < earlier['value'] - CROSS_CHECK_FLOAT_EPSILON):
                    failures.append(
                        f"{later['document_name']} {compare_field} {later['value']:g} is not less "
                        f"than {earlier['document_name']} {earlier['value']:g}"
                    )

        elif comparison_type == CC_WITHIN_TOLERANCE:
            baseline = comparable[0]
            base_value = baseline['value']
            if abs(base_value) <= CROSS_CHECK_FLOAT_EPSILON:
                # A zero baseline makes a percentage meaningless - fall back to plain equality
                # rather than dividing by zero or silently passing everything.
                for other in comparable[1:]:
                    if not _cross_check_values_equal(base_value, other['value']):
                        failures.append(
                            f"{other['document_name']} {compare_field} {other['value']:g} differs from a "
                            f"zero baseline on {baseline['document_name']}"
                        )
            else:
                allowed = abs(base_value) * (CROSS_CHECK_DEFAULT_TOLERANCE_PCT / 100.0)
                for other in comparable[1:]:
                    diff = abs(other['value'] - base_value)
                    if diff > allowed + CROSS_CHECK_FLOAT_EPSILON:
                        pct = (diff / abs(base_value)) * 100.0
                        failures.append(
                            f"{other['document_name']} {compare_field} {other['value']:g} is {pct:.2f}% from "
                            f"{baseline['document_name']} {base_value:g}, over the "
                            f"{CROSS_CHECK_DEFAULT_TOLERANCE_PCT:g}% tolerance"
                        )

        elif comparison_type == CC_MAY_BE_LESS:
            # CONFIRMED DIRECTION (do not invert without re-confirming - getting this backwards
            # silently reverses the audit conclusion rather than erroring):
            #   later-in-chain <= earlier-in-chain.
            #   later <  earlier -> SHORTFALL (soft; reported, not a rejection - Phase 8 writes
            #                       an observation for it)
            #   later >  earlier -> EXCESS (hard fail - more was received/billed than authorised)
            #   equal            -> OK
            # e.g. Purchase Demand 100 -> Purchase Order 100 -> Store Receiving 95 is a
            # legitimate short delivery; Store Receiving 105 is an excess and must fail.
            for earlier, later in zip(comparable, comparable[1:]):
                if later['value'] > earlier['value'] + CROSS_CHECK_FLOAT_EPSILON:
                    failures.append(
                        f"{later['document_name']} {compare_field} {later['value']:g} exceeds "
                        f"{earlier['document_name']} {earlier['value']:g}"
                    )
                elif later['value'] < earlier['value'] - CROSS_CHECK_FLOAT_EPSILON:
                    shortfalls.append({
                        "from_document": earlier['document_name'],
                        "to_document": later['document_name'],
                        "from_value": earlier['value'],
                        "to_value": later['value'],
                        "difference": earlier['value'] - later['value'],
                    })

        else:
            # Unknown comparison_type - shouldn't happen (DB CHECK + router validation both
            # constrain it), but must degrade cleanly rather than silently passing.
            result['status'] = "not_evaluated"
            result['message'] = f"Unknown comparison type '{comparison_type}' - not evaluated."
            return result

    # --- Voucher-line comparison (Phase 5's compare_to_voucher_line flag). Always against the
    # LIVE-resolved anchor, never a statically configured document. Exact equality, no
    # tolerance, per the confirmed Phase 5 design. ---
    if config['compare_to_voucher_line']:
        anchor_value = anchor['value'] if anchor else None
        if anchor_value is None or voucher_amount is None:
            result['voucher_line_comparison'] = {
                "anchor_value": anchor_value,
                "voucher_amount": voucher_amount,
                "match": None,
            }
        else:
            match = _cross_check_values_equal(anchor_value, voucher_amount)
            result['voucher_line_comparison'] = {
                "anchor_value": anchor_value,
                "voucher_amount": voucher_amount,
                "match": match,
            }
            if not match:
                failures.append(
                    f"{anchor['document_name']} {compare_field} {anchor_value:g} does not equal the "
                    f"voucher line amount {voucher_amount:g}"
                )

    result['failures'] = failures
    result['shortfalls'] = shortfalls

    # --- Roll up. A rule with nothing comparable AND no usable voucher-line comparison is
    # "not_evaluated", not a pass - passing it would silently assert a control was satisfied
    # when it was never actually checked. ---
    had_voucher_comparison = bool(
        result['voucher_line_comparison'] and result['voucher_line_comparison']['match'] is not None
    )
    if failures:
        result['status'] = "fail"
        result['message'] = "; ".join(failures)
    elif shortfalls:
        result['status'] = "shortfall"
        result['message'] = "; ".join(
            f"{s['to_document']} short of {s['from_document']} by {s['difference']:g}"
            for s in shortfalls
        )
    elif len(comparable) >= 2 or had_voucher_comparison:
        result['status'] = "pass"
        result['message'] = "All comparisons passed."
    else:
        linked_count = sum(1 for c in chain if c['linked'])
        if linked_count == 0:
            result['message'] = "No configured document is linked to this voucher line."
        else:
            result['message'] = (
                f"Not enough comparable values for '{compare_field}' "
                f"({len(comparable)} of {linked_count} linked documents yielded a value)."
            )

    return result


def evaluate_cross_checks_for_line(voucher_line_id: int, category_id: Optional[int] = None,
                                    sibling_ids: Optional[List[int]] = None,
                                    voucher_amount: Optional[float] = None,
                                    linked_by_document: Optional[Dict[int, List[int]]] = None,
                                    rulebook: Optional[dict] = None) -> dict:
    """
    Chain-aware cross-document evaluation for one voucher line (Phase 6 of 10).

    category_id is optional so Phase 7 can pass the Category it has ALREADY resolved inside
    compute_and_persist_cleared_status(), guaranteeing the two can never disagree. When
    omitted, it's resolved here the same way that function does (account_category_assignments
    keyed client_id + account code). Note this is deliberately NOT the newer latest-Trial-
    Balance-first lookup used by GET /api/verification/category-for-account - that 2026-07-30
    fix only changed the endpoint, and matching the status engine matters more here than
    matching the endpoint. Reconciling those two is a separate, pre-existing concern.

    sibling_ids is likewise optional, passed straight through to _get_linked_evidence_by_
    document() - lets compute_and_persist_cleared_status() share its own already-resolved list
    instead of a second redundant resolve.

    voucher_amount is likewise optional - when the caller already has it (compute_and_persist_
    cleared_status() always does) AND category_id is also supplied, the line-fetch query below
    is skipped entirely, since with category_id given that query's only remaining use was
    re-deriving this same value (client_id from that query is otherwise dead once category_id
    is supplied - the category-resolution branch that needs it never runs on that path). Any
    caller passing category_id without voucher_amount (or vice versa) still gets the query, so
    behavior for get_cross_check_results() and any other partial-argument caller is unchanged.

    Returns a structured result; writes nothing, persists nothing, decides nothing. Never
    raises - every failure path returns evaluated=False with a reason.
    """
    empty_summary = {"passed": 0, "failed": 0, "shortfall": 0, "not_evaluated": 0}

    row = None
    if category_id is None or voucher_amount is None:
        line = execute_query_pg(
            """
            SELECT vd.id, vd.session_id, vd.transaction_ref, cd.row_data, cd.extract_amount, vs.client_id
            FROM verification_details vd
            JOIN client_data cd ON cd.id = vd.client_data_id
            JOIN verification_sessions vs ON vs.id = vd.session_id
            WHERE vd.id = %s
            """,
            (voucher_line_id,)
        )
        if not line:
            return {
                "voucher_line_id": voucher_line_id,
                "category_id": None, "category_name": None,
                "evaluated": False, "reason": "Voucher line not found.",
                "summary": dict(empty_summary), "results": [],
            }
        row = line[0]
        if voucher_amount is None:
            voucher_amount = _cross_check_numeric(row['extract_amount'])

    if rulebook is None and row is not None and row.get('session_id') is not None:
        rulebook = load_verification_rulebook_for_session(row['session_id'])

    category_name = None
    if category_id is None:
        row_data = row['row_data']
        if isinstance(row_data, str):
            try:
                row_data = json.loads(row_data)
            except Exception:
                row_data = {}
        row_data = row_data or {}

        account_code = get_account_code_value(row_data)
        if not account_code:
            return {
                "voucher_line_id": voucher_line_id,
                "category_id": None, "category_name": None,
                "evaluated": False, "reason": "No account code on this line - Category unresolvable.",
                "summary": dict(empty_summary), "results": [],
            }

        cat_rows = execute_query_pg(
            """
            SELECT aca.category_id, arc.name AS category_name
            FROM account_category_assignments aca
            LEFT JOIN audit_rule_categories arc ON arc.id = aca.category_id AND arc.is_active = TRUE
            WHERE aca.client_id = %s AND aca.account_code = %s
            """,
            (row['client_id'], account_code)
        )
        if not cat_rows or not cat_rows[0]['category_id'] or not cat_rows[0]['category_name']:
            return {
                "voucher_line_id": voucher_line_id,
                "category_id": None, "category_name": None,
                "evaluated": False, "reason": "This line's account code has no active Category assigned.",
                "summary": dict(empty_summary), "results": [],
            }
        category_id = cat_rows[0]['category_id']
        category_name = cat_rows[0]['category_name']
    else:
        name_rows = execute_query_pg(
            "SELECT name FROM audit_rule_categories WHERE id = %s AND is_active = TRUE", (category_id,)
        )
        if not name_rows:
            return {
                "voucher_line_id": voucher_line_id,
                "category_id": category_id, "category_name": None,
                "evaluated": False, "reason": f"Category ID {category_id} not found or inactive.",
                "summary": dict(empty_summary), "results": [],
            }
        category_name = name_rows[0]['name']

    configs = _get_category_cross_check_configs(category_id, rulebook=rulebook)
    if linked_by_document is None:
        linked_by_document = _get_linked_evidence_by_document(voucher_line_id, sibling_ids=sibling_ids)

    # Physical Bills are manually entered evidence, not client_data rows, so they cannot be
    # represented in linked_by_document. Expose their numeric paper-bill fields to the same
    # matrix evaluator used by template-backed documents.
    physical_bill_values = {}
    physical_document_ids = {
        document['document_id']
        for config in configs
        for document in (config.get('documents') or [])
        if document.get('document_type') == 'physical_bill'
    }
    if physical_document_ids:
        stored_bills = _get_voucher_line_physical_bills(voucher_line_id, sibling_ids=sibling_ids)
        for document_id in physical_document_ids:
            bills = [b for b in stored_bills.get('bills', [])
                     if b.get('document_id') == document_id]
            totals = {'Quantity': None, 'Rate': None, 'Amount': None, '_row_count': 0}
            for bill in bills:
                for item in bill.get('items') or []:
                    for field, key in (('Quantity', 'quantity'), ('Rate', 'rate'), ('Amount', 'amount')):
                        numeric = _cross_check_numeric(item.get(key))
                        if numeric is not None:
                            totals[field] = numeric if totals[field] is None else totals[field] + numeric
                            totals['_row_count'] += 1
            physical_bill_values[document_id] = totals

    results = [
        _evaluate_single_cross_check(cfg, linked_by_document, voucher_amount, physical_bill_values)
        for cfg in configs
    ]

    summary = {
        "passed": sum(1 for r in results if r['status'] == "pass"),
        "failed": sum(1 for r in results if r['status'] == "fail"),
        "shortfall": sum(1 for r in results if r['status'] == "shortfall"),
        "not_evaluated": sum(1 for r in results if r['status'] == "not_evaluated"),
    }

    return {
        "voucher_line_id": voucher_line_id,
        "category_id": category_id,
        "category_name": category_name,
        "evaluated": True,
        "reason": None,
        "summary": summary,
        "results": results,
    }


@router.get("/api/verification/cross_check_results/{voucher_line_id}")
def get_cross_check_results(voucher_line_id: int):
    """
    Automated cross-document validation engine, Phase 9 of 10: read-only exposure of Phase 6's
    already-computed evaluation result, for the Supporting Documents modal's Data Table segment
    rollup. Zero new evaluation logic - a thin pass-through to evaluate_cross_checks_for_line(),
    the same function compute_and_persist_cleared_status() (Phase 7) already calls.

    category_id is omitted deliberately - the engine resolves it itself via the exact same
    account_category_assignments lookup the status engine uses, so this endpoint's result can
    never drift from what actually gates Cleared/Query for the same line.

    Always 200, even when unresolvable (unknown line, no Category, etc.) - never raises, matching
    evaluate_cross_checks_for_line()'s own contract and this file's "clean no-match, not an
    error" convention for read endpoints (e.g. category-for-account).
    """
    return evaluate_cross_checks_for_line(voucher_line_id)


@router.get("/api/verification/procedure_modal_data/{voucher_line_id}")
def get_procedure_modal_data(voucher_line_id: int, perf: bool = Query(False)):
    """
    Batch modal-open endpoint (Fast Modal-Open Data, Phase 1 of 4): everything the Supporting
    Documents modal (openProcedureModal(), static/procedure_modal.js) fetches on open, EXCEPT
    audit_guidance - that one stays a separate call, since its result also primes
    auditGuidanceCache (verification.js), a cache other UI surfaces (Supporting column badge,
    Guide popover, risk computation) read from independently. Folding it in here would mean
    duplicating that cache-priming logic outside verification.js/procedure_modal.js's shared
    scope, out of scope for this phase.

    A thin fan-out to the existing route functions below - zero query logic duplicated or
    reimplemented, so this can never drift from what each standalone endpoint already returns.
    Those endpoints are left in place, unremoved, for Audit Workbook or any other consumer of
    a subset. Collapses 6 client-server round trips (each paying the Render<->Supabase
    cross-region latency floor separately) into 1.

    404s automatically: get_voucher_line_documents() raises HTTPException(404) for an unknown
    voucher_line_id, which propagates up through this handler unchanged.

    client_id/transaction_ref (needed for the voucher_checks bundle) are derived server-side
    via one small join, the same pattern compute_and_persist_cleared_status() already uses -
    one less thing for client and server to agree on.

    The shared support loader returns the same current-line shapes as the five former reads.
    Cross-check evaluation reuses its linked-document result, while preserving the existing
    field-resolution rules for each evidence row.
    """
    perf_token = start_db_metrics() if perf else None
    perf_started = time.perf_counter()
    component_ms = {}
    try:
        support_started = time.perf_counter()
        support_data = get_voucher_support_data(voucher_line_id)
        current_support = (support_data.get("lines") or {}).get(str(voucher_line_id))
        if current_support is None:
            raise HTTPException(404, "Voucher line not found.")
        linked_documents = current_support["linked_documents"]
        procedure_checks = current_support["procedure_checks"]
        observations = current_support["observations"]
        physical_bills = current_support["physical_bills"]
        if perf:
            component_ms["support_data_ms"] = round((time.perf_counter() - support_started) * 1000, 1)

        linked_by_document = {}
        for document in linked_documents.get("documents", []):
            linked_by_document.setdefault(document["document_id"], []).append(document["client_data_id"])

        # get_voucher_support_data() already resolves transaction_ref and client_id from the
        # same voucher-line/session join. Reuse that metadata instead of paying for a second
        # identical lookup; the fallback keeps compatibility with older test doubles or any
        # alternate caller that returns only the historical support payload fields.
        client_id = support_data.get("client_id")
        transaction_ref = support_data.get("transaction_ref")
        if client_id is None or not transaction_ref:
            voucher_row = execute_query_pg(
                """
                SELECT vd.transaction_ref, vs.client_id
                FROM verification_details vd
                JOIN verification_sessions vs ON vs.id = vd.session_id
                WHERE vd.id = %s
                """,
                (voucher_line_id,)
            )
            if voucher_row:
                transaction_ref = voucher_row[0]['transaction_ref']
                client_id = voucher_row[0]['client_id']
        voucher_checks = []
        if client_id is not None and transaction_ref:
            voucher_checks_started = time.perf_counter()
            voucher_checks = get_voucher_checks_status(
                client_id=client_id,
                transaction_ref=transaction_ref
            )
            if perf:
                component_ms["voucher_checks_ms"] = round((time.perf_counter() - voucher_checks_started) * 1000, 1)

        cross_checks_started = time.perf_counter()
        session_rulebook = load_verification_rulebook_for_session(support_data.get("session_id")) \
            if support_data.get("session_id") is not None else None
        cross_check_kwargs = {"linked_by_document": linked_by_document}
        if session_rulebook is not None:
            cross_check_kwargs["rulebook"] = session_rulebook
        cross_check_results = evaluate_cross_checks_for_line(voucher_line_id, **cross_check_kwargs)
        if perf:
            component_ms["cross_checks_ms"] = round((time.perf_counter() - cross_checks_started) * 1000, 1)

        response = {
            "voucher_line_id": voucher_line_id,
            "linked_documents": linked_documents,
            "procedure_checks": procedure_checks,
            "observations": observations,
            "physical_bills": physical_bills,
            "cross_check_results": cross_check_results,
            "voucher_checks": voucher_checks,
        }
        if perf:
            metrics = get_db_metrics()
            response["_perf"] = {
                "components": component_ms,
                "db_calls": metrics["calls"],
                "db_reads": metrics["reads"],
                "db_writes": metrics["writes"],
                "db_ms": round(metrics["db_time"] * 1000, 1),
            }
        return response
    finally:
        if perf_token is not None:
            metrics = get_db_metrics()
            print(json.dumps({"perf": "procedure_modal_data", "detail_id": voucher_line_id,
                              "total_ms": round((time.perf_counter() - perf_started) * 1000, 1),
                              "components": component_ms, "db_calls": metrics["calls"],
                              "db_reads": metrics["reads"], "db_writes": metrics["writes"],
                              "db_ms": round(metrics["db_time"] * 1000, 1)}))
            stop_db_metrics(perf_token)


@router.get("/api/verification/line_status_reason/{voucher_line_id}")
def get_line_status_reason(voucher_line_id: int):
    """
    Read-only "why is this line Cleared/Query?" breakdown ("Why is this line Query?" fix).

    Runs compute_and_persist_cleared_status() in detail mode with persistence OFF, so this GET
    computes the same six gates the real status engine does but writes nothing at all - no
    cleared_status/matrix_version_applied UPDATE, no shortfall-observation re-stamp. The gate
    values themselves are produced by the unchanged engine code, so this can never report a
    different verdict from the one that actually gated the line.

    404s only on a genuinely unknown voucher_line_id. A line that exists but can't be classified
    (no account code / no active Category) is a normal outcome, returned as cleared_status: null
    plus a plain-English `reason` - same "absence is not an error" convention as
    category-for-account.
    """
    result = compute_and_persist_cleared_status(voucher_line_id, detail=True, persist=False)
    if result is None:
        raise HTTPException(status_code=404, detail="Voucher line not found")
    return {
        "voucher_line_id": voucher_line_id,
        "cleared_status": result.get("cleared_status"),
        "matrix_version_applied": result.get("matrix_version_applied"),
        "reason": result.get("reason"),
        "gates": result.get("gates") or [],
    }
