// Storage Cleanup preview. The first phase is deliberately read-only.

const previewButton = document.getElementById('refreshPreviewBtn');
const previewStatus = document.getElementById('previewStatus');
const cleanupError = document.getElementById('cleanupError');
const tableSizesBody = document.getElementById('tableSizesBody');
const candidateBody = document.getElementById('candidateBody');

function setText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = value == null ? '—' : value;
}

function formatNumber(value) {
    return Number(value || 0).toLocaleString();
}

function showError(message) {
    cleanupError.textContent = message;
    cleanupError.style.display = 'block';
}

function clearError() {
    cleanupError.textContent = '';
    cleanupError.style.display = 'none';
}

function renderTableSizes(rows) {
    tableSizesBody.innerHTML = '';
    (rows || []).forEach(row => {
        const tr = document.createElement('tr');
        [row.table_name, row.classification, Number(row.table_bytes || 0).toLocaleString() + ' B', Number(row.index_bytes || 0).toLocaleString() + ' B', row.formatted].forEach((value, index) => {
            const td = document.createElement('td');
            td.textContent = value;
            if (index === 1) td.className = row.classification === 'protected data' ? 'protected-label' : 'removable-label';
            tr.appendChild(td);
        });
        tableSizesBody.appendChild(tr);
    });
}

function renderCandidates(preview) {
    candidateBody.innerHTML = '';
    const rows = (preview.removable.logs || []).map(log => ({ name: log.table_name, count: log.row_count, size: log.formatted, rule: 'Approved removable application log' }));
    const retired = preview.removable.retired_client_data || {};
    rows.push({ name: 'Retired client_data', count: retired.row_count, size: retired.formatted, rule: retired.rule });
    rows.push({ name: 'Unreferenced upload metadata', count: preview.removable.unreferenced_upload_metadata, size: 'Metadata only', rule: 'No client_data row references upload' });
    rows.push({ name: 'Stale verification presence', count: preview.removable.stale_presence, size: 'Small operational records', rule: 'Ended or heartbeat older than 15 minutes' });
    rows.push({ name: 'Expired login sessions', count: preview.removable.expired_login_sessions, size: 'Small operational records', rule: 'Expiry before current time' });
    rows.forEach(row => {
        const tr = document.createElement('tr');
        [row.name, formatNumber(row.count), row.size, row.rule].forEach(value => { const td = document.createElement('td'); td.textContent = value; tr.appendChild(td); });
        candidateBody.appendChild(tr);
    });
}

function renderPreview(preview) {
    const protectedData = preview.protected || {};
    const logs = (preview.removable && preview.removable.logs) || [];
    const logBytes = logs.reduce((sum, row) => sum + Number(row.physical_bytes || 0), 0);
    setText('databaseSize', preview.database && preview.database.formatted);
    setText('clientDataCount', formatNumber(protectedData.client_data_total));
    setText('verificationCount', formatNumber(protectedData.verification_details));
    setText('trialBalanceCount', formatNumber((protectedData.trial_balance_accounts || 0) + (protectedData.trial_balance_uploads || 0)));
    setText('retiredCount', formatNumber(preview.removable.retired_client_data.row_count));
    setText('logStorage', (logBytes / (1024 * 1024)).toFixed(1) + ' MB');
    renderTableSizes(preview.table_sizes);
    renderCandidates(preview);
    previewStatus.textContent = `Read-only preview generated ${new Date(preview.generated_at).toLocaleString()}`;
}

async function loadPreview() {
    clearError();
    previewButton.disabled = true;
    previewStatus.textContent = 'Loading read-only preview…';
    try {
        if (window.clientContextReady) await window.clientContextReady;
        const response = await fetch(`${API_BASE}/api/storage-cleanup/preview`);
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || 'Preview request failed');
        renderPreview(body);
    } catch (error) {
        previewStatus.textContent = 'Preview unavailable';
        showError(error.message || 'Unable to load the storage preview.');
    } finally {
        previewButton.disabled = false;
    }
}

previewButton.addEventListener('click', loadPreview);
window.addEventListener('load', loadPreview);
