"""
Trial Balance module backend (original 5-phase build complete; now under a
new Column mapping & standard template requirement - Section 1, Phase 1 of
this file's own changes is done: explicit mapping payload + suggest-mapping
endpoint).

New, dedicated feature - deliberately not an extension of import_export.py/
templates.py (those handle generic template-mapped imports; TB needs
hierarchy-aware parent/leaf classification derived from the account code
structure, not client-specific text markers). As of Section 1/Phase 1,
columns are identified by an explicit column-mapping payload the caller
supplies (which real Excel column is Account Code/Name/Debit/Credit, plus
optional Type/Opening Balance/Closing Balance) rather than hardcoded
header-name matching - see validate_column_mapping() below. The normalized
synonym lists (clean_header(), MAPPING_ROLES) are kept and reused only for
suggest_column_mapping()'s best-guess pre-fill, not for auto-driving the
upload itself.

Endpoints: mapping suggestion (POST /suggest-mapping), upload+classify
(POST /upload), read-only list/summary (GET /accounts, GET /summary), and a
row-level manual classification override (PUT /accounts/{id}/override) for
correcting a misclassified account after review.
"""

import json
import re
from datetime import datetime
from io import BytesIO
from typing import Optional

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from database import execute_query, execute_write, get_raw_conn, log_audit

router = APIRouter()


class AccountOverrideRequest(BaseModel):
    """Body for the Phase 5 row-level classification override endpoint."""
    is_leaf: bool

# ---------- COLUMN IDENTIFICATION ----------
# Trial Balance columns are now identified via an explicit column-mapping
# payload supplied by the caller (Section 1, Phase 1) rather than hardcoded
# header-name matching alone. The synonym sets/clean_header() below are kept
# and reused for suggest_column_mapping() - a best-guess suggestion endpoint
# the frontend mapping window (Phase 2) uses to pre-fill likely matches; they
# no longer drive the upload endpoint's own column identification directly.
CODE_SYNONYMS = {"code", "accountcode", "ledgercode", "accode", "glcode", "accountno", "acno"}
NAME_SYNONYMS = {"name", "accountname", "particulars", "description", "accounttitle", "title"}
TYPE_SYNONYMS = {"type", "mastersub", "accounttype", "classification"}
DEBIT_SYNONYMS = {"debit", "dr", "debitamount"}
CREDIT_SYNONYMS = {"credit", "cr", "creditamount"}
OPENING_BALANCE_SYNONYMS = {"openingbalance", "opening", "obalance", "openingbal"}
CLOSING_BALANCE_SYNONYMS = {"closingbalance", "closing", "cbalance", "closingbal"}

# Priority order shared by both suggest_column_mapping() and the required-field
# list in validate_column_mapping() - code/name/debit/credit first (required),
# then the optional roles, so no header is ever claimed by two roles at once.
MAPPING_ROLES = [
    ("account_code_column", CODE_SYNONYMS),
    ("account_name_column", NAME_SYNONYMS),
    ("type_column", TYPE_SYNONYMS),
    ("debit_column", DEBIT_SYNONYMS),
    ("credit_column", CREDIT_SYNONYMS),
    ("opening_balance_column", OPENING_BALANCE_SYNONYMS),
    ("closing_balance_column", CLOSING_BALANCE_SYNONYMS),
]
REQUIRED_MAPPING_ROLES = ["account_code_column", "account_name_column", "debit_column", "credit_column"]

# Human-readable labels for validation error messages.
MAPPING_ROLE_LABELS = {
    "account_code_column": "Account Code",
    "account_name_column": "Account Name",
    "type_column": "Type",
    "debit_column": "Debit",
    "credit_column": "Credit",
    "opening_balance_column": "Opening Balance",
    "closing_balance_column": "Closing Balance",
}


def clean_header(text) -> str:
    """Normalize a raw column header for synonym matching: lowercase, strip
    every non-alphanumeric character (spaces, '/', '.', etc.), so "A/C Code",
    "Account Code", and "account_code" all normalize to the same string."""
    return re.sub(r'[^a-z0-9]', '', str(text).lower())


def read_trial_balance_excel(contents: bytes, filename: str) -> pd.DataFrame:
    """
    Shared file-reading helper used by both the upload endpoint and the
    suggest-mapping endpoint, so the two can never disagree on how a sheet is
    read. dtype=str forces the ENTIRE sheet to raw strings at the pandas layer
    itself - not just the code column - which is the only way to guarantee
    Excel's date/number auto-conversion (the exact bug that corrupted a code
    like 10-10-25-10 in the Leather Crop sample file) can never happen.
    keep_default_na=False turns blank cells into '' instead of NaN, so blank
    checks are plain string checks.
    """
    engine = "xlrd" if filename.lower().endswith('.xls') else "openpyxl"
    try:
        return pd.read_excel(BytesIO(contents), dtype=str, keep_default_na=False, engine=engine)
    except Exception as e:
        raise ValueError(f"Could not read the Excel file: {e}")


def suggest_column_mapping(headers: list) -> dict:
    """
    Best-guess suggestion for every mapping role, for the frontend mapping
    window (Phase 2) to pre-fill. For each role (in MAPPING_ROLES priority
    order, so no header is claimed by two roles): first try an exact
    normalized-synonym match (same precision as the old identify_columns()),
    then fall back to a bidirectional substring match between the normalized
    header and any synonym - mirroring templates.py's get_close_matches()
    ("typed in x or x in typed"), not a fuzzy-matching library. Returns
    {role: header_name_or_None} for every role - purely a suggestion, never
    used to silently drive the upload itself.
    """
    used_headers = set()
    suggestions = {}
    for role, synonyms in MAPPING_ROLES:
        match = None
        # Pass 1: exact normalized match.
        for header in headers:
            if header in used_headers:
                continue
            if clean_header(header) in synonyms:
                match = header
                break
        # Pass 2: bidirectional substring fallback.
        if match is None:
            for header in headers:
                if header in used_headers:
                    continue
                normalized = clean_header(header)
                if any(normalized in syn or syn in normalized for syn in synonyms):
                    match = header
                    break
        suggestions[role] = match
        if match is not None:
            used_headers.add(match)
    return suggestions


def validate_column_mapping(mapping: dict, headers: list) -> None:
    """
    Validate a caller-supplied column mapping against the file's actual
    headers. Raises ValueError naming every problem at once (not just the
    first one hit) if: a required role (Account Code/Name/Debit/Credit) is
    missing/blank, or any provided mapped column name (required or optional)
    doesn't exactly match a real header in the file. The caller's outer
    except Exception block turns this into a clean 400 + an 'error'-status
    upload row, rather than a raw crash deeper in the classification pipeline.
    """
    problems = []
    header_set = set(headers)

    for role in REQUIRED_MAPPING_ROLES:
        value = mapping.get(role)
        if not value or not str(value).strip():
            problems.append(f"{MAPPING_ROLE_LABELS[role]} column mapping is required.")

    for role, _synonyms in MAPPING_ROLES:
        value = mapping.get(role)
        if value and value not in header_set:
            problems.append(
                f"{MAPPING_ROLE_LABELS[role]} is mapped to '{value}', which is not "
                f"a column in the uploaded file."
            )

    if problems:
        raise ValueError(" ".join(problems))


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
    file: UploadFile = File(...),
    # Column mapping (Section 1, Phase 1) - which real Excel column is which
    # field. Required-by-business-rule fields are still Optional[str] at the
    # FastAPI layer (not Form(...)) so validate_column_mapping() below can
    # produce one combined, clear 400 naming every missing/invalid field at
    # once, rather than FastAPI's generic 422 on the first missing field.
    account_code_column: Optional[str] = Form(None),
    account_name_column: Optional[str] = Form(None),
    debit_column: Optional[str] = Form(None),
    credit_column: Optional[str] = Form(None),
    type_column: Optional[str] = Form(None),
    opening_balance_column: Optional[str] = Form(None),
    closing_balance_column: Optional[str] = Form(None),
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
        df = read_trial_balance_excel(contents, file.filename)

        # Validate the caller-supplied mapping against the file's real headers
        # (Section 1, Phase 1) - replaces the old hardcoded identify_columns()
        # auto-detection. A ValueError here (missing required role, or a
        # mapped column that isn't a real header) is caught by the except
        # block below, same as any other parsing failure.
        mapping = {
            "account_code_column": account_code_column,
            "account_name_column": account_name_column,
            "type_column": type_column,
            "debit_column": debit_column,
            "credit_column": credit_column,
            "opening_balance_column": opening_balance_column,
            "closing_balance_column": closing_balance_column,
        }
        validate_column_mapping(mapping, df.columns.tolist())
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
            code = str(row.get(account_code_column, '')).strip()
            if code == '':
                skipped_blank_count += 1
                continue
            if code in seen_codes:
                duplicate_code_count += 1
                continue
            seen_codes.add(code)

            name_value = str(row.get(account_name_column, '')).strip() if account_name_column else ''
            type_value = str(row.get(type_column, '')).strip() if type_column else ''
            debit_value = parse_amount(row.get(debit_column)) if debit_column else None
            credit_value = parse_amount(row.get(credit_column)) if credit_column else None

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


@router.post("/api/trial-balance/suggest-mapping")
async def suggest_trial_balance_mapping(file: UploadFile = File(...)):
    """
    Section 1, Phase 1: stateless read-only endpoint for the frontend mapping
    window (Phase 2) - takes the same raw Excel file the user is about to
    upload, reads its headers, and returns a best-guess suggestion per
    mapping role so the UI can pre-fill likely matches. Nothing is stored;
    this never touches trial_balance_uploads/trial_balance_accounts.
    """
    if not file.filename or not file.filename.lower().endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "Only .xlsx and .xls files are supported")

    try:
        contents = await file.read()
        df = read_trial_balance_excel(contents, file.filename)
    except ValueError as e:
        raise HTTPException(400, str(e))

    headers = df.columns.tolist()
    return {
        "headers": headers,
        "suggestions": suggest_column_mapping(headers),
    }


def _require_upload(upload_id: int):
    """
    Shared existence check for the two read endpoints below - 404s if the
    given upload_id doesn't exist in trial_balance_uploads at all. An upload
    that exists but has zero accounts (e.g. it errored during Phase 2, or
    genuinely had none) is NOT an error here - it just returns an empty
    list / all-zero counts, since that's a legitimate state, not a bad request.
    """
    upload_rows = execute_query("SELECT id FROM trial_balance_uploads WHERE id = ?", (upload_id,))
    if not upload_rows:
        raise HTTPException(404, "Trial balance upload not found")


# ---------- RETRIEVAL ENDPOINTS (Phase 3 of 5: store + retrieve classified accounts) ----------
# Read-only - no editing/override logic yet (that's Phase 5). Both endpoints
# are scoped to a single upload_id at a time, matching the classification
# engine's own one-upload-at-a-time scope (no cross-period comparison).

@router.get("/api/trial-balance/accounts")
def list_trial_balance_accounts(upload_id: int, is_leaf: Optional[bool] = None):
    """
    List one upload's classified accounts, in original file order. Optional
    is_leaf filter narrows to ledger-only (true) or roll-up-only (false)
    accounts, so the Upload UI (Phase 4) and Review screen (Phase 5) can
    show either the full hierarchy or a ledger-only view.
    """
    _require_upload(upload_id)

    query = "SELECT * FROM trial_balance_accounts WHERE upload_id = ?"
    params = [upload_id]
    if is_leaf is not None:
        # classification is stored as the text 'leaf'/'parent' (Phase 1/2) -
        # is_leaf is the boolean shape the brief asks the response to expose.
        query += " AND classification = ?"
        params.append('leaf' if is_leaf else 'parent')
    query += " ORDER BY sort_order"

    rows = execute_query(query, tuple(params))

    accounts = [
        {
            "id": r["id"],
            "account_code": r["account_code"],
            "account_name": r["account_name"],
            "parent_account_code": r["parent_account_code"],
            "level": r["level"],
            "is_leaf": r["classification"] == "leaf",
            "debit_amount": r["debit_amount"],
            "credit_amount": r["credit_amount"],
            "source_account_type": r["source_account_type"],
            "manually_overridden": bool(r["manually_overridden"]),
        }
        for r in rows
    ]

    return {"upload_id": upload_id, "accounts": accounts}


@router.get("/api/trial-balance/summary")
def get_trial_balance_summary(upload_id: int):
    """
    Counts only (total/leaf/parent) for one upload - no ambiguity-detection
    logic here, since the classification engine (Phase 2) doesn't flag any
    row as ambiguous; that would be a separate, not-yet-confirmed scope
    addition, not something to assume in this phase.
    """
    _require_upload(upload_id)

    rows = execute_query(
        "SELECT classification, COUNT(*) as count FROM trial_balance_accounts WHERE upload_id = ? GROUP BY classification",
        (upload_id,)
    )
    counts = {r["classification"]: r["count"] for r in rows}
    leaf_count = counts.get("leaf", 0)
    parent_count = counts.get("parent", 0)

    return {
        "upload_id": upload_id,
        "total_count": leaf_count + parent_count,
        "leaf_count": leaf_count,
        "parent_count": parent_count,
    }


# ---------- REVIEW / OVERRIDE ENDPOINT (Phase 5 of 5: review screen + dashboard link) ----------
# The classification engine (Phase 2) is automatic and can occasionally be wrong on an
# ambiguous code hierarchy - this is the one write endpoint that lets an auditor correct
# a single row after reviewing it. One row at a time, per the locked "keep it simple" scope
# (no bulk-edit/bulk-approve endpoint).

@router.put("/api/trial-balance/accounts/{account_id}/override")
def override_account_classification(account_id: int, payload: AccountOverrideRequest):
    """
    Manually flip one account's classification (leaf <-> parent) and mark it as
    manually_overridden. Returns the updated row in the same shape
    list_trial_balance_accounts() already uses, so the frontend can splice the
    response straight into its rendered row with no reshaping.
    """
    account_rows = execute_query("SELECT id FROM trial_balance_accounts WHERE id = ?", (account_id,))
    if not account_rows:
        raise HTTPException(404, "Trial balance account not found")

    new_classification = 'leaf' if payload.is_leaf else 'parent'
    execute_write(
        "UPDATE trial_balance_accounts SET classification = ?, manually_overridden = 1 WHERE id = ?",
        (new_classification, account_id)
    )

    updated_rows = execute_query("SELECT * FROM trial_balance_accounts WHERE id = ?", (account_id,))
    r = updated_rows[0]

    log_audit("Analyst", "Update", "TrialBalance",
               f"Manually reclassified account {r['account_code']} as {new_classification} "
               f"(upload_id={r['upload_id']})")

    return {
        "id": r["id"],
        "account_code": r["account_code"],
        "account_name": r["account_name"],
        "parent_account_code": r["parent_account_code"],
        "level": r["level"],
        "is_leaf": r["classification"] == "leaf",
        "debit_amount": r["debit_amount"],
        "credit_amount": r["credit_amount"],
        "source_account_type": r["source_account_type"],
        "manually_overridden": bool(r["manually_overridden"]),
    }
