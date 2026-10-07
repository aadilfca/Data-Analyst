import json
from io import BytesIO
from pathlib import Path
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from openpyxl import load_workbook

from routers import reconciliation
from services.ledger_group_summary import build_cashflow_summary, build_financial_statement_summary
from services.ledger_report_source import CASHFLOW_CHILD_DETAILS_VERSION, CASHFLOW_DRILLDOWN_DETAILS_VERSION
from routers import settings as settings_router
from services.reconciliation_xlsx import export_cashflow_xlsx


def _source():
    return SimpleNamespace(
        journal=pd.DataFrame(),
        coa=pd.DataFrame(),
        diagnostics=(),
        ready=True,
        metadata={"source_upload_ids": [21], "source_row_count": 4},
    )


def _balance_source(ready=True, diagnostics=()):
    return SimpleNamespace(
        opening_balances={"B1": 1000},
        actual_closing_balances={"B1": {"2026-01": 1100}},
        diagnostics=diagnostics,
        ready=ready,
        months=["2026-01"],
    )


def _summary():
    return {
        "group_label": "Bank",
        "months": ["2026-01"],
        "selected_vouchers": ["V1"],
        "internal_transfers": pd.DataFrame(),
        "categories_needing_review": pd.DataFrame(),
        "category_totals": pd.DataFrame({"2026-01": [100.0], "Total": [100.0]}, index=["Customers"]),
        "monthly_balance_checks": pd.DataFrame(
            [{"month": "2026-01", "category_total": 100.0, "group_total": 100.0, "variance": 0.0, "balanced": True}]
        ),
        "reconciliation_proof": pd.DataFrame(
            [{"month": "2026-01", "opening_balance": 1000.0, "movement": 100.0, "actual_closing_balance": 1100.0, "variance": 0.0, "status": "Matched"}]
        ),
        "reconciliation_available": True,
        "reconciliation_message": None,
    }


def _cashflow_summary():
    return {
        "months": ["2026-02", "2026-03", "2026-04"],
        "periods": [
            {"key": "2026-Q1", "start": "2026-02-10", "end": "2026-03-31", "label": "Q1 2026"},
            {"key": "2026-Q2", "start": "2026-04-01", "end": "2026-04-18", "label": "Q2 2026"},
        ],
        "group_by": "quarter",
        "category_totals": pd.DataFrame({"2026-Q1": [125.0], "2026-Q2": [50.0], "Total": [175.0]}, index=["Customers"]),
        "transfer_details": pd.DataFrame(),
        "customer_payment_details": pd.DataFrame(),
        "deduction_details": pd.DataFrame(),
        "categories_needing_review": pd.DataFrame(),
        "selected_voucher_count": 2,
        "internal_transfer_count": 0,
        "category_review_count": 0,
        "receipt_presentation": "net",
    }


def _saved_codes(_client_id, group_label=None):
    return {"B1"} if not group_label or group_label.casefold() == "bank" else set()


def test_cashflow_transfer_details_identify_legs_and_preserve_category_totals():
    journal = pd.DataFrame(
        [
            {"voucher_id": "T1", "date": "2026-01-05", "account_code": "BA", "account_name": "Bank Alpha", "voucher_detail": "Bank transfer", "debit": 0, "credit": 100},
            {"voucher_id": "T1", "date": "2026-01-05", "account_code": "BB", "account_name": "Bank Beta", "voucher_detail": "Bank transfer", "debit": 100, "credit": 0},
            {"voucher_id": "T2", "date": "2026-01-06", "account_code": "BA", "account_name": "Bank Alpha", "voucher_detail": "Transfer with fee", "debit": 0, "credit": 100},
            {"voucher_id": "T2", "date": "2026-01-06", "account_code": "BB", "account_name": "Bank Beta", "voucher_detail": "Transfer with fee", "debit": 95, "credit": 0},
            {"voucher_id": "T2", "date": "2026-01-06", "account_code": "CH", "account_name": "Bank charges", "voucher_detail": "Transfer with fee", "debit": 5, "credit": 0},
            {"voucher_id": "T3", "date": "2026-01-07", "account_code": "BA", "account_name": "Bank Alpha", "voucher_detail": "Split transfer", "debit": 0, "credit": 100},
            {"voucher_id": "T3", "date": "2026-01-07", "account_code": "BB", "account_name": "Bank Beta", "voucher_detail": "Split transfer", "debit": 60, "credit": 0},
            {"voucher_id": "T3", "date": "2026-01-07", "account_code": "BC", "account_name": "Bank Central", "voucher_detail": "Split transfer", "debit": 40, "credit": 0},
        ]
    )
    coa = pd.DataFrame(
        [
            {"account_code": "BA", "account_title": "Bank Alpha", "account_type": "Bank", "category": ""},
            {"account_code": "BB", "account_title": "Bank Beta", "account_type": "Bank", "category": ""},
            {"account_code": "BC", "account_title": "Bank Central", "account_type": "Bank", "category": ""},
            {"account_code": "CH", "account_title": "Bank charges", "account_type": "Expense", "category": "Bank charges"},
        ]
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=[],
        bank_account_codes=["BA", "BB", "BC"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        match_internal_transfers=True,
    )

    details = result["transfer_details"].set_index("voucher_id")
    assert details.loc["T1", "classification"] == "confirmed"
    assert details.loc["T1", "transferor_account_code"] == "BA"
    assert details.loc["T1", "transferor_account_name"] == "Bank Alpha"
    assert details.loc["T1", "transferee_account_code"] == "BB"
    assert details.loc["T1", "transferee_account_name"] == "Bank Beta"
    assert details.loc["T1", "amount"] == 100
    assert details.loc["T1", "variance"] == 0
    assert bool(details.loc["T1", "matched"]) is True
    assert details.loc["T2", "classification"] == "mixed"
    assert details.loc["T2", "status"] == "Review required - mixed voucher"
    assert details.loc["T2", "variance"] == -5
    assert details.loc["T3", "classification"] == "ambiguous"
    assert details.loc["T3", "transferee_account_code"] == "BB / BC"
    assert details.loc["T3", "status"] == "Review required - multi-leg or unbalanced"
    assert result["category_totals"].loc["Bank charges", "2026-01"] == -5
    assert result["transfer_reconciliation"] == {
        "candidate_voucher_count": 3,
        "confirmed_count": 1,
        "mixed_count": 1,
        "ambiguous_count": 1,
        "transferor_total": 300.0,
        "transferee_total": 295.0,
        "variance_total": -5.0,
        "status": "Review required",
    }


def test_cashflow_transfer_reconciliation_confirms_cash_and_bank_routes():
    journal = pd.DataFrame(
        [
            {"voucher_id": "BANK-BANK", "date": "2026-01-01", "account_code": "BA", "account_name": "Bank Alpha", "voucher_detail": "Transfer", "debit": 0, "credit": 50},
            {"voucher_id": "BANK-BANK", "date": "2026-01-01", "account_code": "BB", "account_name": "Bank Beta", "voucher_detail": "Transfer", "debit": 50, "credit": 0},
            {"voucher_id": "BANK-CASH", "date": "2026-01-02", "account_code": "BA", "account_name": "Bank Alpha", "voucher_detail": "Transfer", "debit": 0, "credit": 75},
            {"voucher_id": "BANK-CASH", "date": "2026-01-02", "account_code": "CA", "account_name": "Cash Alpha", "voucher_detail": "Transfer", "debit": 75, "credit": 0},
            {"voucher_id": "CASH-BANK", "date": "2026-01-03", "account_code": "CA", "account_name": "Cash Alpha", "voucher_detail": "Transfer", "debit": 0, "credit": 25},
            {"voucher_id": "CASH-BANK", "date": "2026-01-03", "account_code": "BB", "account_name": "Bank Beta", "voucher_detail": "Transfer", "debit": 25, "credit": 0},
        ]
    )
    coa = pd.DataFrame(
        [
            {"account_code": "BA", "account_title": "Bank Alpha", "account_type": "Bank", "category": ""},
            {"account_code": "BB", "account_title": "Bank Beta", "account_type": "Bank", "category": ""},
            {"account_code": "CA", "account_title": "Cash Alpha", "account_type": "Cash", "category": ""},
        ]
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=["CA"],
        bank_account_codes=["BA", "BB"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        match_internal_transfers=True,
    )

    assert result["transfer_reconciliation"]["status"] == "Matched"
    assert result["transfer_reconciliation"]["candidate_voucher_count"] == 3
    assert result["transfer_reconciliation"]["confirmed_count"] == 3
    assert result["transfer_reconciliation"]["variance_total"] == 0.0


def test_cashflow_customer_payment_details_reconcile_gross_and_net_methods():
    journal = pd.DataFrame(
        [
            {"voucher_id": "GROSS", "date": "2026-01-01", "account_code": "BANK", "account_name": "Export Bank", "voucher_detail": "Gross receipt", "debit": 100, "credit": 3},
            {"voucher_id": "GROSS", "date": "2026-01-01", "account_code": "CHG", "account_name": "Bank charges", "voucher_detail": "Gross receipt", "debit": 1, "credit": 0},
            {"voucher_id": "GROSS", "date": "2026-01-01", "account_code": "WHT", "account_name": "Withholding tax", "voucher_detail": "Gross receipt", "debit": 2, "credit": 0},
            {"voucher_id": "GROSS", "date": "2026-01-01", "account_code": "CUST-G", "account_name": "Customer Gross", "voucher_detail": "Gross receipt", "debit": 0, "credit": 100},
            {"voucher_id": "NET", "date": "2026-01-02", "account_code": "BANK", "account_name": "Export Bank", "voucher_detail": "Net receipt", "debit": 97, "credit": 0},
            {"voucher_id": "NET", "date": "2026-01-02", "account_code": "CHG", "account_name": "Bank charges", "voucher_detail": "Net receipt", "debit": 1, "credit": 0},
            {"voucher_id": "NET", "date": "2026-01-02", "account_code": "WHT", "account_name": "Withholding tax", "voucher_detail": "Net receipt", "debit": 2, "credit": 0},
            {"voucher_id": "NET", "date": "2026-01-02", "account_code": "CUST-N", "account_name": "Customer Net", "voucher_detail": "Net receipt", "debit": 0, "credit": 100},
        ]
    )
    coa = pd.DataFrame(
        [
            {"account_code": "BANK", "account_title": "Export Bank", "account_type": "Bank", "category": ""},
            {"account_code": "CHG", "account_title": "Bank charges", "account_type": "Expense", "category": "Bank charges"},
            {"account_code": "WHT", "account_title": "Withholding tax", "account_type": "Tax", "category": "Withholding tax"},
            {"account_code": "CUST-G", "account_title": "Customer Gross", "account_type": "Customer", "category": "Received from customers"},
            {"account_code": "CUST-N", "account_title": "Customer Net", "account_type": "Customer", "category": "Received from customers"},
        ]
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=[],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
        receipt_presentation="gross",
    )

    details = result["customer_payment_details"].set_index("voucher_id")
    assert details.loc["GROSS", "method"] == "Gross"
    assert details.loc["GROSS", "bank_statement_amount"] == 100
    assert details.loc["GROSS", "technical_bank_offset"] == 3
    assert details.loc["GROSS", "net_bank_settlement"] == 97
    assert details.loc["GROSS", "expected_net_settlement"] == 97
    assert details.loc["GROSS", "status"] == "Matched"
    assert details.loc["NET", "method"] == "Net"
    assert details.loc["NET", "technical_bank_offset"] == 0
    assert details.loc["NET", "status"] == "Matched"


def test_cashflow_customer_category_identifies_cash_and_bank_child_accounts():
    journal = pd.DataFrame(
        [
            {"voucher_id": "CASH-RECEIPT", "date": "2026-01-01", "account_code": "CASH", "account_name": "Cash in hand", "voucher_detail": "Cash receipt", "debit": 50, "credit": 0},
            {"voucher_id": "CASH-RECEIPT", "date": "2026-01-01", "account_code": "10-15-55-10-10-0001", "account_name": "Export customer one", "voucher_detail": "Cash receipt", "debit": 0, "credit": 50},
            {"voucher_id": "BANK-RECEIPT", "date": "2026-01-02", "account_code": "BANK", "account_name": "Export Bank", "voucher_detail": "Bank receipt", "debit": 75, "credit": 0},
            {"voucher_id": "BANK-RECEIPT", "date": "2026-01-02", "account_code": "10-15-55-10-10-0002", "account_name": "Export customer two", "voucher_detail": "Bank receipt", "debit": 0, "credit": 75},
        ]
    )
    coa = pd.DataFrame(
        [
            {"account_code": "CASH", "account_title": "Cash in hand", "account_type": "Cash", "category": ""},
            {"account_code": "BANK", "account_title": "Export Bank", "account_type": "Bank", "category": ""},
            {"account_code": "10-15-55-10-10-0001", "account_title": "Export customer one", "account_type": "Customer", "category": " Received from customers "},
            {"account_code": "10-15-55-10-10-0002", "account_title": "Export customer two", "account_type": "Customer", "category": "RECEIVED FROM CUSTOMERS"},
        ]
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=["CASH"],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
    )

    details = result["customer_payment_details"].set_index("voucher_id")
    assert set(details.index) == {"CASH-RECEIPT", "BANK-RECEIPT"}
    assert details.loc["CASH-RECEIPT", "bank_account_type"] == "Cash"
    assert details.loc["BANK-RECEIPT", "bank_account_type"] == "Bank"
    assert details.loc["CASH-RECEIPT", "customer_account_code"] == "10-15-55-10-10-0001"
    assert details.loc["BANK-RECEIPT", "customer_account_code"] == "10-15-55-10-10-0002"
    assert details["status"].eq("Matched").all()


def test_cashflow_customer_payment_details_flags_gross_mismatch_and_multi_customer():
    journal = pd.DataFrame(
        [
            {"voucher_id": "MISMATCH", "date": "2026-01-01", "account_code": "BANK", "account_name": "Export Bank", "voucher_detail": "Mismatch", "debit": 100, "credit": 4},
            {"voucher_id": "MISMATCH", "date": "2026-01-01", "account_code": "CHG", "account_name": "Bank charges", "voucher_detail": "Mismatch", "debit": 1, "credit": 0},
            {"voucher_id": "MISMATCH", "date": "2026-01-01", "account_code": "WHT", "account_name": "Withholding tax", "voucher_detail": "Mismatch", "debit": 2, "credit": 0},
            {"voucher_id": "MISMATCH", "date": "2026-01-01", "account_code": "OTHER", "account_name": "Unexplained debit", "voucher_detail": "Mismatch", "debit": 1, "credit": 0},
            {"voucher_id": "MISMATCH", "date": "2026-01-01", "account_code": "CUST", "account_name": "Customer", "voucher_detail": "Mismatch", "debit": 0, "credit": 100},
            {"voucher_id": "MULTI", "date": "2026-01-02", "account_code": "BANK", "account_name": "Export Bank", "voucher_detail": "Multiple customers", "debit": 100, "credit": 0},
            {"voucher_id": "MULTI", "date": "2026-01-02", "account_code": "CUST-1", "account_name": "Customer One", "voucher_detail": "Multiple customers", "debit": 0, "credit": 60},
            {"voucher_id": "MULTI", "date": "2026-01-02", "account_code": "CUST-2", "account_name": "Customer Two", "voucher_detail": "Multiple customers", "debit": 0, "credit": 40},
        ]
    )
    coa = pd.DataFrame(
        [
            {"account_code": "BANK", "account_title": "Export Bank", "account_type": "Bank", "category": ""},
            {"account_code": "CHG", "account_title": "Bank charges", "account_type": "Expense", "category": "Bank charges"},
            {"account_code": "WHT", "account_title": "Withholding tax", "account_type": "Tax", "category": "Withholding tax"},
            {"account_code": "OTHER", "account_title": "Unexplained debit", "account_type": "Expense", "category": "Other expense"},
            {"account_code": "CUST", "account_title": "Customer", "account_type": "Customer", "category": "Received from customers"},
            {"account_code": "CUST-1", "account_title": "Customer One", "account_type": "Customer", "category": "Received from customers"},
            {"account_code": "CUST-2", "account_title": "Customer Two", "account_type": "Customer", "category": "Received from customers"},
        ]
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=[],
        bank_account_codes=["BANK"],
        period_start="2026-01-01",
        period_end="2026-01-31",
    )

    details = result["customer_payment_details"].set_index("voucher_id")
    assert details.loc["MISMATCH", "method"] == "Gross"
    assert details.loc["MISMATCH", "variance"] == -1
    assert details.loc["MISMATCH", "status"] == "Review required"
    assert details.loc["MULTI", "customer_account_name"] == "Customer One / Customer Two"
    assert details.loc["MULTI", "status"] == "Review required"
def test_cashflow_defaults_resolve_fiscal_year_and_ytd_dynamically():
    values = {
        "cashflow_fiscal_year_start_month": "4",
        "cashflow_default_period": "ytd",
        "cashflow_custom_start": "",
        "cashflow_custom_end": "",
    }
    result = settings_router._resolved_cashflow_defaults(values, pd.Timestamp("2026-09-29").date())
    assert result["resolved_period"] == {
        "start": "2026-04-01",
        "end": "2026-09-29",
        "group_by": "month",
    }

    values["cashflow_default_period"] = "financial_year"
    result = settings_router._resolved_cashflow_defaults(values, pd.Timestamp("2026-02-10").date())
    assert result["resolved_period"]["start"] == "2025-04-01"
    assert result["resolved_period"]["end"] == "2026-03-31"


def test_cashflow_defaults_fall_back_to_safe_ytd_for_invalid_stored_values():
    result = settings_router._resolved_cashflow_defaults(
        {
            "cashflow_fiscal_year_start_month": "not-a-month",
            "cashflow_default_period": "unknown",
            "cashflow_custom_start": "not-a-date",
            "cashflow_custom_end": "also-not-a-date",
        },
        pd.Timestamp("2026-09-29").date(),
    )
    assert result["default_period"] == "ytd"
    assert result["resolved_period"]["start"] == "2026-01-01"
    assert result["resolved_period"]["end"] == "2026-09-29"



def test_preview_report_returns_controls_and_category_preview():
    with patch.object(reconciliation, "_require_active_client"), \
         patch.object(reconciliation, "_stored_group_label", return_value="Bank"), \
         patch.object(reconciliation, "_saved_codes", side_effect=_saved_codes), \
         patch.object(reconciliation, "load_ledger_report_source", return_value=_source()), \
         patch.object(reconciliation, "load_reconciliation_balance_source", return_value=_balance_source()), \
         patch.object(reconciliation, "build_group_summary", return_value=_summary()):
        response = reconciliation.preview_reconciliation_report(
            7,
            "Bank",
            "2026-01-01",
            "2026-01-31",
            journal_upload_id=21,
            journal_template_id=None,
            session=None,
        )

    assert response["can_download"] is True
    assert response["controls"]["ledger_balance"]["status"] == "Balanced"
    assert response["controls"]["categories"]["status"] == "Clear"
    assert response["controls"]["reconciliation"]["status"] == "Matched"
    assert response["preview"]["category_totals"][0]["category"] == "Customers"


def test_preview_report_blocks_download_when_balance_evidence_is_missing():
    diagnostics = ({"code": "missing_closing_months", "severity": "error", "message": "Closing balances are missing."},)
    with patch.object(reconciliation, "_require_active_client"), \
         patch.object(reconciliation, "_stored_group_label", return_value="Bank"), \
         patch.object(reconciliation, "_saved_codes", side_effect=_saved_codes), \
         patch.object(reconciliation, "load_ledger_report_source", return_value=_source()), \
         patch.object(reconciliation, "load_reconciliation_balance_source", return_value=_balance_source(False, diagnostics)):
        response = reconciliation.preview_reconciliation_report(
            7,
            "Bank",
            "2026-01-01",
            "2026-01-31",
            journal_upload_id=21,
            journal_template_id=None,
            session=None,
        )

    assert response["can_download"] is False
    assert response["controls"]["reconciliation"]["status"] == "Blocked"
    assert any("Closing balances are missing" in reason for reason in response["blocking_reasons"])


def test_cashflow_drilldown_rollups_reconcile_and_split_treasury_legs():
    journal = pd.DataFrame(
        [
            {'voucher_id': 'R1', 'date': '2026-01-05', 'account_code': 'BANK', 'account_name': 'Main Bank', 'voucher_detail': 'Receipt', 'debit': 100, 'credit': 0},
            {'voucher_id': 'R1', 'date': '2026-01-05', 'account_code': 'AR', 'account_name': 'Customers', 'voucher_detail': 'Receipt', 'debit': 0, 'credit': 100},
            {'voucher_id': 'R2', 'date': '2026-01-06', 'account_code': 'CASH', 'account_name': 'Main Cash', 'voucher_detail': 'Payment', 'debit': 0, 'credit': 40},
            {'voucher_id': 'R2', 'date': '2026-01-06', 'account_code': 'EXP', 'account_name': 'Operating expense', 'voucher_detail': 'Payment', 'debit': 40, 'credit': 0},
        ]
    )
    coa = pd.DataFrame(
        [
            {'account_code': 'BANK', 'account_title': 'Main Bank', 'account_type': 'Bank', 'category': ''},
            {'account_code': 'CASH', 'account_title': 'Main Cash', 'account_type': 'Cash', 'category': ''},
            {'account_code': 'AR', 'account_title': 'Customers', 'account_type': 'Receivable', 'category': 'Received from Customers'},
            {'account_code': 'EXP', 'account_title': 'Operating expense', 'account_type': 'Expense', 'category': 'Operating expenses'},
        ]
    )

    result = build_cashflow_summary(
        journal,
        coa,
        cash_account_codes=['CASH'],
        bank_account_codes=['BANK'],
        period_start='2026-01-01',
        period_end='2026-01-31',
        include_drilldown=True,
    )

    rollups = result['cashflow_ledger_rollups']
    assert result['cashflow_drilldown_prepared'] is True
    assert set(rollups['treasury_type']) == {'Bank', 'Cash'}
    assert rollups.groupby('category')['amount'].sum().to_dict() == {
        'Operating expenses': -40.0,
        'Received from Customers': 100.0,
    }
    assert all(row['status'] == 'Matched' for row in result['cashflow_drilldown_controls']['category_controls'])
    split = result['cashflow_drilldown_controls']['cash_bank_split']
    assert split['bank_amount'] == 100.0
    assert split['cash_amount'] == -40.0
    assert split['mixed_or_unresolved_line_count'] == 0
    assert split['status'] == 'Matched'


def test_cashflows_preview_exposes_compact_drilldown_contract():
    summary = _cashflow_summary()
    summary['cashflow_ledger_rollups'] = pd.DataFrame(
        [{'category': 'Customers', 'period_key': '2026-Q1', 'period_label': 'Q1 2026', 'account_code': 'AR', 'account_name': 'Customers', 'treasury_type': 'Bank', 'amount': 125.0, 'line_count': 1}]
    )
    summary['cashflow_drilldown_controls'] = {'cash_bank_split': {'status': 'Matched'}}
    summary['cashflow_drilldown_prepared'] = True

    preview = reconciliation._cashflow_preview(summary, {'drilldown'})

    assert len(preview['ledger_rollups']) == 1
    assert preview['ledger_rollups'][0]['treasury_type'] == 'Bank'
    assert preview['drilldown_controls']['cash_bank_split']['status'] == 'Matched'
    assert preview['detail_sections_loaded'] == ['drilldown']


def test_cashflow_drilldown_cache_requires_current_payload_version():
    legacy = {'metadata': {'drilldown_details_prepared': True, 'drilldown_details_version': 0}, 'summary': _cashflow_summary()}
    current = {'metadata': {'drilldown_details_prepared': True, 'drilldown_details_version': CASHFLOW_DRILLDOWN_DETAILS_VERSION}, 'summary': _cashflow_summary()}

    assert reconciliation._cashflow_cache_can_serve_details(legacy, {'drilldown'}) is False
    assert reconciliation._cashflow_cache_can_serve_details(current, {'drilldown'}) is True


def test_cashflow_drilldown_route_filters_and_paginates_server_side():
    result = {
        'period_start': '2026-01-01',
        'period_end': '2026-01-31',
        'group_by': 'month',
        'preview': {
            'ledger_rollups': [
                {'category': 'Customers', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'AR1', 'account_name': 'Main Bank customer', 'treasury_type': 'Bank', 'amount': 100.0, 'line_count': 1},
                {'category': 'Customers', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'AR2', 'account_name': 'Cash customer', 'treasury_type': 'Cash', 'amount': 50.0, 'line_count': 1},
                {'category': 'Payments', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'EXP', 'account_name': 'Operating expense', 'treasury_type': 'Bank', 'amount': -75.0, 'line_count': 2},
            ],
            'drilldown_controls': {'cash_bank_split': {'status': 'Matched'}},
        },
        'source': {'ready': True, 'metadata': {'cashflow_report_revision': 7}},
        'blocking_reasons': [],
    }
    with patch.object(reconciliation, '_build_cashflow_result', return_value=result) as builder:
        response = reconciliation.cashflow_drilldown(
            7,
            '2026-01-01',
            '2026-01-31',
            group_by='month',
            treasury_view='combined',
            search='main',
            treasury_type='Bank',
            page=1,
            limit=1,
            session=None,
        )

    assert response['total_rows'] == 1
    assert response['page_count'] == 1
    assert response['rows'][0]['account_code'] == 'AR1'
    assert response['controls']['cash_bank_split']['status'] == 'Matched'
    assert response['source']['metadata']['cashflow_report_revision'] == 7
    assert builder.call_args.kwargs['detail_sections'] == {'drilldown'}


def test_cashflow_drilldown_returns_baseline_rows_for_unfiltered_request():
    result = {
        'period_start': '2026-01-01',
        'period_end': '2026-01-31',
        'group_by': 'month',
        'preview': {
            'ledger_rollups': [
                {'category': 'Customers', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'AR1', 'account_name': 'Main customer', 'treasury_type': 'Bank', 'amount': 100.0, 'line_count': 1},
            ],
        },
        'source': {'ready': True},
        'blocking_reasons': [],
    }
    with patch.object(reconciliation, '_build_cashflow_result', return_value=result):
        response = reconciliation.cashflow_drilldown(
            7,
            '2026-01-01',
            '2026-01-31',
            group_by='month',
            treasury_view='combined',
            search='',
            category='',
            account_head='',
            treasury_type='',
            period_key='',
            page=1,
            limit=50,
            session=None,
        )

    assert response['base_rows'] == result['preview']['ledger_rollups']
    assert response['rows'] == result['preview']['ledger_rollups']


def test_cashflow_drilldown_filters_account_head_and_returns_scoped_options():
    result = {
        'period_start': '2026-01-01',
        'period_end': '2026-01-31',
        'group_by': 'month',
        'preview': {
            'ledger_rollups': [
                {'category': 'Customers', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'AR1', 'account_name': 'Main Bank customer', 'treasury_type': 'Bank', 'amount': 100.0, 'line_count': 1},
                {'category': 'Customers', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'AR2', 'account_name': 'Cash customer', 'treasury_type': 'Cash', 'amount': 50.0, 'line_count': 1},
                {'category': 'Payments', 'period_key': '2026-01', 'period_label': 'Jan 2026', 'account_code': 'EXP', 'account_name': 'Operating expense', 'treasury_type': 'Bank', 'amount': -75.0, 'line_count': 2},
            ],
        },
        'source': {'ready': True},
        'blocking_reasons': [],
    }
    with patch.object(reconciliation, '_build_cashflow_result', return_value=result):
        response = reconciliation.cashflow_drilldown(
            7,
            '2026-01-01',
            '2026-01-31',
            group_by='month',
            treasury_view='combined',
            category='Customers',
            search='',
            account_head='Cash customer / AR2',
            treasury_type='',
            period_key='',
            page=1,
            limit=1,
            session=None,
        )

    assert response['total_rows'] == 1
    assert response['page_count'] == 1
    assert response['rows'][0]['account_code'] == 'AR2'
    assert response['base_rows'] is None
    assert {option['value'] for option in response['account_head_options']} == {
        'Main Bank customer / AR1',
        'Cash customer / AR2',
        'Operating expense / EXP',
    }

def test_cashflows_preview_passes_grouping_and_returns_period_descriptors():
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()),
        patch.object(reconciliation, "build_cashflow_summary", return_value=_cashflow_summary()) as builder,
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="summary",
            session=None,
        )

    assert response["group_by"] == "quarter"
    assert response["preview"]["group_by"] == "quarter"
    assert response["preview"]["periods"][1]["label"] == "Q2 2026"
    assert response["preview"]["category_totals"][0]["2026-Q1"] == 125.0
    assert builder.call_args.kwargs["group_by"] == "quarter"
    assert builder.call_args.kwargs["group_by"] == response["group_by"]


def test_cashflows_preview_uses_summary_read_model_without_loading_detail_source():
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    cached = {
        "metadata": {
            "source_upload_ids": [21],
            "selected_template_upload_ids": [21],
            "source_row_count": 4,
            "valid_row_count": 4,
        },
        "summary": _cashflow_summary(),
    }
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_cashflow_summary_read_model", return_value=cached),
        patch.object(reconciliation, "load_ledger_report_source") as loader,
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="summary",
            session=None,
        )

    loader.assert_not_called()
    assert response["preview"]["group_by"] == "quarter"
    assert response["preview"]["category_totals"][0]["2026-Q1"] == 125.0


def test_cashflows_preview_uses_prepared_details_for_combined_child_sections():
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    summary = _cashflow_summary()
    summary["transfer_details"] = pd.DataFrame([{
        "voucher_id": "TR-1",
        "period_label": "Q1 2026",
        "transfer_type": "Bank to Cash",
        "amount": 20.0,
        "matched": True,
    }])
    summary["deduction_details"] = pd.DataFrame([{
        "period_label": "Q2 2026",
        "category": "Withholding",
        "account_name": "Tax",
        "amount": 5.0,
    }])
    summary["categories_needing_review"] = pd.DataFrame([{
        "voucher_id": "JV-1",
        "period_label": "Q2 2026",
        "account_code": "4100",
        "account_name": "Other",
        "fallback_category": "Review",
    }])
    cached = {
        "metadata": {
            "source_upload_ids": [21],
            "selected_template_upload_ids": [21],
            "source_row_count": 4,
            "valid_row_count": 4,
            "child_details_prepared": True,
            "child_details_version": CASHFLOW_CHILD_DETAILS_VERSION,
            "summary_read_model": "hit",
        },
        "summary": summary,
    }
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_cashflow_summary_read_model", return_value=cached),
        patch.object(reconciliation, "load_ledger_report_source") as loader,
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="transfers,deductions,review",
            session=None,
        )

    loader.assert_not_called()
    assert len(response["preview"]["transfer_details"]) == 1
    assert len(response["preview"]["deduction_details"]) == 1
    assert len(response["preview"]["categories_needing_review"]) == 1


def test_cashflows_preview_falls_back_for_legacy_summary_cache_on_child_request():
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    cached = {
        "metadata": {
            "source_upload_ids": [21],
            "selected_template_upload_ids": [21],
            "source_row_count": 4,
            "valid_row_count": 4,
            "child_details_prepared": True,
            "summary_read_model": "hit",
        },
        "summary": _cashflow_summary(),
    }
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_cashflow_summary_read_model", return_value=cached),
        patch.object(reconciliation, "load_cashflow_prepared_summary", return_value=None),
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()) as loader,
        patch.object(reconciliation, "build_cashflow_summary", return_value=_cashflow_summary()) as builder,
    ):
        reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="transfers,deductions,review",
            session=None,
        )

    loader.assert_called_once()
    builder.assert_called_once()


def test_cashflows_preview_rejects_previous_child_details_version():
    cached = {
        "metadata": {
            "child_details_prepared": True,
            "child_details_version": CASHFLOW_CHILD_DETAILS_VERSION - 1,
        },
        "summary": _cashflow_summary(),
    }

    assert CASHFLOW_CHILD_DETAILS_VERSION == 6
    assert reconciliation._cashflow_cache_can_serve_details(cached, {"customer_payments"}) is False


def test_cashflow_summary_read_model_round_trip_preserves_period_totals():
    from services import ledger_report_source

    writes = []
    summary = _cashflow_summary()
    summary["transfer_details"] = pd.DataFrame([{
        "voucher_id": "TR-1",
        "date": pd.Timestamp("2026-02-12"),
        "month": "2026-02",
        "period_key": "2026-Q1",
        "period_label": "Q1 2026",
        "transfer_type": "Bank to Cash",
        "amount": 20.0,
        "matched": True,
        "voucher_detail": "Transfer",
    }])
    summary["customer_payment_details"] = pd.DataFrame([{
        "voucher_id": "CP-1",
        "date": pd.Timestamp("2026-02-13"),
        "period_key": "2026-Q1",
        "period_label": "Q1 2026",
        "customer_account_name": "Customer Export",
        "bank_account_name": "Export Bank",
        "method": "Gross",
        "customer_gross_amount": 100.0,
        "net_bank_settlement": 97.0,
        "status": "Matched",
    }])
    summary["deduction_details"] = pd.DataFrame([{
        "category": "Withholding",
        "month": "2026-04",
        "period_key": "2026-Q2",
        "period_label": "Q2 2026",
        "amount": 5.0,
        "account_code": "TAX",
        "account_name": "Tax",
        "date": pd.Timestamp("2026-04-04"),
        "presentation": "shown_separately",
    }])
    summary["categories_needing_review"] = pd.DataFrame([{
        "voucher_id": "JV-1",
        "date": pd.Timestamp("2026-04-04"),
        "period_key": "2026-Q2",
        "period_label": "Q2 2026",
        "account_code": "4100",
        "account_name": "Other",
        "voucher_detail": "Review",
        "debit": 5.0,
        "credit": 0.0,
        "fallback_category": "Review",
    }])
    ledger_report_source.store_cashflow_summary_read_model(
        7,
        journal_template_id=9,
        source_upload_ids=[21],
        configuration_version=4,
        report_revision=7,
        period_start="2026-02-10",
        period_end="2026-04-18",
        group_by="quarter",
        summary=summary,
        write_fn=lambda query, params: writes.append((query, params)),
    )

    payload = json.loads(writes[0][1][-1])

    def query(sql, params):
        if "FROM uploaded_files" in sql:
            return [{"upload_id": 21, "file_name": "period.xlsx", "upload_date": "2026-04-01"}]
        return [{
            "summary_payload": payload,
            "configuration_version": 4,
            "report_revision": 7,
            "created_at": "2026-09-29T00:00:00+00:00",
        }]

    cached = ledger_report_source.load_cashflow_summary_read_model(
        7,
        journal_template_id=9,
        period_start="2026-02-10",
        period_end="2026-04-18",
        group_by="quarter",
        receipt_presentation="net",
        query_fn=query,
    )

    assert cached["summary"]["category_totals"].loc["Customers", "2026-Q1"] == 125.0
    assert cached["metadata"]["summary_read_model"] == "hit"
    assert cached["metadata"]["cashflow_report_revision"] == 7
    assert cached["metadata"]["child_details_prepared"] is True
    assert cached["metadata"]["child_details_version"] == CASHFLOW_CHILD_DETAILS_VERSION
    assert cached["summary"]["transfer_details"].loc[0, "amount"] == 20.0
    assert cached["summary"]["customer_payment_details"].loc[0, "method"] == "Gross"
    assert cached["summary"]["deduction_details"].loc[0, "account_name"] == "Tax"
    assert cached["summary"]["categories_needing_review"].loc[0, "account_code"] == "4100"

def test_prepared_daily_snapshot_rolls_up_to_requested_months():
    from services.ledger_report_source import _cashflow_daily_rollup

    payload = {
        "periods": [
            {"key": "2026-01-01", "start": "2026-01-01", "end": "2026-01-01", "label": "01-Jan-26"},
            {"key": "2026-01-02", "start": "2026-01-02", "end": "2026-01-02", "label": "02-Jan-26"},
            {"key": "2026-02-01", "start": "2026-02-01", "end": "2026-02-01", "label": "01-Feb-26"},
        ],
        "categories": [{"category": "Customers", "values": {"2026-01-01": 10, "2026-01-02": 15, "2026-02-01": 20}}],
        "child_details_prepared": True,
        "child_details_version": CASHFLOW_CHILD_DETAILS_VERSION,
        "transfer_details": [{
            "voucher_id": "TR-1",
            "date": "2026-01-02",
            "month": "2026-01",
            "period_key": "2026-01-02",
            "period_label": "02-Jan-26",
            "transfer_type": "Bank to Cash",
            "amount": 20.0,
            "matched": True,
            "voucher_detail": "Transfer",
        }],
        "customer_payment_details": [{
            "voucher_id": "CP-1",
            "date": "2026-01-02",
            "period_key": "2026-01-02",
            "period_label": "02-Jan-26",
            "customer_account_name": "Customer Export",
            "bank_account_name": "Export Bank",
            "method": "Gross",
            "customer_gross_amount": 100.0,
            "net_bank_settlement": 97.0,
            "status": "Matched",
        }],
        "deduction_details": [{
            "category": "Withholding",
            "month": "2026-02",
            "period_key": "2026-02-01",
            "period_label": "01-Feb-26",
            "amount": 5.0,
            "account_code": "TAX",
            "account_name": "Tax",
            "date": "2026-02-01",
            "presentation": "shown_separately",
        }],
        "categories_needing_review": [{
            "voucher_id": "JV-1",
            "date": "2026-02-01",
            "period_key": "2026-02-01",
            "period_label": "01-Feb-26",
            "account_code": "4100",
            "account_name": "Other",
            "voucher_detail": "Review",
            "debit": 5.0,
            "credit": 0.0,
            "fallback_category": "Review",
        }],
        "period_metrics": {
            "2026-01-01": {"source_line_count": 2, "selected_voucher_count": 1, "internal_transfer_count": 0, "category_review_count": 0},
            "2026-01-02": {"source_line_count": 2, "selected_voucher_count": 1, "internal_transfer_count": 1, "category_review_count": 0},
            "2026-02-01": {"source_line_count": 2, "selected_voucher_count": 1, "internal_transfer_count": 0, "category_review_count": 1},
        },
        "receipt_presentation": "net",
    }

    summary = _cashflow_daily_rollup(payload, "2026-01-01", "2026-02-28", "month")

    assert summary["category_totals"].loc["Customers", "2026-01"] == 25
    assert summary["category_totals"].loc["Customers", "2026-02"] == 20
    assert summary["selected_voucher_count"] == 3
    assert summary["internal_transfer_count"] == 1
    assert summary["category_review_count"] == 1
    assert summary["transfer_details"].loc[0, "period_key"] == "2026-01"
    assert summary["customer_payment_details"].loc[0, "period_key"] == "2026-01"
    assert summary["deduction_details"].loc[0, "period_key"] == "2026-02"
    assert summary["categories_needing_review"].loc[0, "period_key"] == "2026-02"


def test_cashflows_preview_uses_prepared_snapshot_when_exact_cache_misses():
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
        "cashflow_report_revision": 7,
    }
    cached = {
        "metadata": {
            "source_upload_ids": [21],
            "selected_template_upload_ids": [21],
            "source_row_count": 4,
            "valid_row_count": 4,
            "summary_read_model": "prepared_snapshot",
        },
        "summary": _cashflow_summary(),
    }
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_cashflow_summary_read_model", return_value=None) as exact_read_model,
        patch.object(reconciliation, "load_cashflow_prepared_summary", return_value=cached) as prepared_snapshot,
        patch.object(reconciliation, "load_ledger_report_source") as loader,
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="summary",
            session=None,
        )

    loader.assert_not_called()
    assert exact_read_model.call_args.kwargs["include_drilldown"] is False
    assert prepared_snapshot.call_args.kwargs["include_drilldown"] is False
    assert response["preview"]["category_totals"][0]["2026-Q1"] == 125.0
    assert response["cashflow_report_revision"] == 7

def test_preparation_builds_one_daily_snapshot_after_committed_source_changes():
    from services import ledger_report_source

    source = SimpleNamespace(
        ready=True,
        journal=pd.DataFrame(),
        coa=pd.DataFrame(),
        metadata={"selected_template_upload_ids": [21]},
    )
    summary = _cashflow_summary()

    def query(sql, params=None):
        if "account_control_cashflow_treasury_accounts" in sql:
            return [{
                "treasury": [{"account_code": "B1", "treasury_type": "bank"}],
                "settings": {"source_template_id": 9, "match_internal_transfers": False, "receipt_presentation": "net"},
            }]
        if "FROM uploaded_files" in sql:
            return [{"upload_id": 21, "file_name": "period.xlsx", "upload_date": "2026-04-01"}]
        if "MIN(c.transaction_date)" in sql:
            return [{"period_start": "2026-01-01", "period_end": "2026-06-30"}]
        if "FROM cashflow_report_revisions" in sql:
            return [{"revision": 6}]
        raise AssertionError(f"Unexpected preparation read: {sql}")

    with (
        patch.object(ledger_report_source, "load_cashflow_sql_source", return_value=source),
        patch("services.ledger_group_summary.build_cashflow_summary", return_value=summary) as builder,
        patch.object(ledger_report_source, "store_cashflow_summary_read_model") as store,
    ):
        result = ledger_report_source.prepare_cashflow_summary_snapshot(5, query_fn=query)

    assert result["status"] == "prepared"
    builder.assert_called_once()
    assert store.call_args.kwargs["group_by"] == "day"
    assert store.call_args.kwargs["source_upload_ids"] == [21]
    assert store.call_args.kwargs["report_revision"] == 6

def test_cashflow_configuration_uses_one_query_and_preserves_sections():
    calls = []

    def query_fn(query, params):
        calls.append((query, params))
        return [{
            "mappings": [{"account_code": "EXP", "category_key": "operating", "label": "Operating expense"}],
            "treasury": [
                {"account_code": "CASH", "treasury_type": "cash"},
                {"account_code": "BANK", "treasury_type": "bank"},
            ],
            "settings": {
                "match_internal_transfers": True,
                "receipt_presentation": "gross",
                "source_upload_id": None,
                "source_template_id": 9,
            },
        }]

    with patch.object(reconciliation, "execute_query_pg", side_effect=query_fn):
        result = reconciliation._cashflow_configuration(7)

    assert len(calls) == 1
    assert calls[0][1] == (7, 7, 7)
    assert result["category_mappings"] == {"EXP": "Operating expense"}
    assert result["cash_account_codes"] == ["CASH"]
    assert result["bank_account_codes"] == ["BANK"]
    assert result["match_internal_transfers"] is True
    assert result["receipt_presentation"] == "gross"
    assert result["source_template_id"] == 9


def test_cashflows_preview_logs_performance_stages(caplog):
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    with (
        caplog.at_level(logging.INFO, logger="routers.reconciliation"),
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()),
        patch.object(reconciliation, "build_cashflow_summary", return_value=_cashflow_summary()),
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="summary",
            session=None,
        )

    performance_records = [record for record in caplog.records if record.getMessage().startswith("cashflows_performance ")]
    assert len(performance_records) == 1
    message = performance_records[0].getMessage()
    assert "validation_seconds=" in message
    assert "configuration_seconds=" in message
    assert "source_seconds=" in message
    assert "summary_seconds=" in message
    assert "format_seconds=" in message
    assert "total_seconds=" in message
    assert "source_rows=4" in message
    assert "cache_mode=source_rebuild" in message
    assert "cache_hit=False" in message
    assert "read_model=none" in message
    assert "report_revision=1" in message
    assert "authoritative=True" in message
    assert response["preview"]["group_by"] == "quarter"




def test_cashflow_drilldown_uses_targeted_projection_for_category(caplog):
    targeted = {
        "rows": [
            {
                "category": "Customers",
                "period_key": "2026-01",
                "period_label": "Jan-26",
                "account_code": "AR1",
                "account_name": "Main Bank customer",
                "treasury_type": "Bank",
                "amount": 125.0,
                "line_count": 2,
            }
        ],
        "controls": {"cash_bank_split": {"status": "Matched"}},
        "metadata": {
            "targeted_drilldown_projection": True,
            "cashflow_report_revision": 4,
            "source_row_count": 2,
        },
    }
    configuration = {"source_template_id": 9, "cashflow_report_revision": 4}
    with (
        caplog.at_level(logging.INFO, logger="routers.reconciliation"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_cashflow_prepared_drilldown", return_value=targeted) as targeted_loader,
        patch.object(reconciliation, "_build_cashflow_result") as full_report,
    ):
        response = reconciliation.cashflow_drilldown(
            7,
            "2026-01-01",
            "2026-01-31",
            group_by="month",
            category="Customers",
            treasury_view="combined",
            page=1,
            limit=50,
            session=None,
        )

    full_report.assert_not_called()
    targeted_loader.assert_called_once()
    assert response["rows"][0]["account_code"] == "AR1"
    assert response["source"]["metadata"]["targeted_drilldown_projection"] is True
    record = next(record for record in caplog.records if record.getMessage().startswith("cashflows_drilldown_performance "))
    assert "projection_mode=targeted_projection" in record.getMessage()
    assert "cache_mode=targeted_drilldown_projection" in record.getMessage()


def test_cashflow_drilldown_logs_endpoint_timing(caplog):
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    summary = _cashflow_summary()
    summary["cashflow_ledger_rollups"] = pd.DataFrame([{
        "category": "Customers",
        "account_code": "B1",
        "account_name": "Bank One",
        "period_key": "2026-Q1",
        "period_label": "Q1 2026",
        "treasury_type": "Bank",
    }])
    with (
        caplog.at_level(logging.INFO, logger="routers.reconciliation"),
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_cashflow_summary_read_model", return_value=None) as exact_read_model,
        patch.object(reconciliation, "load_cashflow_prepared_summary", return_value=None) as prepared_snapshot,
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()),
        patch.object(reconciliation, "build_cashflow_summary", return_value=summary),
        patch.object(reconciliation, "store_cashflow_summary_read_model"),
    ):
        response = reconciliation.cashflow_drilldown(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            treasury_view="combined",
            search="customer",
            page=1,
            limit=1,
            session=None,
        )

    performance_records = [
        record for record in caplog.records
        if record.getMessage().startswith("cashflows_drilldown_performance ")
    ]
    assert len(performance_records) == 1
    message = performance_records[0].getMessage()
    assert "build_seconds=" in message
    assert "filter_seconds=" in message
    assert "total_seconds=" in message
    assert "filter_count=1" in message
    assert "total_rows=1" in message
    assert "returned_rows=1" in message
    assert "cache_mode=source_rebuild" in message
    assert "cache_hit=False" in message
    assert "report_revision=1" in message
    assert exact_read_model.call_args.kwargs["include_drilldown"] is True
    assert prepared_snapshot.call_args.kwargs["include_drilldown"] is True
    assert response["page_count"] == 1

def test_cashflows_preview_accepts_combined_child_sections(caplog):
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
    }
    summary = _cashflow_summary()
    summary["transfer_details"] = pd.DataFrame([{
        "voucher_id": "TR-1",
        "period_label": "Q1 2026",
        "transfer_type": "Bank to Cash",
        "amount": 20.0,
        "matched": True,
    }])
    summary["customer_payment_details"] = pd.DataFrame([{
        "voucher_id": "CP-1",
        "period_label": "Q1 2026",
        "customer_account_name": "Customer Export",
        "bank_account_name": "Export Bank",
        "method": "Gross",
        "customer_gross_amount": 100.0,
        "net_bank_settlement": 97.0,
        "status": "Matched",
    }])
    summary["deduction_details"] = pd.DataFrame([{
        "period_label": "Q2 2026",
        "category": "Withholding",
        "account_name": "Tax",
        "amount": 5.0,
    }])
    summary["categories_needing_review"] = pd.DataFrame([{
        "voucher_id": "JV-1",
        "period_label": "Q2 2026",
        "account_code": "4100",
        "account_name": "Other",
        "fallback_category": "Review",
    }])
    with (
        caplog.at_level(logging.INFO, logger="routers.reconciliation"),
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()),
        patch.object(reconciliation, "build_cashflow_summary", return_value=summary) as builder,
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="transfers,customer_payments,deductions,review",
            session=None,
        )

    builder.assert_called_once()
    assert response["preview"]["detail_sections_loaded"] == ["customer_payments", "deductions", "review", "transfers"]
    assert len(response["preview"]["transfer_details"]) == 1
    assert len(response["preview"]["customer_payment_details"]) == 1
    assert len(response["preview"]["deduction_details"]) == 1
    assert len(response["preview"]["categories_needing_review"]) == 1
    performance_records = [record for record in caplog.records if record.getMessage().startswith("cashflows_performance ")]
    assert "details=customer_payments,deductions,review,transfers" in performance_records[0].getMessage()


def test_download_report_returns_professional_xlsx_when_controls_pass():
    result = {
        "can_download": True,
        "blocking_reasons": [],
        "group_label": "Bank",
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "_summary": _summary(),
        "_cross_group_check": None,
        "_opening_balances": {"B1": 1000},
    }
    with patch.object(reconciliation, "_build_report_result", return_value=result), \
         patch.object(reconciliation, "export_reconciliation_xlsx", return_value=b"xlsx"):
        response = reconciliation.download_reconciliation_report(
            7,
            "Bank",
            "2026-01-01",
            "2026-01-31",
            journal_upload_id=21,
            journal_template_id=None,
            session=None,
        )
    assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "Bank_Monthly_Summary_2026-01_to_2026-01.xlsx" in response.headers["content-disposition"]

def test_cashflow_export_preserves_visible_periods_and_totals():
    preview = {
        "periods": [
            {"key": "2026-Q1", "start": "2026-01-01", "end": "2026-03-31", "label": "Q1 2026"},
            {"key": "2026-Q2", "start": "2026-04-01", "end": "2026-06-30", "label": "Q2 2026"},
        ],
        "group_by": "quarter",
        "category_totals": [
            {"category": "Customers", "2026-Q1": 125.0, "2026-Q2": 50.0, "Total": 175.0},
            {"category": "Vendors", "2026-Q1": -80.0, "2026-Q2": -20.0, "Total": -100.0},
        ],
        "ledger_rollups": [
            {"category": "Customers", "period_key": "2026-Q1", "period_label": "Q1 2026", "account_code": "AR", "account_name": "Customers", "treasury_type": "Bank", "amount": 125.0, "line_count": 1},
            {"category": "Vendors", "period_key": "2026-Q1", "period_label": "Q1 2026", "account_code": "AP", "account_name": "Vendors", "treasury_type": "Cash", "amount": -80.0, "line_count": 2},
        ],
        "drilldown_controls": {"cash_bank_split": {"status": "Matched"}},
        "transfer_details": [{
            "voucher_id": "TR-1",
            "date": "2026-01-05",
            "transferor_account_code": "BA",
            "transferor_account_name": "Bank Alpha",
            "transferor_type": "Bank",
            "transferee_account_code": "CB",
            "transferee_account_name": "Cash Book",
            "transferee_type": "Cash",
            "amount": 20.0,
            "variance": 0.0,
            "status": "Confirmed",
            "voucher_detail": "Bank to cash transfer",
            "matched": True,
        }],
        "deduction_details": [{"period_label": "Q2 2026", "category": "Withholding", "account_name": "Tax", "amount": 5.0}],
        "categories_needing_review": [{"voucher_id": "JV-1", "period_label": "Q2 2026", "account_code": "4100", "account_name": "Other", "fallback_category": "Review"}],
        "selected_voucher_count": 3,
        "internal_transfer_count": 1,
        "deduction_count": 1,
        "category_review_count": 1,
        "receipt_presentation": "net",
    }

    workbook = load_workbook(
        BytesIO(export_cashflow_xlsx(preview, period_start="2026-01-01", period_end="2026-06-30", group_by="quarter")),
        data_only=True,
    )

    summary = workbook["Cashflows Summary"]
    assert summary.cell(4, 1).value == "Receipts — percentages calculated within this section"
    assert [summary.cell(5, column).value for column in range(1, 7)] == ["Category", "Q1 2026", "Q2 2026", "Total", "% of section", "Local total"]
    assert [summary.cell(6, column).value for column in range(1, 7)] == ["Customers", 125, 50, 175, "100.00%", "175 Rs"]
    assert summary.cell(6, 2).number_format == '#,##0;[Red](#,##0);-'
    assert summary.cell(6, 1).font.bold
    assert summary.cell(6, 4).font.bold
    assert summary.cell(6, 4).font.color.rgb.endswith("0070C0")
    assert summary.cell(6, 5).font.color.rgb.endswith("0070C0")
    assert summary.cell(6, 6).font.bold
    assert summary.cell(6, 6).font.color.rgb.endswith("7E350E")
    assert all(summary.cell(4, column).fill.fgColor.rgb.endswith("E2F0D9") for column in range(1, 7))
    assert [summary.cell(7, column).value for column in range(1, 5)] == ["Total Receipts", 125, 50, 175]
    assert all(summary.cell(7, column).font.bold for column in range(1, 7))
    assert summary.cell(7, 4).font.color.rgb.endswith("0070C0")
    assert summary.cell(7, 5).font.color.rgb.endswith("0070C0")
    assert summary.cell(7, 6).font.color.rgb.endswith("7E350E")
    assert all(summary.cell(7, column).fill.fgColor.rgb.endswith("D9EAF7") for column in range(1, 7))
    assert summary.cell(10, 1).value == "Payments — percentages calculated within this section"
    assert [summary.cell(12, column).value for column in range(1, 5)] == ["Vendors", 80, 20, 100]
    transfers = workbook["Internal Transfers"]
    assert [transfers.cell(4, column).value for column in range(1, 9)] == [
        "Date", "Voucher", "Transferor", "Transferee", "Amount", "Variance", "Status", "Description"
    ]
    assert transfers.cell(5, 1).value.date().isoformat() == "2026-01-05"
    assert transfers.cell(5, 2).value == "TR-1"
    assert transfers.cell(5, 3).value == "Bank Alpha (BA) · Bank"
    assert transfers.cell(5, 4).value == "Cash Book (CB) · Cash"
    assert transfers.cell(5, 5).value == 20
    assert transfers.cell(5, 6).value == 0
    assert transfers.cell(5, 6).number_format == '#,##0;[Red](#,##0);-'
    assert transfers.cell(5, 7).value == "Confirmed"
    assert transfers.cell(5, 8).value == "Bank to cash transfer"
    assert transfers.cell(5, 3).alignment.horizontal == "left"
    assert transfers.cell(5, 5).alignment.horizontal == "right"
    drilldown = workbook["Cashflows Ledger Drill-down"]
    assert [drilldown.cell(4, column).value for column in range(1, 8)] == [
        "Category", "Period", "Ledger account", "Account code", "Cash / Bank", "Amount", "Lines"
    ]
    assert [drilldown.cell(5, column).value for column in range(1, 8)] == [
        "Customers", "Q1 2026", "Customers", "AR", "Bank", 125, 1
    ]
    ledgerwise = workbook["Ledgerwise Summary"]
    assert [ledgerwise.cell(4, column).value for column in range(1, 6)] == [
        "Account Code",
        "Account Name/Ledger",
        "Ledger Total Amount (Cash and Bank)",
        "Total Amount as per Cashflows Summary",
        "Difference (if any)",
    ]
    assert [ledgerwise.cell(5, column).value for column in range(1, 6)] == [None, "Customers", 125, 175, -50]
    assert [ledgerwise.cell(6, column).value for column in range(1, 4)] == ["AR", "Customers", 125]
    assert [ledgerwise.cell(7, column).value for column in range(1, 6)] == [None, "Vendors", 80, 100, -20]
    assert [ledgerwise.cell(8, column).value for column in range(1, 4)] == ["AP", "Vendors", 80]
    assert ledgerwise.cell(5, 3).number_format == '#,##0.00;[Red](#,##0.00);0.00'
    assert ledgerwise.cell(5, 2).fill.fgColor.rgb.endswith("D9EAF7")
    assert ledgerwise.freeze_panes == "C5"
    assert all("#REF!" not in str(cell.value) for row in ledgerwise.iter_rows() for cell in row if cell.value is not None)
    controls = workbook["Export Controls"]
    control_values = {controls.cell(row, 1).value: controls.cell(row, 2).value for row in range(5, controls.max_row + 1)}
    assert control_values["Drill-down rows"] == 2
    assert control_values["Drill-down Bank amount"] == 125
    assert control_values["Drill-down Cash amount"] == -80
    assert control_values["Drill-down detail total"] == 45
    assert set(workbook.sheetnames) == {"Cashflows Summary", "Ledgerwise Summary", "Internal Transfers", "Customer Deductions", "Review Required", "Cashflows Ledger Drill-down", "Export Controls"}



def test_cashflow_ledgerwise_summary_reconciles_combined_cash_and_bank_ledgers():
    preview = {
        "periods": [{"key": "2026-01", "label": "Jan 2026"}],
        "group_by": "month",
        "category_totals": [
            {"category": "Payments", "2026-01": -175.0, "Total": -175.0},
            {"category": "Receipts", "2026-01": 30.0, "Total": 30.0},
        ],
        "ledger_rollups": [
            {"category": "Payments", "period_key": "2026-01", "account_code": "P1", "account_name": "Supplier one", "treasury_type": "Bank", "amount": -100.0},
            {"category": "Payments", "period_key": "2026-01", "account_code": "P1", "account_name": "Supplier one", "treasury_type": "Cash", "amount": -50.0},
            {"category": "Payments", "period_key": "2026-01", "account_code": "P2", "account_name": "Supplier two", "treasury_type": "Bank", "amount": -25.0},
            {"category": "Receipts", "period_key": "2026-01", "account_code": "R1", "account_name": "Customer one", "treasury_type": "Bank", "amount": 30.0},
        ],
    }

    workbook = load_workbook(BytesIO(export_cashflow_xlsx(preview)), data_only=True)
    sheet = workbook["Ledgerwise Summary"]
    assert [sheet.cell(row, 2).value for row in range(5, 10)] == ["Payments", "Supplier one", "Supplier two", "Receipts", "Customer one"]
    assert [sheet.cell(5, column).value for column in range(3, 6)] == [175, 175, 0]
    assert [sheet.cell(6, column).value for column in range(1, 4)] == ["P1", "Supplier one", 150]
    assert [sheet.cell(7, column).value for column in range(1, 4)] == ["P2", "Supplier two", 25]
    assert [sheet.cell(8, column).value for column in range(3, 6)] == [30, 30, 0]


def test_cashflow_export_uses_canonical_drilldown_and_active_filters():
    result = {
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "group_by": "month",
        "preview": {
            "periods": [{"key": "2026-01", "start": "2026-01-01", "end": "2026-01-31", "label": "Jan 2026"}],
            "category_totals": [
                {"category": "Customers", "2026-01": 100.0, "Total": 100.0},
                {"category": "Vendors", "2026-01": -20.0, "Total": -20.0},
            ],
            "ledger_rollups": [
                {"category": "Customers", "period_key": "2026-01", "period_label": "Jan 2026", "account_code": "AR", "account_name": "Main customer", "treasury_type": "Bank", "amount": 100.0, "line_count": 1},
                {"category": "Vendors", "period_key": "2026-01", "period_label": "Jan 2026", "account_code": "AP", "account_name": "Cash vendor", "treasury_type": "Cash", "amount": -20.0, "line_count": 1},
            ],
            "drilldown_controls": {"cash_bank_split": {"status": "Matched"}},
        },
        "source": {"ready": True},
        "blocking_reasons": [],
    }
    with (
        patch.object(reconciliation, "_build_cashflow_result", return_value=result) as builder,
        patch.object(reconciliation, "export_cashflow_xlsx", return_value=b"xlsx") as exporter,
    ):
        response = reconciliation.export_cashflows_report(
            7,
            "2026-01-01",
            "2026-01-31",
            group_by="month",
            treasury_view="combined",
            search="Main",
            category="Customers",
            account_head="Main customer",
            treasury_type="Bank",
            period_key="2026-01",
            summary_filter="customer",
            hide_zero_rows=True,
            session=None,
        )

    assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert builder.call_args.kwargs["detail_sections"] == {"transfers", "customer_payments", "deductions", "review", "drilldown"}
    export_preview = exporter.call_args.args[0]
    assert [row["category"] for row in export_preview["category_totals"]] == ["Customers"]
    assert len(export_preview["ledger_rollups"]) == 1
    assert export_preview["ledger_rollups"][0]["account_code"] == "AR"
    assert export_preview["export_filters"]["hide_zero_rows"] is True
    assert export_preview["export_filters"]["account_head"] == "Main customer"

def test_reports_view_and_dashboard_entry_are_wired():
    root = Path(__file__).resolve().parents[1]
    html = root.joinpath("static", "reconciliation.html").read_text(encoding="utf-8")
    javascript = root.joinpath("static", "reconciliation.js").read_text(encoding="utf-8")
    stylesheet = root.joinpath("static", "style.css").read_text(encoding="utf-8")
    dashboard = root.joinpath("static", "index.html").read_text(encoding="utf-8")
    navigation = root.joinpath("static", "client_context.js").read_text(encoding="utf-8")

    assert "id='reportsView'" in html
    assert "id='cashflowsCard'" in html
    assert "data-report-key='cashflows'" in html
    assert "class='module-card coming-soon'" in html
    assert "/api/reconciliation/reports/preview" not in javascript
    assert 'id="reportsCard"' in dashboard
    assert "reconciliation.html?view=reports" in navigation
    assert 'onclick=' not in html
    assert "details: 'transfers,customer_payments,deductions,review'" in javascript
    assert "cashflowChildRequest" in javascript
    assert "cashflowsLoadCustomerPaymentsButton" in html
    assert "cashflowsCustomerPaymentsSection" in html
    assert "customer_payments" in javascript
    assert "customer_payment_details" in javascript
    assert "label: 'Receiving account'" in javascript
    assert "label: 'Total deductions'" in javascript
    assert "label: 'Technical Bank credit'" in javascript
    assert "label: 'Expected settlement'" in javascript
    assert "label: 'Actual settlement'" in javascript
    assert "label: 'Difference'" in javascript
    assert "cashflows-customer-payments-table" in stylesheet
    assert ".cashflows-customer-payments-table { min-width: 1750px; }" in stylesheet
    assert "formatCashflowTransferParty" in javascript
    assert "renderCashflowTransferReconciliation" in javascript
    assert "cashflowsTransferReconciliation" in html
    assert "prefix + '_account_name'" in javascript
    assert "status_display" in javascript
    assert "setActiveCashflowWindow" in javascript
    assert "cashflows-table-number" in javascript
    assert "grid-template-columns: minmax(0, 1fr)" in stylesheet
    assert ".cashflows-table td.cashflows-table-text" in stylesheet
    assert "cashflowsDrilldownExportButton" in html
    assert "downloadCashflowReport('drilldown')" in javascript
    assert "summary_filter" in javascript
    assert "hide_zero_rows" in javascript
    assert "let hideCashflowZeroRows = true;" in javascript
    assert "if (amount === 0) return '-';" in javascript
    assert "minimumFractionDigits: 0" in javascript
    assert "cashflows-summary-section-row" in javascript
    assert "cashflows-summary-total-row" in javascript
    assert "cashflows-summary-category-value" in javascript
    assert "cashflows-summary-total-value" in javascript
    assert "cashflows-summary-local-total-value" in javascript
    assert "Account code: " in javascript
    assert "cashflows-inline-ledger-type" in javascript
    assert "const accountName = rawName.split" in javascript
    assert ".cashflows-inline-ledger-link { display: inline;" in stylesheet
    assert ".cashflows-summary-pane-table tbody tr:hover td { background: #f8fbfd; }" in stylesheet
    assert "const columns = ['Category'].concat(periods.map" in javascript
    assert "const frozenColumnCount = 1" in javascript
    assert "cashflows-summary-sticky-header" in javascript
    assert "syncCashflowSummaryUniversalScrollbar" in javascript
    assert "cashflows-summary-universal-scrollbar-thumb" in javascript
    assert "role', 'scrollbar'" in javascript
    assert "event.key === 'Home'" in javascript
    assert ".cashflows-summary-scroll-pane::-webkit-scrollbar { display: none; }" in stylesheet
    assert ".cashflows-summary-universal-scrollbar-thumb { position: absolute;" in stylesheet
    assert "cashflowSummaryPaneRowParts" in javascript
    assert "cashflows-summary-sticky-header { position: sticky; top: 72.5px;" in stylesheet
    assert ".cashflows-summary-scroll-pane { min-width: 0; overflow-x: auto; overflow-y: clip; }" in stylesheet
    assert "style.css?v=69" in html
    assert "table.style.width = fillAvailable ? 'max(100%, ' + totalWidth + 'px)' : totalWidth + 'px';" in javascript

    assert "color: #7E350E" in stylesheet
    assert "cashflowInlineLedgerHostMarkup(category, section, colspan, true)" in javascript
    assert ".cashflows-summary-total-row th" in stylesheet
    assert "#0070C0" in stylesheet
    assert "#7E350E" in stylesheet
    assert "--cashflows-summary-frozen-width" in stylesheet
    assert "overflow-wrap: anywhere" in stylesheet
    assert "position: static !important" in stylesheet
    assert ".cashflows-summary-total-row th, .cashflows-summary-total-row td { background: #e8eef7;" in stylesheet
    load_summary_index = html.index("<button type='submit' class='cashflows-primary'>Load summary</button>")
    drilldown_index = html.index("id='cashflowsLedgerDrilldownButton'")
    actions_index = html.index("class='cashflows-report-actions'")
    hide_zero_index = html.index("id='cashflowsHideZeroRowsButton'")
    export_index = html.index("id='cashflowsExportButton'")
    assert load_summary_index < actions_index < drilldown_index < hide_zero_index < export_index

    assert "cashflowsDrilldownShortcuts" in html
    assert "cashflowsDrilldownLoadTransfersButton" in html
    assert "cashflowsDrilldownLoadCustomerPaymentsButton" in html
    assert "cashflowsDrilldownLoadDeductionsButton" in html
    assert "cashflowsDrilldownLoadReviewButton" in html
    assert "cashflowsDrilldownViewTransactionsButton" in html
    assert "data-cashflow-child-shortcut" in html
    assert "cashflowChildShortcutTargets" in javascript
    assert "syncCashflowDrilldownShortcuts" in javascript
    assert javascript.index("function syncCashflowDrilldownShortcuts") < javascript.index("function resetLazyCashflowSections")
    assert "cashflowsDetailResults" in html
    assert "cashflowsMainDetailAnchor" in html
    assert "cashflowsDrilldownDetailHost" in html
    assert "function moveCashflowDetailResults" in javascript
    assert "moveCashflowDetailResults(open)" in javascript
    ledger_table_index = html.index("id='cashflowsLedgerDrilldownTable'")
    ledger_pagination_index = html.index("id='cashflowsDrilldownPrevious'")
    shortcut_index = html.index("id='cashflowsDrilldownShortcuts'")
    detail_host_index = html.index("id='cashflowsDrilldownDetailHost'")
    assert ledger_table_index < ledger_pagination_index < shortcut_index < detail_host_index
    assert ".cashflows-drilldown-shortcuts" in stylesheet
    for search_id in (
        "cashflowsTransfersSearch",
        "cashflowsCustomerPaymentsSearch",
        "cashflowsDeductionsSearch",
        "cashflowsReviewSearch",
    ):
        assert search_id in html
    assert html.count("Search any value in any column") == 4
    assert "Search any visible column" in html
    assert "cashflowDetailSearchInputs" in javascript
    assert "filterCashflowDetailRows" in javascript
    assert "formatCashflowDetailCount" in javascript
    assert "cashflows-detail-search-toolbar" in stylesheet
    assert ".cashflows-detail-search-input" in stylesheet


def test_financial_statement_summary_aggregates_central_categories_and_flags_unmapped():
    journal = pd.DataFrame([
        {"voucher_id": "V1", "date": "2026-01-10", "account_code": "EXP", "account_name": "Expense", "debit": 100, "credit": 0},
        {"voucher_id": "V1", "date": "2026-01-10", "account_code": "CASH", "account_name": "Cash", "debit": 0, "credit": 100},
        {"voucher_id": "V2", "date": "2026-02-05", "account_code": "UNMAPPED", "account_name": "Unmapped", "debit": 25, "credit": 0},
    ])
    coa = pd.DataFrame([
        {"account_code": "EXP", "account_title": "Expense", "account_type": "Expense", "category": "Operating expense"},
        {"account_code": "CASH", "account_title": "Cash", "account_type": "Cash", "category": "Cash and bank"},
    ])

    result = build_financial_statement_summary(journal, coa, "2026-01-01", "2026-02-28")

    assert result["months"] == ["2026-01", "2026-02"]
    totals = result["category_totals"]
    assert totals.loc["Operating expense", "2026-01"] == 100
    assert totals.loc["Cash and bank", "2026-01"] == -100
    assert result["unmapped_account_count"] == 1
    assert result["unmapped_accounts"].iloc[0]["account_code"] == "UNMAPPED"


def test_financial_statement_preview_uses_account_control_mapping_source():
    summary = {
        "months": ["2026-01"],
        "category_totals": pd.DataFrame({"2026-01": [125.0], "Total": [125.0]}, index=["Operating expense"]),
        "account_counts": pd.Series({"Operating expense": 2}),
        "unmapped_accounts": pd.DataFrame(columns=["account_code", "account_name", "debit", "credit", "net_movement"]),
        "selected_voucher_count": 3,
        "source_line_count": 6,
        "mapped_account_count": 2,
        "unmapped_account_count": 0,
    }
    with patch.object(reconciliation, "_require_active_client"), \
         patch.object(reconciliation, "load_ledger_report_source", return_value=_source()) as loader, \
         patch.object(reconciliation, "build_financial_statement_summary", return_value=summary):
        response = reconciliation.preview_financial_statements(
            7, "2026-01-01", "2026-01-31", journal_upload_id=21, journal_template_id=None, session=None
        )

    assert response["controls"]["financial_statement_mapping"]["status"] == "Ready"
    assert response["preview"]["category_totals"][0]["category"] == "Operating expense"
    assert response["preview"]["account_counts"] == [{"category": "Operating expense", "account_count": 2}]
    loader.assert_called_once()
    assert loader.call_args.kwargs["account_category_source"] == "financial_statement"


def test_reports_hub_exposes_requested_report_cards():
    root = Path(__file__).resolve().parents[1]
    html = root.joinpath("static", "reconciliation.html").read_text(encoding="utf-8")
    javascript = root.joinpath("static", "reconciliation.js").read_text(encoding="utf-8")

    assert "Cashflows" in html
    assert "Vendor Reports" in html
    assert "Customer Reports" in html
    assert "Financial Statements" in html
    assert "notes section" in html
    assert "<h1>Reports centre</h1>" not in html
    assert "reportsHubMessage" not in html
    assert "id='previewFinancialStatementsButton'" not in html
    assert "id='financialStatementPreviewPanel'" not in html
    assert "/api/reconciliation/financial-statements/preview?" not in javascript

def test_cashflows_preview_force_refresh_bypasses_cached_snapshots_and_reports_freshness():
    configuration = {
        "source_template_id": 9,
        "cash_account_codes": [],
        "bank_account_codes": ["B1"],
        "match_internal_transfers": False,
        "receipt_presentation": "net",
        "cashflow_report_revision": 7,
    }
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "_cashflow_configuration", return_value=configuration),
        patch.object(reconciliation, "load_cashflow_summary_read_model") as read_model,
        patch.object(reconciliation, "load_cashflow_prepared_summary") as prepared,
        patch.object(reconciliation, "load_ledger_report_source", return_value=_source()) as loader,
        patch.object(reconciliation, "build_cashflow_summary", return_value=_cashflow_summary()),
        patch.object(reconciliation, "store_cashflow_summary_read_model"),
    ):
        response = reconciliation.preview_cashflows(
            7,
            "2026-02-10",
            "2026-04-18",
            group_by="quarter",
            details="summary",
            refresh_from_source=True,
            session=None,
        )

    read_model.assert_not_called()
    prepared.assert_not_called()
    loader.assert_called_once()
    assert response["freshness"]["mode"] == "forced_source_rebuild"
    assert response["freshness"]["authoritative"] is True
    assert response["freshness"]["report_revision"] == 7


def test_cashflow_revision_endpoint_returns_lightweight_durable_revision():
    with (
        patch.object(reconciliation, "_require_active_client"),
        patch.object(reconciliation, "get_cashflow_report_revision", return_value=12),
    ):
        response = reconciliation.cashflow_revision(7, session=None)

    assert response == {"client_id": 7, "cashflow_report_revision": 12}
