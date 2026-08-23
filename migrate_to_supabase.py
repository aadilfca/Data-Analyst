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

Phase 2 (this phase): the SQLite->Postgres type-conversion layer (booleans,
dates/timestamps, JSON->JSONB) that Phase 3's real writes will depend on.
Column types are read live from Postgres's own information_schema (not a
hand-maintained map) so the conversion logic can never silently drift out of
sync with database.py's init_db_postgres() schema. Still zero writes --
--preview-convert only prints a sample converted row per table for manual
verification.
  - Phase 3 will add real write mode with id preservation (OVERRIDING SYSTEM
    VALUE + setval(), same pattern as routers/templates.py's import_backup()),
    using convert_row() from this phase on every row before insert.
  - Phase 4 will add post-migration verification (row-count diff, FK
    spot-checks).

Not wired into any API endpoint -- run manually on demand:
    python migrate_to_supabase.py                    (dry-run row-count report, no writes)
    python migrate_to_supabase.py --preview-convert   (also prints one converted sample row per table)
    python migrate_to_supabase.py --run              (Phase 1-2: still no writes -- see below)
"""
import argparse
import json

from psycopg2.extras import Json

from database import get_db, get_raw_conn_pg, release_raw_conn_pg

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
# was migrated already, just that seed rows exist. Phase 3's id-preserving
# insert will need a deliberate strategy for these (upsert-by-natural-key
# rather than a blind OVERRIDING SYSTEM VALUE insert, since seeded rows won't
# share SQLite's ids). Not handled in this phase -- flagged in the report only.
SEED_COLLISION_TABLES = {
    "master_columns", "templates", "error_codes",
    "audit_procedure_library", "app_settings",
}


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


def print_report(results: list, run_mode: bool):
    print()
    print("=" * 78)
    print(f"SQLite -> Supabase Migration: Dry-Run Row-Count Report ({'--run passed, but Phase 1 does not write yet' if run_mode else 'DRY RUN'})")
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
    parser.add_argument("--run", action="store_true", help="Reserved for a later phase -- Phases 1-2 never write, regardless of this flag.")
    parser.add_argument("--preview-convert", action="store_true", help="Also print one type-converted sample row per table (bool/date/jsonb columns only). Still read-only.")
    args = parser.parse_args()

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

    print("\nPhase 1-2 skeleton only -- no data was written to Postgres.")
    print("Id-preserving writes (Phase 3) are not yet implemented.")
