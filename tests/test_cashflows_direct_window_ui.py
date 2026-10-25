from pathlib import Path
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from test_reconciliation_balance_source import independent_year_boundary_case, horizontal_reconciliation_case, _horizontal_summary
from test_reconciliation_reports import independent_year_boundary_report


@pytest.mark.parametrize('view', ['combined', 'split'])
def test_real_report_payload_renders_independent_year_boundary_balances(
        ledger_cache_page, independent_year_boundary_report, view):
    page = ledger_cache_page
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.evaluate('result => ledgerFixture.load(result)', independent_year_boundary_report(view))
    def numbers(selector):
        values = _balance_values(page, selector)
        result = []
        for value in values:
            amount = value.strip().replace(',', '').replace(' Dr', '').replace(' Cr', '')
            if amount == '-':
                result.append(0)
            elif re.fullmatch(r'-?\d+(\.\d+)?', amount):
                result.append(float(amount) * (-1 if value.strip().endswith(' Cr') else 1))
        return result
    opening = '.cashflows-balance-row[data-cashflow-balance-kind="opening"]'
    closing = '.cashflows-balance-row[data-cashflow-balance-kind="closing"]'
    if view == 'combined':
        expected_opening, expected_closing, expected_net = [-30, 67, 26.70, -30], [67, 26.70, 26.70, 26.70], [97, -40.30, 0, 56.70]
    else:
        # Mixed is an attribution column and has no ledger balance.
        expected_opening = [-30, 0, -30, 17, 50, 67, -9.56, 36.26, 26.70, -30, 0, -30]
        expected_closing = [17, 50, 67, -9.56, 36.26, 26.70, -9.56, 36.26, 26.70, -9.56, 36.26, 26.70]
        expected_net = [47, 50, 0, 97, -26.56, -13.74, 0, -40.30, 0, 0, 0, 0, 20.44, 36.26, 0, 56.70]
    assert numbers(opening) == pytest.approx(expected_opening)
    assert numbers(closing) == pytest.approx(expected_closing)
    assert numbers('.cashflows-balance-net') == pytest.approx(expected_net)
    assert '30 Cr' in _balance_values(page, opening)
    _balance_toggle(page, 'closing').click()
    assert numbers('.cashflows-balance-calculated[data-cashflow-balance-child="closing"]') == pytest.approx(expected_closing)
    page.evaluate("() => ledgerFixture.filter('nothing matches')")
    page.evaluate('() => ledgerFixture.zeros()')
    assert numbers(closing) == pytest.approx(expected_closing)
    assert numbers('.cashflows-balance-net') == pytest.approx(expected_net)
    assert 'allocation under review' in page.locator('.cashflows-summary-frozen-table .cashflows-balance-bridge').nth(2).inner_text()
    assert not errors


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
    assert "reconciliation.js?v=53" in reconciliation_html
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
            cashflowPendingAccounts = cashflowAccountCodes(result);
            element('cashflowsPeriodStart').value = result.period_start;
            element('cashflowsPeriodEnd').value = result.period_end;
            element('cashflowsView').hidden = false;
            element('reportsView').hidden = true;
            document.querySelector('.reconciliation-shell').classList.add('cashflows-view-active');
            renderResult(result);
        },
        expire: function () {cashflowRevisionCheckedAt = 0;},
        generate: function (hard) {return generateCashflows(null, {forceRefresh: hard});},
        accounts: function (accounts, pending) {
            cashflowAccountOptions = accounts;
            cashflowAccountOptionsClient = clientId;
            cashflowAccountOptionsKey = cashflowConfigurationKey;
            if (pending !== undefined) cashflowPendingAccounts = pending;
            renderCashflowAccountOptions();
        },
        pending: function () {return cashflowEffectivePendingAccounts();},
        children: loadCashflowChildren,
        classified: loadClassifiedTransactions,
        download: downloadCashflowReport,
        syncControls: syncCashflowSelection,
        summaryKey: cashflowSummaryContextKey,
        accountCache: function () {return cashflowAccountOptions;},
        renewLoadedReference: function () {lastCashflowsResult=JSON.parse(JSON.stringify(lastCashflowsResult));},
        setPending: function (codes) {cashflowPendingAccounts=codes;syncCashflowAccountButton();},
        setView: function (view) {cashflowTreasuryView=view;renderCashflowAccountOptions();},
        reconcile: openCashflowReconciliation,
        reconciliationLoad: loadCashflowReconciliation,
        reconciliationCache: function () {return Array.from(reconciliationCache.entries());},
        reconciliationState: function () {return {scope: reconciliationState.scope, result: reconciliationState.result,
            pending: !!reconciliationState.pending, loading: !!reconciliationState.loading, opener: !!reconciliationState.opener};},
        rememberReconciliation: rememberReconciliation,
        clearReconciliationCache: clearReconciliationCache,
        takeReconciliation: function (key) {return !!takeReconciliation(key);},
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
            if (change.period_end) cashflowSelection.end = change.period_end;
        }
    };
    initialiseCashflowReconciliation();
    initialiseCashflowAccounts();
    element('cashflowsSummaryTable').addEventListener('click', handleCashflowSummaryClick);'''
    script = script.replace("document.addEventListener('DOMContentLoaded', initialise);", hook)
    with playwright.sync_playwright() as instance:
        browser = instance.chromium.launch(headless=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.abort())
        page.set_content(html)
        page.add_style_tag(content=css)
        page.add_script_tag(content=(ROOT / 'static/loading_ui.js').read_text(encoding='utf-8'))
        page.add_script_tag(content=script)
        yield page
        browser.close()


def _category_button(page, category='Operating'):
    return page.locator('.cashflows-inline-expand').filter(has_text=category).first


def _account_choices():
    return [dict(account_code=code,account_name=name,treasury_type=kind) for code,name,kind in
        [('B1','Bank Alpha','bank'),('B2','Bank Beta','bank'),('C1','Cash <main>','cash'),('C2','Branch cash','cash')]]


def _selected_ui_report(codes,**kwargs):
    report=_ledger_report(**kwargs)
    selection=dict(version=1,mode='selected',account_codes=sorted(codes),configured_account_codes=['B1','B2','C1','C2'])
    report['account_selection']=selection
    report['ledger_context']['account_selection']=selection
    report['preview']['account_selection']=selection
    return report


def test_account_picker_search_checkboxes_pending_state_keyboard_and_empty_validation(ledger_cache_page):
    page=ledger_cache_page;calls=[]
    page.evaluate('report=>ledgerFixture.load(report)',_ledger_report())
    def options(route):
        calls.append(route.request.url)
        route.fulfill(json=dict(client_id=7,configuration_key='config-1',accounts=_account_choices()))
    page.route('**/api/reconciliation/cashflows/account-options?*',options)
    original=page.locator('#cashflowsSummaryTable').inner_text()
    page.locator('#cashflowsAccountButton').click()
    page.wait_for_selector('[data-cashflow-account="B1"]')
    assert page.locator('#cashflowsAccountAll').is_checked()
    page.locator('#cashflowsAccountClear').click()
    page.locator('#cashflowsAccountSearch').fill('alpha')
    assert page.locator('[data-cashflow-account]').count()==1
    page.locator('[data-cashflow-account="B1"]').check()
    page.locator('#cashflowsAccountSearch').fill('C1')
    page.locator('[data-cashflow-account="C1"]').check()
    assert page.locator('.account-name').inner_text()=='Cash <main>'
    assert page.locator('#cashflowsAccountOptions main').count()==0
    page.locator('#cashflowsAccountSearch').fill('')
    assert page.locator('[data-cashflow-account="B1"]').is_checked()
    assert page.locator('#cashflowsAccountValue').inner_text()=='2 selected'
    assert page.locator('#cashflowsAccountButton').get_attribute('class')=='is-pending'
    page.locator('#cashflowsAccountSearch').press('Escape')
    assert page.locator('#cashflowsAccountPopover').is_hidden()
    assert page.locator('#cashflowsAccountButton').evaluate('(node)=>node===document.activeElement')
    assert page.locator('#cashflowsSummaryTable').inner_text()==original
    assert page.evaluate('()=>ledgerFixture.stripped().account_selection || null') is None
    page.locator('#cashflowsAccountButton').click()
    page.locator('#cashflowsAccountClear').click()
    page.locator('#cashflowsAccountDone').click()
    page.evaluate('()=>ledgerFixture.generate(false)')
    assert 'at least one' in page.locator('#cashflowsToast').inner_text()
    assert page.locator('#cashflowsSummaryTable').inner_text()==original
    page.locator('#cashflowsAccountAll').check()
    assert page.evaluate('()=>ledgerFixture.pending()') is None
    assert len(calls)==1


@pytest.mark.parametrize('width',[1440,1366,1280,1024,390])
def test_selected_account_toolbar_and_picker_fit_desktop_and_narrow_screens(ledger_cache_page,width):
    page=ledger_cache_page;page.set_viewport_size(dict(width=width,height=900))
    page.evaluate('report=>ledgerFixture.load(report)',_ledger_report())
    page.evaluate('accounts=>ledgerFixture.accounts(accounts)',_account_choices())
    page.locator('#cashflowsAccountButton').click()
    metrics=page.evaluate('''()=>{
        const ids=['cashflowsGroupingSelect','cashflowsTreasuryViewSelect','cashflowsPeriodButton','cashflowsAccountButton','cashflowsLedgerDrilldownButton','cashflowsReconciliationButton','cashflowsExportButton'];
        return {controls:ids.map(id=>{const r=document.getElementById(id).getBoundingClientRect();return {id,x:r.x,y:r.y,right:r.right,bottom:r.bottom};}),
            picker:document.getElementById('cashflowsAccountPopover').getBoundingClientRect().toJSON(),width:document.documentElement.scrollWidth};
    }''')
    assert metrics['picker']['x']>=0 and metrics['picker']['right']<=width
    if width>=1280:
        bottoms=[row['bottom'] for row in metrics['controls']]
        assert max(bottoms)-min(bottoms)<10,metrics
        assert all(row['right']<=width for row in metrics['controls']),metrics
        assert metrics['controls'][2]['right']<=metrics['controls'][3]['x'],metrics
        assert page.locator('#cashflowsAccountButton').bounding_box()['x']+page.locator('#cashflowsAccountButton').bounding_box()['width']<=page.locator('#cashflowsReportForm button[type=submit]').bounding_box()['x']
    assert page.locator('#cashflowsLedgerDrilldownButton').inner_text()=='Ledger drill-down'
    assert page.locator('#cashflowsExportButton').inner_text()=='XLSX'


def test_explicit_selection_survives_view_change_and_incompatible_view_cannot_become_all(ledger_cache_page):
    page=ledger_cache_page
    page.evaluate('report=>ledgerFixture.load(report)',_ledger_report())
    page.evaluate('accounts=>ledgerFixture.accounts(accounts,["C1"])',_account_choices())
    page.evaluate('()=>ledgerFixture.setView("bank")')
    assert page.evaluate('()=>ledgerFixture.pending()')==[]
    page.evaluate('()=>ledgerFixture.generate(false)')
    assert 'compatible' in page.locator('#cashflowsToast').inner_text()
    page.evaluate('()=>ledgerFixture.setView("cash")')
    assert page.evaluate('()=>ledgerFixture.pending()')==['C1']


def test_parent_cache_never_derives_a_selected_scope_from_all_or_other_accounts(ledger_cache_page):
    page=ledger_cache_page
    report=_selected_ui_report(['B1'])
    report['preview']['period_metrics']={'2026-01':dict(source_line_count=2,selected_voucher_count=2,
        internal_transfer_count=0,category_review_count=0,deduction_count=0,selection_review_count=0)}
    page.evaluate('report=>ledgerFixture.load(report)',report)
    page.evaluate('accounts=>ledgerFixture.accounts(accounts,["B2"])',_account_choices())
    args=[report,report['period_start'],report['period_end'],'month','combined']
    assert page.evaluate('args=>ledgerFixture.parent(...args)',args) is None
    page.evaluate('()=>ledgerFixture.setPending(["B1"])')
    assert page.evaluate('args=>ledgerFixture.parent(...args)',args)['account_selection']['account_codes']==['B1']
    args[0]=_ledger_report()
    assert page.evaluate('args=>ledgerFixture.parent(...args)',args) is None


def test_selected_details_classification_and_excel_use_loaded_scope_despite_pending_edits(ledger_cache_page):
    page=ledger_cache_page;requests=[]
    report=_selected_ui_report(['B1'],complete=False)
    page.evaluate('report=>ledgerFixture.load(report)',report)
    page.evaluate('accounts=>ledgerFixture.accounts(accounts,["C1"])',_account_choices())
    page.evaluate('''()=>{ledgerFixture.mutateScope({period_start:'2026-04-01',period_end:'2026-06-30',group_by:'quarter',treasury_view:'cash'});
        document.getElementById('cashflowsPeriodStart').value='2026-04-01';document.getElementById('cashflowsPeriodEnd').value='2026-06-30';}''')
    def respond(route):
        query=parse_qs(urlparse(route.request.url).query);path=urlparse(route.request.url).path
        requests.append((path,query))
        if path.endswith('/drilldown'):
            route.fulfill(json=dict(report,rows=_ledger_report()['preview']['ledger_cache']['rows'],page=1,
                page_count=1,total_rows=2,limit=50))
        elif path.endswith('/preview'):
            route.fulfill(json=report)
        elif path.endswith('classification-preview'):
            route.fulfill(json=dict(rows=[],page=1,limit=50,total_rows=0,account_selection=report['account_selection']))
        else:
            route.fulfill(body=b'fixture',content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                headers={'Content-Disposition':'attachment; filename="Selected.xlsx"'})
    page.route('**/api/**',respond)
    assert page.evaluate('()=>ledgerFixture.ledger()')['account_selection']['account_codes']==['B1']
    page.evaluate('()=>ledgerFixture.children()')
    page.evaluate('()=>ledgerFixture.classified(true)')
    with page.expect_download():page.evaluate('()=>ledgerFixture.download("summary")')
    assert len(requests)==4
    for path,query in requests:
        assert query['selected_treasury_account_codes']==['B1'],(path,query)
        assert query.get('period_start',query.get('date_start'))==['2026-01-01']
        assert query.get('period_end',query.get('date_end'))==['2026-01-28']
        assert query.get('treasury_view')==['combined']
    assert page.evaluate('()=>ledgerFixture.pending()')==['C1']
    assert page.locator('#cashflowsExportButton').inner_text()=='XLSX'


def test_selected_reconciliation_scope_validation_and_background_export_follow_loaded_accounts(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    report,parent,support,posting=reconciliation_payload('combined')
    selection=dict(version=1,mode='selected',account_codes=['B1'],configured_account_codes=['B1','C1'])
    report['account_selection']=selection;report['preview']['account_selection']=selection
    parent.update(account_selection=selection,cache_schema=1,cache_identity='a'*64,template_id=report['configuration']['source_template_id'])
    queries=[]
    def respond(route):
        queries.append(parse_qs(urlparse(route.request.url).query))
        route.fulfill(json=parent)
    page.route('**/api/reconciliation/cashflows/reconciliation?*',respond)
    page.evaluate('report=>ledgerFixture.load(report)',report)
    page.evaluate('accounts=>ledgerFixture.accounts(accounts,["C1"])',_account_choices())
    page.evaluate('()=>ledgerFixture.reconcile()')
    assert queries[0]['selected_treasury_account_codes']==['B1']
    assert 'Accounts: B1' in page.locator('#reconContext').inner_text()
    jobs=_export_job_routes(page)
    with page.expect_download():page.locator('#reconExport').click()
    assert jobs['requests'][0]['selected_treasury_account_codes']==['B1']
    assert page.evaluate('()=>ledgerFixture.pending()')==['C1']


@pytest.mark.parametrize('wrong_scope',[True,False])
def test_summary_accepts_only_requested_accounts_and_preserves_later_pending_edits(ledger_cache_page,wrong_scope):
    page=ledger_cache_page
    page.evaluate('report=>ledgerFixture.load(report)',_selected_ui_report(['B1']))
    page.evaluate('accounts=>ledgerFixture.accounts(accounts,["B2"])',_account_choices())
    page.evaluate('''()=>{window.originalFetch=window.fetch;window.fetch=(url,options)=>{
        if(String(url).includes('/cashflows/preview?')) return new Promise(resolve=>{window.summaryRequest={url:String(url),resolve};});
        return window.originalFetch(url,options);};window.summaryDone=false;ledgerFixture.generate(false).then(()=>window.summaryDone=true);}''')
    page.wait_for_function('()=>!!window.summaryRequest')
    assert parse_qs(urlparse(page.evaluate('()=>window.summaryRequest.url')).query)['selected_treasury_account_codes']==['B2']
    page.evaluate('()=>ledgerFixture.setPending(["C1"])')
    result=_selected_ui_report(['B1'] if wrong_scope else ['B2'])
    page.evaluate('result=>window.summaryRequest.resolve({ok:true,json:async()=>result})',result)
    page.wait_for_function('()=>window.summaryDone')
    if wrong_scope:
        assert 'no longer matches' in page.locator('#cashflowsToast').inner_text()
        assert page.evaluate('()=>ledgerFixture.stripped()') is None
    else:
        assert page.evaluate('()=>ledgerFixture.stripped().account_selection.account_codes')==['B2']
        assert page.evaluate('()=>ledgerFixture.pending()')==['C1']
        assert page.locator('#cashflowsAccountButton').get_attribute('class')=='is-pending'


def test_selected_transfer_context_and_allocation_reviews_remain_distinct(ledger_cache_page):
    page=ledger_cache_page;report=_selected_ui_report(['C1'])
    preview=dict(report['preview'],transfer_reconciliation=dict(status='Matched',selected_inflow=0,selected_outflow=20),
        transfer_details=[dict(voucher_id='T1',amount=100,selection_inflow=0,selection_outflow=20,selection_net_movement=-20)],
        selection_review_details=[dict(voucher_id='AMB',period_key='2026-01',reason='Competing funding accounts',receipts=0,payments=40)],
        categories_needing_review=[],selection_review_count=1)
    page.evaluate('report=>ledgerFixture.load(report)',report)
    page.evaluate('preview=>ledgerFixture.child("transfers",preview)',preview)
    assert 'voucher amount (context)' in page.locator('#cashflowsTransfersTable').inner_text().lower()
    assert 'selected transfer payments' in page.locator('#cashflowsTransfersTable').inner_text().lower()
    page.evaluate('preview=>ledgerFixture.child("review",preview)',preview)
    assert 'Competing funding accounts' in page.locator('#cashflowsReviewTable').inner_text()
    assert 'selected payments under review' in page.locator('#cashflowsReviewTable').inner_text().lower()


def test_ledger_cache_isolates_selected_accounts_even_with_same_source_and_configuration(ledger_cache_page):
    page=ledger_cache_page
    first=_selected_ui_report(['B1'])
    second=_selected_ui_report(['B2'])
    for row in second['preview']['ledger_cache']['rows']:row['amount']+=100
    page.evaluate('report=>ledgerFixture.load(report)',first)
    page.evaluate('report=>ledgerFixture.load(report)',second)
    second_result=page.evaluate('()=>ledgerFixture.ledger()')
    page.evaluate('report=>ledgerFixture.load(report)',_selected_ui_report(['B1'],complete=False))
    first_result=page.evaluate('()=>ledgerFixture.ledger()')
    assert second_result['rows'][0]['amount']==100.25
    assert first_result['rows'][0]['amount']==.25
    assert first_result['ledger_context']['account_selection']['account_codes']==['B1']


def test_account_choice_failure_retries_without_silently_loading_all(ledger_cache_page):
    page=ledger_cache_page;state=dict(fail=True,calls=0)
    page.evaluate('report=>ledgerFixture.load(report)',_ledger_report())
    def options(route):
        state['calls']+=1
        if state['fail']:route.fulfill(status=500,json=dict(detail='Unavailable'))
        else:route.fulfill(json=dict(client_id=7,configuration_key='config-1',accounts=_account_choices()))
    page.route('**/api/reconciliation/cashflows/account-options?*',options)
    page.locator('#cashflowsAccountButton').click()
    page.wait_for_selector('#cashflowsAccountRetry')
    assert 'could not be loaded' in page.locator('#cashflowsAccountStatus').inner_text()
    assert page.evaluate('()=>ledgerFixture.stripped().account_selection || null') is None
    state['fail']=False
    page.locator('#cashflowsAccountRetry').click()
    page.wait_for_selector('[data-cashflow-account="B1"]')
    assert state['calls']==2
    assert page.locator('#cashflowsAccountStatus').inner_text()==''


def test_same_scope_balance_refresh_does_not_discard_pending_child_details(ledger_cache_page):
    page=ledger_cache_page;report=_selected_ui_report(['B1'])
    page.evaluate('report=>ledgerFixture.load(report)',report)
    page.evaluate('''()=>{window.originalFetch=window.fetch;window.fetch=(url,options)=>{
        if(String(url).includes('/cashflows/preview?'))return new Promise(resolve=>window.finishChild=resolve);
        return window.originalFetch(url,options);};window.childDone=false;ledgerFixture.children().then(value=>{window.childValue=value;window.childDone=true;});}''')
    page.wait_for_function('()=>!!window.finishChild')
    page.evaluate('()=>ledgerFixture.renewLoadedReference()')
    page.evaluate('report=>window.finishChild({ok:true,json:async()=>report})',report)
    page.wait_for_function('()=>window.childDone')
    assert page.evaluate('()=>window.childValue.account_selection.account_codes')==['B1']
    assert page.locator('#cashflowsLoadTransfersButton').is_enabled()


def test_loaded_details_never_mix_a_changed_client_with_the_previous_report(ledger_cache_page):
    page=ledger_cache_page;requests=[]
    page.evaluate('report=>ledgerFixture.load(report)',_selected_ui_report(['B1']))
    page.route('**/api/**',lambda route:(requests.append(route.request.url),route.abort()))
    page.evaluate('''async()=>{ledgerFixture.mutateScope({client_id:8});await ledgerFixture.children();
        await ledgerFixture.classified(true);await ledgerFixture.download('summary');await ledgerFixture.reconcile();}''')
    assert requests==[]
    assert 'Client changed' in page.locator('#cashflowsToast').inner_text()


def test_selected_classified_vouchers_label_other_treasury_accounts_as_context(ledger_cache_page):
    page=ledger_cache_page;report=_selected_ui_report(['B1'])
    page.evaluate('report=>ledgerFixture.load(report)',report)
    page.route('**/api/data/cashflows-classification-preview?*',lambda route:route.fulfill(json=dict(
        account_selection=report['account_selection'],template_columns=['Debit Amount'],total_rows=1,
        rows=[dict(row_data={'Debit Amount':5},classification=dict(type='Bank',account_code='B2',
            account_scope='Other Cash/Bank account — voucher context'))])))
    page.evaluate('()=>ledgerFixture.classified(true)')
    assert 'Account scope' in page.locator('#cashflowsTransactionsTable').text_content()
    assert 'Other Cash/Bank account' in page.locator('#cashflowsTransactionsTable').text_content()


def test_reconciliation_cache_keys_and_validation_isolate_each_selected_scope(ledger_cache_page,reconciliation_payload):
    import copy
    page=ledger_cache_page;report,parent,_,_=reconciliation_payload()
    reads=[];validations=[]
    def response(route):
        query=parse_qs(urlparse(route.request.url).query)
        codes=query.get('selected_treasury_account_codes')
        selection=dict(version=1,mode='selected',account_codes=codes,configured_account_codes=['B1','C1'])
        value=dict(parent,account_selection=selection,cache_schema=1,cache_identity='a'*64,
            template_id=report['configuration']['source_template_id'])
        (validations if 'cache-validation' in route.request.url else reads).append(codes)
        route.fulfill(json=value)
    page.route('**/api/reconciliation/cashflows/reconciliation?*',response)
    page.route('**/api/reconciliation/cashflows/reconciliation/cache-validation?*',response)
    for codes in [['B1'],['C1'],['B1']]:
        scoped=copy.deepcopy(report)
        scoped['account_selection']=dict(version=1,mode='selected',account_codes=codes,configured_account_codes=['B1','C1'])
        scoped['preview']['account_selection']=scoped['account_selection']
        page.evaluate('report=>ledgerFixture.load(report)',scoped)
        page.evaluate('()=>ledgerFixture.reconcile()')
        page.locator('#reconClose').click()
    assert reads==[['B1'],['C1']]
    assert validations==[['B1']]


@pytest.fixture
def reconciliation_payload(horizontal_reconciliation_case):
    """Actual accounting bridge/source shapes, with no database or server dependency."""
    from services.reconciliation_balance_source import build_cashflow_reconciliation, build_cashflow_differences, cashflow_adjustment_sections, build_cashflow_voucher_completeness
    from services.ledger_report_source import cashflow_reconciliation_support_page
    case=horizontal_reconciliation_case
    def build(view='combined'):
        summary=_horizontal_summary(case)
        parent=build_cashflow_reconciliation(summary,case['source'],case['accounts'],view)
        entries=parent.pop('_support_entries')
        counts=build_cashflow_voucher_completeness(summary,case['source'],case['accounts'],parent,entries)
        for proof,part in parent['views'].items():
            part['voucher_completeness']=counts[proof]
            part['adjustment_sections']=cashflow_adjustment_sections([e for values in entries.values() for e in values if e['treasury_view']==proof])
        parent.update(client_id=7,period_start='2026-01-01',period_end='2026-01-31',
            configuration_key='config-1',report_revision=12,evidence_fingerprint='current-proof', cache_schema=1,cache_identity='a'*64,template_id=9)
        report=_ledger_report(view=view)
        report['period_end']='2026-01-31'
        report['ledger_context']['period_end']='2026-01-31'
        report['preview']['periods'][0]['end']='2026-01-31'
        def support(key,period=''):
            selected=[r for r in entries[key] if not period or r['period_key']==period]
            return cashflow_reconciliation_support_page(case['source'],selected,list(case['accounts']),periods=summary['periods'],limit=25,page=1)
        posting=build_cashflow_differences(case['source'],case['accounts'],'2026-01-01','2026-01-31')
        return report,parent,support,posting
    return build


def _adjustment_button(page):
    page.locator('.recon-adjustment-group').evaluate_all('(groups)=>groups.forEach(g=>g.open=true)')
    return page.locator('[data-recon-support]')


def _flat_payload(parent, support, query):
    key=query['support_key'][0];side=query['side'][0]
    book=query.get('source_book',['ledger'])[0];search=query.get('search',[''])[0]
    proof=next(view for view,part in parent['views'].items() if any(r['key']==key for r in part['adjustments']))
    adjustment=next(r for r in parent['views'][proof]['adjustments'] if r['key']==key)
    groups=support(key)['rows']
    source='ledger_lines' if book=='ledger' else 'voucher_lines'
    rows=[dict(row,row_data=dict(row)) for g in groups if g[side]!=0 for row in g[source]]
    if search: rows=[r for r in rows if search.casefold() in json.dumps(r).casefold()]
    limit=50;count=len(rows);pages=max(1,(count+limit-1)//limit);number=min(int(query.get('page',['1'])[0]),pages)
    context={k:v for k,v in parent.items() if k not in ('views','periods','review_controls','diagnostics')}
    return context|dict(support_key=key,side=side,proof=proof,adjustment_total=adjustment['total'][side],
        transactions=dict(rows=rows[(number-1)*limit:number*limit],columns=list(dict.fromkeys(k for r in rows for k in r['row_data'])),
            total_rows=count,page=number,page_count=pages,limit=limit,source_book=book,search=search))


def _route_flat(page,parent,support):
    page.route('**/api/reconciliation/cashflows/reconciliation/adjustment-transactions?*',
        lambda route:route.fulfill(json=_flat_payload(parent,support,parse_qs(urlparse(route.request.url).query,keep_blank_values=True))))


def _open_reconciliation(page, report, parent):
    page.route('**/api/reconciliation/cashflows/reconciliation?*',lambda route:route.fulfill(json=parent))
    page.evaluate('report => ledgerFixture.load(report)',report)
    page.locator('#cashflowsReconciliationButton').click()
    page.wait_for_selector('#reconWorking table')


@pytest.mark.parametrize('width', [1366, 390])
def test_voucher_completeness_reuses_loaded_counts_and_existing_detail_popup(ledger_cache_page, reconciliation_payload, width):
    page=ledger_cache_page
    page.set_viewport_size(dict(width=width,height=1000))
    report,parent,support,_=reconciliation_payload()
    _route_flat(page,parent,support)
    _open_reconciliation(page,report,parent)
    reads=[]
    page.on('request',lambda request:reads.append(request.url))
    page.locator('#reconCompletenessOpen').click()
    popup=page.locator('#reconCompletenessDialog')
    assert popup.is_visible()
    rows=popup.locator('table[aria-label="Voucher completeness comparison"] > tbody > tr')
    assert [rows.nth(i).locator('td').nth(1).inner_text() for i in range(4)]==['4','4','3','3']
    assert 'voucher IDs agree' in popup.inner_text()
    assert 'Not independently certified' in popup.inner_text()
    assert reads==[]  # Opening and expanding the summary need no request.
    popup.locator('#reconCompletenessAdjustments').click()
    assert popup.locator('#reconCompletenessAdjustmentRows').is_visible()
    button=popup.locator('[data-recon-support]').first
    key=button.get_attribute('data-recon-support')
    button.click()
    page.wait_for_selector('#reconAdjustmentDialog[open] .recon-flat-table')
    assert page.locator('#reconAdjustmentDialog').is_visible() and popup.is_visible()
    assert any(key in url for url in reads if '/adjustment-' in url), reads
    page.locator('#reconAdjustmentClose').click()
    assert popup.is_visible() and page.locator('#reconAdjustmentDialog').is_hidden()
    assert page.evaluate('document.activeElement.closest("#reconCompletenessDialog") !== null')
    page.keyboard.press('Escape')
    assert popup.is_hidden() and page.locator('#cashflowReconciliation').is_visible()
    assert page.locator('#reconCompletenessOpen').evaluate('(e)=>e===document.activeElement')
    assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')


def test_voucher_completeness_keeps_unavailable_counts_and_closes_on_reload(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    report,parent,_,_=reconciliation_payload()
    part=parent['views']['combined']['voucher_completeness']
    part['ledger']['vouchers']=part['ledger']['blank_id_rows']=part['source_ids_match']=None
    _open_reconciliation(page,report,parent)
    page.locator('#reconCompletenessOpen').click()
    assert 'Unavailable' in page.locator('#reconCompletenessContent table').first.inner_text()
    page.locator('#reconCompletenessClose').click()
    page.locator('#reconReload').click()
    page.wait_for_selector('#reconWorking table')
    assert page.locator('#reconCompletenessDialog').is_hidden()


def test_voucher_completeness_switches_split_proof_from_the_loaded_snapshot(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    report,parent,_,_=reconciliation_payload('split')
    _open_reconciliation(page,report,parent)
    page.locator('#reconView').select_option('cash')
    page.locator('#reconCompletenessOpen').click()
    popup=page.locator('#reconCompletenessDialog')
    rows=popup.locator('table[aria-label="Voucher completeness comparison"] > tbody > tr')
    assert [rows.nth(i).locator('td').nth(1).inner_text() for i in range(3)]==['1','1','0']
    assert 'cash' in page.locator('#reconCompletenessContext').inner_text()


def test_stale_adjustment_evidence_discards_the_completeness_snapshot(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    report,parent,_,_=reconciliation_payload()
    page.route('**/api/reconciliation/cashflows/reconciliation/adjustment-*',
        lambda route:route.fulfill(status=409,json={'detail':'Source changed'}))
    _open_reconciliation(page,report,parent)
    page.evaluate('''()=>{
        document.getElementById('reconCompletenessOpen').click();
        document.getElementById('reconCompletenessAdjustments').click();
        document.querySelector('#reconCompletenessDialog [data-recon-support]').focus();
    }''')
    page.wait_for_function('document.getElementById("reconCompletenessOpen").disabled && document.getElementById("reconCompletenessDialog").hidden')
    assert page.locator('#reconCompletenessDialog').is_hidden()
    assert page.locator('#reconCompletenessOpen').is_disabled()


def _linked_period_report(end):
    """Small real-engine case: a January loan repayment becomes a half-year receipt category."""
    import pandas as pd
    from services.ledger_group_summary import build_cashflow_summary
    from services.reconciliation_balance_source import build_cashflow_reconciliation
    entries=[('RECEIPT','2026-01-05','B1',1000,0),('RECEIPT','2026-01-05','AR',0,1000),
        ('REPAY','2026-01-10','B1',0,200),('REPAY','2026-01-10','LOAN',200,0),
        ('DRAW','2026-02-05','B1',300,0),('DRAW','2026-02-05','LOAN',0,300),
        ('EXPENSE','2026-02-10','B1',0,50),('EXPENSE','2026-02-10','EXP',50,0)]
    rows=[dict(id=i,voucher_id=v,date=d,account_code=a,account_name=a,debit=dr,credit=cr)
          for i,(v,d,a,dr,cr) in enumerate(entries,1)]
    coa=pd.DataFrame([dict(account_code=code,account_title=code,account_type=kind,category=category)
        for code,kind,category in [('B1','Bank',''),('AR','Asset','Received from customers'),
            ('LOAN','Liability','Bank loan drawn (net)'),('EXP','Expense','General expenses')]])
    summary=build_cashflow_summary(pd.DataFrame(rows),coa,[],['B1'],'2026-01-01',end,
        group_by='month',receipt_presentation='net',include_reconciliation_evidence=True)
    parent=build_cashflow_reconciliation(summary,dict(uploads=[],ledger_rows=[],transaction_rows=rows),{'B1':'Bank'})
    parent.pop('_support_entries')
    parent.update(client_id=7,period_start='2026-01-01',period_end=end,
        configuration_key='config-1',report_revision=12,evidence_fingerprint='proof-'+end)
    report=_ledger_report()
    report['period_end']=report['ledger_context']['period_end']=end
    categories=summary['category_totals'].reset_index().to_dict('records')
    report['preview'].update(periods=summary['periods'],months=summary['months'],
        category_totals=categories,treasury_view_totals=categories)
    report['preview'].pop('ledger_cache')
    return report,parent


@pytest.mark.parametrize('width',[1280,390])
def test_reconciliation_period_lock_follows_loaded_report_and_range_totals(ledger_cache_page,width):
    page=ledger_cache_page;page.set_viewport_size(dict(width=width,height=820))
    january,jan_paper=_linked_period_report('2026-01-31')
    half_year,half_paper=_linked_period_report('2026-06-30')
    assert jan_paper['views']['combined']['category_sections']['Bank loan drawn (net)']=='payments'
    assert half_paper['views']['combined']['category_sections']['Bank loan drawn (net)']=='receipts'
    assert half_paper['views']['combined']['periods'][0]['receipts']['cashflow']==800
    assert half_paper['views']['combined']['overall']['receipts']['cashflow']==1100
    calls=[]
    def respond(route):
        query=parse_qs(urlparse(route.request.url).query);calls.append(query)
        assert 'period_key' not in query
        route.fulfill(json=jan_paper if query['period_end']==['2026-01-31'] else half_paper)
    page.route('**/api/reconciliation/cashflows/reconciliation?*',respond)
    page.evaluate('report=>ledgerFixture.load(report)',january)
    # Unapplied controls must not redefine the report that is being reconciled.
    page.evaluate("()=>{document.getElementById('cashflowsPeriodEnd').value='2026-06-30';}")
    page.locator('#cashflowsReconciliationButton').click();page.wait_for_selector('#reconWorking table')
    assert page.locator('#reconReportPeriod').inner_text()=='01 Jan 2026 – 31 Jan 2026'
    assert page.locator('#reconPeriod').count()==0
    assert page.locator('#reconWorking tr').filter(has_text='Receipts as per Cashflow report').first.inner_text().endswith('1,000.00')
    assert page.locator('#reconWorking tr').filter(has_text='Payments as per Cashflow report').first.inner_text().endswith('200.00')
    assert calls[-1]['period_end']==['2026-01-31']
    page.locator('#reconReload').click();page.wait_for_selector('#reconWorking table')
    assert calls[-1]['period_end']==['2026-01-31']
    page.locator('#reconClose').click()
    page.route('**/api/reconciliation/cashflows/preview?*',lambda route:route.fulfill(json=half_year))
    page.evaluate("()=>ledgerFixture.mutateScope({period_end:'2026-06-30'})")
    page.evaluate('()=>ledgerFixture.generate(true)')
    main_receipts=page.locator('.cashflows-summary-scroll-pane .cashflows-summary-total-row').first
    assert main_receipts.locator('.cashflows-summary-total-value').inner_text()=='1,100'
    page.locator('#cashflowsReconciliationButton').click();page.wait_for_selector('#reconWorking table')
    assert page.locator('#reconReportPeriod').inner_text()=='01 Jan 2026 – 30 Jun 2026'
    assert 'Linked to the loaded Cashflows Report' in page.locator('.recon-toolbar').inner_text()
    assert page.locator('#reconWorking tr').filter(has_text='Receipts as per Cashflow report').first.inner_text().endswith('1,100.00')
    assert page.locator('#reconWorking tr').filter(has_text='Payments as per Cashflow report').first.inner_text().endswith('50.00')
    page.locator('#reconReload').click();page.wait_for_selector('#reconWorking table')
    assert calls[-1]['period_end']==['2026-06-30']
    assert page.locator('#cashflowReconciliation').evaluate('(node)=>node.scrollWidth<=node.clientWidth+1')
    page.locator('#reconClose').click()
    assert page.locator('#reconReportPeriod').inner_text()==''
    page.locator('#cashflowsReconciliationButton').click();page.wait_for_selector('#reconWorking table')
    assert page.locator('#reconReportPeriod').inner_text()=='01 Jan 2026 – 30 Jun 2026'


@pytest.mark.parametrize('start,end,label',[('2026-01-04','2026-02-26','04 Jan 2026 – 26 Feb 2026'),
    ('2025-12-30','2026-01-03','30 Dec 2025 – 03 Jan 2026')])
def test_reconciliation_locked_label_preserves_exact_date_boundaries(ledger_cache_page,reconciliation_payload,start,end,label):
    report,parent,_,_=reconciliation_payload()
    report.update(period_start=start,period_end=end);parent.update(period_start=start,period_end=end)
    report['ledger_context'].update(period_start=start,period_end=end)
    _open_reconciliation(ledger_cache_page,report,parent)
    assert ledger_cache_page.locator('#reconReportPeriod').inner_text()==label
    assert ledger_cache_page.locator('#reconReportPeriod').get_attribute('aria-describedby')=='reconPeriodNote'


def test_horizontal_fullpage_preserves_report_and_uses_backend_amounts(ledger_cache_page,reconciliation_payload):
    _check_flat_popup(ledger_cache_page,reconciliation_payload,'close')


@pytest.mark.parametrize('view',['combined','bank','cash','split'])
@pytest.mark.parametrize('width',[1280,390])
def test_reconciliation_layout_views_and_keyboard(ledger_cache_page,reconciliation_payload,view,width):
    page=ledger_cache_page
    page.set_viewport_size({'width':width,'height':820})
    report,parent,_,_=reconciliation_payload(view)
    _open_reconciliation(page,report,parent)
    assert page.locator('#reconReportPeriod').is_visible()
    assert page.locator('#reconReportPeriod').inner_text()=='01 Jan 2026 – 31 Jan 2026'
    assert page.locator('#reconPeriod').count()==0
    assert page.locator('#reconViewLabel').is_visible()==(view=='split')
    if view=='split':
        for part in ('bank','cash','combined'):
            page.locator('#reconView').select_option(part)
            expected=parent['views'][part]['overall']['receipts']['cashflow']
            text=page.locator('#reconWorking .recon-section').first.locator('tr').filter(has_text='Receipts as per Cashflow report').inner_text()
            assert f'{expected:,.2f}' in text
    box=page.locator('#cashflowReconciliation').bounding_box()
    assert box['x']>=0 and box['x']+box['width']<=width
    assert page.locator('#cashflowReconciliation').evaluate('(d)=>d.scrollWidth<=d.clientWidth+1')
    page.locator('#reconWorkingTab').focus()
    page.keyboard.press('ArrowRight')
    assert page.locator('#reconDifferencesTab').get_attribute('aria-selected')=='true'
    page.keyboard.press('Home')
    assert page.locator('#reconWorkingTab').get_attribute('aria-selected')=='true'
    page.locator('#reconClose').focus();page.keyboard.press('Shift+Tab')
    assert page.evaluate("document.getElementById('cashflowReconciliation').contains(document.activeElement)")
    page.keyboard.press('Escape')


def test_reconciliation_unavailable_signed_values_and_safe_source_text(ledger_cache_page,reconciliation_payload):
    _check_flat_popup(ledger_cache_page,reconciliation_payload,'safe')


def test_reconciliation_difference_paging_and_stale_evidence(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    report,parent,_,posting=reconciliation_payload()
    calls=[];stale=[False]
    def respond(route):
        query=parse_qs(urlparse(route.request.url).query);calls.append(query)
        if 'posting_differences' not in query:return route.fulfill(json=parent)
        assert query['expected_fingerprint']==['current-proof']
        if stale[0]:return route.fulfill(status=409,json={'detail':'Evidence changed; reload the working paper.'})
        number=int(query['page'][0]);rows=posting['findings']
        route.fulfill(json=parent|{'posting_differences':posting|dict(rows=[rows[number-1]],page=number,page_count=2,total_rows=2,limit=1)})
    page.route('**/api/reconciliation/cashflows/reconciliation?*',respond)
    page.evaluate('report => ledgerFixture.load(report)',report)
    page.locator('#cashflowsReconciliationButton').click();page.wait_for_selector('#reconWorking table')
    page.locator('#reconDifferencesTab').click();page.wait_for_selector('#reconDifferenceRows summary')
    page.locator('#reconDifferenceRows [data-recon-page]').filter(has_text='Next').first.click()
    page.wait_for_function("document.getElementById('reconDifferenceRows').innerText.includes('Page 2 of 2')")
    assert calls[-1]['page']==['2']
    page.locator('#reconIncludeMatched').check();page.wait_for_function("document.getElementById('reconDifferenceRows').innerText.includes('Page 1 of 2')")
    assert calls[-1]['differences_only']==['false']
    stale[0]=True
    page.locator('#reconDifferenceRows [data-recon-page]').filter(has_text='Next').first.click()
    page.wait_for_selector('#reconStatus.is-error')
    assert page.locator('#reconWorking').inner_html()=='' and page.locator('#reconDifferenceRows').inner_html()==''
    assert page.locator('#reconDifferencesTab').is_disabled()
    page.locator('[data-recon-retry]').click();page.wait_for_selector('#reconWorking table')
    assert 'posting_differences' not in calls[-1]


def test_reconciliation_failed_request_retry_and_closed_response(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    report,parent,_,_=reconciliation_payload()
    count=[0]
    def respond(route):
        count[0]+=1
        if count[0]==1:return route.fulfill(status=503,json={'detail':'Source temporarily unavailable'})
        route.fulfill(json=parent)
    page.route('**/api/reconciliation/cashflows/reconciliation?*',respond)
    page.evaluate('report => ledgerFixture.load(report)',report)
    page.locator('#cashflowsReconciliationButton').click();page.wait_for_selector('#reconStatus.is-error')
    assert 'Source temporarily unavailable' in page.locator('#reconStatus').inner_text()
    page.locator('[data-recon-retry]').click();page.wait_for_selector('#reconWorking table')
    page.locator('#reconClose').click()
    # Resolve a delayed fetch after returning: it must not repaint or reopen reconciliation.
    page.evaluate('''() => { window.originalFetch=window.fetch; window.fetch=()=>new Promise(resolve=>window.finishReconciliation=resolve); ledgerFixture.reconcile(); }''')
    assert page.locator('#reconProcessing').inner_text().startswith('Preparing reconciliation')
    page.keyboard.press('Escape')
    page.evaluate('data=>window.finishReconciliation({ok:true,json:async()=>data})',parent)
    page.wait_for_timeout(30)
    assert page.locator('#cashflowReconciliation').is_hidden()
    assert page.locator('#reconWorking').inner_html()==''
    page.evaluate('()=>{window.fetch=window.originalFetch;}')


def test_reconciliation_rejects_mismatched_report_and_child_fingerprints(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    report,parent,support,_=reconciliation_payload()
    mode=['configuration']
    def respond(route):
        query=parse_qs(urlparse(route.request.url).query)
        payload=dict(parent)
        if mode[0]=='configuration':payload['configuration_key']='changed'
        elif 'support_key' in query:
            payload.update(evidence_fingerprint='new-source',support=support(query['support_key'][0]))
        route.fulfill(json=payload)
    page.route('**/api/reconciliation/cashflows/reconciliation?*',respond)
    page.evaluate('report=>ledgerFixture.load(report)',report)
    page.evaluate("()=>{document.getElementById('cashflowsPeriodEnd').value='2026-06-30';ledgerFixture.mutateScope({period_end:'2026-06-30'});}")
    page.locator('#cashflowsReconciliationButton').click();page.wait_for_selector('#reconStatus.is-error')
    assert 'loaded report and current source/configuration differ' in page.locator('#reconStatus').inner_text()
    assert page.locator('#reconWorking').inner_html()==''
    refreshed=report|{'cashflow_configuration_key':'changed'}
    preview_calls=[]
    def refresh_report(route):
        preview_calls.append(parse_qs(urlparse(route.request.url).query));route.fulfill(json=refreshed)
    page.route('**/api/reconciliation/cashflows/preview?*',refresh_report)
    page.locator('#reconReload').click();page.wait_for_selector('#reconWorking table')
    assert preview_calls[-1]['period_end']==['2026-01-31']
    assert page.locator('#reconReportPeriod').inner_text()=='01 Jan 2026 – 31 Jan 2026'
    page.locator('#reconClose').click();mode[0]='child'
    parent['configuration_key']='changed'
    page.locator('#cashflowsReconciliationButton').click();page.wait_for_selector('#reconWorking table')
    page.route('**/api/reconciliation/cashflows/reconciliation/adjustment-transactions?*',lambda route:route.fulfill(status=409,json={'detail':'Source changed'}))
    _adjustment_button(page).first.click();page.wait_for_selector('#reconSupport [role=alert]')
    assert page.locator('#reconWorking').inner_html()==''


@pytest.mark.parametrize('feature',[False,True])
def test_reconciliation_navigation_deep_link_loads_only_when_requested(ledger_cache_page,reconciliation_payload,feature):
    page=ledger_cache_page.context.browser.new_page()
    report,parent,_,_=reconciliation_payload()
    report['period_end']=parent['period_end']='2026-06-30'
    html=(ROOT/'static/reconciliation.html').read_text(encoding='utf-8')
    html=re.sub(r'<script\b[^>]*>.*?</script>','',html,flags=re.S)
    html=re.sub(r'<link\b[^>]*>','',html)
    calls=[]
    page.route('**/*',lambda route:route.abort())
    page.route('**/static/reconciliation.html?*',lambda route:route.fulfill(body=html,content_type='text/html'))
    def respond(route):
        calls.append(route.request.url)
        route.fulfill(json=parent if '/reconciliation?' in route.request.url else report)
    page.route('**/api/reconciliation/cashflows/*',respond)
    suffix='&feature=reconciliation' if feature else ''
    page.goto('https://cashflows.test/static/reconciliation.html?report=cashflows'+suffix)
    script=(ROOT/'static/reconciliation.js').read_text(encoding='utf-8')
    script=script.replace("document.addEventListener('DOMContentLoaded', initialise);",'window.startReport=initialise;')
    page.add_script_tag(content=(ROOT/'static/loading_ui.js').read_text(encoding='utf-8'))
    page.add_script_tag(content=script)
    page.evaluate('''async()=>{window.getSelectedClientId=()=>7;await window.startReport();}''')
    assert page.locator('#cashflowsView').is_visible()!=feature
    assert page.locator('#cashflowReconciliation').is_visible()==feature
    assert len(calls)==(2 if feature else 0)
    if feature:
        assert '/preview?' in calls[0] and '/reconciliation?' in calls[1]
        assert 'period_end=2026-06-30' in calls[1]
        page.locator('#reconClose').click()
        assert page.locator('#cashflowsView').is_visible()
        assert 'feature=' not in page.url
    manifest=(ROOT/'static/client_context.js').read_text(encoding='utf-8')
    assert "label: 'Ledger / Cashflow reconciliation'" in manifest
    assert "url: '/static/reconciliation.html?report=cashflows&feature=reconciliation', state: 'ready'" in manifest
    page.close()


def _export_job_routes(page, *, failure=None):
    state = dict(requests=[], polls=0, cancelled=0, downloads=0)
    identity='e'*32
    def respond(route):
        path=urlparse(route.request.url).path
        if route.request.method=='POST':
            state['requests'].append(route.request.post_data_json)
            return route.fulfill(status=202,json=dict(id=identity,state='queued',stage='Reading and validating evidence'))
        if route.request.method=='DELETE':
            state['cancelled']+=1
            return route.fulfill(json=dict(id=identity,state='cancelled'))
        if path.endswith('/download'):
            state['downloads']+=1
            return route.fulfill(body=b'fixture-xlsx-download',content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                headers={'Content-Disposition':'attachment; filename="Reconciliation.xlsx"',
                         'Access-Control-Expose-Headers':'Content-Disposition'})
        state['polls']+=1
        if failure:
            return route.fulfill(json=dict(id=identity,state='failed',error='Export evidence changed' if failure==409 else 'Export unavailable',error_status=failure))
        if state['polls']==1:
            return route.fulfill(json=dict(id=identity,state='running',stage='Writing Voucher Evidence',current=250,total=500))
        return route.fulfill(json=dict(id=identity,state='ready',filename='Reconciliation.xlsx'))
    page.route('**/api/reconciliation/cashflows/reconciliation/exports**',respond)
    return state


@pytest.mark.parametrize('view',['combined','bank','cash','split'])
def test_reconciliation_excel_jobs_use_locked_scope_progress_and_preserve_report(ledger_cache_page,reconciliation_payload,view,tmp_path):
    page=ledger_cache_page
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    _cache_reconciliation_routes(page,reconciliation_payload,view)
    page.evaluate("()=>{document.getElementById('cashflowsPeriodStart').value='2026-04-01';document.getElementById('cashflowsPeriodEnd').value='2026-06-30';}")
    if view=='split':page.locator('#reconView').select_option('bank')
    state=_export_job_routes(page)
    working=page.locator('#reconWorking').inner_html()
    with page.expect_download() as download:
        page.locator('#reconExport').click()
        assert page.locator('#reconExport').is_disabled() and page.locator('#reconReload').is_disabled()
        assert page.locator('[data-recon-cancel-export]').is_visible()
        page.wait_for_function("()=>document.getElementById('reconExportProcessing').innerText.includes('250 of 500')")
        assert 'Writing Voucher Evidence' in page.locator('#reconExportProcessing').inner_text()
        assert page.locator('#reconWorking').inner_html()==working
    item=download.value;item.save_as(tmp_path/'export.xlsx')
    assert item.suggested_filename=='Reconciliation.xlsx'
    assert (tmp_path/'export.xlsx').read_bytes()==b'fixture-xlsx-download'
    page.wait_for_function("()=>!document.getElementById('reconExport').disabled")
    assert state['requests']==[dict(client_id=7,period_start='2026-01-01',period_end='2026-01-31',
        group_by='month',treasury_view=view,expected_fingerprint='current-proof',expected_identity='a'*64)]
    assert state['downloads']==1 and state['polls']==2
    assert page.locator('#reconExportProcessing').is_hidden()
    assert page.locator('#reconWorking').inner_html()==working
    page.locator('#reconClose').click()
    assert page.locator('#cashflowsView').is_visible() and not errors


@pytest.mark.parametrize('status',[401,403,409,500])
def test_reconciliation_excel_job_failures_release_processing_and_stale_papers(ledger_cache_page,reconciliation_payload,status):
    page=ledger_cache_page
    _cache_reconciliation_routes(page,reconciliation_payload)
    state=_export_job_routes(page,failure=status)
    downloads=[];page.on('download',lambda item:downloads.append(item))
    page.locator('#reconExport').click();page.wait_for_selector('#reconStatus.is-error')
    assert 'Export ' in page.locator('#reconStatus').inner_text()
    assert page.locator('#reconExportProcessing').is_hidden()
    assert page.locator('#reconReload').is_enabled()
    if status in (401,403,409):
        assert page.locator('#reconExport').is_disabled()
        assert page.locator('#reconWorking').inner_html()==''
        assert page.evaluate('()=>ledgerFixture.reconciliationCache()')==[]
        page.locator('[data-recon-retry]').click();page.wait_for_selector('#reconWorking table')
    else:
        assert page.locator('#reconExport').is_enabled()
        assert page.locator('#reconWorking > .recon-section').count()==3
    assert state['downloads']==0 and not downloads


@pytest.mark.parametrize('action',['close','tab','cancel','timeout','late-ticket'])
def test_reconciliation_excel_jobs_cancel_and_reject_obsolete_results(ledger_cache_page,reconciliation_payload,action):
    page=ledger_cache_page
    _cache_reconciliation_routes(page,reconciliation_payload,'split')
    state=_export_job_routes(page)
    downloads=[];page.on('download',lambda item:downloads.append(item))
    if action in ('timeout','late-ticket'):
        page.evaluate("""()=>{window.exportOriginal=window.fetch;window.fetch=(url,options)=>{
            if(options&&options.method==='POST'&&String(url).includes('/exports')) return new Promise((resolve,reject)=>{
                window.finishExportTicket=resolve;options.signal.addEventListener('abort',()=>reject(new DOMException('Timed out','AbortError')));});
            return window.exportOriginal(url,options);};}""")
    if action=='timeout':
        page.evaluate("""()=>{const timer=window.setTimeout;window.setTimeout=(fn,delay,...args)=>timer(fn,delay===30000?20:delay,...args);}""")
    page.locator('#reconExport').click()
    if action=='timeout':
        page.wait_for_selector('#reconStatus.is-error')
        assert 'connection timed out' in page.locator('#reconStatus').inner_text()
        assert page.locator('#reconExport').is_enabled()
    elif action in ('close','late-ticket'):
        page.locator('#reconClose').click()
        if action=='late-ticket':
            page.evaluate("()=>window.finishExportTicket({ok:true,json:async()=>({id:'e'.repeat(32),state:'queued'})})")
        page.wait_for_function('()=>true')
        assert page.locator('#cashflowReconciliation').is_hidden()
    elif action=='tab':
        page.locator('#reconDifferencesTab').click();page.wait_for_selector('#reconDifferenceRows summary')
    else:
        page.locator('[data-recon-cancel-export]').click()
        assert page.locator('#reconExport').is_enabled()
    if action!='timeout':
        page.wait_for_function('()=>true');page.wait_for_timeout(50)
        assert state['cancelled']==1
    assert not downloads and state['downloads']==0
    assert page.locator('#reconExportProcessing').is_hidden()


def _cache_reconciliation_routes(page,reconciliation_payload,view='combined'):
    report,parent,support,posting=reconciliation_payload(view)
    parent.update(cache_schema=1,cache_identity='a'*64,template_id=report['configuration']['source_template_id'])
    state=dict(report=report,parent=parent,validation_status=200,validation=None,support=support)
    calls=[]
    def respond(route):
        query=parse_qs(urlparse(route.request.url).query,keep_blank_values=True)
        validation='/cache-validation?' in route.request.url
        calls.append(('validation' if validation else 'full',query))
        if validation:
            value=state['validation']
            if value is None:
                value={key:state['parent'][key] for key in ('cache_schema','cache_identity','template_id','client_id',
                    'period_start','period_end','group_by','treasury_view','configuration_key','report_revision')}
            return route.fulfill(status=state['validation_status'],json=value)
        payload=dict(state['parent'])
        if '/adjustment-transactions?' in route.request.url:
            return route.fulfill(json=_flat_payload(state['parent'],state['support'],query))
        if 'posting_differences' in query:
            number=int(query['page'][0]);rows=posting['findings']
            payload['posting_differences']=posting|dict(rows=[rows[(number-1)%len(rows)]],
                page=number,page_count=2,total_rows=2,limit=25)
        return route.fulfill(json=payload)
    page.route('**/api/reconciliation/cashflows/reconciliation?*',respond)
    page.route('**/api/reconciliation/cashflows/reconciliation/cache-validation?*',respond)
    page.route('**/api/reconciliation/cashflows/reconciliation/adjustment-transactions?*',respond)
    page.evaluate('report=>ledgerFixture.load(report)',report)
    page.locator('#cashflowsReconciliationButton').click();page.wait_for_selector('#reconWorking table')
    return state,calls


@pytest.mark.parametrize('view',['combined','bank','cash','split'])
def test_reconciliation_cache_reuses_validated_parent_and_compact_children(ledger_cache_page,reconciliation_payload,view):
    _check_flat_popup(ledger_cache_page,reconciliation_payload,'cache',view)


@pytest.mark.parametrize('failure',['changed','unavailable','malformed','unauthorized','forbidden'])
def test_reconciliation_cache_never_serves_uncertain_or_unauthorized_evidence(ledger_cache_page,reconciliation_payload,failure):
    page=ledger_cache_page
    state,calls=_cache_reconciliation_routes(page,reconciliation_payload)
    page.locator('#reconClose').click()
    if failure=='changed':state['parent']['cache_identity']='b'*64
    elif failure=='unavailable':state.update(validation_status=503,validation={'detail':'Unavailable'})
    elif failure=='malformed':state['validation']={}
    else:state.update(validation_status=401 if failure=='unauthorized' else 403,validation={'detail':'Access denied'})
    page.locator('#cashflowsReconciliationButton').click()
    if failure in ('unauthorized','forbidden'):
        page.wait_for_selector('#reconStatus.is-error')
        assert 'Access denied' in page.locator('#reconStatus').inner_text()
        assert page.locator('#reconWorking').inner_html()==''
        assert page.evaluate('()=>ledgerFixture.reconciliationCache()')==[]
        assert [kind for kind,_ in calls]==['full','validation']
    else:
        page.wait_for_selector('#reconWorking table')
        assert [kind for kind,_ in calls]==['full','validation','full']
        assert page.evaluate('()=>ledgerFixture.reconciliationState().result.cache_identity')==state['parent']['cache_identity']


def test_reconciliation_cache_ledger_only_change_rejects_child_even_with_same_voucher_fingerprint(ledger_cache_page,reconciliation_payload):
    _check_flat_popup(ledger_cache_page,reconciliation_payload,'changed')


@pytest.mark.parametrize('change',[{'client_id':8},{'template_id':10},{'group_by':'quarter'},
    {'treasury_view':'bank'},{'period_start':'2026-01-02'},{'period_end':'2026-02-28'},
    {'configuration_key':'config-2'},{'report_revision':13}])
def test_reconciliation_cache_isolates_complete_context(ledger_cache_page,reconciliation_payload,change):
    page=ledger_cache_page
    state,calls=_cache_reconciliation_routes(page,reconciliation_payload)
    page.locator('#reconClose').click()
    report=state['report'];parent=state['parent'];parent.update(change)
    for key,value in change.items():
        if key=='template_id':report['configuration']['source_template_id']=value
        elif key=='configuration_key':report['cashflow_configuration_key']=value
        elif key=='report_revision':report['cashflow_report_revision']=value
        else:report[key]=value
    page.evaluate('report=>ledgerFixture.load(report)',report)
    page.locator('#cashflowsReconciliationButton').click();page.wait_for_selector('#reconWorking table')
    assert [kind for kind,_ in calls]==['full','full']


def test_reconciliation_cache_lru_limits_combined_serialized_bytes_and_entries(ledger_cache_page):
    result=ledger_cache_page.evaluate('''()=>{
        const remember=(key,text)=>ledgerFixture.rememberReconciliation(key,'context',
            {cache_schema:1,cache_identity:'a'.repeat(64),text:text});
        for(let i=0;i<20;i++)remember(String(i),'small');
        ledgerFixture.takeReconciliation('0');remember('20','small');
        const keys=ledgerFixture.reconciliationCache().map(([key])=>key);
        remember('bytes-one','账'.repeat(380000));remember('bytes-two','账'.repeat(380000));
        const bytes=ledgerFixture.reconciliationCache().map(([key,value])=>({key,bytes:value.bytes}));
        remember('oversized','账'.repeat(800000));
        return {keys,bytes,final:ledgerFixture.reconciliationCache().map(([key,value])=>({key,bytes:value.bytes}))};
    }''')
    assert len(result['keys'])==20 and '0' in result['keys'] and '1' not in result['keys']
    assert sum(entry['bytes'] for entry in result['final'])<=2*1024*1024
    assert [entry['key'] for entry in result['bytes']]==['bytes-two']
    assert result['final']==result['bytes']


def test_reconciliation_oversized_support_displays_complete_voucher_without_retaining_it(ledger_cache_page,reconciliation_payload):
    _check_flat_popup(ledger_cache_page,reconciliation_payload,'oversized')


def test_reconciliation_cache_coalesces_pending_reads_and_rejects_late_closed_validation(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    state,calls=_cache_reconciliation_routes(page,reconciliation_payload)
    _hold_reconciliation_requests(page)
    page.locator('#reconDifferencesTab').click()
    page.locator('#reconDifferencesTab').click()
    assert page.evaluate('()=>pendingReconciliations.length')==1
    # Changing the page supersedes the shared request; even a fetch ignoring abort is harmless.
    page.evaluate("()=>{ledgerFixture.reconciliationLoad({posting_differences:'true',comparison_view:'combined',differences_only:'true',page:2,limit:25});}")
    assert page.evaluate('()=>pendingReconciliations.length')==2
    assert page.evaluate('()=>pendingReconciliations[0].signal.aborted')
    page.locator('#reconClose').click()
    _finish_reconciliation_request(page,state['parent'])
    _finish_reconciliation_request(page,state['parent'],1)
    page.wait_for_timeout(30)
    assert len(page.evaluate('()=>ledgerFixture.reconciliationCache()'))==1
    page.locator('#cashflowsReconciliationButton').click()
    assert '/cache-validation?' in page.evaluate('()=>pendingReconciliations[2].url')
    assert page.locator('#reconProcessing .ui-spinner').is_visible()
    page.locator('#reconClose').click()
    _finish_reconciliation_request(page,state['parent'],2)
    page.wait_for_timeout(30)
    assert page.locator('#reconWorking').inner_html()==''
    assert page.evaluate('()=>ledgerFixture.reconciliationState()')==dict(scope=None,result=None,pending=False,loading=False,opener=False)


def test_reconciliation_cache_invalidation_during_pending_read_cannot_restore_old_entries(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    state,_=_cache_reconciliation_routes(page,reconciliation_payload)
    page.locator('#reconClose').click();_hold_reconciliation_requests(page)
    page.locator('#cashflowsReconciliationButton').click()
    assert '/cache-validation?' in page.evaluate('()=>pendingReconciliations[0].url')
    page.evaluate('()=>ledgerFixture.clearReconciliationCache()')
    _finish_reconciliation_request(page,state['parent'])
    page.wait_for_function('pendingReconciliations.length===2')
    assert '/reconciliation?' in page.evaluate('()=>pendingReconciliations[1].url')
    page.evaluate('()=>ledgerFixture.clearReconciliationCache()')
    _finish_reconciliation_request(page,state['parent'],1)
    page.wait_for_selector('#reconWorking table')
    assert page.evaluate('()=>ledgerFixture.reconciliationCache()')==[]


def _hold_reconciliation_requests(page):
    page.evaluate('''() => {
        window.pendingReconciliations=[];
        // These fixtures exercise the established paged-reader lifecycle.
        // Dedicated bundle tests separately hold/coalesce the optional preload.
        window.fetch=(url,options)=>String(url).includes('/adjustment-bundle?')
            ?Promise.resolve({ok:false,status:404,json:async()=>({detail:'No preload in paged-reader fixture'})})
            :new Promise(resolve=>{
            window.pendingReconciliations.push({url:String(url),signal:options.signal,resolve});
        });
    }''')


def _finish_reconciliation_request(page,payload,index=0):
    page.evaluate('''({data,index})=>pendingReconciliations[index].resolve({ok:true,json:async()=>data})''',
                  {'data':payload,'index':index})


@pytest.mark.parametrize('width',[1280,390])
def test_reconciliation_initial_processing_fullpage_toolbar_and_return(ledger_cache_page,reconciliation_payload,width):
    page=ledger_cache_page
    page.set_viewport_size({'width':width,'height':820})
    report,parent,_,_=reconciliation_payload('split')
    page.evaluate('report=>ledgerFixture.load(report)',report)
    page.evaluate('''()=>{
        document.getElementById('cashflowsSummaryTable').style.minHeight='1800px';
        document.getElementById('cashflowsReconciliationButton').focus({preventScroll:true});
        window.scrollTo(0,350); window.reportScroll=window.scrollY;
        document.documentElement.style.cursor='help';
    }''')
    _hold_reconciliation_requests(page)
    page.evaluate('()=>{ledgerFixture.reconcile();}')
    assert page.locator('.reconciliation-shell').is_hidden()
    assert page.locator('#cashflowReconciliation').get_attribute('role')=='main'
    assert page.locator('#cashflowReconciliation').get_attribute('aria-labelledby')=='cashflowReconciliationTitle'
    assert page.locator('#cashflowReconciliationTitle').evaluate("(n)=>n.closest('.recon-accessible-context').getBoundingClientRect().height")==1
    assert page.locator('#reconProcessing .ui-spinner').is_visible()
    assert page.locator('#reconProcessing .ui-progress-bar-indeterminate').is_visible()
    assert page.locator('#reconProcessing .ui-skeleton-row').count()==6
    assert page.locator('#reconProcessing').get_attribute('aria-busy')=='true'
    assert 'Preparing reconciliation' in page.locator('#reconProcessing').inner_text()
    assert page.locator('#reconReload').is_disabled()
    assert page.locator('#reconClose').inner_text()=='Cashflows Report'
    assert page.locator('#reconClose').is_enabled()
    assert page.locator('.recon-toolbar #reconReportPeriod').inner_text()=='01 Jan 2026 – 31 Jan 2026'
    assert page.locator('#reconPeriod').count()==0
    assert page.locator('.recon-toolbar #reconReload').count()==1
    assert page.locator('.recon-toolbar #reconClose').count()==1
    toolbar=page.locator('.recon-toolbar').bounding_box()
    back=page.locator('#reconClose').bounding_box()
    assert back['x']>width/2 and back['y']>=toolbar['y']
    assert back['y']+back['height']<=toolbar['y']+toolbar['height']
    assert page.locator('#cashflowReconciliation').bounding_box()['width']==pytest.approx(width,abs=1)
    assert page.evaluate("document.documentElement.style.cursor")=='progress'
    page.locator('#reconClose').click()
    assert page.locator('.reconciliation-shell').is_visible()
    assert page.locator('#reconProcessing').is_hidden()
    assert page.locator('[aria-busy=true]').count()==0
    assert page.evaluate("document.documentElement.style.cursor")=='help'
    assert page.evaluate('pendingReconciliations[0].signal.aborted')
    assert page.evaluate('window.scrollY===window.reportScroll')
    _finish_reconciliation_request(page,parent)
    assert page.locator('#cashflowReconciliation').is_hidden()
    assert page.locator('#reconWorking').inner_html()==''


@pytest.mark.parametrize('action',['reload','differences','paging','matched'])
def test_reconciliation_later_processing_uses_relevant_shared_feedback(ledger_cache_page,reconciliation_payload,action):
    page=ledger_cache_page
    report,parent,support,posting=reconciliation_payload()
    _open_reconciliation(page,report,parent)
    def payload(query):
        if 'support_key' in query:return parent|{'support':support(query['support_key'][0])}
        if 'posting_differences' in query:
            number=int(query['page'][0])
            return parent|{'posting_differences':posting|dict(rows=[posting['findings'][number-1]],
                page=number,page_count=2,total_rows=2,limit=1)}
        return parent
    page.route('**/api/reconciliation/cashflows/reconciliation?*',
        lambda route:route.fulfill(json=payload(parse_qs(urlparse(route.request.url).query))))
    if action in ('paging','matched'):
        page.locator('#reconDifferencesTab').click();page.wait_for_selector('#reconDifferenceRows summary')
    _hold_reconciliation_requests(page)
    if action=='reload':page.locator('#reconReload').click()
    elif action=='support':_adjustment_button(page).first.click()
    elif action=='differences':page.locator('#reconDifferencesTab').click()
    elif action=='paging':page.locator('#reconDifferenceRows [data-recon-page]').filter(has_text='Next').first.click()
    else:page.locator('#reconIncludeMatched').check()
    target='#reconProcessing' if action=='reload' else '#reconSupport' if action=='support' else '#reconDifferenceRows'
    title='Preparing reconciliation' if action=='reload' else 'Loading adjustment voucher support' if action=='support' else 'Loading voucher differences'
    assert title in page.locator(target).inner_text()
    assert page.locator(target+' .ui-spinner').is_visible()
    assert page.locator(target+' .ui-progress-bar-indeterminate').is_visible()
    assert page.locator(target).get_attribute('aria-busy')=='true'
    assert page.locator('#reconClose').is_enabled()
    query=parse_qs(urlparse(page.evaluate('pendingReconciliations[0].url')).query)
    if action!='reload':assert query['expected_fingerprint']==['current-proof']
    if action=='paging':assert query['page']==['2']
    if action=='matched':assert query['differences_only']==['false']
    _finish_reconciliation_request(page,payload(query))
    page.wait_for_function("document.documentElement.style.cursor!=='progress'")
    assert page.locator('[aria-busy=true]').count()==0
    assert page.locator('#cashflowReconciliation .ui-spinner').count()==0
    assert page.locator('#reconReload').is_enabled()


def test_reconciliation_processing_ownership_survives_obsolete_support(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    report,parent,support,_=reconciliation_payload()
    _open_reconciliation(page,report,parent)
    page.evaluate('()=>{window.otherLoading=LoadingUI.startGlobalLoading();}')
    _hold_reconciliation_requests(page)
    _adjustment_button(page).first.click()
    query=parse_qs(urlparse(page.evaluate('pendingReconciliations[0].url')).query,keep_blank_values=True)
    page.locator('#reconAdjustmentFilters button[type=submit]').click()
    assert page.evaluate('pendingReconciliations.length')==1
    assert page.locator('#reconSupport .ui-spinner').is_visible()
    page.locator('#reconAdjustmentClose').click()
    assert page.evaluate('pendingReconciliations[0].signal.aborted')
    _finish_reconciliation_request(page,_flat_payload(parent,support,query))
    assert page.locator('#reconSupport').inner_html()==''
    assert page.locator('#cashflowReconciliation').is_visible()
    assert page.evaluate("document.documentElement.style.cursor")=='progress'
    page.evaluate('()=>LoadingUI.stopGlobalLoading(window.otherLoading)')
    assert page.evaluate("document.documentElement.style.cursor")==''


def test_reconciliation_timeout_releases_processing_and_allows_retry(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    report,parent,_,_=reconciliation_payload()
    page.evaluate('report=>ledgerFixture.load(report)',report)
    page.evaluate('''()=>{
        const originalTimeout=window.setTimeout;
        window.setTimeout=(callback,delay,...args)=>{
            if(delay===120000)window.fireReconciliationTimeout=callback;
            return originalTimeout(callback,delay,...args);
        };
        window.fetch=(url,options)=>new Promise((resolve,reject)=>{
            options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')));
        });
    }''')
    page.locator('#cashflowsReconciliationButton').click()
    assert page.locator('#reconProcessing .ui-spinner').is_visible()
    page.evaluate('()=>window.fireReconciliationTimeout()')
    page.wait_for_selector('#reconStatus.is-error')
    assert 'timed out' in page.locator('#reconStatus').inner_text()
    assert page.locator('#reconProcessing').is_hidden()
    assert page.evaluate("document.documentElement.style.cursor")==''
    assert page.locator('#reconReload').is_enabled()
    _hold_reconciliation_requests(page)
    page.locator('[data-recon-retry]').click()
    assert page.locator('#reconProcessing .ui-spinner').is_visible()
    _finish_reconciliation_request(page,parent)
    page.wait_for_selector('#reconWorking table')
    assert page.locator('#reconProcessing').is_hidden()


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
    if set(change) <= {'group_by','treasury_view','period_start','period_end'}:
        # Pending controls do not change the successfully loaded report scope.
        page.wait_for_selector('.cashflows-inline-ledger-link')
        assert page.locator('.cashflows-inline-ledger-link').count()==1
    else:
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
def test_treasury_bridge_uses_actual_source_and_keeps_mixed_allocation_review(ledger_cache_page, view, expected, tmp_path):
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
        page.screenshot(path=str(tmp_path / 'c4-mixed-bridge.png'), full_page=True)


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


@pytest.mark.parametrize('refresh_fails', [False, True])
def test_reconciliation_waits_for_report_refresh_and_opens_on_first_available_attempt(
        ledger_cache_page, reconciliation_payload, refresh_fails):
    page = ledger_cache_page
    report, parent, _, _ = reconciliation_payload()
    page.evaluate('result => ledgerFixture.load(result)', report)
    page.evaluate('''() => {
        window.waiting = [];
        window.fetch = (url) => new Promise(resolve => waiting.push({url:url, resolve:resolve}));
        window.reportRefresh = ledgerFixture.generate(false);
    }''')
    page.wait_for_function('waiting.length === 1')
    button = page.locator('#cashflowsReconciliationButton')
    assert button.is_visible() and button.is_disabled()
    assert button.get_attribute('aria-busy') == 'true'
    assert 'Checking the loaded report' in button.get_attribute('title')
    # Even a direct call cannot open a report that is about to be replaced.
    page.evaluate('() => ledgerFixture.reconcile()')
    assert page.locator('#cashflowReconciliation').is_hidden()
    assert page.evaluate('waiting.length') == 1
    page.evaluate('''async data => {
        waiting[0].resolve(new Response(JSON.stringify(data.report), {status:data.fails ? 503 : 200}));
        await reportRefresh;
    }''', {'report': report, 'fails': refresh_fails})
    assert button.is_enabled() and button.get_attribute('aria-busy') == 'false'
    button.click()
    page.wait_for_function('waiting.length === 2')
    assert '/cashflows/reconciliation?' in page.evaluate('waiting[1].url')
    assert page.locator('#cashflowReconciliation').is_visible()
    assert page.locator('#reconProcessing').is_visible()
    page.evaluate('data => waiting[1].resolve(new Response(JSON.stringify(data)))', parent)
    page.wait_for_selector('#reconWorking table')
    assert page.locator('#cashflowReconciliation').is_visible()
    assert page.evaluate('waiting.length') == 2  # No whole-paper preload fan-out.
    assert page.locator('#reconProcessing').is_hidden() and page.locator('#reconReload').is_enabled()


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

def _check_flat_popup(page, fixture, mode, view='combined'):
    state,calls=_cache_reconciliation_routes(page,fixture,view)
    assert page.locator('.recon-adjustment-group[open]').count()==0
    assert page.locator('#reconPeriod').count()==0
    button=_adjustment_button(page).first
    if mode=='oversized':
        data=state['support'](button.get_attribute('data-recon-support'))
        data['rows'][0]['ledger_lines'][0]['description']='账'*710000
        state['support']=lambda *args:data
    if mode=='safe':
        data=state['support'](button.get_attribute('data-recon-support'))
        data['rows'][0]['ledger_lines'][0].update(description='<img src=x onerror="window.injected=true">',debit='0.00001',credit='-0.00001',in_period=False)
        state['support']=lambda *args:data
    y=page.evaluate('scrollY')
    button.click()
    page.wait_for_selector('#reconSupport .recon-flat-table')
    assert page.locator('#reconSupport details').count()==0
    assert page.evaluate('scrollY')==y
    evidence=page.locator('#reconSupport').inner_text()
    if mode=='safe':
        assert '0.00001' in evidence and '-0.00001' in evidence
        assert 'Outside range · context only' in evidence
        assert page.locator('#reconSupport img').count()==0
        assert not page.evaluate('Boolean(window.injected)')
    if mode=='oversized':
        assert page.locator('#reconSupport').evaluate("n=>n.textContent.includes('账'.repeat(710000))")
        assert len(page.evaluate('()=>ledgerFixture.reconciliationCache()'))==1
    if mode=='close':
        page.keyboard.press('Escape')
        assert page.locator('#reconAdjustmentDialog').is_hidden()
        assert page.locator('#cashflowReconciliation').is_visible()
        assert button.evaluate('(b)=>b===document.activeElement')
        return
    page.locator('#reconAdjustmentClose').click()
    if mode=='changed':
        state['parent']['cache_identity']='b'*64
        button.click()
        page.wait_for_selector('#reconSupport [role=alert]')
        assert 'Source changed' in page.locator('#reconSupport').inner_text()
        assert page.locator('#reconWorking').inner_html()==''
        assert page.locator('#reconExport').is_disabled()
        return
    button.click()
    page.wait_for_selector('#reconSupport .recon-flat-table')
    assert page.locator('#reconSupport').inner_text()==evidence
    assert [kind for kind,_ in calls]==(['full','full','full'] if mode=='oversized' else ['full','full','validation'])
    page.locator('#reconAdjustmentClose').click()
    if mode=='cache':
        children=[json.loads(v['json']) for _,v in page.evaluate('()=>ledgerFixture.reconciliationCache()') if 'transactions' in json.loads(v['json'])]
        assert len(children)==1 and 'views' not in children[0]
        page.locator('#reconClose').click()
        assert page.locator('#reconSupport').inner_html()==''
        page.locator('#cashflowsReconciliationButton').click()
        page.wait_for_selector('#reconWorking table')
        assert calls[-1][0]=='validation'
        page.locator('#reconReload').click()
        page.wait_for_selector('#reconWorking table')
        assert calls[-1][0]=='full'


@pytest.mark.parametrize('width',[1366,1280,390])
def test_flat_popup_pages_searches_and_contains_all_original_columns(ledger_cache_page,reconciliation_payload,width):
    page=ledger_cache_page;page.set_viewport_size({'width':width,'height':820})
    state,calls=_cache_reconciliation_routes(page,reconciliation_payload)
    assert len(calls)==1 and page.locator('.recon-adjustment-group[open]').count()==0
    button=_adjustment_button(page).first
    data=state['support'](button.get_attribute('data-recon-support'))
    base=data['rows'][0]['ledger_lines'][0]
    data['rows'][0]['ledger_lines']=[dict(base,id=i,description='Unique needle' if i==77 else 'Other',**{'Running balance':str(i)+'.00001'}) for i in range(121)]
    state['support']=lambda *args:data
    button.click();page.wait_for_selector('#reconSupport .recon-flat-table')
    assert '121 source row(s)' in page.locator('#reconAdjustmentPage').inner_text()
    assert page.locator('#reconSupport tbody tr').count()==50
    assert 'Running balance' in page.locator('#reconSupport thead').inner_text()
    dialog=page.locator('#reconAdjustmentDialog').bounding_box()
    assert dialog['x']>=0 and dialog['x']+dialog['width']<=width+1
    assert page.locator('#reconAdjustmentDialog').evaluate('(d)=>d.scrollWidth<=d.clientWidth+1')
    assert page.locator('#reconAdjustmentFilters [data-adjustment-page]').count()==2
    assert page.locator('#reconSupport [data-adjustment-page]').count()==0
    assert page.locator('#reconSupport > p').count()==0
    if width>=1280:
        controls=[page.locator(selector).bounding_box() for selector in
                  ['#reconAdjustmentSearch','#reconAdjustmentBook','#reconAdjustmentFilters button[type=submit]',
                   '#reconAdjustmentClear','#reconAdjustmentPrevious','#reconAdjustmentPage','#reconAdjustmentNext']]
        centers=[box['y']+box['height']/2 for box in controls]
        assert max(centers)-min(centers)<2
        assert all(left['x']+left['width']<=right['x'] for left,right in zip(controls,controls[1:]))
    page.locator('#reconAdjustmentNext').click()
    page.wait_for_function("document.getElementById('reconAdjustmentPage').innerText.includes('Page 2 of 3')")
    assert '50.00001' in page.locator('#reconSupport').inner_text()
    page.locator('#reconAdjustmentSearch').fill('Unique needle')
    page.locator('#reconAdjustmentSearch').press('Enter')
    page.wait_for_function("document.getElementById('reconAdjustmentPage').innerText.includes('1 source row(s)')")
    assert page.locator('#reconSupport tbody tr').count()==1
    assert '77.00001' in page.locator('#reconSupport').inner_text()
    assert page.locator('#reconAdjustmentPrevious').is_disabled()
    assert page.locator('#reconAdjustmentNext').is_disabled()
    assert 'Adjustment contribution' not in page.locator('#reconSupport').inner_text()
    page.locator('#reconAdjustmentSearch').fill('No such original source row')
    page.locator('#reconAdjustmentSearch').press('Enter')
    page.wait_for_function("document.getElementById('reconAdjustmentPage').innerText.includes('0 source row(s)')")
    assert 'No matching source rows' in page.locator('#reconSupport').inner_text()
    assert page.locator('#reconAdjustmentPrevious').is_disabled() and page.locator('#reconAdjustmentNext').is_disabled()
    page.locator('#reconAdjustmentClear').click()
    page.wait_for_function("document.getElementById('reconAdjustmentPage').innerText.includes('121 source row(s)')")
    assert page.locator('#reconSupport tbody tr').count()==50
    assert page.locator('#reconAdjustmentPrevious').is_disabled() and page.locator('#reconAdjustmentNext').is_enabled()
    # Native modal focus containment, with the background report still at its position.
    page.locator('#reconAdjustmentClose').focus();page.keyboard.press('Shift+Tab')
    assert page.locator('#reconAdjustmentDialog').evaluate('(d)=>d.contains(document.activeElement)')
    page.keyboard.press('Escape')
    assert page.locator('#reconAdjustmentDialog').is_hidden()
    assert page.locator('#cashflowReconciliation').is_visible()


@pytest.mark.parametrize('change',['wrong-side','wrong-proof','wrong-total','wrong-accounts'])
def test_flat_popup_rejects_mismatched_detail_scope(ledger_cache_page,reconciliation_payload,change):
    page=ledger_cache_page
    report,parent,support,_=reconciliation_payload()
    _open_reconciliation(page,report,parent)
    def respond(route):
        query=parse_qs(urlparse(route.request.url).query,keep_blank_values=True)
        value=_flat_payload(parent,support,query)
        if change=='wrong-side':value['side']='payments' if value['side']=='receipts' else 'receipts'
        if change=='wrong-proof':value['proof']='bank'
        if change=='wrong-total':value['adjustment_total']+=1
        if change=='wrong-accounts':value['account_selection']={'account_codes':['OTHER']}
        route.fulfill(json=value)
    page.route('**/api/reconciliation/cashflows/reconciliation/adjustment-transactions?*',respond)
    _adjustment_button(page).first.click()
    page.wait_for_selector('#reconSupport [role=alert]')
    assert page.locator('#reconSupport .recon-flat-table').count()==0
    assert page.locator('#reconWorking').inner_html()==''
    assert page.locator('#reconStatus.is-error').is_visible()
    assert page.locator('#reconAdjustmentPrevious').is_disabled() and page.locator('#reconAdjustmentNext').is_disabled()
    assert page.locator('#reconAdjustmentPage').inner_text()=='Transactions unavailable'


def test_flat_popup_preserves_source_digits_without_number_coercion(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    state,_=_cache_reconciliation_routes(page,reconciliation_payload)
    button=_adjustment_button(page).first
    data=state['support'](button.get_attribute('data-recon-support'))
    data['rows'][0]['ledger_lines'][0].update(Amount='9007199254740993.00001',Tiny='1E-26',numeric_columns=['Amount','Tiny'])
    state['support']=lambda *args:data
    button.click();page.wait_for_selector('#reconSupport .recon-flat-table')
    assert '9,007,199,254,740,993.00001' in page.locator('#reconSupport').inner_text()
    assert '1E-26' in page.locator('#reconSupport').inner_text()


@pytest.mark.parametrize('width',[1366,1280,1024,390])
@pytest.mark.parametrize('view',['combined','split'])
def test_reconciliation_compact_toolbar_keeps_scope_tabs_and_actions_together(ledger_cache_page,reconciliation_payload,width,view):
    page=ledger_cache_page;page.set_viewport_size({'width':width,'height':820})
    state,_=_cache_reconciliation_routes(page,reconciliation_payload,view)
    assert page.locator('#cashflowReconciliationTitle').text_content()=='Ledger / Cashflow reconciliation'
    assert page.locator('#reconContext').evaluate("(n)=>n.closest('.recon-accessible-context').getBoundingClientRect().height")==1
    assert page.locator('#reconStatus').is_hidden()
    assert 'Full report period' not in page.locator('#reconWorking').inner_text()
    assert 'Receipt/payment amounts:' not in page.locator('#cashflowReconciliation').inner_text()
    assert page.locator('.recon-toolbar [role=tablist]').count()==1
    selectors=['.recon-report-period']
    if view=='split':selectors+=['#reconViewLabel']
    selectors+=['.recon-tabs','#reconCompletenessOpen','#reconReload','#reconExport','#reconClose']
    assert page.locator('.recon-toolbar #reconCompletenessOpen').count()==1
    assert page.locator('.recon-body > .recon-completeness-actions').count()==0
    boxes=[page.locator(selector).bounding_box() for selector in selectors]
    assert all(box['x']>=0 and box['x']+box['width']<=width+1 for box in boxes)
    if width>=1280:
        centers=[box['y']+box['height']/2 for box in boxes]
        assert max(centers)-min(centers)<2
        assert all(left['x']+left['width']<=right['x'] for left,right in zip(boxes,boxes[1:]))
    else:
        assert boxes[-1]['y']>boxes[0]['y']
    # Tabs retain keyboard access and return to the unmodified accounting schedules.
    page.locator('#reconWorkingTab').focus();page.keyboard.press('ArrowRight')
    assert page.locator('#reconDifferencesTab').get_attribute('aria-selected')=='true'
    page.keyboard.press('ArrowLeft')
    assert page.locator('#reconWorkingTab').get_attribute('aria-selected')=='true'
    assert page.locator('#reconWorking .recon-section').count()==3
    body=page.locator('.recon-body').bounding_box()
    first=page.locator('#reconWorking > .recon-section').first.bounding_box()
    assert first['y']-body['y']<=17


def _bundle_payload(parent,support,proof,book,support_key=None,requested_side=None):
    rows={};groups=[]
    for adjustment in parent['views'][proof]['adjustments']:
        for side in ('receipts','payments'):
            if not adjustment['total'][side] or (support_key and (adjustment['key']!=support_key or side!=requested_side)):continue
            source=support(adjustment['key'])['rows']
            selected={r['id']:r for g in source if g[side]!=0 for r in g['ledger_lines' if book=='ledger' else 'voucher_lines']}
            for row_id,row in selected.items():
                rows[row_id]=dict(row,row_data=dict(row),search_text=json.dumps(row,default=str))
            groups.append(dict(key=adjustment['key'],side=side,adjustment_total=adjustment['total'][side],
                row_ids=list(selected),in_period_row_ids=[i for i,r in selected.items() if r.get('in_period',True)]))
    context={k:v for k,v in parent.items() if k not in ('views','periods','review_controls','diagnostics')}
    return context|dict(bundle_schema=2,support_key=support_key,side=requested_side,proof=proof,source_book=book,complete=True,rows=list(rows.values()),
        columns=list(dict.fromkeys(k for r in rows.values() for k in r['row_data'])),groups=groups)


def _open_bundle_reconciliation(page,fixture,view='combined',large=False,wait=True):
    report,parent,support,posting=fixture(view)
    parent.update(cache_schema=1,cache_identity='a'*64,template_id=report['configuration']['source_template_id'])
    candidates=parent['views']['combined' if view=='split' else view]['adjustments']
    target=next((r for r in candidates if r['total']['receipts']>0),next(r for r in candidates if r['total']['receipts']!=0))
    original=support
    def support(key):
        value=original(key)
        if key==target['key']:
            base=value['rows'][0]['ledger_lines'][0]
            value['rows'][0]['ledger_lines']=[dict(base,id=10000+i,description='Unique %_needle' if i==77 else 'Other',
                **{'Running balance':str(i)+'.00001'}) for i in range(1001 if large else 121)]
        return value
    state=dict(parent=parent,support=support,status=200,override=None);calls=[]
    def respond(route):
        query=parse_qs(urlparse(route.request.url).query,keep_blank_values=True)
        kind='bundle' if '/adjustment-bundle?' in route.request.url else 'validation' if '/cache-validation?' in route.request.url else 'page' if '/adjustment-transactions?' in route.request.url else 'paper'
        calls.append((kind,query))
        if kind=='bundle':
            value=_bundle_payload(state['parent'],state['support'],query['proof'][0],query['source_book'][0],query['support_key'][0],query['side'][0])
            if large:value.update(complete=False,reason='row_limit',rows=[],columns=[],groups=[])
            if state['override']:value=state['override'](value)
            return route.fulfill(status=state['status'],json=value)
        if kind=='page':return route.fulfill(json=_flat_payload(state['parent'],state['support'],query))
        return route.fulfill(json=state['parent'])
    for endpoint in ['reconciliation?','reconciliation/cache-validation?','reconciliation/adjustment-bundle?','reconciliation/adjustment-transactions?']:
        page.route('**/api/reconciliation/cashflows/'+endpoint+'*',respond)
    page.evaluate('r=>ledgerFixture.load(r)',report)
    page.locator('#cashflowsReconciliationButton').click();page.wait_for_selector('#reconWorking table')
    button=page.locator('[data-recon-support="'+target['key']+'"][data-recon-side=receipts]').first
    button.evaluate('(b)=>b.closest("details").open=true')
    assert [kind for kind,_ in calls]==['paper']
    if wait:
        button.hover()
        page.wait_for_function("ledgerFixture.reconciliationCache().some(([k,v])=>JSON.parse(v.json).bundle_schema===2)")
    return state,calls,button


@pytest.mark.parametrize('view',['combined','bank','cash','split'])
def test_adjustment_bundle_preloads_once_and_pages_searches_without_network(ledger_cache_page,reconciliation_payload,view):
    page=ledger_cache_page
    state,calls,button=_open_bundle_reconciliation(page,reconciliation_payload,view)
    assert [kind for kind,_ in calls]==['paper','bundle']
    button.click();page.wait_for_selector('#reconSupport .recon-flat-table')
    assert '121 source row(s)' in page.locator('#reconAdjustmentPage').inner_text()
    before=len(calls)
    page.locator('#reconAdjustmentNext').click()
    assert 'Page 2 of 3' in page.locator('#reconAdjustmentPage').inner_text()
    assert '50.00001' in page.locator('#reconSupport').inner_text()
    page.locator('#reconAdjustmentSearch').fill('%_needle');page.locator('#reconAdjustmentSearch').press('Enter')
    assert '1 source row(s)' in page.locator('#reconAdjustmentPage').inner_text()
    assert '77.00001' in page.locator('#reconSupport').inner_text()
    page.locator('#reconAdjustmentClear').click()
    assert '121 source row(s)' in page.locator('#reconAdjustmentPage').inner_text()
    assert len(calls)==before and page.locator('#reconSupport .ui-spinner').count()==0
    page.locator('#reconAdjustmentClose').click()
    button.click();page.wait_for_selector('#reconSupport .recon-flat-table')
    assert sum(k=='bundle' for k,_ in calls)==1 and sum(k=='validation' for k,_ in calls)>=1
    # The other source book is fetched only on request, then stays local too.
    page.locator('#reconAdjustmentBook').select_option('voucher')
    page.locator('#reconAdjustmentFilters button[type=submit]').click()
    page.wait_for_function("document.querySelector('#reconSupport thead').innerText.includes('Scope')")
    assert sum(k=='bundle' for k,_ in calls)==2
    before=len(calls);page.locator('#reconAdjustmentClear').click()
    assert len(calls)==before
    entries=page.evaluate('()=>ledgerFixture.reconciliationCache()')
    assert len(entries)<=20 and sum(e['bytes'] for _,e in entries)<=2*1024*1024


def test_adjustment_bundle_large_scope_falls_back_without_claiming_completeness(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    state,calls,button=_open_bundle_reconciliation(page,reconciliation_payload,large=True)
    button.click();page.wait_for_selector('#reconSupport .recon-flat-table')
    assert '1001 source row(s)' in page.locator('#reconAdjustmentPage').inner_text()
    page.locator('#reconAdjustmentNext').click()
    page.wait_for_function("document.getElementById('reconAdjustmentPage').innerText.includes('Page 2 of 21')")
    page.wait_for_function("ledgerFixture.reconciliationCache().filter(([k,v])=>JSON.parse(v.json).transactions).length>=3")
    assert sum(k=='page' for k,_ in calls)==3 and sum(k=='bundle' for k,_ in calls)==1


def test_adjustment_group_intent_does_not_fetch_other_groups_and_cached_groups_stay_separate(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    state,calls,first=_open_bundle_reconciliation(page,reconciliation_payload)
    first_key=first.get_attribute('data-recon-support')
    other=next(r for r in state['parent']['views']['combined']['adjustments'] if r['key']!=first_key and r['total']['receipts'])
    second=page.locator('[data-recon-support="'+other['key']+'"][data-recon-side=receipts]').first
    second.evaluate('(b)=>b.closest("details").open=true');second.hover()
    page.wait_for_function("ledgerFixture.reconciliationCache().filter(([k,v])=>JSON.parse(v.json).bundle_schema===2).length===2")
    bundle_calls=[q for kind,q in calls if kind=='bundle']
    assert [q['support_key'][0] for q in bundle_calls]==[first_key,other['key']]
    second.click();page.wait_for_selector('#reconSupport .recon-flat-table')
    assert '121 source row(s)' not in page.locator('#reconAdjustmentPage').inner_text()
    page.locator('#reconAdjustmentClose').click()
    first.click();page.wait_for_selector('#reconSupport .recon-flat-table')
    assert '121 source row(s)' in page.locator('#reconAdjustmentPage').inner_text()
    assert sum(kind=='bundle' for kind,_ in calls)==2


def test_adjustment_hover_waits_for_stable_pointer_and_click_starts_immediately(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    state,calls,first=_open_bundle_reconciliation(page,reconciliation_payload,wait=False)
    first_key=first.get_attribute('data-recon-support')
    other=next(r for r in state['parent']['views']['combined']['adjustments'] if r['key']!=first_key and r['total']['receipts'])
    second=page.locator('[data-recon-support="'+other['key']+'"][data-recon-side=receipts]').first
    first.evaluate('(b)=>b.closest("details").open=true')
    second.evaluate('(b)=>b.closest("details").open=true')

    first.hover()
    page.wait_for_timeout(60)
    second.hover()
    page.wait_for_function("ledgerFixture.reconciliationCache().filter(([k,v])=>JSON.parse(v.json).bundle_schema===2).length===1")
    bundle_calls=[q for kind,q in calls if kind=='bundle']
    assert [q['support_key'][0] for q in bundle_calls]==[other['key']]

    first.focus()
    first.evaluate('(b)=>b.click()')
    page.wait_for_selector('#reconSupport .recon-flat-table')
    page.wait_for_timeout(300)
    bundle_calls=[q for kind,q in calls if kind=='bundle']
    assert [q['support_key'][0] for q in bundle_calls]==[other['key'],first_key]


@pytest.mark.parametrize('status',[401,403,409])
def test_adjustment_group_hover_rejects_auth_or_stale_source(ledger_cache_page,reconciliation_payload,status):
    page=ledger_cache_page
    state,calls,button=_open_bundle_reconciliation(page,reconciliation_payload,wait=False)
    state['status']=status
    button.hover();page.wait_for_selector('#reconStatus.is-error')
    assert page.locator('#reconWorking').inner_html()=='' and page.locator('#reconExport').is_disabled()
    assert not any(json.loads(v['json']).get('bundle_schema') for _,v in page.evaluate('()=>ledgerFixture.reconciliationCache()'))


def test_oversized_next_prefetch_is_shared_and_popup_close_discards_delayed_rows(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    page.evaluate('''()=>{
        window.baseFetch=window.fetch;window.nextReads=[];
        window.fetch=(url,options)=>String(url).includes('/adjustment-transactions?') && new URL(url,'https://cashflows.test').searchParams.get('page')==='2'
            ?new Promise(resolve=>nextReads.push({url,resolve,signal:options.signal})):baseFetch(url,options);
    }''')
    state,calls,button=_open_bundle_reconciliation(page,reconciliation_payload,large=True)
    button.click();page.wait_for_selector('#reconSupport .recon-flat-table')
    page.wait_for_function('nextReads.length===1')
    page.locator('#reconAdjustmentNext').click();page.wait_for_selector('#reconSupport .ui-spinner')
    assert page.evaluate('nextReads.length')==1
    query=parse_qs(urlparse(page.evaluate('nextReads[0].url')).query,keep_blank_values=True)
    value=_flat_payload(state['parent'],state['support'],query)
    page.locator('#reconAdjustmentClose').click()
    assert page.evaluate('nextReads[0].signal.aborted')
    page.evaluate('v=>nextReads[0].resolve({ok:true,json:async()=>v})',value)
    page.wait_for_timeout(30)
    assert page.locator('#reconSupport').inner_html()==''
    assert not any(json.loads(v['json']).get('transactions',{}).get('page')==2 for _,v in page.evaluate('()=>ledgerFixture.reconciliationCache()'))


def test_adjustment_bundle_reopening_rejects_changed_source_and_retains_retry(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    state,calls,button=_open_bundle_reconciliation(page,reconciliation_payload)
    button.click();page.wait_for_selector('#reconSupport .recon-flat-table')
    page.locator('#reconAdjustmentClose').click()
    state['parent']=state['parent']|{'cache_identity':'b'*64}
    button.click();page.wait_for_selector('#reconStatus.is-error')
    assert page.locator('#reconWorking').inner_html()==''
    assert page.locator('#reconExport').is_disabled()
    assert page.locator('#reconSupport .recon-flat-table').count()==0


@pytest.mark.parametrize('damage',['total','proof','rows','missing-group','accounts'])
def test_adjustment_bundle_rejects_incomplete_or_wrong_scope_before_display(ledger_cache_page,reconciliation_payload,damage):
    page=ledger_cache_page
    state,calls,button=_open_bundle_reconciliation(page,reconciliation_payload)
    page.evaluate('()=>ledgerFixture.clearReconciliationCache()')
    def change(value):
        if damage=='total':value['groups'][0]['adjustment_total']+=1
        elif damage=='proof':value['proof']='invalid'
        elif damage=='rows':value['rows'].append(value['rows'][0])
        elif damage=='accounts':value['account_selection']={'account_codes':['OTHER']}
        else:value['groups']=[]
        return value
    state['override']=change
    button.click();page.wait_for_selector('#reconStatus.is-error')
    assert page.locator('#reconSupport .recon-flat-table').count()==0
    assert page.locator('#reconExport').is_disabled()


def test_adjustment_bundle_pending_preload_is_shared_and_late_closure_cannot_restore_cache(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    page.evaluate('''()=>{
        window.baseFetch=window.fetch;window.bundleReads=[];
        window.fetch=(url,options)=>String(url).includes('/adjustment-bundle?')
            ?new Promise(resolve=>bundleReads.push({resolve,signal:options.signal})):baseFetch(url,options);
    }''')
    state,calls,button=_open_bundle_reconciliation(page,reconciliation_payload,wait=False)
    button.click();page.wait_for_selector('#reconSupport .ui-spinner')
    assert page.evaluate('bundleReads.length')==1
    page.locator('#reconAdjustmentClose').click()
    # Ledger preload belongs to the still-open paper, not to an individual popup.
    assert not page.evaluate('bundleReads[0].signal.aborted')
    button.click();page.wait_for_selector('#reconSupport .ui-spinner')
    assert page.evaluate('bundleReads.length')==1
    support_key=button.get_attribute('data-recon-support')
    page.locator('#reconAdjustmentClose').click();page.locator('#reconClose').click()
    assert page.evaluate('bundleReads[0].signal.aborted')
    value=_bundle_payload(state['parent'],state['support'],'combined','ledger',support_key,'receipts')
    page.evaluate('v=>bundleReads[0].resolve({ok:true,json:async()=>v})',value)
    page.wait_for_timeout(30)
    assert not any(json.loads(v['json']).get('bundle_schema') for _,v in page.evaluate('()=>ledgerFixture.reconciliationCache()'))
    assert page.locator('#reconSupport').inner_html()==''


def test_adjustment_bundle_focus_intent_shares_validation_and_focus_return_does_not_refetch(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    state,calls,button=_open_bundle_reconciliation(page,reconciliation_payload)
    button.click();page.wait_for_selector('#reconSupport .recon-flat-table')
    page.locator('#reconAdjustmentClose').click()
    page.locator('#reconReload').focus()
    page.evaluate('''()=>{
        window.baseFetch=window.fetch;window.identityReads=[];
        window.fetch=(url,options)=>String(url).includes('/cache-validation?')
            ?new Promise(resolve=>identityReads.push({resolve,signal:options.signal})):baseFetch(url,options);
    }''')
    button.focus();button.evaluate('(b)=>b.click()')
    page.wait_for_selector('#reconSupport .ui-spinner')
    assert page.evaluate('identityReads.length')==1
    page.evaluate('v=>identityReads[0].resolve({ok:true,json:async()=>v})',state['parent'])
    page.wait_for_selector('#reconSupport .recon-flat-table')
    page.locator('#reconAdjustmentNext').click();assert page.evaluate('identityReads.length')==1
    page.locator('#reconAdjustmentClose').click();assert page.evaluate('identityReads.length')==1


def test_adjustment_bundle_window_return_revalidates_before_reusing_active_snapshot(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    state,calls,button=_open_bundle_reconciliation(page,reconciliation_payload)
    button.click();page.wait_for_selector('#reconSupport .recon-flat-table')
    page.locator('#reconAdjustmentNext').click()
    page.evaluate("()=>window.dispatchEvent(new Event('focus'))")
    page.wait_for_selector('#reconSupport .recon-flat-table')
    assert 'Page 2 of 3' in page.locator('#reconAdjustmentPage').inner_text()
    state['parent']=state['parent']|{'cache_identity':'b'*64}
    page.evaluate("()=>window.dispatchEvent(new Event('focus'))")
    page.wait_for_selector('#reconStatus.is-error')
    assert page.locator('#reconSupport .recon-flat-table').count()==0
    assert page.locator('#reconExport').is_disabled()


def test_adjustment_bundle_unicode_search_uses_authoritative_literal_filter(ledger_cache_page,reconciliation_payload):
    page=ledger_cache_page
    state,calls,button=_open_bundle_reconciliation(page,reconciliation_payload)
    button.click();page.wait_for_selector('#reconSupport .recon-flat-table')
    page.locator('#reconAdjustmentSearch').fill('\u0130stanbul')
    page.locator('#reconAdjustmentSearch').press('Enter')
    page.wait_for_function("document.getElementById('reconAdjustmentPage').innerText.includes('0 source row(s)')")
    assert calls[-1][0]=='page' and calls[-1][1]['search']==['\u0130stanbul']
    before=len(calls);page.locator('#reconAdjustmentClear').click()
    page.wait_for_function("document.getElementById('reconAdjustmentPage').innerText.includes('121 source row(s)')")
    assert sum(k=='bundle' for k,_ in calls)==1
    assert all(kind=='validation' for kind,_ in calls[before:])
