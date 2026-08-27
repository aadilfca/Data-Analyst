import unittest

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


if __name__ == "__main__":
    unittest.main()
