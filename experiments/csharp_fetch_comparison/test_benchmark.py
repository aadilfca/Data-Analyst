import argparse
from pathlib import Path
import tempfile
import unittest

from experiments.csharp_fetch_comparison import benchmark


class FetchBenchmarkTests(unittest.TestCase):
    def test_hash_is_stable_and_covers_each_source_value(self):
        row = {"id": 1, "template_id": 2, "upload_id": 3, "version": 1,
               "source_fingerprint": None, "extract_date": "2026-01-01",
               "extract_amount": "0.0", "row_data": "{\"x\":1}"}
        first, size = benchmark.row_digest([row])
        self.assertEqual(first, benchmark.row_digest([row])[0])
        self.assertEqual(size, sum(len(str(value).encode()) for value in row.values() if value is not None))
        changed = dict(row, row_data="{\"x\":2}")
        self.assertNotEqual(first, benchmark.row_digest([changed])[0])

    def test_validation_requires_live_and_bounded_explicit_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            args = argparse.Namespace(live=False, client_id=5, template_id=1,
                start="2026-01-01", end="2026-06-30", repeats=3, output=str(output))
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.validate_args(args)
            args.live = True
            args.repeats = 4
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.validate_args(args)

    def test_date_limits_and_output_overwrite_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.json"
            args = argparse.Namespace(live=True, client_id=5, template_id=1,
                start="2025-01-01", end="2026-06-30", repeats=3, output=str(output))
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.validate_args(args)
            args.start, args.end = "2026-01-01", "2026-06-30"
            output.write_text("keep", encoding="utf-8")
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.validate_args(args)
            self.assertEqual("keep", output.read_text(encoding="utf-8"))

    def test_pair_mismatch_is_not_written(self):
        left = {"rows": 1, "data_bytes": 10, "sha256": "a"}
        right = {"rows": 1, "data_bytes": 10, "sha256": "b"}
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.assert_comparable(left, right)
        benchmark.assert_comparable(left, dict(left))

    def test_csharp_result_uses_common_timing_and_digest_fields(self):
        normalized = benchmark.normalize_csharp_result({
            "status": "success", "rows": 2, "dataBytes": 8,
            "fetchSeconds": 0.1, "hashSeconds": 0.01, "sha256": "abc"})
        self.assertEqual({"rows": 2, "data_bytes": 8, "fetch_seconds": 0.1,
                          "hash_seconds": 0.01, "sha256": "abc"}, normalized)


if __name__ == "__main__":
    unittest.main()
