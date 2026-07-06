# ==============================================
# 🏢 SYSTEM ANALYST APP - MAIN BACKEND
# ==============================================

import json
import os
import sqlite3
from datetime import datetime
from io import BytesIO
from typing import List

import openpyxl
import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# ---------- CORS CONFIGURATION ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- STATIC FILES ----------
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------- DATABASE ----------
DB_NAME = "test.db"


def get_db():
    """Create a database connection."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def execute_query(query, params=None):
    """Execute a SELECT query and return results as list of dicts."""
    conn = get_db()
    cursor = conn.execute(query, params or ())
    result = cursor.fetchall()
    conn.close()
    return [dict(row) for row in result]


def execute_write(query, params=None):
    """Execute a write query (INSERT, UPDATE, DELETE) and return last ID."""
    conn = get_db()
    cursor = conn.execute(query, params or ())
    conn.commit()
    last_id = cursor.lastrowid
    conn.close()
    return last_id


def log_audit(user, action, module, details=""):
    """Log an action to the audit log."""
    # Check if audit_log table exists, create if not
    try:
        execute_write("""
            INSERT INTO audit_log (user, action, module, details) 
            VALUES (?, ?, ?, ?)
        """, (user, action, module, details))
    except:
        # Table might not exist yet, ignore
        pass


def init_db():
    """Create all tables if they don't exist."""
    conn = get_db()
    
    # ---------- TABLE: column_templates ----------
    conn.execute('''
        CREATE TABLE IF NOT EXISTS column_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_name TEXT NOT NULL UNIQUE,
            columns TEXT NOT NULL,
            created_by TEXT DEFAULT 'Admin',
            created_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1
        )
    ''')
    
    # ---------- TABLE: uploaded_files ----------
    conn.execute('''
        CREATE TABLE IF NOT EXISTS uploaded_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            file_path TEXT,
            file_type TEXT NOT NULL,
            upload_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            uploaded_by TEXT DEFAULT 'Analyst',
            status TEXT DEFAULT 'Pending',
            record_count INTEGER DEFAULT 0,
            notes TEXT
        )
    ''')
    
    # ---------- TABLE: verifications ----------
    conn.execute('''
        CREATE TABLE IF NOT EXISTS verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_id TEXT,
            reference_type TEXT,
            verified_by TEXT,
            verified_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT,
            remarks TEXT,
            file_id INTEGER,
            FOREIGN KEY (file_id) REFERENCES uploaded_files(id)
        )
    ''')
    
    # ---------- TABLE: audit_log ----------
    conn.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            action TEXT,
            module TEXT,
            details TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # ---------- INSERT DEFAULT TEMPLATES ----------
    default_templates = [
        ('Transaction Report', '["Date", "Description", "Amount", "Reference"]'),
        ('Bank Statement', '["Date", "Description", "Deposit", "Withdrawal", "Balance"]'),
        ('Store Report', '["Store Code", "Product", "Qty Sold", "Revenue"]'),
        ('Trial Balance', '["Account Code", "Account Name", "Debit", "Credit"]'),
    ]
    
    for name, cols in default_templates:
        conn.execute('''
            INSERT OR IGNORE INTO column_templates (template_name, columns)
            VALUES (?, ?)
        ''', (name, cols))
    
    conn.commit()
    conn.close()
    print("✅ All tables created/verified successfully.")


@app.on_event("startup")
def startup():
    print("🚀 Starting System Analyst App...")
    init_db()
    print("✅ App ready!")


# ==============================================
# SECTION: DASHBOARD ENDPOINTS
# ==============================================

@app.get("/")
def root():
    return {
        "message": "System Analyst Dashboard",
        "version": "1.0.0",
        "modules": {
            "upload": "/api/upload",
            "verification": "/api/verification",
            "templates": "/api/templates"
        }
    }


@app.get("/api/test")
def test():
    return {
        "status": "Server is running!",
        "timestamp": datetime.now().isoformat()
    }


@app.get("/api/dashboard/stats")
def get_dashboard_stats():
    """Get stats for the dashboard."""
    try:
        uploaded = execute_query("SELECT COUNT(*) as count FROM uploaded_files")[0]['count']
    except:
        uploaded = 0
    
    try:
        pending = execute_query(
            "SELECT COUNT(*) as count FROM verifications WHERE status = 'Pending'"
        )[0]['count']
    except:
        pending = 0
    
    return {
        "total_uploads": uploaded,
        "pending_verifications": pending,
        "active_modules": 2
    }


# ==============================================
# SECTION: COLUMN TEMPLATE MANAGEMENT
# ==============================================

@app.get("/api/templates")
def get_templates():
    """Get all active column templates."""
    return execute_query("""
        SELECT id, template_name, columns, created_by, created_date, is_active
        FROM column_templates
        WHERE is_active = 1
        ORDER BY template_name
    """)


@app.get("/api/templates/{template_id}")
def get_template(template_id: int):
    """Get a specific template by ID."""
    result = execute_query(
        "SELECT * FROM column_templates WHERE id = ? AND is_active = 1",
        (template_id,)
    )
    if not result:
        raise HTTPException(status_code=404, detail="Template not found")
    return result[0]


@app.post("/api/templates")
def create_template(template_name: str, columns: List[str], created_by: str = "Admin"):
    """Create a new column template."""
    # Validate template name
    existing = execute_query(
        "SELECT id FROM column_templates WHERE template_name = ?",
        (template_name,)
    )
    if existing:
        raise HTTPException(status_code=400, detail="Template name already exists")
    
    # Validate columns
    if not columns or len(columns) < 1:
        raise HTTPException(status_code=400, detail="At least one column is required")
    
    # Save template
    columns_json = json.dumps(columns)
    template_id = execute_write("""
        INSERT INTO column_templates (template_name, columns, created_by)
        VALUES (?, ?, ?)
    """, (template_name, columns_json, created_by))
    
    log_audit(created_by, "Create", "Template Management", f"Created template: {template_name}")
    
    return {
        "message": f"Template '{template_name}' created successfully",
        "template_id": template_id,
        "columns": columns
    }


@app.put("/api/templates/{template_id}")
def update_template(template_id: int, template_name: str, columns: List[str]):
    """Update an existing template."""
    # Check if template exists
    existing = execute_query(
        "SELECT id FROM column_templates WHERE id = ?",
        (template_id,)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Template not found")
    
    # Check for duplicate name
    duplicate = execute_query(
        "SELECT id FROM column_templates WHERE template_name = ? AND id != ?",
        (template_name, template_id)
    )
    if duplicate:
        raise HTTPException(status_code=400, detail="Template name already exists")
    
    columns_json = json.dumps(columns)
    execute_write("""
        UPDATE column_templates
        SET template_name = ?, columns = ?
        WHERE id = ?
    """, (template_name, columns_json, template_id))
    
    log_audit("Admin", "Update", "Template Management", f"Updated template: {template_name}")
    
    return {
        "message": f"Template '{template_name}' updated successfully",
        "template_id": template_id
    }


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: int):
    """Soft delete a template (set is_active = 0)."""
    existing = execute_query(
        "SELECT id, template_name FROM column_templates WHERE id = ? AND is_active = 1",
        (template_id,)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Template not found")
    
    execute_write(
        "UPDATE column_templates SET is_active = 0 WHERE id = ?",
        (template_id,)
    )
    
    log_audit("Admin", "Delete", "Template Management", f"Deleted template: {existing[0]['template_name']}")
    
    return {"message": "Template deleted successfully"}


# ==============================================
# SECTION: VERIFICATION STATS
# ==============================================

@app.get("/api/verification/stats")
def get_verification_stats():
    """Get verification statistics."""
    try:
        total = execute_query("SELECT COUNT(*) as count FROM verifications")[0]['count']
        pending = execute_query(
            "SELECT COUNT(*) as count FROM verifications WHERE status = 'Pending'"
        )[0]['count']
        approved = execute_query(
            "SELECT COUNT(*) as count FROM verifications WHERE status = 'Approved'"
        )[0]['count']
    except:
        total = pending = approved = 0
    
    return {
        "total": total,
        "pending": pending,
        "approved": approved,
        "rejected": 0
    }


# ==============================================
# SECTION: FILE IMPORT & EXPORT
# ==============================================


@app.post("/api/import-excel")
async def import_excel(file: UploadFile = File(...)):
    """Import an Excel file and store its metadata."""
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Only .xlsx and .xls files are supported")
    
    try:
        # Read the uploaded file into a pandas DataFrame to validate it
        contents = await file.read()
        df = pd.read_excel(BytesIO(contents))
        
        # Save file to disk
        upload_dir = "uploads"
        os.makedirs(upload_dir, exist_ok=True)
        file_path = os.path.join(upload_dir, file.filename)
        
        # Reset cursor and save
        with open(file_path, "wb") as f:
            f.write(contents)
        
        # Record in database
        file_id = execute_write("""
            INSERT INTO uploaded_files (file_name, file_path, file_type, record_count, status)
            VALUES (?, ?, ?, ?, ?)
        """, (file.filename, file_path, 'excel', len(df), 'Pending'))
        
        log_audit("Analyst", "Upload", "Import", f"Uploaded file: {file.filename} ({len(df)} rows)")
        
        return {
            "message": f"File '{file.filename}' uploaded successfully ({len(df)} rows)",
            "file_id": file_id,
            "record_count": len(df)
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to import file: {str(e)}")


@app.get("/api/download-template")
def download_template():
    """Download an Excel template with columns based on selected template or defaults."""
    try:
        # Try to get the first active template's columns
        templates = execute_query("""
            SELECT columns FROM column_templates WHERE is_active = 1
            ORDER BY id ASC LIMIT 1
        """)
        
        if templates:
            columns = json.loads(templates[0]['columns'])
        else:
            columns = ["Date", "Description", "Amount", "Reference"]
        
        # Create an Excel file in memory
        output = BytesIO()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Template"
        
        # Write header row with styling
        for col_idx, col_name in enumerate(columns, 1):
            cell = ws.cell(row=1, column=col_idx, value=col_name)
            cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
            cell.fill = openpyxl.styles.PatternFill(start_color="1A1A2E", end_color="1A1A2E", fill_type="solid")
            cell.alignment = openpyxl.styles.Alignment(horizontal="center")
        
        # Adjust column widths
        for col_idx in range(1, len(columns) + 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = 20
        
        wb.save(output)
        output.seek(0)
        
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=import_template.xlsx"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate template: {str(e)}")


# ==============================================
# SECTION: UTILITY
# ==============================================

@app.get("/favicon.ico")
async def favicon():
    return {"message": "No favicon available"}


@app.get("/api/health")
def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


# ==============================================
# RUN THE APP
# ==============================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)

# ==============================================
# SECTION: MASTER COLUMNS MANAGEMENT
# ==============================================

@app.get("/api/columns")
def get_columns():
    """Get all active master columns."""
    return execute_query("""
        SELECT id, column_name, data_type, category, is_active
        FROM master_columns
        WHERE is_active = 1
        ORDER BY category, column_name
    """)


@app.get("/api/columns/all")
def get_all_columns():
    """Get all columns (including inactive)."""
    return execute_query("""
        SELECT id, column_name, data_type, category, is_active
        FROM master_columns
        ORDER BY category, column_name
    """)


@app.post("/api/columns")
def create_column(column_name: str, data_type: str, category: str = "General"):
    """Add a new master column."""
    existing = execute_query(
        "SELECT id FROM master_columns WHERE column_name = ?",
        (column_name,)
    )
    if existing:
        raise HTTPException(status_code=400, detail="Column name already exists")
    
    column_id = execute_write("""
        INSERT INTO master_columns (column_name, data_type, category)
        VALUES (?, ?, ?)
    """, (column_name, data_type, category))
    
    log_audit("Admin", "Create", "Columns", f"Added column: {column_name}")
    
    return {
        "message": f"Column '{column_name}' created successfully",
        "column_id": column_id
    }


@app.put("/api/columns/{column_id}")
def update_column(column_id: int, column_name: str, data_type: str, category: str, is_active: bool = 1):
    """Update an existing column."""
    existing = execute_query(
        "SELECT id FROM master_columns WHERE id = ?",
        (column_id,)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Column not found")
    
    execute_write("""
        UPDATE master_columns
        SET column_name = ?, data_type = ?, category = ?, is_active = ?
        WHERE id = ?
    """, (column_name, data_type, category, is_active, column_id))
    
    log_audit("Admin", "Update", "Columns", f"Updated column: {column_name}")
    
    return {"message": f"Column '{column_name}' updated successfully"}


@app.delete("/api/columns/{column_id}")
def delete_column(column_id: int):
    """Soft delete a column."""
    existing = execute_query(
        "SELECT column_name FROM master_columns WHERE id = ? AND is_active = 1",
        (column_id,)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Column not found")
    
    execute_write(
        "UPDATE master_columns SET is_active = 0 WHERE id = ?",
        (column_id,)
    )
    
    log_audit("Admin", "Delete", "Columns", f"Deleted column: {existing[0]['column_name']}")
    
    return {"message": "Column deleted successfully"}


@app.get("/api/columns/categories")
def get_categories():
    """Get all unique categories."""
    return execute_query("""
        SELECT DISTINCT category
        FROM master_columns
        WHERE is_active = 1
        ORDER BY category
    """)


# ==============================================
# SECTION: TEMPLATES MANAGEMENT
# ==============================================

@app.get("/api/templates")
def get_templates():
    """Get all templates with their selected columns."""
    templates = execute_query("""
        SELECT id, template_name, selected_columns, created_by, created_date, is_active
        FROM templates
        WHERE is_active = 1
        ORDER BY template_name
    """)
    
    # Parse JSON for each template
    import json
    for t in templates:
        t['selected_columns'] = json.loads(t['selected_columns'])
    
    return templates


@app.get("/api/templates/{template_id}")
def get_template(template_id: int):
    """Get a specific template by ID."""
    import json
    result = execute_query(
        "SELECT * FROM templates WHERE id = ? AND is_active = 1",
        (template_id,)
    )
    if not result:
        raise HTTPException(status_code=404, detail="Template not found")
    
    result[0]['selected_columns'] = json.loads(result[0]['selected_columns'])
    return result[0]


@app.post("/api/templates")
def create_template(template_name: str, selected_columns: List[int], created_by: str = "Admin"):
    """Create a new template."""
    import json
    
    # Check if template name exists
    existing = execute_query(
        "SELECT id FROM templates WHERE template_name = ?",
        (template_name,)
    )
    if existing:
        raise HTTPException(status_code=400, detail="Template name already exists")
    
    # Validate: at least one column selected
    if not selected_columns or len(selected_columns) < 1:
        raise HTTPException(status_code=400, detail="Select at least one column")
    
    columns_json = json.dumps(selected_columns)
    template_id = execute_write("""
        INSERT INTO templates (template_name, selected_columns, created_by)
        VALUES (?, ?, ?)
    """, (template_name, columns_json, created_by))
    
    log_audit(created_by, "Create", "Templates", f"Created template: {template_name}")
    
    return {
        "message": f"Template '{template_name}' created successfully",
        "template_id": template_id
    }


@app.put("/api/templates/{template_id}")
def update_template(template_id: int, template_name: str, selected_columns: List[int]):
    """Update an existing template."""
    import json
    
    existing = execute_query(
        "SELECT id FROM templates WHERE id = ?",
        (template_id,)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Template not found")
    
    duplicate = execute_query(
        "SELECT id FROM templates WHERE template_name = ? AND id != ?",
        (template_name, template_id)
    )
    if duplicate:
        raise HTTPException(status_code=400, detail="Template name already exists")
    
    columns_json = json.dumps(selected_columns)
    execute_write("""
        UPDATE templates
        SET template_name = ?, selected_columns = ?
        WHERE id = ?
    """, (template_name, columns_json, template_id))
    
    log_audit("Admin", "Update", "Templates", f"Updated template: {template_name}")
    
    return {"message": f"Template '{template_name}' updated successfully"}


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: int):
    """Soft delete a template."""
    existing = execute_query(
        "SELECT template_name FROM templates WHERE id = ? AND is_active = 1",
        (template_id,)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Template not found")
    
    execute_write(
        "UPDATE templates SET is_active = 0 WHERE id = ?",
        (template_id,)
    )
    
    log_audit("Admin", "Delete", "Templates", f"Deleted template: {existing[0]['template_name']}")
    
    return {"message": "Template deleted successfully"}


@app.get("/api/templates/{template_id}/columns")
def get_template_columns(template_id: int):
    """Get the column details for a template."""
    import json
    result = execute_query(
        "SELECT selected_columns FROM templates WHERE id = ? AND is_active = 1",
        (template_id,)
    )
    if not result:
        raise HTTPException(status_code=404, detail="Template not found")
    
    column_ids = json.loads(result[0]['selected_columns'])
    
    # Get column details
    if not column_ids:
        return []
    
    placeholders = ','.join('?' * len(column_ids))
    columns = execute_query(f"""
        SELECT id, column_name, data_type, category
        FROM master_columns
        WHERE id IN ({placeholders}) AND is_active = 1
    """, column_ids)
    
    return columns