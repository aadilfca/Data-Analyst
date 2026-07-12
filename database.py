import sqlite3
import time
import os

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
            ('voucher_patterns', '{"CPV": ["CPV","CV"], "CRV": ["CRV","CR"], "BPV": ["BPV","BV"], "BRV": ["BRV","BR"], "JV": ["JV"]}', 'Voucher prefix patterns for cash, bank, journal vouchers'),
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

        # Schema upgrade: add change_detection_status column for re-upload change tracking (Phase 2)
        try:
            cur = conn.execute("PRAGMA table_info(client_data)")
            columns = [row[1] for row in cur.fetchall()]
            if 'change_detection_status' not in columns:
                conn.execute("ALTER TABLE client_data ADD COLUMN change_detection_status TEXT DEFAULT 'active'")
                print("✅ Added change_detection_status column to client_data")
        except Exception as e:
            print("⚠️ Could not add change_detection_status to client_data:", e)

        print("✅ Created indexes on client_data")

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