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
const VOUCHER_LINE_DESCRIPTION_ALIASES = ['Description', 'Voucher Detail', 'Narration', 'Remarks', 'Particulars'];
const VOUCHER_LINE_DEBIT_ALIASES = ['Debit Amount', 'Debit', 'Dr Amount', 'Dr'];
const VOUCHER_LINE_CREDIT_ALIASES = ['Credit Amount', 'Credit', 'Cr Amount', 'Cr'];

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
let auditGuidanceCache = {};

async function fetchAuditGuidanceData(type) {
    if (Object.prototype.hasOwnProperty.call(auditGuidanceCache, type)) {
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

// Renders the Supporting column's document links for one line, from its currently-selected
// Category's attached Documents (data.documents[]) — replaces the old fixed VOUCHER_LINE_PROCEDURES
// list. Async because the Category's guidance (and thus its Documents) is fetched on demand;
// called both at initial render (for a line with an already-seeded type) and from the Type
// dropdown's change listener. A Document with no template_id configured (Audit Rules module)
// renders as plain muted text, not clickable — openProcedureModal() needs a real template_id to
// search Data Bank evidence against.
async function updateSupportingCellForLine(uid, type) {
    const cell = document.querySelector(`.voucher-line-supporting-cell[data-detail-id="${uid}"]`);
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
        console.error('Failed to load Supporting documents:', e);
    }
    // Stale guard: the line may have moved on to a different type (or a new voucher entirely)
    // while this fetch was in flight — don't clobber a newer selection.
    if (!voucherLineState[uid] || voucherLineState[uid].transactionType !== type) return;
    const documents = (data && data.documents) || [];
    if (!documents.length) {
        cell.innerHTML = '<span style="color:#94a3b8; font-size:12px;">No documents for this category</span>';
        return;
    }
    cell.innerHTML = documents.map(doc => {
        const state = voucherLineState[uid];
        const procKey = `doc_${doc.id}`;
        const confirmed = state && state.procedureData && state.procedureData[procKey];
        const hasTemplate = !!doc.template_id;
        const muted = hasTemplate ? '' : ' style="color:#94a3b8; cursor:default; text-decoration:none;"';
        const title = hasTemplate
            ? (confirmed ? doc.name + ' — confirmed' : 'Search ' + doc.name)
            : (doc.name + ' — no template linked (set one in Audit Rules)');
        return `<span class="voucher-line-proc-link${confirmed ? ' confirmed' : ''}" data-uid="${uid}" data-proc="${procKey}" data-doc-id="${doc.id}" data-label="${escapeHtml(doc.name)}" data-template="${doc.template_id || ''}" title="${escapeHtml(title)}"${muted}>${escapeHtml(doc.name)}</span>`;
    }).join('');
    // This cell's links are created after the render's own batch event-wiring pass already ran
    // (see renderVoucherLines()), so they're wired here instead.
    cell.querySelectorAll('.voucher-line-proc-link').forEach(link => {
        link.addEventListener('click', function(e) {
            e.stopPropagation();
            if (!this.dataset.template) return; // no template configured for this Document yet
            const uid2 = this.dataset.uid;
            const docId2 = parseInt(this.dataset.docId);
            // Phase 3: opens the tabbed modal for the whole line, pre-selecting this document's tab.
            openProcedureModal(uid2, docId2);
        });
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

    // Supporting already no-ops on a falsy type, so only call it when there's a real match.
    if (categoryName) updateSupportingCellForLine(uid, categoryName);
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

// Order matches .voucher-line-summary-grid's columns: Account Code, Type, Account Name, Amount,
// Supporting, Observations, Status. User-resizable (see the drag handles in renderVoucherLines()'s
// header row) — persists at module scope across renders/navigation, same convention as
// voucherLineState, so dragging a column survives Prev/Next voucher navigation.
let voucherColumnWidths = [100, 170, 395, 90, 130, 300, 90];

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

// Maps a validation_status string to the same icon/class convention used by the transaction-list
// popover's .status-icon (renderTransactionPopover()) — reused rather than inventing a new one.
function getVoucherLineStatusIcon(validationStatus) {
    const status = (validationStatus || 'Pending');
    const statusClass = status.toLowerCase();
    let icon = '❌';
    if (statusClass === 'passed') icon = '✅';
    else if (statusClass === 'pending') icon = '⏳';
    return `<span class="status-icon ${statusClass}" title="${status}">${icon}</span>`;
}

// ---------- VOUCHER LINE STATE (Phase 3) ----------
// Seeds voucherLineState fresh from a voucher's lines. Accepts either a /next line object
// (has transaction_type + procedure_data + change_detection_status/old_row_data) or an
// audited_cashbook line object (has procedure_flags, no transaction_type/change-detection fields)
// — same fallback pattern navigateToTransaction() already uses for the single-active-line
// globals, applied per line here.
function seedVoucherLineState(lines) {
    voucherLineState = {};
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
            <span>Account Code<span class="voucher-col-resize-handle" data-col="0"></span></span>
            <span>Type<span class="voucher-col-resize-handle" data-col="1"></span></span>
            <span>Account Name<span class="voucher-col-resize-handle" data-col="2"></span></span>
            <span class="voucher-line-amount-col">Amount<span class="voucher-col-resize-handle" data-col="3"></span></span>
            <span>Supporting<span class="voucher-col-resize-handle" data-col="4"></span></span>
            <span>Observations<span class="voucher-col-resize-handle" data-col="5"></span></span>
            <span>Status<span class="voucher-col-resize-handle" data-col="6"></span></span>
        </div>
    `;

    // Collected during the loop below so the Type auto-fill lookups (which need real DOM nodes
    // to write into) can run once, after container.innerHTML is set.
    const lineAccountCodes = [];

    lines.forEach(line => {
        const uid = line.detail_id;
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
                    <span class="voucher-line-code">${accountCode}</span>
                    <span class="voucher-line-type-cell">
                        <span class="voucher-line-type-value-row">
                            <span class="voucher-line-type-value" data-uid="${uid}">Loading…</span>
                            <span class="voucher-line-type-recycle-icon" data-uid="${uid}" data-account-code="${escapeHtml(accountCode)}" title="No category assigned — click to assign one in Assign Categories" style="display:none">🔄</span>
                        </span>
                        <span class="voucher-line-type-chips">
                            <button type="button" class="voucher-line-chip voucher-line-audit-icon" data-uid="${uid}" title="Audit guidance">📋 Guide</button>
                            <button type="button" class="voucher-line-chip voucher-line-dfd-chip" data-uid="${uid}" title="Data Flow Diagram">🔀 DFD</button>
                        </span>
                    </span>
                    <span class="voucher-line-name-cell">
                        <span class="voucher-line-name">${accountName}${changeIconHtml}${deletedBadgeHtml}</span>
                        <span class="voucher-line-desc">${description}</span>
                    </span>
                    <span class="voucher-line-amount">${amount}</span>
                    <span class="voucher-line-supporting-cell" data-detail-id="${uid}"></span>
                    <span class="voucher-line-observations-cell"></span>
                    <span class="voucher-line-status status-${validationStatus}" data-validation-status="${validationStatus}">${getVoucherLineStatusIcon(line.validation_status)} ${validationStatus}</span>
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

    // Supporting column — populated asynchronously per line (updateSupportingCellForLine(), see
    // above), since it depends on a fetch of the line's currently-selected Category. That
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

// Builds one selectable/greyed result row's HTML — shared by the search dropdown and the browse
// modal so both present identical lock-state treatment, driven by Phase 2's
// is_assigned/assigned_to_voucher_line/assigned_to_voucher_id fields. docKeyColumn (the
// template's own document-key column name, from the same response) labels the row the same way
// Data Bank's own preview identifies a document — falls back to a bare row id if unknown.
function buildProcedureResultRowHtml(uid, row, docKeyColumn) {
    const label = (docKeyColumn && row.row_data && row.row_data[docKeyColumn] != null && row.row_data[docKeyColumn] !== '')
        ? row.row_data[docKeyColumn]
        : `Row #${row.id}`;
    const date = row.extract_date || '—';
    const amount = row.extract_amount != null ? row.extract_amount : '—';
    const isSelf = row.is_assigned && String(row.assigned_to_voucher_line) === String(uid);
    const isLocked = !!row.is_assigned;
    let lockNote = '';
    if (isSelf) {
        lockNote = '<span class="procedure-result-locked">Already linked here</span>';
    } else if (isLocked) {
        lockNote = `<span class="procedure-result-locked">Assigned to voucher ${escapeHtml(row.assigned_to_voucher_id || '')}</span>`;
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
// exists. suggestionApplied (Phase 6, client-only — never persisted) tracks whether the
// auto-suggestion has already been offered for this tab, so it fills an empty box at most
// once and never overwrites text the auditor typed or deliberately cleared. checklist
// (Phase 5) is a map keyed by role_name -> {is_checked, checked_by, checked_at}, seeded from
// GET /api/verification/procedure_checks/{uid} in openProcedureModal() — a role with no entry
// is unchecked.
function getProcedureTabState(uid, docId) {
    if (!procedureModalState[uid]) procedureModalState[uid] = { activeDocId: null, documents: [], assignedDocIds: new Set(), voucherLineAmount: 0, tabs: {} };
    if (!procedureModalState[uid].tabs[docId]) {
        procedureModalState[uid].tabs[docId] = { loaded: false, searchTerm: '', searchResults: [], docKeyColumn: null, selections: [], observation: '', suggestionApplied: false, checklist: {} };
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

    let guidance = null;
    try {
        guidance = await fetchAuditGuidanceData(type);
    } catch (e) {
        console.error('Failed to load Supporting Documents modal data:', e);
        return;
    }
    const documents = (guidance && guidance.documents) || [];
    if (!documents.length) return;

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

    // The voucher line's own amount (for the mismatch badge) — looked up from the already-loaded
    // voucher, no extra fetch needed.
    const voucherLine = currentVoucherLines.find(l => l.detail_id === uid);
    const voucherLineAmount = voucherLine ? parseVoucherAmount(getVoucherLineAmount(voucherLine.row_data || {})) : 0;

    if (!procedureModalState[uid]) procedureModalState[uid] = { activeDocId: null, documents: [], assignedDocIds: new Set(), voucherLineAmount: 0, tabs: {} };
    procedureModalState[uid].documents = documents;
    procedureModalState[uid].assignedDocIds = assignedDocIds;
    procedureModalState[uid].voucherLineAmount = voucherLineAmount;
    const activeDocId = (initialDocId && documents.some(d => d.id === initialDocId)) ? initialDocId : documents[0].id;
    procedureModalState[uid].activeDocId = activeDocId;

    // Re-sync each tab's selections and checklist from the fresh server data (never just seeded
    // once) so reopening the modal always reflects reality, even if a change happened elsewhere.
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
        }
    });

    if (procedureModalTitle) procedureModalTitle.textContent = `📎 Supporting Documents — ${type}`;
    procedureModal.style.display = 'flex';

    renderProcedureModalTabs(uid);
    activateProcedureTab(uid, activeDocId);
}

function closeProcedureModal() {
    if (procedureModal) procedureModal.style.display = 'none';
    // Defensive — the browse modal is nested on top of this one, so it should never remain open
    // once the parent closes.
    closeProcedureBrowseModal();
    // procedureModalState[uid] is deliberately left in place (not cleared here) — reopening the
    // same line's modal within the same voucher should show tab state exactly as it was left;
    // it's only reset on voucher navigation, in seedVoucherLineState().
}

// Renders the tab bar for one line's modal: one button per document, name + a completion dot
// (filled when that document has at least one linked evidence row for this line, per the
// assignedDocIds presence check from openProcedureModal()).
function renderProcedureModalTabs(uid) {
    const container = document.getElementById('procedureModalTabs');
    if (!container) return;
    const lineState = procedureModalState[uid];
    if (!lineState) { container.innerHTML = ''; return; }
    const documents = lineState.documents || [];
    const assignedDocIds = lineState.assignedDocIds || new Set();

    container.innerHTML = documents.map(doc => {
        const isActive = doc.id === lineState.activeDocId;
        const isComplete = assignedDocIds.has(doc.id);
        const dotTitle = isComplete ? 'Evidence linked' : 'No evidence linked yet';
        return `
            <button type="button" class="procedure-tab-btn${isActive ? ' active' : ''}" data-uid="${uid}" data-doc-id="${doc.id}">
                <span class="procedure-tab-dot${isComplete ? ' complete' : ''}" title="${dotTitle}"></span>
                <span class="procedure-tab-label">${escapeHtml(doc.name)}</span>
            </button>
        `;
    }).join('');

    container.querySelectorAll('.procedure-tab-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            activateProcedureTab(this.dataset.uid, parseInt(this.dataset.docId));
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

// Renders one tab's body: a search box + results dropdown, and the selected-lines table (Phase
// 4). Procedure ticks/observations remain Phase 5-6 territory — not added here. A document with
// no template_id configured shows an explicit note and disables search/browse (Phase 3's original
// note, still applies since neither search nor browse can query without one).
function renderProcedureModalPanel(uid, docId) {
    const container = document.getElementById('procedureModalPanels');
    if (!container) return;
    const lineState = procedureModalState[uid];
    const doc = lineState && (lineState.documents || []).find(d => d.id === docId);
    if (!doc) { container.innerHTML = ''; return; }

    const tabState = getProcedureTabState(uid, docId);
    const hasTemplate = !!doc.template_id;

    // Phase 6: offer the auto-suggestion (a one-shot fill, never re-run over edited/cleared/
    // saved text) before building the panel's markup, so the textarea below reflects it.
    applyObservationSuggestionIfNeeded(uid, docId);

    const noTemplateNote = hasTemplate
        ? ''
        : `<p class="procedure-tab-note">No Data Bank template linked to "${escapeHtml(doc.name)}" yet — configure one in Audit Rules before searching.</p>`;

    container.innerHTML = `
        <div class="procedure-tab-panel" data-doc-id="${docId}">
            ${noTemplateNote}
            <div class="procedure-search-row">
                <input type="text" id="procedureSearchInput" class="procedure-search-input"
                    placeholder="Search by reference, vendor, amount..." value="${escapeHtml(tabState.searchTerm)}"
                    ${hasTemplate ? '' : 'disabled'}>
                <button type="button" id="procedureBrowseBtn" class="procedure-browse-btn" title="Browse all rows for this document"
                    ${hasTemplate ? '' : 'disabled'}>👁️</button>
            </div>
            <div id="procedureSearchDropdown" class="procedure-search-dropdown"></div>
            <div class="procedure-selected-section">
                <h4 class="procedure-selected-heading">Linked evidence</h4>
                ${buildSelectedLinesTable(uid, docId)}
            </div>
            <div class="procedure-checklist-section">
                <h4 class="procedure-checklist-heading">Audit Procedures</h4>
                ${buildProcedureChecklist(uid, docId)}
            </div>
            <div class="procedure-observation-section">
                <h4 class="procedure-observation-heading">Observation</h4>
                ${buildObservationSection(uid, docId)}
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

    // Phase 5: one change listener per checklist checkbox — ticking/unticking a role posts the
    // new state immediately (see toggleProcedureCheck()), no separate Save action.
    container.querySelectorAll('.procedure-checklist-checkbox').forEach(cb => {
        cb.addEventListener('change', function() {
            toggleProcedureCheck(uid, docId, this.dataset.role, this.checked);
        });
    });

    // Unassign requirement: one click listener per ✕ unlink button in the Linked Evidence table.
    container.querySelectorAll('.procedure-unlink-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            unassignSupportingDocument(uid, docId, parseInt(this.dataset.assignmentId));
        });
    });

    // Phase 6: the observation textarea is local-only on every keystroke (no network call) — it
    // is only persisted when the Save button is explicitly clicked, per the confirmed brief.
    const observationInput = document.getElementById('procedureObservationInput');
    if (observationInput) {
        observationInput.addEventListener('input', function() {
            tabState.observation = this.value;
        });
    }
    const observationSaveBtn = document.getElementById('procedureObservationSaveBtn');
    if (observationSaveBtn) {
        observationSaveBtn.addEventListener('click', function() {
            saveLineObservation(uid, docId);
        });
    }

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
    dropdown.innerHTML = results.map(row => buildProcedureResultRowHtml(uid, row, tabState.docKeyColumn)).join('');
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

function buildSelectedLinesTable(uid, docId) {
    const tabState = getProcedureTabState(uid, docId);
    const selections = tabState.selections || [];
    if (!selections.length) {
        return '<p class="procedure-selected-empty">No evidence linked yet for this document.</p>';
    }

    // Column order: first-seen order across every selected row's row_data keys, so the table
    // shows every real column present without hardcoding a template's shape.
    const columns = [];
    selections.forEach(sel => {
        Object.keys(sel.row_data || {}).forEach(key => {
            if (!columns.includes(key)) columns.push(key);
        });
    });

    // Trailing "" header for the unlink icon column (Unassign requirement) - no label, matching
    // this app's existing convention for icon-only action columns (e.g. Data Bank's Actions column).
    let html = '<div class="procedure-selected-table-wrapper"><table class="procedure-selected-table"><thead><tr>';
    columns.forEach(col => { html += `<th>${escapeHtml(col)}</th>`; });
    html += '<th></th>';
    html += '</tr></thead><tbody>';
    selections.forEach(sel => {
        html += '<tr>';
        columns.forEach(col => {
            const value = sel.row_data ? sel.row_data[col] : '';
            html += `<td>${escapeHtml(value == null ? '' : String(value))}</td>`;
        });
        html += `<td><button type="button" class="procedure-unlink-btn" data-assignment-id="${sel.id}" title="Unlink this evidence">✕</button></td>`;
        html += '</tr>';
    });
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
// showAuditGuidancePopover()/updateSupportingCellForLine() — no new fetch for the role list
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
    } catch (e) {
        console.error('Failed to save audit procedure check:', e);
        if (previousState) {
            tabState.checklist[roleName] = previousState;
        } else {
            delete tabState.checklist[roleName];
        }
        showStatus('Failed to save procedure tick — please try again.', 'error');
        renderProcedureModalPanel(uid, docId);
    }
}

// ---------- OBSERVATION FLOW (Phase 6 of 6, FINAL) ----------
// Builds an auto-suggested observation for a tab from (a) the Phase 4 amount-mismatch state
// (getSelectedLinesSummary(), shared with buildSelectedLinesTable()'s own badge) and (b) any
// Phase 5 unticked procedures for this document. Returns '' (no forced/neutral text) when
// neither condition applies — only ever offered once per tab (see suggestionApplied in
// getProcedureTabState()'s doc comment), never re-run over text the auditor edited/cleared.
function buildObservationSuggestion(uid, docId) {
    const lineState = procedureModalState[uid];
    const doc = lineState && (lineState.documents || []).find(d => d.id === docId);
    const tabState = getProcedureTabState(uid, docId);
    const lines = [];

    const { total, voucherAmount, mismatch } = getSelectedLinesSummary(uid, docId);
    if (mismatch) {
        const diff = total - voucherAmount;
        lines.push(`Amount mismatch: evidence total ${total.toFixed(2)} vs voucher line amount ${voucherAmount.toFixed(2)} (diff ${diff.toFixed(2)}).`);
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
    if (suggestion) tabState.observation = suggestion;
    tabState.suggestionApplied = true;
}

// Renders one tab's observation section: a textarea (local-only edits) + an explicit Save
// button. Saving is deliberate (not save-on-keystroke, not save-on-modal-close) since the
// auditor is expected to review/edit the auto-suggestion before it's persisted.
function buildObservationSection(uid, docId) {
    const tabState = getProcedureTabState(uid, docId);
    return `
        <textarea id="procedureObservationInput" class="procedure-observation-textarea"
            placeholder="Add an observation for this document...">${escapeHtml(tabState.observation || '')}</textarea>
        <div class="procedure-observation-actions">
            <button type="button" id="procedureObservationSaveBtn" class="procedure-observation-save-btn">💾 Save Observation</button>
            <span id="procedureObservationStatus" class="procedure-observation-status"></span>
        </div>
    `;
}

// Persists the active tab's observation text as a tagged entry (POST save_line_observation).
// Always marks it unsynced server-side — it will be rolled into the voucher line's own
// Observation field the next time that line is saved (saveLineItems(), via
// sync_line_observations_to_voucher() in routers/verification.py).
async function saveLineObservation(uid, docId) {
    const tabState = getProcedureTabState(uid, docId);
    const statusEl = document.getElementById('procedureObservationStatus');
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
        // A save is a deliberate, final decision on this tab's text — it should never be
        // silently replaced by the auto-suggestion again, even if the box is later cleared.
        tabState.suggestionApplied = true;
        if (statusEl) {
            statusEl.textContent = 'Saved';
            statusEl.classList.add('saved');
            setTimeout(() => { statusEl.textContent = ''; statusEl.classList.remove('saved'); }, 2000);
        }
    } catch (e) {
        console.error('Failed to save line observation:', e);
        showStatus('Failed to save observation — please try again.', 'error');
    }
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
function buildProcedureBrowseTableHtml(uid, rows) {
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
        html += `<td>${isAssigned ? escapeHtml(row.assigned_to_voucher_id || '') : ''}</td>`;
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
        procedureBrowseTableContainer.innerHTML = buildProcedureBrowseTableHtml(uid, filteredRows);
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