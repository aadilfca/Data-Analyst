/* Reports centre and the first simple Cashflows report. */
(function () {
    'use strict';
    let clientId = null;
    const CASHFLOW_REQUEST_TIMEOUT_MS = 120000;

    function element(id) { return document.getElementById(id); }
    function escapeHtml(value) {
        return String(value == null ? '' : value).replace(/[&<>'"]/g, function (character) {
            return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character];
        });
    }
    function formatAmount(value) {
        const amount = Number(value || 0);
        return amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    async function api(url, options) {
        const response = await fetch(url, options);
        const payload = await response.json().catch(function () { return {}; });
        if (!response.ok) throw new Error(payload.detail || payload.message || 'The Cashflows report could not be loaded.');
        return payload;
    }
    function setMessage(message, isError) {
        const box = element('cashflowsMessage');
        box.textContent = message || '';
        box.className = 'cashflows-message' + (isError ? ' error' : '');
        box.hidden = !message;
    }
    function renderTable(table, columns, rows, emptyMessage) {
        if (!rows.length) {
            table.innerHTML = '<tbody><tr><td class="cashflows-empty" colspan="' + Math.max(columns.length, 1) + '">' + escapeHtml(emptyMessage) + '</td></tr></tbody>';
            return;
        }
        table.innerHTML = '<thead><tr>' + columns.map(function (column) { return '<th>' + escapeHtml(column.label) + '</th>'; }).join('') + '</tr></thead><tbody>' + rows.map(function (row) {
            return '<tr>' + columns.map(function (column) {
                const value = row[column.key];
                return '<td>' + escapeHtml(column.amount ? formatAmount(value) : (value == null ? '' : value)) + '</td>';
            }).join('') + '</tr>';
        }).join('') + '</tbody>';
    }
    function renderControls(controls) {
        element('cashflowsControls').innerHTML = Object.keys(controls || {}).map(function (key) {
            const control = controls[key] || {};
            return '<div class="cashflows-control ' + String(control.status || '').toLowerCase() + '"><strong>' + escapeHtml(key.replace(/_/g, ' ')) + '</strong><span>' + escapeHtml(control.status || '') + '</span><small>' + escapeHtml(control.message || '') + '</small></div>';
        }).join('');
    }
    function renderSourceQuality(source) {
        const box = element('cashflowsSourceQuality');
        const quality = source.quality || {};
        const warningCount = (source.diagnostics || []).filter(function (item) { return item.severity === 'warning'; }).length;
        const hasQuality = quality.source_row_count || quality.valid_row_count || quality.rejected_row_count || quality.layout_row_count || warningCount;
        if (!hasQuality) {
            box.hidden = true;
            box.textContent = '';
            return;
        }
        box.hidden = false;
        box.textContent = 'Source quality: ' + String(quality.valid_row_count || 0) + ' valid row(s), ' + String(quality.rejected_row_count || 0) + ' quarantined, ' + String(quality.layout_row_count || 0) + ' layout row(s) removed' + (warningCount ? ', ' + warningCount + ' warning(s).' : '.');
    }
    function renderResult(result) {
        const preview = result.preview || {};
        const summaryRows = preview.category_totals || [];
        const months = preview.months || [];
        const columns = [{ key: 'category', label: 'Category' }].concat(months.map(function (month) { return { key: month, label: month, amount: true }; })).concat([{ key: 'Total', label: 'Total', amount: true }]);
        renderTable(element('cashflowsSummaryTable'), columns, summaryRows, 'No Cash Flow activity was found for this period.');
        renderTable(element('cashflowsTransfersTable'), [
            { key: 'voucher_id', label: 'Voucher' }, { key: 'month', label: 'Month' }, { key: 'transfer_type', label: 'Transfer' }, { key: 'amount', label: 'Amount', amount: true }, { key: 'matched', label: 'Matched' },
        ], (preview.transfer_details || []).map(function (row) { return Object.assign({}, row, { matched: row.matched ? 'Yes' : 'Shown separately' }); }), 'No internal transfers identified.');
        renderTable(element('cashflowsDeductionsTable'), [
            { key: 'month', label: 'Month' }, { key: 'category', label: 'Category' }, { key: 'account_name', label: 'Account' }, { key: 'amount', label: 'Amount', amount: true },
        ], preview.deduction_details || [], 'No customer deductions identified.');
        const reviewRows = preview.categories_needing_review || [];
        element('cashflowsReviewSection').hidden = !reviewRows.length;
        renderTable(element('cashflowsReviewTable'), [
            { key: 'voucher_id', label: 'Voucher' }, { key: 'account_code', label: 'Account code' }, { key: 'account_name', label: 'Account' }, { key: 'fallback_category', label: 'Review category' },
        ], reviewRows, 'No review items.');
        element('cashflowsTransferCount').textContent = String(preview.internal_transfer_count || 0) + ' voucher(s)';
        element('cashflowsDeductionCount').textContent = String((preview.deduction_details || []).length) + ' line(s)';
        element('cashflowsReviewCount').textContent = String(preview.category_review_count || 0) + ' line(s)';
        element('cashflowsReceiptMode').textContent = 'Receipt presentation: ' + String(preview.receipt_presentation || 'net').toUpperCase();
        renderControls(result.controls || {});
        const source = result.source || {};
        element('cashflowsSourceStatus').textContent = source.ready ? ((source.quality || {}).rejected_row_count ? 'Source ready with warnings' : 'Source ready') : 'Source needs review';
        renderSourceQuality(source);
        setMessage((result.blocking_reasons || []).join(' '), false);
    }
    async function generateCashflows(event) {
        if (event) event.preventDefault();
        setMessage('Loading Cashflows summary from the configured Transaction Report template...', false);
        const start = element('cashflowsPeriodStart').value;
        const end = element('cashflowsPeriodEnd').value;
        const controller = new AbortController();
        const timeout = window.setTimeout(function () { controller.abort(); }, CASHFLOW_REQUEST_TIMEOUT_MS);
        try {
            const query = new URLSearchParams({ client_id: clientId, period_start: start, period_end: end });
            const result = await api('/api/reconciliation/cashflows/preview?' + query.toString(), { signal: controller.signal });
            renderResult(result);
        } catch (error) {
            setMessage(error.name === 'AbortError'
                ? 'Cashflows is taking longer than expected. Please try again or review the configured Transaction Report template.'
                : error.message, true);
        } finally {
            window.clearTimeout(timeout);
        }
    }
    function setReportView(isReport) {
        element('reportsView').hidden = isReport;
        element('cashflowsView').hidden = !isReport;
    }
    function setSelection(card) {
        if (card.dataset.reportKey !== 'cashflows') return;
        document.querySelectorAll('[data-report-key]').forEach(function (item) { item.classList.toggle('is-selected', item === card); });
        const params = new URLSearchParams(window.location.search);
        params.set('view', 'reports');
        params.set('report', 'cashflows');
        window.history.replaceState({}, '', window.location.pathname + '?' + params.toString());
        setReportView(true);
        setMessage('Select the date range and press Load summary to use the configured Transaction Report template.', false);
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
        const end = today.toISOString().slice(0, 10);
        const start = today.getFullYear() + '-01-01';
        element('cashflowsPeriodStart').value = start;
        element('cashflowsPeriodEnd').value = end;
    }
    async function initialise() {
        if (window.clientContextReady) await window.clientContextReady;
        clientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
        defaultPeriods();
        element('cashflowsReportForm').addEventListener('submit', generateCashflows);

        element('cashflowsBackButton').addEventListener('click', showReportsHub);
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