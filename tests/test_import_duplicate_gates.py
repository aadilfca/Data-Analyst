import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers.import_export import (
    _assert_active_identity_collisions,
    _canonical_source_identity,
    _deduplicate_exact_rows,
    _is_duplicate_upload_noop,
    _strict_duplicate_row_errors,
)


class ImportDuplicateGateTests(unittest.TestCase):
    def setUp(self):
        self.template_cols = [
            {"id": 1, "column_name": "Voucher ID", "data_type": "Text"},
            {"id": 2, "column_name": "Line No", "data_type": "Text"},
            {"id": 3, "column_name": "Amount", "data_type": "Numeric"},
        ]
        self.mapping = {
            "Voucher ID": "Voucher",
            "Line No": "Line",
            "Amount": "Amount",
        }

    def test_line_key_duplicate_is_blocked_even_when_content_differs(self):
        rows = [
            {"Voucher": "V-1", "Line": "1", "Amount": 100},
            {"Voucher": "V-1", "Line": "1", "Amount": 125},
        ]

        errors = _strict_duplicate_row_errors(
            rows, self.mapping, self.template_cols, 1, None, 0
        )

        self.assertEqual(len(errors), 2)
        self.assertTrue(all(error["errors"][0]["error_type"] == "duplicate_row" for error in errors))

    def test_source_identity_changes_only_when_no_line_key_exists(self):
        original = {"Voucher ID": "V-1", "Line No": "1", "Amount": 100}
        corrected = {"Voucher ID": "V-1", "Line No": "1", "Amount": 125}

        self.assertEqual(
            _canonical_source_identity(self.template_cols, original, 1),
            _canonical_source_identity(self.template_cols, corrected, 1),
        )

    def test_existing_identity_collision_is_rejected(self):
        with patch(
            "routers.import_export.execute_query_pg",
            return_value=[{"id": 55, "source_identity": "identity-1"}],
        ):
            with self.assertRaises(HTTPException) as raised:
                _assert_active_identity_collisions(5, 1, ["identity-1"], [])

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("canonical transaction identity", str(raised.exception.detail))

    def test_existing_identity_is_allowed_when_it_is_the_row_being_updated(self):
        with patch(
            "routers.import_export.execute_query_pg",
            return_value=[],
        ) as query:
            _assert_active_identity_collisions(5, 1, ["identity-1"], [55])

        query.assert_called_once()

    def test_same_imported_file_is_noop_before_duplicate_line_gate(self):
        self.assertTrue(
            _is_duplicate_upload_noop(
                {"id": 397},
                {"new_count": 0, "changed_count": 0, "missing_count": 0},
            )
        )
        self.assertFalse(
            _is_duplicate_upload_noop(
                {"id": 397},
                {"new_count": 1, "changed_count": 0, "missing_count": 0},
            )
        )
        self.assertFalse(
            _is_duplicate_upload_noop(
                None,
                {"new_count": 0, "changed_count": 0, "missing_count": 0},
            )
        )

    def test_exact_duplicate_rows_are_collapsed_and_counted(self):
        rows = [
            {"Voucher": "V-1", "Line": "1", "Amount": 100},
            {"Voucher": "V-1", "Line": "1", "Amount": 100},
            {"Voucher": "V-1", "Line": "2", "Amount": 125},
        ]

        result = _deduplicate_exact_rows(
            rows,
            self.mapping,
            self.template_cols,
            1,
            None,
            0,
            row_numbers=[2, 3, 4],
        )

        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["duplicate_group_count"], 1)
        self.assertEqual(result["duplicate_row_count"], 2)
        self.assertEqual(result["duplicate_skipped_count"], 1)
        self.assertEqual(result["duplicate_row_numbers"], [2, 3])
        self.assertEqual(result["duplicate_skipped_row_numbers"], [3])


if __name__ == "__main__":
    unittest.main()
