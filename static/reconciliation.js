/* Reports centre and the first simple Cashflows report. */
(function () {
    'use strict';
    let clientId = null;
    let lastCashflowsResult = null;
    let classifiedTransactionsPage = 1;
    let classifiedTransactionsTotal = 0;
    const classifiedTransactionsLimit = 50;
    let classifiedTransactionsRequest = null;
    const CASHFLOW_REQUEST_TIMEOUT_MS = 120000;
    let cashflowSelection = { start: '', end: '', groupBy: 'month' };
    let cashflowTreasuryView = 'combined';
    // Toolbar edits are pending. Financial readers use lastCashflowsResult.
    let cashflowPendingAccounts = null;
    let cashflowAccountOptions = null;
    let cashflowAccountOptionsPromise = null;
    let cashflowAccountOptionsClient = null;
    let cashflowAccountOptionsKey = '';
    function cashflowAccountCodes(value) {
        const selection = value && value.account_selection;
        return selection ? selection.account_codes : null;
    }
    function cashflowAccountKey(codes) {
        if (codes == null) return 'all';
        if (!Array.isArray(codes) || !codes.length || codes.some(function (code) { return typeof code !== 'string' || !code.trim(); })) return 'invalid';
        return JSON.stringify(Array.from(new Set(codes.map(function (code) { return code.trim(); }))).sort());
    }
    function cashflowAccountsInView(codes, view) {
        if (codes == null || !['bank', 'cash'].includes(view)) return codes;
        if (!cashflowAccountOptions) throw new Error('Reload account choices before filtering this account selection.');
        const eligible = new Set(cashflowAccountOptions.filter(function (account) { return account.treasury_type === view; }).map(function (account) { return account.account_code; }));
        return codes.filter(function (code) { return eligible.has(code); });
    }
    function appendCashflowAccounts(params, codes) {
        if (codes != null) {
            if (cashflowAccountKey(codes) === 'invalid') throw new Error('Select at least one Cash/Bank account.');
            codes.forEach(function (code) { params.append('selected_treasury_account_codes', code); });
        }
        return params;
    }
    function cashflowLoadedScope() {
        const report = lastCashflowsResult;
        return report && report.period_start && report.period_end ? report : {client_id: clientId, period_start: cashflowSelection.start, period_end: cashflowSelection.end,
            group_by: cashflowSelection.groupBy, treasury_view: cashflowTreasuryView};
    }
    function cashflowLoadedClientChanged() {
        const report = lastCashflowsResult;
        const changed = report && report.client_id != null && Number(report.client_id) !== Number(clientId);
        if (changed) showCashflowToast('Client changed. Reload Cashflows to continue.', true);
        return changed;
    }
    function cashflowEligibleAccounts() {
        return (cashflowAccountOptionsClient === clientId ? cashflowAccountOptions || [] : []).filter(function (account) {
            return !['bank', 'cash'].includes(cashflowTreasuryView) || account.treasury_type === cashflowTreasuryView;
        });
    }
    function cashflowEffectivePendingAccounts() {
        if (cashflowPendingAccounts == null) return null;
        if (cashflowAccountOptions && cashflowAccountOptionsClient !== clientId) return [];
        if (!cashflowAccountOptions) return cashflowPendingAccounts.slice().sort();
        const eligible = new Set(cashflowEligibleAccounts().map(function (account) { return account.account_code; }));
        return cashflowPendingAccounts.filter(function (code) { return eligible.has(code); }).sort();
    }
    function syncCashflowAccountButton() {
        const value = element('cashflowsAccountValue'), button = element('cashflowsAccountButton');
        if (!value || !button) return;
        const codes = cashflowEffectivePendingAccounts();
        value.textContent = codes == null ? 'All accounts' : codes.length ? codes.length + ' selected' : 'Select accounts';
        const pending = !!lastCashflowsResult && cashflowAccountKey(codes) !== cashflowAccountKey(cashflowAccountCodes(lastCashflowsResult));
        button.classList.toggle('is-pending', pending);
        button.title = (pending ? 'Pending changes — Load summary to apply. ' : '') +
            (codes == null ? 'All accounts in the Treasury view' : codes.join(', ') || 'Select at least one compatible account');
        element('cashflowsAccountHint').textContent = pending ? 'Pending — Load summary to apply' : 'Apply with Load summary';
        const all = element('cashflowsAccountAll');
        all.checked = cashflowPendingAccounts == null;
        all.indeterminate = cashflowPendingAccounts != null && !!codes.length;
    }
    function renderCashflowAccountOptions() {
        const host = element('cashflowsAccountOptions');
        if (!host) return;
        const search = element('cashflowsAccountSearch').value.trim().toLowerCase();
        const accounts = cashflowEligibleAccounts();
        const selected = new Set(cashflowEffectivePendingAccounts() || accounts.map(function (account) { return account.account_code; }));
        host.innerHTML = ['bank', 'cash'].map(function (kind) {
            const rows = accounts.filter(function (account) {
                return account.treasury_type === kind && (!search ||
                    (account.account_code + ' ' + account.account_name).toLowerCase().includes(search));
            });
            if (!rows.length) return '';
            return '<div class="account-group">' + (kind === 'bank' ? 'Bank accounts' : 'Cash accounts') + '</div>' + rows.map(function (account) {
                return '<label class="account-option"><input type="checkbox" data-cashflow-account="' + escapeHtml(account.account_code)
                    + '"' + (selected.has(account.account_code) ? ' checked' : '') + '><span><span class="account-name">'
                    + escapeHtml(account.account_name) + '</span><span class="account-code">' + escapeHtml(account.account_code) + '</span></span></label>';
            }).join('');
        }).join('');
        if (cashflowAccountOptions && !host.children.length) host.textContent = search ? 'No matching accounts.' : 'No configured accounts in this Treasury view.';
        if (cashflowPendingAccounts != null && !search) {
            const checked = host.querySelector('input:checked');
            if (checked) host.scrollTop = Math.max(0, checked.closest('label').offsetTop - host.offsetTop - 28);
        }
        syncCashflowAccountButton();
    }
    function closeCashflowAccountPopover(focus) {
        element('cashflowsAccountPopover').hidden = true;
        element('cashflowsAccountButton').setAttribute('aria-expanded', 'false');
        if (focus) element('cashflowsAccountButton').focus();
    }
    async function loadCashflowAccountOptions(force) {
        if (cashflowAccountOptionsClient === clientId && cashflowAccountOptions && !force) return true;
        if (cashflowAccountOptionsPromise) return cashflowAccountOptionsPromise;
        const requestedClient = clientId, status = element('cashflowsAccountStatus');
        const task = (async function () {
            let notice = '';
            if (window.LoadingUI) window.LoadingUI.showSpinner(status, 'Loading Cash/Bank accounts…');
            else status.textContent = 'Loading Cash/Bank accounts…';
            element('cashflowsAccountRetry').hidden = true;
            const controller = new AbortController();
            const timer = window.setTimeout(function () { controller.abort(); }, 30000);
            try {
                const result = await api('/api/reconciliation/cashflows/account-options?client_id=' + encodeURIComponent(requestedClient), {signal: controller.signal});
                if (requestedClient !== clientId) return false;
                if (Number(result.client_id) !== Number(clientId) || !Array.isArray(result.accounts)
                    || result.accounts.some(function (account) { return !account.account_code || !['cash', 'bank'].includes(account.treasury_type); })) {
                    throw new Error('Account choices could not be verified. Retry accounts.');
                }
                cashflowAccountOptions = result.accounts;
                cashflowAccountOptionsKey = result.configuration_key || '';
                cashflowAccountOptionsClient = requestedClient;
                status.textContent = '';
                renderCashflowAccountOptions();
                return true;
            } catch (error) {
                if (requestedClient === clientId) {
                    notice = 'Accounts could not be loaded. Retry to select accounts.';
                    element('cashflowsAccountRetry').hidden = false;
                }
                return false;
            } finally {
                window.clearTimeout(timer);
                if (window.LoadingUI) window.LoadingUI.hideSpinner(status);
                if (requestedClient === clientId) status.textContent = notice;
            }
        }());
        cashflowAccountOptionsPromise = task;
        try { return await task; } finally { if (cashflowAccountOptionsPromise === task) cashflowAccountOptionsPromise = null; }
    }
    function initialiseCashflowAccounts() {
        const popover = element('cashflowsAccountPopover');
        if (popover.dataset.initialised) return;
        popover.dataset.initialised = 'true';
        element('cashflowsAccountButton').addEventListener('click', function () {
            if (!popover.hidden) { closeCashflowAccountPopover(false); return; }
            closeCashflowPopover(); closeCashflowRefreshMenu();
            popover.hidden = false;
            element('cashflowsAccountButton').setAttribute('aria-expanded', 'true');
            renderCashflowAccountOptions();
            loadCashflowAccountOptions(false);
            element('cashflowsAccountSearch').focus();
        });
        element('cashflowsAccountSearch').addEventListener('input', renderCashflowAccountOptions);
        element('cashflowsAccountAll').addEventListener('change', function () {
            cashflowPendingAccounts = this.checked ? null : [];
            renderCashflowAccountOptions();
        });
        element('cashflowsAccountOptions').addEventListener('change', function (event) {
            const code = event.target.dataset.cashflowAccount;
            if (!code) return;
            const chosen = new Set(cashflowEffectivePendingAccounts() || cashflowEligibleAccounts().map(function (account) { return account.account_code; }));
            if (event.target.checked) chosen.add(code); else chosen.delete(code);
            cashflowPendingAccounts = Array.from(chosen).sort();
            const configured = (cashflowAccountOptions || []).map(function (account) { return account.account_code; });
            if (cashflowAccountKey(cashflowPendingAccounts) === cashflowAccountKey(configured)) cashflowPendingAccounts = null;
            syncCashflowAccountButton();
        });
        element('cashflowsAccountClear').addEventListener('click', function () { cashflowPendingAccounts = []; renderCashflowAccountOptions(); });
        element('cashflowsAccountDone').addEventListener('click', function () { closeCashflowAccountPopover(true); });
        element('cashflowsAccountRetry').addEventListener('click', function () { loadCashflowAccountOptions(true); });
        document.addEventListener('click', function (event) { if (!event.target.closest('.cashflows-account-control')) closeCashflowAccountPopover(false); });
        popover.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') { event.preventDefault(); closeCashflowAccountPopover(true); }
            if (event.key === 'Tab') {
                const controls = Array.from(popover.querySelectorAll('input, button')).filter(function (node) { return !node.disabled && !node.hidden && node.getClientRects().length; });
                const first = controls[0], last = controls[controls.length-1];
                if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
                else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
            }
        });
    }
    let cashflowGroupingPreference = 'suggested';
    let stagedCashflowSelection = null;
    let activeCashflowPicker = null;
    let cashflowSelectedPeriods = [];
    let cashflowPickerView = null;
    let cashflowToastTimer = null;
    let cashflowFreshnessAlertTimer = null;
    let cashflowProcessingControls = [];
    let cashflowSummaryFilter = '';
    let hideCashflowZeroRows = true;
    let lastClassifiedTransactionsResult = null;
    let cashflowChildPreviews = {};
    let cashflowChildRequest = null;
    let cashflowChildGeneration = 0;
    let activeCashflowWindow = null;
    let cashflowDrilldownPage = 1;
    let cashflowDrilldownController = null;
    let cashflowDrilldownGeneration = 0;
    let lastCashflowDrilldownResult = null;
    let cashflowReportRevision = 0;
    let cashflowReportSourceKey = "";
    let cashflowConfigurationKey = '';
    let cashflowSummaryGeneration = 0;
    let cashflowSummaryController = null;
    let cashflowRevisionCheckPromise = null;
    let cashflowRevisionCheckedAt = 0;
    const CASHFLOW_REVISION_CHECK_TTL_MS = 15000;
    const cashflowSummaryCacheLimit = 5;
    const cashflowSummaryCache = new Map();
    let cashflowBalancesPending = false;
    let cashflowBalanceExpanded = { opening: false, closing: false };
    const cashflowDrilldownBaseCache = new Map();
    const cashflowLedgerCacheOrder = new Map();
    const CASHFLOW_LEDGER_CACHE_MAX_ROWS = 5000;
    const CASHFLOW_LEDGER_CACHE_MAX_BYTES = 2 * 1024 * 1024;
    const cashflowDrilldownLimit = 50;
    let cashflowInlineLedgerCache = new Map();
    let cashflowInlineLedgerControllers = new Map();
    let cashflowInlineLedgerGeneration = 0;
    const cashflowInlineLedgerLimit = 200;
    let cashflowInlineTransactionCache = new Map();
    const cashflowInlineTransactionLimit = 50;
    const cashflowInlineHtmlClose = '>';
    let cashflowInlineTransactionActiveController = null;
    let cashflowInlineTransactionRequestSerial = 0;
    let cashflowInlineTransactionContext = null;
    let cashflowInlineTransactionPage = 1;
    let cashflowInlineTransactionFocus = null;
    let cashflowInlineTransactionDrawer = null;
    const cashflowWindowSections = {
        transfers: 'cashflowsTransfersSection',
        customer_payments: 'cashflowsCustomerPaymentsSection',
        vendor_payments: 'cashflowsVendorPaymentsSection',
        deductions: 'cashflowsDeductionsSection',
        review: 'cashflowsReviewSection',
        transactions: 'cashflowsTransactionsSection',
    };

    function localDate(year, month, day) { return new Date(year, month, day, 12, 0, 0, 0); }
    function toIso(date) {
        return date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0') + '-' + String(date.getDate()).padStart(2, '0');
    }
    function fromIso(value) {
        const parts = String(value || '').split('-').map(Number);
        return parts.length === 3 && parts.every(function (part) { return Number.isFinite(part); }) ? localDate(parts[0], parts[1] - 1, parts[2]) : null;
    }
    function monthStart(date) { return localDate(date.getFullYear(), date.getMonth(), 1); }
    function monthEnd(date) { return localDate(date.getFullYear(), date.getMonth() + 1, 0); }
    function yearStart(date) { return localDate(date.getFullYear(), 0, 1); }
    function yearEnd(date) { return localDate(date.getFullYear(), 11, 31); }
    function formatMonth(date) { return date.toLocaleDateString(undefined, { month: 'short' }); }
    function formatRange(startValue, endValue) {
        const start = fromIso(startValue); const end = fromIso(endValue);
        if (!start || !end) return 'Select period';
        if (startValue === endValue) return start.getDate() + ' ' + formatMonth(start) + ' ' + start.getFullYear();
        if (start.getFullYear() === end.getFullYear() && start.getMonth() === 0 && start.getDate() === 1 && end.getMonth() === 11 && end.getDate() === 31) return String(start.getFullYear());
        if (start.getFullYear() === end.getFullYear() && start.getMonth() === end.getMonth()) return start.getDate() + '–' + end.getDate() + ' ' + formatMonth(start) + ' ' + start.getFullYear();
        if (start.getFullYear() === end.getFullYear()) return formatMonth(start) + '–' + formatMonth(end) + ' ' + start.getFullYear();
        return formatMonth(start) + ' ' + start.getFullYear() + '–' + formatMonth(end) + ' ' + end.getFullYear();
    }
    function periodButtonLabel(selection) { return formatRange(selection.start, selection.end); }
    function syncCashflowSelection() {
        element('cashflowsPeriodStart').value = cashflowSelection.start;
        element('cashflowsPeriodEnd').value = cashflowSelection.end;
        element('cashflowsPeriodButton').textContent = periodButtonLabel(cashflowSelection);
        element('cashflowsPeriodButton').title = cashflowSelection.start + ' to ' + cashflowSelection.end;
        element('cashflowsGroupingSelect').value = cashflowGroupingPreference;
        syncCashflowTreasuryView();
    }
    const cashflowTreasuryViewLabels = { combined: 'Cash and Bank Total', bank: 'Bank only', cash: 'Cash only', split: 'Cash and Bank individually' };
    function cashflowTreasuryViewLabel(value) { return cashflowTreasuryViewLabels[String(value || 'combined')] || cashflowTreasuryViewLabels.combined; }
    function cashflowDefaultTreasuryType() {
        if (cashflowLoadedScope().treasury_view === 'bank') return 'Bank';
        if (cashflowLoadedScope().treasury_view === 'cash') return 'Cash';
        return '';
    }
    function resetCashflowDrilldownFilters() {
        element('cashflowsDrilldownSearch').value = '';
        element('cashflowsDrilldownCategory').value = '';
        element('cashflowsDrilldownAccountHead').value = '';
        element('cashflowsDrilldownTreasury').value = cashflowDefaultTreasuryType();
        element('cashflowsDrilldownPeriod').value = '';
        renderCashflowDrilldownCategoryOptions([]);
        renderCashflowDrilldownAccountOptions([]);
    }
    function syncCashflowTreasuryView() {
        const select = element('cashflowsTreasuryViewSelect');
        if (select) select.value = cashflowTreasuryView;
    }
    function monthsInclusive(startValue, endValue) {
        const start = fromIso(startValue); const end = fromIso(endValue);
        return start && end ? ((end.getFullYear() - start.getFullYear()) * 12) + end.getMonth() - start.getMonth() + 1 : 0;
    }
    function autoCashflowGrouping(startValue, endValue) {
        const start = fromIso(startValue); const end = fromIso(endValue);
        if (!start || !end) return 'month';
        const days = Math.round((end - start) / 86400000) + 1;
        const months = monthsInclusive(startValue, endValue);
        if (days <= 31) return 'day';
        if (months <= 12) return 'month';
        if (months <= 36) return 'quarter';
        return 'year';
    }
    function preferredCashflowGrouping(suggestedGrouping) {
        return cashflowGroupingPreference === 'suggested' ? suggestedGrouping : cashflowGroupingPreference;
    }
    function groupingForPicked(type, startValue, endValue) {
        if (type === 'month' && monthsInclusive(startValue, endValue) === 1) return 'day';
        if (type === 'fortnight' && Math.round((fromIso(endValue) - fromIso(startValue)) / 86400000) + 1 <= 14) return 'day';
        if ((type === 'quarter' || type === 'half_year' || type === 'year') && monthsInclusive(startValue, endValue) <= (type === 'quarter' ? 3 : type === 'half_year' ? 6 : 12)) return 'month';
        return type;
    }
    function closeCashflowPopover() {
        const popover = element('cashflowsPeriodPopover');
        popover.hidden = true;
        element('cashflowsPeriodButton').setAttribute('aria-expanded', 'false');
        stagedCashflowSelection = null;
        activeCashflowPicker = null;
        cashflowSelectedPeriods = [];
    }
    function applyCashflowSelection(loadImmediately) {
        if (!stagedCashflowSelection || !stagedCashflowSelection.start || !stagedCashflowSelection.end) return;
        cashflowSelection = { start: stagedCashflowSelection.start, end: stagedCashflowSelection.end, groupBy: stagedCashflowSelection.groupBy };
        syncCashflowSelection();
        closeCashflowPopover();
        if (loadImmediately) generateCashflows();
    }
    function quickCashflowSelection(name) {
        const today = new Date(); const startYear = today.getFullYear();
        let start = yearStart(today); let end = today; let groupBy = 'month';
        if (name === 'this_month') { start = monthStart(today); groupBy = 'day'; }
        if (name === 'this_quarter') { start = localDate(startYear, Math.floor(today.getMonth() / 3) * 3, 1); groupBy = 'month'; }
        if (name === 'ytd') { start = yearStart(today); groupBy = 'month'; }
        if (name === 'this_year') { start = yearStart(today); end = yearEnd(today); groupBy = 'month'; }
        if (name === 'last_year') { start = localDate(startYear - 1, 0, 1); end = localDate(startYear - 1, 11, 31); groupBy = 'month'; }
        if (name === 'monthly') { start = yearStart(today); groupBy = 'month'; }
        stagedCashflowSelection = { start: toIso(start), end: toIso(end), groupBy: preferredCashflowGrouping(groupBy) };
        applyCashflowSelection(true);
    }
    function pickerPeriods(type, anchor) {
        const year = anchor.getFullYear(); const periods = [];
        if (type === 'month') for (let month = 0; month < 12; month += 1) periods.push({ start: localDate(year, month, 1), end: localDate(year, month + 1, 0), label: formatMonth(localDate(year, month, 1)) });
        if (type === 'quarter') for (let quarter = 0; quarter < 4; quarter += 1) periods.push({ start: localDate(year, quarter * 3, 1), end: localDate(year, quarter * 3 + 3, 0), label: 'Q' + (quarter + 1) });
        if (type === 'half_year') for (let half = 0; half < 2; half += 1) periods.push({ start: localDate(year, half * 6, 1), end: localDate(year, half * 6 + 6, 0), label: 'H' + (half + 1) });
        if (type === 'fortnight') for (let month = 0; month < 12; month += 1) { periods.push({ start: localDate(year, month, 1), end: localDate(year, month, 14), label: 'F1 ' + formatMonth(localDate(year, month, 1)) }); periods.push({ start: localDate(year, month, 15), end: localDate(year, month + 1, 0), label: 'F2 ' + formatMonth(localDate(year, month, 1)) }); }
        if (type === 'year') for (let offset = -5; offset < 7; offset += 1) periods.push({ start: localDate(year + offset, 0, 1), end: localDate(year + offset, 11, 31), label: String(year + offset) });
        return periods;
    }
    function pickerHeading(label) {
        return '<div class="cashflows-picker-heading"><button type="button" class="cashflows-picker-nav" data-picker-nav="prev" aria-label="Previous period">&lsaquo;</button><strong>' + escapeHtml(label) + '</strong><button type="button" class="cashflows-picker-nav" data-picker-nav="next" aria-label="Next period">&rsaquo;</button></div>';
    }
    function moveCashflowPicker(direction) {
        const current = fromIso(cashflowPickerView) || fromIso(stagedCashflowSelection.start) || new Date();
        if (activeCashflowPicker === 'day') {
            cashflowPickerView = toIso(localDate(current.getFullYear(), current.getMonth() + direction, 1));
        } else {
            cashflowPickerView = toIso(localDate(current.getFullYear() + direction, current.getMonth(), 1));
        }
        renderCashflowPeriodPicker(activeCashflowPicker);
    }
    function cashflowPeriodKey(startValue, endValue) { return startValue + '|' + endValue; }
    function isCashflowPeriodSelected(startValue, endValue) {
        const key = cashflowPeriodKey(startValue, endValue);
        return cashflowSelectedPeriods.some(function (period) { return period.key === key; });
    }
    function updateStagedCashflowSelection(type) {
        if (!cashflowSelectedPeriods.length) {
            stagedCashflowSelection = { start: '', end: '', groupBy: type };
            return;
        }
        const starts = cashflowSelectedPeriods.map(function (period) { return fromIso(period.start); });
        const ends = cashflowSelectedPeriods.map(function (period) { return fromIso(period.end); });
        const first = new Date(Math.min.apply(null, starts));
        const last = new Date(Math.max.apply(null, ends));
        const startValue = toIso(first); const endValue = toIso(last);
        stagedCashflowSelection = { start: startValue, end: endValue, groupBy: preferredCashflowGrouping(groupingForPicked(type, startValue, endValue)) };
    }
    function chooseCashflowPeriod(start, end, type) {
        const startValue = toIso(start); const endValue = toIso(end);
        const key = cashflowPeriodKey(startValue, endValue);
        const selectedIndex = cashflowSelectedPeriods.findIndex(function (period) { return period.key === key; });
        if (selectedIndex >= 0) {
            cashflowSelectedPeriods.splice(selectedIndex, 1);
        } else {
            cashflowSelectedPeriods.push({ key: key, start: startValue, end: endValue });
        }
        updateStagedCashflowSelection(type);
        cashflowPickerView = startValue;
        renderCashflowPeriodPicker(type);
    }
    function renderCashflowPeriodPicker(type) {
        const picker = element('cashflowsPeriodPicker');
        const footer = element('cashflowsPeriodFooter');
        picker.hidden = false;
        footer.hidden = false;
        if (type === 'custom') {
            picker.innerHTML = '<div class="cashflows-custom-range"><label>From <input type="date" data-picker-input="start" value="' + escapeHtml(stagedCashflowSelection.start) + '"></label><label>To <input type="date" data-picker-input="end" value="' + escapeHtml(stagedCashflowSelection.end) + '"></label></div>';
            return;
        }
        if (type === 'day') {
            const cursor = fromIso(cashflowPickerView) || fromIso(stagedCashflowSelection.start) || new Date();
            const first = monthStart(cursor); const last = monthEnd(cursor); const days = [];
            for (let day = 1; day <= last.getDate(); day += 1) {
                const date = localDate(cursor.getFullYear(), cursor.getMonth(), day);
                const dateValue = toIso(date); const selected = isCashflowPeriodSelected(dateValue, dateValue);
                days.push('<button type="button" class="cashflows-picker-cell' + (selected ? ' is-selected' : '') + '" aria-pressed="' + String(selected) + '" data-picker-start="' + dateValue + '" data-picker-end="' + dateValue + '">' + day + '</button>');
            }
            picker.innerHTML = pickerHeading(cursor.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })) + '<div class="cashflows-day-grid">' + days.join('') + '</div>';
            return;
        }
        const anchor = fromIso(cashflowPickerView) || fromIso(stagedCashflowSelection.start) || new Date();
        const cells = pickerPeriods(type, anchor).map(function (period) {
            const startValue = toIso(period.start); const endValue = toIso(period.end);
            const selected = isCashflowPeriodSelected(startValue, endValue);
            return '<button type="button" class="cashflows-picker-cell' + (selected ? ' is-selected' : '') + '" aria-pressed="' + String(selected) + '" data-picker-start="' + startValue + '" data-picker-end="' + endValue + '">' + escapeHtml(period.label) + '</button>';
        }).join('');
        picker.innerHTML = pickerHeading(anchor.getFullYear()) + '<div class="cashflows-period-grid">' + cells + '</div>';
    }
    function openCashflowPopover() {
        stagedCashflowSelection = { start: cashflowSelection.start, end: cashflowSelection.end, groupBy: cashflowSelection.groupBy };
        activeCashflowPicker = null;
        cashflowSelectedPeriods = [];
        cashflowPickerView = cashflowSelection.start;
        element('cashflowsPeriodPicker').hidden = true;
        element('cashflowsPeriodFooter').hidden = true;
        const popover = element('cashflowsPeriodPopover');
        popover.hidden = false;
        element('cashflowsPeriodButton').setAttribute('aria-expanded', 'true');
    }
    function handleCashflowPickerChange(event) {
        const target = event.target;
        if (target.matches('[data-picker-nav]')) {
            moveCashflowPicker(target.dataset.pickerNav === 'next' ? 1 : -1);
            return;
        }
        if (target.matches('[data-picker-input="start"]') || target.matches('[data-picker-input="end"]')) {
            stagedCashflowSelection.start = element('cashflowsPeriodPicker').querySelector('[data-picker-input="start"]').value;
            stagedCashflowSelection.end = element('cashflowsPeriodPicker').querySelector('[data-picker-input="end"]').value;
            stagedCashflowSelection.groupBy = preferredCashflowGrouping(autoCashflowGrouping(stagedCashflowSelection.start, stagedCashflowSelection.end));
        }
        if (target.matches('[data-picker-start]')) chooseCashflowPeriod(fromIso(target.dataset.pickerStart), fromIso(target.dataset.pickerEnd), activeCashflowPicker);
    }

    function element(id) { return document.getElementById(id); }
    function escapeHtml(value) {
        return String(value == null ? '' : value).replace(/[&<>'"]/g, function (character) {
            return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character];
        });
    }
    function formatInternationalAmount(value) {
        const amount = Number(value || 0);
        if (amount === 0) return '-';
        return amount.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    }
    function formatCashflowSectionAmount(value, section) {
        const amount = Number(value || 0);
        if (amount === 0) return '-';
        const label = Math.abs(amount).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
        const oppositeMovement = section === 'Receipts' ? amount < 0 : amount > 0;
        return oppositeMovement ? '(' + label + ')' : label;
    }

    function formatLocalAmount(value) {
        const amount = Number(value || 0);
        if (amount === 0) return '-';
        const absolute = Math.abs(amount);
        let divisor = 1;
        let unit = 'Rs';
        if (absolute >= 10000000) { divisor = 10000000; unit = 'crore'; }
        else if (absolute >= 100000) { divisor = 100000; unit = 'lakh'; }
        else if (absolute >= 1000) { divisor = 1000; unit = 'thousand'; }
        const label = (absolute / divisor).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 }) + ' ' + unit;
        return amount < 0 ? '(' + label + ')' : label;
    }
    function formatPercentage(value) {
        return (Number(value || 0) * 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + '%';
    }
    async function api(url, options) {
        const response = await fetch(url, options);
        const payload = await response.json().catch(function () { return {}; });
        if (!response.ok) throw new Error(payload.detail || payload.message || 'The Cashflows report could not be loaded.');
        return payload;
    }
    function showCashflowToast(message, isError) {
        const toast = element('cashflowsToast');
        if (!toast) return;
        if (cashflowToastTimer) window.clearTimeout(cashflowToastTimer);
        toast.textContent = message || '';
        toast.className = 'cashflows-toast' + (isError ? ' is-error' : '');
        toast.hidden = !message;
        if (message) {
            cashflowToastTimer = window.setTimeout(function () {
                toast.hidden = true;
                toast.textContent = '';
            }, 4200);
        }
    }
    function setCashflowProcessingStep(step, message) {
        const order = { prepare: 0, load: 1, render: 2 };
        const currentIndex = order[step] == null ? 0 : order[step];
        document.querySelectorAll('[data-processing-step]').forEach(function (item) {
            const itemIndex = order[item.dataset.processingStep];
            item.classList.toggle('is-complete', itemIndex < currentIndex);
            item.classList.toggle('is-current', item.dataset.processingStep === step);
            if (item.dataset.processingStep === step) item.setAttribute('aria-current', 'step');
            else item.removeAttribute('aria-current');
        });
        element('cashflowsProcessingMessage').textContent = message || '';
    }
    function showCashflowProcessing(title, message) {
        cashflowProcessingControls = Array.from(document.querySelectorAll('button, select, input, textarea')).map(function (control) {
            return { element: control, disabled: control.disabled };
        });
        cashflowProcessingControls.forEach(function (item) { item.element.disabled = true; });
        element('cashflowsView').setAttribute('aria-busy', 'true');
        document.body.classList.add('cashflows-processing-open');
        element('cashflowsProcessing').hidden = false;
        element('cashflowsProcessingTitle').textContent = title || 'Loading Cashflows summary';
        setCashflowProcessingStep('prepare', message || 'Preparing the selected period.');
    }
    function hideCashflowProcessing() {
        cashflowProcessingControls.forEach(function (item) { item.element.disabled = item.disabled; });
        cashflowProcessingControls = [];
        element('cashflowsView').removeAttribute('aria-busy');
        document.body.classList.remove('cashflows-processing-open');
        element('cashflowsProcessing').hidden = true;
        element('cashflowsProcessingTitle').textContent = 'Loading Cashflows summary';
        syncCashflowZeroRowsButton();
        syncCashflowReconciliationEntry();
    }
    function cashflowPreviewPeriods(preview) {
        if (Array.isArray(preview.periods) && preview.periods.length) return preview.periods;
        return (Array.isArray(preview.months) ? preview.months : []).map(function (month) { return { key: month, label: month }; });
    }
    function syncCashflowZeroRowsButton() {
        const button = element('cashflowsHideZeroRowsButton');
        if (!button) return;
        const preview = (lastCashflowsResult || {}).preview || {};
        button.disabled = !cashflowPreviewPeriods(preview).length;
        button.textContent = hideCashflowZeroRows ? 'Show zero rows' : 'Hide zero rows';
    }
    function cashflowRowsWithNonZeroAmounts(columns, rows) {
        if (!hideCashflowZeroRows) return rows;
        const amountColumns = columns.filter(function (column) { return column.amount; });
        if (!amountColumns.length) return rows;
        return rows.filter(function (row) {
            return amountColumns.some(function (column) {
                const value = Number(row[column.key]);
                return Number.isFinite(value) && Math.abs(value) > 0;
            });
        });
    }
    const cashflowDetailSearchInputs = {
        cashflowsTransfersTable: 'cashflowsTransfersSearch',
        cashflowsCustomerPaymentsTable: 'cashflowsCustomerPaymentsSearch',
        cashflowsVendorPaymentsTable: 'cashflowsVendorPaymentsSearch',
        cashflowsDeductionsTable: 'cashflowsDeductionsSearch',
        cashflowsReviewTable: 'cashflowsReviewSearch',
    };
    function filterCashflowDetailRows(table, columns, rows) {
        const searchInputId = cashflowDetailSearchInputs[table.id];
        const searchInput = searchInputId ? element(searchInputId) : null;
        const query = searchInput ? searchInput.value.trim().toLowerCase() : '';
        if (!query) return rows;
        const terms = query.split(/\s+/).filter(Boolean);
        return rows.filter(function (row) {
            return terms.every(function (term) {
                return columns.some(function (column) {
                    const value = row[column.key];
                    const rawValue = value == null ? '' : String(value);
                    const formattedValue = column.amount ? String(formatInternationalAmount(value)) : '';
                    return (rawValue + ' ' + formattedValue).toLowerCase().includes(term);
                });
            });
        });
    }
    function formatCashflowDetailCount(tableId, totalCount, unit) {
        const table = element(tableId);
        const searchInputId = cashflowDetailSearchInputs[tableId];
        const searchInput = searchInputId ? element(searchInputId) : null;
        const query = searchInput ? searchInput.value.trim() : '';
        if (!query) return String(totalCount) + ' ' + unit;
        const matchCount = table ? Number(table.dataset.cashflowSearchMatchCount || 0) : 0;
        return String(matchCount) + ' of ' + String(totalCount) + ' ' + unit;
    }
    function renderTable(table, columns, rows, emptyMessage) {
        const searchedRows = filterCashflowDetailRows(table, columns, rows);
        const visibleRows = cashflowRowsWithNonZeroAmounts(columns, searchedRows);
        table.dataset.cashflowSearchMatchCount = String(searchedRows.length);
        table.dataset.cashflowSearchActive = searchedRows.length !== rows.length ? 'true' : 'false';
        if (!visibleRows.length) {
            table.innerHTML = '<tbody><tr><td class="cashflows-empty" colspan="' + Math.max(columns.length, 1) + '">' + escapeHtml(emptyMessage) + '</td></tr></tbody>';
            return;
        }
        table.innerHTML = '<thead><tr>' + columns.map(function (column) { return '<th class="' + (column.amount ? 'cashflows-table-number' : 'cashflows-table-text') + '">' + escapeHtml(column.label) + '</th>'; }).join('') + '</tr></thead><tbody>' + visibleRows.map(function (row) {
            return '<tr>' + columns.map(function (column) {
                const value = row[column.key];
                return '<td' + (column.accountCode ? ' title="' + escapeHtml(row[column.accountCode] || '') + '"' : '') + ' class="' + (column.amount ? 'cashflows-table-number' : 'cashflows-table-text') + '">' + escapeHtml(column.amount ? (value == null ? '—' : formatInternationalAmount(value)) : (value == null ? '' : value)) + '</td>';
            }).join('') + '</tr>';
        }).join('') + '</tbody>';
    }
    function formatCashflowTransferParty(row, prefix) {
        const code = String(row[prefix + '_account_code'] || '').trim();
        const name = String(row[prefix + '_account_name'] || '').trim();
        const type = String(row[prefix + '_type'] || '').trim();
        const account = name && code && name !== code ? name + ' (' + code + ')' : (name || code);
        return [account, type].filter(Boolean).join(' · ') || '—';
    }
    function formatCashflowTransferDate(value) {
        const text = String(value || '');
        return text.includes('T') ? text.slice(0, 10) : text;
    }
    function renderCategoryOptions(values, replaceExisting) {
        const datalist = element('cashflowsTransactionsCategoryOptions');
        const input = element('cashflowsTransactionsCategory');
        if (!datalist || !input) return;
        const categories = new Set(replaceExisting ? ['Unassigned'] : Array.from(datalist.options || []).map(function (option) { return option.value; }));
        (values || []).forEach(function (value) {
            const category = typeof value === 'string' ? value : (value && value.category);
            if (category) categories.add(String(category));
        });
        if (input.value.trim()) categories.add(input.value.trim());
        datalist.innerHTML = Array.from(categories).sort(function (left, right) { return left.localeCompare(right); }).map(function (category) {
            return '<option value="' + escapeHtml(category) + '"></option>';
        }).join('');
    }
    function syncCashflowDrilldownShortcuts() {
        const childMappings = {
            transfers: ['cashflowsLoadTransfersButton', 'cashflowsDrilldownLoadTransfersButton'],
            customer_payments: ['cashflowsLoadCustomerPaymentsButton', 'cashflowsDrilldownLoadCustomerPaymentsButton'],
            deductions: ['cashflowsLoadDeductionsButton', 'cashflowsDrilldownLoadDeductionsButton'],
            review: ['cashflowsLoadReviewButton', 'cashflowsDrilldownLoadReviewButton'],
        };
        Object.keys(childMappings).forEach(function (name) {
            const source = element(childMappings[name][0]);
            const shortcut = element(childMappings[name][1]);
            if (!source || !shortcut) return;
            shortcut.disabled = source.disabled;
            shortcut.hidden = source.hidden;
            shortcut.textContent = source.textContent;
        });
        const source = element('cashflowsViewTransactionsButton');
        const shortcut = element('cashflowsDrilldownViewTransactionsButton');
        if (source && shortcut) {
            shortcut.disabled = source.disabled;
            shortcut.hidden = source.hidden;
            shortcut.textContent = source.textContent;
        }
    }
    function resetLazyCashflowSections() {
        cashflowChildGeneration += 1;
        cashflowChildRequest = null;
        setActiveCashflowWindow(null);
        ['cashflowsLoadTransfersButton', 'cashflowsLoadCustomerPaymentsButton', 'cashflowsLoadVendorPaymentsButton', 'cashflowsLoadDeductionsButton', 'cashflowsLoadReviewButton'].forEach(function (id) {
            element(id).disabled = false;
        });
        cashflowChildPreviews = {};
        ['cashflowsTransfersSearch', 'cashflowsCustomerPaymentsSearch', 'cashflowsVendorPaymentsSearch', 'cashflowsDeductionsSearch', 'cashflowsReviewSearch'].forEach(function (id) {
            const input = element(id);
            if (input) input.value = '';
        });
        const customerSearch = element('cashflowsCustomerPaymentsCustomerSearch');
        if (customerSearch) { customerSearch.value = ''; customerSearch.hidden = false; }
        const customerWisePanel = element('cashflowsCustomerWisePanel');
        if (customerWisePanel) customerWisePanel.hidden = true;
        const customerPeriodWisePanel = element('cashflowsCustomerPeriodWisePanel');
        if (customerPeriodWisePanel) customerPeriodWisePanel.hidden = true;
        const customerPaymentsVoucherPanel = element('cashflowsCustomerPaymentsVoucherPanel');
        if (customerPaymentsVoucherPanel) customerPaymentsVoucherPanel.hidden = false;
        const customerPaymentsHeading = element('cashflowsCustomerPaymentsHeading');
        if (customerPaymentsHeading) customerPaymentsHeading.textContent = 'Customer payments';
        syncCashflowDrilldownShortcuts();
    }
    function setActiveCashflowWindow(windowKey) {
        activeCashflowWindow = windowKey;
        Object.keys(cashflowWindowSections).forEach(function (key) {
            element(cashflowWindowSections[key]).hidden = key !== windowKey;
        });
    }
    function renderCashflowTransferReconciliation(preview) {
        const panel = element('cashflowsTransferReconciliation');
        const reconciliation = preview && preview.transfer_reconciliation;
        if (!panel || !reconciliation || !reconciliation.status || reconciliation.status === 'No transfers') {
            if (panel) {
                panel.hidden = true;
                panel.innerHTML = '';
            }
            return;
        }
        const status = String(reconciliation.status);
        const statusClass = status === 'Matched' ? 'is-matched' : 'is-review';
        const metric = function (label, value) {
            return '<div class="cashflows-transfer-metric"><span>' + escapeHtml(label) + '</span><strong>' + escapeHtml(value) + '</strong></div>';
        };
        panel.innerHTML = '<div class="cashflows-transfer-reconciliation-heading"><strong>Source reconciliation</strong><span class="cashflows-transfer-status ' + statusClass + '">' + escapeHtml(status) + '</span></div>'
            + '<div class="cashflows-transfer-metrics">'
            + metric('Candidate vouchers', String(reconciliation.candidate_voucher_count || 0))
            + metric('Confirmed', String(reconciliation.confirmed_count || 0))
            + metric('Mixed', String(reconciliation.mixed_count || 0))
            + metric('Ambiguous', String(reconciliation.ambiguous_count || 0))
            + metric('Transferor total', formatInternationalAmount(reconciliation.transferor_total))
            + metric('Transferee total', formatInternationalAmount(reconciliation.transferee_total))
            + metric('Variance', formatInternationalAmount(reconciliation.variance_total))
            + '</div>';
        if (preview.account_selection) panel.innerHTML += '<p>Complete voucher context. Selected transfer receipts: '
            + escapeHtml(formatInternationalAmount(reconciliation.selected_inflow)) + '; selected transfer payments: '
            + escapeHtml(formatInternationalAmount(reconciliation.selected_outflow)) + '.</p>';
        panel.hidden = false;
    }
    function renderCashflowCustomerWiseOptions(preview) {
        const datalist = element('cashflowsCustomerPaymentsCustomerOptions');
        if (!datalist) return;
        const options = Array.isArray(preview && preview.customer_payment_customer_options)
            ? preview.customer_payment_customer_options
            : [];
        datalist.innerHTML = options.map(function (option) {
            const name = String(option.account_name || '').trim();
            const code = String(option.account_code || '').trim();
            const value = name || code;
            return value ? '<option value="' + escapeHtml(value) + '" label="' + escapeHtml(code) + '"></option>' : '';
        }).join('');
    }
    function customerWiseRowMatches(row, query) {
        if (!query) return true;
        return [row.customer_account_code, row.customer_account_name].some(function (value) {
            return String(value || '').toLowerCase().includes(query);
        });
    }
    function renderCashflowCustomerWise(preview) {
        const panel = element('cashflowsCustomerWisePanel');
        const table = element('cashflowsCustomerWiseTable');
        const input = element('cashflowsCustomerPaymentsCustomerSearch');
        if (!panel || !table) return;
        renderCashflowCustomerWiseOptions(preview);
        const query = input ? input.value.trim().toLowerCase() : '';
        const allRows = Array.isArray(preview && preview.customer_payment_customer_totals)
            ? preview.customer_payment_customer_totals
            : [];
        const rows = allRows.filter(function (row) { return customerWiseRowMatches(row, query); });
        renderTable(table, [
            { key: 'customer_account_name', label: 'Customer', accountCode: 'customer_account_code' },
            { key: 'customer_receipts', label: 'Receipts', amount: true },
            { key: 'customer_reversals', label: 'Reversals / refunds', amount: true },
            { key: 'customer_gross_amount', label: 'Net receipts', amount: true },
            { key: 'bank_charges', label: 'Bank charges', amount: true },
            { key: 'withholding_tax', label: 'WHT', amount: true },
            { key: 'other_deductions', label: 'Other deductions', amount: true },
            { key: 'deductions_total', label: 'Total deductions', amount: true },
            { key: 'technical_bank_offset', label: 'Technical bank offset', amount: true },
            { key: 'expected_net_settlement', label: 'Expected settlement', amount: true },
            { key: 'net_bank_settlement', label: 'Actual settlement', amount: true },
            { key: 'cashflow_amount', label: 'Cashflow amount', amount: true },
            { key: 'variance', label: 'Difference', amount: true },
            { key: 'status', label: 'Status' },
        ], rows, 'No customer-wise payments match the selected customer.');
        const reconciliation = preview.customer_payment_reconciliation || {};
        const status = String(reconciliation.status || 'Review required');
        const statusElement = element('cashflowsCustomerWiseStatus');
        if (statusElement) {
            statusElement.textContent = status;
            statusElement.className = 'cashflows-transfer-status ' + (status === 'Matched' ? 'is-matched' : 'is-review');
        }
        const metric = function (label, value) {
            return '<div class="cashflows-transfer-metric"><span>' + escapeHtml(label) + '</span><strong>' + escapeHtml(value) + '</strong></div>';
        };
        const reconciliationPanel = element('cashflowsCustomerWiseReconciliation');
        if (reconciliationPanel) {
            reconciliationPanel.innerHTML = metric('Customer-wise total', formatInternationalAmount(reconciliation.statement_eligible_customer_total))
                + metric('Received from customers', formatInternationalAmount(reconciliation.main_received_from_customers_total))
                + metric('Difference', formatInternationalAmount(reconciliation.difference));
        }
        const voucherPanel = element('cashflowsCustomerPaymentsVoucherPanel');
        const periodPanel = element('cashflowsCustomerPeriodWisePanel');
        const customerSearch = element('cashflowsCustomerPaymentsCustomerSearch');
        if (voucherPanel) voucherPanel.hidden = true;
        if (periodPanel) periodPanel.hidden = true;
        if (customerSearch) customerSearch.hidden = false;
        const heading = element('cashflowsCustomerPaymentsHeading');
        if (heading) heading.textContent = 'Customer-wise payments';
        const count = element('cashflowsCustomerPaymentCount');
        if (count) count.textContent = String(rows.length) + ' customer(s)';
        panel.hidden = false;
    }
    function renderCashflowCustomerPeriodWise(preview) {
        const panel = element('cashflowsCustomerPeriodWisePanel');
        const table = element('cashflowsCustomerPeriodWiseTable');
        if (!panel || !table) return;
        const unfiltered = Array.isArray(preview && preview.customer_payment_account_period_totals)
            ? preview.customer_payment_account_period_totals
            : [];
        const query = (element('cashflowsCustomerPaymentsCustomerSearch').value || '').trim().toLowerCase();
        const rows = unfiltered.filter(function (row) { return customerWiseRowMatches(row, query); });
        renderTable(table, [
            { key: 'period_label', label: 'Period' },
            { key: 'customer_account_name', label: 'Customer', accountCode: 'customer_account_code' },
            { key: 'customer_receipts', label: 'Receipts', amount: true },
            { key: 'customer_reversals', label: 'Reversals / refunds', amount: true },
            { key: 'customer_gross_amount', label: 'Net receipts', amount: true },
            { key: 'bank_charges', label: 'Bank charges', amount: true },
            { key: 'withholding_tax', label: 'WHT', amount: true },
            { key: 'other_deductions', label: 'Other deductions', amount: true },
            { key: 'deductions_total', label: 'Total deductions', amount: true },
            { key: 'technical_bank_offset', label: 'Technical bank offset', amount: true },
            { key: 'expected_net_settlement', label: 'Expected settlement', amount: true },
            { key: 'net_bank_settlement', label: 'Actual settlement', amount: true },
            { key: 'cashflow_amount', label: 'Cashflow amount', amount: true },
            { key: 'variance', label: 'Difference', amount: true },
            { key: 'status', label: 'Status' },
        ], rows, 'No customer payments are available for the selected periods.');
        const status = rows.length && rows.every(function (row) { return row.status === 'Matched'; }) ? 'Matched' : 'Review required';
        const statusElement = element('cashflowsCustomerPeriodWiseStatus');
        if (statusElement) {
            statusElement.textContent = status;
            statusElement.className = 'cashflows-transfer-status ' + (status === 'Matched' ? 'is-matched' : 'is-review');
        }
        const reconciliation = preview.customer_payment_reconciliation || {};
        const metric = function (label, value) {
            return '<div class="cashflows-transfer-metric"><span>' + escapeHtml(label) + '</span><strong>' + escapeHtml(value) + '</strong></div>';
        };
        const reconciliationPanel = element('cashflowsCustomerPeriodWiseReconciliation');
        if (reconciliationPanel) {
            reconciliationPanel.innerHTML = metric('Period-wise eligible total', formatInternationalAmount(reconciliation.statement_eligible_customer_total))
                + metric('Received from customers', formatInternationalAmount(reconciliation.main_received_from_customers_total))
                + metric('Difference', formatInternationalAmount(reconciliation.difference));
        }
        const voucherPanel = element('cashflowsCustomerPaymentsVoucherPanel');
        const customerPanel = element('cashflowsCustomerWisePanel');
        const customerSearch = element('cashflowsCustomerPaymentsCustomerSearch');
        if (voucherPanel) voucherPanel.hidden = true;
        if (customerPanel) customerPanel.hidden = true;
        if (customerSearch) customerSearch.hidden = false;
        const heading = element('cashflowsCustomerPaymentsHeading');
        if (heading) heading.textContent = 'Period-wise customer payments';
        const count = element('cashflowsCustomerPaymentCount');
        if (count) count.textContent = String(rows.length) + ' period(s)';
        panel.hidden = false;
    }
    function renderCashflowVendorPayments(preview) {
        const mode = element('cashflowsVendorView').value;
        const key = mode === 'transactions' ? 'vendor_payment_details' : (mode === 'vendor' ? 'vendor_payment_vendor_totals' : 'vendor_payment_period_totals');
        const rows = (preview[key] || []).map(function (row) {
            return Object.assign({}, row, { date_display: formatCashflowTransferDate(row.date) });
        });
        const columns = [];
        if (mode === 'transactions') columns.push({ key: 'date_display', label: 'Date' }, { key: 'voucher_id', label: 'Voucher' });
        if (mode === 'period') columns.push({ key: 'period_label', label: 'Period' });
        columns.push({ key: 'account_name', label: 'Vendor', accountCode: 'account_code' },
            { key: 'category', label: 'Cashflow category' },
            { key: 'payments', label: 'Payments', amount: true },
            { key: 'reversals', label: 'Reversals / refunds', amount: true },
            { key: 'net_payments', label: 'Net payments', amount: true });
        if (mode === 'transactions') columns.push({ key: 'voucher_detail', label: 'Description' });
        renderTable(element('cashflowsVendorPaymentsTable'), columns, rows, 'No matching vendor movements. Check vendor account settings if accounts are missing.');
        const options = preview.vendor_payment_vendor_totals || [];
        element('cashflowsVendorOptions').innerHTML = options.map(function (row) {
            return '<option value="' + escapeHtml(row.account_name) + '" label="' + escapeHtml(row.account_code) + '"></option>';
        }).join('');
        element('cashflowsVendorPaymentCount').textContent = formatCashflowDetailCount('cashflowsVendorPaymentsTable', rows.length, 'row(s)');
    }
    function renderCashflowChild(section, preview) {
        if (section === 'vendor_payments') {
            renderCashflowVendorPayments(preview);
        } else if (section === 'transfers') {
            const scoped = !!preview.account_selection;
            renderCashflowTransferReconciliation(preview);
            const rows = (preview.transfer_details || []).map(function (row) {
                return Object.assign({}, row, {
                    date_display: formatCashflowTransferDate(row.date || row.period_label || row.month),
                    transferor: formatCashflowTransferParty(row, 'transferor'),
                    transferee: formatCashflowTransferParty(row, 'transferee'),
                    status_display: row.status || (row.matched ? 'Confirmed' : 'Review required'),
                });
            });
            renderTable(element('cashflowsTransfersTable'), [
                { key: 'date_display', label: 'Date' }, { key: 'voucher_id', label: 'Voucher' }, { key: 'transferor', label: 'Transferor' }, { key: 'transferee', label: 'Transferee' }, { key: 'amount', label: scoped ? 'Voucher amount (context)' : 'Amount', amount: true }, { key: 'variance', label: 'Variance', amount: true }, { key: 'status_display', label: 'Status' }, { key: 'voucher_detail', label: 'Description' },
                { key: 'bank_movement', label: scoped ? 'Bank movement (context)' : 'Signed Bank movement', amount: true }, { key: 'cash_movement', label: scoped ? 'Cash movement (context)' : 'Signed Cash movement', amount: true },
            ].concat(scoped ? [{key: 'selection_inflow', label: 'Selected transfer receipts', amount: true},
                {key: 'selection_outflow', label: 'Selected transfer payments', amount: true},
                {key: 'selection_net_movement', label: 'Selected transfer net', amount: true}] : []), rows, 'No internal transfers identified.');
            element('cashflowsTransferCount').textContent = formatCashflowDetailCount('cashflowsTransfersTable', preview.internal_transfer_count || rows.length || 0, 'voucher(s)');
            if (activeCashflowWindow === section) element('cashflowsTransfersSection').hidden = false;
        } else if (section === 'customer_payments') {
            const customerWisePanel = element('cashflowsCustomerWisePanel');
            const customerPeriodWisePanel = element('cashflowsCustomerPeriodWisePanel');
            const customerPaymentsVoucherPanel = element('cashflowsCustomerPaymentsVoucherPanel');
            const customerPaymentsHeading = element('cashflowsCustomerPaymentsHeading');
            const customerWiseActive = !!customerWisePanel && !customerWisePanel.hidden;
            const customerPeriodWiseActive = !!customerPeriodWisePanel && !customerPeriodWisePanel.hidden;
            if (customerPaymentsVoucherPanel) customerPaymentsVoucherPanel.hidden = customerWiseActive || customerPeriodWiseActive;
            if (customerPaymentsHeading) customerPaymentsHeading.textContent = customerPeriodWiseActive ? 'Period-wise customer payments' : (customerWiseActive ? 'Customer-wise payments' : 'Customer payments');
            const rows = (preview.customer_payment_details || []).map(function (row) {
                return Object.assign({}, row, {
                    date_display: formatCashflowTransferDate(row.date || row.period_label || row.month),
                    status_display: row.status || 'Review required',
                });
            });
            renderTable(element('cashflowsCustomerPaymentsTable'), [
                { key: 'date_display', label: 'Date' },
                { key: 'voucher_id', label: 'Voucher' },
                { key: 'customer_account_name', label: 'Customer', accountCode: 'customer_account_code' },
                { key: 'bank_account_name', label: 'Receiving account' },
                { key: 'method', label: 'Method' },
                { key: 'customer_receipts', label: 'Receipts', amount: true },
            { key: 'customer_reversals', label: 'Reversals / refunds', amount: true },
            { key: 'customer_gross_amount', label: 'Net receipts', amount: true },
                { key: 'bank_charges', label: 'Bank charges', amount: true },
                { key: 'withholding_tax', label: 'WHT', amount: true },
                { key: 'other_deductions', label: 'Other deductions', amount: true },
                { key: 'deductions_total', label: 'Total deductions', amount: true },
                { key: 'technical_bank_offset', label: 'Technical bank offset', amount: true },
                { key: 'expected_net_settlement', label: 'Expected settlement', amount: true },
                { key: 'net_bank_settlement', label: 'Actual settlement', amount: true },
                { key: 'variance', label: 'Difference', amount: true },
                { key: 'status_display', label: 'Status' },
                { key: 'voucher_detail', label: 'Description' },
            ], rows, 'No customer payments identified.');
            if (!customerWiseActive && !customerPeriodWiseActive) element('cashflowsCustomerPaymentCount').textContent = formatCashflowDetailCount('cashflowsCustomerPaymentsTable', rows.length, 'payment(s)');
            if (activeCashflowWindow === section) element('cashflowsCustomerPaymentsSection').hidden = false;
        } else if (section === 'deductions') {
            const rows = preview.deduction_details || [];
            renderTable(element('cashflowsDeductionsTable'), [
                { key: 'month', label: 'Month' }, { key: 'category', label: 'Category' }, { key: 'account_name', label: 'Account' }, { key: 'amount', label: 'Amount', amount: true },
            ], rows, 'No customer deductions identified.');
            element('cashflowsDeductionCount').textContent = formatCashflowDetailCount('cashflowsDeductionsTable', preview.deduction_count || rows.length || 0, 'line(s)');
            if (activeCashflowWindow === section) element('cashflowsDeductionsSection').hidden = false;
        } else if (section === 'review') {
            const rows = (preview.categories_needing_review || []).concat((preview.selection_review_details || []).map(function (row) {
                return Object.assign({}, row, {period: row.period_key, fallback_category: 'Selected-account allocation review'});
            }));
            renderTable(element('cashflowsReviewTable'), [
                { key: 'voucher_id', label: 'Voucher' }, { key: 'account_code', label: 'Account code' }, { key: 'account_name', label: 'Account' }, { key: 'fallback_category', label: 'Review category' },
            ].concat(preview.account_selection ? [{key: 'reason', label: 'Allocation review reason'},
                {key: 'receipts', label: 'Selected receipts under review', amount: true},
                {key: 'payments', label: 'Selected payments under review', amount: true}] : []), rows, 'No review items.');
            element('cashflowsReviewCount').textContent = formatCashflowDetailCount('cashflowsReviewTable', rows.length, 'review item(s)');
            if (activeCashflowWindow === section) element('cashflowsReviewSection').hidden = false;
        }
    }
    function setCashflowDrilldownMode(open) {
        const section = element('cashflowsLedgerDrilldownSection');
        const summaryControlsPanel = element('cashflowsSummaryControlsPanel');
        if (!section) return;
        if (summaryControlsPanel) summaryControlsPanel.hidden = open;
        moveCashflowDetailResults(open);
        const summaryTable = element('cashflowsSummaryTable');
        const summarySection = summaryTable ? summaryTable.closest('.cashflows-report-section') : null;
        if (summarySection) summarySection.hidden = open;
        const reportIds = ['cashflowsReportForm', 'cashflowsSummaryTable', 'cashflowsLazyActions', 'cashflowsTransfersSection', 'cashflowsCustomerPaymentsSection', 'cashflowsVendorPaymentsSection', 'cashflowsDeductionsSection', 'cashflowsReviewSection', 'cashflowsTransactionsSection'];
        reportIds.forEach(function (id) {
            const target = element(id);
            if (target) target.hidden = open;
        });
        section.hidden = !open;
        activeCashflowWindow = open ? 'ledger_drilldown' : null;
        if (!open) setActiveCashflowWindow(null);
    }
    function moveCashflowDetailResults(open) {
        const detailResults = element('cashflowsDetailResults');
        const anchor = element('cashflowsMainDetailAnchor');
        const drilldownHost = element('cashflowsDrilldownDetailHost');
        if (!detailResults || !anchor || !drilldownHost) return;
        if (open) {
            drilldownHost.appendChild(detailResults);
        } else if (anchor.parentNode) {
            anchor.parentNode.insertBefore(detailResults, anchor.nextSibling);
        }
    }
    function renderCashflowDrilldownContext(result) {
        const context = element('cashflowsDrilldownContext');
        if (!context) return;
        const groupingLabels = { day: 'Daily', fortnight: 'Fortnightly', month: 'Monthly', quarter: 'Quarterly', half_year: 'Half-yearly', year: 'Annually' };
        const scope = cashflowLoadedScope();
        const grouping = groupingLabels[String(scope.group_by || '')] || String(scope.group_by || '');
        const codes = cashflowAccountCodes(scope);
        context.textContent = 'Period: ' + formatRange(scope.period_start, scope.period_end)
            + ' | View: ' + cashflowTreasuryViewLabel(scope.treasury_view)
            + ' | Grouping: ' + grouping + (codes ? ' | Accounts: ' + codes.join(', ') : '');
        const totals = result && result.database_totals;
        // Cached parent reports may cover a broader period. Counts must carry
        // their own matching client/source/date scope before they are displayed.
        const templateId = lastCashflowsResult && lastCashflowsResult.configuration && lastCashflowsResult.configuration.source_template_id;
        const matches = totals && Number(totals.client_id) === Number(clientId)
            && totals.period_start === scope.period_start && totals.period_end === scope.period_end
            && (templateId == null || Number(totals.template_id) === Number(templateId));
        const rowCount = element('cashflowsDatabaseRowCount');
        const voucherCount = element('cashflowsDatabaseVoucherCount');
        if (rowCount) rowCount.textContent = matches && Number.isSafeInteger(totals.row_count) && totals.row_count >= 0
            ? totals.row_count.toLocaleString('en-PK') : '—';
        if (voucherCount) voucherCount.textContent = matches && Number.isSafeInteger(totals.voucher_count) && totals.voucher_count >= 0
            ? totals.voucher_count.toLocaleString('en-PK') : '—';
    }
    function renderCashflowDrilldownCategoryOptions(values) {
        const datalist = element('cashflowsDrilldownCategoryOptions');
        if (!datalist) return;
        const categories = new Set();
        (values || []).forEach(function (row) {
            const category = typeof row === 'string' ? row : (row && row.category);
            if (category) categories.add(String(category));
        });
        datalist.innerHTML = Array.from(categories).sort(function (left, right) {
            return left.localeCompare(right);
        }).map(function (category) {
            return '<option value="' + escapeHtml(category) + '"></option>';
        }).join('');
    }
    function renderCashflowDrilldownAccountOptions(values) {
        const select = element('cashflowsDrilldownAccountHead');
        const categoryInput = element('cashflowsDrilldownCategory');
        if (!select) return;
        const category = categoryInput ? categoryInput.value.trim().toLowerCase() : '';
        const accounts = new Map();
        (values || []).forEach(function (option) {
            const value = typeof option === 'string' ? option : (option && option.value);
            const optionCategory = typeof option === 'string' ? '' : String(option && option.category || '').trim().toLowerCase();
            if (value && (!category || !optionCategory || optionCategory === category)) accounts.set(String(value).toLowerCase(), String(value));
        });
        const selected = select.value;
        const options = Array.from(accounts.values()).sort(function (left, right) {
            return left.localeCompare(right);
        });
        select.innerHTML = '<option value="">All account heads</option>' + options.map(function (account) {
            return '<option value="' + escapeHtml(account) + '">' + escapeHtml(account) + '</option>';
        }).join('');
        if (selected && accounts.has(selected.toLowerCase())) select.value = selected;
    }
    function cashflowDrilldownContextKey(start, end) {
        return cashflowLedgerContextKey(cashflowLedgerContext(start, end));
    }
    function cashflowSummaryContextKey(start, end, groupBy, treasuryView, revision, codes) {
        return JSON.stringify([clientId, start, end, groupBy || 'month', treasuryView || 'combined', Number(revision || cashflowReportRevision || 0), cashflowReportSourceKey || 'unresolved', cashflowConfigurationKey,
            cashflowAccountKey(codes === undefined ? cashflowEffectivePendingAccounts() : codes)]);
    }
    const cashflowGroupingRank = { day: 0, fortnight: 1, month: 2, quarter: 3, half_year: 4, year: 5 };
    function cashflowGroupingCanAggregate(parentGroup, childGroup) {
        const parentRank = cashflowGroupingRank[String(parentGroup || '')];
        const childRank = cashflowGroupingRank[String(childGroup || '')];
        return Number.isFinite(parentRank) && Number.isFinite(childRank) && parentRank <= childRank;
    }
    function cashflowShortMonthYear(date) {
        return formatMonth(date) + '-' + String(date.getFullYear()).slice(-2);
    }
    function cashflowCanonicalPeriods(startValue, endValue, groupBy) {
        const start = fromIso(startValue); const end = fromIso(endValue);
        const grouping = String(groupBy || 'month');
        if (!start || !end || start > end) return [];
        const periods = [];
        function add(candidateStart, candidateEnd, label, key) {
            const clippedStart = candidateStart < start ? start : candidateStart;
            const clippedEnd = candidateEnd > end ? end : candidateEnd;
            if (clippedStart <= clippedEnd) periods.push({ key: key, start: toIso(clippedStart), end: toIso(clippedEnd), label: label });
        }
        if (grouping === 'day') {
            for (let cursor = start; cursor <= end; cursor = localDate(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() + 1)) {
                add(cursor, cursor, cursor.getDate() + '-' + formatMonth(cursor) + '-' + String(cursor.getFullYear()).slice(-2), toIso(cursor));
            }
        } else if (grouping === 'fortnight') {
            for (let cursor = monthStart(start); cursor <= monthStart(end); cursor = monthStart(localDate(cursor.getFullYear(), cursor.getMonth() + 1, 1))) {
                const monthEndDate = monthEnd(cursor);
                [1, 2].forEach(function (sequence) {
                    const bucketStart = sequence === 1 ? cursor : localDate(cursor.getFullYear(), cursor.getMonth(), 15);
                    const bucketEnd = sequence === 1 ? localDate(cursor.getFullYear(), cursor.getMonth(), 14) : monthEndDate;
                    add(bucketStart, bucketEnd, 'F' + sequence + ' ' + cashflowShortMonthYear(bucketStart), cursor.getFullYear() + '-' + String(cursor.getMonth() + 1).padStart(2, '0') + '-' + sequence);
                });
            }
        } else if (grouping === 'month') {
            for (let cursor = monthStart(start); cursor <= monthStart(end); cursor = monthStart(localDate(cursor.getFullYear(), cursor.getMonth() + 1, 1))) {
                add(cursor, monthEnd(cursor), formatMonth(cursor) + '-' + String(cursor.getFullYear()).slice(-2), cursor.getFullYear() + '-' + String(cursor.getMonth() + 1).padStart(2, '0'));
            }
        } else if (grouping === 'quarter') {
            const firstQuarterMonth = Math.floor(start.getMonth() / 3) * 3;
            for (let cursor = localDate(start.getFullYear(), firstQuarterMonth, 1); cursor <= end; cursor = localDate(cursor.getFullYear(), cursor.getMonth() + 3, 1)) {
                const quarter = Math.floor(cursor.getMonth() / 3) + 1;
                add(cursor, localDate(cursor.getFullYear(), cursor.getMonth() + 3, 0), 'Q' + quarter + ' ' + cursor.getFullYear(), cursor.getFullYear() + '-Q' + quarter);
            }
        } else if (grouping === 'half_year') {
            for (let year = start.getFullYear(); year <= end.getFullYear(); year += 1) {
                [1, 2].forEach(function (half) {
                    const month = half === 1 ? 0 : 6;
                    const bucketStart = localDate(year, month, 1);
                    add(bucketStart, localDate(year, month + 6, 0), 'H' + half + ' ' + year, year + '-H' + half);
                });
            }
        } else if (grouping === 'year') {
            for (let year = start.getFullYear(); year <= end.getFullYear(); year += 1) {
                add(localDate(year, 0, 1), localDate(year, 11, 31), String(year), String(year));
            }
        }
        return periods;
    }
    function cashflowMonthKeys(startValue, endValue) {
        return cashflowCanonicalPeriods(startValue, endValue, 'month').map(function (period) { return period.key; });
    }
    function cashflowParentPeriodGroups(parentPeriods, childPeriods) {
        const groups = {};
        for (const child of childPeriods) {
            const childStart = fromIso(child.start); const childEnd = fromIso(child.end);
            const selected = parentPeriods.filter(function (parent) {
                const parentStart = fromIso(parent.start); const parentEnd = fromIso(parent.end);
                return parentStart && parentEnd && parentStart >= childStart && parentEnd <= childEnd;
            });
            if (!selected.length) return null;
            let cursor = fromIso(selected[0].start);
            const expectedEnd = childEnd;
            for (const parent of selected) {
                const parentStart = fromIso(parent.start); const parentEnd = fromIso(parent.end);
                if (!parentStart || !parentEnd || !cursor || parentStart.getTime() !== cursor.getTime()) return null;
                cursor = localDate(parentEnd.getFullYear(), parentEnd.getMonth(), parentEnd.getDate() + 1);
            }
            if (!cursor || cursor.getTime() !== localDate(expectedEnd.getFullYear(), expectedEnd.getMonth(), expectedEnd.getDate() + 1).getTime()) return null;
            groups[child.key] = selected;
        }
        return groups;
    }
    function cashflowAggregateCategoryRows(rows, parentGroups, childPeriods) {
        return (rows || []).map(function (row) {
            const derivedRow = { category: row.category || '' };
            childPeriods.forEach(function (child) {
                derivedRow[child.key] = (parentGroups[child.key] || []).reduce(function (sum, parent) {
                    return sum + (Number(row[parent.key]) || 0);
                }, 0);
            });
            derivedRow.Total = childPeriods.reduce(function (sum, child) { return sum + (Number(derivedRow[child.key]) || 0); }, 0);
            return derivedRow;
        });
    }
    function cashflowAggregateTreasuryRows(rows, parentGroups, childPeriods) {
        const categories = new Set((rows || []).map(function (row) { return String(row.category || ''); }));
        const byKey = new Map();
        (rows || []).forEach(function (row) {
            const key = String(row.category || '') + '|' + String(row.period_key || '');
            const values = byKey.get(key) || { bank_amount: 0, cash_amount: 0, mixed_amount: 0, total_amount: 0 };
            Object.keys(values).forEach(function (field) { values[field] += Number(row[field]) || 0; });
            byKey.set(key, values);
        });
        const result = [];
        categories.forEach(function (category) {
            childPeriods.forEach(function (child) {
                const values = { bank_amount: 0, cash_amount: 0, mixed_amount: 0, total_amount: 0 };
                (parentGroups[child.key] || []).forEach(function (parent) {
                    const row = byKey.get(category + '|' + String(parent.key));
                    if (!row) return;
                    Object.keys(values).forEach(function (field) { values[field] += Number(row[field]) || 0; });
                });
                result.push(Object.assign({ category: category, period_key: child.key }, values));
            });
        });
        return result;
    }
    function cashflowPeriodSelection(preview, start, end, parentGroup, childGroup) {
        if (!cashflowGroupingCanAggregate(parentGroup, childGroup)) return null;
        const childPeriods = cashflowCanonicalPeriods(start, end, childGroup);
        const parentPeriods = cashflowPreviewPeriods(preview);
        const parentGroups = cashflowParentPeriodGroups(parentPeriods, childPeriods);
        return parentGroups ? { periods: childPeriods, groups: parentGroups } : null;
    }
    function cashflowParentMetricTotals(preview, periods, parentGroups) {
        const source = preview && preview.period_metrics;
        const metrics = ['source_line_count', 'selected_voucher_count', 'internal_transfer_count', 'category_review_count', 'deduction_count'];
        if (preview.account_selection) metrics.push('selection_review_count');
        if (!source || typeof source !== 'object') return null;
        const totals = {};
        metrics.forEach(function (metric) { totals[metric] = 0; });
        const byPeriod = {};
        for (const period of periods) {
            const periodTotals = {};
            metrics.forEach(function (metric) { periodTotals[metric] = 0; });
            for (const parent of parentGroups[period.key] || []) {
                const values = source[String(parent.key)];
                if (!values || typeof values !== 'object') return null;
                for (const metric of metrics) {
                    if (!Object.prototype.hasOwnProperty.call(values, metric)) return null;
                    const value = Number(values[metric]);
                    if (!Number.isFinite(value)) return null;
                    periodTotals[metric] += value;
                    totals[metric] += value;
                }
            }
            byPeriod[period.key] = periodTotals;
        }
        return { totals: totals, byPeriod: byPeriod };
    }
    function deriveCashflowSummaryFromParent(parent, start, end, groupBy, treasuryView, codes) {
        if (!parent || String(parent.treasury_view || '') !== String(treasuryView || 'combined')) return null;
        if (cashflowAccountKey(cashflowAccountCodes(parent)) !== cashflowAccountKey(codes === undefined ? cashflowEffectivePendingAccounts() : codes)) return null;
        const parentPreview = parent.preview || {};
        const selection = cashflowPeriodSelection(parentPreview, start, end, parent.group_by, groupBy);
        if (!selection) return null;
        const periods = selection.periods;
        const parentGroups = selection.groups;
        const metrics = cashflowParentMetricTotals(parentPreview, periods, parentGroups);
        if (!metrics) return null;
        const derivedPreview = Object.assign({}, parentPreview);
        delete derivedPreview.balance_reconciliation;
        derivedPreview.periods = periods.map(function (period) { return Object.assign({}, period); });
        derivedPreview.months = cashflowMonthKeys(start, end);
        derivedPreview.period_metrics = {};
        periods.forEach(function (period) { derivedPreview.period_metrics[String(period.key)] = Object.assign({}, metrics.byPeriod[String(period.key)]); });
        derivedPreview.source_line_count = metrics.totals.source_line_count;
        derivedPreview.selected_voucher_count = metrics.totals.selected_voucher_count;
        derivedPreview.internal_transfer_count = metrics.totals.internal_transfer_count;
        derivedPreview.category_review_count = metrics.totals.category_review_count;
        derivedPreview.deduction_count = metrics.totals.deduction_count;
        if (parentPreview.account_selection) {
            derivedPreview.selection_review_count = metrics.totals.selection_review_count;
            derivedPreview.selection_review_details = [];
        }
        derivedPreview.detail_sections_loaded = [];
        delete derivedPreview.ledger_cache;
        derivedPreview.ledger_rollups = [];
        derivedPreview.drilldown_details_prepared = false;
        derivedPreview.drilldown_controls = {};
        derivedPreview.transfer_details = [];
        derivedPreview.customer_payment_details = [];
        ['customer_payment_customer_totals', 'customer_payment_account_period_totals', 'customer_payment_period_totals',
            'vendor_payment_details', 'vendor_payment_vendor_totals', 'vendor_payment_period_totals'].forEach(function (key) { derivedPreview[key] = []; });
        derivedPreview.customer_payment_reconciliation = {};
        derivedPreview.deduction_details = [];
        derivedPreview.categories_needing_review = [];
        derivedPreview.child_details_prepared = false;
        derivedPreview.child_details_version = 0;
        const treasuryRows = Array.isArray(parentPreview.treasury_category_totals)
            ? cashflowAggregateTreasuryRows(parentPreview.treasury_category_totals, parentGroups, periods)
            : [];
        derivedPreview.treasury_category_totals = treasuryRows;
        // Aggregate only complete finer-period proofs; never carry a parent's
        // full-period bridge into a smaller date selection.
        const proofFields = ['category_movement', 'internal_transfer_movement', 'mixed_voucher_movement',
            'explained_movement', 'source_movement', 'variance', 'unallocated_category_movement', 'mixed_voucher_count'];
        derivedPreview.treasury_movement_proof = [];
        periods.forEach(function (child) {
            ['bank', 'cash', 'combined'].forEach(function (view) {
                const rows = (parentGroups[child.key] || []).map(function (parentPeriod) {
                    return (parentPreview.treasury_movement_proof || []).find(function (row) {
                        return row.period_key === parentPeriod.key && row.treasury_type === view;
                    });
                });
                if (!rows.length || rows.some(function (row) { return !row; })) return;
                const row = {period_key: child.key, treasury_type: view};
                proofFields.forEach(function (field) { row[field] = rows.reduce(function (sum, value) { return sum + Number(value[field]); }, 0); });
                row.status = rows.every(function (value) { return value.status === 'Matched'; }) ? 'Matched' : 'Variance';
                row.allocation_status = rows.some(function (value) { return value.allocation_status === 'Review required'; }) ? 'Review required' : 'Proven';
                derivedPreview.treasury_movement_proof.push(row);
            });
        });
        if (derivedPreview.treasury_movement_proof.length === periods.length * 3) {
            const review = derivedPreview.treasury_movement_proof.some(function (row) { return row.allocation_status === 'Review required'; });
            const matched = derivedPreview.treasury_movement_proof.every(function (row) { return row.status === 'Matched'; });
            derivedPreview.treasury_reconciliation = Object.assign({}, parentPreview.treasury_reconciliation || {}, {
                status: !matched ? 'Variance' : review ? 'Review required' : 'Matched',
                source_status: matched ? 'Matched' : 'Variance', allocation_status: review ? 'Review required' : 'Proven',
                mixed_amount: treasuryRows.reduce(function (sum, row) { return sum + Number(row.mixed_amount); }, 0),
                mixed_row_count: treasuryRows.filter(function (row) { return Math.abs(row.mixed_amount) > 0.005; }).length
            });
            derivedPreview.treasury_view_reconciliation = derivedPreview.treasury_reconciliation;
        }
        const visibleRows = cashflowSummaryRows(parentPreview);
        if (String(treasuryView || 'combined') === 'split') {
            derivedPreview.treasury_view_totals = cashflowAggregateTreasuryRows(visibleRows, parentGroups, periods);
        } else {
            const rows = cashflowAggregateCategoryRows(visibleRows, parentGroups, periods);
            derivedPreview.category_totals = rows;
            derivedPreview.treasury_view_totals = rows;
        }
        const derived = Object.assign({}, parent, {
            period_start: start,
            period_end: end,
            group_by: groupBy,
            treasury_view: treasuryView,
            preview: derivedPreview,
            blocking_reasons: [],
            freshness: Object.assign({}, parent.freshness || {}, { mode: 'browser_parent_cache', authoritative: false })
        });
        return derived;
    }
    function findCashflowParentSummary(start, end, groupBy, treasuryView, codes) {
        const entries = Array.from(cashflowSummaryCache.values()).reverse();
        for (const entry of entries) {
            if (!entry || entry.revision !== cashflowReportRevision || entry.sourceKey !== cashflowReportSourceKey) continue;
            const parent = entry.result;
            const parentStart = fromIso(parent && parent.period_start);
            const parentEnd = fromIso(parent && parent.period_end);
            const childStart = fromIso(start);
            const childEnd = fromIso(end);
            if (!parentStart || !parentEnd || !childStart || !childEnd || parentStart > childStart || parentEnd < childEnd) continue;
            if (!cashflowGroupingCanAggregate(parent.group_by, groupBy)) continue;
            const derived = deriveCashflowSummaryFromParent(parent, start, end, groupBy, treasuryView, codes);
            if (derived) return derived;
        }
        return null;
    }
    function rememberCashflowSummary(result) {
        if (!result || !cashflowReportRevision || !result.period_start || !result.period_end) return;
        // Ledger changes need not advance the voucher revision. Cache movements
        // only; each Load obtains its balance evidence from a fresh snapshot.
        result = Object.assign({}, result, {preview: Object.assign({}, result.preview || {})});
        delete result.preview.balance_reconciliation;
        const key = cashflowSummaryContextKey(result.period_start, result.period_end, result.group_by, result.treasury_view, cashflowReportRevision, cashflowAccountCodes(result));
        cashflowSummaryCache.delete(key);
        cashflowSummaryCache.set(key, { result: result, revision: cashflowReportRevision, sourceKey: cashflowReportSourceKey });
        while (cashflowSummaryCache.size > cashflowSummaryCacheLimit) {
            cashflowSummaryCache.delete(cashflowSummaryCache.keys().next().value);
        }
    }
    function takeCashflowSummary(start, end, groupBy, treasuryView, codes) {
        const key = cashflowSummaryContextKey(start, end, groupBy, treasuryView, cashflowReportRevision, codes);
        const entry = cashflowSummaryCache.get(key);
        if (!entry) return null;
        if (entry.revision !== cashflowReportRevision || entry.sourceKey !== cashflowReportSourceKey) {
            cashflowSummaryCache.delete(key);
            return null;
        }
        cashflowSummaryCache.delete(key);
        cashflowSummaryCache.set(key, entry);
        return entry.result;
    }
    function cashflowReportRevisionFromResult(result) {
        const source = result && result.source || {};
        const metadata = source.metadata || {};
        const raw = result && result.cashflow_report_revision != null
            ? result.cashflow_report_revision
            : (source.cashflow_report_revision != null ? source.cashflow_report_revision : metadata.cashflow_report_revision);
        const revision = Number(raw);
        return Number.isFinite(revision) && revision > 0 ? revision : 0;
    }
    function cashflowReportSourceKeyFromResult(result) {
        const source = result && result.source || {};
        const metadata = source.metadata || {};
        const raw = result && result.cashflow_report_source_key != null
            ? result.cashflow_report_source_key
            : (source.cashflow_report_source_key != null ? source.cashflow_report_source_key : metadata.cashflow_report_source_key);
        return String(raw || '').trim();
    }
    function clearCashflowReportCaches() {
        clearReconciliationCache();
        cashflowSummaryCache.clear();
        cashflowDrilldownBaseCache.clear();
        cashflowLedgerCacheOrder.clear();
        resetCashflowInlineLedgerState();
        cashflowChildPreviews = {};
    }
    function syncCashflowReportRevision(result) {
        const incoming = cashflowReportRevisionFromResult(result);
        const incomingSourceKey = cashflowReportSourceKeyFromResult(result);
        if (incoming && cashflowReportRevision && incoming < cashflowReportRevision) return false;
        const revisionChanged = Boolean(incoming && cashflowReportRevision && incoming !== cashflowReportRevision);
        const sourceChanged = Boolean(incomingSourceKey && cashflowReportSourceKey && incomingSourceKey !== cashflowReportSourceKey);
        const incomingConfiguration = String(result && result.cashflow_configuration_key || '');
        const configurationChanged = Boolean(incomingConfiguration && cashflowConfigurationKey && incomingConfiguration !== cashflowConfigurationKey);
        if (revisionChanged || sourceChanged || configurationChanged) clearCashflowReportCaches();
        if (incoming) cashflowReportRevision = incoming;
        if (incomingSourceKey) cashflowReportSourceKey = incomingSourceKey;
        if (incomingConfiguration) cashflowConfigurationKey = incomingConfiguration;
        cashflowRevisionCheckedAt = Date.now();
        return true;
    }
    function ensureCashflowRevisionFresh(force, failClosed) {
        if (!clientId) return Promise.resolve(true);
        const now = Date.now();
        if (!force && cashflowRevisionCheckedAt && now - cashflowRevisionCheckedAt < CASHFLOW_REVISION_CHECK_TTL_MS) return Promise.resolve(true);
        if (cashflowRevisionCheckPromise) return cashflowRevisionCheckPromise;
        const checkedClient = clientId;
        const checkedGeneration = cashflowSummaryGeneration;
        cashflowRevisionCheckPromise = api("/api/reconciliation/cashflows/revision?client_id=" + encodeURIComponent(clientId))
            .then(function (result) {
                if (checkedClient !== clientId || checkedGeneration !== cashflowSummaryGeneration) return false;
                const incoming = Number(result && result.cashflow_report_revision || 0);
                if (!incoming || incoming < cashflowReportRevision) return false;
                const incomingSourceKey = String(result && result.cashflow_report_source_key || '').trim();
                const revisionChanged = Boolean(incoming && cashflowReportRevision && incoming !== cashflowReportRevision);
                const sourceChanged = Boolean(incomingSourceKey && cashflowReportSourceKey && incomingSourceKey !== cashflowReportSourceKey);
                const incomingConfiguration = String(result && result.cashflow_configuration_key || '');
                const configurationChanged = Boolean(incomingConfiguration && cashflowConfigurationKey && incomingConfiguration !== cashflowConfigurationKey);
                if (revisionChanged || sourceChanged || configurationChanged) {
                    clearCashflowReportCaches();
                    if (incoming) cashflowReportRevision = incoming;
                    if (incomingSourceKey) cashflowReportSourceKey = incomingSourceKey;
                    if (incomingConfiguration) cashflowConfigurationKey = incomingConfiguration;
                    return false;
                }
                if (incoming) cashflowReportRevision = incoming;
                if (incomingSourceKey) cashflowReportSourceKey = incomingSourceKey;
                if (incomingConfiguration) cashflowConfigurationKey = incomingConfiguration;
                cashflowRevisionCheckedAt = Date.now();
                return true;
            })
            .catch(function () { return !failClosed; })
            .finally(function () { cashflowRevisionCheckPromise = null; });
        return cashflowRevisionCheckPromise;
    }
    function renderCashflowFreshness(result) {
        const status = element("cashflowsFreshnessStatus");
        const alert = element("cashflowsFreshnessAlert");
        if (!status || !alert) return;
        const freshness = result && result.freshness || {};
        const revision = Number(freshness.report_revision || cashflowReportRevision || 0);
        const generatedAt = freshness.generated_at ? new Date(freshness.generated_at) : null;
        const generatedLabel = generatedAt && !Number.isNaN(generatedAt.getTime()) ? generatedAt.toLocaleString() : "just now";
        const modeLabel = freshness.mode === 'browser_parent_cache'
            ? "Parent-period cache"
            : (freshness.authoritative ? "Authoritative source refresh" : "Prepared snapshot");
        status.textContent = modeLabel + (revision ? " · Version " + String(revision) : "") + " · Generated " + generatedLabel;
        alert.hidden = false;
        window.clearTimeout(cashflowFreshnessAlertTimer);
        cashflowFreshnessAlertTimer = window.setTimeout(function () {
            alert.hidden = true;
        }, 5000);
    }
    function dismissCashflowFreshnessAlert() {
        const alert = element("cashflowsFreshnessAlert");
        if (!alert) return;
        window.clearTimeout(cashflowFreshnessAlertTimer);
        cashflowFreshnessAlertTimer = null;
        alert.hidden = true;
    }
    function setCashflowRefreshMenuOpen(open) {
        const button = element('cashflowsRefreshMenuButton');
        const menu = element('cashflowsRefreshMenu');
        if (!button || !menu) return;
        const isOpen = Boolean(open);
        menu.hidden = !isOpen;
        button.setAttribute('aria-expanded', String(isOpen));
    }
    function closeCashflowRefreshMenu() {
        setCashflowRefreshMenuOpen(false);
    }
    function cashflowDrilldownFilterState() {
        const selectedTreasury = element('cashflowsDrilldownTreasury').value;
        const view = cashflowLoadedScope().treasury_view;
        const effectiveTreasury = selectedTreasury || (view === 'bank' ? 'Bank' : (view === 'cash' ? 'Cash' : ''));
        return {
            search: element('cashflowsDrilldownSearch').value.trim().toLowerCase(),
            category: element('cashflowsDrilldownCategory').value.trim().toLowerCase(),
            accountHead: element('cashflowsDrilldownAccountHead').value.trim().toLowerCase(),
            treasuryType: effectiveTreasury.toLowerCase(),
            periodKey: element('cashflowsDrilldownPeriod').value.trim().toLowerCase(),
        };
    }
    function cashflowDrilldownRowMatches(row, filters) {
        const searchable = [row.category, row.account_code, row.account_name, row.period_label].map(function (value) {
            return String(value || '');
        }).join(' ').toLowerCase();
        const accountName = String(row.account_name || '').toLowerCase();
        const accountCode = String(row.account_code || '').toLowerCase();
        const accountLabel = [row.account_name, row.account_code].filter(Boolean).join(' / ').toLowerCase();
        return (!filters.search || searchable.indexOf(filters.search) >= 0)
            && (!filters.category || String(row.category || '').toLowerCase() === filters.category)
            && (!filters.accountHead || filters.accountHead === accountName || filters.accountHead === accountCode || filters.accountHead === accountLabel)
            && (!filters.treasuryType || String(row.treasury_type || '').toLowerCase() === filters.treasuryType)
            && (!filters.periodKey || String(row.period_key || '').toLowerCase() === filters.periodKey);
    }
    function cashflowDrilldownOptionsFromRows(rows, filters) {
        const options = new Map();
        (rows || []).forEach(function (row) {
            if (filters.treasuryType && String(row.treasury_type || '').toLowerCase() !== filters.treasuryType) return;
            if (filters.periodKey && String(row.period_key || '').toLowerCase() !== filters.periodKey) return;
            const category = String(row.category || '').trim();
            const value = [row.account_name, row.account_code].filter(Boolean).join(' / ').trim();
            if (category && value) options.set((category + '|' + value).toLowerCase(), { category: category, value: value });
        });
        return Array.from(options.values()).sort(function (left, right) {
            return (left.category + '|' + left.value).localeCompare(right.category + '|' + right.value);
        });
    }
    function cashflowDrilldownLocalResult(base, filters, page) {
        const filteredRows = (base.rows || []).filter(function (row) { return cashflowDrilldownRowMatches(row, filters); });
        const pageCount = Math.max(1, Math.ceil(filteredRows.length / cashflowDrilldownLimit));
        const currentPage = Math.min(page, pageCount);
        const startIndex = (currentPage - 1) * cashflowDrilldownLimit;
        return Object.assign({}, base.result, {
            rows: filteredRows.slice(startIndex, startIndex + cashflowDrilldownLimit),
            account_head_options: cashflowDrilldownOptionsFromRows(base.rows, filters),
            total_rows: filteredRows.length,
            page: currentPage,
            limit: cashflowDrilldownLimit,
            page_count: pageCount,
        });
    }
    // Only compact account/period data lives here. Raw transactions stay on demand.
    function cashflowLedgerContext(start, end) {
        const scope = cashflowLoadedScope();
        return {
            client_id: clientId,
            template_id: ((lastCashflowsResult || {}).configuration || {}).source_template_id,
            period_start: scope.period_start || start, period_end: scope.period_end || end,
            group_by: scope.group_by || 'month', treasury_view: scope.treasury_view || 'combined',
            report_revision: cashflowReportRevision, source_key: cashflowReportSourceKey,
            configuration_key: cashflowConfigurationKey,
            account_selection: scope.account_selection
        };
    }
    function cashflowLedgerContextKey(context) {
        const value = context || {};
        return JSON.stringify(['client_id', 'template_id', 'period_start', 'period_end', 'group_by',
            'treasury_view', 'report_revision', 'source_key', 'configuration_key'].map(function (field) {
            return String(value[field] == null ? '' : value[field]);
        }).concat([cashflowAccountKey(cashflowAccountCodes(value))]));
    }
    function cashflowLedgerMap(kind) {
        return kind === 'baseline' ? cashflowDrilldownBaseCache : cashflowInlineLedgerCache;
    }
    function clearCashflowInlineCache() {
        cashflowInlineLedgerCache.clear();
        cashflowLedgerCacheOrder.forEach(function (entry, key) {
            if (entry.kind === 'inline') cashflowLedgerCacheOrder.delete(key);
        });
    }
    function rememberCashflowLedger(kind, key, value) {
        const map = cashflowLedgerMap(kind);
        const orderKey = kind + '|' + key;
        map.delete(key);
        cashflowLedgerCacheOrder.delete(orderKey);
        const rows = (value.rows || []).length;
        const bytes = new TextEncoder().encode(JSON.stringify(value)).byteLength;
        if (rows > CASHFLOW_LEDGER_CACHE_MAX_ROWS || bytes > CASHFLOW_LEDGER_CACHE_MAX_BYTES) return false;
        map.set(key, value);
        cashflowLedgerCacheOrder.set(orderKey, { kind: kind, key: key, rows: rows, bytes: bytes });
        let totalRows = 0;
        let totalBytes = 0;
        cashflowLedgerCacheOrder.forEach(function (entry) { totalRows += entry.rows; totalBytes += entry.bytes; });
        while (totalRows > CASHFLOW_LEDGER_CACHE_MAX_ROWS || totalBytes > CASHFLOW_LEDGER_CACHE_MAX_BYTES
                || cashflowLedgerCacheOrder.size > 25) {
            const oldestKey = cashflowLedgerCacheOrder.keys().next().value;
            const oldest = cashflowLedgerCacheOrder.get(oldestKey);
            cashflowLedgerMap(oldest.kind).delete(oldest.key);
            cashflowLedgerCacheOrder.delete(oldestKey);
            totalRows -= oldest.rows;
            totalBytes -= oldest.bytes;
        }
        return map.has(key);
    }
    function takeCashflowLedger(kind, key) {
        const value = cashflowLedgerMap(kind).get(key);
        const orderKey = kind + '|' + key;
        const entry = cashflowLedgerCacheOrder.get(orderKey);
        if (value && entry) {
            cashflowLedgerCacheOrder.delete(orderKey);
            cashflowLedgerCacheOrder.set(orderKey, entry);
        }
        return value;
    }
    function cashflowLedgerResponseMatches(payload, context) {
        return payload && payload.ledger_context
            && cashflowLedgerContextKey(payload.ledger_context) === cashflowLedgerContextKey(context);
    }
    function cashflowLoadedLedgerContextMatches(context) {
        const result = lastCashflowsResult || {};
        const loaded = {client_id: result.client_id,
            template_id: (result.ledger_context || {}).template_id || (result.configuration || {}).source_template_id,
            period_start: result.period_start, period_end: result.period_end,
            group_by: result.group_by, treasury_view: result.treasury_view,
            report_revision: cashflowReportRevisionFromResult(result),
            source_key: cashflowReportSourceKeyFromResult(result),
            configuration_key: result.cashflow_configuration_key, account_selection: result.account_selection};
        return cashflowLedgerContextKey(loaded) === cashflowLedgerContextKey(context);
    }
    function seedCashflowReportLedgers(result) {
        const descriptor = (result.preview || {}).ledger_cache;
        if (!descriptor || !descriptor.complete || !Array.isArray(descriptor.rows)
                || descriptor.row_count !== descriptor.rows.length) return false;
        const context = cashflowLedgerContext(result.period_start, result.period_end);
        if (cashflowLedgerContextKey(descriptor.context) !== cashflowLedgerContextKey(context)) return false;
        return rememberCashflowLedger('baseline', cashflowLedgerContextKey(context), {
            rows: descriptor.rows,
            result: {client_id: clientId, period_start: result.period_start, period_end: result.period_end,
                group_by: result.group_by, treasury_view: result.treasury_view,
                ledger_context: context, database_totals: result.database_totals,
                source: {ready: (result.source || {}).ready}, blocking_reasons: result.blocking_reasons || []}
        });
    }
    function cashflowSummaryWithoutLedgers(result) {
        const preview = Object.assign({}, result.preview || {});
        delete preview.ledger_cache;
        preview.ledger_rollups = [];
        preview.drilldown_controls = {};
        preview.drilldown_details_prepared = false;
        return Object.assign({}, result, {preview: preview});
    }
    function cacheCashflowDrilldownBaseline(result) {
        if (!result || !Array.isArray(result.base_rows)) return;
        const context = cashflowLedgerContext(result.period_start, result.period_end);
        if (!cashflowLedgerResponseMatches(result, context)) return;
        const baseResult = Object.assign({}, result, {rows: [], account_head_options: []});
        delete baseResult.base_rows;
        rememberCashflowLedger('baseline', cashflowLedgerContextKey(context), {rows: result.base_rows, result: baseResult});
    }
    function syncCashflowDrilldownPagination(result) {
        const payload = result || lastCashflowDrilldownResult || {};
        const page = Number(payload.page) || 1;
        const pageCount = Number(payload.page_count) || 1;
        element('cashflowsDrilldownPrevious').disabled = page <= 1;
        element('cashflowsDrilldownNext').disabled = page >= pageCount;
    }
    function renderCashflowDrilldown(result) {
        const rows = result && Array.isArray(result.rows) ? result.rows : [];
        renderCashflowDrilldownContext(result);
        renderCashflowDrilldownAccountOptions(result && result.account_head_options);
        renderTable(element('cashflowsLedgerDrilldownTable'), [
            { key: 'category', label: 'Category' },
            { key: 'period_label', label: 'Period' },
            { key: 'account_name', label: 'Ledger account' },
            { key: 'account_code', label: 'Code' },
            { key: 'treasury_type', label: 'Cash / Bank' },
            { key: 'amount', label: 'Amount', amount: true },
            { key: 'line_count', label: 'Lines', amount: false },
        ], rows, 'No ledger detail matches the selected filters.');
        const page = Number(result && result.page) || 1;
        const pageCount = Number(result && result.page_count) || 1;
        element('cashflowsDrilldownPageInfo').textContent = 'Page ' + String(page) + ' of ' + String(pageCount);
        syncCashflowDrilldownPagination(result);
    }
    function cashflowInlineLedgerKey(section, category) {
        return [cashflowDrilldownContextKey(cashflowSelection.start, cashflowSelection.end), section, category].join('|');
    }
    function resetCashflowInlineLedgerState() {
        cashflowInlineLedgerGeneration += 1;
        cashflowInlineLedgerControllers.forEach(function (controller) { controller.abort(); });
        cashflowInlineLedgerControllers = new Map();
        clearCashflowInlineCache();
        cashflowInlineTransactionCache = new Map();
        cashflowInlineTransactionRequestSerial += 1;
        if (cashflowInlineTransactionActiveController) cashflowInlineTransactionActiveController.abort();
        cashflowInlineTransactionActiveController = null;
        if (cashflowInlineTransactionDrawer) {
            cashflowInlineTransactionDrawer.hidden = true;
            document.body.classList.remove('cashflows-inline-transaction-open');
        }
        cashflowInlineTransactionContext = null;
        cashflowInlineTransactionPage = 1;
        cashflowInlineTransactionFocus = null;
    }
    function cashflowInlineLedgerTreasuryBucket(value) {
        const type = String(value || '').toLowerCase();
        if (type.includes('bank') && type.includes('cash')) return 'mixed';
        if (type.includes('bank')) return 'bank';
        if (type.includes('cash')) return 'cash';
        return 'mixed';
    }
    function cashflowInlineLedgerRows(rows, periods, isSplit) {
        const periodKeys = periods.map(function (period) { return String(period.key); });
        const grouped = new Map();
        (rows || []).forEach(function (row) {
            const accountCode = String(row.account_code || '').trim();
            const accountName = String(row.account_name || '').trim();
            const treasuryType = String(row.treasury_type || '').trim();
            const key = [accountCode, accountName, treasuryType].join('|');
            if (!grouped.has(key)) grouped.set(key, { accountCode: accountCode, accountName: accountName, treasuryType: treasuryType, values: {}, splitPeriods: {}, total: 0 });
            const item = grouped.get(key);
            const periodKey = String(row.period_key || '');
            const amount = Number(row.amount) || 0;
            if (!periodKeys.includes(periodKey)) return;
            item.total += amount;
            if (isSplit) {
                if (!item.splitPeriods[periodKey]) item.splitPeriods[periodKey] = { bank: 0, cash: 0, mixed: 0, total: 0 };
                const bucket = cashflowInlineLedgerTreasuryBucket(treasuryType);
                item.splitPeriods[periodKey][bucket] += amount;
                item.splitPeriods[periodKey].total += amount;
            } else item.values[periodKey] = (item.values[periodKey] || 0) + amount;
        });
        return Array.from(grouped.values()).sort(function (left, right) {
            const leftLabel = (left.accountName || left.accountCode || '').toLowerCase();
            const rightLabel = (right.accountName || right.accountCode || '').toLowerCase();
            return leftLabel.localeCompare(rightLabel) || left.accountCode.localeCompare(right.accountCode);
        });
    }
    function cashflowInlineLedgerLabel(item) {
        const rawName = String(item.accountName || '').trim();
        const accountName = rawName.split(/\s+\/\s+/)[0].trim();
        return accountName || item.accountCode || 'Unlabelled ledger';
    }
    function cashflowInlineLedgerGroupValue(section, category) {
        return encodeURIComponent(cashflowInlineLedgerKey(section, category));
    }
    function cashflowInlineLedgerButton(row, category, section) {
        const label = cashflowInlineLedgerLabel(row);
        const accountCode = String(row.accountCode || '').trim();
        const hoverText = accountCode ? 'Account code: ' + accountCode : '';
        const accessibleLabel = accountCode ? label + ' (Account code: ' + accountCode + ')' : label;
        return '<button type=button class=cashflows-inline-ledger-link' + (hoverText ? ' title="' + escapeHtml(hoverText) + '"' : '') + ' aria-label="' + escapeHtml(accessibleLabel) + '" data-cashflow-inline-ledger=true data-cashflow-inline-category=' + encodeURIComponent(category) + ' data-cashflow-inline-section=' + encodeURIComponent(section) + ' data-cashflow-inline-account=' + encodeURIComponent(row.accountCode || '') + ' data-cashflow-inline-account-name=' + encodeURIComponent(row.accountName || '') + ' data-cashflow-inline-treasury=' + encodeURIComponent(row.treasuryType || '') + '>' + escapeHtml(label) + '</button>';
    }
    function cashflowInlineLedgerHostMarkup(category, section, colspan, showLoadingLabel) {
        const group = cashflowInlineLedgerGroupValue(section, category);
        const loadingLabel = showLoadingLabel === false ? '' : 'Loading ledgers...';
        const hidden = showLoadingLabel === false ? ' aria-hidden=true' : '';
        return '<tr class=cashflows-inline-ledger-host-row data-cashflow-inline-ledger-group="' + escapeHtml(group) + '"><td colspan="' + String(colspan) + '"><div class=cashflows-inline-ledger-host' + hidden + '>' + loadingLabel + '</div></td></tr>';
    }
    function cashflowInlineLedgerRowMarkup(row, category, section, periodKeys, isSplit, group, index, rowCount) {
        const classes = ['cashflows-inline-ledger-row'];
        if (index === 0) classes.push('cashflows-inline-ledger-first');
        if (index === rowCount - 1) classes.push('cashflows-inline-ledger-last');
        const ledgerType = String(row.treasuryType || '').trim();
        const ledgerTypeMarkup = ledgerType ? '<span class="cashflows-inline-ledger-type"> - ' + escapeHtml(ledgerType) + '</span>' : '';
        const cells = [
            '<td class="cashflows-table-text">' + cashflowInlineLedgerButton(row, category, section) + ledgerTypeMarkup + '</td>'
        ];
        const splitTotals = { bank: 0, cash: 0, mixed: 0, total: 0 };
        if (isSplit) {
            periodKeys.forEach(function (periodKey) {
                const values = row.splitPeriods[periodKey] || { bank: 0, cash: 0, mixed: 0, total: 0 };
                ['bank', 'cash', 'mixed', 'total'].forEach(function (field) {
                    const value = Number(values[field]) || 0;
                    splitTotals[field] += value;
                    cells.push('<td class=cashflows-table-number>' + escapeHtml(formatCashflowSectionAmount(value, section)) + '</td>');
                });
            });
            ['bank', 'cash', 'mixed', 'total'].forEach(function (field) {
                cells.push('<td class=cashflows-table-number>' + escapeHtml(formatCashflowSectionAmount(splitTotals[field], section)) + '</td>');
            });
        } else {
            periodKeys.forEach(function (periodKey) {
                cells.push('<td class=cashflows-table-number>' + escapeHtml(formatCashflowSectionAmount(row.values[periodKey] || 0, section)) + '</td>');
            });
            cells.push('<td class=cashflows-table-number>' + escapeHtml(formatCashflowSectionAmount(row.total, section)) + '</td>');
        }
        cells.push('<td class=cashflows-table-number>-</td>');
        cells.push('<td class=cashflows-table-number>' + escapeHtml(formatLocalAmount(Math.abs(row.total))) + '</td>');
        return '<tr class="' + classes.join(' ') + '" data-cashflow-inline-ledger-group="' + escapeHtml(group) + '">' + cells.join('') + '</tr>';
    }
    function renderCashflowInlineLedgerHost(host, payload, category, section, periods, isSplit) {
        const hostPair = host && host.left ? host : { left: host, right: null };
        const leftHost = hostPair.left;
        const rightHost = hostPair.right;
        const rows = cashflowInlineLedgerRows(payload.rows || [], periods, isSplit);
        const periodKeys = periods.map(function (period) { return String(period.key); });
        if (!rows.length) {
            leftHost.querySelector('.cashflows-inline-ledger-host').innerHTML = '<span class=cashflows-inline-ledger-empty>No ledgers found for this category.</span>';
            if (rightHost) rightHost.querySelector('.cashflows-inline-ledger-host').innerHTML = '';
            return;
        }
        const group = leftHost.dataset.cashflowInlineLedgerGroup || cashflowInlineLedgerGroupValue(section, category);
        if (!leftHost.isConnected || (rightHost && !rightHost.isConnected)) return;
        let html = rows.map(function (row, index) {
            return cashflowInlineLedgerRowMarkup(row, category, section, periodKeys, isSplit, group, index, rows.length);
        }).join('');
        if (Number(payload.page_count) > 1) {
            const page = Number(payload.page) || 1;
            const pageButton = function (label, target, disabled) {
                return '<button type=button data-cashflow-inline-page=' + target
                    + ' data-cashflow-inline-category=' + encodeURIComponent(category)
                    + ' data-cashflow-inline-section=' + encodeURIComponent(section)
                    + (disabled ? ' disabled' : '') + '>' + label + '</button>';
            };
            html += '<tr class=cashflows-inline-ledger-host-row data-cashflow-inline-ledger-group="' + escapeHtml(group)
                + '"><td>Page ' + page + ' of ' + payload.page_count + ' '
                + pageButton('Previous', page - 1, page <= 1) + ' '
                + pageButton('Next', page + 1, page >= payload.page_count)
                + '</td><td colspan="' + String((isSplit ? (periods.length + 1) * 4 + 2 : periods.length + 3))
                + '">Amounts reflect this page. Use ledger drill-down to filter or export the full category.</td></tr>';
        }
        if (!rightHost) {
            leftHost.insertAdjacentHTML('afterend', html);
            leftHost.remove();
            return;
        }
        const template = document.createElement('tbody');
        template.innerHTML = html;
        const leftMarkup = [];
        const rightMarkup = [];
        Array.from(template.children).forEach(function (sourceRow, index) {
            const parts = cashflowSummaryPaneRowParts(sourceRow, group + '-ledger-' + index, false);
            leftMarkup.push(parts.left.outerHTML);
            rightMarkup.push(parts.right.outerHTML);
        });
        leftHost.insertAdjacentHTML('afterend', leftMarkup.join(''));
        rightHost.insertAdjacentHTML('afterend', rightMarkup.join(''));
        leftHost.remove();
        rightHost.remove();
        requestAnimationFrame(function () { syncCashflowSummaryPaneRows(element('cashflowsSummaryTable')); });
    }
    function toggleCashflowInlineLedger(button) {
        openCashflowTransactionDrawer(button);
    }
    function cashflowTransactionModeForContext(context) {
        const treasuryType = String((context || {}).treasuryType || '').toLowerCase();
        if (treasuryType === 'cash' || treasuryType.indexOf('cash') >= 0 && treasuryType.indexOf('bank') < 0) return 'cash';
        if (treasuryType === 'bank' || treasuryType.indexOf('bank') >= 0 && treasuryType.indexOf('cash') < 0) return 'bank';
        return 'combined';
    }
    function cashflowTransactionModeLabel(mode) {
        return mode === 'cash' ? 'Cash' : mode === 'bank' ? 'Bank' : 'Both Cash and Bank';
    }
    function syncCashflowTransactionModeLabel() {
        const drawer = ensureCashflowTransactionDrawer();
        const select = drawer.querySelector('#cashflowsInlineTransactionMode');
        const label = drawer.querySelector('#cashflowsInlineTransactionModeLabel');
        if (select && label) label.textContent = cashflowTransactionModeLabel(select.value || 'combined');
    }
    function cashflowDrawerTransactionKey(context, page, filters) {
        return [
            clientId, ((lastCashflowsResult || {}).configuration || {}).source_template_id,
            cashflowAccountKey(cashflowAccountCodes(cashflowLoadedScope())), cashflowConfigurationKey,
            cashflowReportRevision,
            filters.start,
            filters.end,
            filters.search,
            filters.mode,
            context.section,
            context.category,
            context.accountCode,
            context.treasuryType,
            page
        ].join('|');
    }
    function ensureCashflowTransactionDrawer() {
        if (cashflowInlineTransactionDrawer) return cashflowInlineTransactionDrawer;
        const drawer = document.createElement('div');
        drawer.id = 'cashflowsInlineTransactionDrawer';
        drawer.className = 'cashflows-inline-transaction-drawer';
        drawer.hidden = true;
        drawer.innerHTML = '<div class=cashflows-inline-transaction-backdrop data-cashflows-inline-transaction-close' + cashflowInlineHtmlClose + '</div'
            + cashflowInlineHtmlClose + '<section class=cashflows-inline-transaction-dialog role=dialog aria-modal=true aria-label=Transaction detail'
            + cashflowInlineHtmlClose + '<header class=cashflows-inline-transaction-header'
            + cashflowInlineHtmlClose + '<div class=cashflows-inline-transaction-context'
            + cashflowInlineHtmlClose + '<div class=cashflows-inline-transaction-context-item' + cashflowInlineHtmlClose
            + '<span class=cashflows-inline-transaction-context-label' + cashflowInlineHtmlClose + 'Assigned Category</span' + cashflowInlineHtmlClose
            + '<strong id=cashflowsInlineTransactionCategory' + cashflowInlineHtmlClose + '</strong' + cashflowInlineHtmlClose
            + '</div' + cashflowInlineHtmlClose
            + '<div class=cashflows-inline-transaction-context-item' + cashflowInlineHtmlClose
            + '<span class=cashflows-inline-transaction-context-label' + cashflowInlineHtmlClose + 'Account Code / Account Name</span' + cashflowInlineHtmlClose
            + '<strong id=cashflowsInlineTransactionAccount' + cashflowInlineHtmlClose + '</strong' + cashflowInlineHtmlClose
            + '</div' + cashflowInlineHtmlClose
            + '<div class=cashflows-inline-transaction-context-item' + cashflowInlineHtmlClose
            + '<span class=cashflows-inline-transaction-context-label' + cashflowInlineHtmlClose + 'View</span' + cashflowInlineHtmlClose
            + '<strong id=cashflowsInlineTransactionModeLabel' + cashflowInlineHtmlClose + '</strong' + cashflowInlineHtmlClose
            + '</div' + cashflowInlineHtmlClose
            + '</div' + cashflowInlineHtmlClose
            + '<button type=button class=cashflows-inline-transaction-close aria-label=Close'
            + cashflowInlineHtmlClose + '&times;</button' + cashflowInlineHtmlClose + '</header' + cashflowInlineHtmlClose
            + '<div class=cashflows-inline-transaction-controls' + cashflowInlineHtmlClose
            + '<label>Search <input id=cashflowsInlineTransactionSearch type=search placeholder=Search'
            + cashflowInlineHtmlClose + '</label' + cashflowInlineHtmlClose
            + '<label>View <select id=cashflowsInlineTransactionMode' + cashflowInlineHtmlClose
            + '<option value=cash' + cashflowInlineHtmlClose + 'Cash</option' + cashflowInlineHtmlClose
            + '<option value=bank' + cashflowInlineHtmlClose + 'Bank</option' + cashflowInlineHtmlClose
            + '<option value=combined' + cashflowInlineHtmlClose + 'Both Cash and Bank</option' + cashflowInlineHtmlClose
            + '</select' + cashflowInlineHtmlClose + '</label' + cashflowInlineHtmlClose
            + '<label>Period <select id=cashflowsInlineTransactionPeriod' + cashflowInlineHtmlClose
            + '</select' + cashflowInlineHtmlClose + '</label' + cashflowInlineHtmlClose
            + '<button type=button class=cashflows-inline-transaction-apply' + cashflowInlineHtmlClose + 'Apply</button' + cashflowInlineHtmlClose
            + '<button type=button class=cashflows-inline-transaction-clear' + cashflowInlineHtmlClose + 'Clear</button' + cashflowInlineHtmlClose
            + '</div' + cashflowInlineHtmlClose
            + '<div id=cashflowsInlineTransactionStatus class=cashflows-inline-transaction-status aria-live=polite'
            + cashflowInlineHtmlClose + '</div' + cashflowInlineHtmlClose
            + '<div class=cashflows-inline-transaction-table-wrap' + cashflowInlineHtmlClose
            + '<table id=cashflowsInlineTransactionTable class=cashflows-inline-transaction-table' + cashflowInlineHtmlClose
            + '</table' + cashflowInlineHtmlClose + '</div' + cashflowInlineHtmlClose
            + '<footer class=cashflows-inline-transaction-footer' + cashflowInlineHtmlClose
            + '<span id=cashflowsInlineTransactionPageInfo' + cashflowInlineHtmlClose + '</span' + cashflowInlineHtmlClose
            + '<div class=cashflows-inline-transaction-pagination' + cashflowInlineHtmlClose
            + '<button type=button class=cashflows-inline-transaction-previous' + cashflowInlineHtmlClose + 'Previous</button' + cashflowInlineHtmlClose
            + '<button type=button class=cashflows-inline-transaction-next' + cashflowInlineHtmlClose + 'Next</button' + cashflowInlineHtmlClose
            + '</div' + cashflowInlineHtmlClose + '</footer' + cashflowInlineHtmlClose
            + '</section' + cashflowInlineHtmlClose;
        document.body.appendChild(drawer);
        cashflowInlineTransactionDrawer = drawer;
        const close = function () { closeCashflowTransactionDrawer(true); };
        drawer.querySelector('[data-cashflows-inline-transaction-close]').addEventListener('click', close);
        drawer.querySelector('.cashflows-inline-transaction-close').addEventListener('click', close);
        drawer.querySelector('.cashflows-inline-transaction-apply').addEventListener('click', function () {
            cashflowInlineTransactionPage = 1;
            loadCashflowTransactionDrawer(true);
        });
        drawer.querySelector('.cashflows-inline-transaction-clear').addEventListener('click', function () {
            drawer.querySelector('#cashflowsInlineTransactionSearch').value = '';
            drawer.querySelector('#cashflowsInlineTransactionPeriod').value = '';
            drawer.querySelector('#cashflowsInlineTransactionMode').value = cashflowTransactionModeForContext(cashflowInlineTransactionContext);
            syncCashflowTransactionModeLabel();
            cashflowInlineTransactionPage = 1;
            loadCashflowTransactionDrawer(true);
        });
        drawer.querySelector('#cashflowsInlineTransactionPeriod').addEventListener('change', function () {
            cashflowInlineTransactionPage = 1;

        });
        drawer.querySelector('#cashflowsInlineTransactionMode').addEventListener('change', function () {
            cashflowInlineTransactionPage = 1;
            syncCashflowTransactionModeLabel();
        });
        drawer.querySelector('#cashflowsInlineTransactionSearch').addEventListener('keydown', function (event) {
            if (event.key === 'Enter') {
                cashflowInlineTransactionPage = 1;
                loadCashflowTransactionDrawer(true);
            }
        });
        drawer.querySelector('.cashflows-inline-transaction-previous').addEventListener('click', function () {
            if (cashflowInlineTransactionPage > 1) {
                cashflowInlineTransactionPage -= 1;
                loadCashflowTransactionDrawer(true);
            }
        });
        drawer.querySelector('.cashflows-inline-transaction-next').addEventListener('click', function () {
            const pageCount = Math.max(1, Math.ceil(Number(drawer.dataset.totalRows || 0) / cashflowInlineTransactionLimit));
            if (cashflowInlineTransactionPage < pageCount) {
                cashflowInlineTransactionPage += 1;
                loadCashflowTransactionDrawer(true);
            }
        });
        document.addEventListener('keydown', function (event) {
            if (!drawer.hidden && event.key === 'Escape') closeCashflowTransactionDrawer(true);
        });
        return drawer;
    }
    function cashflowDrawerPeriods() {
        return (((lastCashflowsResult || {}).preview || {}).periods || []);
    }
    function configureCashflowTransactionPeriods() {
        const drawer = ensureCashflowTransactionDrawer();
        const select = drawer.querySelector('#cashflowsInlineTransactionPeriod');
        select.innerHTML = '<option value' + cashflowInlineHtmlClose + 'All selected periods</option' + cashflowInlineHtmlClose;
        cashflowDrawerPeriods().forEach(function (period) {
            const option = document.createElement('option');
            option.value = period.key;
            option.textContent = period.label || period.key;
            select.appendChild(option);
        });
        select.value = '';
    }
    function cashflowDrawerFilters() {
        const drawer = ensureCashflowTransactionDrawer();
        const selectedKey = drawer.querySelector('#cashflowsInlineTransactionPeriod').value;
        const selected = cashflowDrawerPeriods().find(function (period) { return String(period.key) === String(selectedKey); });
        return {
            start: selected ? selected.start : cashflowLoadedScope().period_start,
            end: selected ? selected.end : cashflowLoadedScope().period_end,
            search: drawer.querySelector('#cashflowsInlineTransactionSearch').value.trim(),
            mode: drawer.querySelector('#cashflowsInlineTransactionMode').value || 'combined'
        };
    }
    function setCashflowTransactionDrawerBusy(isBusy) {
        const drawer = ensureCashflowTransactionDrawer();
        ['.cashflows-inline-transaction-apply', '.cashflows-inline-transaction-clear', '#cashflowsInlineTransactionSearch', '#cashflowsInlineTransactionPeriod', '.cashflows-inline-transaction-previous', '.cashflows-inline-transaction-next'].forEach(function (selector) {
            const control = drawer.querySelector(selector);
            if (control) control.disabled = isBusy;
        });
        const dialog = drawer.querySelector('.cashflows-inline-transaction-dialog');
        if (dialog) {
            dialog.classList.toggle('cashflows-inline-transaction-loading', isBusy);
            dialog.setAttribute('aria-busy', isBusy ? 'true' : 'false');
        }
        drawer.querySelector('#cashflowsInlineTransactionStatus').textContent = '';
        drawer.querySelector('#cashflowsInlineTransactionStatus').hidden = true;
    }
    function cashflowDrawerRawValue(row, aliases) {
        const raw = row.row_data || {};
        for (let index = 0; index < aliases.length; index += 1) {
            const value = raw[aliases[index]];
            if (value !== undefined && value !== null && String(value).trim() !== '') return value;
        }
        return '';
    }
    function cashflowDrawerAmount(value) {
        if (value === undefined || value === null || value === '') return '';
        const amount = Number(value);
        return Number.isFinite(amount) ? formatInternationalAmount(amount) : value;
    }
    function renderCashflowTransactionDrawer(payload) {
        const drawer = ensureCashflowTransactionDrawer();
        const table = drawer.querySelector('#cashflowsInlineTransactionTable');
        const rows = payload.rows || [];
        const columns = ['S.No', 'Voucher Detail', 'Receipts (Cr)', 'Payments (Dr)', 'Transaction Date', 'Voucher ID'];
        const page = Number(payload.page) || 1;
        const totalRows = Number(payload.total_rows) || 0;
        const pageLimit = Number(payload.limit) || cashflowInlineTransactionLimit;
        const pageCount = Math.max(1, Math.ceil(totalRows / pageLimit));
        drawer.dataset.totalRows = String(totalRows);
        drawer.querySelector('#cashflowsInlineTransactionPageInfo').textContent = String(totalRows) + ' transaction line(s) - Page ' + String(page) + ' of ' + String(pageCount);
        drawer.querySelector('.cashflows-inline-transaction-previous').disabled = page <= 1;
        drawer.querySelector('.cashflows-inline-transaction-next').disabled = page >= pageCount;
        if (!rows.length) {
            table.innerHTML = '<tbody' + cashflowInlineHtmlClose + '<tr' + cashflowInlineHtmlClose
                + '<td class=cashflows-empty colspan=' + String(Math.max(columns.length, 1)) + cashflowInlineHtmlClose
                + 'No transactions found for the selected filters.</td' + cashflowInlineHtmlClose
                + '</tr' + cashflowInlineHtmlClose + '</tbody' + cashflowInlineHtmlClose;
            drawer.querySelector('#cashflowsInlineTransactionStatus').textContent = '';
            drawer.querySelector('#cashflowsInlineTransactionStatus').hidden = true;
            return;
        }
        const headerCells = columns.map(function (label) {
            return '<th' + cashflowInlineHtmlClose + escapeHtml(label) + '</th' + cashflowInlineHtmlClose;
        }).join('');
        let totalReceipts = 0;
        let totalPayments = 0;
        const bodyRows = rows.map(function (row, index) {
            const classification = row.classification || {};
            const creditAmount = Number(classification.credit);
            const debitAmount = Number(classification.debit);
            if (Number.isFinite(creditAmount)) totalReceipts += creditAmount;
            if (Number.isFinite(debitAmount)) totalPayments += debitAmount;
            const values = [
                cashflowDrawerRawValue(row, ['Voucher Detail', 'Detail', 'Description', 'Narration', 'Remarks']),
                cashflowDrawerAmount(classification.credit),
                cashflowDrawerAmount(classification.debit),
                classification.transaction_date || cashflowDrawerRawValue(row, ['Transaction Date', 'Date', 'Posting Date']),
                classification.voucher_id || cashflowDrawerRawValue(row, ['Voucher ID', 'Voucher', 'Transaction ID', 'Reference No'])
            ];
            const cells = [
                '<td' + cashflowInlineHtmlClose + String((page - 1) * pageLimit + index + 1) + '</td' + cashflowInlineHtmlClose
            ].concat(values.map(function (value) {
                return '<td' + cashflowInlineHtmlClose + escapeHtml(value) + '</td' + cashflowInlineHtmlClose;
            }));
            return '<tr' + cashflowInlineHtmlClose + cells.join('') + '</tr' + cashflowInlineHtmlClose;
        }).join('');
        const totalRow = '<tfoot' + cashflowInlineHtmlClose
            + '<tr class=cashflows-inline-transaction-total-row' + cashflowInlineHtmlClose
            + '<td class=cashflows-inline-transaction-total-empty' + cashflowInlineHtmlClose + '</td' + cashflowInlineHtmlClose
            + '<th scope=row class=cashflows-inline-transaction-total-label' + cashflowInlineHtmlClose + 'Total</th' + cashflowInlineHtmlClose
            + '<td class=cashflows-inline-transaction-total-amount' + cashflowInlineHtmlClose + escapeHtml(cashflowDrawerAmount(totalReceipts)) + '</td' + cashflowInlineHtmlClose
            + '<td class=cashflows-inline-transaction-total-amount' + cashflowInlineHtmlClose + escapeHtml(cashflowDrawerAmount(totalPayments)) + '</td' + cashflowInlineHtmlClose
            + '<td class=cashflows-inline-transaction-total-empty' + cashflowInlineHtmlClose + '</td' + cashflowInlineHtmlClose
            + '<td class=cashflows-inline-transaction-total-empty' + cashflowInlineHtmlClose + '</td' + cashflowInlineHtmlClose
            + '</tr' + cashflowInlineHtmlClose + '</tfoot' + cashflowInlineHtmlClose;
        table.innerHTML = '<thead' + cashflowInlineHtmlClose + '<tr' + cashflowInlineHtmlClose
            + headerCells + '</tr' + cashflowInlineHtmlClose + '</thead' + cashflowInlineHtmlClose
            + '<tbody' + cashflowInlineHtmlClose + bodyRows + '</tbody' + cashflowInlineHtmlClose
            + totalRow;
        drawer.querySelector('#cashflowsInlineTransactionStatus').textContent = '';
        drawer.querySelector('#cashflowsInlineTransactionStatus').hidden = true;
    }
    function cashflowTransactionTableMessage(drawer, message) {
        const table = drawer.querySelector('#cashflowsInlineTransactionTable');
        table.innerHTML = '<tbody' + cashflowInlineHtmlClose + '<tr' + cashflowInlineHtmlClose
            + '<td class=cashflows-empty colspan=6' + cashflowInlineHtmlClose + escapeHtml(message)
            + '</td' + cashflowInlineHtmlClose + '</tr' + cashflowInlineHtmlClose + '</tbody' + cashflowInlineHtmlClose;
        drawer.querySelector('#cashflowsInlineTransactionStatus').textContent = '';
        drawer.querySelector('#cashflowsInlineTransactionStatus').hidden = true;
    }
    function loadCashflowTransactionDrawer(resetRequest) {
        if (cashflowLoadedClientChanged()) return Promise.resolve(null);
        const drawer = ensureCashflowTransactionDrawer();
        const context = cashflowInlineTransactionContext;
        if (!context || drawer.hidden) return Promise.resolve(null);
        if (resetRequest) cashflowInlineTransactionRequestSerial += 1;
        if (cashflowInlineTransactionActiveController) cashflowInlineTransactionActiveController.abort();
        const requestSerial = cashflowInlineTransactionRequestSerial;
        const filters = cashflowDrawerFilters();
        const loadedKey = cashflowLedgerContextKey(cashflowLedgerContext());
        let selectedCodes;
        try {
            selectedCodes = cashflowAccountsInView(cashflowAccountCodes(cashflowLoadedScope()), filters.mode);
            if (cashflowAccountKey(selectedCodes) === 'invalid') {
                cashflowTransactionTableMessage(drawer, 'No selected accounts in this Cash/Bank view.');
                setCashflowTransactionDrawerBusy(false);
                return Promise.resolve(null);
            }
        } catch (error) {
            cashflowTransactionTableMessage(drawer, error.message);
            return Promise.resolve(null);
        }
        const currentPage = Math.max(1, Number(cashflowInlineTransactionPage) || 1);
        const key = cashflowDrawerTransactionKey(context, currentPage, filters);
        const cached = cashflowInlineTransactionCache.get(key);
        if (cached) {
            renderCashflowTransactionDrawer(cached);
            setCashflowTransactionDrawerBusy(false);
            return Promise.resolve(cached);
        }
        const configuration = (lastCashflowsResult || {}).configuration || {};
        const templateId = configuration.source_template_id;
        if (!templateId) {
            cashflowTransactionTableMessage(drawer, 'Transaction source is not configured.');
            return Promise.resolve(null);
        }
        const controller = new AbortController();
        cashflowInlineTransactionActiveController = controller;
        const params = new URLSearchParams({
            client_id: String(clientId),
            template_id: String(templateId),
            date_start: filters.start,
            date_end: filters.end,
            page: String(currentPage),
            limit: String(cashflowInlineTransactionLimit),
            classification_type: 'counter',
            account_code: context.accountCode,
            category: context.category,
            treasury_view: filters.mode
        });
        appendCashflowAccounts(params, selectedCodes);
        if (filters.search) params.set('search', filters.search);
        setCashflowTransactionDrawerBusy(true);
        return api('/api/data/cashflows-classification-preview?' + params.toString(), { signal: controller.signal })
            .then(function (payload) {
                if (requestSerial !== cashflowInlineTransactionRequestSerial || drawer.hidden
                    || loadedKey !== cashflowLedgerContextKey(cashflowLedgerContext())) return null;
                if (cashflowAccountKey(cashflowAccountCodes(payload)) !== cashflowAccountKey(selectedCodes)) throw new Error('Account scope changed. Reload Cashflows.');
                cashflowInlineTransactionCache.set(key, payload);
                renderCashflowTransactionDrawer(payload);
                return payload;
            })
            .catch(function (error) {
                if (error.name === 'AbortError') return null;
                if (requestSerial === cashflowInlineTransactionRequestSerial && !drawer.hidden) {
                    cashflowTransactionTableMessage(drawer, 'Unable to load transactions: ' + error.message);
                }
                return null;
            })
            .finally(function () {
                if (cashflowInlineTransactionActiveController === controller) cashflowInlineTransactionActiveController = null;
                if (requestSerial === cashflowInlineTransactionRequestSerial && !drawer.hidden) setCashflowTransactionDrawerBusy(false);
            });
    }
    function clearCashflowTransactionDrawerContent(drawer) {
        const table = drawer.querySelector('#cashflowsInlineTransactionTable');
        if (table) table.innerHTML = '';
        drawer.dataset.totalRows = '0';
        const pageInfo = drawer.querySelector('#cashflowsInlineTransactionPageInfo');
        if (pageInfo) pageInfo.textContent = '';
        ['.cashflows-inline-transaction-previous', '.cashflows-inline-transaction-next'].forEach(function (selector) {
            const button = drawer.querySelector(selector);
            if (button) button.disabled = true;
        });
        const status = drawer.querySelector('#cashflowsInlineTransactionStatus');
        if (status) {
            status.textContent = '';
            status.hidden = true;
        }
    }
    function openCashflowTransactionDrawer(button) {
        const drawer = ensureCashflowTransactionDrawer();
        const category = decodeURIComponent(button.dataset.cashflowInlineCategory || '');
        const section = decodeURIComponent(button.dataset.cashflowInlineSection || 'Receipts');
        const accountCode = decodeURIComponent(button.dataset.cashflowInlineAccount || '');
        const accountName = decodeURIComponent(button.dataset.cashflowInlineAccountName || '');
        const treasuryType = decodeURIComponent(button.dataset.cashflowInlineTreasury || '');
        if (!category || !accountCode) return;
        cashflowInlineTransactionFocus = button;
        cashflowInlineTransactionContext = { section: section, category: category, accountCode: accountCode, accountName: accountName, treasuryType: treasuryType };
        cashflowInlineTransactionPage = 1;
        drawer.querySelector('#cashflowsInlineTransactionCategory').textContent = category;
        drawer.querySelector('#cashflowsInlineTransactionAccount').textContent = [accountCode, accountName].filter(Boolean).join(' / ') || '-';
        drawer.querySelector('#cashflowsInlineTransactionMode').value = cashflowTransactionModeForContext(cashflowInlineTransactionContext);
        syncCashflowTransactionModeLabel();
        drawer.querySelector('#cashflowsInlineTransactionSearch').value = '';
        configureCashflowTransactionPeriods();
        clearCashflowTransactionDrawerContent(drawer);
        drawer.hidden = false;
        document.body.classList.add('cashflows-inline-transaction-open');
        drawer.querySelector('#cashflowsInlineTransactionSearch').focus();
        loadCashflowTransactionDrawer(true);
    }
    function closeCashflowTransactionDrawer(restoreFocus) {
        cashflowInlineTransactionRequestSerial += 1;
        if (cashflowInlineTransactionActiveController) cashflowInlineTransactionActiveController.abort();
        cashflowInlineTransactionActiveController = null;
        if (cashflowInlineTransactionDrawer) cashflowInlineTransactionDrawer.hidden = true;
        if (cashflowInlineTransactionDrawer) clearCashflowTransactionDrawerContent(cashflowInlineTransactionDrawer);
        setCashflowTransactionDrawerBusy(false);
        document.body.classList.remove('cashflows-inline-transaction-open');
        const focus = cashflowInlineTransactionFocus;
        cashflowInlineTransactionContext = null;
        cashflowInlineTransactionFocus = null;
        if (restoreFocus && focus && focus.isConnected) focus.focus();
    }
    function cashflowInlineCategoryButton(category, section) {
        return '<button type=button class=cashflows-inline-expand data-cashflow-inline-category=' + encodeURIComponent(category) + ' data-cashflow-inline-section=' + section + ' aria-expanded=false>' + escapeHtml(category) + '</button>';
    }
    function cashflowInlineCategoryCell(category, section) {
        return cashflowInlineCategoryButton(category, section);
    }
    async function loadCashflowInlineLedgers(button, host, category, section, periods, isSplit, requestedPage) {
        const page = Math.max(1, Number(requestedPage) || 1);
        const context = cashflowLedgerContext(cashflowSelection.start, cashflowSelection.end);
        const contextKey = cashflowLedgerContextKey(context);
        const key = cashflowInlineLedgerKey(section, category) + '|page=' + page;
        const controller = new AbortController();
        const generation = cashflowInlineLedgerGeneration;
        cashflowInlineLedgerControllers.set(key, controller);
        try {
            const fresh = await ensureCashflowRevisionFresh(false, true);
            if (!fresh) throw new Error('Report data changed or could not be checked. Reload Cashflows to continue.');
            if (!cashflowLoadedLedgerContextMatches(context)) throw new Error('Report scope changed. Reload Cashflows to continue.');
            if (generation !== cashflowInlineLedgerGeneration
                    || contextKey !== cashflowDrilldownContextKey(cashflowSelection.start, cashflowSelection.end)) return null;
            const baseline = takeCashflowLedger('baseline', contextKey);
            let payload;
            if (baseline) {
                // One complete baseline is shared by every category and the full ledger view.
                const filters = {search: '', category: category.toLowerCase(), accountHead: '', periodKey: '',
                    treasuryType: context.treasury_view === 'cash' ? 'cash' : (context.treasury_view === 'bank' ? 'bank' : '')};
                const rows = baseline.rows.filter(function (row) { return cashflowDrilldownRowMatches(row, filters); });
                payload = {rows: rows, total_rows: rows.length, page: 1, page_count: 1};
            } else {
                payload = takeCashflowLedger('inline', key);
                if (!payload) {
                    const params = new URLSearchParams({client_id: String(clientId), period_start: context.period_start,
                        period_end: context.period_end, group_by: context.group_by, treasury_view: context.treasury_view,
                        category: category, page: String(page), limit: String(cashflowInlineLedgerLimit)});
                    appendCashflowAccounts(params, cashflowAccountCodes(context));
                    payload = await api('/api/reconciliation/cashflows/drilldown?' + params.toString(), {signal: controller.signal});
                    if (generation !== cashflowInlineLedgerGeneration) return null;
                    if (!cashflowLedgerResponseMatches(payload, context)) {
                        throw new Error('Report data changed. Reload Cashflows to continue.');
                    }
                    if (!Number.isInteger(payload.total_rows) || !Number.isInteger(payload.page_count)
                            || payload.page !== page || !Array.isArray(payload.rows)
                            || payload.rows.length !== Math.min(cashflowInlineLedgerLimit, Math.max(0,
                                payload.total_rows - (page - 1) * cashflowInlineLedgerLimit))) {
                        throw new Error('Incomplete account details. Reload Cashflows and try again.');
                    }
                    const compact = Object.assign({}, payload);
                    delete compact.base_rows;
                    rememberCashflowLedger('inline', key, compact);
                }
            }
            if (generation !== cashflowInlineLedgerGeneration || controller.signal.aborted) return null;
            renderCashflowInlineLedgerHost(host, payload, category, section, periods, isSplit);
            return payload;
        } catch (error) {
            if (error.name === 'AbortError') return null;
            const pair = host && host.left ? host : {left: host, right: null};
            if (pair.left && pair.left.isConnected) {
                pair.left.querySelector('.cashflows-inline-ledger-host').innerHTML = '<span class=cashflows-inline-ledger-error>Unable to load ledgers: ' + escapeHtml(error.message) + '</span>';
                if (pair.right && pair.right.isConnected) pair.right.querySelector('.cashflows-inline-ledger-host').innerHTML = '';
            }
            return null;
        } finally {
            if (cashflowInlineLedgerControllers.get(key) === controller) cashflowInlineLedgerControllers.delete(key);
            if (button.isConnected) button.disabled = false;
        }
    }
    function handleCashflowSummaryClick(event) {
        const balanceButton = event.target.closest('[data-cashflow-balance-toggle]');
        if (balanceButton) {
            const kind = balanceButton.dataset.cashflowBalanceToggle;
            cashflowBalanceExpanded[kind] = !cashflowBalanceExpanded[kind];
            const host = element('cashflowsSummaryTable');
            host.querySelectorAll('[data-cashflow-balance-child="' + kind + '"]').forEach(function (row) {
                row.hidden = !cashflowBalanceExpanded[kind];
            });
            balanceButton.setAttribute('aria-expanded', String(cashflowBalanceExpanded[kind]));
            requestAnimationFrame(function () { syncCashflowSummaryPaneRows(host); syncCashflowSummaryUniversalScrollbar(host); });
            return;
        }
        const pageButton = event.target.closest('[data-cashflow-inline-page]');
        if (pageButton) { changeCashflowInlineLedgerPage(pageButton); return; }
        const ledgerButton = event.target.closest('[data-cashflow-inline-ledger]');
        if (ledgerButton) { toggleCashflowInlineLedger(ledgerButton); return; }
        const button = event.target.closest('[data-cashflow-inline-category]');
        if (button) toggleCashflowInlineCategory(button);
    }
    function changeCashflowInlineLedgerPage(button) {
        const category = decodeURIComponent(button.dataset.cashflowInlineCategory || '');
        const section = decodeURIComponent(button.dataset.cashflowInlineSection || 'Receipts');
        const table = element('cashflowsSummaryTable');
        const categoryButton = Array.from(table.querySelectorAll('[data-cashflow-inline-category]:not([data-cashflow-inline-ledger]):not([data-cashflow-inline-page])')).find(function (candidate) {
            return decodeURIComponent(candidate.dataset.cashflowInlineCategory || '') === category
                && candidate.dataset.cashflowInlineSection === section;
        });
        if (!categoryButton) return;
        const group = cashflowInlineLedgerGroupValue(section, category);
        table.querySelectorAll('[data-cashflow-inline-ledger-group]').forEach(function (row) {
            if (row.dataset.cashflowInlineLedgerGroup === group) row.remove();
        });
        categoryButton.setAttribute('aria-expanded', 'false');
        toggleCashflowInlineCategory(categoryButton, Number(button.dataset.cashflowInlinePage));
    }
    function toggleCashflowInlineCategory(button, requestedPage) {
        const category = decodeURIComponent(button.dataset.cashflowInlineCategory || '');
        const section = button.dataset.cashflowInlineSection || 'Receipts';
        const row = button.closest('tr');
        const table = element('cashflowsSummaryTable');
        if (!row || !table || !category) return;
        const group = cashflowInlineLedgerGroupValue(section, category);
        const existingRows = Array.from(table.querySelectorAll('[data-cashflow-inline-ledger-group]')).filter(function (candidate) {
            return candidate.dataset.cashflowInlineLedgerGroup === group;
        });
        if (existingRows.length) {
            existingRows.forEach(function (candidate) { candidate.remove(); });
            button.setAttribute('aria-expanded', 'false');
            return;
        }
        const preview = (lastCashflowsResult || {}).preview || {};
        const periods = cashflowPreviewPeriods(preview);
        const isSplit = String(preview.treasury_view || cashflowTreasuryView || 'combined') === 'split';
        const colspan = isSplit ? 1 + ((periods.length + 1) * 4) + 2 : periods.length + 4;
        const paneLayout = table.querySelector('.cashflows-summary-pane-layout');
        if (paneLayout) {
            const rowId = row.dataset.cashflowSummaryRowId;
            const rightRow = Array.from(paneLayout.querySelectorAll('.cashflows-summary-scroll-table tbody tr[data-cashflow-summary-row-id]')).find(function (candidate) {
                return candidate.dataset.cashflowSummaryRowId === rowId;
            });
            if (!rightRow) return;
            const leftTemplate = document.createElement('tbody');
            leftTemplate.innerHTML = cashflowInlineLedgerHostMarkup(category, section, 1);
            const rightTemplate = document.createElement('tbody');
            rightTemplate.innerHTML = cashflowInlineLedgerHostMarkup(category, section, Math.max(colspan - 1, 1));
            const leftHost = leftTemplate.firstElementChild;
            const rightHost = rightTemplate.firstElementChild;
            row.parentNode.insertBefore(leftHost, row.nextSibling);
            rightRow.parentNode.insertBefore(rightHost, rightRow.nextSibling);
            button.setAttribute('aria-expanded', 'true');
            button.disabled = true;
            loadCashflowInlineLedgers(button, { left: leftHost, right: rightHost }, category, section, periods, isSplit, requestedPage);
            return;
        }
        const template = document.createElement('tbody');
        template.innerHTML = cashflowInlineLedgerHostMarkup(category, section, colspan, true);
        const host = template.firstElementChild;
        row.parentNode.insertBefore(host, row.nextSibling);
        button.setAttribute('aria-expanded', 'true');
        button.disabled = true;
        loadCashflowInlineLedgers(button, host, category, section, periods, isSplit, requestedPage);
    }
    function loadCashflowDrilldown(resetPage) {
        const start = cashflowLoadedScope().period_start;
        const end = cashflowLoadedScope().period_end;
        if (!clientId || !start || !end) return Promise.resolve(null);
        if (resetPage) cashflowDrilldownPage = 1;
        const expectedContext = cashflowLedgerContext(start, end);
        if (!cashflowLoadedLedgerContextMatches(expectedContext)) {
            showCashflowToast('Report scope changed. Reload Cashflows to continue.', true);
            return Promise.resolve(null);
        }
        const expectedKey = cashflowLedgerContextKey(expectedContext);
        const filters = cashflowDrilldownFilterState();
        const baseline = takeCashflowLedger('baseline', cashflowDrilldownContextKey(start, end));
        if (baseline) {
            return ensureCashflowRevisionFresh(false, true).then(function (isFresh) {
                const currentBaseline = cashflowDrilldownBaseCache.get(cashflowDrilldownContextKey(start, end));
                if (!isFresh) { showCashflowToast('Report data changed or could not be checked. Reload Cashflows to continue.', true); return null; }
                if (expectedKey !== cashflowDrilldownContextKey(start, end)) return null;
                if (!currentBaseline) return loadCashflowDrilldown(resetPage);
                const localResult = cashflowDrilldownLocalResult(currentBaseline, filters, cashflowDrilldownPage);
                cashflowDrilldownPage = localResult.page;
                lastCashflowDrilldownResult = localResult;
                renderCashflowDrilldown(localResult);
                return localResult;
            });
        }
        if (cashflowDrilldownController) cashflowDrilldownController.abort();
        const controller = new AbortController();
        cashflowDrilldownController = controller;
        const generation = cashflowDrilldownGeneration;
        const params = new URLSearchParams({
            client_id: String(clientId),
            period_start: start,
            period_end: end,
            group_by: expectedContext.group_by,
            treasury_view: expectedContext.treasury_view,
            page: String(cashflowDrilldownPage),
            limit: String(cashflowDrilldownLimit),
        });
        appendCashflowAccounts(params, cashflowAccountCodes(expectedContext));
        const search = element('cashflowsDrilldownSearch').value.trim();
        const category = element('cashflowsDrilldownCategory').value.trim();
        const accountHead = element('cashflowsDrilldownAccountHead').value.trim();
        const treasuryType = element('cashflowsDrilldownTreasury').value;
        const periodKey = element('cashflowsDrilldownPeriod').value;
        if (search) params.set('search', search);
        if (category) params.set('category', category);
        if (accountHead) params.set('account_head', accountHead);
        if (treasuryType) params.set('treasury_type', treasuryType);
        if (periodKey) params.set('period_key', periodKey);
        showCashflowProcessing('Loading Cashflows ledger details', 'Preparing ledger details.');
        renderCashflowDrilldownContext(null);
        setCashflowProcessingStep('load', 'Loading ledger details.');
        element('cashflowsLedgerDrilldownTable').innerHTML = '<tbody><tr><td class=cashflows-empty colspan=7>Loading ledger details...</td></tr></tbody>';
        return api('/api/reconciliation/cashflows/drilldown?' + params.toString(), { signal: controller.signal })
            .then(function (result) {
                if (generation !== cashflowDrilldownGeneration) return null;
                if (expectedKey !== cashflowDrilldownContextKey(start, end)) return null;
                if (!cashflowLedgerResponseMatches(result, expectedContext)) {
                    throw new Error('Report data changed. Reload Cashflows to continue.');
                }
                setCashflowProcessingStep('render', 'Rendering ledger drill-down details.');
                cacheCashflowDrilldownBaseline(result);
                lastCashflowDrilldownResult = result;
                renderCashflowDrilldown(result);
                return result;
            })
            .catch(function (error) {
                if (error.name === 'AbortError') return null;
                showCashflowToast(error.message, true);
                return null;
            })
            .finally(function () {
                if (cashflowDrilldownController === controller) {
                    cashflowDrilldownController = null;
                    hideCashflowProcessing();
                    syncCashflowDrilldownPagination(lastCashflowDrilldownResult);
                }
            });
    }
    function cashflowSummaryRows(preview) {
        if (!preview) return [];
        return Array.isArray(preview.treasury_view_totals) ? preview.treasury_view_totals : (preview.category_totals || []);
    }
    function cashflowBalanceEvidence(preview, periods) {
        if (cashflowBalancesPending || !periods.length) return null;
        const evidence = preview.balance_reconciliation;
        if (!evidence || !evidence.evidence_fingerprint || !Array.isArray(evidence.periods)
                || evidence.periods.length !== periods.length) return null;
        const matches = evidence.periods.every(function (period, index) {
            return period && period.key === String(periods[index].key) && period.start === periods[index].start
                && period.end === periods[index].end && Array.isArray(period.accounts)
                && period.accounts.every(function (account) { return account && typeof account.account_code === 'string'; });
        });
        return matches ? evidence : null;
    }
    function cashflowBalanceAmount(value, reason, local, review) {
        if (typeof value !== 'number' || !Number.isFinite(value)) {
            const text = cashflowBalancesPending ? 'Checking…' : 'Unavailable';
            return '<span class="cashflows-balance-unavailable" title="' + escapeHtml(reason) + '">' + text + '</span>';
        }
        const absolute = Math.abs(value);
        const amount = value === 0 ? '0' : local ? formatLocalAmount(absolute)
            : absolute.toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 2});
        const label = amount + (value === 0 ? '' : value < 0 ? ' Cr' : ' Dr');
        return '<span class="cashflows-balance-number' + (review ? ' cashflows-balance-review' : '')
            + '" title="' + escapeHtml(label + (reason ? ' — ' + reason : '')) + '">' + escapeHtml(label) + '</span>';
    }
    function cashflowBalancePart(evidence, periods, field) {
        if (!evidence || field === 'mixed') return null;
        if (field === 'total') return evidence;
        const child = (evidence.treasury_breakdown || {})[field];
        return child && Array.isArray(child.periods) && child.periods.length === periods.length
            && child.periods.every(function (p, i) { return p && p.key === periods[i].key && p.start === periods[i].start && p.end === periods[i].end; }) ? child : null;
    }
    function cashflowBalanceRows(preview, periods, kind, splitFields) {
        const evidence = cashflowBalanceEvidence(preview, periods);
        const opening = kind === 'opening';
        const field = opening ? 'opening_total' : 'closing_total';
        const calculatedField = opening ? 'calculated_opening' : 'calculated_closing';
        const accountField = opening ? 'opening_balance' : 'closing_balance';
        const fields = splitFields || ['total'];
        const expanded = cashflowBalanceExpanded[kind];
        const selectedIndex = opening ? 0 : periods.length - 1;
        const reason = cashflowBalancesPending ? 'Checking current ledger evidence for this report.'
            : 'Balances use the full report scope. Expand to review individual accounts and unavailable evidence.';
        function part(fieldName) {
            return cashflowBalancePart(evidence, periods, fieldName);
        }
        function cells(account, calculated) {
            let html = '';
            periods.concat([null]).forEach(function (period, index) {
                const position = period ? index : selectedIndex;
                fields.forEach(function (treasuryField) {
                    const source = part(treasuryField);
                    const entry = source && source.periods[position];
                    let value = entry ? entry[calculated ? calculatedField : field] : null;
                    let explanation = reason;
                    let review = entry && (entry.status === 'Variance' || entry.status === 'Unavailable');
                    if (source && source.status === 'Not applicable') explanation = 'No ' + treasuryField + ' accounts are configured; this column is not applicable.';
                    if (account) {
                        const row = evidence && evidence.periods[position].accounts.find(function (a) { return a.account_code === account.account_code; });
                        const applies = treasuryField === 'total' || (row && String(row.treasury_type).toLowerCase() === treasuryField);
                        if (!applies || treasuryField === 'mixed') {
                            html += '<td class="cashflows-table-number" title="Not applicable to this account">—</td>';
                            return;
                        }
                        value = row && row[accountField];
                        const details = Array.isArray(evidence.diagnostics) ? evidence.diagnostics : [];
                        const diagnostic = details.find(function (d) { return d.account_code === account.account_code && (!d.period_key || d.period_key === periods[position].key); });
                        explanation = diagnostic ? diagnostic.message : reason;
                        review = row && row.status !== 'Ready';
                    } else if (treasuryField === 'mixed') {
                        html += '<td class="cashflows-table-number" title="Ledger balances belong to configured Bank or Cash accounts">—</td>';
                        return;
                    }
                    if (entry && calculated) {
                        const difference = opening ? entry.opening_total - entry.calculated_opening : entry.variance;
                        if (typeof entry[calculatedField] === 'number' && typeof entry[field] === 'number' && Math.abs(difference) > 0.01) {
                            review = true;
                            explanation = 'Difference from ledger balance: ' + difference.toLocaleString(undefined, {maximumFractionDigits: 2});
                        }
                    }
                    html += '<td class="cashflows-table-number">' + cashflowBalanceAmount(value, explanation, false, review) + '</td>';
                });
            });
            let local = null;
            if (account && evidence) {
                const selected = evidence.periods[selectedIndex].accounts.find(function (a) { return a.account_code === account.account_code; });
                local = selected && selected[accountField];
            } else if (evidence) local = evidence.periods[selectedIndex][calculated ? calculatedField : field];
            return html + '<td aria-label="Percentage does not apply to balances"></td><td class="cashflows-table-number">'
                + cashflowBalanceAmount(local, reason, true, false) + '</td>';
        }
        const label = opening ? 'Opening balances—as per ledgers' : 'Closing balances—as per ledgers';
        const state = cashflowBalancesPending ? 'Checking balances…' : !evidence ? 'Balance evidence unavailable'
            : !evidence.periods[0].accounts.length ? 'No accounts configured for this view'
            : evidence.coverage_complete && fields.every(function (name) { return name === 'mixed' || !!part(name); }) ? '' : 'Incomplete ledger evidence';
        let html = '<tr class="cashflows-balance-row" data-cashflow-balance-kind="' + kind + '"><td><button type="button" class="cashflows-inline-expand cashflows-balance-toggle" data-cashflow-balance-toggle="'
            + kind + '" aria-expanded="' + String(expanded) + '" title="Expand or collapse account balances">' + label + '</button>'
            + (state ? '<span class="cashflows-balance-state">' + state + '</span>' : '') + '</td>' + cells(null, false) + '</tr>';
        const accounts = evidence && evidence.periods.length ? evidence.periods[0].accounts.slice() : [];
        accounts.sort(function (a, b) { return String(a.account_name || '').localeCompare(String(b.account_name || '')) || a.account_code.localeCompare(b.account_code); });
        accounts.forEach(function (account) {
            const name = account.account_name && account.account_name !== account.account_code ? account.account_name : 'Account name unavailable';
            html += '<tr class="cashflows-balance-account" data-cashflow-balance-child="' + kind + '"' + (expanded ? '' : ' hidden') + '><td><span class="cashflows-balance-account-label" title="Account code: '
                + escapeHtml(account.account_code) + '" tabindex="0">' + escapeHtml(name) + '</span></td>' + cells(account, false) + '</tr>';
        });
        html += '<tr class="cashflows-balance-calculated" data-cashflow-balance-child="' + kind + '"' + (expanded ? '' : ' hidden') + '><td>'
            + (opening ? 'Calculated opening balance' : 'Calculated closing balance') + '</td>' + cells(null, true) + '</tr>';
        return html;
    }
    function cashflowNetMovementRow(preview, periods, splitFields) {
        const evidence = cashflowBalanceEvidence(preview, periods);
        const fields = splitFields || ['total'];
        function movement(index, field) {
            if (!evidence) return null;
            if (field === 'mixed') return (evidence.unresolved_movements || {})[periods[index].key];
            const part = cashflowBalancePart(evidence, periods, field);
            return part && part.periods[index] ? part.periods[index].cashflow_net_movement : null;
        }
        function amount(value, local) {
            if (typeof value !== 'number' || !Number.isFinite(value)) return cashflowBalanceAmount(null, 'Current full-report movement is unavailable.', false, false);
            const text = value === 0 ? '0' : local ? formatLocalAmount(value) : value.toLocaleString(undefined, {maximumFractionDigits: 2});
            return '<span class="cashflows-balance-number">' + escapeHtml(text) + '</span>';
        }
        let html = '<tr class="cashflows-balance-net"><td title="Net movement for the full selected report, independent of category filtering">Net cash movement</td>';
        periods.forEach(function (_, index) { fields.forEach(function (field) { html += '<td class="cashflows-table-number">' + amount(movement(index, field), false) + '</td>'; }); });
        fields.forEach(function (field) {
            const part = cashflowBalancePart(evidence, periods, field);
            let value = part && part.overall && part.overall.cashflow_net_movement;
            if (field === 'mixed' && evidence) {
                const values = periods.map(function (_, index) { return movement(index, field); });
                value = values.every(function (n) { return typeof n === 'number' && Number.isFinite(n); }) ? values.reduce(function (sum, n) { return sum + n; }, 0) : null;
            }
            html += '<td class="cashflows-table-number">' + amount(value, false) + '</td>';
        });
        return html + '<td></td><td class="cashflows-table-number">' + amount(evidence && evidence.overall && evidence.overall.cashflow_net_movement, true) + '</td></tr>';
    }
    function cashflowMovementBridgeRows(preview, periods, splitFields) {
        const proof = preview.treasury_movement_proof || [];
        if (!proof.length) return '';
        const fields = splitFields || ['total'];
        const view = String(preview.treasury_view || 'combined');
        const explanation = 'Full report scope. Category net + pure internal transfers + mixed-voucher Treasury bridge = net cash movement. Mixed categories remain unallocated; the bridge uses actual Treasury legs and removes their already displayed Mixed category net. It does not allocate categories or confirm mixed transfers.';
        function value(period, field, kind) {
            const type = field === 'total' ? (view === 'bank' || view === 'cash' ? view : 'combined') : field;
            const row = proof.find(function (item) { return item.period_key === period.key && item.treasury_type === (type === 'mixed' ? 'combined' : type); });
            if (!row) return null;
            if (field === 'mixed') return kind === 'category_movement' ? row.unallocated_category_movement
                : kind === 'mixed_voucher_movement' ? -row.unallocated_category_movement : 0;
            return row[kind];
        }
        return [['category_movement', 'Category net movement'], ['internal_transfer_movement', 'Pure internal transfers'],
            ['mixed_voucher_movement', 'Mixed voucher Treasury bridge — allocation under review']].map(function (item) {
            const kind = item[0];
            if (kind === 'mixed_voucher_movement' && !proof.some(function (row) { return row.mixed_voucher_count > 0; })) return '';
            function cell(amount, local) {
                return '<td class="cashflows-table-number">' + (typeof amount === 'number' && Number.isFinite(amount)
                    ? '<span class="cashflows-balance-number">' + escapeHtml(local ? formatLocalAmount(amount) : formatInternationalAmount(amount)) + '</span>' : 'Unavailable') + '</td>';
            }
            let html = '<tr class="cashflows-balance-bridge"><td title="' + escapeHtml(explanation) + '">' + escapeHtml(item[1]) + '</td>';
            periods.forEach(function (period) { fields.forEach(function (field) { html += cell(value(period, field, kind)); }); });
            fields.forEach(function (field) {
                const values = periods.map(function (period) { return value(period, field, kind); });
                html += cell(values.every(function (amount) { return typeof amount === 'number'; }) ? values.reduce(function (sum, amount) { return sum + amount; }, 0) : null);
            });
            const local = periods.map(function (period) { return value(period, 'total', kind); });
            return html + '<td></td>' + cell(local.every(function (amount) { return typeof amount === 'number'; }) ? local.reduce(function (sum, amount) { return sum + amount; }, 0) : null, true) + '</tr>';
        }).join('');
    }
    function cashflowBalanceSplitFields(preview) {
        const allRows = cashflowSplitRows(preview);
        const periodKeys = cashflowPreviewPeriods(preview).map(function (period) { return String(period.key); });
        const showMixedColumn = allRows.some(function (row) {
            return Math.abs(Number(row.mixedTotal) || 0) > 0.005 || periodKeys.some(function (periodKey) {
                return Math.abs(Number((row.periods[periodKey] || {}).mixed) || 0) > 0.005;
            });
        });
        const visibleFields = showMixedColumn ? ['bank', 'cash', 'mixed', 'total'] : ['bank', 'cash', 'total'];
        return visibleFields;
    }
    function cashflowBalanceColumnWidths(source, rightCount, isSplit) {
        const balanceWidth = Array.from(source.querySelectorAll('.cashflows-balance-number')).reduce(function (width, cell) {
            return Math.max(width, Math.min(210, Math.round(cell.textContent.length * 7.2 + 24)));
        }, 112);
        return Array.from({length: rightCount}, function (_, index) {
            if (index >= rightCount - 2) return index === rightCount - 2 ? 100 : 120;
            return !isSplit && index === rightCount - 3 ? Math.max(128, balanceWidth) : balanceWidth;
        });
    }
    function refreshCashflowBalanceRows(preview) {
        // A ledger-only refresh must not close categories or a drilldown that
        // the user opened while the cached movement summary was available.
        const host = element('cashflowsSummaryTable');
        const leftBody = host.querySelector('.cashflows-summary-frozen-table tbody');
        const rightBody = host.querySelector('.cashflows-summary-scroll-table tbody');
        if (!leftBody || !rightBody) return false;
        const periods = cashflowPreviewPeriods(preview);
        const isSplit = String(preview.treasury_view || cashflowTreasuryView) === 'split';
        const fields = isSplit ? cashflowBalanceSplitFields(preview) : null;
        const table = document.createElement('table');
        table.innerHTML = '<tbody>' + cashflowBalanceRows(preview, periods, 'opening', fields)
            + cashflowMovementBridgeRows(preview, periods, fields) + cashflowNetMovementRow(preview, periods, fields)
            + cashflowBalanceRows(preview, periods, 'closing', fields) + '</tbody>';
        const moneyColumns = (periods.length + 1) * (fields ? fields.length : 1) + 2;
        const existingColumns = host.querySelectorAll('.cashflows-summary-scroll-table colgroup col').length;
        if (moneyColumns !== existingColumns) return false;
        const openingLeft = document.createDocumentFragment(), openingRight = document.createDocumentFragment();
        const closingLeft = document.createDocumentFragment(), closingRight = document.createDocumentFragment();
        Array.from(table.tBodies[0].rows).forEach(function (row, index) {
            const opening = (row.dataset.cashflowBalanceKind || row.dataset.cashflowBalanceChild) === 'opening';
            const parts = cashflowSummaryPaneRowParts(row, 'balance-' + (opening ? 'opening-' : 'closing-') + index, false);
            (opening ? openingLeft : closingLeft).appendChild(parts.left);
            (opening ? openingRight : closingRight).appendChild(parts.right);
        });
        host.querySelectorAll('.cashflows-balance-row, .cashflows-balance-account, .cashflows-balance-calculated, .cashflows-balance-net, .cashflows-balance-bridge').forEach(function (row) { row.remove(); });
        leftBody.insertBefore(openingLeft, leftBody.firstChild); rightBody.insertBefore(openingRight, rightBody.firstChild);
        leftBody.appendChild(closingLeft); rightBody.appendChild(closingRight);
        const widths = cashflowBalanceColumnWidths(host, moneyColumns, isSplit);
        host.querySelectorAll('.cashflows-summary-scroll-header-table, .cashflows-summary-scroll-table').forEach(function (paneTable) {
            paneTable.querySelectorAll('colgroup col').forEach(function (col, index) { col.style.width = widths[index] + 'px'; });
            paneTable.style.width = 'max(100%, ' + widths.reduce(function (sum, width) { return sum + width; }, 0) + 'px)';
            paneTable.style.minWidth = '100%';
        });
        requestAnimationFrame(function () {
            syncCashflowSummaryPaneRows(host); syncCashflowSummaryHeaderScroll(host); syncCashflowSummaryUniversalScrollbar(host);
        });
        return true;
    }
    function cashflowSplitRows(preview) {
        const periods = cashflowPreviewPeriods(preview);
        const periodKeys = periods.map(function (period) { return String(period.key); });
        const grouped = {};
        cashflowSummaryRows(preview).forEach(function (row) {
            const category = String(row.category || 'Unlabelled');
            if (!grouped[category]) grouped[category] = { category: category, periods: {}, bankTotal: 0, cashTotal: 0, mixedTotal: 0, totalRaw: 0 };
            const periodKey = String(row.period_key || '');
            if (!periodKey) return;
            if (!grouped[category].periods[periodKey]) grouped[category].periods[periodKey] = { bank: 0, cash: 0, mixed: 0, total: 0 };
            const values = grouped[category].periods[periodKey];
            values.bank += Number(row.bank_amount) || 0;
            values.cash += Number(row.cash_amount) || 0;
            values.mixed += Number(row.mixed_amount) || 0;
            values.total += Number(row.total_amount) || 0;
        });
        return Object.keys(grouped).map(function (category) {
            const row = grouped[category];
            periodKeys.forEach(function (periodKey) {
                if (!row.periods[periodKey]) row.periods[periodKey] = { bank: 0, cash: 0, mixed: 0, total: 0 };
                row.bankTotal += row.periods[periodKey].bank;
                row.cashTotal += row.periods[periodKey].cash;
                row.mixedTotal += row.periods[periodKey].mixed;
                row.totalRaw += row.periods[periodKey].total;
            });
            return row;
        });
    }
    function setCashflowSummaryFilterVisibility(visible) {
        const input = element('cashflowsSummaryFilter');
        const control = input ? input.closest('.cashflows-search-control') : null;
        if (!control) return;
        control.hidden = !visible;
        if (!visible) {
            cashflowSummaryFilter = '';
            input.value = '';
            const clearButton = element('cashflowsSummaryClear');
            if (clearButton) clearButton.hidden = true;
        }
    }
    function cashflowSummaryPaneRowParts(sourceRow, rowId, isHeaderContinuation) {
        const leftRow = sourceRow.cloneNode(false);
        const rightRow = sourceRow.cloneNode(false);
        leftRow.dataset.cashflowSummaryRowId = rowId;
        rightRow.dataset.cashflowSummaryRowId = rowId;
        const cells = Array.from(sourceRow.children);
        const frozenColumnCount = 1;
        if (sourceRow.classList.contains('cashflows-summary-section-row')) {
            const leftCell = cells[0] ? cells[0].cloneNode(true) : document.createElement('th');
            const rightCell = cells[0] ? cells[0].cloneNode(true) : document.createElement('th');
            const sourceColspan = Number(leftCell.getAttribute('colspan')) || cells.length || 1;
            leftCell.setAttribute('colspan', String(Math.min(frozenColumnCount, sourceColspan)));
            rightCell.setAttribute('colspan', String(Math.max(sourceColspan - frozenColumnCount, 1)));
            rightCell.textContent = '';
            leftRow.appendChild(leftCell);
            rightRow.appendChild(rightCell);
        } else if (isHeaderContinuation) {
            for (let index = 0; index < frozenColumnCount; index += 1) {
                const placeholder = document.createElement('th');
                placeholder.setAttribute('aria-hidden', 'true');
                leftRow.appendChild(placeholder);
            }
            cells.forEach(function (cell) { rightRow.appendChild(cell.cloneNode(true)); });
        } else {
            if (cells.length === 1 && (Number(cells[0].getAttribute('colspan')) || 1) > 1) {
                const leftCell = cells[0].cloneNode(true);
                const rightCell = cells[0].cloneNode(true);
                const sourceColspan = Number(cells[0].getAttribute('colspan')) || 1;
                leftCell.setAttribute('colspan', String(Math.min(frozenColumnCount, sourceColspan)));
                rightCell.setAttribute('colspan', String(Math.max(sourceColspan - frozenColumnCount, 1)));
                rightCell.textContent = '';
                leftRow.appendChild(leftCell);
                rightRow.appendChild(rightCell);
            } else {
                cells.slice(0, frozenColumnCount).forEach(function (cell) { leftRow.appendChild(cell.cloneNode(true)); });
                cells.slice(frozenColumnCount).forEach(function (cell) { rightRow.appendChild(cell.cloneNode(true)); });
            }
        }
        if (!rightRow.children.length) {
            const filler = document.createElement(sourceRow.parentElement && sourceRow.parentElement.tagName === 'THEAD' ? 'th' : 'td');
            filler.setAttribute('aria-hidden', 'true');
            rightRow.appendChild(filler);
        }
        return { left: leftRow, right: rightRow };
    }
    function fitCashflowSummaryFrozenPane(host) {
        const layout = host && host.querySelector('.cashflows-summary-pane-layout');
        if (!layout) return;
        const available = host.getBoundingClientRect().width;
        const preferred = Number(layout.dataset.cashflowCategoryWidth) || 300;
        // Leave room for one readable amount on a narrow screen. Frozen labels
        // wrap instead of occupying almost the entire horizontal viewport.
        const width = available > 0 && available < 600 ? Math.min(preferred, Math.max(132, available - 152)) : preferred;
        layout.style.setProperty('--cashflows-summary-frozen-width', width + 'px');
        host.querySelectorAll('.cashflows-summary-frozen-header-table, .cashflows-summary-frozen-table').forEach(function (table) {
            table.style.width = width + 'px'; table.style.minWidth = width + 'px';
            const col = table.querySelector('colgroup col');
            if (col) col.style.width = width + 'px';
        });
    }
    function syncCashflowSummaryPaneRows(host) {
        const layout = host && host.querySelector('.cashflows-summary-pane-layout');
        if (!layout) return;
        fitCashflowSummaryFrozenPane(host);
        const leftRows = Array.from(layout.querySelectorAll('.cashflows-summary-frozen-table tbody tr[data-cashflow-summary-row-id]'));
        const rightRows = Array.from(layout.querySelectorAll('.cashflows-summary-scroll-table tbody tr[data-cashflow-summary-row-id]'));
        const rightById = new Map(rightRows.map(function (row) { return [row.dataset.cashflowSummaryRowId, row]; }));
        leftRows.forEach(function (leftRow) {
            const rightRow = rightById.get(leftRow.dataset.cashflowSummaryRowId);
            if (!rightRow) return;
            leftRow.style.height = '';
            rightRow.style.height = '';
            const height = Math.max(leftRow.getBoundingClientRect().height, rightRow.getBoundingClientRect().height);
            if (height > 0) {
                leftRow.style.height = height + 'px';
                rightRow.style.height = height + 'px';
            }
        });
    }
    function syncCashflowSummaryHeaderScroll(host) {
        const layout = host && host.querySelector('.cashflows-summary-pane-layout');
        const pane = layout && layout.querySelector('.cashflows-summary-scroll-pane');
        const headerTable = layout && layout.querySelector('.cashflows-summary-scroll-header-table');
        if (!pane || !headerTable) return;
        if (pane.dataset.cashflowSummaryHeaderBound !== 'true') {
            pane.addEventListener('scroll', function () {
                headerTable.style.transform = 'translate3d(' + (-pane.scrollLeft) + 'px, 0, 0)';
            }, { passive: true });
            pane.dataset.cashflowSummaryHeaderBound = 'true';
        }
        headerTable.style.transform = 'translate3d(' + (-pane.scrollLeft) + 'px, 0, 0)';
    }
    function syncCashflowSummaryUniversalScrollbar(host) {
        const wrapper = host && host.closest('.cashflows-summary-table-wrap');
        const initialPane = host && host.querySelector('.cashflows-summary-scroll-pane');
        if (!wrapper || !initialPane) return;
        let scrollbar = wrapper.querySelector('.cashflows-summary-universal-scrollbar');
        if (!scrollbar) {
            scrollbar = document.createElement('div');
            scrollbar.className = 'cashflows-summary-universal-scrollbar';
            scrollbar.setAttribute('role', 'scrollbar');
            scrollbar.setAttribute('tabindex', '0');
            scrollbar.setAttribute('aria-orientation', 'horizontal');
            scrollbar.setAttribute('aria-label', 'Cashflows horizontal scroll');
            scrollbar.setAttribute('aria-hidden', 'true');
            const track = document.createElement('div');
            track.className = 'cashflows-summary-universal-scrollbar-track';
            const thumb = document.createElement('div');
            thumb.className = 'cashflows-summary-universal-scrollbar-thumb';
            track.appendChild(thumb);
            scrollbar.appendChild(track);
            wrapper.appendChild(scrollbar);
        }
        const track = scrollbar.querySelector('.cashflows-summary-universal-scrollbar-track');
        const thumb = scrollbar.querySelector('.cashflows-summary-universal-scrollbar-thumb');
        const reportSection = wrapper.closest('.cashflows-report-section') || wrapper;
        const update = function () {
            const currentPane = wrapper.querySelector('.cashflows-summary-scroll-pane');
            if (!currentPane || !track || !thumb) {
                scrollbar.hidden = true;
                scrollbar.setAttribute('aria-hidden', 'true');
                return;
            }
            const reportRect = reportSection.getBoundingClientRect();
            const wrapperRect = wrapper.getBoundingClientRect();
            const visible = reportRect.bottom > 72.5 && reportRect.top < window.innerHeight;
            const overflowing = currentPane.scrollWidth > currentPane.clientWidth + 1;
            const active = visible && overflowing;
            scrollbar.hidden = !active;
            scrollbar.setAttribute('aria-hidden', active ? 'false' : 'true');
            if (!active) return;
            const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
            scrollbar.style.left = Math.max(12, Math.round(wrapperRect.left)) + 'px';
            scrollbar.style.right = Math.max(12, Math.round(viewportWidth - wrapperRect.right)) + 'px';
            const trackWidth = track.clientWidth;
            const maxScroll = Math.max(0, currentPane.scrollWidth - currentPane.clientWidth);
            const thumbWidth = Math.min(trackWidth, Math.max(48, Math.round(trackWidth * currentPane.clientWidth / currentPane.scrollWidth)));
            const travel = Math.max(0, trackWidth - thumbWidth);
            const progress = maxScroll ? currentPane.scrollLeft / maxScroll : 0;
            thumb.style.width = thumbWidth + 'px';
            thumb.style.transform = 'translateX(' + Math.round(travel * progress) + 'px)';
            scrollbar.setAttribute('aria-valuemin', '0');
            scrollbar.setAttribute('aria-valuemax', String(maxScroll));
            scrollbar.setAttribute('aria-valuenow', String(Math.round(currentPane.scrollLeft)));
        };
        const scrollToTrackPoint = function (clientX) {
            const currentPane = wrapper.querySelector('.cashflows-summary-scroll-pane');
            if (!currentPane || !track || !thumb) return;
            const trackRect = track.getBoundingClientRect();
            const travel = Math.max(0, track.clientWidth - thumb.getBoundingClientRect().width);
            const maxScroll = Math.max(0, currentPane.scrollWidth - currentPane.clientWidth);
            if (!travel || !maxScroll) return;
            const offset = clientX - trackRect.left - thumb.getBoundingClientRect().width / 2;
            currentPane.scrollLeft = Math.max(0, Math.min(maxScroll, offset / travel * maxScroll));
        };
        if (scrollbar.dataset.cashflowUniversalBound !== 'true') {
            let dragState = null;
            thumb.addEventListener('pointerdown', function (event) {
                const currentPane = wrapper.querySelector('.cashflows-summary-scroll-pane');
                if (!currentPane) return;
                dragState = { pointerId: event.pointerId, startX: event.clientX, startScroll: currentPane.scrollLeft };
                thumb.classList.add('is-dragging');
                thumb.setPointerCapture(event.pointerId);
                event.preventDefault();
            });
            thumb.addEventListener('pointermove', function (event) {
                if (!dragState || dragState.pointerId !== event.pointerId) return;
                const currentPane = wrapper.querySelector('.cashflows-summary-scroll-pane');
                if (!currentPane) return;
                const travel = Math.max(0, track.clientWidth - thumb.getBoundingClientRect().width);
                const maxScroll = Math.max(0, currentPane.scrollWidth - currentPane.clientWidth);
                if (travel && maxScroll) currentPane.scrollLeft = Math.max(0, Math.min(maxScroll, dragState.startScroll + (event.clientX - dragState.startX) * maxScroll / travel));
            });
            const endDrag = function (event) {
                if (!dragState || dragState.pointerId !== event.pointerId) return;
                dragState = null;
                thumb.classList.remove('is-dragging');
            };
            thumb.addEventListener('pointerup', endDrag);
            thumb.addEventListener('pointercancel', endDrag);
            track.addEventListener('pointerdown', function (event) { if (event.target !== thumb) scrollToTrackPoint(event.clientX); });
            scrollbar.addEventListener('keydown', function (event) {
                const currentPane = wrapper.querySelector('.cashflows-summary-scroll-pane');
                if (!currentPane) return;
                const step = Math.max(48, Math.round(currentPane.clientWidth * .15));
                let next = null;
                if (event.key === 'Home') next = 0;
                else if (event.key === 'End') next = currentPane.scrollWidth - currentPane.clientWidth;
                else if (event.key === 'ArrowLeft' || event.key === 'PageUp') next = currentPane.scrollLeft - (event.key === 'PageUp' ? currentPane.clientWidth : step);
                else if (event.key === 'ArrowRight' || event.key === 'PageDown') next = currentPane.scrollLeft + (event.key === 'PageDown' ? currentPane.clientWidth : step);
                if (next === null) return;
                event.preventDefault();
                currentPane.scrollLeft = Math.max(0, Math.min(currentPane.scrollWidth - currentPane.clientWidth, next));
            });
            window.addEventListener('resize', update, { passive: true });
            window.addEventListener('scroll', update, { passive: true });
            scrollbar.dataset.cashflowUniversalBound = 'true';
        }
        const currentPane = wrapper.querySelector('.cashflows-summary-scroll-pane');
        if (currentPane && currentPane.dataset.cashflowUniversalBound !== 'true') {
            currentPane.addEventListener('scroll', update, { passive: true });
            currentPane.dataset.cashflowUniversalBound = 'true';
        }
        update();
    }
    function syncCashflowSummaryRowHover(host) {
        const layout = host && host.querySelector('.cashflows-summary-pane-layout');
        const bodyLayout = layout && layout.querySelector('.cashflows-summary-body-layout');
        if (!bodyLayout || bodyLayout.dataset.cashflowSummaryHoverBound === 'true') return;
        const rowsForId = function (rowId) {
            return Array.from(bodyLayout.querySelectorAll('tr[data-cashflow-summary-row-id]')).filter(function (candidate) {
                return candidate.dataset.cashflowSummaryRowId === rowId;
            });
        };
        const setRowHover = function (rowId, active) {
            rowsForId(rowId).forEach(function (candidate) {
                candidate.classList.toggle('cashflows-summary-row-hover', active);
            });
        };
        bodyLayout.addEventListener('pointerover', function (event) {
            const row = event.target.closest && event.target.closest('tr[data-cashflow-summary-row-id]');
            if (!row || !bodyLayout.contains(row)) return;
            const related = event.relatedTarget;
            const relatedRow = related && related.closest ? related.closest('tr[data-cashflow-summary-row-id]') : null;
            if (relatedRow && relatedRow.dataset.cashflowSummaryRowId === row.dataset.cashflowSummaryRowId) return;
            setRowHover(row.dataset.cashflowSummaryRowId, true);
        });
        bodyLayout.addEventListener('pointerout', function (event) {
            const row = event.target.closest && event.target.closest('tr[data-cashflow-summary-row-id]');
            if (!row || !bodyLayout.contains(row)) return;
            const related = event.relatedTarget;
            const relatedRow = related && related.closest ? related.closest('tr[data-cashflow-summary-row-id]') : null;
            if (relatedRow && relatedRow.dataset.cashflowSummaryRowId === row.dataset.cashflowSummaryRowId) return;
            setRowHover(row.dataset.cashflowSummaryRowId, false);
        });
        bodyLayout.dataset.cashflowSummaryHoverBound = 'true';
    }
    function cashflowSummaryLogicalColumnCount(row) {
        return Array.from((row || {}).children || []).reduce(function (count, cell) {
            return count + (Number(cell.getAttribute('colspan')) || 1);
        }, 0);
    }
    function cashflowSummaryCategoryWidth(sourceTable) {
        const labels = Array.from(sourceTable.querySelectorAll('tbody tr')).filter(function (row) {
            return !row.hidden && !row.classList.contains('cashflows-summary-section-row')
                && !row.classList.contains('cashflows-summary-total-row')
                && row.children.length > 0
                && !((Number(row.children[0].getAttribute('colspan')) || 1) > 1);
        }).map(function (row) { return row.children[0].textContent.trim(); });
        const longestLabel = labels.reduce(function (longest, label) { return Math.max(longest, label.length); }, 0);
        return Math.min(520, Math.max(300, Math.round(longestLabel * 7.2 + 30)));
    }
    function renderCashflowSummaryPaneLayout(container, markup, isSplit) {
        const sourceTable = document.createElement('table');
        sourceTable.className = 'cashflows-table';
        sourceTable.innerHTML = markup;
        const sourceRow = sourceTable.querySelector('thead tr') || sourceTable.querySelector('tbody tr');
        const logicalCount = Math.max(cashflowSummaryLogicalColumnCount(sourceRow), 2);
        const categoryWidth = cashflowSummaryCategoryWidth(sourceTable);
        const rightCount = Math.max(logicalCount - 1, 1);
        const rightWidths = cashflowBalanceColumnWidths(sourceTable, rightCount, isSplit);
        const addColGroup = function (table, widths, fillAvailable) {
            const colgroup = document.createElement('colgroup');
            widths.forEach(function (width) {
                const col = document.createElement('col');
                col.style.width = width + 'px';
                colgroup.appendChild(col);
            });
            table.insertBefore(colgroup, table.firstChild);
            const totalWidth = widths.reduce(function (sum, width) { return sum + width; }, 0);
            table.style.width = fillAvailable ? 'max(100%, ' + totalWidth + 'px)' : totalWidth + 'px';
            table.style.minWidth = fillAvailable ? '100%' : totalWidth + 'px';
        };
        const layout = document.createElement('div');
        layout.className = 'cashflows-summary-pane-layout';
        layout.dataset.cashflowCategoryWidth = String(categoryWidth);
        layout.style.setProperty('--cashflows-summary-frozen-width', categoryWidth + 'px');
        const stickyHeader = document.createElement('div');
        stickyHeader.className = 'cashflows-summary-sticky-header';
        const frozenHeaderPane = document.createElement('div');
        frozenHeaderPane.className = 'cashflows-summary-frozen-header-pane';
        const scrollHeaderViewport = document.createElement('div');
        scrollHeaderViewport.className = 'cashflows-summary-scroll-header-viewport';
        const frozenHeaderTable = document.createElement('table');
        frozenHeaderTable.className = 'cashflows-summary-header-table cashflows-summary-frozen-header-table';
        const scrollHeaderTable = document.createElement('table');
        scrollHeaderTable.className = 'cashflows-summary-header-table cashflows-summary-scroll-header-table';
        const frozenPane = document.createElement('div');
        frozenPane.className = 'cashflows-summary-frozen-pane';
        const scrollPane = document.createElement('div');
        scrollPane.className = 'cashflows-summary-scroll-pane';
        const bodyLayout = document.createElement('div');
        bodyLayout.className = 'cashflows-summary-body-layout';
        const frozenTable = document.createElement('table');
        frozenTable.className = 'cashflows-table cashflows-summary-pane-table cashflows-summary-frozen-table';
        const scrollTable = document.createElement('table');
        scrollTable.className = 'cashflows-table cashflows-summary-pane-table cashflows-summary-scroll-table';
        const sourceHead = sourceTable.querySelector('thead');
        if (sourceHead) {
            const frozenHead = document.createElement('thead');
            const scrollHead = document.createElement('thead');
            Array.from(sourceHead.children).forEach(function (sourceRow, index) {
                const parts = cashflowSummaryPaneRowParts(sourceRow, 'header-' + index, sourceRow.classList.contains('cashflows-split-header-sub'));
                frozenHead.appendChild(parts.left);
                scrollHead.appendChild(parts.right);
            });
            frozenHeaderTable.appendChild(frozenHead);
            scrollHeaderTable.appendChild(scrollHead);
        }
        ['tbody', 'tfoot'].forEach(function (tagName) {
            const sourceSection = sourceTable.querySelector(tagName);
            if (!sourceSection) return;
            const frozenSection = document.createElement(tagName);
            const scrollSection = document.createElement(tagName);
            Array.from(sourceSection.children).forEach(function (sourceRow, index) {
                const parts = cashflowSummaryPaneRowParts(sourceRow, tagName + '-' + index, false);
                frozenSection.appendChild(parts.left);
                scrollSection.appendChild(parts.right);
            });
            frozenTable.appendChild(frozenSection);
            scrollTable.appendChild(scrollSection);
        });
        addColGroup(frozenHeaderTable, [categoryWidth]);
        addColGroup(frozenTable, [categoryWidth]);
        addColGroup(scrollHeaderTable, rightWidths, true);
        addColGroup(scrollTable, rightWidths, true);
        frozenHeaderPane.appendChild(frozenHeaderTable);
        scrollHeaderViewport.appendChild(scrollHeaderTable);
        stickyHeader.appendChild(frozenHeaderPane);
        stickyHeader.appendChild(scrollHeaderViewport);
        frozenPane.appendChild(frozenTable);
        scrollPane.appendChild(scrollTable);
        bodyLayout.appendChild(frozenPane);
        bodyLayout.appendChild(scrollPane);
        layout.appendChild(stickyHeader);
        layout.appendChild(bodyLayout);
        container.className = 'cashflows-summary-table-host';
        container.classList.toggle('cashflows-summary-split-table', !!isSplit);
        container.innerHTML = '';
        container.appendChild(layout);
        fitCashflowSummaryFrozenPane(container);
        if (container.dataset.cashflowSummaryResizeBound !== 'true') {
            let frame = 0;
            window.addEventListener('resize', function () {
                if (frame) return;
                frame = requestAnimationFrame(function () {
                    frame = 0;
                    syncCashflowSummaryPaneRows(container); syncCashflowSummaryHeaderScroll(container); syncCashflowSummaryUniversalScrollbar(container);
                });
            }, {passive: true});
            container.dataset.cashflowSummaryResizeBound = 'true';
        }
        requestAnimationFrame(function () {
            syncCashflowSummaryPaneRows(container);
            syncCashflowSummaryHeaderScroll(container);
            syncCashflowSummaryUniversalScrollbar(container);
            syncCashflowSummaryRowHover(container);
        });
    }
    function renderCashflowSplitSummary(preview, table) {
        const periods = cashflowPreviewPeriods(preview);
        const periodKeys = periods.map(function (period) { return String(period.key); });
        const allRows = cashflowSplitRows(preview);
        const visibleFields = cashflowBalanceSplitFields(preview);
        const showMixedColumn = visibleFields.indexOf('mixed') >= 0;
        const hasActivityRows = allRows.some(function (row) {
            return periodKeys.some(function (periodKey) {
                const values = row.periods[periodKey] || {};
                return ['bank', 'cash', 'mixed', 'total'].some(function (field) { return Math.abs(Number(values[field]) || 0) > 0; });
            });
        });
        setCashflowSummaryFilterVisibility(allRows.length > 0 && (!hideCashflowZeroRows || hasActivityRows));
        const filter = cashflowSummaryFilter.trim().toLowerCase();
        const filteredRows = allRows.filter(function (row) {
            return !filter || row.category.toLowerCase().indexOf(filter) >= 0;
        });
        const summaryRows = hideCashflowZeroRows ? filteredRows.filter(function (row) {
            return periodKeys.some(function (periodKey) {
                const values = row.periods[periodKey] || {};
                return ['bank', 'cash', 'mixed', 'total'].some(function (field) { return Math.abs(Number(values[field]) || 0) > 0; });
            });
        }) : filteredRows;
        const sectionRows = { Receipts: [], Payments: [] };
        summaryRows.forEach(function (row) {
            const total = Number(row.totalRaw) || 0;
            sectionRows[total < 0 ? 'Payments' : 'Receipts'].push({
                category: row.category,
                periods: row.periods,
                bankTotal: row.bankTotal,
                cashTotal: row.cashTotal,
                mixedTotal: row.mixedTotal,
                totalRaw: total,
                total: Math.abs(total),
            });
        });
        const subColumns = showMixedColumn ? ['Bank', 'Cash', 'Mixed / unresolved', 'Total'] : ['Bank', 'Cash', 'Total'];
        const columnCount = 1 + ((periods.length + 1) * subColumns.length) + 2;
        let html = '<thead><tr class="cashflows-split-header-top"><th rowspan="2">Category</th>';
        periods.forEach(function (period) { html += '<th colspan="' + subColumns.length + '">' + escapeHtml(period.label) + '</th>'; });
        html += '<th colspan="' + subColumns.length + '">Selected total</th><th rowspan="2">% of section</th><th rowspan="2">Local total</th></tr><tr class="cashflows-split-header-sub">';
        periods.concat([{ label: 'Selected total' }]).forEach(function () {
            subColumns.forEach(function (label) { html += '<th class="cashflows-table-number">' + escapeHtml(label) + '</th>'; });
        });
        html += '</tr></thead><tbody>';
        html += cashflowBalanceRows(preview, periods, 'opening', visibleFields);
        if (!summaryRows.length) html += '<tr><td class="cashflows-empty" colspan="' + columnCount + '">No Cash Flow activity was found for this period.</td></tr>';
        ['Receipts', 'Payments'].forEach(function (section) {
            const rows = sectionRows[section];
            if (!rows.length) return;
            const basis = rows.reduce(function (sum, row) { return sum + row.total; }, 0);
            html += '<tr class="cashflows-summary-section-row cashflows-summary-section-' + section.toLowerCase() + '"><th colspan="' + columnCount + '">' + escapeHtml(section) + '</th></tr>';
            rows.forEach(function (row) {
                let cells = ['<td class="cashflows-table-text cashflows-summary-category-value">' + escapeHtml(row.category) + '</td>'];
                cells[0] = '<td class="cashflows-table-text cashflows-summary-category-value">' + cashflowInlineCategoryCell(row.category, section) + '</td>';
                periodKeys.forEach(function (periodKey) {
                    const values = row.periods[periodKey] || { bank: 0, cash: 0, mixed: 0, total: 0 };
                    visibleFields.forEach(function (field) {
                        const className = 'cashflows-table-number' + (field === 'total' ? ' cashflows-summary-total-value' : '');
                        cells.push('<td class="' + className + '">' + escapeHtml(formatCashflowSectionAmount(values[field], section)) + '</td>');
                    });
                });
                const totalValues = { bank: row.bankTotal, cash: row.cashTotal, mixed: row.mixedTotal, total: row.totalRaw };
                visibleFields.forEach(function (field) {
                    const className = 'cashflows-table-number' + (field === 'total' ? ' cashflows-summary-total-value' : '');
                    cells.push('<td class="' + className + '">' + escapeHtml(formatCashflowSectionAmount(totalValues[field], section)) + '</td>');
                });
                cells.push('<td class="cashflows-table-number cashflows-summary-percentage-value">' + escapeHtml(formatPercentage(basis ? row.total / basis : 0)) + '</td>');
                cells.push('<td class="cashflows-table-number cashflows-summary-local-total-value">' + escapeHtml(formatLocalAmount(row.total)) + '</td>');
                html += '<tr>' + cells.join('') + '</tr>';
            });
            const totalFields = { bank: 0, cash: 0, mixed: 0, total: 0 };
            html += '<tr class="cashflows-summary-total-row"><th>Total ' + escapeHtml(section) + '</th>';
            periodKeys.forEach(function (periodKey) {
                const values = { bank: 0, cash: 0, mixed: 0, total: 0 };
                rows.forEach(function (row) {
                    const rowValues = row.periods[periodKey] || {};
                    ['bank', 'cash', 'mixed', 'total'].forEach(function (field) { values[field] += Number(rowValues[field]) || 0; totalFields[field] += Number(rowValues[field]) || 0; });
                });
                visibleFields.forEach(function (field) {
                    const className = 'cashflows-table-number' + (field === 'total' ? ' cashflows-summary-total-value' : '');
                    // Every monthly subtotal follows its section's direction,
                    // including Total; reversals keep the opposite sign.
                    html += '<td class="' + className + '">' + escapeHtml(formatCashflowSectionAmount(values[field], section)) + '</td>';
                });
            });
            visibleFields.forEach(function (field) {
                const className = 'cashflows-table-number' + (field === 'total' ? ' cashflows-summary-total-value' : '');
                html += '<td class="' + className + '">' + escapeHtml(field === 'total' ? formatInternationalAmount(basis) : formatCashflowSectionAmount(totalFields[field], section)) + '</td>';
            });
            html += '<td class="cashflows-table-number cashflows-summary-percentage-value">100.00%</td><td class="cashflows-table-number cashflows-summary-local-total-value">' + escapeHtml(formatLocalAmount(basis)) + '</td></tr>';
        });
        html += cashflowMovementBridgeRows(preview, periods, visibleFields);
        html += cashflowNetMovementRow(preview, periods, visibleFields);
        html += cashflowBalanceRows(preview, periods, 'closing', visibleFields);
        renderCashflowSummaryPaneLayout(table, html + '</tbody>', true);
    }
    function renderCashflowSummary(preview) {
        const table = element('cashflowsSummaryTable');
        const isSplit = String((preview || {}).treasury_view || cashflowTreasuryView || 'combined') === 'split';
        table.classList.toggle('cashflows-summary-split-table', isSplit);
        if (isSplit) {
            renderCashflowSplitSummary(preview, table);
            return;
        }
        const periods = cashflowPreviewPeriods(preview);
        const periodKeys = periods.map(function (period) { return period.key; });
        const rawRows = cashflowSummaryRows(preview);
        const hasActivityRows = rawRows.some(function (row) {
            const values = periodKeys.map(function (key) { return Number(row[key]) || 0; });
            const total = Number(row.Total);
            return values.some(function (value) { return Math.abs(value) > 0; }) || (Number.isFinite(total) && Math.abs(total) > 0);
        });
        setCashflowSummaryFilterVisibility(rawRows.length > 0 && (!hideCashflowZeroRows || hasActivityRows));
        const filter = cashflowSummaryFilter.trim().toLowerCase();
        const filteredRows = rawRows.filter(function (row) {
            return !filter || String(row.category || '').toLowerCase().indexOf(filter) >= 0;
        });
        const summaryRows = hideCashflowZeroRows ? filteredRows.filter(function (row) {
            const values = periodKeys.map(function (key) { return Number(row[key]) || 0; });
            const total = Number(row.Total);
            return values.some(function (value) { return Math.abs(value) > 0; }) || (Number.isFinite(total) && Math.abs(total) > 0);
        }) : filteredRows;
        const sectionRows = { Receipts: [], Payments: [] };
        summaryRows.forEach(function (row) {
            const total = Number.isFinite(Number(row.Total))
                ? Number(row.Total)
                : periodKeys.reduce(function (sum, key) { return sum + (Number(row[key]) || 0); }, 0);
            sectionRows[total < 0 ? 'Payments' : 'Receipts'].push({
                category: row.category || 'Unlabelled',
                values: periodKeys.map(function (key) { return Number(row[key]) || 0; }),
                total: Math.abs(total)
            });
        });
        const columns = ['Category'].concat(periods.map(function (period) { return period.label; })).concat(['Total', '% of section', 'Local total']);
        let html = '<thead><tr>' + columns.map(function (label) {
            return '<th>' + escapeHtml(label) + '</th>';
        }).join('') + '</tr></thead><tbody>';
        html += cashflowBalanceRows(preview, periods, 'opening');
        if (!summaryRows.length) html += '<tr><td class="cashflows-empty" colspan="' + columns.length + '">No Cash Flow activity was found for this period.</td></tr>';
        ['Receipts', 'Payments'].forEach(function (section) {
            const rows = sectionRows[section];
            if (!rows.length) return;
            const basis = rows.reduce(function (sum, row) { return sum + row.total; }, 0);
            html += '<tr class="cashflows-summary-section-row cashflows-summary-section-' + section.toLowerCase() + '"><th colspan="' + columns.length + '">' + escapeHtml(section) + '</th></tr>';
            rows.forEach(function (row) {
                const cells = [cashflowInlineCategoryCell(row.category, section)];
                row.values.forEach(function (value) { cells.push(escapeHtml(formatCashflowSectionAmount(value, section))); });
                cells.push(escapeHtml(formatInternationalAmount(row.total)));
                cells.push(escapeHtml(formatPercentage(basis ? row.total / basis : 0)));
                cells.push(escapeHtml(formatLocalAmount(row.total)));
                html += '<tr>' + cells.map(function (cell, index) {
                    let className = '';
                    if (index === 0) className = 'cashflows-summary-category-value';
                    else if (index === periods.length + 1) className = 'cashflows-summary-total-value';
                    else if (index === periods.length + 2) className = 'cashflows-summary-percentage-value';
                    else if (index === periods.length + 3) className = 'cashflows-summary-local-total-value';
                    return '<td' + (className ? ' class="' + className + '"' : '') + '>' + cell + '</td>';
                }).join('') + '</tr>';
            });
            const periodTotals = periodKeys.map(function (_, index) { return rows.reduce(function (sum, row) { return sum + row.values[index]; }, 0); });
            html += '<tr class="cashflows-summary-total-row"><th>' + escapeHtml('Total ' + section) + '</th>';
            periodTotals.forEach(function (value) { html += '<td>' + escapeHtml(formatCashflowSectionAmount(value, section)) + '</td>'; });
            html += '<td class="cashflows-summary-total-value">' + escapeHtml(formatInternationalAmount(basis)) + '</td><td class="cashflows-summary-percentage-value">100.00%</td><td class="cashflows-summary-local-total-value">' + escapeHtml(formatLocalAmount(basis)) + '</td></tr>';
        });
        html += cashflowMovementBridgeRows(preview, periods);
        html += cashflowNetMovementRow(preview, periods);
        html += cashflowBalanceRows(preview, periods, 'closing');
        renderCashflowSummaryPaneLayout(table, html + '</tbody>', false);
    }
    async function openCashflowDrilldown() {
        const button = element('cashflowsLedgerDrilldownButton');
        const periodSelect = element('cashflowsDrilldownPeriod');
        const preview = (lastCashflowsResult || {}).preview || {};
        const treasurySelect = element('cashflowsDrilldownTreasury');
        if (treasurySelect) treasurySelect.value = cashflowDefaultTreasuryType();
        const periods = cashflowPreviewPeriods(preview);
        if (!button || !periodSelect || !periods.length) return;
        periodSelect.innerHTML = '';
        const allOption = document.createElement('option');
        allOption.value = '';
        allOption.textContent = 'All periods';
        periodSelect.appendChild(allOption);
        periods.forEach(function (period) {
            const option = document.createElement('option');
            option.value = period.key;
            option.textContent = period.label || period.key;
            periodSelect.appendChild(option);
        });
        setCashflowDrilldownMode(true);
        renderCashflowDrilldownContext(lastCashflowsResult);
        button.disabled = true;
        await loadCashflowDrilldown(true);
        button.disabled = false;
    }
    function closeCashflowDrilldown() {
        cashflowDrilldownGeneration += 1;
        if (cashflowDrilldownController) {
            cashflowDrilldownController.abort();
            cashflowDrilldownController = null;
            hideCashflowProcessing();
        }
        lastCashflowDrilldownResult = null;
        setCashflowDrilldownMode(false);
    }
    // A separate page view retains the Cashflows DOM, including filters and expanded rows.
    const reconciliationState = {result: null, scope: null, view: 'combined', tab: 'working',
        controller: null, generation: 0, opener: null, lastRequest: {}, needsReportReload: false, loading: null, pending: null, exportTask: null};
    // Disposable tab memory only. Store serialized data once, never report DOM.
    const reconciliationCache = new Map();
    let reconciliationCacheGeneration = 0;
    const RECONCILIATION_CACHE_MAX_BYTES = 2 * 1024 * 1024;
    const RECONCILIATION_CACHE_MAX_ENTRIES = 20;
    function reconciliationContextKey(scope) {
        return JSON.stringify([1, scope.client_id, scope.template_id, scope.period_start, scope.period_end,
            scope.group_by, scope.treasury_view, scope.configuration_key, scope.report_revision,
            cashflowAccountKey(cashflowAccountCodes(scope))]);
    }
    function clearReconciliationCache(context, identity) {
        reconciliationCacheGeneration += 1;
        reconciliationCache.forEach(function (entry, key) {
            if (!context || (entry.context === context && (!identity || entry.identity !== identity))) reconciliationCache.delete(key);
        });
    }
    function rememberReconciliation(key, context, result, childField) {
        reconciliationCache.delete(key);
        if (result.cache_schema !== 1 || !/^[a-f0-9]{64}$/.test(result.cache_identity || '')) return;
        let value = result;
        if (childField) {
            value = {};
            ['cache_schema', 'cache_identity', 'client_id', 'template_id', 'period_start', 'period_end', 'group_by',
                'treasury_view', 'configuration_key', 'report_revision', 'evidence_fingerprint', childField].forEach(function (field) {
                value[field] = result[field];
            });
        }
        const json = JSON.stringify(value), bytes = new TextEncoder().encode(json + key + context).length;
        // An oversized complete voucher is shown in full and simply not retained.
        if (bytes > RECONCILIATION_CACHE_MAX_BYTES) return;
        reconciliationCache.set(key, {context: context, identity: result.cache_identity, json: json, bytes: bytes});
        let total = Array.from(reconciliationCache.values()).reduce(function (sum, entry) {return sum + entry.bytes;}, 0);
        while (reconciliationCache.size > RECONCILIATION_CACHE_MAX_ENTRIES || total > RECONCILIATION_CACHE_MAX_BYTES) {
            const oldest = reconciliationCache.keys().next().value;
            total -= reconciliationCache.get(oldest).bytes; reconciliationCache.delete(oldest);
        }
    }
    function takeReconciliation(key) {
        const entry = reconciliationCache.get(key);
        if (!entry) return null;
        reconciliationCache.delete(key); reconciliationCache.set(key, entry);
        return entry;
    }
    function reconciliationMoney(value) {
        if (value == null || value === '' || !Number.isFinite(Number(value))) return 'Unavailable';
        const amount = Number(value);
        const label = Math.abs(amount).toLocaleString('en-GB', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        return amount < 0 ? '(' + label + ')' : label;
    }
    function reconciliationPeriodLabel(scope) {
        // Keep both exact dates visible, including partial months and year boundaries.
        const format = function (value) {
            return fromIso(value).toLocaleDateString('en-GB', {day: '2-digit', month: 'short', year: 'numeric'});
        };
        return format(scope.period_start) + ' – ' + format(scope.period_end);
    }
    function cancelReconciliationExport() {
        const task = reconciliationState.exportTask;
        if (!task) return;
        task.cancelled = true;
        reconciliationState.exportTask = null;
        if (task.id) discardReconciliationExport(task);
    }
    function discardReconciliationExport(task) {
        fetch('/api/reconciliation/cashflows/reconciliation/exports/' + encodeURIComponent(task.id)
            + '?client_id=' + encodeURIComponent(task.scope.client_id),
            {method: 'DELETE', cache: 'no-store', keepalive: true}).catch(function () {});
    }
    function cancelReconciliationRequest() {
        cancelReconciliationExport();
        reconciliationState.generation += 1;
        reconciliationState.pending = null;
        if (reconciliationState.controller) reconciliationState.controller.abort();
        reconciliationState.controller = null;
        finishReconciliationProcessing();
    }
    function finishReconciliationProcessing() {
        const loading = reconciliationState.loading;
        if (!loading) return;
        reconciliationState.loading = null;
        window.LoadingUI.hideSpinner(loading.title);
        window.LoadingUI.hideProgressBar(loading.progress);
        window.LoadingUI.hideSkeleton(loading.skeleton);
        window.LoadingUI.stopGlobalLoading(loading.token);
        loading.target.removeAttribute('aria-busy');
        if (loading.target === element('reconProcessing') || loading.target === element('reconExportProcessing')) loading.target.hidden = true;
        syncReconciliationControls();
    }
    function syncReconciliationControls() {
        const state = reconciliationState, busy = !!state.loading;
        element('reconReload').disabled = busy;
        element('reconExport').disabled = busy || !state.result || !state.result.evidence_fingerprint || !state.result.cache_identity;
        element('reconView').disabled = busy || !state.result;
        element('reconIncludeMatched').disabled = busy || !state.result;
        document.querySelectorAll('#cashflowReconciliation [role=tab]').forEach(function (button) { button.disabled = !state.result; });
    }
    function startReconciliationProcessing(options) {
        const support = !!options.support_key, differences = !!options.posting_differences;
        const target = element(options.export ? 'reconExportProcessing' : support ? 'reconSupport' : differences ? 'reconDifferenceRows' : 'reconProcessing');
        const title = options.export ? 'Preparing reconciliation Excel…' : support ? 'Loading adjustment voucher support…' : differences
            ? 'Loading voucher differences — page ' + String(options.page || 1) + '…' : 'Preparing reconciliation…';
        const message = options.export ? 'Validating the displayed working paper and preparing complete supporting evidence.' : support ? 'Reading complete ledger postings and related Transaction Report vouchers.' : differences
            ? 'Comparing original postings for the selected report range.' : 'Reading independent ledger evidence and Cashflow movements.';
        target.hidden = false;
        target.setAttribute('aria-busy', 'true');
        target.innerHTML = '<div class="recon-loading-panel" role="status" aria-live="polite"><h3 class="recon-loading-title"></h3>'
            + '<div class="recon-loading-progress"></div><div class="recon-loading-skeleton" aria-hidden="true"></div></div>';
        const loading = {target: target, title: target.querySelector('.recon-loading-title'),
            progress: target.querySelector('.recon-loading-progress'), skeleton: target.querySelector('.recon-loading-skeleton'),
            token: window.LoadingUI.startGlobalLoading(element('cashflowReconciliation'))};
        reconciliationState.loading = loading;
        if (options.export) {
            loading.skeleton.hidden = true;
            target.querySelector('.recon-loading-panel').insertAdjacentHTML('beforeend',
                '<div class="recon-export-actions"><button type="button" class="cashflows-secondary" data-recon-cancel-export>Cancel export</button></div>');
        }
        window.LoadingUI.showSpinner(loading.title, title);
        window.LoadingUI.showProgressBar(loading.progress, {mode: 'indeterminate', message: message});
        loading.progress.setAttribute('role', 'progressbar');
        loading.progress.setAttribute('aria-label', title);
        if (!options.export) window.LoadingUI.showSkeleton(loading.skeleton, support || differences ? 4 : 6);
        syncReconciliationControls();
    }
    function setReconciliationFeature(active) {
        if (!/^https?:$/.test(window.location.protocol)) return;
        const params = new URLSearchParams(window.location.search);
        if (active) params.set('feature', 'reconciliation');
        else if (params.get('feature') === 'reconciliation') params.delete('feature');
        window.history.replaceState({}, '', window.location.pathname + '?' + params.toString() + window.location.hash);
    }
    function closeCashflowReconciliation() {
        cancelReconciliationRequest();
        const page = element('cashflowReconciliation');
        if (page.hidden) return;
        page.hidden = true;
        reconciliationState.result = null;
        document.querySelector('.reconciliation-shell').hidden = reconciliationState.shellWasHidden;
        setReconciliationFeature(false);
        if (typeof window.setNavModule === 'function') window.setNavModule('Cashflows summary');
        if (typeof window.setUpOverride === 'function') window.setUpOverride(showReportsHub);
        const saved = reconciliationState.scroll;
        if (saved) window.scrollTo(saved.x, saved.y);
        const opener = reconciliationState.opener;
        if (opener && opener.isConnected) opener.focus({preventScroll: true});
        clearReconciliationChildren();
        ['reconWorking', 'reconProcessing', 'reconExportProcessing', 'reconContext', 'reconStatus', 'reconView'].forEach(function (id) { element(id).replaceChildren(); });
        element('reconReportPeriod').textContent = '';
        reconciliationState.scope = null; reconciliationState.opener = null; reconciliationState.scroll = null;
        reconciliationState.lastRequest = {}; reconciliationState.supportKey = null;
        reconciliationState.needsReportReload = false;
    }
    function reconciliationRow(label, amount, className) {
        return '<tr class="' + (className || '') + '"><td>' + escapeHtml(label) + '</td><td class="recon-money">' + reconciliationMoney(amount) + '</td></tr>';
    }
    function reconciliationSchedule(part, side) {
        const title = side === 'receipts' ? 'Receipts' : 'Payments';
        const values = part.overall[side];
        let rows = reconciliationRow(title + ' as per ledgers', values.ledger, 'recon-total');
        let count = 0;
        part.adjustments.forEach(function (adjustment) {
            const value = adjustment.total[side];
            if (value === 0) return;
            count += 1;
            const label = (value == null ? '' : value < 0 ? 'Less: ' : 'Add: ') + adjustment.label
                + (adjustment.category ? ' — ' + adjustment.category : '');
            rows += '<tr><td><button type="button" class="recon-adjustment" data-recon-support="' + escapeHtml(adjustment.key)
                + '">' + escapeHtml(label) + '</button></td><td class="recon-money">' + reconciliationMoney(value) + '</td></tr>';
        });
        if (!count) rows += '<tr><td colspan="2" class="recon-muted">No supported presentation adjustments for this selection.</td></tr>';
        rows += reconciliationRow('Total presentation adjustments', values.adjustments, 'recon-total');
        rows += reconciliationRow('Adjusted ' + side + ' as per ledgers', values.adjusted_ledger, 'recon-total');
        rows += reconciliationRow(title + ' as per Cashflow report', values.cashflow, 'recon-total');
        rows += reconciliationRow('Unexplained difference (adjusted ledger − Cashflow)', values.difference,
            'recon-difference' + (values.difference !== 0 ? ' needs-review' : ''));
        return '<section class="recon-section" aria-label="' + title + ' reconciliation"><h3>' + title
            + '</h3><table><thead><tr><th scope="col">Particulars</th><th scope="col" class="recon-money">Amount</th></tr></thead><tbody>' + rows + '</tbody></table></section>';
    }
    function renderReconciliationStatus() {
        const state = reconciliationState, result = state.result;
        if (!result) return;
        const part = result.views[state.view];
        const reviews = result.review_controls || {};
        const posting = part.posting_comparison || {};
        const status = element('reconStatus');
        status.hidden = false;
        status.classList.remove('is-error');
        status.textContent = 'Receipt/payment amounts: ' + part.amount_status
            + '. Whole-range posting comparison: ' + posting.posting_status
            + '. Category reviews: ' + String(reviews.category_review_count ?? 'Unavailable')
            + '; customer-settlement reviews: ' + String(reviews.customer_settlement_review_count ?? 'Unavailable')
            + '. Extract completeness: ' + result.transaction_coverage_status + '. Treasury allocation: ' + part.allocation_status + '.';
    }
    function renderReconciliationWorking() {
        const state = reconciliationState, result = state.result;
        if (!result) return;
        const part = result.views[state.view];
        const proof = part.overall_balance_proof;
        let rows = reconciliationRow('Opening balance as per ledgers', proof.ledger_opening);
        const totals = part.overall;
        rows += reconciliationRow('Add: Receipts as per Cashflow report', totals.receipts.cashflow);
        rows += reconciliationRow('Less: Payments as per Cashflow report', totals.payments.cashflow);
        rows += reconciliationRow('Separate internal-transfer movement (signed)', proof.separate_transfer_movement);
        rows += reconciliationRow('Separate mixed-voucher movement (signed)', proof.separate_mixed_movement);
        rows += reconciliationRow('Calculated closing balance', proof.calculated_closing, 'recon-total');
        rows += reconciliationRow('Closing balance as per ledgers', proof.ledger_closing, 'recon-total');
        rows += reconciliationRow('Closing difference (ledger − calculated)', proof.difference, 'recon-difference' + (proof.difference !== 0 ? ' needs-review' : ''));
        const unresolved = (part.unresolved || []).map(function (row) {
            return '<li>' + escapeHtml([row.voucher_id, row.period_key, row.reason].filter(Boolean).join(' · ')) + '</li>';
        }).join('');
        const diagnostics = [...(result.diagnostics || []), ...((part.posting_comparison || {}).coverage_diagnostics || [])];
        element('reconWorking').innerHTML = '<p class="recon-muted">Full report period · ' + escapeHtml(cashflowTreasuryViewLabel(state.view))
            + '. To change the period, return to Cashflows Report and load the required dates. Amounts in source currency, to two decimals. Brackets indicate negative values.</p>'
            + reconciliationSchedule(part, 'receipts') + reconciliationSchedule(part, 'payments')
            + '<section class="recon-section" aria-label="Opening and closing proof"><h3>Opening and closing proof</h3><table><tbody>' + rows + '</tbody></table></section>'
            + (unresolved ? '<details><summary>Unresolved presentation evidence (' + part.unresolved.length + ')</summary><ul>' + unresolved + '</ul></details>' : '')
            + (diagnostics.length ? '<details><summary>Source and coverage diagnostics</summary><ul>' + diagnostics.map(function (item) {
                return '<li>' + escapeHtml(typeof item === 'object' ? JSON.stringify(item) : item) + '</li>';
            }).join('') + '</ul></details>' : '')
            + '<p class="recon-muted">' + escapeHtml(result.scope_note || '') + '</p>';
        renderReconciliationStatus();
    }
    function reconciliationSourceTable(title, rows) {
        const columns = [['date', 'Date'], ['account_code', 'Account code'], ['account_name', 'Account name'],
            ['voucher_id', 'Voucher'], ['debit', 'Debit (source)'], ['credit', 'Credit (source)'], ['description', 'Description'],
            ['id', 'Row ID'], ['upload_id', 'Upload'], ['template_id', 'Template'], ['version', 'Version'],
            ['uploaded_at', 'Imported at'], ['fingerprint', 'Fingerprint']];
        return '<h4>' + escapeHtml(title) + ' (' + rows.length + ')</h4><div class="recon-table-wrap"><table class="recon-source-table"><thead><tr>'
            + columns.map(function (col) { return '<th scope="col">' + col[1] + '</th>'; }).join('')
            + '<th scope="col">Scope</th></tr></thead><tbody>' + rows.map(function (row) {
                // Original values are text, including source precision and negative-side ERP postings.
                return '<tr>' + columns.map(function (col) { return '<td>' + escapeHtml(row[col[0]] == null ? '—' : String(row[col[0]])) + '</td>'; }).join('')
                    + '<td>' + (row.in_period === false ? 'Outside range · context only' : 'Within range') + '</td></tr>';
            }).join('') + '</tbody></table></div>' + (!rows.length ? '<p class="recon-muted">No source postings supplied.</p>' : '');
    }
    function reconciliationEvidence(rows, differences) {
        return rows.map(function (row) {
            const label = differences ? [row.account_code, row.voucher_id || 'Blank reference', row.status].join(' · ')
                : [row.voucher_id || 'Blank reference', row.period_key].join(' · ');
            const note = differences ? '<p>' + escapeHtml(row.note || '') + '</p><p>Ledger movement: ' + reconciliationMoney(row.ledger_movement)
                + ' · Transaction movement: ' + reconciliationMoney(row.transaction_movement) + ' · Difference: ' + reconciliationMoney(row.movement_difference)
                + '</p><p class="recon-muted">Changed fields: ' + escapeHtml((row.difference_fields || []).join(', ') || 'None') + '</p>'
                : '<p>Receipt adjustment: ' + reconciliationMoney(row.receipts) + ' · Payment adjustment: ' + reconciliationMoney(row.payments) + '</p>'
                    + '<ul>' + (row.operations || []).map(function (operation) {
                        return '<li>' + escapeHtml([operation.label, operation.category].filter(Boolean).join(' — '))
                            + ': receipts ' + reconciliationMoney(operation.receipts) + '; payments ' + reconciliationMoney(operation.payments) + '</li>';
                    }).join('') + '</ul>';
            return '<details><summary>' + escapeHtml(label) + '</summary>' + note
                + reconciliationSourceTable('Original Cash/Bank ledger postings', row.ledger_lines || [])
                + reconciliationSourceTable('Complete Transaction Report voucher', row.voucher_lines || []) + '</details>';
        }).join('');
    }
    function reconciliationPaging(data, kind) {
        return '<div class="recon-paging"><button type="button" class="cashflows-secondary" data-recon-page="' + kind + '" data-page="' + (data.page - 1) + '"'
            + (data.page <= 1 ? ' disabled' : '') + '>Previous</button><span>Page ' + data.page + ' of ' + data.page_count + ' · ' + data.total_rows
            + ' group(s)</span><button type="button" class="cashflows-secondary" data-recon-page="' + kind + '" data-page="' + (data.page + 1) + '"'
            + (data.page >= data.page_count ? ' disabled' : '') + '>Next</button></div>';
    }
    function reconciliationSetTab(tab) {
        reconciliationState.tab = tab;
        document.querySelectorAll('#cashflowReconciliation [role=tab]').forEach(function (button) {
            const selected = button.dataset.reconTab === tab;
            button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1;
        });
        element('reconWorking').hidden = tab !== 'working';
        element('reconDifferences').hidden = tab !== 'differences';
        element('reconSupport').hidden = true;
    }
    function clearReconciliationChildren() {
        element('reconSupport').hidden = true;
        element('reconSupport').innerHTML = '';
        element('reconDifferenceRows').innerHTML = '';
    }
    function scrollReconciliationEvidence(target) {
        // The wrapped mobile toolbar is taller; keep evidence below the visible controls.
        const toolbar = element('cashflowReconciliation').querySelector('.recon-toolbar');
        target.style.scrollMarginTop = (toolbar.getBoundingClientRect().height + 72.5 + 12) + 'px';
        target.scrollIntoView({block: 'start'});
    }
    function loadCashflowReconciliation(options, control) {
        const state = reconciliationState;
        if (!state.scope || element('cashflowReconciliation').hidden) return Promise.resolve();
        const context = reconciliationContextKey(state.scope);
        const key = JSON.stringify([context, options.support_key || '', options.period_key || '',
            !!options.posting_differences, options.comparison_view || '', options.differences_only || '',
            String(options.page || 1), String(options.limit || 25),
            options.support_key || options.posting_differences ? (state.result || {}).evidence_fingerprint : '']);
        const force = !!(control || {}).forceRefresh, requestKey = key + ':' + force;
        if (state.pending && state.pending.key === requestKey) return state.pending.promise;
        const promise = runCashflowReconciliation(options, {key: key, context: context, forceRefresh: force});
        state.pending = {key: requestKey, promise: promise};
        const finish = function () {if (state.pending && state.pending.promise === promise) state.pending = null;};
        promise.then(finish, finish);
        return promise;
    }
    async function runCashflowReconciliation(options, control) {
        const state = reconciliationState;
        if (!state.scope || element('cashflowReconciliation').hidden) return;
        const child = !!(options.support_key || options.posting_differences);
        if (child && !state.result) return;
        cancelReconciliationRequest();
        const generation = state.generation, scope = state.scope;
        if (control.forceRefresh) clearReconciliationCache(control.context);
        const cached = control.forceRefresh ? null : takeReconciliation(control.key);
        let cacheGeneration = reconciliationCacheGeneration;
        state.lastRequest = {...options};
        const fingerprint = child ? state.result.evidence_fingerprint : '';
        if (!child) {
            state.result = null; clearReconciliationChildren();
            element('reconViewLabel').hidden = true; element('reconView').replaceChildren();
            element('reconWorking').innerHTML = '';
            reconciliationSetTab('working');
        }
        const status = element('reconStatus');
        status.classList.remove('is-error');
        if (child) renderReconciliationStatus(); else { status.hidden = true; status.textContent = ''; }
        startReconciliationProcessing(options);
        if (options.support_key) scrollReconciliationEvidence(element('reconSupport'));
        const query = new URLSearchParams({client_id: scope.client_id, period_start: scope.period_start, period_end: scope.period_end,
            group_by: scope.group_by, treasury_view: scope.treasury_view, ...options});
        appendCashflowAccounts(query, cashflowAccountCodes(scope));
        if (child) query.set('expected_fingerprint', fingerprint);
        const controller = new AbortController(); state.controller = controller;
        const timeout = window.setTimeout(function () { controller.abort(); }, CASHFLOW_REQUEST_TIMEOUT_MS);
        const current = function () { return generation === state.generation && !element('cashflowReconciliation').hidden
            && scope.report === lastCashflowsResult && scope.client_id === clientId; };
        try {
            const read = async function (url) {
                const response = await fetch(url, {signal: controller.signal, cache: 'no-store'});
                const value = await response.json();
                if (!response.ok) { const error = new Error(value.detail || 'Reconciliation could not be loaded.'); error.status = response.status; throw error; }
                return value;
            };
            const checkScope = function (value) {
                return value.client_id === scope.client_id && value.period_start === scope.period_start && value.period_end === scope.period_end
                    && value.group_by === scope.group_by && value.treasury_view === scope.treasury_view
                    && (!value.template_id || value.template_id === scope.template_id)
                    && (!scope.configuration_key || value.configuration_key === scope.configuration_key)
                    && (scope.report_revision == null || String(value.report_revision) === String(scope.report_revision))
                    && cashflowAccountKey(cashflowAccountCodes(value)) === cashflowAccountKey(cashflowAccountCodes(scope));
            };
            let result;
            if (cached) {
                const validationQuery = new URLSearchParams({client_id: scope.client_id, period_start: scope.period_start,
                    period_end: scope.period_end, group_by: scope.group_by, treasury_view: scope.treasury_view});
                appendCashflowAccounts(validationQuery, cashflowAccountCodes(scope));
                let validation;
                try {
                    validation = await read('/api/reconciliation/cashflows/reconciliation/cache-validation?' + validationQuery);
                } catch (error) {
                    if (error.status === 401 || error.status === 403 || error.name === 'AbortError') throw error;
                    // An uncertain check cannot authorize reuse. Try the ordinary fresh read.
                    clearReconciliationCache(control.context);
                }
                if (!current()) return;
                if (cacheGeneration === reconciliationCacheGeneration && validation && checkScope(validation)
                    && validation.cache_schema === 1 && validation.cache_identity === cached.identity) {
                    result = JSON.parse(cached.json);
                } else {
                    clearReconciliationCache(control.context);
                    cacheGeneration = reconciliationCacheGeneration;
                }
            }
            if (!result) result = await read('/api/reconciliation/cashflows/reconciliation?' + query);
            if (!current()) return;
            if (!checkScope(result)) {
                state.needsReportReload = true;
                const error = new Error('The loaded report and current source/configuration differ. Reload to refresh the report and working paper together.'); error.status = 409; throw error;
            }
            if (child && (result.evidence_fingerprint !== fingerprint
                || (state.result.cache_identity && result.cache_identity !== state.result.cache_identity))) {
                const error = new Error('Reconciliation evidence changed. Reload the working paper.'); error.status = 409; throw error;
            }
            // An external refresh may clear memory while this snapshot is in flight.
            if (cacheGeneration === reconciliationCacheGeneration) {
                clearReconciliationCache(control.context, result.cache_identity);
                rememberReconciliation(control.key, control.context, result, options.support_key ? 'support' : child ? 'posting_differences' : '');
            }
            if (!child) {
                state.result = result; state.needsReportReload = false;
                state.view = result.views[scope.treasury_view] ? scope.treasury_view : 'combined';
                element('reconContext').textContent = scope.period_start + ' to ' + scope.period_end + ' · ' + scope.group_by + ' · '
                    + cashflowTreasuryViewLabel(scope.treasury_view) + ' · ' + result.receipt_presentation + ' receipts'
                    + (scope.account_selection ? ' · Accounts: ' + scope.account_selection.account_codes.join(', ') : '');
                element('reconViewLabel').hidden = scope.treasury_view !== 'split';
                element('reconView').innerHTML = Object.keys(result.views).map(function (view) { return '<option value="' + escapeHtml(view) + '">' + escapeHtml(cashflowTreasuryViewLabel(view)) + '</option>'; }).join('');
                element('reconView').value = state.view;
                renderReconciliationWorking();
            } else if (options.support_key) {
                const support = result.support;
                state.supportKey = options.support_key;
                element('reconSupport').innerHTML = '<div class="recon-evidence-head"><h3>Adjustment voucher support</h3><button type="button" class="cashflows-secondary" data-recon-hide-support>Close support</button></div>'
                    + '<p class="recon-muted">Original source amounts; import timestamps do not certify ERP extraction times. Outside-range lines provide context only.</p>'
                    + reconciliationPaging(support, 'support') + reconciliationEvidence(support.rows, false)
                    + (!support.rows.length ? '<p>No supporting groups for this period.</p>' : '') + reconciliationPaging(support, 'support');
                renderReconciliationStatus();
                scrollReconciliationEvidence(element('reconSupport'));
            } else {
                const data = result.posting_differences;
                element('reconDifferenceRows').innerHTML = '<p class="recon-muted">Posting comparison: ' + escapeHtml(data.posting_status) + ' · '
                    + escapeHtml(Object.entries(data.counts).map(function (entry) { return entry.join(': '); }).join(' · ')) + '</p>'
                    + reconciliationPaging(data, 'differences') + reconciliationEvidence(data.rows, true)
                    + (!data.rows.length ? '<p>No posting differences in the current comparison. Extract completeness remains uncertified.</p>' : '')
                    + reconciliationPaging(data, 'differences') + '<p class="recon-muted">' + escapeHtml(data.scope_note) + ' ' + escapeHtml(data.timestamp_note) + '</p>';
                renderReconciliationStatus();
            }
        } catch (error) {
            if (!current()) return;
            if ([401, 403, 409].includes(error.status)) {
                clearReconciliationCache(control.context);
                state.result = null; clearReconciliationChildren(); element('reconWorking').innerHTML = '';
                state.lastRequest = {};
            } else if (options.support_key) element('reconSupport').innerHTML = '';
            else if (child) element('reconDifferenceRows').innerHTML = '';
            status.classList.add('is-error');
            status.hidden = false;
            status.innerHTML = escapeHtml(error.name === 'AbortError' ? 'The reconciliation request timed out.' : error.message)
                + ' <button type="button" class="cashflows-secondary" data-recon-retry>' + (error.status === 409 ? 'Reload working paper' : 'Retry') + '</button>';
        } finally {
            window.clearTimeout(timeout);
            if (current()) {
                state.controller = null;
                finishReconciliationProcessing();
                syncReconciliationControls();
            }
        }
    }
    function syncCashflowReconciliationEntry() {
        const button = element('cashflowsReconciliationButton');
        button.disabled = cashflowBalancesPending || !element('cashflowsProcessing').hidden;
        button.setAttribute('aria-busy', String(cashflowBalancesPending));
        button.title = cashflowBalancesPending
            ? 'Checking the loaded report. Reconciliation will be available when this check finishes.'
            : 'Reconcile the loaded Cashflows report with Cash/Bank ledgers.';
    }
    async function openCashflowReconciliation() {
        if (cashflowLoadedClientChanged()) return;
        const report = lastCashflowsResult;
        if (cashflowBalancesPending || !report || !(report.source || {}).ready || !((report.preview || {}).periods || []).length) return;
        const state = reconciliationState, page = element('cashflowReconciliation');
        if (!page.hidden) return;
        state.opener = document.activeElement;
        state.scroll = {x: window.scrollX, y: window.scrollY};
        const shell = document.querySelector('.reconciliation-shell');
        state.shellWasHidden = shell.hidden;
        state.scope = {report: report, client_id: report.client_id, period_start: report.period_start, period_end: report.period_end,
            template_id: (report.configuration || {}).source_template_id,
            group_by: report.group_by, treasury_view: report.treasury_view || report.preview.treasury_view || 'combined',
            account_selection: report.account_selection,
            report_revision: cashflowReportRevisionFromResult(report), configuration_key: report.cashflow_configuration_key || ''};
        // Use the loaded report, never unapplied dates in the main report controls.
        element('reconReportPeriod').textContent = reconciliationPeriodLabel(state.scope);
        element('reconContext').textContent = report.period_start + ' to ' + report.period_end + ' · Loaded Cashflow report';
        if (report.account_selection) element('reconContext').textContent += ' · Accounts: ' + report.account_selection.account_codes.join(', ');
        element('reconIncludeMatched').checked = false;
        shell.hidden = true; page.hidden = false;
        setReconciliationFeature(true);
        if (typeof window.setNavModule === 'function') window.setNavModule('Cashflows reconciliation');
        if (typeof window.setUpOverride === 'function') window.setUpOverride(closeCashflowReconciliation);
        window.scrollTo(0, 0);
        element('reconClose').focus({preventScroll: true});
        await loadCashflowReconciliation({});
    }
    async function reloadCashflowReconciliation() {
        if (reconciliationState.needsReportReload) {
            const scope = reconciliationState.scope;
            // Refresh the linked report's scope even if its controls contain unapplied edits.
            cashflowSelection = {start: scope.period_start, end: scope.period_end, groupBy: scope.group_by};
            cashflowTreasuryView = scope.treasury_view;
            cashflowPendingAccounts = cashflowAccountCodes(scope) ? cashflowAccountCodes(scope).slice() : null;
            syncCashflowSelection();
            closeCashflowReconciliation();
            await generateCashflows(null, {forceRefresh: true});
            await openCashflowReconciliation();
        } else await loadCashflowReconciliation({}, {forceRefresh: true});
    }
    function loadReconciliationDifferences(page) {
        return loadCashflowReconciliation({posting_differences: 'true', comparison_view: reconciliationState.view,
            differences_only: String(!element('reconIncludeMatched').checked), page: page || 1, limit: 25});
    }
    async function downloadCashflowReconciliation() {
        const state = reconciliationState, scope = state.scope, result = state.result;
        if (!scope || !result || state.loading) return;
        cancelReconciliationRequest();
        const generation = state.generation;
        const current = function () { return generation === state.generation && state.scope === scope
            && scope.report === lastCashflowsResult && scope.client_id === clientId && !element('cashflowReconciliation').hidden; };
        const task = {id: null, scope: scope, cancelled: false}; state.exportTask = task;
        const controller = new AbortController(); state.controller = controller;
        startReconciliationProcessing({export: true});
        const status = element('reconStatus');
        // Generation runs on the server. Every individual HTTP request remains bounded.
        const request = async function (url, options, cancellable) {
            const local = new AbortController(), abort = function () { local.abort(); };
            if (cancellable !== false) {
                if (controller.signal.aborted) local.abort();
                controller.signal.addEventListener('abort', abort, {once: true});
            }
            const timer = window.setTimeout(abort, options && options.download ? 120000 : 30000);
            try {
                const response = await fetch(url, {...options, signal: local.signal, cache: 'no-store'});
                if (!response.ok) {
                    const value = await response.json().catch(function () { return {}; });
                    const error = new Error(typeof value.detail === 'string' ? value.detail : 'The reconciliation export could not be prepared.');
                    error.status = response.status; throw error;
                }
                // Keep the timeout active until the complete response body arrives.
                return options && options.download ? {blob: await response.blob(), headers: response.headers} : await response.json();
            } finally {
                window.clearTimeout(timer); controller.signal.removeEventListener('abort', abort);
            }
        };
        const pause = function () { return new Promise(function (resolve, reject) {
            const abort = function () { window.clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); };
            const timer = window.setTimeout(function () { controller.signal.removeEventListener('abort', abort); resolve(); }, 1000);
            controller.signal.addEventListener('abort', abort, {once: true});
            if (controller.signal.aborted) abort();
        }); };
        try {
            // Await acknowledgement even after closing, so a late-created job can be cancelled.
            let job = await request('/api/reconciliation/cashflows/reconciliation/exports', {method: 'POST',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({client_id: scope.client_id,
                    period_start: scope.period_start, period_end: scope.period_end, group_by: scope.group_by,
                    treasury_view: scope.treasury_view, expected_fingerprint: result.evidence_fingerprint,
                    expected_identity: result.cache_identity,
                    ...(scope.account_selection ? {selected_treasury_account_codes: scope.account_selection.account_codes} : {})})}, false);
            task.id = job.id;
            if (task.cancelled || !current()) { discardReconciliationExport(task); return; }
            const base = '/api/reconciliation/cashflows/reconciliation/exports/' + encodeURIComponent(job.id);
            const clientQuery = '?client_id=' + encodeURIComponent(scope.client_id);
            const deadline = Date.now() + 660000;
            while (true) {
                if (!current()) return;
                if (job.state === 'ready') break;
                if (['failed', 'cancelled'].includes(job.state)) {
                    const error = new Error(job.error || 'Export cancelled.'); error.status = job.error_status; throw error;
                }
                if (Date.now() > deadline) {
                    discardReconciliationExport(task);
                    throw new Error('Export preparation exceeded its limit. Please retry or select a shorter period.');
                }
                const loading = state.loading;
                window.LoadingUI.showSpinner(loading.title, job.stage || 'Preparing reconciliation Excel…');
                const counted = job.total != null && job.total > 0;
                window.LoadingUI.showProgressBar(loading.progress, {mode: counted ? 'determinate' : 'indeterminate',
                    message: counted ? Number(job.current).toLocaleString() + ' of ' + Number(job.total).toLocaleString() + ' rows' : 'Preparing the complete report evidence.'});
                if (counted) window.LoadingUI.updateProgressBar(loading.progress, job.current, job.total);
                await pause();
                job = await request(base + clientQuery);
            }
            const file = await request(base + '/download' + clientQuery, {download: true});
            if (!current()) return;
            const match = (file.headers.get('Content-Disposition') || '').match(/filename="?([^";]+)"?/i);
            const url = URL.createObjectURL(file.blob), link = document.createElement('a');
            link.href = url; link.download = match ? match[1] : 'Cashflow_reconciliation.xlsx';
            document.body.appendChild(link); link.click(); link.remove();
            window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
            status.classList.remove('is-error'); status.hidden = false;
            renderReconciliationStatus(); status.prepend(document.createTextNode('Reconciliation Excel downloaded. '));
        } catch (error) {
            if (!current()) return;
            if ([401, 403, 409].includes(error.status)) {
                clearReconciliationCache(reconciliationContextKey(scope));
                state.result = null; state.lastRequest = {}; clearReconciliationChildren();
                element('reconWorking').replaceChildren();
            }
            status.hidden = false; status.classList.add('is-error');
            status.innerHTML = escapeHtml(error.name === 'AbortError' ? 'The export connection timed out. Retry to check the current export.' : error.message)
                + ([401, 403, 409].includes(error.status) ? ' <button type="button" class="cashflows-secondary" data-recon-retry>Reload working paper</button>' : '');
        } finally {
            if (current()) { state.exportTask = null; state.controller = null; finishReconciliationProcessing(); syncReconciliationControls(); }
        }
    }
    function initialiseCashflowReconciliation() {
        const page = element('cashflowReconciliation');
        element('cashflowsReconciliationButton').addEventListener('click', openCashflowReconciliation);
        element('reconClose').addEventListener('click', closeCashflowReconciliation);
        element('reconReload').addEventListener('click', reloadCashflowReconciliation);
        element('reconExport').addEventListener('click', downloadCashflowReconciliation);
        element('reconView').addEventListener('change', function () {
            cancelReconciliationRequest(); reconciliationState.view = this.value; clearReconciliationChildren(); renderReconciliationWorking();
            if (reconciliationState.tab === 'differences') loadReconciliationDifferences(1);
        });
        element('reconIncludeMatched').addEventListener('change', function () { loadReconciliationDifferences(1); });
        page.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && !event.defaultPrevented && !event.target.matches('select, input')) {
                event.preventDefault(); closeCashflowReconciliation(); return;
            }
            if (!event.target.matches('[role=tab]') || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            const tabs = Array.from(page.querySelectorAll('[role=tab]'));
            const tab = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[1] : tabs[1 - tabs.indexOf(event.target)];
            if (!tab.disabled) { tab.click(); tab.focus(); }
        });
        page.addEventListener('click', function (event) {
            const button = event.target.closest('button'); if (!button) return;
            if (button.hasAttribute('data-recon-cancel-export')) {
                cancelReconciliationRequest(); renderReconciliationStatus(); syncReconciliationControls();
            } else if (button.hasAttribute('data-recon-retry')) {
                if (reconciliationState.needsReportReload) reloadCashflowReconciliation();
                else loadCashflowReconciliation(reconciliationState.lastRequest);
            } else if (button.dataset.reconTab) {
                if (reconciliationState.tab !== button.dataset.reconTab) {
                    cancelReconciliationRequest(); clearReconciliationChildren();
                }
                reconciliationSetTab(button.dataset.reconTab); renderReconciliationStatus();
                syncReconciliationControls();
                if (button.dataset.reconTab === 'differences') loadReconciliationDifferences(1);
            } else if (button.dataset.reconSupport) {
                loadCashflowReconciliation({support_key: button.dataset.reconSupport, page: 1, limit: 25});
            } else if (button.dataset.reconPage === 'support') {
                loadCashflowReconciliation({support_key: reconciliationState.supportKey, page: button.dataset.page, limit: 25});
            } else if (button.dataset.reconPage === 'differences') loadReconciliationDifferences(button.dataset.page);
            else if (button.hasAttribute('data-recon-hide-support')) { cancelReconciliationRequest(); element('reconSupport').hidden = true; renderReconciliationStatus(); }
        });
    }
    function renderResult(result) {
        if (!syncCashflowReportRevision(result)) return;
        closeCashflowReconciliation();
        const summaryResult = cashflowSummaryWithoutLedgers(result);
        rememberCashflowSummary(summaryResult);
        lastCashflowsResult = summaryResult;
        if (cashflowAccountOptionsKey && result.cashflow_configuration_key && cashflowAccountOptionsKey !== result.cashflow_configuration_key) {
            cashflowAccountOptions = null;
            cashflowAccountOptionsKey = '';
        }
        renderCashflowFreshness(result);
        const preview = summaryResult.preview || {};
        resetCashflowInlineLedgerState();
        closeCashflowDrilldown();
        syncCashflowTreasuryView();
        seedCashflowReportLedgers(result);
        syncCashflowAccountButton();
        renderCashflowSummary(preview);
        const summaryRows = cashflowSummaryRows(preview);
        renderCategoryOptions(summaryRows.map(function (row) { return row.category; }), true);
        renderCashflowDrilldownCategoryOptions(summaryRows);
        const source = result.source || {};
        const exportButton = element('cashflowsExportButton');
        if (exportButton) exportButton.hidden = !source.ready || !(preview.periods || []).length;
        const ledgerDrilldownButton = element('cashflowsLedgerDrilldownButton');
        if (ledgerDrilldownButton) ledgerDrilldownButton.hidden = !source.ready || !(preview.periods || []).length;
        element('cashflowsReconciliationButton').hidden = !source.ready || !(preview.periods || []).length;
        syncCashflowReconciliationEntry();

        resetLazyCashflowSections();
        const lazyActions = element('cashflowsLazyActions');
        lazyActions.hidden = !source.ready;
        element('cashflowsLoadTransfersButton').textContent = 'Load internal transfers (' + String(preview.internal_transfer_count || 0) + ')';
        element('cashflowsLoadDeductionsButton').textContent = 'Load customer deductions (' + String(preview.deduction_count || 0) + ')';
        element('cashflowsLoadReviewButton').textContent = 'Load review required (' + String(Number(preview.category_review_count || 0) + Number(preview.selection_review_count || 0)) + ')';

        const viewTransactionsButton = element('cashflowsViewTransactionsButton');
        const transactionsSection = element('cashflowsTransactionsSection');
        const hasSourceTemplate = !!((result.configuration || {}).source_template_id);
        if (viewTransactionsButton) viewTransactionsButton.hidden = !hasSourceTemplate;
        syncCashflowZeroRowsButton();
        if (!hasSourceTemplate) {
            transactionsSection.hidden = true;
            viewTransactionsButton.textContent = 'View classified transactions';
        }
        syncCashflowDrilldownShortcuts();
    }
    function showCashflowResultNotice(result) {
        const preview = result.preview || {};
        const blockingReasons = result.blocking_reasons || [];
        const reviewCount = Number(preview.category_review_count || 0);
        if (blockingReasons.length) showCashflowToast(blockingReasons.join(' '), true);
        else if (preview.selection_review_count) showCashflowToast(String(preview.selection_review_count) + ' voucher(s) need selected-account allocation review. See Review required.', false);
        else if (reviewCount) showCashflowToast(String(reviewCount) + ' counter-account line(s) need category review.', false);
    }
    async function downloadCashflowReport(mode) {
        if (cashflowLoadedClientChanged()) return;
        const isDrilldown = mode === 'drilldown';
        const button = element(isDrilldown ? 'cashflowsDrilldownExportButton' : 'cashflowsExportButton');
        const scope = cashflowLoadedScope();
        const start = scope.period_start;
        const end = scope.period_end;
        if (!button || !start || !end) return;
        button.disabled = true;
        button.textContent = 'Preparing export.';
        try {
            const query = new URLSearchParams({
                client_id: clientId,
                period_start: start,
                period_end: end,
                group_by: scope.group_by || 'month',
                treasury_view: scope.treasury_view,
            });
            appendCashflowAccounts(query, cashflowAccountCodes(scope));
            if (isDrilldown) {
                query.set('search', element('cashflowsDrilldownSearch').value.trim());
                query.set('category', element('cashflowsDrilldownCategory').value.trim());
                query.set('account_head', element('cashflowsDrilldownAccountHead').value.trim());
                query.set('treasury_type', element('cashflowsDrilldownTreasury').value);
                query.set('period_key', element('cashflowsDrilldownPeriod').value);
            } else {
                query.set('summary_filter', cashflowSummaryFilter);
                query.set('hide_zero_rows', hideCashflowZeroRows ? 'true' : 'false');
            }
            const response = await fetch('/api/reconciliation/cashflows/export?' + query.toString());
            if (!response.ok) {
                const payload = await response.json().catch(function () { return {}; });
                const detail = payload.detail;
                const message = typeof detail === 'string'
                    ? detail
                    : (detail && detail.message) || 'The Cashflows export could not be created.';
                throw new Error(message);
            }
            const blob = await response.blob();
            const disposition = response.headers.get('Content-Disposition') || '';
            const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
            const filename = filenameMatch ? filenameMatch[1] : 'Cashflows_Report.xlsx';
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            link.remove();
            window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
            showCashflowToast('Cashflows export downloaded.', false);
        } catch (error) {
            showCashflowToast(error.message, true);
        } finally {
            button.disabled = false;
            button.textContent = isDrilldown ? 'Export filtered XLSX' : 'XLSX';
        }
    }

    async function generateCashflows(event, options) {
        if (event) event.preventDefault();
        if (cashflowPendingAccounts != null && !await loadCashflowAccountOptions(false)) {
            showCashflowToast('Account choices are unavailable. Retry accounts before loading this selection.', true);
            return;
        }
        const requestedAccounts = cashflowEffectivePendingAccounts();
        const requestedAccountKey = cashflowAccountKey(requestedAccounts);
        if (requestedAccountKey === 'invalid') {
            showCashflowToast('Select at least one Cash/Bank account compatible with the Treasury view.', true);
            element('cashflowsAccountButton').click();
            return;
        }
        closeCashflowAccountPopover(false);
        closeCashflowReconciliation();
        element('cashflowsReconciliationButton').hidden = true;
        const forceRefresh = !!(options && options.forceRefresh);
        const generation = ++cashflowSummaryGeneration;
        if (cashflowSummaryController) cashflowSummaryController.abort();
        cashflowBalancesPending = true;
        cashflowBalanceExpanded = { opening: false, closing: false };
        element('cashflowsSummaryTable').innerHTML = '';
        if (forceRefresh) clearCashflowReportCaches();
        resetCashflowInlineLedgerState();
        resetCashflowDrilldownFilters();
        setCashflowSummaryFilterVisibility(false);
        const exportButton = element('cashflowsExportButton');
        if (exportButton) exportButton.hidden = true;
        cashflowSummaryFilter = '';
        element('cashflowsSummaryFilter').value = '';
        lastCashflowsResult = null;
        hideCashflowZeroRows = true;
        lastClassifiedTransactionsResult = null;
        cashflowChildPreviews = {};
        element('cashflowsHideZeroRowsButton').disabled = true;
        element('cashflowsHideZeroRowsButton').textContent = 'Show zero rows';
        showCashflowProcessing();
        let ownsProcessing = true;
        const start = element('cashflowsPeriodStart').value;
        const end = element('cashflowsPeriodEnd').value;
        const requestedClient = clientId;
        const requestedGrouping = cashflowSelection.groupBy;
        const requestedView = cashflowTreasuryView;
        const stillCurrent = function () {
            return generation === cashflowSummaryGeneration && requestedClient === clientId;
        };
        const cachedSummary = !forceRefresh
            ? (takeCashflowSummary(start, end, requestedGrouping, requestedView, requestedAccounts)
                || findCashflowParentSummary(start, end, requestedGrouping, requestedView, requestedAccounts))
            : null;
        const controller = new AbortController();
        cashflowSummaryController = controller;
        const timeout = window.setTimeout(function () { controller.abort(); }, CASHFLOW_REQUEST_TIMEOUT_MS);
        let renderedCached = false;
        try {
            if (cachedSummary) {
                setCashflowProcessingStep('load', 'Checking the cached Cashflows summary.');
                const isFresh = await ensureCashflowRevisionFresh(false, true);
                const freshSummary = isFresh
                    ? (takeCashflowSummary(start, end, requestedGrouping, requestedView, requestedAccounts)
                        || findCashflowParentSummary(start, end, requestedGrouping, requestedView, requestedAccounts))
                    : null;
                if (!stillCurrent()) return;
                if (freshSummary) {
                    setCashflowProcessingStep('render', 'Rendering the cached Cashflows summary.');
                    renderResult(freshSummary);
                    renderedCached = true;
                    showCashflowResultNotice(freshSummary);
                    // Cached movement rows remain usable while current ledger
                    // evidence is checked. Never display a cached balance.
                    hideCashflowProcessing();
                    ownsProcessing = false;
                }
            }
            if (ownsProcessing) setCashflowProcessingStep('load', 'Loading Cashflows data for the selected period.');
            const query = new URLSearchParams({ client_id: requestedClient, period_start: start, period_end: end, group_by: requestedGrouping, treasury_view: requestedView, details: 'summary', warm_ledger_cache: 'true', include_balances: 'true' });
            appendCashflowAccounts(query, requestedAccounts);
            if (forceRefresh) query.set('refresh', 'true');
            const result = await api('/api/reconciliation/cashflows/preview?' + query.toString(), { signal: controller.signal });
            if (!stillCurrent()) return;
            if (Number(result.client_id) !== Number(requestedClient) || result.period_start !== start || result.period_end !== end
                    || result.group_by !== requestedGrouping || result.treasury_view !== requestedView
                    || cashflowAccountKey(cashflowAccountCodes(result)) !== requestedAccountKey) {
                throw new Error('Report data no longer matches the selected client or period. Load the summary again.');
            }
            if (ownsProcessing) setCashflowProcessingStep('render', 'Rendering the grouped Cashflows summary.');
            cashflowBalancesPending = false;
            const sameMovements = renderedCached && lastCashflowsResult
                && cashflowReportRevisionFromResult(result) > 0
                && cashflowReportRevisionFromResult(result) === cashflowReportRevision
                && cashflowReportSourceKeyFromResult(result) === cashflowReportSourceKey
                && result.cashflow_configuration_key === cashflowConfigurationKey;
            let balancesOnly = false;
            if (sameMovements && syncCashflowReportRevision(result)) {
                closeCashflowReconciliation();
                lastCashflowsResult = cashflowSummaryWithoutLedgers(result);
                rememberCashflowSummary(lastCashflowsResult);
                seedCashflowReportLedgers(result);
                renderCashflowFreshness(result);
                balancesOnly = refreshCashflowBalanceRows(lastCashflowsResult.preview || {});
                if (!balancesOnly) renderResult(result);
            } else {
                if (renderedCached) hideCashflowProcessing();
                renderResult(result);
            }
            if (!balancesOnly && !element('cashflowsTransactionsSection').hidden) await loadClassifiedTransactions(true);
            showCashflowResultNotice(result);
        } catch (error) {
            if (!stillCurrent()) return;
            cashflowBalancesPending = false;
            if (lastCashflowsResult) {
                delete lastCashflowsResult.preview.balance_reconciliation;
                if (!refreshCashflowBalanceRows(lastCashflowsResult.preview || {})) renderCashflowSummary(lastCashflowsResult.preview || {});
            }
            showCashflowToast(error.name === 'AbortError'
                ? 'Cashflows is taking longer than expected. Please try again or review the configured Transaction Report template.'
                : error.message, true);
        } finally {
            window.clearTimeout(timeout);
            if (cashflowSummaryController === controller) cashflowSummaryController = null;
            if (generation === cashflowSummaryGeneration) {
                syncCashflowReconciliationEntry();
                if (ownsProcessing) hideCashflowProcessing();
            }
        }
    }
    function classifiedTransactionLayout(result) {
        const rawColumns = (result.template_columns || []).map(function (column) {
            return typeof column === 'string' ? column : column.column_name;
        }).filter(Boolean);
        const normalised = function (value) { return String(value).trim().toLowerCase(); };
        const voucherDetailIndex = rawColumns.findIndex(function (column) { return normalised(column) === 'voucher detail'; });
        const accountNameIndex = rawColumns.findIndex(function (column) { return normalised(column) === 'account name'; });
        const assignedCategoryIndex = voucherDetailIndex >= 0
            ? voucherDetailIndex
            : (accountNameIndex >= 0 ? accountNameIndex + 1 : rawColumns.length);
        const columns = rawColumns.slice();
        columns.splice(assignedCategoryIndex, 0, 'Assigned Category');
        columns.push('Type', 'Account');
        if (result.account_selection) columns.push('Account scope');
        return { rawColumns: rawColumns, columns: columns, assignedCategoryIndex: assignedCategoryIndex };
    }

    function classifiedRowHasNonZeroAmount(row, rawColumns) {
        const raw = row.row_data || {};
        const amountColumns = rawColumns.filter(function (column) {
            return /amount|debit|credit|balance|total|value|net|tax/i.test(String(column)) || /^(dr|cr)$/i.test(String(column).trim());
        });
        if (!amountColumns.length) return true;
        return amountColumns.some(function (column) {
            const value = Number(raw[column]);
            return Number.isFinite(value) && Math.abs(value) > 0;
        });
    }

    function renderClassifiedTransactions(result) {
        if (result) lastClassifiedTransactionsResult = result;
        result = result || lastClassifiedTransactionsResult || {};
        const table = element('cashflowsTransactionsTable');
        const rows = result.rows || [];
        const layout = classifiedTransactionLayout(result);
        renderCategoryOptions(rows.map(function (row) { return row.classification && row.classification.assigned_category; }));
        const rawColumns = layout.rawColumns;
        const columns = layout.columns;
        const visibleRows = hideCashflowZeroRows
            ? rows.map(function (row, index) { return { row: row, index: index }; }).filter(function (item) { return classifiedRowHasNonZeroAmount(item.row, rawColumns); })
            : rows.map(function (row, index) { return { row: row, index: index }; });
        const zeroRowsButton = element('cashflowsHideZeroRowsButton');
        if (zeroRowsButton) zeroRowsButton.textContent = hideCashflowZeroRows ? 'Show zero rows' : 'Hide zero rows';
        const totalPages = Math.max(1, Math.ceil(classifiedTransactionsTotal / classifiedTransactionsLimit));
        element('cashflowsTransactionsCount').textContent = String(classifiedTransactionsTotal) + ' line(s)';
        element('cashflowsTransactionsPageInfo').textContent = 'Page ' + String(classifiedTransactionsPage) + ' of ' + String(totalPages);
        element('cashflowsTransactionsPrevious').disabled = classifiedTransactionsPage <= 1;
        element('cashflowsTransactionsNext').disabled = classifiedTransactionsPage >= totalPages;
        if (!visibleRows.length) {
            const emptyMessage = rows.length ? 'No non-zero transaction rows on this page.' : 'No classified transactions found for the selected criteria.';
            table.innerHTML = '<tbody><tr><td class="cashflows-empty" colspan="' + Math.max(columns.length + 1, 1) + '">' + emptyMessage + '</td></tr></tbody>';
            return;
        }
        const headerCells = ['S.No'].concat(columns).map(function (label) { return '<th>' + escapeHtml(label) + '</th>'; }).join('');
        const bodyRows = visibleRows.map(function (item) {
            const row = item.row;
            const index = item.index;
            const raw = row.row_data || {};
            const classification = row.classification || {};
            const account = [classification.account_code, classification.account_name].filter(Boolean).join(' — ') || '—';
            const assignedCategory = classification.assigned_category || (classification.classification_status === 'unmapped' ? 'Needs review' : '—');
            const rawValues = rawColumns.map(function (column) { return raw[column] == null ? '' : raw[column]; });
            const values = rawValues.slice(0, layout.assignedCategoryIndex).concat([
                assignedCategory
            ], rawValues.slice(layout.assignedCategoryIndex), [
                classification.type || 'Needs review', account
            ]);
            if (result.account_selection) values.push(classification.account_scope || 'Voucher context');
            const cells = values.map(function (value, valueIndex) {
                const label = columns[valueIndex];
                const className = ['Type', 'Account', 'Assigned Category', 'Account scope'].indexOf(label) >= 0 ? ' class="cashflows-classification-cell"' : '';
                return '<td' + className + '>' + escapeHtml(value) + '</td>';
            }).join('');
            const classificationType = String(classification.type || '').trim().toLowerCase();
            const rowClass = classificationType === 'cash'
                ? ' class="cashflows-classified-cash"'
                : (classificationType === 'bank' ? ' class="cashflows-classified-bank"' : '');
            return '<tr' + rowClass + '><td>' + String((classifiedTransactionsPage - 1) * classifiedTransactionsLimit + index + 1) + '</td>' + cells + '</tr>';
        }).join('');
        table.innerHTML = '<thead><tr>' + headerCells + '</tr></thead><tbody>' + bodyRows + '</tbody>';
    }
    async function loadClassifiedTransactions(resetPage) {
        if (cashflowLoadedClientChanged()) return;
        const scope = cashflowLoadedScope();
        const loadedKey = cashflowLedgerContextKey(cashflowLedgerContext());
        const configuration = (lastCashflowsResult || {}).configuration || {};
        const templateId = configuration.source_template_id;
        const start = scope.period_start;
        const end = scope.period_end;
        if (!templateId || !clientId || !start || !end) return;
        if (resetPage) classifiedTransactionsPage = 1;
        if (classifiedTransactionsRequest) classifiedTransactionsRequest.abort();
        classifiedTransactionsRequest = new AbortController();
        const table = element('cashflowsTransactionsTable');
        table.innerHTML = '<tbody><tr><td class="cashflows-empty" colspan="20">Loading classified transactions…</td></tr></tbody>';
        const params = new URLSearchParams({
            client_id: String(clientId),
            template_id: String(templateId),
            date_start: start,
            date_end: end,
            page: String(classifiedTransactionsPage),
            limit: String(classifiedTransactionsLimit)
        });
        appendCashflowAccounts(params, cashflowAccountCodes(scope));
        if (scope.account_selection) params.set('treasury_view', ['cash','bank'].includes(scope.treasury_view) ? scope.treasury_view : 'combined');
        const search = element('cashflowsTransactionsSearch').value.trim();
        const type = element('cashflowsTransactionsType').value;
        const account = element('cashflowsTransactionsAccount').value.trim();
        const category = element('cashflowsTransactionsCategory').value.trim();
        const categoryFilter = category.toLowerCase() === 'unassigned' ? '__unassigned__' : category;
        if (search) params.set('search', search);
        if (type) params.set('classification_type', type);
        if (account) params.set('account_code', account);
        if (categoryFilter) params.set('category', categoryFilter);
        try {
            const result = await api('/api/data/cashflows-classification-preview?' + params.toString(), {
                signal: classifiedTransactionsRequest.signal
            });
            if (loadedKey !== cashflowLedgerContextKey(cashflowLedgerContext())
                || cashflowAccountKey(cashflowAccountCodes(result)) !== cashflowAccountKey(cashflowAccountCodes(scope))) return;
            classifiedTransactionsTotal = Number(result.total_rows || 0);
            renderClassifiedTransactions(result);
        } catch (error) {
            if (error.name === 'AbortError') return;
            table.innerHTML = '<tbody><tr><td class="cashflows-empty">Unable to load classified transactions.</td></tr></tbody>';
            showCashflowToast(error.message, true);
        }
    }
    function refreshCashflowZeroVisibility() {
        if (lastCashflowsResult) renderCashflowSummary(lastCashflowsResult.preview || {});
        Object.keys(cashflowChildPreviews).forEach(function (section) { renderCashflowChild(section, cashflowChildPreviews[section]); });
        if (lastClassifiedTransactionsResult && !element('cashflowsTransactionsSection').hidden) renderClassifiedTransactions();
        syncCashflowZeroRowsButton();
    }
    function toggleCashflowZeroRows() {
        if (!lastCashflowsResult) return;
        hideCashflowZeroRows = !hideCashflowZeroRows;
        refreshCashflowZeroVisibility();
    }

    async function toggleClassifiedTransactions() {
        const section = element('cashflowsTransactionsSection');
        const button = element('cashflowsViewTransactionsButton');
        if (section.hidden) {
            setActiveCashflowWindow('transactions');
            section.hidden = false;
            button.textContent = 'Hide classified transactions';
            syncCashflowDrilldownShortcuts();
            await loadClassifiedTransactions(true);
        } else {
            setActiveCashflowWindow(null);
            button.textContent = 'View classified transactions';
        }
        syncCashflowDrilldownShortcuts();
    }
    function loadCashflowChildren() {
        if (cashflowLoadedClientChanged()) return Promise.resolve(null);
        if (cashflowChildRequest) return cashflowChildRequest;
        const scope = cashflowLoadedScope();
        const loadedKey = cashflowLedgerContextKey(cashflowLedgerContext());
        const configuration = (lastCashflowsResult || {}).configuration || {};
        const start = scope.period_start;
        const end = scope.period_end;
        if (!configuration.source_template_id || !clientId || !start || !end) return Promise.resolve(null);
        const buttonIds = { transfers: 'cashflowsLoadTransfersButton', customer_payments: 'cashflowsLoadCustomerPaymentsButton', vendor_payments: 'cashflowsLoadVendorPaymentsButton', deductions: 'cashflowsLoadDeductionsButton', review: 'cashflowsLoadReviewButton' };
        const labels = { transfers: 'Load internal transfers', customer_payments: 'Load customer payments', vendor_payments: 'Load vendor payments', deductions: 'Load customer deductions', review: 'Load review required' };
        const generation = cashflowChildGeneration;
        Object.keys(buttonIds).forEach(function (name) {
            const button = element(buttonIds[name]);
            if (button) {
                button.disabled = true;
                button.textContent = 'Loading…';
            }
        });
        syncCashflowDrilldownShortcuts();
        const params = new URLSearchParams({
            client_id: String(clientId),
            period_start: start,
            period_end: end,
            group_by: scope.group_by,
            treasury_view: scope.treasury_view,
            details: 'transfers,customer_payments,vendor_payments,deductions,review'
        });
        appendCashflowAccounts(params, cashflowAccountCodes(scope));
        cashflowChildRequest = api('/api/reconciliation/cashflows/preview?' + params.toString())
            .then(function (result) {
                if (generation !== cashflowChildGeneration || loadedKey !== cashflowLedgerContextKey(cashflowLedgerContext())) return null;
                if (cashflowAccountKey(cashflowAccountCodes(result)) !== cashflowAccountKey(cashflowAccountCodes(scope))
                    || (scope.account_selection && !cashflowLedgerResponseMatches(result, cashflowLedgerContext()))) {
                    throw new Error('Account scope changed. Reload Cashflows.');
                }
                const preview = result.preview || {};
                renderCashflowCustomerWiseOptions(preview);
                Object.keys(buttonIds).forEach(function (name) {
                    cashflowChildPreviews[name] = preview;
                    const button = element(buttonIds[name]);
                    if (button) {
                        button.disabled = false;
                        button.textContent = labels[name] + ' (loaded)';
                    }
                });
                syncCashflowDrilldownShortcuts();
                return preview;
            })
            .catch(function (error) {
                if (generation === cashflowChildGeneration) {
                    Object.keys(buttonIds).forEach(function (name) {
                        const button = element(buttonIds[name]);
                        if (button) {
                            button.disabled = false;
                            button.textContent = labels[name];
                        }
                    });
                    showCashflowToast(error.message, true);
                    syncCashflowDrilldownShortcuts();
                }
                return null;
            })
            .finally(function () {
                if (generation === cashflowChildGeneration) cashflowChildRequest = null;
            });
        return cashflowChildRequest;
    }
    async function loadCashflowChild(section) {
        setActiveCashflowWindow(section);
        if (Object.prototype.hasOwnProperty.call(cashflowChildPreviews, section)) {
            renderCashflowChild(section, cashflowChildPreviews[section]);
            return;
        }
        const preview = await loadCashflowChildren();
        if (preview && Object.prototype.hasOwnProperty.call(cashflowChildPreviews, section)) {
            renderCashflowChild(section, cashflowChildPreviews[section]);
        }
    }
    function setReportView(isReport) {
        element('reportsView').hidden = isReport;
        element('cashflowsView').hidden = !isReport;
        const shell = document.querySelector('.reconciliation-shell');
        if (shell) shell.classList.toggle('cashflows-view-active', isReport);
        if (typeof window.setNavModule === 'function') window.setNavModule(isReport ? 'Cashflows summary' : 'Reports');
        if (typeof window.setUpOverride === 'function') window.setUpOverride(isReport ? showReportsHub : null);
    }
    function setSelection(card) {
        if (card.dataset.reportKey !== 'cashflows') return;
        document.querySelectorAll('[data-report-key]').forEach(function (item) { item.classList.toggle('is-selected', item === card); });
        const params = new URLSearchParams(window.location.search);
        params.set('view', 'reports');
        params.set('report', 'cashflows');
        window.history.replaceState({}, '', window.location.pathname + '?' + params.toString());
        setReportView(true);
    }
    function showReportsHub() {
        closeCashflowReconciliation();
        const params = new URLSearchParams(window.location.search);
        params.delete('report');
        params.delete('feature');
        params.set('view', 'reports');
        window.history.replaceState({}, '', window.location.pathname + '?' + params.toString());
        setReportView(false);
    }
    const CASHFLOW_DEFAULT_START = '2026-01-01';
    const CASHFLOW_DEFAULT_END = '2026-06-30';
    function defaultPeriods() {
        cashflowGroupingPreference = 'month';
        cashflowSelection = { start: CASHFLOW_DEFAULT_START, end: CASHFLOW_DEFAULT_END, groupBy: 'month' };
        syncCashflowSelection();
    }
    async function loadCashflowDefaultPeriods() {
        defaultPeriods();
    }
    async function initialise() {
        initialiseCashflowAccounts();
        initialiseCashflowReconciliation();
        setCashflowSummaryFilterVisibility(false);
        element('cashflowsExportButton').addEventListener('click', function () { downloadCashflowReport('summary'); });
        element('cashflowsDrilldownExportButton').addEventListener('click', function () { downloadCashflowReport('drilldown'); });
        element('cashflowsLedgerDrilldownButton').addEventListener('click', openCashflowDrilldown);
        element('cashflowsLedgerDrilldownBack').addEventListener('click', closeCashflowDrilldown);
        element('cashflowsDrilldownApply').addEventListener('click', function () { loadCashflowDrilldown(true); });
        element('cashflowsDrilldownClear').addEventListener('click', function () {
            element('cashflowsDrilldownSearch').value = '';
            element('cashflowsDrilldownCategory').value = '';
            element('cashflowsDrilldownAccountHead').value = '';
            renderCashflowDrilldownAccountOptions((lastCashflowDrilldownResult || {}).account_head_options);
            element('cashflowsDrilldownTreasury').value = cashflowDefaultTreasuryType();
            element('cashflowsDrilldownPeriod').value = '';
            loadCashflowDrilldown(true);
        });
        element('cashflowsDrilldownCategory').addEventListener('input', function () { renderCashflowDrilldownAccountOptions((lastCashflowDrilldownResult || {}).account_head_options); });
        [element('cashflowsDrilldownSearch'), element('cashflowsDrilldownCategory')].forEach(function (input) {
            input.addEventListener('keydown', function (event) {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    loadCashflowDrilldown(true);
                }
            });
        });
        element('cashflowsDrilldownPrevious').addEventListener('click', function () {
            if (cashflowDrilldownPage > 1) {
                cashflowDrilldownPage -= 1;
                loadCashflowDrilldown(false);
            }
        });
        element('cashflowsDrilldownNext').addEventListener('click', function () {
            const pageCount = Number((lastCashflowDrilldownResult || {}).page_count) || 1;
            if (cashflowDrilldownPage < pageCount) {
                cashflowDrilldownPage += 1;
                loadCashflowDrilldown(false);
            }
        });
        if (window.clientContextReady) await window.clientContextReady;
        clientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
        await loadCashflowDefaultPeriods();
        element('cashflowsPeriodButton').addEventListener('click', function () {
            const popover = element('cashflowsPeriodPopover');
            if (popover.hidden) openCashflowPopover(); else closeCashflowPopover();
        });
        element('cashflowsPeriodPopover').addEventListener('click', function (event) {
            const quick = event.target.closest('[data-quick-period]');
            if (quick) { quickCashflowSelection(quick.dataset.quickPeriod); return; }
            const periodType = event.target.closest('[data-period-type]');
            if (periodType) {
                activeCashflowPicker = periodType.dataset.periodType;
                cashflowSelectedPeriods = [];
                cashflowPickerView = stagedCashflowSelection.start;
                if (activeCashflowPicker === 'custom') {
                    stagedCashflowSelection.groupBy = autoCashflowGrouping(stagedCashflowSelection.start, stagedCashflowSelection.end);
                } else {
                    stagedCashflowSelection = { start: '', end: '', groupBy: activeCashflowPicker };
                }
                renderCashflowPeriodPicker(activeCashflowPicker);
                return;
            }
            if (event.target.closest('#cashflowsPeriodCancel')) { closeCashflowPopover(); return; }
            if (event.target.closest('#cashflowsPeriodApply')) { applyCashflowSelection(true); return; }
        });
        element('cashflowsPeriodPicker').addEventListener('click', handleCashflowPickerChange);
        element('cashflowsPeriodPicker').addEventListener('change', handleCashflowPickerChange);
        element('cashflowsGroupingSelect').addEventListener('change', function (event) {
            cashflowGroupingPreference = event.target.value || 'suggested';
            const start = stagedCashflowSelection && stagedCashflowSelection.start ? stagedCashflowSelection.start : cashflowSelection.start;
            const end = stagedCashflowSelection && stagedCashflowSelection.end ? stagedCashflowSelection.end : cashflowSelection.end;
            const groupBy = preferredCashflowGrouping(autoCashflowGrouping(start, end));
            if (stagedCashflowSelection) stagedCashflowSelection.groupBy = groupBy;
            else cashflowSelection.groupBy = groupBy;
            syncCashflowSelection();
        });
        element('cashflowsTreasuryViewSelect').addEventListener('change', function (event) {
            cashflowTreasuryView = event.target.value || 'combined';
            renderCashflowAccountOptions();
        });
        document.addEventListener('click', function (event) {
            const popover = element('cashflowsPeriodPopover');
            const periodControl = popover.closest('.cashflows-period-control');
            const refreshMenu = document.querySelector('.cashflows-refresh-menu');
            const eventPath = typeof event.composedPath === 'function' ? event.composedPath() : [];
            const insidePeriodControl = eventPath.length
                ? eventPath.indexOf(periodControl) >= 0
                : !!event.target.closest('.cashflows-period-control');
            if (!insidePeriodControl && !popover.hidden) closeCashflowPopover();
            const insideRefreshMenu = !!refreshMenu && refreshMenu.contains(event.target);
            if (!insideRefreshMenu) closeCashflowRefreshMenu();
        });
        document.addEventListener('keydown', function (event) {
            if (event.key !== 'Escape') return;
            const menu = element('cashflowsRefreshMenu');
            const button = element('cashflowsRefreshMenuButton');
            if (menu && !menu.hidden) {
                closeCashflowRefreshMenu();
                if (button) button.focus();
            }
        });

        element('cashflowsReportForm').addEventListener('submit', generateCashflows);
        element('cashflowsSummaryFilter').addEventListener('input', function (event) {
            cashflowSummaryFilter = event.target.value || '';
            element('cashflowsSummaryClear').hidden = !cashflowSummaryFilter;
            if (lastCashflowsResult) renderCashflowSummary(lastCashflowsResult.preview || {});
        });
        element('cashflowsSummaryClear').addEventListener('click', function () {
            cashflowSummaryFilter = '';
            element('cashflowsSummaryFilter').value = '';
            element('cashflowsSummaryClear').hidden = true;
            if (lastCashflowsResult) renderCashflowSummary(lastCashflowsResult.preview || {});
        });
        element('cashflowsSummaryTable').addEventListener('click', handleCashflowSummaryClick);
        element('cashflowsViewTransactionsButton').addEventListener('click', toggleClassifiedTransactions);
        element('cashflowsHideZeroRowsButton').addEventListener('click', toggleCashflowZeroRows);
        element('cashflowsRefreshMenuButton').addEventListener('click', function (event) {
            event.stopPropagation();
            const button = element('cashflowsRefreshMenuButton');
            setCashflowRefreshMenuOpen(button.getAttribute('aria-expanded') !== 'true');
        });
        element('cashflowsRefreshButton').addEventListener('click', function () {
            closeCashflowRefreshMenu();
            generateCashflows(null, { forceRefresh: true });
        });
        element('cashflowsFreshnessAlertClose').addEventListener('click', dismissCashflowFreshnessAlert);
        element('cashflowsLoadTransfersButton').addEventListener('click', function () { loadCashflowChild('transfers'); });
        element('cashflowsLoadCustomerPaymentsButton').addEventListener('click', showCashflowVoucherPayments);
        element('cashflowsLoadVendorPaymentsButton').addEventListener('click', function () { loadCashflowChild('vendor_payments'); });
        element('cashflowsVendorView').addEventListener('change', function () {
            if (cashflowChildPreviews.vendor_payments) renderCashflowVendorPayments(cashflowChildPreviews.vendor_payments);
        });
        element('cashflowsLoadDeductionsButton').addEventListener('click', function () { loadCashflowChild('deductions'); });
        element('cashflowsLoadReviewButton').addEventListener('click', function () { loadCashflowChild('review'); });
        const cashflowDetailSearches = [
            { section: 'transfers', input: 'cashflowsTransfersSearch', clear: 'cashflowsTransfersSearchClear' },
            { section: 'vendor_payments', input: 'cashflowsVendorPaymentsSearch', clear: 'cashflowsVendorPaymentsClear' },
            { section: 'customer_payments', input: 'cashflowsCustomerPaymentsSearch', clear: 'cashflowsCustomerPaymentsSearchClear' },
            { section: 'deductions', input: 'cashflowsDeductionsSearch', clear: 'cashflowsDeductionsSearchClear' },
            { section: 'review', input: 'cashflowsReviewSearch', clear: 'cashflowsReviewSearchClear' },
        ];
        cashflowDetailSearches.forEach(function (config) {
            const input = element(config.input);
            const clearButton = element(config.clear);
            if (!input) return;
            const rerender = function () {
                const preview = cashflowChildPreviews[config.section];
                if (preview) renderCashflowChild(config.section, preview);
            };
            input.addEventListener('input', rerender);
            input.addEventListener('keydown', function (event) {
                if (event.key === 'Escape') {
                    input.value = '';
                    rerender();
                }
            });
            if (clearButton) clearButton.addEventListener('click', function () {
                input.value = '';
                rerender();
                input.focus();
            });
        });
        async function showCashflowVoucherPayments() {
            const preview = await loadCashflowChild('customer_payments');
            const customerPreview = cashflowChildPreviews.customer_payments || preview;
            const customerWisePanel = element('cashflowsCustomerWisePanel');
            const customerPeriodWisePanel = element('cashflowsCustomerPeriodWisePanel');
            const customerPaymentsVoucherPanel = element('cashflowsCustomerPaymentsVoucherPanel');
            const customerPaymentsHeading = element('cashflowsCustomerPaymentsHeading');
            const customerWiseSearch = element('cashflowsCustomerPaymentsCustomerSearch');
            if (customerWiseSearch) { customerWiseSearch.value = ''; customerWiseSearch.hidden = false; }
            if (customerWisePanel) customerWisePanel.hidden = true;
            if (customerPeriodWisePanel) customerPeriodWisePanel.hidden = true;
            if (customerPaymentsVoucherPanel) customerPaymentsVoucherPanel.hidden = false;
            if (customerPaymentsHeading) customerPaymentsHeading.textContent = 'Customer payments';
            if (customerPreview) renderCashflowChild('customer_payments', customerPreview);
        }
        const customerPeriodWiseButton = element('cashflowsCustomerPaymentsPeriodWiseButton');
        if (customerPeriodWiseButton) customerPeriodWiseButton.addEventListener('click', async function () {
            const preview = await loadCashflowChild('customer_payments');
            const customerPreview = cashflowChildPreviews.customer_payments || preview;
            if (customerPreview) renderCashflowCustomerPeriodWise(customerPreview);
        });
        const customerWiseSearch = element('cashflowsCustomerPaymentsCustomerSearch');
        const customerWiseButton = element('cashflowsCustomerPaymentsCustomerWiseButton');
        const customerWiseClear = element('cashflowsCustomerPaymentsSearchClear');
        const renderCustomerWisePreview = function () {
            const preview = cashflowChildPreviews.customer_payments;
            if (preview && !element('cashflowsCustomerWisePanel').hidden) renderCashflowCustomerWise(preview);
            if (preview && !element('cashflowsCustomerPeriodWisePanel').hidden) renderCashflowCustomerPeriodWise(preview);
        };
        if (customerWiseSearch) {
            customerWiseSearch.addEventListener('input', renderCustomerWisePreview);
            customerWiseSearch.addEventListener('keydown', function (event) {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    if (customerWiseButton) customerWiseButton.click();
                }
                if (event.key === 'Escape') {
                    customerWiseSearch.value = '';
                    renderCustomerWisePreview();
                }
            });
        }
        if (customerWiseClear) customerWiseClear.addEventListener('click', function () {
            if (customerWiseSearch) customerWiseSearch.value = '';
            renderCustomerWisePreview();
        });
        if (customerWiseButton) customerWiseButton.addEventListener('click', async function () {
            const preview = await loadCashflowChild('customer_payments');
            const customerPreview = cashflowChildPreviews.customer_payments || preview;
            if (customerPreview) renderCashflowCustomerWise(customerPreview);
        });
        const cashflowChildShortcutTargets = {
            transfers: 'cashflowsLoadTransfersButton',
            customer_payments: 'cashflowsLoadCustomerPaymentsButton',
            deductions: 'cashflowsLoadDeductionsButton',
            review: 'cashflowsLoadReviewButton',
        };
        document.querySelectorAll('[data-cashflow-child-shortcut]').forEach(function (shortcut) {
            shortcut.addEventListener('click', function () {
                const source = element(cashflowChildShortcutTargets[shortcut.dataset.cashflowChildShortcut]);
                if (source && !source.disabled) source.click();
            });
        });
        document.querySelectorAll('[data-cashflow-transactions-shortcut]').forEach(function (shortcut) {
            shortcut.addEventListener('click', function () {
                const source = element('cashflowsViewTransactionsButton');
                if (source && !source.disabled) source.click();
            });
        });
        element('cashflowsTransactionsApply').addEventListener('click', function () { loadClassifiedTransactions(true); });
        element('cashflowsTransactionsClear').addEventListener('click', function () {
            element('cashflowsTransactionsSearch').value = '';
            element('cashflowsTransactionsType').value = '';
            element('cashflowsTransactionsAccount').value = '';
            element('cashflowsTransactionsCategory').value = '';
            loadClassifiedTransactions(true);
        });
        element('cashflowsTransactionsPrevious').addEventListener('click', function () {
            if (classifiedTransactionsPage > 1) {
                classifiedTransactionsPage -= 1;
                loadClassifiedTransactions(false);
            }
        });
        element('cashflowsTransactionsNext').addEventListener('click', function () {
            const totalPages = Math.max(1, Math.ceil(classifiedTransactionsTotal / classifiedTransactionsLimit));
            if (classifiedTransactionsPage < totalPages) {
                classifiedTransactionsPage += 1;
                loadClassifiedTransactions(false);
            }
        });
        [element('cashflowsTransactionsSearch'), element('cashflowsTransactionsAccount'), element('cashflowsTransactionsCategory')].forEach(function (input) {
            input.addEventListener('keydown', function (event) {
                if (event.key === 'Enter') loadClassifiedTransactions(true);
            });
        });

        document.querySelectorAll('[data-report-key]').forEach(function (card) {
            card.addEventListener('click', function () { setSelection(card); });
            card.addEventListener('keydown', function (event) { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelection(card); } });
        });
        const requested = new URLSearchParams(window.location.search).get('report');
        const requestedCard = Array.from(document.querySelectorAll('[data-report-key]')).find(function (card) { return card.dataset.reportKey === requested; });
        if (requestedCard && clientId) {
            setSelection(requestedCard);
            if (requested === 'cashflows' && new URLSearchParams(window.location.search).get('feature') === 'reconciliation') {
                await generateCashflows();
                await openCashflowReconciliation();
            }
        }
    }
    document.addEventListener('DOMContentLoaded', initialise);
}());
