import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch

import database
from database import init_db


class AppSmokeTests(unittest.TestCase):
    """Confirms the FastAPI app boots and its routers actually respond, end-to-end over real HTTP
    request/response handling (not just calling a function directly, like the other tests here) —
    against a throwaway temp database (never the real test.db), same isolation pattern as
    test_verification_indexes.py / test_upload_validation_counts.py."""

    def setUp(self):
        pool_guard = patch.object(database, "get_pg_pool", side_effect=AssertionError(
            "Smoke tests must not access live PostgreSQL"))
        pool_guard.start()
        self.addCleanup(pool_guard.stop)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_db_name = database.DB_NAME
        database.DB_NAME = os.path.join(self.tmpdir.name, "test.db")
        init_db()
        # Imported here (not at module scope) so database.DB_NAME is already patched before
        # main.py's app is built and its startup event (which also calls init_db()) runs.
        from fastapi.testclient import TestClient
        import main
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        database.DB_NAME = self.original_db_name
        self.tmpdir.cleanup()

    @contextmanager
    def _persisted_status_fixture(self, profile):
        """Supply the stored snapshot boundary while running the real status gates."""
        from routers import verification

        ruleset = deepcopy(profile)
        for category in ruleset["categories"]:
            category["matrix_version"] = 1
        ruleset["applied_ruleset_hash"] = "fixture-status-ruleset-v1"
        with patch.object(verification, "get_audit_rules_config_version", return_value=1), \
             patch.object(verification, "load_persisted_category_status_ruleset", return_value=ruleset) as loader:
            yield ruleset
        loader.assert_called_with(1, 3, profile, expected_version=1)

    def test_dashboard_page_serves(self):
        response = self.client.get("/static/index.html")
        self.assertEqual(response.status_code, 200)

    def test_static_html_revalidates_without_changing_asset_cache_behavior(self):
        html_response = self.client.get("/static/reconciliation.html")
        self.assertEqual(html_response.status_code, 200)
        self.assertEqual(html_response.headers.get("cache-control"), "no-cache, must-revalidate")
        self.assertEqual(html_response.headers.get("pragma"), "no-cache")
        self.assertEqual(html_response.headers.get("expires"), "0")

        javascript_response = self.client.get("/static/reconciliation.js?v=12")
        self.assertEqual(javascript_response.status_code, 200)
        self.assertNotEqual(javascript_response.headers.get("cache-control"), "no-cache, must-revalidate")

    def test_clients_api_responds_on_fresh_db(self):
        # The clients router is Postgres-backed in production. Keep this smoke test
        # hermetic: the HTTP/router wiring is real, while the external PG read is stubbed.
        with patch("routers.clients.execute_query_pg", return_value=[]):
            response = self.client.get("/api/clients")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_dashboard_db_size_uses_postgres_when_database_url_set(self):
        # The dashboard serves production data from Postgres; keep this smoke test
        # hermetic while proving the route reads the real database size via
        # pg_database_size() and converts it to MB, rather than SQLite's file size.
        with patch("routers.dashboard.execute_query_pg", return_value=[{"size_bytes": 2 * 1024 * 1024}]) as pg_query, \
             patch.dict(os.environ, {"DATABASE_URL": "postgres://example"}):
            response = self.client.get("/api/dashboard/db-size")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"total_bytes": 2 * 1024 * 1024, "formatted": "2.0 MB"})
        pg_query.assert_called_once()

    def test_dashboard_db_size_falls_back_to_sqlite_file_when_no_database_url(self):
        # Local dev has no DATABASE_URL - the route should read the SQLite file's own
        # size on disk (the temp database this test suite already isolates onto).
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            response = self.client.get("/api/dashboard/db-size")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        expected_mb = round(os.path.getsize(database.DB_NAME) / (1024 * 1024), 1)
        self.assertEqual(body["formatted"], f"{expected_mb:.1f} MB")

    def test_two_users_keep_independent_presence_until_leave_or_expiry(self):
        from fastapi import HTTPException
        from routers import audit_rules, verification

        # Execute the production presence SQL against disposable tables. Only the
        # driver placeholders and interval syntax differ between SQLite and PG.
        with sqlite3.connect(":memory:") as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript("""
                CREATE TABLE verification_sessions (id INTEGER PRIMARY KEY, status TEXT);
                INSERT INTO verification_sessions VALUES (10, 'In Progress'), (20, 'In Progress'), (30, 'Cancelled');
                CREATE TABLE verification_presence (
                    verification_session_id INTEGER, session_token TEXT, user_id INTEGER,
                    last_heartbeat TEXT, ended_at TEXT,
                    UNIQUE (verification_session_id, session_token));
            """)

            def execute(sql, params=None):
                sql = sql.replace("%s", "?").replace(
                    "CURRENT_TIMESTAMP - INTERVAL '90 seconds'", "datetime('now', '-90 seconds')")
                cur = conn.execute(sql, params or ())
                return [dict(row) for row in cur.fetchall()] if cur.description else cur.rowcount

            first = {"token": "fixture-one", "user_id": 1}
            second = {"token": "fixture-two", "user_id": 2}
            with patch.object(verification, "execute_write_pg", side_effect=execute), \
                 patch.object(audit_rules, "execute_query_pg", side_effect=execute):
                verification.enter_verification_presence(10, first)
                verification.enter_verification_presence(20, second)
                with self.assertRaises(HTTPException) as blocked:
                    audit_rules.ensure_no_active_verification_sessions()
                self.assertEqual(blocked.exception.status_code, 409)
                verification.leave_verification_presence(10, first)
                with self.assertRaises(HTTPException):
                    audit_rules.ensure_no_active_verification_sessions()
                self.assertTrue(verification.heartbeat_verification_presence(20, second)["active"])
                conn.execute("UPDATE verification_presence SET last_heartbeat = datetime('now', '-100 seconds')")
                audit_rules.ensure_no_active_verification_sessions()
                verification.enter_verification_presence(10, first)
                with self.assertRaises(HTTPException):
                    audit_rules.ensure_no_active_verification_sessions()
                verification.leave_verification_presence(10, first)
                audit_rules.ensure_no_active_verification_sessions()
                for session_id in (30, 999):
                    with self.assertRaises(HTTPException) as missing:
                        verification.enter_verification_presence(session_id, first)
                    self.assertEqual(missing.exception.status_code, 404)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM verification_presence").fetchone()[0], 2)

    def test_status_gates_and_read_only_reasons_agree(self):
        from contextlib import ExitStack
        from routers import verification

        profile = {"categories": [{"category_id": 3, "documents": [{
            "document_id": 44, "name": "Fixture Form", "document_type": "form",
            "form_fields": [{"id": 4401}]}]}],
            "voucher_checks": [{"id": 1, "name": "Fixture check"}]}
        cases = [
            ("complete", {}, "Cleared"),
            ("missing Form", {"form": False}, "Query"),
            ("unchecked voucher", {"check": False}, "Query"),
            ("signature exception", {"signature": 1}, "Query"),
            ("blocking Cross-check", {"cross": "fail"}, "Query"),
            ("nonblocking shortfall", {"cross": "shortfall"}, "Cleared"),
            ("account-head note", {"note": True}, "Query"),
            ("voucher line not confirmed correct", {"correct": False}, "Query"),
        ]
        for name, changes, expected in cases:
            with self.subTest(name=name), ExitStack() as stack:
                state = {"form": True, "check": True, "signature": 0, "cross": "pass", "note": False, "correct": True, **changes}

                def query(sql, params=None):
                    if "SELECT vd.id, vd.session_id" in sql:
                        return [{"id": 7, "session_id": 10, "transaction_ref": "FIXTURE-1",
                                 "row_data": {"Account Code": "100"}, "extract_amount": 100,
                                 "client_id": 1, "category_id_applied": 3, "category_name_applied": "Fixture"}]
                    if "SELECT matrix_version" in sql:
                        return [{"matrix_version": 1}]
                    if "SELECT id, template_id" in sql:
                        return [{"id": 44, "template_id": 2}]
                    if "FROM voucher_line_form_entries" in sql:
                        # Form completion consumes typed values, not an entry marker.
                        return [{"voucher_line_id": 7, "document_id": 44,
                                 "bill_group_index": 0, "row_scope": "header", "sort_order": 0,
                                 "entry_updated_at": None, "field_id": 4401, "field_type": "text",
                                 "value_text": "Fixture evidence", "value_date": None,
                                 "value_numeric": None}] if state["form"] else []
                    if "FROM voucher_check_status" in sql:
                        return [{"check_id": 1}] if state["check"] else []
                    if "COUNT(*) as cnt" in sql:
                        return [{"cnt": state["signature"]}]
                    if "SELECT DISTINCT document_id FROM voucher_line_documents" in sql:
                        return []
                    raise AssertionError(f"Unexpected fixture read: {sql}")

                stack.enter_context(patch.object(verification, "execute_query_pg", side_effect=query))
                ruleset = stack.enter_context(self._persisted_status_fixture(profile))
                stack.enter_context(patch.object(verification, "get_document_auth_roles_batch", return_value={}))
                stack.enter_context(patch.object(verification, "evaluate_cross_checks_for_line", return_value={
                    "results": [{"status": state["cross"], "name": "Fixture rule", "message": "Fixture result"}]}))
                stack.enter_context(patch.object(verification, "_has_pending_account_head_note", return_value=state["note"]))
                stack.enter_context(patch.object(verification, "_has_voucher_line_correct_confirmation", return_value=state["correct"]))
                shortfalls = stack.enter_context(patch.object(verification, "_write_shortfall_observations", return_value=False))
                write = stack.enter_context(patch.object(verification, "execute_write_pg"))
                reason = verification.compute_and_persist_cleared_status(7, detail=True, persist=False, sibling_ids=[7], rulebook=profile)
                self.assertEqual(reason["cleared_status"], expected)
                write.assert_not_called()
                shortfalls.assert_not_called()
                saved = verification.compute_and_persist_cleared_status(7, sibling_ids=[7], rulebook=profile)
                self.assertEqual(saved["cleared_status"], expected)
                self.assertEqual(write.call_args.args[1], (expected, 1, ruleset["applied_ruleset_hash"], 7))

    def test_account_head_recommendation_marker_rolls_up_as_review_finding(self):
        from routers import verification

        matched = verification._format_account_head_observation(
            "__account_head_recommendation__: Office Supplies"
        )
        legacy = verification._format_account_head_observation("Create a new travel account")

        self.assertEqual(
            matched,
            "[Account Head Recommendation]: Review suggested account head: Office Supplies",
        )
        self.assertIn("no match in Trial Balance", legacy)

    def test_unclassified_line_is_not_written_as_cleared(self):
        from routers import verification
        with patch.object(verification, "execute_query_pg", return_value=[{
            "id": 7, "session_id": 10, "transaction_ref": "FIXTURE-1", "row_data": {},
            "extract_amount": 100, "client_id": 1,
            "category_id_applied": None, "category_name_applied": None}]), \
             patch.object(verification, "execute_write_pg") as write:
            result = verification.compute_and_persist_cleared_status(
                7, detail=True, sibling_ids=[7], rulebook={"categories": [], "voucher_checks": []})
        self.assertIsNone(result["cleared_status"])
        write.assert_not_called()

    def test_zero_field_form_is_non_blocking(self):
        from routers import verification

        profile = {"categories": [{"category_id": 3, "documents": [{
            "document_id": 44, "name": "Fixture Form", "document_type": "form",
            "form_fields": []}]}], "voucher_checks": [{"id": 1, "name": "Fixture check"}]}

        def query(sql, params=None):
            if "SELECT vd.id, vd.session_id" in sql:
                return [{"id": 7, "session_id": 10, "transaction_ref": "FIXTURE-1",
                         "row_data": {"Account Code": "100"}, "extract_amount": 100,
                         "client_id": 1, "category_id_applied": 3,
                         "category_name_applied": "Fixture"}]
            if "SELECT matrix_version" in sql:
                return [{"matrix_version": 1}]
            if "SELECT id, template_id" in sql:
                return [{"id": 44, "template_id": 2}]
            if "FROM voucher_line_form_entries" in sql:
                return []
            if "FROM voucher_check_status" in sql:
                return [{"check_id": 1}]
            if "COUNT(*) as cnt" in sql:
                return [{"cnt": 0}]
            if "SELECT DISTINCT document_id FROM voucher_line_documents" in sql:
                return []
            raise AssertionError(f"Unexpected fixture read: {sql}")

        with patch.object(verification, "execute_query_pg", side_effect=query), \
             self._persisted_status_fixture(profile), \
             patch.object(verification, "get_document_auth_roles_batch", return_value={}), \
             patch.object(verification, "evaluate_cross_checks_for_line", return_value={"results": []}), \
             patch.object(verification, "_has_voucher_line_correct_confirmation", return_value=True), \
             patch.object(verification, "_has_pending_account_head_note", return_value=False):
            result = verification.compute_and_persist_cleared_status(
                7, detail=True, persist=False, sibling_ids=[7], rulebook=profile
            )
        self.assertEqual(result["cleared_status"], "Cleared")

    def test_zero_field_form_still_requires_imported_procedure_checks(self):
        from routers import verification

        profile = {"categories": [{"category_id": 3, "documents": [{
            "document_id": 44, "name": "Fixture Form", "document_type": "form",
            "form_fields": [], "authority_roles": [{"role_name": "Approved by"}]}],
            "procedure_evidence": {"checks": [{
                "document_id": 44, "condition": "approved_by", "role_name": "Approved by"
            }]}}], "voucher_checks": [{"id": 1, "name": "Fixture check"}]}
        state = {"procedure": False}

        def query(sql, params=None):
            if "SELECT vd.id, vd.session_id" in sql:
                return [{"id": 7, "session_id": 10, "transaction_ref": "FIXTURE-1",
                         "row_data": {"Account Code": "100"}, "extract_amount": 100,
                         "client_id": 1, "category_id_applied": 3,
                         "category_name_applied": "Fixture"}]
            if "SELECT matrix_version" in sql:
                return [{"matrix_version": 1}]
            if "SELECT id, template_id" in sql:
                return [{"id": 44, "template_id": 2}]
            if "FROM voucher_line_form_entries" in sql:
                return []
            if "FROM voucher_line_procedure_checks" in sql:
                return [{"document_id": 44, "role_name": "Approved by", "is_checked": True,
                         "checked_at": None}] if state["procedure"] else []
            if "FROM voucher_check_status" in sql:
                return [{"check_id": 1}]
            if "COUNT(*) as cnt" in sql:
                return [{"cnt": 0}]
            if "SELECT DISTINCT document_id FROM voucher_line_documents" in sql:
                return []
            raise AssertionError(f"Unexpected fixture read: {sql}")

        with patch.object(verification, "execute_query_pg", side_effect=query), \
             self._persisted_status_fixture(profile), \
             patch.object(verification, "get_document_auth_roles_batch", return_value={}), \
             patch.object(verification, "evaluate_cross_checks_for_line", return_value={"results": []}), \
             patch.object(verification, "_has_voucher_line_correct_confirmation", return_value=True), \
             patch.object(verification, "_has_pending_account_head_note", return_value=False):
            result = verification.compute_and_persist_cleared_status(
                7, detail=True, persist=False, sibling_ids=[7], rulebook=profile
            )
            self.assertEqual(result["cleared_status"], "Query")
            state["procedure"] = True
            result = verification.compute_and_persist_cleared_status(
                7, detail=True, persist=False, sibling_ids=[7], rulebook=profile
            )
        self.assertEqual(result["cleared_status"], "Cleared")

    def test_missing_status_snapshot_blocks_without_writing_verification(self):
        from fastapi import HTTPException
        from routers import verification

        profile = {"categories": [{"category_id": 3}], "voucher_checks": []}
        with patch.object(verification, "execute_query_pg", return_value=[{
            "id": 7, "session_id": 10, "transaction_ref": "FIXTURE-1",
            "row_data": {"Account Code": "100"}, "extract_amount": 100,
            "client_id": 1, "category_id_applied": 3, "category_name_applied": "Fixture",
        }]), patch.object(verification, "get_audit_rules_config_version", return_value=1), \
             patch.object(verification, "load_persisted_category_status_ruleset", return_value=None), \
             patch.object(verification, "execute_write_pg") as write:
            with self.assertRaises(HTTPException) as blocked:
                verification.compute_and_persist_cleared_status(
                    7, detail=True, sibling_ids=[7], rulebook=profile)
        self.assertEqual(blocked.exception.status_code, 503)
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
