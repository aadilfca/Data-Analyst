from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from routers import reconciliation


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
