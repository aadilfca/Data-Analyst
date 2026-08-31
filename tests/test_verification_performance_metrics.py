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


def test_procedure_modal_seeds_sibling_ids_into_each_worker(monkeypatch):
    """The five modal reads reuse one sibling-id resolution across executor threads."""
    resolver_calls = []

    def fake_resolver(voucher_line_id):
        memo = verification._sibling_ids_request_memo.get()
        if memo is not None and voucher_line_id in memo:
            return memo[voucher_line_id]
        resolver_calls.append(voucher_line_id)
        return [voucher_line_id, voucher_line_id + 1]

    def support_result(voucher_line_id):
        fake_resolver(voucher_line_id)
        return {}

    monkeypatch.setattr(verification, "_resolve_sibling_voucher_line_ids", fake_resolver)
    monkeypatch.setattr(verification, "get_voucher_line_documents", support_result)
    monkeypatch.setattr(verification, "get_procedure_checks", support_result)
    monkeypatch.setattr(verification, "get_line_observations", support_result)
    monkeypatch.setattr(verification, "get_voucher_line_physical_bills", support_result)
    monkeypatch.setattr(verification, "get_cross_check_results", support_result)
    monkeypatch.setattr(
        verification,
        "execute_query_pg",
        lambda query, params=(): [{"transaction_ref": "T-1", "client_id": 1}],
    )
    monkeypatch.setattr(verification, "get_voucher_checks_status", lambda **kwargs: [])

    response = verification.get_procedure_modal_data(10, perf=True)

    assert response["voucher_line_id"] == 10
    assert response["_perf"]["db_calls"] == 0
    assert resolver_calls == [10]

if __name__ == "__main__":
    unittest.main()
