"""Populate the audit-only Cashflows canonical-voucher register.

The command is dry-run by default. ``--apply`` writes only derived occurrence
decisions; it never alters Client Data, classifications, report revisions, or
prepared Cashflows read models.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from psycopg2.extras import RealDictCursor


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import database
from services.cashflow_voucher_canonicalization import (
    build_voucher_occurrences,
    resolve_voucher_occurrences,
)
from services.ledger_report_source import load_cashflow_voucher_occurrence_source
from services.cashflow_voucher_occurrence_store import persist_postgres_occurrences, persist_sqlite_occurrences


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve historical Cashflows voucher occurrences without changing reports."
    )
    parser.add_argument("--client-id", type=int, default=None)
    parser.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist derived occurrence decisions. Omit for the default dry run.",
    )
    return parser


def _sqlite_query(conn: Any) -> Callable[..., list[dict[str, Any]]]:
    def query(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        cursor = conn.execute(sql.replace("%s", "?"), params or ())
        return [dict(row) for row in cursor.fetchall()]

    return query


def _postgres_query(conn: Any) -> Callable[..., list[dict[str, Any]]]:
    def query(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(sql, params or ())
            return [dict(row) for row in cursor.fetchall()]

    return query


def _configured_client_ids(query_fn: Callable[..., list[dict[str, Any]]]) -> list[int]:
    rows = query_fn(
        """
        SELECT client_id
        FROM account_control_cashflow_settings
        WHERE source_template_id IS NOT NULL
        ORDER BY client_id
        """
    )
    return [int(row["client_id"]) for row in rows if row.get("client_id") is not None]


def _plan_for_client(
    client_id: int,
    query_fn: Callable[..., list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = load_cashflow_voucher_occurrence_source(client_id, query_fn=query_fn)
    decisions = resolve_voucher_occurrences(build_voucher_occurrences(source.rows)) if source.ready else []
    status_counts = Counter(decision["decision_status"] for decision in decisions)
    diagnostics = Counter(item.get("severity") or "info" for item in source.diagnostics)
    return decisions, {
        "client_id": client_id,
        "template_id": source.template_id,
        "ready": source.ready,
        "source": dict(source.metadata),
        "occurrence_count": len(decisions),
        "decision_counts": dict(sorted(status_counts.items())),
        "diagnostic_counts": dict(sorted(diagnostics.items())),
        "diagnostics": list(source.diagnostics),
    }


def run_sqlite_backfill(conn: Any, *, client_id: int, apply: bool = False) -> dict[str, Any]:
    decisions, summary = _plan_for_client(client_id, _sqlite_query(conn))
    if apply and summary["ready"]:
        persist_sqlite_occurrences(conn, decisions)
        conn.commit()
    summary["mode"] = "apply" if apply else "dry_run"
    summary["written_occurrence_count"] = len(decisions) if apply and summary["ready"] else 0
    return summary


def run_postgres_backfill(conn: Any, *, client_id: int, apply: bool = False) -> dict[str, Any]:
    decisions, summary = _plan_for_client(client_id, _postgres_query(conn))
    if apply and summary["ready"]:
        persist_postgres_occurrences(conn, decisions)
        conn.commit()
    summary["mode"] = "apply" if apply else "dry_run"
    summary["written_occurrence_count"] = len(decisions) if apply and summary["ready"] else 0
    return summary


def main() -> int:
    args = _parser().parse_args()
    if args.backend == "postgres":
        if not os.environ.get("DATABASE_URL"):
            raise SystemExit("--backend postgres requires DATABASE_URL")
        client_ids = [args.client_id] if args.client_id is not None else _configured_client_ids(database.execute_query_pg)
        conn = database.get_raw_conn_pg()
        try:
            results = [run_postgres_backfill(conn, client_id=client_id, apply=args.apply) for client_id in client_ids]
        finally:
            database.release_raw_conn_pg(conn)
    else:
        conn = database.get_raw_conn()
        try:
            query_fn = _sqlite_query(conn)
            client_ids = [args.client_id] if args.client_id is not None else _configured_client_ids(query_fn)
            results = [run_sqlite_backfill(conn, client_id=client_id, apply=args.apply) for client_id in client_ids]
        finally:
            conn.close()
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "clients": results}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())