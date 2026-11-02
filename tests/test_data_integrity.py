import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from database import ROW_OVERHEAD_BYTES
from routers.data_integrity import _module_row, get_data_integrity_overview


class DataIntegrityOverviewTests(unittest.TestCase):
    def _query_response(self, query, params=None):
        normalized = " ".join(query.split()).lower()
        self.assertTrue(normalized.startswith("select"))
        if "from clients" in normalized:
            return [{"id": 7, "client_name": "Leather Crop"}]
        if "sum(length(cd.row_data::text))" in normalized:
            return [{"row_count": 3, "data_bytes": 868, "unidentified_rows": 0}]
        if "from uploaded_files u" in normalized:
            return [{"id": 11, "file_path": self.source_path, "row_bytes": 12}]
        if "group by template_id, source_identity" in normalized:
            return [{"duplicate_groups": 1, "duplicate_extra_rows": 1}]
        if "sum(pg_column_size(a))" in normalized:
            return [{"row_count": 4, "data_bytes": 200}]
        if "from trial_balance_uploads u where" in normalized:
            return [{"id": 21, "row_bytes": 15}, {"id": 22, "row_bytes": 15}]
        if "group by a.upload_id, a.account_code" in normalized:
            return [{"duplicate_groups": 0, "duplicate_extra_rows": 0}]
        if "sum(pg_column_size(r))" in normalized:
            return [{"row_count": 1, "data_bytes": 30}]
        if "group by lower(btrim(group_label))" in normalized:
            return [{"duplicate_groups": 0, "duplicate_extra_rows": 0}]
        if "pg_database_size(current_database())" in normalized:
            return [{"database_bytes": 10000, "shared_index_bytes": 3000}]
        self.fail(f"Unexpected query: {query}")

    def test_inventory_keeps_module_duplicate_scopes_and_sizes_separate(self):
        with tempfile.TemporaryDirectory() as staging:
            self.source_path = os.path.join(staging, "source.xlsx")
            with open(self.source_path, "wb") as source:
                source.write(b"workbook-bytes")
            with patch.dict(os.environ, {"UPLOAD_STAGING_DIR": staging}), patch(
                "routers.data_integrity.execute_query_pg", side_effect=self._query_response
            ) as query:
                result = get_data_integrity_overview(client_id=7)

        self.assertEqual(result["overall_status"], "Review")
        self.assertEqual(result["client_name"], "Leather Crop")
        self.assertEqual(
            [row["module"] for row in result["modules"]],
            ["Client Data", "Trial Balance", "Reconciliation"],
        )
        client_data, trial_balance, reconciliation = result["modules"]
        self.assertEqual(client_data["duplicate_groups"], 1)
        self.assertEqual(client_data["duplicate_extra_rows"], 1)
        self.assertEqual(client_data["total_data_bytes"], 894)
        self.assertEqual(trial_balance["duplicate_status"], "Clear")
        self.assertEqual(trial_balance["associated_record_count"], 2)
        self.assertEqual(trial_balance["total_data_bytes"], 230)
        self.assertEqual(reconciliation["total_data_bytes"], 30)
        self.assertEqual(result["database_context"]["shared_index_bytes"], 3000)
        self.assertTrue(all(call.args[0].lstrip().upper().startswith("SELECT") for call in query.call_args_list))
        client_size_query = next(
            call for call in query.call_args_list if "SUM(LENGTH(cd.row_data::text))" in call.args[0]
        )
        self.assertEqual(client_size_query.args[1], (ROW_OVERHEAD_BYTES, 7))

    def test_source_file_outside_upload_staging_is_not_read_or_counted(self):
        with tempfile.TemporaryDirectory() as staging, tempfile.TemporaryDirectory() as external:
            self.source_path = os.path.join(external, "outside.xlsx")
            with open(self.source_path, "wb") as source:
                source.write(b"private")
            with patch.dict(os.environ, {"UPLOAD_STAGING_DIR": staging}):
                from routers.data_integrity import _measure_source_files

                measured, unmeasured = _measure_source_files([{"file_path": self.source_path}])

        self.assertEqual(measured, 0)
        self.assertEqual(unmeasured, 1)

    def test_missing_identity_marks_duplicate_scan_incomplete_not_clear(self):
        row = _module_row(
            "Client Data", 4, 0, 100, 0, 0, 0, "Saved source identity.", unidentified_rows=1
        )

        self.assertEqual(row["duplicate_status"], "Not fully checked")
        self.assertEqual(row["unidentified_rows"], 1)

    def test_inactive_client_is_rejected_before_scanning(self):
        with patch("routers.data_integrity.execute_query_pg", return_value=[]) as query:
            with self.assertRaises(HTTPException) as raised:
                get_data_integrity_overview(client_id=7)

        self.assertEqual(raised.exception.status_code, 404)
        query.assert_called_once()


if __name__ == "__main__":
    unittest.main()
