import unittest
import os
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

import database
import audit_postgres_indexes
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

    def test_preview_route_reports_safe_evidence_unavailable_message(self):
        with patch.object(
            storage_cleanup,
            "get_storage_cleanup_preview",
            side_effect=ConnectionError("database connection detail must not be exposed"),
        ):
            with self.assertRaises(HTTPException) as error:
                storage_cleanup.storage_cleanup_preview({"role": "admin"})

        self.assertEqual(error.exception.status_code, 503)
        self.assertIn("database evidence could not be read", error.exception.detail)
        self.assertNotIn("database connection detail", error.exception.detail)

    def test_preview_route_logs_failure_without_changing_safe_response(self):
        with patch.object(
            storage_cleanup,
            "get_storage_cleanup_preview",
            side_effect=ConnectionError("database connection detail must not be exposed"),
        ), patch.object(storage_cleanup.logger, "exception") as log:
            with self.assertRaises(HTTPException) as error:
                storage_cleanup.storage_cleanup_preview({"role": "admin"})

        log.assert_called_once()
        self.assertEqual(error.exception.status_code, 503)
        self.assertNotIn("database connection detail", error.exception.detail)

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

    def test_apply_route_forwards_to_cleanup_service_after_admin_guard(self):
        payload = storage_cleanup.StorageCleanupApplyRequest(
            preview_hash='fresh-hash',
            confirmation=cleanup_service.CONFIRMATION_TEXT,
        )
        expected = {"deleted_counts": {}, "preview": {"mode": "dry_run"}}
        with patch.object(storage_cleanup, 'apply_storage_cleanup', return_value=expected) as apply:
            result = storage_cleanup.storage_cleanup_apply(payload, {'username': 'admin', 'role': 'admin'})
        self.assertEqual(result, expected)
        apply.assert_called_once_with(
            preview_hash='fresh-hash',
            operator='admin',
            confirmation=cleanup_service.CONFIRMATION_TEXT,
        )

    def test_validation_plan_cleanup_requires_exact_confirmation_before_preview_or_write(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(cleanup_service, "get_storage_cleanup_preview") as preview, \
                patch.object(cleanup_service, "pg_transaction") as transaction:
            with self.assertRaises(cleanup_service.CleanupConfirmationError):
                cleanup_service.apply_validation_plan_cleanup("fresh-hash", "admin", "DELETE")
        preview.assert_not_called()
        transaction.assert_not_called()

    def test_validation_plan_cleanup_rejects_stale_preview_before_transaction(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(cleanup_service, "get_storage_cleanup_preview", return_value={"preview_hash": "new-hash"}), \
                patch.object(cleanup_service, "pg_transaction") as transaction:
            with self.assertRaises(cleanup_service.CleanupPreviewStaleError):
                cleanup_service.apply_validation_plan_cleanup(
                    "old-hash",
                    "admin",
                    cleanup_service.VALIDATION_PLAN_CLEANUP_CONFIRMATION,
                )
        transaction.assert_not_called()

    def test_validation_plan_cleanup_only_deletes_plans_and_preserves_protected_data(self):
        protected = {
            "client_data_current": 64787,
            "client_data_retained_for_verification": 0,
            "verification_details": 0,
            "trial_balance_accounts": 1008,
            "trial_balance_uploads": 1,
            "cashflow_current_prepared_lines": 0,
            "cashflow_current_prepared_rollups": 0,
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
                patch.object(cleanup_service, "_delete_count", side_effect=lambda query: delete_calls.append(query) or 1), \
                patch.object(cleanup_service, "_protected_counts", return_value=protected):
            result = cleanup_service.apply_validation_plan_cleanup(
                "fresh-hash",
                "admin",
                cleanup_service.VALIDATION_PLAN_CLEANUP_CONFIRMATION,
            )

        self.assertEqual(result["mode"], "validation_plans_only")
        self.assertEqual(result["deleted_counts"], {"upload_validation_plans": 1})
        self.assertEqual(len(delete_calls), 1)
        self.assertIn("DELETE FROM public.upload_validation_plans", delete_calls[0])
        self.assertNotIn("data_audit_log", delete_calls[0])
        self.assertNotIn("client_data", delete_calls[0])

    def test_validation_plan_cleanup_route_forwards_to_service(self):
        payload = storage_cleanup.ValidationPlanCleanupRequest(
            preview_hash="fresh-hash",
            confirmation=cleanup_service.VALIDATION_PLAN_CLEANUP_CONFIRMATION,
        )
        expected = {"mode": "validation_plans_only", "deleted_counts": {"upload_validation_plans": 1}}
        with patch.object(storage_cleanup, "apply_validation_plan_cleanup", return_value=expected) as apply:
            result = storage_cleanup.storage_validation_plan_cleanup(
                payload,
                {"username": "admin", "role": "admin"},
            )

        self.assertEqual(result, expected)
        apply.assert_called_once_with(
            preview_hash="fresh-hash",
            operator="admin",
            confirmation=cleanup_service.VALIDATION_PLAN_CLEANUP_CONFIRMATION,
        )

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

    def test_table_sizes_classify_validation_plans_as_temporary(self):
        with patch.object(cleanup_service, "execute_query_pg", return_value=[
            {"table_name": "upload_validation_plans", "table_bytes": 100, "index_bytes": 20, "total_bytes": 120},
        ]):
            result = cleanup_service._table_sizes()

        self.assertEqual(result[0]["classification"], "temporary validation data")

    def test_index_review_marks_required_and_zero_scan_indexes_without_dropping(self):
        with patch.object(cleanup_service, "execute_query_pg", return_value=[
            {
                "index_name": "client_data_pkey",
                "index_bytes": 100,
                "idx_scan": 3,
                "idx_tup_read": 10,
                "idx_tup_fetch": 9,
                "is_unique": True,
                "is_primary": True,
                "key_columns": ["id"],
            },
            {
                "index_name": "idx_client_data_active_identity_unique",
                "index_bytes": 200,
                "idx_scan": 0,
                "idx_tup_read": 0,
                "idx_tup_fetch": 0,
                "is_unique": True,
                "is_primary": False,
                "key_columns": ["client_id", "template_id", "source_identity"],
            },
            {
                "index_name": "idx_client_data_active_derived_date",
                "index_bytes": 50,
                "idx_scan": 0,
                "idx_tup_read": 0,
                "idx_tup_fetch": 0,
                "is_unique": False,
                "is_primary": False,
                "key_columns": ["client_id", "is_deleted", "derived_transaction_date"],
            },
            {
                "index_name": "idx_client_data_client_deleted",
                "index_bytes": 60,
                "idx_scan": 2,
                "idx_tup_read": 20,
                "idx_tup_fetch": 18,
                "is_unique": False,
                "is_primary": False,
                "key_columns": ["client_id", "is_deleted"],
            },
        ]) as query_mock:
            result = cleanup_service._client_data_index_review()

        query = query_mock.call_args_list[0].args[0]
        self.assertIn("pg_relation_size(stats.indexrelid)", query)
        self.assertNotIn("pg_relation_size(indexrelid)", query)

        self.assertEqual(result["missing_required_indexes"], [])
        self.assertEqual(result["index_bytes_total"], 410)
        self.assertEqual(
            result["zero_scan_indexes"],
            ["idx_client_data_active_identity_unique", "idx_client_data_active_derived_date"],
        )
        self.assertEqual(result["zero_scan_non_protected_indexes"], ["idx_client_data_active_derived_date"])
        self.assertEqual(
            result["protected_indexes"],
            ["client_data_pkey", "idx_client_data_active_identity_unique"],
        )
        self.assertEqual(
            result["possible_overlap_pairs"],
            [{
                "indexes": ["idx_client_data_active_derived_date", "idx_client_data_client_deleted"],
                "reason": "same leading key columns",
                "protected_indexes": [],
            }],
        )
        self.assertIn("never dropped automatically", result["policy"])

    def test_planner_evidence_failure_is_optional_and_does_not_expose_database_error(self):
        with patch.object(
            cleanup_service,
            "execute_query_pg",
            side_effect=RuntimeError("database detail must not reach the response"),
        ), patch.object(cleanup_service.logger, "exception") as log:
            result = cleanup_service._client_data_query_plan_evidence()

        self.assertFalse(result["available"])
        self.assertIn("maintenance actions remain blocked", result["reason"])
        self.assertNotIn("database detail", result["reason"])
        log.assert_called_once()

    def test_bloat_evidence_failure_is_optional_and_blocks_reindex(self):
        with patch.object(
            cleanup_service,
            "execute_query_pg",
            side_effect=RuntimeError("database detail must not reach the response"),
        ), patch.object(cleanup_service.logger, "exception") as log:
            result = cleanup_service._client_data_index_bloat_evidence()

        self.assertFalse(result["available"])
        self.assertIn("reindexing remains blocked", result["reason"])
        self.assertNotIn("database detail", result["reason"])
        log.assert_called_once()

    def test_index_review_requires_planner_evidence_before_manual_candidate(self):
        rows = [{
            "index_name": "idx_client_data_active_derived_date",
            "index_bytes": 50,
            "idx_scan": 0,
            "idx_tup_read": 0,
            "idx_tup_fetch": 0,
            "is_unique": False,
            "is_primary": False,
            "key_columns": ["client_id", "is_deleted", "derived_transaction_date"],
            "index_definition": "CREATE INDEX idx_client_data_active_derived_date ...",
        }]
        evidence = {
            "available": True,
            "plans": [],
            "index_names": [],
        }
        with patch.object(cleanup_service, "execute_query_pg", return_value=rows), \
                patch.object(cleanup_service, "_count", return_value=0), \
                patch.object(cleanup_service, "_client_data_query_plan_evidence", return_value=evidence):
            result = cleanup_service._client_data_index_review(include_query_plans=True)

        candidate = result["maintenance_candidates"]["idx_client_data_active_derived_date"]
        self.assertTrue(candidate["eligible_for_manual_approval"])
        self.assertEqual(candidate["reason"], "zero scans and no representative plan use")

    def test_index_review_blocks_candidate_used_by_representative_plan(self):
        rows = [{
            "index_name": "idx_client_data_active_derived_date",
            "index_bytes": 50,
            "idx_scan": 0,
            "idx_tup_read": 0,
            "idx_tup_fetch": 0,
            "is_unique": False,
            "is_primary": False,
            "key_columns": ["client_id", "is_deleted", "derived_transaction_date"],
        }]
        evidence = {"available": True, "plans": [], "index_names": ["idx_client_data_active_derived_date"]}
        with patch.object(cleanup_service, "execute_query_pg", return_value=rows), \
                patch.object(cleanup_service, "_count", return_value=0), \
                patch.object(cleanup_service, "_client_data_query_plan_evidence", return_value=evidence):
            result = cleanup_service._client_data_index_review(include_query_plans=True)

        candidate = result["maintenance_candidates"]["idx_client_data_active_derived_date"]
        self.assertFalse(candidate["eligible_for_manual_approval"])
        self.assertEqual(candidate["reason"], "a representative query plan uses this index")

    def test_index_review_allows_reindex_only_when_measured_bloat_exceeds_threshold(self):
        rows = [{
            "index_name": "idx_client_data_active_derived_date",
            "index_bytes": 50,
            "idx_scan": 4,
            "idx_tup_read": 20,
            "idx_tup_fetch": 20,
            "is_unique": False,
            "is_primary": False,
            "key_columns": ["client_id", "is_deleted", "derived_transaction_date"],
        }]
        evidence = {
            "available": True,
            "indexes": {
                "idx_client_data_active_derived_date": {
                    "recommended_action": "reindex",
                    "avg_leaf_density": 55.0,
                    "leaf_fragmentation": 42.0,
                }
            },
        }
        with patch.object(cleanup_service, "execute_query_pg", return_value=rows), \
                patch.object(cleanup_service, "_count", return_value=0), \
                patch.object(cleanup_service, "_client_data_query_plan_evidence", return_value={"available": True, "index_names": []}), \
                patch.object(cleanup_service, "_client_data_index_bloat_evidence", return_value=evidence):
            result = cleanup_service._client_data_index_review(
                include_query_plans=True,
                include_bloat_evidence=True,
            )

        candidate = result["maintenance_candidates"]["idx_client_data_active_derived_date"]
        self.assertTrue(candidate["reindex_eligible_for_manual_approval"])
        self.assertIn("reindex", candidate["optimization_actions"])

    def test_index_maintenance_reindex_requires_measured_bloat_evidence(self):
        before = {
            "preview_hash": "fresh-hash",
            "index_review": {
                "maintenance_candidates": {
                    "idx_client_data_active_derived_date": {
                        "reindex_eligible_for_manual_approval": True,
                        "reason": "index has observed scans",
                        "table_name": "client_data",
                        "index_definition": "CREATE INDEX idx_client_data_active_derived_date ON client_data ...",
                        "bloat_evidence": {"recommended_action": "reindex"},
                    }
                }
            },
        }
        after = {"preview_hash": "after-hash", "index_review": {}}
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(cleanup_service, "get_storage_cleanup_preview", side_effect=[before, after]), \
                patch.object(cleanup_service, "reindex_postgres_index_concurrently", return_value="idx_client_data_active_derived_date") as reindex, \
                patch.object(cleanup_service, "drop_postgres_index_concurrently") as drop:
            result = cleanup_service.apply_index_maintenance(
                "fresh-hash",
                "idx_client_data_active_derived_date",
                "admin",
                cleanup_service.INDEX_MAINTENANCE_CONFIRMATION_TEXT,
                operation="reindex",
            )

        reindex.assert_called_once_with("idx_client_data_active_derived_date")
        drop.assert_not_called()
        self.assertEqual(result["operation"], "reindex")

    def test_index_maintenance_requires_exact_confirmation_before_preview_or_drop(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(cleanup_service, "get_storage_cleanup_preview") as preview, \
                patch.object(cleanup_service, "drop_postgres_index_concurrently") as drop:
            with self.assertRaises(cleanup_service.CleanupConfirmationError):
                cleanup_service.apply_index_maintenance(
                    "fresh-hash",
                    "idx_client_data_active_derived_date",
                    "admin",
                    "REMOVE",
                )

        preview.assert_not_called()
        drop.assert_not_called()

    def test_index_maintenance_rejects_stale_preview_before_drop(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(cleanup_service, "get_storage_cleanup_preview", return_value={"preview_hash": "new-hash"}), \
                patch.object(cleanup_service, "drop_postgres_index_concurrently") as drop:
            with self.assertRaises(cleanup_service.CleanupPreviewStaleError):
                cleanup_service.apply_index_maintenance(
                    "old-hash",
                    "idx_client_data_active_derived_date",
                    "admin",
                    cleanup_service.INDEX_MAINTENANCE_CONFIRMATION_TEXT,
                )

        drop.assert_not_called()

    def test_index_maintenance_blocks_non_candidate_without_drop(self):
        preview = {
            "preview_hash": "fresh-hash",
            "index_review": {
                "maintenance_candidates": {
                    "idx_client_data_active_derived_date": {
                        "eligible_for_manual_approval": False,
                        "reason": "a representative query plan uses this index",
                    }
                }
            },
        }
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(cleanup_service, "get_storage_cleanup_preview", return_value=preview), \
                patch.object(cleanup_service, "drop_postgres_index_concurrently") as drop:
            with self.assertRaises(cleanup_service.IndexMaintenanceBlockedError):
                cleanup_service.apply_index_maintenance(
                    "fresh-hash",
                    "idx_client_data_active_derived_date",
                    "admin",
                    cleanup_service.INDEX_MAINTENANCE_CONFIRMATION_TEXT,
                )

        drop.assert_not_called()

    def test_index_maintenance_drops_only_evidence_approved_allow_listed_index(self):
        before = {
            "preview_hash": "fresh-hash",
            "index_review": {
                "maintenance_candidates": {
                    "idx_client_data_active_derived_date": {
                        "eligible_for_manual_approval": True,
                        "reason": "zero scans and no representative plan use",
                        "table_name": "client_data",
                        "index_definition": "CREATE INDEX idx_client_data_active_derived_date ON client_data ...",
                    }
                }
            },
        }
        after = {"preview_hash": "after-hash", "index_review": {}}
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(cleanup_service, "get_storage_cleanup_preview", side_effect=[before, after]), \
                patch.object(cleanup_service, "drop_postgres_index_concurrently", return_value="idx_client_data_active_derived_date") as drop:
            result = cleanup_service.apply_index_maintenance(
                "fresh-hash",
                "idx_client_data_active_derived_date",
                "admin",
                cleanup_service.INDEX_MAINTENANCE_CONFIRMATION_TEXT,
            )

        drop.assert_called_once_with("idx_client_data_active_derived_date")
        self.assertEqual(result["mode"], "index_maintenance_applied")
        self.assertEqual(result["index_definition_before"], before["index_review"]["maintenance_candidates"]["idx_client_data_active_derived_date"]["index_definition"])
        self.assertIn("No client_data rows", " ".join(result["safety"]))

    def test_index_maintenance_route_forwards_to_service(self):
        payload = storage_cleanup.StorageIndexMaintenanceRequest(
            preview_hash="fresh-hash",
            index_name="idx_client_data_active_derived_date",
            confirmation=cleanup_service.INDEX_MAINTENANCE_CONFIRMATION_TEXT,
        )
        expected = {"mode": "index_maintenance_applied"}
        with patch.object(storage_cleanup, "apply_index_maintenance", return_value=expected) as apply:
            result = storage_cleanup.storage_cleanup_index_maintenance(
                payload,
                {"username": "admin", "role": "admin"},
            )

        self.assertEqual(result, expected)
        apply.assert_called_once_with(
            preview_hash="fresh-hash",
            index_name="idx_client_data_active_derived_date",
            operator="admin",
            confirmation=cleanup_service.INDEX_MAINTENANCE_CONFIRMATION_TEXT,
            operation="drop",
        )

    def test_standalone_index_review_is_read_only_and_reports_overlap_blockers(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [
                {"indexname": "client_data_pkey"},
                {"indexname": "idx_client_data_active_identity_unique"},
            ],
            [
                {
                    "index_name": "client_data_pkey",
                    "index_bytes": 100,
                    "idx_scan": 3,
                    "idx_tup_read": 10,
                    "idx_tup_fetch": 9,
                    "is_unique": True,
                    "is_primary": True,
                    "key_columns": ["id"],
                    "predicate": None,
                    "index_definition": "CREATE UNIQUE INDEX client_data_pkey ...",
                },
                {
                    "index_name": "idx_client_data_active_identity_unique",
                    "index_bytes": 200,
                    "idx_scan": 0,
                    "idx_tup_read": 0,
                    "idx_tup_fetch": 0,
                    "is_unique": True,
                    "is_primary": False,
                    "key_columns": ["client_id", "template_id", "source_identity"],
                    "predicate": "is_deleted = false",
                    "index_definition": "CREATE UNIQUE INDEX ...",
                },
                {
                    "index_name": "idx_client_data_active_derived_date",
                    "index_bytes": 50,
                    "idx_scan": 0,
                    "idx_tup_read": 0,
                    "idx_tup_fetch": 0,
                    "is_unique": False,
                    "is_primary": False,
                    "key_columns": ["client_id", "is_deleted", "derived_transaction_date"],
                    "predicate": None,
                    "index_definition": "CREATE INDEX ...",
                },
                {
                    "index_name": "idx_client_data_client_deleted",
                    "index_bytes": 60,
                    "idx_scan": 2,
                    "idx_tup_read": 20,
                    "idx_tup_fetch": 18,
                    "is_unique": False,
                    "is_primary": False,
                    "key_columns": ["client_id", "is_deleted"],
                    "predicate": None,
                    "index_definition": "CREATE INDEX ...",
                },
            ],
            [
                {"client_id": 4, "template_id": 2, "row_count": 3},
            ],
        ]

        result = audit_postgres_indexes.build_review(cursor)

        self.assertEqual(result["missing_required_indexes"], [])
        self.assertEqual(result["active_identity_duplicate_group_count"], 1)
        self.assertEqual(result["indexes"][0]["classification"], "protected")
        self.assertEqual(len(result["possible_overlap_pairs"]), 1)
        self.assertEqual(cursor.execute.call_count, 3)
        cursor.connection.commit.assert_not_called()

    def test_standalone_index_review_can_capture_planner_only_evidence(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [],
            [],
            [],
            [{"QUERY PLAN": [{"Plan": {"Index Name": "idx_client_data_active_derived_date"}}]}],
            [{"QUERY PLAN": [{"Plan": {"Node Type": "Seq Scan"}}]}],
            [{"QUERY PLAN": [{"Plan": {"Index Name": "idx_client_data_client_deleted"}}]}],
        ]

        result = audit_postgres_indexes.build_review(cursor, include_plans=True)

        self.assertTrue(result["plan_evidence"]["available"])
        self.assertEqual(
            result["plan_evidence"]["index_names"],
            ["idx_client_data_active_derived_date", "idx_client_data_client_deleted"],
        )
        self.assertEqual(cursor.execute.call_count, 6)
        cursor.connection.commit.assert_not_called()

    def test_standalone_index_review_can_capture_optional_bloat_evidence(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[], [], []]
        bloat = {
            "available": True,
            "extension": "pgstattuple",
            "indexes": {
                "idx_client_data_active_derived_date": {
                    "recommended_action": "reindex",
                }
            },
        }
        with patch.object(audit_postgres_indexes, "_client_data_index_bloat_evidence", return_value=bloat):
            result = audit_postgres_indexes.build_review(cursor, include_bloat=True)

        self.assertEqual(result["bloat_evidence"], bloat)
        self.assertEqual(cursor.execute.call_count, 3)
        cursor.connection.commit.assert_not_called()

    def test_compaction_requires_exact_confirmation_before_preview(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(cleanup_service, "get_storage_cleanup_preview") as preview, \
                patch.object(cleanup_service, "compact_postgres_tables") as compact:
            with self.assertRaises(cleanup_service.CleanupConfirmationError):
                cleanup_service.compact_storage("fresh-hash", "admin", "COMPACT")

        preview.assert_not_called()
        compact.assert_not_called()

    def test_compaction_rejects_stale_preview_before_vacuum(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}), \
                patch.object(cleanup_service, "get_storage_cleanup_preview", return_value={"preview_hash": "new-hash"}), \
                patch.object(cleanup_service, "compact_postgres_tables") as compact:
            with self.assertRaises(cleanup_service.CleanupPreviewStaleError):
                cleanup_service.compact_storage(
                    "old-hash",
                    "admin",
                    cleanup_service.COMPACTION_CONFIRMATION_TEXT,
                )

        compact.assert_not_called()

    def test_database_compaction_is_allow_listed_and_outside_transactions(self):
        with patch.object(database, "get_raw_conn_pg") as get_conn:
            with self.assertRaises(ValueError):
                database.compact_postgres_tables(["uploaded_files"])
        get_conn.assert_not_called()

        cursor = MagicMock()
        connection = MagicMock()
        connection.autocommit = False
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.cursor.return_value.__exit__.return_value = False
        cursor.execute.side_effect = lambda sql: self.assertTrue(connection.autocommit)
        with patch.object(database, "get_raw_conn_pg", return_value=connection), \
                patch.object(database, "release_raw_conn_pg") as release:
            result = database.compact_postgres_tables(["data_audit_log"], full=False)

        self.assertEqual(result, ["data_audit_log"])
        cursor.execute.assert_called_once_with("VACUUM (ANALYZE) public.data_audit_log")
        release.assert_called_once_with(connection)

    def test_database_index_drop_is_allow_listed_and_concurrent(self):
        with patch.object(database, "get_raw_conn_pg") as get_conn:
            with self.assertRaises(ValueError):
                database.drop_postgres_index_concurrently("client_data_pkey")
        get_conn.assert_not_called()

        cursor = MagicMock()
        connection = MagicMock()
        connection.autocommit = False
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.cursor.return_value.__exit__.return_value = False
        with patch.object(database, "get_raw_conn_pg", return_value=connection), \
                patch.object(database, "release_raw_conn_pg") as release:
            result = database.drop_postgres_index_concurrently(
                "idx_client_data_active_derived_date"
            )

        self.assertEqual(result, "idx_client_data_active_derived_date")
        connection.commit.assert_called_once_with()
        cursor.execute.assert_called_once_with(
            'DROP INDEX CONCURRENTLY IF EXISTS public."idx_client_data_active_derived_date"'
        )
        release.assert_called_once_with(connection)

    def test_database_index_reindex_is_allow_listed_and_concurrent(self):
        with patch.object(database, "get_raw_conn_pg") as get_conn:
            with self.assertRaises(ValueError):
                database.reindex_postgres_index_concurrently("client_data_pkey")
        get_conn.assert_not_called()

        cursor = MagicMock()
        connection = MagicMock()
        connection.autocommit = False
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.cursor.return_value.__exit__.return_value = False
        cursor.execute.side_effect = lambda sql: self.assertTrue(connection.autocommit)
        with patch.object(database, "get_raw_conn_pg", return_value=connection), \
                patch.object(database, "release_raw_conn_pg") as release:
            result = database.reindex_postgres_index_concurrently(
                "idx_client_data_active_derived_date"
            )

        self.assertEqual(result, "idx_client_data_active_derived_date")
        connection.commit.assert_called_once_with()
        cursor.execute.assert_called_once_with(
            'REINDEX INDEX CONCURRENTLY public."idx_client_data_active_derived_date"'
        )
        release.assert_called_once_with(connection)

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
        self.assertTrue(all("active_preparation_revision" not in query for query in prepared_sql))
        self.assertIn("cash_bank_transaction_classifications", retired_sql)
        self.assertIn("cashflow_prepared_lines", retired_sql)
        self.assertTrue(any("upload_validation_plans" in query for query in delete_calls))

    def test_compaction_route_forwards_full_flag_after_admin_guard(self):
        payload = storage_cleanup.StorageCompactionRequest(
            preview_hash="fresh-hash",
            confirmation=cleanup_service.FULL_COMPACTION_CONFIRMATION_TEXT,
            full=True,
        )
        expected = {"mode": "compacted", "full": True}
        with patch.object(storage_cleanup, "compact_storage", return_value=expected) as compact:
            result = storage_cleanup.storage_cleanup_compact(
                payload,
                {"username": "admin", "role": "admin"},
            )

        self.assertEqual(result, expected)
        compact.assert_called_once_with(
            preview_hash="fresh-hash",
            operator="admin",
            confirmation=cleanup_service.FULL_COMPACTION_CONFIRMATION_TEXT,
            full=True,
        )

    def test_existing_postgres_schema_path_ensures_cleanup_run_ledger(self):
        marker_cursor = MagicMock()
        marker_cursor.fetchone.return_value = ("audit_rules_config_state",)
        cleanup_cursor = MagicMock()
        cleanup_cursor.__enter__.return_value = cleanup_cursor
        cleanup_cursor.__exit__.return_value = False
        connection = MagicMock()
        connection.cursor.side_effect = [marker_cursor] + [cleanup_cursor] * 20

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

        executed_sql = [call.args[0] for call in cleanup_cursor.execute.call_args_list]
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS storage_cleanup_runs" in sql for sql in executed_sql))


if __name__ == "__main__":
    unittest.main()
