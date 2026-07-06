from fastapi import APIRouter, HTTPException, Body
from database import execute_query, execute_write, log_audit, get_raw_conn
import json
from typing import List
from pydantic import BaseModel  # <-- Added this line

router = APIRouter()

# ---------- MASTER COLUMNS ----------
@router.get("/api/columns")
def get_columns():
    return execute_query("SELECT id, column_name, data_type, category, is_active FROM master_columns WHERE is_active = 1 ORDER BY category, column_name")

@router.get("/api/columns/all")
def get_all_columns():
    return execute_query("SELECT id, column_name, data_type, category, is_active FROM master_columns ORDER BY category, column_name")

# Define the data structures for incoming JSON
class ColumnCreate(BaseModel):
    column_name: str
    data_type: str
    category: str = "General"

class ColumnUpdate(BaseModel):
    column_name: str
    data_type: str
    category: str = "General"
    is_active: bool = 1

@router.post("/api/columns")
def create_column(column: ColumnCreate):
    existing = execute_query("SELECT id FROM master_columns WHERE column_name = ?", (column.column_name,))
    if existing:
        raise HTTPException(400, "Column name already exists")
    col_id = execute_write(
        "INSERT INTO master_columns (column_name, data_type, category) VALUES (?, ?, ?)",
        (column.column_name, column.data_type, column.category)
    )
    log_audit("Admin", "Create", "Columns", f"Added column: {column.column_name}")
    return {"message": f"Column '{column.column_name}' created successfully", "column_id": col_id}

@router.put("/api/columns/{column_id}")
def update_column(column_id: int, column: ColumnUpdate):
    existing = execute_query("SELECT id FROM master_columns WHERE id = ?", (column_id,))
    if not existing:
        raise HTTPException(404, "Column not found")
    execute_write(
        "UPDATE master_columns SET column_name=?, data_type=?, category=?, is_active=? WHERE id=?",
        (column.column_name, column.data_type, column.category, column.is_active, column_id)
    )
    log_audit("Admin", "Update", "Columns", f"Updated column: {column.column_name}")
    return {"message": f"Column '{column.column_name}' updated successfully"}

@router.delete("/api/columns/{column_id}")
def delete_column(column_id: int):
    existing = execute_query("SELECT column_name FROM master_columns WHERE id = ? AND is_active = 1", (column_id,))
    if not existing:
        raise HTTPException(404, "Column not found")
    execute_write("UPDATE master_columns SET is_active = 0 WHERE id = ?", (column_id,))
    log_audit("Admin", "Delete", "Columns", f"Deleted column: {existing[0]['column_name']}")
    return {"message": "Column deleted successfully"}

@router.get("/api/columns/categories")
def get_categories():
    return execute_query("SELECT DISTINCT category FROM master_columns WHERE is_active = 1 ORDER BY category")

# ---------- TEMPLATES (new) ----------
@router.get("/api/templates")
def get_templates():
    templates = execute_query("SELECT id, template_name, selected_columns, created_by, created_date, is_active FROM templates WHERE is_active = 1 ORDER BY template_name")
    for t in templates:
        t['selected_columns'] = json.loads(t['selected_columns'])
    return templates

@router.get("/api/templates/{template_id}")
def get_template(template_id: int):
    result = execute_query("SELECT * FROM templates WHERE id = ? AND is_active = 1", (template_id,))
    if not result:
        raise HTTPException(404, "Template not found")
    result[0]['selected_columns'] = json.loads(result[0]['selected_columns'])
    return result[0]

@router.post("/api/templates")
def create_template(template_name: str, selected_columns: List[int], created_by: str = "Admin"):
    existing = execute_query("SELECT id FROM templates WHERE template_name = ?", (template_name,))
    if existing:
        raise HTTPException(400, "Template name already exists")
    if not selected_columns:
        raise HTTPException(400, "Select at least one column")
    columns_json = json.dumps(selected_columns)
    template_id = execute_write("INSERT INTO templates (template_name, selected_columns, created_by) VALUES (?, ?, ?)", (template_name, columns_json, created_by))
    log_audit(created_by, "Create", "Templates", f"Created template: {template_name}")
    return {"message": f"Template '{template_name}' created successfully", "template_id": template_id}

@router.put("/api/templates/{template_id}")
def update_template(template_id: int, template_name: str, selected_columns: List[int]):
    existing = execute_query("SELECT id FROM templates WHERE id = ?", (template_id,))
    if not existing:
        raise HTTPException(404, "Template not found")
    duplicate = execute_query("SELECT id FROM templates WHERE template_name = ? AND id != ?", (template_name, template_id))
    if duplicate:
        raise HTTPException(400, "Template name already exists")
    columns_json = json.dumps(selected_columns)
    execute_write("UPDATE templates SET template_name = ?, selected_columns = ? WHERE id = ?", (template_name, columns_json, template_id))
    log_audit("Admin", "Update", "Templates", f"Updated template: {template_name}")
    return {"message": f"Template '{template_name}' updated successfully"}

@router.delete("/api/templates/{template_id}")
def delete_template(template_id: int):
    existing = execute_query("SELECT template_name FROM templates WHERE id = ? AND is_active = 1", (template_id,))
    if not existing:
        raise HTTPException(404, "Template not found")
    execute_write("UPDATE templates SET is_active = 0 WHERE id = ?", (template_id,))
    log_audit("Admin", "Delete", "Templates", f"Deleted template: {existing[0]['template_name']}")
    return {"message": "Template deleted successfully"}

@router.get("/api/templates/{template_id}/columns")
def get_template_columns(template_id: int):
    result = execute_query("SELECT selected_columns FROM templates WHERE id = ? AND is_active = 1", (template_id,))
    if not result:
        raise HTTPException(404, "Template not found")
    column_ids = json.loads(result[0]['selected_columns'])
    if not column_ids:
        return []
    placeholders = ','.join('?' * len(column_ids))
    columns = execute_query(f"SELECT id, column_name, data_type, category FROM master_columns WHERE id IN ({placeholders}) AND is_active = 1", column_ids)
    return columns
# ---------- BACKUP / RESTORE ----------

class BackupData(BaseModel):
    columns: list
    templates: list

@router.get("/api/backup/export")
def export_backup():
    """
    Export all master columns and templates as JSON.
    """
    columns = execute_query("SELECT * FROM master_columns ORDER BY id")
    templates = execute_query("SELECT * FROM templates ORDER BY id")
    # Parse selected_columns JSON for templates
    for t in templates:
        t['selected_columns'] = json.loads(t['selected_columns'])
    return {"columns": columns, "templates": templates}

@router.post("/api/backup/import")
def import_backup(data: BackupData = Body(...)):
    """
    Import (overwrite) all master columns and templates from provided JSON.
    Uses a transaction to ensure atomicity.
    """
    conn = get_raw_conn()
    try:
        conn.execute("BEGIN TRANSACTION")
        # Turn off foreign key checks to allow reinsertion with matching IDs
        conn.execute("PRAGMA foreign_keys = OFF")

        # Delete existing data (hard delete)
        conn.execute("DELETE FROM templates")
        conn.execute("DELETE FROM master_columns")

        # Insert columns
        for col in data.columns:
            conn.execute(
                "INSERT INTO master_columns (id, column_name, data_type, category, is_active) VALUES (?, ?, ?, ?, ?)",
                (col['id'], col['column_name'], col['data_type'], col.get('category', 'General'), col.get('is_active', 1))
            )

        # Insert templates
        for tpl in data.templates:
            selected_json = json.dumps(tpl['selected_columns'])
            conn.execute(
                "INSERT INTO templates (id, template_name, selected_columns, created_by, created_date, is_active) VALUES (?, ?, ?, ?, ?, ?)",
                (tpl['id'], tpl['template_name'], selected_json, tpl.get('created_by', 'Admin'), tpl.get('created_date'), tpl.get('is_active', 1))
            )

        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        log_audit("Admin", "Import", "Backup", "Imported backup data")
        return {"message": f"Successfully imported {len(data.columns)} columns and {len(data.templates)} templates."}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Import failed: {str(e)}")
    finally:
        conn.close()