from fastapi import APIRouter, HTTPException, Body, Query, Response
from database import execute_query_pg, execute_write_pg, log_audit, get_raw_conn_pg, release_raw_conn_pg
from psycopg2.extras import Json
import json
from typing import List, Optional
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
    # is_active is omitted here (unlike /api/columns/all below) - this endpoint already
    # filters WHERE is_active = TRUE, so the field would be redundant on every row it
    # can ever return (Column trimming, Phase 5 of 13).
    return execute_query_pg("SELECT id, column_name, data_type, category, notes FROM master_columns WHERE is_active = TRUE ORDER BY category, column_name")

@router.get("/api/columns/all")
def get_all_columns():
    return execute_query_pg("SELECT id, column_name, data_type, category, is_active, notes FROM master_columns ORDER BY category, column_name")

@router.post("/api/columns")
def create_column(column: ColumnCreate):
    # Case-insensitive duplicate check so "invoice date" is blocked against "Invoice Date"
    existing = execute_query_pg(
        "SELECT id, column_name FROM master_columns WHERE LOWER(column_name) = LOWER(%s)",
        (column.column_name,)
    )
    if existing:
        raise HTTPException(400, f"Column name already exists: '{existing[0]['column_name']}'")
    col_id = execute_write_pg(
        "INSERT INTO master_columns (column_name, data_type, category, notes) VALUES (%s, %s, %s, %s) RETURNING id",
        (column.column_name, column.data_type, column.category, column.notes)
    )
    log_audit("Admin", "Create", "Columns", f"Added column: {column.column_name}")
    return {"message": f"Column '{column.column_name}' created successfully", "column_id": col_id}

@router.put("/api/columns/{column_id}")
def update_column(column_id: int, column: ColumnUpdate):
    existing = execute_query_pg("SELECT id FROM master_columns WHERE id = %s", (column_id,))
    if not existing:
        raise HTTPException(404, "Column not found")
    execute_write_pg(
        "UPDATE master_columns SET column_name=%s, data_type=%s, category=%s, is_active=%s, notes=%s WHERE id=%s",
        (column.column_name, column.data_type, column.category, column.is_active, column.notes, column_id)
    )
    log_audit("Admin", "Update", "Columns", f"Updated column: {column.column_name}")
    return {"message": f"Column '{column.column_name}' updated successfully"}

@router.delete("/api/columns/{column_id}")
def delete_column(column_id: int):
    existing = execute_query_pg("SELECT column_name FROM master_columns WHERE id = %s AND is_active = TRUE", (column_id,))
    if not existing:
        raise HTTPException(404, "Column not found")
    execute_write_pg("UPDATE master_columns SET is_active = FALSE WHERE id = %s", (column_id,))
    log_audit("Admin", "Delete", "Columns", f"Deleted column: {existing[0]['column_name']}")
    return {"message": "Column deleted successfully"}

@router.get("/api/columns/categories")
def get_categories():
    return execute_query_pg("SELECT DISTINCT category FROM master_columns WHERE is_active = TRUE ORDER BY category")

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
    all_columns = execute_query_pg(
        "SELECT id, column_name, data_type, category FROM master_columns WHERE is_active = TRUE"
    )
    return [
        col for col in all_columns
        if col['column_name'].lower() != typed
        and (typed in col['column_name'].lower() or col['column_name'].lower() in typed)
    ]

@router.get("/api/columns/{column_id}/usage")
def get_column_usage(column_id: int):
    """
    Read-only usage/dependency preview for a master column, shown on the edit form
    before a rename is saved -- visibility only, does not block or require confirmation.
    """
    col = execute_query_pg("SELECT id, column_name FROM master_columns WHERE id = %s", (column_id,))
    if not col:
        raise HTTPException(404, "Column not found")
    column_name = col[0]["column_name"]

    documents_count = execute_query_pg(
        "SELECT COUNT(DISTINCT document_id) as cnt FROM audit_rule_document_fields WHERE master_column_id = %s",
        (column_id,)
    )[0]["cnt"]

    # compare_field is free text (not FK'd to master_columns, per database.py's own comment) --
    # match by column_name case-insensitively, same convention as
    # routers/audit_rules.py's resolve_field_name_to_master_column_id()
    cross_checks_count = execute_query_pg(
        "SELECT COUNT(*) as cnt FROM audit_rule_cross_checks WHERE LOWER(TRIM(compare_field)) = LOWER(TRIM(%s))",
        (column_name,)
    )[0]["cnt"]

    templates_count = execute_query_pg(
        """
        SELECT COUNT(*) as cnt FROM template_column_mappings tcm
        JOIN templates t ON t.id = tcm.template_id
        WHERE tcm.column_id = %s AND t.is_active = TRUE
        """,
        (column_id,)
    )[0]["cnt"]

    return {
        "documents_count": documents_count,
        "cross_checks_count": cross_checks_count,
        "templates_count": templates_count,
    }

# ---------- TEMPLATES (with PIVOT table) ----------

RATE_SOURCE_VALUES = ('auto', 'imported', 'derived')
# Rate is only ever imported-vs-derived for these templates (matched case-insensitively) --
# Purchase Demand / Gate Pass never carry a rate_source.
RATE_SOURCE_ELIGIBLE_TEMPLATES = {'purchase order', 'store receiving', 'vendor bill'}

def resolve_rate_source(template_name: str, rate_source: str | None) -> str | None:
    """Only Purchase Order / Store Receiving / Vendor Bill templates carry a rate_source.
    Eligible templates default to 'auto'.
    Any other template's rate_source is always stored as NULL."""
    if template_name.strip().lower() not in RATE_SOURCE_ELIGIBLE_TEMPLATES:
        return None
    if rate_source is None:
        return 'auto'
    if rate_source not in RATE_SOURCE_VALUES:
        raise HTTPException(400, f"rate_source must be one of {RATE_SOURCE_VALUES}")
    return rate_source

class TemplateCreate(BaseModel):
    template_name: str
    selected_columns: List[int]
    created_by: str = "Admin"
    rate_source: str | None = None
    has_subtotals_by_default: bool = False

class TemplateUpdate(BaseModel):
    template_name: str
    selected_columns: List[int]
    rate_source: str | None = None
    has_subtotals_by_default: bool = False

@router.get("/api/templates")
def get_templates(
    response: Response,
    limit: Optional[int] = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Return active templates with their columns (ordered by sort_order).
    limit/offset optionally paginate the top-level template rows (Fast Data Handling Phase 11
    of 13, backend support only this phase) -- omitted (every current caller: templates.js's
    cache, upload.js's dropdown, audit_rules.js's template select) returns the full list with
    no X-Total-Count header, unchanged from before this phase."""
    base_query = "SELECT id, template_name, created_by, created_date, is_active, rate_source, has_subtotals_by_default FROM templates WHERE is_active = TRUE"
    if limit is not None:
        total = execute_query_pg("SELECT COUNT(*) as cnt FROM templates WHERE is_active = TRUE")[0]["cnt"]
        response.headers["X-Total-Count"] = str(total)
        templates = execute_query_pg(base_query + " ORDER BY template_name LIMIT %s OFFSET %s", (limit, offset))
    else:
        templates = execute_query_pg(base_query + " ORDER BY template_name")
    for t in templates:
        mappings = execute_query_pg(
            """
            SELECT m.id, m.column_name, m.data_type, p.sort_order
            FROM template_column_mappings p
            JOIN master_columns m ON p.column_id = m.id
            WHERE p.template_id = %s AND m.is_active = TRUE
            ORDER BY p.sort_order ASC
            """,
            (t['id'],)
        )
        t['columns'] = mappings
    return templates

@router.get("/api/templates/{template_id}")
def get_template(template_id: int):
    result = execute_query_pg(
        "SELECT id, template_name, has_subtotals_by_default FROM templates WHERE id = %s AND is_active = TRUE",
        (template_id,)
    )
    if not result:
        raise HTTPException(404, "Template not found")
    template = result[0]
    mappings = execute_query_pg(
        """
        SELECT m.column_name, m.data_type
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = %s AND m.is_active = TRUE
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )
    template['columns'] = mappings
    return template

@router.post("/api/templates")
def create_template(template: TemplateCreate):
    existing = execute_query_pg("SELECT id FROM templates WHERE template_name = %s", (template.template_name,))
    if existing:
        raise HTTPException(400, "Template name already exists")
    if not template.selected_columns:
        raise HTTPException(400, "Select at least one column")

    resolved_rate_source = resolve_rate_source(template.template_name, template.rate_source)
    template_id = execute_write_pg(
        "INSERT INTO templates (template_name, created_by, rate_source, has_subtotals_by_default) VALUES (%s, %s, %s, %s) RETURNING id",
        (template.template_name, template.created_by, resolved_rate_source, template.has_subtotals_by_default)
    )

    for idx, col_id in enumerate(template.selected_columns):
        execute_write_pg(
            "INSERT INTO template_column_mappings (template_id, column_id, sort_order) VALUES (%s, %s, %s)",
            (template_id, col_id, idx)
        )

    log_audit(template.created_by, "Create", "Templates", f"Created template: {template.template_name}")
    return {"message": f"Template '{template.template_name}' created successfully", "template_id": template_id}

@router.put("/api/templates/{template_id}")
def update_template(template_id: int, template: TemplateUpdate):
    existing = execute_query_pg("SELECT id FROM templates WHERE id = %s AND is_active = TRUE", (template_id,))
    if not existing:
        raise HTTPException(404, "Template not found")
    duplicate = execute_query_pg("SELECT id FROM templates WHERE template_name = %s AND id != %s", (template.template_name, template_id))
    if duplicate:
        raise HTTPException(400, "Template name already exists")

    resolved_rate_source = resolve_rate_source(template.template_name, template.rate_source)
    execute_write_pg(
        "UPDATE templates SET template_name = %s, rate_source = %s, has_subtotals_by_default = %s WHERE id = %s",
        (template.template_name, resolved_rate_source, template.has_subtotals_by_default, template_id)
    )
    execute_write_pg("DELETE FROM template_column_mappings WHERE template_id = %s", (template_id,))
    for idx, col_id in enumerate(template.selected_columns):
        execute_write_pg(
            "INSERT INTO template_column_mappings (template_id, column_id, sort_order) VALUES (%s, %s, %s)",
            (template_id, col_id, idx)
        )

    log_audit("Admin", "Update", "Templates", f"Updated template: {template.template_name}")
    return {"message": f"Template '{template.template_name}' updated successfully"}

@router.delete("/api/templates/{template_id}")
def delete_template(template_id: int):
    existing = execute_query_pg(
        "SELECT template_name, is_system FROM templates WHERE id = %s AND is_active = TRUE",
        (template_id,)
    )
    if not existing:
        raise HTTPException(404, "Template not found")
    if existing[0]['is_system']:
        raise HTTPException(403, "Cannot delete a system template. It is required for the application to function.")

    # Check if any client_data rows reference this template
    dependent_rows = execute_query_pg(
        "SELECT COUNT(*) as count FROM client_data WHERE template_id = %s AND is_deleted = FALSE",
        (template_id,)
    )
    if dependent_rows and dependent_rows[0]['count'] > 0:
        raise HTTPException(
            400,
            f"Cannot delete template because it is used by {dependent_rows[0]['count']} data rows. "
            "Please delete or reassign those rows first."
        )

    execute_write_pg("UPDATE templates SET is_active = FALSE WHERE id = %s", (template_id,))
    execute_write_pg("DELETE FROM template_column_mappings WHERE template_id = %s", (template_id,))
    log_audit("Admin", "Delete", "Templates", f"Soft-deleted template: {existing[0]['template_name']}")
    return {"message": "Template deleted successfully (soft-delete)"}

# ---------- NEW ENDPOINTS FOR UPLOAD & PREVIEW ----------

# ---------- NEW ENDPOINTS FOR UPLOAD & PREVIEW ----------

@router.get("/api/templates/{template_id}/upload-columns")
def get_template_upload_columns(template_id: int):
    """Return the columns of a template with sort_order (for upload preview)."""
    result = execute_query_pg("SELECT id FROM templates WHERE id = %s AND is_active = TRUE", (template_id,))
    if not result:
        raise HTTPException(404, "Template not found")
    mappings = execute_query_pg(
        """
        SELECT m.id, m.column_name, m.data_type, m.category, p.sort_order
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = %s AND m.is_active = TRUE
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )
    return {"template_id": template_id, "columns": mappings, "total_columns": len(mappings)}

@router.post("/api/templates/{template_id}/reorder")
def reorder_template_columns(template_id: int, column_ids: List[int] = Body(...)):
    """Update sort_order for all columns of a template."""
    existing = execute_query_pg("SELECT id FROM templates WHERE id = %s AND is_active = TRUE", (template_id,))
    if not existing:
        raise HTTPException(404, "Template not found")
    for idx, col_id in enumerate(column_ids):
        execute_write_pg(
            "UPDATE template_column_mappings SET sort_order = %s WHERE template_id = %s AND column_id = %s",
            (idx, template_id, col_id)
        )
    log_audit("Admin", "Reorder", "Templates", f"Reordered columns for template ID {template_id}")
    return {"message": "Order updated successfully"}

@router.delete("/api/templates/{template_id}/columns/{column_id}")
def remove_template_column(template_id: int, column_id: int):
    """Remove a specific column mapping from a template."""
    existing = execute_query_pg("SELECT id FROM templates WHERE id = %s AND is_active = TRUE", (template_id,))
    if not existing:
        raise HTTPException(404, "Template not found")
    # RETURNING id (Postgres cutover fix): execute_write_pg only returns a value when
    # the query has RETURNING - SQLite's version relied on cursor.lastrowid after a
    # DELETE, which is meaningless (leftover from the last INSERT, unrelated to this
    # delete), so the old "if deleted == 0" check was already latent-broken. Checking
    # "is None" against a real RETURNING id makes this check correct for the first time.
    deleted_id = execute_write_pg(
        "DELETE FROM template_column_mappings WHERE template_id = %s AND column_id = %s RETURNING id",
        (template_id, column_id)
    )
    if deleted_id is None:
        raise HTTPException(404, "Column mapping not found")
    log_audit("Admin", "RemoveColumn", "Templates", f"Removed column {column_id} from template {template_id}")
    return {"message": "Column removed from template"}
@router.get("/api/templates/by-name/{template_name}")
def get_template_by_name(template_name: str):
    """
    Get template ID and details by its name. Useful for system lookups.
    """
    result = execute_query_pg(
        "SELECT id, template_name, is_system FROM templates WHERE template_name = %s AND is_active = TRUE",
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
    # uploaded_files (Postgres cutover fix): client_data.upload_id is a real FK
    # to uploaded_files(id) now (Phase 1) - the original backup never included
    # this table at all, which SQLite silently tolerated (PRAGMA foreign_keys
    # was off, so a restored client_data row could reference an upload_id that
    # no longer existed) but Postgres correctly rejects. Genuinely missing from
    # the original design, not a deliberate exclusion - added here so a restore
    # actually works.
    uploaded_files: list = []
    client_data: list = []

@router.get("/api/backup/export")
def export_backup():
    import json
    columns = execute_query_pg("SELECT * FROM master_columns ORDER BY id")
    templates = execute_query_pg("SELECT * FROM templates ORDER BY id")
    mappings = execute_query_pg("SELECT * FROM template_column_mappings ORDER BY template_id, sort_order")
    clients = execute_query_pg("SELECT * FROM clients ORDER BY id")
    uploaded_files = execute_query_pg("SELECT * FROM uploaded_files ORDER BY id")

    # Get all client_data rows. row_data is JSONB - execute_query_pg already
    # returns it as a native dict, not a JSON string, so no json.loads() needed
    # (only handle the rare case a value somehow still came back as a string).
    data_rows = execute_query_pg("SELECT * FROM client_data ORDER BY client_id, upload_id, id")
    client_data = []
    for row in data_rows:
        row_dict = dict(row)
        if isinstance(row_dict.get('row_data'), str):
            try:
                row_dict['row_data'] = json.loads(row_dict['row_data'])
            except Exception:
                pass
        client_data.append(row_dict)

    settings = execute_query_pg("SELECT key, value FROM app_settings")
    print(f"💾 Backup: {len(columns)} columns, {len(templates)} templates, {len(mappings)} mappings, {len(clients)} clients, {len(uploaded_files)} uploaded files, {len(client_data)} data rows, {len(settings)} settings")

    return {
        "columns": columns,
        "templates": templates,
        "mappings": mappings,
        "clients": clients,
        "uploaded_files": uploaded_files,
        "client_data": client_data,
        "settings": settings
    }

@router.post("/api/backup/import")
def import_backup(data: BackupData = Body(...)):
    """
    Postgres cutover notes:
    - No PRAGMA foreign_keys OFF equivalent exists in Postgres, and every FK here
      is now real/enforced (Phase 1) - the delete order below is the actual fix:
      `verifications` moves before `uploaded_files` (it has an unenforced-in-SQLite
      -but-now-real FK to uploaded_files.id with no ON DELETE action, so deleting
      uploaded_files first would raise a FK violation).
    - Every INSERT that restores an explicit `id` needs OVERRIDING SYSTEM VALUE -
      Postgres's GENERATED ALWAYS AS IDENTITY columns reject an explicit id
      otherwise. After each such restore, the identity sequence is reset to
      MAX(id) so the next normal insert (no explicit id) doesn't collide with a
      restored one - a real bug a naive port would otherwise introduce silently.
    - row_data is JSONB - written via Json(...) instead of json.dumps().
    """
    conn = get_raw_conn_pg()
    cur = conn.cursor()
    try:
        # Delete existing data in FK-dependency order (children before parents).
        cur.execute("DELETE FROM client_data")
        cur.execute("DELETE FROM verifications")
        cur.execute("DELETE FROM uploaded_files")
        cur.execute("DELETE FROM clients")
        cur.execute("DELETE FROM template_column_mappings")
        cur.execute("DELETE FROM templates")
        cur.execute("DELETE FROM app_settings")
        cur.execute("DELETE FROM master_columns")

        # Restore master_columns
        for col in data.columns:
            cur.execute(
                "INSERT INTO master_columns (id, column_name, data_type, category, is_active) "
                "OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s)",
                (col['id'], col['column_name'], col['data_type'], col.get('category', 'General'), bool(col.get('is_active', True)))
            )
        if data.columns:
            cur.execute("SELECT setval(pg_get_serial_sequence('master_columns', 'id'), (SELECT MAX(id) FROM master_columns), true)")

        # Restore templates
        for tpl in data.templates:
            cur.execute(
                "INSERT INTO templates (id, template_name, created_by, created_date, is_active) "
                "OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s)",
                (tpl['id'], tpl['template_name'], tpl.get('created_by', 'Admin'), tpl.get('created_date'), bool(tpl.get('is_active', True)))
            )
        if data.templates:
            cur.execute("SELECT setval(pg_get_serial_sequence('templates', 'id'), (SELECT MAX(id) FROM templates), true)")

        # Restore template_column_mappings
        for mp in data.mappings:
            cur.execute(
                "INSERT INTO template_column_mappings (id, template_id, column_id, sort_order) "
                "OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s)",
                (mp['id'], mp['template_id'], mp['column_id'], mp['sort_order'])
            )
        if data.mappings:
            cur.execute("SELECT setval(pg_get_serial_sequence('template_column_mappings', 'id'), (SELECT MAX(id) FROM template_column_mappings), true)")

        # Restore clients
        for client in data.clients:
            cur.execute(
                "INSERT INTO clients (id, client_code, client_name, industry, contact_person, email, phone, is_active, created_at) "
                "OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    client['id'],
                    client['client_code'],
                    client['client_name'],
                    client.get('industry'),
                    client.get('contact_person'),
                    client.get('email'),
                    client.get('phone'),
                    bool(client.get('is_active', True)),
                    client.get('created_at')
                )
            )
        if data.clients:
            cur.execute("SELECT setval(pg_get_serial_sequence('clients', 'id'), (SELECT MAX(id) FROM clients), true)")

        # Restore uploaded_files (must come before client_data - its FK target)
        for uf in data.uploaded_files:
            cur.execute(
                "INSERT INTO uploaded_files (id, file_name, file_path, file_type, record_count, status, upload_date, client_id) "
                "OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    uf['id'],
                    uf['file_name'],
                    uf['file_path'],
                    uf.get('file_type'),
                    uf.get('record_count', 0),
                    uf.get('status', 'Pending'),
                    uf.get('upload_date'),
                    uf.get('client_id'),
                )
            )
        if data.uploaded_files:
            cur.execute("SELECT setval(pg_get_serial_sequence('uploaded_files', 'id'), (SELECT MAX(id) FROM uploaded_files), true)")

        # Restore app_settings
        if hasattr(data, 'settings') and data.settings:
            for setting in data.settings:
                cur.execute(
                    "INSERT INTO app_settings (key, value) VALUES (%s, %s)",
                    (setting['key'], setting['value'])
                )

        # Restore client_data - row_data is JSONB, written via Json(...).
        for row in data.client_data:
            cur.execute(
                """
                INSERT INTO client_data (id, client_id, template_id, upload_id, row_data, extract_amount, extract_date, created_at, created_by, is_deleted, version)
                OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row['id'],
                    row['client_id'],
                    row['template_id'],
                    row['upload_id'],
                    Json(row['row_data']) if isinstance(row['row_data'], dict) else row['row_data'],
                    row.get('extract_amount'),
                    row.get('extract_date'),
                    row.get('created_at'),
                    row.get('created_by', 'Analyst'),
                    bool(row.get('is_deleted', False)),
                    row.get('version', 1)
                )
            )
        if data.client_data:
            cur.execute("SELECT setval(pg_get_serial_sequence('client_data', 'id'), (SELECT MAX(id) FROM client_data), true)")

        conn.commit()
        log_audit("Admin", "Import", "Backup", f"Imported backup: {len(data.columns)} columns, {len(data.templates)} templates, {len(data.clients)} clients, {len(data.uploaded_files)} uploaded files, {len(data.client_data)} data rows")

        return {
            "message": f"Successfully imported {len(data.columns)} columns, {len(data.templates)} templates, {len(data.mappings)} mappings, {len(data.clients)} clients, {len(data.uploaded_files)} uploaded files, and {len(data.client_data)} data rows."
        }
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Import failed: {str(e)}")
    finally:
        release_raw_conn_pg(conn)
