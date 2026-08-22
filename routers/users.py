# ==============================================
# 👤 USER MANAGEMENT ROUTER (Admin area - Manage Users)
# ==============================================

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from database import execute_query, execute_write, log_audit
from routers.auth import require_admin

router = APIRouter()

# ---------- DATA MODELS ----------
class UserCreate(BaseModel):
    username: str
    password: str
    role: Optional[str] = None

class UserUpdate(BaseModel):
    username: str
    role: Optional[str] = None

class PasswordReset(BaseModel):
    password: str

class UserResponse(BaseModel):
    id: int
    username: str
    role: Optional[str]
    is_active: bool
    last_login: Optional[str]

# ---------- HELPER ----------
def row_to_user(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row.get("role"),
        "is_active": bool(row["is_active"]),
        "last_login": row.get("last_login"),
    }

# ---------- ENDPOINTS ----------

@router.get("/api/users", response_model=List[UserResponse])
def get_users(include_inactive: bool = False, session=Depends(require_admin)):
    """
    Get all users. By default, only active users are returned.
    Set include_inactive=True to get all.
    """
    query = "SELECT * FROM users"
    if not include_inactive:
        query += " WHERE is_active = 1"
    query += " ORDER BY username"
    rows = execute_query(query)
    return [row_to_user(r) for r in rows]

@router.post("/api/users", response_model=dict)
def create_user(user: UserCreate, session=Depends(require_admin)):
    existing = execute_query("SELECT id FROM users WHERE username = ?", (user.username,))
    if existing:
        raise HTTPException(400, f"Username '{user.username}' already exists")

    password_hash = bcrypt.hashpw(user.password.encode(), bcrypt.gensalt()).decode()
    user_id = execute_write(
        "INSERT INTO users (username, password_hash, is_active, role) VALUES (?, ?, 1, ?)",
        (user.username, password_hash, user.role),
    )
    log_audit(session["username"], "Create", "Users", f"Created user: {user.username}")
    return {"message": f"User '{user.username}' created successfully", "user_id": user_id}

@router.put("/api/users/{user_id}", response_model=dict)
def update_user(user_id: int, user: UserUpdate, session=Depends(require_admin)):
    existing = execute_query("SELECT id FROM users WHERE id = ?", (user_id,))
    if not existing:
        raise HTTPException(404, "User not found")

    dup = execute_query("SELECT id FROM users WHERE username = ? AND id != ?", (user.username, user_id))
    if dup:
        raise HTTPException(400, f"Username '{user.username}' is already used by another user")

    execute_write(
        "UPDATE users SET username = ?, role = ? WHERE id = ?",
        (user.username, user.role, user_id),
    )
    log_audit(session["username"], "Update", "Users", f"Updated user ID {user_id}")
    return {"message": "User updated successfully"}

@router.post("/api/users/{user_id}/deactivate", response_model=dict)
def deactivate_user(user_id: int, session=Depends(require_admin)):
    """
    Soft-deactivate a user (set is_active = 0). No self-deactivation or
    last-admin safeguard - deliberately deferred, see CLAUDE.md/plan history.
    """
    existing = execute_query("SELECT username FROM users WHERE id = ? AND is_active = 1", (user_id,))
    if not existing:
        raise HTTPException(404, "Active user not found")
    execute_write("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    log_audit(session["username"], "Deactivate", "Users", f"Deactivated user: {existing[0]['username']} (ID: {user_id})")
    return {"message": "User deactivated successfully"}

@router.post("/api/users/{user_id}/reactivate", response_model=dict)
def reactivate_user(user_id: int, session=Depends(require_admin)):
    existing = execute_query("SELECT username FROM users WHERE id = ? AND is_active = 0", (user_id,))
    if not existing:
        raise HTTPException(404, "Inactive user not found")
    execute_write("UPDATE users SET is_active = 1 WHERE id = ?", (user_id,))
    log_audit(session["username"], "Reactivate", "Users", f"Reactivated user: {existing[0]['username']} (ID: {user_id})")
    return {"message": "User reactivated successfully"}

@router.put("/api/users/{user_id}/reset-password", response_model=dict)
def reset_password(user_id: int, payload: PasswordReset, session=Depends(require_admin)):
    """
    Admin sets a new password directly - no email/reset-token flow exists in this app.
    """
    existing = execute_query("SELECT username FROM users WHERE id = ?", (user_id,))
    if not existing:
        raise HTTPException(404, "User not found")
    password_hash = bcrypt.hashpw(payload.password.encode(), bcrypt.gensalt()).decode()
    execute_write("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
    log_audit(session["username"], "Reset Password", "Users", f"Reset password for user: {existing[0]['username']} (ID: {user_id})")
    return {"message": "Password reset successfully"}
