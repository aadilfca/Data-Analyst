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
    function resetLazyCashflowSections() {
        ['cashflowsTransfersSection', 'cashflowsDeductionsSection', 'cashflowsReviewSection'].forEach(function (id) {
            element(id).hidden = true;
        });
        ['cashflowsLoadTransfersButton', 'cashflowsLoadDeductionsButton', 'cashflowsLoadReviewButton'].forEach(function (id) {
            element(id).disabled = false;
        });
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
    function renderResult(result) {
        lastCashflowsResult = result;
        const preview = result.preview || {};
        const summaryRows = preview.category_totals || [];
        const months = preview.months || [];
        const columns = [{ key: 'category', label: 'Category' }].concat(months.map(function (month) { return { key: month, label: month, amount: true }; })).concat([{ key: 'Total', label: 'Total', amount: true }]);
        renderTable(element('cashflowsSummaryTable'), columns, summaryRows, 'No Cash Flow activity was found for this period.');
        element('cashflowsReceiptMode').textContent = 'Receipt presentation: ' + String(preview.receipt_presentation || 'net').toUpperCase();
        renderControls(result.controls || {});
        const source = result.source || {};
        element('cashflowsSourceStatus').textContent = source.ready ? ((source.quality || {}).rejected_row_count ? 'Source ready with warnings' : 'Source ready') : 'Source needs review';
        renderSourceQuality(source);
        setMessage((result.blocking_reasons || []).join(' '), false);

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
        if (!hasSourceTemplate) {
            transactionsSection.hidden = true;
            viewTransactionsButton.textContent = 'View classified transactions';
        }
    }
    async function generateCashflows(event) {
        if (event) event.preventDefault();
        setMessage('Loading Cashflows summary from the configured Transaction Report template...', false);
        const start = element('cashflowsPeriodStart').value;
        const end = element('cashflowsPeriodEnd').value;
        const controller = new AbortController();
        const timeout = window.setTimeout(function () { controller.abort(); }, CASHFLOW_REQUEST_TIMEOUT_MS);
        try {
            const query = new URLSearchParams({ client_id: clientId, period_start: start, period_end: end, details: 'summary' });
            const result = await api('/api/reconciliation/cashflows/preview?' + query.toString(), { signal: controller.signal });
            renderResult(result);
            if (!element('cashflowsTransactionsSection').hidden) await loadClassifiedTransactions(true);
        } catch (error) {
            setMessage(error.name === 'AbortError'
                ? 'Cashflows is taking longer than expected. Please try again or review the configured Transaction Report template.'
                : error.message, true);
        } finally {
            window.clearTimeout(timeout);
        }
    }
    function classifiedTransactionColumns(result) {
        const rawColumns = (result.template_columns || []).map(function (column) {
            return typeof column === 'string' ? column : column.column_name;
        }).filter(Boolean);
        return rawColumns.concat(['Type', 'Account', 'Assigned Category']);
    }
    function renderClassifiedTransactions(result) {
        const table = element('cashflowsTransactionsTable');
        const rows = result.rows || [];
        const rawColumns = (result.template_columns || []).map(function (column) {
            return typeof column === 'string' ? column : column.column_name;
        }).filter(Boolean);
        const columns = classifiedTransactionColumns(result);
        const totalPages = Math.max(1, Math.ceil(classifiedTransactionsTotal / classifiedTransactionsLimit));
        element('cashflowsTransactionsCount').textContent = String(classifiedTransactionsTotal) + ' line(s)';
        element('cashflowsTransactionsPageInfo').textContent = 'Page ' + String(classifiedTransactionsPage) + ' of ' + String(totalPages);
        element('cashflowsTransactionsPrevious').disabled = classifiedTransactionsPage <= 1;
        element('cashflowsTransactionsNext').disabled = classifiedTransactionsPage >= totalPages;
        if (!rows.length) {
            table.innerHTML = '<tbody><tr><td class="cashflows-empty" colspan="' + Math.max(columns.length + 1, 1) + '">No classified transactions found for the selected criteria.</td></tr></tbody>';
            return;
        }
        const headerCells = ['S.No'].concat(columns).map(function (label) { return '<th>' + escapeHtml(label) + '</th>'; }).join('');
        const bodyRows = rows.map(function (row, index) {
            const raw = row.row_data || {};
            const classification = row.classification || {};
            const account = [classification.account_code, classification.account_name].filter(Boolean).join(' — ') || '—';
            const assignedCategory = classification.assigned_category || (classification.classification_status === 'unmapped' ? 'Needs review' : '—');
            const values = rawColumns.map(function (column) { return raw[column] == null ? '' : raw[column]; }).concat([
                classification.type || 'Needs review', account, assignedCategory
            ]);
            const cells = values.map(function (value, valueIndex) {
                const className = valueIndex >= rawColumns.length ? ' class="cashflows-classification-cell"' : '';
                return '<td' + className + '>' + escapeHtml(value) + '</td>';
            }).join('');
            return '<tr><td>' + String((classifiedTransactionsPage - 1) * classifiedTransactionsLimit + index + 1) + '</td>' + cells + '</tr>';
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
        if (search) params.set('search', search);
        if (type) params.set('classification_type', type);
        if (account) params.set('account_code', account);
        if (category) params.set('category', category);
        try {
            const result = await api('/api/data/cashflows-classification-preview?' + params.toString(), {
                signal: classifiedTransactionsRequest.signal
            });
            classifiedTransactionsTotal = Number(result.total_rows || 0);
            renderClassifiedTransactions(result);
        } catch (error) {
            if (error.name === 'AbortError') return;
            table.innerHTML = '<tbody><tr><td class="cashflows-empty">Unable to load classified transactions.</td></tr></tbody>';
            setMessage(error.message, true);
        }
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
    async function loadCashflowChild(section) {
        const configuration = (lastCashflowsResult || {}).configuration || {};
        const start = element('cashflowsPeriodStart').value;
        const end = element('cashflowsPeriodEnd').value;
        if (!configuration.source_template_id || !clientId || !start || !end) return;
        const buttonIds = { transfers: 'cashflowsLoadTransfersButton', deductions: 'cashflowsLoadDeductionsButton', review: 'cashflowsLoadReviewButton' };
        const labels = { transfers: 'Load internal transfers', deductions: 'Load customer deductions', review: 'Load review required' };
        const button = element(buttonIds[section]);
        if (!button) return;
        button.disabled = true;
        button.textContent = 'Loading…';
        const params = new URLSearchParams({ client_id: String(clientId), period_start: start, period_end: end, details: section });
        try {
            const result = await api('/api/reconciliation/cashflows/preview?' + params.toString());
            renderCashflowChild(section, result.preview || {});
            button.textContent = labels[section] + ' (loaded)';
        } catch (error) {
            button.textContent = labels[section];
            setMessage(error.message, true);
        } finally {
            button.disabled = false;
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
        element('cashflowsViewTransactionsButton').addEventListener('click', toggleClassifiedTransactions);
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
