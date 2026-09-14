import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers.verification import (
    _evaluate_form_bill_completion,
    _get_form_cross_check_values,
    _materialize_form_entry_values,
    _shape_form_entries,
    get_form_copy_sources,
    get_voucher_support_data,
)


class VoucherSupportBatchingTests(unittest.TestCase):
    def test_arithmetic_form_values_are_server_owned_and_numeric(self):
        fields = {
            1: {'id': 1, 'field_name': 'Persons', 'field_type': 'numeric', 'field_scope': 'item'},
            2: {'id': 2, 'field_name': 'Rate', 'field_type': 'numeric', 'field_scope': 'item'},
            3: {'id': 3, 'field_name': 'Total', 'field_type': 'arithmetic_multiply',
                'field_scope': 'item', 'arithmetic_operands': '["Persons", "Rate"]'},
        }

        values = _materialize_form_entry_values(
            fields, {1: '4', 2: '12.5', 3: '999999'}, 'item'
        )

        self.assertEqual(values[3], (None, None, 50.0))
        self.assertNotEqual(values[3][2], 999999)

    def test_arithmetic_form_value_stays_blank_until_both_operands_exist(self):
        fields = {
            1: {'id': 1, 'field_name': 'Persons', 'field_type': 'numeric', 'field_scope': 'item'},
            2: {'id': 2, 'field_name': 'Rate', 'field_type': 'numeric', 'field_scope': 'item'},
            3: {'id': 3, 'field_name': 'Total', 'field_type': 'arithmetic_multiply',
                'field_scope': 'item', 'arithmetic_operands': '["Persons", "Rate"]'},
        }

        values = _materialize_form_entry_values(fields, {1: '4'}, 'item')

        self.assertNotIn(3, values)

    def test_arithmetic_form_values_shape_as_numeric_for_completion_and_cross_checks(self):
        fields = [
            {'id': 1, 'field_name': 'Persons', 'field_type': 'numeric', 'field_scope': 'item'},
            {'id': 2, 'field_name': 'Rate', 'field_type': 'numeric', 'field_scope': 'item'},
            {'id': 3, 'field_name': 'Total', 'field_type': 'arithmetic_multiply', 'field_scope': 'item'},
        ]
        rows = [
            {'entry_id': 10, 'voucher_line_id': 7, 'document_id': 15, 'bill_group_index': 0,
             'row_scope': 'item', 'sort_order': 0, 'field_id': 3, 'field_name': 'Total',
             'field_type': 'arithmetic_multiply', 'value_text': None, 'value_numeric': 50},
        ]

        shaped = _shape_form_entries(rows)
        self.assertEqual(shaped[0]['values'][0]['value'], 50.0)
        completion_rows = [
            {**rows[0], 'field_id': 1, 'field_name': 'Persons', 'field_type': 'numeric', 'value_numeric': 4},
            {**rows[0], 'field_id': 2, 'field_name': 'Rate', 'field_type': 'numeric', 'value_numeric': 12.5},
            rows[0],
        ]
        self.assertTrue(_evaluate_form_bill_completion(fields, completion_rows, 50))

        with patch("routers.verification.execute_query_pg", return_value=[{
            'client_data_id': 100, 'document_id': 15, 'voucher_line_id': 7,
            'updated_at': '2026-09-14T08:00:00+00:00', 'field_name': 'Total',
            'field_type': 'arithmetic_multiply', 'value_text': None,
            'value_date': None, 'value_numeric': 50,
        }]):
            cross_check_values = _get_form_cross_check_values(7, [7], [15])

        self.assertEqual(cross_check_values[15]['Total'], 50.0)

    def test_form_copy_sources_resolve_active_linked_template_rows_for_same_line(self):
        responses = [
            [{"client_data_id": 9001}],
            [{"id": 15, "template_id": 5, "document_type": "form"}],
            [{"id": 101, "field_name": "Vendor Bill", "field_type": "text", "field_scope": "header", "linked_column": "Vendor"}],
            [{
                "source_client_data_id": 8001,
                "source_document_id": 20,
                "source_document_name": "Store receiving",
                "row_data": '{"Vendor": "Acme", "Quantity": 4}',
            }],
        ]

        def fake_query(*args, **kwargs):
            return responses.pop(0)

        with patch("routers.verification.execute_query_pg", side_effect=fake_query) as query:
            result = get_form_copy_sources(42, 15)

        self.assertEqual(result["template_id"], 5)
        self.assertEqual(result["sources"][0]["row_data"]["Vendor"], "Acme")
        self.assertEqual(query.call_count, 4)
        source_sql, source_params = query.call_args_list[-1].args[:2]
        self.assertIn("vd.client_data_id = %s", source_sql)
        self.assertIn("ard.document_type = 'linked_template'", source_sql)
        self.assertIn("ard.template_id = %s", source_sql)
        self.assertEqual(source_params, (9001, 5))

    def test_form_copy_sources_skip_source_query_without_linked_mapping(self):
        responses = [
            [{"client_data_id": 9001}],
            [{"id": 15, "template_id": 5, "document_type": "form"}],
            [{"id": 101, "field_name": "Manual note", "field_type": "text", "field_scope": "header", "linked_column": None}],
        ]

        def fake_query(*args, **kwargs):
            return responses.pop(0)

        with patch("routers.verification.execute_query_pg", side_effect=fake_query) as query:
            result = get_form_copy_sources(42, 15)

        self.assertEqual(result["sources"], [])
        self.assertEqual(query.call_count, 3)

    def test_unknown_line_returns_404_without_fanout(self):
        with patch("routers.verification.execute_query_pg", return_value=[]) as query:
            with self.assertRaises(HTTPException) as error:
                get_voucher_support_data(999999)
        self.assertEqual(error.exception.status_code, 404)
        query.assert_called_once()

    def test_empty_voucher_returns_empty_collections_with_set_reads(self):
        responses = [
            [{"session_id": 7, "transaction_ref": "CP-1"}],
            [],
        ]

        def fake_query(*args, **kwargs):
            return responses.pop(0)

        with patch("routers.verification.execute_query_pg", side_effect=fake_query) as query:
            result = get_voucher_support_data(42)

        self.assertEqual(result["voucher_line_id"], 42)
        self.assertEqual(result["transaction_ref"], "CP-1")
        self.assertEqual(result["lines"], {})
        self.assertEqual(query.call_count, 2)

    def test_links_are_grouped_by_voucher_line_identity(self):
        responses = [
            [{"session_id": 7, "transaction_ref": "CP-1", "client_id": 5}],
            [{"id": 42}],
            [{"id": 42, "client_data_id": 9001}],
            [{
                "id": 77,
                "voucher_line_id": 42,
                "client_data_id": 8001,
                "voucher_line_client_data_id": 9001,
                "document_id": 15,
                "document_name": "Purchase Demand",
                "document_is_active": True,
                "linked_by": "Analyst",
                "linked_at": None,
                "signature_exception": False,
                "row_data": {"PD no": "PD-1"},
                "extract_date": "2026-08-10",
                "extract_amount": 4,
                "rate_source": None,
            }],
            [],
            [],
            [],
            [],
        ]

        def fake_query(*args, **kwargs):
            return responses.pop(0)

        with patch("routers.verification.execute_query_pg", side_effect=fake_query):
            result = get_voucher_support_data(42)

        documents = result["lines"]["42"]["linked_documents"]["documents"]
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["client_data_id"], 8001)
        self.assertEqual(documents[0]["document_name"], "Purchase Demand")


if __name__ == "__main__":
    unittest.main()
