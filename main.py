# ==============================================
# 🏢 SYSTEM ANALYST APP - MAIN BACKEND
# ==============================================
# 
# 📋 FILE STRUCTURE (EASY TO SPLIT LATER):
#   1. Imports & Configuration  (lines 1-40)
#   2. Database Functions       (lines 42-80)
#   3. Startup Events           (lines 82-100)
#   4. Dashboard Endpoints      (lines 102-130)
#   5. Upload Module            (lines 132-250)
#   6. Verification Module      (lines 252-350) ← COMING SOON
#   7. Reports Module           (lines 352-420) ← FUTURE
#   8. Audit Log Module         (lines 422-480) ← FUTURE
#   9. Utility Endpoints        (lines 482-500)
#
# 👥 TEAM WORK:
#   - Each section can be assigned to one person
#   - Comments mark where to add new code
#   - Easy to split into separate files later
# ==============================================


# ==============================================
# SECTION 1: IMPORTS & CONFIGURATION
# ==============================================

from fastapi import FastAPI, HTTPException, File, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import sqlite3
import pandas as pd
import io
from typing import List, Optional
from datetime import datetime

# Create the FastAPI app
app = FastAPI(title="System Analyst Dashboard", version="1.0.0")

# ---------- CORS CONFIGURATION ----------
# Allows browser to talk to this server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # In production, restrict this
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- STATIC FILES ----------
# Serves HTML, CSS, JS from the 'static' folder
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------- DATABASE CONFIGURATION ----------
DB_NAME = "test.db"


# ==============================================
# SECTION 2: DATABASE FUNCTIONS
# ==============================================
# 📌 NOTE: All database operations go here
# TO SPLIT: Copy this section to 'database.py'

def get_db():
    """Create a database connection."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables if they don't exist.
    
    👥 TEAM: Add new tables here when creating new modules.
    🔄 This runs automatically when the app starts.
    """
    conn = get_db()
    
    # ---------- TABLE: uploaded_files ----------
    # Purpose: Track files uploaded by users
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
    # Purpose: Track verification records
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
    # Purpose: Track all user actions
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
    
    conn.commit()
    conn.close()
    print("✅ All tables created/verified successfully.")


# ---------- DATABASE HELPER FUNCTIONS ----------
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
    execute_write(
        "INSERT INTO audit_log (user, action, module, details) VALUES (?, ?, ?, ?)",
        (user, action, module, details)
    )


# ==============================================
# SECTION 3: STARTUP EVENTS
# ==============================================

@app.on_event("startup")
def startup():
    """Run when the app starts."""
    print("🚀 Starting System Analyst App...")
    init_db()
    print("✅ App ready!")


# ==============================================
# SECTION 4: DASHBOARD ENDPOINTS
# ==============================================
# 📌 NOTE: Dashboard API endpoints
# TO SPLIT: This section stays in main.py (entry point)

@app.get("/")
def root():
    """Welcome message for the API."""
    return {
        "message": "System Analyst Dashboard",
        "version": "1.0.0",
        "modules": {
            "upload": "/api/upload",
            "verification": "/api/verification",
            "reports": "/api/reports",
            "audit": "/api/audit"
        }
    }


@app.get("/api/test")
def test():
    """Simple test endpoint to verify server is running."""
    return {
        "status": "Server is running!",
        "timestamp": datetime.now().isoformat()
    }


@app.get("/api/dashboard/stats")
def get_dashboard_stats():
    """Get stats for the dashboard."""
    uploaded = execute_query("SELECT COUNT(*) as count FROM uploaded_files")[0]['count']
    verified = execute_query("SELECT COUNT(*) as count FROM verifications")[0]['count']
    pending = execute_query(
        "SELECT COUNT(*) as count FROM verifications WHERE status = 'Pending'"
    )[0]['count']
    
    return {
        "total_uploads": uploaded,
        "total_verifications": verified,
        "pending_verifications": pending,
        "active_modules": 2
    }


# ==============================================
# SECTION 5: UPLOAD MODULE
# ==============================================
# 📌 NOTE: File upload and import functionality
# TO SPLIT: Move to 'upload.py' later

# ----- MODELS -----
class FileType(str):
    TRANSACTION = "Transaction Report"
    BANK_STATEMENT = "Bank Statement"
    STORE_REPORT = "Store Report"
    TRIAL_BALANCE = "Trial Balance"

class UploadResponse(BaseModel):
    id: int
    file_name: str
    file_type: str
    status: str
    record_count: int
    message: str


@app.post("/api/upload/file")
async def upload_file(
    file: UploadFile = File(...),
    file_type: str = "Transaction Report",
    uploaded_by: str = "Analyst"
):
    """
    Upload a file to the system.
    
    Parameters:
    - file: The Excel/CSV file
    - file_type: Transaction Report, Bank Statement, Store Report, Trial Balance
    - uploaded_by: Name of the person uploading
    """
    try:
        # Read the file content
        contents = await file.read()
        file_size = len(contents)
        
        # Determine file extension
        file_ext = file.filename.split('.')[-1]
        
        # Validate file type
        if file_ext.lower() not in ['xlsx', 'xls', 'csv']:
            return {"error": "Unsupported file format. Please upload .xlsx, .xls, or .csv"}
        
        # Try to read the file with pandas to validate it
        try:
            if file_ext.lower() == 'csv':
                df = pd.read_csv(io.BytesIO(contents))
            else:
                df = pd.read_excel(io.BytesIO(contents))
        except Exception as e:
            return {"error": f"Invalid Excel/CSV file: {str(e)}"}
        
        # Save file info to database
        record_count = len(df)
        file_id = execute_write("""
            INSERT INTO uploaded_files (file_name, file_type, status, record_count, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (file.filename, file_type, "Uploaded", record_count, f"Records: {record_count}"))
        
        # Log the action
        log_audit(uploaded_by, "Upload", "Upload Module", f"Uploaded {file.filename} ({record_count} records)")
        
        return {
            "message": f"File '{file.filename}' uploaded successfully!",
            "file_id": file_id,
            "record_count": record_count,
            "file_type": file_type
        }
        
    except Exception as e:
        return {"error": f"Upload failed: {str(e)}"}


@app.get("/api/upload/files")
def get_uploaded_files():
    """Get all uploaded files."""
    return execute_query("""
        SELECT id, file_name, file_type, upload_date, status, record_count
        FROM uploaded_files
        ORDER BY upload_date DESC
    """)


@app.get("/api/upload/files/{file_id}")
def get_uploaded_file(file_id: int):
    """Get details of a specific uploaded file."""
    result = execute_query(
        "SELECT * FROM uploaded_files WHERE id = ?",
        (file_id,)
    )
    if not result:
        raise HTTPException(status_code=404, detail="File not found")
    return result[0]


@app.delete("/api/upload/files/{file_id}")
def delete_uploaded_file(file_id: int, user: str = "Analyst"):
    """Delete an uploaded file record."""
    file_info = execute_query(
        "SELECT file_name FROM uploaded_files WHERE id = ?",
        (file_id,)
    )
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")
    
    execute_write("DELETE FROM uploaded_files WHERE id = ?", (file_id,))
    log_audit(user, "Delete", "Upload Module", f"Deleted file: {file_info[0]['file_name']}")
    
    return {"message": "File record deleted successfully"}


# ==============================================
# SECTION 6: VERIFICATION MODULE
# ==============================================
# 📌 NOTE: Verification endpoints
# TO SPLIT: Move to 'verification.py' later
# 
# 👥 TEAM: Add verification endpoints here:
#   - GET /api/verification/pending
#   - GET /api/verification/verified
#   - PUT /api/verification/{id}
#   - GET /api/verification/stats


@app.get("/api/verification/stats")
def get_verification_stats():
    """Get verification statistics for the dashboard."""
    total = execute_query("SELECT COUNT(*) as count FROM verifications")[0]['count']
    pending = execute_query(
        "SELECT COUNT(*) as count FROM verifications WHERE status = 'Pending'"
    )[0]['count']
    approved = execute_query(
        "SELECT COUNT(*) as count FROM verifications WHERE status = 'Approved'"
    )[0]['count']
    rejected = execute_query(
        "SELECT COUNT(*) as count FROM verifications WHERE status = 'Rejected'"
    )[0]['count']
    
    return {
        "total": total,
        "pending": pending,
        "approved": approved,
        "rejected": rejected
    }


# ==============================================
# SECTION 7: REPORTS MODULE (Future)
# ==============================================
# 📌 NOTE: Reports endpoints (coming soon)
# TO SPLIT: Move to 'reports.py' later


# ==============================================
# SECTION 8: AUDIT LOG MODULE (Future)
# ==============================================
# 📌 NOTE: Audit log endpoints (coming soon)
# TO SPLIT: Move to 'audit.py' later

@app.get("/api/audit/log")
def get_audit_log(limit: int = 50):
    """Get recent audit log entries."""
    return execute_query("""
        SELECT * FROM audit_log
        ORDER BY timestamp DESC
        LIMIT ?
    """, (limit,))


# ==============================================
# SECTION 9: UTILITY ENDPOINTS
# ==============================================

@app.get("/favicon.ico")
async def favicon():
    """Prevent 404 error for favicon."""
    return {"message": "No favicon available"}


@app.get("/api/health")
def health_check():
    """Health check endpoint for monitoring."""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


# ==============================================
# RUN THE APP (for local development)
# ==============================================
# To run: uvicorn main:app --reload --port 5000
# For production: uvicorn main:app --host 0.0.0.0 --port 8000

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)