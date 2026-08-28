import os
import tempfile
import unittest
from unittest.mock import patch

import database
from database import execute_write, init_db
from routers.import_export import count_new_and_existing_rows


class UploadValidationCountTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        database.DB_NAME = os.path.join(self.tmpdir.name, "test.db")
        init_db()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_counts_existing_and_new_rows(self):
        execute_write(
            "INSERT INTO client_data (client_id, template_id, upload_id, row_data, created_by, version) VALUES (?, ?, ?, ?, ?, ?)",
            (1, 1, 1, '{"Voucher":"ABC","Amount":100}', "Analyst", 1),
        )

        template_cols = [
            {"id": 1, "column_name": "Voucher", "data_type": "Text"},
            {"id": 2, "column_name": "Amount", "data_type": "Numeric"},
        ]

        class MappingStub:
            def __init__(self, excel_column_name):
                self.excel_column_name = excel_column_name

        mapping_dict = {
            1: MappingStub("Voucher"),
            2: MappingStub("Amount"),
        }

        rows = [
            {"Voucher": "ABC", "Amount": 100},
            {"Voucher": "XYZ", "Amount": 200},
        ]

        # The count helper reads Postgres in production, while the fixture row above
        # is intentionally created in isolated SQLite. Stub only that external read
        # so the comparison logic itself remains under test.
        with patch(
            "routers.import_export.execute_query_pg",
            return_value=[{"row_data": {"Voucher": "ABC", "Amount": 100}}],
        ):
            new_count, existing_count = count_new_and_existing_rows(
                1, 1, rows, template_cols, mapping_dict
            )

        self.assertEqual(new_count, 1)
        self.assertEqual(existing_count, 1)


if __name__ == "__main__":
    unittest.main()
