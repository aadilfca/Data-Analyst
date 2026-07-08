# ==============================================
# ⚙️ APP SETTINGS ROUTER
# ==============================================

from fastapi import APIRouter, HTTPException
from database import execute_query, execute_write
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

class SettingUpdate(BaseModel):
    value: str

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