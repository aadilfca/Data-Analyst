import os
import tempfile
import unittest
from unittest.mock import patch

import database
from database import execute_query, execute_write, init_db
from routers.import_export import (
    ColumnMapping,
    ImportRequest,
    _build_validation_plan_payload,
    _build_upload_storage_path,
    _canonical_source_identity,
    _is_duplicate_upload_noop,
    _strict_duplicate_row_errors,
    _validation_request_signature,
    build_change_report,
    compare_transactions,
    count_new_and_existing_rows,
)


class UploadValidationCountTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        database.DB_NAME = os.path.join(self.tmpdir.name, "test.db")
        init_db()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_same_name_uploads_use_distinct_storage_paths(self):
        first_path = _build_upload_storage_path('All voucher one fine.xlsx')
        second_path = _build_upload_storage_path('All voucher one fine.xlsx')

        self.assertNotEqual(first_path, second_path)
        self.assertEqual(os.path.basename(first_path).split('_', 1)[1], 'All voucher one fine.xlsx')
        self.assertEqual(os.path.basename(second_path).split('_', 1)[1], 'All voucher one fine.xlsx')

    def test_counts_existing_and_new_rows(self):
        execute_write(
            "INSERT INTO client_data (client_id, template_id, upload_id, row_data, created_by, version) VALUES (?, ?, ?, ?, ?, ?)",
            (1, 1, 1, '{"Voucher":"ABC","Amount":100}', "Analyst", 1),
        )

        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        mapping_dict = {
            1: MappingStub("Voucher"),
            2: MappingStub("Amount"),
        }

        rows = [
            {"Voucher": "ABC", "Amount": 100},
            {"Voucher": "XYZ", "Amount": 200},
        ]

        # The count helper reads Postgres in production, while the fixture row above
        # is intentionally created in isolated SQLite. Stub only that external read
        # so the comparison logic itself remains under test.
        with patch(
            "routers.import_export.execute_query_pg",
            return_value=[{"row_data": {"Voucher": "ABC", "Amount": 100}}],
        ):
            new_count, existing_count = count_new_and_existing_rows(
                1, 1, rows, template_cols, mapping_dict
            )

        self.assertEqual(new_count, 1)
        self.assertEqual(existing_count, 1)


    def test_uses_set_based_counts_when_postgres_returns_counts(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        rows = [{"Voucher": "ABC", "Amount": 100}, {"Voucher": "XYZ", "Amount": 200}]
        mapping_dict = {1: MappingStub("Voucher"), 2: MappingStub("Amount")}
        with patch(
            "routers.import_export.execute_query_pg",
            return_value=[{"new_count": 1, "existing_count": 1}],
        ) as query:
            counts = count_new_and_existing_rows(1, 1, rows, template_cols, mapping_dict)

        self.assertEqual(counts, (1, 1))
        query.assert_called_once()

    def test_current_row_comparison_classifies_unchanged_changed_and_added(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        mapping_dict = {1: MappingStub("Voucher"), 2: MappingStub("Amount")}
        existing = [
            {"id": 10, "row_data": {"Voucher": "UNCHANGED", "Amount": 100}, "version": 4,
             "extract_date": None, "validation_status": "Verified", "verification_detail_id": 1},
            {"id": 11, "row_data": {"Voucher": "CHANGED", "Amount": 100}, "version": 2,
             "extract_date": None, "validation_status": "Pending", "verification_detail_id": None},
        ]
        incoming = [
            {"Voucher": "UNCHANGED", "Amount": 100},
            {"Voucher": "CHANGED", "Amount": 125},
            {"Voucher": "ADDED", "Amount": 50},
        ]

        with patch("routers.import_export.execute_query_pg", return_value=existing):
            result = compare_transactions(
                1, 1, template_cols, incoming, {"Voucher": "Voucher", "Amount": "Amount"},
                mapping_dict,
            )

        self.assertEqual(result["unchanged_count"], 1)
        self.assertEqual(result["changed_count"], 1)
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["missing_count"], 0)
        self.assertEqual(result["unchanged"], [10])
        self.assertEqual(result["changed"][0]["client_data_id"], 11)

    def test_identical_reimport_is_idempotent_and_creates_no_actions(self):
        template_cols = [
            {'id': 1, 'column_name': 'Voucher', 'data_type': 'Text'},
            {'id': 2, 'column_name': 'Amount', 'data_type': 'Numeric'},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        mapping_dict = {
            1: MappingStub('Voucher'),
            2: MappingStub('Amount'),
        }
        existing = [{
            'id': 19,
            'row_data': {'Voucher': 'UNCHANGED', 'Amount': 100},
            'version': 3,
            'extract_date': None,
            'validation_status': 'Verified',
            'verification_detail_id': 4,
        }]

        with patch('routers.import_export.execute_query_pg', return_value=existing):
            result = compare_transactions(
                1,
                1,
                template_cols,
                [{'Voucher': 'UNCHANGED', 'Amount': 100}],
                {'Voucher': 'Voucher', 'Amount': 'Amount'},
                mapping_dict,
            )

        self.assertEqual(result['unchanged_count'], 1)
        self.assertEqual(result['changed_count'], 0)
        self.assertEqual(result['new_count'], 0)
        self.assertEqual(result['missing_count'], 0)
        self.assertEqual(result['unchanged'], [19])
        self.assertEqual(result['changed'], [])
        self.assertEqual(result['new'], [])
        self.assertEqual(result['missing'], [])

    def test_changed_row_keeps_existing_canonical_identity(self):
        template_cols = [
            {'id': 1, 'column_name': 'Voucher', 'data_type': 'Text'},
            {'id': 2, 'column_name': 'Amount', 'data_type': 'Numeric'},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        existing = [{
            'id': 27,
            'row_data': {'Voucher': 'V-1', 'Amount': 100},
            'source_identity': 'stable-identity',
            'version': 2,
            'extract_date': None,
            'validation_status': 'Pending',
            'verification_detail_id': None,
        }]
        with patch('routers.import_export.execute_query_pg', return_value=existing):
            result = compare_transactions(
                1,
                1,
                template_cols,
                [{'Voucher': 'V-1', 'Amount': 125}],
                {'Voucher': 'Voucher', 'Amount': 'Amount'},
                {1: MappingStub('Voucher'), 2: MappingStub('Amount')},
            )

        self.assertEqual(result['changed'][0]['source_identity'], 'stable-identity')

    def test_same_source_file_with_no_delta_is_not_importable(self):
        self.assertTrue(_is_duplicate_upload_noop(
            {"id": 397},
            {"new_count": 0, "changed_count": 0, "missing_count": 0},
        ))

    def test_canonical_source_identity_is_repeatable_and_line_stable(self):
        template_cols = [
            {'id': 1, 'column_name': 'Voucher ID', 'data_type': 'Text'},
            {'id': 2, 'column_name': 'Line No', 'data_type': 'Text'},
            {'id': 3, 'column_name': 'Amount', 'data_type': 'Numeric'},
        ]
        original = {'Voucher ID': 'V-1', 'Line No': '2', 'Amount': 100}
        corrected = {'Voucher ID': 'V-1', 'Line No': '2', 'Amount': 125}

        self.assertEqual(
            _canonical_source_identity(template_cols, original, 7),
            _canonical_source_identity(template_cols, original, 7),
        )
        self.assertEqual(
            _canonical_source_identity(template_cols, original, 7),
            _canonical_source_identity(template_cols, corrected, 7),
        )

    def test_multi_line_document_ids_are_compared_without_duplicate_rejection(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher ID", "data_type": "Text"},
            {"id": 2, "column_name": "Account Name", "data_type": "Text"},
            {"id": 3, "column_name": "Debit Amount", "data_type": "Numeric"},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        mapping_dict = {
            1: MappingStub("Voucher ID"),
            2: MappingStub("Account Name"),
            3: MappingStub("Debit Amount"),
        }
        existing = [
            {"id": 30, "row_data": {"Voucher ID": "CPV-0126-100", "Account Name": "Expense", "Debit Amount": 100},
             "version": 1, "extract_date": None, "validation_status": "Pending", "verification_detail_id": None},
            {"id": 31, "row_data": {"Voucher ID": "CPV-0126-100", "Account Name": "Cash", "Debit Amount": 0},
             "version": 1, "extract_date": None, "validation_status": "Pending", "verification_detail_id": None},
        ]
        incoming = [
            {"Voucher ID": "CPV-0126-100", "Account Name": "Expense", "Debit Amount": 125},
            {"Voucher ID": "CPV-0126-100", "Account Name": "Cash", "Debit Amount": 0},
        ]

        with patch("routers.import_export.execute_query_pg", return_value=existing):
            result = compare_transactions(
                1, 1, template_cols, incoming,
                {"Voucher ID": "Voucher ID", "Account Name": "Account Name", "Debit Amount": "Debit Amount"},
                mapping_dict,
            )

        self.assertEqual(result["unchanged_count"], 1)
        self.assertEqual(result["changed_count"], 1)
        self.assertEqual(result["new_count"], 0)
        self.assertEqual(result["missing_count"], 0)
        self.assertEqual(result["unchanged"], [31])
        self.assertEqual(result["changed"][0]["client_data_id"], 30)

    def test_complete_replacement_removes_only_missing_lines_inside_detected_date_range(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
            {"id": 3, "column_name": "Date", "data_type": "Date"},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        mapping_dict = {
            1: MappingStub("Voucher"),
            2: MappingStub("Amount"),
            3: MappingStub("Date"),
        }
        existing = [
            {"id": 20, "row_data": {"Voucher": "KEEP", "Amount": 100, "Date": "01/01/2026"},
             "version": 1, "extract_date": "2026-01-01", "validation_status": "Pending", "verification_detail_id": None},
            {"id": 21, "row_data": {"Voucher": "REMOVE", "Amount": 200, "Date": "02/01/2026"},
             "version": 1, "extract_date": "2026-01-02", "validation_status": "Pending", "verification_detail_id": None},
            {"id": 22, "row_data": {"Voucher": "OUTSIDE", "Amount": 300, "Date": "03/01/2026"},
             "version": 1, "extract_date": "2026-01-03", "validation_status": "Pending", "verification_detail_id": None},
        ]

        with patch("routers.import_export.execute_query_pg", return_value=existing):
            result = compare_transactions(
                1, 1, template_cols,
                [
                    {"Voucher": "KEEP", "Amount": 100, "Date": "01/01/2026"},
                    {"Voucher": "NEW", "Amount": 150, "Date": "02/01/2026"},
                ],
                {"Voucher": "Voucher", "Amount": "Amount", "Date": "Date"},
                mapping_dict,
                comparison_mode="replace_range",
                date_delimiter="/",
                date_order="DMY",
            )

        self.assertEqual(result["date_range"], {"start": "2026-01-01", "end": "2026-01-02"})
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["missing_count"], 1)
        self.assertEqual(result["missing"][0]["client_data_id"], 21)

    def test_validation_plan_keeps_only_actionable_rows_and_fingerprints(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
        ]
        change_detection = {
            "comparison_mode": "correction",
            "date_range": None,
            "unchanged_count": 7,
            "changed_count": 1,
            "new_count": 1,
            "missing_count": 1,
            "duplicate_group_count": 2,
            "duplicate_row_count": 4,
            "duplicate_skipped_count": 2,
            "duplicate_row_numbers": [8, 9, 12, 13],
            "duplicate_skipped_row_numbers": [9, 13],
            "changed": [{
                "client_data_id": 10,
                "old_row_data": {"Voucher": "CHANGED", "Amount": 100},
                "new_row_data": {"Voucher": "CHANGED", "Amount": 125},
                "old_version": 3,
                "verification_detail_id": 99,
            }],
            "new": [{"Voucher": "ADDED", "Amount": 50}],
            "missing": [{
                "client_data_id": 11,
                "old_row_data": {"Voucher": "REMOVED", "Amount": 75},
                "old_version": 2,
                "verification_detail_id": None,
            }],
        }

        payload = _build_validation_plan_payload(
            change_detection,
            template_cols,
            1,
            total_rows=10,
            rejected_rows=[],
            force_included_count=0,
            rate_source=None,
            date_delimiter="/",
            date_order="DMY",
        )

        self.assertEqual(payload["unchanged_count"], 7)
        self.assertEqual(len(payload["changed"]), 1)
        self.assertEqual(len(payload["new"]), 1)
        self.assertEqual(len(payload["missing"]), 1)
        self.assertNotIn("old_row_data", payload["changed"][0])
        self.assertNotIn("old_row_data", payload["missing"][0])
        self.assertTrue(payload["changed"][0]["expected_old_fingerprint"])
        self.assertEqual(payload["new"][0]["row_data"]["Voucher"], "ADDED")
        self.assertEqual(payload["duplicate_group_count"], 2)
        self.assertEqual(payload["duplicate_row_count"], 4)
        self.assertEqual(payload["duplicate_skipped_count"], 2)
        self.assertEqual(payload["duplicate_skipped_row_numbers"], [9, 13])

    def test_validation_signature_ignores_plan_id(self):
        base = {
            "template_id": 1,
            "file_id": 2,
            "client_id": 3,
            "mappings": [
                ColumnMapping(
                    template_column_id=1,
                    template_column_name="Voucher",
                    excel_column_name="Voucher",
                )
            ],
            "comparison_mode": "correction",
        }
        first = ImportRequest(**base, validation_plan_id="plan-one")
        second = ImportRequest(**base, validation_plan_id="plan-two")

        self.assertEqual(
            _validation_request_signature(first),
            _validation_request_signature(second),
        )

    def test_change_report_keeps_old_and_new_values_only_for_changed_fields(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
            {"id": 3, "column_name": "Narration", "data_type": "Text"},
        ]
        report = build_change_report(
            {
                "changed": [{
                    "client_data_id": 10,
                    "old_row_data": {"Voucher": "CPV-1", "Amount": 100, "Narration": "old"},
                    "new_row_data": {"Voucher": "CPV-1", "Amount": 125, "Narration": "new"},
                }],
                "new": [],
                "missing": [],
            },
            template_cols,
            1,
        )

        self.assertEqual(report[0]["changed_fields"], ["Amount", "Narration"])
        self.assertEqual(report[0]["old_values"], {"Amount": 100, "Narration": "old"})
        self.assertEqual(report[0]["new_values"], {"Amount": 125, "Narration": "new"})
        self.assertNotIn("old_row_data", report[0])
        self.assertNotIn("new_row_data", report[0])

    def test_uploaded_files_has_only_compact_upload_summary_columns(self):
        columns = {
            row["name"]
            for row in execute_query("PRAGMA table_info(uploaded_files)")
        }

        self.assertTrue({
            "template_id",
            "uploaded_by",
            "comparison_mode",
            "date_range_start",
            "date_range_end",
            "summary_total_count",
            "summary_added_count",
            "summary_changed_count",
            "summary_removed_count",
            "summary_unchanged_count",
            "summary_updated_at",
        }.issubset(columns))
        self.assertNotIn("change_report_json", columns)

    def test_client_data_has_canonical_identity_derived_columns_and_indexes(self):
        columns = {
            row["name"]
            for row in execute_query("PRAGMA table_info(client_data)")
        }
        self.assertTrue({
            "source_identity",
            "source_fingerprint",
            "derived_voucher_id",
            "derived_account_code",
            "derived_transaction_date",
            "derived_treasury_type",
            "derived_classification_status",
        }.issubset(columns))

        indexes = {
            row["name"]
            for row in execute_query("PRAGMA index_list(client_data)")
        }
        self.assertTrue({
            "idx_client_data_active_identity",
            "idx_client_data_active_identity_unique",
            "idx_client_data_active_derived_date",
            "idx_client_data_active_derived_classification",
        }.issubset(indexes))

    def test_uploaded_files_persists_compact_source_file_hash(self):
        columns = {
            row["name"]
            for row in execute_query("PRAGMA table_info(uploaded_files)")
        }

        self.assertIn("file_sha256", columns)
        indexes = {
            row["name"]
            for row in execute_query("PRAGMA index_list(uploaded_files)")
        }
        self.assertIn("idx_uploaded_files_source_hash_scope", indexes)

    def test_strict_gate_rejects_exact_duplicate_lines_inside_one_document(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher ID", "data_type": "Text"},
            {"id": 2, "column_name": "Account", "data_type": "Text"},
            {"id": 3, "column_name": "Amount", "data_type": "Numeric"},
        ]
        rows = [
            {"Voucher": "V-1", "Ledger": "Cash", "Amount": 100},
            {"Voucher": "V-1", "Ledger": "Cash", "Amount": 100},
        ]

        errors = _strict_duplicate_row_errors(
            rows,
            {"Voucher ID": "Voucher", "Account": "Ledger", "Amount": "Amount"},
            template_cols,
            1,
            None,
            0,
        )

        self.assertEqual([error["row_number"] for error in errors], [2, 3])
        self.assertEqual(errors[0]["errors"][0]["error_type"], "duplicate_row")
        self.assertEqual(errors[0]["errors"][0]["column"], "Row-level")

    def test_strict_gate_allows_same_line_content_in_different_documents(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher ID", "data_type": "Text"},
            {"id": 2, "column_name": "Account", "data_type": "Text"},
            {"id": 3, "column_name": "Amount", "data_type": "Numeric"},
        ]
        rows = [
            {"Voucher": "V-1", "Ledger": "Cash", "Amount": 100},
            {"Voucher": "V-2", "Ledger": "Cash", "Amount": 100},
        ]

        errors = _strict_duplicate_row_errors(
            rows,
            {"Voucher ID": "Voucher", "Account": "Ledger", "Amount": "Amount"},
            template_cols,
            1,
            None,
            0,
        )

        self.assertEqual(errors, [])

if __name__ == "__main__":
    unittest.main()
