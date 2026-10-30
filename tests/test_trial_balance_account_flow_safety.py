"""Isolated contracts for Trial Balance, ACC, Verification, and report data flow."""

from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from fastapi import HTTPException

from routers import account_control_center, audit_rules, reconciliation, trial_balance
from services.ledger_report_source import _load_account_rows


AMOUNT_FIELDS = (
    "opening_dr",
    "opening_cr",
    "period_dr",
    "period_cr",
    "closing_dr",
    "closing_cr",
)


def test_trial_balance_retains_source_amounts_but_acc_mapping_excludes_them():
    source_accounts = [
        {
            "id": 1,
            "account_code": "100",
            "source_account_code": "100",
            "account_name": "Assets",
            "level": 1,
            "classification": "parent",
            "manually_overridden": False,
            "opening_dr": 900,
            "opening_cr": 0,
            "period_dr": 0,
            "period_cr": 0,
            "closing_dr": 900,
            "closing_cr": 0,
        },
        {
            "id": 2,
            "account_code": "100-01",
            "source_account_code": "100-01",
            "account_name": "Cash",
            "level": 2,
            "classification": "leaf",
            "manually_overridden": False,
            "opening_dr": 100,
            "opening_cr": 0,
            "period_dr": 25,
            "period_cr": 5,
            "closing_dr": 120,
            "closing_cr": 0,
        },
    ]
    with patch.object(trial_balance, "_require_upload", return_value={}), patch.object(
        trial_balance, "execute_query_pg", side_effect=[source_accounts, []]
    ):
        saved = trial_balance.list_trial_balance_accounts(upload_id=55)

    imported_leaf = next(row for row in saved["accounts"] if row["account_code"] == "100-01")
    assert {field: imported_leaf[field] for field in AMOUNT_FIELDS} == {
        "opening_dr": 100,
        "opening_cr": 0,
        "period_dr": 25,
        "period_cr": 5,
        "closing_dr": 120,
        "closing_cr": 0,
    }

    mapping_tree = [
        {
            "id": 2,
            "account_code": "100-01",
            "account_name": "Cash",
            "parent_account_code": "100",
            "level": 2,
            "classification": "leaf",
            **{field: 999 for field in AMOUNT_FIELDS},
        }
    ]
    with patch.object(account_control_center, "_selected_control_accounts", return_value=({"id": 55}, mapping_tree)), patch.object(
        account_control_center,
        "execute_query_pg",
        return_value=[{
            "account_code": "100-01",
            "verification_workstream": "cash",
            "financial_statement_category_id": 31,
            "note_category_id": 42,
        }],
    ), patch.object(account_control_center, "_control_upload_options", return_value=[]):
        mapping = account_control_center.get_account_control_mapping(
            client_id=7, trial_balance_upload_id=55, session={"username": "Admin"}
        )

    mapped_account = mapping["accounts"][0]
    assert mapped_account["account_code"] == "100-01"
    assert mapped_account["assignment"] == {
        "verification_workstream": "cash",
        "financial_statement_category_id": 31,
        "note_category_id": 42,
    }
    assert not set(AMOUNT_FIELDS).intersection(mapped_account)


def test_acc_rejects_upload_from_another_client_or_unparsed_upload():
    with patch.object(account_control_center, "_require_active_client"), patch.object(
        account_control_center, "execute_query_pg", return_value=[]
    ) as query:
        try:
            account_control_center._selected_control_accounts(client_id=7, upload_id=900)
        except HTTPException as error:
            assert error.status_code == 400
        else:
            raise AssertionError("A missing, foreign-client, or unparsed upload was accepted")

    sql, params = query.call_args.args
    assert "WHERE id = %s AND client_id = %s AND status = 'parsed'" in sql
    assert params == (900, 7)


@pytest.mark.parametrize(
    "target_assignment, expected_status",
    [
        (None, "copied"),
        ({"verification_workstream": "bank", "financial_statement_category_id": 32, "note_category_id": None}, 409),
    ],
)
def test_account_code_correction_carries_assignments_without_overwriting_conflicts(target_assignment, expected_status):
    source_assignment = {
        "verification_workstream": "cash",
        "financial_statement_category_id": 31,
        "note_category_id": 41,
    }

    def query(sql, params):
        if "FROM account_control_assignments" in sql:
            return [source_assignment] if params == (7, "OLD-1") else ([target_assignment] if target_assignment else [])
        return []

    with patch.object(trial_balance, "execute_query_pg", side_effect=query), patch.object(
        trial_balance, "execute_write_pg"
    ) as write:
        if expected_status == 409:
            with pytest.raises(HTTPException) as raised:
                trial_balance._copy_shared_code_assignments(7, "OLD-1", "NEW-1")
            assert raised.value.status_code == expected_status
            assert not any("account_control_assignments" in call.args[0] for call in write.call_args_list)
        else:
            trial_balance._copy_shared_code_assignments(7, "OLD-1", "NEW-1")
            copied = next(call for call in write.call_args_list if "account_control_assignments" in call.args[0])
            assert "INSERT INTO account_control_assignments" in copied.args[0]
            assert copied.args[1] == (7, "NEW-1", "cash", 31, 41)


def test_financial_statement_uses_acc_categories_and_surfaces_unmapped_ledger_activity():
    queries = []

    def query(sql, params):
        queries.append((sql, params))
        if "FROM trial_balance_accounts a" in sql:
            return [
                {"account_code": "EXP-1", "account_name": "Office expense", "source_account_type": "Expense"},
                {"account_code": "CASH-1", "account_name": "Cash", "source_account_type": "Cash"},
            ]
        if "FROM account_control_assignments a" in sql:
            return [{"account_code": "EXP-1", "category_name": "Operating expenses"}]
        raise AssertionError(f"Unexpected report account query: {sql}")

    accounts = _load_account_rows(7, query, [], category_source="financial_statement")
    assert next(row for row in accounts if row["account_code"] == "EXP-1")["category"] == "Operating expenses"
    assert next(row for row in accounts if row["account_code"] == "CASH-1")["category"] == ""
    assignment_sql, assignment_params = queries[1]
    assert "a.financial_statement_category_id" in assignment_sql
    assert "c.category_type = 'financial_statement'" in assignment_sql
    assert "c.is_active = TRUE" in assignment_sql
    assert assignment_params == (7,)

    source = SimpleNamespace(
        ready=True,
        diagnostics=(),
        metadata={"source_line_count": 2},
        journal=pd.DataFrame([
            {"voucher_id": "V1", "date": "2026-06-15", "account_code": "EXP-1", "account_name": "Office expense", "debit": 125, "credit": 0},
            {"voucher_id": "V2", "date": "2026-06-18", "account_code": "NEW-1", "account_name": "New account", "debit": 25, "credit": 0},
        ]),
        coa=pd.DataFrame(accounts),
    )
    with patch.object(reconciliation, "_require_active_client"), patch.object(
        reconciliation, "load_ledger_report_source", return_value=source
    ) as load_source:
        result = reconciliation._build_financial_statement_result(
            7, "2026-06-01", "2026-06-30", journal_upload_id=81
        )

    assert load_source.call_args.kwargs["account_category_source"] == "financial_statement"
    assert result["controls"]["financial_statement_mapping"]["status"] == "Review"
    assert result["preview"]["category_totals"][0]["2026-06"] == 125
    assert result["preview"]["unmapped_account_count"] == 1
    assert result["preview"]["unmapped_accounts"][0]["account_code"] == "NEW-1"


def test_verification_category_resolution_prefers_latest_trial_balance_then_uses_fallback():
    def query(sql, params):
        if "FROM trial_balance_uploads" in sql:
            return [{"id": 300}]
        if "FROM trial_balance_accounts a" in sql:
            return [{"account_code": "TB-CODE", "category_id": 51, "category_name": "TB Category"}]
        if "FROM account_category_assignments aca" in sql:
            assert params == (7, "OLD-CODE")
            return [{"account_code": "OLD-CODE", "category_id": 52, "category_name": "Carried category"}]
        raise AssertionError(f"Unexpected category query: {sql}")

    with patch.object(audit_rules, "execute_query_pg", side_effect=query):
        resolved = audit_rules.load_client_account_category_index(7, ["TB-CODE", "OLD-CODE"])

    assert resolved == {
        "TB-CODE": {"category_id": 51, "category_name": "TB Category"},
        "OLD-CODE": {"category_id": 52, "category_name": "Carried category"},
    }


def test_balance_sheet_uses_leaf_closing_fields_without_changing_acc_metadata_contract():
    from services.financial_statement_balances import build_balance_sheet
    account = dict(account_code='100', classification='leaf', manually_overridden=False,
                   closing_dr='120.50', closing_cr='0', opening_dr='100', opening_cr='0', period_dr='20.50', period_cr='0')
    category = dict(id=31, label='Cash', row_type='category', parent_category_id=None,
                    statement_section='balance_sheet', presentation_role='asset', is_active=True)
    result = build_balance_sheet([account], [dict(account_code='100', financial_statement_category_id=31)], [category], [], [])
    assert result['categories'][0]['amount'] == '120.50'
    assert result['controls']['audit_adjustment_net'] == '0'
    assert account['closing_dr'] == '120.50'
