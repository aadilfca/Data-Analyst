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
from typing import List, Optional, Dict, Any
import io
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side
# Reused for voucher/line grouping in get_next_transaction (Phase 1) — same document-key
# logic already used by import's compare_transactions, applied at query time.
from routers.import_export import get_document_group_id
# Phase 9 (Audit Rules): audit guidance now sources from Audit Rules Categories instead of
# the old hardcoded audit_procedure_library table — same cross-router import pattern as above.
from routers.audit_rules import get_category_guidance_documents, get_category_guidance_cross_checks

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
        WHERE client_id = ? AND is_deleted = 0
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
        WHERE client_id = ? AND is_deleted = 0
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
        "version": current_version
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
               vd.validation_status, vd.validation_message, vd.status,
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

    # Ordered list of distinct voucher group ids (first appearance = earliest client_data_id,
    # since `details` is already ordered by cd.id ASC).
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
        "voucher_id": anchor['transaction_ref'],
        "voucher_date": anchor['extract_date'],
        "document_group_id": target_group_id,
        "line_count": len(lines),
        "lines": lines,
        "voucher_index": target_index + 1,
        "total_vouchers": total_vouchers
    }

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

    log_audit("Analyst", "Save Line Items", "Verification", f"Detail {detail_id} line items saved.")
    return {"message": "Line items and procedure data saved successfully."}

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
        WHERE client_id = ? AND is_deleted = 0
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
               vld.linked_by, vld.linked_at,
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
            "row_data": row_data,
            "extract_date": r['extract_date'],
            "extract_amount": r['extract_amount']
        })
    return {"voucher_line_id": voucher_line_id, "documents": result, "count": len(result)}


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
# synced so the same text is never appended twice.
# ==============================================

def sync_line_observations_to_voucher(voucher_line_id: int) -> None:
    """
    Roll any unsynced voucher_line_observations entries for this line into the line's own
    verification_details.observations field (append-only), then mark them synced. A no-op
    if there's nothing pending. Never raises - a failure here should not block the
    save_line_items() call it's invoked from.
    """
    try:
        pending = execute_query(
            """
            SELECT vlo.id, vlo.observation_text, ard.name AS document_name
            FROM voucher_line_observations vlo
            JOIN audit_rule_documents ard ON ard.id = vlo.document_id
            WHERE vlo.voucher_line_id = ? AND vlo.synced = 0
            """,
            (voucher_line_id,)
        )
        if not pending:
            return

        # Build one labelled block per pending entry that actually has text; a blank/cleared
        # observation contributes nothing to the appended text but is still marked synced below.
        new_blocks = [
            f"[{p['document_name']}]: {p['observation_text'].strip()}"
            for p in pending
            if p['observation_text'] and p['observation_text'].strip()
        ]

        if new_blocks:
            current = execute_query(
                "SELECT observations FROM verification_details WHERE id = ?",
                (voucher_line_id,)
            )
            existing_text = (current[0]['observations'] or '').strip() if current else ''
            appended_text = "\n".join(new_blocks)
            combined_text = f"{existing_text}\n{appended_text}" if existing_text else appended_text
            execute_write(
                "UPDATE verification_details SET observations = ? WHERE id = ?",
                (combined_text, voucher_line_id)
            )

        # Mark every processed entry synced regardless of whether it had text, so a cleared
        # observation doesn't stay "pending" forever.
        execute_write(
            "UPDATE voucher_line_observations SET synced = 1 WHERE voucher_line_id = ? AND synced = 0",
            (voucher_line_id,)
        )
    except Exception as e:
        # Deliberately swallow - observation roll-up must never block a line save.
        print(f"⚠️ sync_line_observations_to_voucher failed for line {voucher_line_id}: {e}")


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
        SELECT document_id, observation_text, synced, updated_by, updated_at
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
    Save (upsert) one Document tab's observation text for a voucher line. Always marks the
    entry unsynced - an explicit save always means "roll this into the voucher line's
    Observation field on the next save_line_items() call", whether the text is brand new,
    edited, or cleared to blank.
    """
    line = execute_query("SELECT id FROM verification_details WHERE id = ?", (voucher_line_id,))
    if not line:
        raise HTTPException(404, "Voucher line not found.")

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

    updated = execute_query(
        """
        SELECT voucher_line_id, document_id, observation_text, synced, updated_by, updated_at
        FROM voucher_line_observations
        WHERE voucher_line_id = ? AND document_id = ?
        """,
        (voucher_line_id, document_id)
    )[0]
    return {
        "voucher_line_id": updated['voucher_line_id'],
        "document_id": updated['document_id'],
        "observation_text": updated['observation_text'] or '',
        "synced": bool(updated['synced']),
        "updated_by": updated['updated_by'],
        "updated_at": updated['updated_at']
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