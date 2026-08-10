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
const viewParam = urlSearchParamsForType.get('view');
const currentType = (viewParam === 'bankbook') ? 'bank'
    : (viewParam === 'cashbook') ? 'cash'
    : (urlSearchParamsForType.get('type') === 'bank') ? 'bank' : 'cash';

let currentSessionId = null;
let currentDetailId = null;
// The voucher line uid currently shown in #procedureModal, or null when it's closed — lets
// closeProcedureModal() flush any unsaved observation text without needing a uid parameter,
// since its callers (the ✕ button and the outside-click listener) don't have one either.
let currentProcedureModalUid = null;
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

const statusDiv = document.getElementById('verificationStatus');
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

const supportingModal = document.getElementById('supportingModal');
const supportingSearchInput = document.getElementById('supportingSearchInput');
const supportingSearchBtn = document.getElementById('supportingSearchBtn');
const supportingResults = document.getElementById('supportingResults');
const supportingModalClose = document.getElementById('supportingModalClose');

// ---- PROCEDURE MODAL REFS ----
// Voucher Supporting-Document Linking, Phase 3 of 6: rebuilt as a tabbed modal (one tab per
// document belonging to the voucher line's Category) - see openProcedureModal()/
// renderProcedureModalTabs()/renderProcedureModalPanel() below. The old single-document
// search/select refs (procedureSearchInput/procedureSearchBtn/procedureResults/
// procedureCancelBtn/procedureConfirmBtn/procedureStatus) and their module-level state
// (currentProcedureKey/currentTemplateId/selectedDocId/selectedDocData/currentProcedureTarget)
// are removed along with the corresponding markup - real per-tab search/select content is wired
// back in Phase 4.
const procedureModal = document.getElementById('procedureModal');
const procedureModalTitle = document.getElementById('procedureModalTitle');
const procedureModalClose = document.getElementById('procedureModalClose');

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

function showPopupError(msg) {
    if (!popupError) return;
    popupError.textContent = msg;
    popupError.style.display = 'block';
}

function hidePopupError() {
    if (popupError) popupError.style.display = 'none';
}

function getClientId() {
    if (typeof window.getSelectedClientId === 'function') {
        return window.getSelectedClientId();
    }
    return localStorage.getItem('selectedClientId');
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
    startWrapper.appendChild(label);
    startWrapper.appendChild(mount);
    if (endWrapper.parentElement) endWrapper.parentElement.removeChild(endWrapper);
    return initDateRangePicker(mount, handleCashbookDateRangeChange);
}

// ---------- HARD-REFRESH VIEW PERSISTENCE ----------
// Reflects the current view + its identifying id in the URL (state=<view>&id=<id>&type=<currentType>)
// so a hard refresh can restore the right view (see window.onload below) instead of always dropping
// back to the dashboard. Uses history.replaceState, not pushState — this page already manages its
// own "back" behavior via setBackOverride()/the global navbar arrow, not the browser's native back
// button, so no new history entries should be introduced here. Distinct from the pre-existing
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

// ---------- POPUP: LOAD CASHBOOKS ----------
// overrideStart/overrideEnd (confirmed fix): when provided (the user edited the popup's date
// inputs), search that exact range. When omitted, auto-detect the most recent month that
// actually has cashbook-matching data for this client via GET /api/verification/latest_cashbook_date
// (falls back to today's calendar month if the client has no matching data at all) - replaces the
// old hardcoded "always search the current calendar month" default, which failed whenever a
// client's latest data wasn't in the current month.
async function loadCashbooks(overrideStart, overrideEnd) {
    // Confirmed fix: the two fields are independent — changing the date range (via
    // handlePopupPeriodChange(), which calls this function on every Place-1 range edit) must not
    // reset an already-made cashbook selection. Captured before anything below rebuilds the
    // <select>, then re-applied after the rebuild if it's still a valid option for the new range.
    const previousSelection = popupCashbookSelect.value;
    const clientId = getClientId();
    if (!clientId) {
        popupCashbookSelect.innerHTML = '<option value="">-- Select client first --</option>';
        popupCashbookSelect.disabled = true;
        popupStartBtn.disabled = true;
        return;
    }
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
            const latestRes = await fetch(`${API_BASE}/api/verification/latest_cashbook_date?${latestParams}`);
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
        const res = await fetch(`${API_BASE}/api/verification/cashbooks?${params}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || `Failed to fetch ${bookWord}s`);
        }
        const data = await res.json();
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
    // Global navbar back-arrow should return to the Verification module's entry view (not
    // index.html) while viewing a cashbook's transactions.
    if (typeof setBackOverride === 'function') setBackOverride(exitCashbookView);
    // Bank Verification (Phase 8): relabel the page header to match currentType.
    const headerLabel = document.getElementById('cashbookHeaderLabel');
    if (headerLabel) headerLabel.textContent = currentType === 'bank' ? '🏦 Bankbook:' : '📒 Cashbook:';
    cashbookNameDisplay.textContent = cashbook;
    // Cashbook Preview selector (Phase 1 of 3): relabel to match currentType, and populate the
    // dropdown from the same account list already fetched into popupCashbookSelect for the
    // Start-Verification popup (openCashbook()) or by restoreCashbookView()'s own loadCashbooks()
    // call - both callers guarantee that list is already loaded before this function runs.
    if (cashbookSelectorLabel) cashbookSelectorLabel.textContent = currentType === 'bank' ? '🏦 Bankbook' : '📒 Cashbook';
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
    await fetchCashbookData();
}

// ---------- HARD-REFRESH VIEW PERSISTENCE: CASHBOOK VIEW ----------
// Restores the cashbook (pre-session) view after a hard refresh from ?state=cashbook&id=<name> -
// re-fetches the cashbook list and this cashbook's transactions fresh (via loadCashbooks() and
// activateCashbookView()'s own fetchCashbookData() call), never a cached/frozen snapshot. A
// cashbook name that no longer matches any data isn't treated as a hard failure - fetchCashbookData()
// already renders a graceful "No transactions found" state for that case.
async function restoreCashbookView(cashbookName) {
    if (!getClientId()) {
        showDashboardWithMessage('Please select a client to view this cashbook.');
        return;
    }
    await loadCashbooks();
    await activateCashbookView(cashbookName);
}

// ---------- EXIT CASHBOOK VIEW (return to Verification module's entry view) ----------
// Shared by the global navbar back-arrow override (set in openCashbook()/exitSession()) — there
// is no separate in-body "Back to Verification" link/handler anymore, this is the single place
// that logic lives.
function exitCashbookView() {
    cashbookContent.style.display = 'none';
    dashboardContent.style.display = 'block';
    selectedCashbook = null;
    cashbookTableWrapper.innerHTML = '';
    setVerificationUrl('dashboard');
}

// ---------- CASHBOOK PREVIEW SELECTOR: APPLY ----------
// Phase 2 of 3: the Cashbook/Bankbook dropdown (Phase 1) does nothing by itself - only
// clicking Apply commits its currently selected value together with the current date range,
// in one combined reload (mirroring how Apply already behaves for date-only changes). Updates
// the single-source-of-truth `selectedCashbook` global (also read by startSessionFromCashbook(),
// so starting a session after switching here correctly targets the newly selected account) and
// the page header, then reuses fetchCashbookData() unchanged for the actual reload.
function applyCashbookPreview() {
    if (!cashbookSelector) {
        fetchCashbookData();
        return;
    }
    const chosen = cashbookSelector.value;
    if (chosen && chosen !== selectedCashbook) {
        selectedCashbook = chosen;
        cashbookNameDisplay.textContent = chosen;
    }
    fetchCashbookData();
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
    const params = new URLSearchParams({
        client_id: clientId,
        cashbook_name: selectedCashbook,
        date_start: start,
        date_end: end,
        page: cashbookCurrentPage,
        limit: cashbookPageSize
    });
    try {
        const res = await fetch(`${API_BASE}/api/verification/preview_cashbook?${params}`);
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

// ---------- RENDER CASHBOOK TABLE ----------
function renderCashbookTable(rows) {
    if (!rows || rows.length === 0) {
        cashbookTableWrapper.innerHTML = '<div style="text-align:center; color:#94a3b8; padding:20px;">No data to display.</div>';
        return;
    }
    const firstRow = rows[0].row_data;
    const headers = firstRow ? Object.keys(firstRow) : [];
    let html = `<table style="width:100%; border-collapse:collapse; font-size:13px;">
        <thead style="background:#f8fafc; position:sticky; top:0; z-index:1; border-bottom:2px solid #e2e8f0;">
            <tr>`;
    headers.forEach(h => {
        html += `<th style="padding:6px 10px; text-align:left; font-weight:600; white-space:nowrap;">${h}</th>`;
    });
    html += `</tr></thead><tbody>`;
    rows.forEach(row => {
        html += `<tr style="border-bottom:1px solid #f0f2f5;">`;
        headers.forEach(h => {
            const val = row.row_data[h] !== undefined ? row.row_data[h] : '';
            html += `<td style="padding:6px 10px;">${val}</td>`;
        });
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
    try {
        startVerificationBtn.disabled = true;
        startVerificationBtn.textContent = '⏳ Starting...';
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
        await activateSessionView(data.session_id);
    } catch (error) {
        console.error('Error starting session:', error);
        showStatus('❌ Error starting verification: ' + error.message, 'error');
    } finally {
        startVerificationBtn.disabled = false;
        startVerificationBtn.textContent = '✅ Verification';
    }
}

// Switches to the session view for the given session id — shared by startSessionFromCashbook()
// (new session, just started) and restoreSessionView() (hard-refresh URL restore, below), so both
// paths land on the exact same view/state instead of two parallel implementations that could
// drift apart.
// targetVoucherId (optional): when set, restore directly to that voucher instead of the default
// earliest-Pending jump — used by restoreSessionView()'s hard-refresh restore (see below).
// startSessionFromCashbook() (a freshly-started session) omits this, so its behavior is unchanged.
async function activateSessionView(sessionId, targetVoucherId = null) {
    currentSessionId = sessionId;
    // Explicitly hide the dashboard/module-selection view here too (not just cashbookContent) -
    // matches activateCashbookView()'s own hide of dashboardContent. Previously this only ever ran
    // as the tail of startSessionFromCashbook(), which is always reached after
    // activateCashbookView() had already hidden the dashboard, so the omission was invisible until
    // restoreSessionView() (hard-refresh straight into a session) called this function directly,
    // leaving the dashboard's window.onload default of display:'block' stacked underneath.
    dashboardContent.style.display = 'none';
    cashbookContent.style.display = 'none';
    sessionContent.style.display = 'block';
    // Global navbar back-arrow should return to the Cashbook selection screen (not index.html)
    // while a session is active — reuse the same exit flow as the "Cashbook" button.
    if (typeof setBackOverride === 'function') setBackOverride(exitSession);
    // Remove padding from main container when verification session is active
    const container = document.getElementById('verificationContainer');
    if (container) container.classList.add('verification-active');
    setVerificationUrl('session', sessionId);
    // Load transaction list (without showing popover) to get indices and total
    await loadTransactionList(false);
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
        const res = await fetch(`${API_BASE}/api/verification/audited_cashbook/${sessionId}`);
        if (!res.ok) {
            showDashboardWithMessage('This verification session is no longer available.');
            return;
        }
        const data = await res.json();
        // get_audited_cashbook() nests session fields under data.session, not top-level.
        selectedCashbook = (data.session && data.session.cashbook_name) || null;
        sessionTransactions = data.transactions || [];
        totalTransactions = sessionTransactions.length;
        // Voucher hard-refresh restore: a `voucher` param means the auditor was on a specific
        // voucher (not necessarily the session's earliest-Pending one) when they refreshed.
        const targetVoucherId = new URLSearchParams(window.location.search).get('voucher');
        await activateSessionView(sessionId, targetVoucherId);
    } catch (e) {
        console.error('Error restoring verification session:', e);
        showDashboardWithMessage('Could not restore this verification session.');
    }
}

// ---------- LOAD NEXT TRANSACTION ----------
async function loadNextTransaction(detailId = null) {
    if (!currentSessionId) return;
    try {
        isNavigating = true;
        const url = `${API_BASE}/api/verification/next/${currentSessionId}`;
        const res = await fetch(url);
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
        renderVoucherLines(voucherLines);
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
// completeTransaction(), exitSession()), so a manual save had no effect a user could rely on. The
// per-line collapsed/expand rows (renderVoucherLines()) own everything else below this header.
function renderVoucherHeader(voucherId, voucherDate, lines) {
    const bar = document.getElementById('voucherHeaderBar');
    if (!bar) return;
    const lineCount = lines ? lines.length : 0;
    const total = computeVoucherTotal(lines);
    // Phase B: disabled state is voucher-level (getVoucherPosition()), not line-level — a
    // multi-line voucher no longer has to be "the last line" for Next to disable correctly.
    // Position unknown (sessionTransactions not loaded yet) defaults both disabled, same safe
    // default the previous line-based check already fell into before sessionTransactions loaded.
    const pos = getVoucherPosition();
    const prevDisabled = !(pos.total > 0 && pos.index > 1);
    const nextDisabled = !(pos.total > 0 && pos.index < pos.total);
    bar.innerHTML = `
        <div class="verification-header-row voucher-nav-row">
            <button class="btn-voucher-nav" id="prevArrow" ${prevDisabled ? 'disabled' : ''} title="Previous transaction">← Prev voucher</button>
            <div class="voucher-title-block">
                <div class="voucher-title">
                    Voucher ${voucherId || '—'}
                    <span class="vh-icon" id="transactionListIcon" title="View all vouchers" style="cursor:pointer;">
                        <span class="vh-icon-symbol">🔍</span>
                    </span>
                </div>
                <div class="voucher-subtitle">${voucherDate || '—'} · ${lineCount} line${lineCount === 1 ? '' : 's'} · Amount ${total}<span id="voucherPositionCounter"></span></div>
            </div>
            <button class="btn-voucher-nav" id="nextArrow" ${nextDisabled ? 'disabled' : ''} title="Next transaction">Next voucher →</button>
        </div>
    `;

    const cashbookBar = document.getElementById('cashbookBar');
    if (cashbookBar) {
        const cashbookName = window._currentCashbook || selectedCashbook || '—';
        const stats = computeVoucherStats();
        // Bank Verification (Phase 8): "Cashbook"/📒 vs "Bankbook"/🏦 label, driven by currentType.
        const bookLabel = currentType === 'bank' ? 'Bankbook' : 'Cashbook';
        const bookIcon = currentType === 'bank' ? '🏦' : '📒';
        cashbookBar.innerHTML = `
            <div class="cashbook-stats">
                <span class="cashbook-stat"><span class="cashbook-stat-label">${bookLabel}</span><span class="cashbook-stat-value">${cashbookName}</span></span>
                <span class="cashbook-stat"><span class="cashbook-stat-label">Total Vouchers</span><span class="cashbook-stat-value">${stats.totalVouchers}</span></span>
                <span class="cashbook-stat"><span class="cashbook-stat-label">Verified</span><span class="cashbook-stat-value">${stats.verifiedVouchers}</span></span>
            </div>
            <button class="btn-cashbook" id="cashbookReturnBtn"><span class="btn-icon">${bookIcon}</span> ${bookLabel}</button>
        `;
        document.getElementById('cashbookReturnBtn')?.addEventListener('click', function() {
            if (typeof exitSession === 'function') exitSession();
        });
    }

    // ----- Event wiring (rebound after every innerHTML write, matching this file's convention) -----
    document.getElementById('prevArrow')?.addEventListener('click', function() {
        if (typeof goToPreviousTransaction === 'function') goToPreviousTransaction();
    });
    document.getElementById('nextArrow')?.addEventListener('click', function() {
        if (typeof goToNextTransaction === 'function') goToNextTransaction();
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
}

// ---------- VOUCHER LINE ALIAS HELPERS (Phase 3) ----------
// Small, shared alias lookups reused by renderVoucherLines() — mirrors the alias arrays already
// used inside renderTransaction(), kept local to avoid touching that function (left untouched
// this phase per the confirmed plan).
const VOUCHER_LINE_ACCOUNT_CODE_ALIASES = ['Account Detail', 'Account Details', 'Account Code', 'Account No', 'Account Number', 'Code', 'Ledger Code', 'AccountID', 'Account Head', 'Ledger Detail'];
const VOUCHER_LINE_ACCOUNT_NAME_ALIASES = ['Account Name', 'Account', 'Account Detail', 'Ledger', 'Head'];

// Validation Rules Phase 5: synthetic id for the procedure modal's non-document "Voucher Line"
// tab (Prepared/Checked/Approved By) — never a real audit_rule_documents.id, so it can't collide.
const VOUCHER_LINE_TAB_ID = '__voucher_line__';

// Account Head Recommendation follow-up: reserved sentinel document_id for the "no suitable TB
// account" note, reused via the existing save_line_observation/voucher_line_observations flow.
// Must match ACCOUNT_HEAD_OBSERVATION_DOC_ID in routers/verification.py exactly.
const ACCOUNT_HEAD_OBSERVATION_DOC_ID = -1;

// Voucher-level Checks simplified to name-only (Phase 3 of 4) — voucher-level checks cache,
// keyed by transaction_ref (shared across every line of the same voucher, not per-uid) — array
// of { id, name, sort_order, state }. `state` is one of 'untouched'/'done'/'not_done' (Phase 2's
// 3-state model); both tick and cross save immediately server-side, so there is no local-first
// `dirty` field anymore. Fetched once per voucher in openProcedureModal(), reused across lines of
// that same voucher within one modal session.
let voucherChecksCache = {};
// Confirmed fix (2026-07-30): keyed by transaction_ref, holds the in-flight fetch Promise while
// one is outstanding for that voucher — see ensureVoucherChecksLoaded() below for why this exists.
let voucherChecksLoadingPromises = {};
const VOUCHER_LINE_DESCRIPTION_ALIASES = ['Description', 'Voucher Detail', 'Narration', 'Remarks', 'Particulars'];
const VOUCHER_LINE_DEBIT_ALIASES = ['Debit Amount', 'Debit', 'Dr Amount', 'Dr'];
const VOUCHER_LINE_CREDIT_ALIASES = ['Credit Amount', 'Credit', 'Cr Amount', 'Cr'];

// Account Head Recommendation (Validation Rules Phase 7, extended by the reposition/search-select/
// no-match follow-up) — keyed by uid (per voucher line, not shared across lines like
// voucherChecksCache, since suggestions are description-driven).
// `mode`: 'match' (default, search-select over Trial Balance leaf accounts — scratch-only, no
// backend persistence, confirmed) or 'nomatch' (free-text finding — IS persisted, via
// flushAccountHeadNoMatchNote()/ACCOUNT_HEAD_OBSERVATION_DOC_ID). `selectedAccountId`/
// `selectedAccountName` replace the old free-text `recommendedValue` for match mode.
// `noMatchText`/`noMatchDirty` back the no-match textarea; noMatchText is re-seeded from server
// truth (the sentinel voucher_line_observations entry) on every openProcedureModal() call, same
// "never trust stale local state across a reopen" convention every other tab already follows.
const accountHeadCache = {};
function getAccountHeadState(uid) {
    if (!accountHeadCache[uid]) {
        accountHeadCache[uid] = {
            mode: 'match',
            selectedAccountId: null,
            selectedAccountName: '',
            searchTerm: '',
            suggestions: [], matched: null, message: '', loading: false, hasFetched: false,
            noMatchText: '',
            noMatchDirty: false
        };
    }
    return accountHeadCache[uid];
}

// Trial Balance leaf accounts for the Account Head search-select — warmed once per modal open
// (openProcedureModal()) via GET /api/trial-balance/latest-accounts, same source Assign
// Categories already uses. That endpoint returns both parent and leaf accounts, so is_leaf
// filtering happens here, client-side; not pre-filtered server-side.
let latestTbAccountsCache = null;
async function fetchLatestTbAccounts(clientId) {
    if (!clientId) {
        latestTbAccountsCache = [];
        return latestTbAccountsCache;
    }
    try {
        const res = await fetch(`${API_BASE}/api/trial-balance/latest-accounts?client_id=${encodeURIComponent(clientId)}`);
        if (!res.ok) {
            latestTbAccountsCache = [];
            return latestTbAccountsCache;
        }
        const data = await res.json();
        latestTbAccountsCache = (data.accounts || []).filter(a => a.is_leaf);
    } catch (e) {
        console.error('Failed to load Trial Balance leaf accounts:', e);
        latestTbAccountsCache = [];
    }
    return latestTbAccountsCache;
}

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
// Type dropdown stores) — shared between the "Guide" popover and the Supporting column so
// selecting a category doesn't trigger two separate fetches for the same data. 404s cache as
// null; a transient fetch error is deliberately NOT cached, so a later retry can succeed.
// openProcedureModal() passes forceRefresh=true to bypass this cache — its Document list and
// per-Document auth_roles (Prepared by/Checked by/Approved by) must reflect Audit Rules' live
// state on every open, not whatever this Category last returned earlier in the page's lifetime.
let auditGuidanceCache = {};

async function fetchAuditGuidanceData(type, forceRefresh = false) {
    if (!forceRefresh && Object.prototype.hasOwnProperty.call(auditGuidanceCache, type)) {
        return auditGuidanceCache[type];
    }
    const res = await fetch(`${API_BASE}/api/verification/audit_guidance?transaction_type=${encodeURIComponent(type)}`);
    if (res.status === 404) {
        auditGuidanceCache[type] = null;
        return null;
    }
    if (!res.ok) throw new Error('Failed to fetch audit guidance');
    const data = await res.json();
    auditGuidanceCache[type] = data;
    return data;
}

// Confirmed fix (Linked Evidence table dynamic columns): each Document's evidence table needs
// its own template's actual configured columns (name + data_type, sort_order), not a one-size-
// fits-all set. Cached by template_id — several Documents can share the same template, and a
// Category's Documents don't change template mid-session, so no forceRefresh option is needed
// here (unlike auditGuidanceCache above). Fails soft to [] on error/404 so a lookup hiccup never
// blocks the modal from opening; buildSelectedLinesTable() falls back to deriving columns from
// the selections themselves when this returns empty.
let templateColumnsCache = {};

// GRN-to-Vendor-Bill copy: a sibling cache keyed the same way as templateColumnsCache, populated
// by the same fetchTemplateColumns() call (no extra network round trip) — lets
// buildSelectedLinesTable() tell a Store Receiving/GRN document's evidence table apart from any
// other template's without a new endpoint.
let templateNameCache = {};

async function fetchTemplateColumns(templateId) {
    if (Object.prototype.hasOwnProperty.call(templateColumnsCache, templateId)) {
        return templateColumnsCache[templateId];
    }
    try {
        const res = await fetch(`${API_BASE}/api/templates/${templateId}`);
        if (!res.ok) {
            templateColumnsCache[templateId] = [];
            templateNameCache[templateId] = '';
            return [];
        }
        const data = await res.json();
        const columns = (data.columns || []).map(c => ({ column_name: c.column_name, data_type: c.data_type }));
        templateColumnsCache[templateId] = columns;
        templateNameCache[templateId] = data.template_name || '';
        return columns;
    } catch (e) {
        console.error('Failed to load template columns:', e);
        templateColumnsCache[templateId] = [];
        templateNameCache[templateId] = '';
        return [];
    }
}

// Fetch of a line's active assignment records — used to determine which documents count as
// "complete" for the Procedures ring+badge below. Fails soft to [] so a fetch hiccup never breaks
// the cell itself.
async function fetchAssignedDocumentRecords(uid) {
    try {
        const res = await fetch(`${API_BASE}/api/verification/voucher_line_documents/${uid}`);
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
        const res = await fetch(`${API_BASE}/api/verification/procedure_checks/${uid}`);
        if (!res.ok) return [];
        const data = await res.json();
        return data.checks || [];
    } catch (e) {
        console.error('Failed to load procedure checks for Procedures cell:', e);
        return [];
    }
}

// Fetch of a line's saved Physical Bill entries, across all documents — shared by
// openProcedureModal() (Phase 7) and updateProceduresCellForLine()'s standalone completion check
// below (Phase: rail dot + Procedures badge amount-match fix). Same fail-soft-to-[] convention as
// fetchAssignedDocumentRecords()/fetchProcedureChecksForLine().
async function fetchPhysicalBillsForLine(uid) {
    try {
        const res = await fetch(`${API_BASE}/api/verification/physical_bills/${uid}`);
        if (!res.ok) return [];
        const data = await res.json();
        return data.bills || [];
    } catch (e) {
        console.error('Failed to load physical bills for Procedures cell:', e);
        return [];
    }
}

// Small SVG completion ring, adapted from the v7 prototype's ring() helper — pct is 0-100, cls is
// 'pending'/'progress'/'done' (drives the stroke color via the CSS class, matching this file's
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
async function updateProceduresCellForLine(uid, type) {
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
    let data = null;
    try {
        data = await fetchAuditGuidanceData(type);
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
        fetchAssignedDocumentRecords(uid),
        fetchProcedureChecksForLine(uid),
        needsVoucherChecks ? ensureVoucherChecksLoaded(getClientId(), currentVoucherId) : Promise.resolve(null),
        needsPhysicalBills ? fetchPhysicalBillsForLine(uid) : Promise.resolve([])
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
            const bill = physicalBills.find(b => b.document_id === doc.id);
            const fieldsComplete = !!(bill && (bill.bill_no || '').trim() && (bill.bill_date || '').trim()
                && (bill.vendor_name || '').trim() && bill.amount !== '' && bill.amount != null && !isNaN(parseFloat(bill.amount)));
            base = fieldsComplete && Math.abs(parseFloat(bill.amount) - voucherLineAmount) <= 0.005;
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
            openProcedureModal(this.dataset.uid);
        });
        return;
    }

    const done = documents.filter(isDocDone).length;
    const total = documents.length;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const cls = done === 0 ? 'pending' : (done === total ? 'done' : 'progress');

    cell.innerHTML = `
        <span class="voucher-line-procedures-badge ${cls}" data-uid="${uid}" title="${done} of ${total} documents complete">
            ${buildProcedureRingSvg(pct, cls)}${done}/${total}
        </span>
    `;
    cell.querySelector('.voucher-line-procedures-badge')?.addEventListener('click', function(e) {
        e.stopPropagation();
        openProcedureModal(this.dataset.uid);
    });
}

// Fetch of a line's saved observation entries (one per document with a non-empty observation) —
// used only for the Observations column's count/tooltip. Fails soft to [] so a fetch hiccup never
// breaks the cell itself.
async function fetchLineObservationEntries(uid) {
    try {
        const res = await fetch(`${API_BASE}/api/verification/line_observations/${uid}`);
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
async function updateObservationsCellForLine(uid, type) {
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
            data = await fetchAuditGuidanceData(type);
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
            openProcedureModal(this.dataset.uid);
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
        openProcedureModal(this.dataset.uid);
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
function applyCategoryToTypeCell(uid, categoryName) {
    const valueEl = document.querySelector(`.voucher-line-type-value[data-uid="${uid}"]`);
    const iconEl = document.querySelector(`.voucher-line-type-recycle-icon[data-uid="${uid}"]`);
    if (!valueEl) return; // row no longer rendered

    if (!voucherLineState[uid]) voucherLineState[uid] = { transactionType: '', procedureData: {} };
    voucherLineState[uid].transactionType = categoryName || '';
    voucherLineState[uid].procedureData.transaction_type = categoryName || '';

    valueEl.textContent = categoryName || 'Unassigned';
    if (iconEl) iconEl.style.display = categoryName ? 'none' : 'inline-flex';

    // Procedures/Observations cells start empty in the template and are only ever populated once
    // the category resolves — same "only call on a real match" convention the old Supporting cell
    // used, so an Unassigned line's cells simply stay blank rather than being explicitly cleared.
    if (categoryName) {
        updateProceduresCellForLine(uid, categoryName);
        updateObservationsCellForLine(uid, categoryName);
        updateRiskCellForLine(uid, categoryName);
    } else {
        applyRiskToCell(uid, null);
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
async function updateRiskCellForLine(uid, type) {
    if (!type) {
        applyRiskToCell(uid, null);
        return;
    }
    let data = null;
    try {
        data = await fetchAuditGuidanceData(type);
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
        applyCategoryToTypeCell(uid, null);
        return;
    }

    let categoryName = null;
    try {
        if (Object.prototype.hasOwnProperty.call(categoryForAccountCache, accountCode)) {
            categoryName = categoryForAccountCache[accountCode];
        } else {
            const clientId = getClientId();
            const params = new URLSearchParams({ client_id: clientId, account_code: accountCode });
            const res = await fetch(`${API_BASE}/api/verification/category-for-account?${params}`);
            if (!res.ok) throw new Error('Failed to look up category for account');
            const data = await res.json();
            categoryName = data.matched ? data.category_name : null;
            categoryForAccountCache[accountCode] = categoryName;
        }
    } catch (e) {
        console.error('Category auto-fill lookup failed:', e);
        categoryName = null; // fail safe to unassigned — not cached, so a later attempt can retry
    }

    applyCategoryToTypeCell(uid, categoryName);
}

// Order matches .voucher-line-summary-grid's columns: Sr No., Account Code, Account Type, Account
// Name, Amount, Procedures, Observations, Status. User-resizable (see the drag handles in
// renderVoucherLines()'s header row) — persists at module scope across renders/navigation, same
// convention as voucherLineState, so dragging a column survives Prev/Next voucher navigation.
let voucherColumnWidths = [40, 100, 170, 395, 90, 130, 300, 90];

// Pushes voucherColumnWidths into the CSS custom property .voucher-line-summary-grid reads its
// grid-template-columns from. Set on #voucherLinesContainer itself (not its innerHTML content),
// so it survives every renderVoucherLines() re-render without needing to be reapplied — called
// anyway at the top of renderVoucherLines() to stay in sync on first render.
function applyVoucherColumnWidths() {
    const container = document.getElementById('voucherLinesContainer');
    if (container) container.style.setProperty('--voucher-col-widths', voucherColumnWidths.map(w => w + 'px').join(' '));
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
    return `<span class="voucher-line-status-dot ${statusClass}" title="${status}"></span>`;
}

// Suppresses the whole Pending pill (dot + text) once a line's cleared_status is Cleared — the
// Cleared/Query badge already says so, and a "pending" dot no longer reflects reality once the
// required checks are satisfied. Only the Pending case is affected; Failed/Passed pills are
// always shown as-is (icon + text, unchanged).
function getVoucherLineStatusPillHtml(validationStatus, clearedStatus) {
    const status = (validationStatus || 'Pending').toLowerCase();
    if (status === 'pending' && clearedStatus === 'Cleared') return '';
    return `${getVoucherLineStatusIcon(validationStatus)} ${status}`;
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
// gate identically.
const LSR_GATE_ICON = { pass: '✔', fail: '✘', not_applicable: '–' };
function renderGateRowHtml(g) {
    return `
        <div class="lsr-gate ${g.status}">
            <div class="lsr-gate-label"><span class="lsr-gate-icon">${LSR_GATE_ICON[g.status] || '–'}</span>${escapeHtml(g.label)}</div>
            ${g.detail ? `<div class="lsr-gate-detail">${escapeHtml(g.detail)}</div>` : ''}
        </div>
    `;
}

// One row per gate. Failing gates are already ordered first by the endpoint — this only renders.
function buildLineStatusReasonBody(data) {
    if (data.reason) {
        return `<p class="lsr-reason">${escapeHtml(data.reason)}</p>`;
    }
    const gates = data.gates || [];
    if (!gates.length) {
        return '<p class="lsr-reason">No gate breakdown available for this line.</p>';
    }
    return gates.map(renderGateRowHtml).join('');
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
        const response = await fetch(`${API_BASE}/api/verification/line_status_reason/${uid}`);
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

// ---------- "Why is this line Cleared/Query?" inline panel (Supporting Documents modal) ----------
// Same GET /api/verification/line_status_reason/{id} payload as the Status-pill popover above,
// and reuses that popover's own buildLineStatusReasonBody() for the gate rows — one rendering
// path, not two. Toggled by #procedureModalValidateBtn; collapsed by default and reset on every
// openProcedureModal() (see the hideValidatePanel() call there) so stale gate data from a
// previous line can never carry over.
let validatePanelLoadingUid = null;
// Local-only view state for this panel: the last fetched payload (so the N/A toggle can re-render
// without refetching) and whether N/A gates are currently revealed. Both reset every time the
// panel is closed/reopened — never persisted, matching hideValidatePanel()'s existing reset rule.
let validatePanelData = null;
let validatePanelShowNA = false;

function hideValidatePanel() {
    const panel = document.getElementById('procedureValidatePanel');
    if (panel) {
        panel.style.display = 'none';
        panel.innerHTML = '';
    }
    validatePanelLoadingUid = null;
    validatePanelData = null;
    validatePanelShowNA = false;
}

// Splits gates into visible (pass/fail/query) vs N/A, hiding N/A by default behind a
// "Show N/A (n)" toggle link. Reuses renderGateRowHtml() — same row markup as the popover.
function buildValidatePanelBody(data) {
    if (data.reason) {
        return `<p class="lsr-reason">${escapeHtml(data.reason)}</p>`;
    }
    const gates = data.gates || [];
    if (!gates.length) {
        return '<p class="lsr-reason">No gate breakdown available for this line.</p>';
    }
    const visible = gates.filter(g => g.status !== 'not_applicable');
    const naGates = gates.filter(g => g.status === 'not_applicable');

    let html = visible.length
        ? visible.map(renderGateRowHtml).join('')
        : '<p class="lsr-reason">No applicable checks for this line.</p>';

    if (naGates.length) {
        if (validatePanelShowNA) {
            html += naGates.map(renderGateRowHtml).join('');
            html += `<a href="#" id="procedureValidateNAToggle" class="procedure-validate-na-toggle">Hide N/A (${naGates.length})</a>`;
        } else {
            html += `<a href="#" id="procedureValidateNAToggle" class="procedure-validate-na-toggle">Show N/A (${naGates.length})</a>`;
        }
    }
    return html;
}

function renderValidatePanel() {
    const panel = document.getElementById('procedureValidatePanel');
    if (!panel || !validatePanelData) return;
    const data = validatePanelData;
    const status = data.cleared_status || 'Not computed';
    const pillClass = data.cleared_status === 'Cleared' ? 'cleared' : (data.cleared_status === 'Query' ? 'query' : 'unknown');
    panel.innerHTML = `
        <div class="procedure-validate-panel-header">
            <span class="procedure-validate-status-pill ${pillClass}">${escapeHtml(status)}</span>
        </div>
        <div class="procedure-validate-panel-body">${buildValidatePanelBody(data)}</div>
    `;
    const toggleLink = panel.querySelector('#procedureValidateNAToggle');
    if (toggleLink) {
        toggleLink.addEventListener('click', function(e) {
            e.preventDefault();
            validatePanelShowNA = !validatePanelShowNA;
            renderValidatePanel();
        });
    }
}

async function toggleValidatePanel() {
    const panel = document.getElementById('procedureValidatePanel');
    if (!panel) return;
    if (panel.style.display !== 'none') {
        hideValidatePanel();
        return;
    }
    const uid = currentProcedureModalUid;
    if (!uid) return;

    panel.innerHTML = '<p class="lsr-reason">Loading…</p>';
    panel.style.display = 'block';
    validatePanelLoadingUid = uid;

    try {
        const response = await fetch(`${API_BASE}/api/verification/line_status_reason/${uid}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        // Discard a stale response if the panel was closed or the modal moved to a different
        // line while this fetch was in flight.
        if (validatePanelLoadingUid !== uid) return;
        validatePanelData = data;
        validatePanelShowNA = false;
        renderValidatePanel();
    } catch (e) {
        if (validatePanelLoadingUid !== uid) return;
        console.warn('Unable to load line status reason:', e);
        panel.innerHTML = '<p class="lsr-reason">Unable to load the status breakdown.</p>';
    }
}

// ---------- VOUCHER LINE STATE (Phase 3) ----------
// Seeds voucherLineState fresh from a voucher's lines. Accepts either a /next line object
// (has transaction_type + procedure_data + change_detection_status/old_row_data) or an
// audited_cashbook line object (has procedure_flags, no transaction_type/change-detection fields)
// — same fallback pattern navigateToTransaction() already uses for the single-active-line
// globals, applied per line here.
function seedVoucherLineState(lines) {
    voucherLineState = {};
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
    // Phase 4: keeps the full lines array (row_data included) so openProcedureModal() can look up
    // a line's own amount (via the existing getVoucherLineAmount()/parseVoucherAmount() helpers)
    // for the selected-lines table's mismatch badge, without a second fetch.
    currentVoucherLines = lines || [];
    (lines || []).forEach(line => {
        let procedureData = line.procedure_flags ? { ...line.procedure_flags } : {};
        if (line.procedure_data) procedureData = { ...procedureData, ...line.procedure_data };
        const transactionType = line.transaction_type || procedureData.transaction_type || '';
        voucherLineState[line.detail_id] = { transactionType, procedureData, lineItems: line.line_items || [] };
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

function renderVoucherLines(lines) {
    const container = document.getElementById('voucherLinesContainer');
    if (!container) return;
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
            <span>Account Code<span class="voucher-col-resize-handle" data-col="1"></span></span>
            <span>Account Type<span class="voucher-col-resize-handle" data-col="2"></span></span>
            <span>Account Name<span class="voucher-col-resize-handle" data-col="3"></span></span>
            <span class="voucher-line-amount-col">Amount<span class="voucher-col-resize-handle" data-col="4"></span></span>
            <span>Procedures<button type="button" class="voucher-col-toggle-icon" title="Expand">⤢</button><span class="voucher-col-resize-handle" data-col="5"></span></span>
            <span>Observations<button type="button" class="voucher-col-toggle-icon" title="Expand">⤢</button><span class="voucher-col-resize-handle" data-col="6"></span></span>
            <span>Status<span class="voucher-col-resize-handle" data-col="7"></span></span>
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
        const accountCode = getVoucherLineAlias(row, VOUCHER_LINE_ACCOUNT_CODE_ALIASES);
        lineAccountCodes.push({ uid, accountCode });
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
                    <span class="voucher-line-code">${accountCode}</span>
                    <span class="voucher-line-type-cell">
                        <span class="voucher-line-type-value-row">
                            <span class="voucher-line-type-value" data-uid="${uid}">Loading…</span>
                            <span class="voucher-line-type-recycle-icon" data-uid="${uid}" data-account-code="${escapeHtml(accountCode)}" title="No category assigned — click to assign one in Assign Categories" style="display:none">🔄</span>
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
    // loop that read a manually-saved value. Fire-and-forget per line; each call safely no-ops if
    // its row is no longer in the DOM by the time the lookup resolves.
    lineAccountCodes.forEach(({ uid, accountCode }) => {
        autoFillCategoryForLine(uid, accountCode);
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
    // border only resizes that column — the table's total width changes with it (no neighbor
    // compensation), scrolling horizontally via #voucherLinesContainer's overflow-x if it grows
    // past the screen. Widths live in voucherColumnWidths (module scope), so they persist across
    // re-renders/voucher navigation.
    container.querySelectorAll('.voucher-col-resize-handle').forEach(handle => {
        handle.addEventListener('mousedown', function(e) {
            e.preventDefault();
            e.stopPropagation();
            const colIndex = parseInt(this.dataset.col);
            const startX = e.clientX;
            const startWidth = voucherColumnWidths[colIndex];
            this.classList.add('resizing');

            // Small floating "Width: N px" readout that follows the cursor while dragging, same
            // idea as Excel's own column-resize tooltip — created fresh per drag, removed on
            // mouseup so it never lingers in the DOM.
            const tooltip = document.createElement('div');
            tooltip.className = 'voucher-col-resize-tooltip';
            tooltip.textContent = `Width: ${startWidth} px`;
            tooltip.style.left = `${e.clientX + 12}px`;
            tooltip.style.top = `${e.clientY - 28}px`;
            document.body.appendChild(tooltip);

            const onMouseMove = (e2) => {
                const newWidth = Math.max(40, startWidth + (e2.clientX - startX));
                voucherColumnWidths[colIndex] = newWidth;
                applyVoucherColumnWidths();
                tooltip.textContent = `Width: ${newWidth} px`;
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

// Update the voucher position counter (small "· voucher n/total" suffix on the header subtitle,
// written by renderVoucherHeader()).
async function updateProgressDisplay() {
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
                        const res = await fetch(`${API_BASE}/api/verification/session/${currentSessionId}/progress`);
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
                counterEl.textContent = total > 0 ? ` · voucher ${displayIndex}/${total}` : '';
            } else {
                const displayIndex = (voucherIndex >= 0) ? voucherIndex + 1 : '?';
                counterEl.textContent = ` · voucher ${displayIndex}/${voucherTotal}`;
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

async function loadTransactionList(showPopover = false) {
    if (!currentSessionId) {
        showStatus('No active session.', 'error');
        return;
    }

    // If already loaded and we want to show popover, just render it
    if (sessionTransactions.length > 0) {
        if (showPopover) {
            renderTransactionPopover();
        }
        return;
    }

    // Fetch all transactions for this session
    try {
        const res = await fetch(`${API_BASE}/api/verification/audited_cashbook/${currentSessionId}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch transaction list');
        }
        const data = await res.json();
        sessionTransactions = data.transactions || [];
        // Also store the current index based on currentDetailId
        if (currentDetailId) {
            currentTransactionIndex = sessionTransactions.findIndex(t => t.detail_id === currentDetailId);
        }
        // Update totalTransactions if not set
        if (totalTransactions === 0 && sessionTransactions.length > 0) {
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

// Builds the status icon span shared by every transaction row in the popover.
function buildPopoverStatusIcon(validationStatus) {
    const status = validationStatus || 'Pending';
    let statusIcon = '⏳';
    if (status === 'Passed') statusIcon = '✅';
    else if (status !== 'Pending') statusIcon = '❌';
    return `<span class="status-icon ${status.toLowerCase()}">${statusIcon}</span>`;
}

// Returns the subset of sessionTransactions whose Voucher ID, Date, Account Name, Amount, or
// validation Status contains the search term (case-insensitive substring match). Reuses the same
// field-extraction logic renderTransactionPopover() uses per line (getVoucherLineAmount(), the
// same rowData account-name fallback chain, the same default-to-'Pending' status) so results are
// guaranteed consistent with what's actually displayed in the table.
function filterTransactionsForPopover(term) {
    const needle = term.trim().toLowerCase();
    if (!needle) return sessionTransactions;
    return sessionTransactions.filter(t => {
        const rowData = t.row_data || {};
        const accountName = rowData['Account Name'] || rowData['Account'] || rowData['Ledger'] || '';
        const amount = getVoucherLineAmount(rowData);
        const status = t.validation_status || 'Pending';
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
                <td style="text-align:center;">${buildPopoverStatusIcon(t.validation_status)}</td>
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
    renderVoucherLines(voucherLines);

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
function isCurrentVoucherValidated() {
    if (!sessionTransactions.length || !currentVoucherId) return false;
    const voucherLines = sessionTransactions.filter(t => t.voucher_id === currentVoucherId);
    if (!voucherLines.length) return false;
    return voucherLines.every(t => {
        const status = (t.validation_status || '').toLowerCase();
        return status === 'passed' || status === 'verified';
    });
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
    renderVoucherLines(voucherLines);
    return true;
}

// Phase B: fetches the whole adjacent voucher (Phase A's /voucher_nav) and renders it via the
// same pipeline loadNextTransaction() already uses for /next's voucher-shaped response. Returns
// true on success, false on a boundary (no more vouchers that way) or fetch error — both handled
// silently, matching this file's existing silent-boundary convention (e.g. the old "last
// transaction" no-op).
async function moveToAdjacentVoucher(direction) {
    if (!currentSessionId || !currentDetailId) return false;
    try {
        const url = `${API_BASE}/api/verification/voucher_nav/${currentSessionId}?current_detail_id=${currentDetailId}&direction=${direction}`;
        const res = await fetch(url);
        if (!res.ok) return false;
        const data = await res.json();
        if (data.boundary) return false;
        if (!applyVoucherNavRenderData(data)) return false;
        await updateProgressDisplay();
        return true;
    } catch (e) {
        console.error('Error navigating to adjacent voucher:', e);
        return false;
    }
}

// Go to previous voucher – with confirmation if not validated
async function goToPreviousTransaction() {
    const pos = getVoucherPosition();
    if (pos.total > 0 && pos.index <= 1) {
        // No message – silently ignore (already at the first voucher)
        return;
    }
    // Save current state first (always)
    await saveCurrentTransaction();
    // If not validated, ask for confirmation
    if (!isCurrentVoucherValidated()) {
        const confirmed = await showNavigationConfirm();
        if (!confirmed) {
            // User cancelled – no message
            return;
        }
    }
    await moveToAdjacentVoucher('prev');
}

// Go to next voucher – with confirmation if not validated
async function goToNextTransaction() {
    // Silently no-op at the last voucher — the Next button is already disabled there, so
    // this restates nothing new to the user.
    const pos = getVoucherPosition();
    if (pos.total > 0 && pos.index >= pos.total) {
        return;
    }
    // Save current state
    await saveCurrentTransaction();
    // If not validated, ask for confirmation
    if (!isCurrentVoucherValidated()) {
        const confirmed = await showNavigationConfirm();
        if (!confirmed) {
            // Cancelled — stay on the current voucher, no toast (nothing changed for the user
            // to be informed about).
            return;
        }
    }
    // Proceed: validate and move (if validation passes, mark verified; if it fails, still allow
    // navigation since the user already confirmed they want to proceed).
    await validateTransaction(); // updates the active line's .voucher-line-status color/icon/label
    // Check if it passed, if yes mark verified.
    const statusCell = document.querySelector(`.voucher-line-row[data-detail-id="${currentDetailId}"] .voucher-line-status`);
    if (statusCell && statusCell.dataset.validationStatus === 'passed') {
        await completeTransaction(); // marks verified and calls loadNextTransaction
    } else {
        // Even if validation failed, we move to next because user confirmed
        const moved = await moveToAdjacentVoucher('next');
        if (!moved) {
            showStatus('Next transaction not found.', 'error');
        }
    }
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
async function saveLineItems(detailId = currentDetailId) {
    if (!detailId) return;
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
        const payload = {
            detail_id: detailId,
            line_items: state.lineItems || [],
            transaction_type: state.transactionType,
            procedure_data: state.procedureData
        };
        const res = await fetch(`${API_BASE}/api/verification/save_line_items`, {
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
            }
            updateClearedStatusBadgeForLine(detailId, data.cleared_status);
        }
        // Success – no status bar message
    } catch (e) {
        console.error('Error saving:', e);
        // Do not show status bar error – only log
    }
}

// ---------- SAVE ALL VOUCHER LINES (Phase 4) ----------
// Saves every line currently loaded in the voucher via the existing save_line_items endpoint, one
// call per line, instead of a single call for just the active line. Called automatically before
// Prev/Next navigation, on complete, and on exit (see saveCurrentTransaction(),
// completeTransaction(), exitSession()) so edits on any expanded line aren't lost — there is no
// manual save button.
async function saveAllVoucherLines() {
    const uids = Object.keys(voucherLineState);
    for (const uid of uids) {
        await saveLineItems(uid);
    }
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

// ---------- EXIT SESSION ----------
// This is what runs on both the "🏦/📒 Cashbook" button and the global navbar back-arrow (its
// setBackOverride() target, set in activateSessionView()). Returning to the cashbook view now
// reuses restoreCashbookView() — the exact same fresh load/render path a hard refresh into
// ?state=cashbook goes through (loadCashbooks() then activateCashbookView(), which itself handles
// hiding dashboard/session, showing cashbookContent, the back-override, header labels, the
// cashbook selector, date-range defaults, the URL, and fetching data) — instead of manually
// toggling visibility and calling fetchCashbookData() directly. The manual version rendered a
// blank cashbook whenever the session had been reached via a direct ?state=session URL restore
// (restoreSessionView()), since that path never populates cashbookStartDate/cashbookEndDate/the
// selector - fetchCashbookData() would then bail out early on an empty date range.
async function exitSession() {
    if (confirm('Are you sure you want to exit this verification session? Progress will be saved.')) {
        saveAllVoucherLines(); // Phase 4: saves every line, not just the active one
        clearVoucherHeader();
        currentSessionId = null;
        currentDetailId = null;
        // Restore padding when session ends
        const container = document.getElementById('verificationContainer');
        if (container) container.classList.remove('verification-active');
        await restoreCashbookView(selectedCashbook);
    }
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
        const res = await fetch(`${API_BASE}/api/verification/supporting?${params}`);
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
    loadCashbooks();
}

// ---------- EVENT LISTENERS ----------
// Verification hub tiles: Cash/Bank navigate with ?type=, deliberately re-entering this same
// page through the existing window.onload auto-open path (openSessionPopup fires on load when
// a type param is present) — no separate in-page open logic, one entry path for both the
// dashboard's cards (index.html) and these hub tiles. "Coming soon" tiles have no handler.
const cashVoucherTile = document.getElementById('cashVoucherTile');
if (cashVoucherTile) {
    cashVoucherTile.addEventListener('click', function() {
        window.location.href = '/static/verification.html?type=cash';
    });
}
const bankVoucherTile = document.getElementById('bankVoucherTile');
if (bankVoucherTile) {
    bankVoucherTile.addEventListener('click', function() {
        window.location.href = '/static/verification.html?type=bank';
    });
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
function handleCashbookDateRangeChange(start, end) {
    if (popupDateRangePicker) popupDateRangePicker.setRange(start, end);
}
cashbookDateRangePicker = mountCashbookDateRangePicker();
if (popupCancelBtn) {
    popupCancelBtn.addEventListener('click', function() {
        popupOverlay.style.display = 'none';
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
// SUPPORTING DOCUMENTS TABBED MODAL
// Voucher Supporting-Document Linking, Phase 3 of 6 (shell): one tab per document belonging to
// the voucher line's Category, generated dynamically from that Category's documents[] list (the
// same list already fetched for the Guide popover/Supporting cell via fetchAuditGuidanceData()).
// Phase 4 of 6 (this phase): wires each tab to real search (Phase 2's extended
// GET /api/data/preview), a paginated browse fallback (#procedureBrowseModal, below), and a
// selected-lines table sourced from Phase 1's assign_document/voucher_line_documents endpoints.
// Procedure ticks/observations remain Phases 5-6.
// ==============================================

// Wide fixed search/browse window (confirmed with the user): evidence documents (e.g. a Purchase
// Order) can predate the voucher by weeks or months, so search/browse deliberately spans this
// fixed range rather than the voucher's own date window — GET /api/data/preview requires
// date_start/date_end, so this is the widest practical bound, not a real filter.
const PROCEDURE_SEARCH_DATE_START = '2000-01-01';
function getProcedureSearchDateEnd() {
    return new Date().toISOString().slice(0, 10);
}

// Shared fetch used by both the search dropdown and the browse modal — GET /api/data/preview
// scoped to one document's own template, over the wide fixed date window above. search='' (browse
// mode) returns every row for that template, paginated; a non-empty search narrows it.
async function fetchProcedureRows(templateId, search, page, limit) {
    const clientId = getClientId();
    if (!clientId) throw new Error('No client selected.');
    const params = new URLSearchParams({
        client_id: clientId,
        template_id: templateId,
        date_start: PROCEDURE_SEARCH_DATE_START,
        date_end: getProcedureSearchDateEnd(),
        page: page,
        limit: limit
    });
    if (search) params.set('search', search);
    const res = await fetch(`${API_BASE}/api/data/preview?${params}`);
    if (!res.ok) throw new Error('Failed to fetch Data Bank rows.');
    return res.json();
}

// Voucher Supporting-Document Linking follow-up, Phase 2 of 4: formats an assigned row's voucher
// label as "{voucherId} [{lineSrno}]" so the specific line an assignment belongs to is visibly
// verifiable, not just the voucher as a whole. Falls back to the bare voucherId when lineSrno is
// missing (mirrors the backend's own None-on-lookup-failure fallback in _resolve_line_srno(),
// routers/verification.py) — a rare defensive case, not the expected common path. Shared by both
// buildProcedureResultRowHtml() and buildProcedureBrowseTableHtml() so the two surfaces can never
// disagree on this label's format.
function formatAssignedVoucherLabel(voucherId, lineSrno) {
    const label = voucherId || '';
    return (lineSrno !== null && lineSrno !== undefined) ? `${label} [${lineSrno}]` : label;
}

// Bug fix (Voucher Supporting-Document Linking follow-up, Phase 3 of 4): determines whether an
// assigned Data Bank row belongs to the SAME underlying voucher line currently open in the
// modal — regardless of which verification session that assignment was originally made under.
// Every new verification session recreates verification_details rows from scratch for every
// voucher (no session-reuse exists), so the session-specific uid/row.assigned_to_voucher_line
// can no longer be compared directly (a row assigned under an OLDER session's copy of this same
// line would wrongly show as "assigned to a different voucher" even though it's the exact same
// line). Compares by the stable client_data_id of the voucher's own line instead — looked up for
// "uid" via currentVoucherLines (already loaded for the current voucher), against the new
// assigned_to_line_client_data_id field routers/data.py's attach_assignment_flags() now returns.
function isSameUnderlyingLine(uid, row) {
    if (!row.is_assigned) return false;
    const currentLine = currentVoucherLines.find(l => l.detail_id === uid);
    if (!currentLine || currentLine.client_data_id == null || row.assigned_to_line_client_data_id == null) return false;
    return String(currentLine.client_data_id) === String(row.assigned_to_line_client_data_id);
}

// Builds one selectable/greyed result row's HTML — shared by the search dropdown and the browse
// modal so both present identical lock-state treatment, driven by Phase 2's
// is_assigned/assigned_to_voucher_line/assigned_to_voucher_id/assigned_to_document_id/
// assigned_to_document_name/assigned_to_line_srno fields. docKeyColumn (the template's own
// document-key column name, from the same response) labels the row the same way Data Bank's own
// preview identifies a document — falls back to a bare row id if unknown.
//
// Bug fix (Phase 2 of 4): "Already linked here" previously only checked assigned_to_voucher_line
// === uid, which is true even when the row is linked to THIS voucher line under a DIFFERENT
// Document tab (e.g. two Documents sharing the same template_id) — that row would never appear
// in the current tab's own Linked Evidence table (which additionally filters by document_id), so
// Browse and Linked Evidence disagreed about the same row for the same line. Now requires
// document_id to match the tab actually open (activeDocId) before calling it a true self-match.
//
// Bug fix (Phase 3 of 4): "same line" itself is now determined by isSameUnderlyingLine() (stable
// client_data_id comparison) instead of raw assigned_to_voucher_line === uid — see that
// function's own comment for why a session-specific id comparison silently missed assignments
// made in an earlier verification session for the exact same voucher line.
function buildProcedureResultRowHtml(uid, row, docKeyColumn, activeDocId) {
    const label = (docKeyColumn && row.row_data && row.row_data[docKeyColumn] != null && row.row_data[docKeyColumn] !== '')
        ? row.row_data[docKeyColumn]
        : `Row #${row.id}`;
    const date = row.extract_date || '—';
    const amount = row.extract_amount != null ? row.extract_amount : '—';
    const isSameLine = isSameUnderlyingLine(uid, row);
    const isSelf = isSameLine && activeDocId != null && String(row.assigned_to_document_id) === String(activeDocId);
    const isSameLineOtherDoc = isSameLine && !isSelf;
    const isLocked = !!row.is_assigned;
    let lockNote = '';
    if (isSelf) {
        lockNote = '<span class="procedure-result-locked">Already linked here</span>';
    } else if (isSameLineOtherDoc) {
        lockNote = `<span class="procedure-result-locked">Linked to this voucher under "${escapeHtml(row.assigned_to_document_name || 'another document')}"</span>`;
    } else if (isLocked) {
        lockNote = `<span class="procedure-result-locked">Assigned to voucher ${escapeHtml(formatAssignedVoucherLabel(row.assigned_to_voucher_id, row.assigned_to_line_srno))}</span>`;
    }
    return `
        <div class="procedure-result-item${isLocked ? ' locked' : ''}" data-id="${row.id}"${isLocked ? '' : ' role="button" tabindex="0"'}>
            <div class="procedure-result-main">
                <span class="procedure-result-label">${escapeHtml(String(label))}</span>
                <span class="procedure-result-meta">${escapeHtml(date)} · ${escapeHtml(String(amount))}</span>
            </div>
            ${lockNote}
        </div>
    `;
}

// Returns (creating if needed) one tab's own state object, so every reader/writer shares the
// same shape: { loaded, searchTerm, searchResults, docKeyColumn, selections, observation,
// suggestionApplied, checklist }. selections/searchTerm/searchResults persist across tab
// switches within one modal-open session (not reset on close, only on voucher navigation in
// seedVoucherLineState()) — the "switching tabs doesn't lose unsaved work" requirement.
// observation (Phase 6) holds the tab's current observation textarea value, seeded from
// GET /api/verification/line_observations/{uid} in openProcedureModal() when a saved entry
// exists. observationDirty (Phase 3 of the modal-redesign follow-up) tracks whether observation
// has been typed since the last successful save — flushProcedureObservations() uses this to only
// POST tabs that actually changed, on the Prev/Next/Complete/Exit chokepoint and on modal close.
// suggestionApplied (Phase 6, client-only — never persisted) tracks whether the
// auto-suggestion has already been offered for this tab, so it fills an empty box at most
// once and never overwrites text the auditor typed or deliberately cleared. checklist
// (Phase 5) is a map keyed by role_name -> {is_checked, checked_by, checked_at}, seeded from
// GET /api/verification/procedure_checks/{uid} in openProcedureModal() — a role with no entry
// is unchecked.
function getProcedureTabState(uid, docId) {
    if (!procedureModalState[uid]) procedureModalState[uid] = { activeDocId: null, documents: [], assignedDocIds: new Set(), voucherLineAmount: 0, crossCheckResults: [], tabs: {} };
    if (!procedureModalState[uid].tabs[docId]) {
        procedureModalState[uid].tabs[docId] = { loaded: false, searchTerm: '', searchResults: [], docKeyColumn: null, selections: [], observation: '', observationDirty: false, suggestionApplied: false, lastSyncedText: null, checklist: {}, physicalBill: null, physicalBillSaved: false, physicalBillErrors: {}, physicalBillSaving: false, physicalBillSourceDocumentRowId: null, physicalBillSourceTemplate: null };
    }
    return procedureModalState[uid].tabs[docId];
}

// Opens the modal for one voucher line, showing a tab per document in that line's currently
// assigned Category. initialDocId (optional) pre-selects that document's tab; otherwise the
// first document's tab opens. No-ops if the line has no Category assigned yet (nothing to build
// tabs from) or its Category has no documents.
async function openProcedureModal(uid, initialDocId = null) {
    if (!procedureModal) return;
    const lineState = voucherLineState[uid];
    const type = lineState && lineState.transactionType;
    if (!type) return;

    // forceRefresh=true — bypasses auditGuidanceCache so this Category's Document list and each
    // Document's auth_roles always reflect Audit Rules' live state on open, same "re-fetched
    // fresh on every open" convention as assignedRecords/procedureChecks just below.
    let guidance = null;
    try {
        guidance = await fetchAuditGuidanceData(type, true);
    } catch (e) {
        console.error('Failed to load Supporting Documents modal data:', e);
        return;
    }
    const guidanceDocuments = (guidance && guidance.documents) || [];
    if (!guidanceDocuments.length) return;

    // Validation Rules Phase 5: prepend a synthetic, non-document "Voucher Line" tab holding the
    // Prepared/Checked/Approved By fields — always present as the first rail entry, distinct from
    // the Category's real Documents. __voucherTab marks it so renderProcedureModalTabs()/
    // isProcedureDocumentComplete()/renderProcedureModalPanel() can special-case it; every other
    // place that iterates `documents` (e.g. the template-warming filter just below) naturally
    // skips it since it has no template_id. Built as a NEW array (not documents.unshift(), which
    // would mutate guidance.documents in place — that's the same object fetchAuditGuidanceData()
    // just wrote into auditGuidanceCache, so mutating it would permanently pollute the shared
    // cache other callers (Supporting column, Guide popover) read from).
    const documents = [{ id: VOUCHER_LINE_TAB_ID, name: 'Voucher Line', __voucherTab: true }, ...guidanceDocuments];

    // Confirmed fix: warm templateColumnsCache for every template_id among this Category's
    // Documents before the tabs render, so buildSelectedLinesTable() can look up each template's
    // real column list synchronously — mirrors the assignedRecords/procedureChecks/
    // observationEntries pattern below (all awaited before rendering).
    const distinctTemplateIds = [...new Set(documents.filter(d => d.template_id).map(d => d.template_id))];
    await Promise.all(distinctTemplateIds.map(fetchTemplateColumns));

    // Full assignment records (not just IDs) for this line — Phase 1's endpoint. Drives the tab
    // completion dots AND seeds each tab's selected-lines table, grouped by document_id. Re-
    // fetched fresh on every open so both can never drift from true server state.
    let assignedRecords = [];
    try {
        const res = await fetch(`${API_BASE}/api/verification/voucher_line_documents/${uid}`);
        if (res.ok) {
            const assignData = await res.json();
            assignedRecords = assignData.documents || [];
        }
    } catch (e) {
        console.error('Failed to load Supporting Document assignment status:', e);
    }
    const assignedDocIds = new Set(assignedRecords.map(d => d.document_id));

    // Phase 5: every ticked/unticked audit-procedure role for this line, across all documents —
    // fail-soft to an empty list (same convention as assignedRecords above) so a fetch hiccup
    // never blocks the modal opening; re-fetched fresh on every open for the same reason.
    let procedureChecks = [];
    try {
        const checksRes = await fetch(`${API_BASE}/api/verification/procedure_checks/${uid}`);
        if (checksRes.ok) {
            const checksData = await checksRes.json();
            procedureChecks = checksData.checks || [];
        }
    } catch (e) {
        console.error('Failed to load audit procedure checks:', e);
    }

    // Phase 6: every saved observation entry for this line, across all documents — same
    // fail-soft-to-empty convention as assignedRecords/procedureChecks above, re-fetched fresh
    // on every open so a saved note is never silently stale.
    let observationEntries = [];
    try {
        const obsRes = await fetch(`${API_BASE}/api/verification/line_observations/${uid}`);
        if (obsRes.ok) {
            const obsData = await obsRes.json();
            observationEntries = obsData.entries || [];
        }
    } catch (e) {
        console.error('Failed to load voucher line observations:', e);
    }

    // Confirmed fix: tabs for Documents not applicable to this line's Category are fully hidden,
    // not rendered greyed-out — `documents` (built from guidanceDocuments above) already IS that
    // per-Category applicable list (audit_rule_category_documents, is_active=1-filtered server-
    // side), so no further filtering is needed here. This supersedes the earlier "synthesize a
    // locked tab for a Document deactivated after evidence was already recorded against it"
    // design — that tab is now simply omitted; the underlying voucher_line_documents/
    // voucher_line_procedure_checks/voucher_line_observations rows are left untouched (data is
    // not deleted, just no longer reachable from this rail).

    // Physical Bill evidence type, Phase 7 of 8: every saved Physical Bill entry for this line,
    // across all documents - same fail-soft-to-empty, re-fetched-fresh-on-every-open convention
    // as assignedRecords/procedureChecks/observationEntries above. Shared helper (see
    // fetchPhysicalBillsForLine() above) since updateProceduresCellForLine() also needs this now.
    const physicalBillEntries = await fetchPhysicalBillsForLine(uid);

    // Automated cross-document validation engine, Phase 9 of 10: this line's Cross-check
    // evaluation results (Phase 6/7's already-computed structure - zero new evaluation logic,
    // this just fetches it), for the Data Table segment's collapsible pass/fail rollup. Same
    // fail-soft-to-empty, re-fetched-fresh-on-every-open convention as assignedRecords/
    // procedureChecks/observationEntries above.
    let crossCheckResults = [];
    try {
        const ccRes = await fetch(`${API_BASE}/api/verification/cross_check_results/${uid}`);
        if (ccRes.ok) {
            const ccData = await ccRes.json();
            crossCheckResults = ccData.results || [];
        }
    } catch (e) {
        console.error('Failed to load cross-check results:', e);
    }

    // Account Head Recommendation follow-up: re-seed the no-match note from server truth on every
    // open (the ACCOUNT_HEAD_OBSERVATION_DOC_ID sentinel entry, if any) — a stale local edit from a
    // previous session must never silently persist across reload, same convention every other
    // tab's state already follows. The match-mode selection (selectedAccountId/Name) is
    // deliberately NOT re-seeded here — it's confirmed scratch-only, with no server-side source.
    const ahState = getAccountHeadState(uid);
    const savedNoMatchEntry = observationEntries.find(e => e.document_id === ACCOUNT_HEAD_OBSERVATION_DOC_ID);
    ahState.noMatchText = savedNoMatchEntry ? (savedNoMatchEntry.observation_text || '') : '';
    ahState.noMatchDirty = false;
    ahState.mode = ahState.noMatchText ? 'nomatch' : 'match';

    // Warm the Trial Balance leaf-account list for the Account Head search-select — fetched fresh
    // on every open, same fail-soft-to-empty convention as assignedRecords/procedureChecks above.
    await fetchLatestTbAccounts(getClientId());

    // Generic Voucher-Level Checks cutover: the persistent voucher-level checks bar's data — read
    // once per voucher (client_id+transaction_ref), reused across every line of that same voucher
    // rather than re-fetched per line, matching the backend's own "applied to every line of that
    // voucher" design (Phase 4). currentVoucherId is this open voucher's transaction_ref, kept in
    // sync by every navigation function in this module.
    await ensureVoucherChecksLoaded(getClientId(), currentVoucherId);

    // The voucher line's own amount (for the mismatch badge) — looked up from the already-loaded
    // voucher, no extra fetch needed. String()-normalized: uid arrives as a string whenever this
    // is called from a table click (dataset.uid is always a string), but currentVoucherLines[].
    // detail_id is a number straight from the JSON response — a bare === here always missed on
    // that path (confirmed bug: caused the header below to show "Line 0 of 6 — —" and this amount
    // to silently default to 0). Object-key lookups elsewhere (voucherLineState[uid]) don't have
    // this problem since plain-object keys auto-coerce to strings; only Array.find/findIndex do not.
    const lineIdx = currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    const voucherLine = lineIdx !== -1 ? currentVoucherLines[lineIdx] : null;
    const voucherLineAmount = voucherLine ? parseVoucherAmount(getVoucherLineAmount(voucherLine.row_data || {})) : 0;

    if (!procedureModalState[uid]) procedureModalState[uid] = { activeDocId: null, documents: [], assignedDocIds: new Set(), voucherLineAmount: 0, crossCheckResults: [], tabs: {} };
    procedureModalState[uid].documents = documents;
    procedureModalState[uid].assignedDocIds = assignedDocIds;
    procedureModalState[uid].voucherLineAmount = voucherLineAmount;
    procedureModalState[uid].crossCheckResults = crossCheckResults;

    // Re-sync each tab's selections and checklist from the fresh server data (never just seeded
    // once) so reopening the modal always reflects reality, even if a change happened elsewhere.
    // Must run before the default-tab selection below — a no-template document's completeness
    // depends on tabState.checklist, populated here.
    documents.forEach(doc => {
        const tabState = getProcedureTabState(uid, doc.id);
        tabState.selections = assignedRecords.filter(r => r.document_id === doc.id);
        const checklist = {};
        procedureChecks.filter(c => c.document_id === doc.id).forEach(c => {
            checklist[c.role_name] = { is_checked: c.is_checked, checked_by: c.checked_by, checked_at: c.checked_at };
        });
        tabState.checklist = checklist;

        // Phase 6: a saved entry always wins over a fresh auto-suggestion — mark
        // suggestionApplied so buildObservationSuggestion() never overwrites it.
        const savedEntry = observationEntries.find(e => e.document_id === doc.id);
        if (savedEntry) {
            tabState.observation = savedEntry.observation_text;
            tabState.suggestionApplied = true;
            tabState.lastSyncedText = savedEntry.last_synced_text || null;
        }

        // Physical Bill evidence type, Phase 7 of 8: server truth always wins on reopen, same
        // convention as observation/lastSyncedText above — never trust a stale local draft.
        if (doc.document_type === 'physical_bill') {
            const savedBill = physicalBillEntries.find(b => b.document_id === doc.id);
            tabState.physicalBill = savedBill
                ? {
                    bill_no: savedBill.bill_no || '',
                    bill_date: savedBill.bill_date || '',
                    amount: savedBill.amount != null ? String(savedBill.amount) : '',
                    vendor_name: savedBill.vendor_name || '',
                    quantity: savedBill.quantity != null ? String(savedBill.quantity) : '',
                    rate: savedBill.rate != null ? String(savedBill.rate) : ''
                }
                : { bill_no: '', bill_date: '', amount: '', vendor_name: '', quantity: '', rate: '' };
            tabState.physicalBillSaved = !!savedBill;
            tabState.physicalBillErrors = {};
            // Populate Physical Bill from GRN/ERP Vendor Bill (Phase 3 of 4): re-seed the source
            // pointer too, so reopening the modal on an already-populated-from-source bill shows
            // the same readonly/computed Amount state, not just the four original fields.
            tabState.physicalBillSourceDocumentRowId = savedBill ? (savedBill.source_document_row_id || null) : null;
            tabState.physicalBillSourceTemplate = savedBill ? (savedBill.source_template || null) : null;
        }
    });

    // Rail default (Phase 1): an explicit target (a specific document link clicked in the
    // Supporting column) still wins; otherwise open the first document that isn't yet complete,
    // falling back to the first document if everything is already done.
    let activeDocId;
    if (initialDocId && documents.some(d => d.id === initialDocId)) {
        activeDocId = initialDocId;
    } else {
        const firstIncomplete = documents.find(doc => !isProcedureDocumentComplete(uid, doc));
        activeDocId = firstIncomplete ? firstIncomplete.id : documents[0].id;
    }
    procedureModalState[uid].activeDocId = activeDocId;

    currentProcedureModalUid = uid;

    // "Why is this line Cleared/Query?" panel always resets to collapsed on open (new line or
    // reopen) — stale gate data for a different line must never carry over.
    hideValidatePanel();

    // Header: line position/account name, type, amount, risk (Supporting Documents modal
    // redesign follow-up) — srNo/totalLines mirror the same order renderVoucherLines() numbers
    // the table with; risk reuses computeLineRiskData() against the guidance already fetched
    // above, no extra request. lineIdx/voucherLine were already resolved above (String()-normalized).
    const srNo = lineIdx + 1;
    const totalLines = currentVoucherLines.length;
    const accountName = voucherLine ? getVoucherLineAlias(voucherLine.row_data || {}, VOUCHER_LINE_ACCOUNT_NAME_ALIASES) : '—';
    const amountDisplay = voucherLine ? getVoucherLineAmount(voucherLine.row_data || {}) : '';
    const riskData = computeLineRiskData(guidance);
    if (procedureModalTitle) procedureModalTitle.textContent = `Line ${srNo} of ${totalLines} — ${accountName}`;
    const metaEl = document.getElementById('procedureModalMeta');
    if (metaEl) {
        let meta = `${type} · ${amountDisplay}`;
        if (riskData.risk) meta += ` · Risk: ${riskData.risk}`;
        metaEl.textContent = meta;
    }

    // Footer nav: Back disabled on the voucher's first line (unchanged). The old per-line "Next"
    // button is gone — its footer slot is now the voucher-level Verify & Next/Close button, whose
    // label/disabled state is computed in updateVerifyFooterButton() below (voucher position for
    // the label, voucher-checks completeness for disabled).
    const backBtn = document.getElementById('procedureModalBackBtn');
    if (backBtn) backBtn.disabled = lineIdx <= 0;

    procedureModal.style.display = 'flex';

    // Generic Voucher-Level Checks cutover: the persistent bar is shown once per voucher, above
    // the rail — rendered here (not per-tab, unlike renderProcedureModalTabs()/activateProcedureTab()
    // below) since it doesn't belong to any one document's panel.
    renderVoucherChecksBar(uid);
    renderProcedureModalTabs(uid);
    activateProcedureTab(uid, activeDocId);
    updateProcedureModalSavedNote(uid);
}

// Reflects whether any tab for this line has an unsaved (typed-but-not-yet-flushed) observation —
// checklist ticks, the signature-exception tick, and (as of Voucher-level Checks simplified to
// name-only) the voucher-checks tick/cross pills all save immediately now, so only observation
// text can ever be "unsaved" here. Called on modal open, on every keystroke in the observation
// textarea (wireProcedureChecklistAndObservation()), and after flushProcedureObservations()
// resolves.
function updateProcedureModalSavedNote(uid) {
    const noteEl = document.getElementById('procedureModalSavedNote');
    if (!noteEl) return;
    const lineState = procedureModalState[uid];
    const tabs = (lineState && lineState.tabs) || {};
    const dirty = Object.values(tabs).some(t => t.observationDirty);
    noteEl.textContent = dirty ? 'Unsaved changes' : 'All changes saved';
    noteEl.classList.toggle('dirty', dirty);
}

// Moves the open modal to the adjacent voucher line without closing it — distinct from
// activateProcedureTab(), which switches between one line's own Documents. Only ever called with
// delta = -1 now (the footer's "Next" slot is the voucher-level Verify & Next/Close button
// instead — see verifyAndAdvanceVoucher()); the delta parameter is kept generic rather than
// hardcoding -1 internally, since the underlying step logic is direction-agnostic. Flushes any
// unsaved observation text on the line being left, same as closeProcedureModal() does, then
// re-runs openProcedureModal()'s full fetch/render pipeline for the target line so its own tab
// state is always freshly synced from the server. A target line with no Category assigned has
// nothing to show (openProcedureModal() no-ops on a falsy type, per this module's existing
// documented rule) — caught here first so the user gets a clear message instead of the modal
// silently freezing on the old line's content.
async function navigateProcedureModalLine(delta) {
    const uid = currentProcedureModalUid;
    if (!uid) return;
    // String()-normalized — see the matching comment in openProcedureModal() for why a bare ===
    // between currentProcedureModalUid (may be a string) and detail_id (a number) can miss.
    const idx = currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    if (idx === -1) return;
    const newIdx = idx + delta;
    if (newIdx < 0 || newIdx >= currentVoucherLines.length) return;
    const newUid = currentVoucherLines[newIdx].detail_id;
    const newLineState = voucherLineState[newUid];
    if (!newLineState || !newLineState.transactionType) {
        showStatus('This line has no assigned category — supporting documents unavailable.', 'error');
        return;
    }
    // Validation Rules Phase 5: saveLineItems() (not just flushProcedureObservations()) so
    // cleared_status recomputes and the badge refreshes every time the modal moves between
    // lines — the badge lives in the main table, hidden behind the modal while it's open, so
    // this is the natural point for it to catch up.
    await saveLineItems(uid);
    await openProcedureModal(newUid);
}

// Finds the next *unverified* line of the currently loaded voucher, searching forward only from
// fromIdx + 1. "Unverified" is cleared_status !== 'Cleared' — the Validation Rules verdict already
// rendered as the Cleared/Query badge on each table row, recomputed server-side on every
// saveLineItems() call and mirrored straight back into currentVoucherLines, so the value tested
// here is always fresh by the time Verify & Next consults it.
//
// Forward-only (never wraps back to an earlier line) by deliberate design: a line can legitimately
// never reach Cleared — a required document that genuinely doesn't exist, for instance — and a
// wrapping search would trap the auditor inside that voucher with no way out. Falling through to
// the next voucher instead always makes progress.
//
// Lines with no assigned Category are skipped: openProcedureModal() already no-ops on a falsy type
// (this module's existing documented rule), so targeting one would silently freeze the modal on the
// previous line's content. Returns -1 when no qualifying line remains.
function findNextUnverifiedLineIndex(fromIdx) {
    for (let i = fromIdx + 1; i < currentVoucherLines.length; i++) {
        const line = currentVoucherLines[i];
        if (line.cleared_status === 'Cleared') continue;
        const lineState = voucherLineState[line.detail_id];
        if (!lineState || !lineState.transactionType) continue;
        return i;
    }
    return -1;
}

// Pure teardown — hides the modal(s) and clears the "currently open" marker, with no save of any
// kind. Factored out of closeProcedureModal() so the Verify & Next/Close button (which already
// explicitly saves via saveLineItems() before calling verify_voucher_checks) can tear down the
// modal without triggering a redundant second save. procedureModalState[uid] is deliberately left
// in place (not cleared here) — reopening the same line's modal within the same voucher should
// show tab state exactly as it was left; it's only reset on voucher navigation, in
// seedVoucherLineState().
function hideProcedureModalOnly() {
    if (procedureModal) procedureModal.style.display = 'none';
    // Defensive — the browse modal is nested on top of this one, so it should never remain open
    // once the parent closes.
    closeProcedureBrowseModal();
    currentProcedureModalUid = null;
}

async function closeProcedureModal() {
    // Flush any unsaved observation text/voucher-approval edits before hiding — closing via ✕ or
    // outside-click persists the same way Prev/Next/Complete/Exit does, per the confirmed
    // requirement (there is no longer a Save button to rely on instead). saveLineItems() also
    // recomputes cleared_status (Validation Rules Phase 5) so the main table's badge is caught up
    // by the time the modal is gone.
    if (currentProcedureModalUid) await saveLineItems(currentProcedureModalUid);
    hideProcedureModalOnly();
}

// Sets the footer's "Verify & Next/Close" button label and disabled state. The label has three
// states so it always says what the button will actually do: if an unverified line remains below
// this one in the same voucher, pressing it steps there ("Verify & Next Line"); otherwise it leaves
// the voucher, and the wording then comes from voucher position — the same pos.index/pos.total
// check renderVoucherHeader() already uses to disable the main page's own Next-voucher arrow — so
// it always agrees with whether there really is a next voucher to advance to. Disabled is driven
// solely by isVoucherChecksAllTouched() (every active voucher-level check marked done or not_done);
// it is intentionally independent of document/procedure completeness elsewhere in the modal. Called
// on modal open and after every voucher-check toggle (renderVoucherChecksBar()).
function updateVerifyFooterButton(uid) {
    const btn = document.getElementById('procedureModalVerifyBtn');
    const verifyOnlyBtn = document.getElementById('procedureModalVerifyOnlyBtn');
    if (!btn && !verifyOnlyBtn) return;
    const lineIdx = currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    const hasNextLine = lineIdx !== -1 && findNextUnverifiedLineIndex(lineIdx) !== -1;
    const pos = getVoucherPosition();
    const isLastVoucher = !(pos.total > 0 && pos.index < pos.total);
    if (btn) {
        if (hasNextLine) {
            btn.textContent = '✅ Verify & Next Line';
        } else {
            btn.textContent = isLastVoucher ? '✅ Verify & Close' : '✅ Verify & Next';
        }
    }
    // Both buttons hit the same backend precondition (verify_voucher_checks 400s if any active
    // check is untouched), so they share this one gate — the plain "Verify" button's label never
    // changes, unlike Verify & Next's three-state wording, since it never describes "what's next".
    const checks = voucherChecksCache[currentVoucherId] || [];
    const allTouched = isVoucherChecksAllTouched(checks);
    const title = allTouched ? '' : 'Mark every check before verifying';
    if (btn) {
        btn.disabled = !allTouched;
        btn.title = title;
    }
    if (verifyOnlyBtn) {
        verifyOnlyBtn.disabled = !allTouched;
        verifyOnlyBtn.title = title;
    }
}

// Shared save+verify call used by both footer verify buttons (Verify & Next/Close and the plain
// Verify). Saves the current line (existing chokepoint — flushes observations/account-head note)
// then POSTs verify_voucher_checks, which is voucher-scoped (client_id + transaction_ref) and
// idempotent — it recomputes flagged_in_observations fresh each call rather than appending, so
// re-calling it keeps the voucher's Observations in sync with the live check state. Returns the
// parsed response body on success, or null on failure (after showing the error status itself) —
// callers that don't need the next-voucher payload (e.g. the plain Verify button) simply leave it
// unread. Disables/re-enables BOTH verify-type buttons around the request, not just the one
// clicked, so a click on the other button can't double-submit while this one is in flight.
async function performVerifyVoucherChecks(uid) {
    const btn = document.getElementById('procedureModalVerifyBtn');
    const verifyOnlyBtn = document.getElementById('procedureModalVerifyOnlyBtn');
    if (btn) btn.disabled = true;
    if (verifyOnlyBtn) verifyOnlyBtn.disabled = true;
    try {
        await saveLineItems(uid);
        const res = await fetch(`${API_BASE}/api/verification/verify_voucher_checks`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: currentSessionId, current_detail_id: uid })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showStatus(err.detail || 'Could not verify voucher-level checks — please try again.', 'error');
            return null;
        }
        return await res.json();
    } catch (e) {
        console.error('Error verifying voucher checks:', e);
        showStatus('Could not verify voucher-level checks — please try again.', 'error');
        return null;
    } finally {
        // Restores each button to its real gate-driven state (isVoucherChecksAllTouched()) rather
        // than blindly re-enabling — updateVerifyFooterButton() is the single source of truth for
        // both buttons' disabled state, called by every caller of this helper right after.
        updateVerifyFooterButton(uid);
    }
}

// Click handler for the footer's "Verify & Next/Close" button. Verifies this voucher's checks via
// the shared helper above, then steps line-first: if an unverified line remains below the current
// one in the same voucher, the modal stays open and moves there; only once none remains does it
// leave for the next voucher (or close, on the last one). The confirmed fix supersedes the
// button's original voucher-only granularity, which forced the auditor to close the modal and
// re-enter from the table for every line.
async function verifyAndAdvanceVoucher() {
    const uid = currentProcedureModalUid;
    if (!uid) return;
    const btn = document.getElementById('procedureModalVerifyBtn');
    if (btn && btn.disabled) return;
    const data = await performVerifyVoucherChecks(uid);
    if (!data) return;

    // Line-first stepping: saveLineItems() inside the helper above already refreshed this line's
    // cleared_status in currentVoucherLines, so the search below sees this press's own result, not
    // a stale one. String()-normalized lookup for the same reason openProcedureModal() does it —
    // uid can be a string from a table click while detail_id is a number from JSON.
    const lineIdx = currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    const nextLineIdx = lineIdx === -1 ? -1 : findNextUnverifiedLineIndex(lineIdx);
    if (nextLineIdx !== -1) {
        // Stay in the voucher: keep the modal open and re-run the full fetch/render pipeline
        // for the target line, same as navigateProcedureModalLine() does (which re-saves; we
        // already saved above, so openProcedureModal() is called directly instead).
        await openProcedureModal(currentVoucherLines[nextLineIdx].detail_id);
        await updateProgressDisplay();
        return;
    }

    // No unverified line left below this one — leave the voucher, unchanged behaviour.
    // Already saved above — hide only, don't re-save via closeProcedureModal().
    hideProcedureModalOnly();
    if (data.boundary) {
        showStatus('Voucher verified. No more vouchers in this session.', 'success');
    } else {
        applyVoucherNavRenderData(data);
    }
    await updateProgressDisplay();
}

// Click handler for the plain footer "Verify" button (confirmed requirement): performs the exact
// same save+verify action as Verify & Next above, via the same shared helper, but never advances —
// it always just closes the modal back onto the grid at the current voucher, regardless of how
// many lines the voucher has or whether more remain unverified. The current line's Cleared/Query
// badge is already live in the grid by the time this returns (saveLineItems(), inside the helper,
// already refreshes it), so there is nothing further to re-render here.
async function verifyAndStay() {
    const uid = currentProcedureModalUid;
    if (!uid) return;
    const verifyOnlyBtn = document.getElementById('procedureModalVerifyOnlyBtn');
    if (verifyOnlyBtn && verifyOnlyBtn.disabled) return;
    const data = await performVerifyVoucherChecks(uid);
    if (!data) return;
    // Already saved above — hide only, don't re-save via closeProcedureModal().
    hideProcedureModalOnly();
    showStatus('Voucher checks verified.', 'success');
    await updateProgressDisplay();
}

// Shared "are all currently-active voucher-level checks marked done" check — used by any real
// Document flagged transaction_level_approval, whose tab additionally requires this. Mirrors
// compute_and_persist_cleared_status()'s own voucher-checks gate (routers/verification.py),
// which requires state='done' for every active check — a 'not_done' check does NOT satisfy this,
// even though it counts as "touched" for the Verify button's own, separate gate below.
function isVoucherChecksComplete(checks) {
    return Array.isArray(checks) && checks.length > 0 && checks.every(c => c.state === 'done');
}

// Separate, looser gate: "has every active check been marked one way or the other" (done OR
// not_done, just not left untouched) — the sole condition for enabling the footer's Verify &
// Next/Close button. A voucher with zero configured checks is vacuously touched (same
// "empty required set" convention used elsewhere in this codebase).
function isVoucherChecksAllTouched(checks) {
    if (!Array.isArray(checks) || !checks.length) return true;
    return checks.every(c => c.state !== 'untouched');
}

// Fetches and caches one voucher's checks list (client_id+transaction_ref scoped, shared across
// every line of that voucher) — factored out of openProcedureModal() so
// updateProceduresCellForLine()'s standalone badge computation (below) can also warm the same
// cache for a transaction_level_approval-flagged Document, without duplicating this fetch.
async function ensureVoucherChecksLoaded(clientId, transactionRef) {
    if (!transactionRef) return null;
    if (voucherChecksCache[transactionRef]) {
        return voucherChecksCache[transactionRef];
    }
    // Confirmed fix (2026-07-30): this function is called from two independent sites for the
    // same voucher — openProcedureModal() and updateProceduresCellForLine() (the latter fires
    // automatically whenever a line's Category has a transaction_level_approval-flagged
    // Document, e.g. "Purchase Demand"). Without de-duplication, two near-simultaneous callers
    // could each fire their own GET; if a tick was saved (mutating the cache in place) while an
    // older, slower GET for the same voucher was still in flight, that older response would
    // resolve afterward and silently overwrite the cache with pre-tick data — and since the
    // cache is never re-validated once populated, every later reopen kept showing that stale,
    // reset-looking state even though the tick was correctly persisted server-side the whole
    // time. Sharing one in-flight promise per transaction_ref removes the race entirely.
    if (voucherChecksLoadingPromises[transactionRef]) {
        return voucherChecksLoadingPromises[transactionRef];
    }
    const loadPromise = (async () => {
        try {
            const params = new URLSearchParams({ client_id: clientId, transaction_ref: transactionRef });
            const res = await fetch(`${API_BASE}/api/verification/voucher_checks?${params}`);
            if (res.ok) {
                const data = await res.json();
                voucherChecksCache[transactionRef] = data.map(c => ({
                    id: c.id,
                    name: c.name,
                    sort_order: c.sort_order,
                    state: c.state || 'untouched'
                }));
            }
        } catch (e) {
            console.error('Failed to load voucher checks:', e);
        }
        return voucherChecksCache[transactionRef] || null;
    })();
    voucherChecksLoadingPromises[transactionRef] = loadPromise;
    try {
        return await loadPromise;
    } finally {
        delete voucherChecksLoadingPromises[transactionRef];
    }
}

// Tabler "checks" (double-check) outline icon — shared by both select-all/clear-all toggle
// buttons below (roles bar + Audit Procedures checklist header), matching this modal's existing
// inline-SVG-icon convention (e.g. the PB_ICON_* constants) rather than a Tabler icon font/CDN,
// which isn't loaded anywhere in this app.
const ICON_SELECT_ALL_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 12l5 5l10 -10"/><path d="M2 12l5 5m5 -5l5 -5"/></svg>';

// Renders the persistent voucher-level checks bar, shown once per voucher above the document rail
// (#procedureVoucherChecksBar, static/verification.html) — not duplicated per tab/line, since these
// checks apply to the whole voucher (client_id+transaction_ref), not one line. Voucher-level
// Checks simplified to name-only (Phase 3 of 4): each configured check (audit_rule_voucher_checks,
// ordered by sort_order) renders as a name + a tick/cross toggle-pill pair — no auth-roles hint,
// no performed-by name field (both retired alongside the old boolean model). Clicking a pill saves
// its new state immediately (optimistic, reverts on failure) — nothing here is local-first
// anymore, so there is no flush step at the Prev/Next/Complete/Exit chokepoint for this bar.
function renderVoucherChecksBar(uid) {
    const container = document.getElementById('procedureVoucherChecksBar');
    if (!container) return;
    const transactionRef = currentVoucherId;
    const checks = voucherChecksCache[transactionRef] || [];

    if (!checks.length) {
        container.innerHTML = '<p class="procedure-voucher-checks-empty">No voucher-level checks configured.</p>';
        updateVerifyFooterButton(uid);
        return;
    }

    const notDone = checks.filter(c => c.state === 'not_done');
    const warningHtml = notDone.length
        ? `<div class="procedure-voucher-checks-warning">⚠️ ${notDone.map(c => `<strong>${escapeHtml(c.name)}</strong>`).join(', ')} marked not done — observation will auto-note on close.</div>`
        : '';

    // Select-all/clear-all toggle: only ever acts on checks NOT currently marked ✗ (not_done) — a
    // deliberate ✗ never gets silently overwritten by a bulk action in either direction. "All
    // selected" (icon filled) means every such check is currently 'done'.
    const selectAllTargets = checks.filter(c => c.state !== 'not_done');
    const allChecksSelected = selectAllTargets.length > 0 && selectAllTargets.every(c => c.state === 'done');
    const selectAllBtnHtml = `
        <button type="button" id="procedureVoucherChecksSelectAllBtn"
            class="procedure-select-all-btn${allChecksSelected ? ' all-selected' : ''}"
            title="${allChecksSelected ? 'Clear all' : 'Mark all done'}">${ICON_SELECT_ALL_SVG}</button>
    `;

    container.innerHTML = selectAllBtnHtml + checks.map(check => `
        <div class="procedure-voucher-check-row" data-check-id="${check.id}">
            <span class="procedure-voucher-check-label">${escapeHtml(check.name)}</span>
            <div class="procedure-voucher-check-toggle">
                <button type="button" class="procedure-voucher-check-btn tick${check.state === 'done' ? ' selected' : ''}"
                    data-check-id="${check.id}" data-state="done" title="Mark done">✓</button>
                <button type="button" class="procedure-voucher-check-btn cross${check.state === 'not_done' ? ' selected' : ''}"
                    data-check-id="${check.id}" data-state="not_done" title="Mark not done">✗</button>
            </div>
        </div>
    `).join('') + warningHtml;

    const selectAllBtn = document.getElementById('procedureVoucherChecksSelectAllBtn');
    if (selectAllBtn) {
        selectAllBtn.addEventListener('click', function() {
            toggleAllVoucherChecks(uid);
        });
    }

    container.querySelectorAll('.procedure-voucher-check-btn').forEach(btn => {
        btn.addEventListener('click', async function() {
            const checkId = parseInt(this.dataset.checkId);
            const newState = this.dataset.state;
            const check = checks.find(c => c.id === checkId);
            if (!check || check.state === newState) return;
            const previousState = check.state;
            check.state = newState;
            try {
                const res = await fetch(`${API_BASE}/api/verification/save_voucher_check`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        client_id: getClientId(),
                        transaction_ref: transactionRef,
                        check_id: checkId,
                        state: newState
                    })
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail || 'Failed to save voucher check.');
                }
                // Completion-dot/gate logic depends on check state, so a successful toggle must
                // refresh the rail's dots, the main table's Procedures badges, and the footer's
                // Verify button — mirrors toggleProcedureCheck()'s own renderProcedureModalTabs()
                // refresh, extended to this bar's own dependents.
                renderVoucherChecksBar(uid);
                renderProcedureModalTabs(uid);
                await saveAllVoucherLines();
            } catch (e) {
                console.error('Failed to save voucher check:', e);
                check.state = previousState;
                showStatus('Failed to save voucher-level check — please try again.', 'error');
                renderVoucherChecksBar(uid);
            }
        });
    });

    updateVerifyFooterButton(uid);
}

// Bulk-toggles the roles bar's ✓ state — see renderVoucherChecksBar()'s selectAllTargets comment
// for the "never touch ✗" rule. Mirrors the per-check button's own optimistic-save/revert-on-
// failure shape, batched via Promise.all rather than one save per check.
async function toggleAllVoucherChecks(uid) {
    const transactionRef = currentVoucherId;
    const checks = voucherChecksCache[transactionRef] || [];
    const targets = checks.filter(c => c.state !== 'not_done');
    if (!targets.length) return;
    const allSelected = targets.every(c => c.state === 'done');
    const newState = allSelected ? 'untouched' : 'done';
    const toChange = targets.filter(c => c.state !== newState);
    if (!toChange.length) return;

    const previousStates = {};
    toChange.forEach(c => { previousStates[c.id] = c.state; c.state = newState; });
    renderVoucherChecksBar(uid);

    try {
        const responses = await Promise.all(toChange.map(c => fetch(`${API_BASE}/api/verification/save_voucher_check`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                client_id: getClientId(),
                transaction_ref: transactionRef,
                check_id: c.id,
                state: newState
            })
        })));
        for (const res of responses) {
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || 'Failed to save voucher check.');
            }
        }
        renderVoucherChecksBar(uid);
        renderProcedureModalTabs(uid);
        await saveAllVoucherLines();
    } catch (e) {
        console.error('Failed to save all voucher checks:', e);
        toChange.forEach(c => { c.state = previousStates[c.id]; });
        showStatus('Failed to update all checks — please try again.', 'error');
        renderVoucherChecksBar(uid);
    }
}

// Shared completeness check for one document within one voucher line's modal — used both by the
// rail's completion dot (below) and by openProcedureModal()'s default-tab selection. A no-template
// document has no evidence table to link, so linked-evidence presence (assignedDocIds) is
// meaningless for it — its completeness instead reflects whether every one of its Audit Procedures
// is ticked (voucher_line_procedure_checks, same source the checklist itself reads via
// getProcedureTabState()). Zero procedures configured is deliberately NOT complete (empty ≠ done),
// per the confirmed requirement. Transaction level approval (Phase 4 of 4, FINAL, re-pointed by the
// Generic Voucher-Level Checks cutover): a Document flagged transaction_level_approval requires the
// new voucher-level checks bar to ALSO be complete, on top of (not instead of) its own base
// completeness rule above.
function isProcedureDocumentComplete(uid, doc) {
    // Generic Voucher-Level Checks cutover: the synthetic Voucher Line tab now only hosts the
    // Account Head Recommendation card (an optional recommendation, not a gate) — the voucher-level
    // checks it used to hold live in the persistent bar above the rail instead, so this tab's dot
    // is always neutral/complete and never blocks the rail's default-tab selection.
    if (doc.__voucherTab) {
        return true;
    }
    let baseComplete;
    // Physical Bill evidence type, Phase 7 of 8: checked BEFORE doc.template_id (always null for
    // this doc type) - mirrors compute_and_persist_cleared_status()'s own document_type-first
    // branch order (Phase 6), so a physical_bill Document never silently falls into the
    // no-template role-tick branch below, the same trap Phase 6 closed server-side. Reflects
    // SAVED state only (not an unsaved draft edit) - same "dot reflects committed truth"
    // convention every other document type already follows. Also requires the saved bill's Amount
    // to match this line's own amount (procedureModalState[uid].voucherLineAmount, already
    // computed by openProcedureModal() before this function can be called), within the same
    // epsilon the backend gate uses (CROSS_CHECK_FLOAT_EPSILON = 0.005, routers/verification.py's
    // _physical_bill_satisfied()) — closes the gap the original Phase 7 comment deferred to
    // "Phase 8", which turned out not to touch this indicator.
    if (doc.document_type === 'physical_bill') {
        const tabState = getProcedureTabState(uid, doc.id);
        const b = tabState.physicalBill;
        const fieldsComplete = tabState.physicalBillSaved && !!(b && b.bill_no && b.bill_date && b.vendor_name
            && b.amount !== '' && b.amount != null && !isNaN(parseFloat(b.amount)));
        if (fieldsComplete) {
            const lineState = procedureModalState[uid];
            const voucherLineAmount = (lineState && lineState.voucherLineAmount) || 0;
            baseComplete = Math.abs(parseFloat(b.amount) - voucherLineAmount) <= 0.005;
        } else {
            baseComplete = false;
        }
    } else if (doc.template_id) {
        const lineState = procedureModalState[uid];
        baseComplete = !!(lineState && lineState.assignedDocIds && lineState.assignedDocIds.has(doc.id));
    } else {
        const tabState = getProcedureTabState(uid, doc.id);
        const roles = doc.auth_roles || [];
        baseComplete = roles.length > 0 && roles.every(role => tabState.checklist[role] && tabState.checklist[role].is_checked);
    }
    if (!baseComplete) return false;
    if (doc.transaction_level_approval) {
        return isVoucherChecksComplete(voucherChecksCache[currentVoucherId]);
    }
    return true;
}

// Renders the left-side document rail for one line's modal: one entry per document, name + a
// completion dot (see isProcedureDocumentComplete() above).
function renderProcedureModalTabs(uid) {
    const container = document.getElementById('procedureModalTabs');
    if (!container) return;
    const lineState = procedureModalState[uid];
    if (!lineState) { container.innerHTML = ''; return; }
    const documents = lineState.documents || [];

    container.innerHTML = documents.map(doc => {
        const isActive = doc.id === lineState.activeDocId;
        const isComplete = isProcedureDocumentComplete(uid, doc);

        // Generic Voucher-Level Checks cutover: the synthetic Voucher Line tab now only hosts the
        // (optional, non-gating) Account Head Recommendation card — no roles/mismatch concept, and
        // isProcedureDocumentComplete() always returns true for it, so its dot is a fixed neutral
        // "complete" marker rather than reflecting any real approval state.
        if (doc.__voucherTab) {
            return `
                <button type="button" class="procedure-tab-btn${isActive ? ' active' : ''}" data-uid="${uid}" data-doc-id="${doc.id}">
                    <span class="procedure-tab-dot complete" title="Account Head Recommendation"></span>
                    <span class="procedure-tab-text">
                        <span class="procedure-tab-label">${escapeHtml(doc.name)}</span>
                    </span>
                </button>
            `;
        }

        // Physical Bill evidence type, Phase 7 of 8: dot title/subtext for this doc type, checked
        // first since doc.template_id is always null for a physical_bill Document (same
        // document_type-first branch order as isProcedureDocumentComplete() above).
        const isPhysicalBill = doc.document_type === 'physical_bill';
        const dotTitle = isPhysicalBill
            ? (isComplete ? 'Bill entered' : 'Bill not yet entered')
            : doc.template_id
                ? (isComplete ? 'Evidence linked' : 'No evidence linked yet')
                : (isComplete ? 'All procedures confirmed' : 'Procedures pending');

        // "X/Y roles" subtext — same checklist state the completion dot itself reads.
        const tabState = getProcedureTabState(uid, doc.id);
        const roles = doc.auth_roles || [];
        const rolesDone = roles.filter(r => tabState.checklist[r] && tabState.checklist[r].is_checked).length;
        const subText = isPhysicalBill
            ? (tabState.physicalBillSaved ? 'Bill saved' : 'Bill pending')
            : `${rolesDone}/${roles.length} roles`;

        // Mismatch tag — has-template documents only, and only once evidence is actually linked
        // (same gating buildSelectedLinesTable()'s own summary line uses, so the rail never
        // disagrees with the panel about a document with nothing linked yet).
        let mismatchTagHtml = '';
        if (doc.template_id) {
            const { count, mismatch } = getSelectedLinesSummary(uid, doc.id);
            if (count > 0 && mismatch) mismatchTagHtml = '<span class="procedure-tab-mismatch">mismatch</span>';
        }

        return `
            <button type="button" class="procedure-tab-btn${isActive ? ' active' : ''}" data-uid="${uid}" data-doc-id="${doc.id}">
                <span class="procedure-tab-dot${isComplete ? ' complete' : ''}" title="${dotTitle}"></span>
                <span class="procedure-tab-text">
                    <span class="procedure-tab-label">${escapeHtml(doc.name)}</span>
                    <span class="procedure-tab-sub">${subText}</span>
                    ${mismatchTagHtml}
                </span>
            </button>
        `;
    }).join('');

    container.querySelectorAll('.procedure-tab-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            // Validation Rules Phase 5: the synthetic Voucher Line tab's id is a non-numeric
            // string (VOUCHER_LINE_TAB_ID) — parseInt would turn it into NaN, so only real
            // document ids get parsed.
            const rawDocId = this.dataset.docId;
            const docId = rawDocId === VOUCHER_LINE_TAB_ID ? rawDocId : parseInt(rawDocId);
            activateProcedureTab(this.dataset.uid, docId);
        });
    });
}

// Switches the active tab for one line's modal. Other tabs' own state objects are left
// untouched, so unsaved work in a tab (once Phases 4-6 add real content to it) survives
// switching away and back within the same modal session.
function activateProcedureTab(uid, docId) {
    const lineState = procedureModalState[uid];
    if (!lineState) return;
    lineState.activeDocId = docId;
    renderProcedureModalTabs(uid);

    const tabState = getProcedureTabState(uid, docId);
    if (!tabState.loaded) {
        // Lazy-load hook: Phase 4 fetches this document's real search/selection state here on
        // first activation. Nothing to fetch yet this phase — just mark the tab as seen.
        tabState.loaded = true;
    }

    renderProcedureModalPanel(uid, docId);
}

// Wires the Audit Procedures checklist + Observation section's listeners — shared by both the
// full (has-template) panel and the two-segment (no-template) panel below, since both render the
// exact same checklist/observation markup via buildProcedureChecklist()/buildObservationSection().
function wireProcedureChecklistAndObservation(container, uid, docId, tabState) {
    // Phase 5: one change listener per checklist checkbox — ticking/unticking a role posts the
    // new state immediately (see toggleProcedureCheck()), no separate Save action.
    container.querySelectorAll('.procedure-checklist-checkbox').forEach(cb => {
        cb.addEventListener('change', function() {
            toggleProcedureCheck(uid, docId, this.dataset.role, this.checked);
        });
    });

    const selectAllChecklistBtn = document.getElementById('procedureChecklistSelectAllBtn');
    if (selectAllChecklistBtn) {
        selectAllChecklistBtn.addEventListener('click', function() {
            toggleAllProcedureChecks(uid, docId);
        });
    }

    // The observation textarea is local-only on every keystroke (no network call) —
    // flushProcedureObservations() still persists any dirty tab automatically on
    // Prev/Next/Complete/Exit (via saveLineItems()) and on modal close (via closeProcedureModal());
    // the Copy button below is an additional, explicit, immediate trigger for one tab at a time.
    const observationInput = document.getElementById('procedureObservationInput');
    const copyBtn = document.getElementById('procedureObservationCopyBtn');
    const restoreBtn = document.getElementById('procedureObservationRestoreBtn');
    const systemBtn = document.getElementById('procedureObservationSystemBtn');
    const suggestBtn = document.getElementById('procedureObservationSuggestBtn');

    function refreshObservationButtons() {
        if (suggestBtn) {
            // Same rule as System Observations — driven only by whether a suggestion currently
            // exists, never by suggestionApplied/hasText (those gate the old auto-fill-on-open
            // behavior, not this button).
            suggestBtn.disabled = !buildObservationSuggestion(uid, docId);
        }
        if (copyBtn) {
            const hasText = !!(tabState.observation && tabState.observation.trim());
            copyBtn.disabled = !hasText;
            const isCopied = hasText && !tabState.observationDirty;
            copyBtn.classList.toggle('copied', isCopied);
            copyBtn.textContent = isCopied ? '✓ Copied' : '📋 Copy';
        }
        if (restoreBtn) {
            restoreBtn.disabled = !(tabState.lastSyncedText && tabState.lastSyncedText.trim());
        }
        if (systemBtn) {
            systemBtn.disabled = !buildObservationSuggestion(uid, docId);
        }
    }

    if (observationInput) {
        observationInput.addEventListener('input', function() {
            const wasEmpty = !(tabState.observation && tabState.observation.trim());
            tabState.observation = this.value;
            tabState.observationDirty = true;
            updateProcedureModalSavedNote(uid);
            refreshObservationButtons();
            // Deleting/emptying an observation should clear the main table's Observations badge
            // immediately, not wait for an unrelated later save — push the empty string through
            // the same explicit-sync path Copy uses. Only on the genuine clear transition (had
            // text, now doesn't), not on every keystroke while the box is already empty.
            const isEmptyNow = !this.value.trim();
            if (isEmptyNow && !wasEmpty) {
                saveLineObservation(uid, docId).then(refreshObservationButtons);
            }
        });
    }

    if (suggestBtn) {
        suggestBtn.addEventListener('click', function() {
            // Recomputed live at click time, same correctness-guard convention as System
            // Observations' handler. Fills/replaces directly — does NOT go through
            // applyObservationSuggestionIfNeeded(), whose suggestionApplied/non-empty-box guard
            // exists only for the old auto-fill-on-open behavior and would otherwise block a
            // re-fill after the box has been cleared or already suggested once.
            const suggestion = buildObservationSuggestion(uid, docId);
            if (!suggestion) return;
            tabState.observation = suggestion;
            tabState.observationDirty = true;
            if (observationInput) observationInput.value = tabState.observation;
            updateProcedureModalSavedNote(uid);
            refreshObservationButtons();
        });
    }

    if (copyBtn) {
        copyBtn.addEventListener('click', async function() {
            if (copyBtn.disabled) return;
            copyBtn.disabled = true;
            await saveLineObservation(uid, docId);
            refreshObservationButtons();
        });
    }

    if (restoreBtn) {
        restoreBtn.addEventListener('click', function() {
            if (restoreBtn.disabled) return;
            // Reverts to the last-copied snapshot only — does not itself re-sync to the voucher
            // table; a further edit (this counts as one), so Copy is needed again afterward.
            tabState.observation = tabState.lastSyncedText || '';
            tabState.observationDirty = true;
            if (observationInput) observationInput.value = tabState.observation;
            updateProcedureModalSavedNote(uid);
            refreshObservationButtons();
        });
    }

    if (systemBtn) {
        systemBtn.addEventListener('click', function() {
            // Recomputed live at click time (not the render-time value) so this stays correct
            // even if the button's disabled state is momentarily stale — e.g. a checklist box
            // ticked without switching tabs, since toggleProcedureCheck()'s success path
            // intentionally skips a full panel re-render.
            const suggestion = buildObservationSuggestion(uid, docId);
            if (!suggestion) return;
            const current = tabState.observation || '';
            // Never a stored snapshot — always appends below whatever's already there, so the
            // system text is recoverable on demand no matter how many times it's been cleared.
            // Counts as an edit, same as typing: marks dirty, does not itself sync (Copy still
            // required afterward), and leaves suggestionApplied/the one-shot auto-fill untouched.
            tabState.observation = current.trim() ? `${current}\n${suggestion}` : suggestion;
            tabState.observationDirty = true;
            if (observationInput) observationInput.value = tabState.observation;
            updateProcedureModalSavedNote(uid);
            refreshObservationButtons();
        });
    }
}

// Renders one tab's body. A document with a configured template_id gets the full panel: search
// box + results dropdown, the linked-evidence table (Phase 4), the Audit Procedures checklist
// (Phase 5), and the Observation section (Phase 6). A document with no template_id has nothing to
// search Data Bank evidence against, so its tab renders only the two segments that don't depend on
// evidence — Audit Procedures + Observation — reusing their existing markup/logic verbatim.
// Generic Voucher-Level Checks cutover: the synthetic Voucher Line tab now hosts only the Account
// Head Recommendation card — the old Prepared/Checked/Approved-By fields that used to sit below it
// were removed, replaced by the persistent voucher-level checks bar above the rail
// (renderVoucherChecksBar()).
function renderVoucherApprovalPanel(uid) {
    const container = document.getElementById('procedureModalPanels');
    if (!container) return;

    const lineIdx = currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    const voucherLine = lineIdx !== -1 ? currentVoucherLines[lineIdx] : null;
    const currentAccountName = voucherLine ? getVoucherLineAlias(voucherLine.row_data || {}, VOUCHER_LINE_ACCOUNT_NAME_ALIASES) : '—';

    container.innerHTML = `
        <div class="procedure-tab-panel" data-doc-id="${VOUCHER_LINE_TAB_ID}">
            <div class="procedure-segment procedure-segment-data">
                <h4 class="procedure-segment-header">🎯 Account Head Recommendation</h4>
                <div class="procedure-segment-body">
                    <div class="procedure-account-head-current">
                        <label>Current Account Name</label>
                        <span>${escapeHtml(currentAccountName)}</span>
                    </div>
                    <div id="procedureAccountHeadBody"></div>
                </div>
            </div>
        </div>
    `;

    renderAccountHeadCard(uid);
}

// Renders just the Account Head Recommendation segment's body (#procedureAccountHeadBody) —
// scoped re-render so toggling match/no-match mode doesn't disturb sibling state.
// Branches entirely on accountHeadCache[uid].mode:
//   'match'   — search-select over Trial Balance leaf accounts (latestTbAccountsCache), scratch-
//               only per the confirmed decision (no backend persistence), plus the existing
//               fuzzy-match Suggest flow.
//   'nomatch' — free-text finding, persisted via flushAccountHeadNoMatchNote() into the existing
//               Observation flow under the ACCOUNT_HEAD_OBSERVATION_DOC_ID sentinel.
function renderAccountHeadCard(uid) {
    const bodyEl = document.getElementById('procedureAccountHeadBody');
    if (!bodyEl) return;
    const state = getAccountHeadState(uid);

    if (state.mode === 'nomatch') {
        bodyEl.innerHTML = `
            <div class="procedure-voucher-approval-field">
                <label for="procedureAccountHeadNoMatchInput">Finding — no suitable account in Trial Balance</label>
                <textarea id="procedureAccountHeadNoMatchInput" class="procedure-observation-textarea" rows="3"
                    placeholder="Describe the recommended new account head...">${escapeHtml(state.noMatchText || '')}</textarea>
            </div>
            <button type="button" id="procedureAccountHeadToggleBtn" class="procedure-account-head-toggle-link">🔍 Search Trial Balance instead</button>
        `;
        const noMatchInput = document.getElementById('procedureAccountHeadNoMatchInput');
        if (noMatchInput) {
            noMatchInput.addEventListener('input', function() {
                state.noMatchText = this.value;
                state.noMatchDirty = true;
            });
        }
    } else {
        bodyEl.innerHTML = `
            <div class="procedure-voucher-approval-field">
                <label for="procedureAccountHeadSearchInput">Recommended Account Head</label>
                <input type="text" id="procedureAccountHeadSearchInput" class="procedure-search-input"
                    placeholder="Search Trial Balance accounts..." autocomplete="off"
                    value="${escapeHtml(state.selectedAccountName || state.searchTerm || '')}">
            </div>
            <div id="procedureAccountHeadSearchDropdown" class="procedure-search-dropdown"></div>
            <button type="button" id="procedureAccountHeadSuggestBtn" class="procedure-account-head-suggest-btn" ${state.loading ? 'disabled' : ''}>${state.loading ? 'Searching…' : '🔍 Suggest Account Head'}</button>
            <div id="procedureAccountHeadResults" class="procedure-account-head-results"></div>
            <button type="button" id="procedureAccountHeadToggleBtn" class="procedure-account-head-toggle-link">No suitable account exists</button>
        `;
        const searchInput = document.getElementById('procedureAccountHeadSearchInput');
        if (searchInput) {
            searchInput.addEventListener('input', function() {
                state.searchTerm = this.value;
                state.selectedAccountId = null;
                state.selectedAccountName = '';
                renderAccountHeadSearchDropdown(uid);
            });
        }
        const suggestBtn = document.getElementById('procedureAccountHeadSuggestBtn');
        if (suggestBtn) {
            suggestBtn.addEventListener('click', function() {
                fetchAccountHeadSuggestions(uid);
            });
        }
        renderAccountHeadResults(uid);
    }

    const toggleBtn = document.getElementById('procedureAccountHeadToggleBtn');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', function() {
            state.mode = state.mode === 'nomatch' ? 'match' : 'nomatch';
            renderAccountHeadCard(uid);
        });
    }
}

// Client-side filter over latestTbAccountsCache (already fully loaded — no debounce/pagination
// needed, unlike the Data Bank search which hits the server per keystroke). Matches on either
// account name or code, case-insensitive substring, capped at 20 rows shown at once.
function renderAccountHeadSearchDropdown(uid) {
    const dropdownEl = document.getElementById('procedureAccountHeadSearchDropdown');
    if (!dropdownEl) return;
    const state = getAccountHeadState(uid);
    const term = (state.searchTerm || '').trim().toLowerCase();
    if (!term) {
        dropdownEl.innerHTML = '';
        return;
    }
    const matches = (latestTbAccountsCache || []).filter(a =>
        (a.account_name || '').toLowerCase().includes(term) || (a.account_code || '').toLowerCase().includes(term)
    ).slice(0, 20);

    if (!matches.length) {
        dropdownEl.innerHTML = '<div class="procedure-search-empty">No matching Trial Balance accounts.</div>';
        return;
    }

    dropdownEl.innerHTML = matches.map(a => `
        <div class="procedure-result-item" data-account-id="${a.id}" data-account-name="${escapeHtml(a.account_name)}">
            <div class="procedure-result-main">
                <span>${escapeHtml(a.account_name)}</span>
                <span class="procedure-result-meta">${escapeHtml(a.account_code || '')}</span>
            </div>
        </div>
    `).join('');

    dropdownEl.querySelectorAll('.procedure-result-item').forEach(item => {
        item.addEventListener('click', function() {
            selectAccountHeadTbAccount(uid, parseInt(this.dataset.accountId, 10), this.dataset.accountName);
        });
    });
}

// Commits a leaf-account selection — called both from a manual dropdown click and from the
// Suggest flow's "Use" button. Scratch-only (no network call): the confirmed decision keeps the
// TB-match case exactly as scratch as it already was, just via a dropdown instead of free text.
function selectAccountHeadTbAccount(uid, accountId, accountName) {
    const state = getAccountHeadState(uid);
    state.selectedAccountId = accountId;
    state.selectedAccountName = accountName;
    state.searchTerm = accountName;
    const inputEl = document.getElementById('procedureAccountHeadSearchInput');
    if (inputEl) inputEl.value = accountName;
    const dropdownEl = document.getElementById('procedureAccountHeadSearchDropdown');
    if (dropdownEl) dropdownEl.innerHTML = '';
}

// Renders the results container inside the Account Head Recommendation segment based on the
// current accountHeadCache[uid] state — called after every fetch and every "Use" click. Only
// rendered in match mode (see renderAccountHeadCard()) — the no-match textarea has no fuzzy-match
// results of its own.
function renderAccountHeadResults(uid) {
    const resultsEl = document.getElementById('procedureAccountHeadResults');
    if (!resultsEl) return;
    const state = getAccountHeadState(uid);

    if (state.loading || !state.hasFetched) {
        resultsEl.innerHTML = '';
        return;
    }

    if (!state.matched) {
        resultsEl.innerHTML = `<p class="procedure-account-head-no-match">${escapeHtml(state.message || 'No match — new head likely needed')}</p>`;
        return;
    }

    resultsEl.innerHTML = state.suggestions.map((s, idx) => `
        <div class="procedure-account-head-suggestion">
            <div class="procedure-account-head-suggestion-row">
                <span class="procedure-account-head-suggestion-name">${escapeHtml(s.account_name)}</span>
                <span class="procedure-account-head-suggestion-score">Score: ${escapeHtml(String(s.score))}</span>
                <button type="button" class="procedure-account-head-use-btn" data-suggestion-idx="${idx}" title="Select this account in the dropdown above">📋 Use</button>
            </div>
            <ul class="procedure-account-head-reasons">
                ${(s.reasons || []).map(r => `<li>${escapeHtml(r)}</li>`).join('')}
            </ul>
        </div>
    `).join('');

    resultsEl.querySelectorAll('.procedure-account-head-use-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const idx = parseInt(this.dataset.suggestionIdx, 10);
            const suggestion = state.suggestions[idx];
            if (!suggestion) return;
            // The suggestion is a fuzzy-matched Trial Balance leaf account name — look it up in
            // the same latestTbAccountsCache the dropdown itself filters, so "Use" commits a real
            // account id/selection rather than free text. A miss here would mean the TB changed
            // between the suggestion being computed and now (e.g. a re-upload) — surfaced rather
            // than silently writing an unselectable value.
            const match = (latestTbAccountsCache || []).find(a => a.account_name === suggestion.account_name);
            if (match) {
                selectAccountHeadTbAccount(uid, match.id, match.account_name);
            } else {
                showStatus(`"${suggestion.account_name}" is no longer in the current Trial Balance — pick a leaf account manually if it still applies.`, 'error');
            }
        });
    });
}

// Fetches Account Head suggestions for one voucher line (Validation Rules Phase 7) from the
// already-verified GET /api/verification/suggest-account-head endpoint. Description is read from
// the cashbook line's own row_data (Narration/Particulars-style aliases), falling back to the
// account name when the alias lookup finds nothing ('—' sentinel from getVoucherLineAlias).
async function fetchAccountHeadSuggestions(uid) {
    const state = getAccountHeadState(uid);
    const lineIdx = currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    const voucherLine = lineIdx !== -1 ? currentVoucherLines[lineIdx] : null;
    const rowData = voucherLine ? (voucherLine.row_data || {}) : {};
    const accountName = getVoucherLineAlias(rowData, VOUCHER_LINE_ACCOUNT_NAME_ALIASES);
    const rawDescription = getVoucherLineAlias(rowData, VOUCHER_LINE_DESCRIPTION_ALIASES);
    const description = (rawDescription && rawDescription !== '—') ? rawDescription : accountName;
    const category = (voucherLineState[uid] && voucherLineState[uid].transactionType) || '';
    const clientId = getClientId();

    state.loading = true;
    renderAccountHeadResultsButtonState(uid);

    try {
        const params = new URLSearchParams({ client_id: clientId, description: description });
        if (category) params.set('category', category);
        const res = await fetch(`/api/verification/suggest-account-head?${params.toString()}`);
        const data = await res.json();
        state.matched = !!data.matched;
        state.suggestions = data.suggestions || [];
        state.message = data.message || '';
        state.hasFetched = true;
    } catch (e) {
        console.error('Failed to fetch account head suggestions:', e);
        state.matched = false;
        state.suggestions = [];
        state.message = "Couldn't fetch suggestions — try again.";
        state.hasFetched = true;
    } finally {
        state.loading = false;
        renderAccountHeadResultsButtonState(uid);
        renderAccountHeadResults(uid);
    }
}

// Keeps the Suggest button's disabled/label state in sync with accountHeadCache[uid].loading
// without a full panel re-render (which would drop focus/typed input elsewhere in the tab).
function renderAccountHeadResultsButtonState(uid) {
    const btn = document.getElementById('procedureAccountHeadSuggestBtn');
    if (!btn) return;
    const state = getAccountHeadState(uid);
    btn.disabled = state.loading;
    btn.textContent = state.loading ? 'Searching…' : '🔍 Suggest Account Head';
}

// ---------- CROSS-DOCUMENT VALIDATION ROLLUP (Automated cross-document validation engine, Phase 9 of 10) ----------
// Collapsed pass/fail/shortfall line per Cross-check rule, expandable to show the actual chain
// values Phase 6's engine already computed — no raw numbers by default, matching the Linked
// Evidence table's own running-total/mismatch-badge precedent (collapsed summary, expand for
// detail, not a new UI language). Pure display: every value used here already exists in the
// fetched rule result (see GET /api/verification/cross_check_results/{id}), nothing recomputed.

// One compact line per rule. A shortfall gets its own distinct (amber) state — not folded into
// fail — since Phase 7/8 already treat it differently (doesn't gate Cleared, only observed).
function summarizeCrossCheckRule(rule) {
    const field = escapeHtml(rule.compare_field);
    if (rule.status === 'fail') {
        return { icon: '✗', text: `${field} mismatch`, cssClass: 'cc-badge-fail' };
    }
    if (rule.status === 'shortfall') {
        return { icon: '⚠', text: `${field} shortfall`, cssClass: 'cc-badge-shortfall' };
    }
    if (rule.status === 'not_evaluated') {
        return { icon: '—', text: `${field} not enough data`, cssClass: 'cc-badge-neutral' };
    }
    // pass
    const linkedCount = rule.chain.filter(c => c.linked && c.value !== null).length;
    if (linkedCount <= 1 && rule.voucher_line_comparison && rule.voucher_line_comparison.match === true) {
        return { icon: '✓', text: `${field} (${rule.voucher_line_comparison.anchor_value})`, cssClass: 'cc-badge-pass' };
    }
    return { icon: '✓', text: `${field} matched across ${linkedCount} docs`, cssClass: 'cc-badge-pass' };
}

// Expanded detail: the actual per-document values feeding the rule, plus the voucher-line
// comparison (if configured) and the rule's own message — all already present in `rule`, no
// new lookups.
function buildCrossCheckRuleDetail(rule) {
    let html = '<ul class="cc-chain-list">';
    rule.chain.forEach(c => {
        const isAnchor = rule.anchor_document_id === c.document_id;
        const valueText = c.linked
            ? (c.value !== null ? escapeHtml(String(c.value)) : '<span class="cc-chain-novalue">no value</span>')
            : '<span class="cc-chain-notlinked">not linked</span>';
        html += `<li class="cc-chain-item">
            <span class="cc-chain-doc">${escapeHtml(c.document_name)}${isAnchor ? ' <span class="cc-anchor-tag">(anchor)</span>' : ''}</span>
            <span class="cc-chain-value">${valueText}</span>
        </li>`;
    });
    if (rule.voucher_line_comparison) {
        const vlc = rule.voucher_line_comparison;
        const matchIcon = vlc.match === true ? '✓' : (vlc.match === false ? '✗' : '—');
        html += `<li class="cc-chain-item cc-chain-voucher-row">
            <span class="cc-chain-doc">Voucher Line Amount</span>
            <span class="cc-chain-value">${matchIcon} ${vlc.voucher_amount !== null ? escapeHtml(String(vlc.voucher_amount)) : '—'}</span>
        </li>`;
    }
    html += '</ul>';
    if (rule.message) {
        html += `<div class="cc-rule-message cc-rule-message-${rule.status}">${escapeHtml(rule.message)}</div>`;
    }
    return html;
}

function buildCrossCheckRollup(rules) {
    let html = '<div class="cc-rollup">';
    rules.forEach(rule => {
        const { icon, text, cssClass } = summarizeCrossCheckRule(rule);
        html += `
            <div class="cc-rule">
                <div class="cc-rule-row">
                    <span class="cc-rule-badge ${cssClass}">${icon}</span>
                    <span class="cc-rule-summary-text">${text}</span>
                    <span class="cc-rule-caret">▸</span>
                </div>
                <div class="cc-rule-detail">${buildCrossCheckRuleDetail(rule)}</div>
            </div>
        `;
    });
    html += '</div>';
    return html;
}

function renderProcedureModalPanel(uid, docId) {
    const container = document.getElementById('procedureModalPanels');
    if (!container) return;
    const lineState = procedureModalState[uid];
    const doc = lineState && (lineState.documents || []).find(d => d.id === docId);
    if (!doc) { container.innerHTML = ''; return; }

    // Validation Rules Phase 5: the synthetic Voucher Line tab renders a dedicated panel, not the
    // has-template/no-template layouts below (neither concept applies — this isn't a Document).
    if (doc.__voucherTab) {
        renderVoucherApprovalPanel(uid);
        return;
    }

    // Physical Bill evidence type, Phase 7 of 8: a physical_bill Document always has
    // template_id: null, so it's intercepted here before hasTemplate/hasRoles are computed —
    // its own dedicated 4-field entry form, not the has-template/no-template layouts below.
    if (doc.document_type === 'physical_bill') {
        renderPhysicalBillDocumentPanel(uid, doc);
        return;
    }

    const tabState = getProcedureTabState(uid, docId);
    const hasTemplate = !!doc.template_id;
    const hasRoles = !!(doc.auth_roles && doc.auth_roles.length);

    // Segment-level hiding: a Document with no template_id has nothing to search Data Bank
    // evidence against, and a Document with no configured auth_roles has no procedures to check —
    // both segments are omitted entirely (header + body), not rendered as an empty placeholder.
    // Observation always renders. Order stays fixed (Data -> Procedures -> Observation) regardless
    // of which optional segments are present.
    // Automated cross-document validation engine, Phase 9 of 10: only the Cross-check rules
    // whose own chain actually includes this Document (each rule already carries its full
    // chain, so this is a pure client-side filter over already-fetched data, no re-evaluation).
    // A Document in none of the Category's Cross-check chains gets no rollup section at all —
    // not an empty one.
    const docCrossChecks = (lineState.crossCheckResults || [])
        .filter(rule => rule.chain.some(c => c.document_id === docId));

    const dataSegmentHtml = hasTemplate ? `
        <div class="procedure-segment procedure-segment-data">
            <h4 class="procedure-segment-header">🗂️ Data Table</h4>
            <div class="procedure-segment-body">
                ${docCrossChecks.length ? buildCrossCheckRollup(docCrossChecks) : ''}
                <div class="procedure-search-row">
                    <input type="text" id="procedureSearchInput" class="procedure-search-input"
                        placeholder="Search by reference, vendor, amount..." value="${escapeHtml(tabState.searchTerm)}">
                    <button type="button" id="procedureBrowseBtn" class="procedure-browse-btn" title="Browse all rows for this document">👁️</button>
                </div>
                <div id="procedureSearchDropdown" class="procedure-search-dropdown"></div>
                <div class="procedure-selected-section">
                    <h4 class="procedure-selected-heading">Linked evidence</h4>
                    ${buildSelectedLinesTable(uid, docId)}
                </div>
            </div>
        </div>
    ` : '';

    // Select-all/clear-all icon: "all selected" means every configured role is currently ticked
    // (same roles.length>0 && every(...) rule isProcedureDocumentComplete() already uses).
    const allRolesChecked = hasRoles && doc.auth_roles.every(r => tabState.checklist[r] && tabState.checklist[r].is_checked);
    const proceduresSegmentHtml = hasRoles ? `
        <div class="procedure-segment procedure-segment-procedures">
            <h4 class="procedure-segment-header procedure-segment-header-with-toggle">
                <span>✅ Audit Procedures</span>
                <button type="button" id="procedureChecklistSelectAllBtn"
                    class="procedure-select-all-btn${allRolesChecked ? ' all-selected' : ''}"
                    title="${allRolesChecked ? 'Untick all' : 'Tick all'}">${ICON_SELECT_ALL_SVG}</button>
            </h4>
            <div class="procedure-segment-body">${buildProcedureChecklist(uid, docId)}</div>
        </div>
    ` : '';

    container.innerHTML = `
        <div class="procedure-tab-panel" data-doc-id="${docId}">
            ${dataSegmentHtml}
            ${proceduresSegmentHtml}
            <div class="procedure-segment procedure-segment-observation">
                <h4 class="procedure-segment-header">📝 Observation</h4>
                <div class="procedure-segment-body">${buildObservationSection(uid, docId)}</div>
            </div>
        </div>
    `;

    if (hasTemplate) {
        const searchInput = document.getElementById('procedureSearchInput');
        if (searchInput) {
            searchInput.addEventListener('input', function() {
                debounceProcedureSearch(uid, docId, this.value);
            });
        }
        const browseBtn = document.getElementById('procedureBrowseBtn');
        if (browseBtn) {
            browseBtn.addEventListener('click', function() {
                openProcedureBrowseModal(uid, docId);
            });
        }
    }

    wireProcedureChecklistAndObservation(container, uid, docId, tabState);

    if (hasTemplate) {
        // Unassign requirement: one click listener per ✕ unlink button in the Linked Evidence table.
        container.querySelectorAll('.procedure-unlink-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                unassignSupportingDocument(uid, docId, parseInt(this.dataset.assignmentId));
            });
        });

        // Phase 9 of 10: expand/collapse is a plain class toggle, not persisted state — it
        // simply resets on the next re-render (tab switch, modal reopen), same as this being a
        // lightweight disclosure rather than durable UI state like tabState.selections.
        container.querySelectorAll('.cc-rule-row').forEach(row => {
            row.addEventListener('click', function() {
                this.closest('.cc-rule').classList.toggle('cc-rule-expanded');
            });
        });
    }

    // Restore a previously-fetched search's dropdown (extends Phase 3's "switching tabs doesn't
    // lose unsaved work" to cover search results too) — re-renders from tabState, no new fetch.
    if (tabState.searchResults && tabState.searchResults.length) {
        renderProcedureSearchDropdown(uid, docId);
    }
}

// Bulk-toggles every configured role's tick for one document's Audit Procedures checklist. Not a
// per-role toggleProcedureCheck() loop (that function individually re-renders the whole panel on
// failure, which would race against the other in-flight calls) — instead applies the optimistic
// update once, batches the saves, and re-renders once on success or failure, same shape as
// toggleAllVoucherChecks() above.
async function toggleAllProcedureChecks(uid, docId) {
    const lineState = procedureModalState[uid];
    const doc = lineState && (lineState.documents || []).find(d => d.id === docId);
    const roles = (doc && doc.auth_roles) || [];
    if (!roles.length) return;
    const tabState = getProcedureTabState(uid, docId);
    const allChecked = roles.every(r => tabState.checklist[r] && tabState.checklist[r].is_checked);
    const newChecked = !allChecked;
    const toChange = roles.filter(r => !!(tabState.checklist[r] && tabState.checklist[r].is_checked) !== newChecked);
    if (!toChange.length) return;

    const previousStates = {};
    toChange.forEach(role => {
        previousStates[role] = tabState.checklist[role] ? { ...tabState.checklist[role] } : null;
        if (!tabState.checklist[role]) tabState.checklist[role] = {};
        tabState.checklist[role].is_checked = newChecked;
    });
    renderProcedureModalPanel(uid, docId);
    renderProcedureModalTabs(uid);

    try {
        const responses = await Promise.all(toChange.map(role => fetch(`${API_BASE}/api/verification/procedure_checks`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                voucher_line_id: parseInt(uid),
                document_id: docId,
                role_name: role,
                is_checked: newChecked
            })
        })));
        for (const res of responses) {
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || 'Failed to save procedure check.');
            }
        }
        const updatedList = await Promise.all(responses.map(res => res.json()));
        toChange.forEach((role, i) => {
            const updated = updatedList[i];
            tabState.checklist[role] = {
                is_checked: updated.is_checked,
                checked_by: updated.checked_by,
                checked_at: updated.checked_at
            };
        });
        renderProcedureModalTabs(uid);
    } catch (e) {
        console.error('Failed to save all audit procedure checks:', e);
        toChange.forEach(role => {
            if (previousStates[role]) {
                tabState.checklist[role] = previousStates[role];
            } else {
                delete tabState.checklist[role];
            }
        });
        showStatus('Failed to update all procedures — please try again.', 'error');
        renderProcedureModalPanel(uid, docId);
        renderProcedureModalTabs(uid);
    }
}

// ---------- PHYSICAL BILL ENTRY (evidence type, Phase 7 of 8) ----------
// Small line-style SVG icons (stroke=currentColor, no fill) used throughout this form in place of
// emoji — purely decorative, sized/aligned via the shared .pb-icon wrapper class in style.css.
const PB_ICON_RECEIPT = '<svg class="pb-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h12v18l-3-2-3 2-3-2-3 2V3z"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="9" y1="12" x2="15" y2="12"/></svg>';
const PB_ICON_DETAILS = '<svg class="pb-icon" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>';
const PB_ICON_CALC = '<svg class="pb-icon" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="2" width="16" height="20" rx="2"/><line x1="8" y1="6" x2="16" y2="6"/><line x1="8" y1="14" x2="8.01" y2="14"/><line x1="12" y1="14" x2="12.01" y2="14"/><line x1="16" y1="14" x2="16" y2="18"/><line x1="8" y1="18" x2="8.01" y2="18"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>';
const PB_ICON_COPY = '<svg class="pb-icon" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
const PB_ICON_SAVE = '<svg class="pb-icon" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>';
const PB_ICON_CHECK = '<svg class="pb-icon" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

// Client-side mirror of Phase 5's backend validation (save_physical_bill, routers/verification.py):
// Bill No/Vendor name non-blank, Amount numeric, Date non-blank and a valid date. Fails soft — the
// caller renders these as inline messages under each field, never a blocking alert(). Returns a
// {field: message} object; empty means valid.
function validatePhysicalBillFields(bill) {
    const errors = {};
    if (!bill.bill_no || !bill.bill_no.trim()) errors.bill_no = 'Bill No is required.';
    if (!bill.vendor_name || !bill.vendor_name.trim()) errors.vendor_name = 'Vendor name is required.';
    if (bill.amount === '' || bill.amount == null || isNaN(parseFloat(bill.amount))) {
        errors.amount = 'Amount must be numeric.';
    }
    if (!bill.bill_date) {
        errors.bill_date = 'Date is required.';
    } else if (isNaN(Date.parse(bill.bill_date))) {
        errors.bill_date = 'Invalid date.';
    }
    return errors;
}

// Dedicated panel for a physical_bill Document — a 4-field entry form (Bill No/Date/Amount/Vendor
// name) instead of the has-template search/evidence table or the no-template role checklist,
// since neither concept applies to this doc type (Phase 6 backend note: a physical_bill Document
// always has template_id: null, and its auth_roles — if any — are no longer read by the Cleared/
// Query gate for this doc type, so no Audit Procedures segment is rendered here either). The
// Observation segment is unchanged, same as every other document type.
// Live, purely visual signal — shows a small green tick beside the Physical Bill Amount field the
// instant its value exactly matches the voucher line's own amount (same 0.005 epsilon
// _physical_bill_satisfied() uses server-side). Never touches physicalBillSaved/the Save button/
// any backend call — the tick is just a hint while typing, independent of what's actually saved.
function updateAmountMatchTick(uid, rawValue) {
    const tick = document.getElementById('procedureBillAmountTick');
    if (!tick) return;
    const lineState = procedureModalState[uid];
    const voucherAmount = (lineState && lineState.voucherLineAmount) || 0;
    const parsed = parseFloat(rawValue);
    const matches = !isNaN(parsed) && Math.abs(parsed - voucherAmount) <= 0.005;
    tick.style.display = matches ? '' : 'none';
}

// Formats a raw amount value for the prominent "Total Amount" readout at the bottom of the
// Quantity/Rate/Amount sub-section — display only, never fed back into what gets saved.
function formatBillTotalDisplay(value) {
    const num = parseFloat(value);
    if (isNaN(num)) return '—';
    return num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Strips a Quantity/Rate input's typed value down to digits and at most one decimal point (confirmed
// fix) — used on every 'input' event so pasted/typed letters and symbols never survive, and a second
// '.' collapses away rather than producing an unparseable string like "1.2.3".
function pbSanitizeNumericInput(str) {
    let cleaned = str.replace(/[^0-9.]/g, '');
    const firstDot = cleaned.indexOf('.');
    if (firstDot !== -1) {
        cleaned = cleaned.slice(0, firstDot + 1) + cleaned.slice(firstDot + 1).replace(/\./g, '');
    }
    return cleaned;
}

// Populate Physical Bill from GRN/ERP Vendor Bill (Phase 3 of 4); restyle (confirmed fix): Amount
// is now always a read-only, greyed field — never manually typed — live-computed as Qty × Rate
// whenever both are currently valid non-blank numbers. When they aren't (a brand-new entry with
// nothing typed yet, or a legacy bill saved before Quantity/Rate existed), the field simply keeps
// showing whatever value is already there (blank, or the originally-saved Amount) rather than
// becoming editable — there is no manual-entry path for Amount any more. This is a live UI preview
// only — the authoritative rounding (rate_precision) happens server-side in save_physical_bill(),
// same convention as the rest of this app's client-side "computed" fields. Also drives the
// prominent Total Amount readout beneath the field grid, which always mirrors this same value.
function updatePhysicalBillAmount(uid, docId) {
    const qtyEl = document.getElementById('procedureBillQuantityInput');
    const rateEl = document.getElementById('procedureBillRateInput');
    const amountEl = document.getElementById('procedureBillAmountInput');
    const totalEl = document.getElementById('procedureBillTotalValue');
    if (!amountEl) return;
    const qtyStr = qtyEl ? qtyEl.value : '';
    const rateStr = rateEl ? rateEl.value : '';
    const qty = parseFloat(qtyStr);
    const rate = parseFloat(rateStr);
    const bothValid = qtyStr !== '' && rateStr !== '' && !isNaN(qty) && !isNaN(rate);
    if (bothValid) {
        const computedStr = (qty * rate).toFixed(2);
        amountEl.value = computedStr;
        const tabState = getProcedureTabState(uid, docId);
        if (!tabState.physicalBill) tabState.physicalBill = { bill_no: '', bill_date: '', amount: '', vendor_name: '', quantity: '', rate: '' };
        tabState.physicalBill.amount = computedStr;
    }
    if (totalEl) totalEl.textContent = formatBillTotalDisplay(amountEl.value);
    updateAmountMatchTick(uid, amountEl.value);
}

function renderPhysicalBillDocumentPanel(uid, doc) {
    const container = document.getElementById('procedureModalPanels');
    if (!container) return;
    const tabState = getProcedureTabState(uid, doc.id);
    const bill = tabState.physicalBill || { bill_no: '', bill_date: '', amount: '', vendor_name: '', quantity: '', rate: '' };
    const errors = tabState.physicalBillErrors || {};
    const fieldError = (field) => errors[field] ? `<div class="procedure-bill-field-error">${escapeHtml(errors[field])}</div>` : '';

    // Copy from lines (Phase 3 amendment): every GRN/Vendor Bill row already linked as evidence
    // elsewhere on this voucher line — button disabled with none, auto-fills directly with
    // exactly one (handled in the click listener below), opens a small picker for 2+.
    const eligibleSourceRows = getEligiblePhysicalBillSourceRows(uid);
    const initialTotalDisplay = formatBillTotalDisplay(bill.amount);

    container.innerHTML = `
        <div class="procedure-tab-panel" data-doc-id="${doc.id}">
            <div class="procedure-segment procedure-segment-data">
                <h4 class="procedure-segment-header">${PB_ICON_RECEIPT} Physical Bill</h4>
                <div class="procedure-segment-body">
                    <div class="procedure-bill-form">
                        <div class="procedure-bill-section">
                            <div class="procedure-bill-section-header">
                                <div class="procedure-bill-section-title">${PB_ICON_DETAILS} Bill Details</div>
                                <button type="button" id="procedureBillCopyBtn" class="procedure-bill-copy-btn" ${!eligibleSourceRows.length ? 'disabled' : ''} title="${!eligibleSourceRows.length ? 'No GRN or Vendor Bill evidence linked to this voucher line yet' : 'Copy Vendor/Qty/Rate from a linked GRN or Vendor Bill row'}">${PB_ICON_COPY} Copy from lines</button>
                            </div>
                            <div id="procedureBillCopyPicker" class="procedure-search-dropdown" style="display:none;"></div>
                            <div class="procedure-bill-grid">
                                <div class="procedure-bill-field">
                                    <label for="procedureBillNoInput">Bill No</label>
                                    <input type="text" id="procedureBillNoInput" class="procedure-bill-input" value="${escapeHtml(bill.bill_no)}">
                                    ${fieldError('bill_no')}
                                </div>
                                <div class="procedure-bill-field">
                                    <label for="procedureBillDateInput">Date</label>
                                    <input type="date" id="procedureBillDateInput" class="procedure-bill-input" value="${escapeHtml(bill.bill_date)}">
                                    ${fieldError('bill_date')}
                                </div>
                                <div class="procedure-bill-field procedure-bill-field-full">
                                    <label for="procedureBillVendorInput">Vendor Name</label>
                                    <input type="text" id="procedureBillVendorInput" class="procedure-bill-input" value="${escapeHtml(bill.vendor_name)}">
                                    ${fieldError('vendor_name')}
                                </div>
                            </div>
                        </div>

                        <div class="procedure-bill-section procedure-bill-section-amounts">
                            <div class="procedure-bill-section-title">${PB_ICON_CALC} Quantity &amp; Amount</div>
                            <div class="procedure-bill-grid procedure-bill-grid-3">
                                <div class="procedure-bill-field">
                                    <label for="procedureBillQuantityInput">Quantity</label>
                                    <div class="procedure-bill-calc-field" data-calc-field="quantity">
                                        <input type="text" inputmode="decimal" id="procedureBillQuantityInput" class="procedure-bill-input procedure-bill-calc-input" value="${escapeHtml(bill.quantity)}" placeholder="0" title="Type a value, or tap the calculator icon">
                                        <span class="procedure-bill-calc-icon">${PB_ICON_CALC}</span>
                                    </div>
                                </div>
                                <div class="procedure-bill-field">
                                    <label for="procedureBillRateInput">Rate</label>
                                    <div class="procedure-bill-calc-field" data-calc-field="rate">
                                        <input type="text" inputmode="decimal" id="procedureBillRateInput" class="procedure-bill-input procedure-bill-calc-input" value="${escapeHtml(bill.rate)}" placeholder="0" title="Type a value, or tap the calculator icon">
                                        <span class="procedure-bill-calc-icon">${PB_ICON_CALC}</span>
                                    </div>
                                </div>
                                <div class="procedure-bill-field">
                                    <label for="procedureBillAmountInput">Amount</label>
                                    <div class="procedure-bill-amount-row">
                                        <input type="number" step="any" id="procedureBillAmountInput" class="procedure-bill-input procedure-bill-input-computed" value="${escapeHtml(bill.amount)}" readonly title="Auto-calculated as Quantity × Rate">
                                        <span id="procedureBillAmountTick" class="procedure-amount-match-tick" style="display:none;" title="Matches voucher line amount">${PB_ICON_CHECK}</span>
                                    </div>
                                    ${fieldError('amount')}
                                </div>
                            </div>
                            <div class="procedure-bill-total-row">
                                <span class="procedure-bill-total-label">Total Amount</span>
                                <span class="procedure-bill-total-value" id="procedureBillTotalValue">${initialTotalDisplay}</span>
                            </div>
                        </div>

                        ${errors._general ? `<div class="procedure-bill-general-error">${escapeHtml(errors._general)}</div>` : ''}
                        <button type="button" id="procedureBillSaveBtn" class="procedure-bill-save-primary" ${tabState.physicalBillSaving ? 'disabled' : ''}>
                            ${tabState.physicalBillSaving ? 'Saving…' : `${PB_ICON_SAVE} ${tabState.physicalBillSaved ? 'Update Bill' : 'Save Bill'}`}
                        </button>
                        <div class="procedure-bill-save-status">${(!tabState.physicalBillSaving && tabState.physicalBillSaved) ? `${PB_ICON_CHECK} Saved` : ''}</div>
                    </div>
                </div>
            </div>
            <div class="procedure-segment procedure-segment-observation">
                <h4 class="procedure-segment-header">📝 Observation</h4>
                <div class="procedure-segment-body">${buildObservationSection(uid, doc.id)}</div>
            </div>
        </div>
    `;

    // Amount is always read-only/computed. Quantity/Rate are normal editable inputs with two entry
    // routes (confirmed fix): typing directly (this loop) or the calculator popover (below) — both
    // write into tabState.physicalBill the same way and both recalculate Amount.
    const fieldInputs = {
        bill_no: document.getElementById('procedureBillNoInput'),
        bill_date: document.getElementById('procedureBillDateInput'),
        vendor_name: document.getElementById('procedureBillVendorInput')
    };
    Object.keys(fieldInputs).forEach(field => {
        const el = fieldInputs[field];
        if (el) {
            el.addEventListener('input', function() {
                if (!tabState.physicalBill) tabState.physicalBill = { bill_no: '', bill_date: '', amount: '', vendor_name: '', quantity: '', rate: '' };
                tabState.physicalBill[field] = this.value;
            });
        }
    });
    const calcFieldInputs = {
        quantity: document.getElementById('procedureBillQuantityInput'),
        rate: document.getElementById('procedureBillRateInput')
    };
    Object.keys(calcFieldInputs).forEach(field => {
        const el = calcFieldInputs[field];
        if (!el) return;
        // Manual typing is restricted to digits and a single decimal point — strip anything else
        // (letters, symbols, a second '.') on every input, including paste, not just keystrokes.
        el.addEventListener('input', function() {
            const cleaned = pbSanitizeNumericInput(this.value);
            if (cleaned !== this.value) this.value = cleaned;
            if (!tabState.physicalBill) tabState.physicalBill = { bill_no: '', bill_date: '', amount: '', vendor_name: '', quantity: '', rate: '' };
            tabState.physicalBill[field] = cleaned;
            updatePhysicalBillAmount(uid, doc.id);
        });
    });
    // Enter-key flow: Quantity's Enter confirms the value and moves focus to Rate (the next field
    // in this group); Rate's Enter confirms the value, recalculates Amount, and blurs entirely (no
    // destination field) since Rate is the last field. Amount is already kept in sync live by the
    // 'input' listener above — the explicit updatePhysicalBillAmount() call here just guarantees a
    // correct recalculation even for an Enter press with no preceding value change.
    if (calcFieldInputs.quantity) {
        calcFieldInputs.quantity.addEventListener('keydown', function(e) {
            if (e.key !== 'Enter') return;
            e.preventDefault();
            updatePhysicalBillAmount(uid, doc.id);
            if (calcFieldInputs.rate) calcFieldInputs.rate.focus();
        });
    }
    if (calcFieldInputs.rate) {
        calcFieldInputs.rate.addEventListener('keydown', function(e) {
            if (e.key !== 'Enter') return;
            e.preventDefault();
            updatePhysicalBillAmount(uid, doc.id);
            this.blur();
        });
    }
    updatePhysicalBillAmount(uid, doc.id);

    // Quantity/Rate calculator trigger — opens only via the small calculator icon next to the field
    // (confirmed fix), never by clicking the field itself, since the field is a normal typable input.
    // "Done" writes the result back into the field/tabState and recalculates Amount (see
    // openPhysicalBillCalculator()/pbCalcApplyDone() below). triggerEl is the actual <input> (not the
    // wrapper div) so Done's `triggerEl.value = ...` visibly updates the field.
    container.querySelectorAll('.procedure-bill-calc-icon').forEach(icon => {
        icon.addEventListener('click', function(e) {
            e.stopPropagation();
            const wrapper = this.closest('.procedure-bill-calc-field');
            if (!wrapper) return;
            const field = wrapper.dataset.calcField;
            const input = wrapper.querySelector('input');
            openPhysicalBillCalculator(uid, doc.id, field, input);
        });
    });

    const copyBtn = document.getElementById('procedureBillCopyBtn');
    if (copyBtn) {
        copyBtn.addEventListener('click', function() {
            const eligible = getEligiblePhysicalBillSourceRows(uid);
            if (!eligible.length) return;
            if (eligible.length === 1) {
                applyPhysicalBillSource(uid, doc.id, eligible[0].client_data_id, eligible[0].source_type, this);
            } else {
                togglePhysicalBillCopyPicker(uid, doc.id, eligible);
            }
        });
    }

    const saveBtn = document.getElementById('procedureBillSaveBtn');
    if (saveBtn) {
        saveBtn.addEventListener('click', function() {
            savePhysicalBillEntry(uid, doc.id);
        });
    }

    wireProcedureChecklistAndObservation(container, uid, doc.id, tabState);
}

// ---------- QUANTITY/RATE CALCULATOR POPOVER (confirmed fix) ----------
// Quantity and Rate are read-only, tap-to-open fields — this is the shared 4-function calculator
// popover that opens on click. Single global instance (only one field's calculator can be open at
// a time, mirroring togglePhysicalBillCopyPicker()'s one-at-a-time convention); pbCalcState holds
// which uid/docId/field it's editing plus the standard accumulator/pendingOp/waitingForOperand
// state every basic calculator app uses (operator applies to the running accumulator immediately,
// not full order-of-operations — matches a physical desk calculator, not a scientific one).
let pbCalcState = null;

const PB_CALC_KEYPAD = [
    ['C', '⌫', '%', '÷'],
    ['7', '8', '9', '×'],
    ['4', '5', '6', '-'],
    ['1', '2', '3', '+'],
    ['0', '.', '=', 'Done']
];

function pbCalcButtonClass(key) {
    if (key === 'Done') return 'pb-calc-btn-done';
    if (key === '=') return 'pb-calc-btn-equals';
    if (key === 'C' || key === '⌫') return 'pb-calc-btn-fn';
    if (key === '+' || key === '-' || key === '×' || key === '÷' || key === '%') return 'pb-calc-btn-op';
    return 'pb-calc-btn-digit';
}

// Rounds to 12 significant digits to shed floating-point artifacts (e.g. 0.1+0.2) while still
// allowing genuinely precise results, then renders as a plain string.
function pbCalcFormat(num) {
    if (num == null || isNaN(num) || !isFinite(num)) return 'Error';
    return String(parseFloat(num.toPrecision(12)));
}

function pbCalcCompute(a, b, op) {
    switch (op) {
        case '+': return a + b;
        case '-': return a - b;
        case '×': return a * b;
        case '÷': return b === 0 ? NaN : a / b;
        default: return b;
    }
}

function getPhysicalBillCalcPopover() {
    let popover = document.getElementById('pbCalcPopover');
    if (!popover) {
        popover = document.createElement('div');
        popover.id = 'pbCalcPopover';
        popover.className = 'pb-calc-popover';
        popover.style.display = 'none';
        document.body.appendChild(popover);
    }
    return popover;
}

function closePhysicalBillCalculator() {
    const popover = document.getElementById('pbCalcPopover');
    if (popover) {
        popover.style.display = 'none';
        popover.innerHTML = '';
    }
    pbCalcState = null;
}

// Positions the popover near the tapped field, clamped to the viewport — same pattern as
// showVoucherChangePopover()/showAuditGuidancePopover() elsewhere in this file.
function positionPhysicalBillCalculator(anchorEl) {
    const popover = getPhysicalBillCalcPopover();
    const rect = anchorEl.getBoundingClientRect();
    const popoverWidth = 220;
    let left = rect.left;
    if (left + popoverWidth > window.innerWidth) {
        left = Math.max(8, window.innerWidth - popoverWidth - 8);
    }
    popover.style.left = left + 'px';

    const popoverHeight = 300;
    const spaceBelow = window.innerHeight - rect.bottom;
    if (spaceBelow >= popoverHeight + 6) {
        popover.style.top = (rect.bottom + 6) + 'px';
        popover.style.bottom = 'auto';
    } else {
        popover.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
        popover.style.top = 'auto';
    }
    popover.style.display = 'block';
}

function renderPhysicalBillCalculator() {
    if (!pbCalcState) return;
    const popover = getPhysicalBillCalcPopover();
    const rows = PB_CALC_KEYPAD.map(row => `
        <div class="pb-calc-row">
            ${row.map(key => `<button type="button" class="pb-calc-btn ${pbCalcButtonClass(key)}" data-key="${escapeHtml(key)}">${escapeHtml(key)}</button>`).join('')}
        </div>
    `).join('');
    popover.innerHTML = `
        <div class="pb-calc-display">${escapeHtml(pbCalcState.display)}</div>
        <div class="pb-calc-pad">${rows}</div>
    `;
    popover.querySelectorAll('.pb-calc-btn').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            handlePbCalcKey(this.dataset.key);
        });
    });
}

// Opens (or, if the same field's popover is already open, closes) the calculator for `field`
// ('quantity'/'rate') on doc `docId`'s Physical Bill tab — pre-seeds the display from the field's
// current value so re-opening to make a small correction doesn't start from zero.
function openPhysicalBillCalculator(uid, docId, field, triggerEl) {
    if (pbCalcState && pbCalcState.triggerEl === triggerEl) {
        closePhysicalBillCalculator();
        return;
    }
    const tabState = getProcedureTabState(uid, docId);
    const bill = tabState.physicalBill || {};
    const currentVal = bill[field];
    const startDisplay = (currentVal !== '' && currentVal != null && !isNaN(parseFloat(currentVal)))
        ? pbCalcFormat(parseFloat(currentVal))
        : '0';
    pbCalcState = {
        uid, docId, field, triggerEl,
        display: startDisplay,
        accumulator: null,
        pendingOp: null,
        // Waiting-for-operand means the next digit typed replaces (rather than appends to) the
        // display — true here so tapping a digit after reopening starts a fresh number instead of
        // appending onto the pre-filled value.
        waitingForOperand: true
    };
    renderPhysicalBillCalculator();
    positionPhysicalBillCalculator(triggerEl);
}

function handlePbCalcKey(key) {
    if (!pbCalcState) return;
    if (key === 'Done') {
        pbCalcApplyDone();
        return;
    }
    if (key === 'C') {
        pbCalcState.display = '0';
        pbCalcState.accumulator = null;
        pbCalcState.pendingOp = null;
        pbCalcState.waitingForOperand = false;
        renderPhysicalBillCalculator();
        return;
    }
    if (key === '⌫') {
        if (!pbCalcState.waitingForOperand) {
            pbCalcState.display = pbCalcState.display.length > 1 ? pbCalcState.display.slice(0, -1) : '0';
        }
        renderPhysicalBillCalculator();
        return;
    }
    if (key === '.') {
        if (pbCalcState.waitingForOperand) {
            pbCalcState.display = '0.';
            pbCalcState.waitingForOperand = false;
        } else if (pbCalcState.display.indexOf('.') === -1) {
            pbCalcState.display += '.';
        }
        renderPhysicalBillCalculator();
        return;
    }
    if (key === '%') {
        const val = parseFloat(pbCalcState.display) || 0;
        pbCalcState.display = pbCalcFormat(val / 100);
        pbCalcState.waitingForOperand = true;
        renderPhysicalBillCalculator();
        return;
    }
    if (key === '+' || key === '-' || key === '×' || key === '÷' || key === '=') {
        // Pressing an operator right after another operator (no new digit typed yet) just swaps
        // which operator is pending, rather than computing against an unchanged operand twice.
        if (pbCalcState.pendingOp && pbCalcState.waitingForOperand) {
            pbCalcState.pendingOp = key === '=' ? null : key;
            renderPhysicalBillCalculator();
            return;
        }
        const inputValue = parseFloat(pbCalcState.display) || 0;
        if (pbCalcState.accumulator === null) {
            pbCalcState.accumulator = inputValue;
        } else if (pbCalcState.pendingOp) {
            pbCalcState.accumulator = pbCalcCompute(pbCalcState.accumulator, inputValue, pbCalcState.pendingOp);
        }
        pbCalcState.display = pbCalcFormat(pbCalcState.accumulator);
        pbCalcState.pendingOp = key === '=' ? null : key;
        pbCalcState.waitingForOperand = true;
        renderPhysicalBillCalculator();
        return;
    }
    // A digit 0-9.
    if (pbCalcState.waitingForOperand) {
        pbCalcState.display = key;
        pbCalcState.waitingForOperand = false;
    } else {
        pbCalcState.display = pbCalcState.display === '0' ? key : pbCalcState.display + key;
    }
    renderPhysicalBillCalculator();
}

// "Done" — commits the calculator's current display into the target field (both the visible input
// and tabState.physicalBill), closes the popover, then recalculates Amount exactly as if the value
// had been typed directly (updatePhysicalBillAmount() is unchanged, still Qty × Rate).
function pbCalcApplyDone() {
    if (!pbCalcState) return;
    const { uid, docId, field, triggerEl } = pbCalcState;
    const finalValue = parseFloat(pbCalcState.display);
    const valueStr = isNaN(finalValue) ? '' : pbCalcFormat(finalValue);
    const tabState = getProcedureTabState(uid, docId);
    if (!tabState.physicalBill) tabState.physicalBill = { bill_no: '', bill_date: '', amount: '', vendor_name: '', quantity: '', rate: '' };
    tabState.physicalBill[field] = valueStr;
    if (triggerEl) triggerEl.value = valueStr;
    closePhysicalBillCalculator();
    updatePhysicalBillAmount(uid, docId);
}

// Close on any click outside the popover and outside the field that opened it (that field's own
// click handler already handles the "click again to close" toggle case — see
// openPhysicalBillCalculator() above).
document.addEventListener('click', function(e) {
    if (!pbCalcState) return;
    const popover = document.getElementById('pbCalcPopover');
    if (!popover || popover.style.display !== 'block') return;
    if (popover.contains(e.target)) return;
    if (pbCalcState.triggerEl && pbCalcState.triggerEl.contains(e.target)) return;
    closePhysicalBillCalculator();
});

// Saves one Physical Bill entry (POST save_physical_bill, upsert). Validates client-side first —
// on a validation failure, sets physicalBillErrors and re-renders with inline messages, no network
// call and no blocking alert(). On success, marks physicalBillSaved so the tab dot/subtext (rail)
// and the panel's Save/Update button+status both reflect committed truth immediately.
async function savePhysicalBillEntry(uid, docId) {
    const tabState = getProcedureTabState(uid, docId);
    const bill = tabState.physicalBill || { bill_no: '', bill_date: '', amount: '', vendor_name: '', quantity: '', rate: '' };
    const errors = validatePhysicalBillFields(bill);
    if (Object.keys(errors).length) {
        tabState.physicalBillErrors = errors;
        renderProcedureModalPanel(uid, docId);
        return;
    }

    tabState.physicalBillErrors = {};
    tabState.physicalBillSaving = true;
    renderProcedureModalPanel(uid, docId);

    // Populate Physical Bill from GRN/ERP Vendor Bill (Phase 3 of 4): quantity/rate/source_* are
    // all optional on the backend — a manually-entered bill with no source row sends null/null/
    // null/null here and gets byte-identical behavior to before this phase (save_physical_bill()
    // then requires and uses the typed Amount as-is).
    const quantityVal = (bill.quantity !== '' && bill.quantity != null) ? parseFloat(bill.quantity) : null;
    const rateVal = (bill.rate !== '' && bill.rate != null) ? parseFloat(bill.rate) : null;

    try {
        const res = await fetch(`${API_BASE}/api/verification/save_physical_bill`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                voucher_line_id: parseInt(uid),
                document_id: docId,
                bill_no: bill.bill_no.trim(),
                bill_date: bill.bill_date,
                amount: parseFloat(bill.amount),
                vendor_name: bill.vendor_name.trim(),
                quantity: quantityVal,
                rate: rateVal,
                source_document_row_id: tabState.physicalBillSourceDocumentRowId || null,
                source_template: tabState.physicalBillSourceTemplate || null
            })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to save physical bill.');
        }
        tabState.physicalBillSaved = true;
        tabState.physicalBillSaving = false;
        tabState.physicalBillErrors = {};
        renderProcedureModalTabs(uid);
        renderProcedureModalPanel(uid, docId);
    } catch (e) {
        console.error('Failed to save physical bill:', e);
        tabState.physicalBillSaving = false;
        tabState.physicalBillErrors = { _general: e.message || 'Save failed — please try again.' };
        renderProcedureModalPanel(uid, docId);
    }
}

// ---------- COPY FROM LINES (Phase 3 amendment, iDempiere-style) ----------
// Replaces the earlier per-source-row trigger design: a single "Copy from lines" button lives on
// the Physical Bill panel itself instead of on each GRN/Vendor Bill evidence row. Resolves the
// source template (GRN vs ERP Vendor Bill) purely from templateNameCache — same case-insensitive
// match resolve_physical_bill_fields_from_source() (routers/verification.py) uses via its own
// PHYSICAL_BILL_SOURCE_TEMPLATE_NAMES.
function resolvePhysicalBillSourceType(templateId) {
    const templateName = (templateNameCache[templateId] || '').trim().toLowerCase();
    if (templateName === 'store receiving') return 'grn';
    if (templateName === 'vendor bill') return 'vendor_bill';
    return null;
}

// Every GRN/Vendor Bill row currently linked as evidence anywhere in this line's Category — no
// new fetch needed: openProcedureModal() already seeds every document's tabState.selections (not
// just the active tab's) from the batch assignedRecords fetch, and already warms
// templateNameCache for every document's template_id before any tab renders. Bill No is read via
// the same per-source-type field the backend itself resolves (PHYSICAL_BILL_SOURCE_BILL_NO_FIELD)
// so the picker's label always matches what a click on that row would actually populate.
// extract_date is display-only here — the backend never resolves a date automatically.
function getEligiblePhysicalBillSourceRows(uid) {
    const lineState = procedureModalState[uid];
    if (!lineState) return [];
    const rows = [];
    (lineState.documents || []).forEach(otherDoc => {
        const sourceType = resolvePhysicalBillSourceType(otherDoc.template_id);
        if (!sourceType) return;
        const tabState = getProcedureTabState(uid, otherDoc.id);
        (tabState.selections || []).forEach(sel => {
            rows.push({
                client_data_id: sel.client_data_id,
                source_type: sourceType,
                document_name: otherDoc.name,
                vendor: getRowFieldCaseInsensitive(sel.row_data, 'Vendor'),
                bill_no: getRowFieldCaseInsensitive(sel.row_data, sourceType === 'grn' ? 'Bill no' : 'vendor Bill no'),
                extract_date: sel.extract_date
            });
        });
    });
    return rows;
}

// Fetches the read-only preview (Phase 2's physical_bill_source_preview endpoint) and pre-fills
// docId's own tab state with it — Bill No/Vendor Name/Quantity/Rate from the source row, Date
// always left blank for manual entry (never resolved server-side either), Amount left for
// updatePhysicalBillAmount() to compute live once the panel renders. Nothing is saved here — the
// intern reviews/edits then clicks the existing Save Bill button, same savePhysicalBillEntry()
// path as a fully manual entry. docId is always the Physical Bill Document whose own panel this
// was clicked from, so unlike the earlier per-row-trigger design there's no target-document
// lookup and no tab switch — the panel being re-rendered is already the one showing.
async function applyPhysicalBillSource(uid, docId, sourceClientDataId, sourceType, btn) {
    if (!sourceClientDataId) return;
    const originalText = btn ? btn.textContent : null;
    if (btn) {
        btn.disabled = true;
        btn.textContent = '…';
    }

    try {
        const res = await fetch(`${API_BASE}/api/verification/physical_bill_source_preview/${sourceClientDataId}?source_type=${encodeURIComponent(sourceType)}`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(data.detail || 'Unable to load source row for Physical Bill.');
        }

        const tabState = getProcedureTabState(uid, docId);
        tabState.physicalBill = {
            bill_no: data.bill_no || '',
            bill_date: '', // always manual — never resolved by the preview or the backend
            amount: '', // updatePhysicalBillAmount() computes this once the panel renders
            vendor_name: data.vendor_name || '',
            quantity: data.quantity != null ? String(data.quantity) : '',
            rate: data.rate != null ? String(data.rate) : ''
        };
        tabState.physicalBillSourceDocumentRowId = sourceClientDataId;
        tabState.physicalBillSourceTemplate = sourceType;
        tabState.physicalBillErrors = {};

        renderProcedureModalPanel(uid, docId);
        showStatus('Physical Bill form pre-filled from source row — review and Save.', 'success');
    } catch (e) {
        console.error('Failed to populate Physical Bill from source:', e);
        showStatus(e.message || 'Unable to load source row for Physical Bill.', 'error');
        if (btn) {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    }
}

// Small picker row list — reuses .procedure-result-item/.procedure-result-main/
// .procedure-result-label/.procedure-result-meta as-is (already-existing search-dropdown row
// styling), no new CSS needed for the rows themselves.
function buildPhysicalBillCopyPickerHtml(rows) {
    return rows.map((r, idx) => `
        <div class="procedure-result-item procedure-bill-copy-picker-item" data-idx="${idx}">
            <div class="procedure-result-main">
                <span class="procedure-result-label">${escapeHtml(r.vendor || '(no vendor)')}${r.bill_no ? ' — ' + escapeHtml(r.bill_no) : ''}</span>
                <span class="procedure-result-meta">${escapeHtml(r.document_name)}${r.extract_date ? ' · ' + escapeHtml(r.extract_date) : ''}</span>
            </div>
        </div>
    `).join('');
}

// Toggles the inline picker open/closed — plain show/hide within the panel's own flow, not a
// floating popover, since it renders directly beneath the Copy from lines button.
function togglePhysicalBillCopyPicker(uid, docId, eligibleRows) {
    const picker = document.getElementById('procedureBillCopyPicker');
    if (!picker) return;
    const isOpen = picker.style.display !== 'none' && picker.innerHTML !== '';
    if (isOpen) {
        picker.style.display = 'none';
        picker.innerHTML = '';
        return;
    }
    picker.innerHTML = buildPhysicalBillCopyPickerHtml(eligibleRows);
    picker.style.display = 'block';
    picker.querySelectorAll('.procedure-bill-copy-picker-item').forEach(item => {
        item.addEventListener('click', function() {
            const row = eligibleRows[parseInt(this.dataset.idx)];
            picker.style.display = 'none';
            picker.innerHTML = '';
            if (row) applyPhysicalBillSource(uid, docId, row.client_data_id, row.source_type, document.getElementById('procedureBillCopyBtn'));
        });
    });
}

// ---------- SEARCH (debounced, stale-response-guarded) ----------
let procedureSearchDebounceTimer = null;
let procedureSearchRequestToken = 0;

function debounceProcedureSearch(uid, docId, term) {
    clearTimeout(procedureSearchDebounceTimer);
    procedureSearchDebounceTimer = setTimeout(() => performProcedureSearch(uid, docId, term), 300);
}

async function performProcedureSearch(uid, docId, term) {
    const tabState = getProcedureTabState(uid, docId);
    tabState.searchTerm = term;
    const lineState = procedureModalState[uid];
    const doc = lineState && (lineState.documents || []).find(d => d.id === docId);
    const dropdown = document.getElementById('procedureSearchDropdown');
    if (!doc || !doc.template_id) return;

    if (!term.trim()) {
        tabState.searchResults = [];
        if (dropdown) dropdown.innerHTML = '';
        return;
    }

    const thisRequest = ++procedureSearchRequestToken;
    if (dropdown) dropdown.innerHTML = '<div class="procedure-search-loading"><span class="spinner"></span> Searching...</div>';

    try {
        const data = await fetchProcedureRows(doc.template_id, term.trim(), 1, 10);
        // Stale guard: a later search (or a tab switch that changed the input's value) may have
        // started after this one — only the most recently dispatched request may write state/DOM.
        if (thisRequest !== procedureSearchRequestToken) return;
        tabState.searchResults = data.rows || [];
        tabState.docKeyColumn = data.document_key_column;
        renderProcedureSearchDropdown(uid, docId);
    } catch (e) {
        if (thisRequest !== procedureSearchRequestToken) return;
        console.error('Supporting document search failed:', e);
        if (dropdown) dropdown.innerHTML = '<div class="procedure-search-error">Search failed — try again.</div>';
    }
}

function renderProcedureSearchDropdown(uid, docId) {
    const dropdown = document.getElementById('procedureSearchDropdown');
    if (!dropdown) return;
    const tabState = getProcedureTabState(uid, docId);
    const results = tabState.searchResults || [];
    if (!results.length) {
        dropdown.innerHTML = tabState.searchTerm.trim()
            ? '<div class="procedure-search-empty">No matching rows found.</div>'
            : '';
        return;
    }
    dropdown.innerHTML = results.map(row => buildProcedureResultRowHtml(uid, row, tabState.docKeyColumn, docId)).join('');
    dropdown.querySelectorAll('.procedure-result-item:not(.locked)').forEach(item => {
        item.addEventListener('click', async function() {
            const row = results.find(r => r.id === parseInt(this.dataset.id));
            if (row) await selectProcedureDocument(uid, docId, row);
        });
    });
}

// ---------- SELECTING A ROW (Phase 1's assign_document endpoint) ----------
// Links a Data Bank row to this voucher line for the active tab's document, then reflects the
// result locally — no full modal reload needed, since GET /api/data/preview's search/browse rows
// already carry everything the selected-lines table needs (row_data/extract_date/extract_amount).
// Returns true on success, false on failure, so callers (the dropdown and the browse modal) can
// each decide what to do next (clear the dropdown vs. keep the browse modal open and refresh it).
async function selectProcedureDocument(uid, docId, row) {
    const lineState = procedureModalState[uid];
    if (!lineState) return false;
    const tabState = getProcedureTabState(uid, docId);
    try {
        const res = await fetch(`${API_BASE}/api/verification/assign_document`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ voucher_line_id: parseInt(uid), client_data_id: row.id, document_id: docId })
        });
        if (!res.ok) {
            // A real race, not hypothetical — the row's own UNIQUE(client_data_id) constraint is
            // the actual enforcement, so a concurrent assignment elsewhere can land first.
            const err = await res.json().catch(() => ({}));
            showStatus(err.detail || 'That row is no longer available — it was just assigned elsewhere.', 'error');
            if (tabState.searchTerm) performProcedureSearch(uid, docId, tabState.searchTerm);
            return false;
        }
        const assignment = await res.json();
        tabState.selections.push({
            id: assignment.id,
            client_data_id: row.id,
            document_id: docId,
            row_data: row.row_data,
            extract_date: row.extract_date,
            extract_amount: row.extract_amount
        });
        lineState.assignedDocIds.add(docId);
        // Clear the search box/dropdown for this tab now that the row is locked — it stays
        // selected in the table below, no longer meaningful as a search result.
        tabState.searchTerm = '';
        tabState.searchResults = [];
        renderProcedureModalTabs(uid);
        renderProcedureModalPanel(uid, docId);
        showStatus('✅ Evidence linked.', 'success');
        return true;
    } catch (e) {
        console.error('Failed to assign supporting document:', e);
        showStatus('Failed to link evidence — please try again.', 'error');
        return false;
    }
}

// ---------- UNASSIGNING A ROW (Unassign requirement) ----------
// Soft-deletes a link via POST /api/verification/unassign_document, then updates local state and
// re-renders — reusing the exact same buildSelectedLinesTable()/getSelectedLinesSummary() logic
// Phase 4 already built, so the running total and Matches/mismatch badge recalculate automatically
// with no separate recompute path. Observation text for the tab is deliberately left untouched.
async function unassignSupportingDocument(uid, docId, assignmentId) {
    if (!confirm('Unlink this evidence from the voucher line? The Data Bank row will become available to assign elsewhere.')) {
        return;
    }
    const lineState = procedureModalState[uid];
    if (!lineState) return;
    const tabState = getProcedureTabState(uid, docId);

    try {
        const res = await fetch(`${API_BASE}/api/verification/unassign_document`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ assignment_id: assignmentId })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showStatus(err.detail || 'Failed to unlink evidence — please try again.', 'error');
            return;
        }

        tabState.selections = (tabState.selections || []).filter(sel => sel.id !== assignmentId);
        // If that was this document's last active link, its tab dot should un-fill too.
        if (!tabState.selections.length) {
            lineState.assignedDocIds.delete(docId);
        }
        renderProcedureModalTabs(uid);
        renderProcedureModalPanel(uid, docId);
        showStatus('✅ Evidence unlinked.', 'success');
    } catch (e) {
        console.error('Failed to unassign supporting document:', e);
        showStatus('Failed to unlink evidence — please try again.', 'error');
    }
}

// ---------- SELECTED-LINES TABLE ----------
// Renders the "linked evidence" table for one tab: dynamic columns derived from the union of
// row_data keys across selections (same convention template-viewer.js's renderTemplateData() uses
// — read for reference, not modified), plus a totals/mismatch summary below it. Each row also
// gets a ✕ unlink button (Unassign requirement) wired up by renderProcedureModalPanel() below,
// calling unassignSupportingDocument() — removing a selection here and re-rendering reuses this
// same summary/table logic, so the total/badge always recompute correctly with no separate code path.
// Shared by buildSelectedLinesTable() and buildObservationSuggestion() (Phase 6) so both read
// the exact same total/mismatch computation — never recomputed independently.
function getSelectedLinesSummary(uid, docId) {
    const lineState = procedureModalState[uid];
    const tabState = getProcedureTabState(uid, docId);
    const selections = tabState.selections || [];
    const total = selections.reduce((sum, sel) => sum + (parseFloat(sel.extract_amount) || 0), 0);
    const voucherAmount = (lineState && lineState.voucherLineAmount) || 0;
    const mismatch = Math.abs(total - voucherAmount) > 0.01;
    return { total, voucherAmount, mismatch, count: selections.length };
}

// Finds a key in row_data matching fieldName case-insensitively (trimmed) - same matching
// convention the totals row already used for Quantity/Amount. Returns '' if the template's
// row_data has no such field, so a fixed column never breaks the row.
function getRowFieldCaseInsensitive(row_data, fieldName) {
    if (!row_data) return '';
    const target = fieldName.trim().toLowerCase();
    const key = Object.keys(row_data).find(k => k.trim().toLowerCase() === target);
    const value = key !== undefined ? row_data[key] : '';
    return value == null ? '' : String(value);
}

function buildSelectedLinesTable(uid, docId) {
    const tabState = getProcedureTabState(uid, docId);
    const selections = tabState.selections || [];
    if (!selections.length) {
        return '<p class="procedure-selected-empty">No evidence linked yet for this document.</p>';
    }

    // Confirmed fix: render this document's own template's actual configured columns (name +
    // data_type, in sort_order) instead of a hardcoded set standardized across every template —
    // different templates (Gate Pass, Purchase Order, Vendor Bill, ...) have different real
    // fields. Columns come from templateColumnsCache, warmed in openProcedureModal() before the
    // tabs render. Falls back to the union of row_data keys across current selections (the
    // pre-Phase-4 convention, same as template-viewer.js's renderTemplateData()) if the cache has
    // nothing for this template (e.g. a deleted/inactive template) — so the table still renders
    // sensibly rather than going blank. Sr No is a plain row index, not sourced from row_data;
    // values are still looked up case-insensitively via getRowFieldCaseInsensitive(), which
    // renders blank when a row happens to be missing that field.
    const lineState = procedureModalState[uid];
    const doc = lineState && (lineState.documents || []).find(d => d.id === docId);
    let columns = (doc && templateColumnsCache[doc.template_id]) || [];
    if (!columns.length) {
        const unionKeys = [...new Set(selections.flatMap(sel => Object.keys(sel.row_data || {})))];
        columns = unionKeys.map(name => ({ column_name: name, data_type: null }));
    }

    // Trailing "" header for the unlink icon column (Unassign requirement) - no label, matching
    // this app's existing convention for icon-only action columns (e.g. Data Bank's Actions column).
    let html = '<div class="procedure-selected-table-wrapper"><table class="procedure-selected-table"><thead><tr>';
    html += '<th>Sr No</th>';
    columns.forEach(col => { html += `<th>${escapeHtml(col.column_name)}</th>`; });
    html += '<th></th>';
    html += '</tr></thead><tbody>';
    selections.forEach((sel, idx) => {
        html += '<tr>';
        html += `<td>${idx + 1}</td>`;
        columns.forEach(col => {
            const value = getRowFieldCaseInsensitive(sel.row_data, col.column_name);
            // Rate-derivation requirement, Phase 10 (FINAL): same provenance icon as Data Bank
            // preview's renderPreviewRows() (static/databank.js), sourced per-row from
            // sel.rate_source (client_data.rate_source, added to this table's GET
            // voucher_line_documents/{id} response) - never the template-level setting.
            if (col.column_name.trim().toLowerCase() === 'rate' && value && sel.rate_source) {
                const icon = sel.rate_source === 'derived'
                    ? '<span class="rate-source-icon" title="Rate derived as Amount ÷ Quantity">🧮</span>'
                    : '<span class="rate-source-icon" title="Rate imported directly from the source file">📥</span>';
                html += `<td>${escapeHtml(value)}${icon}</td>`;
            } else {
                html += `<td>${escapeHtml(value)}</td>`;
            }
        });
        html += '<td>';
        html += `<button type="button" class="procedure-unlink-btn" data-assignment-id="${sel.id}" title="Unlink this evidence">✕</button>`;
        html += '</td>';
        html += '</tr>';
    });

    // Totals row: sums only columns whose data_type is Numeric (mirrors routers/data.py's own
    // amount_columns convention) — every other cell, including Sr No and the trailing unlink
    // column, is left blank. Recomputes automatically on every re-render of this table (add/
    // remove a line), since this function is the single place the Linked Evidence table is built.
    html += '<tr class="procedure-total-row"><td></td>';
    columns.forEach(col => {
        if (col.data_type === 'Numeric') {
            const colSum = selections.reduce((sum, sel) => sum + (parseFloat(getRowFieldCaseInsensitive(sel.row_data, col.column_name)) || 0), 0);
            html += `<td>${colSum.toFixed(2)}</td>`;
        } else {
            html += '<td></td>';
        }
    });
    html += '<td></td>'; // unlink-icon column stays blank on the totals row
    html += '</tr>';

    html += '</tbody></table></div>';

    const { total, voucherAmount, mismatch, count } = getSelectedLinesSummary(uid, docId);
    html += `
        <div class="procedure-total-summary">
            <span>Voucher line amount: <strong>${voucherAmount.toFixed(2)}</strong></span>
            <span>Evidence total (${count} line${count === 1 ? '' : 's'}): <strong>${total.toFixed(2)}</strong></span>
            ${mismatch
                ? '<span class="procedure-mismatch-badge">⚠️ Mismatch</span>'
                : '<span class="procedure-match-badge">✅ Matches</span>'}
        </div>
    `;
    return html;
}

// ---------- AUDIT PROCEDURES CHECKLIST (Phase 5 of 6) ----------
// Renders one checkbox per auth role already configured on the Document (doc.auth_roles, sourced
// from audit_rule_document_auth_roles via the same audit_guidance fetch/cache already used by
// showAuditGuidancePopover()/updateProceduresCellForLine() — no new fetch for the role list
// itself). Tick state comes from tabState.checklist (seeded in openProcedureModal() from
// GET /api/verification/procedure_checks/{uid}) — a role with no entry renders unchecked. No
// validation behind ticking a role — rules are explicitly deferred to a future phase.
function buildProcedureChecklist(uid, docId) {
    const lineState = procedureModalState[uid];
    const doc = lineState && (lineState.documents || []).find(d => d.id === docId);
    const tabState = getProcedureTabState(uid, docId);
    const roles = (doc && doc.auth_roles) || [];
    if (!roles.length) {
        return '<p class="procedure-checklist-empty">No audit procedures configured for this document.</p>';
    }

    const items = roles.map(role => {
        const state = tabState.checklist[role] || {};
        const checked = !!state.is_checked;
        return `
            <label class="procedure-checklist-item">
                <input type="checkbox" class="procedure-checklist-checkbox" data-role="${escapeHtml(role)}" ${checked ? 'checked' : ''}>
                <span>${escapeHtml(role)}</span>
                <span class="procedure-checklist-source">sourced from Audit Rules</span>
            </label>
        `;
    }).join('');

    return `<div class="procedure-checklist-list">${items}</div>`;
}

// Persists one checkbox toggle. Updates local state from the server's response on success (no
// re-render needed — the checkbox already reflects the click); on failure, reverts the in-memory
// state and re-renders so the checkbox visually snaps back to its prior value.
async function toggleProcedureCheck(uid, docId, roleName, isChecked) {
    const tabState = getProcedureTabState(uid, docId);
    const previousState = tabState.checklist[roleName] ? { ...tabState.checklist[roleName] } : null;
    if (!tabState.checklist[roleName]) tabState.checklist[roleName] = {};
    tabState.checklist[roleName].is_checked = isChecked;

    try {
        const res = await fetch(`${API_BASE}/api/verification/procedure_checks`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                voucher_line_id: parseInt(uid),
                document_id: docId,
                role_name: roleName,
                is_checked: isChecked
            })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to save procedure check.');
        }
        const updated = await res.json();
        tabState.checklist[roleName] = {
            is_checked: updated.is_checked,
            checked_by: updated.checked_by,
            checked_at: updated.checked_at
        };
        // Completion-dot logic (Phase 3): a no-template tab's dot depends on checklist state, so
        // a successful tick/untick must refresh the tab bar too, not just the panel (which the
        // checkbox itself already visually reflects). Has-template tabs' dots are unaffected by
        // this, since their isComplete branch never reads the checklist.
        renderProcedureModalTabs(uid);
    } catch (e) {
        console.error('Failed to save audit procedure check:', e);
        if (previousState) {
            tabState.checklist[roleName] = previousState;
        } else {
            delete tabState.checklist[roleName];
        }
        showStatus('Failed to save procedure tick — please try again.', 'error');
        renderProcedureModalPanel(uid, docId);
        renderProcedureModalTabs(uid);
    }
}

// Validation Rules Phase 5: signature-exception tick per linked evidence row — same optimistic-
// update-then-revert-on-failure shape as toggleProcedureCheck() above, immediate save on toggle.
async function toggleSignatureException(uid, docId, assignmentId, isChecked) {
    const tabState = getProcedureTabState(uid, docId);
    const sel = (tabState.selections || []).find(s => s.id === assignmentId);
    const previousValue = sel ? sel.signature_exception : null;
    if (sel) sel.signature_exception = isChecked;

    try {
        const res = await fetch(`${API_BASE}/api/verification/set_signature_exception`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ assignment_id: assignmentId, signature_exception: isChecked })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to save signature exception.');
        }
    } catch (e) {
        console.error('Failed to save signature exception:', e);
        if (sel) sel.signature_exception = previousValue;
        showStatus('Failed to save signature exception — please try again.', 'error');
        renderProcedureModalPanel(uid, docId);
    }
}

// ---------- OBSERVATION FLOW (Phase 6 of 6, FINAL) ----------
// Builds an auto-suggested observation for a tab from (a) the Phase 4 amount-mismatch state
// (getSelectedLinesSummary(), shared with buildSelectedLinesTable()'s own badge) and (b) any
// Phase 5 unticked procedures for this document. Returns '' (no forced/neutral text) when
// neither condition applies — only ever offered once per tab (see suggestionApplied in
// getProcedureTabState()'s doc comment), never re-run over text the auditor edited/cleared.
// A no-template document has no linked-evidence table/mismatch badge to reference (Two-segment
// panel phase) — its suggestion is limited to unticked procedures only, or blank if none.
function buildObservationSuggestion(uid, docId) {
    const lineState = procedureModalState[uid];
    const doc = lineState && (lineState.documents || []).find(d => d.id === docId);
    const tabState = getProcedureTabState(uid, docId);
    const hasTemplate = !!(doc && doc.template_id);
    const lines = [];

    if (hasTemplate) {
        const { total, voucherAmount, mismatch } = getSelectedLinesSummary(uid, docId);
        if (mismatch) {
            const diff = total - voucherAmount;
            lines.push(`Amount mismatch: evidence total ${total.toFixed(2)} vs voucher line amount ${voucherAmount.toFixed(2)} (diff ${diff.toFixed(2)}).`);
        }
    }

    const roles = (doc && doc.auth_roles) || [];
    const unticked = roles.filter(role => !(tabState.checklist[role] && tabState.checklist[role].is_checked));
    if (unticked.length) {
        lines.push(`Unticked procedures: ${unticked.join(', ')}.`);
    }

    return lines.join('\n');
}

// Applies the auto-suggestion to a tab exactly once — only when the box is genuinely empty and
// no suggestion (or saved entry) has touched it yet, so it never overrides a typed/cleared value.
function applyObservationSuggestionIfNeeded(uid, docId) {
    const tabState = getProcedureTabState(uid, docId);
    if (tabState.suggestionApplied || tabState.observation) return;
    const suggestion = buildObservationSuggestion(uid, docId);
    if (suggestion) {
        tabState.observation = suggestion;
        // Local-only text, never persisted yet — mark dirty so the Copy button (and the
        // eventual Prev/Next/Complete/Exit autosave) correctly treat it as not-yet-pushed.
        tabState.observationDirty = true;
    }
    tabState.suggestionApplied = true;
}

// Renders one tab's observation section: a textarea (local-only edits), a ↺ Restore button that
// reverts the textarea to tabState.lastSyncedText (the exact text last pushed to the voucher
// table — a single snapshot, no auto/manual distinction) without re-syncing, a System
// Observations button that recomputes buildObservationSuggestion() live and appends it (never a
// stored snapshot, so it's always recoverable regardless of edits/clears), and an explicit Copy
// button that pushes this one document's observation into the voucher line's Observations column
// on demand (POSTs immediately via saveLineObservation(), rather than waiting for the
// Prev/Next/Complete/Exit autosave chokepoint). Copy is disabled for a blank observation, shows
// "Copy" (grey) while tabState.observationDirty is true, and flips to "Copied" (green) once it
// isn't — the same flag an edit already sets, so no separate synced flag is needed client-side.
// Restore is disabled whenever lastSyncedText is null/empty — nothing has ever been copied yet.
// System Observations is disabled whenever there's currently no mismatch/unticked-procedure text
// to offer — independent of both lastSyncedText and any saved/copied state.
function buildObservationSection(uid, docId) {
    const tabState = getProcedureTabState(uid, docId);
    const hasText = !!(tabState.observation && tabState.observation.trim());
    const isCopied = hasText && !tabState.observationDirty;
    const hasRestoreTarget = !!(tabState.lastSyncedText && tabState.lastSyncedText.trim());
    const systemSuggestion = buildObservationSuggestion(uid, docId);
    // Disabled only when there's no suggestion to offer (same rule as System Observations) —
    // suggestionApplied/hasText must never grey this out, since Suggest is a repeatable
    // fill/replace action, not the old one-shot auto-fill it replaced.
    const canSuggest = !!systemSuggestion;
    return `
        <textarea id="procedureObservationInput" class="procedure-observation-textarea"
            placeholder="Add an observation for this document...">${escapeHtml(tabState.observation || '')}</textarea>
        <div class="procedure-observation-copy-row">
            <button type="button" id="procedureObservationSuggestBtn"
                class="procedure-observation-suggest-btn"
                title="Fill this observation with the suggested text (mismatch / unticked procedures)"
                ${canSuggest ? '' : 'disabled'}>💡 Suggest</button>
            <button type="button" id="procedureObservationRestoreBtn"
                class="procedure-observation-restore-btn"
                title="Revert to last copied text"
                ${hasRestoreTarget ? '' : 'disabled'}>↺</button>
            <button type="button" id="procedureObservationSystemBtn"
                class="procedure-observation-system-btn"
                title="Append current system-generated observation (mismatch / unticked procedures)"
                ${systemSuggestion ? '' : 'disabled'}>🔄 System Observations</button>
            <button type="button" id="procedureObservationCopyBtn"
                class="procedure-observation-copy-btn${isCopied ? ' copied' : ''}"
                ${hasText ? '' : 'disabled'}>${isCopied ? '✓ Copied' : '📋 Copy'}</button>
        </div>
    `;
}

// Persists one tab's observation text as a tagged entry (POST save_line_observation). Called only
// by flushProcedureObservations() below, never directly from a UI event — there is no Save button
// anymore. Always marks it unsynced server-side — it will be rolled into the voucher line's own
// Observation field the next time that line is saved (saveLineItems(), via
// sync_line_observations_to_voucher() in routers/verification.py), and also immediately here since
// save_line_observation() itself calls that same sync function.
async function saveLineObservation(uid, docId) {
    const tabState = getProcedureTabState(uid, docId);
    try {
        const res = await fetch(`${API_BASE}/api/verification/save_line_observation`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                voucher_line_id: parseInt(uid),
                document_id: docId,
                observation_text: tabState.observation || ''
            })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to save observation.');
        }
        const data = await res.json();
        tabState.observationDirty = false;
        // A save is a deliberate, final decision on this tab's text — it should never be
        // silently replaced by the auto-suggestion again, even if the box is later cleared.
        tabState.suggestionApplied = true;
        // The just-confirmed text is now the baseline the ↺ restore button reverts to — equal to
        // what the backend's sync just wrote into last_synced_text for this entry, so no extra
        // fetch is needed to learn it.
        tabState.lastSyncedText = data.observation_text;

        // The backend rolls this up into the voucher line's own Observation field immediately
        // (sync_line_observations_to_voucher(), called from save_line_observation() itself) —
        // reflect that here too, in the cached line data (currentVoucherLines, so a later full
        // re-render of this voucher stays correct) and the live Observations cell's flag+count
        // (a fresh recount, not a plain-text write — this cell shows count/tooltip only, per the
        // v7 redesign).
        const cachedLine = currentVoucherLines.find(l => l.detail_id === parseInt(uid));
        if (cachedLine) cachedLine.observations = data.voucher_observations;
        const lineType = voucherLineState[uid] && voucherLineState[uid].transactionType;
        if (lineType) updateObservationsCellForLine(uid, lineType);
    } catch (e) {
        console.error('Failed to save line observation:', e);
        showStatus('Failed to save observation — please try again.', 'error');
    }
}

// Account Head Recommendation follow-up: persists a dirty no-match finding — called from the same
// save chokepoint as flushProcedureObservations() (saveLineItems()). No-op
// unless the card is currently in no-match mode with unsaved text. Reuses the existing
// save_line_observation endpoint under the reserved ACCOUNT_HEAD_OBSERVATION_DOC_ID sentinel — the
// backend wraps the raw text into the "[Account Head Recommendation]: New account head
// recommended: ... — no match in Trial Balance" finding at rollup time
// (sync_line_observations_to_voucher()), so only the raw note is sent here.
async function flushAccountHeadNoMatchNote(uid) {
    const state = getAccountHeadState(uid);
    if (state.mode !== 'nomatch' || !state.noMatchDirty) return;

    try {
        const res = await fetch(`${API_BASE}/api/verification/save_line_observation`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                voucher_line_id: parseInt(uid),
                document_id: ACCOUNT_HEAD_OBSERVATION_DOC_ID,
                observation_text: state.noMatchText || ''
            })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to save account head note.');
        }
        const data = await res.json();
        state.noMatchDirty = false;

        // Same "keep the main table's Observations cell caught up immediately" pattern
        // saveLineObservation() already uses for real Document tabs.
        const cachedLine = currentVoucherLines.find(l => l.detail_id === parseInt(uid));
        if (cachedLine) cachedLine.observations = data.voucher_observations;
        const lineType = voucherLineState[uid] && voucherLineState[uid].transactionType;
        if (lineType) updateObservationsCellForLine(uid, lineType);
    } catch (e) {
        console.error('Failed to save account head no-match note:', e);
        showStatus('Failed to save account head finding — please try again.', 'error');
    }
}

// Persists every dirty (unsaved) observation tab for one voucher line — called from the
// Prev/Next/Complete/Exit save chokepoint (saveLineItems()) and from closeProcedureModal(), so an
// observation typed without an explicit Save action is never lost. No-op if the modal was never
// opened for this uid or nothing changed since the last save.
async function flushProcedureObservations(uid) {
    const lineState = procedureModalState[uid];
    if (!lineState || !lineState.tabs) return;
    for (const docId of Object.keys(lineState.tabs)) {
        const tabState = lineState.tabs[docId];
        if (tabState.observationDirty) {
            await saveLineObservation(uid, parseInt(docId));
        }
    }
    updateProcedureModalSavedNote(uid);
}

// ---------- BROWSE MODAL (nested on top of #procedureModal) ----------
// Opened by a tab's 👁️ icon for when the analyst doesn't have a search term — a paginated,
// read-only browse of every row for that document's template, same select/grey-out row treatment
// as the search dropdown (buildProcedureResultRowHtml()). Confirmed with the user as the closest
// achievable equivalent to "the same Data Bank preview modal" since static/databank.js/.html are
// out of scope for this phase.
const procedureBrowseModal = document.getElementById('procedureBrowseModal');
const procedureBrowseModalTitle = document.getElementById('procedureBrowseModalTitle');
const procedureBrowseModalClose = document.getElementById('procedureBrowseModalClose');
const procedureBrowseTableContainer = document.getElementById('procedureBrowseTableContainer');
const procedureBrowsePrevBtn = document.getElementById('procedureBrowsePrevBtn');
const procedureBrowseNextBtn = document.getElementById('procedureBrowseNextBtn');
const procedureBrowsePageInfo = document.getElementById('procedureBrowsePageInfo');
// Phase 3 of 3, FINAL: Status filter + count badge controls.
const procedureBrowseStatusFilterSelect = document.getElementById('procedureBrowseStatusFilter');
const procedureBrowseStatusCountEl = document.getElementById('procedureBrowseStatusCount');

const PROCEDURE_BROWSE_PAGE_SIZE = 20;
// statusFilter is 'all'/'assigned'/'unassigned' — purely client-side, applied to whichever page of
// rows is already loaded (no new fetch, no backend change; see the Phase 3 note on
// renderProcedureBrowseTable() below).
let procedureBrowseState = { uid: null, docId: null, page: 1, totalRows: 0, rows: [], statusFilter: 'all' };

async function openProcedureBrowseModal(uid, docId) {
    if (!procedureBrowseModal) return;
    const lineState = procedureModalState[uid];
    const doc = lineState && (lineState.documents || []).find(d => d.id === docId);
    if (!doc || !doc.template_id) return;

    procedureBrowseState = { uid, docId, page: 1, totalRows: 0, rows: [], statusFilter: 'all' };
    // The <select> element itself retains its own value across modal reopens independent of this
    // state object, so it needs an explicit reset too — "default to All on modal/tab open".
    if (procedureBrowseStatusFilterSelect) procedureBrowseStatusFilterSelect.value = 'all';
    if (procedureBrowseModalTitle) procedureBrowseModalTitle.textContent = `👁️ Browse — ${doc.name}`;
    procedureBrowseModal.style.display = 'flex';
    await fetchProcedureBrowsePage();
}

function closeProcedureBrowseModal() {
    if (procedureBrowseModal) procedureBrowseModal.style.display = 'none';
}

async function fetchProcedureBrowsePage() {
    const { uid, docId, page } = procedureBrowseState;
    const lineState = procedureModalState[uid];
    const doc = lineState && (lineState.documents || []).find(d => d.id === docId);
    if (!doc) return;
    if (procedureBrowseTableContainer) {
        procedureBrowseTableContainer.innerHTML = '<div style="text-align:center; padding:20px;"><span class="spinner"></span> Loading...</div>';
    }
    try {
        const data = await fetchProcedureRows(doc.template_id, '', page, PROCEDURE_BROWSE_PAGE_SIZE);
        procedureBrowseState.rows = data.rows || [];
        procedureBrowseState.totalRows = data.total_rows || 0;
        renderProcedureBrowseTable();
    } catch (e) {
        console.error('Failed to browse Data Bank rows:', e);
        if (procedureBrowseTableContainer) {
            procedureBrowseTableContainer.innerHTML = '<div style="color:#dc2626; text-align:center; padding:20px;">Failed to load rows.</div>';
        }
    }
}

// Blank-safe cell formatter for the browse table (Phase 1 of 3: full-table preview). Deliberately
// a small local helper, not a port of databank.js's formatCellValue() — this table has no
// number/date-specific formatting needs, only the blank -> em-dash convention shared app-wide.
function formatProcedureCellValue(value) {
    if (value === undefined || value === null || value === '') return '—';
    return String(value);
}

// Builds the full-column preview table for the Browse modal (Phase 1 of 3: Full-table preview
// with Status/Voucher No. columns). Column headers are derived dynamically from the first row's
// row_data keys — same convention GET /api/data/preview already uses in Data Bank's own preview
// (databank.js renderPreviewRows()) — with two fixed trailing columns appended: Status
// (Assigned/Unassigned) and Voucher No. (populated only when Assigned). Deliberately flat, one
// row per Data Bank row — no document grouping/subtotal rows this phase (confirmed with the
// user: most Document templates here are single-line-per-document; grouping is out of scope).
//
// Assigned rows reuse this feature's existing "locked" convention (see buildProcedureResultRowHtml()
// above): rendered fully (real data, not hidden) but visually greyed via .procedure-browse-row-assigned
// and non-interactive (no click listener bound, no role/tabindex) — reassignment stays deferred.
//
// Bug fix: the Voucher No. column previously showed only assigned_to_voucher_id, which — for a row
// assigned to THIS SAME voucher line under a DIFFERENT Document tab — looked identical to a genuine
// same-tab assignment (same voucher number shown either way), even though such a row will never
// appear in this tab's own Linked Evidence table (which additionally filters by document_id). The
// Voucher No. cell now also names the Document it's actually linked under whenever that differs
// from the tab currently open, so this "assigned elsewhere in the same voucher" case reads clearly
// instead of looking like a data inconsistency between Browse and Linked Evidence.
//
// Bug fix (Phase 3 of 4): "same voucher line" here is now determined by isSameUnderlyingLine()
// (stable client_data_id comparison) instead of raw assigned_to_voucher_line === uid — see that
// function's own comment for why a session-specific id comparison silently missed assignments
// made in an earlier verification session for the exact same voucher line.
function buildProcedureBrowseTableHtml(uid, rows, activeDocId) {
    // Column-order fix: read column order from the active document's own template
    // (already-warmed templateColumnsCache, same source buildSelectedLinesTable() uses)
    // instead of the first row's own row_data key order, which isn't reliable for a
    // derived field like Rate (see reorder_row_data_by_template()'s docstring, routers/
    // import_export.py). Falls back to the union-of-keys behavior if the cache has
    // nothing for this template.
    const lineState = procedureModalState[uid];
    const activeDoc = lineState && (lineState.documents || []).find(d => d.id === activeDocId);
    const templateColumns = (activeDoc && templateColumnsCache[activeDoc.template_id]) || [];
    const columns = templateColumns.length
        ? templateColumns.map(c => c.column_name)
        : Object.keys(rows[0].row_data || {});

    let html = '<table class="procedure-browse-table"><thead><tr>';
    columns.forEach(col => { html += `<th>${escapeHtml(col)}</th>`; });
    html += '<th>Status</th><th>Voucher No.</th>';
    html += '</tr></thead><tbody>';

    rows.forEach(row => {
        const isAssigned = !!row.is_assigned;
        const rowClass = isAssigned ? ' class="procedure-browse-row-assigned"' : '';
        const interactiveAttrs = isAssigned ? '' : ' role="button" tabindex="0"';
        html += `<tr data-id="${row.id}"${rowClass}${interactiveAttrs}>`;
        columns.forEach(col => {
            html += `<td>${escapeHtml(formatProcedureCellValue(row.row_data ? row.row_data[col] : ''))}</td>`;
        });
        const statusBadgeClass = isAssigned ? 'assigned' : 'unassigned';
        const statusLabel = isAssigned ? 'Assigned' : 'Unassigned';
        html += `<td><span class="procedure-browse-status-badge ${statusBadgeClass}">${statusLabel}</span></td>`;
        let voucherCell = '';
        if (isAssigned) {
            voucherCell = escapeHtml(formatAssignedVoucherLabel(row.assigned_to_voucher_id, row.assigned_to_line_srno));
            const isOtherDocSameLine = isSameUnderlyingLine(uid, row)
                && activeDocId != null && String(row.assigned_to_document_id) !== String(activeDocId);
            if (isOtherDocSameLine) {
                voucherCell += ` <span class="procedure-browse-other-doc-note">(under "${escapeHtml(row.assigned_to_document_name || 'another document')}")</span>`;
            }
        }
        html += `<td>${voucherCell}</td>`;
        html += '</tr>';
    });

    html += '</tbody></table>';
    return html;
}

// Recomputes the Status count badge from the current page's full (unfiltered) row set — always
// shows both counts together as a plain breakdown of what's loaded, independent of which filter is
// currently selected (Phase 3 of 3, FINAL). Page-scoped, per the confirmed brief: this modal loads
// PROCEDURE_BROWSE_PAGE_SIZE rows at a time, so this is never a whole-template total.
function updateProcedureBrowseStatusCount(rows) {
    if (!procedureBrowseStatusCountEl) return;
    if (!rows.length) {
        procedureBrowseStatusCountEl.textContent = '';
        return;
    }
    const assignedCount = rows.filter(r => r.is_assigned).length;
    const unassignedCount = rows.length - assignedCount;
    procedureBrowseStatusCountEl.textContent = `This page: ${unassignedCount} unassigned · ${assignedCount} assigned`;
}

function renderProcedureBrowseTable() {
    if (!procedureBrowseTableContainer) return;
    const { uid, docId, rows, page, totalRows, statusFilter } = procedureBrowseState;
    updateProcedureBrowseStatusCount(rows);

    // Status filter (Phase 3 of 3, FINAL) — purely client-side over the rows already loaded for
    // this page; changing the filter never re-fetches (see the change listener below).
    const filteredRows = statusFilter === 'assigned' ? rows.filter(r => r.is_assigned)
        : statusFilter === 'unassigned' ? rows.filter(r => !r.is_assigned)
        : rows;

    if (!rows.length) {
        procedureBrowseTableContainer.innerHTML = '<div style="color:#94a3b8; text-align:center; padding:20px;">No rows found for this template.</div>';
    } else if (!filteredRows.length) {
        // Distinct from the "no rows at all" message above — the page has data, just none matching
        // the current filter. buildProcedureBrowseTableHtml() indexes rows[0], so it must never be
        // called with an empty array.
        procedureBrowseTableContainer.innerHTML = '<div style="color:#94a3b8; text-align:center; padding:20px;">No rows match this filter.</div>';
    } else {
        procedureBrowseTableContainer.innerHTML = buildProcedureBrowseTableHtml(uid, filteredRows, docId);
        procedureBrowseTableContainer.querySelectorAll('.procedure-browse-table tbody tr:not(.procedure-browse-row-assigned)').forEach(item => {
            item.addEventListener('click', async function() {
                // Looked up against the full (unfiltered) rows — row ids are stable regardless of
                // whether the row is currently hidden by the status filter.
                const row = rows.find(r => r.id === parseInt(this.dataset.id));
                if (!row) return;
                const success = await selectProcedureDocument(uid, docId, row);
                if (success) {
                    closeProcedureBrowseModal();
                } else {
                    // The row was locked by someone else between fetch and click — refresh this
                    // page so it shows correctly instead of staying stale/clickable.
                    fetchProcedureBrowsePage();
                }
            });
        });
    }
    const totalPages = Math.max(Math.ceil(totalRows / PROCEDURE_BROWSE_PAGE_SIZE), 1);
    if (procedureBrowsePageInfo) procedureBrowsePageInfo.textContent = `Page ${page} of ${totalPages} (${totalRows} row${totalRows === 1 ? '' : 's'})`;
    if (procedureBrowsePrevBtn) procedureBrowsePrevBtn.disabled = page <= 1;
    if (procedureBrowseNextBtn) procedureBrowseNextBtn.disabled = page >= totalPages;
}

if (procedureBrowsePrevBtn) {
    procedureBrowsePrevBtn.addEventListener('click', function() {
        if (procedureBrowseState.page > 1) {
            procedureBrowseState.page--;
            fetchProcedureBrowsePage();
        }
    });
}
if (procedureBrowseNextBtn) {
    procedureBrowseNextBtn.addEventListener('click', function() {
        const totalPages = Math.max(Math.ceil(procedureBrowseState.totalRows / PROCEDURE_BROWSE_PAGE_SIZE), 1);
        if (procedureBrowseState.page < totalPages) {
            procedureBrowseState.page++;
            fetchProcedureBrowsePage();
        }
    });
}
if (procedureBrowseStatusFilterSelect) {
    // Phase 3 of 3, FINAL: re-render only — the filter is client-side over the already-loaded
    // page, so no fetch is needed here (unlike Prev/Next above).
    procedureBrowseStatusFilterSelect.addEventListener('change', function() {
        procedureBrowseState.statusFilter = this.value;
        renderProcedureBrowseTable();
    });
}
if (procedureBrowseModalClose) {
    procedureBrowseModalClose.addEventListener('click', closeProcedureBrowseModal);
}
if (procedureBrowseModal) {
    procedureBrowseModal.addEventListener('click', function(e) {
        if (e.target === this) closeProcedureBrowseModal();
    });
}

document.getElementById('procedureModalValidateBtn')?.addEventListener('click', function() {
    toggleValidatePanel();
});
if (procedureModalClose) {
    procedureModalClose.addEventListener('click', closeProcedureModal);
}
if (procedureModal) {
    procedureModal.addEventListener('click', function(e) {
        if (e.target === this) closeProcedureModal();
    });
}
document.getElementById('procedureModalBackBtn')?.addEventListener('click', function() {
    navigateProcedureModalLine(-1);
});
document.getElementById('procedureModalVerifyOnlyBtn')?.addEventListener('click', function() {
    verifyAndStay();
});
document.getElementById('procedureModalVerifyBtn')?.addEventListener('click', function() {
    verifyAndAdvanceVoucher();
});

// ---------- INIT ----------
window.onload = function() {
    if (dashboardContent) dashboardContent.style.display = 'block';
    if (sessionContent) sessionContent.style.display = 'none';
    if (cashbookContent) cashbookContent.style.display = 'none';
    if (typeof setNavModule === 'function') {
        setNavModule('Verification');
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
    // Cashbook/Bankbook" from there (openCashbook()), same as today. No setBackOverride() call is
    // needed here: openCashbook()/startSessionFromCashbook()/exitSession() already register the
    // correct back-arrow override once the user actually reaches a real view.
    const arrivalParams = new URLSearchParams(window.location.search);

    // Hard-refresh view persistence: ?state=session|cashbook&id=... (written by
    // setVerificationUrl()) means the user was previously in that view and hard-refreshed - always
    // re-fetch fresh from the server for that id rather than falling through to the dashboard
    // default below. Checked ahead of the existing ?type=/?view= entry-link handling, which stays
    // completely unchanged for ordinary (non-restored) page loads.
    const stateParam = arrivalParams.get('state');
    const idParam = arrivalParams.get('id');
    if (stateParam === 'session' && idParam) {
        restoreSessionView(parseInt(idParam, 10));
        return;
    }
    if (stateParam === 'cashbook' && idParam) {
        restoreCashbookView(idParam);
        return;
    }

    const arrivalView = arrivalParams.get('view');
    const isDeepLinkView = arrivalView === 'cashbook' || arrivalView === 'bankbook';
    if (arrivalParams.has('type') || isDeepLinkView) {
        openSessionPopup();
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
    try {
        const res = await fetch('/api/templates');
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
    try {
        const res = await fetch('/api/verification/stats');
        if (res.ok) {
            const stats = await res.json();
            document.getElementById('pendingCount').textContent = stats.pending || 0;
            document.getElementById('verifiedToday').textContent = stats.approved || 0;
            document.getElementById('totalVerified').textContent = stats.total || 0;
        } else {
            document.getElementById('pendingCount').textContent = '0';
            document.getElementById('verifiedToday').textContent = '0';
            document.getElementById('totalVerified').textContent = '0';
        }
    } catch (error) {
        document.getElementById('pendingCount').textContent = '0';
        document.getElementById('verifiedToday').textContent = '0';
        document.getElementById('totalVerified').textContent = '0';
    }
}