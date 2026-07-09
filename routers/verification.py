# ==============================================
# ✅ VERIFICATION MODULE – PHASE 2
# Cashbook Identification & Session Start
# ==============================================

from fastapi import APIRouter, HTTPException, Query
from database import execute_query, execute_write, log_audit
from datetime import datetime
import json
from typing import List, Optional

router = APIRouter()

# ==============================================
# HELPER FUNCTIONS
# ==============================================

def get_voucher_patterns() -> dict:
    """Fetch voucher pattern mappings from app_settings."""
    rows = execute_query("SELECT value FROM app_settings WHERE key = 'voucher_patterns'")
    if not rows:
        # Fallback default
        return {"CPV": ["CPV","CV"], "CRV": ["CRV","CR"], "BPV": ["BPV","BV"], "BRV": ["BRV","BR"], "JV": ["JV"]}
    try:
        return json.loads(rows[0]['value'])
    except:
        return {"CPV": ["CPV","CV"], "CRV": ["CRV","CR"], "BPV": ["BPV","BV"], "BRV": ["BRV","BR"], "JV": ["JV"]}

def get_voucher_type(voucher_id: str, patterns: dict) -> str:
    """
    Returns the voucher type: 'CPV', 'CRV', 'BPV', 'BRV', 'JV', or None.
    Compares voucher_id against the pattern lists.
    """
    if not voucher_id:
        return None
    voucher_id = voucher_id.strip()
    for vtype, prefixes in patterns.items():
        for p in prefixes:
            if voucher_id.startswith(p):
                return vtype
    return None

def extract_cashbook_name(row_data: dict, patterns: dict) -> Optional[str]:
    """
    Determines if this row is a cash voucher (CPV or CRV) and returns the cashbook name.
    For CPV: credit > 0 → account_name is cashbook.
    For CRV: debit > 0 → account_name is cashbook.
    Returns None if not a cash voucher or if conditions not met.
    """
    # Assume columns: 'Voucher ID', 'Account Name', 'Debit', 'Credit'
    voucher_id = row_data.get('Voucher ID')
    account_name = row_data.get('Account Name')
    debit = row_data.get('Debit')
    credit = row_data.get('Credit')
    
    if not voucher_id or not account_name:
        return None
    
    # Convert to numeric (could be string)
    try:
        debit = float(debit) if debit else 0.0
        credit = float(credit) if credit else 0.0
    except:
        return None
    
    vtype = get_voucher_type(voucher_id, patterns)
    if vtype == 'CPV' and credit > 0:
        return account_name
    elif vtype == 'CRV' and debit > 0:
        return account_name
    return None

# ==============================================
# ENDPOINTS
# ==============================================

# Existing stats endpoint (preserved)
@router.get("/api/verification/stats")
def get_verification_stats():
    try:
        total = execute_query("SELECT COUNT(*) as count FROM verifications")[0]['count']
        pending = execute_query("SELECT COUNT(*) as count FROM verifications WHERE status = 'Pending'")[0]['count']
        approved = execute_query("SELECT COUNT(*) as count FROM verifications WHERE status = 'Approved'")[0]['count']
    except:
        total = pending = approved = 0
    return {"total": total, "pending": pending, "approved": approved, "rejected": 0}

# ---------- PHASE 2: CASHBOOK LISTING ----------
@router.get("/api/verification/cashbooks")
def get_cashbooks(
    client_id: int = Query(..., description="Client ID"),
    date_start: str = Query(..., description="Start date (YYYY-MM-DD)"),
    date_end: str = Query(..., description="End date (YYYY-MM-DD)")
):
    """
    Returns a list of distinct cashbook names for the given client and date range.
    Cashbooks are derived from CPV/CRV vouchers based on voucher patterns.
    """
    # Validate client exists
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
    
    # Get voucher patterns
    patterns = get_voucher_patterns()
    
    # Query all rows for this client within date range (non-deleted)
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
    for row in rows:
        try:
            row_data = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
            if not isinstance(row_data, dict):
                continue
            cb_name = extract_cashbook_name(row_data, patterns)
            if cb_name:
                cashbooks.add(cb_name)
        except:
            continue
    
    # Return sorted list
    return {"cashbooks": sorted(list(cashbooks)), "count": len(cashbooks)}

# ---------- PHASE 2: START VERIFICATION SESSION ----------
@router.post("/api/verification/start_session")
def start_verification_session(
    client_id: int = Query(..., description="Client ID"),
    cashbook_name: str = Query(..., description="Cashbook name (as extracted)"),
    period_start: str = Query(..., description="Start date (YYYY-MM-DD)"),
    period_end: str = Query(..., description="End date (YYYY-MM-DD)"),
    created_by: str = "Analyst"
):
    """
    Creates a new verification session for the given client, cashbook, and date range.
    Also creates verification_details for each cash voucher transaction found.
    Returns the session ID and number of transactions.
    """
    # Validate client
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")
    
    # Validate dates
    try:
        datetime.fromisoformat(period_start)
        datetime.fromisoformat(period_end)
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")
    
    if period_start > period_end:
        raise HTTPException(400, "Start date must be before or equal to end date.")
    
    # Get voucher patterns
    patterns = get_voucher_patterns()
    
    # Query all transactions for this client and cashbook within date range
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
    
    # Filter rows that belong to this cashbook and are cash vouchers (CPV/CRV)
    # Also we need to identify the "expense" side: for CPV, the debit side (expense) lines; for CRV, the credit side (receipt) lines?
    # According to user requirement: "Separates individual cash books on the basis of credit line account name that shows cashbook,
    # Manually delete the credit entry lines showing cash books, to keep only the expenses"
    # That means for CPV, the credit line is the cashbook, we want the debit lines (expenses). For CRV, the debit line is the cashbook, we want the credit lines (receipts).
    # But the user said: "Payment and receipt vouchers placed separately, preferably all debit [expenses, CPVs] vouchers first and then Credit [Receipt/CRVs] voucher below."
    # Actually they want the cashbook to show expenses (debits for CPV) and receipts (credits for CRV). But the verification is done on the expense side? The user says: "Manually delete the credit entry lines showing cash books, to keep only the expenses" – so they are focusing on expenses. So for verification, they want to verify the expense transactions (debit side of CPV) and the receipt transactions (credit side of CRV)? That is ambiguous.
    
    # However, the user's description: "2.2. Manually delete the credit entry lines showing cash books, to keep only the expenses" – they delete the credit lines that show cashbook, keeping only expenses. So they are verifying expenses (debits) for CPV. For CRV, they might also want to verify receipts? They said "Payment and receipt vouchers placed separately" but the verification process described seems to focus on expenses (they mention bills, purchase orders, etc.). They also mention "For Cash transactions, [Bank and journal vouchers to be discussed in later]" and "2.1. Separates individual cash books on the basis of credit line account name that shows cashbook". So they are only focusing on cash payments (CPV) and cash receipts (CRV) but the verification steps (checking purchase orders, gate passes, etc.) are typically for expenses. They might also want to verify receipts (like customer payments) but they didn't mention specific procedures. For now, we'll include both CPV and CRV transactions, but we'll mark the transaction type (payment or receipt) and the verification procedures may differ. We'll keep it generic for now.
    
    # For the purpose of Phase 2, we simply collect all rows that are cash vouchers (CPV or CRV) and belong to this cashbook.
    # For CPV, we need the debit lines (expenses) – but the row itself is a line from the transaction report; each row has a debit and credit. For CPV, credit > 0 means it's the cashbook line (credit), and debit > 0 lines are expenses. So we want to include rows where debit > 0 (expense) for CPV, and rows where credit > 0 (receipt) for CRV? Actually the user says "Manually delete the credit entry lines showing cash books" – they delete the credit lines, so they keep the debit lines. So for CPV, they keep debit lines (expenses). For CRV, they would keep credit lines (receipts). So we need to filter accordingly.
    
    # We'll implement: For CPV, include rows where debit > 0 (and credit = 0 or maybe the cashbook line has credit > 0, we exclude that). For CRV, include rows where credit > 0 (and debit = 0). This will give the expense lines for CPV and receipt lines for CRV.
    
    # However, the user's requirement: "The cashbook will be as per below logic [Payment and receipt vouchers placed separately, preferably all debit [expenses, CPVs] vouchers first and then Credit [Receipt/CRVs] voucher below." So they want to show debit vouchers (expenses) first, then credit vouchers (receipts). So the verification will be on each of these lines individually.
    
    # So we will collect rows where:
    # - For CPV: debit > 0 (expense lines) – the cashbook line is the one with credit > 0, we skip that.
    # - For CRV: credit > 0 (receipt lines) – the cashbook line is the one with debit > 0, we skip that.
    
    # Also we need to ensure the account_name matches the cashbook_name? Actually the cashbook_name is the account_name of the cashbook line. For CPV, the credit line has account_name = cashbook. For CRV, the debit line has account_name = cashbook. The expense lines have different account names (like purchases, salaries). So we need to filter rows where the cashbook_name (from the credit line for CPV, debit line for CRV) matches the requested cashbook_name. But the row_data we have is a single row (either a debit or credit line). For a given voucher, there are multiple rows (one credit line for cashbook, and several debit lines for expenses). We need to group by voucher to identify the cashbook_name, but the cashbook_name is only on the credit line for CPV and debit line for CRV. So we need to identify which rows are cashbook lines and which are expense lines.
    
    # The user's description: "Separates individual cash books on the basis of credit line account name that shows cashbook" – they are looking at the credit line's account_name to get the cashbook. So for each voucher, they find the line with credit > 0 (for CPV) and that account_name is the cashbook. Then they delete those credit lines and keep the debit lines. So the debit lines are the expenses.
    
    # Therefore, to list cashbooks, we look at rows where credit > 0 and voucher type = CPV -> account_name becomes cashbook. For CRV, we look at debit > 0 and voucher type = CRV -> account_name becomes cashbook.
    
    # To start a session for a specific cashbook, we need to find all debit lines (for CPV) where the corresponding credit line for that voucher has account_name = cashbook. Similarly for CRV, we need to find all credit lines where the corresponding debit line has account_name = cashbook.
    
    # This is more complex: we need to group by voucher ID. For each voucher ID, we need to find the line that is the cashbook line (credit for CPV, debit for CRV) and get its account_name. If that account_name matches the requested cashbook, then we include all other lines of that voucher (the expense lines) for verification.
    
    # So we'll do:
    # 1. Query all rows for client and date range.
    # 2. Group by voucher ID (extract from row_data).
    # 3. For each voucher, determine its type (CPV/CRV) and find the cashbook line (credit > 0 for CPV, debit > 0 for CRV) and get its account_name.
    # 4. If the voucher type is CPV or CRV and the account_name matches the requested cashbook, then collect all other rows of that voucher (those with debit > 0 for CPV, or credit > 0 for CRV) as the transactions to verify.
    
    # We'll implement that.
    
    # Let's do it:
    # First, get all rows
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
    
    # Group by voucher ID
    vouchers = {}
    for row in rows:
        try:
            rd = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
            if not isinstance(rd, dict):
                continue
            voucher_id = rd.get('Voucher ID')
            if not voucher_id:
                continue
            # Determine voucher type
            vtype = get_voucher_type(voucher_id, patterns)
            if vtype not in ['CPV', 'CRV']:
                continue  # skip non-cash
            # Extract debit, credit, account_name
            debit = float(rd.get('Debit', 0) or 0)
            credit = float(rd.get('Credit', 0) or 0)
            account_name = rd.get('Account Name')
            if not account_name:
                continue
            # Store
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
    
    # Now for each voucher, identify the cashbook line and verify if it matches the requested cashbook_name
    transactions_to_verify = []  # will store row_id and row_data for expense lines
    for vid, data in vouchers.items():
        vtype = data['vtype']
        cashbook_account = None
        # Find the cashbook line: for CPV, credit > 0; for CRV, debit > 0
        for row in data['rows']:
            if vtype == 'CPV' and row['credit'] > 0:
                cashbook_account = row['account_name']
                break
            elif vtype == 'CRV' and row['debit'] > 0:
                cashbook_account = row['account_name']
                break
        if cashbook_account is None or cashbook_account != cashbook_name:
            continue  # not this cashbook
        
        # Now collect the expense lines: for CPV, debit > 0 (and credit == 0 or credit line already skipped); for CRV, credit > 0
        for row in data['rows']:
            if vtype == 'CPV' and row['debit'] > 0 and row['credit'] == 0:
                # expense line
                transactions_to_verify.append({
                    'client_data_id': row['row_id'],
                    'row_data': row['row_data'],
                    'voucher_id': vid,
                    'extract_date': row['extract_date'],
                    'extract_amount': row['extract_amount']
                })
            elif vtype == 'CRV' and row['credit'] > 0 and row['debit'] == 0:
                # receipt line
                transactions_to_verify.append({
                    'client_data_id': row['row_id'],
                    'row_data': row['row_data'],
                    'voucher_id': vid,
                    'extract_date': row['extract_date'],
                    'extract_amount': row['extract_amount']
                })
    
    # If no transactions found, raise error
    if not transactions_to_verify:
        raise HTTPException(400, f"No transactions found for cashbook '{cashbook_name}' in the given period.")
    
    # Create session
    session_id = execute_write(
        """
        INSERT INTO verification_sessions (client_id, cashbook_name, period_start, period_end, created_by, status, total_transactions)
        VALUES (?, ?, ?, ?, ?, 'In Progress', ?)
        """,
        (client_id, cashbook_name, period_start, period_end, created_by, len(transactions_to_verify))
    )
    
    # Insert verification_details for each transaction
    for trans in transactions_to_verify:
        # Convert row_data to JSON string (already stored as string in row_data? We'll store it as JSON string)
        row_data_json = json.dumps(trans['row_data']) if isinstance(trans['row_data'], dict) else trans['row_data']
        execute_write(
            """
            INSERT INTO verification_details (session_id, client_data_id, transaction_ref, status, validation_status)
            VALUES (?, ?, ?, 'Pending', 'Pending')
            """,
            (session_id, trans['client_data_id'], trans['voucher_id'])
        )
    
    log_audit(created_by, "Start Verification Session", "Verification", f"Session {session_id}: {cashbook_name}, {len(transactions_to_verify)} transactions")
    
    return {
        "session_id": session_id,
        "cashbook_name": cashbook_name,
        "total_transactions": len(transactions_to_verify),
        "message": f"Verification session started with {len(transactions_to_verify)} transactions."
    }