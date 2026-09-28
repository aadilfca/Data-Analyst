from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cashflows_report_contains_direct_classified_transaction_window():
    html = (ROOT / "static" / "reconciliation.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "reconciliation.js").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert "cashflowsTransactionsSection" in html
    assert "cashflowsTransactionsTable" in html
    assert "cashflowsTransactionsPrevious" in html
    assert "cashflowsTransactionsNext" in html
    assert "/api/data/cashflows-classification-preview?" in script
    assert "toggleClassifiedTransactions" in script
    assert "window.location.href = '/static/databank.html?'" not in script
    assert ".cashflows-transactions-section" in styles
    assert "cashflowsLazyActions" in html
    assert "details: 'summary'" in script
    assert "loadCashflowChild" in script
    assert ".cashflows-lazy-actions" in styles
    assert ".cashflows-transactions-section .cashflows-table th" in styles
    assert "assignedCategoryIndex" in script
    assert "cashflows-classified-cash" in script
    assert "cashflows-classified-bank" in script
    assert "#0070C0" in styles
    assert "cashflowsTransactionsCategoryOptions" in html
    assert "renderCategoryOptions" in script
    assert "Unassigned" in script
    assert "__unassigned__" in script
    assert "cashflowsPeriodButton" in html
    assert "cashflowsPeriodPopover" in html
    assert "cashflowsPeriodPicker" in html
    assert "cashflowsGroupingSelect" in html
    assert "Group by" in html
    assert "cashflowsCustomGrouping" not in html
    assert "cashflowsBackButton" not in html
    assert "cashflowsSourceStatus" not in html
    assert "cashflowsControls" not in html
    assert "cashflowsSourceQuality" not in html
    assert "cashflows-report-header" not in html
    assert "cashflowsMessage" not in html
    assert "cashflowsToast" in html
    assert "cashflowsProcessing" in html
    assert "cashflowsProcessingMessage" in html
    assert "data-processing-step='load'" in html
    assert "data-period-type='fortnight'" in html
    assert 'data-picker-nav=' + chr(34) + 'prev' + chr(34) in script
    assert 'data-picker-nav=' + chr(34) + 'next' + chr(34) in script
    assert "cashflowSelectedPeriods" in script
    assert "isCashflowPeriodSelected" in script
    assert "findIndex" in script
    assert ".cashflows-picker-cell.is-selected" in styles
    assert "event.composedPath" in script
    assert "periodControl" in script
    assert "insidePeriodControl" in script
    assert "cashflowGroupingPreference" in script
    assert "preferredCashflowGrouping" in script
    assert "cashflowsGroupingSelect" in script
    assert "cashflowsCustomGroupingSelect" not in script
    assert "cashflowsBackButton" not in script
    assert "cashflowsSourceStatus" not in script
    assert "renderControls" not in script
    assert "renderSourceQuality" not in script
    assert "setUpOverride" in script
    assert "setMessage" not in script
    assert "showCashflowToast" in script
    assert "showCashflowProcessing" in script
    assert "hideCashflowProcessing" in script
    assert "setCashflowProcessingStep" in script
    assert "cashflowProcessingControls" in script
    assert "group_by: cashflowSelection.groupBy" in script
    assert ".cashflows-grouping-control" in styles
    assert ".cashflows-template-note" not in styles
    assert ".cashflows-report-header" not in styles
    assert ".cashflows-control-grid" not in styles
    assert ".cashflows-source-quality" not in styles
    assert ".cashflows-message" not in styles
    assert ".cashflows-toast" in styles
    assert ".cashflows-processing" in styles
    assert ".cashflows-processing-panel" in styles
