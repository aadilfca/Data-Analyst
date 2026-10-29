"""Focused coverage for the Account Control Centre foundations."""

import os
import sqlite3
import tempfile
import json
import unittest
from contextlib import nullcontext
from decimal import Decimal
from unittest.mock import patch
from io import BytesIO

import pandas as pd

from fastapi import HTTPException

import database
from routers import account_control_center as control_center
from routers import trial_balance


class TrialBalanceHierarchyTests(unittest.TestCase):
    def test_numeric_zero_padding_does_not_hide_a_parent(self):
        codes = {"1", "01-0001", "01-0001-0002", "01-0001-0002-0006"}

        hierarchy = trial_balance.build_account_code_hierarchy(codes)

        self.assertEqual(hierarchy["classification"]["1"], "parent")
        self.assertEqual(hierarchy["classification"]["01-0001"], "parent")
        self.assertEqual(hierarchy["classification"]["01-0001-0002"], "parent")
        self.assertEqual(hierarchy["classification"]["01-0001-0002-0006"], "leaf")
        self.assertEqual(hierarchy["parent_account_code"]["01-0001"], "1")
        self.assertEqual(hierarchy["parent_account_code"]["01-0001-0002"], "01-0001")

    def test_exact_parent_wins_when_normalized_codes_collide(self):
        hierarchy = trial_balance.build_account_code_hierarchy({"1", "01", "01-0001"})

        self.assertEqual(hierarchy["parent_account_code"]["01-0001"], "01")
        self.assertEqual(hierarchy["classification"]["01"], "parent")
        self.assertEqual(hierarchy["classification"]["1"], "leaf")

    def test_account_control_tree_refreshes_old_stored_classification(self):
        stored_accounts = [
            {"id": 1, "account_code": "1", "account_name": "Assets", "parent_account_code": None, "level": 1, "classification": "leaf", "manually_overridden": False, "sort_order": 1},
            {"id": 2, "account_code": "01-0001", "account_name": "Current assets", "parent_account_code": "01", "level": 2, "classification": "leaf", "manually_overridden": False, "sort_order": 2},
            {"id": 3, "account_code": "01-0001-0001", "account_name": "Cash", "parent_account_code": "01-0001", "level": 3, "classification": "leaf", "manually_overridden": False, "sort_order": 3},
            {"id": 4, "account_code": "2", "account_name": "Liabilities", "parent_account_code": None, "level": 1, "classification": "leaf", "manually_overridden": True, "sort_order": 4},
        ]

        with patch.object(control_center, "execute_query_pg", return_value=stored_accounts):
            accounts = control_center._accounts_for_upload(77)

        self.assertEqual(accounts[0]["classification"], "parent")
        self.assertEqual(accounts[1]["parent_account_code"], "1")
        self.assertEqual(accounts[1]["classification"], "parent")
        self.assertEqual(accounts[3]["classification"], "leaf")

    def test_financial_statement_parent_assignment_reaches_padded_descendants(self):
        payload = control_center.AccountControlBulkAssignmentPayload(
            client_id=1,
            parent_account_code="1",
            financial_statement_category_id=41,
        )
        accounts = [
            {"id": 1, "account_code": "1", "classification": "parent"},
            {"id": 2, "account_code": "01-0001", "classification": "parent"},
            {"id": 3, "account_code": "01-0001-0002", "classification": "leaf"},
            {"id": 4, "account_code": "01-0001-0002-0006", "classification": "leaf"},
            {"id": 5, "account_code": "10-0001", "classification": "leaf"},
        ]
        with patch.object(control_center, "_selected_control_accounts", return_value=({"id": 77}, accounts)), patch.object(
            control_center, "_validate_control_categories"
        ), patch.object(control_center, "_upsert_control_assignment") as upsert, patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ):
            result = control_center.apply_account_control_mapping_to_children(payload, session={"username": "Admin"})

        self.assertEqual(result["leaf_count"], 2)
        self.assertEqual(
            {call.args[0]["account_code"] for call in upsert.call_args_list},
            {"01-0001-0002", "01-0001-0002-0006"},
        )


class TrialBalanceMappedColumnsTests(unittest.TestCase):
    def test_unmapped_source_columns_are_not_retained_in_imported_raw_row(self):
        row = {
            "Account Code": "1",
            "Account Name": "ASSETS",
            "Opening Debit": "148701915",
            "Opening Credit": "64486535",
            "Period Debit": "1721855708",
            "Period Credit": "1664962705",
            "Closing Debit": "0",
            "Closing Credit": "0",
            "Start Date": "2025-07-01 00:00:00",
            "End Date ": "2026-06-30 00:00:00",
            "Unnamed: 10": "",
        }
        mapping = {
            "account_code_column": "Account Code",
            "account_name_column": "Account Name",
            "opening_dr_column": "Opening Debit",
            "opening_cr_column": "Opening Credit",
            "period_dr_column": "Period Debit",
            "period_cr_column": "Period Credit",
            "closing_dr_column": "Closing Debit",
            "closing_cr_column": "Closing Credit",
            "type_column": None,
        }

        imported_raw_row = trial_balance._mapped_raw_row_data(row, mapping)

        self.assertEqual(imported_raw_row, {header: value for header, value in row.items() if header in mapping.values()})
        self.assertNotIn("Start Date", imported_raw_row)
        self.assertNotIn("End Date ", imported_raw_row)
        self.assertNotIn("Unnamed: 10", imported_raw_row)

    def test_optional_mapped_column_is_retained_with_its_exact_header(self):
        row = {"Account Code": "1", "Account Name": "ASSETS", "End Date ": "2026-06-30"}
        mapping = {
            "account_code_column": "Account Code",
            "account_name_column": "Account Name",
            "optional_period_column": "End Date ",
            "unused_column": None,
        }

        self.assertEqual(
            trial_balance._mapped_raw_row_data(row, mapping),
            {"Account Code": "1", "Account Name": "ASSETS", "End Date ": "2026-06-30"},
        )


class TrialBalanceRollforwardAdjustmentTests(unittest.TestCase):
    def test_debit_shortfall_is_recorded_on_closing_debit_side(self):
        result = trial_balance.calculate_rollforward_adjustment({
            "opening_dr_column": Decimal("100"), "opening_cr_column": Decimal("0"),
            "period_dr_column": Decimal("20"), "period_cr_column": Decimal("0"),
            "closing_dr_column": Decimal("110"), "closing_cr_column": Decimal("0"),
        })

        self.assertEqual(result["difference_net"], Decimal("10"))
        self.assertEqual(result["closing_dr_adjustment"], Decimal("10"))
        self.assertEqual(result["closing_cr_adjustment"], Decimal("0"))

    def test_credit_shortfall_is_recorded_on_closing_credit_side(self):
        result = trial_balance.calculate_rollforward_adjustment({
            "opening_dr_column": Decimal("0"), "opening_cr_column": Decimal("100"),
            "period_dr_column": Decimal("0"), "period_cr_column": Decimal("20"),
            "closing_dr_column": Decimal("0"), "closing_cr_column": Decimal("110"),
        })

        self.assertEqual(result["difference_net"], Decimal("-10"))
        self.assertEqual(result["closing_dr_adjustment"], Decimal("0"))
        self.assertEqual(result["closing_cr_adjustment"], Decimal("10"))

    def test_difference_within_tolerance_does_not_create_adjustment(self):
        amounts = {
            "opening_dr_column": Decimal("100"), "opening_cr_column": Decimal("0"),
            "period_dr_column": Decimal("0"), "period_cr_column": Decimal("0"),
            "closing_dr_column": Decimal("99.995"), "closing_cr_column": Decimal("0"),
        }

        self.assertIsNone(trial_balance.calculate_rollforward_adjustment(amounts))

    def test_row_differences_are_warnings_and_do_not_replace_source_amounts(self):
        frame = pd.DataFrame([
            {"Code": "1", "Name": "Assets parent", "Opening Dr": 999, "Opening Cr": 0, "Period Dr": 0, "Period Cr": 0, "Closing Dr": 0, "Closing Cr": 0},
            {"Code": "1-01", "Name": "Debit leaf", "Opening Dr": 100, "Opening Cr": 0, "Period Dr": 0, "Period Cr": 0, "Closing Dr": 90, "Closing Cr": 0},
            {"Code": "B1", "Name": "Credit leaf", "Opening Dr": 0, "Opening Cr": 100, "Period Dr": 0, "Period Cr": 0, "Closing Dr": 0, "Closing Cr": 90},
        ])
        workbook = BytesIO()
        frame.to_excel(workbook, index=False)
        mapping = {
            "account_code_column": "Code", "account_name_column": "Name", "type_column": None,
            "opening_dr_column": "Opening Dr", "opening_cr_column": "Opening Cr",
            "period_dr_column": "Period Dr", "period_cr_column": "Period Cr",
            "closing_dr_column": "Closing Dr", "closing_cr_column": "Closing Cr",
        }

        result = trial_balance.validate_trial_balance_contents(workbook.getvalue(), "tb.xlsx", mapping, no_total_row=True)

        self.assertTrue(result["valid"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["rollforward_difference_count"], 2)
        self.assertEqual([item["severity"] for item in result["warnings"]], ["warning", "warning"])
        self.assertEqual(
            {item["account_code"] for item in result["rollforward_differences"]},
            {"1-01", "B1"},
        )
        self.assertEqual(result["rollforward_differences"][0]["closing_dr_adjustment"], 10.0)
        self.assertEqual(result["rollforward_differences"][1]["closing_cr_adjustment"], 10.0)
        self.assertEqual(next(c for c in result["checks"] if c["key"] == "roll_forward")["status"], "adjusted")

    def test_whole_file_footing_difference_still_blocks_a_row_adjustment(self):
        frame = pd.DataFrame([{
            "Code": "A1", "Name": "Cash", "Opening Dr": 100, "Opening Cr": 0,
            "Period Dr": 0, "Period Cr": 0, "Closing Dr": 90, "Closing Cr": 0,
        }])
        workbook = BytesIO()
        frame.to_excel(workbook, index=False)
        mapping = {
            "account_code_column": "Code", "account_name_column": "Name", "type_column": None,
            "opening_dr_column": "Opening Dr", "opening_cr_column": "Opening Cr",
            "period_dr_column": "Period Dr", "period_cr_column": "Period Cr",
            "closing_dr_column": "Closing Dr", "closing_cr_column": "Closing Cr",
        }

        result = trial_balance.validate_trial_balance_contents(workbook.getvalue(), "tb.xlsx", mapping, no_total_row=True)

        self.assertFalse(result["valid"])
        self.assertEqual(result["rollforward_difference_count"], 1)
        self.assertTrue(any(issue["excel_row"] is None for issue in result["errors"]))

    def test_saved_adjustments_are_returned_separately_from_source_amounts(self):
        account_row = {
            "id": 10, "account_code": "A1", "source_account_code": "A1", "account_name": "Cash",
            "level": 1, "classification": "leaf", "manually_overridden": False,
            "opening_dr": 100, "opening_cr": 0, "period_dr": 20, "period_cr": 0,
            "closing_dr": 110, "closing_cr": 0,
        }
        adjustment_row = {
            "account_id": 10, "account_code": "A1", "account_name": "Cash", "source_excel_row": 2,
            "expected_closing_net": 120, "original_closing_net": 110, "difference_net": 10,
            "closing_dr_adjustment": 10, "closing_cr_adjustment": 0,
        }
        with patch.object(trial_balance, "_require_upload"), patch.object(
            trial_balance, "execute_query_pg", side_effect=[[account_row], [adjustment_row]]
        ):
            result = trial_balance.list_trial_balance_accounts(77)

        self.assertEqual(result["accounts"][0]["closing_dr"], 110)
        self.assertEqual(result["adjustments"][0]["closing_dr_adjustment"], 10)
        self.assertEqual(result["adjustments"][0]["account_id"], 10)


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
                    upload_columns = {row[1] for row in conn.execute("PRAGMA table_info(trial_balance_uploads)").fetchall()}
                    category_columns = {row[1] for row in conn.execute("PRAGMA table_info(account_control_categories)").fetchall()}
                conn.close()
                self.assertIn("validation_controls", upload_columns)
                self.assertTrue(
                    {
                        "account_sets",
                        "account_set_versions",
                        "account_set_selectors",
                        "account_control_categories",
                        "account_control_statement_calculations",
                        "account_control_statement_calculation_components",
                        "account_control_assignments",
                        "account_control_cashflow_categories",
                        "account_control_cashflow_category_assignments",
                        "account_control_cashflow_treasury_accounts",
                        "account_control_cashflow_settings",
                        "account_control_cashflow_config_versions",
                        "account_control_cashflow_config_entries",
                        "trial_balance_rollforward_adjustments",
                    }.issubset(tables)
                )
                self.assertIn("source_statement_category_id", category_columns)
                self.assertTrue(
                    {
                        "idx_account_sets_client",
                        "idx_account_set_versions_set",
                        "idx_account_set_versions_upload",
                        "idx_account_set_selectors_version",
                        "idx_account_control_categories_client_type",
                        "idx_statement_subtotal_heading",
                        "idx_account_control_assignments_client",
                        "idx_account_control_assignments_workstream",
                        "idx_account_control_cashflow_categories_client",
                        "idx_account_control_cashflow_category_assignments_client",
                        "idx_account_control_cashflow_treasury_client",
                        "idx_account_control_cashflow_config_versions_client",
                        "idx_account_control_cashflow_config_entries_version",
                        "idx_trial_balance_rollforward_adjustments_upload",
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
        self.assertIn("CREATE TABLE IF NOT EXISTS account_control_statement_calculations", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_control_statement_calculation_components", sql)
        self.assertIn("idx_statement_subtotal_heading", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS statement_section", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS row_type", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS parent_category_id", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS source_statement_category_id", sql)
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

    def test_postgres_rollforward_adjustment_migration_creates_table_and_index(self):
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
        database._ensure_postgres_trial_balance_rollforward_adjustments(connection)
        sql = "\n".join(connection.cursor_instance.statements)

        self.assertIn("CREATE TABLE IF NOT EXISTS trial_balance_rollforward_adjustments", sql)
        self.assertIn("ALTER TABLE trial_balance_uploads ADD COLUMN IF NOT EXISTS validation_controls TEXT", sql)
        self.assertIn("closing_dr_adjustment NUMERIC NOT NULL DEFAULT 0", sql)
        self.assertIn("closing_cr_adjustment NUMERIC NOT NULL DEFAULT 0", sql)
        self.assertIn("idx_trial_balance_rollforward_adjustments_upload", sql)
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
            [],
        ]):
            result = control_center.get_cashflow_setup(1, trial_balance_upload_id=None, session={"username": "Admin"})

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
    def test_note_category_can_link_to_one_active_statement_category(self):
        category = control_center._clean_category_payload(
            control_center.AccountControlCategoryPayload(
                client_id=1, category_type="note", category_key="statement_12", label="Inventory",
                source_statement_category_id=12,
            )
        )
        with patch.object(control_center, "execute_query_pg", side_effect=[
            [{"row_type": "category", "is_active": True, "statement_section": "balance_sheet"}], [],
        ]) as query:
            control_center._validate_statement_parent(category)
        self.assertEqual(query.call_args_list[0].args[1], (12, 1))

    def test_note_category_rejects_duplicate_statement_link(self):
        category = {"category_type": "note", "client_id": 1, "parent_category_id": None, "source_statement_category_id": 12, "statement_section": None}
        with patch.object(control_center, "execute_query_pg", side_effect=[
            [{"row_type": "category", "is_active": True, "statement_section": "balance_sheet"}], [{"id": 30}],
        ]):
            with self.assertRaises(HTTPException) as raised:
                control_center._validate_statement_parent(category)
        self.assertEqual(raised.exception.status_code, 409)

    def test_move_linked_note_within_same_statement_preserves_assignments(self):
        payload = control_center.AccountControlCategoryUpdatePayload(
            client_id=1, category_type="note", label="Printing expenses",
            source_statement_category_id=11, is_active=True,
        )
        note_row = {"id": 50, "source_statement_category_id": 10, "parent_category_id": None}
        inherited_note = {"parent_category_id": None, "source_statement_category_id": 10}
        with patch.object(control_center, "execute_query_pg", side_effect=[
            [note_row], [inherited_note], [{"statement_section": "income_statement"}],
            [{"statement_section": "income_statement"}],
            [{"statement_section": "income_statement", "row_type": "category", "is_active": True}], [],
        ]), patch.object(control_center, "execute_write_pg") as write:
            result = control_center.update_account_control_category(50, payload, session={"username": "Admin"})
        self.assertTrue(result["saved"])
        self.assertEqual(result["category"]["source_statement_category_id"], 11)
        sql = write.call_args.args[0]
        self.assertIn("source_statement_category_id = %s", sql)
        self.assertNotIn("account_control_assignments", sql)

    def test_move_linked_note_between_statements_is_rejected(self):
        payload = control_center.AccountControlCategoryUpdatePayload(
            client_id=1, category_type="note", label="Printing expenses",
            source_statement_category_id=21, is_active=True,
        )
        with patch.object(control_center, "execute_query_pg", side_effect=[
            [{"id": 50, "source_statement_category_id": 10, "parent_category_id": None}],
            [{"parent_category_id": None, "source_statement_category_id": 10}],
            [{"statement_section": "income_statement"}],
            [{"statement_section": "balance_sheet"}],
        ]), patch.object(control_center, "execute_write_pg") as write:
            with self.assertRaises(HTTPException) as raised:
                control_center.update_account_control_category(50, payload, session={"username": "Admin"})
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("same statement", raised.exception.detail)
        write.assert_not_called()

    def test_reorder_note_breakdowns_persists_complete_sibling_order(self):
        selected = [
            {"id": 52, "parent_category_id": None, "source_statement_category_id": None, "is_active": True},
            {"id": 51, "parent_category_id": None, "source_statement_category_id": None, "is_active": True},
        ]
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ), patch.object(control_center, "execute_query_pg", side_effect=[selected, [{"parent_category_id": None, "source_statement_category_id": None}], [{"parent_category_id": None, "source_statement_category_id": None}], [{"id": 51}, {"id": 52}]]), patch.object(
            control_center, "execute_write_pg"
        ) as write:
            result = control_center.reorder_account_control_categories(
                control_center.AccountControlCategoryReorderPayload(
                    client_id=1, category_type="note", parent_category_id=None, category_ids=[52, 51]
                ),
                session={"username": "Admin"},
            )
        self.assertEqual(result["category_ids"], [52, 51])
        self.assertEqual([call.args[1] for call in write.call_args_list], [(None, 0, 52, 1), (None, 10, 51, 1)])

    def test_reorder_rejects_note_breakdown_move_between_statements(self):
        selected = [{"id": 52, "parent_category_id": 10, "source_statement_category_id": None, "is_active": True}]
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ), patch.object(control_center, "_validate_statement_parent"), patch.object(
            control_center, "_note_statement_section", side_effect=["balance_sheet", "income_statement"]
        ), patch.object(control_center, "execute_query_pg", return_value=selected), patch.object(
            control_center, "execute_write_pg"
        ) as write:
            with self.assertRaises(HTTPException) as raised:
                control_center.reorder_account_control_categories(
                    control_center.AccountControlCategoryReorderPayload(
                        client_id=1, category_type="note", parent_category_id=20, category_ids=[52]
                    ),
                    session={"username": "Admin"},
                )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("same statement", raised.exception.detail)
        write.assert_not_called()

    def test_reorder_financial_statement_siblings_persists_requested_order(self):
        selected = [
            {"id": 2, "row_type": "category", "statement_section": "balance_sheet", "parent_category_id": 10},
            {"id": 1, "row_type": "category", "statement_section": "balance_sheet", "parent_category_id": 10},
        ]
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ), patch.object(control_center, "execute_query_pg", side_effect=[selected, [{"id": 1}, {"id": 2}]]), patch.object(
            control_center, "execute_write_pg"
        ) as write:
            result = control_center.reorder_account_control_categories(
                control_center.AccountControlCategoryReorderPayload(client_id=1, category_ids=[2, 1]),
                session={"username": "Admin"},
            )
        self.assertEqual(result, {"saved": True, "category_ids": [2, 1]})
        self.assertEqual([call.args[1] for call in write.call_args_list], [(0, 2, 1), (10, 1, 1)])

    def test_reorder_rejects_rows_with_different_parent_groups(self):
        rows = [
            {"id": 2, "row_type": "category", "statement_section": "balance_sheet", "parent_category_id": 10},
            {"id": 3, "row_type": "category", "statement_section": "balance_sheet", "parent_category_id": 11},
        ]
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ), patch.object(control_center, "execute_query_pg", return_value=rows), patch.object(
            control_center, "execute_write_pg"
        ) as write:
            with self.assertRaises(HTTPException) as raised:
                control_center.reorder_account_control_categories(
                    control_center.AccountControlCategoryReorderPayload(client_id=1, category_ids=[2, 3]),
                    session={"username": "Admin"},
                )
        self.assertEqual(raised.exception.status_code, 400)
        write.assert_not_called()

    def test_reorder_requires_the_complete_sibling_group(self):
        selected = [{"id": 2, "row_type": "category", "statement_section": "balance_sheet", "parent_category_id": 10}]
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ), patch.object(control_center, "execute_query_pg", side_effect=[selected, [{"id": 1}, {"id": 2}]]), patch.object(
            control_center, "execute_write_pg"
        ) as write:
            with self.assertRaises(HTTPException) as raised:
                control_center.reorder_account_control_categories(
                    control_center.AccountControlCategoryReorderPayload(client_id=1, category_ids=[2]),
                    session={"username": "Admin"},
                )
        self.assertEqual(raised.exception.status_code, 400)
        write.assert_not_called()

    def test_delete_unplaced_financial_statement_category(self):
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ), patch.object(
            control_center, "execute_query_pg", side_effect=[
                [], [{"id": 31, "label": "Unused", "statement_section": None, "row_type": "category"}], [], [], [],
                [{"account_code": "OLD-1"}],
            ]
        ), patch.object(control_center, "execute_write_pg") as write:
            result = control_center.delete_account_control_category(
                31, client_id=1, session={"username": "Admin"}
            )
        self.assertEqual(result, {"deleted": True, "category_id": 31, "deleted_child_count": 0, "orphan_assignments_cleared": 1})
        self.assertEqual(write.call_count, 3)
        self.assertIn("UPDATE account_control_assignments", write.call_args_list[0].args[0])
        self.assertIn("SET financial_statement_category_id = NULL", write.call_args_list[0].args[0])
        self.assertIn("calculation_components", write.call_args_list[1].args[0])
        self.assertIn("DELETE FROM account_control_categories", write.call_args_list[2].args[0])

    def test_delete_heading_cascades_unassigned_child_categories(self):
        children = [{"id": 32, "label": "Land"}, {"id": 33, "label": "Equipment"}]
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ), patch.object(
            control_center, "execute_query_pg", side_effect=[
                [],
                [{"id": 31, "label": "Fixed assets", "statement_section": "balance_sheet", "row_type": "heading"}],
                children, [], [], [], [], [],
            ]
        ), patch.object(control_center, "execute_write_pg") as write:
            result = control_center.delete_account_control_category(
                31, client_id=1, session={"username": "Admin"}
            )
        self.assertEqual(result["deleted_child_count"], 2)
        self.assertEqual(result["orphan_assignments_cleared"], 0)
        self.assertEqual(write.call_count, 3)
        self.assertIn("calculation_components", write.call_args_list[0].args[0])
        self.assertIn("parent_category_id = %s", write.call_args_list[1].args[0])
        self.assertIn("DELETE FROM account_control_categories", write.call_args_list[2].args[0])

    def test_delete_heading_is_blocked_when_child_category_has_real_account_assignment(self):
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ), patch.object(
            control_center, "execute_query_pg", side_effect=[
                [],
                [{"id": 31, "label": "Fixed assets", "statement_section": "balance_sheet", "row_type": "heading"}],
                [{"id": 32, "label": "Land"}], [], [], [], [{"account_code": "10-01"}],
            ]
        ), patch.object(control_center, "execute_write_pg") as write:
            with self.assertRaises(HTTPException) as raised:
                control_center.delete_account_control_category(
                    31, client_id=1, session={"username": "Admin"}
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("child categories", raised.exception.detail)
        write.assert_not_called()

    def test_delete_unplaced_category_rejects_existing_account_assignment(self):
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ), patch.object(
            control_center, "execute_query_pg", side_effect=[
                [], [{"id": 31, "statement_section": None, "row_type": "category"}], [], [], [{"id": 5}],
            ]
        ), patch.object(control_center, "execute_write_pg") as write:
            with self.assertRaises(HTTPException) as raised:
                control_center.delete_account_control_category(
                    31, client_id=1, session={"username": "Admin"}
                )
        self.assertEqual(raised.exception.status_code, 409)
        write.assert_not_called()

    def test_delete_category_rejects_direct_total_reference(self):
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ), patch.object(control_center, "execute_query_pg", side_effect=[
            [], [{"id": 31, "statement_section": "balance_sheet", "row_type": "category"}], [], [{"id": 9}],
        ]), patch.object(control_center, "execute_write_pg") as write:
            with self.assertRaises(HTTPException) as raised:
                control_center.delete_account_control_category(31, client_id=1, session={"username": "Admin"})
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("Remove this category from its Total line", raised.exception.detail)
        write.assert_not_called()

    def test_moving_direct_total_category_preserves_reference(self):
        payload = control_center.AccountControlCategoryUpdatePayload(
            client_id=1, category_type="financial_statement", label="Cash and Bank",
            statement_section="balance_sheet", row_type="category", parent_category_id=30,
        )
        with patch.object(control_center, "execute_query_pg", side_effect=[
            [{"id": 31, "source_statement_category_id": None, "statement_section": "balance_sheet", "row_type": "category", "parent_category_id": 20}],
            [{"statement_section": "balance_sheet", "row_type": "heading", "is_active": True}],
            [{"calculation_id": 88}], [],
        ]), patch.object(control_center, "execute_write_pg") as write:
            result = control_center.update_account_control_category(31, payload, session={"username": "Admin"})
        self.assertTrue(result["saved"])
        self.assertIn("calculation_type = 'subtotal'", write.call_args_list[0].args[0])

    def test_moving_category_cannot_overlap_destination_subtotal_in_total(self):
        payload = control_center.AccountControlCategoryUpdatePayload(
            client_id=1, category_type="financial_statement", label="Cash and Bank",
            statement_section="balance_sheet", row_type="category", parent_category_id=30,
        )
        with patch.object(control_center, "execute_query_pg", side_effect=[
            [{"id": 31, "source_statement_category_id": None, "statement_section": "balance_sheet", "row_type": "category", "parent_category_id": 20}],
            [{"statement_section": "balance_sheet", "row_type": "heading", "is_active": True}],
            [{"calculation_id": 88}], [{"id": 90}],
        ]), patch.object(control_center, "execute_write_pg") as write:
            with self.assertRaises(HTTPException) as raised:
                control_center.update_account_control_category(31, payload, session={"username": "Admin"})
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("destination heading subtotal", raised.exception.detail)
        write.assert_not_called()

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


class FinancialStatementCalculationTests(unittest.TestCase):
    def test_subtotal_persists_signed_direct_categories(self):
        payload = control_center.StatementCalculationPayload(
            client_id=1,
            statement_section="balance_sheet",
            calculation_type="subtotal",
            label="Total Fixed Assets",
            parent_heading_id=30,
            components=[
                control_center.StatementCalculationComponentPayload(source_category_id=31, operator="add"),
                control_center.StatementCalculationComponentPayload(source_category_id=32, operator="subtract"),
            ],
        )
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "execute_query_pg", side_effect=[
                [{"id": 30, "statement_section": "balance_sheet", "row_type": "heading", "is_active": True}],
                [{"id": 31}, {"id": 32}],
                [],
            ]
        ), patch.object(control_center, "execute_write_pg", side_effect=[77, None, None]) as write, patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ):
            result = control_center.create_statement_calculation(payload, session={"username": "Admin"})
        self.assertEqual(result, {"saved": True, "calculation_id": 77})
        component_calls = [call for call in write.call_args_list if "calculation_components" in call.args[0]]
        self.assertEqual([call.args[1][3] for call in component_calls], ["add", "subtract"])

    def test_subtotal_rejects_stale_or_cross_heading_categories(self):
        calculation = {
            "client_id": 1,
            "statement_section": "balance_sheet",
            "calculation_type": "subtotal",
            "parent_heading_id": 30,
            "components": [control_center.StatementCalculationComponentPayload(source_category_id=31)],
        }
        with patch.object(control_center, "execute_query_pg", side_effect=[
            [{"id": 30, "statement_section": "balance_sheet", "row_type": "heading", "is_active": True}],
            [{"id": 31}, {"id": 32}],
        ]):
            with self.assertRaises(HTTPException) as raised:
                control_center._validate_statement_calculation_components(calculation)
        self.assertEqual(raised.exception.status_code, 409)

    def test_statement_total_accepts_subtotals_and_direct_categories(self):
        payload = control_center.StatementCalculationPayload(
            client_id=1,
            statement_section="balance_sheet",
            calculation_type="total",
            label="Total Assets",
            components=[
                control_center.StatementCalculationComponentPayload(source_calculation_id=77, operator="add"),
                control_center.StatementCalculationComponentPayload(source_category_id=31, operator="subtract"),
            ],
        )
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "execute_query_pg", side_effect=[
                [{"last_order": 0}],
                [{"id": 77, "parent_heading_id": 30, "calculation_type": "subtotal", "sort_order": 0}],
                [{"id": 31, "parent_category_id": 40}],
            ]
        ), patch.object(control_center, "execute_write_pg", side_effect=[88, None, None]) as write, patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ):
            result = control_center.create_statement_calculation(payload, session={"username": "Admin"})
        self.assertEqual(result["calculation_id"], 88)
        self.assertEqual(write.call_args_list[0].args[1][-1], 1)
        component_calls = [call for call in write.call_args_list if "calculation_components" in call.args[0]]
        self.assertEqual([(call.args[1][1], call.args[1][2], call.args[1][3]) for call in component_calls], [
            (None, 77, "add"), (31, None, "subtract"),
        ])

    def test_total_rejects_subtotal_and_category_from_same_heading(self):
        calculation = {
            "client_id": 1,
            "statement_section": "balance_sheet",
            "calculation_type": "total",
            "parent_heading_id": None,
            "components": [
                control_center.StatementCalculationComponentPayload(source_calculation_id=77),
                control_center.StatementCalculationComponentPayload(source_category_id=31),
            ],
        }
        with patch.object(control_center, "execute_query_pg", side_effect=[
            [{"id": 77, "parent_heading_id": 30, "calculation_type": "subtotal", "sort_order": 0}],
            [{"id": 31, "parent_category_id": 30}],
        ]):
            with self.assertRaises(HTTPException) as raised:
                control_center._validate_statement_calculation_components(calculation)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("either a heading subtotal or its categories", raised.exception.detail)

    def test_total_can_reference_an_earlier_total(self):
        calculation = {
            "client_id": 1,
            "statement_section": "income_statement",
            "calculation_type": "total",
            "sort_order": 1,
            "parent_heading_id": None,
            "components": [control_center.StatementCalculationComponentPayload(source_calculation_id=77)],
        }
        with patch.object(control_center, "execute_query_pg", side_effect=[
            [{"id": 77, "parent_heading_id": None, "calculation_type": "total", "sort_order": 0}],
            [{"calculation_id": 77, "calculation_type": "total", "parent_heading_id": None, "source_category_id": 31, "source_calculation_id": None}],
            [{"id": 31, "parent_category_id": 40}],
        ]):
            components = control_center._validate_statement_calculation_components(calculation)
        self.assertEqual(components[0]["source_calculation_id"], 77)

    def test_total_rejects_later_or_self_total_references(self):
        calculation = {
            "client_id": 1,
            "statement_section": "income_statement",
            "calculation_type": "total",
            "sort_order": 1,
            "parent_heading_id": None,
            "components": [control_center.StatementCalculationComponentPayload(source_calculation_id=77)],
        }
        for source_order, calculation_id in ((2, None), (1, 77)):
            with self.subTest(source_order=source_order, calculation_id=calculation_id), patch.object(
                control_center, "execute_query_pg", return_value=[{
                    "id": 77, "parent_heading_id": None, "calculation_type": "total", "sort_order": source_order,
                }]
            ):
                with self.assertRaises(HTTPException) as raised:
                    control_center._validate_statement_calculation_components(calculation, calculation_id=calculation_id)
            self.assertEqual(raised.exception.status_code, 400)
            self.assertIn("earlier totals", raised.exception.detail)

    def test_create_total_persists_earlier_total_reference(self):
        payload = control_center.StatementCalculationPayload(
            client_id=1,
            statement_section="income_statement",
            calculation_type="total",
            label="Operating Profit",
            components=[control_center.StatementCalculationComponentPayload(source_calculation_id=77)],
        )
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "execute_query_pg", side_effect=[
                [{"last_order": 0}],
                [{"id": 77, "parent_heading_id": None, "calculation_type": "total", "sort_order": 0}],
                [{"calculation_id": 77, "calculation_type": "total", "parent_heading_id": None, "source_category_id": 31, "source_calculation_id": None}],
                [{"id": 31, "parent_category_id": 40}],
            ]
        ), patch.object(control_center, "execute_write_pg", side_effect=[88, None]) as write, patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ):
            result = control_center.create_statement_calculation(payload, session={"username": "Admin"})
        self.assertEqual(result["calculation_id"], 88)
        component_insert = next(call for call in write.call_args_list if "calculation_components" in call.args[0])
        self.assertEqual(component_insert.args[1][2], 77)

    def test_total_list_returns_earlier_total_label(self):
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "execute_query_pg", side_effect=[
                [{"id": 88, "client_id": 1, "statement_section": "income_statement", "calculation_type": "total", "label": "Operating Profit", "parent_heading_id": None, "sort_order": 1}],
                [{"source_category_id": None, "source_calculation_id": 77, "operator": "add", "sort_order": 0}],
                [{"id": 77, "label": "Gross Profit", "calculation_type": "total"}],
            ]
        ):
            result = control_center.list_statement_calculations(client_id=1, statement_section="income_statement", session={"username": "Admin"})
        component = result["calculations"][0]["components"][0]
        self.assertEqual((component["label"], component["source_calculation_id"]), ("Gross Profit", 77))

    def test_total_rejects_category_already_included_via_earlier_total(self):
        calculation = {
            "client_id": 1,
            "statement_section": "income_statement",
            "calculation_type": "total",
            "sort_order": 1,
            "components": [
                control_center.StatementCalculationComponentPayload(source_calculation_id=77),
                control_center.StatementCalculationComponentPayload(source_category_id=31),
            ],
        }
        with patch.object(control_center, "execute_query_pg", side_effect=[
            [{"id": 77, "parent_heading_id": None, "calculation_type": "total", "sort_order": 0}],
            [{"id": 31, "parent_category_id": 40}],
            [{"calculation_id": 77, "calculation_type": "total", "parent_heading_id": None, "source_category_id": 31, "source_calculation_id": None}],
            [{"id": 31, "parent_category_id": 40}],
        ]):
            with self.assertRaises(HTTPException) as raised:
                control_center._validate_statement_calculation_components(calculation)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Avoid double-counting", raised.exception.detail)

    def test_editing_total_cannot_make_later_total_double_count(self):
        calculation = {
            "client_id": 1,
            "statement_section": "income_statement",
            "calculation_type": "total",
            "sort_order": 0,
            "components": [control_center.StatementCalculationComponentPayload(source_category_id=32)],
        }
        with patch.object(control_center, "execute_query_pg", side_effect=[
            [{"id": 32, "parent_category_id": 40}],
            [
                {"calculation_id": 77, "calculation_type": "total", "parent_heading_id": None, "source_category_id": 31, "source_calculation_id": None},
                {"calculation_id": 88, "calculation_type": "total", "parent_heading_id": None, "source_category_id": None, "source_calculation_id": 77},
                {"calculation_id": 88, "calculation_type": "total", "parent_heading_id": None, "source_category_id": 32, "source_calculation_id": None},
            ],
            [{"id": 31, "parent_category_id": 40}, {"id": 32, "parent_category_id": 50}],
        ]):
            with self.assertRaises(HTTPException) as raised:
                control_center._validate_statement_calculation_components(calculation, calculation_id=77)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("later total", raised.exception.detail)

    def test_referenced_total_cannot_be_deleted(self):
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "execute_query_pg", side_effect=[[{"calculation_type": "total"}], [{"id": 1}]]
        ), patch.object(control_center, "execute_write_pg") as write, patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ):
            with self.assertRaises(HTTPException) as raised:
                control_center.delete_statement_calculation(77, client_id=1, session={"username": "Admin"})
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("later Total lines", raised.exception.detail)
        write.assert_not_called()

    def test_subtotal_list_includes_new_direct_categories_with_plus_by_default(self):
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "execute_query_pg", side_effect=[
                [{"id": 77, "client_id": 1, "statement_section": "balance_sheet", "calculation_type": "subtotal", "label": "Total Assets", "parent_heading_id": 30, "sort_order": 0}],
                [{"source_category_id": 31, "source_calculation_id": None, "operator": "subtract"}],
                [{"id": 31, "label": "Land"}, {"id": 32, "label": "New equipment"}],
            ]
        ):
            result = control_center.list_statement_calculations(client_id=1, statement_section="balance_sheet", session={"username": "Admin"})
        components = result["calculations"][0]["components"]
        self.assertEqual([(row["source_category_id"], row["operator"]) for row in components], [(31, "subtract"), (32, "add")])

    def test_total_list_returns_subtotal_and_category_components(self):
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "execute_query_pg", side_effect=[
                [{"id": 88, "client_id": 1, "statement_section": "balance_sheet", "calculation_type": "total", "label": "Total Assets", "parent_heading_id": None, "sort_order": 0}],
                [
                    {"source_category_id": None, "source_calculation_id": 77, "operator": "add", "sort_order": 0},
                    {"source_category_id": 31, "source_calculation_id": None, "operator": "subtract", "sort_order": 1},
                ],
                [{"id": 77, "label": "Total Current Assets"}],
                [{"id": 31, "label": "Cash and Bank"}],
            ]
        ):
            result = control_center.list_statement_calculations(client_id=1, statement_section="balance_sheet", session={"username": "Admin"})
        components = result["calculations"][0]["components"]
        self.assertEqual([(row["label"], row["source_category_id"], row["source_calculation_id"], row["operator"]) for row in components], [
            ("Total Current Assets", None, 77, "add"), ("Cash and Bank", 31, None, "subtract"),
        ])

    def test_income_statement_calculation_payload_is_accepted(self):
        payload = control_center.StatementCalculationPayload(
            client_id=1,
            statement_section="income_statement",
            calculation_type="total",
            label="Profit",
        )
        cleaned = control_center._clean_statement_calculation(payload)
        self.assertEqual(cleaned["statement_section"], "income_statement")

    def test_income_statement_subtotal_uses_its_own_heading_categories(self):
        payload = control_center.StatementCalculationPayload(
            client_id=1,
            statement_section="income_statement",
            calculation_type="subtotal",
            label="Total Revenue",
            parent_heading_id=30,
            components=[control_center.StatementCalculationComponentPayload(source_category_id=31)],
        )
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "execute_query_pg", side_effect=[
                [{"id": 30, "statement_section": "income_statement", "row_type": "heading", "is_active": True}],
                [{"id": 31}],
                [],
            ]
        ) as query, patch.object(control_center, "execute_write_pg", side_effect=[77, None]) as write, patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ):
            result = control_center.create_statement_calculation(payload, session={"username": "Admin"})
        self.assertEqual(result["calculation_id"], 77)
        self.assertEqual(query.call_args_list[1].args[1], (1, "income_statement", 30))
        self.assertEqual(write.call_args_list[0].args[1][1], "income_statement")

    def test_income_statement_total_persists_only_income_statement_sources(self):
        payload = control_center.StatementCalculationPayload(
            client_id=1,
            statement_section="income_statement",
            calculation_type="total",
            label="Net profit",
            components=[
                control_center.StatementCalculationComponentPayload(source_calculation_id=77, operator="add"),
                control_center.StatementCalculationComponentPayload(source_category_id=51, operator="subtract"),
            ],
        )
        with patch.object(control_center, "_require_active_client"), patch.object(
            control_center, "execute_query_pg", side_effect=[
                [{"last_order": -1}],
                [{"id": 77, "parent_heading_id": 30, "calculation_type": "subtotal", "sort_order": 0}],
                [{"id": 51, "parent_category_id": 40}],
            ]
        ), patch.object(control_center, "execute_write_pg", side_effect=[88, None, None]) as write, patch.object(
            control_center, "pg_transaction", return_value=nullcontext()
        ):
            result = control_center.create_statement_calculation(payload, session={"username": "Admin"})
        self.assertEqual(result["calculation_id"], 88)
        insert_call = write.call_args_list[0]
        self.assertIn("statement_section", insert_call.args[0])
        self.assertEqual(insert_call.args[1][1], "income_statement")

    def test_income_statement_total_accepts_standalone_category(self):
        calculation = {
            "client_id": 1,
            "statement_section": "income_statement",
            "calculation_type": "total",
            "parent_heading_id": None,
            "components": [control_center.StatementCalculationComponentPayload(source_category_id=51)],
        }
        with patch.object(control_center, "execute_query_pg", return_value=[{"id": 51, "parent_category_id": None}]) as query:
            components = control_center._validate_statement_calculation_components(calculation)
        self.assertEqual(components[0]["source_category_id"], 51)
        self.assertIn("c.parent_category_id IS NULL", query.call_args.args[0])

    def test_balance_sheet_total_still_rejects_standalone_category(self):
        calculation = {
            "client_id": 1,
            "statement_section": "balance_sheet",
            "calculation_type": "total",
            "parent_heading_id": None,
            "components": [control_center.StatementCalculationComponentPayload(source_category_id=51)],
        }
        with patch.object(control_center, "execute_query_pg", return_value=[]):
            with self.assertRaises(HTTPException) as raised:
                control_center._validate_statement_calculation_components(calculation)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("categories under headings", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()


class TrialBalanceOrderedControlTests(unittest.TestCase):
    def validate(self, rows, **kwargs):
        columns = ['Code', 'Name', 'OD', 'OC', 'PD', 'PC', 'CD', 'CC']
        frame = pd.DataFrame(rows, columns=columns)
        cache = getattr(self, '_workbook_cache', {})
        key = json.dumps(rows)
        if key not in cache:
            stream = BytesIO()
            frame.to_excel(stream, index=False)
            cache[key] = stream.getvalue()
        self._workbook_cache = cache
        mapping = dict(zip(['account_code_column', 'account_name_column', 'opening_dr_column',
                            'opening_cr_column', 'period_dr_column', 'period_cr_column',
                            'closing_dr_column', 'closing_cr_column'], columns))
        self._last_contents, self._last_mapping = cache[key], mapping
        kwargs.setdefault('no_total_row', 'total_excel_row' not in kwargs)
        return trial_balance.validate_trial_balance_contents(cache[key], 'tb.xlsx', mapping, **kwargs)

    def test_explicit_erp_total_is_excluded_and_compared_before_adjustments(self):
        result = self.validate([
            ['1', 'Parent', 900, 0, 0, 0, 900, 0],
            ['1-1', 'Cash', 100, 0, 20, 0, 110, 0],
            ['B', 'Credit', 0, 100, 0, 20, 0, 110],
            ['Totals :', '', 100, 100, 20, 20, 110, 110],
        ], total_excel_row='5')
        self.assertTrue(result['valid'])
        self.assertEqual(result['account_count'], 3)
        self.assertEqual(result['source_total_rows'], [5])
        self.assertEqual(result['checks'][1]['status'], 'pass')
        self.assertEqual(result['rollforward_difference_count'], 2)
        self.assertEqual(result['source_total_comparison'][-2]['leaf_total'], '110')

    def test_footer_without_code_and_no_total_are_supported(self):
        rows = [['A', 'Debit', 10, 0, 0, 0, 10, 0], ['B', 'Credit', 0, 10, 0, 0, 0, 10]]
        self.assertEqual(self.validate(rows)['checks'][1]['status'], 'not_applicable')
        result = self.validate(rows + [['', '', 10, 10, 0, 0, 10, 10]], total_excel_row='4')
        self.assertTrue(result['valid'])
        self.assertEqual(result['source_total_rows'], [4])

    def test_erp_mismatch_blocks_both_adjustment_stages(self):
        result = self.validate([['A', 'Cash', 100, 0, 20, 0, 110, 0],
                                ['Total', '', 99, 0, 20, 0, 110, 0]], total_excel_row='3')
        self.assertFalse(result['valid'])
        self.assertEqual(result['checks'][1]['status'], 'fail')
        self.assertEqual(result['rollforward_differences'], [])
        self.assertEqual(result['global_adjustments'], [])

    def test_code_correction_releases_later_checks(self):
        rows = [['2025-01-01', 'Cash', 100, 0, 0, 0, 100, 0]]
        result = self.validate(rows)
        self.assertEqual(result['checks'][0]['status'], 'fail')
        self.assertEqual(result['rollforward_differences'], [])
        fixed = self.validate(rows, code_overrides={'2': 'A1'})
        self.assertEqual(fixed['checks'][0]['status'], 'pass')
        self.assertTrue(fixed['global_adjustments'])

    def test_confirmation_binds_exact_source_and_balances_all_pairs(self):
        rows = [['A', 'Cash', 100, 0, 0, 20, 70, 0]]
        pending = self.validate(rows)
        self.assertFalse(pending['valid'])
        self.assertEqual(pending['rollforward_difference_count'], 1)
        self.assertEqual(pending['global_adjustments'][0]['opening_cr_adjustment'], '100')
        self.assertEqual(pending['global_adjustments'][1]['period_dr_adjustment'], '20')
        accepted = self.validate(rows, adjustment_confirmation=pending['adjustment_confirmation_token'])
        self.assertTrue(accepted['valid'])
        self.assertTrue(accepted['adjustments_confirmed'])
        for check in accepted['checks'][-3:]:
            self.assertEqual(check['debit_total'], check['credit_total'])
        changed = self.validate([['A', 'Cash', 101, 0, 0, 20, 70, 0]],
                                adjustment_confirmation=pending['adjustment_confirmation_token'])
        self.assertFalse(changed['valid'])
        self.assertFalse(changed['adjustments_confirmed'])

    def test_saved_controls_are_read_from_upload_metadata(self):
        controls = {'global_adjustments': [{'opening_cr_adjustment': '100'}], 'adjustments_confirmed': True}
        with patch.object(trial_balance, 'execute_query_pg', return_value=[{'id': 1, 'validation_controls': json.dumps(controls)}]):
            self.assertEqual(trial_balance._require_upload(1), controls)


    def test_upload_rechecks_confirmation_and_persists_controls_without_footer_account(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        pending = self.validate([['A', 'Cash', 100, 0, 0, 0, 100, 0],
                                 ['Totals :', '', 100, 0, 0, 0, 100, 0]], total_excel_row='3')
        app = FastAPI()
        app.include_router(trial_balance.router)
        client = TestClient(app)
        data = {'client_id': '1', 'as_of_date': '2026-06-30', 'total_excel_row': '3', **self._last_mapping}
        files = {'file': ('tb.xlsx', self._last_contents, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}
        with patch.object(trial_balance, 'execute_query_pg', side_effect=lambda sql, args: [{'id': 1}] if 'FROM clients' in sql else []), \
             patch.object(trial_balance, 'execute_write_pg', return_value=77) as write, \
             patch.object(trial_balance, 'pg_transaction', side_effect=nullcontext), \
             patch.object(trial_balance, 'get_raw_conn_pg', return_value=object()), \
             patch.object(trial_balance, 'release_raw_conn_pg'), \
             patch.object(trial_balance, 'execute_batch_pg') as batch, \
             patch.object(trial_balance, 'log_audit'), \
             patch.object(trial_balance, 'invalidate_category_for_account_cache'):
            rejected = client.post('/api/trial-balance/upload', data=data, files=files)
            self.assertEqual(rejected.status_code, 400)
            write.assert_not_called()
            accepted = client.post('/api/trial-balance/upload',
                                   data={**data, 'adjustment_confirmation': pending['adjustment_confirmation_token']}, files=files)
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(accepted.json()['global_adjustment_count'], 1)
            saved_rows = batch.call_args.args[2]
            self.assertEqual(len(saved_rows), 1)
            self.assertEqual(saved_rows[0][1], 'A')
            self.assertEqual(saved_rows[0][7], 100)
            metadata_call = next(c for c in write.call_args_list if 'validation_controls = ' in c.args[0])
            controls = json.loads(metadata_call.args[1][2])
            self.assertTrue(controls['adjustments_confirmed'])
            self.assertEqual(controls['source_total_rows'], [3])
            self.assertEqual(controls['global_adjustments'][0]['closing_cr_adjustment'], '100')


    def test_total_choice_is_required_and_rejects_invalid_or_conflicting_choices(self):
        rows = [['A', 'Cash', 10, 0, 0, 0, 10, 0]]
        for value in (None, '', '1', '3', '2.5', '2e0', '-2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.validate(rows, total_excel_row=value, no_total_row=False)
        with self.assertRaisesRegex(ValueError, 'not both'):
            self.validate(rows, total_excel_row='2', no_total_row=True)

    def test_manual_selection_uses_excel_position_and_ignores_row_labels(self):
        rows = [['A', 'Debit', 10, 0, 0, 0, 10, 0],
                ['B', 'Credit', 0, 10, 0, 0, 0, 10],
                ['', '', '', '', '', '', '', ''],
                ['ERP footer code', 'Summary without a total label', 10, 10, 0, 0, 10, 10]]
        result = self.validate(rows, total_excel_row='5')
        self.assertTrue(result['valid'])
        self.assertEqual(result['account_count'], 2)
        self.assertEqual(result['source_total_rows'], [5])
        with self.assertRaisesRegex(ValueError, 'no mapped amounts'):
            self.validate(rows, total_excel_row='4')
        invalid = rows[:]
        invalid[-1] = ['Footer', '', 'invalid amount', 10, 0, 0, 10, 10]
        with self.assertRaisesRegex(ValueError, 'invalid mapped amount'):
            self.validate(invalid, total_excel_row='5')

    def test_no_total_does_not_guess_from_account_name_or_code(self):
        result = self.validate([['Total', 'A real ledger', 0, 0, 0, 0, 0, 0]], no_total_row=True)
        self.assertTrue(result['valid'])
        self.assertEqual(result['source_total_rows'], [])
        self.assertEqual(result['account_count'], 1)

    def test_changing_selected_total_row_requires_new_difference_confirmation(self):
        rows = [['A', 'Cash', 100, 0, 0, 0, 100, 0],
                ['', '', 100, 0, 0, 0, 100, 0],
                ['', '', 100, 0, 0, 0, 100, 0]]
        pending = self.validate(rows, total_excel_row='3', excluded_excel_rows=[4])
        changed = self.validate(rows, total_excel_row='4', excluded_excel_rows=[3],
                                adjustment_confirmation=pending['adjustment_confirmation_token'])
        self.assertFalse(changed['adjustments_confirmed'])
        self.assertFalse(changed['valid'])
