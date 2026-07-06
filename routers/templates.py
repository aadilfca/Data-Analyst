from fastapi import APIRouter, HTTPException
from database import execute_query, execute_write, log_audit
import json
from typing import List

router = APIRouter()

# ---------- MASTER COLUMNS ----------
@router.get("/api/columns")
def get_columns():
    return execute_query("SELECT id, column_name, data_type, category, is_active FROM master_columns WHERE is_active = 1 ORDER BY category, column_name")

@router.get("/api/columns/all")
def get_all_columns():
    return execute_query("SELECT id, column_name, data_type, category, is_active FROM master_columns ORDER BY category, column_name")

@router.post("/api/columns")
def create_column(column_name: str, data_type: str, category: str = "General"):
    existing = execute_query("SELECT id FROM master_columns WHERE column_name = ?", (column_name,))
    if existing:
        raise HTTPException(400, "Column name already exists")
    col_id = execute_write("INSERT INTO master_columns (column_name, data_type, category) VALUES (?, ?, ?)", (column_name, data_type, category))
    log_audit("Admin", "Create", "Columns", f"Added column: {column_name}")
    return {"message": f"Column '{column_name}' created successfully", "column_id": col_id}

@router.put("/api/columns/{column_id}")
def update_column(column_id: int, column_name: str, data_type: str, category: str, is_active: bool = 1):
    existing = execute_query("SELECT id FROM master_columns WHERE id = ?", (column_id,))
    if not existing:
        raise HTTPException(404, "Column not found")
    execute_write("UPDATE master_columns SET column_name=?, data_type=?, category=?, is_active=? WHERE id=?", (column_name, data_type, category, is_active, column_id))
    log_audit("Admin", "Update", "Columns", f"Updated column: {column_name}")
    return {"message": f"Column '{column_name}' updated successfully"}

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