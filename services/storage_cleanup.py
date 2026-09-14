"""Safe, administrator-controlled PostgreSQL storage cleanup.

The preview and apply rules are kept together so the destructive operation cannot
silently broaden the protected-data boundaries shown to the administrator.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from typing import Any, Dict, List

from database import execute_query_pg, execute_write_pg, pg_transaction


CONFIRMATION_TEXT = "DELETE APPROVED STORAGE DATA"
CLEANUP_SCOPE = (
    "approved_logs,retired_client_data,deleted_upload_metadata,"
    "stale_presence,expired_sessions"
)
REMOVABLE_LOG_TABLES = (
    "data_audit_log",
    "audit_log",
    "error_log",
    "audit_rules_config_changes",
)
PROTECTED_DATA_TABLES = (
    "client_data",
    "verification_details",
    "trial_balance_accounts",
    "trial_balance_uploads",
)
SIZING_TABLES = REMOVABLE_LOG_TABLES + PROTECTED_DATA_TABLES + (
    "uploaded_files",
    "verification_sessions",
    "verification_presence",
    "sessions",
)


class CleanupConfirmationError(ValueError):
    """The operator did not supply the exact destructive-action phrase."""


class CleanupPreviewStaleError(ValueError):
    """The administrator is applying a preview that no longer matches the DB."""


def _first(query: str, params=None) -> Dict[str, Any]:
    rows = execute_query_pg(query, params)
    return rows[0] if rows else {}


def _count(query: str, params=None) -> int:
    return int(_first(query, params).get("count") or 0)


def _format_bytes(value: int) -> str:
    amount = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return "0 B"


def _table_sizes() -> List[Dict[str, Any]]:
    rows = execute_query_pg(
        """
        SELECT c.relname AS table_name,
               pg_relation_size(c.oid) AS table_bytes,
               pg_indexes_size(c.oid) AS index_bytes,
               pg_total_relation_size(c.oid) AS total_bytes
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
          AND n.nspname = 'public'
          AND c.relname = ANY(%s)
        ORDER BY pg_total_relation_size(c.oid) DESC, c.relname
        """,
        (list(SIZING_TABLES),),
    )
    removable = set(REMOVABLE_LOG_TABLES)
    protected = set(PROTECTED_DATA_TABLES)
    result = []
    for row in rows:
        total = int(row.get("total_bytes") or 0)
        table_name = row["table_name"]
        result.append(
            {
                "table_name": table_name,
                "classification": (
                    "removable log" if table_name in removable
                    else "protected data" if table_name in protected
                    else "operational metadata"
                ),
                "table_bytes": int(row.get("table_bytes") or 0),
                "index_bytes": int(row.get("index_bytes") or 0),
                "total_bytes": total,
                "formatted": _format_bytes(total),
            }
        )
    return result


def _protected_counts() -> Dict[str, int]:
    return {
        "client_data_total": _count("SELECT COUNT(*) AS count FROM public.client_data"),
        "client_data_current": _count(
            "SELECT COUNT(*) AS count FROM public.client_data "
            "WHERE is_deleted = FALSE AND change_detection_status != 'deleted'"
        ),
        "client_data_retained_for_verification": _count(
            "SELECT COUNT(*) AS count FROM public.client_data cd "
            "WHERE EXISTS (SELECT 1 FROM public.verification_details vd "
            "WHERE vd.client_data_id = cd.id)"
        ),
        "verification_details": _count(
            "SELECT COUNT(*) AS count FROM public.verification_details"
        ),
        "trial_balance_accounts": _count(
            "SELECT COUNT(*) AS count FROM public.trial_balance_accounts"
        ),
        "trial_balance_uploads": _count(
            "SELECT COUNT(*) AS count FROM public.trial_balance_uploads"
        ),
    }


def _preview_fingerprint_payload(preview: Dict[str, Any]) -> Dict[str, Any]:
    """Select all candidate/protection aggregates that must match before apply."""
    return {
        "database": preview["database"],
        "table_sizes": preview["table_sizes"],
        "protected": preview["protected"],
        "removable": preview["removable"],
    }


def _preview_hash(preview: Dict[str, Any]) -> str:
    payload = json.dumps(
        _preview_fingerprint_payload(preview),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def get_storage_cleanup_preview() -> Dict[str, Any]:
    """Return aggregate storage usage and candidates; never writes to PostgreSQL."""
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("PostgreSQL is not configured")

    database_bytes = int(
        _first("SELECT pg_database_size(current_database()) AS total_bytes").get(
            "total_bytes"
        )
        or 0
    )
    sizes = _table_sizes()
    size_map = {row["table_name"]: row for row in sizes}

    logs = []
    for table_name in REMOVABLE_LOG_TABLES:
        logs.append(
            {
                "table_name": table_name,
                "row_count": _count(f"SELECT COUNT(*) AS count FROM public.{table_name}"),
                "physical_bytes": size_map.get(table_name, {}).get("total_bytes", 0),
                "formatted": size_map.get(table_name, {}).get("formatted", "0 B"),
            }
        )

    retired_candidate = _first(
        """
        SELECT COUNT(*) AS count,
               COALESCE(SUM(pg_column_size(cd.row_data)), 0) AS payload_bytes
        FROM public.client_data cd
        WHERE cd.is_deleted = TRUE
          AND NOT EXISTS (SELECT 1 FROM public.verification_details vd WHERE vd.client_data_id = cd.id)
          AND NOT EXISTS (SELECT 1 FROM public.voucher_line_documents vld WHERE vld.client_data_id = cd.id)
          AND NOT EXISTS (SELECT 1 FROM public.voucher_line_physical_bill_items vpbbi WHERE vpbbi.source_document_row_id = cd.id)
        """
    )

    preview = {
        "mode": "dry_run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "total_bytes": database_bytes,
            "formatted": _format_bytes(database_bytes),
        },
        "table_sizes": sizes,
        "protected": _protected_counts(),
        "removable": {
            "logs": logs,
            "retired_client_data": {
                "row_count": int(retired_candidate.get("count") or 0),
                "payload_bytes": int(retired_candidate.get("payload_bytes") or 0),
                "formatted": _format_bytes(int(retired_candidate.get("payload_bytes") or 0)),
                "rule": "is_deleted=true and no Verification, document-link, or physical-bill source reference",
            },
            "unreferenced_upload_metadata": _count(
                "SELECT COUNT(*) AS count FROM public.uploaded_files uf "
                "WHERE uf.status = 'Deleted' AND NOT EXISTS "
                "(SELECT 1 FROM public.client_data cd WHERE cd.upload_id = uf.id)"
            ),
            "stale_presence": _count(
                "SELECT COUNT(*) AS count FROM public.verification_presence "
                "WHERE ended_at IS NOT NULL OR last_heartbeat < CURRENT_TIMESTAMP - INTERVAL '15 minutes'"
            ),
            "expired_login_sessions": _count(
                "SELECT COUNT(*) AS count FROM public.sessions WHERE expiry < CURRENT_TIMESTAMP"
            ),
        },
        "safety": [
            "This preview performs no INSERT, UPDATE, DELETE, TRUNCATE, or VACUUM operation.",
            "Current Data Bank rows, Verification details, Trial Balance data, and linked evidence are protected.",
            "Apply requires this fresh preview hash and the exact typed confirmation phrase.",
            "VACUUM/compaction is intentionally deferred to a separate maintenance phase.",
        ],
    }
    preview["preview_hash"] = _preview_hash(preview)
    return preview


def _delete_count(query: str) -> int:
    wrapped = (
        "WITH deleted AS ("
        + query.rstrip()
        + " RETURNING 1) SELECT COUNT(*) AS count FROM deleted"
    )
    return _count(wrapped)


def apply_storage_cleanup(
    preview_hash: str,
    operator: str,
    confirmation: str,
) -> Dict[str, Any]:
    """Apply only the approved candidate rules in one Postgres transaction."""
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("PostgreSQL is not configured")
    if confirmation != CONFIRMATION_TEXT:
        raise CleanupConfirmationError(
            f"Type exactly: {CONFIRMATION_TEXT}"
        )

    fresh_preview = get_storage_cleanup_preview()
    if preview_hash != fresh_preview["preview_hash"]:
        raise CleanupPreviewStaleError(
            "The preview is stale. Refresh the preview and submit the new confirmation."
        )

    protected_before = fresh_preview["protected"]
    deleted_counts: Dict[str, int] = {}
    clean_operator = (operator or "Unknown administrator")[:200]

    with pg_transaction():
        execute_write_pg("SET LOCAL statement_timeout = '120s'")
        execute_write_pg(
            """
            INSERT INTO storage_cleanup_runs
                (operator, scope, status, deleted_counts, preview_hash)
            VALUES (%s, %s, 'started', %s, %s)
            """,
            (clean_operator, CLEANUP_SCOPE, "{}", preview_hash),
        )

        for table_name in REMOVABLE_LOG_TABLES:
            deleted_counts[table_name] = _delete_count(
                f"DELETE FROM public.{table_name}"
            )

        deleted_counts["retired_client_data"] = _delete_count(
            """
            DELETE FROM public.client_data cd
            WHERE cd.is_deleted = TRUE
              AND NOT EXISTS (SELECT 1 FROM public.verification_details vd WHERE vd.client_data_id = cd.id)
              AND NOT EXISTS (SELECT 1 FROM public.voucher_line_documents vld WHERE vld.client_data_id = cd.id)
              AND NOT EXISTS (SELECT 1 FROM public.voucher_line_physical_bill_items vpbbi WHERE vpbbi.source_document_row_id = cd.id)
            """
        )
        deleted_counts["unreferenced_deleted_upload_metadata"] = _delete_count(
            """
            DELETE FROM public.uploaded_files uf
            WHERE uf.status = 'Deleted'
              AND NOT EXISTS (SELECT 1 FROM public.client_data cd WHERE cd.upload_id = uf.id)
            """
        )
        deleted_counts["stale_presence"] = _delete_count(
            """
            DELETE FROM public.verification_presence
            WHERE ended_at IS NOT NULL
               OR last_heartbeat < CURRENT_TIMESTAMP - INTERVAL '15 minutes'
            """
        )
        deleted_counts["expired_login_sessions"] = _delete_count(
            "DELETE FROM public.sessions WHERE expiry < CURRENT_TIMESTAMP"
        )

        protected_after = _protected_counts()
        for key in (
            "client_data_current",
            "client_data_retained_for_verification",
            "verification_details",
            "trial_balance_accounts",
            "trial_balance_uploads",
        ):
            if protected_after[key] != protected_before[key]:
                raise RuntimeError(
                    f"Protected-data safety check failed for {key}; transaction rolled back"
                )

        execute_write_pg(
            """
            UPDATE storage_cleanup_runs
            SET status = 'completed', deleted_counts = %s, completed_at = CURRENT_TIMESTAMP
            WHERE preview_hash = %s AND status = 'started'
            """,
            (json.dumps(deleted_counts, sort_keys=True), preview_hash),
        )

    try:
        final_preview = get_storage_cleanup_preview()
    except Exception:
        # The transaction is already committed; do not misreport a successful
        # cleanup as rolled back if the follow-up read is temporarily unavailable.
        final_preview = None

    return {
        "mode": "applied",
        "deleted_counts": deleted_counts,
        "protected": protected_after,
        "safety": [
            "Protected Verification details, Trial Balance data, current Data Bank rows, and linked evidence were not deleted.",
            "The cleanup was committed as one transaction after protected-data checks passed.",
            "VACUUM/compaction was not run; it remains a separate maintenance phase.",
        ],
        "preview": final_preview,
    }
