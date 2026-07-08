from fastapi import APIRouter, File, UploadFile, HTTPException, Body
from fastapi.responses import StreamingResponse
import os, json, openpyxl, pandas as pd
from io import BytesIO
from database import execute_query, execute_write, log_audit, get_raw_conn, log_data_audit
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

# ⭐ MUST HAVE THIS LINE
router = APIRouter()

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

class AutoMapRequest(BaseModel):
    file_id: int
    template_id: int
    sheet_name: str = "Sheet1"

# ---------- HELPER: Extract numeric & date columns from row data ----------
def extract_columns_from_row(row_data: dict, template_cols: list) -> tuple:
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
                # Try to parse various date formats
                from datetime import datetime
                if isinstance(value, datetime):
                    date_str = value.strftime('%Y-%m-%d')
                elif isinstance(value, str):
                    # Try common formats
                    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%m/%d/%Y', '%d-%m-%Y'):
                        try:
                            dt = datetime.strptime(value, fmt)
                            date_str = dt.strftime('%Y-%m-%d')
                            break
                        except:
                            continue
                elif isinstance(value, (int, float)):
                    # Excel serial date?
                    try:
                        from datetime import datetime, timedelta
                        # Excel serial date starts from 1900-01-01 (but with bug for 1900)
                        dt = datetime(1899, 12, 30) + timedelta(days=value)
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

# ---------- VALIDATE & IMPORT ----------
@router.post("/api/upload/import")
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

        existing = execute_query(
            """
            SELECT id, row_data, extract_amount, extract_date
            FROM client_data
            WHERE client_id = ? AND template_id = ? AND is_deleted = 0
            AND json_extract(row_data, '$."{}"') = ?
            """.format(unique_col_name),
            (request.client_id, request.template_id, str(unique_value))
        )

        if existing:
            old_data = json.loads(existing[0]['row_data']) if existing[0]['row_data'] else {}
            duplicates.append({
                "unique_value": unique_value,
                "old_data": old_data,
                "new_data": row_data
            })

    return {
        "has_duplicates": len(duplicates) > 0,
        "duplicates": duplicates,
        "count": len(duplicates)
    }
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

    # Build a dict to map template column ID to its name (for unique column lookup)
    col_id_to_name = {col['id']: col['column_name'] for col in template_cols}
    
    # Build column mapping (template column name -> Excel column name)
    col_mapping = {}
    for t_col in template_cols:
        mapping = mapping_dict.get(t_col['id'])
        if mapping and mapping.excel_column_name:
            col_mapping[t_col['column_name']] = mapping.excel_column_name

    unique_col_name = None
    if request.mode == "update":
        if request.unique_column_id is None:
            raise HTTPException(400, "Unique ID column is required when mode is 'Update Existing'")
        unique_col_name = col_id_to_name.get(request.unique_column_id)
        if not unique_col_name:
            raise HTTPException(400, "Selected Unique ID column is not part of this template")
        # Ensure the unique column is mapped to an Excel column
        if unique_col_name not in col_mapping:
            raise HTTPException(400, f"Unique column '{unique_col_name}' must be mapped to an Excel column")

    # Read Excel data
    try:
        df = pd.read_excel(file_info[0]['file_path'], sheet_name=request.sheet_name)
        # Keep only columns that are mapped
        mapped_excel_cols = [m.excel_column_name for m in request.mappings if m.excel_column_name]
        df = df[[col for col in mapped_excel_cols if col in df.columns]]
    except Exception as e:
        raise HTTPException(400, f"Error reading Excel: {str(e)}")

    rows = df.to_dict('records')
    
    # --- Validation ---
    validation_errors = []
    for row_idx, row in enumerate(rows, start=2):
        row_errors = []
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
                    try:
                        if isinstance(value, str):
                            datetime.strptime(value, "%d/%m/%Y")
                    except:
                        row_errors.append({
                            "column": t_col['column_name'],
                            "excel_column": excel_col,
                            "error_type": "data_type",
                            "message": f"Expected Date (DD/MM/YYYY) but got '{value}'"
                        })
        if row_errors:
            validation_errors.append({"row_number": row_idx, "errors": row_errors})

    if validation_errors:
        return {
            "status": "validation_failed",
            "errors": validation_errors,
            "total_rows": len(rows),
            "error_count": len(validation_errors)
        }

    # col_mapping is already defined above; skip redefinition.

    # --- UPSERT LOGIC ---
    # Update uploaded_files status and set client_id
    execute_write(
        "UPDATE uploaded_files SET status = 'Imported', client_id = ? WHERE id = ?",
        (request.client_id, request.file_id)
    )

    inserted_count = 0
    updated_count = 0
    skipped_count = 0

    for row in rows:
        # Build JSON row_data (same as before)
        row_data = {}
        for t_col_name, excel_col in col_mapping.items():
            if excel_col in row:
                value = row[excel_col]
                if pd.isna(value):
                    value = None
                if isinstance(value, datetime):
                    value = value.strftime('%d/%m/%Y')
                row_data[t_col_name] = value

        # Extract amount and date for indexing
        amount, date_str = extract_columns_from_row(row_data, template_cols)

                # Check if mode is update and we have a unique column
        if request.mode == "update" and unique_col_name:
            unique_value = row_data.get(unique_col_name)
            # If unique value is empty, we cannot find an existing row, so we'll insert new
            if unique_value is None or unique_value == '':
                # Insert as new
                row_id = execute_write(
                    """
                    INSERT INTO client_data (client_id, template_id, upload_id, row_data, extract_amount, extract_date, created_by, version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (request.client_id, request.template_id, request.file_id,
                     json.dumps(row_data), amount, date_str, "Analyst", 1)
                )
                inserted_count += 1
                # Log INSERT
                log_data_audit(row_id, "Analyst", "INSERT", None, row_data)
                continue

            # Query for existing row with same unique value
            existing = execute_query(
                """
                SELECT id, version, row_data FROM client_data
                WHERE client_id = ? AND template_id = ? AND is_deleted = 0
                AND json_extract(row_data, '$."{}"') = ?
                """.format(unique_col_name),
                (request.client_id, request.template_id, str(unique_value))
            )

            if existing:
                # Overwrite existing row – no version increment, keep only one version
                row_id = existing[0]['id']
                old_row_data = json.loads(existing[0]['row_data']) if existing[0]['row_data'] else {}
                execute_write(
                    """
                    UPDATE client_data
                    SET row_data = ?, extract_amount = ?, extract_date = ?
                    WHERE id = ?
                    """,
                    (json.dumps(row_data), amount, date_str, row_id)
                )
                updated_count += 1
                # Log the overwrite (action = 'OVERWRITE' for clarity)
                log_data_audit(row_id, "Analyst", "OVERWRITE", old_row_data, row_data)
            else:
                # Insert new row
                row_id = execute_write(
                    """
                    INSERT INTO client_data (client_id, template_id, upload_id, row_data, extract_amount, extract_date, created_by, version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (request.client_id, request.template_id, request.file_id,
                     json.dumps(row_data), amount, date_str, "Analyst", 1)
                )
                inserted_count += 1
                log_data_audit(row_id, "Analyst", "INSERT", None, row_data)
        else:
            # Mode is "create" (insert always)
            row_id = execute_write(
                """
                INSERT INTO client_data (client_id, template_id, upload_id, row_data, extract_amount, extract_date, created_by, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (request.client_id, request.template_id, request.file_id,
                 json.dumps(row_data), amount, date_str, "Analyst", 1)
            )
            inserted_count += 1
            log_data_audit(row_id, "Analyst", "INSERT", None, row_data)

    log_audit("Analyst", "Import", "Upload",
              f"Imported {inserted_count} new records, updated {updated_count} records for client {request.client_id}, template {template[0]['template_name']}, file ID {request.file_id}")

    return {
        "status": "success",
        "message": f"Successfully imported: {inserted_count} new, {updated_count} updated",
        "total_rows": len(rows),
        "imported_count": inserted_count,
        "updated_count": updated_count,
        "validation_errors": []
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