/*
 * Phase 4 — Account-group configuration interface.
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
    };

    const el = {};

    function cacheElements() {
        [
            'sourceForm', 'clientId', 'groupLabel', 'loadButton', 'savedGroupSelect', 'globalMessage',
            'workspace', 'sourceStatus', 'activeGroupHeading', 'totalAccounts', 'selectedAccounts',
            'suggestedAccounts', 'reviewAccounts', 'accountSearch', 'accountFilter', 'selectVisibleButton',
            'clearVisibleButton', 'accountTableBody', 'tableMessage', 'visibleCount', 'selectionHint',
            'saveButton',
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

    function getClientIdFromUrl() {
        const value = new URLSearchParams(window.location.search).get('client_id');
        return /^\d+$/.test(value || '') ? value : '';
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
        const clientId = normaliseText(el.clientId.value);
        if (!/^\d+$/.test(clientId) || Number(clientId) < 1) {
            setMessage('Enter a valid positive Client ID.', 'error');
            el.clientId.focus();
            return;
        }
        state.clientId = clientId;
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
            setMessage('Load a client and reporting group before saving.', 'error');
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
    }

    function initialise() {
        cacheElements();
        const clientId = getClientIdFromUrl();
        if (clientId) {
            el.clientId.value = clientId;
            el.groupLabel.value = 'Bank';
        }
        bindEvents();
        if (clientId) loadWorkspace();
    }

    document.addEventListener('DOMContentLoaded', initialise);
}());
