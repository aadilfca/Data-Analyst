// ============================================================
// Trial Balance module — Phase 4: Upload UI
// Lets the auditor upload a client's Trial Balance Excel file
// (with an as-of date), triggers the backend's upload+classify
// endpoint (Phase 2), then fetches and displays the classification
// summary (Phase 3). No per-account list/corrections here — that's
// a later phase.
// ============================================================

// Client selection is entirely handled by client_context.js: it reads/writes
// localStorage, renders the navbar client badge, and (on this non-dashboard
// page) redirects to the dashboard if no client is selected. This page only
// ever reads the current client id via window.getSelectedClientId().

let selectedFile = null;

// ---------- DOM HELPERS ----------
function getElement(id) {
    const el = document.getElementById(id);
    if (!el) console.warn('⚠️ Element not found:', id);
    return el;
}

const clientSelectionWarning = getElement('clientSelectionWarning');
const asOfDateInput = getElement('asOfDateInput');
const uploadFileBtn = getElement('uploadFileBtn');
const fileInput = getElement('fileInput');
const fileInfo = getElement('fileInfo');
const fileName = getElement('fileName');
const fileSize = getElement('fileSize');
const clearBtn = getElement('clearBtn');
const uploadTrialBalanceBtn = getElement('uploadTrialBalanceBtn');
const statusBar = getElement('statusBar');
const statusIcon = getElement('statusIcon');
const statusMessage = getElement('statusMessage');
const summaryContainer = getElement('summaryContainer');
const summaryTotalCount = getElement('summaryTotalCount');
const summaryLeafCount = getElement('summaryLeafCount');
const summaryParentCount = getElement('summaryParentCount');

// ---------- STATUS BAR ----------
// Same show/hide + color-coded classes convention as upload.js's showStatus().
function showStatus(msg, type = 'info') {
    if (!statusBar || !statusMessage) return;
    const icons = { info: 'ℹ️', success: '✅', error: '❌', loading: '⏳' };
    if (statusIcon) statusIcon.textContent = icons[type] || icons.info;
    statusMessage.textContent = msg;
    statusBar.className = 'status-bar ' + type;
    statusBar.style.display = 'flex';
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
}

// ---------- CLIENT-DEPENDENT UI ----------
// Enables the Upload & Classify button only once a client is selected, a file
// is chosen, and an as-of date is filled in. Client switching on this app
// always navigates away to the dashboard (confirmSwitchClient() in
// client_context.js), so this only needs to run on load and after each of
// the three inputs changes — no live cross-page client-change listener needed.
function updateClientDependentUI() {
    const hasClient = typeof window.isClientSelected === 'function' && window.isClientSelected();

    if (clientSelectionWarning) {
        clientSelectionWarning.style.display = hasClient ? 'none' : 'flex';
    }

    const hasFile = !!selectedFile;
    const hasDate = !!(asOfDateInput && asOfDateInput.value);

    if (uploadTrialBalanceBtn) {
        uploadTrialBalanceBtn.disabled = !(hasClient && hasFile && hasDate);
    }
}

// ---------- FILE SELECTION ----------
if (uploadFileBtn) {
    uploadFileBtn.addEventListener('click', function () {
        if (fileInput) fileInput.click();
    });
}

if (fileInput) {
    fileInput.addEventListener('change', function () {
        if (this.files && this.files.length > 0) {
            handleFileSelected(this.files[0]);
            this.value = ''; // reset so re-selecting the same file still fires 'change'
        }
    });
}

function handleFileSelected(file) {
    const validExtension = /\.(xlsx|xls)$/i.test(file.name);
    if (!validExtension) {
        showStatus('⚠️ Please select a valid Excel file (.xlsx or .xls)', 'error');
        return;
    }
    selectedFile = file;
    if (fileName) fileName.textContent = file.name;
    if (fileSize) fileSize.textContent = formatFileSize(file.size);
    if (fileInfo) fileInfo.style.display = 'flex';
    updateClientDependentUI();
}

if (clearBtn) {
    clearBtn.addEventListener('click', function () {
        selectedFile = null;
        if (fileInfo) fileInfo.style.display = 'none';
        if (fileName) fileName.textContent = '';
        if (fileSize) fileSize.textContent = '';
        updateClientDependentUI();
    });
}

if (asOfDateInput) {
    asOfDateInput.addEventListener('change', updateClientDependentUI);
}

// ---------- UPLOAD & CLASSIFY ----------
if (uploadTrialBalanceBtn) {
    uploadTrialBalanceBtn.addEventListener('click', uploadTrialBalance);
}

async function uploadTrialBalance() {
    // Defensive guard -- the button is disabled unless all three are present,
    // but re-check directly in case of a race (e.g. a stale click event).
    const clientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
    const asOfDate = asOfDateInput ? asOfDateInput.value : '';
    if (!clientId || !selectedFile || !asOfDate) {
        showStatus('⚠️ Select a client, an as-of date, and a file first.', 'error');
        return;
    }

    if (summaryContainer) summaryContainer.style.display = 'none';
    showStatus('Uploading and classifying...', 'loading');
    uploadTrialBalanceBtn.disabled = true;

    try {
        const formData = new FormData();
        formData.append('client_id', clientId);
        formData.append('as_of_date', asOfDate);
        formData.append('file', selectedFile);

        const res = await fetch(API_BASE + '/api/trial-balance/upload', {
            method: 'POST',
            body: formData
        });
        const result = await res.json();

        if (!res.ok) {
            throw new Error(result.detail || 'Upload failed');
        }

        showStatus('✅ Uploaded and classified successfully', 'success');
        await loadSummary(result.upload_id);
    } catch (error) {
        showStatus('❌ ' + error.message, 'error');
    } finally {
        updateClientDependentUI();
    }
}

// ---------- SUMMARY DISPLAY (Phase 3's read-only summary endpoint) ----------
async function loadSummary(uploadId) {
    try {
        const res = await fetch(API_BASE + '/api/trial-balance/summary?upload_id=' + uploadId);
        const result = await res.json();

        if (!res.ok) {
            throw new Error(result.detail || 'Failed to load summary');
        }

        if (summaryTotalCount) summaryTotalCount.textContent = result.total_count;
        if (summaryLeafCount) summaryLeafCount.textContent = result.leaf_count;
        if (summaryParentCount) summaryParentCount.textContent = result.parent_count;
        if (summaryContainer) summaryContainer.style.display = 'block';
    } catch (error) {
        // Don't overwrite the earlier upload-success status message -- the upload itself
        // already succeeded, only the summary re-fetch failed.
        console.warn('⚠️ Could not load Trial Balance summary:', error.message);
    }
}

// ---------- INIT ----------
window.onload = function () {
    updateClientDependentUI();
};
