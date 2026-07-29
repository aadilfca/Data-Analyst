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

// DOM refs
const dashboardContent = document.getElementById('dashboardContent');
const sessionContent = document.getElementById('verificationSessionContent');
const cashbookContent = document.getElementById('cashbookContent');
const popupOverlay = document.getElementById('sessionPopupOverlay');
const popupCashbookSelect = document.getElementById('popupCashbookSelect');
const popupStartBtn = document.getElementById('popupStartBtn');
const popupCancelBtn = document.getElementById('popupCancelBtn');
const popupError = document.getElementById('popupError');
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

// ---------- POPUP: LOAD CASHBOOKS ----------
async function loadCashbooks() {
    const clientId = getClientId();
    if (!clientId) {
        popupCashbookSelect.innerHTML = '<option value="">-- Select client first --</option>';
        popupCashbookSelect.disabled = true;
        popupStartBtn.disabled = true;
        return;
    }
    const now = new Date();
    const firstDay = new Date(now.getFullYear(), now.getMonth(), 1);
    const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    const start = formatDateInput(firstDay);
    const end = formatDateInput(lastDay);
    // Bank Verification (Phase 8): book-type-specific copy, since this same function serves both
    // Cash and Bank discovery now (only the category_group filter/wording differs).
    const bookWord = currentType === 'bank' ? 'bankbook' : 'cashbook';
    const bookWordCap = currentType === 'bank' ? 'Bankbook' : 'Cashbook';
    const voucherHint = currentType === 'bank' ? 'BPV/BRV' : 'CPV/CRV';
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
    const now = new Date();
    const firstDay = new Date(now.getFullYear(), now.getMonth(), 1);
    const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    cashbookStartDate.value = formatDateInput(firstDay);
    cashbookEndDate.value = formatDateInput(lastDay);
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
    const start = cashbookStartDate.value;
    const end = cashbookEndDate.value;
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
    const start = cashbookStartDate.value;
    const end = cashbookEndDate.value;
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

// Voucher-level approval cache, keyed by transaction_ref (shared across every line of the same
// voucher, not per-uid) — { client_id, transaction_ref, prepared_by, checked_by, approved_by,
// dirty, loaded }. Fetched once per voucher in openProcedureModal(), reused across lines of that
// same voucher within one modal session.
let voucherApprovalCache = {};
const VOUCHER_LINE_DESCRIPTION_ALIASES = ['Description', 'Voucher Detail', 'Narration', 'Remarks', 'Particulars'];
const VOUCHER_LINE_DEBIT_ALIASES = ['Debit Amount', 'Debit', 'Dr Amount', 'Dr'];
const VOUCHER_LINE_CREDIT_ALIASES = ['Credit Amount', 'Credit', 'Cr Amount', 'Cr'];

// Account Head Recommendation (Validation Rules Phase 7, extended by the reposition/search-select/
// no-match follow-up) — keyed by uid (per voucher line, not shared across lines like
// voucherApprovalCache, since suggestions are description-driven).
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
        const columns = (data.columns || []).map(c => ({ column_name: c.column_name, data_type: c.data_type }));
        templateColumnsCache[templateId] = columns;
        return columns;
    } catch (e) {
        console.error('Failed to load template columns:', e);
        templateColumnsCache[templateId] = [];
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

    // Transaction level approval (Phase 4 of 4, FINAL): only fetch/await the voucher-level
    // approval record when at least one of this Category's Documents is actually flagged —
    // avoids an extra network call for the common unflagged case. Warms the same
    // voucherApprovalCache openProcedureModal() reads, via the shared ensureVoucherApprovalLoaded().
    const needsVoucherApproval = documents.some(doc => doc.transaction_level_approval);
    const [assignedRecords, procedureChecks, va] = await Promise.all([
        fetchAssignedDocumentRecords(uid),
        fetchProcedureChecksForLine(uid),
        needsVoucherApproval ? ensureVoucherApprovalLoaded(getClientId(), currentVoucherId) : Promise.resolve(null)
    ]);
    // Re-check the stale guard after these awaits, same reason as above.
    if (!voucherLineState[uid] || voucherLineState[uid].transactionType !== type) return;
    const assignedDocIds = new Set(assignedRecords.map(r => r.document_id));
    // Mirrors isProcedureDocumentComplete()'s base-then-gate shape so this standalone badge never
    // disagrees with the modal tab's own completion dot for a flagged Document.
    const isDocDone = doc => {
        let base;
        if (doc.template_id) {
            base = assignedDocIds.has(doc.id);
        } else {
            const roles = doc.auth_roles || [];
            base = roles.length > 0 && roles.every(role => procedureChecks.some(c => c.document_id === doc.id && c.role_name === role && c.is_checked));
        }
        if (!base) return false;
        if (doc.transaction_level_approval) return isVoucherApprovalComplete(va);
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
                    <span class="voucher-line-status-cell" data-detail-id="${uid}">
                        <span class="voucher-line-status status-${validationStatus}" data-validation-status="${validationStatus}">${getVoucherLineStatusIcon(line.validation_status)} ${validationStatus}</span>
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
    // Validation Rules Phase 5: same "flush on save" treatment for a dirty voucher-approval edit.
    await flushVoucherApproval(detailId);
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
            statusCell.innerHTML = `${getVoucherLineStatusIcon(data.validation_status)} ${status}`;
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
    if (!procedureModalState[uid]) procedureModalState[uid] = { activeDocId: null, documents: [], assignedDocIds: new Set(), voucherLineAmount: 0, tabs: {} };
    if (!procedureModalState[uid].tabs[docId]) {
        procedureModalState[uid].tabs[docId] = { loaded: false, searchTerm: '', searchResults: [], docKeyColumn: null, selections: [], observation: '', observationDirty: false, suggestionApplied: false, lastSyncedText: null, checklist: {} };
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

    // Validation Rules Phase 5: the voucher-level Prepared/Checked/Approved By record — read once
    // per voucher (client_id+transaction_ref), reused across every line of that same voucher
    // rather than re-fetched per line, matching the backend's own "applied to every line of that
    // voucher" design (Phase 4). currentVoucherId is this open voucher's transaction_ref, kept in
    // sync by every navigation function in this module.
    await ensureVoucherApprovalLoaded(getClientId(), currentVoucherId);

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

    if (!procedureModalState[uid]) procedureModalState[uid] = { activeDocId: null, documents: [], assignedDocIds: new Set(), voucherLineAmount: 0, tabs: {} };
    procedureModalState[uid].documents = documents;
    procedureModalState[uid].assignedDocIds = assignedDocIds;
    procedureModalState[uid].voucherLineAmount = voucherLineAmount;

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

    // Footer nav: Back disabled on the voucher's first line, Next on its last.
    const backBtn = document.getElementById('procedureModalBackBtn');
    const nextBtn = document.getElementById('procedureModalNextBtn');
    if (backBtn) backBtn.disabled = lineIdx <= 0;
    if (nextBtn) nextBtn.disabled = lineIdx === -1 || lineIdx >= totalLines - 1;

    procedureModal.style.display = 'flex';

    renderProcedureModalTabs(uid);
    activateProcedureTab(uid, activeDocId);
    updateProcedureModalSavedNote(uid);
}

// Reflects whether any tab for this line has an unsaved (typed-but-not-yet-flushed) observation,
// or the voucher-level approval fields have an unsaved edit (Validation Rules Phase 5) — checklist
// ticks and the signature-exception tick already save immediately, so only text fields can ever
// be "unsaved" here. Called on modal open, on every keystroke in the observation textarea
// (wireProcedureChecklistAndObservation()) or the voucher-approval fields
// (renderVoucherApprovalPanel()), and after flushProcedureObservations()/flushVoucherApproval()
// resolve.
function updateProcedureModalSavedNote(uid) {
    const noteEl = document.getElementById('procedureModalSavedNote');
    if (!noteEl) return;
    const lineState = procedureModalState[uid];
    const tabs = (lineState && lineState.tabs) || {};
    const va = voucherApprovalCache[currentVoucherId];
    const dirty = Object.values(tabs).some(t => t.observationDirty) || !!(va && va.dirty);
    noteEl.textContent = dirty ? 'Unsaved changes' : 'All changes saved';
    noteEl.classList.toggle('dirty', dirty);
}

// Moves the open modal to the adjacent voucher line (delta = -1 for Back, +1 for Next) without
// closing it — distinct from activateProcedureTab(), which switches between one line's own
// Documents. Flushes any unsaved observation text on the line being left, same as
// closeProcedureModal() does, then re-runs openProcedureModal()'s full fetch/render pipeline for
// the target line so its own tab state is always freshly synced from the server. A target line
// with no Category assigned has nothing to show (openProcedureModal() no-ops on a falsy type, per
// this module's existing documented rule) — caught here first so the user gets a clear message
// instead of the modal silently freezing on the old line's content.
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
    // this is the natural point for it to catch up. saveLineItems() already calls
    // flushProcedureObservations()/flushVoucherApproval() internally, so this is a strict
    // superset of the old call, not a behavior change to the observation flush itself.
    await saveLineItems(uid);
    await openProcedureModal(newUid);
}

async function closeProcedureModal() {
    // Flush any unsaved observation text/voucher-approval edits before hiding — closing via ✕ or
    // outside-click persists the same way Prev/Next/Complete/Exit does, per the confirmed
    // requirement (there is no longer a Save button to rely on instead). saveLineItems() also
    // recomputes cleared_status (Validation Rules Phase 5) so the main table's badge is caught up
    // by the time the modal is gone.
    if (currentProcedureModalUid) await saveLineItems(currentProcedureModalUid);
    if (procedureModal) procedureModal.style.display = 'none';
    // Defensive — the browse modal is nested on top of this one, so it should never remain open
    // once the parent closes.
    closeProcedureBrowseModal();
    // procedureModalState[uid] is deliberately left in place (not cleared here) — reopening the
    // same line's modal within the same voucher should show tab state exactly as it was left;
    // it's only reset on voucher navigation, in seedVoucherLineState().
    currentProcedureModalUid = null;
}

// Shared "are all three voucher-level approval fields filled" check — used by the synthetic
// Voucher Line tab's own completeness below, and (Transaction level approval, Phase 4 of 4) by
// any real Document flagged transaction_level_approval, whose tab additionally requires this.
function isVoucherApprovalComplete(va) {
    return !!(va && (va.prepared_by || '').trim() && (va.checked_by || '').trim() && (va.approved_by || '').trim());
}

// Fetches and caches one voucher's Prepared/Checked/Approved By record (client_id+transaction_ref
// scoped, shared across every line of that voucher) — factored out of openProcedureModal() so
// updateProceduresCellForLine()'s standalone badge computation (below) can also warm the same
// cache for a transaction_level_approval-flagged Document, without duplicating this fetch.
async function ensureVoucherApprovalLoaded(clientId, transactionRef) {
    if (!transactionRef) return null;
    if (voucherApprovalCache[transactionRef] && voucherApprovalCache[transactionRef].loaded) {
        return voucherApprovalCache[transactionRef];
    }
    try {
        const params = new URLSearchParams({ client_id: clientId, transaction_ref: transactionRef });
        const vaRes = await fetch(`${API_BASE}/api/verification/voucher_approval?${params}`);
        if (vaRes.ok) {
            const vaData = await vaRes.json();
            voucherApprovalCache[transactionRef] = {
                client_id: clientId,
                transaction_ref: transactionRef,
                prepared_by: vaData.prepared_by || '',
                checked_by: vaData.checked_by || '',
                approved_by: vaData.approved_by || '',
                dirty: false,
                loaded: true
            };
        }
    } catch (e) {
        console.error('Failed to load voucher approval record:', e);
    }
    return voucherApprovalCache[transactionRef] || null;
}

// Shared completeness check for one document within one voucher line's modal — used both by the
// rail's completion dot (below) and by openProcedureModal()'s default-tab selection. A no-template
// document has no evidence table to link, so linked-evidence presence (assignedDocIds) is
// meaningless for it — its completeness instead reflects whether every one of its Audit Procedures
// is ticked (voucher_line_procedure_checks, same source the checklist itself reads via
// getProcedureTabState()). Zero procedures configured is deliberately NOT complete (empty ≠ done),
// per the confirmed requirement. Transaction level approval (Phase 4 of 4, FINAL): a Document
// flagged transaction_level_approval requires the voucher-level approval fields to ALSO be
// complete, on top of (not instead of) its own base completeness rule above.
function isProcedureDocumentComplete(uid, doc) {
    // Validation Rules Phase 5: the synthetic Voucher Line tab is complete only when all three
    // approval fields are filled — currentVoucherId is this open voucher's transaction_ref
    // (the modal only ever shows lines of the currently open voucher).
    if (doc.__voucherTab) {
        return isVoucherApprovalComplete(voucherApprovalCache[currentVoucherId]);
    }
    let baseComplete;
    if (doc.template_id) {
        const lineState = procedureModalState[uid];
        baseComplete = !!(lineState && lineState.assignedDocIds && lineState.assignedDocIds.has(doc.id));
    } else {
        const tabState = getProcedureTabState(uid, doc.id);
        const roles = doc.auth_roles || [];
        baseComplete = roles.length > 0 && roles.every(role => tabState.checklist[role] && tabState.checklist[role].is_checked);
    }
    if (!baseComplete) return false;
    if (doc.transaction_level_approval) {
        return isVoucherApprovalComplete(voucherApprovalCache[currentVoucherId]);
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

        // Validation Rules Phase 5: the synthetic Voucher Line tab has no roles/mismatch concept —
        // just name + completion dot.
        if (doc.__voucherTab) {
            const dotTitle = isComplete ? 'Approval recorded' : 'Approval pending';
            return `
                <button type="button" class="procedure-tab-btn${isActive ? ' active' : ''}" data-uid="${uid}" data-doc-id="${doc.id}">
                    <span class="procedure-tab-dot${isComplete ? ' complete' : ''}" title="${dotTitle}"></span>
                    <span class="procedure-tab-text">
                        <span class="procedure-tab-label">${escapeHtml(doc.name)}</span>
                    </span>
                </button>
            `;
        }

        const dotTitle = doc.template_id
            ? (isComplete ? 'Evidence linked' : 'No evidence linked yet')
            : (isComplete ? 'All procedures confirmed' : 'Procedures pending');

        // "X/Y roles" subtext — same checklist state the completion dot itself reads.
        const tabState = getProcedureTabState(uid, doc.id);
        const roles = doc.auth_roles || [];
        const rolesDone = roles.filter(r => tabState.checklist[r] && tabState.checklist[r].is_checked).length;

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
                    <span class="procedure-tab-sub">${rolesDone}/${roles.length} roles</span>
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
// Validation Rules Phase 5: Prepared/Checked/Approved By fields for the synthetic Voucher Line
// tab. Reuses the app's neutral "Data Table" segment styling (grey header, white body) so it
// stays visually consistent with the other tabs' segment cards without inventing a new look.
// Local-first, no-network-per-keystroke — same pattern as the Observation textarea (see
// buildObservationSection()) — flushed via flushVoucherApproval() at the same save points.
function renderVoucherApprovalPanel(uid) {
    const container = document.getElementById('procedureModalPanels');
    if (!container) return;
    const transactionRef = currentVoucherId;
    const va = voucherApprovalCache[transactionRef] || { prepared_by: '', checked_by: '', approved_by: '', dirty: false, loaded: false };

    const lineIdx = currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    const voucherLine = lineIdx !== -1 ? currentVoucherLines[lineIdx] : null;
    const currentAccountName = voucherLine ? getVoucherLineAlias(voucherLine.row_data || {}, VOUCHER_LINE_ACCOUNT_NAME_ALIASES) : '—';

    // Reposition (confirmed fix): Account Head Recommendation now sits above Voucher Approval —
    // DOM order swapped from the original Phase 5/7 build, no other change to this outer shell.
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
            <div class="procedure-segment procedure-segment-data">
                <h4 class="procedure-segment-header">🖊️ Voucher Approval</h4>
                <div class="procedure-segment-body">
                    <p class="procedure-tab-note">Mandatory for every line of this voucher — independent of Category and any document's own approval requirement.</p>
                    <div class="procedure-voucher-approval-field">
                        <label for="procedureVoucherPreparedBy">Prepared By</label>
                        <input type="text" id="procedureVoucherPreparedBy" class="procedure-search-input" value="${escapeHtml(va.prepared_by)}">
                    </div>
                    <div class="procedure-voucher-approval-field">
                        <label for="procedureVoucherCheckedBy">Checked By</label>
                        <input type="text" id="procedureVoucherCheckedBy" class="procedure-search-input" value="${escapeHtml(va.checked_by)}">
                    </div>
                    <div class="procedure-voucher-approval-field">
                        <label for="procedureVoucherApprovedBy">Approved By</label>
                        <input type="text" id="procedureVoucherApprovedBy" class="procedure-search-input" value="${escapeHtml(va.approved_by)}">
                    </div>
                </div>
            </div>
        </div>
    `;

    const fieldMap = {
        procedureVoucherPreparedBy: 'prepared_by',
        procedureVoucherCheckedBy: 'checked_by',
        procedureVoucherApprovedBy: 'approved_by'
    };
    Object.keys(fieldMap).forEach(elId => {
        const el = document.getElementById(elId);
        if (!el) return;
        el.addEventListener('input', function() {
            if (!voucherApprovalCache[transactionRef]) {
                voucherApprovalCache[transactionRef] = { client_id: getClientId(), transaction_ref: transactionRef, prepared_by: '', checked_by: '', approved_by: '', dirty: false, loaded: true };
            }
            voucherApprovalCache[transactionRef][fieldMap[elId]] = this.value;
            voucherApprovalCache[transactionRef].dirty = true;
            updateProcedureModalSavedNote(uid);
        });
    });

    renderAccountHeadCard(uid);
}

// Renders just the Account Head Recommendation segment's body (#procedureAccountHeadBody) —
// scoped re-render so toggling match/no-match mode doesn't disturb the sibling Voucher Approval
// fields' focus/typed state. Branches entirely on accountHeadCache[uid].mode:
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

    const tabState = getProcedureTabState(uid, docId);
    const hasTemplate = !!doc.template_id;

    if (!hasTemplate) {
        // No template means no Data Bank template to search evidence against, so the Data Table
        // segment holds only the explanatory note — search box/results dropdown/linked-evidence
        // table are skipped entirely. Still rendered as a segment (not omitted) so the fixed
        // Data Table -> Audit Procedures -> Observation order/visual grouping stays consistent
        // regardless of whether a document has a template.
        container.innerHTML = `
            <div class="procedure-tab-panel" data-doc-id="${docId}">
                <div class="procedure-segment procedure-segment-data">
                    <h4 class="procedure-segment-header">🗂️ Data Table</h4>
                    <div class="procedure-segment-body">
                        <p class="procedure-tab-note">No Data Bank template linked to "${escapeHtml(doc.name)}" — evidence search isn't available for this document. Configure a template in Audit Rules to enable it.</p>
                    </div>
                </div>
                <div class="procedure-segment procedure-segment-procedures">
                    <h4 class="procedure-segment-header">✅ Audit Procedures</h4>
                    <div class="procedure-segment-body">${buildProcedureChecklist(uid, docId)}</div>
                </div>
                <div class="procedure-segment procedure-segment-observation">
                    <h4 class="procedure-segment-header">📝 Observation</h4>
                    <div class="procedure-segment-body">${buildObservationSection(uid, docId)}</div>
                </div>
            </div>
        `;
        wireProcedureChecklistAndObservation(container, uid, docId, tabState);
        return;
    }

    container.innerHTML = `
        <div class="procedure-tab-panel" data-doc-id="${docId}">
            <div class="procedure-segment procedure-segment-data">
                <h4 class="procedure-segment-header">🗂️ Data Table</h4>
                <div class="procedure-segment-body">
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
            <div class="procedure-segment procedure-segment-procedures">
                <h4 class="procedure-segment-header">✅ Audit Procedures</h4>
                <div class="procedure-segment-body">${buildProcedureChecklist(uid, docId)}</div>
            </div>
            <div class="procedure-segment procedure-segment-observation">
                <h4 class="procedure-segment-header">📝 Observation</h4>
                <div class="procedure-segment-body">${buildObservationSection(uid, docId)}</div>
            </div>
        </div>
    `;

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

    wireProcedureChecklistAndObservation(container, uid, docId, tabState);

    // Unassign requirement: one click listener per ✕ unlink button in the Linked Evidence table.
    container.querySelectorAll('.procedure-unlink-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            unassignSupportingDocument(uid, docId, parseInt(this.dataset.assignmentId));
        });
    });

    // Validation Rules Phase 5: one change listener per Sig. Exc. checkbox in the Linked
    // Evidence table — immediate save on toggle, same convention as the Audit Procedures
    // checklist ticks (no separate Save action).
    container.querySelectorAll('.procedure-sig-exception-checkbox').forEach(cb => {
        cb.addEventListener('change', function() {
            toggleSignatureException(uid, docId, parseInt(this.dataset.assignmentId), this.checked);
        });
    });

    // Restore a previously-fetched search's dropdown (extends Phase 3's "switching tabs doesn't
    // lose unsaved work" to cover search results too) — re-renders from tabState, no new fetch.
    if (tabState.searchResults && tabState.searchResults.length) {
        renderProcedureSearchDropdown(uid, docId);
    }
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
    // Sig. Exc. (Validation Rules Phase 5): defaults unticked/OK — the auditor ticks it only to
    // flag a confirmed mismatch against the document's signature specimen, per Phase 1's locked
    // assumption. Sits in the existing Linked Evidence row, not a separate control.
    let html = '<div class="procedure-selected-table-wrapper"><table class="procedure-selected-table"><thead><tr>';
    html += '<th>Sr No</th>';
    columns.forEach(col => { html += `<th>${escapeHtml(col.column_name)}</th>`; });
    html += '<th>Sig. Exc.</th>';
    html += '<th></th>';
    html += '</tr></thead><tbody>';
    selections.forEach((sel, idx) => {
        html += '<tr>';
        html += `<td>${idx + 1}</td>`;
        columns.forEach(col => {
            const value = getRowFieldCaseInsensitive(sel.row_data, col.column_name);
            html += `<td>${escapeHtml(value)}</td>`;
        });
        const sigChecked = !!sel.signature_exception;
        html += `<td><input type="checkbox" class="procedure-sig-exception-checkbox" data-assignment-id="${sel.id}" title="Flag a confirmed signature mismatch" ${sigChecked ? 'checked' : ''}></td>`;
        html += `<td><button type="button" class="procedure-unlink-btn" data-assignment-id="${sel.id}" title="Unlink this evidence">✕</button></td>`;
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
    html += '<td></td>'; // Sig. Exc. column stays blank on the totals row
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

// Validation Rules Phase 5: persists a dirty (unsaved) voucher-approval edit — called from the
// same save chokepoint as flushProcedureObservations() (saveLineItems()). No-op if nothing
// changed since the last save. On a successful save, refreshes every currently-loaded line of
// this voucher (not just the active one) via saveAllVoucherLines(), since the approval record is
// shared across the whole voucher, not scoped to one line — matches the backend's own "applied to
// every line of that voucher" design (Phase 4). Marking dirty=false before that call means any
// nested saveLineItems()->flushVoucherApproval() calls it triggers simply no-op, not recurse.
async function flushVoucherApproval(uid) {
    const transactionRef = currentVoucherId;
    const va = voucherApprovalCache[transactionRef];
    if (!va || !va.dirty) return;

    try {
        const res = await fetch(`${API_BASE}/api/verification/save_voucher_approval`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                client_id: va.client_id,
                transaction_ref: transactionRef,
                prepared_by: va.prepared_by,
                checked_by: va.checked_by,
                approved_by: va.approved_by
            })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to save voucher approval.');
        }
        va.dirty = false;
        updateProcedureModalSavedNote(uid);
        await saveAllVoucherLines();
    } catch (e) {
        console.error('Failed to save voucher approval:', e);
        showStatus('Failed to save voucher approval — please try again.', 'error');
    }
}

// Account Head Recommendation follow-up: persists a dirty no-match finding — called from the same
// save chokepoint as flushProcedureObservations()/flushVoucherApproval() (saveLineItems()). No-op
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
    const columns = Object.keys(rows[0].row_data || {});

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
document.getElementById('procedureModalNextBtn')?.addEventListener('click', function() {
    navigateProcedureModalLine(1);
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