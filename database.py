import sqlite3
import time
import contextvars
from contextlib import contextmanager
import os
import sys
import shutil
import bcrypt
from datetime import datetime

import psycopg2
import psycopg2.extras
import psycopg2.pool
from dotenv import load_dotenv

load_dotenv()

DB_NAME = "test.db"
_POSTGRES_SCHEMA_READY = False
# Print absolute path for verification
# Windows may use cp1252 for console output; startup diagnostics contain Unicode icons.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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


def execute_batch(conn, query, params_list, chunk_size=500):
    """
    Run `query` once per params tuple via executemany(), in chunks of
    chunk_size, committing after each chunk. A chunk that raises rolls back
    only that chunk (already-committed chunks stay committed) before
    re-raising. Caller owns the connection's lifecycle (open/close).
    """
    for i in range(0, len(params_list), chunk_size):
        chunk = params_list[i:i + chunk_size]
        try:
            conn.executemany(query, chunk)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def execute_batch_insert_with_ids(conn, query, params_list, table, chunk_size=500):
    """
    INSERT-only variant of execute_batch() for an INTEGER PRIMARY KEY
    AUTOINCREMENT table: returns the assigned rowids in the same order as
    params_list. Relies on SQLite allocating rowids strictly sequentially
    within one connection's transaction (no other writer can interleave
    while this connection holds the write lock) - a chunk's ids are exactly
    (start_id+1 .. start_id+len(chunk)), where start_id is MAX(id) read
    just before the chunk's executemany.
    """
    ids = []
    for i in range(0, len(params_list), chunk_size):
        chunk = params_list[i:i + chunk_size]
        try:
            start_id = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
            conn.executemany(query, chunk)
            ids.extend(range(start_id + 1, start_id + 1 + len(chunk)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return ids


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


# Database size visibility (dashboard/per-client/per-template): this is a fixed
# per-row byte allowance covering client_data's other columns (id, client_id,
# template_id, upload_id, extract_amount, extract_date, created_at, created_by,
# is_deleted, version, data_quality_flag) plus SQLite's own per-row overhead.
# It is an ESTIMATE only, not exact disk usage.
ROW_OVERHEAD_BYTES = 100


def estimate_client_data_size(client_id: int = None, template_id: int = None) -> dict:
    """
    Estimate the storage footprint of client_data rows: SUM(LENGTH(row_data))
    (the dominant, JSON-encoded contributor) + COUNT(*) * ROW_OVERHEAD_BYTES,
    filtered to is_deleted = 0 (same convention as get_dashboard_stats()'s
    total_records count). Optionally scoped to one client and/or one template
    - omitting both aggregates across every client's template data.
    Computed live on every call, no caching - deferred until/if it becomes a
    performance issue with data growth (per the confirmed requirement).
    """
    where_clauses = ["is_deleted = 0"]
    params = []
    if client_id is not None:
        where_clauses.append("client_id = ?")
        params.append(client_id)
    if template_id is not None:
        where_clauses.append("template_id = ?")
        params.append(template_id)
    where_sql = " AND ".join(where_clauses)

    result = execute_query(
        f"SELECT COALESCE(SUM(LENGTH(row_data)), 0) as data_bytes, COUNT(*) as row_count "
        f"FROM client_data WHERE {where_sql}",
        params
    )
    data_bytes = result[0]["data_bytes"] if result else 0
    row_count = result[0]["row_count"] if result else 0
    total_bytes = data_bytes + (row_count * ROW_OVERHEAD_BYTES)

    return {
        "total_bytes": total_bytes,
        "row_count": row_count,
        "formatted": format_bytes(total_bytes)
    }


def format_bytes(n: int) -> str:
    """Human-readable byte formatting (B/KB/MB/GB), 1 decimal place above B."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


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
                is_active INTEGER DEFAULT 1,
                notes TEXT
            )
        ''')
        try:
            cur = conn.execute("PRAGMA table_info(master_columns)")
            columns = [row[1] for row in cur.fetchall()]
            if 'notes' not in columns:
                conn.execute("ALTER TABLE master_columns ADD COLUMN notes TEXT")
                print("✅ Added notes column to master_columns")
        except Exception as e:
            print("⚠️ Could not add notes column:", e)

        conn.execute('''
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_name TEXT NOT NULL UNIQUE,
                created_by TEXT DEFAULT 'Admin',
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1,
                is_system INTEGER DEFAULT 0,
                has_subtotals_by_default INTEGER DEFAULT 0
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
        try:
            cur = conn.execute("PRAGMA table_info(templates)")
            columns = [row[1] for row in cur.fetchall()]
            if 'rate_source' not in columns:
                conn.execute("ALTER TABLE templates ADD COLUMN rate_source TEXT")
                print("✅ Added rate_source column to templates")
        except Exception as e:
            print("⚠️ Could not add rate_source column:", e)
        try:
            cur = conn.execute(
                """
                UPDATE templates
                SET rate_source = 'auto'
                WHERE rate_source IS NULL
                  AND LOWER(TRIM(template_name)) IN ('purchase order', 'store receiving', 'vendor bill')
                """
            )
            if cur.rowcount > 0:
                print(f"✅ Migrated {cur.rowcount} template(s) with NULL rate_source to 'auto'")
        except Exception as e:
            print("⚠️ Could not migrate templates.rate_source to 'auto':", e)
        try:
            cur = conn.execute("PRAGMA table_info(templates)")
            columns = [row[1] for row in cur.fetchall()]
            if 'has_subtotals_by_default' not in columns:
                conn.execute("ALTER TABLE templates ADD COLUMN has_subtotals_by_default INTEGER DEFAULT 0")
                print("✅ Added has_subtotals_by_default column to templates")
        except Exception as e:
            print("⚠️ Could not add has_subtotals_by_default column:", e)

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
                rulebook_version INTEGER,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS verification_presence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                verification_session_id INTEGER NOT NULL,
                session_token TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                last_heartbeat TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                UNIQUE(verification_session_id, session_token)
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

        # Rulebook snapshot version (Audit Rules redesign Phase 3): nullable so historical
        # sessions remain compatible and can continue using the existing live-read behavior.
        try:
            cur = conn.execute("PRAGMA table_info(verification_sessions)")
            columns = [row[1] for row in cur.fetchall()]
            if 'rulebook_version' not in columns:
                conn.execute("ALTER TABLE verification_sessions ADD COLUMN rulebook_version INTEGER")
                print("✅ Added rulebook_version column to verification_sessions")
        except Exception as e:
            print("⚠️ Could not add rulebook_version to verification_sessions:", e)

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

        # Schema upgrade (Validation Rules, Phase 1 of 7): first ALTER-TABLE upgrade block for
        # this table (it already carries live data, so these can't just join the CREATE TABLE
        # above). cleared_status is the persisted/cached Cleared-vs-Query status - recalculated
        # on save by a later phase's status engine, not computed live on every read.
        # matrix_version_applied records audit_rule_categories.matrix_version as it stood when
        # this line was last classified/cleared, so a later matrix edit doesn't silently
        # reclassify an already-Cleared voucher. Both nullable - existing rows start unset
        # until the status engine (a later phase) first computes them.
        try:
            cur = conn.execute("PRAGMA table_info(verification_details)")
            columns = [row[1] for row in cur.fetchall()]
            if 'cleared_status' not in columns:
                conn.execute("ALTER TABLE verification_details ADD COLUMN cleared_status TEXT")
                print("✅ Added cleared_status column to verification_details")
            if 'matrix_version_applied' not in columns:
                conn.execute("ALTER TABLE verification_details ADD COLUMN matrix_version_applied INTEGER")
                print("✅ Added matrix_version_applied column to verification_details")
        except Exception as e:
            print("⚠️ Could not add cleared_status/matrix_version_applied to verification_details:", e)

        # category_id_applied/category_name_applied (Freeze Category on Voucher Lines):
        # freezes the Category actually in effect the first time this line is
        # successfully cleared/classified, the same "don't retroactively reinterpret
        # already-completed work" rationale as matrix_version_applied above, applied to
        # the category itself rather than just its matrix version - so a later Trial
        # Balance re-upload/deletion or category reassignment only affects lines not yet
        # audited. Both nullable - existing rows start unset until the status engine
        # first freezes them.
        try:
            cur = conn.execute("PRAGMA table_info(verification_details)")
            columns = [row[1] for row in cur.fetchall()]
            if 'category_id_applied' not in columns:
                conn.execute("ALTER TABLE verification_details ADD COLUMN category_id_applied INTEGER")
                print("✅ Added category_id_applied column to verification_details")
            if 'category_name_applied' not in columns:
                conn.execute("ALTER TABLE verification_details ADD COLUMN category_name_applied TEXT")
                print("✅ Added category_name_applied column to verification_details")
        except Exception as e:
            print("⚠️ Could not add category_id_applied/category_name_applied to verification_details:", e)

        # Schema upgrade (session date-range reconciliation): lets an active verification
        # session's voucher list be re-resolved against a new Place 2 date range without
        # deleting anything. range_status mirrors voucher_line_documents.status ('active'/
        # 'unassigned') + unassigned_at exactly - 'active' rows are the session's current
        # voucher set, 'excluded' rows are a voucher that fell out of the reconciled range,
        # left intact (not deleted) so the row's data survives if the range is widened back.
        # No CHECK constraint (can't add one via post-hoc ALTER) - validated in Python only.
        try:
            cur = conn.execute("PRAGMA table_info(verification_details)")
            columns = [row[1] for row in cur.fetchall()]
            if 'range_status' not in columns:
                conn.execute("ALTER TABLE verification_details ADD COLUMN range_status TEXT NOT NULL DEFAULT 'active'")
                print("✅ Added range_status column to verification_details (backfilled existing rows as 'active')")
            if 'excluded_at' not in columns:
                conn.execute("ALTER TABLE verification_details ADD COLUMN excluded_at TIMESTAMP")
                print("✅ Added excluded_at column to verification_details")
        except Exception as e:
            print("⚠️ Could not add range_status/excluded_at to verification_details:", e)

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
                "INSERT INTO templates (template_name, created_by, is_system, has_subtotals_by_default) VALUES (?, ?, ?, ?)",
                ("Transaction Report", "System", 1, 1)
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
            # Ensure is_system flag and default-subtotals flag are set
            conn.execute(
                "UPDATE templates SET is_system = 1, has_subtotals_by_default = 1 WHERE id = ?",
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
            ('rate_precision', '0', 'Decimal places for derived/imported Rate column (0, 2, 4)'),
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

        # Durable version for stable Audit Rules/Rulebook configuration. The singleton row
        # survives process restarts, unlike the in-memory cache version maintained by the router.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rules_config_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                config_version INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute(
            "INSERT OR IGNORE INTO audit_rules_config_state (id, config_version) VALUES (1, 1)"
        )
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rules_config_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_version INTEGER NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                entity_name TEXT,
                action TEXT NOT NULL,
                changed_by TEXT DEFAULT 'System',
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_audit_rules_config_changes_version ON audit_rules_config_changes (config_version)")

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

        # Schema upgrade: add rate_source column (Phase 7 of the Rate-derivation
        # requirement) -- per-row audit trail of how THIS row's Rate was actually
        # obtained ('imported'/'derived'), copied from the template's rate_source
        # at the time of import. Distinct from templates.rate_source (the setting,
        # which can change later) -- this is the historical record for the row.
        # Recorded even when Rate itself is null (Qty=0/blank case): it's
        # provenance of the attempt, not the result.
        try:
            cur = conn.execute("PRAGMA table_info(client_data)")
            columns = [row[1] for row in cur.fetchall()]
            if 'rate_source' not in columns:
                conn.execute("ALTER TABLE client_data ADD COLUMN rate_source TEXT DEFAULT NULL")
                print("✅ Added rate_source column to client_data")
        except Exception as e:
            print("⚠️ Could not add rate_source to client_data:", e)

        # Schema cleanup: drop source_grn_row_id column (Populate Physical Bill from
        # GRN/ERP Vendor Bill requirement, Phase 4 of 4, FINAL) -- this column belonged to
        # the "Create Vendor Bill from GRN" feature, which has been fully superseded and
        # removed (frontend in Phase 3, backend resolver/endpoints alongside this same
        # cleanup) in favour of the Physical Bill entry work. Confirmed 0 rows had this
        # column set on real data before dropping. DROP COLUMN is a plain, direct ALTER
        # here (no CHECK constraint/index depends on this column, so the rename/recreate/
        # copy/drop table-rebuild pattern used elsewhere in this file for CHECK-constraint
        # changes isn't needed) -- supported by SQLite 3.35+, well below what this project
        # ships with. Idempotent: a database that never had the column (a fresh install, or
        # one already migrated by this block) is a safe no-op.
        try:
            cur = conn.execute("PRAGMA table_info(client_data)")
            columns = [row[1] for row in cur.fetchall()]
            if 'source_grn_row_id' in columns:
                conn.execute("ALTER TABLE client_data DROP COLUMN source_grn_row_id")
                print("✅ Dropped source_grn_row_id column from client_data")
        except Exception as e:
            print("⚠️ Could not drop source_grn_row_id from client_data:", e)

        print("✅ Created indexes on client_data")

        # ---------- RATE COLUMN DEFINITION (Phase 8 of the Rate-derivation requirement) ----------
        # Phase 6 already writes derived/imported Rate values into row_data at import time for
        # Purchase Order/Store Receiving/Vendor Bill, and Phase 7 already stamps client_data.rate_source
        # per row -- but none of the 3 templates ever declared "Rate" as a real column, so nothing
        # (Data Bank preview, template column lists, etc. -- all name/sort_order driven off
        # master_columns/template_column_mappings) has anywhere to render it. This block is idempotent
        # (safe to run on every startup) and retroactively attaches a shared "Rate" master_columns row
        # to the 3 already-live templates, positioned immediately after Quantity (before Amount) --
        # matching create_template()/update_template()'s own delete-then-reinsert/contiguous-renumber
        # sort_order convention (routers/templates.py), just done directly here since those endpoints
        # only handle a caller-submitted full column list, not a targeted retroactive insert. Does NOT
        # touch templates.rate_source (left NULL, a separate manual per-template toggle from Phase 5/5a)
        # or any existing client_data/row_data -- column definition only.
        try:
            rate_col = conn.execute(
                "SELECT id FROM master_columns WHERE column_name = 'Rate'"
            ).fetchone()
            if rate_col:
                rate_col_id = rate_col[0]
            else:
                amount_col = conn.execute(
                    "SELECT category FROM master_columns WHERE column_name = 'Amount'"
                ).fetchone()
                rate_category = amount_col[0] if amount_col and amount_col[0] else 'General'
                conn.execute(
                    "INSERT INTO master_columns (column_name, data_type, category) VALUES (?, ?, ?)",
                    ('Rate', 'Numeric', rate_category)
                )
                rate_col_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                print("✅ Created column: Rate")

            rate_eligible_template_names = ('purchase order', 'store receiving', 'vendor bill')
            eligible_templates = conn.execute(
                "SELECT id, template_name FROM templates WHERE LOWER(template_name) IN (?, ?, ?)",
                rate_eligible_template_names
            ).fetchall()
            for tpl_id, tpl_name in eligible_templates:
                already_wired = conn.execute(
                    "SELECT 1 FROM template_column_mappings WHERE template_id = ? AND column_id = ?",
                    (tpl_id, rate_col_id)
                ).fetchone()
                if already_wired:
                    print(f"✅ Rate column already present on template: {tpl_name}")
                    continue

                existing_mappings = conn.execute(
                    "SELECT column_id FROM template_column_mappings WHERE template_id = ? ORDER BY sort_order ASC",
                    (tpl_id,)
                ).fetchall()
                existing_col_ids = [row[0] for row in existing_mappings]

                qty_col = conn.execute(
                    "SELECT id FROM master_columns WHERE column_name = 'Quantity'"
                ).fetchone()
                if not qty_col or qty_col[0] not in existing_col_ids:
                    print(f"⚠️ Skipped attaching Rate to template '{tpl_name}': no Quantity column found")
                    continue
                qty_col_id = qty_col[0]

                qty_index = existing_col_ids.index(qty_col_id)
                new_col_ids = existing_col_ids[:qty_index + 1] + [rate_col_id] + existing_col_ids[qty_index + 1:]

                conn.execute("DELETE FROM template_column_mappings WHERE template_id = ?", (tpl_id,))
                for idx, col_id in enumerate(new_col_ids):
                    conn.execute(
                        "INSERT INTO template_column_mappings (template_id, column_id, sort_order) VALUES (?, ?, ?)",
                        (tpl_id, col_id, idx)
                    )
                print(f"✅ Attached Rate column to template '{tpl_name}' (positioned after Quantity)")
        except Exception as e:
            print("⚠️ Could not attach Rate column to eligible templates:", e)

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

        # ---------- UPLOAD FREQUENCY (Overdue Uploads Phase C1 of 4: schema) ----------
        # How often a given client is expected to upload data for a given template
        # (Daily/Weekly/Monthly) - feeds the later Overdue Uploads detection (Phase D1).
        # Keyed by client_id+template_id (mirrors saved_column_mappings above), since
        # different clients can have different frequencies for the same template. No row
        # is created until a user explicitly sets a non-default value - absence of a row
        # means Daily, resolved in application code (routers/data.py), not seeded here.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS client_template_upload_frequency (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                template_id INTEGER NOT NULL,
                frequency TEXT NOT NULL DEFAULT 'Daily' CHECK (frequency IN ('Daily', 'Weekly', 'Monthly')),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE CASCADE,
                UNIQUE(client_id, template_id)
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_upload_frequency_client_template ON client_template_upload_frequency (client_id, template_id)")
        print("✅ Created client_template_upload_frequency table")

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
                document_type TEXT NOT NULL DEFAULT 'linked_template'
                    CHECK (document_type IN ('linked_template', 'physical_bill')),
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

        # Audit Risk indicator (Supporting Documents modal redesign, final phase): a Document's
        # risk rating ('Low'/'Medium'/'High'), surfaced on the main voucher table's Risk column.
        # No CHECK constraint here (this is a post-hoc ALTER, not the original CREATE TABLE) -
        # validity is enforced in Python (routers/audit_rules.py's RISK_RATINGS/validate_document_risk_rating).
        # Same upgrade-block treatment as is_active/template_id above.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_documents)")
            columns = [row[1] for row in cur.fetchall()]
            if 'risk_rating' not in columns:
                conn.execute("ALTER TABLE audit_rule_documents ADD COLUMN risk_rating TEXT")
                print("✅ Added risk_rating column to audit_rule_documents")
        except Exception as e:
            print("⚠️ Could not add risk_rating to audit_rule_documents:", e)

        # Transaction level approval (new, confirmed requirement): a per-Document flag saying
        # whether transaction-level approval applies to this Document - read later by
        # Verification's procedure window (#procedureModal) to decide what to show/require for
        # that Document's tab. Lives directly on audit_rule_documents (not per-Category, not the
        # Category-Document junction table approval_required uses) since this is a property of
        # the Document itself. Same upgrade-block treatment as is_active/template_id/risk_rating
        # above.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_documents)")
            columns = [row[1] for row in cur.fetchall()]
            if 'transaction_level_approval' not in columns:
                conn.execute("ALTER TABLE audit_rule_documents ADD COLUMN transaction_level_approval INTEGER DEFAULT 0")
                print("✅ Added transaction_level_approval column to audit_rule_documents")
        except Exception as e:
            print("⚠️ Could not add transaction_level_approval to audit_rule_documents:", e)

        # Physical Bill evidence type (new, confirmed 8-phase requirement, Phase 1 of 8):
        # document_type says HOW a Document's evidence is satisfied - 'linked_template' is
        # today's behaviour (a linked Data Bank row, or ticked procedure roles when the
        # Document has no template), 'physical_bill' is the new manually-keyed Bill No /
        # Date / Amount / Vendor name captured during verification (see
        # voucher_line_physical_bills below). Nothing reads this column yet - the gate that
        # acts on it lands in Phase 6.
        #
        # The CREATE TABLE above carries a real CHECK constraint, but a post-hoc ALTER
        # cannot retrofit one onto an existing table without a full rebuild - so on an
        # already-initialized database this column has no DB-level CHECK. Same deliberate
        # trade-off (and same wording) as risk_rating above: validity is enforced in Python,
        # in routers/audit_rules.py, from Phase 2 onward.
        #
        # The ALTER's own DEFAULT backfills every existing row to 'linked_template', so no
        # separate backfill pass is needed - same mechanism as signature_specimens.is_active.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_documents)")
            columns = [row[1] for row in cur.fetchall()]
            if 'document_type' not in columns:
                conn.execute("ALTER TABLE audit_rule_documents ADD COLUMN document_type TEXT DEFAULT 'linked_template'")
                print("✅ Added document_type column to audit_rule_documents")
        except Exception as e:
            print("⚠️ Could not add document_type to audit_rule_documents:", e)

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

        # ID-based Cross-check-eligible Fields, Phase 1 of 4: field_name stays free text (the
        # existing chip UI and Cross-check compare_field equality checks keep reading it
        # unchanged), but each row now also carries a best-effort resolved master_column_id -
        # the field's real master_columns row, scoped to the Document's own linked template.
        # Nothing reads this column yet; it's the data foundation for a later dropdown UI.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_document_fields)")
            columns = [row[1] for row in cur.fetchall()]
            if 'master_column_id' not in columns:
                conn.execute("ALTER TABLE audit_rule_document_fields ADD COLUMN master_column_id INTEGER")
                print("✅ Added master_column_id column to audit_rule_document_fields")
        except Exception as e:
            print("⚠️ Could not add master_column_id to audit_rule_document_fields:", e)

        # One-time (rerun-safe) backfill: match each still-unresolved field_name to a
        # master_columns row mapped onto its own Document's template, case-insensitive/trimmed.
        # No match (no template_id, or no same-named column on that template) leaves
        # master_column_id NULL - the row is left exactly as-is, never dropped, flagged for
        # manual re-selection once a later phase's dropdown UI lands. Only NULL rows are
        # selected, so this is safe to run on every startup.
        try:
            unmatched_rows = conn.execute(
                """
                SELECT f.id, f.field_name, d.template_id
                FROM audit_rule_document_fields f
                JOIN audit_rule_documents d ON f.document_id = d.id
                WHERE f.master_column_id IS NULL
                """
            ).fetchall()
            matched_count = 0
            skipped_count = 0
            for field_id, field_name, template_id in unmatched_rows:
                if not template_id:
                    skipped_count += 1
                    continue
                match = conn.execute(
                    """
                    SELECT mc.id FROM master_columns mc
                    JOIN template_column_mappings tcm ON tcm.column_id = mc.id
                    WHERE tcm.template_id = ? AND LOWER(TRIM(mc.column_name)) = LOWER(TRIM(?))
                    """,
                    (template_id, field_name)
                ).fetchone()
                if match:
                    conn.execute(
                        "UPDATE audit_rule_document_fields SET master_column_id = ? WHERE id = ?",
                        (match[0], field_id)
                    )
                    matched_count += 1
                else:
                    skipped_count += 1
            if matched_count or skipped_count:
                print(f"✅ Cross-check-eligible field migration: matched {matched_count}, left {skipped_count} unmatched (manual re-selection needed later)")
        except Exception as e:
            print("⚠️ Could not backfill master_column_id on audit_rule_document_fields:", e)

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

        # Required Authority (new, confirmed requirement): a free-text authority level
        # attached to an "Approved by"-type role entry, for a later phase to compare
        # against a voucher's actual approver. Scoped to the role row itself, not a new
        # table. Only ever populated for a role matching the approval pattern (see
        # routers/audit_rules.py's is_approval_role()) - left NULL for every other role.
        # Same idempotent upgrade-block pattern as audit_rule_documents.transaction_level_approval.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_document_auth_roles)")
            columns = [row[1] for row in cur.fetchall()]
            if 'required_authority' not in columns:
                conn.execute("ALTER TABLE audit_rule_document_auth_roles ADD COLUMN required_authority TEXT")
                print("✅ Added required_authority column to audit_rule_document_auth_roles")
        except Exception as e:
            print("⚠️ Could not add required_authority to audit_rule_document_auth_roles:", e)

        # Cross-checks: name, the field being compared, and a comparison type from a fixed
        # set. compare_field is free text (not FK'd to audit_rule_document_fields) since
        # different documents attached to the same cross-check may use differently-named
        # but equivalent fields.
        #
        # Cross-Document Validation Engine, Phase 1 of 10: comparison_type's CHECK constraint
        # gains two new values ('Must be equal', 'May be less, never more') alongside the
        # original 4 (confirmed with Aadil: none of the original 4 are deprecated/dropped).
        # SQLite can't ALTER a CHECK constraint in place, so upgrading an existing database
        # needs the same rename -> recreate -> copy -> drop rebuild used for
        # client_voucher_prefixes' category CHECK above - simpler here since no value
        # remapping is needed, just a straight row copy. Gated on inspecting sqlite_master
        # (schema-driven, not data-driven, since there's no "old row" to detect - the
        # constraint itself is what's stale) so a fresh or already-migrated database is a
        # cheap no-op on every subsequent startup.
        NEW_COMPARISON_TYPES = ('All equal', 'Sequential increasing', 'Sequential decreasing',
                                 'Within tolerance %', 'Must be equal', 'May be less, never more')

        existing_cc_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_rule_cross_checks'"
        ).fetchone()

        if existing_cc_sql and 'Must be equal' not in existing_cc_sql[0]:
            print("⚠️ audit_rule_cross_checks.comparison_type CHECK is on the old 4-value scheme - rebuilding.")
            conn.execute("DROP TABLE IF EXISTS audit_rule_cross_checks_old")
            conn.execute("ALTER TABLE audit_rule_cross_checks RENAME TO audit_rule_cross_checks_old")
            conn.execute(f'''
                CREATE TABLE audit_rule_cross_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    compare_field TEXT NOT NULL,
                    comparison_type TEXT NOT NULL CHECK (comparison_type IN
                        {NEW_COMPARISON_TYPES}),
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('''
                INSERT INTO audit_rule_cross_checks (id, name, compare_field, comparison_type, is_active, created_at)
                SELECT id, name, compare_field, comparison_type, is_active, created_at FROM audit_rule_cross_checks_old
            ''')
            conn.execute("DROP TABLE audit_rule_cross_checks_old")
            print("✅ Rebuilt audit_rule_cross_checks with expanded comparison_type CHECK")
        else:
            conn.execute(f'''
                CREATE TABLE IF NOT EXISTS audit_rule_cross_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    compare_field TEXT NOT NULL,
                    comparison_type TEXT NOT NULL CHECK (comparison_type IN
                        {NEW_COMPARISON_TYPES}),
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

        # Schema upgrade (Cross-Document Validation Engine, Phase 1 of 10): compare_to_voucher_line
        # marks a Cross-check as comparing a Document's field against the voucher line's own
        # amount, rather than only Document-to-Document. The existing audit_rule_cross_check_documents
        # junction is untouched - a voucher-line comparison still needs at least one real Document
        # (the anchor) to compare against, so this is a flag alongside that junction, not a
        # replacement for it. Nullable-with-default, defaults FALSE (0) so no existing Cross-check
        # changes behavior.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_cross_checks)")
            columns = [row[1] for row in cur.fetchall()]
            if 'compare_to_voucher_line' not in columns:
                conn.execute("ALTER TABLE audit_rule_cross_checks ADD COLUMN compare_to_voucher_line INTEGER DEFAULT 0")
                print("✅ Added compare_to_voucher_line column to audit_rule_cross_checks")
        except Exception as e:
            print("⚠️ Could not add compare_to_voucher_line to audit_rule_cross_checks:", e)

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

        # Schema upgrade (Cross-check per-document field names, Phase 1 of 4): each Document
        # in a Cross-check chain can now name its compared field differently (e.g. "Quantity"
        # on a Purchase Order vs "Quantity Received" on Store Receiving) - the fields are
        # matched by chain position/comparison intent, not by identical name. Nullable, no
        # default; a NULL row means "fall back to the parent Cross-check's shared compare_field".
        # Existing rows are backfilled with that shared value so no existing Cross-check breaks
        # or needs re-saving. Same idempotent PRAGMA-gated pattern as the columns above.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_cross_check_documents)")
            columns = [row[1] for row in cur.fetchall()]
            if 'compare_field' not in columns:
                conn.execute("ALTER TABLE audit_rule_cross_check_documents ADD COLUMN compare_field TEXT")
                print("✅ Added compare_field column to audit_rule_cross_check_documents")
        except Exception as e:
            print("⚠️ Could not add compare_field to audit_rule_cross_check_documents:", e)
        # Backfill any still-NULL junction rows from the parent Cross-check's shared field.
        # Runs on every startup - a cheap no-op once every row is populated.
        try:
            conn.execute('''
                UPDATE audit_rule_cross_check_documents
                SET compare_field = (
                    SELECT compare_field FROM audit_rule_cross_checks c
                    WHERE c.id = audit_rule_cross_check_documents.cross_check_id
                )
                WHERE compare_field IS NULL
            ''')
        except Exception as e:
            print("⚠️ Could not backfill compare_field on audit_rule_cross_check_documents:", e)

        # Categories: name, one KB overview note, and (via junction tables below) the
        # Documents it uses and the Cross-checks it applies. This is the entity a voucher
        # will eventually reference (Phase 9).
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rule_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kb_overview TEXT,
                is_active INTEGER DEFAULT 1,
                evidence_type TEXT CHECK (evidence_type IN ('billable', 'non_billable')),
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

        # Schema upgrade (Validation Rules, Phase 1 of 7): matrix_version is bumped by a later
        # phase whenever this Category's document/approval requirements change - lets
        # matrix_version_applied (verification_details, below) detect a stale classification
        # without silently reclassifying an already-Cleared voucher.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_categories)")
            columns = [row[1] for row in cur.fetchall()]
            if 'matrix_version' not in columns:
                conn.execute("ALTER TABLE audit_rule_categories ADD COLUMN matrix_version INTEGER NOT NULL DEFAULT 1")
                print("✅ Added matrix_version column to audit_rule_categories")
        except Exception as e:
            print("⚠️ Could not add matrix_version to audit_rule_categories:", e)

        # Physical Bill evidence type (Phase 1 of 8): evidence_type says WHAT kind of evidence
        # this Category requires - 'billable' requires a Physical Bill Document (whose Amount
        # is later hard-matched against the voucher line's own amount), 'non_billable' keeps
        # today's procedure-role gate. Deliberately nullable with NO default: every existing
        # Category stays NULL, i.e. "not yet classified", and needs a manual backfill before
        # Phase 6 turns the gate on - silently defaulting historic Categories to either value
        # would hide that decision rather than surface it.
        #
        # NULL always passes a SQL CHECK in SQLite, so nullable and CHECK-constrained coexist
        # correctly in the CREATE TABLE above. On an already-initialized database this ALTER
        # carries no DB-level CHECK (same post-hoc-ALTER limitation, and same Python-side
        # enforcement plan, as audit_rule_documents.document_type / risk_rating).
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_categories)")
            columns = [row[1] for row in cur.fetchall()]
            if 'evidence_type' not in columns:
                conn.execute("ALTER TABLE audit_rule_categories ADD COLUMN evidence_type TEXT")
                print("✅ Added evidence_type column to audit_rule_categories")
        except Exception as e:
            print("⚠️ Could not add evidence_type to audit_rule_categories:", e)

        # Documents a Category uses. approval_required (Validation Rules Phase 1 of 7) has since
        # been hard-removed (confirmed requirement, 2026-08-02) - approval requirement is now
        # decided entirely at Document level via the "Approved by" role's Required Authority
        # field (audit_rule_document_auth_roles.required_authority), not configured per Category.
        # A database still carrying the old column needs the same rename -> recreate -> copy ->
        # drop rebuild already used for audit_rule_cross_checks.comparison_type's CHECK expansion
        # above - gated on inspecting sqlite_master's stored CREATE TABLE sql (schema-driven, not
        # data-driven) so a fresh or already-migrated database is a no-op on every subsequent
        # startup.
        existing_acd_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_rule_category_documents'"
        ).fetchone()

        if existing_acd_sql and 'approval_required' in existing_acd_sql[0]:
            print("⚠️ audit_rule_category_documents still has the retired approval_required column - rebuilding.")
            conn.execute("DROP TABLE IF EXISTS audit_rule_category_documents_old")
            conn.execute("ALTER TABLE audit_rule_category_documents RENAME TO audit_rule_category_documents_old")
            conn.execute('''
                CREATE TABLE audit_rule_category_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_id INTEGER NOT NULL,
                    document_id INTEGER NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_required INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (category_id) REFERENCES audit_rule_categories(id) ON DELETE CASCADE,
                    FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id) ON DELETE CASCADE,
                    UNIQUE(category_id, document_id)
                )
            ''')
            conn.execute('''
                INSERT INTO audit_rule_category_documents (id, category_id, document_id, sort_order, is_required)
                SELECT id, category_id, document_id, sort_order, is_required FROM audit_rule_category_documents_old
            ''')
            conn.execute("DROP TABLE audit_rule_category_documents_old")
            print("✅ Rebuilt audit_rule_category_documents without approval_required")
        else:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS audit_rule_category_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category_id INTEGER NOT NULL,
                    document_id INTEGER NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_required INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (category_id) REFERENCES audit_rule_categories(id) ON DELETE CASCADE,
                    FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id) ON DELETE CASCADE,
                    UNIQUE(category_id, document_id)
                )
            ''')

        # Schema upgrade (Cross-Document Validation Engine, Phase 1 of 10): is_required flags
        # whether this Document is mandatory for this Category (vs. optional/informational).
        # Defaults to TRUE (1) so every existing Category-Document pairing keeps behaving
        # exactly as it does today, where every configured document is implicitly required.
        # Kept as a defensive upgrade block too (in case a database predates is_required and
        # never carried approval_required to begin with, so the rebuild branch above never fired).
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_category_documents)")
            columns = [row[1] for row in cur.fetchall()]
            if 'is_required' not in columns:
                conn.execute("ALTER TABLE audit_rule_category_documents ADD COLUMN is_required INTEGER NOT NULL DEFAULT 1")
                print("✅ Added is_required column to audit_rule_category_documents")
        except Exception as e:
            print("⚠️ Could not add is_required to audit_rule_category_documents:", e)

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

        # Signature specimens (Validation Rules, Phase 1 of 7: schema): the expected signatory
        # name for a given role, on a given Document, for a given client - name-only, no image
        # storage, per the locked assumption. Scoped per client (signatures are a client's own
        # staff, not a global list). role_name is a free string matching the existing convention
        # used by audit_rule_document_auth_roles.role_name/voucher_line_procedure_checks.role_name
        # (no role id exists anywhere in this app). Brand-new table with no prior data, so a
        # plain CREATE TABLE IF NOT EXISTS is sufficient - no ALTER TABLE upgrade block needed.
        # Relax-uniqueness requirement (new, confirmed 4-phase requirement), Phase 2 of 4: a
        # fresh install lands directly on the final desired shape - is_active present, no
        # table-level UNIQUE(client_id, document_id, role_name) - since that constraint would
        # otherwise block creating a replacement specimen (routine personnel turnover) once any
        # row, even a deactivated one, exists for the same triple. Replaced by application-level
        # validation in routers/audit_rules.py (no duplicate ACTIVE person per client+document+
        # role; multiple inactive/historical rows and multiple concurrently-active rows for
        # genuinely different people are both allowed).
        conn.execute('''
            CREATE TABLE IF NOT EXISTS signature_specimens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                document_id INTEGER NOT NULL,
                role_name TEXT NOT NULL,
                expected_signatory_name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id) ON DELETE CASCADE
            )
        ''')

        # SQLite can't ALTER/DROP a table constraint in place, so an existing database that
        # still has the old UNIQUE(client_id, document_id, role_name) constraint needs the same
        # schema-driven rename -> recreate -> copy -> drop rebuild already used above for
        # audit_rule_cross_checks.comparison_type's CHECK-constraint expansion - gated on
        # inspecting sqlite_master's stored CREATE TABLE sql (not data), so a fresh or
        # already-migrated database is a no-op on every subsequent startup.
        existing_ss_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='signature_specimens'"
        ).fetchone()
        if existing_ss_sql and 'UNIQUE(client_id, document_id, role_name)' in existing_ss_sql[0]:
            print("⚠️ signature_specimens still has the old UNIQUE(client_id, document_id, role_name) constraint - rebuilding.")
            conn.execute("DROP TABLE IF EXISTS signature_specimens_old")
            conn.execute("ALTER TABLE signature_specimens RENAME TO signature_specimens_old")
            conn.execute('''
                CREATE TABLE signature_specimens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER NOT NULL,
                    document_id INTEGER NOT NULL,
                    role_name TEXT NOT NULL,
                    expected_signatory_name TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                    FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id) ON DELETE CASCADE
                )
            ''')
            # is_active may or may not already exist on the old table depending on whether
            # Phase 1's ALTER block already ran in an earlier startup - handle both, don't
            # assume order.
            old_cols = [row[1] for row in conn.execute("PRAGMA table_info(signature_specimens_old)").fetchall()]
            is_active_expr = "is_active" if 'is_active' in old_cols else "1"
            conn.execute(f'''
                INSERT INTO signature_specimens
                    (id, client_id, document_id, role_name, expected_signatory_name, is_active, created_at, updated_at)
                SELECT id, client_id, document_id, role_name, expected_signatory_name, {is_active_expr}, created_at, updated_at
                FROM signature_specimens_old
            ''')
            conn.execute("DROP TABLE signature_specimens_old")
            print("✅ Rebuilt signature_specimens without the UNIQUE(client_id, document_id, role_name) constraint")

        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_signature_specimens_client ON signature_specimens (client_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_signature_specimens_document ON signature_specimens (document_id)")
        print("✅ Created signature_specimens table (Validation Rules, Phase 1 of 7: schema)")

        # Soft-delete conversion (new, confirmed 4-phase requirement), Phase 1 of 4: signature
        # specimens were originally hard-delete only (no is_active column). Same upgrade-block
        # pattern as audit_rule_documents.transaction_level_approval above - existing rows
        # default to is_active = 1 via the ALTER's own DEFAULT, no separate backfill needed.
        # Kept in place after the Phase 2 rebuild above too - for an already-rebuilt table this
        # is a clean no-op (is_active already present), and it remains correct for any database
        # state the rebuild block above doesn't touch.
        try:
            cur = conn.execute("PRAGMA table_info(signature_specimens)")
            columns = [row[1] for row in cur.fetchall()]
            if 'is_active' not in columns:
                conn.execute("ALTER TABLE signature_specimens ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
                print("✅ Added is_active column to signature_specimens")
        except Exception as e:
            print("⚠️ Could not add is_active to signature_specimens:", e)

        # Authority (new, confirmed requirement): a free-text authority level for the actual
        # signatory on file, captured alongside Name at add-time - a later phase will compare
        # this against a Document's Required Authority (audit_rule_document_auth_roles.
        # required_authority) for the "Approved by" role. Unconditional - every specimen gets
        # this field, not gated to an approval-type role. Same idempotent upgrade-block
        # pattern as the is_active addition directly above.
        try:
            cur = conn.execute("PRAGMA table_info(signature_specimens)")
            columns = [row[1] for row in cur.fetchall()]
            if 'authority' not in columns:
                conn.execute("ALTER TABLE signature_specimens ADD COLUMN authority TEXT")
                print("✅ Added authority column to signature_specimens")
        except Exception as e:
            print("⚠️ Could not add authority to signature_specimens:", e)

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

        # ---------- ACCOUNT CATEGORY ASSIGNMENTS (Verification <-> Assign Categories link, Phase 1 of 5: schema) ----------
        # Persistent account-code -> Category mapping, so the Verification module's per-line
        # "Type" field can auto-fill from what was assigned on the Assign Categories screen
        # instead of being manually picked each time. Assign Categories remains the only place
        # a Category is ever set (Verification only reads, never writes, this table).
        # Keyed by client_id + account_code (not by trial_balance_upload_id) so the mapping
        # survives a new Trial Balance upload - re-uploading keeps prior assignments and only
        # genuinely new account codes show up as unassigned. Matched on account code, not
        # account name, and scoped per client since account codes are not globally unique
        # across clients. category_id is a soft link to audit_rule_categories.id - no
        # FOREIGN KEY clause, same convention as trial_balance_accounts.category_id above (this
        # app never enables PRAGMA foreign_keys, so every audit_rule_* reference elsewhere in
        # this file is already a soft reference in practice, not an enforced one). Brand-new
        # table with no prior data, so a plain CREATE TABLE IF NOT EXISTS is sufficient - no
        # ALTER TABLE upgrade block needed (unlike trial_balance_accounts.category_id, which
        # had to retrofit an already-live table). Schema only this phase - no backend endpoint,
        # no UI, no migration/backfill from trial_balance_accounts.category_id (that carry-
        # forward logic is Phase 2's job).
        conn.execute('''
            CREATE TABLE IF NOT EXISTS account_category_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                account_code TEXT NOT NULL,
                category_id INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                UNIQUE(client_id, account_code)
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_account_category_assignments_client ON account_category_assignments (client_id)")
        print("✅ Created account_category_assignments table (Phase 1 of 5: schema foundation)")

        # ---------- VOUCHER LINE SUPPORTING DOCUMENTS (new 6-phase requirement, Phase 1 of 6: schema) ----------
        # Records which Data Bank row (client_data) has been linked as supporting evidence to
        # which voucher line (verification_details.id - the same "uid" used throughout
        # verification.js), and for which Document "slot" (audit_rule_documents.id - the live
        # per-Category document list already powering the Supporting column, reused here rather
        # than a free-text document type). A row's `status` (Unassign requirement, single phase)
        # is 'active' or 'unassigned' - unassigning is a soft delete (status flip + unassigned_at
        # stamp via POST /api/verification/unassign_document), not a row delete, so linked_by/
        # linked_at stay intact as a provenance trail. "Once linked, a Data Bank row can't be
        # assigned again to any other voucher line WHILE the link is active" is enforced by a
        # PARTIAL unique index on client_data_id scoped to status='active' (see below) rather than
        # a blanket table-level UNIQUE - a plain UNIQUE(client_data_id) would permanently block
        # reassignment even after the original link is unassigned, since the old (now-inactive)
        # row would still occupy that unique slot. Every read query against this table must filter
        # `status = 'active'` (assign_supporting_document's duplicate check, get_voucher_line_documents,
        # get_assignment_status_map in routers/verification.py) - an unassigned row must never be
        # treated as a live link. No FOREIGN KEY enforcement fires at runtime (this app never sets
        # PRAGMA foreign_keys = ON), so the assignment endpoints validate each referenced id
        # explicitly before writing - the FK clauses below are documentation/ON DELETE CASCADE intent only.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS voucher_line_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_line_id INTEGER NOT NULL,
                client_data_id INTEGER NOT NULL,
                document_id INTEGER NOT NULL,
                linked_by TEXT DEFAULT 'Analyst',
                linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'unassigned')),
                unassigned_at TIMESTAMP,
                FOREIGN KEY (voucher_line_id) REFERENCES verification_details(id) ON DELETE CASCADE,
                FOREIGN KEY (client_data_id) REFERENCES client_data(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id)
            )
        ''')
        print("✅ Created voucher_line_documents table (Phase 1 of 6: schema foundation)")

        # Schema upgrade: the table above originally shipped with a blanket table-level
        # UNIQUE(client_data_id) constraint and no status/unassigned_at columns - the Unassign
        # requirement needs to swap that for the partial-unique-index shape created above, which
        # SQLite can't do via ALTER TABLE (a table constraint can't be dropped in place). Detect
        # the old shape (no 'status' column) and, if found and the table has any rows, rebuild:
        # rename -> recreate with the new shape (CREATE TABLE IF NOT EXISTS above already ran and
        # is a no-op on an existing table, so this recreates for real) -> copy every row across as
        # status='active' (preserving all pre-existing links exactly as they were) -> drop the
        # renamed old table. A cheap file-copy backup is taken first, same convention as the
        # client_voucher_prefixes 5-way migration above. Gated so this only ever runs once.
        try:
            existing_columns = [row[1] for row in conn.execute("PRAGMA table_info(voucher_line_documents)").fetchall()]
        except sqlite3.OperationalError:
            existing_columns = []

        if existing_columns and 'status' not in existing_columns:
            print("⚠️ Found voucher_line_documents on the old blanket-UNIQUE(client_data_id) shape - migrating to the status/partial-unique-index shape.")

            backup_path = f"{DB_NAME}.bak-premigrate-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            shutil.copy2(DB_NAME, backup_path)
            print(f"✅ Backed up database to {backup_path} before migrating voucher_line_documents")

            before_count = conn.execute("SELECT COUNT(*) FROM voucher_line_documents").fetchone()[0]

            conn.execute("DROP TABLE IF EXISTS voucher_line_documents_old")
            conn.execute("ALTER TABLE voucher_line_documents RENAME TO voucher_line_documents_old")
            conn.execute('''
                CREATE TABLE voucher_line_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    voucher_line_id INTEGER NOT NULL,
                    client_data_id INTEGER NOT NULL,
                    document_id INTEGER NOT NULL,
                    linked_by TEXT DEFAULT 'Analyst',
                    linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'unassigned')),
                    unassigned_at TIMESTAMP,
                    FOREIGN KEY (voucher_line_id) REFERENCES verification_details(id) ON DELETE CASCADE,
                    FOREIGN KEY (client_data_id) REFERENCES client_data(id) ON DELETE CASCADE,
                    FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id)
                )
            ''')
            conn.execute('''
                INSERT INTO voucher_line_documents
                    (id, voucher_line_id, client_data_id, document_id, linked_by, linked_at, status)
                SELECT id, voucher_line_id, client_data_id, document_id, linked_by, linked_at, 'active'
                FROM voucher_line_documents_old
            ''')
            conn.execute("DROP TABLE voucher_line_documents_old")

            after_count = conn.execute("SELECT COUNT(*) FROM voucher_line_documents WHERE status = 'active'").fetchone()[0]
            print(f"✅ Migrated voucher_line_documents: {before_count} row(s) carried forward as status='active' ({after_count} confirmed).")

        # Indexes are (re)created here, after the migration block above has guaranteed the table
        # is on the status/status-partial-unique-index shape (either it always was, on a fresh
        # install, or it was just rebuilt into that shape) - creating the partial index any earlier
        # would fail with "no such column: status" against a not-yet-migrated old-shape table.
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_voucher_line_documents_line ON voucher_line_documents (voucher_line_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_voucher_line_documents_active_line ON voucher_line_documents (voucher_line_id) WHERE status = 'active'")
        create_index_with_retry(conn, "CREATE UNIQUE INDEX IF NOT EXISTS idx_voucher_line_documents_active_client_data ON voucher_line_documents (client_data_id) WHERE status = 'active'")

        # Schema upgrade (Validation Rules, Phase 1 of 7): signature_exception flags a confirmed
        # mismatch between a linked evidence document's actual signatory and its expected
        # signature_specimens entry. Default 0 = OK (matches expected specimen, or not yet
        # checked) - only ever set to 1 on a confirmed mismatch, per the locked assumption.
        # Per voucher_line_documents row (i.e. per linked evidence document), not per voucher
        # line, since a signature check is inherently about one specific linked document.
        try:
            cur = conn.execute("PRAGMA table_info(voucher_line_documents)")
            columns = [row[1] for row in cur.fetchall()]
            if 'signature_exception' not in columns:
                conn.execute("ALTER TABLE voucher_line_documents ADD COLUMN signature_exception INTEGER DEFAULT 0")
                print("✅ Added signature_exception column to voucher_line_documents")
        except Exception as e:
            print("⚠️ Could not add signature_exception to voucher_line_documents:", e)

        # ---------- VOUCHER LINE PROCEDURE CHECKS (Phase 5 of 6: audit procedures checklist) ----------
        # Tick state for the audit-procedures checklist rendered under each Document tab's
        # Linked Evidence table in the Supporting Documents modal. A procedure/role is
        # identified by role_name (matching audit_rule_document_auth_roles.role_name) rather
        # than a role id, since get_document_auth_roles() already exposes roles as plain
        # strings with no id surfaced anywhere in this app - staying consistent with that.
        # Scoped to (voucher_line_id, document_id, role_name) rather than living on
        # voucher_line_documents, since a checklist must exist even before any evidence row
        # has been linked for that document (voucher_line_documents has no row until then).
        # A role that's never been ticked simply has no row here - absence means unchecked,
        # the same "missing = not set" convention already used by voucher_line_documents
        # itself. No validation of what a tick "means" lives here (rules are deferred to a
        # future phase, per the confirmed brief) - this table only persists the checkbox
        # state itself. Brand-new table with no prior data, so a plain CREATE TABLE IF NOT
        # EXISTS is sufficient - no ALTER TABLE upgrade block needed (same reasoning as
        # voucher_line_documents above). FOREIGN KEY clauses are documentation/ON DELETE
        # CASCADE intent only - this app never sets PRAGMA foreign_keys = ON, so
        # routers/verification.py's endpoints validate each referenced id explicitly.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS voucher_line_procedure_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_line_id INTEGER NOT NULL,
                document_id INTEGER NOT NULL,
                role_name TEXT NOT NULL,
                is_checked INTEGER NOT NULL DEFAULT 0,
                checked_by TEXT,
                checked_at TIMESTAMP,
                FOREIGN KEY (voucher_line_id) REFERENCES verification_details(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id),
                UNIQUE(voucher_line_id, document_id, role_name)
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_voucher_line_procedure_checks_line ON voucher_line_procedure_checks (voucher_line_id)")
        print("✅ Created voucher_line_procedure_checks table (Phase 5 of 6: audit procedures checklist)")

        # ---------- PHYSICAL BILL EVIDENCE (Multi-Bill / Multi-Line, current shape) ----------
        # The evidence a 'billable' Category (audit_rule_categories.evidence_type) requires, in
        # place of a linked Data Bank row, when the relevant Document is a Physical Bill. A
        # voucher line can carry more than one vendor bill against a given Document, so the
        # structure is voucher line -> bill(s) -> item line(s): voucher_line_physical_bills is
        # the bill header (Bill No/Vendor Name/Date), voucher_line_physical_bill_items holds
        # each bill's Item/Rate/Qty/Amount rows. document_id is carried on the header since a
        # Billable Category can attach more than one Physical Bill Document, and the Cleared/
        # Query gate's check #1 iterates per required Document. No UNIQUE constraint on the
        # header - a line can have several bills against the same Document.
        #
        # voucher_line_id keys to the session-scoped verification_details.id, matching every
        # other voucher_line_* table. Reads MUST go through _resolve_sibling_voucher_line_ids()
        # (routers/verification.py) the way voucher_line_documents/_observations/
        # _procedure_checks already do - reopening a cashbook creates fresh verification_details
        # rows, and skipping that resolution is the exact orphaning bug this module has already
        # fixed three times. FOREIGN KEY clauses throughout are documentation/intent only: this
        # app never sets PRAGMA foreign_keys = ON, so routers validate each referenced id
        # explicitly.
        #
        # This replaced an earlier flat shape (one row = one bill, with quantity/rate/amount for
        # a single implicit item, and UNIQUE(voucher_line_id, document_id) limiting a line to one
        # bill per Document) as part of the Multi-Bill/Multi-Line Physical Bill requirement,
        # Phase 1 of 6. SQLite can't drop a column/constraint in place, so upgrading a database
        # that still has that old flat shape (detected via the quantity column living directly on
        # this table) requires a full table rebuild - same idiom already used elsewhere in this
        # file for client_voucher_prefixes/audit_rule_cross_checks/audit_rule_category_documents/
        # signature_specimens/voucher_line_documents: rename -> recreate both tables -> copy
        # header rows across (ids preserved) -> fold each old row's quantity/rate/amount/
        # source_* into one item row keyed to that same id as bill_id -> drop the renamed table.
        existing_vpb_cols = [row[1] for row in conn.execute("PRAGMA table_info(voucher_line_physical_bills)").fetchall()]
        needs_vpb_rebuild = 'quantity' in existing_vpb_cols

        if needs_vpb_rebuild:
            print("⚠️ voucher_line_physical_bills is still the old flat (one bill = one item) shape - rebuilding for multi-bill/multi-line support.")
            conn.execute("DROP TABLE IF EXISTS voucher_line_physical_bills_old")
            conn.execute("ALTER TABLE voucher_line_physical_bills RENAME TO voucher_line_physical_bills_old")

        conn.execute('''
            CREATE TABLE IF NOT EXISTS voucher_line_physical_bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_line_id INTEGER NOT NULL,
                document_id INTEGER NOT NULL,
                bill_no TEXT,
                bill_date TEXT,
                vendor_name TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (voucher_line_id) REFERENCES verification_details(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id)
            )
        ''')

        # Item lines under a bill (Item/Rate/Qty/Amount). source_document_row_id/source_template
        # (which GRN/ERP Vendor Bill row, and which template, a given entry was populated from)
        # move here from the old header-level columns, since "copy from lines" now seeds one
        # item per source Store Receiving row - each item can trace back to a different source
        # row within the same bill. amount is stored (Qty x Rate, rounded to rate_precision at
        # save time), not recomputed on every read, matching this table's own existing
        # convention of persisting amount rather than deriving it live.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS voucher_line_physical_bill_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bill_id INTEGER NOT NULL,
                item_name TEXT,
                quantity REAL,
                rate REAL,
                amount REAL,
                source_document_row_id INTEGER,
                source_template TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (bill_id) REFERENCES voucher_line_physical_bills(id) ON DELETE CASCADE
            )
        ''')

        if needs_vpb_rebuild:
            conn.execute('''
                INSERT INTO voucher_line_physical_bills
                    (id, voucher_line_id, document_id, bill_no, bill_date, vendor_name, sort_order, created_at, updated_at)
                SELECT id, voucher_line_id, document_id, bill_no, bill_date, vendor_name, 0, created_at, updated_at
                FROM voucher_line_physical_bills_old
            ''')
            conn.execute('''
                INSERT INTO voucher_line_physical_bill_items
                    (bill_id, item_name, quantity, rate, amount, source_document_row_id, source_template, sort_order, created_at, updated_at)
                SELECT id, NULL, quantity, rate, amount, source_document_row_id, source_template, 0, created_at, updated_at
                FROM voucher_line_physical_bills_old
                WHERE quantity IS NOT NULL OR rate IS NOT NULL OR amount IS NOT NULL
                   OR source_document_row_id IS NOT NULL OR source_template IS NOT NULL
            ''')
            conn.execute("DROP TABLE voucher_line_physical_bills_old")
            print("✅ Rebuilt voucher_line_physical_bills as a bill header + migrated existing bills into voucher_line_physical_bill_items")
        else:
            print("✅ Created voucher_line_physical_bills/voucher_line_physical_bill_items tables (Multi-Bill/Multi-Line Physical Bill, Phase 1 of 6)")

        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_voucher_line_physical_bills_line ON voucher_line_physical_bills (voucher_line_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_voucher_line_physical_bill_items_bill ON voucher_line_physical_bill_items (bill_id)")

        # ---------- VOUCHER-LEVEL APPROVAL (Validation Rules, Phase 1 addendum) ----------
        # Prepared By / Checked By / Approved By as one independent, mandatory record per
        # real-world voucher - separate from (not replacing) the per-document approval_required
        # flag on audit_rule_category_documents. Text/name fields only, no signature capture,
        # no linked document. Conceptually surfaces on the procedure modal's first tab (renamed
        # "Voucher Line") in a later phase - schema only this phase.
        #
        # Keyed by (client_id, transaction_ref) rather than (session_id, transaction_ref):
        # reopening a cashbook creates a brand-new verification_sessions row every time, and this
        # exact orphaning failure mode has already been fixed three times in this module
        # (voucher_line_documents/voucher_line_procedure_checks/voucher_line_observations, see
        # _resolve_sibling_voucher_line_ids()) - keying by client_id keeps one approval record
        # alive across every session that ever touches the same real-world voucher. Brand-new
        # table with no prior data, so a plain CREATE TABLE IF NOT EXISTS is sufficient - no
        # ALTER TABLE upgrade block needed (same reasoning as voucher_line_documents above).
        conn.execute('''
            CREATE TABLE IF NOT EXISTS voucher_approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                transaction_ref TEXT NOT NULL,
                prepared_by TEXT,
                checked_by TEXT,
                approved_by TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                UNIQUE(client_id, transaction_ref)
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_voucher_approvals_client_ref ON voucher_approvals (client_id, transaction_ref)")
        print("✅ Created voucher_approvals table (Validation Rules, Phase 1 addendum: voucher-level approval)")

        # ---------- GENERIC VOUCHER-LEVEL CHECKS (new, confirmed 6-phase requirement, Phase 1 of 6: schema) ----------
        # Replaces the hardcoded Prepared/Checked/Approved-By concept (voucher_approvals above,
        # left in place untouched - not dropped/migrated yet) with a data-driven list of checks,
        # so a future check (e.g. "Received By") can be added with no schema change. Checks are
        # global (apply across all clients), not per-client. Brand-new tables with no prior data,
        # so plain CREATE TABLE IF NOT EXISTS is sufficient - no ALTER TABLE upgrade block needed
        # (same reasoning as voucher_approvals/voucher_line_observations above).
        #
        # Check definitions: name + display order + active flag. Named with the audit_rule_*
        # prefix (matching audit_rule_documents/audit_rule_cross_checks/audit_rule_categories)
        # since this gets its own CRUD/tab in the Audit Rules module in a later phase.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rule_voucher_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        print("✅ Created audit_rule_voucher_checks table (Generic Voucher-Level Checks, Phase 1 of 6: schema)")

        # Authority/Designation field (new, confirmed requirement) - applies uniformly to every
        # check (Prepared by/Checked by/Approved by/any future check), no role-specific gating.
        # Informational only this phase - not matched/compared against anything yet. Same
        # idempotent PRAGMA table_info() upgrade-block pattern as audit_rule_documents'
        # transaction_level_approval/risk_rating additions.
        try:
            cur = conn.execute("PRAGMA table_info(audit_rule_voucher_checks)")
            columns = [row[1] for row in cur.fetchall()]
            if 'authority' not in columns:
                conn.execute("ALTER TABLE audit_rule_voucher_checks ADD COLUMN authority TEXT")
                print("✅ Added authority column to audit_rule_voucher_checks")
        except Exception as e:
            print("⚠️ Could not add authority to audit_rule_voucher_checks:", e)

        # Role-gating on a check, modelled identically to audit_rule_document_auth_roles - a
        # list from the start so more than one required role can gate a single check.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_rule_voucher_check_auth_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_check_id INTEGER NOT NULL,
                role_name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (voucher_check_id) REFERENCES audit_rule_voucher_checks(id) ON DELETE CASCADE,
                UNIQUE(voucher_check_id, role_name)
            )
        ''')
        print("✅ Created audit_rule_voucher_check_auth_roles table (Generic Voucher-Level Checks, Phase 1 of 6: schema)")

        # Per-voucher status: one row per (voucher, check) capturing both the tick and the
        # inline name of who did it (no login/identity system exists to auto-fill this). Keyed
        # by (client_id, transaction_ref) rather than session_id, deliberately mirroring
        # voucher_approvals' own keying rationale above - a cashbook reopen creates a brand-new
        # verification_sessions row every time, and session-scoped keys have already caused
        # three separate orphaning bugs elsewhere in this module (voucher_line_documents/
        # voucher_line_procedure_checks/voucher_line_observations). New checks apply going
        # forward only - no retroactive backfill onto in-progress vouchers, per confirmed scope.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS voucher_check_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                transaction_ref TEXT NOT NULL,
                check_id INTEGER NOT NULL,
                ticked INTEGER NOT NULL DEFAULT 0,
                performed_by_name TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY (check_id) REFERENCES audit_rule_voucher_checks(id),
                UNIQUE(client_id, transaction_ref, check_id)
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_audit_rule_voucher_check_auth_roles_check ON audit_rule_voucher_check_auth_roles (voucher_check_id)")
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_voucher_check_status_client_ref ON voucher_check_status (client_id, transaction_ref)")
        print("✅ Created voucher_check_status table (Generic Voucher-Level Checks, Phase 1 of 6: schema)")

        # Voucher-level Checks simplified to name-only (new 4-phase requirement), Phase 2 of 4:
        # replaces the old boolean ticked/performed_by_name model with a 3-state per-check
        # status (untouched/done/not_done, validated in Python - same convention as
        # audit_rule_documents.risk_rating having no SQL CHECK constraint). `flagged_in_
        # observations` is a second, independent flag written ONLY by the new "verify" endpoint
        # (routers/verification.py's verify_voucher_checks) - it records which checks were
        # not-done as of the last Verify action, so that ordinary line saves (Prev/Next/close)
        # can keep re-rendering the voucher's Observation field without themselves changing
        # what it contains; only an explicit Verify call is allowed to add/update/remove a
        # check's "marked not done" line. Same idempotent PRAGMA table_info() upgrade-block
        # pattern as audit_rule_documents' risk_rating/transaction_level_approval additions.
        try:
            cur = conn.execute("PRAGMA table_info(voucher_check_status)")
            columns = [row[1] for row in cur.fetchall()]
            if 'state' not in columns:
                conn.execute("ALTER TABLE voucher_check_status ADD COLUMN state TEXT NOT NULL DEFAULT 'untouched'")
                # One-time backfill from the old boolean column so already-ticked checks aren't
                # silently reset to blank - only ever runs the moment this column is first added.
                conn.execute("UPDATE voucher_check_status SET state = CASE WHEN ticked = 1 THEN 'done' ELSE 'untouched' END")
                print("✅ Added state column to voucher_check_status (backfilled from ticked)")
        except Exception as e:
            print("⚠️ Could not add state to voucher_check_status:", e)
        try:
            cur = conn.execute("PRAGMA table_info(voucher_check_status)")
            columns = [row[1] for row in cur.fetchall()]
            if 'flagged_in_observations' not in columns:
                conn.execute("ALTER TABLE voucher_check_status ADD COLUMN flagged_in_observations INTEGER NOT NULL DEFAULT 0")
                print("✅ Added flagged_in_observations column to voucher_check_status")
        except Exception as e:
            print("⚠️ Could not add flagged_in_observations to voucher_check_status:", e)

        # ---------- VOUCHER LINE OBSERVATIONS (Phase 6 of 6, FINAL: observation flow) ----------
        # One row per (voucher_line_id, document_id) - a Document tab's observation text is
        # upserted in place here, not appended as a history log. `synced` flags whether the
        # CURRENT text has already been rolled into that line's own verification_details.observations
        # field (routers/verification.py's sync_line_observations_to_voucher(), called from
        # save_line_items() - the single chokepoint every Save/Next/Prev/Validate/Complete/Exit
        # path already runs per line). Every explicit save via POST /api/verification/save_line_observation
        # unconditionally resets synced=0, so an edited-and-resaved entry is picked up again on
        # the next line-save; already-synced entries are skipped so the same text is never
        # appended twice. Brand-new table with no prior data, so a plain CREATE TABLE IF NOT
        # EXISTS is sufficient - no ALTER TABLE upgrade block needed (same reasoning as
        # voucher_line_documents/voucher_line_procedure_checks above). FOREIGN KEY clauses are
        # documentation/ON DELETE CASCADE intent only - this app never sets PRAGMA foreign_keys
        # = ON, so routers/verification.py's endpoints validate each referenced id explicitly.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS voucher_line_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_line_id INTEGER NOT NULL,
                document_id INTEGER NOT NULL,
                observation_text TEXT,
                synced INTEGER NOT NULL DEFAULT 0,
                last_synced_text TEXT,
                updated_by TEXT DEFAULT 'Analyst',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (voucher_line_id) REFERENCES verification_details(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES audit_rule_documents(id),
                UNIQUE(voucher_line_id, document_id)
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_voucher_line_observations_line ON voucher_line_observations (voucher_line_id)")
        print("✅ Created voucher_line_observations table (Phase 6 of 6, FINAL: observation flow)")

        # Schema upgrade (confirmed fix, 2026-07-25): add a nullable last_synced_text column to
        # voucher_line_observations - the table already has live data (this feature's earlier
        # save_line_observation() fix now rolls up on every explicit save, not just on the next
        # save_line_items() call), so a plain CREATE TABLE addition alone would be a no-op on any
        # existing database - same idempotent ALTER TABLE ADD COLUMN upgrade-block pattern used
        # for trial_balance_accounts' category_id/period columns above. Records what text was
        # last actually rolled into verification_details.observations for this
        # (voucher_line_id, document_id), separate from observation_text (which always holds the
        # current saved value, even before it's synced) - lets sync_line_observations_to_voucher()
        # recognize a repeat save of unchanged text as a no-op instead of appending a duplicate
        # block every time.
        try:
            cur = conn.execute("PRAGMA table_info(voucher_line_observations)")
            existing_columns = [row[1] for row in cur.fetchall()]
            if 'last_synced_text' not in existing_columns:
                conn.execute("ALTER TABLE voucher_line_observations ADD COLUMN last_synced_text TEXT")
            print("✅ Added last_synced_text column to voucher_line_observations")
        except Exception as e:
            print("⚠️ Could not add last_synced_text column to voucher_line_observations:", e)

        # ---------- USERS & SESSIONS (new User module, Phase 1 of 7: schema only) ----------
        # Foundational tables for the upcoming Login -> Client Cards -> Dashboard entry flow.
        # role is nullable/unused until the follow-on roles/permissions module populates it.
        # No ALTER-TABLE upgrade block needed - brand-new tables, no prior data anywhere.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                role TEXT,
                last_login TIMESTAMP
            )
        ''')

        # token is the session's own primary key (a securely-generated random string minted by
        # the future login endpoint, not this phase's concern). selected_client_id is nullable
        # and added now even though it's only wired up in a later phase, since it lives on this
        # same table. FOREIGN KEY clauses are documentation/intent only, same convention as the
        # rest of this file (PRAGMA foreign_keys is never turned ON) - future endpoints validate
        # referenced ids explicitly.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expiry TIMESTAMP NOT NULL,
                selected_client_id INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (selected_client_id) REFERENCES clients(id) ON DELETE SET NULL
            )
        ''')
        create_index_with_retry(conn, "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id)")
        print("✅ Created users/sessions tables")

        # Seed a single admin user (same "seed if empty" pattern as error_codes/master_columns
        # above) - credentials are a build-time placeholder until a change-password flow exists.
        existing_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing_users == 0:
            admin_password_hash = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode()
            conn.execute(
                "INSERT INTO users (username, password_hash, is_active, role, last_login) VALUES (?, ?, ?, ?, ?)",
                ("admin", admin_password_hash, 1, "admin", None)
            )
            print("✅ Seeded admin user")

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


# ============================================================================
# POSTGRES (SUPABASE) SCHEMA - Phase 1 of 8 of the Supabase/Render migration.
# ============================================================================
# Hand-ported, idiomatic Postgres equivalent of everything init_db() (above)
# builds for SQLite. This function is NOT wired into app startup yet - no
# connection layer/env-var switch exists (that's Phase 2). It targets a
# brand-new, empty Supabase project only - there is no existing Postgres
# database to upgrade in place, so none of init_db()'s ALTER TABLE/rebuild
# scaffolding is needed here; every table is written directly in its final
# shape. `conn` is any DB-API connection exposing .cursor()/.commit()
# (psycopg2/psycopg - the connection layer itself lands in Phase 2).
#
# Deliberate differences from the SQLite schema (confirmed with the user):
#  - INTEGER 0/1 flags -> BOOLEAN; JSON-in-TEXT columns -> JSONB; TEXT date
#    columns -> DATE; TEXT/TIMESTAMP audit columns -> TIMESTAMPTZ DEFAULT now().
#  - Every FK declared in the SQLite DDL is a REAL, enforced constraint here
#    (SQLite never enabled PRAGMA foreign_keys, so those were advisory only).
#    A column that had NO FK declared in SQLite (error_log.error_code_id,
#    audit_rule_document_fields.master_column_id, trial_balance_accounts.
#    category_id, account_category_assignments.category_id) stays a plain
#    column here too - not a place to invent new enforcement.
#  - data_audit_log.changed_at: get_data_audit_history() (above) queries a
#    changed_at column the SQLite table never actually had (only created_at) -
#    a genuine pre-existing bug. Fixed here by naming the column changed_at.
#  - client_voucher_prefixes.prefix used SQLite's COLLATE NOCASE (no direct
#    Postgres equivalent) - ported via the citext extension instead.
#  - No new CHECK constraints beyond what the SQLite DDL already enforces.
#    Several columns have documented-but-unenforced value sets in code
#    comments elsewhere in this file (range_status, cleared_status,
#    risk_rating, period_type, state, change_detection_status,
#    data_quality_flag) - left as plain typed columns, matching today's
#    actual (unenforced) behavior, since Phase 3-6 hasn't verified every
#    app code path against a stricter constraint yet.
def init_db_postgres(conn):
    cur = conn.cursor()

    def run(sql, label=None):
        cur.execute(sql)
        if label:
            print(f"[postgres] created {label}")

    # citext gives client_voucher_prefixes.prefix the same case-insensitive
    # comparison/uniqueness SQLite's COLLATE NOCASE provided.
    run("CREATE EXTENSION IF NOT EXISTS citext")

    # ---------- 1. master_columns ----------
    run('''
        CREATE TABLE IF NOT EXISTS master_columns (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            column_name TEXT NOT NULL UNIQUE,
            data_type TEXT NOT NULL,
            category TEXT DEFAULT 'General',
            is_active BOOLEAN DEFAULT TRUE,
            notes TEXT
        )
    ''', "master_columns")

    # ---------- 2. templates ----------
    run('''
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            template_name TEXT NOT NULL UNIQUE,
            created_by TEXT DEFAULT 'Admin',
            created_date TIMESTAMPTZ DEFAULT now(),
            is_active BOOLEAN DEFAULT TRUE,
            is_system BOOLEAN DEFAULT FALSE,
            has_subtotals_by_default BOOLEAN DEFAULT FALSE,
            rate_source TEXT
        )
    ''', "templates")

    # ---------- 3. clients ----------
    run('''
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_code TEXT UNIQUE NOT NULL,
            client_name TEXT NOT NULL,
            industry TEXT,
            contact_person TEXT,
            email TEXT,
            phone TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    ''', "clients")

    # ---------- 4. audit_rule_documents ----------
    run('''
        CREATE TABLE IF NOT EXISTS audit_rule_documents (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name TEXT NOT NULL,
            kb_note TEXT,
            template_id INTEGER REFERENCES templates(id),
            is_active BOOLEAN DEFAULT TRUE,
            document_type TEXT NOT NULL DEFAULT 'linked_template'
                CHECK (document_type IN ('linked_template', 'physical_bill')),
            created_at TIMESTAMPTZ DEFAULT now(),
            risk_rating TEXT,
            transaction_level_approval BOOLEAN DEFAULT FALSE
        )
    ''', "audit_rule_documents")

    # ---------- 5. template_column_mappings ----------
    run('''
        CREATE TABLE IF NOT EXISTS template_column_mappings (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
            column_id INTEGER NOT NULL REFERENCES master_columns(id) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE(template_id, column_id)
        )
    ''', "template_column_mappings")

    # ---------- 6. uploaded_files ----------
    run('''
        CREATE TABLE IF NOT EXISTS uploaded_files (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_type TEXT,
            record_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Pending',
            upload_date TIMESTAMPTZ DEFAULT now(),
            client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL
        )
    ''', "uploaded_files")

    # ---------- 7. verifications ----------
    run('''
        CREATE TABLE IF NOT EXISTS verifications (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            file_id INTEGER REFERENCES uploaded_files(id),
            item_id INTEGER,
            status TEXT DEFAULT 'Pending',
            verified_by TEXT,
            verified_date TIMESTAMPTZ,
            comments TEXT
        )
    ''', "verifications")

    # ---------- 8. audit_log ----------
    run('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            "user" TEXT NOT NULL,
            action TEXT NOT NULL,
            module TEXT NOT NULL,
            details TEXT,
            "timestamp" TIMESTAMPTZ DEFAULT now()
        )
    ''', "audit_log")

    # ---------- 9. client_data ----------
    run('''
        CREATE TABLE IF NOT EXISTS client_data (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
            upload_id INTEGER NOT NULL REFERENCES uploaded_files(id) ON DELETE CASCADE,
            row_data JSONB NOT NULL,
            extract_amount REAL,
            extract_date DATE,
            created_at TIMESTAMPTZ DEFAULT now(),
            created_by TEXT DEFAULT 'Analyst',
            is_deleted BOOLEAN DEFAULT FALSE,
            version INTEGER DEFAULT 1,
            data_quality_flag TEXT DEFAULT NULL,
            document_group_id TEXT DEFAULT NULL,
            change_detection_status TEXT DEFAULT 'active',
            rate_source TEXT DEFAULT NULL
        )
    ''', "client_data")
    run("CREATE INDEX IF NOT EXISTS idx_client_data_client_deleted_extract_date ON client_data (client_id, is_deleted, extract_date)")
    run("CREATE INDEX IF NOT EXISTS idx_client_data_client ON client_data (client_id)")
    run("CREATE INDEX IF NOT EXISTS idx_client_data_template ON client_data (template_id)")
    run("CREATE INDEX IF NOT EXISTS idx_client_data_upload ON client_data (upload_id)")
    run("CREATE INDEX IF NOT EXISTS idx_client_data_date ON client_data (extract_date)")
    run("CREATE INDEX IF NOT EXISTS idx_client_data_deleted ON client_data (is_deleted)")
    run("CREATE INDEX IF NOT EXISTS idx_client_data_client_date ON client_data (client_id, extract_date)")

    # ---------- 10. error_codes ----------
    run('''
        CREATE TABLE IF NOT EXISTS error_codes (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            severity TEXT NOT NULL,
            category TEXT NOT NULL,
            user_message TEXT NOT NULL,
            tech_description TEXT,
            suggested_action TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    ''', "error_codes")

    # ---------- 11. error_log ----------
    # error_code_id has no FK in the SQLite source (advisory-only reference to
    # error_codes) - left as a plain column here too, per the "only port
    # declared FKs" decision.
    run('''
        CREATE TABLE IF NOT EXISTS error_log (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
            input_data JSONB,
            suggested_fix TEXT,
            is_resolved BOOLEAN DEFAULT FALSE,
            resolution_notes TEXT,
            resolved_by TEXT,
            resolved_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    ''', "error_log")
    run("CREATE INDEX IF NOT EXISTS idx_error_log_code ON error_log (error_code)")
    run("CREATE INDEX IF NOT EXISTS idx_error_log_module ON error_log (module)")
    run("CREATE INDEX IF NOT EXISTS idx_error_log_created ON error_log (created_at)")
    run("CREATE INDEX IF NOT EXISTS idx_error_log_resolved ON error_log (is_resolved)")
    run("CREATE INDEX IF NOT EXISTS idx_error_log_category ON error_log (error_category)")

    # ---------- 12. verification_sessions ----------
    run('''
        CREATE TABLE IF NOT EXISTS verification_sessions (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            cashbook_name TEXT NOT NULL,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            created_by TEXT DEFAULT 'Analyst',
            created_at TIMESTAMPTZ DEFAULT now(),
            status TEXT DEFAULT 'In Progress',
            total_transactions INTEGER DEFAULT 0,
            verified_count INTEGER DEFAULT 0,
            session_type TEXT NOT NULL DEFAULT 'cash',
            rulebook_version INTEGER
        )
    ''', "verification_sessions")
    run("ALTER TABLE verification_sessions ADD COLUMN IF NOT EXISTS rulebook_version INTEGER", "verification_sessions")
    run("CREATE INDEX IF NOT EXISTS idx_verification_sessions_client ON verification_sessions (client_id)")

    run('''
        CREATE TABLE IF NOT EXISTS verification_presence (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            verification_session_id INTEGER NOT NULL REFERENCES verification_sessions(id) ON DELETE CASCADE,
            session_token TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),
            ended_at TIMESTAMPTZ,
            UNIQUE(verification_session_id, session_token)
        )
    ''', "verification_presence")
    run("CREATE INDEX IF NOT EXISTS idx_verification_presence_live ON verification_presence (last_heartbeat, ended_at)")

    # ---------- 13. verification_details ----------
    run('''
        CREATE TABLE IF NOT EXISTS verification_details (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES verification_sessions(id) ON DELETE CASCADE,
            client_data_id INTEGER NOT NULL REFERENCES client_data(id) ON DELETE CASCADE,
            transaction_ref TEXT,
            line_items JSONB,
            procedure_data JSONB,
            validation_status TEXT DEFAULT 'Pending',
            validation_message TEXT,
            status TEXT DEFAULT 'Pending',
            comments TEXT,
            observations TEXT,
            verified_by TEXT,
            verified_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now(),
            cleared_status TEXT,
            matrix_version_applied INTEGER,
            range_status TEXT NOT NULL DEFAULT 'active',
            excluded_at TIMESTAMPTZ,
            category_id_applied INTEGER,
            category_name_applied TEXT
        )
    ''', "verification_details")
    run("CREATE INDEX IF NOT EXISTS idx_verification_details_session ON verification_details (session_id)")
    run("CREATE INDEX IF NOT EXISTS idx_verification_details_clientdata ON verification_details (client_data_id)")
    run("CREATE INDEX IF NOT EXISTS idx_verification_details_status ON verification_details (status)")
    run("CREATE INDEX IF NOT EXISTS idx_verification_details_validation ON verification_details (validation_status)")
    # category_id_applied/category_name_applied (Freeze Category on Voucher Lines): the live
    # Supabase table already existed before this pair was added to the schema above, so
    # CREATE TABLE IF NOT EXISTS is a no-op against it - an explicit ALTER is needed to reach
    # it, same "don't retroactively reinterpret already-completed work" rationale as
    # matrix_version_applied (see that column's own comment in the SQLite ALTER block).
    run("ALTER TABLE verification_details ADD COLUMN IF NOT EXISTS category_id_applied INTEGER")
    run("ALTER TABLE verification_details ADD COLUMN IF NOT EXISTS category_name_applied TEXT")

    # ---------- 14. audit_procedure_library ----------
    run('''
        CREATE TABLE IF NOT EXISTS audit_procedure_library (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            transaction_type TEXT UNIQUE NOT NULL,
            management_controls TEXT,
            audit_risks TEXT,
            audit_procedures TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    ''', "audit_procedure_library")

    # ---------- 15. app_settings ----------
    run('''
        CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    ''', "app_settings")

    # Durable singleton version for stable Audit Rules/Rulebook configuration. It is kept
    # separate from app_settings so setup backup/restore cannot roll back safety state.
    run('''
        CREATE TABLE IF NOT EXISTS audit_rules_config_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            config_version INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ DEFAULT now()
        )
    ''', "audit_rules_config_state")
    run('''
        INSERT INTO audit_rules_config_state (id, config_version)
        VALUES (1, 1)
        ON CONFLICT (id) DO NOTHING
    ''')
    run('''
        CREATE TABLE IF NOT EXISTS audit_rules_config_changes (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            config_version INTEGER NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            entity_name TEXT,
            action TEXT NOT NULL,
            changed_by TEXT DEFAULT 'System',
            changed_at TIMESTAMPTZ DEFAULT now()
        )
    ''', "audit_rules_config_changes")
    run("CREATE INDEX IF NOT EXISTS idx_audit_rules_config_changes_version ON audit_rules_config_changes (config_version)")

    # ---------- 16. data_audit_log ----------
    # changed_at (not created_at): fixes get_data_audit_history()'s pre-existing
    # "no such column" bug - see the module-level comment above.
    run('''
        CREATE TABLE IF NOT EXISTS data_audit_log (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_data_id INTEGER NOT NULL REFERENCES client_data(id) ON DELETE CASCADE,
            "user" TEXT DEFAULT 'System',
            action TEXT NOT NULL,
            old_row_data JSONB,
            new_row_data JSONB,
            changed_at TIMESTAMPTZ DEFAULT now()
        )
    ''', "data_audit_log")
    run("CREATE INDEX IF NOT EXISTS idx_data_audit_log_clientdata ON data_audit_log (client_data_id)")
    run("CREATE INDEX IF NOT EXISTS idx_data_audit_log_action ON data_audit_log (action)")

    # ---------- 17. client_voucher_prefixes ----------
    run('''
        CREATE TABLE IF NOT EXISTS client_voucher_prefixes (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            category TEXT NOT NULL CHECK (category IN
                ('Cash Payment', 'Cash Receipt', 'Bank Payment', 'Bank Receipt', 'Journal')),
            prefix CITEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(client_id, category, prefix)
        )
    ''', "client_voucher_prefixes")
    run("CREATE INDEX IF NOT EXISTS idx_client_voucher_prefixes_client ON client_voucher_prefixes (client_id)")

    # ---------- 18. saved_column_mappings ----------
    run('''
        CREATE TABLE IF NOT EXISTS saved_column_mappings (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
            template_column_id INTEGER NOT NULL,
            excel_column TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(client_id, template_id, template_column_id)
        )
    ''', "saved_column_mappings")
    run("CREATE INDEX IF NOT EXISTS idx_saved_column_mappings_client_template ON saved_column_mappings (client_id, template_id)")

    # ---------- 19. client_template_upload_frequency ----------
    run('''
        CREATE TABLE IF NOT EXISTS client_template_upload_frequency (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
            frequency TEXT NOT NULL DEFAULT 'Daily' CHECK (frequency IN ('Daily', 'Weekly', 'Monthly')),
            updated_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(client_id, template_id)
        )
    ''', "client_template_upload_frequency")
    run("CREATE INDEX IF NOT EXISTS idx_upload_frequency_client_template ON client_template_upload_frequency (client_id, template_id)")

    # ---------- 20. audit_rule_document_fields ----------
    # master_column_id has no FK in the SQLite source (best-effort resolved
    # link, app-validated) - left as a plain column here too.
    run('''
        CREATE TABLE IF NOT EXISTS audit_rule_document_fields (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES audit_rule_documents(id) ON DELETE CASCADE,
            field_name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            master_column_id INTEGER,
            UNIQUE(document_id, field_name)
        )
    ''', "audit_rule_document_fields")
    run("CREATE INDEX IF NOT EXISTS idx_audit_rule_document_fields_document ON audit_rule_document_fields (document_id)")

    # ---------- 21. audit_rule_document_auth_roles ----------
    run('''
        CREATE TABLE IF NOT EXISTS audit_rule_document_auth_roles (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES audit_rule_documents(id) ON DELETE CASCADE,
            role_name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            required_authority TEXT,
            UNIQUE(document_id, role_name)
        )
    ''', "audit_rule_document_auth_roles")
    run("CREATE INDEX IF NOT EXISTS idx_audit_rule_document_auth_roles_document ON audit_rule_document_auth_roles (document_id)")

    # ---------- 22. audit_rule_cross_checks ----------
    run('''
        CREATE TABLE IF NOT EXISTS audit_rule_cross_checks (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name TEXT NOT NULL,
            compare_field TEXT NOT NULL,
            comparison_type TEXT NOT NULL CHECK (comparison_type IN
                ('All equal', 'Sequential increasing', 'Sequential decreasing',
                 'Within tolerance %', 'Must be equal', 'May be less, never more')),
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT now(),
            compare_to_voucher_line BOOLEAN DEFAULT FALSE
        )
    ''', "audit_rule_cross_checks")

    # ---------- 23. audit_rule_cross_check_documents ----------
    run('''
        CREATE TABLE IF NOT EXISTS audit_rule_cross_check_documents (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            cross_check_id INTEGER NOT NULL REFERENCES audit_rule_cross_checks(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES audit_rule_documents(id) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            compare_field TEXT,
            UNIQUE(cross_check_id, document_id)
        )
    ''', "audit_rule_cross_check_documents")
    run("CREATE INDEX IF NOT EXISTS idx_audit_rule_cross_check_documents_crosscheck ON audit_rule_cross_check_documents (cross_check_id)")
    run("CREATE INDEX IF NOT EXISTS idx_audit_rule_cross_check_documents_document ON audit_rule_cross_check_documents (document_id)")
    # Cross-check per-document field names (Phase 1 of 4): per-Document compared field,
    # matched by chain position not identical name. NULL means "use the parent Cross-check's
    # shared compare_field". Added for existing databases, then backfilled from that shared
    # value so no existing Cross-check breaks.
    run("ALTER TABLE audit_rule_cross_check_documents ADD COLUMN IF NOT EXISTS compare_field TEXT")
    run('''
        UPDATE audit_rule_cross_check_documents ccd
        SET compare_field = c.compare_field
        FROM audit_rule_cross_checks c
        WHERE c.id = ccd.cross_check_id AND ccd.compare_field IS NULL
    ''')

    # ---------- 24. audit_rule_categories ----------
    run('''
        CREATE TABLE IF NOT EXISTS audit_rule_categories (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name TEXT NOT NULL,
            kb_overview TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            evidence_type TEXT CHECK (evidence_type IN ('billable', 'non_billable')),
            created_at TIMESTAMPTZ DEFAULT now(),
            matrix_version INTEGER NOT NULL DEFAULT 1
        )
    ''', "audit_rule_categories")

    # ---------- 25. audit_rule_category_documents ----------
    run('''
        CREATE TABLE IF NOT EXISTS audit_rule_category_documents (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            category_id INTEGER NOT NULL REFERENCES audit_rule_categories(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES audit_rule_documents(id) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_required BOOLEAN NOT NULL DEFAULT TRUE,
            UNIQUE(category_id, document_id)
        )
    ''', "audit_rule_category_documents")
    run("CREATE INDEX IF NOT EXISTS idx_audit_rule_category_documents_category ON audit_rule_category_documents (category_id)")
    run("CREATE INDEX IF NOT EXISTS idx_audit_rule_category_documents_document ON audit_rule_category_documents (document_id)")

    # ---------- 26. audit_rule_category_cross_checks ----------
    run('''
        CREATE TABLE IF NOT EXISTS audit_rule_category_cross_checks (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            category_id INTEGER NOT NULL REFERENCES audit_rule_categories(id) ON DELETE CASCADE,
            cross_check_id INTEGER NOT NULL REFERENCES audit_rule_cross_checks(id) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE(category_id, cross_check_id)
        )
    ''', "audit_rule_category_cross_checks")
    run("CREATE INDEX IF NOT EXISTS idx_audit_rule_category_cross_checks_category ON audit_rule_category_cross_checks (category_id)")
    run("CREATE INDEX IF NOT EXISTS idx_audit_rule_category_cross_checks_crosscheck ON audit_rule_category_cross_checks (cross_check_id)")

    # ---------- 27. signature_specimens ----------
    # No table-level UNIQUE (deliberate - matches the SQLite final shape,
    # which relaxed this so a new specimen can replace an inactive one;
    # uniqueness among ACTIVE rows is enforced in application code).
    run('''
        CREATE TABLE IF NOT EXISTS signature_specimens (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES audit_rule_documents(id) ON DELETE CASCADE,
            role_name TEXT NOT NULL,
            expected_signatory_name TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            authority TEXT
        )
    ''', "signature_specimens")
    run("CREATE INDEX IF NOT EXISTS idx_signature_specimens_client ON signature_specimens (client_id)")
    run("CREATE INDEX IF NOT EXISTS idx_signature_specimens_document ON signature_specimens (document_id)")

    # ---------- 28. holidays ----------
    run('''
        CREATE TABLE IF NOT EXISTS holidays (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            holiday_date DATE NOT NULL,
            label TEXT NOT NULL,
            repeats_yearly BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    ''', "holidays")
    run("CREATE INDEX IF NOT EXISTS idx_holidays_date ON holidays (holiday_date)")

    # ---------- 29. trial_balance_uploads ----------
    run('''
        CREATE TABLE IF NOT EXISTS trial_balance_uploads (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            as_of_date DATE NOT NULL,
            file_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'parsed', 'error')),
            row_count INTEGER DEFAULT 0,
            notes TEXT,
            uploaded_at TIMESTAMPTZ DEFAULT now(),
            period_start DATE,
            period_end DATE,
            period_type TEXT
        )
    ''', "trial_balance_uploads")
    run("CREATE INDEX IF NOT EXISTS idx_trial_balance_uploads_client ON trial_balance_uploads (client_id)")

    # ---------- 30. trial_balance_accounts ----------
    # category_id has no FK in the SQLite source (soft link to
    # audit_rule_categories.id) - left as a plain column here too.
    run('''
        CREATE TABLE IF NOT EXISTS trial_balance_accounts (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            upload_id INTEGER NOT NULL REFERENCES trial_balance_uploads(id) ON DELETE CASCADE,
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
            manually_overridden BOOLEAN NOT NULL DEFAULT FALSE,
            category_id INTEGER,
            sort_order INTEGER NOT NULL DEFAULT 0,
            raw_row_data JSONB,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(upload_id, account_code)
        )
    ''', "trial_balance_accounts")
    run("CREATE INDEX IF NOT EXISTS idx_trial_balance_accounts_upload ON trial_balance_accounts (upload_id)")
    run("CREATE INDEX IF NOT EXISTS idx_trial_balance_accounts_parent ON trial_balance_accounts (upload_id, parent_account_code)")

    # ---------- 31. account_category_assignments ----------
    # category_id has no FK in the SQLite source - left as a plain column here too.
    run('''
        CREATE TABLE IF NOT EXISTS account_category_assignments (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            account_code TEXT NOT NULL,
            category_id INTEGER,
            updated_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(client_id, account_code)
        )
    ''', "account_category_assignments")
    run("CREATE INDEX IF NOT EXISTS idx_account_category_assignments_client ON account_category_assignments (client_id)")

    # ---------- 32. voucher_line_documents ----------
    run('''
        CREATE TABLE IF NOT EXISTS voucher_line_documents (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            voucher_line_id INTEGER NOT NULL REFERENCES verification_details(id) ON DELETE CASCADE,
            client_data_id INTEGER NOT NULL REFERENCES client_data(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES audit_rule_documents(id),
            linked_by TEXT DEFAULT 'Analyst',
            linked_at TIMESTAMPTZ DEFAULT now(),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'unassigned')),
            unassigned_at TIMESTAMPTZ,
            signature_exception BOOLEAN DEFAULT FALSE
        )
    ''', "voucher_line_documents")
    run("CREATE INDEX IF NOT EXISTS idx_voucher_line_documents_line ON voucher_line_documents (voucher_line_id)")
    run("CREATE INDEX IF NOT EXISTS idx_voucher_line_documents_active_line ON voucher_line_documents (voucher_line_id) WHERE status = 'active'")
    run('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_voucher_line_documents_active_client_data
        ON voucher_line_documents (client_data_id) WHERE status = 'active'
    ''')

    # ---------- 33. voucher_line_procedure_checks ----------
    run('''
        CREATE TABLE IF NOT EXISTS voucher_line_procedure_checks (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            voucher_line_id INTEGER NOT NULL REFERENCES verification_details(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES audit_rule_documents(id),
            role_name TEXT NOT NULL,
            is_checked BOOLEAN NOT NULL DEFAULT FALSE,
            checked_by TEXT,
            checked_at TIMESTAMPTZ,
            UNIQUE(voucher_line_id, document_id, role_name)
        )
    ''', "voucher_line_procedure_checks")
    run("CREATE INDEX IF NOT EXISTS idx_voucher_line_procedure_checks_line ON voucher_line_procedure_checks (voucher_line_id)")

    # ---------- 34. voucher_line_physical_bills ----------
    run('''
        CREATE TABLE IF NOT EXISTS voucher_line_physical_bills (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            voucher_line_id INTEGER NOT NULL REFERENCES verification_details(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES audit_rule_documents(id),
            bill_no TEXT,
            bill_date DATE,
            vendor_name TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
    ''', "voucher_line_physical_bills")
    run("CREATE INDEX IF NOT EXISTS idx_voucher_line_physical_bills_line ON voucher_line_physical_bills (voucher_line_id)")

    # ---------- 35. voucher_line_physical_bill_items ----------
    run('''
        CREATE TABLE IF NOT EXISTS voucher_line_physical_bill_items (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            bill_id INTEGER NOT NULL REFERENCES voucher_line_physical_bills(id) ON DELETE CASCADE,
            item_name TEXT,
            quantity REAL,
            rate REAL,
            amount REAL,
            source_document_row_id INTEGER,
            source_template TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
    ''', "voucher_line_physical_bill_items")
    run("CREATE INDEX IF NOT EXISTS idx_voucher_line_physical_bill_items_bill ON voucher_line_physical_bill_items (bill_id)")

    # ---------- 36. voucher_approvals ----------
    # Superseded/dead (see CLAUDE.md), ported anyway for parity with the
    # SQLite schema - no reads/writes hit it, so its presence is inert.
    run('''
        CREATE TABLE IF NOT EXISTS voucher_approvals (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            transaction_ref TEXT NOT NULL,
            prepared_by TEXT,
            checked_by TEXT,
            approved_by TEXT,
            updated_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(client_id, transaction_ref)
        )
    ''', "voucher_approvals")
    run("CREATE INDEX IF NOT EXISTS idx_voucher_approvals_client_ref ON voucher_approvals (client_id, transaction_ref)")

    # ---------- 37. audit_rule_voucher_checks ----------
    run('''
        CREATE TABLE IF NOT EXISTS audit_rule_voucher_checks (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT now(),
            authority TEXT
        )
    ''', "audit_rule_voucher_checks")

    # ---------- 38. audit_rule_voucher_check_auth_roles ----------
    run('''
        CREATE TABLE IF NOT EXISTS audit_rule_voucher_check_auth_roles (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            voucher_check_id INTEGER NOT NULL REFERENCES audit_rule_voucher_checks(id) ON DELETE CASCADE,
            role_name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE(voucher_check_id, role_name)
        )
    ''', "audit_rule_voucher_check_auth_roles")
    run("CREATE INDEX IF NOT EXISTS idx_audit_rule_voucher_check_auth_roles_check ON audit_rule_voucher_check_auth_roles (voucher_check_id)")

    # ---------- 39. voucher_check_status ----------
    run('''
        CREATE TABLE IF NOT EXISTS voucher_check_status (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            transaction_ref TEXT NOT NULL,
            check_id INTEGER NOT NULL REFERENCES audit_rule_voucher_checks(id),
            ticked BOOLEAN NOT NULL DEFAULT FALSE,
            performed_by_name TEXT,
            updated_at TIMESTAMPTZ DEFAULT now(),
            state TEXT NOT NULL DEFAULT 'untouched',
            flagged_in_observations BOOLEAN NOT NULL DEFAULT FALSE,
            UNIQUE(client_id, transaction_ref, check_id)
        )
    ''', "voucher_check_status")
    run("CREATE INDEX IF NOT EXISTS idx_voucher_check_status_client_ref ON voucher_check_status (client_id, transaction_ref)")

    # ---------- 40. voucher_line_observations ----------
    run('''
        CREATE TABLE IF NOT EXISTS voucher_line_observations (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            voucher_line_id INTEGER NOT NULL REFERENCES verification_details(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES audit_rule_documents(id),
            observation_text TEXT,
            synced BOOLEAN NOT NULL DEFAULT FALSE,
            last_synced_text TEXT,
            updated_by TEXT DEFAULT 'Analyst',
            updated_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(voucher_line_id, document_id)
        )
    ''', "voucher_line_observations")
    run("CREATE INDEX IF NOT EXISTS idx_voucher_line_observations_line ON voucher_line_observations (voucher_line_id)")

    # ---------- 41. users ----------
    run('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            role TEXT,
            last_login TIMESTAMPTZ
        )
    ''', "users")

    # ---------- 42. sessions ----------
    # token stays a plain TEXT PRIMARY KEY (app-generated, not sequential) -
    # the one table in this schema with a non-identity primary key.
    run('''
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expiry TIMESTAMPTZ NOT NULL,
            selected_client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL
        )
    ''', "sessions")
    run("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id)")

    conn.commit()
    print("[postgres] all tables/indexes created")

    _seed_postgres_data(conn)

    conn.commit()
    print("[postgres] schema initialization complete")
    global _POSTGRES_SCHEMA_READY
    _POSTGRES_SCHEMA_READY = True


def _seed_postgres_data(conn):
    """
    Seed data for a fresh Supabase/Postgres database - the same seed content
    init_db() inserts for SQLite (error_codes, audit_procedure_library,
    master_columns defaults, the "Transaction Report" system template + Rate
    column wiring, app_settings, the 7 legacy Audit Rules categories, and the
    admin user), using INSERT ... ON CONFLICT DO NOTHING for idempotency
    instead of SQLite's "count == 0" checks.
    """
    cur = conn.cursor()

    # ---- error_codes (16 rows) ----
    error_codes = [
        ("ERR-SYN-001", "Error", "Syntax",
         "The system tried to use something that doesn't exist – like a missing function or variable.",
         "NameError: name '...' is not defined",
         "Check the spelling of the function/variable name. Make sure it is imported or defined before use."),
        ("ERR-SYN-002", "Error", "Syntax",
         "The system encountered a syntax error – a missing bracket, comma, or a typo in the code.",
         "SyntaxError: invalid syntax",
         "Check the code for missing brackets '()', '{}', '[]', or missing commas. Look at the highlighted line in the error report."),
        ("ERR-IMP-001", "Error", "Import",
         "The system could not import a required module or function.",
         "ImportError: No module named '...'",
         "Check if the module is installed (e.g., with `pip install`). If it's a local file, make sure the file name and path are correct."),
        ("ERR-IMP-002", "Error", "Import",
         "The system tried to import something that doesn't exist in the module.",
         "ImportError: cannot import name '...'",
         "Check the spelling of the function/class name. Make sure it is exported in the module."),
        ("ERR-DB-001", "Fatal", "Database",
         "The system could not connect to the database. This might be because the database file is missing or corrupted.",
         "sqlite3.OperationalError: unable to open database file",
         "Check that the database file exists in the correct location. Restart the server and try again."),
        ("ERR-DB-002", "Error", "Database",
         "A database constraint was violated – e.g., trying to insert a duplicate unique value.",
         "sqlite3.IntegrityError: UNIQUE constraint failed",
         "Check the data you are trying to insert. Make sure no duplicate values exist in unique columns (like Invoice Number)."),
        ("ERR-DB-003", "Error", "Database",
         "The database operation failed because the table structure doesn't match the query.",
         "sqlite3.OperationalError: no such table/column",
         "This may happen after upgrading the app. Try restarting the server and check if the database schema is up to date."),
        ("ERR-NET-001", "Fatal", "Network",
         "The system could not connect to the server or the server is not responding.",
         "ConnectionError: Failed to connect to server",
         "Make sure the server is running. Check your internet connection and firewall settings."),
        ("ERR-NET-002", "Error", "Network",
         "The server took too long to respond (timeout).",
         "TimeoutError: Request timed out",
         "Try again later. If this keeps happening, check the server load and network speed."),
        ("ERR-VAL-001", "Warning", "Validation",
         "The data you entered is incorrect – e.g., missing required fields, invalid format, or duplicate value.",
         "ValidationError: Invalid input",
         "Check your input data. Make sure all required fields are filled and in the correct format. For duplicates, use a different unique value."),
        ("ERR-VAL-002", "Warning", "Validation",
         "The file you uploaded is not supported. Only Excel files (.xlsx, .xls) are allowed.",
         "HTTPException: Only .xlsx and .xls files are supported",
         "Convert your file to .xlsx or .xls format and try again."),
        ("ERR-VAL-003", "Warning", "Validation",
         "A required unique column was not selected for update mode. Please select a unique column.",
         "HTTPException: Unique ID column is required when mode is 'Update Existing'",
         "In the upload mapping, select a column (like Invoice Number) as the 'Unique ID'."),
        ("ERR-SYS-001", "Fatal", "System",
         "A critical system error occurred. This could be due to a bug in the code or a missing file.",
         "Internal Server Error (500)",
         "Restart the server. If the problem persists, contact your developer and provide the error report."),
        ("ERR-SYS-002", "Error", "System",
         "The system ran out of memory or resources.",
         "MemoryError",
         "Restart the server. If you are processing large files, try splitting them into smaller chunks."),
        ("ERR-UNK-001", "Error", "Unknown",
         "An unknown error occurred. We don't yet have specific information about this error.",
         "Unknown error",
         "Please use the 'Report Error' button to send this error to your developer for investigation."),
    ]
    cur.executemany(
        """
        INSERT INTO error_codes (code, severity, category, user_message, tech_description, suggested_action)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (code) DO NOTHING
        """,
        error_codes
    )
    print(f"[postgres] seeded error_codes ({len(error_codes)} rows)")

    # ---- audit_procedure_library (7 rows) ----
    library_data = [
        ("Material Purchased",
         "Authorized Purchase demand; Approved Purchase order; Signed Gate pass; Inspection note; Store receiving; Vendor bill",
         "Unauthorized purchases; Inflated quantities; Unapproved vendors; Missing GRN",
         "Verify purchase demand authorization; Check PO approval; Inspect gate pass; Review inspection note; Match GRN with PO; Compare vendor bill with PO/GRN; Reconcile quantities and amounts"),
        ("Repair & Maintenance (In-house)",
         "Authorized Purchase demand; Signature of relevant supervisor on bill; Vendor bill",
         "Unapproved repairs; Overcharging; Duplicate bills",
         "Check purchase demand authorization; Verify supervisor signature on bill; Review vendor bill for reasonableness; Compare with maintenance log if available"),
        ("Fuel Expense",
         "Vehicle log; Fuel receipts; Mileage records",
         "Unexplained fuel consumption; Theft; Unauthorized vehicles",
         "Reconcile fuel receipts with vehicle log; Check mileage against fuel consumption; Verify authorized vehicles"),
        ("Short Term Advance against Salary",
         "Approved advance request; Employee acknowledgment; Payroll deduction schedule",
         "Unauthorized advances; Non-recovery; Duplicate advances",
         "Verify approved advance request; Check employee acknowledgment; Review deduction schedule; Confirm with payroll records"),
        ("Payment to Contractors",
         "Signed contract; Work completion certificate; Tax deduction certificate; Vendor bill",
         "Fake contractors; Unauthorized work; Overpayment; Non-deduction of tax",
         "Review contract; Inspect work completion certificate; Verify tax deduction; Match vendor bill with contract"),
        ("Fixed Asset Purchased",
         "Capital budget approval; Purchase order; Gate pass; Inspection note; Asset tagging",
         "Unauthorized purchase; Misclassification as expense; Incorrect valuation; Missing asset",
         "Check capital budget approval; Verify PO; Inspect gate pass; Review inspection note; Ensure asset tagged and entered in fixed asset register"),
        ("Kitchen Expenses",
         "Daily consumption report; Purchase invoices; Inventory count",
         "Theft; Over-purchasing; Unauthorized items; Missing invoices",
         "Reconcile purchase invoices with consumption report; Verify inventory count; Check for approved purchase; Review daily consumption logs"),
    ]
    cur.executemany(
        """
        INSERT INTO audit_procedure_library (transaction_type, management_controls, audit_risks, audit_procedures)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (transaction_type) DO NOTHING
        """,
        library_data
    )
    print(f"[postgres] seeded audit_procedure_library ({len(library_data)} rows)")

    # ---- master_columns defaults (14 rows) ----
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
        ('Reference', 'Text', 'General'),
    ]
    cur.executemany(
        """
        INSERT INTO master_columns (column_name, data_type, category)
        VALUES (%s, %s, %s)
        ON CONFLICT (column_name) DO NOTHING
        """,
        default_columns
    )
    print(f"[postgres] seeded master_columns defaults ({len(default_columns)} rows)")

    # ---- system template: Transaction Report + its 7 columns ----
    cur.execute("SELECT id FROM templates WHERE template_name = 'Transaction Report'")
    tpl_row = cur.fetchone()
    if not tpl_row:
        cur.execute(
            "INSERT INTO templates (template_name, created_by, is_system, has_subtotals_by_default) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            ("Transaction Report", "System", True, True)
        )
        tpl_id = cur.fetchone()[0]
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
            cur.execute("SELECT id FROM master_columns WHERE column_name = %s", (col_name,))
            row = cur.fetchone()
            if row:
                col_id = row[0]
            else:
                cur.execute(
                    "INSERT INTO master_columns (column_name, data_type, category) VALUES (%s, %s, %s) RETURNING id",
                    (col_name, data_type, category)
                )
                col_id = cur.fetchone()[0]
            col_ids.append(col_id)
        for idx, col_id in enumerate(col_ids):
            cur.execute(
                "INSERT INTO template_column_mappings (template_id, column_id, sort_order) VALUES (%s, %s, %s)",
                (tpl_id, col_id, idx)
            )
        print("[postgres] seeded system template: Transaction Report")
    else:
        tpl_id = tpl_row[0]
        cur.execute(
            "UPDATE templates SET is_system = TRUE, has_subtotals_by_default = TRUE WHERE id = %s",
            (tpl_id,)
        )

    # ---- Rate column, attached after Quantity on Purchase Order / Store
    # Receiving / Vendor Bill (mirrors the SQLite retroactive-attach block) ----
    cur.execute("SELECT id FROM master_columns WHERE column_name = 'Rate'")
    rate_row = cur.fetchone()
    if rate_row:
        rate_col_id = rate_row[0]
    else:
        cur.execute("SELECT category FROM master_columns WHERE column_name = 'Amount'")
        amount_row = cur.fetchone()
        rate_category = amount_row[0] if amount_row and amount_row[0] else 'General'
        cur.execute(
            "INSERT INTO master_columns (column_name, data_type, category) VALUES (%s, %s, %s) RETURNING id",
            ('Rate', 'Numeric', rate_category)
        )
        rate_col_id = cur.fetchone()[0]
        print("[postgres] created column: Rate")

    rate_eligible_template_names = ('purchase order', 'store receiving', 'vendor bill')
    cur.execute(
        "SELECT id, template_name FROM templates WHERE LOWER(template_name) IN (%s, %s, %s)",
        rate_eligible_template_names
    )
    eligible_templates = cur.fetchall()
    for tpl_row_id, tpl_name in eligible_templates:
        cur.execute(
            "SELECT 1 FROM template_column_mappings WHERE template_id = %s AND column_id = %s",
            (tpl_row_id, rate_col_id)
        )
        if cur.fetchone():
            continue

        cur.execute(
            "SELECT column_id FROM template_column_mappings WHERE template_id = %s ORDER BY sort_order ASC",
            (tpl_row_id,)
        )
        existing_col_ids = [row[0] for row in cur.fetchall()]

        cur.execute("SELECT id FROM master_columns WHERE column_name = 'Quantity'")
        qty_row = cur.fetchone()
        if not qty_row or qty_row[0] not in existing_col_ids:
            print(f"[postgres] skipped attaching Rate to template '{tpl_name}': no Quantity column found")
            continue
        qty_col_id = qty_row[0]

        qty_index = existing_col_ids.index(qty_col_id)
        new_col_ids = existing_col_ids[:qty_index + 1] + [rate_col_id] + existing_col_ids[qty_index + 1:]

        cur.execute("DELETE FROM template_column_mappings WHERE template_id = %s", (tpl_row_id,))
        for idx, col_id in enumerate(new_col_ids):
            cur.execute(
                "INSERT INTO template_column_mappings (template_id, column_id, sort_order) VALUES (%s, %s, %s)",
                (tpl_row_id, col_id, idx)
            )
        print(f"[postgres] attached Rate column to template '{tpl_name}' (positioned after Quantity)")

    # ---- app_settings (6 keys) ----
    default_settings = [
        ('date_format', 'DD/MM/YYYY', 'Global date format for display'),
        ('amount_grouping', '000', 'Amount grouping: 000 (thousands) or Lacs'),
        ('decimal_precision', '0', 'Default decimal places for amounts (0, 2, 4)'),
        ('rate_precision', '0', 'Decimal places for derived/imported Rate column (0, 2, 4)'),
        ('default_date_range', 'Current Month', 'Default date range for dashboards'),
        ('materiality_threshold', '50000', 'Threshold for flagging high-value items'),
    ]
    cur.executemany(
        """
        INSERT INTO app_settings (key, value, description)
        VALUES (%s, %s, %s)
        ON CONFLICT (key) DO NOTHING
        """,
        default_settings
    )
    print(f"[postgres] seeded app_settings ({len(default_settings)} rows)")

    # ---- audit_rule_categories: 7 legacy transaction types (KB text only) ----
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
        cur.execute("SELECT id FROM audit_rule_categories WHERE name = %s", (cat_name,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO audit_rule_categories (name, kb_overview) VALUES (%s, %s)",
                (cat_name, kb_overview)
            )
            migrated_count += 1
    if migrated_count:
        print(f"[postgres] seeded {migrated_count} legacy transaction type(s) into Audit Rules Categories")

    # ---- admin user (username 'admin', placeholder password 'admin123') ----
    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        admin_password_hash = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode()
        cur.execute(
            "INSERT INTO users (username, password_hash, is_active, role, last_login) VALUES (%s, %s, %s, %s, %s)",
            ("admin", admin_password_hash, True, "admin", None)
        )
        print("[postgres] seeded admin user")


# ============================================================================
# POSTGRES (SUPABASE) CONNECTION LAYER - Phase 2 of 8 of the Supabase/Render
# migration.
# ============================================================================
# Pooled, env-var-driven Postgres equivalents of the SQLite helpers above
# (get_db/execute_query/execute_write/get_raw_conn). Purely additive: no
# SQLite function is touched, DB_NAME/get_db()/execute_query()/execute_write()/
# get_raw_conn()/init_db() all behave exactly as before. These functions exist
# so a Supabase project can actually be connected to and initialized (see
# main.py's startup handler) - Phase 3+ is what migrates individual routers
# over to calling these instead of the SQLite versions.
#
# Uses a psycopg2 ThreadedConnectionPool (not SQLAlchemy) to match this
# codebase's existing raw-SQL-plus-dict-rows style. Connections are borrowed
# from the pool and returned (never closed) when done - RealDictCursor is
# what gives rows dict-like access, the Postgres analogue of sqlite3.Row.
#
# Postgres uses %s placeholders (not SQLite's ?) - callers write native
# psycopg2-style SQL, same convention _seed_postgres_data() (above) already
# uses.
_pg_pool = None
_pg_transaction = contextvars.ContextVar("_pg_transaction", default=None)


class _ExistingConnection:
    def __init__(self, conn):
        self.conn = conn
    def __enter__(self):
        return self.conn
    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


@contextmanager
def pg_transaction():
    """Borrow one pooled connection and make all PG helpers share its transaction."""
    existing = _pg_transaction.get()
    if existing is not None:
        yield existing
        return
    conn = get_pg_pool().getconn()
    token = _pg_transaction.set(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pg_transaction.reset(token)
        get_pg_pool().putconn(conn)


def get_pg_pool():
    """
    Lazily create and cache a module-level connection pool on first call.
    Raises a clear error if DATABASE_URL isn't set - callers (main.py's
    startup wiring) decide whether that's fatal or just logged, this
    function itself doesn't swallow the error.
    """
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set - copy .env.example to .env and fill in "
            "your Supabase connection string, or set it as a real environment "
            "variable (Render, Phase 7)."
        )

    min_conn = int(os.environ.get("PG_POOL_MIN", "1"))
    max_conn = int(os.environ.get("PG_POOL_MAX", "15"))
    _pg_pool = psycopg2.pool.ThreadedConnectionPool(min_conn, max_conn, dsn=database_url)
    print(f"[postgres] connection pool created (min={min_conn}, max={max_conn})")
    return _pg_pool


class _PooledConnection:
    """
    Context-manager wrapper returned by get_db_pg(): `with get_db_pg() as conn:`
    borrows a connection from the pool and returns it (never closes it) when
    the block exits - the pooled analogue of SQLite's get_db(), which opens
    and closes a real file connection every call.
    """
    def __init__(self):
        self._pool = get_pg_pool()
        self._conn = self._pool.getconn()

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self._conn.rollback()
        self._pool.putconn(self._conn)
        return False


def get_db_pg():
    """Postgres equivalent of get_db() - use as `with get_db_pg() as conn:`."""
    return _PooledConnection()


# === OPT-IN PERFORMANCE DIAGNOSTICS ========================================
# Request handlers explicitly enable this context when their `perf=1` query
# parameter is present. Normal production traffic pays only one ContextVar
# lookup per DB helper call and produces no diagnostic log output.
_db_stats_var = contextvars.ContextVar("_db_stats_var", default=None)


def start_db_metrics():
    """Start request-local DB metrics and return the token needed to restore context."""
    return _db_stats_var.set({"calls": 0, "reads": 0, "writes": 0, "db_time": 0.0})


def get_db_metrics():
    """Return a copy so callers cannot mutate the request-local counters."""
    stats = _db_stats_var.get()
    return dict(stats) if stats is not None else {"calls": 0, "reads": 0, "writes": 0, "db_time": 0.0}


def stop_db_metrics(token):
    """Restore the previous context, including after an exception."""
    _db_stats_var.reset(token)


def _instrument_db_call(kind):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            stats = _db_stats_var.get()
            if stats is None:
                return fn(*args, **kwargs)
            started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                stats["calls"] += 1
                stats[kind] += 1
                stats["db_time"] += time.perf_counter() - started
        return wrapper
    return decorator
# === END OPT-IN PERFORMANCE DIAGNOSTICS ====================================


@_instrument_db_call("reads")
def execute_query_pg(query, params=None):
    """
    Postgres equivalent of execute_query(): borrows a pooled connection, runs
    a SELECT with a RealDictCursor, returns a list of dicts, returns the
    connection to the pool. Uses %s placeholders (psycopg2 native), not
    SQLite's ?.
    """
    active = _pg_transaction.get()
    with (get_db_pg() if active is None else _ExistingConnection(active)) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params or ())
            return [dict(row) for row in cur.fetchall()]


@_instrument_db_call("writes")
def execute_write_pg(query, params=None):
    """
    Postgres equivalent of execute_write(): borrows a pooled connection, runs
    an INSERT/UPDATE/DELETE, commits, returns the connection to the pool.
    Postgres has no sqlite3-style lastrowid - a caller that needs the new
    row's id should add RETURNING id to its own query (already the pattern
    _seed_postgres_data() above uses) and read it off the first returned row.
    """
    active = _pg_transaction.get()
    with (get_db_pg() if active is None else _ExistingConnection(active)) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            if active is None:
                conn.commit()
            if cur.description:
                row = cur.fetchone()
                return row[0] if row else None
            return None


def is_postgres_schema_ready() -> bool:
    """Return whether startup completed the Postgres schema initialization."""
    return _POSTGRES_SCHEMA_READY


def get_persisted_audit_rules_config_version() -> int | None:
    """Read the durable Rulebook configuration version from the active database."""
    if _POSTGRES_SCHEMA_READY:
        rows = execute_query_pg(
            "SELECT config_version FROM audit_rules_config_state WHERE id = 1"
        )
    else:
        try:
            rows = execute_query(
                "SELECT config_version FROM audit_rules_config_state WHERE id = 1"
            )
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise
    return int(rows[0]["config_version"]) if rows else None


def bump_persisted_audit_rules_config_version() -> int | None:
    """Atomically advance and return the durable Rulebook configuration version."""
    if _POSTGRES_SCHEMA_READY:
        return execute_write_pg(
            "UPDATE audit_rules_config_state "
            "SET config_version = config_version + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = 1 RETURNING config_version"
        )

    try:
        execute_write(
            "UPDATE audit_rules_config_state "
            "SET config_version = config_version + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = 1"
        )
        return get_persisted_audit_rules_config_version()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise


def record_audit_rules_config_change(config_version: int, entity_type: str,
                                     entity_id=None, entity_name=None,
                                     action: str = "Updated", changed_by: str = "System"):
    """Record the stable Rulebook change that produced a configuration version."""
    if _POSTGRES_SCHEMA_READY:
        execute_write_pg(
            "INSERT INTO audit_rules_config_changes "
            "(config_version, entity_type, entity_id, entity_name, action, changed_by) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (config_version, entity_type, entity_id, entity_name, action, changed_by)
        )
        return
    try:
        execute_write(
            "INSERT INTO audit_rules_config_changes "
            "(config_version, entity_type, entity_id, entity_name, action, changed_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (config_version, entity_type, entity_id, entity_name, action, changed_by)
        )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise


def get_audit_rules_config_changes_since(config_version: int):
    """Return Rulebook changes newer than a session's captured version."""
    if _POSTGRES_SCHEMA_READY:
        return execute_query_pg(
            "SELECT config_version, entity_type, entity_id, entity_name, action, changed_by, changed_at "
            "FROM audit_rules_config_changes WHERE config_version > %s "
            "ORDER BY config_version, id",
            (config_version,)
        )
    try:
        return execute_query(
            "SELECT config_version, entity_type, entity_id, entity_name, action, changed_by, changed_at "
            "FROM audit_rules_config_changes WHERE config_version > ? "
            "ORDER BY config_version, id",
            (config_version,)
        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise


def get_raw_conn_pg():
    """
    Postgres equivalent of get_raw_conn(): borrows a connection for a caller
    that wants to manage its own transaction/cursor directly (e.g.
    init_db_postgres(), which expects a conn with .cursor()/.commit()). Pair
    with release_raw_conn_pg(conn) instead of conn.close() - closing a pooled
    connection directly would just discard it rather than returning it.
    """
    return _pg_transaction.get() or get_pg_pool().getconn()


def release_raw_conn_pg(conn):
    """Returns a connection borrowed via get_raw_conn_pg() back to the pool."""
    if conn is not _pg_transaction.get():
        get_pg_pool().putconn(conn)


def close_pg_pool():
    """
    Closes every pooled connection. Only meant for process shutdown - not
    called anywhere yet (no shutdown hook exists this phase).
    """
    global _pg_pool
    if _pg_pool is not None:
        _pg_pool.closeall()
        _pg_pool = None
        print("[postgres] connection pool closed")


def get_client_voucher_prefixes_pg(client_id):
    """
    Postgres equivalent of get_client_voucher_prefixes() - same shape/return
    (see that function's docstring), via execute_query_pg().
    """
    rows = execute_query_pg(
        "SELECT category, prefix FROM client_voucher_prefixes WHERE client_id = %s ORDER BY category, prefix",
        (client_id,)
    )
    result = {category: [] for category in VOUCHER_PREFIX_CATEGORIES}
    for row in rows:
        result[row["category"]].append(row["prefix"])
    return result


def save_client_voucher_prefixes_pg(client_id, prefixes):
    """
    Postgres equivalent of save_client_voucher_prefixes() - same replace-all
    -in-one-transaction semantics, via a raw pooled connection instead of
    SQLite's lock-retry loop (Postgres doesn't need that retry pattern - a
    real transaction just blocks/serializes instead of raising "database is
    locked").
    """
    conn = get_raw_conn_pg()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM client_voucher_prefixes WHERE client_id = %s", (client_id,))
        for category in VOUCHER_PREFIX_CATEGORIES:
            for prefix in prefixes.get(category, []):
                cur.execute(
                    "INSERT INTO client_voucher_prefixes (client_id, category, prefix) VALUES (%s, %s, %s)",
                    (client_id, category, prefix)
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_raw_conn_pg(conn)


def execute_batch_pg(conn, query, params_list, chunk_size=500):
    """
    Postgres equivalent of execute_batch(): runs `query` once per params tuple
    via psycopg2.extras.execute_batch(), in chunks of chunk_size, committing
    after each chunk - same partial-durability contract as the SQLite version
    (a chunk that raises rolls back only that chunk; already-committed chunks
    stay committed). For UPDATE/plain-INSERT batches with no ids to collect -
    see execute_batch_insert_with_ids_pg() for that case.
    """
    cur = conn.cursor()
    for i in range(0, len(params_list), chunk_size):
        chunk = params_list[i:i + chunk_size]
        try:
            psycopg2.extras.execute_batch(cur, query, chunk)
            if _pg_transaction.get() is None:
                conn.commit()
        except Exception:
            if _pg_transaction.get() is None:
                conn.rollback()
            raise


def execute_batch_insert_with_ids_pg(conn, query, params_list, chunk_size=500, template=None):
    """
    Postgres equivalent of execute_batch_insert_with_ids() - NOT a rowid-
    guessing trick (SQLite's MAX(id)+1.. approach relies on single-writer
    guarantees that don't hold under Postgres's real concurrency model).
    Instead uses psycopg2.extras.execute_values() against a query ending in
    RETURNING id, relying on Postgres's well-established behavior that a
    plain multi-row INSERT ... VALUES ... RETURNING returns rows in the same
    order as the input list (true for a bare INSERT with no ON CONFLICT/
    subquery reordering, which is all this app ever does).

    `query` should end in "VALUES %s RETURNING id" (execute_values replaces
    the single %s with the multi-row VALUES list). If a row also needs an
    inline SQL literal alongside the bound columns (e.g. a fixed 'active'/
    'added' status the SQLite version wrote as a literal in the VALUES
    clause), pass `template` - a string like "(%s, %s, 'active', %s)" -
    since execute_values' auto-generated template can't interleave a literal
    constant with bound values.

    Same chunked-commit contract as execute_batch_pg(): each chunk commits
    independently, so a failure partway through a huge import still preserves
    everything already written.
    """
    cur = conn.cursor()
    ids = []
    for i in range(0, len(params_list), chunk_size):
        chunk = params_list[i:i + chunk_size]
        try:
            results = psycopg2.extras.execute_values(cur, query, chunk, template=template, fetch=True)
            ids.extend(row[0] for row in results)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return ids


def estimate_client_data_size_pg(client_id: int = None, template_id: int = None) -> dict:
    """
    Postgres equivalent of estimate_client_data_size() - same formula/shape,
    but LENGTH() needs an explicit ::text cast since Postgres's LENGTH()
    doesn't accept jsonb directly (row_data is JSONB here, TEXT in SQLite).
    """
    where_clauses = ["is_deleted = FALSE"]
    params = []
    if client_id is not None:
        where_clauses.append("client_id = %s")
        params.append(client_id)
    if template_id is not None:
        where_clauses.append("template_id = %s")
        params.append(template_id)
    where_sql = " AND ".join(where_clauses)

    result = execute_query_pg(
        f"SELECT COALESCE(SUM(LENGTH(row_data::text)), 0) as data_bytes, COUNT(*) as row_count "
        f"FROM client_data WHERE {where_sql}",
        params
    )
    data_bytes = result[0]["data_bytes"] if result else 0
    row_count = result[0]["row_count"] if result else 0
    total_bytes = data_bytes + (row_count * ROW_OVERHEAD_BYTES)

    return {
        "total_bytes": total_bytes,
        "row_count": row_count,
        "formatted": format_bytes(total_bytes)
    }


def log_data_audit_pg(client_data_id, user, action, old_data=None, new_data=None):
    """
    Postgres equivalent of log_data_audit() - old_row_data/new_row_data are
    JSONB here, so they're passed via psycopg2.extras.Json(...) rather than
    json.dumps() (letting psycopg2/Postgres own the actual encoding).
    """
    try:
        execute_write_pg(
            "INSERT INTO data_audit_log (client_data_id, \"user\", action, old_row_data, new_row_data) VALUES (%s, %s, %s, %s, %s)",
            (
                client_data_id,
                user,
                action,
                psycopg2.extras.Json(old_data) if old_data else None,
                psycopg2.extras.Json(new_data) if new_data else None,
            )
        )
    except Exception as e:
        print(f"⚠️ Failed to log data audit (Postgres): {e}")


def get_data_audit_history_pg(client_data_id):
    """
    Postgres equivalent of get_data_audit_history() - old_row_data/new_row_data
    come back as native dicts (or None) already, not JSON text, since the
    columns are JSONB.
    """
    return execute_query_pg(
        "SELECT id, \"user\", action, old_row_data, new_row_data, changed_at "
        "FROM data_audit_log WHERE client_data_id = %s ORDER BY changed_at ASC",
        (client_data_id,)
    )


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
