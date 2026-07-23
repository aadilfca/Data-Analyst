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
supplies (which real Excel column is which field) rather than hardcoded
header-name matching - see validate_column_mapping() below. The standard
mapping is 8 required fields (Account Code, Account Name, Opening Dr/Cr,
Period Dr/Cr, Closing Dr/Cr), plus one optional field (Type, a cross-check-only
display value). The normalized synonym lists (clean_header(), MAPPING_ROLES)
are kept and reused only for suggest_column_mapping()'s best-guess pre-fill,
not for auto-driving the upload itself.

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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from database import execute_query, execute_write, get_raw_conn, log_audit

router = APIRouter()


class AccountOverrideRequest(BaseModel):
    """Body for the Phase 5 row-level classification override endpoint."""
    is_leaf: bool


class CategoryAssignmentRequest(BaseModel):
    """Body for the Assign Categories cascade-assignment endpoint (Section 2,
    Phase 4). force_apply defaults to False (smart-fill) - True (force-apply)
    is only ever sent via the explicit UI icon in Phase 5, never the default
    path. category_id is nullable (confirmed fix) - null means "Not assigned":
    selecting it via the normal path clears just the target account, and
    selecting it with force_apply=True clears the target and cascades that
    clearing to every descendant, same rule as assigning a real category."""
    account_id: int
    category_id: Optional[int] = None
    force_apply: bool = False

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
# Standard 8-field trial balance mapping fix: the original single Debit/Credit pair
# is replaced by 3 Dr/Cr pairs (Opening, Period, Closing) - Period Dr/Cr is the
# direct successor of the old Debit/Credit synonyms (bare "debit"/"dr"/"credit"/"cr"
# only ever mean the current-period movement on a real trial balance), Opening/
# Closing are new pairs alongside it.
OPENING_DR_SYNONYMS = {"openingdr", "openingdebit", "openingbalancedr", "openingbaldr", "obdr", "openingdramount"}
OPENING_CR_SYNONYMS = {"openingcr", "openingcredit", "openingbalancecr", "openingbalcr", "obcr", "openingcramount"}
PERIOD_DR_SYNONYMS = {"perioddr", "perioddebit", "debit", "dr", "debitamount", "duringperioddebit", "movementdr", "transactiondebit"}
PERIOD_CR_SYNONYMS = {"periodcr", "periodcredit", "credit", "cr", "creditamount", "duringperiodcredit", "movementcr", "transactioncredit"}
CLOSING_DR_SYNONYMS = {"closingdr", "closingdebit", "closingbalancedr", "closingbaldr", "cbdr", "closingdramount"}
CLOSING_CR_SYNONYMS = {"closingcr", "closingcredit", "closingbalancecr", "closingbalcr", "cbcr", "closingcramount"}

# Priority order shared by both suggest_column_mapping() and the required-field
# list in validate_column_mapping() - code/name first (required), then the 6
# amount roles (all required, standard 8-field mapping fix), then the one
# optional role (type_column), so no header is ever claimed by two roles at once.
MAPPING_ROLES = [
    ("account_code_column", CODE_SYNONYMS),
    ("account_name_column", NAME_SYNONYMS),
    ("type_column", TYPE_SYNONYMS),
    ("opening_dr_column", OPENING_DR_SYNONYMS),
    ("opening_cr_column", OPENING_CR_SYNONYMS),
    ("period_dr_column", PERIOD_DR_SYNONYMS),
    ("period_cr_column", PERIOD_CR_SYNONYMS),
    ("closing_dr_column", CLOSING_DR_SYNONYMS),
    ("closing_cr_column", CLOSING_CR_SYNONYMS),
]
REQUIRED_MAPPING_ROLES = [
    "account_code_column", "account_name_column",
    "opening_dr_column", "opening_cr_column",
    "period_dr_column", "period_cr_column",
    "closing_dr_column", "closing_cr_column",
]

# Human-readable labels for validation error messages.
MAPPING_ROLE_LABELS = {
    "account_code_column": "Account Code",
    "account_name_column": "Account Name",
    "type_column": "Type",
    "opening_dr_column": "Opening Dr",
    "opening_cr_column": "Opening Cr",
    "period_dr_column": "Period Dr",
    "period_cr_column": "Period Cr",
    "closing_dr_column": "Closing Dr",
    "closing_cr_column": "Closing Cr",
}

# Section 1, Phase 3: header row (in mapping order) for the downloadable standard
# template - the escape hatch for a source file too irregular to map cleanly. Only
# the 8 required fields (Type is optional and not part of the "standard" shape).
# No versioning: if the 8-field standard changes again, this list is simply updated
# and the template is regenerated - there's no "v2" concept, per the locked scope.
STANDARD_TEMPLATE_HEADERS = [
    "Account Code", "Account Name",
    "Opening Dr", "Opening Cr",
    "Period Dr", "Period Cr",
    "Closing Dr", "Closing Cr",
]


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


def find_conflicting_uploads(client_id: int, period_start: str, period_end: str) -> list:
    """
    Section 2, Phase 5: shared exact-period conflict lookup, used by both the
    standalone check endpoint and the upload endpoint's own confirm_overwrite
    gate below - single source of truth so the two can never disagree.

    Exact match only on client_id + period_start + period_end (NOT an overlap
    check) - a different-but-overlapping period, e.g. a monthly TB inside an
    already-uploaded quarterly one, is never a conflict, per the locked scope.
    Matches an existing upload regardless of its status (parsed/error/pending)
    - the schema draws no distinction, and "an upload already exists for this
    exact period" is a simpler, more predictable rule than special-casing a
    prior failed attempt. Returns the raw row list (most recent first) since
    more than one conflicting row can exist (e.g. repeated failed attempts at
    the same period).
    """
    return execute_query(
        """
        SELECT * FROM trial_balance_uploads
        WHERE client_id = ? AND period_start = ? AND period_end = ?
        ORDER BY uploaded_at DESC
        """,
        (client_id, period_start, period_end)
    )


def _validate_period_dates(period_start: str, period_end: str) -> None:
    """
    Shared ISO-date + ordering validation for period_start/period_end, used
    by both the check endpoint and the upload endpoint - same strptime
    pattern already used for as_of_date elsewhere in this file.
    """
    try:
        start_dt = datetime.strptime(period_start, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"'{period_start}' is not a valid period_start date (expected YYYY-MM-DD)")
    try:
        end_dt = datetime.strptime(period_end, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"'{period_end}' is not a valid period_end date (expected YYYY-MM-DD)")
    if end_dt < start_dt:
        raise ValueError(f"period_end ({period_end}) cannot be before period_start ({period_start})")


def _serialize_conflicting_upload(row) -> dict:
    """Shared response shape for a conflicting upload row, reused by both the
    check endpoint's 'existing_upload' and the upload endpoint's 409 detail,
    so the frontend (Phase 6) can render both with one function."""
    return {
        "upload_id": row["id"],
        "as_of_date": row["as_of_date"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "period_type": row["period_type"],
        "file_name": row["file_name"],
        "status": row["status"],
        "row_count": row["row_count"],
        "uploaded_at": row["uploaded_at"],
    }


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
    type_column: Optional[str] = Form(None),
    opening_dr_column: Optional[str] = Form(None),
    opening_cr_column: Optional[str] = Form(None),
    period_dr_column: Optional[str] = Form(None),
    period_cr_column: Optional[str] = Form(None),
    closing_dr_column: Optional[str] = Form(None),
    closing_cr_column: Optional[str] = Form(None),
    # Section 2, Phase 5: optional period tracking + overwrite confirmation.
    # All optional and backward-compatible - omitting period_start/period_end
    # entirely reproduces this endpoint's exact pre-Phase-5 behavior (no
    # conflict check runs at all).
    period_start: Optional[str] = Form(None),
    period_end: Optional[str] = Form(None),
    period_type: Optional[str] = Form(None),
    confirm_overwrite: bool = Form(False),
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

    # 3b. Section 2, Phase 5: period validation + exact-period overwrite gate.
    # Both period_start/period_end must be given together, or neither.
    if bool(period_start) != bool(period_end):
        raise HTTPException(400, "period_start and period_end must both be provided together, or neither.")

    if period_start and period_end:
        try:
            _validate_period_dates(period_start, period_end)
        except ValueError as e:
            raise HTTPException(400, str(e))

        conflicts = find_conflicting_uploads(client_id, period_start, period_end)
        if conflicts:
            if not confirm_overwrite:
                # Reject before anything is created or touched - this is what
                # protects against Phase 6's UI being skipped or a stray API
                # call proceeding blindly, per the confirmed brief.
                raise HTTPException(409, detail={
                    "conflict": True,
                    "message": (
                        f"An upload already exists for this client for the exact period "
                        f"{period_start} to {period_end}. Re-upload with confirm_overwrite=true "
                        f"to replace it."
                    ),
                    "existing_upload": _serialize_conflicting_upload(conflicts[0]),
                    "conflicting_upload_count": len(conflicts),
                })

            # Confirmed overwrite: true delete, not just superseding by
            # convention - the prior exact-period upload(s) and their
            # accounts are removed before the new one is created. Explicit
            # two-step delete rather than relying on ON DELETE CASCADE, since
            # this app's connections don't enable PRAGMA foreign_keys = ON
            # (documented elsewhere in this codebase's own conventions), so a
            # bare parent-row delete would silently leave orphaned account rows.
            for old_upload in conflicts:
                execute_write("DELETE FROM trial_balance_accounts WHERE upload_id = ?", (old_upload["id"],))
                execute_write("DELETE FROM trial_balance_uploads WHERE id = ?", (old_upload["id"],))
            log_audit("Analyst", "Delete", "TrialBalance",
                       f"Overwrote {len(conflicts)} prior trial balance upload(s) for client "
                       f"{client_id}, exact period {period_start} to {period_end}")

    # 4. Create the upload row immediately (status='pending') so even a
    # failed parse below leaves a visible, queryable record rather than
    # silently vanishing.
    upload_id = execute_write(
        """
        INSERT INTO trial_balance_uploads
            (client_id, as_of_date, file_name, status, period_start, period_end, period_type)
        VALUES (?, ?, ?, 'pending', ?, ?, ?)
        """,
        (client_id, as_of_date, file.filename, period_start, period_end, period_type)
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
            "opening_dr_column": opening_dr_column,
            "opening_cr_column": opening_cr_column,
            "period_dr_column": period_dr_column,
            "period_cr_column": period_cr_column,
            "closing_dr_column": closing_dr_column,
            "closing_cr_column": closing_cr_column,
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
            opening_dr_value = parse_amount(row.get(opening_dr_column)) if opening_dr_column else None
            opening_cr_value = parse_amount(row.get(opening_cr_column)) if opening_cr_column else None
            period_dr_value = parse_amount(row.get(period_dr_column)) if period_dr_column else None
            period_cr_value = parse_amount(row.get(period_cr_column)) if period_cr_column else None
            closing_dr_value = parse_amount(row.get(closing_dr_column)) if closing_dr_column else None
            closing_cr_value = parse_amount(row.get(closing_cr_column)) if closing_cr_column else None

            accounts.append({
                "account_code": code,
                "account_name": name_value or None,
                "source_account_type": type_value or None,
                "opening_dr": opening_dr_value,
                "opening_cr": opening_cr_value,
                "period_dr": period_dr_value,
                "period_cr": period_cr_value,
                "closing_dr": closing_dr_value,
                "closing_cr": closing_cr_value,
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
                         classification, opening_dr, opening_cr, period_dr, period_cr,
                         closing_dr, closing_cr, source_account_type, sort_order, raw_row_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        upload_id, account["account_code"], account["account_name"],
                        account["parent_account_code"], account["level"], account["classification"],
                        account["opening_dr"], account["opening_cr"], account["period_dr"], account["period_cr"],
                        account["closing_dr"], account["closing_cr"], account["source_account_type"],
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


@router.get("/api/trial-balance/check-period-conflict")
def check_period_conflict(client_id: int, period_start: str, period_end: str):
    """
    Section 2, Phase 5: read-only "step 1" of the two-step overwrite flow -
    tells the frontend (Phase 6) whether an exact-period upload already
    exists for this client, before the user commits to an actual upload
    call. Touches no data. Exact match only (client_id + period_start +
    period_end) - see find_conflicting_uploads()'s own docstring for why an
    overlapping-but-different period is never flagged.
    """
    try:
        _validate_period_dates(period_start, period_end)
    except ValueError as e:
        raise HTTPException(400, str(e))

    conflicts = find_conflicting_uploads(client_id, period_start, period_end)
    if not conflicts:
        return {"conflict": False, "existing_upload": None}

    return {
        "conflict": True,
        "existing_upload": _serialize_conflicting_upload(conflicts[0]),
        "conflicting_upload_count": len(conflicts),
    }


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


@router.get("/api/trial-balance/download-template")
def download_trial_balance_template():
    """
    Section 1, Phase 3: escape-hatch downloadable standard template for a source
    file too irregular for the mapping window to handle cleanly (e.g. ambiguous
    repeated headers). Just the 8 standard headers, no sample/data rows, no
    per-client scoping - a file built from this still goes through the exact
    same suggest-mapping/upload flow as any other file (no separate "trusted
    format" path), per the locked scope.
    """
    output = BytesIO()
    df = pd.DataFrame(columns=STANDARD_TEMPLATE_HEADERS)
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Trial Balance')
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=trial_balance_standard_template.xlsx"}
    )


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
            "opening_dr": r["opening_dr"],
            "opening_cr": r["opening_cr"],
            "period_dr": r["period_dr"],
            "period_cr": r["period_cr"],
            "closing_dr": r["closing_dr"],
            "closing_cr": r["closing_cr"],
            "source_account_type": r["source_account_type"],
            "manually_overridden": bool(r["manually_overridden"]),
        }
        for r in rows
    ]

    return {"upload_id": upload_id, "accounts": accounts}


@router.get("/api/trial-balance/uploads")
def list_trial_balance_uploads(client_id: int):
    """
    Available Trial Balances, Section 1/Phase 1: list every upload for a
    client, so the upcoming "Available Trial Balances" page (Phase 2) has
    data to populate its dropdown. Returns just enough per upload (period
    dates/type, status, row_count) to build a label like "Q1 2026 (Jul-Sep)"
    without a second call per row.

    Ordered most-recent-period-first (period_end desc), since that's the
    most useful default when browsing - falls back to uploaded_at desc for
    any row with no period_end (e.g. an upload made before Section 2's
    period fields existed), so older/legacy rows still sort predictably
    instead of being pushed to an arbitrary position by a null comparison.

    No filtering/pagination this phase, per the locked scope - a client's
    full upload history is returned in one call.
    """
    client_rows = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not client_rows:
        raise HTTPException(404, "Client not found")

    rows = execute_query(
        """
        SELECT id, period_start, period_end, period_type, status, row_count
        FROM trial_balance_uploads
        WHERE client_id = ?
        ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC
        """,
        (client_id,)
    )

    uploads = [
        {
            "id": r["id"],
            "period_start": r["period_start"],
            "period_end": r["period_end"],
            "period_type": r["period_type"],
            "status": r["status"],
            "row_count": r["row_count"],
        }
        for r in rows
    ]

    return {"client_id": client_id, "uploads": uploads}


@router.delete("/api/trial-balance/uploads/{upload_id}")
def delete_trial_balance_upload(upload_id: int):
    """
    Delete an upload and all its account rows. trial_balance_accounts.upload_id
    declares ON DELETE CASCADE, but this app's connections never enable
    PRAGMA foreign_keys = ON (documented elsewhere in this file/codebase), so
    the cascade would not actually fire - accounts are deleted explicitly
    first, then the upload row, same two-step order/style already used by the
    Section 2/Phase 5 overwrite-conflict delete above.
    """
    _require_upload(upload_id)

    # Count first so the audit log can name how many account rows went with it.
    count_rows = execute_query(
        "SELECT COUNT(*) as count FROM trial_balance_accounts WHERE upload_id = ?",
        (upload_id,)
    )
    account_count = count_rows[0]["count"] if count_rows else 0

    execute_write("DELETE FROM trial_balance_accounts WHERE upload_id = ?", (upload_id,))
    execute_write("DELETE FROM trial_balance_uploads WHERE id = ?", (upload_id,))

    log_audit("Analyst", "Delete", "TrialBalance",
               f"Deleted trial balance upload {upload_id} and its {account_count} account row(s)")

    return {"upload_id": upload_id, "deleted": True, "deleted_account_count": account_count}


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
        "opening_dr": r["opening_dr"],
        "opening_cr": r["opening_cr"],
        "period_dr": r["period_dr"],
        "period_cr": r["period_cr"],
        "closing_dr": r["closing_dr"],
        "closing_cr": r["closing_cr"],
        "source_account_type": r["source_account_type"],
        "manually_overridden": bool(r["manually_overridden"]),
    }


# ---------- ASSIGN CATEGORIES (Section 2, Phase 4: latest-TB lookup + cascade assignment) ----------
# Lets an auditor tag trial_balance_accounts rows with an Audit Rules Category
# (audit_rule_categories, see routers/audit_rules.py). category_id already
# exists on trial_balance_accounts (Phase 3 schema) - this phase is backend
# logic only, no new schema.

def _validate_active_category(category_id: int) -> None:
    """Category must exist and be active before any write - mirrors this
    file's existing _require_upload() style. audit_rule_categories has no
    dedicated getter helper in database.py, so this queries it directly."""
    rows = execute_query(
        "SELECT id FROM audit_rule_categories WHERE id = ? AND is_active = 1",
        (category_id,)
    )
    if not rows:
        raise HTTPException(400, f"Category {category_id} not found or inactive")


def _find_latest_parsed_upload(client_id: int):
    """
    Shared "latest Trial Balance" lookup - the most recent upload (by
    period_end, falling back to uploaded_at as tiebreaker for uploads made
    before period tracking existed) with status='parsed'. Used by both
    get_latest_trial_balance_accounts() and reset_trial_balance_categories()
    (confirmed fix) so "latest" is defined identically in both places.
    Returns the raw upload row, or None if no parsed upload exists.
    """
    upload_rows = execute_query(
        """
        SELECT id, period_start, period_end, period_type, as_of_date
        FROM trial_balance_uploads
        WHERE client_id = ? AND status = 'parsed'
        ORDER BY (period_end IS NULL), period_end DESC, uploaded_at DESC
        LIMIT 1
        """,
        (client_id,)
    )
    return upload_rows[0] if upload_rows else None


@router.get("/api/trial-balance/latest-accounts")
def get_latest_trial_balance_accounts(client_id: int):
    """
    Latest Trial Balance for a client - the most recent upload (by
    period_end, falling back to uploaded_at as tiebreaker for uploads made
    before period tracking existed) with status='parsed'. Returns every
    account with its current category assignment (if any), for the Assign
    Categories page (Phase 5).
    """
    client_rows = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not client_rows:
        raise HTTPException(404, "Client not found")

    upload = _find_latest_parsed_upload(client_id)
    if not upload:
        raise HTTPException(404, "No parsed trial balance found for this client")

    rows = execute_query(
        """
        SELECT a.*, c.name AS category_name, c.is_active AS category_is_active
        FROM trial_balance_accounts a
        LEFT JOIN audit_rule_categories c ON a.category_id = c.id
        WHERE a.upload_id = ?
        ORDER BY a.sort_order
        """,
        (upload["id"],)
    )

    accounts = [
        {
            "id": r["id"],
            "account_code": r["account_code"],
            "account_name": r["account_name"],
            "parent_account_code": r["parent_account_code"],
            "level": r["level"],
            "is_leaf": r["classification"] == "leaf",
            "category_id": r["category_id"],
            "category_name": r["category_name"],
        }
        for r in rows
    ]

    return {
        "upload_id": upload["id"],
        "period_start": upload["period_start"],
        "period_end": upload["period_end"],
        "period_type": upload["period_type"],
        "as_of_date": upload["as_of_date"],
        "accounts": accounts,
    }


@router.put("/api/trial-balance/assign-category")
def assign_trial_balance_category(payload: CategoryAssignmentRequest):
    """
    Assign an Audit Rules Category to one trial balance account, with
    parent-to-child cascade. The target account's own category_id is always
    set. If the target is a leaf, that's the whole operation (force_apply is
    a no-op for a leaf - there's nothing to cascade into).

    If the target is a parent, every descendant (recursively, via the
    parent_account_code hierarchy already persisted by the classification
    engine - no new hierarchy logic here) is cascaded into:
      - smart-fill (force_apply=False, default): only descendants whose
        category_id is currently NULL are updated: any descendant that
        already carries a category (user-set or previously cascaded) is
        left alone.
      - force-apply (force_apply=True): every descendant is overwritten
        regardless of its current value. Only reached via an explicit UI
        action (Phase 5), never the default path.
    """
    account_rows = execute_query("SELECT * FROM trial_balance_accounts WHERE id = ?", (payload.account_id,))
    if not account_rows:
        raise HTTPException(404, "Trial balance account not found")
    target = account_rows[0]

    # category_id is nullable (confirmed fix) - null means "Not assigned", so
    # the active-category check only applies when a real category is given.
    if payload.category_id is not None:
        _validate_active_category(payload.category_id)

    # Target account's own assignment is unconditional - this is the explicit
    # action requested, regardless of force_apply or its current value. When
    # payload.category_id is None this writes NULL, i.e. clears the category.
    execute_write(
        "UPDATE trial_balance_accounts SET category_id = ? WHERE id = ?",
        (payload.category_id, payload.account_id)
    )

    is_leaf = target["classification"] == "leaf"
    descendants_updated = []
    descendants_skipped = []

    if not is_leaf:
        # Fetch every account in this upload once, build a parent_code ->
        # [child rows] map, then walk down from the target's own code to
        # collect every descendant (parents and leaves alike), not just
        # direct children.
        sibling_rows = execute_query(
            "SELECT id, account_code, parent_account_code, category_id FROM trial_balance_accounts WHERE upload_id = ?",
            (target["upload_id"],)
        )
        children_by_parent_code = {}
        for row in sibling_rows:
            parent_code = row["parent_account_code"]
            if parent_code:
                children_by_parent_code.setdefault(parent_code, []).append(row)

        descendants = []
        stack = list(children_by_parent_code.get(target["account_code"], []))
        while stack:
            row = stack.pop()
            descendants.append(row)
            stack.extend(children_by_parent_code.get(row["account_code"], []))

        for row in descendants:
            if payload.force_apply or row["category_id"] is None:
                descendants_updated.append(row["id"])
            else:
                descendants_skipped.append(row["id"])

        if descendants_updated:
            placeholders = ",".join("?" for _ in descendants_updated)
            execute_write(
                f"UPDATE trial_balance_accounts SET category_id = ? WHERE id IN ({placeholders})",
                tuple([payload.category_id] + descendants_updated)
            )

    category_label = payload.category_id if payload.category_id is not None else "None (cleared)"
    log_audit("Analyst", "Update", "TrialBalance",
               f"Assigned category {category_label} to account {target['account_code']} "
               f"(id={payload.account_id}, force_apply={payload.force_apply}); "
               f"{len(descendants_updated)} descendant(s) updated, {len(descendants_skipped)} skipped")

    return {
        "account_id": payload.account_id,
        "category_id": payload.category_id,
        "force_apply": payload.force_apply,
        "is_leaf": is_leaf,
        "descendants_updated_count": len(descendants_updated),
        "descendants_skipped_count": len(descendants_skipped),
        "descendants_updated": descendants_updated,
        "descendants_skipped": descendants_skipped,
    }


@router.post("/api/trial-balance/reset-categories")
def reset_trial_balance_categories(client_id: int):
    """
    Confirmed fix: bulk "Reset" for the Assign Categories page. Sets every
    account's category_id to NULL for the client's CURRENTLY LOADED (latest
    parsed) Trial Balance only - not historical uploads - using the same
    "latest" lookup as get_latest_trial_balance_accounts() (via
    _find_latest_parsed_upload()), so the two endpoints can never disagree on
    which upload "the current one" means.
    """
    client_rows = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not client_rows:
        raise HTTPException(404, "Client not found")

    upload = _find_latest_parsed_upload(client_id)
    if not upload:
        raise HTTPException(404, "No parsed trial balance found for this client")
    upload_id = upload["id"]

    # Count first so the audit log/response can say how many rows actually
    # had a category cleared (rather than reporting the upload's full row
    # count, which would overstate a reset that finds most rows already
    # unassigned).
    count_rows = execute_query(
        "SELECT COUNT(*) as count FROM trial_balance_accounts WHERE upload_id = ? AND category_id IS NOT NULL",
        (upload_id,)
    )
    reset_count = count_rows[0]["count"] if count_rows else 0

    execute_write("UPDATE trial_balance_accounts SET category_id = NULL WHERE upload_id = ?", (upload_id,))

    log_audit("Analyst", "Update", "TrialBalance",
               f"Reset all category assignments for client {client_id}'s latest trial balance "
               f"(upload_id={upload_id}); {reset_count} account(s) had a category cleared")

    return {"upload_id": upload_id, "reset_count": reset_count}
