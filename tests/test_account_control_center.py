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
        self.assertIn("CREATE TABLE IF NOT EXISTS account_control_assignments", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_sets", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_set_versions", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS account_set_selectors", sql)
        self.assertIn("idx_account_set_selectors_version", sql)
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



if __name__ == "__main__":
    unittest.main()
