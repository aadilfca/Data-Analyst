import os
import sqlite3
import tempfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pandas as pd
from fastapi import HTTPException

import database
from routers import account_control_center, reconciliation
from services.reconciliation_xlsx import _cashflow_section_records, build_cashflow_workbook


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
                role_columns = {row[1] for row in conn.execute("PRAGMA table_info(account_control_cashflow_role_categories)")}
                role_indexes = {row[1] for row in conn.execute("PRAGMA index_list(account_control_cashflow_role_categories)")}
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
            assert {"client_id", "role_key", "category_id"}.issubset(role_columns)
            assert "idx_account_control_cashflow_role_categories_client" in role_indexes
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


def test_cashflow_role_categories_are_deduplicated_and_sorted():
    payload = account_control_center.CashFlowSettingsPayload(
        client_id=5,
        customer_receipt_category_ids=[9, 3, 9, 0],
        bank_charge_category_ids=[7, 2, 7],
        withholding_tax_category_ids=[4],
        other_deduction_category_ids=[],
    )
    assert account_control_center._clean_cashflow_role_categories(payload) == {
        "customer_receipt": [3, 9],
        "bank_charge": [2, 7],
        "withholding_tax": [4],
        "other_deduction": [],
    }


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


def test_reports_hub_uses_shared_cards_and_removes_legacy_workspace():
    html = Path(__file__).resolve().parents[1].joinpath("static", "reconciliation.html").read_text(encoding="utf-8")
    javascript = Path(__file__).resolve().parents[1].joinpath("static", "reconciliation.js").read_text(encoding="utf-8")

    assert "id='reportsView'" in html
    assert "id='cashflowsCard'" in html
    assert "data-report-key='cashflows'" in html
    assert "Vendor Reports" in html
    assert "Customer Reports" in html
    assert "Financial Statements" in html
    assert "notes section" in html
    assert "data-report-key" in javascript
    assert "id='cashflowsView'" in html
    assert "/api/reconciliation/cashflows/preview" in javascript
    assert "cashflowsSourceSelect" not in html
    assert "Loading Cashflows summary" in html
    assert "Load summary" in html
    assert "loadCashflowSources" not in javascript
    assert "journal_upload_id" not in javascript
    assert "cashflowsSummaryTable" in html
    assert "reportsHubMessage" not in html
    assert "CASHFLOW_REQUEST_TIMEOUT_MS" in javascript
    assert "AbortController" in javascript
    assert "signal: controller.signal" in javascript
    assert "AbortError" in javascript
    assert "Cashflows is selected" not in javascript
    for legacy_id in (
        "id='reportForm'",
        "id='setupView'",
        "id='reportDiagnosticsPanel'",
        "id='reportPreviewPanel'",
        "id='financialStatementPreviewPanel'",
        "id='balanceForm'",
    ):
        assert legacy_id not in html
    assert "onclick=" not in html
    assert "onchange=" not in html


def test_cashflow_summary_nets_customer_receipt_deductions_and_keeps_detail():
    journal = pd.DataFrame([
        {"voucher_id": "V1", "date": "2026-01-15", "account_code": "B1", "account_name": "Bank", "debit": 100, "credit": 0},
        {"voucher_id": "V1", "date": "2026-01-15", "account_code": "B1", "account_name": "Bank", "debit": 0, "credit": 3},
        {"voucher_id": "V1", "date": "2026-01-15", "account_code": "CH", "account_name": "Bank charges", "debit": 1, "credit": 0},
        {"voucher_id": "V1", "date": "2026-01-15", "account_code": "WT", "account_name": "Withholding tax", "debit": 2, "credit": 0},
        {"voucher_id": "V1", "date": "2026-01-15", "account_code": "CU", "account_name": "Customer", "debit": 0, "credit": 100},
    ])
    coa = pd.DataFrame([
        {"account_code": "CH", "account_title": "Bank charges", "category": "Bank charges"},
        {"account_code": "WT", "account_title": "Withholding tax", "category": "Withholding tax"},
        {"account_code": "CU", "account_title": "Customer", "category": "Customer receipts"},
    ])
    summary = reconciliation.build_cashflow_summary(journal, coa, [], ["B1"], "2026-01-01", "2026-01-31")
    assert summary["category_totals"].loc["Customer receipts", "2026-01"] == 97
    assert "Bank charges" not in summary["category_totals"].index
    assert sorted(summary["deduction_details"]["category"].tolist()) == ["Bank charges", "Withholding tax"]


def test_cashflow_summary_matches_same_type_internal_transfers():
    journal = pd.DataFrame([
        {"voucher_id": "T1", "date": "2026-01-20", "account_code": "B1", "account_name": "Bank A", "debit": 0, "credit": 100},
        {"voucher_id": "T1", "date": "2026-01-20", "account_code": "B2", "account_name": "Bank B", "debit": 100, "credit": 0},
    ])
    summary = reconciliation.build_cashflow_summary(journal, pd.DataFrame(columns=["account_code", "category"]), [], ["B1", "B2"], "2026-01-01", "2026-01-31", match_internal_transfers=True)
    assert bool(summary["transfer_details"].iloc[0]["matched"]) is True
    assert summary["category_totals"].empty


def test_cashflow_summary_treasury_totals_reconcile_to_combined_category_total():
    journal = pd.DataFrame([
        {"voucher_id": "V1", "date": "2026-01-05", "account_code": "B1", "account_name": "Bank", "debit": 100, "credit": 0},
        {"voucher_id": "V1", "date": "2026-01-05", "account_code": "EXP", "account_name": "Operating", "debit": 0, "credit": 100},
        {"voucher_id": "V2", "date": "2026-01-10", "account_code": "C1", "account_name": "Cash", "debit": 0, "credit": 40},
        {"voucher_id": "V2", "date": "2026-01-10", "account_code": "EXP", "account_name": "Operating", "debit": 40, "credit": 0},
        {"voucher_id": "V3", "date": "2026-01-20", "account_code": "B1", "account_name": "Bank", "debit": 60, "credit": 0},
        {"voucher_id": "V3", "date": "2026-01-20", "account_code": "C1", "account_name": "Cash", "debit": 40, "credit": 0},
        {"voucher_id": "V3", "date": "2026-01-20", "account_code": "EXP", "account_name": "Operating", "debit": 0, "credit": 100},
    ])
    coa = pd.DataFrame([
        {"account_code": "EXP", "account_title": "Operating", "category": "Operating"},
    ])

    summary = reconciliation.build_cashflow_summary(
        journal,
        coa,
        ["C1"],
        ["B1"],
        "2026-01-01",
        "2026-01-31",
    )

    from services.ledger_report_source import _cashflow_summary_from_payload, _cashflow_summary_payload

    payload = _cashflow_summary_payload(summary)
    assert payload["treasury_totals_version"] == 1
    restored = _cashflow_summary_from_payload(payload, "month")
    split = restored["treasury_category_totals"].set_index(["category", "period_key"]).loc[("Operating", "2026-01")]
    legacy = _cashflow_summary_from_payload({"periods": payload["periods"], "categories": payload["categories"]}, "month")
    assert legacy["treasury_totals_version"] == 0
    assert legacy["treasury_category_totals"].empty
    assert summary["category_totals"].loc["Operating", "2026-01"] == 160
    assert split["bank_amount"] == 160
    assert split["cash_amount"] == 0
    assert split["mixed_amount"] == 0
    assert split["total_amount"] == 160
    assert summary["treasury_reconciliation"]["status"] == "Matched"
    assert summary["treasury_reconciliation"]["max_abs_variance"] == 0

    bank_preview = reconciliation._cashflow_preview(summary, set(), "bank")
    cash_preview = reconciliation._cashflow_preview(summary, set(), "cash")
    split_preview = reconciliation._cashflow_preview(summary, set(), "split")
    assert bank_preview["treasury_view"] == "bank"
    assert bank_preview["treasury_view_ready"] is True
    assert bank_preview["treasury_view_totals"][0]["2026-01"] == 160
    assert cash_preview["treasury_view_totals"][0]["2026-01"] == 0
    assert split_preview["treasury_view_totals"][0]["total_amount"] == 160
    assert split_preview["treasury_view_reconciliation"]["status"] == "Matched"


def test_cashflow_mixed_signs_are_preserved_for_any_selected_period_grain():
    preview = {
        "periods": [
            {"key": "day-1", "label": "Day 1"},
            {"key": "fortnight-1", "label": "Fortnight 1"},
            {"key": "quarter-1", "label": "Quarter 1"},
        ],
        "category_totals": [
            {"category": "ALI (related party)", "day-1": 0, "fortnight-1": -3000000, "quarter-1": 3000000, "Total": 0},
            {"category": "Mixed payment", "day-1": -100, "fortnight-1": 50, "quarter-1": -50, "Total": -100},
        ],
    }

    receipts = _cashflow_section_records(preview, ["day-1", "fortnight-1", "quarter-1"], "Receipts")
    payments = _cashflow_section_records(preview, ["day-1", "fortnight-1", "quarter-1"], "Payments")
    assert receipts == [("ALI (related party)", [0.0, -3000000.0, 3000000.0], 0.0)]
    assert payments == [("Mixed payment", [-100.0, 50.0, -50.0], 100.0)]

    workbook = build_cashflow_workbook(preview)
    sheet = workbook["Cashflows Summary"]
    assert sheet["C6"].value == -3000000
    assert sheet["C6"].number_format == '#,##0;[Red](#,##0);-'
    assert sheet["C12"].value == -50
    assert sheet["C12"].number_format == '#,##0;[Red](#,##0);-'


def test_cashflow_treasury_view_cache_requires_versioned_payload_for_noncombined_views():
    legacy = {"metadata": {"treasury_totals_version": 0}}
    current = {"metadata": {"treasury_totals_version": 1}}

    assert reconciliation._cashflow_cache_can_serve_details(legacy, set(), "combined") is True
    assert reconciliation._cashflow_cache_can_serve_details(legacy, set(), "bank") is False
    assert reconciliation._cashflow_cache_can_serve_details(current, set(), "bank") is True
    with pytest.raises(ValueError):
        reconciliation.normalize_cashflow_treasury_view("unknown")


def test_cashflow_preview_endpoint_returns_configuration_and_preview():
    source = SimpleNamespace(journal=pd.DataFrame(), coa=pd.DataFrame(), diagnostics=(), metadata={"source_row_count": 0}, ready=True)
    configuration = {"category_mappings": {}, "cash_account_codes": ["C1"], "bank_account_codes": [], "match_internal_transfers": False, "receipt_presentation": "net", "source_template_id": 1}
    with patch.object(reconciliation, "_require_active_client"), patch.object(reconciliation, "_cashflow_configuration", return_value=configuration), patch.object(reconciliation, "load_ledger_report_source", return_value=source):
        response = reconciliation.preview_cashflows(7, "2026-01-01", "2026-01-31", session={"username": "Admin"})
    assert response["configuration"]["cash_account_count"] == 1
    assert response["preview"]["category_totals"] == []
    assert response["controls"]["treasury"]["status"] == "Ready"


def test_cashflow_sources_returns_explicit_upload_choices():
    rows = [{
        "upload_id": 290,
        "template_id": 1,
        "template_name": "Voucher template",
        "file_name": "June vouchers.xlsx",
        "source_row_count": 8173,
        "period_start": "2026-06-01",
        "period_end": "2026-06-30",
    }]
    with patch.object(reconciliation, "_require_active_client"), patch.object(
        reconciliation, "execute_query_pg", return_value=rows
    ):
        response = reconciliation.list_cashflow_sources(7, session={"username": "Admin"})

    assert response == {
        "client_id": 7,
        "sources": [{
            "selector_type": "upload",
            "selector_id": 290,
            "upload_id": 290,
            "template_id": 1,
            "template_name": "Voucher template",
            "file_name": "June vouchers.xlsx",
            "source_row_count": 8173,
            "period_start": "2026-06-01",
            "period_end": "2026-06-30",
        }],
    }


def test_cashflow_template_columns_returns_saved_roles():
    template_columns = [
        {"template_column_id": 15, "column_name": "Voucher ID", "data_type": "Text", "sort_order": 0},
        {"template_column_id": 12, "column_name": "Transaction Date", "data_type": "Date", "sort_order": 1},
        {"template_column_id": 16, "column_name": "Account Detail", "data_type": "Text", "sort_order": 2},
        {"template_column_id": 19, "column_name": "Debit Amount", "data_type": "Numeric", "sort_order": 5},
        {"template_column_id": 20, "column_name": "Credit Amount", "data_type": "Numeric", "sort_order": 6},
    ]
    saved = [
        {"role": "voucher_id", "template_column_id": 15, "column_name": "Voucher ID"},
        {"role": "date", "template_column_id": 12, "column_name": "Transaction Date"},
        {"role": "account_code", "template_column_id": 16, "column_name": "Account Detail"},
        {"role": "debit", "template_column_id": 19, "column_name": "Debit Amount"},
        {"role": "credit", "template_column_id": 20, "column_name": "Credit Amount"},
    ]

    def query(sql, params=None):
        if "FROM templates" in sql:
            return [{"id": 1, "template_name": "Transaction Report", "is_system": True}]
        if "FROM template_column_mappings" in sql:
            return template_columns
        if "FROM account_control_cashflow_source_mappings" in sql:
            return saved
        raise AssertionError(f"Unexpected query: {sql}")

    with patch.object(reconciliation, "_require_active_client"), patch.object(reconciliation, "execute_query_pg", side_effect=query):
        response = reconciliation.get_cashflow_template_columns(7, 1, session={"username": "Admin"})

    assert response["template"]["template_name"] == "Transaction Report"
    assert response["ready"] is True
    assert response["missing_required_roles"] == []
    assert next(item for item in response["roles"] if item["role"] == "account_code")["column_name"] == "Account Detail"


def test_save_cashflow_template_columns_validates_and_persists_mapping():
    writes = []

    def query(sql, params=None):
        if "SELECT id FROM templates" in sql:
            return [{"id": 1}]
        if "FROM template_column_mappings" in sql:
            return [{"template_column_id": column_id} for column_id in (15, 12, 16, 19, 20)]
        raise AssertionError(f"Unexpected query: {sql}")

    def write(sql, params=None):
        writes.append((sql, params))

    mapping = reconciliation.CashflowTemplateMappingUpdate(
        client_id=7,
        template_id=1,
        mappings=[
            reconciliation.CashflowColumnMappingEntry(role="voucher_id", template_column_id=15),
            reconciliation.CashflowColumnMappingEntry(role="date", template_column_id=12),
            reconciliation.CashflowColumnMappingEntry(role="account_code", template_column_id=16),
            reconciliation.CashflowColumnMappingEntry(role="debit", template_column_id=19),
            reconciliation.CashflowColumnMappingEntry(role="credit", template_column_id=20),
        ],
    )
    with patch.object(reconciliation, "_require_active_client"), patch.object(reconciliation, "execute_query_pg", side_effect=query), patch.object(reconciliation, "execute_write_pg", side_effect=write), patch.object(reconciliation, "pg_transaction", return_value=nullcontext()), patch.object(reconciliation, "_cashflow_template_mapping_view", return_value={"ready": True}), patch.object(reconciliation, "log_audit"):
        response = reconciliation.save_cashflow_template_columns(mapping, session={"username": "Admin"})

    assert response["ready"] is True
    assert sum("INSERT INTO account_control_cashflow_source_mappings" in sql for sql, _ in writes) == 5
    assert any("source_template_id" in sql for sql, _ in writes)


def test_cashflow_source_selection_requires_one_source_and_persists_upload():
    with patch.object(reconciliation, "_require_active_client"), patch.object(reconciliation, "execute_query_pg", return_value=[{"exists": 1}]), patch.object(reconciliation, "execute_write_pg") as write, patch.object(reconciliation, "pg_transaction", return_value=nullcontext()), patch.object(reconciliation, "_cashflow_configuration", return_value={"source_upload_id": 290}), patch.object(reconciliation, "log_audit"):
        response = reconciliation.save_cashflow_source(
            reconciliation.CashflowSourceSelectionUpdate(client_id=7, upload_id=290),
            session={"username": "Admin"},
        )

    assert response["source_upload_id"] == 290
    assert any("source_upload_id" in call.args[0] for call in write.call_args_list)
    with patch.object(reconciliation, "_require_active_client"):
        with pytest.raises(HTTPException) as error:
            reconciliation.save_cashflow_source(
                reconciliation.CashflowSourceSelectionUpdate(client_id=7, upload_id=290, template_id=1),
                session={"username": "Admin"},
            )
    assert error.value.status_code == 400


def test_cashflow_preview_uses_configured_transaction_template():
    source = SimpleNamespace(journal=pd.DataFrame(), coa=pd.DataFrame(), diagnostics=(), metadata={"source_row_count": 9859}, ready=True)
    configuration = {
        "category_mappings": {},
        "cash_account_codes": ["C1"],
        "bank_account_codes": [],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
        "source_upload_id": 290,
        "source_template_id": 1,
    }
    captured = {}

    def load(*args, **kwargs):
        captured.update(kwargs)
        return source

    with patch.object(reconciliation, "_require_active_client"), patch.object(reconciliation, "_cashflow_configuration", return_value=configuration), patch.object(reconciliation, "load_ledger_report_source", side_effect=load):
        response = reconciliation.preview_cashflows(7, "2026-01-01", "2026-08-20", session={"username": "Admin"})

    assert captured.get("journal_upload_id") is None
    assert captured["journal_template_id"] == 1
    assert response["configuration"]["source_template_id"] == 1
    assert "source_upload_id" not in response["configuration"]

def test_cashflow_detail_sections_parser_and_summary_filter():
    assert reconciliation._parse_cashflow_detail_sections(None) is None
    assert reconciliation._parse_cashflow_detail_sections("summary") == set()
    assert reconciliation._parse_cashflow_detail_sections("transfers, review") == {"transfers", "review"}
    with pytest.raises(HTTPException) as error:
        reconciliation._parse_cashflow_detail_sections("unknown")
    assert error.value.status_code == 400

    summary = {
        "months": ["2026-01"],
        "category_totals": pd.DataFrame(),
        "transfer_details": pd.DataFrame([{"voucher_id": "V1"}]),
        "deduction_details": pd.DataFrame([{"category": "Charge"}]),
        "categories_needing_review": pd.DataFrame([{"voucher_id": "V1"}]),
        "selected_voucher_count": 1,
        "internal_transfer_count": 1,
        "category_review_count": 1,
        "receipt_presentation": "net",
    }
    preview = reconciliation._cashflow_preview(summary, set())
    assert preview["transfer_details"] == []
    assert preview["deduction_details"] == []
    assert preview["categories_needing_review"] == []
    assert preview["internal_transfer_count"] == 1
    assert preview["category_review_count"] == 1
    assert preview["deduction_count"] == 1
    assert preview["detail_sections_loaded"] == []

def test_cashflow_treasury_view_selector_is_wired_to_preview():
    html = Path(__file__).resolve().parents[1].joinpath("static", "reconciliation.html").read_text(encoding="utf-8")
    javascript = Path(__file__).resolve().parents[1].joinpath("static", "reconciliation.js").read_text(encoding="utf-8")

    assert "id='cashflowsTreasuryViewSelect'" in html
    assert "value='combined'" in html
    assert "value='bank'" in html
    assert "value='cash'" in html
    assert "value='split'" in html
    assert "treasury_view: cashflowTreasuryView" in javascript
    assert "cashflowSummaryRows(preview)" in javascript
    assert "cashflowTreasuryViewLabel(" in javascript


def test_cashflow_preview_endpoint_passes_selected_treasury_view():
    with patch.object(reconciliation, "_build_cashflow_result", return_value={"ok": True}) as build:
        response = reconciliation.preview_cashflows(
            7,
            "2026-01-01",
            "2026-01-31",
            treasury_view="bank",
            session={"username": "Admin"},
        )

    assert response == {"ok": True}
    assert build.call_args.kwargs["treasury_view"] == "bank"

def test_cashflow_split_view_preserves_backend_reconciliation_contract():
    html = Path(__file__).resolve().parents[1].joinpath("static", "reconciliation.html").read_text(encoding="utf-8")
    javascript = Path(__file__).resolve().parents[1].joinpath("static", "reconciliation.js").read_text(encoding="utf-8")

    assert "cashflowsTreasuryReconciliation" not in html
    assert "renderCashflowSplitSummary" in javascript
    assert "Mixed / unresolved" in javascript
    assert "const showMixedColumn = allRows.some" in javascript
    assert "const visibleFields = showMixedColumn ?" in javascript
    assert "renderCashflowTreasuryReconciliation" not in javascript
    assert "cashflows-summary-split-table" in javascript

def test_cashflow_split_xlsx_preserves_bank_cash_mixed_and_total_cells():
    preview = {
        "periods": [{"key": "2026-01", "label": "Jan-26"}],
        "group_by": "month",
        "treasury_view": "split",
        "treasury_view_totals": [{
            "category": "Operating",
            "period_key": "2026-01",
            "bank_amount": 100,
            "cash_amount": 20,
            "mixed_amount": 5,
            "total_amount": 125,
        }],
        "treasury_view_reconciliation": {"status": "Matched", "max_abs_variance": 0, "mixed_amount": 5, "mixed_row_count": 1},
        "receipt_presentation": "net",
    }

    workbook = build_cashflow_workbook(preview)
    sheet = workbook["Cashflows Summary"]

    assert sheet["A5"].value == "Category"
    assert sheet["B5"].value == "Jan-26"
    assert sheet["B6"].value == "Bank"
    assert sheet["D6"].value == "Mixed / unresolved"
    assert sheet["E6"].value == "Total"
    assert sheet["B7"].value == 100
    assert sheet["C7"].value == 20
    assert sheet["D7"].value == 5
    assert sheet["E7"].value == 125
    assert sheet["I7"].value == 125
    assert sheet["E7"].number_format == '#,##0;[Red](#,##0);-'


def test_cashflow_drilldown_applies_selected_bank_view_filter_by_default():
    result = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "group_by": "month",
        "treasury_view": "bank",
        "preview": {
            "ledger_rollups": [{
                "category": "Operating",
                "period_key": "2026-01",
                "period_label": "Jan-26",
                "account_name": "Bank A",
                "account_code": "B1",
                "treasury_type": "bank",
                "amount": 100,
                "line_count": 1,
            }],
            "drilldown_controls": {},
        },
        "source": {},
        "blocking_reasons": [],
    }
    with patch.object(reconciliation, "_build_cashflow_result", return_value=result) as build:
        response = reconciliation.cashflow_drilldown(
            7,
            "2026-01-01",
            "2026-01-31",
            treasury_view="bank",
            page=1,
            limit=50,
            session={"username": "Admin"},
        )

    assert build.call_args.kwargs["treasury_view"] == "bank"
    assert response["treasury_view"] == "bank"
    assert response["total_rows"] == 1


def test_cashflow_export_passes_selected_split_view_to_xlsx_and_filename():
    preview = {
        "periods": [{"key": "2026-01", "label": "Jan-26"}],
        "treasury_view": "split",
        "treasury_view_totals": [{
            "category": "Operating",
            "period_key": "2026-01",
            "bank_amount": 100,
            "cash_amount": 20,
            "mixed_amount": 5,
            "total_amount": 125,
        }],
        "category_totals": [],
        "ledger_rollups": [],
        "transfer_details": [],
        "deduction_details": [],
        "categories_needing_review": [],
        "treasury_view_reconciliation": {"status": "Matched", "max_abs_variance": 0, "mixed_amount": 5, "mixed_row_count": 1},
    }
    result = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "group_by": "month",
        "treasury_view": "split",
        "preview": preview,
        "source": {"ready": True},
        "blocking_reasons": [],
    }
    with patch.object(reconciliation, "_build_cashflow_result", return_value=result) as build, patch.object(
        reconciliation, "export_cashflow_xlsx", return_value=b"xlsx"
    ) as export:
        response = reconciliation.export_cashflows_report(
            7,
            "2026-01-01",
            "2026-01-31",
            treasury_view="split",
            session={"username": "Admin"},
        )

    assert build.call_args.kwargs["treasury_view"] == "split"
    assert export.call_args.args[0]["treasury_view"] == "split"
    assert export.call_args.args[0]["treasury_view_totals"][0]["total_amount"] == 125
    assert "Cashflows_split_month_" in response.headers["content-disposition"]
