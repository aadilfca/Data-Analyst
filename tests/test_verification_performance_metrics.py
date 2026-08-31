import unittest

from routers import verification
from database import (
    _instrument_db_call,
    get_db_metrics,
    start_db_metrics,
    stop_db_metrics,
)


class PerformanceMetricsTests(unittest.TestCase):
    """The opt-in counter must observe calls without changing their results or errors."""

    def test_disabled_metrics_leave_wrapped_calls_unchanged(self):
        wrapped = _instrument_db_call("reads")(lambda value: {"value": value})

        self.assertEqual(wrapped(7), {"value": 7})
        self.assertEqual(
            get_db_metrics(),
            {"calls": 0, "reads": 0, "writes": 0, "db_time": 0.0},
        )

    def test_enabled_metrics_count_reads_and_writes_without_changing_results(self):
        read = _instrument_db_call("reads")(lambda: [1, 2])
        write = _instrument_db_call("writes")(lambda: 42)
        token = start_db_metrics()
        try:
            self.assertEqual(read(), [1, 2])
            self.assertEqual(write(), 42)
            metrics = get_db_metrics()
            self.assertEqual(metrics["calls"], 2)
            self.assertEqual(metrics["reads"], 1)
            self.assertEqual(metrics["writes"], 1)
            self.assertGreaterEqual(metrics["db_time"], 0)
        finally:
            stop_db_metrics(token)

        self.assertEqual(get_db_metrics()["calls"], 0)

    def test_enabled_metrics_preserve_exceptions_and_restore_context(self):
        def fail():
            raise RuntimeError("original failure")

        wrapped = _instrument_db_call("reads")(fail)
        token = start_db_metrics()
        try:
            with self.assertRaisesRegex(RuntimeError, "original failure"):
                wrapped()
            self.assertEqual(get_db_metrics()["calls"], 1)
        finally:
            stop_db_metrics(token)

        self.assertEqual(get_db_metrics()["calls"], 0)


def test_procedure_modal_uses_shared_support_loader_for_cross_checks(monkeypatch):
    """The modal reuses one set-based support payload instead of repeating helper reads."""
    support_loader_calls = []
    cross_check_args = []

    def fake_support_loader(voucher_line_id):
        support_loader_calls.append(voucher_line_id)
        return {
            "transaction_ref": "T-1",
            "client_id": 1,
            "lines": {
                str(voucher_line_id): {
                    "linked_documents": {
                        "voucher_line_id": voucher_line_id,
                        "documents": [{"document_id": 3, "client_data_id": 100}],
                        "count": 1,
                    },
                    "procedure_checks": {"voucher_line_id": voucher_line_id, "checks": []},
                    "observations": {"voucher_line_id": voucher_line_id, "entries": []},
                    "physical_bills": {"voucher_line_id": voucher_line_id, "bills": [], "count": 0},
                }
            }
        }

    monkeypatch.setattr(verification, "get_voucher_support_data", fake_support_loader)
    def fail_query(*args, **kwargs):
        raise AssertionError("procedure modal should reuse support-loader metadata")

    monkeypatch.setattr(verification, "execute_query_pg", fail_query)
    monkeypatch.setattr(verification, "get_voucher_checks_status", lambda **kwargs: [])
    monkeypatch.setattr(
        verification,
        "evaluate_cross_checks_for_line",
        lambda *args, **kwargs: cross_check_args.append(kwargs) or {"results": []},
    )

    response = verification.get_procedure_modal_data(10, perf=True)

    assert response["voucher_line_id"] == 10
    assert response["linked_documents"]["count"] == 1
    assert response["procedure_checks"]["checks"] == []
    # The test replaces execute_query_pg with a stub, so the instrumentation wrapper is not
    # involved; the production request still counts every real database helper call.
    assert response["_perf"]["db_calls"] == 0
    assert support_loader_calls == [10]
    assert cross_check_args == [{"linked_by_document": {3: [100]}}]


def test_cross_check_config_loader_batches_document_chains(monkeypatch):
    """A cold Cross-check config load uses one rule read and one document-chain read."""
    queries = []

    def fake_query(query, params=()):
        queries.append((query, params))
        if "FROM audit_rule_category_cross_checks" in query:
            return [
                {"id": 30, "name": "Invoice to order", "compare_field": "Amount",
                 "comparison_type": "Exact", "compare_to_voucher_line": False},
                {"id": 31, "name": "Receipt to invoice", "compare_field": "Amount",
                 "comparison_type": "Exact", "compare_to_voucher_line": True},
            ]
        if "FROM audit_rule_cross_check_documents" in query:
            return [
                {"cross_check_id": 30, "document_id": 10, "sort_order": 0,
                 "document_name": "Invoice"},
                {"cross_check_id": 31, "document_id": 11, "sort_order": 0,
                 "document_name": "Receipt"},
            ]
        raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr(verification, "cache_get", lambda key: None)
    monkeypatch.setattr(verification, "cache_set", lambda key, value: None)
    monkeypatch.setattr(verification, "execute_query_pg", fake_query)

    configs = verification._get_category_cross_check_configs(7)

    assert len(queries) == 2
    assert len([query for query, _ in queries
                if "FROM audit_rule_cross_check_documents" in query]) == 1
    assert queries[1][1] == (30, 31)
    assert [doc["document_id"] for doc in configs[0]["documents"]] == [10]
    assert [doc["document_id"] for doc in configs[1]["documents"]] == [11]

if __name__ == "__main__":
    unittest.main()
