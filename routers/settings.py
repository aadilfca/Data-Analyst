# ==============================================
# ⚙️ APP SETTINGS ROUTER
# ==============================================

from fastapi import APIRouter, HTTPException
from database import execute_query, execute_write, log_audit
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

router = APIRouter()

class SettingUpdate(BaseModel):
    value: str

class HolidayCreate(BaseModel):
    date: str  # 'YYYY-MM-DD'
    label: str
    repeats_yearly: bool = False

class HolidayUpdate(BaseModel):
    date: str
    label: str
    repeats_yearly: bool = False

def validate_holiday_payload(date: str, label: str):
    """Raises HTTPException(400) on a malformed date or empty label."""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"Invalid date '{date}' - expected format YYYY-MM-DD")
    if not label.strip():
        raise HTTPException(400, "Label cannot be empty")

def row_to_holiday(row: dict) -> dict:
    return {
        "id": row["id"],
        "date": row["holiday_date"],
        "label": row["label"],
        "repeats_yearly": bool(row["repeats_yearly"]),
        "created_at": row["created_at"]
    }

# ---------- HOLIDAYS (Phase 1 of 6: Client Data Availability & Coverage Redesign) ----------
# Firm-wide holiday list (Sundays are separate hardcoded day-of-week logic, not stored here).
# Registered above the generic /api/settings/{key} routes below - FastAPI matches routes in
# registration order, and /api/settings/holidays would otherwise be swallowed by that
# single-path-segment catch-all (the exact bug already hit once with Error Log's /stats route).
@router.get("/api/settings/holidays")
def list_holidays():
    """List all firm-wide holidays, ordered by date."""
    rows = execute_query("SELECT * FROM holidays ORDER BY holiday_date")
    return [row_to_holiday(r) for r in rows]

@router.post("/api/settings/holidays")
def create_holiday(holiday: HolidayCreate):
    """Add a new firm-wide holiday."""
    validate_holiday_payload(holiday.date, holiday.label)
    holiday_id = execute_write(
        "INSERT INTO holidays (holiday_date, label, repeats_yearly) VALUES (?, ?, ?)",
        (holiday.date, holiday.label.strip(), int(holiday.repeats_yearly))
    )
    log_audit("Admin", "Create", "Settings", f"Added holiday: {holiday.label.strip()} ({holiday.date})")
    rows = execute_query("SELECT * FROM holidays WHERE id = ?", (holiday_id,))
    return row_to_holiday(rows[0])

@router.put("/api/settings/holidays/{holiday_id}")
def update_holiday(holiday_id: int, holiday: HolidayUpdate):
    """Update an existing firm-wide holiday."""
    existing = execute_query("SELECT id FROM holidays WHERE id = ?", (holiday_id,))
    if not existing:
        raise HTTPException(404, f"Holiday ID {holiday_id} not found")
    validate_holiday_payload(holiday.date, holiday.label)
    execute_write(
        "UPDATE holidays SET holiday_date = ?, label = ?, repeats_yearly = ? WHERE id = ?",
        (holiday.date, holiday.label.strip(), int(holiday.repeats_yearly), holiday_id)
    )
    log_audit("Admin", "Update", "Settings", f"Updated holiday ID {holiday_id}")
    rows = execute_query("SELECT * FROM holidays WHERE id = ?", (holiday_id,))
    return row_to_holiday(rows[0])

@router.delete("/api/settings/holidays/{holiday_id}")
def delete_holiday(holiday_id: int):
    """Remove a firm-wide holiday (hard delete - a plain reference list, no audit-trail requirement)."""
    existing = execute_query("SELECT label, holiday_date FROM holidays WHERE id = ?", (holiday_id,))
    if not existing:
        raise HTTPException(404, f"Holiday ID {holiday_id} not found")
    execute_write("DELETE FROM holidays WHERE id = ?", (holiday_id,))
    log_audit("Admin", "Delete", "Settings", f"Deleted holiday: {existing[0]['label']} ({existing[0]['holiday_date']})")
    return {"message": f"Holiday ID {holiday_id} deleted successfully"}

@router.get("/api/settings")
def get_settings():
    """
    Get all app settings as a key-value object.
    """
    rows = execute_query("SELECT key, value FROM app_settings")
    return {row['key']: row['value'] for row in rows}

@router.get("/api/settings/{key}")
def get_setting(key: str):
    rows = execute_query("SELECT value FROM app_settings WHERE key = ?", (key,))
    if not rows:
        raise HTTPException(404, f"Setting '{key}' not found")
    return {"key": key, "value": rows[0]['value']}

@router.post("/api/settings/{key}")
def update_setting(key: str, update: SettingUpdate):
    # Validate key exists
    rows = execute_query("SELECT id FROM app_settings WHERE key = ?", (key,))
    if not rows:
        raise HTTPException(404, f"Setting '{key}' not found")
    execute_write("UPDATE app_settings SET value = ? WHERE key = ?", (update.value, key))
    return {"message": f"Setting '{key}' updated successfully", "key": key, "value": update.value}