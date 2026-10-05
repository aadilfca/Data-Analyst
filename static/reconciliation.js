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
    let cashflowGroupingPreference = 'suggested';
    let stagedCashflowSelection = null;
    let activeCashflowPicker = null;
    let cashflowSelectedPeriods = [];
    let cashflowPickerView = null;
    let cashflowToastTimer = null;
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
    let cashflowRevisionCheckPromise = null;
    let cashflowRevisionCheckedAt = 0;
    const CASHFLOW_REVISION_CHECK_TTL_MS = 15000;
    const cashflowSummaryCacheLimit = 5;
    const cashflowSummaryCache = new Map();
    const cashflowDrilldownBaseCache = new Map();
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
        element('cashflowsGroupingSelect').value = cashflowGroupingPreference;
        syncCashflowTreasuryView();
    }
    const cashflowTreasuryViewLabels = { combined: 'Cash and Bank Total', bank: 'Bank only', cash: 'Cash only', split: 'Cash and Bank individually' };
    function cashflowTreasuryViewLabel(value) { return cashflowTreasuryViewLabels[String(value || 'combined')] || cashflowTreasuryViewLabels.combined; }
    function cashflowDefaultTreasuryType() {
        if (cashflowTreasuryView === 'bank') return 'Bank';
        if (cashflowTreasuryView === 'cash') return 'Cash';
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
                return '<td class="' + (column.amount ? 'cashflows-table-number' : 'cashflows-table-text') + '">' + escapeHtml(column.amount ? formatInternationalAmount(value) : (value == null ? '' : value)) + '</td>';
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
        ['cashflowsLoadTransfersButton', 'cashflowsLoadCustomerPaymentsButton', 'cashflowsLoadDeductionsButton', 'cashflowsLoadReviewButton'].forEach(function (id) {
            element(id).disabled = false;
        });
        cashflowChildPreviews = {};
        ['cashflowsTransfersSearch', 'cashflowsCustomerPaymentsSearch', 'cashflowsDeductionsSearch', 'cashflowsReviewSearch'].forEach(function (id) {
            const input = element(id);
            if (input) input.value = '';
        });
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
        panel.hidden = false;
    }
    function renderCashflowChild(section, preview) {
        if (section === 'transfers') {
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
                { key: 'date_display', label: 'Date' }, { key: 'voucher_id', label: 'Voucher' }, { key: 'transferor', label: 'Transferor' }, { key: 'transferee', label: 'Transferee' }, { key: 'amount', label: 'Amount', amount: true }, { key: 'variance', label: 'Variance', amount: true }, { key: 'status_display', label: 'Status' }, { key: 'voucher_detail', label: 'Description' },
            ], rows, 'No internal transfers identified.');
            element('cashflowsTransferCount').textContent = formatCashflowDetailCount('cashflowsTransfersTable', preview.internal_transfer_count || rows.length || 0, 'voucher(s)');
            if (activeCashflowWindow === section) element('cashflowsTransfersSection').hidden = false;
        } else if (section === 'customer_payments') {
            const rows = (preview.customer_payment_details || []).map(function (row) {
                return Object.assign({}, row, {
                    date_display: formatCashflowTransferDate(row.date || row.period_label || row.month),
                    status_display: row.status || 'Review required',
                });
            });
            renderTable(element('cashflowsCustomerPaymentsTable'), [
                { key: 'date_display', label: 'Date' },
                { key: 'voucher_id', label: 'Voucher' },
                { key: 'customer_account_name', label: 'Customer' },
                { key: 'bank_account_name', label: 'Receiving account' },
                { key: 'method', label: 'Method' },
                { key: 'customer_gross_amount', label: 'Gross receipt', amount: true },
                { key: 'bank_charges', label: 'Bank charges', amount: true },
                { key: 'withholding_tax', label: 'WHT', amount: true },
                { key: 'other_deductions', label: 'Other deductions', amount: true },
                { key: 'deductions_total', label: 'Total deductions', amount: true },
                { key: 'technical_bank_offset', label: 'Technical Bank credit', amount: true },
                { key: 'expected_net_settlement', label: 'Expected settlement', amount: true },
                { key: 'net_bank_settlement', label: 'Actual settlement', amount: true },
                { key: 'variance', label: 'Difference', amount: true },
                { key: 'status_display', label: 'Status' },
                { key: 'voucher_detail', label: 'Description' },
            ], rows, 'No customer payments identified.');
            element('cashflowsCustomerPaymentCount').textContent = formatCashflowDetailCount('cashflowsCustomerPaymentsTable', rows.length, 'payment(s)');
            if (activeCashflowWindow === section) element('cashflowsCustomerPaymentsSection').hidden = false;
        } else if (section === 'deductions') {
            const rows = preview.deduction_details || [];
            renderTable(element('cashflowsDeductionsTable'), [
                { key: 'month', label: 'Month' }, { key: 'category', label: 'Category' }, { key: 'account_name', label: 'Account' }, { key: 'amount', label: 'Amount', amount: true },
            ], rows, 'No customer deductions identified.');
            element('cashflowsDeductionCount').textContent = formatCashflowDetailCount('cashflowsDeductionsTable', preview.deduction_count || rows.length || 0, 'line(s)');
            if (activeCashflowWindow === section) element('cashflowsDeductionsSection').hidden = false;
        } else if (section === 'review') {
            const rows = preview.categories_needing_review || [];
            renderTable(element('cashflowsReviewTable'), [
                { key: 'voucher_id', label: 'Voucher' }, { key: 'account_code', label: 'Account code' }, { key: 'account_name', label: 'Account' }, { key: 'fallback_category', label: 'Review category' },
            ], rows, 'No review items.');
            element('cashflowsReviewCount').textContent = formatCashflowDetailCount('cashflowsReviewTable', preview.category_review_count || rows.length || 0, 'line(s)');
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
        const reportIds = ['cashflowsReportForm', 'cashflowsSummaryTable', 'cashflowsLazyActions', 'cashflowsTransfersSection', 'cashflowsCustomerPaymentsSection', 'cashflowsDeductionsSection', 'cashflowsReviewSection', 'cashflowsTransactionsSection'];
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
        const grouping = groupingLabels[String(cashflowSelection.groupBy || '')] || String(cashflowSelection.groupBy || '');
        context.textContent = 'Period: ' + formatRange(cashflowSelection.start, cashflowSelection.end)
            + ' | View: ' + cashflowTreasuryViewLabel(result && result.treasury_view || cashflowTreasuryView)
            + ' | Grouping: ' + grouping;
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
        return [clientId, start, end, cashflowSelection.groupBy || 'month', cashflowTreasuryView || 'combined', cashflowReportRevision].join('|');
    }
    function cashflowSummaryContextKey(start, end, groupBy, treasuryView, revision) {
        return [clientId, start, end, groupBy || 'month', treasuryView || 'combined', Number(revision || cashflowReportRevision || 0)].join('|');
    }
    function cashflowPeriodSelection(preview, start, end) {
        const childStart = fromIso(start);
        const childEnd = fromIso(end);
        const periods = cashflowPreviewPeriods(preview);
        if (!childStart || !childEnd || !periods.length || periods.some(function (period) {
            return !fromIso(period.start) || !fromIso(period.end);
        })) return null;
        const selected = periods.filter(function (period) {
            const periodStart = fromIso(period.start);
            const periodEnd = fromIso(period.end);
            return periodStart >= childStart && periodEnd <= childEnd;
        });
        if (!selected.length) return null;
        const firstStart = fromIso(selected[0].start);
        const lastEnd = fromIso(selected[selected.length - 1].end);
        if (firstStart.getTime() !== childStart.getTime() || lastEnd.getTime() !== childEnd.getTime()) return null;
        return selected;
    }
    function cashflowParentMetricTotals(preview, periods) {
        const source = preview && preview.period_metrics;
        const metrics = ['source_line_count', 'selected_voucher_count', 'internal_transfer_count', 'category_review_count', 'deduction_count'];
        if (!source || typeof source !== 'object') return null;
        const totals = {};
        metrics.forEach(function (metric) { totals[metric] = 0; });
        for (const period of periods) {
            const values = source[String(period.key)];
            if (!values || typeof values !== 'object') return null;
            for (const metric of metrics) {
                if (!Object.prototype.hasOwnProperty.call(values, metric)) return null;
                const value = Number(values[metric]);
                if (!Number.isFinite(value)) return null;
                totals[metric] += value;
            }
        }
        return totals;
    }
    function deriveCashflowSummaryFromParent(parent, start, end, groupBy, treasuryView) {
        if (!parent || groupBy !== 'month') return null;
        if (String(parent.group_by || '') !== 'month' || String(parent.treasury_view || '') !== String(treasuryView || 'combined')) return null;
        const parentPreview = parent.preview || {};
        const periods = cashflowPeriodSelection(parentPreview, start, end);
        const metrics = cashflowParentMetricTotals(parentPreview, periods || []);
        if (!periods || !metrics) return null;
        const periodKeys = periods.map(function (period) { return String(period.key); });
        const derivedPreview = Object.assign({}, parentPreview);
        derivedPreview.periods = periods.map(function (period) { return Object.assign({}, period); });
        derivedPreview.months = periodKeys.slice();
        derivedPreview.period_metrics = {};
        periods.forEach(function (period) { derivedPreview.period_metrics[String(period.key)] = Object.assign({}, parentPreview.period_metrics[String(period.key)]); });
        derivedPreview.source_line_count = metrics.source_line_count;
        derivedPreview.selected_voucher_count = metrics.selected_voucher_count;
        derivedPreview.internal_transfer_count = metrics.internal_transfer_count;
        derivedPreview.category_review_count = metrics.category_review_count;
        derivedPreview.deduction_count = metrics.deduction_count;
        derivedPreview.detail_sections_loaded = [];
        derivedPreview.transfer_details = [];
        derivedPreview.customer_payment_details = [];
        derivedPreview.deduction_details = [];
        derivedPreview.categories_needing_review = [];
        derivedPreview.child_details_prepared = false;
        derivedPreview.child_details_version = 0;
        const treasuryRows = Array.isArray(parentPreview.treasury_category_totals)
            ? parentPreview.treasury_category_totals.filter(function (row) { return periodKeys.indexOf(String(row.period_key || '')) >= 0; })
            : [];
        derivedPreview.treasury_category_totals = treasuryRows;
        const visibleRows = cashflowSummaryRows(parentPreview);
        if (String(treasuryView || 'combined') === 'split') {
            derivedPreview.treasury_view_totals = visibleRows.filter(function (row) { return periodKeys.indexOf(String(row.period_key || '')) >= 0; });
        } else {
            const rows = visibleRows.map(function (row) {
                const derivedRow = { category: row.category || '' };
                let total = 0;
                periodKeys.forEach(function (key) {
                    const value = Number(row[key]) || 0;
                    derivedRow[key] = value;
                    total += value;
                });
                derivedRow.Total = total;
                return derivedRow;
            });
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
    function findCashflowParentSummary(start, end, groupBy, treasuryView) {
        if (groupBy !== 'month') return null;
        const entries = Array.from(cashflowSummaryCache.values()).reverse();
        for (const entry of entries) {
            if (!entry || entry.revision !== cashflowReportRevision) continue;
            const parent = entry.result;
            const parentStart = fromIso(parent && parent.period_start);
            const parentEnd = fromIso(parent && parent.period_end);
            const childStart = fromIso(start);
            const childEnd = fromIso(end);
            if (!parentStart || !parentEnd || !childStart || !childEnd || parentStart > childStart || parentEnd < childEnd) continue;
            const derived = deriveCashflowSummaryFromParent(parent, start, end, groupBy, treasuryView);
            if (derived) return derived;
        }
        return null;
    }
    function rememberCashflowSummary(result) {
        if (!result || !cashflowReportRevision || !result.period_start || !result.period_end) return;
        const key = cashflowSummaryContextKey(result.period_start, result.period_end, result.group_by, result.treasury_view, cashflowReportRevision);
        cashflowSummaryCache.delete(key);
        cashflowSummaryCache.set(key, { result: result, revision: cashflowReportRevision });
        while (cashflowSummaryCache.size > cashflowSummaryCacheLimit) {
            cashflowSummaryCache.delete(cashflowSummaryCache.keys().next().value);
        }
    }
    function takeCashflowSummary(start, end, groupBy, treasuryView) {
        const key = cashflowSummaryContextKey(start, end, groupBy, treasuryView, cashflowReportRevision);
        const entry = cashflowSummaryCache.get(key);
        if (!entry) return null;
        if (entry.revision !== cashflowReportRevision) {
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
    function syncCashflowReportRevision(result) {
        const incoming = cashflowReportRevisionFromResult(result);
        if (!incoming) return true;
        if (cashflowReportRevision && incoming < cashflowReportRevision) return false;
        if (cashflowReportRevision && incoming !== cashflowReportRevision) {
            cashflowSummaryCache.clear();
            cashflowDrilldownBaseCache.clear();
            resetCashflowInlineLedgerState();
            cashflowChildPreviews = {};
        }
        cashflowReportRevision = incoming;
        cashflowRevisionCheckedAt = Date.now();
        return true;
    }
    function ensureCashflowRevisionFresh(force, failClosed) {
        if (!clientId) return Promise.resolve(true);
        const now = Date.now();
        if (!force && cashflowRevisionCheckedAt && now - cashflowRevisionCheckedAt < CASHFLOW_REVISION_CHECK_TTL_MS) return Promise.resolve(true);
        if (cashflowRevisionCheckPromise) return cashflowRevisionCheckPromise;
        cashflowRevisionCheckPromise = api("/api/reconciliation/cashflows/revision?client_id=" + encodeURIComponent(clientId))
            .then(function (result) {
                const incoming = Number(result && result.cashflow_report_revision || 0);
                if (incoming && cashflowReportRevision && incoming !== cashflowReportRevision) {
                    cashflowSummaryCache.clear();
                    cashflowDrilldownBaseCache.clear();
                    resetCashflowInlineLedgerState();
                    cashflowChildPreviews = {};
                    cashflowReportRevision = incoming;
                    return false;
                }
                if (incoming) cashflowReportRevision = incoming;
                cashflowRevisionCheckedAt = Date.now();
                return true;
            })
            .catch(function () { return !failClosed; })
            .finally(function () { cashflowRevisionCheckPromise = null; });
        return cashflowRevisionCheckPromise;
    }
    function renderCashflowFreshness(result) {
        const status = element("cashflowsFreshnessStatus");
        if (!status) return;
        const freshness = result && result.freshness || {};
        const revision = Number(freshness.report_revision || cashflowReportRevision || 0);
        const generatedAt = freshness.generated_at ? new Date(freshness.generated_at) : null;
        const generatedLabel = generatedAt && !Number.isNaN(generatedAt.getTime()) ? generatedAt.toLocaleString() : "just now";
        const modeLabel = freshness.mode === 'browser_parent_cache'
            ? "Parent-period cache"
            : (freshness.authoritative ? "Authoritative source refresh" : "Prepared snapshot");
        status.textContent = modeLabel + (revision ? " · Version " + String(revision) : "") + " · Generated " + generatedLabel;
    }
    function cashflowDrilldownFilterState() {
        const selectedTreasury = element('cashflowsDrilldownTreasury').value;
        const effectiveTreasury = selectedTreasury || (cashflowTreasuryView === 'bank' ? 'Bank' : (cashflowTreasuryView === 'cash' ? 'Cash' : ''));
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
    function cacheCashflowDrilldownBaseline(result) {
        if (!result || !Array.isArray(result.base_rows)) return;
        if (!syncCashflowReportRevision(result)) return;
        const baseResult = Object.assign({}, result);
        delete baseResult.base_rows;
        cashflowDrilldownBaseCache.set(cashflowDrilldownContextKey(result.period_start, result.period_end), {
            rows: result.base_rows,
            result: baseResult,
            revision: cashflowReportRevision,
        });
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
        return [cashflowSelection.start, cashflowSelection.end, cashflowSelection.groupBy || 'month', cashflowTreasuryView, section, category, cashflowReportRevision].join('|');
    }
    function resetCashflowInlineLedgerState() {
        cashflowInlineLedgerGeneration += 1;
        cashflowInlineLedgerControllers.forEach(function (controller) { controller.abort(); });
        cashflowInlineLedgerControllers = new Map();
        cashflowInlineLedgerCache = new Map();
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
        const account = [item.accountName, item.accountCode].filter(Boolean).join(' / ') || 'Unlabelled ledger';
        return item.treasuryType ? account + ' / ' + item.treasuryType : account;
    }
    function cashflowInlineLedgerGroupValue(section, category) {
        return encodeURIComponent(cashflowInlineLedgerKey(section, category));
    }
    function cashflowInlineLedgerButton(row, category, section) {
        return '<button type=button class=cashflows-inline-ledger-link data-cashflow-inline-ledger=true data-cashflow-inline-category=' + encodeURIComponent(category) + ' data-cashflow-inline-section=' + encodeURIComponent(section) + ' data-cashflow-inline-account=' + encodeURIComponent(row.accountCode || '') + ' data-cashflow-inline-account-name=' + encodeURIComponent(row.accountName || '') + ' data-cashflow-inline-treasury=' + encodeURIComponent(row.treasuryType || '') + '>' + escapeHtml(cashflowInlineLedgerLabel(row)) + '</button>';
    }
    function cashflowInlineLedgerHostMarkup(category, section, colspan) {
        const group = cashflowInlineLedgerGroupValue(section, category);
        return '<tr class=cashflows-inline-ledger-host-row data-cashflow-inline-ledger-group="' + escapeHtml(group) + '"><td colspan="' + String(colspan) + '"><div class=cashflows-inline-ledger-host>Loading ledgers...</div></td></tr>';
    }
    function cashflowInlineLedgerRowMarkup(row, category, section, periodKeys, isSplit, group, index, rowCount) {
        const classes = ['cashflows-inline-ledger-row'];
        if (index === 0) classes.push('cashflows-inline-ledger-first');
        if (index === rowCount - 1) classes.push('cashflows-inline-ledger-last');
        const cells = ['<td class=cashflows-table-text>' + cashflowInlineLedgerButton(row, category, section) + '</td>'];
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
        const html = rows.map(function (row, index) {
            return cashflowInlineLedgerRowMarkup(row, category, section, periodKeys, isSplit, group, index, rows.length);
        }).join('');
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
            start: selected ? selected.start : cashflowSelection.start,
            end: selected ? selected.end : cashflowSelection.end,
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
        const drawer = ensureCashflowTransactionDrawer();
        const context = cashflowInlineTransactionContext;
        if (!context || drawer.hidden) return Promise.resolve(null);
        if (resetRequest) cashflowInlineTransactionRequestSerial += 1;
        if (cashflowInlineTransactionActiveController) cashflowInlineTransactionActiveController.abort();
        const requestSerial = cashflowInlineTransactionRequestSerial;
        const filters = cashflowDrawerFilters();
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
        if (filters.search) params.set('search', filters.search);
        setCashflowTransactionDrawerBusy(true);
        return api('/api/data/cashflows-classification-preview?' + params.toString(), { signal: controller.signal })
            .then(function (payload) {
                if (requestSerial !== cashflowInlineTransactionRequestSerial || drawer.hidden) return null;
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
    function loadCashflowInlineLedgers(button, host, category, section, periods, isSplit) {
        const key = cashflowInlineLedgerKey(section, category);
        const cached = cashflowInlineLedgerCache.get(key);
        if (cached) {
            renderCashflowInlineLedgerHost(host, cached, category, section, periods, isSplit);
            button.disabled = false;
            return Promise.resolve(cached);
        }
        const controller = new AbortController();
        const generation = cashflowInlineLedgerGeneration;
        cashflowInlineLedgerControllers.set(key, controller);
        const params = new URLSearchParams({
            client_id: String(clientId),
            period_start: cashflowSelection.start,
            period_end: cashflowSelection.end,
            group_by: cashflowSelection.groupBy || 'month',
            treasury_view: cashflowTreasuryView,
            category: category,
            page: '1',
            limit: String(cashflowInlineLedgerLimit),
        });
        return api('/api/reconciliation/cashflows/drilldown?' + params.toString(), { signal: controller.signal })
            .then(function (payload) {
                if (generation !== cashflowInlineLedgerGeneration) return null;
                if (!syncCashflowReportRevision(payload)) return null;
                cashflowInlineLedgerCache.set(cashflowInlineLedgerKey(section, category), payload);
                renderCashflowInlineLedgerHost(host, payload, category, section, periods, isSplit);
                return payload;
            })
            .catch(function (error) {
                if (error.name === 'AbortError') return null;
                const hostPair = host && host.left ? host : { left: host, right: null };
                if (generation === cashflowInlineLedgerGeneration && hostPair.left && hostPair.left.isConnected) {
                    hostPair.left.querySelector('.cashflows-inline-ledger-host').innerHTML = '<span class=cashflows-inline-ledger-error>Unable to load ledgers: ' + escapeHtml(error.message) + '</span>';
                    if (hostPair.right && hostPair.right.isConnected) hostPair.right.querySelector('.cashflows-inline-ledger-host').innerHTML = '';
                }
                return null;
            })
            .finally(function () {
                if (cashflowInlineLedgerControllers.get(key) === controller) cashflowInlineLedgerControllers.delete(key);
                if (button.isConnected) button.disabled = false;
            });
    }
    function toggleCashflowInlineCategory(button) {
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
            loadCashflowInlineLedgers(button, { left: leftHost, right: rightHost }, category, section, periods, isSplit);
            return;
        }
        const template = document.createElement('tbody');
        template.innerHTML = cashflowInlineLedgerHostMarkup(category, section, colspan);
        const host = template.firstElementChild;
        row.parentNode.insertBefore(host, row.nextSibling);
        button.setAttribute('aria-expanded', 'true');
        button.disabled = true;
        loadCashflowInlineLedgers(button, host, category, section, periods, isSplit);
    }
    function loadCashflowDrilldown(resetPage) {
        const start = element('cashflowsPeriodStart').value;
        const end = element('cashflowsPeriodEnd').value;
        if (!clientId || !start || !end) return Promise.resolve(null);
        if (resetPage) cashflowDrilldownPage = 1;
        const filters = cashflowDrilldownFilterState();
        const baseline = cashflowDrilldownBaseCache.get(cashflowDrilldownContextKey(start, end));
        if (baseline) {
            return ensureCashflowRevisionFresh(false).then(function (isFresh) {
                const currentBaseline = cashflowDrilldownBaseCache.get(cashflowDrilldownContextKey(start, end));
                if (!isFresh || !currentBaseline) return loadCashflowDrilldown(resetPage);
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
            group_by: cashflowSelection.groupBy || 'month',
            treasury_view: cashflowTreasuryView,
            page: String(cashflowDrilldownPage),
            limit: String(cashflowDrilldownLimit),
        });
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
        setCashflowProcessingStep('load', 'Loading ledger details.');
        element('cashflowsLedgerDrilldownTable').innerHTML = '<tbody><tr><td class=cashflows-empty colspan=7>Loading ledger details...</td></tr></tbody>';
        return api('/api/reconciliation/cashflows/drilldown?' + params.toString(), { signal: controller.signal })
            .then(function (result) {
                if (generation !== cashflowDrilldownGeneration) return null;
                if (!syncCashflowReportRevision(result)) return null;
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
        if (sourceRow.classList.contains('cashflows-summary-section-row')) {
            const leftCell = cells[0] ? cells[0].cloneNode(true) : document.createElement('th');
            const rightCell = cells[0] ? cells[0].cloneNode(true) : document.createElement('th');
            const sourceColspan = Number(leftCell.getAttribute('colspan')) || cells.length || 1;
            leftCell.setAttribute('colspan', '1');
            rightCell.setAttribute('colspan', String(Math.max(sourceColspan - 1, 1)));
            rightCell.textContent = '';
            leftRow.appendChild(leftCell);
            rightRow.appendChild(rightCell);
        } else if (isHeaderContinuation) {
            const placeholder = document.createElement('th');
            placeholder.setAttribute('aria-hidden', 'true');
            leftRow.appendChild(placeholder);
            cells.forEach(function (cell) { rightRow.appendChild(cell.cloneNode(true)); });
        } else {
            if (cells[0]) leftRow.appendChild(cells[0].cloneNode(true));
            cells.slice(1).forEach(function (cell) { rightRow.appendChild(cell.cloneNode(true)); });
        }
        if (!rightRow.children.length) {
            const filler = document.createElement(sourceRow.parentElement && sourceRow.parentElement.tagName === 'THEAD' ? 'th' : 'td');
            filler.setAttribute('aria-hidden', 'true');
            rightRow.appendChild(filler);
        }
        return { left: leftRow, right: rightRow };
    }
    function syncCashflowSummaryPaneRows(host) {
        const layout = host && host.querySelector('.cashflows-summary-pane-layout');
        if (!layout) return;
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
    function applyCashflowSummaryColumnContract(sourceTable, tables, isSplit) {
        const sourceRow = sourceTable.querySelector('thead tr') || sourceTable.querySelector('tbody tr');
        const logicalCount = Math.max(cashflowSummaryLogicalColumnCount(sourceRow), 2);
        const frozenWidth = 360;
        const rightCount = Math.max(logicalCount - 1, 1);
        const rightWidths = Array.from({ length: rightCount }, function (_, index) {
            if (isSplit) return index >= rightCount - 2 ? (index === rightCount - 2 ? 100 : 120) : 96;
            if (index >= rightCount - 3) return index === rightCount - 3 ? 128 : index === rightCount - 2 ? 100 : 120;
            return 112;
        });
        const addColGroup = function (table, widths) {
            const colgroup = document.createElement('colgroup');
            widths.forEach(function (width) {
                const col = document.createElement('col');
                col.style.width = width + 'px';
                colgroup.appendChild(col);
            });
            table.insertBefore(colgroup, table.firstChild);
            table.style.width = widths.reduce(function (sum, width) { return sum + width; }, 0) + 'px';
        };
        addColGroup(tables.frozenHeader, [frozenWidth]);
        addColGroup(tables.frozenBody, [frozenWidth]);
        addColGroup(tables.scrollHeader, rightWidths);
        addColGroup(tables.scrollBody, rightWidths);
    }
    function renderCashflowSummaryPaneLayout(container, markup, isSplit) {
        const sourceTable = document.createElement('table');
        sourceTable.className = 'cashflows-table';
        sourceTable.innerHTML = markup;
        const layout = document.createElement('div');
        layout.className = 'cashflows-summary-pane-layout';
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
                const parts = cashflowSummaryPaneRowParts(sourceRow, tagName + '-' + index, tagName === 'thead' && sourceRow.classList.contains('cashflows-split-header-sub'));
                frozenSection.appendChild(parts.left);
                scrollSection.appendChild(parts.right);
            });
            frozenTable.appendChild(frozenSection);
            scrollTable.appendChild(scrollSection);
        });
        applyCashflowSummaryColumnContract(sourceTable, {
            frozenHeader: frozenHeaderTable,
            frozenBody: frozenTable,
            scrollHeader: scrollHeaderTable,
            scrollBody: scrollTable
        }, isSplit);
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
        requestAnimationFrame(function () {
            syncCashflowSummaryPaneRows(container);
            syncCashflowSummaryHeaderScroll(container);
            syncCashflowSummaryRowHover(container);
        });
    }
    function renderCashflowSplitSummary(preview, table) {
        const periods = cashflowPreviewPeriods(preview);
        const periodKeys = periods.map(function (period) { return String(period.key); });
        const allRows = cashflowSplitRows(preview);
        const showMixedColumn = allRows.some(function (row) {
            return Math.abs(Number(row.mixedTotal) || 0) > 0.005 || periodKeys.some(function (periodKey) {
                const values = row.periods[periodKey] || {};
                return Math.abs(Number(values.mixed) || 0) > 0.005;
            });
        });
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
        const visibleFields = showMixedColumn ? ['bank', 'cash', 'mixed', 'total'] : ['bank', 'cash', 'total'];
        const subColumns = showMixedColumn ? ['Bank', 'Cash', 'Mixed / unresolved', 'Total'] : ['Bank', 'Cash', 'Total'];
        const columnCount = 1 + ((periods.length + 1) * subColumns.length) + 2;
        if (!summaryRows.length) {
            renderCashflowSummaryPaneLayout(table, '<tbody><tr><td class="cashflows-empty" colspan="' + columnCount + '">No Cash Flow activity was found for this period.</td></tr></tbody>', true);
            return;
        }
        let html = '<thead><tr class="cashflows-split-header-top"><th rowspan="2">Category</th>';
        periods.forEach(function (period) { html += '<th colspan="' + subColumns.length + '">' + escapeHtml(period.label) + '</th>'; });
        html += '<th colspan="' + subColumns.length + '">Selected total</th><th rowspan="2">% of section</th><th rowspan="2">Local total</th></tr><tr class="cashflows-split-header-sub">';
        periods.concat([{ label: 'Selected total' }]).forEach(function () {
            subColumns.forEach(function (label) { html += '<th class="cashflows-table-number">' + escapeHtml(label) + '</th>'; });
        });
        html += '</tr></thead><tbody>';
        ['Receipts', 'Payments'].forEach(function (section) {
            const rows = sectionRows[section];
            if (!rows.length) return;
            const basis = rows.reduce(function (sum, row) { return sum + row.total; }, 0);
            html += '<tr class="cashflows-summary-section-row cashflows-summary-section-' + section.toLowerCase() + '"><th colspan="' + columnCount + '">' + escapeHtml(section) + '</th></tr>';
            rows.forEach(function (row) {
                let cells = ['<td class="cashflows-table-text">' + escapeHtml(row.category) + '</td>'];
                cells[0] = '<td class=cashflows-table-text>' + cashflowInlineCategoryCell(row.category, section) + '</td>';
                periodKeys.forEach(function (periodKey) {
                    const values = row.periods[periodKey] || { bank: 0, cash: 0, mixed: 0, total: 0 };
                    visibleFields.forEach(function (field) {
                        cells.push('<td class="cashflows-table-number">' + escapeHtml(formatCashflowSectionAmount(values[field], section)) + '</td>');
                    });
                });
                const totalValues = { bank: row.bankTotal, cash: row.cashTotal, mixed: row.mixedTotal, total: row.totalRaw };
                visibleFields.forEach(function (field) {
                    cells.push('<td class="cashflows-table-number">' + escapeHtml(formatCashflowSectionAmount(totalValues[field], section)) + '</td>');
                });
                cells.push('<td class="cashflows-table-number">' + escapeHtml(formatPercentage(basis ? row.total / basis : 0)) + '</td>');
                cells.push('<td class="cashflows-table-number">' + escapeHtml(formatLocalAmount(row.total)) + '</td>');
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
                visibleFields.forEach(function (field) { html += '<td class="cashflows-table-number">' + escapeHtml(field === 'total' ? formatInternationalAmount(values[field]) : formatCashflowSectionAmount(values[field], section)) + '</td>'; });
            });
            visibleFields.forEach(function (field) { html += '<td class="cashflows-table-number">' + escapeHtml(field === 'total' ? formatInternationalAmount(basis) : formatCashflowSectionAmount(totalFields[field], section)) + '</td>'; });
            html += '<td class="cashflows-table-number">100.00%</td><td class="cashflows-table-number">' + escapeHtml(formatLocalAmount(basis)) + '</td></tr>';
        });
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
        if (!summaryRows.length) {
            renderCashflowSummaryPaneLayout(table, '<tbody><tr><td class="cashflows-empty" colspan="' + Math.max(columns.length, 1) + '">No Cash Flow activity was found for this period.</td></tr></tbody>', false);
            return;
        }
        let html = '<thead><tr>' + columns.map(function (label) { return '<th>' + escapeHtml(label) + '</th>'; }).join('') + '</tr></thead><tbody>';
        ['Receipts', 'Payments'].forEach(function (section) {
            const rows = sectionRows[section];
            if (!rows.length) return;
            const basis = rows.reduce(function (sum, row) { return sum + row.total; }, 0);
            html += '<tr class="cashflows-summary-section-row cashflows-summary-section-' + section.toLowerCase() + '"><th colspan="' + columns.length + '">' + escapeHtml(section) + '</th></tr>';
            rows.forEach(function (row) {
                const cells = [escapeHtml(row.category)];
                cells[0] = cashflowInlineCategoryCell(row.category, section);
                row.values.forEach(function (value) { cells.push(escapeHtml(formatCashflowSectionAmount(value, section))); });
                cells.push(escapeHtml(formatInternationalAmount(row.total)));
                cells.push(escapeHtml(formatPercentage(basis ? row.total / basis : 0)));
                cells.push(escapeHtml(formatLocalAmount(row.total)));
                html += '<tr>' + cells.map(function (cell) { return '<td>' + cell + '</td>'; }).join('') + '</tr>';
            });
            const periodTotals = periodKeys.map(function (_, index) { return rows.reduce(function (sum, row) { return sum + row.values[index]; }, 0); });
            html += '<tr class="cashflows-summary-total-row"><th>' + escapeHtml('Total ' + section) + '</th>';
            periodTotals.forEach(function (value) { html += '<td>' + escapeHtml(formatCashflowSectionAmount(value, section)) + '</td>'; });
            html += '<td>' + escapeHtml(formatInternationalAmount(basis)) + '</td><td>100.00%</td><td>' + escapeHtml(formatLocalAmount(basis)) + '</td></tr>';
        });
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
    function renderResult(result) {
        if (!syncCashflowReportRevision(result)) return;
        rememberCashflowSummary(result);
        lastCashflowsResult = result;
        renderCashflowFreshness(result);
        cashflowDrilldownBaseCache.clear();
        const preview = result.preview || {};
        resetCashflowInlineLedgerState();
        closeCashflowDrilldown();
        cashflowTreasuryView = String(preview.treasury_view || cashflowTreasuryView || 'combined');
        syncCashflowTreasuryView();
        renderCashflowSummary(preview);
        const summaryRows = cashflowSummaryRows(preview);
        renderCategoryOptions(summaryRows.map(function (row) { return row.category; }), true);
        renderCashflowDrilldownCategoryOptions(summaryRows);
        const source = result.source || {};
        const exportButton = element('cashflowsExportButton');
        if (exportButton) exportButton.hidden = !source.ready || !(preview.periods || []).length;
        const ledgerDrilldownButton = element('cashflowsLedgerDrilldownButton');
        if (ledgerDrilldownButton) ledgerDrilldownButton.hidden = !source.ready || !(preview.periods || []).length;

        resetLazyCashflowSections();
        const lazyActions = element('cashflowsLazyActions');
        lazyActions.hidden = !source.ready;
        element('cashflowsLoadTransfersButton').textContent = 'Load internal transfers (' + String(preview.internal_transfer_count || 0) + ')';
        element('cashflowsLoadDeductionsButton').textContent = 'Load customer deductions (' + String(preview.deduction_count || 0) + ')';
        element('cashflowsLoadReviewButton').textContent = 'Load review required (' + String(preview.category_review_count || 0) + ')';

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
        else if (reviewCount) showCashflowToast(String(reviewCount) + ' counter-account line(s) need category review.', false);
    }
    async function downloadCashflowReport(mode) {
        const isDrilldown = mode === 'drilldown';
        const button = element(isDrilldown ? 'cashflowsDrilldownExportButton' : 'cashflowsExportButton');
        const start = element('cashflowsPeriodStart').value;
        const end = element('cashflowsPeriodEnd').value;
        if (!button || !start || !end) return;
        button.disabled = true;
        button.textContent = 'Preparing export.';
        try {
            const query = new URLSearchParams({
                client_id: clientId,
                period_start: start,
                period_end: end,
                group_by: cashflowSelection.groupBy || 'month',
                treasury_view: cashflowTreasuryView,
            });
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
            button.textContent = isDrilldown ? 'Export filtered XLSX' : 'Export XLSX';
        }
    }

    async function generateCashflows(event, options) {
        if (event) event.preventDefault();
        const forceRefresh = !!(options && options.forceRefresh);
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
        const start = element('cashflowsPeriodStart').value;
        const end = element('cashflowsPeriodEnd').value;
        const cachedSummary = !forceRefresh
            ? (takeCashflowSummary(start, end, cashflowSelection.groupBy, cashflowTreasuryView)
                || findCashflowParentSummary(start, end, cashflowSelection.groupBy, cashflowTreasuryView))
            : null;
        const controller = new AbortController();
        const timeout = window.setTimeout(function () { controller.abort(); }, CASHFLOW_REQUEST_TIMEOUT_MS);
        try {
            if (cachedSummary) {
                setCashflowProcessingStep('load', 'Checking the cached Cashflows summary.');
                const isFresh = await ensureCashflowRevisionFresh(true, true);
                const freshSummary = isFresh
                    ? (takeCashflowSummary(start, end, cashflowSelection.groupBy, cashflowTreasuryView)
                        || findCashflowParentSummary(start, end, cashflowSelection.groupBy, cashflowTreasuryView))
                    : null;
                if (freshSummary) {
                    setCashflowProcessingStep('render', 'Rendering the cached Cashflows summary.');
                    renderResult(freshSummary);
                    showCashflowResultNotice(freshSummary);
                    return;
                }
            }
            setCashflowProcessingStep('load', 'Loading Cashflows data for the selected period.');
            const query = new URLSearchParams({ client_id: clientId, period_start: start, period_end: end, group_by: cashflowSelection.groupBy, treasury_view: cashflowTreasuryView, details: 'summary' });
            if (forceRefresh) query.set('refresh', 'true');
            const result = await api('/api/reconciliation/cashflows/preview?' + query.toString(), { signal: controller.signal });
            setCashflowProcessingStep('render', 'Rendering the grouped Cashflows summary.');
            renderResult(result);
            if (!element('cashflowsTransactionsSection').hidden) await loadClassifiedTransactions(true);
            showCashflowResultNotice(result);
        } catch (error) {
            showCashflowToast(error.name === 'AbortError'
                ? 'Cashflows is taking longer than expected. Please try again or review the configured Transaction Report template.'
                : error.message, true);
        } finally {
            window.clearTimeout(timeout);
            hideCashflowProcessing();
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
            const cells = values.map(function (value, valueIndex) {
                const label = columns[valueIndex];
                const className = ['Type', 'Account', 'Assigned Category'].indexOf(label) >= 0 ? ' class="cashflows-classification-cell"' : '';
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
        const configuration = (lastCashflowsResult || {}).configuration || {};
        const templateId = configuration.source_template_id;
        const start = element('cashflowsPeriodStart').value;
        const end = element('cashflowsPeriodEnd').value;
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
        if (cashflowChildRequest) return cashflowChildRequest;
        const configuration = (lastCashflowsResult || {}).configuration || {};
        const start = element('cashflowsPeriodStart').value;
        const end = element('cashflowsPeriodEnd').value;
        if (!configuration.source_template_id || !clientId || !start || !end) return Promise.resolve(null);
        const buttonIds = { transfers: 'cashflowsLoadTransfersButton', customer_payments: 'cashflowsLoadCustomerPaymentsButton', deductions: 'cashflowsLoadDeductionsButton', review: 'cashflowsLoadReviewButton' };
        const labels = { transfers: 'Load internal transfers', customer_payments: 'Load customer payments', deductions: 'Load customer deductions', review: 'Load review required' };
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
            group_by: cashflowSelection.groupBy,
            details: 'transfers,customer_payments,deductions,review'
        });
        cashflowChildRequest = api('/api/reconciliation/cashflows/preview?' + params.toString())
            .then(function (result) {
                if (generation !== cashflowChildGeneration) return null;
                const preview = result.preview || {};
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
        const params = new URLSearchParams(window.location.search);
        params.delete('report');
        params.set('view', 'reports');
        window.history.replaceState({}, '', window.location.pathname + '?' + params.toString());
        setReportView(false);
    }
    function defaultPeriods() {
        const today = new Date();
        cashflowGroupingPreference = 'month';
        const start = toIso(yearStart(today)); const end = toIso(today);
        cashflowSelection = { start: start, end: end, groupBy: 'month' };
        syncCashflowSelection();
    }
    async function loadCashflowDefaultPeriods() {
        try {
            const result = await api('/api/settings/cashflows-defaults');
            const resolved = result.resolved_period || {};
            if (!resolved.start || !resolved.end) throw new Error('Incomplete Cashflows defaults');
            cashflowGroupingPreference = 'month';
            cashflowSelection = { start: resolved.start, end: resolved.end, groupBy: 'month' };
            syncCashflowSelection();
        } catch (error) {
            defaultPeriods();
        }
    }
    async function initialise() {
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
        });
        document.addEventListener('click', function (event) {
            const popover = element('cashflowsPeriodPopover');
            const periodControl = popover.closest('.cashflows-period-control');
            const eventPath = typeof event.composedPath === 'function' ? event.composedPath() : [];
            const insidePeriodControl = eventPath.length
                ? eventPath.indexOf(periodControl) >= 0
                : !!event.target.closest('.cashflows-period-control');
            if (!insidePeriodControl && !popover.hidden) closeCashflowPopover();
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
        element('cashflowsSummaryTable').addEventListener('click', function (event) {
            const ledgerButton = event.target.closest('[data-cashflow-inline-ledger]');
            if (ledgerButton) {
                toggleCashflowInlineLedger(ledgerButton);
                return;
            }
            const button = event.target.closest('[data-cashflow-inline-category]');
            if (button) toggleCashflowInlineCategory(button);
        });
        element('cashflowsViewTransactionsButton').addEventListener('click', toggleClassifiedTransactions);
        element('cashflowsHideZeroRowsButton').addEventListener('click', toggleCashflowZeroRows);
        element('cashflowsRefreshButton').addEventListener('click', function () { generateCashflows(null, { forceRefresh: true }); });
        element('cashflowsLoadTransfersButton').addEventListener('click', function () { loadCashflowChild('transfers'); });
        element('cashflowsLoadCustomerPaymentsButton').addEventListener('click', function () { loadCashflowChild('customer_payments'); });
        element('cashflowsLoadDeductionsButton').addEventListener('click', function () { loadCashflowChild('deductions'); });
        element('cashflowsLoadReviewButton').addEventListener('click', function () { loadCashflowChild('review'); });
        const cashflowDetailSearches = [
            { section: 'transfers', input: 'cashflowsTransfersSearch', clear: 'cashflowsTransfersSearchClear' },
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
        if (requestedCard && clientId) setSelection(requestedCard);
    }
    document.addEventListener('DOMContentLoaded', initialise);
}());
