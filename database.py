import sqlite3
import time
import os
import shutil
from datetime import datetime

DB_NAME = "test.db"
# Print absolute path for verification
print(f"📁 Database path: {os.path.abspath(DB_NAME)}")


def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def execute_query(query, params=None, retries=3, delay=0.5):
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
    conn = sqlite3.connect(DB_NAME, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
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
            )
        )
    except Exception as e:
        print(f"⚠️ Failed to log data audit: {e}")


def get_data_audit_history(client_data_id):
    """
    Return the full revision timeline for one client_data row, oldest first,
    from data_audit_log. Generic across templates: callers diff the raw
    old/new JSON snapshots themselves via diff_row_data().
    """
    return execute_query(
        "SELECT id, user, action, old_row_data, new_row_data, changed_at "
        "FROM data_audit_log WHERE client_data_id = ? ORDER BY changed_at ASC",
        (client_data_id,)
    )


def diff_row_data(old_data, new_data):
    """
    Generic field-level diff between two row_data dicts (any template's
    column set). Returns only fields whose value differs, as
    [{"field", "old_value", "new_value"}]. A field present on only one side
    is reported with the other side's value as None (covers INSERT/DELETE).
    """
    old_data = old_data or {}
    new_data = new_data or {}
    changes = []
    for field in sorted(set(old_data.keys()) | set(new_data.keys())):
        old_value = old_data.get(field)
        new_value = new_data.get(field)
        if old_value != new_value:
            changes.append({"field": field, "old_value": old_value, "new_value": new_value})
    return changes


# 5-way category scheme (Payment/Receipt split explicitly, not derived from debit/credit
# sign at detection time) - replaced the earlier 3-way Cash/Bank/Journal scheme, which had
# no way to tell a Payment-type voucher from a Receipt-type voucher without already knowing
# the cashbook account name. See routers/verification.py's extract_cashbook_name().
VOUCHER_PREFIX_CATEGORIES = ("Cash Payment", "Cash Receipt", "Bank Payment", "Bank Receipt", "Journal")

# Default single prefix seeded per category for a brand-new client (create_client() in
# routers/clients.py) and used as the migration target for pre-existing 3-way rows.
DEFAULT_VOUCHER_PREFIXES = {
    "Cash Payment": ["CPV"],
    "Cash Receipt": ["CRV"],
    "Bank Payment": ["BPV"],
    "Bank Receipt": ["BRV"],
    "Journal": ["JV"],
}


def get_client_voucher_prefixes(client_id):
    """
    Return a client's voucher prefix configuration as
    {"Cash Payment": [...], "Cash Receipt": [...], "Bank Payment": [...],
    "Bank Receipt": [...], "Journal": [...]}. Categories with no prefixes still
    appear as an empty list (a client may legitimately have none).
    """
    rows = execute_query(
        "SELECT category, prefix FROM client_voucher_prefixes WHERE client_id = ? ORDER BY category, prefix",
        (client_id,)
    )
    result = {category: [] for category in VOUCHER_PREFIX_CATEGORIES}
    for row in rows:
        result[row["category"]].append(row["prefix"])
    return result


def save_client_voucher_prefixes(client_id, prefixes, retries=5, delay=0.5):
    """
    Replace a client's entire voucher prefix configuration with `prefixes`
    (dict of category -> list of prefix strings). Deletes existing rows for
    the client and inserts the new set in one transaction so the replace is
    atomic. Cross-category duplicate validation is the caller's responsibility
    (routers/clients.py) - this function trusts its input.
    """
    last_exception = None
    for attempt in range(retries):
        conn = None
        try:
            conn = get_db()
            conn.execute("DELETE FROM client_voucher_prefixes WHERE client_id = ?", (client_id,))
            for category in VOUCHER_PREFIX_CATEGORIES:
                for prefix in prefixes.get(category, []):
                    conn.execute(
                        "INSERT INTO client_voucher_prefixes (client_id, category, prefix) VALUES (?, ?, ?)",
                        (client_id, category, prefix)
                    )
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
                last_exception = e
                continue
            raise e
        finally:
            if conn:
                conn.close()
    if last_exception:
        raise last_exception


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
        print("🔌 Database connection opened for initialization")

        # Drop old tables to ensure clean schema (safe for development)
        # Drop table made inactive for production safety. Uncomment during development if needed.
        # conn.execute("DROP TABLE IF EXISTS templates")
        # conn.execute("DROP TABLE IF EXISTS template_column_mappings")

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
        try:
            cur = conn.execute("PRAGMA table_info(templates)")
            columns = [row[1] for row in cur.fetchall()]
            if 'is_system' not in columns:
                conn.execute("ALTER TABLE templates ADD COLUMN is_system INTEGER DEFAULT 0")
                print("✅ Added is_system column to templates")
        except Exception as e:
            print("⚠️ Could not add is_system column:", e)

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
                data_quality_flag TEXT DEFAULT NULL,
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

        # Schema upgrade: add session_type column distinguishing Cash vs Bank verification
        # sessions (prerequisite for the Bank Verification entry point/mode, Phases 7-8).
        # No CHECK constraint exists on this table, so a plain ALTER TABLE ADD COLUMN is
        # sufficient here - unlike client_voucher_prefixes' CHECK-constraint rebuild in an
        # earlier migration. DEFAULT 'cash' both backfills every pre-existing session (Bank
        # Verification didn't exist yet, so every session created so far is necessarily a
        # Cash session) and covers any future insert that omits the column.
        try:
            cur = conn.execute("PRAGMA table_info(verification_sessions)")
            columns = [row[1] for row in cur.fetchall()]
            if 'session_type' not in columns:
                conn.execute("ALTER TABLE verification_sessions ADD COLUMN session_type TEXT NOT NULL DEFAULT 'cash'")
                print("✅ Added session_type column to verification_sessions (backfilled existing rows as 'cash')")
        except Exception as e:
            print("⚠️ Could not add session_type to verification_sessions:", e)

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

        # --- SEEDING: All data seeding happens here, while connection is still open ---

        # Seed the error_codes table if it's empty
        existing = conn.execute("SELECT COUNT(*) FROM error_codes").fetchone()[0]
        if existing == 0:
            error_codes = [
                # ---- SYNTAX & IMPORT ERRORS ----
                {
                    "code": "ERR-SYN-001",
                    "severity": "Error",
                    "category": "Syntax",
                    "user_message": "The system tried to use something that doesn't exist – like a missing function or variable.",
                    "tech_description": "NameError: name '...' is not defined",
                    "suggested_action": "Check the spelling of the function/variable name. Make sure it is imported or defined before use."
                },
                {
                    "code": "ERR-SYN-002",
                    "severity": "Error",
                    "category": "Syntax",
                    "user_message": "The system encountered a syntax error – a missing bracket, comma, or a typo in the code.",
                    "tech_description": "SyntaxError: invalid syntax",
                    "suggested_action": "Check the code for missing brackets '()', '{}', '[]', or missing commas. Look at the highlighted line in the error report."
                },
                {
                    "code": "ERR-IMP-001",
                    "severity": "Error",
                    "category": "Import",
                    "user_message": "The system could not import a required module or function.",
                    "tech_description": "ImportError: No module named '...'",
                    "suggested_action": "Check if the module is installed (e.g., with `pip install`). If it's a local file, make sure the file name and path are correct."
                },
                {
                    "code": "ERR-IMP-002",
                    "severity": "Error",
                    "category": "Import",
                    "user_message": "The system tried to import something that doesn't exist in the module.",
                    "tech_description": "ImportError: cannot import name '...'",
                    "suggested_action": "Check the spelling of the function/class name. Make sure it is exported in the module."
                },
                
                # ---- DATABASE ERRORS ----
                {
                    "code": "ERR-DB-001",
                    "severity": "Fatal",
                    "category": "Database",
                    "user_message": "The system could not connect to the database. This might be because the database file is missing or corrupted.",
                    "tech_description": "sqlite3.OperationalError: unable to open database file",
                    "suggested_action": "Check that the database file exists in the correct location. Restart the server and try again."
                },
                {
                    "code": "ERR-DB-002",
                    "severity": "Error",
                    "category": "Database",
                    "user_message": "A database constraint was violated – e.g., trying to insert a duplicate unique value.",
                    "tech_description": "sqlite3.IntegrityError: UNIQUE constraint failed",
                    "suggested_action": "Check the data you are trying to insert. Make sure no duplicate values exist in unique columns (like Invoice Number)."
                },
                {
                    "code": "ERR-DB-003",
                    "severity": "Error",
                    "category": "Database",
                    "user_message": "The database operation failed because the table structure doesn't match the query.",
                    "tech_description": "sqlite3.OperationalError: no such table/column",
                    "suggested_action": "This may happen after upgrading the app. Try restarting the server and check if the database schema is up to date."
                },
                
                # ---- NETWORK & CONNECTION ERRORS ----
                {
                    "code": "ERR-NET-001",
                    "severity": "Fatal",
                    "category": "Network",
                    "user_message": "The system could not connect to the server or the server is not responding.",
                    "tech_description": "ConnectionError: Failed to connect to server",
                    "suggested_action": "Make sure the server is running. Check your internet connection and firewall settings."
                },
                {
                    "code": "ERR-NET-002",
                    "severity": "Error",
                    "category": "Network",
                    "user_message": "The server took too long to respond (timeout).",
                    "tech_description": "TimeoutError: Request timed out",
                    "suggested_action": "Try again later. If this keeps happening, check the server load and network speed."
                },
                
                # ---- VALIDATION & USER INPUT ERRORS ----
                {
                    "code": "ERR-VAL-001",
                    "severity": "Warning",
                    "category": "Validation",
                    "user_message": "The data you entered is incorrect – e.g., missing required fields, invalid format, or duplicate value.",
                    "tech_description": "ValidationError: Invalid input",
                    "suggested_action": "Check your input data. Make sure all required fields are filled and in the correct format. For duplicates, use a different unique value."
                },
                {
                    "code": "ERR-VAL-002",
                    "severity": "Warning",
                    "category": "Validation",
                    "user_message": "The file you uploaded is not supported. Only Excel files (.xlsx, .xls) are allowed.",
                    "tech_description": "HTTPException: Only .xlsx and .xls files are supported",
                    "suggested_action": "Convert your file to .xlsx or .xls format and try again."
                },
                {
                    "code": "ERR-VAL-003",
                    "severity": "Warning",
                    "category": "Validation",
                    "user_message": "A required unique column was not selected for update mode. Please select a unique column.",
                    "tech_description": "HTTPException: Unique ID column is required when mode is 'Update Existing'",
                    "suggested_action": "In the upload mapping, select a column (like Invoice Number) as the 'Unique ID'."
                },
                
                # ---- SYSTEM ERRORS ----
                {
                    "code": "ERR-SYS-001",
                    "severity": "Fatal",
                    "category": "System",
                    "user_message": "A critical system error occurred. This could be due to a bug in the code or a missing file.",
                    "tech_description": "Internal Server Error (500)",
                    "suggested_action": "Restart the server. If the problem persists, contact your developer and provide the error report."
                },
                {
                    "code": "ERR-SYS-002",
                    "severity": "Error",
                    "category": "System",
                    "user_message": "The system ran out of memory or resources.",
                    "tech_description": "MemoryError",
                    "suggested_action": "Restart the server. If you are processing large files, try splitting them into smaller chunks."
                },
                
                # ---- UNKNOWN / FALLBACK ----
                {
                    "code": "ERR-UNK-001",
                    "severity": "Error",
                    "category": "Unknown",
                    "user_message": "An unknown error occurred. We don't yet have specific information about this error.",
                    "tech_description": "Unknown error",
                    "suggested_action": "Please use the 'Report Error' button to send this error to your developer for investigation."
                }
            ]
            
            for ec in error_codes:
                conn.execute(
                    """
                    INSERT INTO error_codes (code, severity, category, user_message, tech_description, suggested_action)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (ec["code"], ec["severity"], ec["category"], ec["user_message"], ec["tech_description"], ec["suggested_action"])
                )
            print("✅ Seeded error codes (" + str(len(error_codes)) + " entries)")

        # ---------- SEED AUDIT PROCEDURE LIBRARY ----------
        existing_library = conn.execute("SELECT COUNT(*) FROM audit_procedure_library").fetchone()[0]
        if existing_library == 0:
            library_data = [
                {
                    "transaction_type": "Material Purchased",
                    "management_controls": "Authorized Purchase demand; Approved Purchase order; Signed Gate pass; Inspection note; Store receiving; Vendor bill",
                    "audit_risks": "Unauthorized purchases; Inflated quantities; Unapproved vendors; Missing GRN",
                    "audit_procedures": "Verify purchase demand authorization; Check PO approval; Inspect gate pass; Review inspection note; Match GRN with PO; Compare vendor bill with PO/GRN; Reconcile quantities and amounts"
                },
                {
                    "transaction_type": "Repair & Maintenance (In-house)",
                    "management_controls": "Authorized Purchase demand; Signature of relevant supervisor on bill; Vendor bill",
                    "audit_risks": "Unapproved repairs; Overcharging; Duplicate bills",
                    "audit_procedures": "Check purchase demand authorization; Verify supervisor signature on bill; Review vendor bill for reasonableness; Compare with maintenance log if available"
                },
                {
                    "transaction_type": "Fuel Expense",
                    "management_controls": "Vehicle log; Fuel receipts; Mileage records",
                    "audit_risks": "Unexplained fuel consumption; Theft; Unauthorized vehicles",
                    "audit_procedures": "Reconcile fuel receipts with vehicle log; Check mileage against fuel consumption; Verify authorized vehicles"
                },
                {
                    "transaction_type": "Short Term Advance against Salary",
                    "management_controls": "Approved advance request; Employee acknowledgment; Payroll deduction schedule",
                    "audit_risks": "Unauthorized advances; Non-recovery; Duplicate advances",
                    "audit_procedures": "Verify approved advance request; Check employee acknowledgment; Review deduction schedule; Confirm with payroll records"
                },
                {
                    "transaction_type": "Payment to Contractors",
                    "management_controls": "Signed contract; Work completion certificate; Tax deduction certificate; Vendor bill",
                    "audit_risks": "Fake contractors; Unauthorized work; Overpayment; Non-deduction of tax",
                    "audit_procedures": "Review contract; Inspect work completion certificate; Verify tax deduction; Match vendor bill with contract"
                },
                {
                    "transaction_type": "Fixed Asset Purchased",
                    "management_controls": "Capital budget approval; Purchase order; Gate pass; Inspection note; Asset tagging",
                    "audit_risks": "Unauthorized purchase; Misclassification as expense; Incorrect valuation; Missing asset",
                    "audit_procedures": "Check capital budget approval; Verify PO; Inspect gate pass; Review inspection note; Ensure asset tagged and entered in fixed asset register"
                },
                {
                    "transaction_type": "Kitchen Expenses",
                    "management_controls": "Daily consumption report; Purchase invoices; Inventory count",
                    "audit_risks": "Theft; Over-purchasing; Unauthorized items; Missing invoices",
                    "audit_procedures": "Reconcile purchase invoices with consumption report; Verify inventory count; Check for approved purchase; Review daily consumption logs"
                }
            ]
            for lib in library_data:
                conn.execute(
                    """
                    INSERT INTO audit_procedure_library (transaction_type, management_controls, audit_risks, audit_procedures)
                    VALUES (?, ?, ?, ?)
                    """,
                    (lib["transaction_type"], lib["management_controls"], lib["audit_risks"], lib["audit_procedures"])
                )
            print("✅ Seeded audit procedure library (" + str(len(library_data)) + " entries)")

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

        # ---------- CREATE SYSTEM TEMPLATE: Transaction Report ----------
        # Check if template exists
        tpl = conn.execute("SELECT id FROM templates WHERE template_name = 'Transaction Report'").fetchone()
        if not tpl:
            # Create the template
            conn.execute(
                "INSERT INTO templates (template_name, created_by, is_system) VALUES (?, ?, ?)",
                ("Transaction Report", "System", 1)
            )
            tpl_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # Define required columns and their IDs (create if missing)
            required_columns = [
                ("Voucher ID", "Text", "Transaction"),
                ("Transaction Date", "Date", "Transaction"),
                ("Account Detail", "Text", "Accounting"),
                ("Account Name", "Text", "Accounting"),
                ("Voucher Detail", "Text", "Transaction"),
                ("Debit Amount", "Numeric", "Financial"),
                ("Credit Amount", "Numeric", "Financial"),
            ]
            col_ids = []
            for col_name, data_type, category in required_columns:
                # Check if column exists
                col = conn.execute(
                    "SELECT id FROM master_columns WHERE column_name = ?", (col_name,)
                ).fetchone()
                if col:
                    col_id = col[0]
                else:
                    # Insert new column
                    conn.execute(
                        "INSERT INTO master_columns (column_name, data_type, category) VALUES (?, ?, ?)",
                        (col_name, data_type, category)
                    )
                    col_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    print(f"✅ Created column: {col_name}")
                col_ids.append(col_id)
            # Map columns to template
            for idx, col_id in enumerate(col_ids):
                conn.execute(
                    "INSERT INTO template_column_mappings (template_id, column_id, sort_order) VALUES (?, ?, ?)",
                    (tpl_id, col_id, idx)
                )
            print("✅ Created system template: Transaction Report")
        else:
            # Ensure is_system flag is set
            conn.execute(
                "UPDATE templates SET is_system = 1 WHERE id = ?",
                (tpl[0],)
            )
            print("✅ Transaction Report template already exists (system flag ensured)")

        # ---------- APP SETTINGS ----------
        conn.execute('''
            CREATE TABLE IF NOT EXISTS app_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Seed default settings
        default_settings = [
            ('date_format', 'DD/MM/YYYY', 'Global date format for display'),
            ('amount_grouping', '000', 'Amount grouping: 000 (thousands) or Lacs'),
            ('decimal_precision', '0', 'Default decimal places for amounts (0, 2, 4)'),
            ('default_date_range', 'Current Month', 'Default date range for dashboards'),
            ('materiality_threshold', '50000', 'Threshold for flagging high-value items'),
        ]
        for key, val, desc in default_settings:
            cur = conn.execute("SELECT id FROM app_settings WHERE key = ?", (key,))
            if not cur.fetchone():
                conn.execute(
                    "INSERT INTO app_settings (key, value, description) VALUES (?, ?, ?)",
                    (key, val, desc)
                )
        print("✅ Seeded app settings")

        # --- Schema upgrades for existing databases ---
        # Add client_id column to uploaded_files if missing
        try:
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
        # Composite index for faster date-range queries on a specific client
        conn.execute("CREATE INDEX IF NOT EXISTS idx_client_data_client_date ON client_data (client_id, extract_date)")

        # ---------- DATA AUDIT LOG TABLE (for change detection old_row_data) ----------
        conn.execute('''
            CREATE TABLE IF NOT EXISTS data_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_data_id INTEGER NOT NULL,
                user TEXT DEFAULT 'System',
                action TEXT NOT NULL,
                old_row_data TEXT,
                new_row_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_data_id) REFERENCES client_data(id) ON DELETE CASCADE
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_data_audit_log_clientdata ON data_audit_log (client_data_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_data_audit_log_action ON data_audit_log (action)")
        print("✅ Created data_audit_log table for change tracking")

        # Schema upgrade: add document_group_id column for grouping rows by Document ID (Phase 1)
        try:
            cur = conn.execute("PRAGMA table_info(client_data)")
            columns = [row[1] for row in cur.fetchall()]
            if 'document_group_id' not in columns:
                conn.execute("ALTER TABLE client_data ADD COLUMN document_group_id TEXT DEFAULT NULL")
                print("✅ Added document_group_id column to client_data")
        except Exception as e:
            print("⚠️ Could not add document_group_id to client_data:", e)

        # Schema upgrade: add change_detection_status column for re-upload change tracking (Phase 2)
        try:
            cur = conn.execute("PRAGMA table_info(client_data)")
            columns = [row[1] for row in cur.fetchall()]
            if 'change_detection_status' not in columns:
                conn.execute("ALTER TABLE client_data ADD COLUMN change_detection_status TEXT DEFAULT 'active'")
                print("✅ Added change_detection_status column to client_data")
        except Exception as e:
            print("⚠️ Could not add change_detection_status to client_data:", e)

        # Schema upgrade: add data_quality_flag column (Phase 4 of the row-level
        # blank-field-rejection requirement) -- marks a row 'incomplete' when it was
        # force-imported with a blank Document ID/Amount/Date via the Exceptions override.
        try:
            cur = conn.execute("PRAGMA table_info(client_data)")
            columns = [row[1] for row in cur.fetchall()]
            if 'data_quality_flag' not in columns:
                conn.execute("ALTER TABLE client_data ADD COLUMN data_quality_flag TEXT DEFAULT NULL")
                print("✅ Added data_quality_flag column to client_data")
        except Exception as e:
            print("⚠️ Could not add data_quality_flag to client_data:", e)

        print("✅ Created indexes on client_data")

        # ---------- CLIENT VOUCHER PREFIXES (per-client voucher-type detection) ----------
        # Stores each client's voucher-prefix list per category. Categories are 5-way,
        # splitting Payment vs Receipt explicitly (Cash Payment/Cash Receipt/Bank Payment/
        # Bank Receipt/Journal) - see VOUCHER_PREFIX_CATEGORIES above. This replaced an
        # earlier 3-way Cash/Bank/Journal scheme that derived Payment vs Receipt from
        # debit/credit sign at detection time; that approach couldn't distinguish the two
        # before a cashbook name was already known (see routers/verification.py's
        # extract_cashbook_name()), so the distinction is now stored directly instead.
        #
        # SQLite can't ALTER a CHECK constraint in place, so upgrading a database that still
        # has old-scheme (Cash/Bank/Journal) rows requires a full table rebuild: rename ->
        # recreate with the new CHECK -> copy rows across (remapping old category+prefix to
        # the correct new category) -> drop the renamed old table. This block detects that
        # case and only runs the rebuild when old-scheme rows are actually found, so a fresh
        # or already-migrated database is a cheap no-op on every subsequent startup.
        old_category_rows = []
        try:
            old_category_rows = conn.execute(
                "SELECT id, client_id, category, prefix FROM client_voucher_prefixes "
                "WHERE category NOT IN (?, ?, ?, ?, ?)",
                VOUCHER_PREFIX_CATEGORIES
            ).fetchall()
        except sqlite3.OperationalError:
            # Table doesn't exist yet on a brand-new database - nothing to migrate.
            old_category_rows = []

        if old_category_rows:
            print(f"⚠️ Found {len(old_category_rows)} client_voucher_prefixes row(s) on the old "
                  f"Cash/Bank/Journal scheme - migrating to the 5-way Payment/Receipt scheme.")

            # Defense in depth: this app has previously had a real incident where touching
            # live client data went wrong (see CLAUDE.md's "known pre-existing bug" note) -
            # a cheap file-copy backup here makes this migration trivially reversible.
            backup_path = f"{DB_NAME}.bak-premigrate-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            shutil.copy2(DB_NAME, backup_path)
            print(f"✅ Backed up database to {backup_path} before migrating client_voucher_prefixes")

            before_counts = dict(conn.execute(
                "SELECT client_id, COUNT(*) FROM client_voucher_prefixes GROUP BY client_id"
            ).fetchall())

            conn.execute("DROP TABLE IF EXISTS client_voucher_prefixes_old")
            conn.execute("ALTER TABLE client_voucher_prefixes RENAME TO client_voucher_prefixes_old")
            conn.execute('''
                CREATE TABLE client_voucher_prefixes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER NOT NULL,
                    category TEXT NOT NULL CHECK (category IN
                        ('Cash Payment', 'Cash Receipt', 'Bank Payment', 'Bank Receipt', 'Journal')),
                    prefix TEXT NOT NULL COLLATE NOCASE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                    UNIQUE(client_id, category, prefix)
                )
            ''')

            def _remapped_category(old_category, prefix):
                if old_category == 'Journal':
                    return 'Journal'
                # Old scheme's implicit convention (mirrors the pre-per-client hardcoded
                # {"CPV": ["CPV","CV"], "CRV": ["CRV","CR"], ...} bucketing): a prefix
                # containing 'R' is Receipt-type (CRV/CR/BRV/BR), anything else defaults to
                # Payment-type (CPV/CV/BPV/BV).
                is_receipt = 'R' in prefix.strip().upper()
                if old_category == 'Cash':
                    return 'Cash Receipt' if is_receipt else 'Cash Payment'
                if old_category == 'Bank':
                    return 'Bank Receipt' if is_receipt else 'Bank Payment'
                return None  # unrecognised old category - drop rather than guess wrong

            all_old_rows = conn.execute(
                "SELECT client_id, category, prefix FROM client_voucher_prefixes_old"
            ).fetchall()
            migrated = 0
            skipped = 0
            for row in all_old_rows:
                new_category = _remapped_category(row[1], row[2])
                if new_category is None:
                    skipped += 1
                    continue
                try:
                    conn.execute(
                        "INSERT INTO client_voucher_prefixes (client_id, category, prefix) VALUES (?, ?, ?)",
                        (row[0], new_category, row[2])
                    )
                    migrated += 1
                except sqlite3.IntegrityError:
                    # Same prefix already inserted under this new category for this client
                    # (e.g. a duplicate old row) - safe to skip, not a data loss.
                    skipped += 1

            conn.execute("DROP TABLE client_voucher_prefixes_old")
            create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_client_voucher_prefixes_client ON client_voucher_prefixes (client_id)")

            after_counts = dict(conn.execute(
                "SELECT client_id, COUNT(*) FROM client_voucher_prefixes GROUP BY client_id"
            ).fetchall())
            print(f"✅ Migrated client_voucher_prefixes: {migrated} row(s) remapped, {skipped} skipped. "
                  f"Per-client row counts before: {before_counts}, after: {after_counts}")
        else:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS client_voucher_prefixes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER NOT NULL,
                    category TEXT NOT NULL CHECK (category IN
                        ('Cash Payment', 'Cash Receipt', 'Bank Payment', 'Bank Receipt', 'Journal')),
                    prefix TEXT NOT NULL COLLATE NOCASE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                    UNIQUE(client_id, category, prefix)
                )
            ''')
            create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_client_voucher_prefixes_client ON client_voucher_prefixes (client_id)")
        print("✅ Created client_voucher_prefixes table")

        # Seed clients with zero voucher-prefix rows using the 5-way defaults (covers clients
        # created before this migration ran; create_client() in routers/clients.py also seeds
        # new clients directly at creation time, so this is a defensive backfill, not the
        # primary seeding path for new clients going forward).
        existing_clients = conn.execute("SELECT id FROM clients").fetchall()
        for client_row in existing_clients:
            cid = client_row[0]
            has_rows = conn.execute(
                "SELECT 1 FROM client_voucher_prefixes WHERE client_id = ? LIMIT 1", (cid,)
            ).fetchone()
            if not has_rows:
                for category, prefixes in DEFAULT_VOUCHER_PREFIXES.items():
                    for prefix in prefixes:
                        conn.execute(
                            "INSERT INTO client_voucher_prefixes (client_id, category, prefix) VALUES (?, ?, ?)",
                            (cid, category, prefix)
                        )
        print("✅ Seeded default voucher prefixes for clients with none configured")

        # ---------- SAVED COLUMN MAPPINGS (Phase A1 of 3: save column mapping feature) ----------
        # Remembers the Excel-column -> template-column mapping a user confirmed for a given
        # client+template, so the Upload page can auto-apply it next time instead of the user
        # re-mapping from scratch. Keyed by client_id+template_id (not template_id alone) since
        # different clients may export the same template with differently worded Excel headers.
        # One row per mapped template column (not one JSON blob) - mirrors how
        # template_column_mappings itself is stored, and keeps a future single-column
        # edit/delete (Phase A3) simple. Only created/updated when explicitly saved by the user
        # (Phase A2) - no automatic saving happens in this phase.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS saved_column_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                template_id INTEGER NOT NULL,
                template_column_id INTEGER NOT NULL,
                excel_column TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE CASCADE,
                UNIQUE(client_id, template_id, template_column_id)
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_saved_column_mappings_client_template ON saved_column_mappings (client_id, template_id)")
        print("✅ Created saved_column_mappings table")

        # ---------- AUDIT RULES MODULE (Phase 1 of 9: schema foundation) ----------
        # Three reusable entities assembled by reference (not duplicated per-category):
        # Documents, Cross-checks, and Categories. A Category is the thing a voucher will
        # eventually reference (wired up in Phase 9) - this phase is schema only, no
        # voucher/client_data changes, no API, no UI. This is intended to eventually
        # replace the hardcoded audit_procedure_library lookup used by the verification
        # module (migration confirmed for Phase 9, not a parallel system).
        # All tables use the audit_rule_ prefix to stay distinct from the pre-existing,
        # unrelated audit_log/audit_procedure_library tables.
        # Phase 2 addition: audit_rule_documents gained is_active (soft-delete/restore for
        # the Documents CRUD API, matching the clients table convention) - added directly to
        # the CREATE TABLE below rather than an ALTER TABLE upgrade block, since this table
        # has no real pre-existing data to migrate (unlike verification_sessions.session_type).

        # Documents: name, one KB note, and (via child tables below) a list of
        # cross-checkable fields and a list of authorisation roles.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rule_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kb_note TEXT,
                template_id INTEGER,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (template_id) REFERENCES templates(id)
            )
        ''')
        # Schema upgrade: a database that already ran the Phase 1 CREATE TABLE (with no
        # is_active column) needs this column added explicitly - CREATE TABLE IF NOT EXISTS
        # above is a no-op once the table exists, same reasoning as verification_sessions'
        # session_type upgrade block.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_documents)")
            columns = [row[1] for row in cur.fetchall()]
            if 'is_active' not in columns:
                conn.execute("ALTER TABLE audit_rule_documents ADD COLUMN is_active INTEGER DEFAULT 1")
                print("✅ Added is_active column to audit_rule_documents")
        except Exception as e:
            print("⚠️ Could not add is_active to audit_rule_documents:", e)
        # Phase 9 addition: template_id links a Document to the Data Bank template its
        # supporting evidence should be searched in (verification.js's Supporting column
        # needs this to call openProcedureModal() for any Category's Documents, not just
        # the old hardcoded five). Same upgrade-block treatment as is_active above.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_documents)")
            columns = [row[1] for row in cur.fetchall()]
            if 'template_id' not in columns:
                conn.execute("ALTER TABLE audit_rule_documents ADD COLUMN template_id INTEGER")
                print("✅ Added template_id column to audit_rule_documents")
        except Exception as e:
            print("⚠️ Could not add template_id to audit_rule_documents:", e)

        # Fields on a Document available for cross-checks (e.g. Quantity, Amount, Date) -
        # stored here so they're a controlled per-document list, not free-typed elsewhere.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rule_document_fields (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id) ON DELETE CASCADE,
                UNIQUE(document_id, field_name)
            )
        ''')

        # Authorisation roles required on a Document (e.g. "Approved by") - modelled as a
        # list from the start so more roles can be added later with no schema change.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rule_document_auth_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                role_name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id) ON DELETE CASCADE,
                UNIQUE(document_id, role_name)
            )
        ''')

        # Cross-checks: name, the field being compared, and a comparison type from a fixed
        # set. compare_field is free text (not FK'd to audit_rule_document_fields) since
        # different documents attached to the same cross-check may use differently-named
        # but equivalent fields.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rule_cross_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                compare_field TEXT NOT NULL,
                comparison_type TEXT NOT NULL CHECK (comparison_type IN
                    ('All equal', 'Sequential increasing', 'Sequential decreasing', 'Within tolerance %')),
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Schema upgrade: same reasoning as audit_rule_documents' is_active upgrade above -
        # a database that already ran the Phase 1 CREATE TABLE (with no is_active column)
        # needs it added explicitly, since CREATE TABLE IF NOT EXISTS is a no-op once the
        # table exists. Added in Phase 3 to support Cross-checks soft-delete/restore.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_cross_checks)")
            columns = [row[1] for row in cur.fetchall()]
            if 'is_active' not in columns:
                conn.execute("ALTER TABLE audit_rule_cross_checks ADD COLUMN is_active INTEGER DEFAULT 1")
                print("✅ Added is_active column to audit_rule_cross_checks")
        except Exception as e:
            print("⚠️ Could not add is_active to audit_rule_cross_checks:", e)

        # Ordered list of Documents a Cross-check applies to.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rule_cross_check_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cross_check_id INTEGER NOT NULL,
                document_id INTEGER NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (cross_check_id) REFERENCES audit_rule_cross_checks(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id) ON DELETE CASCADE,
                UNIQUE(cross_check_id, document_id)
            )
        ''')

        # Categories: name, one KB overview note, and (via junction tables below) the
        # Documents it uses and the Cross-checks it applies. This is the entity a voucher
        # will eventually reference (Phase 9).
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rule_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kb_overview TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Schema upgrade: same reasoning as audit_rule_documents/audit_rule_cross_checks'
        # is_active upgrades above - a database that already ran the Phase 1 CREATE TABLE
        # (with no is_active column) needs it added explicitly. Added in Phase 4 to support
        # Categories soft-delete/restore - the third and final audit_rule_* entity table to
        # need this treatment.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_categories)")
            columns = [row[1] for row in cur.fetchall()]
            if 'is_active' not in columns:
                conn.execute("ALTER TABLE audit_rule_categories ADD COLUMN is_active INTEGER DEFAULT 1")
                print("✅ Added is_active column to audit_rule_categories")
        except Exception as e:
            print("⚠️ Could not add is_active to audit_rule_categories:", e)

        # Documents a Category uses.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rule_category_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                document_id INTEGER NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (category_id) REFERENCES audit_rule_categories(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id) ON DELETE CASCADE,
                UNIQUE(category_id, document_id)
            )
        ''')

        # Cross-checks a Category applies.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rule_category_cross_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                cross_check_id INTEGER NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (category_id) REFERENCES audit_rule_categories(id) ON DELETE CASCADE,
                FOREIGN KEY (cross_check_id) REFERENCES audit_rule_cross_checks(id) ON DELETE CASCADE,
                UNIQUE(category_id, cross_check_id)
            )
        ''')

        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_audit_rule_document_fields_document ON audit_rule_document_fields (document_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_audit_rule_document_auth_roles_document ON audit_rule_document_auth_roles (document_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_audit_rule_cross_check_documents_crosscheck ON audit_rule_cross_check_documents (cross_check_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_audit_rule_cross_check_documents_document ON audit_rule_cross_check_documents (document_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_audit_rule_category_documents_category ON audit_rule_category_documents (category_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_audit_rule_category_documents_document ON audit_rule_category_documents (document_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_audit_rule_category_cross_checks_category ON audit_rule_category_cross_checks (category_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_audit_rule_category_cross_checks_crosscheck ON audit_rule_category_cross_checks (cross_check_id)")
        print("✅ Created Audit Rules module tables (Phase 1 of 9: schema foundation)")

        # ---------- AUDIT RULES MODULE (Phase 9 of 9: migrate legacy transaction types) ----------
        # One-time, idempotent: copies the 7 hardcoded transaction types (that verification.js's
        # Type dropdown used to list, matched against audit_procedure_library) into real
        # audit_rule_categories rows, so nothing is lost when the dropdown switches to sourcing
        # live Categories instead. Only kb_overview text is migrated - Documents/Cross-checks are
        # deliberately left unattached (no structured document list existed in the old data) for
        # manual setup via the Audit Rules UI. Checked by name before insert, so reruns are a
        # no-op - same idempotent-migration convention as the client_voucher_prefixes rebuild.
        legacy_category_kb = {
            "Material Purchased": (
                "Management Controls: Authorized Purchase demand; Approved Purchase order; "
                "Signed Gate pass; Inspection note; Store receiving; Vendor bill\n\n"
                "Audit Risks: Unauthorized purchases; Inflated quantities; Unapproved vendors; Missing GRN\n\n"
                "Audit Procedures: Verify purchase demand authorization; Check PO approval; Inspect gate pass; "
                "Review inspection note; Match GRN with PO; Compare vendor bill with PO/GRN; Reconcile quantities and amounts"
            ),
            "Repair & Maintenance (In-house)": (
                "Management Controls: Authorized Purchase demand; Signature of relevant supervisor on bill; Vendor bill\n\n"
                "Audit Risks: Unapproved repairs; Overcharging; Duplicate bills\n\n"
                "Audit Procedures: Check purchase demand authorization; Verify supervisor signature on bill; "
                "Review vendor bill for reasonableness; Compare with maintenance log if available"
            ),
            "Fuel Expense": (
                "Management Controls: Vehicle log; Fuel receipts; Mileage records\n\n"
                "Audit Risks: Unexplained fuel consumption; Theft; Unauthorized vehicles\n\n"
                "Audit Procedures: Reconcile fuel receipts with vehicle log; Check mileage against fuel consumption; "
                "Verify authorized vehicles"
            ),
            "Short Term Advance against Salary": (
                "Management Controls: Approved advance request; Employee acknowledgment; Payroll deduction schedule\n\n"
                "Audit Risks: Unauthorized advances; Non-recovery; Duplicate advances\n\n"
                "Audit Procedures: Verify approved advance request; Check employee acknowledgment; "
                "Review deduction schedule; Confirm with payroll records"
            ),
            "Payment to Contractors": (
                "Management Controls: Signed contract; Work completion certificate; Tax deduction certificate; Vendor bill\n\n"
                "Audit Risks: Fake contractors; Unauthorized work; Overpayment; Non-deduction of tax\n\n"
                "Audit Procedures: Review contract; Inspect work completion certificate; Verify tax deduction; "
                "Match vendor bill with contract"
            ),
            "Fixed Asset Purchased": (
                "Management Controls: Capital budget approval; Purchase order; Gate pass; Inspection note; Asset tagging\n\n"
                "Audit Risks: Unauthorized purchase; Misclassification as expense; Incorrect valuation; Missing asset\n\n"
                "Audit Procedures: Check capital budget approval; Verify PO; Inspect gate pass; Review inspection note; "
                "Ensure asset tagged and entered in fixed asset register"
            ),
            "Kitchen Expenses": (
                "Management Controls: Daily consumption report; Purchase invoices; Inventory count\n\n"
                "Audit Risks: Theft; Over-purchasing; Unauthorized items; Missing invoices\n\n"
                "Audit Procedures: Reconcile purchase invoices with consumption report; Verify inventory count; "
                "Check for approved purchase; Review daily consumption logs"
            ),
        }
        migrated_count = 0
        for cat_name, kb_overview in legacy_category_kb.items():
            existing = conn.execute("SELECT id FROM audit_rule_categories WHERE name = ?", (cat_name,)).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO audit_rule_categories (name, kb_overview) VALUES (?, ?)",
                    (cat_name, kb_overview)
                )
                migrated_count += 1
        if migrated_count:
            print(f"✅ Migrated {migrated_count} legacy transaction type(s) into Audit Rules Categories")

        # ---------- HOLIDAYS (Phase 1 of 6: Client Data Availability & Coverage Redesign) ----------
        # Firm-wide (not per-client) holiday list, so later phases can exclude these dates -
        # alongside Sundays, which stay separate hardcoded day-of-week logic, not stored here -
        # from "missing data" detection in the Client Data module. repeats_yearly distinguishes
        # fixed-date holidays (e.g. Pakistan Day - recurs every year) from lunar/one-off holidays
        # (e.g. Eid, Muharram - re-added as a new dated row each year). Brand-new table, no prior
        # data to reconcile, so a plain CREATE TABLE IF NOT EXISTS is sufficient (no ALTER TABLE
        # upgrade block needed, unlike the is_active-column gaps hit repeatedly in Audit Rules).
        conn.execute('''
            CREATE TABLE IF NOT EXISTS holidays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                holiday_date TEXT NOT NULL,
                label TEXT NOT NULL,
                repeats_yearly INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_holidays_date ON holidays (holiday_date)")
        print("✅ Created holidays table")

        # ---------- TRIAL BALANCE MODULE (Phase 1 of 5: schema foundation) ----------
        # New, dedicated feature - deliberately not an extension of import_export.py/
        # templates.py (those handle generic template-mapped imports; TB needs
        # hierarchy-aware parent/leaf classification derived from the account code
        # structure itself, not client-specific text markers like "+"/"»"). Schema only
        # this phase - no parsing/classification logic, API, or UI yet (Phases 2-5).
        # Brand-new tables, no prior data to reconcile, so plain CREATE TABLE IF NOT
        # EXISTS is sufficient (no ALTER TABLE upgrade block needed, unlike the
        # is_active-column gaps hit repeatedly in Audit Rules).

        # One row per upload event. Re-uploads for the same client/period intentionally
        # do NOT overwrite a prior upload - each becomes its own row, so history isn't
        # silently lost.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS trial_balance_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                as_of_date TEXT NOT NULL,
                file_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'parsed', 'error')),
                row_count INTEGER DEFAULT 0,
                notes TEXT,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                period_start TEXT,
                period_end TEXT,
                period_type TEXT,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            )
        ''')

        # One row per account line, per upload. classification (parent vs leaf) is
        # derived from account_code's dash-delimited hierarchy at parse time (Phase 2) -
        # source_account_type is a cross-check only, never authoritative for
        # classification. manually_overridden is added now (unused until Phase 5) so the
        # later review/correction screen has a column to write to without a further
        # schema change. No redundant client_id column - reached via upload_id, matching
        # how verification_details reaches client_id via its session rather than a
        # direct column.
        # debit_amount/credit_amount are the original (now-legacy) single amount pair -
        # superseded by the standard 8-field mapping fix below (period_dr/period_cr is
        # their direct successor), but kept in the CREATE TABLE rather than dropped,
        # since this table already has live data and this app's convention is
        # additive-only schema changes, never a column drop/rename.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS trial_balance_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id INTEGER NOT NULL,
                account_code TEXT NOT NULL,
                account_name TEXT,
                parent_account_code TEXT,
                level INTEGER NOT NULL DEFAULT 1,
                classification TEXT NOT NULL CHECK (classification IN ('parent', 'leaf')),
                debit_amount REAL,
                credit_amount REAL,
                opening_dr REAL,
                opening_cr REAL,
                period_dr REAL,
                period_cr REAL,
                closing_dr REAL,
                closing_cr REAL,
                source_account_type TEXT,
                manually_overridden INTEGER NOT NULL DEFAULT 0,
                category_id INTEGER,
                sort_order INTEGER NOT NULL DEFAULT 0,
                raw_row_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (upload_id) REFERENCES trial_balance_uploads(id) ON DELETE CASCADE,
                UNIQUE(upload_id, account_code)
            )
        ''')

        # Schema upgrade: Trial Balance mapping fix - expand the single debit_amount/
        # credit_amount pair into the standard 3-pair Dr/Cr shape (Opening, Period,
        # Closing). This table already existed with data from the completed 5-phase
        # build, so a plain CREATE TABLE IF NOT EXISTS column addition alone would be a
        # no-op there - same ALTER TABLE ADD COLUMN upgrade-block pattern already used
        # for verification_sessions.session_type/client_data.data_quality_flag.
        try:
            cur = conn.execute("PRAGMA table_info(trial_balance_accounts)")
            existing_columns = [row[1] for row in cur.fetchall()]
            for new_col in ('opening_dr', 'opening_cr', 'period_dr', 'period_cr', 'closing_dr', 'closing_cr'):
                if new_col not in existing_columns:
                    conn.execute(f"ALTER TABLE trial_balance_accounts ADD COLUMN {new_col} REAL")
            print("✅ Added opening_dr/opening_cr/period_dr/period_cr/closing_dr/closing_cr columns to trial_balance_accounts")
        except Exception as e:
            print("⚠️ Could not add new amount columns to trial_balance_accounts:", e)

        # Schema upgrade: Audit Rules "Assign Categories", Section 2 Phase 3 - add a
        # nullable category_id link from trial_balance_accounts to
        # audit_rule_categories.id, so an auditor can later (Phase 4/5) assign a
        # Category to a TB account with parent-to-child cascade. Nullable because most
        # accounts won't have a category assigned immediately after upload - assignment
        # happens later via the Phase 5 UI. This table already existed with data, so
        # same ALTER TABLE ADD COLUMN upgrade-block pattern as the two blocks above.
        # No FOREIGN KEY clause (in the CREATE TABLE above or here) - this app never
        # enables PRAGMA foreign_keys, and no other soft-reference column on this table
        # (e.g. parent_account_code) declares one either, so this is a soft reference in
        # practice, consistent with the rest of the app; a Category deleted while
        # accounts still reference it is not handled at the DB layer. No index added
        # this phase - would be premature until a real filter-by-category query exists.
        try:
            cur = conn.execute("PRAGMA table_info(trial_balance_accounts)")
            existing_columns = [row[1] for row in cur.fetchall()]
            if 'category_id' not in existing_columns:
                conn.execute("ALTER TABLE trial_balance_accounts ADD COLUMN category_id INTEGER")
            print("✅ Added category_id column to trial_balance_accounts")
        except Exception as e:
            print("⚠️ Could not add category_id column to trial_balance_accounts:", e)

        # Schema upgrade: Periodic dates & overwrite warning, Section 2 Phase 4 - add
        # period tracking to trial_balance_uploads so an upload records the actual
        # period it covers (monthly/quarterly/half_yearly/annual), not just a single
        # as_of_date. This table already existed with data from the completed 5-phase
        # build, so a plain CREATE TABLE IF NOT EXISTS column addition alone would be a
        # no-op there - same ALTER TABLE ADD COLUMN upgrade-block pattern used just
        # above for trial_balance_accounts. as_of_date is left in place, untouched -
        # not renamed/removed, and no backfill of period_start/period_end/period_type
        # is done for pre-existing rows (schema only this phase; validation/overwrite-
        # detection is a later phase). Overlapping periods for the same client are
        # explicitly allowed - no UNIQUE/exclusion constraint on the period range.
        try:
            cur = conn.execute("PRAGMA table_info(trial_balance_uploads)")
            existing_columns = [row[1] for row in cur.fetchall()]
            for new_col in ('period_start', 'period_end', 'period_type'):
                if new_col not in existing_columns:
                    conn.execute(f"ALTER TABLE trial_balance_uploads ADD COLUMN {new_col} TEXT")
            print("✅ Added period_start/period_end/period_type columns to trial_balance_uploads")
        except Exception as e:
            print("⚠️ Could not add period tracking columns to trial_balance_uploads:", e)

        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_trial_balance_uploads_client ON trial_balance_uploads (client_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_trial_balance_accounts_upload ON trial_balance_accounts (upload_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_trial_balance_accounts_parent ON trial_balance_accounts (upload_id, parent_account_code)")
        print("✅ Created Trial Balance module tables (Phase 1 of 5: schema foundation)")

        conn.commit()
        print("✅ Database initialization complete")

    except Exception as e:
        print(f"❌ Database initialization failed: {e}")
        raise  # Re-raise so FastAPI startup knows something went wrong
    finally:
        if conn:
            conn.close()
            print("🔌 Database connection closed")
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