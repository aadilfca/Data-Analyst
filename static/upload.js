// ================================================================
// 📤 UPLOAD MODULE – FINAL
// ================================================================

// API_BASE is defined globally in client_context.js

// ---------- DOM REFS ----------
function getElement(id) {
    const el = document.getElementById(id);
    if (!el) console.warn('⚠️ Element not found:', id);
    return el;
}

const templateDropdown = getElement('templateDropdown');
const sheetSelector = getElement('sheetSelector');
const sheetField = getElement('sheetField');
const statusBadge = getElement('statusBadge');
const dateDelimiterDropdown = getElement('dateDelimiterDropdown');
const dateOrderDropdown = getElement('dateOrderDropdown');
const dateFormatGroup = getElement('dateFormatGroup');
const dateFormatTriggerBtn = getElement('dateFormatTriggerBtn');
const dateFormatTriggerLabel = getElement('dateFormatTriggerLabel');
const dateFormatPopover = getElement('dateFormatPopover');

const uploadFileBtn = getElement('uploadFileBtn');
const fileInput = getElement('fileInput');
if (!fileInput) {
    console.error('❌ File input element with id "fileInput" not found – upload will not work');
}
const fileInfo = getElement('fileInfo');
const fileName = getElement('fileName');
const fileSize = getElement('fileSize');
const clearBtn = getElement('clearBtn');

const leftPanel = getElement('leftPanel');
const mappingPanel = getElement('mappingPanel');
const mappingBody = getElement('mappingBody');
const mappingCount = getElement('mappingCount');
const saveMappingBtn = getElement('saveMappingBtn');
const clearMappingBtn = getElement('clearMappingBtn');

const modeActions = getElement('modeActions');

const downloadBtn = getElement('downloadBtn');
const importBtn = getElement('importBtn');
const resetBtn = getElement('resetBtn');

const statusBar = getElement('statusBar');
const statusMessage = getElement('statusMessage');
const statusExtra = getElement('statusExtra');

const footerMeta = getElement('footerMeta');
const footerFile = getElement('footerFile');
const footerRows = getElement('footerRows');
const footerDate = getElement('footerDate');

// ---------- DATE FORMAT OPTIONS ----------
// Order codes (DMY/MDY/YMD) match the backend's parse_date_flexible() `order` param
// (routers/import_export.py) so Phase 4 can wire these straight through.
const DATE_ORDER_OPTIONS = {
    '/': [['DMY', 'DD/MM/YYYY'], ['MDY', 'MM/DD/YYYY'], ['YMD', 'YYYY/MM/DD']],
    '-': [['DMY', 'DD-MM-YYYY'], ['MDY', 'MM-DD-YYYY'], ['YMD', 'YYYY-MM-DD']],
    '': [['DMY', 'DDMMYYYY'], ['MDY', 'MMDDYYYY'], ['YMD', 'YYYYMMDD']]
};

// ---------- STATE ----------
let selectedTemplateId = null;
let selectedDateDelimiter = '/';
let selectedDateOrder = 'DMY';
let selectedFile = null;
let fileId = null;
let currentMappings = [];
let excelHeaders = [];
let templateColumns = [];
let sheetNames = [];
let currentSheet = null;
let workbookData = null;
let isProcessing = false;
let validationStatus = 'idle'; // 'idle' | 'validating' | 'valid' | 'invalid'
// Pre-import Rate mode visibility (Phase D of the Rate-derivation requirement) --
// the validate response's rate_mode field ({template_setting, effective_source, label}),
// or null for a non-eligible template. Only ever trustworthy alongside a 'valid'
// validationStatus -- cleared in lockstep with it by resetValidationState().
let lastRateModeInfo = null;

// Pagination state variables
let previewCurrentPage = 1;
let previewPageSize = 50;
let previewTotalRows = 0;
let previewAllRows = [];
let previewHeaders = [];
let previewHiddenCount = 0; // rows hidden from the preview because the user excluded them

// ---------- ROW SELECTION GRID STATE (Phase 2 of the row-exclusion requirement) ----------
// excludedRowNumbers holds Excel row numbers (header row=1, first data row=2 -- same
// scheme routers/import_export.py's excluded_rows/validation_errors[].row_number use)
// that the user has unchecked. Empty = nothing excluded (all rows included), matching
// the "checked by default" requirement.
let excludedRowNumbers = new Set();
let rowExclusionAllRows = [];   // current sheet's data rows (raw arrays), same shape as previewAllRows
let rowExclusionHeaders = [];
let rowExclusionCurrentPage = 1;
const ROW_EXCLUSION_PAGE_SIZE = 10; // fixed page size for this compact inline grid

// validationFailedRows (Phase 4): Excel row number -> that row's validation `errors` array
// (same {column, excel_column, error_type, message} shape showValidationErrors() renders).
// Distinct from excludedRowNumbers -- a validation failure never auto-excludes a row.
let validationFailedRows = new Map();

// ---------- REJECTED-ROWS EXCEPTIONS STATE (Phase 3 of the row-level blank-field-rejection
// requirement) ----------
// rejectedRowsData: Excel row number -> that row's `reasons` array (routers/import_export.py's
// rejected_rows[].reasons -- {role, column, excel_column, message}), for rows the backend
// rejected because a Document ID/Amount/Date was blank. Kept separate from validationFailedRows
// (which covers genuine data-type mismatches) -- a rejected-for-blank-critical-field row is its
// own category, not a validation error.
let rejectedRowsData = new Map();
// rejectedRowOverrides: Excel row numbers the user has explicitly toggled "Import anyway" for.
// This phase is state-only -- toggling does not yet change what's sent to the backend (Phase 4).
let rejectedRowOverrides = new Set();

// ---------- SESSION-RESTORE STATE (mid-upload navigate-away/back recovery) ----------
// A browser <input type="file"> can never be repopulated by JS, so the raw file itself is not
// restorable -- only Template/Sheet-name/Date-format/Row-exclusion selections are. These two
// "pending" values hold a restored session's Sheet name and excluded-row numbers until the user
// re-selects a file (handleFile()) and the mapping/row-exclusion grid rebuild for it, at which
// point each is consumed once and cleared. See saveUploadSessionState()/restoreUploadSessionState()
// further down.
const UPLOAD_SESSION_STORAGE_KEY = 'uploadPageState';
let pendingRestoreSheetName = null;
let pendingExcludedRowNumbersToRestore = null;

// ---------- STEP INDICATOR / SHEET-DOWNLOAD REVEAL (Phase 2 of the Upload header reorg) ----------
// Download Template + Sheet selector are gated together behind Template selection instead of
// Download always being visible (see #sheetDownloadGroup in upload.html). Single place both
// reveal and hide go through.
function setSheetDownloadGroupVisible(visible) {
    const group = document.getElementById('sheetDownloadGroup');
    if (group) group.style.display = visible ? 'flex' : 'none';
}

// Go to Data Bank (Phase 4) -- stays visually inert until Template -> Sheet -> mapping all
// resolve; navigation behavior in the click handler (window.onload, further down) is unchanged.
function updateGoToDataBankState(enabled) {
    const btn = document.getElementById('goToDataBankBtn');
    if (btn) btn.disabled = !enabled;
}

// Moves the step-indicator strip's .active class between step-items (data-step attribute,
// added in Phase 1's markup). Only steps 1/2 are used this phase -- steps 3/4 stay neutral,
// their activation is Phases 3-4.
function updateStepIndicator(step) {
    document.querySelectorAll('#uploadStepIndicator .step-item').forEach(item => {
        item.classList.toggle('active', item.dataset.step === String(step));
    });
}

// ---------- SESSION-RESTORE: SAVE / RESTORE / CLEAR ----------
// Persists just enough of this page's in-memory state to sessionStorage (tab-scoped only, no
// backend change) so that navigating away mid-upload (e.g. "Go to Data Bank") and back (e.g.
// "Upload More") restores Template/Sheet/Date-format/Row-exclusion selections automatically and
// silently on page load -- no confirmation dialog. The uploaded file's raw bytes cannot be
// restored this way (browser <input type="file"> elements can't be repopulated by JS, and there
// is no backend endpoint that serves the original file content back), so `selectedFile.name` is
// saved purely to tell the user which file to re-select via the existing status bar.
function saveUploadSessionState() {
    // Nothing meaningful to persist until a template has been chosen.
    if (!selectedTemplateId) return;
    const state = {
        templateId: selectedTemplateId,
        sheetName: currentSheet,
        fileName: selectedFile ? selectedFile.name : null,
        dateDelimiter: selectedDateDelimiter,
        dateOrder: selectedDateOrder,
        excludedRowNumbers: Array.from(excludedRowNumbers),
        // Lightweight snapshot for the read-only "locked preview" a restored session shows
        // before the file is re-selected (renderRestorePreview()) -- just the column-name/type/
        // matched-status fields the preview table needs, not the full mapping objects, and row
        // counts rather than the raw rows themselves (which are never persisted -- too large,
        // and only ever exist client-side after reading the actual file).
        mappings: currentMappings.map(m => ({
            column_name: m.column_name,
            excel_column: m.excel_column,
            data_type: m.data_type,
            is_matched: m.is_matched
        })),
        totalRows: rowExclusionAllRows.length,
        excludedCount: excludedRowNumbers.size
    };
    try {
        sessionStorage.setItem(UPLOAD_SESSION_STORAGE_KEY, JSON.stringify(state));
    } catch (error) {
        console.warn('⚠️ Could not save upload session state:', error);
    }
}

function clearUploadSessionState() {
    sessionStorage.removeItem(UPLOAD_SESSION_STORAGE_KEY);
    pendingRestoreSheetName = null;
    pendingExcludedRowNumbersToRestore = null;
}

// Called once from window.onload, after loadTemplates() resolves (the Template dropdown's
// <option>s must exist before its value can be set). Restores Date format immediately (it's
// file-independent); Template selection is restored by awaiting selectTemplate() directly (the
// same function the dropdown's own 'change' listener calls) rather than dispatching a synthetic
// event, so the "resuming session" status message below is guaranteed to show only after
// selectTemplate()'s own async status updates have settled, not raced/overwritten by them. Sheet
// name and row-exclusion selections are stashed as "pending" until the user re-selects a file
// (see handleFile()/renderRowExclusionGrid()).
async function restoreUploadSessionState() {
    let state;
    try {
        const raw = sessionStorage.getItem(UPLOAD_SESSION_STORAGE_KEY);
        if (!raw) return;
        state = JSON.parse(raw);
    } catch (error) {
        console.warn('⚠️ Could not read upload session state:', error);
        sessionStorage.removeItem(UPLOAD_SESSION_STORAGE_KEY);
        return;
    }
    if (!state || !state.templateId) return;

    // Date format is file-independent -- restore it directly.
    if (state.dateDelimiter !== undefined && dateDelimiterDropdown) {
        selectedDateDelimiter = state.dateDelimiter;
        dateDelimiterDropdown.value = selectedDateDelimiter;
        populateDateOrderDropdown(selectedDateDelimiter);
    }
    if (state.dateOrder && dateOrderDropdown) {
        selectedDateOrder = state.dateOrder;
        dateOrderDropdown.value = selectedDateOrder;
    }
    updateDateFormatTriggerLabel();

    // Stash Sheet/row-exclusion targets for when the user re-selects a file.
    pendingRestoreSheetName = state.sheetName || null;
    pendingExcludedRowNumbersToRestore = Array.isArray(state.excludedRowNumbers) ? state.excludedRowNumbers : null;

    // Restore Template selection via selectTemplate() -- it fetches the template's columns and
    // reveals the Sheet/Download group, but (correctly) stops short of mapping since no file is
    // present yet, so the Column Mapping panel stays hidden (Fix 2) until the user re-selects a
    // file.
    if (templateDropdown) {
        // isClientSelected() is a synchronous localStorage read (client_context.js), already
        // populated by the time window.onload runs -- if genuinely no client is selected yet,
        // there's nothing valid to restore into, so bail out rather than clear a session that
        // may still be legitimately resumable once a client is picked.
        if (!window.isClientSelected || !window.isClientSelected()) return;

        templateDropdown.value = state.templateId;
        if (templateDropdown.value === String(state.templateId)) {
            await selectTemplate(state.templateId);
            // Shown after selectTemplate() finishes so this message is the one the user sees,
            // not overwritten by selectTemplate()'s own "Template loaded..." status text.
            const templateLabel = templateDropdown.options[templateDropdown.selectedIndex]
                ? templateDropdown.options[templateDropdown.selectedIndex].textContent
                : 'your previous template';
            const fileHint = state.fileName ? ` Please re-select "${state.fileName}" to continue.` : ' Please re-select your file to continue.';
            showStatus(`📌 Resuming previous session for template "${templateLabel}".${fileHint}`, 'info');
            // Show a grayed-out, read-only echo of the previous mapping/row-exclusion state so
            // the page doesn't look like everything was lost -- just inert until the file returns.
            renderRestorePreview(state);
        } else {
            // The saved template no longer exists (e.g. deleted) -- nothing to restore.
            clearUploadSessionState();
        }
    }
}

// ---------- RESTORE PREVIEW (grayed-out, read-only echo of a restored session) ----------
// Built entirely from the saved sessionStorage snapshot -- no live file data exists yet at this
// point. Purely visual/non-interactive (dimmed + pointer-events:none via the
// "restore-preview-locked" class on #leftPanel, see upload.html's <style> block): the Template
// column/Excel column/Type/Matched columns mirror the live mapping table's shape but render as
// plain text instead of <select> dropdowns, since there's no real Excel header list yet to
// populate a dropdown from. Replaced by the real, interactive table (clearRestorePreview()) the
// moment autoMapColumns() succeeds after the user re-selects their file.
function renderRestorePreview(state) {
    if (!leftPanel || !mappingPanel || !mappingBody) return;
    if (!state.mappings || state.mappings.length === 0) return;

    leftPanel.style.display = 'block';
    mappingPanel.style.display = 'block';
    leftPanel.classList.add('restore-preview-locked');

    mappingBody.innerHTML = '';
    state.mappings.forEach(m => {
        const tr = document.createElement('tr');

        const td1 = document.createElement('td');
        td1.textContent = m.column_name;
        td1.style.fontWeight = '500';
        td1.style.whiteSpace = 'nowrap';
        tr.appendChild(td1);

        const td2 = document.createElement('td');
        td2.textContent = m.excel_column || '(unmapped)';
        tr.appendChild(td2);

        const td3 = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = 'type-badge';
        if (m.data_type === 'Numeric') badge.classList.add('numeric');
        if (m.data_type === 'Date') badge.classList.add('date');
        badge.textContent = m.data_type;
        td3.appendChild(badge);
        tr.appendChild(td3);

        const td4 = document.createElement('td');
        const icon = document.createElement('span');
        icon.style.fontSize = '16px';
        icon.textContent = m.is_matched ? '✅' : '❌';
        icon.style.color = m.is_matched ? '#16a34a' : '#dc2626';
        td4.appendChild(icon);
        tr.appendChild(td4);

        mappingBody.appendChild(tr);
    });

    const matched = state.mappings.filter(m => m.is_matched).length;
    if (mappingCount) mappingCount.textContent = matched + ' / ' + state.mappings.length + ' matched (locked preview)';

    // One-line locked banner above the mapping table, inserted directly into #leftPanel (not
    // #mappingPanel) so it stays at full opacity while the table content below it is dimmed --
    // it's the active "you need to act" element, not part of the inert preview.
    let banner = document.getElementById('restorePreviewBanner');
    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'restorePreviewBanner';
        banner.className = 'restore-preview-banner';
        leftPanel.insertBefore(banner, leftPanel.firstChild);
    }
    const sheetText = state.sheetName ? `sheet "${state.sheetName}"` : 'a previous sheet';
    const rowsText = (typeof state.totalRows === 'number' && state.totalRows > 0)
        ? `, ${state.totalRows - (state.excludedCount || 0)} of ${state.totalRows} rows were set to import`
        : '';
    banner.textContent = `🔒 Locked preview from ${sheetText}${rowsText} — re-select your file to reactivate.`;
}

// Removes the locked-preview dimming/banner once the real, interactive mapping table takes over
// (called from autoMapColumns()'s success path). A no-op if no preview was ever shown.
function clearRestorePreview() {
    if (leftPanel) leftPanel.classList.remove('restore-preview-locked');
    const banner = document.getElementById('restorePreviewBanner');
    if (banner) banner.remove();
}

// ---------- RESET VALIDATION STATE ----------
function resetValidationState() {
    validationStatus = 'idle';
    if (importBtn) {
        importBtn.innerHTML = 'Validate';
        importBtn.style.background = '';
        importBtn.style.color = '';
        importBtn.disabled = false;
    }
    updatePreviewButtonState();
    // Prior rate_mode result no longer reflects the current mapping -- clear it until
    // the next successful Validate call.
    lastRateModeInfo = null;
    renderRateModeIndicator();
}

// ---------- RATE MODE INDICATOR (Phase D of the Rate-derivation requirement) ----------
// Renders lastRateModeInfo (set from validate_only()'s rate_mode response field) as a
// small badge in both the Row Selection grid header and the Excel Preview header --
// informational only, no override control here (that stays in Template setup).
function renderRateModeIndicator() {
    const badges = [document.getElementById('rateModeBadgeGrid'), document.getElementById('rateModeBadgePreview')];
    const icon = lastRateModeInfo ? (lastRateModeInfo.effective_source === 'derived' ? '🧮' : '📥') : '';
    badges.forEach(el => {
        if (!el) return;
        if (lastRateModeInfo) {
            el.textContent = `${icon} Rate: ${lastRateModeInfo.label}`;
            el.style.display = 'inline-flex';
        } else {
            el.textContent = '';
            el.style.display = 'none';
        }
    });
}

// ---------- HELPERS ----------
// ---------- IMPORT PROGRESS BAR (Import Progress requirement, frontend phase) ----------
// Polls GET /api/upload/import-status/{file_id} (routers/import_export.py) while
// performImport()'s POST is in flight -- see the interval start/clear in performImport() below.
let importProgressPollInterval = null;

function showImportProgressBar() {
    const bar = document.getElementById('importProgressBar');
    const fill = document.getElementById('importProgressFill');
    const label = document.getElementById('importProgressLabel');
    if (!bar) return;
    if (fill) fill.style.width = '0%';
    if (label) label.textContent = '0 of 0 transactions uploaded';
    bar.style.display = 'flex';
}

function hideImportProgressBar() {
    const bar = document.getElementById('importProgressBar');
    if (bar) bar.style.display = 'none';
}

function updateImportProgressBar(current, total) {
    const fill = document.getElementById('importProgressFill');
    const label = document.getElementById('importProgressLabel');
    const pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0;
    if (fill) fill.style.width = pct + '%';
    if (label) label.textContent = `${current} of ${total} transactions uploaded`;
}

function pollImportProgress(fileId) {
    importProgressPollInterval = setInterval(async () => {
        try {
            const res = await fetch(API_BASE + '/api/upload/import-status/' + fileId);
            if (!res.ok) return;
            const status = await res.json();
            updateImportProgressBar(status.current || 0, status.total || 0);
        } catch (e) {
            // Silently skip a failed poll tick -- the next tick (or the final import
            // response itself) will resolve the UI either way.
        }
    }, 1000);
}

function showStatus(msg, type = 'info', extra = '') {
    if (!statusBar || !statusMessage) {
        return;
    }
    statusMessage.innerHTML = msg;
    if (extra) {
        statusMessage.innerHTML += '<div style="font-size:12px; opacity:0.9; margin-top:2px;">' + extra + '</div>';
    }
    if (statusExtra) statusExtra.textContent = '';
    statusBar.className = 'status-bar ' + type;
    statusBar.style.display = 'flex';
}

function setBadge(text, type = '') {
    if (!statusBadge) return;
    statusBadge.textContent = text;
    statusBadge.className = 'badge' + (type ? ' ' + type : '');
}

function showSpinner(show) {
    if (!importBtn) return;
    if (show) {
        importBtn.innerHTML = '<span class="spinner"></span> Processing...';
        importBtn.disabled = true;
    } else {
        importBtn.innerHTML = '🚀 Upload to Server';
        importBtn.disabled = false;
    }
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
}

function formatDate(d) {
    const date = new Date(d);
    return date.toLocaleDateString('en-GB') + ' ' + date.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
}

// Resolves the current client id -- same isClientSelected()/getSelectedClientId()/localStorage
// fallback pattern already used inline in the importBtn handler, pulled out here so the Save
// Mapping action and the saved-mapping auto-apply check (Phase A2) don't duplicate it.
function getCurrentClientId() {
    if (typeof window.isClientSelected === 'function' && window.isClientSelected()) {
        return window.getSelectedClientId();
    }
    return localStorage.getItem('selectedClientId');
}

// ---------- PREVIEW BUTTON STATE ----------
function updatePreviewButtonState() {
    const previewBtn = document.getElementById('previewBtn');
    if (!previewBtn) return;

    const isFileLoaded = workbookData !== null;
    const isSheetSelected = currentSheet !== null && currentSheet !== '';
    const isTemplateSelected = selectedTemplateId !== null;
    const isMappingDone = currentMappings.length > 0 && mappingPanel && mappingPanel.style.display !== 'none';
    const isValidated = validationStatus === 'valid';

    const isReady = isFileLoaded && isSheetSelected && isTemplateSelected && isMappingDone && isValidated;

    // Only appears once the file is loaded AND validated -- hidden (not just disabled)
    // beforehand, since it has nothing to preview yet.
    if (isReady) {
        previewBtn.style.display = 'inline-block';
        previewBtn.disabled = false;
    } else {
        previewBtn.style.display = 'none';
        previewBtn.disabled = true;
    }
}

// ---------- DATE FORMAT DROPDOWNS ----------
// Repopulates the Date Order dropdown based on the chosen delimiter, defaulting to DMY.
function populateDateOrderDropdown(delimiter) {
    if (!dateOrderDropdown) return;
    const options = DATE_ORDER_OPTIONS[delimiter] || DATE_ORDER_OPTIONS['/'];
    dateOrderDropdown.innerHTML = '';
    options.forEach(([value, label]) => {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = label;
        dateOrderDropdown.appendChild(opt);
    });
    selectedDateOrder = 'DMY';
    dateOrderDropdown.value = selectedDateOrder;
}

if (dateDelimiterDropdown) {
    dateDelimiterDropdown.addEventListener('change', function() {
        selectedDateDelimiter = this.value;
        populateDateOrderDropdown(selectedDateDelimiter);
        refreshDateFormatPreview();
        updateDateFormatTriggerLabel();
        saveUploadSessionState();
    });
}

if (dateOrderDropdown) {
    dateOrderDropdown.addEventListener('change', function() {
        selectedDateOrder = this.value;
        refreshDateFormatPreview();
        updateDateFormatTriggerLabel();
        saveUploadSessionState();
    });
}

// ---------- DATE FORMAT TRIGGER + POPOVER (Phase 3 of the Upload header reorg) ----------
// Collapses the Date Delimiter/Order dropdowns behind one trigger button + popover. The
// popover holds the real, listener-bound <select> elements (see upload.html) rather than
// JS-rebuilt content, so nothing here needs to touch the change listeners above.
let dateFormatPopoverOpen = false;

function setDateFormatGroupVisible(visible) {
    if (dateFormatGroup) dateFormatGroup.style.display = visible ? 'flex' : 'none';
}

// Reflects the current selectedDateDelimiter/selectedDateOrder on the trigger button, e.g.
// "Date format: DD-MM-YYYY", using the same friendly labels DATE_ORDER_OPTIONS already holds.
function updateDateFormatTriggerLabel() {
    if (!dateFormatTriggerLabel) return;
    const options = DATE_ORDER_OPTIONS[selectedDateDelimiter] || DATE_ORDER_OPTIONS['/'];
    const match = options.find(([code]) => code === selectedDateOrder);
    dateFormatTriggerLabel.textContent = match ? match[1] : '';
}

function openDateFormatPopover() {
    if (!dateFormatPopover) return;
    dateFormatPopover.style.display = 'flex';
    dateFormatPopoverOpen = true;
}

function closeDateFormatPopover() {
    if (!dateFormatPopover) return;
    dateFormatPopover.style.display = 'none';
    dateFormatPopoverOpen = false;
}

if (dateFormatTriggerBtn) {
    dateFormatTriggerBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        if (dateFormatPopoverOpen) {
            closeDateFormatPopover();
        } else {
            openDateFormatPopover();
        }
    });
}

// Close the popover on any click outside it (the trigger's own click is stopped from
// propagating above, so this only fires for clicks elsewhere) -- same pattern as
// verification.js's #voucherChangePopover outside-click listener.
document.addEventListener('click', function(e) {
    if (dateFormatPopoverOpen && dateFormatPopover && !dateFormatPopover.contains(e.target)) {
        closeDateFormatPopover();
    }
});

updateDateFormatTriggerLabel(); // Set the initial trigger label to match the defaults above

// ---------- DATE FORMAT LIVE PREVIEW (Phase 5) ----------
// Fetches 2-3 real sample dates from the uploaded file's mapped Date columns,
// parsed under the currently selected delimiter/order, so a wrong format
// choice is caught before running full validation/import. No-op until a
// file is uploaded and template mapping is known (Date columns identified).
async function refreshDateFormatPreview() {
    const container = getElement('dateFormatPreview');
    if (!container) return;

    if (!fileId || !currentMappings || currentMappings.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }

    const dateMappings = currentMappings
        .filter(m => m.data_type === 'Date' && m.excel_column)
        .map(m => ({ template_column_name: m.column_name, excel_column_name: m.excel_column }));

    if (dateMappings.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }

    try {
        const res = await fetch(API_BASE + '/api/upload/date-preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_id: fileId,
                sheet_name: currentSheet || 'Sheet1',
                date_mappings: dateMappings,
                date_delimiter: selectedDateDelimiter,
                date_order: selectedDateOrder
            })
        });
        if (!res.ok) throw new Error('Preview request failed');
        const data = await res.json();
        renderDateFormatPreview(data.samples || []);
    } catch (error) {
        console.error('Date preview error:', error);
        container.style.display = 'none';
        container.innerHTML = '';
    }
}

// Renders up to 3 sample lines as small text (not a table) -- a sanity check,
// not a data grid. Invalid samples render in red with a clear error message.
function renderDateFormatPreview(samples) {
    const container = getElement('dateFormatPreview');
    if (!container) return;

    if (!samples || samples.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }

    const shown = samples.slice(0, 3);
    let html = '<label>Preview</label>';
    shown.forEach(s => {
        if (s.valid) {
            html += `<span class="date-preview-line">${s.raw} → ${s.parsed}</span>`;
        } else {
            html += `<span class="date-preview-line error">Could not parse "${s.raw}" as ${selectedDateOrder}</span>`;
        }
    });
    container.innerHTML = html;
    container.style.display = 'flex';
}

// ---------- LOAD TEMPLATES ----------
async function loadTemplates() {
    try {
        const res = await fetch(API_BASE + '/api/templates');
        if (!res.ok) throw new Error('Failed to load templates');
        const templates = await res.json();
        if (templateDropdown) {
            templateDropdown.innerHTML = '<option value="">-- Select Template --</option>';
            templates.forEach(tpl => {
                const opt = document.createElement('option');
                opt.value = tpl.id;
                opt.textContent = tpl.template_name;
                templateDropdown.appendChild(opt);
            });
        }
        setBadge('✅ ' + templates.length + ' templates loaded');
    } catch (error) {
        console.error('Error loading templates:', error);
        setBadge('❌ Failed to load templates', 'error');
        showStatus('❌ Failed to load templates: ' + error.message, 'error');
    }
}

// ---------- TEMPLATE SELECTION ----------
if (templateDropdown) {
    templateDropdown.addEventListener('change', async function() {
        // Check if client is selected
        if (!window.isClientSelected || !window.isClientSelected()) {
            showStatus('⚠️ Please select a client first from the Dashboard.', 'error');
            this.value = '';
            setBadge('⛔ No client selected');
            if (importBtn) importBtn.disabled = true;
            updatePreviewButtonState();
            return;
        }
                const id = this.value;
        if (!id) {
            selectedTemplateId = null;
            if (mappingPanel) mappingPanel.style.display = 'none';
            if (leftPanel) leftPanel.style.display = 'none';
            if (modeActions) modeActions.style.display = 'none';
            if (sheetField) sheetField.style.display = 'none';
            if (importBtn) importBtn.disabled = true;
            setBadge('⏳ Select a template');
            showStatus('Select a template and upload an Excel file to begin.', 'info');
            resetValidationState(); // Reset validation when template is cleared
            setSheetDownloadGroupVisible(false); // Template cleared -- hide Sheet/Download again
            setDateFormatGroupVisible(false); // ...and the Date format trigger/popover too
            closeDateFormatPopover();
            updateGoToDataBankState(false); // ...and disable Go to Data Bank again
            updateStepIndicator(1);
            updatePreviewButtonState();
            // Template deliberately cleared -- any previously saved session (for the old
            // template) is no longer relevant, so drop it rather than leave it to spuriously
            // restore on a later page load.
            clearUploadSessionState();
            clearRestorePreview();
            return;
        }
        await selectTemplate(id);
    });
}

// Extracted from the dropdown's 'change' listener above so restoreUploadSessionState()
// (session-restore) can await this exact logic directly instead of dispatching a synthetic
// event -- dispatchEvent() can't be awaited, which would let restoreUploadSessionState()'s own
// "resuming session" status message race ahead of (and get overwritten by) this function's own
// later status updates. No behavior change for the normal dropdown-click path.
async function selectTemplate(id) {
    selectedTemplateId = parseInt(id);
    saveUploadSessionState(); // Template chosen -- worth remembering even if the fetch below fails
    // A template is now chosen -- reveal Sheet/Download together and advance the step
    // strip, regardless of whether the column-fetch below succeeds (Download only needs
    // the template id, already known; Sheet is populated independently once a file loads).
    setSheetDownloadGroupVisible(true);
    updateStepIndicator(2);
    setBadge('⏳ Loading template...', 'loading');

    try {
        const res = await fetch(API_BASE + `/api/templates/${id}/upload-columns`);
        if (!res.ok) throw new Error('Failed to fetch template columns');
        const data = await res.json();
        templateColumns = data.columns;
        if (downloadBtn) {
            downloadBtn.dataset.templateId = id;
            downloadBtn.disabled = false;
        }
        setBadge('✅ Template loaded: ' + data.total_columns + ' columns');

        if (fileId && currentSheet) {
            await autoMapColumns();
        } else if (fileId && !currentSheet) {
            showStatus('✅ Template loaded. Please select a sheet.', 'info');
        } else {
            showStatus('✅ Template loaded. Upload an Excel file to begin mapping.', 'success');
        }
    } catch (error) {
        console.error('Error loading template columns:', error);
        setBadge('❌ Failed to load columns', 'error');
        showStatus('❌ Failed to load template columns: ' + error.message, 'error');
    }
    updatePreviewButtonState();
    resetValidationState(); // New template means new data – reset validation
}

// ---------- FILE UPLOAD ----------
// "Upload File" button (replaces the old breadcrumb AND the old drag-and-drop area, removed
// entirely in Phase 2 — click-to-upload only, no drag/drop capability anymore). Reuses the
// existing fileInput/handleFile() flow the drag-and-drop area used to call.
if (uploadFileBtn) {
    uploadFileBtn.addEventListener('click', function() {
        if (fileInput) {
            fileInput.click();
        } else {
            console.error('❌ fileInput element not found');
        }
    });
} else {
    console.error('❌ Upload File button element not found');
}

if (fileInput) {
    fileInput.addEventListener('change', function(e) {
        if (this.files && this.files.length > 0) {
            handleFile(this.files[0]);
            this.value = '';
        } else {
            console.warn('⚠️ No file selected');
        }
    });
}

async function handleFile(file) {
    const validTypes = ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.ms-excel'];
    if (!validTypes.includes(file.type) && !file.name.endsWith('.xlsx') && !file.name.endsWith('.xls')) {
        showStatus('⚠️ Please select a valid Excel file (.xlsx or .xls)', 'error');
        return;
    }

    selectedFile = file;
    if (fileName) fileName.textContent = file.name;
    if (fileSize) fileSize.textContent = formatFileSize(file.size);
    if (fileInfo) fileInfo.style.display = 'flex';
    if (footerMeta) footerMeta.style.display = 'flex';
    if (footerFile) footerFile.textContent = '📄 ' + file.name;

    const reader = new FileReader();
    reader.onload = function(e) {
        try {
            const data = new Uint8Array(e.target.result);
            workbookData = data;
            const workbook = XLSX.read(data, { type: 'array' });
            sheetNames = workbook.SheetNames;

            if (sheetNames.length === 0) {
                showStatus('❌ No sheets found in the file.', 'error');
                return;
            }

            populateSheetSelector(sheetNames);
            // Prefer a restored session's previously-chosen sheet, if this file has a sheet by
            // that name -- consumed once, then cleared either way (a non-matching sheet name
            // just falls through to the normal default-to-first-sheet behavior below).
            if (pendingRestoreSheetName && sheetNames.includes(pendingRestoreSheetName)) {
                currentSheet = pendingRestoreSheetName;
            } else {
                currentSheet = sheetNames[0];
            }
            pendingRestoreSheetName = null;
            if (sheetSelector) sheetSelector.value = currentSheet;
            if (sheetField) sheetField.style.display = 'block';
            uploadFileToServer(file);

                        showStatus('📄 File loaded. Sheet "' + currentSheet + '" selected.', 'info');
            resetValidationState(); // Reset validation for new file
                    updatePreviewButtonState();
        resetValidationState(); // New mapping – reset validation
        saveUploadSessionState(); // File selected -- remember its name and the resolved sheet
        } catch (err) {
            showStatus('❌ Error reading file: ' + err.message, 'error');
        }
    };
    reader.readAsArrayBuffer(file);
}

// ---------- SHEET SELECTOR ----------
function populateSheetSelector(sheets) {
    if (!sheetSelector) return;
    sheetSelector.innerHTML = '<option value="">-- Select Sheet --</option>';
    sheets.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        sheetSelector.appendChild(opt);
    });
    if (sheetField) sheetField.style.display = 'block';
}

if (sheetSelector) {
    sheetSelector.addEventListener('change', function() {
        currentSheet = this.value;
                if (currentSheet && selectedTemplateId && fileId) {
            autoMapColumns();
            showStatus('📄 Sheet changed to "' + currentSheet + '". Re‑mapping...', 'info');
        } else if (currentSheet && selectedTemplateId && !fileId) {
            if (selectedFile) {
                uploadFileToServer(selectedFile);
            }
        }
        resetValidationState(); // Reset validation on sheet change
        updatePreviewButtonState();
        saveUploadSessionState(); // Sheet changed -- remember the new selection
    });
}

// ---------- UPLOAD TO SERVER ----------
async function uploadFileToServer(file) {
    const formData = new FormData();
    formData.append('file', file);
    try {
        const res = await fetch('/api/import-excel', {
            method: 'POST',
            body: formData
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.message || 'Upload failed');
        fileId = result.file_id;
        if (footerRows) footerRows.textContent = '📊 ' + result.record_count + ' rows';
        if (footerDate) footerDate.textContent = '📁 Uploaded: ' + formatDate(new Date());

        showStatus('✅ File uploaded: ' + result.record_count + ' rows', 'success');

        if (selectedTemplateId && currentSheet) {
            await autoMapColumns();
        } else if (selectedTemplateId && !currentSheet) {
            showStatus('✅ File uploaded. Please select a sheet.', 'info');
        } else {
            showStatus('✅ File uploaded. Please select a template.', 'info');
        }
    } catch (error) {
        showStatus('❌ Upload error: ' + error.message, 'error');
        resetUpload();
    }
}

// ---------- CLEAR FILE ----------
if (clearBtn) {
    clearBtn.addEventListener('click', resetUpload);
}

function resetUpload() {
    selectedFile = null;
    fileId = null;
    if (fileInfo) fileInfo.style.display = 'none';
    if (fileInput) fileInput.value = '';
    if (mappingPanel) mappingPanel.style.display = 'none';
    if (leftPanel) leftPanel.style.display = 'none';
    if (modeActions) modeActions.style.display = 'none';
    if (sheetField) sheetField.style.display = 'none';
    setSheetDownloadGroupVisible(false); // Full reset -- collapse the Sheet/Download group too
    updateStepIndicator(1);
    if (sheetSelector) sheetSelector.innerHTML = '';
    currentSheet = null;
    sheetNames = [];
    workbookData = null;
    currentMappings = [];
    excelHeaders = [];
    templateColumns = [];
    if (footerMeta) footerMeta.style.display = 'none';
    if (importBtn) importBtn.disabled = true;
        showStatus('🔄 Reset complete. Select a template and upload a file.', 'info');
    setBadge('⏳ Select a template');
    if (mappingBody) mappingBody.innerHTML = '';
    updatePreviewButtonState();

    // Clear the date format live preview (Phase 5) – no file/mapping left to sample from.
    const dateFormatPreviewEl = getElement('dateFormatPreview');
    if (dateFormatPreviewEl) {
        dateFormatPreviewEl.style.display = 'none';
        dateFormatPreviewEl.innerHTML = '';
    }
    // Hide the Date format trigger/popover too (Phase 3) -- no mapping left to base it on.
    setDateFormatGroupVisible(false);
    closeDateFormatPopover();

    // Disable Go to Data Bank again (Phase 4) -- no mapping left to base it on.
    updateGoToDataBankState(false);

    // Hide the row selection grid and clear its state (Phase 2 of row-exclusion) –
    // no file/mapping left to build it from.
    const rowExclusionContainerEl = document.getElementById('rowExclusionContainer');
    if (rowExclusionContainerEl) rowExclusionContainerEl.style.display = 'none';
    excludedRowNumbers = new Set();
    validationFailedRows = new Map();
    rowExclusionAllRows = [];
    rowExclusionHeaders = [];
    rowExclusionCurrentPage = 1;

    // Hide/clear the exceptions list (Phase 3 of the row-level blank-field-rejection
    // requirement) -- no file/mapping left to base it on.
    rejectedRowsData = new Map();
    rejectedRowOverrides = new Set();
    const rowRejectionContainerEl = document.getElementById('rowRejectionContainer');
    if (rowRejectionContainerEl) rowRejectionContainerEl.style.display = 'none';
    const rowRejectionListEl = document.getElementById('rowRejectionList');
    if (rowRejectionListEl) rowRejectionListEl.innerHTML = '';

    // Reset validation status
    validationStatus = 'idle';
    if (importBtn) {
        importBtn.innerHTML = 'Validate';
        importBtn.style.background = '';
        importBtn.style.color = '';
        importBtn.disabled = false;
    }

    // Session-restore: a reset (explicit Reset button, or the Close/overlay-click handlers in
    // showImportSummary() after a successful import) means there's nothing left to resume --
    // drop any saved session state, any not-yet-consumed restore hints, and any locked preview
    // still showing.
    clearUploadSessionState();
    clearRestorePreview();
}

// ---------- SAVE COLUMN MAPPING (Phase A2 of 3) ----------
// Persists the currently matched columns for this client+template via Phase A1's
// POST /api/upload/save-mapping. Always a full overwrite (backend upserts via delete-then-
// reinsert) -- there's no separate edit/delete UI this phase (Phase A3).
if (saveMappingBtn) {
    saveMappingBtn.addEventListener('click', async function() {
        const clientId = getCurrentClientId();
        if (!clientId) {
            showStatus('⚠️ Please select a client first from the Dashboard.', 'error');
            return;
        }
        if (!selectedTemplateId || !currentMappings || currentMappings.length === 0) {
            showStatus('⚠️ Map at least one column before saving.', 'error');
            return;
        }

        // SavedMappingEntry.excel_column (routers/import_export.py) is a required str, so
        // unmatched (null excel_column) rows must be filtered out here, not left for the
        // endpoint's own blank-skip to handle.
        const mappingsToSave = currentMappings
            .filter(m => m.excel_column)
            .map(m => ({ template_column_id: m.column_id, excel_column: m.excel_column }));

        if (mappingsToSave.length === 0) {
            showStatus('⚠️ Map at least one column before saving.', 'error');
            return;
        }

        saveMappingBtn.disabled = true;
        try {
            const res = await fetch(API_BASE + '/api/upload/save-mapping', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    client_id: parseInt(clientId),
                    template_id: selectedTemplateId,
                    mappings: mappingsToSave
                })
            });
            const result = await res.json();
            if (!res.ok) throw new Error(result.detail || result.message || 'Save failed');
            showStatus(`💾 Mapping saved (${result.saved_count} column${result.saved_count === 1 ? '' : 's'}) — it will auto-fill next time you upload this template for this client.`, 'success');
        } catch (error) {
            console.error('Save mapping error:', error);
            showStatus('❌ Failed to save mapping: ' + error.message, 'error');
        } finally {
            saveMappingBtn.disabled = false;
        }
    });
}

// ---------- CLEAR SAVED COLUMN MAPPING (Phase A3 of 3, final phase) ----------
// Removes the persisted saved mapping for this client+template via the new DELETE endpoint --
// "edit" is already covered by Save Mapping's own overwrite behavior (Phase A2), so this is the
// one remaining action: an explicit way to remove a saved mapping entirely. Deliberately does not
// touch the currently-displayed mapping table/currentMappings -- only the saved copy is removed,
// so a later reload/re-select for this client+template no longer auto-applies anything.
if (clearMappingBtn) {
    clearMappingBtn.addEventListener('click', async function() {
        const clientId = getCurrentClientId();
        if (!clientId) {
            showStatus('⚠️ Please select a client first from the Dashboard.', 'error');
            return;
        }
        if (!selectedTemplateId) {
            showStatus('⚠️ Select a template first.', 'error');
            return;
        }

        const confirmed = window.confirm('Remove the saved column mapping for this client + template? Future uploads will fall back to auto-mapping until you save a new one.');
        if (!confirmed) return;

        clearMappingBtn.disabled = true;
        try {
            const res = await fetch(API_BASE + `/api/upload/saved-mapping?client_id=${clientId}&template_id=${selectedTemplateId}`, {
                method: 'DELETE'
            });
            const result = await res.json();
            if (!res.ok) throw new Error(result.detail || result.message || 'Clear failed');
            if (result.deleted_count > 0) {
                showStatus(`🗑️ Saved mapping cleared (${result.deleted_count} column${result.deleted_count === 1 ? '' : 's'}). Future uploads will auto-map instead.`, 'success');
            } else {
                showStatus('ℹ️ No saved mapping existed for this client/template.', 'info');
            }
        } catch (error) {
            console.error('Clear saved mapping error:', error);
            showStatus('❌ Failed to clear saved mapping: ' + error.message, 'error');
        } finally {
            clearMappingBtn.disabled = false;
        }
    });
}

// ---------- AUTO-APPLY SAVED MAPPING (Phase A2 of 3) ----------
// Called from autoMapColumns() right after the backend's own auto-map guess is in hand. Overrides
// a currentMappings entry with its saved excel_column ONLY when that exact header still exists on
// the current sheet (excelHeaders) -- so a saved mapping can never force a mismatch onto a sheet
// with different headers; anything that doesn't match keeps auto-mapping's own result untouched.
// Returns the number of columns actually applied (0 if none/no saved mapping/lookup failed).
async function applySavedMapping() {
    const clientId = getCurrentClientId();
    if (!clientId || !selectedTemplateId) return 0;

    let data;
    try {
        const res = await fetch(API_BASE + `/api/upload/saved-mapping?client_id=${clientId}&template_id=${selectedTemplateId}`);
        if (!res.ok) return 0;
        data = await res.json();
    } catch (error) {
        console.warn('⚠️ Could not check for a saved column mapping:', error);
        return 0;
    }

    if (!data || !data.has_saved_mapping || !Array.isArray(data.mappings) || data.mappings.length === 0) {
        return 0;
    }

    let appliedCount = 0;
    data.mappings.forEach(saved => {
        if (!saved.excel_column || !excelHeaders.includes(saved.excel_column)) return; // header no longer present -- fall back to auto-mapping
        const target = currentMappings.find(m => m.column_id === saved.template_column_id);
        if (!target) return;
        target.excel_column = saved.excel_column;
        target.is_matched = true;
        appliedCount++;
    });
    return appliedCount;
}

// ---------- AUTO-MAP ----------
async function autoMapColumns() {
    if (!selectedTemplateId || !currentSheet || !fileId) {
        showStatus('⚠️ Please select a template, a sheet, and upload a file.', 'error');
        return;
    }

    try {
        showStatus('⏳ Auto‑mapping columns...', 'loading');
        setBadge('⏳ Mapping...', 'loading');

        const res = await fetch(API_BASE + '/api/upload/auto-map', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_id: fileId,
                template_id: selectedTemplateId,
                sheet_name: currentSheet
            })
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Auto‑mapping failed');
        }

        const data = await res.json();
        currentMappings = data.mappings;
        excelHeaders = data.excel_headers;

        clearRestorePreview(); // Real data has arrived -- drop any grayed-out restore preview first

        // Auto-apply a previously saved mapping (Phase A2) on top of the backend's own auto-map
        // guess -- only overrides columns whose saved Excel header still exists on this sheet, so
        // it can never force a mismatch; anything else keeps auto-mapping's own result untouched.
        const savedMappingAppliedCount = await applySavedMapping();

        renderMapping(currentMappings);
        // populateUniqueIdDropdown() removed – no longer needed

        if (modeActions) modeActions.style.display = 'block';
        if (mappingPanel) mappingPanel.style.display = 'block';
        if (leftPanel) leftPanel.style.display = 'block';
        if (importBtn) importBtn.disabled = false;

        const matched = currentMappings.filter(m => m.is_matched).length;
        if (mappingCount) mappingCount.textContent = matched + ' / ' + data.total_columns + ' matched';
        setBadge('✅ ' + matched + '/' + data.total_columns + ' matched');
        let mapStatusMsg = '✅ Auto‑mapped ' + matched + '/' + data.total_columns + ' columns';
        if (savedMappingAppliedCount > 0) {
            mapStatusMsg += ` (${savedMappingAppliedCount} from your saved mapping)`;
        }
        showStatus(mapStatusMsg, 'success');

        updatePreviewButtonState();

        // Date columns are now known via the mapping – reveal the Date format trigger/popover
        // and show the live sample-date preview inside it.
        setDateFormatGroupVisible(true);
        refreshDateFormatPreview();

        // Mapping is complete – show the row selection grid (Phase 2 of row-exclusion).
        renderRowExclusionGrid();

        // Mapping resolved (Phase 4) -- enable Go to Data Bank and advance the step strip to
        // "Map columns". Preview's own visibility/enabled-state is driven by
        // updatePreviewButtonState() (called just below), not this group any more.
        updateGoToDataBankState(true);
        updateStepIndicator(3);
        saveUploadSessionState(); // Mapping resolved -- checkpoint the full session state

    } catch (error) {
        console.error('Auto-map error:', error);
        setBadge('❌ Mapping failed', 'error');
        showStatus('❌ Auto‑mapping failed: ' + error.message, 'error');
    }
}

// ---------- RENDER MAPPING TABLE ----------
function renderMapping(mappings) {
    if (!mappingBody) return;
    mappingBody.innerHTML = '';
    if (!mappings || mappings.length === 0) {
        if (mappingPanel) mappingPanel.style.display = 'none';
        if (leftPanel) leftPanel.style.display = 'none';
        return;
    }

    mappings.forEach((m, rowIndex) => {
        const tr = document.createElement('tr');

        const td1 = document.createElement('td');
        td1.textContent = m.column_name;
        td1.style.fontWeight = '500';
        td1.style.whiteSpace = 'nowrap';
        tr.appendChild(td1);

        const td2 = document.createElement('td');
        const select = document.createElement('select');
        select.dataset.rowIndex = rowIndex;
        select.dataset.columnId = m.column_id;

        function buildOptions() {
            const selectedValues = currentMappings
                .filter((_, idx) => idx !== rowIndex)
                .map(mm => mm.excel_column)
                .filter(v => v !== null && v !== '');
            select.innerHTML = '';
            const noneOpt = document.createElement('option');
            noneOpt.value = '';
            noneOpt.textContent = '— None —';
            select.appendChild(noneOpt);
            excelHeaders.forEach(h => {
                if (selectedValues.includes(h)) return;
                const opt = document.createElement('option');
                opt.value = h;
                opt.textContent = h;
                if (m.excel_column === h) {
                    opt.selected = true;
                }
                select.appendChild(opt);
            });
            if (m.excel_column && ![...select.options].some(o => o.value === m.excel_column)) {
                m.excel_column = null;
                m.is_matched = false;
                const statusTd = tr.children[3];
                if (statusTd) {
                    const icon = statusTd.querySelector('span');
                    if (icon) {
                        icon.textContent = '❌';
                        icon.style.color = '#dc2626';
                    }
                }
                updateCounts();
            } else {
                if (m.excel_column) {
                    const opt = select.querySelector(`option[value="${m.excel_column}"]`);
                    if (opt) opt.selected = true;
                }
            }
        }
        buildOptions();

                select.addEventListener('change', function() {
            const newVal = this.value || null;
            m.excel_column = newVal;
            m.is_matched = !!newVal;
            const statusTd = tr.children[3];
            if (statusTd) {
                const icon = statusTd.querySelector('span');
                if (icon) {
                    if (m.is_matched) {
                        icon.textContent = '✅';
                        icon.style.color = '#16a34a';
                    } else {
                        icon.textContent = '❌';
                        icon.style.color = '#dc2626';
                    }
                }
            }
            // Column mapping changed -- reset row selection grid checkboxes/flags (Phase 4
            // of the row-exclusion requirement), since the key column basis may have shifted.
            resetRowExclusionSelections();
            requestAnimationFrame(() => {
                renderMapping(currentMappings);
            });
        });

        td2.appendChild(select);
        tr.appendChild(td2);

        const td3 = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = 'type-badge';
        if (m.data_type === 'Numeric') badge.classList.add('numeric');
        if (m.data_type === 'Date') badge.classList.add('date');
        badge.textContent = m.data_type;
        td3.appendChild(badge);
        tr.appendChild(td3);

        const td4 = document.createElement('td');
        const icon = document.createElement('span');
        icon.style.fontSize = '16px';
        if (m.is_matched) {
            icon.textContent = '✅';
            icon.style.color = '#16a34a';
        } else {
            icon.textContent = '❌';
            icon.style.color = '#dc2626';
        }
        td4.appendChild(icon);
        tr.appendChild(td4);

        mappingBody.appendChild(tr);
    });

     updateCounts();

    function updateCounts() {
        const matched = currentMappings.filter(m => m.is_matched).length;
        if (mappingCount) mappingCount.textContent = matched + ' / ' + currentMappings.length + ' matched';
        setBadge('✅ ' + matched + '/' + currentMappings.length + ' matched');
    }
}

// ---------- UNIQUE ID DROPDOWN (removed – upsert is automatic) ----------

// ---------- DOWNLOAD TEMPLATE ----------
if (downloadBtn) {
    downloadBtn.addEventListener('click', function() {
        const templateId = this.dataset.templateId;
        if (!templateId) {
            showStatus('⚠️ Please select a template first', 'error');
            return;
        }
        window.open(API_BASE + `/api/upload/download-template/${templateId}`, '_blank');
    });
}

// ---------- RESET ----------
if (resetBtn) {
    resetBtn.addEventListener('click', function() {
        resetUpload();
        if (templateDropdown) templateDropdown.value = '';
        selectedTemplateId = null;
        setBadge('⏳ Select a template');
        showStatus('🔄 Reset complete. Select a template and upload a file.', 'info');
    });
}
// ---------- IMPORT SUMMARY POPUP ----------
function showImportSummary(result) {
    // Create overlay
    const overlay = document.createElement('div');
    overlay.id = 'importSummaryOverlay';
    overlay.style.cssText = `
        position: fixed; top:0; left:0; width:100%; height:100%;
        background: rgba(0,0,0,0.5); z-index:2000;
        display: flex; justify-content:center; align-items:center;
    `;
    
    // Create modal
    const modal = document.createElement('div');
    modal.style.cssText = `
        background: white; border-radius: 12px; padding: 30px;
        max-width: 560px; width: 90%; box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        font-family: 'Segoe UI', sans-serif;
        max-height: 90vh;
        overflow-y: auto;
    `;
    
    const total = result.total_rows || 0;
    const inserted = result.imported_count || 0;
    const updated = result.updated_count || 0;
    const errors = result.validation_errors || [];
    const errorCount = errors.length;
    
    // --- Change detection counts ---
    const cd = result.change_detection || {};
    const unchanged = cd.unchanged_count || 0;
    const changed = cd.changed_count || 0;
    const newRows = cd.new_count || 0;
    const deleted = cd.deleted_count || 0;
    
    let statusIcon = '✅';
    let statusColor = '#16a34a';
    let statusText = 'Import Successful';
    if (errorCount > 0) {
        statusIcon = '⚠️';
        statusColor = '#f59e0b';
        statusText = 'Import with Warnings';
    }

    // Build a plain, single-line outcome summary (no inline emoji, no run-on sentence).
    let detailMessage = '';
    if (inserted > 0 && updated > 0) {
        detailMessage = `${inserted} new row${inserted === 1 ? '' : 's'} inserted &middot; ${updated} existing row${updated === 1 ? '' : 's'} updated`;
    } else if (inserted > 0) {
        detailMessage = `${inserted} new row${inserted === 1 ? '' : 's'} inserted`;
    } else if (updated > 0) {
        detailMessage = `${updated} existing row${updated === 1 ? '' : 's'} updated`;
    } else {
        detailMessage = 'No changes were made';
    }

    // Change Detection Summary: labeled stat block matching the tile styling below, color-coded via left border instead of emoji.
    let changeSummary = '';
    if (unchanged > 0 || changed > 0 || newRows > 0 || deleted > 0) {
        changeSummary = `
            <div style="margin-top:20px;">
                <div style="font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:0.04em; color:#94a3b8; margin-bottom:8px;">Change Detection</div>
                <div style="display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:8px;">
                    <div style="background:#f8fafc; border-left:3px solid #94a3b8; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${unchanged}</div>
                        <div style="font-size:11px; color:#64748b;">Unchanged</div>
                    </div>
                    <div style="background:#f8fafc; border-left:3px solid #f59e0b; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${changed}</div>
                        <div style="font-size:11px; color:#64748b;">Changed</div>
                    </div>
                    <div style="background:#f8fafc; border-left:3px solid #16a34a; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${newRows}</div>
                        <div style="font-size:11px; color:#64748b;">New</div>
                    </div>
                    <div style="background:#f8fafc; border-left:3px solid #dc2626; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${deleted}</div>
                        <div style="font-size:11px; color:#64748b;">Missing</div>
                    </div>
                </div>
                <div style="font-size:11px; color:#94a3b8; margin-top:8px;">
                    Changed rows have been reset to "Pending" and require re-verification.
                </div>
            </div>
        `;
    }

    // Row Exceptions summary (Phase 4 of the blank-field-rejection requirement) --
    // deliberately kept as two DISTINCT counts, not lumped together: rows truly
    // excluded from import vs. rows force-included with a genuinely blank critical
    // field (Document ID/Amount/Date). Only rendered when at least one row was
    // identified as rejected this run.
    const rejectedCount = result.rejected_count || 0;
    const forceIncludedCount = result.force_included_count || 0;
    let rejectionSummary = '';
    if (rejectedCount > 0) {
        const excludedCount = rejectedCount - forceIncludedCount;
        rejectionSummary = `
            <div style="margin-top:20px;">
                <div style="font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:0.04em; color:#94a3b8; margin-bottom:8px;">Row Exceptions</div>
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
                    <div style="background:#f8fafc; border-left:3px solid #dc2626; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${excludedCount}</div>
                        <div style="font-size:11px; color:#64748b;">Rejected (excluded)</div>
                    </div>
                    <div style="background:#f8fafc; border-left:3px solid #f59e0b; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${forceIncludedCount}</div>
                        <div style="font-size:11px; color:#64748b;">Imported with missing data</div>
                    </div>
                </div>
                <div style="font-size:11px; color:#94a3b8; margin-top:8px;">
                    Rows imported with missing data have a blank Document ID, Amount, or Date and are flagged for follow-up.
                </div>
            </div>
        `;
    }

    // Explanatory note about debit/credit lines, styled as a plain muted info box.
    const note = `<div style="font-size:12px; color:#64748b; background:#f8fafc; padding:10px 12px; border-radius:6px; margin-top:16px;">
        <strong style="color:#475569;">Note:</strong> Each voucher may appear twice in the file (debit and credit).
        The system stores one transaction per voucher ID. Total records in the Data Bank reflect distinct vouchers.
    </div>`;

    modal.innerHTML = `
        <div style="text-align:center; margin-bottom:20px;">
            <div style="font-size:40px; line-height:1;">${statusIcon}</div>
            <h2 style="margin:10px 0 4px; color:#1a1a2e; font-size:20px;">${statusText}</h2>
            <p style="color:#64748b; font-size:14px; margin:0;">${detailMessage}</p>
        </div>
        <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px;">
            <div style="background:#f8fafc; padding:12px; border-radius:8px; text-align:center;">
                <div style="font-size:24px; font-weight:700; color:#3b82f6;">${total}</div>
                <div style="font-size:12px; color:#64748b;">Lines Processed</div>
            </div>
            <div style="background:#f8fafc; padding:12px; border-radius:8px; text-align:center;">
                <div style="font-size:24px; font-weight:700; color:#16a34a;">${inserted}</div>
                <div style="font-size:12px; color:#64748b;">New Rows</div>
            </div>
            <div style="background:#f8fafc; padding:12px; border-radius:8px; text-align:center;">
                <div style="font-size:24px; font-weight:700; color:#f59e0b;">${updated}</div>
                <div style="font-size:12px; color:#64748b;">Updated Rows</div>
            </div>
        </div>
        ${changeSummary}
        ${rejectionSummary}
        ${note}
        ${errorCount > 0 ? `
            <div style="background:#fef2f2; border:1px solid #fecaca; padding:12px; border-radius:8px; margin-top:16px; max-height:150px; overflow-y:auto;">
                <div style="font-weight:600; color:#991b1b; margin-bottom:4px; font-size:13px;">${errorCount} validation error${errorCount === 1 ? '' : 's'}</div>
                ${errors.slice(0,5).map(err => `
                    <div style="font-size:12px; color:#991b1b; padding:2px 0; border-bottom:1px solid #fecaca;">
                        Row ${err.row_number}: ${err.errors.map(e => `${e.column} - ${e.message}`).join('; ')}
                    </div>
                `).join('')}
                ${errors.length > 5 ? `<div style="font-size:12px; color:#991b1b; margin-top:2px;">...and ${errors.length - 5} more.</div>` : ''}
            </div>
        ` : ''}
        <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:20px; flex-wrap:wrap;">
            <a href="/static/databank.html" target="_blank" style="padding:8px 20px; background:#3b82f6; color:white; border:none; border-radius:6px; font-weight:600; cursor:pointer; text-decoration:none; display:inline-block; font-size:14px;">View in Data Bank</a>
            <button id="importSummaryCloseBtn" style="padding:8px 24px; background:#e2e8f0; color:#1a1a2e; border:none; border-radius:6px; font-weight:600; cursor:pointer; font-size:14px;">Close</button>
        </div>
    `;
    
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    
    // Close on button click
    overlay.querySelector('#importSummaryCloseBtn').addEventListener('click', function() {
        document.body.removeChild(overlay);
        resetUpload();
    });
    // Close on overlay click (background)
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) {
            document.body.removeChild(overlay);
            resetUpload();
        }
    });
}
// ---------- SHOW VALIDATION ERRORS ----------
function showValidationErrors(result) {
    const overlay = document.createElement('div');
    overlay.id = 'validationErrorOverlay';
    overlay.style.cssText = `
        position: fixed; top:0; left:0; width:100%; height:100%;
        background: rgba(0,0,0,0.5); z-index:2000;
        display: flex; justify-content:center; align-items:center;
        font-family: 'Segoe UI', sans-serif;
    `;

    const modal = document.createElement('div');
    modal.style.cssText = `
        background: white; border-radius: 12px; padding: 24px 30px;
        max-width: 700px; width: 95%; max-height: 80vh; overflow: auto;
        box-shadow: 0 20px 60px rgba(0,0,0,0.3);
    `;

    let errorRows = '';
    const errors = result.errors || [];
    errors.forEach(err => {
        const rowNum = err.row_number;
        const errMsgs = err.errors.map(e => `${e.column}: ${e.message}`).join('; ');
        errorRows += `<tr style="border-bottom:1px solid #f0f2f5;">
            <td style="padding:6px 10px; font-weight:600;">${rowNum}</td>
            <td style="padding:6px 10px; color:#dc2626;">${errMsgs}</td>
        </tr>`;
    });

    modal.innerHTML = `
        <h2 style="margin:0 0 4px; color:#dc2626;">❌ Validation Errors</h2>
        <p style="color:#64748b; margin-bottom:12px;">
            <strong>${result.error_count}</strong> error(s) found in <strong>${result.total_rows}</strong> rows.
            Please fix the errors in your Excel file and try again.
        </p>
        <div style="overflow:auto; max-height:300px; border:1px solid #e2e8f0; border-radius:6px; margin-bottom:12px;">
            <table style="width:100%; border-collapse:collapse; font-size:13px;">
                <thead style="position:sticky; top:0; background:#f1f5f9;">
                    <tr>
                        <th style="padding:6px 10px; text-align:left; border-bottom:2px solid #e2e8f0;">Row</th>
                        <th style="padding:6px 10px; text-align:left; border-bottom:2px solid #e2e8f0;">Error</th>
                    </tr>
                </thead>
                <tbody>${errorRows}</tbody>
            </table>
        </div>
        <div style="display:flex; gap:10px; justify-content:flex-end;">
            <button id="validationErrorCloseBtn" style="padding:8px 24px; background:#3b82f6; color:white; border:none; border-radius:6px; font-weight:600; cursor:pointer;">Close</button>
        </div>
    `;

    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    overlay.querySelector('#validationErrorCloseBtn').addEventListener('click', function() {
        document.body.removeChild(overlay);
    });
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) {
            document.body.removeChild(overlay);
        }
    });
}
// ---------- OVERWRITE CONFIRMATION DIALOG (Enhanced) ----------
function showOverwriteConfirmation(duplicates, count) {
    return new Promise((resolve) => {
        // Create overlay
        const overlay = document.createElement('div');
        overlay.id = 'overwriteConfirmOverlay';
        overlay.style.cssText = `
            position: fixed; top:0; left:0; width:100%; height:100%;
            background: rgba(0,0,0,0.6); z-index:3000;
            display: flex; justify-content:center; align-items:center;
            font-family: 'Segoe UI', sans-serif;
        `;

        // Determine all columns from the first duplicate
        let allColumns = [];
        if (duplicates.length > 0 && duplicates[0].columns) {
            allColumns = duplicates[0].columns;
        } else if (duplicates.length > 0 && duplicates[0].old_data) {
            allColumns = Object.keys(duplicates[0].old_data);
        }

        // Build full table
        let tableRows = '';
        const maxShow = Math.min(duplicates.length, 50); // Show up to 50 rows
        for (let i = 0; i < maxShow; i++) {
            const dup = duplicates[i];
            const oldData = dup.old_data || {};
            const newData = dup.new_data || {};

            // Row header with unique value and row number
            let rowHtml = `<tr style="border-bottom:1px solid #e2e8f0; background:${i % 2 === 0 ? '#fafcff' : '#ffffff'};">
                <td style="padding:6px 10px; font-weight:600; font-size:12px; position:sticky; left:0; background:${i % 2 === 0 ? '#fafcff' : '#ffffff'}; z-index:1;">#${i+1}<br><span style="font-weight:400; color:#64748b;">${dup.unique_value}</span></td>`;

            // For each column, show old → new
            allColumns.forEach(col => {
                const oldVal = oldData[col] !== undefined && oldData[col] !== null ? oldData[col] : '—';
                const newVal = newData[col] !== undefined && newData[col] !== null ? newData[col] : '—';
                const isChanged = String(oldVal) !== String(newVal);
                rowHtml += `<td style="padding:4px 8px; font-size:12px; border-left:1px solid #f0f2f5; min-width:80px;">
                    <div style="color:#dc2626; font-size:11px; ${isChanged ? '' : 'opacity:0.5;'}">${oldVal}</div>
                    <div style="color:#16a34a; font-size:11px; ${isChanged ? 'font-weight:500;' : 'opacity:0.5;'}">${isChanged ? '→ ' : '= '}${newVal}</div>
                </td>`;
            });

            rowHtml += `</tr>`;
            tableRows += rowHtml;
        }

        const modal = document.createElement('div');
        modal.style.cssText = `
            background: white; border-radius: 12px; padding: 20px 24px;
            max-width: 95vw; width: 95%; max-height: 90vh; overflow: auto;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            font-family: 'Segoe UI', sans-serif;
        `;

        // Create the modal content with progress bar area
        modal.innerHTML = `
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                <h2 style="margin:0; color:#f59e0b; font-size:20px;">⚠️ Overwrite Confirmation</h2>
                <span style="font-size:13px; color:#64748b;">${count} duplicate row(s)</span>
            </div>
            <p style="color:#64748b; margin-bottom:12px; font-size:14px;">
                The following <strong>${count}</strong> row(s) will be <strong>overwritten</strong> with new data.
                Changes are logged in the audit trail.
            </p>
            <div style="overflow:auto; max-height:45vh; border:1px solid #e2e8f0; border-radius:6px; margin-bottom:12px;">
                <table style="width:100%; border-collapse:collapse; font-size:12px; min-width:${Math.max(400, allColumns.length * 80)}px;">
                    <thead style="position:sticky; top:0; background:#f1f5f9; z-index:2;">
                        <tr>
                            <th style="padding:6px 10px; text-align:left; border-bottom:2px solid #e2e8f0; position:sticky; left:0; background:#f1f5f9; z-index:3; min-width:80px;">Row / ID</th>
                            ${allColumns.map(col => `<th style="padding:6px 10px; text-align:left; border-bottom:2px solid #e2e8f0; white-space:nowrap; font-size:11px;">${col}</th>`).join('')}
                        </tr>
                    </thead>
                    <tbody>${tableRows}</tbody>
                </table>
                ${duplicates.length > 50 ? `<div style="padding:8px; text-align:center; color:#94a3b8; font-size:12px;">... and ${duplicates.length - 50} more</div>` : ''}
            </div>

            <!-- Progress Bar Area (hidden initially) -->
            <div id="overwriteProgressArea" style="display:none; margin:8px 0 12px 0;">
                <div style="background:#f1f5f9; border-radius:4px; height:6px; overflow:hidden;">
                    <div id="overwriteProgressBar" style="background:#f59e0b; height:6px; width:0%; transition:width 0.3s ease;"></div>
                </div>
                <div id="overwriteProgressText" style="font-size:13px; color:#64748b; margin-top:4px;">0 of ${count} rows overwritten</div>
            </div>

            <!-- Button Row -->
            <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:12px; border-top:1px solid #e2e8f0; padding-top:12px;">
                <button id="overwriteCancelBtn" style="padding:8px 24px; background:#e2e8f0; color:#1a1a2e; border:none; border-radius:6px; font-weight:600; cursor:pointer;">Cancel</button>
                <button id="overwriteConfirmBtn" style="padding:8px 24px; background:#f59e0b; color:white; border:none; border-radius:6px; font-weight:600; cursor:pointer; min-width:140px;">
                    ✅ Overwrite ${count} Rows
                </button>
            </div>
        `;

        overlay.appendChild(modal);
        document.body.appendChild(overlay);

        let isResolved = false;

        // ---------- CANCEL ----------
        document.getElementById('overwriteCancelBtn').addEventListener('click', function() {
            if (isResolved) return;
            isResolved = true;
            document.body.removeChild(overlay);
            resolve(false);
        });

        // ---------- CONFIRM (with progress) ----------
        document.getElementById('overwriteConfirmBtn').addEventListener('click', function() {
            if (isResolved) return;

            const confirmBtn = this;
            const progressArea = document.getElementById('overwriteProgressArea');
            const progressBar = document.getElementById('overwriteProgressBar');
            const progressText = document.getElementById('overwriteProgressText');

            // Show progress area
            progressArea.style.display = 'block';
            confirmBtn.disabled = true;
            confirmBtn.innerHTML = '<span class="spinner"></span> Overwriting...';
            confirmBtn.style.background = '#94a3b8';

            // Simulate progress (will be updated by the calling function)
            // We'll resolve after the import is done, but we keep the modal open
            // The progress will be updated from the import function

            // Store reference to the modal elements for the import function
            window._overwriteModal = {
                overlay: overlay,
                confirmBtn: confirmBtn,
                progressBar: progressBar,
                progressText: progressText,
                progressArea: progressArea,
                total: count,
                resolve: resolve,
                isResolved: isResolved
            };

            // Resolve with true – the import will handle the rest
            isResolved = true;
            resolve(true);
        });

        // Close on overlay click
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay && !isResolved) {
                isResolved = true;
                document.body.removeChild(overlay);
                resolve(false);
            }
        });
    });
}
// ---------- IMPORT (UPLOAD TO SERVER) ----------
if (importBtn) {
    importBtn.addEventListener('click', async function() {
        // Ensure client is selected
        let clientId = null;
        if (typeof window.isClientSelected === 'function' && window.isClientSelected()) {
            clientId = window.getSelectedClientId();
        } else {
            clientId = localStorage.getItem('selectedClientId');
        }
        if (!clientId) {
            showStatus('⚠️ Please select a client first from the Dashboard.', 'error');
            return;
        }
        if (!fileId || !selectedTemplateId) {
            showStatus('⚠️ Please upload a file and select a template.', 'error');
            return;
        }

        if (!mappingBody) return;
        const rows = mappingBody.querySelectorAll('tr');
        const finalMappings = [];
        rows.forEach(tr => {
            const select = tr.querySelector('select');
            const templateColName = tr.children[0].textContent;
            const mapping = currentMappings.find(m => m.column_name === templateColName);
            if (mapping) {
                finalMappings.push({
                    template_column_id: mapping.column_id,
                    template_column_name: mapping.column_name,
                    excel_column_name: select.value || null,
                    matched: !!select.value,
                    mapping_status: select.value ? 'matched' : 'unmatched'
                });
            }
        });

        const payload = {
            template_id: selectedTemplateId,
            file_id: fileId,
            client_id: parseInt(clientId),
            mappings: finalMappings,
            mode: 'create',
            unique_column_id: null,
            sheet_name: currentSheet || 'Sheet1',
            // Selected date format (Phase 3 dropdowns) – drives Date validation/import parsing on the backend.
            date_delimiter: selectedDateDelimiter,
            date_order: selectedDateOrder,
            // Rows the user unchecked in the row selection grid (Phase 2 of row-exclusion) –
            // Excel row numbers, matches routers/import_export.py's excluded_rows contract.
            excluded_rows: Array.from(excludedRowNumbers),
            // Rows explicitly ticked "Import anyway" in the Exceptions -- Rejected Rows list
            // (Phase 4 of the blank-field-rejection requirement) -- rebuilt fresh on every
            // click, so overrides ticked before or after the Validate call are both picked up.
            force_include_rows: Array.from(rejectedRowOverrides)
        };

        // ---- NEW LOGIC: Validation Flow ----
        // If validation status is already 'valid', proceed with import
        if (validationStatus === 'valid') {
            // Perform the actual import
            await performImport(payload);
            return;
        }

        // Otherwise, run validation first
        try {
            // Disable button and show spinner
            importBtn.disabled = true;
            importBtn.innerHTML = '<span class="spinner"></span> Validating...';
            showStatus('⏳ Validating data...', 'loading');
            setBadge('⏳ Validating...', 'loading');

            const res = await fetch(API_BASE + '/api/upload/validate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const result = await res.json();
            if (!res.ok) throw new Error(result.message || 'Validation failed');

            // Rejected-rows exceptions list (Phase 3 of the row-level blank-field-rejection
            // requirement) -- populated regardless of has_errors, since a row can be rejected
            // for a blank Document ID/Amount/Date independently of other rows' validation errors.
            setRejectedRows(result.rejected_rows);

            if (result.has_errors) {
                // Show validation errors
                showValidationErrors(result);
                // Mark the failing rows in the row selection grid (Phase 4) -- distinct from
                // user-excluded rows, and does not auto-exclude them.
                setValidationFailedRows(result.errors);
                validationStatus = 'invalid';
                // Reset button state
                importBtn.innerHTML = 'Validate';
                importBtn.disabled = false;
                importBtn.style.background = '#22c55e'; // Keep green but maybe orange? We'll keep default.
                showStatus('❌ Validation failed. Please fix errors and try again.', 'error');
                setBadge('❌ Validation failed', 'error');
                return;
            }

                                    // Validation passed – clear any stale failed-row markers from a
            // prior attempt (Phase 4), then show simple success message
            setValidationFailedRows([]);
            // Pre-import Rate mode visibility (Phase D) -- reflects this Validate call's
            // resolved Auto/Forced Imported/Derived decision until the next mapping change.
            lastRateModeInfo = result.rate_mode || null;
            renderRateModeIndicator();
            const totalRows = result.total_rows || 0;
            const newRowsCount = result.new_rows_count || 0;
            const existingRowsCount = result.existing_rows_count || 0;
            validationStatus = 'valid';
            importBtn.innerHTML = '🚀 Upload';
            importBtn.style.background = '#16a34a';
            importBtn.style.color = 'white';
            importBtn.disabled = false;
            let statusMsg = `✅ Validation passed! ${totalRows} rows identified. Click "Upload" to import all rows.`;
            const extraMsg = `<span style="color: maroon; font-weight: bold;">${newRowsCount} new row(s) • ${existingRowsCount} already exists in client data</span>`;
            showStatus(statusMsg, 'success', extraMsg);
            setBadge('✅ Data valid, ready to upload');
            updatePreviewButtonState();
            // Validation succeeded -- this is the genuine "ready to import" milestone the step
            // strip's final step represents (confirmed with the user: mapping-resolved alone
            // isn't distinct enough, since it fires at the same instant Import becomes enabled).
            updateStepIndicator(4);

        } catch (error) {
            showStatus('❌ Validation error: ' + error.message, 'error');
            setBadge('❌ Validation failed', 'error');
            importBtn.innerHTML = 'Validate';
            importBtn.disabled = false;
            importBtn.style.background = '';
            validationStatus = 'invalid';
        }
    });
}
// ---------- PERFORM IMPORT (after validation) ----------
// ---------- PERFORM IMPORT (after validation) ----------
async function performImport(payload) {
    try {
        importBtn.disabled = true;
        importBtn.innerHTML = 'Processing...';
        showStatus('⏳ Importing data...', 'loading');
        setBadge('⏳ Importing...', 'loading');
        showImportProgressBar();
        pollImportProgress(payload.file_id);

        // --- FIX: Add skip_validation if validation already passed ---
        if (validationStatus === 'valid') {
            payload.skip_validation = true;
        }

                // No duplicate check – we always insert new rows (simple insert)
        let proceed = true;

        if (!proceed) return;

        // Perform the actual import
        const res = await fetch(API_BASE + '/api/upload/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.message || 'Import failed');

        // --- Handle overwrite progress (if modal is open) ---
        const modalRef = window._overwriteModal;
        if (modalRef && !modalRef.isResolved) {
            const updated = result.updated_count || 0;
            const total = modalRef.total || 1;
            const pct = Math.min(100, Math.round((updated / total) * 100));
            modalRef.progressBar.style.width = pct + '%';
            modalRef.progressText.textContent = `${updated} of ${total} rows overwritten`;

            if (updated >= total) {
                modalRef.confirmBtn.innerHTML = 'OK';
                modalRef.confirmBtn.style.background = '#16a34a';
                modalRef.confirmBtn.disabled = false;
                modalRef.progressBar.style.background = '#16a34a';
                modalRef.progressText.textContent = `✅ ${updated} of ${total} rows overwritten`;

                modalRef.confirmBtn.onclick = function() {
                    if (modalRef.overlay && modalRef.overlay.parentNode) {
                        document.body.removeChild(modalRef.overlay);
                    }
                    modalRef.resolve(true);
                    window._overwriteModal = null;
                    showImportSummary(result);
                };
            }
        }

        // Rejected-rows exceptions list (Phase 3), same reasoning as the validate-response
        // handler above -- populated regardless of status, kept in sync with the latest response.
        setRejectedRows(result.rejected_rows);

        if (result.status === 'validation_failed') {
            showValidationErrors(result);
            // Mark the failing rows in the row selection grid (Phase 4), same as the
            // pre-import validate path -- does not auto-exclude them.
            setValidationFailedRows(result.errors);
            showStatus('❌ Import validation errors found.', 'error');
            setBadge('❌ Import failed', 'error');
        } else {
            if (!modalRef || modalRef.isResolved) {
                showImportSummary(result);
            }
            setBadge('✅ Import complete');
        }
    } catch (error) {
        showStatus('❌ Import error: ' + error.message, 'error');
        setBadge('❌ Import failed', 'error');
    } finally {
        clearInterval(importProgressPollInterval);
        hideImportProgressBar();
        importBtn.disabled = false;
        importBtn.innerHTML = '🚀 Upload';
    }
}

function renderPreviewPage() {
    const previewHead = document.getElementById('previewSheetHead');
    const previewBody = document.getElementById('previewSheetBody');
    const previewInfo = document.getElementById('previewInfo');
    const previewRowRange = document.getElementById('previewRowRange');
    const previewPageInfo = document.getElementById('previewPageInfo');
    const previewFirstPage = document.getElementById('previewFirstPage');
    const previewPrevPage = document.getElementById('previewPrevPage');
    const previewNextPage = document.getElementById('previewNextPage');
    const previewLastPage = document.getElementById('previewLastPage');

    const headers = previewHeaders || [];
    const totalRows = previewAllRows.length;
    previewTotalRows = totalRows;

    // Calculate pagination details
    const totalPages = Math.ceil(totalRows / previewPageSize) || 1;
    if (previewCurrentPage > totalPages) {
        previewCurrentPage = totalPages;
    }
    if (previewCurrentPage < 1) {
        previewCurrentPage = 1;
    }

    const startIndex = (previewCurrentPage - 1) * previewPageSize;
    const endIndex = Math.min(startIndex + previewPageSize, totalRows);

    // Build headers
    previewHead.innerHTML = '';
    if (headers.length) {
        const tr = document.createElement('tr');
        
        // Add S.No header column as the first column with specified styling
        const thSNo = document.createElement('th');
        thSNo.textContent = 'S.No';
        thSNo.style.textAlign = 'center';
        thSNo.style.color = '#64748b';
        thSNo.style.width = '50px';
        thSNo.style.fontWeight = '500';
        tr.appendChild(thSNo);

        headers.forEach(h => {
            const th = document.createElement('th');
            th.textContent = h || '—';
            tr.appendChild(th);
        });
        previewHead.appendChild(tr);
    }

    // Build body for current page rows
    previewBody.innerHTML = '';
    const pageRows = previewAllRows.slice(startIndex, endIndex);

    pageRows.forEach((row, index) => {
        const tr = document.createElement('tr');
        const rowIndex = startIndex + index; // Actual 0-based index in full Excel sheet
        
        // Add S.No serial number cell (starting from 1) before all other cells
        const tdSNo = document.createElement('td');
        tdSNo.textContent = rowIndex + 1;
        tdSNo.style.textAlign = 'center';
        tdSNo.style.color = '#64748b';
        tdSNo.style.width = '50px';
        tdSNo.style.fontWeight = '500';
        tr.appendChild(tdSNo);

        row.forEach((cell, idx) => {
            const td = document.createElement('td');
            let displayValue = cell;
            const headerName = (headers[idx] || '').toLowerCase();
            if (typeof cell === 'number' && cell > 30000 && 
                (headerName.includes('date') || headerName.includes('day') || headerName.includes('month'))) {
                const date = new Date((cell - 25569) * 86400 * 1000);
                if (!isNaN(date)) {
                    const d = String(date.getDate()).padStart(2, '0');
                    const m = String(date.getMonth() + 1).padStart(2, '0');
                    const y = date.getFullYear();
                    displayValue = d + '/' + m + '/' + y;
                }
            } else if (typeof cell === 'string' && /^\d{1,2}\/\d{1,2}\/\d{2,4}$/.test(cell)) {
                const parts = cell.split('/');
                if (parts.length === 3) {
                    let day, month, year;
                    if (parseInt(parts[0]) > 12) {
                        day = parts[0];
                        month = parts[1];
                    } else {
                        month = parts[0];
                        day = parts[1];
                    }
                    year = parts[2];
                    if (year.length === 2) year = '20' + year;
                    displayValue = day.padStart(2, '0') + '/' + month.padStart(2, '0') + '/' + year;
                }
            }
            td.textContent = displayValue !== undefined ? displayValue : '';
            tr.appendChild(td);
        });
        previewBody.appendChild(tr);
    });

    // Update displays
    if (previewInfo) {
        previewInfo.textContent = `${totalRows} rows · ${headers.length} columns` +
            (previewHiddenCount > 0 ? ` (${previewHiddenCount} excluded)` : '');
    }
    if (previewRowRange) {
        if (totalRows === 0) {
            previewRowRange.textContent = 'Showing 0-0 of 0 rows';
        } else {
            previewRowRange.textContent = `Showing ${startIndex + 1}-${endIndex} of ${totalRows} rows`;
        }
    }
    if (previewPageInfo) {
        previewPageInfo.textContent = `Page ${previewCurrentPage} of ${totalPages}`;
    }

    // Disable buttons at boundaries
    if (previewFirstPage) {
        previewFirstPage.disabled = (previewCurrentPage === 1);
    }
    if (previewPrevPage) {
        previewPrevPage.disabled = (previewCurrentPage === 1);
    }
    if (previewNextPage) {
        previewNextPage.disabled = (previewCurrentPage === totalPages);
    }
    if (previewLastPage) {
        previewLastPage.disabled = (previewCurrentPage === totalPages);
    }
}

// ---------- ROW SELECTION GRID (Phase 2 of the row-exclusion requirement) ----------
// Reads the current sheet's raw rows the same way showPreview() does,
// and (re)builds the checkbox-per-row grid. Called once column mapping succeeds (including
// on a sheet change, via autoMapColumns()'s success path) -- a fresh excludedRowNumbers Set
// here is correct since the underlying rows genuinely differ. A per-column remap that
// doesn't change sheets uses the lighter resetRowExclusionSelections() instead (Phase 4).
function renderRowExclusionGrid() {
    const container = document.getElementById('rowExclusionContainer');
    if (!container || !workbookData) return;

    try {
        const workbook = XLSX.read(workbookData, { type: 'array' });
        const sheetName = currentSheet || workbook.SheetNames[0];
        const sheet = workbook.Sheets[sheetName];
        // defval: '' -- same fix as showPreview(): avoids holes in the row array so
        // renderRowExclusionPage()'s row.forEach() doesn't skip blank cells and shift
        // later columns left.
        const jsonData = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: '' });

        rowExclusionHeaders = jsonData[0] || [];
        rowExclusionAllRows = jsonData.slice(1);
        excludedRowNumbers = new Set();
        validationFailedRows = new Map(); // new sheet/mapping -- any prior validation result is stale
        rejectedRowsData = new Map(); // new sheet/mapping -- any prior rejected-rows result is stale
        rejectedRowOverrides = new Set();
        renderRejectedRowsList();
        rowExclusionCurrentPage = 1;

        // Re-apply a restored session's excluded rows (session-restore), bounds-checked against
        // this file's actual row count -- consumed once, then cleared.
        if (pendingExcludedRowNumbersToRestore) {
            const maxRowNumber = rowExclusionAllRows.length + 1; // header=1, so last data row = length+1
            excludedRowNumbers = new Set(
                pendingExcludedRowNumbersToRestore.filter(n => n >= 2 && n <= maxRowNumber)
            );
            pendingExcludedRowNumbersToRestore = null;
        }

        container.style.display = 'block';
        renderRowExclusionPage();
    } catch (error) {
        console.error('Row selection grid error:', error);
        container.style.display = 'none';
    }
}

// ---------- AUTO-FLAG HEURISTIC (Phase 3 of the row-exclusion requirement) ----------
// A visual hint only -- never unchecks a row automatically, the user always decides.
// Key column = the first mapped template column (currentMappings[0]), mirroring the
// app-wide convention that the first template column is the document's unique key
// (see get_document_group_id() / the Conventions note in CLAUDE.md).
function getRowExclusionKeyColumnIndex() {
    if (!currentMappings || currentMappings.length === 0) return -1;
    const keyMapping = currentMappings[0];
    if (!keyMapping || !keyMapping.excel_column) return -1;
    return (rowExclusionHeaders || []).indexOf(keyMapping.excel_column);
}

// Flags a row when the key column is blank, or its text contains "total" (case-insensitive) --
// this single substring check also naturally catches "subtotal"/"grand total". Simple and
// transparent by design, no fuzzy matching.
function isRowExclusionFlagged(row, keyColIndex) {
    if (keyColIndex < 0) return false;
    const value = row[keyColIndex];
    if (value === undefined || value === null || String(value).trim() === '') return true;
    return String(value).toLowerCase().includes('total');
}

// ---------- LIVE "X of Y rows will be imported" COUNTER (Phase 3) ----------
// Always reflects the whole dataset (not just the visible page).
function updateRowExclusionCounter() {
    const counterEl = document.getElementById('rowExclusionCounter');
    if (!counterEl) return;
    const total = rowExclusionAllRows.length;
    const included = total - excludedRowNumbers.size;
    counterEl.textContent = `${included} of ${total} rows will be imported`;
}

// ---------- SELECT ALL / DESELECT ALL (Phase 3) ----------
// Operate on the full excludedRowNumbers Set, so they apply across all pages, not
// just the currently visible one; a single re-render refreshes the visible page + counter.
function rowExclusionSelectAll() {
    excludedRowNumbers.clear();
    renderRowExclusionPage();
    saveUploadSessionState();
}

function rowExclusionDeselectAll() {
    const total = rowExclusionAllRows.length;
    excludedRowNumbers = new Set();
    for (let i = 0; i < total; i++) {
        excludedRowNumbers.add(i + 2); // Excel row numbers: first data row = 2
    }
    renderRowExclusionPage();
    saveUploadSessionState();
}

// ---------- RESET ON MAPPING CHANGE (Phase 4 of the row-exclusion requirement) ----------
// Sheet changes already reset the grid correctly via renderRowExclusionGrid() (called from
// autoMapColumns()'s success path), since the underlying rows genuinely differ. A per-column
// remap in the mapping table doesn't change the sheet's rows -- only which column feeds the
// auto-flag heuristic/validation -- so this is a lighter reset: clear selections/flags and
// re-render, without re-reading the file.
function resetRowExclusionSelections() {
    if (!rowExclusionAllRows.length) return; // grid not built yet, nothing to reset
    excludedRowNumbers = new Set();
    validationFailedRows = new Map(); // mapping changed -- any prior validation result is stale
    rejectedRowsData = new Map(); // mapping changed -- any prior rejected-rows result is stale
    rejectedRowOverrides = new Set();
    renderRejectedRowsList();
    rowExclusionCurrentPage = 1;
    renderRowExclusionPage();
    saveUploadSessionState(); // Row-exclusion selections just cleared -- keep the saved session in sync
}

// ---------- VALIDATION-FAILED ROWS (Phase 4) ----------
// Captures the validate/import response's errors[] (row_number + errors) so the grid can
// show which rows failed, distinct from which rows the user chose to exclude. Never touches
// excludedRowNumbers -- a validation failure must not auto-exclude a row.
function setValidationFailedRows(errorsList) {
    validationFailedRows = new Map();
    (errorsList || []).forEach(e => {
        validationFailedRows.set(e.row_number, e.errors || []);
    });
    if (document.getElementById('rowExclusionContainer') && rowExclusionAllRows.length) {
        renderRowExclusionPage();
    }
}

// ---------- EXCEPTIONS LIST -- REJECTED ROWS (Phase 3 of the row-level blank-field-rejection
// requirement) ----------
// Captures a validate/import response's rejected_rows[] (rows the backend excluded because a
// Document ID/Amount/Date was blank -- routers/import_export.py's find_critical_blank_reasons()).
// Called from both the validate-response and import-response handlers, same as
// setValidationFailedRows() above.
function setRejectedRows(rejectedRowsList) {
    rejectedRowsData = new Map();
    (rejectedRowsList || []).forEach(r => {
        rejectedRowsData.set(r.row_number, r.reasons || []);
    });
    // An override for a row that's no longer rejected (e.g. the file was fixed and re-validated)
    // is meaningless -- prune it so a stale override can't silently resurface later.
    rejectedRowOverrides.forEach(rowNum => {
        if (!rejectedRowsData.has(rowNum)) rejectedRowOverrides.delete(rowNum);
    });
    renderRejectedRowsList();
}

// Renders one item per rejected row into #rowRejectionContainer -- deliberately a separate,
// distinctly-styled panel from the Row Selection grid (see .row-rejection-* in upload.html),
// not folded into that grid's own excluded/flagged/validation-failed states. Toggling a row's
// checkbox only updates rejectedRowOverrides this phase -- wiring it into the actual
// validate/import payload is Phase 4.
function renderRejectedRowsList() {
    const container = document.getElementById('rowRejectionContainer');
    const listEl = document.getElementById('rowRejectionList');
    const countEl = document.getElementById('rowRejectionCount');
    if (!container || !listEl) return;

    if (rejectedRowsData.size === 0) {
        container.style.display = 'none';
        listEl.innerHTML = '';
        if (countEl) countEl.textContent = '0 row(s) rejected';
        return;
    }

    container.style.display = 'block';
    if (countEl) countEl.textContent = `${rejectedRowsData.size} row(s) rejected`;

    listEl.innerHTML = '';
    // Sort by row number so the list reads top-to-bottom in file order.
    const sortedRowNumbers = Array.from(rejectedRowsData.keys()).sort((a, b) => a - b);
    sortedRowNumbers.forEach(rowNum => {
        const reasons = rejectedRowsData.get(rowNum) || [];

        const item = document.createElement('div');
        item.className = 'row-rejection-item';

        const rowNumEl = document.createElement('span');
        rowNumEl.className = 'row-rejection-rownum';
        rowNumEl.textContent = `Row ${rowNum}`;
        item.appendChild(rowNumEl);

        const reasonEl = document.createElement('span');
        reasonEl.className = 'row-rejection-reason';
        // Reuses the backend's own wording verbatim -- no re-derivation of the message.
        reasonEl.textContent = reasons.map(r => r.message).join('; ');
        item.appendChild(reasonEl);

        const label = document.createElement('label');
        label.className = 'row-rejection-override-label';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = rejectedRowOverrides.has(rowNum);
        checkbox.addEventListener('change', function() {
            if (this.checked) {
                rejectedRowOverrides.add(rowNum);
            } else {
                rejectedRowOverrides.delete(rowNum);
            }
        });
        const fieldNames = reasons.map(r => r.column).join(', ');
        const labelText = document.createElement('span');
        labelText.textContent = `Import anyway (with blank in ${fieldNames})`;
        label.appendChild(checkbox);
        label.appendChild(labelText);
        item.appendChild(label);

        listEl.appendChild(item);
    });
}

function renderRowExclusionPage() {
    const headRow = document.getElementById('rowExclusionHeadRow');
    const body = document.getElementById('rowExclusionBody');
    const rangeEl = document.getElementById('rowExclusionRange');
    const pageInfoEl = document.getElementById('rowExclusionPageInfo');
    const firstBtn = document.getElementById('rowExclusionFirstPage');
    const prevBtn = document.getElementById('rowExclusionPrevPage');
    const nextBtn = document.getElementById('rowExclusionNextPage');
    const lastBtn = document.getElementById('rowExclusionLastPage');
    if (!headRow || !body) return;

    const headers = rowExclusionHeaders || [];
    const totalRows = rowExclusionAllRows.length;

    const totalPages = Math.ceil(totalRows / ROW_EXCLUSION_PAGE_SIZE) || 1;
    if (rowExclusionCurrentPage > totalPages) rowExclusionCurrentPage = totalPages;
    if (rowExclusionCurrentPage < 1) rowExclusionCurrentPage = 1;

    const startIndex = (rowExclusionCurrentPage - 1) * ROW_EXCLUSION_PAGE_SIZE;
    const endIndex = Math.min(startIndex + ROW_EXCLUSION_PAGE_SIZE, totalRows);

    // Auto-flag heuristic key column (Phase 3) -- computed once per page render
    const keyColIndex = getRowExclusionKeyColumnIndex();

    // Build header: checkbox col + Row col + one <th> per Excel column
    headRow.innerHTML = '';
    const thCheck = document.createElement('th');
    thCheck.className = 'row-exclusion-check-col';
    // "Mother" checkbox (Fix 1): checked = all rows included, unchecked = all excluded,
    // indeterminate = a partial selection -- standard select-all convention (e.g. Gmail).
    // Reflects the whole dataset, not just the visible page, same scope as
    // rowExclusionSelectAll()/rowExclusionDeselectAll() which it now triggers.
    const masterCheckbox = document.createElement('input');
    masterCheckbox.type = 'checkbox';
    const totalForMaster = rowExclusionAllRows.length;
    const excludedCount = excludedRowNumbers.size;
    masterCheckbox.checked = totalForMaster > 0 && excludedCount === 0;
    masterCheckbox.indeterminate = excludedCount > 0 && excludedCount < totalForMaster;
    masterCheckbox.title = 'Select all / deselect all rows';
    masterCheckbox.addEventListener('change', function() {
        if (this.checked) {
            rowExclusionSelectAll();
        } else {
            rowExclusionDeselectAll();
        }
    });
    thCheck.appendChild(masterCheckbox);
    headRow.appendChild(thCheck);
    const thRowNum = document.createElement('th');
    thRowNum.className = 'row-exclusion-rownum-col';
    thRowNum.textContent = 'Row';
    headRow.appendChild(thRowNum);
    headers.forEach(h => {
        const th = document.createElement('th');
        th.textContent = h || '—';
        headRow.appendChild(th);
    });

    // Build body for current page
    body.innerHTML = '';
    const pageRows = rowExclusionAllRows.slice(startIndex, endIndex);
    pageRows.forEach((row, index) => {
        // jsonData[0] is the header (Excel row 1), jsonData[1] is the first data row
        // (Excel row 2) -- so array index i (0-based, after slicing off the header) is
        // Excel row i + 2. Matches routers/import_export.py's excluded_rows scheme.
        const excelRowNumber = startIndex + index + 2;

        const tr = document.createElement('tr');
        if (excludedRowNumbers.has(excelRowNumber)) tr.classList.add('row-excluded');
        const flagged = isRowExclusionFlagged(row, keyColIndex);
        if (flagged) tr.classList.add('row-flagged');
        const failed = validationFailedRows.has(excelRowNumber);
        if (failed) tr.classList.add('row-validation-failed');

        const tdCheck = document.createElement('td');
        tdCheck.className = 'row-exclusion-check-col';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = !excludedRowNumbers.has(excelRowNumber);
        checkbox.addEventListener('change', function() {
            if (this.checked) {
                excludedRowNumbers.delete(excelRowNumber);
                tr.classList.remove('row-excluded');
            } else {
                excludedRowNumbers.add(excelRowNumber);
                tr.classList.add('row-excluded');
            }
            updateRowExclusionCounter();
            saveUploadSessionState();
        });
        tdCheck.appendChild(checkbox);
        tr.appendChild(tdCheck);

        const tdRowNum = document.createElement('td');
        tdRowNum.className = 'row-exclusion-rownum-col';
        // Prefixes concatenate rather than overwrite -- a row can be both auto-flagged and
        // validation-failed at once, and both signals should stay visible (e.g. "⚠️❌ 5").
        let prefix = (flagged ? '⚠️' : '') + (failed ? '❌' : '');
        tdRowNum.textContent = (prefix ? prefix + ' ' : '') + excelRowNumber;
        const titleParts = [];
        if (flagged) titleParts.push('Possible junk row: blank or "total"/"subtotal" value in the key column');
        if (failed) {
            const rowErrors = validationFailedRows.get(excelRowNumber) || [];
            const msg = rowErrors.map(e => `${e.column}: ${e.message}`).join('; ');
            titleParts.push('Failed validation' + (msg ? ': ' + msg : ''));
        }
        if (titleParts.length) tdRowNum.title = titleParts.join(' | ');
        tr.appendChild(tdRowNum);

        row.forEach((cell, idx) => {
            const td = document.createElement('td');
            td.textContent = cell !== undefined ? cell : '';
            tr.appendChild(td);
        });

        body.appendChild(tr);
    });

    if (rangeEl) {
        rangeEl.textContent = totalRows === 0
            ? 'Showing 0-0 of 0 rows'
            : `Showing ${startIndex + 1}-${endIndex} of ${totalRows} rows`;
    }
    if (pageInfoEl) {
        pageInfoEl.textContent = `Page ${rowExclusionCurrentPage} of ${totalPages}`;
    }
    if (firstBtn) firstBtn.disabled = (rowExclusionCurrentPage === 1);
    if (prevBtn) prevBtn.disabled = (rowExclusionCurrentPage === 1);
    if (nextBtn) nextBtn.disabled = (rowExclusionCurrentPage === totalPages);
    if (lastBtn) lastBtn.disabled = (rowExclusionCurrentPage === totalPages);

    updateRowExclusionCounter();
}

function showPreview() {
    const previewSheet = document.getElementById('previewSheet');
    const mainUploadPage = document.getElementById('mainUploadPage');

    if (!workbookData) {
        showStatus('⚠️ No file loaded to preview.', 'error');
        return;
    }

    try {
        const workbook = XLSX.read(workbookData, { type: 'array' });
        const sheetName = currentSheet || workbook.SheetNames[0];
        const sheet = workbook.Sheets[sheetName];
        // defval: '' forces every column slot to exist even when a cell is blank --
        // without it, SheetJS leaves a hole in the row array, which row.forEach() below
        // silently skips, shifting every later value one column to the left.
        const jsonData = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: '' });

        previewHeaders = jsonData[0] || [];
        const rawDataRows = jsonData.slice(1); // All rows, not limited
        // Show only rows the user hasn't excluded in the Row Selection grid, so the
        // preview reflects what will actually be imported. Same Excel row-number scheme
        // as excludedRowNumbers / routers/import_export.py's excluded_rows (header=1,
        // first data row=2). S.No is renumbered sequentially over the filtered set by
        // renderPreviewPage() (unchanged) since excluded rows are gone entirely, not
        // just hidden -- a clean list of what will be imported, not a gapped one.
        previewAllRows = rawDataRows.filter((_, idx) => !excludedRowNumbers.has(idx + 2));
        previewHiddenCount = rawDataRows.length - previewAllRows.length;
        previewCurrentPage = 1;

        // Render current page
        renderPreviewPage();

        // Update footer with client and user info
        const clientName = window.getSelectedClientName ? window.getSelectedClientName() : 'No Client';
        const clientCode = window.getSelectedClientCode ? window.getSelectedClientCode() : '';
        const userName = 'Analyst';
        const now = new Date();
        const timeStr = now.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
        const dateStr = now.toLocaleDateString('en-GB', { day: '2-digit', month: '2-digit', year: 'numeric' });

        document.getElementById('footerClientName').textContent = clientCode ? `${clientName} (${clientCode})` : clientName;
        document.getElementById('footerUserName').textContent = userName;
        document.getElementById('footerTimeValue').textContent = timeStr;
        document.getElementById('footerDateValue').textContent = dateStr;

        // Show preview sheet, hide main page
        previewSheet.classList.add('active');
        mainUploadPage.style.display = 'none';
        // Set container to fixed height
        const container = document.getElementById('uploadContainer');
        if (container) container.classList.add('preview-active');

    } catch (error) {
        showStatus('❌ Error previewing file: ' + error.message, 'error');
    }
}

// ---------- CLOSE PREVIEW ----------
const closePreviewBtn = document.getElementById('closePreviewBtn');
if (closePreviewBtn) {
    closePreviewBtn.addEventListener('click', function(e) {
        e.preventDefault();
        document.getElementById('previewSheet').classList.remove('active');
        document.getElementById('mainUploadPage').style.display = 'block';
        // Remove fixed height from container
        const container = document.getElementById('uploadContainer');
        if (container) container.classList.remove('preview-active');
        // Re-enable preview button after closing (state already managed)
        updatePreviewButtonState();
    });
}

// ---------- PREVIEW BUTTON ----------
const previewBtn = document.getElementById('previewBtn');
if (previewBtn) {
    previewBtn.addEventListener('click', function() {
        if (!workbookData) {
            showStatus('⚠️ Please upload a file first.', 'error');
            return;
        }
        if (!currentSheet) {
            showStatus('⚠️ Please select a sheet.', 'error');
            return;
        }
        if (!selectedTemplateId) {
            showStatus('⚠️ Please select a template.', 'error');
            return;
        }
        if (currentMappings.length === 0) {
            showStatus('⚠️ Please wait for auto-mapping to complete.', 'error');
            return;
        }
        // --- NEW: Check validation ---
        if (validationStatus !== 'valid') {
            showStatus('⚠️ Please validate the data first (click "Validate" button).', 'error');
            return;
        }
        // All conditions satisfied
        showPreview();
    });
}

// ---------- INIT ----------
window.onload = async function() {
    // Await so the Template dropdown's <option>s exist before restoreUploadSessionState()
    // (session-restore) tries to set the dropdown's value.
    await loadTemplates();
    populateDateOrderDropdown(selectedDateDelimiter);
    // No initial placeholder status message on page load -- redundant hand-holding text; the
    // status bar stays hidden by default (#statusBar) and only appears once something actually
    // happens (a real error, success, or progress message via showStatus()).
    updatePreviewButtonState();

    // Session-restore: silently reapply a previously in-progress upload (Template/Sheet/Date
    // format/Row-exclusion selections) if the user navigated away mid-upload and back within
    // this browser tab. No-ops if nothing was saved. Overwrites the default status message
    // above with a "resuming..." message only when there's something to restore.
    await restoreUploadSessionState();

    // Set initial button text
    if (importBtn) {
        importBtn.innerHTML = 'Validate';
        importBtn.style.background = '';
        importBtn.style.color = '';
    }

    // Go to Data Bank shortcut (top bar)
    const goToDataBankBtn = document.getElementById('goToDataBankBtn');
    if (goToDataBankBtn) {
        goToDataBankBtn.addEventListener('click', function() {
            // Opens in a new tab (not window.location.href) so this Upload tab is never
            // navigated away from -- the loaded file, mapping, and row-exclusion state stay
            // alive in memory exactly as the user left them, no session-restore needed for
            // this path. The session-restore/locked-preview logic elsewhere in this file still
            // covers other ways of leaving the page (back button, closing the tab, navbar links).
            window.open('/static/databank.html', '_blank');
        });
    }

    // Pagination controls event listeners
    const previewPageSizeDropdown = document.getElementById('previewPageSize');
    if (previewPageSizeDropdown) {
        previewPageSizeDropdown.addEventListener('change', function() {
            previewPageSize = parseInt(this.value) || 50;
            previewCurrentPage = 1;
            renderPreviewPage();
        });
    }

    const previewFirstPageBtn = document.getElementById('previewFirstPage');
    if (previewFirstPageBtn) {
        previewFirstPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            if (previewCurrentPage > 1) {
                previewCurrentPage = 1;
                renderPreviewPage();
            }
        });
    }

    const previewPrevPageBtn = document.getElementById('previewPrevPage');
    if (previewPrevPageBtn) {
        previewPrevPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            if (previewCurrentPage > 1) {
                previewCurrentPage--;
                renderPreviewPage();
            }
        });
    }

    const previewNextPageBtn = document.getElementById('previewNextPage');
    if (previewNextPageBtn) {
        previewNextPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            const totalPages = Math.ceil(previewTotalRows / previewPageSize) || 1;
            if (previewCurrentPage < totalPages) {
                previewCurrentPage++;
                renderPreviewPage();
            }
        });
    }

    const previewLastPageBtn = document.getElementById('previewLastPage');
    if (previewLastPageBtn) {
        previewLastPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            const totalPages = Math.ceil(previewTotalRows / previewPageSize) || 1;
            if (previewCurrentPage < totalPages) {
                previewCurrentPage = totalPages;
                renderPreviewPage();
            }
        });
    }

    // Row selection grid pagination controls (Phase 2 of row-exclusion)
    const rowExclusionFirstPageBtn = document.getElementById('rowExclusionFirstPage');
    if (rowExclusionFirstPageBtn) {
        rowExclusionFirstPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            if (rowExclusionCurrentPage > 1) {
                rowExclusionCurrentPage = 1;
                renderRowExclusionPage();
            }
        });
    }

    const rowExclusionPrevPageBtn = document.getElementById('rowExclusionPrevPage');
    if (rowExclusionPrevPageBtn) {
        rowExclusionPrevPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            if (rowExclusionCurrentPage > 1) {
                rowExclusionCurrentPage--;
                renderRowExclusionPage();
            }
        });
    }

    const rowExclusionNextPageBtn = document.getElementById('rowExclusionNextPage');
    if (rowExclusionNextPageBtn) {
        rowExclusionNextPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            const totalPages = Math.ceil(rowExclusionAllRows.length / ROW_EXCLUSION_PAGE_SIZE) || 1;
            if (rowExclusionCurrentPage < totalPages) {
                rowExclusionCurrentPage++;
                renderRowExclusionPage();
            }
        });
    }

    const rowExclusionLastPageBtn = document.getElementById('rowExclusionLastPage');
    if (rowExclusionLastPageBtn) {
        rowExclusionLastPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            const totalPages = Math.ceil(rowExclusionAllRows.length / ROW_EXCLUSION_PAGE_SIZE) || 1;
            if (rowExclusionCurrentPage < totalPages) {
                rowExclusionCurrentPage = totalPages;
                renderRowExclusionPage();
            }
        });
    }

    // Select All / Deselect All are now a single "mother" checkbox in the grid's own
    // header row (built in renderRowExclusionPage()), not separate buttons -- no init
    // wiring needed here.
};
