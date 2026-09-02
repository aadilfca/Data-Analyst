import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers.verification import get_voucher_support_data


class VoucherSupportBatchingTests(unittest.TestCase):
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
