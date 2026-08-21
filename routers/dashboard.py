from fastapi import APIRouter
from datetime import datetime
from database import execute_query, estimate_client_data_size

router = APIRouter()

@router.get("/")
def root():
    return {"message": "System Analyst Dashboard", "version": "1.0.0"}

@router.get("/api/test")
def test():
    return {"status": "Server is running!", "timestamp": datetime.now().isoformat()}

@router.get("/api/dashboard/stats")
def get_dashboard_stats():
    try:
        uploaded = execute_query("SELECT COUNT(*) as count FROM uploaded_files")[0]['count']
    except:
        uploaded = 0
    try:
        pending = execute_query("SELECT COUNT(*) as count FROM verifications WHERE status = 'Pending'")[0]['count']
    except:
        pending = 0
    try:
        total_records = execute_query("SELECT COUNT(*) as count FROM client_data WHERE is_deleted = 0")[0]['count']
    except:
        total_records = 0
    try:
        last_upload = execute_query("SELECT upload_date FROM uploaded_files ORDER BY upload_date DESC LIMIT 1")
        last_upload = last_upload[0]['upload_date'] if last_upload else None
    except:
        last_upload = None
    return {
        "total_uploads": uploaded,
        "pending_verifications": pending,
        "active_modules": 2,
        "total_records": total_records,
        "last_upload": last_upload
    }

@router.get("/api/dashboard/db-size")
def get_dashboard_db_size():
    """
    Database size visibility (Main Dashboard): estimated storage footprint
    aggregated across every client's template data (client_data table only -
    not error_log/app_settings/etc). See database.py's estimate_client_data_size()
    for the estimation formula/caveats.
    """
    return estimate_client_data_size()


@router.get("/api/health")
def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@router.get("/favicon.ico")
async def favicon():
    return {"message": "No favicon available"}