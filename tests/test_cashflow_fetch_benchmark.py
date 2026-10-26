"""Offline guards, lossless transport and full-report/export sandbox checks."""

import argparse
from datetime import date
import json
from types import SimpleNamespace

import pytest

import database
from experiments.cashflow_fetch import benchmark
from experiments.cashflow_fetch.variants import (
    COLUMNS, DESCRIPTORS, METHODS, REMAINING, decode_rows,
    materialize_category_values, materialized_category_sql, transport_sql,
)
from routers import reconciliation as reports
from services import ledger_report_source as sources


@pytest.fixture(autouse=True)
def no_live_access(monkeypatch, tmp_path):
    monkeypatch.setattr(database.psycopg2, "connect", benchmark.blocked)
    monkeypatch.setattr(database, "get_pg_pool", benchmark.blocked)
    monkeypatch.setattr(database, "DB_NAME", str(tmp_path / "not_used.db"))
    monkeypatch.setattr(database, "_pg_pool", None)
    monkeypatch.setenv("PG_POOL_MIN", "1")
    monkeypatch.setenv("PG_POOL_MAX", "1")


def native_row(**updates):
    result = dict(zip(COLUMNS, (
        "V1", date(2026, 1, 1), "110", "Bank اردو", "Original narration",
        100.0, 0.0, "bank", "Bank", "2026-01", "2026-01-H1", 1,
        "mapped", 8, 4, False,
    )))
    result.update(updates)
    return result


def envelope(method, rows):
    """Emulate the fixed SQL envelope, retaining exact numeric text/date types."""
    def value(key, item):
        if item is None:
            return None
        return str(item) if key in {
            "date", "debit", "credit", "source_line_count", "database_row_count",
            "database_voucher_count",
        } else item

    if method == "baseline":
        return rows
    if method == "array_rows":
        return [{"experiment_payload": [[value(key, row[key]) for key in COLUMNS] for row in rows]}]
    descriptions, encoded = [], []
    for row in rows:
        description = [row[key] for key in DESCRIPTORS]
        if description not in descriptions:
            descriptions.append(description)
        encoded.append([descriptions.index(description) + 1,
                        *[value(key, row[key]) for key in REMAINING]])
    return [{"experiment_payload": {"descriptors": descriptions, "rows": encoded}}]


@pytest.mark.parametrize("method", METHODS)
def test_transport_preserves_precision_multiplicity_nulls_zero_and_order(method):
    rows = [native_row(debit=5.239999999999997, credit=-0.0),
            native_row(debit=5.239999999999997, credit=-0.0),
            native_row(voucher_id="V2", account_name=None, debit=0.0),
            {key: None for key in COLUMNS}]
    rows[-1].update(database_totals_only=True, database_row_count=0, database_voucher_count=0)
    restored = decode_rows(method, envelope(method, rows))
    benchmark.assert_equal(rows, restored, "Typed transport")
    assert len(restored) == 4
    assert restored[0]["credit"].hex() == "-0x0.0p+0"


@pytest.mark.parametrize("method", METHODS)
def test_transport_handles_empty_rows(method):
    assert decode_rows(method, envelope(method, [])) == []


@pytest.mark.parametrize("method", METHODS)
def test_generated_query_wraps_source_without_changing_math_or_parameters(method):
    source = "WITH database_totals AS (SELECT %s AS n) SELECT * FROM database_totals"
    sql = transport_sql(source, method)
    benchmark.assert_select(sql)
    assert source in sql
    assert sql.count("%s") == 1
    if method != "baseline":
        assert "::text" in sql
        assert "ORDER BY b.date, b.voucher_id, b.account_code" in sql


def test_materialized_category_values_follow_current_mapping_only_for_counter_rows():
    rows = [
        {"derived_account_code": "210", "derived_classification_status": "non_treasury"},
        {"derived_account_code": "110", "derived_classification_status": "bank"},
        {"derived_account_code": "310", "derived_classification_status": "unmapped"},
    ]
    original = [dict(row) for row in rows]
    first = {"210": "Sales", "110": "Bank label"}
    assert materialize_category_values(rows, first) == ["Sales", "", ""]
    assert rows == original

    # Assignment/label edits are represented by taking a fresh current mapping.
    reassigned = {"210": "Other income", "110": "Bank label"}
    assert materialize_category_values(rows, reassigned) == ["Other income", "", ""]
    assert materialize_category_values(rows, {"110": "Bank label"}) == ["", "", ""]


def test_materialized_category_query_removes_both_live_joins():
    source = """WITH derived_lines AS (
        SELECT cd.id, COALESCE(category.label, '') AS category
        FROM client_data cd
            LEFT JOIN account_control_cashflow_category_assignments category_assignment
              ON category_assignment.client_id = cd.client_id
             AND category_assignment.account_code = cd.derived_account_code
            LEFT JOIN account_control_cashflow_categories category
              ON category.id = category_assignment.category_id
             AND category.client_id = cd.client_id
             AND category.is_active = TRUE
        WHERE cd.client_id = %s
    ) SELECT * FROM derived_lines"""
    candidate = materialized_category_sql(source)
    assert "COALESCE(cd.derived_category, '') AS category" in candidate
    assert "category_assignment" not in candidate
    assert "account_control_cashflow_categories" not in candidate
    assert candidate.count("%s") == source.count("%s")


def test_materialized_category_query_fails_closed_if_source_contract_changes():
    with pytest.raises(ValueError, match="SQL changed"):
        materialized_category_sql("SELECT cd.derived_category FROM client_data cd")


@pytest.mark.parametrize("sql", [
    "DELETE FROM client_data", "SELECT 1; DELETE FROM client_data",
    "WITH x AS (DELETE FROM client_data RETURNING *) SELECT * FROM x",
    "CREATE TEMP TABLE x AS SELECT 1", "SELECT nextval('source_id')",
    "SELECT setval('source_id', 1)", "SELECT 1 FOR UPDATE",
    "COPY client_data TO STDOUT", "SELECT pg_advisory_lock(1)",
])
def test_nonread_sql_is_refused(sql):
    with pytest.raises(benchmark.ExperimentError):
        benchmark.assert_select(sql)


@pytest.mark.parametrize("method,payload", [
    ("array_rows", [{"experiment_payload": [[1]]}]),
    ("array_rows", [{"experiment_payload": None}]),
    ("dictionary_rows", [{"experiment_payload": {"rows": [], "descriptors": None}}]),
    ("dictionary_rows", [{"experiment_payload": {"rows": [[0, *([None] * len(REMAINING))]], "descriptors": []}}]),
    ("dictionary_rows", [{"experiment_payload": {"rows": [[True, *([None] * len(REMAINING))]], "descriptors": [[None] * len(DESCRIPTORS)]}}]),
    ("baseline", [{"new_source_contract": True}]),
])
def test_malformed_or_changed_source_contracts_fail_closed(method, payload):
    with pytest.raises(ValueError):
        decode_rows(method, payload)


def test_default_cli_cannot_connect_or_call_live_runner(monkeypatch, capsys):
    monkeypatch.setattr(benchmark, "run_live", benchmark.blocked)
    assert benchmark.main([]) == 0
    assert json.loads(capsys.readouterr().out)["network_access"] is False


def test_cli_requires_scope_and_refuses_repository_or_existing_output(tmp_path):
    args = SimpleNamespace(live=True, client_id=5, start="2026-01-01", end="2026-06-30", repeats=2, output=None)
    benchmark.validate_arguments(args)
    for updates in ({"client_id": None}, {"client_id": -1}, {"start": None}, {"start": "wrong"},
                    {"end": "2025-01-01"}, {"repeats": 10},
                    {"output": str(benchmark.ROOT / "evidence.json")}):
        altered = SimpleNamespace(**{**vars(args), **updates})
        with pytest.raises(benchmark.ExperimentError):
            benchmark.validate_arguments(altered)
    target = tmp_path / "exists.json"
    target.write_text("protected", encoding="utf-8")
    args.output = str(target)
    with pytest.raises(benchmark.ExperimentError):
        benchmark.validate_arguments(args)
    assert target.read_text(encoding="utf-8") == "protected"


def test_local_fixture_mode_requires_external_csv_config_and_isolated_data_dir(tmp_path):
    csv_path = tmp_path / "client-data.csv"
    config_path = tmp_path / "account-control.json"
    data_dir = tmp_path / "cluster"
    csv_path.write_text("header", encoding="utf-8")
    config_path.write_text("{}", encoding="utf-8")
    data_dir.mkdir()
    args = SimpleNamespace(local_csv=str(csv_path), config_json=str(config_path),
        local_data_dir=str(data_dir), local_port=55431, repeats=2, client_id=5,
        start="2026-01-01", end="2026-06-30", output=None)
    benchmark.validate_local_fixture_arguments(args)
    for updates in ({"local_port": 0}, {"repeats": 4}, {"start": "wrong"},
                    {"local_data_dir": str(benchmark.ROOT)}):
        altered = SimpleNamespace(**{**vars(args), **updates})
        with pytest.raises(benchmark.ExperimentError):
            benchmark.validate_local_fixture_arguments(altered)


def test_sqlite_adapter_translates_only_the_known_cashflow_source_dialect():
    source = """WITH database_totals AS (
        SELECT COUNT(*) AS database_row_count FROM client_data cd WHERE cd.client_id = %s
    ), derived_lines AS (
        SELECT COALESCE(
            cd.row_data ->> 'Voucher Detail', cd.row_data ->> 'Detail',
            cd.row_data ->> 'Description', cd.row_data ->> 'Narration',
            cd.row_data ->> 'Remarks', ''
        ) AS voucher_detail,
        COALESCE(cd.derived_debit, 0)::double precision AS debit,
        BTRIM(cd.derived_account_code) AS account_code
        FROM client_data cd WHERE cd.is_deleted = FALSE AND cd.derived_treasury_type = 'cash'
    ), selected_vouchers AS (SELECT voucher_id FROM derived_lines), report_lines AS (
        SELECT * FROM derived_lines l JOIN selected_vouchers v ON v.voucher_id = l.voucher_id
        GROUP BY l.voucher_id
    ) SELECT * FROM report_lines"""
    translated = benchmark._sqlite_cashflow_sql(source)
    assert "json_extract(cd.row_data" in translated
    assert "->>" not in translated
    assert "::double precision" not in translated
    assert "BTRIM(" not in translated
    assert "cd.is_deleted = 0" in translated
    assert "l.voucher_id IN (SELECT voucher_id FROM selected_vouchers)" in translated
    assert "JOIN selected_vouchers" not in translated
    assert translated.count("?") == 1
    assert_select = benchmark.assert_select
    assert_select(translated)


def test_sqlite_report_source_and_materialized_category_have_exact_counter_category_parity():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    benchmark._create_sqlite_fixture_tables(conn)
    rows = [
        (1, 5, 1, 10, '{"Voucher Detail":"Receipt"}', 0, "active", "V1", "100", "Cash",
         "2026-01-05", "2026-01", "2026-01-H1", 100.0, 0.0, "cash", "mapped"),
        (2, 5, 1, 10, '{"Voucher Detail":"Receipt"}', 0, "active", "V1", "210", "Customer",
         "2026-01-05", "2026-01", "2026-01-H1", 0.0, 100.0, "", "non_treasury"),
    ]
    conn.executemany("""INSERT INTO client_data (
        id, client_id, template_id, upload_id, row_data, is_deleted, change_detection_status,
        derived_voucher_id, derived_account_code, derived_account_name, derived_transaction_date,
        derived_transaction_month, derived_transaction_fortnight, derived_debit, derived_credit,
        derived_treasury_type, derived_classification_status
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
    config_raw = {
        "client_id": 5,
        "categories": [{"id": 1, "label": "Sales", "is_active": True}],
        "accounts": [{"account_code": "210", "category_id": 1}],
        "settings": {"source_template_id": 1, "receipt_presentation": "net"},
        "treasury_accounts": {"cash": ["100"], "bank": []},
        "mapping_accounts": [],
    }
    benchmark._load_sqlite_category_config(conn, config_raw, 5)
    benchmark.refresh_sqlite_derived_categories(conn, 5, 1)
    config = benchmark._local_cashflow_config(config_raw)
    args = SimpleNamespace(client_id=5, start="2026-01-01", end="2026-06-30")
    baseline, _ = benchmark._run_sqlite_method(conn, args, config, "baseline")
    candidate, _ = benchmark._run_sqlite_method(conn, args, config, "derived_category")
    benchmark.assert_equal(baseline, candidate, "SQLite source and report")

    from services.ledger_report_source import load_cashflow_client_data_source

    source = load_cashflow_client_data_source(
        5, journal_template_id=1, period_start=args.start, period_end=args.end,
        cashflow_group_by="month", query_fn=lambda sql, params: [dict(row) for row in conn.execute(
            benchmark._sqlite_cashflow_sql(sql), params or ()).fetchall()],
    )
    counter = source.journal.loc[source.journal["account_code"] == "210", "category"].tolist()
    assert counter == ["Sales"]
    conn.close()


def test_local_report_configuration_uses_current_labels_and_vendor_scope():
    config = {
        "categories": [
            {"id": 1, "label": "Receipts", "is_active": True},
            {"id": 2, "label": "Retired", "is_active": False},
        ],
        "settings": {"source_template_id": 1, "receipt_presentation": "net",
                     "role_categories": {"customer_receipt": [1, 2]}},
        "treasury_accounts": {"cash": ["100"], "bank": ["110"]},
        "mapping_accounts": [
            {"account_code": "210", "assignment": {"verification_workstream": "local_vendor"}},
            {"account_code": "220", "assignment": {"verification_workstream": "cash"}},
        ],
    }
    result = benchmark._local_cashflow_config(config)
    assert result["customer_payment_roles"]["customer_receipt"] == ["Receipts"]
    assert result["vendor_account_codes"] == ["210"]
    assert result["source_template_id"] == 1


def test_fingerprints_reject_missing_duplicate_rounding_and_signed_zero_changes():
    original = [native_row(debit=5.239999999999997, credit=-0.0)] * 2
    for changed in (original[:1], [native_row(debit=5.24)] * 2,
                    [native_row(debit=5.239999999999997, credit=0.0)] * 2):
        with pytest.raises(benchmark.ExperimentError):
            benchmark.assert_equal(original, changed, "Source")


def test_replay_has_no_live_fallback_and_checks_scope_and_completion():
    captured = [("SELECT %s", (5,), [{"id": 5}])]
    replay, complete = benchmark.replay_query(captured)
    with pytest.raises(benchmark.ExperimentError):
        complete()
    with pytest.raises(benchmark.ExperimentError):
        replay("SELECT %s", (7,))
    assert replay("SELECT %s", (5,)) == [{"id": 5}]
    complete()
    with pytest.raises(benchmark.ExperimentError):
        replay("SELECT %s", (5,))


def test_safety_patches_block_writes_startup_and_extra_connections_then_restore():
    original = database.execute_write_pg
    with benchmark.safety_guards(database, reports, sources):
        for operation in (database.execute_write_pg, database.execute_write, database.log_audit,
                          database.init_db, database.init_db_postgres, database.get_db,
                          database.get_raw_conn_pg, database.pg_transaction,
                          reports.execute_write_pg, reports.log_audit, sources.execute_query_pg):
            with pytest.raises(benchmark.ExperimentError):
                operation()
    assert database.execute_write_pg is original


def install_fake_read_snapshot(monkeypatch, read_only="on", corrupt=None):
    """Run real orchestration/engine/export over synthetic canonical source rows."""
    source_rows = [
        native_row(),
        native_row(account_code="210", account_name="Customer", debit=0.0, credit=100.0,
                   treasury_type="", category="Sales", source_line_count=2),
        native_row(voucher_id="V2", account_code="110", debit=0.0, credit=50.0),
        native_row(voucher_id="V2", account_code="310", account_name="Expense", debit=50.0,
                   treasury_type="", category="Expenses"),
    ]
    config = {"mappings": [], "treasury": [{"account_code": "110", "treasury_type": "bank"}],
              "settings": {"source_template_id": 1}, "customer_payment_roles": [],
              "cashflow_report_revision": 3}
    calls, setups = [], []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql): setups.append(sql)
        def fetchone(self): return (read_only,)

    class Connection:
        def cursor(self): return Cursor()

    class Pool:
        closed = False
        def closeall(self): self.closed = True

    pool = Pool()

    def snapshot(callback):
        database._pg_pool = pool
        conn = Connection()
        token = database._pg_transaction.set(conn)
        try:
            return callback(conn)
        finally:
            database._pg_transaction.reset(token)

    def query(sql, params=None):
        benchmark.assert_select(sql)
        calls.append(sql)
        if "WITH database_totals AS" in sql:
            method = "dictionary_rows" if "benchmark_ranked" in sql else (
                "array_rows" if "experiment_payload" in sql else "baseline")
            payload = envelope(method, source_rows)
            if corrupt == method:
                payload = envelope(method, [source_rows[0]])
            return payload
        if "treasury_client_id" in sql:
            return [config]
        if "SELECT id FROM clients" in sql:
            return [{"id": 5}]
        raise AssertionError("Unexpected benchmark SQL")

    monkeypatch.setattr(database, "run_read_snapshot_pg", snapshot)
    monkeypatch.setattr(database, "execute_query_pg", query)
    return calls, setups, pool


def live_args():
    return argparse.Namespace(live=True, client_id=5, start="2026-01-01", end="2026-06-30", repeats=2, output=None)


def test_entire_offline_benchmark_proves_report_drilldown_and_sheet_parity(monkeypatch):
    calls, setups, pool = install_fake_read_snapshot(monkeypatch)
    evidence = benchmark.run_live(live_args())
    assert evidence["status"] == "comparisons_passed"
    assert len(calls) == 18  # three SELECTs x three methods x two rounds; no proof reads
    assert len(evidence["runs"]) == 6
    assert evidence["database_totals"]["row_count"] == 8
    assert evidence["database_totals"]["voucher_count"] == 4
    assert all(proof["worksheets"] == 8 and proof["exact_match"] for proof in evidence["proofs"].values())
    assert len({proof["full_business_sha256"] for proof in evidence["proofs"].values()}) == 1
    assert all("30s" in sql or "3s" in sql or sql == "SHOW transaction_read_only" for sql in setups)
    text = json.dumps(evidence)
    assert "Original narration" not in text and "Customer" not in text and "Bank اردو" not in text
    assert pool.closed


def test_live_runner_refuses_server_without_read_only_enforcement(monkeypatch):
    calls, _, pool = install_fake_read_snapshot(monkeypatch, read_only="off")
    with pytest.raises(benchmark.ExperimentError, match="read-only"):
        benchmark.run_live(live_args())
    assert calls == []
    assert pool.closed


def test_abandoned_snapshot_timings_are_not_mixed_into_completed_round(monkeypatch):
    calls, _, _ = install_fake_read_snapshot(monkeypatch)
    snapshot = database.run_read_snapshot_pg
    invocations = 0

    def retry_once(callback):
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            # Simulate a helper discarding an attempt before successful return.
            snapshot(callback)
        return snapshot(callback)

    monkeypatch.setattr(database, "run_read_snapshot_pg", retry_once)
    evidence = benchmark.run_live(live_args())
    assert len(calls) == 27
    assert len(evidence["runs"]) == 6
    assert sum(run["round"] == 1 for run in evidence["runs"]) == 3


def test_changed_transport_is_disqualified_instead_of_reported_as_success(monkeypatch):
    install_fake_read_snapshot(monkeypatch, corrupt="dictionary_rows")
    evidence = benchmark.run_live(live_args())
    assert evidence["status"] == "rejected_accuracy"
    assert any(item["method"] == "dictionary_rows" for item in evidence["failures"])
    assert evidence["proofs"]["dictionary_rows"] == {"exact_match": False, "disqualified": True}


def test_failure_output_does_not_expose_driver_sql_or_credentials(monkeypatch, capsys):
    def driver_failure(args):
        raise RuntimeError("postgresql://private:password@host SELECT private_transaction")
    monkeypatch.setattr(benchmark, "run_live", driver_failure)
    assert benchmark.main(["--live", "--client-id", "5", "--start", "2026-01-01", "--end", "2026-06-30"]) == 1
    error = capsys.readouterr().err
    assert "password" not in error and "private_transaction" not in error
    assert json.loads(error)["error_type"] == "RuntimeError"


def test_summary_exposes_first_run_bias_and_disqualifies_inaccurate_candidates():
    runs = [{"round": number, "method": method, "report_seconds": seconds,
             "source_query_seconds": seconds / 2, "transport_json_equivalent_bytes": 100}
            for number, values in ((1, (15.0, 6.0, 6.0)), (2, (6.4, 6.5, 6.0)))
            for method, seconds in zip(METHODS, values)]
    summary = benchmark.summarize_runs(runs, [{"method": "dictionary_rows"}])
    assert not summary["array_rows"]["faster_in_each_round"]
    dictionary = summary["dictionary_rows"]
    assert dictionary["paired_round_improvements"][0]["improvement_percent"] == 60
    assert dictionary["paired_round_improvements"][1]["improvement_percent"] == pytest.approx(6.25)
    assert not dictionary["accuracy_qualified"]
