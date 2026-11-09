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
const statusBadge = document.getElementById('statusBadge');
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
const comparisonModeDropdown = getElement('comparisonModeDropdown');
const uploadCoverageStart = getElement('uploadCoverageStart');
const uploadCoverageEnd = getElement('uploadCoverageEnd');
const uploadConfirmCoverage = getElement('uploadConfirmCoverage');
const documentDateSuggestion = getElement('documentDateSuggestion');
const uploadConfirmEmpty = getElement('uploadConfirmEmpty');
// Keep system-filled dates separate from manual choices so a preview refresh never overwrites a widened ERP period.
let lastAutoSuggestedCoverage = null;
let coverageManuallyEdited = false;
let datePreviewRequestGeneration = 0;
let uploadMappingGeneration = 0;
let rowExclusionNativeDateColumns = new Set();

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
let comparisonPreviewMode = false;
let comparisonPreviewRequest = null;
let comparisonPreviewAction = 'all';
let comparisonPreviewReturn = null;
let comparisonPreviewRows = [];
let comparisonPreviewColumns = [];
let comparisonPreviewStale = false;
let databasePreviewMode = false;
let databasePreviewContext = null;
let previewPageCache = new Map();
// File-specific footer confirmation is deliberately absent from saved mappings
// and restored sessions. The backend alone calculates reconciliation amounts.
let sourceControlResult = null;

function sourceFootingMappings() {
    return currentMappings.filter(mapping => mapping.reconciliation_role === 'footing');
}

function confirmedSourceControlRow() {
    const row = Number(getElement('sourceControlRow')?.value);
    return sourceFootingMappings().length && getElement('sourceControlConfirmed')?.checked
        && Number.isInteger(row) && row >= 2 && row <= rowExclusionAllRows.length + 1 ? row : null;
}

function resetSourceControlChoice(suggest = false) {
    sourceControlResult = null;
    clearCoverageConfirmation();
    const input = getElement('sourceControlRow');
    const confirmed = getElement('sourceControlConfirmed');
    if (input) input.value = '';
    clearUploadFieldError(input, getElement('sourceControlPreview'));
    if (confirmed) confirmed.checked = false;
    if (suggest && sourceFootingMappings().length) {
        const keyIndex = getRowExclusionKeyColumnIndex();
        const candidates = [];
        rowExclusionAllRows.forEach((row, index) => {
            const key = String(row[keyIndex] ?? '').trim();
            if ((!key || /^(?:grand\s*)?total\s*:?$/i.test(key))
                && row.some(value => /^(?:grand\s*)?total\s*:?$/i.test(String(value).trim()))) candidates.push(index + 2);
        });
        if (candidates.length === 1 && input) input.value = candidates[0];
    }
    renderSourceControls();
}

function renderSourceControls(result = sourceControlResult) {
    sourceControlResult = result;
    const panel = getElement('sourceControlPanel');
    if (!panel) return;
    const footings = sourceFootingMappings();
    panel.hidden = !footings.length || !workbookData;
    if (panel.hidden) return;
    getElement('sourceControlHelp').textContent = `Footing: ${footings.map(m => m.column_name).join(' · ')}. Required: ERP total or an Excel SUM total. Header is row 1.`;
    const rowNumber = Number(getElement('sourceControlRow').value);
    const row = rowExclusionAllRows[rowNumber - 2];
    const preview = getElement('sourceControlPreview');
    preview.hidden = false;
    preview.textContent = row
        ? `Row ${rowNumber}: ${footings.map(m => `${m.column_name}: ${formatSourceControlAmount(row[rowExclusionHeaders.indexOf(m.excel_column)] ?? 'Blank')}`).join(' · ')}`
        : 'Choose the Excel row containing the report totals, then confirm it.';
    const results = getElement('sourceControlResults');
    results.replaceChildren();
    if (!result) return;
    const message = document.createElement('p');
    message.dataset.status = result.status;
    const statusMessages = {
        matched: 'Source totals exactly match the full sheet detail.',
        rounding_tolerated: 'A difference below 0.01 was accepted as rounding noise; the exact amounts are preserved.',
        confirmation_required: 'The difference is within 1.00 currency unit and needs explicit confirmation before import.',
        confirmed_variance: 'The variance was confirmed. Exact source and detail amounts are preserved in the upload record.',
        mismatch: 'The difference exceeds 1.00 currency unit. Correct the Excel workbook with client management before import.'
    };
    message.textContent = statusMessages[result.status] || 'Source totals could not be reconciled.';
    results.appendChild(message);
    const container = document.createElement('div'); container.className = 'source-control-table';
    const table = document.createElement('table');
    const head = table.createTHead().insertRow();
    ['Field', 'Excel total', 'Detail total', 'Difference'].forEach(label => {
        const th = document.createElement('th'); th.textContent = label; head.appendChild(th);
    });
    const body = table.createTBody();
    (result.checks || []).forEach(check => {
        const tr = body.insertRow();
        [check.column, check.excel_total, check.detail_total, check.difference].forEach((value, index) => {
            tr.insertCell().textContent = index ? formatSourceControlAmount(value) : value;
        });
    });
    container.appendChild(table); results.appendChild(container);
    const summary = document.createElement('p');
    summary.textContent = `Full sheet detail: ${result.full_sheet.row_count} rows · ${result.full_sheet.document_count} unique documents.`;
    if (result.approved_detail) {
        summary.textContent += ` Approved for import: ${result.approved_detail.row_count} rows · ${result.approved_detail.document_count} unique documents.`;
        if (result.omitted_detail_rows) summary.textContent += ` Approved totals: ${Object.entries(result.approved_detail.footing_totals || {}).map(([name, value]) => `${name}: ${formatSourceControlAmount(value)}`).join(' · ')}.`;
        if (!result.scope?.complete_period) summary.textContent += ' Partial extract: full-period database equality is not established.';
    }
    results.appendChild(summary);
}

function formatSourceControlAmount(value) {
    // Group digits without floating-point conversion or presentation rounding.
    const text = String(value);
    if (!/^[+-]?\d+(?:\.\d+)?$/.test(text)) return text;
    const [whole, fraction] = text.split('.');
    return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (fraction === undefined ? '' : `.${fraction}`);
}

getElement('sourceControlRow')?.addEventListener('input', () => {
    clearUploadFieldError(getElement('sourceControlRow'), getElement('sourceControlPreview'));
    getElement('sourceControlConfirmed').checked = false;
    clearCoverageConfirmation();
    resetValidationState();
    renderSourceControls(null); renderRowExclusionPage(); refreshDateFormatPreview();
});
getElement('sourceControlConfirmed')?.addEventListener('change', () => {
    clearUploadFieldError(getElement('sourceControlRow'), getElement('sourceControlPreview'));
    clearCoverageConfirmation();
    resetValidationState();
    renderSourceControls(null); renderRowExclusionPage(); refreshDateFormatPreview();
});
let excelHeaders = [];
let templateColumns = [];
let sheetNames = [];
let currentSheet = null;
let workbookData = null;
let uploadWorkbookDate1904 = false;
let isProcessing = false;
// Phase 3B: one Upload action (validation or import) owns the button and shared cursor for its
// complete async sequence while the rest of the page remains usable.
let uploadActionInFlight = false;
let validationStatus = 'idle'; // 'idle' | 'validating' | 'valid' | 'invalid'
let validationPlanId = null;
let footingVarianceConfirmed = false;
let uploadValidationGeneration = 0;
let lastChangePreview = null;
let lastUploadSummary = null;
let lastIgnoredResult = null;
// Pre-import Rate mode visibility (Phase D of the Rate-derivation requirement) --
// the validate response's rate_mode field ({template_setting, effective_source, label}),
// or null for a non-eligible template. Only ever trustworthy alongside a 'valid'
// validationStatus -- cleared in lockstep with it by resetValidationState().
let lastRateModeInfo = null;

// Ledger identity belongs to this client/upload, never to repeated Excel cells.
let uploadTemplatePurpose = 'standard';
let uploadLedgerColumnRoles = {};
let uploadTemplateCatalog = [];
let uploadTemplateGeneration = 0;
let ledgerAccountChoices = [];
let ledgerSelectedAccountCode = '';
let ledgerSelectionClientId = null;
let pendingLedgerRestoreCode = '';
let ledgerRecognisedControlRows = new Set();
let ledgerRecognisedNoteRows = new Map();
let ledgerAccountLoad = null;
const ledgerAccountSelect = document.getElementById('ledgerAccountSelect');
const ledgerAccountToggle = document.getElementById('ledgerAccountToggle');
const ledgerAccountOptions = document.getElementById('ledgerAccountOptions');
let ledgerAccountMatches = [];
let ledgerAccountActiveIndex = -1;
function isLedgerUpload() { return uploadTemplatePurpose === 'account_ledger'; }
function ledgerUploadLabel() {
    const account = ledgerAccountChoices.find(a => a.account_code === ledgerSelectedAccountCode);
    return account ? `${account.account_name} (${account.account_code})` : '';
}
function clearUploadFieldError(field, help) {
    field?.removeAttribute('aria-invalid');
    field?.classList.remove('upload-field-error');
    help?.classList.remove('upload-inline-error');
    if (help?.id === 'ledgerAccountHelp') help.setAttribute('role', 'status');
    if (help?.dataset.uploadError === 'true') {
        help.hidden = true;
        help.textContent = '';
        delete help.dataset.uploadError;
    }
}
function showUploadFieldError(field, help, message) {
    if (!field || !help) return;
    field.classList.add('upload-field-error');
    field.setAttribute('aria-invalid', 'true');
    help.classList.add('upload-inline-error');
    help.setAttribute('role', 'alert');
    help.dataset.uploadError = 'true';
    help.textContent = message;
    help.hidden = false;
    field.scrollIntoView({block: 'center', behavior: 'smooth'});
    field.focus({preventScroll: true});
}
function clearLedgerUploadContext() {
    finishLedgerAccountLoading();
    ledgerAccountChoices = []; ledgerSelectedAccountCode = ''; ledgerSelectionClientId = null;
    document.getElementById('ledgerUploadContext').hidden = true;
    document.getElementById('ledgerAccountHelp').hidden = true;
    document.getElementById('ledgerEvidenceSummary').hidden = true;
    renderLedgerUploadAccounts();
}
function renderLedgerUploadAccounts() {
    closeLedgerAccountOptions();
    const account = ledgerAccountChoices.find(a => a.account_code === ledgerSelectedAccountCode);
    ledgerAccountSelect.value = account ? ledgerAccountOptionLabel(account) : '';
    ledgerAccountSelect.title = ledgerAccountSelect.value;
    ledgerAccountSelect.disabled = !!ledgerAccountLoad || !ledgerAccountChoices.length;
    ledgerAccountToggle.disabled = ledgerAccountSelect.disabled;
}
function ledgerAccountOptionLabel(account) {
    return `${account.account_name} — ${account.account_code}`;
}
function closeLedgerAccountOptions() {
    ledgerAccountOptions.hidden = true;
    ledgerAccountSelect.setAttribute('aria-expanded', 'false');
    ledgerAccountSelect.removeAttribute('aria-activedescendant');
    ledgerAccountMatches = []; ledgerAccountActiveIndex = -1;
}
function selectLedgerUploadAccount(account) {
    // Only an explicit choice supplies the identity used by validation/import.
    ledgerSelectedAccountCode = account.account_code;
    clearUploadFieldError(ledgerAccountSelect, document.getElementById('ledgerAccountHelp'));
    renderLedgerUploadAccounts();
    resetValidationState(); saveUploadSessionState();
}
function showLedgerAccountOptions(query = '') {
    if (!isLedgerUpload() || ledgerAccountSelect.disabled) return;
    const term = query.trim().toLocaleLowerCase();
    const matches = ledgerAccountChoices.filter(a => ledgerAccountOptionLabel(a).toLocaleLowerCase().includes(term));
    ledgerAccountMatches = matches.slice(0, 100);
    ledgerAccountActiveIndex = -1;
    ledgerAccountSelect.removeAttribute('aria-activedescendant');
    ledgerAccountOptions.replaceChildren();
    ledgerAccountMatches.forEach((account, index) => {
        const option = document.createElement('button');
        option.type = 'button'; option.tabIndex = -1;
        option.id = `upload-ledger-option-${index}`;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', String(account.account_code === ledgerSelectedAccountCode));
        option.textContent = ledgerAccountOptionLabel(account);
        option.title = account.account_code;
        option.addEventListener('mousedown', event => event.preventDefault());
        option.addEventListener('click', () => selectLedgerUploadAccount(account));
        ledgerAccountOptions.appendChild(option);
    });
    if (!matches.length || matches.length > 100) {
        const message = document.createElement('div');
        message.className = 'ledger-option-message';
        message.textContent = matches.length ? 'Showing the first 100 matches. Type more to narrow the list.' : 'No matching accounts.';
        ledgerAccountOptions.appendChild(message);
    }
    ledgerAccountOptions.hidden = false;
    ledgerAccountSelect.setAttribute('aria-expanded', 'true');
    // This field is at the bottom of mapping: open upwards when space is tight.
    const bounds = ledgerAccountSelect.getBoundingClientRect();
    const above = bounds.top - 12, below = window.innerHeight - bounds.bottom - 12;
    const opensUp = below < 180 && above > below;
    ledgerAccountOptions.style.top = opensUp ? 'auto' : 'calc(100% + 4px)';
    ledgerAccountOptions.style.bottom = opensUp ? 'calc(100% + 4px)' : 'auto';
    ledgerAccountOptions.style.maxHeight = `${Math.max(80, Math.min(240, opensUp ? above : below))}px`;
}
function finishLedgerAccountLoading() {
    // One owner per field: an obsolete response cannot clear a newer spinner.
    if (!ledgerAccountLoad) return;
    window.LoadingUI.hideFieldLoading(ledgerAccountSelect);
    window.LoadingUI.stopGlobalLoading(ledgerAccountLoad.token);
    ledgerAccountSelect.removeAttribute('aria-busy');
    ledgerAccountLoad = null;
}
async function loadLedgerUploadAccounts(preferred = '', generation = uploadTemplateGeneration) {
    finishLedgerAccountLoading();
    const client = Number(getCurrentClientId());
    ledgerAccountChoices = []; ledgerSelectedAccountCode = ''; ledgerSelectionClientId = null;
    renderLedgerUploadAccounts(); resetValidationState();
    const help = document.getElementById('ledgerAccountHelp');
    clearUploadFieldError(ledgerAccountSelect, help);
    help.hidden = false;
    help.textContent = 'Loading accounts…';
    const load = {token: window.LoadingUI.startGlobalLoading(ledgerAccountSelect)};
    ledgerAccountLoad = load;
    ledgerAccountSelect.setAttribute('aria-busy', 'true');
    window.LoadingUI.showFieldLoading(ledgerAccountSelect);
    try {
        const response = await fetchUpload(API_BASE + '/api/upload/ledger-accounts?client_id=' + client);
        const result = await response.json();
        if (ledgerAccountLoad !== load || !isLedgerUpload() || generation !== uploadTemplateGeneration || client !== Number(getCurrentClientId())) return;
        if (!Array.isArray(result.accounts)) throw new Error('Account response is incomplete. Select the template again to retry.');
        ledgerAccountChoices = result.accounts;
        ledgerSelectionClientId = client;
        ledgerSelectedAccountCode = result.accounts.some(a => a.account_code === preferred) ? preferred : '';
        renderLedgerUploadAccounts();
        help.textContent = result.accounts.length ? ''
            : 'No posting accounts. Upload a Trial Balance, then reselect this template.';
        help.hidden = result.accounts.length > 0;
        saveUploadSessionState();
    } catch (error) {
        if (ledgerAccountLoad !== load || generation !== uploadTemplateGeneration || !isLedgerUpload() || client !== Number(getCurrentClientId())) return;
        help.textContent = 'Accounts could not be loaded. Select the template again to retry.';
        showUploadFailure(error.message, 'Ledger account selection', error);
    } finally {
        if (ledgerAccountLoad === load) {
            finishLedgerAccountLoading();
            renderLedgerUploadAccounts();
            if (client !== Number(getCurrentClientId()) && isLedgerUpload()) {
                help.textContent = 'Client changed. Select the template again to load its accounts.';
                help.hidden = false;
            }
        }
    }
}
ledgerAccountSelect.addEventListener('focus', () => {
    showLedgerAccountOptions(ledgerSelectedAccountCode ? '' : ledgerAccountSelect.value);
    ledgerAccountSelect.select();
});
ledgerAccountSelect.addEventListener('input', () => {
    ledgerSelectedAccountCode = ''; ledgerAccountSelect.title = '';
    resetValidationState(); saveUploadSessionState();
    showLedgerAccountOptions(ledgerAccountSelect.value);
});
ledgerAccountToggle.addEventListener('mousedown', event => event.preventDefault());
ledgerAccountToggle.addEventListener('click', () => {
    if (!ledgerAccountOptions.hidden) closeLedgerAccountOptions();
    else { ledgerAccountSelect.focus(); showLedgerAccountOptions(); }
});
document.getElementById('ledgerUploadContext').addEventListener('focusout', event => {
    if (!event.currentTarget.contains(event.relatedTarget)) closeLedgerAccountOptions();
});
ledgerAccountSelect.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !ledgerAccountOptions.hidden) {
        event.preventDefault(); event.stopPropagation();
        renderLedgerUploadAccounts();
    } else if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault();
        if (ledgerAccountOptions.hidden) showLedgerAccountOptions(ledgerSelectedAccountCode ? '' : ledgerAccountSelect.value);
        if (!ledgerAccountMatches.length) return;
        ledgerAccountActiveIndex = ledgerAccountActiveIndex < 0
            ? (event.key === 'ArrowDown' ? 0 : ledgerAccountMatches.length - 1)
            : (ledgerAccountActiveIndex + (event.key === 'ArrowDown' ? 1 : -1) + ledgerAccountMatches.length) % ledgerAccountMatches.length;
        const options = [...ledgerAccountOptions.querySelectorAll('[role="option"]')];
        options.forEach((option, index) => option.classList.toggle('active', index === ledgerAccountActiveIndex));
        const active = options[ledgerAccountActiveIndex];
        ledgerAccountSelect.setAttribute('aria-activedescendant', active.id);
        active.scrollIntoView({block: 'nearest'});
    } else if (event.key === 'Enter') {
        event.preventDefault();
        const account = ledgerAccountMatches[ledgerAccountActiveIndex] || (ledgerAccountMatches.length === 1 ? ledgerAccountMatches[0] : null);
        if (!ledgerAccountOptions.hidden && account) selectLedgerUploadAccount(account);
    }
});

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
// Template selection reveals the download and file picker; sheet/date controls
// follow only when the workbook and its mapping are available.
function setSheetDownloadGroupVisible(visible) {
    const group = document.getElementById('sheetDownloadGroup');
    if (group) group.style.display = visible ? 'flex' : 'none';
    const uploadGroup = document.getElementById('uploadFileGroup');
    if (uploadGroup) uploadGroup.style.display = visible ? 'flex' : 'none';
}

// Reveal the Data Bank shortcut only after the source mapping is ready.
function updateGoToDataBankState(enabled) {
    const btn = document.getElementById('goToDataBankBtn');
    if (btn) {
        btn.disabled = !enabled;
        btn.style.display = enabled ? 'inline-flex' : 'none';
    }
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
        clientId: getCurrentClientId(),
        ledgerAccountCode: isLedgerUpload() ? ledgerSelectedAccountCode : '',
        sheetName: currentSheet,
        fileName: selectedFile ? selectedFile.name : null,
        dateDelimiter: selectedDateDelimiter,
        dateOrder: selectedDateOrder,
        comparisonMode: comparisonModeDropdown ? comparisonModeDropdown.value : 'correction',
        coverageStart: coverageInputSessionValue(uploadCoverageStart),
        coverageEnd: coverageInputSessionValue(uploadCoverageEnd),
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
            reconciliation_role: m.reconciliation_role || null,
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
    pendingLedgerRestoreCode = '';
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
    if (state.clientId && String(state.clientId) !== String(getCurrentClientId())) { clearUploadSessionState(); return; }
    pendingLedgerRestoreCode = state.clientId ? state.ledgerAccountCode || '' : '';

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

    if (comparisonModeDropdown && (state.comparisonMode === 'correction' || state.comparisonMode === 'replace_range')) {
        comparisonModeDropdown.value = state.comparisonMode;
    }
    updateCoverageDateInputHints();
    if (uploadCoverageStart) uploadCoverageStart.value = restoreCoverageInputDate(state.coverageStart);
    if (uploadCoverageEnd) uploadCoverageEnd.value = restoreCoverageInputDate(state.coverageEnd);
    coverageManuallyEdited = Boolean(state.coverageStart || state.coverageEnd);
    updateUploadCoverageVisibility();

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
        icon.style.fontSize = '12px';
        icon.textContent = m.is_matched ? '✅' : '❌';
        icon.style.color = m.is_matched ? '#16a34a' : '#dc2626';
        td4.appendChild(icon);
        tr.appendChild(td4);

        mappingBody.appendChild(tr);
    });

    const matched = state.mappings.filter(m => m.is_matched).length;
    if (mappingCount) mappingCount.textContent = matched + '/' + state.mappings.length + ' columns mapped';

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
    uploadValidationGeneration++;
    clearIgnoredResult();
    validationStatus = 'idle';
    validationPlanId = null;
    footingVarianceConfirmed = false;
    comparisonPreviewRequest = null;
    previewPageCache.clear();
    databasePreviewMode = false;
    databasePreviewContext = null;
    comparisonPreviewStale = false;
    document.getElementById('uploadReplacementStalePlan')?.setAttribute('hidden', '');
    const confirmButton = document.getElementById('uploadReplacementConfirm');
    if (confirmButton) confirmButton.disabled = false;
    renderSourceControls(null);
    document.getElementById('ledgerEvidenceSummary').hidden = true;
    ledgerRecognisedControlRows = new Set();
    ledgerRecognisedNoteRows = new Map();
    lastChangePreview = null;
    lastUploadSummary = null;
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
    let warned = false;
    clearInterval(importProgressPollInterval);
    const pollId = setInterval(async () => {
        try {
            const res = await fetchUpload(API_BASE + '/api/upload/import-status/' + fileId);
            if (!res.ok) return;
            const status = await res.json();
            if (importProgressPollInterval !== pollId) return;
            updateImportProgressBar(status.current || 0, status.total || 0);
            if (status.status === 'archiving') {
                const label = document.getElementById('importProgressLabel');
                if (label) label.textContent = 'Saving and verifying the external archive before replacement…';
            }
        } catch (e) {
            if (!warned && importProgressPollInterval === pollId) {
                warned = true;
                showUploadIssue({stage: 'Import progress', severity: 'warning', message: e.message,
                    action: 'Live progress is temporarily unavailable. The import request is still running; wait for its final result before retrying.'});
            }
        }
    }, 1000);
    importProgressPollInterval = pollId;
}

function showStatus(msg, type = 'info', extra = '') {
    if (!statusBar || !statusMessage) {
        return;
    }
    const ignoredAction = lastIgnoredResult
        ? statusMessage.querySelector('#reopenIgnoredResult')?.parentElement : null;
    statusMessage.innerHTML = msg;
    if (extra) {
        statusMessage.innerHTML += '<div style="font-size:12px; opacity:0.9; margin-top:2px;">' + extra + '</div>';
    }
    if (ignoredAction && lastIgnoredResult) statusMessage.appendChild(ignoredAction);
    if (statusExtra) statusExtra.textContent = '';
    statusBar.className = 'status-bar ' + type;
    statusBar.style.display = 'flex';
}

function clearIgnoredResult() {
    lastIgnoredResult = null;
    document.getElementById('reopenIgnoredResult')?.parentElement?.remove();
}

function showIgnoredResultAction(result) {
    clearIgnoredResult();
    if (!statusMessage) return;
    lastIgnoredResult = result;
    const actions = document.createElement('div');
    actions.style.cssText = 'font-size:12px; margin-top:6px;';
    const button = document.createElement('button');
    button.id = 'reopenIgnoredResult';
    button.type = 'button';
    button.textContent = 'View result';
    button.setAttribute('aria-label', 'View no-change upload result');
    button.style.cssText = 'padding:4px 10px; background:#475569; color:white; border:0; border-radius:5px; font-weight:600; cursor:pointer; font-size:11px;';
    button.addEventListener('click', () => {
        if (lastIgnoredResult) showImportSummary(lastIgnoredResult);
    });
    actions.appendChild(button);
    statusMessage.appendChild(actions);
}

// All upload API failures carry a stage and a safe explanation, including
// non-JSON proxy responses. Mutations are never automatically replayed here.
function uploadRequestStage(url) {
    const path = String(url).split('?')[0];
    if (path.includes('import-status')) return 'Import progress';
    if (path.endsWith('/import')) return 'Import';
    if (path.includes('import-excel')) return 'File upload';
    if (path.includes('date-preview')) return 'Date preview';
    if (path.includes('ledger-accounts')) return 'Ledger account selection';
    if (path.includes('validate')) return 'Validation';
    if (path.includes('mapping') || path.includes('auto-map')) return 'Column mapping';
    return 'Template selection';
}

async function fetchUpload(url, options) {
    const stage = uploadRequestStage(url);
    let response;
    let result;
    try {
        response = await fetch(url, options);
        try { result = await response.json(); } catch (_) {
            throw new Error(response.status === 401 || response.redirected
                ? 'Your session has expired. Sign in again.'
                : `The server returned an unreadable response (HTTP ${response.status}).`);
        }
        if (!response.ok || result === null || typeof result !== 'object' || result.status === 'error' || result.success === false) {
            const error = new Error(response.status === 401
                ? 'Your session has expired. Sign in again.'
                : uploadFailureMessage(result, `Request failed (HTTP ${response.status}).`));
            error.result = result;
            throw error;
        }
        const path = String(url).split('?')[0];
        const invalidShape = (stage === 'Import' && !['success', 'validation_failed'].includes(result.status))
            || (stage === 'Validation' && typeof result.can_import !== 'boolean' && typeof result.has_errors !== 'boolean')
            || (stage === 'Date preview' && !Array.isArray(result.samples))
            || (path.endsWith('/templates') && !Array.isArray(result))
            || (path.endsWith('/upload-columns') && !Array.isArray(result.columns))
            || (path.endsWith('/auto-map') && (!Array.isArray(result.mappings) || !Array.isArray(result.excel_headers)));
        if (invalidShape) throw new Error('The server response is incomplete. Retry this step.');
        return {ok: response.ok, status: response.status, json: async () => result};
    } catch (error) {
        error.stage = stage;
        error.httpStatus = response?.status;
        const outcomeNotApplied = result?.detail?.outcome === 'not_applied';
        error.outcomeUnknown = stage === 'Import' && (!response || (response.status >= 500 && !outcomeNotApplied) || response.ok);
        if (!response) error.message = 'Connection lost. Retry this step.';
        throw error;
    }
}

function uploadFailureMessage(result, fallback) {
    const candidates = [result?.detail?.message, result?.detail, result?.error?.http_detail,
        result?.message];
    const message = candidates.find(value => typeof value === 'string' && value.trim());
    if (message) return message;
    if (Array.isArray(result?.detail)) {
        return result.detail.map(item => item.msg).filter(Boolean).join('; ') || fallback;
    }
    // Legacy global errors can hide the cause behind the registry's Unknown text.
    // Display only the supplied reason/support reference, never its stack or code location.
    const problem = result?.error;
    const userMessage = problem?.user_message;
    const reason = problem?.technical_details?.message;
    if (typeof reason === 'string' && reason.trim()
        && (!userMessage || problem.error_code === 'ERR-UNK-001' || /unknown|unexpected error/i.test(userMessage))) {
        const reference = [problem.error_code, problem.error_id ? `reference ${problem.error_id}` : ''].filter(Boolean).join(', ');
        return reason + (reference ? ` (${reference})` : '');
    }
    if (typeof userMessage === 'string' && userMessage.trim()) return userMessage;
    return fallback;
}

function showUploadFailure(message, stage = 'Upload', error = null) {
    message = String(message || 'Upload failed.').replace(/^\s*(?:❌|⚠️)\s*/, '')
        .replace(/^(?:Validation error|Import error|Upload error|Date preview failed|Auto-mapping failed|Error reading file|Error previewing file|Failed to load (?:templates|template columns)):\s*/i, '');
    const unknown = Boolean(error?.outcomeUnknown);
    const missingStagedFile = error?.result?.detail?.code === 'staged_file_missing';
    const outcomeNotApplied = error?.result?.detail?.outcome === 'not_applied';
    const action = outcomeNotApplied
        ? 'Reconnect this app to Google Drive, then validate again and retry.'
        : missingStagedFile
        ? 'Select the file again, then validate.'
        : unknown
        ? 'Reconnect, then retry Import without changing the file or settings.'
        : error?.httpStatus === 401
            ? 'Sign in again, then reopen Data Upload.'
            : null;
    const focusTarget = stage === 'Ledger account selection' ? ledgerAccountSelect
        : stage === 'Ledger coverage' ? uploadCoverageStart : null;
    if (stage === 'Ledger account selection') {
        showUploadFieldError(ledgerAccountSelect, document.getElementById('ledgerAccountHelp'), message);
    } else if (stage === 'Ledger coverage') {
        showUploadFieldError(uploadCoverageStart, document.getElementById('uploadCoverageHelp'), message);
    }
    showStatus('', unknown ? 'warning' : 'error');
    // Voucher/account identifiers and server messages are source text, never HTML.
    if (statusMessage) statusMessage.textContent = unknown ? 'Import outcome unconfirmed. ' + message
        : outcomeNotApplied ? 'Import not applied. ' + message : message;
    showUploadIssue({stage: error?.stage || stage, severity: unknown ? 'unconfirmed' : 'error',
        message, action, focusTarget, result: error?.result?.detail || error?.result});
}

function showSourceDiagnostics(result) {
    const controls = result.source_diagnostics;
    if (!controls || (!controls.excluded_count && !controls.warnings?.length)) return;
    showUploadIssue({stage: 'Source review', severity: controls.warnings?.length ? 'warning' : 'info',
        message: isLedgerUpload() && controls.warnings?.length
            ? `Ledger balance/source review: ${controls.warnings.length} item(s) need attention.`
            : `${controls.excluded_count || 0} blank or total rows skipped.`,
        action: controls.warnings?.length
            ? 'Review the balance differences before import.'
            : 'Review these row numbers if needed.',
        rows: [...(controls.warnings || []), ...(controls.excluded_rows || []).map(row => ({
            row_number: row.row_number, message: `Excluded ${row.kind} row${row.label ? ': ' + row.label : ''}`
        }))]});
}

function formatUploadPreviewCell(value, header) {
    const dateColumn = currentMappings.some(mapping =>
        mapping.excel_column === header && mapping.data_type === 'Date');
    if (dateColumn && typeof value === 'number' && Number.isFinite(value)) {
        const date = XLSX.SSF.parse_date_code(value, { date1904: uploadWorkbookDate1904 });
        if (date && date.y >= 1 && date.y <= 9999) {
            return String(date.d).padStart(2, '0') + '/' + String(date.m).padStart(2, '0')
                + '/' + String(date.y).padStart(4, '0');
        }
    }
    // Text dates retain their source order; the date-format preview explains
    // their interpretation. Never infer MDY from a day <= 12.
    return value == null ? '' : value;
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

async function runUploadAction(target, action) {
    if (uploadActionInFlight) return;
    uploadActionInFlight = true;
    const globalLoadingToken = window.LoadingUI
        ? window.LoadingUI.startGlobalLoading(target)
        : null;
    try {
        await action();
    } finally {
        if (globalLoadingToken) window.LoadingUI.stopGlobalLoading(globalLoadingToken);
        uploadActionInFlight = false;
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

function selectedCoverageDateFormat(delimiter = selectedDateDelimiter, order = selectedDateOrder) {
    return DATE_ORDER_OPTIONS[delimiter]?.find(([code]) => code === order)?.[1] || 'DD/MM/YYYY';
}

function formatCoverageInputDate(isoDate, delimiter = selectedDateDelimiter, order = selectedDateOrder) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate || ''));
    if (!match) return '';
    const [, year, month, day] = match;
    const parts = {D: day, M: month, Y: year};
    const orderParts = {DMY: ['D', 'M', 'Y'], MDY: ['M', 'D', 'Y'], YMD: ['Y', 'M', 'D']}[order];
    if (!orderParts) return '';
    return orderParts.map(part => parts[part]).join(delimiter);
}

function parseCoverageInputDate(value, delimiter = selectedDateDelimiter, order = selectedDateOrder) {
    const raw = String(value ?? '').trim();
    if (!raw) return {valid: true, iso: ''};
    const orderParts = {DMY: ['D', 'M', 'Y'], MDY: ['M', 'D', 'Y'], YMD: ['Y', 'M', 'D']}[order];
    if (!orderParts) return {valid: false, iso: ''};
    let parts;
    if (delimiter) {
        const separator = delimiter.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const match = raw.match(new RegExp(`^(\\d{${order === 'YMD' ? '4' : '1,2'}})${separator}(\\d{${order === 'YMD' ? '1,2' : '1,2'}})${separator}(\\d{${order === 'YMD' ? '1,2' : '4'}})$`));
        if (!match) return {valid: false, iso: ''};
        parts = match.slice(1).map(Number);
    } else {
        const match = /^(\d{8})$/.exec(raw);
        if (!match) return {valid: false, iso: ''};
        const digits = match[1];
        parts = order === 'YMD'
            ? [Number(digits.slice(0, 4)), Number(digits.slice(4, 6)), Number(digits.slice(6, 8))]
            : [Number(digits.slice(0, 2)), Number(digits.slice(2, 4)), Number(digits.slice(4, 8))];
    }
    const components = Object.fromEntries(orderParts.map((part, index) => [
        {D: 'day', M: 'month', Y: 'year'}[part], parts[index]
    ]));
    if (components.year < 1 || components.month < 1 || components.month > 12 || components.day < 1 || components.day > 31) {
        return {valid: false, iso: ''};
    }
    const check = new Date(0);
    check.setUTCHours(0, 0, 0, 0);
    check.setUTCFullYear(components.year, components.month - 1, components.day);
    if (check.getUTCFullYear() !== components.year || check.getUTCMonth() !== components.month - 1
        || check.getUTCDate() !== components.day) return {valid: false, iso: ''};
    const iso = `${String(components.year).padStart(4, '0')}-${String(components.month).padStart(2, '0')}-${String(components.day).padStart(2, '0')}`;
    return {valid: true, iso};
}

function captureCoverageDateInputs() {
    return [uploadCoverageStart, uploadCoverageEnd].map(control => {
        const raw = control?.value || '';
        const parsed = parseCoverageInputDate(raw);
        return {control, raw, iso: parsed.valid ? parsed.iso : ''};
    });
}

function reformatCoverageDateInputs(snapshot) {
    (snapshot || []).forEach(({control, raw, iso}) => {
        if (!control) return;
        control.value = iso ? formatCoverageInputDate(iso) : raw;
    });
    updateCoverageDateInputHints();
}

function updateCoverageDateInputHints() {
    const format = selectedCoverageDateFormat();
    [uploadCoverageStart, uploadCoverageEnd].forEach((control, index) => {
        if (!control) return;
        control.placeholder = format;
        control.maxLength = selectedDateDelimiter ? 10 : 8;
        control.setAttribute('aria-label', `${index === 0 ? 'From' : 'To'} date (${format})`);
    });
}

function restoreCoverageInputDate(value) {
    const raw = String(value || '');
    const isoMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
    const parsed = isoMatch ? parseCoverageISODate(raw) : parseCoverageInputDate(raw);
    return parsed?.valid && parsed.iso ? formatCoverageInputDate(parsed.iso) : raw;
}

function parseCoverageISODate(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ''));
    if (!match) return {valid: false, iso: ''};
    const [year, month, day] = match.slice(1).map(Number);
    const check = new Date(0);
    check.setUTCHours(0, 0, 0, 0);
    check.setUTCFullYear(year, month - 1, day);
    return check.getUTCFullYear() === year && check.getUTCMonth() === month - 1 && check.getUTCDate() === day
        ? {valid: true, iso: value} : {valid: false, iso: ''};
}

function coverageInputSessionValue(control) {
    const raw = control?.value || '';
    const parsed = parseCoverageInputDate(raw);
    return parsed.valid && parsed.iso ? parsed.iso : raw;
}

function autoSelectDateDelimiterFromMappedRows() {
    if (!dateDelimiterDropdown || !rowExclusionHeaders.length || !rowExclusionAllRows.length) return false;
    const dateIndexes = [...new Set(currentMappings
        .filter(mapping => mapping.data_type === 'Date' && mapping.excel_column)
        .map(mapping => rowExclusionHeaders.indexOf(mapping.excel_column))
        .filter(index => index >= 0))];
    if (!dateIndexes.length) return false;

    // Inspect the entire mapped date column set, not just the sample rows shown in the preview.
    // Native Excel date cells arrive as numbers; only date-shaped text indicates a delimiter.
    const delimiters = new Set();
    const dateText = /^\s*\d{1,4}\s*([/-])\s*\d{1,2}\s*\1\s*\d{1,4}(?=$|[\sTt])/;
    rowExclusionAllRows.forEach(row => {
        dateIndexes.forEach(index => {
            const value = row[index];
            if (typeof value !== 'string' || !value.trim()) return;
            const match = value.match(dateText);
            if (match) delimiters.add(match[1]);
        });
    });
    // Native Excel dates have no textual separator. Give them a stable slash
    // display instead of inheriting the preceding sheet's automatic dash choice.
    const detectedDelimiter = delimiters.size === 1 ? [...delimiters][0]
        : delimiters.size === 0 && dateIndexes.some(index => rowExclusionNativeDateColumns.has(index)) ? '/' : null;
    if (detectedDelimiter === null) return false;
    if (detectedDelimiter === selectedDateDelimiter) return false;

    // Infer only the separator. DMY/MDY/YMD remains the user's choice.
    const coverageSnapshot = captureCoverageDateInputs();
    const preservedOrder = selectedDateOrder;
    selectedDateDelimiter = detectedDelimiter;
    dateDelimiterDropdown.value = detectedDelimiter;
    populateDateOrderDropdown(detectedDelimiter);
    if (DATE_ORDER_OPTIONS[detectedDelimiter].some(([order]) => order === preservedOrder)) {
        selectedDateOrder = preservedOrder;
        dateOrderDropdown.value = preservedOrder;
    }
    reformatCoverageDateInputs(coverageSnapshot);
    clearCoverageConfirmation();
    resetValidationState();
    updateDateFormatTriggerLabel();
    saveUploadSessionState();
    return true;
}

if (dateDelimiterDropdown) {
    dateDelimiterDropdown.addEventListener('change', function() {
        const coverageSnapshot = captureCoverageDateInputs();
        selectedDateDelimiter = this.value;
        clearCoverageConfirmation();
        resetValidationState();
        populateDateOrderDropdown(selectedDateDelimiter);
        reformatCoverageDateInputs(coverageSnapshot);
        refreshDateFormatPreview();
        updateDateFormatTriggerLabel();
        saveUploadSessionState();
    });
}

if (dateOrderDropdown) {
    dateOrderDropdown.addEventListener('change', function() {
        const coverageSnapshot = captureCoverageDateInputs();
        selectedDateOrder = this.value;
        reformatCoverageDateInputs(coverageSnapshot);
        clearCoverageConfirmation();
        resetValidationState();
        refreshDateFormatPreview();
        updateDateFormatTriggerLabel();
        saveUploadSessionState();
    });
}

if (comparisonModeDropdown) {
    comparisonModeDropdown.addEventListener('change', function() {
        clearUploadCoverageError();
        clearCoverageConfirmation();
        updateUploadCoverageVisibility();
        resetValidationState();
        saveUploadSessionState();
    });
}

function clearUploadCoverageError() {
    const help = document.getElementById('uploadCoverageHelp');
    clearUploadFieldError(uploadCoverageStart, help);
    clearUploadFieldError(uploadCoverageEnd, help);
    clearUploadFieldError(uploadConfirmCoverage, help);
}

function updateUploadCoverageVisibility() {
    const container = document.getElementById('uploadCoverageFields');
    const complete = comparisonModeDropdown?.value === 'replace_range';
    if (container) container.style.display = complete || isLedgerUpload() ? 'flex' : 'none';
    document.getElementById('uploadEmptyConfirmLabel').hidden = !complete;
    document.getElementById('uploadCoverageConfirmLabel').hidden = !complete;
    const datesLocked = complete && Boolean(uploadConfirmCoverage?.checked);
    if (uploadCoverageStart) uploadCoverageStart.disabled = datesLocked;
    if (uploadCoverageEnd) uploadCoverageEnd.disabled = datesLocked;
}

function clearCoverageConfirmation() {
    if (uploadConfirmCoverage) uploadConfirmCoverage.checked = false;
    updateUploadCoverageVisibility();
}

function clearSourceCoverageDates() {
    ++datePreviewRequestGeneration;
    lastAutoSuggestedCoverage = null;
    coverageManuallyEdited = false;
    if (uploadCoverageStart) uploadCoverageStart.value = '';
    if (uploadCoverageEnd) uploadCoverageEnd.value = '';
    if (uploadConfirmEmpty) uploadConfirmEmpty.checked = false;
    clearCoverageConfirmation();
    clearUploadCoverageError();
    if (documentDateSuggestion) documentDateSuggestion.textContent = 'Map a Document Date role to see date suggestions.';
    updateCoverageDateInputHints();
}

updateCoverageDateInputHints();

[uploadCoverageStart, uploadCoverageEnd, uploadConfirmEmpty].forEach(control => {
    if (control) control.addEventListener('change', () => {
        clearUploadCoverageError();
        if (control === uploadCoverageStart || control === uploadCoverageEnd) coverageManuallyEdited = true;
        clearCoverageConfirmation();
        resetValidationState();
        saveUploadSessionState();
    });
});

uploadConfirmCoverage?.addEventListener('change', () => {
    clearUploadCoverageError();
    updateUploadCoverageVisibility();
    resetValidationState();
});

function reviewUploadReplacement(payload) {
    const dialog = document.getElementById('uploadReplacementReview');
    document.getElementById('uploadReplacementStalePlan').hidden = !comparisonPreviewStale;
    dialog.querySelector('h2').textContent = payload.comparison_mode === 'correction'
        ? 'Review matched corrections' : 'Review complete report';
    const summary = lastChangePreview || {};
    const backupCell = document.getElementById('uploadReplacementBackup');
    backupCell.replaceChildren();
    const backupText = document.createElement('span');
    backupText.textContent = payload.backup_enabled === false
        ? 'Development mode: no recovery ZIP or saved Excel report will be created.'
        : 'A recovery ZIP (when old rows are replaced) and a separate Excel change report will be verified in Google Drive before import.';
    backupCell.appendChild(backupText);
    if (payload.backup_enabled !== false && uploadArchiveFolderUrl) {
        const folderLink = document.createElement('a');
        folderLink.href = uploadArchiveFolderUrl;
        folderLink.target = '_blank';
        folderLink.rel = 'noopener';
        folderLink.textContent = 'Open configured Drive folder';
        folderLink.style.display = 'block';
        folderLink.style.marginTop = '5px';
        backupCell.appendChild(folderLink);
        const pathHint = document.createElement('small');
        pathHint.textContent = 'Excel reports: Upload Change Reports / client / year / import date';
        pathHint.style.display = 'block';
        backupCell.appendChild(pathHint);
        if (!uploadDriveConnectionReady) {
            const connectionWarning = document.createElement('small');
            connectionWarning.textContent = 'The folder is identified, but this server’s Drive connection is incomplete; imports need all Drive settings.';
            connectionWarning.style.display = 'block';
            backupCell.appendChild(connectionWarning);
        }
    } else if (payload.backup_enabled !== false) {
        const missing = document.createElement('small');
        missing.textContent = 'Drive folder address is not configured on this server.';
        missing.style.display = 'block';
        backupCell.appendChild(missing);
    }
    const count = key => Number(summary[key] || 0).toLocaleString();
    document.getElementById('uploadReplacementScope').textContent =
        `${window.getSelectedClientName ? window.getSelectedClientName() : 'Selected client'}${payload.ledger_account_code ? ' · ' + ledgerUploadLabel() : ''} · ${templateDropdown?.selectedOptions[0]?.textContent || 'Selected template'}`;
    const dateLabel = value => {
        const day = new Date(value + 'T00:00:00');
        return Number.isNaN(day.getTime()) ? value : day.toLocaleDateString('en-GB', {day: '2-digit', month: 'short', year: 'numeric'});
    };
    document.getElementById('uploadReplacementPeriod').textContent = payload.coverage_start
        ? `${dateLabel(payload.coverage_start)} – ${dateLabel(payload.coverage_end)}` : 'Matched corrections';
    // Presentation only: retain the server's separate occurrence/change counts.
    const counts = document.getElementById('uploadReplacementCounts');
    counts.replaceChildren();
    const deletedLabel = payload.comparison_mode === 'correction'
        ? 'Deleted — not applicable to correction uploads'
        : 'Deleted — absent from this complete report';
    for (const [label, key] of [
        ['Unchanged — already in database — not imported', 'unchanged_count'],
        ['Incoming new — will be added', 'new_count'],
        ['Changed — existing rows to update', 'changed_count'],
        ['Rows with unchecked-field values to refresh', 'informational_update_count'],
        [deletedLabel, 'missing_count'],
    ]) {
        const row = document.createElement('tr');
        const name = document.createElement('td'); name.textContent = label;
        const value = document.createElement('td'); value.className = 'upload-review-count'; value.textContent = count(key);
        row.append(name, value); counts.appendChild(row);
    }
    const vouchers = document.getElementById('uploadReplacementVouchers');
    vouchers.hidden = summary.changed_voucher_count === undefined;
    document.getElementById('uploadReplacementChangedVouchers').textContent = count('changed_voucher_count');
    document.getElementById('uploadReplacementRemovedVouchers').textContent = count('removed_voucher_count');
    const dateMoves = document.getElementById('uploadReplacementDateMoves');
    const moves = summary.date_moves || [];
    dateMoves.hidden = moves.length === 0;
    const moveBody = document.getElementById('uploadReplacementDateMoveRows');
    moveBody.replaceChildren();
    for (const move of moves) {
        const row = document.createElement('tr');
        for (const text of [move.voucher_id, move.previous_dates.join(', '), move.incoming_dates.join(', ')]) {
            const cell = document.createElement('td'); cell.textContent = text; row.appendChild(cell);
        }
        moveBody.appendChild(row);
    }
    return new Promise(resolve => {
        const confirm = document.getElementById('uploadReplacementConfirm');
        const cancel = document.getElementById('uploadReplacementCancel');
        const preview = document.getElementById('uploadReplacementPreview');
        function finish(approved) {
            confirm.removeEventListener('click', accept);
            cancel.removeEventListener('click', reject);
            preview.removeEventListener('click', openPreview);
            dialog.removeEventListener('cancel', escape);
            dialog.close();
            resolve(approved);
        }
        function accept() { finish(true); }
        function reject() { finish(false); }
        function escape(event) { event.preventDefault(); finish(false); }
        function openPreview() {
            comparisonPreviewRequest = payload;
            comparisonPreviewAction = 'all';
            comparisonPreviewReturn = () => dialog.showModal();
            dialog.close();
            showChangePreview();
        }
        confirm.disabled = comparisonPreviewStale;
        confirm.addEventListener('click', accept);
        cancel.addEventListener('click', reject);
        preview.addEventListener('click', openPreview);
        dialog.addEventListener('cancel', escape);
        dialog.showModal();
    });
}

async function showChangePreview() {
    comparisonPreviewMode = true;
    databasePreviewMode = false;
    comparisonPreviewAction = 'all';
    previewCurrentPage = 1;
    const selector = document.getElementById('previewDataView');
    if (selector) selector.value = 'all';
    enterPreviewSheet('← Back to report review', 'Validated changes from this upload plan.');
    await fetchChangePreviewPage(1);
}

function enterPreviewSheet(backLabel, note) {
    document.getElementById('previewSheet').classList.add('active');
    document.getElementById('mainUploadPage').style.display = 'none';
    document.getElementById('uploadContainer')?.classList.add('preview-active');
    document.getElementById('closePreviewBtn').textContent = backLabel;
    const noteElement = document.getElementById('previewSourceNote');
    noteElement.textContent = note || '';
    noteElement.hidden = !note;
}

async function showDatabasePreview(context, returnCallback = null) {
    databasePreviewContext = context;
    comparisonPreviewReturn = returnCallback;
    databasePreviewMode = true;
    comparisonPreviewMode = false;
    previewCurrentPage = 1;
    const selector = document.getElementById('previewDataView');
    if (selector) selector.value = 'uploaded';
    const matchText = context.reconciliationStatus === 'matched'
        ? 'Database rows after import · Excel row count, document count and footing controls matched.'
        : 'Database rows after import · review the reconciliation result shown in the import summary.';
    enterPreviewSheet('← Back to import result', matchText);
    await fetchDatabasePreviewPage('uploaded', 1);
}

async function fetchChangePreviewPage(page = previewCurrentPage) {
    if (!comparisonPreviewRequest) return;
    const body = document.getElementById('previewSheetBody');
    body.replaceChildren();
    const loading = document.createElement('tr');
    const message = document.createElement('td'); message.colSpan = Math.max(1, comparisonPreviewColumns.length + 2);
    message.textContent = 'Loading validated changes…'; loading.appendChild(message); body.appendChild(loading);
    const size = Number(document.getElementById('previewPageSize').value) || 50;
    const cacheKey = `plan:${comparisonPreviewRequest.validation_plan_id}:${comparisonPreviewAction}:${page}:${size}`;
    if (previewPageCache.has(cacheKey)) {
        const data = previewPageCache.get(cacheKey);
        comparisonPreviewRows = data.rows || [];
        comparisonPreviewColumns = (data.columns || []).map(column => column.name);
        previewCurrentPage = data.page || 1;
        previewPageSize = size;
        previewTotalRows = data.total || 0;
        comparisonPreviewPageCount = data.pages || 1;
        renderChangePreviewPage(data);
        return;
    }
    const query = new URLSearchParams({action: comparisonPreviewAction, page: String(page), page_size: String(size)});
    try {
        const response = await fetch(`${API_BASE}/api/upload/change-preview?${query}`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({import_request: comparisonPreviewRequest})
        });
        let data;
        try { data = await response.json(); }
        catch (_) { throw new Error('The server returned an unreadable preview response. Reload and validate the file again.'); }
        if (!response.ok) throw new Error(data?.detail || 'Could not load the validated changes.');
        if (!data || !Array.isArray(data.rows) || !Array.isArray(data.columns)) {
            throw new Error('The server returned an incomplete preview response. Reload and validate the file again.');
        }
        cachePreviewPage(cacheKey, data);
        comparisonPreviewRows = data.rows || [];
        comparisonPreviewColumns = (data.columns || []).map(column => column.name);
        previewCurrentPage = data.page || 1;
        previewPageSize = size;
        previewTotalRows = data.total || 0;
        comparisonPreviewPageCount = data.pages || 1;
        renderChangePreviewPage(data);
    } catch (error) {
        document.getElementById('previewSheetHead').replaceChildren();
        body.replaceChildren();
        const row = document.createElement('tr'); const cell = document.createElement('td');
        cell.colSpan = 2;
        cell.textContent = error.message; row.appendChild(cell); body.appendChild(row);
        document.getElementById('previewInfo').textContent = 'Preview unavailable';
        previewTotalRows = 0;
        document.getElementById('previewRowRange').textContent = 'Showing 0-0 of 0 changes';
        if (/validate the file again|validation plan|reviewed row changed/i.test(error.message)) {
            comparisonPreviewStale = true;
            previewPageCache.clear();
            document.getElementById('uploadReplacementStalePlan').hidden = false;
            document.getElementById('uploadReplacementConfirm').disabled = true;
        }
        setComparisonPreviewControls(1, 1);
    }
}

function cachePreviewPage(key, value) {
    previewPageCache.delete(key);
    previewPageCache.set(key, value);
    while (previewPageCache.size > 20) previewPageCache.delete(previewPageCache.keys().next().value);
}

async function fetchDatabasePreviewPage(view, page = previewCurrentPage) {
    const context = databasePreviewContext;
    if (!context) return;
    const size = Number(document.getElementById('previewPageSize').value) || 50;
    const cacheKey = `db:${context.file_id}:${view}:${page}:${size}:${context.date_start}:${context.date_end}`;
    const render = data => {
        previewCurrentPage = data.page || 1;
        previewPageSize = size;
        previewTotalRows = data.total || 0;
        comparisonPreviewPageCount = data.pages || 1;
        comparisonPreviewColumns = (data.columns || []).map(column => column.name);
        comparisonPreviewRows = data.rows || [];
        renderDatabasePreviewPage(data);
    };
    if (previewPageCache.has(cacheKey)) { render(previewPageCache.get(cacheKey)); return; }
    const body = document.getElementById('previewSheetBody');
    body.replaceChildren();
    const loading = document.createElement('tr'); const cell = document.createElement('td');
    cell.colSpan = Math.max(1, comparisonPreviewColumns.length + 2);
    cell.textContent = 'Loading database rows…'; loading.appendChild(cell); body.appendChild(loading);
    const query = new URLSearchParams({page: String(page), page_size: String(size)});
    try {
        const response = await fetch(`${API_BASE}/api/upload/database-preview?${query}`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({client_id: context.client_id, template_id: context.template_id,
                file_id: context.file_id, view, date_start: context.date_start, date_end: context.date_end,
                ledger_account_code: context.ledger_account_code || null,
                informational_client_data_ids: context.informationalClientDataIds || []})
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Could not load stored database rows.');
        cachePreviewPage(cacheKey, data);
        render(data);
    } catch (error) {
        body.replaceChildren(); const row = document.createElement('tr'); const message = document.createElement('td');
        message.colSpan = Math.max(1, comparisonPreviewColumns.length + 2); message.textContent = error.message;
        row.appendChild(message); body.appendChild(row);
        document.getElementById('previewInfo').textContent = 'Database preview unavailable';
        setComparisonPreviewControls(1, 1);
    }
}

let comparisonPreviewPageCount = 1;
function setPreviewDataView(view) {
    if (!['uploaded', 'all', 'added', 'changed', 'deleted'].includes(view)) view = 'uploaded';
    previewCurrentPage = 1;
    if (view === 'uploaded') {
        comparisonPreviewMode = false;
        if (databasePreviewContext) {
            databasePreviewMode = true;
            document.getElementById('previewSourceNote').textContent = 'These rows are read from the current database for the upload period.';
            document.getElementById('previewSourceNote').hidden = false;
            void fetchDatabasePreviewPage('uploaded', 1);
        } else {
            databasePreviewMode = false;
            try { preparePreviewWorkbookRows(); }
            catch (error) { document.getElementById('previewInfo').textContent = error.message; return; }
            renderPreviewPage();
        }
        return;
    }
    if (databasePreviewContext) {
        comparisonPreviewMode = false;
        databasePreviewMode = true;
        document.getElementById('previewSourceNote').textContent = 'These changes are read from stored current and history rows for this upload.';
        document.getElementById('previewSourceNote').hidden = false;
        void fetchDatabasePreviewPage(view, 1);
        return;
    }
    if (!comparisonPreviewRequest) {
        document.getElementById('previewInfo').textContent = 'Validate this upload to review its changes.';
        return;
    }
    comparisonPreviewMode = true;
    databasePreviewMode = false;
    comparisonPreviewAction = view;
    document.getElementById('previewSourceNote').textContent = 'Validated changes from the current upload plan.';
    document.getElementById('previewSourceNote').hidden = false;
    void fetchChangePreviewPage(1);
}
function setComparisonPreviewControls(page, pages) {
    document.getElementById('previewRowRange').textContent = `Showing ${previewTotalRows ? (page - 1) * previewPageSize + 1 : 0}-${Math.min(page * previewPageSize, previewTotalRows)} of ${previewTotalRows} changes`;
    document.getElementById('previewPageInfo').textContent = `Page ${page} of ${pages}`;
    document.getElementById('previewFirstPage').disabled = page <= 1;
    document.getElementById('previewPrevPage').disabled = page <= 1;
    document.getElementById('previewNextPage').disabled = page >= pages;
    document.getElementById('previewLastPage').disabled = page >= pages;
}

function renderChangePreviewPage(data) {
    const head = document.getElementById('previewSheetHead');
    const body = document.getElementById('previewSheetBody');
    head.replaceChildren(); body.replaceChildren();
    const heading = document.createElement('tr');
    for (const label of ['#', 'Action', ...comparisonPreviewColumns]) {
        const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = label; heading.appendChild(cell);
    }
    head.appendChild(heading);
    comparisonPreviewRows.forEach((item, index) => {
        const row = document.createElement('tr');
        const serial = document.createElement('td'); serial.textContent = String((data.page - 1) * data.page_size + index + 1); row.appendChild(serial);
        const action = document.createElement('td'); action.textContent = item.action; row.appendChild(action);
        comparisonPreviewColumns.forEach(name => {
            const cell = document.createElement('td');
            const oldValue = item.previous?.[name]; const newValue = item.incoming?.[name];
            if (item.action === 'Changed' && (item.changed_fields || []).includes(name)) {
                cell.textContent = `${oldValue ?? '—'} → ${newValue ?? '—'}`;
            } else {
                const value = item.action === 'Deleted' ? oldValue : newValue;
                cell.textContent = value == null || value === '' ? '—' : String(value);
            }
            row.appendChild(cell);
        });
        body.appendChild(row);
    });
    const countLabel = data.counts || {};
    document.getElementById('previewInfo').textContent = `${data.total} ${data.action === 'all' ? 'changes' : data.action} · ${countLabel.added || 0} added · ${countLabel.changed || 0} changed · ${countLabel.deleted || 0} deleted`;
    setComparisonPreviewControls(data.page, data.pages);
}

function renderDatabasePreviewPage(data) {
    const head = document.getElementById('previewSheetHead');
    const body = document.getElementById('previewSheetBody');
    head.replaceChildren(); body.replaceChildren();
    const heading = document.createElement('tr');
    for (const label of ['#', 'Action', ...comparisonPreviewColumns]) {
        const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = label; heading.appendChild(cell);
    }
    head.appendChild(heading);
    comparisonPreviewRows.forEach((item, index) => {
        const row = document.createElement('tr');
        const serial = document.createElement('td'); serial.textContent = String((data.page - 1) * data.page_size + index + 1); row.appendChild(serial);
        const action = document.createElement('td'); action.textContent = item.action; row.appendChild(action);
        comparisonPreviewColumns.forEach(name => {
            const cell = document.createElement('td');
            const oldValue = item.previous?.[name]; const newValue = item.incoming?.[name];
            if (item.action === 'Changed' && (item.changed_fields || []).includes(name)) {
                cell.textContent = `${oldValue ?? '—'} → ${newValue ?? '—'}`;
            } else {
                const value = item.action === 'Deleted' ? oldValue : newValue;
                cell.textContent = value == null || value === '' ? '—' : String(value);
            }
            row.appendChild(cell);
        });
        body.appendChild(row);
    });
    if (data.view === 'uploaded' && (data.footing_columns || []).length) {
        const totalRow = document.createElement('tr');
        totalRow.className = 'preview-footing-total';
        const label = document.createElement('th');
        label.scope = 'row';
        label.colSpan = 2;
        label.textContent = data.footing_totals_available
            ? 'Stored total · full selected scope' : 'Stored total unavailable';
        label.style.fontWeight = '700';
        totalRow.appendChild(label);
        const totals = data.footing_totals || {};
        const footingNames = new Set(data.footing_columns);
        comparisonPreviewColumns.forEach(name => {
            const cell = document.createElement('td');
            if (footingNames.has(name)) {
                const value = data.footing_totals_available ? totals[name] : null;
                cell.textContent = value == null ? 'Unavailable' : formatSourceControlAmount(value);
                cell.style.fontWeight = '700';
            }
            totalRow.appendChild(cell);
        });
        body.appendChild(totalRow);
    }
    document.getElementById('previewInfo').textContent = data.view === 'uploaded'
        ? `${data.total} stored rows${(data.footing_columns || []).length ? ' · footing totals cover the full selected scope' : ''}`
        : `${data.total} ${data.view === 'all' ? 'changes' : data.view} · ${data.counts?.added ?? 0} added · ${data.counts?.changed ?? 0} changed · ${data.counts?.deleted ?? 0} deleted`;
    setComparisonPreviewControls(data.page, data.pages);
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
    const requestGeneration = ++datePreviewRequestGeneration;
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

    const role = currentMappings.find(m => m.reconciliation_role === 'document_date');
    if (documentDateSuggestion) {
        documentDateSuggestion.textContent = !role
            ? 'Configure a Document Date role in Manage Templates to calculate suggestions.'
            : !role.excel_column ? 'Map the configured Document Date column to calculate suggestions.'
                : 'Calculating suggested dates…';
    }
    if (dateMappings.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        renderDocumentDateSuggestion(null);
        return;
    }

    try {
        const res = await fetchUpload(API_BASE + '/api/upload/date-preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_id: fileId,
                template_id: selectedTemplateId,
                sheet_name: currentSheet || 'Sheet1',
                date_mappings: dateMappings,
                column_mappings: currentMappings.filter(m => m.excel_column).map(m => ({
                    template_column_name: m.column_name,
                    excel_column_name: m.excel_column
                })),
                control_total_row: sourceFootingMappings().length ? Number(getElement('sourceControlRow')?.value) || null : null,
                control_total_confirmed: Boolean(sourceFootingMappings().length && getElement('sourceControlConfirmed')?.checked),
                date_delimiter: selectedDateDelimiter,
                date_order: selectedDateOrder
            })
        });
        if (!res.ok) throw new Error('Preview request failed');
        const data = await res.json();
        if (requestGeneration !== datePreviewRequestGeneration) return;
        renderDateFormatPreview(data.samples || []);
        renderDocumentDateSuggestion(data.date_suggestion);
    } catch (error) {
        if (requestGeneration !== datePreviewRequestGeneration) return;
        console.error('Date preview error:', error);
        showUploadFailure('Date preview failed: ' + error.message, 'Date preview', error);
        container.style.display = 'none';
        container.innerHTML = '';
    }
}

function renderDocumentDateSuggestion(suggestion) {
    if (!documentDateSuggestion) return;
    const clearUnconfirmedSuggestion = () => {
        if (!lastAutoSuggestedCoverage || uploadConfirmCoverage?.checked) return;
        if (!coverageManuallyEdited
            && parseCoverageInputDate(uploadCoverageStart?.value).iso === lastAutoSuggestedCoverage.start
            && parseCoverageInputDate(uploadCoverageEnd?.value).iso === lastAutoSuggestedCoverage.end) {
            uploadCoverageStart.value = '';
            uploadCoverageEnd.value = '';
            clearUploadCoverageError();
            resetValidationState();
            saveUploadSessionState();
        }
        lastAutoSuggestedCoverage = null;
    };
    if (!suggestion) {
        clearUnconfirmedSuggestion();
        return;
    }
    if (suggestion.status === 'configure_document_date') {
        clearUnconfirmedSuggestion();
        documentDateSuggestion.textContent = 'Configure a Document Date role in Manage Templates to calculate suggestions.';
    } else if (suggestion.status === 'confirm_footing') {
        clearUnconfirmedSuggestion();
        documentDateSuggestion.textContent = 'Confirm the Excel Footing row to calculate suggestions from transaction details.';
    } else if (suggestion.start && suggestion.end) {
        documentDateSuggestion.textContent = `Transaction-date suggestion: ${formatCoverageDate(suggestion.start)} – ${formatCoverageDate(suggestion.end)} (${suggestion.transaction_dates} valid dates). The full ERP period may be wider.`;
        if (!coverageManuallyEdited && !uploadConfirmCoverage?.checked
            && (parseCoverageInputDate(uploadCoverageStart?.value).iso !== suggestion.start
                || parseCoverageInputDate(uploadCoverageEnd?.value).iso !== suggestion.end)) {
            uploadCoverageStart.value = formatCoverageInputDate(suggestion.start);
            uploadCoverageEnd.value = formatCoverageInputDate(suggestion.end);
            lastAutoSuggestedCoverage = {start: suggestion.start, end: suggestion.end};
            clearUploadCoverageError();
            resetValidationState();
            saveUploadSessionState();
        }
    } else {
        clearUnconfirmedSuggestion();
        documentDateSuggestion.textContent = 'No valid Document Date values were found in the transaction rows.';
    }
}

function formatCoverageDate(value) {
    const day = new Date(value + 'T00:00:00');
    return Number.isNaN(day.getTime()) ? value
        : day.toLocaleDateString('en-GB', {day: '2-digit', month: 'short', year: 'numeric'});
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

    container.replaceChildren();
    const label = document.createElement('label'); label.textContent = 'Preview'; container.appendChild(label);
    samples.slice(0, 3).forEach(sample => {
        const line = document.createElement('span');
        line.className = sample.valid ? 'date-preview-line' : 'date-preview-line error';
        line.textContent = sample.valid ? `${sample.raw} → ${sample.parsed}` : `Could not parse "${sample.raw}" as ${selectedDateOrder}`;
        container.appendChild(line);
    });
    container.style.display = 'flex';
}

// ---------- LOAD TEMPLATES ----------
async function loadTemplates() {
    if (templateDropdown) window.LoadingUI.showFieldLoading(templateDropdown);
    try {
        const res = await fetchUpload(API_BASE + '/api/templates');
        if (!res.ok) throw new Error('Failed to load templates');
        const templates = await res.json();
        uploadTemplateCatalog = templates;
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
        showUploadFailure('Failed to load templates: ' + error.message, 'Template selection', error);
    } finally {
        if (templateDropdown) window.LoadingUI.hideFieldLoading(templateDropdown);
    }
}

// ---------- TEMPLATE SELECTION ----------
if (templateDropdown) {
    templateDropdown.addEventListener('change', async function() {
        // Check if client is selected
        if (!window.isClientSelected || !window.isClientSelected()) {
            showUploadFailure('⚠️ Please select a client first from the Dashboard.');
            this.value = '';
            setBadge('⛔ No client selected');
            if (importBtn) importBtn.disabled = true;
            updatePreviewButtonState();
            return;
        }
                const id = this.value;
        if (!id) {
            clearSourceCoverageDates();
            ++uploadTemplateGeneration; uploadTemplatePurpose = 'standard'; uploadLedgerColumnRoles = {}; clearLedgerUploadContext();
            selectedTemplateId = null;
            if (mappingPanel) mappingPanel.style.display = 'none';
            if (leftPanel) leftPanel.style.display = 'none';
            if (modeActions) modeActions.style.display = 'none';
            if (sheetField) sheetField.style.display = 'none';
            getElement('rowExclusionContainer').style.display = 'none';
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
    if (fileId && workbookData) clearSourceCoverageDates();
    resetValidationState();
    if (importBtn) importBtn.disabled = true;
    selectedTemplateId = parseInt(id);
    const generation = ++uploadTemplateGeneration;
    uploadTemplatePurpose = 'standard'; uploadLedgerColumnRoles = {}; clearLedgerUploadContext();
    saveUploadSessionState(); // Template chosen -- worth remembering even if the fetch below fails
    // A template is now chosen -- reveal Sheet/Download together and advance the step
    // strip, regardless of whether the column-fetch below succeeds (Download only needs
    // the template id, already known; Sheet is populated independently once a file loads).
    setSheetDownloadGroupVisible(true);
    updateStepIndicator(2);
    setBadge('⏳ Loading template...', 'loading');

    try {
        const res = await fetchUpload(API_BASE + `/api/templates/${id}/upload-columns`);
        if (!res.ok) throw new Error('Failed to fetch template columns');
        const data = await res.json();
        if (generation !== uploadTemplateGeneration || Number(id) !== selectedTemplateId) return;
        templateColumns = data.columns;
        uploadTemplatePurpose = data.data_purpose || 'standard';
        uploadLedgerColumnRoles = data.ledger_columns || {};
        if (isLedgerUpload()) {
            document.getElementById('ledgerUploadContext').hidden = false;
            await loadLedgerUploadAccounts(pendingLedgerRestoreCode, generation);
            pendingLedgerRestoreCode = '';
            if (generation !== uploadTemplateGeneration) return;
        }
        updateUploadCoverageVisibility();
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
        if (generation !== uploadTemplateGeneration) return;
        console.error('Error loading template columns:', error);
        setBadge('❌ Failed to load columns', 'error');
        showUploadFailure('Failed to load template columns: ' + error.message, 'Template columns', error);
        return;
    }
    if (generation !== uploadTemplateGeneration) return;
    updatePreviewButtonState();
    resetValidationState(); // New template means new data – reset validation
    resetSourceControlChoice();
}

// ---------- FILE UPLOAD ----------
// "Upload File" button (replaces the old breadcrumb AND the old drag-and-drop area, removed
// entirely in Phase 2 — click-to-upload only, no drag/drop capability anymore). Reuses the
// existing fileInput/handleFile() flow the drag-and-drop area used to call.
if (uploadFileBtn) {
    uploadFileBtn.addEventListener('click', function() {
        if (!selectedTemplateId) return;
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
        showUploadFailure('⚠️ Please select a valid Excel file (.xlsx or .xls)');
        return;
    }

    const restoringCoverage = Boolean(pendingRestoreSheetName);
    ++uploadMappingGeneration;
    datePreviewRequestGeneration++;
    selectedFile = file;
    rowExclusionNativeDateColumns = new Set();
    fileId = null;
    lastAutoSuggestedCoverage = null;
    coverageManuallyEdited = restoringCoverage && Boolean(uploadCoverageStart?.value || uploadCoverageEnd?.value);
    if (!restoringCoverage) {
        clearSourceCoverageDates();
    }
    updateUploadCoverageVisibility();
    resetValidationState();
    resetSourceControlChoice();
    if (importBtn) importBtn.disabled = true;
    // Replacing a workbook must not leave the previous workbook's tables visible
    // under the new filename while its file reading/upload/mapping is in flight.
    if (mappingPanel) mappingPanel.style.display = 'none';
    if (leftPanel) leftPanel.style.display = 'none';
    if (modeActions) modeActions.style.display = 'none';
    if (sheetField) sheetField.style.display = 'none';
    getElement('rowExclusionContainer').style.display = 'none';
    setDateFormatGroupVisible(false);
    closeDateFormatPopover();
    updateGoToDataBankState(false);
    if (fileName) {
        fileName.textContent = file.name;
        fileName.title = file.name;
    }
    document.getElementById('uploadFileGroup')?.classList.add('has-file');
    if (fileSize) fileSize.textContent = formatFileSize(file.size);
    if (fileInfo) fileInfo.style.display = 'flex';
    if (footerMeta) footerMeta.style.display = 'flex';
    if (footerFile) footerFile.textContent = '📄 ' + file.name;

    const reader = new FileReader();
    reader.onload = async function(e) {
        if (selectedFile !== file) return;
        try {
            const data = new Uint8Array(e.target.result);
            workbookData = data;
            const workbook = XLSX.read(data, { type: 'array' });
            sheetNames = workbook.SheetNames;

            if (sheetNames.length === 0) {
                showUploadFailure('❌ No sheets found in the file.');
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
            await uploadFileToServer(file);
            if (selectedFile !== file) return;
            updatePreviewButtonState();
            saveUploadSessionState(); // Remember the successfully staged file and sheet.
        } catch (err) {
            showUploadFailure('Error reading file: ' + err.message, 'File reading', err);
        }
    };
    reader.onerror = () => { if (selectedFile === file) showUploadFailure('The file could not be read. Check access and select it again.', 'File reading'); };
    reader.onabort = () => { if (selectedFile === file) showUploadFailure('File reading was interrupted. Select the file again.', 'File reading'); };
    try { reader.readAsArrayBuffer(file); }
    catch (error) { showUploadFailure(error.message, 'File reading', error); }
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
        ++uploadMappingGeneration;
        clearSourceCoverageDates();
        currentSheet = this.value;
        resetValidationState();
        if (importBtn) importBtn.disabled = true;
        resetSourceControlChoice();
        if (currentSheet && selectedTemplateId && fileId) {
            autoMapColumns();
            showStatus('📄 Sheet changed to "' + currentSheet + '". Re‑mapping...', 'info');
        } else if (currentSheet && selectedTemplateId && !fileId) {
            if (selectedFile) {
                uploadFileToServer(selectedFile);
            }
        }
        updatePreviewButtonState();
        saveUploadSessionState(); // Sheet changed -- remember the new selection
    });
}

// ---------- UPLOAD TO SERVER ----------
async function uploadFileToServer(file) {
    const formData = new FormData();
    formData.append('file', file);
    try {
        const res = await fetchUpload('/api/import-excel', {
            method: 'POST',
            body: formData
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.message || 'Upload failed');
        if (selectedFile !== file) return;
        if (!result.file_id) throw new Error('The server did not return an uploaded file reference. Select the file again.');
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
        if (selectedFile !== file) return;
        resetUpload();
        showUploadFailure('Upload error: ' + error.message, 'File upload', error);
    }
}

// ---------- CLEAR FILE ----------
if (clearBtn) {
    clearBtn.addEventListener('click', removeUploadFile);
}

async function removeUploadFile() {
    const templateId = selectedTemplateId;
    resetUpload();
    if (!templateId) return;
    // Reuse template loading so ledger choices and download metadata also return
    // to their normal template-selected state after the source file is removed.
    if (templateDropdown) templateDropdown.value = String(templateId);
    await selectTemplate(templateId);
    if (selectedTemplateId === templateId && !selectedFile) {
        showStatus('File removed. Upload another file for the selected template.', 'info');
    }
}

function resetUpload() {
    ++uploadMappingGeneration;
    ++uploadTemplateGeneration;
    clearSourceCoverageDates();
    ledgerSelectedAccountCode = ''; pendingLedgerRestoreCode = '';
    if (isLedgerUpload()) renderLedgerUploadAccounts();
    else clearLedgerUploadContext();
    selectedFile = null;
    document.getElementById('uploadFileGroup')?.classList.remove('has-file');
    fileId = null;
    if (comparisonModeDropdown) comparisonModeDropdown.value = 'replace_range';
    updateUploadCoverageVisibility();
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
    resetSourceControlChoice();
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
    rowExclusionNativeDateColumns = new Set();
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
    footingVarianceConfirmed = false;
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
    if (templateDropdown) templateDropdown.value = '';
    selectedTemplateId = null;
    uploadTemplatePurpose = 'standard';
    uploadLedgerColumnRoles = {};
    clearLedgerUploadContext();
    updateUploadCoverageVisibility();
}

// ---------- SAVE COLUMN MAPPING (Phase A2 of 3) ----------
// Persists the currently matched columns for this client+template via Phase A1's
// POST /api/upload/save-mapping. Always a full overwrite (backend upserts via delete-then-
// reinsert) -- there's no separate edit/delete UI this phase (Phase A3).
if (saveMappingBtn) {
    saveMappingBtn.addEventListener('click', async function() {
        const clientId = getCurrentClientId();
        if (!clientId) {
            showUploadFailure('⚠️ Please select a client first from the Dashboard.');
            return;
        }
        if (!selectedTemplateId || !currentMappings || currentMappings.length === 0) {
            showUploadFailure('⚠️ Map at least one column before saving.');
            return;
        }

        // SavedMappingEntry.excel_column (routers/import_export.py) is a required str, so
        // unmatched (null excel_column) rows must be filtered out here, not left for the
        // endpoint's own blank-skip to handle.
        const mappingsToSave = currentMappings
            .filter(m => m.excel_column)
            .map(m => ({ template_column_id: m.column_id, excel_column: m.excel_column }));

        if (mappingsToSave.length === 0) {
            showUploadFailure('⚠️ Map at least one column before saving.');
            return;
        }

        saveMappingBtn.disabled = true;
        const globalLoadingToken = window.LoadingUI
            ? window.LoadingUI.startGlobalLoading(this)
            : null;
        try {
            const res = await fetchUpload(API_BASE + '/api/upload/save-mapping', {
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
            showStatus(`💾 Mapping and comparison choices saved (${result.saved_count} column${result.saved_count === 1 ? '' : 's'}) — they will be reused for this client and template.`, 'success');
        } catch (error) {
            console.error('Save mapping error:', error);
            showUploadFailure('Failed to save mapping: ' + error.message, 'Save mapping', error);
        } finally {
            saveMappingBtn.disabled = false;
            if (globalLoadingToken) window.LoadingUI.stopGlobalLoading(globalLoadingToken);
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
            showUploadFailure('⚠️ Please select a client first from the Dashboard.');
            return;
        }
        if (!selectedTemplateId) {
            showUploadFailure('⚠️ Select a template first.');
            return;
        }

        const confirmed = window.confirm('Remove the saved column mapping for this client + template? Future uploads will fall back to auto-mapping until you save a new one.');
        if (!confirmed) return;

        clearMappingBtn.disabled = true;
        const globalLoadingToken = window.LoadingUI
            ? window.LoadingUI.startGlobalLoading(this)
            : null;
        try {
            const res = await fetchUpload(API_BASE + `/api/upload/saved-mapping?client_id=${clientId}&template_id=${selectedTemplateId}`, {
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
            showUploadFailure('Failed to clear saved mapping: ' + error.message, 'Clear mapping', error);
        } finally {
            clearMappingBtn.disabled = false;
            if (globalLoadingToken) window.LoadingUI.stopGlobalLoading(globalLoadingToken);
        }
    });
}

// ---------- AUTO-APPLY SAVED MAPPING (Phase A2 of 3) ----------
// Called from autoMapColumns() right after the backend's own auto-map guess is in hand. Overrides
// a currentMappings entry with its saved excel_column ONLY when that exact header still exists on
// the current sheet (excelHeaders) -- so a saved mapping can never force a mismatch onto a sheet
// with different headers; anything that doesn't match keeps auto-mapping's own result untouched.
// Returns the number of columns actually applied (0 if none/no saved mapping/lookup failed).
async function applySavedMapping(isCurrent = () => true) {
    const clientId = getCurrentClientId();
    if (!clientId || !selectedTemplateId) return 0;

    let data;
    try {
        const res = await fetchUpload(API_BASE + `/api/upload/saved-mapping?client_id=${clientId}&template_id=${selectedTemplateId}`);
        if (!res.ok) return 0;
        data = await res.json();
    } catch (error) {
        if (!isCurrent()) return 0;
        console.warn('⚠️ Could not check for a saved column mapping:', error);
        showUploadIssue({stage: 'Saved mapping', severity: 'warning', message: error.message,
            action: 'Auto-mapping is available. Check every selected column before validation; retry the saved mapping after reconnecting.'});
        return 0;
    }

    if (!isCurrent() || !data || !data.has_saved_mapping || !Array.isArray(data.mappings) || data.mappings.length === 0) {
        return 0;
    }

    let appliedCount = 0;
    data.mappings.forEach(saved => {
        const target = currentMappings.find(m => m.column_id === saved.template_column_id);
        if (!target) return;
        if (!saved.excel_column || !excelHeaders.includes(saved.excel_column)) return; // header no longer present -- fall back to auto-mapping
        target.excel_column = saved.excel_column;
        target.is_matched = true;
        appliedCount++;
    });
    return appliedCount;
}

// ---------- AUTO-MAP ----------
async function autoMapColumns() {
    if (!selectedTemplateId || !currentSheet || !fileId) {
        showUploadFailure('⚠️ Please select a template, a sheet, and upload a file.');
        return;
    }

    const generation = ++uploadMappingGeneration;
    const scope = {fileId, templateId: selectedTemplateId, sheet: currentSheet, templateGeneration: uploadTemplateGeneration};
    const isCurrent = () => generation === uploadMappingGeneration && scope.fileId === fileId
        && scope.templateId === selectedTemplateId && scope.sheet === currentSheet
        && scope.templateGeneration === uploadTemplateGeneration;
    try {
        showStatus('⏳ Auto‑mapping columns...', 'loading');
        setBadge('⏳ Mapping...', 'loading');

        const res = await fetchUpload(API_BASE + '/api/upload/auto-map', {
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
        if (!isCurrent()) return;
        currentMappings = data.mappings;
        excelHeaders = data.excel_headers;

        clearRestorePreview(); // Real data has arrived -- drop any grayed-out restore preview first

        // Auto-apply a previously saved mapping (Phase A2) on top of the backend's own auto-map
        // guess -- only overrides columns whose saved Excel header still exists on this sheet, so
        // it can never force a mismatch; anything else keeps auto-mapping's own result untouched.
        const savedMappingAppliedCount = await applySavedMapping(isCurrent);
        if (!isCurrent()) return;

        renderMapping(currentMappings);
        // populateUniqueIdDropdown() removed – no longer needed

        if (modeActions) modeActions.style.display = 'block';
        if (mappingPanel) mappingPanel.style.display = 'block';
        if (leftPanel) leftPanel.style.display = 'block';
        if (importBtn) importBtn.disabled = false;

        const matched = currentMappings.filter(m => m.is_matched).length;
        if (mappingCount) mappingCount.textContent = matched + '/' + data.total_columns + ' columns mapped';
        setBadge(matched + '/' + data.total_columns + ' columns mapped');
        let mapStatusMsg = '✅ Auto‑mapped ' + matched + '/' + data.total_columns + ' columns';
        if (savedMappingAppliedCount > 0) {
            mapStatusMsg += ` (${savedMappingAppliedCount} from your saved mapping)`;
        }
        showStatus(mapStatusMsg, 'success');

        updatePreviewButtonState();

        // Date columns are now known via the mapping. Build the complete row set first so
        // delimiter detection can inspect every mapped date value before refreshing the preview.
        setDateFormatGroupVisible(true);

        // Mapping is complete – show the row selection grid (Phase 2 of row-exclusion).
        renderRowExclusionGrid();
        autoSelectDateDelimiterFromMappedRows();
        refreshDateFormatPreview();

        // Mapping resolved (Phase 4) -- enable Go to Data Bank and advance the step strip to
        // "Map columns". Preview's own visibility/enabled-state is driven by
        // updatePreviewButtonState() (called just below), not this group any more.
        updateGoToDataBankState(true);
        updateStepIndicator(3);
        saveUploadSessionState(); // Mapping resolved -- checkpoint the full session state

    } catch (error) {
        if (!isCurrent()) return;
        if (importBtn) importBtn.disabled = true;
        console.error('Auto-map error:', error);
        setBadge('❌ Mapping failed', 'error');
        showUploadFailure('Auto-mapping failed: ' + error.message, 'Column mapping', error);
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
        const setMappingStatus = (element, matched) => {
            element.textContent = matched ? '✓ Mapped' : 'Choose column';
            element.classList.toggle('is-unmapped', !matched);
            element.setAttribute('aria-label', matched ? 'Mapped' : 'Choose an Excel column');
            element.title = matched ? 'Mapped' : 'Choose an Excel column';
        };

        const td1 = document.createElement('td');
        td1.textContent = m.column_name;
        td1.style.fontWeight = '500';
        td1.style.whiteSpace = 'nowrap';
        tr.appendChild(td1);

        const td2 = document.createElement('td');
        const select = document.createElement('select');
        select.dataset.rowIndex = rowIndex;
        select.dataset.columnId = m.column_id;
        select.setAttribute('aria-label', `${m.column_name}: Excel column`);

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
                const status = tr.querySelector('.mapping-status');
                if (status) setMappingStatus(status, false);
                const compare = tr.querySelector('input[data-compare-column]');
                if (compare) compare.disabled = true;
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
            const status = tr.querySelector('.mapping-status');
            if (status) setMappingStatus(status, m.is_matched);
            const compare = tr.querySelector('input[data-compare-column]');
            if (compare) compare.disabled = !m.is_matched;
            if (m.data_type === 'Date') autoSelectDateDelimiterFromMappedRows();
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
        icon.className = 'mapping-status';
        icon.style.fontSize = '16px';
        setMappingStatus(icon, m.is_matched);
        td4.appendChild(icon);
        tr.appendChild(td4);

        mappingBody.appendChild(tr);
    });

     updateCounts();

    function updateCounts() {
        const matched = currentMappings.filter(m => m.is_matched).length;
        if (mappingCount) mappingCount.textContent = matched + '/' + currentMappings.length + ' columns mapped';
        setBadge(matched + '/' + currentMappings.length + ' columns mapped');
    }
}

// ---------- UNIQUE ID DROPDOWN (removed – upsert is automatic) ----------

// ---------- DOWNLOAD TEMPLATE ----------
if (downloadBtn) {
    downloadBtn.addEventListener('click', function() {
        const templateId = this.dataset.templateId;
        if (!templateId) {
            showUploadFailure('⚠️ Please select a template first');
            return;
        }
        window.open(API_BASE + `/api/upload/download-template/${templateId}`, '_blank');
    });
}

// ---------- RESET ----------
if (resetBtn) {
    resetBtn.addEventListener('click', resetUpload);
}
function sourceFootingReportRows(sourceReconciliation) {
    if (!sourceReconciliation?.checks?.length) return [];
    const policy = sourceReconciliation.tolerance_policy || {};
    return [
        ['Source footing controls'],
        ['Control-total row', sourceReconciliation.control_total_row ?? 'Unavailable'],
        ['Decision', sourceReconciliation.status || 'Unavailable'],
        ['Auto-accept below', policy.auto_accept_below ?? '0.01'],
        ['Confirmation limit', policy.confirmation_limit ?? '1.00'],
        ['Variance confirmed', policy.variance_confirmed ? 'Yes' : 'No'],
        ['Full-sheet rows', sourceReconciliation.full_sheet?.row_count ?? 'Unavailable'],
        ['Unique documents', sourceReconciliation.full_sheet?.document_count ?? 'Unavailable'],
        ['Footing', 'Excel total', 'Detail total', 'Detail minus Excel', 'Decision'],
        ...sourceReconciliation.checks.map(check => [check.column, check.excel_total,
            check.detail_total, check.difference, check.status || sourceReconciliation.status])
    ];
}

function downloadChangeReport(report, summary = {}, reconciliation = null, sourceReconciliation = null) {
    const reportRows = Array.isArray(report) ? report : [];
    const dateRange = summary.date_range || {};
    const header = [
        'Upload ID', 'Upload Date', 'Uploaded By', 'File Name', 'Template',
        'Comparison Mode', 'Date Range Start', 'Date Range End', 'Total Lines',
        'Added', 'Changed', 'Removed', 'Unchanged', 'Unchecked-field refreshes', 'Action', 'Client Data ID',
        'Document ID', 'Line Key', 'Changed Fields', 'Old Values', 'New Values'
    ];
    const metadata = [
        summary.upload_id,
        summary.upload_date,
        summary.uploaded_by,
        summary.file_name,
        summary.template_name,
        summary.comparison_mode,
        dateRange.start,
        dateRange.end,
        summary.total_rows,
        summary.added_count,
        summary.changed_count,
        summary.removed_count,
        summary.unchanged_count,
        summary.informational_update_count,
    ];
    const rows = reportRows.length
        ? reportRows.map(item => [
            ...metadata,
            item.action,
            item.client_data_id,
            item.document_id,
            item.line_key,
            item.changed_fields,
            item.old_values,
            item.new_values,
        ])
        : [[...metadata, 'SUMMARY', '', '', '', '', '', '']];
    // Create the review workbook in the browser; no report file is retained by
    // the server. Explicit strings remain text, including formula-like values.
    const workbook = XLSX.utils.book_new();
    const values = [header, ...rows].map(row => row.map(value =>
        Array.isArray(value) ? value.join('; ') : value && typeof value === 'object'
            ? JSON.stringify(value) : value ?? ''));
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet(values), 'Upload review');
    if (reconciliation) {
        const scope = reconciliation.scope || {};
        const period = scope.date_range || {};
        const checks = [
            ['Import reconciliation'],
            ['Upload ID', scope.upload_id || summary.upload_id || 'Unavailable'],
            ['Client', scope.client_name || scope.client_id || 'Unavailable'],
            ['Template', scope.template_name || scope.template_id || 'Unavailable'],
            ['Period', `${period.start || 'Unavailable'} – ${period.end || 'Unavailable'}`],
            ['Checked at', reconciliation.checked_at || 'Unavailable'],
            ['Status', reconciliation.status],
            ['Result', reconciliation.message],
            ['Document ID', reconciliation.document_column || 'Unavailable'],
            ['Footing', reconciliation.footing_configured ? 'Configured' : 'Not configured'],
            ['Metric', 'Excel detail', 'Stored data', 'Stored minus Excel'],
            ...(reconciliation.rows || []).map(row => [row.label, row.excel ?? 'Unavailable',
                row.stored ?? 'Unavailable', row.difference ?? 'Not comparable'])
        ];
        XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet(checks), 'Reconciliation');
    }
    const sourceChecks = sourceFootingReportRows(sourceReconciliation);
    if (sourceChecks.length) {
        XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet(sourceChecks), 'Source footing');
    }
    XLSX.writeFile(workbook, `upload-change-report-${summary.upload_id || 'latest'}.xlsx`);
}

function attachValidationReportDownload(report, summary, sourceReconciliation = null) {
    const button = document.getElementById('downloadValidationReportBtn');
    if (button) {
        button.addEventListener('click', () => downloadChangeReport(report, summary, null, sourceReconciliation));
    }
}

async function downloadFullChangeReport(report, summary = {}, reconciliation = null, sourceReconciliation = null) {
    const scope = reconciliation?.scope || {};
    const informational = (Array.isArray(report) ? report : []).filter(item => item.action === 'Informational update').map(item => ({
        client_data_id: item.client_data_id,
        field_updates: item.new_values || {},
        old_field_values: item.old_values || {},
    }));
    const response = await fetch(`${API_BASE}/api/upload/change-report`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            client_id: Number(summary.client_id || window.getSelectedClientId?.()),
            template_id: Number(summary.template_id || selectedTemplateId),
            file_id: Number(summary.upload_id || fileId),
            date_range: scope.date_range || summary.date_range || {},
            comparison_mode: summary.comparison_mode || comparisonModeDropdown?.value,
            reconciliation: reconciliation || null, informational_updates: informational,
        })
    });
    if (!response.ok) {
        let message = 'Could not prepare the upload report.';
        try { message = (await response.json()).detail || message; } catch (_) {}
        throw new Error(message);
    }
    let blob = await response.blob();
    const sourceChecks = sourceFootingReportRows(sourceReconciliation);
    if (sourceChecks.length) {
        const workbook = XLSX.read(await blob.arrayBuffer(), {type: 'array'});
        if (!workbook.SheetNames.includes('Source footing')) {
            XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet(sourceChecks), 'Source footing');
            blob = new Blob([XLSX.write(workbook, {bookType: 'xlsx', type: 'array'})],
                {type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
        }
    }
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `upload-change-report-${summary.upload_id || fileId || 'latest'}.xlsx`;
    document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
}

// ---------- IMPORT SUMMARY POPUP ----------
function renderImportReconciliation(container, reconciliation) {
    container.className = 'result-reconciliation';
    const result = reconciliation || {status: 'unavailable', rows: [],
        message: 'Stored-data verification is unavailable for this result. No reconciliation is certified.'};
    const heading = document.createElement('strong'); heading.textContent = 'Import reconciliation';
    container.appendChild(heading);
    const scope = result.scope || {}, period = scope.date_range || {};
    const context = document.createElement('p');
    context.textContent = `${scope.client_name || `Client ${scope.client_id ?? 'unavailable'}`} · ${scope.template_name || `Template ${scope.template_id ?? 'unavailable'}`} · ${period.start || 'Period unavailable'}${period.end ? ` – ${period.end}` : ''}${scope.ledger_account_code ? ` · Account ${scope.ledger_account_code}` : ''}`;
    container.appendChild(context);
    const message = document.createElement('p'); message.dataset.status = result.status;
    message.setAttribute('role', result.status === 'matched' ? 'status' : 'alert');
    message.textContent = result.message;
    container.appendChild(message);
    const table = document.createElement('table');
    const head = table.createTHead().insertRow();
    ['Metric', 'Excel detail', 'Stored data'].forEach(label => {
        const th = document.createElement('th'); th.scope = 'col'; th.textContent = label; head.appendChild(th);
    });
    const body = table.createTBody();
    (result.rows || []).forEach(row => {
        const tr = body.insertRow(); tr.dataset.matches = String(row.matches);
        tr.insertCell().textContent = row.label;
        tr.insertCell().textContent = row.excel == null ? 'Unavailable' : formatSourceControlAmount(row.excel);
        tr.insertCell().textContent = row.stored == null ? 'Unavailable' : formatSourceControlAmount(row.stored);
        if (row.matches === false) tr.title = `Stored minus Excel: ${formatSourceControlAmount(row.difference)}`;
    });
    const scroll = document.createElement('div'); scroll.className = 'reconciliation-table-scroll';
    scroll.appendChild(table); container.appendChild(scroll);
    const differences = (result.rows || []).filter(row => row.matches === false);
    if (differences.length) {
        const note = document.createElement('p');
        note.dataset.status = 'mismatch';
        note.textContent = `Stored minus Excel: ${differences.map(row => `${row.label}: ${formatSourceControlAmount(row.difference)}`).join(' · ')}.`;
        container.appendChild(note);
    }
    const details = document.createElement('p');
    details.textContent = `Document ID: ${result.document_column || 'Unavailable'}.${result.footing_configured ? '' : ' Footing not configured.'}${result.checked_at ? ` Checked: ${result.checked_at}.` : ''}`;
    container.appendChild(details);
    if (result.status === 'unavailable') {
        const retry = document.createElement('p');
        retry.textContent = 'Validate a fresh copy of the source to retry the check. Import is not repeated automatically.';
        container.appendChild(retry);
    }
}

function renderSourceFootingReconciliation(container, sourceReconciliation) {
    if (!sourceReconciliation?.checks?.length) return;
    container.className = 'result-reconciliation';
    container.dataset.sourceFooting = 'true';
    const heading = document.createElement('strong');
    heading.textContent = 'Source footing controls';
    container.appendChild(heading);
    const statusText = {
        matched: 'Excel controls match the detail totals exactly.',
        rounding_tolerated: 'A difference below 0.01 was accepted as rounding noise; amounts were not changed.',
        confirmed_variance: 'Variance confirmed by the user; exact source and detail totals are retained below.'
    }[sourceReconciliation.status] || 'Source footing variance requires review.';
    const status = document.createElement('p');
    status.dataset.status = sourceReconciliation.status || 'unavailable';
    status.textContent = statusText;
    container.appendChild(status);
    const table = document.createElement('table');
    const header = table.createTHead().insertRow();
    ['Footing', 'Excel total', 'Detail total', 'Difference', 'Decision'].forEach(label => {
        const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = label; header.appendChild(cell);
    });
    const body = table.createTBody();
    sourceReconciliation.checks.forEach(check => {
        const row = body.insertRow();
        [check.column, check.excel_total, check.detail_total, check.difference, check.status || sourceReconciliation.status]
            .forEach((value, index) => row.insertCell().textContent = index ? formatSourceControlAmount(value) : String(value ?? 'Unavailable'));
    });
    const scroll = document.createElement('div');
    scroll.className = 'reconciliation-table-scroll';
    scroll.appendChild(table);
    container.appendChild(scroll);
    const row = document.createElement('p');
    row.textContent = `Excel control row ${sourceReconciliation.control_total_row ?? 'unavailable'} · ${sourceReconciliation.full_sheet?.row_count ?? 'Unavailable'} detail rows · ${sourceReconciliation.full_sheet?.document_count ?? 'Unavailable'} unique documents.`;
    container.appendChild(row);
}

function showImportSummary(result, ledgerPresentation = null) {
    const priorOverlay = document.getElementById('importSummaryOverlay');
    if (priorOverlay?._summaryKeydown) document.removeEventListener('keydown', priorOverlay._summaryKeydown);
    priorOverlay?.remove();
    const previousFocus = document.activeElement;
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
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'importSummaryTitle');
    modal.tabIndex = -1;
    modal.style.cssText = `
        background: white; border-radius: 12px; padding: 30px;
        max-width: 600px; width: calc(100% - 24px); box-sizing: border-box; box-shadow: 0 20px 60px rgba(0,0,0,0.3);
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
    const informationalUpdated = Number(result.informational_updated_count ?? cd.informational_update_count ?? 0);
    const changed = Number(result.updated_count ?? cd.changed_count ?? 0);
    const newRows = Number(result.imported_count ?? cd.new_count ?? 0);
    const deleted = Number(result.deleted_count ?? cd.missing_count ?? 0);
    const deletedOutcomeLabel = cd.comparison_mode === 'correction'
        ? 'Deleted — not used in correction uploads'
        : 'Deleted — removed';
    const changeReport = cd.report || [];
    const uploadSummary = result.upload_summary || {
        upload_id: result.reconciliation?.scope?.upload_id,
        template_name: result.reconciliation?.scope?.template_name,
        date_range: result.reconciliation?.scope?.date_range,
        total_rows: result.total_rows, added_count: inserted, changed_count: updated, removed_count: deleted
    };
    const resultScope = result.reconciliation?.scope || {};
    const storedPreviewContext = {
        client_id: Number(uploadSummary.client_id ?? resultScope.client_id ?? window.getSelectedClientId?.()),
        template_id: Number(uploadSummary.template_id ?? resultScope.template_id ?? selectedTemplateId),
        file_id: Number(uploadSummary.upload_id ?? resultScope.upload_id ?? fileId),
        date_start: uploadSummary.date_range?.start || resultScope.date_range?.start,
        date_end: uploadSummary.date_range?.end || resultScope.date_range?.end,
        ledger_account_code: resultScope.ledger_account_code || null,
        reconciliationStatus: result.reconciliation?.status || 'unavailable',
        informationalClientDataIds: changeReport.filter(item => item.action === 'Informational update')
            .map(item => Number(item.client_data_id)).filter(Number.isFinite),
    };
    const canShowStoredPreview = storedPreviewContext.client_id && storedPreviewContext.template_id
        && storedPreviewContext.file_id && storedPreviewContext.date_start && storedPreviewContext.date_end;
    
    let statusIcon = '✅';
    let statusColor = '#16a34a';
    let statusText = result.ignored ? (result.reconciliation?.status === 'matched' ? 'No changes required' : 'No rows imported') : 'Import Successful';
    if (result.reconciliation && ['mismatch', 'unavailable', 'incomplete'].includes(result.reconciliation.status)) {
        statusIcon = '⚠️'; statusColor = '#92400e';
        if (!result.ignored) statusText = 'Import completed — review reconciliation';
    }
    if (errorCount > 0) {
        statusIcon = '⚠️';
        statusColor = '#f59e0b';
        statusText = 'Import with Warnings';
    }

    // Build a plain, single-line outcome summary (no inline emoji, no run-on sentence).
    let detailMessage = '';
    if (result.ignored) {
        detailMessage = 'All validated transactions were already in the database. No rows were imported or changed.';
    } else {
        const appliedActions = [];
        if (inserted > 0) appliedActions.push(`${inserted} new row${inserted === 1 ? '' : 's'} added`);
        if (updated > 0) appliedActions.push(`${updated} existing row${updated === 1 ? '' : 's'} changed`);
        if (informationalUpdated > 0) appliedActions.push(`${informationalUpdated} row${informationalUpdated === 1 ? '' : 's'} refreshed without transaction changes`);
        if (deleted > 0) appliedActions.push(`${deleted} old row${deleted === 1 ? '' : 's'} deleted`);
        if (informationalUpdated > 0) appliedActions.push(`${informationalUpdated} row${informationalUpdated === 1 ? '' : 's'} refreshed; transactions unchanged`);
        detailMessage = appliedActions.length ? appliedActions.join(' · ') : 'No transaction rows changed.';
    }

    // Change Detection Summary: labeled stat block matching the tile styling below, color-coded via left border instead of emoji.
    let changeSummary = '';
    if (unchanged > 0 || changed > 0 || newRows > 0 || deleted > 0 || informationalUpdated > 0) {
        changeSummary = `
            <div style="margin-top:20px;">
                <div style="font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:0.04em; color:#94a3b8; margin-bottom:8px;">Import result by transaction row</div>
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
                    <div style="background:#f8fafc; border-left:3px solid #94a3b8; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${unchanged}</div>
                        <div style="font-size:11px; color:#64748b;">Unchanged — kept in database</div>
                    </div>
                    <div style="background:#f8fafc; border-left:3px solid #f59e0b; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${changed}</div>
                        <div style="font-size:11px; color:#64748b;">Changed — updated</div>
                    </div>
                    <div style="background:#f8fafc; border-left:3px solid #16a34a; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${newRows}</div>
                        <div style="font-size:11px; color:#64748b;">Incoming new — added</div>
                    </div>
                    <div style="background:#f8fafc; border-left:3px solid #dc2626; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${deleted}</div>
                        <div style="font-size:11px; color:#64748b;">${deletedOutcomeLabel}</div>
                    </div>
                    ${informationalUpdated > 0 ? `<div style="background:#f8fafc; border-left:3px solid #0f766e; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${informationalUpdated}</div>
                        <div style="font-size:11px; color:#64748b;">Rows refreshed; transaction unchanged</div>
                    </div>` : ''}
                </div>
                ${changed > 0 ? '<div style="font-size:11px; color:#94a3b8; margin-top:8px;">Changed rows were reset to Pending and require re-verification.</div>' : ''}
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

    // The rows are saved even when the follow-up Cashflows job could not be queued;
    // say so plainly so a saved upload is never mistaken for a failed one.
    // The text itself is set via textContent once the modal exists (no HTML injection).
    const preparationWarning = result.cashflow_preparation_warning
        ? `<div id="importSummaryPreparationWarning" style="font-size:12px; color:#92400e; background:#fffbeb; border-left:3px solid #f59e0b; padding:10px 12px; border-radius:6px; margin-top:16px;"></div>`
        : '';

    const savedReportStatus = result.change_report
        ? 'Verified Excel change report saved in Google Drive.'
        : result.backup_mode === 'none'
            ? 'Development mode: no recovery ZIP or saved Excel report was created.'
            : '';

    // Explanatory note about debit/credit lines, styled as a plain muted info box.
    const note = `<div style="font-size:12px; color:#64748b; background:#f8fafc; padding:10px 12px; border-radius:6px; margin-top:16px;">
        <strong style="color:#475569;">Note:</strong> A voucher can contain several accounting lines.
        Every valid line is preserved. Unchanged rows stay in the database and are not imported again.
    </div>`;

    modal.innerHTML = `
        <div style="text-align:center; margin-bottom:20px;">
            <div style="font-size:40px; line-height:1;">${statusIcon}</div>
            <h2 id="importSummaryTitle" style="margin:10px 0 4px; color:#1a1a2e; font-size:20px;">${statusText}</h2>
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
        <div id="importReconciliation"></div>
        ${changeSummary}
        ${rejectionSummary}
        ${preparationWarning}
        ${savedReportStatus ? '<div id="importSummarySavedReport" style="font-size:12px; color:#166534; background:#f0fdf4; border-left:3px solid #22c55e; padding:10px 12px; border-radius:6px; margin-top:16px;"></div>' : ''}
        ${note}
        ${errorCount > 0 ? `
            <div style="background:#fef2f2; border:1px solid #fecaca; padding:12px; border-radius:8px; margin-top:16px; max-height:150px; overflow-y:auto;">
                <div style="font-weight:600; color:#991b1b; margin-bottom:4px; font-size:13px;">${errorCount} validation error${errorCount === 1 ? '' : 's'}</div>
                ${errors.slice(0,5).map(err => `
                    <div style="font-size:12px; color:#991b1b; padding:2px 0; border-bottom:1px solid #fecaca;">
                        Row ${err.row_number}: ${err.errors.map(e => `${e.column || 'Row-level'} - ${e.message}`).join('; ')}
                    </div>
                `).join('')}
                ${errors.length > 5 ? `<div style="font-size:12px; color:#991b1b; margin-top:2px;">...and ${errors.length - 5} more.</div>` : ''}
            </div>
        ` : ''}
        <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:20px; flex-wrap:wrap;">
            <a href="/static/databank.html" target="_blank" style="padding:8px 20px; background:#3b82f6; color:white; border:none; border-radius:6px; font-weight:600; cursor:pointer; text-decoration:none; display:inline-block; font-size:14px;">View in Data Bank</a>
            ${canShowStoredPreview ? '<button id="viewImportedDataBtn" style="padding:8px 20px; background:#28446b; color:white; border:0; border-radius:6px; font-weight:600; cursor:pointer; font-size:14px;">👁 Preview stored data</button>' : ''}
            <button id="downloadChangeReportBtn" style="padding:8px 20px; background:#0f766e; color:white; border:none; border-radius:6px; font-weight:600; cursor:pointer; font-size:14px;">Download upload report</button>
            <button id="importSummaryCloseBtn" style="padding:8px 24px; background:#e2e8f0; color:#1a1a2e; border:none; border-radius:6px; font-weight:600; cursor:pointer; font-size:14px;">Close</button>
        </div>
    `;
    
    if (ledgerPresentation || isLedgerUpload()) {
        const context = document.createElement('p');
        context.textContent = ledgerPresentation || `${ledgerUploadLabel()} · ${uploadCoverageStart.value} through ${uploadCoverageEnd.value}`;
        modal.prepend(context);
    }
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    renderImportReconciliation(overlay.querySelector('#importReconciliation'), result.reconciliation);
    const sourceReconciliationBox = document.createElement('div');
    renderSourceFootingReconciliation(sourceReconciliationBox, result.source_reconciliation);
    if (sourceReconciliationBox.hasChildNodes()) overlay.querySelector('#importReconciliation').after(sourceReconciliationBox);

    // A verified no-op opens its final result during validation. Keep source-row
    // notes in that same dialog instead of leaving a warning dialog underneath it.
    const source = result.ignored ? result.source_diagnostics : null;
    if (source && (source.excluded_count || source.warnings?.length)) {
        const section = document.createElement('section');
        section.className = 'result-source-notes';
        const heading = document.createElement('strong');
        heading.textContent = 'Source notes';
        section.appendChild(heading);
        if (source.excluded_count) {
            const count = document.createElement('p');
            count.textContent = `${source.excluded_count} blank or total source row${source.excluded_count === 1 ? '' : 's'} skipped.`;
            section.appendChild(count);
        }
        const notes = [...(source.warnings || []), ...(source.excluded_rows || []).map(row => ({
            row_number: row.row_number,
            message: `Excluded ${row.kind || 'source'} row${row.label ? ': ' + row.label : ''}`
        }))];
        if (notes.length) {
            const list = document.createElement('ul');
            notes.slice(0, 50).forEach(note => {
                const item = document.createElement('li');
                item.textContent = `${note.row_number == null ? 'Source row' : `Row ${note.row_number}`}${note.column ? ` · ${note.column}` : ''}: ${note.message || ''}`;
                list.appendChild(item);
            });
            if (notes.length > 50) {
                const more = document.createElement('li');
                more.textContent = `${notes.length - 50} more source notes are available in the upload review.`;
                list.appendChild(more);
            }
            section.appendChild(list);
        }
        overlay.querySelector('#importReconciliation').after(section);
    }

    const preparationWarningBox = overlay.querySelector('#importSummaryPreparationWarning');
    if (preparationWarningBox) {
        preparationWarningBox.textContent = result.cashflow_preparation_warning;
    }

    const reportBox = overlay.querySelector('#importSummarySavedReport');
    if (reportBox) {
        reportBox.textContent = savedReportStatus;
        if (result.change_report) {
            for (const [label, value] of [['Open saved Excel report', result.change_report.file_url],
                ['Open dated report folder', result.change_report.folder_url]]) {
                try {
                    const url = new URL(value);
                    if (url.protocol !== 'https:' || url.hostname !== 'drive.google.com') continue;
                    const link = document.createElement('a');
                    link.href = url.href;
                    link.target = '_blank';
                    link.rel = 'noopener';
                    link.textContent = label;
                    link.style.display = 'block';
                    reportBox.appendChild(link);
                } catch (_) { /* Do not render an invalid or untrusted link. */ }
            }
            if (result.change_report.folder_path) {
                const location = document.createElement('small');
                location.textContent = 'Drive folder: ' + result.change_report.folder_path;
                location.style.display = 'block';
                reportBox.appendChild(location);
            }
        }
    }

    const downloadReportBtn = overlay.querySelector('#downloadChangeReportBtn');
    if (downloadReportBtn) {
        downloadReportBtn.addEventListener('click', async () => {
            downloadReportBtn.disabled = true;
            const priorText = downloadReportBtn.textContent;
            downloadReportBtn.textContent = 'Preparing report…';
            try { await downloadFullChangeReport(changeReport, uploadSummary, result.reconciliation, result.source_reconciliation); }
            catch (error) { showStatus(error.message, 'error'); }
            finally { downloadReportBtn.disabled = false; downloadReportBtn.textContent = priorText; }
        });
    }
    const storedPreviewBtn = overlay.querySelector('#viewImportedDataBtn');
    if (storedPreviewBtn) {
        storedPreviewBtn.addEventListener('click', () => {
            document.removeEventListener('keydown', overlay._summaryKeydown);
            overlay.style.display = 'none';
            showDatabasePreview(storedPreviewContext, () => {
                overlay.style.display = 'flex';
                document.addEventListener('keydown', overlay._summaryKeydown);
                storedPreviewBtn.focus({preventScroll: true});
            });
        });
    }
    
    // Keep keyboard users inside the result until it is closed, then return
    // focus to the control that opened it.
    const closeSummary = () => {
        if (!overlay.isConnected) return;
        document.removeEventListener('keydown', overlay._summaryKeydown);
        overlay.remove();
        if (!result.ignored) resetUpload();
        const reopenButton = result.ignored ? document.getElementById('reopenIgnoredResult') : null;
        if (reopenButton?.isConnected) reopenButton.focus({preventScroll: true});
        else if (previousFocus?.isConnected) previousFocus.focus({preventScroll: true});
    };
    overlay.querySelector('#importSummaryCloseBtn').addEventListener('click', closeSummary);
    // Close on overlay click (background)
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) closeSummary();
    });
    overlay._summaryKeydown = event => {
        if (!overlay.isConnected) return;
        if (event.key === 'Escape') { event.preventDefault(); closeSummary(); return; }
        if (event.key !== 'Tab') return;
        const controls = [...modal.querySelectorAll('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')]
            .filter(control => control.getClientRects().length);
        const first = controls[0], last = controls[controls.length - 1];
        if (!first) { event.preventDefault(); modal.focus(); }
        else if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
            event.preventDefault(); last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
            event.preventDefault(); first.focus();
        }
    };
    document.addEventListener('keydown', overlay._summaryKeydown);
    overlay.querySelector('#importSummaryCloseBtn').focus();
}
// ---------- SHOW VALIDATION ERRORS ----------
// Reuse the existing issue dialog for every stage; page row details to keep
// even a large rejected workbook responsive. All source text uses textContent.
function showUploadIssue({stage = 'Validation', severity = 'error', message, action, focusTarget, result, rows = [], confirmLabel = null}) {
    document.getElementById('validationErrorOverlay')?.remove();
    const previousFocus = document.activeElement;
    const overlay = document.createElement('div');
    overlay.id = 'validationErrorOverlay';
    const modal = document.createElement('section');
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'uploadIssueTitle');
    modal.dataset.severity = severity;
    overlay.dataset.stage = stage;
    const add = (tag, text, parent = modal) => {
        const node = document.createElement(tag);
        node.textContent = text;
        parent.appendChild(node);
        return node;
    };
    const validation = stage === 'Validation' && severity === 'error' &&
        (result?.errors?.length || result?.rejected_rows?.length);
    const header = add('header', ''); header.className = 'upload-issue-header';
    const title = severity === 'unconfirmed' ? 'Import outcome unconfirmed'
        : severity === 'warning' ? 'Upload warning' : severity === 'info' ? 'Source rows recognised'
        : validation ? 'Fix upload errors' : 'Upload issue';
    add('h2', title, header).id = 'uploadIssueTitle';
    add('p', stage, header).className = 'upload-issue-stage';
    add('p', message || result?.message || 'Review the upload details.').className = 'upload-issue-summary';
    if (action) add('p', action).className = 'upload-issue-action';
    const details = [...rows];
    const sourceRows = new Map();
    for (const item of [...(result?.errors || []), ...(result?.rejected_rows || [])]) {
        const reasons = item.errors || item.reasons || [];
        const key = item.row_number == null ? item : String(item.row_number);
        if (!sourceRows.has(key)) sourceRows.set(key, {row_number: item.row_number, reasons: [],
            voucher: item.row_data ? Object.values(item.row_data)[0] : ''});
        sourceRows.get(key).reasons.push(...reasons);
    }
    details.push(...sourceRows.values());
    if (details.length) {
        const tableBox = add('div', ''); tableBox.className = 'upload-issue-details';
        const table = add('table', '', tableBox);
        const head = add('tr', '', add('thead', '', table));
        ['Excel row', 'Voucher', 'What to fix'].forEach(label => add('th', label, head));
        const body = add('tbody', '', table);
        const nav = add('div', ''); nav.className = 'upload-issue-navigation';
        const prev = add('button', 'Previous', nav), label = add('span', '', nav), next = add('button', 'Next', nav);
        let page = 0;
        const render = () => {
            body.replaceChildren();
            details.slice(page * 50, page * 50 + 50).forEach(item => {
                const row = add('tr', '', body);
                const rowNumberCell = add('td', '', row);
                if (item.row_number !== undefined && item.row_number !== null) {
                    const rowLink = add('button', item.row_number, rowNumberCell);
                    rowLink.type = 'button';
                    rowLink.className = 'upload-issue-row-link';
                    rowLink.title = `Go to Excel row ${item.row_number}`;
                    rowLink.addEventListener('click', () => {
                        overlay.remove();
                        focusUploadSourceRow(item.row_number);
                    });
                } else {
                    rowNumberCell.textContent = '—';
                }
                add('td', item.voucher || '—', row);
                const fix = add('td', '', row);
                // Keep the affected field distinct from the instruction, using
                // text nodes so workbook/server content can never become HTML.
                for (const reason of item.reasons || [item]) {
                    const entry = add('div', '', fix); entry.className = 'upload-issue-item';
                    const field = reason.column || reason.excel_column || reason.role;
                    if (field) add('span', field, entry).className = severity === 'error' ? 'upload-issue-field' : 'upload-issue-label';
                    add('span', reason.message || '', entry).className = 'upload-issue-fix';
                }
            });
            label.textContent = `Showing ${page * 50 + 1}–${Math.min(details.length, page * 50 + 50)} of ${details.length.toLocaleString()}`;
            prev.disabled = page === 0; next.disabled = (page + 1) * 50 >= details.length;
        };
        prev.addEventListener('click', () => { page--; render(); });
        next.addEventListener('click', () => { page++; render(); });
        render();
    }
    const footer = add('footer', ''); footer.className = 'upload-issue-footer';
    const close = add('button', confirmLabel ? 'Cancel' : 'Close', footer); close.id = 'validationErrorCloseBtn';
    let resolveConfirmation = null;
    const confirmation = confirmLabel ? new Promise(resolve => { resolveConfirmation = resolve; }) : null;
    const dismiss = () => {
        overlay.remove();
        const target = focusTarget && !focusTarget.disabled ? focusTarget : previousFocus;
        target?.scrollIntoView?.({block: 'center'});
        target?.focus?.({preventScroll: true});
        resolveConfirmation?.(false);
    };
    close.addEventListener('click', dismiss);
    if (confirmLabel) {
        const confirm = add('button', confirmLabel, footer);
        confirm.id = 'footingVarianceConfirmBtn';
        confirm.type = 'button';
        confirm.style.cssText = 'background:#b45309;color:white;border:0;border-radius:6px;padding:8px 14px;font-weight:600;cursor:pointer;';
        confirm.addEventListener('click', () => {
            overlay.remove();
            previousFocus?.focus?.({preventScroll: true});
            resolveConfirmation?.(true);
        });
    }
    overlay.addEventListener('click', event => { if (event.target === overlay) dismiss(); });
    overlay.appendChild(modal); document.body.appendChild(overlay); close.focus();
    return confirmation;
}

function showFootingVarianceConfirmation(result) {
    const controls = result?.source_reconciliation || {};
    const rows = (controls.checks || []).map(check => ({
        row_number: controls.control_total_row,
        voucher: `Footing: ${check.column}`,
        reasons: [
            {column: 'Excel total', message: formatSourceControlAmount(check.excel_total)},
            {column: 'Detail total', message: formatSourceControlAmount(check.detail_total)},
            {column: 'Difference', message: formatSourceControlAmount(check.difference)}
        ]
    }));
    return showUploadIssue({stage: 'Validation', severity: 'warning',
        message: 'Excel footing totals differ from the detail total by 0.01–1.00 currency units.',
        action: 'Review each exact difference. Continue only after checking it with client management. No transaction amount will be changed.',
        rows, confirmLabel: 'Confirm variance and revalidate'});
}

// Document-level handling also covers focus lost when a paging button becomes
// disabled on the final page. The dialog remains dismissible and traps Tab.
document.addEventListener('keydown', event => {
    const overlay = document.getElementById('validationErrorOverlay');
    if (!overlay) return;
    if (event.key === 'Escape') overlay.querySelector('#validationErrorCloseBtn').click();
    if (event.key === 'Tab') {
        const buttons = [...overlay.querySelectorAll('button:not(:disabled)')];
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (!overlay.contains(document.activeElement)) {
            event.preventDefault(); (event.shiftKey ? last : first).focus();
        } else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
});

function showValidationErrors(result) {
    const errorCount = Number(result.error_count || 0);
    const rejectedCount = Number(result.rejected_count || 0);
    const rowNumbers = new Set([...(result.errors || []), ...(result.rejected_rows || [])]
        .filter(row => row.row_number != null).map(row => String(row.row_number)));
    const affected = Math.max(rowNumbers.size, errorCount, rejectedCount);
    const errorLabel = affected === 1 ? '1 row needs correction' : `${affected.toLocaleString()} rows need correction`;
    const dateOnly = rejectedCount === 0 && result.errors?.length && result.errors.every(row =>
        row.errors?.length && row.errors.every(error => error.error_type === 'data_type' &&
            error.message === 'Enter a valid date in the selected format.'));
    showUploadIssue({stage: 'Validation', result,
        message: `${errorLabel}${rejectedCount ? ` · ${rejectedCount.toLocaleString()} rejected` : ''}`,
        action: dateOnly ? 'Check Date format, then validate again.' : 'Fix the marked fields, then validate again.'});
}

function focusUploadSourceRow(rowNumber) {
    const excelRow = Number(rowNumber);
    const index = excelRow - 2;
    const container = document.getElementById('rowExclusionContainer');
    if (!container || !Number.isInteger(excelRow) || index < 0 || index >= rowExclusionAllRows.length) return;
    rowExclusionCurrentPage = Math.floor(index / ROW_EXCLUSION_PAGE_SIZE) + 1;
    container.style.display = 'block';
    renderRowExclusionPage();
    const row = container.querySelector(`[data-excel-row-number="${excelRow}"]`);
    row?.scrollIntoView({block: 'center', behavior: 'smooth'});
    row?.querySelector('input[type="checkbox"]')?.focus({preventScroll: true});
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
    importBtn.addEventListener('click', function() {
        runUploadAction(this, async () => {
        // Ensure client is selected
        let clientId = null;
        if (typeof window.isClientSelected === 'function' && window.isClientSelected()) {
            clientId = window.getSelectedClientId();
        } else {
            clientId = localStorage.getItem('selectedClientId');
        }
        if (!clientId) {
            showUploadFailure('⚠️ Please select a client first from the Dashboard.');
            return;
        }
        if (!fileId || !selectedTemplateId) {
            showUploadFailure('⚠️ Please upload a file and select a template.');
            return;
        }

        if (!mappingBody) return;
        const rows = [...mappingBody.querySelectorAll('tr')];
        // Restored previews are text-only, and file mapping can still be in
        // flight. Never submit a partial mapping or read a missing dropdown.
        if (leftPanel?.classList.contains('restore-preview-locked') || !currentMappings.length
            || rows.length !== currentMappings.length || rows.some((tr, index) => {
                const select = tr.querySelector('select');
                return !select || select.disabled || Number(select.dataset.columnId) !== Number(currentMappings[index].column_id);
            })) {
            showUploadFailure('Re-select your source file and wait for column mapping to finish before validating or importing.', 'Column mapping');
            return;
        }
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

        const coverageStartDate = parseCoverageInputDate(uploadCoverageStart?.value);
        const coverageEndDate = parseCoverageInputDate(uploadCoverageEnd?.value);
        const needsCoverageDates = isLedgerUpload() || comparisonModeDropdown?.value === 'replace_range';
        if (needsCoverageDates && ((!coverageStartDate.valid || !coverageStartDate.iso)
            && Boolean(uploadCoverageStart?.value))) {
            showUploadFieldError(uploadCoverageStart, document.getElementById('uploadCoverageHelp'),
                `Enter a valid start date in ${selectedCoverageDateFormat()}.`);
            return;
        }
        if (needsCoverageDates && ((!coverageEndDate.valid || !coverageEndDate.iso)
            && Boolean(uploadCoverageEnd?.value))) {
            showUploadFieldError(uploadCoverageEnd, document.getElementById('uploadCoverageHelp'),
                `Enter a valid end date in ${selectedCoverageDateFormat()}.`);
            return;
        }

        const payload = {
            backup_enabled: uploadBackupRequired,
            template_id: selectedTemplateId,
            file_id: fileId,
            client_id: parseInt(clientId),
            mappings: finalMappings,
            mode: 'create',
            comparison_mode: comparisonModeDropdown ? comparisonModeDropdown.value : 'correction',
            control_total_row: sourceFootingMappings().length ? Number(getElement('sourceControlRow').value) || null : null,
            control_total_confirmed: Boolean(sourceFootingMappings().length && getElement('sourceControlConfirmed').checked),
            confirm_footing_variance: footingVarianceConfirmed,
            coverage_start: comparisonModeDropdown?.value === 'replace_range' ? coverageStartDate.iso || null : null,
            coverage_end: comparisonModeDropdown?.value === 'replace_range' ? coverageEndDate.iso || null : null,
            confirm_report_coverage: Boolean(uploadConfirmCoverage?.checked),
            confirm_empty_period: Boolean(uploadConfirmEmpty?.checked),
            validation_plan_id: validationPlanId,
            ledger_account_code: isLedgerUpload() ? ledgerSelectedAccountCode || null : null,
            ledger_period_start: isLedgerUpload() ? coverageStartDate.iso || null : null,
            ledger_period_end: isLedgerUpload() ? coverageEndDate.iso || null : null,
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
            force_include_rows: isLedgerUpload() ? [] : Array.from(rejectedRowOverrides)
        };
        if (isLedgerUpload() && (ledgerSelectionClientId !== Number(clientId) || !ledgerSelectedAccountCode
            || !ledgerAccountChoices.some(a => a.account_code === ledgerSelectedAccountCode))) {
            const help = document.getElementById('ledgerAccountHelp');
            const message = ledgerAccountChoices.length
                ? 'Choose a posting account from the Trial Balance.'
                : (help.textContent || 'No posting accounts are available.');
            showUploadFieldError(ledgerAccountSelect, help, message);
            return;
        }
        if (isLedgerUpload() && (!payload.ledger_period_start || !payload.ledger_period_end || payload.ledger_period_start > payload.ledger_period_end)) {
            const reversed = payload.ledger_period_start && payload.ledger_period_end && payload.ledger_period_start > payload.ledger_period_end;
            const coverageField = reversed || !payload.ledger_period_start ? uploadCoverageStart : uploadCoverageEnd;
            showUploadFieldError(coverageField, document.getElementById('uploadCoverageHelp'),
                reversed ? 'Start date must be on or before the end date.' : 'Enter both coverage dates.');
            return;
        }
        if (payload.comparison_mode === 'replace_range' && (!payload.coverage_start || !payload.coverage_end || payload.coverage_start > payload.coverage_end)) {
            const reversed = payload.coverage_start && payload.coverage_end && payload.coverage_start > payload.coverage_end;
            const coverageField = reversed || !payload.coverage_start ? uploadCoverageStart : uploadCoverageEnd;
            showUploadFieldError(coverageField, document.getElementById('uploadCoverageHelp'),
                reversed ? 'Start date must be on or before the end date.' : 'Enter both report coverage dates.');
            return;
        }
        if (payload.comparison_mode === 'replace_range' && !payload.confirm_report_coverage) {
            showUploadFieldError(uploadConfirmCoverage, document.getElementById('uploadCoverageHelp'),
                'Confirm that these dates cover the full ERP report period before validation.');
            return;
        }
        if (sourceFootingMappings().length && !confirmedSourceControlRow()) {
            showUploadFieldError(getElement('sourceControlRow'), getElement('sourceControlPreview'),
                'Choose a valid Excel total row and confirm it before validation.');
            return;
        }

        // ---- NEW LOGIC: Validation Flow ----
        // If validation status is already 'valid', proceed with import
        if (validationStatus === 'valid') {
            // Perform the actual import
            await performImport(payload);
            return;
        }
        if (validationStatus === 'ignored') {
            showStatus('ℹ️ Upload stopped. All validated lines are unchanged; nothing will be imported.', 'info');
            return;
        }

        // Discard a response if account, coverage, mapping or source changed in flight.
        const requestGeneration = uploadValidationGeneration;
        // Otherwise, run validation first
        try {
            // Disable button and show spinner
            importBtn.disabled = true;
            importBtn.innerHTML = '<span class="spinner"></span> Validating...';
            showStatus('⏳ Validating data...', 'loading');
            setBadge('⏳ Validating...', 'loading');

            let res = await fetchUpload(API_BASE + '/api/upload/validate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            let result = await res.json();
            if (requestGeneration !== uploadValidationGeneration) {
                showUploadFailure('Upload settings changed during validation. Validate the current selections again.', 'Validation'); return;
            }
            if (!res.ok) {
                resetValidationState();
                throw new Error(uploadFailureMessage(result, 'Validation failed (' + res.status + ')'));
            }
            renderSourceControls(result.source_reconciliation || null);
            if (result.footing_variance_confirmation_required) {
                const accepted = await showFootingVarianceConfirmation(result);
                if (!accepted) {
                    validationStatus = 'idle';
                    validationPlanId = null;
                    importBtn.textContent = 'Validate';
                    importBtn.disabled = false;
                    showStatus('Variance confirmation cancelled. No data was changed.', 'info');
                    setBadge('Variance not confirmed');
                    return;
                }
                if (requestGeneration !== uploadValidationGeneration) {
                    showUploadFailure('Upload settings changed during variance review. Validate the current selections again.', 'Validation'); return;
                }
                footingVarianceConfirmed = true;
                payload.confirm_footing_variance = true;
                importBtn.textContent = 'Revalidating confirmed variance...';
                importBtn.disabled = true;
                showStatus('⏳ Rechecking the confirmed footing variance...', 'loading');
                res = await fetchUpload(API_BASE + '/api/upload/validate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                result = await res.json();
                if (requestGeneration !== uploadValidationGeneration) {
                    showUploadFailure('Upload settings changed during revalidation. Validate the current selections again.', 'Validation'); return;
                }
                if (!res.ok) {
                    resetValidationState();
                    throw new Error(uploadFailureMessage(result, 'Validation failed (' + res.status + ')'));
                }
                renderSourceControls(result.source_reconciliation || null);
            }

            // Rejected-rows exceptions list (Phase 3 of the row-level blank-field-rejection
            // requirement) -- populated regardless of has_errors, since a row can be rejected
            // for a blank Document ID/Amount/Date independently of other rows' validation errors.
            setRejectedRows(result.rejected_rows);
            if (isLedgerUpload() && result.ledger_evidence) {
                const evidence = result.ledger_evidence;
                ledgerRecognisedControlRows = new Set((evidence.control_rows || []).map(row => row.row_number));
                ledgerRecognisedNoteRows = new Map((evidence.continuation_rows || []).map(row => [row.row_number, row]));
                const display = value => value === null || value === undefined ? 'Not supplied' : String(value);
                const note = document.getElementById('ledgerEvidenceSummary');
                note.textContent = `Opening control: ${display(evidence.opening)} · Closing control: ${display(evidence.closing)} · ${(evidence.control_rows || []).length} balance control row(s) recognised; these are not transactions.`;
                if (ledgerRecognisedNoteRows.size) note.textContent += ` ${ledgerRecognisedNoteRows.size} continuation note(s) attached to their preceding transactions; original descriptions and amounts are preserved.`;
                if (evidence.balance_check) {
                    const check = evidence.balance_check;
                    const signed = value => value === null || value === undefined ? 'Unavailable'
                        : String(value).startsWith('-') ? `${String(value).slice(1)} Cr` : `${value} Dr`;
                    const sources = {erp_opening: 'ERP opening', inferred_from_transaction: 'inferred from transactions',
                        inferred_from_closing: 'inferred from closing control', previous_coverage: 'previous continuous coverage', unavailable: 'unavailable'};
                    note.textContent += ` Calculated opening: ${signed(check.opening)} (${sources[check.opening_source] || 'unavailable'}). Calculated closing: ${signed(check.closing)}.`;
                    note.textContent += check.opening === null || check.opening === undefined || check.closing === null || check.closing === undefined
                        ? ' Balance comparison: unavailable.' : ` Balance differences: ${check.difference_count || 0}.`;
                    if (check.continuity?.status === 'difference') note.textContent += ' Previous closing differs from this opening.';
                    if (check.continuity?.status === 'gap') note.textContent += ' Previous coverage has a gap.';
                    if (check.opening_source?.startsWith('inferred') && check.continuity?.status !== 'matched') note.textContent += ' This opening has no independent prior-balance confirmation.';
                    if (check.status === 'partial_or_incomplete' || check.status === 'unordered') note.textContent += ' Complete balance reconciliation is unavailable for this extract.';
                }
                note.hidden = false;
            }

            const totalRows = result.total_rows || 0;
            const newRowsCount = result.new_rows_count || 0;
            const existingRowsCount = result.existing_rows_count || 0;
            lastChangePreview = result.change_detection || null;
            lastUploadSummary = result.upload_summary || null;
            const previewAdded = lastChangePreview ? (lastChangePreview.new_count || 0) : newRowsCount;
            const previewChanged = lastChangePreview ? (lastChangePreview.changed_count || 0) : 0;
            const previewRemoved = lastChangePreview ? (lastChangePreview.missing_count || 0) : 0;
            const previewUnchanged = lastChangePreview ? (lastChangePreview.unchanged_count || 0) : existingRowsCount;
            const duplicateRowCount = result.duplicate_row_count || (lastChangePreview && lastChangePreview.duplicate_row_count) || 0;
            const duplicateSkippedCount = result.duplicate_skipped_count || (lastChangePreview && lastChangePreview.duplicate_skipped_count) || 0;
            const duplicateGroupCount = result.duplicate_group_count || (lastChangePreview && lastChangePreview.duplicate_group_count) || 0;
            const extraMsg = `<span style="color: maroon; font-weight: bold;">${newRowsCount} new row(s) can be uploaded • ${existingRowsCount} already exists in client data</span>`;
            const previewInformational = Number(lastChangePreview?.informational_update_count || 0);
            const changePreviewMsg = `<br><span style="color: #475569; font-weight: 600;">${previewAdded} added · ${previewChanged} changed · ${previewRemoved} removed · ${previewUnchanged} unchanged${previewInformational ? ` · ${previewInformational} rows with unchecked-field refreshes` : ''}</span>`;
            const duplicateSummaryMsg = duplicateRowCount
                ? `<br><span style="color: #92400e; font-weight: 600;">${duplicateRowCount} duplicate row(s) found in ${duplicateGroupCount} group(s); ${duplicateSkippedCount} repeated copy/copies will be skipped.</span>`
                : '';

            if (result.has_errors || result.status === 'validation_failed') {
                // Show validation errors
                showValidationErrors(result);
                // Mark the failing rows in the row selection grid (Phase 4) -- distinct from
                // user-excluded rows, and does not auto-exclude them.
                setValidationFailedRows([...(result.errors || []), ...(result.rejected_rows || []).map(row => ({...row, errors: row.reasons}))]);
                validationStatus = 'invalid';
                validationPlanId = null;
                // Reset button state
                importBtn.innerHTML = 'Validate';
                importBtn.disabled = false;
                importBtn.style.background = '#22c55e'; // Keep green but maybe orange? We'll keep default.
                showStatus('Fix the marked rows, then validate again.', 'error', extraMsg + duplicateSummaryMsg + changePreviewMsg);
                setBadge('Rows need review', 'error');
                return;
            }

                                    // Validation passed – clear any stale failed-row markers from a
            // prior attempt (Phase 4), then show simple success message
            setValidationFailedRows([]);
            // Pre-import Rate mode visibility (Phase D) -- reflects this Validate call's
            // resolved Auto/Forced Imported/Derived decision until the next mapping change.
            lastRateModeInfo = result.rate_mode || null;
            renderRateModeIndicator();
            const canImport = Boolean(result.can_import && result.validation_plan_id);
            // A verified no-op shows source notes inside its result dialog. Import-ready
            // results keep the existing source-review dialog before the user can upload.
            if (canImport || !result.reconciliation) showSourceDiagnostics(result);
            if (typeof result.backup_enabled === 'boolean') {
                ++uploadBackupPolicyGeneration;
                uploadBackupRequired = result.backup_enabled;
                renderUploadBackupPolicy();
            }
            validationPlanId = canImport ? result.validation_plan_id : null;
            comparisonPreviewRequest = canImport
                ? {...payload, validation_plan_id: result.validation_plan_id}
                : null;
            comparisonPreviewStale = false;
            validationStatus = canImport ? 'valid' : 'ignored';
            importBtn.textContent = canImport ? (result.ledger_evidence_refresh ? 'Refresh balance evidence' : '🚀 Import selected changes') : 'No changes to import';
            importBtn.style.background = canImport ? '#16a34a' : '#94a3b8';
            importBtn.style.color = 'white';
            importBtn.disabled = !canImport;
            let statusMsg = `✅ Validation passed! ${totalRows} rows identified. Click "Upload" to import all rows.`;
            if (!canImport && result.duplicate_upload_noop) {
                showStatus('This data is already uploaded. Nothing needs to be uploaded.', 'info', extraMsg + duplicateSummaryMsg + changePreviewMsg);
                setBadge('Same data - upload stopped');
            } else if (canImport) {
                showStatus('✅ Validation passed. Import will process additions, transaction changes, removals, and any unchecked-field refreshes.', 'success', extraMsg + duplicateSummaryMsg + changePreviewMsg);
                setBadge('✅ Changes validated, ready to import');
            } else {
                showStatus('ℹ️ Validation complete. All validated lines are unchanged; no import is available.', 'info', extraMsg + duplicateSummaryMsg + changePreviewMsg);
                setBadge('ℹ️ Upload ignored: no changes');
            }
            if (lastUploadSummary && statusMessage) {
                statusMessage.insertAdjacentHTML(
                    'beforeend',
                    '<div style="font-size:12px; margin-top:4px;"><button id="downloadValidationReportBtn" style="padding:4px 10px; background:#0f766e; color:white; border:none; border-radius:5px; font-weight:600; cursor:pointer; font-size:11px;">Download upload report</button></div>'
                );
                attachValidationReportDownload(
                    lastChangePreview ? lastChangePreview.report : [],
                    lastUploadSummary,
                    result.source_reconciliation
                );
            }
            updatePreviewButtonState();
            // Validation succeeded -- this is the genuine "ready to import" milestone the step
            // strip's final step represents (confirmed with the user: mapping-resolved alone
            // isn't distinct enough, since it fires at the same instant Import becomes enabled).
            updateStepIndicator(4);
            if (!canImport && result.reconciliation) {
                lastIgnoredResult = {...result, status: 'success', ignored: true,
                    imported_count: 0, updated_count: 0, deleted_count: 0};
                showIgnoredResultAction(lastIgnoredResult);
                showImportSummary(lastIgnoredResult);
            }

        } catch (error) {
            resetValidationState();
            if (error?.result?.detail?.code === 'staged_file_missing') fileId = null;
            showUploadFailure('❌ Validation error: ' + error.message, 'Validation', error);
            setBadge('❌ Validation failed', 'error');
            importBtn.innerHTML = 'Validate';
            importBtn.disabled = false;
            importBtn.style.background = '';
            validationStatus = 'invalid';
        }
        });
    });
}
// ---------- PERFORM IMPORT (after validation) ----------
// ---------- PERFORM IMPORT (after validation) ----------
async function performImport(payload) {
    const submittedAccount = ledgerAccountChoices.find(a => a.account_code === payload.ledger_account_code);
    const ledgerPresentation = payload.ledger_account_code
        ? `${submittedAccount?.account_name || 'Ledger account'} (${payload.ledger_account_code}) · ${payload.ledger_period_start} through ${payload.ledger_period_end}` : null;
    try {
        if (!await loadUploadBackupPolicy()) {
            showUploadFailure('Could not check the app backup setting. Retry Import.', 'Import');
            return;
        }
        if (payload.backup_enabled !== uploadBackupRequired) {
            showUploadFailure('The administrator changed the backup setting. Validate again.', 'Validation');
            return;
        }
        if (payload.comparison_mode === 'replace_range' || payload.backup_enabled === false
            || Number(lastChangePreview?.informational_update_count || 0) > 0) {
            const confirmed = await reviewUploadReplacement(payload);
            if (!confirmed) {
                showStatus('Upload cancelled. No data was changed.', 'info');
                return;
            }
        }
        window.LoadingUI.showSpinner(importBtn, 'Importing');
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
        const res = await fetchUpload(API_BASE + '/api/upload/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const result = await res.json();
        if (!res.ok) {
            const serverMessage = uploadFailureMessage(result, 'Import failed (' + res.status + ')');
            if (res.status === 409) {
                resetValidationState();
            }
            throw new Error(serverMessage);
        }

        if (document.getElementById('validationErrorOverlay')?.dataset.stage === 'Import progress') {
            document.getElementById('validationErrorOverlay').remove();
        }
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
                    showImportSummary(result, ledgerPresentation);
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
            setValidationFailedRows([...(result.errors || []), ...(result.rejected_rows || []).map(row => ({...row, errors: row.reasons}))]);
            showStatus('❌ Import validation errors found.', 'error');
            setBadge('❌ Import failed', 'error');
        } else {
            if (!modalRef || modalRef.isResolved) {
                showImportSummary(result, ledgerPresentation);
            }
            setBadge('✅ Import complete');
            validationPlanId = null;
            validationStatus = 'idle';
        }
    } catch (error) {
        if (error.httpStatus === 409) resetValidationState();
        showUploadFailure('❌ Import error: ' + error.message, 'Import', error);
        setBadge(error.outcomeUnknown ? 'Import outcome unconfirmed' : '❌ Import failed', error.outcomeUnknown ? 'warning' : 'error');
    } finally {
        clearInterval(importProgressPollInterval);
        importProgressPollInterval = null;
        hideImportProgressBar();
        window.LoadingUI.hideSpinner(importBtn);
    }
}

function renderPreviewPage() {
    if (comparisonPreviewMode) {
        // Comparison pages come from the server's validated plan; fetch only the
        // requested page rather than returning all compared rows in validation.
        void fetchChangePreviewPage(previewCurrentPage);
        return;
    }
    if (databasePreviewMode) {
        void fetchDatabasePreviewPage(document.getElementById('previewDataView').value, previewCurrentPage);
        return;
    }
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
            td.textContent = formatUploadPreviewCell(cell, headers[idx]);
            tr.appendChild(td);
        });
        previewBody.appendChild(tr);
    });

    const footingMappings = sourceFootingMappings();
    const footingTotals = sourceControlResult?.approved_detail?.footing_totals;
    if (footingMappings.length && footingTotals) {
        const totalRow = document.createElement('tr');
        totalRow.className = 'preview-footing-total';
        const label = document.createElement('th');
        label.scope = 'row'; label.textContent = 'Total'; label.style.fontWeight = '700';
        totalRow.appendChild(label);
        headers.forEach(header => {
            const cell = document.createElement('td');
            const mapping = footingMappings.find(item => item.excel_column === header);
            if (mapping) {
                cell.textContent = formatSourceControlAmount(footingTotals[mapping.column_name] ?? '0');
                cell.style.fontWeight = '700';
            }
            totalRow.appendChild(cell);
        });
        previewBody.appendChild(totalRow);
    }

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
        const workbook = XLSX.read(workbookData, { type: 'array', cellNF: true });
        uploadWorkbookDate1904 = !!workbook.Workbook?.WBProps?.date1904;
        const sheetName = currentSheet || workbook.SheetNames[0];
        const sheet = workbook.Sheets[sheetName];
        // defval: '' -- same fix as showPreview(): avoids holes in the row array so
        // renderRowExclusionPage()'s row.forEach() doesn't skip blank cells and shift
        // later columns left.
        const jsonData = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: '' });

        rowExclusionHeaders = jsonData[0] || [];
        rowExclusionAllRows = jsonData.slice(1);
        rowExclusionNativeDateColumns = new Set();
        // Keep source types for every column so a later Date-column remap can
        // detect native dates without inheriting the old mapping's separator.
        const dateFormats = new Map();
        for (const address in sheet) {
            const cell = sheet[address];
            if (cell?.t !== 'n' || !cell.z) continue;
            if (!dateFormats.has(cell.z)) dateFormats.set(cell.z, XLSX.SSF.is_date(cell.z));
            if (dateFormats.get(cell.z)) rowExclusionNativeDateColumns.add(XLSX.utils.decode_cell(address).c);
        }
        excludedRowNumbers = new Set();
        validationFailedRows = new Map(); // new sheet/mapping -- any prior validation result is stale
        rejectedRowsData = new Map(); // new sheet/mapping -- any prior rejected-rows result is stale
        rejectedRowOverrides = new Set();
        renderRejectedRowsList();
        rowExclusionCurrentPage = 1;
        resetSourceControlChoice(true);

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
        showUploadFailure('Row selection could not be displayed: ' + error.message, 'Row selection', error);
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
    const configured = currentMappings.filter(mapping => mapping.reconciliation_role === 'document_id');
    // Ambiguous configuration is rejected by the server; never guess a key in the grid.
    if (configured.length > 1) return -1;
    const keyMapping = configured[0] || currentMappings[0];
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
    const control = confirmedSourceControlRow();
    const total = rowExclusionAllRows.length - (control ? 1 : 0);
    const included = total - [...excludedRowNumbers].filter(number => number !== control).length;
    counterEl.textContent = isLedgerUpload() ? `${included} of ${total} source rows selected; balance controls are recorded separately`
        : `${included} of ${total} rows selected${control ? ' · 1 control total excluded' : ''}`;
}

// ---------- SELECT ALL / DESELECT ALL (Phase 3) ----------
// Operate on the full excludedRowNumbers Set, so they apply across all pages, not
// just the currently visible one; a single re-render refreshes the visible page + counter.
function rowExclusionSelectAll() {
    excludedRowNumbers.clear();
    resetValidationState();
    renderRowExclusionPage();
    saveUploadSessionState();
}

function rowExclusionDeselectAll() {
    const total = rowExclusionAllRows.length;
    excludedRowNumbers = new Set();
    for (let i = 0; i < total; i++) {
        if (i + 2 !== confirmedSourceControlRow()) excludedRowNumbers.add(i + 2);
    }
    resetValidationState();
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
    resetValidationState();
    clearCoverageConfirmation();
    resetSourceControlChoice(true);
    refreshDateFormatPreview();
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
            resetValidationState();
        });
        const fieldNames = reasons.map(r => r.column).join(', ');
        const labelText = document.createElement('span');
        labelText.textContent = `Import anyway (with blank in ${fieldNames})`;
        label.appendChild(checkbox);
        label.appendChild(labelText);
        if (!isLedgerUpload()) item.appendChild(label);

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
    const sourceControl = confirmedSourceControlRow();
    const totalForMaster = rowExclusionAllRows.length - (sourceControl ? 1 : 0);
    const excludedCount = [...excludedRowNumbers].filter(number => number !== sourceControl).length;
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
        tr.dataset.excelRowNumber = excelRowNumber;
        const isSourceControl = excelRowNumber === sourceControl;
        if (isSourceControl) tr.classList.add('source-control-row');
        if (!isSourceControl && excludedRowNumbers.has(excelRowNumber)) tr.classList.add('row-excluded');
        const ledgerControl = isLedgerUpload() && ledgerRecognisedControlRows.has(excelRowNumber);
        const ledgerNote = isLedgerUpload() ? ledgerRecognisedNoteRows.get(excelRowNumber) : null;
        const flagged = !isSourceControl && !ledgerControl && !ledgerNote && isRowExclusionFlagged(row, keyColIndex);
        if (flagged) tr.classList.add('row-flagged');
        const failed = validationFailedRows.has(excelRowNumber);
        if (failed) tr.classList.add('row-validation-failed');

        const tdCheck = document.createElement('td');
        tdCheck.className = 'row-exclusion-check-col';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = !isSourceControl && !excludedRowNumbers.has(excelRowNumber);
        checkbox.disabled = isSourceControl;
        if (isSourceControl) checkbox.title = 'Excel control total — not a transaction';
        checkbox.addEventListener('change', function() {
            if (this.checked) {
                excludedRowNumbers.delete(excelRowNumber);
                tr.classList.remove('row-excluded');
            } else {
                excludedRowNumbers.add(excelRowNumber);
                tr.classList.add('row-excluded');
            }
            resetValidationState();
            updateRowExclusionCounter();
            saveUploadSessionState();
        });
        tdCheck.appendChild(checkbox);
        tr.appendChild(tdCheck);

        const tdRowNum = document.createElement('td');
        tdRowNum.className = 'row-exclusion-rownum-col';
        // Prefixes concatenate rather than overwrite -- a row can be both auto-flagged and
        // validation-failed at once, and both signals should stay visible (e.g. "⚠️❌ 5").
        let prefix = (ledgerControl || ledgerNote ? '•' : '') + (flagged ? '⚠️' : '') + (failed ? '❌' : '');
        tdRowNum.textContent = (prefix ? prefix + ' ' : '') + excelRowNumber + (isSourceControl ? ' · Total' : '');
        const titleParts = [];
        if (ledgerControl) titleParts.push('Ledger balance control: recorded separately, not a transaction');
        if (ledgerNote) titleParts.push(`Ledger continuation note: attached to Excel row ${ledgerNote.parent_row_number}, voucher ${ledgerNote.voucher}; not a separate transaction`);
        if (flagged) titleParts.push('Possible junk row: blank or "total"/"subtotal" value in the key column');
        if (failed) {
            const rowErrors = validationFailedRows.get(excelRowNumber) || [];
            const msg = rowErrors.map(e => `${e.column || 'Row-level'}: ${e.message}`).join('; ');
            titleParts.push('Failed validation' + (msg ? ': ' + msg : ''));
        }
        if (titleParts.length) tdRowNum.title = titleParts.join(' | ');
        tr.appendChild(tdRowNum);

        row.forEach((cell, idx) => {
            const td = document.createElement('td');
            td.textContent = formatUploadPreviewCell(cell, headers[idx]);
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

function preparePreviewWorkbookRows() {
    if (!workbookData) throw new Error('No file loaded to preview.');
    const workbook = XLSX.read(workbookData, { type: 'array' });
    uploadWorkbookDate1904 = !!workbook.Workbook?.WBProps?.date1904;
    const sheetName = currentSheet || workbook.SheetNames[0];
    const sheet = workbook.Sheets[sheetName];
    // Keep every cell position, including blanks, so preview values stay under their Excel headers.
    const jsonData = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: '' });
    previewHeaders = jsonData[0] || [];
    const rawDataRows = jsonData.slice(1);
    const controlRow = confirmedSourceControlRow();
    previewAllRows = rawDataRows.filter((_, index) => index + 2 !== controlRow && !excludedRowNumbers.has(index + 2));
    previewHiddenCount = rawDataRows.length - previewAllRows.length;
    previewCurrentPage = 1;
    const notes = ['Incoming transaction rows from the selected Excel sheet.'];
    if (controlRow) notes.push(`Footing control row ${controlRow} is shown separately and is not a transaction.`);
    if (excludedRowNumbers.size) notes.push(`${excludedRowNumbers.size} unchecked row(s) are not shown.`);
    const note = document.getElementById('previewSourceNote');
    note.textContent = notes.join(' ');
    note.hidden = !notes.length;
}

function showPreview() {
    const previewSheet = document.getElementById('previewSheet');
    const mainUploadPage = document.getElementById('mainUploadPage');

    if (!workbookData) {
        showUploadFailure('⚠️ No file loaded to preview.');
        return;
    }

    try {
        comparisonPreviewMode = false;
        databasePreviewMode = false;
        databasePreviewContext = null;
        comparisonPreviewAction = 'all';
        const dataView = document.getElementById('previewDataView');
        if (dataView) dataView.value = 'uploaded';
        preparePreviewWorkbookRows();

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
        showUploadFailure('Error previewing file: ' + error.message, 'Workbook preview', error);
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
        if (comparisonPreviewMode || databasePreviewMode) {
            comparisonPreviewMode = false;
            databasePreviewMode = false;
            document.getElementById('closePreviewBtn').textContent = '← Back to Upload';
            const returnToReview = comparisonPreviewReturn;
            comparisonPreviewReturn = null;
            if (comparisonPreviewStale) {
                document.getElementById('uploadReplacementStalePlan').hidden = false;
                document.getElementById('uploadReplacementConfirm').disabled = true;
            }
            returnToReview?.();
            return;
        }
        // Re-enable preview button after closing (state already managed)
        updatePreviewButtonState();
    });
}

// ---------- PREVIEW BUTTON ----------
const previewBtn = document.getElementById('previewBtn');
if (previewBtn) {
    previewBtn.addEventListener('click', function() {
        if (!workbookData) {
            showUploadFailure('⚠️ Please upload a file first.');
            return;
        }
        if (!currentSheet) {
            showUploadFailure('⚠️ Please select a sheet.');
            return;
        }
        if (!selectedTemplateId) {
            showUploadFailure('⚠️ Please select a template.');
            return;
        }
        if (currentMappings.length === 0) {
            showUploadFailure('⚠️ Please wait for auto-mapping to complete.');
            return;
        }
        // --- NEW: Check validation ---
        if (validationStatus !== 'valid') {
            showUploadFailure('⚠️ Please validate the data first (click "Validate" button).');
            return;
        }
        // All conditions satisfied
        showPreview();
    });
}

const previewDataView = document.getElementById('previewDataView');
if (previewDataView) previewDataView.addEventListener('change', () => setPreviewDataView(previewDataView.value));

// ---------- INIT ----------
let uploadBackupPolicyGeneration = 0;
let uploadBackupRequired = true;
let uploadArchiveFolderUrl = null;
let uploadDriveConnectionReady = false;
function renderUploadBackupPolicy() {
    document.getElementById('uploadBackupHelp').textContent = uploadBackupRequired
        ? 'Backups are ON. A verified Google Drive copy is required before replacement.'
        : 'Backups are OFF for development. No recovery backup will be created.';
}

async function loadUploadBackupPolicy() {
    const generation = ++uploadBackupPolicyGeneration;
    const help = document.getElementById('uploadBackupHelp');
    let allowed = false;
    let loaded = false;
    try {
        const response = await fetch(API_BASE + '/api/upload/backup-policy');
        if (!response.ok) throw new Error('Backup setting unavailable');
        const policy = await response.json();
        if (typeof policy.allow_without_backup !== 'boolean') throw new Error('Backup setting unavailable');
        allowed = policy.allow_without_backup === true;
        try {
            const folderUrl = new URL(policy.archive_folder_url || '');
            uploadArchiveFolderUrl = folderUrl.protocol === 'https:' && folderUrl.hostname === 'drive.google.com'
                ? folderUrl.href : null;
        } catch (_) { uploadArchiveFolderUrl = null; }
        uploadDriveConnectionReady = policy.drive_connection_ready === true;
        loaded = true;
    } catch (_) { /* Keep backups required if policy cannot be loaded. */ }
    if (generation !== uploadBackupPolicyGeneration) return false;
    if (uploadBackupRequired !== !allowed) resetValidationState();
    uploadBackupRequired = !allowed;
    renderUploadBackupPolicy();
    if (!loaded) help.textContent = 'Could not check the app backup setting. Retry before importing.';
    return loaded;
}

window.addEventListener('focus', () => { void loadUploadBackupPolicy(); });

window.onload = async function() {
    if (window.clientContextReady) await window.clientContextReady;
    await loadUploadBackupPolicy();
    // Await so the Template dropdown's <option>s exist before restoreUploadSessionState()
    // (session-restore) tries to set the dropdown's value.
    await loadTemplates();
    updateUploadCoverageVisibility();
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
    if (!selectedTemplateId && new URLSearchParams(window.location.search).get('feature') === 'account-ledger') {
        const ledgers = uploadTemplateCatalog.filter(t => t.data_purpose === 'account_ledger');
        if (ledgers.length === 1) { templateDropdown.value = String(ledgers[0].id); await selectTemplate(ledgers[0].id); }
        else showStatus(ledgers.length ? 'Select an Account Ledger template to begin.' : 'Prepare an Account Ledger template in Manage Templates, then return here.', 'info');
    }

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
            if (comparisonPreviewMode) void fetchChangePreviewPage(1);
            else renderPreviewPage();
        });
    }
    const previewFirstPageBtn = document.getElementById('previewFirstPage');
    if (previewFirstPageBtn) {
        previewFirstPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            if (previewCurrentPage > 1) {
                previewCurrentPage = 1;
                if (comparisonPreviewMode) void fetchChangePreviewPage(1); else renderPreviewPage();
            }
        });
    }

    const previewPrevPageBtn = document.getElementById('previewPrevPage');
    if (previewPrevPageBtn) {
        previewPrevPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            if (previewCurrentPage > 1) {
                previewCurrentPage--;
                if (comparisonPreviewMode) void fetchChangePreviewPage(previewCurrentPage); else renderPreviewPage();
            }
        });
    }

    const previewNextPageBtn = document.getElementById('previewNextPage');
    if (previewNextPageBtn) {
        previewNextPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            const totalPages = comparisonPreviewMode ? comparisonPreviewPageCount : (Math.ceil(previewTotalRows / previewPageSize) || 1);
            if (previewCurrentPage < totalPages) {
                previewCurrentPage++;
                if (comparisonPreviewMode) void fetchChangePreviewPage(previewCurrentPage); else renderPreviewPage();
            }
        });
    }

    const previewLastPageBtn = document.getElementById('previewLastPage');
    if (previewLastPageBtn) {
        previewLastPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            const totalPages = comparisonPreviewMode ? comparisonPreviewPageCount : (Math.ceil(previewTotalRows / previewPageSize) || 1);
            if (previewCurrentPage < totalPages) {
                previewCurrentPage = totalPages;
                if (comparisonPreviewMode) void fetchChangePreviewPage(previewCurrentPage); else renderPreviewPage();
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
