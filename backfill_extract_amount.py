"""
Standalone backfill script for the extract_columns_from_row() fix (2026-08-09).

extract_columns_from_row() previously used a single shared `break` for both its
Numeric and Date branches, so on every template that lists a Date column before
its Amount column (i.e. nearly all of them, including the Transaction Report
cashbook template) the loop exited the moment it found the date -- before it
ever reached a Numeric column. extract_amount ended up NULL on every real row,
regardless of template, which left the Physical Bill evidence-type gate's
Amount-match check permanently inert (nothing to compare a bill's amount
against).

This script recomputes extract_amount for every already-imported row using the
fixed extraction logic. extract_date is deliberately left untouched -- it was
already correctly populated on the vast majority of rows by the old code (the
bug only ever blocked amount, never date, given every template's real column
order), and guessing a delimiter/order to re-parse the handful of rows that did
fail would risk overwriting already-correct historical dates with a wrong guess.

Deliberately scans EVERY client_data row with a NULL extract_amount, regardless
of is_deleted/change_detection_status -- confirmed with the user, 2026-08-09.
Unlike backfill_rate.py (which only cares about the current live dataset used
for future change-detection comparisons), a voucher line's own client_data row
is referenced by a fixed foreign key (verification_details.client_data_id) that
stays valid even after is_deleted=1 is set on it by a later whole-dataset
soft-delete/re-import-replace (routers/data.py) -- confirmed live that real,
currently-open voucher lines routinely point at such rows. Restricting to
is_deleted=0 would leave most real, still-referenced vouchers unfixed.

Not wired into any API endpoint -- run manually on demand:
    python backfill_extract_amount.py            (dry-run report only, no writes)
    python backfill_extract_amount.py --run      (actually perform the backfill)

PostgreSQL precision preparation (separate from the legacy SQLite backfill):
    python backfill_extract_amount.py --postgres --output <external-report.json>
This audits every client/template, including non-NULL and retired rows, in one
read-only snapshot. It never changes PostgreSQL data or schema; --postgres --run
is explicitly rejected until a separately approved live repair is implemented.

Phase 2 can build an offline plan with --repair-plan <assessment.json>. The
rehearsal_step_pg API accepts only a caller-owned, isolated local PostgreSQL
connection; there is no CLI or application-pool path to PostgreSQL writes.
"""
import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from psycopg2.extras import RealDictCursor, execute_values

from database import (execute_query, execute_write, log_audit, log_data_audit,
                      run_read_snapshot_pg, run_isolated_pg_transaction, maintenance_storage_budget)
from routers.import_export import extract_columns_from_row, find_named_column


class UnsupportedAmountPrecision(ValueError):
    """The existing float-compatible helper cannot preserve the source value."""


def expected_helper_amount(row_data, template_cols):
    """Reuse application selection rules and reject unsafe numeric conversions.

    The second pass identifies the selected original value, solely to check for
    precision loss. It must agree with the application's authoritative helper.
    Dates and raw values are never changed by this assessment.
    """
    if not isinstance(row_data, dict):
        raise ValueError("Source row must be a JSON object")
    amount, _ = extract_columns_from_row(row_data, template_cols)
    if amount is None:
        # Zero/blank amounts are legitimate; malformed numeric source cells are
        # review blockers rather than permission to invent a replacement value.
        for column in template_cols:
            value = row_data.get(column["column_name"])
            if column["data_type"] == "Numeric" and value is not None and value != "":
                try:
                    if not math.isfinite(float(value)):
                        raise ValueError("Non-finite source amount")
                except (TypeError, ValueError, OverflowError) as error:
                    raise ValueError("Invalid numeric source cell") from error
        return None
    if not math.isfinite(amount):
        raise ValueError("Non-finite source amount")
    preferred = find_named_column(template_cols, "Amount")
    candidates = ([preferred] if preferred and preferred["data_type"] == "Numeric" else []) + [
        col for col in template_cols if col["data_type"] == "Numeric" and col is not preferred
    ]
    for column in candidates:
        value = row_data.get(column["column_name"])
        if value is None or value == "":
            continue
        try:
            selected = float(value)
        except (ValueError, TypeError, OverflowError):
            continue
        if selected == 0:
            continue
        if selected != amount:
            raise ValueError("Source selection disagrees with application extraction")
        try:
            source_number = Decimal(str(value).strip())
        except InvalidOperation as error:
            raise ValueError("Selected source is not a decimal number") from error
        if source_number != Decimal(str(amount)):
            raise UnsupportedAmountPrecision("Selected source exceeds float-compatible helper precision")
        return amount
    raise ValueError("Selected amount source could not be established")


def _digest_records(records):
    return hashlib.sha256(json.dumps(records, sort_keys=True, default=str,
                                     separators=(",", ":")).encode()).hexdigest()


def _audit_postgres_snapshot(conn, page_size, *, rehearsal=False, candidate=False, previous=False):
    """All metadata and source rows share the caller's read-only snapshot."""
    def read(sql, params=None):
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(sql, params or ())
            return [dict(row) for row in cursor.fetchall()]

    storage = read("""SELECT pg_database_size(current_database()) AS database_bytes,
        pg_total_relation_size('public.client_data') AS table_total_bytes,
        pg_table_size('public.client_data') AS table_bytes,
        pg_indexes_size('public.client_data') AS index_bytes,
        current_setting('transaction_read_only') AS transaction_read_only,
        current_setting('transaction_isolation') AS transaction_isolation""")[0]
    if (not rehearsal and storage["transaction_read_only"] != "on") or storage["transaction_isolation"] != "repeatable read":
        raise RuntimeError("Precision audit requires an enforced read-only repeatable snapshot")
    helper_columns = read("""SELECT data_type, udt_name, is_nullable, column_default
        FROM information_schema.columns WHERE table_schema='public'
        AND table_name='client_data' AND column_name='extract_amount'""")
    if len(helper_columns) != 1:
        raise RuntimeError("Expected one canonical extract_amount column")
    mappings = read("""SELECT t.id AS template_id, t.template_name, t.is_active AS template_active,
        m.id AS column_id, m.column_name, m.data_type, p.sort_order
        FROM templates t LEFT JOIN template_column_mappings p ON p.template_id=t.id
        LEFT JOIN master_columns m ON m.id=p.column_id AND m.is_active=TRUE
        ORDER BY t.id,p.sort_order,m.id""")
    columns_by_template = {}
    names = {}
    for item in mappings:
        names[item["template_id"]] = item["template_name"]
        if item["column_id"] is not None:
            columns_by_template.setdefault(item["template_id"], []).append({
                "id": item["column_id"], "column_name": item["column_name"], "data_type": item["data_type"],
            })
    dependencies = read("""SELECT pg_describe_object(d.classid,d.objid,d.objsubid) AS dependent_object,
        d.deptype FROM pg_depend d JOIN pg_attribute a ON a.attrelid=d.refobjid
        AND a.attnum=d.refobjsubid WHERE a.attrelid='public.client_data'::regclass
        AND a.attname IN ('extract_amount','extract_amount_repair','extract_amount_previous')
        ORDER BY dependent_object,d.deptype""")
    # Include column attributes and non-index dependencies that a rename must not
    # silently lose. Temporary rehearsal helpers are excluded from this seal.
    schema_guard = read("""SELECT json_build_object(
        'columns', (SELECT json_agg(json_build_array(a.attname,
            CASE WHEN a.attname='extract_amount' THEN 'amount-helper' ELSE format_type(a.atttypid,a.atttypmod) END,
            a.attnotnull,pg_get_expr(d.adbin,d.adrelid),a.attidentity,a.attgenerated) ORDER BY a.attname)
            FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attrelid='public.client_data'::regclass AND a.attnum>0 AND NOT a.attisdropped
            AND a.attname NOT IN ('extract_amount_repair','extract_amount_previous')),
        'constraints', (SELECT json_agg(pg_get_constraintdef(oid) ORDER BY conname)
            FROM pg_constraint WHERE conrelid='public.client_data'::regclass),
        'triggers', (SELECT json_agg(pg_get_triggerdef(oid) ORDER BY tgname)
            FROM pg_trigger WHERE tgrelid='public.client_data'::regclass AND NOT tgisinternal),
        'policies', (SELECT json_agg(to_jsonb(p) ORDER BY policyname)
            FROM pg_policies p WHERE schemaname='public' AND tablename='client_data'),
        'grants', (SELECT json_agg(to_jsonb(g) ORDER BY grantee,privilege_type,column_name)
            FROM information_schema.column_privileges g WHERE table_schema='public' AND table_name='client_data'
            AND column_name NOT IN ('extract_amount_repair','extract_amount_previous'))
        ) AS repair_schema_guard""")[0]["repair_schema_guard"]
    indexes = read("""SELECT ci.relname AS index_name, i.indisprimary, i.indisunique,
        i.indisvalid, i.indisready, pg_get_indexdef(i.indexrelid) AS definition,
        pg_relation_size(i.indexrelid) AS index_bytes FROM pg_index i
        JOIN pg_class ci ON ci.oid=i.indexrelid WHERE i.indrelid='public.client_data'::regclass
        ORDER BY ci.relname""")
    duplicates = read("""SELECT count(*) AS duplicate_groups FROM (
        SELECT client_id,template_id,source_identity FROM client_data
        WHERE NOT is_deleted AND change_detection_status<>'deleted'
        AND source_identity IS NOT NULL AND source_identity<>''
        GROUP BY client_id,template_id,source_identity HAVING count(*)>1) groups""")[0]
    copy_counts = read("""SELECT 'cash_bank_transaction_classifications' AS table_name,count(*) AS rows
        FROM cash_bank_transaction_classifications UNION ALL SELECT 'cashflow_prepared_lines',count(*)
        FROM cashflow_prepared_lines UNION ALL SELECT 'cashflow_prepared_rollups',count(*)
        FROM cashflow_prepared_rollups UNION ALL SELECT 'cash_bank_summary_read_models',count(*)
        FROM cash_bank_summary_read_models UNION ALL SELECT 'cashflow_ledger_rollup_read_models',count(*)
        FROM cashflow_ledger_rollup_read_models UNION ALL SELECT 'upload_validation_plans',count(*)
        FROM upload_validation_plans""")

    datasets = {}
    fields = ("rows_scanned", "active_rows", "retired_rows", "matching_amounts", "different_amounts",
              "missing_amounts", "no_source_amount", "unresolved_existing_amount", "invalid_source_rows",
              "unsupported_source_precision_rows", "invalid_stored_rows", "binary_storage_difference_rows",
              "missing_active_identity_rows", "candidate_mismatch_rows")
    amount_column = "extract_amount_previous" if previous else "extract_amount"
    candidate_sql = ",cd.extract_amount_repair AS candidate_amount" if candidate else ""
    with conn.cursor(name="amount_precision_audit", cursor_factory=RealDictCursor) as cursor:
        cursor.execute(f"""SELECT cd.id,cd.client_id,cd.template_id,cd.row_data,cd.{amount_column} AS extract_amount,
            cd.{amount_column}::double precision AS binary_stored_amount,
            cd.is_deleted,cd.change_detection_status,cd.source_identity,
            md5(cd.row_data::text) AS raw_fingerprint,
            md5((to_jsonb(cd)-'extract_amount'-'extract_amount_repair'-'extract_amount_previous')::text) AS protected_fingerprint
            {candidate_sql} FROM client_data cd ORDER BY cd.id""")
        while True:
            page = cursor.fetchmany(page_size)
            if not page:
                break
            for row in page:
                key = (row["client_id"], row["template_id"])
                if key not in datasets:
                    datasets[key] = {**dict.fromkeys(fields, 0), "client_id": key[0], "template_id": key[1],
                        "template_name": names.get(key[1]), "examples": [],
                        "_raw_digest": hashlib.sha256(), "_protected_digest": hashlib.sha256(),
                        "_helper_digest": hashlib.sha256()}
                counts = datasets[key]
                counts["rows_scanned"] += 1
                active = not row["is_deleted"] and row["change_detection_status"] != "deleted"
                counts["active_rows" if active else "retired_rows"] += 1
                if active and not row["source_identity"]:
                    counts["missing_active_identity_rows"] += 1
                for name, fingerprint in (("_raw_digest", "raw_fingerprint"),
                                          ("_protected_digest", "protected_fingerprint")):
                    counts[name].update(f"{row['id']}:{row[fingerprint]}\n".encode())
                counts["_helper_digest"].update(f"{row['id']}:{row['binary_stored_amount']!r}\n".encode())
                try:
                    cols = columns_by_template.get(key[1])
                    if not cols:
                        raise ValueError("No active mapped columns for this source template")
                    data = json.loads(row["row_data"]) if isinstance(row["row_data"], str) else row["row_data"]
                    expected = expected_helper_amount(data, cols)
                except (ValueError, TypeError, OverflowError) as error:
                    kind = "unsupported_source_precision_rows" if isinstance(error, UnsupportedAmountPrecision) else "invalid_source_rows"
                    counts[kind] += 1
                    if len(counts["examples"]) < 5:
                        counts["examples"].append({"row_id": row["id"], "issue": kind})
                    continue
                if candidate and row["candidate_amount"] != expected:
                    counts["candidate_mismatch_rows"] += 1
                stored, binary_stored = row["extract_amount"], row["binary_stored_amount"]
                if stored is not None and (not math.isfinite(stored) or not math.isfinite(binary_stored)):
                    counts["invalid_stored_rows"] += 1
                    continue
                if expected is None:
                    counts["no_source_amount"] += 1
                    if stored is not None:
                        counts["unresolved_existing_amount"] += 1
                    continue
                if stored is None:
                    counts["missing_amounts"] += 1
                elif stored != expected:
                    counts["different_amounts"] += 1
                else:
                    counts["matching_amounts"] += 1
                if binary_stored is not None and binary_stored != expected:
                    counts["binary_storage_difference_rows"] += 1
                if stored != expected and len(counts["examples"]) < 5:
                    counts["examples"].append({"row_id": row["id"], "stored_amount": stored,
                                                "expected_amount": expected})

    summaries = []
    for key, counts in sorted(datasets.items()):
        counts["raw_sha256"] = counts.pop("_raw_digest").hexdigest()
        counts["protected_fields_sha256"] = counts.pop("_protected_digest").hexdigest()
        counts["helper_values_sha256"] = counts.pop("_helper_digest").hexdigest()
        summaries.append(counts)
    totals = {field: sum(item[field] for item in summaries) for field in fields}
    helper_type = helper_columns[0]["data_type"]
    rewrite_estimate = int(storage["database_bytes"]) + int(storage["table_total_bytes"]) + 4 * totals["rows_scanned"]
    problems = []
    for field in ("invalid_source_rows", "unsupported_source_precision_rows", "invalid_stored_rows",
                  "unresolved_existing_amount", "missing_active_identity_rows"):
        if totals[field]:
            problems.append(f"{field}: {totals[field]}")
    if int(duplicates["duplicate_groups"]):
        problems.append(f"duplicate_identity_groups: {duplicates['duplicate_groups']}")
    protected_index = next((item for item in indexes if item["index_name"] == "idx_client_data_active_identity_unique"), None)
    if not protected_index or not all(protected_index[field] for field in ("indisunique", "indisvalid", "indisready")):
        problems.append("Active identity uniqueness index is missing or invalid")
    if helper_type not in {"real", "double precision"}:
        problems.append("Helper type requires a separate compatibility review")
    return {
        "phase": "isolated_rehearsal" if rehearsal else "precision_preparation_only", "read_only": not rehearsal,
        "assessed_at_utc": datetime.now(timezone.utc).isoformat(),
        "live_apply_available": False, "backup_restore_verified": False, "safe_peak_verified": False,
        "storage": storage, "helper_column": helper_columns[0], "dependencies": dependencies,
        "repair_schema_guard": schema_guard,
        "indexes": indexes, "mapping_sha256": _digest_records(mappings),
        "duplicate_identity_groups": int(duplicates["duplicate_groups"]),
        "copy_table_counts": {item["table_name"]: int(item["rows"]) for item in copy_counts},
        "datasets": summaries, "totals": totals, "source_or_integrity_blockers": problems,
        "repair_preparation": {
            "target_type": "DOUBLE PRECISION", "only_allowed_field_change": "extract_amount",
            "rebuild_from": "preserved raw values using existing application selection rules",
            "schema_change_required": helper_type != "double precision",
            "approximate_direct_rewrite_peak_bytes": rewrite_estimate if helper_type == "real" else None,
            "direct_rewrite_exceeds_400mb_allowance": helper_type == "real" and rewrite_estimate >= 400_000_000,
            "estimate_limit": "Planning allowance only; excludes WAL/temp overhead and is not a proven peak.",
            "bounded_staging_peak_bytes": None,
            "dependency_review_required": bool(dependencies),
            "proposed_method": "Assess bounded helper-column staging and one validated atomic switch; never copy transaction rows.",
            "required_before_live": ["fresh backup and restoration proof", "dependency and complete storage-headroom review",
                "tested interruption/recovery and write-pause procedure", "separate live maintenance approval"],
        },
    }


def audit_postgres(page_size=2000):
    """Audit only; never initializes schemas, writes audits, or invokes SQLite."""
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 10000:
        raise ValueError("page_size must be an integer between 1 and 10000")
    return run_read_snapshot_pg(lambda conn: _audit_postgres_snapshot(conn, page_size))


def _repair_binding(assessment, *, include_helper=True):
    """Bind a plan to source, mappings, protected fields and schema, not size/time."""
    fields = ["client_id", "template_id", "rows_scanned", "active_rows", "retired_rows",
              "raw_sha256", "protected_fields_sha256"]
    if include_helper:
        fields.append("helper_values_sha256")
    # A restored database deliberately has another name. Keep grantor, grantee,
    # schema, table, column, privilege and grantability intact; only the catalog
    # label is environmental. The original assessment retains the full record.
    schema = dict(assessment["repair_schema_guard"])
    if schema.get("grants") is not None:
        schema["grants"] = [{key: value for key, value in grant.items() if key != "table_catalog"}
                            for grant in schema["grants"]]
    return {"datasets": [{field: item[field] for field in fields} for item in assessment["datasets"]],
            "mapping_sha256": assessment["mapping_sha256"],
            "schema": schema,
            "indexes": [{key: value for key, value in index.items() if key != "index_bytes"}
                        for index in assessment["indexes"]],
            "copy_table_counts": assessment["copy_table_counts"]}


def build_repair_plan(assessment):
    """Produce compact rehearsal instructions; never certify backups or peak size."""
    if assessment.get("read_only") is not True or not assessment.get("repair_schema_guard"):
        raise ValueError("A fresh read-only assessment with schema safeguards is required")
    if assessment["helper_column"] != {"data_type": "real", "udt_name": "float4",
                                       "is_nullable": "YES", "column_default": None}:
        raise ValueError("Rehearsal requires the reviewed nullable REAL helper without a default")
    if assessment["dependencies"] or assessment["source_or_integrity_blockers"]:
        raise ValueError("Source or helper dependencies require review before rehearsal")
    if assessment["repair_schema_guard"].get("triggers"):
        raise ValueError("User triggers require explicit staging side-effect review before rehearsal")
    if any(assessment["copy_table_counts"].values()):
        raise ValueError("Transaction-copy tables and validation plans must be empty")
    plan = {"format_version": 2, "purpose": "isolated_local_rehearsal_only",
            "target_type": "DOUBLE PRECISION", "binding": _repair_binding(assessment),
            "live_apply_available": False,
            "backup_requirements": ["Fresh complete logical dump including schema, data and sequence state",
                "Inventory required roles/extensions and verify restoration privileges",
                "Two protected external copies with matching SHA-256",
                "Restore into a separate local precision_rehearsal_* database",
                "Verify row fingerprints, identities, configuration, constraints and sequence state"],
            "live_prerequisites": ["Measured full-size storage rehearsal plus recovery headroom",
                "Restoration proof and fresh independent cashflow/7-sheet export comparison",
                "Controlled application/write pause and fresh dependency review",
                "Separate live maintenance implementation and approval"],
            "recovery": "Old helper remains active until atomic switch; retain previous helper for verified rollback.",
            "storage_limit_bytes": 400_000_000, "measured_peak_bytes": None,
            "backup_restore_verified": False, "cashflow_parity_verified": False}
    plan["plan_sha256"] = _digest_records(plan)
    return plan


def _validate_repair_plan(plan):
    body = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if (plan.get("purpose") != "isolated_local_rehearsal_only" or plan.get("format_version") != 2
            or plan.get("live_apply_available") is not False
            or plan.get("storage_limit_bytes") != 400_000_000
            or plan.get("plan_sha256") != _digest_records(body)):
        raise ValueError("Invalid or modified rehearsal plan")


def rehearsal_step_pg(conn, plan, action, *, after_id=0, batch_size=200,
                      batch_growth_allowance_bytes=8_000_000, recovery_reserve_bytes=20_000_000):
    """Exercise resumable helper maintenance on an isolated local restore only.

    Each step is one transaction, without retry. On an uncertain commit, inspect
    status and restart scanning at ID zero with the SAME plan. Updates use IS
    DISTINCT FROM, so committed batches are harmless no-ops when rescanned.
    Physical growth checks are rehearsal measurements, never a live guarantee.
    """
    _validate_repair_plan(plan)
    if action not in {"prepare", "batch", "switch", "rollback", "status"}:
        raise ValueError("Unknown rehearsal action")
    if (isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 2000
            or isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0):
        raise ValueError("Invalid rehearsal batch size or continuation ID")
    maintenance_storage_budget(0, batch_growth_allowance_bytes, recovery_reserve_bytes)
    if batch_growth_allowance_bytes < 1 or recovery_reserve_bytes < 1:
        raise ValueError("Positive batch allowance and recovery reserve are required")
    marker = "amount-repair rehearsal:" + plan["plan_sha256"]

    def step(active):
        def query(sql, params=()):
            with active.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(sql, params)
                return [dict(row) for row in cursor.fetchall()]

        def execute(sql, params=()):
            with active.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.rowcount

        def allocated():
            return int(query("SELECT pg_database_size(current_database()) AS database_bytes")[0]["database_bytes"])

        # Blocks competing writes for this step. Between-step changes are caught
        # by the complete source/schema seal before either rename operation.
        lock_mode = "ACCESS EXCLUSIVE" if action in {"prepare", "switch", "rollback"} else "SHARE ROW EXCLUSIVE"
        execute("LOCK TABLE public.client_data IN " + lock_mode + " MODE")
        execute("LOCK TABLE public.templates,public.master_columns,public.template_column_mappings IN SHARE MODE")
        before_bytes = allocated()
        budget = maintenance_storage_budget(before_bytes, batch_growth_allowance_bytes, recovery_reserve_bytes)
        if action != "status" and budget["blocked"]:
            raise RuntimeError("Storage headroom is insufficient; existing helper remains unchanged")
        columns = {row["name"]: row for row in query("""SELECT a.attname AS name,
            format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull AS not_null,
            pg_get_expr(d.adbin,d.adrelid) AS default_value,
            col_description(a.attrelid,a.attnum) AS comment
            FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attrelid='public.client_data'::regclass AND NOT a.attisdropped
            AND a.attname IN ('extract_amount','extract_amount_repair','extract_amount_previous')""")}
        old, staged, previous = (columns.get(name) for name in
                                ("extract_amount", "extract_amount_repair", "extract_amount_previous"))
        original = bool(old and old["type"] == "real" and not old["not_null"] and old["default_value"] is None)
        staged_ok = bool(staged and staged["type"] == "double precision" and not staged["not_null"]
                         and staged["default_value"] is None and staged["comment"] == marker)
        switched = bool(old and old["type"] == "double precision" and not old["not_null"]
                        and old["default_value"] is None and old["comment"] == marker
                        and previous and previous["type"] == "real" and not previous["not_null"]
                        and previous["default_value"] is None and staged is None)
        if not ((original and previous is None and (staged is None or staged_ok)) or switched):
            raise RuntimeError("Unexpected helper schema or unowned staging column; manual review required")

        def validate_source(*, with_candidate=False, old_previous=False, include_helper=True):
            assessment = _audit_postgres_snapshot(active, 2000, rehearsal=True,
                                                 candidate=with_candidate, previous=old_previous)
            expected_binding = plan["binding"]
            if not include_helper:
                expected_binding = {**expected_binding, "datasets": [
                    {key: value for key, value in row.items() if key != "helper_values_sha256"}
                    for row in expected_binding["datasets"]]}
            if (assessment["source_or_integrity_blockers"] or assessment["dependencies"]
                    or _repair_binding(assessment, include_helper=include_helper) != expected_binding):
                raise RuntimeError("Source, helper, mappings, indexes or schema changed since the plan")
            if with_candidate and assessment["totals"]["candidate_mismatch_rows"]:
                raise RuntimeError("Staging is incomplete or incorrect; original helper remains active")
            return assessment

        result = {"action": action, "live_apply_available": False, "plan_sha256": plan["plan_sha256"],
                  "state": "switched" if switched else "staging" if staged_ok else "not_prepared"}
        if action == "prepare":
            if switched:
                raise RuntimeError("Already switched; inspect status instead of preparing again")
            validate_source()
            if staged is None:
                execute("ALTER TABLE public.client_data ADD COLUMN extract_amount_repair DOUBLE PRECISION")
                execute("COMMENT ON COLUMN public.client_data.extract_amount_repair IS %s", (marker,))
            result["state"] = "staging"
        elif action == "batch":
            if not original or not staged_ok:
                raise RuntimeError("Prepare staging before processing batches")
            mappings = query("""SELECT t.id AS template_id, t.template_name, t.is_active AS template_active,
                m.id AS column_id, m.column_name, m.data_type, p.sort_order
                FROM templates t LEFT JOIN template_column_mappings p ON p.template_id=t.id
                LEFT JOIN master_columns m ON m.id=p.column_id AND m.is_active=TRUE
                ORDER BY t.id,p.sort_order,m.id""")
            if _digest_records(mappings) != plan["binding"]["mapping_sha256"]:
                raise RuntimeError("Template mappings changed; refuse further staging")
            by_template = {}
            for item in mappings:
                if item["column_id"] is not None:
                    by_template.setdefault(item["template_id"], []).append({
                        "id": item["column_id"], "column_name": item["column_name"], "data_type": item["data_type"]})
            rows = query("""SELECT id,template_id,row_data FROM public.client_data
                WHERE id>%s ORDER BY id LIMIT %s FOR UPDATE""", (after_id, batch_size))
            amounts = []
            for row in rows:
                data = json.loads(row["row_data"]) if isinstance(row["row_data"], str) else row["row_data"]
                expected = expected_helper_amount(data, by_template[row["template_id"]])
                amounts.append((row["id"], expected))
            updated = 0
            if amounts:
                # One bounded statement and one outer commit: avoid a network
                # round trip for every source row and validate the entire batch
                # before issuing writes. Explicit casts also cover NULL values.
                with active.cursor() as cursor:
                    execute_values(cursor, """UPDATE public.client_data AS cd
                        SET extract_amount_repair=amounts.amount
                        FROM (VALUES %s) AS amounts(id,amount)
                        WHERE cd.id=amounts.id AND cd.extract_amount_repair IS DISTINCT FROM amounts.amount""",
                        amounts, template="(%s::bigint,%s::double precision)", page_size=len(amounts))
                    updated = cursor.rowcount
            result.update(rows_scanned=len(rows), rows_updated=updated,
                          after_id=rows[-1]["id"] if rows else after_id, scan_complete=not rows)
        elif action == "switch":
            if switched:
                validate_source(old_previous=True)
            else:
                if not staged_ok:
                    raise RuntimeError("Prepare and validate staging before switching")
                validate_source(with_candidate=True)
                execute("ALTER TABLE public.client_data RENAME COLUMN extract_amount TO extract_amount_previous")
                execute("ALTER TABLE public.client_data RENAME COLUMN extract_amount_repair TO extract_amount")
            checked = validate_source(include_helper=False)
            if any(checked["totals"][key] for key in
                   ("different_amounts", "missing_amounts", "binary_storage_difference_rows")):
                raise RuntimeError("Post-switch amount verification failed; transaction must roll back")
            result["state"] = "switched"
        elif action == "rollback":
            if not switched:
                raise RuntimeError("Verified rollback requires a switched helper and its retained predecessor")
            validate_source(old_previous=True)
            execute("ALTER TABLE public.client_data RENAME COLUMN extract_amount TO extract_amount_repair")
            execute("ALTER TABLE public.client_data RENAME COLUMN extract_amount_previous TO extract_amount")
            validate_source()
            result["state"] = "staging"
        else:
            validate_source(old_previous=switched)
        after_bytes = allocated()
        if action != "status" and (after_bytes - before_bytes > batch_growth_allowance_bytes
                or maintenance_storage_budget(after_bytes, 0, recovery_reserve_bytes)["blocked"]):
            raise RuntimeError("Observed growth exceeded rehearsal allowance; rollback may retain allocated pages")
        result.update(database_bytes_before=before_bytes, database_bytes_after=after_bytes,
                      allocated_growth_bytes=after_bytes-before_bytes, storage_budget=budget,
                      physical_peak_proven=False, backup_restore_verified=False, cashflow_parity_verified=False)
        return result

    return run_isolated_pg_transaction(conn, step)


def get_active_templates() -> list:
    return execute_query("SELECT id, template_name FROM templates WHERE is_active = 1")


def get_template_columns(template_id: int) -> list:
    """Same column-fetch shape used throughout routers/import_export.py."""
    return execute_query(
        """
        SELECT m.id, m.column_name, m.data_type
        FROM template_column_mappings p
        JOIN master_columns m ON p.column_id = m.id
        WHERE p.template_id = ? AND m.is_active = 1
        ORDER BY p.sort_order ASC
        """,
        (template_id,)
    )


def scan(dry_run: bool) -> dict:
    """
    Walks every active template's qualifying client_data rows (extract_amount IS
    NULL) and either reports (dry_run=True) or performs (dry_run=False) the
    backfill. Returns per-template counts plus a grand total.
    """
    report = {"templates": [], "grand_total": {
        "total_candidates": 0,
        "backfilled": 0,
        "skipped_no_numeric_value": 0,
        "skipped_no_columns": 0,
    }}

    for template in get_active_templates():
        template_id = template['id']
        template_name = template['template_name']
        counts = {
            "template_id": template_id,
            "template_name": template_name,
            "total_candidates": 0,
            "backfilled": 0,
            "skipped_no_numeric_value": 0,
            "skipped_no_columns": 0,
        }

        template_cols = get_template_columns(template_id)
        if not template_cols:
            report["templates"].append(counts)
            continue  # this template has no active columns at all -- nothing to backfill

        rows = execute_query(
            """
            SELECT id, row_data FROM client_data
            WHERE template_id = ? AND extract_amount IS NULL
            """,
            (template_id,)
        )

        for row in rows:
            counts["total_candidates"] += 1
            try:
                row_data = json.loads(row['row_data']) if isinstance(row['row_data'], str) else row['row_data']
            except Exception:
                counts["skipped_no_numeric_value"] += 1
                continue

            amount, _date_str = extract_columns_from_row(row_data, template_cols)
            if amount is None:
                counts["skipped_no_numeric_value"] += 1
                continue

            counts["backfilled"] += 1
            if not dry_run:
                execute_write(
                    "UPDATE client_data SET extract_amount = ? WHERE id = ?",
                    (amount, row['id'])
                )
                log_data_audit(row['id'], "Analyst", "UPDATE",
                                {"extract_amount": None}, {"extract_amount": amount})

        report["templates"].append(counts)
        for key in report["grand_total"]:
            report["grand_total"][key] += counts[key]

    if not dry_run and report["grand_total"]["backfilled"] > 0:
        log_audit(
            "Analyst", "Backfill", "Upload",
            f"extract_amount backfill: {report['grand_total']['backfilled']} rows backfilled "
            f"across {len(report['templates'])} template(s)"
        )

    return report


def print_report(report: dict, dry_run: bool):
    verb = "Would backfill" if dry_run else "Backfilled"
    print()
    print("=" * 70)
    print(f"extract_amount Backfill Report ({'DRY RUN' if dry_run else 'LIVE RUN'})")
    print("=" * 70)
    for t in report["templates"]:
        print(f"\n{t['template_name']} (template_id={t['template_id']}):")
        print(f"  Rows scanned (amount NULL): {t['total_candidates']}")
        print(f"  No numeric value found:     {t['skipped_no_numeric_value']}")
        print(f"  {verb}:{' ' * (26 - len(verb))}{t['backfilled']}")

    gt = report["grand_total"]
    print("\n" + "-" * 70)
    print("GRAND TOTAL")
    print(f"  Rows scanned (amount NULL): {gt['total_candidates']}")
    print(f"  No numeric value found:     {gt['skipped_no_numeric_value']}")
    print(f"  {verb}:{' ' * (26 - len(verb))}{gt['backfilled']}")
    print("=" * 70)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill extract_amount for historical client_data rows.")
    parser.add_argument("--run", action="store_true", help="SQLite legacy backfill only; unavailable with --postgres.")
    parser.add_argument("--postgres", action="store_true", help="Read-only PostgreSQL precision assessment; no schema/data repair.")
    parser.add_argument("--repair-plan", type=Path, help="Build an offline rehearsal plan from a fresh assessment; no database connection.")
    parser.add_argument("--output", type=Path, help="Save a compact JSON report to a new local/external file; never overwrite.")
    args = parser.parse_args(argv)
    if args.postgres and args.run:
        parser.error("PostgreSQL live repair is disabled; separate implementation, proof and approval are required.")
    if args.repair_plan and (args.postgres or args.run):
        parser.error("--repair-plan is offline and cannot be combined with --postgres or --run.")
    if args.output and (args.output.exists() or not args.output.parent.is_dir()):
        parser.error("Output must be a new file inside an existing directory.")

    if args.postgres or args.repair_plan:
        report = (build_repair_plan(json.loads(args.repair_plan.read_text(encoding="utf-8")))
                  if args.repair_plan else audit_postgres())
        output = json.dumps(report, indent=2, default=str, allow_nan=False)
        if args.output:
            with args.output.open("x", encoding="utf-8") as target:
                target.write(output + "\n")
        print(output)
        return report

    dry_run = not args.run
    result = scan(dry_run=dry_run)
    print_report(result, dry_run)
    if args.output:
        with args.output.open("x", encoding="utf-8") as target:
            target.write(json.dumps(result, indent=2) + "\n")

    if dry_run:
        print("\nDry run only -- no changes written. Re-run with --run to apply.")
    else:
        print("\nBackfill complete.")
    return result


if __name__ == "__main__":
    main()
