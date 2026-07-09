import os
import sqlite3
import tempfile
import unittest

import database
from database import init_db


class VerificationIndexTests(unittest.TestCase):
    def test_cashbook_lookup_index_is_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, 'test.db')
            database.DB_NAME = db_path
            init_db()

            conn = sqlite3.connect(db_path)
            try:
                indexes = [row[1] for row in conn.execute("PRAGMA index_list('client_data')")]
                self.assertIn('idx_client_data_client_deleted_extract_date', indexes)
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
