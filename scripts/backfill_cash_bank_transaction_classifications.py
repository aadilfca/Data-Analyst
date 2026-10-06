"""Run the Phase 3 Cash/Bank transaction-line backfill.

Examples:
    python scripts/backfill_cash_bank_transaction_classifications.py --dry-run
    python scripts/backfill_cash_bank_transaction_classifications.py --client-id 7
    python scripts/backfill_cash_bank_transaction_classifications.py --backend postgres --dry-run
    python scripts/backfill_cash_bank_transaction_classifications.py --account-name-only --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# Allow direct execution from the repository's scripts/ directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import database
from services.cash_bank_classification import (
    backfill_account_names_postgres,
    backfill_account_names_sqlite,
    backfill_postgres,
    backfill_sqlite,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill derived Cash/Bank transaction classifications."
    )
    parser.add_argument("--client-id", type=int, default=None)
    parser.add_argument("--configuration-version", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--account-name-only",
        action="store_true",
        help="Fill blank derived account labels without reclassifying transaction rows.",
    )
    parser.add_argument(
        "--backend",
        choices=("sqlite", "postgres"),
        default="sqlite",
        help="Database to target explicitly; defaults to local SQLite.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")
    if args.configuration_version < 1:
        raise SystemExit("--configuration-version must be positive")

    if args.backend == "postgres":
        if not os.environ.get("DATABASE_URL"):
            raise SystemExit("--backend postgres requires DATABASE_URL")
        conn = database.get_raw_conn_pg()
        try:
            if args.account_name_only:
                summary = backfill_account_names_postgres(
                    conn,
                    client_id=args.client_id,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                )
            else:
                summary = backfill_postgres(
                    conn,
                    client_id=args.client_id,
                    configuration_version=args.configuration_version,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                )
        finally:
            database.release_raw_conn_pg(conn)
    else:
        conn = database.get_raw_conn()
        try:
            if args.account_name_only:
                summary = backfill_account_names_sqlite(
                    conn,
                    client_id=args.client_id,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                )
            else:
                summary = backfill_sqlite(
                    conn,
                    client_id=args.client_id,
                    configuration_version=args.configuration_version,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                )
        finally:
            conn.close()

    print(json.dumps(summary.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())