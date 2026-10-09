
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, RedirectResponse
from database import init_db, log_error, get_error_code_for_exception, execute_query
from fastapi.concurrency import run_in_threadpool
import os
from routers import account_control_center, audit_rules, auth, clients, dashboard, data, error_log, import_export, reconciliation, settings, storage_cleanup, templates, trial_balance, users, verification
from routers.auth import get_current_user
from datetime import datetime
import traceback
import sys
import threading

# ✅ CREATE the app instance FIRST
app = FastAPI(title="System Analyst Dashboard", version="1.0.0")
startup_state = {"postgres": "not_started"}

# Then add middleware and static files
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Fast-data-handling Phase 1 of 13: compress responses over slow connections (Sialkot)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def revalidate_static_html(request: Request, call_next):
    """Require browsers to revalidate static HTML while preserving asset caching."""
    response = await call_next(request)
    path = request.url.path.lower()
    if path.startswith("/static/") and path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.mount("/static", StaticFiles(directory="static"), name="static")

# Lightweight process health signal used by the tray controller and local launchers. It does not
# query Postgres: it distinguishes a live web process from database readiness without blocking.
@app.get("/healthz")
def healthz():
    return {"status": "ok", "postgres": startup_state["postgres"]}

# ---------- ROOT ROUTE: LOGIN → CLIENT CARDS ENTRY FLOW ----------
@app.get("/")
async def root(request: Request):
    """
    No valid session -> login.html. Valid session -> client_select.html (never straight
    to the dashboard, so the user always re-picks a client - matches the app's existing
    "never auto-resume" convention).
    """
    try:
        get_current_user(request)
        return RedirectResponse(url="/static/client_select.html")
    except HTTPException:
        return RedirectResponse(url="/static/login.html")

# ✅ THEN include routers
app.include_router(dashboard.router)
app.include_router(verification.router)
app.include_router(import_export.router)
app.include_router(templates.router)
app.include_router(clients.router)
app.include_router(data.router)
app.include_router(error_log.router)
app.include_router(settings.router)
app.include_router(audit_rules.router)
app.include_router(trial_balance.router)
app.include_router(account_control_center.router)
app.include_router(reconciliation.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(storage_cleanup.router)

# ---------- GLOBAL EXCEPTION HANDLER ----------
from fastapi import Request, status
from fastapi.responses import JSONResponse
from database import log_error, get_error_code_for_exception

def extract_error_context(tb):
    """
    Extract file name, line number, and surrounding code context from a traceback.
    """
    tb_frame = tb.tb_frame
    tb_lineno = tb.tb_lineno
    file_name = tb_frame.f_code.co_filename
    # Get source lines around the error (3 before, 3 after)
    try:
        with open(file_name, 'r') as f:
            lines = f.readlines()
        start = max(0, tb_lineno - 4)
        end = min(len(lines), tb_lineno + 3)
        context_lines = []
        for i in range(start, end):
            prefix = ">>> " if i == tb_lineno - 1 else "    "
            context_lines.append(f"{prefix}{i+1}: {lines[i].rstrip()}")
        code_context = "\n".join(context_lines)
    except:
        code_context = "Unable to read source file."
    return file_name, tb_lineno, code_context

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Global exception handler for all unhandled exceptions.
    Logs the error, returns a user‑friendly JSON response.
    """
    # Gather basic information
    user_id = "System"  # Could be extracted from session/token
    client_id = None    # Could be extracted from global context
    module = "Unknown"
    endpoint = request.url.path
    method = request.method
    status_code = 500
    
    # If it's an HTTPException, respect its status code and detail
    if isinstance(exc, HTTPException):
        status_code = exc.status_code
        error_message = str(exc.detail)
        module = "HTTP"
    else:
        error_message = str(exc) or exc.__class__.__name__
        # Try to determine module from request path
        path_parts = endpoint.split('/')
        if len(path_parts) > 2:
            module = path_parts[2].capitalize() or "Unknown"
    
    # Get stack trace details
    tb = sys.exc_info()[2]
    stack_trace = "".join(traceback.format_tb(tb))
    file_name = None
    line_number = None
    code_context = None
    if tb:
        file_name, line_number, code_context = extract_error_context(tb)
    
    # Try to match to an error code in the registry
    error_code_id, error_code, suggested_fix = get_error_code_for_exception(exc, error_message)
    
    # If no matching error code found, use "Unknown" fallback
    if not error_code:
        error_code = "ERR-UNK-001"
        suggested_fix = "Please report this error using the 'Report Error' button."
        # Try to find the unknown error code ID
        result = await run_in_threadpool(execute_query, "SELECT id FROM error_codes WHERE code = ?", (error_code,))
        if result:
            error_code_id = result[0]['id']
    
    # Determine category from exception type name
    exception_name = exc.__class__.__name__
    category_map = {
        "NameError": "Syntax",
        "ImportError": "Import",
        "SyntaxError": "Syntax",
        "IndentationError": "Syntax",
        "KeyError": "System",
        "ValueError": "Validation",
        "TypeError": "System",
        "IndexError": "System",
        "AttributeError": "System",
        "ConnectionError": "Network",
        "TimeoutError": "Network",
        "OSError": "System",
        "FileNotFoundError": "System",
        "PermissionError": "System",
        "IntegrityError": "Database",
        "OperationalError": "Database",
        "DatabaseError": "Database",
        "HTTPException": "Validation",
    }
    error_category = category_map.get(exception_name, "Unknown")
    
        # Safely get input data – skip for file uploads
    input_data = None
    if request.method in ["POST", "PUT", "PATCH"]:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                input_data = await request.json()
            except:
                pass
        # For multipart/form-data, we cannot read the body easily, so skip
    else:
        input_data = dict(request.query_params) if request.query_params else None

    # Log the error
    error_log_id = await run_in_threadpool(
        log_error,
        error_code_id=error_code_id,
        error_code=error_code,
        user_id=user_id,
        client_id=client_id,
        module=module,
        endpoint=endpoint,
        method=method,
        status_code=status_code,
        error_category=error_category,
        error_message=error_message,
        file_name=file_name,
        line_number=line_number,
        code_context=code_context,
        stack_trace=stack_trace,
        input_data=input_data,
        suggested_fix=suggested_fix
    )

    # Build user-friendly response
    # Try to fetch the user_message from error_codes
    user_message = "An unexpected error occurred. Please try again or contact support."
    if error_code_id:
        result = await run_in_threadpool(execute_query, "SELECT user_message FROM error_codes WHERE id = ?", (error_code_id,))
        if result:
            user_message = result[0]['user_message']
    
    response_data = {
        "success": False,
        "error": {
            "error_code": error_code,
            "error_id": error_log_id,
            "user_message": user_message,
            "suggested_action": suggested_fix,
            "technical_details": {
                "category": error_category,
                "file": file_name,
                "line": line_number,
                "message": error_message,
                "timestamp": datetime.now().isoformat()
            } if file_name else None
        }
    }
    
    # For HTTPException, also include the detail in the response
    if isinstance(exc, HTTPException):
        response_data["error"]["http_detail"] = exc.detail
    
    return JSONResponse(
        status_code=status_code,
        content=response_data
    )

def _initialize_postgres_in_background():
    """Initialize the optional Postgres schema without delaying web startup."""
    try:
        from database import get_pg_pool, get_raw_conn_pg, release_raw_conn_pg, init_db_postgres
        get_pg_pool()
        pg_conn = get_raw_conn_pg()
        try:
            init_db_postgres(pg_conn)
        finally:
            release_raw_conn_pg(pg_conn)
        from services.cashflow_preparation import recover_cashflow_preparation_jobs_postgres
        try:
            recovery_results = recover_cashflow_preparation_jobs_postgres()
        except Exception as recovery_error:
            recovery_results = [{"status": "deferred", "error": str(recovery_error)}]
            print(f"Cashflows preparation recovery deferred: {recovery_error}", flush=True)
        startup_state["postgres"] = "ready"
        print(
            f"Postgres (Supabase) schema initialized; Cashflows recovery results: {recovery_results}",
            flush=True,
        )
    except Exception as e:
        startup_state["postgres"] = "degraded"
        print(f"Postgres init skipped (non-fatal): {e}", flush=True)


# Startup event and run block...
@app.on_event("startup")
def startup():
    print("Starting System Analyst App...")
    init_db()

    # Supabase (Postgres) migration is best-effort and non-fatal. SQLite remains
    # the app's real database until Postgres works end-to-end, so a remote lock
    # or network timeout must never block web startup.
    if os.environ.get("DATABASE_URL"):
        startup_state["postgres"] = "initializing"
        threading.Thread(
            target=_initialize_postgres_in_background,
            name="postgres-schema-init",
            daemon=True,
        ).start()
    else:
        startup_state["postgres"] = "not_configured"
        print("DATABASE_URL not set - skipping Postgres init (SQLite-only mode)")

    print("App ready!", flush=True)

@app.on_event("shutdown")
def shutdown():
    """Return all Postgres connections cleanly when the local server stops."""
    try:
        from database import close_pg_pool
        close_pg_pool()
    except Exception as e:
        print(f"⚠️ Postgres pool cleanup skipped: {e}", flush=True)

if __name__ == "__main__":
    import uvicorn
    # Render (Phase 7 of 8) injects PORT at runtime; local dev falls back to 5000.
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
