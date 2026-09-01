// ==============================================
// ✅ VERIFICATION MODULE – CASHBOOK + VERIFICATION FLOW
// ==============================================

// API_BASE defined in client_context.js

// Bank Verification (Phase 8): read once at load, matching the existing ?client_id= precedent
// in client_settings.js. Drives category filtering/labels/session_type for the rest of this
// page load - never re-read after this point. Anything other than exactly 'bank' defaults to
// 'cash', matching today's only-cash behavior for direct navigation with no ?type param.
//
// Global quick-nav deep-link (Phase 3): the Cashbook/Bank book manifest entries link here with
// ?view=cashbook / ?view=bankbook instead of ?type=. ?view= is checked first and, when present
// with a recognised value, wins over ?type= — per the confirmed spec, the two params shouldn't
// both appear at once (the manifest only ever builds ?view= links), but if they somehow did,
// ?view= targets a specific screen so it takes priority. Any other/missing ?view= value falls
// through to the existing ?type= resolution unchanged.
const urlSearchParamsForType = new URLSearchParams(window.location.search);
// Opt-in diagnostics expose timings/counts only and remain inert without ?perf=1.
window.VerificationPerf = {
    enabled: urlSearchParamsForType.get('perf') === '1',
    querySuffix() { return this.enabled ? '?perf=true' : ''; },
    log(operation, startedAt, details = {}) {
        if (!this.enabled) return;
        console.info('[verification-perf]', {
            operation,
            elapsed_ms: Math.round((performance.now() - startedAt) * 10) / 10,
            ...details
        });
    }
};
const viewParam = urlSearchParamsForType.get('view');
const currentType = (viewParam === 'bankbook') ? 'bank'
    : (viewParam === 'cashbook') ? 'cash'
    : (urlSearchParamsForType.get('type') === 'bank') ? 'bank' : 'cash';

let currentSessionId = null;
let currentDetailId = null;
let sessionRulebook = null;
let verificationPresenceHeartbeat = null;

function startVerificationPresence(sessionId) {
    const body = JSON.stringify({ session_id: sessionId });
    fetch(`${API_BASE}/api/verification/presence/enter`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    if (verificationPresenceHeartbeat) clearInterval(verificationPresenceHeartbeat);
    verificationPresenceHeartbeat = setInterval(() => {
        fetch(`${API_BASE}/api/verification/presence/heartbeat`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    }, 30000);
}

function stopVerificationPresence() {
    if (!currentSessionId) return;
    const body = JSON.stringify({ session_id: currentSessionId });
    if (verificationPresenceHeartbeat) clearInterval(verificationPresenceHeartbeat);
    verificationPresenceHeartbeat = null;
    if (navigator.sendBeacon) {
        navigator.sendBeacon(`${API_BASE}/api/verification/presence/leave`, new Blob([body], { type: 'application/json' }));
    } else {
        fetch(`${API_BASE}/api/verification/presence/leave`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true });
    }
}

// Shared by every navigation exit from an active Verification session. The unfinished business
// session stays resumable; only the active-presence lease is released after the user confirms.
async function leaveVerificationSafely(continueNavigation) {
    if (!currentSessionId) {
        await continueNavigation();
        return true;
    }
    const confirmed = window.confirm(
        'You are in an active Verification session. Leave the session safely before continuing?'
    );
    if (!confirmed) return false;
    await saveAllVoucherLines();
    stopVerificationPresence();
    await continueNavigation();
    return true;
}
window.leaveVerificationSafely = leaveVerificationSafely;
// Header format cleanup: the active session's own date range (YYYY-MM-DD, same format the
// header displays directly, no reformatting needed) - distinct from any individual voucher's own
// date. Set from GET audited_cashbook's session.period_start/period_end wherever that's fetched
// fresh (loadTransactionList(), restoreSessionView()); read by renderVoucherHeader().
let currentSessionPeriodStart = null;
let currentSessionPeriodEnd = null;
// Header format cleanup: one-shot caption text for the fixed-height slot under "Voucher X of Y"
// (#voucherNavCaption) - set immediately before a navigateToTransaction() call that should show
// it (currently only the orphaned-voucher auto-navigate case in reconcileActiveSessionRange()),
// then reset to '' by renderVoucherHeader() right after it's consumed, so every other navigation
// path naturally renders it blank with no per-call-site clearing needed.
let voucherNavCaptionText = '';
let currentClientDataId = null;
let currentVoucherId = null;
let currentTransactionType = '';
let lineItems = [];
let procedureData = {};
let isNavigating = false;
// Phase 3: per-line client state for the collapsed/expand voucher lines list, keyed by
// detail_id. Fully separate from currentTransactionType/procedureData above, which remain
// scoped to the session's single "active" line — no shared state between the two.
let voucherLineState = {};
// Monotonic per-line revisions distinguish changed lines from untouched lines that navigation
// previously re-saved every time.
let voucherLineDirtyRevisions = {};

function markVoucherLineDirty(detailId, reason = 'line change') {
    if (detailId == null) return;
    const key = String(detailId);
    voucherLineDirtyRevisions[key] = (voucherLineDirtyRevisions[key] || 0) + 1;
    if (window.VerificationPerf.enabled) {
        console.info('[verification-perf]', {
            operation: 'mark_line_dirty', detail_id: key,
            revision: voucherLineDirtyRevisions[key], reason
        });
    }
}

function markAllVoucherLinesDirty(reason = 'voucher-wide change') {
    Object.keys(voucherLineState).forEach(uid => markVoucherLineDirty(uid, reason));
}

function isVoucherLineDirty(detailId) {
    return (voucherLineDirtyRevisions[String(detailId)] || 0) > 0;
}
// Processing/Loading UI, Phase 6: incremented once per renderVoucherHeader() call (once per new
// voucher painted) — lets a slow-resolving PREVIOUS voucher's completion recognize itself as
// stale and skip hiding the badge, instead of wrongly hiding the NEW voucher's still-loading one.
let voucherLoadToken = 0;
// Ref-counted so two independent producers (the automatic per-voucher line batch in
// renderVoucherLines(), and openProcedureModal()'s click-triggered loads via the adapter) can each
// show/hide this one shared badge without one hiding it out from under the other. Reset to 0 in
// renderVoucherHeader() — see the voucherLoadToken comment above for why.
let voucherLoadingBadgeRefCount = 0;
// Prev/Next processing UI: true for the whole duration of one voucher navigation — from the click,
// through the save/validate phase (several sequential POSTs, the ~3s the user used to spend staring
// at an unchanged screen), through the fetch and render. Read by getVoucherNavAllowed()'s callers
// as ONE of two independent conditions gating the Prev/Next buttons; see
// applyVoucherNavButtonState() for why it is never a competing "last write wins" state.
let voucherNavInFlight = false;
// The voucherLoadToken value current when runGuardedVoucherNav() showed its badge. Compared again
// before hiding it — see hideVoucherNavBadge() for the cross-voucher hazard this closes.
let voucherNavBadgeToken = null;
// Phase C: transient click-to-lock focus aid — the detail_id of the one voucher line currently
// "locked"/dimmed, or null if none. Purely visual, not persisted; reset whenever a new voucher's
// lines are rendered (see renderVoucherLines()).
let lockedLineUid = null;
// Voucher Supporting-Document Linking, Phase 3 of 6 (shell)/Phase 4 (real content): per-voucher-
// line state for the tabbed #procedureModal. Keyed by voucher line uid; each entry is
// { activeDocId, documents, assignedDocIds, voucherLineAmount,
//   tabs: { [docId]: { loaded, searchTerm, searchResults, selections, observation } } }.
// documents/assignedDocIds/voucherLineAmount are (re-)fetched/computed fresh on every modal open
// (see openProcedureModal()) so they can never drift from server state; each tab's own
// searchTerm/searchResults/selections persist across tab switches within one modal-open session
// (not cleared on close, only on voucher navigation, in seedVoucherLineState()). `observation`
// stays an unused placeholder — Phase 6's territory.
let procedureModalState = {};
// Phase 4: the currently loaded voucher's full lines array (row_data included), set alongside
// voucherLineState in seedVoucherLineState() — lets openProcedureModal() look up a line's own
// amount for the mismatch badge without a second fetch.
let currentVoucherLines = [];
let voucherSupportCache = null;
let voucherSupportLoading = null;

async function loadVoucherSupportData() {
    const anchor = currentVoucherLines[0] && currentVoucherLines[0].detail_id;
    if (!anchor) return {};
    if (voucherSupportCache && String(voucherSupportCache.voucher_line_id) === String(anchor)) {
        console.debug('[voucher-support-cache] hit', { anchor: String(anchor) });
        return voucherSupportCache.lines || {};
    }
    if (voucherSupportLoading) {
        console.debug('[voucher-support-cache] in-flight reuse', { anchor: String(anchor) });
        return voucherSupportLoading;
    }
    console.debug('[voucher-support-cache] miss/request', { anchor: String(anchor) });
    voucherSupportLoading = window.ApiUtils.apiFetch(`${API_BASE}/api/verification/voucher_support_data/${anchor}`)
        .then(async res => {
            if (!res.ok) return {};
            const data = await res.json();
            voucherSupportCache = data;
            return data.lines || {};
        }).catch(() => ({})).finally(() => { voucherSupportLoading = null; });
    return voucherSupportLoading;
}

// Drop the cached voucher-support payload (procedure ticks + observation rollup for every line in
// the loaded voucher). save_and_verify_voucher_bundle persists all of currentVoucherLines in one
// request, and save_line_items persists one line (also flushing its observation text) — but this
// cache is a single voucher-wide object (.lines keyed by line), so any successful save must clear
// it, or hideProcedureModalOnly()'s fire-and-forget badge refresh
// (updateProcedures/ObservationsCellForLine → loadVoucherSupportData) serves the pre-save copy and
// the Procedures/Observations badges show stale state until the next voucher navigation. Mirrors
// invalidateProcedureModalDataForVoucher() in procedure_modal.js, which handles the sibling
// procedure_modal_data cache the same way. Nulling voucherSupportLoading too matches the hard reset
// in seedVoucherLineState(); a save is never concurrent with a support-data fetch in practice.
function invalidateVoucherSupportCache() {
    voucherSupportCache = null;
    voucherSupportLoading = null;
    console.debug('[voucher-support-cache] invalidated', { reason: 'saved-data-changed' });
}

// ---------- PROCEDURE MODAL ADAPTER (Phase 1b) ----------
// static/procedure_modal.js (the shared Supporting-Document modal, extracted from this file) no
// longer reads this file's globals directly — it receives them through this adapter object,
// passed to openProcedureModal() at each real open-time call site below. Built fresh per call
// (cheap — a handful of function refs + getters) rather than once at load, purely to keep the
// mechanism obviously matching "adapter passed at open-time"; a stale adapter object would behave
// identically either way, since every piece of live state below is exposed as a `get` accessor
// (always resolves the CURRENT value of these variables at read-time, not a snapshot taken when
// this function ran) and every function reference is a stable, never-reassigned declaration.
// escapeHtml is included here (not duplicated locally in procedure_modal.js) specifically to
// avoid a same-named top-level `function` declaration in both classic <script> files silently
// shadowing one another depending on load order. API_BASE is NOT included — it's a page-level
// global from client_context.js, not a verification.js dependency, and every host page already
// loads that script independently.
function buildProcedureModalAdapter() {
    return {
        getClientId,
        getVoucherPosition,
        showStatus,
        saveLineItems,
        buildLineSavePayload,
        saveAndVerifyVoucher,
        saveAllVoucherLines,
        markLineDirty: markVoucherLineDirty,
        markAllLinesDirty: markAllVoucherLinesDirty,
        escapeHtml,
        getVoucherLineAlias,
        getVoucherLineAmount,
        renderGateRowHtml,
        renderWorkingPaperGatesHtml,
        fetchAuditGuidanceData,
        updateProceduresCellForLine,
        updateObservationsCellForLine,
        parseVoucherAmount,
        computeLineRiskData,
        updateProgressDisplay,
        applyVoucherNavRenderData,
        get VOUCHER_LINE_ACCOUNT_NAME_ALIASES() { return VOUCHER_LINE_ACCOUNT_NAME_ALIASES; },
        get VOUCHER_LINE_DESCRIPTION_ALIASES() { return VOUCHER_LINE_DESCRIPTION_ALIASES; },
        get currentVoucherId() { return currentVoucherId; },
        get currentSessionId() { return currentSessionId; },
        get sessionRulebook() { return sessionRulebook; },
        get currentVoucherLines() { return currentVoucherLines; },
        get voucherLineState() { return voucherLineState; },
        get procedureModalState() { return procedureModalState; }
    };
}

let selectedCashbook = null;
let cashbookCurrentPage = 1;
let cashbookPageSize = 50;
let cashbookTotalRows = 0;
let totalTransactions = 0;
// Single-calendar date range picker (Phase 1): the reusable initDateRangePicker() instance
// mounted in place of the old two-input Start/End Date fields in the open-cashbook dialog
// (#sessionPopup). Set once by mountPopupDateRangePicker() in the event-listener wiring below.
let popupDateRangePicker = null;
// Single-calendar date range picker (Phase 2): the in-session cashbook filter bar's own,
// independently-editable instance — mirrored to popupDateRangePicker above (see
// handleCashbookDateRangeChange()), never fetches on its own (that stays gated behind the
// existing "📊 Apply" button/applyCashbookPreview()). Set once by mountCashbookDateRangePicker().
let cashbookDateRangePicker = null;

// DOM refs
const dashboardContent = document.getElementById('dashboardContent');
const sessionContent = document.getElementById('verificationSessionContent');
const cashbookContent = document.getElementById('cashbookContent');
const popupOverlay = document.getElementById('sessionPopupOverlay');
const popupCashbookSelect = document.getElementById('popupCashbookSelect');
const popupStartBtn = document.getElementById('popupStartBtn');
const popupCancelBtn = document.getElementById('popupCancelBtn');
const popupError = document.getElementById('popupError');
// Editable search-range inputs (confirmed fix) - populated with the auto-detected default range
// by loadCashbooks(), editable afterward to re-trigger the search with a different range.
const popupPeriodStart = document.getElementById('popupPeriodStart');
const popupPeriodEnd = document.getElementById('popupPeriodEnd');
// Client Data Links card strip - populated dynamically by loadDataLinksCards() from the real
// templates list, so it never needs a hardcoded per-template edit again.
const dataLinksCardStrip = document.getElementById('dataLinksCardStrip');
// Quick Stats card grid - loading target for loadVerificationStats()'s spinner.
const verificationStatsGrid = document.querySelector('.stats-grid');

const statusDiv = document.getElementById('verificationStatus');
// voucherVerifyToast DOM ref now lives in procedure_modal.js.
// Phase 2: standalone transaction view removed — prevBtn/nextBtn/verifyBtn/saveProgressBtn/exitBtn
// elements no longer exist in verification.html (the nav controls are now in #voucherHeaderBar).
// Their JS references and listener blocks have been removed alongside the old view.

const cashbookNameDisplay = document.getElementById('cashbookNameDisplay');
// Phase 1 of 3 (Cashbook Preview selector): label + dropdown in the date-range bar, populated
// by openCashbook() below. UI + population only this phase - no change listener yet (Phase 2).
const cashbookSelectorLabel = document.getElementById('cashbookSelectorLabel');
const cashbookSelector = document.getElementById('cashbookSelector');
const cashbookStartDate = document.getElementById('cashbookStartDate');
const cashbookEndDate = document.getElementById('cashbookEndDate');
const cashbookFetchBtn = document.getElementById('cashbookFetchBtn');
const cashbookStatus = document.getElementById('cashbookStatus');
const cashbookTableWrapper = document.getElementById('cashbookTableWrapper');
const cashbookPageSizeSelect = document.getElementById('cashbookPageSize');
const cashbookPrevPage = document.getElementById('cashbookPrevPage');
const cashbookNextPage = document.getElementById('cashbookNextPage');
const cashbookPageInfo = document.getElementById('cashbookPageInfo');
const cashbookRowCount = document.getElementById('cashbookRowCount');
const startVerificationBtn = document.getElementById('startVerificationBtn');
// Audit Workbook peer nav button on the Cashbook preview screen (mirrors #cashbookBar's own copy
// in the session header) - see navigateToAuditWorkbook() and its enable/disable logic in
// activateCashbookView().
const cashbookAuditWorkbookBtn = document.getElementById('cashbookAuditWorkbookBtn');

const supportingModal = document.getElementById('supportingModal');
const supportingSearchInput = document.getElementById('supportingSearchInput');
const supportingSearchBtn = document.getElementById('supportingSearchBtn');
const supportingResults = document.getElementById('supportingResults');
const supportingModalClose = document.getElementById('supportingModalClose');

// procedureModal/procedureModalTitle/procedureModalClose DOM refs now live in procedure_modal.js.

// ---------- HELPERS ----------
function showStatus(msg, type = 'info') {
    if (!statusDiv) return;
    statusDiv.textContent = msg;
    statusDiv.className = 'upload-status ' + type;
    statusDiv.style.display = 'block';
    clearTimeout(window.statusTimeout);
    window.statusTimeout = setTimeout(() => {
        if (statusDiv) statusDiv.style.display = 'none';
    }, 5000);
}

// voucherVerifyToastTimeout/showVerifyToast() now live in procedure_modal.js.

function showPopupError(msg) {
    if (!popupError) return;
    popupError.textContent = msg;
    popupError.style.display = 'block';
}

function hidePopupError() {
    if (popupError) popupError.style.display = 'none';
}

// Client selection is session-backed (client_context.js reads GET /api/auth/current-client into its
// own cache). The old localStorage 'selectedClientId' fallback was removed: nothing writes that key
// any more, so it could only ever return null or a stale pre-migration value.
function getClientId() {
    if (typeof window.getSelectedClientId === 'function') {
        return window.getSelectedClientId();
    }
    return null;
}

function formatDateInput(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
}

// ---------- REUSABLE SINGLE-CALENDAR DATE RANGE PICKER ----------
// initDateRangePicker(container, onChange) builds a self-contained date-range picker inside
// `container` (cleared first) and returns { setRange(startStr, endStr), getRange() }. One
// reusable component, mounted separately wherever Verification needs a date range (the
// open-cashbook dialog this phase; the in-session filter bar in a later phase) so both stay
// visually/behaviorally identical.
//
// Interaction: first click on a day sets the start date; hovering (while a start is set and no
// end yet) previews the in-between range without committing; a second click sets the end date
// (swapping if it precedes the start — a single-day range, start === end, is valid) and fires
// onChange(startStr, endStr), both 'YYYY-MM-DD'; a third click starts a brand-new range. Dates
// outside the visible month are not shown/selectable — the user navigates months via the
// prev/next arrows (confirmed out of scope for a wider net).
//
// Styling this phase is inline/functional only (positioning, grid, existing app color tokens) —
// matching the app's visual language is a later phase's job, done in static/style.css.
function initDateRangePicker(container, onChange) {
    if (!container) return null;
    container.innerHTML = '';
    container.classList.add('drp-container');

    let visibleMonth = new Date(); // only getFullYear()/getMonth() are read off this
    let rangeStart = null; // Date or null
    let rangeEnd = null;   // Date or null
    let hoverDate = null;  // Date or null — only meaningful while rangeStart set & rangeEnd null
    let isOpen = false;
    // {date, el} pairs for the currently-rendered month's real day cells (never the blank leading
    // cells) — repopulated on every renderCalendar() rebuild, read by updateDayHighlights() so a
    // hover/click state change can restyle the existing cells without recreating them (see the
    // confirmed-fix note on renderCalendar() below).
    let dayCellElements = [];

    function parseYmd(s) {
        if (!s) return null;
        const parts = s.split('-').map(Number);
        return new Date(parts[0], parts[1] - 1, parts[2]);
    }
    function fmt(d) {
        return formatDateInput(d);
    }
    function sameDay(a, b) {
        return !!a && !!b && a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
    }

    const trigger = document.createElement('div');
    trigger.className = 'drp-trigger';
    const calendar = document.createElement('div');
    calendar.className = 'drp-calendar';
    container.appendChild(trigger);
    container.appendChild(calendar);

    function renderTrigger() {
        if (rangeStart && rangeEnd) {
            trigger.textContent = `📅 ${fmt(rangeStart)}  →  ${fmt(rangeEnd)}`;
        } else if (rangeStart) {
            trigger.textContent = `📅 ${fmt(rangeStart)}  →  ...`;
        } else {
            trigger.textContent = '📅 Select date range';
        }
    }

    function inPreviewRange(day) {
        if (!rangeStart || rangeEnd) return false;
        const hover = hoverDate || rangeStart;
        const lo = hover < rangeStart ? hover : rangeStart;
        const hi = hover < rangeStart ? rangeStart : hover;
        return day >= lo && day <= hi;
    }
    function inCommittedRange(day) {
        if (!rangeStart || !rangeEnd) return false;
        return day >= rangeStart && day <= rangeEnd;
    }

    // Confirmed fix: restyles the already-rendered day cells in place (classList only, no
    // innerHTML/createElement) instead of the full renderCalendar() rebuild every hover/click
    // used to trigger. That rebuild was destroying and recreating the very cell the user was
    // mid-hover/mid-click on, which is what made the second click of a range (and a click right
    // after navigating to a different month, since month-nav also rebuilds) unreliable. Only
    // genuine structural changes (which dates exist at all) still go through renderCalendar().
    function updateDayHighlights() {
        dayCellElements.forEach(function(entry) {
            const cell = entry.el;
            const day = entry.date;
            cell.classList.remove('drp-day-endpoint', 'drp-day-in-range');
            if (sameDay(day, rangeStart) || sameDay(day, rangeEnd)) {
                cell.classList.add('drp-day-endpoint');
            } else if (inCommittedRange(day) || inPreviewRange(day)) {
                cell.classList.add('drp-day-in-range');
            }
        });
    }

    // Confirmed fix (viewport clipping): flips the dropdown to open upward when there isn't
    // enough room below the trigger — measured fresh against the current window size, same
    // "measure the anchor against window.innerWidth/innerHeight at open time" approach this app's
    // own .nav-menu-panel (positionNavPanel() in client_context.js) already uses. Called from the
    // end of renderCalendar() (guarded on isOpen) rather than only once at open, since a month
    // navigation can change the grid from 4 rows to 6 rows and change whether it still fits below.
    function positionCalendar() {
        const anchorRect = container.getBoundingClientRect();
        const calendarHeight = calendar.offsetHeight;
        const margin = 4; // matches .drp-calendar's own margin-top/-bottom
        const spaceBelow = window.innerHeight - anchorRect.bottom;
        const spaceAbove = anchorRect.top;
        const flipUp = spaceBelow < (calendarHeight + margin) && spaceAbove > spaceBelow;
        calendar.classList.toggle('drp-calendar-flip-up', flipUp);
    }

    function renderCalendar() {
        calendar.innerHTML = '';
        dayCellElements = [];

        const header = document.createElement('div');
        header.className = 'drp-header';
        const prevBtn = document.createElement('button');
        prevBtn.type = 'button';
        prevBtn.textContent = '‹';
        prevBtn.className = 'drp-nav-btn';
        prevBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            visibleMonth = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() - 1, 1);
            renderCalendar();
        });
        const nextBtn = document.createElement('button');
        nextBtn.type = 'button';
        nextBtn.textContent = '›';
        nextBtn.className = 'drp-nav-btn';
        nextBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            visibleMonth = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() + 1, 1);
            renderCalendar();
        });
        const monthLabel = document.createElement('span');
        monthLabel.className = 'drp-month-label';
        monthLabel.textContent = visibleMonth.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
        header.appendChild(prevBtn);
        header.appendChild(monthLabel);
        header.appendChild(nextBtn);
        calendar.appendChild(header);

        const weekdays = document.createElement('div');
        weekdays.className = 'drp-weekdays';
        ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'].forEach(function(w) {
            const cell = document.createElement('span');
            cell.textContent = w;
            weekdays.appendChild(cell);
        });
        calendar.appendChild(weekdays);

        const daysGrid = document.createElement('div');
        daysGrid.className = 'drp-days';
        daysGrid.addEventListener('mouseleave', function() {
            if (rangeStart && !rangeEnd && hoverDate) {
                hoverDate = null;
                updateDayHighlights();
            }
        });
        const year = visibleMonth.getFullYear();
        const month = visibleMonth.getMonth();
        const firstWeekday = new Date(year, month, 1).getDay();
        const daysInMonth = new Date(year, month + 1, 0).getDate();

        for (let i = 0; i < firstWeekday; i++) {
            const blank = document.createElement('span');
            daysGrid.appendChild(blank);
        }
        for (let d = 1; d <= daysInMonth; d++) {
            const dayDate = new Date(year, month, d);
            const cell = document.createElement('span');
            cell.textContent = String(d);
            cell.className = 'drp-day';
            dayCellElements.push({ date: dayDate, el: cell });
            if (sameDay(dayDate, rangeStart) || sameDay(dayDate, rangeEnd)) {
                cell.classList.add('drp-day-endpoint');
            } else if (inCommittedRange(dayDate) || inPreviewRange(dayDate)) {
                cell.classList.add('drp-day-in-range');
            }
            cell.addEventListener('click', function(e) {
                e.stopPropagation();
                handleDayClick(dayDate);
            });
            cell.addEventListener('mouseenter', function() {
                if (rangeStart && !rangeEnd) {
                    hoverDate = dayDate;
                    updateDayHighlights();
                }
            });
            daysGrid.appendChild(cell);
        }
        calendar.appendChild(daysGrid);
        updateDayHighlights();
        if (isOpen) positionCalendar();
    }

    function handleDayClick(day) {
        if (!rangeStart || rangeEnd) {
            rangeStart = day;
            rangeEnd = null;
            hoverDate = null;
            renderTrigger();
            updateDayHighlights();
        } else {
            if (day < rangeStart) {
                rangeEnd = rangeStart;
                rangeStart = day;
            } else {
                rangeEnd = day;
            }
            hoverDate = null;
            renderTrigger();
            updateDayHighlights();
            const committedStart = rangeStart;
            const committedEnd = rangeEnd;
            closeCalendar();
            if (typeof onChange === 'function') onChange(fmt(committedStart), fmt(committedEnd));
        }
    }

    function openCalendar() {
        isOpen = true;
        const anchor = rangeStart || new Date();
        visibleMonth = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
        calendar.style.display = 'block';
        renderCalendar();
    }
    function closeCalendar() {
        isOpen = false;
        calendar.style.display = 'none';
    }
    trigger.addEventListener('click', function(e) {
        e.stopPropagation();
        if (isOpen) closeCalendar(); else openCalendar();
    });
    document.addEventListener('click', function(e) {
        if (isOpen && !container.contains(e.target)) closeCalendar();
    });

    renderTrigger();

    return {
        setRange: function(startStr, endStr) {
            rangeStart = parseYmd(startStr);
            rangeEnd = parseYmd(endStr);
            hoverDate = null;
            renderTrigger();
            if (isOpen) {
                visibleMonth = new Date((rangeStart || new Date()).getFullYear(), (rangeStart || new Date()).getMonth(), 1);
                renderCalendar();
            }
        },
        getRange: function() {
            return { start: rangeStart ? fmt(rangeStart) : null, end: rangeEnd ? fmt(rangeEnd) : null };
        }
    };
}

// ---------- POPUP: MOUNT DATE RANGE PICKER ----------
// Replaces the two-input Start/End Date fields (#popupPeriodStart/#popupPeriodEnd) with the
// reusable initDateRangePicker() component, mounted in place inside their shared wrapper -
// runtime DOM surgery rather than an HTML edit, since this phase is scoped to verification.js
// only. The two original <input type="date"> elements end up detached (never re-attached, never
// read again); popupPeriodStart/popupPeriodEnd stay only as the (now-inert) means of locating
// that wrapper before it's cleared.
function mountPopupDateRangePicker() {
    if (!popupPeriodStart) return null;
    const wrapper = popupPeriodStart.closest('.field');
    if (!wrapper) return null;
    wrapper.removeAttribute('style'); // was "display:flex; gap:10px" for the old two-input layout
    wrapper.innerHTML = '';
    const label = document.createElement('label');
    label.textContent = 'Date Range';
    const mount = document.createElement('div');
    wrapper.appendChild(label);
    wrapper.appendChild(mount);
    return initDateRangePicker(mount, handlePopupPeriodChange);
}

// ---------- CASHBOOK FILTER BAR: MOUNT DATE RANGE PICKER ----------
// Replaces the two-input Start/End Date fields (#cashbookStartDate/#cashbookEndDate) with the
// same reusable initDateRangePicker() component, in place of the "Start Date" wrapper div - this
// bar has no shared wrapper around just the two date fields (unlike #sessionPopup's .field), so
// the now-redundant "End Date" wrapper div is removed from the DOM entirely rather than reused.
// Runtime DOM surgery, not an HTML edit, same as mountPopupDateRangePicker() above.
// cashbookStartDate/cashbookEndDate stay only as the (now-inert) means of locating these wrapper
// divs before they're cleared/removed.
function mountCashbookDateRangePicker() {
    if (!cashbookStartDate || !cashbookEndDate) return null;
    const startWrapper = cashbookStartDate.parentElement;
    const endWrapper = cashbookEndDate.parentElement;
    if (!startWrapper || !endWrapper) return null;
    startWrapper.innerHTML = '';
    const label = document.createElement('label');
    label.className = 'drp-field-label';
    label.textContent = 'Date Range';
    const mount = document.createElement('div');
    // Session date-range reconciliation, Phase 2 of 4: supersedes the 2026-08-11 "no effect on an
    // active session" tooltip - picking a new range here and clicking Apply now reconciles an
    // active session's voucher list against it (see reconcileActiveSessionRange()). Picking a
    // range alone (this picker's onChange, handleCashbookDateRangeChange() below) still has no
    // effect by itself - Apply is what commits it, same as the non-session preview case.
    mount.title = 'Click Apply to update the voucher list to this date range.';
    startWrapper.appendChild(label);
    startWrapper.appendChild(mount);
    if (endWrapper.parentElement) endWrapper.parentElement.removeChild(endWrapper);
    return initDateRangePicker(mount, handleCashbookDateRangeChange);
}

// ---------- HARD-REFRESH VIEW PERSISTENCE ----------
// Reflects the current view + its identifying id in the URL (state=<view>&id=<id>&type=<currentType>)
// so a hard refresh can restore the right view (see window.onload below) instead of always dropping
// back to the dashboard. Uses history.replaceState, not pushState — this page already manages its
// own Up/Back behavior via setUpOverride()/pushInPageNavEntry()/the global navbar icons, not the
// browser's native back button, so no new history entries should be introduced here. Distinct from the pre-existing
// entry-only ?type=/?view= params (which only mean "open the Start-Verification popup" and are read
// once by window.onload) — this always deletes ?view=, since it's served its purpose once a real
// view is reached.
function setVerificationUrl(view, id) {
    const url = new URL(window.location.href);
    url.searchParams.delete('view');
    // A fresh view transition (dashboard/cashbook/a newly-entered session) should never carry
    // over a stale voucher id left from a previous session — the session case re-populates this
    // immediately afterward via setVerificationUrlVoucher() (see loadNextTransaction()).
    url.searchParams.delete('voucher');
    if (view === 'dashboard') {
        url.searchParams.delete('state');
        url.searchParams.delete('id');
        url.searchParams.delete('type');
    } else {
        url.searchParams.set('state', view);
        url.searchParams.set('id', String(id));
        url.searchParams.set('type', currentType);
    }
    history.replaceState(null, '', url);
}

// Keeps the URL's `voucher` param in sync with whichever voucher is currently on screen, so a
// hard refresh mid-session can restore to that exact voucher (see activateSessionView() below)
// instead of always defaulting to the session's earliest-Pending voucher. A dedicated function
// rather than folded into setVerificationUrl() above, since this fires on every voucher-to-voucher
// move (Prev/Next, the search popover, auto-advance after completing a voucher) and shouldn't
// re-touch state/id/type each time. No-op outside an active session, where a voucher id has no
// meaning.
function setVerificationUrlVoucher(voucherId) {
    const url = new URL(window.location.href);
    if (url.searchParams.get('state') !== 'session') return;
    if (voucherId) {
        url.searchParams.set('voucher', String(voucherId));
    } else {
        url.searchParams.delete('voucher');
    }
    history.replaceState(null, '', url);
}

// Shows the dashboard/hub view with a clear inline message instead of a broken screen — used when
// a URL-restored view (session/cashbook) can't actually be resumed (e.g. the session was completed
// or deleted elsewhere since the URL was last saved).
function showDashboardWithMessage(message) {
    if (sessionContent) sessionContent.style.display = 'none';
    if (cashbookContent) cashbookContent.style.display = 'none';
    if (dashboardContent) dashboardContent.style.display = 'block';
    const container = document.getElementById('verificationContainer');
    if (container) container.classList.remove('verification-active');
    setVerificationUrl('dashboard');
    const msgEl = document.getElementById('dashboardFallbackMessage');
    if (msgEl) {
        msgEl.textContent = '⚠️ ' + message;
        msgEl.style.display = 'block';
    }
}

// Returns the {start, end} (YYYY-MM-DD) of the calendar month containing the given Date.
function monthBoundsAround(date) {
    const firstDay = new Date(date.getFullYear(), date.getMonth(), 1);
    const lastDay = new Date(date.getFullYear(), date.getMonth() + 1, 0);
    return { start: formatDateInput(firstDay), end: formatDateInput(lastDay) };
}

// ---------- DATE RANGE: SESSIONSTORAGE PERSISTENCE (confirmed fix) ----------
// Place 1's popup (openSessionPopup()) only ever opens via a fresh page load, so there is no
// in-memory picker state left to capture the way returnToCashbookFromSession() does for Place 2 - the user's last
// committed range has to survive across navigation/reload within the tab instead. Tab-scoped only
// (sessionStorage, not localStorage) per the confirmed requirement - cleared on tab/app close, not
// persisted across app restarts. Mirrors static/upload.js's UPLOAD_SESSION_STORAGE_KEY/
// saveUploadSessionState()/restoreUploadSessionState() error-handling shape (try/catch, warn-only,
// clear-on-parse-error) - distinct key, no collision with that page's own 'uploadPageState' or
// client_context.js's own STORAGE_KEY (selected client id).
const VERIFICATION_DATE_RANGE_STORAGE_KEY = 'verificationDateRange';

function saveVerificationDateRange(start, end) {
    try {
        sessionStorage.setItem(VERIFICATION_DATE_RANGE_STORAGE_KEY, JSON.stringify({ start, end }));
    } catch (error) {
        console.warn('⚠️ Could not save verification date range:', error);
    }
}

// Returns {start, end} if a valid, previously-committed range is on record, else null (covers
// first-ever load with nothing saved yet, a corrupted/tampered value, or a malformed range) - the
// "genuinely empty" case callers fall back to today's auto-detect for.
function getPersistedVerificationDateRange() {
    let raw;
    try {
        raw = sessionStorage.getItem(VERIFICATION_DATE_RANGE_STORAGE_KEY);
    } catch (error) {
        console.warn('⚠️ Could not read verification date range:', error);
        return null;
    }
    if (!raw) return null;
    let parsed;
    try {
        parsed = JSON.parse(raw);
    } catch (error) {
        console.warn('⚠️ Could not parse verification date range:', error);
        sessionStorage.removeItem(VERIFICATION_DATE_RANGE_STORAGE_KEY);
        return null;
    }
    if (!parsed || !parsed.start || !parsed.end || parsed.start > parsed.end) return null;
    return { start: parsed.start, end: parsed.end };
}

// ---------- CASHBOOK SELECTION: SESSIONSTORAGE PERSISTENCE (confirmed fix) ----------
// Same gap as the date range above, for the selected cashbook: openSessionPopup() only ever
// fires from a fresh page load, so loadCashbooks()'s own in-memory previousSelection capture
// (popupCashbookSelect.value, read at the top of that function) is always empty by then - the
// last-picked cashbook has to survive across reload the same way the date range now does.
const VERIFICATION_SELECTED_CASHBOOK_STORAGE_KEY = 'verificationSelectedCashbook';

function saveSelectedCashbook(name) {
    try {
        sessionStorage.setItem(VERIFICATION_SELECTED_CASHBOOK_STORAGE_KEY, name);
    } catch (error) {
        console.warn('⚠️ Could not save selected cashbook:', error);
    }
}

function getPersistedSelectedCashbook() {
    try {
        return sessionStorage.getItem(VERIFICATION_SELECTED_CASHBOOK_STORAGE_KEY) || null;
    } catch (error) {
        console.warn('⚠️ Could not read selected cashbook:', error);
        return null;
    }
}

// ---------- POPUP: LOAD CASHBOOKS ----------
// overrideStart/overrideEnd (confirmed fix): when provided (the user edited the popup's date
// inputs), search that exact range. When omitted, auto-detect the most recent month that
// actually has cashbook-matching data for this client via GET /api/verification/latest_cashbook_date
// (falls back to today's calendar month if the client has no matching data at all) - replaces the
// old hardcoded "always search the current calendar month" default, which failed whenever a
// client's latest data wasn't in the current month.
async function loadCashbooks(overrideStart, overrideEnd, overrideCashbook) {
    // Confirmed fix: the two fields are independent — changing the date range (via
    // handlePopupPeriodChange(), which calls this function on every Place-1 range edit) must not
    // reset an already-made cashbook selection. Captured before anything below rebuilds the
    // <select>, then re-applied after the rebuild if it's still a valid option for the new range.
    // overrideCashbook (confirmed fix): lets a fresh-page-load caller (openSessionPopup()) inject
    // the sessionStorage-persisted selection here too, since popupCashbookSelect.value is always
    // empty at that point - same "override wins, else fall back to live state" shape as
    // overrideStart/overrideEnd above.
    const previousSelection = overrideCashbook || popupCashbookSelect.value;
    const clientId = getClientId();
    if (!clientId) {
        popupCashbookSelect.innerHTML = '<option value="">-- Select client first --</option>';
        popupCashbookSelect.disabled = true;
        popupStartBtn.disabled = true;
        return;
    }
    window.LoadingUI.showFieldLoading(popupCashbookSelect, { prominent: true });
    // Bank Verification (Phase 8): book-type-specific copy, since this same function serves both
    // Cash and Bank discovery now (only the category_group filter/wording differs).
    const bookWord = currentType === 'bank' ? 'bankbook' : 'cashbook';
    const bookWordCap = currentType === 'bank' ? 'Bankbook' : 'Cashbook';
    const voucherHint = currentType === 'bank' ? 'BPV/BRV' : 'CPV/CRV';

    let start = overrideStart;
    let end = overrideEnd;
    if (!start || !end) {
        let anchorDate = new Date();
        try {
            const latestParams = new URLSearchParams({ client_id: clientId, category_group: currentType });
            const latestRes = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/latest_cashbook_date?${latestParams}`);
            if (latestRes.ok) {
                const latestData = await latestRes.json();
                if (latestData.latest_date) {
                    anchorDate = new Date(latestData.latest_date + 'T00:00:00');
                }
            }
        } catch (e) {
            console.error(`Error detecting latest ${bookWord} date:`, e);
            // Fall through to today's month - same behavior as before this fix.
        }
        const bounds = monthBoundsAround(anchorDate);
        start = bounds.start;
        end = bounds.end;
    }
    if (popupDateRangePicker) popupDateRangePicker.setRange(start, end);

    try {
        const params = new URLSearchParams({
            client_id: clientId,
            date_start: start,
            date_end: end,
            category_group: currentType
        });
        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/cashbooks?${params}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || `Failed to fetch ${bookWord}s`);
        }
        const data = await res.json();
        window.LoadingUI.hideFieldLoading(popupCashbookSelect);
        if (data.cashbooks && data.cashbooks.length > 0) {
            popupCashbookSelect.innerHTML = `<option value="">-- Select ${bookWordCap} --</option>`;
            data.cashbooks.forEach(cb => {
                const opt = document.createElement('option');
                opt.value = cb;
                opt.textContent = cb;
                popupCashbookSelect.appendChild(opt);
            });
            // Re-apply the previous selection if it's still valid for this range — if it's
            // genuinely not present anymore, the placeholder stays selected, same as before.
            if (previousSelection && data.cashbooks.includes(previousSelection)) {
                popupCashbookSelect.value = previousSelection;
            }
            popupCashbookSelect.disabled = false;
            popupStartBtn.disabled = false;
        } else {
            popupCashbookSelect.innerHTML = `<option value="">⚠️ No ${bookWord}s found</option>`;
            popupCashbookSelect.disabled = true;
            popupStartBtn.disabled = true;
            showPopupError(
                `No ${bookWord}s found for this client.\n` +
                `Make sure you have uploaded data containing ${voucherHint} vouchers.\n` +
                'The column names must be exactly: "Voucher ID", "Account Name", "Debit", "Credit".'
            );
        }
    } catch (e) {
        console.error(`Error loading ${bookWord}s:`, e);
        window.LoadingUI.hideFieldLoading(popupCashbookSelect);
        popupCashbookSelect.innerHTML = '<option value="">⚠️ Error loading</option>';
        popupCashbookSelect.disabled = true;
        showPopupError(`Failed to load ${bookWord}s: ` + e.message);
    }
}

// ---------- POPUP: OPEN CASHBOOK ----------
async function openCashbook() {
    if (!popupCashbookSelect) return;
    const cashbook = popupCashbookSelect.value;
    if (!cashbook) {
        showPopupError('Please select a cashbook.');
        return;
    }
    hidePopupError();
    popupOverlay.style.display = 'none';
    await activateCashbookView(cashbook);
}

// Switches to the cashbook view for the given book name and fetches fresh data — shared by
// openCashbook() (Start-Verification popup flow) and restoreCashbookView() (hard-refresh URL
// restore, below), so both paths land on the exact same view/state instead of two parallel
// implementations that could drift apart.
async function activateCashbookView(cashbook) {
    selectedCashbook = cashbook;
    dashboardContent.style.display = 'none';
    sessionContent.style.display = 'none';
    cashbookContent.style.display = 'block';
    // Session date-range reconciliation, Phase 2 of 4: this view can now be reached either fresh
    // (no session) or mid-session (currentSessionId still set, via returnToCashbookFromSession() -
    // the old exitSession() no longer tears the session down) - always restore Place 2's own
    // padding/layout here rather than relying on the caller to have done it, since a mid-session
    // visit arrives with .verification-active still applied from activateSessionView().
    const verificationContainer = document.getElementById('verificationContainer');
    if (verificationContainer) verificationContainer.classList.remove('verification-active');
    // Global navbar Up icon: the Cashbook view's structural parent is always the module's entry
    // hub, regardless of whether a session is active in the background — resuming that session is
    // now the Back icon's job (chronological), not Up's. See exitCashbookView()'s setUpOverride(null).
    if (typeof setUpOverride === 'function') setUpOverride(exitCashbookView);
    // Bank Verification (Phase 8): relabel the page header to match currentType.
    const headerLabel = document.getElementById('cashbookHeaderLabel');
    if (headerLabel) headerLabel.textContent = currentType === 'bank' ? '🏦 Bankbook:' : '📒 Cashbook:';
    cashbookNameDisplay.textContent = cashbook;
    // Cashbook Preview selector (Phase 1 of 3): relabel to match currentType, and populate the
    // dropdown from the same account list already fetched into popupCashbookSelect for the
    // Start-Verification popup (openCashbook()) or by restoreCashbookView()'s own loadCashbooks()
    // call - both callers guarantee that list is already loaded before this function runs.
    if (cashbookSelectorLabel) cashbookSelectorLabel.textContent = currentType === 'bank' ? '🏦 Bankbook' : '📒 Cashbook';
    // Audit Workbook is session-scoped (navigateToAuditWorkbook() no-ops with no currentSessionId) -
    // this screen can be reached with or without an active session, so reflect that here each time
    // it's (re)entered rather than leaving the button silently inert with no visual feedback.
    if (cashbookAuditWorkbookBtn) {
        cashbookAuditWorkbookBtn.disabled = !currentSessionId;
    }
    if (cashbookSelector) {
        cashbookSelector.innerHTML = '';
        Array.from(popupCashbookSelect.options)
            .filter(opt => opt.value)
            .forEach(opt => {
                const clone = document.createElement('option');
                clone.value = opt.value;
                clone.textContent = opt.textContent;
                cashbookSelector.appendChild(clone);
            });
        cashbookSelector.value = cashbook;
    }
    // Confirmed fix: sessions are flexible - neither the cashbook dropdown nor Start Verification
    // is force-disabled while a session is already active (the 2026-08-11 lock that did this is
    // removed). Picking a different cashbook and clicking Apply still only switches when no
    // session is active (see applyCashbookPreview()'s own guard, unchanged); clicking Start
    // Verification mid-session simply starts a fresh session via startSessionFromCashbook(), which
    // already works standalone regardless of currentSessionId.
    // Confirmed fix: seed Place 2's picker from Place 1's own final range (whatever the user last
    // picked/left in the dialog, or its own auto-detected default) instead of always hardcoding
    // "today's calendar month" — the previous hardcode is what silently discarded a Place-1
    // selection the instant the dialog closed, regardless of any live mirroring. Falls back to
    // today's month only if popupDateRangePicker somehow has no range yet (defensive - both
    // callers of this function already run loadCashbooks(), which seeds it, before reaching here).
    const popupRange = popupDateRangePicker ? popupDateRangePicker.getRange() : { start: null, end: null };
    let cashbookViewStart = popupRange.start;
    let cashbookViewEnd = popupRange.end;
    if (!cashbookViewStart || !cashbookViewEnd) {
        const now = new Date();
        const firstDay = new Date(now.getFullYear(), now.getMonth(), 1);
        const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0);
        cashbookViewStart = formatDateInput(firstDay);
        cashbookViewEnd = formatDateInput(lastDay);
    }
    if (cashbookDateRangePicker) cashbookDateRangePicker.setRange(cashbookViewStart, cashbookViewEnd);
    setVerificationUrl('cashbook', cashbook);
    // Global navbar Back icon: records this as a chronological history entry — covers both a
    // fresh entry (openCashbook()) and a hard-refresh restore (restoreCashbookView()), the latter
    // a no-op via pushInPageNavEntry()'s own dedup since the page's own load-time push already
    // recorded this exact URL.
    if (typeof window.pushInPageNavEntry === 'function') window.pushInPageNavEntry(null, 'Cashbook Preview');
    await fetchCashbookData();
}

// ---------- HARD-REFRESH VIEW PERSISTENCE: CASHBOOK VIEW ----------
// Restores the cashbook (pre-session) view after a hard refresh from ?state=cashbook&id=<name> -
// re-fetches the cashbook list and this cashbook's transactions fresh (via loadCashbooks() and
// activateCashbookView()'s own fetchCashbookData() call), never a cached/frozen snapshot. A
// cashbook name that no longer matches any data isn't treated as a hard failure - fetchCashbookData()
// already renders a graceful "No transactions found" state for that case.
// overrideStart/overrideEnd (confirmed fix): when provided, forwarded straight into
// loadCashbooks() so its own auto-detect logic is bypassed - lets a caller that already has a
// live/meaningful date range (returnToCashbookFromSession(), returning from an active session) preserve it
// instead of losing it to the auto-detected default. Omitted by the hard-refresh restore caller
// (window.onload's ?state=cashbook handling, below), where no prior in-memory range exists and
// auto-detect is exactly the desired behavior - that path is unchanged.
async function restoreCashbookView(cashbookName, overrideStart, overrideEnd) {
    if (!getClientId()) {
        showDashboardWithMessage('Please select a client to view this cashbook.');
        return;
    }
    await loadCashbooks(overrideStart, overrideEnd);
    await activateCashbookView(cashbookName);
}

// ---------- EXIT CASHBOOK VIEW (return to Verification module's entry view) ----------
// Shared by the global navbar back-arrow override (set in openCashbook()/returnToCashbookFromSession()) — there
// is no separate in-body "Back to Verification" link/handler anymore, this is the single place
// that logic lives.
function exitCashbookView() {
    cashbookContent.style.display = 'none';
    dashboardContent.style.display = 'block';
    selectedCashbook = null;
    cashbookTableWrapper.innerHTML = '';
    setVerificationUrl('dashboard');
    // Now at the module's entry hub — Up falls back to the static default (getUpTarget()).
    if (typeof setUpOverride === 'function') setUpOverride(null);
}

// ---------- CASHBOOK PREVIEW SELECTOR: APPLY ----------
// Phase 2 of 3: the Cashbook/Bankbook dropdown (Phase 1) does nothing by itself - only
// clicking Apply commits its currently selected value together with the current date range,
// in one combined reload (mirroring how Apply already behaves for date-only changes). Updates
// the single-source-of-truth `selectedCashbook` global (also read by startSessionFromCashbook(),
// so starting a session after switching here correctly targets the newly selected account) and
// the page header, then reuses fetchCashbookData() unchanged for the actual reload.
// Session date-range reconciliation, Phase 2 of 4: when reached with an active session
// (currentSessionId set - only possible now that returnToCashbookFromSession() keeps one alive),
// Apply instead reconciles that session's voucher list against the new range, staying on Place 2 -
// reconcileActiveSessionRange() itself now also refreshes Place 2's own visible preview table
// (confirmed fix), so this no longer looks unresponsive from Place 2's perspective. The
// cashbook-switch block below is still skipped in that case - reconcile is keyed to the session's
// own fixed cashbook, switching which cashbook a session targets isn't supported mid-session (the
// dropdown itself is no longer force-disabled while a session is active - confirmed fix - but
// picking a different value here still has no effect until a new session is started for it).
async function applyCashbookPreview() {
    if (!cashbookSelector) {
        if (currentSessionId) {
            await reconcileActiveSessionRange();
        } else {
            fetchCashbookData();
        }
        return;
    }
    const chosen = cashbookSelector.value;
    if (!currentSessionId && chosen && chosen !== selectedCashbook) {
        selectedCashbook = chosen;
        cashbookNameDisplay.textContent = chosen;
        // Confirmed fix: mirrors handleCashbookDateRangeChange()'s existing Place 2 -> Place 1
        // persistence for dates, so a cashbook switched here also survives a reload back to Place 1.
        saveSelectedCashbook(chosen);
    }
    if (currentSessionId) {
        await reconcileActiveSessionRange();
    } else {
        fetchCashbookData();
    }
}

// Orphaned-voucher auto-navigation: given a reference date, finds the closest voucher (by
// absolute date distance, ties broken toward the later one) among sessionTransactions' distinct
// voucher_ids - called against the already-reconciled list, so "nearest" here always means
// "nearest still-active voucher". Returns null only if sessionTransactions is empty.
function findNearestInRangeVoucherId(targetDate) {
    const seen = new Set();
    const uniqueVouchers = [];
    for (const t of sessionTransactions) {
        if (t.voucher_id && !seen.has(t.voucher_id)) {
            seen.add(t.voucher_id);
            uniqueVouchers.push({ voucher_id: t.voucher_id, extract_date: t.extract_date });
        }
    }
    if (!uniqueVouchers.length) return null;
    if (!targetDate) return uniqueVouchers[0].voucher_id;
    let best = uniqueVouchers[0];
    let bestDiff = Infinity;
    const targetTime = new Date(targetDate).getTime();
    for (const v of uniqueVouchers) {
        const diff = Math.abs(new Date(v.extract_date).getTime() - targetTime);
        if (diff < bestDiff || (diff === bestDiff && v.extract_date > best.extract_date)) {
            bestDiff = diff;
            best = v;
        }
    }
    return best.voucher_id;
}

// ---------- RECONCILE AN ACTIVE SESSION AGAINST A NEW DATE RANGE ----------
// Session date-range reconciliation, Phase 2 of 4: calls Phase 1's backend reconcile endpoint.
// Newly-in-range/reactivated/excluded lines are all handled server-side (see
// reconcile_session_range() in routers/verification.py) - this is call-and-refresh only, no
// client-side range-status logic.
// Confirmed fix (regressions found right after Phase 2 landed): this deliberately never switches
// which view is visible - Apply only refreshes data and stays on Place 2.
// Orphaned-voucher auto-navigation: navigateToTransaction() (called below) only updates in-memory
// state and re-renders #voucherHeaderBar/#voucherLinesContainer - it never toggles which view is
// visible, unlike returnToSessionFromCashbook() (which this function still never calls). So
// calling it here keeps Place 3's hidden DOM correct and, when the auditor's voucher fell out of
// range, already showing the "moved" caption - all without ever making Place 3 visible.
// Confirmed fix: also refreshes Place 2's own visible preview table (fetchCashbookData()) - the
// original reconcile-only version updated sessionTransactions (Place 3's data, hidden behind this
// view) but never touched the on-screen cashbook table the auditor is actually looking at, so
// Apply looked completely unresponsive from Place 2 even though the reconcile itself worked.
// fetchCashbookData() sets its own cashbookStatus text (row count / error) on completion, same as
// it always has outside a session - that's the visible confirmation Apply was missing, not a
// reintroduced banner/toast (the earlier "stay silent" fix was about not navigating away, not
// about suppressing this table's own always-present status line).
async function reconcileActiveSessionRange() {
    if (!currentSessionId) return;
    const range = cashbookDateRangePicker ? cashbookDateRangePicker.getRange() : { start: null, end: null };
    const start = range.start;
    const end = range.end;
    if (!start || !end) {
        cashbookStatus.textContent = '⚠️ Please select date range';
        return;
    }
    if (start > end) {
        cashbookStatus.textContent = '⚠️ Start must be ≤ end';
        return;
    }
    cashbookStatus.textContent = '⏳ Applying new range...';
    cashbookFetchBtn.disabled = true;
    try {
        const params = new URLSearchParams({ period_start: start, period_end: end });
        const res = await fetch(`${API_BASE}/api/verification/session/${currentSessionId}/reconcile_range?${params}`, {
            method: 'POST'
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to apply new range');
        }
        // Capture the current voucher's identity/date before sessionTransactions is overwritten -
        // needed as the "target date" for findNearestInRangeVoucherId() if it turns out to have
        // fallen out of range.
        const previousVoucherId = currentVoucherId;
        const previousLine = sessionTransactions.find(t => t.detail_id === currentDetailId);
        const previousVoucherDate = previousLine ? previousLine.extract_date : null;

        // The reconcile endpoint's response isn't needed here - force-refresh sessionTransactions
        // fresh instead, since it's already populated from before the reconcile and
        // loadTransactionList()'s normal cache guard would otherwise skip re-fetching.
        await loadTransactionList(false, true);
        // Confirmed fix: also refresh Place 2's own visible preview table (see the comment above
        // this function) - independent of the session-nav branching below, since this table isn't
        // tied to session state at all, only to selectedCashbook/the current date range.
        await fetchCashbookData();

        const stillInRange = previousVoucherId && sessionTransactions.some(t => t.voucher_id === previousVoucherId);
        if (stillInRange) {
            // Unaffected - resume exactly where the auditor was, no caption.
            if (sessionTransactions.some(t => t.detail_id === currentDetailId)) {
                navigateToTransaction(currentDetailId);
            } else {
                // Rare: same voucher_id survived but this specific line didn't - land on
                // whichever of that voucher's lines remains.
                const firstRemainingLine = sessionTransactions.find(t => t.voucher_id === previousVoucherId);
                navigateToTransaction(firstRemainingLine.detail_id);
            }
        } else if (sessionTransactions.length > 0) {
            // Orphaned - auto-navigate to the nearest remaining in-range voucher and caption it.
            const nearestVoucherId = findNearestInRangeVoucherId(previousVoucherDate);
            const nearestLine = sessionTransactions.find(t => t.voucher_id === nearestVoucherId);
            if (nearestLine) {
                voucherNavCaptionText = 'Moved from a voucher now outside the selected range';
                navigateToTransaction(nearestLine.detail_id);
            }
        } else {
            // Reconciled to zero active vouchers - nothing to navigate to.
            clearVoucherHeader();
            await updateProgressDisplay();
        }
        // No explicit blank-out here - fetchCashbookData() above already set cashbookStatus to its
        // own final row-count text, which is the visible confirmation this fix restores.
    } catch (error) {
        console.error('Error reconciling session range:', error);
        cashbookStatus.textContent = `⚠️ ${error.message}`;
    } finally {
        cashbookFetchBtn.disabled = false;
    }
}

// ---------- CASHBOOK DATA FETCH ----------
async function fetchCashbookData() {
    if (!selectedCashbook) return;
    const clientId = getClientId();
    if (!clientId) {
        cashbookStatus.textContent = '⚠️ No client selected';
        return;
    }
    const cashbookRange = cashbookDateRangePicker ? cashbookDateRangePicker.getRange() : { start: null, end: null };
    const start = cashbookRange.start;
    const end = cashbookRange.end;
    if (!start || !end) {
        cashbookStatus.textContent = '⚠️ Please select date range';
        return;
    }
    if (start > end) {
        cashbookStatus.textContent = '⚠️ Start must be ≤ end';
        return;
    }
    cashbookStatus.textContent = '⏳ Loading...';
    cashbookFetchBtn.disabled = true;
    window.LoadingUI.showSkeleton(cashbookTableWrapper, 8);
    const params = new URLSearchParams({
        client_id: clientId,
        cashbook_name: selectedCashbook,
        date_start: start,
        date_end: end,
        page: cashbookCurrentPage,
        limit: cashbookPageSize
    });
    try {
        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/preview_cashbook?${params}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch cashbook data');
        }
        const data = await res.json();
        cashbookTotalRows = data.total_rows || 0;
        const rows = data.rows || [];
        renderCashbookTable(rows);
        updateCashbookPagination();
        if (rows.length === 0 && cashbookTotalRows === 0) {
            cashbookTableWrapper.innerHTML = '<div style="text-align:center; color:#94a3b8; padding:20px;">No transactions found for this cashbook and date range.</div>';
            cashbookRowCount.textContent = '0 rows';
        } else {
            cashbookRowCount.textContent = `${cashbookTotalRows} rows`;
        }
        cashbookStatus.textContent = `✅ ${cashbookTotalRows} rows`;
        cashbookStatus.style.color = '#16a34a';
    } catch (error) {
        console.error('Cashbook fetch error:', error);
        cashbookStatus.textContent = '❌ ' + error.message;
        cashbookStatus.style.color = '#dc2626';
        cashbookTableWrapper.innerHTML = `<div style="text-align:center; color:#dc2626; padding:20px;">❌ ${error.message}</div>`;
    } finally {
        cashbookFetchBtn.disabled = false;
    }
}

// Cashbook Status column (Phase 2 of 3) — small inline-styled pill, same palette already
// established for .aw-status-badge/.voucher-line-cleared-badge/.status-badge.pending elsewhere
// in this app (Cleared #dcfce7/#166534, Query #fee2e2/#991b1b, Pending #fef3c7/#92400e), plus a
// new muted gray pair for "Not Started" (a voucher with zero verification activity at all,
// distinct from Pending). Kept as inline styles, matching renderCashbookTable()'s own existing
// 100%-inline-styled convention rather than introducing a new CSS class.
function getCashbookStatusBadgeHtml(status) {
    const palettes = {
        'Cleared': { bg: '#dcfce7', color: '#166534' },
        'Query': { bg: '#fee2e2', color: '#991b1b' },
        'Pending': { bg: '#fef3c7', color: '#92400e' },
        'Not Started': { bg: '#f1f5f9', color: '#64748b' }
    };
    const p = palettes[status] || palettes['Not Started'];
    const label = status || 'Not Started';
    return `<span style="display:inline-block; padding:2px 10px; border-radius:12px; font-weight:600; font-size:11px; white-space:nowrap; background:${p.bg}; color:${p.color};">${label}</span>`;
}

// Cashbook Amount column (Phase 3 of 3, FINAL): collapses Debit/Credit into one signed value —
// right-aligned, 2-decimal formatted, colour-coded so the sign isn't the only signal. Positive
// (CPV/BPV, a Payment) uses the table's own default text colour; negative (CRV/BRV, a Receipt)
// uses a distinct violet accent (#7c3aed, already used elsewhere in this app for .btn-template,
// carries no existing status meaning) — deliberately not red/green, since those already mean
// Query/Cleared on the adjacent Status column (Phase 2) and reusing them here would visually
// collide with that column's own meaning on the same row.
function getCashbookAmountHtml(amount) {
    const value = typeof amount === 'number' ? amount : 0;
    const color = value < 0 ? '#7c3aed' : '#1a1a2e';
    const formatted = value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return `<span style="font-weight:600; color:${color};">${formatted}</span>`;
}

// ---------- RENDER CASHBOOK TABLE ----------
function renderCashbookTable(rows) {
    if (!rows || rows.length === 0) {
        cashbookTableWrapper.innerHTML = '<div style="text-align:center; color:#94a3b8; padding:20px;">No data to display.</div>';
        return;
    }
    const firstRow = rows[0].row_data;
    const originalKeys = firstRow ? Object.keys(firstRow) : [];
    // Debit/Credit columns (Phase 3): collapsed into one "Amount" column, inserted in place of
    // the first Debit/Credit key so it lands in its natural reading position instead of being
    // appended at the end. debit_credit_keys comes from the backend (the literal keys it matched
    // for this row), not a separate frontend alias list, so it can't drift out of sync.
    const dcKeys = new Set(rows[0].debit_credit_keys || []);
    const headers = [];
    let amountInserted = false;
    originalKeys.forEach(k => {
        if (dcKeys.has(k)) {
            if (!amountInserted) {
                headers.push('__AMOUNT__');
                amountInserted = true;
            }
        } else {
            headers.push(k);
        }
    });
    if (!amountInserted) headers.push('__AMOUNT__');

    let html = `<table style="width:100%; border-collapse:collapse; font-size:13px;">
        <thead style="background:#f8fafc; position:sticky; top:0; z-index:1; border-bottom:2px solid #e2e8f0;">
            <tr>`;
    headers.forEach(h => {
        const label = h === '__AMOUNT__' ? 'Amount' : h;
        const align = h === '__AMOUNT__' ? 'right' : 'left';
        html += `<th style="padding:6px 10px; text-align:${align}; font-weight:600; white-space:nowrap;">${label}</th>`;
    });
    html += `<th style="padding:6px 10px; text-align:left; font-weight:600; white-space:nowrap;">Status</th>`;
    html += `</tr></thead><tbody>`;
    rows.forEach(row => {
        html += `<tr style="border-bottom:1px solid #f0f2f5;">`;
        headers.forEach(h => {
            if (h === '__AMOUNT__') {
                html += `<td style="padding:6px 10px; text-align:right;">${getCashbookAmountHtml(row.amount)}</td>`;
            } else {
                const val = row.row_data[h] !== undefined ? row.row_data[h] : '';
                html += `<td style="padding:6px 10px;">${val}</td>`;
            }
        });
        html += `<td style="padding:6px 10px;">${getCashbookStatusBadgeHtml(row.status)}</td>`;
        html += `</tr>`;
    });
    html += `</tbody></table>`;
    cashbookTableWrapper.innerHTML = html;
}

// ---------- CASHBOOK PAGINATION ----------
function updateCashbookPagination() {
    const totalPages = Math.ceil(cashbookTotalRows / cashbookPageSize) || 1;
    cashbookPageInfo.textContent = `Page ${cashbookCurrentPage} of ${totalPages}`;
    cashbookPrevPage.disabled = cashbookCurrentPage <= 1;
    cashbookNextPage.disabled = cashbookCurrentPage >= totalPages;
}

// ---------- START VERIFICATION SESSION ----------
async function startSessionFromCashbook() {
    const clientId = getClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected.', 'error');
        return;
    }
    if (!selectedCashbook) {
        showStatus('⚠️ No cashbook selected.', 'error');
        return;
    }
    const sessionRange = cashbookDateRangePicker ? cashbookDateRangePicker.getRange() : { start: null, end: null };
    const start = sessionRange.start;
    const end = sessionRange.end;
    if (!start || !end) {
        showStatus('⚠️ Please select date range.', 'error');
        return;
    }
    if (start > end) {
        showStatus('⚠️ Start must be ≤ end.', 'error');
        return;
    }
    // No real progress feed from start_session (the backend only returns total_transactions once
    // the whole build is done) - cashbookTotalRows (fetchCashbookData(), already populated since
    // this button lives on the same cashbook view) is used as an upfront estimate, animated toward
    // completion on a timer, same as loading_ui.js's own 'simulated' curve.
    let progressIntervalId = null;
    try {
        const estimatedTotal = cashbookTotalRows || 0;
        if (estimatedTotal > 0) {
            window.LoadingUI.showProgressBar(startVerificationBtn, {
                mode: 'determinate',
                total: estimatedTotal,
                message: 'Loading transactions'
            });
            const startTime = Date.now();
            progressIntervalId = setInterval(function () {
                const elapsed = Date.now() - startTime;
                const current = Math.floor(estimatedTotal * 0.9 * (1 - Math.exp(-elapsed / 2000)));
                window.LoadingUI.updateProgressBar(startVerificationBtn, current, estimatedTotal);
            }, 150);
        } else {
            window.LoadingUI.showProgressBar(startVerificationBtn, { mode: 'indeterminate', message: 'Starting verification…' });
        }
        const params = new URLSearchParams({
            client_id: clientId,
            cashbook_name: selectedCashbook,
            period_start: start,
            period_end: end,
            created_by: 'Analyst',
            // Bank Verification (Phase 8): stores which mode this session was started under
            // (see the session_type column added to verification_sessions in Phase 6).
            session_type: currentType
        });
        const res = await fetch(`${API_BASE}/api/verification/start_session?${params}`, {
            method: 'POST'
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to start session');
        }
            const data = await res.json();
        totalTransactions = data.total_transactions || 0;
        if (progressIntervalId) {
            clearInterval(progressIntervalId);
            progressIntervalId = null;
        }
        window.LoadingUI.updateProgressBar(startVerificationBtn, totalTransactions, totalTransactions);
        await activateSessionView(data.session_id);
    } catch (error) {
        if (progressIntervalId) {
            clearInterval(progressIntervalId);
            progressIntervalId = null;
        }
        console.error('Error starting session:', error);
        showStatus('❌ Error starting verification: ' + error.message, 'error');
    } finally {
        window.LoadingUI.hideProgressBar(startVerificationBtn);
    }
}

// Switches to the session view for the given session id — shared by startSessionFromCashbook()
// (new session, just started) and restoreSessionView() (hard-refresh URL restore, below), so both
// paths land on the exact same view/state instead of two parallel implementations that could
// drift apart.
// targetVoucherId (optional): when set, restore directly to that voucher instead of the default
// earliest-Pending jump — used by restoreSessionView()'s hard-refresh restore (see below).
// rulebookPromise (optional): restoreSessionView() starts this read beside its audited-cashbook
// existence/data read, so hard-refresh restore does not serialize two independent reads. Fresh
// session start omits it and activateSessionView() creates the read here.
async function loadSessionRulebook(sessionId) {
    const rulebookResponse = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/session/${sessionId}/rulebook`);
    if (!rulebookResponse.ok) {
        const payload = await rulebookResponse.json().catch(() => ({}));
        const detail = payload.detail !== undefined
            ? payload.detail
            : (payload.error && payload.error.http_detail);
        if (rulebookResponse.status === 409 && detail && typeof detail === 'object' &&
            detail.code === 'RULEBOOK_VERSION_MISMATCH') {
            const error = new Error(detail.message || 'This Verification session uses an older Rulebook version.');
            error.code = detail.code;
            error.recovery = detail.recovery || null;
            error.changes = Array.isArray(detail.changes) ? detail.changes : [];
            error.sessionVersion = detail.session_version;
            error.currentVersion = detail.current_version;
            throw error;
        }
        const message = typeof detail === 'string'
            ? detail
            : (payload.error && payload.error.user_message) || 'Unable to load the Verification Rulebook';
        throw new Error(message);
    }
    return (await rulebookResponse.json()).rulebook || null;
}

function rulebookChangeLabel(change) {
    const subject = change.entity_name
        ? `${change.entity_type || 'Rulebook item'} “${change.entity_name}”`
        : (change.entity_type || 'Audit Rules configuration');
    return `${change.action || 'Updated'}: ${subject}`;
}

function showRulebookChangeAlert(error) {
    return new Promise(resolve => {
        document.getElementById('rulebookChangeAlert')?.remove();
        const changes = error.changes || [];
        const changeHtml = changes.length
            ? changes.map(change => `<li>${escapeHtml(rulebookChangeLabel(change))}</li>`).join('')
            : '<li>One or more Audit Rules settings changed.</li>';
        document.body.insertAdjacentHTML('beforeend', `
            <div id="rulebookChangeAlert" class="rulebook-change-alert" role="dialog" aria-modal="true" aria-labelledby="rulebookChangeAlertTitle">
                <div class="rulebook-change-alert-card">
                    <div class="rulebook-change-alert-icon" aria-hidden="true">⚠️</div>
                    <h2 id="rulebookChangeAlertTitle">Audit Rules changed</h2>
                    <p>This session was started with an older Rulebook. The following factory setting change may affect verification results:</p>
                    <ul>${changeHtml}</ul>
                    <p class="rulebook-change-alert-versions">Session version ${escapeHtml(String(error.sessionVersion ?? 'unknown'))} → current version ${escapeHtml(String(error.currentVersion ?? 'unknown'))}</p>
                    <p class="rulebook-change-alert-preserved">Your saved verification work will be preserved.</p>
                    <div class="rulebook-change-alert-actions">
                        <button type="button" class="btn-ghost" data-rulebook-alert-action="cancel">Leave session unchanged</button>
                        <button type="button" class="btn-primary" data-rulebook-alert-action="recover">Update session and continue</button>
                    </div>
                </div>
            </div>
        `);
        const alert = document.getElementById('rulebookChangeAlert');
        const finish = recovered => {
            alert?.remove();
            resolve(recovered);
        };
        alert?.querySelector('[data-rulebook-alert-action="cancel"]')?.addEventListener('click', () => finish(false));
        alert?.querySelector('[data-rulebook-alert-action="recover"]')?.addEventListener('click', () => finish(true));
        alert?.querySelector('.rulebook-change-alert-card')?.focus?.();
    });
}

// A Rulebook mismatch is safe to recover only through an explicit user action. The backend
// changes the session version only; all existing verification work remains in place.
async function loadSessionRulebookWithRecovery(sessionId) {
    try {
        return await loadSessionRulebook(sessionId);
    } catch (error) {
        if (error.code !== 'RULEBOOK_VERSION_MISMATCH' || !error.recovery) throw error;
        const confirmed = await showRulebookChangeAlert(error);
        if (!confirmed) {
            error.recoveryDeclined = true;
            throw error;
        }
        const recoveryResponse = await window.ApiUtils.apiFetchMutate(error.recovery.path, {
            method: error.recovery.method || 'POST'
        });
        const recoveryPayload = await recoveryResponse.json().catch(() => ({}));
        if (!recoveryResponse.ok) {
            const detail = recoveryPayload.detail ||
                (recoveryPayload.error && recoveryPayload.error.http_detail) ||
                (recoveryPayload.error && recoveryPayload.error.user_message) ||
                'Unable to recover this Verification session.';
            throw new Error(typeof detail === 'string' ? detail : 'Unable to recover this Verification session.');
        }
        return await loadSessionRulebook(sessionId);
    }
}

// startSessionFromCashbook() (a freshly-started session) omits rulebookPromise, so its
// behavior remains the same apart from overlapping the independent audited-cashbook read with
// the mandatory Rulebook read.
async function activateSessionView(sessionId, targetVoucherId = null, rulebookPromise = null) {
    currentSessionId = sessionId;
    sessionRulebook = null;
    // Both reads are independent. Start the transaction-list request immediately, but still
    // await and validate the Rulebook before any voucher/category/procedure data is rendered.
    const sessionTransactionsPromise = loadTransactionList(false);
    sessionRulebook = await (rulebookPromise || loadSessionRulebookWithRecovery(sessionId));
    startVerificationPresence(sessionId);
    // Header voucher-wise stats (Phase 2 of 3): kick off the session-load fetch in parallel with
    // the render path below — not awaited, so it never blocks getting the first voucher on screen.
    // Stats are refreshed by updateProgressDisplay() after the first transaction render.
    // Calling refreshVoucherStats() here as well duplicated the request during activation.
    // Explicitly hide the dashboard/module-selection view here too (not just cashbookContent) -
    // matches activateCashbookView()'s own hide of dashboardContent. Previously this only ever ran
    // as the tail of startSessionFromCashbook(), which is always reached after
    // activateCashbookView() had already hidden the dashboard, so the omission was invisible until
    // restoreSessionView() (hard-refresh straight into a session) called this function directly,
    // leaving the dashboard's window.onload default of display:'block' stacked underneath.
    dashboardContent.style.display = 'none';
    cashbookContent.style.display = 'none';
    sessionContent.style.display = 'block';
    // Global navbar Up icon should return to the Cashbook selection screen (its structural
    // parent) — reuse the same flow as the "Cashbook" button.
    if (typeof setUpOverride === 'function') setUpOverride(returnToCashbookFromSession);
    // Remove padding from main container when verification session is active
    const container = document.getElementById('verificationContainer');
    if (container) container.classList.add('verification-active');
    setVerificationUrl('session', sessionId);
    // Global navbar Back icon: records this as a chronological history entry — covers both a
    // fresh entry (startSessionFromCashbook()) and a hard-refresh restore (restoreSessionView()),
    // the latter a no-op via pushInPageNavEntry()'s own dedup.
    if (typeof window.pushInPageNavEntry === 'function') window.pushInPageNavEntry(null, 'Verification Session');
    // Load transaction list (without showing popover) to get indices and total. The request was
    // started above beside the Rulebook read; awaiting it here preserves the existing render order.
    await sessionTransactionsPromise;
    // Restore to the specific voucher the auditor was on (targetVoucherId), reusing
    // navigateToTransaction()'s existing synchronous render from the just-loaded
    // sessionTransactions — falls back to the default earliest-Pending jump if no target was
    // given, or if that voucher is no longer present in this session (e.g. session data changed).
    let restoredToTarget = false;
    if (targetVoucherId) {
        const targetLine = sessionTransactions.find(t => t.voucher_id === targetVoucherId);
        if (targetLine) {
            navigateToTransaction(targetLine.detail_id);
            restoredToTarget = true;
        }
    }
    if (!restoredToTarget) {
        await loadNextTransaction();
    }
}

// ---------- HARD-REFRESH VIEW PERSISTENCE: SESSION VIEW ----------
// Restores the session view after a hard refresh from ?state=session&id=<sessionId> - re-fetches
// this session fresh from the server rather than restoring any cached view. GET audited_cashbook
// is used as the existence+data check since it already 404s cleanly on a missing/cancelled session
// (see get_audited_cashbook() in routers/verification.py); on success it also carries the cashbook
// name and full transaction list, so no extra request is needed to seed that state before handing
// off to activateSessionView(). Falls back to the dashboard with a clear message on failure,
// instead of leaving a broken session screen.
async function restoreSessionView(sessionId) {
    if (!sessionId || isNaN(sessionId)) {
        showDashboardWithMessage('That verification session link is invalid.');
        return;
    }
    try {
        // The Rulebook and audited-cashbook reads are independent. Keep the Rulebook promise
        // alive and pass it into activateSessionView() so it is fetched exactly once and still
        // must validate before the first voucher is rendered.
        const rulebookPromise = loadSessionRulebookWithRecovery(sessionId);
        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/audited_cashbook/${sessionId}`);
        if (!res.ok) {
            await rulebookPromise.catch(() => null);
            showDashboardWithMessage('This verification session is no longer available.');
            return;
        }
        const data = await res.json();
        // get_audited_cashbook() nests session fields under data.session, not top-level.
        selectedCashbook = (data.session && data.session.cashbook_name) || null;
        // Header format cleanup: set directly here (not left to the nested loadTransactionList()
        // call inside activateSessionView() below) - that call's own "already loaded, skip
        // re-fetch" guard short-circuits once sessionTransactions is already populated, which it
        // is by the very next line, so it would never reach the code that sets this otherwise.
        currentSessionPeriodStart = (data.session && data.session.period_start) || null;
        currentSessionPeriodEnd = (data.session && data.session.period_end) || null;
        sessionTransactions = data.transactions || [];
        totalTransactions = sessionTransactions.length;
        // Voucher hard-refresh restore: a `voucher` param means the auditor was on a specific
        // voucher (not necessarily the session's earliest-Pending one) when they refreshed.
        const targetVoucherId = new URLSearchParams(window.location.search).get('voucher');
        await activateSessionView(sessionId, targetVoucherId, rulebookPromise);
    } catch (e) {
        console.error('Error restoring verification session:', e);
        if (e.code === 'RULEBOOK_VERSION_MISMATCH' && e.recoveryDeclined) {
            showDashboardWithMessage('The session was left unchanged because Rulebook recovery was declined.');
        } else {
            showDashboardWithMessage('Could not restore this verification session.');
        }
    }
}

// ---------- LOAD NEXT TRANSACTION ----------
async function loadNextTransaction(detailId = null) {
    if (!currentSessionId) return;
    try {
        isNavigating = true;
        window.LoadingUI.showSkeleton(document.getElementById('voucherLinesContainer'), 3);
        const url = `${API_BASE}/api/verification/next/${currentSessionId}`;
        const res = await window.ApiUtils.apiFetch(url);
        if (!res.ok) {
            const err = await res.json();
        if (err.detail && err.detail.includes('All transactions')) {
                showStatus('✅ All transactions have been verified!', 'success');
                clearVoucherHeader();
                await updateProgressDisplay();
                return;
            }
            throw new Error(err.detail || 'Failed to load transaction');
        }
        const data = await res.json();
        if (data.completed) {
            showStatus('✅ All transactions have been verified!', 'success');
            clearVoucherHeader();
            await updateProgressDisplay();
            await updateProgressDisplay();
            return;
        }
        // Phase 2: /next now returns a voucher (header fields + ordered lines[]) instead of a
        // single row. Segment 1/2/3 (renderTransaction) still render one line at a time — the
        // first line is treated as "active" for now; per-line switching arrives in Phase 3.
        const voucherLines = data.lines || [];
        const activeLine = voucherLines[0];
        if (!activeLine) {
            showStatus('Voucher has no lines to display.', 'error');
            return;
        }
        currentDetailId = activeLine.detail_id;
        currentClientDataId = activeLine.client_data_id;
        currentVoucherId = data.voucher_id;
        setVerificationUrlVoucher(currentVoucherId);
        currentTransactionType = activeLine.transaction_type || '';
        lineItems = activeLine.line_items || [];
        procedureData = activeLine.procedure_data || {};
        // Update currentTransactionIndex from sessionTransactions
        if (sessionTransactions.length > 0) {
            currentTransactionIndex = sessionTransactions.findIndex(t => t.detail_id === currentDetailId);
            if (currentTransactionIndex === -1) {
                // If not found, maybe the list is not up to date; we'll set to 0
                currentTransactionIndex = 0;
            }
        } else {
            // If list not loaded, we can increment from previous index (if we have one)
            // But we'll just keep it as is; updateProgressDisplay will handle
        }
        // Cleanup pass: the standalone renderTransaction(activeLine) card is gone — the voucher
        // header bar (nav+save, above) and each line's own row (below) are the only rendering now.
        renderVoucherHeader(data.voucher_id, data.voucher_date, voucherLines);
        seedVoucherLineState(voucherLines);
        renderVoucherLines(voucherLines, data.voucher_date);
        await updateProgressDisplay(); // update the new progress element
        // Removed "Loaded transaction" message
    } catch (e) {
        console.error('Error loading transaction:', e);
        showStatus('Error loading transaction: ' + e.message, 'error');
    } finally {
        isNavigating = false;
    }
}

// ---------- VOUCHER HEADER ----------
// Renders the voucher-identity row, scoped to the voucher panel's own (60%) width: Prev voucher
// on the left, a centered title (voucher id + the 🔍 view-all icon inline right next to it — the
// icon is how you search for a specific voucher, so it sits as a sibling of the voucher number)
// with the date/line-count/amount/position subtitle below, and Next voucher on the right. Also
// renders #cashbookBar, outside .voucher-panel so it isn't confined to the table's width: cashbook
// name + session-wide voucher stats (total/verified, see computeVoucherStats()) on the left, the
// Cashbook return button on the right — all page-level info/actions, not scoped to "this voucher"
// the way Prev/Next are. No manual Save Progress button — progress already autosaves via
// saveAllVoucherLines() on Prev/Next/Complete/Exit (see saveCurrentTransaction(),
// completeTransaction(), returnToCashbookFromSession()), so a manual save had no effect a user could rely on. The
// per-line collapsed/expand rows (renderVoucherLines()) own everything else below this header.
function renderVoucherHeader(voucherId, voucherDate, lines) {
    const bar = document.getElementById('voucherHeaderBar');
    if (!bar) return;
    // Prev/Next processing UI: both conditions ANDed, exactly as applyVoucherNavButtonState() does
    // — a render that happens WHILE a navigation is still running must not re-enable the buttons.
    // That is not hypothetical: goToNextTransaction()'s passed branch runs completeTransaction() →
    // loadNextTransaction(), which re-renders this header mid-navigation.
    const pos = getVoucherPosition();
    const allowed = getVoucherNavAllowed(pos);
    const prevDisabled = voucherNavInFlight || !allowed.prev;
    const nextDisabled = voucherNavInFlight || !allowed.next;
    // Header format cleanup: subtitle is now "[session date range] · Voucher X of Y" - the old
    // "N line(s) · Amount X" segments duplicated the table body/total row below and were dropped.
    // The date range is the ACTIVE SESSION's own range (currentSessionPeriodStart/End), not the
    // individual voucher's own date - falls back to the voucher's date only if the range isn't
    // known yet (shouldn't normally happen - every entry path sets it before the first render).
    const rangeText = (currentSessionPeriodStart && currentSessionPeriodEnd)
        ? (currentSessionPeriodStart === currentSessionPeriodEnd
            ? currentSessionPeriodStart
            : `${currentSessionPeriodStart} – ${currentSessionPeriodEnd}`)
        : (voucherDate || '—');
    // The leading " · " lives inside the dynamic text itself (not static markup around the span),
    // same convention updateProgressDisplay() already uses - so an unknown/blank position shows
    // nothing dangling. Computed synchronously from `pos` (already available above) rather than
    // starting blank, so there's no flash of emptiness before updateProgressDisplay()'s async
    // confirmation lands.
    const posText = (pos.total > 0) ? ` · Voucher ${pos.index} of ${pos.total}` : '';
    bar.innerHTML = `
        <div class="verification-header-row voucher-nav-row">
            <button class="btn-voucher-nav" id="prevArrow" ${prevDisabled ? 'disabled' : ''} title="Previous transaction">← Prev voucher</button>
            <div class="voucher-title-block">
                <div class="voucher-title-content">
                    <div class="voucher-title">
                        <span class="voucher-title-link" id="voucherTitleAuditWorkbookLink" title="Open this voucher in Audit Workbook">Voucher ${voucherId || '—'}</span>
                        <span class="vh-icon" id="transactionListIcon" title="View all vouchers" style="cursor:pointer;">
                            <span class="vh-icon-symbol">🔍</span>
                        </span>
                        <span class="voucher-loading-badge-slot" id="voucherLoadingBadgeSlot"></span>
                    </div>
                    <div class="voucher-subtitle">${rangeText}<span id="voucherPositionCounter">${posText}</span></div>
                </div>
                <div class="voucher-nav-caption" id="voucherNavCaption">${voucherNavCaptionText}</div>
            </div>
            <button class="btn-voucher-nav" id="nextArrow" ${nextDisabled ? 'disabled' : ''} title="Next transaction">Next voucher →</button>
        </div>
    `;
    // One-shot: this render just consumed whatever caption was queued (if any) - reset so the
    // next render (any other navigation) shows the reserved slot blank, with no explicit
    // per-call-site clearing needed anywhere else.
    voucherNavCaptionText = '';
    // Processing/Loading UI, Phase 6: this is "a new voucher just got painted" - invalidate any
    // still-pending previous voucher's loading badge tracking (see voucherLoadToken's own comment).
    voucherLoadToken++;
    voucherLoadingBadgeRefCount = 0;

    const cashbookBar = document.getElementById('cashbookBar');
    if (cashbookBar) {
        const cashbookName = window._currentCashbook || selectedCashbook || '—';
        // Header voucher-wise stats (Phase 2 of 3): Total/Cleared/Query/Pending, sourced from
        // voucherStatsCache (GET /api/verification/session/{id}/voucher_stats, refreshVoucherStats()
        // below) instead of the old dead "Verified" counter. Total falls back to the synchronous,
        // client-side computeVoucherStats().totalVouchers until the first fetch resolves — Phase 1
        // confirmed this agrees exactly with the endpoint's own `total`, so the fallback is never
        // wrong, just momentarily sourced differently; Cleared/Query/Pending default to 0 until
        // then, mirroring the old counter's default "Verified 0" state.
        const statsTotal = voucherStatsCache ? voucherStatsCache.total : computeVoucherStats().totalVouchers;
        const statsCleared = voucherStatsCache ? voucherStatsCache.cleared : 0;
        const statsQuery = voucherStatsCache ? voucherStatsCache.query : 0;
        const statsPending = voucherStatsCache ? voucherStatsCache.pending : 0;
        // Bank Verification (Phase 8): "Cashbook"/📒 vs "Bankbook"/🏦 label, driven by currentType.
        const bookLabel = currentType === 'bank' ? 'Bankbook' : 'Cashbook';
        const bookIcon = currentType === 'bank' ? '🏦' : '📒';
        cashbookBar.innerHTML = `
            <div class="cashbook-stats">
                <span class="cashbook-stat"><span class="cashbook-stat-label">${bookLabel}</span><span class="cashbook-stat-value">${cashbookName}</span></span>
                <span class="cashbook-stat"><span class="cashbook-stat-label">Total</span><span class="cashbook-stat-value" id="voucherStatTotal">${statsTotal}</span></span>
                <span class="cashbook-stat"><span class="cashbook-stat-label">Cleared</span><span class="cashbook-stat-value cleared" id="voucherStatCleared">${statsCleared}</span></span>
                <span class="cashbook-stat"><span class="cashbook-stat-label">Query</span><span class="cashbook-stat-value query" id="voucherStatQuery">${statsQuery}</span></span>
                <span class="cashbook-stat"><span class="cashbook-stat-label">Pending</span><span class="cashbook-stat-value pending" id="voucherStatPending">${statsPending}</span></span>
            </div>
            <div class="cashbook-bar-actions">
                <button class="btn-cashbook" id="cashbookReturnBtn"><span class="btn-icon">${bookIcon}</span> Change ${bookLabel}</button>
                <button class="btn-cashbook" id="auditWorkbookBtn"><span class="btn-icon">📗</span> Audit Workbook</button>
            </div>
        `;
        document.getElementById('cashbookReturnBtn')?.addEventListener('click', function() {
            if (typeof returnToCashbookFromSession === 'function') returnToCashbookFromSession();
        });
        // Audit Workbook peer nav button (Phase 5, FINAL): same-tab navigation to the read-only
        // per-voucher rollup view for this exact session — a plain, neutral action button here,
        // not "active"-styled, since neither this button nor "Change Cashbook" represents the
        // screen currently open (that's the voucher/session view itself, not either destination).
        document.getElementById('auditWorkbookBtn')?.addEventListener('click', function() {
            if (typeof navigateToAuditWorkbook === 'function') navigateToAuditWorkbook();
        });
    }

    // ----- Event wiring (rebound after every innerHTML write, matching this file's convention) -----
    document.getElementById('prevArrow')?.addEventListener('click', function() {
        if (typeof goToPreviousTransaction === 'function') goToPreviousTransaction();
    });
    document.getElementById('nextArrow')?.addEventListener('click', function() {
        if (typeof goToNextTransaction === 'function') goToNextTransaction();
    });
    // Voucher No. text (distinct from the 🔍 mirror icon right beside it, which is untouched) —
    // reuses navigateToAuditWorkbook() verbatim, the same session-based lookup the peer nav
    // button in #cashbookBar already uses, per the confirmed brief (no new endpoint). Unlike that
    // peer button, this link carries the specific voucherId so Audit Workbook lands already
    // positioned on this exact voucher's row (see navigateToAuditWorkbook()'s voucherNo param).
    document.getElementById('voucherTitleAuditWorkbookLink')?.addEventListener('click', function() {
        if (typeof navigateToAuditWorkbook === 'function') navigateToAuditWorkbook(voucherId);
    });
    document.getElementById('transactionListIcon')?.addEventListener('click', function() {
        const popover = document.getElementById('transactionListPopover');
        if (!popover) return;
        if (popover.style.display === 'block') {
            closeTransactionPopover();
            return;
        }
        if (typeof loadTransactionList === 'function') loadTransactionList(true);
    });
}

// Clear the voucher header bar and Cashbook bar (used when a session completes or exits).
function clearVoucherHeader() {
    const bar = document.getElementById('voucherHeaderBar');
    if (bar) bar.innerHTML = '';
    const cashbookBar = document.getElementById('cashbookBar');
    if (cashbookBar) cashbookBar.innerHTML = '';
    // Reset so a later session's first render doesn't briefly show this session's stale counts.
    voucherStatsCache = null;
}

// Persistent badge next to the voucher title, shown for the whole-voucher automatic load only
// (renderVoucherLines()'s per-line batch, driven by the ref count below) — unrelated to a
// Procedures/Observations badge click, which shows its own in-field spinner instead (see
// openProcedureModal() in procedure_modal.js). Ref-counted rather than a plain boolean so an
// in-flight batch from a just-superseded voucher (see the myLoadToken guard where this is called)
// can never wrongly hide the current voucher's badge. Looks up #voucherLoadingBadgeSlot fresh on
// every call since renderVoucherHeader() rebuilds it as a new DOM node on every voucher render.
// Built directly (not via LoadingUI.showBadge, which is text-only) so it can carry a small spinner
// icon alongside the message — .ui-badge still supplies the overflow-safe pill styling.
function showVoucherLoadingBadge(message) {
    voucherLoadingBadgeRefCount++;
    const slot = document.getElementById('voucherLoadingBadgeSlot');
    if (!slot) return;
    slot.innerHTML = `<span class="ui-badge voucher-loading-badge">` +
        `<span class="ui-spinner ui-spinner-sm"></span>` +
        `<span>${escapeHtml(message || 'Loading…')}</span></span>`;
}

function hideVoucherLoadingBadge() {
    if (voucherLoadingBadgeRefCount > 0) voucherLoadingBadgeRefCount--;
    if (voucherLoadingBadgeRefCount > 0) return;
    const slot = document.getElementById('voucherLoadingBadgeSlot');
    if (slot) slot.innerHTML = '';
}

// Audit Workbook (Phase 5, FINAL): navigates to the read-only per-voucher rollup page for the
// currently open session, same-tab (this is "switch screens", not a side lookup — matches the
// existing "Change Cashbook" button's own same-tab behavior). Session-scoped only by default (the
// peer nav button in #cashbookBar calls this with no arg). An optional voucherNo (Voucher No. →
// Audit Workbook deep link, confirmed requirement) is carried as a companion ?voucher= param so
// Audit Workbook can land already positioned on that specific voucher's row — see
// positionOnTargetVoucher() in audit_workbook.js for the landing side of this.
function navigateToAuditWorkbook(voucherNo) {
    if (!currentSessionId) return;
    const params = new URLSearchParams({ session: currentSessionId });
    if (voucherNo) params.set('voucher', voucherNo);
    window.location.href = `/static/audit_workbook.html?${params}`;
}

// ---------- VOUCHER LINE ALIAS HELPERS (Phase 3) ----------
// Small, shared alias lookups reused by renderVoucherLines() — mirrors the alias arrays already
// used inside renderTransaction(), kept local to avoid touching that function (left untouched
// this phase per the confirmed plan).
const VOUCHER_LINE_ACCOUNT_CODE_ALIASES = ['Account Detail', 'Account Details', 'Account Code', 'Account No', 'Account Number', 'Code', 'Ledger Code', 'AccountID', 'Account Head', 'Ledger Detail'];
const VOUCHER_LINE_ACCOUNT_NAME_ALIASES = ['Account Name', 'Account', 'Account Detail', 'Ledger', 'Head'];

// VOUCHER_LINE_TAB_ID/ACCOUNT_HEAD_OBSERVATION_DOC_ID/voucherChecksCache/voucherChecksLoadingPromises now live in procedure_modal.js.

// Header voucher-wise stats (Phase 2 of 3) — last-fetched {total, cleared, query, pending} for
// the whole cashbook session (GET /api/verification/session/{id}/voucher_stats), not just the
// open voucher. null until refreshVoucherStats() resolves at least once for the current session.
// See refreshVoucherStats() and renderVoucherHeader() below.
let voucherStatsCache = null;
const VOUCHER_LINE_DESCRIPTION_ALIASES = ['Description', 'Voucher Detail', 'Narration', 'Remarks', 'Particulars'];
const VOUCHER_LINE_DEBIT_ALIASES = ['Debit Amount', 'Debit', 'Dr Amount', 'Dr'];
const VOUCHER_LINE_CREDIT_ALIASES = ['Credit Amount', 'Credit', 'Cr Amount', 'Cr'];

// accountHeadCache/getAccountHeadState() now live in procedure_modal.js.

// latestTbAccountsCache/fetchLatestTbAccounts() now live in procedure_modal.js.

function getVoucherLineAlias(row, aliases) {
    for (const alias of aliases) {
        if (row[alias] !== undefined && row[alias] !== null && row[alias] !== '') return row[alias];
    }
    return '—';
}

// Phase 9 (Audit Rules): the Supporting column's document links are sourced live from Audit
// Rules Categories, replacing the old fixed 5-document VOUCHER_LINE_PROCEDURES constant.
// (The Type dropdown's own option list — previously also sourced from Audit Rules Categories via
// a module-level auditCategories array/loadAuditCategories() — was removed in the Verification ↔
// Assign Categories link, Phase 4: Type is no longer manually picked, so there is no dropdown
// left to populate options for.)

// GET /api/verification/audit_guidance response, keyed by Category name (the same value the
// Type dropdown stores) — shared between the "Guide" popover, the Supporting column, and (as of
// Session-level ref-data caching, Phase 2 of 4) openProcedureModal(), so a Category's guidance is
// fetched once per page session and reused everywhere else that needs it. 404s cache as null; a
// transient fetch error is deliberately NOT cached, so a later retry can succeed. The response now
// carries matrix_version (audit_rule_categories.matrix_version) alongside the rest of the
// payload — reserved for a future live staleness check; nothing in this phase reads it yet.
let auditGuidanceCache = {};
// In-flight-request dedup fix (confirmed live 2026-08-26, HAR-traced): two lines sharing a
// Category can both trigger fetchAuditGuidanceData() in the same render pass before either
// fetch resolves — a completed-cache-only check (auditGuidanceCache above) lets both through,
// since neither has written the cache yet. Mirrors ensureVoucherChecksLoaded()'s
// voucherChecksLoadingPromises (procedure_modal.js) — same race, same fix shape.
let auditGuidanceLoadingPromises = {};
let categoryForAccountLoadingPromises = {};

// The session Rulebook intentionally stores cross-check document references as stable IDs. The
// legacy guidance endpoint already returns display names, so normalize the session shape here at
// the Verification adapter boundary instead of changing either public response contract. Keep the
// original IDs and all document metadata available for future consumers while supplying the
// `documents` name list expected by buildAuditGuidanceTable().
function normalizeSessionRulebookGuidance(profile) {
    const sourceDocuments = Array.isArray(profile?.documents) ? profile.documents : [];
    const documents = sourceDocuments.map(document => {
        const documentId = document.document_id ?? document.id;
        const authorityRoles = Array.isArray(document.authority_roles)
            ? document.authority_roles
            : (Array.isArray(document.auth_roles) ? document.auth_roles : []);
        return {
            ...document,
            document_id: documentId,
            id: documentId,
            auth_roles: authorityRoles.map(role => role?.role_name || role)
        };
    });
    const documentNamesById = new Map(
        documents
            .filter(document => document.document_id != null)
            .map(document => [String(document.document_id), document.name || `Document ${document.document_id}`])
    );
    const crossChecks = (Array.isArray(profile?.cross_checks) ? profile.cross_checks : []).map(check => {
        const documentIds = Array.isArray(check.document_ids)
            ? check.document_ids
            : (Array.isArray(check.documentIds) ? check.documentIds : []);
        const resolvedNames = documentIds.map(documentId =>
            documentNamesById.get(String(documentId)) || `Document ${documentId}`
        );
        const existingNames = Array.isArray(check.documents) ? check.documents : [];
        return {
            ...check,
            document_ids: documentIds,
            documents: resolvedNames.length ? resolvedNames : existingNames
        };
    });
    return {
        category_id: profile?.category_id,
        category_name: profile?.name,
        kb_overview: profile?.kb_overview,
        matrix_version: profile?.matrix_version,
        documents,
        cross_checks: crossChecks
    };
}

async function fetchAuditGuidanceData(type, forceRefresh = false) {
    if (!forceRefresh && Object.prototype.hasOwnProperty.call(auditGuidanceCache, type)) {
        return auditGuidanceCache[type];
    }
    if (!forceRefresh && auditGuidanceLoadingPromises[type]) {
        return auditGuidanceLoadingPromises[type];
    }
    const loadPromise = (async () => {
        if (sessionRulebook) {
            const profile = (sessionRulebook.categories || []).find(category => category.name === type);
            if (profile) {
                const guidance = normalizeSessionRulebookGuidance(profile);
                auditGuidanceCache[type] = guidance;
                return guidance;
            }
        }
        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/audit_guidance?transaction_type=${encodeURIComponent(type)}`);
        if (res.status === 404) {
            auditGuidanceCache[type] = null;
            return null;
        }
        if (!res.ok) throw new Error('Failed to fetch audit guidance');
        const data = await res.json();
        auditGuidanceCache[type] = data;
        return data;
    })();
    auditGuidanceLoadingPromises[type] = loadPromise;
    try {
        return await loadPromise;
    } finally {
        delete auditGuidanceLoadingPromises[type];
    }
}

// templateColumnsCache/templateNameCache/fetchTemplateColumns() now live in procedure_modal.js.

// Fetch of a line's active assignment records — used to determine which documents count as
// "complete" for the Procedures ring+badge below. Fails soft to [] so a fetch hiccup never breaks
// the cell itself.
async function fetchAssignedDocumentRecords(uid) {
    try {
        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/voucher_line_documents/${uid}`);
        if (!res.ok) return [];
        const data = await res.json();
        return data.documents || [];
    } catch (e) {
        console.error('Failed to load assignment status for Procedures cell:', e);
        return [];
    }
}

// Fetch of a line's ticked/unticked audit-procedure roles, across all documents — needed so the
// Procedures cell can judge a no-template document's completion (every configured role ticked)
// without depending on procedureModalState, which is only populated once the modal has actually
// been opened. Same fail-soft-to-[] convention as fetchAssignedDocumentRecords().
async function fetchProcedureChecksForLine(uid) {
    try {
        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/procedure_checks/${uid}`);
        if (!res.ok) return [];
        const data = await res.json();
        return data.checks || [];
    } catch (e) {
        console.error('Failed to load procedure checks for Procedures cell:', e);
        return [];
    }
}

// Small SVG completion ring, adapted from the v7 prototype's ring() helper — pct is 0-100, cls is
// 'pending'/'partial'/'done' (drives the stroke color via the CSS class, matching this file's
// existing traffic-light convention rather than the prototype's own separate palette).
function buildProcedureRingSvg(pct, cls) {
    const r = 6, c = 2 * Math.PI * r;
    const off = c - (pct / 100) * c;
    return `<svg class="voucher-line-proc-ring" viewBox="0 0 16 16">
        <circle cx="8" cy="8" r="${r}" fill="none" stroke="#e2e8f0" stroke-width="2.5"/>
        <circle cx="8" cy="8" r="${r}" fill="none" class="voucher-line-proc-ring-fill ${cls}" stroke-width="2.5"
            stroke-dasharray="${c}" stroke-dashoffset="${off}" transform="rotate(-90 8 8)" stroke-linecap="round"/>
    </svg>`;
}

// ---------- PROCEDURES/OBSERVATIONS COLUMN EXPAND TOGGLE (Phase 4) ----------
// A single shared boolean drives both columns' render together — there is deliberately no
// per-column state. Either header's icon toggles it; both columns re-render in lockstep.
// Not persisted: reset to collapsed on every new voucher load (see seedVoucherLineState()).
let columnsExpanded = false;

// Syncs both header icons' glyph/title/class to the current columnsExpanded value — called after
// every render and after every toggle, so a stale icon never disagrees with what's showing below.
function updateColumnToggleIcons() {
    document.querySelectorAll('.voucher-col-toggle-icon').forEach(icon => {
        icon.textContent = columnsExpanded ? '⤡' : '⤢';
        icon.title = columnsExpanded ? 'Collapse' : 'Expand';
        icon.classList.toggle('expanded', columnsExpanded);
    });
    // Drives the flex-start alignment override (style.css) so an expanded cell's list sits at the
    // top of its row instead of the collapsed badge's vertically-centered position.
    const container = document.getElementById('voucherLinesContainer');
    if (container) container.classList.toggle('columns-expanded', columnsExpanded);
}

// Flips the shared state and re-renders every currently-rendered line's Procedures/Observations
// cells under it — reuses each line's already-resolved Category (voucherLineState[uid].
// transactionType) rather than re-running the Type auto-fill lookup itself.
function toggleColumnsExpanded() {
    columnsExpanded = !columnsExpanded;
    updateColumnToggleIcons();
    Object.keys(voucherLineState).forEach(uid => {
        const type = voucherLineState[uid].transactionType;
        if (type) {
            updateProceduresCellForLine(uid, type);
            updateObservationsCellForLine(uid, type);
        }
    });
}

// Renders the Procedures column's ring+count badge for one line, from its currently-selected
// Category's attached Documents — replaces the old per-document link list (a document is now
// reached via the modal's own left rail, once open, not from a per-name link in the table).
// Async because the Category's guidance (and thus its Documents) is fetched on demand; called both
// at initial render (for a line with an already-seeded type) and from the Type auto-fill.
// Completion mirrors isProcedureDocumentComplete() (template doc → assigned; no-template doc →
// every configured role ticked) but computed standalone here, since procedureModalState is only
// populated once the modal has actually been opened for this line.
async function updateProceduresCellForLine(uid, type, guidancePromise) {
    // Targets the inner slot, not the outer .voucher-line-procedures-cell — the cell also holds
    // the persistent risk dot (see applyRiskToCell()), which must survive this function's
    // innerHTML rewrites rather than being wiped out by them.
    const cell = document.querySelector(`.voucher-line-procedures-slot[data-detail-id="${uid}"]`);
    if (!cell) return;
    if (!type) {
        cell.innerHTML = '';
        return;
    }
    cell.innerHTML = '<span style="color:#94a3b8; font-size:12px;">Loading…</span>';
    // These two don't depend on the guidance fetch's result — start them immediately rather than
    // after guidance resolves (per-line fetch batching). Fail-soft to [] internally, so letting
    // them run even when the early "no documents" return below fires is harmless.
    const cachedModalData = procedureModalState[uid] && procedureModalState[uid].standaloneData;
    const voucherDataPromise = loadVoucherSupportData();
    const assignedPromise = voucherDataPromise.then(all => all[String(uid)]?.linked_documents?.documents || (cachedModalData?.assignedRecords || []));
    const checksListPromise = voucherDataPromise.then(all => all[String(uid)]?.procedure_checks?.checks || (cachedModalData?.procedureChecks || []));
    let data = null;
    try {
        data = await (guidancePromise || fetchAuditGuidanceData(type));
    } catch (e) {
        console.error('Failed to load Procedures documents:', e);
    }
    // Stale guard: the line may have moved on to a different type (or a new voucher entirely)
    // while this fetch was in flight — don't clobber a newer selection.
    if (!voucherLineState[uid] || voucherLineState[uid].transactionType !== type) return;
    const documents = (data && data.documents) || [];
    if (!documents.length) {
        cell.innerHTML = '<span style="color:#94a3b8; font-size:12px;">No documents for this category</span>';
        return;
    }

    // Transaction level approval (Phase 4 of 4, FINAL, re-pointed by the Generic Voucher-Level
    // Checks cutover): only fetch/await the voucher-level checks list when at least one of this
    // Category's Documents is actually flagged — avoids an extra network call for the common
    // unflagged case. Warms the same voucherChecksCache openProcedureModal() reads, via the shared
    // ensureVoucherChecksLoaded(). Physical Bill evidence type: same lazy-fetch pattern for the
    // saved bill entries, only when at least one of this Category's Documents is physical_bill.
    const needsVoucherChecks = documents.some(doc => doc.transaction_level_approval);
    const needsPhysicalBills = documents.some(doc => doc.document_type === 'physical_bill');
    const [assignedRecords, procedureChecks, checks, physicalBills] = await Promise.all([
        assignedPromise,
        checksListPromise,
        needsVoucherChecks ? ensureVoucherChecksLoaded(getClientId(), currentVoucherId) : Promise.resolve(null),
        needsPhysicalBills ? voucherDataPromise.then(all => all[String(uid)]?.physical_bills?.bills || (cachedModalData?.physicalBillEntries || [])) : Promise.resolve([])
    ]);
    // Re-check the stale guard after these awaits, same reason as above.
    if (!voucherLineState[uid] || voucherLineState[uid].transactionType !== type) return;
    const assignedDocIds = new Set(assignedRecords.map(r => r.document_id));
    // This line's own amount, for the physical_bill amount-match check below — same lookup
    // openProcedureModal() already uses (currentVoucherLines is populated by seedVoucherLineState()
    // before renderVoucherLines()/this function can ever run).
    const lineIdx = currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    const voucherLine = lineIdx !== -1 ? currentVoucherLines[lineIdx] : null;
    const voucherLineAmount = voucherLine ? parseVoucherAmount(getVoucherLineAmount(voucherLine.row_data || {})) : 0;
    // Mirrors isProcedureDocumentComplete()'s base-then-gate shape so this standalone badge never
    // disagrees with the modal tab's own completion dot for a flagged Document.
    const isDocDone = doc => {
        let base;
        // Physical Bill evidence type: checked BEFORE doc.template_id (always null for this doc
        // type), same document_type-first branch order as isProcedureDocumentComplete() and the
        // backend gate — a physical_bill Document must never fall into the generic no-template
        // auth_roles check below, which is not its real completion signal.
        if (doc.document_type === 'physical_bill') {
            // Multi-Bill/Multi-Line Physical Bill (Phases 1-3): a Document can now have several
            // bills, each with several items — .filter() (not .find()) and sum every matching
            // bill's server-computed bill_total (null whenever that bill has zero items or any
            // item's amount doesn't resolve numeric — same rule _evaluate_physical_bill_completion()
            // uses server-side, reproduced here in one field check rather than re-deriving item
            // sums client-side).
            const docBills = physicalBills.filter(b => b.document_id === doc.id);
            const headersComplete = docBills.length > 0 && docBills.every(bill =>
                (bill.bill_no || '').trim() && (bill.bill_date || '').trim() && (bill.vendor_name || '').trim() && bill.bill_total != null
            );
            base = headersComplete && Math.abs(docBills.reduce((sum, bill) => sum + bill.bill_total, 0) - voucherLineAmount) <= 0.005;
        } else if (doc.template_id) {
            base = assignedDocIds.has(doc.id);
        } else {
            const roles = doc.auth_roles || [];
            base = roles.length > 0 && roles.every(role => procedureChecks.some(c => c.document_id === doc.id && c.role_name === role && c.is_checked));
        }
        if (!base) return false;
        if (doc.transaction_level_approval) return isVoucherChecksComplete(checks);
        return true;
    };

    if (columnsExpanded) {
        const itemsHtml = documents.map(doc => {
            const done = isDocDone(doc);
            return `
                <div class="voucher-line-proc-expanded-item">
                    <span class="voucher-line-proc-expanded-dot ${done ? 'done' : 'pending'}">${done ? '✔' : '●'}</span>
                    <span class="voucher-line-proc-expanded-name">${escapeHtml(doc.name)}</span>
                </div>
            `;
        }).join('');
        cell.innerHTML = `<div class="voucher-line-proc-expanded-list" data-uid="${uid}">${itemsHtml}</div>`;
        cell.querySelector('.voucher-line-proc-expanded-list')?.addEventListener('click', function(e) {
            e.stopPropagation();
            openProcedureModal(this.dataset.uid, null, buildProcedureModalAdapter(), this);
        });
        return;
    }

    const done = documents.filter(isDocDone).length;
    const total = documents.length;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const cls = done === 0 ? 'pending' : (done === total ? 'done' : 'partial');

    cell.innerHTML = `
        <span class="voucher-line-procedures-badge ${cls}" data-uid="${uid}" title="${done} of ${total} documents complete">
            ${buildProcedureRingSvg(pct, cls)}${done}/${total}
        </span>
    `;
    cell.querySelector('.voucher-line-procedures-badge')?.addEventListener('click', function(e) {
        e.stopPropagation();
        openProcedureModal(this.dataset.uid, null, buildProcedureModalAdapter(), this);
    });
}

// Fetch of a line's saved observation entries (one per document with a non-empty observation) —
// used only for the Observations column's count/tooltip. Fails soft to [] so a fetch hiccup never
// breaks the cell itself.
async function fetchLineObservationEntries(uid) {
    // Voucher rendering already loads all line observations through the single voucher-level
    // support request. Reuse that result here; otherwise every line pays another cross-region
    // request even though the data is already present in voucherSupportCache.
    const voucherData = await loadVoucherSupportData();
    const cached = voucherData[String(uid)]?.observations?.entries;
    if (Array.isArray(cached)) return cached;
    try {
        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/line_observations/${uid}`);
        if (!res.ok) return [];
        const data = await res.json();
        return data.entries || [];
    } catch (e) {
        console.error('Failed to load observations for Observations cell:', e);
        return [];
    }
}

// Renders the Observations column's flag+count for one line — replaces the old raw-text preview.
// Reading the actual observation text now requires opening the modal (each document's own
// Observation segment), matching the v7 prototype exactly.
async function updateObservationsCellForLine(uid, type, guidancePromise) {
    const cell = document.querySelector(`.voucher-line-observations-cell[data-detail-id="${uid}"]`);
    if (!cell) return;
    if (!type) {
        cell.innerHTML = '';
        return;
    }
    const entries = await fetchLineObservationEntries(uid);
    // Stale guard: the line may have moved on to a different type (or a new voucher entirely)
    // while this fetch was in flight — don't clobber a newer selection.
    if (!voucherLineState[uid] || voucherLineState[uid].transactionType !== type) return;

    // A document can have a voucher_line_observations row with blank text (e.g. cleared then
    // explicitly synced) — that's a "nothing recorded" state, not a recorded observation, so both
    // the collapsed count and the expanded list must exclude it (previously only the expanded
    // branch did, leaving the collapsed badge permanently over-counting once anything was cleared).
    const nonEmpty = entries.filter(e => (e.observation_text || '').trim());

    if (columnsExpanded) {
        if (!nonEmpty.length) {
            cell.innerHTML = '<span class="voucher-line-obs-none">—</span>';
            return;
        }
        // Document names aren't on the entry itself — reuse the same cached guidance fetch the
        // Procedures cell already draws from, no new endpoint.
        let data = null;
        try {
            data = await (guidancePromise || fetchAuditGuidanceData(type));
        } catch (e) {
            console.error('Failed to load document names for expanded Observations:', e);
        }
        if (!voucherLineState[uid] || voucherLineState[uid].transactionType !== type) return;
        const nameById = {};
        ((data && data.documents) || []).forEach(doc => { nameById[doc.id] = doc.name; });
        const itemsHtml = nonEmpty.map(entry => `
            <div class="voucher-line-obs-expanded-item">
                <span class="voucher-line-obs-expanded-doc">${escapeHtml(nameById[entry.document_id] || 'Document')}:</span>
                <span class="voucher-line-obs-expanded-text">${escapeHtml(entry.observation_text)}</span>
            </div>
        `).join('');
        cell.innerHTML = `<div class="voucher-line-obs-expanded-list" data-uid="${uid}">${itemsHtml}</div>`;
        cell.querySelector('.voucher-line-obs-expanded-list')?.addEventListener('click', function(e) {
            e.stopPropagation();
            openProcedureModal(this.dataset.uid, null, buildProcedureModalAdapter(), this);
        });
        return;
    }

    const count = nonEmpty.length;
    if (!count) {
        cell.innerHTML = '<span class="voucher-line-obs-none">—</span>';
        return;
    }
    cell.innerHTML = `<span class="voucher-line-obs-flag" data-uid="${uid}" title="${count} observation${count === 1 ? '' : 's'} recorded">⚑ ${count}</span>`;
    cell.querySelector('.voucher-line-obs-flag')?.addEventListener('click', function(e) {
        e.stopPropagation();
        openProcedureModal(this.dataset.uid, null, buildProcedureModalAdapter(), this);
    });
}

// ---------- TYPE AUTO-FILL FROM ASSIGN CATEGORIES (Verification ↔ Assign Categories link, Phase 4) ----------
// The Type field is no longer manually picked — it auto-fills from whatever Category was set for
// this account code on the Assign Categories screen (Trial Balance module), via Phase 3's
// GET /api/verification/category-for-account lookup. Assign Categories remains the only place a
// Category is ever set; Verification only reads. Cached by account code (not by client, since a
// session is always scoped to one client) — mirrors auditGuidanceCache's convention — so
// revisiting an already-looked-up voucher this session doesn't re-hit the endpoint. A matched
// name caches as itself, "no match" caches as null; a genuine fetch error is NOT cached, so a
// later retry (e.g. reloading the voucher) can succeed.
let categoryForAccountCache = {};

// Writes the resolved Category (or null/unassigned) into one line's Type cell and state. Re-queries
// the DOM by data-uid rather than taking element references, so it's a safe no-op if the line's
// row is no longer present (e.g. the voucher was navigated away from while the lookup was in
// flight) instead of writing into a detached, stale node.
function applyCategoryToTypeCell(uid, categoryName, { frozen = false } = {}) {
    const valueEl = document.querySelector(`.voucher-line-type-value[data-uid="${uid}"]`);
    const iconEl = document.querySelector(`.voucher-line-type-recycle-icon[data-uid="${uid}"]`);
    const lockEl = document.querySelector(`.voucher-line-type-lock-icon[data-uid="${uid}"]`);
    if (!valueEl) return; // row no longer rendered

    if (!voucherLineState[uid]) voucherLineState[uid] = { transactionType: '', procedureData: {} };
    voucherLineState[uid].transactionType = categoryName || '';
    voucherLineState[uid].procedureData.transaction_type = categoryName || '';

    valueEl.textContent = categoryName || 'Unassigned';
    // Freeze Category on voucher lines: a frozen line is by construction always matched (only a
    // successful resolve ever gets frozen), so the recycle icon stays exclusive to the live
    // "Unassigned" case and the lock icon is exclusive to the frozen case - never both.
    if (iconEl) iconEl.style.display = (!frozen && !categoryName) ? 'inline-flex' : 'none';
    if (lockEl) lockEl.style.display = frozen ? 'inline-flex' : 'none';

    // Procedures/Observations cells start empty in the template and are only ever populated once
    // the category resolves — same "only call on a real match" convention the old Supporting cell
    // used, so an Unassigned line's cells simply stay blank rather than being explicitly cleared.
    if (categoryName) {
        // Shared across all three cell updates so a cold auditGuidanceCache only triggers one
        // network fetch per line instead of each of them racing to fetch it independently.
        const guidancePromise = fetchAuditGuidanceData(categoryName);
        // Processing/Loading UI, Phase 6: returned (not fire-and-forget) so renderVoucherLines()
        // can track when this line's procedure badge — specifically, the one assumption #3 of the
        // voucher-loading badge cares about — has actually resolved. Observations/Risk aren't part
        // of that signal (per the confirmed brief), so their own promises stay un-awaited here.
        const proceduresPromise = updateProceduresCellForLine(uid, categoryName, guidancePromise);
        updateObservationsCellForLine(uid, categoryName, guidancePromise);
        updateRiskCellForLine(uid, categoryName, guidancePromise);
        return proceduresPromise;
    } else {
        applyRiskToCell(uid, null);
        return Promise.resolve();
    }
}

// ---------- AUDIT RISK INDICATOR (Supporting Documents modal redesign, final phase) ----------
// Read-only reference: a Category's highest-rated Document risk_rating, shown as a colored pill,
// with a hover tooltip listing each Document's required procedures (auth_roles). Sourced from the
// same cached fetchAuditGuidanceData(type) the Supporting column already uses — no new endpoint.
const RISK_RATING_ORDER = { Low: 1, Medium: 2, High: 3 };

// Computes { risk, tooltip } from an audit_guidance response's documents[] — risk is the highest
// risk_rating among them (documents with no rating set are ignored, never fabricated), tooltip is
// one line per document that has auth_roles, e.g. "Purchase Order: Prepared by, Approved by".
function computeLineRiskData(auditGuidanceData) {
    const documents = (auditGuidanceData && auditGuidanceData.documents) || [];
    let risk = null;
    documents.forEach(doc => {
        if (doc.risk_rating && (!risk || RISK_RATING_ORDER[doc.risk_rating] > RISK_RATING_ORDER[risk])) {
            risk = doc.risk_rating;
        }
    });
    const tooltip = documents
        .filter(doc => (doc.auth_roles || []).length)
        .map(doc => `${doc.name}: ${doc.auth_roles.join(', ')}`)
        .join('\n');
    return { risk, tooltip };
}

// Writes the risk dot inside one line's Procedures cell (sits beside the procedure ring+count
// badge) — hidden (no risk-* class) if no document in its category has a risk rating set, "never
// fabricated" per the original design. Re-queries the DOM by data-uid, same safe-no-op-if-row-gone
// convention as applyCategoryToTypeCell(). Originally a standalone text pill in its own Risk
// column, then a dot in a since-removed "Learning" cluster; risk-low/medium/high reuse the exact
// hex values the old .voucher-line-risk-pill used.
function applyRiskToCell(uid, riskData) {
    const dot = document.querySelector(`.voucher-line-risk-dot[data-uid="${uid}"]`);
    if (!dot) return;
    dot.className = 'voucher-line-risk-dot';
    if (!riskData || !riskData.risk) {
        dot.title = '';
        return;
    }
    const riskClass = riskData.risk.toLowerCase();
    const tooltip = riskData.tooltip || 'No required procedures configured';
    dot.classList.add(`risk-${riskClass}`);
    dot.title = `${riskData.risk} risk\n${tooltip}`;
}

// Looks up the Risk cell's data for one line's Category, mirroring updateProceduresCellForLine()'s
// shape (blank on no type, stale-guard re-check after the await since the line's Category can
// change while this fetch is in flight).
async function updateRiskCellForLine(uid, type, guidancePromise) {
    if (!type) {
        applyRiskToCell(uid, null);
        return;
    }
    let data = null;
    try {
        data = await (guidancePromise || fetchAuditGuidanceData(type));
    } catch (e) {
        console.error('Failed to load Risk indicator:', e);
    }
    if (!voucherLineState[uid] || voucherLineState[uid].transactionType !== type) return;
    applyRiskToCell(uid, computeLineRiskData(data));
}

// Looks up the Category for one line's account code and applies it to that line's Type cell.
// accountCode may be the '—' placeholder (getVoucherLineAlias() found no matching column) — in
// that case there is nothing to look up, so it's treated as unassigned without a network call.
async function autoFillCategoryForLine(uid, accountCode) {
    if (!accountCode || accountCode === '—') {
        return applyCategoryToTypeCell(uid, null);
    }

    let categoryName = null;
    try {
        const rulebookCategory = sessionRulebook?.account_category_index?.[accountCode];
        if (rulebookCategory) {
            categoryName = rulebookCategory.category_name || null;
            categoryForAccountCache[accountCode] = categoryName;
        } else if (Object.prototype.hasOwnProperty.call(categoryForAccountCache, accountCode)) {
            categoryName = categoryForAccountCache[accountCode];
        } else if (categoryForAccountLoadingPromises[accountCode]) {
            categoryName = await categoryForAccountLoadingPromises[accountCode];
        } else {
            const clientId = getClientId();
            const params = new URLSearchParams({ client_id: clientId, account_code: accountCode });
            const loadPromise = (async () => {
                const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/category-for-account?${params}`);
                if (!res.ok) throw new Error('Failed to look up category for account');
                const data = await res.json();
                const name = data.matched ? data.category_name : null;
                categoryForAccountCache[accountCode] = name;
                return name;
            })();
            categoryForAccountLoadingPromises[accountCode] = loadPromise;
            try { categoryName = await loadPromise; }
            finally { delete categoryForAccountLoadingPromises[accountCode]; }
        }
    } catch (e) {
        console.error('Category auto-fill lookup failed:', e);
        categoryName = null; // fail safe to unassigned — not cached, so a later attempt can retry
    }

    return applyCategoryToTypeCell(uid, categoryName);
}

// Order matches .voucher-line-summary-grid's columns: Sr No., Date, Account Code, Account Type,
// Account Name, Amount, Procedures, Observations, Status. Values are percentages of the container
// width, summing to 100 by default so the table fits the visible screen with no horizontal scroll.
// User-resizable (see the drag handles in renderVoucherLines()'s header row, percentage-of-
// container math) — persists at module scope across renders/navigation, same convention as
// voucherLineState, so dragging a column survives Prev/Next voucher navigation. A drag can push
// the total over 100%, in which case the container's overflow-x: auto is the intended fallback.
let voucherColumnWidths = [4, 9, 9, 14, 28, 8, 9, 9, 10];

// Pushes voucherColumnWidths into the CSS custom property .voucher-line-summary-grid reads its
// grid-template-columns from. Set on #voucherLinesContainer itself (not its innerHTML content),
// so it survives every renderVoucherLines() re-render without needing to be reapplied — called
// anyway at the top of renderVoucherLines() to stay in sync on first render.
function applyVoucherColumnWidths() {
    const container = document.getElementById('voucherLinesContainer');
    if (container) container.style.setProperty('--voucher-col-widths', voucherColumnWidths.map(w => w + '%').join(' '));
}

// Returns a single Amount value for a line's row_data — this cashbook only ever populates one
// side (Debit for CPV vouchers, Credit for CRV vouchers; the other side is always zero at
// source), so Dr/Cr is not a meaningful distinction here and the row shows one plain number.
function getVoucherLineAmount(row) {
    const debit = getVoucherLineAlias(row, VOUCHER_LINE_DEBIT_ALIASES);
    if (debit !== '—') return debit;
    const credit = getVoucherLineAlias(row, VOUCHER_LINE_CREDIT_ALIASES);
    return credit !== '—' ? credit : '';
}

// Parses an Amount display value (may contain commas/currency formatting) into a number, 0 if empty.
function parseVoucherAmount(value) {
    if (value === '' || value === undefined || value === null) return 0;
    const n = parseFloat(String(value).replace(/,/g, ''));
    return isNaN(n) ? 0 : n;
}

// Sums Amount across all of a voucher's lines, for the header subtitle and the total line.
function computeVoucherTotal(lines) {
    let total = 0;
    (lines || []).forEach(line => {
        total += parseVoucherAmount(getVoucherLineAmount(line.row_data || {}));
    });
    return total;
}

// v7 redesign: a plain colored dot instead of an emoji icon, matching the prototype's
// .statusDot. Single source for all three call sites that build this cell (initial render here,
// plus the two direct .voucher-line-status innerHTML writes in validateTransaction() and the
// Next/validate-and-advance flow) — changing it here updates all three.
function getVoucherLineStatusIcon(validationStatus) {
    const status = (validationStatus || 'Pending');
    const statusClass = status.toLowerCase();
    if (statusClass === 'pending') return '';
    return `<span class="voucher-line-status-dot ${statusClass}" title="${status}"></span>`;
}

// Suppresses the whole Pending pill (dot + text) once a line's cleared_status has been computed
// (Cleared or Query) — the Cleared/Query badge already says so, and a "pending" dot alongside it
// is a leftover from validation_status, a separate axis that hasn't caught up. The three statuses
// (Pending/Cleared/Query) are meant to be mutually exclusive on screen. Only the Pending case is
// affected; Failed/Passed pills are always shown as-is (icon + text, unchanged).
function getVoucherLineStatusPillHtml(validationStatus, clearedStatus) {
    const status = (validationStatus || 'Pending').toLowerCase();
    if (status === 'pending') {
        if (clearedStatus === 'Cleared' || clearedStatus === 'Query') return '';
        return getPendingBadgeHtml();
    }
    return `${getVoucherLineStatusIcon(validationStatus)} ${status}`.trim();
}

// Pending rendered as a badge matching the visual weight of the Cleared/Query badges
// (.voucher-line-cleared-badge) — same class/pill treatment, distinct amber palette (this app's
// existing .status-badge.pending convention) so Pending reads as the same "weight" of status
// indicator instead of the old plain dot+text leftover from validation_status.
function getPendingBadgeHtml() {
    return `<span class="voucher-line-cleared-badge pending">Pending</span>`;
}

// Validation Rules Phase 5: Cleared/Query badge, a sibling pill next to the existing
// validation-status dot+label inside .voucher-line-status — not a new column, styled on the
// existing .procedure-mismatch-badge/.status-badge.passed convention (see style.css). Renders
// nothing when cleared_status is null ("not yet computed" is a normal state, per Phase 4).
function getClearedStatusBadgeHtml(clearedStatus) {
    if (clearedStatus !== 'Cleared' && clearedStatus !== 'Query') return '';
    const cls = clearedStatus === 'Cleared' ? 'cleared' : 'query';
    return `<span class="voucher-line-cleared-badge ${cls}">${clearedStatus}</span>`;
}

// Targeted update of one line's Cleared/Query badge — a separate persistent sibling slot next to
// .voucher-line-status (not nested inside it), since validateTransaction() wholesale-rewrites
// .voucher-line-status's own innerHTML on every validation and would otherwise wipe this out.
// Mirrors updateRiskCellForLine()'s "independent sibling slot" pattern for the Risk dot.
function updateClearedStatusBadgeForLine(uid, clearedStatus) {
    const slot = document.querySelector(`.voucher-line-cleared-badge-slot[data-detail-id="${uid}"]`);
    if (slot) slot.innerHTML = getClearedStatusBadgeHtml(clearedStatus);
    // Suppress/restore the whole Pending pill alongside the badge, same rule as initial render —
    // this is the live path a save takes when cleared_status flips without a full line re-render.
    const statusSpan = document.querySelector(`.voucher-line-status-cell[data-detail-id="${uid}"] .voucher-line-status`);
    if (statusSpan) {
        const validationStatus = statusSpan.dataset.validationStatus || 'pending';
        statusSpan.innerHTML = getVoucherLineStatusPillHtml(validationStatus, clearedStatus);
    }
}

// ---------- "Why is this line Cleared/Query?" popover ----------
// The engine (compute_and_persist_cleared_status(), routers/verification.py) ANDs six independent
// gates into a bare Cleared/Query string; this popover asks the read-only
// GET /api/verification/line_status_reason/{id} endpoint which gate actually failed and shows the
// breakdown. Anchored to the whole .voucher-line-status-cell rather than the Cleared/Query badge,
// since that badge renders nothing at all for a line whose status hasn't been computed yet — the
// exact case most worth explaining. Fetched fresh on every open, never cached.
let lineStatusReasonUid = null;

// The popover element is created here rather than declared in verification.html, so this feature
// needs no markup change. Created once, reused for every line.
function getLineStatusReasonPopover() {
    let popover = document.getElementById('lineStatusReasonPopover');
    if (!popover) {
        popover = document.createElement('div');
        popover.id = 'lineStatusReasonPopover';
        popover.className = 'line-status-reason-popover';
        popover.style.display = 'none';
        document.body.appendChild(popover);
    }
    return popover;
}

function hideLineStatusReasonPopover() {
    const popover = document.getElementById('lineStatusReasonPopover');
    if (popover) popover.style.display = 'none';
    lineStatusReasonUid = null;
}

// One gate row. Shared by the Status-pill popover (buildLineStatusReasonBody, all gates) and the
// Validate panel (buildValidatePanelBody, N/A gates hidden by default) so both surfaces render a
// gate identically. showDetail defaults to true so every existing caller (the Validate panel via
// the procedure-modal adapter, which passes only g) keeps showing the explanatory text — only the
// status-pill popover passes false, per the confirmed requirement.
const LSR_GATE_ICON = { pass: '✔', fail: '✘', not_applicable: '–' };
function renderGateRowHtml(g, showDetail = true) {
    return `
        <div class="lsr-gate ${g.status}">
            <div class="lsr-gate-label"><span class="lsr-gate-icon">${LSR_GATE_ICON[g.status] || '–'}</span>${escapeHtml(g.label)}</div>
            ${g.detail && showDetail ? `<div class="lsr-gate-detail">${escapeHtml(g.detail)}</div>` : ''}
        </div>
    `;
}

function renderWorkingPaperGatesHtml(data, showDetail = true) {
    const groups = data.working_paper_gates || [];
    return groups.map(group => `
        <div class="lsr-working-gate ${group.status}">
            ${renderGateRowHtml(group, false)}
            ${showDetail && group.detail ? `<div class="lsr-gate-detail">${escapeHtml(group.detail)}</div>` : ''}
            ${showDetail && group.checks && group.checks.length ? `<div class="lsr-working-gate-details">${group.checks.map(check => renderGateRowHtml(check, true)).join('')}</div>` : ''}
        </div>
    `).join('');
}

// One row per gate. Failing gates are already ordered first by the endpoint — this only renders.
// Confirmed requirement: the status popover shows ONLY the gate name + ✅/❌ badge — the greyed
// explanatory detail lines are hidden entirely (showDetail=false). The `detail` field stays in the
// backend response (locked assumption: hiding is frontend-only), it's just not rendered here.
function buildLineStatusReasonBody(data) {
    if (data.reason) {
        return `<p class="lsr-reason">${escapeHtml(data.reason)}</p>`;
    }
    const gates = data.gates || [];
    if (!gates.length) {
        return '<p class="lsr-reason">No gate breakdown available for this line.</p>';
    }
    return data.working_paper_gates?.length
        ? renderWorkingPaperGatesHtml(data, false)
        : gates.map(g => renderGateRowHtml(g, false)).join('');
}

async function showLineStatusReasonPopover(cell, uid) {
    const popover = getLineStatusReasonPopover();
    popover.innerHTML = `
        <div class="popover-header">Why this status?</div>
        <div class="popover-body"><p class="lsr-reason">Loading…</p></div>
    `;

    // Position near the cell, clamped so it stays within the viewport — same logic as
    // showAuditGuidancePopover()/showVoucherChangePopover().
    const rect = cell.getBoundingClientRect();
    const popoverWidth = 380;
    let left = rect.left;
    if (left + popoverWidth > window.innerWidth) {
        left = Math.max(8, window.innerWidth - popoverWidth - 8);
    }
    popover.style.left = left + 'px';

    const popoverMaxHeight = 340;
    const spaceBelow = window.innerHeight - rect.bottom;
    if (spaceBelow >= popoverMaxHeight + 6) {
        popover.style.top = (rect.bottom + 6) + 'px';
        popover.style.bottom = 'auto';
    } else {
        popover.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
        popover.style.top = 'auto';
    }

    popover.style.display = 'block';
    lineStatusReasonUid = uid;

    try {
        const response = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/line_status_reason/${uid}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        // Discard a stale response if the popover was closed or switched to another line while
        // this fetch was in flight.
        if (lineStatusReasonUid !== uid) return;
        const status = data.cleared_status || 'Not computed';
        const header = popover.querySelector('.popover-header');
        if (header) header.textContent = `Why this line is ${status}`;
        const body = popover.querySelector('.popover-body');
        if (body) body.innerHTML = buildLineStatusReasonBody(data);
    } catch (e) {
        if (lineStatusReasonUid !== uid) return;
        // Fail soft — never break the table over an explanatory popover.
        console.warn('Unable to load line status reason:', e);
        const body = popover.querySelector('.popover-body');
        if (body) body.innerHTML = '<p class="lsr-reason">Unable to load the status breakdown.</p>';
    }
}

// Close on any click outside the popover (the status cell's own click stops propagating, so this
// only fires for clicks elsewhere — clicks inside the popover leave it open).
document.addEventListener('click', function(e) {
    const popover = document.getElementById('lineStatusReasonPopover');
    if (popover && popover.style.display === 'block' && !popover.contains(e.target)) {
        hideLineStatusReasonPopover();
    }
});

// The Supporting Documents modal's Validate panel (hideValidatePanel/buildValidatePanelBody/
// renderValidatePanel/toggleValidatePanel) now lives in procedure_modal.js.
// ---------- VOUCHER LINE STATE (Phase 3) ----------
// Seeds voucherLineState fresh from a voucher's lines. Accepts either a /next line object
// (has transaction_type + procedure_data + change_detection_status/old_row_data) or an
// audited_cashbook line object (has procedure_flags, no transaction_type/change-detection fields)
// — same fallback pattern navigateToTransaction() already uses for the single-active-line
// globals, applied per line here.
function seedVoucherLineState(lines) {
    voucherLineState = {};
    voucherLineDirtyRevisions = {};
    // Phase 4 (Procedures/Observations expand toggle): not persisted — every new voucher load
    // starts collapsed, same reset point as the other per-voucher state below.
    columnsExpanded = false;
    // Phase 4: each line's own change-alert diff data (undefined/absent fields for
    // audited_cashbook-sourced lines simply mean no icon shows for that line — not a regression,
    // same pre-existing gap noted for transaction_type in Phases 2/3).
    voucherLineDiffData = {};
    // Voucher Supporting-Document Linking, Phase 3: per-line Supporting Documents tab state
    // (active tab, per-tab search/selection/observation placeholders) is scoped to the currently
    // loaded voucher, same as voucherLineState/voucherLineDiffData above — reset here so a new
    // voucher never shows a stale tab set from a previously viewed line.
    procedureModalState = {};
    voucherSupportCache = null;
    voucherSupportLoading = null;
    console.debug('[voucher-support-cache] invalidated', { reason: 'voucher-changed' });
    // Phase 4: keeps the full lines array (row_data included) so openProcedureModal() can look up
    // a line's own amount (via the existing getVoucherLineAmount()/parseVoucherAmount() helpers)
    // for the selected-lines table's mismatch badge, without a second fetch.
    currentVoucherLines = lines || [];
    (lines || []).forEach(line => {
        let procedureData = line.procedure_flags ? { ...line.procedure_flags } : {};
        if (line.procedure_data) procedureData = { ...procedureData, ...line.procedure_data };
        const transactionType = line.transaction_type || procedureData.transaction_type || '';
        voucherLineState[line.detail_id] = { transactionType, procedureData, lineItems: line.line_items || [] };
        voucherLineDirtyRevisions[String(line.detail_id)] = 0;
        if (line.change_detection_status === 'updated') {
            voucherLineDiffData[line.detail_id] = { old: line.old_row_data, new: line.row_data };
        }
    });
}

// ---------- RENDER VOUCHER LINES (Phase 3) ----------
// Renders a column-title row, one row per line, and a total-line footer into
// #voucherLinesContainer. There is no expand/collapse — every field and action (Type + Guide/DFD
// chips, Amount, Supporting → Procedures chip, Status) is visible directly in the row.
// Phase C: click-to-lock focus. Toggles the dim class on the clicked row and updates
// lockedLineUid — pure DOM class manipulation, no re-render, so it can't disturb any other
// line's state (dropdown selection, etc.). Only one line can be locked at a time: locking a new
// line automatically unlocks whichever one was previously locked.
function toggleVoucherLineLock(uid) {
    const container = document.getElementById('voucherLinesContainer');
    if (!container) return;
    if (lockedLineUid === uid) {
        const row = container.querySelector(`.voucher-line-row[data-detail-id="${uid}"]`);
        if (row) row.classList.remove('voucher-line-locked');
        lockedLineUid = null;
        return;
    }
    if (lockedLineUid) {
        const prevRow = container.querySelector(`.voucher-line-row[data-detail-id="${lockedLineUid}"]`);
        if (prevRow) prevRow.classList.remove('voucher-line-locked');
    }
    const row = container.querySelector(`.voucher-line-row[data-detail-id="${uid}"]`);
    if (row) row.classList.add('voucher-line-locked');
    lockedLineUid = uid;
}

// First-landing render-timing fix: on a fresh navigation into this page, style.css can still be
// downloading/applying (and, if the browser ever gains a webfont, still swapping) at the exact
// moment the very first voucher-line table renders — the grid briefly shows overlapping headers/
// collapsed-default columns until the browser catches up. Resolves once (window 'load' + fonts.ready,
// or immediately if both are already done) and is cached, so it only actually delays the first
// renderVoucherLines() call — every later call (Prev/Next, cashbook nav, hard-refresh restore)
// awaits an already-resolved promise, effectively zero delay.
// `document.readyState === 'complete'` (or the 'load' event) only means the stylesheet has
// finished downloading — it's a microtask-speed check, not a guarantee the browser has actually
// run a layout/paint pass with that CSS applied yet. A double requestAnimationFrame forces the
// browser through one real paint cycle before resolving, closing that residual gap (this is what
// fixed the flash for one voucher type but not the other last phase — a race, not a Cash/Bank
// code-path difference; there is only one renderVoucherLines(), shared by both).
function waitForPaint() {
    return new Promise(resolve => {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
    });
}
let pageAssetsReadyPromise = null;
function pageAssetsReady() {
    if (!pageAssetsReadyPromise) {
        const loadReady = document.readyState === 'complete'
            ? Promise.resolve()
            : new Promise(resolve => window.addEventListener('load', resolve, { once: true }));
        const fontsReady = (document.fonts && document.fonts.ready) ? document.fonts.ready : Promise.resolve();
        pageAssetsReadyPromise = Promise.all([loadReady, fontsReady]).then(waitForPaint);
    }
    return pageAssetsReadyPromise;
}

async function renderVoucherLines(lines, voucherDate) {
    const container = document.getElementById('voucherLinesContainer');
    if (!container) return;
    await pageAssetsReady();
    applyVoucherColumnWidths();
    // Phase C: lock state is a transient focus aid, not saved data — reset on every new render
    // (voucher load/navigation) rather than trying to carry it across different lines/vouchers.
    lockedLineUid = null;
    if (!lines || lines.length === 0) {
        container.innerHTML = '';
        return;
    }

    // Column-title row, matching the cashbook's own column names — aligned with the summary
    // row's grid via the shared .voucher-line-summary-grid class. Each cell carries a drag-to-
    // resize handle (data-col matches voucherColumnWidths' index) — only the header row gets one,
    // per the standard spreadsheet convention of resizing from the column header, not the body.
    let html = `
        <div class="voucher-line-header-row voucher-line-summary-grid">
            <span>Sr No.<span class="voucher-col-resize-handle" data-col="0"></span></span>
            <span>Date<span class="voucher-col-resize-handle" data-col="1"></span></span>
            <span>Account Code<span class="voucher-col-resize-handle" data-col="2"></span></span>
            <span>Account Type<span class="voucher-col-resize-handle" data-col="3"></span></span>
            <span>Account Name<span class="voucher-col-resize-handle" data-col="4"></span></span>
            <span class="voucher-line-amount-col">Amount<span class="voucher-col-resize-handle" data-col="5"></span></span>
            <span>Procedures<button type="button" class="voucher-col-toggle-icon" title="Expand">⤢</button><span class="voucher-col-resize-handle" data-col="6"></span></span>
            <span>Observations<button type="button" class="voucher-col-toggle-icon" title="Expand">⤢</button><span class="voucher-col-resize-handle" data-col="7"></span></span>
            <span>Status<span class="voucher-col-resize-handle" data-col="8"></span></span>
        </div>
    `;

    // Collected during the loop below so the Type auto-fill lookups (which need real DOM nodes
    // to write into) can run once, after container.innerHTML is set.
    const lineAccountCodes = [];

    // Phase 1 of 4 (Sequential line Sr no.): idx is this line's 0-based position within the
    // current voucher's own lines array — the same order already rendered top-to-bottom, so
    // srNo = idx + 1 resets to 1 for every new voucher with no separate counter/state needed.
    lines.forEach((line, idx) => {
        const uid = line.detail_id;
        const srNo = idx + 1;
        const row = line.row_data || {};
        // Date column: prefer this specific line's own extract_date (present on lines sourced from
        // sessionTransactions — Prev/popover navigation) and fall back to the voucher-level date
        // passed in (present on lines sourced from get_next_transaction/voucher_nav, which don't
        // carry a per-line extract_date) — same fallback renderVoucherHeader() already uses.
        const lineDate = line.extract_date || voucherDate || '—';
        const accountCode = getVoucherLineAlias(row, VOUCHER_LINE_ACCOUNT_CODE_ALIASES);
        lineAccountCodes.push({
            uid, accountCode,
            categoryIdApplied: line.category_id_applied,
            categoryNameApplied: line.category_name_applied
        });
        const accountName = getVoucherLineAlias(row, VOUCHER_LINE_ACCOUNT_NAME_ALIASES);
        const description = getVoucherLineAlias(row, VOUCHER_LINE_DESCRIPTION_ALIASES);
        const amount = getVoucherLineAmount(row);
        // Phase 4: validation-status color, moved here from the removed .amount-row (same
        // status-pending/failed/passed convention as before), now driving the status cell's color.
        const validationStatus = (line.validation_status || 'Pending').toLowerCase();
        // Change-alert icon (updated) / deleted-badge chip — functional signals, kept inline next
        // to the account name/description. Only 'updated' lines have diff data in voucherLineDiffData.
        const changeIconHtml = voucherLineDiffData[uid]
            ? `<span class="voucher-line-change-icon" id="voucherChangeIcon-${uid}" title="Changed on re-upload">⚠️</span>`
            : '';
        const deletedBadgeHtml = line.change_detection_status === 'deleted'
            ? `<span class="voucher-line-deleted-badge" title="Missing from latest re-upload">🗑️ Deleted</span>`
            : '';

        html += `
            <div class="voucher-line-row" data-detail-id="${uid}">
                <div class="voucher-line-summary voucher-line-summary-grid" data-detail-id="${uid}">
                    <span class="voucher-line-srno">${srNo}</span>
                    <span class="voucher-line-date">${lineDate}</span>
                    <span class="voucher-line-code">${accountCode}</span>
                    <span class="voucher-line-type-cell">
                        <span class="voucher-line-type-value-row">
                            <span class="voucher-line-type-value" data-uid="${uid}">Loading…</span>
                            <span class="voucher-line-type-recycle-icon" data-uid="${uid}" data-account-code="${escapeHtml(accountCode)}" title="No category assigned — click to assign one in Assign Categories" style="display:none">🔄</span>
                            <span class="voucher-line-type-lock-icon" data-uid="${uid}" title="Category locked — this line was already audited under it; a later Trial Balance change won't affect it" style="display:none; font-size:12px; opacity:0.55;">🔒</span>
                        </span>
                        <span class="voucher-line-type-chips" data-uid="${uid}">
                            <button type="button" class="voucher-line-learn-icon voucher-line-audit-icon" data-uid="${uid}" title="Audit guidance">📋</button>
                            <button type="button" class="voucher-line-learn-icon voucher-line-dfd-chip" data-uid="${uid}" title="Data Flow Diagram">🔀</button>
                        </span>
                    </span>
                    <span class="voucher-line-name-cell">
                        <span class="voucher-line-name">${accountName}${changeIconHtml}${deletedBadgeHtml}</span>
                        <span class="voucher-line-desc">${description}</span>
                    </span>
                    <span class="voucher-line-amount">${amount}</span>
                    <span class="voucher-line-procedures-cell" data-detail-id="${uid}">
                        <span class="voucher-line-procedures-slot" data-detail-id="${uid}"></span>
                        <span class="voucher-line-risk-dot" data-uid="${uid}"></span>
                    </span>
                    <span class="voucher-line-observations-cell" data-detail-id="${uid}"></span>
                    <span class="voucher-line-status-cell voucher-line-status-clickable" data-detail-id="${uid}" title="Click to see why this line is Cleared or Query">
                        <span class="voucher-line-status status-${validationStatus}" data-validation-status="${validationStatus}">${getVoucherLineStatusPillHtml(line.validation_status, line.cleared_status)}</span>
                        <span class="voucher-line-cleared-badge-slot" data-detail-id="${uid}">${getClearedStatusBadgeHtml(line.cleared_status)}</span>
                    </span>
                </div>
            </div>
        `;
    });

    // Total line: cashbook name in the description slot, summed Amount — see
    // computeVoucherTotal(). Not a real line (no detail-id, not expandable).
    const cashbookName = window._currentCashbook || selectedCashbook || '';
    const total = computeVoucherTotal(lines);
    html += `
        <div class="voucher-line-total-row voucher-line-summary-grid">
            <span></span>
            <span></span>
            <span></span>
            <span></span>
            <span class="voucher-line-name-cell">
                <span class="voucher-line-name">Total</span>
                <span class="voucher-line-desc">${cashbookName}</span>
            </span>
            <span class="voucher-line-amount">${total}</span>
            <span></span>
            <span></span>
            <span></span>
        </div>
    `;

    container.innerHTML = html;

    // Type auto-fill (Verification ↔ Assign Categories link, Phase 4): look up each line's Category
    // by its account code and populate the read-only Type cell — replaces the old dropdown-sync
    // loop that read a manually-saved value. Each call safely no-ops if its row is no longer in the
    // DOM by the time the lookup resolves.
    // Processing/Loading UI, Phase 6: the voucher-loading badge (next to the title, shown by
    // renderVoucherHeader()'s paired call) tracks this whole batch via the promises these calls now
    // return — myLoadToken guards against a stale (superseded-by-a-newer-voucher) batch hiding the
    // badge out from under the voucher that's actually on screen now.
    const myLoadToken = voucherLoadToken;
    showVoucherLoadingBadge();
    // Freeze Category on voucher lines: a line whose category is already frozen renders directly
    // from the already-loaded voucher data - no network call - and skips straight to the locked
    // state; only a not-yet-frozen line goes through the live category-for-account lookup.
    Promise.all(lineAccountCodes.map(({ uid, accountCode, categoryIdApplied, categoryNameApplied }) =>
        categoryIdApplied != null
            ? applyCategoryToTypeCell(uid, categoryNameApplied, { frozen: true })
            : autoFillCategoryForLine(uid, accountCode)
    )).then(() => {
        if (myLoadToken === voucherLoadToken) hideVoucherLoadingBadge();
    });

    // Phase 4: attach each line's change-alert icon (only present for lines flagged 'updated').
    lines.forEach(line => {
        const uid = line.detail_id;
        setupVoucherChangeIcon(uid);
    });

    // ----- Event wiring (rebound after every innerHTML write, matching this file's convention) -----

    // Phase C: click-to-lock/dim focus. Every interactive control inside a row (type dropdown,
    // Guide/DFD chips, procedure links, resize handles — wired below) already calls
    // e.stopPropagation() in its own click handler, so this row-level listener only ever fires for
    // clicks that land on blank row space / account name / amount / status — it never competes
    // with or gates any of those controls, which stay fully clickable regardless of lock state.
    container.querySelectorAll('.voucher-line-row').forEach(row => {
        row.addEventListener('click', function() {
            toggleVoucherLineLock(this.dataset.detailId);
        });
    });

    // Recycle-arrow icon (Verification ↔ Assign Categories link, Phase 4) — visible only on
    // unassigned lines (toggled by applyCategoryToTypeCell()). Opens Assign Categories in a new
    // tab so the current Verification session stays untouched, carrying client_id + account_code
    // forward as query params. Phase 5 (not yet built) is what makes audit_rules.html/.js actually
    // read those params and jump straight to that account — this phase only emits the link.
    container.querySelectorAll('.voucher-line-type-recycle-icon').forEach(icon => {
        icon.addEventListener('click', function(e) {
            e.stopPropagation();
            const accountCode = this.dataset.accountCode;
            const clientId = getClientId();
            const params = new URLSearchParams({ client_id: clientId, account_code: accountCode });
            window.open(`/static/audit_rules.html?${params}`, '_blank');
        });
    });

    // "Guide" chip (below the Type dropdown) — opens the audit guidance content for this line's
    // type in a small popover anchored to the chip (not a full-page modal), so the chip stays
    // clickable to toggle it closed.
    container.querySelectorAll('.voucher-line-audit-icon').forEach(icon => {
        icon.addEventListener('click', function(e) {
            e.stopPropagation();
            const uid = this.dataset.uid;
            if (auditGuidancePopoverUid === uid) {
                hideAuditGuidancePopover();
            } else {
                showAuditGuidancePopover(this, uid);
            }
        });
    });

    // "DFD" chip (below the Type dropdown) — reuses the existing Data Flow Diagram placeholder
    // modal (openDfdModal(), same one this app already has elsewhere).
    container.querySelectorAll('.voucher-line-dfd-chip').forEach(chip => {
        chip.addEventListener('click', function(e) {
            e.stopPropagation();
            if (typeof openDfdModal === 'function') openDfdModal();
        });
    });

    // Status cell → "why is this Cleared/Query?" popover ("Why is this line Query?" fix).
    // Wired here per render alongside every other row control rather than as a document-level
    // delegated listener: the row itself has a click-to-lock handler that fires first on bubble,
    // so this needs its own e.stopPropagation() to open the popover without also toggling the
    // line lock — the same pattern every other in-row control in this loop already uses.
    container.querySelectorAll('.voucher-line-status-clickable').forEach(cell => {
        cell.addEventListener('click', function(e) {
            e.stopPropagation();
            const uid = this.dataset.detailId;
            if (lineStatusReasonUid === uid) {
                hideLineStatusReasonPopover();
            } else {
                showLineStatusReasonPopover(this, uid);
            }
        });
    });

    // Procedures/Observations column expand toggle (Phase 4): either header icon flips the one
    // shared columnsExpanded state; sync icon glyphs to whatever it currently is on every render.
    container.querySelectorAll('.voucher-col-toggle-icon').forEach(icon => {
        icon.addEventListener('click', function(e) {
            e.stopPropagation();
            toggleColumnsExpanded();
        });
    });
    updateColumnToggleIcons();

    // Procedures/Observations cells — populated asynchronously per line
    // (updateProceduresCellForLine()/updateObservationsCellForLine()/updateRiskCellForLine(), see
    // above), since they depend on a fetch of the line's currently-selected Category. That
    // function wires each of its own links' click listeners once its content lands, since this
    // batch pass runs before any of those async fetches resolve.

    // Column drag-to-resize handles (header row only). Excel/Sheets-style: dragging a column's
    // border only resizes that column — no neighbor compensation. Widths are percentages of the
    // container, so the drag delta (raw mouse pixels) is converted to a percentage of the
    // container's own rendered width; the total can end up over 100%, in which case
    // #voucherLinesContainer's overflow-x is the fallback. Widths live in voucherColumnWidths
    // (module scope), so they persist across re-renders/voucher navigation.
    container.querySelectorAll('.voucher-col-resize-handle').forEach(handle => {
        handle.addEventListener('mousedown', function(e) {
            e.preventDefault();
            e.stopPropagation();
            const colIndex = parseInt(this.dataset.col);
            const startX = e.clientX;
            const startWidth = voucherColumnWidths[colIndex];
            const containerWidth = container.getBoundingClientRect().width;
            this.classList.add('resizing');

            // Small floating "Width: N%" readout that follows the cursor while dragging, same
            // idea as Excel's own column-resize tooltip — created fresh per drag, removed on
            // mouseup so it never lingers in the DOM.
            const tooltip = document.createElement('div');
            tooltip.className = 'voucher-col-resize-tooltip';
            tooltip.textContent = `Width: ${startWidth}%`;
            tooltip.style.left = `${e.clientX + 12}px`;
            tooltip.style.top = `${e.clientY - 28}px`;
            document.body.appendChild(tooltip);

            const onMouseMove = (e2) => {
                const deltaPercent = ((e2.clientX - startX) / containerWidth) * 100;
                const newWidth = Math.round(Math.max(3, startWidth + deltaPercent) * 10) / 10;
                voucherColumnWidths[colIndex] = newWidth;
                applyVoucherColumnWidths();
                tooltip.textContent = `Width: ${newWidth}%`;
                tooltip.style.left = `${e2.clientX + 12}px`;
                tooltip.style.top = `${e2.clientY - 28}px`;
            };
            const onMouseUp = () => {
                handle.classList.remove('resizing');
                tooltip.remove();
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
            };
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
    });
}

// Phase 2: renderLineItems(), addLineItemRow(), and fetchLibrary() were standalone-only functions
// used exclusively by the now-deleted renderTransaction() single-transaction view. They are removed
// to avoid leaving dead code — no callers remain anywhere in the codebase.

// ---------- SEGMENT 2 AUDIT TAB: fetch + render guidance for currentTransactionType ----------
function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

// Phase 9 (Audit Rules): guidance content now comes from a Category's kb_overview, attached
// Documents, and attached Cross-checks (GET /api/verification/audit_guidance), replacing the old
// three-column management_controls/audit_risks/audit_procedures text. Cross-checks are shown as
// read-only guidance text only — no automated comparison against real voucher data (deferred,
// separate future requirement).
function buildAuditGuidanceTable(data) {
    const overviewHtml = data.kb_overview
        ? `<div style="margin-bottom:10px; padding:8px 10px; background:#f8fafc; border-radius:6px; font-size:13px; color:#334155; white-space:pre-line;">${escapeHtml(data.kb_overview)}</div>`
        : '';
    const documentsHtml = (data.documents && data.documents.length)
        ? data.documents.map(doc => {
            const roles = (doc.auth_roles || []).join(', ');
            return `<li><strong>${escapeHtml(doc.name)}</strong>${roles ? ` <span style="color:#94a3b8; font-size:12px;">(${escapeHtml(roles)})</span>` : ''}</li>`;
        }).join('')
        : '<li style="color:#94a3b8;">No documents attached to this category yet.</li>';
    const crossChecksHtml = (data.cross_checks && data.cross_checks.length)
        ? data.cross_checks.map(cc => {
            const chain = (cc.documents || []).join(' → ');
            return `<li>Check: ${escapeHtml(cc.compare_field)} — ${escapeHtml(cc.comparison_type)}${chain ? ` across ${escapeHtml(chain)}` : ''}</li>`;
        }).join('')
        : '<li style="color:#94a3b8;">No cross-checks attached to this category yet.</li>';
    return `
        ${overviewHtml}
        <div class="audit-guidance-columns">
            <div class="audit-guidance-col">
                <div class="audit-guidance-col-header">Documents</div>
                <ul>${documentsHtml}</ul>
            </div>
            <div class="audit-guidance-col">
                <div class="audit-guidance-col-header">Cross-checks</div>
                <ul>${crossChecksHtml}</ul>
            </div>
        </div>
    `;
}

// buildAuditGuidanceTable()/escapeHtml() are now consumed only by showAuditGuidancePopover().

// Header voucher-wise stats (Phase 2 of 3): fetches Total/Cleared/Query/Pending for the whole
// cashbook session and patches the four #voucherStat* spans in place (no full header re-render,
// so #cashbookReturnBtn's listener isn't redundantly rebound). Called once on session load
// (activateSessionView()) and from updateProgressDisplay() below — the latter already runs after
// every save/verify/navigation action, which is what keeps these counts live through a work
// session rather than freezing at whatever they were when the session was first opened. Fails
// soft (console.warn only, cache left as-is) — same convention as this file's other non-fatal
// background fetches, e.g. refreshDateFormatPreview().
async function refreshVoucherStats() {
    if (!currentSessionId) return;
    try {
        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/session/${currentSessionId}/voucher_stats`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        voucherStatsCache = await res.json();
        const totalEl = document.getElementById('voucherStatTotal');
        const clearedEl = document.getElementById('voucherStatCleared');
        const queryEl = document.getElementById('voucherStatQuery');
        const pendingEl = document.getElementById('voucherStatPending');
        if (totalEl) totalEl.textContent = voucherStatsCache.total;
        if (clearedEl) clearedEl.textContent = voucherStatsCache.cleared;
        if (queryEl) queryEl.textContent = voucherStatsCache.query;
        if (pendingEl) pendingEl.textContent = voucherStatsCache.pending;
    } catch (e) {
        console.warn('Could not fetch voucher stats for header:', e);
    }
}

// Update the voucher position counter (small " · Voucher X of Y" suffix on the header subtitle,
// written by renderVoucherHeader()).
async function updateProgressDisplay() {
    refreshVoucherStats();
    const counterEl = document.getElementById('voucherPositionCounter');
    if (counterEl) {
        if (!currentSessionId) {
            counterEl.textContent = '';
        } else {
            // Compute voucher-based position from sessionTransactions' unique voucher_ids,
            // rather than the old line-level currentTransactionIndex / totalTransactions.
            const uniqueVouchers = [...new Set(sessionTransactions.map(t => t.voucher_id).filter(Boolean))];
            let voucherIndex = -1;
            let voucherTotal = uniqueVouchers.length;
            if (currentVoucherId && voucherTotal > 0) {
                voucherIndex = uniqueVouchers.indexOf(currentVoucherId);
            }
            if (voucherTotal === 0) {
                // Fallback: if no voucher list yet (e.g. first load), use line-level values.
                let index = currentTransactionIndex;
                let total = totalTransactions;
                if (total === 0) {
                    try {
                        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/session/${currentSessionId}/progress`);
                        if (res.ok) {
                            const data = await res.json();
                            total = data.total_transactions || 0;
                            totalTransactions = total;
                        }
                    } catch (e) {
                        console.warn('Could not fetch progress for header:', e);
                    }
                }
                const displayIndex = (index >= 0) ? index + 1 : '?';
                counterEl.textContent = total > 0 ? ` · Voucher ${displayIndex} of ${total}` : '';
            } else {
                const displayIndex = (voucherIndex >= 0) ? voucherIndex + 1 : '?';
                counterEl.textContent = ` · Voucher ${displayIndex} of ${voucherTotal}`;
            }
        }
    }
}
// ---------- Phase 3: Build diff rows for change detection banner ----------
function buildDiffRows(oldData, newData) {
    if (!oldData || !newData) return '<tr><td colspan="3" style="color:#94a3b8; text-align:center;">No previous data available.</td></tr>';
    const allFields = new Set([...Object.keys(oldData), ...Object.keys(newData)]);
    let rows = '';
    allFields.forEach(field => {
        const oldVal = oldData[field] !== undefined ? oldData[field] : '';
        const newVal = newData[field] !== undefined ? newData[field] : '';
        const oldStr = String(oldVal).trim();
        const newStr = String(newVal).trim();
        if (oldStr === newStr) {
            rows += `<tr><td class="field-label">${field}</td><td class="same-value" colspan="2">${newStr || '—'}</td></tr>`;
        } else {
            rows += `<tr><td class="field-label">${field}</td><td class="old-value">${oldStr || '—'}</td><td class="new-value">${newStr || '—'}</td></tr>`;
        }
    });
    return rows;
}

// ---------- Phase 4 (was Phase 8, now per-line): Voucher ID change-alert popover ----------
// Holds each line's own old/new row data, keyed by detail_id (uid). Seeded in
// seedVoucherLineState() from that line's change_detection_status/old_row_data/row_data — no
// fetch needed, the diff data is already loaded per line.
let voucherLineDiffData = {};
// Tracks whether the popover is currently open, mirroring databank.js's
// activeHistoryIcon so the outside-click handler knows when to act.
let voucherChangePopoverOpen = false;

function hideVoucherChangePopover() {
    const popover = document.getElementById('voucherChangePopover');
    if (popover) popover.style.display = 'none';
    voucherChangePopoverOpen = false;
}

function showVoucherChangePopover(icon, uid) {
    const popover = document.getElementById('voucherChangePopover');
    const diffData = voucherLineDiffData[uid];
    if (!popover || !diffData) return;

    popover.innerHTML = `
        <div class="popover-header">Changed on Re-upload</div>
        <div class="popover-body">
            <table class="diff-table">
                <thead>
                    <tr><th>Field</th><th>Old Value</th><th>New Value</th></tr>
                </thead>
                <tbody>
                    ${buildDiffRows(diffData.old, diffData.new)}
                </tbody>
            </table>
        </div>
    `;

    // Position near the icon, clamped so it stays within the viewport
    // (same logic as showRowHistoryPopover in databank.js).
    const rect = icon.getBoundingClientRect();
    const popoverWidth = 360;
    let left = rect.left;
    if (left + popoverWidth > window.innerWidth) {
        left = Math.max(8, window.innerWidth - popoverWidth - 8);
    }
    popover.style.left = left + 'px';

    const popoverMaxHeight = 320;
    const spaceBelow = window.innerHeight - rect.bottom;
    if (spaceBelow >= popoverMaxHeight + 6) {
        popover.style.top = (rect.bottom + 6) + 'px';
        popover.style.bottom = 'auto';
    } else {
        popover.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
        popover.style.top = 'auto';
    }

    popover.style.display = 'block';
    voucherChangePopoverOpen = true;
}

// Attach one line's change-alert icon click listener (called per line from renderVoucherLines()).
function setupVoucherChangeIcon(uid) {
    const icon = document.getElementById(`voucherChangeIcon-${uid}`);
    if (!icon) return;
    icon.addEventListener('click', function(e) {
        e.stopPropagation();
        if (voucherChangePopoverOpen) {
            hideVoucherChangePopover();
        } else {
            showVoucherChangePopover(icon, uid);
        }
    });
}

// Close the popover when clicking anywhere outside it (the icon's own click
// is stopped from propagating here, so this only fires for clicks elsewhere).
document.addEventListener('click', function(e) {
    const popover = document.getElementById('voucherChangePopover');
    if (popover && popover.style.display === 'block' && !popover.contains(e.target)) {
        hideVoucherChangePopover();
    }
});

// ---------- Audit Guidance popover ("Guide" chip) ----------
// Small popover anchored to the "Guide" chip below each line's Type dropdown — fetches audit
// guidance for that line's transaction type as a lightweight popover instead of a full-page
// modal, so the chip stays clickable to toggle it closed (a full-screen modal's backdrop would
// otherwise cover it). Tracks the open line's uid (not just a boolean) so clicking a different
// line's chip switches content instead of needing a close-then-reopen.
let auditGuidancePopoverUid = null;

function hideAuditGuidancePopover() {
    const popover = document.getElementById('auditGuidancePopover');
    if (popover) popover.style.display = 'none';
    auditGuidancePopoverUid = null;
}

async function showAuditGuidancePopover(icon, uid) {
    const popover = document.getElementById('auditGuidancePopover');
    if (!popover) return;
    const state = voucherLineState[uid];
    const type = state ? state.transactionType : '';
    // No transaction type picked yet for this line — do nothing, same as the expanded panel's
    // Audit Guidance button.
    if (!type) return;

    popover.innerHTML = `
        <div class="popover-header">📋 Audit Guidance</div>
        <div class="popover-body"><p class="segment2-placeholder-text">Loading audit guidance...</p></div>
    `;

    // Position near the icon, clamped so it stays within the viewport (same logic as
    // showVoucherChangePopover()/showRowHistoryPopover() in databank.js).
    const rect = icon.getBoundingClientRect();
    const popoverWidth = 480;
    let left = rect.left;
    if (left + popoverWidth > window.innerWidth) {
        left = Math.max(8, window.innerWidth - popoverWidth - 8);
    }
    popover.style.left = left + 'px';

    const popoverMaxHeight = 360;
    const spaceBelow = window.innerHeight - rect.bottom;
    if (spaceBelow >= popoverMaxHeight + 6) {
        popover.style.top = (rect.bottom + 6) + 'px';
        popover.style.bottom = 'auto';
    } else {
        popover.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
        popover.style.top = 'auto';
    }

    popover.style.display = 'block';
    auditGuidancePopoverUid = uid;

    const body = popover.querySelector('.popover-body');
    try {
        const data = await fetchAuditGuidanceData(type);
        // Discard a stale response if the popover was closed or switched to another line/type
        // while the fetch was in flight.
        if (auditGuidancePopoverUid !== uid) return;
        if (!data) {
            if (body) body.innerHTML = '<p class="segment2-placeholder-text">No guidance available for this category yet.</p>';
            return;
        }
        if (body) body.innerHTML = buildAuditGuidanceTable(data);
    } catch (e) {
        if (auditGuidancePopoverUid !== uid) return;
        console.error('Error fetching audit guidance:', e);
        if (body) body.innerHTML = '<p class="segment2-placeholder-text">Unable to load audit guidance.</p>';
    }
}

// Close the popover when clicking anywhere outside it (the icon's own click is stopped from
// propagating here, so this only fires for clicks elsewhere — including scrolling/clicking
// inside the popover itself, which .contains(e.target) correctly leaves open).
document.addEventListener('click', function(e) {
    const popover = document.getElementById('auditGuidancePopover');
    if (popover && popover.style.display === 'block' && !popover.contains(e.target)) {
        hideAuditGuidancePopover();
    }
});

// ==============================================
// PHASE 2 – TRANSACTION LIST POPOVER
// ==============================================

let sessionTransactions = [];
let currentTransactionIndex = -1;
// Client-side filter term for the voucher list popover's search box (#transactionListSearchInput).
// Cleared automatically by closeTransactionPopover() so reopening the popover later always starts
// with the full list rather than a stale filter.
let transactionSearchTerm = '';

// forceRefresh (Session date-range reconciliation, Phase 2 of 4): bypasses the "already loaded,
// skip re-fetch" guard below - needed after reconcileActiveSessionRange() changes what's active
// server-side, since sessionTransactions being non-empty from before the reconcile would otherwise
// make this a no-op and silently keep showing the pre-reconcile voucher list.
async function loadTransactionList(showPopover = false, forceRefresh = false) {
    if (!currentSessionId) {
        showStatus('No active session.', 'error');
        return;
    }

    // If already loaded and we want to show popover, just render it
    if (sessionTransactions.length > 0 && !forceRefresh) {
        if (showPopover) {
            renderTransactionPopover();
        }
        return;
    }

    // Fetch all transactions for this session
    try {
        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/audited_cashbook/${currentSessionId}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch transaction list');
        }
        const data = await res.json();
        sessionTransactions = data.transactions || [];
        // Header format cleanup: keep the header's date-range text in sync with the session's
        // real current range (this endpoint already returns it - previously discarded).
        if (data.session) {
            currentSessionPeriodStart = data.session.period_start || null;
            currentSessionPeriodEnd = data.session.period_end || null;
        }
        // Also store the current index based on currentDetailId
        if (currentDetailId) {
            currentTransactionIndex = sessionTransactions.findIndex(t => t.detail_id === currentDetailId);
        }
        // Update totalTransactions: normal load only ever grows it from 0 (first load); a forced
        // refresh always recomputes it, since a reconcile can shrink the active set too.
        if (forceRefresh) {
            totalTransactions = sessionTransactions.length;
        } else if (totalTransactions === 0 && sessionTransactions.length > 0) {
            totalTransactions = sessionTransactions.length;
        }
        if (showPopover) {
            renderTransactionPopover();
        }
        // Update the header title now that we have data
        updateProgressDisplay();
    } catch (error) {
        console.error('Error loading transaction list:', error);
        showStatus('❌ Failed to load transaction list: ' + error.message, 'error');
    }
}

// Builds the Status cell shared by every transaction row in the popover — reuses the exact same
// helpers the main voucher table's Status column already uses (getClearedStatusBadgeHtml() for
// Cleared/Query, getPendingBadgeHtml() for the Pending badge) so this popover never drifts out
// of sync with what the main table actually shows, per the 2026-08-09/2026-08-10 cutover from the
// legacy validation_status field to cleared_status.
function buildPopoverStatusHtml(clearedStatus) {
    const badge = getClearedStatusBadgeHtml(clearedStatus);
    if (badge) return badge;
    return getPendingBadgeHtml();
}

// Returns the subset of sessionTransactions whose Voucher ID, Date, Account Name, Amount, or
// Cleared/Query/Pending status contains the search term (case-insensitive substring match).
// Reuses the same field-extraction logic renderTransactionPopover() uses per line
// (getVoucherLineAmount(), the same rowData account-name fallback chain, the same
// default-to-'Pending' status) so results are guaranteed consistent with what's actually
// displayed in the table.
function filterTransactionsForPopover(term) {
    const needle = term.trim().toLowerCase();
    if (!needle) return sessionTransactions;
    return sessionTransactions.filter(t => {
        const rowData = t.row_data || {};
        const accountName = rowData['Account Name'] || rowData['Account'] || rowData['Ledger'] || '';
        const amount = getVoucherLineAmount(rowData);
        const status = t.cleared_status || 'Pending';
        const haystack = [
            t.voucher_id || '',
            t.extract_date || '',
            accountName,
            amount,
            status
        ].join(' ').toLowerCase();
        return haystack.includes(needle);
    });
}

function renderTransactionPopover() {
    const popover = document.getElementById('transactionListPopover');
    const tbody = document.getElementById('transactionListBody');
    const totalSpan = document.getElementById('popoverTotal');
    if (!popover || !tbody) return;

    // Filter over the already-loaded session data — no new backend call. transactionSearchTerm
    // is updated by the #transactionListSearchInput 'input' listener wired below.
    const filteredTransactions = transactionSearchTerm ? filterTransactionsForPopover(transactionSearchTerm) : sessionTransactions;
    const count = filteredTransactions.length;

    if (sessionTransactions.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; color:#94a3b8;">No transactions found.</td></tr>`;
        if (totalSpan) totalSpan.textContent = '';
        popover.style.display = 'block';
        return;
    }

    if (count === 0) {
        // Distinct from the "No transactions found." state above — the session has data, the
        // current search term just doesn't match any of it.
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; color:#94a3b8;">No transactions match your search.</td></tr>`;
        if (totalSpan) totalSpan.textContent = 'Total vouchers: 0 | Total: 0 transactions';
        popover.style.display = 'block';
        return;
    }

    // Group transactions by Voucher ID, preserving first-seen order (the array is usually
    // already contiguous by voucher, but this doesn't assume it).
    const groupOrder = [];
    const groupsByVoucherId = {};
    filteredTransactions.forEach(t => {
        const vid = t.voucher_id || '—';
        if (!groupsByVoucherId[vid]) {
            groupsByVoucherId[vid] = [];
            groupOrder.push(vid);
        }
        groupsByVoucherId[vid].push(t);
    });

    // Header: "Total vouchers: X | Total: Y transactions", both from the current dataset.
    if (totalSpan) {
        totalSpan.textContent = `Total vouchers: ${groupOrder.length} | Total: ${count} transaction${count > 1 ? 's' : ''}`;
    }

    let html = '';
    groupOrder.forEach((vid, groupIndex) => {
        const lines = groupsByVoucherId[vid];
        const isMultiLine = lines.length > 1;
        const groupDate = lines[0].extract_date || '—';
        // Voucher-level serial number (1-based), replacing the old per-line S.No.
        const voucherSerial = groupIndex + 1;
        // Phase E: highlight is per-voucher (whole block), not per-line — replaces the old
        // isCurrent = (t.detail_id === currentDetailId) check, which only ever shaded a single
        // line and never the group header, so a multi-line voucher never read as "active" as a
        // whole. currentVoucherId is already kept in sync by loadNextTransaction()/
        // moveToAdjacentVoucher()/navigateToTransaction().
        const isActiveVoucher = (vid === currentVoucherId);

        if (isMultiLine) {
            html += `<tr class="voucher-group-header-row${isActiveVoucher ? ' popover-voucher-active' : ''}">
                <td colspan="5">${voucherSerial}. <strong>Voucher ${vid}</strong> &nbsp;·&nbsp; ${groupDate}</td>
            </tr>`;
        }

        let subtotal = 0;
        lines.forEach(t => {
            const rowData = t.row_data || {};
            const accountName = rowData['Account Name'] || rowData['Account'] || rowData['Ledger'] || '—';
            const amount = getVoucherLineAmount(rowData);
            subtotal += parseVoucherAmount(amount);
            // Multi-line vouchers already show Voucher ID/Date on the group header row above,
            // so line rows leave those two cells blank instead of repeating the same value.
            // Single-line vouchers have no header row, so their serial number is prefixed
            // directly onto the Voucher ID cell here instead.
            const voucherCell = isMultiLine ? '' : `${voucherSerial}. ${t.voucher_id || '—'}`;
            const dateCell = isMultiLine ? '' : (t.extract_date || '—');
            html += `<tr data-detail-id="${t.detail_id}" class="${isActiveVoucher ? 'popover-voucher-active' : ''}">
                <td>${voucherCell}</td>
                <td>${dateCell}</td>
                <td>${accountName}</td>
                <td style="text-align:right;">${amount !== '' ? amount : '—'}</td>
                <td style="text-align:center;">${buildPopoverStatusHtml(t.cleared_status)}</td>
            </tr>`;
        });

        if (isMultiLine) {
            html += `<tr class="voucher-group-subtotal-row">
                <td></td>
                <td></td>
                <td>Subtotal</td>
                <td style="text-align:right;">${subtotal}</td>
                <td></td>
            </tr>`;
        }

        // Spacer between groups (not before the first one) for visual breathing room.
        if (groupIndex < groupOrder.length - 1) {
            html += `<tr class="voucher-group-spacer"><td colspan="5"></td></tr>`;
        }
    });

    tbody.innerHTML = html;

    // Add click listeners to rows
    tbody.querySelectorAll('tr[data-detail-id]').forEach(row => {
        row.addEventListener('click', function() {
            const detailId = parseInt(this.dataset.detailId);
            if (detailId) {
                navigateToTransaction(detailId);
            }
        });
    });

    popover.style.display = 'block';
}

function closeTransactionPopover() {
    const popover = document.getElementById('transactionListPopover');
    if (popover) popover.style.display = 'none';
    // Reset the search filter on close so reopening the popover later (e.g. after navigating to
    // a different voucher) always starts with the full list instead of a stale filter.
    transactionSearchTerm = '';
    const searchInput = document.getElementById('transactionListSearchInput');
    if (searchInput) searchInput.value = '';
}

// Wire the popover search input once — element is static markup (verification.html), never
// rebuilt, so this listener is set up alongside popoverCloseBtn below rather than re-attached on
// every renderTransactionPopover() call. No debounce: this filters already-loaded in-memory data,
// not a backend query (same convention as template-viewer.js's own client-side search).
const transactionListSearchInput = document.getElementById('transactionListSearchInput');
if (transactionListSearchInput) {
    transactionListSearchInput.addEventListener('input', function() {
        transactionSearchTerm = this.value;
        renderTransactionPopover();
    });
}
// Close popover on outside click and close button
document.addEventListener('click', function(e) {
    const popover = document.getElementById('transactionListPopover');
    if (!popover) return;
    if (popover.style.display === 'block') {
        // Check if click is inside popover or on the mirror icon
        const icon = document.getElementById('transactionListIcon');
        if (popover.contains(e.target) || (icon && icon.contains(e.target))) {
            return;
        }
        closeTransactionPopover();
    }
});

// Also wire the close button in the popover header
const popoverCloseBtn = document.querySelector('#transactionListPopover .popover-header .close-btn');
if (popoverCloseBtn) {
    popoverCloseBtn.addEventListener('click', closeTransactionPopover);
}
function navigateToTransaction(detailId) {
    const transaction = sessionTransactions.find(t => t.detail_id === detailId);
    if (!transaction) {
        showStatus('Transaction not found.', 'error');
        return;
    }

    // Update current state
    currentDetailId = detailId;
    currentClientDataId = transaction.client_data_id;
    currentVoucherId = transaction.voucher_id;
    setVerificationUrlVoucher(currentVoucherId);
    currentTransactionType = transaction.transaction_type || '';
    lineItems = transaction.line_items || [];
    procedureData = transaction.procedure_flags ? transaction.procedure_flags : {};
    // Also merge procedure_data if exists
    if (transaction.procedure_data) {
        procedureData = { ...procedureData, ...transaction.procedure_data };
    }
    // Find index
    currentTransactionIndex = sessionTransactions.findIndex(t => t.detail_id === detailId);

    // Phase 2: keep the voucher header bar in sync when navigating via Prev/popover — these
    // paths step through the flat per-line sessionTransactions list, not /next, so the voucher's
    // lines are derived by matching voucher_id within that already-loaded list.
    const voucherLines = sessionTransactions.filter(t => t.voucher_id === transaction.voucher_id);
    renderVoucherHeader(transaction.voucher_id, transaction.extract_date, voucherLines);
    seedVoucherLineState(voucherLines);
    renderVoucherLines(voucherLines, transaction.extract_date);

    // Cleanup pass: the standalone renderTransaction(transaction) card is gone — the voucher
    // header bar and per-line rows (above) are the only rendering now.
    updateProgressDisplay();
    closeTransactionPopover();

    // Enable/disable navigation buttons (Phase 3 will handle this fully)
    // For now, we just update the UI
}
// Save current transaction without validation (for going back). Phase 4: saves every line in the
// voucher (not just the active one), so edits on any expanded line survive navigation.
async function saveCurrentTransaction() {
    await saveAllVoucherLines();
    // Optionally, we could also validate but not mark as verified
    // For now, just save.
}

// Show confirmation modal and return a promise that resolves to true (proceed) or false (cancel)
function showNavigationConfirm() {
    return new Promise((resolve) => {
        const modal = document.getElementById('navConfirmModal');
        const cancelBtn = document.getElementById('navConfirmCancel');
        const okBtn = document.getElementById('navConfirmOk');
        if (!modal) {
            resolve(true); // fallback if modal missing
            return;
        }
        modal.style.display = 'flex';
        const cleanup = () => {
            modal.style.display = 'none';
            cancelBtn.removeEventListener('click', onCancel);
            okBtn.removeEventListener('click', onOk);
        };
        const onCancel = () => { cleanup(); resolve(false); };
        const onOk = () => { cleanup(); resolve(true); };
        cancelBtn.addEventListener('click', onCancel);
        okBtn.addEventListener('click', onOk);
        // Close on overlay click (optional)
        modal.addEventListener('click', function(e) {
            if (e.target === modal) {
                cleanup();
                resolve(false);
            }
        });
    });
}

// Phase B: Prev/Next now move by whole voucher, so "has the current work been validated"
// must consider every line of the current voucher, not just one arbitrary line — reuses the
// same per-voucher aggregation computeVoucherStats() already does.
// Source of truth is cleared_status (compute_and_persist_cleared_status(), Validation Rules
// module) — the same field the Status column's Cleared/Query badge reads — not the older
// validation_status (passed/verified), which predates that module and can disagree with it.
function isCurrentVoucherValidated() {
    if (!sessionTransactions.length || !currentVoucherId) return false;
    const voucherLines = sessionTransactions.filter(t => t.voucher_id === currentVoucherId);
    if (!voucherLines.length) return false;
    return voucherLines.every(t => t.cleared_status === 'Cleared');
}

// Phase B: 1-based position of currentVoucherId among sessionTransactions' distinct voucher_ids,
// e.g. {index: 3, total: 7} — used by the Prev/Next arrows' disabled state and boundary guards.
// {index: 0, total: 0} means "unknown" (sessionTransactions not loaded yet, or no current voucher).
function getVoucherPosition() {
    const uniqueVouchers = [...new Set(sessionTransactions.map(t => t.voucher_id).filter(Boolean))];
    const total = uniqueVouchers.length;
    if (!currentVoucherId || total === 0) return { index: 0, total: 0 };
    const idx = uniqueVouchers.indexOf(currentVoucherId);
    return { index: idx >= 0 ? idx + 1 : 0, total };
}

// Prev/Next processing UI: the POSITION half of the Prev/Next enabled rule, extracted so
// renderVoucherHeader() and applyVoucherNavButtonState() can't drift apart on what "there is a
// voucher that way" means. Position unknown (sessionTransactions not loaded yet) disallows both —
// the same safe default the rule has always had.
function getVoucherNavAllowed(pos) {
    const position = pos || getVoucherPosition();
    return {
        prev: position.total > 0 && position.index > 1,
        next: position.total > 0 && position.index < position.total
    };
}

// Prev/Next processing UI: the single place the buttons' disabled state is computed. Deliberately
// two INDEPENDENT conditions ANDed together — a button is clickable only if no navigation is in
// flight AND position says there's a voucher that way — rather than two writers each setting
// `disabled` at different moments. With competing writes, whichever finished last would win, so a
// fetch settling could leave a button enabled that position says must stay dead (or vice versa);
// ANDing removes that race by construction rather than by ordering the writes carefully.
function applyVoucherNavButtonState() {
    const allowed = getVoucherNavAllowed();
    const prevBtn = document.getElementById('prevArrow');
    const nextBtn = document.getElementById('nextArrow');
    if (prevBtn) prevBtn.disabled = voucherNavInFlight || !allowed.prev;
    if (nextBtn) nextBtn.disabled = voucherNavInFlight || !allowed.next;
}

// Prev/Next processing UI: the save-phase indicator. Reuses the existing voucher loading badge
// (spinner + message pill beside the voucher title) rather than a new primitive.
function showVoucherNavBadge(message) {
    voucherNavBadgeToken = voucherLoadToken;
    showVoucherLoadingBadge(message);
}

// The badge is ref-counted and shared. If a header re-render happened while our navigation was
// running (completeTransaction() → loadNextTransaction() does exactly this), renderVoucherHeader()
// already reset the ref count to 0 and renderVoucherLines() started the NEW voucher's own badge —
// so decrementing now would hide that one instead of ours, which is long gone. voucherLoadToken is
// the file's existing marker for "a new voucher has been painted since then" (see the myLoadToken
// guard in renderVoucherLines()), so the same check applies here.
function hideVoucherNavBadge() {
    if (voucherNavBadgeToken === null) return;
    if (voucherNavBadgeToken === voucherLoadToken) hideVoucherLoadingBadge();
    voucherNavBadgeToken = null;
}

// Prev/Next processing UI: the one guarded entry point for a whole voucher navigation. Wraps the
// entire save → validate → fetch → render sequence (not just the fetch) so the buttons are dead and
// the badge is up for every second the user is waiting, including the several sequential
// save_line_items POSTs that used to run in complete silence.
// The in-flight check is also the stacked-call guard: a second click (or a programmatic call from
// another entry point) while one navigation is running is dropped rather than queued behind it —
// the same reasoning as saveLineItems()'s inFlightLineSaves map one level down.
// Every exit route restores the buttons through the finally, including a cancelled confirm dialog,
// a boundary, Phase 1's error block, and a thrown error — there is no path that leaves them stuck
// disabled.
async function runGuardedVoucherNav(message, fn) {
    if (voucherNavInFlight) return;
    voucherNavInFlight = true;
    applyVoucherNavButtonState();
    showVoucherNavBadge(message);
    try {
        return await fn();
    } finally {
        hideVoucherNavBadge();
        voucherNavInFlight = false;
        applyVoucherNavButtonState();
    }
}

// Counts distinct vouchers in the session (not lines/transactions) and how many are fully
// verified (every one of that voucher's lines is Passed/Verified). Reads sessionTransactions,
// which validateTransaction() keeps patched with fresh validation_status so this stays accurate
// as the user works through the session, without a dedicated refresh fetch.
function computeVoucherStats() {
    const linesByVoucher = {};
    sessionTransactions.forEach(t => {
        if (!t.voucher_id) return;
        if (!linesByVoucher[t.voucher_id]) linesByVoucher[t.voucher_id] = [];
        linesByVoucher[t.voucher_id].push(t);
    });
    const voucherIds = Object.keys(linesByVoucher);
    const verifiedCount = voucherIds.filter(id => linesByVoucher[id].every(t => {
        const status = (t.validation_status || '').toLowerCase();
        return status === 'passed' || status === 'verified';
    })).length;
    return { totalVouchers: voucherIds.length, verifiedVouchers: verifiedCount };
}

// Consumes a voucher-nav-shaped response (non-boundary: {voucher_id, voucher_date, lines, ...})
// and renders it onto the main page — the shared render half of moveToAdjacentVoucher() below,
// factored out so the Verify & Next/Close button (verifyAndAdvanceVoucher()) can render its own
// verify_voucher_checks response the same way, since that endpoint returns the identical shape
// (it's built via the same _compute_voucher_nav_result() helper server-side). Returns true on
// success, false if the response has no lines to render (defensive — should not happen for a
// non-boundary response). Does NOT call updateProgressDisplay() — callers do that themselves,
// since the Verify path also has its own boundary branch to consider.
function applyVoucherNavRenderData(data) {
    const voucherLines = data.lines || [];
    const activeLine = voucherLines[0];
    if (!activeLine) return false;
    currentDetailId = activeLine.detail_id;
    currentClientDataId = activeLine.client_data_id;
    currentVoucherId = data.voucher_id;
    setVerificationUrlVoucher(currentVoucherId);
    currentTransactionType = activeLine.transaction_type || '';
    lineItems = activeLine.line_items || [];
    procedureData = activeLine.procedure_data || {};
    if (sessionTransactions.length > 0) {
        currentTransactionIndex = sessionTransactions.findIndex(t => t.detail_id === currentDetailId);
        if (currentTransactionIndex === -1) currentTransactionIndex = 0;
    }
    renderVoucherHeader(data.voucher_id, data.voucher_date, voucherLines);
    seedVoucherLineState(voucherLines);
    renderVoucherLines(voucherLines, data.voucher_date);
    return true;
}

// Human-readable direction word for the nav messages below ('prev'/'next' are the API's values,
// not something to show an auditor).
function voucherNavDirectionLabel(direction) {
    return direction === 'prev' ? 'previous' : 'next';
}

// Skeleton recovery, part 1: replaces a stuck loading skeleton with a readable failure state the
// user can act on. moveToAdjacentVoucher() paints a skeleton into #voucherLinesContainer before
// fetching, and every one of its non-success exits used to return without clearing it — leaving
// the voucher table shimmering forever, recoverable only by refreshing the page. Both buttons
// reuse existing button classes (.btn-voucher-nav/.btn-cashbook) so this needs no style.css
// change, and therefore no app-wide stylesheet cache-bump.
// Try again simply re-runs the same navigation: that call repaints the skeleton itself, so the
// retry gets its own visible feedback without a separate spinner. Reload page is the fallback for
// a session that's genuinely lost — the page's ?voucher= hard-refresh restore path (kept current
// by setVerificationUrlVoucher()) brings the auditor back to this same voucher.
function showVoucherNavError(direction, message) {
    const container = document.getElementById('voucherLinesContainer');
    if (!container) return;
    window.LoadingUI.hideSkeleton(container);
    // .voucher-nav-error carries no CSS of its own — it exists as a lookup/test hook. The few
    // inline styles below use the app's established primary/secondary text colours.
    container.innerHTML = `
        <div class="voucher-nav-error" style="padding: 28px 20px; text-align: center;">
            <div style="font-size: 15px; font-weight: 600; color: #1a1a2e; margin-bottom: 6px;">⚠️ ${message}</div>
            <div style="font-size: 13px; color: #64748b; margin-bottom: 16px;">The voucher you were on is still saved — nothing has been lost.</div>
            <div style="display: flex; gap: 10px; justify-content: center;">
                <button type="button" class="btn-voucher-nav" id="voucherNavRetryBtn">↻ Try again</button>
                <button type="button" class="btn-cashbook" id="voucherNavReloadBtn">Reload page</button>
            </div>
        </div>
    `;
    // Listeners attached after the innerHTML write — no inline onclick (project convention).
    // Routed through runGuardedVoucherNav() like Prev/Next themselves: this button calls
    // moveToAdjacentVoucher() directly, bypassing goToNextTransaction(), so without the wrapper a
    // retry would run with both nav buttons live — the one hole left in the stacked-call guard.
    container.querySelector('#voucherNavRetryBtn')?.addEventListener('click', function () {
        runGuardedVoucherNav(`Loading the ${voucherNavDirectionLabel(direction)} voucher…`, function () {
            return moveToAdjacentVoucher(direction);
        });
    });
    container.querySelector('#voucherNavReloadBtn')?.addEventListener('click', function () {
        window.location.reload();
    });
}

// Skeleton recovery, part 2: the boundary case, which is NOT a failure — the server correctly
// reporting there is no voucher in that direction. The auditor is still on a perfectly good
// voucher, so the skeleton is replaced by re-rendering that same voucher rather than by an error
// block. Re-rendering (not restoring saved innerHTML) is required: renderVoucherLines() attaches
// direct per-row listeners, so a raw HTML restore would look right but be dead to clicks.
// currentVoucherLines is the cache seedVoucherLineState() already keeps for the loaded voucher, so
// this costs no refetch; voucherDate is safely omitted since each row falls back to its own
// line.extract_date. If that cache is somehow empty we fall back to the error block rather than
// render an empty container — a blank table would just be a quieter version of the same dead end.
async function restoreCurrentVoucherLines(direction, message) {
    const container = document.getElementById('voucherLinesContainer');
    if (!container) return;
    if (!currentVoucherLines || currentVoucherLines.length === 0) {
        showVoucherNavError(direction, `Could not load the ${voucherNavDirectionLabel(direction)} voucher.`);
        return;
    }
    window.LoadingUI.hideSkeleton(container);
    await renderVoucherLines(currentVoucherLines);
    if (message) showStatus(message, 'info');
}

// Phase B: fetches the whole adjacent voucher (Phase A's /voucher_nav) and renders it via the
// same pipeline loadNextTransaction() already uses for /next's voucher-shaped response. Returns
// true on success, false on a boundary (no more vouchers that way) or fetch error.
// Skeleton recovery: every exit below now clears the skeleton it painted — a real failure leaves
// an error block with a retry action, a boundary re-renders the voucher the user is still on plus
// a toast. Nothing returns while the container is still shimmering, and no path is silent, so
// callers no longer need to report a false return themselves.
async function moveToAdjacentVoucher(direction) {
    if (!currentSessionId || !currentDetailId) return false;
    const label = voucherNavDirectionLabel(direction);
    try {
        window.LoadingUI.showSkeleton(document.getElementById('voucherLinesContainer'), 3);
        const url = `${API_BASE}/api/verification/voucher_nav/${currentSessionId}?current_detail_id=${currentDetailId}&direction=${direction}`;
        const res = await window.ApiUtils.apiFetch(url);
        if (!res.ok) {
            showVoucherNavError(direction, `Could not load the ${label} voucher (server responded ${res.status}).`);
            return false;
        }
        const data = await res.json();
        if (data.boundary) {
            await restoreCurrentVoucherLines(direction, `Already at the ${direction === 'prev' ? 'first' : 'last'} voucher in this session.`);
            return false;
        }
        if (!applyVoucherNavRenderData(data)) {
            showVoucherNavError(direction, `The ${label} voucher came back with no lines to display.`);
            return false;
        }
        // Deliberately its own try/catch, not part of the block above: this runs AFTER the new
        // voucher is already on screen, so letting it fall through to the outer catch would wipe a
        // correctly rendered voucher and replace it with an error block — a new bug introduced by
        // the fix. A stale progress count is the lesser problem, and it's logged.
        try {
            await updateProgressDisplay();
        } catch (progressError) {
            console.error('Voucher rendered but progress display failed to refresh:', progressError);
        }
        return true;
    } catch (e) {
        console.error('Error navigating to adjacent voucher:', e);
        showVoucherNavError(direction, `Could not load the ${label} voucher. Check your connection and try again.`);
        return false;
    }
}

// Go to previous voucher – with confirmation if not validated
async function goToPreviousTransaction() {
    const pos = getVoucherPosition();
    if (pos.total > 0 && pos.index <= 1) {
        // No message – silently ignore (already at the first voucher). Deliberately BEFORE the
        // guarded wrapper: an at-the-boundary click is a no-op, so it shouldn't flash the buttons
        // disabled and the badge on for nothing.
        return;
    }
    return runGuardedVoucherNav('Saving your work…', async () => {
        // Save current state first (always)
        await saveCurrentTransaction();
        // If not validated, ask for confirmation
        if (!isCurrentVoucherValidated()) {
            // Badge down while the dialog is up: a spinner claiming work is in progress alongside
            // a question waiting on the user would be misleading. The buttons stay disabled — they
            // sit behind the modal and nothing should navigate until this is answered.
            hideVoucherNavBadge();
            const confirmed = await showNavigationConfirm();
            if (!confirmed) {
                // User cancelled – no message. The wrapper's finally re-enables the buttons.
                return;
            }
            showVoucherNavBadge('Saving your work…');
        }
        await moveToAdjacentVoucher('prev');
    });
}

// Go to next voucher – with confirmation if not validated
async function goToNextTransaction() {
    // Silently no-op at the last voucher — the Next button is already disabled there, so
    // this restates nothing new to the user.
    const pos = getVoucherPosition();
    if (pos.total > 0 && pos.index >= pos.total) {
        return;
    }
    return runGuardedVoucherNav('Saving your work…', async () => {
        // Save current state
        await saveCurrentTransaction();
        // If not validated, ask for confirmation (badge down for the dialog — see the Prev
        // function above for why).
        if (!isCurrentVoucherValidated()) {
            hideVoucherNavBadge();
            const confirmed = await showNavigationConfirm();
            if (!confirmed) {
                // Cancelled — stay on the current voucher, no toast (nothing changed for the user
                // to be informed about). The wrapper's finally re-enables the buttons.
                return;
            }
            showVoucherNavBadge('Saving your work…');
        }
        // Proceed: validate and move (if validation passes, mark verified; if it fails, still allow
        // navigation since the user already confirmed they want to proceed).
        await validateTransaction(); // updates the active line's .voucher-line-status color/icon/label
        // Check if it passed, if yes mark verified.
        const statusCell = document.querySelector(`.voucher-line-row[data-detail-id="${currentDetailId}"] .voucher-line-status`);
        if (statusCell && statusCell.dataset.validationStatus === 'passed') {
            await completeTransaction(); // marks verified and calls loadNextTransaction
        } else {
            // Even if validation failed, we move to next because user confirmed.
            // No failure message here: moveToAdjacentVoucher() now reports every one of its own
            // outcomes in place (error block with a retry action, or a boundary toast). A second
            // message from here would double-report, and would wrongly say "not found" over a
            // boundary — which isn't a failure at all.
            await moveToAdjacentVoucher('next');
        }
    });
}
// Override the mirror icon click – we replace the alert
document.addEventListener('DOMContentLoaded', function() {
    // We'll set up the listener after the card is rendered; but we can use event delegation or attach in renderTransaction.
    // Instead, we'll attach a global listener for the icon that may not exist yet.
    // We'll use a mutation observer or just attach in the renderTransaction function.
    // But we already have a placeholder in renderTransaction; we'll replace that.
    // We'll modify the renderTransaction to call our new function.
    // Actually, we can do a direct replacement in the existing code: we'll replace the alert with loadTransactionList.
    // We'll do that in a separate chunk to avoid conflict.
});
// ---------- SAVE LINE ITEMS ----------
// Phase 4: accepts an explicit detailId (defaults to the active line, currentDetailId) so any
// line in the voucher can be saved independently, not just the active one — see
// saveAllVoucherLines(). Reads/writes that line's own voucherLineState entry (kept in sync by
// the per-line type-dropdown/observations/comments/procedure-modal listeners in
// renderVoucherLines()) rather than the bare currentTransactionType/procedureData/lineItems
// globals, which are legacy leftovers from the removed standalone view and not reconciled with
// voucherLineState.
// Concurrency guard: multiple independent triggers (per-check toggle, "check all", Prev/Next,
// a fast double-click, etc.) can all try to save the same detail_id before an earlier save for
// that same line has finished. Firing them concurrently doesn't parallelize anything — they
// queue behind each other on the same-row DB lock, so total save duration grows with every
// stacked call instead of staying flat. inFlightLineSaves makes a second call for a detail_id
// already being saved await/reuse that same in-flight request instead of issuing a duplicate
// POST — this is the single chokepoint every caller (saveAllVoucherLines(), toggleAllVoucherChecks()
// via hostAdapter, etc.) funnels through, so guarding here covers all of them.
const inFlightLineSaves = new Map(); // detailId -> Promise

async function saveLineItems(detailId = currentDetailId, options = {}) {
    if (!detailId) return;
    if (!options.force && !isVoucherLineDirty(detailId)) {
        return { ok: true, skipped: true };
    }
    if (inFlightLineSaves.has(detailId)) {
        return inFlightLineSaves.get(detailId);
    }
    const revisionAtStart = voucherLineDirtyRevisions[String(detailId)] || 0;
    const savePromise = doSaveLineItems(detailId).then(result => {
        if (result.ok && (voucherLineDirtyRevisions[String(detailId)] || 0) === revisionAtStart) {
            voucherLineDirtyRevisions[String(detailId)] = 0;
        }
        return result;
    });
    inFlightLineSaves.set(detailId, savePromise);
    try {
        return await savePromise;
    } finally {
        inFlightLineSaves.delete(detailId);
    }
}

async function doSaveLineItems(detailId) {
    const perfStarted = performance.now();
    if (!voucherLineState[detailId]) {
        voucherLineState[detailId] = { transactionType: '', procedureData: {}, lineItems: [] };
    }
    const state = voucherLineState[detailId];

    // Legacy vendor/unit/qty/rate mini-table — #lineItemsBody is not rendered by the current
    // voucher-line templates, so this is inert; kept unchanged from before voucher/line grouping.
    const tbody = document.getElementById('lineItemsBody');
    if (tbody) {
        const rows = tbody.querySelectorAll('tr');
        const items = [];
        rows.forEach(row => {
            const vendor = row.querySelector('.li-vendor')?.value || '';
            const unit = row.querySelector('.li-unit')?.value || '';
            const qty = parseFloat(row.querySelector('.li-qty')?.value) || 0;
            const rate = parseFloat(row.querySelector('.li-rate')?.value) || 0;
            const calc = parseFloat(row.querySelector('.li-calc')?.value) || 0;
            items.push({ vendor, unit, qty, rate, calculated_amount: calc });
        });
        state.lineItems = items;
    }

    // Legacy checkbox-based procedure inputs — also not rendered by the current templates
    // (procedure state is written by the supporting-document modal's Confirm handler instead).
    const procKeys = ['purchase_demand', 'purchase_order', 'gate_pass', 'store_receiving', 'vendor_bill'];
    procKeys.forEach(key => {
        const cb = document.getElementById(`proc_${key}`);
        if (cb) state.procedureData[key] = cb.checked;
        const refInput = document.querySelector(`input[data-proc="${key}"]`);
        if (refInput) state.procedureData[key + '_ref'] = refInput.value;
    });

    // Flush any unsaved Supporting Documents observation text for this line before/alongside the
    // legacy save below — this is what lets the observation textarea skip a dedicated Save button
    // and still survive Prev/Next/Complete/Exit (see flushProcedureObservations()).
    await flushProcedureObservations(detailId);
    // Voucher-level Checks simplified to name-only (Phase 3 of 4): the checks bar has no
    // local-first field anymore (tick/cross both save immediately) — no flush step needed here.
    // Account Head Recommendation follow-up: same "flush on save" treatment for a dirty no-match
    // note (the TB-match search-select selection stays confirmed scratch-only and is never flushed).
    await flushAccountHeadNoMatchNote(detailId);

    try {
        const payload = buildLineSavePayload(detailId);
        const res = await fetch(`${API_BASE}/api/verification/save_line_items${window.VerificationPerf.querySuffix()}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to save');
        }
        // Validation Rules Phase 5: the response now carries the freshly recomputed Cleared/Query
        // status — refresh the cached line + this line's badge without a second fetch, same
        // "live" refresh the mismatch badge already gets from already-loaded state.
        const data = await res.json();
        if ('cleared_status' in data) {
            const cachedLine = currentVoucherLines.find(l => String(l.detail_id) === String(detailId));
            if (cachedLine) {
                cachedLine.cleared_status = data.cleared_status;
                cachedLine.matrix_version_applied = data.matrix_version_applied;
                cachedLine.category_id_applied = data.category_id_applied;
                cachedLine.category_name_applied = data.category_name_applied;
            }
            // sessionTransactions is a SEPARATE cache from currentVoucherLines — fetched once by
            // loadTransactionList()/loadTransactionListPreservingPosition() at session start and
            // otherwise never patched. isCurrentVoucherValidated() (the Next/Prev "Transaction Not
            // Validated" gate) reads cleared_status from THIS array, not currentVoucherLines, so
            // without this patch a voucher's live status change (e.g. from completing a line other
            // than the one that happened to be active when the session was loaded) never reaches
            // the value the Next/Prev check reads, even though the Status column (which reads
            // currentVoucherLines/the rendered DOM) already shows the correct, current status.
            const cachedTransaction = sessionTransactions.find(t => String(t.detail_id) === String(detailId));
            if (cachedTransaction) {
                cachedTransaction.cleared_status = data.cleared_status;
                cachedTransaction.matrix_version_applied = data.matrix_version_applied;
                cachedTransaction.category_id_applied = data.category_id_applied;
                cachedTransaction.category_name_applied = data.category_name_applied;
            }
            updateClearedStatusBadgeForLine(detailId, data.cleared_status);
            // Freeze Category on voucher lines: reflect a first-time freeze immediately, same
            // "live" refresh the cleared-status badge already gets, without a full re-render.
            if (data.category_id_applied != null) {
                applyCategoryToTypeCell(detailId, data.category_name_applied, { frozen: true });
            }
        }
        window.VerificationPerf.log('save_line_items', perfStarted, {
            detail_id: String(detailId),
            revision_at_start: voucherLineDirtyRevisions[String(detailId)] || 0
        });
        if (procedureModalState[detailId]) delete procedureModalState[detailId].standaloneData;
        // The line (incl. its flushed observation text) is now persisted — drop the voucher-wide
        // support cache so the Procedures/Observations badge refresh re-reads saved state.
        invalidateVoucherSupportCache();
        return { ok: true, skipped: false };
        // Success – no status bar message
    } catch (e) {
        console.error('Error saving:', e);
        return { ok: false, skipped: false, error: e };
        // Do not show status bar error – only log
    }
}

// ---------- SAVE ALL VOUCHER LINES (Phase 4) ----------
// Saves only dirty lines, sequentially through the existing endpoint. Called automatically before
// navigation, completion, and exit; unchanged lines issue no request. Sequential execution remains
// deliberate until a later atomic voucher-save backend exists. Former call sites include
// completeTransaction(), returnToCashbookFromSession()) so edits on any expanded line aren't lost — there is no
// manual save button.
async function saveAllVoucherLines() {
    const allUids = Object.keys(voucherLineState);
    const uids = allUids.filter(isVoucherLineDirty);
    const perfStarted = performance.now();
    if (window.VerificationPerf.enabled) {
        console.info('[verification-perf]', {
            operation: 'save_all_voucher_lines_inventory',
            voucher_id: currentVoucherId,
            line_count: allUids.length,
            dirty_count: uids.length,
            skipped_count: allUids.length - uids.length,
            detail_ids: uids.map(String),
            dirty_state: 'only listed detail_ids will be saved sequentially'
        });
    }
    let savedCount = 0;
    let failedCount = 0;
    for (const uid of uids) {
        const result = await saveLineItems(uid);
        if (result && result.ok && !result.skipped) savedCount += 1;
        if (result && !result.ok) failedCount += 1;
    }
    window.VerificationPerf.log('save_all_voucher_lines', perfStarted, {
        voucher_id: currentVoucherId,
        dirty_count: uids.length,
        saved_count: savedCount,
        failed_count: failedCount,
        skipped_count: allUids.length - uids.length,
        execution: 'sequential'
    });
    return { savedCount, failedCount, skippedCount: allUids.length - uids.length };
}

// Shared payload builder for the normal save and the atomic Save + Verify request. Keeping this
// in one place prevents the faster path from silently omitting any current line fields.
function buildLineSavePayload(detailId) {
    const state = voucherLineState[detailId] || {};
    return {
        detail_id: detailId,
        line_items: state.lineItems || [],
        transaction_type: state.transactionType,
        procedure_data: state.procedureData || {}
    };
}

async function saveAndVerifyVoucher(detailId, verify = true) {
    // The grouped request persists every line in the current voucher. Capture each line's dirty
    // revision so a successful response clears only edits included in this request; an edit made
    // while the request is in flight must remain dirty for a later retry/save.
    const revisionsAtStart = {};
    currentVoucherLines.forEach(line => {
        revisionsAtStart[String(line.detail_id)] = voucherLineDirtyRevisions[String(line.detail_id)] || 0;
    });
    const lines = currentVoucherLines.map(line => ({
        ...buildLineSavePayload(line.detail_id),
        transaction_ref: line.transaction_ref || line.voucher_id
    }));
    // Fallback shape must match getVoucherTickSaveState()'s own snake_case keys — see the
    // comment there — since both are spread straight into the JSON body below.
    const tickState = typeof getVoucherTickSaveState === 'function'
        ? getVoucherTickSaveState() : { procedure_checks: [], voucher_checks: [] };
    const res = await fetch(`${API_BASE}/api/verification/save_and_verify_voucher_bundle${window.VerificationPerf.querySuffix()}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lines, ...tickState, verify, session_id: currentSessionId, current_detail_id: detailId })
    });
    if (!res.ok) return { ok: false, error: await res.json().catch(() => ({})) };
    const data = await res.json();
    const statusById = data.line_statuses || {};
    Object.entries(statusById).forEach(([id, status]) => {
        const line = currentVoucherLines.find(l => String(l.detail_id) === String(id));
        if (line) {
            line.cleared_status = status;
            updateClearedStatusBadgeForLine(id, status);
        }
        const cachedTransaction = sessionTransactions.find(t => String(t.detail_id) === String(id));
        if (cachedTransaction) cachedTransaction.cleared_status = status;
    });
    const line = currentVoucherLines.find(l => String(l.detail_id) === String(detailId));
    if (line && data.cleared_status) line.cleared_status = data.cleared_status;
    currentVoucherLines.forEach(savedLine => {
        const key = String(savedLine.detail_id);
        if ((voucherLineDirtyRevisions[key] || 0) === revisionsAtStart[key]) {
            voucherLineDirtyRevisions[key] = 0;
        }
    });
    // The bundle just persisted every line in this voucher (procedure/voucher ticks included) —
    // drop the voucher-wide support cache, mirroring invalidateProcedureModalDataForVoucher() at
    // procedure_modal.js's own post-bundle-save call sites, so hideProcedureModalOnly()'s badge
    // refresh reads saved state rather than the pre-save copy.
    invalidateVoucherSupportCache();
    return { ok: true, data };
}

// ---------- VALIDATE TRANSACTION ----------
async function validateTransaction() {
    if (!currentDetailId) return;
    await saveLineItems();
    try {
        const payload = { detail_id: currentDetailId, inventory_flag: false };
        const res = await fetch(`${API_BASE}/api/verification/validate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Validation failed');
        }
        const data = await res.json();
        // Reflect result on the active line's status cell (icon + label + color).
        const statusCell = document.querySelector(`.voucher-line-row[data-detail-id="${currentDetailId}"] .voucher-line-status`);
        if (statusCell) {
            const status = data.validation_status.toLowerCase();
            statusCell.className = `voucher-line-status status-${status}`;
            statusCell.dataset.validationStatus = status;
            const cachedLine = currentVoucherLines.find(l => String(l.detail_id) === String(currentDetailId));
            const clearedStatus = cachedLine ? cachedLine.cleared_status : null;
            statusCell.innerHTML = getVoucherLineStatusPillHtml(data.validation_status, clearedStatus);
        }
        // Keep the cached sessionTransactions entry in sync — it's fetched once by
        // loadTransactionList() and otherwise never patched, so isCurrentVoucherValidated()
        // and computeVoucherStats() would see stale data without this.
        const cachedTransaction = sessionTransactions.find(t => t.detail_id === currentDetailId);
        if (cachedTransaction) cachedTransaction.validation_status = data.validation_status;
        // Phase 2: standalone transaction view removed — verifyBtn DOM element no longer exists.
        // Validation result is reflected via the per-line status cell above.
    } catch (e) {
        console.error('Validation error:', e);
        // Do not show status bar message – only log to console
    }
}

// ---------- COMPLETE TRANSACTION ----------
async function completeTransaction() {
    if (!currentDetailId) return;
    // Phase 4: save every line before completing/reloading, not just the active one — otherwise
    // edits on a different expanded line would be lost when loadNextTransaction() re-renders.
    await saveAllVoucherLines();
    await validateTransaction();
    try {
        const payload = { detail_id: currentDetailId, verified_by: 'Analyst' };
        const res = await fetch(`${API_BASE}/api/verification/complete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to complete');
        }
        // No status message – proceed silently
        await loadNextTransaction();
    } catch (e) {
        console.error('Error completing:', e);
        // Do not show status bar error – only log
    }
}

// ---------- RETURN TO CASHBOOK VIEW FROM AN ACTIVE SESSION ----------
// Renamed from exitSession() (Session date-range reconciliation, Phase 2 of 4): this used to be a
// destructive "end the session" action (confirm dialog, nulled currentSessionId/currentDetailId,
// cleared the voucher header) - it no longer is. Reaching Place 2 (the cashbook view) from an
// active session now keeps the session alive in the background (currentSessionId/currentDetailId
// untouched, sessionContent's DOM left intact, just hidden) so Apply's new range-reconcile flow
// (applyCashbookPreview() -> reconcileActiveSessionRange()) has a session to reconcile against,
// and so leaving Place 2 again without touching Apply (returnToSessionFromCashbook()) can cheaply
// resume exactly where the auditor left off - no re-fetch, no confirm dialog, since nothing is
// actually lost.
// This is what runs on both the "🏦/📒 Cashbook" button and the global navbar Up icon (its
// setUpOverride() target, set in activateSessionView()). Returning to the cashbook view still
// reuses restoreCashbookView() — the exact same fresh load/render path a hard refresh into
// ?state=cashbook goes through (loadCashbooks() then activateCashbookView(), which itself handles
// hiding dashboard/session, showing cashbookContent, the Up override, header labels, the
// cashbook selector, date-range defaults, the URL, and fetching data) — instead of manually
// toggling visibility and calling fetchCashbookData() directly. The manual version rendered a
// blank cashbook whenever the session had been reached via a direct ?state=session URL restore
// (restoreSessionView()), since that path never populates cashbookStartDate/cashbookEndDate/the
// selector - fetchCashbookData() would then bail out early on an empty date range.
// Known accepted gap (confirmed with the user): there is currently no separate "fully end this
// session" action distinct from this navigate-to-cashbook one - flagged as a follow-up decision,
// not built this phase.
async function returnToCashbookFromSession() {
    // Confirmed fix: capture Place 2's live range before navigating away so restoreCashbookView()
    // can preserve it, instead of letting its own loadCashbooks() call silently overwrite it
    // with a freshly auto-detected default (the "resets to session-start range" bug). Only a
    // defensive fallback (picker/range genuinely missing - shouldn't occur, since a session
    // can't be started without a valid range) falls through to the existing auto-detect
    // behavior by omitting the override args.
    await leaveVerificationSafely(async function() {
        const preservedRange = cashbookDateRangePicker ? cashbookDateRangePicker.getRange() : null;
        if (preservedRange && preservedRange.start && preservedRange.end) {
            await restoreCashbookView(selectedCashbook, preservedRange.start, preservedRange.end);
        } else {
            await restoreCashbookView(selectedCashbook);
        }
    });
}

// ---------- RESUME AN ACTIVE SESSION FROM PLACE 2 (no re-fetch) ----------
// Session date-range reconciliation, Phase 2 of 4: the back-arrow target while viewing Place 2
// with a still-open session (set by activateCashbookView()) - and the tail end of a successful
// reconcileActiveSessionRange(). sessionContent's DOM/state was only ever hidden by
// activateCashbookView(), never destroyed, so this is just the reverse view-swap (mirrors
// activateSessionView()'s own swap/back-override/URL lines) with no network call of its own -
// callers that need fresh data (reconcileActiveSessionRange()) fetch it themselves first.
function returnToSessionFromCashbook() {
    dashboardContent.style.display = 'none';
    cashbookContent.style.display = 'none';
    sessionContent.style.display = 'block';
    if (typeof setUpOverride === 'function') setUpOverride(returnToCashbookFromSession);
    const container = document.getElementById('verificationContainer');
    if (container) container.classList.add('verification-active');
    if (currentSessionId) setVerificationUrl('session', currentSessionId);
}

// ---------- SUPPORTING DOCUMENTS MODAL ----------
function openSupportingModal(templateKey, label) {
    if (!supportingModal) return;
    supportingModal.style.display = 'flex';
    if (supportingResults) supportingResults.innerHTML = '<div style="color:#94a3b8; text-align:center; padding:20px;">Enter a search term and click Search.</div>';
    if (supportingModal) {
        supportingModal.dataset.template = templateKey;
        supportingModal.dataset.label = label;
    }
    if (supportingSearchInput) supportingSearchInput.focus();
}

async function searchSupportingDocuments() {
    if (!supportingSearchInput || !supportingResults) return;
    const searchTerm = supportingSearchInput.value.trim();
    if (!searchTerm) {
        supportingResults.innerHTML = '<div style="color:#f59e0b; text-align:center; padding:10px;">Please enter a search term.</div>';
        return;
    }
    const clientId = getClientId();
    if (!clientId) {
        supportingResults.innerHTML = '<div style="color:#dc2626; text-align:center; padding:10px;">No client selected.</div>';
        return;
    }
    const templateMap = {
        'purchase_orders': 1,
        'gate_passes': 2,
        'store_receiving': 3,
        'vendor_bills': 4
    };
    const templateId = templateMap[supportingModal.dataset.template] || 1;
    try {
        const params = new URLSearchParams({
            client_id: clientId,
            search: searchTerm,
            template_id: templateId,
            limit: 20
        });
        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/supporting?${params}`);
        if (!res.ok) throw new Error('Failed to search');
        const data = await res.json();
        const docs = data.supporting_documents || [];
        if (docs.length === 0) {
            supportingResults.innerHTML = '<div style="color:#94a3b8; text-align:center; padding:10px;">No documents found.</div>';
        } else {
            let html = '<div style="font-size:13px; font-weight:600; padding:4px 8px; background:#f8fafc;">Results:</div>';
            docs.forEach(doc => {
                const row = doc.row_data || {};
                const desc = row['Description'] || row['PO Number'] || row['GRN Number'] || row['Invoice Number'] || '';
                const amount = doc.extract_amount || '';
                html += `<div class="result-item" data-id="${doc.id}" style="display:flex; justify-content:space-between; align-items:center;">
                    <span>${desc} ${amount ? ' - Amount: '+amount : ''}</span>
                    <button class="btn-outline" style="padding:2px 12px; font-size:12px;">Select</button>
                </div>`;
            });
            supportingResults.innerHTML = html;
            supportingResults.querySelectorAll('.result-item .btn-outline').forEach(btn => {
                btn.addEventListener('click', function() {
                    const item = this.closest('.result-item');
                    const id = item.dataset.id;
                    alert('Selected document ID: ' + id + '\nYou can copy reference details manually.');
                });
            });
        }
    } catch (e) {
        console.error('Search error:', e);
        supportingResults.innerHTML = `<div style="color:#dc2626; text-align:center; padding:10px;">Error: ${e.message}</div>`;
    }
}

// ---------- BANK VERIFICATION (Phase 8): OPEN SESSION POPUP ----------
// Sets all book-type-specific popup text based on currentType, then opens it and loads
// candidates. Called automatically on page load when a ?type= param is present (see
// window.onload below) - the only entry path now: both the dashboard's Cash/Bank cards
// (static/index.html) and this page's own hub tiles (#cashVoucherTile/#bankVoucherTile)
// navigate here with ?type= already chosen, so the hub grid is skipped on those loads.
function openSessionPopup() {
    const title = document.getElementById('sessionPopupTitle');
    const subtitle = document.getElementById('sessionPopupSubtitle');
    const label = document.getElementById('popupCashbookLabel');
    if (currentType === 'bank') {
        if (title) title.textContent = '📋 Start Bank Verification';
        if (subtitle) subtitle.textContent = 'Select a bankbook to begin verification.';
        if (label) label.textContent = '🏦 Bankbook';
        if (popupStartBtn) popupStartBtn.textContent = '🚀 Open Bankbook';
    } else {
        if (title) title.textContent = '📋 Start Cash Verification';
        if (subtitle) subtitle.textContent = 'Select a cashbook to begin verification.';
        if (label) label.textContent = '📒 Cashbook';
        if (popupStartBtn) popupStartBtn.textContent = '🚀 Open Cashbook';
    }
    popupOverlay.style.display = 'flex';
    // Confirmed fix: reopening the popup is always a fresh page load (this is the only caller of
    // openSessionPopup(), and it only ever fires from window.onload), so there is no in-memory
    // range left to fall back on - use the persisted range as an explicit override (same mechanism
    // handlePopupPeriodChange()/returnToCashbookFromSession() already use) when one exists, falling back to
    // A fresh Vouchers entry starts with today's date only, so the common case avoids the
    // latest-date lookup and a full-month query. Users can widen the range manually.
    // Confirmed fix: same reasoning applies to the cashbook selection itself (previously missed) -
    // read the persisted pick and pass it through as loadCashbooks()'s overrideCashbook so its
    // existing "reapply if still valid" logic picks it back up.
    const persisted = getPersistedVerificationDateRange();
    const persistedCashbook = getPersistedSelectedCashbook();
    if (persisted) {
        loadCashbooks(persisted.start, persisted.end, persistedCashbook);
    } else {
        const today = formatDateInput(new Date());
        loadCashbooks(today, today, persistedCashbook);
    }
}

// ---------- EVENT LISTENERS ----------
// Verification hub tiles: Cash/Bank navigate with ?type=, deliberately re-entering this same
// page through the existing window.onload auto-open path (openSessionPopup fires on load when
// a type param is present) — no separate in-page open logic, one entry path for both the
// dashboard's cards (index.html) and these hub tiles. "Coming soon" tiles have no handler.
//
// Because that entry path is a full page reload whose popup can't appear until the slow
// current-client call resolves (see the clientContextReady await in window.onload below), the
// tile would otherwise sit there looking idle and stay clickable through the entire wait — and a
// second click just restarts the reload, making the wait longer. So a clicked tile is locked:
// LoadingUI's spinner gives visible feedback, and hubTileNavigationInFlight blocks any further
// click. A .module-card is a <div>, not a <button>, so showSpinner can't set .disabled for us
// (its isButton test is false for a div) — the flag IS the lock, there is no native one.
let hubTileNavigationInFlight = false;
let lockedHubTile = null;

// One wording for both the click and the on-arrival lock, keyed off the tile's own type. The
// click happens before the reload and the arrival lock after it, so using the same message on
// both sides makes the reload read as one continuous wait instead of two different ones.
function hubTileOpeningMessage(tileType) {
    return tileType === 'bank' ? 'Opening bankbook…' : 'Opening cashbook…';
}

// Locks one hub tile: spinner in place of its contents (hideSpinner restores them) plus the
// in-flight flag. Shared by the click path and the on-arrival path below so both behave
// identically.
function lockHubTile(tile, message) {
    if (!tile || hubTileNavigationInFlight) return;
    hubTileNavigationInFlight = true;
    lockedHubTile = tile;
    if (window.LoadingUI) window.LoadingUI.showSpinner(tile, message || 'Opening…');
}

// Releases whichever tile is locked. Safe to call when nothing is locked, so every release path
// can call it unconditionally rather than testing first.
function unlockHubTiles() {
    if (lockedHubTile && window.LoadingUI) window.LoadingUI.hideSpinner(lockedHubTile);
    lockedHubTile = null;
    hubTileNavigationInFlight = false;
}

// One delegated listener for every live tile, rather than a handler per tile: a tile is live
// purely by carrying data-verification-type (see the contract comment in verification.html), so a
// tile added later gets this same spinner + double-click lock with no change here. A "Coming
// soon" tile has no such attribute, so closest() returns null and the click is ignored — exactly
// its behaviour before this listener existed.
if (dashboardContent) {
    dashboardContent.addEventListener('click', function(event) {
        const tile = event.target.closest('[data-verification-type]');
        if (!tile) return;
        // The double-click lock. A second click while the first navigation is still in flight is
        // dropped here, before it can restart the page load.
        if (hubTileNavigationInFlight) return;
        const tileType = tile.getAttribute('data-verification-type');
        if (!tileType) return;
        lockHubTile(tile, hubTileOpeningMessage(tileType));
        window.location.href = '/static/verification.html?type=' + encodeURIComponent(tileType);
    });
}

// Back/bfcache reset path. Assigning window.location.href gives no failure callback, and on
// success this page is destroyed — so the one case where a locked tile can actually get stuck is
// the browser restoring this exact page from the back/forward cache, spinner and flag intact.
// event.persisted is true only for that restore; an ordinary Back re-parses this script fresh, so
// the state is already clean there. Nothing should ever require a refresh to un-stick a tile.
window.addEventListener('pageshow', function(event) {
    if (event.persisted) unlockHubTiles();
});

// Stop-loading reset path. Escape IS the browser's "stop loading" gesture, so a user who gives up
// on a slow load and presses it cancels the navigation while this page stays alive — the one
// remaining way a locked tile could otherwise sit there permanently, needing a refresh to clear.
// Deliberately keyed to the real gesture rather than a timeout: a guessed duration would also fire
// during a load that is simply slow but still perfectly on its way, which is the exact situation
// this whole feature exists to support. Escape while nothing is locked is a no-op, so this can't
// interfere with the popup's own Escape handling.
document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape' && hubTileNavigationInFlight) unlockHubTiles();
});

// On-arrival lock. The tile is only half the wait: this page renders its hub visible by default,
// so on the ?type= load the tiles are live and clickable again from first paint right through the
// clientContextReady await, which is where the double-click race actually happens. Locking here
// at parse time — the earliest possible moment, before that await — closes it, and window.onload
// releases the tile once the popup is actually up.
//
// The condition deliberately mirrors window.onload's own popup-open decision exactly rather than
// just testing for ?type=. setVerificationUrl() writes state and type TOGETHER, so a hard-refresh
// URL like ?state=session&id=5&type=cash carries a type but takes the restore branch and never
// opens the popup — a looser check would strand a spinner on the hub permanently there.
const hubArrivalParams = new URLSearchParams(window.location.search);
const hubArrivalState = hubArrivalParams.get('state');
const hubArrivalView = hubArrivalParams.get('view');
const hubPopupWillOpen =
    (hubArrivalParams.has('type') || hubArrivalView === 'cashbook' || hubArrivalView === 'bankbook') &&
    hubArrivalState !== 'session' && hubArrivalState !== 'cashbook';
if (hubPopupWillOpen) {
    // currentType already resolves ?view=bankbook → 'bank' and ?type= otherwise, so it's the
    // single source of truth for which tile is the one being opened — no second derivation here.
    const arrivingTile = document.querySelector('[data-verification-type="' + currentType + '"]');
    lockHubTile(arrivingTile, hubTileOpeningMessage(currentType));
}
if (popupStartBtn) popupStartBtn.addEventListener('click', openCashbook);
// Editable search range (confirmed fix): re-run the search with the user's chosen dates once a
// full range is picked - mirrors the existing cashbookStartDate/cashbookEndDate pattern in
// #cashbookContent. Now fired directly as the date-range picker's onChange callback (Phase 1 of
// the single-calendar range-picker rollout) instead of a DOM 'change' listener on two inputs.
// Confirmed fix: also mirrors into Place 2's picker (cashbookDateRangePicker) here, symmetric
// with handleCashbookDateRangeChange()'s existing Place 2 → Place 1 mirror below - previously
// this direction had no mirror call at all. Safe against a mirroring loop since setRange() never
// fires onChange. The real value carrier for the "Open Cashbook" transition is
// activateCashbookView() reading popupDateRangePicker.getRange() directly, but this keeps the two
// pickers in sync live too, matching the confirmed "keep both places mirrored" requirement.
function handlePopupPeriodChange(start, end) {
    if (cashbookDateRangePicker) cashbookDateRangePicker.setRange(start, end);
    if (start && end && start <= end) {
        saveVerificationDateRange(start, end);
        loadCashbooks(start, end);
    }
}
// Replaces the old two-input Start/End Date fields with the reusable date-range picker, mounted
// in place inside their shared wrapper (static/verification.html is not edited this phase - see
// mountPopupDateRangePicker()).
popupDateRangePicker = mountPopupDateRangePicker();
// Cashbook filter bar's own picker instance (Phase 2 of the single-calendar range-picker
// rollout) - independently editable, deliberately does NOT trigger a fetch itself (that stays
// gated behind the existing "📊 Apply" button/applyCashbookPreview(), unchanged); only mirrors
// the picked range into Place 1's picker so it stays in sync if that dialog is reopened.
// Superseded (Session date-range reconciliation, Phase 2 of 4): the 2026-08-11 investigation
// found a session's voucher list was a fixed snapshot with no way to change it short of starting
// a new session - that's no longer true. Phase 1 (routers/verification.py) added
// POST /api/verification/session/{id}/reconcile_range, which re-resolves an active session's
// voucher list against a new range without losing anything (soft-excludes what falls out,
// reactivates what comes back, adds what's newly in range); this phase wires it up so clicking
// Apply on this picker's currently-set range calls it when a session is active
// (reconcileActiveSessionRange(), applyCashbookPreview()). Picking a date here still only mirrors
// the range into Place 1's picker and persists it - by itself, still no fetch/reconcile; that
// only happens on Apply, same as it always has for the non-session preview case.
function handleCashbookDateRangeChange(start, end) {
    if (popupDateRangePicker) popupDateRangePicker.setRange(start, end);
    if (start && end && start <= end) {
        saveVerificationDateRange(start, end);
    }
}
cashbookDateRangePicker = mountCashbookDateRangePicker();
if (popupCancelBtn) {
    popupCancelBtn.addEventListener('click', function() {
        popupOverlay.style.display = 'none';
    });
}
// Confirmed fix: persist Place 1's cashbook selection the moment it's picked, mirroring
// handlePopupPeriodChange()'s immediate-save-on-pick for the date range - not gated behind
// clicking "Open Cashbook", so a reload before starting a session still remembers the pick.
if (popupCashbookSelect) {
    popupCashbookSelect.addEventListener('change', function() {
        if (popupCashbookSelect.value) saveSelectedCashbook(popupCashbookSelect.value);
    });
}

// Cashbook page events
// Apply commits both the dropdown's currently selected cashbook/bankbook AND the date range
// together (applyCashbookPreview) - the dropdown itself has no change listener, so picking a
// different value does nothing until this button is clicked (Phase 2 of 3).
if (cashbookFetchBtn) cashbookFetchBtn.addEventListener('click', applyCashbookPreview);
if (cashbookPageSizeSelect) {
    cashbookPageSizeSelect.addEventListener('change', function() {
        cashbookPageSize = parseInt(this.value);
        cashbookCurrentPage = 1;
        fetchCashbookData();
    });
}
if (cashbookPrevPage) {
    cashbookPrevPage.addEventListener('click', function() {
        if (cashbookCurrentPage > 1) {
            cashbookCurrentPage--;
            fetchCashbookData();
        }
    });
}
if (cashbookNextPage) {
    cashbookNextPage.addEventListener('click', function() {
        const totalPages = Math.ceil(cashbookTotalRows / cashbookPageSize);
        if (cashbookCurrentPage < totalPages) {
            cashbookCurrentPage++;
            fetchCashbookData();
        }
    });
}
if (startVerificationBtn) startVerificationBtn.addEventListener('click', startSessionFromCashbook);
if (cashbookAuditWorkbookBtn) cashbookAuditWorkbookBtn.addEventListener('click', navigateToAuditWorkbook);

// Phase 2: standalone transaction view removed — prevBtn/nextBtn/verifyBtn/saveProgressBtn/exitBtn
// are no longer static DOM elements (nav controls are dynamically rendered in #voucherHeaderBar).
// Navigation event wiring is handled by renderVoucherHeader() which binds #prevArrow/#nextArrow/
// #cashbookReturnBtn after each innerHTML write.

// Supporting modal
if (supportingSearchBtn) supportingSearchBtn.addEventListener('click', searchSupportingDocuments);
if (supportingSearchInput) {
    supportingSearchInput.addEventListener('keypress', function(e) {
        if (e.key === 'Enter') searchSupportingDocuments();
    });
}
if (supportingModalClose) {
    supportingModalClose.addEventListener('click', function() {
        if (supportingModal) supportingModal.style.display = 'none';
    });
}
if (supportingModal) {
    supportingModal.addEventListener('click', function(e) {
        if (e.target === this) supportingModal.style.display = 'none';
    });
}

// ---------- DFD (DATA FLOW DIAGRAM) PLACEHOLDER MODAL (Phase 5: shell only, no diagram content) ----------
const dfdModal = document.getElementById('dfdModal');
const dfdModalClose = document.getElementById('dfdModalClose');

function openDfdModal() {
    if (dfdModal) dfdModal.style.display = 'flex';
}

function closeDfdModal() {
    if (dfdModal) dfdModal.style.display = 'none';
}

if (dfdModalClose) {
    dfdModalClose.addEventListener('click', closeDfdModal);
}
if (dfdModal) {
    dfdModal.addEventListener('click', function(e) {
        if (e.target === this) closeDfdModal();
    });
}

// ==============================================
// The entire Supporting Documents procedure modal (search/assign/unassign evidence, Audit
// Procedures checklist, observations, physical bill entry + calculator, browse modal, the
// voucher-checks bar, the Validate panel's fetch, and this section's own event-listener wiring)
// now lives in static/procedure_modal.js — see that file for the full implementation.
// ==============================================

// ---------- GLOBAL NAVBAR BACK: RESTORE A VIEW FROM THE CURRENT URL ----------
// Factored out of window.onload's own hard-refresh restore branches (below) so the same dispatch
// logic can also serve as the Back history stack's in-page restore handler (see
// window.setInPageRestoreHandler() in window.onload) — reached only in the narrow edge case where
// the popped stack entry's URL exactly matches the current URL (Back's own "different page, do a
// real navigation" path already handles every other case via a full reload straight into this same
// ?state=/?id= restore logic, since pushInPageNavEntry() always pushes AFTER setVerificationUrl()
// has updated the URL to match the view being entered).
function restoreVerificationViewFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const stateParam = params.get('state');
    const idParam = params.get('id');
    if (stateParam === 'session' && idParam) {
        restoreSessionView(parseInt(idParam, 10));
        return;
    }
    if (stateParam === 'cashbook' && idParam) {
        restoreCashbookView(idParam);
        return;
    }
    // No recognized state param — the dashboard hub itself.
    if (dashboardContent) dashboardContent.style.display = 'block';
    if (sessionContent) sessionContent.style.display = 'none';
    if (cashbookContent) cashbookContent.style.display = 'none';
    const container = document.getElementById('verificationContainer');
    if (container) container.classList.remove('verification-active');
    if (typeof setUpOverride === 'function') setUpOverride(null);
}

// ---------- INIT ----------
window.onload = async function() {
    // client_context.js populates the session-backed client cache from an async fetch. window.onload
    // is not a safe barrier for that (see the ready-signal note in client_context.js) -- without this
    // await, loadVerificationStats()/loadDataLinksCards() below run with a null client id.
    if (window.clientContextReady) await window.clientContextReady;

    if (dashboardContent) dashboardContent.style.display = 'block';
    if (sessionContent) sessionContent.style.display = 'none';
    if (cashbookContent) cashbookContent.style.display = 'none';
    if (typeof setNavModule === 'function') {
        setNavModule('Verification');
    }
    // Global navbar Back icon: lets the history stack restore this page's own in-page views (see
    // restoreVerificationViewFromUrl() above) for the rare same-URL edge case.
    if (typeof window.setInPageRestoreHandler === 'function') {
        window.setInPageRestoreHandler(restoreVerificationViewFromUrl);
    }
    loadVerificationStats();
    loadDataLinksCards();
    // Bank Verification (Phase 8): a ?type= param means the user already chose Cash or Bank on
    // the dashboard (static/index.html) - skip this page's own internal Module Grid and open the
    // session popup directly, instead of asking the user to pick a verification type a second
    // time. Direct navigation to this page with no ?type param is unaffected (currentType
    // defaults to 'cash', and the grid still shows normally for that entry path).
    //
    // Global quick-nav deep-link (Phase 3): a ?view=cashbook/bankbook param (from the ⊞ dropdown
    // or search field's Cashbook/Bank book entries) reuses this exact same entry path - there is
    // no book name in the link, so the furthest this can jump straight to is the same pre-filled
    // popup ?type= already opens; the user picks a specific book and clicks "Open
    // Cashbook/Bankbook" from there (openCashbook()), same as today. No setUpOverride() call is
    // needed here: openCashbook()/startSessionFromCashbook()/returnToCashbookFromSession() already register the
    // correct Up-icon override once the user actually reaches a real view.
    const arrivalParams = new URLSearchParams(window.location.search);

    // try/finally purely so the on-arrival hub tile lock (see lockHubTile() above) is always
    // released — whether this ends by opening the popup, returning early down the hard-refresh
    // restore branch, or throwing on the way. Without the finally, a failure in here would leave
    // the hub's tile spinning and unclickable with no way back short of a refresh, which is the
    // stuck state this whole phase exists to prevent. unlockHubTiles() is a no-op when nothing is
    // locked, so the entry paths that never lock a tile are completely unaffected.
    try {
        // Hard-refresh view persistence: ?state=session|cashbook&id=... (written by
        // setVerificationUrl()) means the user was previously in that view and hard-refreshed - always
        // re-fetch fresh from the server for that id rather than falling through to the dashboard
        // default below. Checked ahead of the existing ?type=/?view= entry-link handling, which stays
        // completely unchanged for ordinary (non-restored) page loads.
        const stateParam = arrivalParams.get('state');
        if (stateParam === 'session' || stateParam === 'cashbook') {
            restoreVerificationViewFromUrl();
            return;
        }

        const arrivalView = arrivalParams.get('view');
        const isDeepLinkView = arrivalView === 'cashbook' || arrivalView === 'bankbook';
        if (arrivalParams.has('type') || isDeepLinkView) {
            openSessionPopup();
        }
    } finally {
        // openSessionPopup() displays the popup overlay synchronously before its own async
        // cashbook load, so by the time this runs the popup is genuinely on screen — the tile's
        // job is done and the modal's own spinner takes over from here.
        unlockHubTiles();
    }
};

// Icon lookup for Client Data Links cards, keyed by exact template_name. Purely a display detail
// (the templates table has no icon column) - any template name not listed here still gets a card,
// just with the generic fallback icon, so a brand-new template needs no code change here either.
const TEMPLATE_LINK_ICONS = {
    'Transaction Report': '📊',
    'Purchase Demand': '📋',
    'Purchase Order': '📄',
    'Gate Pass': '🚪',
    'Store receiving': '📥',
    'Vendor Bill': '📃'
};
const TEMPLATE_LINK_ICON_FALLBACK = '📁';

// Builds the Client Data Links card strip from the real, active templates list instead of a
// hardcoded set of cards - so a template added/renamed later (e.g. "Vendor Bill") automatically
// gets/keeps a correct shortcut card with no HTML/JS edits required.
async function loadDataLinksCards() {
    if (!dataLinksCardStrip) return;
    window.LoadingUI.showSkeleton(dataLinksCardStrip, 4);
    try {
        const res = await window.ApiUtils.apiFetch('/api/templates');
        if (!res.ok) throw new Error('Failed to load templates');
        const templates = await res.json();

        dataLinksCardStrip.innerHTML = '';
        templates.forEach(function(template) {
            const card = document.createElement('a');
            card.className = 'link-card';
            card.target = '_blank';
            card.href = '/static/template-viewer.html?template=' + template.id;

            const iconSpan = document.createElement('span');
            iconSpan.className = 'icon';
            iconSpan.textContent = TEMPLATE_LINK_ICONS[template.template_name] || TEMPLATE_LINK_ICON_FALLBACK;

            const labelSpan = document.createElement('span');
            labelSpan.className = 'label';
            labelSpan.textContent = template.template_name;

            const badgeSpan = document.createElement('span');
            badgeSpan.className = 'badge';
            badgeSpan.textContent = 'Template ' + template.id;

            card.appendChild(iconSpan);
            card.appendChild(labelSpan);
            card.appendChild(badgeSpan);
            dataLinksCardStrip.appendChild(card);
        });
    } catch (error) {
        dataLinksCardStrip.innerHTML = '';
        const errorText = document.createElement('span');
        errorText.style.cssText = 'font-size:13px; color:#94a3b8;';
        errorText.textContent = 'Unable to load data links.';
        dataLinksCardStrip.appendChild(errorText);
    }
}

async function loadVerificationStats() {
    if (verificationStatsGrid) window.LoadingUI.showSpinner(verificationStatsGrid, 'Loading stats');
    try {
        const res = await window.ApiUtils.apiFetch('/api/verification/stats');
        const stats = res.ok ? await res.json() : null;
        if (verificationStatsGrid) window.LoadingUI.hideSpinner(verificationStatsGrid);
        if (stats) {
            document.getElementById('pendingCount').textContent = stats.pending || 0;
            document.getElementById('verifiedToday').textContent = stats.approved || 0;
            document.getElementById('totalVerified').textContent = stats.total || 0;
        } else {
            document.getElementById('pendingCount').textContent = '0';
            document.getElementById('verifiedToday').textContent = '0';
            document.getElementById('totalVerified').textContent = '0';
        }
    } catch (error) {
        if (verificationStatsGrid) window.LoadingUI.hideSpinner(verificationStatsGrid);
        document.getElementById('pendingCount').textContent = '0';
        document.getElementById('verifiedToday').textContent = '0';
        document.getElementById('totalVerified').textContent = '0';
    }
}
