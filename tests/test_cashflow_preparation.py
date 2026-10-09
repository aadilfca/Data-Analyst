import json
import sqlite3
import tempfile

import pytest

import database
import services.cashflow_preparation as preparation
from scripts.backfill_cashflow_prepared_read_model import run_client
from services.cashflow_preparation import (
    build_preparation_plan,
    cashflow_preparation_requires_recovery,
    enqueue_cashflow_preparation_job,
    get_cashflow_preparation_status,
    mark_cashflow_preparation_job_failed,
    prepare_client,
    prepare_incremental_category_client,
    prepare_incremental_client,
    prepare_incremental_treasury_client,
    process_cashflow_preparation_job,
    recover_cashflow_preparation_jobs_postgres,
    retry_cashflow_preparation_job,
)


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


def _add_voucher_lines(
    conn,
    *,
    template_id,
    occurrence_id,
    upload_id,
    voucher_id,
    first_client_data_id,
    add_upload=False,
    treasury=True,
):
    if add_upload:
        conn.execute(
            "INSERT INTO uploaded_files (id, file_name, file_path, client_id) VALUES (?, ?, ?, 1)",
            (upload_id, f"source-{upload_id}.xlsx", f"source-{upload_id}.xlsx"),
        )
    conn.execute(
        """INSERT INTO cashflow_voucher_occurrences
           (id, client_id, template_id, upload_id, voucher_id, voucher_identity,
            occurrence_key, voucher_fingerprint, source_line_count,
            decision_status, canonical_occurrence_id)
           VALUES (?, 1, ?, ?, ?, ?, ?, ?, 2, 'current', ?)""",
        (
            occurrence_id,
            template_id,
            upload_id,
            voucher_id,
            voucher_id,
            f"upload-{upload_id}:{voucher_id}",
            f"fingerprint-{occurrence_id}",
            occurrence_id,
        ),
    )
    rows = [
        (first_client_data_id, "BANK", "Bank", 100, 0, "bank", "bank"),
        (first_client_data_id + 1, "CUSTOMER", "Customer", 0, 100, None, "non_treasury"),
    ]
    for client_data_id, account_code, account_name, debit, credit, treasury_type, status in rows:
        conn.execute(
            "INSERT INTO client_data (id, client_id, template_id, upload_id, row_data, extract_date) VALUES (?, 1, ?, ?, ?, '2026-01-23')",
            (client_data_id, template_id, upload_id, json.dumps({"Voucher ID": voucher_id, "Account": account_code})),
        )
        conn.execute(
            """INSERT INTO cash_bank_transaction_classifications
               (client_data_id, client_id, template_id, upload_id, voucher_id,
                account_code, account_name, transaction_date, transaction_month,
                transaction_fortnight, debit, credit, treasury_type,
                configuration_version, classification_status, is_current)
               VALUES (?, 1, ?, ?, ?, ?, ?, '2026-01-23', '2026-01',
                       '2026-01-F2', ?, ?, ?, 1, ?, 1)""",
            (
                client_data_id,
                template_id,
                upload_id,
                voucher_id,
                account_code,
                account_name,
                debit,
                credit,
                treasury_type if treasury else None,
                status,
            ),
        )


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


def test_incremental_revision_replaces_revised_voucher_and_preserves_unaffected_lines(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            template_id = _seed_cashflow_fixture(conn)
            _add_voucher_lines(
                conn,
                template_id=template_id,
                occurrence_id=20,
                upload_id=1,
                voucher_id="V200",
                first_client_data_id=201,
            )
            conn.commit()
            prepare_client(conn, client_id=1, preparation_revision=1)
            conn.commit()
            raw_before = conn.execute("SELECT COUNT(*) FROM client_data").fetchone()[0]
            _add_voucher_lines(
                conn,
                template_id=template_id,
                occurrence_id=11,
                upload_id=2,
                voucher_id="V100",
                first_client_data_id=301,
                add_upload=True,
            )
            conn.execute(
                "UPDATE cashflow_voucher_occurrences SET decision_status = 'superseded', canonical_occurrence_id = 11 WHERE id = 10"
            )
            conn.commit()
            result = prepare_incremental_client(
                conn,
                client_id=1,
                template_id=template_id,
                affected_occurrence_ids=[10, 11],
                current_occurrence_ids=[11],
            )
            conn.commit()
            assert result["preparation_revision"] == 1
            assert result["prepared_line_count"] == 2
            rows = conn.execute(
                "SELECT canonical_occurrence_id, source_client_data_id FROM cashflow_prepared_lines WHERE preparation_revision = 1"
            ).fetchall()
            assert 10 not in {row[0] for row in rows}
            assert 11 in {row[0] for row in rows if row[1] in {301, 302}}
            assert 20 in {row[0] for row in rows}
            assert conn.execute(
                "SELECT COUNT(*) FROM cashflow_prepared_rollups WHERE preparation_revision = 1"
            ).fetchone()[0] == 4
            assert conn.execute(
                "SELECT COUNT(*) FROM cashflow_prepared_lines WHERE preparation_revision = 2"
            ).fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM client_data").fetchone()[0] == raw_before + 2
        finally:
            conn.close()


def test_incremental_ineligible_revision_removes_old_prepared_lines(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            template_id = _seed_cashflow_fixture(conn)
            prepare_client(conn, client_id=1, preparation_revision=1)
            conn.commit()
            conn.execute(
                "UPDATE cash_bank_transaction_classifications SET treasury_type = NULL, classification_status = 'non_treasury' WHERE client_data_id = 101"
            )
            conn.commit()
            result = prepare_incremental_client(
                conn,
                client_id=1,
                template_id=template_id,
                affected_occurrence_ids=[10],
                current_occurrence_ids=[10],
            )
            conn.commit()
            assert result["excluded_ineligible_occurrence_count"] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM cashflow_prepared_lines WHERE preparation_revision = 1"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT active_preparation_revision FROM cashflow_preparation_state WHERE client_id = 1"
            ).fetchone()[0] == 1
        finally:
            conn.close()


def test_incremental_noop_does_not_create_a_new_revision(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            template_id = _seed_cashflow_fixture(conn)
            prepare_client(conn, client_id=1, preparation_revision=1)
            conn.commit()
            result = prepare_incremental_client(
                conn,
                client_id=1,
                template_id=template_id,
                affected_occurrence_ids=[],
                current_occurrence_ids=[],
            )
            assert result["changed"] is False
            assert result["preparation_revision"] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM cashflow_prepared_lines WHERE preparation_revision = 1"
            ).fetchone()[0] == 3
            assert conn.execute(
                "SELECT COUNT(*) FROM cashflow_prepared_lines WHERE preparation_revision = 2"
            ).fetchone()[0] == 0
        finally:
            conn.close()


def test_incremental_category_change_updates_only_affected_account_lines(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            template_id = _seed_cashflow_fixture(conn)
            conn.execute(
                "INSERT INTO account_control_cashflow_categories (id, client_id, category_key, label) VALUES (2, 1, 'tax_payments', 'Tax payments')"
            )
            prepare_client(conn, client_id=1, preparation_revision=1)
            conn.commit()
            raw_before = conn.execute("SELECT row_data FROM client_data WHERE id = 102").fetchone()[0]
            conn.execute(
                "UPDATE account_control_cashflow_category_assignments SET category_id = 2 WHERE client_id = 1 AND account_code = 'CUSTOMER'"
            )
            conn.commit()

            result = prepare_incremental_category_client(
                conn,
                client_id=1,
                account_codes=["CUSTOMER"],
            )
            conn.commit()

            assert result["preparation_revision"] == 1
            customer = conn.execute(
                """SELECT category_key, category_label_snapshot
                   FROM cashflow_prepared_lines
                   WHERE preparation_revision = 1 AND account_code = 'CUSTOMER'"""
            ).fetchone()
            bank = conn.execute(
                """SELECT category_key, category_label_snapshot
                   FROM cashflow_prepared_lines
                   WHERE preparation_revision = 1 AND account_code = 'BANK'"""
            ).fetchone()
            assert tuple(customer) == ("tax_payments", "Tax payments")
            assert tuple(bank) == ("__unassigned__", "")
            assert conn.execute(
                """SELECT COUNT(*) FROM cashflow_prepared_rollups
                   WHERE preparation_revision = 1 AND category_key = 'customer_receipts'"""
            ).fetchone()[0] == 0
            assert conn.execute("SELECT row_data FROM client_data WHERE id = 102").fetchone()[0] == raw_before
        finally:
            conn.close()


def test_incremental_treasury_change_removes_all_lines_for_now_ineligible_voucher(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            template_id = _seed_cashflow_fixture(conn)
            prepare_client(conn, client_id=1, preparation_revision=1)
            conn.commit()
            conn.execute(
                "UPDATE cash_bank_transaction_classifications SET treasury_type = NULL, classification_status = 'non_treasury' WHERE client_data_id = 101"
            )
            conn.commit()

            result = prepare_incremental_treasury_client(
                conn,
                client_id=1,
                account_codes=["BANK"],
            )
            conn.commit()

            assert result["preparation_revision"] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM cashflow_prepared_lines WHERE preparation_revision = 1"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT active_preparation_revision FROM cashflow_preparation_state WHERE client_id = 1"
            ).fetchone()[0] == 1
        finally:
            conn.close()


def test_incremental_treasury_change_adds_all_lines_for_newly_eligible_voucher(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            template_id = _seed_cashflow_fixture(conn, include_treasury=False)
            prepare_client(conn, client_id=1, preparation_revision=1)
            conn.commit()
            conn.execute(
                "UPDATE cash_bank_transaction_classifications SET treasury_type = 'bank', classification_status = 'bank' WHERE client_data_id = 101"
            )
            conn.commit()

            result = prepare_incremental_treasury_client(
                conn,
                client_id=1,
                account_codes=["BANK"],
            )
            conn.commit()

            assert result["preparation_revision"] == 1
            assert {
                row[0] for row in conn.execute(
                    "SELECT source_client_data_id FROM cashflow_prepared_lines WHERE preparation_revision = 1"
                ).fetchall()
            } == {101, 102, 103}
        finally:
            conn.close()


def test_durable_preparation_job_processes_and_publishes_revision(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            _seed_cashflow_fixture(conn)
            prepare_client(conn, client_id=1, preparation_revision=1)
            conn.commit()
            conn.execute(
                "UPDATE cashflow_prepared_lines SET category_key = 'old_category' WHERE account_code = 'CUSTOMER' AND preparation_revision = 1"
            )
            conn.execute(
                "UPDATE account_control_cashflow_category_assignments SET category_id = NULL WHERE account_code = 'CUSTOMER'"
            )
            conn.commit()
            job = enqueue_cashflow_preparation_job(
                conn,
                client_id=1,
                reason="category_assignment",
                affected_account_codes=["CUSTOMER"],
            )
            assert get_cashflow_preparation_status(lambda sql, params: [dict(row) for row in conn.execute(sql.replace('%s', '?'), params or ()).fetchall()], client_id=1)["status"] == "updating"
            result = process_cashflow_preparation_job(conn, job_id=job["job_id"])
            conn.commit()

            assert result["status"] == "completed"
            status = get_cashflow_preparation_status(lambda sql, params: [dict(row) for row in conn.execute(sql.replace('%s', '?'), params or ()).fetchall()], client_id=1)
            assert status["status"] == "ready"
            assert status["active_preparation_revision"] == 1
            assert conn.execute(
                "SELECT status FROM cashflow_preparation_jobs WHERE id = ?", (job["job_id"],)
            ).fetchone()[0] == "completed"
        finally:
            conn.close()


def test_no_prepared_change_job_returns_state_to_ready(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            _seed_cashflow_fixture(conn)
            prepare_client(conn, client_id=1, preparation_revision=1)
            conn.commit()
            job = enqueue_cashflow_preparation_job(
                conn,
                client_id=1,
                reason="category_assignment",
                affected_account_codes=["UNTOUCHED"],
            )
            result = process_cashflow_preparation_job(conn, job_id=job["job_id"])
            conn.commit()

            assert result["status"] == "completed"
            assert result["changed"] is False
            assert tuple(conn.execute(
                "SELECT active_preparation_revision, requested_preparation_revision, status FROM cashflow_preparation_state WHERE client_id = 1"
            ).fetchone()) == (1, 1, "ready")
        finally:
            conn.close()


def test_treasury_incremental_failure_falls_back_to_full_preparation(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            _seed_cashflow_fixture(conn)
            prepare_client(conn, client_id=1, preparation_revision=1)
            conn.commit()
            job = enqueue_cashflow_preparation_job(
                conn,
                client_id=1,
                reason="treasury_accounts",
                affected_account_codes=["BANK"],
            )
            conn.execute("CREATE TABLE treasury_savepoint_probe (value TEXT)")

            def fail_after_a_write(connection, **kwargs):
                connection.execute(
                    "INSERT INTO treasury_savepoint_probe (value) VALUES (?)",
                    ("must be rolled back",),
                )
                raise TypeError("object is not iterable")

            monkeypatch.setattr(
                preparation,
                "prepare_incremental_treasury_client",
                fail_after_a_write,
            )

            result = process_cashflow_preparation_job(conn, job_id=job["job_id"])
            conn.commit()

            assert result["status"] == "completed"
            assert result["reason"] == "treasury_accounts_full_rebuild"
            assert tuple(conn.execute(
                "SELECT active_preparation_revision, status FROM cashflow_preparation_state WHERE client_id = 1"
            ).fetchone()) == (1, "ready")
            assert conn.execute("SELECT COUNT(*) FROM treasury_savepoint_probe").fetchone()[0] == 0
        finally:
            conn.close()


def test_enqueue_accepts_nullable_or_scalar_job_scopes(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            _seed_cashflow_fixture(conn)
            job = preparation.enqueue_cashflow_preparation_job(
                conn,
                client_id=1,
                reason="incremental_upload",
                affected_upload_ids=101,
                affected_occurrence_ids=None,
                current_occurrence_ids=None,
                category_ids=None,
            )
            conn.commit()

            stored = conn.execute(
                "SELECT affected_upload_ids, affected_voucher_identities FROM cashflow_preparation_jobs WHERE id = ?",
                (job["job_id"],),
            ).fetchone()
            assert json.loads(stored[0]) == [101]
            assert json.loads(stored[1]) == {
                "affected_occurrence_ids": [],
                "current_occurrence_ids": [],
                "category_ids": [],
            }
        finally:
            conn.close()


def test_runner_persists_failure_and_leaves_connection_usable(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()

        class _Transaction:
            def __enter__(self):
                return conn

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type:
                    conn.rollback()
                else:
                    conn.commit()

        try:
            _seed_cashflow_fixture(conn)
            prepare_client(conn, client_id=1, preparation_revision=1)
            conn.commit()
            job = enqueue_cashflow_preparation_job(
                conn,
                client_id=1,
                reason="unsupported_change",
            )
            conn.commit()
            monkeypatch.setattr(database, "pg_transaction", lambda: _Transaction())

            def fail_build(*args, **kwargs):
                raise RuntimeError("simulated preparation failure")

            monkeypatch.setattr(preparation, "prepare_client", fail_build)

            result = preparation.run_cashflow_preparation_job_postgres(job["job_id"])

            assert result["status"] == "failed"
            assert conn.execute(
                "SELECT status, error_details FROM cashflow_preparation_jobs WHERE id = ?",
                (job["job_id"],),
            ).fetchone()[0] == "failed"
            assert tuple(conn.execute(
                "SELECT active_preparation_revision, status FROM cashflow_preparation_state WHERE client_id = 1"
            ).fetchone()) == (1, "failed")
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        finally:
            conn.close()

def test_first_treasury_change_bootstraps_missing_preparation_revision(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            _seed_cashflow_fixture(conn)
            conn.execute(
                "UPDATE cash_bank_transaction_classifications SET treasury_type = NULL, classification_status = 'non_treasury' WHERE client_data_id = 101"
            )
            conn.commit()
            job = enqueue_cashflow_preparation_job(
                conn,
                client_id=1,
                reason="treasury_accounts",
                affected_account_codes=["BANK"],
            )

            result = process_cashflow_preparation_job(conn, job_id=job["job_id"])
            conn.commit()

            assert result["status"] == "completed"
            assert result["preparation_revision"] == 1
            assert tuple(conn.execute(
                "SELECT active_preparation_revision, status FROM cashflow_preparation_state WHERE client_id = 1"
            ).fetchone()) == (1, "ready")
            assert conn.execute(
                "SELECT COUNT(*) FROM cashflow_prepared_lines WHERE client_id = 1 AND preparation_revision = 1"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT status FROM cashflow_preparation_jobs WHERE id = ?", (job["job_id"],)
            ).fetchone()[0] == "completed"
        finally:
            conn.close()


def test_persisted_failed_state_can_be_recovered_by_a_full_preparation_job(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            _seed_cashflow_fixture(conn)
            conn.execute(
                """INSERT INTO cashflow_preparation_state
                   (client_id, active_preparation_revision, requested_preparation_revision, status, last_error)
                   VALUES (1, 0, 1, 'failed', 'active_revision_missing')"""
            )
            conn.commit()
            assert cashflow_preparation_requires_recovery(
                lambda sql, params: [
                    dict(row) for row in conn.execute(sql.replace('%s', '?'), params or ()).fetchall()
                ],
                client_id=1,
            )

            job = enqueue_cashflow_preparation_job(
                conn,
                client_id=1,
                reason="preparation_recovery",
            )
            result = process_cashflow_preparation_job(conn, job_id=job["job_id"])
            conn.commit()

            assert result["status"] == "completed"
            assert tuple(conn.execute(
                "SELECT active_preparation_revision, status FROM cashflow_preparation_state WHERE client_id = 1"
            ).fetchone()) == (1, "ready")
            assert conn.execute(
                "SELECT COUNT(*) FROM cashflow_prepared_lines WHERE client_id = 1 AND preparation_revision = 1"
            ).fetchone()[0] == 3
        finally:
            conn.close()


def test_failed_preparation_job_keeps_previous_active_revision(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            _seed_cashflow_fixture(conn)
            prepare_client(conn, client_id=1, preparation_revision=1)
            conn.commit()
            job = enqueue_cashflow_preparation_job(
                conn,
                client_id=1,
                reason="unsupported_change",
            )
            conn.commit()
            with pytest.raises(RuntimeError, match="simulated preparation failure"):
                raise RuntimeError("simulated preparation failure")
            conn.rollback()
            mark_cashflow_preparation_job_failed(conn, job_id=job["job_id"], error="simulated preparation failure")
            conn.commit()

            state = conn.execute(
                "SELECT active_preparation_revision, status, last_error FROM cashflow_preparation_state WHERE client_id = 1"
            ).fetchone()
            assert tuple(state) == (1, "failed", "simulated preparation failure")
            assert conn.execute(
                "SELECT status FROM cashflow_preparation_jobs WHERE id = ?", (job["job_id"],)
            ).fetchone()[0] == "failed"
            with pytest.raises(LookupError):
                retry_cashflow_preparation_job(conn, job_id=job["job_id"], client_id=2)
            retry = retry_cashflow_preparation_job(conn, job_id=job["job_id"])
            conn.commit()
            assert retry["status"] == "pending"
            retry_result = process_cashflow_preparation_job(conn, job_id=job["job_id"])
            conn.commit()
            assert retry_result["status"] == "completed"
            assert tuple(conn.execute(
                "SELECT active_preparation_revision, status FROM cashflow_preparation_state WHERE client_id = 1"
            ).fetchone()) == (1, "ready")
        finally:
            conn.close()


def test_startup_recovery_requeues_only_latest_failed_treasury_job(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()

        class _Transaction:
            def __enter__(self):
                return conn

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type:
                    conn.rollback()
                else:
                    conn.commit()

        try:
            _seed_cashflow_fixture(conn)
            conn.execute(
                """INSERT INTO cashflow_preparation_state
                   (client_id, active_preparation_revision, requested_preparation_revision, status)
                   VALUES (1, 1, 3, 'failed')"""
            )
            conn.execute(
                """INSERT INTO cashflow_preparation_jobs
                   (client_id, reason, affected_account_codes, affected_voucher_identities,
                    requested_preparation_revision, status, retry_count)
                   VALUES (1, 'treasury_accounts', '[\"OLD\"]', '{}', 2, 'failed', 0)"""
            )
            conn.execute(
                """INSERT INTO cashflow_preparation_jobs
                   (client_id, reason, affected_account_codes, affected_voucher_identities,
                    requested_preparation_revision, status, retry_count)
                   VALUES (1, 'treasury_accounts', '[\"NEW\"]', '{}', 3, 'failed', 0)"""
            )
            conn.commit()
            monkeypatch.setattr(database, "pg_transaction", lambda: _Transaction())
            monkeypatch.setattr(
                preparation,
                "run_cashflow_preparation_job_postgres",
                lambda job_id: {"job_id": job_id, "status": "completed"},
            )

            results = recover_cashflow_preparation_jobs_postgres()

            latest_id = conn.execute(
                "SELECT id FROM cashflow_preparation_jobs WHERE requested_preparation_revision = 3"
            ).fetchone()[0]
            assert results == [{"job_id": latest_id, "status": "completed"}]
            assert tuple(conn.execute(
                "SELECT status, retry_count FROM cashflow_preparation_jobs WHERE requested_preparation_revision = 2"
            ).fetchone()) == ("failed", 0)
            assert tuple(conn.execute(
                "SELECT status, retry_count FROM cashflow_preparation_jobs WHERE requested_preparation_revision = 3"
            ).fetchone()) == ("pending", 1)
        finally:
            conn.close()


def test_startup_recovery_never_leaves_runner_exception_pending(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()

        class _Transaction:
            def __enter__(self):
                return conn

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type:
                    conn.rollback()
                else:
                    conn.commit()

        try:
            _seed_cashflow_fixture(conn)
            conn.execute(
                """INSERT INTO cashflow_preparation_state
                   (client_id, active_preparation_revision, requested_preparation_revision, status)
                   VALUES (1, 1, 2, 'updating')"""
            )
            conn.execute(
                """INSERT INTO cashflow_preparation_jobs
                   (client_id, reason, affected_account_codes, affected_voucher_identities,
                    requested_preparation_revision, status, retry_count)
                   VALUES (1, 'treasury_accounts', '[\"BANK\"]', '{}', 2, 'pending', 0)"""
            )
            conn.commit()
            monkeypatch.setattr(database, "pg_transaction", lambda: _Transaction())
            monkeypatch.setattr(
                preparation,
                "run_cashflow_preparation_job_postgres",
                lambda job_id: (_ for _ in ()).throw(RuntimeError("runner stopped before execution")),
            )

            results = recover_cashflow_preparation_jobs_postgres()

            assert results[0]["status"] == "failed"
            assert conn.execute(
                "SELECT status, error_details FROM cashflow_preparation_jobs"
            ).fetchone()[0] == "failed"
            assert conn.execute(
                "SELECT status, last_error FROM cashflow_preparation_state"
            ).fetchone()[0] == "failed"
        finally:
            conn.close()
