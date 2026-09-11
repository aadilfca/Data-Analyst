import os
import sqlite3
import tempfile
from contextlib import nullcontext
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
            finally:
                conn.close()
            assert {"client_id", "group_label", "account_code", "is_active"}.issubset(columns)
            assert "idx_account_groups_client" in indexes
            assert "idx_account_groups_client_label" in indexes
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
