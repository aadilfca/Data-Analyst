// ============================================================
// Trial Balance module — Phase 5 of 5, FINAL: Review screen
// Lets the auditor upload a client's Trial Balance Excel file
// (with an as-of date), triggers the backend's upload+classify
// endpoint (Phase 2), fetches and displays the classification
// summary (Phase 3), and now (Phase 5) lists every classified
// account so the auditor can flip a misclassified row between
// Leaf/Parent via the new per-row override endpoint.
//
// Column mapping & standard template, Section 1, Phase 2 (standard 8-field fix):
// once a file is selected, suggest-mapping (Phase 1 backend) is called to
// pre-fill a dropdown-per-field mapping table -- Account Code/Name plus the
// standard 3 Dr/Cr pairs (Opening, Period, Closing) -- with a small live
// preview of the file's first few rows alongside it so the reviewer can
// visually confirm each column choice before uploading.
// ============================================================

// Client selection is entirely handled by client_context.js: it reads/writes
// localStorage, renders the navbar client badge, and (on this non-dashboard
// page) redirects to the dashboard if no client is selected. This page only
// ever reads the current client id via window.getSelectedClientId().

let selectedFile = null;

// Which upload's accounts are currently on screen in the review table -- needed so the
// row-toggle handler knows which upload's summary tiles to refresh after a flip.
let currentUploadId = null;

// ---------- PERIOD TRACKING + OVERWRITE CONFLICT STATE (Section 2, Phase 6) ----------
// Caches the outcome of the last check-period-conflict call so the exact same period
// pair isn't re-checked/re-prompted twice in a row (e.g. once on file selection, again
// on the actual Upload & Classify click, when nothing changed in between). Reset to
// null whenever either period date changes, so a stale confirmation from a different
// period pair can never be reused.
let lastConflictCheckPeriod = null; // { start, end } the cached result below applies to
let lastConflictCheckResult = null; // 'no_conflict' | 'confirmed' | 'declined' | 'error'

// ---------- COLUMN MAPPING STATE (Section 1, Phase 2 -- standard 8-field fix) ----------
// The 8 required mapping fields this UI surfaces, matching the standard trial
// balance shape (Opening/Period/Closing Dr+Cr pairs). Type is supported by the
// backend as an optional role but isn't part of the standard 8, so it's simply
// never sent (the backend treats it as Optional[str] = Form(None), so omitting
// it is a no-op).
const TB_MAPPING_FIELDS = [
    { role: 'account_code_column', label: 'Account Code' },
    { role: 'account_name_column', label: 'Account Name' },
    { role: 'opening_dr_column', label: 'Opening Dr' },
    { role: 'opening_cr_column', label: 'Opening Cr' },
    { role: 'period_dr_column', label: 'Period Dr' },
    { role: 'period_cr_column', label: 'Period Cr' },
    { role: 'closing_dr_column', label: 'Closing Dr' },
    { role: 'closing_cr_column', label: 'Closing Cr' },
];

// The file's real header list, from suggest-mapping's response -- this is the single
// source of truth for both the mapping dropdowns' options and the preview table's
// column labels, since it's exactly what the upload endpoint will validate against.
let fileHeaders = [];

// First 5 data rows (arrays, header row already dropped), from a client-side SheetJS
// parse of the same file -- matched to fileHeaders purely by column index.
let filePreviewRows = [];

// Current mapping selection, keyed by role -- seeded from suggest-mapping's
// suggestions, then mutated in place by each dropdown's change listener.
let tbColumnMapping = {
    account_code_column: null, account_name_column: null,
    opening_dr_column: null, opening_cr_column: null,
    period_dr_column: null, period_cr_column: null,
    closing_dr_column: null, closing_cr_column: null,
};

// Bumped on every new file selection so a fast re-select can't let a slower, stale
// suggest-mapping/preview-parse response overwrite state from the newer file
// (same stale-response-guard convention used elsewhere in this app, e.g.
// Verification's audit guidance panel).
let mappingRequestToken = 0;

// ---------- DOM HELPERS ----------
function getElement(id) {
    const el = document.getElementById(id);
    if (!el) console.warn('⚠️ Element not found:', id);
    return el;
}

const clientSelectionWarning = getElement('clientSelectionWarning');
const periodStartInput = getElement('periodStartInput');
const periodEndInput = getElement('periodEndInput');
const periodTypeSelect = getElement('periodTypeSelect');
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
const mappingSection = getElement('mappingSection');
const mappingValidationMessage = getElement('mappingValidationMessage');
const mappingValidationText = getElement('mappingValidationText');
const mappingTableBody = getElement('mappingTableBody');
const mappingPreviewHead = getElement('mappingPreviewHead');
const mappingPreviewBody = getElement('mappingPreviewBody');
const downloadStandardTemplateBtn = getElement('downloadStandardTemplateBtn');

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
// is chosen, and Period Start/End are filled in (Period End's value is what gets
// sent to the backend as as_of_date -- the old standalone As of Date field is gone,
// so both period dates are now the required date source). Client switching on this
// app always navigates away to the dashboard (confirmSwitchClient() in
// client_context.js), so this only needs to run on load and after each of
// the relevant inputs changes — no live cross-page client-change listener needed.
function updateClientDependentUI() {
    const hasClient = typeof window.isClientSelected === 'function' && window.isClientSelected();

    if (clientSelectionWarning) {
        clientSelectionWarning.style.display = hasClient ? 'none' : 'flex';
    }

    const hasFile = !!selectedFile;
    const hasPeriod = !!(periodStartInput && periodEndInput && periodStartInput.value && periodEndInput.value);
    // A file with no headers yet (still loading, or suggest-mapping failed) can never
    // be considered mapped -- checkMappingComplete() alone would incorrectly pass on
    // an all-null tbColumnMapping before fileHeaders has ever been populated.
    const hasMapping = fileHeaders.length > 0 && checkMappingComplete();

    if (uploadTrialBalanceBtn) {
        uploadTrialBalanceBtn.disabled = !(hasClient && hasFile && hasPeriod && hasMapping);
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
    loadColumnMappingForFile(file);

    // Section 2, Phase 6: early heads-up -- if a period is already entered when the
    // file is chosen, check now rather than waiting for Upload & Classify. This is a
    // warning only: a 'declined'/'error' outcome here does NOT touch the file
    // selection or the column mapping already loading above -- the brief's own
    // instruction is that the form stays as-is so the auditor can adjust just the
    // period. The authoritative, final gate is in uploadTrialBalance() below.
    const clientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
    if (clientId && periodStartInput && periodEndInput && periodStartInput.value && periodEndInput.value) {
        checkAndConfirmPeriodOverwrite(clientId, periodStartInput.value, periodEndInput.value)
            .then(function (result) {
                if (result === 'declined') {
                    showStatus('⚠️ An upload already exists for this exact period. Adjust the period dates above, or click Upload & Classify when ready to confirm the overwrite.', 'error');
                }
            });
    }
}

// Resets the 3 column-mapping state vars and hides the mapping section -- shared by
// the Clear button and by loadColumnMappingForFile()'s own error path, so a failed
// suggest-mapping call never leaves stale mapping state visible for the wrong file.
function resetColumnMapping() {
    fileHeaders = [];
    filePreviewRows = [];
    tbColumnMapping = {
        account_code_column: null, account_name_column: null,
        opening_dr_column: null, opening_cr_column: null,
        period_dr_column: null, period_cr_column: null,
        closing_dr_column: null, closing_cr_column: null,
    };
    if (mappingSection) mappingSection.style.display = 'none';
    if (mappingValidationMessage) mappingValidationMessage.style.display = 'none';
}

if (clearBtn) {
    clearBtn.addEventListener('click', function () {
        selectedFile = null;
        if (fileInfo) fileInfo.style.display = 'none';
        if (fileName) fileName.textContent = '';
        if (fileSize) fileSize.textContent = '';
        resetColumnMapping();
        updateClientDependentUI();
    });
}

// ---------- PERIOD TYPE AUTO-SUGGESTION (Section 2, Phase 6) ----------
// Buckets the day-span between Period Start/End into the 4 standard period types.
// Purely a suggestion -- periodTypeSelect stays a normal, always-editable <select>,
// an auditor can pick a different value afterward and nothing here locks it.
function suggestPeriodType(startStr, endStr) {
    const start = new Date(startStr);
    const end = new Date(endStr);
    const diffDays = Math.round((end - start) / 86400000);
    if (diffDays <= 40) return 'monthly';
    if (diffDays <= 100) return 'quarterly';
    if (diffDays <= 200) return 'half_yearly';
    return 'annual';
}

function updatePeriodTypeSuggestion() {
    if (!periodStartInput || !periodEndInput || !periodTypeSelect) return;
    if (!periodStartInput.value || !periodEndInput.value) return; // nothing to suggest yet
    periodTypeSelect.value = suggestPeriodType(periodStartInput.value, periodEndInput.value);
}

// Any period-date edit invalidates a previously cached conflict-check result -- it
// applied to a different period pair and must not be silently reused. Also re-checks
// button enablement, since Period Start/End are now the required date source that
// updateClientDependentUI() gates on (previously this reactivity came from the old
// standalone As of Date field's own change listener).
function handlePeriodDateChanged() {
    updatePeriodTypeSuggestion();
    lastConflictCheckPeriod = null;
    lastConflictCheckResult = null;
    updateClientDependentUI();
}

if (periodStartInput) {
    periodStartInput.addEventListener('change', handlePeriodDateChanged);
}
if (periodEndInput) {
    periodEndInput.addEventListener('change', handlePeriodDateChanged);
}

// ---------- OVERWRITE-CONFLICT CHECK + CONFIRMATION (Section 2, Phase 6) ----------
// Shared by both the early "on file selection" heads-up and the authoritative check
// right before the actual upload request. Uses Phase 5's read-only check endpoint --
// this call never touches any data itself.
async function checkAndConfirmPeriodOverwrite(clientId, periodStart, periodEnd) {
    // Reuse a cached result for the exact same period pair ONLY when it was a settled,
    // safe-to-proceed outcome ('no_conflict'/'confirmed') -- a 'declined'/'error' result
    // must never be silently reused, since nothing has actually been confirmed for this
    // period yet; every later attempt (re-selecting the file, clicking Upload &
    // Classify) should re-prompt fresh until the auditor actually confirms or the
    // period changes.
    if (lastConflictCheckPeriod &&
        lastConflictCheckPeriod.start === periodStart &&
        lastConflictCheckPeriod.end === periodEnd &&
        (lastConflictCheckResult === 'no_conflict' || lastConflictCheckResult === 'confirmed')) {
        return lastConflictCheckResult;
    }

    let result;
    try {
        const url = API_BASE + '/api/trial-balance/check-period-conflict'
            + '?client_id=' + encodeURIComponent(clientId)
            + '&period_start=' + encodeURIComponent(periodStart)
            + '&period_end=' + encodeURIComponent(periodEnd);
        const res = await fetch(url);
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || 'Failed to check for a conflicting upload');
        }

        if (!data.conflict) {
            result = 'no_conflict';
        } else {
            const existing = data.existing_upload;
            // Deliberately explicit, non-softened wording -- matches the confirmed
            // delete-then-insert behaviour built in Phase 5, not a vague "replace".
            const message =
                'An upload already exists for this client for the exact period ' +
                existing.period_start + ' to ' + existing.period_end + ' ' +
                '(uploaded ' + existing.uploaded_at + ', ' + existing.row_count + ' accounts).\n\n' +
                'Confirming will PERMANENTLY DELETE that prior upload and all of its account ' +
                'classifications -- including any manual Leaf/Parent overrides made on it -- ' +
                'before creating the new one. This cannot be undone.\n\n' +
                'Click OK to overwrite it, or Cancel to go back and adjust the period.';
            result = window.confirm(message) ? 'confirmed' : 'declined';
        }
    } catch (error) {
        showStatus('❌ Could not check for a conflicting upload: ' + error.message, 'error');
        result = 'error';
    }

    lastConflictCheckPeriod = { start: periodStart, end: periodEnd };
    lastConflictCheckResult = result;
    return result;
}

// ---------- COLUMN MAPPING (Section 1, Phase 2) ----------

// Section 1, Phase 3: escape-hatch standard-template download -- opens the new
// backend endpoint in a new tab (same window.open() pattern static/upload.js's
// own Download Template button already uses), so it never disturbs whatever
// file/mapping state is already in progress on this page.
if (downloadStandardTemplateBtn) {
    downloadStandardTemplateBtn.addEventListener('click', function () {
        window.open(API_BASE + '/api/trial-balance/download-template', '_blank');
    });
}

// POSTs the raw file to Phase 1's suggest-mapping endpoint and returns its
// {headers, suggestions} shape. Same throw-with-detail convention as every other
// fetch call in this file.
async function suggestColumnMapping(file) {
    const formData = new FormData();
    formData.append('file', file);

    const res = await fetch(API_BASE + '/api/trial-balance/suggest-mapping', {
        method: 'POST',
        body: formData
    });
    const result = await res.json();

    if (!res.ok) {
        throw new Error(result.detail || 'Failed to read file for mapping suggestions');
    }
    return result;
}

// Reads the selected file client-side via SheetJS to get its first sheet's raw rows
// (array of arrays, header row included at index 0) -- used only for the preview
// table's cell values; the header labels themselves come from suggestColumnMapping()
// instead (see the Header list authority design note in this phase's plan).
function readFileRowsForPreview(file) {
    return new Promise(function (resolve, reject) {
        const reader = new FileReader();
        reader.onload = function (e) {
            try {
                const data = new Uint8Array(e.target.result);
                const workbook = XLSX.read(data, { type: 'array' });
                const sheet = workbook.Sheets[workbook.SheetNames[0]];
                const rows = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: '' });
                resolve(rows);
            } catch (err) {
                reject(err);
            }
        };
        reader.onerror = function () {
            reject(new Error('Could not read the file'));
        };
        reader.readAsArrayBuffer(file);
    });
}

// Runs suggest-mapping and the client-side preview parse in parallel for a newly
// selected file, seeds tbColumnMapping from the suggestions, and renders both the
// mapping dropdowns and the preview table. Guarded by mappingRequestToken so a fast
// re-select of a different file can't let this call's (now stale) result overwrite
// state belonging to the newer file.
async function loadColumnMappingForFile(file) {
    const token = ++mappingRequestToken;
    showStatus('Reading file and suggesting column mapping...', 'loading');

    try {
        const results = await Promise.all([
            suggestColumnMapping(file),
            readFileRowsForPreview(file)
        ]);
        if (token !== mappingRequestToken) return; // a newer file was selected meanwhile

        const suggestResult = results[0];
        const rawRows = results[1];

        fileHeaders = suggestResult.headers || [];
        filePreviewRows = rawRows.slice(1, 6); // first 5 data rows, header row dropped
        tbColumnMapping = {
            account_code_column: suggestResult.suggestions.account_code_column || null,
            account_name_column: suggestResult.suggestions.account_name_column || null,
            opening_dr_column: suggestResult.suggestions.opening_dr_column || null,
            opening_cr_column: suggestResult.suggestions.opening_cr_column || null,
            period_dr_column: suggestResult.suggestions.period_dr_column || null,
            period_cr_column: suggestResult.suggestions.period_cr_column || null,
            closing_dr_column: suggestResult.suggestions.closing_dr_column || null,
            closing_cr_column: suggestResult.suggestions.closing_cr_column || null,
        };

        renderMappingDropdowns();
        renderMappingPreviewTable();
        checkMappingComplete();
        if (mappingSection) mappingSection.style.display = 'block';
        showStatus('✅ Column mapping suggested -- review and correct below if needed.', 'success');
    } catch (error) {
        if (token !== mappingRequestToken) return;
        showStatus('❌ Could not read file for column mapping: ' + error.message, 'error');
        resetColumnMapping();
    } finally {
        if (token === mappingRequestToken) {
            updateClientDependentUI();
        }
    }
}

// Builds one <tr> per TB_MAPPING_FIELDS entry: field label, a <select> of fileHeaders
// (excluding whatever's already chosen by another role), and a ✅/❌ status cell.
// Same interaction pattern as the generic Upload module's mapping table
// (static/upload.js's renderMapping()) -- full re-render on any change so every row's
// exclusion list and status stay in sync.
function renderMappingDropdowns() {
    if (!mappingTableBody) return;
    mappingTableBody.innerHTML = '';

    TB_MAPPING_FIELDS.forEach(function (field) {
        const tr = document.createElement('tr');

        const tdLabel = document.createElement('td');
        tdLabel.textContent = field.label;
        tr.appendChild(tdLabel);

        const tdSelect = document.createElement('td');
        const select = document.createElement('select');
        select.dataset.role = field.role;

        const blankOption = document.createElement('option');
        blankOption.value = '';
        blankOption.textContent = '— Select column —';
        select.appendChild(blankOption);

        const selectedElsewhere = TB_MAPPING_FIELDS
            .filter(function (f) { return f.role !== field.role; })
            .map(function (f) { return tbColumnMapping[f.role]; })
            .filter(Boolean);

        fileHeaders.forEach(function (header) {
            if (selectedElsewhere.indexOf(header) !== -1 && header !== tbColumnMapping[field.role]) {
                return; // already claimed by another field's dropdown
            }
            const option = document.createElement('option');
            option.value = header;
            option.textContent = header;
            if (header === tbColumnMapping[field.role]) option.selected = true;
            select.appendChild(option);
        });

        select.addEventListener('change', function () {
            tbColumnMapping[field.role] = this.value || null;
            renderMappingDropdowns(); // full re-render so other rows' exclusions/status update
            checkMappingComplete();
            updateClientDependentUI();
        });

        tdSelect.appendChild(select);
        tr.appendChild(tdSelect);

        const tdStatus = document.createElement('td');
        tdStatus.textContent = tbColumnMapping[field.role] ? '✅' : '❌';
        tr.appendChild(tdStatus);

        mappingTableBody.appendChild(tr);
    });
}

// Renders the small "first 5 rows" preview: column labels from fileHeaders (the same
// list the mapping dropdowns use), cell values from filePreviewRows indexed by
// column position -- not by re-matching header text.
function renderMappingPreviewTable() {
    if (!mappingPreviewHead || !mappingPreviewBody) return;
    mappingPreviewHead.innerHTML = '';
    mappingPreviewBody.innerHTML = '';

    const headerRow = document.createElement('tr');
    fileHeaders.forEach(function (header) {
        const th = document.createElement('th');
        th.textContent = header;
        headerRow.appendChild(th);
    });
    mappingPreviewHead.appendChild(headerRow);

    filePreviewRows.forEach(function (rowArray) {
        const tr = document.createElement('tr');
        fileHeaders.forEach(function (_header, idx) {
            const td = document.createElement('td');
            const value = rowArray[idx];
            td.textContent = value !== undefined ? String(value) : '';
            tr.appendChild(td);
        });
        mappingPreviewBody.appendChild(tr);
    });
}

// Checks all 4 required fields are mapped; shows/hides the inline validation message
// naming every still-missing field (a frontend mirror of Phase 1's backend
// validate_column_mapping() -- the backend remains the authoritative check on the
// actual upload call, this is just an earlier, clearer signal to the user).
function checkMappingComplete() {
    const missing = TB_MAPPING_FIELDS
        .filter(function (field) { return !tbColumnMapping[field.role]; })
        .map(function (field) { return field.label; });

    if (mappingValidationMessage && mappingValidationText) {
        if (missing.length === 0) {
            mappingValidationMessage.style.display = 'none';
        } else {
            mappingValidationText.textContent = 'Please map the following required field(s) before uploading: ' + missing.join(', ') + '.';
            mappingValidationMessage.style.display = 'flex';
        }
    }
    return missing.length === 0;
}

// ---------- UPLOAD & CLASSIFY ----------
if (uploadTrialBalanceBtn) {
    uploadTrialBalanceBtn.addEventListener('click', uploadTrialBalance);
}

async function uploadTrialBalance() {
    // Defensive guard -- the button is disabled unless all of these are present,
    // but re-check directly in case of a race (e.g. a stale click event). Period
    // Start/End are now required (not optional) since the old standalone As of Date
    // field is gone and Period End's value is what actually gets sent as as_of_date.
    const clientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
    const hasPeriod = !!(periodStartInput && periodEndInput && periodStartInput.value && periodEndInput.value);
    if (!clientId || !selectedFile || !hasPeriod) {
        showStatus('⚠️ Select a client, a Period Start/End, and a file first.', 'error');
        return;
    }
    // Defensive re-check of the column mapping too, same reasoning as the guard above --
    // the button is disabled unless mapping is complete, but re-verify directly in case
    // of a race (e.g. a stale click event from before the last dropdown change).
    if (!checkMappingComplete()) {
        return;
    }

    // Section 2, Phase 6: authoritative overwrite-conflict gate. This re-checks (or
    // reuses a still-valid cached result) right before the actual request -- catches
    // a period entered/changed after file selection, and re-prompts if an earlier
    // check on this exact period was declined or failed. Period is now always present
    // by this point (guarded above), so this check always runs.
    const conflictResult = await checkAndConfirmPeriodOverwrite(clientId, periodStartInput.value, periodEndInput.value);
    if (conflictResult === 'declined' || conflictResult === 'error') {
        // No request sent -- the form stays exactly as the auditor left it so
        // they can adjust the period (or try again) themselves.
        showStatus('Upload cancelled.', 'info');
        return;
    }
    const confirmOverwrite = conflictResult === 'confirmed';

    if (summaryContainer) summaryContainer.style.display = 'none';
    if (reviewContainer) reviewContainer.style.display = 'none';
    showStatus('Uploading and classifying...', 'loading');
    uploadTrialBalanceBtn.disabled = true;

    try {
        const formData = new FormData();
        formData.append('client_id', clientId);
        // The backend's as_of_date field still exists (not touched this phase) -- Period
        // End's value fills it now that the old standalone As of Date input is gone.
        formData.append('as_of_date', periodEndInput.value);
        formData.append('file', selectedFile);
        // Section 1, Phase 2: send the confirmed column mapping alongside the file --
        // the backend (Phase 1) validates this same payload again on its own.
        TB_MAPPING_FIELDS.forEach(function (field) {
            formData.append(field.role, tbColumnMapping[field.role]);
        });
        // Section 2, Phase 6: period fields are always sent now (hasPeriod is guaranteed
        // true by the guard above). confirm_overwrite is always sent explicitly so the
        // backend's default (false) is never relied on silently.
        formData.append('period_start', periodStartInput.value);
        formData.append('period_end', periodEndInput.value);
        formData.append('period_type', periodTypeSelect ? periodTypeSelect.value : '');
        formData.append('confirm_overwrite', confirmOverwrite ? 'true' : 'false');

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
