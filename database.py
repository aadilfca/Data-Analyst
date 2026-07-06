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
    # Create all tables: column_templates, uploaded_files, verifications, audit_log, master_columns, templates
    conn.execute('''CREATE TABLE IF NOT EXISTS column_templates (...)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS uploaded_files (...)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS verifications (...)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS audit_log (...)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS master_columns (...)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS templates (...)''')
    # Insert default data if needed
    conn.commit()
    conn.close()