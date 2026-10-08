"""Build the prepared Cashflows read model for existing canonical data.

The command is dry-run by default. ``--apply`` writes one complete preparation
revision per client; raw Client Data, classifications, and report reads remain
unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from psycopg2.extras import RealDictCursor


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import database
from services.cashflow_preparation import build_preparation_plan, prepare_client


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build revisioned Cashflows prepared lines and rollups."
    )
    parser.add_argument("--client-id", type=int, default=None)
    parser.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist a complete prepared revision. Omit for the default dry run.",
    )
    parser.add_argument("--preparation-revision", type=int, default=None)
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


def _summary(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in plan.items()
        if key not in {"prepared_lines", "rollups"}
    }


def run_client(
    conn: Any,
    *,
    client_id: int,
    apply: bool = False,
    preparation_revision: int | None = None,
) -> dict[str, Any]:
    plan = build_preparation_plan(conn, client_id=client_id)
    result = _summary(plan)
    result["mode"] = "apply" if apply else "dry_run"
    if not plan.get("ready") or not apply:
        result["rollup_count"] = len(plan.get("rollups", []))
        return result
    result.update(
        prepare_client(
            conn,
            client_id=client_id,
            preparation_revision=preparation_revision,
        )
    )
    return result


def main() -> int:
    args = _parser().parse_args()
    if args.backend == "postgres":
        if not os.environ.get("DATABASE_URL"):
            raise SystemExit("--backend postgres requires DATABASE_URL")
        conn = database.get_raw_conn_pg()
        try:
            query_fn = _postgres_query(conn)
            client_ids = [args.client_id] if args.client_id is not None else _configured_client_ids(query_fn)
            results = []
            for client_id in client_ids:
                results.append(
                    run_client(
                        conn,
                        client_id=client_id,
                        apply=args.apply,
                        preparation_revision=args.preparation_revision,
                    )
                )
                if args.apply:
                    conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            database.release_raw_conn_pg(conn)
    else:
        conn = database.get_raw_conn()
        try:
            query_fn = _sqlite_query(conn)
            client_ids = [args.client_id] if args.client_id is not None else _configured_client_ids(query_fn)
            results = []
            for client_id in client_ids:
                results.append(
                    run_client(
                        conn,
                        client_id=client_id,
                        apply=args.apply,
                        preparation_revision=args.preparation_revision,
                    )
                )
                if args.apply:
                    conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "clients": results}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
