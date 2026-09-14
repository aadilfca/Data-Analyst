"""Read-only storage cleanup inventory for the administrator maintenance screen.

This phase deliberately exposes no deletion operation. The protected and retired-row
rules live here so a later destructive phase can reuse the same dependency checks.
"""

from datetime import datetime, timezone
import os
from typing import Any, Dict, List

from database import execute_query_pg


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


def get_storage_cleanup_preview() -> Dict[str, Any]:
    """Return aggregate storage usage and candidates; never writes to PostgreSQL."""
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("PostgreSQL is not configured")

    database_bytes = int(_first("SELECT pg_database_size(current_database()) AS total_bytes").get("total_bytes") or 0)
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
    retired_payload_bytes = int(retired_candidate.get("payload_bytes") or 0)

    protected = {
        "client_data_total": _count("SELECT COUNT(*) AS count FROM public.client_data"),
        "client_data_current": _count(
            "SELECT COUNT(*) AS count FROM public.client_data WHERE is_deleted = FALSE AND change_detection_status != 'deleted'"
        ),
        "client_data_retained_for_verification": _count(
            "SELECT COUNT(*) AS count FROM public.client_data cd WHERE EXISTS (SELECT 1 FROM public.verification_details vd WHERE vd.client_data_id = cd.id)"
        ),
        "verification_details": _count("SELECT COUNT(*) AS count FROM public.verification_details"),
        "trial_balance_accounts": _count("SELECT COUNT(*) AS count FROM public.trial_balance_accounts"),
        "trial_balance_uploads": _count("SELECT COUNT(*) AS count FROM public.trial_balance_uploads"),
    }

    removable = {
        "logs": logs,
        "retired_client_data": {
            "row_count": int(retired_candidate.get("count") or 0),
            "payload_bytes": retired_payload_bytes,
            "formatted": _format_bytes(retired_payload_bytes),
            "rule": "is_deleted=true and no Verification, document-link, or physical-bill source reference",
        },
        "unreferenced_upload_metadata": _count(
            "SELECT COUNT(*) AS count FROM public.uploaded_files uf WHERE NOT EXISTS (SELECT 1 FROM public.client_data cd WHERE cd.upload_id = uf.id)"
        ),
        "stale_presence": _count(
            "SELECT COUNT(*) AS count FROM public.verification_presence WHERE ended_at IS NOT NULL OR last_heartbeat < CURRENT_TIMESTAMP - INTERVAL '15 minutes'"
        ),
        "expired_login_sessions": _count("SELECT COUNT(*) AS count FROM public.sessions WHERE expiry < CURRENT_TIMESTAMP"),
    }

    return {
        "mode": "dry_run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": {"total_bytes": database_bytes, "formatted": _format_bytes(database_bytes)},
        "table_sizes": sizes,
        "protected": protected,
        "removable": removable,
        "safety": [
            "This preview performs no INSERT, UPDATE, DELETE, TRUNCATE, or VACUUM operation.",
            "Current Data Bank rows, Verification details, Trial Balance data, and linked evidence are protected.",
            "A later deletion phase must obtain a fresh preview before applying any change.",
        ],
    }
