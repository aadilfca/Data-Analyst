/*
 * Phase 8 — Client-scoped report configuration interface.
 *
 * This screen deliberately uses exact account codes from the reconciliation API. Account titles
 * and Trial Balance type suggestions are review aids only; neither can change membership by itself.
 */
(function () {
    'use strict';

    const state = {
        clientId: null,
        accounts: [],
        groups: [],
        selectedCodes: new Set(),
        loadedGroupLabel: '',
        isBusy: false,
        reportPayload: null,
        setupTab: 'groups',
        categorySet: null,
        categoryDraft: [],
        categoryAccounts: [],
        categoryAssignments: new Map(),
        balanceAccounts: [],
        balanceExisting: [],
        balanceRows: [],
        balanceMonths: [],
    };

    const el = {};

    function cacheElements() {
        [
            'sourceForm', 'groupLabel', 'loadButton', 'savedGroupSelect', 'globalMessage',
            'workspace', 'sourceStatus', 'activeGroupHeading', 'totalAccounts', 'selectedAccounts',
            'suggestedAccounts', 'reviewAccounts', 'accountSearch', 'accountFilter', 'selectVisibleButton',
            'clearVisibleButton', 'accountTableBody', 'tableMessage', 'visibleCount', 'selectionHint',
            'saveButton',
            'reportsView', 'setupView', 'reportsViewButton', 'setupViewButton', 'reportForm', 'reportDataset',
            'reportGroup', 'reportPeriodStart', 'reportPeriodEnd', 'previewReportButton', 'downloadReportButton',
            'previewFinancialStatementsButton', 'financialStatementPreviewPanel', 'financialStatementStatus',
            'financialStatementMessage', 'financialStatementMeta', 'financialStatementUnmapped',
            'financialStatementPreviewEmpty', 'financialStatementPreviewHead', 'financialStatementPreviewBody',
            'reportStatus', 'reportMessage', 'reportControls', 'reportDiagnosticsPanel', 'reportDiagnostics',
            'reportBlockingReasons', 'reportPreviewPanel', 'reportPreviewMeta', 'reportPreviewEmpty',
            'reportPreviewHead', 'reportPreviewBody',
            'reportLedgerStatus', 'reportLedgerMessage', 'reportCategoryStatus', 'reportCategoryMessage',
            'reportTransferStatus', 'reportTransferMessage', 'reportCrossStatus', 'reportCrossMessage',
            'reportReconStatus', 'reportReconMessage',
            'setupGroupsPanel', 'setupCategoriesPanel', 'setupBalancesPanel', 'setupGroupsTab',
            'setupCategoriesTab', 'setupBalancesTab',
            'categoryStatus', 'categorySetCreate', 'categorySetNameInput', 'createCategorySetButton',
            'categoryWorkspace', 'categorySetName', 'categoryAssignmentSummary', 'categoryMessage',
            'categoryDefinitionsPanel', 'categoryDefinitionsEmpty', 'categoryTableBody', 'addCategoryButton',
            'saveCategoriesButton', 'categoryAssignmentsPanel', 'categoryAccountSearch', 'categoryAccountFilter',
            'categoryAssignmentsEmpty', 'categoryAssignmentsBody', 'saveCategoryAssignmentsButton',
            'categoryVisibleCount', 'categoryAssignmentHint',
            'balanceStatus', 'balanceForm', 'balanceGroup', 'balancePeriodStart', 'balancePeriodEnd',
            'loadBalancesButton', 'balanceMessage', 'balanceWorkspace', 'balanceTableBody',
            'saveBalancesButton', 'balanceGridCount',
        ].forEach((id) => { el[id] = document.getElementById(id); });
    }

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function normaliseText(value) {
        return String(value ?? '').trim();
    }

    function setMessage(message, kind) {
        el.globalMessage.hidden = !message;
        el.globalMessage.className = `reconciliation-message${kind ? ` is-${kind}` : ''}`;
        el.globalMessage.textContent = message || '';
    }

    function setTableMessage(message) {
        el.tableMessage.hidden = !message;
        el.tableMessage.textContent = message || '';
    }

    function setStatus(text, kind) {
        el.sourceStatus.textContent = text;
        el.sourceStatus.className = `reconciliation-status-pill is-${kind || 'muted'}`;
    }

    function setReportMessage(message, kind) {
        el.reportMessage.hidden = !message;
        el.reportMessage.className = `reconciliation-message${kind ? ` is-${kind}` : ''}`;
        el.reportMessage.textContent = message || '';
    }

    function setReportStatus(text, kind) {
        el.reportStatus.textContent = text;
        el.reportStatus.className = `reconciliation-status-pill is-${kind || 'muted'}`;
    }

    function setCategoryMessage(message, kind) {
        el.categoryMessage.hidden = !message;
        el.categoryMessage.className = `reconciliation-message${kind ? ` is-${kind}` : ''}`;
        el.categoryMessage.textContent = message || '';
    }

    function setCategoryStatus(text, kind) {
        el.categoryStatus.textContent = text;
        el.categoryStatus.className = `reconciliation-status-pill is-${kind || 'muted'}`;
    }

    function setBalanceMessage(message, kind) {
        el.balanceMessage.hidden = !message;
        el.balanceMessage.className = `reconciliation-message${kind ? ` is-${kind}` : ''}`;
        el.balanceMessage.textContent = message || '';
    }

    function setBalanceStatus(text, kind) {
        el.balanceStatus.textContent = text;
        el.balanceStatus.className = `reconciliation-status-pill is-${kind || 'muted'}`;
    }

    function setBusy(isBusy) {
        state.isBusy = isBusy;
        el.loadButton.disabled = isBusy;
        el.saveButton.disabled = isBusy || !state.accounts.length;
        el.selectVisibleButton.disabled = isBusy || !state.accounts.length;
        el.clearVisibleButton.disabled = isBusy || !state.accounts.length;
        el.loadButton.textContent = isBusy ? 'Loading…' : 'Load accounts';
        el.saveButton.textContent = isBusy ? 'Working…' : 'Save group';
    }

    async function apiFetch(url, options) {
        const response = await fetch(url, {
            credentials: 'include',
            ...options,
            headers: {
                Accept: 'application/json',
                ...(options && options.body ? { 'Content-Type': 'application/json' } : {}),
                ...((options && options.headers) || {}),
            },
        });
        let payload = null;
        try {
            payload = await response.json();
        } catch (error) {
            payload = null;
        }
        if (!response.ok) {
            if (response.status === 401 || response.status === 403) {
                throw new Error('Your session does not have permission to manage reconciliation groups. Please sign in as an administrator.');
            }
            const detail = payload && payload.detail;
            if (detail && typeof detail === 'object') {
                throw new Error(detail.message || detail.account_codes || detail.category_keys || 'The request could not be completed.');
            }
            throw new Error(typeof detail === 'string' ? detail : 'The request could not be completed.');
        }
        return payload || {};
    }

    function currentGroupLabel() {
        return normaliseText(el.groupLabel.value);
    }

    function accountMatches(account) {
        const query = normaliseText(el.accountSearch.value).toLowerCase();
        if (!query) return true;
        return [account.account_code, account.account_name, account.account_type, account.suggested_group]
            .map((value) => normaliseText(value).toLowerCase())
            .some((value) => value.includes(query));
    }

    function accountReviewNote(account) {
        const group = currentGroupLabel().toLowerCase();
        const type = normaliseText(account.account_type).toLowerCase();
        if (!state.selectedCodes.has(account.account_code)) return '';
        if ((group === 'bank' || group === 'cash') && type && type !== group) {
            return `Selected as ${group}; Trial Balance type is ${account.account_type}.`;
        }
        return '';
    }

    function visibleAccounts() {
        const filter = el.accountFilter.value;
        return state.accounts.filter((account) => {
            if (!accountMatches(account)) return false;
            const selected = state.selectedCodes.has(account.account_code);
            const suggested = account.suggested_group && account.suggested_group.toLowerCase() === currentGroupLabel().toLowerCase();
            const review = Boolean(accountReviewNote(account));
            if (filter === 'selected') return selected;
            if (filter === 'suggested') return suggested;
            if (filter === 'review') return review;
            return true;
        });
    }

    function renderMetrics() {
        const reviewCount = state.accounts.filter((account) => Boolean(accountReviewNote(account))).length;
        const suggestionCount = state.accounts.filter((account) => (
            account.suggested_group && account.suggested_group.toLowerCase() === currentGroupLabel().toLowerCase()
        )).length;
        el.totalAccounts.textContent = state.accounts.length;
        el.selectedAccounts.textContent = state.selectedCodes.size;
        el.suggestedAccounts.textContent = suggestionCount;
        el.reviewAccounts.textContent = reviewCount;
        el.activeGroupHeading.textContent = currentGroupLabel() || 'Reporting';
        el.selectionHint.textContent = state.selectedCodes.size
            ? `${state.selectedCodes.size} exact account code${state.selectedCodes.size === 1 ? '' : 's'} selected.`
            : 'Select accounts to include them in this group.';
    }

    function renderAccounts() {
        const accounts = visibleAccounts();
        setTableMessage(state.accounts.length && !accounts.length ? 'No accounts match the current search or filter.' : '');
        el.visibleCount.textContent = `${accounts.length} account${accounts.length === 1 ? '' : 's'} shown`;
        el.accountTableBody.innerHTML = accounts.map((account) => {
            const code = normaliseText(account.account_code);
            const selected = state.selectedCodes.has(code);
            const reviewNote = accountReviewNote(account);
            const suggestion = account.suggested_group && account.suggested_group.toLowerCase() === currentGroupLabel().toLowerCase();
            const typeClass = normaliseText(account.account_type).toLowerCase().replace(/[^a-z0-9]+/g, '-');
            return `
                <tr class="${selected ? 'is-selected' : ''} ${reviewNote ? 'has-review' : ''}">
                    <td class="reconciliation-check-column">
                        <input class="account-toggle" type="checkbox" data-account-code="${escapeHtml(code)}" ${selected ? 'checked' : ''} aria-label="Include ${escapeHtml(code)}">
                    </td>
                    <td><code class="reconciliation-account-code">${escapeHtml(code)}</code></td>
                    <td><span class="reconciliation-account-title">${escapeHtml(account.account_name || '—')}</span></td>
                    <td><span class="reconciliation-type-chip ${typeClass ? `is-${typeClass}` : ''}">${escapeHtml(account.account_type || 'Unclassified')}</span></td>
                    <td>${suggestion ? '<span class="reconciliation-suggestion">Suggested match</span>' : '<span class="reconciliation-muted">—</span>'}</td>
                    <td>${reviewNote ? `<span class="reconciliation-review-note">${escapeHtml(reviewNote)}</span>` : '<span class="reconciliation-muted">—</span>'}</td>
                </tr>`;
        }).join('');
        renderMetrics();
    }

    function populateSavedGroups() {
        const current = currentGroupLabel();
        el.savedGroupSelect.innerHTML = '<option value="">Saved groups</option>' + state.groups.map((group) => (
            `<option value="${escapeHtml(group.group_label)}">${escapeHtml(group.group_label)} · ${group.account_codes.length} codes</option>`
        )).join('');
        if (state.groups.some((group) => group.group_label.toLowerCase() === current.toLowerCase())) {
            el.savedGroupSelect.value = state.groups.find((group) => group.group_label.toLowerCase() === current.toLowerCase()).group_label;
        }
    }

    async function loadGroups() {
        const payload = await apiFetch(`/api/reconciliation/groups?client_id=${encodeURIComponent(state.clientId)}`);
        state.groups = Array.isArray(payload.groups) ? payload.groups : [];
        populateSavedGroups();
        populateReportGroups();
        populateBalanceGroups();
    }

    function populateReportGroups() {
        if (!el.reportGroup) return;
        const current = el.reportGroup.value;
        el.reportGroup.innerHTML = '<option value="">Select a saved group</option>' + state.groups.map((group) => (
            `<option value="${escapeHtml(group.group_label)}">${escapeHtml(group.group_label)} · ${group.account_codes.length} codes</option>`
        )).join('');
        if (state.groups.some((group) => group.group_label === current)) el.reportGroup.value = current;
        if (!el.reportGroup.value && state.groups.length) {
            const preferred = state.groups.find((group) => ['bank', 'cash'].includes(group.group_label.toLowerCase()));
            el.reportGroup.value = (preferred || state.groups[0]).group_label;
        }
    }

    function populateBalanceGroups() {
        const current = el.balanceGroup.value;
        el.balanceGroup.innerHTML = '<option value="">Select a saved group</option>' + state.groups.map((group) => (
            `<option value="${escapeHtml(group.group_label)}">${escapeHtml(group.group_label)} · ${group.account_codes.length} codes</option>`
        )).join('');
        if (state.groups.some((group) => group.group_label === current)) el.balanceGroup.value = current;
        if (!el.balanceGroup.value && state.groups.length) {
            const preferred = state.groups.find((group) => ['bank', 'cash'].includes(group.group_label.toLowerCase()));
            el.balanceGroup.value = (preferred || state.groups[0]).group_label;
        }
    }

    async function loadReportDatasets() {
        const datasets = await apiFetch(`/api/data/datasets?client_id=${encodeURIComponent(state.clientId)}`);
        const usable = (Array.isArray(datasets) ? datasets : []).filter((item) => (
            item.status !== 'Deleted' && Number(item.record_count || 0) > 0
        ));
        el.reportDataset.innerHTML = '<option value="">Select an imported dataset</option>' + usable.map((item) => (
            `<option value="${escapeHtml(item.upload_id)}">${escapeHtml(item.file_name || item.template_name || `Upload ${item.upload_id}`)} · ${Number(item.record_count || 0).toLocaleString()} rows</option>`
        )).join('');
        if (usable.length) el.reportDataset.value = String(usable[0].upload_id);
        if (!usable.length) setReportMessage('No imported Client Data Bank dataset is available for this client.', 'error');
    }

    function showSetupTab(tab, loadData = true) {
        const selected = ['groups', 'categories', 'balances'].includes(tab) ? tab : 'groups';
        state.setupTab = selected;
        el.setupGroupsPanel.hidden = selected !== 'groups';
        el.setupCategoriesPanel.hidden = selected !== 'categories';
        el.setupBalancesPanel.hidden = selected !== 'balances';
        [['groups', el.setupGroupsTab], ['categories', el.setupCategoriesTab], ['balances', el.setupBalancesTab]].forEach(([key, button]) => {
            const active = key === selected;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-selected', String(active));
        });
        const params = new URLSearchParams(window.location.search);
        params.set('view', 'setup');
        params.set('setup_tab', selected);
        window.history.replaceState({}, '', `${window.location.pathname}?${params.toString()}`);
        if (loadData && state.clientId) {
            if (selected === 'categories') loadCategoryConfiguration();
            if (selected === 'balances') loadBalanceSetup();
        }
    }

    function showView(view) {
        const reports = view !== 'setup';
        el.reportsView.hidden = !reports;
        el.setupView.hidden = reports;
        el.reportsViewButton.classList.toggle('is-active', reports);
        el.setupViewButton.classList.toggle('is-active', !reports);
        el.reportsViewButton.setAttribute('aria-selected', String(reports));
        el.setupViewButton.setAttribute('aria-selected', String(!reports));
        if (!reports) {
            const requested = new URLSearchParams(window.location.search).get('setup_tab') || state.setupTab;
            showSetupTab(requested);
            return;
        }
        const params = new URLSearchParams(window.location.search);
        params.set('view', 'reports');
        params.delete('setup_tab');
        window.history.replaceState({}, '', `${window.location.pathname}?${params.toString()}`);
    }

    function reportQueryString() {
        const params = new URLSearchParams({
            client_id: String(state.clientId),
            group_label: el.reportGroup.value,
            period_start: el.reportPeriodStart.value,
            period_end: el.reportPeriodEnd.value,
            journal_upload_id: el.reportDataset.value,
        });
        return params.toString();
    }

    function setControlStatus(prefix, control) {
        const status = control || { status: 'Blocked', message: 'Unavailable.' };
        el[`report${prefix}Status`].textContent = status.status || 'Blocked';
        el[`report${prefix}Message`].textContent = status.message || '';
        el[`report${prefix}Status`].className = `reconciliation-control-value is-${String(status.status || 'blocked').toLowerCase().replace(/[^a-z]+/g, '-')}`;
    }

    function formatReportNumber(value) {
        const number = Number(value);
        return Number.isFinite(number) ? number.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—';
    }

    function renderReportPreview(payload) {
        state.reportPayload = payload;
        const controls = payload.controls || {};
        setControlStatus('Ledger', controls.ledger_balance);
        setControlStatus('Category', controls.categories);
        setControlStatus('Transfer', controls.transfers);
        setControlStatus('Cross', controls.cross_group);
        setControlStatus('Recon', controls.reconciliation);
        el.reportControls.hidden = false;
        el.reportDiagnosticsPanel.hidden = false;
        el.reportPreviewPanel.hidden = false;

        const diagnostics = (payload.source && payload.source.diagnostics) || [];
        el.reportDiagnostics.innerHTML = diagnostics.length
            ? diagnostics.map((item) => `<li class="is-${escapeHtml(item.severity || 'warning')}"><strong>${escapeHtml(item.code || 'Review')}</strong> ${escapeHtml(item.message || '')}</li>`).join('')
            : '<li class="is-success">Source and accounting inputs passed the diagnostic checks.</li>';
        const blocking = payload.blocking_reasons || [];
        el.reportBlockingReasons.hidden = !blocking.length;
        el.reportBlockingReasons.innerHTML = blocking.length
            ? `<strong>Download blocked until resolved:</strong><ul>${blocking.map((reason) => `<li>${escapeHtml(reason)}</li>`).join('')}</ul>`
            : '';

        const preview = payload.preview || {};
        const months = preview.months || [];
        const rows = preview.category_totals || [];
        el.reportPreviewMeta.textContent = `${preview.selected_voucher_count || 0} vouchers · ${preview.internal_transfer_count || 0} internal transfers`;
        el.reportPreviewHead.innerHTML = `<tr><th>Category</th>${months.map((month) => `<th>${escapeHtml(month)}</th>`).join('')}<th>Total</th></tr>`;
        el.reportPreviewBody.innerHTML = rows.map((row) => `<tr><td><strong>${escapeHtml(row.category || '')}</strong></td>${months.map((month) => `<td>${formatReportNumber(row[month])}</td>`).join('')}<td>${formatReportNumber(row.Total)}</td></tr>`).join('');
        el.reportPreviewEmpty.hidden = Boolean(rows.length);
        el.reportPreviewEmpty.textContent = rows.length ? '' : 'No category movements were found for the selected period.';
        el.downloadReportButton.disabled = !payload.can_download;
        setReportStatus(payload.can_download ? 'Ready to download' : 'Review required', payload.can_download ? 'ready' : 'error');
        setReportMessage(
            payload.can_download ? 'All controls passed. The professional Excel report is ready.' : 'Preview is available, but Excel download is blocked until the listed controls are resolved.',
            payload.can_download ? 'success' : 'info'
        );
    }

    function financialStatementQueryString() {
        const params = new URLSearchParams({
            client_id: String(state.clientId),
            period_start: el.reportPeriodStart.value,
            period_end: el.reportPeriodEnd.value,
            journal_upload_id: el.reportDataset.value,
        });
        return params.toString();
    }

    function setFinancialStatementStatus(text, kind) {
        el.financialStatementStatus.textContent = text;
        el.financialStatementStatus.className = "reconciliation-status-pill is-" + (kind || "muted");
    }

    function setFinancialStatementMessage(message, kind) {
        el.financialStatementMessage.hidden = !message;
        el.financialStatementMessage.className = "reconciliation-message" + (kind ? " is-" + kind : "");
        el.financialStatementMessage.textContent = message || "";
    }

    function renderFinancialStatementPreview(payload) {
        const preview = payload.preview || {};
        const control = (payload.controls || {}).financial_statement_mapping || {};
        const months = preview.months || [];
        const rows = preview.category_totals || [];
        const accountCounts = new Map((preview.account_counts || []).map((item) => [item.category, item.account_count]));
        el.financialStatementPreviewPanel.hidden = false;
        el.financialStatementMeta.textContent = (preview.selected_voucher_count || 0) + " vouchers Â· " + (preview.mapped_account_count || 0) + " mapped account codes Â· " + (preview.unmapped_account_count || 0) + " unmapped";
        el.financialStatementPreviewHead.innerHTML = "<tr><th>Category</th><th>Accounts</th>" + months.map((month) => "<th>" + escapeHtml(month) + "</th>").join("") + "<th>Total</th></tr>";
        el.financialStatementPreviewBody.innerHTML = rows.map((row) => "<tr><td><strong>" + escapeHtml(row.category || "") + "</strong></td><td>" + (accountCounts.get(row.category) || 0) + "</td>" + months.map((month) => "<td>" + formatReportNumber(row[month]) + "</td>").join("") + "<td>" + formatReportNumber(row.Total) + "</td></tr>").join("");
        el.financialStatementPreviewEmpty.hidden = Boolean(rows.length);
        el.financialStatementPreviewEmpty.textContent = rows.length ? "" : "No Financial Statement category movements were found for the selected period.";
        const unmapped = preview.unmapped_accounts || [];
        el.financialStatementUnmapped.hidden = !unmapped.length;
        el.financialStatementUnmapped.innerHTML = unmapped.length
            ? "<strong>Review required:</strong> " + escapeHtml(control.message || "Some account codes are not mapped.") + "<ul>" + unmapped.map((item) => "<li><code>" + escapeHtml(item.account_code || "") + "</code> " + escapeHtml(item.account_name || "") + " Â· net " + formatReportNumber(item.net_movement) + "</li>").join("") + "</ul>"
            : "";
        const blocked = (payload.blocking_reasons || []).length;
        setFinancialStatementStatus(blocked ? "Blocked" : (unmapped.length ? "Review required" : "Ready"), blocked ? "error" : (unmapped.length ? "muted" : "ready"));
        setFinancialStatementMessage(
            blocked ? "The Financial Statement preview could not be calculated." : (unmapped.length ? "Preview loaded. Assign the listed accounts in Account Control Centre for complete coverage." : "Financial Statement preview loaded from central Account Control mappings."),
            blocked ? "error" : (unmapped.length ? "info" : "success")
        );
    }

    async function previewFinancialStatements() {
        if (!el.reportDataset.value || !el.reportPeriodStart.value || !el.reportPeriodEnd.value) {
            setFinancialStatementStatus("Inputs required", "error");
            setFinancialStatementMessage("Select a dataset and complete period before previewing Financial Statements.", "error");
            return;
        }
        el.previewFinancialStatementsButton.disabled = true;
        setFinancialStatementStatus("LoadingÂ€¦", "loading");
        setFinancialStatementMessage("", "");
        try {
            const payload = await apiFetch("/api/reconciliation/financial-statements/preview?" + financialStatementQueryString());
            renderFinancialStatementPreview(payload);
        } catch (error) {
            setFinancialStatementStatus("Preview failed", "error");
            setFinancialStatementMessage(error.message || "The Financial Statement preview could not be generated.", "error");
        } finally {
            el.previewFinancialStatementsButton.disabled = false;
        }
    }
    async function previewReport(event) {
        if (event) event.preventDefault();
        if (!el.reportDataset.value || !el.reportGroup.value || !el.reportPeriodStart.value || !el.reportPeriodEnd.value) {
            setReportMessage('Select a dataset, reporting group, and complete period before previewing.', 'error');
            return;
        }
        el.previewReportButton.disabled = true;
        el.downloadReportButton.disabled = true;
        setReportStatus('Checking controls…', 'loading');
        try {
            const payload = await apiFetch(`/api/reconciliation/reports/preview?${reportQueryString()}`);
            renderReportPreview(payload);
        } catch (error) {
            setReportStatus('Preview failed', 'error');
            setReportMessage(error.message || 'The report preview could not be generated.', 'error');
        } finally {
            el.previewReportButton.disabled = false;
        }
    }

    function downloadReport() {
        if (!state.reportPayload || !state.reportPayload.can_download) return;
        window.location.href = `/api/reconciliation/reports/download?${reportQueryString()}`;
    }

    async function loadReportsWorkspace() {
        setReportStatus('Loading inputs…', 'loading');
        try {
            await loadGroups();
            await loadReportDatasets();
            setReportStatus('Inputs ready', 'ready');
        } catch (error) {
            setReportStatus('Needs attention', 'error');
            setReportMessage(error.message || 'Report inputs could not be loaded.', 'error');
        }
    }

    async function loadAccounts() {
        const label = currentGroupLabel();
        if (!label) throw new Error('Enter a reporting group before loading accounts.');
        const payload = await apiFetch(
            `/api/reconciliation/account-suggestions?client_id=${encodeURIComponent(state.clientId)}&group_label=${encodeURIComponent(label)}`
        );
        state.accounts = Array.isArray(payload.accounts) ? payload.accounts : [];
        state.loadedGroupLabel = label;
        state.selectedCodes = new Set(
            state.accounts.filter((account) => account.is_selected).map((account) => account.account_code)
        );
        el.workspace.hidden = false;
        setTableMessage('');
        renderAccounts();
        setStatus(`${state.accounts.length} accounts ready`, 'ready');
    }

    async function loadWorkspace(event) {
        if (event) event.preventDefault();
        if (!state.clientId) {
            setMessage('Select a client before configuring a reporting group.', 'error');
            return;
        }
        setMessage('', '');
        setBusy(true);
        setStatus('Loading…', 'loading');
        try {
            await loadGroups();
            await loadAccounts();
            setMessage('Accounts loaded. Review the suggested classifications, then save the exact code selection.', 'success');
        } catch (error) {
            state.accounts = [];
            state.selectedCodes = new Set();
            el.workspace.hidden = true;
            setStatus('Needs attention', 'error');
            setMessage(error.message || 'The accounts could not be loaded.', 'error');
        } finally {
            setBusy(false);
        }
    }

    function toggleAccount(code, checked) {
        if (checked) state.selectedCodes.add(code);
        else state.selectedCodes.delete(code);
        renderAccounts();
    }

    function setVisibleSelection(checked) {
        visibleAccounts().forEach((account) => {
            if (checked) state.selectedCodes.add(account.account_code);
            else state.selectedCodes.delete(account.account_code);
        });
        renderAccounts();
    }

    async function saveGroup() {
        const groupLabel = currentGroupLabel();
        if (!state.clientId || !groupLabel) {
            setMessage('Load a reporting group before saving.', 'error');
            return;
        }
        if (groupLabel.toLowerCase() !== state.loadedGroupLabel.toLowerCase()) {
            setMessage('The reporting group changed. Click Load accounts before saving so the selection belongs to the correct group.', 'error');
            return;
        }
        setBusy(true);
        setMessage('', '');
        try {
            const payload = await apiFetch(`/api/reconciliation/groups/${encodeURIComponent(state.clientId)}`, {
                method: 'PUT',
                body: JSON.stringify({
                    group_label: groupLabel,
                    account_codes: Array.from(state.selectedCodes),
                }),
            });
            state.loadedGroupLabel = payload.group_label || groupLabel;
            await loadGroups();
            setStatus(`${state.selectedCodes.size} codes saved`, 'saved');
            setMessage(`Saved ${state.selectedCodes.size} exact account code${state.selectedCodes.size === 1 ? '' : 's'} for ${state.loadedGroupLabel}.`, 'success');
        } catch (error) {
            setStatus('Save failed', 'error');
            setMessage(error.message || 'The group could not be saved.', 'error');
        } finally {
            setBusy(false);
        }
    }

    function categoryKey(value) {
        return normaliseText(value).toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
    }

    function categoryDraftFromDom() {
        return Array.from(el.categoryTableBody.querySelectorAll('tr')).map((row, index) => {
            const label = normaliseText(row.querySelector('[data-category-label]').value);
            return {
                category_key: categoryKey(label),
                label,
                parent_category_key: row.querySelector('[data-category-parent]').value || null,
                sort_order: Number(row.querySelector('[data-category-order]').value || index),
            };
        });
    }

    function setCategoryWorkspaceVisible(isVisible) {
        el.categorySetCreate.hidden = isVisible;
        el.categoryWorkspace.hidden = !isVisible;
        el.categoryDefinitionsPanel.hidden = !isVisible;
        el.categoryAssignmentsPanel.hidden = !isVisible;
    }

    function renderCategoryDefinitions() {
        const categories = state.categoryDraft;
        const parentOptions = categories.map((category) => (
            `<option value="${escapeHtml(category.category_key)}">${escapeHtml(category.label || 'Unnamed category')}</option>`
        )).join('');
        el.categoryDefinitionsEmpty.hidden = Boolean(categories.length);
        el.categoryTableBody.innerHTML = categories.map((category, index) => `
            <tr>
                <td><input class="reconciliation-cell-input" data-category-label type="text" maxlength="120" value="${escapeHtml(category.label)}" placeholder="e.g. Operating expenses"></td>
                <td><select class="reconciliation-select reconciliation-cell-select" data-category-parent><option value="">No parent category</option>${parentOptions}</select></td>
                <td><input class="reconciliation-cell-input reconciliation-order-input" data-category-order type="number" min="0" step="1" value="${Number.isFinite(Number(category.sort_order)) ? Number(category.sort_order) : index}"></td>
                <td class="reconciliation-action-cell"><button class="reconciliation-icon-button" type="button" data-category-remove="${index}" aria-label="Remove ${escapeHtml(category.label || 'category')}">×</button></td>
            </tr>
        `).join('');
        categories.forEach((category, index) => {
            const select = el.categoryTableBody.querySelectorAll('[data-category-parent]')[index];
            if (select) select.value = category.parent_category_key || '';
        });
    }

    function renderCategoryAssignments() {
        const search = normaliseText(el.categoryAccountSearch.value).toLowerCase();
        const filter = el.categoryAccountFilter.value;
        const categories = state.categoryDraft.filter((category) => category.category_key && category.label);
        const visible = state.categoryAccounts.filter((account) => {
            const assigned = state.categoryAssignments.has(account.account_code);
            const haystack = `${account.account_code || ''} ${account.account_name || ''} ${account.account_type || ''}`.toLowerCase();
            return (!search || haystack.includes(search))
                && (filter === 'all' || (filter === 'assigned' ? assigned : !assigned));
        });
        const assignedCount = state.categoryAssignments.size;
        el.categoryAssignmentSummary.textContent = `${assignedCount} account code${assignedCount === 1 ? '' : 's'} assigned`;
        el.categoryVisibleCount.textContent = `${visible.length} account${visible.length === 1 ? '' : 's'} shown`;
        el.categoryAssignmentHint.textContent = `${assignedCount} of ${state.categoryAccounts.length} Trial Balance account codes assigned.`;
        el.categoryAssignmentsEmpty.hidden = Boolean(categories.length && visible.length);
        el.categoryAssignmentsEmpty.textContent = !categories.length
            ? 'Save at least one category before assigning Trial Balance account codes.'
            : (visible.length ? '' : 'No Trial Balance accounts match the current search or filter.');
        el.categoryAssignmentsBody.innerHTML = visible.map((account) => {
            const selected = state.categoryAssignments.get(account.account_code) || '';
            const options = '<option value="">Not assigned</option>' + categories.map((category) => (
                `<option value="${escapeHtml(category.category_key)}"${selected === category.category_key ? ' selected' : ''}>${escapeHtml(category.label)}</option>`
            )).join('');
            const type = normaliseText(account.account_type) || 'Unclassified';
            return `<tr><td><code class="reconciliation-account-code">${escapeHtml(account.account_code)}</code></td><td><span class="reconciliation-account-title">${escapeHtml(account.account_name || '—')}</span></td><td><span class="reconciliation-type-chip">${escapeHtml(type)}</span></td><td><select class="reconciliation-select reconciliation-cell-select" data-category-account="${escapeHtml(account.account_code)}">${options}</select></td></tr>`;
        }).join('');
    }

    async function loadCategoryConfiguration() {
        if (!state.clientId) return;
        setCategoryStatus('Loading…', 'loading');
        try {
            const setPayload = await apiFetch(`/api/reconciliation/category-sets?client_id=${encodeURIComponent(state.clientId)}`);
            const categorySet = (setPayload.category_sets || []).find((item) => item.purpose_key === 'ledger_summary');
            state.categorySet = categorySet || null;
            if (!categorySet) {
                state.categoryDraft = [];
                state.categoryAccounts = [];
                state.categoryAssignments = new Map();
                setCategoryWorkspaceVisible(false);
                setCategoryStatus('Not configured', 'muted');
                setCategoryMessage('', '');
                return;
            }
            const [categoriesPayload, assignmentsPayload, accountsPayload] = await Promise.all([
                apiFetch(`/api/reconciliation/category-sets/${encodeURIComponent(categorySet.id)}/categories`),
                apiFetch(`/api/reconciliation/category-sets/${encodeURIComponent(categorySet.id)}/assignments`),
                apiFetch(`/api/reconciliation/account-suggestions?client_id=${encodeURIComponent(state.clientId)}`),
            ]);
            state.categoryDraft = (categoriesPayload.categories || []).filter((item) => item.is_active).map((item) => ({
                category_key: item.category_key,
                label: item.label,
                parent_category_key: item.parent_category_key || null,
                sort_order: item.sort_order,
            }));
            state.categoryAccounts = Array.isArray(accountsPayload.accounts) ? accountsPayload.accounts : [];
            state.categoryAssignments = new Map((assignmentsPayload.assignments || []).map((item) => [item.account_code, item.category_key]));
            el.categorySetName.textContent = categorySet.set_name;
            setCategoryWorkspaceVisible(true);
            renderCategoryDefinitions();
            renderCategoryAssignments();
            setCategoryStatus(`${state.categoryDraft.length} categories ready`, 'ready');
        } catch (error) {
            setCategoryWorkspaceVisible(false);
            setCategoryStatus('Needs attention', 'error');
            setCategoryMessage(error.message || 'Ledger Summary categories could not be loaded.', 'error');
        }
    }

    async function createCategorySet() {
        const setName = normaliseText(el.categorySetNameInput.value);
        if (!setName) {
            setCategoryMessage('Enter a name for the Ledger Summary category set.', 'error');
            return;
        }
        el.createCategorySetButton.disabled = true;
        setCategoryMessage('', '');
        try {
            await apiFetch(`/api/reconciliation/category-sets?client_id=${encodeURIComponent(state.clientId)}`, {
                method: 'POST',
                body: JSON.stringify({ purpose_key: 'ledger_summary', set_name: setName }),
            });
            await loadCategoryConfiguration();
            setCategoryMessage('Ledger Summary categories are ready. Add category labels, then save them before mapping account codes.', 'success');
        } catch (error) {
            setCategoryStatus('Create failed', 'error');
            setCategoryMessage(error.message || 'The Ledger Summary category set could not be created.', 'error');
        } finally {
            el.createCategorySetButton.disabled = false;
        }
    }

    function addCategory() {
        state.categoryDraft = categoryDraftFromDom();
        state.categoryDraft.push({ category_key: '', label: '', parent_category_key: null, sort_order: state.categoryDraft.length });
        renderCategoryDefinitions();
        const input = el.categoryTableBody.querySelector('tr:last-child [data-category-label]');
        if (input) input.focus();
    }

    function removeCategory(index) {
        state.categoryDraft = categoryDraftFromDom();
        state.categoryDraft.splice(index, 1);
        state.categoryDraft = state.categoryDraft.map((item, order) => ({ ...item, sort_order: order }));
        renderCategoryDefinitions();
    }

    async function saveCategories() {
        if (!state.categorySet) return;
        const categories = categoryDraftFromDom();
        if (!categories.length || categories.some((category) => !category.label || !category.category_key)) {
            setCategoryMessage('Add a non-blank category label before saving.', 'error');
            return;
        }
        const keys = categories.map((category) => category.category_key);
        if (new Set(keys).size !== keys.length) {
            setCategoryMessage('Category labels must be distinct after punctuation and spacing are ignored.', 'error');
            return;
        }
        el.saveCategoriesButton.disabled = true;
        try {
            await apiFetch(`/api/reconciliation/category-sets/${encodeURIComponent(state.categorySet.id)}/categories`, {
                method: 'PUT', body: JSON.stringify({ categories }),
            });
            await loadCategoryConfiguration();
            setCategoryMessage('Ledger Summary categories saved. Now assign the relevant exact Trial Balance codes.', 'success');
        } catch (error) {
            setCategoryStatus('Save failed', 'error');
            setCategoryMessage(error.message || 'Categories could not be saved.', 'error');
        } finally {
            el.saveCategoriesButton.disabled = false;
        }
    }

    async function saveCategoryAssignments() {
        if (!state.categorySet || !state.categoryDraft.length) {
            setCategoryMessage('Save at least one Ledger Summary category before assigning account codes.', 'error');
            return;
        }
        const assignments = Array.from(state.categoryAssignments.entries())
            .filter(([, categoryKeyValue]) => categoryKeyValue)
            .map(([account_code, category_key]) => ({ account_code, category_key }));
        el.saveCategoryAssignmentsButton.disabled = true;
        try {
            await apiFetch(`/api/reconciliation/category-sets/${encodeURIComponent(state.categorySet.id)}/assignments`, {
                method: 'PUT', body: JSON.stringify({ assignments }),
            });
            setCategoryStatus(`${assignments.length} codes assigned`, 'saved');
            setCategoryMessage('Exact Trial Balance code assignments saved for this client.', 'success');
        } catch (error) {
            setCategoryStatus('Save failed', 'error');
            setCategoryMessage(error.message || 'Account category assignments could not be saved.', 'error');
        } finally {
            el.saveCategoryAssignmentsButton.disabled = false;
        }
    }

    function monthsBetween(startMonth, endMonth) {
        if (!/^\d{4}-\d{2}$/.test(startMonth) || !/^\d{4}-\d{2}$/.test(endMonth) || startMonth > endMonth) return [];
        const months = [];
        const cursor = new Date(`${startMonth}-01T00:00:00Z`);
        const end = new Date(`${endMonth}-01T00:00:00Z`);
        while (cursor <= end) {
            months.push(`${cursor.getUTCFullYear()}-${String(cursor.getUTCMonth() + 1).padStart(2, '0')}`);
            cursor.setUTCMonth(cursor.getUTCMonth() + 1);
        }
        return months;
    }

    function balanceInputValue(value) {
        return Number.isFinite(Number(value)) && normaliseText(value) !== '' ? String(value) : '';
    }

    function balanceRecordKey(accountCode, month) {
        return `${accountCode}|${month}`;
    }

    function formatBalanceMonth(month) {
        return new Intl.DateTimeFormat(undefined, { month: 'short', year: 'numeric', timeZone: 'UTC' }).format(new Date(`${month}-01T00:00:00Z`));
    }

    function renderBalanceRows() {
        el.balanceTableBody.innerHTML = state.balanceRows.map((row, index) => `
            <tr><td><code class="reconciliation-account-code">${escapeHtml(row.account_code)}</code></td><td><span class="reconciliation-account-title">${escapeHtml(row.account_name || '—')}</span></td><td>${escapeHtml(formatBalanceMonth(row.period_month))}</td><td><input class="reconciliation-cell-input reconciliation-balance-input" data-balance-index="${index}" data-balance-field="opening_balance" type="number" step="0.01" value="${escapeHtml(balanceInputValue(row.opening_balance))}" ${row.is_first_month ? '' : 'readonly'}></td><td><input class="reconciliation-cell-input reconciliation-balance-input" data-balance-index="${index}" data-balance-field="actual_closing_balance" type="number" step="0.01" value="${escapeHtml(balanceInputValue(row.actual_closing_balance))}"></td></tr>
        `).join('');
        el.balanceGridCount.textContent = `${state.balanceRows.length} balance row${state.balanceRows.length === 1 ? '' : 's'}`;
    }

    async function loadBalanceSetup() {
        if (!state.clientId) return;
        setBalanceStatus('Loading groups…', 'loading');
        try {
            await loadGroups();
            setBalanceStatus(state.groups.length ? 'Inputs ready' : 'Group required', state.groups.length ? 'ready' : 'error');
            if (!state.groups.length) setBalanceMessage('Save a reporting group before entering reconciliation balances.', 'info');
        } catch (error) {
            setBalanceStatus('Needs attention', 'error');
            setBalanceMessage(error.message || 'Saved reporting groups could not be loaded.', 'error');
        }
    }

    async function loadBalanceGrid(event) {
        if (event) event.preventDefault();
        const groupLabel = el.balanceGroup.value;
        const months = monthsBetween(el.balancePeriodStart.value, el.balancePeriodEnd.value);
        if (!groupLabel || !months.length) {
            setBalanceMessage('Select a saved group and a valid first/last month.', 'error');
            return;
        }
        el.loadBalancesButton.disabled = true;
        setBalanceStatus('Loading balance grid…', 'loading');
        try {
            const [accountsPayload, balancesPayload] = await Promise.all([
                apiFetch(`/api/reconciliation/account-suggestions?client_id=${encodeURIComponent(state.clientId)}&group_label=${encodeURIComponent(groupLabel)}`),
                apiFetch(`/api/reconciliation/balances?client_id=${encodeURIComponent(state.clientId)}&group_label=${encodeURIComponent(groupLabel)}`),
            ]);
            state.balanceAccounts = (accountsPayload.accounts || []).filter((account) => account.is_selected);
            if (!state.balanceAccounts.length) throw new Error('The selected reporting group has no saved account codes.');
            state.balanceExisting = balancesPayload.balances || [];
            state.balanceMonths = months;
            const existing = new Map(state.balanceExisting.map((row) => [balanceRecordKey(row.account_code, row.period_month), row]));
            state.balanceRows = state.balanceAccounts.flatMap((account) => {
                let priorClosing = null;
                return months.map((month, index) => {
                    const stored = existing.get(balanceRecordKey(account.account_code, month));
                    const opening = index === 0
                        ? balanceInputValue(stored && stored.opening_balance)
                        : (priorClosing !== null ? priorClosing : balanceInputValue(stored && stored.opening_balance));
                    const closing = balanceInputValue(stored && stored.actual_closing_balance);
                    priorClosing = closing;
                    return { account_code: account.account_code, account_name: account.account_name, period_month: month, opening_balance: opening, actual_closing_balance: closing, is_first_month: index === 0 };
                });
            });
            el.balanceWorkspace.hidden = false;
            renderBalanceRows();
            setBalanceStatus(`${state.balanceRows.length} rows ready`, 'ready');
            setBalanceMessage('', '');
        } catch (error) {
            el.balanceWorkspace.hidden = true;
            setBalanceStatus('Needs attention', 'error');
            setBalanceMessage(error.message || 'The balance grid could not be loaded.', 'error');
        } finally {
            el.loadBalancesButton.disabled = false;
        }
    }

    function updateBalanceRow(index, field, value) {
        const row = state.balanceRows[index];
        if (!row) return;
        row[field] = value;
        if (field === 'actual_closing_balance') {
            for (let cursor = index + 1; cursor < state.balanceRows.length; cursor += 1) {
                const next = state.balanceRows[cursor];
                const previous = state.balanceRows[cursor - 1];
                if (next.account_code !== row.account_code) break;
                next.opening_balance = previous.actual_closing_balance;
            }
            renderBalanceRows();
        }
    }

    async function saveBalances() {
        const groupLabel = el.balanceGroup.value;
        const invalid = state.balanceRows.find((row) => !Number.isFinite(Number(row.opening_balance)) || !Number.isFinite(Number(row.actual_closing_balance)) || normaliseText(row.opening_balance) === '' || normaliseText(row.actual_closing_balance) === '');
        if (!groupLabel || !state.balanceRows.length || invalid) {
            setBalanceMessage('Enter a numeric opening and actual closing balance for every displayed account-month. Zero is valid.', 'error');
            return;
        }
        const activeCodes = new Set(state.balanceAccounts.map((account) => account.account_code));
        const gridKeys = new Set(state.balanceRows.map((row) => balanceRecordKey(row.account_code, row.period_month)));
        const preserved = state.balanceExisting.filter((row) => activeCodes.has(row.account_code) && !gridKeys.has(balanceRecordKey(row.account_code, row.period_month)));
        const balances = preserved.concat(state.balanceRows).map((row) => ({
            account_code: row.account_code,
            period_month: row.period_month,
            opening_balance: Number(row.opening_balance),
            actual_closing_balance: Number(row.actual_closing_balance),
        }));
        el.saveBalancesButton.disabled = true;
        try {
            await apiFetch(`/api/reconciliation/balances/${encodeURIComponent(state.clientId)}`, {
                method: 'PUT', body: JSON.stringify({ group_label: groupLabel, balances }),
            });
            state.balanceExisting = balances;
            setBalanceStatus(`${state.balanceRows.length} rows saved`, 'saved');
            setBalanceMessage('Reconciliation balances saved. Months outside this grid were retained for active group accounts.', 'success');
        } catch (error) {
            setBalanceStatus('Save failed', 'error');
            setBalanceMessage(error.message || 'Reconciliation balances could not be saved.', 'error');
        } finally {
            el.saveBalancesButton.disabled = false;
        }
    }

    function bindEvents() {
        el.sourceForm.addEventListener('submit', loadWorkspace);
        el.savedGroupSelect.addEventListener('change', () => {
            if (el.savedGroupSelect.value) {
                el.groupLabel.value = el.savedGroupSelect.value;
                loadWorkspace();
            }
        });
        el.groupLabel.addEventListener('change', () => {
            if (state.accounts.length && currentGroupLabel().toLowerCase() !== state.loadedGroupLabel.toLowerCase()) {
                setMessage('The group name changed. Click Load accounts to refresh saved membership for this group.', 'info');
            }
        });
        el.accountSearch.addEventListener('input', renderAccounts);
        el.accountFilter.addEventListener('change', renderAccounts);
        el.selectVisibleButton.addEventListener('click', () => setVisibleSelection(true));
        el.clearVisibleButton.addEventListener('click', () => setVisibleSelection(false));
        el.accountTableBody.addEventListener('change', (event) => {
            const checkbox = event.target.closest('.account-toggle');
            if (!checkbox) return;
            toggleAccount(checkbox.dataset.accountCode, checkbox.checked);
        });
        el.saveButton.addEventListener('click', saveGroup);
        el.reportsViewButton.addEventListener('click', () => showView('reports'));
        el.setupViewButton.addEventListener('click', () => showView('setup'));
        el.setupGroupsTab.addEventListener('click', () => showSetupTab('groups'));
        el.setupCategoriesTab.addEventListener('click', () => showSetupTab('categories'));
        el.setupBalancesTab.addEventListener('click', () => showSetupTab('balances'));
        el.reportForm.addEventListener('submit', previewReport);
        el.downloadReportButton.addEventListener('click', downloadReport);
        el.previewFinancialStatementsButton.addEventListener('click', previewFinancialStatements);
        el.createCategorySetButton.addEventListener('click', createCategorySet);
        el.addCategoryButton.addEventListener('click', addCategory);
        el.saveCategoriesButton.addEventListener('click', saveCategories);
        el.categoryTableBody.addEventListener('click', (event) => {
            const button = event.target.closest('[data-category-remove]');
            if (!button) return;
            removeCategory(Number(button.dataset.categoryRemove));
        });
        el.categoryAccountSearch.addEventListener('input', renderCategoryAssignments);
        el.categoryAccountFilter.addEventListener('change', renderCategoryAssignments);
        el.categoryAssignmentsBody.addEventListener('change', (event) => {
            const select = event.target.closest('[data-category-account]');
            if (!select) return;
            const accountCode = select.dataset.categoryAccount;
            if (select.value) state.categoryAssignments.set(accountCode, select.value);
            else state.categoryAssignments.delete(accountCode);
            renderCategoryAssignments();
        });
        el.saveCategoryAssignmentsButton.addEventListener('click', saveCategoryAssignments);
        el.balanceForm.addEventListener('submit', loadBalanceGrid);
        el.saveBalancesButton.addEventListener('click', saveBalances);
        el.balanceTableBody.addEventListener('change', (event) => {
            const input = event.target.closest('[data-balance-index]');
            if (!input) return;
            updateBalanceRow(Number(input.dataset.balanceIndex), input.dataset.balanceField, input.value);
        });
    }

    async function initialise() {
        cacheElements();
        if (window.clientContextReady) await window.clientContextReady;
        const clientId = String(window.getSelectedClientId ? getSelectedClientId() : '');
        if (clientId) {
            state.clientId = clientId;
            el.groupLabel.value = 'Bank';
        }
        bindEvents();
        const view = new URLSearchParams(window.location.search).get('view') || 'reports';
        showView(view);
        if (clientId && view === 'setup' && state.setupTab === 'groups') loadWorkspace();
        if (clientId && view !== 'setup') loadReportsWorkspace();
        if (!clientId) {
            setStatus('Client required', 'error');
            setReportStatus('Client required', 'error');
            setMessage('Select a client before configuring a reporting group.', 'error');
            setReportMessage('Select a client before preparing a report.', 'error');
        }
    }

    document.addEventListener('DOMContentLoaded', initialise);
}());
