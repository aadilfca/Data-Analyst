# ==============================================
# ✅ VERIFICATION MODULE – PHASES 2, 3, 4
# Cashbook Identification, Session Start,
# Verification Workflow, Audited Cashbook & Export
# ==============================================

from fastapi import APIRouter, HTTPException, Query, Body
from fastapi.responses import StreamingResponse
from database import execute_query, execute_write, log_audit, get_raw_conn
from datetime import datetime
import json
from typing import List, Optional, Dict, Any
import io
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side

router = APIRouter()

# ==============================================
# HELPER FUNCTIONS
# ==============================================

def get_voucher_patterns() -> dict:
    """Hardcoded voucher pattern mappings (no database lookup)."""
    return {"CPV": ["CPV","CV"], "CRV": ["CRV","CR"], "BPV": ["BPV","BV"], "BRV": ["BRV","BR"], "JV": ["JV"]}

def get_voucher_type(voucher_id: str, patterns: dict) -> str:
    if not voucher_id:
        return None
    voucher_id = voucher_id.strip()
    for vtype, prefixes in patterns.items():
        for p in prefixes:
            if voucher_id.startswith(p):
                return vtype
    return None

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

def extract_cashbook_name(row_data: dict, patterns: dict) -> Optional[str]:
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

    vtype = get_voucher_type(voucher_id, patterns)
    if vtype == 'CPV' and credit > 0:
        return account_name
    elif vtype == 'CRV' and debit > 0:
        return account_name
    return None

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
    debug: bool = Query(False, description="If true, return detailed debug info")
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
    
    patterns = get_voucher_patterns()
    print(f"🔍 [CASHBOOK] Fetching rows for client={client_id}, date_range={date_start} to {date_end}")
    
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
    
    print(f"🔍 [CASHBOOK] Found {len(rows)} rows total")
    
    cashbooks = set()
    debug_info = []
    for row in rows:
        try:
            row_data = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
            if not isinstance(row_data, dict):
                continue
            voucher_id = row_data.get('Voucher ID')
            vtype = get_voucher_type(voucher_id, patterns)
            cb_name = None
            if vtype in ['CPV', 'CRV']:
                cb_name = extract_cashbook_name(row_data, patterns)
                if cb_name:
                    print(f"✅ [CASHBOOK] Row {row['id']}: {voucher_id} -> {vtype} -> cashbook: {cb_name}")
            if debug:
                debug_info.append({
                    "row_id": row['id'],
                    "voucher_id": voucher_id,
                    "voucher_type": vtype,
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
    
    print(f"🔍 [CASHBOOK] Final cashbooks found: {cashbooks}")
    
    if debug:
        return {
            "cashbooks": sorted(list(cashbooks)),
            "count": len(cashbooks),
            "debug": {
                "total_rows": len(rows),
                "patterns_used": patterns,
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
    created_by: str = "Analyst"
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
    
    patterns = get_voucher_patterns()
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
            vtype = get_voucher_type(voucher_id, patterns)
            if vtype not in ['CPV', 'CRV']:
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
                    'vtype': vtype,
                    'rows': [],
                    'cashbook_account': None,
                    'has_cashbook_line': False
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
        vtype = data['vtype']
        cashbook_account = None
        for row in data['rows']:
            if vtype == 'CPV' and row['credit'] > 0:
                cashbook_account = row['account_name']
                break
            elif vtype == 'CRV' and row['debit'] > 0:
                cashbook_account = row['account_name']
                break
        if cashbook_account is None or cashbook_account != cashbook_name:
            continue
        for row in data['rows']:
            if vtype == 'CPV' and row['debit'] > 0 and row['credit'] == 0:
                transactions_to_verify.append({
                    'client_data_id': row['row_id'],
                    'row_data': row['row_data'],
                    'voucher_id': vid,
                    'extract_date': row['extract_date'],
                    'extract_amount': row['extract_amount']
                })
            elif vtype == 'CRV' and row['credit'] > 0 and row['debit'] == 0:
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
        INSERT INTO verification_sessions (client_id, cashbook_name, period_start, period_end, created_by, status, total_transactions)
        VALUES (?, ?, ?, ?, ?, 'In Progress', ?)
        """,
        (client_id, cashbook_name, period_start, period_end, created_by, len(transactions_to_verify))
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

@router.get("/api/verification/next/{session_id}")
def get_next_transaction(session_id: int):
    session = execute_query("SELECT id FROM verification_sessions WHERE id = ? AND status != 'Cancelled'", (session_id,))
    if not session:
        raise HTTPException(404, "Verification session not found or cancelled.")
    
    detail = execute_query(
        """
        SELECT id, client_data_id, transaction_ref, line_items, procedure_data, validation_status, validation_message
        FROM verification_details
        WHERE session_id = ? AND status = 'Pending'
        ORDER BY id
        LIMIT 1
        """,
        (session_id,)
    )
    if not detail:
        pending_count = execute_query(
            "SELECT COUNT(*) as count FROM verification_details WHERE session_id = ? AND status = 'Pending'",
            (session_id,)
        )[0]['count']
        if pending_count == 0:
            return {"message": "All transactions in this session have been verified.", "completed": True}
        else:
            raise HTTPException(404, "No pending transaction found.")
    
    d = detail[0]
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
    
    patterns = get_voucher_patterns()
    
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
            vtype = get_voucher_type(voucher_id, patterns)
            if vtype not in ['CPV', 'CRV']:
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
                    'vtype': vtype,
                    'rows': [],
                    'cashbook_account': None
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
        vtype = data['vtype']
        cashbook_account = None
        for row in data['rows']:
            if vtype == 'CPV' and row['credit'] > 0:
                cashbook_account = row['account_name']
                break
            elif vtype == 'CRV' and row['debit'] > 0:
                cashbook_account = row['account_name']
                break
        if cashbook_account is None or cashbook_account != cashbook_name:
            continue
        for row in data['rows']:
            if vtype == 'CPV' and row['debit'] > 0 and row['credit'] == 0:
                transactions.append({
                    'client_data_id': row['row_id'],
                    'row_data': row['row_data'],
                    'voucher_id': vid,
                    'extract_date': row['extract_date'],
                    'extract_amount': row['extract_amount']
                })
            elif vtype == 'CRV' and row['credit'] > 0 and row['debit'] == 0:
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