"""
One-off script (Fast-data-handling requirement, Phase 2 of 13: Postgres index audit):
checks the live Postgres database for indexes that the app's real query patterns need
but init_db_postgres() (database.py) doesn't create.

This is a manual, standalone check -- not wired into any API endpoint or startup path.
Mirrors migrate_to_supabase.py's own dry-run/--run convention.

Audit method (done once, by hand, this phase): reviewed init_db_postgres()'s ~40
CREATE INDEX statements against real WHERE/JOIN usage across every Postgres-migrated
router. One high-confidence gap found: client_data has indexes on client_id alone,
template_id alone, (client_id, is_deleted, extract_date), and (client_id, extract_date)
-- but no index leads with (client_id, template_id, is_deleted), which is the exact
filter shape used repeatedly on the app's largest table (409k+ rows) by the upload/
validate/import hot path (routers/import_export.py's compare_transactions() and
check_duplicates(), routers/data.py's row-count/delete-by-upload checks and
compute_overdue_uploads()'s all-clients GROUP BY, routers/verification.py's client
data link fetch). Every other candidate column checked (transaction_ref, document_id,
category_id, upload_id, account_code, username, ...) was already covered by an
existing single or composite index, often via a UNIQUE(...) constraint's implicit
index -- deliberately not padded with indexes unbacked by a real query.

Usage:
    python audit_postgres_indexes.py          (dry-run report, no writes)
    python audit_postgres_indexes.py --run    (creates any missing index found above)
"""
import argparse

from database import get_raw_conn_pg, release_raw_conn_pg

# Each entry: (index_name, table, CREATE INDEX statement). Add future audit findings
# here rather than starting a second script -- this file is the one-stop index audit.
CANDIDATE_INDEXES = [
    (
        "idx_client_data_client_template_deleted",
        "client_data",
        "CREATE INDEX IF NOT EXISTS idx_client_data_client_template_deleted "
        "ON client_data (client_id, template_id, is_deleted)",
    ),
]


def get_existing_index_names(cur) -> set:
    cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    return {row[0] for row in cur.fetchall()}


def main():
    parser = argparse.ArgumentParser(description="Audit and add missing Postgres indexes.")
    parser.add_argument("--run", action="store_true", help="Actually create any missing index (default is a dry-run report only).")
    args = parser.parse_args()

    conn = get_raw_conn_pg()
    try:
        cur = conn.cursor()
        existing = get_existing_index_names(cur)

        missing = [c for c in CANDIDATE_INDEXES if c[0] not in existing]

        print(f"Postgres Index Audit ({'--run passed -- creating missing indexes below' if args.run else 'DRY RUN'})")
        print(f"Checked {len(CANDIDATE_INDEXES)} candidate index(es); {len(existing)} index(es) already exist in the schema.\n")

        if not missing:
            print("✅ No missing indexes -- schema already matches the audit's findings.")
            return

        for name, table, sql in missing:
            print(f"MISSING: {name} on {table}")
            print(f"  {sql}")
            if args.run:
                cur.execute(sql)
                conn.commit()
                print(f"  ✅ created")
            print()

        if not args.run:
            print("Dry run only -- no indexes were created. Re-run with --run to create them.")
    finally:
        release_raw_conn_pg(conn)


if __name__ == "__main__":
    main()
