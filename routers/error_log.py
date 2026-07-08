# ==============================================
# 📋 ERROR LOG API ROUTER
# ==============================================

from fastapi import APIRouter, HTTPException, Query
from database import execute_query, execute_write, log_audit
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()

# ---------- MODELS ----------
class ErrorLogUpdate(BaseModel):
    is_resolved: bool
    resolution_notes: Optional[str] = None
    resolved_by: Optional[str] = "Admin"

class ErrorLogResponse(BaseModel):
    id: int
    error_code: Optional[str]
    user_id: str
    client_id: Optional[int]
    module: Optional[str]
    endpoint: Optional[str]
    method: Optional[str]
    status_code: Optional[int]
    error_category: Optional[str]
    error_message: Optional[str]
    file_name: Optional[str]
    line_number: Optional[int]
    code_context: Optional[str]
    stack_trace: Optional[str]
    input_data: Optional[str]
    suggested_fix: Optional[str]
    is_resolved: bool
    resolution_notes: Optional[str]
    resolved_by: Optional[str]
    resolved_at: Optional[str]
    created_at: str

# ---------- HELPER ----------
def format_datetime(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        return dt.strftime('%d/%m/%Y %H:%M')
    except:
        return dt_str

def row_to_error_log(row: dict) -> dict:
    return {
        "id": row["id"],
        "error_code": row["error_code"],
        "user_id": row["user_id"],
        "client_id": row["client_id"],
        "module": row["module"],
        "endpoint": row["endpoint"],
        "method": row["method"],
        "status_code": row["status_code"],
        "error_category": row["error_category"],
        "error_message": row["error_message"],
        "file_name": row["file_name"],
        "line_number": row["line_number"],
        "code_context": row["code_context"],
        "stack_trace": row["stack_trace"],
        "input_data": row["input_data"],
        "suggested_fix": row["suggested_fix"],
        "is_resolved": bool(row["is_resolved"]),
        "resolution_notes": row["resolution_notes"],
        "resolved_by": row["resolved_by"],
        "resolved_at": format_datetime(row["resolved_at"]) if row["resolved_at"] else None,
        "created_at": format_datetime(row["created_at"])
    }

# ---------- ENDPOINTS ----------

@router.get("/api/error-logs", response_model=List[ErrorLogResponse])
def get_error_logs(
    skip: int = Query(0, ge=0, description="Number of records to skip for pagination"),
    limit: int = Query(50, ge=1, le=200, description="Number of records to return"),
    module: Optional[str] = Query(None, description="Filter by module (e.g., Upload, DataBank)"),
    error_category: Optional[str] = Query(None, description="Filter by category (e.g., Syntax, Database, Validation)"),
    is_resolved: Optional[bool] = Query(None, description="Filter by resolved status (true/false)"),
    search: Optional[str] = Query(None, description="Search in error_message or file_name"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)")
):
    """
    Get paginated list of error logs with optional filters.
    """
    # Build WHERE clause dynamically
    where_clauses = ["1=1"]  # Always true
    params = []

    if module:
        where_clauses.append("module = ?")
        params.append(module)
    if error_category:
        where_clauses.append("error_category = ?")
        params.append(error_category)
    if is_resolved is not None:
        where_clauses.append("is_resolved = ?")
        params.append(1 if is_resolved else 0)
    if search:
        where_clauses.append("(error_message LIKE ? OR file_name LIKE ?)")
        params.append(f"%{search}%")
        params.append(f"%{search}%")
    if start_date:
        where_clauses.append("date(created_at) >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("date(created_at) <= ?")
        params.append(end_date)

    where_sql = " AND ".join(where_clauses)

    # Get total count
    count_sql = f"SELECT COUNT(*) as count FROM error_log WHERE {where_sql}"
    total = execute_query(count_sql, params)[0]['count']

    # Get paginated results, ordered by most recent first
    query_params = params + [limit, skip]
    data_sql = f"""
        SELECT *
        FROM error_log
        WHERE {where_sql}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """
    rows = execute_query(data_sql, query_params)

    result = [row_to_error_log(r) for r in rows]
    
    # Include total count in the response headers? But we'll just return the list.
    # Optionally, we could add X-Total-Count header. But for simplicity, return full objects.
    return result

@router.get("/api/error-logs/{error_id}", response_model=ErrorLogResponse)
def get_error_log(error_id: int):
    """
    Get detailed information about a specific error log.
    """
    rows = execute_query("SELECT * FROM error_log WHERE id = ?", (error_id,))
    if not rows:
        raise HTTPException(404, "Error log not found")
    return row_to_error_log(rows[0])

@router.put("/api/error-logs/{error_id}", response_model=dict)
def update_error_log(error_id: int, update: ErrorLogUpdate):
    """
    Mark an error as resolved and/or add resolution notes.
    """
    existing = execute_query("SELECT id FROM error_log WHERE id = ?", (error_id,))
    if not existing:
        raise HTTPException(404, "Error log not found")
    
    resolved_at = datetime.now().isoformat() if update.is_resolved else None
    
    execute_write(
        """
        UPDATE error_log
        SET is_resolved = ?, resolution_notes = ?, resolved_by = ?, resolved_at = ?
        WHERE id = ?
        """,
        (
            1 if update.is_resolved else 0,
            update.resolution_notes,
            update.resolved_by,
            resolved_at,
            error_id
        )
    )
    
    log_audit("Admin", "Update", "ErrorLog", f"Updated error log {error_id}: resolved={update.is_resolved}")
    return {"message": "Error log updated successfully"}

@router.delete("/api/error-logs/{error_id}", response_model=dict)
def delete_error_log(error_id: int):
    """
    Delete an error log entry (permanent deletion).
    Use with caution – soft delete is better, but we'll allow hard delete for cleanup.
    """
    existing = execute_query("SELECT id FROM error_log WHERE id = ?", (error_id,))
    if not existing:
        raise HTTPException(404, "Error log not found")
    
    execute_write("DELETE FROM error_log WHERE id = ?", (error_id,))
    log_audit("Admin", "Delete", "ErrorLog", f"Deleted error log {error_id}")
    return {"message": "Error log deleted successfully"}

# ---------- STATS ENDPOINT ----------
@router.get("/api/error-logs/stats")
def get_error_stats():
    """
    Get summary statistics: total, resolved, pending, breakdown by category.
    """
    total = execute_query("SELECT COUNT(*) as count FROM error_log")[0]['count']
    resolved = execute_query("SELECT COUNT(*) as count FROM error_log WHERE is_resolved = 1")[0]['count']
    pending = total - resolved
    
    # Breakdown by category
    categories = execute_query(
        """
        SELECT error_category, COUNT(*) as count
        FROM error_log
        GROUP BY error_category
        ORDER BY count DESC
        """
    )
    category_breakdown = {row['error_category']: row['count'] for row in categories}
    
    # Last 7 days activity
    last_7_days = execute_query(
        """
        SELECT date(created_at) as day, COUNT(*) as count
        FROM error_log
        WHERE created_at >= date('now', '-7 days')
        GROUP BY date(created_at)
        ORDER BY day DESC
        """
    )
    daily_counts = {row['day']: row['count'] for row in last_7_days}
    
    return {
        "total": total,
        "resolved": resolved,
        "pending": pending,
        "category_breakdown": category_breakdown,
        "daily_counts": daily_counts
    }