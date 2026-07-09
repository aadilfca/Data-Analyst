import sqlite3
import time
import os

DB_NAME = "test.db"


def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def execute_query(query, params=None, retries=3, delay=0.5):
    import time

    last_exception = None
    for attempt in range(retries):
        try:
            conn = get_db()
            cursor = conn.execute(query, params or ())
            result = cursor.fetchall()
            conn.close()
            return [dict(row) for row in result]
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
                last_exception = e
                continue
            raise e
        except Exception as e:
            raise e
    if last_exception:
        raise last_exception


def execute_write(query, params=None, retries=5, delay=0.5):
    import time

    last_exception = None
    for attempt in range(retries):
        conn = None
        try:
            conn = get_db()
            cursor = conn.execute(query, params or ())
            conn.commit()
            last_id = cursor.lastrowid
            return last_id
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
                last_exception = e
                continue
            raise e
        except Exception as e:
            raise e
        finally:
            if conn:
                conn.close()
    if last_exception:
        raise last_exception


def get_raw_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def log_audit(user, action, module, details=""):
    try:
        execute_write(
            "INSERT INTO audit_log (user, action, module, details) VALUES (?, ?, ?, ?)",
            (user, action, module, details)
        )
    except Exception:
        pass


def log_data_audit(client_data_id, user, action, old_data=None, new_data=None):
    try:
        import json
        execute_write(
            "INSERT INTO data_audit_log (client_data_id, user, action, old_row_data, new_row_data) VALUES (?, ?, ?, ?, ?)",
            (
                client_data_id,
                user,
                action,
                json.dumps(old_data) if old_data else None,
                json.dumps(new_data) if new_data else None,
            ),
        )
    except Exception as e:
        print(f"⚠️ Failed to log data audit: {e}")


def create_index_with_retry(conn, sql, retries=5, delay=0.5):
    last_exception = None
    for attempt in range(retries):
        try:
            conn.execute(sql)
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
                last_exception = e
                continue
            raise
    if last_exception:
        raise last_exception


def init_db():
    conn = None
    try:
        conn = get_db()

        # Create tables and indexes
        conn.execute('''
            CREATE TABLE IF NOT EXISTS master_columns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                column_name TEXT NOT NULL UNIQUE,
                data_type TEXT NOT NULL,
                category TEXT DEFAULT 'General',
                is_active INTEGER DEFAULT 1
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_name TEXT NOT NULL UNIQUE,
                created_by TEXT DEFAULT 'Admin',
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1,
                is_system INTEGER DEFAULT 0
            )
        ''')
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
        conn.execute('''
            CREATE TABLE IF NOT EXISTS client_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                template_id INTEGER NOT NULL,
                upload_id INTEGER NOT NULL,
                row_data TEXT NOT NULL,
                extract_amount REAL,
                extract_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT DEFAULT 'Analyst',
                is_deleted INTEGER DEFAULT 0,
                version INTEGER DEFAULT 1,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE CASCADE,
                FOREIGN KEY (upload_id) REFERENCES uploaded_files(id) ON DELETE CASCADE
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_client_data_client_deleted_extract_date ON client_data (client_id, is_deleted, extract_date)")
        conn.execute('''
            CREATE TABLE IF NOT EXISTS error_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                severity TEXT NOT NULL,
                category TEXT NOT NULL,
                user_message TEXT NOT NULL,
                tech_description TEXT,
                suggested_action TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS error_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                error_code_id INTEGER,
                error_code TEXT,
                user_id TEXT DEFAULT 'System',
                client_id INTEGER,
                module TEXT,
                endpoint TEXT,
                method TEXT,
                status_code INTEGER,
                error_category TEXT,
                error_message TEXT,
                file_name TEXT,
                line_number INTEGER,
                code_context TEXT,
                stack_trace TEXT,
                input_data TEXT,
                suggested_fix TEXT,
                is_resolved BOOLEAN DEFAULT 0,
                resolution_notes TEXT,
                resolved_by TEXT,
                resolved_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_error_log_code ON error_log (error_code)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_error_log_module ON error_log (module)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_error_log_created ON error_log (created_at)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_error_log_resolved ON error_log (is_resolved)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_error_log_category ON error_log (error_category)")
        conn.execute('''
            CREATE TABLE IF NOT EXISTS verification_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                cashbook_name TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                created_by TEXT DEFAULT 'Analyst',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'In Progress',
                total_transactions INTEGER DEFAULT 0,
                verified_count INTEGER DEFAULT 0,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS verification_details (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                client_data_id INTEGER NOT NULL,
                transaction_ref TEXT,
                line_items TEXT,
                procedure_data TEXT,
                validation_status TEXT DEFAULT 'Pending',
                validation_message TEXT,
                status TEXT DEFAULT 'Pending',
                comments TEXT,
                observations TEXT,
                verified_by TEXT,
                verified_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES verification_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY (client_data_id) REFERENCES client_data(id) ON DELETE CASCADE
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_procedure_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_type TEXT UNIQUE NOT NULL,
                management_controls TEXT,
                audit_risks TEXT,
                audit_procedures TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_verification_sessions_client ON verification_sessions (client_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_verification_details_session ON verification_details (session_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_verification_details_clientdata ON verification_details (client_data_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_verification_details_status ON verification_details (status)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_verification_details_validation ON verification_details (validation_status)")

        conn.commit()
        print("✅ Database tables created/verified")
    finally:
        if conn:
            conn.close()


# Re-export the original module interface for compatibility
from database import *  # noqa: F401,F403
