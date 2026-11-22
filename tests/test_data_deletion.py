import os
import sqlite3
import unittest
from datetime import datetime
from contextlib import ExitStack
from unittest.mock import MagicMock, patch
import pytest

from fastapi import HTTPException

import database
from routers import clients
from routers import data
from services import storage_cleanup


class PermanentDataDeletionTests(unittest.TestCase):
    def test_upload_history_api_is_scoped_paginated_and_newest_first(self):
        rows = [{"id": 9, "client_data_id": 44, "action": "deleted",
                 "changed_at": datetime(2026, 11, 4, 10, 30),
                 "changed_by": "Analyst", "row_data": {"Voucher": "V-1"}}]
        with patch.object(data, "execute_query_pg", side_effect=[
            [{"id": 5}], [{"id": 2, "template_name": "Ledger"}], [{"total": 26}], rows
        ]) as query, patch.object(data, "get_template_columns_ordered", return_value=[{"column_name": "Voucher"}]):
            result = data.get_client_data_history(5, 2, 2, 25, "all", None)

        self.assertEqual(result["total"], 26)
        self.assertEqual(result["total_pages"], 2)
        self.assertEqual(result["rows"][0]["changed_at"], "2026-11-04T10:30:00")
        self.assertIn("ORDER BY h.changed_at DESC, h.id DESC", query.call_args_list[-1].args[0])
        self.assertEqual(query.call_args_list[-1].args[1], (5, 2, 25, 25))

    def test_upload_history_api_rejects_inactive_client(self):
        with patch.object(data, "execute_query_pg", return_value=[]):
            with self.assertRaises(HTTPException) as error:
                data.get_client_data_history(5, 2, 1, 25, "all", None)
        self.assertEqual(error.exception.status_code, 404)

    def test_upload_history_api_filters_by_action_and_ledger_account(self):
        with patch.object(data, "execute_query_pg", side_effect=[
            [{"id": 5}],
            [{"id": 2, "template_name": "Ledger", "data_purpose": "account_ledger"}],
            [{"id": 1}],
            [{"total": 1}],
            [{"id": 9, "client_data_id": 44, "action": "changed", "row_data": {},
              "changed_fields": {"Debit": {"old_value": 1, "new_value": 2}}}],
        ]) as query, patch.object(
            data, "get_template_columns_ordered", return_value=[{"column_name": "Voucher"}]
        ):
            result = data.get_client_data_history(5, 2, 1, 25, "changed", "50-95-0003")

        self.assertEqual(result["rows"][0]["changed_fields"]["Debit"]["new_value"], 2)
        self.assertIn("h.action = %s", query.call_args_list[-2].args[0])
        self.assertIn("COALESCE(h.ledger_account_code, uf.ledger_account_code) = %s", query.call_args_list[-1].args[0])
        self.assertEqual(query.call_args_list[-1].args[1], (5, 2, "changed", "50-95-0003", 25, 0))

    def test_ledger_account_choices_include_history_accounts(self):
        with patch.object(data, "execute_query_pg", side_effect=[
            [{"id": 5}], [{"id": 2, "data_purpose": "account_ledger"}],
            [{"account_code": "CURRENT", "account_name": "Current"}],
            [{"account_code": "OLD", "account_name": "Old"}],
        ]):
            result = data.get_preview_ledger_accounts(5, 2)
        self.assertEqual({row["account_code"] for row in result["accounts"]}, {"CURRENT", "OLD"})

    def test_local_schema_has_scoped_history_table_and_single_lookup_index(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory, patch.object(
            database, "DB_NAME", os.path.join(directory, "history-test.db")
        ):
            database.init_db()
            conn = sqlite3.connect(database.DB_NAME)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(client_data_history)")}
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(client_data_history)")}
            finally:
                conn.close()

        self.assertTrue({"client_id", "template_id", "client_data_id", "action", "changed_at", "row_data"}.issubset(columns))
        self.assertTrue({"changed_fields", "ledger_account_code", "ledger_account_name"}.issubset(columns))
        self.assertIn("idx_client_data_history_scope_time", indexes)

    def test_local_schema_adds_history_details_to_an_existing_table(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory, patch.object(
            database, "DB_NAME", os.path.join(directory, "old-history-test.db")
        ):
            conn = sqlite3.connect(database.DB_NAME)
            conn.execute("""CREATE TABLE client_data_history (
                id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL,
                template_id INTEGER NOT NULL, client_data_id INTEGER NOT NULL,
                action TEXT NOT NULL, changed_at TIMESTAMP, changed_by TEXT,
                source_upload_id INTEGER, replacing_upload_id INTEGER,
                source_identity TEXT, row_data TEXT NOT NULL
            )""")
            conn.commit()
            conn.close()
            database.init_db()
            conn = sqlite3.connect(database.DB_NAME)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(client_data_history)")}
            finally:
                conn.close()

        self.assertTrue({"changed_fields", "ledger_account_code", "ledger_account_name"}.issubset(columns))

    def test_existing_postgres_schema_creates_history_table_and_index(self):
        class CursorStub:
            def __init__(self):
                self.statements = []

            def execute(self, statement, params=None):
                self.statements.append(statement)

            def fetchone(self):
                return ("audit_rules_config_state",)

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        conn = MagicMock()
        cursor = CursorStub()
        conn.cursor.return_value = cursor
        migration_helpers = (
            "_ensure_postgres_storage_cleanup_runs",
            "_ensure_postgres_upload_validation_plans",
            "_ensure_postgres_upload_summary_columns",
            "_ensure_postgres_audit_rule_document_type_constraint",
            "_ensure_postgres_form_field_columns",
            "_ensure_postgres_form_tables",
            "_ensure_account_ledger_schema",
            "_ensure_upload_lifecycle_schema",
            "_ensure_postgres_report_category_tables",
            "_ensure_postgres_account_set_tables",
            "_ensure_postgres_trial_balance_source_code",
            "_ensure_postgres_trial_balance_column_mappings",
            "_ensure_postgres_trial_balance_rollforward_adjustments",
            "_ensure_postgres_cash_bank_transaction_classifications",
            "_ensure_postgres_cashflow_voucher_occurrences",
            "_ensure_postgres_cashflow_prepared_tables",
            "_ensure_postgres_cashflow_report_source_policy",
            "_ensure_postgres_audit_rule_assertions",
            "_ensure_postgres_category_compiled_rulebooks",
            "_ensure_postgres_category_document_assertions",
            "_ensure_postgres_category_cross_check_assertions",
            "_ensure_postgres_voucher_check_assertions",
            "_ensure_postgres_applied_ruleset_hash",
            "_ensure_postgres_local_vendor_discovery_indexes",
            "_ensure_postgres_client_data_canonical_columns",
        )

        with ExitStack() as stack:
            stack.enter_context(patch.object(database, "_POSTGRES_SCHEMA_READY", False))
            for helper in migration_helpers:
                stack.enter_context(patch.object(database, helper, return_value=False))
            database.init_db_postgres(conn)

        statements = "\n".join(cursor.statements)
        self.assertIn("CREATE TABLE IF NOT EXISTS client_data_history", statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS changed_fields JSONB", statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS ledger_account_code TEXT", statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS ledger_account_name TEXT", statements)
        self.assertIn("idx_client_data_history_scope_time", statements)
        conn.commit.assert_called_once_with()

    def test_scope_predicate_is_parameterized_for_client_and_template(self):
        predicate, params = storage_cleanup._purge_scope_predicate("cd", 5, 1)
        self.assertEqual(predicate, "cd.client_id = %s AND cd.template_id = %s")
        self.assertEqual(params, [5, 1])

    def test_upload_path_cleanup_never_leaves_uploads_directory(self):
        self.assertTrue(storage_cleanup._safe_upload_file_path("uploads/example.xlsx"))
        self.assertIsNone(storage_cleanup._safe_upload_file_path("C:/outside/example.xlsx"))

    def test_preview_hash_changes_when_scope_counts_change(self):
        preview = {
            "scope": {"client_id": 5, "template_id": 1},
            "upload_ids": [10],
            "counts": {"client_data": 3},
            "protected": {"verification_details": 0},
        }
        first = storage_cleanup._purge_preview_hash(preview)
        preview["counts"]["client_data"] = 4
        self.assertNotEqual(first, storage_cleanup._purge_preview_hash(preview))

    def test_preview_hash_binds_linked_verification_choice_and_counts(self):
        base = {
            "scope": {"client_id": 5, "template_id": 1},
            "upload_ids": [],
            "counts": {"client_data": 3},
            "protected": {},
            "include_linked_verification": False,
            "linked_verification": {},
        }
        ordinary_hash = storage_cleanup._purge_preview_hash(base)
        reset = {**base, "include_linked_verification": True,
                 "linked_verification": {"verification_details": 78}}
        self.assertNotEqual(ordinary_hash, storage_cleanup._purge_preview_hash(reset))

    def test_test_verification_purge_requires_matching_explicit_supabase_ref(self):
        with patch.dict(os.environ, {
            "ALLOW_TEST_VERIFICATION_DATA_PURGE": "true",
            "TEST_DATA_RESET_SUPABASE_PROJECT_REF": "testproject123",
            "DATABASE_URL": "postgresql://postgres:secret@db.testproject123.supabase.co:5432/postgres",
        }, clear=False):
            self.assertTrue(storage_cleanup._test_verification_purge_is_enabled())
        with patch.dict(os.environ, {
            "ALLOW_TEST_VERIFICATION_DATA_PURGE": "true",
            "TEST_DATA_RESET_SUPABASE_PROJECT_REF": "testproject123",
            "DATABASE_URL": "postgresql://postgres:secret@db.production123.supabase.co:5432/postgres",
        }, clear=False):
            self.assertFalse(storage_cleanup._test_verification_purge_is_enabled())
        with patch.dict(os.environ, {
            "ALLOW_TEST_VERIFICATION_DATA_PURGE": "true",
            "TEST_DATA_RESET_SUPABASE_PROJECT_REF": "testproject123",
            "DATABASE_URL": "postgresql://postgres.testproject123:secret@aws-0-ap-south-1.pooler.supabase.com:6543/postgres",
        }, clear=False):
            self.assertTrue(storage_cleanup._test_verification_purge_is_enabled())

    def test_apply_rejects_wrong_confirmation_before_reading_preview(self):
        with patch.object(storage_cleanup, "get_client_data_purge_preview") as preview:
            with self.assertRaises(storage_cleanup.CleanupConfirmationError):
                storage_cleanup.apply_client_data_purge(
                    "hash", "admin", "DELETE", 5
                )
        preview.assert_not_called()

    def test_apply_rejects_stale_preview_before_transaction(self):
        with patch.object(
            storage_cleanup,
            "get_client_data_purge_preview",
            return_value={"preview_hash": "new", "can_apply": True},
        ), patch.object(storage_cleanup, "pg_transaction") as transaction:
            with self.assertRaises(storage_cleanup.DataPurgePreviewStaleError):
                storage_cleanup.apply_client_data_purge(
                    "old",
                    "admin",
                    storage_cleanup.PERMANENT_DATA_DELETE_CONFIRMATION,
                    5,
                )
        transaction.assert_not_called()

    def test_apply_blocks_protected_references_before_transaction(self):
        preview = {"preview_hash": "same", "can_apply": False, "protected": {"verification_details": 1}}
        with patch.object(storage_cleanup, "get_client_data_purge_preview", return_value=preview), \
                patch.object(storage_cleanup, "pg_transaction") as transaction:
            with self.assertRaises(storage_cleanup.DataPurgeProtectedError):
                storage_cleanup.apply_client_data_purge(
                    "same",
                    "admin",
                    storage_cleanup.PERMANENT_DATA_DELETE_CONFIRMATION,
                    5,
                )
        transaction.assert_not_called()

    def test_apply_deletes_derived_rows_before_source_and_upload_metadata(self):
        preview = {
            "preview_hash": "same",
            "can_apply": True,
            "scope": {"client_id": 5, "template_id": None},
            "upload_ids": [10],
        }
        transaction = MagicMock()
        transaction.__enter__.return_value = transaction
        transaction.__exit__.return_value = False
        delete_sql = []

        def count(query, params=None):
            if query.lstrip().startswith("WITH deleted AS"):
                delete_sql.append(query)
            return 0

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(storage_cleanup, "get_client_data_purge_preview", side_effect=[preview, preview, preview]), \
                patch.object(storage_cleanup, "_purge_target_uploads", return_value=[{"file_path": "uploads/a.xlsx"}]), \
                patch.object(storage_cleanup, "pg_transaction", return_value=transaction), \
                patch.object(storage_cleanup, "_count", side_effect=count), \
                patch.object(storage_cleanup, "execute_query_pg"), \
                patch.object(storage_cleanup, "execute_write_pg"), \
                patch.object(storage_cleanup, "_remove_staged_upload_files", return_value=[]):
            result = storage_cleanup.apply_client_data_purge(
                "same",
                "admin",
                storage_cleanup.PERMANENT_DATA_DELETE_CONFIRMATION,
                5,
            )

        self.assertEqual(result["staged_file_cleanup_failures"], [])
        sql_text = "\n".join(delete_sql)
        self.assertLess(sql_text.index("cashflow_prepared_lines"), sql_text.index("client_data"))
        self.assertLess(sql_text.index("DELETE FROM public.client_data_history"),
                        sql_text.index("DELETE FROM public.client_data cd"))
        self.assertLess(sql_text.index("DELETE FROM public.client_data cd"), sql_text.index("uploaded_files"))

    def test_test_reset_deletes_only_scoped_verification_children_before_source(self):
        preview = {
            "preview_hash": "same",
            "can_apply": True,
            "scope": {"client_id": 5, "template_id": 1},
            "upload_ids": [],
            "audit_log_ids": [],
        }
        transaction = MagicMock()
        transaction.__enter__.return_value = transaction
        transaction.__exit__.return_value = False
        delete_sql = []

        def count(query, params=None):
            if query.lstrip().startswith("WITH deleted AS"):
                delete_sql.append(query)
            return 0

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(storage_cleanup, "get_client_data_purge_preview", side_effect=[preview, preview, preview]), \
                patch.object(storage_cleanup, "_purge_target_uploads", return_value=[]), \
                patch.object(storage_cleanup, "pg_transaction", return_value=transaction), \
                patch.object(storage_cleanup, "_count", side_effect=count), \
                patch.object(storage_cleanup, "execute_query_pg", return_value=[]), \
                patch.object(storage_cleanup, "execute_write_pg"), \
                patch.object(storage_cleanup, "_remove_staged_upload_files", return_value=[]):
            storage_cleanup.apply_client_data_purge(
                "same", "admin", storage_cleanup.PERMANENT_DATA_DELETE_CONFIRMATION,
                5, 1, include_linked_verification=True,
            )

        sql_text = "\n".join(delete_sql)
        self.assertIn("DELETE FROM public.voucher_line_form_values", sql_text)
        self.assertIn("DELETE FROM public.voucher_line_form_entries", sql_text)
        self.assertIn("DELETE FROM public.voucher_check_status", sql_text)
        self.assertIn("DELETE FROM public.verification_details", sql_text)
        self.assertLess(sql_text.index("voucher_line_form_values"), sql_text.index("voucher_line_form_entries"))
        self.assertLess(sql_text.index("voucher_line_form_entries"), sql_text.index("verification_details"))
        self.assertLess(sql_text.index("verification_details"), sql_text.index("DELETE FROM public.client_data cd"))

    def test_preview_route_is_admin_scoped_and_read_only(self):
        expected = {"mode": "permanent_data_delete_dry_run"}
        with patch.object(clients, "get_client_data_purge_preview", return_value=expected) as preview:
            result = clients.client_data_purge_preview(5, 1, session={"role": "admin"})
        self.assertEqual(result, expected)
        preview.assert_called_once_with(5, 1, False)

    def test_apply_route_maps_protected_reference_to_conflict(self):
        payload = clients.ClientDataPurgeRequest(
            preview_hash="same",
            confirmation=storage_cleanup.PERMANENT_DATA_DELETE_CONFIRMATION,
        )
        with patch.object(
            clients,
            "apply_client_data_purge",
            side_effect=storage_cleanup.DataPurgeProtectedError("protected"),
        ):
            with self.assertRaises(HTTPException) as error:
                clients.client_data_purge(5, payload, {"username": "admin", "role": "admin"})
        self.assertEqual(error.exception.status_code, 409)

    def test_apply_route_forwards_explicit_linked_verification_choice(self):
        payload = clients.ClientDataPurgeRequest(
            preview_hash="same",
            confirmation=storage_cleanup.PERMANENT_DATA_DELETE_CONFIRMATION,
            template_id=1,
            include_linked_verification=True,
        )
        with patch.object(clients, "apply_client_data_purge", return_value={"ok": True}) as apply:
            result = clients.client_data_purge(5, payload, {"username": "admin", "role": "admin"})
        self.assertEqual(result, {"ok": True})
        self.assertTrue(apply.call_args.kwargs["include_linked_verification"])


def test_replacement_purge_blocks_evidence_before_any_delete():
    from contextlib import nullcontext
    import pytest
    with patch.object(storage_cleanup, "pg_transaction", return_value=nullcontext()), \
            patch.object(storage_cleanup, "execute_query_pg", return_value=[{"id": 9, "upload_id": 4, "derived_voucher_id": "V1"}]), \
            patch.object(storage_cleanup, "_count", return_value=1), \
            patch.object(storage_cleanup, "execute_write_pg") as write, \
            pytest.raises(storage_cleanup.DataPurgeProtectedError):
        storage_cleanup.purge_replaced_transaction_rows(5, 1, [9], "test")
    write.assert_not_called()


def test_purge_preview_detects_corrected_content_with_unchanged_counts():
    preview = {"scope": {"client_id": 5, "template_id": 1}, "upload_ids": [4],
               "counts": {"client_data": 2}, "protected": {}, "source_fingerprint": "before"}
    original = storage_cleanup._purge_preview_hash(preview)
    preview["source_fingerprint"] = "after"
    assert storage_cleanup._purge_preview_hash(preview) != original


def test_replacement_purge_permanently_removes_exact_rows_and_old_payloads():
    from contextlib import nullcontext
    writes = []
    with patch.object(storage_cleanup, "pg_transaction", return_value=nullcontext()), \
            patch.object(storage_cleanup, "execute_query_pg", side_effect=[[{'status': 'verified', 'drive_file_id': 'remote', 'archive_sha256': 'checksum'}], [{"id": 9, "upload_id": 4, "derived_voucher_id": "V1"}], [{'table_name':None}], []]), \
            patch.object(storage_cleanup, "_count", return_value=0), \
            patch.object(storage_cleanup, "execute_write_pg", side_effect=lambda sql, params=None: writes.append((sql, params))):
        result = storage_cleanup.purge_replaced_transaction_rows(5, 1, [9], "test",
            archive_proof={'operation_id': 'operation', 'ids': [9], 'archive': {'drive_file_id': 'remote', 'sha256': 'checksum'}})
    assert result["deleted_count"] == 1
    assert any("DELETE FROM public.client_data WHERE id = ANY(%s)" in sql and params == ([9],) for sql, params in writes)
    assert any("DELETE FROM public.data_audit_log" in sql for sql, _ in writes)
    assert not any("DELETE FROM public.client_data_history" in sql for sql, _ in writes)
    assert not any("is_deleted = TRUE" in sql for sql, _ in writes)


@pytest.mark.parametrize('legacy_table', [None, 'cashflow_ledger_rollup_read_models'])
def test_replacement_purge_cleans_legacy_copy_only_when_present(legacy_table):
    from contextlib import nullcontext
    writes = []
    replies = [[{'status':'verified','drive_file_id':'remote','archive_sha256':'checksum'}],
               [{'id':9,'upload_id':4,'derived_voucher_id':'V1'}],
               [{'table_name':legacy_table}], []]
    with patch.object(storage_cleanup,'pg_transaction',return_value=nullcontext()), \
            patch.object(storage_cleanup,'execute_query_pg',side_effect=replies), \
            patch.object(storage_cleanup,'_count',return_value=0), \
            patch.object(storage_cleanup,'execute_write_pg',side_effect=lambda sql,params=None:writes.append((sql,params))):
        result=storage_cleanup.purge_replaced_transaction_rows(5,1,[9],'test',
            archive_proof={'operation_id':'operation','ids':[9],'archive':{'drive_file_id':'remote','sha256':'checksum'}})
    assert result['deleted_count']==1
    legacy=[(sql,params) for sql,params in writes if 'DELETE FROM public.cashflow_ledger_rollup_read_models' in sql]
    assert bool(legacy)==bool(legacy_table)
    if legacy:
        assert legacy[0][1]==(5,1)
    assert any('DELETE FROM public.client_data WHERE id = ANY(%s)' in sql and params==([9],) for sql,params in writes)


if __name__ == "__main__":
    unittest.main()
