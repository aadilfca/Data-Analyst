from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_cashflows_report_contains_direct_classified_transaction_window():
    html = (ROOT / "static" / "reconciliation.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "reconciliation.js").read_text(encoding="utf-8")
    styles = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert "cashflowsTransactionsSection" in html
    assert "cashflowsCustomerPaymentsCustomerSearch" in html
    assert "cashflowsCustomerPaymentsCustomerOptions" in html
    assert "cashflowsCustomerPaymentsCustomerWiseButton" in html
    assert "cashflowsCustomerPaymentsPeriodWiseButton" in html
    assert "cashflowsCustomerPaymentsPeriodWiseSelect" not in html
    assert "cashflowsCustomerPeriodWiseTable" in html
    assert "cashflowsCustomerWiseTable" in html
    assert "cashflowsCustomerPaymentsVoucherPanel" in html
    assert "cashflowsCustomerPaymentsHeading" in html
    assert "showCashflowVoucherPayments" in script
    assert "renderCashflowCustomerWise" in script
    assert "renderCashflowCustomerPeriodWise" in script
    assert "customer_payment_account_period_totals" in script
    assert "customer_payment_customer_totals" in script
    assert "customer_payment_reconciliation" in script
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
    assert 'let cashflowReportSourceKey = "";' in script
    assert "cashflowReportRevisionFromResult" in script
    assert "cashflowReportSourceKeyFromResult" in script
    assert "cashflow_report_source_key" in script
    assert "metadata.cashflow_report_revision" in script
    assert "syncCashflowReportRevision" in script
    assert "resetCashflowDrilldownFilters" in script
    assert "cashflowsDrilldownAccountHead" in script
    assert "cashflowLedgerContextKey" in script
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
    assert ".cashflows-treasury-control { width: 190px; }" in styles
    assert ".cashflows-period-control { position: relative; width: 180px; }" in styles
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

def test_cashflows_report_does_not_expose_preparation_status_polling():
    html = (ROOT / "static" / "reconciliation.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "reconciliation.js").read_text(encoding="utf-8")
    account_control = (ROOT / "static" / "account_control_center.js").read_text(encoding="utf-8")

    assert "cashflowsPreparationAlert" not in html
    assert "/api/reconciliation/cashflows/preparation-status" not in script
    assert "/api/reconciliation/cashflows/preparation-retry" not in script
    assert "Back office Cashflows" not in script
    assert "/api/reconciliation/cashflows/preparation-status" not in account_control
    assert "/api/reconciliation/cashflows/preparation-retry" not in account_control
    assert "Back office Cashflows" not in account_control


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
    assert 'style.css?v=71' in inline_html
    assert 'cashflows-summary-table-host' in inline_html
    assert 'renderCashflowSummaryPaneLayout' in inline_script
    assert 'syncCashflowSummaryPaneRows' in inline_script
    assert 'syncCashflowSummaryHeaderScroll' in inline_script
    assert 'syncCashflowSummaryRowHover' in inline_script
    assert 'addColGroup(frozenTable' in inline_script
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
    assert 'cashflows-inline-ledger-row td:nth-child(n+2)' in inline_styles
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

def test_cashflows_report_preserves_current_default_period_and_monthly_grouping():

    settings_html = (ROOT / "static" / "settings.html").read_text(encoding="utf-8")
    settings_script = (ROOT / "static" / "settings.js").read_text(encoding="utf-8")
    reconciliation_html = (ROOT / "static" / "reconciliation.html").read_text(encoding="utf-8")
    reconciliation_script = (ROOT / "static" / "reconciliation.js").read_text(encoding="utf-8")
    reconciliation_styles = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert "cashflowDefaultsContainer" in settings_html
    assert "Reports -&gt; Defaults" in settings_script
    assert "/api/settings/cashflows-defaults" in settings_script
    assert "cashflowFiscalYearStartMonth" in settings_script
    assert "cashflowDefaultPeriod" in settings_script
    assert "cashflowCustomStart" in settings_script
    assert "cashflowCustomEnd" in settings_script
    assert "const CASHFLOW_DEFAULT_START = '2026-01-01';" in reconciliation_script
    assert "const CASHFLOW_DEFAULT_END = '2026-06-30';" in reconciliation_script
    assert "await loadCashflowDefaultPeriods();" in reconciliation_script
    assert "defaultPeriods();" in reconciliation_script
    assert "groupBy: 'month'" in reconciliation_script
    assert "cashflowsExportButton" in reconciliation_html
    assert "cashflows-report-actions" in reconciliation_html
    assert "cashflows-load-actions" in reconciliation_html
    assert "cashflows-notification-stack" in reconciliation_html
    assert "cashflowsRefreshMenuButton" in reconciliation_html
    assert "cashflowsRefreshMenu" in reconciliation_html
    assert "cashflowsFreshnessAlert" in reconciliation_html
    assert "cashflowsFreshnessAlertClose" in reconciliation_html
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
    assert "style.css?v=71" in reconciliation_html
    assert "reconciliation.js?v=39" in reconciliation_html
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
    assert "setCashflowRefreshMenuOpen" in reconciliation_script
    assert "event.stopPropagation();" in reconciliation_script
    assert "refreshMenu.contains(event.target)" in reconciliation_script
    assert "generateCashflows(null, { forceRefresh: true })" in reconciliation_script
    assert "window.setTimeout" in reconciliation_script
    assert "5000" in reconciliation_script
    assert "cashflows-freshness-alert" in reconciliation_styles
    assert "overflow: visible; border-radius: 7px" in reconciliation_styles
    assert "background: #2f855a" in reconciliation_styles
    assert "cashflows-load-actions > .cashflows-primary { border-radius: 7px;" in reconciliation_styles
    assert "border: 0; border-radius: 0 7px 7px 0; background: #2f855a" in reconciliation_styles
    assert "min-width: 0; overflow: visible" in reconciliation_styles
    assert "overflow-x: auto" not in reconciliation_styles.split(".cashflows-report-scope", 1)[1].split(".cashflows-filter-bar label", 1)[0]
    assert "flex-wrap: nowrap" in reconciliation_styles
    assert "cashflows-notification-stack" in reconciliation_styles


def test_cashflows_summary_panes_contain_full_year_horizontal_overflow():
    playwright = pytest.importorskip("playwright.sync_api")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    headers = "".join("<th>Period-%s</th>" % index for index in range(15))
    cells = "".join("<td>%s</td>" % ("3,000,000" if index == 3 else "-") for index in range(15))
    frozen_cols = "<colgroup><col style='width:360px'></colgroup>"
    period_cols = "<colgroup>" + "".join("<col style='width:112px'>" for _ in range(15)) + "</colgroup>"
    markup = (
        "<div class='cashflows-summary-table-wrap'><div id='cashflowsSummaryTable' class='cashflows-summary-table-host'>"
        "<div class='cashflows-summary-pane-layout' style='--cashflows-summary-frozen-width:360px'>"
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


@pytest.fixture
def cashflow_database_totals_page():
    """Run the real report script/header against test data with no network access."""
    playwright = pytest.importorskip("playwright.sync_api")
    html = (ROOT / "static" / "reconciliation.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    script = (ROOT / "static" / "reconciliation.js").read_text(encoding="utf-8")
    header = html[html.index("<section id='cashflowsLedgerDrilldownSection'"):]
    header = header.split("<div class='cashflows-transactions-toolbar", 1)[0] + "</section>"
    header = header.replace(" hidden aria-label", " aria-label", 1)
    hook = """window.countsFixture = {
        render: renderCashflowDrilldownContext,
        localResult: cashflowDrilldownLocalResult,
        scope: function (value) {
            clientId = value.client_id;
            cashflowSelection = {start: value.period_start, end: value.period_end, groupBy: 'month'};
            lastCashflowsResult = {configuration: {source_template_id: value.template_id}};
        }
    };"""
    assert script.count("document.addEventListener('DOMContentLoaded', initialise);") == 1
    script = script.replace("document.addEventListener('DOMContentLoaded', initialise);", hook)
    with playwright.sync_playwright() as instance:
        browser = instance.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 260})
        page.route("**/*", lambda route: route.abort())
        page.set_content("<style>" + css + "</style><body class='reconciliation-page'><main style='padding:20px'>" + header + "</main></body>")
        page.add_script_tag(content=script)
        yield page
        browser.close()


def _render_database_totals(page, totals):
    page.evaluate("""totals => {
        window.countsFixture.scope({client_id: 7, template_id: 9,
            period_start: '2026-01-01', period_end: '2026-06-30'});
        window.countsFixture.render({treasury_view: 'combined', database_totals: totals});
    }""", totals)


def test_database_totals_render_formatted_counts_and_preserve_them_when_filtering(cashflow_database_totals_page):
    page = cashflow_database_totals_page
    totals = {"client_id": 7, "template_id": 9, "period_start": "2026-01-01",
              "period_end": "2026-06-30", "row_count": 12345, "voucher_count": 6789}
    _render_database_totals(page, totals)
    assert page.locator("#cashflowsDatabaseRowCount").inner_text() == "12,345"
    assert page.locator("#cashflowsDatabaseVoucherCount").inner_text() == "6,789"
    assert "Monthly" in page.locator("#cashflowsDrilldownContext").inner_text()
    assert "Cash and Bank Total" in page.locator("#cashflowsDrilldownContext").inner_text()
    assert "non-cash and zero-value rows" in page.locator("#cashflowsDrilldownDatabaseTotals").get_attribute("title")
    local = page.evaluate("""totals => window.countsFixture.localResult({
        rows: [{category: 'Operating', amount: 10}],
        result: {database_totals: totals}
    }, {search: 'no-match', category: '', accountHead: '', treasuryType: '', periodKey: ''}, 2)""", totals)
    assert local["total_rows"] == 0
    assert local["database_totals"] == totals


def test_database_totals_hide_stale_scopes_and_distinguish_zero_from_loading(cashflow_database_totals_page):
    page = cashflow_database_totals_page
    valid = {"client_id": 7, "template_id": 9, "period_start": "2026-01-01",
             "period_end": "2026-06-30", "row_count": 0, "voucher_count": 0}
    _render_database_totals(page, valid)
    assert page.locator("#cashflowsDatabaseRowCount").inner_text() == "0"
    assert page.locator("#cashflowsDatabaseVoucherCount").inner_text() == "0"
    for change in ({"client_id": 8}, {"template_id": 10}, {"period_start": "2026-02-01"},
                   {"period_end": "2026-12-31"}, {"row_count": -1, "voucher_count": 1.5}):
        _render_database_totals(page, {**valid, **change})
        assert page.locator("#cashflowsDatabaseRowCount").inner_text() == "—"
        assert page.locator("#cashflowsDatabaseVoucherCount").inner_text() == "—"
    _render_database_totals(page, None)
    assert page.locator("#cashflowsDatabaseRowCount").inner_text() == "—"


def test_database_totals_header_fits_desktop_and_narrow_screens(cashflow_database_totals_page):
    page = cashflow_database_totals_page
    for width in (1200, 390):
        page.set_viewport_size({"width": width, "height": 340})
        _render_database_totals(page, {"client_id": 7, "template_id": 9,
            "period_start": "2026-01-01", "period_end": "2026-06-30",
            "row_count": 12345, "voucher_count": 6789})
        metrics = page.evaluate("""() => ({
            documentWidth: document.documentElement.scrollWidth,
            right: document.querySelector('#cashflowsDrilldownDatabaseTotals').getBoundingClientRect().right
        })""")
        assert metrics["documentWidth"] <= width
        assert metrics["right"] <= width


def _ledger_report(row_count=2, complete=True, month='2026-01', view='combined'):
    context = dict(client_id=7, template_id=9, period_start=month + '-01',
        period_end=month + '-28', group_by='month', treasury_view=view,
        report_revision=12, source_key='client-data-v1:7:9', configuration_key='config-1')
    rows = [dict(category='Operating' if i % 2 == 0 else 'Other', period_key=month,
        period_label=month, account_code=f'A{i}', account_name=f'Account {i}',
        treasury_type='Cash' if view == 'cash' else 'Bank', amount=i + 0.25, line_count=1)
        for i in range(row_count)]
    # Use one category for the large-category completeness fixtures.
    if row_count > 2:
        for row in rows:
            row['category'] = 'Operating'
    categories = []
    for category in dict.fromkeys(row['category'] for row in rows):
        total = sum(row['amount'] for row in rows if row['category'] == category)
        categories.append({'category': category, month: total, 'Total': total})
    return dict(client_id=7, period_start=context['period_start'], period_end=context['period_end'],
        group_by='month', treasury_view=view, cashflow_report_revision=12,
        cashflow_configuration_key='config-1', ledger_context=context,
        configuration={'source_template_id': 9}, source={'ready': True, 'metadata': {
            'cashflow_report_source_key': context['source_key']}}, blocking_reasons=[],
        preview=dict(group_by='month', treasury_view=view, periods=[dict(key=month,
            start=context['period_start'], end=context['period_end'], label=month)],
            months=[month], category_totals=categories, treasury_view_totals=(categories if view != 'split' else [
                dict(category=row['category'], period_key=month, bank_amount=row['Total'],
                     cash_amount=0, mixed_amount=0, total_amount=row['Total']) for row in categories]),
            ledger_cache=dict(context=context, complete=complete, row_count=row_count,
                              rows=rows if complete else [])))


@pytest.fixture
def ledger_cache_page():
    """Real report DOM/script; every HTTP request is intercepted locally."""
    playwright = pytest.importorskip('playwright.sync_api')
    html = (ROOT / 'static/reconciliation.html').read_text(encoding='utf-8')
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
    html = re.sub(r'<link\b[^>]*>', '', html)
    html = html.replace('<head>', '<head><base href="https://cashflows.test/">')
    script = (ROOT / 'static/reconciliation.js').read_text(encoding='utf-8')
    css = (ROOT / 'static/style.css').read_text(encoding='utf-8')
    hook = '''window.ledgerFixture = {
        load: function (result) {
            clientId = result.client_id;
            cashflowBalancesPending = false;
            cashflowBalanceExpanded = {opening: false, closing: false};
            cashflowSelection = {start: result.period_start, end: result.period_end, groupBy: result.group_by};
            cashflowTreasuryView = result.treasury_view;
            element('cashflowsPeriodStart').value = result.period_start;
            element('cashflowsPeriodEnd').value = result.period_end;
            element('cashflowsView').hidden = false;
            element('reportsView').hidden = true;
            document.querySelector('.reconciliation-shell').classList.add('cashflows-view-active');
            renderResult(result);
        },
        expire: function () {cashflowRevisionCheckedAt = 0;},
        generate: function (hard) {return generateCashflows(null, {forceRefresh: hard});},
        ledger: function () {return loadCashflowDrilldown(true);},
        stripped: function () {return lastCashflowsResult;},
        stats: function () {return Array.from(cashflowLedgerCacheOrder.values());},
        parent: deriveCashflowSummaryFromParent,
        child: function (section, preview) { setActiveCashflowWindow(section); renderCashflowChild(section, preview); },
        customerPeriods: renderCashflowCustomerPeriodWise,
        customerTotals: renderCashflowCustomerWise,
        filter: function (value) {cashflowSummaryFilter = value; renderCashflowSummary(lastCashflowsResult.preview);},
        zeros: toggleCashflowZeroRows,
        remember: function (key, rows) {return rememberCashflowLedger('baseline', key, {rows: rows, result: {}});},
        mutateScope: function (change) {
            if (change.client_id) clientId = change.client_id;
            if (change.template_id) lastCashflowsResult.configuration.source_template_id = change.template_id;
            if (change.group_by) cashflowSelection.groupBy = change.group_by;
            if (change.treasury_view) cashflowTreasuryView = change.treasury_view;
            if (change.period_start) cashflowSelection.start = change.period_start;
        }
    };
    element('cashflowsSummaryTable').addEventListener('click', handleCashflowSummaryClick);'''
    script = script.replace("document.addEventListener('DOMContentLoaded', initialise);", hook)
    with playwright.sync_playwright() as instance:
        browser = instance.chromium.launch(headless=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.abort())
        page.set_content(html)
        page.add_style_tag(content=css)
        page.add_script_tag(content=script)
        yield page
        browser.close()


def _category_button(page, category='Operating'):
    return page.locator('.cashflows-inline-expand').filter(has_text=category).first


def test_cashflow_transaction_drawer_keeps_server_date_and_same_day_sequence(ledger_cache_page):
    page=ledger_cache_page
    page.evaluate('result => window.ledgerFixture.load(result)',_ledger_report())
    rows=[{'id':key,'row_data':{'Voucher Detail':voucher},'classification':{
        'voucher_id':voucher,'transaction_date':day,'debit':10,'credit':0}}
        for key,voucher,day in [(90,'V9','2026-01-01'),(10,'V1','2026-01-01'),(4,'V8','2026-01-02')]]
    page.route('**/api/data/cashflows-classification-preview?*',lambda route:route.fulfill(json={
        'rows':rows,'page':1,'limit':50,'total_rows':3}))
    _category_button(page).click()
    page.locator('.cashflows-inline-ledger-link').first.click()
    page.wait_for_function("document.getElementById('cashflowsInlineTransactionTable').innerText.includes('V9')")
    assert page.locator('#cashflowsInlineTransactionTable tbody tr td:nth-child(6)').all_text_contents()==['V9','V1','V8']
    assert page.locator('#cashflowsInlineTransactionTable tbody tr td:nth-child(5)').all_text_contents()==['2026-01-01','2026-01-01','2026-01-02']


@pytest.mark.parametrize('view', ['combined', 'bank', 'cash', 'split'])
def test_category_and_full_ledger_share_initial_report_cache(ledger_cache_page, view):
    page = ledger_cache_page
    requests = []
    page.on('request', lambda request: requests.append(request.url))
    page.evaluate('result => window.ledgerFixture.load(result)', _ledger_report(view=view))
    _category_button(page).click()
    page.wait_for_selector('.cashflows-inline-ledger-link')
    assert page.locator('.cashflows-inline-ledger-link').count() == 1
    _category_button(page, 'Other').click()
    assert page.locator('.cashflows-inline-ledger-link').count() == 2
    _category_button(page).click()
    _category_button(page).click()
    result = page.evaluate('() => window.ledgerFixture.ledger()')
    assert result['total_rows'] == 2
    assert requests == []
    stripped = page.evaluate('() => window.ledgerFixture.stripped()')
    assert 'ledger_cache' not in stripped['preview']
    assert stripped['preview']['ledger_rollups'] == []


def test_complete_category_over_200_rows_is_not_truncated(ledger_cache_page):
    page = ledger_cache_page
    page.evaluate('result => window.ledgerFixture.load(result)', _ledger_report(201))
    _category_button(page).click()
    page.wait_for_function("document.querySelectorAll('.cashflows-inline-ledger-link').length === 201")
    assert page.locator('[data-cashflow-inline-page]').count() == 0


def test_oversized_category_pages_are_explicit_and_reachable(ledger_cache_page):
    page = ledger_cache_page
    report = _ledger_report(201, complete=False)
    all_rows = _ledger_report(201)['preview']['ledger_cache']['rows']
    requested_pages = []

    def respond(route):
        number = int(parse_qs(urlparse(route.request.url).query)['page'][0])
        requested_pages.append(number)
        payload = dict(client_id=7, period_start=report['period_start'], period_end=report['period_end'],
            group_by='month', treasury_view='combined', ledger_context=report['ledger_context'],
            cashflow_report_revision=12, cashflow_configuration_key='config-1', page=number,
            page_count=2, total_rows=201, limit=200, rows=all_rows[(number-1)*200:number*200])
        route.fulfill(json=payload)

    page.route('**/api/reconciliation/cashflows/drilldown?*', respond)
    page.evaluate('result => window.ledgerFixture.load(result)', report)
    _category_button(page).click()
    page.wait_for_function("document.querySelectorAll('.cashflows-inline-ledger-link').length === 200")
    assert 'Amounts reflect this page' in page.locator('#cashflowsSummaryTable').inner_text()
    page.locator('[data-cashflow-inline-page="2"]').click()
    page.wait_for_function("document.querySelectorAll('.cashflows-inline-ledger-link').length === 1")
    assert page.locator('.cashflows-inline-ledger-link').inner_text() == 'Account 200'
    page.locator('[data-cashflow-inline-page="1"]').click()
    page.wait_for_function("document.querySelectorAll('.cashflows-inline-ledger-link').length === 200")
    assert requested_pages == [1, 2]


@pytest.mark.parametrize('failure', ['unavailable', 'changed'])
def test_expired_cache_never_displays_when_revision_check_fails(ledger_cache_page, failure):
    page = ledger_cache_page
    report = _ledger_report()
    page.evaluate('result => window.ledgerFixture.load(result)', report)
    if failure == 'unavailable':
        page.route('**/api/reconciliation/cashflows/revision?*', lambda route: route.fulfill(status=503, json={}))
    else:
        page.route('**/api/reconciliation/cashflows/revision?*', lambda route: route.fulfill(json={
            'cashflow_report_revision': 13, 'cashflow_report_source_key': 'client-data-v1:7:9',
            'cashflow_configuration_key': 'config-1'}))
    page.evaluate('() => window.ledgerFixture.expire()')
    _category_button(page).click()
    page.wait_for_selector('.cashflows-inline-ledger-error')
    assert 'Reload Cashflows' in page.locator('.cashflows-inline-ledger-error').inner_text()
    assert page.locator('.cashflows-inline-ledger-link').count() == 0


@pytest.mark.parametrize('change', [dict(client_id=8), dict(template_id=10), dict(group_by='quarter'),
                                  dict(treasury_view='cash'), dict(period_start='2026-02-01')])
def test_cache_is_scoped_to_complete_report_context(ledger_cache_page, change):
    page = ledger_cache_page
    requests = []
    page.route('**/api/reconciliation/cashflows/drilldown?*', lambda route: (
        requests.append(route.request.url), route.fulfill(status=503, json={})))
    page.evaluate('result => window.ledgerFixture.load(result)', _ledger_report())
    page.evaluate('change => window.ledgerFixture.mutateScope(change)', change)
    _category_button(page).click()
    page.wait_for_selector('.cashflows-inline-ledger-error')
    assert page.locator('.cashflows-inline-ledger-link').count() == 0
    assert requests == []


def test_ledger_cache_evicts_by_combined_rows_and_utf8_bytes(ledger_cache_page):
    page = ledger_cache_page
    outcome = page.evaluate('''() => {
        const rows = Array.from({length: 3000}, (_, i) => ({account_code: String(i), amount: i}));
        ledgerFixture.remember('one', rows);
        ledgerFixture.remember('two', rows);
        const afterRows = ledgerFixture.stats();
        const oversized = ledgerFixture.remember('huge', [{account_name: '账'.repeat(800000)}]);
        return {afterRows, oversized, final: ledgerFixture.stats()};
    }''')
    assert [entry['key'] for entry in outcome['afterRows']] == ['two']
    assert outcome['oversized'] is False
    assert sum(entry['rows'] for entry in outcome['final']) <= 5000
    assert sum(entry['bytes'] for entry in outcome['final']) <= 2 * 1024 * 1024
    entries = page.evaluate('''() => {
        ledgerFixture.remember('bytes-one', [{account_name: 'a'.repeat(1100000)}]);
        ledgerFixture.remember('bytes-two', [{account_name: 'b'.repeat(1100000)}]);
        return ledgerFixture.stats();
    }''')
    assert [entry['key'] for entry in entries] == ['bytes-two']


def test_hard_refresh_reloads_source_and_reseeds_rollups(ledger_cache_page):
    page = ledger_cache_page
    report = _ledger_report()
    requests = []

    def respond(route):
        requests.append(parse_qs(urlparse(route.request.url).query))
        route.fulfill(json=report)

    page.route('**/api/reconciliation/cashflows/preview?*', respond)
    page.evaluate('result => window.ledgerFixture.load(result)', report)
    page.evaluate('() => window.ledgerFixture.generate(true)')
    assert requests[0]['refresh'] == ['true']
    assert requests[0]['warm_ledger_cache'] == ['true']
    assert sum(entry['rows'] for entry in page.evaluate('() => window.ledgerFixture.stats()')) == 2


def test_late_summary_response_cannot_replace_new_period(ledger_cache_page):
    page = ledger_cache_page
    january, february = _ledger_report(), _ledger_report(month='2026-02')
    page.evaluate('result => ledgerFixture.load(result)', january)
    page.evaluate('''() => {
        window.pendingReports = [];
        // Deliberately ignore abort to prove generation/context rejection too.
        window.fetch = () => new Promise(resolve => pendingReports.push(resolve));
        window.firstReport = ledgerFixture.generate(true);
    }''')
    page.evaluate('result => ledgerFixture.load(result)', february)
    page.evaluate('() => {window.secondReport = ledgerFixture.generate(true);}')
    assert page.evaluate('() => pendingReports.length') == 2
    page.evaluate('''async result => {
        pendingReports[1](new Response(JSON.stringify(result)));
        await secondReport;
    }''', february)
    page.evaluate('''async result => {
        pendingReports[0](new Response(JSON.stringify(result)));
        await firstReport;
    }''', january)
    assert page.evaluate('() => ledgerFixture.stripped().period_start') == '2026-02-01'
    assert sum(entry['rows'] for entry in page.evaluate('() => ledgerFixture.stats()')) == 2


def test_parent_summary_reuse_drops_incompatible_ledger_data(ledger_cache_page):
    report = _ledger_report()
    report['preview']['period_metrics'] = {'2026-01': dict(source_line_count=2,
        selected_voucher_count=1, internal_transfer_count=0, category_review_count=0, deduction_count=0)}
    derived = ledger_cache_page.evaluate('''result => ledgerFixture.parent(result,
        result.period_start, result.period_end, 'quarter', 'combined')''', report)
    assert derived is not None
    assert derived['preview']['ledger_rollups'] == []
    assert 'ledger_cache' not in derived['preview']


def test_incomplete_fallback_response_is_not_cached_or_displayed(ledger_cache_page):
    page = ledger_cache_page
    report = _ledger_report(201, complete=False)
    page.route('**/api/reconciliation/cashflows/drilldown?*', lambda route: route.fulfill(json=dict(
        ledger_context=report['ledger_context'], rows=[], total_rows=201, page=1, page_count=2)))
    page.evaluate('result => ledgerFixture.load(result)', report)
    _category_button(page).click()
    page.wait_for_selector('.cashflows-inline-ledger-error')
    assert 'Incomplete account details' in page.locator('.cashflows-inline-ledger-error').inner_text()
    assert page.locator('.cashflows-inline-ledger-link').count() == 0
    assert page.evaluate('() => ledgerFixture.stats()') == []


def _balance_report(view='combined', month='2026-01', two_periods=False):
    report = _ledger_report(view=view, month=month)
    keys = [month, '2026-02'] if two_periods else [month]
    periods = [dict(key=key, start=key+'-01', end=key+'-28', label=key) for key in keys]
    report['period_end'] = periods[-1]['end']
    report['ledger_context']['period_end'] = report['period_end']
    report['preview']['periods'] = periods
    report['preview']['months'] = keys
    bank_codes = ['B1'] if view != 'cash' else []
    cash_codes = ['C1'] if view != 'bank' else []
    types = bank_codes + cash_codes
    names = {'B1': 'Bank Alpha', 'C1': 'Office Cash'}
    typed = {'B1': 'Bank', 'C1': 'Cash'}
    source_periods = []
    for index, period in enumerate(periods):
        opening = {'B1': 100+50*index, 'C1': 50-20*index}
        closing = {'B1': opening['B1']+50, 'C1': opening['C1']-20}
        accounts = [dict(account_code=code, account_name=names[code], treasury_type=typed[code],
            opening_balance=opening[code], closing_balance=closing[code], status='Ready') for code in types]
        source_periods.append(dict(period, accounts=accounts, complete=True, status='Matched',
            opening_total=sum(opening[c] for c in types), closing_total=sum(closing[c] for c in types),
            calculated_opening=sum(opening[c] for c in types), calculated_closing=sum(closing[c] for c in types),
            cashflow_net_movement=sum(closing[c]-opening[c] for c in types), variance=0))
    def make_evidence(values):
        return dict(periods=values, evidence_fingerprint='1'*64, ready=True, coverage_complete=True,
            status='Matched', diagnostics=[], overall=dict(opening_total=values[0]['opening_total'],
                closing_total=values[-1]['closing_total'], cashflow_net_movement=sum(p['cashflow_net_movement'] for p in values)))
    evidence = make_evidence(source_periods)
    if view == 'split':
        evidence['treasury_breakdown'] = {}
        for code, key in (('B1', 'bank'), ('C1', 'cash')):
            entries = []
            for period in source_periods:
                account = next(a for a in period['accounts'] if a['account_code']==code)
                entries.append(dict(period, opening_total=account['opening_balance'], closing_total=account['closing_balance'],
                    calculated_opening=account['opening_balance'], calculated_closing=account['closing_balance'],
                    cashflow_net_movement=50 if code=='B1' else -20, accounts=[account]))
            evidence['treasury_breakdown'][key] = make_evidence(entries)
        evidence['unresolved_movements'] = {key:0 for key in keys}
    report['preview']['balance_reconciliation'] = evidence
    receipts, payments = (0, -20) if view=='cash' else (50, 0) if view=='bank' else (50, -20)
    categories = [dict(category=name, Total=value*len(keys), **{key:value for key in keys})
                  for name,value in (('Operating',receipts),('Other',payments))]
    report['preview']['category_totals'] = categories
    report['preview']['treasury_view_totals'] = categories if view!='split' else [
        dict(category=name,period_key=key,bank_amount=bank,cash_amount=cash,mixed_amount=0,total_amount=bank+cash)
        for key in keys for name,bank,cash in (('Operating',50,0),('Other',0,-20))]
    report['preview']['period_metrics'] = {key:dict(source_line_count=2, selected_voucher_count=2,
        internal_transfer_count=0, category_review_count=0,deduction_count=0) for key in keys}
    return report


def _balance_toggle(page, kind='opening'):
    return page.locator('[data-cashflow-balance-toggle="'+kind+'"]')


def _c4_report(view):
    import pandas as pd
    from services.ledger_group_summary import build_cashflow_summary
    from routers.reconciliation import _cashflow_preview
    report = _balance_report(view)
    journal = pd.DataFrame([
        ['T', '2026-01-05', 'B1', 0, 100], ['T', '2026-01-05', 'C1', 100, 0],
        ['M', '2026-01-06', 'B1', 0, 105], ['M', '2026-01-06', 'C1', 100, 0], ['M', '2026-01-06', 'E', 5, 0],
    ], columns=['voucher_id', 'date', 'account_code', 'debit', 'credit'])
    coa = pd.DataFrame([dict(account_code=code, account_title=code, category='Charges' if code == 'E' else '') for code in ['B1', 'C1', 'E']])
    summary = build_cashflow_summary(journal, coa, ['C1'], ['B1'], report['period_start'], report['period_end'])
    report['preview'].update(_cashflow_preview(summary, {'transfers'}, view))
    evidence = report['preview']['balance_reconciliation']
    parts = dict(evidence.get('treasury_breakdown') or {})
    parts['combined' if view == 'split' else view] = evidence
    for kind, part in parts.items():
        opening, closing, movement = {'bank': (300, 95, -205), 'cash': (100, 300, 200), 'combined': (400, 395, -5)}[kind]
        part['periods'][0].update(opening_total=opening, closing_total=closing, calculated_opening=opening,
            calculated_closing=closing, cashflow_net_movement=movement, variance=0)
        part['overall'].update(opening_total=opening, closing_total=closing, cashflow_net_movement=movement)
    return report


@pytest.mark.parametrize('view,expected', [('bank', ['-100']), ('cash', ['100']), ('combined', ['-']), ('split', ['-100', '100', '-', '-'])])
def test_treasury_bridge_uses_actual_source_and_keeps_mixed_allocation_review(ledger_cache_page, view, expected):
    page = ledger_cache_page
    report = _c4_report(view)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.evaluate('result=>ledgerFixture.load(result)', report)
    assert page.locator('.cashflows-summary-frozen-table .cashflows-balance-bridge').count() == 3
    bridge = page.locator('.cashflows-summary-scroll-table .cashflows-balance-bridge').nth(1)
    assert bridge.locator('td').all_inner_texts()[:len(expected)] == expected
    assert 'allocation under review' in page.locator('.cashflows-summary-frozen-table .cashflows-balance-bridge').nth(2).inner_text()
    assert _balance_values(page, '.cashflows-balance-net')[0] == ('200' if view == 'cash' else '-5' if view == 'combined' else '-205')
    page.evaluate("() => ledgerFixture.filter('nothing matches')")
    assert page.locator('.cashflows-summary-frozen-table .cashflows-balance-bridge').count() == 3
    assert not errors
    if view == 'split':
        assert _balance_values(page, '.cashflows-balance-bridge')[:4] == ['-', '-', '-5', '-5']
        mixed = page.locator('.cashflows-summary-scroll-table .cashflows-balance-bridge').nth(2)
        assert mixed.locator('td').all_inner_texts()[:4] == ['-105', '100', '5', '-']
        page.screenshot(path='C:/Users/LENOVO/.codex/visualizations/2026/10/21/01a15eeb-7a5e-7ef2-9607-b5ec5a138869/c4-mixed-bridge.png', full_page=True)


def test_treasury_bridge_refresh_preserves_expansion_and_does_not_duplicate_rows(ledger_cache_page):
    page = ledger_cache_page
    report = _c4_report('split')
    page.evaluate('result=>ledgerFixture.load(result)', report)
    page.evaluate('''() => {
        window.fetch = () => new Promise(resolve => {window.c4Resolver = resolve;});
        window.c4Refresh = ledgerFixture.generate(false);
    }''')
    page.wait_for_function('typeof c4Resolver === "function"')
    _balance_toggle(page, 'closing').click()
    page.evaluate('''async report => {c4Resolver(new Response(JSON.stringify(report))); await c4Refresh;}''', report)
    assert _balance_toggle(page, 'closing').get_attribute('aria-expanded') == 'true'
    assert page.locator('.cashflows-summary-frozen-table .cashflows-balance-bridge').count() == 3
    assert page.locator('.cashflows-summary-scroll-table .cashflows-balance-bridge').count() == 3


def test_treasury_parent_cache_aggregates_proofs_to_the_selected_period_only(ledger_cache_page):
    page = ledger_cache_page
    report = _c4_report('split')
    derived = page.evaluate("report => ledgerFixture.parent(report, '2026-01-01', '2026-01-28', 'quarter', 'split')", report)
    assert derived is not None
    proof = derived['preview']['treasury_movement_proof']
    assert {row['period_key'] for row in proof} == {'2026-Q1'}
    assert next(row for row in proof if row['treasury_type'] == 'bank')['source_movement'] == -205
    assert derived['preview']['treasury_view_reconciliation']['allocation_status'] == 'Review required'
    assert page.evaluate("report => ledgerFixture.parent(report, '2026-01-02', '2026-01-28', 'quarter', 'split')", report) is None


@pytest.mark.parametrize('reversal', [False, True])
def test_split_monthly_payment_subtotals_share_section_sign_and_preserve_reversals(ledger_cache_page, reversal):
    page = ledger_cache_page
    report = _balance_report('split', two_periods=True)
    if reversal:
        for row in report['preview']['treasury_view_totals']:
            if row['category'] == 'Other':
                row['cash_amount'] = row['total_amount'] = 40 if row['period_key'] == '2026-01' else -60
        for row in report['preview']['category_totals']:
            if row['category'] == 'Other':
                row.update({'2026-01': 40, '2026-02': -60, 'Total': -20})
    page.evaluate('result=>ledgerFixture.load(result)', report)
    totals = page.locator('.cashflows-summary-scroll-table .cashflows-summary-total-row')
    assert totals.nth(0).locator('td').all_inner_texts()[:9] == ['50', '-', '50', '50', '-', '50', '100', '-', '100']
    expected = ['-', '(40)', '(40)', '-', '60', '60', '-', '20', '20'] if reversal else ['-', '20', '20', '-', '20', '20', '-', '40', '40']
    assert totals.nth(1).locator('td').all_inner_texts()[:9] == expected


def _balance_values(page, selector):
    return page.locator('.cashflows-summary-scroll-table '+selector).locator('td').all_text_contents()


@pytest.mark.parametrize('view', ['combined','bank','cash','split'])
def test_balance_rows_start_compact_and_expand_names_with_code_hover(ledger_cache_page, view):
    page = ledger_cache_page
    page.evaluate('result=>ledgerFixture.load(result)', _balance_report(view))
    assert page.locator('.cashflows-summary-frozen-table .cashflows-balance-row').count()==2
    assert page.locator('.cashflows-balance-account:visible').count()==0
    _balance_toggle(page).click()
    assert _balance_toggle(page).get_attribute('aria-expanded')=='true'
    expected = 1 if view in ('bank','cash') else 2
    labels = page.locator('.cashflows-summary-frozen-table [data-cashflow-balance-child="opening"] .cashflows-balance-account-label')
    assert labels.count()==expected
    assert labels.first.is_visible()
    assert 'B1' not in labels.first.inner_text()
    assert labels.first.get_attribute('title').startswith('Account code: ')
    assert page.locator('.cashflows-summary-frozen-table .cashflows-balance-calculated:visible').inner_text()=='Calculated opening balance'
    _balance_toggle(page).focus()
    page.keyboard.press('Space')
    assert _balance_toggle(page).get_attribute('aria-expanded')=='false'
    page.keyboard.press('Enter')
    assert _balance_toggle(page).get_attribute('aria-expanded')=='true'


def test_split_source_and_calculated_columns_match_with_first_opening_last_closing_totals(ledger_cache_page):
    page = ledger_cache_page
    page.evaluate('result=>ledgerFixture.load(result)', _balance_report('split',two_periods=True))
    opening = _balance_values(page,'.cashflows-balance-row:first-of-type')
    assert opening[:3]==['100 Dr','50 Dr','150 Dr']
    assert opening[3:6]==['150 Dr','30 Dr','180 Dr']
    assert opening[6:9]==['100 Dr','50 Dr','150 Dr']
    closing = _balance_values(page,'.cashflows-balance-row[data-cashflow-balance-kind="closing"]')
    assert closing[:3]==['150 Dr','30 Dr','180 Dr']
    assert closing[3:6]==closing[6:9]==['200 Dr','10 Dr','210 Dr']
    _balance_toggle(page,'closing').click()
    assert _balance_values(page,'.cashflows-balance-calculated[data-cashflow-balance-child="closing"]')[:9]==closing[:9]
    assert _balance_values(page,'.cashflows-balance-net')[:3]==['50','-20','30']


def test_balances_survive_category_search_hide_zero_and_existing_category_expansion(ledger_cache_page):
    page=ledger_cache_page
    report=_balance_report()
    page.evaluate('result=>ledgerFixture.load(result)',report)
    _category_button(page).click()
    page.wait_for_selector('.cashflows-inline-ledger-link')
    before=page.locator('.cashflows-inline-ledger-link').count()
    _balance_toggle(page).click()
    assert page.locator('.cashflows-inline-ledger-link').count()==before
    page.evaluate("() => ledgerFixture.filter('nothing matches')")
    assert page.locator('.cashflows-summary-frozen-table .cashflows-balance-row').count()==2
    assert _balance_values(page,'.cashflows-balance-net')[0]=='30'
    assert _balance_toggle(page).get_attribute('aria-expanded')=='true'
    page.evaluate('() => ledgerFixture.zeros()')
    assert _balance_values(page,'.cashflows-balance-row:first-of-type')[0]=='150 Dr'


def test_zero_credit_partial_and_literal_source_names_are_displayed_without_guessing(ledger_cache_page):
    page=ledger_cache_page
    report=_balance_report()
    evidence=report['preview']['balance_reconciliation']
    period=evidence['periods'][0]
    period.update(opening_total=0,closing_total=None,status='Unavailable')
    period['accounts'][0].update(account_name='<b>Bank & Co</b>',opening_balance=0,closing_balance=-25,status='Partial')
    period['accounts'][1].update(opening_balance=0,closing_balance=None,status='Unavailable')
    evidence.update(ready=False,coverage_complete=False,diagnostics=[dict(account_code='C1',code='ledger_not_uploaded',message='Ledger not uploaded.')])
    page.evaluate('result=>ledgerFixture.load(result)',report)
    assert _balance_values(page,'.cashflows-balance-row:first-of-type')[0]=='0'
    _balance_toggle(page,'closing').click()
    labels=page.locator('.cashflows-summary-frozen-table [data-cashflow-balance-child="closing"] .cashflows-balance-account-label')
    assert labels.first.inner_text()=='<b>Bank & Co</b>'
    assert labels.locator('b').count()==0
    values=page.locator('.cashflows-summary-scroll-table .cashflows-balance-account[data-cashflow-balance-child="closing"]')
    assert values.nth(0).locator('td').first.inner_text()=='25 Cr'
    assert values.nth(1).locator('.cashflows-balance-unavailable').first.get_attribute('title')=='Ledger not uploaded.'
    assert _balance_values(page,'.cashflows-balance-row[data-cashflow-balance-kind="closing"]')[0]=='Unavailable'


def test_old_balance_is_never_reused_when_only_ledger_changes_without_voucher_revision(ledger_cache_page):
    page=ledger_cache_page
    report=_balance_report()
    page.evaluate('result=>ledgerFixture.load(result)',report)
    page.evaluate('''() => {
        window.fetch = (url) => new Promise(resolve => {window.balanceResolver=resolve;window.balanceURL=url;});
        window.balanceRefresh=ledgerFixture.generate(false);
    }''')
    page.wait_for_function('typeof balanceResolver === "function"')
    assert 'include_balances=true' in page.evaluate('() => balanceURL')
    assert _category_button(page).is_visible()
    assert _balance_values(page,'.cashflows-balance-row:first-of-type')[0]=='Checking…'
    _category_button(page).click()
    page.wait_for_selector('.cashflows-inline-ledger-link')
    _balance_toggle(page).click()
    fresh=_balance_report()
    fresh['preview']['balance_reconciliation']['evidence_fingerprint']='2'*64
    fresh['preview']['balance_reconciliation']['periods'][0]['opening_total']=999
    page.evaluate('''async report => {balanceResolver(new Response(JSON.stringify(report)));await balanceRefresh;}''',fresh)
    assert _balance_values(page,'.cashflows-balance-row:first-of-type')[0]=='999 Dr'
    assert page.locator('.cashflows-inline-ledger-link').count()==1
    assert _balance_toggle(page).get_attribute('aria-expanded')=='true'


@pytest.mark.parametrize('failure',['network','wrong_scope','missing_evidence','malformed_evidence'])
def test_balance_refresh_failure_retains_movements_but_never_old_balances(ledger_cache_page,failure):
    page=ledger_cache_page
    report=_balance_report()
    page.evaluate('result=>ledgerFixture.load(result)',report)
    if failure=='network':
        page.evaluate('''async () => {window.fetch=()=>Promise.reject(new TypeError('offline'));await ledgerFixture.generate(false);}''')
    else:
        fresh=_balance_report()
        if failure=='wrong_scope':fresh['client_id']=8
        elif failure=='missing_evidence':del fresh['preview']['balance_reconciliation']
        else:fresh['preview']['balance_reconciliation']['periods'][0]['start']='2025-01-01'
        page.route('**/api/reconciliation/cashflows/preview?*',lambda route:route.fulfill(json=fresh))
        page.evaluate('() => ledgerFixture.generate(false)')
    assert _category_button(page).is_visible()
    assert _balance_values(page,'.cashflows-balance-row:first-of-type')[0]=='Unavailable'
    assert page.locator('#cashflowsProcessing').is_hidden()


def test_parent_reuse_never_copies_or_adds_old_opening_closing_balances(ledger_cache_page):
    report=_balance_report()
    derived=ledger_cache_page.evaluate('''r=>ledgerFixture.parent(r,r.period_start,r.period_end,'quarter','combined')''',report)
    assert derived is not None
    assert 'balance_reconciliation' not in derived['preview']


def test_mixed_movements_remain_explicit_without_inventing_a_mixed_ledger_balance(ledger_cache_page):
    page=ledger_cache_page
    report=_balance_report('split')
    report['preview']['treasury_view_totals'].append(dict(category='Unresolved',period_key='2026-01',
        bank_amount=0,cash_amount=0,mixed_amount=3,total_amount=3))
    evidence=report['preview']['balance_reconciliation']
    evidence['unresolved_movements']['2026-01']=3
    evidence['periods'][0].update(cashflow_net_movement=33,calculated_closing=183,variance=-3,status='Variance')
    evidence['overall']['cashflow_net_movement']=33
    page.evaluate('result=>ledgerFixture.load(result)',report)
    assert _balance_values(page,'.cashflows-balance-row[data-cashflow-balance-kind="closing"]')[:4]==['150 Dr','30 Dr','—','180 Dr']
    assert _balance_values(page,'.cashflows-balance-net')[:4]==['50','-20','3','33']
    _balance_toggle(page,'closing').click()
    calculated=page.locator('.cashflows-summary-scroll-table .cashflows-balance-calculated[data-cashflow-balance-child="closing"]')
    assert calculated.locator('td').nth(3).inner_text()=='183 Dr'
    assert calculated.locator('td').nth(3).locator('.cashflows-balance-review').count()==1


def test_mismatched_split_evidence_is_unavailable_in_both_balance_and_net_rows(ledger_cache_page):
    page=ledger_cache_page
    report=_balance_report('split')
    report['preview']['balance_reconciliation']['treasury_breakdown']['bank']['periods'][0]['start']='2025-01-01'
    page.evaluate('result=>ledgerFixture.load(result)',report)
    assert _balance_values(page,'.cashflows-balance-row:first-of-type')[:3]==['Unavailable','50 Dr','150 Dr']
    assert _balance_values(page,'.cashflows-balance-net')[:3]==['Unavailable','-20','30']
    assert 'Incomplete ledger evidence' in page.locator('.cashflows-summary-frozen-table .cashflows-balance-row').first.inner_text()


def test_late_balance_response_cannot_replace_a_new_period(ledger_cache_page):
    page=ledger_cache_page
    january,february=_balance_report(),_balance_report(month='2026-02')
    february['preview']['balance_reconciliation']['periods'][0]['opening_total']=300
    page.evaluate('result=>ledgerFixture.load(result)',january)
    page.evaluate('''()=>{window.waiting=[];window.fetch=()=>new Promise(resolve=>waiting.push(resolve));window.firstBalance=ledgerFixture.generate(true);}''')
    page.evaluate('result=>ledgerFixture.load(result)',february)
    page.evaluate('''()=>{window.secondBalance=ledgerFixture.generate(true);}''')
    page.evaluate('''async result=>{waiting[1](new Response(JSON.stringify(result)));await secondBalance;}''',february)
    page.evaluate('''async result=>{waiting[0](new Response(JSON.stringify(result)));await firstBalance;}''',january)
    assert _balance_values(page,'.cashflows-balance-row:first-of-type')[0]=='300 Dr'
    assert page.evaluate('()=>ledgerFixture.stripped().period_start')=='2026-02-01'


def test_background_balance_completion_does_not_dismiss_an_active_ledger_load(ledger_cache_page):
    page=ledger_cache_page
    report=_balance_report()
    del report['preview']['ledger_cache']  # Force the independent drilldown to load remotely.
    page.evaluate('result=>ledgerFixture.load(result)',report)
    page.evaluate('''()=>{
        window.waiting=[];window.fetch=(url)=>new Promise(resolve=>waiting.push({url:url,resolve:resolve}));
        window.balanceLoad=ledgerFixture.generate(false);
    }''')
    page.wait_for_function('waiting.length===1')
    page.evaluate('''()=>{window.detailLoad=ledgerFixture.ledger();}''')
    page.wait_for_function('waiting.length===2')
    assert page.locator('#cashflowsProcessing').is_visible()
    title=page.locator('#cashflowsProcessingTitle').inner_text()
    page.evaluate('''async report=>{waiting[0].resolve(new Response(JSON.stringify(report)));await balanceLoad;}''',report)
    assert page.locator('#cashflowsProcessing').is_visible()
    assert page.locator('#cashflowsProcessingTitle').inner_text()==title
    response=dict(ledger_context=report['ledger_context'],rows=[],total_rows=0,page=1,page_count=1,
                  limit=200,client_id=7,period_start=report['period_start'],period_end=report['period_end'],
                  group_by='month',treasury_view='combined',source={'ready':True},blocking_reasons=[])
    page.evaluate('''async result=>{waiting[1].resolve(new Response(JSON.stringify(result)));await detailLoad;}''',response)
    assert page.locator('#cashflowsProcessing').is_hidden()


@pytest.mark.parametrize('width,view',[(1400,'combined'),(390,'combined'),(1400,'split'),(390,'split')])
def test_balance_expansion_keeps_frozen_and_numeric_rows_aligned_without_page_overflow(ledger_cache_page,width,view):
    page=ledger_cache_page
    page.set_viewport_size({'width':width,'height':800})
    report=_balance_report(view,two_periods=True)
    for period in report['preview']['balance_reconciliation']['periods']:
        period['accounts'][0]['account_name']='A long bank account name that wraps across the frozen account column'
    page.evaluate('result=>ledgerFixture.load(result)',report)
    _balance_toggle(page).click()
    _balance_toggle(page,'closing').click()
    page.wait_for_function('''() => {
        const right=new Map(Array.from(document.querySelectorAll('.cashflows-summary-scroll-table tr[data-cashflow-summary-row-id]')).map(row=>[row.dataset.cashflowSummaryRowId,row]));
        return Array.from(document.querySelectorAll('.cashflows-summary-frozen-table tbody tr[data-cashflow-summary-row-id]')).filter(row=>!row.hidden).every(row=>{
            const a=row.getBoundingClientRect(),b=right.get(row.dataset.cashflowSummaryRowId).getBoundingClientRect();
            return Math.abs(a.height-b.height)<=1 && Math.abs(a.top-b.top)<=1;
        });
    }''')
    assert page.evaluate('() => document.documentElement.scrollWidth')<=width
    if width==390:
        assert page.locator('.cashflows-summary-scroll-pane').evaluate('(e)=>e.clientWidth')>=140
    colors=page.evaluate('''() => ['.cashflows-balance-row','.cashflows-balance-calculated'].map(selector=>getComputedStyle(document.querySelector(selector+' td')).backgroundColor)''')
    assert colors==['rgb(226, 239, 218)','rgb(237, 241, 247)']


def test_signed_vendor_and_customer_period_columns_in_real_dom(ledger_cache_page):
    page = ledger_cache_page
    page.evaluate('result => window.ledgerFixture.load(result)', _ledger_report())
    vendor = dict(period_key='2026-01', period_label='Jan-26', account_code='AP-1',
                  account_name='Vendor <One>', category='Suppliers', payments=100, reversals=140, net_payments=-40)
    preview = dict(vendor_payment_period_totals=[vendor], vendor_payment_vendor_totals=[vendor])
    page.evaluate("p => window.ledgerFixture.child('vendor_payments', p)", preview)
    table = page.locator('#cashflowsVendorPaymentsTable')
    assert 'Reversals / refunds' in table.text_content()
    assert 'Vendor <One>' in table.inner_text()
    assert '-40' in table.inner_text()
    assert table.locator('td[title="AP-1"]').inner_text() == 'Vendor <One>'
    assert table.locator('script').count() == 0
    row = dict(period_key='2026-01', period_label='Jan-26', customer_account_code='AR-1',
               customer_account_name='Customer One', customer_receipts=100, customer_reversals=140,
               customer_gross_amount=-40, cashflow_amount=-40, status='Matched')
    page.evaluate('p => window.ledgerFixture.customerPeriods(p)',
                  dict(customer_payment_account_period_totals=[row], customer_payment_reconciliation={}))
    table = page.locator('#cashflowsCustomerPeriodWiseTable')
    assert 'Customer One' in table.inner_text()
    assert 'Reversals / refunds' in table.text_content()
    assert '-40' in table.inner_text()
    assert table.locator('td[title="AR-1"]').count() == 1


def test_parent_summary_discards_signed_party_details(ledger_cache_page):
    parent = _balance_report(two_periods=True)
    parent['preview']['vendor_payment_details'] = [{'voucher_id': 'OLD'}]
    parent['preview']['customer_payment_account_period_totals'] = [{'customer_reversals': 55}]
    derived = ledger_cache_page.evaluate("p => window.ledgerFixture.parent(p, '2026-01-01', '2026-01-28', 'month', 'combined')", parent)
    assert derived is not None
    assert derived['preview']['vendor_payment_details'] == []
    assert derived['preview']['customer_payment_account_period_totals'] == []
