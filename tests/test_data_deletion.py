import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from routers import clients
from services import storage_cleanup


class PermanentDataDeletionTests(unittest.TestCase):
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
        self.assertLess(sql_text.index("client_data"), sql_text.index("uploaded_files"))

    def test_preview_route_is_admin_scoped_and_read_only(self):
        expected = {"mode": "permanent_data_delete_dry_run"}
        with patch.object(clients, "get_client_data_purge_preview", return_value=expected) as preview:
            result = clients.client_data_purge_preview(5, 1, {"role": "admin"})
        self.assertEqual(result, expected)
        preview.assert_called_once_with(5, 1)

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
            patch.object(storage_cleanup, "execute_query_pg", side_effect=[[{'status': 'verified', 'drive_file_id': 'remote', 'archive_sha256': 'checksum'}], [{"id": 9, "upload_id": 4, "derived_voucher_id": "V1"}], []]), \
            patch.object(storage_cleanup, "_count", return_value=0), \
            patch.object(storage_cleanup, "execute_write_pg", side_effect=lambda sql, params=None: writes.append((sql, params))):
        result = storage_cleanup.purge_replaced_transaction_rows(5, 1, [9], "test",
            archive_proof={'operation_id': 'operation', 'ids': [9], 'archive': {'drive_file_id': 'remote', 'sha256': 'checksum'}})
    assert result["deleted_count"] == 1
    assert any("DELETE FROM public.client_data WHERE id = ANY(%s)" in sql and params == ([9],) for sql, params in writes)
    assert any("DELETE FROM public.data_audit_log" in sql for sql, _ in writes)
    assert not any("is_deleted = TRUE" in sql for sql, _ in writes)


if __name__ == "__main__":
    unittest.main()
