"""Standalone, opt-in read-only and local-fixture benchmarks. No app startup.

Run without --live/--local-fixture to print the boundary only. Run pytest for offline safeguards.
Evidence contains timings/counts/hashes, never transactions or credentials.
"""

import argparse
import csv
from contextlib import ExitStack, contextmanager
from datetime import date, datetime
from decimal import Decimal
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import statistics
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.cashflow_fetch.variants import (
    METHODS, decode_rows, materialized_category_sql, transport_sql,
)


class ExperimentError(RuntimeError):
    """A bounded, non-sensitive failure safe to include in compact evidence."""


def blocked(*args, **kwargs):
    raise ExperimentError("Writes, schema setup and extra connections are disabled")


def assert_select(sql):
    """Additional tripwire; PostgreSQL READ ONLY remains the authoritative guard."""
    text = re.sub(r"/\*.*?\*/|--[^\n]*", " ", sql, flags=re.S).strip()
    if not re.match(r"^(SELECT|WITH)\b", text, re.I) or ";" in text:
        raise ExperimentError("Only a single benchmark SELECT is allowed")
    forbidden = r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|COPY|CALL|DO|GRANT|REVOKE|VACUUM|ANALYZE|NEXTVAL|SETVAL|PG_ADVISORY_LOCK)\b"
    if re.search(forbidden, text, re.I):
        raise ExperimentError("Non-read operation refused")


def _portable(value):
    # Float.hex distinguishes signed zero and protects exact native float inputs.
    if type(value).__name__ == "DataFrame":
        return {"dataframe": _portable(value.to_dict(orient="split"))}
    if type(value).__name__ == "Series":
        return {"series": _portable(value.to_dict())}
    if type(value).__name__ in {"NAType", "NaTType"}:
        return None
    if type(value).__module__.startswith("numpy") and hasattr(value, "item"):
        return _portable(value.item())
    if isinstance(value, float):
        return {"float": value.hex()}
    if isinstance(value, (date, datetime, Decimal)):
        return {"type": type(value).__name__, "value": str(value)}
    if isinstance(value, dict):
        return {str(key): _portable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_portable(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, (list, tuple)):
        return [_portable(item) for item in value]
    return value


def fingerprint(value):
    data = json.dumps(_portable(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def assert_equal(expected, actual, label):
    if fingerprint(expected) != fingerprint(actual):
        raise ExperimentError(f"{label} differs; candidate is disqualified")


def business_result(result):
    """Exclude clocks/query durations, preserving all accounting/control fields."""
    fields = ("client_id", "period_start", "period_end", "group_by", "treasury_view",
              "cashflow_report_revision", "configuration", "controls", "blocking_reasons",
              "database_totals", "preview")
    business = {name: result[name] for name in fields}
    business["source"] = {
        "ready": result["source"]["ready"], "quality": result["source"]["quality"],
        "diagnostics": result["source"]["diagnostics"],
    }
    return business


def workbook_cells(reports, result):
    import openpyxl
    content = reports.export_cashflow_xlsx(
        result["preview"], period_start=result["period_start"],
        period_end=result["period_end"], group_by=result["group_by"],
    )
    with BytesIO(content) as stream:
        book = openpyxl.load_workbook(stream, read_only=True, data_only=False)
        try:
            return [(sheet.title, list(sheet.values)) for sheet in book]
        finally:
            book.close()


def replay_query(captured):
    """Exact ordered query replay; no live fallback and no persisted source copy."""
    position = 0

    def replay(sql, params=None, **kwargs):
        nonlocal position
        if kwargs or position >= len(captured):
            raise ExperimentError("Unexpected replay query")
        original_sql, original_params, rows = captured[position]
        assert_equal((original_sql, original_params), (sql, params), "Replay request")
        position += 1
        return rows

    def complete():
        if position != len(captured):
            raise ExperimentError("Incomplete query replay")

    return replay, complete


@contextmanager
def safety_guards(database, reports, sources):
    """Process-local patches; imported application objects are restored on exit."""
    with ExitStack() as stack:
        for module, names in (
            (database, ("execute_write", "execute_write_pg", "log_audit", "get_db",
                        "init_db", "init_db_postgres", "get_raw_conn_pg", "pg_transaction")),
            (reports, ("execute_write_pg", "log_audit", "pg_transaction")),
        ):
            for name in names:
                stack.enter_context(patch.object(module, name, blocked))
        # The canonical report must use its explicit query_fn, not a hidden read
        # helper/default that could escape the supplied snapshot or replay.
        stack.enter_context(patch.object(sources, "execute_query_pg", blocked))
        yield


def validate_arguments(args):
    if not args.live:
        return
    if not args.client_id or args.client_id < 1 or not args.start or not args.end:
        raise ExperimentError("Live mode requires a client and explicit start/end dates")
    try:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    except ValueError as error:
        raise ExperimentError("Use ISO dates") from error
    if start > end or (end - start).days > 366:
        raise ExperimentError("Choose an ordered period no longer than one year")
    if not 2 <= args.repeats <= 3:
        raise ExperimentError("Use two or three bounded comparison rounds")
    if args.output:
        path = Path(args.output).resolve()
        if path.is_relative_to(ROOT) or path.exists() or not path.parent.is_dir():
            raise ExperimentError("Evidence needs a new path outside the repository")


def summarize_runs(runs, failures):
    """Keep the first-run outlier visible; don't sell its median as a warm gain."""
    medians = {
        method: {key: statistics.median(run[key] for run in runs if run["method"] == method)
                 for key in ("report_seconds", "source_query_seconds", "transport_json_equivalent_bytes")}
        for method in METHODS
    }
    baseline_seconds = medians["baseline"]["report_seconds"]
    rounds = sorted({run["round"] for run in runs})
    for method in METHODS[1:]:
        paired = []
        for number in rounds:
            baseline = next(run["report_seconds"] for run in runs if run["round"] == number and run["method"] == "baseline")
            candidate = next(run["report_seconds"] for run in runs if run["round"] == number and run["method"] == method)
            paired.append({"round": number, "improvement_percent": 100 * (baseline - candidate) / baseline})
        medians[method].update({
            "median_report_improvement_percent": 100 * (baseline_seconds - medians[method]["report_seconds"]) / baseline_seconds,
            "paired_round_improvements": paired,
            "faster_in_each_round": all(item["improvement_percent"] > 0 for item in paired),
            "accuracy_qualified": not any(item["method"] == method for item in failures),
        })
    return medians


def run_live(args):
    """Three SELECT methods, alternating order, within enforced read-only snapshots."""
    validate_arguments(args)
    if not args.live:
        raise ExperimentError("Live access must be explicitly requested")
    # Imports alone do not initialize the app/database. Never import main.app.
    import os
    os.environ["PG_POOL_MIN"] = "1"
    os.environ["PG_POOL_MAX"] = "1"
    import database
    from routers import reconciliation as reports
    from services import ledger_report_source as sources

    if database._pg_pool is not None:
        raise ExperimentError("Run this benchmark in its own fresh Python process")
    query = database.execute_query_pg
    evidence = {
        "scope": {"client_id": args.client_id, "period_start": args.start,
                  "period_end": args.end, "group_by": "month", "treasury_view": "combined"},
        "boundary": "read-only standalone process; no production integration",
        "payload_size_note": "JSON-equivalent bytes; NOT measured network traffic",
        "timings_exclude": "login, browser rendering, snapshot setup, proof and payload measurement",
        "comparison_limits": "Sequential laptop tests, not cloud benchmarks. First baseline may include cold/network delay; inspect paired rounds, not only medians. Two/three rounds do not establish a service-level guarantee.",
        "runs": [], "proofs": {}, "failures": [],
    }
    started = perf_counter()
    first_inputs = {}
    full_sections = {"transfers", "customer_payments", "deductions", "review", "drilldown"}

    def build(details):
        return reports._build_cashflow_result(
            args.client_id, args.start, args.end, detail_sections=details,
            group_by="month", treasury_view="combined", force_refresh=True,
        )

    def round_read(conn, order, round_number):
        with conn.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute("SET LOCAL lock_timeout = '3s'")
            cursor.execute("SHOW transaction_read_only")
            if cursor.fetchone()[0] != "on":
                raise ExperimentError("Server did not enforce read-only access")
        records, timed_runs, failures = {}, [], []
        for method in order:
            captured, reads = [], []
            transport_result = None

            def measured(sql, params=None, **kwargs):
                nonlocal transport_result
                if perf_counter() - started > 180:
                    raise ExperimentError("Benchmark time budget exhausted")
                if database._pg_transaction.get() is not conn or kwargs:
                    raise ExperimentError("Query escaped its read-only snapshot")
                assert_select(sql)
                source_query = "WITH database_totals AS" in sql
                candidate = transport_sql(sql, method) if source_query else sql
                assert_select(candidate)
                before = perf_counter()
                rows = query(candidate, params)
                query_seconds = perf_counter() - before
                if source_query:
                    transport_result = rows
                    before = perf_counter()
                    rows = decode_rows(method, rows)
                    decode_seconds = perf_counter() - before
                else:
                    decode_seconds = 0.0
                reads.append({"kind": "source" if source_query else "configuration_or_client",
                              "query_seconds": query_seconds, "decode_seconds": decode_seconds})
                captured.append((sql, params, rows))
                return rows

            with patch.object(reports, "execute_query_pg", measured):
                before = perf_counter()
                result = build({"summary"})
                elapsed = perf_counter() - before
            if transport_result is None or len(reads) != 3:
                raise ExperimentError("Canonical report/query contract changed")
            payload_bytes = len(json.dumps(transport_result, default=str,
                                          separators=(",", ":")).encode())
            row = {"round": round_number, "method": method, "report_seconds": elapsed,
                   "query_seconds": sum(item["query_seconds"] for item in reads),
                   "decode_seconds": sum(item["decode_seconds"] for item in reads),
                   "source_query_seconds": next(item["query_seconds"] for item in reads if item["kind"] == "source"),
                   "transport_json_equivalent_bytes": payload_bytes}
            timed_runs.append(row)
            records[method] = (captured, business_result(result))
            print(json.dumps({"progress": "timed", **row}), flush=True)
        baseline_inputs, baseline_result = records["baseline"]
        baseline_source = next(rows for sql, _, rows in baseline_inputs if "WITH database_totals AS" in sql)
        for method, (inputs, result) in records.items():
            try:
                candidate_source = next(rows for sql, _, rows in inputs if "WITH database_totals AS" in sql)
                assert_equal(baseline_source, candidate_source, "Ordered typed source rows")
                assert_equal(baseline_result, result, "Summary and completeness controls")
            except ExperimentError as error:
                failures.append({"method": method, "round": round_number, "reason": str(error)})
        return records, timed_runs, failures

    try:
        with safety_guards(database, reports, sources):
            for number in range(1, args.repeats + 1):
                order = METHODS if number % 2 else tuple(reversed(METHODS))
                records, timed_runs, failures = database.run_read_snapshot_pg(
                    lambda conn: round_read(conn, order, number)
                )
                # Publish only completed snapshots. The shared read helper can
                # retry after disconnect; discard that attempt's partial metrics.
                evidence["runs"].extend(timed_runs)
                evidence["failures"].extend(failures)
                inputs, result = records["baseline"]
                if number == 1:
                    first_inputs.update({method: item[0] for method, item in records.items()})
                    source_rows = next(rows for sql, _, rows in inputs if "WITH database_totals AS" in sql)
                    evidence["source_rows_sha256"] = fingerprint(source_rows)
                    evidence["database_totals"] = result["database_totals"]
                else:
                    assert_equal(first_inputs["baseline"], inputs, "Source/configuration between rounds")
            # All proof calculations replay the captured read results. No extra
            # Supabase query, source export or durable cache is needed.
            baseline_business = baseline_sheets = None
            for method in METHODS:
                if any(failure["method"] == method for failure in evidence["failures"]):
                    evidence["proofs"][method] = {"exact_match": False, "disqualified": True}
                    continue
                replay, complete = replay_query(first_inputs[method])
                with patch.object(reports, "execute_query_pg", replay):
                    result = build(full_sections)
                complete()
                business, sheets = business_result(result), workbook_cells(reports, result)
                if method == "baseline":
                    baseline_business, baseline_sheets = business, sheets
                try:
                    assert_equal(baseline_business, business, "Full report/drilldown")
                    assert_equal(baseline_sheets, sheets, "Every exported worksheet cell")
                    evidence["proofs"][method] = {"full_business_sha256": fingerprint(business),
                                                "workbook_sha256": fingerprint(sheets),
                                                "worksheets": len(sheets), "exact_match": True}
                except ExperimentError as error:
                    evidence["failures"].append({"method": method, "reason": str(error)})
        evidence["medians"] = summarize_runs(evidence["runs"], evidence["failures"])
        evidence["status"] = "rejected_accuracy" if evidence["failures"] else "comparisons_passed"
        evidence["recommendation"] = "Sandbox evidence only; production changes require separate approval"
        return evidence
    finally:
        if database._pg_pool is not None:
            database._pg_pool.closeall()


def validate_local_fixture_arguments(args):
    if not args.local_csv or not args.config_json or not args.local_data_dir:
        raise ExperimentError("Local mode requires a CSV, Account Control Centre JSON, and isolated data directory")
    for value, label in ((args.local_csv, "CSV"), (args.config_json, "configuration")):
        path = Path(value).resolve()
        if not path.is_file() or path.is_relative_to(ROOT):
            raise ExperimentError(f"Local {label} must be an existing file outside the repository")
    data_dir = Path(args.local_data_dir).resolve()
    if not data_dir.is_dir() or data_dir.is_relative_to(ROOT):
        raise ExperimentError("PostgreSQL data directory must be an existing isolated directory outside the repository")
    if not 1 <= args.local_port <= 65535 or not 2 <= args.repeats <= 3:
        raise ExperimentError("Use a valid loopback port and two or three comparison rounds")
    if not args.client_id or args.client_id < 1 or not args.start or not args.end:
        raise ExperimentError("Local mode requires a client and explicit ISO start/end dates")
    try:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    except ValueError as error:
        raise ExperimentError("Use ISO dates") from error
    if start > end or (end - start).days > 366:
        raise ExperimentError("Choose an ordered period no longer than one year")
    if args.output:
        output = Path(args.output).resolve()
        if output.is_relative_to(ROOT) or output.exists() or not output.parent.is_dir():
            raise ExperimentError("Evidence needs a new path outside the repository")


def _local_cashflow_config(config):
    categories = {int(row["id"]): row for row in config.get("categories", []) if row.get("id") is not None}
    settings = config.get("settings") or {}
    role_categories = {}
    for role, category_ids in (settings.get("role_categories") or {}).items():
        role_categories[role] = [
            categories[int(category_id)]["label"] for category_id in category_ids
            if int(category_id) in categories and categories[int(category_id)].get("is_active")
        ]
    vendors = set()
    for row in config.get("mapping_accounts") or []:
        account = row.get("account") or row
        assignment = account.get("assignment") or {}
        if assignment.get("verification_workstream") in {"local_vendor", "import_vendor"} and account.get("account_code"):
            vendors.add(str(account["account_code"]))
    return {
        "source_template_id": int(settings.get("source_template_id") or 0),
        "cash_account_codes": [str(code) for code in (config.get("treasury_accounts") or {}).get("cash", [])],
        "bank_account_codes": [str(code) for code in (config.get("treasury_accounts") or {}).get("bank", [])],
        "match_internal_transfers": bool(settings.get("match_internal_transfers")),
        "receipt_presentation": settings.get("receipt_presentation") or "net",
        "customer_payment_roles": role_categories,
        "vendor_account_codes": sorted(vendors),
    }


def _create_local_fixture_tables(conn):
    statements = [
        """CREATE TABLE client_data (
            id BIGINT PRIMARY KEY, client_id BIGINT NOT NULL, template_id BIGINT NOT NULL,
            upload_id BIGINT NOT NULL, row_data JSONB NOT NULL, is_deleted BOOLEAN DEFAULT FALSE,
            change_detection_status TEXT DEFAULT 'active', derived_voucher_id TEXT,
            derived_account_code TEXT, derived_account_name TEXT, derived_transaction_date DATE,
            derived_transaction_month TEXT, derived_transaction_fortnight TEXT,
            derived_debit DOUBLE PRECISION DEFAULT 0, derived_credit DOUBLE PRECISION DEFAULT 0,
            derived_treasury_type TEXT, derived_classification_status TEXT,
            derived_category TEXT
        )""",
        """CREATE TABLE account_control_cashflow_categories (
            id BIGINT PRIMARY KEY, client_id BIGINT NOT NULL, label TEXT NOT NULL,
            is_active BOOLEAN NOT NULL
        )""",
        """CREATE TABLE account_control_cashflow_category_assignments (
            client_id BIGINT NOT NULL, account_code TEXT NOT NULL, category_id BIGINT,
            PRIMARY KEY (client_id, account_code)
        )""",
        "CREATE INDEX idx_fixture_client_date ON client_data (client_id, is_deleted, derived_transaction_date)",
        "CREATE INDEX idx_fixture_client_class_date ON client_data (client_id, is_deleted, derived_treasury_type, derived_transaction_date)",
    ]
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


def _load_local_fixture_rows(conn, csv_path, client_id, template_id):
    from psycopg2.extras import Json, execute_values

    expected = {
        "id", "client_id", "template_id", "upload_id", "row_data", "is_deleted",
        "change_detection_status", "derived_voucher_id", "derived_account_code",
        "derived_account_name", "derived_transaction_date", "derived_transaction_month",
        "derived_transaction_fortnight", "derived_debit", "derived_credit",
        "derived_treasury_type", "derived_classification_status",
    }
    loaded, batch = 0, []
    insert_sql = """INSERT INTO client_data (
        id, client_id, template_id, upload_id, row_data, is_deleted, change_detection_status,
        derived_voucher_id, derived_account_code, derived_account_name, derived_transaction_date,
        derived_transaction_month, derived_transaction_fortnight, derived_debit, derived_credit,
        derived_treasury_type, derived_classification_status
    ) VALUES %s"""
    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not expected <= set(reader.fieldnames or []):
            raise ExperimentError("CSV is missing required client_data source columns")
        for row in reader:
            if row.get("client_id") != str(client_id) or row.get("template_id") != str(template_id):
                continue
            try:
                raw_json = json.loads(row["row_data"])
            except (TypeError, ValueError) as error:
                raise ExperimentError("CSV row_data is not valid JSON") from error
            batch.append((
                int(row["id"]), int(row["client_id"]), int(row["template_id"]), int(row["upload_id"]),
                Json(raw_json), str(row.get("is_deleted") or "false").casefold() in {"true", "t", "1"},
                row.get("change_detection_status") or "active", row.get("derived_voucher_id") or None,
                row.get("derived_account_code") or None, row.get("derived_account_name") or None,
                row.get("derived_transaction_date") or None, row.get("derived_transaction_month") or None,
                row.get("derived_transaction_fortnight") or None, row.get("derived_debit") or None,
                row.get("derived_credit") or None, row.get("derived_treasury_type") or None,
                row.get("derived_classification_status") or None,
            ))
            if len(batch) >= 1000:
                with conn.cursor() as cursor:
                    execute_values(cursor, insert_sql, batch, page_size=1000)
                loaded += len(batch)
                batch.clear()
    if batch:
        with conn.cursor() as cursor:
            execute_values(cursor, insert_sql, batch, page_size=1000)
        loaded += len(batch)
    if not loaded:
        raise ExperimentError("CSV has no rows for the selected client/template")
    return loaded


def _load_local_category_config(conn, config, client_id):
    from psycopg2.extras import execute_values

    categories = config.get("categories") or []
    if not categories:
        raise ExperimentError("Account Control Centre JSON contains no categories")
    assignments = {}
    for collection in (config.get("accounts") or [], config.get("historical_orphans") or []):
        for item in collection:
            code = str(item.get("account_code") or "").strip()
            category_id = item.get("category_id")
            if code:
                assignments[code] = int(category_id) if category_id is not None else None
    with conn.cursor() as cursor:
        execute_values(cursor,
            "INSERT INTO account_control_cashflow_categories (id, client_id, label, is_active) VALUES %s",
            [(int(row["id"]), client_id, str(row["label"]), bool(row.get("is_active"))) for row in categories],
            page_size=500)
        execute_values(cursor,
            "INSERT INTO account_control_cashflow_category_assignments (client_id, account_code, category_id) VALUES %s",
            [(client_id, code, category_id) for code, category_id in assignments.items()],
            page_size=1000)
    return len(categories), sum(category_id is not None for category_id in assignments.values())


def refresh_local_derived_categories(conn, client_id, template_id, *, account_codes=None, category_id=None):
    """Experiment-only refresh; updates only non-Treasury rows in the local clone."""
    where = ["cd.client_id = %s", "cd.template_id = %s", "cd.derived_classification_status = 'non_treasury'",
             "cd.is_deleted = FALSE", "cd.change_detection_status <> 'deleted'"]
    params = [client_id, template_id]
    if account_codes is not None:
        codes = sorted({str(code).strip() for code in account_codes if str(code).strip()})
        if not codes:
            return 0
        where.append("cd.derived_account_code = ANY(%s)")
        params.append(codes)
    if category_id is not None:
        where.append("EXISTS (SELECT 1 FROM account_control_cashflow_category_assignments scope_assignment "
                     "WHERE scope_assignment.client_id = cd.client_id "
                     "AND scope_assignment.account_code = cd.derived_account_code "
                     "AND scope_assignment.category_id = %s)")
        params.append(int(category_id))
    sql = f"""UPDATE client_data cd
        SET derived_category = COALESCE((
            SELECT category.label
            FROM account_control_cashflow_category_assignments assignment
            JOIN account_control_cashflow_categories category
              ON category.id = assignment.category_id
             AND category.client_id = assignment.client_id
             AND category.is_active = TRUE
            WHERE assignment.client_id = cd.client_id
              AND assignment.account_code = cd.derived_account_code
            LIMIT 1
        ), '')
        WHERE {' AND '.join(where)}"""
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def _local_business_payload(source, summary):
    def without_category(frame):
        values = frame.to_dict(orient="split")
        if "category" in values["columns"]:
            index = values["columns"].index("category")
            values["columns"].pop(index)
            for row in values["data"]:
                row.pop(index)
        return values

    return {
        # Category labels are asserted separately for every active non-Treasury
        # client_data row. Exclude them here because ACC may also label Treasury
        # rows, which are outside this experiment's materialized-column scope.
        "journal": without_category(source.journal),
        "coa": without_category(source.coa),
        "diagnostics": list(source.diagnostics),
        "metadata": {key: value for key, value in source.metadata.items() if not key.endswith("_seconds")},
        "summary": summary,
    }


def _assert_local_categories_match_assignments(conn, client_id, template_id):
    sql = """SELECT COUNT(*)
        FROM client_data cd
        LEFT JOIN account_control_cashflow_category_assignments assignment
          ON assignment.client_id = cd.client_id
         AND assignment.account_code = cd.derived_account_code
        LEFT JOIN account_control_cashflow_categories category
          ON category.id = assignment.category_id
         AND category.client_id = cd.client_id
         AND category.is_active = TRUE
        WHERE cd.client_id = %s AND cd.template_id = %s
          AND cd.derived_classification_status = 'non_treasury'
          AND cd.is_deleted = FALSE
          AND COALESCE(cd.change_detection_status, 'active') <> 'deleted'
          AND cd.derived_category IS DISTINCT FROM COALESCE(category.label, '')"""
    with conn.cursor() as cursor:
        cursor.execute(sql, (client_id, template_id))
        mismatches = int(cursor.fetchone()[0])
    if mismatches:
        raise ExperimentError(f"Derived_Category is stale for {mismatches} active non-Treasury rows")
    return mismatches


def _run_local_method(conn, args, config, method):
    from psycopg2.extras import RealDictCursor
    from services.ledger_group_summary import build_cashflow_summary
    from services.ledger_report_source import load_cashflow_client_data_source

    query_seconds = [0.0]

    def query_fn(sql, params=None):
        if method == "derived_category" and "WITH database_totals AS" in sql:
            sql = materialized_category_sql(sql)
        assert_select(sql)
        started = perf_counter()
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(sql, params)
            rows = [dict(row) for row in cursor.fetchall()]
        query_seconds[0] += perf_counter() - started
        return rows

    source_started = perf_counter()
    source = load_cashflow_client_data_source(
        args.client_id, journal_template_id=config["source_template_id"],
        period_start=args.start, period_end=args.end, cashflow_group_by="month", query_fn=query_fn,
    )
    source_seconds = perf_counter() - source_started
    if not source.ready:
        raise ExperimentError("Local source is not ready for the selected report period")
    summary_started = perf_counter()
    summary = build_cashflow_summary(
        source.journal, source.coa, config["cash_account_codes"], config["bank_account_codes"],
        args.start, args.end, group_by="month",
        match_internal_transfers=config["match_internal_transfers"],
        receipt_presentation=config["receipt_presentation"],
        cashflow_role_categories=config["customer_payment_roles"],
        vendor_account_codes=config["vendor_account_codes"], include_drilldown=True,
    )
    summary_seconds = perf_counter() - summary_started
    return _local_business_payload(source, summary), {
        "method": method, "source_seconds": source_seconds,
        "source_query_seconds": query_seconds[0], "summary_seconds": summary_seconds,
        "report_core_seconds": source_seconds + summary_seconds,
        "source_rows": int(source.metadata.get("source_row_count") or 0),
        "selected_vouchers": int(summary.get("selected_voucher_count") or 0),
    }


def run_local_fixture_benchmark(args):
    """Compare the current join with a materialized category in a verified local cluster."""
    validate_local_fixture_arguments(args)
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2.extensions import quote_ident

    config_raw = json.loads(Path(args.config_json).read_text(encoding="utf-8"))
    if int(config_raw.get("client_id") or 0) != args.client_id:
        raise ExperimentError("Account Control Centre export client does not match benchmark scope")
    config = _local_cashflow_config(config_raw)
    template_id = config["source_template_id"]
    if not template_id:
        raise ExperimentError("Account Control Centre export has no configured Cashflows template")
    data_dir = Path(args.local_data_dir).resolve()
    conn = psycopg2.connect(host="127.0.0.1", port=args.local_port, dbname="postgres",
                            user="postgres", connect_timeout=3)
    schema = "cashflow_category_trial_" + uuid.uuid4().hex[:12]
    quoted_schema = quote_ident(schema, conn)
    verified_cluster = False
    try:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("SHOW data_directory")
            if Path(cursor.fetchone()[0]).resolve() != data_dir:
                raise ExperimentError("Connected PostgreSQL is not the specified isolated test cluster")
            cursor.execute("SELECT host(inet_server_addr())")
            if cursor.fetchone()[0] != "127.0.0.1":
                raise ExperimentError("Test cluster must listen only on IPv4 loopback")
            cursor.execute("CREATE SCHEMA " + quoted_schema)
            cursor.execute("SET search_path TO " + quoted_schema + ", public")
            cursor.execute("SET statement_timeout = '120s'")
        verified_cluster = True
        conn.autocommit = False
        _create_local_fixture_tables(conn)
        rows_loaded = _load_local_fixture_rows(conn, args.local_csv, args.client_id, template_id)
        category_count, assignment_count = _load_local_category_config(conn, config_raw, args.client_id)
        refresh_started = perf_counter()
        initial_refresh_rows = refresh_local_derived_categories(conn, args.client_id, template_id)
        initial_refresh_seconds = perf_counter() - refresh_started
        initial_category_mismatches = _assert_local_categories_match_assignments(conn, args.client_id, template_id)
        with conn.cursor() as cursor:
            cursor.execute("CREATE INDEX idx_fixture_category_assignment ON account_control_cashflow_category_assignments (client_id, account_code, category_id)")
            cursor.execute("ANALYZE client_data")
            cursor.execute("ANALYZE account_control_cashflow_category_assignments")
            cursor.execute("ANALYZE account_control_cashflow_categories")
            cursor.execute("SELECT COALESCE(SUM(octet_length(derived_category)), 0) FROM client_data WHERE derived_category <> ''")
            label_bytes = int(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM client_data WHERE derived_classification_status <> 'non_treasury' AND derived_category <> ''")
            non_counter_labels = int(cursor.fetchone()[0])
        conn.commit()
        args._local_schema = schema
        evidence = {
            "status": "running", "boundary": "temporary loopback-only PostgreSQL cluster; no Supabase writes",
            "scope": {"client_id": args.client_id, "template_id": template_id,
                      "period_start": args.start, "period_end": args.end, "group_by": "month"},
            "timing_scope": "local source query plus summary/drilldown calculation; excludes network, configuration and uploaded-ledger balance reads",
            "fixture_rows": rows_loaded, "category_count": category_count,
            "category_assignment_count": assignment_count,
            "initial_category_refresh_rows": initial_refresh_rows,
            "initial_category_refresh_seconds": initial_refresh_seconds,
            "initial_category_mapping_mismatches": initial_category_mismatches,
            "materialized_label_bytes": label_bytes,
            "non_counter_rows_with_category": non_counter_labels,
            "runs": [], "category_refresh_checks": [], "failures": [],
        }

        def prove_parity(label):
            baseline, _ = _run_local_method(conn, args, config, "baseline")
            candidate, _ = _run_local_method(conn, args, config, "derived_category")
            assert_equal(baseline, candidate, label)
            evidence["category_refresh_checks"].append({"check": label, "exact_match": True})

        if non_counter_labels:
            raise ExperimentError("Experiment populated Derived_Category outside non-Treasury rows")
        prove_parity("Initial source, summary, and drilldown parity")
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("""SELECT cd.derived_account_code AS account_code, assignment.category_id
                FROM client_data cd JOIN account_control_cashflow_category_assignments assignment
                  ON assignment.client_id = cd.client_id AND assignment.account_code = cd.derived_account_code
                WHERE cd.client_id = %s AND cd.template_id = %s
                  AND cd.derived_classification_status = 'non_treasury'
                  AND cd.derived_transaction_date BETWEEN %s AND %s
                  AND assignment.category_id IS NOT NULL
                GROUP BY cd.derived_account_code, assignment.category_id
                ORDER BY cd.derived_account_code LIMIT 1""",
                (args.client_id, template_id, args.start, args.end))
            target = cursor.fetchone()
            cursor.execute("SELECT id FROM account_control_cashflow_categories WHERE client_id = %s AND is_active AND id <> %s ORDER BY id LIMIT 1",
                           (args.client_id, target["category_id"] if target else -1))
            alternate = cursor.fetchone()
        if not target or not alternate:
            raise ExperimentError("Fixture lacks a mapped counter-account or alternate active category for refresh proof")

        started = perf_counter()
        with conn.cursor() as cursor:
            cursor.execute("UPDATE account_control_cashflow_category_assignments SET category_id = %s WHERE client_id = %s AND account_code = %s",
                           (alternate["id"], args.client_id, target["account_code"]))
        changed_rows = refresh_local_derived_categories(conn, args.client_id, template_id,
                                                        account_codes=[target["account_code"]])
        mapping_mismatches = _assert_local_categories_match_assignments(conn, args.client_id, template_id)
        conn.commit()
        evidence["category_refresh_checks"].append({"check": "Account reassignment refresh",
            "rows_updated": changed_rows, "mapping_mismatches": mapping_mismatches,
            "refresh_seconds": perf_counter() - started})
        prove_parity("Account reassignment parity")

        with conn.cursor() as cursor:
            cursor.execute("SELECT label FROM account_control_cashflow_categories WHERE id = %s", (alternate["id"],))
            old_label = cursor.fetchone()[0]
            cursor.execute("UPDATE account_control_cashflow_categories SET label = %s WHERE id = %s",
                           (old_label + " [trial]", alternate["id"]))
        started = perf_counter()
        changed_rows = refresh_local_derived_categories(conn, args.client_id, template_id, category_id=alternate["id"])
        mapping_mismatches = _assert_local_categories_match_assignments(conn, args.client_id, template_id)
        conn.commit()
        evidence["category_refresh_checks"].append({"check": "Category rename refresh",
            "rows_updated": changed_rows, "mapping_mismatches": mapping_mismatches,
            "refresh_seconds": perf_counter() - started})
        prove_parity("Category rename parity")

        with conn.cursor() as cursor:
            cursor.execute("UPDATE account_control_cashflow_categories SET is_active = FALSE WHERE id = %s", (alternate["id"],))
        started = perf_counter()
        changed_rows = refresh_local_derived_categories(conn, args.client_id, template_id, category_id=alternate["id"])
        mapping_mismatches = _assert_local_categories_match_assignments(conn, args.client_id, template_id)
        conn.commit()
        evidence["category_refresh_checks"].append({"check": "Category deactivation refresh",
            "rows_updated": changed_rows, "mapping_mismatches": mapping_mismatches,
            "refresh_seconds": perf_counter() - started})
        prove_parity("Category deactivation parity")

        for number in range(1, args.repeats + 1):
            order = ("baseline", "derived_category") if number % 2 else ("derived_category", "baseline")
            pair = {}
            for method in order:
                business, timing = _run_local_method(conn, args, config, method)
                pair[method] = business
                row = {"round": number, **timing}
                evidence["runs"].append(row)
                print(json.dumps({"progress": "local_timed", **row}), flush=True)
            assert_equal(pair["baseline"], pair["derived_category"], "Timed source, summary, and drilldown parity")
        evidence["business_sha256"] = fingerprint(pair["baseline"])
        evidence["status"] = "rejected_accuracy" if evidence["failures"] else "comparisons_passed"
        evidence["timing_medians"] = {
            method: {key: statistics.median(row[key] for row in evidence["runs"] if row["method"] == method)
                     for key in ("source_query_seconds", "source_seconds", "summary_seconds", "report_core_seconds")}
            for method in ("baseline", "derived_category")
        }
        evidence["paired_report_core_improvement_percent"] = [
            {"round": number, "improvement_percent": 100 * (
                next(row["report_core_seconds"] for row in evidence["runs"] if row["round"] == number and row["method"] == "baseline")
                - next(row["report_core_seconds"] for row in evidence["runs"] if row["round"] == number and row["method"] == "derived_category")
            ) / max(0.000001, next(row["report_core_seconds"] for row in evidence["runs"] if row["round"] == number and row["method"] == "baseline"))}
            for number in range(1, args.repeats + 1)
        ]
        evidence["recommendation"] = "Local experiment only; production schema and application changes require separate approval"
        return evidence
    finally:
        if verified_cluster:
            try:
                conn.rollback()
                conn.autocommit = True
                with conn.cursor() as cursor:
                    cursor.execute("DROP SCHEMA IF EXISTS " + quoted_schema + " CASCADE")
            except Exception:
                pass
        conn.close()


def validate_sqlite_fixture_arguments(args):
    """Require explicit local inputs; the SQLite clone is always temporary."""
    if not args.local_csv or not args.config_json:
        raise ExperimentError("SQLite mode requires an external CSV and Account Control Centre JSON")
    for value, label in ((args.local_csv, "CSV"), (args.config_json, "configuration")):
        path = Path(value).resolve()
        if not path.is_file() or path.is_relative_to(ROOT):
            raise ExperimentError(f"Local {label} must be an existing file outside the repository")
    if not args.client_id or args.client_id < 1 or not args.start or not args.end:
        raise ExperimentError("SQLite mode requires a client and explicit ISO start/end dates")
    try:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    except ValueError as error:
        raise ExperimentError("Use ISO dates") from error
    if start > end or (end - start).days > 366:
        raise ExperimentError("Choose an ordered period no longer than one year")
    if not 2 <= args.repeats <= 3:
        raise ExperimentError("Use two or three bounded comparison rounds")
    if args.output:
        output = Path(args.output).resolve()
        if output.is_relative_to(ROOT) or output.exists() or not output.parent.is_dir():
            raise ExperimentError("Evidence needs a new path outside the repository")


def _create_sqlite_fixture_tables(conn):
    """Build the report's required tables only in the temporary SQLite clone."""
    conn.executescript("""
        CREATE TABLE client_data (
            id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, template_id INTEGER NOT NULL,
            upload_id INTEGER NOT NULL, row_data TEXT NOT NULL, is_deleted INTEGER DEFAULT 0,
            change_detection_status TEXT DEFAULT 'active', derived_voucher_id TEXT,
            derived_account_code TEXT, derived_account_name TEXT, derived_transaction_date TEXT,
            derived_transaction_month TEXT, derived_transaction_fortnight TEXT,
            derived_debit REAL DEFAULT 0, derived_credit REAL DEFAULT 0,
            derived_treasury_type TEXT, derived_classification_status TEXT,
            derived_category TEXT DEFAULT ''
        );
        CREATE TABLE account_control_cashflow_categories (
            id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, label TEXT NOT NULL,
            is_active INTEGER NOT NULL
        );
        CREATE TABLE account_control_cashflow_category_assignments (
            client_id INTEGER NOT NULL, account_code TEXT NOT NULL, category_id INTEGER,
            PRIMARY KEY (client_id, account_code)
        );
        CREATE INDEX idx_fixture_client_date
            ON client_data (client_id, is_deleted, derived_transaction_date);
        CREATE INDEX idx_fixture_client_class_date
            ON client_data (client_id, is_deleted, derived_treasury_type, derived_transaction_date);
        CREATE INDEX idx_fixture_account_code ON client_data (client_id, template_id, derived_account_code);
        CREATE INDEX idx_fixture_category_assignment
            ON account_control_cashflow_category_assignments (client_id, account_code, category_id);
    """)


def _load_sqlite_fixture_rows(conn, csv_path, client_id, template_id):
    """Stream the selected client's canonical export into the temporary clone."""
    expected = {
        "id", "client_id", "template_id", "upload_id", "row_data", "is_deleted",
        "change_detection_status", "derived_voucher_id", "derived_account_code",
        "derived_account_name", "derived_transaction_date", "derived_transaction_month",
        "derived_transaction_fortnight", "derived_debit", "derived_credit",
        "derived_treasury_type", "derived_classification_status",
    }
    insert = """INSERT INTO client_data (
        id, client_id, template_id, upload_id, row_data, is_deleted, change_detection_status,
        derived_voucher_id, derived_account_code, derived_account_name, derived_transaction_date,
        derived_transaction_month, derived_transaction_fortnight, derived_debit, derived_credit,
        derived_treasury_type, derived_classification_status
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    loaded = 0
    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not expected <= set(reader.fieldnames or []):
            raise ExperimentError("CSV is missing required client_data source columns")
        batch = []
        for row in reader:
            if row.get("client_id") != str(client_id) or row.get("template_id") != str(template_id):
                continue
            try:
                json.loads(row["row_data"])
                item = (
                    int(row["id"]), int(row["client_id"]), int(row["template_id"]), int(row["upload_id"]),
                    row["row_data"], int(str(row.get("is_deleted") or "false").casefold() in {"true", "t", "1"}),
                    row.get("change_detection_status") or "active", row.get("derived_voucher_id") or None,
                    row.get("derived_account_code") or None, row.get("derived_account_name") or None,
                    row.get("derived_transaction_date") or None, row.get("derived_transaction_month") or None,
                    row.get("derived_transaction_fortnight") or None, row.get("derived_debit") or 0,
                    row.get("derived_credit") or 0, row.get("derived_treasury_type") or None,
                    row.get("derived_classification_status") or None,
                )
            except (TypeError, ValueError) as error:
                raise ExperimentError("CSV contains an invalid source row") from error
            batch.append(item)
            if len(batch) >= 1000:
                conn.executemany(insert, batch)
                loaded += len(batch)
                batch.clear()
        if batch:
            conn.executemany(insert, batch)
            loaded += len(batch)
    if not loaded:
        raise ExperimentError("CSV has no rows for the selected client/template")
    return loaded


def _load_sqlite_category_config(conn, config, client_id):
    categories = config.get("categories") or []
    if not categories:
        raise ExperimentError("Account Control Centre JSON contains no categories")
    assignments = {}
    for collection in (config.get("accounts") or [], config.get("historical_orphans") or []):
        for item in collection:
            code = str(item.get("account_code") or "").strip()
            if code:
                category_id = item.get("category_id")
                assignments[code] = int(category_id) if category_id is not None else None
    conn.executemany(
        "INSERT INTO account_control_cashflow_categories (id, client_id, label, is_active) VALUES (?, ?, ?, ?)",
        [(int(row["id"]), client_id, str(row["label"]), int(bool(row.get("is_active")))) for row in categories],
    )
    conn.executemany(
        "INSERT INTO account_control_cashflow_category_assignments (client_id, account_code, category_id) VALUES (?, ?, ?)",
        [(client_id, code, category_id) for code, category_id in assignments.items()],
    )
    return len(categories), sum(category_id is not None for category_id in assignments.values())


def refresh_sqlite_derived_categories(conn, client_id, template_id, *, account_codes=None, category_id=None):
    """Apply ACC labels only to active non-Treasury rows in the temporary clone."""
    where = ["cd.client_id = ?", "cd.template_id = ?", "cd.derived_classification_status = 'non_treasury'",
             "cd.is_deleted = 0", "COALESCE(cd.change_detection_status, 'active') <> 'deleted'"]
    params = [client_id, template_id]
    if account_codes is not None:
        codes = sorted({str(code).strip() for code in account_codes if str(code).strip()})
        if not codes:
            return 0
        where.append("cd.derived_account_code IN (" + ", ".join("?" for _ in codes) + ")")
        params.extend(codes)
    if category_id is not None:
        where.append("EXISTS (SELECT 1 FROM account_control_cashflow_category_assignments scope_assignment "
                     "WHERE scope_assignment.client_id = cd.client_id "
                     "AND scope_assignment.account_code = cd.derived_account_code "
                     "AND scope_assignment.category_id = ?)")
        params.append(int(category_id))
    sql = f"""UPDATE client_data AS cd
        SET derived_category = COALESCE((
            SELECT category.label
            FROM account_control_cashflow_category_assignments assignment
            JOIN account_control_cashflow_categories category
              ON category.id = assignment.category_id
             AND category.client_id = assignment.client_id
             AND category.is_active = 1
            WHERE assignment.client_id = cd.client_id
              AND assignment.account_code = cd.derived_account_code
            LIMIT 1
        ), '')
        WHERE {' AND '.join(where)}"""
    cursor = conn.execute(sql, params)
    return cursor.rowcount


def _assert_sqlite_categories_match_assignments(conn, client_id, template_id):
    cursor = conn.execute("""SELECT COUNT(*) FROM client_data cd
        LEFT JOIN account_control_cashflow_category_assignments assignment
          ON assignment.client_id = cd.client_id AND assignment.account_code = cd.derived_account_code
        LEFT JOIN account_control_cashflow_categories category
          ON category.id = assignment.category_id AND category.client_id = assignment.client_id
         AND category.is_active = 1
        WHERE cd.client_id = ? AND cd.template_id = ?
          AND cd.derived_classification_status = 'non_treasury'
          AND cd.is_deleted = 0 AND COALESCE(cd.change_detection_status, 'active') <> 'deleted'
          AND COALESCE(cd.derived_category, '') <> COALESCE(category.label, '')""", (client_id, template_id))
    mismatches = int(cursor.fetchone()[0])
    if mismatches:
        raise ExperimentError(f"Derived_Category is stale for {mismatches} active non-Treasury rows")
    return mismatches


def _sqlite_cashflow_sql(sql):
    """Translate PostgreSQL syntax and preserve efficient SQLite voucher membership."""
    assert_select(sql)
    detail_expression = re.compile(
        r"COALESCE\(\s*cd\.row_data\s*->>\s*'Voucher Detail',\s*"
        r"cd\.row_data\s*->>\s*'Detail',\s*cd\.row_data\s*->>\s*'Description',\s*"
        r"cd\.row_data\s*->>\s*'Narration',\s*cd\.row_data\s*->>\s*'Remarks',\s*''\s*\)", re.S,
    )
    sql, replacements = detail_expression.subn(
        "COALESCE(json_extract(cd.row_data, '$.\\\"Voucher Detail\\\"'), "
        "json_extract(cd.row_data, '$.Detail'), json_extract(cd.row_data, '$.Description'), "
        "json_extract(cd.row_data, '$.Narration'), json_extract(cd.row_data, '$.Remarks'), '')", sql,
    )
    if replacements != 1:
        raise ExperimentError("Canonical Cashflows JSON projection changed; review SQLite adapter")
    sql = re.sub(r"::double precision\b", "", sql, flags=re.I)
    sql = re.sub(r"\bBTRIM\(", "TRIM(", sql, flags=re.I)
    sql = re.sub(r"\bFALSE\b", "0", sql, flags=re.I)
    sql = re.sub(r"\bTRUE\b", "1", sql, flags=re.I)
    sql = sql.replace("%s", "?")
    membership_join = re.compile(
        r"FROM derived_lines l\s+JOIN selected_vouchers v\s+ON v\.voucher_id = l\.voucher_id",
        re.I,
    )
    sql, membership_rewrites = membership_join.subn(
        "FROM derived_lines l\n        WHERE l.voucher_id IN (SELECT voucher_id FROM selected_vouchers)", sql,
    )
    if membership_rewrites != 1:
        raise ExperimentError("Canonical Cashflows voucher selection changed; review SQLite adapter")
    assert_select(sql)
    return sql


def _run_sqlite_method(conn, args, config, method):
    from services.ledger_group_summary import build_cashflow_summary
    from services.ledger_report_source import load_cashflow_client_data_source

    query_seconds = [0.0]

    def query_fn(sql, params=None):
        if method == "derived_category" and "WITH database_totals AS" in sql:
            sql = materialized_category_sql(sql)
        translated = _sqlite_cashflow_sql(sql)
        started = perf_counter()
        cursor = conn.execute(translated, params or ())
        rows = [dict(row) for row in cursor.fetchall()]
        query_seconds[0] += perf_counter() - started
        return rows

    source_started = perf_counter()
    source = load_cashflow_client_data_source(
        args.client_id, journal_template_id=config["source_template_id"],
        period_start=args.start, period_end=args.end, cashflow_group_by="month", query_fn=query_fn,
    )
    source_seconds = perf_counter() - source_started
    if not source.ready:
        raise ExperimentError("SQLite source is not ready for the selected report period")
    print(json.dumps({"progress": "sqlite_source_complete", "method": method,
                      "source_seconds": source_seconds, "source_query_seconds": query_seconds[0],
                      "source_rows": int(source.metadata.get("source_row_count") or 0)}), flush=True)
    summary_started = perf_counter()
    summary = build_cashflow_summary(
        source.journal, source.coa, config["cash_account_codes"], config["bank_account_codes"],
        args.start, args.end, group_by="month",
        match_internal_transfers=config["match_internal_transfers"],
        receipt_presentation=config["receipt_presentation"],
        cashflow_role_categories=config["customer_payment_roles"],
        vendor_account_codes=config["vendor_account_codes"], include_drilldown=True,
    )
    summary_seconds = perf_counter() - summary_started
    print(json.dumps({"progress": "sqlite_summary_complete", "method": method,
                      "summary_seconds": summary_seconds}), flush=True)
    return _local_business_payload(source, summary), {
        "method": method, "source_seconds": source_seconds,
        "source_query_seconds": query_seconds[0], "summary_seconds": summary_seconds,
        "report_core_seconds": source_seconds + summary_seconds,
        "source_rows": int(source.metadata.get("source_row_count") or 0),
        "selected_vouchers": int(summary.get("selected_voucher_count") or 0),
    }


def run_sqlite_fixture_benchmark(args):
    """Compare report variants on a temporary SQLite clone; never touches app DB."""
    import sqlite3

    validate_sqlite_fixture_arguments(args)
    config_raw = json.loads(Path(args.config_json).read_text(encoding="utf-8"))
    if int(config_raw.get("client_id") or 0) != args.client_id:
        raise ExperimentError("Account Control Centre export client does not match benchmark scope")
    config = _local_cashflow_config(config_raw)
    template_id = config["source_template_id"]
    if not template_id:
        raise ExperimentError("Account Control Centre export has no configured Cashflows template")
    evidence = {
        "status": "running", "boundary": "temporary SQLite file; no app DB or Supabase access",
        "scope": {"client_id": args.client_id, "template_id": template_id,
                  "period_start": args.start, "period_end": args.end, "group_by": "month"},
        "timing_scope": "local SQLite source query plus summary/drilldown calculation; excludes network, configuration and uploaded-ledger balance reads",
        "runs": [], "category_refresh_checks": [], "failures": [],
    }
    with tempfile.TemporaryDirectory(prefix="cashflow-sqlite-benchmark-") as scratch:
        conn = sqlite3.connect(str(Path(scratch) / "cashflow.sqlite"))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
            _create_sqlite_fixture_tables(conn)
            rows_loaded = _load_sqlite_fixture_rows(conn, args.local_csv, args.client_id, template_id)
            category_count, assignment_count = _load_sqlite_category_config(conn, config_raw, args.client_id)
            refresh_started = perf_counter()
            initial_refresh_rows = refresh_sqlite_derived_categories(conn, args.client_id, template_id)
            conn.commit()
            initial_refresh_seconds = perf_counter() - refresh_started
            mismatches = _assert_sqlite_categories_match_assignments(conn, args.client_id, template_id)
            cursor = conn.execute("SELECT COUNT(*) FROM client_data WHERE derived_classification_status <> 'non_treasury' AND derived_category <> ''")
            non_counter_labels = int(cursor.fetchone()[0])
            if mismatches or non_counter_labels:
                raise ExperimentError("SQLite materialization did not match current category assignments")
            evidence.update({
                "fixture_rows": rows_loaded, "category_count": category_count,
                "category_assignment_count": assignment_count,
                "initial_category_refresh_rows": initial_refresh_rows,
                "initial_category_refresh_seconds": initial_refresh_seconds,
                "initial_category_mapping_mismatches": mismatches,
                "non_counter_rows_with_category": non_counter_labels,
            })
            print(json.dumps({"progress": "sqlite_fixture_loaded", "fixture_rows": rows_loaded,
                              "initial_category_refresh_rows": initial_refresh_rows}), flush=True)

            target = conn.execute("""SELECT cd.derived_account_code AS account_code, assignment.category_id
                FROM client_data cd JOIN account_control_cashflow_category_assignments assignment
                  ON assignment.client_id = cd.client_id AND assignment.account_code = cd.derived_account_code
                WHERE cd.client_id = ? AND cd.template_id = ?
                  AND cd.derived_classification_status = 'non_treasury'
                  AND cd.derived_transaction_date BETWEEN ? AND ? AND assignment.category_id IS NOT NULL
                GROUP BY cd.derived_account_code, assignment.category_id
                ORDER BY cd.derived_account_code LIMIT 1""", (args.client_id, template_id, args.start, args.end)).fetchone()
            alternate = conn.execute("""SELECT id FROM account_control_cashflow_categories
                WHERE client_id = ? AND is_active = 1 AND id <> ? ORDER BY id LIMIT 1""",
                (args.client_id, target["category_id"] if target else -1)).fetchone()
            if not target or not alternate:
                raise ExperimentError("Fixture lacks a mapped counter-account or alternate active category for refresh proof")

            started = perf_counter()
            conn.execute("UPDATE account_control_cashflow_category_assignments SET category_id = ? WHERE client_id = ? AND account_code = ?",
                         (alternate["id"], args.client_id, target["account_code"]))
            changed = refresh_sqlite_derived_categories(conn, args.client_id, template_id,
                                                        account_codes=[target["account_code"]])
            mapping_mismatches = _assert_sqlite_categories_match_assignments(conn, args.client_id, template_id)
            conn.commit()
            evidence["category_refresh_checks"].append({"check": "Account reassignment refresh", "rows_updated": changed,
                                                         "mapping_mismatches": mapping_mismatches,
                                                         "refresh_seconds": perf_counter() - started})

            old_label = conn.execute("SELECT label FROM account_control_cashflow_categories WHERE id = ?", (alternate["id"],)).fetchone()[0]
            started = perf_counter()
            conn.execute("UPDATE account_control_cashflow_categories SET label = ? WHERE id = ?", (old_label + " [trial]", alternate["id"]))
            changed = refresh_sqlite_derived_categories(conn, args.client_id, template_id, category_id=alternate["id"])
            mapping_mismatches = _assert_sqlite_categories_match_assignments(conn, args.client_id, template_id)
            conn.commit()
            evidence["category_refresh_checks"].append({"check": "Category rename refresh", "rows_updated": changed,
                                                         "mapping_mismatches": mapping_mismatches,
                                                         "refresh_seconds": perf_counter() - started})

            started = perf_counter()
            conn.execute("UPDATE account_control_cashflow_categories SET is_active = 0 WHERE id = ?", (alternate["id"],))
            changed = refresh_sqlite_derived_categories(conn, args.client_id, template_id, category_id=alternate["id"])
            mapping_mismatches = _assert_sqlite_categories_match_assignments(conn, args.client_id, template_id)
            conn.commit()
            evidence["category_refresh_checks"].append({"check": "Category deactivation refresh", "rows_updated": changed,
                                                         "mapping_mismatches": mapping_mismatches,
                                                         "refresh_seconds": perf_counter() - started})

            print(json.dumps({"progress": "category_refresh_checks_passed",
                              "checks": len(evidence["category_refresh_checks"])}), flush=True)
            warmups = {}
            for method in ("baseline", "derived_category"):
                warmups[method], _ = _run_sqlite_method(conn, args, config, method)
            assert_equal(warmups["baseline"], warmups["derived_category"], "Warm-up report parity")
            evidence["warmup"] = {"runs_excluded_from_timing": 2, "exact_match": True}
            print(json.dumps({"progress": "sqlite_warmup_complete", "exact_match": True}), flush=True)
            for number in range(1, args.repeats + 1):
                order = ("baseline", "derived_category") if number % 2 else ("derived_category", "baseline")
                pair = {}
                for method in order:
                    business, timing = _run_sqlite_method(conn, args, config, method)
                    pair[method] = business
                    evidence["runs"].append({"round": number, **timing})
                assert_equal(pair["baseline"], pair["derived_category"], "Timed source, summary, and drilldown parity")
            evidence["business_sha256"] = fingerprint(pair["baseline"])
            evidence["status"] = "comparisons_passed"
            evidence["timing_medians"] = {
                method: {key: statistics.median(row[key] for row in evidence["runs"] if row["method"] == method)
                         for key in ("source_query_seconds", "source_seconds", "summary_seconds", "report_core_seconds")}
                for method in ("baseline", "derived_category")
            }
            evidence["paired_report_core_improvement_percent"] = [
                {"round": number, "improvement_percent": 100 * (
                    next(row["report_core_seconds"] for row in evidence["runs"] if row["round"] == number and row["method"] == "baseline")
                    - next(row["report_core_seconds"] for row in evidence["runs"] if row["round"] == number and row["method"] == "derived_category")
                ) / max(0.000001, next(row["report_core_seconds"] for row in evidence["runs"] if row["round"] == number and row["method"] == "baseline"))}
                for number in range(1, args.repeats + 1)
            ]
            evidence["recommendation"] = "SQLite-only experiment; it does not predict Supabase production latency"
            return evidence
        finally:
            conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--local-fixture", action="store_true",
                        help="Run the category-column comparison against a verified isolated loopback PostgreSQL cluster")
    parser.add_argument("--sqlite-fixture", action="store_true",
                        help="Run the category-column comparison in a temporary local SQLite database")
    parser.add_argument("--client-id", type=int)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--local-csv")
    parser.add_argument("--config-json")
    parser.add_argument("--local-data-dir")
    parser.add_argument("--local-port", type=int, default=55431)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if sum((bool(args.live), bool(args.local_fixture), bool(args.sqlite_fixture))) > 1:
            raise ExperimentError("Choose only one benchmark mode")
        if args.local_fixture:
            evidence = run_local_fixture_benchmark(args)
            if args.output:
                with Path(args.output).open("x", encoding="utf-8") as stream:
                    json.dump(evidence, stream, indent=2)
            print(json.dumps(evidence, indent=2))
            return 1 if evidence["failures"] else 0
        if args.sqlite_fixture:
            evidence = run_sqlite_fixture_benchmark(args)
            if args.output:
                with Path(args.output).open("x", encoding="utf-8") as stream:
                    json.dump(evidence, stream, indent=2)
            print(json.dumps(evidence, indent=2))
            return 1 if evidence["failures"] else 0
        validate_arguments(args)
        if not args.live:
            print(json.dumps({"status": "offline", "network_access": False,
                              "next": "Run tests/test_cashflow_fetch_benchmark.py; --live is explicit opt-in"}))
            return 0
        evidence = run_live(args)
        if args.output:
            with Path(args.output).open("x", encoding="utf-8") as stream:
                json.dump(evidence, stream, indent=2)
        print(json.dumps(evidence, indent=2))
        return 1 if evidence["failures"] else 0
    except Exception as error:
        # Driver error messages may contain SQL/data/connection details. Never
        # print them or a traceback into a shareable benchmark log.
        failure = {"status": "failed", "error_type": type(error).__name__}
        if isinstance(error, ExperimentError):
            failure["reason"] = str(error)
        print(json.dumps(failure), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
