import unittest
from unittest.mock import patch
import os
import sqlite3
import tempfile
import importlib
from pathlib import Path

from playwright.sync_api import sync_playwright

import database
from database import init_db
from database import get_audit_rules_config_changes_since
from routers.audit_rules import (
    cache_get,
    cache_set,
    get_audit_rules_config_version,
    get_document_cross_check_fields,
    invalidate_audit_rules_cache,
    invalidate_category_for_account_cache,
    load_client_account_category_index,
    load_client_verification_rulebook,
    row_to_document_list,
)
from routers.audit_rules import compile_verification_rulebook
from routers.verification import (
    _build_working_paper_gates,
    get_verification_session_rulebook,
    rebase_verification_session_rulebook,
)


VERIFICATION_JS = Path(__file__).resolve().parents[1] / "static" / "verification.js"


class AuditRulebookCompilerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        database.DB_NAME = os.path.join(self.tmpdir.name, "test.db")
        init_db()
        invalidate_audit_rules_cache()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_invalidation_increments_version_and_expires_cached_value(self):
        starting_version = get_audit_rules_config_version()
        cache_set("rulebook:test", {"value": 1})
        self.assertEqual(cache_get("rulebook:test"), {"value": 1})

        invalidate_audit_rules_cache()

        self.assertEqual(get_audit_rules_config_version(), starting_version + 1)
        self.assertIsNone(cache_get("rulebook:test"))

    def test_invalidation_records_rulebook_change_details(self):
        starting_version = get_audit_rules_config_version()
        invalidate_audit_rules_cache({
            "entity_type": "Voucher-level Check",
            "entity_id": 9,
            "entity_name": "Test check",
            "action": "Deleted",
        })

        changes = get_audit_rules_config_changes_since(starting_version)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["config_version"], starting_version + 1)
        self.assertEqual(changes[0]["entity_type"], "Voucher-level Check")
        self.assertEqual(changes[0]["entity_name"], "Test check")
        self.assertEqual(changes[0]["action"], "Deleted")

    def test_session_schema_has_nullable_rulebook_version(self):
        conn = sqlite3.connect(database.DB_NAME)
        try:
            columns = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(verification_sessions)")}
        finally:
            conn.close()
        self.assertIn("rulebook_version", columns)
        self.assertEqual(columns["rulebook_version"], 0)

    def test_rulebook_config_version_survives_router_reload(self):
        first_version = get_audit_rules_config_version()
        invalidate_audit_rules_cache()
        expected_version = first_version + 1

        import routers.audit_rules as audit_rules_module
        reloaded = importlib.reload(audit_rules_module)

        self.assertEqual(reloaded.get_audit_rules_config_version(), expected_version)

    def test_session_rulebook_mismatch_includes_explicit_recovery(self):
        with patch(
            "routers.verification.execute_query_pg",
            return_value=[{"client_id": 7, "rulebook_version": 1}],
        ), patch("routers.verification.get_audit_rules_config_version", return_value=2), patch(
            "routers.verification.get_audit_rules_config_changes_since",
            return_value=[{"entity_type": "Voucher-level Check", "entity_name": "Test check", "action": "Deleted"}],
        ):
            with self.assertRaises(Exception) as raised:
                get_verification_session_rulebook(12)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "RULEBOOK_VERSION_MISMATCH")
        self.assertEqual(raised.exception.detail["changes"][0]["entity_name"], "Test check")
        self.assertTrue(raised.exception.detail["recovery"]["preserves_work"])

    def test_explicit_session_rebase_preserves_work_contract(self):
        with patch(
            "routers.verification.execute_query_pg",
            return_value=[{"client_id": 7, "rulebook_version": 1}],
        ), patch("routers.verification.get_audit_rules_config_version", return_value=2), patch(
            "routers.verification.execute_write_pg", return_value=12
        ) as write:
            result = rebase_verification_session_rulebook(12, {"user_id": 4})

        self.assertTrue(result["recovered"])
        self.assertEqual(result["rulebook_version"], 2)
        self.assertIn("rulebook_version = %s", write.call_args.args[0])

    def test_account_index_prefers_trial_balance_and_uses_assignment_fallback(self):
        def query(sql, params=()):
            if "FROM trial_balance_uploads" in sql:
                return [{"id": 50}]
            if "FROM trial_balance_accounts" in sql:
                return [{"account_code": "6000", "category_id": 2, "category_name": "TB category"}]
            if "FROM account_category_assignments" in sql:
                return [{"account_code": "7000", "category_id": 3, "category_name": "Fallback category"}]
            self.fail(f"Unhandled query: {sql}")

        with patch("routers.audit_rules.execute_query_pg", side_effect=query):
            result = load_client_account_category_index(7, ["6000", "7000", "8000"])

        self.assertEqual(result["6000"]["category_name"], "TB category")
        self.assertEqual(result["7000"]["category_name"], "Fallback category")
        self.assertIsNone(result["8000"]["category_id"])

    def test_working_paper_groups_preserve_detailed_gate_failures(self):
        detailed = [
            {"key": "voucher_checks", "label": "Voucher checks complete", "status": "pass", "detail": ""},
            {"key": "approval_authority", "label": "Approved-by authority on file", "status": "fail", "detail": "Missing specimen"},
            {"key": "documents", "label": "Required documents linked", "status": "pass", "detail": ""},
            {"key": "account_head", "label": "No pending Account Head note", "status": "pass", "detail": ""},
        ]
        grouped = _build_working_paper_gates(detailed)
        self.assertEqual([gate["key"] for gate in grouped], ["approval", "supporting_documents", "account_head_classification", "observation"])
        self.assertEqual(grouped[0]["status"], "fail")
        self.assertEqual(grouped[0]["checks"][0]["key"], "voucher_checks")
        self.assertEqual(grouped[1]["status"], "pass")

    def test_compiles_category_profile_and_client_specific_data(self):
        def query(sql, params=()):
            if "FROM audit_rule_categories" in sql:
                return [{"id": 1, "name": "Fuel", "kb_overview": "Fuel controls", "evidence_type": "billable", "matrix_version": 3}]
            if "FROM audit_rule_documents" in sql:
                return [{"id": 10, "name": "Invoice", "kb_note": "Keep invoice", "template_id": 5, "risk_rating": "High", "transaction_level_approval": True, "document_type": "linked_template"}]
            if "FROM audit_rule_document_auth_roles" in sql:
                return [{"document_id": 10, "role_name": "Approved by", "required_authority": "Manager", "sort_order": 0}]
            if "SELECT id, column_name FROM master_columns" in sql:
                return [
                    {"id": 20, "column_name": "Amount"},
                    {"id": 21, "column_name": "Vendor"},
                ]
            if "FROM audit_rule_category_documents" in sql:
                return [{"category_id": 1, "document_id": 10, "is_required": True, "sort_order": 0}]
            if "FROM audit_rule_cross_checks" in sql:
                return [{"id": 30, "name": "Invoice to order", "compare_field": "Amount", "comparison_type": "Exact", "compare_to_voucher_line": False}]
            if "FROM audit_rule_cross_check_documents" in sql:
                return [{"cross_check_id": 30, "document_id": 10, "sort_order": 0}]
            if "FROM audit_rule_category_cross_checks" in sql:
                return [{"category_id": 1, "cross_check_id": 30, "sort_order": 0}]
            if "FROM audit_rule_voucher_checks" in sql:
                return [{"id": 40, "name": "Review", "authority": "Manager", "sort_order": 0}]
            if "FROM templates" in sql:
                return [{"id": 5, "name": "Invoices"}]
            if "FROM template_column_mappings" in sql:
                return [{"template_id": 5, "column_id": 20, "column_name": "Amount", "data_type": "number", "sort_order": 0}]
            if "FROM account_category_assignments" in sql:
                return [{"account_code": "6000", "category_id": 1, "category_name": "Fuel"}]
            if "FROM trial_balance_uploads" in sql:
                return []
            if "FROM signature_specimens" in sql:
                return [{"document_id": 10, "role_name": "Approved by", "expected_signatory_name": "A. Manager", "authority": "Manager"}]
            self.fail(f"Unhandled query: {sql}")

        with patch("routers.audit_rules.execute_query_pg", side_effect=query):
            rulebook = compile_verification_rulebook(7)

        self.assertEqual(rulebook["client_id"], 7)
        self.assertEqual(rulebook["account_category_index"]["6000"]["category_name"], "Fuel")
        document = rulebook["categories"][0]["documents"][0]
        self.assertTrue(document["required"])
        self.assertEqual(document["fields"], [
            {"field_name": "Amount", "master_column_id": 20},
            {"field_name": "Vendor", "master_column_id": 21},
        ])
        self.assertEqual(document["signature_requirements"][0]["specimens"][0]["expected_signatory_name"], "A. Manager")
        self.assertEqual(rulebook["categories"][0]["cross_checks"][0]["document_ids"], [10])
        self.assertEqual(rulebook["templates"][0]["columns"], [{
            "column_id": 20,
            "column_name": "Amount",
            "data_type": "number",
            "sort_order": 0,
        }])
        self.assertEqual(rulebook["warnings"], [])

    def test_document_cross_check_fields_mirror_active_columns(self):
        def query(sql, params=()):
            if "SELECT document_type FROM audit_rule_documents" in sql:
                return [{"document_type": "linked_template"}]
            if "SELECT id, column_name FROM master_columns" in sql:
                return [
                    {"id": 20, "column_name": "Amount"},
                    {"id": 21, "column_name": "Vendor"},
                ]
            self.fail(f"Unhandled query: {sql}")

        with patch("routers.audit_rules.execute_query_pg", side_effect=query):
            self.assertEqual(
                get_document_cross_check_fields(10),
                ["Amount", "Vendor"],
            )

        self.assertEqual(
            row_to_document_list(
                {"id": 11, "name": "Physical Bill", "is_active": True, "document_type": "physical_bill"},
                ["Amount", "Vendor"],
            )["cross_check_fields"],
            [],
        )

    def test_session_guidance_normalizer_maps_cross_check_ids_to_names(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.set_content("<html><body></body></html>")
                page.add_script_tag(content="""
                    var API_BASE = '';
                    async function flushProcedureObservations() {}
                    async function flushAccountHeadNoMatchNote() {}
                """)
                page.add_script_tag(path=str(VERIFICATION_JS))
                result = page.evaluate("""() => normalizeSessionRulebookGuidance({
                    category_id: 1,
                    name: 'Fuel',
                    kb_overview: 'Fuel controls',
                    matrix_version: 3,
                    documents: [{
                        document_id: 10,
                        name: 'Invoice',
                        template_id: 5,
                        document_type: 'linked_template',
                        authority_roles: [{role_name: 'Approved by'}]
                    }],
                    cross_checks: [{
                        id: 30,
                        compare_field: 'Amount',
                        comparison_type: 'Exact',
                        document_ids: [10, 99]
                    }]
                })""")
                self.assertEqual(result["documents"][0]["id"], 10)
                self.assertEqual(result["documents"][0]["auth_roles"], ["Approved by"])
                self.assertEqual(result["cross_checks"][0]["document_ids"], [10, 99])
                self.assertEqual(result["cross_checks"][0]["documents"], ["Invoice", "Document 99"])
                self.assertEqual(result["cross_checks"][0]["compare_field"], "Amount")
                self.assertEqual(result["cross_checks"][0]["comparison_type"], "Exact")
            finally:
                browser.close()

    def test_warns_for_inactive_template_and_assignment_category(self):
        def query(sql, params=()):
            if "FROM audit_rule_categories" in sql:
                return [{"id": 1, "name": "Fuel", "kb_overview": None, "evidence_type": None, "matrix_version": 1}]
            if "FROM audit_rule_documents" in sql:
                return [{"id": 10, "name": "Invoice", "kb_note": None, "template_id": 99, "risk_rating": None, "transaction_level_approval": False, "document_type": "linked_template"}]
            if "FROM audit_rule_category_documents" in sql:
                return [{"category_id": 1, "document_id": 10, "is_required": True, "sort_order": 0}]
            if "FROM templates" in sql or "FROM template_column_mappings" in sql or "FROM signature_specimens" in sql:
                return []
            if "FROM trial_balance_uploads" in sql:
                return []
            if "FROM account_category_assignments" in sql:
                return [{"account_code": "7000", "category_id": 88}]
            if "FROM audit_rule_document_auth_roles" in sql or "FROM audit_rule_document_fields" in sql or "FROM master_columns" in sql or "FROM audit_rule_cross_checks" in sql or "FROM audit_rule_cross_check_documents" in sql or "FROM audit_rule_category_cross_checks" in sql or "FROM audit_rule_voucher_checks" in sql:
                return []
            self.fail(f"Unhandled query: {sql}")

        with patch("routers.audit_rules.execute_query_pg", side_effect=query):
            warnings = compile_verification_rulebook(7)["warnings"]

        codes = {warning["code"] for warning in warnings}
        self.assertIn("inactive_template", codes)
        self.assertIn("inactive_account_category", codes)

    def test_versioned_rulebook_loader_caches_and_invalidates_client_data(self):
        calls = []

        with patch("routers.audit_rules.compile_verification_rulebook", side_effect=lambda client_id: calls.append(client_id) or {"client_id": client_id}):
            first = load_client_verification_rulebook(7)
            second = load_client_verification_rulebook(7)
            self.assertIs(first, second)
            self.assertEqual(calls, [7])

            invalidate_category_for_account_cache(7)
            load_client_verification_rulebook(7)
            self.assertEqual(calls, [7, 7])

    def test_session_rulebook_returns_packet_for_matching_version(self):
        version = get_audit_rules_config_version()
        with patch("routers.verification.execute_query_pg", return_value=[{"client_id": 7, "rulebook_version": version}]), \
             patch("routers.verification.load_client_verification_rulebook", return_value={"global_version": version, "client_id": 7}) as loader:
            result = get_verification_session_rulebook(12)

        self.assertEqual(result["session_id"], 12)
        self.assertEqual(result["rulebook"]["client_id"], 7)
        loader.assert_called_once_with(7)

    def test_session_rulebook_rejects_version_mismatch(self):
        with patch("routers.verification.execute_query_pg", return_value=[{"client_id": 7, "rulebook_version": get_audit_rules_config_version() - 1}]), \
             patch("routers.verification.load_client_verification_rulebook") as loader:
            with self.assertRaises(Exception) as raised:
                get_verification_session_rulebook(12)

        self.assertEqual(raised.exception.status_code, 409)
        loader.assert_not_called()


class CrossCheckPerDocumentFieldSchemaTests(unittest.TestCase):
    """Phase 1 of 4: per-Document compare_field column on the cross-check junction table."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        database.DB_NAME = os.path.join(self.tmpdir.name, "test.db")
        init_db()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_junction_table_has_compare_field_column(self):
        conn = database.get_db()
        try:
            cols = [row[1] for row in conn.execute(
                "PRAGMA table_info(audit_rule_cross_check_documents)"
            ).fetchall()]
        finally:
            conn.close()
        self.assertIn("compare_field", cols)

    def test_backfill_populates_null_rows_from_parent_shared_field(self):
        conn = database.get_db()
        try:
            conn.execute(
                "INSERT INTO audit_rule_documents (name) VALUES ('Purchase Order')"
            )
            doc_id = conn.execute("SELECT id FROM audit_rule_documents").fetchone()[0]
            conn.execute(
                "INSERT INTO audit_rule_cross_checks (name, compare_field, comparison_type) "
                "VALUES ('Qty chain', 'Quantity', 'All equal')"
            )
            cc_id = conn.execute("SELECT id FROM audit_rule_cross_checks").fetchone()[0]
            # Junction row inserted the pre-Phase-1 way: no compare_field supplied -> NULL.
            conn.execute(
                "INSERT INTO audit_rule_cross_check_documents (cross_check_id, document_id, sort_order) "
                "VALUES (?, ?, 0)",
                (cc_id, doc_id),
            )
            conn.commit()
        finally:
            conn.close()

        # Re-running init_db() must backfill the NULL junction row from the parent's shared field.
        init_db()

        conn = database.get_db()
        try:
            value = conn.execute(
                "SELECT compare_field FROM audit_rule_cross_check_documents WHERE cross_check_id = ?",
                (cc_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(value, "Quantity")


class CrossCheckPerDocumentFieldBackendTests(unittest.TestCase):
    """Phase 2 of 4: validation / save / response logic for per-Document compare_field."""

    def _doc_row(self, sql, params):
        # Every validate_cross_check_payload() DB read in these tests is the document lookup.
        return [{"name": f"Doc {params[0]}", "document_type": "linked_template"}]

    def test_validate_accepts_per_document_field_override(self):
        from routers import audit_rules

        fields_by_doc = {10: ["Quantity"], 11: ["Quantity Received"]}
        with patch.object(audit_rules, "execute_query_pg", side_effect=self._doc_row), \
             patch.object(audit_rules, "get_document_cross_check_fields",
                          side_effect=lambda doc_id, dtype=None: fields_by_doc[doc_id]):
            # Shared field "Quantity" is invalid on Doc 11, but its own override rescues it.
            audit_rules.validate_cross_check_payload(
                "Quantity", "All equal", [10, 11], False, {11: "Quantity Received"}
            )

    def test_validate_rejects_when_effective_field_not_eligible(self):
        from routers import audit_rules

        fields_by_doc = {10: ["Quantity"], 11: ["Quantity Received"]}
        with patch.object(audit_rules, "execute_query_pg", side_effect=self._doc_row), \
             patch.object(audit_rules, "get_document_cross_check_fields",
                          side_effect=lambda doc_id, dtype=None: fields_by_doc[doc_id]):
            with self.assertRaises(Exception) as raised:
                # No override for Doc 11 -> falls back to shared "Quantity", not eligible there.
                audit_rules.validate_cross_check_payload("Quantity", "All equal", [10, 11], False, {})
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Quantity", raised.exception.detail)

    def test_validate_blank_override_falls_back_to_shared_field(self):
        from routers import audit_rules

        with patch.object(audit_rules, "execute_query_pg", side_effect=self._doc_row), \
             patch.object(audit_rules, "get_document_cross_check_fields",
                          side_effect=lambda doc_id, dtype=None: ["Amount"]):
            # "  " normalises to None -> shared "Amount" is used and is eligible.
            audit_rules.validate_cross_check_payload("Amount", "All equal", [10], False, {10: "  "})

    def test_save_persists_per_document_compare_field_with_null_fallback(self):
        from routers import audit_rules

        writes = []
        with patch.object(audit_rules, "execute_write_pg",
                          side_effect=lambda sql, params=None: writes.append((sql, params))):
            audit_rules.save_cross_check_documents(30, [10, 11], {11: "Quantity Received"})

        inserts = [params for sql, params in writes if "INSERT INTO audit_rule_cross_check_documents" in sql]
        self.assertEqual(inserts, [
            (30, 10, 0, None),
            (30, 11, 1, "Quantity Received"),
        ])

    def test_row_to_cross_check_exposes_effective_field(self):
        from routers import audit_rules

        junction = [
            {"document_id": 10, "name": "Purchase Order", "compare_field": None},
            {"document_id": 11, "name": "Store Receiving", "compare_field": "Quantity Received"},
        ]
        with patch.object(audit_rules, "execute_query_pg", side_effect=lambda sql, params=None: junction), \
             patch.object(audit_rules, "get_cross_check_used_by_count", return_value=0):
            result = audit_rules.row_to_cross_check({
                "id": 30, "name": "Qty chain", "compare_field": "Quantity",
                "comparison_type": "All equal", "compare_to_voucher_line": 0, "is_active": True,
            })

        docs = {d["document_id"]: d for d in result["documents"]}
        self.assertIsNone(docs[10]["compare_field"])
        self.assertEqual(docs[10]["effective_compare_field"], "Quantity")
        self.assertEqual(docs[11]["compare_field"], "Quantity Received")
        self.assertEqual(docs[11]["effective_compare_field"], "Quantity Received")


if __name__ == "__main__":
    unittest.main()
