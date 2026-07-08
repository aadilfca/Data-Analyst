from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from database import init_db, log_error, get_error_code_for_exception, execute_query
from routers import dashboard, verification, import_export, templates, clients, data, error_log
from datetime import datetime
import traceback
import sys

# ✅ CREATE the app instance FIRST
app = FastAPI(title="System Analyst Dashboard", version="1.0.0")

# Then add middleware and static files
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ✅ THEN include routers
app.include_router(dashboard.router)
app.include_router(verification.router)
app.include_router(import_export.router)
app.include_router(templates.router)
app.include_router(clients.router)
app.include_router(data.router)
app.include_router(error_log.router)

# ---------- GLOBAL EXCEPTION HANDLER ----------
import traceback
import sys
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
    if isinstance(exc, fastapi.HTTPException):
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
        result = execute_query("SELECT id FROM error_codes WHERE code = ?", (error_code,))
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
    
    # Log the error
    error_log_id = log_error(
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
        input_data=dict(request.query_params) if request.query_params else (await request.json() if request.method in ["POST", "PUT", "PATCH"] else None),
        suggested_fix=suggested_fix
    )
    
    # Build user-friendly response
    # Try to fetch the user_message from error_codes
    user_message = "An unexpected error occurred. Please try again or contact support."
    if error_code_id:
        result = execute_query("SELECT user_message FROM error_codes WHERE id = ?", (error_code_id,))
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
    if isinstance(exc, fastapi.HTTPException):
        response_data["error"]["http_detail"] = exc.detail
    
    return JSONResponse(
        status_code=status_code,
        content=response_data
    )

# Startup event and run block...
@app.on_event("startup")
def startup():
    print("🚀 Starting System Analyst App...")
    init_db()
    print("✅ App ready!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)