# ==============================================
# 🔐 AUTH ROUTER (Login / Logout)
# ==============================================

import secrets
from datetime import datetime, timedelta

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from database import execute_query, execute_write, log_error

router = APIRouter()

SESSION_COOKIE_NAME = "session_token"
SESSION_DURATION_HOURS = 10  # within the confirmed 8-12h window


class LoginRequest(BaseModel):
    username: str
    password: str


class SelectClientRequest(BaseModel):
    client_id: int


@router.post("/api/auth/login")
def login(payload: LoginRequest, response: Response):
    rows = execute_query("SELECT * FROM users WHERE username = ?", (payload.username,))
    user = rows[0] if rows else None

    if not user or not bcrypt.checkpw(payload.password.encode(), user["password_hash"].encode()):
        log_error(
            module="Auth",
            endpoint="/api/auth/login",
            method="POST",
            status_code=401,
            error_category="Auth",
            error_message="Invalid username or password",
            input_data={"username": payload.username},
        )
        raise HTTPException(401, "Invalid username or password")

    if not user["is_active"]:
        log_error(
            module="Auth",
            endpoint="/api/auth/login",
            method="POST",
            status_code=403,
            error_category="Auth",
            error_message="Login attempt on inactive account",
            input_data={"username": payload.username},
        )
        raise HTTPException(403, "Account is inactive")

    token = secrets.token_hex(32)
    expiry = datetime.now() + timedelta(hours=SESSION_DURATION_HOURS)

    execute_write(
        "INSERT INTO sessions (token, user_id, expiry) VALUES (?, ?, ?)",
        (token, user["id"], expiry.isoformat()),
    )
    execute_write(
        "UPDATE users SET last_login = ? WHERE id = ?",
        (datetime.now().isoformat(), user["id"]),
    )

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=SESSION_DURATION_HOURS * 3600,
        samesite="lax",
    )

    return {"success": True, "username": user["username"]}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        execute_write("DELETE FROM sessions WHERE token = ?", (token,))
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"success": True}


def get_current_user(request: Request):
    """FastAPI dependency: require a valid, unexpired session cookie.

    Returns the joined session+user row (user_id, username, is_active,
    expiry, selected_client_id) so a route can read it via Depends() without
    a second query. Raises 401 on missing/invalid/expired session - no
    redirect logic here, that's the frontend's job in a later phase.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Not authenticated")

    rows = execute_query(
        "SELECT s.*, u.username, u.is_active, u.role FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token = ?",
        (token,),
    )
    session = rows[0] if rows else None
    if not session:
        raise HTTPException(401, "Not authenticated")

    if datetime.fromisoformat(session["expiry"]) < datetime.now():
        raise HTTPException(401, "Session expired")

    return session


def require_admin(session=Depends(get_current_user)):
    """FastAPI dependency: require the current session's user to have role='admin'.

    Builds on get_current_user - raises 403 if the authenticated user isn't an
    admin. Use as Depends(require_admin) on any admin-only route.
    """
    if session["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    return session


def _get_client_summary(client_id):
    rows = execute_query(
        "SELECT id, client_name, client_code FROM clients WHERE id = ?", (client_id,)
    )
    return rows[0] if rows else None


@router.put("/api/auth/select-client")
def select_client(payload: SelectClientRequest, session=Depends(get_current_user)):
    client = _get_client_summary(payload.client_id)
    if not client:
        raise HTTPException(404, "Client not found")

    execute_write(
        "UPDATE sessions SET selected_client_id = ? WHERE token = ?",
        (client["id"], session["token"]),
    )

    return {
        "success": True,
        "client_id": client["id"],
        "client_name": client["client_name"],
        "client_code": client["client_code"],
    }


@router.get("/api/auth/current-client")
def current_client(session=Depends(get_current_user)):
    client_id = session["selected_client_id"]
    if not client_id:
        return {"client_id": None, "client_name": None, "client_code": None}

    client = _get_client_summary(client_id)
    if not client:
        return {"client_id": None, "client_name": None, "client_code": None}

    return {
        "client_id": client["id"],
        "client_name": client["client_name"],
        "client_code": client["client_code"],
    }
