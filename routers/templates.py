from fastapi import APIRouter, HTTPException, Body
from database import execute_query, execute_write, log_audit, get_raw_conn
import json
from typing import List
from pydantic import BaseModel

router = APIRouter()

# ---------- MASTER COLUMNS ----------
class ColumnCreate(BaseModel):
    column_name: str
    data_type: str
    category: str = "General"
    notes: str | None = None

class ColumnUpdate(BaseModel):
    column_name: str
    data_type: str
    category: str = "General"
    is_active: bool = 1
    notes: str | None = None

@router.get("/api/columns")
def get_columns():
    return execute_query("SELECT id, column_name, data_type, category, is_active, notes FROM master_columns WHERE is_active = 1 ORDER BY category, column_name")

@router.get("/api/columns/all")
def get_all_columns():
    return execute_query("SELECT id, column_name, data_type, category, is_active, notes FROM master_columns ORDER BY category, column_name")

@router.post("/api/columns")
def create_column(column: ColumnCreate):
    # Case-insensitive duplicate check so "invoice date" is blocked against "Invoice Date"
    existing = execute_query(
        "SELECT id, column_name FROM master_columns WHERE LOWER(column_name) = LOWER(?)",
        (column.column_name,)
    )
    if existing:
        raise HTTPException(400, f"Column name already exists: '{existing[0]['column_name']}'")
    col_id = execute_write(
        "INSERT INTO master_columns (column_name, data_type, category, notes) VALUES (?, ?, ?, ?)",
        (column.column_name, column.data_type, column.category, column.notes)
    )
    log_audit("Admin", "Create", "Columns", f"Added column: {column.column_name}")
    return {"message": f"Column '{column.column_name}' created successfully", "column_id": col_id}

@router.put("/api/columns/{column_id}")
def update_column(column_id: int, column: ColumnUpdate):
    existing = execute_query("SELECT id FROM master_columns WHERE id = ?", (column_id,))
    if not existing:
        raise HTTPException(404, "Column not found")
    execute_write(
        "UPDATE master_columns SET column_name=?, data_type=?, category=?, is_active=?, notes=? WHERE id=?",
        (column.column_name, column.data_type, column.category, column.is_active, column.notes, column_id)
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

@router.get("/api/columns/close-matches")
def get_close_matches(name: str):
    """
    Advisory-only lookup: returns active master columns whose name contains,
    or is contained by, the typed name (case-insensitive substring match).
    Not a duplicate check -- the frontend uses this to show a
    'Did you mean ...?' hint without blocking save.
    """
    typed = name.strip().lower()
    if not typed:
        return []
    all_columns = execute_query(
        "SELECT id, column_name, data_type, category FROM master_columns WHERE is_active = 1"
    )
    return [
        col for col in all_columns
        if col['column_name'].lower() != typed
        and (typed in col['column_name'].lower() or col['column_name'].lower() in typed)
    ]

# ---------- TEMPLATES (with PIVOT table) ----------

class TemplateCreate(BaseModel):
    template_name: str
    selected_columns: List[int]
    created_by: str = "Admin"

class TemplateUpdate(BaseModel):
    template_name: str
    selected_columns: List[int]

@router.get("/api/templates")
def get_templates():
    """Return all active templates with their columns (ordered by sort_order)."""
    templates = execute_query(
        "SELECT id, template_name, created_by, created_date, is_active FROM templates WHERE is_active = 1 ORDER BY template_name"
    )
    for t in templates:
        mappings = execute_query(
            """
            SELECT m.id, m.column_name, m.data_type, m.category, p.sort_order
            FROM template_column_mappings p
            JOIN master_columns m ON p.column_id = m.id
            WHERE p.template_id = ? AND m.is_active = 1
            ORDER BY p.sort_order ASC
            """,
            (t['id'],)
        )
        t['columns'] = mappings
    return templates

@router.get("/api/templates/{template_id}")
def get_template(template_id: int):
    result = execute_query("SELECT * FROM templates WHERE id = ? AND is_active = 1", (template_id,))
    if not result:
        raise HTTPException(404, "Template not found")
    template = result[0]
    mappings = execute_query(
        """
        SELECT m.id, m.column_name, m.data_type, m.category, p.sort_order
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = ? AND m.is_active = 1
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )
    template['columns'] = mappings
    return template

@router.post("/api/templates")
def create_template(template: TemplateCreate):
    existing = execute_query("SELECT id FROM templates WHERE template_name = ?", (template.template_name,))
    if existing:
        raise HTTPException(400, "Template name already exists")
    if not template.selected_columns:
        raise HTTPException(400, "Select at least one column")

    template_id = execute_write(
        "INSERT INTO templates (template_name, created_by) VALUES (?, ?)",
        (template.template_name, template.created_by)
    )

    for idx, col_id in enumerate(template.selected_columns):
        execute_write(
            "INSERT INTO template_column_mappings (template_id, column_id, sort_order) VALUES (?, ?, ?)",
            (template_id, col_id, idx)
        )

    log_audit(template.created_by, "Create", "Templates", f"Created template: {template.template_name}")
    return {"message": f"Template '{template.template_name}' created successfully", "template_id": template_id}

@router.put("/api/templates/{template_id}")
def update_template(template_id: int, template: TemplateUpdate):
    existing = execute_query("SELECT id FROM templates WHERE id = ? AND is_active = 1", (template_id,))
    if not existing:
        raise HTTPException(404, "Template not found")
    duplicate = execute_query("SELECT id FROM templates WHERE template_name = ? AND id != ?", (template.template_name, template_id))
    if duplicate:
        raise HTTPException(400, "Template name already exists")

    execute_write("UPDATE templates SET template_name = ? WHERE id = ?", (template.template_name, template_id))
    execute_write("DELETE FROM template_column_mappings WHERE template_id = ?", (template_id,))
    for idx, col_id in enumerate(template.selected_columns):
        execute_write(
            "INSERT INTO template_column_mappings (template_id, column_id, sort_order) VALUES (?, ?, ?)",
            (template_id, col_id, idx)
        )

    log_audit("Admin", "Update", "Templates", f"Updated template: {template.template_name}")
    return {"message": f"Template '{template.template_name}' updated successfully"}

@router.delete("/api/templates/{template_id}")
def delete_template(template_id: int):
    existing = execute_query(
        "SELECT template_name, is_system FROM templates WHERE id = ? AND is_active = 1",
        (template_id,)
    )
    if not existing:
        raise HTTPException(404, "Template not found")
    if existing[0]['is_system'] == 1:
        raise HTTPException(403, "Cannot delete a system template. It is required for the application to function.")

    # Check if any client_data rows reference this template
    dependent_rows = execute_query(
        "SELECT COUNT(*) as count FROM client_data WHERE template_id = ? AND is_deleted = 0",
        (template_id,)
    )
    if dependent_rows and dependent_rows[0]['count'] > 0:
        raise HTTPException(
            400,
            f"Cannot delete template because it is used by {dependent_rows[0]['count']} data rows. "
            "Please delete or reassign those rows first."
        )

    execute_write("UPDATE templates SET is_active = 0 WHERE id = ?", (template_id,))
    execute_write("DELETE FROM template_column_mappings WHERE template_id = ?", (template_id,))
    log_audit("Admin", "Delete", "Templates", f"Soft-deleted template: {existing[0]['template_name']}")
    return {"message": "Template deleted successfully (soft-delete)"}

# ---------- NEW ENDPOINTS FOR UPLOAD & PREVIEW ----------

# ---------- NEW ENDPOINTS FOR UPLOAD & PREVIEW ----------

@router.get("/api/templates/{template_id}/upload-columns")
def get_template_upload_columns(template_id: int):
    """Return the columns of a template with sort_order (for upload preview)."""
    result = execute_query("SELECT id FROM templates WHERE id = ? AND is_active = 1", (template_id,))
    if not result:
        raise HTTPException(404, "Template not found")
    mappings = execute_query(
        """
        SELECT m.id, m.column_name, m.data_type, m.category, p.sort_order
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = ? AND m.is_active = 1
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )
    return {"template_id": template_id, "columns": mappings, "total_columns": len(mappings)}

@router.post("/api/templates/{template_id}/reorder")
def reorder_template_columns(template_id: int, column_ids: List[int] = Body(...)):
    """Update sort_order for all columns of a template."""
    existing = execute_query("SELECT id FROM templates WHERE id = ? AND is_active = 1", (template_id,))
    if not existing:
        raise HTTPException(404, "Template not found")
    for idx, col_id in enumerate(column_ids):
        execute_write(
            "UPDATE template_column_mappings SET sort_order = ? WHERE template_id = ? AND column_id = ?",
            (idx, template_id, col_id)
        )
    log_audit("Admin", "Reorder", "Templates", f"Reordered columns for template ID {template_id}")
    return {"message": "Order updated successfully"}

@router.delete("/api/templates/{template_id}/columns/{column_id}")
def remove_template_column(template_id: int, column_id: int):
    """Remove a specific column mapping from a template."""
    existing = execute_query("SELECT id FROM templates WHERE id = ? AND is_active = 1", (template_id,))
    if not existing:
        raise HTTPException(404, "Template not found")
    deleted = execute_write(
        "DELETE FROM template_column_mappings WHERE template_id = ? AND column_id = ?",
        (template_id, column_id)
    )
    if deleted == 0:
        raise HTTPException(404, "Column mapping not found")
    log_audit("Admin", "RemoveColumn", "Templates", f"Removed column {column_id} from template {template_id}")
    return {"message": "Column removed from template"}
@router.get("/api/templates/by-name/{template_name}")
def get_template_by_name(template_name: str):
    """
    Get template ID and details by its name. Useful for system lookups.
    """
    result = execute_query(
        "SELECT id, template_name, is_system FROM templates WHERE template_name = ? AND is_active = 1",
        (template_name,)
    )
    if not result:
        raise HTTPException(404, f"Template '{template_name}' not found or inactive.")
    return {"id": result[0]['id'], "template_name": result[0]['template_name'], "is_system": result[0]['is_system']}
# ---------- BACKUP / RESTORE ----------
class BackupData(BaseModel):
    columns: list
    templates: list
    mappings: list
    clients: list = []
    client_data: list = []

@router.get("/api/backup/export")
def export_backup():
    import json
    columns = execute_query("SELECT * FROM master_columns ORDER BY id")
    templates = execute_query("SELECT * FROM templates ORDER BY id")
    mappings = execute_query("SELECT * FROM template_column_mappings ORDER BY template_id, sort_order")
    clients = execute_query("SELECT * FROM clients ORDER BY id")
    
    # Get all client_data rows (convert JSON strings to objects for clean backup)
    data_rows = execute_query("SELECT * FROM client_data ORDER BY client_id, upload_id, id")
    client_data = []
    for row in data_rows:
        row_dict = dict(row)
        # Convert row_data from JSON string to object for readable backup
        try:
            row_dict['row_data'] = json.loads(row_dict['row_data'])
        except:
            pass
        client_data.append(row_dict)
    
    settings = execute_query("SELECT key, value FROM app_settings")
    print(f"💾 Backup: {len(columns)} columns, {len(templates)} templates, {len(mappings)} mappings, {len(clients)} clients, {len(client_data)} data rows, {len(settings)} settings")
    
    return {
        "columns": columns,
        "templates": templates,
        "mappings": mappings,
        "clients": clients,
        "client_data": client_data,
        "settings": settings
    }

@router.post("/api/backup/import")
def import_backup(data: BackupData = Body(...)):
    import json
    conn = get_raw_conn()
    try:
        conn.execute("BEGIN TRANSACTION")
        conn.execute("PRAGMA foreign_keys = OFF")
        
        # Delete existing data in correct order (child tables first)
        conn.execute("DELETE FROM client_data")
        conn.execute("DELETE FROM uploaded_files")
        conn.execute("DELETE FROM verifications")
        conn.execute("DELETE FROM clients")
        conn.execute("DELETE FROM template_column_mappings")
        conn.execute("DELETE FROM templates")
        conn.execute("DELETE FROM app_settings")
        conn.execute("DELETE FROM master_columns")
        
        # Restore master_columns
        for col in data.columns:
            conn.execute(
                "INSERT INTO master_columns (id, column_name, data_type, category, is_active) VALUES (?, ?, ?, ?, ?)",
                (col['id'], col['column_name'], col['data_type'], col.get('category', 'General'), col.get('is_active', 1))
            )
        
        # Restore templates
        for tpl in data.templates:
            conn.execute(
                "INSERT INTO templates (id, template_name, created_by, created_date, is_active) VALUES (?, ?, ?, ?, ?)",
                (tpl['id'], tpl['template_name'], tpl.get('created_by', 'Admin'), tpl.get('created_date'), tpl.get('is_active', 1))
            )
        
        # Restore template_column_mappings
        for mp in data.mappings:
            conn.execute(
                "INSERT INTO template_column_mappings (id, template_id, column_id, sort_order) VALUES (?, ?, ?, ?)",
                (mp['id'], mp['template_id'], mp['column_id'], mp['sort_order'])
            )
        
        # Restore clients
        for client in data.clients:
            conn.execute(
                "INSERT INTO clients (id, client_code, client_name, industry, contact_person, email, phone, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    client['id'],
                    client['client_code'],
                    client['client_name'],
                    client.get('industry'),
                    client.get('contact_person'),
                    client.get('email'),
                    client.get('phone'),
                    client.get('is_active', 1),
                    client.get('created_at')
                )
            )
        
        # Restore app_settings
        if hasattr(data, 'settings') and data.settings:
            for setting in data.settings:
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?)",
                    (setting['key'], setting['value'])
                )
        
        # Restore client_data (convert row_data back to JSON string)
        for row in data.client_data:
            row_data_str = json.dumps(row['row_data']) if isinstance(row['row_data'], dict) else row['row_data']
            conn.execute(
                """
                INSERT INTO client_data (id, client_id, template_id, upload_id, row_data, extract_amount, extract_date, created_at, created_by, is_deleted, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row['id'],
                    row['client_id'],
                    row['template_id'],
                    row['upload_id'],
                    row_data_str,
                    row.get('extract_amount'),
                    row.get('extract_date'),
                    row.get('created_at'),
                    row.get('created_by', 'Analyst'),
                    row.get('is_deleted', 0),
                    row.get('version', 1)
                )
            )
        
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        log_audit("Admin", "Import", "Backup", f"Imported backup: {len(data.columns)} columns, {len(data.templates)} templates, {len(data.clients)} clients, {len(data.client_data)} data rows")
        
        return {
            "message": f"Successfully imported {len(data.columns)} columns, {len(data.templates)} templates, {len(data.mappings)} mappings, {len(data.clients)} clients, and {len(data.client_data)} data rows."
        }
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Import failed: {str(e)}")
    finally:
        conn.close()
