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
        [row.table_name, row.classification, formatBytes(row.table_bytes), formatBytes(row.index_bytes),
            formatBytes(row.other_relation_bytes),
            row.estimated_live_rows == null ? 'Unavailable' : formatNumber(row.estimated_live_rows),
            row.estimated_dead_rows == null ? 'Unavailable' : formatNumber(row.estimated_dead_rows),
            row.last_autovacuum ? new Date(row.last_autovacuum).toLocaleString() : 'Not recorded',
            row.formatted].forEach((value, index) => {
            const td = document.createElement('td');
            td.textContent = value;
            if (index === 1) td.className = row.classification === 'protected data' ? 'protected-label' : 'removable-label';
            tr.appendChild(td);
        });
        tableSizesBody.appendChild(tr);
    });
}

function renderStorageDiagnostics(preview) {
    const target = document.getElementById('storageDiagnostics');
    if (!target) return;
    target.replaceChildren();
    const diagnostics = preview.storage_diagnostics;
    const paragraph = (text) => {
        const p = document.createElement('p');
        p.textContent = text;
        target.appendChild(p);
    };
    if (!diagnostics) {
        paragraph('Detailed storage evidence is unavailable. No saving or index-removal recommendation can be made.');
        return;
    }
    paragraph(diagnostics.explanation);
    paragraph(`Allocated database files: ${formatBytes(diagnostics.allocated_bytes)}. Public tables and their indexes: ${formatBytes(diagnostics.public_relation_bytes)}. Other database allocation: ${formatBytes(diagnostics.other_database_bytes)}.`);
    paragraph(diagnostics.statistics_note);
    const maintenance = diagnostics.maintenance || {};
    if (maintenance.available) {
        paragraph(`Long snapshot transactions visible to this role: ${formatNumber(maintenance.long_snapshot_transactions)}. These can delay reuse of deleted-row space.`);
        const auto = (maintenance.settings || []).find(item => item.name === 'autovacuum');
        paragraph(`Automatic vacuum setting: ${auto ? auto.setting : 'Unavailable'}. A missing vacuum timestamp does not measure wasted space.`);
    } else {
        paragraph(maintenance.reason || 'Maintenance statistics are unavailable.');
    }
    const audit = diagnostics.audit_breakdown || {};
    if (audit.available) {
        (audit.actions || []).forEach(row => paragraph(`Audit action ${row.action}: ${formatNumber(row.row_count)} records; ${formatBytes(row.payload_bytes)} in old/new value payloads. This excludes record overhead and indexes.`));
    } else paragraph(audit.reason || 'Audit action breakdown is unavailable.');
    const emptyReviews = diagnostics.empty_table_reviews || [];
    if (emptyReviews.length) paragraph('Empty tables still need index structures for future uploads. Their allocated index files are listed below; this is not a measurement of waste or recoverable space. Rebuilding requires separate administrator approval.');
    emptyReviews.forEach(row => {
        paragraph(`${row.table_name}: ${formatNumber(row.exact_row_count)} records; ${formatBytes(row.allocated_index_bytes)} allocated to indexes.`);
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
    renderStorageDiagnostics(preview);
    renderCandidates(preview);
    renderIndexReview(preview);
    previewStatus.textContent = `Fresh preview generated ${new Date(preview.generated_at).toLocaleString()}`;
    cleanupComplete.style.display = 'none';
    if (indexMaintenanceResult) indexMaintenanceResult.style.display = 'none';
    updateApplyState();
}

async function loadPreview() {
    if (window.AppNativeStorageCleanup) { await loadNativeCleanup(); return; }
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
    if (window.AppNativeStorageCleanup) return;
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
    if (window.AppNativeStorageCleanup) return;
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

// Native hosting has its own bounded scope. It never falls through to global
// legacy cleanup or index-maintenance requests, including when disabled.
let nativeCleanupSequence = 0;
let nativeCleanupPreview = null;
let nativeCleanupBusy = false;
const nativeCleanupStatus = document.getElementById('nativeCleanupStatus');
const nativeCleanupSelect = document.getElementById('nativeCleanupTemplate');
const nativeCleanupApply = document.getElementById('nativeCleanupApply');
const nativeCleanupConfirm = document.getElementById('nativeCleanupConfirmation');
function nativeCleanupApplyState() {
    nativeCleanupApply.disabled = nativeCleanupBusy || !nativeCleanupPreview?.can_apply || nativeCleanupConfirm.value !== 'DELETE APPROVED VALIDATION PLANS';
}
async function loadNativeCleanup() {
    const sequence = ++nativeCleanupSequence;
    nativeCleanupPreview = null; nativeCleanupConfirm.value = ''; nativeCleanupApplyState();
    document.querySelectorAll('.cleanup-card,.cleanup-note').forEach(node => { if (node.id !== 'nativeCleanupPanel') node.hidden = true; });
    document.getElementById('nativeCleanupPanel').hidden = false;
    document.getElementById('nativeCleanupCounts').textContent = '';
    if (!window.AppNativeStorageCleanup.enabled || !window.AppNativeStorageCleanup.canApply) {
        nativeCleanupStatus.textContent = 'Native storage cleanup is disabled or unavailable for your access.';
        document.getElementById('nativeCleanupRefresh').disabled = true; nativeCleanupSelect.disabled = true; return;
    }
    if (window.clientContextReady) await window.clientContextReady;
    const client = Number(getSelectedClientId());
    if (!client) { nativeCleanupStatus.textContent = 'Choose a client before checking cleanup.'; return; }
    nativeCleanupStatus.textContent = 'Checking the selected client…';
    try {
        const scopes = await window.ApiUtils.apiFetch(`/api/storage-cleanup/scopes?client_id=${client}`, { cache: 'no-store' });
        if (!scopes.ok) throw new Error('Cleanup access or configuration needs review.');
        const body = await scopes.json();
        if (sequence !== nativeCleanupSequence || client !== Number(getSelectedClientId()) || body.client_id !== client) return;
        const previous = nativeCleanupSelect.value;
        nativeCleanupSelect.replaceChildren();
        body.templates.forEach(template => { const option = document.createElement('option'); option.value = template.id; option.textContent = template.name; nativeCleanupSelect.appendChild(option); });
        if ([...nativeCleanupSelect.options].some(option => option.value === previous)) nativeCleanupSelect.value = previous;
        const template = Number(nativeCleanupSelect.value);
        if (!template) { nativeCleanupStatus.textContent = 'No native templates are available for this client.'; return; }
        const params = new URLSearchParams({ client_id: client, template_id: template });
        const [preview, recent] = await Promise.all([
            window.ApiUtils.apiFetch(`/api/storage-cleanup/preview?${params}&kind=plans`, { cache: 'no-store' }),
            window.ApiUtils.apiFetch(`/api/storage-cleanup/operations?${params}`, { cache: 'no-store' })
        ]);
        if (!preview.ok || !recent.ok) throw new Error('Cleanup evidence is unavailable. No cleanup was submitted.');
        const data = await preview.json(), operations = await recent.json();
        if (sequence !== nativeCleanupSequence || client !== Number(getSelectedClientId()) || template !== Number(nativeCleanupSelect.value)) return;
        if (data.client_id !== client || data.template_id !== template || data.kind !== 'plans') throw new Error('Cleanup scope changed. Refresh to continue.');
        nativeCleanupPreview = data;
        document.getElementById('nativeCleanupCounts').textContent = `${Number(data.counts.upload_validation_plans).toLocaleString()} temporary plans are eligible.`;
        nativeCleanupStatus.textContent = data.can_apply ? `Fresh preview checked ${new Date(data.generated_at).toLocaleString()}.` : 'Active work or stored data needs review; cleanup is blocked.';
        const list = document.getElementById('nativeCleanupRecent'); list.replaceChildren();
        operations.forEach(operation => {
            const row = document.createElement('p');
            row.textContent = `${new Date(operation.completed_at).toLocaleString()} · ${operation.kind === 'purge' ? 'Stored data deleted' : 'Temporary plans removed'} · ${operation.artifact_status === 'complete' ? 'Artifact cleanup complete' : operation.artifact_status === 'pending' ? 'Temporary-file cleanup pending' : 'Temporary-file cleanup needs review'}`;
            list.appendChild(row);
        });
        if (!operations.length) list.textContent = 'No completed cleanup operations for this template.';
    } catch (error) {
        if (sequence === nativeCleanupSequence) { nativeCleanupPreview = null; nativeCleanupStatus.textContent = error.message; document.getElementById('nativeCleanupRecent').replaceChildren(); }
    } finally { if (sequence === nativeCleanupSequence) nativeCleanupApplyState(); }
}
async function applyNativePlanCleanup() {
    if (nativeCleanupBusy || nativeCleanupApply.disabled || !nativeCleanupPreview) return;
    const preview = nativeCleanupPreview, client = preview.client_id, template = preview.template_id;
    if (client !== Number(getSelectedClientId()) || template !== Number(nativeCleanupSelect.value)) return;
    nativeCleanupBusy = true; nativeCleanupApplyState(); nativeCleanupStatus.textContent = 'Submitting the confirmed cleanup…';
    const operation = crypto.randomUUID();
    // Only compact recovery handles are retained, never source rows or credentials.
    try { sessionStorage.setItem('native-cleanup-operation', JSON.stringify({ operation_id: operation, client_id: client, template_id: template })); } catch (_) {}
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 75000);
    try {
        const response = await window.ApiUtils.apiFetchMutate(`/api/storage-cleanup/apply?client_id=${client}&template_id=${template}&kind=plans`, {
            method: 'POST', signal: controller.signal, headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ operation_id: operation, preview_token: preview.preview_token, confirmation: nativeCleanupConfirm.value })
        });
        const result = await response.json();
        if (!response.ok || result.operation_id !== operation) throw new Error('Cleanup was not confirmed. Refresh to check recorded outcomes before retrying.');
        try { sessionStorage.removeItem('native-cleanup-operation'); } catch (_) {}
        nativeCleanupStatus.textContent = 'Temporary-plan cleanup committed.';
    } catch (error) { nativeCleanupStatus.textContent = error.name === 'AbortError' ? 'Outcome not confirmed; refresh to check recorded outcomes before retrying.' : error.message || 'Outcome not confirmed; check recorded outcomes before retrying.'; }
    finally { clearTimeout(timer); nativeCleanupBusy = false; nativeCleanupPreview = null; nativeCleanupConfirm.value = ''; nativeCleanupApplyState(); }
}
nativeCleanupConfirm.addEventListener('input', nativeCleanupApplyState);
nativeCleanupApply.addEventListener('click', applyNativePlanCleanup);
nativeCleanupSelect.addEventListener('change', () => { if (!nativeCleanupBusy) void loadNativeCleanup(); });
document.getElementById('nativeCleanupRefresh').addEventListener('click', () => { if (!nativeCleanupBusy) void loadNativeCleanup(); });
window.addEventListener('focus', () => { if (window.AppNativeStorageCleanup?.enabled && !nativeCleanupBusy) void loadNativeCleanup(); });
document.addEventListener('visibilitychange', () => {
    if (!window.AppNativeStorageCleanup) return;
    ++nativeCleanupSequence; nativeCleanupPreview = null; nativeCleanupApplyState();
    if (document.visibilityState === 'visible' && !nativeCleanupBusy) void loadNativeCleanup();
});
