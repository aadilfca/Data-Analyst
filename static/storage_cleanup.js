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
const indexReviewBody = document.getElementById('indexReviewBody');
const indexMaintenanceConfirmation = document.getElementById('indexMaintenanceConfirmation');
const indexMaintenanceResult = document.getElementById('indexMaintenanceResult');
const INDEX_MAINTENANCE_CONFIRMATION = 'REMOVE APPROVED INDEX';
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
    document.querySelectorAll('.index-maintenance-button').forEach(button => {
        button.disabled = !currentPreviewHash || indexMaintenanceConfirmation.value !== INDEX_MAINTENANCE_CONFIRMATION;
    });
}

function formatBytes(value) {
    const bytes = Number(value || 0);
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / Math.pow(1024, exponent)).toFixed(exponent === 0 ? 0 : 1)} ${units[exponent]}`;
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
    const cashflow = preview.removable.cashflow_historical_revisions || {};
    const legacy = cashflow.legacy_rollup_rows || {};
    const preparedLines = cashflow.prepared_line_revisions || {};
    const preparedRollups = cashflow.prepared_rollup_revisions || {};
    rows.push({ name: 'Obsolete Cashflows legacy rollup rows', count: legacy.row_count, size: legacy.formatted, rule: legacy.rule });
    rows.push({ name: 'Stale Cashflows prepared lines', count: preparedLines.row_count, size: preparedLines.formatted, rule: preparedLines.rule });
    rows.push({ name: 'Stale Cashflows prepared rollups', count: preparedRollups.row_count, size: preparedRollups.formatted, rule: preparedRollups.rule });
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

function renderEvidenceStatus(elementId, label, evidence, availableText) {
    const element = document.getElementById(elementId);
    if (!element) return;
    if (evidence && evidence.available) {
        element.className = 'cleanup-evidence cleanup-evidence-ok';
        element.textContent = `${label}: ${availableText}`;
        return;
    }
    element.className = 'cleanup-evidence cleanup-evidence-warning';
    element.textContent = `${label}: unavailable; related actions remain blocked. ${evidence && evidence.reason ? evidence.reason : 'The server did not provide evidence.'}`;
}

function renderIndexReview(preview) {
    const review = preview.index_review || {};
    const indexes = review.indexes || [];
    const candidates = review.maintenance_candidates || {};
    setText('indexReviewTotal', formatNumber(indexes.length));
    setText('indexReviewBytes', formatBytes(review.index_bytes_total));
    setText('duplicateIdentityGroups', formatNumber(review.active_identity_duplicate_group_count));

    const plans = review.plan_evidence || {};
    renderEvidenceStatus(
        'indexPlanEvidenceStatus',
        'Planner evidence',
        plans,
        `${(plans.plans || []).length} representative plan(s) captured.`
    );
    const bloat = review.bloat_evidence || {};
    renderEvidenceStatus(
        'indexBloatEvidenceStatus',
        'Bloat evidence',
        bloat,
        `${Object.keys(bloat.indexes || {}).length} allow-listed index measurement(s) captured.`
    );

    indexReviewBody.innerHTML = '';
    if (!indexes.length) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 6;
        cell.textContent = 'No client_data index evidence was returned.';
        row.appendChild(cell);
        indexReviewBody.appendChild(row);
        return;
    }

    indexes.forEach(index => {
        const candidate = candidates[index.index_name] || {};
        const row = document.createElement('tr');
        const values = [
            index.index_name,
            formatBytes(index.index_bytes),
            formatNumber(index.idx_scan),
            index.protected ? 'Protected' : 'Review candidate',
            candidate.reason || (index.protected ? 'Protected integrity/search index' : 'No server decision'),
        ];
        values.forEach((value, position) => {
            const cell = document.createElement('td');
            cell.textContent = value;
            if (position === 3) cell.className = index.protected ? 'protected-label' : 'removable-label';
            row.appendChild(cell);
        });
        const actionCell = document.createElement('td');
        const actions = candidate.optimization_actions || [];
        if (!actions.length) {
            actionCell.textContent = '—';
        } else {
            actions.forEach(operation => {
                const eligible = operation === 'drop'
                    ? candidate.eligible_for_manual_approval
                    : candidate.reindex_eligible_for_manual_approval;
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'cleanup-button cleanup-maintenance-button index-maintenance-button';
                button.dataset.indexName = index.index_name;
                button.dataset.operation = operation;
                button.textContent = operation === 'drop' ? 'Approve drop' : 'Approve reindex';
                button.title = eligible ? 'Use the separate confirmation below to enable this action.' : 'Blocked by the server evidence gate.';
                button.disabled = true;
                if (!eligible) button.classList.add('cleanup-evidence-blocked');
                actionCell.appendChild(button);
            });
        }
        row.appendChild(actionCell);
        indexReviewBody.appendChild(row);
    });
}

function showIndexMaintenanceResult(body) {
    if (!indexMaintenanceResult) return;
    const verification = body.post_verification || {};
    const status = verification.status || 'unavailable';
    indexMaintenanceResult.className = `cleanup-maintenance-result${status === 'passed' ? '' : ' review-required'}`;
    indexMaintenanceResult.textContent = status === 'passed'
        ? `${body.operation || 'Index maintenance'} completed for ${body.index_name}. Post-maintenance verification passed; protected row counts and protected index definitions were unchanged.`
        : `${body.operation || 'Index maintenance'} completed for ${body.index_name}, but post-maintenance verification requires review. ${verification.message || 'Do not perform another optimization action until the database is reviewed.'}`;
    indexMaintenanceResult.style.display = 'block';
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
    renderIndexReview(preview);
    previewStatus.textContent = `Fresh preview generated ${new Date(preview.generated_at).toLocaleString()}`;
    cleanupComplete.style.display = 'none';
    if (indexMaintenanceResult) indexMaintenanceResult.style.display = 'none';
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
        showError(error.message || 'Storage preview is unavailable; no cleanup or optimization action was performed.');
    } finally {
        previewButton.disabled = false;
    }
}

async function applyIndexMaintenance(event) {
    clearError();
    const button = event.currentTarget;
    const indexName = button.dataset.indexName;
    const operation = button.dataset.operation;
    if (!currentPreviewHash || indexMaintenanceConfirmation.value !== INDEX_MAINTENANCE_CONFIRMATION || !indexName || !operation) return;
    if (!window.confirm(`Apply the server-approved ${operation} action for ${indexName}? Canonical data rows will not be changed.`)) return;

    document.querySelectorAll('.index-maintenance-button').forEach(actionButton => { actionButton.disabled = true; });
    previewButton.disabled = true;
    previewStatus.textContent = `Applying ${operation} concurrently…`;
    try {
        const response = await fetch(`${API_BASE}/api/storage-cleanup/index-maintenance`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                preview_hash: currentPreviewHash,
                index_name: indexName,
                operation,
                confirmation: indexMaintenanceConfirmation.value,
            })
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || 'Index maintenance request failed');
        indexMaintenanceConfirmation.value = '';
        if (body.after) {
            renderPreview(body.after);
            previewStatus.textContent = 'Index maintenance completed; fresh evidence displayed.';
        } else {
            currentPreviewHash = '';
            updateApplyState();
            previewStatus.textContent = 'Index maintenance completed; post-preview unavailable.';
        }
        showIndexMaintenanceResult(body);
    } catch (error) {
        currentPreviewHash = '';
        updateApplyState();
        showError(error.message || 'Index maintenance was not applied; review the database state before retrying.');
        previewStatus.textContent = 'Index maintenance not applied';
    } finally {
        previewButton.disabled = false;
        updateApplyState();
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
indexMaintenanceConfirmation.addEventListener('input', updateApplyState);
previewButton.addEventListener('click', loadPreview);
applyButton.addEventListener('click', applyCleanup);
indexReviewBody.addEventListener('click', event => {
    const button = event.target.closest('.index-maintenance-button');
    if (button) applyIndexMaintenance({ currentTarget: button });
});
window.addEventListener('load', loadPreview);
