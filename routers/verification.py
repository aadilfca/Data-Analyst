# ==============================================
# ✅ VERIFICATION MODULE – PHASES 2, 3, 4
# Cashbook Identification, Session Start,
# Verification Workflow, Audited Cashbook & Export
# ==============================================

from fastapi import APIRouter, HTTPException, Query, Body
from fastapi.responses import StreamingResponse
from database import execute_query, execute_write, log_audit, get_raw_conn, get_client_voucher_prefixes
from datetime import datetime
import json
import re
from typing import List, Optional, Dict, Any
import io
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
from rapidfuzz import fuzz, utils as fuzz_utils
# Reused for voucher/line grouping in get_next_transaction (Phase 1) — same document-key
# logic already used by import's compare_transactions, applied at query time.
from routers.import_export import get_document_group_id
# Phase 9 (Audit Rules): audit guidance now sources from Audit Rules Categories instead of
# the old hardcoded audit_procedure_library table — same cross-router import pattern as above.
from routers.audit_rules import get_category_guidance_documents, get_category_guidance_cross_checks, get_category_documents

router = APIRouter()

# ==============================================
# HELPER FUNCTIONS
# ==============================================

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

def compute_and_persist_cleared_status(voucher_line_id: int) -> Optional[dict]:
    """
    Validation Rules Phase 4: compute this line's Cleared/Query status from four AND'ed
    checks and persist it to verification_details.cleared_status/matrix_version_applied.
    Called from save_line_items() - the same chokepoint every Save/Next/Prev/Validate/
    Complete/Exit path already runs per line (mirrors sync_line_observations_to_voucher()).

    Returns the computed {"cleared_status", "matrix_version_applied"} dict, or None if the
    line/its account code/its Category can't be resolved - cleared_status is left exactly
    as it was (NULL for a never-computed line) rather than written to, since "not yet
    classified" is a normal state (a line with no resolved Category can't even open
    Supporting Documents today, per the existing Verification workflow convention).
    """
    line = execute_query(
        """
        SELECT vd.id, vd.transaction_ref, cd.row_data, vs.client_id
        FROM verification_details vd
        JOIN client_data cd ON cd.id = vd.client_data_id
        JOIN verification_sessions vs ON vs.id = vd.session_id
        WHERE vd.id = ?
        """,
        (voucher_line_id,)
    )
    if not line:
        return None
    row = line[0]
    client_id = row['client_id']
    transaction_ref = row['transaction_ref']
    row_data = row['row_data']
    if isinstance(row_data, str):
        try:
            row_data = json.loads(row_data)
        except Exception:
            row_data = {}
    row_data = row_data or {}

    account_code = get_column_value(row_data, ACCOUNT_CODE_ALIASES)
    if not account_code:
        return None

    cat_rows = execute_query(
        """
        SELECT aca.category_id, arc.name AS category_name, arc.matrix_version
        FROM account_category_assignments aca
        LEFT JOIN audit_rule_categories arc ON arc.id = aca.category_id AND arc.is_active = 1
        WHERE aca.client_id = ? AND aca.account_code = ?
        """,
        (client_id, account_code)
    )
    if not cat_rows or not cat_rows[0]['category_id'] or not cat_rows[0]['category_name']:
        return None

    category_id = cat_rows[0]['category_id']
    matrix_version = cat_rows[0]['matrix_version']

    sibling_ids = _resolve_sibling_voucher_line_ids(voucher_line_id) or [voucher_line_id]
    placeholders = ",".join("?" for _ in sibling_ids)

    # 1. Document requirement check: Category's required documents vs linked (active) documents.
    required_docs = get_category_documents(category_id)
    required_doc_ids = {d['document_id'] for d in required_docs}

    linked_rows = execute_query(
        f"""
        SELECT DISTINCT document_id FROM voucher_line_documents
        WHERE voucher_line_id IN ({placeholders}) AND status = 'active'
        """,
        tuple(sibling_ids)
    )
    linked_doc_ids = {r['document_id'] for r in linked_rows}
    doc_requirement_met = required_doc_ids.issubset(linked_doc_ids)

    # 2. Per-document approval-required check: each approval_required document among the
    # required set must have at least one ticked role matching "approv" (case-insensitive
    # substring) for that specific document_id.
    approval_required_doc_ids = {d['document_id'] for d in required_docs if d['approval_required']}
    approval_docs_ok = True
    if approval_required_doc_ids:
        check_rows = execute_query(
            f"""
            SELECT document_id, role_name FROM voucher_line_procedure_checks
            WHERE voucher_line_id IN ({placeholders}) AND is_checked = 1
            """,
            tuple(sibling_ids)
        )
        approved_doc_ids = {
            r['document_id'] for r in check_rows
            if 'approv' in (r['role_name'] or '').lower()
        }
        approval_docs_ok = approval_required_doc_ids.issubset(approved_doc_ids)

    # 3. Voucher-level checks gate: independent of Category/documents, always required.
    # Generic Voucher-Level Checks (Phase 5 cutover) - reads voucher_check_status/
    # audit_rule_voucher_checks (keyed client_id+transaction_ref, not session-scoped, same
    # rationale as the old voucher_approvals table it replaces) instead of the retired
    # voucher_approvals-based Prepared/Checked/Approved-By fields. Passes only if every
    # currently-active check has been ticked - a check with no voucher_check_status row yet
    # counts as unticked (fails safe, same "no record = normal not-yet-done state, but that
    # state doesn't pass the gate" convention used elsewhere in this function). No transaction_ref
    # = fail.
    voucher_checks_ok = False
    if transaction_ref:
        active_check_ids = {
            c['id'] for c in execute_query("SELECT id FROM audit_rule_voucher_checks WHERE is_active = 1")
        }
        ticked_rows = execute_query(
            "SELECT check_id FROM voucher_check_status WHERE client_id = ? AND transaction_ref = ? AND state = 'done'",
            (client_id, transaction_ref)
        )
        ticked_check_ids = {r['check_id'] for r in ticked_rows}
        # Vacuously true when zero checks are configured (nothing required = nothing to satisfy),
        # same as an empty required-documents set already behaves in check #1 above.
        voucher_checks_ok = active_check_ids.issubset(ticked_check_ids)

    # 4. Signature-exception check: defaults OK, fails only on a confirmed flagged mismatch.
    sig_rows = execute_query(
        f"""
        SELECT COUNT(*) as cnt FROM voucher_line_documents
        WHERE voucher_line_id IN ({placeholders}) AND status = 'active' AND signature_exception = 1
        """,
        tuple(sibling_ids)
    )
    signature_ok = (sig_rows[0]['cnt'] == 0)

    all_ok = doc_requirement_met and approval_docs_ok and voucher_checks_ok and signature_ok
    cleared_status = "Cleared" if all_ok else "Query"

    execute_write(
        "UPDATE verification_details SET cleared_status = ?, matrix_version_applied = ? WHERE id = ?",
        (cleared_status, matrix_version, voucher_line_id)
    )
    return {"cleared_status": cleared_status, "matrix_version_applied": matrix_version}

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
    rows = execute_query("SELECT row_data FROM client_data WHERE id = ?", (client_data_id,))
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
    return execute_query(
        """
        SELECT m.id AS column_id, m.column_name, m.data_type, p.sort_order
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = ? AND m.is_active = 1
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )

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
        total = execute_query("SELECT COUNT(*) as count FROM verifications")[0]['count']
        pending = execute_query("SELECT COUNT(*) as count FROM verifications WHERE status = 'Pending'")[0]['count']
        approved = execute_query("SELECT COUNT(*) as count FROM verifications WHERE status = 'Approved'")[0]['count']
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
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")
    try:
        datetime.fromisoformat(date_start)
        datetime.fromisoformat(date_end)
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")
    if date_start > date_end:
        raise HTTPException(400, "Start date must be before or equal to end date.")

    client_prefixes = get_client_voucher_prefixes(client_id)
    if category_group == 'cash':
        client_prefixes = {k: v for k, v in client_prefixes.items() if k in ('Cash Payment', 'Cash Receipt')}
    elif category_group == 'bank':
        client_prefixes = {k: v for k, v in client_prefixes.items() if k in ('Bank Payment', 'Bank Receipt')}

    rows = execute_query(
        """
        SELECT id, row_data
        FROM client_data
        WHERE client_id = ? AND is_deleted = 0 AND change_detection_status != 'deleted'
          AND extract_date BETWEEN ? AND ?
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
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")
    try:
        datetime.fromisoformat(period_start)
        datetime.fromisoformat(period_end)
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")
    if period_start > period_end:
        raise HTTPException(400, "Start date must be before or equal to end date.")
    
    client_prefixes = get_client_voucher_prefixes(client_id)
    rows = execute_query(
        """
        SELECT id, row_data, extract_date, extract_amount
        FROM client_data
        WHERE client_id = ? AND is_deleted = 0 AND change_detection_status != 'deleted'
          AND extract_date BETWEEN ? AND ?
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
    session_id = execute_write(
        """
        INSERT INTO verification_sessions (client_id, cashbook_name, period_start, period_end, created_by, status, total_transactions, session_type)
        VALUES (?, ?, ?, ?, ?, 'In Progress', ?, ?)
        """,
        (client_id, cashbook_name, period_start, period_end, created_by, len(transactions_to_verify), session_type)
    )
    
    # Prepare parameters for all details in one batch
    detail_params = []
    for trans in transactions_to_verify:
        detail_params.append((session_id, trans['client_data_id'], trans['voucher_id']))
    
    # Insert all details in a single transaction (fast)
    conn = get_raw_conn()
    try:
        conn.executemany(
            """
            INSERT INTO verification_details (session_id, client_data_id, transaction_ref, status, validation_status)
            VALUES (?, ?, ?, 'Pending', 'Pending')
            """,
            detail_params
        )
        conn.commit()
    finally:
        conn.close()
    
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
    change_info = execute_query(
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
        WHERE cd.id = ?
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
        "matrix_version_applied": d.get('matrix_version_applied')
    }

def _fetch_session_details_grouped(session_id: int) -> list:
    """
    Fetch all verification_details for a session (joined with client_data),
    each annotated with its computed '_doc_group_id' (Phase A: shared by
    get_next_transaction and get_voucher_nav so both group vouchers the same
    way). Ordered by client_data_id so line/voucher order matches the
    physical row order. Does not filter by status.
    """
    details = execute_query(
        """
        SELECT vd.id, vd.client_data_id, vd.transaction_ref, vd.line_items, vd.procedure_data,
               vd.validation_status, vd.validation_message, vd.status, vd.observations,
               vd.cleared_status, vd.matrix_version_applied,
               cd.template_id, cd.row_data, cd.extract_date
        FROM verification_details vd
        JOIN client_data cd ON vd.client_data_id = cd.id
        WHERE vd.session_id = ?
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
    session = execute_query("SELECT id FROM verification_sessions WHERE id = ? AND status != 'Cancelled'", (session_id,))
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
    session = execute_query("SELECT id FROM verification_sessions WHERE id = ? AND status != 'Cancelled'", (session_id,))
    if not session:
        raise HTTPException(404, "Verification session not found or cancelled.")

    details = _fetch_session_details_grouped(session_id)
    return _compute_voucher_nav_result(details, current_detail_id, direction)


@router.post("/api/verification/verify_voucher_checks")
def verify_voucher_checks(
    session_id: int = Body(..., embed=True),
    current_detail_id: int = Body(..., embed=True, description="detail_id of any line in the currently displayed voucher")
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
    session = execute_query("SELECT id, client_id FROM verification_sessions WHERE id = ? AND status != 'Cancelled'", (session_id,))
    if not session:
        raise HTTPException(404, "Verification session not found or cancelled.")
    client_id = session[0]['client_id']

    details = _fetch_session_details_grouped(session_id)
    current = next((d for d in details if d['id'] == current_detail_id), None)
    if current is None:
        raise HTTPException(404, "Verification detail not found in this session.")

    transaction_ref = current['transaction_ref']
    voucher_rows = [d for d in details if d['_doc_group_id'] == current['_doc_group_id']]

    active_checks = execute_query(
        "SELECT id, name FROM audit_rule_voucher_checks WHERE is_active = 1 ORDER BY sort_order"
    )
    status_rows = execute_query(
        "SELECT check_id, state FROM voucher_check_status WHERE client_id = ? AND transaction_ref = ?",
        (client_id, transaction_ref)
    )
    state_by_check_id = {r['check_id']: r['state'] for r in status_rows}

    untouched = [c for c in active_checks if state_by_check_id.get(c['id'], 'untouched') == 'untouched']
    if untouched:
        names = ", ".join(c['name'] for c in untouched)
        raise HTTPException(400, f"Cannot verify: {len(untouched)} check(s) still not marked ({names}).")

    not_done_ids = {c['id'] for c in active_checks if state_by_check_id.get(c['id']) == 'not_done'}
    for c in active_checks:
        execute_write(
            "UPDATE voucher_check_status SET flagged_in_observations = ? WHERE client_id = ? AND transaction_ref = ? AND check_id = ?",
            (1 if c['id'] in not_done_ids else 0, client_id, transaction_ref, c['id'])
        )

    for row in voucher_rows:
        sync_line_observations_to_voucher(row['id'])

    result = _compute_voucher_nav_result(details, current_detail_id, "next")
    result["verified"] = True
    result["not_done_count"] = len(not_done_ids)
    return result

@router.post("/api/verification/save_line_items")
def save_line_items(
    detail_id: int = Body(..., embed=True),
    line_items: List[Dict[str, Any]] = Body(..., embed=True),
    transaction_type: Optional[str] = Body(None, embed=True),
    procedure_data: Optional[Dict[str, Any]] = Body(None, embed=True)
):
    detail = execute_query(
        "SELECT id, status FROM verification_details WHERE id = ?",
        (detail_id,)
    )
    if not detail:
        raise HTTPException(404, "Verification detail not found.")
    if detail[0]['status'] != 'Pending':
        raise HTTPException(400, "Transaction is already verified or skipped.")
    
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
    
    line_items_json = json.dumps(line_items)
    updates = {"line_items": line_items_json}
    if transaction_type is not None:
        if procedure_data is not None:
            proc = procedure_data.copy()
            proc['transaction_type'] = transaction_type
            updates["procedure_data"] = json.dumps(proc)
        else:
            updates["procedure_data"] = json.dumps({"transaction_type": transaction_type})
    elif procedure_data is not None:
        updates["procedure_data"] = json.dumps(procedure_data)
    
    set_clauses = []
    params = []
    for key, value in updates.items():
        set_clauses.append(f"{key} = ?")
        params.append(value)
    params.append(detail_id)
    sql = f"UPDATE verification_details SET {', '.join(set_clauses)} WHERE id = ?"
    execute_write(sql, params)

    # Phase 6: roll any unsynced per-document observation entries into this line's own
    # Observation field. save_line_items() is the single chokepoint every Save/Next/Prev/
    # Validate/Complete/Exit path already calls per line, so this is the correct place to sync.
    sync_line_observations_to_voucher(detail_id)

    # Validation Rules Phase 4: recompute Cleared/Query status on every save, same
    # chokepoint as the observation sync directly above. Phase 5: surface the result in the
    # response so the frontend can refresh the line's badge without a second round-trip.
    status_result = compute_and_persist_cleared_status(detail_id)

    log_audit("Analyst", "Save Line Items", "Verification", f"Detail {detail_id} line items saved.")
    response = {"message": "Line items and procedure data saved successfully."}
    if status_result:
        response["cleared_status"] = status_result["cleared_status"]
        response["matrix_version_applied"] = status_result["matrix_version_applied"]
    return response

@router.post("/api/verification/validate")
def validate_transaction(
    detail_id: int = Body(..., embed=True),
    inventory_flag: bool = Body(False, embed=True)
):
    detail = execute_query(
        """
        SELECT vd.id, vd.client_data_id, vd.line_items, vd.status, cd.extract_amount
        FROM verification_details vd
        JOIN client_data cd ON vd.client_data_id = cd.id
        WHERE vd.id = ?
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
    execute_write(
        """
        UPDATE verification_details
        SET validation_status = ?, validation_message = ?
        WHERE id = ?
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
    detail = execute_query(
        "SELECT id, validation_status, status FROM verification_details WHERE id = ?",
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
    
    now = datetime.now().isoformat()
    execute_write(
        """
        UPDATE verification_details
        SET status = 'Verified', verified_by = ?, verified_at = ?
        WHERE id = ?
        """,
        (verified_by, now, detail_id)
    )
    
    session_id = execute_query(
        "SELECT session_id FROM verification_details WHERE id = ?",
        (detail_id,)
    )[0]['session_id']
    execute_write(
        """
        UPDATE verification_sessions
        SET verified_count = verified_count + 1
        WHERE id = ?
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
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive.")
    template = execute_query("SELECT id FROM templates WHERE id = ? AND is_active = 1", (template_id,))
    if not template:
        raise HTTPException(404, "Template not found or inactive.")
    
    query = """
        SELECT id, row_data, extract_amount, extract_date
        FROM client_data
        WHERE client_id = ? AND template_id = ? AND is_deleted = 0
          AND EXISTS (
              SELECT 1 FROM json_each(row_data) 
              WHERE LOWER(value) LIKE LOWER(?)
          )
        ORDER BY extract_date DESC
        LIMIT ?
    """
    rows = execute_query(query, (client_id, template_id, f"%{search}%", limit))
    
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
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
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
    
    client_prefixes = get_client_voucher_prefixes(client_id)

    # Fetch rows
    rows = execute_query(
        """
        SELECT id, row_data, extract_date, extract_amount
        FROM client_data
        WHERE client_id = ? AND is_deleted = 0 AND change_detection_status != 'deleted'
          AND extract_date BETWEEN ? AND ?
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
                    'extract_amount': row['extract_amount']
                })
            elif vtype in ('CRV', 'BRV') and row['credit'] > 0 and row['debit'] == 0:
                transactions.append({
                    'client_data_id': row['row_id'],
                    'row_data': row['row_data'],
                    'voucher_id': vid,
                    'extract_date': row['extract_date'],
                    'extract_amount': row['extract_amount']
                })
    
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
            'extract_amount': trans['extract_amount']
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
    session = execute_query(
        """
        SELECT id, client_id, cashbook_name, period_start, period_end, 
               total_transactions, verified_count, status, created_by, created_at
        FROM verification_sessions
        WHERE id = ? AND status != 'Cancelled'
        """,
        (session_id,)
    )
    if not session:
        raise HTTPException(404, "Verification session not found or cancelled.")
    
    s = session[0]
    # Get all details
    details = execute_query(
        """
        SELECT vd.id, vd.client_data_id, vd.transaction_ref, vd.line_items, vd.procedure_data,
               vd.validation_status, vd.validation_message, vd.status as verification_status,
               vd.comments, vd.observations, vd.verified_by, vd.verified_at,
               cd.row_data, cd.extract_amount, cd.extract_date
        FROM verification_details vd
        JOIN client_data cd ON vd.client_data_id = cd.id
        WHERE vd.session_id = ?
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
            "verified_at": d['verified_at']
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
@router.get("/api/verification/session/{session_id}/progress")
def get_session_progress(session_id: int):
    """
    Lightweight endpoint to get only the progress counts for a session.
    Returns total_transactions and verified_count.
    """
    session = execute_query(
        """
        SELECT total_transactions, verified_count
        FROM verification_sessions
        WHERE id = ? AND status != 'Cancelled'
        """,
        (session_id,)
    )
    if not session:
        raise HTTPException(404, "Session not found or cancelled.")
    return {
        "total_transactions": session[0]['total_transactions'],
        "verified_count": session[0]['verified_count']
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
            "Verified At": t.get('verified_at', '')
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
    """
    rows = execute_query(
        "SELECT id, kb_overview FROM audit_rule_categories WHERE name = ? AND is_active = 1",
        (transaction_type,)
    )
    if not rows:
        raise HTTPException(404, f"No audit guidance found for category '{transaction_type}'.")
    category_id = rows[0]['id']
    return {
        "transaction_type": transaction_type,
        "category_id": category_id,
        "kb_overview": rows[0]['kb_overview'],
        "documents": get_category_guidance_documents(category_id),
        "cross_checks": get_category_guidance_cross_checks(category_id)
    }

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
    """
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
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
    upload_rows = execute_query(
        """
        SELECT id FROM trial_balance_uploads
        WHERE client_id = ? AND status = 'parsed'
        ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC
        LIMIT 1
        """,
        (client_id,)
    )
    if upload_rows:
        tb_rows = execute_query(
            """
            SELECT a.category_id, c.name AS category_name
            FROM trial_balance_accounts a
            LEFT JOIN audit_rule_categories c ON c.id = a.category_id AND c.is_active = 1
            WHERE a.upload_id = ? AND a.account_code = ?
            """,
            (upload_rows[0]['id'], account_code)
        )
        if tb_rows and tb_rows[0]['category_name']:
            return {
                "matched": True,
                "category_id": tb_rows[0]['category_id'],
                "category_name": tb_rows[0]['category_name']
            }

    # LEFT JOIN (with the category's own is_active check folded into the ON
    # clause) means category_name comes back NULL for every "no match" case
    # in one query: no assignment row at all, an assignment explicitly
    # cleared to NULL, or an assignment pointing at a since-soft-deleted
    # category (no FK enforcement in this app, so a stale id is possible).
    rows = execute_query(
        """
        SELECT aca.category_id, arc.name AS category_name
        FROM account_category_assignments aca
        LEFT JOIN audit_rule_categories arc
            ON arc.id = aca.category_id AND arc.is_active = 1
        WHERE aca.client_id = ? AND aca.account_code = ?
        """,
        (client_id, account_code)
    )

    if not rows or not rows[0]['category_name']:
        return {"matched": False, "category_id": None, "category_name": None}

    return {
        "matched": True,
        "category_id": rows[0]['category_id'],
        "category_name": rows[0]['category_name']
    }

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
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    no_match_response = {"matched": False, "message": "No match — new head likely needed", "suggestions": []}

    upload_rows = execute_query(
        """
        SELECT id FROM trial_balance_uploads
        WHERE client_id = ? AND status = 'parsed'
        ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC
        LIMIT 1
        """,
        (client_id,)
    )
    if not upload_rows:
        return no_match_response

    accounts = execute_query(
        """
        SELECT a.account_code, a.account_name, a.category_id, c.name AS category_name
        FROM trial_balance_accounts a
        LEFT JOIN audit_rule_categories c ON a.category_id = c.id AND c.is_active = 1
        WHERE a.upload_id = ? AND a.classification = 'leaf'
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
    checks = execute_query(
        "SELECT id, name, sort_order FROM audit_rule_voucher_checks WHERE is_active = 1 ORDER BY sort_order"
    )
    status_rows = execute_query(
        "SELECT check_id, state FROM voucher_check_status WHERE client_id = ? AND transaction_ref = ?",
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

    check = execute_query(
        "SELECT id FROM audit_rule_voucher_checks WHERE id = ? AND is_active = 1",
        (check_id,)
    )
    if not check:
        raise HTTPException(400, f"Check {check_id} is not a configured active voucher-level check.")

    execute_write(
        """
        INSERT INTO voucher_check_status (client_id, transaction_ref, check_id, state, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
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
    line - so a caller can query "WHERE voucher_line_id IN (...)" instead of "= ?" and see
    assignments/ticks/observations regardless of which session they were made under. Returns None
    if voucher_line_id itself doesn't exist (caller 404s, same as before this fix). Writes are NOT
    affected by this helper - they still target whichever voucher_line_id the caller passes in,
    exactly as before.
    """
    line = execute_query("SELECT client_data_id FROM verification_details WHERE id = ?", (voucher_line_id,))
    if not line:
        return None
    siblings = execute_query(
        "SELECT id FROM verification_details WHERE client_data_id = ?",
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
    line = execute_query("SELECT id FROM verification_details WHERE id = ?", (voucher_line_id,))
    if not line:
        raise HTTPException(404, "Voucher line not found.")

    row = execute_query("SELECT id FROM client_data WHERE id = ? AND is_deleted = 0", (client_data_id,))
    if not row:
        raise HTTPException(404, "Data Bank row not found or has been deleted.")

    doc = execute_query("SELECT id FROM audit_rule_documents WHERE id = ? AND is_active = 1", (document_id,))
    if not doc:
        raise HTTPException(404, "Document not found or inactive.")

    existing = execute_query(
        """
        SELECT vld.id, vld.voucher_line_id, vd.transaction_ref
        FROM voucher_line_documents vld
        JOIN verification_details vd ON vd.id = vld.voucher_line_id
        WHERE vld.client_data_id = ? AND vld.status = 'active'
        """,
        (client_data_id,)
    )
    if existing:
        raise HTTPException(
            400,
            f"This Data Bank row is already assigned to voucher '{existing[0]['transaction_ref']}' (line {existing[0]['voucher_line_id']})."
        )

    assignment_id = execute_write(
        """
        INSERT INTO voucher_line_documents (voucher_line_id, client_data_id, document_id, linked_by)
        VALUES (?, ?, ?, ?)
        """,
        (voucher_line_id, client_data_id, document_id, linked_by)
    )

    log_audit(linked_by, "Assign Supporting Document", "Verification",
               f"Linked client_data {client_data_id} to voucher line {voucher_line_id} (document {document_id}).")

    linked_row = execute_query(
        "SELECT id, voucher_line_id, client_data_id, document_id, linked_by, linked_at FROM voucher_line_documents WHERE id = ?",
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

    placeholders = ",".join("?" for _ in sibling_ids)
    rows = execute_query(
        f"""
        SELECT vld.id, vld.voucher_line_id, vld.client_data_id, vld.document_id,
               vld.linked_by, vld.linked_at, vld.signature_exception,
               ard.name AS document_name,
               cd.row_data, cd.extract_date, cd.extract_amount
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
            "linked_by": r['linked_by'],
            "linked_at": r['linked_at'],
            # Validation Rules Phase 5: default OK (0/False) - only ever set to True on a
            # confirmed signature mismatch, per Phase 1's locked assumption.
            "signature_exception": bool(r['signature_exception']),
            "row_data": row_data,
            "extract_date": r['extract_date'],
            "extract_amount": r['extract_amount']
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
    row = execute_query(
        "SELECT id, voucher_line_id FROM voucher_line_documents WHERE id = ? AND status = 'active'",
        (assignment_id,)
    )
    if not row:
        raise HTTPException(404, "Linked evidence row not found or no longer active.")

    execute_write(
        "UPDATE voucher_line_documents SET signature_exception = ? WHERE id = ?",
        (1 if signature_exception else 0, assignment_id)
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
    link = execute_query(
        "SELECT id, voucher_line_id, client_data_id, document_id, status FROM voucher_line_documents WHERE id = ?",
        (assignment_id,)
    )
    if not link:
        raise HTTPException(404, "Supporting document link not found.")
    if link[0]['status'] != 'active':
        raise HTTPException(404, "This supporting document link has already been unassigned.")

    execute_write(
        "UPDATE voucher_line_documents SET status = 'unassigned', unassigned_at = CURRENT_TIMESTAMP WHERE id = ?",
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

    placeholders = ",".join("?" for _ in sibling_ids)
    rows = execute_query(
        f"""
        SELECT document_id, role_name, is_checked, checked_by, checked_at
        FROM voucher_line_procedure_checks
        WHERE voucher_line_id IN ({placeholders})
        """,
        tuple(sibling_ids)
    )
    # Dedup by (document_id, role_name), keeping whichever sibling's row was checked/unchecked
    # most recently - checked_at is a SQLite CURRENT_TIMESTAMP string ("YYYY-MM-DD HH:MM:SS"),
    # which sorts correctly as a plain string comparison.
    latest_by_key: Dict[tuple, dict] = {}
    for r in rows:
        key = (r['document_id'], r['role_name'])
        existing = latest_by_key.get(key)
        if existing is None or (r['checked_at'] or '') > (existing['checked_at'] or ''):
            latest_by_key[key] = r

    checks = [
        {
            "document_id": r['document_id'],
            "role_name": r['role_name'],
            "is_checked": bool(r['is_checked']),
            "checked_by": r['checked_by'],
            "checked_at": r['checked_at']
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
    line = execute_query("SELECT id FROM verification_details WHERE id = ?", (voucher_line_id,))
    if not line:
        raise HTTPException(404, "Voucher line not found.")

    doc = execute_query("SELECT id FROM audit_rule_documents WHERE id = ? AND is_active = 1", (document_id,))
    if not doc:
        raise HTTPException(404, "Document not found or inactive.")

    role = execute_query(
        "SELECT id FROM audit_rule_document_auth_roles WHERE document_id = ? AND role_name = ?",
        (document_id, role_name)
    )
    if not role:
        raise HTTPException(400, f"'{role_name}' is not a configured auth role for this document.")

    execute_write(
        """
        INSERT INTO voucher_line_procedure_checks (voucher_line_id, document_id, role_name, is_checked, checked_by, checked_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(voucher_line_id, document_id, role_name)
        DO UPDATE SET is_checked = excluded.is_checked, checked_by = excluded.checked_by, checked_at = CURRENT_TIMESTAMP
        """,
        (voucher_line_id, document_id, role_name, 1 if is_checked else 0, checked_by)
    )

    log_audit(checked_by, "Set Audit Procedure Check", "Verification",
               f"Set '{role_name}' ({document_id}) to {is_checked} for voucher line {voucher_line_id}.")

    updated = execute_query(
        """
        SELECT voucher_line_id, document_id, role_name, is_checked, checked_by, checked_at
        FROM voucher_line_procedure_checks
        WHERE voucher_line_id = ? AND document_id = ? AND role_name = ?
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

def sync_line_observations_to_voucher(voucher_line_id: int) -> None:
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
        sibling_ids = _resolve_sibling_voucher_line_ids(voucher_line_id)
        if not sibling_ids:
            # voucher_line_id itself doesn't exist - nothing to do (shouldn't happen, since
            # save_line_items()/save_line_observation() already validated it exists).
            return

        placeholders = ",".join("?" for _ in sibling_ids)
        # LEFT JOIN (not INNER) so the reserved ACCOUNT_HEAD_OBSERVATION_DOC_ID sentinel entry -
        # which has no matching audit_rule_documents row by design - isn't silently dropped from
        # the rollup; its document_name comes back NULL and is handled in the block-building loop
        # below instead.
        all_entries = execute_query(
            f"""
            SELECT vlo.id, vlo.document_id, vlo.observation_text, vlo.updated_at,
                   ard.name AS document_name
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
            if existing is None or (entry['updated_at'] or '') > (existing['updated_at'] or ''):
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
            return f"[{entry['document_name']}]: {entry['observation_text'].strip()}"

        doc_blocks = [
            _format_block(entry)
            for entry in sorted(latest_by_doc.values(), key=lambda e: e['document_id'])
            if entry['observation_text'] and entry['observation_text'].strip()
        ]

        # Voucher-check blocks: derived from this line's own (client_id, transaction_ref), not
        # from sibling ids - voucher_check_status is deliberately keyed session-independently
        # (see that table's own schema comment), so a plain lookup on the line itself is enough.
        line_row = execute_query(
            """
            SELECT vd.transaction_ref, vs.client_id
            FROM verification_details vd
            JOIN verification_sessions vs ON vs.id = vd.session_id
            WHERE vd.id = ?
            """,
            (voucher_line_id,)
        )
        check_blocks: List[str] = []
        if line_row:
            check_rows = execute_query(
                """
                SELECT arvc.name
                FROM voucher_check_status vcs
                JOIN audit_rule_voucher_checks arvc ON arvc.id = vcs.check_id
                WHERE vcs.client_id = ? AND vcs.transaction_ref = ?
                  AND arvc.is_active = 1 AND vcs.flagged_in_observations = 1
                ORDER BY arvc.sort_order
                """,
                (line_row[0]['client_id'], line_row[0]['transaction_ref'])
            )
            check_blocks = [f"{r['name']} marked not done" for r in check_rows]

        blocks = check_blocks + doc_blocks
        new_text = "\n".join(blocks) if blocks else None

        execute_write(
            "UPDATE verification_details SET observations = ? WHERE id = ?",
            (new_text, voucher_line_id)
        )

        # Bookkeeping only under this rebuild design (see docstring) - kept accurate for
        # consistency/audit visibility, not because anything still reads it to gate behaviour.
        for entry in all_entries:
            execute_write(
                "UPDATE voucher_line_observations SET synced = 1, last_synced_text = ? WHERE id = ?",
                (entry['observation_text'], entry['id'])
            )
    except Exception as exc:
        # Deliberately swallow - observation roll-up must never block a line save.
        print(f"⚠️ sync_line_observations_to_voucher failed for line {voucher_line_id}: {exc}")


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

    placeholders = ",".join("?" for _ in sibling_ids)
    rows = execute_query(
        f"""
        SELECT document_id, observation_text, synced, last_synced_text, updated_by, updated_at
        FROM voucher_line_observations
        WHERE voucher_line_id IN ({placeholders})
        """,
        tuple(sibling_ids)
    )
    # Dedup by document_id, keeping whichever sibling's entry was updated most recently -
    # updated_at is a SQLite CURRENT_TIMESTAMP string, sorts correctly as a string comparison.
    latest_by_doc: Dict[int, dict] = {}
    for r in rows:
        existing = latest_by_doc.get(r['document_id'])
        if existing is None or (r['updated_at'] or '') > (existing['updated_at'] or ''):
            latest_by_doc[r['document_id']] = r

    entries = [
        {
            "document_id": r['document_id'],
            "observation_text": r['observation_text'] or '',
            "synced": bool(r['synced']),
            "last_synced_text": r['last_synced_text'],
            "updated_by": r['updated_by'],
            "updated_at": r['updated_at']
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
    line = execute_query("SELECT id FROM verification_details WHERE id = ?", (voucher_line_id,))
    if not line:
        raise HTTPException(404, "Voucher line not found.")

    # ACCOUNT_HEAD_OBSERVATION_DOC_ID is a reserved sentinel, not a real audit_rule_documents row -
    # skip the existence/active check for it (see the constant's definition above).
    if document_id != ACCOUNT_HEAD_OBSERVATION_DOC_ID:
        doc = execute_query("SELECT id FROM audit_rule_documents WHERE id = ? AND is_active = 1", (document_id,))
        if not doc:
            raise HTTPException(404, "Document not found or inactive.")

    execute_write(
        """
        INSERT INTO voucher_line_observations (voucher_line_id, document_id, observation_text, synced, updated_by, updated_at)
        VALUES (?, ?, ?, 0, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(voucher_line_id, document_id)
        DO UPDATE SET observation_text = excluded.observation_text, synced = 0, updated_by = excluded.updated_by, updated_at = CURRENT_TIMESTAMP
        """,
        (voucher_line_id, document_id, observation_text, updated_by)
    )

    log_audit(updated_by, "Save Line Observation", "Verification",
               f"Saved observation for document {document_id} on voucher line {voucher_line_id}.")

    # Roll up immediately rather than waiting for the next save_line_items() call - see
    # docstring above. Runs before the final SELECT below so the response's own "synced" field
    # correctly reflects the roll-up that just happened, not a stale pre-sync value.
    sync_line_observations_to_voucher(voucher_line_id)

    updated = execute_query(
        """
        SELECT voucher_line_id, document_id, observation_text, synced, updated_by, updated_at
        FROM voucher_line_observations
        WHERE voucher_line_id = ? AND document_id = ?
        """,
        (voucher_line_id, document_id)
    )[0]

    # Confirmed fix, 2026-07-25: also return the line's own current rolled-up Observation text
    # (post-sync, since the call above already ran) so the frontend can update the voucher
    # table's OBSERVATIONS cell immediately, without a full voucher reload.
    voucher_row = execute_query(
        "SELECT observations FROM verification_details WHERE id = ?",
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
    placeholders = ",".join("?" for _ in client_data_ids)
    rows = execute_query(
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