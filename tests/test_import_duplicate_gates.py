import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers.import_export import (
    _assert_active_identity_collisions,
    _canonical_source_identity,
    _canonical_source_identities,
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

    def test_exact_duplicate_rows_are_retained_and_counted(self):
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

        self.assertEqual(len(result["rows"]), 3)
        self.assertEqual(result["row_numbers"], [2, 3, 4])
        self.assertEqual(result["duplicate_group_count"], 1)
        self.assertEqual(result["duplicate_row_count"], 2)
        self.assertEqual(result["duplicate_skipped_count"], 0)
        self.assertEqual(result["duplicate_row_numbers"], [2, 3])
        self.assertEqual(result["duplicate_skipped_row_numbers"], [])

    def test_repeated_no_line_key_rows_get_distinct_stable_identities(self):
        template_cols = [
            {"id": 1, "column_name": "Voucher ID", "data_type": "Text"},
            {"id": 2, "column_name": "Account", "data_type": "Text"},
            {"id": 3, "column_name": "Amount", "data_type": "Numeric"},
        ]
        rows = [
            {"Voucher ID": "V-1", "Account": "Cash", "Amount": 100},
            {"Voucher ID": "V-1", "Account": "Cash", "Amount": 100},
        ]

        identities = _canonical_source_identities(template_cols, rows, 1)

        self.assertEqual(len(set(identities)), 2)
        self.assertEqual(
            identities[0],
            _canonical_source_identities(template_cols, rows, 1)[0],
        )


def test_different_explicit_line_id_is_added_without_inheriting_history():
    from routers.import_export import compare_transactions
    columns = [{"id": i, "column_name": name, "data_type": "Numeric" if name == "Amount" else "Text"}
               for i, name in enumerate(["Voucher", "Line ID", "Amount"], 1)]
    old = {"id": 42, "row_data": {"Voucher": "V1", "Line ID": "A", "Amount": 100},
           "source_identity": "identity-A", "version": 1, "verification_detail_id": 7}
    with patch("routers.import_export.execute_query_pg", return_value=[old]):
        result = compare_transactions(5, 1, columns, [{"Voucher": "V1", "Line ID": "B", "Amount": 100}],
                                      {c["column_name"]: c["column_name"] for c in columns}, {})
    assert result["changed_count"] == 0
    assert result["new_count"] == 1
    assert result["missing_count"] == 0
    assert result["new_source_identities"] != ["identity-A"]


def test_ambiguous_repeated_account_corrections_require_line_ids():
    import pytest
    from routers.import_export import compare_transactions
    columns = [{"id": i, "column_name": name, "data_type": "Text"}
               for i, name in enumerate(["Voucher", "Account Code", "Debit", "Credit"], 1)]
    existing = [{"id": i, "row_data": {"Voucher": "V1", "Account Code": "A", "Debit": amount, "Credit": 0}}
                for i, amount in [(1, 10), (2, 20)]]
    incoming = [{"Voucher": "V1", "Account Code": "A", "Debit": amount, "Credit": 0} for amount in [11, 21]]
    with patch("routers.import_export.execute_query_pg", return_value=existing), pytest.raises(HTTPException) as error:
        compare_transactions(5, 1, columns, incoming, {c["column_name"]: c["column_name"] for c in columns}, {})
    assert error.value.status_code == 409


def test_transaction_template_account_detail_pairs_unique_corrections_in_place():
    from routers.import_export import compare_transactions
    columns = [{"id": i, "column_name": name, "data_type": "Text"}
               for i, name in enumerate(["Voucher ID", "Account Detail", "Debit Amount", "Credit Amount"], 1)]
    existing = [
        {"id": 10, "row_data": {"Voucher ID": "V1", "Account Detail": "A", "Debit Amount": 100, "Credit Amount": 0}, "source_identity": "A"},
        {"id": 11, "row_data": {"Voucher ID": "V1", "Account Detail": "B", "Debit Amount": 0, "Credit Amount": 100}, "source_identity": "B"},
    ]
    incoming = [{**item["row_data"], "Debit Amount": item["row_data"]["Debit Amount"] * 2,
                 "Credit Amount": item["row_data"]["Credit Amount"] * 2} for item in existing]
    with patch("routers.import_export.execute_query_pg", return_value=existing):
        result = compare_transactions(5, 1, columns, incoming,
                                      {c["column_name"]: c["column_name"] for c in columns}, {})
    assert result["changed_count"] == 2
    assert result["new_count"] == result["missing_count"] == 0
    assert {item["client_data_id"] for item in result["changed"]} == {10, 11}
    assert {item["source_identity"] for item in result["changed"]} == {"A", "B"}


def test_repeat_comparison_requests_a_bounded_snapshot_and_preserves_occurrences():
    from routers.import_export import compare_transactions
    columns = [{"id": i, "column_name": name, "data_type": "Text"}
               for i, name in enumerate(["Voucher", "Account", "Amount"], 1)]
    incoming = [{"Voucher": "V1", "Account": "Cash", "Amount": 100}] * 2
    existing = [{"id": n, "row_data": row, "source_identity": f"line-{n}",
                 "version": 4, "validation_status": "Verified", "verification_detail_id": n + 100}
                for n, row in enumerate(incoming, 10)]
    with patch("routers.import_export.execute_query_pg", return_value=existing) as query:
        result = compare_transactions(5, 1, columns, incoming,
                                      {c["column_name"]: c["column_name"] for c in columns}, {})
    assert query.call_args.kwargs["page_size"] == 2000
    assert "ORDER BY cd.id" in query.call_args.args[0]
    assert result["unchanged"] == [10, 11]
    assert result["unchanged_count"] == 2
    assert result["new"] == result["changed"] == result["missing"] == []


if __name__ == "__main__":
    unittest.main()
