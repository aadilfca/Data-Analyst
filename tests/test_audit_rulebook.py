import unittest
from unittest.mock import patch
import os
import sqlite3
import tempfile

import database
from database import init_db
from routers.audit_rules import (
    cache_get,
    cache_set,
    get_audit_rules_config_version,
    invalidate_audit_rules_cache,
    invalidate_category_for_account_cache,
    load_client_account_category_index,
    load_client_verification_rulebook,
)
from routers.audit_rules import compile_verification_rulebook
from routers.verification import _build_working_paper_gates, get_verification_session_rulebook


class AuditRulebookCompilerTests(unittest.TestCase):
    def setUp(self):
        invalidate_audit_rules_cache()

    def test_invalidation_increments_version_and_expires_cached_value(self):
        starting_version = get_audit_rules_config_version()
        cache_set("rulebook:test", {"value": 1})
        self.assertEqual(cache_get("rulebook:test"), {"value": 1})

        invalidate_audit_rules_cache()

        self.assertEqual(get_audit_rules_config_version(), starting_version + 1)
        self.assertIsNone(cache_get("rulebook:test"))

    def test_session_schema_has_nullable_rulebook_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            database.DB_NAME = os.path.join(tmpdir, "test.db")
            init_db()
            conn = sqlite3.connect(database.DB_NAME)
            try:
                columns = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(verification_sessions)")}
            finally:
                conn.close()
            self.assertIn("rulebook_version", columns)
            self.assertEqual(columns["rulebook_version"], 0)

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
            if "FROM audit_rule_document_fields" in sql:
                return [{"document_id": 10, "field_name": "Amount", "sort_order": 0, "master_column_id": 20}]
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
        self.assertEqual(document["signature_requirements"][0]["specimens"][0]["expected_signatory_name"], "A. Manager")
        self.assertEqual(rulebook["categories"][0]["cross_checks"][0]["document_ids"], [10])
        self.assertEqual(rulebook["templates"][0]["columns"], [{
            "column_id": 20,
            "column_name": "Amount",
            "data_type": "number",
            "sort_order": 0,
        }])
        self.assertEqual(rulebook["warnings"], [])

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
            if "FROM audit_rule_document_auth_roles" in sql or "FROM audit_rule_document_fields" in sql or "FROM audit_rule_cross_checks" in sql or "FROM audit_rule_cross_check_documents" in sql or "FROM audit_rule_category_cross_checks" in sql or "FROM audit_rule_voucher_checks" in sql:
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


if __name__ == "__main__":
    unittest.main()
