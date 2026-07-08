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
    
    # Data Audit Log table – tracks changes to individual data rows
    conn.execute('''
        CREATE TABLE IF NOT EXISTS data_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_data_id INTEGER NOT NULL,
            user TEXT NOT NULL,
            action TEXT NOT NULL,                 -- 'INSERT', 'UPDATE', 'DELETE', 'RESTORE'
            old_row_data TEXT,                    -- JSON of previous row (for UPDATE/DELETE)
            new_row_data TEXT,                    -- JSON of new row (for INSERT/UPDATE)
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (client_data_id) REFERENCES client_data(id) ON DELETE CASCADE
        )
    ''')
    
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