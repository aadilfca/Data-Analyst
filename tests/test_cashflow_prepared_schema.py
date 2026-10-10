import sqlite3
import tempfile

import database
import pytest


def _columns(conn, table_name):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def test_prepared_schema_is_idempotent_and_has_required_keys(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        database.init_db()
        conn = database.get_db()
        try:
            expected = {
                "cashflow_prepared_lines": {
                    "client_id",
                    "preparation_revision",
                    "canonical_occurrence_id",
                    "source_client_data_id",
                    "category_key",
                    "transaction_month",
                    "transaction_fortnight",
                },
                "cashflow_prepared_rollups": {
                    "client_id",
                    "preparation_revision",
                    "period_grain",
                    "period_key",
                    "category_key",
                    "net_total",
                },
                "cashflow_preparation_state": {
                    "client_id",
                    "active_preparation_revision",
                    "requested_preparation_revision",
                    "status",
                },
                "cashflow_preparation_jobs": {
                    "client_id",
                    "requested_preparation_revision",
                    "status",
                    "retry_count",
                },
            }
            for table_name, columns in expected.items():
                assert columns <= _columns(conn, table_name)
                assert conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table_name,),
                ).fetchone()[0] == 1

            line_indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list(cashflow_prepared_lines)").fetchall()
            }
            assert any("revision" in index_name for index_name in line_indexes)
        finally:
            conn.close()


def test_prepared_lines_preserve_repeated_source_lines_and_do_not_touch_raw_rows(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
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
                """INSERT INTO cashflow_voucher_occurrences
                   (id, client_id, template_id, upload_id, occurrence_key, voucher_fingerprint,
                    decision_status, canonical_occurrence_id)
                   VALUES (10, 1, ?, 1, 'v1', 'fingerprint', 'current', 10)""",
                (template_id,),
            )
            conn.execute(
                "INSERT INTO client_data (id, client_id, template_id, upload_id, row_data) VALUES (1001, 1, ?, 1, '{}')",
                (template_id,),
            )
            raw_before = conn.execute("SELECT COUNT(*) FROM client_data").fetchone()[0]
            line = {
                "client_id": 1,
                "template_id": template_id,
                "preparation_revision": 7,
                "canonical_occurrence_id": 10,
                "upload_id": 1,
                "source_client_data_id": 1001,
                "account_code": "BANK-01",
                "account_name": "Bank",
                "debit": 100,
                "credit": 0,
                "net_amount": 100,
                "treasury_type": "Bank",
                "category_key": "bank",
                "classification_status": "classified",
            }
            columns = ", ".join(line)
            placeholders = ", ".join("?" for _ in line)
            conn.execute(
                f"INSERT INTO cashflow_prepared_lines ({columns}) VALUES ({placeholders})",
                tuple(line.values()),
            )
            repeated_line = dict(line, source_client_data_id=1002, account_code="BANK-01")
            conn.execute(
                f"INSERT INTO cashflow_prepared_lines ({columns}) VALUES ({placeholders})",
                tuple(repeated_line.values()),
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    f"INSERT INTO cashflow_prepared_lines ({columns}) VALUES ({placeholders})",
                    tuple(line.values()),
                )
            conn.commit()
            assert conn.execute("SELECT COUNT(*) FROM cashflow_prepared_lines").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM client_data").fetchone()[0] == raw_before
        finally:
            conn.close()


def test_preparation_state_and_jobs_have_idempotent_identity_keys(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setattr(database, "DB_NAME", f"{temp_dir}/cashflows.sqlite")
        database.init_db()
        conn = database.get_db()
        try:
            conn.execute("INSERT INTO cashflow_preparation_state (client_id) VALUES (1)")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO cashflow_preparation_state (client_id) VALUES (1)")
            conn.execute(
                "INSERT INTO cashflow_preparation_jobs (client_id, reason, requested_preparation_revision) VALUES (1, 'upload', 2)"
            )
            conn.execute(
                "INSERT INTO cashflow_preparation_jobs (client_id, reason, requested_preparation_revision) VALUES (1, 'upload', 2)"
            )
            assert conn.execute(
                "SELECT COUNT(*) FROM cashflow_preparation_jobs"
            ).fetchone()[0] == 2
        finally:
            conn.close()
