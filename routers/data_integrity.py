"""Read-only, client-scoped inventory of canonical data and duplicate identities."""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from database import ROW_OVERHEAD_BYTES, execute_query_pg


router = APIRouter()


def _byte_label(value: int) -> str:
    size = float(max(0, value or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return "0 B"


def _upload_staging_root() -> str:
    configured = os.environ.get("UPLOAD_STAGING_DIR")
    app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.realpath(configured or os.path.join(app_root, "uploads"))


def _measure_source_files(upload_rows: list[dict]) -> tuple[int, int]:
    """Measure staged workbooks only when their paths remain inside upload storage."""
    root = _upload_staging_root()
    total_bytes = 0
    unmeasured = 0
    for row in upload_rows:
        path = row.get("file_path")
        if not path:
            continue
        resolved = os.path.realpath(path)
        try:
            inside_root = os.path.normcase(os.path.commonpath((root, resolved))) == os.path.normcase(root)
        except ValueError:
            inside_root = False
        if not inside_root or not os.path.isfile(resolved):
            unmeasured += 1
            continue
        try:
            total_bytes += os.path.getsize(resolved)
        except OSError:
            unmeasured += 1
    return total_bytes, unmeasured


def _module_row(
    name: str,
    row_count: int,
    associated_count: int,
    data_bytes: int,
    associated_bytes: int,
    duplicate_groups: int,
    duplicate_extra_rows: int,
    duplicate_basis: str,
    *,
    unidentified_rows: int = 0,
    unmeasured_source_files: int = 0,
) -> dict:
    total_bytes = data_bytes + associated_bytes
    if duplicate_groups:
        duplicate_status = "Review"
    elif unidentified_rows:
        duplicate_status = "Not fully checked"
    else:
        duplicate_status = "Clear"
    return {
        "module": name,
        "row_count": row_count,
        "associated_record_count": associated_count,
        "data_bytes": data_bytes,
        "data_size": _byte_label(data_bytes),
        "associated_data_bytes": associated_bytes,
        "associated_data_size": _byte_label(associated_bytes),
        "total_data_bytes": total_bytes,
        "total_data_size": _byte_label(total_bytes),
        "size_status": "Incomplete" if unmeasured_source_files else "Estimated",
        "duplicate_groups": duplicate_groups,
        "duplicate_extra_rows": duplicate_extra_rows,
        "duplicate_status": duplicate_status,
        "duplicate_basis": duplicate_basis,
        "unidentified_rows": unidentified_rows,
        "unmeasured_source_files": unmeasured_source_files,
    }


def _client_data_module(client_id: int) -> dict:
    rows = execute_query_pg(
        """
        SELECT COUNT(*) AS row_count,
               COALESCE(SUM(LENGTH(cd.row_data::text)), 0) + COUNT(*) * %s AS data_bytes,
               COUNT(*) FILTER (WHERE cd.source_identity IS NULL) AS unidentified_rows
        FROM client_data cd
        WHERE cd.client_id = %s
          AND cd.is_deleted = FALSE
          AND cd.change_detection_status <> 'deleted'
        """,
        (ROW_OVERHEAD_BYTES, client_id),
    )
    data = rows[0] if rows else {}
    uploads = execute_query_pg(
        """
        SELECT u.id, u.file_path, COALESCE(pg_column_size(u), 0) AS row_bytes
        FROM uploaded_files u
        WHERE u.client_id = %s OR EXISTS (
            SELECT 1 FROM client_data cd
            WHERE cd.upload_id = u.id AND cd.client_id = %s
        )
        ORDER BY u.id
        """,
        (client_id, client_id),
    )
    source_bytes, unmeasured_files = _measure_source_files(uploads)
    associated_bytes = sum(int(row.get("row_bytes") or 0) for row in uploads) + source_bytes
    duplicate_rows = execute_query_pg(
        """
        SELECT COUNT(*) AS duplicate_groups,
               COALESCE(SUM(group_size - 1), 0) AS duplicate_extra_rows
        FROM (
            SELECT template_id, source_identity, COUNT(*) AS group_size
            FROM client_data
            WHERE client_id = %s
              AND is_deleted = FALSE
              AND change_detection_status <> 'deleted'
              AND source_identity IS NOT NULL
            GROUP BY template_id, source_identity
            HAVING COUNT(*) > 1
        ) duplicates
        """,
        (client_id,),
    )
    duplicate = duplicate_rows[0] if duplicate_rows else {}
    return _module_row(
        "Client Data",
        int(data.get("row_count") or 0),
        len(uploads),
        int(data.get("data_bytes") or 0),
        associated_bytes,
        int(duplicate.get("duplicate_groups") or 0),
        int(duplicate.get("duplicate_extra_rows") or 0),
        "Active rows, grouped by template and saved source identity.",
        unidentified_rows=int(data.get("unidentified_rows") or 0),
        unmeasured_source_files=unmeasured_files,
    )


def _trial_balance_module(client_id: int) -> dict:
    account_rows = execute_query_pg(
        """
        SELECT COUNT(a.id) AS row_count,
               COALESCE(SUM(pg_column_size(a)), 0) AS data_bytes
        FROM trial_balance_uploads u
        LEFT JOIN trial_balance_accounts a ON a.upload_id = u.id
        WHERE u.client_id = %s
        """,
        (client_id,),
    )
    data = account_rows[0] if account_rows else {}
    uploads = execute_query_pg(
        """
        SELECT u.id, COALESCE(pg_column_size(u), 0) AS row_bytes
        FROM trial_balance_uploads u
        WHERE u.client_id = %s
        ORDER BY u.id
        """,
        (client_id,),
    )
    duplicate_rows = execute_query_pg(
        """
        SELECT COUNT(*) AS duplicate_groups,
               COALESCE(SUM(group_size - 1), 0) AS duplicate_extra_rows
        FROM (
            SELECT a.upload_id, a.account_code, COUNT(*) AS group_size
            FROM trial_balance_accounts a
            JOIN trial_balance_uploads u ON u.id = a.upload_id
            WHERE u.client_id = %s
            GROUP BY a.upload_id, a.account_code
            HAVING COUNT(*) > 1
        ) duplicates
        """,
        (client_id,),
    )
    duplicate = duplicate_rows[0] if duplicate_rows else {}
    return _module_row(
        "Trial Balance",
        int(data.get("row_count") or 0),
        len(uploads),
        int(data.get("data_bytes") or 0),
        sum(int(row.get("row_bytes") or 0) for row in uploads),
        int(duplicate.get("duplicate_groups") or 0),
        int(duplicate.get("duplicate_extra_rows") or 0),
        "Account codes are compared only within the same Trial Balance upload.",
    )


def _reconciliation_module(client_id: int) -> dict:
    rows = execute_query_pg(
        """
        SELECT COUNT(*) AS row_count,
               COALESCE(SUM(pg_column_size(r)), 0) AS data_bytes
        FROM report_balance_inputs r
        WHERE r.client_id = %s
        """,
        (client_id,),
    )
    data = rows[0] if rows else {}
    duplicate_rows = execute_query_pg(
        """
        SELECT COUNT(*) AS duplicate_groups,
               COALESCE(SUM(group_size - 1), 0) AS duplicate_extra_rows
        FROM (
            SELECT LOWER(BTRIM(group_label)) AS group_key,
                   BTRIM(account_code) AS account_key,
                   period_month,
                   COUNT(*) AS group_size
            FROM report_balance_inputs
            WHERE client_id = %s
            GROUP BY LOWER(BTRIM(group_label)), BTRIM(account_code), period_month
            HAVING COUNT(*) > 1
        ) duplicates
        """,
        (client_id,),
    )
    duplicate = duplicate_rows[0] if duplicate_rows else {}
    return _module_row(
        "Reconciliation",
        int(data.get("row_count") or 0),
        0,
        int(data.get("data_bytes") or 0),
        0,
        int(duplicate.get("duplicate_groups") or 0),
        int(duplicate.get("duplicate_extra_rows") or 0),
        "Balance inputs are compared by group, account code, and month.",
    )


@router.get("/api/data-integrity/overview")
def get_data_integrity_overview(client_id: int = Query(..., ge=1)):
    """Return a read-only inventory and record-type-scoped duplicate scan."""
    clients = execute_query_pg(
        "SELECT id, client_name FROM clients WHERE id = %s AND is_active = TRUE",
        (client_id,),
    )
    if not clients:
        raise HTTPException(404, "Client not found or inactive")

    modules = [
        _client_data_module(client_id),
        _trial_balance_module(client_id),
        _reconciliation_module(client_id),
    ]
    database_rows = execute_query_pg(
        """
        SELECT pg_database_size(current_database()) AS database_bytes,
               COALESCE((SELECT SUM(pg_relation_size(indexrelid))
                         FROM pg_stat_user_indexes), 0) AS shared_index_bytes
        """
    )
    database = database_rows[0] if database_rows else {}
    duplicate_statuses = [row["duplicate_status"] for row in modules]
    has_review = "Review" in duplicate_statuses
    has_incomplete_scan = "Not fully checked" in duplicate_statuses
    overall_status = (
        "Review; scan incomplete" if has_review and has_incomplete_scan
        else "Review" if has_review
        else "Not fully checked" if has_incomplete_scan
        else "Clear"
    )
    return {
        "client_id": client_id,
        "client_name": clients[0]["client_name"],
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "overall_status": overall_status,
        "modules": modules,
        "database_context": {
            "database_bytes": int(database.get("database_bytes") or 0),
            "database_size": _byte_label(int(database.get("database_bytes") or 0)),
            "shared_index_bytes": int(database.get("shared_index_bytes") or 0),
            "shared_index_size": _byte_label(int(database.get("shared_index_bytes") or 0)),
        },
        "size_note": (
            "Module sizes are estimates from stored rows and related upload data. "
            "Shared indexes are shown separately and are not assigned to one client."
        ),
        "scope_note": (
            "Duplicate checks are limited to active records in this client and use each module's "
            "record identity. Matching IDs across modules are not counted as duplicates."
        ),
    }
