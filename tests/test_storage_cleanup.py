import unittest
import os
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

import database
from routers import storage_cleanup
from routers.auth import require_admin
from services import storage_cleanup as cleanup_service


class StorageCleanupPreviewTests(unittest.TestCase):
    def test_preview_route_returns_read_only_payload(self):
        expected = {"mode": "dry_run", "database": {"total_bytes": 100, "formatted": "100 B"}}
        with patch.object(storage_cleanup, "get_storage_cleanup_preview", return_value=expected):
            result = storage_cleanup.storage_cleanup_preview({"role": "admin"})
        self.assertEqual(result, expected)

    def test_preview_route_only_calls_preview_service(self):
        with patch.object(storage_cleanup, "get_storage_cleanup_preview", return_value={"mode": "dry_run"}) as preview:
            storage_cleanup.storage_cleanup_preview({"role": "admin"})
        preview.assert_called_once_with()

    def test_admin_guard_rejects_non_admin(self):
        with self.assertRaises(HTTPException) as error:
            require_admin({"role": "analyst"})
        self.assertEqual(error.exception.status_code, 403)

    def test_apply_requires_exact_confirmation_before_preview_or_write(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(cleanup_service, "get_storage_cleanup_preview") as preview, \
                patch.object(cleanup_service, "pg_transaction") as transaction:
            with self.assertRaises(cleanup_service.CleanupConfirmationError):
                cleanup_service.apply_storage_cleanup("fresh-hash", "admin", "DELETE")
        preview.assert_not_called()
        transaction.assert_not_called()

    def test_apply_rejects_stale_preview_before_transaction(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(cleanup_service, "get_storage_cleanup_preview", return_value={"preview_hash": "new-hash"}), \
                patch.object(cleanup_service, "pg_transaction") as transaction:
            with self.assertRaises(cleanup_service.CleanupPreviewStaleError):
                cleanup_service.apply_storage_cleanup(
                    "old-hash",
                    "admin",
                    cleanup_service.CONFIRMATION_TEXT,
                )
        transaction.assert_not_called()

    def test_apply_route_is_locked_before_cleanup_service(self):
        payload = storage_cleanup.StorageCleanupApplyRequest(
            preview_hash='fresh-hash',
            confirmation=cleanup_service.CONFIRMATION_TEXT,
        )
        with patch.object(storage_cleanup, 'apply_storage_cleanup') as apply:
            with self.assertRaises(HTTPException) as error:
                storage_cleanup.storage_cleanup_apply(payload, {'username': 'admin', 'role': 'admin'})
        self.assertEqual(error.exception.status_code, 423)
        apply.assert_not_called()

    def test_delete_count_uses_returning_count_query(self):
        with patch.object(cleanup_service, "execute_query_pg", return_value=[{"count": 7}]) as query:
            self.assertEqual(
                cleanup_service._delete_count("DELETE FROM public.audit_log"),
                7,
            )
        query.assert_called_once()
        self.assertIn("WITH deleted AS (DELETE FROM public.audit_log RETURNING 1)", query.call_args.args[0])

    def test_cashflow_revision_preview_reports_stale_rows_and_retains_uninitialized_clients(self):
        first_rows = iter([
            {"count": 12, "payload_bytes": 1200},
            {"count": 3, "payload_bytes": 300},
            {"count": 4, "payload_bytes": 400},
        ])
        with patch.object(cleanup_service, "_first", side_effect=lambda *args, **kwargs: next(first_rows)), \
                patch.object(cleanup_service, "_count", side_effect=[8, 9]):
            result = cleanup_service._cashflow_revision_candidates()

        self.assertEqual(result["legacy_rollup_rows"]["row_count"], 12)
        self.assertEqual(result["prepared_line_revisions"]["payload_bytes"], 300)
        self.assertEqual(result["prepared_rollup_revisions"]["formatted"], "400 B")
        self.assertEqual(result["retained_without_active_state"], {"prepared_lines": 8, "prepared_rollups": 9})
        self.assertIn("positive active preparation revision", result["safety_rule"])

    def test_table_sizes_classify_cashflow_derived_tables(self):
        with patch.object(cleanup_service, "execute_query_pg", return_value=[
            {"table_name": "cash_bank_transaction_classifications", "table_bytes": 10, "index_bytes": 20, "total_bytes": 30},
            {"table_name": "cashflow_prepared_lines", "table_bytes": 40, "index_bytes": 50, "total_bytes": 90},
        ]):
            result = cleanup_service._table_sizes()

        self.assertEqual({row["classification"] for row in result}, {"derived current data"})
        self.assertEqual({row["table_name"] for row in result}, {
            "cash_bank_transaction_classifications",
            "cashflow_prepared_lines",
        })

    def test_apply_contains_current_only_cashflow_and_reference_guards(self):
        protected = {
            "client_data_current": 10,
            "client_data_retained_for_verification": 2,
            "verification_details": 3,
            "trial_balance_accounts": 4,
            "trial_balance_uploads": 1,
            "cashflow_current_prepared_lines": 6,
            "cashflow_current_prepared_rollups": 7,
        }
        preview = {"preview_hash": "fresh-hash", "protected": protected}
        delete_calls = []
        transaction = MagicMock()
        transaction.__enter__.return_value = transaction
        transaction.__exit__.return_value = False
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(cleanup_service, "get_storage_cleanup_preview", side_effect=[preview, preview]), \
                patch.object(cleanup_service, "pg_transaction", return_value=transaction), \
                patch.object(cleanup_service, "execute_write_pg"), \
                patch.object(cleanup_service, "_delete_count", side_effect=lambda query: delete_calls.append(query) or 0), \
                patch.object(cleanup_service, "_protected_counts", return_value=protected):
            result = cleanup_service.apply_storage_cleanup(
                "fresh-hash",
                "admin",
                cleanup_service.CONFIRMATION_TEXT,
            )

        self.assertEqual(result["mode"], "applied")
        legacy_sql = next(query for query in delete_calls if "cashflow_ledger_rollup_read_models" in query)
        prepared_sql = [
            query for query in delete_calls
            if query.lstrip().startswith("DELETE FROM public.cashflow_prepared_")
        ]
        retired_sql = next(query for query in delete_calls if "DELETE FROM public.client_data" in query)
        self.assertNotIn("report_revision", legacy_sql)
        self.assertEqual(len(prepared_sql), 2)
        self.assertTrue(all("active_preparation_revision > 0" in query for query in prepared_sql))
        self.assertIn("cash_bank_transaction_classifications", retired_sql)
        self.assertIn("cashflow_prepared_lines", retired_sql)

    def test_existing_postgres_schema_path_ensures_cleanup_run_ledger(self):
        marker_cursor = MagicMock()
        marker_cursor.fetchone.return_value = ("audit_rules_config_state",)
        cleanup_cursor = MagicMock()
        cleanup_cursor.__enter__.return_value = cleanup_cursor
        cleanup_cursor.__exit__.return_value = False
        connection = MagicMock()
        connection.cursor.side_effect = [marker_cursor, cleanup_cursor]

        with patch.object(database, "_POSTGRES_SCHEMA_READY", False), \
                patch.object(database, "_ensure_postgres_audit_rule_document_type_constraint", return_value=[]), \
                patch.object(database, "_ensure_postgres_form_field_columns", return_value=[]), \
                patch.object(database, "_ensure_postgres_form_tables", return_value=[]), \
                patch.object(database, "_ensure_postgres_report_category_tables"), \
                patch.object(database, "_ensure_postgres_audit_rule_assertions", return_value=False), \
                patch.object(database, "_ensure_postgres_category_document_assertions", return_value=False), \
                patch.object(database, "_ensure_postgres_category_cross_check_assertions", return_value=False), \
                patch.object(database, "_ensure_postgres_voucher_check_assertions", return_value=False):
            database.init_db_postgres(connection)

        cleanup_cursor.execute.assert_called_once()
        self.assertIn("CREATE TABLE IF NOT EXISTS storage_cleanup_runs", cleanup_cursor.execute.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
