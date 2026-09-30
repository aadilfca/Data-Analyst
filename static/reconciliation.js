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
    let cashflowGroupingPreference = 'suggested';
    let stagedCashflowSelection = null;
    let activeCashflowPicker = null;
    let cashflowSelectedPeriods = [];
    let cashflowPickerView = null;
    let cashflowToastTimer = null;
    let cashflowProcessingControls = [];
    let cashflowSummaryFilter = '';
    let hideCashflowZeroRows = false;
    let lastClassifiedTransactionsResult = null;
    let cashflowChildPreviews = {};
    let cashflowChildRequest = null;
    let cashflowChildGeneration = 0;

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
        return amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function formatLocalAmount(value) {
        const amount = Number(value || 0);
        const absolute = Math.abs(amount);
        let divisor = 1;
        let unit = 'Rs';
        if (absolute >= 10000000) { divisor = 10000000; unit = 'crore'; }
        else if (absolute >= 100000) { divisor = 100000; unit = 'lakh'; }
        else if (absolute >= 1000) { divisor = 1000; unit = 'thousand'; }
        const label = (absolute / divisor).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ' + unit;
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
    function showCashflowProcessing() {
        cashflowProcessingControls = Array.from(document.querySelectorAll('button, select, input, textarea')).map(function (control) {
            return { element: control, disabled: control.disabled };
        });
        cashflowProcessingControls.forEach(function (item) { item.element.disabled = true; });
        element('cashflowsView').setAttribute('aria-busy', 'true');
        document.body.classList.add('cashflows-processing-open');
        element('cashflowsProcessing').hidden = false;
        setCashflowProcessingStep('prepare', 'Preparing the selected period.');
    }
    function hideCashflowProcessing() {
        cashflowProcessingControls.forEach(function (item) { item.element.disabled = item.disabled; });
        cashflowProcessingControls = [];
        element('cashflowsView').removeAttribute('aria-busy');
        document.body.classList.remove('cashflows-processing-open');
        element('cashflowsProcessing').hidden = true;
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
    function renderTable(table, columns, rows, emptyMessage) {
        const visibleRows = cashflowRowsWithNonZeroAmounts(columns, rows);
        if (!visibleRows.length) {
            table.innerHTML = '<tbody><tr><td class="cashflows-empty" colspan="' + Math.max(columns.length, 1) + '">' + escapeHtml(emptyMessage) + '</td></tr></tbody>';
            return;
        }
        table.innerHTML = '<thead><tr>' + columns.map(function (column) { return '<th>' + escapeHtml(column.label) + '</th>'; }).join('') + '</tr></thead><tbody>' + visibleRows.map(function (row) {
            return '<tr>' + columns.map(function (column) {
                const value = row[column.key];
                return '<td>' + escapeHtml(column.amount ? formatInternationalAmount(value) : (value == null ? '' : value)) + '</td>';
            }).join('') + '</tr>';
        }).join('') + '</tbody>';
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
    function resetLazyCashflowSections() {
        cashflowChildGeneration += 1;
        cashflowChildRequest = null;
        ['cashflowsTransfersSection', 'cashflowsDeductionsSection', 'cashflowsReviewSection'].forEach(function (id) {
            element(id).hidden = true;
        });
        ['cashflowsLoadTransfersButton', 'cashflowsLoadDeductionsButton', 'cashflowsLoadReviewButton'].forEach(function (id) {
            element(id).disabled = false;
        });
        cashflowChildPreviews = {};
    }
    function renderCashflowChild(section, preview) {
        if (section === 'transfers') {
            const rows = (preview.transfer_details || []).map(function (row) {
                return Object.assign({}, row, { matched: row.matched ? 'Yes' : 'Shown separately' });
            });
            renderTable(element('cashflowsTransfersTable'), [
                { key: 'voucher_id', label: 'Voucher' }, { key: 'month', label: 'Month' }, { key: 'transfer_type', label: 'Transfer' }, { key: 'amount', label: 'Amount', amount: true }, { key: 'matched', label: 'Matched' },
            ], rows, 'No internal transfers identified.');
            element('cashflowsTransferCount').textContent = String(preview.internal_transfer_count || rows.length || 0) + ' voucher(s)';
            element('cashflowsTransfersSection').hidden = false;
        } else if (section === 'deductions') {
            const rows = preview.deduction_details || [];
            renderTable(element('cashflowsDeductionsTable'), [
                { key: 'month', label: 'Month' }, { key: 'category', label: 'Category' }, { key: 'account_name', label: 'Account' }, { key: 'amount', label: 'Amount', amount: true },
            ], rows, 'No customer deductions identified.');
            element('cashflowsDeductionCount').textContent = String(preview.deduction_count || rows.length || 0) + ' line(s)';
            element('cashflowsDeductionsSection').hidden = false;
        } else if (section === 'review') {
            const rows = preview.categories_needing_review || [];
            renderTable(element('cashflowsReviewTable'), [
                { key: 'voucher_id', label: 'Voucher' }, { key: 'account_code', label: 'Account code' }, { key: 'account_name', label: 'Account' }, { key: 'fallback_category', label: 'Review category' },
            ], rows, 'No review items.');
            element('cashflowsReviewCount').textContent = String(preview.category_review_count || rows.length || 0) + ' line(s)';
            element('cashflowsReviewSection').hidden = false;
        }
    }
    function renderCashflowSummary(preview) {
        const table = element('cashflowsSummaryTable');
        const periods = cashflowPreviewPeriods(preview);
        const periodKeys = periods.map(function (period) { return period.key; });
        const rawRows = preview.category_totals || [];
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
                values: periodKeys.map(function (key) { return Math.abs(Number(row[key]) || 0); }),
                total: Math.abs(total)
            });
        });
        const columns = ['Category'].concat(periods.map(function (period) { return period.label; })).concat(['Total', '% of section', 'Local total']);
        if (!summaryRows.length) {
            table.innerHTML = '<tbody><tr><td class="cashflows-empty" colspan="' + Math.max(columns.length, 1) + '">No Cash Flow activity was found for this period.</td></tr></tbody>';
            return;
        }
        let html = '<thead><tr>' + columns.map(function (label) { return '<th>' + escapeHtml(label) + '</th>'; }).join('') + '</tr></thead><tbody>';
        ['Receipts', 'Payments'].forEach(function (section) {
            const rows = sectionRows[section];
            if (!rows.length) return;
            const basis = rows.reduce(function (sum, row) { return sum + row.total; }, 0);
            html += '<tr><th colspan="' + columns.length + '">' + escapeHtml(section) + '</th></tr>';
            rows.forEach(function (row) {
                const cells = [escapeHtml(row.category)];
                row.values.forEach(function (value) { cells.push(escapeHtml(formatInternationalAmount(value))); });
                cells.push(escapeHtml(formatInternationalAmount(row.total)));
                cells.push(escapeHtml(formatPercentage(basis ? row.total / basis : 0)));
                cells.push(escapeHtml(formatLocalAmount(row.total)));
                html += '<tr>' + cells.map(function (cell) { return '<td>' + cell + '</td>'; }).join('') + '</tr>';
            });
            const periodTotals = periodKeys.map(function (_, index) { return rows.reduce(function (sum, row) { return sum + row.values[index]; }, 0); });
            html += '<tr><th>' + escapeHtml('Total ' + section) + '</th>';
            periodTotals.forEach(function (value) { html += '<td>' + escapeHtml(formatInternationalAmount(value)) + '</td>'; });
            html += '<td>' + escapeHtml(formatInternationalAmount(basis)) + '</td><td>100.00%</td><td>' + escapeHtml(formatLocalAmount(basis)) + '</td></tr>';
        });
        table.innerHTML = html + '</tbody>';

    }
    function renderResult(result) {
        lastCashflowsResult = result;
        const preview = result.preview || {};
        renderCashflowSummary(preview);
        renderCategoryOptions((preview.category_totals || []).map(function (row) { return row.category; }), true);
        element('cashflowsReceiptMode').textContent = 'Receipt presentation: ' + String(preview.receipt_presentation || 'net').toUpperCase();
        const source = result.source || {};
        const exportButton = element('cashflowsExportButton');
        if (exportButton) exportButton.hidden = !source.ready || !(preview.periods || []).length;

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
    }
    function showCashflowResultNotice(result) {
        const preview = result.preview || {};
        const blockingReasons = result.blocking_reasons || [];
        const reviewCount = Number(preview.category_review_count || 0);
        if (blockingReasons.length) showCashflowToast(blockingReasons.join(' '), true);
        else if (reviewCount) showCashflowToast(String(reviewCount) + ' counter-account line(s) need category review.', false);
    }
    async function downloadCashflowReport() {
        const button = element('cashflowsExportButton');
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
            });
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
            button.textContent = 'Export XLSX';
        }
    }

    async function generateCashflows(event) {
        if (event) event.preventDefault();
        const exportButton = element('cashflowsExportButton');
        if (exportButton) exportButton.hidden = true;
        cashflowSummaryFilter = '';
        element('cashflowsSummaryFilter').value = '';
        lastCashflowsResult = null;
        hideCashflowZeroRows = false;
        lastClassifiedTransactionsResult = null;
        cashflowChildPreviews = {};
        element('cashflowsHideZeroRowsButton').disabled = true;
        element('cashflowsHideZeroRowsButton').textContent = 'Hide zero rows';
        showCashflowProcessing();
        const start = element('cashflowsPeriodStart').value;
        const end = element('cashflowsPeriodEnd').value;
        const controller = new AbortController();
        const timeout = window.setTimeout(function () { controller.abort(); }, CASHFLOW_REQUEST_TIMEOUT_MS);
        try {
            setCashflowProcessingStep('load', 'Loading Cashflows data for the selected period.');
            const query = new URLSearchParams({ client_id: clientId, period_start: start, period_end: end, group_by: cashflowSelection.groupBy, details: 'summary' });
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
            section.hidden = false;
            button.textContent = 'Hide classified transactions';
            await loadClassifiedTransactions(true);
        } else {
            section.hidden = true;
            button.textContent = 'View classified transactions';
        }
    }
    function loadCashflowChildren() {
        if (cashflowChildRequest) return cashflowChildRequest;
        const configuration = (lastCashflowsResult || {}).configuration || {};
        const start = element('cashflowsPeriodStart').value;
        const end = element('cashflowsPeriodEnd').value;
        if (!configuration.source_template_id || !clientId || !start || !end) return Promise.resolve(null);
        const buttonIds = { transfers: 'cashflowsLoadTransfersButton', deductions: 'cashflowsLoadDeductionsButton', review: 'cashflowsLoadReviewButton' };
        const labels = { transfers: 'Load internal transfers', deductions: 'Load customer deductions', review: 'Load review required' };
        const generation = cashflowChildGeneration;
        Object.keys(buttonIds).forEach(function (name) {
            const button = element(buttonIds[name]);
            if (button) {
                button.disabled = true;
                button.textContent = 'Loading…';
            }
        });
        const params = new URLSearchParams({
            client_id: String(clientId),
            period_start: start,
            period_end: end,
            group_by: cashflowSelection.groupBy,
            details: 'transfers,deductions,review'
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
                }
                return null;
            })
            .finally(function () {
                if (generation === cashflowChildGeneration) cashflowChildRequest = null;
            });
        return cashflowChildRequest;
    }
    async function loadCashflowChild(section) {
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
        element('cashflowsExportButton').addEventListener('click', downloadCashflowReport);
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
        element('cashflowsViewTransactionsButton').addEventListener('click', toggleClassifiedTransactions);
        element('cashflowsHideZeroRowsButton').addEventListener('click', toggleCashflowZeroRows);
        element('cashflowsLoadTransfersButton').addEventListener('click', function () { loadCashflowChild('transfers'); });
        element('cashflowsLoadDeductionsButton').addEventListener('click', function () { loadCashflowChild('deductions'); });
        element('cashflowsLoadReviewButton').addEventListener('click', function () { loadCashflowChild('review'); });
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
