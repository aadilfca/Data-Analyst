import unittest
from unittest.mock import patch
import json
import os
import sqlite3
import tempfile
import importlib
from unittest.mock import Mock
from contextlib import nullcontext
from datetime import date
from pathlib import Path

from fastapi import HTTPException
from playwright.sync_api import sync_playwright

import database
from database import init_db
from database import get_audit_rules_config_changes_since
from routers.audit_rules import (
    cache_get,
    cache_set,
    get_audit_rules_config_version,
    get_document_cross_check_fields,
    get_document_cross_check_field_specs,
    build_verification_field_policy,
    validate_and_normalize_verification_field_policy,
    normalize_form_fields,
    CategoryCreate,
    CategoryUpdate,
    create_category,
    update_category,
    PhysicalBillMigrationPreviewRequest,
    PhysicalBillMigrationApplyRequest,
    get_physical_bill_migration_inventory,
    preview_physical_bill_migration,
    apply_physical_bill_migration,
    DocumentUpdate,
    invalidate_audit_rules_cache,
    invalidate_category_for_account_cache,
    load_client_account_category_index,
    load_client_verification_rulebook,
    row_to_document_list,
    validate_cross_check_cell,
)
from routers.audit_rules import compile_verification_rulebook
from routers.audit_rules import CategoryValidationRules, validate_category_validation_rules
from routers.verification import (
    _build_working_paper_gates,
    _evaluate_matrix_cross_check,
    _get_form_cross_check_values,
    _rulebook_cross_check_configs,
    _form_value_for_storage,
    _shape_form_entries,
    _replace_form_entries,
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

    def test_category_create_ignores_legacy_evidence_type_and_does_not_write_it(self):
        category = CategoryCreate(name="Material", evidence_type="billable")
        with patch("routers.audit_rules.validate_category_payload"), \
             patch("routers.audit_rules.execute_write_pg", return_value=9) as write, \
             patch("routers.audit_rules.save_category_documents"), \
             patch("routers.audit_rules.save_category_cross_checks"), \
             patch("routers.audit_rules.log_audit"), \
             patch("routers.audit_rules.invalidate_audit_rules_cache"):
            result = create_category(category)

        sql, params = write.call_args.args
        self.assertEqual(result["category_id"], 9)
        self.assertNotIn("evidence_type", sql)
        self.assertEqual(params, ("Material", None))

    def test_category_update_preserves_existing_legacy_evidence_type(self):
        category = CategoryUpdate(name="Material Revised", kb_overview="Updated")
        with patch("routers.audit_rules.execute_query_pg", return_value=[{"id": 9}]), \
             patch("routers.audit_rules.validate_category_payload"), \
             patch("routers.audit_rules.get_category_document_requireds", return_value={}), \
             patch("routers.audit_rules.execute_write_pg") as write, \
             patch("routers.audit_rules.save_category_documents"), \
             patch("routers.audit_rules.save_category_cross_checks"), \
             patch("routers.audit_rules.log_audit"), \
             patch("routers.audit_rules.invalidate_audit_rules_cache"):
            result = update_category(9, category)

        sql, params = write.call_args.args
        self.assertEqual(result["message"], "Category updated successfully")
        self.assertNotIn("evidence_type", sql)
        self.assertEqual(params, ("Material Revised", "Updated", 9))

    def test_category_evidence_type_authoring_surface_is_retired(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "static" / "audit_rules.html").read_text(encoding="utf-8")
        script = (root / "static" / "audit_rules.js").read_text(encoding="utf-8")

        self.assertNotIn("arCategoryEvidenceTypeSelect", html)
        self.assertNotIn("arCategoryEvidenceTypeSelect", script)
        self.assertNotIn("evidence_type", script)

    def test_physical_bill_migration_inventory_is_read_only_and_complete(self):
        responses = [
            [
                {"id": 11, "name": "Legacy Physical Bill", "document_type": "physical_bill", "is_active": True},
                {"id": 21, "name": "Physical Vendor Bill", "document_type": "form", "is_active": True},
            ],
            [{"document_id": 11, "category_id": 7, "category_name": "Material", "is_required": True}],
            [{"document_id": 11, "bill_count": 3, "line_count": 2}],
            [{"document_id": 11, "cross_check_count": 1}],
            [{"document_id": 21, "id": 31, "field_name": "Bill No", "field_type": "text", "field_scope": "header", "sort_order": 0}],
        ]
        with patch("routers.audit_rules.execute_query_pg", side_effect=responses) as query:
            result = get_physical_bill_migration_inventory()

        self.assertEqual(query.call_count, 5)
        legacy = next(d for d in result["documents"] if d["document_id"] == 11)
        self.assertEqual(legacy["legacy_data"], {"bill_count": 3, "line_count": 2})
        self.assertEqual(legacy["categories"][0]["category_name"], "Material")
        self.assertEqual(result["legacy_fields"][-1], "Amount")
        self.assertTrue(any("read-only" in warning for warning in result["warnings"]))

    def test_physical_bill_migration_preview_requires_explicit_scoped_mapping(self):
        responses = [
            [
                {"id": 11, "name": "Legacy Physical Bill", "document_type": "physical_bill", "is_active": True},
                {"id": 21, "name": "Physical Vendor Bill", "document_type": "form", "is_active": True},
            ],
            [{"id": 31, "field_name": "Bill No", "field_type": "text", "field_scope": "header", "sort_order": 0}],
            [{"bill_count": 3}],
            [{"entry_count": 0}],
            [],
            [],
            [],
            [],
        ]
        request = PhysicalBillMigrationPreviewRequest(
            source_document_id=11,
            target_document_id=21,
            field_mapping={"Bill No": 31},
        )
        with patch("routers.audit_rules.execute_query_pg", side_effect=responses):
            result = preview_physical_bill_migration(request)

        self.assertFalse(result["ready"])
        self.assertEqual(result["legacy_bill_count"], 3)
        self.assertFalse(result["writes_performed"])
        self.assertTrue(any("Bill Date" in blocker for blocker in result["blockers"]))

    def _physical_bill_form_fields(self):
        return [
            {"id": 101, "field_name": "Bill No", "field_type": "text", "field_scope": "header", "sort_order": 0},
            {"id": 102, "field_name": "Bill Date", "field_type": "date", "field_scope": "header", "sort_order": 1},
            {"id": 103, "field_name": "Vendor Name", "field_type": "text", "field_scope": "header", "sort_order": 2},
            {"id": 104, "field_name": "Item Name", "field_type": "text", "field_scope": "item", "sort_order": 3},
            {"id": 105, "field_name": "Quantity", "field_type": "numeric", "field_scope": "item", "sort_order": 4},
            {"id": 106, "field_name": "Rate", "field_type": "numeric", "field_scope": "item", "sort_order": 5},
            {"id": 107, "field_name": "Amount", "field_type": "numeric", "field_scope": "item", "sort_order": 6},
        ]

    def _physical_bill_mapping(self):
        return {
            "Bill No": 101, "Bill Date": 102, "Vendor Name": 103,
            "Item Name": 104, "Quantity": 105, "Rate": 106, "Amount": 107,
        }

    def _physical_bill_preview_responses(self, target_entry_count=0):
        return [
            [
                {"id": 11, "name": "Legacy Physical Bill", "document_type": "physical_bill", "is_active": True},
                {"id": 21, "name": "Physical Vendor Bill", "document_type": "form", "is_active": True},
            ],
            self._physical_bill_form_fields(),
            [{"bill_count": 1}],
            [{"entry_count": target_entry_count}],
            [{"id": 501, "category_id": 7, "is_required": True, "target_id": None}],
            [{"id": 601, "cross_check_id": 8, "sort_order": 2, "compare_field": "Amount", "target_id": None}],
            [{"id": 701, "voucher_line_id": 42, "role_name": "Prepared by", "target_id": None}],
            [{"id": 801, "voucher_line_id": 42, "observation_text": "Legacy note", "synced": False, "last_synced_text": None, "updated_by": "Analyst", "target_id": None}],
        ]

    def test_physical_bill_migration_apply_copies_entries_and_retargets_references(self):
        responses = self._physical_bill_preview_responses() + [
            [{"id": 901, "voucher_line_id": 42, "bill_no": "B-1", "bill_date": date(2026, 8, 1), "vendor_name": "Acme", "sort_order": 0}],
            [{"bill_id": 901, "item_name": "Paper", "quantity": 2, "rate": 5, "amount": 10, "sort_order": 0}],
        ]
        writes = []
        write_results = iter([1001, None, None, None, 1002, None, None, None, None, None, None, None, None, None])
        request = PhysicalBillMigrationApplyRequest(
            source_document_id=11,
            target_document_id=21,
            field_mapping=self._physical_bill_mapping(),
            confirm=True,
        )
        with patch("routers.audit_rules.execute_query_pg", side_effect=responses), \
             patch("routers.audit_rules.execute_write_pg", side_effect=lambda sql, params=None: (writes.append((sql, params)), next(write_results))[1]), \
             patch("routers.audit_rules.pg_transaction", return_value=nullcontext()), \
             patch("routers.audit_rules.invalidate_audit_rules_cache"), \
             patch("routers.audit_rules.log_audit"):
            result = apply_physical_bill_migration(request)

        self.assertTrue(result["writes_performed"])
        self.assertTrue(result["legacy_rows_preserved"])
        self.assertEqual(result["migrated_bill_count"], 1)
        self.assertEqual(result["migrated_form_entry_count"], 2)
        self.assertEqual(result["migrated_reference_counts"], {
            "categories": 1, "cross_checks": 1, "procedure_checks": 1, "observations": 1,
        })
        sql_text = "\n".join(sql for sql, _ in writes)
        self.assertIn("INSERT INTO voucher_line_form_entries", sql_text)
        self.assertIn("UPDATE audit_rule_category_documents", sql_text)
        self.assertIn("UPDATE audit_rule_cross_check_documents", sql_text)
        self.assertNotIn("DELETE FROM voucher_line_physical_bills", sql_text)

    def test_physical_bill_migration_apply_blocks_existing_target_data_without_writes(self):
        request = PhysicalBillMigrationApplyRequest(
            source_document_id=11,
            target_document_id=21,
            field_mapping=self._physical_bill_mapping(),
            confirm=True,
        )
        with patch("routers.audit_rules.execute_query_pg", side_effect=self._physical_bill_preview_responses(target_entry_count=2)), \
             patch("routers.audit_rules.execute_write_pg") as write, \
             patch("routers.audit_rules.pg_transaction", return_value=nullcontext()):
            with self.assertRaises(HTTPException) as raised:
                apply_physical_bill_migration(request)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("will not merge or overwrite", str(raised.exception.detail))
        write.assert_not_called()

    def test_physical_bill_migration_apply_requires_explicit_confirmation(self):
        request = PhysicalBillMigrationApplyRequest(
            source_document_id=11,
            target_document_id=21,
            field_mapping=self._physical_bill_mapping(),
        )
        with patch("routers.audit_rules.pg_transaction") as transaction:
            with self.assertRaises(HTTPException) as raised:
                apply_physical_bill_migration(request)

        self.assertEqual(raised.exception.status_code, 400)
        transaction.assert_not_called()

    def test_physical_bill_migration_apply_propagates_failure_for_transaction_rollback(self):
        class TrackingTransaction:
            def __init__(self):
                self.exception_type = None

            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                self.exception_type = exception_type
                return False

        transaction = TrackingTransaction()
        responses = self._physical_bill_preview_responses() + [
            [{"id": 901, "voucher_line_id": 42, "bill_no": "B-1", "bill_date": date(2026, 8, 1), "vendor_name": "Acme", "sort_order": 0}],
            [{"bill_id": 901, "item_name": "Paper", "quantity": 2, "rate": 5, "amount": 10, "sort_order": 0}],
        ]
        request = PhysicalBillMigrationApplyRequest(
            source_document_id=11,
            target_document_id=21,
            field_mapping=self._physical_bill_mapping(),
            confirm=True,
        )
        with patch("routers.audit_rules.execute_query_pg", side_effect=responses), \
             patch("routers.audit_rules.execute_write_pg", side_effect=RuntimeError("simulated migration failure")), \
             patch("routers.audit_rules.pg_transaction", return_value=transaction), \
             patch("routers.audit_rules.invalidate_audit_rules_cache"), \
             patch("routers.audit_rules.log_audit"):
            with self.assertRaisesRegex(RuntimeError, "simulated migration failure"):
                apply_physical_bill_migration(request)

        self.assertIs(transaction.exception_type, RuntimeError)

    def test_session_schema_has_nullable_rulebook_version(self):
        conn = sqlite3.connect(database.DB_NAME)
        try:
            columns = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(verification_sessions)")}
        finally:
            conn.close()
        self.assertIn("rulebook_version", columns)
        self.assertEqual(columns["rulebook_version"], 0)

    def test_document_schema_has_verification_field_policy(self):
        conn = sqlite3.connect(database.DB_NAME)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_rule_documents)")}
        finally:
            conn.close()
        self.assertIn("verification_field_policy", columns)

    def test_verification_policy_normalizes_order_and_rejects_duplicate_columns(self):
        columns = [
            {"column_id": 1, "column_name": "Vendor Name", "data_type": "Text", "sort_order": 0},
            {"column_id": 2, "column_name": "Quantity", "data_type": "Numeric", "sort_order": 1},
            {"column_id": 3, "column_name": "Batch No", "data_type": "Text", "sort_order": 2},
        ]
        policy = {"core": {"vendor": 1, "quantity": 2},
                  "optional": [{"column_id": 3, "risk_level": "High", "sort_order": 8}]}
        with patch("routers.audit_rules.execute_query_pg", return_value=columns):
            normalized = validate_and_normalize_verification_field_policy(9, policy)
        self.assertEqual(normalized["optional"], [{"column_id": 3, "risk_level": "High", "sort_order": 0}])

        duplicate = {"core": {"vendor": 1, "item": 1}}
        with patch("routers.audit_rules.execute_query_pg", return_value=columns):
            with self.assertRaises(Exception) as raised:
                validate_and_normalize_verification_field_policy(9, duplicate)
        self.assertEqual(raised.exception.status_code, 400)

    def test_verification_policy_uses_core_aliases_and_reports_unavailable_saved_fields(self):
        columns = [
            {"column_id": 1, "column_name": "Supplier Name", "data_type": "Text", "sort_order": 0},
            {"column_id": 2, "column_name": "Quantity", "data_type": "Numeric", "sort_order": 1},
        ]
        stored = {"version": 1, "core": {"vendor": 1, "amount": 99},
                  "optional": [{"column_id": 88, "risk_level": "Low", "sort_order": 0}]}
        with patch("routers.audit_rules.execute_query_pg", return_value=columns):
            result = build_verification_field_policy(9, stored)
        vendor = next(field for field in result["core_fields"] if field["key"] == "vendor")
        self.assertEqual(vendor["column_name"], "Supplier Name")
        self.assertEqual({item["column_id"] for item in result["unavailable_fields"]}, {99, 88})

    def test_category_validation_rules_accepts_omitted_legacy_math_rules(self):
        # The current UI sends Procedure/Evidence only; the legacy mathematical list is
        # intentionally omitted so the backend can preserve any older stored value.
        rules = CategoryValidationRules(procedure_evidence={"checks": []})
        validate_category_validation_rules(rules)

    def test_category_validation_rules_rejects_document_outside_applicable_set(self):
        rules = CategoryValidationRules(
            procedure_evidence={"checks": [{"document_id": 20, "condition": "required_document", "risk": "Medium"}]}
        )

        with patch(
            "routers.audit_rules.execute_query_pg",
            return_value=[{"document_id": 10}],
        ):
            with self.assertRaises(Exception) as raised:
                validate_category_validation_rules(rules, category_id=7)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Applicable Documents", raised.exception.detail)

    def test_category_validation_rules_accepts_document_in_applicable_set(self):
        rules = CategoryValidationRules(
            procedure_evidence={"checks": [{"document_id": 20, "condition": "required_document", "risk": "Medium"}]}
        )

        def data_query(sql, params=()):
            if "audit_rule_category_documents" in sql:
                return [{"document_id": 20}]
            if "audit_rule_documents" in sql:
                return [{"id": 20}]
            raise AssertionError("Unexpected validation query: " + sql)

        with patch("routers.audit_rules.execute_query_pg", side_effect=data_query):
            validate_category_validation_rules(rules, category_id=7)

    def test_category_procedure_rule_requires_valid_risk_and_role_detail(self):
        def data_query(sql, params=()):
            if "audit_rule_category_documents" in sql:
                return [{"document_id": 20}]
            if "audit_rule_documents" in sql:
                return [{"id": 20}]
            raise AssertionError("Unexpected validation query: " + sql)

        invalid_risk = CategoryValidationRules(
            procedure_evidence={"checks": [{
                "document_id": 20, "condition": "approved_by", "sub_value": "Approved by", "risk": "Critical"
            }]}
        )
        with patch("routers.audit_rules.execute_query_pg", side_effect=data_query):
            with self.assertRaises(Exception) as raised:
                validate_category_validation_rules(invalid_risk, category_id=7)
        self.assertIn("valid risk", raised.exception.detail)

        missing_detail = CategoryValidationRules(
            procedure_evidence={"checks": [{
                "document_id": 20, "condition": "approved_by", "risk": "High"
            }]}
        )
        with patch("routers.audit_rules.execute_query_pg", side_effect=data_query):
            with self.assertRaises(Exception) as raised:
                validate_category_validation_rules(missing_detail, category_id=7)
        self.assertIn("role or signatory detail", raised.exception.detail)

    def test_preview_unconfigured_document_policy_projects_all_template_columns(self):
        from routers.data import get_preview_rows

        columns = [
            {"column_id": 1, "column_name": "Vendor", "data_type": "Text", "sort_order": 0},
            {"column_id": 2, "column_name": "Internal Note", "data_type": "Text", "sort_order": 1},
        ]
        captured = []

        def data_query(sql, params=()):
            captured.append((sql, params))
            if "FROM clients" in sql:
                return [{"id": 7}]
            if "FROM templates WHERE" in sql:
                return [{"id": 9, "template_name": "Purchase Data"}]
            if "FROM audit_rule_documents" in sql:
                return [{"id": 12, "template_id": 9, "document_type": "linked_template", "verification_field_policy": None}]
            if "SELECT m.column_name, m.data_type" in sql:
                return columns
            if "COUNT(*) as total" in sql:
                return [{"total": 1}]
            if "COUNT(*) as sunday_count" in sql:
                return [{"sunday_count": 0}]
            if "SELECT cd.id" in sql:
                return [{"id": 501, "row_data": {"Vendor": "Acme", "Internal Note": "memo"},
                         "extract_amount": 100, "extract_date": "2026-08-11",
                         "data_quality_flag": None, "rate_source": None}]
            if "data_audit_log" in sql:
                return []
            raise AssertionError("Unexpected Data Bank query: " + sql)

        with patch("routers.data.execute_query_pg", side_effect=data_query), \
             patch("routers.audit_rules.execute_query_pg", return_value=columns), \
             patch("routers.data.attach_history_flags"), \
             patch("routers.data.attach_assignment_flags"):
            result = get_preview_rows(client_id=7, template_id=9, date_start="2026-08-01",
                                      date_end="2026-08-31", page=1, limit=50, search=None,
                                      amount_min=None, amount_max=None, sunday_only=False,
                                      data_quality=None, document_id=12, perf=True)

        self.assertEqual(result["rows"][0]["row_data"], {"Vendor": "Acme", "Internal Note": "memo"})
        self.assertIn("_perf", result)
        self.assertIn("db_calls", result["_perf"])
        data_call = next((item for item in captured if "SELECT cd.id" in item[0]), None)
        self.assertIsNotNone(data_call)
        self.assertEqual(data_call[1][0], ["Vendor", "Internal Note"])

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
                return [{
                    "id": 1, "name": "Fuel", "kb_overview": "Fuel controls", "evidence_type": "billable",
                    "matrix_version": 3,
                    "validation_rules_json": json.dumps({"procedure_evidence": {"checks": [{
                        "document_id": 10, "condition": "signature", "sub_value": "Approved by",
                        "role": "Approved by", "risk": "High"
                    }]}}),
                }]
            if "FROM audit_rule_documents" in sql:
                return [{
                    "id": 10, "name": "Invoice", "kb_note": "Keep invoice", "template_id": 5,
                    "risk_rating": "High", "transaction_level_approval": True,
                    "document_type": "linked_template",
                    "verification_field_policy": {"version": 1, "core": {"amount": 20}, "optional": []},
                }]
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
            if "FROM audit_rule_cross_check_fields" in sql:
                return [{
                    "id": 31, "cross_check_id": 30, "sort_order": 0, "operator": ">=",
                    "tolerance": 0.5, "missing_data_behavior": "missing", "blocking": True, "risk": "High",
                }]
            if "FROM audit_rule_cross_check_cells" in sql:
                return [{"field_id": 31, "document_id": 10, "column_name": "Amount"}]
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
        self.assertTrue(document["is_active"])
        self.assertTrue(document["required"])
        self.assertEqual(document["fields"], [
            {"field_name": "Amount", "master_column_id": 20},
            {"field_name": "Vendor", "master_column_id": 21},
        ])
        self.assertEqual(document["signature_requirements"][0]["specimens"][0]["expected_signatory_name"], "A. Manager")
        self.assertEqual(document["verification_fields"][0]["column_name"], "Amount")
        self.assertEqual(document["signatories"][0]["role_name"], "Approved by")
        procedure = rulebook["categories"][0]["procedure_evidence"]["checks"][0]
        self.assertEqual(procedure["document_name"], "Invoice")
        self.assertEqual(procedure["role_name"], "Approved by")
        self.assertEqual(procedure["selected_detail"], "Approved by")
        self.assertEqual(procedure["configured_signatories"][0]["expected_signatory_name"], "A. Manager")
        self.assertEqual(rulebook["categories"][0]["cross_checks"][0]["document_ids"], [10])
        matrix = rulebook["categories"][0]["cross_checks"][0]["matrix"]
        self.assertEqual(matrix["documents"][0]["name"], "Invoice")
        self.assertEqual(matrix["fields"][0]["cells"], {"10": "Amount"})
        self.assertEqual(matrix["fields"][0]["operator"], ">=")
        self.assertEqual(rulebook["templates"][0]["columns"], [{
            "column_id": 20,
            "column_name": "Amount",
            "data_type": "number",
            "sort_order": 0,
        }])
        self.assertEqual(rulebook["warnings"], [])

    def test_compiler_blocks_attached_cross_check_with_unselected_dependency(self):
        def query(sql, params=()):
            if "FROM audit_rule_categories" in sql:
                return [{
                    "id": 1, "name": "Material", "kb_overview": None, "evidence_type": "billable",
                    "matrix_version": 1, "validation_rules_json": None,
                }]
            if "FROM audit_rule_documents" in sql:
                return [
                    {"id": 10, "name": "Purchase Demand", "kb_note": None, "template_id": 5,
                     "risk_rating": None, "transaction_level_approval": False,
                     "document_type": "linked_template", "verification_field_policy": None},
                    {"id": 11, "name": "Purchase Order", "kb_note": None, "template_id": 5,
                     "risk_rating": None, "transaction_level_approval": False,
                     "document_type": "linked_template", "verification_field_policy": None},
                ]
            if "FROM audit_rule_category_documents" in sql:
                return [{"category_id": 1, "document_id": 10, "is_required": True, "sort_order": 0}]
            if "FROM audit_rule_cross_checks" in sql:
                return [{"id": 30, "name": "Demand to order", "compare_field": "Amount",
                         "comparison_type": "Exact", "compare_to_voucher_line": False}]
            if "FROM audit_rule_cross_check_documents" in sql:
                return [{"cross_check_id": 30, "document_id": 10, "sort_order": 0},
                        {"cross_check_id": 30, "document_id": 11, "sort_order": 1}]
            if "FROM audit_rule_category_cross_checks" in sql:
                return [{"category_id": 1, "cross_check_id": 30, "sort_order": 0}]
            if "FROM audit_rule_document_auth_roles" in sql or "FROM audit_rule_document_fields" in sql \
                    or "FROM master_columns" in sql or "FROM audit_rule_cross_check_fields" in sql \
                    or "FROM audit_rule_cross_check_cells" in sql or "FROM audit_rule_voucher_checks" in sql \
                    or "FROM templates" in sql or "FROM template_column_mappings" in sql \
                    or "FROM signature_specimens" in sql or "FROM trial_balance_uploads" in sql \
                    or "FROM account_category_assignments" in sql:
                return []
            self.fail(f"Unhandled query: {sql}")

        with patch("routers.audit_rules.execute_query_pg", side_effect=query):
            rulebook = compile_verification_rulebook(7)

        profile = rulebook["categories"][0]
        self.assertEqual(profile["cross_checks"], [])
        warning = next(item for item in rulebook["warnings"] if item["code"] == "cross_check_dependency_invalid")
        self.assertEqual(warning["category_id"], 1)
        self.assertEqual(warning["cross_check_id"], 30)
        self.assertEqual(warning["missing_document_ids"], [11])

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

    def test_form_cross_check_field_specs_keep_form_name_and_linked_column(self):
        with patch("routers.audit_rules.execute_query_pg", return_value=[
            {"field_name": "Quantity", "field_type": "numeric", "linked_column": "Receive Quantity"},
            {"field_name": "Rate", "field_type": "numeric", "linked_column": "Rate"},
        ]):
            specs = get_document_cross_check_field_specs(42, "form")
            field_names = get_document_cross_check_fields(42, "form")

        self.assertEqual(specs[0], {
            "field_name": "Quantity",
            "data_type": "numeric",
            "linked_column": "Receive Quantity",
            "source": "form",
        })
        self.assertEqual(field_names, ["Quantity", "Rate"])

    def test_form_cross_check_cell_validation_uses_form_fields_with_linked_template(self):
        from fastapi import HTTPException
        from routers import audit_rules

        def query(sql, params=()):
            if "FROM audit_rule_cross_check_fields WHERE id" in sql:
                return [{"cross_check_id": 30}]
            if "FROM audit_rule_cross_check_documents WHERE cross_check_id" in sql:
                return [{"exists": 1}]
            if "SELECT document_type FROM audit_rule_documents" in sql:
                return [{"document_type": "form"}]
            if "FROM audit_rule_document_fields" in sql:
                return [{"field_name": "Quantity", "field_type": "numeric", "linked_column": "Receive Quantity"}]
            self.fail(f"Unhandled query: {sql}")

        with patch("routers.audit_rules.execute_query_pg", side_effect=query), \
             patch("routers.audit_rules.get_document_template_columns",
                   return_value=[{"column_name": "Receive Quantity"}, {"column_name": "Rate"}]), \
             patch.object(audit_rules, "execute_write_pg") as write, \
             patch.object(audit_rules, "invalidate_audit_rules_cache"), \
             patch.object(audit_rules, "log_audit"):
            audit_rules.set_cross_check_cell(5, 42, audit_rules.CrossCheckCellUpdate(column_name="Quantity"))
            self.assertEqual(write.call_args.args[1], (5, 42, "Quantity"))
            write.reset_mock()
            for invalid_name in ("Receive Quantity", "Rate", "Unknown"):
                with self.subTest(field=invalid_name), self.assertRaises(HTTPException) as raised:
                    audit_rules.set_cross_check_cell(5, 42, audit_rules.CrossCheckCellUpdate(column_name=invalid_name))
                self.assertEqual(raised.exception.status_code, 400)
            write.assert_not_called()

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
                        authority_roles: [{role_name: 'Approved by'}],
                        verification_fields: [{column_name: 'Amount', data_type: 'Numeric'}],
                        signatories: [{role_name: 'Approved by', expected_signatory_name: 'A. Manager'}]
                    }],
                    procedure_evidence: {checks: [{
                        document_id: 10, condition: 'approved_by', selected_detail: 'A. Manager'
                    }]},
                    cross_checks: [{
                        id: 30,
                        compare_field: 'Amount',
                        comparison_type: 'Exact',
                        document_ids: [10, 99]
                    }]
                })""")
                self.assertEqual(result["documents"][0]["id"], 10)
                self.assertEqual(result["documents"][0]["auth_roles"], ["Approved by"])
                self.assertEqual(result["documents"][0]["verification_fields"][0]["column_name"], "Amount")
                self.assertEqual(result["documents"][0]["procedure_checks"][0]["condition"], "approved_by")
                self.assertTrue(result["documents"][0]["procedure_rules_configured"])
                self.assertEqual(result["cross_checks"][0]["document_ids"], [10, 99])
                self.assertEqual(result["cross_checks"][0]["documents"], ["Invoice", "Document 99"])
                self.assertEqual(result["cross_checks"][0]["compare_field"], "Amount")
                self.assertEqual(result["cross_checks"][0]["comparison_type"], "Exact")
            finally:
                browser.close()

    def test_frozen_session_without_category_does_not_fall_back_to_live_guidance(self):
        verification_js = Path(__file__).resolve().parents[1] / "static" / "verification.js"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.set_content("<html><body></body></html>")
                page.add_script_tag(content="""
                    var API_BASE = '';
                    window.ApiUtils = { apiFetch: async () => {
                        window.liveGuidanceFallbackCalled = true;
                        return {status: 404};
                    }};
                """)
                page.add_script_tag(path=str(verification_js))
                result = page.evaluate("""async () => {
                    sessionRulebook = {categories: []};
                    const guidance = await fetchAuditGuidanceData('Missing category');
                    return {guidance, fallbackCalled: Boolean(window.liveGuidanceFallbackCalled)};
                }""")
                self.assertIsNone(result["guidance"])
                self.assertFalse(result["fallbackCalled"])
            finally:
                browser.close()

    def test_rulebook_cross_check_adapter_preserves_matrix_rows(self):
        rulebook = {
            "categories": [{
                "category_id": 7,
                "cross_checks": [{
                    "cross_check_id": 30,
                    "name": "Order to receipt",
                    "matrix": {
                        "documents": [
                            {"document_id": 10, "name": "Order", "sort_order": 0},
                            {"document_id": 11, "name": "Receipt", "sort_order": 1},
                        ],
                        "fields": [{
                            "id": 31, "cells": {"10": "Quantity", "11": "Quantity"},
                            "operator": ">=", "tolerance": 0.5,
                            "missing_data_behavior": "shortfall", "blocking": False, "risk": "High",
                        }],
                    },
                }],
            }]
        }
        configs = _rulebook_cross_check_configs(7, rulebook)
        self.assertEqual(configs[0]["documents"][1]["document_id"], 11)
        self.assertEqual(configs[0]["matrix_fields"][0]["cells"]["10"], "Quantity")
        self.assertEqual(configs[0]["matrix_fields"][0]["operator"], ">=")

    def test_matrix_cross_check_applies_operator_tolerance_and_blocking(self):
        config = {
            "id": 30,
            "name": "Order to receipt",
            "documents": [
                {"document_id": 10, "document_name": "Order", "sort_order": 0},
                {"document_id": 11, "document_name": "Receipt", "sort_order": 1},
            ],
            "matrix_fields": [{
                "id": 31, "sort_order": 0,
                "cells": {"10": "Quantity", "11": "Quantity"},
                "operator": ">=", "tolerance": 0,
                "missing_data_behavior": "not_configured", "blocking": True, "risk": "High",
            }],
        }

        def values(ids, field_name):
            return ({1001: (90, 1), 1002: (100, 1)}[ids[0]])

        with patch("routers.verification._sum_document_field", side_effect=values):
            result = _evaluate_matrix_cross_check(config, {10: [1001], 11: [1002]}, 90)

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["matrix_rows"][0]["status"], "fail")
        self.assertEqual(result["risk"], "High")

        config["matrix_fields"][0]["blocking"] = False
        with patch("routers.verification._sum_document_field", side_effect=values):
            result = _evaluate_matrix_cross_check(config, {10: [1001], 11: [1002]}, 90)
        self.assertEqual(result["status"], "shortfall")
        self.assertEqual(result["matrix_rows"][0]["status"], "shortfall")

    def test_matrix_cross_check_reads_saved_form_values_by_document_id(self):
        config = {
            "id": 30,
            "name": "Order to Form Bill",
            "documents": [
                {"document_id": 10, "document_name": "Purchase Order", "sort_order": 0},
                {"document_id": 42, "document_name": "Physical Bill", "document_type": "form", "sort_order": 1},
            ],
            "matrix_fields": [{
                "id": 31, "sort_order": 0,
                "cells": {"10": "Quantity", "42": "Quantity"},
                "operator": "=", "tolerance": 0,
                "missing_data_behavior": "not_configured", "blocking": True, "risk": "High",
            }],
        }

        def values(ids, field_name, manual=None):
            if manual is not None:
                return manual.get(field_name), manual.get("_row_count", 0)
            return 10, 1

        with patch("routers.verification._sum_document_field", side_effect=values):
            result = _evaluate_matrix_cross_check(
                config,
                {10: [1001]},
                90,
                form_values={42: {"Quantity": 10, "_row_count": 1}},
            )

        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["matrix_rows"][0]["values"][1]["linked"])
        self.assertEqual(result["matrix_rows"][0]["values"][1]["value"], 10)

    def test_form_cross_check_values_use_latest_saved_entry_per_document(self):
        rows = [
            {
                "voucher_line_id": 101, "client_data_id": 9001, "document_id": 42,
                "updated_at": "2026-09-01T09:00:00", "field_name": "Quantity",
                "field_type": "numeric", "value_text": None, "value_date": None,
                "value_numeric": 4,
            },
            {
                "voucher_line_id": 102, "client_data_id": 9001, "document_id": 42,
                "updated_at": "2026-09-02T09:00:00", "field_name": "Quantity",
                "field_type": "numeric", "value_text": None, "value_date": None,
                "value_numeric": 10,
            },
        ]
        with patch("routers.verification.execute_query_pg", return_value=rows):
            result = _get_form_cross_check_values(102, [101, 102], [42])

        self.assertEqual(result, {42: {"_row_count": 1, "Quantity": 10.0}})

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
            if "FROM audit_rule_document_auth_roles" in sql or "FROM audit_rule_document_fields" in sql or "FROM master_columns" in sql or "FROM audit_rule_cross_checks" in sql or "FROM audit_rule_cross_check_documents" in sql or "FROM audit_rule_cross_check_fields" in sql or "FROM audit_rule_cross_check_cells" in sql or "FROM audit_rule_category_cross_checks" in sql or "FROM audit_rule_voucher_checks" in sql:
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


class CrossCheckMatrixSchemaTests(unittest.TestCase):
    """Cross-Check Matrix Redesign, Phase 1 of 5: the field-row + cell tables for the
    document x field matrix (additive schema change, old compare_field columns untouched)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        database.DB_NAME = os.path.join(self.tmpdir.name, "test.db")
        init_db()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _columns(self, table):
        conn = database.get_db()
        try:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        finally:
            conn.close()

    def test_field_row_table_shape(self):
        self.assertEqual(
            set(self._columns("audit_rule_cross_check_fields")),
            {"id", "cross_check_id", "sort_order", "operator", "tolerance",
             "missing_data_behavior", "blocking", "risk"},
        )

    def test_cell_table_shape(self):
        self.assertEqual(
            set(self._columns("audit_rule_cross_check_cells")),
            {"id", "field_id", "document_id", "column_name"},
        )

    def _seed_field(self, conn):
        conn.execute(
            "INSERT INTO audit_rule_cross_checks (name, compare_field, comparison_type) "
            "VALUES ('CC', 'X', 'All equal')"
        )
        cc_id = conn.execute("SELECT id FROM audit_rule_cross_checks").fetchone()[0]
        conn.execute("INSERT INTO audit_rule_documents (name) VALUES ('Doc A')")
        doc_id = conn.execute("SELECT id FROM audit_rule_documents").fetchone()[0]
        conn.execute(
            "INSERT INTO audit_rule_cross_check_fields (cross_check_id, sort_order) VALUES (?, 0)",
            (cc_id,),
        )
        field_id = conn.execute("SELECT id FROM audit_rule_cross_check_fields").fetchone()[0]
        return field_id, doc_id

    def test_cell_column_name_accepts_null(self):
        conn = database.get_db()
        try:
            field_id, doc_id = self._seed_field(conn)
            conn.execute(
                "INSERT INTO audit_rule_cross_check_cells (field_id, document_id, column_name) "
                "VALUES (?, ?, NULL)",
                (field_id, doc_id),
            )
            conn.commit()
            value = conn.execute(
                "SELECT column_name FROM audit_rule_cross_check_cells WHERE field_id = ?",
                (field_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(value)

    def test_cell_unique_on_field_and_document(self):
        conn = database.get_db()
        try:
            field_id, doc_id = self._seed_field(conn)
            conn.execute(
                "INSERT INTO audit_rule_cross_check_cells (field_id, document_id, column_name) "
                "VALUES (?, ?, 'Amount')",
                (field_id, doc_id),
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO audit_rule_cross_check_cells (field_id, document_id, column_name) "
                    "VALUES (?, ?, 'Total')",
                    (field_id, doc_id),
                )
                conn.commit()
        finally:
            conn.close()

    def test_cells_cascade_when_field_row_deleted(self):
        conn = database.get_db()
        try:
            conn.execute("PRAGMA foreign_keys = ON")  # app never enables this; needed here to assert CASCADE
            field_id, doc_id = self._seed_field(conn)
            conn.execute(
                "INSERT INTO audit_rule_cross_check_cells (field_id, document_id, column_name) "
                "VALUES (?, ?, 'Amount')",
                (field_id, doc_id),
            )
            conn.execute("DELETE FROM audit_rule_cross_check_fields WHERE id = ?", (field_id,))
            conn.commit()
            remaining = conn.execute(
                "SELECT COUNT(*) FROM audit_rule_cross_check_cells WHERE field_id = ?",
                (field_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(remaining, 0)


class CrossCheckMatrixEndpointTests(unittest.TestCase):
    """Cross-Check Matrix Redesign, Phase 2 of 5: field-row / cell CRUD + matrix read.
    Mock-style like the rest of this file - execute_query_pg/execute_write_pg are Postgres-only."""

    def test_add_field_computes_next_sort_order(self):
        from routers import audit_rules

        writes = []

        def query(sql, params=()):
            if "FROM audit_rule_cross_checks WHERE id" in sql:
                return [{"name": "CC"}]
            if "MAX(sort_order)" in sql:
                return [{"max_order": 2}]
            self.fail(f"Unhandled query: {sql}")

        with patch.object(audit_rules, "execute_query_pg", side_effect=query), \
             patch.object(audit_rules, "execute_write_pg",
                          side_effect=lambda sql, params=None: writes.append((sql, params)) or 7), \
             patch.object(audit_rules, "invalidate_audit_rules_cache"), \
             patch.object(audit_rules, "log_audit"):
            result = audit_rules.add_cross_check_field(30)

        self.assertEqual(result, {
            "field_id": 7,
            "operator": "=",
            "tolerance": 0,
            "missing_data_behavior": "not_configured",
            "blocking": False,
            "risk": "Medium",
        })
        insert = next(p for s, p in writes if "INSERT INTO audit_rule_cross_check_fields" in s)
        self.assertEqual(insert, (30, 3))

    def test_add_field_404_for_missing_cross_check(self):
        from routers import audit_rules

        with patch.object(audit_rules, "execute_query_pg", side_effect=lambda sql, params=(): []):
            with self.assertRaises(Exception) as raised:
                audit_rules.add_cross_check_field(999)
        self.assertEqual(raised.exception.status_code, 404)

    def test_delete_field_removes_cells_first(self):
        from routers import audit_rules

        writes = []
        with patch.object(audit_rules, "execute_query_pg",
                          side_effect=lambda sql, params=(): [{"cross_check_id": 30}]), \
             patch.object(audit_rules, "execute_write_pg",
                          side_effect=lambda sql, params=None: writes.append(sql)), \
             patch.object(audit_rules, "invalidate_audit_rules_cache"), \
             patch.object(audit_rules, "log_audit"):
            audit_rules.delete_cross_check_field(5)

        self.assertTrue(writes[0].startswith("DELETE FROM audit_rule_cross_check_cells"))
        self.assertTrue(writes[1].startswith("DELETE FROM audit_rule_cross_check_fields"))

    def test_reorder_rejects_mismatched_id_set(self):
        from routers import audit_rules

        def query(sql, params=()):
            if "FROM audit_rule_cross_checks WHERE id" in sql:
                return [{"name": "CC"}]
            if "FROM audit_rule_cross_check_fields WHERE cross_check_id" in sql:
                return [{"id": 1}, {"id": 2}]
            self.fail(f"Unhandled query: {sql}")

        with patch.object(audit_rules, "execute_query_pg", side_effect=query):
            with self.assertRaises(Exception) as raised:
                audit_rules.reorder_cross_check_fields(30, audit_rules.CrossCheckFieldOrder(field_ids=[1, 3]))
        self.assertEqual(raised.exception.status_code, 400)

    def test_reorder_writes_sort_order_by_index(self):
        from routers import audit_rules

        writes = []

        def query(sql, params=()):
            if "FROM audit_rule_cross_checks WHERE id" in sql:
                return [{"name": "CC"}]
            if "FROM audit_rule_cross_check_fields WHERE cross_check_id" in sql:
                return [{"id": 1}, {"id": 2}, {"id": 3}]
            self.fail(f"Unhandled query: {sql}")

        with patch.object(audit_rules, "execute_query_pg", side_effect=query), \
             patch.object(audit_rules, "execute_write_pg",
                          side_effect=lambda sql, params=None: writes.append(params)), \
             patch.object(audit_rules, "invalidate_audit_rules_cache"), \
             patch.object(audit_rules, "log_audit"):
            audit_rules.reorder_cross_check_fields(30, audit_rules.CrossCheckFieldOrder(field_ids=[3, 1, 2]))

        self.assertEqual(writes, [(0, 3), (1, 1), (2, 2)])

    def test_set_cell_rejects_column_not_on_template(self):
        from routers import audit_rules

        def query(sql, params=()):
            if "SELECT document_type FROM audit_rule_documents" in sql:
                return [{"document_type": "linked_template"}]
            if "FROM audit_rule_cross_check_fields WHERE id" in sql:
                return [{"cross_check_id": 30}]
            if "FROM audit_rule_cross_check_documents WHERE cross_check_id" in sql:
                return [{"exists": 1}]
            self.fail(f"Unhandled query: {sql}")

        with patch.object(audit_rules, "execute_query_pg", side_effect=query), \
             patch.object(audit_rules, "get_document_template_columns",
                          return_value=[{"column_name": "Amount"}, {"column_name": "Vendor"}]):
            with self.assertRaises(Exception) as raised:
                audit_rules.set_cross_check_cell(5, 10, audit_rules.CrossCheckCellUpdate(column_name="Quantity"))
        self.assertEqual(raised.exception.status_code, 400)

    def test_set_cell_rejects_document_not_a_column(self):
        from routers import audit_rules

        def query(sql, params=()):
            if "FROM audit_rule_cross_check_fields WHERE id" in sql:
                return [{"cross_check_id": 30}]
            if "FROM audit_rule_cross_check_documents WHERE cross_check_id" in sql:
                return []
            self.fail(f"Unhandled query: {sql}")

        with patch.object(audit_rules, "execute_query_pg", side_effect=query):
            with self.assertRaises(Exception) as raised:
                audit_rules.set_cross_check_cell(5, 99, audit_rules.CrossCheckCellUpdate(column_name="Amount"))
        self.assertEqual(raised.exception.status_code, 400)

    def test_set_cell_upserts_valid_column(self):
        from routers import audit_rules

        writes = []

        def query(sql, params=()):
            if "SELECT document_type FROM audit_rule_documents" in sql:
                return [{"document_type": "linked_template"}]
            if "FROM audit_rule_cross_check_fields WHERE id" in sql:
                return [{"cross_check_id": 30}]
            if "FROM audit_rule_cross_check_documents WHERE cross_check_id" in sql:
                return [{"exists": 1}]
            self.fail(f"Unhandled query: {sql}")

        with patch.object(audit_rules, "execute_query_pg", side_effect=query), \
             patch.object(audit_rules, "get_document_template_columns",
                          return_value=[{"column_name": "Amount"}]), \
             patch.object(audit_rules, "execute_write_pg",
                          side_effect=lambda sql, params=None: writes.append((sql, params))), \
             patch.object(audit_rules, "invalidate_audit_rules_cache"), \
             patch.object(audit_rules, "log_audit"):
            audit_rules.set_cross_check_cell(5, 10, audit_rules.CrossCheckCellUpdate(column_name="  Amount  "))

        sql, params = writes[0]
        self.assertIn("ON CONFLICT (field_id, document_id) DO UPDATE", sql)
        self.assertEqual(params, (5, 10, "Amount"))

    def test_set_cell_clear_deletes_row(self):
        from routers import audit_rules

        writes = []

        def query(sql, params=()):
            if "FROM audit_rule_cross_check_fields WHERE id" in sql:
                return [{"cross_check_id": 30}]
            if "FROM audit_rule_cross_check_documents WHERE cross_check_id" in sql:
                return [{"exists": 1}]
            self.fail(f"Unhandled query: {sql}")

        with patch.object(audit_rules, "execute_query_pg", side_effect=query), \
             patch.object(audit_rules, "execute_write_pg",
                          side_effect=lambda sql, params=None: writes.append((sql, params))), \
             patch.object(audit_rules, "invalidate_audit_rules_cache"), \
             patch.object(audit_rules, "log_audit"):
            audit_rules.set_cross_check_cell(5, 10, audit_rules.CrossCheckCellUpdate(column_name=None))

        sql, params = writes[0]
        self.assertTrue(sql.startswith("DELETE FROM audit_rule_cross_check_cells"))
        self.assertEqual(params, (5, 10))

    def test_matrix_shape(self):
        from routers import audit_rules

        def query(sql, params=()):
            if "SELECT id FROM audit_rule_cross_checks WHERE id" in sql:
                return [{"id": 30}]
            if "FROM audit_rule_cross_check_documents ccd" in sql:
                return [{"document_id": 10, "name": "Invoice", "template_id": 5, "sort_order": 0}]
            if "FROM audit_rule_cross_check_fields" in sql:
                return [
                    {"id": 1, "sort_order": 0, "operator": ">=", "tolerance": 0.5,
                     "missing_data_behavior": "missing", "blocking": True, "risk": "High"},
                    {"id": 2, "sort_order": 1, "operator": "=", "tolerance": 0,
                     "missing_data_behavior": "not_configured", "blocking": False, "risk": "Medium"},
                ]
            if "FROM audit_rule_cross_check_cells" in sql:
                return [{"field_id": 1, "document_id": 10, "column_name": "Amount"}]
            self.fail(f"Unhandled query: {sql}")

        with patch.object(audit_rules, "execute_query_pg", side_effect=query):
            matrix = audit_rules.get_cross_check_matrix(30)

        self.assertEqual(matrix["documents"][0]["template_id"], 5)
        self.assertEqual(matrix["fields"][0]["cells"], {"10": "Amount"})
        self.assertEqual(matrix["fields"][0]["operator"], ">=")
        self.assertEqual(matrix["fields"][0]["tolerance"], 0.5)
        self.assertTrue(matrix["fields"][0]["blocking"])
        self.assertEqual(matrix["fields"][0]["risk"], "High")
        self.assertEqual(matrix["fields"][1]["cells"], {})

    def test_condition_validation_rejects_invalid_values(self):
        from routers import audit_rules

        with self.assertRaises(Exception) as raised:
            audit_rules.validate_cross_check_condition(
                audit_rules.CrossCheckConditionUpdate(operator="~")
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_set_condition_persists_complete_reusable_rule(self):
        from routers import audit_rules

        writes = []

        with patch.object(audit_rules, "execute_query_pg", return_value=[{"cross_check_id": 30}]), \
             patch.object(audit_rules, "execute_write_pg",
                          side_effect=lambda sql, params=None: writes.append((sql, params))), \
             patch.object(audit_rules, "invalidate_audit_rules_cache"), \
             patch.object(audit_rules, "log_audit"):
            result = audit_rules.set_cross_check_condition(
                5,
                audit_rules.CrossCheckConditionUpdate(
                    operator=">=", tolerance=1.25, missing_data_behavior="shortfall",
                    blocking=True, risk="High"
                ),
            )

        self.assertEqual(result["message"], "Cross-check condition updated")
        self.assertEqual(writes[0][1], (">=", 1.25, "shortfall", True, "High", 5))

    def test_matrix_404(self):
        from routers import audit_rules

        with patch.object(audit_rules, "execute_query_pg", side_effect=lambda sql, params=(): []):
            with self.assertRaises(Exception) as raised:
                audit_rules.get_cross_check_matrix(999)
        self.assertEqual(raised.exception.status_code, 404)

    def test_get_document_template_columns_no_template(self):
        from routers import audit_rules

        with patch.object(audit_rules, "execute_query_pg",
                          side_effect=lambda sql, params=(): [{"template_id": None}]):
            self.assertEqual(audit_rules.get_document_template_columns(10), [])

    def test_get_document_template_columns_returns_all_active_template_columns(self):
        from routers import audit_rules

        columns = [
            {"column_id": 20, "column_name": "Amount", "data_type": "number", "sort_order": 0},
            {"column_id": 21, "column_name": "Vendor", "data_type": "text", "sort_order": 1},
        ]
        with patch.object(audit_rules, "execute_query_pg",
                          return_value=[{"template_id": 5}]), \
             patch("routers.templates.get_cached_template_columns", return_value=columns) as cached:
            result = audit_rules.get_document_template_columns(10)

        self.assertEqual(result, columns)
        cached.assert_called_once_with(5)

    def test_signature_specimen_listing_excludes_inactive_rows_by_default(self):
        from routers import audit_rules

        captured = []
        specimen = {
            "id": 7,
            "client_id": 3,
            "document_id": 10,
            "document_name": "Purchase Order",
            "role_name": "Approved by",
            "expected_signatory_name": "A. Manager",
            "authority": None,
            "is_active": True,
            "created_at": "2026-09-04T00:00:00",
            "updated_at": "2026-09-04T00:00:00",
        }

        def query(sql, params=()):
            captured.append(sql)
            return [specimen]

        with patch.object(audit_rules, "execute_query_pg", side_effect=query):
            result = audit_rules.get_signature_specimens(client_id=3, document_id=10)

        self.assertEqual(result[0]["expected_signatory_name"], "A. Manager")
        self.assertIn("s.is_active = TRUE", captured[0])

    def test_signature_specimen_delete_uses_soft_delete(self):
        from routers import audit_rules

        writes = []

        def query(sql, params=()):
            return [{"expected_signatory_name": "A. Manager"}]

        with patch.object(audit_rules, "execute_query_pg", side_effect=query), \
             patch.object(audit_rules, "execute_write_pg",
                          side_effect=lambda sql, params=None: writes.append((sql, params))), \
             patch.object(audit_rules, "invalidate_audit_rules_cache"), \
             patch.object(audit_rules, "log_audit"):
            result = audit_rules.delete_signature_specimen(7)

        self.assertIn("UPDATE signature_specimens SET is_active = FALSE", writes[0][0])
        self.assertEqual(result["message"], "Signature specimen deleted (soft-delete) successfully")


class CrossCheckMatrixColumnAxisAndMetaTests(unittest.TestCase):
    """Cross-Check Matrix Redesign, Phase 5 of 5: the /documents (column axis) and /meta endpoints."""

    def test_set_documents_replaces_junction_and_cleans_orphan_cells(self):
        from routers import audit_rules

        writes = []
        with patch.object(audit_rules, "execute_query_pg",
                          side_effect=lambda sql, params=(): [{"name": "CC"}] if "audit_rule_cross_checks" in sql else [{"id": params[0]}]), \
             patch.object(audit_rules, "execute_write_pg",
                          side_effect=lambda sql, params=None: writes.append((sql, params))), \
             patch.object(audit_rules, "invalidate_audit_rules_cache"), \
             patch.object(audit_rules, "log_audit"):
            audit_rules.set_cross_check_documents(30, audit_rules.CrossCheckDocuments(document_ids=[10, 11]))

        self.assertTrue(writes[0][0].startswith("DELETE FROM audit_rule_cross_check_documents"))
        inserts = [p for s, p in writes if "INSERT INTO audit_rule_cross_check_documents" in s]
        self.assertEqual(inserts, [(30, 10, 0), (30, 11, 1)])
        cleanup = [s for s, p in writes if "DELETE FROM audit_rule_cross_check_cells" in s]
        self.assertEqual(len(cleanup), 1)
        self.assertIn("NOT IN", cleanup[0])

    def test_set_documents_empty_clears_all_cells(self):
        from routers import audit_rules

        writes = []
        with patch.object(audit_rules, "execute_query_pg",
                          side_effect=lambda sql, params=(): [{"name": "CC"}]), \
             patch.object(audit_rules, "execute_write_pg",
                          side_effect=lambda sql, params=None: writes.append((sql, params))), \
             patch.object(audit_rules, "invalidate_audit_rules_cache"), \
             patch.object(audit_rules, "log_audit"):
            audit_rules.set_cross_check_documents(30, audit_rules.CrossCheckDocuments(document_ids=[]))

        cleanup = [s for s, p in writes if "DELETE FROM audit_rule_cross_check_cells" in s]
        self.assertEqual(len(cleanup), 1)
        self.assertNotIn("NOT IN", cleanup[0])

    def test_set_documents_404_and_400(self):
        from routers import audit_rules

        with patch.object(audit_rules, "execute_query_pg", side_effect=lambda sql, params=(): []):
            with self.assertRaises(Exception) as raised:
                audit_rules.set_cross_check_documents(999, audit_rules.CrossCheckDocuments(document_ids=[]))
        self.assertEqual(raised.exception.status_code, 404)

        def q(sql, params=()):
            if "FROM audit_rule_cross_checks" in sql:
                return [{"name": "CC"}]
            return []  # document lookup misses
        with patch.object(audit_rules, "execute_query_pg", side_effect=q):
            with self.assertRaises(Exception) as raised:
                audit_rules.set_cross_check_documents(30, audit_rules.CrossCheckDocuments(document_ids=[77]))
        self.assertEqual(raised.exception.status_code, 400)

    def test_meta_updates_scalars_without_touching_compare_field(self):
        from routers import audit_rules

        writes = []
        with patch.object(audit_rules, "execute_query_pg",
                          side_effect=lambda sql, params=(): [{"id": 30}]), \
             patch.object(audit_rules, "execute_write_pg",
                          side_effect=lambda sql, params=None: writes.append((sql, params))), \
             patch.object(audit_rules, "invalidate_audit_rules_cache"), \
             patch.object(audit_rules, "log_audit"):
            audit_rules.update_cross_check_meta(30, audit_rules.CrossCheckMeta(
                name="Renamed", comparison_type="All equal", compare_to_voucher_line=True))

        sql, params = writes[0]
        self.assertIn("UPDATE audit_rule_cross_checks SET name", sql)
        self.assertNotIn("compare_field", sql)
        self.assertEqual(params, ("Renamed", "All equal", True, 30))

    def test_meta_404_and_bad_comparison_type(self):
        from routers import audit_rules

        with patch.object(audit_rules, "execute_query_pg", side_effect=lambda sql, params=(): []):
            with self.assertRaises(Exception) as raised:
                audit_rules.update_cross_check_meta(999, audit_rules.CrossCheckMeta(
                    name="x", comparison_type="All equal"))
        self.assertEqual(raised.exception.status_code, 404)

        with patch.object(audit_rules, "execute_query_pg", side_effect=lambda sql, params=(): [{"id": 30}]):
            with self.assertRaises(Exception) as raised:
                audit_rules.update_cross_check_meta(30, audit_rules.CrossCheckMeta(
                    name="x", comparison_type="Nonsense"))
        self.assertEqual(raised.exception.status_code, 400)


class ProcedureModalPersistenceTests(unittest.TestCase):
    def test_procedure_tabs_use_only_active_unique_guidance_documents(self):
        procedure_modal_js = Path(__file__).resolve().parents[1] / "static" / "procedure_modal.js"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.set_content("<html><body></body></html>")
                page.add_script_tag(content="var API_BASE = ''; window.ApiUtils = {};" )
                page.add_script_tag(path=str(procedure_modal_js))
                result = page.evaluate("""() => filterProcedureGuidanceDocuments([
                    {id: 10, name: 'Purchase Demand'},
                    {document_id: 10, name: 'Purchase Demand duplicate'},
                    {id: 11, name: 'Inactive', is_active: false},
                    {id: 12, name: 'Physical Bill', is_active: true}
                ]).map(document => document.id ?? document.document_id)""")
                self.assertEqual(result, [10, 12])
            finally:
                browser.close()

    def test_procedure_checklist_uses_category_rules_and_signatory_details(self):
        procedure_modal_js = Path(__file__).resolve().parents[1] / "static" / "procedure_modal.js"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.set_content("<html><body></body></html>")
                page.add_script_tag(content="var API_BASE = ''; window.ApiUtils = {};" )
                page.add_script_tag(path=str(procedure_modal_js))
                page.add_script_tag(content="hostAdapter = {escapeHtml: value => String(value ?? '')};")
                result = page.evaluate("""() => getProcedureChecklistEntries({
                    auth_roles: ['Prepared by', 'Checked by', 'Approved by'],
                    signatories: [
                        {role_name: 'Prepared by', expected_signatory_name: 'Aslam'},
                        {role_name: 'Approved by', expected_signatory_name: 'Amjad'}
                    ],
                    procedure_rules_configured: true,
                    procedure_checks: [
                        {condition: 'prepared_by', selected_detail: 'Aslam'},
                        {condition: 'approved_by', selected_detail: 'Amjad'}
                    ]
                })""")
                self.assertEqual([entry["role"] for entry in result], ["Prepared by", "Approved by"])
                self.assertIn("Aslam", result[0]["label"])
                self.assertIn("Amjad", result[1]["label"])
                unmatched = page.evaluate("""() => getProcedureChecklistEntries({
                    auth_roles: ['Approved by'],
                    signatories: [{role_name: 'Approved by', expected_signatory_name: 'Amjad'}],
                    procedure_rules_configured: true,
                    procedure_checks: [{condition: 'approved_by', selected_detail: 'Someone else'}]
                })""")
                self.assertEqual(unmatched, [])
                role_only_signature = page.evaluate("""() => getProcedureChecklistEntries({
                    auth_roles: ['Approved by'],
                    signatories: [],
                    procedure_rules_configured: true,
                    procedure_checks: [{condition: 'signature', selected_detail: 'Approved by'}]
                })""")
                self.assertEqual([entry["role"] for entry in role_only_signature], ["Approved by"])
                display = page.evaluate("""() => {
                    const rule = {
                        name: 'Order to receipt', status: 'pass', matrix_rows: [{
                            operator: '>=', tolerance: 0, status: 'pass',
                            values: [{document_name: 'Order', field_name: 'Quantity', value: 10, linked: true},
                                     {document_name: 'Receipt', field_name: 'Quantity', value: 10, linked: true}]
                        }], chain: [], voucher_line_comparison: null
                    };
                    return {summary: summarizeCrossCheckRule(rule), detail: buildCrossCheckRuleDetail(rule)};
                }""")
                self.assertIn("Order to receipt passed", display["summary"]["text"])
                self.assertIn("Order · Quantity", display["detail"])
                self.assertIn("tolerance 0", display["detail"])
            finally:
                browser.close()

    def test_invalidated_inflight_modal_response_cannot_repopulate_cache(self):
        procedure_modal_js = Path(__file__).resolve().parents[1] / "static" / "procedure_modal.js"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.set_content("<html><body></body></html>")
                page.add_script_tag(content="""
                    var API_BASE = '';
                    window.ApiUtils = { apiFetch: null };
                """)
                page.add_script_tag(path=str(procedure_modal_js))
                result = page.evaluate("""async () => {
                    const responses = [];
                    window.ApiUtils.apiFetch = () => new Promise(resolve => responses.push(resolve));
                    const first = loadProcedureModalData(7, '');
                    invalidateProcedureModalData(7);
                    const second = loadProcedureModalData(7, '');
                    responses[0]({ok: true, json: async () => ({value: 'stale'})});
                    responses[1]({ok: true, json: async () => ({value: 'fresh'})});
                    const values = await Promise.all([first, second]);
                    const cached = await loadProcedureModalData(7, '');
                    return {values, cached, requestCount: responses.length};
                }""")
                self.assertEqual(result["requestCount"], 2)
                self.assertEqual(result["values"][0], {"value": "stale"})
                self.assertEqual(result["values"][1], {"value": "fresh"})
                self.assertEqual(result["cached"], {"value": "fresh"})
            finally:
                browser.close()

class FormDocumentSchemaTests(unittest.TestCase):
    """Phase A: generic Form document metadata and value storage contract."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        database.DB_NAME = os.path.join(self.tmpdir.name, "test.db")
        init_db()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _columns(self, table):
        conn = database.get_db()
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        finally:
            conn.close()

    def test_form_schema_has_typed_fields_and_repeatable_values(self):
        self.assertTrue({"field_type", "field_scope", "linked_column"}.issubset(
            self._columns("audit_rule_document_fields")
        ))
        self.assertEqual(
            self._columns("voucher_line_form_entries"),
            {"id", "voucher_line_id", "document_id", "row_scope", "sort_order",
             "created_at", "updated_at"},
        )
        self.assertEqual(
            self._columns("voucher_line_form_values"),
            {"id", "entry_id", "field_id", "value_text", "value_date", "value_numeric",
             "created_at", "updated_at"},
        )

    def test_form_document_type_is_allowed_by_schema(self):
        conn = database.get_db()
        try:
            conn.execute("INSERT INTO audit_rule_documents (name, document_type) VALUES (?, ?)",
                         ("Paper Vendor Bill", "form"))
            conn.commit()
            self.assertEqual(conn.execute(
                "SELECT document_type FROM audit_rule_documents WHERE name = ?",
                ("Paper Vendor Bill",)
            ).fetchone()[0], "form")
        finally:
            conn.close()

    def test_form_fields_normalize_order_and_defaults(self):
        result = normalize_form_fields("form", None, [
            {"field_name": "Bill No", "field_type": "text"},
            {"field_name": "Bill Date", "field_type": "date", "field_scope": "header"},
            {"field_name": "Quantity", "field_type": "numeric", "field_scope": "item"},
        ])
        self.assertEqual([field["sort_order"] for field in result], [0, 1, 2])
        self.assertEqual(result[0]["field_scope"], "header")
        self.assertEqual(result[2]["field_scope"], "item")

    def test_form_fields_reject_duplicate_names_and_invalid_types(self):
        with self.assertRaises(Exception):
            normalize_form_fields("form", None, [{"field_name": "Amount"}, {"field_name": " amount "}])
        with self.assertRaises(Exception):
            normalize_form_fields("form", None, [{"field_name": "Amount", "field_type": "currency"}])

    def test_document_update_omits_form_fields_without_requesting_a_clear(self):
        payload = DocumentUpdate(name="Paper Vendor Bill")
        self.assertIsNone(payload.form_fields)

    def test_form_field_save_round_trip_preserves_ordered_typed_mapping(self):
        fields = normalize_form_fields("form", None, [
            {"field_name": "Bill No", "field_type": "text", "field_scope": "header"},
            {"field_name": "Quantity", "field_type": "numeric", "field_scope": "item"},
        ])
        writes = []
        with patch("routers.audit_rules.execute_write_pg", side_effect=lambda sql, params: writes.append((sql, params))):
            from routers.audit_rules import save_document_form_fields
            save_document_form_fields(17, fields)
        inserts = [params for sql, params in writes if "INSERT INTO audit_rule_document_fields" in sql]
        self.assertEqual([params[2] for params in inserts], [0, 1])
        self.assertEqual([params[3] for params in inserts], ["text", "numeric"])
        self.assertEqual([params[4] for params in inserts], ["header", "item"])


class FormEvidenceContractTests(unittest.TestCase):
    def test_form_linked_template_does_not_use_verification_policy_guard(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "audit_rules.js").read_text(encoding="utf-8")
        self.assertIn("const usesVerificationPolicy = documentType === 'linked_template';", source)
        self.assertIn("if (arDocumentTypeSelect.value === 'form')", source)
        self.assertIn("arVerificationFieldsField.style.display = isFormDocument ? 'none' : '';", source)

    def test_postgres_document_type_migration_adds_form_to_an_old_constraint(self):
        cursor = Mock()
        cursor.fetchone.return_value = ("CHECK ((document_type = ANY (ARRAY['linked_template'::text, 'physical_bill'::text])))",)
        conn = Mock()
        conn.cursor.return_value = cursor

        self.assertTrue(database._ensure_postgres_audit_rule_document_type_constraint(conn))
        executed_sql = "\n".join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn("DROP CONSTRAINT audit_rule_documents_document_type_check", executed_sql)
        self.assertIn("'form'", executed_sql)
        conn.rollback.assert_not_called()

    def test_postgres_document_type_migration_is_idempotent_when_form_is_allowed(self):
        cursor = Mock()
        cursor.fetchone.return_value = ("CHECK ((document_type = ANY (ARRAY['linked_template'::text, 'form'::text, 'physical_bill'::text])))",)
        conn = Mock()
        conn.cursor.return_value = cursor

        self.assertFalse(database._ensure_postgres_audit_rule_document_type_constraint(conn))
        cursor.execute.assert_called_once()
        conn.rollback.assert_not_called()

    def test_form_values_are_normalized_to_the_declared_types(self):
        self.assertEqual(_form_value_for_storage('text', '  Bill  ', 'Bill No'), ('Bill', None, None))
        text, date_value, numeric = _form_value_for_storage('date', '2026-09-05', 'Bill Date')
        self.assertEqual(date_value.isoformat(), '2026-09-05')
        self.assertIsNone(text)
        self.assertIsNone(numeric)
        self.assertEqual(_form_value_for_storage('numeric', '1200.5', 'Quantity')[2], 1200.5)

    def test_form_values_are_shaped_back_as_text_date_and_numeric(self):
        rows = [
            {'entry_id': 9, 'voucher_line_id': 4, 'document_id': 7, 'row_scope': 'header', 'sort_order': 0,
             'field_id': 1, 'field_name': 'Bill No', 'field_type': 'text', 'value_text': 'PB-1',
             'value_date': None, 'value_numeric': None},
            {'entry_id': 10, 'voucher_line_id': 4, 'document_id': 7, 'row_scope': 'item', 'sort_order': 0,
             'field_id': 2, 'field_name': 'Bill Date', 'field_type': 'date', 'value_text': None,
             'value_date': date(2026, 9, 5), 'value_numeric': None},
            {'entry_id': 10, 'voucher_line_id': 4, 'document_id': 7, 'row_scope': 'item', 'sort_order': 0,
             'field_id': 3, 'field_name': 'Quantity', 'field_type': 'numeric', 'value_text': None,
             'value_date': None, 'value_numeric': 3},
        ]
        result = _shape_form_entries(rows)
        self.assertEqual(result[0]['values'][0]['value'], 'PB-1')
        self.assertEqual(result[1]['values'][0]['value'], '2026-09-05')
        self.assertEqual(result[1]['values'][1]['value'], 3.0)

    def test_form_replace_rejects_a_field_from_another_document(self):
        responses = [
            [{'id': 4}], [{'id': 7}],
            [{'id': 11, 'field_name': 'Bill No', 'field_type': 'text', 'field_scope': 'header'}],
        ]
        with patch('routers.verification.execute_query_pg', side_effect=responses):
            with self.assertRaises(Exception) as raised:
                _replace_form_entries(4, 7, [{'row_scope': 'header', 'values': [{'field_id': 99, 'value': 'x'}]}])
        self.assertEqual(raised.exception.status_code, 400)

    def test_form_replace_clears_with_an_empty_entry_list(self):
        responses = [[{'id': 4}], [{'id': 7}], []]
        writes = []
        with patch('routers.verification.execute_query_pg', side_effect=responses), \
             patch('routers.verification.execute_write_pg', side_effect=lambda sql, params=None: writes.append((sql, params))):
            self.assertEqual(_replace_form_entries(4, 7, []), 0)
        self.assertIn('DELETE FROM voucher_line_form_entries', writes[0][0])


if __name__ == "__main__":
    unittest.main()
