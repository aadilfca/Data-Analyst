from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_cashflows_report_contains_direct_classified_transaction_window():
    html = (ROOT / "static" / "reconciliation.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "reconciliation.js").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert "cashflowsTransactionsSection" in html
    assert "cashflowsTransactionsTable" in html
    assert "cashflowsTransactionsPrevious" in html
    assert "cashflowsTransactionsNext" in html
    assert "cashflowsLedgerDrilldownButton" in html
    assert "cashflowsTreasuryViewSelect" in html
    assert "cashflowsTreasuryReconciliation" not in html
    assert "cashflowsLedgerDrilldownSection" in html
    assert "cashflowsDrilldownContext" in html
    assert "cashflowsSummaryControlsPanel" in html
    assert "cashflows-ledger-drilldown-table-wrap" in html
    assert "cashflows-transactions-table-wrap cashflows-ledger-drilldown-table-wrap" not in html
    assert "cashflows-drilldown-heading" in html
    assert "cashflowsLedgerDrilldownCount" not in html
    assert "cashflowsLedgerDrilldownControls" not in html
    assert "cashflowsDrilldownTreasury" in html
    assert "cashflowsDrilldownCategoryOptions" in html
    assert "cashflowsDrilldownAccountHead" in html
    assert "<select id='cashflowsDrilldownAccountHead'" in html
    assert "cashflowsDrilldownAccountHeadOptions" not in html
    assert html.index("cashflowsDrilldownCategory") < html.index("cashflowsDrilldownAccountHead") < html.index("cashflowsDrilldownTreasury")
    assert "renderCashflowDrilldownCategoryOptions" in script
    assert "renderCashflowDrilldownAccountOptions" in script
    assert "account_head_options" in script
    assert "account_head" in script
    assert "cashflowDrilldownBaseCache" in script
    assert "cashflowReportRevision" in script
    assert "cashflowReportRevisionFromResult" in script
    assert "metadata.cashflow_report_revision" in script
    assert "syncCashflowReportRevision" in script
    assert "resetCashflowDrilldownFilters" in script
    assert "cashflowsDrilldownAccountHead" in script
    assert "cashflowReportRevision].join" in script
    assert "resetCashflowDrilldownFilters();" in script
    assert "revision: cashflowReportRevision" in script
    assert "cashflowDrilldownLocalResult" in script
    assert "cacheCashflowDrilldownBaseline" in script
    assert "base_rows" in script
    assert "renderCashflowDrilldownContext" in script
    assert "cashflowsSummaryControlsPanel" in script
    assert "renderCashflowDrilldownControls" not in script
    assert "Ledger roll-up control:" not in script
    assert "syncCashflowDrilldownPagination" in script
    assert "cashflowsDrilldownPrevious" in html
    assert "cashflowsDrilldownNext" in html
    assert "/api/reconciliation/cashflows/drilldown?" in script
    assert "cashflowDrilldownController" in script
    assert "Loading ledger details" in script
    assert "Loading Cashflows ledger details" in script
    assert "event.key === 'Enter'" in script
    assert "cashflowsProcessingTitle" in script
    assert "openCashflowDrilldown" in script
    assert "renderCashflowDrilldown" in script
    assert "/api/data/cashflows-classification-preview?" in script
    assert "toggleClassifiedTransactions" in script
    assert "window.location.href = '/static/databank.html?'" not in script
    assert ".cashflows-transactions-section" in styles
    assert ".cashflows-drilldown-context" in styles
    assert "body .cashflows-drilldown-toolbar { display: grid;" in styles
    assert styles.index(".cashflows-transactions-toolbar { display: flex;") < styles.index("body .cashflows-drilldown-toolbar { display: grid;")
    assert "grid-template-columns: minmax(220px, 1.4fr)" in styles
    assert "overflow-x: auto" in styles
    assert ".cashflows-drilldown-toolbar { display: flex; flex-wrap: wrap; }" not in styles
    assert "body .cashflows-drilldown-toolbar > input[list], body .cashflows-drilldown-toolbar > select" in styles
    assert ".cashflows-ledger-drilldown-table-wrap" in styles
    assert "#cashflowsLedgerDrilldownTable thead th" in styles
    assert "overflow: visible; max-height: none" in styles
    assert ".cashflows-transactions-section .cashflows-transactions-table-wrap .cashflows-table th" in styles
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
    assert "cashflows-report-scope" in html
    assert "cashflows-report-heading" not in html
    assert "Cash Flow summary" not in html
    assert "cashflowsReceiptMode" not in html
    assert html.index("cashflowsGroupingSelect") < html.index("cashflowsTreasuryViewSelect") < html.index("cashflowsPeriodButton") < html.index("Load summary")
    assert ".cashflows-treasury-control { width: 235px; }" in styles
    assert ".cashflows-period-control { position: relative; width: 210px; }" in styles
    assert "Group by" in html
    assert "cashflowsCustomGrouping" not in html
    assert "cashflowsBackButton" not in html
    assert "cashflowsSourceStatus" not in html
    assert "cashflowsControls" not in html
    assert "cashflowsSourceQuality" not in html
    assert "cashflows-report-header" in html
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
    assert "setNavModule(isReport ? 'Cashflows summary' : 'Reports')" in script
    assert "cashflows-view-active" in script
    assert "cashflowsReceiptMode" not in script
    assert "setMessage" not in script
    assert "showCashflowToast" in script
    assert "showCashflowProcessing" in script
    assert "hideCashflowProcessing" in script
    assert "setCashflowProcessingStep" in script
    assert "cashflowProcessingControls" in script
    assert "group_by: cashflowSelection.groupBy" in script
    assert ".cashflows-grouping-control" in styles
    assert ".cashflows-template-note" not in styles
    assert ".cashflows-report-header" in styles
    assert ".cashflows-report { display: grid; gap: 10px; }" in styles
    assert ".reconciliation-page > .navbar {" in styles
    assert ".reconciliation-page > .reconciliation-shell { padding-top: 104px; }" in styles
    assert ".reconciliation-page > .reconciliation-shell.cashflows-view-active { padding-top: 78.5px; }" in styles
    assert ".cashflows-report-heading" not in styles
    assert ".cashflows-control-grid" not in styles
    assert ".cashflows-source-quality" not in styles
    assert ".cashflows-message" not in styles
    assert ".cashflows-toast" in styles
    assert ".cashflows-processing" in styles
    assert ".cashflows-processing-panel" in styles

def test_cashflows_summary_contains_inline_category_ledger_contract():
    inline_html = (ROOT / 'static' / 'reconciliation.html').read_text(encoding='utf-8')
    inline_script = (ROOT / 'static' / 'reconciliation.js').read_text(encoding='utf-8')
    inline_styles = (ROOT / 'static' / 'style.css').read_text(encoding='utf-8')
    assert 'cashflowsSummaryTable' in inline_html
    assert 'cashflows-summary-table-wrap' in inline_html
    assert 'cashflows-inline-expand' in inline_script
    assert 'cashflowInlineLedgerCache' in inline_script
    assert 'cashflowInlineLedgerLimit' in inline_script
    assert 'cashflowInlineTransactionCache' in inline_script
    assert 'cashflowInlineTransactionLimit' in inline_script
    assert 'cashflowDrawerTransactionKey' in inline_script
    assert 'data-cashflow-inline-category' in inline_script
    assert 'data-cashflow-inline-ledger' in inline_script
    assert '/api/data/cashflows-classification-preview?' in inline_script
    assert 'classification_type:' in inline_script
    assert 'openCashflowTransactionDrawer' in inline_script
    assert 'cashflowsInlineTransactionSearch' in inline_script
    assert 'cashflowsInlineTransactionPeriod' in inline_script
    assert 'cashflowsInlineTransactionMode' in inline_script
    assert 'cashflowsInlineTransactionCategory' in inline_script
    assert 'cashflowsInlineTransactionAccount' in inline_script
    assert 'treasury_view: filters.mode' in inline_script
    assert 'Voucher Detail' in inline_script
    assert "const columns = ['S.No', 'Voucher Detail', 'Receipts (Cr)', 'Payments (Dr)', 'Transaction Date', 'Voucher ID'];" in inline_script
    assert 'Debit Amount' not in inline_script
    assert 'Credit Amount' not in inline_script
    assert 'cashflowDrawerAmount(classification.credit)' in inline_script
    assert 'cashflowDrawerAmount(classification.debit)' in inline_script
    assert 'let totalReceipts = 0;' in inline_script
    assert 'let totalPayments = 0;' in inline_script
    assert 'cashflows-inline-transaction-total-row' in inline_script
    assert 'cashflows-inline-transaction-total-label' in inline_script
    assert 'cashflows-inline-transaction-total-empty' in inline_script
    assert '<th colspan=2 scope=row' not in inline_script
    assert 'cashflows-inline-transaction-total-label' in inline_styles
    assert '<tfoot' in inline_script
    assert 'cashflows-inline-transaction-table tfoot th' in inline_styles
    assert 'cashflows-inline-transaction-total-amount' in inline_styles
    assert 'white-space: nowrap; font-variant-numeric: tabular-nums;' in inline_styles
    assert 'Transaction Date' in inline_script
    assert 'Voucher ID' in inline_script
    assert 'Period selection pending - click Apply.' not in inline_script
    assert 'View selection pending - click Apply.' not in inline_script
    assert 'Loading transactions...' not in inline_script
    assert 'cashflows-inline-transaction-loading' in inline_script
    assert 'aria-busy' in inline_script
    assert 'cashflows-inline-transaction-table th:nth-child(3)' in inline_styles
    assert 'cashflows-inline-transaction-dialog.cashflows-inline-transaction-loading' in inline_styles
    assert 'Showing the selected transaction columns.' not in inline_script
    assert "cashflowsInlineTransactionStatus" in inline_script
    assert "function clearCashflowTransactionDrawerContent(drawer)" in inline_script
    assert "clearCashflowTransactionDrawerContent(drawer);" in inline_script
    assert "clearCashflowTransactionDrawerContent(cashflowInlineTransactionDrawer);" in inline_script
    assert 'hidden = true' in inline_script
    assert 'cashflowsInlineTransactionContext' not in inline_script
    assert 'Cashflows transaction detail' not in inline_script
    assert 'Ledger transactions' not in inline_script
    assert 'category: category' in inline_script
    assert 'button.disabled = false' in inline_script
    assert 'cashflows-inline-ledger-row' in inline_script
    assert 'cashflows-inline-ledger-host' in inline_styles
    assert 'cashflows-inline-ledger-error' in inline_styles
    assert 'cashflowInlineLedgerRowMarkup' in inline_script
    assert 'data-cashflow-inline-ledger-group' in inline_script
    assert 'cashflows-inline-ledger-link' in inline_script
    assert 'cashflows-inline-ledger-link' in inline_styles
    assert 'cashflows-inline-expand-icon' not in inline_script
    assert 'cashflows-inline-expand-icon' not in inline_styles
    assert 'style.css?v=64' in inline_html
    assert 'cashflows-summary-table-host' in inline_html
    assert 'renderCashflowSummaryPaneLayout' in inline_script
    assert 'syncCashflowSummaryPaneRows' in inline_script
    assert 'syncCashflowSummaryHeaderScroll' in inline_script
    assert 'syncCashflowSummaryRowHover' in inline_script
    assert 'applyCashflowSummaryColumnContract' in inline_script
    assert 'cashflowSummaryLogicalColumnCount' in inline_script
    assert 'cashflows-summary-pane-layout' in inline_styles
    assert 'cashflows-summary-sticky-header' in inline_styles
    assert 'cashflows-summary-scroll-header-viewport' in inline_styles
    assert 'cashflows-summary-row-hover' in inline_styles
    assert 'background: inherit;' not in inline_styles[inline_styles.find('.cashflows-summary-scroll-pane .cashflows-summary-pane-table tbody tr'):inline_styles.find('.cashflows-summary-scroll-pane .cashflows-summary-pane-table tbody tr') + 260]
    assert 'cashflows-summary-frozen-header-table' in inline_styles
    assert 'table-layout: fixed;' in inline_styles
    assert 'cashflows-summary-scroll-pane' in inline_styles
    assert 'cashflows-inline-ledger-row td:first-child' in inline_styles
    assert 'cashflows-inline-ledger-row td:not(:first-child)' in inline_styles
    assert 'cashflows-inline-ledger-table' not in inline_script
    assert 'cashflows-inline-ledger-table' not in inline_styles
    assert 'cashflows-inline-ledger-note' not in inline_script
    assert 'cashflows-inline-ledger-indent' not in inline_script
    assert 'cashflows-inline-transaction-table' in inline_styles
    assert 'cashflows-inline-transaction-drawer' in inline_styles
    assert 'cashflows-inline-transaction-dialog' in inline_styles
    assert 'cashflows-inline-transaction-table-wrap' in inline_styles
    assert '.cashflows-inline-transaction-table-wrap { position: relative; isolation: isolate; flex: 1 1 auto; overflow: auto; padding: 0 22px 16px; }' in inline_styles
    assert '.cashflows-inline-transaction-table thead' in inline_styles
    assert 'cashflows-inline-transaction-context' in inline_styles
    assert 'cashflows-inline-transaction-table thead' in inline_styles
    assert '.cashflows-inline-transaction-table { width: 100%; min-width: 0;' in inline_styles
    assert '.cashflows-inline-transaction-table th:nth-child(2)' in inline_styles
    assert 'white-space: normal; overflow-wrap: anywhere; word-break: break-word;' in inline_styles
    assert '.cashflows-summary-table-wrap { overflow: visible; }' in inline_styles
    assert '.cashflows-summary-frozen-pane' in inline_styles
    assert '.cashflows-summary-scroll-pane' in inline_styles
    assert 'overflow-y: clip' in inline_styles
    assert '.cashflows-summary-table-wrap::before' in inline_styles
    assert 'font-variant-numeric: tabular-nums;' in inline_styles

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
    assert "function setCashflowSummaryFilterVisibility" in reconciliation_script
    assert "setCashflowSummaryFilterVisibility(rawRows.length > 0 && (!hideCashflowZeroRows || hasActivityRows));" in reconciliation_script
    assert "setCashflowSummaryFilterVisibility(allRows.length > 0 && (!hideCashflowZeroRows || hasActivityRows));" in reconciliation_script
    assert "setCashflowSummaryFilterVisibility(false);" in reconciliation_script
    assert "cashflowPreviewPeriods" in reconciliation_script
    assert "syncCashflowZeroRowsButton" in reconciliation_script
    assert "style.css?v=64" in reconciliation_html
    assert "reconciliation.js?v=24" in reconciliation_html
    assert "cashflowsSummaryBasis" not in reconciliation_html
    assert "cashflows-transactions-note" not in reconciliation_html
    assert "renderCashflowSummary" in reconciliation_script
    assert "formatInternationalAmount" in reconciliation_script
    assert "formatLocalAmount" in reconciliation_script
    assert "formatPercentage" in reconciliation_script
    assert "/api/reconciliation/cashflows/export?" in reconciliation_script
    assert "downloadCashflowReport" in reconciliation_script
    assert "treasury_view: cashflowTreasuryView" in reconciliation_script
    assert "cashflowDefaultTreasuryType" in reconciliation_script


def test_cashflows_summary_panes_contain_full_year_horizontal_overflow():
    playwright = pytest.importorskip("playwright.sync_api")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    headers = "".join("<th>Period-%s</th>" % index for index in range(15))
    cells = "".join("<td>%s</td>" % ("3,000,000" if index == 3 else "-") for index in range(15))
    frozen_cols = "<colgroup><col style='width:360px'></colgroup>"
    period_cols = "<colgroup>" + "".join("<col style='width:112px'>" for _ in range(15)) + "</colgroup>"
    markup = (
        "<div class='cashflows-summary-table-wrap'><div id='cashflowsSummaryTable' class='cashflows-summary-table-host'>"
        "<div class='cashflows-summary-pane-layout'>"
        "<div class='cashflows-summary-sticky-header'>"
        "<div class='cashflows-summary-frozen-header-pane'><table class='cashflows-summary-header-table cashflows-summary-frozen-header-table'>" + frozen_cols +
        "<thead><tr><th>Category</th></tr></thead></table></div>"
        "<div class='cashflows-summary-scroll-header-viewport'><table class='cashflows-summary-header-table cashflows-summary-scroll-header-table'>" + period_cols +
        "<thead><tr>" + headers + "</tr></thead></table></div></div>"
        "<div class='cashflows-summary-body-layout'>"
        "<div class='cashflows-summary-frozen-pane'><table class='cashflows-table cashflows-summary-pane-table cashflows-summary-frozen-table'>" + frozen_cols +
        "<tbody><tr data-cashflow-summary-row-id='category-1'><td>Received from customers</td></tr></tbody></table></div>"
        "<div class='cashflows-summary-scroll-pane'><table class='cashflows-table cashflows-summary-pane-table cashflows-summary-scroll-table'>" + period_cols +
        "<tbody><tr data-cashflow-summary-row-id='category-1'>" + cells + "</tr></tbody></table></div></div>"
        "</div></div></div>"
    )
    with playwright.sync_playwright() as instance:
        browser = instance.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 700})
        page.set_content("<style>" + css + "</style>" + markup)
        pane = page.locator(".cashflows-summary-scroll-pane")
        pane.evaluate("element => { element.scrollLeft = 220; }")
        metrics = page.locator("#cashflowsSummaryTable").evaluate(
            """host => {
                const pane = host.querySelector('.cashflows-summary-scroll-pane');
                const frozen = host.querySelector('.cashflows-summary-frozen-pane');
                const stickyHeader = host.querySelector('.cashflows-summary-sticky-header');
                const header = host.querySelector('.cashflows-summary-frozen-header-table thead th');
                const scrollHeader = host.querySelector('.cashflows-summary-scroll-header-table thead th');
                const scrollHeaderTable = host.querySelector('.cashflows-summary-scroll-header-table');
                const frozenRow = host.querySelector('.cashflows-summary-frozen-pane tbody tr');
                const scrollRow = host.querySelector('.cashflows-summary-scroll-pane tbody tr');
                frozenRow.classList.add('cashflows-summary-row-hover');
                scrollRow.classList.add('cashflows-summary-row-hover');
                const paneRect = pane.getBoundingClientRect();
                const frozenRect = frozen.getBoundingClientRect();
                return {
                    documentScrollWidth: document.documentElement.scrollWidth,
                    paneScrollWidth: pane.scrollWidth,
                    paneClientWidth: pane.clientWidth,
                    frozenLeft: frozenRect.left,
                    paneLeft: paneRect.left,
                    paneRight: paneRect.right,
                    headerPosition: getComputedStyle(stickyHeader).position,
                    headerTop: header.getBoundingClientRect().top,
                    scrollHeaderTop: scrollHeader.getBoundingClientRect().top,
                    scrollHeaderWidth: scrollHeaderTable.getBoundingClientRect().width,
                    frozenRowBackground: getComputedStyle(frozenRow.cells[0]).backgroundColor,
                    firstPeriodBackground: getComputedStyle(scrollRow.cells[0]).backgroundColor,
                    lastPeriodBackground: getComputedStyle(scrollRow.cells[scrollRow.cells.length - 1]).backgroundColor
                };
            }"""
        )
        browser.close()
    assert metrics["documentScrollWidth"] <= 1200
    assert metrics["paneScrollWidth"] > metrics["paneClientWidth"]
    assert metrics["scrollHeaderWidth"] > metrics["paneClientWidth"]
    assert abs(metrics["frozenLeft"]) <= 1
    assert abs(metrics["paneLeft"] - 360) <= 1
    assert metrics["paneRight"] <= 1201
    assert metrics["headerPosition"] == "sticky"
    assert abs(metrics["headerTop"] - metrics["scrollHeaderTop"]) <= 1
    assert metrics["frozenRowBackground"] == "rgb(248, 251, 253)"
    assert metrics["firstPeriodBackground"] == "rgb(248, 251, 253)"
    assert metrics["lastPeriodBackground"] == "rgb(248, 251, 253)"
