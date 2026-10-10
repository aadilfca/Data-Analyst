from contextlib import contextmanager
import json
import unittest
from unittest.mock import patch

import pandas as pd

import database
from routers import import_export
from services.cash_bank_classification import upsert_classification_rows_postgres


class _Cursor:
    def __init__(self):
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, params=None):
        self.rows = []

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self):
        self.commit_count = 0
        self.rollback_count = 0

    def cursor(self, *args, **kwargs):
        return _Cursor()

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1


class UploadClassificationTests(unittest.TestCase):
    def test_compare_treats_reordered_rows_within_document_as_unchanged(self):
        template_cols = [
            {"id": 15, "column_name": "Voucher ID", "data_type": "Text"},
            {"id": 12, "column_name": "Transaction Date", "data_type": "Date"},
            {"id": 16, "column_name": "Account Detail", "data_type": "Text"},
            {"id": 19, "column_name": "Debit Amount", "data_type": "Numeric"},
        ]
        existing_rows = [
            {
                "id": 101,
                "row_data": {
                    "Voucher ID": "V-1",
                    "Transaction Date": "01/01/2026",
                    "Account Detail": "1000",
                    "Debit Amount": 25,
                },
                "version": 1,
                "document_group_id": "1-V-1",
                "validation_status": "Pending",
                "verification_detail_id": None,
            },
            {
                "id": 102,
                "row_data": {
                    "Voucher ID": "V-1",
                    "Transaction Date": "01/01/2026",
                    "Account Detail": "2000",
                    "Debit Amount": 10,
                },
                "version": 1,
                "document_group_id": "1-V-1",
                "validation_status": "Pending",
                "verification_detail_id": None,
            },
        ]
        incoming_rows = [
            {
                "Voucher ID": "V-1",
                "Date": "01/01/2026",
                "Account Code": "2000",
                "Debit": 10,
            },
            {
                "Voucher ID": "V-1",
                "Date": "01/01/2026",
                "Account Code": "1000",
                "Debit": 25,
            },
        ]

        with patch.object(import_export, "execute_query_pg", return_value=existing_rows):
            result = import_export.compare_transactions(
                client_id=3,
                template_id=1,
                template_cols=template_cols,
                incoming_rows=incoming_rows,
                col_mapping={
                    "Voucher ID": "Voucher ID",
                    "Transaction Date": "Date",
                    "Account Detail": "Account Code",
                    "Debit Amount": "Debit",
                },
                mapping_dict={},
            )

        self.assertEqual(result["unchanged_count"], 2)
        self.assertEqual(result["changed_count"], 0)
        self.assertEqual(result["new_count"], 0)
        self.assertEqual(result["missing_count"], 0)

    def test_derived_upsert_refreshes_same_source_line_without_mutating_payload(self):
        connection = _Connection()
        source_row = {
            "id": 7,
            "client_id": 3,
            "template_id": 5,
            "upload_id": 9,
            "row_data": {
                "Voucher ID": "V-7",
                "Account Detail": "1000",
                "Account Name": "Cash in hand",
                "Date": "2024-01-02",
                "Debit Amount": "25.50",
                "Credit Amount": "0",
            },
            "extract_date": "2024-01-02",
        }
        original_payload = json.loads(json.dumps(source_row["row_data"]))

        with patch(
            "services.cash_bank_classification.execute_values"
        ) as execute_values:
            derived = upsert_classification_rows_postgres(
                connection,
                [source_row],
                {"1000": {"cash"}},
                configuration_version=1,
            )

        sql_statements = [call.args[1] for call in execute_values.call_args_list]
        self.assertIn("UPDATE cash_bank_transaction_classifications", sql_statements[0])
        self.assertTrue(
            any("UPDATE client_data AS target" in statement for statement in sql_statements),
            "canonical client_data derived fields must be refreshed",
        )
        self.assertEqual(derived[0]["classification_status"], "cash")
        self.assertEqual(derived[0]["account_name"], "Cash in hand")
        self.assertEqual(source_row["row_data"], original_payload)
        self.assertEqual(connection.commit_count, 0)

    def test_batch_insert_defers_commit_inside_active_transaction(self):
        connection = _Connection()
        token = database._pg_transaction.set(object())
        try:
            with patch(
                "database.psycopg2.extras.execute_values",
                return_value=[(101,)],
            ):
                ids = database.execute_batch_insert_with_ids_pg(
                    connection,
                    "INSERT INTO client_data VALUES %s RETURNING id",
                    [(1,)],
                )
        finally:
            database._pg_transaction.reset(token)

        self.assertEqual(ids, [101])
        self.assertEqual(connection.commit_count, 0)
        self.assertEqual(connection.rollback_count, 0)

    def _request(self):
        columns = [
            (1, "Voucher ID", "Text", "Voucher ID"),
            (2, "Date", "Date", "Date"),
            (3, "Account Detail", "Text", "Account Detail"),
            (4, "Debit Amount", "Numeric", "Debit Amount"),
            (5, "Credit Amount", "Numeric", "Credit Amount"),
        ]
        mappings = [
            import_export.ColumnMapping(
                template_column_id=column_id,
                template_column_name=template_name,
                excel_column_name=excel_name,
                matched=True,
            )
            for column_id, template_name, _data_type, excel_name in columns
        ]
        return (
            import_export.ImportRequest(
                template_id=5,
                file_id=9,
                client_id=3,
                mappings=mappings,
            ),
            columns,
        )

    def _query_side_effect(self, sql, params=None):
        if "FROM templates" in sql:
            return [{"id": 5, "template_name": "Transaction Report", "rate_source": None}]
        if "SELECT file_path" in sql:
            return [{"file_path": "unused.xlsx"}]
        if "SELECT id FROM clients" in sql:
            return [{"id": 3}]
        if "FROM template_column_mappings" in sql:
            return [
                {"id": 1, "column_name": "Voucher ID", "data_type": "Text"},
                {"id": 2, "column_name": "Date", "data_type": "Date"},
                {"id": 3, "column_name": "Account Detail", "data_type": "Text"},
                {"id": 4, "column_name": "Debit Amount", "data_type": "Numeric"},
                {"id": 5, "column_name": "Credit Amount", "data_type": "Numeric"},
            ]
        return []

    def _run_import(self, upsert_side_effect=None):
        request, _columns = self._request()
        connection = _Connection()
        self.last_connection = connection
        captured_sources = []

        @contextmanager
        def transaction():
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

        def capture_upsert(
            conn,
            source_rows,
            treasury_mapping,
            configuration_version=1,
        ):
            captured_sources.extend(source_rows)
            if upsert_side_effect:
                raise upsert_side_effect
            return source_rows

        dataframe = pd.DataFrame(
            [
                {
                    "Voucher ID": "V-1",
                    "Date": "02/01/2024",
                    "Account Detail": "1000",
                    "Debit Amount": 25,
                    "Credit Amount": 0,
                },
                {
                    "Voucher ID": "V-2",
                    "Date": "03/01/2024",
                    "Account Detail": "2000",
                    "Debit Amount": 0,
                    "Credit Amount": 25,
                },
            ]
        )

        patches = [
            patch.object(import_export, "execute_query_pg", side_effect=self._query_side_effect),
            patch.object(import_export, "execute_write_pg"),
            patch.object(import_export, "execute_batch_pg"),
            patch.object(import_export, "execute_batch_insert_with_ids_pg", return_value=[101, 102]),
            patch.object(import_export, "compare_transactions", return_value=None),
            patch.object(import_export, "get_rate_precision", return_value=0),
            patch.object(import_export, "load_treasury_mapping_postgres", return_value={"1000": {"cash"}, "2000": {"bank"}}),
            patch.object(import_export, "upsert_classification_rows_postgres", side_effect=capture_upsert),
            patch.object(import_export, "prepare_cashflow_summary_snapshot", return_value={"status": "skipped"}),
            patch.object(import_export, "log_audit"),
            patch.object(import_export.pd, "read_excel", return_value=dataframe),
            patch.object(import_export, "pg_transaction", side_effect=lambda: transaction()),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
            result = import_export.validate_and_import(request)

        return result, connection, captured_sources

    def test_upload_classifies_new_rows_inside_transaction_and_preserves_payload(self):
        result, connection, sources = self._run_import()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["imported_count"], 2)
        self.assertEqual(connection.commit_count, 1)
        self.assertEqual(connection.rollback_count, 0)
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["row_data"]["Account Detail"], "1000")
        self.assertEqual(sources[1]["row_data"]["Account Detail"], "2000")

    def test_upload_rolls_back_when_classification_fails(self):
        with self.assertRaises(RuntimeError):
            self._run_import(RuntimeError("classification failed"))

        self.assertEqual(self.last_connection.commit_count, 0)
        self.assertEqual(self.last_connection.rollback_count, 1)


if __name__ == "__main__":
    unittest.main()
