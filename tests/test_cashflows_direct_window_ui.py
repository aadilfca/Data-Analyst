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
    assert "cashflows-detail-actions" in html
    assert ".cashflows-search-control input[type='search']::-webkit-search-cancel-button" in styles
    assert "toggleCashflowZeroRows" in script
    assert "cashflowRowsWithNonZeroAmounts" in script
    assert "cashflowChildPreviews" in script
    assert html.index("cashflowsLazyActions") < html.index("cashflowsViewTransactionsButton")
    assert html.index("cashflowsHideZeroRowsButton") < html.index("cashflowsLazyActions")
    assert html.index("cashflowsExportButton") < html.index("cashflowsLazyActions")
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

def test_cashflows_report_loads_administrator_defaults_and_monthly_grouping():
    settings_html = (ROOT / "static" / "settings.html").read_text(encoding="utf-8")
    settings_script = (ROOT / "static" / "settings.js").read_text(encoding="utf-8")
    reconciliation_html = (ROOT / "static" / "reconciliation.html").read_text(encoding="utf-8")
    reconciliation_script = (ROOT / "static" / "reconciliation.js").read_text(encoding="utf-8")

    assert "cashflowDefaultsContainer" in settings_html
    assert "Reports -&gt; Defaults" in settings_script
    assert "/api/settings/cashflows-defaults" in settings_script
    assert "cashflowFiscalYearStartMonth" in settings_script
    assert "cashflowDefaultPeriod" in settings_script
    assert "cashflowCustomStart" in settings_script
    assert "cashflowCustomEnd" in settings_script
    assert "/api/settings/cashflows-defaults" in reconciliation_script
    assert "await loadCashflowDefaultPeriods();" in reconciliation_script
    assert "defaultPeriods();" in reconciliation_script
    assert "groupBy: 'month'" in reconciliation_script
    assert "cashflowsExportButton" in reconciliation_html
    assert "cashflows-report-actions" in reconciliation_html
    assert "cashflowsHideZeroRowsButton" in reconciliation_html
    assert "cashflowsSummaryFilter" in reconciliation_html
    assert "cashflowsSummaryClear" in reconciliation_html
    assert "type='text'" in reconciliation_html
    assert "id='cashflowsSummaryFilter' type='search'" not in reconciliation_html
    assert "cashflowsSummaryClear" in reconciliation_script
    assert "cashflowPreviewPeriods" in reconciliation_script
    assert "syncCashflowZeroRowsButton" in reconciliation_script
    assert "style.css?v=36" in reconciliation_html
    assert "reconciliation.js?v=11" in reconciliation_html
    assert "cashflowsSummaryBasis" not in reconciliation_html
    assert "cashflows-transactions-note" not in reconciliation_html
    assert "renderCashflowSummary" in reconciliation_script
    assert "formatInternationalAmount" in reconciliation_script
    assert "formatLocalAmount" in reconciliation_script
    assert "formatPercentage" in reconciliation_script
    assert "/api/reconciliation/cashflows/export?" in reconciliation_script
    assert "downloadCashflowReport" in reconciliation_script
