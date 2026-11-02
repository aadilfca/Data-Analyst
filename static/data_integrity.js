const integrityTableBody = document.getElementById('integrityTableBody');
const integrityOverallStatus = document.getElementById('integrityOverallStatus');
const integrityScannedAt = document.getElementById('integrityScannedAt');
const integrityMessage = document.getElementById('integrityMessage');
const refreshIntegrityBtn = document.getElementById('refreshIntegrityBtn');
let integrityRequest = 0;

function integrityCount(value) {
    return Number(value || 0).toLocaleString();
}

function integrityStatusClass(value) {
    if (String(value || '').toLowerCase().includes('review')) return 'review';
    if (String(value || '').toLowerCase().includes('incomplete')) return 'incomplete';
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
        } else if (module.duplicate_status === 'Not fully checked') {
            duplicate.textContent = `Incomplete · ${integrityCount(module.unidentified_rows)} rows need identity`;
        } else {
            duplicate.textContent = 'Clear · 0 groups';
        }
        const duplicateCell = addIntegrityCell(row, '');
        duplicateCell.appendChild(duplicate);

        name.title = module.duplicate_basis || '';
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
    const status = data.overall_status || 'Not fully checked';
    integrityOverallStatus.textContent = status.includes('scan incomplete') ? 'Review · incomplete' : status;
    integrityOverallStatus.className = `integrity-status ${integrityStatusClass(status)}`.trim();
    integrityScannedAt.textContent = data.scanned_at
        ? `Checked ${new Date(data.scanned_at).toLocaleString()}`
        : 'Check complete';
    document.getElementById('integrityClientLabel').textContent =
        `Stored data and duplicate checks · ${data.client_name || 'Selected client'}`;
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
    const clientId = typeof getSelectedClientId === 'function' ? getSelectedClientId() : null;
    if (!clientId) {
        integrityOverallStatus.textContent = 'Select a client';
        integrityOverallStatus.className = 'integrity-status incomplete';
        integrityScannedAt.textContent = 'No client selected';
        integrityTableBody.replaceChildren();
        showIntegrityMessage('Select a client from the Dashboard to check its data.');
        return;
    }

    refreshIntegrityBtn.disabled = true;
    refreshIntegrityBtn.textContent = 'Checking…';
    integrityScannedAt.textContent = 'Checking records…';
    integrityOverallStatus.textContent = 'Checking';
    integrityOverallStatus.className = 'integrity-status';
    showIntegrityMessage('');
    try {
        const response = await window.ApiUtils.apiFetch(
            `/api/data-integrity/overview?client_id=${encodeURIComponent(clientId)}`
        );
        if (!response.ok) throw new Error('Could not load data check.');
        const data = await response.json();
        if (request !== integrityRequest || Number(getSelectedClientId()) !== Number(clientId)) return;
        renderIntegrityOverview(data);
    } catch (error) {
        if (request !== integrityRequest) return;
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
        if (request === integrityRequest) {
            refreshIntegrityBtn.disabled = false;
            refreshIntegrityBtn.textContent = 'Refresh check';
        }
    }
}

refreshIntegrityBtn.addEventListener('click', loadDataIntegrityOverview);
window.onload = async function() {
    if (window.clientContextReady) await window.clientContextReady;
    await loadDataIntegrityOverview();
};
