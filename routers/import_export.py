from fastapi import APIRouter, File, UploadFile, HTTPException, Body
from fastapi.responses import StreamingResponse
import os, json, openpyxl, pandas as pd
from io import BytesIO
from database import execute_query, execute_write, log_audit, get_raw_conn, log_data_audit
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime, time

# ⭐ MUST HAVE THIS LINE
router = APIRouter()

# ---------- FLEXIBLE DATE PARSING (Phase 1 of the date-format requirement) ----------
# Default date-import format. There is no UI to choose a different format yet
# (that's a later phase), so these defaults reproduce today's DD/MM/YYYY-only
# behavior exactly. A later phase will let the caller override these per
# upload session instead of always using the default.
DEFAULT_DATE_DELIMITER = "/"
DEFAULT_DATE_ORDER = "DMY"  # one of DMY, MDY, YMD

def parse_date_flexible(value, delimiter: str = DEFAULT_DATE_DELIMITER, order: str = DEFAULT_DATE_ORDER) -> datetime:
    """
    Parse a date string using an explicit delimiter + component order
    (e.g. delimiter="/", order="DMY" -> "25/12/2025").
    Raises ValueError with a clear message on a malformed string or an
    out-of-range date (e.g. day=45) -- strptime itself enforces range checks.
    """
    order = order.upper()
    format_map = {
        "DMY": f"%d{delimiter}%m{delimiter}%Y",
        "MDY": f"%m{delimiter}%d{delimiter}%Y",
        "YMD": f"%Y{delimiter}%m{delimiter}%d",
    }
    if order not in format_map:
        raise ValueError(f"Unsupported date order '{order}'")
    try:
        return datetime.strptime(str(value).strip(), format_map[order])
    except ValueError:
        raise ValueError(f"'{value}' is not a valid date for format {order} (delimiter '{delimiter}')")


def classify_date_cell(value) -> str:
    """
    Classify a raw cell value from an uploaded Excel file for a Date-typed
    template column, so callers can decide whether to use it as-is or run
    it through parse_date_flexible().
    Returns one of:
      'native'       - already a real date (pandas/openpyxl parsed the cell
                        as a genuine Excel date, e.g. a datetime/Timestamp)
                        -- must NOT be passed to parse_date_flexible().
      'text'         - a non-empty string; needs parse_date_flexible().
      'excel_serial' - a bare number that may be an Excel date serial,
                        handled separately from the text parser.
      'empty'        - no value to classify.
    """
    if value is None:
        return 'empty'
    if isinstance(value, datetime):
        return 'native'
    if isinstance(value, str):
        return 'empty' if value.strip() == '' else 'text'
    if isinstance(value, (int, float)):
        if isinstance(value, float) and pd.isna(value):
            return 'empty'
        return 'excel_serial'
    return 'text'


def excel_serial_to_datetime(value) -> datetime:
    """
    Converts a bare Excel date serial number (classify_date_cell() == 'excel_serial')
    to a datetime. Excel's epoch is 1899-12-30 (accounting for its 1900 leap-year bug).
    Single source of truth for this conversion -- reused by extract_columns_from_row()
    and the date-preview endpoint so they can never disagree.
    """
    from datetime import timedelta
    return datetime(1899, 12, 30) + timedelta(days=value)


# ---------- JSON-SAFE CELL COERCION (Phase 2 of the upload-reliability requirement) ----------
def json_safe_cell_value(value, date_fmt: str = '%d/%m/%Y'):
    """
    Coerces a raw cell value (as read by pandas/openpyxl) into something safe to
    store in a row_data dict and pass straight to json.dumps(). Single source of
    truth for this coercion -- every place in this file that builds a row_data
    dict from a raw Excel row should go through this instead of repeating the
    isna/isinstance checks inline.

    This is the actual root-cause fix for a real crash (ERR-UNK-001 "Object of
    type time is not JSON serializable", seen repeatedly against /api/upload/import
    and /api/upload/validate): a Date-typed column's cell can come back from
    pandas as a bare datetime.time (e.g. a trailing totals/subtotal row's Date
    cell with odd/time-only formatting) rather than a full datetime.datetime or
    NaN/NaT. datetime.time is neither caught by pd.isna() nor by
    isinstance(value, datetime) (datetime.time does not subclass datetime.datetime),
    so it was falling through untouched into a raw row_data dict and then blowing
    up the first time json.dumps() saw it, with no error handling around it.
    """
    if pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.strftime(date_fmt)
    if isinstance(value, time):
        return value.strftime('%H:%M:%S')
    return value


# ---------- ROW EXCLUSION (Phase 1 of the row-exclusion requirement) ----------
def filter_excluded_rows(rows: list, excluded_rows: Optional[List[int]]) -> list:
    """
    Removes rows the user marked for exclusion before this upload session
    (row_number matches validation_errors[].row_number: header row=1, first data row=2).
    Excluded rows are session-only and never persisted -- this is the single point
    where exclusion is applied, so callers downstream (validation, import,
    document_group_id grouping) never see excluded rows at all. Returns a new
    list; None/empty excluded_rows is a no-op (identical to today's behavior).
    """
    if not excluded_rows:
        return rows
    excluded_set = set(excluded_rows)
    return [row for idx, row in enumerate(rows, start=2) if idx not in excluded_set]


def count_new_and_existing_rows(client_id: int, template_id: int, rows: list, template_cols: list, mapping_dict: dict):
    """Count rows that are new versus already present for the same client/template."""
    existing_rows = execute_query(
        """
        SELECT row_data
        FROM client_data
        WHERE client_id = ? AND template_id = ? AND is_deleted = 0
        """,
        (client_id, template_id),
    )

    seen_existing = set()
    for row in existing_rows:
        raw = row.get("row_data")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                seen_existing.add(json.dumps(parsed, sort_keys=True, ensure_ascii=False))
            except Exception:
                seen_existing.add(raw)

    new_count = 0
    existing_count = 0

    for row in rows:
        try:
            row_data = {}
            for t_col in template_cols:
                mapping = mapping_dict.get(t_col["id"])
                if mapping and mapping.excel_column_name and mapping.excel_column_name in row:
                    row_data[t_col["column_name"]] = json_safe_cell_value(row[mapping.excel_column_name])

            row_json = json.dumps(row_data, sort_keys=True, ensure_ascii=False)
            if row_json in seen_existing:
                existing_count += 1
            else:
                new_count += 1
                seen_existing.add(row_json)
        except Exception:
            # A row that can't be parsed/serialized is already going to be
            # flagged by the main validation loop -- don't let it crash the
            # whole /validate request just to compute this count.
            continue

    return new_count, existing_count


# ---------- CHANGE DETECTION (Phase 3: document-level grouping using document_group_id) ----------
def compare_transactions(client_id: int, template_id: int, template_cols: list,
                         incoming_rows: list, col_mapping: dict, mapping_dict: dict) -> dict:
    """
    Compares incoming transaction rows with existing data for the same client+template.
    Groups rows by document_group_id (derived from the first column / Document ID).
    If ANY row within a document has changed, the ENTIRE document is treated as changed.

    Returns a dict with counts AND per-document data for the import loop to act upon.
    """
    result = {
        "unchanged_count": 0,
        "changed_count": 0,
        "new_count": 0,
        "missing_count": 0,
        "total_existing_before_import": 0,
        # Detailed lists consumed by the import loop:
        "unchanged": [],      # list of existing client_data_ids to skip
        "changed": [],        # list of {client_data_id, old_row_data, new_row_data, old_validation_status}
        "new": [],            # list of new row_data dicts to insert
        "missing": []         # list of {client_data_id, old_row_data} to flag as 'deleted'
    }

    # Fetch existing client_data rows for this client+template, with their document_group_id and validation status
    existing_rows = execute_query(
        """
        SELECT cd.id, cd.row_data, cd.version, cd.document_group_id,
               vd.validation_status, vd.id AS verification_detail_id
        FROM client_data cd
        LEFT JOIN verification_details vd ON vd.client_data_id = cd.id
        WHERE cd.client_id = ? AND cd.template_id = ? AND cd.is_deleted = 0 AND cd.change_detection_status != 'deleted'
        """,
        (client_id, template_id)
    )
    result["total_existing_before_import"] = len(existing_rows)

    # Group existing rows by document_group_id
    # existing_docs: doc_group_id -> { client_data_ids, row_datas, validation_statuses, ... }
    existing_docs = {}
    for row in existing_rows:
        try:
            row_data = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
        except Exception:
            continue
        doc_group_id = row.get('document_group_id') or f"legacy-{row['id']}"
        if doc_group_id not in existing_docs:
            existing_docs[doc_group_id] = {
                "client_data_ids": [],
                "row_datas": [],
                "versions": [],
                "validation_statuses": [],
                "verification_detail_ids": []
            }
        existing_docs[doc_group_id]["client_data_ids"].append(row['id'])
        existing_docs[doc_group_id]["row_datas"].append(row_data)
        existing_docs[doc_group_id]["versions"].append(row.get('version', 1))
        existing_docs[doc_group_id]["validation_statuses"].append(row.get("validation_status") or "Pending")
        existing_docs[doc_group_id]["verification_detail_ids"].append(row.get("verification_detail_id"))

    # Build incoming row data with their document_group_id
    incoming_docs = {}  # doc_group_id -> list of row_data dicts
    for raw_row in incoming_rows:
        row_data = {}
        for t_col_name, excel_col in col_mapping.items():
            if excel_col in raw_row:
                row_data[t_col_name] = json_safe_cell_value(raw_row[excel_col])

        # Compute document_group_id for this incoming row
        doc_group_id = get_document_group_id(template_cols, row_data, template_id)
        if doc_group_id not in incoming_docs:
            incoming_docs[doc_group_id] = []
        incoming_docs[doc_group_id].append(row_data)

    # Track which doc_group_ids appear in the incoming data
    seen_doc_ids = set()

    for doc_group_id, incoming_rows_data in incoming_docs.items():
        seen_doc_ids.add(doc_group_id)

        if doc_group_id in existing_docs:
            existing = existing_docs[doc_group_id]
            existing_row_datas = existing["row_datas"]

            # Compare ALL rows of this document
            # If incoming has different number of rows OR any field differs → changed
            is_same = len(incoming_rows_data) == len(existing_row_datas)
            if is_same:
                for new_row_data, old_row_data in zip(incoming_rows_data, existing_row_datas):
                    all_fields = set(list(new_row_data.keys()) + list(old_row_data.keys()))
                    for field in all_fields:
                        new_val = new_row_data.get(field)
                        old_val = old_row_data.get(field)
                        if str(new_val or '').strip() != str(old_val or '').strip():
                            is_same = False
                            break
                    if not is_same:
                        break

            if is_same:
                # Unchanged: preserve validation status – skip all rows in this document
                result["unchanged_count"] += len(existing["client_data_ids"])
                result["unchanged"].extend(existing["client_data_ids"])
            else:
                # Changed: overwrite ALL rows of this document
                # Pair incoming rows with existing rows 1:1 (or handle mismatch)
                for i, new_row_data in enumerate(incoming_rows_data):
                    new_row_data_for_import = new_row_data
                    if i < len(existing["client_data_ids"]):
                        result["changed"].append({
                            "client_data_id": existing["client_data_ids"][i],
                            "old_row_data": existing["row_datas"][i],
                            "new_row_data": new_row_data_for_import,
                            "old_validation_status": existing["validation_statuses"][i],
                            "old_version": existing["versions"][i],
                            "verification_detail_id": existing["verification_detail_ids"][i]
                        })
                        result["changed_count"] += 1
                    else:
                        # More incoming rows than existing – insert extras as new
                        result["new"].append(new_row_data_for_import)
                        result["new_count"] += 1
        else:
            # New document – all rows are new
            for new_row_data in incoming_rows_data:
                result["new"].append(new_row_data)
                result["new_count"] += 1

    # Missing: existing doc_group_ids not present in incoming data
    for doc_group_id, existing in existing_docs.items():
        if doc_group_id not in seen_doc_ids:
            for i, cid in enumerate(existing["client_data_ids"]):
                result["missing"].append({
                    "client_data_id": cid,
                    "old_row_data": existing["row_datas"][i],
                    "validation_status": existing["validation_statuses"][i],
                    "verification_detail_id": existing["verification_detail_ids"][i]
                })
                result["missing_count"] += 1

    return result


# ---------- DATA MODELS ----------
class ColumnMapping(BaseModel):
    template_column_id: int
    template_column_name: str
    excel_column_name: Optional[str] = None
    matched: bool = False
    mapping_status: str = "unmatched"

class ImportRequest(BaseModel):
    template_id: int
    file_id: int
    client_id: int
    mappings: List[ColumnMapping]
    mode: str = "create"
    unique_column_id: Optional[int] = None
    skip_validation: bool = False
    sheet_name: str = "Sheet1"
    # Selected date format (Phase 4 of the date-format requirement). None means
    # "not sent" and falls back to DEFAULT_DATE_DELIMITER/DEFAULT_DATE_ORDER --
    # distinct from "" which is itself a valid delimiter (no delimiter).
    date_delimiter: Optional[str] = None
    date_order: Optional[str] = None
    # Session-only row exclusion (Phase 1 of the row-exclusion requirement).
    # Row numbers match validation_errors[].row_number (header=1, first data row=2).
    # None/absent means "exclude nothing" -- identical to today's behavior.
    excluded_rows: Optional[List[int]] = None

class AutoMapRequest(BaseModel):
    file_id: int
    template_id: int
    sheet_name: str = "Sheet1"

# ---------- DATE PREVIEW MODELS (Phase 5 of the date-format requirement) ----------
class DatePreviewMapping(BaseModel):
    template_column_name: str
    excel_column_name: str

class DatePreviewRequest(BaseModel):
    file_id: int
    sheet_name: str = "Sheet1"
    date_mappings: List[DatePreviewMapping]  # only the Date-typed columns currently mapped
    date_delimiter: Optional[str] = None
    date_order: Optional[str] = None

# ---------- HELPER: Compute document_group_id from the first template column ----------
def get_document_group_id(template_cols: list, row_data: dict, template_id: int) -> str:
    """
    Returns a unique document group ID by taking the value of the first template column
    (lowest sort_order) and prefixing it with the template_id for uniqueness.
    If the column value is empty/missing, returns a fallback hash.
    """
    # Find the first column by sort_order (lowest sort_order = first column)
    first_col = template_cols[0] if template_cols else None
    if first_col:
        doc_value = row_data.get(first_col["column_name"])
        if doc_value is not None and doc_value != '':
            return f"{template_id}-{str(doc_value).strip()}"
    # Fallback: use a hash of all row_data
    import hashlib
    return f"{template_id}-{hashlib.md5(str(row_data).encode()).hexdigest()[:8]}"


# ---------- HELPER: Extract numeric & date columns from row data ----------
def extract_columns_from_row(row_data: dict, template_cols: list,
                              delimiter: str = DEFAULT_DATE_DELIMITER, order: str = DEFAULT_DATE_ORDER) -> tuple:
    """
    Extracts the first numeric and first date value from a row based on template column types.
    Returns (amount, date_str) where amount is float or None, date_str is ISO date or None.
    """
    amount = None
    date_str = None
    for col in template_cols:
        col_name = col['column_name']
        value = row_data.get(col_name)
        if value is None or value == '':
            continue
        if col['data_type'] == 'Numeric':
            try:
                amount = float(value)
                break  # take first numeric
            except:
                pass
        elif col['data_type'] == 'Date':
            try:
                kind = classify_date_cell(value)
                if kind == 'native':
                    date_str = value.strftime('%Y-%m-%d')
                elif kind == 'text':
                    try:
                        dt = parse_date_flexible(value, delimiter, order)
                        date_str = dt.strftime('%Y-%m-%d')
                    except ValueError:
                        pass
                elif kind == 'excel_serial':
                    try:
                        dt = excel_serial_to_datetime(value)
                        date_str = dt.strftime('%Y-%m-%d')
                    except:
                        pass
                break  # take first date
            except:
                pass
    return amount, date_str
# ---------- EXISTING UPLOAD ENDPOINT ----------
@router.post("/api/import-excel")
async def import_excel(file: UploadFile = File(...)):
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(400, "Only .xlsx and .xls files are supported")
    contents = await file.read()
    df = pd.read_excel(BytesIO(contents))
    upload_dir = "uploads"
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, file.filename)
    with open(file_path, "wb") as f:
        f.write(contents)
    file_id = execute_write(
        "INSERT INTO uploaded_files (file_name, file_path, file_type, record_count, status) VALUES (?, ?, ?, ?, ?)",
        (file.filename, file_path, 'excel', len(df), 'Pending')
    )
    log_audit("Analyst", "Upload", "Import", f"Uploaded file: {file.filename} ({len(df)} rows)")
    return {"message": f"File '{file.filename}' uploaded successfully ({len(df)} rows)", "file_id": file_id, "record_count": len(df)}

# ---------- AUTO-MAP ENDPOINT (FIXED with Pydantic model) ----------
@router.post("/api/upload/auto-map")
def auto_map_headers(request: AutoMapRequest):
    file_id = request.file_id
    template_id = request.template_id
    sheet_name = request.sheet_name

    # Fetch template columns
    template_cols = execute_query(
        """
        SELECT m.id AS column_id, m.column_name, m.data_type, p.sort_order
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = ? AND m.is_active = 1
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )
    if not template_cols:
        raise HTTPException(400, "Template has no columns")

    file_info = execute_query("SELECT file_path FROM uploaded_files WHERE id = ?", (file_id,))
    if not file_info:
        raise HTTPException(404, "File not found")

    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=sheet_name)
        excel_headers = df.columns.tolist()
    except Exception as e:
        # Fallback to first sheet if specified sheet doesn't exist
        try:
            df = pd.read_excel(file_info[0]['file_path'])
            excel_headers = df.columns.tolist()
        except Exception as e2:
            raise HTTPException(400, f"Error reading Excel file: {str(e2)}")

    mappings = []
    used = set()
    for t_col in template_cols:
        t_name_clean = t_col['column_name'].strip().lower().replace(" ", "")
        best_match = None
        best_score = 0
        for h in excel_headers:
            if h in used:
                continue
            h_clean = str(h).strip().lower().replace(" ", "")
            if h_clean == t_name_clean:
                best_match = h
                best_score = 1.0
                break
            if t_name_clean in h_clean or h_clean in t_name_clean:
                score = min(len(t_name_clean), len(h_clean)) / max(len(t_name_clean), len(h_clean))
                if score > best_score:
                    best_score = score
                    best_match = h
        if best_match is None and len(excel_headers) > len(template_cols):
            pos = template_cols.index(t_col)
            if pos < len(excel_headers) and excel_headers[pos] not in used:
                best_match = excel_headers[pos]
                best_score = 0.3

        mappings.append({
            "column_id": t_col['column_id'],
            "column_name": t_col['column_name'],
            "data_type": t_col['data_type'],
            "excel_column": best_match,
            "is_matched": best_match is not None,
            "match_score": best_score,
            "sort_order": t_col['sort_order']
        })
        if best_match:
            used.add(best_match)

    return {
        "file_id": file_id,
        "template_id": template_id,
        "excel_headers": excel_headers,
        "mappings": mappings,
        "auto_mapped_count": len([m for m in mappings if m['is_matched']]),
        "total_columns": len(mappings)
    }

# ---------- CHECK DUPLICATES ----------
@router.post("/api/upload/check-duplicates")
def check_duplicates(request: ImportRequest):
    """
    Check if any rows already exist for the given client, template, and unique column.
    Returns a list of duplicate records with old and new values for comparison.
    """
    # Validate template, file, and client
    template = execute_query("SELECT id, template_name FROM templates WHERE id = ? AND is_active = 1", (request.template_id,))
    if not template:
        raise HTTPException(404, "Template not found")
    file_info = execute_query("SELECT file_path FROM uploaded_files WHERE id = ?", (request.file_id,))
    if not file_info:
        raise HTTPException(404, "File not found")
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (request.client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    # Get template columns with their types
    template_cols = execute_query(
        """
        SELECT m.id, m.column_name, m.data_type
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = ? AND m.is_active = 1
        ORDER BY p.sort_order ASC
        """,
        (request.template_id,)
    )
    if not template_cols:
        raise HTTPException(400, "Template has no columns")

    mapping_dict = {m.template_column_id: m for m in request.mappings}

    # Build column mapping (template column name -> Excel column name)
    col_mapping = {}
    for t_col in template_cols:
        mapping = mapping_dict.get(t_col['id'])
        if mapping and mapping.excel_column_name:
            col_mapping[t_col['column_name']] = mapping.excel_column_name

    # Read Excel data
    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=request.sheet_name)
        mapped_excel_cols = [m.excel_column_name for m in request.mappings if m.excel_column_name]
        df = df[[col for col in mapped_excel_cols if col in df.columns]]
    except Exception as e:
        raise HTTPException(400, f"Error reading Excel: {str(e)}")

    rows = df.to_dict('records')

    # Build row data for each row
    row_data_list = []
    for row in rows:
        row_data = {}
        for t_col_name, excel_col in col_mapping.items():
            if excel_col in row:
                value = row[excel_col]
                if pd.isna(value):
                    value = None
                if isinstance(value, datetime):
                    value = value.strftime('%d/%m/%Y')
                row_data[t_col_name] = value
        row_data_list.append(row_data)

    # If no unique column selected, return empty (no duplicates to check)
    if not request.unique_column_id:
        return {"has_duplicates": False, "duplicates": []}

    # Get the unique column name
    col_id_to_name = {col['id']: col['column_name'] for col in template_cols}
    unique_col_name = col_id_to_name.get(request.unique_column_id)
    if not unique_col_name:
        return {"has_duplicates": False, "duplicates": []}

    # Check for duplicates
    duplicates = []
    for row_data in row_data_list:
        unique_value = row_data.get(unique_col_name)
        if unique_value is None or unique_value == '':
            continue

                # Case‑insensitive matching – compare lowercased values
        existing = execute_query(
            """
            SELECT id, row_data, extract_amount, extract_date
            FROM client_data
            WHERE client_id = ? AND template_id = ? AND is_deleted = 0
            AND LOWER(json_extract(row_data, '$."{}"')) = LOWER(?)
            """.format(unique_col_name),
            (request.client_id, request.template_id, str(unique_value))
        )

        if existing:
            old_data = json.loads(existing[0]['row_data']) if existing[0]['row_data'] else {}
            # Get the column names from the template
            col_names = [col['column_name'] for col in template_cols]
            duplicates.append({
                "unique_value": unique_value,
                "old_data": old_data,
                "new_data": row_data,
                "columns": col_names  # <-- ADD THIS: all column names for the frontend
            })

    return {
        "has_duplicates": len(duplicates) > 0,
        "duplicates": duplicates,
        "count": len(duplicates)
    }
# ---------- DATE FORMAT LIVE PREVIEW (Phase 5 of the date-format requirement) ----------
@router.post("/api/upload/date-preview")
def preview_date_format(request: DatePreviewRequest):
    """
    Returns up to 3 sample values per mapped Date column, parsed under the
    currently selected delimiter/order, so the user can sanity-check their
    format choice before running full validation/import. Read-only -- no
    DB writes. Reuses classify_date_cell()/parse_date_flexible() exactly as
    validate_only()/validate_and_import() do, so the preview can never
    disagree with what those endpoints will actually do.
    """
    file_info = execute_query("SELECT file_path FROM uploaded_files WHERE id = ?", (request.file_id,))
    if not file_info:
        raise HTTPException(404, "File not found")

    if not request.date_mappings:
        return {"samples": []}

    effective_delimiter = request.date_delimiter if request.date_delimiter is not None else DEFAULT_DATE_DELIMITER
    effective_order = request.date_order if request.date_order else DEFAULT_DATE_ORDER

    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=request.sheet_name)
    except Exception as e:
        raise HTTPException(400, f"Error reading Excel: {str(e)}")

    samples = []
    for mapping in request.date_mappings:
        excel_col = mapping.excel_column_name
        if excel_col not in df.columns:
            continue
        # First 3 non-empty values in this column.
        col_values = [v for v in df[excel_col].tolist() if not (isinstance(v, float) and pd.isna(v)) and v is not None][:3]
        for value in col_values:
            kind = classify_date_cell(value)
            if kind == 'native':
                samples.append({
                    "column_name": mapping.template_column_name,
                    "raw": str(value),
                    "parsed": value.strftime('%d %B %Y'),
                    "valid": True
                })
            elif kind == 'excel_serial':
                try:
                    dt = excel_serial_to_datetime(value)
                    samples.append({
                        "column_name": mapping.template_column_name,
                        "raw": str(value),
                        "parsed": dt.strftime('%d %B %Y'),
                        "valid": True
                    })
                except Exception as e:
                    samples.append({
                        "column_name": mapping.template_column_name,
                        "raw": str(value),
                        "parsed": None,
                        "valid": False,
                        "error": str(e)
                    })
            elif kind == 'text':
                try:
                    dt = parse_date_flexible(value, effective_delimiter, effective_order)
                    samples.append({
                        "column_name": mapping.template_column_name,
                        "raw": str(value),
                        "parsed": dt.strftime('%d %B %Y'),
                        "valid": True
                    })
                except ValueError as e:
                    samples.append({
                        "column_name": mapping.template_column_name,
                        "raw": str(value),
                        "parsed": None,
                        "valid": False,
                        "error": str(e)
                    })
            # 'empty' cells produce no sample -- nothing meaningful to preview.

    return {"samples": samples}


# ---------- VALIDATE ONLY (NO IMPORT) ----------
@router.post("/api/upload/validate")
def validate_only(request: ImportRequest):
    """
    Validate the Excel file without importing any data.
    Returns row-by-row validation errors.
    """
    # Validate template and file
    template = execute_query("SELECT id, template_name FROM templates WHERE id = ? AND is_active = 1", (request.template_id,))
    if not template:
        raise HTTPException(404, "Template not found")
    file_info = execute_query("SELECT file_path FROM uploaded_files WHERE id = ?", (request.file_id,))
    if not file_info:
        raise HTTPException(404, "File not found")
    
    # Validate client exists
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (request.client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    # Resolve the effective date format: use what the frontend sent, falling back
    # to the module defaults if absent (delimiter checks None since "" is itself valid).
    effective_delimiter = request.date_delimiter if request.date_delimiter is not None else DEFAULT_DATE_DELIMITER
    effective_order = request.date_order if request.date_order else DEFAULT_DATE_ORDER

    # Get template columns with their types
    template_cols = execute_query(
        """
        SELECT m.id, m.column_name, m.data_type
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = ? AND m.is_active = 1
        ORDER BY p.sort_order ASC
        """,
        (request.template_id,)
    )
    if not template_cols:
        raise HTTPException(400, "Template has no columns")

    mapping_dict = {m.template_column_id: m for m in request.mappings}

    # Build column mapping
    col_mapping = {}
    for t_col in template_cols:
        mapping = mapping_dict.get(t_col['id'])
        if mapping and mapping.excel_column_name:
            col_mapping[t_col['column_name']] = mapping.excel_column_name

    # Read Excel data
    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=request.sheet_name)
        mapped_excel_cols = [m.excel_column_name for m in request.mappings if m.excel_column_name]
        df = df[[col for col in mapped_excel_cols if col in df.columns]]
    except Exception as e:
        raise HTTPException(400, f"Error reading Excel: {str(e)}")

    rows = df.to_dict('records')
    # Drop user-excluded rows before anything else sees them (validation, counts).
    rows = filter_excluded_rows(rows, request.excluded_rows)

        # --- Validation (same logic as import but without inserting) ---
    validation_errors = []
    for row_idx, row in enumerate(rows, start=2):  # row 1 = headers, row 2 = first data
        row_errors = []
        row_data_full = {}
        try:
            # Build a full row data dict (template column names -> values)
            for t_col in template_cols:
                mapping = mapping_dict.get(t_col['id'])
                if mapping and mapping.excel_column_name:
                    excel_col = mapping.excel_column_name
                    if excel_col in row:
                        row_data_full[t_col['column_name']] = json_safe_cell_value(row[excel_col])
                    else:
                        row_data_full[t_col['column_name']] = None
            # Now check each column for errors
            for t_col in template_cols:
                mapping = mapping_dict.get(t_col['id'])
                if not mapping or not mapping.excel_column_name:
                    continue
                excel_col = mapping.excel_column_name
                if excel_col not in row:
                    continue
                value = row[excel_col]

                # Required field check
                if pd.isna(value):
                    row_errors.append({
                        "column": t_col['column_name'],
                        "excel_column": excel_col,
                        "error_type": "required",
                        "message": f"Required field '{t_col['column_name']}' is empty"
                    })
                else:
                    # Data type checks
                    if t_col['data_type'] == "Numeric":
                        try:
                            float(value)
                        except:
                            row_errors.append({
                                "column": t_col['column_name'],
                                "excel_column": excel_col,
                                "error_type": "data_type",
                                "message": f"Expected Numeric but got '{value}'"
                            })
                    elif t_col['data_type'] == "Date":
                        kind = classify_date_cell(value)
                        if kind == 'native':
                            pass  # genuine Excel date cell -- already correct, nothing to validate
                        elif kind == 'excel_serial':
                            pass  # Excel serial date – will be handled on import, skip validation here
                        elif kind == 'text':
                            try:
                                parse_date_flexible(value, effective_delimiter, effective_order)
                            except (ValueError, TypeError):
                                row_errors.append({
                                    "column": t_col['column_name'],
                                    "excel_column": excel_col,
                                    "error_type": "data_type",
                                    "message": f"Expected Date ({effective_order}, delimiter '{effective_delimiter}') but got '{value}'"
                                })
        except Exception as e:
            # A row that fails to parse for any unforeseen reason (e.g. an
            # unexpected cell type) must not crash the whole /validate request --
            # surface it as a graceful per-row error instead of a 500.
            row_errors = [{
                "column": None,
                "excel_column": None,
                "error_type": "parse_error",
                "message": f"Row could not be parsed: {e}"
            }]

        if row_errors:
            validation_errors.append({
                "row_number": row_idx,
                "row_data": row_data_full,  # <-- NEW: full row data
                "errors": row_errors
            })

    new_count, existing_count = count_new_and_existing_rows(
        request.client_id,
        request.template_id,
        rows,
        template_cols,
        mapping_dict,
    )

    return {
        "status": "validation_complete",
        "has_errors": len(validation_errors) > 0,
        "error_count": len(validation_errors),
        "total_rows": len(rows),
        "errors": validation_errors,
        "valid_rows": len(rows) - len(validation_errors),
        "new_rows_count": new_count,
        "existing_rows_count": existing_count,
    }

# ---------- VALIDATE & IMPORT ----------
@router.post("/api/upload/import")
def validate_and_import(request: ImportRequest):
    # Validate template and file
    template = execute_query("SELECT id, template_name FROM templates WHERE id = ? AND is_active = 1", (request.template_id,))
    if not template:
        raise HTTPException(404, "Template not found")
    file_info = execute_query("SELECT file_path FROM uploaded_files WHERE id = ?", (request.file_id,))
    if not file_info:
        raise HTTPException(404, "File not found")
    # Validate client exists
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (request.client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")

    # Resolve the effective date format: use what the frontend sent, falling back
    # to the module defaults if absent (delimiter checks None since "" is itself valid).
    effective_delimiter = request.date_delimiter if request.date_delimiter is not None else DEFAULT_DATE_DELIMITER
    effective_order = request.date_order if request.date_order else DEFAULT_DATE_ORDER

    # Get template columns with their types
    template_cols = execute_query(
        """
        SELECT m.id, m.column_name, m.data_type
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = ? AND m.is_active = 1
        ORDER BY p.sort_order ASC
        """,
        (request.template_id,)
    )
    if not template_cols:
        raise HTTPException(400, "Template has no columns")

    mapping_dict = {m.template_column_id: m for m in request.mappings}

    # Build column mapping (template column name -> Excel column name)
    col_mapping = {}
    for t_col in template_cols:
        mapping = mapping_dict.get(t_col['id'])
        if mapping and mapping.excel_column_name:
            col_mapping[t_col['column_name']] = mapping.excel_column_name

    # Read Excel data
    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=request.sheet_name)
        # Keep only columns that are mapped
        mapped_excel_cols = [m.excel_column_name for m in request.mappings if m.excel_column_name]
        df = df[[col for col in mapped_excel_cols if col in df.columns]]
    except Exception as e:
        raise HTTPException(400, f"Error reading Excel: {str(e)}")
    print(f"🔍 DataFrame shape: {df.shape}")  # rows, columns
    print(f"🔍 DataFrame columns: {df.columns.tolist()}")
    rows = df.to_dict('records')
    # Drop user-excluded rows before validation, import, and document_group_id
    # grouping (compare_transactions) ever see them.
    rows = filter_excluded_rows(rows, request.excluded_rows)

    # --- Validation ---
    validation_errors = []
    for row_idx, row in enumerate(rows, start=2):
        row_errors = []
        try:
            for t_col in template_cols:
                mapping = mapping_dict.get(t_col['id'])
                if not mapping or not mapping.excel_column_name:
                    continue
                excel_col = mapping.excel_column_name
                if excel_col not in row:
                    continue
                value = row[excel_col]
                if pd.isna(value):
                    row_errors.append({
                        "column": t_col['column_name'],
                        "excel_column": excel_col,
                        "error_type": "required",
                        "message": f"Required field '{t_col['column_name']}' is empty"
                    })
                else:
                    if t_col['data_type'] == "Numeric":
                        try:
                            float(value)
                        except:
                            row_errors.append({
                                "column": t_col['column_name'],
                                "excel_column": excel_col,
                                "error_type": "data_type",
                                "message": f"Expected Numeric but got '{value}'"
                            })
                    elif t_col['data_type'] == "Date":
                        kind = classify_date_cell(value)
                        if kind == 'native':
                            pass  # genuine Excel date cell -- already correct, nothing to validate
                        elif kind == 'excel_serial':
                            pass  # Excel serial date – will be handled on import, skip validation here
                        elif kind == 'text':
                            try:
                                parse_date_flexible(value, effective_delimiter, effective_order)
                            except (ValueError, TypeError):
                                row_errors.append({
                                    "column": t_col['column_name'],
                                    "excel_column": excel_col,
                                    "error_type": "data_type",
                                    "message": f"Expected Date ({effective_order}, delimiter '{effective_delimiter}') but got '{value}'"
                                })
        except Exception as e:
            # Same graceful-degradation guarantee as validate_only(): a row that
            # fails to parse must not crash the whole /import request.
            row_errors = [{
                "column": None,
                "excel_column": None,
                "error_type": "parse_error",
                "message": f"Row could not be parsed: {e}"
            }]
        if row_errors:
            validation_errors.append({"row_number": row_idx, "errors": row_errors})

    if validation_errors:
        return {
            "status": "validation_failed",
            "errors": validation_errors,
            "total_rows": len(rows),
            "error_count": len(validation_errors)
        }

    # ---- CHANGE DETECTION (Phase 2): Compare incoming rows with existing data ----
    # Change detection always runs; it groups rows by Document ID and identifies
    # unchanged, changed, new, and missing documents.
    change_detection = compare_transactions(
        client_id=request.client_id,
        template_id=request.template_id,
        template_cols=template_cols,
        incoming_rows=rows,
        col_mapping=col_mapping,
        mapping_dict=mapping_dict
    )

    # ---- IMPORT: Use change_detection result to decide per-row action ----
    # Update uploaded_files status and set client_id
    execute_write(
        "UPDATE uploaded_files SET status = 'Imported', client_id = ? WHERE id = ?",
        (request.client_id, request.file_id)
    )

    inserted_count = 0
    updated_count = 0
    deleted_count = 0

    if change_detection is not None and change_detection.get("unchanged") is not None:
        # --- Re-upload scenario: handle each category ---
        # 1. Unchanged: skip — retain existing validation status (no action needed)
        #    (These rows already exist in client_data and their validation_status is preserved)

        # 2. Changed: update row_data, reset validation_status to 'Pending', log old values
        for item in change_detection["changed"]:
            cid = item["client_data_id"]
            new_version = (item.get("old_version") or 1) + 1
            new_row_data = item["new_row_data"]

            # Compute document_group_id from the new row_data
            doc_group_id = get_document_group_id(template_cols, new_row_data, request.template_id)

            # Update the existing row_data, increment version, update document_group_id, set change_detection_status='updated'
            execute_write(
                """
                UPDATE client_data
                SET row_data = ?, version = ?, document_group_id = ?, change_detection_status = 'updated'
                WHERE id = ?
                """,
                (json.dumps(new_row_data), new_version, doc_group_id, cid)
            )

            # Log the old values for audit trail
            log_data_audit(cid, "Analyst", "UPDATE", item["old_row_data"], new_row_data)

            # Reset validation_status to 'Pending' if a verification_detail row exists
            if item.get("verification_detail_id"):
                execute_write(
                    "UPDATE verification_details SET validation_status = 'Pending' WHERE id = ?",
                    (item["verification_detail_id"],)
                )

            updated_count += 1

        # 3. New: insert fresh rows
        for new_row_data in change_detection["new"]:
            amount, date_str = extract_columns_from_row(new_row_data, template_cols, effective_delimiter, effective_order)
            doc_group_id = get_document_group_id(template_cols, new_row_data, request.template_id)
            row_id = execute_write(
                """
                INSERT INTO client_data (client_id, template_id, upload_id, row_data, extract_amount, extract_date, created_by, version, change_detection_status, document_group_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'added', ?)
                """,
                (request.client_id, request.template_id, request.file_id,
                 json.dumps(new_row_data), amount, date_str, "Analyst", 1, doc_group_id)
            )
            inserted_count += 1
            log_data_audit(row_id, "Analyst", "INSERT", None, new_row_data)

        # 4. Missing: flag existing rows as 'deleted' for auditor review (soft flag)
        for item in change_detection["missing"]:
            cid = item["client_data_id"]
            execute_write(
                """
                UPDATE client_data
                SET change_detection_status = 'deleted'
                WHERE id = ?
                """,
                (cid,)
            )
            log_data_audit(cid, "Analyst", "DELETE", item["old_row_data"], None)
            deleted_count += 1

    else:
        # --- First-time upload (no existing data with valid composite key): insert all rows as new ---
        for row in rows:
            # Build JSON row_data
            row_data = {}
            for t_col_name, excel_col in col_mapping.items():
                if excel_col in row:
                    row_data[t_col_name] = json_safe_cell_value(row[excel_col])

            # Compute document_group_id from the first column value
            doc_group_id = get_document_group_id(template_cols, row_data, request.template_id)

            # Extract amount and date for indexing
            amount, date_str = extract_columns_from_row(row_data, template_cols, effective_delimiter, effective_order)

            # Insert every row as new
            row_id = execute_write(
                """
                INSERT INTO client_data (client_id, template_id, upload_id, row_data, extract_amount, extract_date, created_by, version, change_detection_status, document_group_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (request.client_id, request.template_id, request.file_id,
                 json.dumps(row_data), amount, date_str, "Analyst", 1, doc_group_id)
            )
            inserted_count += 1
            log_data_audit(row_id, "Analyst", "INSERT", None, row_data)

    log_audit("Analyst", "Import", "Upload",
              f"Imported {inserted_count} new, {updated_count} updated, {deleted_count} flagged missing for client {request.client_id}, template {template[0]['template_name']}, file ID {request.file_id}")

    return {
        "status": "success",
        "message": f"Successfully imported: {inserted_count} new, {updated_count} updated, {deleted_count} flagged missing",
        "total_rows": len(rows),
        "imported_count": inserted_count,
        "updated_count": updated_count,
        "deleted_count": deleted_count,
        "validation_errors": [],
        "change_detection": change_detection  # Phase 2: detailed breakdown of unchanged/changed/new/missing
    }
# ---------- DOWNLOAD TEMPLATE ----------
@router.get("/api/upload/download-template/{template_id}")
def download_template(template_id: int):
    template = execute_query("SELECT id, template_name FROM templates WHERE id = ? AND is_active = 1", (template_id,))
    if not template:
        raise HTTPException(404, "Template not found")
    columns = execute_query(
        """
        SELECT m.column_name, m.data_type
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = ? AND m.is_active = 1
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )
    if not columns:
        raise HTTPException(400, "Template has no columns")

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        header_row = [col['column_name'] for col in columns]
        sample_row = []
        for col in columns:
            if col['data_type'] == "Numeric":
                sample_row.append(1000)
            elif col['data_type'] == "Date":
                sample_row.append("01/01/2025")
            else:
                sample_row.append("Sample")
        df = pd.DataFrame([sample_row], columns=header_row)
        df.to_excel(writer, index=False, sheet_name='Template')

        workbook = writer.book
        instructions = workbook.create_sheet("Instructions")
        instructions.append(["📋 Template Instructions"])
        instructions.append([""])
        instructions.append(["1. This template contains the columns required for import."])
        instructions.append(["2. The column headers match the template defined in the system."])
        instructions.append(["3. The sample row shows the expected data format for each column."])
        instructions.append(["4. Required columns must be filled."])
        instructions.append([""])
        instructions.append(["📌 Data Type Guidelines:"])
        instructions.append(["- Text: Any alphanumeric value"])
        instructions.append(["- Numeric: Numbers only (e.g., 1000, 25.50)"])
        instructions.append(["- Date: Format DD/MM/YYYY (e.g., 25/12/2025)"])
        instructions.append([""])
        instructions.append(["⚠️ Important:"])
        instructions.append(["- Do not change the column headers."])
        instructions.append(["- Delete the sample row before adding your data."])
        instructions.append(["- Save the file as .xlsx before uploading."])

        for sheet in workbook.sheetnames:
            ws = workbook[sheet]
            for col in ws.columns:
                max_len = 0
                col_letter = col[0].column_letter
                for cell in col:
                    try:
                        if len(str(cell.value)) > max_len:
                            max_len = len(str(cell.value))
                    except:
                        pass
                ws.column_dimensions[col_letter].width = min(max_len + 2, 50)

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=template_{template[0]['template_name']}.xlsx"}
    )

# ---------- GET FILE DETAILS ----------
@router.get("/api/upload/file/{file_id}")
def get_file_details(file_id: int):
    file_info = execute_query("SELECT * FROM uploaded_files WHERE id = ?", (file_id,))
    if not file_info:
        raise HTTPException(404, "File not found")
    return file_info[0]