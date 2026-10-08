"""Shared persistence for Cashflows canonical voucher occurrences.

The store is deliberately separate from report SQL. It resolves only supplied
voucher identities and stores audit decisions without altering source data.
"""

from __future__ import annotations

from collections import Counter
import json
from typing import Any, Mapping, Sequence

from psycopg2.extras import RealDictCursor, execute_values

from services.cashflow_voucher_canonicalization import (
    build_voucher_occurrences,
    normalize_voucher_id,
    resolve_voucher_occurrences,
)
from services.ledger_report_source import CASHFLOW_REQUIRED_COLUMN_ROLES, build_report_source


def _write_values(decision: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        decision["client_id"], decision["template_id"], decision["upload_id"],
        decision.get("voucher_id"), decision.get("voucher_identity"),
        decision["occurrence_key"], decision["voucher_fingerprint"],
        decision["fingerprint_version"], decision.get("transaction_date"),
        decision.get("source_line_count") or 0, decision.get("source_extract_as_of"),
        decision["decision_status"], decision.get("decision_reason"),
    )


def persist_sqlite_occurrences(conn: Any, decisions: Sequence[Mapping[str, Any]]) -> None:
    """Upsert resolved decisions without committing the caller's transaction."""
    if not decisions:
        return
    conn.executemany(
        """INSERT INTO cashflow_voucher_occurrences (
            client_id, template_id, upload_id, voucher_id, voucher_identity,
            occurrence_key, voucher_fingerprint, fingerprint_version,
            transaction_date, source_line_count, source_extract_as_of,
            decision_status, decision_reason, canonical_occurrence_id, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, CURRENT_TIMESTAMP)
        ON CONFLICT(client_id, template_id, upload_id, occurrence_key) DO UPDATE SET
            voucher_id=excluded.voucher_id, voucher_identity=excluded.voucher_identity,
            voucher_fingerprint=excluded.voucher_fingerprint, fingerprint_version=excluded.fingerprint_version,
            transaction_date=excluded.transaction_date, source_line_count=excluded.source_line_count,
            source_extract_as_of=excluded.source_extract_as_of, decision_status=excluded.decision_status,
            decision_reason=excluded.decision_reason, canonical_occurrence_id=NULL, resolved_at=CURRENT_TIMESTAMP""",
        [_write_values(item) for item in decisions],
    )
    for client_id, template_id in sorted({(item["client_id"], item["template_id"]) for item in decisions}):
        ids = {
            row["occurrence_key"]: row["id"]
            for row in conn.execute(
                "SELECT id, occurrence_key FROM cashflow_voucher_occurrences WHERE client_id=? AND template_id=?",
                (client_id, template_id),
            ).fetchall()
        }
        updates = [
            (ids.get(item.get("canonical_occurrence_key")), item["decision_status"], item.get("decision_reason"),
             item["client_id"], item["template_id"], item["upload_id"], item["occurrence_key"])
            for item in decisions if item["client_id"] == client_id and item["template_id"] == template_id
        ]
        conn.executemany(
            """UPDATE cashflow_voucher_occurrences
               SET canonical_occurrence_id=?, decision_status=?, decision_reason=?, resolved_at=CURRENT_TIMESTAMP
               WHERE client_id=? AND template_id=? AND upload_id=? AND occurrence_key=?""",
            updates,
        )


def persist_postgres_occurrences(conn: Any, decisions: Sequence[Mapping[str, Any]]) -> None:
    """PostgreSQL equivalent; caller owns commit/rollback."""
    if not decisions:
        return
    with conn.cursor() as cursor:
        execute_values(
            cursor,
            """INSERT INTO cashflow_voucher_occurrences (
                client_id, template_id, upload_id, voucher_id, voucher_identity,
                occurrence_key, voucher_fingerprint, fingerprint_version,
                transaction_date, source_line_count, source_extract_as_of,
                decision_status, decision_reason, canonical_occurrence_id, resolved_at
            ) VALUES %s
            ON CONFLICT(client_id, template_id, upload_id, occurrence_key) DO UPDATE SET
                voucher_id=EXCLUDED.voucher_id, voucher_identity=EXCLUDED.voucher_identity,
                voucher_fingerprint=EXCLUDED.voucher_fingerprint, fingerprint_version=EXCLUDED.fingerprint_version,
                transaction_date=EXCLUDED.transaction_date, source_line_count=EXCLUDED.source_line_count,
                source_extract_as_of=EXCLUDED.source_extract_as_of, decision_status=EXCLUDED.decision_status,
                decision_reason=EXCLUDED.decision_reason, canonical_occurrence_id=NULL, resolved_at=CURRENT_TIMESTAMP""",
            [_write_values(item) for item in decisions],
            template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, CURRENT_TIMESTAMP)",
            page_size=1000,
        )
        for client_id, template_id in sorted({(item["client_id"], item["template_id"]) for item in decisions}):
            cursor.execute(
                "SELECT id, occurrence_key FROM cashflow_voucher_occurrences WHERE client_id=%s AND template_id=%s",
                (client_id, template_id),
            )
            ids = {key: identifier for identifier, key in cursor.fetchall()}
            updates = [
                (ids.get(item.get("canonical_occurrence_key")), item["decision_status"], item.get("decision_reason"),
                 item["client_id"], item["template_id"], item["upload_id"], item["occurrence_key"])
                for item in decisions if item["client_id"] == client_id and item["template_id"] == template_id
            ]
            execute_values(
                cursor,
                """UPDATE cashflow_voucher_occurrences AS target
                   SET canonical_occurrence_id=data.canonical_occurrence_id,
                       decision_status=data.decision_status,
                       decision_reason=data.decision_reason,
                       resolved_at=CURRENT_TIMESTAMP
                   FROM (VALUES %s) AS data(
                       canonical_occurrence_id, decision_status, decision_reason,
                       client_id, template_id, upload_id, occurrence_key
                   )
                   WHERE target.client_id=data.client_id
                     AND target.template_id=data.template_id
                     AND target.upload_id=data.upload_id
                     AND target.occurrence_key=data.occurrence_key""",
                updates,
                template="(%s, %s, %s, %s, %s, %s, %s)",
                page_size=1000,
            )


def _query(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]




def _resolve_occurrence_input_lines_postgres(
    conn: Any,
    *,
    client_id: int,
    template_id: int,
    input_lines: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    incoming = build_voucher_occurrences(input_lines)
    identities = sorted({item["voucher_identity"] for item in incoming if item.get("voucher_identity")})
    existing: list[dict[str, Any]] = []
    prior_fingerprints: dict[str, str] = {}
    if identities:
        existing = _query(
            conn,
            """
            SELECT id, client_id, template_id, upload_id, voucher_id, voucher_identity, occurrence_key,
                   voucher_fingerprint, fingerprint_version, transaction_date, source_line_count,
                   source_extract_as_of, decision_status
            FROM cashflow_voucher_occurrences
            WHERE client_id=%s AND template_id=%s AND voucher_identity = ANY(%s)
            """,
            (client_id, template_id, identities),
        )
        prior_fingerprints = {
            str(row["voucher_identity"]): str(row["voucher_fingerprint"])
            for row in existing
            if row.get("decision_status") in {"current", "current_revised"}
        }
    incoming_keys = {item["occurrence_key"] for item in incoming}
    existing = [
        {
            **row,
            "source_sequence": row.get("source_extract_as_of") or row.get("upload_id"),
            "has_multiple_dates": False,
        }
        for row in existing
        if row.get("occurrence_key") not in incoming_keys
    ]
    decisions = resolve_voucher_occurrences([*existing, *incoming])
    persist_postgres_occurrences(conn, decisions)
    current_fingerprints = {
        str(item["voucher_identity"]): str(item["voucher_fingerprint"])
        for item in decisions
        if item.get("voucher_identity")
        and item.get("decision_status") in {"current", "current_revised"}
    }
    changed_identities = sorted(
        identity
        for identity, fingerprint in current_fingerprints.items()
        if prior_fingerprints.get(identity) != fingerprint
    )
    changed_rows = _query(
        conn,
        """SELECT id, decision_status
           FROM cashflow_voucher_occurrences
           WHERE client_id=%s AND template_id=%s AND voucher_identity = ANY(%s)""",
        (client_id, template_id, changed_identities),
    ) if changed_identities else []
    affected_occurrence_ids = sorted(int(row["id"]) for row in changed_rows)
    current_occurrence_ids = sorted(
        int(row["id"])
        for row in changed_rows
        if row.get("decision_status") in {"current", "current_revised"}
    )
    counts = Counter(
        item["decision_status"]
        for item in decisions
        if item.get("occurrence_key") in incoming_keys
    )
    return {
        "eligible": True,
        "occurrence_count": len(incoming),
        "decision_counts": dict(sorted(counts.items())),
        "changed_identity_count": len(changed_identities),
        "changed_identities": changed_identities,
        "affected_occurrence_ids": affected_occurrence_ids,
        "current_occurrence_ids": current_occurrence_ids,
        "preparation_required": bool(changed_identities),
        "cache_invalidation_required": bool(changed_identities),
    }


def resolve_classified_upload_occurrences_postgres(
    conn: Any,
    *,
    client_id: int,
    template_id: int,
    upload_id: int,
) -> dict[str, Any]:
    """Resolve one historical upload from its current prepared classifications."""
    configured = _query(
        conn,
        "SELECT source_template_id FROM account_control_cashflow_settings WHERE client_id=%s",
        (client_id,),
    )
    if not configured or configured[0].get("source_template_id") != template_id:
        return {"eligible": False, "reason": "template_not_configured", "cache_invalidation_required": False}
    upload = _query(conn, "SELECT upload_date FROM uploaded_files WHERE id=%s", (upload_id,))
    source_extract_as_of = str(upload[0].get("upload_date") or "") if upload else ""
    rows = _query(
        conn,
        """
        SELECT voucher_id, account_code, account_name, transaction_date, debit, credit
        FROM cash_bank_transaction_classifications
        WHERE client_id=%s AND template_id=%s AND upload_id=%s AND is_current=TRUE
        ORDER BY client_data_id
        """,
        (client_id, template_id, upload_id),
    )
    if not rows:
        return {"eligible": False, "reason": "no_current_classifications", "cache_invalidation_required": False}
    input_lines = [
        {
            "client_id": client_id,
            "template_id": template_id,
            "upload_id": upload_id,
            "voucher_id": row.get("voucher_id"),
            "transaction_date": row.get("transaction_date"),
            "account_code": row.get("account_code"),
            "account_name": row.get("account_name"),
            "debit": row.get("debit"),
            "credit": row.get("credit"),
            "source_extract_as_of": source_extract_as_of or None,
            "source_sequence": source_extract_as_of or upload_id,
        }
        for row in rows
    ]
    result = _resolve_occurrence_input_lines_postgres(
        conn,
        client_id=client_id,
        template_id=template_id,
        input_lines=input_lines,
    )
    result["source_line_count"] = len(rows)
    return result


def backfill_cashflow_voucher_occurrences_postgres(
    conn: Any,
    *,
    client_id: int,
    template_id: int,
) -> dict[str, Any]:
    """Rebuild the canonical register from current prepared classifications.

    The caller owns the transaction. Raw Client Data and category assignments
    are never modified; the register is a rebuildable derived artifact.
    """
    configured = _query(
        conn,
        "SELECT source_template_id FROM account_control_cashflow_settings WHERE client_id=%s",
        (client_id,),
    )
    if not configured or configured[0].get("source_template_id") != template_id:
        return {"eligible": False, "reason": "template_not_configured", "cache_invalidation_required": False}
    uploads = _query(
        conn,
        """
        SELECT c.upload_id, MAX(f.upload_date) AS upload_date
        FROM cash_bank_transaction_classifications c
        LEFT JOIN uploaded_files f ON f.id=c.upload_id
        WHERE c.client_id=%s AND c.template_id=%s AND c.is_current=TRUE
        GROUP BY c.upload_id
        ORDER BY MAX(f.upload_date) NULLS FIRST, c.upload_id
        """,
        (client_id, template_id),
    )
    input_lines: list[dict[str, Any]] = []
    source_line_count = 0
    for upload in uploads:
        upload_id = int(upload["upload_id"])
        rows = _query(
            conn,
            """
            SELECT voucher_id, account_code, account_name, transaction_date, debit, credit
            FROM cash_bank_transaction_classifications
            WHERE client_id=%s AND template_id=%s AND upload_id=%s AND is_current=TRUE
            ORDER BY client_data_id
            """,
            (client_id, template_id, upload_id),
        )
        source_extract_as_of = str(upload.get("upload_date") or "") or None
        for row in rows:
            input_lines.append({
                "client_id": client_id,
                "template_id": template_id,
                "upload_id": upload_id,
                "voucher_id": row.get("voucher_id"),
                "transaction_date": row.get("transaction_date"),
                "account_code": row.get("account_code"),
                "account_name": row.get("account_name"),
                "debit": row.get("debit"),
                "credit": row.get("credit"),
                "source_extract_as_of": source_extract_as_of,
                "source_sequence": source_extract_as_of or upload_id,
            })
        source_line_count += len(rows)
    decisions = resolve_voucher_occurrences(build_voucher_occurrences(input_lines))
    with conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM cashflow_voucher_occurrences WHERE client_id=%s AND template_id=%s",
            (client_id, template_id),
        )
    persist_postgres_occurrences(conn, decisions)
    totals = Counter(item["decision_status"] for item in decisions)
    return {
        "eligible": True,
        "reason": "backfilled",
        "upload_count": len(uploads),
        "occurrence_count": len(decisions),
        "source_line_count": source_line_count,
        "decision_counts": dict(sorted(totals.items())),
        "cache_invalidation_required": bool(decisions),
    }


def resolve_upload_occurrences_postgres(
    conn: Any,
    *,
    client_id: int,
    template_id: int,
    upload_id: int,
    row_data: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Persist decisions for one newly imported workbook."""
    configured = _query(
        conn,
        "SELECT source_template_id FROM account_control_cashflow_settings WHERE client_id=%s",
        (client_id,),
    )
    if not configured or configured[0].get("source_template_id") != template_id:
        return {"eligible": False, "reason": "template_not_configured", "cache_invalidation_required": False}
    mappings = _query(
        conn,
        """SELECT m.role, c.column_name FROM account_control_cashflow_source_mappings m
           JOIN master_columns c ON c.id=m.template_column_id
           WHERE m.client_id=%s AND m.template_id=%s""",
        (client_id, template_id),
    )
    mapping = {str(row["role"]): str(row["column_name"]) for row in mappings}
    missing = [role for role in CASHFLOW_REQUIRED_COLUMN_ROLES if role not in mapping]
    if missing:
        return {"eligible": False, "reason": "mapping_incomplete", "cache_invalidation_required": False}
    upload = _query(conn, "SELECT upload_date FROM uploaded_files WHERE id=%s", (upload_id,))
    source_extract_as_of = str(upload[0].get("upload_date") or "") if upload else ""
    normalized = build_report_source([{"row_data": dict(row)} for row in row_data], column_mapping=mapping)
    if not normalized.ready:
        return {"eligible": False, "reason": "incoming_source_invalid", "diagnostics": list(normalized.diagnostics), "cache_invalidation_required": False}
    input_lines = [
        {
            "client_id": client_id,
            "template_id": template_id,
            "upload_id": upload_id,
            "voucher_id": row.get("voucher_id"),
            "transaction_date": row.get("date"),
            "account_code": row.get("account_code"),
            "account_name": row.get("account_name"),
            "voucher_detail": row.get("voucher_detail"),
            "debit": row.get("debit"),
            "credit": row.get("credit"),
            "source_extract_as_of": source_extract_as_of or None,
            "source_sequence": source_extract_as_of or upload_id,
        }
        for row in normalized.journal.to_dict("records")
    ]
    result = _resolve_occurrence_input_lines_postgres(
        conn,
        client_id=client_id,
        template_id=template_id,
        input_lines=input_lines,
    )
    return result
