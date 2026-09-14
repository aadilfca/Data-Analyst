// Administrator storage cleanup. Apply is enabled only for the latest preview
// and the exact typed confirmation phrase.

const CONFIRMATION_TEXT = 'DELETE APPROVED STORAGE DATA';
const previewButton = document.getElementById('refreshPreviewBtn');
const previewStatus = document.getElementById('previewStatus');
const cleanupError = document.getElementById('cleanupError');
const confirmationInput = document.getElementById('cleanupConfirmation');
const applyButton = document.getElementById('applyCleanupBtn');
const cleanupComplete = document.getElementById('cleanupComplete');
const tableSizesBody = document.getElementById('tableSizesBody');
const candidateBody = document.getElementById('candidateBody');
let currentPreviewHash = '';

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

function updateApplyState() {
    applyButton.disabled = !currentPreviewHash || confirmationInput.value !== CONFIRMATION_TEXT;
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
    rows.push({ name: 'Unreferenced deleted upload metadata', count: preview.removable.unreferenced_upload_metadata, size: 'Metadata only', rule: 'status=Deleted and no client_data row references upload' });
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
    currentPreviewHash = preview.preview_hash || '';
    setText('databaseSize', preview.database && preview.database.formatted);
    setText('clientDataCount', formatNumber(protectedData.client_data_total));
    setText('verificationCount', formatNumber(protectedData.verification_details));
    setText('trialBalanceCount', formatNumber((protectedData.trial_balance_accounts || 0) + (protectedData.trial_balance_uploads || 0)));
    setText('retiredCount', formatNumber(preview.removable.retired_client_data.row_count));
    setText('logStorage', (logBytes / (1024 * 1024)).toFixed(1) + ' MB');
    renderTableSizes(preview.table_sizes);
    renderCandidates(preview);
    previewStatus.textContent = `Fresh preview generated ${new Date(preview.generated_at).toLocaleString()}`;
    cleanupComplete.style.display = 'none';
    updateApplyState();
}

async function loadPreview() {
    clearError();
    currentPreviewHash = '';
    updateApplyState();
    previewButton.disabled = true;
    previewStatus.textContent = 'Loading preview…';
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

async function applyCleanup() {
    clearError();
    if (!currentPreviewHash || confirmationInput.value !== CONFIRMATION_TEXT) return;
    if (!window.confirm('Apply this fresh cleanup preview? Protected data will be retained.')) return;
    applyButton.disabled = true;
    previewButton.disabled = true;
    previewStatus.textContent = 'Applying cleanup transaction…';
    try {
        const response = await fetch(`${API_BASE}/api/storage-cleanup/apply`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preview_hash: currentPreviewHash, confirmation: confirmationInput.value })
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || 'Cleanup request failed');
        const deleted = Object.entries(body.deleted_counts || {}).map(([name, count]) => `${name}: ${formatNumber(count)}`).join(', ');
        const completionMessage = `Cleanup completed. Deleted — ${deleted || 'no matching rows'}. Compaction was not run.`;
        confirmationInput.value = '';
        if (body.preview) {
            renderPreview(body.preview);
        } else {
            currentPreviewHash = '';
            updateApplyState();
            previewStatus.textContent = 'Cleanup completed; refreshing preview…';
            await loadPreview();
        }
        cleanupComplete.textContent = completionMessage;
        cleanupComplete.style.display = 'block';
        previewStatus.textContent = 'Cleanup completed; preview refreshed.';
    } catch (error) {
        showError(error.message || 'Cleanup failed; the transaction was rolled back.');
        currentPreviewHash = '';
        updateApplyState();
        previewStatus.textContent = 'Cleanup not applied';
    } finally {
        previewButton.disabled = false;
        updateApplyState();
    }
}

confirmationInput.addEventListener('input', updateApplyState);
previewButton.addEventListener('click', loadPreview);
applyButton.addEventListener('click', applyCleanup);
window.addEventListener('load', loadPreview);
