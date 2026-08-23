"""
One-time migration script: copies real data from the local SQLite database
(test.db) into the live Supabase (Postgres) database, preserving original ids
so every FK reference (client_data.client_id, verification_details.client_data_id,
etc.) still resolves correctly after the move.

This is NOT a repeatable sync -- it's a one-shot cutover script. File uploads
(the uploads/ folder) are explicitly out of scope; that's a separate Supabase
Storage requirement.

Phase 1 (done): connect to both databases, walk every table in FK-safe
order, and print a dry-run row-count comparison. No writes, no type
conversion.

Phase 2 (done): the SQLite->Postgres type-conversion layer (booleans,
dates/timestamps, JSON->JSONB). Column types are read live from Postgres's
own information_schema (not a hand-maintained map) so the conversion logic
can never silently drift out of sync with database.py's init_db_postgres()
schema.

Phase 3 (this phase): real write mode. --run performs a full wipe-and-replace
across all 42 tables -- DELETE every existing Postgres row (confirmed, via a
read-only check, to hold only _seed_postgres_data()'s bootstrap content plus
disposable same-day E2E test artifacts from the Phase 8 cutover verification,
not real production usage), then INSERT every SQLite row with its original id
preserved via OVERRIDING SYSTEM VALUE + setval() -- the same delete-then
-restore shape routers/templates.py's import_backup() already uses for its
own 6 tables, extended here to all 42. Unlike import_backup()'s single
transaction, this commits per delete-phase and per insert chunk (409K rows is
too much to hold open in one transaction against a pooled connection) -- a
useful side effect is the script is safely re-runnable if it fails partway,
since a re-run's delete phase always fully wipes any partial state first.
Requires typing "MIGRATE" at a confirmation prompt before writing anything.
Run live 2026-08-23: 409,336 of 409,362 rows migrated; 26 skipped (pre-existing
dangling FK references SQLite never enforced -- see Phase 4 below).

Phase 4 (FINAL, this phase): formal, repeatable post-migration verification
(--verify, read-only) -- a row-count diff that correctly accounts for the
legitimate dangling-reference skips (rather than a naive equality check that
would misreport those 3 tables as mismatched), plus FK spot-checks sampled
across every relationship in the schema, confirming id preservation actually
held. Additive only -- does not touch Phase 1-3's already-proven-live code.

Not wired into any API endpoint -- run manually on demand:
    python migrate_to_supabase.py                    (dry-run row-count report, no writes)
    python migrate_to_supabase.py --preview-convert   (also prints one converted sample row per table)
    python migrate_to_supabase.py --run              (WRITES: full wipe-and-replace, asks for confirmation first)
    python migrate_to_supabase.py --verify           (post-migration row-count + FK verification, read-only)
"""
import argparse
import json

from psycopg2.extras import Json

from database import get_db, get_raw_conn_pg, release_raw_conn_pg, execute_batch_pg

# Same 42-table order as database.py's init_db_postgres() (see that function's
# numbered "---------- N. table_name ----------" comments, database.py:2656-3341)
# -- already FK-safe, every table's own FK targets appear earlier in this list.
# Mirroring it here (rather than re-deriving it) keeps this script's insert
# order (Phase 3) guaranteed consistent with the schema that created these
# tables in the first place.
TABLES_IN_FK_ORDER = [
    "master_columns",
    "templates",
    "clients",
    "audit_rule_documents",
    "template_column_mappings",
    "uploaded_files",
    "verifications",
    "audit_log",
    "client_data",
    "error_codes",
    "error_log",
    "verification_sessions",
    "verification_details",
    "audit_procedure_library",
    "app_settings",
    "data_audit_log",
    "client_voucher_prefixes",
    "saved_column_mappings",
    "client_template_upload_frequency",
    "audit_rule_document_fields",
    "audit_rule_document_auth_roles",
    "audit_rule_cross_checks",
    "audit_rule_cross_check_documents",
    "audit_rule_categories",
    "audit_rule_category_documents",
    "audit_rule_category_cross_checks",
    "signature_specimens",
    "holidays",
    "trial_balance_uploads",
    "trial_balance_accounts",
    "account_category_assignments",
    "voucher_line_documents",
    "voucher_line_procedure_checks",
    "voucher_line_physical_bills",
    "voucher_line_physical_bill_items",
    "voucher_approvals",
    "audit_rule_voucher_checks",
    "audit_rule_voucher_check_auth_roles",
    "voucher_check_status",
    "voucher_line_observations",
    "users",
    "sessions",
]

# Tables _seed_postgres_data() (database.py) already populates on a fresh
# Postgres schema init -- a nonzero Postgres count here doesn't mean real data
# was migrated already, just that seed rows exist. Confirmed (read-only check,
# Phase 3) that every nonzero-count table in a fresh cutover is either this
# seed content or disposable same-day E2E test artifacts -- Phase 3's --run
# wipes and replaces all of them, no special-casing needed beyond this report
# flag.
SEED_COLLISION_TABLES = {
    "master_columns", "templates", "template_column_mappings", "error_codes",
    "audit_procedure_library", "app_settings", "audit_rule_categories", "users",
}

# sessions.token is a plain TEXT PRIMARY KEY (app-generated, not sequential) --
# the one table in the whole schema with a non-identity primary key (see
# database.py's own comment at that table's definition). It gets a plain
# INSERT with no OVERRIDING SYSTEM VALUE clause and no setval() call.
NON_IDENTITY_TABLES = {"sessions"}


def count_sqlite(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def count_postgres(conn, table: str) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def scan() -> list:
    """
    Opens one SQLite connection and one raw Postgres connection, counts every
    table in TABLES_IN_FK_ORDER on both sides, and returns a list of per-table
    result dicts. Read-only -- no writes on either side.
    """
    sqlite_conn = get_db()
    pg_conn = get_raw_conn_pg()
    results = []
    try:
        for table in TABLES_IN_FK_ORDER:
            sqlite_count = count_sqlite(sqlite_conn, table)
            postgres_count = count_postgres(pg_conn, table)
            results.append({
                "table": table,
                "sqlite_count": sqlite_count,
                "postgres_count": postgres_count,
                "seed_collision": table in SEED_COLLISION_TABLES,
            })
    finally:
        sqlite_conn.close()
        release_raw_conn_pg(pg_conn)
    return results


def get_postgres_column_types(pg_conn) -> dict:
    """
    Reads each table's real column types straight from Postgres's own
    information_schema -- not a hand-maintained map -- so conversion logic
    can never silently drift out of sync with database.py's
    init_db_postgres() schema. Returns {table: {column: data_type}}, e.g.
    {"clients": {"id": "integer", "is_active": "boolean", ...}, ...}.
    """
    cur = pg_conn.cursor()
    cur.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (TABLES_IN_FK_ORDER,)
    )
    column_types = {table: {} for table in TABLES_IN_FK_ORDER}
    for table_name, column_name, data_type in cur.fetchall():
        column_types[table_name][column_name] = data_type
    return column_types


def convert_value(pg_type: str, value):
    """
    Converts one SQLite-sourced value into the shape psycopg2 needs for the
    target Postgres column type. None always stays None regardless of type.
      - boolean:            SQLite's 0/1 -> Python bool (Postgres rejects an
                             implicit int->bool cast on insert).
      - date / timestamp*:  left as the ISO-formatted string SQLite already
                             stores (CURRENT_TIMESTAMP/date columns are
                             "YYYY-MM-DD[ HH:MM:SS]", which Postgres parses
                             natively on insert -- no need to hand-parse into
                             date/datetime objects).
      - jsonb:               SQLite's json.dumps() string -> json.loads() ->
                             psycopg2.extras.Json(...), same convention
                             routers/templates.py's import_backup() uses. A
                             value that's already a dict/list is wrapped
                             directly (defensive -- sqlite3.Row never
                             actually produces this).
      - anything else:      passed through unchanged (TEXT/INTEGER/REAL).
    """
    if value is None:
        return None
    if pg_type == "boolean":
        return bool(value)
    if pg_type == "jsonb":
        if isinstance(value, (dict, list)):
            return Json(value)
        return Json(json.loads(value))
    return value


def convert_row(row: dict, column_types: dict) -> dict:
    """
    Applies convert_value() to every column in one SQLite row dict, using the
    target table's {column: data_type} map from get_postgres_column_types().
    A column Postgres doesn't recognise (shouldn't happen) defaults to a
    plain passthrough rather than raising.
    """
    return {
        col: convert_value(column_types.get(col, "text"), val)
        for col, val in row.items()
    }


CONVERTED_TYPE_PREFIXES = ("boolean", "date", "timestamp", "jsonb")


def preview_convert(sqlite_conn, pg_column_types: dict):
    """
    For each table, pulls one sample SQLite row (if any exist) and prints its
    before/after values for only the columns whose Postgres type actually
    changes shape (bool/date/timestamp/jsonb) -- plain passthrough columns are
    omitted as noise. Read-only: no writes to either database.
    """
    print()
    print("=" * 78)
    print("Type-Conversion Preview (one sample row per table, converted columns only)")
    print("=" * 78)

    for table in TABLES_IN_FK_ORDER:
        column_types = pg_column_types.get(table, {})
        interesting_cols = [
            col for col, pg_type in column_types.items()
            if pg_type.startswith(CONVERTED_TYPE_PREFIXES)
        ]
        if not interesting_cols:
            continue

        row = sqlite_conn.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
        if row is None:
            continue

        row_dict = dict(row)
        converted = convert_row(row_dict, column_types)

        print(f"\n{table}:")
        for col in interesting_cols:
            if col not in row_dict:
                continue
            before = row_dict[col]
            after = converted[col]
            print(f"    {col} ({column_types[col]}): {before!r} -> {after!r}")

    print("\n" + "=" * 78)


def delete_all_postgres_data(pg_conn) -> dict:
    """
    DELETE every row from every table, in reverse TABLES_IN_FK_ORDER (children
    before parents) so no FK violation is possible -- same reasoning
    import_backup()'s own (smaller) delete list already documents. One
    connection, committed once at the end. Returns {table: rows_deleted}.
    """
    cur = pg_conn.cursor()
    deleted_counts = {}
    try:
        for table in reversed(TABLES_IN_FK_ORDER):
            cur.execute(f"DELETE FROM {table}")
            deleted_counts[table] = cur.rowcount
        pg_conn.commit()
    except Exception:
        pg_conn.rollback()
        raise
    return deleted_counts


def get_sqlite_columns(sqlite_conn, table: str) -> list:
    """Table's real column names, straight from SQLite's own schema (LIMIT 0 -- no rows fetched)."""
    cur = sqlite_conn.execute(f"SELECT * FROM {table} LIMIT 0")
    return [d[0] for d in cur.description]


def get_fk_constraints(pg_conn) -> dict:
    """
    Reads every FK constraint (table -> [(fk_column, ref_table, ref_column), ...])
    straight from Postgres's own information_schema. Needed because this
    app's SQLite connections never enable PRAGMA foreign_keys = ON (see
    CLAUDE.md) -- a dangling reference that was silently tolerated in SQLite
    for years (found live: 1 of data_audit_log's 164,926 rows points at a
    client_data id that no longer exists) is a hard error under Postgres's
    real FK enforcement. migrate_table() uses this to filter such rows out
    before inserting, rather than letting the whole table's chunk fail.
    """
    cur = pg_conn.cursor()
    cur.execute(
        """
        SELECT tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
            ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
        """
    )
    fk_constraints = {}
    for table_name, fk_column, ref_table, ref_column in cur.fetchall():
        fk_constraints.setdefault(table_name, []).append((fk_column, ref_table, ref_column))
    return fk_constraints


def migrate_table(sqlite_conn, pg_conn, table: str, pg_column_types: dict,
                   fk_constraints: dict, valid_id_cache: dict) -> tuple:
    """
    Fetches every SQLite row for `table`, converts each via convert_row()
    (Phase 2), and inserts it into Postgres with its original id preserved
    (OVERRIDING SYSTEM VALUE, except NON_IDENTITY_TABLES) via
    database.execute_batch_pg() -- reused, not reimplemented, same
    chunked-commit/partial-durability contract every other batch write in
    this codebase already relies on. Resets the identity sequence afterward
    so a later normal insert (no explicit id) doesn't collide. Returns the
    number of rows inserted (0 if the table was empty in SQLite -- skipped
    entirely, no insert/setval call made).

    Column list is filtered to the intersection of SQLite's columns and
    Postgres's real columns (from pg_column_types) -- SQLite's schema has
    drifted ahead of database.py's init_db_postgres() port on a couple of
    tables (found live: uploaded_files has notes/uploaded_by with no Postgres
    equivalent). A dropped SQLite-only column is reported so the gap is
    visible rather than silently losing data.

    Rows are also filtered against fk_constraints before insert: SQLite never
    enforces its own declared FKs here (no PRAGMA foreign_keys = ON, per
    CLAUDE.md), so a genuinely dangling reference that was silently tolerated
    there (found live: 1 data_audit_log row pointing at a deleted
    client_data id) would otherwise fail Postgres's real FK enforcement and
    abort the whole table's insert chunk. A skipped row is reported (table,
    column, up to 10 example ids) rather than silently dropped. Since tables
    are processed in FK order, every table this table could reference has
    already been fully inserted by the time it's checked, so a
    valid-id lookup against Postgres itself is authoritative -- valid_id_cache
    (shared across the whole run) avoids re-querying the same referenced
    table's id set once per referencing table.

    Returns (rows_inserted, rows_skipped) -- both 0 if the table was empty in
    SQLite (skipped entirely, no insert/setval call made).
    """
    column_types = pg_column_types.get(table, {})
    sqlite_columns = get_sqlite_columns(sqlite_conn, table)
    columns = [c for c in sqlite_columns if c in column_types]
    dropped = [c for c in sqlite_columns if c not in column_types]
    if dropped:
        print(f"    WARNING: {table} -- SQLite column(s) {dropped} have no Postgres equivalent, dropped from migration")

    rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        return 0, 0

    table_fks = [fk for fk in fk_constraints.get(table, []) if fk[0] in columns]

    def get_valid_ids(ref_table, ref_column):
        key = (ref_table, ref_column)
        if key not in valid_id_cache:
            cur = pg_conn.cursor()
            cur.execute(f'SELECT "{ref_column}" FROM {ref_table}')
            valid_id_cache[key] = {r[0] for r in cur.fetchall()}
        return valid_id_cache[key]

    params_list = []
    skipped_examples = {}
    skipped_count = 0
    for row in rows:
        row_dict = dict(row)
        violated_fk = None
        for fk_column, ref_table, ref_column in table_fks:
            value = row_dict.get(fk_column)
            if value is not None and value not in get_valid_ids(ref_table, ref_column):
                violated_fk = (fk_column, ref_table, value)
                break
        if violated_fk:
            skipped_count += 1
            skipped_examples.setdefault(violated_fk[:2], []).append(violated_fk[2])
            continue
        converted = convert_row(row_dict, column_types)
        params_list.append(tuple(converted[col] for col in columns))

    for (fk_column, ref_table), example_ids in skipped_examples.items():
        shown = example_ids[:10]
        suffix = f" (+{len(example_ids) - 10} more)" if len(example_ids) > 10 else ""
        print(f"    WARNING: {table} -- skipped {len(example_ids)} row(s) with a {fk_column} not present in {ref_table}: {shown}{suffix}")

    if not params_list:
        return 0, skipped_count

    # Double-quoted identifiers: a couple of columns (audit_log.user/timestamp,
    # data_audit_log.user) are Postgres reserved words -- database.py's own
    # schema already quotes them for the same reason (init_db_postgres()).
    # Quoting every column here (not just the known reserved ones) is
    # harmless for ordinary identifiers and avoids having to hand-maintain a
    # reserved-word list that could silently miss one.
    col_list = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    overriding = "" if table in NON_IDENTITY_TABLES else " OVERRIDING SYSTEM VALUE"
    insert_sql = f"INSERT INTO {table} ({col_list}){overriding} VALUES ({placeholders})"

    execute_batch_pg(pg_conn, insert_sql, params_list, chunk_size=1000)

    if table not in NON_IDENTITY_TABLES:
        cur = pg_conn.cursor()
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT MAX(id) FROM {table}), true)"
        )
        pg_conn.commit()

    # This table's own id column may now be a referenceable target for a
    # later table -- but valid_id_cache is only ever populated lazily via
    # get_valid_ids() the first time a later table's FK actually needs it,
    # so no explicit cache update is needed here.

    return len(params_list), skipped_count


def run_migration(sqlite_conn, pg_conn, pg_column_types: dict) -> dict:
    """
    Full wipe-and-replace: delete phase (all 42 tables, reverse FK order),
    then insert phase (all 42 tables, forward FK order). fk_constraints/
    valid_id_cache are built once and threaded through every migrate_table()
    call so a dangling SQLite reference (tolerated there, rejected by
    Postgres's real FK enforcement) is skipped and reported per-table instead
    of aborting the whole run. Returns
    {table: {"deleted": N, "inserted": N, "skipped": N}}.
    """
    print("\nDeleting existing Postgres data...")
    deleted_counts = delete_all_postgres_data(pg_conn)

    fk_constraints = get_fk_constraints(pg_conn)
    valid_id_cache = {}

    print("Inserting SQLite data...")
    results = {}
    for table in TABLES_IN_FK_ORDER:
        inserted, skipped = migrate_table(sqlite_conn, pg_conn, table, pg_column_types, fk_constraints, valid_id_cache)
        results[table] = {"deleted": deleted_counts.get(table, 0), "inserted": inserted, "skipped": skipped}
        skip_note = f", skipped {skipped}" if skipped else ""
        print(f"  {table}: deleted {results[table]['deleted']}, inserted {inserted}{skip_note}")

    return results


def print_migration_report(results: dict):
    print()
    print("=" * 78)
    print("SQLite -> Supabase Migration: Write Report")
    print("=" * 78)
    print(f"{'Table':<42} {'Deleted':>10} {'Inserted':>10} {'Skipped':>10}")
    print("-" * 78)

    total_deleted = 0
    total_inserted = 0
    total_skipped = 0
    for table in TABLES_IN_FK_ORDER:
        r = results[table]
        print(f"{table:<42} {r['deleted']:>10} {r['inserted']:>10} {r['skipped']:>10}")
        total_deleted += r["deleted"]
        total_inserted += r["inserted"]
        total_skipped += r["skipped"]

    print("-" * 78)
    print(f"{'GRAND TOTAL':<42} {total_deleted:>10} {total_inserted:>10} {total_skipped:>10}")
    print("=" * 78)
    if total_skipped:
        print(f"\n{total_skipped} row(s) were skipped due to a dangling FK reference SQLite never enforced -- see WARNING lines above for details.")


def count_dangling_rows(sqlite_conn, pg_conn, table: str, fk_constraints: dict, valid_id_cache: dict) -> int:
    """
    Read-only count of SQLite rows in `table` whose FK column value doesn't
    resolve in the corresponding Postgres referenced table -- the same check
    migrate_table() (Phase 3) applies when deciding what to skip during a
    real write, reimplemented separately here (not refactored out of
    migrate_table()) so this verification pass can't risk touching that
    already-proven-live write path. valid_id_cache is the caller's dict,
    reused across tables/calls to avoid re-querying the same referenced
    table's id set repeatedly.
    """
    table_fks = fk_constraints.get(table, [])
    if not table_fks:
        return 0

    def get_valid_ids(ref_table, ref_column):
        key = (ref_table, ref_column)
        if key not in valid_id_cache:
            cur = pg_conn.cursor()
            cur.execute(f'SELECT "{ref_column}" FROM {ref_table}')
            valid_id_cache[key] = {r[0] for r in cur.fetchall()}
        return valid_id_cache[key]

    sqlite_columns = get_sqlite_columns(sqlite_conn, table)
    table_fks = [fk for fk in table_fks if fk[0] in sqlite_columns]
    if not table_fks:
        return 0

    rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
    dangling = 0
    for row in rows:
        row_dict = dict(row)
        for fk_column, ref_table, ref_column in table_fks:
            value = row_dict.get(fk_column)
            if value is not None and value not in get_valid_ids(ref_table, ref_column):
                dangling += 1
                break
    return dangling


def verify_row_counts(sqlite_conn, pg_conn, fk_constraints: dict) -> list:
    """
    Row-count diff for every table, adjusted for legitimate dangling-reference
    skips (count_dangling_rows()) -- a naive sqlite_count == postgres_count
    check would misreport the tables with a known dangling reference (e.g.
    data_audit_log, off by the 1 orphaned row confirmed live 2026-08-23) as a
    real mismatch. Returns a list of per-table dicts with a MATCH/MISMATCH
    status. Read-only.
    """
    valid_id_cache = {}
    results = []
    for table in TABLES_IN_FK_ORDER:
        sqlite_count = count_sqlite(sqlite_conn, table)
        postgres_count = count_postgres(pg_conn, table)
        dangling_count = count_dangling_rows(sqlite_conn, pg_conn, table, fk_constraints, valid_id_cache)
        expected_postgres_count = sqlite_count - dangling_count
        if postgres_count == expected_postgres_count:
            status = "MATCH"
        elif postgres_count > expected_postgres_count:
            # Postgres is the live app's real database post-migration -- a
            # count higher than expected most likely means real usage
            # happened since the migration ran (new sessions, new
            # verification work, etc.), not data loss. Flagged separately
            # from a genuine SHORTFALL so this expected, healthy case
            # doesn't read as an alarm.
            status = "GREW (likely live usage since migration)"
        else:
            status = "SHORTFALL"
        results.append({
            "table": table,
            "sqlite_count": sqlite_count,
            "postgres_count": postgres_count,
            "dangling_count": dangling_count,
            "expected_postgres_count": expected_postgres_count,
            "status": status,
        })
    return results


def verify_fk_spot_checks(sqlite_conn, pg_conn, fk_constraints: dict, sample_size: int = 5) -> list:
    """
    For every real FK relationship in the schema (table, fk_column, ref_table,
    ref_column), samples up to `sample_size` rows from the migrated Postgres
    child table (non-null fk_column) and compares that same row id's
    fk_column value between SQLite and Postgres. This is the direct test of
    what Phase 3 was supposed to guarantee -- id preservation held, so a
    given row's relationships still point at the same place on both sides --
    not a check of Postgres's own internal FK validity, which its real FK
    constraints already guarantee structurally and would be redundant to
    re-check. Returns a list of per-relationship dicts. Read-only.
    """
    results = []
    for table in TABLES_IN_FK_ORDER:
        # sessions.token is the one non-identity, non-"id" primary key in the
        # whole schema (see NON_IDENTITY_TABLES) -- every other table samples
        # by its "id" column.
        pk_col = "token" if table in NON_IDENTITY_TABLES else "id"

        for fk_column, ref_table, ref_column in fk_constraints.get(table, []):
            pg_cur = pg_conn.cursor()
            pg_cur.execute(
                f'SELECT "{pk_col}", "{fk_column}" FROM {table} WHERE "{fk_column}" IS NOT NULL LIMIT %s',
                (sample_size,)
            )
            sampled = pg_cur.fetchall()

            checked = 0
            mismatches = []
            for row_id, pg_fk_value in sampled:
                sqlite_row = sqlite_conn.execute(
                    f'SELECT "{fk_column}" FROM {table} WHERE "{pk_col}" = ?', (row_id,)
                ).fetchone()
                if sqlite_row is None:
                    continue
                checked += 1
                if sqlite_row[0] != pg_fk_value:
                    mismatches.append((row_id, sqlite_row[0], pg_fk_value))

            if checked == 0:
                continue
            results.append({
                "table": table,
                "fk_column": fk_column,
                "ref_table": ref_table,
                "checked": checked,
                "mismatches": mismatches,
            })
    return results


def print_verification_report(row_count_results: list, fk_spot_check_results: list):
    print()
    print("=" * 78)
    print("SQLite -> Supabase Migration: Post-Migration Verification (--verify)")
    print("=" * 78)
    print("\n-- Row-Count Diff (adjusted for known dangling-reference skips) --")
    print(f"{'Table':<42} {'SQLite':>8} {'Postgres':>9} {'Dangling':>9}  Status")
    print("-" * 78)
    shortfall_count = 0
    grew_count = 0
    for r in row_count_results:
        if r["status"] == "SHORTFALL":
            shortfall_count += 1
        elif r["status"].startswith("GREW"):
            grew_count += 1
        print(f"{r['table']:<42} {r['sqlite_count']:>8} {r['postgres_count']:>9} {r['dangling_count']:>9}  {r['status']}")
    print("-" * 78)
    if shortfall_count:
        print(f"{shortfall_count} table(s) SHORTFALL -- Postgres has FEWER rows than expected, not explained by a known dangling reference. This is a real problem -- investigate before trusting the migration.")
    else:
        print("No SHORTFALLs -- Postgres never has fewer rows than SQLite + known dangling skips can explain.")
    if grew_count:
        print(f"{grew_count} table(s) GREW beyond the migrated snapshot -- expected if the live app has been used since the migration ran (Postgres is now the real database, SQLite is a frozen snapshot).")

    print("\n-- FK Spot-Checks (sampled rows per relationship, id preservation across both DBs) --")
    if not fk_spot_check_results:
        print("No FK relationships had any sampled rows to check (empty tables).")
    else:
        total_checked = 0
        total_mismatches = 0
        for r in fk_spot_check_results:
            total_checked += r["checked"]
            total_mismatches += len(r["mismatches"])
            status = "OK" if not r["mismatches"] else f"{len(r['mismatches'])} MISMATCH"
            print(f"  {r['table']}.{r['fk_column']} -> {r['ref_table']}: {r['checked']} sampled, {status}")
            for row_id, sqlite_val, pg_val in r["mismatches"]:
                print(f"      id={row_id}: sqlite={sqlite_val!r} != postgres={pg_val!r}")
        print("-" * 78)
        print(f"{total_checked} row(s) sampled across {len(fk_spot_check_results)} relationship(s), {total_mismatches} mismatch(es).")

    print("=" * 78)


def print_report(results: list, run_mode: bool):
    print()
    print("=" * 78)
    print(f"SQLite -> Supabase Migration: Dry-Run Row-Count Report ({'--run passed -- real writes follow below, after confirmation' if run_mode else 'DRY RUN'})")
    print("=" * 78)
    print(f"{'Table':<42} {'SQLite':>10} {'Postgres':>10}   Note")
    print("-" * 78)

    total_sqlite = 0
    total_postgres = 0
    for r in results:
        note = ""
        if r["postgres_count"] > 0 and r["seed_collision"]:
            note = "seed data already present"
        elif r["postgres_count"] > 0:
            note = "Postgres already has rows"
        print(f"{r['table']:<42} {r['sqlite_count']:>10} {r['postgres_count']:>10}   {note}")
        total_sqlite += r["sqlite_count"]
        total_postgres += r["postgres_count"]

    print("-" * 78)
    print(f"{'GRAND TOTAL':<42} {total_sqlite:>10} {total_postgres:>10}")
    print("=" * 78)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate real data from SQLite (test.db) into Supabase (Postgres), preserving original ids."
    )
    parser.add_argument("--run", action="store_true", help="Perform a real, full wipe-and-replace write of all 42 tables. Asks for a typed confirmation first.")
    parser.add_argument("--preview-convert", action="store_true", help="Also print one type-converted sample row per table (bool/date/jsonb columns only). Read-only.")
    parser.add_argument("--verify", action="store_true", help="Run post-migration verification (row-count diff adjusted for known dangling-reference skips, plus FK spot-checks). Read-only.")
    args = parser.parse_args()

    if args.verify:
        sqlite_conn = get_db()
        pg_conn = get_raw_conn_pg()
        try:
            fk_constraints = get_fk_constraints(pg_conn)
            row_count_results = verify_row_counts(sqlite_conn, pg_conn, fk_constraints)
            fk_spot_check_results = verify_fk_spot_checks(sqlite_conn, pg_conn, fk_constraints)
            print_verification_report(row_count_results, fk_spot_check_results)
        finally:
            sqlite_conn.close()
            release_raw_conn_pg(pg_conn)
        raise SystemExit(0)

    report = scan()
    print_report(report, run_mode=args.run)

    if args.preview_convert:
        sqlite_conn = get_db()
        pg_conn = get_raw_conn_pg()
        try:
            pg_column_types = get_postgres_column_types(pg_conn)
            preview_convert(sqlite_conn, pg_column_types)
        finally:
            sqlite_conn.close()
            release_raw_conn_pg(pg_conn)

    if not args.run:
        print("\nDry run only -- no data was written to Postgres. Re-run with --run to perform the real migration.")
    else:
        tables_to_delete = [r["table"] for r in report if r["postgres_count"] > 0]
        rows_to_delete = sum(r["postgres_count"] for r in report)
        rows_to_insert = sum(r["sqlite_count"] for r in report)

        print("\n" + "!" * 78)
        print("REAL WRITE MODE -- this will PERMANENTLY DELETE all existing Postgres data")
        print("and REPLACE it with SQLite's data, preserving original ids.")
        print("!" * 78)
        print(f"  {rows_to_delete} row(s) will be deleted across {len(tables_to_delete)} table(s): {', '.join(tables_to_delete)}")
        print(f"  {rows_to_insert} row(s) will be inserted from SQLite.")
        print("\nThis cannot be undone. Type MIGRATE (all caps) to proceed, anything else to abort.")

        confirmation = input("> ").strip()
        if confirmation != "MIGRATE":
            print("\nAborted -- no changes made.")
        else:
            sqlite_conn = get_db()
            pg_conn = get_raw_conn_pg()
            try:
                pg_column_types = get_postgres_column_types(pg_conn)
                results = run_migration(sqlite_conn, pg_conn, pg_column_types)
                print_migration_report(results)
                print("\nMigration complete.")
            finally:
                sqlite_conn.close()
                release_raw_conn_pg(pg_conn)
