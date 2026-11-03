# ==============================================
# ⚙️ APP SETTINGS ROUTER
# ==============================================

from fastapi import APIRouter, Depends, HTTPException
from database import execute_query, execute_write, log_audit, execute_write_pg
from pydantic import BaseModel, StrictBool
from services.upload_lifecycle import UPLOAD_BACKUP_SETTING, unbacked_uploads_allowed
from typing import Optional
from routers.auth import get_current_user, require_admin
from datetime import date, datetime, timedelta

router = APIRouter()

class SettingUpdate(BaseModel):
    value: str


class UploadBackupPolicyUpdate(BaseModel):
    allow_without_backup: StrictBool

class CashflowDefaultsUpdate(BaseModel):
    fiscal_year_start_month: int = 1
    default_period: str = "ytd"
    custom_start: Optional[str] = None
    custom_end: Optional[str] = None

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
CASHFLOW_DEFAULT_KEYS = (
    "cashflow_fiscal_year_start_month",
    "cashflow_default_period",
    "cashflow_custom_start",
    "cashflow_custom_end",
)
CASHFLOW_DEFAULT_PERIODS = {"financial_year", "ytd", "last_12_months", "custom"}

def _cashflow_setting_map() -> dict:
    rows = execute_query(
        "SELECT key, value FROM app_settings WHERE key IN (?, ?, ?, ?)",
        CASHFLOW_DEFAULT_KEYS,
    )
    return {row["key"]: row["value"] for row in rows}

def _parse_cashflow_date(value: Optional[str], field_name: str, required: bool = False) -> Optional[date]:
    if not value:
        if required:
            raise HTTPException(400, f"{field_name} is required for a custom period")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"{field_name} must use YYYY-MM-DD format")

def _month_start_after_months(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    year, month_zero_based = divmod(month_index, 12)
    return date(year, month_zero_based + 1, 1)

def _resolved_cashflow_defaults(values: dict, today: Optional[date] = None) -> dict:
    today = today or date.today()
    try:
        fiscal_month = int(values.get("cashflow_fiscal_year_start_month", "1"))
    except (TypeError, ValueError):
        fiscal_month = 1
    if fiscal_month not in range(1, 13):
        fiscal_month = 1

    period = values.get("cashflow_default_period", "ytd")
    if period not in CASHFLOW_DEFAULT_PERIODS:
        period = "ytd"

    try:
        custom_start = _parse_cashflow_date(values.get("cashflow_custom_start"), "Custom start date")
        custom_end = _parse_cashflow_date(values.get("cashflow_custom_end"), "Custom end date")
    except HTTPException:
        custom_start = custom_end = None
    if period == "custom" and (not custom_start or not custom_end or custom_start > custom_end):
        period = "ytd"

    fiscal_year_start = date(
        today.year if today.month >= fiscal_month else today.year - 1,
        fiscal_month,
        1,
    )
    if period == "financial_year":
        start = fiscal_year_start
        end = date(fiscal_year_start.year + 1, fiscal_month, 1) - timedelta(days=1)
    elif period == "last_12_months":
        start = _month_start_after_months(today, -11)
        end = today
    elif period == "custom" and custom_start and custom_end:
        start = custom_start
        end = custom_end
    else:
        start = fiscal_year_start
        end = today

    return {
        "fiscal_year_start_month": fiscal_month,
        "default_period": period,
        "custom_start": custom_start.isoformat() if custom_start else "",
        "custom_end": custom_end.isoformat() if custom_end else "",
        "resolved_period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "group_by": "month",
        },
    }

def _validate_cashflow_defaults(update: CashflowDefaultsUpdate) -> tuple[date | None, date | None]:
    if update.fiscal_year_start_month not in range(1, 13):
        raise HTTPException(400, "Fiscal-year start month must be between 1 and 12")
    if update.default_period not in CASHFLOW_DEFAULT_PERIODS:
        raise HTTPException(400, "Invalid Cashflows default period")
    custom_start = _parse_cashflow_date(update.custom_start, "Custom start date", update.default_period == "custom")

    custom_end = _parse_cashflow_date(update.custom_end, "Custom end date", update.default_period == "custom")
    if custom_start and custom_end and custom_start > custom_end:
        raise HTTPException(400, "Custom start date cannot be after custom end date")
    return custom_start, custom_end

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


@router.get("/api/settings/cashflows-defaults")
def get_cashflow_defaults(session=Depends(get_current_user)):
    """Return stored Cashflows defaults plus the current dynamically resolved range."""
    return _resolved_cashflow_defaults(_cashflow_setting_map())

@router.post("/api/settings/cashflows-defaults")
def update_cashflow_defaults(update: CashflowDefaultsUpdate, session=Depends(require_admin)):
    """Update the firm-wide Cashflows reporting defaults."""
    custom_start, custom_end = _validate_cashflow_defaults(update)
    values = {
        "cashflow_fiscal_year_start_month": str(update.fiscal_year_start_month),
        "cashflow_default_period": update.default_period,
        "cashflow_custom_start": custom_start.isoformat() if custom_start else "",
        "cashflow_custom_end": custom_end.isoformat() if custom_end else "",
    }
    for key, value in values.items():
        if execute_query("SELECT id FROM app_settings WHERE key = ?", (key,)):
            execute_write("UPDATE app_settings SET value = ? WHERE key = ?", (value, key))
        else:
            execute_write("INSERT INTO app_settings (key, value) VALUES (?, ?)", (key, value))
    log_audit("Admin", "Update", "Settings", "Updated Cashflows reporting defaults")
    return _resolved_cashflow_defaults(values)

# This policy belongs to the shared transaction database, not legacy SQLite
# settings. Restrict writes to this dedicated administrator route.
@router.get('/api/settings/upload-backups')
def get_upload_backup_policy(session=Depends(require_admin)):
    return {'allow_without_backup': unbacked_uploads_allowed()}


@router.post('/api/settings/upload-backups')
def save_upload_backup_policy(update: UploadBackupPolicyUpdate, session=Depends(require_admin)):
    execute_write_pg('''INSERT INTO app_settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value''',
        (UPLOAD_BACKUP_SETTING, 'true' if update.allow_without_backup else 'false'))
    log_audit(session.get('username', 'Admin'), 'Update', 'Settings',
              'Development uploads without backup: ' + ('allowed' if update.allow_without_backup else 'blocked'))
    return {'allow_without_backup': update.allow_without_backup}


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
    if key == UPLOAD_BACKUP_SETTING:
        raise HTTPException(403, 'Use the administrator Upload backups setting.')
    # Validate key exists
    rows = execute_query("SELECT id FROM app_settings WHERE key = ?", (key,))
    if not rows:
        raise HTTPException(404, f"Setting '{key}' not found")
    execute_write("UPDATE app_settings SET value = ? WHERE key = ?", (update.value, key))
    return {"message": f"Setting '{key}' updated successfully", "key": key, "value": update.value}
