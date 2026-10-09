import json
import os
import sqlite3
import tempfile
import unittest

import database
from services.cash_bank_classification import (
    backfill_account_names_postgres,
    backfill_account_names_sqlite,
    backfill_postgres,
    backfill_sqlite,
    build_classification_row,
)


class _PostgresBackfillCursor:
    def __init__(self, source_rows):
        self.source_rows = source_rows
        self.rows = []
        self.source_index = 0
        self.mode = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, params=None):
        if "FROM account_control_cashflow_treasury_accounts" in statement:
            self.mode = "mapping"
            self.rows = [{"account_code": "1000", "treasury_type": "cash"}]
        elif "SELECT id, client_id, template_id, upload_id" in statement:
            self.mode = "source"
            self.source_index = 0
        elif "SELECT c.client_data_id" in statement:
            self.mode = "account_names"
            self.source_index = 0
        elif "SELECT client_data_id" in statement:
            self.mode = "existing"
            self.rows = []
        else:
            raise AssertionError(f"Unexpected PostgreSQL statement: {statement}")

    def fetchall(self):
        return self.rows

    def fetchmany(self, size):
        if self.mode == "account_names":
            batch = self.source_rows[self.source_index:self.source_index + size]
            self.source_index += len(batch)
            return [
                {"client_data_id": row["id"], "row_data": row["row_data"]}
                for row in batch
            ]
        if self.mode != "source":
            return []
        batch = self.source_rows[self.source_index:self.source_index + size]
        self.source_index += len(batch)
        return batch

    def executemany(self, statement, values):
        raise AssertionError("dry-run should not write derived classifications")


class _PostgresBackfillConnection:
    def __init__(self, source_rows):
        self.source_rows = source_rows
        self.commit_count = 0
        self.rollback_count = 0

    def cursor(self, *args, **kwargs):
        return _PostgresBackfillCursor(self.source_rows)

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1


class CashBankTransactionBackfillTests(unittest.TestCase):
    def setUp(self):
        self.original_db_name = database.DB_NAME
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_NAME = os.path.join(self.temp_dir.name, "phase3.sqlite")
        database.init_db()
        self.connection = sqlite3.connect(database.DB_NAME)
        self.connection.row_factory = sqlite3.Row
        self._seed_rows()

    def tearDown(self):
        self.connection.close()
        database.DB_NAME = self.original_db_name
        self.temp_dir.cleanup()

    def _seed_rows(self):
        self.connection.execute(
            "INSERT INTO clients (client_code, client_name) VALUES (?, ?)",
            ("P3", "Phase 3 Test Client"),
        )
        client_id = self.connection.execute(
            "SELECT id FROM clients WHERE client_code = 'P3'"
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO templates (template_name) VALUES (?)",
            ("Phase 3 Template",),
        )
        template_id = self.connection.execute(
            "SELECT id FROM templates WHERE template_name = 'Phase 3 Template'"
        ).fetchone()[0]
        self.connection.execute(
            """
            INSERT INTO uploaded_files
                (file_name, file_path, file_type, client_id)
            VALUES (?, ?, ?, ?)
            """,
            ("phase3.xlsx", "phase3.xlsx", "xlsx", client_id),
        )
        upload_id = self.connection.execute(
            "SELECT id FROM uploaded_files WHERE file_name = 'phase3.xlsx'"
        ).fetchone()[0]
        self.connection.executemany(
            """
            INSERT INTO account_control_cashflow_treasury_accounts
                (client_id, account_code, treasury_type)
            VALUES (?, ?, ?)
            """,
            [
                (client_id, "1000", "cash"),
                (client_id, "2000", "bank"),
            ],
        )
        source_rows = [
            {
                "Voucher ID": "V-001",
                "Account Name": "Cash account",
                "Account Detail": "1000",
                "Date": "2024-01-02",
                "Debit Amount": "100.50",
                "Credit Amount": "0",
            },
            {
                "Voucher ID": "V-001",
                "Account Name": "Bank account",
                "Account Detail": "2000",
                "Date": "2024-01-02",
                "Debit Amount": "0",
                "Credit Amount": "100.50",
            },
            {
                "Voucher ID": "V-002",
                "Account Name": "Expense account",
                "Account Detail": "3000",
                "Date": "2024-01-03",
                "Debit Amount": "25",
                "Credit Amount": "0",
            },
            {
                "Voucher ID": "V-003",
                "Description": "Missing account mapping",
                "Debit Amount": "10",
                "Credit Amount": "0",
            },
        ]
        self.connection.executemany(
            """
            INSERT INTO client_data
                (client_id, template_id, upload_id, row_data, extract_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    client_id,
                    template_id,
                    upload_id,
                    json.dumps(row),
                    "2024-01-02",
                )
                for row in source_rows
            ],
        )
        self.connection.commit()
        self.client_id = client_id

    def test_backfill_classifies_lines_and_preserves_raw_rows(self):
        before = self.connection.execute(
            "SELECT id, row_data FROM client_data ORDER BY id"
        ).fetchall()

        first = backfill_sqlite(
            self.connection,
            client_id=self.client_id,
            configuration_version=1,
            batch_size=2,
        )

        self.assertEqual(first.source_rows, 4)
        self.assertEqual(first.inserted_rows, 4)
        self.assertEqual(first.cash_rows, 1)
        self.assertEqual(first.bank_rows, 1)
        self.assertEqual(first.non_treasury_rows, 1)
        self.assertEqual(first.unmapped_rows, 1)

        rows = self.connection.execute(
            """
            SELECT voucher_id, account_code, account_name, transaction_month, transaction_fortnight,
                   treasury_type, classification_status, debit, credit, configuration_version
            FROM cash_bank_transaction_classifications
            ORDER BY client_data_id
            """
        ).fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual(dict(rows[0]), {
            "voucher_id": "V-001",
            "account_code": "1000",
            "account_name": "Cash account",
            "transaction_month": "2024-01",
            "transaction_fortnight": "2024-01-F1",
            "treasury_type": "cash",
            "classification_status": "cash",
            "debit": 100.5,
            "credit": 0.0,
            "configuration_version": 1,
        })
        self.assertEqual(rows[1]["treasury_type"], "bank")
        self.assertEqual(rows[2]["classification_status"], "non_treasury")
        self.assertEqual(rows[3]["classification_status"], "unmapped")

        after = self.connection.execute(
            "SELECT id, row_data FROM client_data ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])

    def test_account_name_backfill_updates_only_blank_labels(self):
        backfill_sqlite(
            self.connection,
            client_id=self.client_id,
            configuration_version=1,
        )
        self.connection.execute(
            "UPDATE cash_bank_transaction_classifications SET account_name = NULL WHERE client_data_id = 1"
        )
        self.connection.execute(
            "UPDATE cash_bank_transaction_classifications SET account_name = ? WHERE client_data_id = 2",
            ("Preserved bank label",),
        )
        self.connection.execute(
            """
            INSERT INTO cash_bank_summary_read_models
                (client_id, template_id, source_upload_ids_key, period_start, period_end,
                 group_by, receipt_presentation, summary_payload)
            VALUES (?, 1, '1', '2024-01-01', '2024-01-31', 'day', 'net', '{}')
            """,
            (self.client_id,),
        )
        self.connection.commit()
        before = self.connection.execute(
            "SELECT id, row_data FROM client_data ORDER BY id"
        ).fetchall()

        dry_run = backfill_account_names_sqlite(
            self.connection,
            client_id=self.client_id,
            batch_size=2,
            dry_run=True,
        )
        self.assertEqual(dry_run.candidate_rows, 2)
        self.assertEqual(dry_run.would_update_rows, 1)
        self.assertEqual(dry_run.updated_rows, 0)
        self.assertEqual(dry_run.missing_source_names, 1)
        self.assertIsNone(
            self.connection.execute(
                "SELECT account_name FROM cash_bank_transaction_classifications WHERE client_data_id = 1"
            ).fetchone()[0]
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM cash_bank_summary_read_models WHERE client_id = ?",
                (self.client_id,),
            ).fetchone()[0],
            1,
        )

        updated = backfill_account_names_sqlite(
            self.connection,
            client_id=self.client_id,
            batch_size=2,
        )
        self.assertEqual(updated.updated_rows, 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT account_name FROM cash_bank_transaction_classifications WHERE client_data_id = 1"
            ).fetchone()[0],
            "Cash account",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT account_name FROM cash_bank_transaction_classifications WHERE client_data_id = 2"
            ).fetchone()[0],
            "Preserved bank label",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM cash_bank_summary_read_models WHERE client_id = ?",
                (self.client_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT revision FROM cashflow_report_revisions WHERE client_id = ?",
                (self.client_id,),
            ).fetchone()[0],
            1,
        )
        self.connection.execute(
            "UPDATE cash_bank_transaction_classifications SET account_name = NULL WHERE client_data_id = 2"
        )
        repeated = backfill_account_names_sqlite(
            self.connection,
            client_id=self.client_id,
            batch_size=2,
        )
        self.assertEqual(repeated.updated_rows, 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT revision FROM cashflow_report_revisions WHERE client_id = ?",
                (self.client_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT account_name FROM cash_bank_transaction_classifications WHERE client_data_id = 2"
            ).fetchone()[0],
            "Bank account",
        )
        after = self.connection.execute(
            "SELECT id, row_data FROM client_data ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])

    def test_period_keys_use_inclusive_fortnight_boundaries(self):
        expected = [
            ("2024-01-14", "2024-01", "2024-01-F1"),
            ("2024-01-15", "2024-01", "2024-01-F2"),
            ("2024-02-29", "2024-02", "2024-02-F2"),
            ("2024-03-01", "2024-03", "2024-03-F1"),
        ]
        for offset, (date_value, month_key, fortnight_key) in enumerate(expected, start=1):
            derived = build_classification_row(
                {
                    "id": 100 + offset,
                    "client_id": self.client_id,
                    "template_id": 1,
                    "upload_id": 1,
                    "row_data": json.dumps({
                        "Account Detail": "1000",
                        "Date": date_value,
                    }),
                    "extract_date": date_value,
                },
                {"1000": {"cash"}},
                configuration_version=1,
            )
            self.assertEqual(derived["transaction_month"], month_key)
            self.assertEqual(derived["transaction_fortnight"], fortnight_key)
    def test_backfill_is_idempotent_and_dry_run_does_not_write(self):
        first = backfill_sqlite(
            self.connection,
            client_id=self.client_id,
            configuration_version=1,
        )
        self.assertEqual(first.inserted_rows, 4)

        second = backfill_sqlite(
            self.connection,
            client_id=self.client_id,
            configuration_version=1,
        )
        self.assertEqual(second.inserted_rows, 0)
        self.assertEqual(second.skipped_existing_rows, 4)

        dry_run = backfill_sqlite(
            self.connection,
            client_id=self.client_id,
            configuration_version=2,
            dry_run=True,
        )
        self.assertEqual(dry_run.source_rows, 4)
        self.assertEqual(dry_run.inserted_rows, 0)
        self.assertEqual(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM cash_bank_transaction_classifications
                WHERE configuration_version = 2
                """
            ).fetchone()[0],
            0,
        )

    def test_new_configuration_updates_current_rows_in_place(self):
        first = backfill_sqlite(
            self.connection,
            client_id=self.client_id,
            configuration_version=1,
        )
        self.assertEqual(first.inserted_rows, 4)

        second = backfill_sqlite(
            self.connection,
            client_id=self.client_id,
            configuration_version=2,
        )
        self.assertEqual(second.inserted_rows, 0)
        self.assertEqual(second.skipped_existing_rows, 4)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM cash_bank_transaction_classifications"
            ).fetchone()[0],
            4,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(DISTINCT configuration_version) FROM cash_bank_transaction_classifications WHERE is_current = 1"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT MIN(configuration_version) FROM cash_bank_transaction_classifications WHERE is_current = 1"
            ).fetchone()[0],
            2,
        )
    def test_postgres_account_name_backfill_dry_run_uses_source_identity(self):
        source_rows = [
            {
                "id": index,
                "row_data": {
                    "Account Name": "Account " + str(index),
                },
            }
            for index in (1, 2, 3)
        ]
        source_rows.append({"id": 4, "row_data": {}})
        connection = _PostgresBackfillConnection(source_rows)

        summary = backfill_account_names_postgres(
            connection,
            client_id=7,
            batch_size=2,
            dry_run=True,
        )

        self.assertEqual(summary.candidate_rows, 4)
        self.assertEqual(summary.would_update_rows, 3)
        self.assertEqual(summary.updated_rows, 0)
        self.assertEqual(summary.missing_source_names, 1)
        self.assertEqual(connection.commit_count, 0)
        self.assertEqual(connection.rollback_count, 0)

    def test_postgres_backfill_keeps_source_cursor_across_batches(self):
        source_rows = [
            {
                "id": index,
                "client_id": 7,
                "template_id": 9,
                "upload_id": 11,
                "row_data": {
                    "Voucher ID": f"V-{index}",
                    "Account Detail": "1000",
                    "Date": f"2026-0{index}-01",
                    "Debit Amount": "10",
                    "Credit Amount": "0",
                },
                "extract_date": f"2026-0{index}-01",
            }
            for index in (1, 2, 3, 4)
        ]
        connection = _PostgresBackfillConnection(source_rows)

        summary = backfill_postgres(
            connection,
            client_id=7,
            configuration_version=1,
            batch_size=2,
            dry_run=True,
        )

        self.assertEqual(summary.source_rows, 4)
        self.assertEqual(summary.skipped_existing_rows, 0)
        self.assertEqual(summary.cash_rows, 4)
        self.assertEqual(connection.commit_count, 0)
        self.assertEqual(connection.rollback_count, 0)
if __name__ == "__main__":
    unittest.main()
