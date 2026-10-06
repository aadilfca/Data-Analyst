import os
import sqlite3
import tempfile
import unittest

import database


TABLE_NAME = "cash_bank_transaction_classifications"
INDEX_NAMES = {
    "idx_cash_bank_classifications_client_date",
    "idx_cash_bank_classifications_client_voucher",
    "idx_cash_bank_classifications_client_account_date",
    "idx_cash_bank_classifications_source_version",
}


class _FakeCursor:
    def __init__(self):
        self.statements = []
        self.closed = False

    def execute(self, statement):
        self.statements.append(statement)

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self):
        self.cursor_instance = _FakeCursor()
        self.rollback_called = False

    def cursor(self):
        return self.cursor_instance

    def rollback(self):
        self.rollback_called = True


class CashBankTransactionClassificationSchemaTests(unittest.TestCase):
    def setUp(self):
        self.original_db_name = database.DB_NAME
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_NAME = os.path.join(self.temp_dir.name, "phase2.sqlite")

    def tearDown(self):
        database.DB_NAME = self.original_db_name
        self.temp_dir.cleanup()

    def test_sqlite_schema_is_additive_and_indexed(self):
        database.init_db()
        with sqlite3.connect(database.DB_NAME) as connection:
            table = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (TABLE_NAME,),
            ).fetchone()
            self.assertIsNotNone(table)

            columns = {
                row[1]
                for row in connection.execute(
                    f"PRAGMA table_info({TABLE_NAME})"
                ).fetchall()
            }
            self.assertTrue(
                {
                    "client_data_id",
                    "voucher_id",
                    "account_code",
                    "account_name",
                    "transaction_date",
                    "debit",
                    "credit",
                    "configuration_version",
                    "classification_status",
                    "is_current",
                }.issubset(columns)
            )

            indexes = {
                row[1]
                for row in connection.execute(
                    "SELECT type, name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
            self.assertTrue(INDEX_NAMES.issubset(indexes))
        connection.close()

    def test_postgres_fast_path_uses_idempotent_table_and_indexes(self):
        connection = _FakeConnection()
        database._ensure_postgres_cash_bank_transaction_classifications(connection)

        statements = "\n".join(connection.cursor_instance.statements)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS cash_bank_transaction_classifications",
            statements,
        )
        for index_name in INDEX_NAMES:
            self.assertIn(index_name, statements)
        self.assertTrue(connection.cursor_instance.closed)
        self.assertFalse(connection.rollback_called)


if __name__ == "__main__":
    unittest.main()