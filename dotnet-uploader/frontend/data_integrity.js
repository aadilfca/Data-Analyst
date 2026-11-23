const integrityTableBody = document.getElementById('integrityTableBody');
const integrityOverallStatus = document.getElementById('integrityOverallStatus');
const integrityScannedAt = document.getElementById('integrityScannedAt');
const integrityMessage = document.getElementById('integrityMessage');
const refreshIntegrityBtn = document.getElementById('refreshIntegrityBtn');
let integrityRequest = 0;
let integrityAbort = null;
let integrityReady = false;
const safeguardsOverallStatus = document.getElementById('safeguardsOverallStatus');
const safeguardsModules = document.getElementById('safeguardsModules');
const safeguardsScannedAt = document.getElementById('safeguardsScannedAt');
const integrityTabs = [document.getElementById('safeguardsTab'), document.getElementById('inventoryTab')];

function selectIntegrityTab(feature, updateUrl = true) {
    const inventory = feature === 'module-inventory';
    integrityTabs.forEach((tab, index) => {
        const selected = inventory === (index === 1);
        tab.setAttribute('aria-selected', String(selected));
        tab.tabIndex = selected ? 0 : -1;
        document.getElementById(tab.getAttribute('aria-controls')).hidden = !selected;
    });
    if (updateUrl) {
        const url = new URL(window.location.href);
        url.searchParams.set('feature', inventory ? 'module-inventory' : 'database-safeguards');
        window.history.replaceState(null, '', url);
    }
}

integrityTabs.forEach((tab, index) => {
    const activate = () => selectIntegrityTab(index ? 'module-inventory' : 'database-safeguards');
    tab.addEventListener('click', activate);
    tab.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const target = event.key === 'Home' ? 0 : event.key === 'End' ? 1 : 1 - index;
        integrityTabs[target].click();
        integrityTabs[target].focus();
    });
});
selectIntegrityTab(new URLSearchParams(window.location.search).get('feature'), false);

function safeguardStatus(value) {
    return ['Passed', 'Needs attention', 'Not checked'].includes(value) ? value : 'Not checked';
}

function setSafeguardBadge(element, status) {
    element.textContent = status;
    element.className = `integrity-status ${status === 'Needs attention' ? 'review' : status === 'Passed' ? '' : 'incomplete'}`.trim();
}

function combinedSafeguardStatus(statuses) {
    if (statuses.includes('Needs attention')) return 'Needs attention';
    return statuses.length && statuses.every(status => status === 'Passed') ? 'Passed' : 'Not checked';
}

function renderDatabaseSafeguards(data) {
    const safeguards = data.safeguards || {};
    safeguardsModules.replaceChildren();
    const moduleStatuses = [];
    let incomplete = false;
    // Missing module/check evidence is shown explicitly, including an older server response.
    ['Client Data', 'Trial Balance', 'Reconciliation'].forEach(name => {
        const module = (safeguards.modules || []).find(item => item.module === name) || {};
        const card = document.createElement('article');
        card.className = 'safeguard-module';
        const header = document.createElement('div');
        header.className = 'safeguard-module-header';
        const title = document.createElement('h2');
        title.textContent = name;
        const badge = document.createElement('span');
        header.append(title, badge);
        const checks = document.createElement('div');
        checks.className = 'safeguard-checks';
        const checkStatuses = [];
        ['Required indexes', 'Record identification', 'Duplicate check'].forEach(label => {
            const check = (module.checks || []).find(item => item.label === label) || {};
            const status = safeguardStatus(check.status);
            checkStatuses.push(status);
            if (status === 'Not checked') incomplete = true;
            const block = document.createElement('div');
            block.className = 'safeguard-check';
            const heading = document.createElement('strong');
            heading.textContent = label;
            const result = document.createElement('span');
            setSafeguardBadge(result, status);
            const detail = document.createElement('p');
            detail.textContent = check.detail || 'This check is unavailable. Refresh or restart the updated server.';
            block.append(heading, result, detail);
            checks.appendChild(block);
        });
        const status = combinedSafeguardStatus([...checkStatuses, safeguardStatus(module.status)]);
        setSafeguardBadge(badge, status);
        moduleStatuses.push(status);
        card.append(header, checks);
        if (module.row_count === 0) {
            const empty = document.createElement('p');
            empty.className = 'integrity-detail';
            empty.textContent = 'No records for this client. Index structure checks still apply.';
            card.appendChild(empty);
        }
        safeguardsModules.appendChild(card);
    });
    const status = combinedSafeguardStatus([...moduleStatuses, safeguardStatus(safeguards.status)]);
    setSafeguardBadge(safeguardsOverallStatus, status);
    const descriptions = {
        'Passed': 'Required checks passed for the three listed modules.',
        'Needs attention': 'One or more checks need review. Expand the explanation below for the check scope.',
        'Not checked': 'Some safeguards could not be verified. This is not a successful check.'
    };
    document.getElementById('safeguardsSummary').textContent = descriptions[status] +
        (status === 'Needs attention' && incomplete ? ' Some checks are also unavailable.' : '');
    document.getElementById('safeguardsScopeNote').textContent = safeguards.scope_note ||
        'Index checks cover the three listed modules. Record checks cover the selected client.';
    document.getElementById('safeguardsLimitNote').textContent = safeguards.limit_note ||
        'This checks the required baseline, not every physical index entry, all stored values, query speed or reclaimable storage.';
}

function integrityCount(value) {
    if (value === null || value === undefined) return 'Unavailable';
    return Number(value || 0).toLocaleString();
}

function integrityStatusClass(value) {
    if (String(value || '').toLowerCase().includes('review')) return 'review';
    if (/incomplete|not fully|unavailable|checking/i.test(String(value || ''))) return 'incomplete';
    return '';
}

function addIntegrityCell(row, value, className = '') {
    const cell = document.createElement('td');
    if (className) cell.className = className;
    cell.textContent = value == null ? '—' : String(value);
    row.appendChild(cell);
    return cell;
}

function renderIntegrityModules(data) {
    integrityTableBody.replaceChildren();
    (data.modules || []).forEach(module => {
        const row = document.createElement('tr');
        const name = addIntegrityCell(row, module.module, 'integrity-module');
        addIntegrityCell(row, integrityCount(module.row_count));
        const dataCell = addIntegrityCell(row, module.data_size || '—');
        const dataDetail = document.createElement('span');
        dataDetail.className = 'integrity-detail';
        dataDetail.textContent = module.size_status || 'Estimated';
        dataCell.appendChild(dataDetail);

        const associatedCell = addIntegrityCell(row, module.associated_data_size || '—');
        const associatedDetail = document.createElement('span');
        associatedDetail.className = 'integrity-detail';
        associatedDetail.textContent = `${integrityCount(module.associated_record_count)} related records`;
        associatedCell.appendChild(associatedDetail);
        if (module.unmeasured_source_files) {
            const unavailable = document.createElement('span');
            unavailable.className = 'integrity-detail';
            unavailable.textContent = `${integrityCount(module.unmeasured_source_files)} source files unavailable`;
            associatedCell.appendChild(unavailable);
        }
        addIntegrityCell(row, module.total_data_size || '—');

        const duplicate = document.createElement('span');
        duplicate.className = `integrity-duplicate ${integrityStatusClass(module.duplicate_status)}`.trim();
        if (module.duplicate_status === 'Review') {
            const groups = Number(module.duplicate_groups || 0);
            const extra = Number(module.duplicate_extra_rows || 0);
            duplicate.textContent = `Review · ${integrityCount(groups)} ${groups === 1 ? 'group' : 'groups'} / ${integrityCount(extra)} extra`;
        } else if (module.duplicate_status === 'Not fully checked' || module.duplicate_groups == null) {
            duplicate.textContent = module.unidentified_rows == null || module.duplicate_groups == null
                ? 'Not checked · evidence unavailable'
                : `Incomplete · ${integrityCount(module.unidentified_rows)} rows need identity`;
        } else {
            duplicate.textContent = 'Clear · 0 groups';
        }
        const duplicateCell = addIntegrityCell(row, '');
        duplicateCell.appendChild(duplicate);

        name.title = module.duplicate_basis || '';
        if (module.invalid_upload_links > 0) {
            const detail = document.createElement('span');
            detail.className = 'integrity-detail';
            detail.textContent = `${integrityCount(module.invalid_upload_links)} invalid upload links; associated size incomplete`;
            name.appendChild(detail);
        }
        integrityTableBody.appendChild(row);
    });
    if (!data.modules || !data.modules.length) {
        const row = document.createElement('tr');
        const cell = addIntegrityCell(row, 'No module data found for this client.');
        cell.colSpan = 6;
        integrityTableBody.appendChild(row);
    }
}

function showIntegrityMessage(text, isError = false) {
    integrityMessage.textContent = text;
    integrityMessage.classList.toggle('error', isError);
    integrityMessage.hidden = !text;
}

function renderIntegrityOverview(data) {
    renderIntegrityModules(data);
    renderDatabaseSafeguards(data);
    const status = data.overall_status || 'Not fully checked';
    integrityOverallStatus.textContent = status.includes('scan incomplete') ? 'Review · incomplete' : status;
    integrityOverallStatus.className = `integrity-status ${integrityStatusClass(status)}`.trim();
    integrityScannedAt.textContent = data.scanned_at
        ? `Checked ${new Date(data.scanned_at).toLocaleString()}`
        : 'Check complete';
    safeguardsScannedAt.textContent = integrityScannedAt.textContent;
    document.getElementById('integrityClientLabel').textContent =
        `Database safeguards and stored data · ${data.client_name || 'Selected client'}`;
    document.getElementById('integritySizeNote').textContent = data.size_note || 'Sizes are estimates.';
    document.getElementById('integrityScopeNote').textContent = data.scope_note ||
        'Matching IDs across modules are treated as separate data.';
    const context = data.database_context || {};
    document.getElementById('integrityDatabaseContext').textContent = context.database_size
        ? `Whole database: ${context.database_size} · shared indexes: ${context.shared_index_size || '—'}`
        : '';
    const unavailableFiles = (data.modules || []).reduce(
        (sum, module) => sum + Number(module.unmeasured_source_files || 0), 0
    );
    showIntegrityMessage(unavailableFiles
        ? `Size estimate incomplete: ${integrityCount(unavailableFiles)} source files could not be measured.`
        : '');
}

async function loadDataIntegrityOverview() {
    const request = ++integrityRequest;
    if (integrityAbort) integrityAbort.abort();
    // Presence identifies native hosting even when this feature is disabled.
    if (window.AppNativeDataIntegrity && !window.AppNativeDataIntegrity.enabled) {
        invalidateIntegrityEvidence('Native Data Integrity is disabled.');
        refreshIntegrityBtn.disabled = true;
        refreshIntegrityBtn.textContent = 'Refresh check';
        return;
    }
    const clientId = typeof getSelectedClientId === 'function' ? getSelectedClientId() : null;
    if (!clientId) {
        renderDatabaseSafeguards({});
        safeguardsScannedAt.textContent = 'No client selected';
        refreshIntegrityBtn.disabled = false;
        refreshIntegrityBtn.textContent = 'Refresh check';
        integrityOverallStatus.textContent = 'Select a client';
        integrityOverallStatus.className = 'integrity-status incomplete';
        integrityScannedAt.textContent = 'No client selected';
        integrityTableBody.replaceChildren();
        document.getElementById('integrityDatabaseContext').textContent = '';
        showIntegrityMessage('Select a client from the Dashboard to check its data.');
        return;
    }

    refreshIntegrityBtn.disabled = true;
    refreshIntegrityBtn.textContent = 'Checking…';
    integrityScannedAt.textContent = 'Checking records…';
    safeguardsScannedAt.textContent = 'Checking records…';
    safeguardsModules.replaceChildren();
    setSafeguardBadge(safeguardsOverallStatus, 'Checking');
    document.getElementById('safeguardsSummary').textContent = 'Checking the required indexes, record identification and duplicates.';
    integrityTableBody.replaceChildren();
    document.getElementById('integrityDatabaseContext').textContent = '';
    integrityOverallStatus.textContent = 'Checking';
    integrityOverallStatus.className = 'integrity-status incomplete';
    showIntegrityMessage('');
    const controller = new AbortController();
    integrityAbort = controller;
    const timeout = setTimeout(() => controller.abort(), 65000);
    try {
        const response = await window.ApiUtils.apiFetch(
            `/api/data-integrity/overview?client_id=${encodeURIComponent(clientId)}`,
            { signal: controller.signal, cache: 'no-store' }
        );
        if (!response.ok) throw new Error('Could not load data check.');
        const data = await response.json();
        if (request !== integrityRequest || Number(getSelectedClientId()) !== Number(clientId)) return;
        if (Number(data.client_id) !== Number(clientId)) throw new Error('Client scope mismatch.');
        renderIntegrityOverview(data);
    } catch (error) {
        if (request !== integrityRequest || Number(getSelectedClientId()) !== Number(clientId)) return;
        renderDatabaseSafeguards({});
        safeguardsScannedAt.textContent = 'Check could not be completed';
        integrityTableBody.replaceChildren();
        const row = document.createElement('tr');
        const cell = addIntegrityCell(row, 'Data check unavailable. Try again.');
        cell.colSpan = 6;
        integrityTableBody.appendChild(row);
        integrityOverallStatus.textContent = 'Unavailable';
        integrityOverallStatus.className = 'integrity-status incomplete';
        integrityScannedAt.textContent = 'Check could not be completed';
        showIntegrityMessage('Data check unavailable. Try again.', true);
    } finally {
        clearTimeout(timeout);
        if (request === integrityRequest) {
            integrityAbort = null;
            refreshIntegrityBtn.disabled = false;
            refreshIntegrityBtn.textContent = 'Refresh check';
        }
    }
}

function invalidateIntegrityEvidence(message) {
    renderDatabaseSafeguards({});
    safeguardsScannedAt.textContent = message;
    integrityOverallStatus.textContent = 'Not checked';
    integrityOverallStatus.className = 'integrity-status incomplete';
    integrityScannedAt.textContent = message;
    integrityTableBody.replaceChildren();
    document.getElementById('integrityDatabaseContext').textContent = '';
    document.getElementById('integrityClientLabel').textContent = 'Check database safeguards and stored data for this client.';
    showIntegrityMessage(message);
}

function suspendIntegrityEvidence() {
    ++integrityRequest;
    if (integrityAbort) integrityAbort.abort();
    integrityAbort = null;
    invalidateIntegrityEvidence('Return to refresh this check.');
    refreshIntegrityBtn.disabled = false;
    refreshIntegrityBtn.textContent = 'Refresh check';
}

function resumeIntegrityEvidence() {
    if (integrityReady && document.visibilityState === 'visible' && !integrityAbort) loadDataIntegrityOverview();
}
window.addEventListener('pagehide', suspendIntegrityEvidence);
window.addEventListener('pageshow', event => { if (event.persisted) resumeIntegrityEvidence(); });
window.addEventListener('focus', resumeIntegrityEvidence);
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') suspendIntegrityEvidence();
    else resumeIntegrityEvidence();
});

refreshIntegrityBtn.addEventListener('click', loadDataIntegrityOverview);
window.onload = async function() {
    if (window.clientContextReady) await window.clientContextReady;
    integrityReady = true;
    await loadDataIntegrityOverview();
};
