// ==============================================
// 🔧 SETUP MODULE LOGIC
// ==============================================

// API_BASE is defined globally in client_context.js

// ---------- LOAD STATS ----------
async function loadSetupStats() {
    try {
        // Get column count
        const colRes = await window.ApiUtils.apiFetch(API_BASE + '/api/columns');
        const columns = await colRes.json();
        document.getElementById('totalColumns').textContent = columns.length || 0;

        // Get template count
        const tempRes = await window.ApiUtils.apiFetch(API_BASE + '/api/templates');
        const templates = await tempRes.json();
        document.getElementById('totalTemplates').textContent = templates.length || 0;

        // Count active templates
        const active = templates.filter(t => t.is_active !== 0).length;
        document.getElementById('activeTemplates').textContent = active || 0;
    } catch (error) {
        document.getElementById('totalColumns').textContent = '⚠️';
        document.getElementById('totalTemplates').textContent = '⚠️';
        document.getElementById('activeTemplates').textContent = '⚠️';
    }
}

// ---------- ADD CLICK FEEDBACK ----------
document.querySelectorAll('.module-card').forEach(card => {
    card.addEventListener('click', function() {
        this.style.transform = 'scale(0.95)';
        setTimeout(() => {
            this.style.transform = '';
        }, 150);
    });
});

// ---------- BACKUP EXPORT ----------
async function exportBackup() {
    if (window.AppConfigurationBackup) return;
    const statusSpan = document.getElementById('backupStatus');
    try {
        statusSpan.style.display = 'inline';
        statusSpan.textContent = '⏳ Exporting...';
        statusSpan.style.color = '#92400e';

        const res = await fetch(API_BASE + '/api/backup/export');
        if (!res.ok) throw new Error('Export failed');
        const data = await res.json();

        // Create download
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `setup_backup_${new Date().toISOString().slice(0,10)}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);

        statusSpan.textContent = '✅ Export successful!';
        statusSpan.style.color = '#166534';
        setTimeout(() => {
            statusSpan.style.display = 'none';
        }, 4000);
    } catch (error) {
        statusSpan.textContent = '❌ Export error: ' + error.message;
        statusSpan.style.color = '#991b1b';
        console.error(error);
    }
}

// ---------- BACKUP IMPORT ----------
async function handleImportFile(event) {
    if (window.AppConfigurationBackup) return;
    const file = event.target.files[0];
    if (!file) return;

    const statusSpan = document.getElementById('backupStatus');
    try {
        const reader = new FileReader();
        reader.onload = async function(e) {
            try {
                const jsonData = JSON.parse(e.target.result);
                // Validate structure
                if (!jsonData.columns || !jsonData.templates) {
                    throw new Error('Invalid backup file: missing "columns" or "templates" keys.');
                }

                const confirmMsg = `This legacy import REPLACES existing setup, client and transaction data, and clears verification records.\n` +
                    `It is not the full-database recovery process. Use it only for an approved transfer after taking a verified full backup.\n\n` +
                    `Columns: ${jsonData.columns.length}\nTemplates: ${jsonData.templates.length}\nClient-data rows: ${(jsonData.client_data || []).length}\n\nContinue?`;
                if (!confirm(confirmMsg)) {
                    statusSpan.style.display = 'inline';
                    statusSpan.textContent = '⛔ Import cancelled.';
                    statusSpan.style.color = '#92400e';
                    setTimeout(() => {
                        statusSpan.style.display = 'none';
                    }, 3000);
                    return;
                }

                statusSpan.style.display = 'inline';
                statusSpan.textContent = '⏳ Importing...';
                statusSpan.style.color = '#92400e';

                const res = await fetch(API_BASE + '/api/backup/import', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(jsonData)
                });
                const result = await res.json();
                if (!res.ok) {
                    throw new Error(result.detail || result.message || 'Import failed');
                }

                statusSpan.textContent = '✅ ' + result.message;
                statusSpan.style.color = '#166534';
                // Reload stats
                loadSetupStats();
                setTimeout(() => {
                    statusSpan.style.display = 'none';
                }, 5000);
            } catch (error) {
                statusSpan.textContent = '❌ Import error: ' + error.message;
                statusSpan.style.color = '#991b1b';
                console.error(error);
            }
        };
        reader.readAsText(file);
    } catch (error) {
        statusSpan.textContent = '❌ File read error: ' + error.message;
        statusSpan.style.color = '#991b1b';
        console.error(error);
    } finally {
        // Reset file input so same file can be re-uploaded
        event.target.value = '';
    }
}

// ---------- LOAD ON PAGE OPEN ----------
window.onload = async function() {
    if (window.clientContextReady) await window.clientContextReady;
    loadSetupStats();

    const storageCleanupCard = document.getElementById('storageCleanupCard');
    if (storageCleanupCard) {
        const openStorageCleanup = () => { window.location.href = '/static/storage_cleanup.html'; };
        storageCleanupCard.addEventListener('click', openStorageCleanup);
        storageCleanupCard.addEventListener('keydown', function(event) {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                openStorageCleanup();
            }
        });
    }

    // Backup export button
    if (window.AppConfigurationBackup) { document.getElementById('legacySetupTransfer').hidden = true; return; }
    document.getElementById('exportBackupBtn').addEventListener('click', exportBackup);
    // Backup import button triggers file input
    document.getElementById('importBackupBtn').addEventListener('click', function() {
        document.getElementById('importFileInput').click();
    });
    document.getElementById('importFileInput').addEventListener('change', handleImportFile);
};

// Reload stats when page becomes visible again (e.g., after navigating back from columns/templates)
window.addEventListener('pageshow', function(event) {
    loadSetupStats();
});

// Manual full backup is isolated from the existing JSON setup-data importer.
(function () {
    'use strict';
    const localOnly = window.AppLocalDatabaseBackup?.native === true;
    const pendingKey = 'fullDatabaseBackup.pending';
    let ready = false;
    let busy = false;
    let running = false;
    let operation = null;
    let progressTimer = null;
    let progressToken = 0;
    let lastLoggedStage = null;
    const panel = document.getElementById('fullDatabaseBackup');
    const createButton = document.getElementById('createFullDatabaseBackupBtn');
    const checkButton = document.getElementById('checkFullDatabaseBackupBtn');
    const message = document.getElementById('fullDatabaseBackupStatus');

    // A C# host with this default-off capability must not probe the backup API
    // or expose the panel until local backup is explicitly enabled.
    if (!localOnly || window.AppLocalDatabaseBackup?.enabled !== true) {
        panel.hidden = true;
        return;
    }

    function remember(value) {
        operation = value;
        try {
            if (value) sessionStorage.setItem(pendingKey, value);
            else sessionStorage.removeItem(pendingKey);
        } catch (_) { /* In-memory identity still protects this page's retry. */ }
    }

    function say(text, kind) {
        message.textContent = text;
        message.dataset.kind = kind || '';
    }

    function buttons() {
        createButton.disabled = busy || running || !ready;
        createButton.textContent = busy ? 'Creating backup…' : 'Create full backup';
        checkButton.hidden = !operation;
        checkButton.disabled = busy;
    }

    function showBackup(value) {
        if (!value || !value.bytes && value.state !== 'complete') return;
        document.getElementById('fullDatabaseBackupDetails').hidden = false;
        document.getElementById('fullDatabaseBackupFile').textContent = value.file_name || 'Not yet created';
        document.getElementById('fullDatabaseBackupPath').textContent = value.local_path || '';
        document.getElementById('fullDatabaseBackupLocalStatus').textContent = value.state === 'complete' ? 'Verified locally' : 'Not yet verified';
        document.getElementById('fullDatabaseBackupCreated').textContent = value.created_at ? new Date(value.created_at).toLocaleString() : '';
        document.getElementById('fullDatabaseBackupVerified').textContent = value.verified_at ? new Date(value.verified_at).toLocaleString() : 'Not yet completed';
    }

    function showProgress(value, active) {
        if (!value) return;
        const fallback = value.state === 'complete' ? { stage: 'complete', completed_steps: 4, label: 'Both backup copies verified' } : { stage: 'export', completed_steps: 0, label: 'Saving database data' };
        const progress = value.progress || fallback;
        const completed = Math.max(0, Math.min(4, Number(progress.completed_steps) || 0));
        const area = document.getElementById('fullDatabaseBackupProgress');
        area.hidden = false;
        area.setAttribute('aria-busy', String(!!active));
        document.getElementById('fullDatabaseBackupSpinner').hidden = !active;
        const label = completed === 4 ? progress.label : `Step ${completed + 1} of 4 — ${progress.label}`;
        document.getElementById('fullDatabaseBackupStep').textContent = label;
        const seconds = Math.max(0, Number(progress.elapsed_seconds) || 0);
        const elapsed = `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
        const size = (Math.max(0, Number(progress.local_bytes) || 0) / 1048576).toFixed(1);
        document.getElementById('fullDatabaseBackupMetrics').textContent = `${completed}/4 stages complete · ${size} MiB saved locally · ${elapsed} elapsed`;
        document.getElementById('fullDatabaseBackupActivity').textContent = progress.note || progress.label;
        const bar = document.getElementById('fullDatabaseBackupBar');
        bar.setAttribute('aria-valuenow', String(completed));
        bar.setAttribute('aria-valuetext', label);
        document.getElementById('fullDatabaseBackupFill').style.width = `${completed * 25}%`;
        panel.querySelectorAll('[data-backup-step]').forEach(item => {
            const step = Number(item.dataset.backupStep);
            item.classList.toggle('done', step < completed);
            item.classList.toggle('active', completed < 4 && step === completed);
        });
        const logKey = progress.stage + ':' + (value.error ? 'failed' : '');
        if (lastLoggedStage !== logKey) {
            const item = document.createElement('li');
            item.textContent = new Date().toLocaleTimeString() + ' — ' + (value.error || progress.note || progress.label);
            const log = document.getElementById('fullDatabaseBackupLog');
            log.appendChild(item);
            while (log.children.length > 8) log.firstElementChild.remove();
            lastLoggedStage = logKey;
        }
    }

    function stopProgress() {
        progressToken += 1;
        if (progressTimer) clearTimeout(progressTimer);
        progressTimer = null;
    }

    function startProgress(reference) {
        stopProgress();
        const token = progressToken;
        async function poll() {
            if (!busy || token !== progressToken) return;
            try {
                const response = await window.ApiUtils.apiFetch(API_BASE + '/api/database-backup/status?operation_id=' + encodeURIComponent(reference));
                if (response.ok) {
                    const data = await response.json();
                    if (busy && token === progressToken && data.backup) {
                        showProgress(data.backup, data.backup.running === true);
                        showBackup(data.backup);
                    }
                }
            } catch (_) { /* The POST keeps its identity; a failed status read never restarts it. */ }
            if (busy && token === progressToken) progressTimer = setTimeout(poll, 2500);
        }
        progressTimer = setTimeout(poll, 1000);
    }

    async function checkStatus() {
        if (busy) return;
        const suffix = operation ? '?operation_id=' + encodeURIComponent(operation) : '';
        let response = await window.ApiUtils.apiFetch(API_BASE + '/api/database-backup/status' + suffix);
        // A pending reference from another admin session is never reused.
        if (response.status === 404 && operation) {
            remember(null);
            response = await window.ApiUtils.apiFetch(API_BASE + '/api/database-backup/status');
        }
        if (response.status === 403 || response.status === 404) { panel.hidden = true; return; }
        panel.hidden = false;
        if (!response.ok) throw new Error('Backup availability could not be checked. Try Check backup status.');
        const data = await response.json();
        ready = data.ready === true;
        running = data.backup && data.backup.running === true;
        showBackup(data.backup);
        showProgress(data.backup, running);
        if (data.backup && data.backup.state === 'complete') {
            remember(null);
            say('Full database backup saved and verified on this computer. Your working data stays in use.', 'success');
        } else if (running) {
            say('The backup is running. Its current step is shown below; check status before starting another copy.');
        } else if (data.backup && data.backup.state === 'incomplete') {
            remember(null);
            say(data.backup.error || 'The earlier local backup did not finish. Its files were retained; you can create a new backup.', 'error');
        } else {
            say(ready ? 'Ready to create and verify a new dated local backup.'
                : (data.requirements || ['Full backup is not configured on this computer.']).join(' '));
        }
        buttons();
    }

    async function create() {
        if (busy || running || !ready) return;
        if (!operation) {
            const bytes = new Uint8Array(16);
            crypto.getRandomValues(bytes);
            remember(Array.from(bytes, value => value.toString(16).padStart(2, '0')).join(''));
        }
        busy = true;
        running = false;
        lastLoggedStage = null;
        document.getElementById('fullDatabaseBackupLog').replaceChildren();
        document.getElementById('fullDatabaseBackupDetails').hidden = true;
        buttons();
        say('Creating and verifying your full local backup. This can take several minutes.');
        showProgress({ state: 'creating', progress: { stage: 'export', completed_steps: 0, label: 'Saving database data' } }, true);
        startProgress(operation);
        try {
            // Never retry POST automatically: the saved operation reference supports
            // deliberate recovery after a lost response without a second dump.
            const response = await window.ApiUtils.apiFetchMutate(API_BASE + '/api/database-backup/create', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ operation_id: operation })
            }, { target: createButton, message: 'Creating backup' });
            const data = await response.json();
            stopProgress();
            showBackup(response.ok ? data : data.backup);
            showProgress(response.ok ? data : data.backup, false);
            if (!response.ok) {
                if (!data.retry_same) remember(null);
                say(typeof data.detail === 'string' ? data.detail : 'The backup could not be completed. Check its status before retrying.', 'error');
                return;
            }
            remember(null);
            say('Full database backup saved and verified on this computer. Your working data stays in use.', 'success');
        } catch (_) {
            say('The connection was interrupted. Use Check backup status; the same backup reference has been retained.', 'error');
        } finally {
            stopProgress();
            document.getElementById('fullDatabaseBackupSpinner').hidden = true;
            document.getElementById('fullDatabaseBackupProgress').setAttribute('aria-busy', 'false');
            busy = false;
            buttons();
        }
    }

    window.addEventListener('load', async function () {
        await window.clientContextReady;
        if (!window.isAdminUser || !window.isAdminUser()) return;
        try {
            const stored = sessionStorage.getItem(pendingKey);
            if (/^[0-9a-f]{32}$/.test(stored || '')) operation = stored;
        } catch (_) { /* No persistent pending reference is available. */ }
        createButton.addEventListener('click', create);
        checkButton.addEventListener('click', function () {
            checkStatus().catch(error => { say(error.message, 'error'); buttons(); });
        });
        try { await checkStatus(); }
        catch (error) { panel.hidden = false; say(error.message, 'error'); buttons(); }
    });
    window.addEventListener('pagehide', stopProgress);
})();

// Native configuration files stay in memory with their exact JSON text. A
// parsed copy supplies display labels only; version counters never round-trip
// through JavaScript numbers on export or restore.
(function () {
    'use strict';
    const capability = window.AppConfigurationBackup;
    if (!capability) return;
    const panel = document.getElementById('configurationBackup');
    const message = document.getElementById('configurationBackupStatus');
    const exportButton = document.getElementById('exportConfigurationBtn');
    const selectButton = document.getElementById('selectConfigurationBtn');
    const fileInput = document.getElementById('configurationFileInput');
    const previewPanel = document.getElementById('configurationRestorePreview');
    const checkbox = document.getElementById('confirmConfigurationRestore');
    const restoreButton = document.getElementById('restoreConfigurationBtn');
    const cancelButton = document.getElementById('cancelConfigurationRestoreBtn');
    const maximumBytes = 7 * 1024 * 1024;
    let busy = false, generation = 0, preview = null, activeRequest = null;
    const allowed = capability.enabled && capability.canEdit;
    panel.hidden = false;
    document.getElementById('legacySetupTransfer').hidden = true;
    function say(text, kind = '') { message.textContent = text; message.dataset.kind = kind; }
    function buttons() {
        exportButton.disabled = !allowed || busy; selectButton.disabled = !allowed || busy;
        restoreButton.disabled = !allowed || busy || !preview || !checkbox.checked;
        checkbox.disabled = busy; cancelButton.disabled = busy;
        panel.setAttribute('aria-busy', String(busy));
    }
    function clearReview() {
        generation += 1; activeRequest?.abort(); preview = null; checkbox.checked = false;
        previewPanel.hidden = true; document.getElementById('configurationRestoreItems').replaceChildren(); buttons();
    }
    async function readText(response) {
        const reader = response.body.getReader(), chunks = []; let bytes = 0;
        try {
            while (true) {
                const item = await reader.read(); if (item.done) break;
                bytes += item.value.byteLength;
                if (bytes > maximumBytes) throw new Error('Configuration backup exceeds the 7 MiB file limit.');
                chunks.push(item.value);
            }
        } catch (error) { await reader.cancel().catch(() => {}); throw error; }
        const combined = new Uint8Array(bytes); let position = 0;
        chunks.forEach(chunk => { combined.set(chunk, position); position += chunk.byteLength; });
        return new TextDecoder('utf-8', {fatal: true}).decode(combined);
    }
    async function request(path, body) {
        const controller = new AbortController(); activeRequest = controller;
        const timer = setTimeout(() => controller.abort(), 30000);
        try {
            const response = await fetch(API_BASE + '/api/backup/' + path, { method: body === undefined ? 'GET' : 'POST',
                cache: 'no-store', signal: controller.signal,
                headers: body === undefined ? {} : {'Content-Type': 'application/json'}, body });
            const text = await readText(response);
            let data; try { data = JSON.parse(text); } catch (_) { throw new Error('The configuration response could not be read.'); }
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Configuration request failed.');
            return {text, data};
        } finally { clearTimeout(timer); if (activeRequest === controller) activeRequest = null; }
    }
    function addItem(text) {
        const item = document.createElement('li'); item.textContent = text; document.getElementById('configurationRestoreItems').appendChild(item);
    }
    function showPreview(backup, result) {
        document.getElementById('configurationRestoreWarning').textContent = result.warning;
        addItem('Settings: ' + (result.settings || []).join(', '));
        addItem('Columns: ' + (result.columns || []).map(c => c.name).join(', '));
        (result.templates || []).forEach(template => {
            const wanted = backup.templates.find(t => Number(t.id) === template.id);
            const columns = new Map((wanted?.columns || []).map(c => [Number(c.id), c.name]));
            const labels = {amount:'Amount',quantity:'Quantity',rate:'Rate',account_code:'Account Code',account_name:'Account Name',debit:'Debit',credit:'Credit',description:'Description',line_id:'Line ID'};
            const bindings = wanted?.uploadBindings || {};
            const assignments = Object.entries(labels).filter(([role]) => Object.hasOwn(bindings, role))
                .map(([role,label]) => label + ': ' + columns.get(Number(bindings[role])));
            addItem('Template: ' + template.name + ' — ' + (assignments.join('; ') || 'No accounting field assignments'));
        });
        addItem('Saved header mappings: ' + (result.mappings || []).length);
        addItem('Holidays: ' + (result.holidays || []).map(h => h.label).join(', '));
        previewPanel.hidden = false;
    }
    exportButton.addEventListener('click', async () => {
        if (!allowed || busy) return; clearReview(); const token = generation; busy = true; buttons(); say('Exporting workspace configuration…');
        try {
            const result = await request('export'); if (token !== generation) return;
            const url = URL.createObjectURL(new Blob([result.text], {type:'application/json'}));
            const link = document.createElement('a'); link.href = url; link.download = 'workspace_configuration_' + new Date().toISOString().slice(0,10) + '.json';
            document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
            say('Configuration download started.', 'success');
        } catch (error) { if (token === generation) say(error.message, 'error'); }
        finally { busy = false; buttons(); }
    });
    selectButton.addEventListener('click', () => { if (allowed && !busy) fileInput.click(); });
    fileInput.addEventListener('change', async () => {
        const file = fileInput.files[0]; fileInput.value = ''; if (!file || !allowed || busy) return;
        clearReview(); const token = generation; busy = true; buttons(); say('Checking the selected configuration…');
        try {
            if (file.size > maximumBytes) throw new Error('Configuration backup exceeds the 7 MiB file limit.');
            const text = await file.text(); if (token !== generation) return;
            const document = JSON.parse(text);
            if (![1,2].includes(document.version) || !Array.isArray(document.columns) || !Array.isArray(document.templates))
                throw new Error('Choose a workspace configuration backup.');
            const result = await request('preview', text); if (token !== generation) return;
            if (typeof result.data.revision !== 'string' || !/^[0-9A-F]{64}$/.test(result.data.revision)) throw new Error('A fresh restore preview is required.');
            preview = {text, revision:result.data.revision}; showPreview(document, result.data);
            say('Review the listed configuration and confirm when ready.');
        } catch (error) { if (token === generation) say(error instanceof SyntaxError ? 'Choose a valid JSON configuration backup.' : error.message, 'error'); }
        finally { busy = false; buttons(); }
    });
    checkbox.addEventListener('change', buttons);
    cancelButton.addEventListener('click', () => { if (!busy) { clearReview(); say('Restore review cancelled.'); } });
    restoreButton.addEventListener('click', async () => {
        if (!allowed || busy || !preview || !checkbox.checked) return;
        const selected = preview, token = generation; preview = null; busy = true; buttons(); say('Restoring the confirmed configuration…');
        try {
            const body = '{"backup":' + selected.text + ',"revision":' + JSON.stringify(selected.revision) + ',"confirmed":true}';
            const result = await request('import', body); if (token !== generation) return;
            clearReview(); say(result.data.message || 'Configuration restored.', 'success'); await loadSetupStats();
        } catch (error) {
            if (token === generation) { clearReview(); say(error.message + ' Refresh the configuration and choose the file for a fresh preview before another restore.', 'error'); }
        } finally { busy = false; buttons(); }
    });
    window.addEventListener('pagehide', clearReview);
    say(!capability.enabled ? 'Configuration backup is unavailable.' : !capability.canEdit ? 'A workspace administrator can export and restore configuration.' : 'Ready to export or review a configuration backup.');
    buttons();
    if (new URLSearchParams(location.search).get('feature') === 'configuration-backup') document.getElementById('configurationBackupTitle').focus();
})();
