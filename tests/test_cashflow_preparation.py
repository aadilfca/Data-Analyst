import json
import sqlite3
import tempfile

import pytest

import database
from scripts.backfill_cashflow_prepared_read_model import run_client
from services.cashflow_preparation import build_preparation_plan, prepare_client


def _seed_cashflow_fixture(conn, *, include_treasury=True):
    template_id = conn.execute(
        "SELECT id FROM templates WHERE template_name = 'Transaction Report'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO clients (id, client_code, client_name) VALUES (1, 'C1', 'Client One')"
    )
    conn.execute(
        "INSERT INTO uploaded_files (id, file_name, file_path, client_id) VALUES (1, 'source.xlsx', 'source.xlsx', 1)"
    )
    conn.execute(
        "INSERT INTO account_control_cashflow_settings (client_id, source_template_id) VALUES (1, ?)"
        , (template_id,)
    )
    conn.execute(
        "INSERT INTO account_control_cashflow_categories (id, client_id, category_key, label) VALUES (1, 1, 'customer_receipts', 'Customer receipts')"
    )
    conn.execute(
        """INSERT INTO account_control_cashflow_category_assignments
           (client_id, account_code, category_id) VALUES (1, 'CUSTOMER', 1)"""
    )
    conn.execute(
        """INSERT INTO cashflow_voucher_occurrences
           (id, client_id, template_id, upload_id, voucher_id, voucher_identity,
            occurrence_key, voucher_fingerprint, source_line_count,
            decision_status, canonical_occurrence_id)
           VALUES (10, 1, ?, 1, 'V100', 'V100', 'upload-1:V100', 'fingerprint', 3,
                   'current', 10)""",
        (template_id,),
    )
    rows = [
        (101, "BANK", "Bank", 100, 0, "bank", "bank"),
        (102, "CUSTOMER", "Customer", 0, 100, None, "non_treasury"),
        (103, "TAX", "Tax", 0, 0, None, "non_treasury"),
    ]
    for client_data_id, account_code, account_name, debit, credit, treasury_type, status in rows:
        conn.execute(
            "INSERT INTO client_data (id, client_id, template_id, upload_id, row_data, extract_date) VALUES (?, 1, ?, 1, ?, '2026-01-23')",
            (client_data_id, template_id, json.dumps({"Voucher ID": "V100", "Account": account_code})),
        )
        conn.execute(
            """INSERT INTO cash_bank_transaction_classifications
               (client_data_id, client_id, template_id, upload_id, voucher_id,
                account_code, account_name, transaction_date, transaction_month,
                transaction_fortnight, debit, credit, treasury_type,
                configuration_version, classification_status, is_current)
               VALUES (?, 1, ?, 1, 'V100', ?, ?, '2026-01-23', '2026-01',
                       '2026-01-F2', ?, ?, ?, 1, ?, 1)""",
            (client_data_id, template_id, account_code, account_name, debit, credit, treasury_type, status),
        )
    if not include_treasury:
        conn.execute(
            "UPDATE cash_bank_transaction_classifications SET treasury_type = NULL, classification_status = 'non_treasury' WHERE client_data_id = 101"
        )
    conn.commit()
    return template_id


def test_plan_includes_all_lines_of_an_eligible_voucher_and_maps_counter_accounts(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            _seed_cashflow_fixture(conn)
            plan = build_preparation_plan(conn, client_id=1)
            assert plan["ready"] is True
            assert plan["eligible_occurrence_count"] == 1
            assert plan["prepared_line_count"] == 3
            assert {line["source_client_data_id"] for line in plan["prepared_lines"]} == {101, 102, 103}
            customer_line = next(line for line in plan["prepared_lines"] if line["account_code"] == "CUSTOMER")
            assert customer_line["category_key"] == "customer_receipts"
            assert customer_line["category_label_snapshot"] == "Customer receipts"
            assert customer_line["net_amount"] == -100
            month_rollup = next(rollup for rollup in plan["rollups"] if rollup["period_grain"] == "month" and rollup["account_code"] == "CUSTOMER")
            assert month_rollup["line_count"] == 1
            assert month_rollup["voucher_count"] == 1
        finally:
            conn.close()


def test_ineligible_voucher_is_not_prepared(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            _seed_cashflow_fixture(conn, include_treasury=False)
            plan = build_preparation_plan(conn, client_id=1)
            assert plan["eligible_occurrence_count"] == 0
            assert plan["excluded_ineligible_occurrence_count"] == 1
            assert plan["prepared_lines"] == []
        finally:
            conn.close()


def test_preparation_revision_rebuild_is_idempotent_and_preserves_raw_rows(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            _seed_cashflow_fixture(conn)
            raw_before = conn.execute("SELECT COUNT(*) FROM client_data").fetchone()[0]
            first = prepare_client(conn, client_id=1, preparation_revision=7)
            conn.commit()
            second = prepare_client(conn, client_id=1, preparation_revision=7)
            conn.commit()
            assert first["preparation_revision"] == second["preparation_revision"] == 7
            assert conn.execute(
                "SELECT COUNT(*) FROM cashflow_prepared_lines WHERE client_id = 1 AND preparation_revision = 7"
            ).fetchone()[0] == 3
            assert conn.execute(
                "SELECT COUNT(*) FROM cashflow_prepared_rollups WHERE client_id = 1 AND preparation_revision = 7"
            ).fetchone()[0] == 6
            state = conn.execute(
                "SELECT active_preparation_revision, status, affected_row_count FROM cashflow_preparation_state WHERE client_id = 1"
            ).fetchone()
            assert tuple(state) == (7, "ready", 3)
            assert conn.execute("SELECT COUNT(*) FROM client_data").fetchone()[0] == raw_before
        finally:
            conn.close()


def test_unconfigured_client_returns_without_writing(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            conn.execute("INSERT INTO clients (id, client_code, client_name) VALUES (1, 'C1', 'Client One')")
            conn.commit()
            result = prepare_client(conn, client_id=1)
            assert result["ready"] is False
            assert result["reason"] == "template_not_configured"
            assert conn.execute("SELECT COUNT(*) FROM cashflow_preparation_state").fetchone()[0] == 0
        finally:
            conn.close()


def test_operator_runner_is_dry_by_default_and_applies_requested_revision(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            _seed_cashflow_fixture(conn)
            dry_run = run_client(conn, client_id=1)
            assert dry_run["mode"] == "dry_run"
            assert conn.execute("SELECT COUNT(*) FROM cashflow_prepared_lines").fetchone()[0] == 0
            applied = run_client(conn, client_id=1, apply=True, preparation_revision=11)
            conn.commit()
            assert applied["mode"] == "apply"
            assert applied["preparation_revision"] == 11
            assert conn.execute("SELECT COUNT(*) FROM cashflow_prepared_lines").fetchone()[0] == 3
        finally:
            conn.close()
