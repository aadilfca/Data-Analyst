import os
import sqlite3
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import database
from routers import reconciliation


def _account_rows():
    return [
        {
            "id": 1,
            "account_code": "B1",
            "account_name": "Bank A",
            "source_account_type": "Bank",
            "classification": "leaf",
        },
        {
            "id": 2,
            "account_code": "C1",
            "account_name": "Cash A",
            "source_account_type": "Cash",
            "classification": "leaf",
        },
        {
            "id": 3,
            "account_code": "EXP",
            "account_name": "Expense",
            "source_account_type": "Expense",
            "classification": "leaf",
        },
    ]


def test_sqlite_init_creates_account_groups_table_and_indexes():
    with tempfile.TemporaryDirectory() as temp_dir:
        original_db_name = database.DB_NAME
        database.DB_NAME = os.path.join(temp_dir, "test.db")
        try:
            database.init_db()
            conn = sqlite3.connect(database.DB_NAME)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(account_groups)")}
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(account_groups)")}
                report_tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'report_%'"
                    )
                }
                balance_columns = {row[1] for row in conn.execute("PRAGMA table_info(report_balance_inputs)")}
                balance_indexes = {row[1] for row in conn.execute("PRAGMA index_list(report_balance_inputs)")}
            finally:
                conn.close()
            assert {"client_id", "group_label", "account_code", "is_active"}.issubset(columns)
            assert "idx_account_groups_client" in indexes
            assert "idx_account_groups_client_label" in indexes
            assert report_tables == {
                "report_category_sets",
                "report_categories",
                "report_account_category_assignments",
                "report_balance_inputs",
            }
            assert {"group_label", "account_code", "period_month", "opening_balance", "actual_closing_balance"}.issubset(balance_columns)
            assert "idx_report_balance_inputs_client_group" in balance_indexes
            assert "idx_report_balance_inputs_period" in balance_indexes
        finally:
            database.DB_NAME = original_db_name


def test_normalizers_preserve_codes_and_clean_group_labels():
    assert reconciliation.normalize_group_label("  Bank   Accounts ") == "Bank Accounts"
    assert reconciliation.normalize_account_code(" 10-15-115-10-0001 ") == "10-15-115-10-0001"


def test_suggestions_are_advisory_and_show_saved_exact_codes():
    def query(sql, params=None):
        if "FROM clients" in sql:
            return [{"id": 7}]
        if "FROM trial_balance_accounts" in sql:
            return _account_rows()
        if "FROM account_groups" in sql:
            return [{"account_code": "B1"}]
        raise AssertionError(f"Unexpected query: {sql}")

    with patch.object(reconciliation, "execute_query_pg", side_effect=query):
        response = reconciliation.list_account_group_suggestions(7, "Bank")

    assert response["accounts"][0]["suggested_group"] == "Bank"
    assert response["accounts"][0]["is_selected"] is True
    assert response["accounts"][1]["suggested_group"] == "Cash"
    assert response["accounts"][2]["suggested_group"] is None


def test_save_group_rejects_unknown_or_duplicate_codes():
    payload = reconciliation.AccountGroupUpdate(group_label="Bank", account_codes=["B1", "B1"])
    with pytest.raises(HTTPException) as duplicate:
        reconciliation._clean_group_request(payload)
    assert duplicate.value.status_code == 400

    def query(sql, params=None):
        if "FROM clients" in sql:
            return [{"id": 7}]
        if "FROM trial_balance_accounts" in sql:
            return _account_rows()
        if "FROM account_groups" in sql:
            return []
        raise AssertionError(f"Unexpected query: {sql}")

    with patch.object(reconciliation, "execute_query_pg", side_effect=query):
        with pytest.raises(HTTPException) as unknown:
            reconciliation.save_account_group(
                7,
                reconciliation.AccountGroupUpdate(group_label="Bank", account_codes=["MISSING"]),
            )
    assert unknown.value.status_code == 400
    assert unknown.value.detail["account_codes"] == ["MISSING"]


def test_save_group_replaces_membership_transactionally_and_keeps_silent_codes():
    writes = []

    def query(sql, params=None):
        if "FROM clients" in sql:
            return [{"id": 7}]
        if "FROM trial_balance_accounts" in sql:
            return _account_rows()
        if "FROM account_groups" in sql:
            return []
        raise AssertionError(f"Unexpected query: {sql}")

    def write(sql, params=None):
        writes.append((sql, params))

    with patch.object(reconciliation, "execute_query_pg", side_effect=query), \
         patch.object(reconciliation, "execute_write_pg", side_effect=write), \
         patch.object(reconciliation, "pg_transaction", return_value=nullcontext()), \
         patch.object(reconciliation, "log_audit"):
        response = reconciliation.save_account_group(
            7,
            reconciliation.AccountGroupUpdate(group_label="Bank", account_codes=["B1"]),
        )

    assert response == {"client_id": 7, "group_label": "Bank", "account_codes": ["B1"], "count": 1}
    assert "UPDATE account_groups" in writes[0][0]
    assert "INSERT INTO account_groups" in writes[1][0]
    assert writes[1][1] == (7, "Bank", "B1")


def test_list_groups_returns_only_active_membership_as_grouped_codes():
    def query(sql, params=None):
        if "FROM clients" in sql:
            return [{"id": 7}]
        return [
            {"group_label": "Bank", "account_code": "B1", "updated_at": None},
            {"group_label": "Bank", "account_code": "B2", "updated_at": None},
            {"group_label": "Cash", "account_code": "C1", "updated_at": None},
        ]

    with patch.object(reconciliation, "execute_query_pg", side_effect=query):
        response = reconciliation.list_account_groups(7)

    assert response["groups"] == [
        {"group_label": "Bank", "account_codes": ["B1", "B2"], "updated_at": None},
        {"group_label": "Cash", "account_codes": ["C1"], "updated_at": None},
    ]


def test_clean_balance_request_normalizes_months_and_rejects_duplicates():
    payload = reconciliation.ReconciliationBalanceInputsUpdate(
        group_label=" Bank ",
        balances=[
            reconciliation.ReconciliationBalanceInput(
                account_code=" B1 ",
                period_month="2026-01-31",
                opening_balance=0,
                actual_closing_balance=100,
            )
        ],
    )
    group_label, balances = reconciliation._clean_balance_request(payload)
    assert group_label == "Bank"
    assert balances[0]["account_code"] == "B1"
    assert balances[0]["period_month"] == "2026-01"
    assert balances[0]["opening_balance"] == 0

    duplicate = reconciliation.ReconciliationBalanceInputsUpdate(
        group_label="Bank",
        balances=[
            reconciliation.ReconciliationBalanceInput(account_code="B1", period_month="2026-01", opening_balance=0, actual_closing_balance=1),
            reconciliation.ReconciliationBalanceInput(account_code="B1", period_month="2026-01-31", opening_balance=0, actual_closing_balance=1),
        ],
    )
    with pytest.raises(HTTPException) as error:
        reconciliation._clean_balance_request(duplicate)
    assert error.value.status_code == 400


def test_replace_balances_requires_group_membership_and_writes_complete_rows():
    writes = []

    def query(sql, params=None):
        if "FROM clients" in sql:
            return [{"id": 7}]
        if "FROM trial_balance_accounts" in sql:
            return _account_rows()
        if "SELECT account_code FROM account_groups" in sql:
            return [{"account_code": "B1"}]
        if "SELECT group_label" in sql and "FROM account_groups" in sql:
            return [{"group_label": "Bank"}]
        raise AssertionError(f"Unexpected query: {sql}")

    def write(sql, params=None):
        writes.append((sql, params))

    payload = reconciliation.ReconciliationBalanceInputsUpdate(
        group_label="Bank",
        balances=[
            reconciliation.ReconciliationBalanceInput(account_code="B1", period_month="2026-01", opening_balance=0, actual_closing_balance=100),
            reconciliation.ReconciliationBalanceInput(account_code="B1", period_month="2026-02", opening_balance=0, actual_closing_balance=0),
        ],
    )
    with patch.object(reconciliation, "execute_query_pg", side_effect=query), \
         patch.object(reconciliation, "execute_write_pg", side_effect=write), \
         patch.object(reconciliation, "pg_transaction", return_value=nullcontext()), \
         patch.object(reconciliation, "log_audit"):
        response = reconciliation.replace_reconciliation_balances(7, payload)

    assert response["count"] == 2
    assert writes[0][0].startswith("DELETE FROM report_balance_inputs")
    assert writes[1][1] == (7, "Bank", "B1", "2026-01", 0.0, 100.0)


def test_replace_balances_rejects_account_outside_saved_group():
    def query(sql, params=None):
        if "FROM clients" in sql:
            return [{"id": 7}]
        if "FROM trial_balance_accounts" in sql:
            return _account_rows()
        if "SELECT account_code FROM account_groups" in sql:
            return [{"account_code": "B1"}]
        raise AssertionError(f"Unexpected query: {sql}")

    payload = reconciliation.ReconciliationBalanceInputsUpdate(
        group_label="Bank",
        balances=[
            reconciliation.ReconciliationBalanceInput(account_code="C1", period_month="2026-01", opening_balance=0, actual_closing_balance=1),
        ],
    )
    with patch.object(reconciliation, "execute_query_pg", side_effect=query):
        with pytest.raises(HTTPException) as error:
            reconciliation.replace_reconciliation_balances(7, payload)
    assert error.value.status_code == 400
    assert error.value.detail["account_codes"] == ["C1"]


def test_report_category_set_creation_is_separate_from_audit_categories():
    def query(sql, params=None):
        if "FROM clients" in sql:
            return [{"id": 7}]
        if "FROM report_category_sets" in sql:
            return []
        raise AssertionError(f"Unexpected query: {sql}")

    with patch.object(reconciliation, "execute_query_pg", side_effect=query), \
         patch.object(reconciliation, "execute_write_pg", return_value=31), \
         patch.object(reconciliation, "log_audit"):
        response = reconciliation.create_report_category_set(
            7,
            reconciliation.ReportCategorySetCreate(
                purpose_key="Ledger Summary",
                set_name="Ledger Summary Categories",
            ),
        )

    assert response["id"] == 31
    assert response["purpose_key"] == "ledger_summary"
    assert response["category_count"] == 0


def test_report_categories_validate_parent_and_replace_active_definitions():
    writes = []

    def query(sql, params=None):
        if "FROM report_category_sets" in sql:
            return [{"id": 31, "client_id": 7, "purpose_key": "ledger_summary", "set_name": "Ledger", "is_active": True}]
        raise AssertionError(f"Unexpected query: {sql}")

    def write(sql, params=None):
        writes.append((sql, params))

    with patch.object(reconciliation, "execute_query_pg", side_effect=query), \
         patch.object(reconciliation, "execute_write_pg", side_effect=write), \
         patch.object(reconciliation, "pg_transaction", return_value=nullcontext()), \
         patch.object(reconciliation, "log_audit"):
        response = reconciliation.replace_report_categories(
            31,
            reconciliation.ReportCategoryDefinitionsUpdate(
                categories=[
                    reconciliation.ReportCategoryDefinition(category_key="Operating", label="Operating"),
                    reconciliation.ReportCategoryDefinition(
                        category_key="Operating Expense",
                        label="Operating Expense",
                        parent_category_key="Operating",
                        sort_order=1,
                    ),
                ]
            ),
        )

    assert response["count"] == 2
    assert response["categories"][1]["parent_category_key"] == "operating"
    assert sum("INSERT INTO report_categories" in sql for sql, _ in writes) == 2


def test_report_assignments_validate_trial_balance_codes_and_category_keys():
    writes = []

    def query(sql, params=None):
        if "FROM report_category_sets" in sql:
            return [{"id": 31, "client_id": 7, "purpose_key": "ledger_summary", "set_name": "Ledger", "is_active": True}]
        if "FROM trial_balance_accounts" in sql:
            return _account_rows()
        if "FROM report_categories" in sql:
            return [{"id": 101, "category_key": "bank"}]
        raise AssertionError(f"Unexpected query: {sql}")

    def write(sql, params=None):
        writes.append((sql, params))

    with patch.object(reconciliation, "execute_query_pg", side_effect=query), \
         patch.object(reconciliation, "execute_write_pg", side_effect=write), \
         patch.object(reconciliation, "pg_transaction", return_value=nullcontext()), \
         patch.object(reconciliation, "log_audit"):
        response = reconciliation.replace_report_category_assignments(
            31,
            reconciliation.ReportCategoryAssignmentsUpdate(
                assignments=[
                    reconciliation.ReportAccountCategoryAssignment(account_code="B1", category_key="Bank"),
                ]
            ),
        )

    assert response["count"] == 1
    assert writes[0][0].startswith("DELETE FROM report_account_category_assignments")
    assert writes[1][1] == (7, 31, "B1", 101)

    with pytest.raises(HTTPException) as duplicate:
        reconciliation._clean_report_assignments(
            reconciliation.ReportCategoryAssignmentsUpdate(
                assignments=[
                    reconciliation.ReportAccountCategoryAssignment(account_code="B1", category_key="Bank"),
                    reconciliation.ReportAccountCategoryAssignment(account_code="B1", category_key="Bank"),
                ]
            )
        )
    assert duplicate.value.status_code == 400


def test_reconciliation_configuration_screen_uses_delegated_actions_and_exact_code_controls():
    html = Path(__file__).resolve().parents[1].joinpath("static", "reconciliation.html").read_text(encoding="utf-8")
    javascript = Path(__file__).resolve().parents[1].joinpath("static", "reconciliation.js").read_text(encoding="utf-8")

    assert 'id="accountTableBody"' in html
    assert 'id="saveButton"' in html
    assert "account-suggestions?client_id=" in javascript
    assert "account_codes: Array.from(state.selectedCodes)" in javascript
    assert "onclick=" not in html
    assert "onchange=" not in html
