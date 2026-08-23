"""
One-time migration script: copies real data from the local SQLite database
(test.db) into the live Supabase (Postgres) database, preserving original ids
so every FK reference (client_data.client_id, verification_details.client_data_id,
etc.) still resolves correctly after the move.

This is NOT a repeatable sync -- it's a one-shot cutover script. File uploads
(the uploads/ folder) are explicitly out of scope; that's a separate Supabase
Storage requirement.

Phase 1 (this phase): connect to both databases, walk every table in
FK-safe order, and print a dry-run row-count comparison. No writes, no type
conversion yet.
  - Phase 2 will add the SQLite->Postgres type-conversion layer (booleans,
    dates, timestamps, JSON->JSONB).
  - Phase 3 will add real write mode with id preservation (OVERRIDING SYSTEM
    VALUE + setval(), same pattern as routers/templates.py's import_backup()).
  - Phase 4 will add post-migration verification (row-count diff, FK
    spot-checks).

Not wired into any API endpoint -- run manually on demand:
    python migrate_to_supabase.py            (dry-run report only, no writes)
    python migrate_to_supabase.py --run      (Phase 1: still no writes -- see below)
"""
import argparse

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
    parser.add_argument("--run", action="store_true", help="Reserved for a later phase -- Phase 1 never writes, regardless of this flag.")
    args = parser.parse_args()

    report = scan()
    print_report(report, run_mode=args.run)

    print("\nPhase 1 skeleton only -- no data was written to Postgres.")
    print("Type conversion (Phase 2) and id-preserving writes (Phase 3) are not yet implemented.")
