import sqlite3

DB_NAME = "test.db"

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

def init_db():
    conn = get_db()
    
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
    
    # Templates table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_name TEXT NOT NULL UNIQUE,
            selected_columns TEXT NOT NULL,
            created_by TEXT DEFAULT 'Admin',
            created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )
    ''')
    
    # Uploaded Files table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS uploaded_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_type TEXT,
            record_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Pending',
            upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Verifications table
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
    
    # Audit Log table
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
    
    # Insert some default columns if table is empty
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
    
    conn.commit()
    conn.close()
    print("✅ Database tables created/verified")