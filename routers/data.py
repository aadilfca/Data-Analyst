# ==============================================
# 📊 CLIENT DATA BANK – BACKEND (FIXED)
# ==============================================

from fastapi import APIRouter, HTTPException, Query
from database import execute_query, execute_write, log_audit, log_data_audit
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime
import json

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
def format_datetime(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        return dt.strftime('%d/%m/%Y %H:%M')
    except:
        return dt_str

# ---------- ENDPOINTS ----------

@router.get("/api/data/datasets")
def get_datasets(client_id: int = Query(..., description="Client ID")):
    """
    Get all datasets (uploads) for a given client.
    Uses both uploaded_files.client_id and client_data.client_id to handle missing client_id in uploaded_files.
    """
    print(f"🔍 Data Bank request for client_id={client_id}")
    # Verify client exists
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not client:
        print(f"❌ Client {client_id} not found")
        raise HTTPException(404, "Client not found or inactive")

    # Get all uploads that either have client_id matching OR have client_data rows with that client_id
    uploads = execute_query(
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
        LEFT JOIN client_data cd ON cd.upload_id = u.id AND cd.is_deleted = 0
        LEFT JOIN templates t ON t.id = cd.template_id
        LEFT JOIN client_data cd2 ON cd2.upload_id = u.id AND cd2.is_deleted = 0
        WHERE (u.client_id = ? OR EXISTS (
            SELECT 1 FROM client_data cd3 WHERE cd3.upload_id = u.id AND cd3.client_id = ? AND cd3.is_deleted = 0
        ))
        GROUP BY u.id
        ORDER BY u.upload_date DESC
        """,
        (client_id, client_id)
    )
    print(f"📊 Found {len(uploads)} uploads for client {client_id}")

    result = []
    for row in uploads:
        # If template_name is 'Unknown' but we have a template_id, try to fetch name
        template_name = row["template_name"]
        if template_name == "Unknown" and row["template_id"]:
            tpl = execute_query("SELECT template_name FROM templates WHERE id = ?", (row["template_id"],))
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
    pending = execute_query(
        """
        SELECT id, file_name, upload_date, status, record_count
        FROM uploaded_files
        WHERE client_id = ? AND status = 'Pending'
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
    
    print(f"📤 Returning {len(result)} datasets")
    return result

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
    sunday_only: bool = Query(False, description="If true, only show rows with extract_date on Sunday")
):
    """
    Get paginated rows for a specific client, template, and date range.
    Aggregates data from all uploads for that template.
    Supports search, amount filters, and Sunday-only filter.
    Returns rows, total count, Sunday count, and pagination info.
    """
    # Validate client and template exist
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
    if not client:
        raise HTTPException(404, "Client not found or inactive")
    
    template = execute_query("SELECT id, template_name FROM templates WHERE id = ? AND is_active = 1", (template_id,))
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
    
    # Build WHERE clause dynamically
    where_clauses = [
        "cd.client_id = ?",
        "cd.template_id = ?",
        "cd.is_deleted = 0",
        "cd.extract_date BETWEEN ? AND ?"
    ]
    params = [client_id, template_id, date_start, date_end]
    
    # Sunday-only filter
    if sunday_only:
        where_clauses.append("strftime('%w', cd.extract_date) = '0'")
    
    # Search in row_data (case-insensitive) – use json_each to search all values
    if search:
        where_clauses.append(
            "EXISTS (SELECT 1 FROM json_each(cd.row_data) WHERE LOWER(value) LIKE LOWER(?))"
        )
        params.append(f"%{search}%")
    
    # Amount range
    if amount_min is not None:
        where_clauses.append("cd.extract_amount >= ?")
        params.append(amount_min)
    if amount_max is not None:
        where_clauses.append("cd.extract_amount <= ?")
        params.append(amount_max)
    
    where_sql = " AND ".join(where_clauses)
    
    # Get total count
    count_sql = f"SELECT COUNT(*) as total FROM client_data cd WHERE {where_sql}"
    total_result = execute_query(count_sql, params)
    total_count = total_result[0]['total'] if total_result else 0
    
    if total_count == 0:
        return {
            "template_id": template_id,
            "template_name": template[0]['template_name'],
            "total_rows": 0,
            "sunday_count": 0,
            "page": page,
            "limit": limit,
            "rows": []
        }
    
    # Get Sunday count (for footer) – need to compute separately if we have filters applied
    # We'll compute it by counting rows where extract_date is Sunday (without sunday_only filter? 
    # Actually we want to show Sunday count in the footer even when not filtering by Sunday.
    # So we need to compute it separately, without the sunday_only filter.
    sunday_params = params.copy()
    # Remove sunday_only condition from the where clause for the Sunday count
    # We'll build a separate query without the sunday_only filter, but with all other filters.
    sunday_where = [c for c in where_clauses if "strftime('%w', cd.extract_date)" not in c]
    sunday_sql = f"""
        SELECT COUNT(*) as sunday_count 
        FROM client_data cd 
        WHERE {' AND '.join(sunday_where)} AND strftime('%w', cd.extract_date) = '0'
    """
    # But we need to adjust params: the sunday_where clauses have the same params as the original without the sunday_only condition.
    # We'll need to use the original params without the sunday_only param if it was added.
    # Actually, easier: we'll just compute the Sunday count from the data we fetch? That could be heavy if we paginate.
    # Better: compute it separately using a query.
    # Since we have the original params (without the sunday_only condition), we can use them.
    # But the params list may have been modified if sunday_only was true, we need to reconstruct.
    # Simpler: we'll compute using a subquery with the same filters but adding the Sunday condition.
    # We'll build the Sunday count query similarly.
    # We'll use the original where_clauses without the sunday_only clause.
    # We'll store the base params before adding sunday_only.
    # So we'll restructure: build base params and clauses first, then add sunday_only condition.
    # Let's refactor a bit.
    
    # Rebuild to get sunday_count correctly:
    # We'll separate the base filters (without sunday_only) and then add it.
    base_where = [
        "cd.client_id = ?",
        "cd.template_id = ?",
        "cd.is_deleted = 0",
        "cd.extract_date BETWEEN ? AND ?"
    ]
    base_params = [client_id, template_id, date_start, date_end]
    
    if search:
        base_where.append("EXISTS (SELECT 1 FROM json_each(cd.row_data) WHERE LOWER(value) LIKE LOWER(?))")
        base_params.append(f"%{search}%")
    if amount_min is not None:
        base_where.append("cd.extract_amount >= ?")
        base_params.append(amount_min)
    if amount_max is not None:
        base_where.append("cd.extract_amount <= ?")
        base_params.append(amount_max)
    
    # Now build the final where with sunday_only if needed
    final_where = base_where.copy()
    final_params = base_params.copy()
    if sunday_only:
        final_where.append("strftime('%w', cd.extract_date) = '0'")
        # No extra param needed for strftime
    
    where_sql = " AND ".join(final_where)
    
    # Get total count with final filters
    count_sql = f"SELECT COUNT(*) as total FROM client_data cd WHERE {where_sql}"
    total_result = execute_query(count_sql, final_params)
    total_count = total_result[0]['total'] if total_result else 0
    
    # Get Sunday count (without sunday_only filter, but with other filters)
    sunday_where = base_where + ["strftime('%w', cd.extract_date) = '0'"]
    sunday_sql = f"SELECT COUNT(*) as sunday_count FROM client_data cd WHERE {' AND '.join(sunday_where)}"
    sunday_result = execute_query(sunday_sql, base_params)
    sunday_count = sunday_result[0]['sunday_count'] if sunday_result else 0
    
    if total_count == 0:
        return {
            "template_id": template_id,
            "template_name": template[0]['template_name'],
            "total_rows": 0,
            "sunday_count": sunday_count,
            "page": page,
            "limit": limit,
            "rows": []
        }
    
    # Pagination
    offset = (page - 1) * limit
    data_sql = f"""
        SELECT cd.id, cd.row_data, cd.extract_amount, cd.extract_date, cd.created_at
        FROM client_data cd
        WHERE {where_sql}
        ORDER BY cd.extract_date DESC, cd.id
        LIMIT ? OFFSET ?
    """
    data_params = final_params + [limit, offset]
    rows = execute_query(data_sql, data_params)
    
    # Format rows
    result_rows = []
    for r in rows:
        try:
            row_dict = json.loads(r["row_data"])
        except:
            row_dict = {}
        result_rows.append({
            "id": r["id"],
            "row_data": row_dict,
            "extract_amount": r["extract_amount"],
            "extract_date": r["extract_date"],
            "created_at": format_datetime(r["created_at"])
        })
    
    return {
        "template_id": template_id,
        "template_name": template[0]['template_name'],
        "total_rows": total_count,
        "sunday_count": sunday_count,
        "page": page,
        "limit": limit,
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
    upload = execute_query(
        "SELECT id, client_id, status FROM uploaded_files WHERE id = ?",
        (upload_id,)
    )
    if not upload:
        raise HTTPException(404, "Upload not found")
    
    # Build WHERE clause dynamically
    where_clauses = ["upload_id = ? AND is_deleted = 0"]
    params = [upload_id]
    
    # Search in row_data JSON (case-insensitive)
    if search:
        # SQLite JSON search: use json_each to search all values
        where_clauses.append(
            "EXISTS (SELECT 1 FROM json_each(row_data) WHERE LOWER(value) LIKE LOWER(?))"
        )
        params.append(f"%{search}%")
    
    # Amount range
    if amount_min is not None:
        where_clauses.append("extract_amount >= ?")
        params.append(amount_min)
    if amount_max is not None:
        where_clauses.append("extract_amount <= ?")
        params.append(amount_max)
    
    # Date range
    if date_start:
        where_clauses.append("extract_date >= ?")
        params.append(date_start)
    if date_end:
        where_clauses.append("extract_date <= ?")
        params.append(date_end)
    
    where_sql = " AND ".join(where_clauses)
    
    # Get total count with filters
    count_sql = f"SELECT COUNT(*) as count FROM client_data WHERE {where_sql}"
    total = execute_query(count_sql, params)[0]['count']
    
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
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    rows = execute_query(data_sql, params)
    
    result_rows = []
    for r in rows:
        try:
            row_dict = json.loads(r["row_data"])
        except:
            row_dict = {}
        result_rows.append({
            "id": r["id"],
            "row_data": row_dict,
            "extract_amount": r["extract_amount"],
            "extract_date": r["extract_date"],
            "created_at": format_datetime(r["created_at"])
        })
    
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
    upload = execute_query(
        "SELECT id, file_name FROM uploaded_files WHERE id = ?",
        (upload_id,)
    )
    if not upload:
        raise HTTPException(404, "Upload not found")
    
    # Fetch all rows in this dataset (to log their data before deletion)
    rows = execute_query(
        "SELECT id, row_data FROM client_data WHERE upload_id = ? AND is_deleted = 0",
        (upload_id,)
    )
    
    # Soft-delete all rows
    execute_write(
        "UPDATE client_data SET is_deleted = 1 WHERE upload_id = ?",
        (upload_id,)
    )
    
    # Log each row deletion
    import json
    for row in rows:
        old_data = json.loads(row["row_data"]) if row["row_data"] else {}
        log_data_audit(row["id"], "Analyst", "DELETE", old_data, None)
    
    # Update upload status
    execute_write(
        "UPDATE uploaded_files SET status = 'Deleted' WHERE id = ?",
        (upload_id,)
    )
    log_audit("Analyst", "Delete", "DataBank", f"Deleted dataset upload_id={upload_id} ({len(rows)} rows)")
    
    return {"message": f"Dataset '{upload[0]['file_name']}' deleted successfully ({len(rows)} rows)"}

@router.post("/api/data/datasets/{upload_id}/restore")
def restore_dataset(upload_id: int):
    """
    Restore a soft-deleted dataset.
    """
    upload = execute_query(
        "SELECT id, file_name FROM uploaded_files WHERE id = ? AND status = 'Deleted'",
        (upload_id,)
    )
    if not upload:
        raise HTTPException(404, "Deleted upload not found")
    
    # Fetch rows that are currently deleted
    rows = execute_query(
        "SELECT id, row_data FROM client_data WHERE upload_id = ? AND is_deleted = 1",
        (upload_id,)
    )
    
    # Restore rows (set is_deleted = 0)
    execute_write(
        "UPDATE client_data SET is_deleted = 0 WHERE upload_id = ?",
        (upload_id,)
    )
    
    # Log each row restore
    import json
    for row in rows:
        new_data = json.loads(row["row_data"]) if row["row_data"] else {}
        log_data_audit(row["id"], "Analyst", "RESTORE", None, new_data)
    
    execute_write(
        "UPDATE uploaded_files SET status = 'Imported' WHERE id = ?",
        (upload_id,)
    )
    log_audit("Analyst", "Restore", "DataBank", f"Restored dataset upload_id={upload_id} ({len(rows)} rows)")
    
    return {"message": f"Dataset '{upload[0]['file_name']}' restored successfully ({len(rows)} rows)"}

# ==============================================
# 📊 AVAILABILITY CHECKLIST ENDPOINT
# ==============================================

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
    - date_min / date_max: FULL date range of all data for this template
    - sunday_count: Sunday rows matching the filter (for preview context)
    - status: 'complete' if any data exists (unfiltered), else 'missing'
    - filtered_status: 'complete' if filtered rows > 0, else 'missing'
    """
    # Validate client exists
    client = execute_query("SELECT id FROM clients WHERE id = ? AND is_active = 1", (client_id,))
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
            COALESCE(SUM(CASE WHEN strftime('%w', cd.extract_date) = '0' THEN 1 ELSE 0 END), 0) AS filtered_sunday_count
        FROM templates t
        LEFT JOIN client_data cd 
            ON cd.template_id = t.id 
            AND cd.client_id = ? 
            AND cd.is_deleted = 0 
            AND cd.extract_date BETWEEN ? AND ?
    """
    params = [client_id, date_start, date_end]
    
    if template_id is not None:
        filtered_query += " AND t.id = ?"
        params.append(template_id)
    
    filtered_query += " WHERE t.is_active = 1"
    filtered_query += " GROUP BY t.id ORDER BY t.template_name"
    
    filtered_rows = execute_query(filtered_query, params)
    
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
            AND cd.client_id = ? 
            AND cd.is_deleted = 0
    """
    total_params = [client_id]
    
    if template_id is not None:
        total_query += " AND t.id = ?"
        total_params.append(template_id)
    
    total_query += " WHERE t.is_active = 1"
    total_query += " GROUP BY t.id ORDER BY t.template_name"
    
    total_rows = execute_query(total_query, total_params)
    
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
        
        result.append({
            "template_id": tid,
            "template_name": filtered["template_name"] if filtered["template_name"] != "Unknown" else total.get("template_name", f"Template {tid}"),
            "filtered_row_count": filtered["filtered_row_count"],
            "total_row_count": total["total_row_count"],
            "date_min": total["date_min"],           # FULL date range (unfiltered)
            "date_max": total["date_max"],           # FULL date range (unfiltered)
            "filtered_sunday_count": filtered["filtered_sunday_count"],
            "status": status,                        # based on total data
            "filtered_status": filtered_status       # based on filtered data
        })
    
    return result