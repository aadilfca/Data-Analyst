"""Phase 5 coverage for versioned Account Control Centre synchronization."""

import unittest
from unittest.mock import patch

from services import cash_bank_classification as classification


class _Cursor:
    def __init__(self):
        self.statements = []
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, params=None):
        sql = " ".join(statement.split())
        self.statements.append((sql, params))
        if "SELECT account_code, treasury_type" in sql:
            self._rows = [{"account_code": "CASH", "treasury_type": "cash"}]
        elif "MAX(version_number)" in sql:
            self._rows = [{"current_version": 1}]
        elif "RETURNING id" in sql:
            self._rows = [(9,)]
        elif "SELECT id, client_id, template_id" in sql:
            self._rows = [{
                "id": 44,
                "client_id": 7,
                "template_id": 2,
                "upload_id": 3,
                "row_data": {"Account Code": "BANK", "Voucher ID": "V-1", "Debit": "10"},
                "extract_date": "2026-01-01",
            }]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Connection:
    def __init__(self):
        self.cursor_instance = _Cursor()

    def cursor(self, cursor_factory=None):
        return self.cursor_instance


class CashBankConfigurationSyncTests(unittest.TestCase):
    def test_historical_orphans_are_read_only_and_grouped_under_nearest_current_parent(self):
        queried = []

        def query(sql, params):
            queried.append((sql, params))
            return [{
                "account_code": "60-10-70-0007",
                "source_account_name": "Pool Bikes",
                "transaction_count": 6,
                "voucher_count": 2,
                "first_transaction_date": "2026-01-01",
                "last_transaction_date": "2026-01-30",
                "category_id": 4,
            }]

        result = classification.discover_historical_orphan_accounts_postgres(
            7,
            [{"account_code": "60-10-70", "account_name": "Vehicles Running And Maintenance - Admin"}],
            query_fn=query,
        )

        self.assertEqual(len(queried), 1)
        self.assertEqual(queried[0][1], (7,))
        self.assertIn("account_control_cashflow_category_assignments", queried[0][0])
        self.assertEqual(result[0]["category_id"], 4)
        self.assertNotIn("INSERT", queried[0][0].upper())
        self.assertEqual(result[0]["parent_account_code"], "60-10-70")
        self.assertEqual(result[0]["parent_account_name"], "Vehicles Running And Maintenance - Admin")
        self.assertTrue(result[0]["is_historical_orphan"])
        self.assertTrue(result[0]["is_read_only"])
    def test_action_names_cover_add_edit_and_deactivate(self):
        current = {("CASH", "cash")}
        self.assertEqual(classification._configuration_action(set(), current), "created")
        self.assertEqual(classification._configuration_action(current, set()), "deactivated")
        self.assertEqual(classification._configuration_action(current, current | {("BANK", "bank")}), "added")
        self.assertEqual(classification._configuration_action(current, {("BANK", "bank")}), "edited")

    def test_sync_creates_new_version_soft_deactivates_and_preserves_source_payload(self):
        connection = _Connection()
        source_before = {
            "id": 44,
            "client_id": 7,
            "template_id": 2,
            "upload_id": 3,
            "row_data": {"Account Code": "BANK", "Voucher ID": "V-1", "Debit": "10"},
            "extract_date": "2026-01-01",
        }
        with patch.object(classification, "execute_values"), patch.object(
            classification, "upsert_classification_rows_postgres", return_value=[]
        ) as upsert:
            result = classification.synchronize_treasury_accounts_postgres(
                connection,
                client_id=7,
                cash_account_codes=[],
                bank_account_codes=["BANK"],
                changed_by="Admin",
            )

        self.assertTrue(result["changed"])
        self.assertEqual(result["configuration_version"], 2)
        self.assertEqual(result["action"], "edited")
        self.assertEqual(result["affected_account_codes"], ["BANK", "CASH"])
        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.kwargs["configuration_version"], 2)
        self.assertEqual(
            source_before["row_data"],
            {"Account Code": "BANK", "Voucher ID": "V-1", "Debit": "10"},
        )
        sql = "\n".join(statement for statement, _params in connection.cursor_instance.statements)
        self.assertIn("SET is_active = FALSE", sql)
        self.assertIn("SET is_current = FALSE", sql)
        self.assertNotIn("DELETE FROM account_control_cashflow_treasury_accounts", sql)


if __name__ == "__main__":
    unittest.main()
