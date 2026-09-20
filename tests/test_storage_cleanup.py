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

    def test_apply_route_passes_fresh_hash_and_admin_operator(self):
        payload = storage_cleanup.StorageCleanupApplyRequest(
            preview_hash="fresh-hash",
            confirmation=cleanup_service.CONFIRMATION_TEXT,
        )
        expected = {"mode": "applied", "deleted_counts": {"audit_log": 2}}
        with patch.object(storage_cleanup, "apply_storage_cleanup", return_value=expected) as apply:
            result = storage_cleanup.storage_cleanup_apply(payload, {"username": "admin", "role": "admin"})
        self.assertEqual(result, expected)
        apply.assert_called_once_with(
            preview_hash="fresh-hash",
            operator="admin",
            confirmation=cleanup_service.CONFIRMATION_TEXT,
        )

    def test_delete_count_uses_returning_count_query(self):
        with patch.object(cleanup_service, "execute_query_pg", return_value=[{"count": 7}]) as query:
            self.assertEqual(
                cleanup_service._delete_count("DELETE FROM public.audit_log"),
                7,
            )
        query.assert_called_once()
        self.assertIn("WITH deleted AS (DELETE FROM public.audit_log RETURNING 1)", query.call_args.args[0])

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
                patch.object(database, "_ensure_postgres_category_document_assertions", return_value=False):
            database.init_db_postgres(connection)

        cleanup_cursor.execute.assert_called_once()
        self.assertIn("CREATE TABLE IF NOT EXISTS storage_cleanup_runs", cleanup_cursor.execute.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
