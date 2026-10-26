"""Focused coverage for the Account Control Centre foundations."""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import database
from routers import account_control_center as control_center


class AccountSetSchemaTests(unittest.TestCase):
    def test_sqlite_initialisation_creates_account_set_tables_and_indexes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_db_name = database.DB_NAME
            database.DB_NAME = os.path.join(temp_dir, "account_sets.db")
            try:
                database.init_db()
                with sqlite3.connect(database.DB_NAME) as conn:
                    tables = {
                        row[0]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        ).fetchall()
                    }
                    indexes = {
                        row[0]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'index'"
                        ).fetchall()
                    }
                conn.close()
                self.assertTrue(
                    {
                        "account_sets",
                        "account_set_versions",
                        "account_set_selectors",
                        "account_control_categories",
                        "account_control_assignments",
                        "account_control_cashflow_categories",
                        "account_control_cashflow_category_assignments",
                        "account_control_cashflow_treasury_accounts",
                        "account_control_cashflow_settings",
                        "account_control_cashflow_config_versions",
                        "account_control_cashflow_config_entries",
                    }.issubset(tables)
                )
                self.assertTrue(
                    {
                        "idx_account_sets_client",
                        "idx_account_set_versions_set",
                        "idx_account_set_versions_upload",
                        "idx_account_set_selectors_version",
                        "idx_account_control_categories_client_type",
                        "idx_account_control_assignments_client",
                        "idx_account_control_assignments_workstream",
                        "idx_account_control_cashflow_categories_client",
                        "idx_account_control_cashflow_category_assignments_client",
                        "idx_account_control_cashflow_treasury_client",
                        "idx_account_control_cashflow_config_versions_client",
                        "idx_account_control_cashflow_config_entries_version",
                    }.issubset(indexes)
                )
            finally:
                database.DB_NAME = original_db_name



class AccountSetPostgresMigrationTests(unittest.TestCase):
    def test_existing_postgres_upgrade_creates_all_account_set_tables_and_indexes(self):
        class Cursor:
            def __init__(self):
                self.statements = []
                self.closed = False

            def execute(self, statement):
                self.statements.append(statement)

            def close(self):
                self.closed = True

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()
                self.rollback_called = False

            def cursor(self):
                return self.cursor_instance

            def rollback(self):
                self.rollback_called = True

        connection = Connection()
        database._ensure_postgres_account_set_tables(connection)
        sql = "\n".join(connection.cursor_instance.statements)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_control_categories", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS statement_section", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS row_type", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS parent_category_id", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_control_assignments", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_sets", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_set_versions", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_set_selectors", sql)
        self.assertIn("idx_account_set_selectors_version", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_control_cashflow_categories", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_control_cashflow_category_assignments", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_control_cashflow_treasury_accounts", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_control_cashflow_settings", sql)
        self.assertIn("idx_account_control_cashflow_categories_client", sql)
        self.assertIn("idx_account_control_cashflow_category_assignments_client", sql)
        self.assertIn("idx_account_control_cashflow_treasury_client", sql)
        self.assertIn("account_control_cashflow_config_versions", sql)
        self.assertIn("account_control_cashflow_config_entries", sql)
        self.assertIn("is_active BOOLEAN NOT NULL DEFAULT TRUE", sql)
        self.assertTrue(connection.cursor_instance.closed)
        self.assertFalse(connection.rollback_called)
class AccountSetResolverTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [
            {"id": 1, "account_code": "20", "account_name": "Liabilities", "classification": "parent", "level": 1},
            {"id": 2, "account_code": "20-15-50", "account_name": "Local creditors", "classification": "parent", "level": 3},
            {"id": 3, "account_code": "20-15-50-0001", "account_name": "Local supplier", "classification": "leaf", "level": 4},
            {"id": 4, "account_code": "20-15-50-10", "account_name": "Excluded branch", "classification": "parent", "level": 4},
            {"id": 5, "account_code": "20-15-50-10-0001", "account_name": "Excluded supplier", "classification": "leaf", "level": 5},
            {"id": 6, "account_code": "20-15-500-0001", "account_name": "Different hierarchy", "classification": "leaf", "level": 4},
        ]

    def test_subtree_selection_uses_dash_boundaries_and_exclusion_wins(self):
        selectors = control_center._clean_selectors(
            [
                control_center.AccountSetSelectorPayload(selector_type="include_subtree", account_code="20-15-50"),
                control_center.AccountSetSelectorPayload(selector_type="exclude_subtree", account_code="20-15-50-10"),
            ]
        )
        resolved = control_center._resolve_leaf_accounts(self.accounts, selectors)
        self.assertEqual([row["account_code"] for row in resolved], ["20-15-50-0001"])
        self.assertEqual(resolved[0]["matched_by"], [{"selector_type": "include_subtree", "account_code": "20-15-50"}])

    def test_exact_selection_only_accepts_the_selected_leaf(self):
        selectors = control_center._clean_selectors(
            [
                control_center.AccountSetSelectorPayload(selector_type="include_exact", account_code="20-15-500-0001"),
            ]
        )
        resolved = control_center._resolve_leaf_accounts(self.accounts, selectors)
        self.assertEqual([row["account_code"] for row in resolved], ["20-15-500-0001"])

    def test_duplicate_selector_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            control_center._clean_selectors(
                [
                    control_center.AccountSetSelectorPayload(selector_type="include_subtree", account_code="20-15-50"),
                    control_center.AccountSetSelectorPayload(selector_type="include_subtree", account_code="20-15-50"),
                ]
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_published_version_cannot_be_edited(self):
        payload = control_center.AccountSetDraftUpdatePayload(
            display_name="Local vendors",
            selectors=[],
        )
        with patch.object(
            control_center,
            "_get_version",
            return_value={"status": "published", "client_id": 1, "source_upload_id": 1, "account_set_id": 1},
        ):
            with self.assertRaises(HTTPException) as raised:
                control_center.update_account_set_draft(44, payload, session={"username": "Admin"})
        self.assertEqual(raised.exception.status_code, 409)

    def test_publish_requires_a_draft_version(self):
        with patch.object(
            control_center,
            "_get_version",
            return_value={"status": "published", "client_id": 1, "source_upload_id": 1, "account_set_id": 1},
        ):
            with self.assertRaises(HTTPException) as raised:
                control_center.publish_account_set_version(44, session={"username": "Admin"})
        self.assertEqual(raised.exception.status_code, 409)


class SimplifiedAccountControlTests(unittest.TestCase):
    def test_workstream_payload_is_normalised_and_invalid_values_are_rejected(self):
        payload = control_center.AccountControlAssignmentPayload(
            client_id=1,
            account_code=" 20-15-50-0001 ",
            verification_workstream=" Local_Vendor ",
        )
        cleaned = control_center._clean_control_assignment(payload)
        self.assertEqual(cleaned["account_code"], "20-15-50-0001")
        self.assertEqual(cleaned["verification_workstream"], "local_vendor")

        invalid = control_center.AccountControlAssignmentPayload(
            client_id=1, account_code="20-15-50-0001", verification_workstream="sales"
        )
        with self.assertRaises(HTTPException) as raised:
            control_center._clean_control_assignment(invalid)
        self.assertEqual(raised.exception.status_code, 400)

    def test_workstream_can_be_cleared_without_creating_a_second_assignment(self):
        payload = control_center.AccountControlAssignmentPayload(
            client_id=1, account_code="10-15-115-10-0001", verification_workstream=None
        )
        cleaned = control_center._clean_control_assignment(payload)
        self.assertIsNone(cleaned["verification_workstream"])

    def test_parent_bulk_payload_keeps_parent_code_separate(self):
        payload = control_center.AccountControlBulkAssignmentPayload(
            client_id=1,
            parent_account_code=" 20-15-50 ",
            verification_workstream="local_vendor",
        )
        self.assertEqual(control_center._clean_control_account_code(payload.parent_account_code), "20-15-50")
        self.assertEqual(payload.verification_workstream, "local_vendor")



class CashFlowControlTests(unittest.TestCase):
    def test_cashflow_setup_includes_read_only_historical_orphans(self):
        accounts = [{
            "id": 1,
            "account_code": "60-10-70",
            "account_name": "Vehicles Running And Maintenance - Admin",
            "parent_account_code": None,
            "level": 3,
            "classification": "parent",
        }]
        with patch.object(control_center, "_latest_control_accounts", return_value=({"id": 9}, accounts)), patch.object(
            control_center,
            "discover_historical_orphan_accounts_postgres",
            return_value=[{"account_code": "60-10-70-0007", "is_read_only": True}],
        ), patch.object(control_center, "execute_query_pg", side_effect=[
            [],
            [{"account_code": "60-10-70-0007", "category_id": 4}],
            [],
            [],
            [],
        ]):
            result = control_center.get_cashflow_setup(1, session={"username": "Admin"})

        self.assertEqual(result["historical_orphans"][0]["account_code"], "60-10-70-0007")
        self.assertEqual(result["historical_orphans"][0]["category_id"], 4)
    def test_category_payload_normalises_key_and_label(self):
        cleaned = control_center._clean_cashflow_category_payload(
            control_center.CashFlowCategoryPayload(client_id=1, category_key=" Customer Receipts ", label="  Customer receipts  ")
        )
        self.assertEqual(cleaned["category_key"], "customer_receipts")
        self.assertEqual(cleaned["label"], "Customer receipts")

    def test_historical_orphan_category_assignment_is_allowed_for_exact_code(self):
        payload = control_center.CashFlowCategoryAssignmentPayload(
            client_id=1, account_code="60-10-70-0007", category_id=4
        )
        accounts = [{"account_code": "60-10-70", "classification": "parent"}]
        with patch.object(control_center, "_latest_control_accounts", return_value=({}, accounts)), patch.object(
            control_center,
            "discover_historical_orphan_accounts_postgres",
            return_value=[{"account_code": "60-10-70-0007", "is_historical_orphan": True}],
        ), patch.object(control_center, "_validate_cashflow_category"), patch.object(
            control_center, "_upsert_cashflow_category_assignment"
        ) as upsert:
            result = control_center.save_cashflow_category_assignment(payload, session={"username": "Admin"})

        self.assertEqual(result["classification_scope"], "historical_orphan")
        upsert.assert_called_once()

    def test_parent_category_assignment_includes_historical_orphans_under_surviving_parent(self):
        payload = control_center.CashFlowCategoryBulkAssignmentPayload(
            client_id=1, parent_account_code="60-10-70", category_id=4
        )
        accounts = [
            {"account_code": "60-10-70", "classification": "parent"},
            {"account_code": "60-10-70-0001", "classification": "leaf"},
        ]
        with patch.object(control_center, "_latest_control_accounts", return_value=({}, accounts)), patch.object(
            control_center,
            "discover_historical_orphan_accounts_postgres",
            return_value=[{"account_code": "60-10-70-0007", "parent_account_code": "60-10-70", "is_historical_orphan": True}],
        ), patch.object(control_center, "_validate_cashflow_category"), patch.object(
            control_center, "_upsert_cashflow_category_assignment"
        ) as upsert, patch.object(control_center, "_refresh_cashflow_snapshot"), patch.object(
            control_center, "pg_transaction"
        ):
            result = control_center.apply_cashflow_category_to_children(payload, session={"username": "Admin"})

        saved_codes = {call.args[0]["account_code"] for call in upsert.call_args_list}
        self.assertEqual(saved_codes, {"60-10-70-0001", "60-10-70-0007"})
        self.assertEqual(result["leaf_count"], 2)
        self.assertEqual(result["historical_orphan_count"], 1)
    def test_category_assignment_payload_cleans_account_code_and_allows_clear(self):
        cleaned = control_center._clean_cashflow_category_assignment(
            control_center.CashFlowCategoryAssignmentPayload(client_id=1, account_code=" 10-15-0001 ", category_id=None)
        )
        self.assertEqual(cleaned, {"client_id": 1, "account_code": "10-15-0001", "category_id": None})

    def test_settings_accept_only_net_or_gross_at_model_boundary(self):
        self.assertEqual(control_center.CashFlowSettingsPayload(client_id=1, receipt_presentation="gross").receipt_presentation, "gross")

    def test_unknown_category_assignment_code_is_rejected(self):
        payload = control_center.CashFlowCategoryAssignmentPayload(
            client_id=1, account_code="99-99-9999", category_id=4
        )
        with patch.object(control_center, "_latest_control_accounts", return_value=({}, [])), patch.object(
            control_center,
            "discover_historical_orphan_accounts_postgres",
            return_value=[],
        ), patch.object(control_center, "_validate_cashflow_category"):
            with self.assertRaises(HTTPException) as raised:
                control_center.save_cashflow_category_assignment(payload, session={"username": "Admin"})

        self.assertEqual(raised.exception.status_code, 400)
    def test_category_assignment_upsert_writes_new_assignment_table(self):
        with patch.object(control_center, "execute_write_pg") as write:
            control_center._upsert_cashflow_category_assignment(
                {"client_id": 1, "account_code": "CASH", "category_id": 3}, "Admin"
            )
        sql = write.call_args.args[0]
        self.assertIn("account_control_cashflow_category_assignments", sql)
        self.assertIn("category_id", sql)

    def test_treasury_payload_rejects_duplicate_cash_and_bank_account(self):
        payload = control_center.CashFlowTreasuryAccountsPayload(
            client_id=1, cash_account_codes=["CASH"], bank_account_codes=["CASH"]
        )
        with patch.object(control_center, "_latest_control_accounts", return_value=({}, [{"account_code": "CASH", "classification": "leaf"}])), self.assertRaises(HTTPException) as raised:
            control_center.save_cashflow_treasury_accounts(payload, session={"username": "Admin"})
        self.assertEqual(raised.exception.status_code, 400)

    def test_treasury_change_codes_detects_added_and_removed_accounts(self):
        with patch.object(control_center, "execute_query_pg", return_value=[
            {"account_code": "CASH", "treasury_type": "cash"},
            {"account_code": "BANK", "treasury_type": "bank"},
        ]):
            changed = control_center._cashflow_treasury_change_codes(
                1,
                {"BANK"},
                set(),
            )
        self.assertEqual(changed, ["BANK", "CASH"])

    def test_treasury_change_codes_returns_empty_for_unchanged_accounts(self):
        with patch.object(control_center, "execute_query_pg", return_value=[
            {"account_code": "CASH", "treasury_type": "cash"},
        ]):
            changed = control_center._cashflow_treasury_change_codes(
                1,
                {"CASH"},
                set(),
            )
        self.assertEqual(changed, [])


class FinancialStatementControlTests(unittest.TestCase):
    def test_statement_outline_payload_keeps_section_kind_and_parent(self):
        category = control_center._clean_category_payload(
            control_center.AccountControlCategoryPayload(
                client_id=1, category_key="local_sales", label="Local sales",
                statement_section="income_statement", parent_category_id=9,
            )
        )
        self.assertEqual(category["statement_section"], "income_statement")
        self.assertEqual(category["row_type"], "category")
        self.assertEqual(category["parent_category_id"], 9)

    def test_statement_heading_cannot_be_assigned_to_account(self):
        assignment = {"financial_statement_category_id": 4, "note_category_id": None}
        with patch.object(control_center, "execute_query_pg", return_value=[{"id": 4, "row_type": "heading"}]):
            with self.assertRaises(HTTPException) as raised:
                control_center._validate_control_categories(1, assignment)
        self.assertEqual(raised.exception.status_code, 400)

    def test_tab_payloads_track_only_the_fields_they_supply(self):
        verification = control_center._clean_control_assignment(
            control_center.AccountControlAssignmentPayload(
                client_id=1, account_code="10-15-115-10-0001", verification_workstream="cash"
            )
        )
        financial = control_center._clean_control_assignment(
            control_center.AccountControlAssignmentPayload(
                client_id=1, account_code="10-15-115-10-0001", financial_statement_category_id=7
            )
        )
        note = control_center._clean_control_assignment(
            control_center.AccountControlAssignmentPayload(
                client_id=1, account_code="10-15-115-10-0001", note_category_id=9
            )
        )
        self.assertEqual(verification["_provided_fields"], {"verification_workstream"})
        self.assertEqual(financial["_provided_fields"], {"financial_statement_category_id"})
        self.assertEqual(note["_provided_fields"], {"note_category_id"})

    def test_upsert_for_financial_tab_does_not_update_verification_column(self):
        assignment = control_center._clean_control_assignment(
            control_center.AccountControlAssignmentPayload(
                client_id=1, account_code="10-15-115-10-0001", financial_statement_category_id=7
            )
        )
        with patch.object(control_center, "execute_write_pg") as write:
            control_center._upsert_control_assignment(assignment, "Admin")
        sql = write.call_args.args[0]
        self.assertIn("financial_statement_category_id", sql)
        self.assertNotIn("verification_workstream = EXCLUDED", sql)

    def test_category_payload_normalises_key_and_label(self):
        category = control_center._clean_category_payload(
            control_center.AccountControlCategoryPayload(
                client_id=1, category_key=" Current Assets ", label="  Current   assets "
            )
        )
        self.assertEqual(category["category_key"], "current_assets")
        self.assertEqual(category["label"], "Current assets")
        self.assertEqual(category["category_type"], "financial_statement")

    def test_create_category_rejects_duplicate_key(self):
        payload = control_center.AccountControlCategoryPayload(
            client_id=1, category_key="current_assets", label="Current assets"
        )
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "execute_query_pg", return_value=[{"id": 5}]
        ), patch.object(control_center, "execute_write_pg") as write:
            with self.assertRaises(HTTPException) as raised:
                control_center.create_account_control_category(payload, session={"username": "Admin"})
        self.assertEqual(raised.exception.status_code, 409)
        write.assert_not_called()

    def test_update_category_supports_label_order_and_active_state(self):
        payload = control_center.AccountControlCategoryUpdatePayload(
            client_id=1, category_type="financial_statement", label="Current assets", sort_order=3, is_active=False
        )
        with patch.object(control_center, "execute_query_pg", return_value=[{"id": 7}]), patch.object(
            control_center, "execute_write_pg"
        ) as write:
            result = control_center.update_account_control_category(7, payload, session={"username": "Admin"})
        self.assertTrue(result["saved"])
        self.assertEqual(result["category"]["sort_order"], 3)
        self.assertFalse(result["category"]["is_active"])
        self.assertIn("sort_order", write.call_args.args[0])

    def test_list_categories_can_include_inactive_rows(self):
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "execute_query_pg", return_value=[
                {"id": 1, "client_id": 1, "category_type": "financial_statement", "category_key": "assets", "label": "Assets", "sort_order": 0, "is_active": True},
            ]
        ):
            result = control_center.list_account_control_categories(
                client_id=1, category_type="financial_statement", include_inactive=True, session={"username": "Admin"}
            )
        self.assertEqual(result["categories"][0]["category_key"], "assets")


if __name__ == "__main__":
    unittest.main()
