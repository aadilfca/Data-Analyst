"""Build the revisioned Cashflows prepared read model.

This module is an operator/back-office service. It reads the canonical voucher
register and current derived classifications, then writes complete prepared
voucher lines and small report rollups without modifying raw Client Data.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from psycopg2.extras import RealDictCursor, execute_values

from services.cashflow_voucher_canonicalization import (
    STATUS_CURRENT,
    STATUS_CURRENT_REVISED,
    _normalize_account_code,
    normalize_voucher_id,
)


QueryFn = Callable[[str, Sequence[Any] | None], list[dict[str, Any]]]


def _is_sqlite(conn: Any) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "sqlite3"


def _query_fn(conn: Any) -> QueryFn:
    if _is_sqlite(conn):
        def query(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
            cursor = conn.execute(sql.replace("%s", "?"), params or ())
            return [dict(row) for row in cursor.fetchall()]

        return query

    def query(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(sql, params or ())
            return [dict(row) for row in cursor.fetchall()]

    return query


def _execute(conn: Any, sql: str, params: Sequence[Any] = ()) -> None:
    if _is_sqlite(conn):
        conn.execute(sql.replace("%s", "?"), params)
        return
    with conn.cursor() as cursor:
        cursor.execute(sql, params)


def _number(value: Any) -> float:
    try:
        amount = Decimal(str(0 if value in (None, "") else value))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    return float(amount) if amount.is_finite() else 0.0


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "").replace("T", " ").split(" ", 1)[0]


def _period_keys(transaction_date: str, month: Any, fortnight: Any) -> tuple[str, str]:
    month_key = str(month or "").strip()
    fortnight_key = str(fortnight or "").strip()
    if month_key and fortnight_key:
        return month_key, fortnight_key
    if len(transaction_date) >= 10 and transaction_date[4] == "-" and transaction_date[7] == "-":
        month_key = month_key or transaction_date[:7]
        if not fortnight_key:
            try:
                fortnight_key = f"{month_key}-{'F1' if int(transaction_date[8:10]) <= 14 else 'F2'}"
            except ValueError:
                fortnight_key = ""
    return month_key, fortnight_key


def _category_lookup(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = _normalize_account_code(row.get("account_code"))
        if code:
            lookup[code] = {
                "category_id": row.get("category_id"),
                "category_key": str(row.get("category_key") or "__unassigned__"),
                "category_label": str(row.get("category_label") or ""),
            }
    return lookup


def _rollups(lines: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    voucher_sets: dict[tuple[Any, ...], set[int]] = defaultdict(set)
    for line in lines:
        for grain, period_key in (
            ("month", line.get("transaction_month")),
            ("fortnight", line.get("transaction_fortnight")),
        ):
            period = str(period_key or "__undated__")
            key = (
                grain,
                period,
                line["category_key"],
                line["account_code"],
                line["treasury_type"],
            )
            bucket = buckets.setdefault(
                key,
                {
                    "period_grain": grain,
                    "period_key": period,
                    "category_id": line.get("category_id"),
                    "category_key": line["category_key"],
                    "category_label_snapshot": line["category_label_snapshot"],
                    "account_code": line["account_code"],
                    "account_name": line["account_name"],
                    "treasury_type": line["treasury_type"],
                    "debit_total": 0.0,
                    "credit_total": 0.0,
                    "net_total": 0.0,
                    "voucher_count": 0,
                    "line_count": 0,
                },
            )
            bucket["debit_total"] += _number(line.get("debit"))
            bucket["credit_total"] += _number(line.get("credit"))
            bucket["net_total"] += _number(line.get("net_amount"))
            bucket["line_count"] += 1
            voucher_sets[key].add(int(line["canonical_occurrence_id"]))
    for key, bucket in buckets.items():
        bucket["voucher_count"] = len(voucher_sets[key])
    return list(buckets.values())

def _line_bucket_keys(lines: Sequence[Mapping[str, Any]]) -> set[tuple[str, str, str, str, str]]:
    keys: set[tuple[str, str, str, str, str]] = set()
    for line in lines:
        for grain, period_key in (
            ("month", line.get("transaction_month")),
            ("fortnight", line.get("transaction_fortnight")),
        ):
            keys.add((
                grain,
                str(period_key or "__undated__"),
                str(line.get("category_key") or "__unassigned__"),
                str(line.get("account_code") or ""),
                str(line.get("treasury_type") or "").casefold(),
            ))
    return keys


def _prepared_line_from_classification(
    row: Mapping[str, Any],
    *,
    client_id: int,
    template_id: int,
    occurrence_id: int,
    category: Mapping[str, Any],
) -> dict[str, Any]:
    transaction_date = _date_text(row.get("transaction_date"))
    month_key, fortnight_key = _period_keys(
        transaction_date,
        row.get("transaction_month"),
        row.get("transaction_fortnight"),
    )
    debit = _number(row.get("debit"))
    credit = _number(row.get("credit"))
    return {
        "client_id": client_id,
        "template_id": template_id,
        "canonical_occurrence_id": occurrence_id,
        "upload_id": int(row["upload_id"]),
        "source_client_data_id": int(row["client_data_id"]),
        "voucher_id": row.get("voucher_id"),
        "transaction_date": transaction_date or None,
        "account_code": _normalize_account_code(row.get("account_code")),
        "account_name": str(row.get("account_name") or "").strip(),
        "debit": debit,
        "credit": credit,
        "net_amount": debit - credit,
        "treasury_type": str(row.get("treasury_type") or "").casefold(),
        "category_id": category["category_id"],
        "category_label_snapshot": category["category_label"],
        "category_key": category["category_key"],
        "transaction_month": month_key,
        "transaction_fortnight": fortnight_key,
        "classification_status": str(row.get("classification_status") or "unmapped"),
    }


def _prepared_lines_for_occurrences(
    query: QueryFn,
    *,
    client_id: int,
    template_id: int,
    occurrences: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not occurrences:
        return {
            "prepared_lines": [],
            "rollups": [],
            "eligible_occurrence_count": 0,
            "excluded_ineligible_occurrence_count": 0,
            "unmatched_occurrence_count": 0,
        }
    upload_ids = sorted({int(row["upload_id"]) for row in occurrences})
    placeholders = ", ".join("%s" for _ in upload_ids)
    classifications = query(
        f"SELECT client_data_id, upload_id, voucher_id, transaction_date, account_code, account_name, debit, credit, treasury_type, transaction_month, transaction_fortnight, classification_status FROM cash_bank_transaction_classifications WHERE client_id = %s AND template_id = %s AND is_current = TRUE AND upload_id IN ({placeholders}) ORDER BY upload_id, client_data_id",
        (client_id, template_id, *upload_ids),
    )
    category_rows = query(
        "SELECT a.account_code, a.category_id, c.category_key, c.label AS category_label FROM account_control_cashflow_category_assignments a LEFT JOIN account_control_cashflow_categories c ON c.id = a.category_id AND c.client_id = a.client_id AND c.is_active = TRUE WHERE a.client_id = %s",
        (client_id,),
    )
    categories = _category_lookup(category_rows)
    lines_by_identity: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in classifications:
        identity = normalize_voucher_id(row.get("voucher_id"))
        if identity:
            lines_by_identity[(int(row["upload_id"]), identity)].append(row)
    prepared_lines = []
    excluded_ineligible = 0
    unmatched_occurrences = 0
    eligible_occurrences = 0
    for occurrence in occurrences:
        identity = normalize_voucher_id(occurrence.get("voucher_identity"))
        source_lines = lines_by_identity.get((int(occurrence["upload_id"]), identity), [])
        if not source_lines:
            unmatched_occurrences += 1
            continue
        if not any(str(row.get("treasury_type") or "").casefold() in {"cash", "bank"} for row in source_lines):
            excluded_ineligible += 1
            continue
        eligible_occurrences += 1
        for row in source_lines:
            account_code = _normalize_account_code(row.get("account_code"))
            category = categories.get(account_code, {"category_id": None, "category_key": "__unassigned__", "category_label": ""})
            prepared_lines.append(_prepared_line_from_classification(
                row,
                client_id=client_id,
                template_id=template_id,
                occurrence_id=int(occurrence["id"]),
                category=category,
            ))
    return {
        "prepared_lines": prepared_lines,
        "rollups": _rollups(prepared_lines),
        "eligible_occurrence_count": eligible_occurrences,
        "excluded_ineligible_occurrence_count": excluded_ineligible,
        "unmatched_occurrence_count": unmatched_occurrences,
    }


def build_preparation_plan(conn: Any, *, client_id: int) -> dict[str, Any]:
    """Build a complete in-memory preparation plan without writing anything."""
    query = _query_fn(conn)
    settings = query(
        """
        SELECT source_template_id
        FROM account_control_cashflow_settings
        WHERE client_id = %s AND source_template_id IS NOT NULL
        """,
        (client_id,),
    )
    if not settings:
        return {
            "ready": False,
            "client_id": client_id,
            "reason": "template_not_configured",
            "prepared_lines": [],
            "rollups": [],
        }
    template_id = int(settings[0]["source_template_id"])
    occurrences = query(
        """
        SELECT id, upload_id, voucher_id, voucher_identity, decision_status
        FROM cashflow_voucher_occurrences
        WHERE client_id = %s AND template_id = %s
          AND decision_status IN ('current', 'current_revised')
          AND NULLIF(TRIM(COALESCE(voucher_identity, '')), '') IS NOT NULL
        ORDER BY id
        """,
        (client_id, template_id),
    )
    classifications = query(
        """
        SELECT client_data_id, client_id, template_id, upload_id, voucher_id,
               transaction_date, account_code, account_name, debit, credit,
               treasury_type, transaction_month, transaction_fortnight,
               classification_status
        FROM cash_bank_transaction_classifications
        WHERE client_id = %s AND template_id = %s AND is_current = TRUE
        ORDER BY upload_id, client_data_id
        """,
        (client_id, template_id),
    )
    category_rows = query(
        """
        SELECT a.account_code, a.category_id,
               c.category_key, c.label AS category_label
        FROM account_control_cashflow_category_assignments a
        LEFT JOIN account_control_cashflow_categories c
          ON c.id = a.category_id
         AND c.client_id = a.client_id
         AND c.is_active = TRUE
        WHERE a.client_id = %s
        """,
        (client_id,),
    )
    categories = _category_lookup(category_rows)
    lines_by_identity: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in classifications:
        identity = normalize_voucher_id(row.get("voucher_id"))
        if identity:
            lines_by_identity[(int(row["upload_id"]), identity)].append(row)

    prepared_lines: list[dict[str, Any]] = []
    excluded_ineligible = 0
    unmatched_occurrences = 0
    eligible_occurrences = 0
    for occurrence in occurrences:
        identity = normalize_voucher_id(occurrence.get("voucher_identity"))
        source_lines = lines_by_identity.get((int(occurrence["upload_id"]), identity), [])
        if not source_lines:
            unmatched_occurrences += 1
            continue
        if not any(str(row.get("treasury_type") or "").casefold() in {"cash", "bank"} for row in source_lines):
            excluded_ineligible += 1
            continue
        eligible_occurrences += 1
        occurrence_id = int(occurrence["id"])
        for row in source_lines:
            account_code = _normalize_account_code(row.get("account_code"))
            category = categories.get(account_code, {
                "category_id": None,
                "category_key": "__unassigned__",
                "category_label": "",
            })
            transaction_date = _date_text(row.get("transaction_date"))
            month_key, fortnight_key = _period_keys(
                transaction_date,
                row.get("transaction_month"),
                row.get("transaction_fortnight"),
            )
            debit = _number(row.get("debit"))
            credit = _number(row.get("credit"))
            prepared_lines.append({
                "client_id": client_id,
                "template_id": template_id,
                "canonical_occurrence_id": occurrence_id,
                "upload_id": int(row["upload_id"]),
                "source_client_data_id": int(row["client_data_id"]),
                "voucher_id": row.get("voucher_id"),
                "transaction_date": transaction_date or None,
                "account_code": account_code,
                "account_name": str(row.get("account_name") or "").strip(),
                "debit": debit,
                "credit": credit,
                "net_amount": debit - credit,
                "treasury_type": str(row.get("treasury_type") or "").casefold(),
                "category_id": category["category_id"],
                "category_label_snapshot": category["category_label"],
                "category_key": category["category_key"],
                "transaction_month": month_key,
                "transaction_fortnight": fortnight_key,
                "classification_status": str(row.get("classification_status") or "unmapped"),
            })

    return {
        "ready": True,
        "client_id": client_id,
        "template_id": template_id,
        "occurrence_count": len(occurrences),
        "eligible_occurrence_count": eligible_occurrences,
        "excluded_ineligible_occurrence_count": excluded_ineligible,
        "unmatched_occurrence_count": unmatched_occurrences,
        "prepared_line_count": len(prepared_lines),
        "prepared_lines": prepared_lines,
        "rollups": _rollups(prepared_lines),
    }


def _next_revision(conn: Any, client_id: int) -> int:
    rows = _query_fn(conn)(
        "SELECT active_preparation_revision, requested_preparation_revision FROM cashflow_preparation_state WHERE client_id = %s",
        (client_id,),
    )
    if not rows:
        return 1
    return max(int(rows[0].get("active_preparation_revision") or 0), int(rows[0].get("requested_preparation_revision") or 0)) + 1


def _set_state_updating(conn: Any, client_id: int, revision: int) -> None:
    _execute(
        conn,
        """
        INSERT INTO cashflow_preparation_state
            (client_id, active_preparation_revision, requested_preparation_revision,
             status, started_at, affected_row_count, last_error)
        VALUES (%s, 0, %s, 'updating', CURRENT_TIMESTAMP, 0, NULL)
        ON CONFLICT (client_id) DO UPDATE SET
            requested_preparation_revision = EXCLUDED.requested_preparation_revision,
            status = 'updating', started_at = CURRENT_TIMESTAMP,
            completed_at = NULL, affected_row_count = 0, last_error = NULL
        """,
        (client_id, revision),
    )


def _write_values(conn: Any, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]], conflict: str, updates: str) -> None:
    if not rows:
        return
    if _is_sqlite(conn):
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
        conn.executemany(sql, rows)
        return
    template = "(" + ", ".join("%s" for _ in columns) + ")"
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
    with conn.cursor() as cursor:
        execute_values(cursor, sql, rows, template=template, page_size=1000)


def prepare_client(conn: Any, *, client_id: int, preparation_revision: int | None = None) -> dict[str, Any]:
    """Materialize one complete revision; the caller owns commit/rollback."""
    plan = build_preparation_plan(conn, client_id=client_id)
    if not plan["ready"]:
        return plan
    revision = int(preparation_revision or _next_revision(conn, client_id))
    _set_state_updating(conn, client_id, revision)
    _execute(conn, "DELETE FROM cashflow_prepared_rollups WHERE client_id = %s AND preparation_revision = %s", (client_id, revision))
    _execute(conn, "DELETE FROM cashflow_prepared_lines WHERE client_id = %s AND preparation_revision = %s", (client_id, revision))

    line_columns = (
        "client_id", "template_id", "preparation_revision", "canonical_occurrence_id", "upload_id",
        "source_client_data_id", "voucher_id", "transaction_date", "account_code", "account_name",
        "debit", "credit", "net_amount", "treasury_type", "category_id", "category_label_snapshot",
        "category_key", "transaction_month", "transaction_fortnight", "classification_status",
    )
    source_line_columns = tuple(column for column in line_columns if column != "preparation_revision")
    line_rows = [tuple(line.get(column) for column in source_line_columns) for line in plan["prepared_lines"]]
    if line_rows:
        line_rows = [
            (
                row[0], row[1], revision, *row[2:]
            )
            for row in line_rows
        ]
        _write_values(
            conn,
            "cashflow_prepared_lines",
            line_columns,
            line_rows,
            "client_id, preparation_revision, canonical_occurrence_id, source_client_data_id",
            ", ".join(f"{column}=excluded.{column}" for column in line_columns[5:]),
        )

    rollup_columns = (
        "client_id", "template_id", "preparation_revision", "period_grain", "period_key", "category_id",
        "category_key", "category_label_snapshot", "account_code", "account_name", "treasury_type",
        "debit_total", "credit_total", "net_total", "voucher_count", "line_count",
    )
    rollup_rows = [
        tuple([client_id, plan["template_id"], revision] + [rollup[column] for column in rollup_columns[3:]])
        for rollup in plan["rollups"]
    ]
    _write_values(
        conn,
        "cashflow_prepared_rollups",
        rollup_columns,
        rollup_rows,
        "client_id, preparation_revision, period_grain, period_key, category_key, account_code, treasury_type",
        ", ".join(f"{column}=excluded.{column}" for column in rollup_columns[5:]),
    )
    _execute(
        conn,
        """
        UPDATE cashflow_preparation_state
        SET active_preparation_revision = %s,
            requested_preparation_revision = %s,
            status = 'ready', completed_at = CURRENT_TIMESTAMP,
            affected_row_count = %s, last_successful_build_at = CURRENT_TIMESTAMP,
            last_error = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE client_id = %s
        """,
        (revision, revision, len(plan["prepared_lines"]), client_id),
    )
    return {
        **{key: value for key, value in plan.items() if key not in {"prepared_lines", "rollups"}},
        "preparation_revision": revision,
        "prepared_line_count": len(plan["prepared_lines"]),
        "rollup_count": len(plan["rollups"]),
    }


def _copy_prepared_revision(conn: Any, *, client_id: int, source_revision: int, target_revision: int) -> None:
    _execute(conn, """
        INSERT INTO cashflow_prepared_lines (
            client_id, template_id, preparation_revision, canonical_occurrence_id, upload_id,
            source_client_data_id, voucher_id, transaction_date, account_code, account_name,
            debit, credit, net_amount, treasury_type, category_id, category_label_snapshot,
            category_key, transaction_month, transaction_fortnight, classification_status
        )
        SELECT client_id, template_id, %s, canonical_occurrence_id, upload_id,
               source_client_data_id, voucher_id, transaction_date, account_code, account_name,
               debit, credit, net_amount, treasury_type, category_id, category_label_snapshot,
               category_key, transaction_month, transaction_fortnight, classification_status
        FROM cashflow_prepared_lines
        WHERE client_id = %s AND preparation_revision = %s
        """, (target_revision, client_id, source_revision))
    _execute(conn, """
        INSERT INTO cashflow_prepared_rollups (
            client_id, template_id, preparation_revision, period_grain, period_key, category_id,
            category_key, category_label_snapshot, account_code, account_name, treasury_type,
            debit_total, credit_total, net_total, voucher_count, line_count
        )
        SELECT client_id, template_id, %s, period_grain, period_key, category_id,
               category_key, category_label_snapshot, account_code, account_name, treasury_type,
               debit_total, credit_total, net_total, voucher_count, line_count
        FROM cashflow_prepared_rollups
        WHERE client_id = %s AND preparation_revision = %s
        """, (target_revision, client_id, source_revision))


def _delete_rollup_keys(conn: Any, *, client_id: int, preparation_revision: int, keys: set[tuple[str, str, str, str, str]]) -> None:
    for grain, period_key, category_key, account_code, treasury_type in keys:
        _execute(conn, """
            DELETE FROM cashflow_prepared_rollups
            WHERE client_id = %s AND preparation_revision = %s
              AND period_grain = %s AND period_key = %s
              AND category_key = %s AND account_code = %s AND treasury_type = %s
            """, (client_id, preparation_revision, grain, period_key, category_key, account_code, treasury_type))


def _rollups_for_keys(conn: Any, *, client_id: int, preparation_revision: int, keys: set[tuple[str, str, str, str, str]]) -> list[dict[str, Any]]:
    query = _query_fn(conn)
    rollups = []
    for grain, period_key, category_key, account_code, treasury_type in keys:
        period_column = "transaction_month" if grain == "month" else "transaction_fortnight"
        rows = query(
            f"""SELECT canonical_occurrence_id, category_id, category_key, category_label_snapshot,
                       account_code, account_name, debit, credit, net_amount, treasury_type,
                       transaction_month, transaction_fortnight
                FROM cashflow_prepared_lines
                WHERE client_id = %s AND preparation_revision = %s
                  AND COALESCE(NULLIF({period_column}, ''), '__undated__') = %s
                  AND category_key = %s AND account_code = %s AND treasury_type = %s""",
            (client_id, preparation_revision, period_key, category_key, account_code, treasury_type),
        )
        rollups.extend(_rollups(rows))
    return rollups


def _insert_prepared_lines(conn: Any, *, revision: int, lines: Sequence[Mapping[str, Any]]) -> None:
    if not lines:
        return
    columns = (
        "client_id", "template_id", "preparation_revision", "canonical_occurrence_id", "upload_id",
        "source_client_data_id", "voucher_id", "transaction_date", "account_code", "account_name",
        "debit", "credit", "net_amount", "treasury_type", "category_id", "category_label_snapshot",
        "category_key", "transaction_month", "transaction_fortnight", "classification_status",
    )
    source_columns = tuple(column for column in columns if column != "preparation_revision")
    rows = [tuple(line.get(column) for column in source_columns) for line in lines]
    rows = [(row[0], row[1], revision, *row[2:]) for row in rows]
    _write_values(
        conn, "cashflow_prepared_lines", columns, rows,
        "client_id, preparation_revision, canonical_occurrence_id, source_client_data_id",
        ", ".join(f"{column}=excluded.{column}" for column in columns[5:]),
    )


def _insert_rollups(conn: Any, *, revision: int, client_id: int, template_id: int, rollups: Sequence[Mapping[str, Any]]) -> None:
    if not rollups:
        return
    columns = (
        "client_id", "template_id", "preparation_revision", "period_grain", "period_key", "category_id",
        "category_key", "category_label_snapshot", "account_code", "account_name", "treasury_type",
        "debit_total", "credit_total", "net_total", "voucher_count", "line_count",
    )
    rows = [tuple([client_id, template_id, revision] + [rollup[column] for column in columns[3:]]) for rollup in rollups]
    _write_values(
        conn, "cashflow_prepared_rollups", columns, rows,
        "client_id, preparation_revision, period_grain, period_key, category_key, account_code, treasury_type",
        ", ".join(f"{column}=excluded.{column}" for column in columns[5:]),
    )


def prepare_incremental_client(
    conn: Any,
    *,
    client_id: int,
    template_id: int,
    affected_occurrence_ids: Sequence[int],
    current_occurrence_ids: Sequence[int],
    preparation_revision: int | None = None,
) -> dict[str, Any]:
    """Publish a copy-on-write revision for changed voucher occurrences."""
    affected_ids = sorted({int(identifier) for identifier in affected_occurrence_ids})
    current_ids = sorted({int(identifier) for identifier in current_occurrence_ids})
    query = _query_fn(conn)
    if not affected_ids:
        state = query("SELECT active_preparation_revision FROM cashflow_preparation_state WHERE client_id = %s", (client_id,))
        return {
            "ready": True,
            "changed": False,
            "reason": "no_prepared_changes",
            "client_id": client_id,
            "preparation_revision": int(state[0].get("active_preparation_revision") or 0) if state else 0,
            "prepared_line_count": 0,
            "rollup_count": 0,
        }
    configured = query("SELECT source_template_id FROM account_control_cashflow_settings WHERE client_id = %s", (client_id,))
    if not configured or int(configured[0].get("source_template_id") or 0) != int(template_id):
        return {"ready": False, "changed": False, "reason": "template_not_configured", "client_id": client_id}
    state = query(
        "SELECT active_preparation_revision FROM cashflow_preparation_state WHERE client_id = %s",
        (client_id,),
    )
    active_revision = int(state[0].get("active_preparation_revision") or 0) if state else 0
    if active_revision <= 0:
        return {"ready": False, "changed": False, "reason": "active_revision_missing", "client_id": client_id}
    current_placeholders = ", ".join("%s" for _ in current_ids) or "NULL"
    occurrences = query(
        f"""SELECT id, upload_id, voucher_id, voucher_identity, decision_status
            FROM cashflow_voucher_occurrences
            WHERE client_id = %s AND template_id = %s AND id IN ({current_placeholders})
              AND decision_status IN ('current', 'current_revised')
              AND NULLIF(TRIM(COALESCE(voucher_identity, '')), '') IS NOT NULL
            ORDER BY id""",
        (client_id, template_id, *current_ids),
    )
    line_plan = _prepared_lines_for_occurrences(
        query, client_id=client_id, template_id=template_id, occurrences=occurrences
    )
    affected_placeholders = ", ".join("%s" for _ in affected_ids)
    old_lines = query(
        f"""SELECT canonical_occurrence_id, category_id, category_key, category_label_snapshot,
                   account_code, account_name, debit, credit, net_amount, treasury_type,
                   transaction_month, transaction_fortnight
            FROM cashflow_prepared_lines
            WHERE client_id = %s AND preparation_revision = %s
              AND canonical_occurrence_id IN ({affected_placeholders})""",
        (client_id, active_revision, *affected_ids),
    )
    touched_keys = _line_bucket_keys(old_lines) | _line_bucket_keys(line_plan["prepared_lines"])
    revision = int(preparation_revision or _next_revision(conn, client_id))
    if revision == active_revision:
        revision = _next_revision(conn, client_id)
    _set_state_updating(conn, client_id, revision)
    _execute(conn, "DELETE FROM cashflow_prepared_rollups WHERE client_id = %s AND preparation_revision = %s", (client_id, revision))
    _execute(conn, "DELETE FROM cashflow_prepared_lines WHERE client_id = %s AND preparation_revision = %s", (client_id, revision))
    _copy_prepared_revision(conn, client_id=client_id, source_revision=active_revision, target_revision=revision)
    _execute(
        conn,
        f"""DELETE FROM cashflow_prepared_lines
            WHERE client_id = %s AND preparation_revision = %s
              AND canonical_occurrence_id IN ({affected_placeholders})""",
        (client_id, revision, *affected_ids),
    )
    _insert_prepared_lines(conn, revision=revision, lines=line_plan["prepared_lines"])
    _delete_rollup_keys(conn, client_id=client_id, preparation_revision=revision, keys=touched_keys)
    replacement_rollups = _rollups_for_keys(
        conn, client_id=client_id, preparation_revision=revision, keys=touched_keys
    )
    _insert_rollups(
        conn, revision=revision, client_id=client_id, template_id=template_id, rollups=replacement_rollups
    )
    _execute(
        conn,
        """UPDATE cashflow_preparation_state
           SET active_preparation_revision = %s, requested_preparation_revision = %s,
               status = 'ready', completed_at = CURRENT_TIMESTAMP,
               affected_row_count = %s, last_successful_build_at = CURRENT_TIMESTAMP,
               last_error = NULL, updated_at = CURRENT_TIMESTAMP
           WHERE client_id = %s""",
        (revision, revision, len(line_plan["prepared_lines"]), client_id),
    )
    return {
        "ready": True,
        "changed": True,
        "reason": "incremental_upload",
        "client_id": client_id,
        "template_id": template_id,
        "preparation_revision": revision,
        "affected_occurrence_count": len(affected_ids),
        "current_occurrence_count": len(occurrences),
        "eligible_occurrence_count": line_plan["eligible_occurrence_count"],
        "excluded_ineligible_occurrence_count": line_plan["excluded_ineligible_occurrence_count"],
        "unmatched_occurrence_count": line_plan["unmatched_occurrence_count"],
        "prepared_line_count": len(line_plan["prepared_lines"]),
        "rollup_count": len(replacement_rollups),
    }


def _no_prepared_change_result(query: QueryFn, *, client_id: int, reason: str) -> dict[str, Any]:
    state = query(
        "SELECT active_preparation_revision FROM cashflow_preparation_state WHERE client_id = %s",
        (client_id,),
    )
    return {
        "ready": True,
        "changed": False,
        "reason": reason,
        "client_id": client_id,
        "preparation_revision": int(state[0].get("active_preparation_revision") or 0) if state else 0,
        "prepared_line_count": 0,
        "rollup_count": 0,
    }


def prepare_incremental_category_client(
    conn: Any,
    *,
    client_id: int,
    account_codes: Sequence[str] = (),
    category_ids: Sequence[int] = (),
    preparation_revision: int | None = None,
) -> dict[str, Any]:
    """Publish a revision for only changed Cashflows category assignments."""
    query = _query_fn(conn)
    normalized_codes = sorted({
        code
        for code in (_normalize_account_code(value) for value in account_codes)
        if code
    })
    normalized_category_ids = sorted({int(value) for value in category_ids})
    if not normalized_codes and not normalized_category_ids:
        return _no_prepared_change_result(query, client_id=client_id, reason="no_category_changes")

    configured = query(
        "SELECT source_template_id FROM account_control_cashflow_settings WHERE client_id = %s",
        (client_id,),
    )
    if not configured or not configured[0].get("source_template_id"):
        return {"ready": False, "changed": False, "reason": "template_not_configured", "client_id": client_id}
    template_id = int(configured[0]["source_template_id"])
    state = query(
        "SELECT active_preparation_revision FROM cashflow_preparation_state WHERE client_id = %s",
        (client_id,),
    )
    active_revision = int(state[0].get("active_preparation_revision") or 0) if state else 0
    if active_revision <= 0:
        return {"ready": False, "changed": False, "reason": "active_revision_missing", "client_id": client_id}

    if normalized_category_ids:
        placeholders = ", ".join("%s" for _ in normalized_category_ids)
        assigned_codes = query(
            f"""SELECT DISTINCT account_code
                FROM account_control_cashflow_category_assignments
                WHERE client_id = %s AND category_id IN ({placeholders})""",
            (client_id, *normalized_category_ids),
        )
        normalized_codes = sorted({
            code
            for code in normalized_codes + [
                _normalize_account_code(row.get("account_code")) for row in assigned_codes
            ]
            if code
        })

    if not normalized_codes:
        return _no_prepared_change_result(query, client_id=client_id, reason="no_prepared_changes")

    code_placeholders = ", ".join("%s" for _ in normalized_codes)
    old_lines = query(
        f"""SELECT canonical_occurrence_id, category_id, category_key, category_label_snapshot,
                   account_code, account_name, debit, credit, net_amount, treasury_type,
                   transaction_month, transaction_fortnight
            FROM cashflow_prepared_lines
            WHERE client_id = %s AND preparation_revision = %s
              AND account_code IN ({code_placeholders})""",
        (client_id, active_revision, *normalized_codes),
    )
    if not old_lines:
        return _no_prepared_change_result(query, client_id=client_id, reason="no_prepared_changes")

    category_rows = query(
        f"""SELECT a.account_code, a.category_id, c.category_key, c.label AS category_label
            FROM account_control_cashflow_category_assignments a
            LEFT JOIN account_control_cashflow_categories c
              ON c.id = a.category_id
             AND c.client_id = a.client_id
             AND c.is_active = TRUE
            WHERE a.client_id = %s AND a.account_code IN ({code_placeholders})""",
        (client_id, *normalized_codes),
    )
    categories = _category_lookup(category_rows)
    revision = int(preparation_revision or _next_revision(conn, client_id))
    if revision == active_revision:
        revision = _next_revision(conn, client_id)
    _set_state_updating(conn, client_id, revision)
    _execute(conn, "DELETE FROM cashflow_prepared_rollups WHERE client_id = %s AND preparation_revision = %s", (client_id, revision))
    _execute(conn, "DELETE FROM cashflow_prepared_lines WHERE client_id = %s AND preparation_revision = %s", (client_id, revision))
    _copy_prepared_revision(conn, client_id=client_id, source_revision=active_revision, target_revision=revision)
    for account_code in normalized_codes:
        category = categories.get(account_code, {
            "category_id": None,
            "category_key": "__unassigned__",
            "category_label": "",
        })
        _execute(
            conn,
            """UPDATE cashflow_prepared_lines
               SET category_id = %s, category_label_snapshot = %s, category_key = %s
               WHERE client_id = %s AND preparation_revision = %s AND account_code = %s""",
            (
                category["category_id"],
                category["category_label"],
                category["category_key"],
                client_id,
                revision,
                account_code,
            ),
        )
    new_lines = query(
        f"""SELECT canonical_occurrence_id, category_id, category_key, category_label_snapshot,
                   account_code, account_name, debit, credit, net_amount, treasury_type,
                   transaction_month, transaction_fortnight
            FROM cashflow_prepared_lines
            WHERE client_id = %s AND preparation_revision = %s
              AND account_code IN ({code_placeholders})""",
        (client_id, revision, *normalized_codes),
    )
    touched_keys = _line_bucket_keys(old_lines) | _line_bucket_keys(new_lines)
    _delete_rollup_keys(conn, client_id=client_id, preparation_revision=revision, keys=touched_keys)
    replacement_rollups = _rollups_for_keys(
        conn, client_id=client_id, preparation_revision=revision, keys=touched_keys
    )
    _insert_rollups(
        conn, revision=revision, client_id=client_id, template_id=template_id, rollups=replacement_rollups
    )
    _execute(
        conn,
        """UPDATE cashflow_preparation_state
           SET active_preparation_revision = %s, requested_preparation_revision = %s,
               status = 'ready', completed_at = CURRENT_TIMESTAMP,
               affected_row_count = %s, last_successful_build_at = CURRENT_TIMESTAMP,
               last_error = NULL, updated_at = CURRENT_TIMESTAMP
           WHERE client_id = %s""",
        (revision, revision, len(new_lines), client_id),
    )
    return {
        "ready": True,
        "changed": True,
        "reason": "incremental_category",
        "client_id": client_id,
        "template_id": template_id,
        "preparation_revision": revision,
        "affected_account_code_count": len(normalized_codes),
        "prepared_line_count": len(new_lines),
        "rollup_count": len(replacement_rollups),
    }


def prepare_incremental_treasury_client(
    conn: Any,
    *,
    client_id: int,
    account_codes: Sequence[str],
    preparation_revision: int | None = None,
) -> dict[str, Any]:
    """Republish only vouchers affected by a Cash/Bank account selection change."""
    query = _query_fn(conn)
    normalized_codes = sorted({
        code
        for code in (_normalize_account_code(value) for value in account_codes)
        if code
    })
    if not normalized_codes:
        return _no_prepared_change_result(query, client_id=client_id, reason="no_treasury_changes")
    configured = query(
        "SELECT source_template_id FROM account_control_cashflow_settings WHERE client_id = %s",
        (client_id,),
    )
    if not configured or not configured[0].get("source_template_id"):
        return {"ready": False, "changed": False, "reason": "template_not_configured", "client_id": client_id}
    template_id = int(configured[0]["source_template_id"])
    code_placeholders = ", ".join("%s" for _ in normalized_codes)
    classifications = query(
        f"""SELECT upload_id, voucher_id
            FROM cash_bank_transaction_classifications
            WHERE client_id = %s AND template_id = %s AND is_current = TRUE
              AND account_code IN ({code_placeholders})""",
        (client_id, template_id, *normalized_codes),
    )
    occurrences = query(
        """SELECT id, upload_id, voucher_identity
           FROM cashflow_voucher_occurrences
           WHERE client_id = %s AND template_id = %s
             AND decision_status IN ('current', 'current_revised')
             AND NULLIF(TRIM(COALESCE(voucher_identity, '')), '') IS NOT NULL""",
        (client_id, template_id),
    )
    occurrence_by_identity = {
        (int(row["upload_id"]), normalize_voucher_id(row.get("voucher_identity"))): int(row["id"])
        for row in occurrences
        if normalize_voucher_id(row.get("voucher_identity"))
    }
    current_ids = sorted({
        occurrence_by_identity[(int(row["upload_id"]), identity)]
        for row in classifications
        if (identity := normalize_voucher_id(row.get("voucher_id")))
        and (int(row["upload_id"]), identity) in occurrence_by_identity
    })
    state = query(
        "SELECT active_preparation_revision FROM cashflow_preparation_state WHERE client_id = %s",
        (client_id,),
    )
    active_revision = int(state[0].get("active_preparation_revision") or 0) if state else 0
    old_ids = []
    if active_revision > 0:
        old_rows = query(
            f"""SELECT DISTINCT canonical_occurrence_id
                FROM cashflow_prepared_lines
                WHERE client_id = %s AND preparation_revision = %s
                  AND account_code IN ({code_placeholders})""",
            (client_id, active_revision, *normalized_codes),
        )
        old_ids = [int(row["canonical_occurrence_id"]) for row in old_rows]
    affected_ids = sorted(set(old_ids) | set(current_ids))
    if not affected_ids:
        return _no_prepared_change_result(query, client_id=client_id, reason="no_prepared_changes")
    result = prepare_incremental_client(
        conn,
        client_id=client_id,
        template_id=template_id,
        affected_occurrence_ids=affected_ids,
        current_occurrence_ids=current_ids,
        preparation_revision=preparation_revision,
    )
    if result.get("changed"):
        result["reason"] = "incremental_treasury"
    return result
