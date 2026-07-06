# ==============================================
# 🏢 SYSTEM ANALYST APP - MAIN BACKEND
# ==============================================

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import sqlite3
import json
from typing import List
from datetime import datetime

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