import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers.import_export import (
    _assert_active_identity_collisions,
    _canonical_source_identity,
    _canonical_source_identities,
    _deduplicate_exact_rows,
    _is_duplicate_upload_noop,
    _assert_upload_outcome_matches_plan,
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

    def test_success_counts_must_match_the_validated_actions(self):
        _assert_upload_outcome_matches_plan(99, 2, 1, 99, 2, 1)
        with self.assertRaises(HTTPException) as raised:
            _assert_upload_outcome_matches_plan(99, 0, 100, 0, 0, 100)
        self.assertEqual(raised.exception.status_code, 500)
        self.assertIn("No changes were committed", raised.exception.detail)

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


def test_unchecked_ledger_balance_stays_unchanged_and_creates_separate_refresh_action():
    from routers.import_export import compare_transactions, _build_validation_plan_payload, _comparison_column_names, ColumnMapping

    columns = [
        {"id": 1, "column_name": "Voucher ID", "data_type": "Text"},
        {"id": 2, "column_name": "Transaction Date", "data_type": "Date"},
        {"id": 3, "column_name": "Description", "data_type": "Text"},
        {"id": 4, "column_name": "Debit Amount", "data_type": "Numeric"},
        {"id": 5, "column_name": "Credit Amount", "data_type": "Numeric"},
        {"id": 6, "column_name": "Balance", "data_type": "Text"},
    ]
    old = {"Voucher ID": "BPV-1", "Transaction Date": "01/10/2026",
           "Description": "Rent", "Debit Amount": 0, "Credit Amount": 20, "Balance": "100 DR"}
    incoming = [{**old, "Balance": "80 DR"}]
    mappings = [
        ColumnMapping(
            template_column_id=column['id'], template_column_name=column['column_name'],
            excel_column_name=column['column_name'], compare_for_changes=column['column_name'] != 'Balance'
        ) for column in columns
    ]
    with patch("routers.import_export.execute_query_pg", return_value=[{
        "id": 77, "row_data": old, "version": 3, "extract_date": "2026-10-01",
        "source_identity": "old-identity", "validation_status": "Verified",
    }]):
        changes = compare_transactions(
            5, 1, columns, incoming, {name: name for name in old}, {},
            included_fields={name for name in old if name != "Balance"},
        )

    assert changes["unchanged_count"] == 1
    assert changes["changed_count"] == changes["new_count"] == changes["missing_count"] == 0
    assert changes["informational_update_count"] == 1
    assert changes["informational_updates"][0]["field_updates"] == {"Balance": "80 DR"}
    plan = _build_validation_plan_payload(changes, columns, 1, 1, [], 0, None, "/", "DMY")
    assert plan["informational_update_count"] == 1
    assert plan["informational_updates"][0]["client_data_id"] == 77
    assert "Balance" not in plan["comparison_fields"]

    legacy_mappings = [ColumnMapping(template_column_id=c['id'], template_column_name=c['column_name'])
                       for c in columns]
    assert _comparison_column_names(columns, legacy_mappings, {
        'data_purpose': 'account_ledger', 'ledger_columns': {'balance': 6}
    }) == {column['column_name'] for column in columns if column['column_name'] != 'Balance'}


def test_ledger_account_context_cannot_leak_into_ordinary_document_identity():
    from routers.import_export import _ledger_context, get_document_group_id
    columns = [{"id": 1, "column_name": "Voucher", "data_type": "Text"}]
    token = _ledger_context.set({"account_code": "ACCOUNT-A"})
    try:
        assert get_document_group_id(columns, {"Voucher": "V1"}, 7) == "7-ledger:ACCOUNT-A:V1"
    finally:
        _ledger_context.reset(token)
    assert get_document_group_id(columns, {"Voucher": "V1"}, 7) == "7-V1"


def _formatting_columns():
    return [{"id": i, "column_name": name, "data_type": kind} for i, (name, kind) in enumerate([
        ("Voucher ID", "Text"), ("Transaction Date", "Date"), ("Account Detail", "Text"),
        ("Voucher Detail", "Text"), ("Debit Amount", "Numeric"), ("Credit Amount", "Numeric"),
    ], 1)]


def _formatting_row(**changes):
    return {"Voucher ID": "V-APR", "Transaction Date": "01-04-2026", "Account Detail": "ACCOUNT-A",
            "Voucher Detail": "Office  supplies", "Debit Amount": 100.0, "Credit Amount": 0.0, **changes}


def _compare_formatting(existing, incoming, **options):
    from routers.import_export import compare_transactions
    columns = _formatting_columns()
    with patch("routers.import_export.execute_query_pg", return_value=existing) as read:
        result = compare_transactions(5, 1, columns, incoming,
                                      {c['column_name']: c['column_name'] for c in columns}, {}, **options)
    read.assert_called_once()
    return result


def test_native_text_and_serial_dates_match_without_rewriting_repeated_source_lines():
    import copy
    from datetime import datetime
    from routers.import_export import _row_fingerprint_token
    for new_date, delimiter, order in [(datetime(2026, 4, 1), '/', 'MDY'),
                                       (46113, '/', 'DMY'), ('04/01/2026', '/', 'MDY'),
                                       ('01-04-2026', '-', 'DMY')]:
        old_row = _formatting_row()
        existing = [{"id": i, "row_data": copy.deepcopy(old_row), "extract_date": "2026-04-01",
                     "version": 7, "source_identity": f"original-{i}",
                     "validation_status": "Verified", "verification_detail_id": 100 + i} for i in [1, 2]]
        before = copy.deepcopy(existing)
        fingerprint = _row_fingerprint_token(old_row)
        incoming = [_formatting_row(**{"Transaction Date": new_date, "Voucher Detail": "Office supplies"})] * 2
        result = _compare_formatting(existing, incoming, date_delimiter=delimiter, date_order=order)
        assert result['unchanged_count'] == 2
        assert result['changed_count'] == result['new_count'] == result['missing_count'] == 0
        assert result['unchanged'] == [1, 2]
        assert existing == before
        assert _row_fingerprint_token(old_row) == fingerprint


def test_exact_description_match_keeps_priority_over_formatting_equivalent_line():
    existing = [{"id": i, "row_data": _formatting_row(**{"Voucher Detail": text}),
                 "extract_date": "2026-04-01"} for i, text in [(1, "Office  supplies"), (2, "Office supplies")]]
    incoming = [_formatting_row(**{"Voucher Detail": text}) for text in ["Office   supplies", "Office supplies"]]
    result = _compare_formatting(existing, incoming, date_delimiter='-')
    assert result['unchanged'] == [2, 1]
    assert result['new_count'] == result['changed_count'] == 0


def test_real_description_change_reports_only_meaningful_fields_and_preserves_identity():
    from datetime import datetime
    from routers.import_export import build_change_report
    existing = [{"id": 1, "row_data": _formatting_row(**{"Voucher Detail": "Office supplies\nTag: A"}),
                 "extract_date": "2026-04-01", "source_identity": "original-1"}]
    incoming = [_formatting_row(**{"Transaction Date": datetime(2026, 4, 1), "Voucher Detail": "Office supplies"})]
    result = _compare_formatting(existing, incoming)
    assert result['changed_count'] == 1
    assert result['changed'][0]['source_identity'] == 'original-1'
    assert build_change_report(result, _formatting_columns(), 1)[0]['changed_fields'] == ['Voucher Detail']


def test_complete_report_classifies_a_single_corrected_line_as_changed():
    columns = _formatting_columns()
    old = {"id": 1, "row_data": _formatting_row(**{"Voucher Detail": "Old detail"}),
           "extract_date": "2026-04-01", "source_identity": "original-1"}
    incoming = [_formatting_row(**{"Voucher Detail": "Corrected detail"})]
    with patch("routers.import_export.execute_query_pg", side_effect=[[old], [{"id": 1}] ]):
        from routers.import_export import compare_transactions
        result = compare_transactions(
            5, 1, columns, incoming,
            {column["column_name"]: column["column_name"] for column in columns}, {},
            comparison_mode="replace_range", coverage_start="2026-04-01", coverage_end="2026-04-30",
            date_delimiter="-",
        )
    assert result["unchanged_count"] == 0
    assert result["changed_count"] == 1
    assert result["new_count"] == result["missing_count"] == 0
    assert result["changed"][0]["client_data_id"] == 1


def test_complete_report_keeps_99_matching_rows_unchanged_and_only_deletes_the_absent_row():
    columns = _formatting_columns()
    incoming = []
    existing = []
    for number in range(1, 100):
        row = _formatting_row(**{"Voucher ID": f"V-{number:03}", "Voucher Detail": "Same"})
        incoming.append(row)
        existing.append({"id": number, "row_data": dict(row), "extract_date": "2026-04-01"})
    absent = _formatting_row(**{"Voucher ID": "V-100", "Voucher Detail": "Was here"})
    existing.append({"id": 100, "row_data": absent, "extract_date": "2026-04-01"})
    with patch("routers.import_export.execute_query_pg", side_effect=[existing, [{"id": 100}]]):
        from routers.import_export import compare_transactions
        result = compare_transactions(
            5, 1, columns, incoming,
            {column["column_name"]: column["column_name"] for column in columns}, {},
            comparison_mode="replace_range", coverage_start="2026-04-01", coverage_end="2026-04-30",
            date_delimiter="-",
        )
    assert result["total_existing_before_import"] == 100
    assert result["unchanged_count"] == 99
    assert result["new_count"] == result["changed_count"] == 0
    assert result["missing_count"] == 1
    assert result["missing"][0]["client_data_id"] == 100


def test_genuine_repeated_tag_changes_still_block_with_voucher_account_and_fields():
    import pytest
    from datetime import datetime
    existing = [{"id": i, "row_data": _formatting_row(**{"Voucher Detail": f"Office supplies\nTag: {i}"}),
                 "extract_date": "2026-04-01"} for i in [1, 2]]
    incoming = [_formatting_row(**{"Transaction Date": datetime(2026, 4, 1), "Voucher Detail": "Office supplies"})] * 2
    with pytest.raises(HTTPException) as error:
        _compare_formatting(existing, incoming)
    assert error.value.status_code == 409
    detail = error.value.detail
    assert all(text in detail for text in ['V-APR', 'ACCOUNT-A', '2 uploaded and 2 existing', 'Voucher Detail', 'Line IDs'])
    assert 'Transaction Date' not in detail


def test_identical_date_text_with_different_calendar_meaning_is_a_real_change():
    old = _formatting_row(**{"Transaction Date": '01/04/2026'})
    result = _compare_formatting([{"id": 1, "row_data": old, "extract_date": '2026-01-04'}], [dict(old)])
    assert result['unchanged_count'] == 0
    assert result['changed_count'] == 1
    assert result['changed'][0]['changed_fields'] == ['Transaction Date']


def test_unknown_ambiguous_stored_date_order_is_not_guessed():
    from datetime import datetime
    from routers.import_export import _comparison_calendar_date
    assert _comparison_calendar_date('04/05/2026', '/', 'MDY', stored=True) is None
    assert _comparison_calendar_date('13-04-2026', '/', 'MDY', stored=True) == '2026-04-13'
    assert _comparison_calendar_date('2026-04-01', '/', 'DMY', stored=True) == '2026-04-01'
    assert _comparison_calendar_date(datetime(2026, 4, 1), '-', 'MDY') == '2026-04-01'


def test_spacing_normalization_is_limited_to_descriptions_and_retains_line_breaks():
    from routers.import_export import _formatting_comparison_values
    old = _formatting_row(**{"Voucher Detail": "Office\t supplies\r\nTag: A", "Account Detail": "A  B"})
    values = _formatting_comparison_values(old, _formatting_columns(), '-', 'DMY')
    assert values['Voucher Detail'] == 'Office supplies\nTag: A'
    assert values['Account Detail'] == 'A  B'
    assert values['Voucher Detail'] != 'Office supplies Tag: A'


if __name__ == "__main__":
    unittest.main()
