// ==============================================
// 📊 CLIENT DATA BANK - FINAL POLISHED VERSION
// ==============================================

// API_BASE and STORAGE_KEY are defined in client_context.js
const dataBankPerfEnabled = new URLSearchParams(window.location.search).get('perf') === '1';
const nativeDataBank = window.AppNativeDataBank;
let nativeLoadedExportKey = null;
let nativeDownloadController = null;
let nativeDownloadSequence = 0;
let nativeRevisionSequence = 0;
function nativeReadAvailable() { return !nativeDataBank || nativeDataBank.enabled; }
function assertNativeClient(response, clientId) {
    if (nativeDataBank && response.headers.get('X-DataBank-Client') !== String(clientId)) throw new Error('Data scope changed. Refresh to continue.');
}
function boundDataBankCache(cache) { while (cache.size > PREVIEW_CACHE_MAX_ENTRIES) cache.delete(cache.keys().next().value); }

// Native cells retain JSON types; exact numeric tokens bypass JSON.parse's binary64 conversion.
function nativeExactNumber(value) {
    const match = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(String(value).trim());
    if (!match || !(match[2] || match[3]) || String(value).length > 4096) return null;
    const exponent = Number(match[4] || 0); if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 4096) return null;
    let scale = (match[3] || '').length - exponent;
    let coefficient = BigInt((match[1] === '-' ? '-' : '') + (match[2] || '0') + (match[3] || ''));
    if (scale < 0) { coefficient *= 10n ** BigInt(-scale); scale = 0; }
    return { coefficient, scale };
}
function addNativeExact(left, right) {
    if (!left || !right) return null;
    const scale = Math.max(left.scale, right.scale);
    return { coefficient: left.coefficient * 10n ** BigInt(scale - left.scale) + right.coefficient * 10n ** BigInt(scale - right.scale), scale };
}
function nativeExactText(value) {
    if (!value) return 'Unavailable';
    let text = (value.coefficient < 0n ? -value.coefficient : value.coefficient).toString().padStart(value.scale + 1, '0');
    if (value.scale) text = text.slice(0, -value.scale) + '.' + text.slice(-value.scale);
    return (value.coefficient < 0n ? '-' : '') + text;
}

function nativeHistoryValue(value) {
    if (value == null || value === '') return '—';
    return typeof value === 'object' ? JSON.stringify(value) : String(value);
}
function nativeExportKey(spec) {
    if (!spec) return null;
    const params = new URLSearchParams(spec.params);
    ['page', 'limit', 'include_metrics', 'perf'].forEach(key => params.delete(key)); params.sort();
    return params.toString();
}
function updateNativeExportButton() {
    const button = document.getElementById('nativeExportMatching'); if (!button) return;
    button.hidden = !nativeDataBank?.exports;
    if (!nativeDataBank?.exports) return;
    button.disabled = !!nativeDownloadController || isPreviewLoading || !isPreviewOpen || previewMode !== 'raw'
        || !nativeLoadedExportKey || nativeExportKey(buildPreviewRequestSpec()) !== nativeLoadedExportKey;
}
function cancelNativeDownload() { nativeDownloadSequence++; nativeDownloadController?.abort(); }
async function nativeDownload(templateId, backup = false) {
    if (!nativeDataBank?.exports || nativeDownloadController || backup && (!nativeDataBank.history || !nativeDataBank.canBackup)) return;
    const clientId = Number(getSelectedClientId()); if (!clientId) return;
    const key = backup ? null : nativeExportKey(buildPreviewRequestSpec());
    if (!backup && (!isPreviewOpen || previewMode !== 'raw' || isPreviewLoading || key !== nativeLoadedExportKey || Number(previewTemplateId) !== Number(templateId))) return;
    const params = backup ? new URLSearchParams({ client_id: String(clientId) }) : new URLSearchParams(key);
    if (!backup) params.delete('template_id');
    const controller = new AbortController(); nativeDownloadController = controller; const sequence = ++nativeDownloadSequence;
    const panel = document.getElementById('nativeDownloadPanel'), status = document.getElementById('nativeDownloadStatus'), cancel = document.getElementById('nativeDownloadCancel');
    panel.hidden = false; cancel.hidden = false; status.textContent = backup ? 'Preparing the complete current/history backup…' : 'Preparing all rows matching the loaded preview…'; updateNativeExportButton();
    const timer = setTimeout(() => controller.abort(), 75000);
    try {
        const response = await fetch(`${API_BASE}/api/data/template/${templateId}/${backup ? 'backup' : 'export'}?${params}`, { signal: controller.signal, cache: 'no-store' });
        if (!response.ok) { let message = 'Download could not be completed.'; try { message = (await response.json()).detail || message; } catch (_) {} throw new Error(message); }
        assertNativeClient(response, clientId);
        if (response.headers.get('X-DataBank-Template') !== String(templateId) || response.headers.get('X-Export-Scope') !== (backup ? 'backup' : 'matching')) throw new Error('Download scope changed. Retry.');
        const maximum = nativeDataBank.maximumExportBytes;
        if (!Number.isSafeInteger(maximum) || maximum < 1 || Number(response.headers.get('Content-Length')) > maximum) throw new Error('The complete workbook exceeds the download limit.');
        const bytes = await response.arrayBuffer(); if (!bytes.byteLength || bytes.byteLength > maximum) throw new Error('The complete workbook exceeds the download limit.');
        if (sequence !== nativeDownloadSequence || Number(getSelectedClientId()) !== clientId || !backup && (key !== nativeLoadedExportKey || key !== nativeExportKey(buildPreviewRequestSpec()))) throw new Error('Selection changed. Request the download again.');
        const header = response.headers.get('Content-Disposition') || '';
        const utf = /filename\*=UTF-8''([^;]+)/i.exec(header), plain = /filename="([^"]+)"/i.exec(header);
        let name = utf ? decodeURIComponent(utf[1]) : plain ? plain[1] : 'data-export.xlsx'; name = name.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_');
        const url = URL.createObjectURL(new Blob([bytes], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }));
        const link = document.createElement('a'); link.href = url; link.download = name; document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
        status.textContent = backup ? 'Complete backup download started. No data was deleted.' : 'Matching-row download started.';
    } catch (error) {
        status.textContent = controller.signal.aborted || sequence !== nativeDownloadSequence ? 'Download cancelled or timed out. Request it again to retry.' : 'Download unavailable: ' + error.message;
    } finally {
        clearTimeout(timer); if (nativeDownloadController === controller) nativeDownloadController = null;
        cancel.hidden = true; updateNativeExportButton();
    }
}
async function nativeRowRevisions(icon, rowId, source = 'uploads', page = 1) {
    const clientId = Number(getSelectedClientId()), template = Number(previewTemplateId), sequence = ++nativeRevisionSequence;
    const current = () => sequence === nativeRevisionSequence && activeHistoryIcon === icon && isPreviewOpen && Number(getSelectedClientId()) === clientId && Number(previewTemplateId) === template;
    rowHistoryBody.replaceChildren(document.createTextNode('Loading recorded changes…'));
    try {
        const params = new URLSearchParams({ client_id: clientId, template_id: template, source, page, limit: 25 });
        const response = await fetch(`${API_BASE}/api/data/rows/${rowId}/history?${params}`);
        if (!response.ok) throw new Error('Recorded changes unavailable.'); const data = await response.json(); if (!current()) return;
        assertNativeClient(response, clientId);
        if (Number(data.client_data_id) !== Number(rowId) || Number(data.template_id) !== template || data.source !== source || data.page !== page) throw new Error('Revision scope changed.');
        const choose = document.createElement('select'); choose.setAttribute('aria-label', 'Revision source');
        for (const [value, label] of [['uploads', 'Saved upload changes'], ['legacy', 'Legacy audit records']]) { const option = document.createElement('option'); option.value = value; option.textContent = label; choose.appendChild(option); }
        choose.value = source; choose.addEventListener('change', () => nativeRowRevisions(icon, rowId, choose.value, 1));
        const note = document.createElement('p'); note.textContent = 'Sources may describe the same change; their record counts are separate.';
        const count = document.createElement('p'); count.textContent = `${data.total} records · page ${page} of ${data.total_pages || 1}`; rowHistoryBody.replaceChildren(choose, note, count);
        for (const revision of data.revisions || []) {
            const block = document.createElement('section'), title = document.createElement('p'); title.textContent = `${revision.action} · ${formatHistoryTimestamp(revision.changed_at)} · ${revision.user || 'System'}`; block.appendChild(title);
            if (!revision.details_available) block.appendChild(document.createTextNode('Field-by-field details were not saved.'));
            else for (const [field, change] of Object.entries(revision.changed_fields || {})) {
                const line = document.createElement('p'), name = document.createElement('strong'); name.textContent = field + ': '; line.appendChild(name);
                appendHistoryChangeValue(line, change, revision.numeric_text?.[field]); block.appendChild(line);
            }
            rowHistoryBody.appendChild(block);
        }
        for (const [label, target, disabled] of [['Previous', page - 1, page <= 1], ['Next', page + 1, page >= data.total_pages]]) {
            const button = document.createElement('button'); button.type = 'button'; button.textContent = label; button.disabled = disabled;
            button.addEventListener('click', () => nativeRowRevisions(icon, rowId, source, target)); rowHistoryBody.appendChild(button);
        }
    } catch (error) { if (current()) rowHistoryBody.replaceChildren(document.createTextNode(error.message)); }
}

function logDataBankPerf(operation, startedAt, details) {
    if (!dataBankPerfEnabled) return;
    console.info('[data-preview-perf]', {
        operation,
        elapsed_ms: Math.round((performance.now() - startedAt) * 10) / 10,
        ...details
    });
}

// ---------- STATE ----------
let availabilityData = [];
let selectedTemplateId = null;
let dateStart = null;
let dateEnd = null;
let currentPage = 1;
let totalPages = 1;
let previewLimit = 50;
let previewTotalRows = 0;
let previewSundayCount = 0;
// Document grouping / subtotal support: identify which columns are the
// document key / date / amount(s) for the current preview, sourced straight
// from GET /api/data/preview's response so the frontend never re-derives the
// grouping key itself. previewAmountColumns drives the subtotal row in
// renderPreviewRows(); previewDocumentKeyColumn/previewDateColumn are no
// longer used to blank cells (continuation lines show Document No./Date on
// every line per a later fix) - kept here since they're still part of the
// API response and the grouping key it backs.
let previewDocumentKeyColumn = null;
let previewDateColumn = null;
let previewAmountColumns = [];
// Subtotal toggle (Subtotal toggle: Data Bank preview UI, FINAL phase):
// previewShowSubtotals is initialised from the opened template's
// has_subtotals_by_default flag (see fetchTemplateSubtotalDefault() below,
// called from openPreview()) and freely overridable per session via the
// #subtotalToggleCheckbox listener - no persistence across preview opens.
// previewCachedRows holds the last-fetched page's rows so toggling re-
// renders instantly without a network round-trip.
let previewShowSubtotals = true;
let previewCachedRows = [];
let previewTemplateId = null;
let previewTemplateName = '';
let previewMode = 'raw'; // raw Client Data preview or read-only Cashflows classification preview
let isPreviewOpen = false;
let isPreviewLoading = false;
// Data Bank response cache: keep a bounded set of complete preview pages in
// memory for this page session. The request key includes every server-side
// filter, so a cached page can never be reused for a different view.
const PREVIEW_CACHE_MAX_ENTRIES = 12;
const previewPageCache = new Map();
const previewPageInFlight = new Map();
let previewCacheGeneration = 0;
let previewRequestSequence = 0;
let previewOpenSequence = 0;
let previewAccountState = 'standard';
let previewLedgerAccounts = [];
let previewLedgerAccountCode = '';
let previewLastLedgerAccountCode = '';
let previewLedgerMatches = [];
let previewLedgerActiveIndex = -1;
let previewLedgerLoading = null;
// Availability is also keyed by its client/date scope. This prevents duplicate
// entry/date-range requests while keeping the cache limited to this page tab.
const availabilityCache = new Map();
const availabilityInFlight = new Map();
const availabilityMetricsCache = new Map();
const availabilityMetricsInFlight = new Map();
let availabilityRequestSequence = 0;
// Phase 3B: delete is scoped per template, so one confirmed destructive action blocks only its
// own repeated request while other datasets and unrelated controls remain available.
const deleteTemplateInFlight = new Set();
let pendingTemplateDelete = null;

// Coverage Calendar state — independent of the shared date range (see
// sharedRangeFromInput/sharedRangeToInput below). As of the Coverage
// Calendar → per-template popover fix, this is scoped to whichever single
// template's modal is currently open (calendarYear/calendarMonth reset to
// the current month each time it opens; currentPopoverTemplateId/Name track
// which template that is) — there is no more multi-template selection.
let calendarYear = new Date().getFullYear();
let calendarMonth = new Date().getMonth() + 1; // 1-12
let calendarRequestSequence = 0;
let currentPopoverTemplateId = null;
let currentPopoverTemplateName = '';

// "Your Datasets" search (Coverage Calendar → per-template popover fix):
// filters the unified template list by name, replacing the old chip
// picker's search. See renderFilteredYourDatasetsTable().
let yourDatasetsSearchTerm = '';

// ---------- DOM REFS ----------
const statusDiv = document.getElementById('statusMessage');

// Redesign Phase 5: "Your datasets" table — fed by fetchAvailability() data,
// never a second fetch. See renderYourDatasetsTable()/resetDatasetTables().
// Coverage Calendar → per-template popover fix: this is now also the single
// unified template list, with its own search input and the relocated
// Upload More button in a header row above it.
const yourDatasetsBody = document.getElementById('yourDatasetsBody');
const yourDatasetsNoData = document.getElementById('yourDatasetsNoData');
const yourDatasetsSearchInput = document.getElementById('yourDatasetsSearch');
// 3-fix: clear (✕) icon, only visible once the field has text.
const yourDatasetsSearchClearBtn = document.getElementById('yourDatasetsSearchClear');

// Redesign Phase 1: header's Upload More button (moved off an inline onclick).
const uploadMoreBtn = document.getElementById('uploadMoreBtn');

// Template calendar modal DOM refs (Coverage Calendar → per-template popover
// fix). #calendarPrevMonthBtn/#calendarNextMonthBtn/#calendarMonthLabel/
// #coverageCalendarsContainer/#missingDatesReportBody are the same elements
// the old page-level widget used, just relocated into this modal.
const templateCalendarModal = document.getElementById('templateCalendarModal');
const templateCalendarModalTitle = document.getElementById('templateCalendarModalTitle');
const closeTemplateCalendarBtn = document.getElementById('closeTemplateCalendarBtn');
const calendarPrevMonthBtn = document.getElementById('calendarPrevMonthBtn');
const calendarNextMonthBtn = document.getElementById('calendarNextMonthBtn');
const calendarMonthLabel = document.getElementById('calendarMonthLabel');
const coverageCalendarsContainer = document.getElementById('coverageCalendarsContainer');

// Missing Dates Report DOM ref
const missingDatesReportBody = document.getElementById('missingDatesReportBody');

const statsTotalRecords = document.getElementById('totalRecords');
const statsTotalTemplates = document.getElementById('totalTemplates');
const statsClientDbSize = document.getElementById('clientDbSize');
// Overdue Uploads (Phase D2, FINAL): renamed from statsSundayRows/#totalSundayRows.
const statsOverdueUploads = document.getElementById('overdueUploadsCount');
const overdueUploadsTile = document.getElementById('overdueUploadsTile');
const overdueUploadsPanel = document.getElementById('overdueUploadsPanel');
const overdueUploadsTableBody = document.getElementById('overdueUploadsTableBody');
const overdueUploadsNoData = document.getElementById('overdueUploadsNoData');
let overdueUploadsData = [];

// Preview modal elements
const previewModal = document.getElementById('previewModal');
const previewMetrics = document.getElementById('previewMetrics');

function previewMetricScopeMatches(scope, spec) {
    const params = spec.params;
    if (!scope || Number(scope.client_id) !== Number(spec.clientId) || Number(scope.template_id) !== Number(spec.templateId)
        || scope.date_start !== params.get('date_start') || scope.date_end !== params.get('date_end')) return false;
    for (const key of ['search', 'ledger_account_code', 'data_quality']) {
        if ((scope[key] || '') !== (params.get(key) || '')) return false;
    }
    for (const key of ['amount_min', 'amount_max']) {
        if (params.has(key) ? scope[key] == null || Number(scope[key]) !== Number(params.get(key)) : scope[key] != null) return false;
    }
    return Boolean(scope.sunday_only) === (params.get('sunday_only') === 'true');
}

function setPreviewMetricsState(message = 'Loading totals…') {
    if (!previewMetrics) return;
    previewMetrics.replaceChildren();
    previewMetrics.hidden = previewMode !== 'raw';
    if (!previewMetrics.hidden) previewMetrics.textContent = message;
}

function renderPreviewMetrics(data, spec) {
    setPreviewMetricsState('');
    if (!previewMetrics || previewMetrics.hidden) return;
    const metrics = data.preview_metrics;
    const add = (label, value, title) => {
        const item = document.createElement('span'); item.className = 'preview-metric';
        item.title = title || label;
        const name = document.createElement('span'); name.className = 'preview-metric-label'; name.textContent = label;
        const number = document.createElement('span'); number.className = 'preview-metric-value';
        // Preserve every decimal digit, without a floating-point conversion.
        const [whole, fraction] = String(value).split('.');
        number.textContent = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (fraction === undefined ? '' : `.${fraction}`);
        item.append(name, number); previewMetrics.appendChild(item);
    };
    add('Rows', data.total_rows ?? '—', 'All matching source detail rows; pagination and subtotals are excluded.');
    const totals = metrics?.footing_totals;
    const validMetrics = metrics?.status === 'ok' && Number.isSafeInteger(metrics.row_count)
        && metrics.row_count === data.total_rows && Number.isSafeInteger(metrics.document_count)
        && metrics.document_count >= 0 && metrics.document_count <= metrics.row_count
        && totals && typeof totals === 'object' && !Array.isArray(totals)
        && Object.values(totals).every(value => /^[+-]?\d+(?:\.\d+)?$/.test(String(value)))
        && Boolean(Object.keys(totals).length) === metrics.footing_configured;
    if (!validMetrics || !previewMetricScopeMatches(metrics.scope, spec)) {
        const unavailable = document.createElement('span'); unavailable.textContent = 'Document/Footing totals unavailable';
        previewMetrics.appendChild(unavailable); return;
    }
    add('Documents', metrics.document_count, `Unique nonblank Document IDs: ${metrics.document_column}`);
    for (const [column, total] of Object.entries(metrics.footing_totals || {})) add(column, total, `Footing total: ${column}`);
    if (!metrics.footing_configured) {
        const note = document.createElement('span'); note.textContent = 'Footing not configured'; previewMetrics.appendChild(note);
    }
    if (metrics.missing_document_rows) {
        const note = document.createElement('span'); note.textContent = `${metrics.missing_document_rows} rows without Document ID`; previewMetrics.appendChild(note);
    }
    if (spec.params.has('search') || spec.params.has('amount_min') || spec.params.has('amount_max')
        || spec.params.has('data_quality') || spec.params.has('sunday_only')) {
        const note = document.createElement('span'); note.className = 'preview-metric-scope'; note.textContent = 'Filtered selection'; previewMetrics.appendChild(note);
    }
}

let previewFocusRefreshQueued = false;
function queuePreviewFocusRefresh() {
    if (!isPreviewOpen || previewMode !== 'raw' || document.visibilityState === 'hidden' || previewFocusRefreshQueued) return;
    previewFocusRefreshQueued = true;
    // Focus/visibility can describe the same browser event; coalesce that frame.
    requestAnimationFrame(() => {
        previewFocusRefreshQueued = false;
        if (!isPreviewOpen || previewMode !== 'raw' || document.visibilityState === 'hidden') return;
        invalidateDataBankCaches({clientId: getSelectedClientId(), templateId: previewTemplateId});
        previewPageInFlight.clear();
        setPreviewMetricsState();
        void fetchPreviewData();
    });
}
window.addEventListener('focus', queuePreviewFocusRefresh);
document.addEventListener('visibilitychange', queuePreviewFocusRefresh);
const templateDeleteModal = document.getElementById('templateDeleteModal');
const templateDeleteTitle = document.getElementById('templateDeleteTitle');
const templateDeleteStatus = document.getElementById('templateDeleteStatus');
const backupAndDeleteTemplateBtn = document.getElementById('backupAndDeleteTemplateBtn');
const cancelTemplateDeleteBtn = document.getElementById('cancelTemplateDeleteBtn');
const closeTemplateDeleteBtn = document.getElementById('closeTemplateDeleteBtn');
let linkedVerificationPurgeCheckbox = null;
function ensureLinkedVerificationPurgeOption() {
    if (nativeDataBank || linkedVerificationPurgeCheckbox) return linkedVerificationPurgeCheckbox;
    const label = document.createElement('label');
    label.className = 'template-delete-verification-option';
    label.style.display = 'block';
    label.style.margin = '12px 0';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.id = 'includeLinkedVerificationPurge';
    checkbox.style.marginRight = '8px';
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(
        'Also permanently delete this template’s linked test Verification details and evidence. These are not in the Excel backup; session headers remain.'
    ));
    templateDeleteStatus.parentElement.insertBefore(label, templateDeleteStatus);
    const storageNote = document.createElement('small');
    storageNote.className = 'template-delete-storage-note';
    storageNote.textContent = 'Deleting makes database space reusable, but may not immediately reduce Supabase’s displayed database size.';
    templateDeleteStatus.parentElement.insertBefore(storageNote, templateDeleteStatus);
    linkedVerificationPurgeCheckbox = checkbox;
    return checkbox;
}
const historyModal = document.getElementById('historyModal');
const closeHistoryBtn = document.getElementById('closeHistoryBtn');
const historyTitle = document.getElementById('historyTitle');
const historyStatus = document.getElementById('historyStatus');
const historyActionFilter = document.getElementById('historyActionFilter');
const historyLedgerControl = document.getElementById('historyLedgerControl');
const historyLedgerSelect = document.getElementById('historyLedgerSelect');
const historyTableHead = document.getElementById('historyTableHead');
const historyTableBody = document.getElementById('historyTableBody');
const historyPageInfo = document.getElementById('historyPageInfo');
const historyPrevBtn = document.getElementById('historyPrevBtn');
const historyNextBtn = document.getElementById('historyNextBtn');
let historyPage = 1;
let historyTotalPages = 1;
let historyClientId = null;
let historyTemplateId = null;
let historyTemplateName = '';
let historyDocumentKeyColumn = null;
let historyColumns = [];
let historyRequestSequence = 0;
const previewTitle = document.getElementById('previewTitle');
const previewTableHead = document.getElementById('previewTableHead');
const previewTableBody = document.getElementById('previewTableBody');
const pageSizeSelect = document.getElementById('pageSizeSelect');
const sundayOnlyCheckbox = document.getElementById('sundayOnlyCheckbox');
const subtotalToggleCheckbox = document.getElementById('subtotalToggleCheckbox');
const dataQualityFilter = document.getElementById('dataQualityFilter');
const sundayCountBadge = document.getElementById('sundayCountBadge');
const cashflowsClassificationFilters = document.getElementById('cashflowsClassificationFilters');
const classificationTypeFilter = document.getElementById('classificationTypeFilter');
const classificationAccountFilter = document.getElementById('classificationAccountFilter');
const classificationCategoryFilter = document.getElementById('classificationCategoryFilter');

// Shared date range modal elements (Phase 2 — Unified date range; restyled
// from a small anchored popover to a centred overlay modal per the 3-fix).
// One modal, two entry points: the row 👁️ icon (gate mode, before the
// preview opens) and this modal's own trigger button (live mode, while it's
// open).
const modalDateRangeBtn = document.getElementById('modalDateRangeBtn');
const sharedRangePopover = document.getElementById('sharedRangePopover');
const closeSharedRangeBtn = document.getElementById('closeSharedRangeBtn');
const sharedRangeFromInput = document.getElementById('sharedRangeFrom');
const sharedRangeToInput = document.getElementById('sharedRangeTo');
const sharedRangeConfirmBtn = document.getElementById('sharedRangeConfirmBtn');
// Set only while the popover is in "gate" mode (opened from a row's preview
// icon, before the modal exists) — holds which template to open once the
// range is confirmed. Null in "live" mode (popover opened from inside an
// already-open modal), where confirming just refreshes in place.
let pendingPreviewOpen = null;
const searchInput = document.getElementById('searchInput');
const previewLedgerField = document.getElementById('previewLedgerField');
const previewLedgerInput = document.getElementById('previewLedgerInput');
const previewLedgerToggle = document.getElementById('previewLedgerToggle');
const previewLedgerOptions = document.getElementById('previewLedgerOptions');
const previewLedgerStatus = document.getElementById('previewLedgerStatus');
const previewLedgerStatusText = document.getElementById('previewLedgerStatusText');
const previewLedgerRetry = document.getElementById('previewLedgerRetry');
const amountMin = document.getElementById('amountMin');
const amountMax = document.getElementById('amountMax');
const applyFiltersBtn = document.getElementById('applyFiltersBtn');
const firstPageBtn = document.getElementById('firstPageBtn');
const prevPageBtn = document.getElementById('prevPageBtn');
const nextPageBtn = document.getElementById('nextPageBtn');
const lastPageBtn = document.getElementById('lastPageBtn');
const pageInfo = document.getElementById('pageInfo');
const previewCountInfo = document.getElementById('previewCountInfo');
const closePreviewBtn = document.getElementById('closePreviewBtn');
const clientDataLoadingOverlay = document.getElementById('clientDataLoadingOverlay');
const clientDataLoadingDetail = document.getElementById('clientDataLoadingDetail');
const clientDataLoadingPercent = document.getElementById('clientDataLoadingPercent');
const clientDataLoadingProgress = document.getElementById('clientDataLoadingProgress');

// Row revision history popover elements (Phase 3)
const rowHistoryPopover = document.getElementById('rowHistoryPopover');
const rowHistoryBody = document.getElementById('rowHistoryBody');
// Tracks which history icon (if any) currently has its popover open, so the
// click handler can toggle it closed and the outside-click handler knows
// what counts as "inside".
let activeHistoryIcon = null;

// ---------- HELPERS ----------
function showStatus(msg, type = 'info') {
    statusDiv.textContent = msg;
    statusDiv.className = 'upload-status ' + type;
    statusDiv.style.display = 'block';
    clearTimeout(window.statusTimeout);
    window.statusTimeout = setTimeout(() => {
        statusDiv.style.display = 'none';
    }, 5000);
}

function showClientDataLoading() {
    if (!clientDataLoadingOverlay || !clientDataLoadingProgress) return;
    if (clientDataLoadingOverlay.style.display === 'flex') return;
    clientDataLoadingOverlay.style.display = 'flex';
    clientDataLoadingOverlay.setAttribute('aria-hidden', 'false');
    clientDataLoadingDetail.textContent = 'Connecting to the data service…';
    clientDataLoadingPercent.textContent = '0%';
    window.LoadingUI.showProgressBar(clientDataLoadingProgress, {
        mode: 'indeterminate',
        message: 'Fetching template availability…'
    });
}

function startClientDataRendering(totalTemplates) {
    if (!clientDataLoadingProgress) return;
    window.LoadingUI.hideProgressBar(clientDataLoadingProgress);
    clientDataLoadingDetail.textContent = totalTemplates > 0
        ? `Preparing ${totalTemplates} template${totalTemplates === 1 ? '' : 's'}…`
        : 'Finalizing the empty template list…';
    clientDataLoadingPercent.textContent = '0%';
    window.LoadingUI.showProgressBar(clientDataLoadingProgress, {
        mode: 'determinate',
        message: 'Rendering templates',
        total: Math.max(totalTemplates, 1)
    });
}

function updateClientDataRendering(current, total) {
    if (!clientDataLoadingProgress) return;
    const progressTotal = Math.max(total, 1);
    const progressCurrent = total === 0 ? 1 : current;
    window.LoadingUI.updateProgressBar(clientDataLoadingProgress, progressCurrent, progressTotal);
    clientDataLoadingPercent.textContent = `${Math.round((progressCurrent / progressTotal) * 100)}%`;
    clientDataLoadingDetail.textContent = total === 0
        ? 'No templates found. Page is ready.'
        : `${current} of ${total} templates ready`;
}

async function hideClientDataLoading(showCompletion = false) {
    if (!clientDataLoadingOverlay || !clientDataLoadingProgress) return;
    if (showCompletion) await new Promise(resolve => setTimeout(resolve, 250));
    window.LoadingUI.hideProgressBar(clientDataLoadingProgress);
    clientDataLoadingOverlay.style.display = 'none';
    clientDataLoadingOverlay.setAttribute('aria-hidden', 'true');
}

function formatDateForDisplay(dateStr) {
    if (!dateStr) return '—';
    try {
        const d = new Date(dateStr);
        return d.toLocaleDateString('en-GB', { day: '2-digit', month: '2-digit', year: 'numeric' });
    } catch {
        return dateStr;
    }
}

function getDateRangeFromFilter() {
    // Redesign Phase 6: sharedRangeFrom/sharedRangeTo (the popover's own
    // inputs) are now the persisted date-range state itself, not a copy of
    // the removed #dateStart/#dateEnd filter-bar inputs.
    const start = sharedRangeFromInput.value;
    const end = sharedRangeToInput.value;
    if (start && end) {
        if (start <= end) {
            return { start, end };
        } else {
            showStatus('⚠️ Start date must be before or equal to end date.', 'error');
            return null;
        }
    }
    return null;
}

function isSunday(dateStr) {
    if (!dateStr) return false;
    try {
        const d = new Date(dateStr);
        return d.getDay() === 0;
    } catch {
        return false;
    }
}

function formatCellValue(value, columnName = '') {
    if (value === undefined || value === null || value === '') return '—';
    if (typeof value === 'number') {
        if (window.formatAmount) {
            return window.formatAmount(value);
        }
        return value.toString();
    }
    if (typeof value === 'string' && value.length > 100) {
        return value.substring(0, 100) + '…';
    }
    return value;
}

function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value === undefined || value === null ? '' : String(value);
    return div.innerHTML;
}

// ---------- ROW REVISION HISTORY POPOVER (Phase 3) ----------
// Click-triggered popover on the S.No column's history icon. Fetches from
// the Phase 1 endpoint and renders generically off the "changes" array,
// so it works for any template without hardcoding field names.
function hideRowHistoryPopover() {
    nativeRevisionSequence++;
    rowHistoryPopover.style.display = 'none';
    activeHistoryIcon = null;
}

function renderRowHistoryPopover(data) {
    if (!data.revisions || data.revisions.length === 0) {
        rowHistoryBody.innerHTML = '<div style="color:#94a3b8;">No revision history.</div>';
        return;
    }
    rowHistoryBody.innerHTML = data.revisions.map(rev => {
        const changeRows = (rev.changes || []).map(c => `
            <tr>
                <td class="field-name">${escapeHtml(c.field)}</td>
                <td class="value-change">
                    <span class="old-value">${escapeHtml(formatCellValue(c.old_value))}</span>
                    &rarr;
                    <span class="new-value">${escapeHtml(formatCellValue(c.new_value))}</span>
                </td>
            </tr>`).join('');
        return `
            <div class="revision-block">
                <div class="revision-meta">${escapeHtml(rev.action)} &middot; ${escapeHtml(rev.changed_at)} &middot; ${escapeHtml(rev.user)}</div>
                <table>${changeRows}</table>
            </div>`;
    }).join('');
}

async function showRowHistoryPopover(icon, clientDataId) {
    // Position near the icon, clamped so it stays within the viewport.
    const rect = icon.getBoundingClientRect();
    const popoverWidth = 340;
    let left = rect.left;
    if (left + popoverWidth > window.innerWidth) {
        left = Math.max(8, window.innerWidth - popoverWidth - 8);
    }
    rowHistoryPopover.style.left = left + 'px';

    // Flip above the icon when there isn't enough room below (matches the
    // popover's CSS max-height of 320px), so it's never cut off at the
    // bottom of the viewport. Anchoring with "bottom" when flipped lets it
    // grow upward without needing to know its rendered height in advance.
    const popoverMaxHeight = 320;
    const spaceBelow = window.innerHeight - rect.bottom;
    if (spaceBelow >= popoverMaxHeight + 6) {
        rowHistoryPopover.style.top = (rect.bottom + 6) + 'px';
        rowHistoryPopover.style.bottom = 'auto';
    } else {
        rowHistoryPopover.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
        rowHistoryPopover.style.top = 'auto';
    }

    rowHistoryBody.innerHTML = '<div style="color:#94a3b8;">Loading...</div>';
    rowHistoryPopover.style.display = 'block';
    activeHistoryIcon = icon;

    if (nativeDataBank) {
        if (nativeDataBank.history) await nativeRowRevisions(icon, clientDataId);
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/api/data/rows/${clientDataId}/history`);
        if (!res.ok) throw new Error('Failed to fetch history');
        const data = await res.json();
        renderRowHistoryPopover(data);
    } catch (error) {
        rowHistoryBody.innerHTML = '<div style="color:#b91c1c;">Failed to load history.</div>';
    }
}

// ---------- SHARED DATE RANGE MODAL (Phase 2 — Unified date range) ----------
// One modal backs both entry points. As of Redesign Phase 6 (FINAL), this
// modal's own #sharedRangeFrom/#sharedRangeTo inputs ARE the single source
// of truth for the range (the old #dateStart/#dateEnd filter-bar inputs they
// used to mirror were removed along with the rest of the old filter bar).
// 3-fix: restyled from a small anchored popover (positioned via JS-computed
// coordinates) to a centred overlay modal — positionSharedRangePopover() is
// gone, and openSharedRangePopover() no longer takes an anchor element.

// mode: 'gate' (opened from a row's 👁️ icon, before the preview modal
// exists — confirming will open it) or 'live' (opened from inside the
// already-open modal — confirming just refreshes in place).
function openSharedRangePopover(mode, templateId, templateName, previewKind = 'raw') {
    // Redesign Phase 6: sharedRangeFrom/sharedRangeTo already hold the
    // persisted range themselves (no more #dateStart/#dateEnd to copy from).
    pendingPreviewOpen = (mode === 'gate') ? { templateId, templateName, previewKind } : null;
    sharedRangeConfirmBtn.textContent = (mode === 'gate') ? 'Open Preview' : 'Apply';
    sharedRangePopover.style.display = 'flex';
}

function closeSharedRangePopover() {
    sharedRangePopover.style.display = 'none';
    pendingPreviewOpen = null;
}

// Validates the popover's From/To (already the persisted shared range — see
// getDateRangeFromFilter()), then propagates the change to whichever views
// need it. fetchAvailability() (awaited) is the single point that also refreshes an
// already-open preview (see its own trailing isPreviewOpen check) — awaiting
// it here before opening a pending (gate-mode) preview means that check runs
// while isPreviewOpen is still false, so openPreview()'s own fetch is the
// only preview fetch that happens, avoiding a redundant duplicate call.
async function confirmSharedRangePopover() {
    const start = sharedRangeFromInput.value;
    const end = sharedRangeToInput.value;
    if (!start || !end) {
        showStatus('⚠️ Please select both a start and end date.', 'error');
        return;
    }
    if (start > end) {
        showStatus('⚠️ Start date must be before or equal to end date.', 'error');
        return;
    }

    // Redesign Phase 6: start/end already came straight from
    // sharedRangeFromInput/sharedRangeToInput — nothing to write back.
    const gateTarget = pendingPreviewOpen;
    closeSharedRangePopover();

    if (getSelectedClientId()) {
        await fetchAvailability();
    }

    if (gateTarget) {
        if (gateTarget.previewKind === 'classification') {
            openClassificationPreview(gateTarget.templateId, gateTarget.templateName);
        } else {
            openPreview(gateTarget.templateId, gateTarget.templateName);
        }
    }
}

// ---------- TEMPLATE CALENDAR MODAL (Coverage Calendar → per-template
// popover fix) ----------
// Shows one template's coverage calendar/legend/missing-dates report at a
// time, opened from a "Your Datasets" row's 📅 icon. Independent of the
// shared date range (sharedRangeFromInput/sharedRangeToInput), which stays
// dedicated to the "Your Datasets" table/Preview/Export flow.

const CALENDAR_MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'];
// Redesign Phase 3: full weekday names (was 3-letter abbreviations), per the
// prototype — buildCalendarDayGrid() just iterates this array, so it needed
// no change itself to pick up the longer labels.
const CALENDAR_DAY_HEADERS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

function updateCalendarMonthLabel() {
    calendarMonthLabel.textContent = `${CALENDAR_MONTH_NAMES[calendarMonth - 1]} ${calendarYear}`;
}

// Opens the modal for exactly one template — resets to the current month
// each time (a stale month carried over from a previously-viewed template
// would be confusing), sets the title, and fetches.
function openTemplateCalendarPopover(templateId, templateName) {
    currentPopoverTemplateId = templateId;
    currentPopoverTemplateName = templateName;
    calendarYear = new Date().getFullYear();
    calendarMonth = new Date().getMonth() + 1;
    templateCalendarModalTitle.textContent = `📅 ${templateName} — Coverage Calendar`;
    templateCalendarModal.style.display = 'flex';
    fetchTemplateCalendar();
}

function closeTemplateCalendarPopover() {
    calendarRequestSequence++;
    templateCalendarModal.style.display = 'none';
    currentPopoverTemplateId = null;
    currentPopoverTemplateName = '';
}

// Fetches Phase 3's per-day coverage endpoint for the single template the
// modal is currently open for, and renders it — renderCoverageCalendars()/
// renderMissingDatesReport() are unchanged, since both already handle
// data.templates generically as an array (a 1-item array needs no
// special-casing).
async function fetchTemplateCalendar() {
    if (!nativeReadAvailable()) return;
    const sequence = ++calendarRequestSequence;
    const template = currentPopoverTemplateId;
    const year = calendarYear, month = calendarMonth;
    const current = () => sequence === calendarRequestSequence && template === currentPopoverTemplateId
        && year === calendarYear && month === calendarMonth && Number(clientId) === Number(getSelectedClientId());
    updateCalendarMonthLabel();

    const clientId = getSelectedClientId();
    if (!clientId) {
        coverageCalendarsContainer.innerHTML = '<div class="calendar-empty-state">⚠️ No client selected. Please select a client from the Dashboard.</div>';
        return;
    }

    if (!currentPopoverTemplateId) {
        return;
    }

    const params = new URLSearchParams({
        client_id: clientId,
        year: calendarYear,
        month: calendarMonth
    });
    params.append('template_ids', currentPopoverTemplateId);

    coverageCalendarsContainer.innerHTML = '<div class="calendar-empty-state">⏳ Loading calendar...</div>';

    try {
        const res = await fetch(`${API_BASE}/api/data/availability/calendar?${params.toString()}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch coverage calendar');
        }
        const data = await res.json();
        if (!current()) return;
        assertNativeClient(res, clientId);
        if (data.year !== year || data.month !== month || data.templates.some(t => Number(t.template_id) !== Number(template))) throw new Error('Calendar scope changed. Retry.');
        renderCoverageCalendars(data);
    } catch (error) {
        if (!current()) return;
        console.error('Coverage calendar fetch error:', error);
        coverageCalendarsContainer.innerHTML = `<div class="calendar-empty-state">❌ Error loading calendar: ${escapeHtml(error.message)}</div>`;
    }
}

// Builds the 7-column day grid for one template's "days" array, with leading/
// trailing blank cells so the grid stays calendar-shaped. 0=Sunday matches
// both JS's Date.getDay() and the backend's own strftime('%w') convention.
function buildCalendarDayGrid(days, year, month) {
    const grid = document.createElement('div');
    grid.className = 'coverage-calendar-grid';

    CALENDAR_DAY_HEADERS.forEach(label => {
        const th = document.createElement('div');
        th.className = 'calendar-day-header';
        th.textContent = label;
        grid.appendChild(th);
    });

    const leadingBlanks = new Date(year, month - 1, 1).getDay();
    for (let i = 0; i < leadingBlanks; i++) {
        const blank = document.createElement('div');
        blank.className = 'calendar-day-cell empty';
        grid.appendChild(blank);
    }

    days.forEach(day => {
        const cell = document.createElement('div');
        cell.className = `calendar-day-cell ${day.status}`;
        cell.textContent = String(parseInt(day.date.slice(-2), 10));
        if (day.status === 'holiday' && day.label) {
            cell.title = day.label;
        }
        grid.appendChild(cell);
    });

    const totalCells = leadingBlanks + days.length;
    const trailingBlanks = (7 - (totalCells % 7)) % 7;
    for (let i = 0; i < trailingBlanks; i++) {
        const blank = document.createElement('div');
        blank.className = 'calendar-day-cell empty';
        grid.appendChild(blank);
    }

    return grid;
}

function buildCalendarLegend() {
    const legend = document.createElement('div');
    legend.className = 'calendar-legend';
    const items = [
        { cls: 'present', label: 'Present' },
        { cls: 'missing', label: 'Missing' },
        { cls: 'sunday', label: 'Sunday' },
        { cls: 'holiday', label: 'Holiday' }
    ];
    items.forEach(item => {
        const el = document.createElement('div');
        el.className = 'calendar-legend-item';
        const swatch = document.createElement('span');
        swatch.className = `calendar-legend-swatch ${item.cls}`;
        el.appendChild(swatch);
        el.appendChild(document.createTextNode(item.label));
        legend.appendChild(el);
    });
    return legend;
}

function renderCoverageCalendars(data) {
    coverageCalendarsContainer.innerHTML = '';
    if (!data.templates || data.templates.length === 0) {
        coverageCalendarsContainer.innerHTML = '<div class="calendar-empty-state">No data found for this template.</div>';
        renderMissingDatesReport(data);
        return;
    }

    data.templates.forEach(tpl => {
        const block = document.createElement('div');
        block.className = 'coverage-calendar-block';

        const heading = document.createElement('h3');
        heading.className = 'coverage-calendar-heading';
        heading.textContent = tpl.template_name;
        block.appendChild(heading);

        block.appendChild(buildCalendarLegend());
        block.appendChild(buildCalendarDayGrid(tpl.days, data.year, data.month));

        coverageCalendarsContainer.appendChild(block);
    });

    renderMissingDatesReport(data);
}

// ---------- MISSING DATES REPORT ----------
// Plain-language summary of missing days for the template the modal is open
// for — reuses the exact same "data" object renderCoverageCalendars() already
// received from fetchTemplateCalendar(), so no separate network call is
// made. Sundays and holidays are already excluded from "missing" by the
// underlying endpoint, so every date rendered here is a genuine gap. Still
// iterates data.templates generically (unchanged), even though it's always a
// 1-item array now.

// "2026-07-12" -> "12 Jul". Reuses CALENDAR_MONTH_NAMES (already defined
// above for the calendar heading) sliced to 3 letters, rather than a second
// month-name list.
function formatShortDate(isoDateStr) {
    const parts = isoDateStr.split('-'); // [year, month, day]
    const day = parseInt(parts[2], 10);
    const monthName = CALENDAR_MONTH_NAMES[parseInt(parts[1], 10) - 1].slice(0, 3);
    return `${day} ${monthName}`;
}

function renderMissingDatesReport(data) {
    missingDatesReportBody.innerHTML = '';

    if (!data.templates || data.templates.length === 0) {
        missingDatesReportBody.innerHTML = '<div class="calendar-empty-state">No data found for this template.</div>';
        return;
    }

    data.templates.forEach(tpl => {
        const missingDays = tpl.days.filter(d => d.status === 'missing');
        const row = document.createElement('div');
        row.className = 'missing-dates-template-row';

        const nameSpan = document.createElement('span');
        nameSpan.className = 'missing-dates-template-name';
        nameSpan.textContent = tpl.template_name;
        row.appendChild(nameSpan);

        if (missingDays.length === 0) {
            const okSpan = document.createElement('span');
            okSpan.className = 'missing-dates-complete';
            okSpan.textContent = ': ✅ No missing days this month';
            row.appendChild(okSpan);
        } else {
            const dayWord = missingDays.length === 1 ? 'day' : 'days';
            const countSpan = document.createElement('span');
            countSpan.className = 'missing-dates-count';
            countSpan.textContent = `: missing ${missingDays.length} ${dayWord} — `;
            row.appendChild(countSpan);

            const listSpan = document.createElement('span');
            listSpan.className = 'missing-dates-list';
            listSpan.textContent = missingDays.map(d => formatShortDate(d.date)).join(', ');
            row.appendChild(listSpan);
        }

        missingDatesReportBody.appendChild(row);
    });
}

function goToPrevMonth() {
    calendarMonth--;
    if (calendarMonth < 1) {
        calendarMonth = 12;
        calendarYear--;
    }
    fetchTemplateCalendar();
}

function goToNextMonth() {
    calendarMonth++;
    if (calendarMonth > 12) {
        calendarMonth = 1;
        calendarYear++;
    }
    fetchTemplateCalendar();
}

// ---------- SET DEFAULT DATE RANGE (Current Month) ----------
function setDefaultDateRange() {
    const now = new Date();
    const firstDay = new Date(now.getFullYear(), now.getMonth(), 1);
    const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    
    const formatDate = (d) => {
        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${y}-${m}-${day}`;
    };
    
    // Redesign Phase 6: writes into the popover's own inputs, which are now
    // the persisted date-range state (see getDateRangeFromFilter()).
    sharedRangeFromInput.value = formatDate(firstDay);
    sharedRangeToInput.value = formatDate(lastDay);
}

// Redesign Phase 6: reset helper for the "Your datasets" table's empty/error
// state (was shared with the now-removed old Availability Table, which had
// its own #availabilityBody/#noDataMessage — those are gone, so this now
// only resets the one remaining table).
// Coverage Calendar → per-template popover fix: also clears the "Your
// Datasets" search term/input, so a stale search doesn't linger visually
// across a client switch or error state.
function resetDatasetTables(message) {
    yourDatasetsBody.innerHTML = '';
    yourDatasetsNoData.style.display = 'block';
    yourDatasetsNoData.innerHTML = message;
    yourDatasetsSearchTerm = '';
    yourDatasetsSearchInput.value = '';
    yourDatasetsSearchClearBtn.style.display = 'none';
}

// ---------- FETCH AVAILABILITY DATA ----------
const AVAILABILITY_REQUEST_TIMEOUT_MS = 15000;
const AVAILABILITY_METRICS_TIMEOUT_MS = 60000;

async function fetchAvailabilityEndpoint(url, timeoutMs = AVAILABILITY_REQUEST_TIMEOUT_MS) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const fetcher = window.ApiUtils ? window.ApiUtils.apiFetch : fetch;
        return await fetcher(url, { signal: controller.signal });
    } finally {
        clearTimeout(timeoutId);
    }
}

function getAvailabilityCacheKey(clientId, dateRange) {
    return `${clientId}|${dateRange.start}|${dateRange.end}`;
}

async function loadAvailabilityPayload(clientId, dateRange) {
    const cacheKey = getAvailabilityCacheKey(clientId, dateRange);
    const cached = availabilityCache.get(cacheKey);
    if (cached) return cached;

    const pending = availabilityInFlight.get(cacheKey);
    if (pending) return pending;

    const cacheGeneration = previewCacheGeneration;
    const request = (async () => {
        const params = new URLSearchParams({
            client_id: clientId,
            date_start: dateRange.start,
            date_end: dateRange.end,
            include_metrics: 'false'
        });
        const res = await fetchAvailabilityEndpoint(`${API_BASE}/api/data/availability?${params.toString()}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch availability');
        }
        const data = await res.json();
        assertNativeClient(res, clientId);
        const payload = { data, clientId };
        if (cacheGeneration === previewCacheGeneration) {
            availabilityCache.set(cacheKey, payload);
            boundDataBankCache(availabilityCache);
        }
        return payload;
    })();

    availabilityInFlight.set(cacheKey, request);
    try {
        return await request;
    } finally {
        if (availabilityInFlight.get(cacheKey) === request) {
            availabilityInFlight.delete(cacheKey);
        }
    }
}

async function loadAvailabilityMetrics(clientId, cacheGeneration = previewCacheGeneration) {
    const cacheKey = `${clientId}|${cacheGeneration}`;
    const cached = availabilityMetricsCache.get(cacheKey);
    if (cached) return cached.payload;

    const pending = availabilityMetricsInFlight.get(cacheKey);
    if (pending) return pending;

    const request = (async () => {
        const res = await fetchAvailabilityEndpoint(
            `${API_BASE}/api/data/availability/metrics?client_id=${encodeURIComponent(clientId)}`,
            AVAILABILITY_METRICS_TIMEOUT_MS,
        );
        if (!res.ok) {
            let detail = 'Could not load document counts and size estimates.';
            try {
                const err = await res.json();
                detail = err.detail || detail;
            } catch (_) { /* Keep the safe fallback message for non-JSON errors. */ }
            throw new Error(detail);
        }
        const payload = await res.json();
        assertNativeClient(res, clientId);
        if (Number(payload.client_id) !== Number(clientId) || !Array.isArray(payload.templates)) {
            throw new Error('The data service returned metrics for an unexpected client.');
        }
        if (cacheGeneration === previewCacheGeneration) {
            availabilityMetricsCache.set(cacheKey, { clientId: Number(clientId), payload });
            boundDataBankCache(availabilityMetricsCache);
        }
        return payload;
    })();

    availabilityMetricsInFlight.set(cacheKey, request);
    try {
        return await request;
    } finally {
        if (availabilityMetricsInFlight.get(cacheKey) === request) {
            availabilityMetricsInFlight.delete(cacheKey);
        }
    }
}

function mergeAvailabilityMetrics(data, metricsPayload) {
    const metricLookup = new Map((metricsPayload.templates || []).map(item => [Number(item.template_id), item]));
    return data.map(item => {
        const metrics = metricLookup.get(Number(item.template_id));
        return metrics ? { ...item, ...metrics, metrics_unavailable: false } : {
            ...item,
            document_count: null,
            estimated_size_bytes: null,
            estimated_size_formatted: null,
            metrics_unavailable: true,
        };
    });
}

function isCurrentAvailabilityRequest(requestSequence, clientId, cacheGeneration) {
    return requestSequence === availabilityRequestSequence
        && cacheGeneration === previewCacheGeneration
        && Number(getSelectedClientId()) === Number(clientId);
}

function updateRenderedAvailabilityMetrics(metricsByTemplate) {
    for (const row of yourDatasetsBody.querySelectorAll('tr[data-template-id]')) {
        const metrics = metricsByTemplate.get(Number(row.dataset.templateId));
        const documentCell = row.querySelector('[data-availability-metric="documents"]');
        const sizeCell = row.querySelector('[data-availability-metric="size"]');
        if (documentCell) {
            documentCell.textContent = metrics?.document_count ?? 'Unavailable';
            documentCell.title = metrics ? '' : 'This metric was not returned by the data service.';
        }
        if (sizeCell) {
            sizeCell.textContent = metrics?.estimated_size_formatted || 'Unavailable';
            sizeCell.title = metrics ? 'Estimated storage size (row count × average row size)' : 'This metric was not returned by the data service.';
        }
    }
}

async function loadAndApplyAvailabilityMetrics(clientId, requestSequence, cacheGeneration) {
    try {
        const payload = await loadAvailabilityMetrics(clientId, cacheGeneration);
        if (!isCurrentAvailabilityRequest(requestSequence, clientId, cacheGeneration)) return;

        const metricsByTemplate = new Map(payload.templates.map(item => [Number(item.template_id), item]));
        availabilityData = mergeAvailabilityMetrics(availabilityData, payload);
        statsClientDbSize.textContent = payload.client_size_formatted || '—';
        updateRenderedAvailabilityMetrics(metricsByTemplate);
    } catch (error) {
        if (!isCurrentAvailabilityRequest(requestSequence, clientId, cacheGeneration)) return;
        console.warn('Data Bank metrics unavailable:', error);
        availabilityData = availabilityData.map(item => ({
            ...item,
            document_count: null,
            estimated_size_bytes: null,
            estimated_size_formatted: null,
            metrics_unavailable: true,
        }));
        statsClientDbSize.textContent = 'Unavailable';
        for (const cell of yourDatasetsBody.querySelectorAll('[data-availability-metric]')) {
            cell.textContent = 'Unavailable';
            cell.title = 'This metric could not be loaded. Refresh the Data Bank to retry.';
        }
    }
}

function invalidateDataBankCaches({ clientId = null, templateId = null } = {}) {
    nativeLoadedExportKey = null; updateNativeExportButton();
    previewCacheGeneration += 1;
    availabilityRequestSequence += 1;
    if (nativeDataBank) { templateColumnsCache = {}; templateSubtotalDefaultCache = {}; templatePurposeCache = {}; }

    for (const key of availabilityInFlight.keys()) {
        if (clientId === null || key.startsWith(`${clientId}|`)) {
            availabilityInFlight.delete(key);
        }
    }

    for (const [key, entry] of previewPageCache.entries()) {
        if ((clientId === null || entry.clientId === clientId) &&
            (templateId === null || entry.templateId === templateId)) {
            previewPageCache.delete(key);
        }
    }

    for (const [key, entry] of availabilityCache.entries()) {
        if (clientId === null || entry.clientId === clientId) {
            availabilityCache.delete(key);
        }
    }

    for (const [key, entry] of availabilityMetricsCache.entries()) {
        if (clientId === null || entry.clientId === Number(clientId)) {
            availabilityMetricsCache.delete(key);
        }
    }
}

async function fetchAvailability() {
    if (!nativeReadAvailable()) return;
    const requestSequence = ++availabilityRequestSequence;
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ Please select a client from the Dashboard first.', 'error');
        resetDatasetTables('⚠️ No client selected. Please go to the Dashboard and select a client.');
        return;
    }

    const dateRange = getDateRangeFromFilter();
    if (!dateRange) {
        showStatus('⚠️ Please select a valid date range (start ≤ end).', 'error');
        return;
    }
    
    // Redesign Phase 6: no per-template filter anymore — "Your datasets"
    // always lists every template, matching the removed old filter bar's
    // "All Templates" default behavior with no way to narrow it further.
    showClientDataLoading();
    const cacheGeneration = previewCacheGeneration;
    
    try {
        const { data } = await loadAvailabilityPayload(clientId, dateRange);
        if (!isCurrentAvailabilityRequest(requestSequence, clientId, cacheGeneration)) return;

        const cachedMetrics = availabilityMetricsCache.get(`${clientId}|${cacheGeneration}`);
        availabilityData = cachedMetrics
            ? mergeAvailabilityMetrics(data, cachedMetrics.payload)
            : data.map(item => ({
                ...item,
                document_count: null,
                estimated_size_bytes: null,
                estimated_size_formatted: null,
                metrics_unavailable: false,
            }));

        // Update stats - use total_row_count (unfiltered) for Total Records.
        const totalRows = data.reduce((sum, item) => sum + (item.total_row_count || 0), 0);
        const totalTemplates = data.length;

        statsTotalRecords.textContent = totalRows;
        statsTotalTemplates.textContent = totalTemplates;

        statsClientDbSize.textContent = cachedMetrics?.payload.client_size_formatted || 'Calculating…';

        // Overdue Uploads (Phase D2, FINAL): not date-range-scoped (it's a
        // MAX(extract_date) freshness check), so fetched once per client
        // selection rather than re-fetched on every date-range change.
        fetchOverdueUploads(clientId);

        // Redesign Phase 5: new "Your datasets" table reflects the exact
        // same fetch/data — no separate network call. (The old Availability
        // Table's own renderAvailabilityTable() was removed in Phase 6.)
        // Coverage Calendar → per-template popover fix: goes through the
        // search-aware helper so a re-fetch (e.g. date range change) re-applies
        // whatever search text is currently active instead of discarding it.
        const visibleTemplateCount = yourDatasetsSearchTerm
            ? data.filter(item => item.template_name.toLowerCase().includes(yourDatasetsSearchTerm)).length
            : totalTemplates;
        startClientDataRendering(visibleTemplateCount);
        await renderFilteredYourDatasetsTable(clientDataLoadingProgress);
        updateClientDataRendering(visibleTemplateCount, visibleTemplateCount);
        await hideClientDataLoading(true);

        if (totalTemplates === 0) {
            showStatus('ℹ️ No templates found for the selected criteria.', 'info');
        } else {
            showStatus(`✅ Found ${totalTemplates} template(s) with ${totalRows} total rows.`, 'success');
        }

        if (!cachedMetrics) {
            // Slow all-time metrics must never hold the dataset list behind its
            // loading state. Their failures update only the metric cells.
            void loadAndApplyAvailabilityMetrics(clientId, requestSequence, cacheGeneration);
        }

        // Phase 2 (Unified date range): the shared range may have just changed
        // (this is the main-sheet side of the two-way sync) — keep an open
        // preview in step with it immediately, rather than only on next open.
        if (isPreviewOpen) {
            currentPage = 1;
            fetchPreviewData();
        }

    } catch (error) {
        if (!isCurrentAvailabilityRequest(requestSequence, clientId, cacheGeneration)) return;
        await hideClientDataLoading();
        if (!isCurrentAvailabilityRequest(requestSequence, clientId, cacheGeneration)) return;
        console.error('Fetch availability error:', error);
        const reason = error.name === 'AbortError'
            ? 'The data service did not respond within 15 seconds.'
            : (error.message || 'The data service returned an error.');
        showStatus('❌ Data Bank unavailable: ' + reason, 'error');
        resetDatasetTables('❌ Uploaded data could not be loaded. No zero-record result was assumed; retry or check the data service.');
        statsTotalRecords.textContent = '—';
        statsTotalTemplates.textContent = '—';
        statsClientDbSize.textContent = '—';
    }
}

// ---------- OVERDUE UPLOADS (Phase D2, FINAL) ----------
// Single-client list from GET /api/data/overdue-uploads, already sorted
// most-overdue-first by the backend. No Client column (this view is already
// scoped to one client). Row-click-to-jump-to-upload-page deferred per brief.
function getOverdueSeverityBadge(daysOverdue) {
    // Same fixed threshold/badge convention as the Main Dashboard's version
    // (static/script.js) — exact thresholds left to implementation per the brief.
    const cls = daysOverdue >= 5 ? 'failed' : 'pending';
    return `<span class="status-badge ${cls}">${daysOverdue}</span>`;
}

function renderOverdueUploadsTable() {
    if (!overdueUploadsTableBody) return;
    if (overdueUploadsData.length === 0) {
        overdueUploadsTableBody.innerHTML = '';
        if (overdueUploadsNoData) overdueUploadsNoData.style.display = 'block';
        return;
    }
    if (overdueUploadsNoData) overdueUploadsNoData.style.display = 'none';
    overdueUploadsTableBody.innerHTML = overdueUploadsData.map(item => `
        <tr>
            <td>${escapeHtml(item.template_name)}</td>
            <td>${formatDateForDisplay(item.last_upload_date)}</td>
            <td>${escapeHtml(item.frequency)}</td>
            <td>${getOverdueSeverityBadge(item.days_overdue)}</td>
        </tr>
    `).join('');
}

async function fetchOverdueUploads(clientId) {
    const generation = previewCacheGeneration;
    if (!clientId) {
        overdueUploadsData = [];
        if (statsOverdueUploads) statsOverdueUploads.textContent = '0';
        renderOverdueUploadsTable();
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/api/data/overdue-uploads?client_id=${clientId}`);
        if (generation !== previewCacheGeneration || Number(clientId) !== Number(getSelectedClientId())) return;
        if (res.ok) {
            const data = await res.json();
            if (generation !== previewCacheGeneration || Number(clientId) !== Number(getSelectedClientId())) return;
            assertNativeClient(res, clientId);
            if (Number(data.client_id) !== Number(clientId)) throw new Error('Overdue scope changed.');
            overdueUploadsData = data.overdue || [];
        } else {
            overdueUploadsData = [];
        }
    } catch (error) {
        if (generation !== previewCacheGeneration || Number(clientId) !== Number(getSelectedClientId())) return;
        overdueUploadsData = [];
    }
    if (statsOverdueUploads) statsOverdueUploads.textContent = overdueUploadsData.length;
    renderOverdueUploadsTable();
}

if (overdueUploadsTile) {
    overdueUploadsTile.addEventListener('click', () => {
        if (!overdueUploadsPanel) return;
        overdueUploadsPanel.style.display = overdueUploadsPanel.style.display === 'none' ? 'block' : 'none';
    });
}

// Overdue Uploads Phase C2: builds the Frequency cell — an inline
// Daily/Weekly/Monthly dropdown, per client+template. Saves on change via
// PUT /api/data/upload-frequency (Phase C1); on failure the select reverts
// to its previous value rather than leaving a stale/wrong-looking selection.
const UPLOAD_FREQUENCY_OPTIONS = ['Daily', 'Weekly', 'Monthly'];

function buildFrequencyCell(item) {
    const tdFrequency = document.createElement('td');
    tdFrequency.style.textAlign = 'center';
    if (nativeDataBank) { tdFrequency.textContent = item.upload_frequency || 'Daily'; tdFrequency.title = 'Frequency editing is awaiting migration.'; return tdFrequency; }

    const select = document.createElement('select');
    UPLOAD_FREQUENCY_OPTIONS.forEach(freq => {
        const option = document.createElement('option');
        option.value = freq;
        option.textContent = freq;
        select.appendChild(option);
    });
    select.value = item.upload_frequency || 'Daily';

    select.addEventListener('change', async () => {
        const previousValue = select.dataset.lastValue || 'Daily';
        const newValue = select.value;
        const clientId = getSelectedClientId();
        const previousDisabled = select.disabled;
        const globalLoadingToken = window.LoadingUI
            ? window.LoadingUI.startGlobalLoading(select)
            : null;
        select.disabled = true;

        try {
            const res = await fetch(`${API_BASE}/api/data/upload-frequency`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    client_id: clientId,
                    template_id: item.template_id,
                    frequency: newValue
                })
            });
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Failed to update upload frequency');
            }
            item.upload_frequency = newValue;
            select.dataset.lastValue = newValue;
            showStatus('✅ Upload frequency updated.', 'success');
        } catch (error) {
            console.error('Update upload frequency error:', error);
            select.value = previousValue;
            showStatus('❌ Error: ' + error.message, 'error');
        } finally {
            select.disabled = previousDisabled;
            if (globalLoadingToken) window.LoadingUI.stopGlobalLoading(globalLoadingToken);
        }
    });

    select.dataset.lastValue = select.value;
    tdFrequency.appendChild(select);
    return tdFrequency;
}

// Status column removal fix: builds the Coverage cell — just the 📅 icon,
// now in its own column (previously shared the Actions cell with
// Preview/Export/Delete). Always shown, even for a template with zero rows —
// a missing-days calendar is exactly the useful thing to see for a template
// with no data yet.
function buildCoverageCell(item) {
    const tdCoverage = document.createElement('td');
    tdCoverage.style.textAlign = 'center';

    const calendarBtn = document.createElement('button');
    calendarBtn.className = 'action-btn calendar';
    calendarBtn.textContent = '📅';
    calendarBtn.title = 'Coverage calendar';
    calendarBtn.setAttribute('data-tooltip', 'View coverage calendar & missing dates');
    calendarBtn.addEventListener('click', () => {
        openTemplateCalendarPopover(item.template_id, item.template_name);
    });
    tdCoverage.appendChild(calendarBtn);

    return tdCoverage;
}

// Redesign Phase 5: builds the Preview/Export/Delete Actions cell, shared by
// "Your datasets" table (the old Availability Table that originally also used
// this helper was removed in Redesign Phase 6).
function buildDatasetActionButtons(item) {
    const tdActions = document.createElement('td');
    tdActions.style.textAlign = 'center';
    tdActions.style.whiteSpace = 'nowrap';

    // Preview button - enabled if there's ANY data (total_row_count > 0)
    if (item.total_row_count > 0) {
        const previewBtn = document.createElement('button');
        previewBtn.className = 'action-btn preview';
        previewBtn.textContent = '👁️';
        previewBtn.title = 'Preview Data';
        previewBtn.setAttribute('data-tooltip', 'Preview data for this template');
        // Phase 2 (Unified date range): the 👁️ icon always opens the shared
        // date-range modal first (gate mode) — the preview itself only
        // opens once a range is confirmed there.
        previewBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            openSharedRangePopover('gate', item.template_id, item.template_name);
        });
        tdActions.appendChild(previewBtn);
        // Cashflows classification is deliberately separate from the raw eye
        // preview: it is read-only and shows current Account Control Centre
        // assignments alongside the unchanged source columns.
        if (!nativeDataBank && (item.template_name || '').trim().toLowerCase() === 'transaction report') {
            const classificationBtn = document.createElement('button');
            classificationBtn.className = 'action-btn preview';
            classificationBtn.textContent = '🏷️';
            classificationBtn.title = 'Cashflows classification preview';
            classificationBtn.setAttribute('data-tooltip', 'View read-only Cashflows classification');
            classificationBtn.style.marginLeft = '6px';
            classificationBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                openSharedRangePopover('gate', item.template_id, item.template_name, 'classification');
            });
            tdActions.appendChild(classificationBtn);
        }

        // Export button
        const exportBtn = document.createElement('button');
        exportBtn.className = 'action-btn export';
        exportBtn.textContent = '📥';
        exportBtn.title = 'Export to Excel';
        exportBtn.setAttribute('data-tooltip', 'Export all data for this template');
        exportBtn.style.marginLeft = '6px';
        exportBtn.addEventListener('click', () => {
            exportDataset(item.template_id, item.template_name);
        });
        if (!nativeDataBank) tdActions.appendChild(exportBtn);
        else if (nativeDataBank.exports) {
            exportBtn.title = 'Choose matching data and export';
            const nativeExport = document.createElement('button'); nativeExport.className = 'action-btn export';
            nativeExport.textContent = '📥'; nativeExport.title = 'Choose matching data and export';
            nativeExport.addEventListener('click', () => openSharedRangePopover('gate', item.template_id, item.template_name));
            tdActions.appendChild(nativeExport);
        }

    }

    // Deletion remains available when only changed/deleted upload history is
    // left, so users can clear the complete template scope after backing it up.
    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'action-btn delete';
    deleteBtn.textContent = '🗑️';
    deleteBtn.title = 'Back up and delete this template’s data';
    deleteBtn.setAttribute('aria-label', `Back up and delete data for ${item.template_name}`);
    deleteBtn.setAttribute('data-tooltip', 'Download current data and changed/deleted history, then delete');
    deleteBtn.style.marginLeft = '6px';
    deleteBtn.style.color = '#dc2626';
    deleteBtn.addEventListener('click', () => {
        deleteTemplateData(item.template_id, item.template_name, deleteBtn);
    });
    if (!nativeDataBank || nativePurgeAvailable()) tdActions.appendChild(deleteBtn);

    // History remains available when a complete upload has removed every active
    // row from this template; that is when users may need the deletion record most.
    const historyBtn = document.createElement('button');
    historyBtn.className = 'action-btn preview';
    historyBtn.textContent = '🕘';
    historyBtn.title = 'Changed/Deleted data';
    historyBtn.setAttribute('aria-label', `Changed/Deleted data for ${item.template_name}`);
    historyBtn.setAttribute('data-tooltip', 'Review transactions changed or removed by uploads');
    historyBtn.style.marginLeft = '6px';
    historyBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        openUploadHistory(item);
    });
    if (!nativeDataBank || nativeDataBank.history) tdActions.appendChild(historyBtn);
    if (nativeDataBank?.exports && nativeDataBank.history && nativeDataBank.canBackup) {
        const backup = document.createElement('button'); backup.className = 'action-btn export'; backup.textContent = '📦';
        backup.title = 'Back up all current data and history'; backup.setAttribute('aria-label', `Back up all current data and history for ${item.template_name}`);
        backup.addEventListener('click', () => nativeDownload(item.template_id, true)); tdActions.appendChild(backup);
    }

    return tdActions;
}

// ---------- RENDER "YOUR DATASETS" TABLE (Redesign Phase 5) ----------
// New, simplified table (Template / Full Date Range / No of Documents /
// Coverage / Actions — Status replaced by Coverage per the Status column
// removal fix; Total Rows renamed to No of Documents per the unique-document
// count fix) fed by the exact same data fetchAvailability() already retrieves.
// The old Availability Table this used to coexist with (with its extra "#"/
// Filtered Rows/Sunday columns) was removed in Redesign Phase 6 — this is
// now the sole datasets table on the page, and (Coverage Calendar →
// per-template popover fix) also the single unified template list, filtered
// by renderFilteredYourDatasetsTable() below.
async function renderYourDatasetsTable(data, emptyMessage = 'No templates found for the selected criteria.', progressTarget = null) {
    yourDatasetsBody.innerHTML = '';
    if (!data || data.length === 0) {
        yourDatasetsNoData.style.display = 'block';
        yourDatasetsNoData.innerHTML = emptyMessage;
        if (progressTarget) updateClientDataRendering(0, 0);
        return;
    }
    yourDatasetsNoData.style.display = 'none';

    for (let index = 0; index < data.length; index++) {
        const item = data[index];
        const tr = document.createElement('tr');
        tr.dataset.templateId = item.template_id;

        // Template Name
        const tdName = document.createElement('td');
        tdName.textContent = item.template_name;
        tdName.style.fontWeight = '500';
        tr.appendChild(tdName);

        // Frequency (Overdue Uploads Phase C2) — inline-editable Daily/Weekly/
        // Monthly dropdown, seeded from the C1-extended /api/data/availability
        // response (defaults 'Daily' server-side when no override is saved).
        tr.appendChild(buildFrequencyCell(item));

        // Full Date Range (unfiltered) — same fallback logic as the old table.
        const tdDate = document.createElement('td');
        if (item.date_min && item.date_max) {
            tdDate.textContent = `${formatDateForDisplay(item.date_min)} – ${formatDateForDisplay(item.date_max)}`;
        } else {
            tdDate.textContent = '—';
            tdDate.style.color = '#94a3b8';
        }
        tr.appendChild(tdDate);

        // No of Documents (all data, unfiltered) — unique documents/vouchers,
        // not raw rows: a multi-line voucher counts once. Sourced from the
        // API's document_count field (total_row_count itself is untouched,
        // still used by the metric tile/Preview-gate/delete-count elsewhere).
        const tdDocCount = document.createElement('td');
        tdDocCount.textContent = item.metrics_unavailable ? 'Unavailable' : (item.document_count ?? 'Calculating…');
        tdDocCount.style.textAlign = 'center';
        tdDocCount.style.fontWeight = '600';
        tdDocCount.dataset.availabilityMetric = 'documents';
        tdDocCount.setAttribute('data-tooltip', 'Unique documents across all dates for this template');
        tr.appendChild(tdDocCount);

        // Database (Database Size Visibility, Phase A2) — estimated storage
        // footprint for this template (all data, unfiltered), sourced from
        // the API's estimated_size_formatted field.
        const tdSize = document.createElement('td');
        tdSize.textContent = item.metrics_unavailable ? 'Unavailable' : (item.estimated_size_formatted || 'Calculating…');
        tdSize.style.textAlign = 'center';
        tdSize.dataset.availabilityMetric = 'size';
        tdSize.setAttribute('data-tooltip', 'Estimated storage size (row count × avg row size)');
        tr.appendChild(tdSize);

        // Coverage — the 📅 icon, now its own column (was previously part of
        // the Actions cell; see buildCoverageCell()).
        tr.appendChild(buildCoverageCell(item));

        // Actions — same shared buttons/click handlers as the old table.
        tr.appendChild(buildDatasetActionButtons(item));

        yourDatasetsBody.appendChild(tr);
        if (progressTarget) {
            updateClientDataRendering(index + 1, data.length);
            // Yield so the browser paints each real template-progress step.
            await new Promise(resolve => requestAnimationFrame(resolve));
        }
    }
}

// Coverage Calendar → per-template popover fix: filters the full fetched
// set (availabilityData, from fetchAvailability()) by template-name
// substring and renders through the unchanged renderYourDatasetsTable() —
// used both by the search input's own listener and in place of
// fetchAvailability()'s direct render call, so a re-fetch (e.g. a date range
// change) re-applies whatever search text is currently active rather than
// silently discarding it.
async function renderFilteredYourDatasetsTable(progressTarget = null) {
    const filtered = yourDatasetsSearchTerm
        ? availabilityData.filter(item => item.template_name.toLowerCase().includes(yourDatasetsSearchTerm))
        : availabilityData;
    const emptyMessage = (yourDatasetsSearchTerm && availabilityData.length > 0)
        ? 'No templates match your search.'
        : 'No templates found for the selected criteria.';
    await renderYourDatasetsTable(filtered, emptyMessage, progressTarget);
}

function deleteTemplateData(templateId, templateName, triggerTarget = null) {
    if (nativeDataBank && !nativePurgeAvailable()) return;
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected.', 'error');
        return;
    }
    if (deleteTemplateInFlight.has(String(templateId))) return;
    pendingTemplateDelete = {
        clientId: Number(clientId),
        templateId: Number(templateId),
        templateName: templateName || `Template ${templateId}`,
        triggerTarget,
    };
    templateDeleteTitle.textContent = `Back up and delete ${pendingTemplateDelete.templateName}`;
    const linkedVerificationOption = ensureLinkedVerificationPurgeOption();
    if (linkedVerificationOption) linkedVerificationOption.checked = false;
    templateDeleteStatus.textContent = '';
    templateDeleteStatus.style.display = 'none';
    backupAndDeleteTemplateBtn.disabled = false;
    cancelTemplateDeleteBtn.disabled = false;
    closeTemplateDeleteBtn.disabled = false;
    templateDeleteModal.style.display = 'flex';
    if (nativeDataBank) {
        document.getElementById('nativePurgeConfirmationPanel').hidden = false;
        document.getElementById('nativePurgeConfirmation').value = '';
        backupAndDeleteTemplateBtn.disabled = true;
    }
    backupAndDeleteTemplateBtn.focus();
}

async function fetchTemplateBackupRows(clientId, templateId) {
    const rows = [];
    let page = 1;
    let metadata = null;
    do {
        const params = new URLSearchParams({ client_id: String(clientId), page: String(page), limit: '1000' });
        const response = await fetch(`${API_BASE}/api/data/template/${templateId}/backup-rows?${params}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Could not read current template data.');
        metadata = data;
        rows.push(...(data.rows || []));
        page += 1;
    } while (metadata && page <= metadata.total_pages);
    return { rows, columns: metadata ? metadata.columns || [] : [] };
}

async function fetchTemplateBackupHistory(clientId, templateId) {
    const rows = [];
    let page = 1;
    let metadata = null;
    do {
        const params = new URLSearchParams({
            client_id: String(clientId), template_id: String(templateId),
            page: String(page), limit: '100', action: 'all',
        });
        const response = await fetch(`${API_BASE}/api/data/history?${params}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Could not read changed/deleted history.');
        metadata = data;
        rows.push(...(data.rows || []));
        page += 1;
    } while (metadata && page <= metadata.total_pages);
    return { rows, columns: metadata ? metadata.columns || [] : [] };
}

function downloadTemplateBackup(templateName, templateColumns, currentRows, historyRows) {
    if (typeof XLSX === 'undefined') throw new Error('Excel workbook support is unavailable. No data was deleted.');
    const columnNames = templateColumns.map((column) => column.column_name).filter(Boolean);
    const dataHeaders = columnNames.length
        ? columnNames
        : (currentRows[0] ? Object.keys(currentRows[0]) : []);
    if (currentRows.length + 1 > 1048576 || historyRows.length + 1 > 1048576) {
        throw new Error('This template exceeds Excel’s worksheet row limit. No data was deleted.');
    }

    const workbook = XLSX.utils.book_new();
    const toCellValue = (value) => value && typeof value === 'object'
        ? JSON.stringify(value)
        : (value ?? '');
    const dataSheet = XLSX.utils.aoa_to_sheet([
        dataHeaders,
        ...currentRows.map((row) => dataHeaders.map((name) => toCellValue(row[name]))),
    ]);
    XLSX.utils.book_append_sheet(workbook, dataSheet, 'Data');

    const historyHeaders = ['Action', 'Changed At', 'Changed By', ...dataHeaders, 'Changed Fields'];
    const historySheet = XLSX.utils.aoa_to_sheet([
        historyHeaders,
        ...historyRows.map((entry) => [
            entry.action || '', entry.changed_at || '', entry.changed_by || '',
            ...dataHeaders.map((name) => toCellValue((entry.row_data || {})[name])),
            entry.changed_fields ? JSON.stringify(entry.changed_fields) : '',
        ]),
    ]);
    XLSX.utils.book_append_sheet(workbook, historySheet, 'Changed-Deleted History');

    const bytes = XLSX.write(workbook, { bookType: 'xlsx', type: 'array' });
    if (!bytes || !bytes.byteLength) throw new Error('The Excel backup could not be created. No data was deleted.');
    const safeName = (templateName || 'template').replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').slice(0, 100);
    const date = new Date().toISOString().slice(0, 10);
    const blob = new Blob([bytes], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${safeName}_backup_${date}.xlsx`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 30000);
}

async function runTemplateBackupAndDelete() {
    if (nativeDataBank) { await runNativeTemplatePurge(); return; }
    if (!pendingTemplateDelete) return;
    const { clientId, templateId, templateName, triggerTarget } = pendingTemplateDelete;
    const includeLinkedVerification = !nativeDataBank
        && !!linkedVerificationPurgeCheckbox?.checked;
    const key = String(templateId);
    if (deleteTemplateInFlight.has(key)) return;
    deleteTemplateInFlight.add(key);
    const previousDisabled = triggerTarget ? triggerTarget.disabled : undefined;
    if (triggerTarget) triggerTarget.disabled = true;
    backupAndDeleteTemplateBtn.disabled = true;
    cancelTemplateDeleteBtn.disabled = true;
    closeTemplateDeleteBtn.disabled = true;
    const globalLoadingToken = window.LoadingUI
        ? window.LoadingUI.startGlobalLoading(backupAndDeleteTemplateBtn)
        : null;
    let downloadStarted = false;
    let deletionApplied = false;
    let linkedVerificationSummary = '';

    try {
        templateDeleteStatus.textContent = 'Checking the deletion scope…';
        templateDeleteStatus.style.display = 'block';
        showStatus('⏳ Preparing the template backup…', 'loading');

        const previewResponse = await fetch(
            `${API_BASE}/api/clients/${clientId}/data-purge-preview?template_id=${templateId}`
            + (includeLinkedVerification ? '&include_linked_verification=true' : '')
        );
        const preview = await previewResponse.json();
        if (!previewResponse.ok) throw new Error(preview.detail || 'Could not check the deletion scope.');
        if (includeLinkedVerification) {
            const linkedSummary = Object.entries(preview.linked_verification || {})
                .filter(([, count]) => Number(count) > 0)
                .map(([name, count]) => `${name.replaceAll('_', ' ')}: ${Number(count).toLocaleString()}`)
                .join(', ');
            linkedVerificationSummary = linkedSummary
                ? `The Excel backup does not contain Verification data. This action will also permanently remove: ${linkedSummary}.`
                : 'The Excel backup does not contain Verification data. No linked Verification records were found.';
            templateDeleteStatus.textContent = linkedVerificationSummary;
        }
        if (!preview.can_apply) {
            const protectedItems = Object.entries(preview.protected || {})
                .filter(([, count]) => Number(count) > 0)
                .map(([name, count]) => `${name}: ${count}`)
                .join(', ');
            throw new Error(`Deletion is blocked by linked verification or evidence${protectedItems ? ` (${protectedItems})` : ''}.`);
        }

        templateDeleteStatus.textContent = `${linkedVerificationSummary ? `${linkedVerificationSummary} ` : ''}Collecting all current data and changed/deleted history…`;
        const [current, history] = await Promise.all([
            fetchTemplateBackupRows(clientId, templateId),
            fetchTemplateBackupHistory(clientId, templateId),
        ]);
        const columns = current.columns.length ? current.columns : history.columns;
        downloadTemplateBackup(templateName, columns, current.rows, history.rows);
        downloadStarted = true;
        templateDeleteStatus.textContent = `${linkedVerificationSummary ? `${linkedVerificationSummary} ` : ''}Backup download started. Deleting the template data…`;
        showStatus('⏳ Backup prepared; deleting template data…', 'loading');
        await new Promise((resolve) => window.setTimeout(resolve, 150));

        const response = await fetch(`${API_BASE}/api/clients/${clientId}/data-purge`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                preview_hash: preview.preview_hash,
                confirmation: 'PERMANENTLY DELETE SELECTED DATA',
                template_id: templateId,
                include_linked_verification: includeLinkedVerification,
            }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || 'Deletion was not confirmed by the server.');
        deletionApplied = true;

        templateDeleteModal.style.display = 'none';
        pendingTemplateDelete = null;
        invalidateDataBankCaches({ clientId, templateId });
        await fetchAvailability();
        const failures = result.staged_file_cleanup_failures || [];
        showStatus(failures.length
            ? `⚠️ Template data was deleted, but ${failures.length} staged upload file(s) could not be removed.`
            : '✅ Backup downloaded and template data permanently deleted.', failures.length ? 'error' : 'success');
    } catch (error) {
        console.error('Template backup/delete error:', error);
        if (deletionApplied) {
            showStatus('✅ Backup downloaded and template data deleted. Reload the Data Bank because its list could not refresh.', 'success');
            return;
        }
        const prefix = downloadStarted
            ? 'The backup download was started, but deletion was not confirmed. Refresh the Data Bank before retrying. '
            : 'No deletion was performed. ';
        templateDeleteStatus.textContent = prefix + error.message;
        backupAndDeleteTemplateBtn.disabled = false;
        cancelTemplateDeleteBtn.disabled = false;
        closeTemplateDeleteBtn.disabled = false;
        showStatus(`❌ ${prefix}${error.message}`, 'error');
    } finally {
        if (triggerTarget) triggerTarget.disabled = previousDisabled;
        if (globalLoadingToken) window.LoadingUI.stopGlobalLoading(globalLoadingToken);
        deleteTemplateInFlight.delete(key);
    }
}
// ==============================================
// 📋 PREVIEW MODAL – FULL IMPLEMENTATION
// ==============================================

// ---------- OPEN PREVIEW ----------
function setPreviewLedgerStatus(message = '', retry = false) {
    previewLedgerStatusText.textContent = message;
    previewLedgerStatus.hidden = !message;
    previewLedgerRetry.hidden = !retry;
}

function finishPreviewLedgerLoading() {
    if (!previewLedgerLoading) return;
    window.LoadingUI.hideFieldLoading(previewLedgerInput);
    window.LoadingUI.stopGlobalLoading(previewLedgerLoading.token);
    previewLedgerInput.removeAttribute('aria-busy');
    previewLedgerField.classList.remove('is-loading');
    previewLedgerLoading = null;
}

function closePreviewLedgerOptions() {
    previewLedgerOptions.hidden = true;
    previewLedgerInput.setAttribute('aria-expanded', 'false');
    previewLedgerInput.removeAttribute('aria-activedescendant');
    previewLedgerActiveIndex = -1;
}

function clearPreviewForAccount(message) {
    // Immediately discard the previous account's rendered rows and any late
    // response, while retaining the bounded, fully scoped page cache.
    previewRequestSequence++;
    setPreviewMetricsState();
    isPreviewLoading = false;
    previewTotalRows = 0; previewSundayCount = 0; previewCachedRows = [];
    currentPage = 1;
    previewTableHead.replaceChildren();
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 100; cell.textContent = message;
    cell.style.cssText = 'padding:32px; text-align:center; color:#64748b;';
    row.appendChild(cell); previewTableBody.replaceChildren(row);
    previewCountInfo.textContent = '0 rows';
    sundayCountBadge.textContent = 'Sunday rows: 0';
    updatePagination();
}

function ledgerAccountLabel(account) {
    return `${account.account_name} — ${account.account_code}`;
}

function selectPreviewLedgerAccount(account, fetchRows = true) {
    previewLedgerAccountCode = account.account_code;
    previewLastLedgerAccountCode = account.account_code;
    previewLedgerInput.value = ledgerAccountLabel(account);
    previewLedgerInput.title = ledgerAccountLabel(account);
    closePreviewLedgerOptions(); setPreviewLedgerStatus();
    currentPage = 1;
    if (fetchRows) fetchPreviewData();
}

function restorePreviewLedgerSelection() {
    const previous = previewLedgerAccounts.find(a => a.account_code === previewLastLedgerAccountCode);
    closePreviewLedgerOptions();
    if (previous) {
        const changed = !previewLedgerAccountCode;
        selectPreviewLedgerAccount(previous, changed);
    } else {
        previewLedgerInput.value = '';
    }
}

function renderPreviewLedgerOptions(query = '') {
    if (previewAccountState !== 'ledger' || previewLedgerInput.disabled) return;
    const term = query.trim().toLocaleLowerCase();
    const matches = previewLedgerAccounts.filter(a => ledgerAccountLabel(a).toLocaleLowerCase().includes(term));
    previewLedgerMatches = matches.slice(0, 100);
    previewLedgerActiveIndex = -1;
    previewLedgerInput.removeAttribute('aria-activedescendant');
    previewLedgerOptions.replaceChildren();
    previewLedgerMatches.forEach((account, index) => {
        const option = document.createElement('button');
        option.type = 'button'; option.tabIndex = -1; option.setAttribute('role', 'option');
        option.id = `preview-ledger-option-${index}`;
        option.setAttribute('aria-selected', String(account.account_code === previewLedgerAccountCode));
        option.textContent = ledgerAccountLabel(account);
        option.title = account.account_code;
        option.addEventListener('mousedown', e => e.preventDefault());
        option.addEventListener('click', () => selectPreviewLedgerAccount(account));
        previewLedgerOptions.appendChild(option);
    });
    if (!matches.length || matches.length > 100) {
        const message = document.createElement('div');
        message.className = 'ledger-option-message';
        message.textContent = !matches.length ? 'No matching uploaded accounts.' : 'Showing the first 100 matches. Type more to narrow the list.';
        previewLedgerOptions.appendChild(message);
    }
    previewLedgerOptions.hidden = false;
    previewLedgerInput.setAttribute('aria-expanded', 'true');
}

async function loadPreviewLedgerAccounts(opening, clientId, templateId, preferred = '') {
    finishPreviewLedgerLoading(); closePreviewLedgerOptions();
    previewLedgerAccounts = []; previewLedgerAccountCode = ''; previewLastLedgerAccountCode = '';
    previewLedgerInput.value = ''; previewLedgerInput.title = '';
    previewLedgerInput.disabled = false; previewLedgerToggle.disabled = true;
    setPreviewLedgerStatus('Loading uploaded accounts…');
    clearPreviewForAccount('Loading uploaded accounts…');
    const load = {token: window.LoadingUI.startGlobalLoading(previewLedgerInput)};
    previewLedgerLoading = load;
    previewLedgerField.classList.add('is-loading');
    previewLedgerInput.setAttribute('aria-busy', 'true');
    window.LoadingUI.showFieldLoading(previewLedgerInput);
    const current = () => previewLedgerLoading === load && opening === previewOpenSequence
        && isPreviewOpen && Number(getSelectedClientId()) === Number(clientId) && previewTemplateId === templateId;
    try {
        const params = new URLSearchParams({client_id: clientId, template_id: templateId});
        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/data/ledger-accounts?${params}`);
        const data = await res.json();
        if (!current()) return;
        if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Uploaded accounts could not be loaded.');
        if (!Array.isArray(data.accounts) || data.accounts.some(a => !a || typeof a.account_code !== 'string'
            || !a.account_code.trim() || typeof a.account_name !== 'string')
            || new Set(data.accounts.map(a => a.account_code)).size !== data.accounts.length) {
            throw new Error('The uploaded account list is incomplete.');
        }
        previewLedgerAccounts = data.accounts;
        finishPreviewLedgerLoading();
        previewLedgerInput.disabled = !data.accounts.length;
        previewLedgerToggle.disabled = !data.accounts.length;
        const selected = data.accounts.find(a => a.account_code === preferred)
            || (data.accounts.length === 1 ? data.accounts[0] : null);
        if (selected) selectPreviewLedgerAccount(selected, false);
        else {
            const message = data.accounts.length ? 'Select an account to view its ledger.' : 'No ledgers have been uploaded for this client and template.';
            setPreviewLedgerStatus(data.accounts.length ? 'Search by account name or code.' : message);
            clearPreviewForAccount(message);
        }
    } catch (error) {
        if (!current()) return;
        finishPreviewLedgerLoading();
        previewLedgerInput.disabled = true; previewLedgerToggle.disabled = true;
        setPreviewLedgerStatus(`${error.message} Retry to load uploaded accounts.`, true);
        clearPreviewForAccount('Account list unavailable. Retry to continue.');
    } finally {
        if (previewLedgerLoading === load) finishPreviewLedgerLoading();
    }
}

async function openPreview(templateId, templateName, mode = 'raw') {
    if (!nativeReadAvailable() || nativeDataBank && mode !== 'raw') return;
    historyRequestSequence++;
    historyModal.style.display = 'none';
    // The shared range (already validated by whichever caller reached this
    // point — confirmSharedRangePopover()) is read fresh here as a defensive
    // fallback, not a copy — see getDateRangeFromFilter().
    const dateRange = getDateRangeFromFilter();
    if (!dateRange) {
        showStatus('⚠️ Please select a date range.', 'error');
        return;
    }

    // Check if client is still selected
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected. Please select a client from the Dashboard.', 'error');
        return;
    }
    const opening = ++previewOpenSequence;
    finishPreviewLedgerLoading(); closePreviewLedgerOptions();
    clearTimeout(searchTimeout);
    previewLedgerAccounts = []; previewLedgerAccountCode = ''; previewLastLedgerAccountCode = '';
    previewLedgerInput.value = ''; previewLedgerField.hidden = true;
    document.getElementById('previewFilterToolbar').classList.remove('ledger-preview-active');
    setPreviewLedgerStatus();
    previewAccountState = 'loading';

    previewTemplateId = templateId;
    previewTemplateName = templateName;
    previewMode = mode === 'classification' ? 'classification' : 'raw';
    if (previewMode === 'raw') {
        invalidateDataBankCaches({clientId, templateId});
        previewPageInFlight.clear();
    }
    previewLimit = parseInt(pageSizeSelect.value) || 50;
    currentPage = 1;
    isPreviewOpen = true;

    // Show modal
    previewModal.style.display = 'flex';
    previewTitle.textContent = previewMode === 'classification'
        ? `🏷️ ${templateName} — Cashflows classification (${getSelectedClientName() || 'Unknown Client'})`
        : `📋 ${templateName} – Preview (${getSelectedClientName() || 'Unknown Client'})`;
    sundayOnlyCheckbox.checked = false;
    searchInput.value = '';
    amountMin.value = '';
    amountMax.value = '';
    dataQualityFilter.value = '';
    classificationTypeFilter.value = '';
    classificationAccountFilter.value = '';
    classificationCategoryFilter.value = '';
    sundayCountBadge.textContent = 'Sunday rows: 0';
    cashflowsClassificationFilters.style.display = previewMode === 'classification' ? 'flex' : 'none';
    const rawFilterRow = document.querySelector('.sunday-filter-row');
    if (rawFilterRow) rawFilterRow.style.display = previewMode === 'classification' ? 'none' : 'flex';
    if (amountMin.parentElement) amountMin.parentElement.style.display = previewMode === 'classification' ? 'none' : 'flex';
    if (dataQualityFilter.parentElement) dataQualityFilter.parentElement.style.display = previewMode === 'classification' ? 'none' : 'flex';
    clearPreviewForAccount('Loading preview settings…');

    // Subtotal toggle: re-initialise from this template's own default flag
    // every time the preview is (re)opened - no cross-open persistence.
    try {
        const subtotals = await fetchTemplateSubtotalDefault(templateId);
        if (opening !== previewOpenSequence || !isPreviewOpen || Number(getSelectedClientId()) !== Number(clientId)) return;
        previewShowSubtotals = subtotals;
        subtotalToggleCheckbox.checked = subtotals;
        const ledger = previewMode === 'raw' && templatePurposeCache[templateId] === 'account_ledger';
        previewAccountState = ledger ? 'ledger' : 'standard';
        previewLedgerField.hidden = !ledger;
        document.getElementById('previewFilterToolbar').classList.toggle('ledger-preview-active', ledger);
        if (ledger) await loadPreviewLedgerAccounts(opening, clientId, templateId);
        if (opening !== previewOpenSequence || !isPreviewOpen || Number(getSelectedClientId()) !== Number(clientId)) return;
    } catch (error) {
        if (opening !== previewOpenSequence || !isPreviewOpen) return;
        previewAccountState = 'error';
        setPreviewLedgerStatus('Preview settings could not be loaded. Retry to continue.', true);
        clearPreviewForAccount('Preview settings unavailable.');
        return;
    }

    // Fetch data
    fetchPreviewData();
}

function formatHistoryTimestamp(value) {
    if (!value) return 'Time unavailable';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime())
        ? String(value)
        : parsed.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function appendHistoryChangeValue(cell, change, tokens = {}) {
    const oldValue = document.createElement('span');
    oldValue.className = 'history-old-value';
    oldValue.textContent = nativeDataBank ? tokens.old_value ?? nativeHistoryValue(change.old_value) : formatCellValue(change.old_value);
    const arrow = document.createTextNode(' → ');
    const newValue = document.createElement('span');
    newValue.className = 'history-new-value';
    newValue.textContent = nativeDataBank ? tokens.new_value ?? nativeHistoryValue(change.new_value) : formatCellValue(change.new_value);
    cell.append(oldValue, arrow, newValue);
}

function renderUploadHistory(data) {
    historyTableHead.replaceChildren();
    historyTableBody.replaceChildren();
    const header = document.createElement('tr');
    ['Action', 'Changed at', 'Changed by', ...historyColumns.map(column => column.column_name)].forEach((label) => {
        const th = document.createElement('th');
        th.scope = 'col';
        th.textContent = label;
        header.appendChild(th);
    });
    historyTableHead.appendChild(header);

    const rows = data.rows || [];
    if (!rows.length) {
        historyStatus.textContent = 'No transactions match these history filters.';
        const empty = document.createElement('tr');
        const cell = document.createElement('td');
        cell.className = 'history-empty';
        cell.colSpan = historyColumns.length + 3;
        cell.textContent = 'No changed or deleted transactions found.';
        empty.appendChild(cell);
        historyTableBody.appendChild(empty);
    } else {
        historyStatus.textContent = `${data.total} record${data.total === 1 ? '' : 's'} · newest first`;
        rows.forEach((entry) => {
            const rowData = entry.row_data || {};
            const changes = entry.changed_fields || null;
            const tr = document.createElement('tr');
            const displayAction = entry.display_action || entry.action;
            const isDeleted = displayAction === 'deleted';
            tr.className = isDeleted ? 'history-row-deleted' : 'history-row-changed';
            const actionCell = document.createElement('td');
            actionCell.textContent = isDeleted ? 'Deleted' : 'Changed';
            actionCell.style.fontWeight = '600';
            actionCell.style.color = isDeleted ? '#b91c1c' : '#1d4ed8';
            tr.appendChild(actionCell);
            const changedAtCell = document.createElement('td');
            changedAtCell.textContent = formatHistoryTimestamp(entry.changed_at);
            changedAtCell.style.whiteSpace = 'nowrap';
            tr.appendChild(changedAtCell);
            const changedByCell = document.createElement('td');
            changedByCell.textContent = entry.changed_by || 'System';
            tr.appendChild(changedByCell);

            historyColumns.forEach((column) => {
                const name = column.column_name;
                const cell = document.createElement('td');
                const change = changes && changes[name];
                if (isDeleted) {
                    cell.textContent = nativeDataBank ? entry.numeric_text?.[name] ?? nativeHistoryValue(rowData[name]) : formatCellValue(rowData[name]);
                } else if (change) {
                    appendHistoryChangeValue(cell, change, entry.changed_numeric_text?.[name]);
                } else if (name === historyDocumentKeyColumn) {
                    // Keep the document/voucher identifier visible as context;
                    // other unchanged fields stay blank to focus on the effect.
                    cell.textContent = nativeDataBank ? entry.numeric_text?.[name] ?? nativeHistoryValue(rowData[name]) : formatCellValue(rowData[name]);
                }
                tr.appendChild(cell);
            });
            historyTableBody.appendChild(tr);

            if (!isDeleted && (!changes || Object.keys(changes).length === 0)) {
                const note = document.createElement('tr');
                note.className = 'history-row-note';
                const noteCell = document.createElement('td');
                noteCell.colSpan = historyColumns.length + 3;
                noteCell.textContent = changes === null
                    ? 'Field-by-field details were not saved for this earlier change.'
                    : 'No specific field values were available for this change.';
                note.appendChild(noteCell);
                historyTableBody.appendChild(note);
            }
        });
    }
    historyPage = data.page || 1;
    historyTotalPages = data.total_pages || 1;
    historyPageInfo.textContent = `Page ${historyPage} of ${historyTotalPages}`;
    historyPrevBtn.disabled = historyPage <= 1;
    historyNextBtn.disabled = historyPage >= historyTotalPages;
}

async function loadHistoryLedgerAccounts(sequence) {
    const params = new URLSearchParams({
        client_id: String(historyClientId),
        template_id: String(historyTemplateId),
    });
    if (nativeDataBank) params.set('history', 'true');
    const response = await fetch(`${API_BASE}/api/data/ledger-accounts?${params}`);
    if (!response.ok) throw new Error('Could not load ledger accounts');
    const data = await response.json();
    if (sequence !== historyRequestSequence) return;
    historyLedgerSelect.replaceChildren();
    const all = document.createElement('option');
    all.value = '';
    all.textContent = 'All ledger accounts';
    historyLedgerSelect.appendChild(all);
    (data.accounts || []).forEach((account) => {
        const option = document.createElement('option');
        option.value = account.account_code;
        option.textContent = `${account.account_name} (${account.account_code})`;
        historyLedgerSelect.appendChild(option);
    });
}

async function openUploadHistory(item) {
    if (nativeDataBank && !nativeDataBank.history) return;
    if (nativeDataBank) { previewRequestSequence++; isPreviewOpen = false; previewModal.style.display = 'none'; hideRowHistoryPopover(); }
    const clientId = getSelectedClientId();
    if (!clientId || !item.template_id) return;
    historyRequestSequence++;
    historyClientId = Number(clientId);
    historyTemplateId = Number(item.template_id);
    historyTemplateName = item.template_name || `Template ${item.template_id}`;
    historyColumns = [];
    historyDocumentKeyColumn = null;
    historyPage = 1;
    historyTotalPages = 1;
    historyActionFilter.value = 'all';
    historyLedgerSelect.replaceChildren();
    historyLedgerControl.hidden = true;
    if (nativeDataBank) historyLedgerControl.style.display = 'none';
    historyTableHead.replaceChildren();
    historyTableBody.replaceChildren();
    historyTitle.textContent = `📜 Changed/Deleted data — ${historyTemplateName}`;
    historyStatus.textContent = 'Loading history…';
    historyModal.style.display = 'flex';
    await fetchUploadHistory(1);
}

async function fetchUploadHistory(page = 1) {
    if (!historyClientId || !historyTemplateId) return;
    const sequence = ++historyRequestSequence;
    historyStatus.textContent = 'Loading history…';
    historyTableBody.replaceChildren();
    const params = new URLSearchParams({
        client_id: String(historyClientId),
        template_id: String(historyTemplateId),
        page: String(page),
        limit: '50',
        action: historyActionFilter.value || 'all',
    });
    if (historyLedgerSelect.value) params.set('ledger_account_code', historyLedgerSelect.value);
    try {
        const response = await fetch(`${API_BASE}/api/data/history?${params}`);
        if (!response.ok) throw new Error('History request failed');
        const data = await response.json();
        if (sequence !== historyRequestSequence
            || Number(getSelectedClientId()) !== historyClientId
            || Number(data.template_id) !== historyTemplateId) return;
        assertNativeClient(response, historyClientId);
        if (nativeDataBank && Number(data.client_id) !== historyClientId) throw new Error('History scope changed.');
        historyTemplateName = data.template_name || historyTemplateName;
        historyColumns = data.columns || [];
        historyDocumentKeyColumn = data.document_key_column || null;
        historyTitle.textContent = `📜 Changed/Deleted data — ${historyTemplateName}`;
        historyLedgerControl.hidden = data.data_purpose !== 'account_ledger';
        if (nativeDataBank) historyLedgerControl.style.display = data.data_purpose === 'account_ledger' ? 'flex' : 'none';
        renderUploadHistory(data);
        if (data.data_purpose === 'account_ledger' && historyLedgerSelect.options.length <= 1) {
            try {
                await loadHistoryLedgerAccounts(sequence);
            } catch (error) {
                historyStatus.textContent += ' Ledger account choices could not be loaded.';
            }
        }
    } catch (error) {
        if (sequence !== historyRequestSequence) return;
        historyStatus.textContent = 'Could not load history. Please close this window and try again.';
    }
}

function openClassificationPreview(templateId, templateName) {
    return openPreview(templateId, templateName, 'classification');
}
// Subtotal toggle: per-template has_subtotals_by_default lookup, cached
// like fetchTemplateColumns() below (same underlying GET /api/templates/{id}
// endpoint, kept as a separate small cache rather than folding into
// templateColumnsCache since that cache's shape is a plain column-name array
// consumed elsewhere).
let templateSubtotalDefaultCache = {};
let templatePurposeCache = {};

async function fetchTemplateSubtotalDefault(templateId) {
    const generation = previewCacheGeneration;
    if (Object.prototype.hasOwnProperty.call(templateSubtotalDefaultCache, templateId)) {
        return templateSubtotalDefaultCache[templateId];
    }
    try {
        const res = await fetch(`${API_BASE}/api/templates/${templateId}`);
        if (!res.ok) throw new Error('Preview settings could not be loaded.');
        const data = await res.json();
        const flag = !!data.has_subtotals_by_default;
        if (!nativeDataBank || generation === previewCacheGeneration) {
            templateSubtotalDefaultCache[templateId] = flag;
            templatePurposeCache[templateId] = data.data_purpose || 'standard';
        }
        return flag;
    } catch (e) {
        console.error('Failed to load template subtotal default:', e);
        throw e; // Do not cache a failure or load an unscoped ledger preview.
    }
}

// Skeleton column count for fetchPreviewData()'s loading state (Shared Loading
// UI, Phase 3 of 6) — prefers the cached column list templateColumnsCache
// already holds for this template (fetchTemplateColumns() below, +1 for the
// S.No column), falls back to whatever header is currently rendered from a
// prior fetch, and only falls back further to LoadingUI's own 1-column
// default on a template's very first preview open this session, when the
// real shape genuinely isn't known yet.
function getPreviewSkeletonColumnCount() {
    const cached = templateColumnsCache[previewTemplateId];
    if (cached && cached.length) return cached.length + 1 + (previewMode === 'classification' ? 3 : 0);
    const headerCount = previewTableHead.querySelectorAll('th').length;
    if (headerCount) return headerCount;
    return undefined;
}

function getPreviewCacheKey(endpoint, params) {
    return `${endpoint}?${params.toString()}`;
}

function getCachedPreviewPage(cacheKey) {
    const entry = previewPageCache.get(cacheKey);
    if (!entry) return null;
    // Map insertion order gives us a small LRU cache without another
    // dependency: touch the entry whenever it is reused.
    previewPageCache.delete(cacheKey);
    previewPageCache.set(cacheKey, entry);
    return entry.data;
}

function setCachedPreviewPage(cacheKey, data, clientId, templateId) {
    previewPageCache.delete(cacheKey);
    previewPageCache.set(cacheKey, { data, clientId, templateId });
    while (previewPageCache.size > PREVIEW_CACHE_MAX_ENTRIES) {
        previewPageCache.delete(previewPageCache.keys().next().value);
    }
}

async function loadPreviewPage(endpoint, params, cacheKey, clientId, templateId) {
    const cached = getCachedPreviewPage(cacheKey);
    if (cached) return cached;

    const pending = previewPageInFlight.get(cacheKey);
    if (pending) return pending;

    const cacheGeneration = previewCacheGeneration;
    const requestParams = new URLSearchParams(params);
    if (dataBankPerfEnabled) requestParams.set('perf', 'true');
    const request = (async () => {
        const res = await window.ApiUtils.apiFetch(
            `${API_BASE}${endpoint}?${requestParams.toString()}`
        );
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch preview');
        }
        const data = await res.json();
        assertNativeClient(res, clientId);
        if (endpoint === '/api/data/preview' && (Number(data.template_id) !== Number(templateId)
            || (data.preview_metrics?.scope && !previewMetricScopeMatches(data.preview_metrics.scope, {clientId, templateId, params})))) {
            throw new Error('Preview scope changed. Reopen the preview and retry.');
        }
        if (cacheGeneration === previewCacheGeneration) {
            setCachedPreviewPage(cacheKey, data, clientId, templateId);
        }
        return data;
    })();

    previewPageInFlight.set(cacheKey, request);
    try {
        return await request;
    } finally {
        if (previewPageInFlight.get(cacheKey) === request) {
            previewPageInFlight.delete(cacheKey);
        }
    }
}

function buildPreviewRequestSpec(page = currentPage) {
    const clientId = getSelectedClientId();
    const dateRange = getDateRangeFromFilter();
    if (!clientId || !previewTemplateId || !dateRange) return null;

    const params = new URLSearchParams({
        client_id: clientId,
        template_id: previewTemplateId,
        date_start: dateRange.start,
        date_end: dateRange.end,
        page,
        limit: previewLimit
    });
    if (searchInput.value.trim()) params.append('search', searchInput.value.trim());
    if (previewMode === 'raw' && previewAccountState === 'ledger' && previewLedgerAccountCode) {
        params.append('ledger_account_code', previewLedgerAccountCode);
    }
    if (previewMode === 'raw') params.append('include_metrics', 'true');
    if (previewMode === 'classification') {
        if (classificationTypeFilter.value) params.append('classification_type', classificationTypeFilter.value);
        if (classificationAccountFilter.value.trim()) params.append('account_code', classificationAccountFilter.value.trim());
        if (classificationCategoryFilter.value.trim()) params.append('category', classificationCategoryFilter.value.trim());
    }
    if (amountMin.value) params.append('amount_min', amountMin.value);
    if (amountMax.value) params.append('amount_max', amountMax.value);
    if (sundayOnlyCheckbox.checked) params.append('sunday_only', 'true');
    if (dataQualityFilter.value) params.append('data_quality', dataQualityFilter.value);

    const endpoint = previewMode === 'classification'
        ? '/api/data/cashflows-classification-preview'
        : '/api/data/preview';
    return {
        clientId,
        templateId: previewTemplateId,
        endpoint,
        params,
        cacheKey: getPreviewCacheKey(endpoint, params)
    };
}

function prefetchNextPreviewPage(spec, data) {
    const totalPages = Math.ceil((data.total_rows || 0) / previewLimit) || 1;
    if (currentPage >= totalPages) return;

    const nextParams = new URLSearchParams(spec.params);
    nextParams.set('page', String(currentPage + 1));
    const nextKey = getPreviewCacheKey(spec.endpoint, nextParams);
    loadPreviewPage(spec.endpoint, nextParams, nextKey, spec.clientId, spec.templateId)
        .catch(() => {
            // Prefetch is an optimization; the visible page will report any
            // real error if the auditor navigates there later.
        });
}

// ---------- FETCH PREVIEW DATA ----------
async function fetchPreviewData() {
    if (!nativeReadAvailable()) return;
    if (previewAccountState === 'loading' || previewAccountState === 'error'
        || (previewAccountState === 'ledger' && !previewLedgerAccountCode)) return;
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected.', 'error');
        return;
    }
    // Read the shared date range fresh on every call (rather than a cached
    // copy) — this is what lets a range change made elsewhere (main sheet
    // or the in-modal popover) take effect the next time data is fetched,
    // with no separate preview-only range to keep in sync.
    const requestSpec = buildPreviewRequestSpec(currentPage);
    if (!requestSpec) {
        showStatus('⚠️ Missing preview parameters.', 'error');
        return;
    }

    const requestId = ++previewRequestSequence;
    nativeLoadedExportKey = null; updateNativeExportButton();
    setPreviewMetricsState();
    const requestWasCached = !!getCachedPreviewPage(requestSpec.cacheKey);
    isPreviewLoading = true;
    if (!requestWasCached) {
        window.LoadingUI.showSkeleton(previewTableBody, 8, { columns: getPreviewSkeletonColumnCount() });
        previewTableHead.innerHTML = '';
        previewCountInfo.textContent = 'Loading...';
    }

    // Add filters. Cashflows mode sends only derived-layer filters; raw preview
    // keeps its existing amount, Sunday, and data-quality behavior unchanged.
    try {
        const perfStarted = dataBankPerfEnabled ? performance.now() : null;
        const data = await loadPreviewPage(
            requestSpec.endpoint,
            requestSpec.params,
            requestSpec.cacheKey,
            requestSpec.clientId,
            requestSpec.templateId
        );
        if (requestId !== previewRequestSequence || !isPreviewOpen || Number(clientId) !== Number(getSelectedClientId())) return;
        if (previewMode === 'raw' && (Number(data.template_id) !== Number(requestSpec.templateId)
            || (data.preview_metrics?.scope && !previewMetricScopeMatches(data.preview_metrics.scope, requestSpec)))) {
            throw new Error('Preview scope changed. Reopen the preview and retry.');
        }
        
        previewTotalRows = data.total_rows;
        previewSundayCount = data.sunday_count || 0;
        previewDocumentKeyColumn = data.document_key_column || null;
        previewDateColumn = data.date_column || null;
        previewAmountColumns = data.amount_columns || [];
        if (previewMode === 'raw' && Array.isArray(data.column_order)) templateColumnsCache[previewTemplateId] = data.column_order;
        renderPreviewMetrics(data, requestSpec);
        const rows = data.rows || [];
        
        // Update Sunday badge
        sundayCountBadge.textContent = `Sunday rows: ${previewSundayCount}`;
        
        // Render table
        const renderStarted = dataBankPerfEnabled ? performance.now() : null;
        await renderPreviewRows(rows, requestId);
        if (requestId !== previewRequestSequence || !isPreviewOpen) return;
        nativeLoadedExportKey = nativeExportKey(requestSpec);
        updatePagination();
        if (dataBankPerfEnabled) {
            logDataBankPerf('databank_preview', perfStarted, {
                template_id: previewTemplateId,
                row_count: rows.length,
                total_rows: previewTotalRows,
                fetch_ms: Math.round((renderStarted - perfStarted) * 10) / 10,
                render_ms: Math.round((performance.now() - renderStarted) * 10) / 10,
                backend: data._perf || null
            });
        }

        prefetchNextPreviewPage(requestSpec, data);
        
        if (rows.length === 0 && previewTotalRows === 0) {
            previewTableBody.innerHTML = '<tr><td colspan="100" style="text-align:center; padding:40px; color:#94a3b8;">No data found for the selected criteria.</td></tr>';
            previewCountInfo.textContent = '0 rows';
        }
        
    } catch (error) {
        if (requestId !== previewRequestSequence || error.debounced) return;
        setPreviewMetricsState('Totals unavailable');
        console.error('Preview fetch error:', error);
        showStatus('❌ Error loading preview: ' + error.message, 'error');
        previewTableBody.innerHTML = `<tr><td colspan="100" style="text-align:center; padding:40px; color:#dc2626;">Error loading data. Please try again.</td></tr>`;
        previewCountInfo.textContent = 'Error loading';
    } finally {
        if (requestId === previewRequestSequence) {
            isPreviewLoading = false;
            updateNativeExportButton();
        }
    }
}

// Column-order fix: a row's own row_data JSON key order isn't reliable (a
// derived field like Rate, with no Excel mapping, gets appended at whatever
// position it was first set, not its configured slot) -- so header/column
// order is read from the template's own sort_order via GET /api/templates/{id}
// instead of Object.keys(row_data). Same cache/fetch shape as
// static/verification.js's templateColumnsCache/fetchTemplateColumns().
let templateColumnsCache = {};

async function fetchTemplateColumns(templateId) {
    if (Object.prototype.hasOwnProperty.call(templateColumnsCache, templateId)) {
        return templateColumnsCache[templateId];
    }
    try {
        const res = await fetch(`${API_BASE}/api/templates/${templateId}`);
        if (!res.ok) {
            templateColumnsCache[templateId] = [];
            return [];
        }
        const data = await res.json();
        const columns = (data.columns || []).map(c => c.column_name);
        templateColumnsCache[templateId] = columns;
        return columns;
    } catch (e) {
        console.error('Failed to load template columns:', e);
        templateColumnsCache[templateId] = [];
        return [];
    }
}

// ---------- RENDER PREVIEW ROWS ----------
async function renderPreviewRows(rows, requestId = previewRequestSequence) {
    hideRowHistoryPopover();
    // Subtotal toggle: cache the current page's rows so toggling later
    // re-renders instantly from memory, with no refetch.
    previewCachedRows = rows || [];
    if (!rows || rows.length === 0) {
        previewTableBody.innerHTML = '<tr><td colspan="100" style="text-align:center; padding:40px; color:#94a3b8;">No rows to display.</td></tr>';
        previewTableHead.innerHTML = '';
        previewCountInfo.textContent = '0 rows';
        return;
    }

    // Build headers from the template's own configured column order (falls back to the
    // first row's own row_data key order only if the template lookup returns nothing, e.g.
    // a deleted/inactive template).
    const firstRow = rows[0].row_data;
    const templateHeaders = await fetchTemplateColumns(previewTemplateId);
    if (requestId !== previewRequestSequence || !isPreviewOpen) return;
    const headers = templateHeaders.length ? templateHeaders : (firstRow ? Object.keys(firstRow) : []);
    const classificationHeaders = previewMode === 'classification' ? ['Type', 'Account', 'Assigned Category'] : [];
    previewTableHead.innerHTML = '';
    if (headers.length) {
        const tr = document.createElement('tr');
        
        // Add Serial Number column header
        const thSerial = document.createElement('th');
        thSerial.textContent = 'S.No';
        thSerial.style.padding = '8px 12px';
        thSerial.style.textAlign = 'center';
        thSerial.style.borderBottom = '2px solid #e2e8f0';
        thSerial.style.whiteSpace = 'nowrap';
        thSerial.style.position = 'sticky';
        thSerial.style.top = '0';
        thSerial.style.background = '#f8fafc';
        thSerial.style.width = '60px';
        tr.appendChild(thSerial);
        
        headers.forEach(h => {
            const th = document.createElement('th');
            th.textContent = h;
            th.style.padding = '8px 12px';
            th.style.textAlign = 'left';
            th.style.borderBottom = '2px solid #e2e8f0';
            th.style.whiteSpace = 'nowrap';
            th.style.position = 'sticky';
            th.style.top = '0';
            th.style.background = '#f8fafc';
            tr.appendChild(th);
        });
        if (classificationHeaders.length) {
            classificationHeaders.forEach((h) => {
                const th = document.createElement('th');
                th.textContent = h;
                th.style.padding = '8px 12px';
                th.style.textAlign = 'left';
                th.style.borderBottom = '2px solid #e2e8f0';
                th.style.whiteSpace = 'nowrap';
                th.style.position = 'sticky';
                th.style.top = '0';
                th.style.background = '#f8fafc';
                tr.appendChild(th);
            });
        }
        previewTableHead.appendChild(tr);
    }
    
    // Render body rows with serial numbers
    previewTableBody.innerHTML = '';
    const startSerial = (currentPage - 1) * previewLimit + 1;

    // Document grouping / subtotal state: consecutive rows sharing the same
    // document_group_id (same document key + date, computed backend-side)
    // are treated as one document's lines - grouping is purely per-page, per
    // the confirmed brief (a voucher split across a page boundary is not
    // stitched back together). groupSums accumulates each amount column's
    // running total across the current group's rows, reset whenever a new
    // group starts.
    let groupSums = Object.create(null);

    rows.forEach((row, index) => {
        const tr = document.createElement('tr');
        // Check if this row's extract_date is Sunday
        const isSun = isSunday(row.extract_date);
        if (isSun) {
            tr.className = 'sunday-highlight';
        }

        const prevGroupId = index > 0 ? rows[index - 1].document_group_id : undefined;
        const nextGroupId = index < rows.length - 1 ? rows[index + 1].document_group_id : undefined;
        const isFirstInGroup = previewMode === 'classification' || row.document_group_id !== prevGroupId;
        const isLastInGroup = previewMode === 'classification' || row.document_group_id !== nextGroupId;

        if (isFirstInGroup) {
            groupSums = Object.create(null);
            previewAmountColumns.forEach(col => { groupSums[col] = nativeDataBank ? { coefficient: 0n, scale: 0 } : 0; });
        }
        previewAmountColumns.forEach(col => {
            if (nativeDataBank) {
                const value = row.numeric_text?.[col] ?? row.row_data[col];
                groupSums[col] = addNativeExact(groupSums[col], nativeExactNumber(value == null || value === '' ? '0' : value)); return;
            }
            const num = parseFloat(row.row_data[col]);
            if (!isNaN(num)) groupSums[col] += num;
        });

        // Serial number column - number shown only on the first line of each
        // document's group; blank on subsequent lines of the same document.
        // History/quality-flag icons are still appended per-row below
        // regardless of group position, since they're facts about that
        // specific client_data row, not the document as a whole.
        const tdSerial = document.createElement('td');
        tdSerial.style.padding = '6px 12px';
        tdSerial.style.borderBottom = '1px solid #f0f2f5';
        tdSerial.style.textAlign = 'center';
        tdSerial.style.color = '#64748b';
        tdSerial.textContent = isFirstInGroup ? (startSerial + index) : '';

        // Revision history icon: only shown for rows overwritten during a re-upload.
        if (row.has_history) {
            const historyIcon = document.createElement('span');
            historyIcon.className = 'history-icon';
            historyIcon.textContent = '⚠️';
            historyIcon.title = nativeDataBank ? `${row.upload_revision_count || 0} saved upload changes; ${row.revision_count || 0} legacy audit records` : `${row.revision_count} revision(s)`;
            // Click-to-open/toggle instead of hover: hover closed the popover
            // as soon as the mouse moved toward it, before it could be read.
            historyIcon.addEventListener('click', (e) => {
                e.stopPropagation();
                if (activeHistoryIcon === historyIcon) {
                    hideRowHistoryPopover();
                } else {
                    showRowHistoryPopover(historyIcon, row.id);
                }
            });
            tdSerial.appendChild(historyIcon);
        }

        // Data quality flag icon: only shown for rows force-imported with a
        // blank Document ID/Amount/Date (via the Upload page's override).
        if (row.data_quality_flag === 'incomplete') {
            const qualityIcon = document.createElement('span');
            qualityIcon.className = 'quality-flag-icon';
            qualityIcon.textContent = '🚩';
            qualityIcon.title = 'Imported with missing data (blank Document ID/Amount/Date)';
            tdSerial.appendChild(qualityIcon);
        }

        tr.appendChild(tdSerial);

        headers.forEach(h => {
            const td = document.createElement('td');
            td.style.padding = '6px 12px';
            td.style.borderBottom = '1px solid #f0f2f5';
            // Document No. and Date are shown on every line, including
            // continuation lines - only the S.No cell above is blanked for
            // grouping. Every column always shows that specific line's own
            // value.
            const value = row.row_data[h] !== undefined ? row.row_data[h] : '';
            const formatted = nativeDataBank && (row.numeric_text?.[h] != null || previewAmountColumns.includes(h))
                ? String(row.numeric_text?.[h] ?? (value == null || value === '' ? '—' : value)) : formatCellValue(value, h);
            td.textContent = formatted;

            // Rate-derivation requirement, Phase 10 (FINAL): a small provenance
            // icon on the Rate column's cells (only when it has a real value),
            // sourced per-row from row.rate_source (client_data.rate_source) -
            // never the template-level setting, since a historic row may have
            // been imported under a since-changed setting. Mirrors the
            // .quality-flag-icon markup pattern above (span, title, cursor:help).
            if (h.trim().toLowerCase() === 'rate' && formatted !== '—' && row.rate_source) {
                const provIcon = document.createElement('span');
                provIcon.className = 'rate-source-icon';
                if (row.rate_source === 'derived') {
                    provIcon.textContent = '🧮';
                    provIcon.title = 'Rate derived as Amount ÷ Quantity';
                } else {
                    provIcon.textContent = '📥';
                    provIcon.title = 'Rate imported directly from the source file';
                }
                td.appendChild(provIcon);
            }
            tr.appendChild(td);
        });
        if (previewMode === 'classification') {
            const classification = row.classification || {};
            const values = [
                classification.type || 'Needs review',
                [classification.account_code, classification.account_name].filter(Boolean).join(' — ') || '—',
                classification.assigned_category || (classification.classification_status === 'unmapped' ? 'Needs review' : '—')
            ];
            values.forEach((value) => {
                const td = document.createElement('td');
                td.style.padding = '6px 12px';
                td.style.borderBottom = '1px solid #f0f2f5';
                td.textContent = value;
                tr.appendChild(td);
            });
        }
        previewTableBody.appendChild(tr);

        // Subtotal row: inserted after the last line of each document,
        // summing only the Amount column(s) - every other cell stays blank.
        // Boxed via the .preview-subtotal-row CSS (top+bottom border) so
        // each document's block reads as visually closed off. Skipped
        // entirely when the template has no Amount (Numeric) column, since
        // there's nothing meaningful to subtotal.
        if (previewMode === 'raw' && isLastInGroup && previewAmountColumns.length > 0 && previewShowSubtotals) {
            const subtotalTr = document.createElement('tr');
            subtotalTr.className = 'preview-subtotal-row';

            const tdSubSerial = document.createElement('td');
            tdSubSerial.style.padding = '6px 12px';
            subtotalTr.appendChild(tdSubSerial);

            headers.forEach(h => {
                const td = document.createElement('td');
                td.style.padding = '6px 12px';
                if (previewAmountColumns.includes(h)) {
                    td.textContent = nativeDataBank ? nativeExactText(groupSums[h]) : formatCellValue(groupSums[h], h);
                }
                subtotalTr.appendChild(td);
            });

            previewTableBody.appendChild(subtotalTr);
        }
    });

    previewCountInfo.textContent = `Showing ${rows.length} of ${previewTotalRows} rows`;
}

// ---------- UPDATE PAGINATION ----------
function updatePagination() {
    const totalPages = Math.ceil(previewTotalRows / previewLimit) || 1;
    pageInfo.textContent = `Page ${currentPage} of ${totalPages}`;
    firstPageBtn.disabled = currentPage <= 1;
    prevPageBtn.disabled = currentPage <= 1;
    nextPageBtn.disabled = currentPage >= totalPages;
    lastPageBtn.disabled = currentPage >= totalPages;
}

// ---------- APPLY PREVIEW FILTERS ----------
function applyPreviewFilters() {
    if (!isPreviewOpen) return;
    currentPage = 1;
    fetchPreviewData();
}

// ---------- CLEAR PREVIEW FILTERS ----------
function clearPreviewFilters() {
    if (!isPreviewOpen) return;
    clearTimeout(searchTimeout);
    searchInput.value = '';
    amountMin.value = '';
    amountMax.value = '';
    sundayOnlyCheckbox.checked = false;
    dataQualityFilter.value = '';
    classificationTypeFilter.value = '';
    classificationAccountFilter.value = '';
    classificationCategoryFilter.value = '';
    // Account is the ledger being viewed, not a text-search criterion.
    restorePreviewLedgerSelection();
    currentPage = 1;
    fetchPreviewData();
}

// ---------- EXPORT DATASET ----------
async function exportDataset(templateId, templateName) {
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected.', 'error');
        return;
    }
    
    const dateRange = getDateRangeFromFilter();
    if (!dateRange) {
        showStatus('⚠️ Please select a date range.', 'error');
        return;
    }
    
    showStatus('⏳ Preparing export...', 'loading');
    
    try {
        // Fetch all rows in batches. Capped at 200 to match GET /api/data/preview's
        // own server-side limit (Query(..., le=200)) — a higher value here gets
        // rejected outright with a 422 before the request is even processed.
        let allRows = [];
        let page = 1;
        const limit = 200;
        let totalFetched = 0;
        const maxRows = 10000;
        
        const params = new URLSearchParams({
            client_id: clientId,
            template_id: templateId,
            date_start: dateRange.start,
            date_end: dateRange.end,
            limit: limit
        });
        
        do {
            params.set('page', page);
            const res = await fetch(`${API_BASE}/api/data/preview?${params.toString()}`);
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Export failed');
            }
            const data = await res.json();
            const rows = data.rows || [];
            if (rows.length === 0) break;
            
            allRows = allRows.concat(rows);
            totalFetched += rows.length;
            page++;
            
            if (totalFetched >= maxRows) {
                showStatus(`⚠️ Export limited to ${maxRows} rows. Use filters to reduce data.`, 'error');
                break;
            }
        } while (true);
        
        if (allRows.length === 0) {
            showStatus('⚠️ No data to export.', 'error');
            return;
        }
        
        // Build Excel data — headers come from the template's own configured column order
        // (falls back to the first row's own row_data key order if the template lookup
        // returns nothing), same fix as renderPreviewRows() above.
        const firstRow = allRows[0];
        const templateHeaders = await fetchTemplateColumns(templateId);
        const headers = templateHeaders.length ? templateHeaders : (firstRow.row_data ? Object.keys(firstRow.row_data) : []);
        const excelData = [headers];
        allRows.forEach(row => {
            const rowData = headers.map(h => row.row_data[h] !== undefined ? row.row_data[h] : '');
            excelData.push(rowData);
        });
        
        // Create workbook
        const ws = XLSX.utils.aoa_to_sheet(excelData);
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, 'Data');
        const wbout = XLSX.write(wb, { bookType: 'xlsx', type: 'array' });
        
        const blob = new Blob([wbout], { type: 'application/octet-stream' });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${templateName || 'dataset'}_${new Date().toISOString().slice(0,10)}.xlsx`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);
        
        showStatus(`✅ Exported ${allRows.length} rows to Excel`, 'success');
    } catch (error) {
        showStatus('❌ Export error: ' + error.message, 'error');
    }
}

// ---------- TEMPLATE CALENDAR MODAL EVENT LISTENERS ----------
// Coverage Calendar → per-template popover fix: month nav now drives
// fetchTemplateCalendar() for whichever single template the modal is open
// for; the old chip search listener is gone (see yourDatasetsSearchInput's
// listener further below for its replacement).
calendarPrevMonthBtn.addEventListener('click', goToPrevMonth);
calendarNextMonthBtn.addEventListener('click', goToNextMonth);
closeTemplateCalendarBtn.addEventListener('click', closeTemplateCalendarPopover);
templateCalendarModal.addEventListener('click', (e) => {
    if (e.target === templateCalendarModal) {
        closeTemplateCalendarPopover();
    }
});

// "Your Datasets" search (Coverage Calendar → per-template popover fix):
// pure client-side filter over the already-fetched availabilityData, no
// network call — replaces the old chip picker's search box.
// 3-fix: also toggles the ✕ clear icon's visibility based on whether the
// field currently has text.
yourDatasetsSearchInput.addEventListener('input', () => {
    yourDatasetsSearchTerm = yourDatasetsSearchInput.value.trim().toLowerCase();
    yourDatasetsSearchClearBtn.style.display = yourDatasetsSearchInput.value ? 'block' : 'none';
    renderFilteredYourDatasetsTable();
});

// 3-fix: clicking ✕ clears instantly (no confirm), same interaction pattern
// as this app's other clear/✕ icons — restores the full unfiltered list.
yourDatasetsSearchClearBtn.addEventListener('click', () => {
    yourDatasetsSearchInput.value = '';
    yourDatasetsSearchTerm = '';
    yourDatasetsSearchClearBtn.style.display = 'none';
    renderFilteredYourDatasetsTable();
    yourDatasetsSearchInput.focus();
});

// ---------- EVENT LISTENERS (Main) ----------
// Redesign Phase 1: same destination/behavior as the old inline onclick.
uploadMoreBtn.addEventListener('click', () => {
    window.location.href = '/static/upload.html';
});
// Redesign Phase 6: the old filter bar's Check Availability/Clear Filters/
// Refresh buttons and their keydown-to-Enter shortcuts are gone — the shared
// date range popover's own confirm button (see confirmSharedRangePopover())
// is now the sole way to change the range and refresh, alongside automatic
// refreshes on client change and popover confirm.

// Preview modal events
closePreviewBtn.addEventListener('click', () => {
    previewModal.style.display = 'none';
    isPreviewOpen = false;
    finishPreviewLedgerLoading(); closePreviewLedgerOptions();
    hideRowHistoryPopover();
    closeSharedRangePopover();
});
closeHistoryBtn.addEventListener('click', () => {
    historyRequestSequence++;
    historyModal.style.display = 'none';
});
function closeTemplateDeleteModal() {
    if (nativeDataBank && nativePurgeController && !nativePurgePostStarted) {
        nativePurgeSequence++; nativePurgeController.abort(); templateDeleteModal.style.display = 'none'; pendingTemplateDelete = null; return;
    }
    if (pendingTemplateDelete && deleteTemplateInFlight.has(String(pendingTemplateDelete.templateId))) return;
    templateDeleteModal.style.display = 'none';
    pendingTemplateDelete = null;
    templateDeleteStatus.textContent = '';
    templateDeleteStatus.style.display = 'none';
}

backupAndDeleteTemplateBtn.addEventListener('click', runTemplateBackupAndDelete);
cancelTemplateDeleteBtn.addEventListener('click', closeTemplateDeleteModal);
closeTemplateDeleteBtn.addEventListener('click', closeTemplateDeleteModal);
templateDeleteModal.addEventListener('click', (event) => {
    if (event.target === templateDeleteModal) closeTemplateDeleteModal();
});

historyModal.addEventListener('click', (event) => {
    if (event.target === historyModal) {
        historyRequestSequence++;
        historyModal.style.display = 'none';
    }
});
historyActionFilter.addEventListener('change', () => fetchUploadHistory(1));
historyLedgerSelect.addEventListener('change', () => fetchUploadHistory(1));
historyPrevBtn.addEventListener('click', () => {
    if (historyPage > 1) fetchUploadHistory(historyPage - 1);
});
historyNextBtn.addEventListener('click', () => {
    if (historyPage < historyTotalPages) fetchUploadHistory(historyPage + 1);
});
previewModal.addEventListener('click', (e) => {
    if (e.target === previewModal) {
        previewModal.style.display = 'none';
        isPreviewOpen = false;
        finishPreviewLedgerLoading(); closePreviewLedgerOptions();
        hideRowHistoryPopover();
        closeSharedRangePopover();
    }
});

// Pagination
firstPageBtn.addEventListener('click', () => {
    if (currentPage > 1) {
        currentPage = 1;
        fetchPreviewData();
    }
});
prevPageBtn.addEventListener('click', () => {
    if (currentPage > 1) {
        currentPage--;
        fetchPreviewData();
    }
});
nextPageBtn.addEventListener('click', () => {
    const totalPages = Math.ceil(previewTotalRows / previewLimit) || 1;
    if (currentPage < totalPages) {
        currentPage++;
        fetchPreviewData();
    }
});
lastPageBtn.addEventListener('click', () => {
    const totalPages = Math.ceil(previewTotalRows / previewLimit) || 1;
    if (currentPage < totalPages) {
        currentPage = totalPages;
        fetchPreviewData();
    }
});

// Page size change
pageSizeSelect.addEventListener('change', function() {
    previewLimit = parseInt(this.value) || 50;
    currentPage = 1;
    if (isPreviewOpen) {
        fetchPreviewData();
    }
});

// Preview filters
previewLedgerInput.addEventListener('focus', () => {
    renderPreviewLedgerOptions(previewLedgerAccountCode ? '' : previewLedgerInput.value);
    previewLedgerInput.select();
});
previewLedgerInput.addEventListener('input', () => {
    previewLedgerAccountCode = '';
    previewLedgerInput.title = '';
    clearPreviewForAccount('Select an account from the matching uploaded ledgers.');
    renderPreviewLedgerOptions(previewLedgerInput.value);
});
previewLedgerToggle.addEventListener('click', () => {
    if (!previewLedgerOptions.hidden) closePreviewLedgerOptions();
    else {
        previewLedgerInput.focus();
        renderPreviewLedgerOptions();
    }
});
previewLedgerField.addEventListener('focusout', e => {
    if (!previewLedgerField.contains(e.relatedTarget)) restorePreviewLedgerSelection();
});
previewLedgerField.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !previewLedgerOptions.hidden) {
        e.preventDefault(); e.stopPropagation(); restorePreviewLedgerSelection();
    } else if (e.target === previewLedgerInput && ['ArrowDown', 'ArrowUp'].includes(e.key)) {
        e.preventDefault();
        if (previewLedgerOptions.hidden) renderPreviewLedgerOptions(previewLedgerAccountCode ? '' : previewLedgerInput.value);
        if (!previewLedgerMatches.length) return;
        previewLedgerActiveIndex = previewLedgerActiveIndex < 0
            ? (e.key === 'ArrowDown' ? 0 : previewLedgerMatches.length - 1)
            : (previewLedgerActiveIndex + (e.key === 'ArrowDown' ? 1 : -1)
                + previewLedgerMatches.length) % previewLedgerMatches.length;
        const options = [...previewLedgerOptions.querySelectorAll('[role="option"]')];
        options.forEach((option, index) => option.classList.toggle('active', index === previewLedgerActiveIndex));
        const active = options[previewLedgerActiveIndex];
        previewLedgerInput.setAttribute('aria-activedescendant', active.id);
        active.scrollIntoView({block: 'nearest'});
    } else if (e.target === previewLedgerInput && e.key === 'Enter') {
        e.preventDefault();
        const account = previewLedgerMatches[previewLedgerActiveIndex] || (previewLedgerMatches.length === 1 ? previewLedgerMatches[0] : null);
        if (!previewLedgerOptions.hidden && account) selectPreviewLedgerAccount(account);
    }
});
previewLedgerRetry.addEventListener('click', async () => {
    if (!isPreviewOpen) return;
    if (previewAccountState === 'error') {
        openPreview(previewTemplateId, previewTemplateName, previewMode);
    } else {
        await loadPreviewLedgerAccounts(previewOpenSequence, getSelectedClientId(), previewTemplateId, previewLastLedgerAccountCode);
        if (isPreviewOpen) fetchPreviewData();
    }
});
applyFiltersBtn.addEventListener('click', applyPreviewFilters);
// Modal clear button (using the updated ID from the HTML fix)
const modalClearBtn = document.getElementById('clearPreviewFiltersBtn');
if (modalClearBtn) {
    modalClearBtn.addEventListener('click', clearPreviewFilters);
}

// The main clear button is already handled.

// Sunday checkbox
sundayOnlyCheckbox.addEventListener('change', function() {
    if (isPreviewOpen) {
        currentPage = 1;
        fetchPreviewData();
    }
});

// Subtotal toggle: a display setting, not a data filter - re-renders the
// already-fetched current page from previewCachedRows, no refetch.
subtotalToggleCheckbox.addEventListener('change', function() {
    previewShowSubtotals = this.checked;
    if (isPreviewOpen) {
        renderPreviewRows(previewCachedRows);
    }
});

// Data quality filter (All / Complete / Incomplete)
dataQualityFilter.addEventListener('change', function() {
    if (isPreviewOpen) {
        currentPage = 1;
        fetchPreviewData();
    }
});

// Search with debounce
let searchTimeout;
searchInput.addEventListener('input', function() {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
        if (isPreviewOpen) {
            currentPage = 1;
            fetchPreviewData();
        }
    }, 300);  // Fast Data Handling Phase 6 of 13: retimed from 400ms to 300ms
});

// Amount filters – apply on Enter key
amountMin.addEventListener('keydown', (e) => { if (e.key === 'Enter' && isPreviewOpen) applyPreviewFilters(); });
amountMax.addEventListener('keydown', (e) => { if (e.key === 'Enter' && isPreviewOpen) applyPreviewFilters(); });

// Cashflows classification filters refresh only the derived-layer query.
classificationTypeFilter.addEventListener('change', () => {
    if (isPreviewOpen && previewMode === 'classification') applyPreviewFilters();
});
[classificationAccountFilter, classificationCategoryFilter].forEach((input) => {
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && isPreviewOpen && previewMode === 'classification') applyPreviewFilters();
    });
});
// Shared date range modal (Phase 2 — Unified date range): the preview
// modal's own trigger opens it in "live" mode (no pending template —
// confirming just re-fetches the currently open preview).
modalDateRangeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    openSharedRangePopover('live');
});
sharedRangeConfirmBtn.addEventListener('click', confirmSharedRangePopover);
// 3-fix: close (×) button, matching #previewModal/#templateCalendarModal's
// own close-button pattern.
closeSharedRangeBtn.addEventListener('click', closeSharedRangePopover);
// Clicking the overlay backdrop itself (not the .modal-content inside it)
// closes the modal — same self-click pattern already used by
// previewModal/templateCalendarModal below, replacing the old anchored-
// popover's document-level "click outside" check (see further down).
sharedRangePopover.addEventListener('click', (e) => {
    if (e.target === sharedRangePopover) {
        closeSharedRangePopover();
    }
});
// Enter-to-confirm from either date field, same convention as amountMin/amountMax.
sharedRangeFromInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') confirmSharedRangePopover();
});
sharedRangeToInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') confirmSharedRangePopover();
});

// Close the row history popover when clicking anywhere outside it (the icon's
// own click is stopped from propagating here, so this only fires for clicks
// elsewhere on the page). The shared date range modal is no longer handled
// here — as a centred overlay it has its own self-click-to-close listener
// (see sharedRangePopover.addEventListener('click', ...) above), matching
// previewModal/templateCalendarModal's own pattern.
document.addEventListener('click', (e) => {
    if (rowHistoryPopover.style.display === 'block' && !rowHistoryPopover.contains(e.target)) {
        hideRowHistoryPopover();
    }
});

// ESC key closes preview (or just the shared range modal / template
// calendar modal, if either is open)
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && templateDeleteModal.style.display === 'flex') {
        closeTemplateDeleteModal();
        return;
    }
    if (e.key === 'Escape' && historyModal.style.display === 'flex') {
        historyRequestSequence++;
        historyModal.style.display = 'none';
        return;
    }
    if (e.key === 'Escape' && sharedRangePopover.style.display === 'flex') {
        closeSharedRangePopover();
        return;
    }
    if (e.key === 'Escape' && templateCalendarModal.style.display === 'flex') {
        closeTemplateCalendarPopover();
        return;
    }
    if (e.key === 'Escape' && isPreviewOpen) {
        previewModal.style.display = 'none';
        isPreviewOpen = false;
        finishPreviewLedgerLoading(); closePreviewLedgerOptions();
    }
    // Ctrl+Enter triggers check availability
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        fetchAvailability();
    }
});

// ---------- CLIENT CHANGE HOOK (with debounce) ----------
let clientChangeTimeout;
window.onClientChange = function(clientId) {
    clearTimeout(clientChangeTimeout);
    if (nativeDataBank) {
        if (nativePurgeController && !nativePurgePostStarted) { nativePurgeSequence++; nativePurgeController.abort(); }
        templateDeleteModal.style.display = 'none'; pendingTemplateDelete = null;
        cancelNativeDownload(); historyRequestSequence++; historyModal.style.display = 'none'; hideRowHistoryPopover();
        invalidateDataBankCaches();
        resetDatasetTables('Client changed. Loading current data…');
        [statsTotalRecords, statsTotalTemplates, statsClientDbSize, statsOverdueUploads].forEach(el => el.textContent = '—');
        overdueUploadsData = []; renderOverdueUploadsTable();
        calendarRequestSequence++; closeTemplateCalendarPopover();
        previewModal.style.display = 'none'; isPreviewOpen = false;
    }
    previewRequestSequence++; previewOpenSequence++;
    setPreviewMetricsState();
    finishPreviewLedgerLoading(); closePreviewLedgerOptions();
    if (templateDeleteModal.style.display === 'flex') closeTemplateDeleteModal();
    clientChangeTimeout = setTimeout(() => {
        if (clientId) {
            historyRequestSequence++;
            historyModal.style.display = 'none';
            invalidateDataBankCaches();
            // Redesign Phase 6: reads the popover's own inputs, now the
            // persisted date-range state (see getDateRangeFromFilter()).
            if (sharedRangeFromInput.value && sharedRangeToInput.value) {
                fetchAvailability();
            } else {
                setDefaultDateRange();
                fetchAvailability();
            }
            // If preview is open, close it
            if (isPreviewOpen) {
                previewModal.style.display = 'none';
                isPreviewOpen = false;
            }
            // Coverage Calendar → per-template popover fix: presence data is
            // per-client, so a stale calendar for the previous client's
            // template shouldn't stay open — close it rather than refetching,
            // matching the preview-modal-close branch just above.
            if (templateCalendarModal.style.display === 'flex') {
                closeTemplateCalendarPopover();
            }
        }
    }, 300);
};

async function openClassificationFromQuery() {
    if (nativeDataBank) return;
    const query = new URLSearchParams(window.location.search);
    if (query.get('cashflows_classification') !== '1') return;
    const templateId = Number(query.get('template_id'));
    if (!templateId) return;
    const start = query.get('date_start');
    const end = query.get('date_end');
    if (start) sharedRangeFromInput.value = start;
    if (end) sharedRangeToInput.value = end;
    await openClassificationPreview(templateId, query.get('template_name') || 'Transaction Report');
}
// ---------- INIT ----------
document.getElementById('nativeExportMatching').addEventListener('click', () => nativeDownload(previewTemplateId));
document.getElementById('nativeDownloadCancel').addEventListener('click', cancelNativeDownload);
previewModal.addEventListener('input', updateNativeExportButton);
previewModal.addEventListener('change', updateNativeExportButton);
window.addEventListener('focus', () => { if (nativeDataBank?.history && historyModal.style.display === 'flex') void fetchUploadHistory(historyPage); });

// P33 is a separate native path. Legacy paged/browser-built backups and purge
// URLs above remain Python-only. No write is automatically retried.
let nativePurgeController = null;
let nativePurgeSequence = 0;
let nativePurgePostStarted = false;
let nativePurgeRecoveryBusy = false;
function nativePurgeAvailable() {
    const capability = window.AppNativeStorageCleanup;
    return !!(capability?.enabled && capability.purge && capability.canApply && nativeDataBank?.exports && nativeDataBank.history && nativeDataBank.canBackup);
}
function nativePurgeMessage(operation) {
    return operation.artifact_status === 'complete' ? 'Stored data deleted; registered temporary-file cleanup complete.'
        : operation.artifact_status === 'pending' ? 'Stored data deleted; temporary-file cleanup is pending.'
        : 'Stored data deleted; temporary-file cleanup needs review.';
}
function showNativePurgeRecovery(message) {
    const node = document.getElementById('nativePurgeRecovery'); node.hidden = false; node.replaceChildren();
    const text = document.createElement('p'); text.textContent = message;
    const check = document.createElement('button'); check.type = 'button'; check.textContent = 'Check recorded outcome';
    check.addEventListener('click', () => void checkNativePurgeRecovery());
    const link = document.createElement('a'); link.href = '/static/storage_cleanup.html'; link.textContent = ' Review cleanup outcomes';
    node.append(text, check, link);
}
async function checkNativePurgeRecovery() {
    if (!window.AppNativeStorageCleanup?.enabled || nativePurgeRecoveryBusy) return;
    let handle;
    try { handle = JSON.parse(sessionStorage.getItem('native-cleanup-operation') || 'null'); } catch (_) { return; }
    if (!handle) return;
    if (handle.client_id !== Number(getSelectedClientId())) { showNativePurgeRecovery('An earlier cleanup needs checking in its original client.'); return; }
    nativePurgeRecoveryBusy = true;
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 35000);
    try {
        const response = await window.ApiUtils.apiFetch(`/api/storage-cleanup/operations/${encodeURIComponent(handle.operation_id)}?client_id=${handle.client_id}&template_id=${handle.template_id}`, { cache: 'no-store', signal: controller.signal });
        const body = await response.json();
        if (!response.ok || body.operation_id !== handle.operation_id) throw new Error('The earlier outcome is not yet confirmed. Check again before retrying.');
        if (handle.client_id !== Number(getSelectedClientId())) return;
        showNativePurgeRecovery(body.kind === 'purge' ? nativePurgeMessage(body) : 'Temporary-plan cleanup committed.');
        if (body.artifact_status === 'complete') try { sessionStorage.removeItem('native-cleanup-operation'); } catch (_) {}
        invalidateDataBankCaches({ clientId: handle.client_id, templateId: handle.template_id });
        await fetchAvailability();
    } catch (error) { showNativePurgeRecovery(error.message || 'Outcome not confirmed; no deletion was retried.'); }
    finally { clearTimeout(timeout); nativePurgeRecoveryBusy = false; }
}
async function runNativeTemplatePurge() {
    if (!pendingTemplateDelete || !nativePurgeAvailable() || nativePurgeController
        || document.getElementById('nativePurgeConfirmation').value !== 'PERMANENTLY DELETE SELECTED DATA') return;
    const scope = { ...pendingTemplateDelete }, key = String(scope.templateId), sequence = ++nativePurgeSequence;
    if (scope.clientId !== Number(getSelectedClientId())) return;
    const controller = new AbortController(); nativePurgeController = controller; nativePurgePostStarted = false;
    deleteTemplateInFlight.add(key); backupAndDeleteTemplateBtn.disabled = true;
    const status = text => { if (pendingTemplateDelete && sequence === nativePurgeSequence) { templateDeleteStatus.style.display = 'block'; templateDeleteStatus.textContent = text; } };
    const current = () => sequence === nativePurgeSequence && scope.clientId === Number(getSelectedClientId()) && document.visibilityState === 'visible';
    const params = new URLSearchParams({ client_id: scope.clientId, template_id: scope.templateId });
    let operation = null, timer = setTimeout(() => controller.abort(), 75000);
    try {
        status('Checking the exact deletion scope…');
        const previewResponse = await window.ApiUtils.apiFetch(`/api/storage-cleanup/preview?${params}&kind=purge`, { cache: 'no-store', signal: controller.signal });
        const preview = await previewResponse.json();
        if (!previewResponse.ok || !preview.can_apply) throw new Error(preview.blockers?.includes('upload_transfer_grace')
            ? 'A recent upload is still within its transfer safety window. Wait a few minutes and refresh. No deletion was submitted.'
            : 'Protected references, active uploads or stored data need review. No deletion was submitted.');
        if (!current() || preview.client_id !== scope.clientId || preview.template_id !== scope.templateId || preview.kind !== 'purge') throw new Error('Selection changed. No deletion was submitted.');
        status(`Preparing the complete backup: ${Number(preview.counts.client_data).toLocaleString()} current rows and ${Number(preview.counts.client_data_history).toLocaleString()} prior rows…`);
        const download = await window.ApiUtils.apiFetch(`/api/data/template/${scope.templateId}/backup?client_id=${scope.clientId}&purge=true`, { cache: 'no-store', signal: controller.signal });
        if (!download.ok) throw new Error('The complete backup could not be prepared. No deletion was submitted.');
        assertNativeClient(download, scope.clientId);
        const proof = download.headers.get('X-Purge-Backup-Proof');
        if (!proof || download.headers.get('X-DataBank-Template') !== String(scope.templateId) || download.headers.get('X-Export-Scope') !== 'backup') throw new Error('Backup scope could not be verified.');
        const bytes = await download.arrayBuffer();
        if (!bytes.byteLength || bytes.byteLength > nativeDataBank.maximumExportBytes || !current()) throw new Error('Backup limits or selection changed. No deletion was submitted.');
        const header = download.headers.get('Content-Disposition') || '';
        const match = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(header);
        const name = (match ? decodeURIComponent(match[1]) : 'template_backup.xlsx').replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_');
        const url = URL.createObjectURL(new Blob([bytes], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }));
        const link = document.createElement('a'); link.href = url; link.download = name; document.body.appendChild(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
        if (!current()) throw new Error('Selection changed. The backup started; no deletion was submitted.');
        operation = crypto.randomUUID();
        // Persist before submitting, so a lost response/refresh can reconcile by ID.
        try { sessionStorage.setItem('native-cleanup-operation', JSON.stringify({ operation_id: operation, client_id: scope.clientId, template_id: scope.templateId })); }
        catch (_) { throw new Error('Recovery information could not be saved. No deletion was submitted.'); }
        nativePurgePostStarted = true; cancelTemplateDeleteBtn.disabled = true; closeTemplateDeleteBtn.disabled = true;
        clearTimeout(timer); timer = setTimeout(() => controller.abort(), 70000);
        status('Backup download started. Submitting the confirmed deletion…');
        const response = await window.ApiUtils.apiFetchMutate(`/api/storage-cleanup/apply?${params}&kind=purge`, {
            method: 'POST', signal: controller.signal, headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ operation_id: operation, preview_token: preview.preview_token, backup_token: proof, confirmation: 'PERMANENTLY DELETE SELECTED DATA' })
        });
        const result = await response.json();
        if (!response.ok || result.operation_id !== operation || result.client_id !== scope.clientId || result.template_id !== scope.templateId)
            throw new Error('Deletion was not confirmed. Check the recorded outcome before retrying.');
        if (result.artifact_status === 'complete') try { sessionStorage.removeItem('native-cleanup-operation'); } catch (_) {}
        if (scope.clientId !== Number(getSelectedClientId())) return;
        templateDeleteModal.style.display = 'none'; pendingTemplateDelete = null;
        invalidateDataBankCaches({ clientId: scope.clientId, templateId: scope.templateId });
        showNativePurgeRecovery(nativePurgeMessage(result)); showStatus(nativePurgeMessage(result), 'success');
        await fetchAvailability();
    } catch (error) {
        const message = nativePurgePostStarted ? 'Deletion outcome is not confirmed. Check the recorded outcome; no write was retried.' : error.message || 'Backup cancelled. No deletion was submitted.';
        status(message);
        if (operation && nativePurgePostStarted) {
            // The recovery action is outside the modal. Release the overlay
            // after an uncertain response so the user can check its outcome.
            templateDeleteModal.style.display = 'none'; pendingTemplateDelete = null;
            showNativePurgeRecovery(message);
        }
        else if (operation) try { sessionStorage.removeItem('native-cleanup-operation'); } catch (_) {}
    } finally {
        clearTimeout(timer); nativePurgeController = null; nativePurgePostStarted = false; deleteTemplateInFlight.delete(key);
        cancelTemplateDeleteBtn.disabled = false; closeTemplateDeleteBtn.disabled = false;
        backupAndDeleteTemplateBtn.disabled = document.getElementById('nativePurgeConfirmation').value !== 'PERMANENTLY DELETE SELECTED DATA';
    }
}
document.getElementById('nativePurgeConfirmation').addEventListener('input', () => {
    if (nativeDataBank) backupAndDeleteTemplateBtn.disabled = !!nativePurgeController || document.getElementById('nativePurgeConfirmation').value !== 'PERMANENTLY DELETE SELECTED DATA';
});
window.addEventListener('focus', () => { if (nativeDataBank && !nativePurgeController) void checkNativePurgeRecovery(); });
window.addEventListener('load', async () => { if (nativeDataBank) { if (window.clientContextReady) await window.clientContextReady; await checkNativePurgeRecovery(); } });
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden' && nativePurgeController && !nativePurgePostStarted) { nativePurgeSequence++; nativePurgeController.abort(); }
});
window.onload = async function() {
    // Show feedback before session/settings initialization so the user never
    // lands on an apparently idle Client Data page.
    showClientDataLoading();
    if (!nativeReadAvailable()) {
        await hideClientDataLoading();
        showStatus('Native Data Bank reads are disabled. Continue using the accepted Python application.', 'info');
        resetDatasetTables('Data Bank reads are unavailable.');
        document.querySelectorAll('.main-content button, .main-content input, #uploadMoreBtn').forEach(el => el.disabled = true);
        return;
    }

    // client_context.js populates the session-backed client cache from an async fetch. window.onload
    // is not a safe barrier for that (see the ready-signal note in client_context.js) -- without this
    // await, getSelectedClientId() below returns null and this page redirects to the dashboard after
    // 3 seconds, despite a valid session with a client selected.
    if (window.clientContextReady) await window.clientContextReady;

    // Load utilities first
    if (typeof initUtils === 'function') {
        await initUtils();
    }

    setDefaultDateRange();

    const clientId = getSelectedClientId();
    if (!clientId) {
        await hideClientDataLoading();
        showStatus('⚠️ Please select a client from the Dashboard first.', 'error');
        resetDatasetTables('⚠️ No client selected. Please go to the Dashboard and select a client.');
        setTimeout(() => {
            window.location.href = '/static/index.html';
        }, 3000);
        return;
    }

    await fetchAvailability();
    await openClassificationFromQuery();
};
