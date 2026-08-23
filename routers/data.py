# ==============================================
# 📊 CLIENT DATA BANK – BACKEND (FIXED)
# ==============================================

from fastapi import APIRouter, HTTPException, Query
from database import execute_query_pg, execute_write_pg, log_audit, log_data_audit_pg, get_data_audit_history_pg, diff_row_data, estimate_client_data_size_pg
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime, date
import calendar
import json
# Voucher Supporting-Document Linking (Phase 2 of 6): reuses verification.py's own
# voucher_line_documents lookup instead of duplicating that SQL here - no circular import,
# since routers/verification.py never imports from this module.
from routers.verification import get_assignment_status_map

router = APIRouter()

# ---------- MODELS ----------
class DatasetSummary(BaseModel):
    upload_id: int
    template_name: str
    template_id: int
    file_name: str
    upload_date: str
    record_count: int
    status: str

class RowData(BaseModel):
    id: int
    row_data: dict
    extract_amount: Optional[float]
    extract_date: Optional[str]
    created_at: str

# ---------- HELPER ----------
def _coerce_date_str(value):
    """
    Postgres cutover: extract_date/holiday_date etc. are DATE columns now, not
    TEXT - psycopg2 returns real date/datetime objects, not 'YYYY-MM-DD'
    strings. This normalizes either shape to a plain 'YYYY-MM-DD' string, for
    the string-keyed comparisons/lookups this file does throughout (exact_dates
    sets, date_str-in-dict membership checks, etc.). A value that's already a
    string is returned unchanged.
    """
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime('%Y-%m-%d')
    return value


def _coerce_date_obj(value):
    """
    Companion to _coerce_date_str(): normalizes either a string or an already
    -date/datetime value to a real date/datetime object, for code that needs
    to do ordinal arithmetic (_count_business_days_elapsed()'s day-by-day walk).
    """
    if isinstance(value, (date, datetime)):
        return value
    return datetime.fromisoformat(value)


def format_datetime(dt_str):
    try:
        # Postgres cutover: upload_date/created_at/etc. are TIMESTAMPTZ now, not
        # TEXT - psycopg2 returns a real datetime object, which has no
        # .replace('Z', ...) string method to call.
        if isinstance(dt_str, datetime):
            dt = dt_str
        else:
            dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        return dt.strftime('%d/%m/%Y %H:%M')
    except:
        return dt_str

def attach_history_flags(rows):
    """
    Given a list of row dicts (each with an "id"), batch-check data_audit_log
    for revision history and attach two additive fields per row:
      - has_history: bool  -> True if at least one data_audit_log entry exists
      - revision_count: int -> number of data_audit_log entries for that row
    Uses a single grouped query for the whole page instead of one query per row.
    """
    if not rows:
        return
    row_ids = [r["id"] for r in rows]
    placeholders = ','.join(['%s'] * len(row_ids))
    counts = execute_query_pg(
        f"SELECT client_data_id, COUNT(*) as cnt FROM data_audit_log "
        f"WHERE client_data_id IN ({placeholders}) GROUP BY client_data_id",
        row_ids
    )
    count_lookup = {c["client_data_id"]: c["cnt"] for c in counts}
    for r in rows:
        cnt = count_lookup.get(r["id"], 0)
        r["has_history"] = cnt > 0
        r["revision_count"] = cnt

def attach_assignment_flags(rows):
    """
    Given a list of row dicts (each with an "id"), batch-check voucher_line_documents (via
    routers/verification.py's get_assignment_status_map()) and attach four additive fields
    per row, for the Verification supporting-document search/select UI (Phases 3-4) to grey
    out and label rows already locked to another voucher line:
      - is_assigned: bool                    -> True if this row is linked to any voucher line
      - assigned_to_voucher_line: int|None    -> the voucher_line_id (verification_details.id)
      - assigned_to_voucher_id: str|None      -> the human-readable voucher number
      - assigned_to_document_id: int|None     -> which audit_rule_documents "slot" it's linked under
      - assigned_to_document_name: str|None   -> that Document's name, for display
      - assigned_to_line_srno: int|None       -> the assigned line's 1-based position within its
                                                  own voucher (Voucher Supporting-Document Linking
                                                  follow-up, Phase 2 of 4), so Browse can show
                                                  "{voucher_id} [{line_srno}]" instead of the
                                                  voucher number alone
      - assigned_to_line_client_data_id: int|None -> the STABLE client_data_id of the assigned
                                                  row's own voucher line (bug fix, Phase 3 of 4),
                                                  used instead of the voucher_line_id itself since
                                                  a new verification session recreates
                                                  verification_details rows from scratch for every
                                                  voucher - the frontend compares THIS field
                                                  against the current line's own client_data_id.
    document_id/document_name (bug fix) let the Supporting Documents modal tell "assigned to
    THIS voucher line under a DIFFERENT Document tab" apart from a true same-tab match - without
    them, the Browse modal's Status column and a document tab's own Linked Evidence table could
    disagree about the same row for the same voucher line (see get_assignment_status_map()).
    Uses one batched query for the whole page instead of one query per row, mirroring
    attach_history_flags() above.
    """
    if not rows:
        return
    row_ids = [r["id"] for r in rows]
    assigned_map = get_assignment_status_map(row_ids)
    for r in rows:
        match = assigned_map.get(r["id"])
        if match:
            r["is_assigned"] = True
            r["assigned_to_voucher_id"] = match["voucher_id"]
            r["assigned_to_document_id"] = match["document_id"]
            r["assigned_to_document_name"] = match["document_name"]
            r["assigned_to_line_srno"] = match["line_srno"]
            r["assigned_to_line_client_data_id"] = match["voucher_line_client_data_id"]
        else:
            r["is_assigned"] = False
            r["assigned_to_voucher_id"] = None
            r["assigned_to_document_id"] = None
            r["assigned_to_document_name"] = None
            r["assigned_to_line_srno"] = None
            r["assigned_to_line_client_data_id"] = None

# ---------- ENDPOINTS ----------

@router.get("/api/data/datasets")
def get_datasets(client_id: int = Query(..., description="Client ID")):
    """
    Get all datasets (uploads) for a given client.
    Uses both uploaded_files.client_id and client_data.client_id to handle missing client_id in uploaded_files.
    """
    # Verify client exists
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    # Get all uploads that either have client_id matching OR have client_data rows with that client_id
    uploads = execute_query_pg(
        """
        SELECT
            u.id AS upload_id,
            u.file_name,
            u.upload_date,
            u.status,
            u.record_count,
            COALESCE(t.id, cd.template_id) AS template_id,
            COALESCE(t.template_name, 'Unknown') AS template_name,
            COUNT(cd2.id) AS actual_row_count
        FROM uploaded_files u
        LEFT JOIN client_data cd ON cd.upload_id = u.id AND cd.is_deleted = FALSE
        LEFT JOIN templates t ON t.id = cd.template_id
        LEFT JOIN client_data cd2 ON cd2.upload_id = u.id AND cd2.is_deleted = FALSE
        WHERE (u.client_id = %s OR EXISTS (
            SELECT 1 FROM client_data cd3 WHERE cd3.upload_id = u.id AND cd3.client_id = %s AND cd3.is_deleted = FALSE
        ))
        GROUP BY u.id, t.id, cd.template_id, t.template_name
        ORDER BY u.upload_date DESC
        """,
        (client_id, client_id)
    )
    result = []
    for row in uploads:
        # If template_name is 'Unknown' but we have a template_id, try to fetch name
        template_name = row["template_name"]
        if template_name == "Unknown" and row["template_id"]:
            tpl = execute_query_pg("SELECT template_name FROM templates WHERE id = %s", (row["template_id"],))
            if tpl:
                template_name = tpl[0]["template_name"]

        result.append({
            "upload_id": row["upload_id"],
            "template_name": template_name,
            "template_id": row["template_id"],
            "file_name": row["file_name"],
            "upload_date": format_datetime(row["upload_date"]),
            "record_count": row["actual_row_count"] or 0,
            "status": row["status"]
        })
    
    # Also include uploads that are still pending (no data yet) but have matching client_id
    pending = execute_query_pg(
        """
        SELECT id, file_name, upload_date, status, record_count
        FROM uploaded_files
        WHERE client_id = %s AND status = 'Pending'
        ORDER BY upload_date DESC
        """,
        (client_id,)
    )
    for row in pending:
        # Check if already in result
        if not any(r["upload_id"] == row["id"] for r in result):
            result.append({
                "upload_id": row["id"],
                "template_name": "Pending Import",
                "template_id": None,
                "file_name": row["file_name"],
                "upload_date": format_datetime(row["upload_date"]),
                "record_count": 0,
                "status": row["status"]
            })
    
    return result

@router.get("/api/data/db-size")
def get_client_db_size(client_id: int = Query(..., description="Client ID")):
    """
    Database size visibility (Client Data module): estimated storage footprint
    aggregated across all of this client's templates. See database.py's
    estimate_client_data_size() for the estimation formula/caveats.
    """
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    result = estimate_client_data_size_pg(client_id=client_id)
    result["client_id"] = client_id
    return result


# ==============================================
# 📅 UPLOAD FREQUENCY (Overdue Uploads Phase C1)
# ==============================================

VALID_UPLOAD_FREQUENCIES = ("Daily", "Weekly", "Monthly")

# Overdue Uploads (Phase D1): allowed gap, in business days (Sundays + firm-wide
# holidays excluded), between a client+template's last upload and today before
# it counts as overdue. Weekly/Monthly are business-day equivalents of a
# calendar week (~6 business days) and month (~22 business days), not raw
# calendar-day counts.
ALLOWED_GAP_BUSINESS_DAYS = {"Daily": 1, "Weekly": 6, "Monthly": 22}

class UploadFrequencyUpdate(BaseModel):
    client_id: int
    template_id: int
    frequency: str

@router.put("/api/data/upload-frequency")
def set_upload_frequency(payload: UploadFrequencyUpdate):
    """
    Set (or change) the expected upload frequency for a client+template pair.
    Feeds the later Overdue Uploads detection (Phase D1) - this endpoint only
    stores the setting. Absence of a row means Daily (see get_availability()'s
    frequency_lookup), so this endpoint is only called when a user picks a
    non-default value or explicitly changes an existing one.
    """
    if payload.frequency not in VALID_UPLOAD_FREQUENCIES:
        raise HTTPException(400, f"frequency must be one of {VALID_UPLOAD_FREQUENCIES}")

    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (payload.client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    template = execute_query_pg("SELECT id FROM templates WHERE id = %s AND is_active = TRUE", (payload.template_id,))
    if not template:
        raise HTTPException(404, "Template not found or inactive")

    execute_write_pg(
        """
        INSERT INTO client_template_upload_frequency (client_id, template_id, frequency, updated_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT(client_id, template_id) DO UPDATE SET
            frequency = excluded.frequency,
            updated_at = CURRENT_TIMESTAMP
        """,
        (payload.client_id, payload.template_id, payload.frequency)
    )

    return {"client_id": payload.client_id, "template_id": payload.template_id, "frequency": payload.frequency}


# ==============================================
# 📅 OVERDUE UPLOADS (Phase D1: business-day overdue detection)
# ==============================================

def _get_all_holidays_lookup():
    """
    Unfiltered read of the whole holidays table (unlike
    _resolve_holidays_for_month() above, which is scoped to a single
    year/month - a business-day count here can span many months/years).
    Returns (exact_dates, repeating_month_days):
      - exact_dates: set of 'YYYY-MM-DD' strings for non-repeating holidays
      - repeating_month_days: set of (month, day) tuples for repeats_yearly=1
        holidays, so they match any year
    """
    holiday_rows = execute_query_pg("SELECT holiday_date, repeats_yearly FROM holidays")
    exact_dates = set()
    repeating_month_days = set()
    for h in holiday_rows:
        if h["repeats_yearly"]:
            try:
                d = _coerce_date_obj(h["holiday_date"])
            except ValueError:
                continue
            repeating_month_days.add((d.month, d.day))
        else:
            exact_dates.add(_coerce_date_str(h["holiday_date"]))
    return exact_dates, repeating_month_days


def _is_business_day(date_obj: datetime, exact_dates: set, repeating_month_days: set) -> bool:
    """False for a Sunday or a firm-wide holiday (exact date or repeating month/day match)."""
    if date_obj.strftime('%w') == '0':
        return False
    date_str = date_obj.strftime('%Y-%m-%d')
    if date_str in exact_dates:
        return False
    if (date_obj.month, date_obj.day) in repeating_month_days:
        return False
    return True


def _count_business_days_elapsed(last_upload_date: str, today_str: str, exact_dates: set, repeating_month_days: set) -> int:
    """
    Counts business days strictly after last_upload_date, through today_str
    inclusive. 0 means today is still within the same business day the last
    upload was made (or earlier), i.e. no gap has opened up yet.
    """
    start = _coerce_date_obj(last_upload_date)
    end = _coerce_date_obj(today_str)
    count = 0
    current = start
    while current < end:
        current = current.fromordinal(current.toordinal() + 1)
        if _is_business_day(current, exact_dates, repeating_month_days):
            count += 1
    return count


def compute_overdue_uploads(client_id: Optional[int] = None) -> list:
    """
    Shared core for both the all-clients (routers/dashboard.py) and
    single-client overdue-uploads endpoints. For every client+template pair
    that has at least one client_data row, compares business days elapsed
    since its last upload (MAX(extract_date), the data date - not the file
    import timestamp) against the allowed gap for its saved upload_frequency
    (absent = Daily, same convention as get_availability()'s
    frequency_lookup). Returns only pairs that are actually overdue
    (days_overdue > 0), sorted most-overdue-first.
    """
    where = ["cd.is_deleted = FALSE", "c.is_active = TRUE", "t.is_active = TRUE"]
    params = []
    if client_id is not None:
        where.append("cd.client_id = %s")
        params.append(client_id)
    where_sql = " AND ".join(where)

    # GROUP BY includes c.client_name/t.template_name explicitly (Postgres cutover
    # fix): unlike SQLite, Postgres only infers functional dependency on a
    # grouped column via that TABLE's own primary key being in GROUP BY - cd.id
    # isn't grouped here, cd.client_id/cd.template_id are, so c.client_name/
    # t.template_name (from joined tables) must be listed explicitly or Postgres
    # raises "column must appear in GROUP BY clause".
    pairs = execute_query_pg(
        f"""
        SELECT cd.client_id, c.client_name, cd.template_id, t.template_name,
               MAX(cd.extract_date) as last_upload_date
        FROM client_data cd
        JOIN clients c ON c.id = cd.client_id
        JOIN templates t ON t.id = cd.template_id
        WHERE {where_sql}
        GROUP BY cd.client_id, c.client_name, cd.template_id, t.template_name
        """,
        params
    )

    if client_id is not None:
        freq_rows = execute_query_pg(
            "SELECT client_id, template_id, frequency FROM client_template_upload_frequency WHERE client_id = %s",
            (client_id,)
        )
    else:
        freq_rows = execute_query_pg(
            "SELECT client_id, template_id, frequency FROM client_template_upload_frequency"
        )
    freq_lookup = {(r["client_id"], r["template_id"]): r["frequency"] for r in freq_rows}

    exact_dates, repeating_month_days = _get_all_holidays_lookup()
    today_str = datetime.now().strftime('%Y-%m-%d')

    result = []
    for r in pairs:
        if not r["last_upload_date"]:
            continue
        frequency = freq_lookup.get((r["client_id"], r["template_id"]), "Daily")
        allowed_gap = ALLOWED_GAP_BUSINESS_DAYS[frequency]
        elapsed = _count_business_days_elapsed(r["last_upload_date"], today_str, exact_dates, repeating_month_days)
        days_overdue = elapsed - allowed_gap
        if days_overdue > 0:
            result.append({
                "client_id": r["client_id"],
                "client_name": r["client_name"],
                "template_id": r["template_id"],
                "template_name": r["template_name"],
                "last_upload_date": _coerce_date_str(r["last_upload_date"]),
                "frequency": frequency,
                "days_overdue": days_overdue,
            })

    result.sort(key=lambda x: x["days_overdue"], reverse=True)
    return result


@router.get("/api/data/overdue-uploads")
def get_client_overdue_uploads(client_id: int = Query(..., description="Client ID")):
    """
    Client Data module: overdue-upload list for a single client, sorted
    most-overdue-first. See compute_overdue_uploads() for the detection logic.
    """
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    return {"client_id": client_id, "overdue": compute_overdue_uploads(client_id=client_id)}


# ==============================================
# 📊 PREVIEW ROWS (Aggregated across multiple uploads)
# ==============================================

@router.get("/api/data/preview")
def get_preview_rows(
    client_id: int = Query(..., description="Client ID"),
    template_id: int = Query(..., description="Template ID"),
    date_start: str = Query(..., description="Start date (YYYY-MM-DD)"),
    date_end: str = Query(..., description="End date (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=200, description="Rows per page (max 200)"),
    search: Optional[str] = Query(None, description="Search text across all columns"),
    amount_min: Optional[float] = Query(None, description="Minimum extract_amount"),
    amount_max: Optional[float] = Query(None, description="Maximum extract_amount"),
    sunday_only: bool = Query(False, description="If true, only show rows with extract_date on Sunday"),
    data_quality: Optional[str] = Query(None, description="Filter by data_quality_flag: 'complete' (flag is null) or 'incomplete'")
):
    """
    Get paginated rows for a specific client, template, and date range.
    Aggregates data from all uploads for that template.
    Supports search, amount filters, and Sunday-only filter.
    Returns rows, total count, Sunday count, and pagination info.
    """
    # Validate client and template exist
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    template = execute_query_pg("SELECT id, template_name FROM templates WHERE id = %s AND is_active = TRUE", (template_id,))
    if not template:
        raise HTTPException(404, "Template not found")
    
    # Validate dates
    try:
        datetime.fromisoformat(date_start)
        datetime.fromisoformat(date_end)
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")
    
    if date_start > date_end:
        raise HTTPException(400, "Start date must be before or equal to end date.")

    # Document grouping / subtotal support: resolve the document-key column
    # (first template column), the Date column, and all Amount (Numeric)
    # columns once per request - reused for both the empty-result early
    # return and the main response below, and to compute each row's
    # document_group_id.
    template_cols = get_template_columns_ordered(template_id)
    doc_key_column = template_cols[0]["column_name"] if template_cols else None
    date_column = next((c["column_name"] for c in template_cols if c["data_type"] == "Date"), None)
    amount_columns = [c["column_name"] for c in template_cols if c["data_type"] == "Numeric"]

    # Build WHERE clause dynamically (base filters, excluding sunday_only)
    base_where = [
        "cd.client_id = %s",
        "cd.template_id = %s",
        "cd.is_deleted = FALSE",
        "cd.extract_date BETWEEN %s AND %s"
    ]
    base_params = [client_id, template_id, date_start, date_end]

    # Search in row_data (case-insensitive) – jsonb_each_text is the Postgres
    # equivalent of SQLite's json_each for searching all values in a JSONB column.
    if search:
        base_where.append("EXISTS (SELECT 1 FROM jsonb_each_text(cd.row_data) WHERE LOWER(value) LIKE LOWER(%s))")
        base_params.append(f"%{search}%")

    # Amount range
    if amount_min is not None:
        base_where.append("cd.extract_amount >= %s")
        base_params.append(amount_min)
    if amount_max is not None:
        base_where.append("cd.extract_amount <= %s")
        base_params.append(amount_max)

    # Data quality flag ('incomplete' = force-imported with a blank critical
    # field, see Phase 4; 'complete' = flag is null). Omitted/other = no filter.
    if data_quality == 'incomplete':
        base_where.append("cd.data_quality_flag = 'incomplete'")
    elif data_quality == 'complete':
        base_where.append("cd.data_quality_flag IS NULL")

    # Final filters add sunday_only on top of the base filters (used for the
    # count/data queries); the Sunday-count query below intentionally uses
    # base_where only, so it always reflects the full filtered range.
    final_where = base_where.copy()
    final_params = base_params.copy()
    if sunday_only:
        final_where.append("EXTRACT(DOW FROM cd.extract_date) = 0")

    where_sql = " AND ".join(final_where)

    # Get total count
    count_sql = f"SELECT COUNT(*) as total FROM client_data cd WHERE {where_sql}"
    total_result = execute_query_pg(count_sql, final_params)
    total_count = total_result[0]['total'] if total_result else 0

    if total_count == 0:
        return {
            "template_id": template_id,
            "template_name": template[0]['template_name'],
            "total_rows": 0,
            "sunday_count": 0,
            "page": page,
            "limit": limit,
            "document_key_column": doc_key_column,
            "date_column": date_column,
            "amount_columns": amount_columns,
            "rows": []
        }

    # Get Sunday count (for footer) – uses base filters only, without the
    # sunday_only filter, so it always reflects the full filtered range.
    sunday_where = base_where + ["EXTRACT(DOW FROM cd.extract_date) = 0"]
    sunday_sql = f"SELECT COUNT(*) as sunday_count FROM client_data cd WHERE {' AND '.join(sunday_where)}"
    sunday_result = execute_query_pg(sunday_sql, base_params)
    sunday_count = sunday_result[0]['sunday_count'] if sunday_result else 0

    # Pagination
    offset = (page - 1) * limit
    data_sql = f"""
        SELECT cd.id, cd.row_data, cd.extract_amount, cd.extract_date, cd.data_quality_flag, cd.rate_source
        FROM client_data cd
        WHERE {where_sql}
        ORDER BY cd.extract_date DESC, cd.id
        LIMIT %s OFFSET %s
    """
    data_params = final_params + [limit, offset]
    rows = execute_query_pg(data_sql, data_params)

    # Format rows. row_data is JSONB - execute_query_pg already returns a
    # native dict (or {} if NULL), no json.loads() needed.
    result_rows = []
    for r in rows:
        row_dict = r["row_data"] or {}
        result_rows.append({
            "id": r["id"],
            "row_data": row_dict,
            "extract_amount": r["extract_amount"],
            "extract_date": r["extract_date"],
            "data_quality_flag": r["data_quality_flag"],
            # Rate-derivation requirement, Phase 10 (FINAL): per-row provenance for the Rate
            # column (imported/derived/None), read straight from client_data — not the
            # template-level rate_source setting, since a historic row may have been imported
            # under a since-changed template setting.
            "rate_source": r["rate_source"],
            "document_group_id": compute_preview_document_group_id(row_dict, r["extract_date"], doc_key_column, r["id"])
        })

    # Batch-flag rows that have revision history (one query for the whole page)
    attach_history_flags(result_rows)

    # Voucher Supporting-Document Linking (Phase 2 of 6): batch-flag rows already locked to a
    # voucher line, so the Verification search/select UI (Phases 3-4) can grey them out.
    attach_assignment_flags(result_rows)

    return {
        "template_id": template_id,
        "template_name": template[0]['template_name'],
        "total_rows": total_count,
        "sunday_count": sunday_count,
        "page": page,
        "limit": limit,
        "document_key_column": doc_key_column,
        "date_column": date_column,
        "amount_columns": amount_columns,
        "rows": result_rows
    }
@router.get("/api/data/datasets/{upload_id}/rows")
def get_dataset_rows(
    upload_id: int,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    search: Optional[str] = Query(None, description="Search text (matches any column)"),
    amount_min: Optional[float] = Query(None, description="Minimum amount"),
    amount_max: Optional[float] = Query(None, description="Maximum amount"),
    date_start: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_end: Optional[str] = Query(None, description="End date (YYYY-MM-DD)")
):
    """
    Get paginated rows for a specific upload, with optional search/filters.
    """
    # Check upload exists
    upload = execute_query_pg(
        "SELECT id, client_id, status FROM uploaded_files WHERE id = %s",
        (upload_id,)
    )
    if not upload:
        raise HTTPException(404, "Upload not found")

    # Build WHERE clause dynamically
    where_clauses = ["upload_id = %s AND is_deleted = FALSE"]
    params = [upload_id]

    # Search in row_data JSONB (case-insensitive) - jsonb_each_text is the
    # Postgres equivalent of SQLite's json_each for searching all values.
    if search:
        where_clauses.append(
            "EXISTS (SELECT 1 FROM jsonb_each_text(row_data) WHERE LOWER(value) LIKE LOWER(%s))"
        )
        params.append(f"%{search}%")

    # Amount range
    if amount_min is not None:
        where_clauses.append("extract_amount >= %s")
        params.append(amount_min)
    if amount_max is not None:
        where_clauses.append("extract_amount <= %s")
        params.append(amount_max)

    # Date range
    if date_start:
        where_clauses.append("extract_date >= %s")
        params.append(date_start)
    if date_end:
        where_clauses.append("extract_date <= %s")
        params.append(date_end)

    where_sql = " AND ".join(where_clauses)

    # Get total count with filters
    count_sql = f"SELECT COUNT(*) as count FROM client_data WHERE {where_sql}"
    total = execute_query_pg(count_sql, params)[0]['count']

    if total == 0:
        return {
            "upload_id": upload_id,
            "total_rows": 0,
            "page": page,
            "limit": limit,
            "rows": []
        }

    offset = (page - 1) * limit
    data_sql = f"""
        SELECT id, row_data, extract_amount, extract_date, created_at
        FROM client_data
        WHERE {where_sql}
        ORDER BY id
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])
    rows = execute_query_pg(data_sql, params)

    # row_data is JSONB - already a native dict, no json.loads() needed.
    result_rows = []
    for r in rows:
        row_dict = r["row_data"] or {}
        result_rows.append({
            "id": r["id"],
            "row_data": row_dict,
            "extract_amount": r["extract_amount"],
            "extract_date": r["extract_date"],
            "created_at": format_datetime(r["created_at"])
        })

    # Batch-flag rows that have revision history (one query for the whole page)
    attach_history_flags(result_rows)

    return {
        "upload_id": upload_id,
        "total_rows": total,
        "page": page,
        "limit": limit,
        "rows": result_rows
    }

@router.delete("/api/data/datasets/{upload_id}")
def delete_dataset(upload_id: int):
    """
    Soft-delete all rows of a dataset (set is_deleted=1) and mark upload as 'Deleted'.
    """
    upload = execute_query_pg(
        "SELECT id, file_name FROM uploaded_files WHERE id = %s",
        (upload_id,)
    )
    if not upload:
        raise HTTPException(404, "Upload not found")

    # Fetch all rows in this dataset (to log their data before deletion)
    rows = execute_query_pg(
        "SELECT id, row_data FROM client_data WHERE upload_id = %s AND is_deleted = FALSE",
        (upload_id,)
    )

    # Soft-delete all rows
    execute_write_pg(
        "UPDATE client_data SET is_deleted = TRUE WHERE upload_id = %s",
        (upload_id,)
    )

    # Log each row deletion. row_data is JSONB - already a native dict, no
    # json.loads() needed.
    for row in rows:
        old_data = row["row_data"] or {}
        log_data_audit_pg(row["id"], "Analyst", "DELETE", old_data, None)

    # Update upload status
    execute_write_pg(
        "UPDATE uploaded_files SET status = 'Deleted' WHERE id = %s",
        (upload_id,)
    )
    log_audit("Analyst", "Delete", "DataBank", f"Deleted dataset upload_id={upload_id} ({len(rows)} rows)")

    return {"message": f"Dataset '{upload[0]['file_name']}' deleted successfully ({len(rows)} rows)"}

@router.post("/api/data/datasets/{upload_id}/restore")
def restore_dataset(upload_id: int):
    """
    Restore a soft-deleted dataset.
    """
    upload = execute_query_pg(
        "SELECT id, file_name FROM uploaded_files WHERE id = %s AND status = 'Deleted'",
        (upload_id,)
    )
    if not upload:
        raise HTTPException(404, "Deleted upload not found")

    # Fetch rows that are currently deleted
    rows = execute_query_pg(
        "SELECT id, row_data FROM client_data WHERE upload_id = %s AND is_deleted = TRUE",
        (upload_id,)
    )

    # Restore rows (set is_deleted = FALSE)
    execute_write_pg(
        "UPDATE client_data SET is_deleted = FALSE WHERE upload_id = %s",
        (upload_id,)
    )

    # Log each row restore. row_data is JSONB - already a native dict, no
    # json.loads() needed.
    for row in rows:
        new_data = row["row_data"] or {}
        log_data_audit_pg(row["id"], "Analyst", "RESTORE", None, new_data)

    execute_write_pg(
        "UPDATE uploaded_files SET status = 'Imported' WHERE id = %s",
        (upload_id,)
    )
    log_audit("Analyst", "Restore", "DataBank", f"Restored dataset upload_id={upload_id} ({len(rows)} rows)")

    return {"message": f"Dataset '{upload[0]['file_name']}' restored successfully ({len(rows)} rows)"}

@router.delete("/api/data/template/{template_id}")
def delete_template_data(
    template_id: int,
    client_id: int = Query(..., description="Client ID")
):
    """
    Soft-delete all client_data rows for a given client and template.
    Also marks all associated uploaded_files as 'Deleted'.
    """
    # Verify client exists
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    # Verify template exists and is active
    template = execute_query_pg("SELECT id, template_name FROM templates WHERE id = %s AND is_active = TRUE", (template_id,))
    if not template:
        raise HTTPException(404, "Template not found or inactive")

    # Get all upload_ids for this client+template that have data
    uploads = execute_query_pg(
        """
        SELECT DISTINCT upload_id
        FROM client_data
        WHERE client_id = %s AND template_id = %s AND is_deleted = FALSE
        """,
        (client_id, template_id)
    )

    if not uploads:
        return {"message": f"No data found for template '{template[0]['template_name']}' and client {client_id}."}

    # Count rows to be deleted
    count_result = execute_query_pg(
        "SELECT COUNT(*) as count FROM client_data WHERE client_id = %s AND template_id = %s AND is_deleted = FALSE",
        (client_id, template_id)
    )
    row_count = count_result[0]['count'] if count_result else 0

    # Soft-delete client_data rows
    execute_write_pg(
        "UPDATE client_data SET is_deleted = TRUE WHERE client_id = %s AND template_id = %s AND is_deleted = FALSE",
        (client_id, template_id)
    )

    # Mark uploaded_files as 'Deleted'
    upload_ids = [u['upload_id'] for u in uploads]
    if upload_ids:
        placeholders = ','.join(['%s'] * len(upload_ids))
        execute_write_pg(
            f"UPDATE uploaded_files SET status = 'Deleted' WHERE id IN ({placeholders})",
            upload_ids
        )
    
    log_audit("Analyst", "Delete", "DataBank", f"Deleted {row_count} rows for template {template[0]['template_name']} (client {client_id})")
    
    return {
        "message": f"Deleted {row_count} rows for template '{template[0]['template_name']}'.",
        "rows_deleted": row_count,
        "template_id": template_id,
        "template_name": template[0]['template_name']
    }

# ==============================================
# 📊 ROW REVISION HISTORY ENDPOINT
# ==============================================

@router.get("/api/data/rows/{client_data_id}/history")
def get_row_history(client_data_id: int):
    """
    Return the full revision timeline for a single client_data row (any
    template), built from data_audit_log entries logged by import_export.py
    on INSERT/UPDATE/DELETE/RESTORE. Each revision includes the per-field
    old-value -> new-value changes, computed generically so it works for any
    template's column set.
    """
    row = execute_query_pg("SELECT id FROM client_data WHERE id = %s", (client_data_id,))
    if not row:
        raise HTTPException(404, "Row not found")

    audit_rows = get_data_audit_history_pg(client_data_id)

    # old_row_data/new_row_data are JSONB - already native dicts (or None), no
    # json.loads() needed.
    revisions = []
    for a in audit_rows:
        old_data = a["old_row_data"]
        new_data = a["new_row_data"]
        revisions.append({
            "action": a["action"],
            "user": a["user"],
            "changed_at": format_datetime(a["changed_at"]),
            "changes": diff_row_data(old_data, new_data)
        })

    return {
        "client_data_id": client_data_id,
        "revision_count": len(revisions),
        "revisions": revisions
    }

# ==============================================
# 📊 AVAILABILITY CHECKLIST ENDPOINT
# ==============================================

def get_template_columns_ordered(template_id: int) -> list:
    """
    Returns every active column for a template, ordered by sort_order ASC, as
    {"column_name", "data_type"} dicts - same JOIN shape as
    get_template_document_key_column() below, just selecting data_type too.
    Used by the Data Bank preview's document-grouping/subtotal feature to
    identify the document-key column, the Date column, and all Amount
    (Numeric) columns in one query, mirroring the template_cols convention
    already used by get_document_group_id()/get_critical_field_columns() in
    routers/import_export.py.
    """
    return execute_query_pg(
        """
        SELECT m.column_name, m.data_type
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = %s AND m.is_active = TRUE
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )


def compute_preview_document_group_id(row_data: dict, extract_date: Optional[str], doc_key_column: Optional[str], row_id: int) -> str:
    """
    Groups Data Bank preview rows into documents: same document key (first
    template column, same convention as get_document_group_id() in
    routers/import_export.py) + same date. Falls back to a per-row-unique id
    when there's no key column or the key value is blank, so such rows are
    never silently grouped together with unrelated rows.
    """
    if doc_key_column:
        doc_value = row_data.get(doc_key_column)
        if doc_value is not None and str(doc_value).strip() != '':
            return f"{str(doc_value).strip()}||{extract_date}"
    return f"__row_{row_id}"


def get_template_document_key_column(template_id: int) -> Optional[str]:
    """
    Returns the column_name of a template's unique document key: the first
    column by sort_order, mirroring get_document_group_id()'s (routers/
    import_export.py) "first template column = unique document key"
    convention used elsewhere for voucher/document grouping. Returns None if
    the template has no active columns configured.
    """
    row = execute_query_pg(
        """
        SELECT m.column_name
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = %s AND m.is_active = TRUE
        ORDER BY p.sort_order ASC
        LIMIT 1
        """,
        (template_id,)
    )
    return row[0]["column_name"] if row else None


@router.get("/api/data/availability")
def get_availability(
    client_id: int = Query(..., description="Client ID"),
    template_id: Optional[int] = Query(None, description="Filter by template ID"),
    date_start: str = Query(..., description="Start date (YYYY-MM-DD)"),
    date_end: str = Query(..., description="End date (YYYY-MM-DD)")
):
    """
    Get aggregated availability data for templates for a given client and date range.
    Returns:
    - filtered_row_count: rows matching the date filter
    - total_row_count: ALL rows for this template (unfiltered)
    - document_count: unique documents among ALL rows (unfiltered) - counts
      distinct values of the template's document-key column (first column by
      sort_order, same convention get_document_group_id() uses), so a
      multi-line voucher counts once regardless of how many lines it has.
      Used only by the "Your Datasets" table's "No of Documents" column -
      total_row_count itself is unchanged and still backs the Total Records
      metric tile, the Preview-button data-exists check, and the delete
      confirmation's row count.
    - date_min / date_max: FULL date range of all data for this template
    - sunday_count: Sunday rows matching the filter (for preview context)
    - status: 'complete' if any data exists (unfiltered), else 'missing'
    - filtered_status: 'complete' if filtered rows > 0, else 'missing'
    """
    # Validate client exists
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    # Validate date format
    try:
        datetime.fromisoformat(date_start)
        datetime.fromisoformat(date_end)
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")

    if date_start > date_end:
        raise HTTPException(400, "Start date must be before or equal to end date.")

    # ------------------------------------------------------------
    # 1. FIRST QUERY: Filtered data (matching the date range)
    # ------------------------------------------------------------
    filtered_query = """
        SELECT
            t.id AS template_id,
            t.template_name,
            COALESCE(COUNT(cd.id), 0) AS filtered_row_count,
            COALESCE(SUM(CASE WHEN EXTRACT(DOW FROM cd.extract_date) = 0 THEN 1 ELSE 0 END), 0) AS filtered_sunday_count
        FROM templates t
        LEFT JOIN client_data cd
            ON cd.template_id = t.id
            AND cd.client_id = %s
            AND cd.is_deleted = FALSE
            AND cd.extract_date BETWEEN %s AND %s
    """
    params = [client_id, date_start, date_end]

    if template_id is not None:
        filtered_query += " AND t.id = %s"
        params.append(template_id)

    filtered_query += " WHERE t.is_active = TRUE"
    filtered_query += " GROUP BY t.id ORDER BY t.template_name"

    filtered_rows = execute_query_pg(filtered_query, params)

    # ------------------------------------------------------------
    # 2. SECOND QUERY: Total data (unfiltered, all time)
    # ------------------------------------------------------------
    total_query = """
        SELECT
            t.id AS template_id,
            COALESCE(COUNT(cd.id), 0) AS total_row_count,
            MIN(cd.extract_date) AS date_min,
            MAX(cd.extract_date) AS date_max
        FROM templates t
        LEFT JOIN client_data cd
            ON cd.template_id = t.id
            AND cd.client_id = %s
            AND cd.is_deleted = FALSE
    """
    total_params = [client_id]

    if template_id is not None:
        total_query += " AND t.id = %s"
        total_params.append(template_id)

    total_query += " WHERE t.is_active = TRUE"
    total_query += " GROUP BY t.id ORDER BY t.template_name"

    total_rows = execute_query_pg(total_query, total_params)

    # ------------------------------------------------------------
    # 2b. Unique document counts per template (for "No of Documents") -
    # counts distinct document-key values instead of raw rows, since several
    # lines can belong to one voucher/document. Uses each template's own
    # document-key column (first column by sort_order), same convention
    # get_document_group_id() (routers/import_export.py) already uses.
    # ------------------------------------------------------------
    document_count_lookup = {}
    for row in total_rows:
        tid = row["template_id"]
        key_column = get_template_document_key_column(tid)
        if key_column:
            # json_extract(row_data, '$."col"') -> the JSONB ->> text-extraction
            # operator. key_column comes from master_columns (not raw user
            # input), same trust model as the SQLite version's f-string embed.
            doc_count_result = execute_query_pg(
                f"""
                SELECT COUNT(DISTINCT cd.row_data->>'{key_column}') as doc_count
                FROM client_data cd
                WHERE cd.client_id = %s AND cd.template_id = %s AND cd.is_deleted = FALSE
                """,
                (client_id, tid)
            )
            document_count_lookup[tid] = doc_count_result[0]["doc_count"] if doc_count_result else 0
        else:
            # Template has no configured columns - fall back to the raw row count
            document_count_lookup[tid] = row["total_row_count"]

    # ------------------------------------------------------------
    # 2c. Upload Frequency (Overdue Uploads Phase C1) - one batched query for
    # every template this client has a saved frequency for; absent = Daily.
    # ------------------------------------------------------------
    frequency_rows = execute_query_pg(
        "SELECT template_id, frequency FROM client_template_upload_frequency WHERE client_id = %s",
        (client_id,)
    )
    frequency_lookup = {r["template_id"]: r["frequency"] for r in frequency_rows}

    # ------------------------------------------------------------
    # 3. Merge results
    # ------------------------------------------------------------
    result = []
    
    # Create a lookup for filtered data
    filtered_lookup = {row["template_id"]: row for row in filtered_rows}
    total_lookup = {row["template_id"]: row for row in total_rows}
    
    # Get all template IDs from both queries
    all_template_ids = set(filtered_lookup.keys()) | set(total_lookup.keys())
    
    for tid in all_template_ids:
        filtered = filtered_lookup.get(tid, {"template_name": "Unknown", "filtered_row_count": 0, "filtered_sunday_count": 0})
        total = total_lookup.get(tid, {"total_row_count": 0, "date_min": None, "date_max": None})
        
        # Determine status: if total_row_count > 0, the template has data (complete)
        status = "complete" if total["total_row_count"] > 0 else "missing"
        # Filtered status: if filtered_row_count > 0, the filter matches some data
        filtered_status = "complete" if filtered["filtered_row_count"] > 0 else "missing"
        
        # Database size visibility: per-template estimated storage footprint
        # (unfiltered, all-time - same scope as total_row_count), reusing
        # estimate_client_data_size_pg() rather than a separate endpoint.
        size_estimate = estimate_client_data_size_pg(client_id=client_id, template_id=tid)

        result.append({
            "template_id": tid,
            "template_name": filtered["template_name"] if filtered["template_name"] != "Unknown" else total.get("template_name", f"Template {tid}"),
            "filtered_row_count": filtered["filtered_row_count"],
            "total_row_count": total["total_row_count"],
            "document_count": document_count_lookup.get(tid, total["total_row_count"]),
            "date_min": total["date_min"],           # FULL date range (unfiltered)
            "date_max": total["date_max"],           # FULL date range (unfiltered)
            "filtered_sunday_count": filtered["filtered_sunday_count"],
            "status": status,                        # based on total data
            "filtered_status": filtered_status,      # based on filtered data
            "estimated_size_bytes": size_estimate["total_bytes"],
            "estimated_size_formatted": size_estimate["formatted"],
            "upload_frequency": frequency_lookup.get(tid, "Daily")
        })

    return result

# ==============================================
# 📊 AVAILABILITY CALENDAR ENDPOINT (Phase 3 of 6:
# Client Data Availability & Coverage Redesign)
# ==============================================

def _resolve_holidays_for_month(year: int, month: int) -> dict:
    """
    Build a {date_str: label} lookup of firm-wide holidays (holidays table,
    routers/settings.py CRUD) that fall within the given year/month.
    - Non-repeating holidays: kept only if their stored date is in this exact
      year/month.
    - Repeating holidays (repeats_yearly=1): their month/day is projected onto
      the requested year, so a holiday configured in any past year still
      resolves correctly for a future month/year request.
    Sundays are NOT part of this lookup - they stay separate, hardcoded
    day-of-week logic, per the confirmed Phase 1 design.
    """
    holiday_rows = execute_query_pg("SELECT holiday_date, label, repeats_yearly FROM holidays")

    resolved = {}
    for h in holiday_rows:
        if h["repeats_yearly"]:
            # Project this holiday's month/day onto the requested year.
            try:
                original = _coerce_date_obj(h["holiday_date"])
            except ValueError:
                continue  # skip a malformed stored date rather than fail the whole request
            if original.month != month:
                continue
            date_str = f"{year:04d}-{month:02d}-{original.day:02d}"
        else:
            # holiday_date is a DATE column now (a real date object, not TEXT) -
            # compare year/month directly instead of the old string .startswith()
            # (which a date object has no such method for).
            original = _coerce_date_obj(h["holiday_date"])
            if original.year != year or original.month != month:
                continue
            date_str = _coerce_date_str(h["holiday_date"])
        resolved[date_str] = h["label"]

    return resolved


@router.get("/api/data/availability/calendar")
def get_availability_calendar(
    client_id: int = Query(..., description="Client ID"),
    template_ids: List[int] = Query(..., description="One or more template IDs"),
    year: int = Query(..., description="Calendar year, e.g. 2026"),
    month: int = Query(..., ge=1, le=12, description="Calendar month (1-12)")
):
    """
    Per-day coverage status for one or more templates over a single month, for
    the Data Bank availability calendar (Phase 4). For each requested template,
    every day of the month is classified as exactly one of:
      - present: client_data exists for that template/date (highest priority -
        real data on a Sunday/holiday still counts as present)
      - sunday:  no data, and the day is a Sunday (hardcoded day-of-week check)
      - holiday: no data, not a Sunday, and the day matches a firm-wide holiday
        (see _resolve_holidays_for_month)
      - missing: none of the above
    This is intentionally separate from /api/data/availability, which answers a
    coarser "does this template have any data in this range" question for the
    still-in-use date-picker/buttons UI - that endpoint is unchanged.
    """
    # Validate client exists (same convention as every other Data Bank endpoint)
    client = execute_query_pg("SELECT id FROM clients WHERE id = %s AND is_active = TRUE", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    # Validate every requested template exists and is active
    templates_by_id = {}
    for tid in template_ids:
        tpl = execute_query_pg("SELECT id, template_name FROM templates WHERE id = %s AND is_active = TRUE", (tid,))
        if not tpl:
            raise HTTPException(404, f"Template not found or inactive: {tid}")
        templates_by_id[tid] = tpl[0]["template_name"]

    # Build the list of YYYY-MM-DD strings for every day in the requested month
    days_in_month = calendar.monthrange(year, month)[1]
    month_start = f"{year:04d}-{month:02d}-01"
    month_end = f"{year:04d}-{month:02d}-{days_in_month:02d}"
    all_dates = [f"{year:04d}-{month:02d}-{day:02d}" for day in range(1, days_in_month + 1)]

    # Holiday lookup is shared across all requested templates (firm-wide, not per-template)
    holiday_lookup = _resolve_holidays_for_month(year, month)

    templates_result = []
    for tid in template_ids:
        # Distinct dates this template actually has data for, within the month
        present_rows = execute_query_pg(
            """
            SELECT DISTINCT extract_date FROM client_data
            WHERE client_id = %s AND template_id = %s AND is_deleted = FALSE
              AND extract_date BETWEEN %s AND %s
            """,
            (client_id, tid, month_start, month_end)
        )
        # extract_date is a DATE column now (a real date object) - coerce to the
        # 'YYYY-MM-DD' string form so the `date_str in present_dates` membership
        # check below (against Python-generated strings) keeps working.
        present_dates = {_coerce_date_str(r["extract_date"]) for r in present_rows}

        days = []
        present_count = missing_count = sunday_count = holiday_count = 0
        for date_str in all_dates:
            if date_str in present_dates:
                status = "present"
                present_count += 1
                days.append({"date": date_str, "status": status})
                continue

            is_sunday = datetime.fromisoformat(date_str).strftime('%w') == '0'
            if is_sunday:
                status = "sunday"
                sunday_count += 1
                days.append({"date": date_str, "status": status})
                continue

            if date_str in holiday_lookup:
                status = "holiday"
                holiday_count += 1
                days.append({"date": date_str, "status": status, "label": holiday_lookup[date_str]})
                continue

            status = "missing"
            missing_count += 1
            days.append({"date": date_str, "status": status})

        templates_result.append({
            "template_id": tid,
            "template_name": templates_by_id[tid],
            "present_count": present_count,
            "missing_count": missing_count,
            "sunday_count": sunday_count,
            "holiday_count": holiday_count,
            "days": days
        })

    return {
        "year": year,
        "month": month,
        "templates": templates_result
    }