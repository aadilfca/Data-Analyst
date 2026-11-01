// ============================================================
// Trial Balance module: map source fields, validate the full workbook without
// saving, then persist the reconciled Trial Balance with code/classification edits.
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
let availableUploadsRequestToken = 0;

// Which saved upload is currently active for the Available Trial Balances view.
let currentUploadId = null;
// Draft account codes never alter the saved hierarchy or assignments until one save succeeds.
const tbPendingCodeCorrections = new Map();
let tbCodeSaveRunning = false;
let tbImportCodeChangesPending = false;
let tbCodeDecisionPending = null;
let tbCodeGuardResuming = false;


// ---------- PERIOD TRACKING + OVERWRITE CONFLICT STATE (Section 2, Phase 6) ----------
// Caches the outcome of the last check-period-conflict call so the exact same period
// pair isn't re-checked/re-prompted twice in a row (e.g. once on file selection, again
// on the actual Upload & Classify click, when nothing changed in between). Reset to
// null whenever either period date changes, so a stale confirmation from a different
// period pair can never be reused.
let lastConflictCheckPeriod = null; // { start, end } the cached result below applies to
let lastConflictCheckResult = null; // 'no_conflict' | 'confirmed' | 'declined' | 'error'

// ---------- COLUMN MAPPING STATE (standard 8-field Trial Balance mapping) ----------
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

// First 10 data rows for the compact Client Excel mirror plus all rows for full validation.
const PREVIEW_ROW_COUNT = 10;
let filePreviewRows = [];
let fileAllRows = [];
let accountCodeCorrections = {};
let accountClassificationOverrides = {};
let autoClassificationByRow = {};
let includedSubtotalRows = new Set();
let showExcludedSubtotalRows = false;
let validationResult = null;
let validationIssueSnapshot = { errors: [], warnings: [] };
let validationDirty = true;
let validationRequestToken = 0;
let validationRevision = 0;
let criticalIssueRows = [];
let criticalIssueIndex = -1;
let criticalIssueVisitedRows = new Set();
let criticalIssueReviewComplete = false;
let criticalIssueValidationRunning = false;
let criticalIssueHighlightTimer = null;

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

// Keep every Trial Balance API request on the shared GET-retry/mutation path.
function tbApiRequest(url, options) {
    const requestOptions = options || {};
    const method = String(requestOptions.method || 'GET').toUpperCase();
    if (!window.ApiUtils) throw new Error('The shared API request helper is unavailable.');
    return method === 'GET'
        ? window.ApiUtils.apiFetch(url, requestOptions)
        : window.ApiUtils.apiFetchMutate(url, requestOptions);
}

const clientSelectionWarning = getElement('clientSelectionWarning');
const uploadToolbar = getElement('uploadToolbar');
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
const mappingSection = getElement('mappingSection');
const mappingValidationMessage = getElement('mappingValidationMessage');
const mappingValidationText = getElement('mappingValidationText');
const mappingTableBody = getElement('mappingTableBody');
const tbTotalExcelRow = getElement('tbTotalExcelRow');
const tbNoTotalRow = getElement('tbNoTotalRow');
const saveTrialBalanceMappingBtn = getElement('saveTrialBalanceMappingBtn');
const mappingPreviewHead = getElement('mappingPreviewHead');
const mappingPreviewBody = getElement('mappingPreviewBody');
const validateTrialBalanceBtn = getElement('validateTrialBalanceBtn');
const tbValidationSheet = getElement('tbValidationSheet');
const tbValidationBackBtn = getElement('tbValidationBackBtn');
const tbValidationRunBtn = getElement('tbValidationRunBtn');
const tbValidationHeader = getElement('tbValidationHeader');
const tbValidationStatus = getElement('tbValidationStatus');
const tbValidationFileInfo = getElement('tbValidationFileInfo');
const tbValidationHead = getElement('tbValidationHead');
const tbValidationBody = getElement('tbValidationBody');
const tbValidationTableWrap = getElement('tbValidationTableWrap');
const tbValidationStickyHead = getElement('tbValidationStickyHead');
const tbCriticalIssueNavigator = getElement('tbCriticalIssueNavigator');
const tbCriticalIssueCount = getElement('tbCriticalIssueCount');
const tbSubtotalToolbar = getElement('tbSubtotalToolbar');
const tbSubtotalSummary = getElement('tbSubtotalSummary');
const tbSubtotalToggleBtn = getElement('tbSubtotalToggleBtn');
const downloadStandardTemplateBtn = getElement('downloadStandardTemplateBtn');

// Available Trial Balances (Section 1, Phase 2) elements.
const uploadViewContainer = getElement('uploadViewContainer');
const availableTrialBalancesBtn = getElement('availableTrialBalancesBtn');
const availableTrialBalancesSection = getElement('availableTrialBalancesSection');
const closeAvailableTrialBalancesBtn = getElement('closeAvailableTrialBalancesBtn');
const tbPageContainer = getElement('tbPageContainer');
const availableUploadsDropdown = getElement('availableUploadsDropdown');
const availableUploadsEmptyHint = getElement('availableUploadsEmptyHint');
const availableAccountsTableWrapper = getElement('availableAccountsTableWrapper');
const availableAccountsTableBody = getElement('availableAccountsTableBody');
const tbSavedStickyHead = getElement('tbSavedStickyHead');
const tbSavedCategorySearch = getElement('tbSavedCategorySearch');
const tbSavedCategoryTrigger = getElement('tbSavedCategoryTrigger');
const tbSavedCategoryOptions = getElement('tbSavedCategoryOptions');
const tbSavedNoteSearch = getElement('tbSavedNoteSearch');
const tbSavedNoteTrigger = getElement('tbSavedNoteTrigger');
const tbSavedNoteOptions = getElement('tbSavedNoteOptions');
const tbSavedCategoryNoMatches = getElement('tbSavedCategoryNoMatches');
let availableAccountCategoryByCode = new Map();
let availableAccountCategoryLookupReady = false;
let availableAccountNoteByCode = new Map();
let availableAccountNoteLookupReady = false;
let availableAccountLookupMessage = '';
let availableAccountsLoadToken = 0;
let availableCategoryUploadId = null;
const tbAssignmentWorkflow = getElement('tbAssignmentWorkflow');
const tbAssignmentSetup = getElement('tbAssignmentSetup');
const tbAssignmentSetupMenu = getElement('tbAssignmentSetupMenu');
let tbWorkflowMode = window.AccountAssignments ? AccountAssignments.mode(new URLSearchParams(location.search).get('workflow') || new URLSearchParams(location.search).get('acc_tab')) : 'financial';
let tbWorkflowLoadToken = 0;
let tbWorkflowContext = '';
const tbWorkflowStates = new Map();
const tbWorkflowCache = new Map();
const tbSavedCategorySelections = new Set();
const tbSavedNoteSelections = new Set();
let tbWorkflowReturnRestored = false;
let tbFsWorkspace = null;
let tbFsAccounts = [];
let tbFsMappingReady = false;
let tbFsLeaves = new Map();
let tbFsIsAdmin = false;
let tbFsBusyCount = 0;
let tbFsModalOpen = false;
let tbFsLockState = null;
let tbFsRenderedCategories = null;
let tbFsRenderedNotes = null;
let tbFsMetadataRevision = 0;
let tbCashflowWorkspace = null;
let tbCashflowAccounts = [];
let tbCashflowCategories = [];
let tbCashflowLeaves = new Map();
let tbVerificationWorkspace = null;
let tbVerificationAccounts = [];
let tbVerificationLeaves = new Map();
function savedAssignmentColumnCount() { return tbWorkflowMode === 'financial' ? 2 : 1; }
async function tbReadAssignmentJson(url) {
    const response = await tbApiRequest(API_BASE + url);
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not load account assignments');
    return result;
}
if (availableAccountsTableWrapper && tbSavedStickyHead) {
    const savedTable = availableAccountsTableWrapper.querySelector('table');
    const heading = savedTable.cloneNode(false);
    heading.appendChild(savedTable.querySelector('thead').cloneNode(true));
    tbSavedStickyHead.appendChild(heading);
    availableAccountsTableWrapper.addEventListener('scroll', function () {
        heading.style.transform = `translateX(-${availableAccountsTableWrapper.scrollLeft}px)`;
    }, { passive: true });
}

// Keep the heading aligned while its table scrolls horizontally at any screen size.
function syncTrialBalanceStickyHeader() {
    if (!tbValidationTableWrap || !tbValidationStickyHead) return;
    const stickyTable = tbValidationStickyHead.querySelector('.tb-validation-table');
    if (stickyTable) stickyTable.style.transform = `translateX(-${tbValidationTableWrap.scrollLeft}px)`;
    updateTrialBalanceUniversalScrollbar();
}

function bindTrialBalanceFloatingScrollbar(wrapper, isActive, label, marker) {
    if (!wrapper || wrapper.dataset.floatingScrollBound === 'true') return;
    const scrollbar = document.createElement('div');
    scrollbar.className = 'tb-validation-universal-scrollbar ' + marker;
    scrollbar.setAttribute('role', 'scrollbar');
    scrollbar.setAttribute('tabindex', '0');
    scrollbar.setAttribute('aria-orientation', 'horizontal');
    scrollbar.setAttribute('aria-label', label);
    scrollbar.setAttribute('aria-hidden', 'true');
    const track = document.createElement('div');
    track.className = 'tb-validation-universal-scrollbar-track';
    const thumb = document.createElement('div');
    thumb.className = 'tb-validation-universal-scrollbar-thumb';
    track.appendChild(thumb);
    scrollbar.appendChild(track);
    document.body.appendChild(scrollbar);

    const update = function () {
        const table = wrapper.querySelector('table');
        const wrapperRect = wrapper.getBoundingClientRect();
        const activeView = isActive();
        const visible = activeView && wrapperRect.bottom > 72.5 && wrapperRect.top < window.innerHeight;
        const overflowing = table && wrapper.scrollWidth > wrapper.clientWidth + 1;
        const active = Boolean(visible && overflowing);
        scrollbar.hidden = !active;
        scrollbar.setAttribute('aria-hidden', active ? 'false' : 'true');
        if (!active) return;
        const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
        scrollbar.style.left = Math.max(12, Math.round(wrapperRect.left)) + 'px';
        scrollbar.style.right = Math.max(12, Math.round(viewportWidth - wrapperRect.right)) + 'px';
        const trackWidth = track.clientWidth;
        const maxScroll = Math.max(0, wrapper.scrollWidth - wrapper.clientWidth);
        const thumbWidth = Math.min(trackWidth, Math.max(48, Math.round(trackWidth * wrapper.clientWidth / wrapper.scrollWidth)));
        const travel = Math.max(0, trackWidth - thumbWidth);
        thumb.style.width = thumbWidth + 'px';
        thumb.style.transform = 'translateX(' + Math.round(travel * (maxScroll ? wrapper.scrollLeft / maxScroll : 0)) + 'px)';
        scrollbar.setAttribute('aria-valuemin', '0');
        scrollbar.setAttribute('aria-valuemax', String(maxScroll));
        scrollbar.setAttribute('aria-valuenow', String(Math.round(wrapper.scrollLeft)));
    };
    const scrollToTrackPoint = function (clientX) {
        const trackRect = track.getBoundingClientRect();
        const travel = Math.max(0, track.clientWidth - thumb.getBoundingClientRect().width);
        const maxScroll = Math.max(0, wrapper.scrollWidth - wrapper.clientWidth);
        if (!travel || !maxScroll) return;
        const offset = clientX - trackRect.left - thumb.getBoundingClientRect().width / 2;
        wrapper.scrollLeft = Math.max(0, Math.min(maxScroll, offset / travel * maxScroll));
    };
    let dragState = null;
    thumb.addEventListener('pointerdown', function (event) {
        dragState = {pointerId:event.pointerId, startX:event.clientX, startScroll:wrapper.scrollLeft};
        thumb.classList.add('is-dragging');
        thumb.setPointerCapture(event.pointerId);
        event.preventDefault();
    });
    thumb.addEventListener('pointermove', function (event) {
        if (!dragState || dragState.pointerId !== event.pointerId) return;
        const travel = Math.max(0, track.clientWidth - thumb.getBoundingClientRect().width);
        const maxScroll = Math.max(0, wrapper.scrollWidth - wrapper.clientWidth);
        if (travel && maxScroll) wrapper.scrollLeft = Math.max(0, Math.min(maxScroll,
            dragState.startScroll + (event.clientX - dragState.startX) * maxScroll / travel));
    });
    const endDrag = function (event) {
        if (!dragState || dragState.pointerId !== event.pointerId) return;
        dragState = null;
        thumb.classList.remove('is-dragging');
    };
    thumb.addEventListener('pointerup', endDrag);
    thumb.addEventListener('pointercancel', endDrag);
    track.addEventListener('pointerdown', function (event) { if (event.target !== thumb) scrollToTrackPoint(event.clientX); });
    scrollbar.addEventListener('keydown', function (event) {
        const step = Math.max(48, Math.round(wrapper.clientWidth * .15));
        let next = null;
        if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = wrapper.scrollWidth - wrapper.clientWidth;
        else if (event.key === 'ArrowLeft' || event.key === 'PageUp') next = wrapper.scrollLeft - (event.key === 'PageUp' ? wrapper.clientWidth : step);
        else if (event.key === 'ArrowRight' || event.key === 'PageDown') next = wrapper.scrollLeft + (event.key === 'PageDown' ? wrapper.clientWidth : step);
        if (next === null) return;
        event.preventDefault();
        wrapper.scrollLeft = Math.max(0, Math.min(wrapper.scrollWidth - wrapper.clientWidth, next));
    });
    wrapper.addEventListener('scroll', update, {passive:true});
    window.addEventListener('resize', update, {passive:true});
    window.addEventListener('scroll', update, {passive:true});
    wrapper.dataset.floatingScrollBound = 'true';
    wrapper._updateFloatingScrollbar = update;
    update();
}

function updateTrialBalanceUniversalScrollbar() {
    if (tbValidationTableWrap && typeof tbValidationTableWrap._updateFloatingScrollbar === 'function') {
        tbValidationTableWrap._updateFloatingScrollbar();
    }
}

if (tbValidationTableWrap) {
    bindTrialBalanceFloatingScrollbar(tbValidationTableWrap, () => tbValidationSheet && !tbValidationSheet.hidden && tbValidationSheet.classList.contains('active'), 'Trial Balance validation horizontal scroll', 'tb-validation-floating-scroll');
    tbValidationTableWrap.addEventListener('scroll', syncTrialBalanceStickyHeader, { passive: true });
}
if (availableAccountsTableWrapper) {
    bindTrialBalanceFloatingScrollbar(availableAccountsTableWrapper, () => availableTrialBalancesSection && availableTrialBalancesSection.style.display === 'block' && availableAccountsTableWrapper.style.display !== 'none', 'Saved Trial Balance horizontal scroll', 'tb-saved-floating-scroll');
}
const deleteUploadBtn = getElement('deleteUploadBtn');

// Mutation keys prevent duplicate writes for the same Trial Balance resource while leaving
// unrelated controls usable. The shared cursor token is deliberately owned by each complete
// action, including any confirmation check and post-write refresh it triggers.
const trialBalanceActionsInFlight = new Set();

function startTrialBalanceLoading(target) {
    if (!window.LoadingUI || typeof window.LoadingUI.startGlobalLoading !== 'function') return null;
    return window.LoadingUI.startGlobalLoading(target);
}

function stopTrialBalanceLoading(token) {
    if (token && window.LoadingUI && typeof window.LoadingUI.stopGlobalLoading === 'function') {
        window.LoadingUI.stopGlobalLoading(token);
    }
}

function showTrialBalanceActionSpinner(target, message) {
    if (!target || !window.LoadingUI || typeof window.LoadingUI.showSpinner !== 'function') return false;
    window.LoadingUI.showSpinner(target, message, { escalate: false });
    return true;
}

function hideTrialBalanceActionSpinner(target) {
    if (target && window.LoadingUI && typeof window.LoadingUI.hideSpinner === 'function') {
        window.LoadingUI.hideSpinner(target);
    }
}

function waitForTrialBalancePaint() {
    return new Promise(function (resolve) {
        requestAnimationFrame(function () {
            requestAnimationFrame(resolve);
        });
    });
}

// ---------- STATUS BAR ----------
// Same show/hide + color-coded classes convention as upload.js's showStatus().
function showStatus(msg, type = 'info') {
    const savedStatus = getElement('tbSavedStatus');
    if (savedStatus && availableTrialBalancesSection && availableTrialBalancesSection.style.display === 'block') {
        savedStatus.textContent = msg;
        savedStatus.className = 'status-bar ' + type;
        savedStatus.style.display = 'flex';
        return;
    }
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
// Enables validation after a client, file, period and complete mapping exist.
// Save & Classify additionally requires a current, passing validation result. Client switching on this
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
    const hasTotalChoice = checkTotalRowChoice();

    if (validateTrialBalanceBtn) {
        validateTrialBalanceBtn.disabled = !(hasClient && hasFile && hasPeriod && hasMapping && hasTotalChoice);
    }
    if (uploadTrialBalanceBtn) {
        uploadTrialBalanceBtn.disabled = !(hasClient && hasPeriod && validationResult && validationResult.valid && !validationDirty);
    }
    if (saveTrialBalanceMappingBtn) {
        saveTrialBalanceMappingBtn.disabled = !(hasClient && hasFile && hasMapping && hasTotalChoice)
            || trialBalanceActionsInFlight.has('saved-column-mapping');
    }
}

function setValidationStatus(message, type) {
    if (!tbValidationStatus) return;
    tbValidationStatus.textContent = message;
    tbValidationStatus.className = 'status-bar ' + (type || 'info') + ' tb-validation-status';
}

function invalidateTrialBalanceValidation(message, preserveCriticalQueue) {
    validationRevision += 1;
    validationDirty = true;
    validationResult = null;
    if (preserveCriticalQueue) {
        criticalIssueReviewComplete = false;
    } else {
        clearCriticalIssueQueue();
        validationIssueSnapshot = { errors: [], warnings: [] };
    }
    updateCriticalIssueNavigator();
    updateClientDependentUI();
    if (tbValidationSheet && tbValidationSheet.classList.contains('active')) {
        setValidationStatus(message || 'Changes detected. Revalidate before saving.', 'info');
    }
}

function buildTrialBalanceFormData() {
    const formData = new FormData();
    formData.append('file', selectedFile);
    TB_MAPPING_FIELDS.forEach(function (field) {
        formData.append(field.role, tbColumnMapping[field.role]);
    });
    formData.append('account_code_overrides', JSON.stringify(accountCodeCorrections));
    formData.append('account_classification_overrides', JSON.stringify(accountClassificationOverrides));
    formData.append('excluded_excel_rows', JSON.stringify(getExcludedSubtotalRowNumbers()));
    appendTotalRowChoice(formData);
    return formData;
}

async function openTrialBalanceValidation() {
    if (!tbPageContainer || !uploadViewContainer || !tbValidationSheet) return;
    invalidateTrialBalanceValidation();

    const transitionToken = startTrialBalanceLoading(validateTrialBalanceBtn);
    showTrialBalanceActionSpinner(validateTrialBalanceBtn, 'Opening validation…');
    await waitForTrialBalancePaint();
    hideTrialBalanceActionSpinner(validateTrialBalanceBtn);
    stopTrialBalanceLoading(transitionToken);

    tbPageContainer.classList.add('tb-validation-active');
    uploadViewContainer.hidden = true;
    tbValidationSheet.hidden = false;
    tbValidationSheet.classList.add('active');
    tbValidationSheet.setAttribute('aria-hidden', 'false');
    if (tbValidationFileInfo) tbValidationFileInfo.textContent = selectedFile ? selectedFile.name : '';
    window.scrollTo(0, 0);

    setValidationStatus('Preparing the full Trial Balance for review…', 'loading');
    const validationToken = startTrialBalanceLoading(tbValidationRunBtn);
    const spinnerShown = showTrialBalanceActionSpinner(tbValidationRunBtn, 'Preparing validation…');
    await waitForTrialBalancePaint();
    renderTrialBalanceValidationGrid();
    await runTrialBalanceValidation({ loadingToken: validationToken, spinnerShown: spinnerShown });
}

function closeTrialBalanceValidation() {
    if (tbPageContainer) tbPageContainer.classList.remove('tb-validation-active');
    if (uploadViewContainer) uploadViewContainer.hidden = false;
    if (tbValidationSheet) {
        tbValidationSheet.classList.remove('active');
        tbValidationSheet.hidden = true;
        tbValidationSheet.setAttribute('aria-hidden', 'true');
    }
    window.scrollTo(0, 0);
}

if (validateTrialBalanceBtn) {
    validateTrialBalanceBtn.addEventListener('click', openTrialBalanceValidation);
}
if (tbValidationBackBtn) tbValidationBackBtn.addEventListener('click', closeTrialBalanceValidation);
if (tbValidationRunBtn) tbValidationRunBtn.addEventListener('click', function () {
    runTrialBalanceValidation();
});
if (uploadTrialBalanceBtn) uploadTrialBalanceBtn.addEventListener('click', uploadTrialBalance);

async function runTrialBalanceValidation(options) {
    const opts = options || {};
    const hasClient = typeof window.isClientSelected === 'function' && window.isClientSelected();
    const hasPeriod = !!(periodStartInput && periodEndInput && periodStartInput.value && periodEndInput.value);
    if (!hasClient || !selectedFile || !hasPeriod || (!checkMappingComplete() || !checkTotalRowChoice(true))) {
        setValidationStatus('Select a client, file and period, then complete the required column mappings.', 'error');
        if (opts.spinnerShown) hideTrialBalanceActionSpinner(tbValidationRunBtn);
        stopTrialBalanceLoading(opts.loadingToken);
        return;
    }
    const token = ++validationRequestToken;
    const revision = validationRevision;
    const loadingToken = opts.loadingToken || startTrialBalanceLoading(tbValidationRunBtn || validateTrialBalanceBtn);
    const spinnerShown = opts.spinnerShown || showTrialBalanceActionSpinner(tbValidationRunBtn, 'Validating Trial Balance…');
    if (tbValidationRunBtn) tbValidationRunBtn.disabled = true;
    if (uploadTrialBalanceBtn) uploadTrialBalanceBtn.disabled = true;
    validationResult = null;
    criticalIssueValidationRunning = true;
    updateCriticalIssueNavigator();
    validationDirty = true;
    setValidationStatus('Validating mapped rows and Trial Balance controls…', 'loading');
    try {
        const res = await tbApiRequest(API_BASE + '/api/trial-balance/validate', {
            method: 'POST',
            body: buildTrialBalanceFormData()
        });
        const result = await res.json();
        if (token !== validationRequestToken) return;
        if (revision !== validationRevision) {
            setValidationStatus('Values changed while validation was running. Revalidate before saving.', 'info');
            return;
        }
        if (!res.ok) {
            const detail = result.detail;
            throw new Error(typeof detail === 'string' ? detail : (detail && detail.message) || 'Trial Balance validation failed.');
        }
        validationResult = result;
        renderTrialBalanceControls(result);
        validationIssueSnapshot = {
            errors: Array.isArray(result.errors) ? result.errors.slice() : [],
            warnings: Array.isArray(result.warnings) ? result.warnings.slice() : [],
        };
        autoClassificationByRow = result.auto_classification || {};
        setCriticalIssueQueue(result.errors);
        validationDirty = false;
        renderTrialBalanceValidationGrid();
        const issueCount = Number(result.issue_count || 0);
        const warningCount = Number(result.warning_count || 0);
        const rollforwardCount = Number(result.rollforward_difference_count || 0);
        const parentWarningCount = (result.warnings || []).filter(function (issue) { return issue.field === 'Account Code'; }).length;
        const globalDifferenceCount = (result.global_adjustments || []).length;
        if (result.valid) {
            const notes = [];
            if (rollforwardCount) notes.push(`${rollforwardCount} leaf roll-forward difference(s) are listed below and will be saved separately.`);
            if (parentWarningCount) notes.push(`${parentWarningCount} non-blocking Parent account-code warning(s) are also shown.`);
            if (globalDifferenceCount) notes.push(`${globalDifferenceCount} global footing difference line(s) will be saved separately.`);
            const note = notes.length ? ` ⚠️ ${notes.join(' ')}` : '';
            const readyText = rollforwardCount || globalDifferenceCount
                ? 'Trial Balance is ready to save for draft accounts with unresolved differences.' : 'Trial Balance is ready to save.';
            setValidationStatus(`✅ ${readyText} ${result.account_count} accounts checked; source amounts remain unchanged and row-level differences use leaf accounts with a 0.01 tolerance.${note}`, warningCount ? 'info' : 'success');
        } else {
            const globalIssues = (result.errors || []).filter(function (issue) { return issue.excel_row == null; })
                .map(function (issue) { return issue.message; }).join(' ');
            const omitted = result.omitted_issue_count ? ` ${result.omitted_issue_count} additional issue(s) are not listed individually.` : '';
            const warningNotes = [];
            if (rollforwardCount) warningNotes.push(`${rollforwardCount} leaf roll-forward difference(s) are listed below.`);
            if (parentWarningCount) warningNotes.push(`${parentWarningCount} non-blocking Parent account-code warning(s) are also shown.`);
            const warningNote = warningNotes.length ? ` ${warningNotes.join(' ')}` : '';
            setValidationStatus(`⚠️ ${issueCount} blocking issue(s) found. ${globalIssues} Resolve the listed issues, then revalidate before saving.${warningNote}${omitted}`, 'error');
        }
        updateClientDependentUI();
    } catch (error) {
        if (token !== validationRequestToken) return;
        validationResult = null;
        validationDirty = true;
        renderTrialBalanceValidationGrid();
        setValidationStatus(error.message, 'error');
        updateClientDependentUI();
    } finally {
        if (token === validationRequestToken) {
            criticalIssueValidationRunning = false;
            updateCriticalIssueNavigator();
        }
        if (token === validationRequestToken && tbValidationRunBtn) tbValidationRunBtn.disabled = false;
        if (spinnerShown) hideTrialBalanceActionSpinner(tbValidationRunBtn);
        stopTrialBalanceLoading(loadingToken);
    }
}

function getMappedColumnIndexes() {
    return Array.from(new Set(TB_MAPPING_FIELDS
        .map(function (field) { return fileHeaders.indexOf(tbColumnMapping[field.role]); })
        .filter(function (index) { return index >= 0; })));
}

function isMappedSourceRowEmpty(row) {
    return !getMappedColumnIndexes().some(function (index) {
        return accountCodeCellText(row[index]) !== '';
    });
}

function getSourceSubtotalRowNumbers() {
    // Keep the raw row array intact so filtered lines retain their original Excel row numbers.
    const codeIndex = fileHeaders.indexOf(tbColumnMapping.account_code_column);
    if (codeIndex < 0) return new Set();
    const subtotalRows = new Set();
    fileAllRows.forEach(function (row, rowIndex) {
        if (!accountCodeCellText(row[codeIndex]) && !isMappedSourceRowEmpty(row)) {
            subtotalRows.add(rowIndex + 2);
        }
    });
    return subtotalRows;
}

function getExcludedSubtotalRowNumbers() {
    return Array.from(getSourceSubtotalRowNumbers())
        .filter(function (rowNumber) { return !includedSubtotalRows.has(rowNumber); })
        .sort(function (left, right) { return left - right; });
}

function renderValidationClassificationBadge(cell, rowNumber, value, automaticClass) {
    const classification = value || automaticClass || 'leaf';
    const badge = buildClassificationCellContent({
        is_leaf: classification === 'leaf',
        manually_overridden: Object.prototype.hasOwnProperty.call(accountClassificationOverrides, rowNumber),
    });
    const badgeElement = badge.querySelector('.tb-classification-badge');
    badgeElement.classList.add('tb-validation-classification-display');
    badgeElement.title = 'Double-click to change';
    cell.dataset.excelRow = String(rowNumber);
    cell.dataset.value = classification;
    cell.dataset.automatic = automaticClass || 'leaf';
    cell.replaceChildren(badge);
}

function renderTrialBalanceValidationGrid() {
    if (!tbValidationHead || !tbValidationBody) return;
    tbValidationHead.innerHTML = '';
    tbValidationBody.innerHTML = '';
    const codeIndex = fileHeaders.indexOf(tbColumnMapping.account_code_column);
    const nameIndex = fileHeaders.indexOf(tbColumnMapping.account_name_column);
    const mappingRoleByIndex = new Map();
    const mappedSourceIndexes = new Set(getMappedColumnIndexes());
    TB_MAPPING_FIELDS.forEach(function (field) {
        const index = fileHeaders.indexOf(tbColumnMapping[field.role]);
        if (index >= 0) mappingRoleByIndex.set(index, field.role);
    });
    const columns = [{ kind: 'row', label: 'Excel Row', className: 'tb-validation-row-number' }];
    fileHeaders.forEach(function (label, index) {
        // Keep the validation view aligned with the import mapping. The mapping
        // pane remains the full source preview for checking unmapped columns.
        if (!mappedSourceIndexes.has(index)) return;
        const role = mappingRoleByIndex.get(index);
        let className = 'tb-validation-source-column';
        if (index === codeIndex) className += ' tb-validation-account-code';
        if (index === nameIndex) className += ' tb-validation-account-name';
        if (role && role.endsWith('_column') && role !== 'account_code_column' && role !== 'account_name_column') {
            className += ' tb-validation-amount';
        }
        columns.push({ kind: 'source', label: label, index: index, className: className });
        if (index === nameIndex) {
            columns.push({ kind: 'classification', label: 'Classification', className: 'tb-validation-classification' });
        }
    });
    if (nameIndex < 0) {
        columns.push({ kind: 'classification', label: 'Classification', className: 'tb-validation-classification' });
    }
    columns.push({ kind: 'issues', label: 'Validation Issues', className: 'tb-validation-issue-column' });

    const header = document.createElement('tr');
    columns.forEach(function (column) {
        const th = document.createElement('th');
        th.textContent = column.label;
        th.className = column.className;
        header.appendChild(th);
    });
    tbValidationHead.appendChild(header);
    if (tbValidationStickyHead) {
        tbValidationStickyHead.innerHTML = '';
        const stickyTable = document.createElement('table');
        stickyTable.className = 'tb-validation-table';
        const stickyThead = document.createElement('thead');
        const stickyHead = header.cloneNode(true);
        stickyThead.appendChild(stickyHead);
        stickyTable.appendChild(stickyThead);
        tbValidationStickyHead.appendChild(stickyTable);
        syncTrialBalanceStickyHeader();
    }

    const issuesByRow = new Map();
    function collectRowIssues(items, severity) {
        (Array.isArray(items) ? items : []).forEach(function (issue) {
            if (issue.excel_row == null) return;
            const key = String(issue.excel_row);
            if (!issuesByRow.has(key)) issuesByRow.set(key, []);
            issuesByRow.get(key).push({ message: issue.message, severity: severity, field:issue.field });
        });
    }
    collectRowIssues(validationIssueSnapshot.errors, 'error');
    collectRowIssues(validationIssueSnapshot.warnings, 'warning');
    const subtotalRows = getSourceSubtotalRowNumbers();
    const excludedSubtotalRows = getExcludedSubtotalRowNumbers();
    if (tbSubtotalToolbar && tbSubtotalSummary && tbSubtotalToggleBtn) {
        tbSubtotalToolbar.hidden = subtotalRows.size === 0;
        const includedCount = subtotalRows.size - excludedSubtotalRows.length;
        tbSubtotalSummary.textContent = `${excludedSubtotalRows.length} subtotal rows excluded${includedCount ? ` · ${includedCount} treated as accounts` : ''}`;
        tbSubtotalToggleBtn.textContent = showExcludedSubtotalRows ? 'Hide excluded subtotal rows' : 'Show excluded subtotal rows';
        tbSubtotalToggleBtn.hidden = excludedSubtotalRows.length === 0;
    }
    const fragment = document.createDocumentFragment();
    fileAllRows.forEach(function (row, rowIndex) {
        const excelRow = rowIndex + 2;
        if (isMappedSourceRowEmpty(row)) return;
        const isControlTotal = validationResult && (validationResult.source_total_rows || []).includes(excelRow);
        if (isControlTotal) return;
        const isSubtotalCandidate = subtotalRows.has(excelRow);
        const isExcludedSubtotal = isSubtotalCandidate && !includedSubtotalRows.has(excelRow);
        if (isExcludedSubtotal && !showExcludedSubtotalRows) return;
        const tr = document.createElement('tr');
        tr.dataset.excelRow = String(excelRow);
        if (isExcludedSubtotal) tr.classList.add('tb-subtotal-row');
        const issuesContent = document.createElement('div');
        issuesContent.className = 'tb-validation-issue-content';
        (issuesByRow.get(String(excelRow)) || []).forEach(function (issue) {
            const message = document.createElement('span');
            message.className = issue.severity === 'warning' ? 'tb-validation-warning' : 'tb-validation-error';
            message.textContent = issue.message;
            issuesContent.appendChild(message);
        });
        if (isSubtotalCandidate) {
            const treatmentButton = document.createElement('button');
            treatmentButton.type = 'button';
            treatmentButton.className = 'btn-secondary-action tb-subtotal-row-action';
            treatmentButton.dataset.excelRow = String(excelRow);
            treatmentButton.dataset.subtotalAction = isExcludedSubtotal ? 'include' : 'exclude';
            treatmentButton.textContent = isExcludedSubtotal ? 'Treat as account' : 'Mark as subtotal';
            issuesContent.appendChild(treatmentButton);
        }

        columns.forEach(function (column) {
            const cell = document.createElement('td');
            cell.className = column.className;
            if (column.kind === 'row') {
                cell.textContent = String(excelRow);
            } else if (column.kind === 'source') {
                const sourceValue = accountCodeCellText(row[column.index]);
                if (column.index === codeIndex && !isExcludedSubtotal) {
                    const display = document.createElement('span');
                    display.className = 'tb-account-code-display';
                    display.textContent = Object.prototype.hasOwnProperty.call(accountCodeCorrections, excelRow)
                        ? accountCodeCorrections[excelRow] : sourceValue;
                    display.title = 'Double-click to edit';
                    display.dataset.excelRow = String(excelRow);
                    display.dataset.sourceCode = sourceValue;
                    if ((issuesByRow.get(String(excelRow)) || []).some(issue => issue.field === 'Account Code')) cell.classList.add('tb-account-code-review');
                    cell.appendChild(display);
                } else {
                    if (column.className.includes('tb-validation-amount')) {
                        cell.textContent = sourceValue === '' ? '' : formatTrialBalanceControlAmount(sourceValue, 0);
                        cell.title = 'Unrounded amount: ' + sourceValue;
                    } else {
                        cell.textContent = sourceValue;
                    }
                }
            } else if (column.kind === 'classification') {
                if (isExcludedSubtotal) {
                    cell.textContent = 'Excluded';
                } else {
                    const automaticClass = autoClassificationByRow[String(excelRow)] || 'leaf';
                    const classification = accountClassificationOverrides[excelRow] || automaticClass || 'leaf';
                    renderValidationClassificationBadge(cell, excelRow, classification, automaticClass);
                }
            } else if (column.kind === 'issues') {
                cell.classList.add('tb-validation-issue-cell');
                cell.appendChild(issuesContent);
            }
            tr.appendChild(cell);
        });
        fragment.appendChild(tr);
    });
    tbValidationBody.appendChild(fragment);
    const differences = validationResult
        ? (validationResult.rollforward_differences || []).concat(validationResult.global_adjustments || []) : [];
    if (differences.length) {
        const sectionRow = document.createElement('tr');
        sectionRow.className = 'tb-difference-section';
        const sectionCell = document.createElement('td');
        sectionCell.colSpan = columns.length;
        sectionCell.textContent = `System-generated differences (${differences.length}) — leaf corrections and unassigned global differences; source amounts are unchanged.`;
        sectionRow.appendChild(sectionCell);
        tbValidationBody.appendChild(sectionRow);

        differences.forEach(function (difference) {
            const tr = document.createElement('tr');
            tr.className = 'tb-generated-difference';
            columns.forEach(function (column) {
                const cell = document.createElement('td');
                cell.className = column.className;
                if (column.kind === 'row') {
                    cell.textContent = difference.global_adjustment ? 'System' : String(difference.excel_row);
                } else if (column.kind === 'source') {
                    const role = mappingRoleByIndex.get(column.index);
                    let value = '';
                    if (role === 'account_code_column') value = difference.account_code || '';
                    else if (role === 'account_name_column') value = 'Difference — ' + (difference.account_name || difference.account_code || 'Leaf account');
                    else if (role && role.endsWith('_column')) {
                        const amount = difference[role.replace('_column', '_adjustment')] ?? 0;
                        value = Number(amount) === 0 ? '' : amount;
                    }
                    if (role && /_(dr|cr)_column$/.test(role)) {
                        cell.classList.add('tb-validation-amount');
                        const rounded = formatTrialBalanceControlAmount(value, 0);
                        cell.textContent = value === '' ? '' : rounded === '0' ? '<1' : rounded;
                        if (value !== '') cell.title = 'Unrounded adjustment: ' + value;
                    } else {
                        cell.textContent = String(value);
                    }
                } else if (column.kind === 'classification') {
                    cell.textContent = difference.global_adjustment ? 'Unassigned global difference' : 'Leaf adjustment';
                } else if (column.kind === 'issues') {
                    cell.textContent = difference.global_adjustment
                        ? 'Global footing difference recorded separately; original amounts are retained.'
                        : 'Roll-forward difference recorded separately; original source figures and observation are retained.';
                }
                tr.appendChild(cell);
            });
            tbValidationBody.appendChild(tr);
        });
    }
    renderTrialBalanceControlFooter(columns, mappingRoleByIndex);
    fitTrialBalanceValidationAmountColumns(columns);
    updateTrialBalanceUniversalScrollbar();
    updateCriticalIssueNavigator();
}

// The queue is a snapshot from the last completed validation and survives local edits until revalidation.
function setCriticalIssueQueue(issues) {
    const rowNumbers = new Set();
    (Array.isArray(issues) ? issues : []).forEach(function (issue) {
        const rowNumber = Number(issue.excel_row);
        if (Number.isInteger(rowNumber) && rowNumber > 0) rowNumbers.add(rowNumber);
    });
    criticalIssueRows = Array.from(rowNumbers).sort(function (left, right) { return left - right; });
    criticalIssueIndex = -1;
    criticalIssueVisitedRows = new Set();
    criticalIssueReviewComplete = false;
    updateCriticalIssueNavigator();
}

function clearCriticalIssueQueue() {
    criticalIssueRows = [];
    criticalIssueIndex = -1;
    criticalIssueVisitedRows = new Set();
    criticalIssueReviewComplete = false;
    updateCriticalIssueNavigator();
}

function markCriticalIssueVisited(rowNumber) {
    if (!criticalIssueRows.includes(rowNumber)) return;
    criticalIssueVisitedRows.add(rowNumber);
    criticalIssueReviewComplete = false;
    updateCriticalIssueNavigator();
}

function findNextUnvisitedCriticalIssueIndex() {
    if (!criticalIssueRows.length) return -1;
    for (let offset = 1; offset <= criticalIssueRows.length; offset += 1) {
        const index = (criticalIssueIndex + offset) % criticalIssueRows.length;
        if (!criticalIssueVisitedRows.has(criticalIssueRows[index])) return index;
    }
    return -1;
}

function focusRevalidateControl() {
    if (!tbValidationHeader) return;
    const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    tbValidationHeader.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
    window.requestAnimationFrame(function () {
        if (!tbValidationRunBtn) return;
        try { tbValidationRunBtn.focus({ preventScroll: true }); } catch (_error) { tbValidationRunBtn.focus(); }
    });
}

// Navigate only through row-level blocking findings from the last completed validation.
function updateCriticalIssueNavigator() {
    if (!tbCriticalIssueNavigator) return;
    const allVisited = criticalIssueRows.length > 0 && criticalIssueVisitedRows.size >= criticalIssueRows.length;
    tbCriticalIssueNavigator.disabled = criticalIssueRows.length === 0
        || criticalIssueValidationRunning || criticalIssueReviewComplete;
    const visitedCount = criticalIssueVisitedRows.size;
    const accessibleLabel = criticalIssueValidationRunning
        ? `Revalidation in progress, ${visitedCount} of ${criticalIssueRows.length} rows reviewed`
        : criticalIssueReviewComplete
            ? 'Revalidate to refresh critical issues'
            : allVisited
                ? `Go to Revalidate, all ${criticalIssueRows.length} critical rows reviewed`
                : criticalIssueRows.length
                    ? `Next critical issue, ${visitedCount} of ${criticalIssueRows.length} rows reviewed`
        : 'No row-level critical issues to navigate';
    tbCriticalIssueNavigator.setAttribute('aria-label', accessibleLabel);
    tbCriticalIssueNavigator.title = accessibleLabel;
    if (tbCriticalIssueCount) tbCriticalIssueCount.textContent = `${visitedCount}/${criticalIssueRows.length}`;
}

if (tbCriticalIssueNavigator) {
    tbCriticalIssueNavigator.addEventListener('click', function () {
        if (!criticalIssueRows.length || !tbValidationBody || criticalIssueValidationRunning || criticalIssueReviewComplete) return;
        const nextIndex = findNextUnvisitedCriticalIssueIndex();
        if (nextIndex < 0) {
            criticalIssueReviewComplete = true;
            updateCriticalIssueNavigator();
            focusRevalidateControl();
            return;
        }
        criticalIssueIndex = nextIndex;
        const rowNumber = criticalIssueRows[criticalIssueIndex];
        const row = tbValidationBody.querySelector('tr[data-excel-row="' + rowNumber + '"]');
        if (!row) {
            criticalIssueVisitedRows.add(rowNumber);
            updateCriticalIssueNavigator();
            return;
        }

        tbValidationBody.querySelectorAll('.tb-critical-row-highlight').forEach(function (highlightedRow) {
            highlightedRow.classList.remove('tb-critical-row-highlight');
        });
        if (criticalIssueHighlightTimer) clearTimeout(criticalIssueHighlightTimer);
        criticalIssueVisitedRows.add(rowNumber);
        row.tabIndex = -1;
        try { row.focus({ preventScroll: true }); } catch (_error) { row.focus(); }
        const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        row.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center', inline: 'nearest' });
        row.classList.remove('tb-critical-row-highlight');
        void row.offsetWidth;
        row.classList.add('tb-critical-row-highlight');
        criticalIssueHighlightTimer = setTimeout(function () {
            row.classList.remove('tb-critical-row-highlight');
            criticalIssueHighlightTimer = null;
        }, 1900);
        updateCriticalIssueNavigator();
    });
}

if (tbValidationBody) {
    tbValidationBody.addEventListener('input', function (event) {
        const input = event.target.closest('.tb-code-review-input');
        if (!input) return;
        const rowNumber = Number(input.dataset.excelRow);
        markCriticalIssueVisited(rowNumber);
        const corrected = input.value.trim();
        if (corrected === input.dataset.sourceCode) delete accountCodeCorrections[rowNumber];
        else accountCodeCorrections[rowNumber] = corrected;
        tbImportCodeChangesPending = Object.keys(accountCodeCorrections).length > 0;
        invalidateTrialBalanceValidation('Account code changed. Revalidate before saving.', true);
    });
    tbValidationBody.addEventListener('change', function (event) {
        const select = event.target.closest('.tb-classification-review');
        if (!select) return;
        const rowNumber = Number(select.dataset.excelRow);
        markCriticalIssueVisited(rowNumber);
        if (select.value === select.dataset.automatic) delete accountClassificationOverrides[rowNumber];
        else accountClassificationOverrides[rowNumber] = select.value;
        invalidateTrialBalanceValidation('Classification changed. Revalidate before saving.', true);
    });
    // Keep both correction controls quiet until a deliberate double-click; blur commits/closes them.
    tbValidationBody.addEventListener('dblclick', function (event) {
        const codeDisplay = event.target.closest('.tb-account-code-display');
        if (codeDisplay) {
            const cell = codeDisplay.parentElement;
            if (!cell || cell.querySelector('.tb-code-review-input')) return;
            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'tb-code-review-input';
            input.maxLength = 120;
            input.value = codeDisplay.textContent;
            input.dataset.excelRow = codeDisplay.dataset.excelRow;
            input.dataset.sourceCode = codeDisplay.dataset.sourceCode;
            input.setAttribute('aria-label', 'Account code for Excel row ' + input.dataset.excelRow);
            cell.replaceChildren(input);
            input.focus();
            input.select();
            input.addEventListener('keydown', function (keyEvent) {
                if (keyEvent.key === 'Enter') input.blur();
                if (keyEvent.key === 'Escape') {
                    input.value = codeDisplay.textContent;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.blur();
                }
            });
            input.addEventListener('blur', function () {
                if (!cell.contains(input)) return;
                const display = document.createElement('span');
                display.className = 'tb-account-code-display';
                display.textContent = input.value.trim();
                display.title = 'Double-click to edit';
                display.dataset.excelRow = input.dataset.excelRow;
                display.dataset.sourceCode = input.dataset.sourceCode;
                cell.replaceChildren(display);
            });
            return;
        }

        const cell = event.target.closest('td.tb-validation-classification');
        if (!cell || !cell.dataset.excelRow) return;
        if (!cell || cell.querySelector('.tb-classification-review')) return;
        const select = document.createElement('select');
        select.className = 'tb-classification-review';
        select.dataset.excelRow = cell.dataset.excelRow;
        select.dataset.automatic = cell.dataset.automatic;
        [['leaf', 'Leaf'], ['parent', 'Parent']].forEach(function (optionData) {
            const option = document.createElement('option');
            option.value = optionData[0];
            option.textContent = optionData[1];
            select.appendChild(option);
        });
        select.value = cell.dataset.value;
        cell.replaceChildren(select);
        select.focus();
        if (typeof select.showPicker === 'function') {
            try { select.showPicker(); } catch (_error) { /* Focus still exposes the native selector. */ }
        }
        select.addEventListener('blur', function () {
            if (cell.contains(select)) {
                renderValidationClassificationBadge(
                    cell,
                    Number(select.dataset.excelRow),
                    select.value,
                    select.dataset.automatic
                );
            }
        });
    });
    tbValidationBody.addEventListener('click', function (event) {
        const treatmentButton = event.target.closest('.tb-subtotal-row-action');
        if (treatmentButton) {
            const rowNumber = Number(treatmentButton.dataset.excelRow);
            if (treatmentButton.dataset.subtotalAction === 'include') {
                includedSubtotalRows.add(rowNumber);
                invalidateTrialBalanceValidation('Subtotal row selected as an account. Enter its account code, then revalidate.');
            } else {
                includedSubtotalRows.delete(rowNumber);
                delete accountCodeCorrections[rowNumber];
                delete accountClassificationOverrides[rowNumber];
                invalidateTrialBalanceValidation('Row excluded as a source subtotal. Revalidate before saving.');
            }
            renderTrialBalanceValidationGrid();
            return;
        }
    });
}

if (tbSubtotalToggleBtn) {
    tbSubtotalToggleBtn.addEventListener('click', function () {
        showExcludedSubtotalRows = !showExcludedSubtotalRows;
        renderTrialBalanceValidationGrid();
    });
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
    resetColumnMapping();
    accountCodeCorrections = {};
    accountClassificationOverrides = {};
    autoClassificationByRow = {};
    validationIssueSnapshot = { errors: [], warnings: [] };
    invalidateTrialBalanceValidation('File changed. Validate the selected Trial Balance before saving.');
    if (uploadToolbar) uploadToolbar.classList.add('has-selected-file');
    if (fileName) fileName.textContent = file.name;
    if (fileName) fileName.title = file.name;
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
    fileAllRows = [];
    if (tbTotalExcelRow) tbTotalExcelRow.value = '';
    if (tbNoTotalRow) tbNoTotalRow.checked = false;
    renderTotalRowPreview();
    checkTotalRowChoice(false);
    accountCodeCorrections = {};
    accountClassificationOverrides = {};
    autoClassificationByRow = {};
    clearCriticalIssueQueue();
    includedSubtotalRows = new Set();
    showExcludedSubtotalRows = false;
    validationResult = null;
    validationDirty = true;
    closeTrialBalanceValidation();
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
        if (uploadToolbar) uploadToolbar.classList.remove('has-selected-file');
        if (fileInfo) fileInfo.style.display = 'none';
        if (fileName) fileName.textContent = '';
        if (fileName) fileName.removeAttribute('title');
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
    invalidateTrialBalanceValidation('Period changed. Validate again before saving.');
    updateClientDependentUI();
}

if (periodStartInput) {
    periodStartInput.addEventListener('change', handlePeriodDateChanged);
}
if (periodEndInput) {
    periodEndInput.addEventListener('change', handlePeriodDateChanged);
}
if (periodTypeSelect) {
    periodTypeSelect.addEventListener('change', function () {
        invalidateTrialBalanceValidation('Period type changed. Validate again before saving.');
    });
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
        const res = await tbApiRequest(url);
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

    const res = await tbApiRequest(API_BASE + '/api/trial-balance/suggest-mapping', {
        method: 'POST',
        body: formData
    });
    const result = await res.json();

    if (!res.ok) {
        throw new Error(result.detail || 'Failed to read file for mapping suggestions');
    }
    return result;
}

async function fetchSavedTrialBalanceColumnMapping(clientId) {
    const url = API_BASE + '/api/trial-balance/saved-column-mapping?client_id=' + encodeURIComponent(clientId);
    const res = await tbApiRequest(url);
    const result = await res.json();
    if (!res.ok) throw new Error(result.detail || 'Could not load the saved client mapping.');
    return result.mapping;
}

function savedMappingMatchesHeaders(mapping, headers) {
    if (!mapping || typeof mapping !== 'object') return false;
    const mappedHeaders = TB_MAPPING_FIELDS.map(function (field) { return mapping[field.role]; });
    return mappedHeaders.every(function (header) {
        return typeof header === 'string' && header.length > 0 && headers.includes(header);
    }) && new Set(mappedHeaders).size === TB_MAPPING_FIELDS.length;
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
                const workbook = XLSX.read(data, { type: 'array', cellDates: true });
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
    const loadingToken = startTrialBalanceLoading(uploadFileBtn);
    const clientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
    showStatus('Reading file and suggesting column mapping...', 'loading');

    try {
        const results = await Promise.all([
            suggestColumnMapping(file),
            readFileRowsForPreview(file),
            clientId
                ? fetchSavedTrialBalanceColumnMapping(clientId).then(function (mapping) { return { mapping: mapping }; })
                    .catch(function (error) { return { error: error }; })
                : Promise.resolve({ mapping: null })
        ]);
        if (token !== mappingRequestToken) return; // a newer file was selected meanwhile

        const suggestResult = results[0];
        const rawRows = results[1];
        const savedMappingResult = results[2];

        fileHeaders = suggestResult.headers || [];
        fileAllRows = rawRows.slice(1);
        filePreviewRows = rawRows.slice(1, PREVIEW_ROW_COUNT + 1); // header row dropped
        accountCodeCorrections = {};
        accountClassificationOverrides = {};
        autoClassificationByRow = {};
        includedSubtotalRows = new Set();
        showExcludedSubtotalRows = false;
        invalidateTrialBalanceValidation('File mapping ready. Validate before saving.');
        const suggestedMapping = {
            account_code_column: suggestResult.suggestions.account_code_column || null,
            account_name_column: suggestResult.suggestions.account_name_column || null,
            opening_dr_column: suggestResult.suggestions.opening_dr_column || null,
            opening_cr_column: suggestResult.suggestions.opening_cr_column || null,
            period_dr_column: suggestResult.suggestions.period_dr_column || null,
            period_cr_column: suggestResult.suggestions.period_cr_column || null,
            closing_dr_column: suggestResult.suggestions.closing_dr_column || null,
            closing_cr_column: suggestResult.suggestions.closing_cr_column || null,
        };
        const savedMappingApplied = savedMappingMatchesHeaders(savedMappingResult.mapping, fileHeaders);
        tbColumnMapping = savedMappingApplied ? savedMappingResult.mapping : suggestedMapping;

        renderMappingDropdowns();
        renderMappingPreviewTable();
        checkMappingComplete();
        if (mappingSection) mappingSection.style.display = 'block';
        if (savedMappingApplied) {
            showStatus('✅ Saved mapping for this client applied. Review the columns, then validate the Trial Balance.', 'success');
        } else if (savedMappingResult.error) {
            showStatus('⚠️ Could not load this client’s saved mapping. Review the suggested columns before validating.', 'error');
        } else if (savedMappingResult.mapping) {
            showStatus('⚠️ This file’s headers do not match the saved client mapping. Review the suggested columns before validating.', 'info');
        } else {
            showStatus('✅ Column mapping suggested. Review the sample rows, then validate the full Trial Balance.', 'success');
        }
    } catch (error) {
        if (token !== mappingRequestToken) return;
        showStatus('❌ Could not read file for column mapping: ' + error.message, 'error');
        resetColumnMapping();
    } finally {
        if (token === mappingRequestToken) {
            updateClientDependentUI();
        }
        stopTrialBalanceLoading(loadingToken);
    }
}

// Builds one <tr> per TB_MAPPING_FIELDS entry: field label, a <select> of fileHeaders
// (excluding whatever's already chosen by another role), and a ✅/❌ status cell.
// Same interaction pattern as the generic Upload module's mapping table
// (static/upload.js's renderMapping()) -- full re-render on any change so every row's
// exclusion list and status stay in sync.
function renderMappingDropdowns() {
    renderTotalRowPreview();
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
            accountCodeCorrections = {};
            accountClassificationOverrides = {};
            autoClassificationByRow = {};
            includedSubtotalRows = new Set();
            showExcludedSubtotalRows = false;
            invalidateTrialBalanceValidation('Column mapping changed. Validate again before saving.');
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

// Renders the first PREVIEW_ROW_COUNT rows as a mirror of the uploaded Client Excel file:
// column labels come from fileHeaders (the same
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
            td.textContent = accountCodeCellText(value);
            tr.appendChild(td);
        });
        mappingPreviewBody.appendChild(tr);
    });
}

function accountCodeCellText(value) {
    if (value instanceof Date && !Number.isNaN(value.getTime())) {
        const year = value.getFullYear();
        const month = String(value.getMonth() + 1).padStart(2, '0');
        const day = String(value.getDate()).padStart(2, '0');
        const hours = String(value.getHours()).padStart(2, '0');
        const minutes = String(value.getMinutes()).padStart(2, '0');
        const seconds = String(value.getSeconds()).padStart(2, '0');
        return year + '-' + month + '-' + day + ' ' + hours + ':' + minutes + ':' + seconds;
    }
    return value === undefined || value === null ? '' : String(value).trim();
}

// Checks all 8 required fields are mapped; shows/hides the inline validation message
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
            mappingValidationText.textContent = 'Map the following required field(s) before validation: ' + missing.join(', ') + '.';
            mappingValidationMessage.style.display = 'flex';
        }
    }
    return missing.length === 0;
}

async function saveTrialBalanceColumnMapping() {
    const clientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
    if (!clientId || !selectedFile || !fileHeaders.length || (!checkMappingComplete() || !checkTotalRowChoice(true))) {
        showStatus('Select a client and file, then map all required Trial Balance fields before saving.', 'error');
        return;
    }
    const mappedHeaders = TB_MAPPING_FIELDS.map(function (field) { return tbColumnMapping[field.role]; });
    if (mappedHeaders.some(function (header) { return !fileHeaders.includes(header); })
        || new Set(mappedHeaders).size !== TB_MAPPING_FIELDS.length) {
        showStatus('The current mapping must use distinct columns from the selected file.', 'error');
        return;
    }
    const actionKey = 'saved-column-mapping';
    if (trialBalanceActionsInFlight.has(actionKey)) return;
    trialBalanceActionsInFlight.add(actionKey);
    const loadingToken = startTrialBalanceLoading(saveTrialBalanceMappingBtn);
    updateClientDependentUI();
    try {
        const res = await tbApiRequest(API_BASE + '/api/trial-balance/saved-column-mapping', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ client_id: clientId, mapping: tbColumnMapping })
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || 'Could not save the client mapping.');
        showStatus('✅ Column mapping saved for this client. Matching future files will reuse it.', 'success');
    } catch (error) {
        showStatus('❌ ' + error.message, 'error');
    } finally {
        trialBalanceActionsInFlight.delete(actionKey);
        updateClientDependentUI();
        stopTrialBalanceLoading(loadingToken);
    }
}

if (saveTrialBalanceMappingBtn) {
    saveTrialBalanceMappingBtn.addEventListener('click', saveTrialBalanceColumnMapping);
}

// ---------- IMPORT PROGRESS BAR (Fast Trial Balance upload) ----------
// Polls GET /api/trial-balance/upload-status/{progress_key} (routers/trial_balance.py) while
// uploadTrialBalance()'s POST is in flight -- mirrors upload.js's own pollImportProgress()/
// updateImportProgressBar(), tb-prefixed since this page has its own progressKey scheme
// (TB upload is a single one-shot endpoint, so the key is generated client-side, not
// server-issued the way Data Bank's file_id is).
let tbUploadProgressPollInterval = null;

function showTbUploadProgressBar() {
    const bar = document.getElementById('tbUploadProgressBar');
    const fill = document.getElementById('tbUploadProgressFill');
    const label = document.getElementById('tbUploadProgressLabel');
    if (!bar) return;
    if (fill) fill.style.width = '0%';
    if (label) label.textContent = '0 of 0 accounts uploaded';
    bar.style.display = 'flex';
}

function hideTbUploadProgressBar() {
    const bar = document.getElementById('tbUploadProgressBar');
    if (bar) bar.style.display = 'none';
}

function updateTbUploadProgressBar(current, total) {
    const fill = document.getElementById('tbUploadProgressFill');
    const label = document.getElementById('tbUploadProgressLabel');
    const pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0;
    if (fill) fill.style.width = pct + '%';
    if (label) label.textContent = `${current} of ${total} accounts uploaded`;
}

function pollTbUploadProgress(progressKey) {
    tbUploadProgressPollInterval = setInterval(async () => {
        try {
            const res = await tbApiRequest(API_BASE + '/api/trial-balance/upload-status/' + progressKey);
            if (!res.ok) return;
            const status = await res.json();
            updateTbUploadProgressBar(status.current || 0, status.total || 0);
        } catch (e) {
            // Silently skip a failed poll tick -- the next tick (or the final upload
            // response itself) will resolve the UI either way, same as upload.js's convention.
        }
    }, 1000);
}

function stopTbUploadProgressPoll() {
    if (tbUploadProgressPollInterval) {
        clearInterval(tbUploadProgressPollInterval);
        tbUploadProgressPollInterval = null;
    }
}

// ---------- UPLOAD & CLASSIFY ----------
async function uploadTrialBalance() {

    // Defensive guard -- the button is disabled unless all of these are present,
    // but re-check directly in case of a race (e.g. a stale click event). Period
    // Start/End are now required (not optional) since the old standalone As of Date
    // field is gone and Period End's value is what actually gets sent as as_of_date.
    const clientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
    const hasPeriod = !!(periodStartInput && periodEndInput && periodStartInput.value && periodEndInput.value);
    if (!clientId || !selectedFile || !hasPeriod) {
        setValidationStatus('Select a client, a Period Start/End, and a file first.', 'error');
        return;
    }
    if (!validationResult || !validationResult.valid || validationDirty) {
        setValidationStatus('Run validation and resolve all issues before saving.', 'error');
        return;
    }
    // Defensive re-check of the column mapping too, same reasoning as the guard above --
    // the button is disabled unless mapping is complete, but re-verify directly in case
    // of a race (e.g. a stale click event from before the last dropdown change).
    if ((!checkMappingComplete() || !checkTotalRowChoice(true))) {
        return;
    }

    const unlinkedParents = (validationResult?.warnings || []).filter(issue => issue.message.includes('Parent has no linked child accounts'));
    if (unlinkedParents.length && !window.confirm(unlinkedParents.length + ' parent account(s) have no linked children. Correct their codes before import where possible. Import with these parents intentionally unlinked?')) return;
    if (trialBalanceActionsInFlight.has('upload')) return;
    trialBalanceActionsInFlight.add('upload');
    const loadingToken = startTrialBalanceLoading(uploadTrialBalanceBtn);
    const previousDisabled = uploadTrialBalanceBtn ? uploadTrialBalanceBtn.disabled : false;
    if (uploadTrialBalanceBtn) uploadTrialBalanceBtn.disabled = true;

    try {
        // Section 2, Phase 6: authoritative overwrite-conflict gate. This re-checks (or
        // reuses a still-valid cached result) right before the actual request -- catches
        // a period entered/changed after file selection, and re-prompts if an earlier
        // check on this exact period was declined or failed. Period is now always present
        // by this point (guarded above), so this check always runs.
        const conflictResult = await checkAndConfirmPeriodOverwrite(clientId, periodStartInput.value, periodEndInput.value);
        if (conflictResult === 'declined' || conflictResult === 'error') {
            // No request sent -- the form stays exactly as the auditor left it so
            // they can adjust the period (or try again) themselves.
            setValidationStatus('Upload cancelled.', 'info');
            return;
        }
        const confirmOverwrite = conflictResult === 'confirmed';

        if (summaryContainer) summaryContainer.style.display = 'none';
        setValidationStatus('Saving the validated Trial Balance…', 'loading');

        // Fast Trial Balance upload: client-generated progress key (TB upload is a single
        // one-shot endpoint, so there's no server-issued file_id the way Data Bank's two-step
        // upload flow has). Started before the request fires so the bar is visible for the
        // whole request, not just after the response comes back.
        const progressKey = 'tb_' + Date.now() + '_' + Math.random().toString(36).slice(2);
        showTbUploadProgressBar();
        pollTbUploadProgress(progressKey);

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
        formData.append('progress_key', progressKey);
        formData.append('account_code_overrides', JSON.stringify(accountCodeCorrections));
        formData.append('account_classification_overrides', JSON.stringify(accountClassificationOverrides));
        formData.append('excluded_excel_rows', JSON.stringify(getExcludedSubtotalRowNumbers()));
        appendTotalRowChoice(formData);

        const res = await tbApiRequest(API_BASE + '/api/trial-balance/upload', {
            method: 'POST',
            body: formData
        });
        const result = await res.json();

        if (!res.ok) {
            const detail = result.detail;
            if (detail && typeof detail === 'object' && detail.validation) {
                validationResult = detail.validation;
                validationDirty = false;
                renderTrialBalanceValidationGrid();
                updateClientDependentUI();
            }
            throw new Error(typeof detail === 'string' ? detail : (detail && detail.message) || 'Upload failed');
        }

        tbImportCodeChangesPending = false;
        currentUploadId = result.upload_id;
        closeTrialBalanceValidation();
        const adjustmentCount = Number(result.rollforward_adjustment_count || 0) + Number(result.global_adjustment_count || 0);
        showStatus(
            adjustmentCount
                ? `✅ Uploaded and classified. ${adjustmentCount} difference line(s) were saved separately; original amounts were retained.`
                : '✅ Uploaded and classified successfully',
            'success'
        );
        await loadSummary(result.upload_id);
    } catch (error) {
        setValidationStatus(error.message, 'error');
    } finally {
        // Always stop polling and hide the bar no matter how the request ends -- covers
        // both a successful upload and a thrown error the same way.
        stopTbUploadProgressPoll();
        hideTbUploadProgressBar();
        if (uploadTrialBalanceBtn) uploadTrialBalanceBtn.disabled = previousDisabled;
        updateClientDependentUI();
        stopTrialBalanceLoading(loadingToken);
        trialBalanceActionsInFlight.delete('upload');
    }
}

// ---------- SUMMARY DISPLAY (Phase 3's read-only summary endpoint) ----------
async function loadSummary(uploadId) {
    try {
        const res = await tbApiRequest(API_BASE + '/api/trial-balance/summary?upload_id=' + uploadId);
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

// ---------- AVAILABLE TRIAL BALANCES (Section 1, Phase 2: account-code correction view) ----------
// A separate, self-contained in-page view: an upload-selection dropdown (fed by Phase 1's
// list-uploads endpoint) and a Code/Name/Classification table for whichever upload is selected.
// Deliberately kept independent from the temporary full-file validation sheet.

// Human-readable period-type labels for the dropdown -- mirrors periodTypeSelect's own
// option text so a listed upload's period type reads the same way here as it does on
// the upload form itself.
const PERIOD_TYPE_LABELS = {
    monthly: 'Monthly',
    quarterly: 'Quarterly',
    half_yearly: 'Half-Yearly',
    annual: 'Annual',
};

const SHORT_MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// Builds a dropdown label for one upload, e.g. "Quarterly 2026 (Jul-Sep)" or, for a
// period spanning two calendar years, "Quarterly (Jul 2025 - Jun 2026)". A real quarter
// number (Q1/Q2...) isn't derived, since an arbitrary date range can't be reliably
// mapped to a calendar quarter without assuming alignment that isn't guaranteed here --
// the period type name is used instead. Uploads made before Section 2's period fields
// existed have no period_start/period_end and fall back to an id/status/row-count label.
function buildUploadLabel(upload) {
    const typeLabel = PERIOD_TYPE_LABELS[upload.period_type] || 'Period';

    if (!upload.period_start || !upload.period_end) {
        return 'Upload #' + upload.id + ' (' + upload.status + ', ' + upload.row_count + ' rows)';
    }

    const start = new Date(upload.period_start);
    const end = new Date(upload.period_end);
    const startMonth = SHORT_MONTH_NAMES[start.getMonth()];
    const endMonth = SHORT_MONTH_NAMES[end.getMonth()];
    const startYear = start.getFullYear();
    const endYear = end.getFullYear();

    if (startYear === endYear) {
        return typeLabel + ' ' + endYear + ' (' + startMonth + '-' + endMonth + ')';
    }
    return typeLabel + ' (' + startMonth + ' ' + startYear + ' - ' + endMonth + ' ' + endYear + ')';
}

function closeSavedSetupMenu() {
    if (tbAssignmentSetupMenu) tbAssignmentSetupMenu.hidden = true;
    if (tbAssignmentSetup) tbAssignmentSetup.setAttribute('aria-expanded', 'false');
}
function savedFilterInput(kind) {
    return kind === 'note' ? tbSavedNoteSearch : tbSavedCategorySearch;
}
function savedFilterQuery(kind) {
    const input = savedFilterInput(kind);
    return input ? String(input.dataset.savedFilterQuery ?? input.value ?? '') : '';
}
function setSavedFilterQuery(kind, value) {
    const input = savedFilterInput(kind);
    if (!input) return;
    input.dataset.savedFilterQuery = String(value ?? '');
    input.value = input.dataset.savedFilterQuery;
    syncSavedFilterDisplay(kind);
}
function syncSavedFilterDisplay(kind) {
    const input = savedFilterInput(kind);
    const trigger = kind === 'note' ? tbSavedNoteTrigger : tbSavedCategoryTrigger;
    const selection = kind === 'note' ? tbSavedNoteSelections : tbSavedCategorySelections;
    if (!input || !trigger) return;
    const label = trigger.querySelector('[data-saved-filter-trigger-value]');
    if (!label) return;
    if (selection.size) {
        const selected = Array.from(selection);
        const chosen = selected.length === 1
            ? (kind === 'note' ? tbSavedNoteOptions : tbSavedCategoryOptions)?.querySelector('[data-saved-filter-option][data-saved-filter-value="' + CSS.escape(selected[0]) + '"]')?.querySelector('span')?.textContent || selected[0]
            : selected.length + ' selected';
        const summary = chosen;
        label.textContent = summary;
        trigger.title = selected.join('; ');
    } else {
        const query = savedFilterQuery(kind).trim();
        label.textContent = query || 'Search or choose';
        if (query) trigger.title = query; else trigger.removeAttribute('title');
    }
}
function setSavedFilterAvailability(kind, disabled) {
    const input = savedFilterInput(kind);
    const trigger = kind === 'note' ? tbSavedNoteTrigger : tbSavedCategoryTrigger;
    if (input) input.disabled = disabled;
    if (trigger) trigger.disabled = disabled;
}
function rememberSavedWorkflow() {
    tbWorkflowStates.set(tbWorkflowMode, {category: savedFilterQuery('category'),
        note: savedFilterQuery('note'), categorySelections: Array.from(tbSavedCategorySelections),
        noteSelections: Array.from(tbSavedNoteSelections), left: availableAccountsTableWrapper ? availableAccountsTableWrapper.scrollLeft : 0, top: window.scrollY});
}
function clearSavedWorkflowStates() {
    tbWorkflowStates.clear();
    tbSavedCategorySelections.clear();
    tbSavedNoteSelections.clear();
}
function syncSavedWorkflowColumns() {
    const financial = tbWorkflowMode === 'financial';
    const definition = window.AccountAssignments && AccountAssignments.workflows[tbWorkflowMode];
    if (tbAssignmentWorkflow) tbAssignmentWorkflow.value = tbWorkflowMode;
    const legacyAssignments = getElement('tbAssignCategoriesBtn');
    if (legacyAssignments) legacyAssignments.hidden = financial;
    if (tbSavedNoteSearch) tbSavedNoteSearch.closest('.tb-saved-note-filter').hidden = !financial;
    const label = getElement('tbSavedCategoryLabel');
    if (label && definition) label.textContent = definition.column;
    [availableAccountsTableWrapper, tbSavedStickyHead].filter(Boolean).forEach(function (container) {
        container.querySelectorAll('thead tr').forEach(function (row) {
            if (row.children[3] && definition) row.children[3].textContent = definition.column;
            if (row.children[4]) row.children[4].hidden = !financial;
        });
    });
    if (availableAccountsTableBody) {
        availableAccountsTableBody.querySelectorAll('tr[data-category-filter], tr.tb-generated-difference, tr.tb-draft-total').forEach(function (row) {
            if (row.children[4]) row.children[4].hidden = !financial;
        });
        availableAccountsTableBody.querySelectorAll('.tb-difference-section td, .tb-erp-warning td').forEach(cell => { cell.colSpan = 9 + savedAssignmentColumnCount(); });
    }
    if (tbAssignmentSetup) {
        tbAssignmentSetup.disabled = !availableUploadsDropdown || !availableUploadsDropdown.value;
        if (definition) { tbAssignmentSetup.title = definition.label + ' setup'; tbAssignmentSetup.setAttribute('aria-label', 'Open ' + definition.label + ' setup'); }
        if(financial || tbWorkflowMode === 'cashflow' || tbWorkflowMode === 'verification'){tbAssignmentSetup.removeAttribute('aria-haspopup');tbAssignmentSetup.removeAttribute('aria-expanded');}
        else {tbAssignmentSetup.setAttribute('aria-haspopup','menu');if(!tbAssignmentSetup.hasAttribute('aria-expanded'))tbAssignmentSetup.setAttribute('aria-expanded','false');}
    }
    if (tbAssignmentSetupMenu && definition) {
        tbAssignmentSetupMenu.replaceChildren();
        (financial || tbWorkflowMode === 'cashflow' || tbWorkflowMode === 'verification'?[]:definition.setups).forEach(function (action) {
            const link = document.createElement('a'); link.textContent = action[1];
            link.href = AccountAssignments.accUrl(tbWorkflowMode, availableUploadsDropdown && availableUploadsDropdown.value, action[0]);
            if(tbWorkflowMode==='financial') {
                link.href='#';
                link.addEventListener('click',function(event){event.preventDefault();closeSavedSetupMenu();tbOpenFinancialSetup(action[0]);});
            } else link.addEventListener('click', saveSavedWorkflowReturnState);
            tbAssignmentSetupMenu.appendChild(link);
        });
    }
}
function saveSavedWorkflowReturnState() {
    rememberSavedWorkflow();
    try {
        const clientId = window.getSelectedClientId ? window.getSelectedClientId() : '';
        sessionStorage.setItem('tb-assignment-return', JSON.stringify({clientId: String(clientId), uploadId: availableUploadsDropdown.value,
            mode: tbWorkflowMode, states: Array.from(tbWorkflowStates)}));
    } catch (_error) { /* Navigation remains usable if browser storage is unavailable. */ }
}
function restoreSavedWorkflowReturnState() {
    if (tbWorkflowReturnRestored || new URLSearchParams(location.search).get('restore') !== '1') return;
    tbWorkflowReturnRestored = true;
    try {
        const saved = JSON.parse(sessionStorage.getItem('tb-assignment-return') || 'null');
        const clientId = window.getSelectedClientId ? window.getSelectedClientId() : '';
        if (!saved || saved.clientId !== String(clientId) || saved.uploadId !== availableUploadsDropdown.value) return;
        for (const [mode, state] of saved.states || []) if (Object.hasOwn(AccountAssignments.workflows, mode)) tbWorkflowStates.set(mode, state);
        restoreSavedWorkflowFilters();
    } catch (_error) { /* A missing/obsolete return state uses the normal view defaults. */ }
}
function restoreSavedWorkflowFilters() {
    const state = tbWorkflowStates.get(tbWorkflowMode);
    setSavedFilterQuery('category', state ? state.category : '');
    setSavedFilterQuery('note', state ? state.note : '');
    tbSavedCategorySelections.clear();
    tbSavedNoteSelections.clear();
    (state && state.categorySelections || []).forEach(value => tbSavedCategorySelections.add(String(value)));
    (state && state.noteSelections || []).forEach(value => tbSavedNoteSelections.add(String(value)));
    updateSavedFilterOptions('category');
    syncSavedFilterOptionsFromRows('note');
    updateSavedFilterOptions('note');
    syncSavedFilterDisplay('category');
    syncSavedFilterDisplay('note');
    if (state) requestAnimationFrame(function () {
        if (availableAccountsTableWrapper) availableAccountsTableWrapper.scrollLeft = state.left || 0;
        window.scrollTo(0, state.top || 0);
    });
}
function finishSavedWorkflowLookup() {
    tbWorkflowCache.set(tbWorkflowMode, {category: availableAccountCategoryByCode, note: availableAccountNoteByCode,
        categoryReady: availableAccountCategoryLookupReady, noteReady: availableAccountNoteLookupReady, message: availableAccountLookupMessage,
        options: tbSavedCategoryOptions ? tbSavedCategoryOptions.innerHTML : '', noteOptions: tbSavedNoteOptions ? tbSavedNoteOptions.innerHTML : '',
        accounts: tbWorkflowMode === 'verification' ? tbVerificationAccounts : null});
    if (!availableAccountCategoryLookupReady || (tbWorkflowMode === 'financial' && !availableAccountNoteLookupReady)) tbWorkflowCache.delete(tbWorkflowMode);
    setSavedFilterAvailability('category', !availableAccountCategoryLookupReady);
    setSavedFilterAvailability('note', !availableAccountNoteLookupReady);
    availableAccountsTableBody.querySelectorAll('tr[data-category-filter]').forEach(function (row) {
        const code = row.dataset.accountCode;
        const category = availableAccountCategoryByCode.get(code) || 'Category unavailable';
        const note = availableAccountNoteByCode.get(code) || 'Note unavailable';
        row.dataset.categoryFilter = category; row.dataset.noteFilter = note;
        const categoryCell = row.querySelector('.tb-saved-category-cell');
        const noteCell = row.querySelector('.tb-saved-note-cell');
        if (categoryCell) categoryCell.textContent = category === 'Category unavailable' ? 'Unavailable' : category;
        if (noteCell) noteCell.textContent = note === 'Note unavailable' ? 'Unavailable' : note;
    });
    syncSavedWorkflowColumns();
    if(tbWorkflowMode==='financial' && tbFsIsAdmin)tbRenderFinancialAssignments();
    if(tbWorkflowMode==='cashflow' && tbFsIsAdmin)tbRenderCashflowAssignments();
    if(tbWorkflowMode==='verification' && tbFsIsAdmin)tbRenderVerificationAssignments();
    syncSavedFilterOptionsFromRows('category');
    if (tbWorkflowMode === 'financial') syncSavedFilterOptionsFromRows('note');
    applySavedCategoryFilter();
    fitSavedTrialBalanceAmountColumns();
}
function tbIndexCashflowLeaves(accounts) {
    const byCode = new Map((accounts || []).map(account => [AccountAssignments.normaliseCode(account.account_code), account]));
    const leaves = new Map();
    (accounts || []).filter(account => account.is_leaf).forEach(function (leaf) {
        let parentCode = AccountAssignments.normaliseCode(leaf.parent_account_code);
        const seen = new Set();
        while (parentCode && !seen.has(parentCode)) {
            seen.add(parentCode);
            if (!leaves.has(parentCode)) leaves.set(parentCode, []);
            leaves.get(parentCode).push(leaf);
            const parent = byCode.get(parentCode);
            parentCode = parent ? AccountAssignments.normaliseCode(parent.parent_account_code) : '';
        }
    });
    return leaves;
}
function tbCashflowCategoryFor(account) {
    const direct = account.category_id;
    if (account.is_leaf || direct != null) return direct == null ? '' : String(direct);
    const values = Array.from(new Set((tbCashflowLeaves.get(AccountAssignments.normaliseCode(account.account_code)) || []).map(leaf => leaf.category_id == null ? '' : String(leaf.category_id))));
    return values.length === 1 ? values[0] : values.length > 1 ? '__mixed' : '';
}
function tbRenderCashflowAssignments() {
    if (tbWorkflowMode !== 'cashflow' || !tbFsIsAdmin || !availableAccountsTableBody || !tbCashflowAccounts.length) return;
    const byCode = new Map(tbCashflowAccounts.map(account => [AccountAssignments.normaliseCode(account.account_code), account]));
    for (const row of availableAccountsTableBody.querySelectorAll('tr[data-category-filter]')) {
        const account = byCode.get(AccountAssignments.normaliseCode(row.dataset.accountCode));
        const cell = row.querySelector('.tb-saved-category-cell');
        if (!account || !cell) continue;
        const selected = tbCashflowCategoryFor(account);
        const category = tbCashflowCategories.find(item => String(item.id) === String(selected));
        const label = selected === '__mixed' ? 'Mixed assignments' : category ? category.label : selected ? 'Category unavailable' : 'Unassigned';
        row.dataset.categoryFilter = label;
        availableAccountCategoryByCode.set(row.dataset.accountCode, label);
        const signature = JSON.stringify([selected, tbCashflowCategories]);
        if (row.dataset.cashflowSignature !== signature || !cell.querySelector('select.tb-cashflow-category-picker')) {
            row.dataset.cashflowSignature = signature;
            cell.innerHTML = tbCashflowWorkspace.categorySelect(account, tbCashflowCategories, selected);
        }
    }
    applySavedCategoryFilter();
}
async function tbRefreshCashflowAssignments() {
    const clientId = window.getSelectedClientId(), uploadId = availableUploadsDropdown.value;
    AccountAssignments.invalidate(tbReadAssignmentJson);
    const result = await AccountAssignments.read('cashflow', clientId, uploadId, tbReadAssignmentJson);
    if (String(clientId) !== String(window.getSelectedClientId()) || String(uploadId) !== String(availableUploadsDropdown.value)) return;
    tbCashflowAccounts = result.accounts || []; tbCashflowCategories = result.categories || [];
    tbCashflowLeaves = tbIndexCashflowLeaves(tbCashflowAccounts);
    availableAccountCategoryByCode = new Map(tbCashflowAccounts.map(account => {
        const selected = tbCashflowCategoryFor(account);
        const category = tbCashflowCategories.find(item => String(item.id) === String(selected));
        return [AccountAssignments.normaliseCode(account.account_code), selected === '__mixed' ? 'Mixed assignments' : category ? category.label : selected ? 'Category unavailable' : 'Unassigned'];
    }));
    populateSavedCategoryOptions(tbCashflowCategories);
    availableAccountCategoryLookupReady = true;
    finishSavedWorkflowLookup();
}
function tbVerificationWorkstreamFor(account) {
    const direct = account.assignment && account.assignment.verification_workstream;
    if (account.is_leaf || direct) return direct || '';
    const values = Array.from(new Set((tbVerificationLeaves.get(AccountAssignments.normaliseCode(account.account_code)) || []).map(leaf => {
        const value = leaf.assignment && leaf.assignment.verification_workstream;
        return value == null ? '' : String(value);
    })));
    return values.length === 1 ? values[0] : values.length > 1 ? '__mixed' : '';
}
function tbRenderVerificationAssignments() {
    if (tbWorkflowMode !== 'verification' || !tbFsIsAdmin || !availableAccountsTableBody || !tbVerificationAccounts.length || !tbVerificationWorkspace) return;
    const byCode = new Map(tbVerificationAccounts.map(account => [AccountAssignments.normaliseCode(account.account_code), account]));
    const labels = new Map(AccountAssignments.workstreams.map(item => [String(item.value), item.label]));
    for (const row of availableAccountsTableBody.querySelectorAll('tr[data-category-filter]')) {
        const account = byCode.get(AccountAssignments.normaliseCode(row.dataset.accountCode));
        const cell = row.querySelector('.tb-saved-category-cell');
        if (!account || !cell) continue;
        const selected = tbVerificationWorkstreamFor(account);
        const label = selected === '__mixed' ? 'Mixed assignments' : labels.get(String(selected)) || (selected ? 'Workstream unavailable' : 'Unassigned');
        row.dataset.categoryFilter = label;
        availableAccountCategoryByCode.set(row.dataset.accountCode, label);
        const hasLeaves = account.is_leaf || (tbVerificationLeaves.get(AccountAssignments.normaliseCode(account.account_code)) || []).length > 0;
        const signature = JSON.stringify([selected, hasLeaves, tbFsIsAdmin]);
        if (row.dataset.verificationSignature !== signature || !cell.querySelector('.tb-verification-workstream-picker')) {
            row.dataset.verificationSignature = signature;
            cell.innerHTML = tbVerificationWorkspace.selectMarkup(Object.assign({}, account, {has_leaf_accounts: hasLeaves}), selected, AccountAssignments.workstreams);
            cell.title = hasLeaves ? '' : 'No linked leaf accounts. Correct the account code first.';
        }
        const codeCell = row.cells[0];
        codeCell.classList.toggle('tb-account-code-review', !account.is_leaf && !hasLeaves);
        if (!account.is_leaf && !hasLeaves) {
            codeCell.title = 'No linked child accounts. Correct this account code before assigning a workstream.';
            if (!codeCell.querySelector('.tb-code-review-hint')) { const hint = document.createElement('small'); hint.className = 'tb-code-review-hint'; hint.textContent = 'Check code'; codeCell.appendChild(hint); }
        } else codeCell.querySelector('.tb-code-review-hint')?.remove();
    }
    const cached = tbWorkflowCache.get('verification');
    if (cached) Object.assign(cached, {category: availableAccountCategoryByCode, options: tbSavedCategoryOptions.innerHTML, accounts: tbVerificationAccounts});
    applySavedCategoryFilter();
}
async function tbSaveVerificationAssignment(account, workstream, parent) {
    const context = {clientId: window.getSelectedClientId(), uploadId: Number(availableUploadsDropdown.value), accountCode: account.account_code};
    return tbFsWithBusy(parent ? 'Applying Verification assignment…' : 'Saving Verification assignment…', function () {
        return AccountAssignments.saveMapping(context, {verification_workstream: workstream}, tbFsApi, parent, false);
    });
}
async function tbRefreshVerificationAssignments() {
    const clientId = window.getSelectedClientId(), uploadId = availableUploadsDropdown.value;
    AccountAssignments.invalidate(tbReadAssignmentJson);
    const result = await AccountAssignments.read('mapping', clientId, uploadId, tbReadAssignmentJson);
    if (String(clientId) !== String(window.getSelectedClientId()) || String(uploadId) !== String(availableUploadsDropdown.value)) return null;
    tbVerificationAccounts = result.accounts || [];
    tbVerificationLeaves = tbIndexCashflowLeaves(tbVerificationAccounts);
    availableAccountCategoryByCode = new Map(tbVerificationAccounts.map(account => {
        const selected = tbVerificationWorkstreamFor(account);
        const label = selected === '__mixed' ? 'Mixed assignments' : AccountAssignments.workstreams.find(item => String(item.value) === String(selected))?.label || (selected ? 'Workstream unavailable' : 'Unassigned');
        return [AccountAssignments.normaliseCode(account.account_code), label];
    }));
    populateSavedCategoryOptions(AccountAssignments.workstreams.map(item => ({label:item.label,is_active:true})));
    availableAccountCategoryLookupReady = true; availableAccountNoteLookupReady = false; availableAccountLookupMessage = '';
    finishSavedWorkflowLookup();
    return result;
}
async function tbApplyVerificationAssignment(accountCode, workstream, parent) {
    AccountAssignments.invalidate(tbReadAssignmentJson);
    if (parent) return tbRefreshVerificationAssignments();
    const code = AccountAssignments.normaliseCode(accountCode);
    const account = tbVerificationAccounts.find(item => AccountAssignments.normaliseCode(item.account_code) === code);
    if (!account) return tbRefreshVerificationAssignments();
    account.assignment = Object.assign({}, account.assignment, {verification_workstream: workstream || null});
    const label = workstream ? (AccountAssignments.workstreams.find(item => item.value === workstream)?.label || 'Workstream unavailable') : 'Unassigned';
    availableAccountCategoryByCode.set(code, label);
    const row = Array.from(availableAccountsTableBody.querySelectorAll('tr[data-category-filter]')).find(item => AccountAssignments.normaliseCode(item.dataset.accountCode) === code);
    if (row) {
        row.dataset.categoryFilter = label;
        row.dataset.verificationSignature = JSON.stringify([workstream || '', true, tbFsIsAdmin]);
        const picker = row.querySelector('.tb-verification-workstream-picker');
        if (picker) picker.value = workstream || '';
    }
    const cached = tbWorkflowCache.get('verification');
    if (cached) Object.assign(cached, {category: availableAccountCategoryByCode, accounts: tbVerificationAccounts});
    applySavedCategoryFilter();
    return {accounts: tbVerificationAccounts};
}
async function tbHandleVerificationWorkstreamChange(event) {
    const select = event.target.closest('.tb-verification-workstream-picker');
    if (!select || tbWorkflowMode !== 'verification') return;
    const account = tbVerificationAccounts.find(item => AccountAssignments.normaliseCode(item.account_code) === AccountAssignments.normaliseCode(select.dataset.verificationCode));
    if (!account) return;
    const parent = !account.is_leaf;
    const leaves = tbVerificationLeaves.get(AccountAssignments.normaliseCode(account.account_code)) || [];
    if (parent && !leaves.length) { showStatus('No linked leaf accounts. Correct the account code first.', 'error'); tbRenderVerificationAssignments(); return; }
    if (parent && !window.confirm('Apply this Verification workstream to all ' + leaves.length + ' leaf accounts below ' + account.account_code + '?')) { tbRenderVerificationAssignments(); return; }
    try {
        await tbSaveVerificationAssignment(account, select.value || null, parent);
        showStatus('Verification assignment saved for ' + account.account_code + '.', 'success');
        await tbApplyVerificationAssignment(account.account_code, select.value || null, parent);
    } catch (error) { showStatus(error.message, 'error'); await tbRefreshVerificationAssignments().catch(() => {}); }
}
async function tbHandleCashflowCategoryChange(event) {
    const select = event.target.closest('.tb-cashflow-category-picker');
    if (!select || tbWorkflowMode !== 'cashflow') return;
    const account = tbCashflowAccounts.find(item => AccountAssignments.normaliseCode(item.account_code) === AccountAssignments.normaliseCode(select.dataset.cashflowCode));
    if (!account) return;
    const categoryId = select.value && select.value !== '__mixed' ? Number(select.value) : null;
    try {
        if (!account.is_leaf) {
            if (!window.confirm('Apply this Cashflow category to all leaf accounts below ' + account.account_code + '?')) { tbRenderCashflowAssignments(); return; }
            await tbFsApi('/api/account-control-center/cashflow/category-assignment/apply-to-children', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:window.getSelectedClientId(),trial_balance_upload_id:Number(availableUploadsDropdown.value),parent_account_code:account.account_code,category_id:categoryId})});
        } else {
            await tbFsApi('/api/account-control-center/cashflow/category-assignment', {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:window.getSelectedClientId(),trial_balance_upload_id:Number(availableUploadsDropdown.value),account_code:account.account_code,category_id:categoryId})});
        }
        showStatus('Cashflow category saved for ' + account.account_code + '.', 'success');
        await tbRefreshCashflowAssignments();
    } catch (error) { showStatus(error.message, 'error'); await tbRefreshCashflowAssignments().catch(() => {}); }
}
async function switchSavedWorkflow(value) {
    closeSavedSetupMenu();
    rememberSavedWorkflow();
    tbWorkflowMode = AccountAssignments.mode(value);
    const params = new URLSearchParams(location.search); params.set('workflow', tbWorkflowMode);
    if (location.protocol !== 'about:') history.replaceState(null, '', location.pathname + '?' + params);
    tbWorkflowLoadToken++;
    availableAccountCategoryByCode = new Map(); availableAccountNoteByCode = new Map();
    availableAccountCategoryLookupReady = false; availableAccountNoteLookupReady = false; availableAccountLookupMessage = '';
    if (tbSavedCategoryOptions) tbSavedCategoryOptions.replaceChildren();
    if (tbSavedNoteOptions) tbSavedNoteOptions.replaceChildren();
    restoreSavedWorkflowFilters(); syncSavedWorkflowColumns();
    const cached = tbWorkflowCache.get(tbWorkflowMode);
    if (cached) {
        availableAccountCategoryByCode = cached.category; availableAccountNoteByCode = cached.note;
        availableAccountCategoryLookupReady = cached.categoryReady; availableAccountNoteLookupReady = cached.noteReady;
        availableAccountLookupMessage = cached.message;
        if (tbWorkflowMode === 'verification') {
            tbVerificationAccounts = Array.isArray(cached.accounts) ? cached.accounts : [];
            tbVerificationLeaves = tbIndexCashflowLeaves(tbVerificationAccounts);
        }
        if (tbSavedCategoryOptions) tbSavedCategoryOptions.innerHTML = cached.options;
        if (tbSavedNoteOptions) tbSavedNoteOptions.innerHTML = cached.noteOptions;
        finishSavedWorkflowLookup();
    } else {
        setSavedFilterAvailability('category', true);
        setSavedFilterAvailability('note', true);
        finishSavedWorkflowLookup(); tbWorkflowCache.delete(tbWorkflowMode);
        if (availableCategoryUploadId) await loadAvailableUploadCategories(availableCategoryUploadId, window.getSelectedClientId(), availableAccountsLoadToken);
    }
    restoreSavedWorkflowFilters();
}
if (tbAssignmentWorkflow) tbAssignmentWorkflow.addEventListener('change', function () { switchSavedWorkflow(this.value); });
if (tbAssignmentSetup) tbAssignmentSetup.addEventListener('click', function () {
    if(tbWorkflowMode==='financial'){closeSavedSetupMenu();tbOpenFinancialSetup('financial');return;}
    if(tbWorkflowMode==='cashflow'){closeSavedSetupMenu();tbOpenCashflowSetup();return;}
    if(tbWorkflowMode==='verification'){closeSavedSetupMenu();tbOpenVerificationSetup();return;}
    const open = tbAssignmentSetupMenu.hidden;
    tbAssignmentSetupMenu.hidden = !open; this.setAttribute('aria-expanded', String(open));
    if (open && tbAssignmentSetupMenu.firstElementChild) tbAssignmentSetupMenu.firstElementChild.focus();
});
if (availableAccountsTableBody) availableAccountsTableBody.addEventListener('change', function (event) {
    tbHandleCashflowCategoryChange(event);
    tbHandleVerificationWorkstreamChange(event);
});
document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && tbAssignmentSetupMenu && !tbAssignmentSetupMenu.hidden) {
        tbAssignmentSetupMenu.hidden = true; tbAssignmentSetup.setAttribute('aria-expanded', 'false'); tbAssignmentSetup.focus();
    }
});
document.addEventListener('click', function (event) {
    if (tbAssignmentSetupMenu && !tbAssignmentSetupMenu.hidden && !tbAssignmentSetupMenu.contains(event.target) && event.target !== tbAssignmentSetup && !tbAssignmentSetup.contains(event.target)) {
        tbAssignmentSetupMenu.hidden = true; tbAssignmentSetup.setAttribute('aria-expanded', 'false');
    }
});

function populateSavedCategoryOptions(categories) {
    populateSavedFilterOptions(tbSavedCategoryOptions, categories, 'category');
}

function populateSavedNoteOptions(categories) {
    populateSavedFilterOptions(tbSavedNoteOptions, categories, 'note');
}

// Keep exact selections separate from the text query so users can add several exact values without losing contains-search.
function populateSavedFilterOptions(container, categories, kind) {
    if (!container) return;
    if (kind === 'note' && tbWorkflowMode === 'financial' && tbFsWorkspace) {
        const values = new Set(['Unassigned']);
        (tbFsWorkspace.STATE.categories || []).filter(category => category.is_active !== false && category.row_type !== 'heading')
            .forEach(category => (tbFsWorkspace.accGetEffectiveNoteOptions(category.id) || []).forEach(item => values.add(item.path.join(' '))));
        renderSavedFilterValues(container, values, kind);
        return;
    }
    const values = new Set((Array.isArray(categories) ? categories : [])
        .filter(category => category.is_active !== false && category.row_type !== 'heading')
        .map(category => String(category.label || '').trim()).filter(Boolean));
    values.add('Unassigned');
    renderSavedFilterValues(container, values, kind, false, categories);
}

function makeSavedFilterOption(label, value, kind, searchText = label) {
    const option = document.createElement('button');
    option.type = 'button';
    option.className = 'acc2-fs-picker-option';
    option.setAttribute('role', 'option');
    option.dataset.fsPickerOption = '';
    option.dataset.fsPickerValue = value;
    option.dataset.fsPickerSearch = String(searchText || label).toLocaleLowerCase();
    option.dataset.savedFilterOption = '';
    option.dataset.savedFilterValue = value;
    option.dataset.savedFilterSearch = String(searchText || label);
    option.dataset.savedFilterKind = kind;
    option.setAttribute('aria-selected', 'false');
    const text = document.createElement('span');
    text.textContent = label;
    const check = document.createElement('span');
    check.className = 'acc2-fs-picker-check';
    check.setAttribute('aria-hidden', 'true');
    check.textContent = '✓';
    option.append(text, check);
    return option;
}

function appendSavedFilterSection(container, label, key) {
    const section = document.createElement('div');
    section.className = 'acc2-fs-picker-section';
    section.dataset.fsPickerSection = '';
    if (key) section.dataset.fsPickerSectionKey = key;
    const heading = document.createElement('div');
    heading.className = 'acc2-fs-picker-section-title';
    heading.setAttribute('role', 'presentation');
    heading.textContent = label;
    section.appendChild(heading);
    container.appendChild(section);
    return section;
}

function appendSavedFilterHeading(section, label = '') {
    const group = document.createElement('div');
    group.className = 'acc2-fs-picker-heading-group';
    group.dataset.fsPickerHeadingGroup = '';
    if (label) {
        const heading = document.createElement('div');
        heading.className = 'acc2-fs-picker-heading';
        heading.setAttribute('role', 'presentation');
        heading.textContent = label;
        group.appendChild(heading);
    }
    section.appendChild(group);
    return group;
}

function renderSavedFilterValues(container, values, kind, preserveUnavailableSelections = false, categories = []) {
    container.replaceChildren();
    const selection = kind === 'note' ? tbSavedNoteSelections : tbSavedCategorySelections;
    if (kind === 'category' && tbWorkflowMode === 'financial' && tbFsWorkspace) {
        const categoryOptions = categories.length ? categories : (tbFsWorkspace.STATE.categories || []);
        const template = document.createElement('template');
        template.innerHTML = tbFsWorkspace.accFinancialPickerMenuMarkup(null);
        const source = template.content.querySelector('[data-fs-picker-option-wrap]');
        if (source) {
            Array.from(source.querySelectorAll('[data-fs-picker-option]')).forEach(function (option) {
                const id = option.dataset.fsPickerValue || '';
                const category = categoryOptions.find(item => String(item.id) === String(id));
                const value = id ? String(category && category.label || '') : 'Unassigned';
                if (id && (!category || category.is_active === false || !values.has(value))) { option.remove(); return; }
                option.dataset.savedFilterOption = '';
                option.dataset.savedFilterValue = value;
                option.dataset.savedFilterSearch = [option.dataset.fsPickerSearch, option.closest('[data-fs-picker-heading-group]')?.innerText,
                    option.closest('[data-fs-picker-section]')?.querySelector('.acc2-fs-picker-section-title')?.innerText].filter(Boolean).join(' ');
                option.dataset.savedFilterKind = kind;
            });
            Array.from(source.querySelectorAll('[data-fs-picker-section], [data-fs-picker-heading-group]')).forEach(group => {
                if (!group.querySelector('[data-fs-picker-option]')) group.remove();
            });
            source.querySelector('[data-fs-picker-no-results]')?.remove();
            container.append(...Array.from(source.children));
        }
    } else if (kind === 'note' && tbWorkflowMode === 'financial' && tbFsWorkspace) {
        const sourceCategories = tbFsWorkspace.STATE.categories || [];
        const noteValues = new Set(values);
        if (noteValues.has('Unassigned')) {
            const unassigned = appendSavedFilterHeading(appendSavedFilterSection(container, 'Notes', 'notes'));
            unassigned.appendChild(makeSavedFilterOption('Unassigned', 'Unassigned', kind));
        }
        sourceCategories.filter(category => category.is_active !== false && category.row_type !== 'heading').forEach(function (category) {
            const options = tbFsWorkspace.accGetEffectiveNoteOptions(category.id) || [];
            const matching = options.filter(item => noteValues.has(item.path.join(' ')));
            if (!matching.length) return;
            const section = appendSavedFilterSection(container, String(category.label || 'Financial Statement') + ' notes', 'note-' + category.id);
            const group = appendSavedFilterHeading(section);
            matching.forEach(item => group.appendChild(makeSavedFilterOption(item.path.join(' › '), item.path.join(' '), kind, item.path.join(' '))));
        });
        const special = Array.from(noteValues).filter(value => value !== 'Unassigned'
            && !sourceCategories.some(category => (tbFsWorkspace.accGetEffectiveNoteOptions(category.id) || []).some(item => item.path.join(' ') === value)));
        if (special.length) {
            const section = appendSavedFilterSection(container, 'Other notes', 'note-other');
            const group = appendSavedFilterHeading(section);
            special.sort((a, b) => a.localeCompare(b)).forEach(value => group.appendChild(makeSavedFilterOption(value, value, kind)));
        }
    } else {
        const section = appendSavedFilterSection(container, kind === 'note' ? 'Notes' : 'Categories', kind);
        const group = appendSavedFilterHeading(section);
        Array.from(values).sort((left, right) => left.localeCompare(right))
            .forEach(value => group.appendChild(makeSavedFilterOption(value, value, kind)));
    }
    const noMatches = document.createElement('div');
    noMatches.className = 'acc2-fs-picker-no-results';
    noMatches.dataset.savedFilterNoMatches = '';
    noMatches.textContent = 'No matching ' + (kind === 'note' ? 'notes.' : 'categories.');
    noMatches.hidden = true;
    container.appendChild(noMatches);
    const validLabels = new Set(Array.from(container.querySelectorAll('[data-saved-filter-option]'), option => option.dataset.savedFilterValue));
    if (!preserveUnavailableSelections) selection.forEach(value => { if (!validLabels.has(value)) selection.delete(value); });
    updateSavedFilterOptions(kind);
}

function syncSavedFilterOptionsFromRows(kind) {
    const container = kind === 'note' ? tbSavedNoteOptions : tbSavedCategoryOptions;
    if (!container || !availableAccountsTableBody) return;
    const field = kind === 'note' ? 'noteFilter' : 'categoryFilter';
    const rows = Array.from(availableAccountsTableBody.querySelectorAll('tr[data-category-filter]'));
    const categoryFilterActive = kind === 'note' && hasSavedCategoryFilter();
    const values = new Set(rows
        .filter(row => !categoryFilterActive || savedCategoryFilterMatchesRow(row))
        .map(row => row.dataset[field] || '')
        .filter(Boolean));
    if (!categoryFilterActive) values.add('Unassigned');
    renderSavedFilterValues(container, values, kind, categoryFilterActive);
}

function updateSavedFilterOptions(kind) {
    const input = kind === 'note' ? tbSavedNoteSearch : tbSavedCategorySearch;
    const container = kind === 'note' ? tbSavedNoteOptions : tbSavedCategoryOptions;
    if (!input || !container) return;
    const selection = kind === 'note' ? tbSavedNoteSelections : tbSavedCategorySelections;
    const query = savedFilterQuery(kind).trim().toLocaleLowerCase();
    container.querySelectorAll('[data-saved-filter-option]').forEach(option => {
        const value = option.dataset.savedFilterValue || '';
        const search = [value, option.dataset.savedFilterSearch, option.dataset.fsPickerSearch,
            option.closest('[data-fs-picker-heading-group]')?.innerText,
            option.closest('[data-fs-picker-section]')?.querySelector('.acc2-fs-picker-section-title')?.innerText]
            .filter(Boolean).join(' ').toLocaleLowerCase();
        option.hidden = !!query && !search.includes(query) && !selection.has(value);
        option.setAttribute('aria-selected', String(selection.has(value)));
    });
    container.querySelectorAll('[data-fs-picker-heading-group]').forEach(group => {
        group.hidden = !group.querySelector('[data-saved-filter-option]:not([hidden])');
    });
    container.querySelectorAll('[data-fs-picker-section]').forEach(section => {
        section.hidden = !section.querySelector('[data-saved-filter-option]:not([hidden])');
    });
    const menu = container.closest('[data-saved-filter-menu]');
    const summary = menu && menu.querySelector('[data-saved-filter-summary]');
    if (summary) summary.textContent = selection.size ? selection.size + ' exact selected' : 'Check exact matches';
    const clear = menu && menu.querySelector('[data-saved-filter-clear]');
    if (clear) clear.hidden = !selection.size;
    const noMatches = container.querySelector('[data-saved-filter-no-matches]');
    if (noMatches) noMatches.hidden = Array.from(container.querySelectorAll('[data-saved-filter-option]')).some(option => !option.hidden);
}

function hasSavedCategoryFilter() {
    return availableAccountCategoryLookupReady
        && (tbSavedCategorySelections.size > 0 || !!savedFilterQuery('category').trim());
}

function savedCategoryFilterMatchesRow(row) {
    if (!availableAccountCategoryLookupReady) return true;
    const categoryValue = row.dataset.categoryFilter || '';
    if (tbSavedCategorySelections.size) return tbSavedCategorySelections.has(categoryValue);
    const query = savedFilterQuery('category').trim().toLocaleLowerCase();
    return !query || categoryValue.toLocaleLowerCase().includes(query);
}

function applySavedCategoryFilter() {
    if (!availableAccountsTableBody) return;
    const noteQuery = tbWorkflowMode === 'financial' && availableAccountNoteLookupReady
        ? savedFilterQuery('note').trim().toLocaleLowerCase() : '';
    const noteSelections = tbWorkflowMode === 'financial' && availableAccountNoteLookupReady ? tbSavedNoteSelections : new Set();
    const categoryFilterActive = hasSavedCategoryFilter();
    const noteFilterActive = noteSelections.size > 0 || !!noteQuery;
    const rows = availableAccountsTableBody.querySelectorAll('tr[data-category-filter]');
    let visibleCount = 0;
    rows.forEach(function (row) {
        const noteValue = String(row.dataset.noteFilter || '').toLocaleLowerCase();
        const categoryMatches = savedCategoryFilterMatchesRow(row);
        const noteMatches = noteSelections.size
            ? noteSelections.has(row.dataset.noteFilter || '')
            : (!noteQuery || noteValue.includes(noteQuery));
        const matches = categoryMatches && noteMatches;
        row.hidden = !matches;
        if (matches) visibleCount += 1;
    });
    if (tbSavedCategoryNoMatches) {
        tbSavedCategoryNoMatches.textContent = availableAccountLookupMessage || 'No Trial Balance accounts match these filters.';
        tbSavedCategoryNoMatches.hidden = availableAccountLookupMessage ? false : ((!categoryFilterActive && !noteFilterActive) || visibleCount > 0);
    }
    updateSavedDraftFilteredTotal();
    fitSavedTrialBalanceAmountColumns();
}

function sumSavedTrialBalanceValues(values) {
    const parsed = values.map(function (value) {
        const text = String(value ?? '0').replaceAll(',', '').trim();
        const match = /^([+-]?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(text);
        if (!match) return {units: 0n, scale: 0};
        const fraction = match[3] || '';
        const exponent = Number(match[4] || 0);
        let units = BigInt(match[2] + fraction);
        if (match[1] === '-') units = -units;
        return {units, scale: fraction.length - exponent};
    });
    const scale = Math.max(0, ...parsed.map(item => item.scale));
    const total = parsed.reduce(function (sum, item) {
        return sum + item.units * (10n ** BigInt(scale - item.scale));
    }, 0n);
    const negative = total < 0n;
    const digits = (negative ? -total : total).toString().padStart(scale + 1, '0');
    const amount = scale
        ? (negative ? '-' : '') + digits.slice(0, -scale) + '.' + digits.slice(-scale)
        : (negative ? '-' : '') + digits;
    return formatTrialBalanceControlAmount(amount, 2);
}

function updateSavedDraftFilteredTotal() {
    if (!availableAccountsTableBody) return;
    const heading = availableAccountsTableBody.querySelector('tr.tb-draft-heading');
    if (!heading) return;
    const categoryQuery = availableAccountCategoryLookupReady ? savedFilterQuery('category').trim() : '';
    const noteQuery = tbWorkflowMode === 'financial' && availableAccountNoteLookupReady ? savedFilterQuery('note').trim() : '';
    const hasActiveFilter = Boolean(categoryQuery || noteQuery || tbSavedCategorySelections.size
        || (tbWorkflowMode === 'financial' && tbSavedNoteSelections.size));
    if (!hasActiveFilter) {
        heading.classList.remove('tb-draft-filtered-total');
        heading.replaceChildren();
        const cell = document.createElement('td');
        cell.colSpan = 9 + savedAssignmentColumnCount();
        cell.textContent = 'Draft Trial Balance reconciliation';
        heading.appendChild(cell);
        return;
    }

    const matchingRows = Array.from(availableAccountsTableBody.querySelectorAll('tr[data-category-filter]'))
        .filter(row => !row.hidden);
    heading.classList.add('tb-draft-filtered-total');
    heading.replaceChildren();
    const label = document.createElement('td');
    label.colSpan = 3 + savedAssignmentColumnCount();
    label.textContent = 'Draft Trial Balance reconciliation · Filtered account totals';
    label.title = 'Totals include matching Trial Balance account rows only; separate adjustment rows are excluded.';
    heading.appendChild(label);

    ['opening_dr', 'opening_cr', 'period_dr', 'period_cr', 'closing_dr', 'closing_cr'].forEach(function (field) {
        const values = matchingRows.map(function (row) {
            const amounts = JSON.parse(row.dataset.tbAmounts || '[]');
            return amounts[['opening_dr', 'opening_cr', 'period_dr', 'period_cr', 'closing_dr', 'closing_cr'].indexOf(field)];
        });
        const cell = document.createElement('td');
        cell.className = 'tb-amount-cell';
        cell.textContent = sumSavedTrialBalanceValues(values);
        cell.title = 'Total of matching Trial Balance account rows only.';
        heading.appendChild(cell);
    });
}

async function loadAvailableUploadCategories(uploadId, clientId, loadToken) {
    if (!clientId) return;
    const mode = tbWorkflowMode;
    const workflowToken = ++tbWorkflowLoadToken;
    const isCurrent = () => workflowToken === tbWorkflowLoadToken && loadToken === availableAccountsLoadToken && mode === tbWorkflowMode && String(availableUploadsDropdown.value) === String(uploadId);
    if (mode !== 'financial') {
        try {
            const result = await AccountAssignments.read(mode === 'cashflow' ? 'cashflow' : 'mapping', clientId, uploadId, tbReadAssignmentJson);
            if (!isCurrent()) return;
            const categories = mode === 'cashflow' ? result.categories || [] : AccountAssignments.workstreams.map(item => ({id: item.value, label: item.label, is_active: true}));
            if (mode === 'cashflow') {
                tbCashflowAccounts = result.accounts || [];
                tbCashflowCategories = categories;
                tbCashflowLeaves = tbIndexCashflowLeaves(tbCashflowAccounts);
            } else {
                tbVerificationAccounts = result.accounts || [];
                tbVerificationLeaves = tbIndexCashflowLeaves(tbVerificationAccounts);
            }
            const labels = new Map(categories.map(item => [String(item.id), item.label]));
            availableAccountCategoryByCode = new Map(result.accounts.map(account => {
                const id = mode === 'cashflow' ? account.category_id : account.assignment && account.assignment.verification_workstream;
                return [AccountAssignments.normaliseCode(account.account_code), id == null || id === '' ? 'Unassigned' : labels.get(String(id)) || 'Category unavailable'];
            }));
            availableAccountCategoryLookupReady = true; availableAccountNoteLookupReady = false;
            availableAccountNoteByCode = new Map(); availableAccountLookupMessage = '';
            populateSavedCategoryOptions(categories);
        } catch (error) {
            if (!isCurrent()) return;
            availableAccountCategoryLookupReady = false;
            availableAccountLookupMessage = AccountAssignments.workflows[mode].label + ' assignments are unavailable. Trial Balance figures remain available.';
        }
        if (isCurrent()) finishSavedWorkflowLookup();
        return;
    }
    const requests = [
        AccountAssignments.read('mapping', clientId, uploadId, tbReadAssignmentJson),
        tbApiRequest(API_BASE + '/api/account-control-center/categories?client_id=' + encodeURIComponent(clientId) + '&category_type=financial_statement&include_inactive=true'),
        tbApiRequest(API_BASE + '/api/account-control-center/categories?client_id=' + encodeURIComponent(clientId) + '&category_type=note&include_inactive=true'),
    ];
    const responses = await Promise.allSettled(requests);
    if (!isCurrent()) return;

    let mapping = null;
    if (responses[0].status === 'fulfilled') {
        try {
            const result = responses[0].value;
            if ( String(result.client_id) === String(clientId)
                && result.upload && String(result.upload.id) === String(uploadId)
                && Array.isArray(result.accounts)) mapping = result;
        } catch (_error) { /* Keep ACC fields unavailable without blocking the Trial Balance. */ }
    }

    async function readCategories(responseResult) {
        if (responseResult.status !== 'fulfilled') return null;
        try {
            const response = responseResult.value;
            const result = await response.json();
            return response.ok && Array.isArray(result.categories) ? result.categories : null;
        } catch (_error) { return null; }
    }
    const [financialCategories, noteCategories] = await Promise.all([
        readCategories(responses[1]), readCategories(responses[2]),
    ]);
    if (!isCurrent()) return;

    if(mapping)tbSetFinancialAccounts(mapping.accounts);
    tbFsWorkspace.STATE.categories=financialCategories||[];tbFsWorkspace.STATE.notes=noteCategories||[];
    tbFsWorkspace.STATE.pickerCategoriesReady=Boolean(financialCategories);tbFsWorkspace.STATE.notesLoaded=Boolean(noteCategories);
    availableAccountCategoryLookupReady = Boolean(mapping && financialCategories);
    availableAccountNoteLookupReady = Boolean(mapping && noteCategories);
    if (availableAccountCategoryLookupReady) {
        const labelsById = new Map(financialCategories.map(category => [String(category.id), category.label]));
        availableAccountCategoryByCode = new Map(mapping.accounts.map(account => {
            const categoryId = account.assignment && account.assignment.financial_statement_category_id;
            return [String(account.account_code || '').trim(), categoryId == null ? 'Unassigned' : (labelsById.get(String(categoryId)) || 'Category unavailable')];
        }));
        populateSavedCategoryOptions(financialCategories);
    }
    if (availableAccountNoteLookupReady) {
        const labelsById = new Map(noteCategories.map(category => [String(category.id), category.label]));
        availableAccountNoteByCode = new Map(mapping.accounts.map(account => {
            const noteId = account.assignment && account.assignment.note_category_id;
            return [String(account.account_code || '').trim(), noteId == null ? 'Unassigned' : (labelsById.get(String(noteId)) || 'Note unavailable')];
        }));
        populateSavedNoteOptions(noteCategories);
    }
    if (tbSavedCategoryNoMatches) {
        const unavailable = [];
        if (!availableAccountCategoryLookupReady) unavailable.push('categories');
        if (!availableAccountNoteLookupReady) unavailable.push('notes');
        availableAccountLookupMessage = unavailable.length
            ? 'ACC ' + (unavailable.length === 2 ? 'categories and notes are' : unavailable[0] + ' are') + ' unavailable. Trial Balance figures remain available.'
            : '';
        tbSavedCategoryNoMatches.hidden = !availableAccountLookupMessage;
    }
    finishSavedWorkflowLookup();
}

// Fetches Phase 1's list-uploads endpoint and populates the dropdown. Only 'parsed'
// uploads are listed -- an 'error'/'pending' upload has no accounts to browse, so
// including it would just be a dead entry that returns an empty table on selection.
async function loadAvailableUploads(target = availableTrialBalancesBtn, requestedUploadId = null) {
    if (!availableUploadsDropdown) return;
    const requestToken = ++availableUploadsRequestToken;
    const loadingToken = startTrialBalanceLoading(target);

    availableUploadsDropdown.disabled = true;
    availableUploadsDropdown.innerHTML = '<option value="">Loading Trial Balances…</option>';
    if (availableUploadsEmptyHint) availableUploadsEmptyHint.style.display = 'none';
    if (availableAccountsTableWrapper) availableAccountsTableWrapper.style.display = 'none';
    if (tbSavedStickyHead) tbSavedStickyHead.hidden = true;
    if (availableAccountsTableWrapper && availableAccountsTableWrapper._updateFloatingScrollbar) availableAccountsTableWrapper._updateFloatingScrollbar();
    if (availableAccountsTableBody) availableAccountsTableBody.innerHTML = '';
    availableAccountsLoadToken += 1;
    tbWorkflowLoadToken++; tbWorkflowCache.clear(); clearSavedWorkflowStates();
    availableCategoryUploadId = null;
    availableAccountCategoryByCode = new Map();
    availableAccountCategoryLookupReady = false;
    availableAccountNoteByCode = new Map();
    availableAccountNoteLookupReady = false;
    availableAccountLookupMessage = '';
    setSavedFilterQuery('category', '');
    setSavedFilterQuery('note', '');
    setSavedFilterAvailability('category', true);
    setSavedFilterAvailability('note', true);
    if (tbSavedCategoryOptions) tbSavedCategoryOptions.replaceChildren();
    if (tbSavedNoteOptions) tbSavedNoteOptions.replaceChildren();
    if (tbSavedCategoryNoMatches) { tbSavedCategoryNoMatches.hidden = true; tbSavedCategoryNoMatches.textContent = 'No Trial Balance accounts match these filters.'; }
    if (deleteUploadBtn) deleteUploadBtn.style.display = 'none';
    syncSavedWorkflowColumns();
    closeSavedSetupMenu();

    let clientId = null;
    try {
        // The page can be used before the window load handler finishes waiting for the
        // session client; wait here too so an early click never silently skips the list.
        if (window.clientContextReady) await window.clientContextReady;
        if (requestToken !== availableUploadsRequestToken) return;

        clientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
        if (!clientId) throw new Error('No client is selected. Return to the dashboard and select a client first.');

        const url = API_BASE + '/api/trial-balance/uploads?client_id=' + encodeURIComponent(clientId);
        const res = await window.ApiUtils.apiFetch(url);
        const result = await res.json();

        // Repeated opens or a client switch can leave an older request in flight. Only
        // the newest request for the still-selected client may populate this dropdown.
        if (requestToken !== availableUploadsRequestToken
            || String(window.getSelectedClientId()) !== String(clientId)) return;

        if (!res.ok) {
            throw new Error(result.detail || 'Failed to load available trial balances');
        }
        if (!result || !Array.isArray(result.uploads)) {
            throw new Error('The server returned an invalid Trial Balance upload list.');
        }

        availableUploadsDropdown.innerHTML = '<option value="">— Select an upload —</option>';
        const parsedUploads = result.uploads.filter(function (u) {
            return String(u.status || '').trim().toLowerCase() === 'parsed';
        });

        if (parsedUploads.length === 0) {
            if (availableUploadsEmptyHint) availableUploadsEmptyHint.style.display = 'block';
            if (requestedUploadId) showStatus('The requested Trial Balance is not available for this client. Select another upload.', 'error');
            return;
        }

        parsedUploads.forEach(function (upload) {
            const option = document.createElement('option');
            option.value = upload.id;
            option.textContent = buildUploadLabel(upload);
            availableUploadsDropdown.appendChild(option);
        });
        if (requestedUploadId) {
            if (parsedUploads.some(function (upload) { return String(upload.id) === requestedUploadId; })) {
                availableUploadsDropdown.value = requestedUploadId;
                if (deleteUploadBtn) deleteUploadBtn.style.display = 'inline-block';
                await loadAvailableUploadAccounts(requestedUploadId);
            } else {
                showStatus('The requested Trial Balance is not available for this client. Select another upload.', 'error');
            }
        }
    } catch (error) {
        if (requestToken === availableUploadsRequestToken
            && (!clientId || String(window.getSelectedClientId()) === String(clientId))) {
            availableUploadsDropdown.innerHTML = '<option value="">— Select an upload —</option>';
            showStatus('❌ Could not load available trial balances: ' + error.message, 'error');
        }
    } finally {
        if (requestToken === availableUploadsRequestToken) availableUploadsDropdown.disabled = false;
        stopTrialBalanceLoading(loadingToken);
    }
}

// Fetches the existing accounts-list endpoint for the selected upload (unchanged --
// same endpoint the Review Classifications screen already uses) and renders it into
// this view's own table.
async function loadAvailableUploadAccounts(uploadId, target = availableUploadsDropdown) {
    if (!availableAccountsTableWrapper || !availableAccountsTableBody) return;
    const loadToken = ++availableAccountsLoadToken;
    const clientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
    const context = String(clientId) + ':' + String(uploadId);
    if (context !== tbWorkflowContext) { clearSavedWorkflowStates(); tbWorkflowContext = context; }
    tbWorkflowCache.clear();
    tbSetFinancialAccounts([],false);
    tbVerificationAccounts = []; tbVerificationLeaves = new Map();
    tbFsWorkspace.STATE.financialLoaded=false;tbFsWorkspace.STATE.pickerCategoriesReady=false;tbFsWorkspace.STATE.notesLoaded=false;
    if (window.AccountAssignments) AccountAssignments.invalidate(tbReadAssignmentJson);
    tbWorkflowLoadToken++;
    const previousUploadId = availableCategoryUploadId;
    if (previousUploadId !== String(uploadId)) {
        setSavedFilterQuery('category', '');
        setSavedFilterQuery('note', '');
        if (tbSavedCategoryNoMatches) tbSavedCategoryNoMatches.hidden = true;
        availableAccountsTableWrapper.style.display = 'none';
        if (tbSavedStickyHead) tbSavedStickyHead.hidden = true;
    }
    availableCategoryUploadId = String(uploadId);
    syncSavedWorkflowColumns();
    closeSavedSetupMenu();
    availableAccountCategoryByCode = new Map();
    availableAccountCategoryLookupReady = false;
    availableAccountNoteByCode = new Map();
    availableAccountNoteLookupReady = false;
    availableAccountLookupMessage = '';
    setSavedFilterAvailability('category', true);
    setSavedFilterAvailability('note', true);
    if (tbSavedCategoryOptions) tbSavedCategoryOptions.replaceChildren();
    if (tbSavedNoteOptions) tbSavedNoteOptions.replaceChildren();
    if (tbSavedCategoryNoMatches) { tbSavedCategoryNoMatches.hidden = true; tbSavedCategoryNoMatches.textContent = 'No Trial Balance accounts match these filters.'; }
    const loadingToken = startTrialBalanceLoading(target);

    try {
        const res = await tbApiRequest(API_BASE + '/api/trial-balance/accounts?upload_id=' + encodeURIComponent(uploadId));
        if (loadToken !== availableAccountsLoadToken || (availableUploadsDropdown && String(availableUploadsDropdown.value) !== String(uploadId))) return;
        const result = await res.json();
        if (loadToken !== availableAccountsLoadToken || (availableUploadsDropdown && String(availableUploadsDropdown.value) !== String(uploadId))) return;

        if (!res.ok) {
            throw new Error(result.detail || 'Failed to load accounts');
        }

        renderTrialBalanceControls(result.validation_controls || {}, 'tbSavedValidationControls');
        renderAvailableAccountsTable(result.accounts, (result.adjustments || []).concat(result.global_adjustments || []), result.validation_controls || {});
        availableAccountsTableWrapper.style.display = 'block';
        if (tbSavedStickyHead) tbSavedStickyHead.hidden = false;
        fitSavedTrialBalanceAmountColumns();
        restoreSavedWorkflowReturnState();
        syncSavedWorkflowColumns();
        applySavedCategoryFilter();
        loadAvailableUploadCategories(uploadId, clientId, loadToken);
        return true;
    } catch (error) {
        showStatus('❌ ' + error.message, 'error');
        return false;
    } finally {
        stopTrialBalanceLoading(loadingToken);
    }
}

// Saved account codes and classifications use the same deliberate editing gesture.
function renderAvailableAccountsTable(accounts, adjustments, controls = {}) {
    availableAccountsTableBody.innerHTML = '';

    const amountFields = ['opening_dr', 'opening_cr', 'period_dr', 'period_cr', 'closing_dr', 'closing_cr'];

    accounts.forEach(function (account) {
        const tr = document.createElement('tr');
        const categoryLabel = availableAccountCategoryLookupReady
            ? (availableAccountCategoryByCode.get(String(account.account_code || '').trim()) || 'Category unavailable')
            : 'Category unavailable';
        const noteLabel = availableAccountNoteLookupReady
            ? (availableAccountNoteByCode.get(String(account.account_code || '').trim()) || 'Note unavailable')
            : 'Note unavailable';
        tr.dataset.accountCode = String(account.account_code || '').trim();
        tr.dataset.categoryFilter = categoryLabel;
        tr.dataset.noteFilter = noteLabel;
        tr.dataset.tbAmounts = JSON.stringify(amountFields.map(field => account[field] ?? null));

        const tdCode = document.createElement('td');
        const display = document.createElement('span');
        display.className = 'tb-account-code-display';
        display.textContent = account.account_code || '';
        display.title = 'Double-click to edit' + (account.source_account_code && account.source_account_code !== account.account_code ? '\nImported: ' + account.source_account_code : '');
        tdCode.appendChild(display);
        display.addEventListener('dblclick', function () {
            const input = document.createElement('input');
            if (tbCodeSaveRunning || tbFsBusyCount > 0 || tbFsModalOpen || trialBalanceActionsInFlight.has('account-code-upload:' + availableUploadsDropdown.value)) return;
            input.type = 'text'; input.className = 'tb-code-review-input'; input.maxLength = 120;
            input.value = display.textContent || '';
            input.setAttribute('aria-label', 'Account code for ' + (account.account_name || account.account_code));
            tdCode.replaceChildren(input); input.focus(); input.select();
            input.addEventListener('keydown', function (event) {
                if (event.key === 'Escape') input.value = display.textContent || '';
                if (event.key === 'Enter' || event.key === 'Escape') input.blur();
            });
            input.addEventListener('blur', function () {
                tbStageAccountCode(account, input.value, display);
                tdCode.replaceChildren(display);
            }, { once: true });
        });
        tr.appendChild(tdCode);

        const tdName = document.createElement('td');
        tdName.textContent = account.account_name || '—';
        tr.appendChild(tdName);

        const tdClassification = document.createElement('td');
        tdClassification.appendChild(buildClassificationCellContent(account));
        tdClassification.title = 'Double-click to edit';
        tdClassification.addEventListener('dblclick', function () {
            if (tdClassification.querySelector('select')) return;
            const select = document.createElement('select');
            select.setAttribute('aria-label', 'Classification for ' + (account.account_name || account.account_code));
            [['leaf', 'Leaf'], ['parent', 'Parent']].forEach(function (item) {
                const option = document.createElement('option'); option.value = item[0]; option.textContent = item[1];
                select.appendChild(option);
            });
            const previous = account.is_leaf ? 'leaf' : 'parent';
            select.value = previous;
            tdClassification.replaceChildren(select); select.focus();
            select.addEventListener('keydown', function (event) {
                if (event.key === 'Escape') { select.value = previous; select.blur(); }
                else if (event.key === 'Enter') select.blur();
            });
            select.addEventListener('blur', function () {
                if (select.value === previous) { tdClassification.replaceChildren(buildClassificationCellContent(account)); return; }
                saveAvailableClassification(account, select.value === 'leaf', select).then(function (saved) {
                    if (!saved && tdClassification.contains(select)) tdClassification.replaceChildren(buildClassificationCellContent(account));
                });
            }, {once: true});
        });
        tr.appendChild(tdClassification);

        const tdCategory = document.createElement('td');
        tdCategory.className = 'tb-saved-category-cell';
        tdCategory.textContent = categoryLabel === 'Category unavailable' ? 'Unavailable' : categoryLabel;
        if (categoryLabel === 'Category unavailable') tdCategory.title = 'Account Control Centre category lookup could not be verified.';
        tr.appendChild(tdCategory);

        const tdNote = document.createElement('td');
        tdNote.className = 'tb-saved-note-cell';
        tdNote.textContent = noteLabel === 'Note unavailable' ? 'Unavailable' : noteLabel;
        if (noteLabel === 'Note unavailable') tdNote.title = 'Account Control Centre note lookup could not be verified.';
        tr.appendChild(tdNote);

        amountFields.forEach(function (field) {
            const tdAmount = document.createElement('td');
            tdAmount.className = 'tb-amount-cell';
            tdAmount.textContent = typeof window.formatAmount === 'function'
                ? window.formatAmount(account[field])
                : (account[field] === null || account[field] === undefined ? '—' : String(account[field]));
            tr.appendChild(tdAmount);
        });

        availableAccountsTableBody.appendChild(tr);
    });

    const savedAdjustments = Array.isArray(adjustments) ? adjustments : [];
    if (!savedAdjustments.length) {
        renderSavedDraftReconciliation(controls);
        return;
    }
    const sectionRow = document.createElement('tr');
    sectionRow.className = 'tb-difference-section';
    const sectionCell = document.createElement('td');
    sectionCell.colSpan = 11;
    sectionCell.textContent = `System-generated differences (${savedAdjustments.length}) — separate from original Trial Balance amounts.`;
    sectionRow.appendChild(sectionCell);
    availableAccountsTableBody.appendChild(sectionRow);

    savedAdjustments.forEach(function (adjustment) {
        const tr = document.createElement('tr');
        tr.className = 'tb-generated-difference';
        const values = [
            adjustment.account_code || '—',
            'Difference — ' + (adjustment.account_name || adjustment.account_code || 'Leaf account'),
            adjustment.global_adjustment ? 'Unassigned global difference' : 'Leaf adjustment',
            '',
            '',
            Number(adjustment.opening_dr_adjustment || 0) || '', Number(adjustment.opening_cr_adjustment || 0) || '',
            Number(adjustment.period_dr_adjustment || 0) || '', Number(adjustment.period_cr_adjustment || 0) || '',
            Number(adjustment.closing_dr_adjustment || 0) || '', Number(adjustment.closing_cr_adjustment || 0) || '',
        ];
        values.forEach(function (value, index) {
            const td = document.createElement('td');
            if (index >= 5 && index <= 10) td.className = 'tb-amount-cell';
            td.textContent = value === '' ? '' : (index >= 5 && index <= 10
                ? (typeof window.formatAmount === 'function' ? window.formatAmount(value) : String(value))
                : String(value));
            tr.appendChild(td);
        });
        availableAccountsTableBody.appendChild(tr);
    });
    renderSavedDraftReconciliation(controls);
}

function tbUpdateCodeCorrectionsBar() {
    const bar = document.getElementById('tbCodeCorrectionsBar');
    if (bar) bar.hidden = !tbPendingCodeCorrections.size;
    const label = document.getElementById('tbCodeCorrectionsCount');
    if (label) label.textContent = tbPendingCodeCorrections.size + ' unsaved code correction(s)';
    ['tbSaveCodeCorrections', 'tbDiscardCodeCorrections'].forEach(id => {
        const button = document.getElementById(id); if (button) button.disabled = tbCodeSaveRunning;
    });
}

function tbStageAccountCode(account, code, display) {
    if (tbCodeSaveRunning) return;
    const value = code.trim();
    if (value === account.account_code) tbPendingCodeCorrections.delete(account.id);
    else tbPendingCodeCorrections.set(account.id, {account, account_code:value, display});
    display.textContent = value;
    display.classList.toggle('tb-code-pending', value !== account.account_code);
    tbUpdateCodeCorrectionsBar();
}

function tbDiscardAccountCodes() {
    tbPendingCodeCorrections.forEach(item => {
        item.display.textContent = item.account.account_code;
        item.display.classList.remove('tb-code-pending');
    });
    tbPendingCodeCorrections.clear();
    tbUpdateCodeCorrectionsBar();
}

async function tbSaveAccountCodes() {
    if (tbCodeSaveRunning || tbFsBusyCount > 0 || tbFsModalOpen || trialBalanceActionsInFlight.has('account-code-upload:' + availableUploadsDropdown.value)) return false;
    if (!tbPendingCodeCorrections.size) return true;
    const uploadId = String(availableUploadsDropdown.value);
    const clientId = window.getSelectedClientId();
    const corrections = [...tbPendingCodeCorrections.values()].map(item => ({
        account_id:item.account.id, account_code:item.account_code, expected_account_code:item.account.account_code
    }));
    if (corrections.some(item => !item.account_code || item.account_code.length > 120)
        || new Set(corrections.map(item => item.account_code)).size !== corrections.length) {
        showStatus('Enter distinct, nonblank account codes of up to 120 characters.', 'error'); return false;
    }
    const sourceControls = [availableUploadsDropdown, tbAssignmentWorkflow].filter(Boolean).map(control => [control, control.disabled]);
    sourceControls.forEach(([control]) => { control.disabled = true; });
    tbCodeSaveRunning = true; tbUpdateCodeCorrectionsBar();
    const token = startTrialBalanceLoading(document.getElementById('tbSaveCodeCorrections'));
    let saved = false;
    try {
        const response = await tbApiRequest(API_BASE + '/api/trial-balance/uploads/' + encodeURIComponent(uploadId) + '/account-codes', {
            method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({corrections})
        });
        const result = await response.json();
        if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not save account codes.');
        if (String(result.upload_id) !== uploadId) throw new Error('Save acknowledgement could not be verified. Reload before retrying.');
        saved = true;
        tbPendingCodeCorrections.clear(); tbUpdateCodeCorrectionsBar();
        if (String(window.getSelectedClientId()) !== String(clientId) || String(availableUploadsDropdown.value) !== uploadId) return true;
        const refreshed = await loadAvailableUploadAccounts(uploadId);
        showStatus(refreshed ? 'Account code corrections saved. Hierarchy refreshed.' : 'Corrections saved, but refresh failed. Reload this Trial Balance.', refreshed ? 'success' : 'error');
        if (String(currentUploadId) === uploadId) await loadSummary(currentUploadId);
        return true;
    } catch (error) {
        showStatus((saved ? 'Corrections saved; refresh failed. ' : '') + error.message, 'error');
        return saved;
    } finally {
        sourceControls.forEach(([control, disabled]) => { control.disabled = disabled; });
        tbCodeSaveRunning = false; tbUpdateCodeCorrectionsBar(); stopTrialBalanceLoading(token);
    }
}

function tbAskAboutAccountCodes() {
    if (tbCodeDecisionPending) return tbCodeDecisionPending;
    const dialog = document.getElementById('tbCodeCorrectionsDialog');
    if (!dialog) return Promise.resolve('stay');
    tbCodeDecisionPending = new Promise(resolve => {
        const finish = value => {
            dialog.close(); dialog.removeEventListener('cancel', cancel);
            listeners.forEach(([button, listener]) => button.removeEventListener('click', listener));
            tbCodeDecisionPending = null; resolve(value);
        };
        const cancel = event => { event.preventDefault(); finish('stay'); };
        dialog.addEventListener('cancel', cancel);
        const listeners = [['tbCodeStay','stay'],['tbCodeDiscard','discard'],['tbCodeSave','save']].map(([id, value]) => {
            const button = document.getElementById(id), listener = () => finish(value);
            button.addEventListener('click', listener); return [button, listener];
        });
        dialog.showModal();
    });
    return tbCodeDecisionPending;
}

// Gate page actions, while moving between Code cells remains immediate. Browser
// Back/refresh/close uses its native unsaved-change warning (cannot await a save).
async function tbGuardAccountCodeAction(event) {
    if (tbCodeGuardResuming || (!tbPendingCodeCorrections.size && !tbCodeSaveRunning)) return;
    const target = event.target.closest('a,button,input,select,td');
    if (!target || target.closest('#tbCodeCorrectionsBar,#tbCodeCorrectionsDialog')) return;
    if (target.closest('#availableAccountsTableBody td:first-child')) return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (tbCodeSaveRunning || tbCodeDecisionPending) return;
    const decision = await tbAskAboutAccountCodes();
    if (decision === 'stay') return;
    if (decision === 'save' && !await tbSaveAccountCodes()) return;
    if (decision === 'discard') tbDiscardAccountCodes();
    tbCodeGuardResuming = true;
    try {
        if (target.matches('a,button')) target.click();
        else target.focus();
    } finally { tbCodeGuardResuming = false; }
}
document.addEventListener('click', event => {
    if (!tbImportCodeChangesPending || !selectedFile || !Object.keys(accountCodeCorrections).length) return;
    const target = event.target.closest('a,button');
    if (!target || target.closest('#uploadViewContainer,#tbValidationSheet,#availableTrialBalancesSection')) return;
    if (!window.confirm('Leave this Trial Balance review? Your code corrections have not been imported.')) {
        event.preventDefault(); event.stopImmediatePropagation();
    }
}, true);
document.addEventListener('click', tbGuardAccountCodeAction, true);
document.addEventListener('dblclick', tbGuardAccountCodeAction, true);
document.addEventListener('focusin', event => {
    if (event.target.matches('input,select')) tbGuardAccountCodeAction(event);
}, true);
// Keyboard/native select changes need the same gate as pointer actions.
document.addEventListener('change', async event => {
    if (tbCodeGuardResuming || !tbPendingCodeCorrections.size) return;
    const select = event.target;
    if (select !== availableUploadsDropdown && select !== tbAssignmentWorkflow) return;
    const wanted = select.value;
    select.value = select === availableUploadsDropdown ? availableCategoryUploadId : tbWorkflowMode;
    event.stopImmediatePropagation();
    if (tbCodeDecisionPending || tbCodeSaveRunning) return;
    const decision = await tbAskAboutAccountCodes();
    if (decision === 'stay' || (decision === 'save' && !await tbSaveAccountCodes())) return;
    if (decision === 'discard') tbDiscardAccountCodes();
    tbCodeGuardResuming = true;
    try { select.value = wanted; select.dispatchEvent(new Event('change', {bubbles:true})); }
    finally { tbCodeGuardResuming = false; }
}, true);
window.addEventListener('beforeunload', event => {
    if (tbPendingCodeCorrections.size || tbCodeSaveRunning || (selectedFile && tbImportCodeChangesPending && Object.keys(accountCodeCorrections).length)) { event.preventDefault(); event.returnValue = ''; }
});
document.getElementById('tbSaveCodeCorrections')?.addEventListener('click', tbSaveAccountCodes);
document.getElementById('tbDiscardCodeCorrections')?.addEventListener('click', tbDiscardAccountCodes);

// Reload saved controls after a classification change so stale reconciliation is visible.
async function saveAvailableClassification(account, isLeaf, trigger) {
    const uploadId = availableUploadsDropdown ? availableUploadsDropdown.value : null;
    const actionKey = 'account-code-upload:' + uploadId;
    if (!uploadId || trialBalanceActionsInFlight.has(actionKey)) return false;
    trialBalanceActionsInFlight.add(actionKey);
    const loadingToken = startTrialBalanceLoading(trigger);
    trigger.disabled = true;
    try {
        const response = await tbApiRequest(API_BASE + '/api/trial-balance/accounts/' + account.id + '/override', {
            method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({is_leaf: isLeaf})
        });
        const result = await response.json();
        if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not save classification');
        showStatus('✅ Classification saved for this upload.', 'success');
        await loadAvailableUploadAccounts(uploadId, trigger);
        if (String(currentUploadId) === String(uploadId)) await loadSummary(uploadId);
        return true;
    } catch (error) {
        showStatus('❌ ' + error.message, 'error');
        return false;
    } finally {
        trigger.disabled = false;
        stopTrialBalanceLoading(loadingToken);
        trialBalanceActionsInFlight.delete(actionKey);
    }
}

// Match the saved heading and body widths to the widest rendered amount, including footings.
function fitSavedTrialBalanceAmountColumns() {
    if (!availableAccountsTableWrapper || !availableAccountsTableBody || !tbSavedStickyHead) return;
    const table = availableAccountsTableWrapper.querySelector('table');
    const heading = tbSavedStickyHead.querySelector('table');
    if (!table || !heading) return;
    const cells = availableAccountsTableBody.querySelectorAll('td.tb-amount-cell');
    const context = document.createElement('canvas').getContext('2d');
    let amountWidth = 104;
    if (context && cells.length) {
        const style = getComputedStyle(cells[0]);
        context.font = '700 ' + style.fontSize + ' ' + style.fontFamily;
        new Set(Array.from(cells, cell => cell.textContent)).forEach(function (value) {
            amountWidth = Math.max(amountWidth, Math.ceil(context.measureText(value).width) + 24);
        });
    }
    syncSavedWorkflowColumns();
    const widths = [145, 220, 110, 180, ...(tbWorkflowMode === 'financial' ? [190] : []), ...Array(6).fill(amountWidth)];
    [table, heading].forEach(function (target) {
        const group = document.createElement('colgroup');
        widths.forEach(function (width) { const col = document.createElement('col'); col.style.width = width + 'px'; group.appendChild(col); });
        const existing = target.querySelector('colgroup'); if (existing) existing.remove();
        target.prepend(group);
        target.style.minWidth = Math.max(1240, widths.reduce((sum, width) => sum + width, 0)) + 'px';
    });
    heading.style.transform = `translateX(-${availableAccountsTableWrapper.scrollLeft}px)`;
    if (availableAccountsTableWrapper._updateFloatingScrollbar) availableAccountsTableWrapper._updateFloatingScrollbar();
}

function clearSelectedAvailableUploadView() {
    availableAccountsLoadToken += 1;
    tbWorkflowLoadToken++; tbWorkflowCache.clear(); clearSavedWorkflowStates();
    availableCategoryUploadId = null;
    availableAccountCategoryByCode = new Map();
    availableAccountCategoryLookupReady = false;
    availableAccountNoteByCode = new Map();
    availableAccountNoteLookupReady = false;
    availableAccountLookupMessage = '';
    setSavedFilterQuery('category', '');
    setSavedFilterQuery('note', '');
    setSavedFilterAvailability('category', true);
    setSavedFilterAvailability('note', true);
    if (tbSavedCategoryOptions) tbSavedCategoryOptions.replaceChildren();
    if (tbSavedNoteOptions) tbSavedNoteOptions.replaceChildren();
    if (tbSavedCategoryNoMatches) { tbSavedCategoryNoMatches.hidden = true; tbSavedCategoryNoMatches.textContent = 'No Trial Balance accounts match these filters.'; }
    if (availableAccountsTableWrapper) availableAccountsTableWrapper.style.display = 'none';
    if (tbSavedStickyHead) tbSavedStickyHead.hidden = true;
    if (availableAccountsTableWrapper && availableAccountsTableWrapper._updateFloatingScrollbar) availableAccountsTableWrapper._updateFloatingScrollbar();
    if (availableAccountsTableBody) availableAccountsTableBody.innerHTML = '';
    if (deleteUploadBtn) deleteUploadBtn.style.display = 'none';
    syncSavedWorkflowColumns();
    closeSavedSetupMenu();
}

if (availableUploadsDropdown) {
    availableUploadsDropdown.addEventListener('change', function () {
        clearSelectedAvailableUploadView();
        if (this.value) {
            if (deleteUploadBtn) deleteUploadBtn.style.display = 'inline-block';
            loadAvailableUploadAccounts(this.value, this);
        }
    });
}

function openSavedFilterOptions(kind) {
    const picker = (kind === 'note' ? tbSavedNoteTrigger : tbSavedCategoryTrigger)?.closest('[data-fs-picker]');
    if (!picker || !tbFsWorkspace) return;
    tbFsWorkspace.accOpenFinancialPicker(picker);
    const input = savedFilterInput(kind);
    if (input) {
        input.value = savedFilterQuery(kind);
        updateSavedFilterOptions(kind);
        tbFsWorkspace.accPositionFinancialPicker(picker);
    }
}
function closeSavedFilterOptions() {
    if (tbFsWorkspace) {
        [tbSavedCategoryTrigger, tbSavedNoteTrigger].forEach(trigger => {
            const picker = trigger && trigger.closest('[data-fs-picker]');
            if (picker) tbFsWorkspace.accCloseFinancialPicker(picker, false);
        });
    } else {
        [tbSavedCategoryTrigger, tbSavedNoteTrigger].forEach(trigger => {
            const picker = trigger && trigger.closest('[data-fs-picker]');
            if (!picker) return;
            picker.classList.remove('is-open');
            const menu = picker.querySelector('[data-fs-picker-menu]');
            if (menu) menu.hidden = true;
            trigger.setAttribute('aria-expanded', 'false');
        });
    }
}
[[tbSavedCategoryTrigger, 'category'], [tbSavedNoteTrigger, 'note']].forEach(function ([trigger, kind]) {
    if (!trigger) return;
    trigger.addEventListener('click', function () {
        if (trigger.getAttribute('aria-expanded') === 'true') closeSavedFilterOptions();
        else openSavedFilterOptions(kind);
    });
});
[[tbSavedCategorySearch, 'category'], [tbSavedNoteSearch, 'note']].forEach(function ([input, kind]) {
    if (!input) return;
    input.addEventListener('input', function () {
        input.dataset.savedFilterQuery = input.value;
        updateSavedFilterOptions(kind);
        syncSavedFilterDisplay(kind);
        if (kind === 'category') syncSavedFilterOptionsFromRows('note');
        applySavedCategoryFilter();
        rememberSavedWorkflow();
    });
});
document.addEventListener('click', function (event) {
    const clear = event.target.closest('[data-saved-filter-clear]');
    if (clear) {
        const kind = clear.dataset.savedFilterClear;
        (kind === 'note' ? tbSavedNoteSelections : tbSavedCategorySelections).clear();
        if (kind === 'category') syncSavedFilterOptionsFromRows('note');
        updateSavedFilterOptions(kind);
        syncSavedFilterDisplay(kind);
        applySavedCategoryFilter();
        rememberSavedWorkflow();
        return;
    }
    const option = event.target.closest('[data-saved-filter-option]');
    if (option) {
        const kind = option.dataset.savedFilterKind;
        const selection = kind === 'note' ? tbSavedNoteSelections : tbSavedCategorySelections;
        const value = option.dataset.savedFilterValue || '';
        if (selection.has(value)) selection.delete(value); else selection.add(value);
        if (kind === 'category') syncSavedFilterOptionsFromRows('note');
        updateSavedFilterOptions(kind);
        syncSavedFilterDisplay(kind);
        applySavedCategoryFilter();
        rememberSavedWorkflow();
        savedFilterInput(kind)?.focus();
        return;
    }
    if (!event.target.closest('[data-fs-picker]')) closeSavedFilterOptions();
});
document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
        const open = document.querySelector('.tb-saved-filter-picker.is-open');
        if (open) {
            const trigger = open.querySelector('[data-fs-picker-trigger]');
            closeSavedFilterOptions();
            trigger?.focus();
        }
    }
});

// Confirmed fix: deletes the currently selected upload (and all its accounts) after
// a native confirm() -- same single-flat-destructive-action pattern this file already
// uses for the Section 2/Phase 6 overwrite confirmation. On success, refreshes the
// dropdown/table so the deleted upload disappears from view.
if (deleteUploadBtn) {
    deleteUploadBtn.addEventListener('click', async function () {
        const uploadId = availableUploadsDropdown ? availableUploadsDropdown.value : null;
        if (!uploadId) return;

        const confirmed = window.confirm('Delete this Trial Balance and all its accounts? This cannot be undone.');
        if (!confirmed) return;

        if (trialBalanceActionsInFlight.has('delete-upload:' + uploadId)) return;
        trialBalanceActionsInFlight.add('delete-upload:' + uploadId);
        const loadingToken = startTrialBalanceLoading(deleteUploadBtn);
        const previousDeleteDisabled = deleteUploadBtn.disabled;
        const previousDropdownDisabled = availableUploadsDropdown ? availableUploadsDropdown.disabled : false;
        deleteUploadBtn.disabled = true;
        if (availableUploadsDropdown) availableUploadsDropdown.disabled = true;

        try {
            const res = await tbApiRequest(API_BASE + '/api/trial-balance/uploads/' + uploadId, {
                method: 'DELETE'
            });
            const result = await res.json();

            if (!res.ok) {
                throw new Error(result.detail || 'Failed to delete trial balance upload');
            }

            showStatus('✅ Trial balance upload deleted.', 'success');

            // Reset this view's selection/table state, then reload the dropdown so
            // the deleted upload is gone from the list.
            if (availableAccountsTableWrapper) availableAccountsTableWrapper.style.display = 'none';
            if (tbSavedStickyHead) tbSavedStickyHead.hidden = true;
            if (availableAccountsTableWrapper && availableAccountsTableWrapper._updateFloatingScrollbar) availableAccountsTableWrapper._updateFloatingScrollbar();
            if (availableAccountsTableBody) availableAccountsTableBody.innerHTML = '';
            deleteUploadBtn.style.display = 'none';
            await loadAvailableUploads();
        } catch (error) {
            showStatus('❌ ' + error.message, 'error');
        } finally {
            deleteUploadBtn.disabled = previousDeleteDisabled;
            if (availableUploadsDropdown) availableUploadsDropdown.disabled = previousDropdownDisabled;
            stopTrialBalanceLoading(loadingToken);
            trialBalanceActionsInFlight.delete('delete-upload:' + uploadId);
        }
    });
}

// Opens the browsing view: hides the normal upload flow (untouched underneath) and
// shows this section, fetching the upload list fresh every time it's opened.
if (availableTrialBalancesBtn) {
    availableTrialBalancesBtn.addEventListener('click', function () {
        if (uploadViewContainer) uploadViewContainer.style.display = 'none';
        if (availableTrialBalancesSection) availableTrialBalancesSection.style.display = 'block';
        loadAvailableUploads(this);
    });
}

const tbAssignCategoriesBtn = getElement('tbAssignCategoriesBtn');
if (tbAssignCategoriesBtn) {
    tbAssignCategoriesBtn.addEventListener('click', function () {
        if (tbWorkflowMode === 'financial') { tbOpenFinancialSetup('financial'); return; }
        if (tbWorkflowMode === 'verification') { tbOpenVerificationSetup(); return; }
        tbOpenCashflowSetup();
    });
}

// Back affordance: restores the normal upload flow exactly as it was left (nothing in
// uploadViewContainer was touched while hidden) and resets this view's own dropdown/table
// so the next open starts clean rather than showing a stale selection.
if (closeAvailableTrialBalancesBtn) {
    closeAvailableTrialBalancesBtn.addEventListener('click', function () {
        const savedStatus = getElement('tbSavedStatus');
        if (savedStatus) savedStatus.style.display = 'none';
        if (availableTrialBalancesSection) availableTrialBalancesSection.style.display = 'none';
        if (uploadViewContainer) uploadViewContainer.style.display = 'block';
        if (availableUploadsDropdown) availableUploadsDropdown.innerHTML = '<option value="">— Select an upload —</option>';
        if (availableAccountsTableWrapper) availableAccountsTableWrapper.style.display = 'none';
        if (tbSavedStickyHead) tbSavedStickyHead.hidden = true;
        if (availableAccountsTableWrapper && availableAccountsTableWrapper._updateFloatingScrollbar) availableAccountsTableWrapper._updateFloatingScrollbar();
        if (availableAccountsTableBody) availableAccountsTableBody.innerHTML = '';
        availableAccountsLoadToken += 1;
        tbWorkflowLoadToken++; tbWorkflowCache.clear(); clearSavedWorkflowStates();
        availableCategoryUploadId = null;
        availableAccountCategoryByCode = new Map();
        availableAccountCategoryLookupReady = false;
        availableAccountNoteByCode = new Map();
        availableAccountNoteLookupReady = false;
        availableAccountLookupMessage = '';
        setSavedFilterQuery('category', '');
        setSavedFilterQuery('note', '');
        setSavedFilterAvailability('category', true);
        setSavedFilterAvailability('note', true);
        if (tbSavedCategoryOptions) tbSavedCategoryOptions.replaceChildren();
        if (tbSavedNoteOptions) tbSavedNoteOptions.replaceChildren();
        if (tbSavedCategoryNoMatches) { tbSavedCategoryNoMatches.hidden = true; tbSavedCategoryNoMatches.textContent = 'No Trial Balance accounts match these filters.'; }
        if (availableUploadsEmptyHint) availableUploadsEmptyHint.style.display = 'none';
        if (deleteUploadBtn) deleteUploadBtn.style.display = 'none';
        closeSavedSetupMenu();
    });
}

// ---------- INIT ----------
window.onload = async function () {
    // client_context.js populates the session-backed client cache from an async fetch. window.onload
    // is not a safe barrier for that (see the ready-signal note in client_context.js) -- without this
    // await, getSelectedClientId() returns null here and the page shows its "no client selected"
    // warning with Upload permanently disabled, despite a valid session.
    if (window.clientContextReady) await window.clientContextReady;
    if (typeof window.initUtils === 'function') await window.initUtils();
    updateClientDependentUI();
    tbFsIsAdmin=typeof window.isAdminUser==='function'?await window.isAdminUser():false;
    syncSavedWorkflowColumns();
    const params = new URLSearchParams(window.location.search);
    if (params.get('view') === 'available' && availableTrialBalancesSection) {
        if (uploadViewContainer) uploadViewContainer.style.display = 'none';
        availableTrialBalancesSection.style.display = 'block';
        await loadAvailableUploads(availableTrialBalancesBtn, params.get('upload_id'));
        if (availableUploadsDropdown && availableUploadsDropdown.value && params.get('setup')) {
            await tbOpenLegacySetup(params.get('setup'));
        }
    }
};

// Keep the saved-control warning visible if account selection changes after saving.
function renderTrialBalanceControls(result, targetId) {
    const container = getElement(targetId || 'tbValidationControls');
    if (!container) return;
    container.replaceChildren();
    if (result.stale) {
        container.textContent = 'Account codes or leaf selection changed after validation. These are the original saved differences; re-import the file to validate the revised selection.';
        return;
    }
}

// Total-row selection belongs to the current workbook, never the saved client mapping.
function checkTotalRowChoice(showError) {
    const noTotal = !!(tbNoTotalRow && tbNoTotalRow.checked);
    const raw = tbTotalExcelRow ? tbTotalExcelRow.value.trim() : '';
    const rowNumber = Number(raw);
    const hasRow = /^[0-9]+$/.test(raw) && rowNumber >= 2 && rowNumber <= fileAllRows.length + 1;
    const source = hasRow ? fileAllRows[rowNumber - 2] : null;
    const amountFields = TB_MAPPING_FIELDS.filter(function (field) { return /_(dr|cr)_column$/.test(field.role); });
    const hasAmounts = source && amountFields.some(function (field) {
        const index = fileHeaders.indexOf(tbColumnMapping[field.role]);
        return index >= 0 && String(source[index] ?? '').trim() !== '';
    });
    const valid = noTotal || (hasRow && !!hasAmounts);
    const error = getElement('tbTotalRowError');
    if (error) {
        if (valid) error.hidden = true;
        else if (showError !== undefined) error.hidden = !showError;
        error.textContent = !hasRow ? 'Enter a whole Excel row number from 2 to ' + (fileAllRows.length + 1) + ', or select No total row.'
            : 'The selected row has no mapped amounts. Check its Excel row number.';
    }
    if (tbTotalExcelRow) tbTotalExcelRow.setAttribute('aria-invalid', String(!!showError && !valid));
    return valid;
}
function appendTotalRowChoice(formData) {
    const noTotal = !!(tbNoTotalRow && tbNoTotalRow.checked);
    formData.append('no_total_row', noTotal ? 'true' : 'false');
    if (!noTotal && tbTotalExcelRow) formData.append('total_excel_row', tbTotalExcelRow.value.trim());
}
function renderTotalRowPreview() {
    if (!tbTotalExcelRow || !tbNoTotalRow) return;
    tbTotalExcelRow.disabled = tbNoTotalRow.checked;
    tbTotalExcelRow.required = !tbNoTotalRow.checked;
    tbTotalExcelRow.max = String(fileAllRows.length + 1);
    const valid = checkTotalRowChoice();
    const status = getElement('tbTotalRowStatus');
    if (status) status.textContent = tbNoTotalRow.checked ? 'N/A' : valid ? 'Selected' : 'Required';
    const preview = getElement('tbTotalRowPreview');
    const amounts = getElement('tbTotalRowAmounts');
    if (!preview || !amounts) return;
    preview.hidden = tbNoTotalRow.checked || !valid;
    amounts.replaceChildren();
    if (preview.hidden) return;
    const source = fileAllRows[Number(tbTotalExcelRow.value) - 2];
    const code = source[fileHeaders.indexOf(tbColumnMapping.account_code_column)] || '';
    const name = source[fileHeaders.indexOf(tbColumnMapping.account_name_column)] || '';
    getElement('tbTotalRowPreviewLabel').textContent = 'Row ' + tbTotalExcelRow.value + ' · ' + (name || code || 'Total');
    ['opening', 'period', 'closing'].forEach(function (period) {
        const group = document.createElement('div');
        const title = document.createElement('strong');
        title.textContent = period === 'period' ? 'Period' : period.charAt(0).toUpperCase() + period.slice(1);
        group.appendChild(title);
        ['dr', 'cr'].forEach(function (side) {
            const line = document.createElement('span');
            const value = source[fileHeaders.indexOf(tbColumnMapping[period + '_' + side + '_column'])];
            line.textContent = side === 'dr' ? 'Dr ' : 'Cr ';
            line.textContent += value === undefined || value === null || String(value).trim() === '' ? '—' : String(value);
            group.appendChild(line);
        });
        amounts.appendChild(group);
    });
}
function totalRowChoiceChanged() {
    invalidateTrialBalanceValidation('Total row selection changed. Revalidate before saving.');
    renderTotalRowPreview();
    checkTotalRowChoice(true);
    updateClientDependentUI();
}
if (tbTotalExcelRow) tbTotalExcelRow.addEventListener('input', totalRowChoiceChanged);
if (tbNoTotalRow) tbNoTotalRow.addEventListener('change', totalRowChoiceChanged);

// Source strings stay exact. Validation uses whole numbers; saved controls retain
// their existing cents display. Round once with integer arithmetic at either scale.
function formatTrialBalanceControlAmount(value, decimalPlaces = 2) {
    const text = String(value ?? '').replaceAll(',', '').trim();
    const match = /^([+-]?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(text);
    if (!match) return text || '-';
    const fraction = match[3] || '';
    const digits = match[2] + fraction;
    const scale = decimalPlaces + Number(match[4] || 0) - fraction.length;
    let units;
    if (scale >= 0) {
        units = BigInt(digits) * (10n ** BigInt(scale));
    } else {
        const cut = digits.length + scale;
        units = BigInt(cut > 0 ? digits.slice(0, cut) : '0');
        if (cut >= 0 && digits[cut] >= '5') units += 1n;
    }
    const rounded = units.toString().padStart(decimalPlaces + 1, '0');
    const whole = (decimalPlaces ? rounded.slice(0, -decimalPlaces) : rounded).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return (match[1] === '-' && units !== 0n ? '-' : '') + whole + (decimalPlaces ? '.' + rounded.slice(-decimalPlaces) : '');
}

// Measure all displayed amounts, including bold footing totals, once per render.
// Identical colgroups keep the separate sticky heading and body aligned. The
// issues column takes spare screen space; amount columns stay compact and equal.
function fitTrialBalanceValidationAmountColumns(columns) {
    const table = tbValidationBody.closest('.tb-validation-table');
    if (!table) return;
    const cells = tbValidationBody.querySelectorAll('td.tb-validation-amount');
    const context = document.createElement('canvas').getContext('2d');
    let amountWidth = 104;
    if (context && cells.length) {
        const style = getComputedStyle(cells[0]);
        context.font = '700 ' + style.fontSize + ' ' + style.fontFamily;
        new Set(Array.from(cells, cell => cell.textContent)).forEach(function (text) {
            amountWidth = Math.max(amountWidth, Math.ceil(context.measureText(text).width) + 24);
        });
    }
    const widths = {row: 58, classification: 110, issues: 280};
    const columnWidths = columns.map(function (column) {
        if (column.className.includes('tb-validation-amount')) return amountWidth;
        if (column.className.includes('tb-validation-account-code')) return 145;
        if (column.className.includes('tb-validation-account-name')) return 220;
        return widths[column.kind];
    });
    [table, tbValidationStickyHead && tbValidationStickyHead.querySelector('table')].filter(Boolean).forEach(function (target) {
        const group = document.createElement('colgroup');
        columns.forEach(function (column, index) {
            const col = document.createElement('col');
            if (column.kind !== 'issues') col.style.width = columnWidths[index] + 'px';
            group.appendChild(col);
        });
        const existing = target.querySelector('colgroup');
        if (existing) existing.remove();
        target.prepend(group);
        target.style.minWidth = columnWidths.reduce((total, width) => total + width, 0) + 'px';
    });
    syncTrialBalanceStickyHeader();
}
function renderTrialBalanceControlFooter(columns, mappingRoleByIndex) {
    const result = validationResult;
    if (!result) return;
    const sourceRows = result.source_total_rows || [];
    if (!sourceRows.length && !result.no_total_row) return;
    const labels = {
        opening_dr_column: 'Opening Dr', opening_cr_column: 'Opening Cr',
        period_dr_column: 'Period Dr', period_cr_column: 'Period Cr',
        closing_dr_column: 'Closing Dr', closing_cr_column: 'Closing Cr'
    };
    const comparison = result.source_total_comparison || [];
    const byField = new Map(comparison.map(function (entry) { return [entry.field, entry]; }));
    const source = sourceRows.length ? fileAllRows[sourceRows[0] - 2] || [] : [];
    const accountCheck = (result.checks || []).find(function (check) { return check.key === 'account_codes'; });
    const leafTotalsReady = accountCheck && accountCheck.status === 'pass';
    const section = document.createElement('tr');
    section.className = 'tb-control-heading';
    const heading = document.createElement('td');
    heading.colSpan = columns.length;
    heading.textContent = 'ERP control total';
    section.appendChild(heading);
    tbValidationBody.appendChild(section);
    [
        {label: 'Original leaf totals', kind: 'leaf'},
        {label: 'Client ERP total', kind: 'erp'},
        {label: 'Footing difference', kind: 'difference'}
    ].forEach(function (definition) {
        const tr = document.createElement('tr');
        tr.className = 'tb-control-total tb-control-' + definition.kind;
        columns.forEach(function (column) {
            const cell = document.createElement('td');
            cell.className = column.className;
            if (column.kind === 'row' && definition.kind === 'erp') {
                cell.textContent = sourceRows.length ? String(sourceRows[0]) : '';
            } else if (column.kind === 'source') {
                const role = mappingRoleByIndex.get(column.index);
                if (role === 'account_name_column') cell.textContent = definition.label;
                else if (labels[role]) {
                    cell.classList.add('tb-validation-amount');
                    const entry = byField.get(labels[role]);
                    let value;
                    if (definition.kind === 'leaf') value = leafTotalsReady ? (result.source_leaf_totals || {})[role] : 'Pending';
                    else if (definition.kind === 'erp') value = result.no_total_row ? 'N/A' : entry ? entry.source_total : source[column.index];
                    else value = result.no_total_row ? 'N/A' : entry ? entry.difference : 'Pending';
                    cell.title = 'Unrounded amount: ' + String(value ?? '');
                    if (definition.kind === 'difference' && entry) {
                        // New responses supply the authoritative Decimal comparison. The
                        // fallback supports older responses without changing their data.
                        const withinTolerance = typeof entry.within_tolerance === 'boolean'
                            ? entry.within_tolerance
                            : Math.abs(Number(entry.difference)) <= Number(result.tolerance ?? 0.01);
                        const rounded = formatTrialBalanceControlAmount(value, 0);
                        cell.textContent = withinTolerance ? '0' : rounded === '0' ? '<1' : rounded;
                        cell.classList.add(withinTolerance ? 'tb-footing-zero' : 'tb-footing-nonzero');
                        cell.title = 'Unrounded difference: ' + String(value) + '; tolerance: ' + String(result.tolerance ?? 0.01);
                    } else {
                        cell.textContent = formatTrialBalanceControlAmount(value, 0);
                    }
                }
            } else if (column.kind === 'classification') {
                cell.textContent = definition.kind === 'leaf' ? 'Leaf rows only' : 'Control';
            } else if (column.kind === 'issues') {
                if (definition.kind === 'leaf') cell.textContent = 'Original amounts before adjustments';
                else if (result.no_total_row) cell.textContent = 'No ERP total row selected';
                else if (definition.kind === 'difference') cell.textContent = comparison.length === 6
                    ? 'Original leaf totals minus Client ERP total' : 'Complete account checks to compare totals';
            }
            tr.appendChild(cell);
        });
        tbValidationBody.appendChild(tr);
    });
    renderDraftReconciliation(tbValidationBody, columns, mappingRoleByIndex, result);

}

// Reuse one footer for validation and saved uploads. Its values are exact server
// summaries of the retained source accounts and existing adjustment entries.
function renderDraftReconciliation(body, columns, mappingRoleByIndex, controls, includeOriginal = false) {
    if (!Object.prototype.hasOwnProperty.call(controls, 'draft_reconciliation')) return;
    const summary = controls.stale ? null : controls.draft_reconciliation;
    const decimalPlaces = body === tbValidationBody ? 0 : 2;
    function section(label, className) {
        const row = document.createElement('tr');
        row.className = className;
        const cell = document.createElement('td');
        cell.colSpan = columns.length; cell.textContent = label;
        row.appendChild(cell); body.appendChild(row);
    }
    if (controls.erp_unbalanced && !controls.stale) section('ERP imbalance: query pending.', 'tb-erp-warning');
    section('Draft Trial Balance reconciliation', 'tb-draft-heading');
    const definitions = includeOriginal ? [{label:'Original leaf totals', key:'original_totals'}] : [];
    definitions.push({label:'Difference adjustments', key:'adjustment_totals'},
                     {label:'Adjusted App totals', key:'adjusted_totals'},
                     {label:'Debit-credit difference', key:'pairs'});
    definitions.forEach(function (definition) {
        const row = document.createElement('tr');
        row.className = 'tb-draft-total tb-draft-' + definition.key;
        columns.forEach(function (column) {
            const cell = document.createElement('td');
            cell.className = column.className || '';
            const role = column.role || mappingRoleByIndex.get(column.index);
            if (column.kind === 'source' && role === 'account_name_column') {
                cell.textContent = definition.label;
            } else if (column.kind === 'source' && /_(dr|cr)_column$/.test(role || '')) {
                cell.classList.add(body === tbValidationBody ? 'tb-validation-amount' : 'tb-amount-cell');
                if (definition.key === 'pairs') {
                    // Show one result per pair in its debit column, even when the
                    // user's mapped source columns are arranged in a different order.
                    if (role.endsWith('_dr_column')) {
                        const pair = summary && summary.pair_differences[role.split('_')[0]];
                        const rounded = pair && formatTrialBalanceControlAmount(pair.difference, decimalPlaces);
                        cell.textContent = !pair ? 'Pending' : pair.within_tolerance
                            ? formatTrialBalanceControlAmount(0, decimalPlaces)
                            : decimalPlaces === 0 && rounded === '0' ? '<1' : rounded;
                        if (pair) {
                            cell.classList.add(pair.within_tolerance ? 'tb-footing-zero' : 'tb-footing-nonzero');
                            cell.title = 'Unrounded debit minus credit: ' + pair.difference;
                        }
                    }
                } else {
                    const value = summary && summary[definition.key][role];
                    cell.textContent = summary ? formatTrialBalanceControlAmount(value, decimalPlaces) : 'Pending';
                    if (summary) cell.title = 'Unrounded amount: ' + value;
                }
            } else if (column.kind === 'classification') {
                cell.textContent = 'Control';
                if (includeOriginal) cell.title = !summary ? 'Revalidate the source data' : definition.key === 'pairs'
                    ? 'Opening / Period / Closing: debit minus credit'
                    : definition.key === 'adjusted_totals' ? 'Draft totals including separate differences' : 'Source figures retained';
            } else if (column.kind === 'issues') {
                cell.textContent = !summary ? 'Revalidate the source data' : definition.key === 'pairs'
                    ? 'Opening / Period / Closing: debit minus credit'
                    : definition.key === 'adjusted_totals' ? 'Draft totals including separate differences' : 'Source figures retained';
            }
            row.appendChild(cell);
        });
        body.appendChild(row);
    });
}
function renderSavedDraftReconciliation(controls) {
    const columns = [
        {kind:'source',role:'account_code_column'}, {kind:'source',role:'account_name_column'},
        {kind:'classification'}, {kind:'category'}, {kind:'note'},
        ...['opening','period','closing'].flatMap(function (period) {
            return ['dr','cr'].map(function (side) { return {kind:'source',role:period+'_'+side+'_column',className:'tb-amount-cell'}; });
        }),
    ];
    renderDraftReconciliation(availableAccountsTableBody, columns, new Map(), controls, true);
}


// The financial workspace owns assignment metadata; the TB rows retain their source amounts.
function tbSetFinancialAccounts(accounts,ready=true) {
    tbFsMappingReady=ready;
    tbFsAccounts=Array.isArray(accounts)?accounts:[];
    tbFsLeaves=new Map();
    const byCode=new Map(tbFsAccounts.map(account=>[AccountAssignments.normaliseCode(account.account_code),account]));
    for(const account of tbFsAccounts.filter(account=>account.is_leaf)) {
        let code=AccountAssignments.normaliseCode(account.account_code),seen=new Set();
        while(code && !seen.has(code)) {
            seen.add(code);const rows=tbFsLeaves.get(code)||[];rows.push(account);tbFsLeaves.set(code,rows);
            const parent=byCode.get(code);code=parent?AccountAssignments.normaliseCode(parent.parent_account_code):'';
        }
    }
}
async function tbFsApi(url,options) {
    const response=await tbApiRequest(API_BASE+url,options);
    const result=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'The Financial Statement request failed.');
    return result;
}
function tbFsUpdateLock() {
    const locked=tbFsBusyCount>0||tbFsModalOpen;
    if(locked && !tbFsLockState)tbFsLockState={upload:availableUploadsDropdown?.disabled,workflow:tbAssignmentWorkflow?.disabled};
    if(locked){if(availableUploadsDropdown)availableUploadsDropdown.disabled=true;if(tbAssignmentWorkflow)tbAssignmentWorkflow.disabled=true;}
    else if(tbFsLockState){if(availableUploadsDropdown)availableUploadsDropdown.disabled=!!tbFsLockState.upload;if(tbAssignmentWorkflow)tbAssignmentWorkflow.disabled=!!tbFsLockState.workflow;tbFsLockState=null;}
    if(availableAccountsTableBody){availableAccountsTableBody.inert=tbFsBusyCount>0;availableAccountsTableBody.setAttribute('aria-busy',String(tbFsBusyCount>0));}
}
async function tbFsWithBusy(message,operation) {
    tbFsBusyCount++;tbFsUpdateLock();const token=startTrialBalanceLoading(tbAssignmentSetup);
    try{return await operation();}finally{stopTrialBalanceLoading(token);tbFsBusyCount--;tbFsUpdateLock();}
}
function tbRenderFinancialAssignments() {
    if(!tbFsWorkspace || tbWorkflowMode!=='financial' || !tbFsIsAdmin || !availableAccountsTableBody)return;
    const state=tbFsWorkspace.STATE;
    if(!tbFsMappingReady || !(state.financialLoaded || state.pickerCategoriesReady))return;
    availableAccountCategoryLookupReady=true;availableAccountNoteLookupReady=!!state.notesLoaded;
    setSavedFilterAvailability('category', false);
    setSavedFilterAvailability('note', !state.notesLoaded);
    if(state.notesLoaded)availableAccountLookupMessage='';
    if(tbFsRenderedCategories!==state.categories || tbFsRenderedNotes!==state.notes){
        tbFsMetadataRevision++;tbFsRenderedCategories=state.categories;tbFsRenderedNotes=state.notes;
    }
    // Filter options can be cleared by a workflow switch even when metadata is unchanged.
    if(tbSavedCategoryOptions.dataset.fsRevision!==String(tbFsMetadataRevision) || !tbSavedCategoryOptions.children.length){
        populateSavedCategoryOptions(state.categories);tbSavedCategoryOptions.dataset.fsRevision=String(tbFsMetadataRevision);
    }
    if(state.notesLoaded && (tbSavedNoteOptions.dataset.fsRevision!==String(tbFsMetadataRevision) || !tbSavedNoteOptions.children.length)){
        populateSavedNoteOptions(state.notes);tbSavedNoteOptions.dataset.fsRevision=String(tbFsMetadataRevision);
    }
    const byCode=new Map(tbFsAccounts.map(account=>[AccountAssignments.normaliseCode(account.account_code),account]));
    for(const row of availableAccountsTableBody.querySelectorAll('tr[data-category-filter]')) {
        const account=byCode.get(row.dataset.accountCode);if(!account)continue;
        const categoryId=tbFsWorkspace.accEffectiveFinancialCategory(account);
        const noteId=tbFsWorkspace.accEffectiveNoteCategory(account);
        const category=state.categories.find(item=>String(item.id)===String(categoryId));
        const categoryText=categoryId==='__mixed'?'Mixed assignments':category?category.label:categoryId?'Unavailable':'Unassigned';
        const noteText=tbFsWorkspace.accNoteAssignmentSearchText(account);
        row.dataset.categoryFilter=categoryText;row.dataset.noteFilter=noteText;
        availableAccountCategoryByCode.set(row.dataset.accountCode,categoryText);
        availableAccountNoteByCode.set(row.dataset.accountCode,noteText);
        const categoryCell=row.querySelector('.tb-saved-category-cell');const noteCell=row.querySelector('.tb-saved-note-cell');
        const unlinkedParent=!account.is_leaf && !(tbFsLeaves.get(row.dataset.accountCode)||[]).length;
        const codeCell=row.cells[0];
        codeCell.classList.toggle('tb-account-code-review',unlinkedParent);
        if(unlinkedParent){
            codeCell.title='No linked child accounts. Double-click to correct this account code.';
            if(!codeCell.querySelector('.tb-code-review-hint')){const hint=document.createElement('small');hint.className='tb-code-review-hint';hint.textContent='Check code';codeCell.appendChild(hint);}
        }else{codeCell.querySelector('.tb-code-review-hint')?.remove();}
        const signature=JSON.stringify([tbFsMetadataRevision,categoryId,noteId,!!state.notesLoaded]);
        if(row.dataset.fsSignature===signature && categoryCell.querySelector('select')?.value===String(categoryId||'')
            && (!state.notesLoaded || noteCell.querySelector('select')?.value===String(noteId||'')))continue;
        row.dataset.fsSignature=signature;
        categoryCell.innerHTML=tbFsWorkspace.accFinancialPickerMarkup(account,categoryId);categoryCell.title='';
        if(!state.notesLoaded)categoryCell.querySelector('[data-fs-picker-trigger]').disabled=true;
        if(state.notesLoaded){const options=categoryId && categoryId!=='__mixed'?tbFsWorkspace.accGetEffectiveNoteOptions(categoryId):[];
            noteCell.innerHTML=tbFsWorkspace.accNotePickerMarkup(account,noteId,categoryId,options);noteCell.title='';}
    }
    const cache=tbWorkflowCache.get('financial');
    if(cache)Object.assign(cache,{category:availableAccountCategoryByCode,note:availableAccountNoteByCode,options:tbSavedCategoryOptions.innerHTML,noteOptions:tbSavedNoteOptions.innerHTML});
    syncSavedFilterOptionsFromRows('note');
    applySavedCategoryFilter();
}
function tbApplyConfirmedFinancialChanges(result,context,targets) {
    if(!result?.saved || String(result.client_id)!==String(context.clientId)
        || String(result.trial_balance_upload_id)!==String(context.uploadId)
        || String(window.getSelectedClientId())!==String(context.clientId)
        || String(availableUploadsDropdown.value)!==String(context.uploadId)
        || !Array.isArray(result.assignment_changes))return false;
    const expected=new Set(targets.map(item=>AccountAssignments.normaliseCode(item.account_code)));
    const byCode=new Map(tbFsAccounts.map(item=>[AccountAssignments.normaliseCode(item.account_code),item]));
    const changes=result.assignment_changes;
    const received=new Set(changes.map(item=>AccountAssignments.normaliseCode(item.account_code)));
    if(changes.length!==expected.size || received.size!==expected.size || [...received].some(code=>!expected.has(code) || !byCode.has(code))
        || changes.some(item=>!Object.hasOwn(item,'financial_statement_category_id') || !Object.hasOwn(item,'note_category_id')))return false;
    // Apply only server-confirmed metadata. Source amounts and unrelated workstreams stay intact.
    for(const change of changes){
        const account=byCode.get(AccountAssignments.normaliseCode(change.account_code));
        account.assignment=Object.assign({},account.assignment);
        for(const field of ['financial_statement_category_id','note_category_id','verification_workstream']){
            if(Object.hasOwn(change,field))account.assignment[field]=change[field];
        }
    }
    return true;
}
async function tbRefreshFinancialMapping() {
    const clientId=window.getSelectedClientId(),uploadId=availableUploadsDropdown.value;
    AccountAssignments.invalidate(tbReadAssignmentJson);
    const result=await AccountAssignments.read('mapping',clientId,uploadId,tbReadAssignmentJson);
    if(String(clientId)!==String(window.getSelectedClientId())||uploadId!==availableUploadsDropdown.value)return;
    tbSetFinancialAccounts(result.accounts);tbRenderFinancialAssignments();
}
async function tbOpenFinancialSetup(mode) {
    if(!availableUploadsDropdown?.value)return;
    if(!tbFsIsAdmin){showStatus('Only administrators can manage account assignments.','error');return;}
    try{await tbFsWorkspace.open(mode);}catch(error){showStatus(error.message,'error');}
}
tbCashflowWorkspace = CashflowWorkspace.create({
    get clientId(){return window.getSelectedClientId?window.getSelectedClientId():null;},
    get uploadId(){return availableUploadsDropdown && availableUploadsDropdown.value ? Number(availableUploadsDropdown.value) : null;},
    get isAdmin(){return tbFsIsAdmin;},
    request:(url,options)=>tbApiRequest(API_BASE+url,options),
    onSaved:tbRefreshCashflowAssignments,
    onModalChanged:open=>{tbFsModalOpen=open;tbFsUpdateLock();},
});
async function tbOpenCashflowSetup() {
    if(!availableUploadsDropdown?.value)return;
    if(!tbFsIsAdmin){showStatus('Only administrators can manage Cashflow setup.','error');return;}
    try{await tbCashflowWorkspace.open('categories');}catch(error){showStatus(error.message,'error');}
}
async function tbOpenLegacySetup(setup) {
    if (setup === 'financial' || setup === 'notes') return tbOpenFinancialSetup(setup);
    if (setup === 'verification') return tbOpenVerificationSetup();
    if (!['cashflow', 'treasury', 'template'].includes(setup)) return;
    await tbOpenCashflowSetup();
    const tab = {cashflow: 'categories', treasury: 'accounts', template: 'template'}[setup];
    const control = document.querySelector('#cfwBackdrop [data-cfw-tab="' + tab + '"]');
    if (control) control.click();
}
tbVerificationWorkspace = window.VerificationWorkspace ? VerificationWorkspace.create({
    clientId:()=>window.getSelectedClientId?window.getSelectedClientId():null,
    uploadId:()=>availableUploadsDropdown && availableUploadsDropdown.value ? String(availableUploadsDropdown.value) : null,
    isAdmin:()=>tbFsIsAdmin,
    readMapping:()=>AccountAssignments.read('mapping',window.getSelectedClientId(),availableUploadsDropdown.value,tbReadAssignmentJson),
    save:(account,workstream,parent)=>tbSaveVerificationAssignment(account,workstream,parent),
    onSaved:change=>tbApplyVerificationAssignment(change.accountCode,change.workstream,change.parent),
    onModalChanged:open=>{tbFsModalOpen=open;tbFsUpdateLock();},
}) : null;
async function tbOpenVerificationSetup() {
    if(!availableUploadsDropdown?.value)return;
    if(!tbFsIsAdmin){showStatus('Only administrators can manage Verification assignments.','error');return;}
    if(!tbVerificationWorkspace){showStatus('Verification setup is unavailable. Reload the Trial Balance page.','error');return;}
    try{await tbVerificationWorkspace.open();}catch(error){showStatus(error.message,'error');}
}
tbFsWorkspace=FinancialStatementWorkspace.create({
    lazyPickers:true,applyConfirmedChanges:tbApplyConfirmedFinancialChanges,
    get accounts(){return tbFsAccounts;},get clientId(){return window.getSelectedClientId?window.getSelectedClientId():null;},get isAdmin(){return tbFsIsAdmin;},
    leavesBelow:code=>tbFsLeaves.get(AccountAssignments.normaliseCode(code))||[],
    uploadPayload:()=>({trial_balance_upload_id:Number(availableUploadsDropdown?.value)||null}),api:tbFsApi,
    refreshMapping:tbRefreshFinancialMapping,renderFinancialRows:tbRenderFinancialAssignments,renderNotesRows:tbRenderFinancialAssignments,
    showStatus:(message,type)=>showStatus(message,type),clearStatus:()=>{const status=getElement('tbSavedStatus');if(status)status.style.display='none';},
    focusTarget:()=>tbAssignmentSetup,withBusy:tbFsWithBusy,modalChanged:open=>{tbFsModalOpen=open;tbFsUpdateLock();},
});
if(availableAccountsTableBody)tbFsWorkspace.bindPickers(availableAccountsTableBody);
