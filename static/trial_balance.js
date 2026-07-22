// ============================================================
// Trial Balance module — Phase 5 of 5, FINAL: Review screen
// Lets the auditor upload a client's Trial Balance Excel file
// (with an as-of date), triggers the backend's upload+classify
// endpoint (Phase 2), fetches and displays the classification
// summary (Phase 3), and now (Phase 5) lists every classified
// account so the auditor can flip a misclassified row between
// Leaf/Parent via the new per-row override endpoint.
// ============================================================

// Client selection is entirely handled by client_context.js: it reads/writes
// localStorage, renders the navbar client badge, and (on this non-dashboard
// page) redirects to the dashboard if no client is selected. This page only
// ever reads the current client id via window.getSelectedClientId().

let selectedFile = null;

// Which upload's accounts are currently on screen in the review table -- needed so the
// row-toggle handler knows which upload's summary tiles to refresh after a flip.
let currentUploadId = null;

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
const reviewContainer = getElement('reviewContainer');
const reviewTableBody = getElement('reviewTableBody');

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
    if (reviewContainer) reviewContainer.style.display = 'none';
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
        await loadAccounts(result.upload_id);
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

// ---------- REVIEW TABLE (Phase 5: list + manual classification override) ----------

// Fetches every classified account for an upload (Phase 3's read-only list endpoint)
// and renders the review table. Same non-fatal-on-error convention as loadSummary() --
// the upload itself already succeeded, so a failure here shouldn't overwrite that status.
async function loadAccounts(uploadId) {
    try {
        const res = await fetch(API_BASE + '/api/trial-balance/accounts?upload_id=' + uploadId);
        const result = await res.json();

        if (!res.ok) {
            throw new Error(result.detail || 'Failed to load accounts');
        }

        currentUploadId = uploadId;
        renderReviewTable(result.accounts);
        if (reviewContainer) reviewContainer.style.display = 'block';
    } catch (error) {
        console.warn('⚠️ Could not load Trial Balance accounts:', error.message);
    }
}

// Builds one <tr> per account. Uses createElement/textContent throughout (no innerHTML)
// since account codes/names come from the uploaded file, not from this app's own control.
function renderReviewTable(accounts) {
    if (!reviewTableBody) return;
    reviewTableBody.innerHTML = '';

    accounts.forEach(function (account) {
        const tr = document.createElement('tr');
        tr.dataset.accountId = account.id;

        const tdCode = document.createElement('td');
        tdCode.textContent = account.account_code;
        tr.appendChild(tdCode);

        const tdName = document.createElement('td');
        tdName.textContent = account.account_name || '—';
        tr.appendChild(tdName);

        const tdLevel = document.createElement('td');
        tdLevel.textContent = account.level;
        tr.appendChild(tdLevel);

        const tdClassification = document.createElement('td');
        tdClassification.appendChild(buildClassificationCellContent(account));
        tr.appendChild(tdClassification);

        const tdAction = document.createElement('td');
        tdAction.appendChild(buildToggleButton(account));
        tr.appendChild(tdAction);

        reviewTableBody.appendChild(tr);
    });
}

// Badge + optional "overridden" tag, grouped in one fragment so both the initial render
// and the in-place row update (after a toggle) can build/replace this cell's content identically.
function buildClassificationCellContent(account) {
    const fragment = document.createDocumentFragment();

    const badge = document.createElement('span');
    badge.className = 'tb-classification-badge ' + (account.is_leaf ? 'leaf' : 'parent');
    badge.textContent = account.is_leaf ? 'Leaf' : 'Parent';
    fragment.appendChild(badge);

    if (account.manually_overridden) {
        const tag = document.createElement('span');
        tag.className = 'tb-overridden-tag';
        tag.textContent = 'Overridden';
        fragment.appendChild(tag);
    }

    return fragment;
}

function buildToggleButton(account) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'tb-toggle-btn';
    btn.dataset.accountId = account.id;
    btn.dataset.currentIsLeaf = account.is_leaf;
    btn.textContent = account.is_leaf ? 'Flip to Parent' : 'Flip to Leaf';
    return btn;
}

// Single delegated click listener on the table body (added once, not per-row) -- reads the
// clicked button's data attributes rather than each row carrying its own listener.
if (reviewTableBody) {
    reviewTableBody.addEventListener('click', function (event) {
        const btn = event.target.closest('.tb-toggle-btn');
        if (!btn) return;

        const accountId = btn.dataset.accountId;
        const currentIsLeaf = btn.dataset.currentIsLeaf === 'true';
        toggleAccountClassification(accountId, !currentIsLeaf);
    });
}

// PUTs the new classification to the Phase 5 override endpoint, then updates just that
// row in place (no full table re-render) and refreshes the summary tiles so the
// Leaf/Parent counts stay correct after the flip.
async function toggleAccountClassification(accountId, newIsLeaf) {
    try {
        const res = await fetch(API_BASE + '/api/trial-balance/accounts/' + accountId + '/override', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_leaf: newIsLeaf })
        });
        const result = await res.json();

        if (!res.ok) {
            throw new Error(result.detail || 'Failed to update classification');
        }

        const row = reviewTableBody.querySelector('tr[data-account-id="' + accountId + '"]');
        if (row) {
            const tdClassification = row.children[3];
            tdClassification.innerHTML = '';
            tdClassification.appendChild(buildClassificationCellContent(result));

            const tdAction = row.children[4];
            tdAction.innerHTML = '';
            tdAction.appendChild(buildToggleButton(result));
        }

        if (currentUploadId) await loadSummary(currentUploadId);
    } catch (error) {
        showStatus('❌ ' + error.message, 'error');
    }
}

// ---------- INIT ----------
window.onload = function () {
    updateClientDependentUI();
};
