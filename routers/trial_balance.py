"""
Trial Balance module backend (Phase 2 of 5: parsing & classification engine).

New, dedicated feature - deliberately not an extension of import_export.py/
templates.py (those handle generic template-mapped imports; TB needs
hierarchy-aware parent/leaf classification derived from the account code
structure, not client-specific text markers). There is no column-mapping UI
for this feature, so columns are identified by header-name matching against
a normalized synonym list per role (Code/Name/Type/Debit/Credit) instead of
a fixed position - see clean_header()/identify_columns() below.

This phase implements a single upload endpoint: read the file -> classify
every account as parent/leaf from its code hierarchy -> store the result in
trial_balance_uploads/trial_balance_accounts (Phase 1 schema). There is no
"list accounts" retrieval endpoint (Phase 3) or review/correction UI
(Phase 5) yet.
"""

import json
import re
from datetime import datetime
from io import BytesIO

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from database import execute_query, execute_write, get_raw_conn, log_audit

router = APIRouter()

# ---------- COLUMN IDENTIFICATION ----------
# No column-mapping UI exists for Trial Balance (unlike import_export.py's
# template system), so the source file's own headers are matched against a
# normalized synonym list per role. Matching is exact (post-normalization),
# not substring, and processed in this priority order so a header can never
# be claimed by two roles at once. Only "code" is required - a file with no
# recognizable Code-like header is rejected with a clear error.
CODE_SYNONYMS = {"code", "accountcode", "ledgercode", "accode", "glcode", "accountno", "acno"}
NAME_SYNONYMS = {"name", "accountname", "particulars", "description", "accounttitle", "title"}
TYPE_SYNONYMS = {"type", "mastersub", "accounttype", "classification"}
DEBIT_SYNONYMS = {"debit", "dr", "debitamount"}
CREDIT_SYNONYMS = {"credit", "cr", "creditamount"}


def clean_header(text) -> str:
    """Normalize a raw column header for synonym matching: lowercase, strip
    every non-alphanumeric character (spaces, '/', '.', etc.), so "A/C Code",
    "Account Code", and "account_code" all normalize to the same string."""
    return re.sub(r'[^a-z0-9]', '', str(text).lower())


def identify_columns(columns) -> dict:
    """
    Match the source file's own headers against the role synonym sets above.
    Returns a dict of role -> actual column name (None for an unmatched
    optional role). Raises ValueError if no Code-like column is found -
    the caller turns this into a clean 400 + an 'error'-status upload row,
    rather than a raw crash.
    """
    role_synonyms = [
        ("code", CODE_SYNONYMS),
        ("name", NAME_SYNONYMS),
        ("type", TYPE_SYNONYMS),
        ("debit", DEBIT_SYNONYMS),
        ("credit", CREDIT_SYNONYMS),
    ]
    used_columns = set()
    result = {}
    for role, synonyms in role_synonyms:
        match = None
        for col in columns:
            if col in used_columns:
                continue
            if clean_header(col) in synonyms:
                match = col
                break
        result[role] = match
        if match is not None:
            used_columns.add(match)

    if result["code"] is None:
        raise ValueError(
            "Could not find an Account Code column in the uploaded file. "
            "Expected a header like 'Code' or 'Account Code'."
        )
    return result


def parse_amount(raw_value):
    """
    Best-effort parse of a Debit/Credit cell into a float. Never raises -
    amounts are secondary to classification this phase, so an unparsable or
    blank value simply becomes None rather than blocking the whole upload.
    Handles thousands separators (",") and the accounting parenthesis-negative
    convention (e.g. "(1,234.56)" -> -1234.56).
    """
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if text == '':
        return None
    negative = text.startswith('(') and text.endswith(')')
    if negative:
        text = text[1:-1]
    text = text.replace(',', '').strip()
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def classify_accounts(codes: set) -> dict:
    """
    Classify every code as 'parent' or 'leaf' per the locked rule: a code is
    a leaf unless some OTHER code in the same file starts with
    "this code + '-'".

    Implemented by walking every code's own proper prefixes (dash-delimited,
    e.g. "1-100-001" has proper prefixes "1" and "1-100") and marking any
    prefix that is itself a real code in this file as 'parent'. This is
    mathematically equivalent to the "any other code starts with mine + '-'"
    rule: if some code Y starts with X + '-', then X is necessarily one of
    Y's dash-delimited proper prefixes (since X ends exactly at a dash
    boundary within Y), so walking every code's prefix chain and marking
    matches covers every such X - without an O(n^2) any-other-code scan.
    """
    classification = {code: 'leaf' for code in codes}
    for code in codes:
        parts = code.split('-')
        for i in range(1, len(parts)):
            prefix = '-'.join(parts[:i])
            if prefix in classification:
                classification[prefix] = 'parent'
    return classification


@router.post("/api/trial-balance/upload")
async def upload_trial_balance(
    client_id: int = Form(...),
    as_of_date: str = Form(...),
    file: UploadFile = File(...)
):
    """
    Upload a client's Trial Balance Excel file, classify every account as
    parent/leaf from its code hierarchy, and store the result. One pass:
    upload + classify + store (no separate retrieval/review endpoint yet).
    """
    # 1. Client must exist and be active.
    client_rows = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not client_rows:
        raise HTTPException(404, "Client not found")

    # 2. as_of_date must be a real ISO date.
    try:
        datetime.strptime(as_of_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"'{as_of_date}' is not a valid date (expected YYYY-MM-DD)")

    # 3. Extension check - same two extensions import_excel() already accepts.
    if not file.filename or not file.filename.lower().endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "Only .xlsx and .xls files are supported")

    # 4. Create the upload row immediately (status='pending') so even a
    # failed parse below leaves a visible, queryable record rather than
    # silently vanishing.
    upload_id = execute_write(
        "INSERT INTO trial_balance_uploads (client_id, as_of_date, file_name, status) VALUES (?, ?, ?, 'pending')",
        (client_id, as_of_date, file.filename)
    )

    try:
        contents = await file.read()

        # dtype=str forces the ENTIRE sheet to raw strings at the pandas
        # layer itself - not just the code column - which is the only way
        # to guarantee Excel's date/number auto-conversion (the exact bug
        # that corrupted a code like 10-10-25-10 in the Leather Crop sample
        # file) can never happen. keep_default_na=False turns blank cells
        # into '' instead of NaN, so blank checks are plain string checks.
        engine = "xlrd" if file.filename.lower().endswith('.xls') else "openpyxl"
        try:
            df = pd.read_excel(BytesIO(contents), dtype=str, keep_default_na=False, engine=engine)
        except Exception as e:
            raise ValueError(f"Could not read the Excel file: {e}")

        columns = identify_columns(df.columns.tolist())
        rows = df.to_dict('records')

        # Walk every row once: skip blanks/duplicates, capture the rest.
        # sort_order uses the same "header=1, first data row=2" Excel row
        # numbering convention already established elsewhere in this app.
        seen_codes = set()
        accounts = []  # list of dicts, pre-classification
        skipped_blank_count = 0
        duplicate_code_count = 0

        for idx, row in enumerate(rows):
            row_number = idx + 2
            code = str(row.get(columns["code"], '')).strip()
            if code == '':
                skipped_blank_count += 1
                continue
            if code in seen_codes:
                duplicate_code_count += 1
                continue
            seen_codes.add(code)

            name_value = str(row.get(columns["name"], '')).strip() if columns["name"] else ''
            type_value = str(row.get(columns["type"], '')).strip() if columns["type"] else ''
            debit_value = parse_amount(row.get(columns["debit"])) if columns["debit"] else None
            credit_value = parse_amount(row.get(columns["credit"])) if columns["credit"] else None

            accounts.append({
                "account_code": code,
                "account_name": name_value or None,
                "source_account_type": type_value or None,
                "debit_amount": debit_value,
                "credit_amount": credit_value,
                "sort_order": row_number,
                # raw_row_data is a JSON safety net (mirrors client_data.row_data's
                # existing convention) since TB formats vary across clients -
                # every cell is already a plain string thanks to dtype=str above.
                "raw_row_data": json.dumps(row),
            })

        if not accounts:
            raise ValueError("No valid account rows found in the file (every row had a blank Account Code)")

        # 8. Classify every distinct code from its dash-delimited hierarchy.
        classification = classify_accounts(seen_codes)
        for account in accounts:
            code = account["account_code"]
            parts = code.split('-')
            account["level"] = len(parts)
            account["parent_account_code"] = '-'.join(parts[:-1]) if len(parts) > 1 else None
            account["classification"] = classification[code]

        # 9. Bulk insert in one transaction (same BEGIN/loop/commit/rollback
        # shape already used by templates.py's import_backup()).
        conn = get_raw_conn()
        try:
            conn.execute("BEGIN TRANSACTION")
            for account in accounts:
                conn.execute(
                    """
                    INSERT INTO trial_balance_accounts
                        (upload_id, account_code, account_name, parent_account_code, level,
                         classification, debit_amount, credit_amount, source_account_type,
                         sort_order, raw_row_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        upload_id, account["account_code"], account["account_name"],
                        account["parent_account_code"], account["level"], account["classification"],
                        account["debit_amount"], account["credit_amount"], account["source_account_type"],
                        account["sort_order"], account["raw_row_data"]
                    )
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        # Summarize any skipped/duplicate rows in notes - visible without
        # having blocked the rest of the file.
        notes_parts = []
        if skipped_blank_count:
            notes_parts.append(f"{skipped_blank_count} blank-code row(s) skipped")
        if duplicate_code_count:
            notes_parts.append(f"{duplicate_code_count} duplicate code row(s) skipped")
        notes = "; ".join(notes_parts) if notes_parts else None

        execute_write(
            "UPDATE trial_balance_uploads SET status = 'parsed', row_count = ?, notes = ? WHERE id = ?",
            (len(accounts), notes, upload_id)
        )

        parent_count = sum(1 for a in accounts if a["classification"] == "parent")
        leaf_count = len(accounts) - parent_count

        log_audit("Analyst", "Upload", "TrialBalance",
                   f"Uploaded trial balance for client {client_id}: {len(accounts)} accounts "
                   f"({parent_count} parent, {leaf_count} leaf)")

        return {
            "upload_id": upload_id,
            "row_count": len(accounts),
            "parent_count": parent_count,
            "leaf_count": leaf_count,
            "skipped_blank_count": skipped_blank_count,
            "duplicate_code_count": duplicate_code_count,
        }

    except Exception as e:
        # Leave the upload row visible with status='error' rather than
        # deleting it - the 'error' status + notes column exist for exactly
        # this case (Phase 1 schema).
        execute_write(
            "UPDATE trial_balance_uploads SET status = 'error', notes = ? WHERE id = ?",
            (str(e), upload_id)
        )
        raise HTTPException(400, f"Trial balance parsing failed: {e}")
