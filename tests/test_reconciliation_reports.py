from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from routers import reconciliation
from services.ledger_group_summary import build_financial_statement_summary


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


def _saved_codes(_client_id, group_label=None):
    return {"B1"} if not group_label or group_label.casefold() == "bank" else set()


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


def test_reports_view_and_dashboard_entry_are_wired():
    root = Path(__file__).resolve().parents[1]
    html = root.joinpath("static", "reconciliation.html").read_text(encoding="utf-8")
    javascript = root.joinpath("static", "reconciliation.js").read_text(encoding="utf-8")
    dashboard = root.joinpath("static", "index.html").read_text(encoding="utf-8")
    navigation = root.joinpath("static", "client_context.js").read_text(encoding="utf-8")

    assert 'id="reportsView"' in html
    assert 'id="downloadReportButton"' in html
    assert "/api/reconciliation/reports/preview" in javascript
    assert 'id="reportsCard"' in dashboard
    assert "reconciliation.html?view=reports" in navigation
    assert 'onclick=' not in html


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


def test_reports_ui_exposes_financial_statement_preview():
    root = Path(__file__).resolve().parents[1]
    html = root.joinpath("static", "reconciliation.html").read_text(encoding="utf-8")
    javascript = root.joinpath("static", "reconciliation.js").read_text(encoding="utf-8")

    assert 'id="previewFinancialStatementsButton"' in html
    assert 'id="financialStatementPreviewPanel"' in html
    assert "/api/reconciliation/financial-statements/preview?" in javascript
    assert "account control mappings" in javascript.lower()
