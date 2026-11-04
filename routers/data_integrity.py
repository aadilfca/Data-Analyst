"""Read-only, client-scoped inventory of canonical data and duplicate identities."""

import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from database import ROW_OVERHEAD_BYTES, execute_query_pg, run_read_snapshot_pg


router = APIRouter()

# Baseline taken from the canonical schemas, not from index names or scan counts.
# Lookup requirements accept a longer index with the same leading columns.
_ACTIVE_IDENTITY_PREDICATE = (
    "is_deleted = FALSE AND change_detection_status <> 'deleted' "
    "AND source_identity IS NOT NULL AND source_identity <> ''"
)
_REQUIRED_INDEXES = {
    "Client Data": (
        ("Row identification", "client_data", ("id",), "primary", None),
        ("Duplicate prevention", "client_data", ("client_id", "template_id", "source_identity"),
         "unique", _ACTIVE_IDENTITY_PREDICATE),
        ("Template identity lookup", "client_data", ("client_id", "template_id", "is_deleted", "source_identity"), "lookup", None),
        ("Stored-date lookup", "client_data", ("client_id", "is_deleted", "extract_date"), "lookup", None),
        ("Report-date lookup", "client_data", ("client_id", "is_deleted", "derived_transaction_date"), "lookup", None),
        ("Cash/Bank classification lookup", "client_data", ("client_id", "is_deleted", "derived_treasury_type", "derived_transaction_date"), "lookup", None),
    ),
    "Trial Balance": (
        ("Upload identification", "trial_balance_uploads", ("id",), "primary", None),
        ("Client upload lookup", "trial_balance_uploads", ("client_id",), "lookup", None),
        ("Account identification", "trial_balance_accounts", ("id",), "primary", None),
        ("Duplicate prevention", "trial_balance_accounts", ("upload_id", "account_code"), "unique", None),
        ("Account hierarchy lookup", "trial_balance_accounts", ("upload_id", "parent_account_code"), "lookup", None),
    ),
    "Reconciliation": (
        ("Row identification", "report_balance_inputs", ("id",), "primary", None),
        ("Duplicate prevention", "report_balance_inputs", ("client_id", "group_label", "account_code", "period_month"), "unique", None),
        ("Period lookup", "report_balance_inputs", ("client_id", "group_label", "period_month"), "lookup", None),
    ),
}


def _predicate_tokens(predicate):
    """Ignore PostgreSQL's redundant parentheses/text casts, retaining operators/literals.

    Only our fixed AND-only predicate is accepted; an OR or an extra condition
    cannot become equivalent through this formatting normalization.
    """
    tokens = re.findall(r"'(?:''|[^'])*'|::|<>|[A-Za-z_][\w]*|[^\s()]", predicate or "")
    result = []
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token == "::" and position + 1 < len(tokens) and tokens[position + 1].lower() == "text":
            position += 2
            continue
        result.append(token if token.startswith("'") else token.lower())
        position += 1
    return result


def _matches_required_index(index, requirement):
    _, table, columns, kind, predicate = requirement
    if index.get("table_name") != table or index.get("access_method") != "btree":
        return False
    if not all(index.get(flag) is True for flag in (
        "is_valid", "is_ready", "is_live", "standard_keys"
    )):
        return False
    keys = tuple(index.get("key_columns") or ())
    if (keys[:len(columns)] != columns if kind == "lookup" else keys != columns):
        return False
    if _predicate_tokens(index.get("predicate")) != _predicate_tokens(predicate):
        return False
    if kind in ("primary", "unique") and not (
        index.get("is_unique") is True and index.get("is_immediate") is True
    ):
        return False
    return kind != "primary" or index.get("is_primary") is True


def _database_safeguards(modules):
    """Inspect shared index definitions and reuse this client's record checks.

    No repair, per-row index assignment, index removal, or storage maintenance.
    A catalog failure must never be translated into a successful check.
    """
    tables = sorted({item[1] for requirements in _REQUIRED_INDEXES.values() for item in requirements})
    try:
        indexes = execute_query_pg(
            """
            SELECT t.relname AS table_name, ix.relname AS index_name,
                   am.amname AS access_method,
                   i.indisvalid AS is_valid, i.indisready AS is_ready,
                   i.indislive AS is_live, i.indisunique AS is_unique,
                   i.indisprimary AS is_primary, i.indimmediate AS is_immediate,
                   pg_get_expr(i.indpred, i.indrelid) AS predicate,
                   ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum, pos)
                         LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                         WHERE k.pos <= i.indnkeyatts ORDER BY k.pos) AS key_columns,
                   NOT EXISTS (
                       SELECT 1 FROM unnest(i.indclass, i.indcollation, i.indkey)
                           WITH ORDINALITY k(opclass, collation_oid, attnum, pos)
                       LEFT JOIN pg_opclass oc ON oc.oid = k.opclass
                       LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                       WHERE k.pos <= i.indnkeyatts
                         AND (oc.opcdefault IS DISTINCT FROM TRUE OR a.attname IS NULL
                              OR k.collation_oid IS DISTINCT FROM a.attcollation)
                   ) AS standard_keys
            FROM pg_class t
            JOIN pg_namespace n ON n.oid = t.relnamespace
            LEFT JOIN pg_index i ON i.indrelid = t.oid
            LEFT JOIN pg_class ix ON ix.oid = i.indexrelid
            LEFT JOIN pg_am am ON am.oid = ix.relam
            WHERE n.nspname = 'public' AND t.relname = ANY(%s)
            """,
            (tables,),
        )
    except Exception:
        indexes = None

    results = []
    for module in modules:
        requirements = _REQUIRED_INDEXES.get(module["module"], ())
        missing = [] if indexes is None else [
            item[0] for item in requirements
            if not any(_matches_required_index(index, item) for index in indexes)
        ]
        index_status = "Not checked" if indexes is None or not requirements else "Needs attention" if missing else "Passed"
        identity_count = module.get("unidentified_rows")
        identity_status = "Not checked" if identity_count is None else "Needs attention" if identity_count else "Passed"
        duplicate_status = (
            "Needs attention" if module["duplicate_groups"]
            else "Not checked" if module["duplicate_status"] != "Clear"
            else "Passed"
        )
        checks = [
            {"label": "Required indexes", "status": index_status,
             "detail": "Index definitions could not be verified." if index_status == "Not checked"
             else "Missing, different or unavailable: " + ", ".join(missing) if missing
             else f"{len(requirements)} required structures are valid and ready."},
            {"label": "Record identification", "status": identity_status,
             "detail": "Record identification could not be checked." if identity_count is None
             else f"{identity_count:,} rows need identifying details." if identity_count
             else "No records to check." if not module["row_count"]
             else f"All {module['row_count']:,} records have the required identifying details."},
            {"label": "Duplicate check", "status": duplicate_status,
             "detail": f"{module['duplicate_groups']:,} duplicate groups found." if module["duplicate_groups"]
             else "Some records could not be fully compared." if duplicate_status == "Not checked"
             else "No duplicate groups found within this module's check scope."},
        ]
        statuses = [check["status"] for check in checks]
        status = "Needs attention" if "Needs attention" in statuses else "Not checked" if "Not checked" in statuses else "Passed"
        results.append({"module": module["module"], "row_count": module["row_count"],
                        "status": status, "checks": checks, "incomplete": "Not checked" in statuses})
    statuses = [item["status"] for item in results]
    return {
        "status": "Needs attention" if "Needs attention" in statuses else "Not checked" if not statuses or "Not checked" in statuses else "Passed",
        "incomplete": any(item["incomplete"] for item in results),
        "modules": results,
        "scope_note": "Index structures are shared across clients. Record identification and duplicate checks cover only the selected client and the three listed modules.",
        "limit_note": "This checks the required index baseline and identifying details. It does not inspect every physical index entry, verify all stored values, or measure query speed or reclaimable storage.",
    }


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
               COUNT(*) FILTER (WHERE cd.template_id IS NULL OR cd.source_identity IS NULL
                                  OR BTRIM(cd.source_identity) = '') AS unidentified_rows
        FROM client_data cd
        WHERE cd.client_id = %s
          AND cd.is_deleted = FALSE
          AND cd.change_detection_status <> 'deleted'
        """,
        (ROW_OVERHEAD_BYTES, client_id),
    )
    data = rows[0]
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
    duplicate = duplicate_rows[0]
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
               COALESCE(SUM(pg_column_size(a)), 0) AS data_bytes,
               COUNT(a.id) FILTER (WHERE a.upload_id IS NULL OR a.account_code IS NULL
                                      OR BTRIM(a.account_code) = '') AS unidentified_rows
        FROM trial_balance_uploads u
        LEFT JOIN trial_balance_accounts a ON a.upload_id = u.id
        WHERE u.client_id = %s
        """,
        (client_id,),
    )
    data = account_rows[0]
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
    duplicate = duplicate_rows[0]
    return _module_row(
        "Trial Balance",
        int(data.get("row_count") or 0),
        len(uploads),
        int(data.get("data_bytes") or 0),
        sum(int(row.get("row_bytes") or 0) for row in uploads),
        int(duplicate.get("duplicate_groups") or 0),
        int(duplicate.get("duplicate_extra_rows") or 0),
        "Account codes are compared only within the same Trial Balance upload.",
        unidentified_rows=int(data["unidentified_rows"]),
    )


def _reconciliation_module(client_id: int) -> dict:
    rows = execute_query_pg(
        """
        SELECT COUNT(*) AS row_count,
               COALESCE(SUM(pg_column_size(r)), 0) AS data_bytes,
               COUNT(*) FILTER (WHERE group_label IS NULL OR BTRIM(group_label) = ''
                                  OR account_code IS NULL OR BTRIM(account_code) = ''
                                  OR period_month IS NULL OR BTRIM(period_month) = '') AS unidentified_rows
        FROM report_balance_inputs r
        WHERE r.client_id = %s
        """,
        (client_id,),
    )
    data = rows[0]
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
    duplicate = duplicate_rows[0]
    return _module_row(
        "Reconciliation",
        int(data.get("row_count") or 0),
        0,
        int(data.get("data_bytes") or 0),
        0,
        int(duplicate.get("duplicate_groups") or 0),
        int(duplicate.get("duplicate_extra_rows") or 0),
        "Balance inputs are compared by group, account code, and month.",
        unidentified_rows=int(data["unidentified_rows"]),
    )


def _get_data_integrity_overview(client_id: int):
    """Build inventory and safeguards from one consistent database snapshot."""
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
        "safeguards": _database_safeguards(modules),
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


@router.get("/api/data-integrity/overview")
def get_data_integrity_overview(client_id: int = Query(..., ge=1)):
    """Read-only refresh; no repairs or stored check history are created."""
    return run_read_snapshot_pg(lambda _conn: _get_data_integrity_overview(client_id))
