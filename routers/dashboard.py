from fastapi import APIRouter
from datetime import datetime
from database import execute_query

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
    return {"total_uploads": uploaded, "pending_verifications": pending, "active_modules": 2}

@router.get("/api/health")
def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@router.get("/favicon.ico")
async def favicon():
    return {"message": "No favicon available"}