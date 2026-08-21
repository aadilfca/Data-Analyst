from fastapi import APIRouter, File, UploadFile, HTTPException, Body
from fastapi.responses import StreamingResponse
import os, json, openpyxl, pandas as pd
from io import BytesIO
from database import execute_query, execute_write, log_audit, get_raw_conn, log_data_audit, execute_batch, execute_batch_insert_with_ids
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime, time

# ⭐ MUST HAVE THIS LINE
router = APIRouter()

# In-memory row-write progress for an in-flight import, keyed by file_id (known to
# the frontend before it fires POST /api/upload/import, so it can poll
# GET /api/upload/import-status/{file_id} while that request is still in flight).
# Synchronous single-request import, no queue/background job -- state only lives
# for the duration of one import call.
IMPORT_PROGRESS: dict = {}

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


class _NativeDateValue(datetime):
    """
    Passthrough for a genuine native Excel date cell (see json_safe_cell_value()'s
    preserve_native_dates flag). Stringifies to the same '%d/%m/%Y' text the
    non-preserving path would have produced -- via __str__, used by both
    compare_transactions()'s diff (str(new_val or '')) and json.dumps(...,
    default=str) at the storage call sites -- so change-detection and the stored
    row_data JSON stay byte-identical to before. But because it IS a real datetime
    instance, extract_columns_from_row()'s classify_date_cell() still resolves it
    via the 'native' branch instead of misparsing it against the text-column
    delimiter/order picker (which is only correct for genuinely text-based dates).
    """
    def __str__(self):
        return self.strftime('%d/%m/%Y')


# ---------- JSON-SAFE CELL COERCION (Phase 2 of the upload-reliability requirement) ----------
def json_safe_cell_value(value, date_fmt: str = '%d/%m/%Y', preserve_native_dates: bool = False):
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

    preserve_native_dates=True returns a _NativeDateValue instead of stringifying
    a native datetime/Timestamp -- opt in only where the resulting row_data later
    reaches extract_columns_from_row(), so a real Excel date is extracted via its
    own value instead of being needlessly re-parsed against the text delimiter/
    order picker (see _NativeDateValue's docstring). Default False preserves
    today's behavior for every other caller.
    """
    if pd.isna(value):
        return None
    if isinstance(value, datetime):
        if preserve_native_dates:
            return _NativeDateValue(value.year, value.month, value.day,
                                     value.hour, value.minute, value.second, value.microsecond)
        return value.strftime(date_fmt)
    if isinstance(value, time):
        return value.strftime('%H:%M:%S')
    return value


# ---------- EDITED BLANK-CELL VALUES (Phase 4 of the upload-reliability requirement) ----------
def apply_edited_cell_values(rows: list, edited_cells) -> list:
    """
    Applies values the user typed into blank cells in the Row Selection grid
    (static/upload.js's editedCellValues, Phase 3) before the row is validated/imported.
    row_number matches the same scheme as filter_excluded_rows()/validation_errors[].row_number
    (header=1, first data row=2), so this MUST run before filter_excluded_rows() -- that
    function returns a plain filtered list with no memory of original position, and anything
    enumerating rows after it renumbers from scratch, which would misalign edited_cells against
    the wrong rows once any row is excluded.

    Only overwrites a cell that is CURRENTLY blank -- a stray/stale edit can never clobber real
    data, even though the frontend already restricts editing to blank, non-key-column cells.
    A cell the user never edited is left exactly as read from the sheet (still blank), per the
    locked "no default value is auto-inserted" requirement.
    """
    if not edited_cells:
        return rows
    edits_by_row = {}
    for edit in edited_cells:
        edits_by_row.setdefault(edit.row_number, {})[edit.excel_column] = edit.value
    for row_idx, row in enumerate(rows, start=2):
        row_edits = edits_by_row.get(row_idx)
        if not row_edits:
            continue
        for excel_column, value in row_edits.items():
            if excel_column not in row:
                continue  # not a real mapped Excel column -- ignore
            current = row[excel_column]
            is_blank = pd.isna(current) if not isinstance(current, str) else current.strip() == ''
            if is_blank:
                row[excel_column] = value
    return rows


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
                         incoming_rows: list, col_mapping: dict, mapping_dict: dict,
                         rate_source: Optional[str] = None, rate_precision: int = 0) -> dict:
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
                row_data[t_col_name] = json_safe_cell_value(raw_row[excel_col], preserve_native_dates=True)

        # Rate derivation (Phase 7 of the Rate-derivation requirement) -- must run
        # here, at first assembly, so the diff below and the eventual insert/update
        # both see the same finished row_data (see apply_rate_derivation()'s docstring).
        apply_rate_derivation(row_data, template_cols, rate_source, rate_precision)
        # Restores the template's real column order -- apply_rate_derivation() may have
        # just appended 'Rate' at the end of the dict (see reorder_row_data_by_template()'s
        # docstring).
        row_data = reorder_row_data_by_template(row_data, template_cols)

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

class EditedCellValue(BaseModel):
    # A value the user typed into a blank cell in the Row Selection grid (Phase 3/4 of the
    # upload-reliability requirement). row_number matches validation_errors[].row_number
    # (header=1, first data row=2); excel_column is the raw Excel header text.
    row_number: int
    excel_column: str
    value: str

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
    # Values typed into blank cells (Phase 4 of the upload-reliability requirement).
    # None/absent means "no edits" -- identical to today's behavior (genuine blanks stay blank).
    edited_cells: Optional[List[EditedCellValue]] = None
    # Rows explicitly ticked "Import anyway" in the Exceptions -- Rejected Rows list
    # (Phase 4 of the blank-field-rejection requirement). Same row-number scheme as
    # excluded_rows/rejected_rows[].row_number (header=1, first data row=2).
    # None/absent means "force-include nothing" -- identical to today's behavior
    # (every rejected row stays excluded).
    force_include_rows: Optional[List[int]] = None

class AutoMapRequest(BaseModel):
    file_id: int
    template_id: int
    sheet_name: str = "Sheet1"

# ---------- SAVED COLUMN MAPPING MODELS (Phase A1 of 3: save column mapping feature) ----------
class SavedMappingEntry(BaseModel):
    template_column_id: int
    excel_column: str

class SaveMappingRequest(BaseModel):
    client_id: int
    template_id: int
    mappings: List[SavedMappingEntry]

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

# ---------- RATE DERIVATION AT IMPORT TIME (Phase 7 of the Rate-derivation requirement) ----------
def get_rate_precision() -> int:
    """
    Reads the 'rate_precision' app_settings key (Phase 1/2 of the Rate-derivation
    requirement), independent of decimal_precision. Falls back to 0 if missing
    or unparseable, same defensive convention as everywhere else that reads a
    setting expected to be a small int (0/2/4).
    """
    rows = execute_query("SELECT value FROM app_settings WHERE key = 'rate_precision'")
    if not rows:
        return 0
    try:
        return int(rows[0]['value'])
    except (TypeError, ValueError):
        return 0


def find_named_column(template_cols: list, name: str) -> Optional[dict]:
    """
    Case-insensitive/trimmed lookup of a template column by column_name --
    generalizes the "must be named exactly 'Rate'/'Quantity'/'Amount'" naming
    convention already established for Rate-derivation eligible templates.
    """
    target = name.strip().lower()
    for col in template_cols:
        if col['column_name'].strip().lower() == target:
            return col
    return None


def resolve_upload_rate_source(template_cols: list, col_mapping: Optional[dict]) -> str:
    """
    Resolves a template's 'auto' rate_source into a concrete 'imported'/'derived'
    for one specific upload (Phase B of the Rate Source 'auto' requirement).
    Unlike apply_rate_derivation()'s per-row work, this is a per-upload decision --
    every row in the same upload shares the same column mapping, so it's resolved
    once and reused for the whole file, never re-derived per row.

    'imported' if this upload's own column mapping (`col_mapping`: template column
    name -> Excel column name, as actually chosen on THIS upload -- not the
    template's static column list) maps a Rate column; 'derived' otherwise, i.e.
    the uploaded file simply has no Rate column mapped, so it must be computed.
    """
    mapped_cols = [c for c in template_cols if col_mapping and col_mapping.get(c['column_name'])]
    return 'imported' if find_named_column(mapped_cols, 'Rate') else 'derived'


def apply_rate_derivation(row_data: dict, template_cols: list, rate_source: Optional[str], rate_precision: int) -> None:
    """
    Populates/rounds row_data['Rate'] in place for Purchase Order/Store Receiving/
    Vendor Bill templates (rate_source is None for every other template -- a
    complete no-op here). Must be called at the point row_data is first assembled
    from the raw Excel row, before compare_transactions() diffs it against
    existing stored rows -- doing it after would make a re-upload look
    artificially "changed" forever, since a previously-imported row already has
    a Rate value baked into its stored row_data.

    rate_source here is always already resolved to 'imported'/'derived' -- a
    template's 'auto' value is resolved once per upload by
    resolve_upload_rate_source() before this function is ever called (see call
    sites), so this function never sees the literal 'auto'.

    'derived': Rate = Amount / Quantity, rounded to rate_precision. Quantity
    missing/blank/0 leaves Rate as None -- a normal "Rate unavailable" outcome,
    never an error/rejection (Rate is not one of the 3 critical fields
    document_id/amount/date checked by find_critical_blank_reasons()).
    'imported': Rate is already whatever came from the mapped Excel "Rate"
    column (or absent, if the file has no such column) -- just round it.
    """
    if rate_source is None:
        return
    rate_col = find_named_column(template_cols, 'Rate')
    if not rate_col:
        return  # this template has no Rate column to populate

    if rate_source == 'derived':
        qty_col = find_named_column(template_cols, 'Quantity')
        amount_col = find_named_column(template_cols, 'Amount')
        if not qty_col or not amount_col:
            return  # nothing to derive from
        qty_raw = row_data.get(qty_col['column_name'])
        amount_raw = row_data.get(amount_col['column_name'])
        try:
            qty = float(qty_raw) if qty_raw not in (None, '') else 0.0
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0:
            row_data['Rate'] = None
            return
        try:
            amount = float(amount_raw) if amount_raw not in (None, '') else 0.0
        except (TypeError, ValueError):
            row_data['Rate'] = None
            return
        row_data['Rate'] = round(amount / qty, rate_precision)
    else:  # 'imported'
        rate_raw = row_data.get('Rate')
        if rate_raw in (None, ''):
            return
        try:
            row_data['Rate'] = round(float(rate_raw), rate_precision)
        except (TypeError, ValueError):
            pass  # leave whatever non-numeric value was there -- not this function's concern


def reorder_row_data_by_template(row_data: dict, template_cols: list) -> dict:
    """
    Rebuilds row_data with keys in the template's configured sort_order, keeping
    only keys that are actually present. Needed because a derived field (e.g.
    Rate, which has no Excel mapping) gets appended at the end of the dict by
    apply_rate_derivation() -- called right after it, at every point row_data is
    first assembled, so the stored JSON's key order always matches the
    template's real column order regardless of when a given key was added.
    """
    return {t_col['column_name']: row_data[t_col['column_name']]
            for t_col in template_cols if t_col['column_name'] in row_data}


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

    amount/date are tracked with independent found-flags -- confirmed live (2026-08-09) that a
    shared `break` here previously exited the loop the moment EITHER was found, so any template
    listing its Date column before its Amount column (i.e. nearly all of them) permanently lost
    the amount, since the Date column matches first and the loop never reaches the later Numeric
    column at all. Do not reintroduce a single shared break for both branches.

    A literal 0 in a Numeric column is also treated as "no value here" and skipped -- confirmed
    live (2026-08-09) the Transaction Report template's Debit Amount/Credit Amount columns always
    store BOTH as a real number, never blank (a receipt-type voucher has Debit Amount: 0.0), so
    without this a Debit-side 0 would permanently win over the real Credit Amount for ~62% of
    real cashbook rows. Matches the existing debit/credit-side convention already established by
    is_cashbook_leg_row() (routers/verification.py), which treats a 0 the same way.

    A column literally named "Amount" (case-insensitive/trimmed, via find_named_column()) is
    preferred over the generic "first Numeric column" rule below when one exists -- confirmed
    with the user, 2026-08-09. Purchase Order/Store Receiving/Vendor Bill templates all have
    Quantity as a Numeric column before Amount in sort_order, and GET /api/data/preview's
    amount_min/amount_max filter reads extract_amount directly, so "first numeric" would
    silently resolve to Quantity there. A blank/zero named-Amount value falls through to the
    generic ordered search below, same as any other unresolved Numeric column.
    """
    amount = None
    date_str = None
    amount_found = False
    date_found = False

    amount_col = find_named_column(template_cols, 'Amount')
    if amount_col and amount_col['data_type'] == 'Numeric':
        named_value = row_data.get(amount_col['column_name'])
        if named_value is not None and named_value != '':
            try:
                parsed_named_amount = float(named_value)
                if parsed_named_amount != 0:
                    amount = parsed_named_amount
                    amount_found = True
            except:
                pass

    for col in template_cols:
        if amount_found and date_found:
            break
        col_name = col['column_name']
        value = row_data.get(col_name)
        if value is None or value == '':
            continue
        if col['data_type'] == 'Numeric' and not amount_found:
            try:
                parsed_amount = float(value)
                if parsed_amount != 0:
                    amount = parsed_amount
                    amount_found = True  # take first non-zero numeric
            except:
                pass
        elif col['data_type'] == 'Date' and not date_found:
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
                date_found = True  # take first date
            except:
                pass
    return amount, date_str


# ---------- ROW-LEVEL CRITICAL FIELD REJECTION (Phase 2 of the blank-field-rejection requirement) ----------
def get_critical_field_columns(template_cols: list) -> dict:
    """
    Identifies which template columns fill the 3 "critical" roles used for
    row-level blank rejection -- Document ID / Amount / Date -- using the SAME
    positional conventions already established elsewhere in this file, since
    this schema has no explicit "Document ID"/"Amount"/"Date" column names:
      - document_id = the first template column by sort_order (matches
        get_document_group_id()'s "first column = unique document key" rule)
      - amount      = the first Numeric-typed column (matches
        extract_columns_from_row()'s "take first numeric" rule)
      - date        = the first Date-typed column (matches
        extract_columns_from_row()'s "take first date" rule)
    A role is None if this template has no column filling it (e.g. no Date column).
    """
    return {
        'document_id': template_cols[0] if template_cols else None,
        'amount': next((c for c in template_cols if c['data_type'] == 'Numeric'), None),
        'date': next((c for c in template_cols if c['data_type'] == 'Date'), None),
    }


CRITICAL_FIELD_LABELS = {
    'document_id': 'Document ID',
    'amount': 'Amount',
    'date': 'Date',
}


def find_critical_blank_reasons(row: dict, critical_cols: dict, mapping_dict: dict) -> list:
    """
    Returns a reason dict for each critical role (Document ID/Amount/Date) that is
    both mapped in this upload session and blank in this raw Excel row. A role with
    no column in this template, or whose column isn't mapped to an Excel header
    this session, is skipped -- nothing to check.

    "Blank" uses the same test as apply_edited_cell_values() (pd.isna() for
    non-strings, stripped-empty for strings) for consistency within this file.
    """
    reasons = []
    for role, t_col in critical_cols.items():
        if not t_col:
            continue
        mapping = mapping_dict.get(t_col['id'])
        if not mapping or not mapping.excel_column_name:
            continue
        excel_col = mapping.excel_column_name
        if excel_col not in row:
            continue
        value = row[excel_col]
        is_blank = pd.isna(value) if not isinstance(value, str) else value.strip() == ''
        if is_blank:
            reasons.append({
                "role": role,
                "column": t_col['column_name'],
                "excel_column": excel_col,
                "message": f"{CRITICAL_FIELD_LABELS[role]} ('{t_col['column_name']}') is blank"
            })
    return reasons


def compute_data_quality_flag(row_data: dict, critical_cols: dict) -> Optional[str]:
    """
    Returns 'incomplete' if any critical field (Document ID/Amount/Date) is blank in
    the final row_data (template column names -> values), else None.

    A blank critical field can only reach this point (insert/update time) if the row
    was rejected by find_critical_blank_reasons() and then explicitly force-included
    via the Exceptions -- Rejected Rows override (Phase 4) -- normal validation never
    lets a blank-critical-field row survive to insertion otherwise. Computing the flag
    here, from the final row_data, avoids having to thread a "force-included" marker
    through compare_transactions()'s own row_data reconstruction.
    """
    for t_col in critical_cols.values():
        if not t_col:
            continue
        value = row_data.get(t_col['column_name'])
        if value is None or value == '':
            return 'incomplete'
    return None


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

# ---------- SAVE / LOAD COLUMN MAPPING (Phase A1 of 3: save column mapping feature) ----------
@router.post("/api/upload/save-mapping")
def save_column_mapping(request: SaveMappingRequest):
    """
    Saves (or overwrites) the confirmed Excel-column -> template-column mapping for a
    client+template, so it can be auto-applied on a later upload (Phase A2). Upsert via
    delete-then-reinsert, same pattern used elsewhere in this app for replacing an
    ordered/child row set (e.g. update_template() in routers/templates.py).
    """
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (request.client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")
    template = execute_query("SELECT id FROM templates WHERE id = ? AND is_active = 1", (request.template_id,))
    if not template:
        raise HTTPException(404, "Template not found")

    execute_write(
        "DELETE FROM saved_column_mappings WHERE client_id = ? AND template_id = ?",
        (request.client_id, request.template_id)
    )

    saved_count = 0
    for entry in request.mappings:
        # Nothing meaningful to save for a column that wasn't matched to an Excel header.
        if not entry.excel_column or not entry.excel_column.strip():
            continue
        execute_write(
            """
            INSERT INTO saved_column_mappings (client_id, template_id, template_column_id, excel_column)
            VALUES (?, ?, ?, ?)
            """,
            (request.client_id, request.template_id, entry.template_column_id, entry.excel_column)
        )
        saved_count += 1

    log_audit("Analyst", "Save", "Upload",
              f"Saved column mapping for client {request.client_id}, template {request.template_id} ({saved_count} columns)")
    return {"message": "Column mapping saved successfully", "saved_count": saved_count}


@router.get("/api/upload/saved-mapping")
def get_saved_column_mapping(client_id: int, template_id: int):
    """
    Returns the previously saved mapping for a client+template, if any. Returns an empty/false
    result rather than 404 when nothing is saved yet -- Phase A2's auto-apply-on-load only needs
    to check has_saved_mapping, no special-case error handling required on the frontend.
    """
    rows = execute_query(
        "SELECT template_column_id, excel_column FROM saved_column_mappings WHERE client_id = ? AND template_id = ?",
        (client_id, template_id)
    )
    return {
        "has_saved_mapping": len(rows) > 0,
        "mappings": [{"template_column_id": r["template_column_id"], "excel_column": r["excel_column"]} for r in rows]
    }


@router.delete("/api/upload/saved-mapping")
def delete_saved_column_mapping(client_id: int, template_id: int):
    """
    Removes the saved mapping for a client+template (Phase A3, final phase). No client/template
    existence checks -- same leniency as the GET endpoint above, since "nothing was saved" is a
    normal outcome, not an error. After this, the next auto-apply check (applySavedMapping() in
    upload.js) sees has_saved_mapping: false and falls back to ordinary auto-mapping, identical to
    a client+template that never had a saved mapping.
    """
    existing = execute_query(
        "SELECT COUNT(*) AS cnt FROM saved_column_mappings WHERE client_id = ? AND template_id = ?",
        (client_id, template_id)
    )
    deleted_count = existing[0]["cnt"] if existing else 0

    execute_write(
        "DELETE FROM saved_column_mappings WHERE client_id = ? AND template_id = ?",
        (client_id, template_id)
    )

    log_audit("Analyst", "Delete", "Upload",
              f"Cleared saved column mapping for client {client_id}, template {template_id} ({deleted_count} columns)")
    return {"message": "Saved column mapping cleared", "deleted_count": deleted_count}


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
    template = execute_query("SELECT id, template_name, rate_source FROM templates WHERE id = ? AND is_active = 1", (request.template_id,))
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

    # Pre-import Rate mode visibility (Phase D of the Rate-derivation requirement) --
    # resolves the same 'auto' -> 'imported'/'derived' decision validate_and_import()
    # makes at commit time, via the same resolve_upload_rate_source() helper, so the
    # frontend can show it before the user ever clicks Upload. template_rate_source is
    # already NULL for every non-eligible template (enforced by resolve_rate_source()
    # in routers/templates.py), so rate_mode stays None there with no extra check needed.
    template_rate_source = template[0].get('rate_source')
    rate_mode = None
    if template_rate_source:
        effective_source = template_rate_source
        if effective_source == 'auto':
            effective_source = resolve_upload_rate_source(template_cols, col_mapping)
        label = {
            'auto': f"Auto → {effective_source.capitalize()}",
            'imported': 'Forced Imported',
            'derived': 'Forced Derived',
        }[template_rate_source]
        rate_mode = {
            "template_setting": template_rate_source,
            "effective_source": effective_source,
            "label": label,
        }

    # Read Excel data
    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=request.sheet_name)
        mapped_excel_cols = [m.excel_column_name for m in request.mappings if m.excel_column_name]
        df = df[[col for col in mapped_excel_cols if col in df.columns]]
    except Exception as e:
        raise HTTPException(400, f"Error reading Excel: {str(e)}")

    rows = df.to_dict('records')
    # Apply any typed blank-cell values before exclusion filtering (see
    # apply_edited_cell_values()'s docstring for why the ordering matters).
    rows = apply_edited_cell_values(rows, request.edited_cells)
    # Drop user-excluded rows before anything else sees them (validation, counts).
    rows = filter_excluded_rows(rows, request.excluded_rows)

    # Which columns fill the 3 "critical" roles (Document ID/Amount/Date) for this
    # template -- computed once, reused for every row (Phase 2 of the
    # blank-field-rejection requirement).
    critical_cols = get_critical_field_columns(template_cols)

        # --- Validation (same logic as import but without inserting) ---
    validation_errors = []
    rejected_rows = []
    for row_idx, row in enumerate(rows, start=2):  # row 1 = headers, row 2 = first data
        row_errors = []
        row_reasons = []
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

                # A blank value is only a problem for the 3 critical fields
                # (handled separately below via find_critical_blank_reasons) --
                # a blank in any other column imports fine, nothing to check here.
                is_blank = pd.isna(value) if not isinstance(value, str) else value.strip() == ''
                if is_blank:
                    continue

                # Data type checks (non-blank values only)
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

            # Row-level rejection: blank Document ID/Amount/Date (Phase 2 of the
            # blank-field-rejection requirement) -- distinct from the generic
            # data-type errors above, reported separately, does not add to row_errors.
            row_reasons = find_critical_blank_reasons(row, critical_cols, mapping_dict)
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
        if row_reasons:
            rejected_rows.append({
                "row_number": row_idx,
                "row_data": row_data_full,
                "reasons": row_reasons
            })

    new_count, existing_count = count_new_and_existing_rows(
        request.client_id,
        request.template_id,
        rows,
        template_cols,
        mapping_dict,
    )

    # A row can appear in both lists (e.g. a real Numeric format error in one
    # column plus a blank Date in another) -- union by row_number so it's only
    # subtracted once from valid_rows.
    bad_row_numbers = {r["row_number"] for r in validation_errors} | {r["row_number"] for r in rejected_rows}

    return {
        "status": "validation_complete",
        "has_errors": len(validation_errors) > 0,
        "error_count": len(validation_errors),
        "has_rejections": len(rejected_rows) > 0,
        "rejected_count": len(rejected_rows),
        "rejected_rows": rejected_rows,
        "total_rows": len(rows),
        "errors": validation_errors,
        "valid_rows": len(rows) - len(bad_row_numbers),
        "new_rows_count": new_count,
        "existing_rows_count": existing_count,
        "rate_mode": rate_mode,
    }

DATA_AUDIT_LOG_INSERT_SQL = (
    "INSERT INTO data_audit_log (client_data_id, user, action, old_row_data, new_row_data) "
    "VALUES (?, ?, ?, ?, ?)"
)


def _audit_row_params(client_data_id, action, old_data=None, new_data=None, user="Analyst"):
    """
    Builds one params tuple for DATA_AUDIT_LOG_INSERT_SQL, matching
    log_data_audit()'s own JSON-encoding convention exactly (used for the
    batched import write path instead of calling log_data_audit() per row).
    """
    return (
        client_data_id,
        user,
        action,
        json.dumps(old_data, default=str) if old_data else None,
        json.dumps(new_data, default=str) if new_data else None,
    )


# ---------- VALIDATE & IMPORT ----------
@router.post("/api/upload/import")
def validate_and_import(request: ImportRequest):
    # Validate template and file
    template = execute_query("SELECT id, template_name, rate_source FROM templates WHERE id = ? AND is_active = 1", (request.template_id,))
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
    # Apply any typed blank-cell values before exclusion filtering (see
    # apply_edited_cell_values()'s docstring for why the ordering matters).
    rows = apply_edited_cell_values(rows, request.edited_cells)
    # Drop user-excluded rows before validation, import, and document_group_id
    # grouping (compare_transactions) ever see them.
    rows = filter_excluded_rows(rows, request.excluded_rows)

    # Which columns fill the 3 "critical" roles (Document ID/Amount/Date) for this
    # template -- computed once, reused for every row (Phase 2 of the
    # blank-field-rejection requirement).
    critical_cols = get_critical_field_columns(template_cols)

    # Rate derivation config (Phase 7 of the Rate-derivation requirement) --
    # rate_source is None for every template except Purchase Order/Store
    # Receiving/Vendor Bill (see resolve_rate_source() in routers/templates.py).
    rate_source = template[0].get('rate_source')
    rate_precision = get_rate_precision()
    # Phase B of the Rate Source 'auto' requirement: 'auto' is only a template-level
    # trigger, resolved once per upload (not per row -- every row in this upload
    # shares the same col_mapping) into a concrete 'imported'/'derived' before
    # anything downstream (compare_transactions, first-time-upload insert, or the
    # rate_source column stored per row) ever sees it.
    if rate_source == 'auto':
        rate_source = resolve_upload_rate_source(template_cols, col_mapping)

    # --- Validation ---
    validation_errors = []
    rejected_rows = []
    for row_idx, row in enumerate(rows, start=2):
        row_errors = []
        row_reasons = []
        try:
            for t_col in template_cols:
                mapping = mapping_dict.get(t_col['id'])
                if not mapping or not mapping.excel_column_name:
                    continue
                excel_col = mapping.excel_column_name
                if excel_col not in row:
                    continue
                value = row[excel_col]

                # A blank value is only a problem for the 3 critical fields
                # (handled separately below via find_critical_blank_reasons) --
                # a blank in any other column imports fine, nothing to check here.
                is_blank = pd.isna(value) if not isinstance(value, str) else value.strip() == ''
                if is_blank:
                    continue

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

            # Row-level rejection: blank Document ID/Amount/Date (Phase 2 of the
            # blank-field-rejection requirement) -- distinct from the generic
            # data-type errors above, reported separately, does not add to row_errors.
            row_reasons = find_critical_blank_reasons(row, critical_cols, mapping_dict)
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
        if row_reasons:
            rejected_rows.append({"row_number": row_idx, "reasons": row_reasons})

    if validation_errors:
        # Generic validation errors (bad Numeric/Date format on a non-blank value)
        # are unaffected by row-level rejection -- they still block the entire
        # import, same as before. rejected_rows is included here too, purely for
        # visibility, since a row can independently appear in both lists.
        return {
            "status": "validation_failed",
            "errors": validation_errors,
            "total_rows": len(rows),
            "error_count": len(validation_errors),
            "rejected_rows": rejected_rows,
            "rejected_count": len(rejected_rows),
        }

    # Total attempted rows, captured before rejected rows are excluded below --
    # the success response's total_rows should reflect what was attempted, not
    # just what actually got inserted.
    total_rows_attempted = len(rows)

    # Exclude rows rejected for a blank critical field from insertion -- EXCEPT rows
    # the user explicitly ticked "Import anyway" for in the Exceptions -- Rejected Rows
    # list (Phase 4 of the blank-field-rejection requirement). A force-included row
    # stays in `rows` unchanged, still carrying its genuinely blank critical-field
    # cell -- no default/placeholder value is ever inserted in its place.
    force_include_set = set(request.force_include_rows or [])
    force_included_count = sum(1 for r in rejected_rows if r["row_number"] in force_include_set)
    rows_to_exclude = [r["row_number"] for r in rejected_rows if r["row_number"] not in force_include_set]
    if rows_to_exclude:
        rows = filter_excluded_rows(rows, rows_to_exclude)

    # ---- CHANGE DETECTION (Phase 2): Compare incoming rows with existing data ----
    # Change detection always runs; it groups rows by Document ID and identifies
    # unchanged, changed, new, and missing documents.
    change_detection = compare_transactions(
        client_id=request.client_id,
        template_id=request.template_id,
        template_cols=template_cols,
        incoming_rows=rows,
        col_mapping=col_mapping,
        mapping_dict=mapping_dict,
        rate_source=rate_source,
        rate_precision=rate_precision
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

    # Row-write progress tracking (Import progress requirement, phase 1 of 3) --
    # total reflects rows about to actually be written, not the earlier validation pass.
    if change_detection is not None and change_detection.get("unchanged") is not None:
        progress_total = len(change_detection["changed"]) + len(change_detection["new"]) + len(change_detection["missing"])
    else:
        progress_total = len(rows)
    IMPORT_PROGRESS[request.file_id] = {"current": 0, "total": progress_total, "status": "processing"}

    # Reuse a single connection for the whole write phase instead of opening/closing
    # a new sqlite3 connection (and re-setting PRAGMAs) on every row write -- this was
    # 94.6% of total import time for a ~4,900-row import. Each write category below is
    # batched via executemany() in chunks (database.py's execute_batch()/
    # execute_batch_insert_with_ids()), committing per chunk rather than per row or in
    # one giant transaction.
    conn = get_raw_conn()
    try:
        if change_detection is not None and change_detection.get("unchanged") is not None:
            # --- Re-upload scenario: handle each category ---
            # 1. Unchanged: skip — retain existing validation status (no action needed)
            #    (These rows already exist in client_data and their validation_status is preserved)

            # 2. Changed: update row_data, reset validation_status to 'Pending', log old values
            changed_client_data_params = []
            changed_verification_params = []
            changed_audit_params = []
            for item in change_detection["changed"]:
                cid = item["client_data_id"]
                new_version = (item.get("old_version") or 1) + 1
                new_row_data = item["new_row_data"]

                # Compute document_group_id from the new row_data
                doc_group_id = get_document_group_id(template_cols, new_row_data, request.template_id)

                # data_quality_flag (Phase 4 of the blank-field-rejection requirement) is
                # recomputed from the new row_data -- a force-included row with a still-blank
                # critical field is flagged 'incomplete', otherwise cleared back to NULL.
                data_quality_flag = compute_data_quality_flag(new_row_data, critical_cols)
                # extract_amount/extract_date (confirmed fix, 2026-08-09) are recomputed from the
                # new row_data too -- this UPDATE previously left both columns untouched on a
                # changed-row re-upload, so a row's amount/date would never refresh even after the
                # extraction bug above was fixed.
                amount, date_str = extract_columns_from_row(new_row_data, template_cols, effective_delimiter, effective_order)
                changed_client_data_params.append(
                    (json.dumps(new_row_data, default=str), new_version, doc_group_id, data_quality_flag, rate_source, amount, date_str, cid)
                )
                changed_audit_params.append(_audit_row_params(cid, "UPDATE", item["old_row_data"], new_row_data))
                if item.get("verification_detail_id"):
                    changed_verification_params.append((item["verification_detail_id"],))

                updated_count += 1
                IMPORT_PROGRESS[request.file_id]["current"] += 1

            if changed_client_data_params:
                execute_batch(
                    conn,
                    """
                    UPDATE client_data
                    SET row_data = ?, version = ?, document_group_id = ?, change_detection_status = 'updated', data_quality_flag = ?, rate_source = ?, extract_amount = ?, extract_date = ?
                    WHERE id = ?
                    """,
                    changed_client_data_params
                )
            if changed_verification_params:
                execute_batch(
                    conn,
                    "UPDATE verification_details SET validation_status = 'Pending' WHERE id = ?",
                    changed_verification_params
                )
            if changed_audit_params:
                execute_batch(conn, DATA_AUDIT_LOG_INSERT_SQL, changed_audit_params)

            # 3. New: insert fresh rows
            new_client_data_params = []
            new_row_data_list = []
            for new_row_data in change_detection["new"]:
                amount, date_str = extract_columns_from_row(new_row_data, template_cols, effective_delimiter, effective_order)
                doc_group_id = get_document_group_id(template_cols, new_row_data, request.template_id)
                # data_quality_flag (Phase 4 of the blank-field-rejection requirement) --
                # 'incomplete' for a force-included row still carrying a blank critical field.
                data_quality_flag = compute_data_quality_flag(new_row_data, critical_cols)
                new_client_data_params.append(
                    (request.client_id, request.template_id, request.file_id,
                     json.dumps(new_row_data, default=str), amount, date_str, "Analyst", 1, doc_group_id, data_quality_flag, rate_source)
                )
                new_row_data_list.append(new_row_data)
                inserted_count += 1
                IMPORT_PROGRESS[request.file_id]["current"] += 1

            if new_client_data_params:
                new_ids = execute_batch_insert_with_ids(
                    conn,
                    """
                    INSERT INTO client_data (client_id, template_id, upload_id, row_data, extract_amount, extract_date, created_by, version, change_detection_status, document_group_id, data_quality_flag, rate_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'added', ?, ?, ?)
                    """,
                    new_client_data_params,
                    table="client_data"
                )
                new_audit_params = [
                    _audit_row_params(row_id, "INSERT", None, new_row_data)
                    for row_id, new_row_data in zip(new_ids, new_row_data_list)
                ]
                execute_batch(conn, DATA_AUDIT_LOG_INSERT_SQL, new_audit_params)

            # 4. Missing: flag existing rows as 'deleted' for auditor review (soft flag)
            missing_client_data_params = []
            missing_audit_params = []
            for item in change_detection["missing"]:
                cid = item["client_data_id"]
                missing_client_data_params.append((cid,))
                missing_audit_params.append(_audit_row_params(cid, "DELETE", item["old_row_data"], None))
                deleted_count += 1
                IMPORT_PROGRESS[request.file_id]["current"] += 1

            if missing_client_data_params:
                execute_batch(
                    conn,
                    "UPDATE client_data SET change_detection_status = 'deleted' WHERE id = ?",
                    missing_client_data_params
                )
            if missing_audit_params:
                execute_batch(conn, DATA_AUDIT_LOG_INSERT_SQL, missing_audit_params)

        else:
            # --- First-time upload (no existing data with valid composite key): insert all rows as new ---
            first_upload_client_data_params = []
            first_upload_row_data_list = []
            for row in rows:
                # Build JSON row_data
                row_data = {}
                for t_col_name, excel_col in col_mapping.items():
                    if excel_col in row:
                        row_data[t_col_name] = json_safe_cell_value(row[excel_col], preserve_native_dates=True)

                # Rate derivation (Phase 7 of the Rate-derivation requirement) -- see
                # apply_rate_derivation()'s docstring for why this must happen right
                # here, at first assembly.
                apply_rate_derivation(row_data, template_cols, rate_source, rate_precision)
                # Restores the template's real column order -- apply_rate_derivation() may have
                # just appended 'Rate' at the end of the dict (see reorder_row_data_by_template()'s
                # docstring).
                row_data = reorder_row_data_by_template(row_data, template_cols)

                # Compute document_group_id from the first column value
                doc_group_id = get_document_group_id(template_cols, row_data, request.template_id)

                # Extract amount and date for indexing
                amount, date_str = extract_columns_from_row(row_data, template_cols, effective_delimiter, effective_order)

                # data_quality_flag (Phase 4 of the blank-field-rejection requirement) --
                # 'incomplete' for a force-included row still carrying a blank critical field.
                data_quality_flag = compute_data_quality_flag(row_data, critical_cols)

                first_upload_client_data_params.append(
                    (request.client_id, request.template_id, request.file_id,
                     json.dumps(row_data, default=str), amount, date_str, "Analyst", 1, doc_group_id, data_quality_flag, rate_source)
                )
                first_upload_row_data_list.append(row_data)
                inserted_count += 1
                IMPORT_PROGRESS[request.file_id]["current"] += 1

            if first_upload_client_data_params:
                first_upload_ids = execute_batch_insert_with_ids(
                    conn,
                    """
                    INSERT INTO client_data (client_id, template_id, upload_id, row_data, extract_amount, extract_date, created_by, version, change_detection_status, document_group_id, data_quality_flag, rate_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    first_upload_client_data_params,
                    table="client_data"
                )
                first_upload_audit_params = [
                    _audit_row_params(row_id, "INSERT", None, row_data)
                    for row_id, row_data in zip(first_upload_ids, first_upload_row_data_list)
                ]
                execute_batch(conn, DATA_AUDIT_LOG_INSERT_SQL, first_upload_audit_params)
    finally:
        conn.close()

    log_audit("Analyst", "Import", "Upload",
              f"Imported {inserted_count} new, {updated_count} updated, {deleted_count} flagged missing for client {request.client_id}, template {template[0]['template_name']}, file ID {request.file_id}")

    IMPORT_PROGRESS[request.file_id]["status"] = "complete"

    return {
        "status": "success",
        "message": f"Successfully imported: {inserted_count} new, {updated_count} updated, {deleted_count} flagged missing",
        "total_rows": total_rows_attempted,
        "imported_count": inserted_count,
        "updated_count": updated_count,
        "deleted_count": deleted_count,
        "validation_errors": [],
        "rejected_rows": rejected_rows,  # Phase 2 of the blank-field-rejection requirement
        "rejected_count": len(rejected_rows),
        # Phase 4: of the rows above, how many were force-included via the Exceptions
        # override (imported with the critical field genuinely blank, flagged
        # 'incomplete') -- the frontend derives "truly rejected/excluded" as
        # rejected_count - force_included_count.
        "force_included_count": force_included_count,
        "change_detection": change_detection  # Phase 2: detailed breakdown of unchanged/changed/new/missing
    }

@router.get("/api/upload/import-status/{file_id}")
def get_import_status(file_id: int):
    """Polled by the frontend while POST /api/upload/import is in flight for this file_id."""
    return IMPORT_PROGRESS.get(file_id, {"current": 0, "total": 0, "status": "idle"})

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