import sqlite3

import os
DB_NAME = "test.db"
# Print absolute path for verification
print(f"📁 Database path: {os.path.abspath(DB_NAME)}")

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def execute_query(query, params=None):
    conn = get_db()
    cursor = conn.execute(query, params or ())
    result = cursor.fetchall()
    conn.close()
    return [dict(row) for row in result]

def execute_write(query, params=None):
    conn = get_db()
    cursor = conn.execute(query, params or ())
    conn.commit()
    last_id = cursor.lastrowid
    conn.close()
    return last_id

def get_raw_conn():
    """
    Returns a raw SQLite connection with row_factory set to sqlite3.Row.
    Used for manual transaction management (e.g., backup import).
    """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def log_audit(user, action, module, details=""):
    try:
        execute_write(
            "INSERT INTO audit_log (user, action, module, details) VALUES (?, ?, ?, ?)",
            (user, action, module, details)
        )
    except:
        pass

def log_data_audit(client_data_id, user, action, old_data=None, new_data=None):
    """
    Log a change to a specific data row.
    - client_data_id: ID of the row in client_data
    - user: username (e.g., 'Analyst', 'Admin')
    - action: 'INSERT', 'UPDATE', 'DELETE', 'RESTORE'
    - old_data: dict or None (previous row_data for UPDATE/DELETE)
    - new_data: dict or None (new row_data for INSERT/UPDATE)
    """
    try:
        import json
        execute_write(
            "INSERT INTO data_audit_log (client_data_id, user, action, old_row_data, new_row_data) VALUES (?, ?, ?, ?, ?)",
            (
                client_data_id,
                user,
                action,
                json.dumps(old_data) if old_data else None,
                json.dumps(new_data) if new_data else None
            )
        )
    except Exception as e:
        print(f"⚠️ Failed to log data audit: {e}")

def init_db():
    conn = get_db()
    
    # Drop old tables to ensure clean schema (safe for development)
    # Drop table made inactive for production safety. Uncomment during development if needed.
    # conn.execute("DROP TABLE IF EXISTS templates")
    # conn.execute("DROP TABLE IF EXISTS template_column_mappings")
    
    # Master Columns table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS master_columns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            column_name TEXT NOT NULL UNIQUE,
            data_type TEXT NOT NULL,
            category TEXT DEFAULT 'General',
            is_active INTEGER DEFAULT 1
        )
    ''')
    
    # Templates table (without selected_columns)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_name TEXT NOT NULL UNIQUE,
            created_by TEXT DEFAULT 'Admin',
            created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )
    ''')
    
    # Pivot table for template-column mappings
    conn.execute('''
        CREATE TABLE IF NOT EXISTS template_column_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL,
            column_id INTEGER NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE CASCADE,
            FOREIGN KEY (column_id) REFERENCES master_columns(id) ON DELETE CASCADE,
            UNIQUE(template_id, column_id)
        )
    ''')
    
        # Uploaded Files table (unchanged)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS uploaded_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_type TEXT,
            record_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Pending',
            upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            client_id INTEGER,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL
        )
    ''')
    
    # Verifications table (unchanged)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER,
            item_id INTEGER,
            status TEXT DEFAULT 'Pending',
            verified_by TEXT,
            verified_date TIMESTAMP,
            comments TEXT,
            FOREIGN KEY (file_id) REFERENCES uploaded_files(id)
        )
    ''')
    
        # Audit Log table (unchanged)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            action TEXT NOT NULL,
            module TEXT NOT NULL,
            details TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
        # Clients table – each client represents a separate company/entity
    conn.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_code TEXT UNIQUE NOT NULL,
            client_name TEXT NOT NULL,
            industry TEXT,
            contact_person TEXT,
            email TEXT,
            phone TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
            # Client Data table – stores imported rows as JSON with indexed extracted columns
    conn.execute('''
        CREATE TABLE IF NOT EXISTS client_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            template_id INTEGER NOT NULL,
            upload_id INTEGER NOT NULL,
            row_data TEXT NOT NULL,               -- JSON object: {"column_name": "value", ...}
            extract_amount REAL,                   -- extracted from JSON if a numeric column exists
            extract_date TEXT,                     -- ISO date string (YYYY-MM-DD) if a date column exists
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT DEFAULT 'Analyst',
            is_deleted INTEGER DEFAULT 0,
            version INTEGER DEFAULT 1,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
            FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE CASCADE,
            FOREIGN KEY (upload_id) REFERENCES uploaded_files(id) ON DELETE CASCADE
        )
    ''')
    
    # ---------- ERROR HANDLING TABLES ----------
    # Error Codes table – pre-defined catalogue of all possible errors
    conn.execute('''
        CREATE TABLE IF NOT EXISTS error_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,            -- e.g., "ERR-SYN-001", "ERR-IMP-002"
            severity TEXT NOT NULL,               -- 'Fatal', 'Error', 'Warning', 'Info'
            category TEXT NOT NULL,              -- 'Syntax', 'Import', 'Database', 'Network', 'Validation', 'System'
            user_message TEXT NOT NULL,           -- Plain English: "The system couldn't find a function called..."
            tech_description TEXT,                -- For developers: "NameError: name 'log_data_audit' is not defined"
            suggested_action TEXT,                -- Plain English: "Add 'from database import log_data_audit' at the top of the file"
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Error Log table – stores every error occurrence
    conn.execute('''
        CREATE TABLE IF NOT EXISTS error_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_code_id INTEGER,                -- References error_codes.id (if known), else NULL
            error_code TEXT,                      -- Fallback: the actual error code string
            user_id TEXT DEFAULT 'System',        -- Who was using the app
            client_id INTEGER,                    -- Which client context (if any)
            module TEXT,                          -- e.g., 'Upload', 'DataBank', 'Setup'
            endpoint TEXT,                        -- e.g., '/api/upload/import'
            method TEXT,                          -- e.g., 'POST', 'GET'
            status_code INTEGER,                  -- HTTP status code (500, 400, etc.)
            error_category TEXT,                  -- 'Syntax', 'Import', 'Database', 'Network', 'Validation', 'System', 'Unknown'
            error_message TEXT,                   -- The actual error text
            file_name TEXT,                       -- The Python file where error occurred
            line_number INTEGER,                  -- Line number in the file
            code_context TEXT,                    -- 3 lines before and after the error line
            stack_trace TEXT,                     -- Full traceback (if available)
            input_data TEXT,                      -- JSON of sanitised input (if any)
            suggested_fix TEXT,                   -- Plain English suggestion from error_codes
            is_resolved BOOLEAN DEFAULT 0,
            resolution_notes TEXT,                -- Admin notes on how it was fixed
            resolved_by TEXT,
            resolved_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create indexes for performance
    conn.execute("CREATE INDEX IF NOT EXISTS idx_error_log_code ON error_log (error_code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_error_log_module ON error_log (module)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_error_log_created ON error_log (created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_error_log_resolved ON error_log (is_resolved)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_error_log_category ON error_log (error_category)")
    
        # Insert default columns if master_columns is empty
    existing = conn.execute("SELECT COUNT(*) FROM master_columns").fetchone()[0]
    if existing == 0:
        default_columns = [
            ('Invoice Number', 'Text', 'Sales'),
            ('Invoice Date', 'Date', 'Sales'),
            ('Customer Name', 'Text', 'Customer'),
            ('Customer ID', 'Text', 'Customer'),
            ('Amount', 'Numeric', 'Financial'),
            ('Tax Amount', 'Numeric', 'Financial'),
            ('Total Amount', 'Numeric', 'Financial'),
            ('Payment Status', 'Text', 'Payment'),
            ('Payment Date', 'Date', 'Payment'),
            ('Bank Name', 'Text', 'Banking'),
            ('Account Number', 'Text', 'Banking'),
            ('Transaction Date', 'Date', 'Banking'),
            ('Description', 'Text', 'General'),
            ('Reference', 'Text', 'General')
        ]
        for col in default_columns:
            conn.execute(
                "INSERT INTO master_columns (column_name, data_type, category) VALUES (?, ?, ?)",
                (col[0], col[1], col[2])
            )
        print("✅ Inserted default columns")
    
    # --- Schema upgrades for existing databases ---
    # Add client_id column to uploaded_files if missing
    try:
        # Check if column already exists
        cur = conn.execute("PRAGMA table_info(uploaded_files)")
        columns = [row[1] for row in cur.fetchall()]
        if 'client_id' not in columns:
            conn.execute("ALTER TABLE uploaded_files ADD COLUMN client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL")
            print("✅ Added client_id column to uploaded_files")
    except Exception as e:
        print("⚠️ Could not add client_id to uploaded_files:", e)
    
    # Create indexes on client_data for performance
    conn.execute("CREATE INDEX IF NOT EXISTS idx_client_data_client ON client_data (client_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_client_data_template ON client_data (template_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_client_data_upload ON client_data (upload_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_client_data_date ON client_data (extract_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_client_data_deleted ON client_data (is_deleted)")
    print("✅ Created indexes on client_data")
    
    conn.commit()
    conn.close()
    print("✅ Database tables created/verified")
    # ---------- ERROR HANDLING HELPER FUNCTIONS ----------

def log_error(
    error_code_id=None,
    error_code=None,
    user_id="System",
    client_id=None,
    module=None,
    endpoint=None,
    method=None,
    status_code=500,
    error_category="Unknown",
    error_message="",
    file_name=None,
    line_number=None,
    code_context=None,
    stack_trace=None,
    input_data=None,
    suggested_fix=None
):
    """
    Log an error to the error_log table.
    Returns the ID of the new error log entry.
    """
    try:
        import json
        # If error_code_id is not provided, try to find it from error_code string
        if error_code_id is None and error_code:
            result = execute_query("SELECT id FROM error_codes WHERE code = ?", (error_code,))
            if result:
                error_code_id = result[0]['id']
            else:
                # Insert as 'Unknown' error code automatically? We'll handle this in the main handler.
                pass
        
        # If error_code is not provided but error_code_id is, fetch it
        if error_code is None and error_code_id:
            result = execute_query("SELECT code FROM error_codes WHERE id = ?", (error_code_id,))
            if result:
                error_code = result[0]['code']
        
        # If suggested_fix is not provided, try to fetch from error_codes
        if suggested_fix is None and error_code_id:
            result = execute_query("SELECT suggested_action FROM error_codes WHERE id = ?", (error_code_id,))
            if result:
                suggested_fix = result[0]['suggested_action']
        
        # Sanitise input_data (remove sensitive info if needed)
        input_json = None
        if input_data:
            try:
                # Convert dict/list to JSON string
                input_json = json.dumps(input_data) if isinstance(input_data, (dict, list)) else str(input_data)
                # Truncate if too long
                if len(input_json) > 5000:
                    input_json = input_json[:5000] + "... (truncated)"
            except:
                input_json = str(input_data)[:1000]
        
        error_id = execute_write(
            """
            INSERT INTO error_log (
                error_code_id, error_code, user_id, client_id, module, endpoint, method,
                status_code, error_category, error_message, file_name, line_number,
                code_context, stack_trace, input_data, suggested_fix
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                error_code_id,
                error_code,
                user_id,
                client_id,
                module,
                endpoint,
                method,
                status_code,
                error_category,
                error_message[:1000] if error_message else None,
                file_name,
                line_number,
                code_context[:1000] if code_context else None,
                stack_trace[:5000] if stack_trace else None,
                input_json,
                suggested_fix[:500] if suggested_fix else None
            )
        )
        return error_id
    except Exception as e:
        print(f"⚠️ Failed to log error: {e}")
        return None

def get_error_code_for_exception(exception, error_message=""):
    """
    Given an exception and error message, try to find a matching error_code.
    Returns (error_code_id, error_code_string, suggested_fix).
    """
    # Import the exception type name for category detection
    exception_name = exception.__class__.__name__ if exception else "Unknown"
    
    # Map exception types to error categories
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
    }
    
    category = category_map.get(exception_name, "Unknown")
    
    # Try to find a matching error code in the registry
    # First, try exact match on exception_name
    result = execute_query(
        "SELECT id, code, suggested_action FROM error_codes WHERE code LIKE ? OR tech_description LIKE ?",
        (f"%{exception_name}%", f"%{exception_name}%")
    )
    
    if result:
        return result[0]['id'], result[0]['code'], result[0]['suggested_action']
    
    # No match found – return None so a new "unknown" error code can be suggested
    return None, None, None