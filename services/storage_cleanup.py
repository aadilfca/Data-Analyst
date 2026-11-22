"""Safe, administrator-controlled PostgreSQL storage cleanup.

The preview and apply rules are kept together so the destructive operation cannot
silently broaden the protected-data boundaries shown to the administrator.
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse
from psycopg2 import sql
from services.upload_lifecycle import unbacked_receipt_matches

from database import (
    POSTGRES_INDEX_MAINTENANCE_ALLOWLIST,
    compact_postgres_tables,
    drop_postgres_index_concurrently,
    execute_query_pg,
    execute_write_pg,
    pg_transaction,
    reindex_postgres_index_concurrently,
    rebuild_empty_classification_indexes,
    run_read_snapshot_pg,
)


logger = logging.getLogger(__name__)


CONFIRMATION_TEXT = "DELETE APPROVED STORAGE DATA"
VALIDATION_PLAN_CLEANUP_CONFIRMATION = "DELETE APPROVED VALIDATION PLANS"
COMPACTION_CONFIRMATION_TEXT = "COMPACT APPROVED STORAGE DATA"
FULL_COMPACTION_CONFIRMATION_TEXT = "FULL COMPACTION APPROVED"
INDEX_MAINTENANCE_CONFIRMATION_TEXT = "REMOVE APPROVED INDEX"
TEMPLATE_FINALIZATION_CONFIRMATION = "FINALIZE REVIEWED TEMPLATE CLEANUP"
INDEX_BLOAT_REINDEX_THRESHOLDS = {
    "avg_leaf_density_below": 70.0,
    "leaf_fragmentation_above": 30.0,
}
CLEANUP_SCOPE = (
    "approved_logs,retired_client_data,deleted_upload_metadata,"
    "stale_presence,expired_sessions,obsolete_cashflow_copies,prepared_revisions"
)
REMOVABLE_LOG_TABLES = (
    "data_audit_log",
    "audit_log",
    "error_log",
    "audit_rules_config_changes",
)
TEMPORARY_PLAN_TABLES = ("upload_validation_plans",)
REQUIRED_CLIENT_DATA_INDEXES = (
    "client_data_pkey",
    "idx_client_data_active_identity_unique",
)
PROTECTED_DATA_TABLES = (
    "client_data",
    "client_data_history",
    "verification_details",
    "trial_balance_accounts",
    "trial_balance_uploads",
)
CASHFLOW_DERIVED_TABLES = (
    "cash_bank_transaction_classifications",
    "cash_bank_summary_read_models",
    "cashflow_ledger_rollup_read_models",
    "cashflow_prepared_lines",
    "cashflow_prepared_rollups",
)
SIZING_TABLES = REMOVABLE_LOG_TABLES + PROTECTED_DATA_TABLES + (
    "uploaded_files",
    "verification_sessions",
    "verification_presence",
    "sessions",
) + CASHFLOW_DERIVED_TABLES + (
    "cashflow_report_revisions",
    "cashflow_preparation_state",
) + TEMPORARY_PLAN_TABLES


class CleanupConfirmationError(ValueError):
    """The operator did not supply the exact destructive-action phrase."""


class CleanupPreviewStaleError(ValueError):
    """The administrator is applying a preview that no longer matches the DB."""


PERMANENT_DATA_DELETE_CONFIRMATION = "PERMANENTLY DELETE SELECTED DATA"
_VOUCHER_ID_FROM_ROW = "COALESCE(cd.row_data->>'Voucher ID', cd.row_data->>'Voucher No', cd.row_data->>'Voucher Number', cd.row_data->>'Voucher')"


class DataPurgeProtectedError(ValueError):
    """The requested data still has protected verification/evidence references."""


class DataPurgePreviewStaleError(ValueError):
    """The permanent-data-delete preview no longer matches PostgreSQL."""


def _active_supabase_project_ref() -> str | None:
    """Read the Supabase project ref from a direct or session-pooler DATABASE_URL."""
    parsed = urlparse(os.environ.get("DATABASE_URL", ""))
    host = (parsed.hostname or "").lower()
    if host.startswith("db.") and host.endswith(".supabase.co"):
        return host[3:].split(".", 1)[0]
    if host.endswith(".pooler.supabase.com"):
        user = unquote(parsed.username or "")
        if user.startswith("postgres."):
            return user.split(".", 1)[1]
    return None


def _test_verification_purge_is_enabled() -> bool:
    """Require an explicit opt-in and a project-ref match before deleting linked reviews."""
    allowed_ref = os.environ.get("TEST_DATA_RESET_SUPABASE_PROJECT_REF", "").strip()
    return (
        os.environ.get("ALLOW_TEST_VERIFICATION_DATA_PURGE", "").strip().lower() == "true"
        and bool(allowed_ref)
        and _active_supabase_project_ref() == allowed_ref
    )


def _require_test_verification_purge_enabled() -> None:
    if not _test_verification_purge_is_enabled():
        raise DataPurgeProtectedError(
            "Deleting linked Verification data is disabled. It requires an explicit test-project setting that matches the active Supabase project."
        )


class IndexMaintenanceBlockedError(ValueError):
    """The requested index has not passed the evidence and safety gates."""


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


def _possible_index_overlaps(indexes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flag same-predicate duplicate/prefix keys for human review only."""
    overlaps = []
    for position, left in enumerate(indexes):
        left_keys = tuple(left.get("key_columns") or ())
        left_predicate = left.get("predicate") or ""
        if not left_keys:
            continue
        for right in indexes[position + 1:]:
            right_keys = tuple(right.get("key_columns") or ())
            if not right_keys or left_predicate != (right.get("predicate") or ""):
                continue
            same_keys = left_keys == right_keys
            prefix = (
                left_keys[: min(len(left_keys), len(right_keys))] ==
                right_keys[: min(len(left_keys), len(right_keys))]
            )
            if same_keys or (prefix and len(left_keys) != len(right_keys)):
                overlaps.append(
                    {
                        "indexes": [left["index_name"], right["index_name"]],
                        "reason": "same key definition" if same_keys else "same leading key columns",
                        "protected_indexes": [
                            item["index_name"]
                            for item in (left, right)
                            if item.get("protected")
                        ],
                    }
                )
    return overlaps


INDEX_REVIEW_PLAN_QUERIES = (
    (
        "active_identity_lookup",
        """
        EXPLAIN (FORMAT JSON)
        SELECT id
        FROM public.client_data
        WHERE client_id = 1
          AND template_id = 1
          AND is_deleted = FALSE
          AND change_detection_status <> 'deleted'
          AND source_identity = 'phase2-review-probe'
        """,
    ),
    (
        "derived_date_lookup",
        """
        EXPLAIN (FORMAT JSON)
        SELECT id
        FROM public.client_data
        WHERE client_id = 1
          AND is_deleted = FALSE
          AND derived_transaction_date >= DATE '2000-01-01'
          AND derived_transaction_date < DATE '2000-01-02'
        ORDER BY derived_transaction_date
        """,
    ),
    (
        "client_template_deleted_lookup",
        """
        EXPLAIN (FORMAT JSON)
        SELECT id
        FROM public.client_data
        WHERE client_id = 1
          AND template_id = 1
          AND is_deleted = FALSE
        """,
    ),
)


def _plan_index_names(plan_node: Any) -> List[str]:
    """Collect index names from a PostgreSQL JSON plan tree."""
    names = []
    if isinstance(plan_node, dict):
        index_name = plan_node.get("Index Name")
        if index_name:
            names.append(str(index_name))
        for value in plan_node.values():
            names.extend(_plan_index_names(value))
    elif isinstance(plan_node, list):
        for value in plan_node:
            names.extend(_plan_index_names(value))
    return sorted(set(names))


def _client_data_query_plan_evidence() -> Dict[str, Any]:
    """Capture planner-only evidence; EXPLAIN does not execute the queries."""
    plans = []
    for query_name, query in INDEX_REVIEW_PLAN_QUERIES:
        try:
            rows = execute_query_pg(query)
            raw_plan = rows[0].get("QUERY PLAN") if rows else None
            if raw_plan is None and rows:
                raw_plan = rows[0].get("query plan")
            plan_tree = raw_plan[0] if isinstance(raw_plan, list) and raw_plan else raw_plan
            plans.append(
                {
                    "query_name": query_name,
                    "available": bool(plan_tree),
                    "index_names": _plan_index_names(plan_tree),
                }
            )
        except Exception as exc:
            logger.exception(
                "storage_cleanup_planner_evidence_failed query_name=%s error_type=%s",
                query_name,
                type(exc).__name__,
            )
            return {
                "available": False,
                "reason": f"Planner evidence unavailable for {query_name}; maintenance actions remain blocked.",
                "plans": plans,
            }

    return {
        "available": len(plans) == len(INDEX_REVIEW_PLAN_QUERIES),
        "probe": "Planner-only fixed-shape probes; no query execution or row reads.",
        "plans": plans,
        "index_names": sorted({name for plan in plans for name in plan["index_names"]}),
    }


def _client_data_index_bloat_evidence() -> Dict[str, Any]:
    """Capture optional pgstattuple evidence without making maintenance decisions."""
    evidence = {}
    for index_name in POSTGRES_INDEX_MAINTENANCE_ALLOWLIST:
        try:
            rows = execute_query_pg(
                "SELECT index_size, avg_leaf_density, leaf_fragmentation "
                "FROM pgstatindex(%s)",
                (f'public."{index_name}"',),
            )
            row = rows[0] if rows else {}
            density = row.get("avg_leaf_density")
            fragmentation = row.get("leaf_fragmentation")
            density = float(density) if density is not None else None
            fragmentation = float(fragmentation) if fragmentation is not None else None
            evidence[index_name] = {
                "available": density is not None and fragmentation is not None,
                "index_size": int(row.get("index_size") or 0),
                "avg_leaf_density": density,
                "leaf_fragmentation": fragmentation,
                "recommended_action": (
                    "reindex"
                    if density is not None
                    and fragmentation is not None
                    and (
                        density < INDEX_BLOAT_REINDEX_THRESHOLDS["avg_leaf_density_below"]
                        or fragmentation > INDEX_BLOAT_REINDEX_THRESHOLDS["leaf_fragmentation_above"]
                    )
                    else None
                ),
            }
        except Exception as exc:
            logger.exception(
                "storage_cleanup_bloat_evidence_failed index_name=%s error_type=%s",
                index_name,
                type(exc).__name__,
            )
            return {
                "available": False,
                "extension": "pgstattuple",
                "reason": "Bloat evidence unavailable; reindexing remains blocked.",
                "indexes": evidence,
                "thresholds": INDEX_BLOAT_REINDEX_THRESHOLDS,
            }

    return {
        "available": len(evidence) == len(POSTGRES_INDEX_MAINTENANCE_ALLOWLIST),
        "extension": "pgstattuple",
        "thresholds": INDEX_BLOAT_REINDEX_THRESHOLDS,
        "indexes": evidence,
        "probe": "Read-only pgstatindex measurements; no index rebuild was performed.",
    }


def _index_maintenance_candidates(
    indexes: List[Dict[str, Any]],
    plan_evidence: Dict[str, Any],
    bloat_evidence: Dict[str, Any] | None = None,
) -> Dict[str, Dict[str, Any]]:
    """Return evidence-gated candidates; never infer permission from size alone."""
    bloat_evidence = bloat_evidence or {"available": False, "indexes": {}}
    by_name = {row["index_name"]: row for row in indexes}
    planned_indexes = set(plan_evidence.get("index_names") or [])
    measured_bloat = bloat_evidence.get("indexes") or {}
    candidates = {}
    for index_name in POSTGRES_INDEX_MAINTENANCE_ALLOWLIST:
        row = by_name.get(index_name)
        candidate = {
            "index_name": index_name,
            "table_name": POSTGRES_INDEX_MAINTENANCE_ALLOWLIST[index_name],
            "eligible_for_manual_approval": False,
            "reindex_eligible_for_manual_approval": False,
            "optimization_actions": [],
            "reason": "index is not present",
        }
        if row:
            candidate["index_definition"] = row.get("index_definition")
            candidate["index_bytes"] = row.get("index_bytes", 0)
            candidate["idx_scan"] = row.get("idx_scan", 0)
            if row.get("protected"):
                candidate["reason"] = "protected index"
            elif row.get("idx_scan", 0) != 0:
                candidate["reason"] = "index has observed scans"
            elif not plan_evidence.get("available"):
                candidate["reason"] = "query-plan evidence is unavailable"
            elif index_name in planned_indexes:
                candidate["reason"] = "a representative query plan uses this index"
            else:
                candidate["eligible_for_manual_approval"] = True
                candidate["optimization_actions"].append("drop")
                candidate["reason"] = "zero scans and no representative plan use"

            measured = measured_bloat.get(index_name) or {}
            if (
                not row.get("protected")
                and bloat_evidence.get("available")
                and measured.get("recommended_action") == "reindex"
            ):
                candidate["reindex_eligible_for_manual_approval"] = True
                candidate["optimization_actions"].append("reindex")
                candidate["bloat_evidence"] = measured
        candidates[index_name] = candidate
    return candidates


def _table_sizes() -> List[Dict[str, Any]]:
    rows = execute_query_pg(
        """
        SELECT c.relname AS table_name,
               pg_relation_size(c.oid) AS table_bytes,
               pg_indexes_size(c.oid) AS index_bytes,
               pg_total_relation_size(c.oid) AS total_bytes,
               stats.n_live_tup, stats.n_dead_tup,
               stats.last_vacuum, stats.last_autovacuum,
               stats.last_analyze, stats.last_autoanalyze,
               c.reloptions AS maintenance_options
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables stats ON stats.relid = c.oid
        WHERE c.relkind IN ('r', 'm')
          AND n.nspname = 'public'
        ORDER BY pg_total_relation_size(c.oid) DESC, c.relname
        """,
    )
    removable = set(REMOVABLE_LOG_TABLES)
    temporary = set(TEMPORARY_PLAN_TABLES)
    protected = set(PROTECTED_DATA_TABLES)
    derived = set(CASHFLOW_DERIVED_TABLES)
    result = []
    for row in rows:
        total = int(row.get("total_bytes") or 0)
        table_name = row["table_name"]
        result.append(
            {
                "table_name": table_name,
                "classification": (
                    "removable log" if table_name in removable
                    else "temporary validation data" if table_name in temporary
                    else "protected data" if table_name in protected
                    else "derived current data" if table_name in derived
                    else "operational metadata" if table_name in SIZING_TABLES
                    else "other module / configuration"
                ),
                "table_bytes": int(row.get("table_bytes") or 0),
                "index_bytes": int(row.get("index_bytes") or 0),
                "total_bytes": total,
                "formatted": _format_bytes(total),
                "other_relation_bytes": max(0, total - int(row.get("table_bytes") or 0) - int(row.get("index_bytes") or 0)),
                "estimated_live_rows": row.get("n_live_tup"),
                "estimated_dead_rows": row.get("n_dead_tup"),
                "last_vacuum": row.get("last_vacuum"),
                "last_autovacuum": row.get("last_autovacuum"),
                "last_analyze": row.get("last_analyze"),
                "last_autoanalyze": row.get("last_autoanalyze"),
                "maintenance_options": row.get("maintenance_options") or [],
            }
        )
    return result


def _storage_diagnostics(database_bytes: int, sizes: List[Dict[str, Any]], removable: Dict[str, Any]) -> Dict[str, Any]:
    """Explain allocation without equating statistics or empty tables with reclaimable bytes."""
    maintenance = {"available": False}
    audit = {"available": False}
    try:
        settings = execute_query_pg(
            "SELECT name, setting, unit FROM pg_settings WHERE name = ANY(%s) ORDER BY name",
            (["autovacuum", "autovacuum_naptime", "autovacuum_vacuum_threshold",
              "autovacuum_vacuum_scale_factor", "autovacuum_vacuum_insert_threshold",
              "autovacuum_vacuum_insert_scale_factor"],),
        )
        blockers = _first("""SELECT COUNT(*) AS long_transaction_count,
                   COALESCE(MAX(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - xact_start))), 0) AS oldest_seconds
            FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()
              AND backend_xmin IS NOT NULL AND xact_start < CURRENT_TIMESTAMP - INTERVAL '5 minutes'""")
        maintenance = {"available": True, "settings": settings,
                       "long_snapshot_transactions": int(blockers.get("long_transaction_count") or 0),
                       "oldest_snapshot_seconds": float(blockers.get("oldest_seconds") or 0),
                       "visibility": "Transactions visible to this database role; no query text is collected."}
    except Exception:
        maintenance = {"available": False, "reason": "Maintenance statistics unavailable; reclamation decisions need further evidence."}
    try:
        rows = execute_query_pg("""SELECT action, COUNT(*) AS row_count,
            COALESCE(SUM(COALESCE(pg_column_size(old_row_data), 0)
                         + COALESCE(pg_column_size(new_row_data), 0)), 0) AS payload_bytes
            FROM public.data_audit_log GROUP BY action ORDER BY action""")
        audit = {"available": True, "actions": rows,
                 "policy": "These counts do not authorize audit deletion; review INSERT metadata separately from required change history."}
    except Exception:
        audit = {"available": False, "reason": "Audit action breakdown unavailable."}

    by_name = {row["table_name"]: row for row in sizes}
    legacy = removable.get("cashflow_legacy_transaction_copies", {}).get("tables", {})
    review = []
    for name, candidate in legacy.items():
        size = by_name.get(name, {})
        if candidate.get("row_count") == 0 and int(size.get("index_bytes") or 0) > 0:
            review.append({"table_name": name, "exact_row_count": 0,
                           "allocated_index_bytes": size["index_bytes"],
                           "reclaimable_bytes": None, "live_action_enabled": False,
                           "recommendation": "Empty tables still need index structures for future uploads. Allocated bytes are not promised savings; any rebuild needs separate measurement and approval."})
    inventoried = sum(int(row.get("total_bytes") or 0) for row in sizes)
    return {
        "measurement": "allocated_database_files", "byte_unit": "bytes",
        "allocated_bytes": database_bytes, "public_relation_bytes": inventoried,
        "other_database_bytes": max(0, database_bytes - inventoried),
        "explanation": "DELETE removes records. Space inside table and index files may remain allocated for reuse. Allocated size alone cannot prove duplication or quantify waste.",
        "statistics_note": "Live/dead row counts are PostgreSQL estimates. Dead-row estimates are not a measurement of reclaimable bytes. Other database allocation includes catalogs and objects outside this public-table inventory.",
        "maintenance": maintenance, "audit_breakdown": audit, "empty_table_reviews": review,
    }


def _client_data_index_review(
    include_query_plans: bool = False,
    include_bloat_evidence: bool = False,
) -> Dict[str, Any]:
    """Report index cost and usage without making index decisions automatically."""
    rows = execute_query_pg(
        """
        SELECT indexrelname AS index_name,
               pg_relation_size(stats.indexrelid) AS index_bytes,
               COALESCE(idx_scan, 0) AS idx_scan,
               COALESCE(idx_tup_read, 0) AS idx_tup_read,
               COALESCE(idx_tup_fetch, 0) AS idx_tup_fetch,
               ix.indisunique AS is_unique,
               ix.indisprimary AS is_primary,
               pg_get_expr(ix.indpred, ix.indrelid) AS predicate,
               pg_get_indexdef(stats.indexrelid) AS index_definition,
               ARRAY(
                   SELECT COALESCE(att.attname, '<expression>')
                   FROM unnest(ix.indkey) WITH ORDINALITY AS keys(attnum, ord)
                   LEFT JOIN pg_attribute att
                     ON att.attrelid = ix.indrelid
                    AND att.attnum = keys.attnum
                   ORDER BY keys.ord
               ) AS key_columns
        FROM pg_stat_user_indexes stats
        JOIN pg_index ix ON ix.indexrelid = stats.indexrelid
        WHERE stats.schemaname = 'public' AND stats.relname = 'client_data'
        ORDER BY pg_relation_size(stats.indexrelid) DESC, stats.indexrelname
        """
    )
    indexes = [
        {
            "index_name": row["index_name"],
            "index_bytes": int(row.get("index_bytes") or 0),
            "idx_scan": int(row.get("idx_scan") or 0),
            "idx_tup_read": int(row.get("idx_tup_read") or 0),
            "idx_tup_fetch": int(row.get("idx_tup_fetch") or 0),
            "is_unique": bool(row.get("is_unique")),
            "is_primary": bool(row.get("is_primary")),
            "protected": bool(
                row.get("is_primary")
                or row.get("is_unique")
                or row["index_name"] in REQUIRED_CLIENT_DATA_INDEXES
            ),
            "key_columns": [str(value) for value in (row.get("key_columns") or [])],
            "predicate": row.get("predicate"),
            "index_definition": row.get("index_definition"),
        }
        for row in rows
    ]
    names = {row["index_name"] for row in indexes}
    identity_duplicate_groups = _count(
        """
        SELECT COUNT(*) AS count
        FROM (
            SELECT client_id, template_id, source_identity
            FROM public.client_data
            WHERE is_deleted = FALSE
              AND change_detection_status <> 'deleted'
              AND source_identity IS NOT NULL
              AND source_identity <> ''
            GROUP BY client_id, template_id, source_identity
            HAVING COUNT(*) > 1
        ) duplicate_groups
        """
    )
    plan_evidence = (
        _client_data_query_plan_evidence()
        if include_query_plans
        else {"available": False, "reason": "query-plan capture not requested", "plans": []}
    )
    bloat_evidence = (
        _client_data_index_bloat_evidence()
        if include_bloat_evidence
        else {"available": False, "reason": "bloat capture not requested", "indexes": {}}
    )
    return {
        "table_name": "client_data",
        "index_bytes_total": sum(row["index_bytes"] for row in indexes),
        "required_indexes": list(REQUIRED_CLIENT_DATA_INDEXES),
        "missing_required_indexes": [
            name for name in REQUIRED_CLIENT_DATA_INDEXES if name not in names
        ],
        "zero_scan_indexes": [
            row["index_name"] for row in indexes if row["idx_scan"] == 0
        ],
        "zero_scan_non_protected_indexes": [
            row["index_name"]
            for row in indexes
            if row["idx_scan"] == 0 and not row["protected"]
        ],
        "protected_indexes": [
            row["index_name"] for row in indexes if row["protected"]
        ],
        "active_identity_duplicate_group_count": identity_duplicate_groups,
        "possible_overlap_pairs": _possible_index_overlaps(indexes),
        "plan_evidence": plan_evidence,
        "bloat_evidence": bloat_evidence,
        "maintenance_candidates": _index_maintenance_candidates(
            indexes,
            plan_evidence,
            bloat_evidence,
        ),
        "indexes": indexes,
        "policy": (
            "Indexes are never dropped automatically; primary, unique, and "
            "active-identity indexes are protected, and zero-scan or overlapping "
            "indexes require EXPLAIN and workload evidence."
        ),
    }


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
        "cashflow_current_prepared_lines": _count(
            """
            SELECT COUNT(*) AS count
            FROM public.cashflow_prepared_lines p
            JOIN public.cashflow_preparation_state state
              ON state.client_id = p.client_id
             AND state.active_preparation_revision = p.preparation_revision
            WHERE state.active_preparation_revision > 0
            """
        ),
        "cashflow_current_prepared_rollups": _count(
            """
            SELECT COUNT(*) AS count
            FROM public.cashflow_prepared_rollups p
            JOIN public.cashflow_preparation_state state
              ON state.client_id = p.client_id
             AND state.active_preparation_revision = p.preparation_revision
            WHERE state.active_preparation_revision > 0
            """
        ),
    }


def _cashflow_revision_candidates() -> Dict[str, Any]:
    """Measure obsolete legacy rows and stale prepared revisions."""
    legacy_rollups = _first(
        """
        SELECT COUNT(*) AS count,
               COALESCE(SUM(pg_column_size(r)), 0) AS payload_bytes
        FROM public.cashflow_ledger_rollup_read_models r
        """
    )
    prepared_lines = _first(
        """
        SELECT COUNT(*) AS count,
               COALESCE(SUM(pg_column_size(p)), 0) AS payload_bytes
        FROM public.cashflow_prepared_lines p
        JOIN public.cashflow_preparation_state state ON state.client_id = p.client_id
        WHERE state.active_preparation_revision > 0
          AND p.preparation_revision <> state.active_preparation_revision
        """
    )
    prepared_rollups = _first(
        """
        SELECT COUNT(*) AS count,
               COALESCE(SUM(pg_column_size(p)), 0) AS payload_bytes
        FROM public.cashflow_prepared_rollups p
        JOIN public.cashflow_preparation_state state ON state.client_id = p.client_id
        WHERE state.active_preparation_revision > 0
          AND p.preparation_revision <> state.active_preparation_revision
        """
    )
    retained_without_active_state = {
        "prepared_lines": _count(
            """
            SELECT COUNT(*) AS count
            FROM public.cashflow_prepared_lines p
            LEFT JOIN public.cashflow_preparation_state state ON state.client_id = p.client_id
            WHERE state.client_id IS NULL OR state.active_preparation_revision <= 0
            """
        ),
        "prepared_rollups": _count(
            """
            SELECT COUNT(*) AS count
            FROM public.cashflow_prepared_rollups p
            LEFT JOIN public.cashflow_preparation_state state ON state.client_id = p.client_id
            WHERE state.client_id IS NULL OR state.active_preparation_revision <= 0
            """
        ),
    }

    def candidate(row: Dict[str, Any], rule: str) -> Dict[str, Any]:
        payload_bytes = int(row.get("payload_bytes") or 0)
        return {
            "row_count": int(row.get("count") or 0),
            "payload_bytes": payload_bytes,
            "formatted": _format_bytes(payload_bytes),
            "rule": rule,
        }

    return {
        "legacy_rollup_rows": candidate(
            legacy_rollups,
            "legacy rollup table is obsolete; current reports use prepared lines and rollups",
        ),
        "prepared_line_revisions": candidate(
            prepared_lines,
            "preparation_revision differs from a positive active preparation revision",
        ),
        "prepared_rollup_revisions": candidate(
            prepared_rollups,
            "preparation_revision differs from a positive active preparation revision",
        ),
        "retained_without_active_state": retained_without_active_state,
        "safety_rule": "Rows without a positive active preparation revision are retained for explicit rebuild/reconciliation.",
    }


def _cashflow_legacy_copy_candidates() -> Dict[str, Any]:
    """Measure transaction-level copies that are no longer report sources."""
    candidates: Dict[str, Any] = {}
    for table_name in (
        "cash_bank_transaction_classifications",
        "cash_bank_summary_read_models",
        "cashflow_ledger_rollup_read_models",
        "cashflow_prepared_lines",
        "cashflow_prepared_rollups",
    ):
        row = _first(
            f"""SELECT COUNT(*) AS count,
                       COALESCE(SUM(pg_column_size(t)), 0) AS payload_bytes
                FROM public.{table_name} t"""
        )
        candidates[table_name] = {
            "row_count": int(row.get("count") or 0),
            "payload_bytes": int(row.get("payload_bytes") or 0),
            "formatted": _format_bytes(int(row.get("payload_bytes") or 0)),
            "rule": "legacy transaction/report copy; canonical client_data is the only report source",
        }
    return {
        "tables": candidates,
        "safety_rule": "cashflow_voucher_occurrences is retained because it stores compact decision metadata only",
    }


def _preview_fingerprint_payload(preview: Dict[str, Any]) -> Dict[str, Any]:
    """Select all candidate/protection aggregates that must match before apply."""
    return {
        "database": preview["database"],
        # Estimated row statistics and maintenance timestamps are diagnostic;
        # they do not expand the approved cleanup scope.
        "table_sizes": [{key: row.get(key) for key in (
            "table_name", "classification", "table_bytes", "index_bytes", "total_bytes", "formatted"
        )} for row in preview["table_sizes"]],
        "index_review": preview["index_review"],
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


def _purge_scope_predicate(alias: str, client_id: int, template_id: int | None = None):
    """Return a parameterized client/template predicate for purge SQL."""
    predicate = f"{alias}.client_id = %s"
    params = [client_id]
    if template_id is not None:
        predicate += f" AND {alias}.template_id = %s"
        params.append(template_id)
    return predicate, params


def _purge_target_uploads(client_id: int, template_id: int | None = None) -> List[Dict[str, Any]]:
    """Find upload metadata owned by the requested source-data scope."""
    direct = "uf.client_id = %s"
    source = "cd.client_id = %s"
    params = [client_id, client_id]
    if template_id is not None:
        direct += " AND uf.template_id = %s"
        source += " AND cd.template_id = %s"
        params = [client_id, template_id, client_id, template_id]
    return execute_query_pg(
        f"""
        SELECT uf.id, uf.file_name, uf.file_path, uf.status
        FROM public.uploaded_files uf
        WHERE ({direct})
           OR EXISTS (
                SELECT 1 FROM public.client_data cd
                WHERE cd.upload_id = uf.id AND {source}
           )
        ORDER BY uf.id
        """,
        params,
    )


def _purge_scoped_count(
    table_name: str,
    alias: str,
    client_id: int,
    template_id: int | None = None,
) -> int:
    if table_name == "cashflow_ledger_rollup_read_models" and not _first(
        "SELECT to_regclass('public.cashflow_ledger_rollup_read_models') AS table_name"
    ).get("table_name"):
        return 0
    predicate, params = _purge_scope_predicate(alias, client_id, template_id)
    return _count(f"SELECT COUNT(*) AS count FROM public.{table_name} {alias} WHERE {predicate}", params)


def _purge_audit_log_ids(client_id: int, template_id: int | None) -> List[int]:
    """Match only structured purge events; unrelated application logs stay intact."""
    rows = execute_query_pg(
        "SELECT id, details FROM public.audit_log "
        "WHERE (action = 'Permanent data delete' AND module = 'DataBank') "
        "OR (action = 'Replacement data purge' AND module = 'Upload') ORDER BY id"
    )
    ids = []
    for row in rows:
        try:
            details = row["details"] if isinstance(row["details"], dict) else json.loads(row["details"] or "{}")
        except (TypeError, ValueError):
            continue
        if (isinstance(details, dict) and details.get("client_id") == client_id
                and (template_id is None or details.get("template_id") == template_id)):
            ids.append(int(row["id"]))
    return ids


def _finalization_controls(audit_log_id: int) -> Dict[str, Any]:
    """Compare stored values without retaining source rows or a database receipt.

    Session/presence/error traffic is excluded explicitly. Only the exact approved
    audit entry is excluded from the audit-table fingerprint; other logs remain.
    Catalog-sourced names are quoted identifiers, never interpolated arguments.
    """
    tables = execute_query_pg("""SELECT c.relname AS table_name
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND c.relname <> ALL(%s) ORDER BY c.relname""",
        (["sessions", "verification_presence", "error_log"],))
    if not tables or not {"client_data", "uploaded_files", "audit_log"}.issubset(
        {row["table_name"] for row in tables}
    ):
        raise RuntimeError("Protected table inventory is incomplete")
    statements = []
    params = []
    for row in tables:
        name = row["table_name"]
        condition = sql.SQL("WHERE r.id <> %s") if name == "audit_log" else sql.SQL("")
        statements.append(sql.SQL("""SELECT {label} AS table_name, COUNT(*) AS row_count,
            md5(COALESCE(string_agg(md5(to_jsonb(r)::text), ''
                ORDER BY md5(to_jsonb(r)::text)), '')) AS fingerprint
            FROM public.{table} r {condition}""").format(
                label=sql.Literal(name), table=sql.Identifier(name), condition=condition))
        if name == "audit_log":
            params.append(audit_log_id)
    rows = execute_query_pg(sql.SQL(" UNION ALL ").join(statements), tuple(params))
    if len(rows) != len(tables):
        raise RuntimeError("Protected record checks are incomplete")
    indexes = execute_query_pg("""SELECT t.relname AS table_name, c.relname AS index_name,
        pg_get_indexdef(i.indexrelid) AS definition, i.indisprimary AS is_primary,
        i.indisunique AS is_unique, i.indisvalid AS is_valid, i.indisready AS is_ready
        FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_class c ON c.oid = i.indexrelid
        WHERE n.nspname = 'public' ORDER BY t.relname, c.relname""")
    if not indexes:
        raise RuntimeError("Protected index checks are incomplete")
    return {"tables": sorted(rows, key=lambda row: row["table_name"]), "indexes": indexes,
            "excluded_volatile_tables": ["sessions", "verification_presence", "error_log"]}


def _template_cleanup_finalization_preview(client_id, template_id, audit_log_id):
    entry = _first("SELECT id, action, module, details FROM public.audit_log WHERE id = %s", (audit_log_id,))
    try:
        details = entry.get("details")
        details = details if isinstance(details, dict) else json.loads(details or "{}")
    except (TypeError, ValueError):
        details = None
    if (not isinstance(details, dict) or entry.get("action") != "Permanent data delete"
            or entry.get("module") != "DataBank" or details.get("client_id") != client_id
            or details.get("template_id") != template_id):
        raise ValueError("The exact legacy deletion entry does not match the reviewed client/template")
    if _count("SELECT COUNT(*) AS count FROM public.cash_bank_transaction_classifications") != 0:
        raise IndexMaintenanceBlockedError("The reviewed classification table is no longer empty")
    controls = _finalization_controls(audit_log_id)
    target_indexes = [row for row in controls["indexes"]
                      if row["table_name"] == "cash_bank_transaction_classifications"]
    if len(target_indexes) != 8 or not all(row["is_valid"] and row["is_ready"] for row in target_indexes):
        raise IndexMaintenanceBlockedError("The eight reviewed indexes are not all present and ready")
    evidence = {"scope": {"client_id": client_id, "template_id": template_id,
                          "audit_log_id": audit_log_id}, "entry": entry, "controls": controls}
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)
    return {"scope": evidence["scope"], "preview_hash": hashlib.sha256(encoded.encode()).hexdigest(),
            "controls": controls, "target_indexes": target_indexes}


def get_template_cleanup_finalization_preview(client_id: int, template_id: int, audit_log_id: int):
    """Operator-only preview; no HTTP route, startup hook, or automatic maintenance."""
    return run_read_snapshot_pg(lambda _conn: _template_cleanup_finalization_preview(
        client_id, template_id, audit_log_id))


def finalize_reviewed_template_cleanup(client_id: int, template_id: int, audit_log_id: int,
                                      preview_hash: str, confirmation: str):
    """One explicit, non-retried transaction; rollback on stale/unexpected evidence.

    Requires action-time administrator approval. It removes one scoped old audit
    entry, rebuilds only empty classification indexes and keeps no new receipt.
    """
    if confirmation != TEMPLATE_FINALIZATION_CONFIRMATION:
        raise CleanupConfirmationError("The exact finalization confirmation is required")
    with pg_transaction() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute("SET LOCAL statement_timeout = '120s'")
            cursor.execute("LOCK TABLE public.cash_bank_transaction_classifications IN ACCESS EXCLUSIVE MODE NOWAIT")
            cursor.execute("SELECT id FROM public.audit_log WHERE id = %s FOR UPDATE NOWAIT", (audit_log_id,))
            if not cursor.fetchone():
                raise CleanupPreviewStaleError("The reviewed legacy audit entry is no longer present")
        before = _template_cleanup_finalization_preview(client_id, template_id, audit_log_id)
        if before["preview_hash"] != preview_hash:
            raise CleanupPreviewStaleError("Finalization evidence changed; refresh the preview")
        deleted = _count("WITH deleted AS (DELETE FROM public.audit_log WHERE id = %s RETURNING id) "
                         "SELECT COUNT(*) AS count FROM deleted", (audit_log_id,))
        if deleted != 1:
            raise RuntimeError("Legacy deletion did not affect exactly one entry")
        fields = ("index_name", "definition", "is_primary", "is_unique", "is_valid", "is_ready")
        definitions = [tuple(row[field] for field in fields) for row in before["target_indexes"]]
        rebuild = rebuild_empty_classification_indexes(definitions)
        after = _finalization_controls(audit_log_id)
        if before["controls"] != after:
            raise RuntimeError("Protected data or index checks changed; finalization will roll back")
    return {"mode": "reviewed_template_cleanup_finalized", "scope": before["scope"],
            "deleted_audit_entries": deleted, "index_rebuild": rebuild,
            "protected_controls": after, "verification": "passed"}


def _linked_verification_counts(client_id: int, template_id: int) -> Dict[str, int]:
    """Count Verification records that a template-scoped test reset will remove."""
    target_details = """
        SELECT vd.id
        FROM public.verification_details vd
        JOIN public.client_data cd ON cd.id = vd.client_data_id
        WHERE cd.client_id = %s AND cd.template_id = %s
    """
    refs = """
        SELECT DISTINCT vd.transaction_ref
        FROM public.verification_details vd
        JOIN public.client_data cd ON cd.id = vd.client_data_id
        WHERE cd.client_id = %s AND cd.template_id = %s
          AND vd.transaction_ref IS NOT NULL
    """
    params = (client_id, template_id)
    base = {
        "verification_details": _count(
            f"SELECT COUNT(*) AS count FROM ({target_details}) scoped", params
        ),
        "voucher_line_documents": _count(
            f"SELECT COUNT(*) AS count FROM public.voucher_line_documents vld WHERE vld.voucher_line_id IN ({target_details})",
            params,
        ),
        "voucher_line_procedure_checks": _count(
            f"SELECT COUNT(*) AS count FROM public.voucher_line_procedure_checks c WHERE c.voucher_line_id IN ({target_details})",
            params,
        ),
        "voucher_line_observations": _count(
            f"SELECT COUNT(*) AS count FROM public.voucher_line_observations o WHERE o.voucher_line_id IN ({target_details})",
            params,
        ),
        "voucher_line_form_entries": _count(
            f"SELECT COUNT(*) AS count FROM public.voucher_line_form_entries e WHERE e.voucher_line_id IN ({target_details})",
            params,
        ),
        "voucher_line_physical_bills": _count(
            f"SELECT COUNT(*) AS count FROM public.voucher_line_physical_bills b WHERE b.voucher_line_id IN ({target_details})",
            params,
        ),
        "voucher_line_physical_bill_items": _count(
            f"SELECT COUNT(*) AS count FROM public.voucher_line_physical_bill_items i JOIN public.voucher_line_physical_bills b ON b.id = i.bill_id WHERE b.voucher_line_id IN ({target_details})",
            params,
        ),
        "voucher_line_form_values": _count(
            f"SELECT COUNT(*) AS count FROM public.voucher_line_form_values v JOIN public.voucher_line_form_entries e ON e.id = v.entry_id WHERE e.voucher_line_id IN ({target_details})",
            params,
        ),
        "voucher_check_status_to_delete": _count(
            f"""SELECT COUNT(*) AS count
                FROM public.voucher_check_status vcs
                WHERE vcs.client_id = %s AND vcs.transaction_ref IN ({refs})
                  AND NOT EXISTS (
                    SELECT 1 FROM public.client_data other_cd
                    WHERE other_cd.client_id = %s
                      AND other_cd.template_id <> %s
                      AND {_VOUCHER_ID_FROM_ROW.replace('cd.', 'other_cd.')} = vcs.transaction_ref
                  )""",
            (client_id, client_id, template_id, client_id, template_id),
        ),
        "voucher_check_status_retained_for_other_templates": _count(
            f"""SELECT COUNT(*) AS count
                FROM public.voucher_check_status vcs
                WHERE vcs.client_id = %s AND vcs.transaction_ref IN ({refs})
                  AND EXISTS (
                    SELECT 1 FROM public.client_data other_cd
                    WHERE other_cd.client_id = %s
                      AND other_cd.template_id <> %s
                      AND {_VOUCHER_ID_FROM_ROW.replace('cd.', 'other_cd.')} = vcs.transaction_ref
                  )""",
            (client_id, client_id, template_id, client_id, template_id),
        ),
    }
    return base


def _purge_protected_counts(
    client_id: int,
    template_id: int | None = None,
    include_linked_verification: bool = False,
) -> Dict[str, int]:
    predicate, params = _purge_scope_predicate("cd", client_id, template_id)
    if include_linked_verification and template_id is not None:
        _require_test_verification_purge_enabled()
        target_details = """
            SELECT vd.id
            FROM public.verification_details vd
            JOIN public.client_data target_cd ON target_cd.id = vd.client_data_id
            WHERE target_cd.client_id = %s AND target_cd.template_id = %s
        """
        # Keep mismatched links protected: the review line must itself belong to this scope.
        params_for_details = (client_id, template_id)
        verification_count_sql = "SELECT 0 AS count"
        verification_count_params = ()
        document_sql = f"""
            SELECT COUNT(*) AS count
            FROM public.voucher_line_documents vld
            JOIN public.client_data cd ON cd.id = vld.client_data_id
            WHERE {predicate}
              AND vld.voucher_line_id NOT IN ({target_details})
        """
        document_params = (*params, *params_for_details)
        physical_item_sql = f"""
            SELECT COUNT(*) AS count
            FROM public.voucher_line_physical_bill_items item
            JOIN public.client_data cd ON cd.id = item.source_document_row_id
            JOIN public.voucher_line_physical_bills bill ON bill.id = item.bill_id
            WHERE {predicate}
              AND bill.voucher_line_id NOT IN ({target_details})
        """
        physical_item_params = (*params, *params_for_details)
    else:
        verification_count_sql = f"""
            SELECT COUNT(*) AS count
            FROM public.verification_details vd
            JOIN public.client_data cd ON cd.id = vd.client_data_id
            WHERE {predicate}
        """
        verification_count_params = params
        document_sql = f"""
            SELECT COUNT(*) AS count
            FROM public.voucher_line_documents vld
            JOIN public.client_data cd ON cd.id = vld.client_data_id
            WHERE {predicate}
        """
        document_params = params
        physical_item_sql = f"""
            SELECT COUNT(*) AS count
            FROM public.voucher_line_physical_bill_items item
            JOIN public.client_data cd ON cd.id = item.source_document_row_id
            WHERE {predicate}
        """
        physical_item_params = params
    protected = {
        "verification_details": _count(verification_count_sql, verification_count_params),
        "voucher_line_documents": _count(document_sql, document_params),
        "physical_bill_items": _count(
            physical_item_sql,
            physical_item_params,
        ),
        "prepared_lines": _count(
            f"""
            SELECT COUNT(*) AS count
            FROM public.cashflow_prepared_lines p
            JOIN public.client_data cd ON cd.id = p.source_client_data_id
            WHERE {predicate}
            """,
            params,
        ),
    }
    return protected


def purge_replaced_transaction_rows(client_id, template_id, row_ids, operator, archive_proof=None):
    """Purge explicitly missing import rows in the caller's transaction.

    Lock and validate the exact row scope, then protect the same references as
    full dataset deletion. File removal is returned for post-commit execution.
    """
    ids = sorted(set(int(value) for value in row_ids))
    if not ids:
        return {"deleted_count": 0, "staged_paths": []}
    if (not archive_proof or (not archive_proof.get('archive') and archive_proof.get('archive_mode') != 'none')
            or not set(ids).issubset(archive_proof.get('ids', []))):
        raise DataPurgeProtectedError('A verified external archive is required before removing replacement source rows.')
    with pg_transaction():
        receipt = execute_query_pg('''SELECT status, drive_file_id, archive_sha256, archive_state_hash, scope_json
            FROM upload_lifecycle_runs WHERE operation_id = %s AND client_id = %s AND template_id = %s FOR UPDATE''',
            (archive_proof['operation_id'], client_id, template_id))
        proof = archive_proof.get('archive') or {}
        unbacked = bool(receipt and unbacked_receipt_matches(archive_proof, receipt[0], execute_query_pg))
        if not unbacked and (not receipt or receipt[0]['status'] != 'verified'
                or not proof
                or receipt[0]['drive_file_id'] != proof['drive_file_id']
                or receipt[0]['archive_sha256'] != proof['sha256']):
            raise DataPurgeProtectedError('The replacement archive receipt is missing or does not match this scope.')
        rows = execute_query_pg(
            """SELECT id, upload_id, derived_voucher_id FROM public.client_data
               WHERE client_id = %s AND template_id = %s AND id = ANY(%s)
               ORDER BY id FOR UPDATE""", (client_id, template_id, ids),
        )
        if {int(row["id"]) for row in rows} != set(ids):
            raise DataPurgeProtectedError("Replacement scope changed; validate the upload again.")
        for table, column in (
            ("verification_details", "client_data_id"),
            ("voucher_line_documents", "client_data_id"),
            ("voucher_line_physical_bill_items", "source_document_row_id"),
            ("cashflow_prepared_lines", "source_client_data_id"),
        ):
            if _count(f"SELECT COUNT(*) AS count FROM public.{table} WHERE {column} = ANY(%s)", (ids,)):
                raise DataPurgeProtectedError("Replacement blocked by protected verification or evidence references.")
        for table in ("cash_bank_transaction_classifications", "data_audit_log"):
            execute_write_pg(f"DELETE FROM public.{table} WHERE client_data_id = ANY(%s)", (ids,))
        vouchers = list({row["derived_voucher_id"] for row in rows if row.get("derived_voucher_id")})
        execute_write_pg(
            """DELETE FROM public.cashflow_voucher_occurrences
               WHERE client_id = %s AND template_id = %s AND voucher_id = ANY(%s)""",
            (client_id, template_id, vouchers),
        )
        # Retired report copies cannot retain rows that have just been removed.
        for table in ("cashflow_prepared_rollups", "cash_bank_summary_read_models", "cashflow_ledger_rollup_read_models", "upload_validation_plans"):
            if table == 'cashflow_ledger_rollup_read_models':
                # Fresh databases omit this retired copy; older databases must
                # still purge it. Never recreate a second transaction store.
                existing = execute_query_pg("SELECT to_regclass('public.cashflow_ledger_rollup_read_models') AS table_name")
                if not existing or not existing[0].get('table_name'):
                    continue
            execute_write_pg(f"DELETE FROM public.{table} WHERE client_id = %s AND template_id = %s", (client_id, template_id))
        execute_write_pg("DELETE FROM public.client_data WHERE id = ANY(%s)", (ids,))
        upload_ids = list({row["upload_id"] for row in rows if row.get("upload_id") is not None})
        orphans = execute_query_pg(
            """SELECT uf.id, uf.file_path FROM public.uploaded_files uf
               WHERE uf.id = ANY(%s) AND uf.client_id = %s AND uf.template_id = %s
                 AND NOT EXISTS (SELECT 1 FROM public.client_data cd WHERE cd.upload_id = uf.id)
                 AND NOT EXISTS (SELECT 1 FROM public.verifications v WHERE v.file_id = uf.id)
               FOR UPDATE""", (upload_ids, client_id, template_id),
        )
        if orphans:
            execute_write_pg("DELETE FROM public.uploaded_files WHERE id = ANY(%s)", ([row["id"] for row in orphans],))
        execute_write_pg(
            'INSERT INTO audit_log ("user", action, module, details) VALUES (%s, %s, %s, %s)',
            (operator, "Replacement data purge", "Upload", json.dumps({"client_id": client_id, "template_id": template_id, "deleted_count": len(ids)})),
        )
    return {"deleted_count": len(ids), "staged_paths": [row["file_path"] for row in orphans]}


def _purge_preview_hash(preview: Dict[str, Any]) -> str:
    payload = {
        "scope": preview["scope"],
        "upload_ids": preview["upload_ids"],
        "counts": preview["counts"],
        "protected": preview["protected"],
        "include_linked_verification": bool(preview.get("include_linked_verification")),
        "linked_verification": preview.get("linked_verification", {}),
        "source_fingerprint": preview.get("source_fingerprint"),
        "audit_log_ids": preview.get("audit_log_ids", []),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_client_data_purge_preview(
    client_id: int,
    template_id: int | None = None,
    include_linked_verification: bool = False,
) -> Dict[str, Any]:
    """Return a read-only, exact-scope preview for permanent source-data deletion."""
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("PostgreSQL is not configured")

    client = _first(
        "SELECT id, client_name, is_active FROM public.clients WHERE id = %s",
        (client_id,),
    )
    if not client:
        raise ValueError("Client not found")
    if template_id is not None and not _first(
        "SELECT id, template_name FROM public.templates WHERE id = %s",
        (template_id,),
    ):
        raise ValueError("Template not found")
    if include_linked_verification:
        if template_id is None:
            raise DataPurgeProtectedError(
                "Linked Verification cleanup must be limited to one template."
            )
        _require_test_verification_purge_enabled()

    uploads = _purge_target_uploads(client_id, template_id)
    upload_ids = [int(row["id"]) for row in uploads]
    audit_log_ids = _purge_audit_log_ids(client_id, template_id)
    predicate, params = _purge_scope_predicate("cd", client_id, template_id)
    counts = {
        "client_data": _purge_scoped_count("client_data", "cd", client_id, template_id),
        "cash_bank_transaction_classifications": _purge_scoped_count(
            "cash_bank_transaction_classifications", "c", client_id, template_id
        ),
        "cashflow_voucher_occurrences": _purge_scoped_count(
            "cashflow_voucher_occurrences", "o", client_id, template_id
        ),
        "cashflow_prepared_lines": _purge_scoped_count(
            "cashflow_prepared_lines", "p", client_id, template_id
        ),
        "cashflow_prepared_rollups": _purge_scoped_count(
            "cashflow_prepared_rollups", "p", client_id, template_id
        ),
        "cash_bank_summary_read_models": _purge_scoped_count(
            "cash_bank_summary_read_models", "s", client_id, template_id
        ),
        "cashflow_ledger_rollup_read_models": _purge_scoped_count(
            "cashflow_ledger_rollup_read_models", "r", client_id, template_id
        ),
        "data_audit_log": _count(
            f"""
            SELECT COUNT(*) AS count
            FROM public.data_audit_log a
            JOIN public.client_data cd ON cd.id = a.client_data_id
            WHERE {predicate}
            """,
            params,
        ),
        "client_data_history": _purge_scoped_count(
            "client_data_history", "h", client_id, template_id
        ),
        "upload_validation_plans": _purge_scoped_count(
            "upload_validation_plans", "p", client_id, template_id
        ),
        "verifications": _count(
            "SELECT COUNT(*) AS count FROM public.verifications WHERE file_id = ANY(%s)",
            (upload_ids,),
        ) if upload_ids else 0,
        "uploaded_files": len(upload_ids),
        "audit_log": len(audit_log_ids),
        "upload_lifecycle_runs": _purge_scoped_count(
            "upload_lifecycle_runs", "l", client_id, template_id
        ),
    }
    if template_id is None:
        counts.update({
            "cashflow_report_revisions": _count(
                "SELECT COUNT(*) AS count FROM public.cashflow_report_revisions WHERE client_id = %s",
                (client_id,),
            ),
            "cashflow_preparation_state": _count(
                "SELECT COUNT(*) AS count FROM public.cashflow_preparation_state WHERE client_id = %s",
                (client_id,),
            ),
            "cashflow_preparation_jobs": _count(
                "SELECT COUNT(*) AS count FROM public.cashflow_preparation_jobs WHERE client_id = %s",
                (client_id,),
            ),
            "verification_sessions": _count(
                "SELECT COUNT(*) AS count FROM public.verification_sessions WHERE client_id = %s",
                (client_id,),
            ),
        })

    linked_verification = (
        _linked_verification_counts(client_id, template_id)
        if include_linked_verification and template_id is not None
        else {}
    )
    protected = _purge_protected_counts(
        client_id, template_id, include_linked_verification
    )
    # Counts alone cannot detect a correction made after backup/preview.
    source_fingerprint = _first(
        "SELECT md5(string_agg(cd.id::text || '|' || COALESCE(cd.source_fingerprint, '') || '|' || COALESCE(cd.source_identity, ''), ',' ORDER BY cd.id)) AS fingerprint FROM public.client_data cd WHERE " + predicate,
        params,
    ).get("fingerprint")
    preview = {
        "mode": "permanent_data_delete_dry_run",
        "scope": {"client_id": client_id, "template_id": template_id},
        "client": {
            "id": client_id,
            "name": client["client_name"],
            "is_active": bool(client["is_active"]),
        },
        "upload_ids": upload_ids,
        "audit_log_ids": audit_log_ids,
        "staged_file_names": [row["file_name"] for row in uploads],
        "counts": counts,
        "protected": protected,
        "include_linked_verification": include_linked_verification,
        "linked_verification": linked_verification,
        "source_fingerprint": source_fingerprint,
        "can_apply": not any(protected.values()),
        "safety": [
            "This preview performs no INSERT, UPDATE, DELETE, or VACUUM operation.",
            (
                "Linked Verification records are included only in the explicit test-project reset mode; other linked evidence and prepared-line blockers remain protected."
                if include_linked_verification else
                "Any verification, evidence, physical-bill, or prepared-line reference blocks deletion."
            ),
            "The apply operation re-runs this exact preview hash inside a transaction.",
            "Staged source files are removed only after the database transaction commits.",
        ],
    }
    preview["preview_hash"] = _purge_preview_hash(preview)
    return preview


def _safe_upload_file_path(file_path: str) -> str | None:
    if not file_path:
        return None
    root = os.path.abspath("uploads")
    candidate = os.path.abspath(file_path)
    try:
        if os.path.commonpath([root, candidate]) != root:
            return None
    except ValueError:
        return None
    return candidate


def _remove_staged_upload_files(paths: List[str]) -> List[str]:
    failures = []
    for file_path in paths:
        safe_path = _safe_upload_file_path(file_path)
        if not safe_path:
            failures.append(file_path)
            continue
        if not os.path.exists(safe_path):
            continue
        try:
            os.remove(safe_path)
        except OSError:
            failures.append(file_path)
    return failures


def apply_client_data_purge(
    preview_hash: str,
    operator: str,
    confirmation: str,
    client_id: int,
    template_id: int | None = None,
    include_linked_verification: bool = False,
) -> Dict[str, Any]:
    """Permanently remove one client/template source-data scope after preview approval."""
    if confirmation != PERMANENT_DATA_DELETE_CONFIRMATION:
        raise CleanupConfirmationError(
            f"Type exactly: {PERMANENT_DATA_DELETE_CONFIRMATION}"
        )

    fresh_preview = get_client_data_purge_preview(
        client_id, template_id, include_linked_verification
    )
    if preview_hash != fresh_preview["preview_hash"]:
        raise DataPurgePreviewStaleError(
            "The permanent-delete preview is stale. Refresh the preview before applying it."
        )
    if not fresh_preview["can_apply"]:
        raise DataPurgeProtectedError(
            "Protected verification or evidence references exist; deletion was not performed."
        )

    upload_ids = fresh_preview["upload_ids"]
    upload_rows = _purge_target_uploads(client_id, template_id)
    staged_paths = [row["file_path"] for row in upload_rows]
    predicate, params = _purge_scope_predicate("cd", client_id, template_id)
    deleted_counts: Dict[str, int] = {}

    with pg_transaction():
        execute_write_pg("SET LOCAL statement_timeout = '120s'")
        execute_query_pg("SELECT pg_advisory_xact_lock(hashtext('upload-storage-budget'))")
        execute_query_pg(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"permanent-data-delete:{client_id}:{template_id or 'all'}",),
        )
        if include_linked_verification:
            # These tables are not fully tied to source rows by foreign keys; block
            # concurrent writers while the fresh preview and exact scoped deletes run.
            execute_query_pg(
                "LOCK TABLE public.voucher_check_status, public.voucher_line_form_entries "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
        # Protect the preview through deletion, including concurrent FK checks.
        _count("SELECT COUNT(*) AS count FROM (SELECT cd.id FROM public.client_data cd WHERE "
               + predicate + " ORDER BY cd.id FOR UPDATE) locked_source", params)
        locked_preview = get_client_data_purge_preview(
            client_id, template_id, include_linked_verification
        )
        if preview_hash != locked_preview["preview_hash"]:
            raise DataPurgePreviewStaleError(
                "The permanent-delete preview changed while waiting for the deletion lock."
            )
        if not locked_preview["can_apply"]:
            raise DataPurgeProtectedError(
                "Protected verification or evidence references appeared; deletion was rolled back."
            )

        delete_specs = []
        if include_linked_verification:
            target_details = """
                SELECT vd.id
                FROM public.verification_details vd
                JOIN public.client_data cd ON cd.id = vd.client_data_id
                WHERE cd.client_id = %s AND cd.template_id = %s
            """
            details_params = (client_id, template_id)
            delete_specs.extend([
                ("voucher_line_form_values", f"""
                    DELETE FROM public.voucher_line_form_values v
                    WHERE v.entry_id IN (
                        SELECT e.id FROM public.voucher_line_form_entries e
                        WHERE e.voucher_line_id IN ({target_details})
                    )
                """),
                ("voucher_line_form_entries", f"""
                    DELETE FROM public.voucher_line_form_entries e
                    WHERE e.voucher_line_id IN ({target_details})
                """),
                ("voucher_line_observations", f"""
                    DELETE FROM public.voucher_line_observations o
                    WHERE o.voucher_line_id IN ({target_details})
                """),
                ("voucher_line_procedure_checks", f"""
                    DELETE FROM public.voucher_line_procedure_checks c
                    WHERE c.voucher_line_id IN ({target_details})
                """),
                ("voucher_line_documents", f"""
                    DELETE FROM public.voucher_line_documents vld
                    WHERE vld.voucher_line_id IN ({target_details})
                """),
                ("voucher_line_physical_bill_items", f"""
                    DELETE FROM public.voucher_line_physical_bill_items i
                    WHERE i.bill_id IN (
                        SELECT b.id FROM public.voucher_line_physical_bills b
                        WHERE b.voucher_line_id IN ({target_details})
                    )
                """),
                ("voucher_line_physical_bills", f"""
                    DELETE FROM public.voucher_line_physical_bills b
                    WHERE b.voucher_line_id IN ({target_details})
                """),
                ("voucher_check_status", f"""
                    DELETE FROM public.voucher_check_status vcs
                    WHERE vcs.client_id = %s
                      AND vcs.transaction_ref IN (
                        SELECT DISTINCT vd.transaction_ref
                        FROM public.verification_details vd
                        JOIN public.client_data cd ON cd.id = vd.client_data_id
                        WHERE cd.client_id = %s AND cd.template_id = %s
                          AND vd.transaction_ref IS NOT NULL
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM public.client_data other_cd
                        WHERE other_cd.client_id = %s
                          AND other_cd.template_id <> %s
                          AND {_VOUCHER_ID_FROM_ROW.replace('cd.', 'other_cd.')} = vcs.transaction_ref
                      )
                """),
                ("verification_details", f"""
                    DELETE FROM public.verification_details vd
                    WHERE vd.id IN ({target_details})
                """),
            ])
        delete_specs.extend([
            ("cashflow_prepared_lines", f"DELETE FROM public.cashflow_prepared_lines p WHERE {predicate.replace('cd.', 'p.')}"),
            ("cashflow_prepared_rollups", f"DELETE FROM public.cashflow_prepared_rollups p WHERE {predicate.replace('cd.', 'p.')}"),
            ("cash_bank_summary_read_models", f"DELETE FROM public.cash_bank_summary_read_models s WHERE {predicate.replace('cd.', 's.')}"),
            ("cashflow_ledger_rollup_read_models", f"DELETE FROM public.cashflow_ledger_rollup_read_models r WHERE {predicate.replace('cd.', 'r.')}"),
            ("cash_bank_transaction_classifications", f"DELETE FROM public.cash_bank_transaction_classifications c WHERE {predicate.replace('cd.', 'c.')}"),
            ("cashflow_voucher_occurrences", f"DELETE FROM public.cashflow_voucher_occurrences o WHERE {predicate.replace('cd.', 'o.')}"),
            ("client_data_history", f"DELETE FROM public.client_data_history h WHERE {predicate.replace('cd.', 'h.')}"),
            ("data_audit_log", f"""
                DELETE FROM public.data_audit_log a
                USING public.client_data cd
                WHERE a.client_data_id = cd.id AND {predicate}
            """),
            ("upload_validation_plans", f"DELETE FROM public.upload_validation_plans p WHERE {predicate.replace('cd.', 'p.')}"),
            ("upload_lifecycle_runs", f"DELETE FROM public.upload_lifecycle_runs l WHERE {predicate.replace('cd.', 'l.')}"),
        ])
        if upload_ids:
            delete_specs.append((
                "verifications",
                "DELETE FROM public.verifications WHERE file_id = ANY(%s)",
            ))
        for name, sql in delete_specs:
            if name == "cashflow_ledger_rollup_read_models" and not _first(
                "SELECT to_regclass('public.cashflow_ledger_rollup_read_models') AS table_name"
            ).get("table_name"):
                deleted_counts[name] = 0
                continue
            if name == "verifications":
                sql_params = (upload_ids,)
            elif name in {
                "voucher_line_form_values", "voucher_line_form_entries",
                "voucher_line_observations", "voucher_line_procedure_checks",
                "voucher_line_documents", "voucher_line_physical_bill_items",
                "voucher_line_physical_bills", "verification_details",
            }:
                sql_params = details_params
            elif name == "voucher_check_status":
                sql_params = (
                    client_id, client_id, template_id, client_id, template_id
                )
            else:
                sql_params = params

            deleted_counts[name] = _delete_count(sql.replace("%s", "%s")) if not sql_params else _count(
                "WITH deleted AS (" + sql.rstrip() + " RETURNING 1) SELECT COUNT(*) AS count FROM deleted",
                sql_params,
            )

        if template_id is None:
            for name, sql in (
                ("cashflow_report_revisions", "DELETE FROM public.cashflow_report_revisions WHERE client_id = %s"),
                ("cashflow_preparation_state", "DELETE FROM public.cashflow_preparation_state WHERE client_id = %s"),
                ("cashflow_preparation_jobs", "DELETE FROM public.cashflow_preparation_jobs WHERE client_id = %s"),
                ("verification_sessions", "DELETE FROM public.verification_sessions WHERE client_id = %s"),
            ):
                deleted_counts[name] = _count(
                    "WITH deleted AS (" + sql + " RETURNING 1) SELECT COUNT(*) AS count FROM deleted",
                    (client_id,),
                )

        # Keep reusable Cashflows settings, but detach the source upload before
        # removing its metadata (also supports older schemas without the FK).
        if upload_ids:
            execute_write_pg(
                "UPDATE public.account_control_cashflow_settings SET source_upload_id = NULL "
                "WHERE client_id = %s AND source_upload_id = ANY(%s)",
                (client_id, upload_ids),
            )
        audit_ids = locked_preview.get("audit_log_ids", [])
        deleted_counts["audit_log"] = _count(
            "WITH deleted AS (DELETE FROM public.audit_log WHERE id = ANY(%s) RETURNING 1) "
            "SELECT COUNT(*) AS count FROM deleted", (audit_ids,),
        ) if audit_ids else 0

        deleted_counts["client_data"] = _count(
            "WITH deleted AS (DELETE FROM public.client_data cd WHERE "
            + predicate
            + " RETURNING 1) SELECT COUNT(*) AS count FROM deleted",
            params,
        )
        if upload_ids:
            deleted_counts["uploaded_files"] = _count(
                "WITH deleted AS (DELETE FROM public.uploaded_files WHERE id = ANY(%s) RETURNING 1) "
                "SELECT COUNT(*) AS count FROM deleted",
                (upload_ids,),
            )
        else:
            deleted_counts["uploaded_files"] = 0

        # A permanent scope purge intentionally leaves no new deletion receipt.

    file_cleanup_failures = _remove_staged_upload_files(staged_paths)
    return {
        "mode": "permanent_data_delete_applied",
        "scope": fresh_preview["scope"],
        "deleted_counts": deleted_counts,
        "staged_file_cleanup_failures": file_cleanup_failures,
        "verification": get_client_data_purge_preview(
            client_id, template_id, include_linked_verification
        ),
    }


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

    validation_plans = []
    for table_name in TEMPORARY_PLAN_TABLES:
        validation_plans.append(
            {
                "table_name": table_name,
                "row_count": _count(
                    f"SELECT COUNT(*) AS count FROM public.{table_name}"
                ),
                "physical_bytes": size_map.get(table_name, {}).get("total_bytes", 0),
                "formatted": size_map.get(table_name, {}).get("formatted", "0 B"),
                "rule": "temporary validation payload; applied/expired/superseded plans are removable",
            }
        )

    retired_candidate = _first(
        """
        SELECT COUNT(*) AS count,
               COALESCE(SUM(pg_column_size(cd.row_data)), 0) AS payload_bytes
        FROM public.client_data cd
        WHERE (cd.is_deleted = TRUE OR cd.change_detection_status = 'deleted')
          AND NOT EXISTS (SELECT 1 FROM public.verification_details vd WHERE vd.client_data_id = cd.id)
          AND NOT EXISTS (SELECT 1 FROM public.voucher_line_documents vld WHERE vld.client_data_id = cd.id)
          AND NOT EXISTS (SELECT 1 FROM public.voucher_line_physical_bill_items vpbbi WHERE vpbbi.source_document_row_id = cd.id)
          AND NOT EXISTS (SELECT 1 FROM public.cash_bank_transaction_classifications c WHERE c.client_data_id = cd.id)
          AND NOT EXISTS (SELECT 1 FROM public.cashflow_prepared_lines p WHERE p.source_client_data_id = cd.id)
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
        "index_review": _client_data_index_review(
            include_query_plans=True,
            include_bloat_evidence=True,
        ),
        "protected": _protected_counts(),
        "removable": {
            "logs": logs,
            "validation_plans": validation_plans,
            "retired_client_data": {
                "row_count": int(retired_candidate.get("count") or 0),
                "payload_bytes": int(retired_candidate.get("payload_bytes") or 0),
                "formatted": _format_bytes(int(retired_candidate.get("payload_bytes") or 0)),
                "rule": "deleted/change_deleted row with no Verification, document-link, physical-bill, classification, or prepared-line reference",
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
            "cashflow_historical_revisions": _cashflow_revision_candidates(),
            "cashflow_legacy_transaction_copies": _cashflow_legacy_copy_candidates(),
        },
        "safety": [
            "This preview performs no INSERT, UPDATE, DELETE, TRUNCATE, or VACUUM operation.",
            "Current Data Bank rows, Verification details, Trial Balance data, and linked evidence are protected.",
            "Apply requires this fresh preview hash and the exact typed confirmation phrase.",
            "VACUUM, reindex, and index removal are separate explicit maintenance actions with post-checks.",
        ],
    }
    preview["storage_diagnostics"] = _storage_diagnostics(
        database_bytes, sizes, preview["removable"]
    )
    preview["preview_hash"] = _preview_hash(preview)
    return preview


def _optimization_invariants(preview: Dict[str, Any]) -> Dict[str, Any]:
    """Extract values that must not change during physical optimization."""
    protected = preview.get("protected") or {}
    review = preview.get("index_review") or {}
    protected_definitions = {
        row.get("index_name"): row.get("index_definition")
        for row in review.get("indexes", [])
        if row.get("protected")
    }
    return {
        "client_data_total": protected.get("client_data_total"),
        "client_data_current": protected.get("client_data_current"),
        "client_data_retained_for_verification": protected.get(
            "client_data_retained_for_verification"
        ),
        "verification_details": protected.get("verification_details"),
        "trial_balance_accounts": protected.get("trial_balance_accounts"),
        "trial_balance_uploads": protected.get("trial_balance_uploads"),
        "active_identity_duplicate_group_count": review.get(
            "active_identity_duplicate_group_count"
        ),
        "missing_required_indexes": tuple(review.get("missing_required_indexes") or []),
        "protected_indexes": tuple(review.get("protected_indexes") or []),
        "protected_index_definitions": protected_definitions,
    }


def _post_optimization_verification(
    before: Dict[str, Any],
    after: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Compare post-maintenance invariants without pretending DDL is reversible."""
    if not after:
        return {
            "status": "unavailable",
            "message": "Post-maintenance preview was unavailable; stop before another optimization action.",
        }
    before_values = _optimization_invariants(before)
    after_values = _optimization_invariants(after)
    mismatches = {
        key: {"before": before_values[key], "after": after_values[key]}
        for key in before_values
        if before_values[key] != after_values[key]
    }
    return {
        "status": "passed" if not mismatches else "failed",
        "invariants": list(before_values),
        "mismatches": mismatches,
        "message": (
            "Canonical row counts, protected references, duplicate blockers, and protected index definitions were unchanged."
            if not mismatches
            else "An optimization invariant changed; stop and review the database before any further maintenance."
        ),
    }


def apply_index_maintenance(
    preview_hash: str,
    index_name: str,
    operator: str,
    confirmation: str,
    operation: str = "drop",
) -> Dict[str, Any]:
    """Run one evidence-approved index optimization outside a transaction."""
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("PostgreSQL is not configured")
    if confirmation != INDEX_MAINTENANCE_CONFIRMATION_TEXT:
        raise CleanupConfirmationError(
            f"Type exactly: {INDEX_MAINTENANCE_CONFIRMATION_TEXT}"
        )
    if index_name not in POSTGRES_INDEX_MAINTENANCE_ALLOWLIST:
        raise IndexMaintenanceBlockedError(
            f"Index {index_name} is not an approved maintenance candidate."
        )
    if operation not in {"drop", "reindex"}:
        raise IndexMaintenanceBlockedError(
            f"Index operation {operation} is not allow-listed."
        )

    before = get_storage_cleanup_preview()
    if preview_hash != before["preview_hash"]:
        raise CleanupPreviewStaleError(
            "The preview is stale. Refresh the preview before index maintenance."
        )

    candidate = before["index_review"]["maintenance_candidates"].get(index_name)
    eligible_key = (
        "eligible_for_manual_approval"
        if operation == "drop"
        else "reindex_eligible_for_manual_approval"
    )
    if not candidate or not candidate.get(eligible_key):
        reason = candidate["reason"] if candidate else "candidate was not found"
        if operation == "reindex" and candidate and candidate.get("bloat_evidence"):
            reason = "measured bloat did not meet the reindex threshold"
        raise IndexMaintenanceBlockedError(
            f"Index {index_name} did not pass the evidence gate: {reason}."
        )

    maintained = (
        drop_postgres_index_concurrently(index_name)
        if operation == "drop"
        else reindex_postgres_index_concurrently(index_name)
    )
    try:
        after = get_storage_cleanup_preview()
    except Exception:
        after = None
    post_verification = _post_optimization_verification(before, after)
    return {
        "mode": "index_maintenance_applied",
        "operator": (operator or "Unknown administrator")[:200],
        "operation": operation,
        "index_name": maintained,
        "table_name": candidate["table_name"],
        "index_definition_before": candidate.get("index_definition"),
        "before": before,
        "after": after,
        "post_verification": post_verification,
        "requires_review": post_verification["status"] != "passed",
        "safety": [
            f"Only the evidence-approved non-integrity index was targeted for {operation}.",
            "No client_data rows or canonical transaction values were changed.",
            "The original index definition is returned for controlled recreation if required.",
            "The operation used a PostgreSQL CONCURRENTLY command outside the application transaction.",
        ],
    }


def compact_storage(
    preview_hash: str,
    operator: str,
    confirmation: str,
    full: bool = False,
) -> Dict[str, Any]:
    """Compact the approved storage tables outside the cleanup transaction."""
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("PostgreSQL is not configured")
    required_confirmation = (
        FULL_COMPACTION_CONFIRMATION_TEXT if full else COMPACTION_CONFIRMATION_TEXT
    )
    if confirmation != required_confirmation:
        raise CleanupConfirmationError(
            f"Type exactly: {required_confirmation}"
        )

    before = get_storage_cleanup_preview()
    if preview_hash != before["preview_hash"]:
        raise CleanupPreviewStaleError(
            "The preview is stale. Refresh the preview before compaction."
        )

    compacted_tables = compact_postgres_tables(
        ("client_data", "client_data_history", "data_audit_log", "upload_validation_plans"),
        full=bool(full),
    )
    try:
        after = get_storage_cleanup_preview()
    except Exception:
        after = None
    post_verification = _post_optimization_verification(before, after)
    return {
        "mode": "compacted",
        "operator": (operator or "Unknown administrator")[:200],
        "full": bool(full),
        "tables": compacted_tables,
        "before": before,
        "after": after,
        "post_verification": post_verification,
        "requires_review": post_verification["status"] != "passed",
        "safety": [
            "Only allow-listed transaction, history, audit, and validation-plan tables were compacted.",
            "No rows were deleted by this operation.",
            "VACUUM FULL may lock and rewrite tables; run it only during an approved maintenance window.",
        ],
    }


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

        deleted_counts["upload_validation_plans"] = _delete_count(
            """
            DELETE FROM public.upload_validation_plans
            WHERE status IN ('applied', 'expired', 'superseded')
               OR expires_at < CURRENT_TIMESTAMP
            """
        )

        # These are all rebuildable transaction/report copies. Delete them
        # before retired client_data so their foreign-key references do not
        # unnecessarily protect rows already marked for removal.
        for table_name in (
            "cash_bank_transaction_classifications",
            "cash_bank_summary_read_models",
            "cashflow_ledger_rollup_read_models",
            "cashflow_prepared_lines",
            "cashflow_prepared_rollups",
        ):
            deleted_counts[f"cashflow_legacy_{table_name}"] = _delete_count(
                f"DELETE FROM public.{table_name}"
            )

        deleted_counts["retired_client_data"] = _delete_count(
            """
            DELETE FROM public.client_data cd
            WHERE (cd.is_deleted = TRUE OR cd.change_detection_status = 'deleted')
              AND NOT EXISTS (SELECT 1 FROM public.verification_details vd WHERE vd.client_data_id = cd.id)
              AND NOT EXISTS (SELECT 1 FROM public.voucher_line_documents vld WHERE vld.client_data_id = cd.id)
              AND NOT EXISTS (SELECT 1 FROM public.voucher_line_physical_bill_items vpbbi WHERE vpbbi.source_document_row_id = cd.id)
              AND NOT EXISTS (SELECT 1 FROM public.cash_bank_transaction_classifications c WHERE c.client_data_id = cd.id)
              AND NOT EXISTS (SELECT 1 FROM public.cashflow_prepared_lines p WHERE p.source_client_data_id = cd.id)
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
            "Validation plans that were applied, expired, superseded, or past expiry were removed.",
            "VACUUM/compaction was not run; use the separate administrator compaction action to reclaim physical pages.",
        ],
        "preview": final_preview,
    }


def apply_validation_plan_cleanup(
    preview_hash: str,
    operator: str,
    confirmation: str,
) -> Dict[str, Any]:
    """Remove only completed or expired validation plans; preserve all logs and data."""
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("PostgreSQL is not configured")
    if confirmation != VALIDATION_PLAN_CLEANUP_CONFIRMATION:
        raise CleanupConfirmationError(
            f"Type exactly: {VALIDATION_PLAN_CLEANUP_CONFIRMATION}"
        )

    fresh_preview = get_storage_cleanup_preview()
    if preview_hash != fresh_preview["preview_hash"]:
        raise CleanupPreviewStaleError(
            "The preview is stale. Refresh the preview and submit the new confirmation."
        )

    protected_before = fresh_preview["protected"]
    clean_operator = (operator or "Unknown administrator")[:200]
    deleted_counts: Dict[str, int] = {}

    with pg_transaction():
        execute_write_pg("SET LOCAL statement_timeout = '120s'")
        execute_write_pg(
            """
            INSERT INTO storage_cleanup_runs
                (operator, scope, status, deleted_counts, preview_hash)
            VALUES (%s, %s, 'started', %s, %s)
            """,
            (clean_operator, "validation_plans_only", "{}", preview_hash),
        )
        deleted_counts["upload_validation_plans"] = _delete_count(
            """
            DELETE FROM public.upload_validation_plans
            WHERE status IN ('applied', 'expired', 'superseded')
               OR expires_at < CURRENT_TIMESTAMP
            """
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
        final_preview = None

    return {
        "mode": "validation_plans_only",
        "deleted_counts": deleted_counts,
        "protected": protected_after,
        "safety": [
            "Only applied, expired, superseded, or past-expiry validation plans were removed.",
            "Canonical client_data, audit logs, derived occurrences, and upload metadata were not deleted.",
            "VACUUM/compaction was not run; use the separate administrator compaction action.",
        ],
        "preview": final_preview,
    }
