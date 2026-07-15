// ==============================================
// ✅ VERIFICATION MODULE – CASHBOOK + VERIFICATION FLOW
// ==============================================

// API_BASE defined in client_context.js
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
// scoped to #transactionCard's single "active" line — no shared state between the two.
let voucherLineState = {};
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

const statusDiv = document.getElementById('verificationStatus');
// Phase 2: standalone transaction view removed — prevBtn/nextBtn/verifyBtn/saveProgressBtn/exitBtn
// elements no longer exist in verification.html (the nav controls are now in #voucherHeaderBar).
// Their JS references and listener blocks have been removed alongside the old view.

const cashbookNameDisplay = document.getElementById('cashbookNameDisplay');
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
const exitCashbookLink = document.getElementById('exitCashbookLink');

const supportingModal = document.getElementById('supportingModal');
const supportingSearchInput = document.getElementById('supportingSearchInput');
const supportingSearchBtn = document.getElementById('supportingSearchBtn');
const supportingResults = document.getElementById('supportingResults');
const supportingModalClose = document.getElementById('supportingModalClose');

// ---- PROCEDURE MODAL REFS ----
const procedureModal = document.getElementById('procedureModal');
const procedureModalTitle = document.getElementById('procedureModalTitle');
const procedureSearchInput = document.getElementById('procedureSearchInput');
const procedureSearchBtn = document.getElementById('procedureSearchBtn');
const procedureResults = document.getElementById('procedureResults');
const procedureCancelBtn = document.getElementById('procedureCancelBtn');
const procedureConfirmBtn = document.getElementById('procedureConfirmBtn');
const procedureStatus = document.getElementById('procedureStatus');
const procedureModalClose = document.getElementById('procedureModalClose');

let currentProcedureKey = null;
let currentTemplateId = null;
let selectedDocId = null;
let selectedDocData = null;
// Phase 3: null = the existing #transactionCard flow (unchanged behavior below); set to a line's
// detail_id when the modal was opened from a voucher line's collapsed/expand row.
let currentProcedureTarget = null;

// ---- REPORT VIEW REFS ----
let reportCurrentPage = 1;
let reportPageSize = 25;
let reportTotalRows = 0;
let reportTemplateId = null;

const reportContent = document.getElementById('transactionReportContent');
const reportStartDate = document.getElementById('reportStartDate');
const reportEndDate = document.getElementById('reportEndDate');
const reportFetchBtn = document.getElementById('reportFetchBtn');
const reportStatus = document.getElementById('reportStatus');
const reportTableWrapper = document.getElementById('reportTableWrapper');
const reportPageSizeSelect = document.getElementById('reportPageSize');
const reportPrevPage = document.getElementById('reportPrevPage');
const reportNextPage = document.getElementById('reportNextPage');
const reportPageInfo = document.getElementById('reportPageInfo');
const reportRowCount = document.getElementById('reportRowCount');
const reportClientInfo = document.getElementById('reportClientInfo');
const exitReportLink = document.getElementById('exitReportLink');

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
    try {
        const params = new URLSearchParams({
            client_id: clientId,
            date_start: start,
            date_end: end
        });
        const res = await fetch(`${API_BASE}/api/verification/cashbooks?${params}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch cashbooks');
        }
        const data = await res.json();
        if (data.cashbooks && data.cashbooks.length > 0) {
            popupCashbookSelect.innerHTML = '<option value="">-- Select Cashbook --</option>';
            data.cashbooks.forEach(cb => {
                const opt = document.createElement('option');
                opt.value = cb;
                opt.textContent = cb;
                popupCashbookSelect.appendChild(opt);
            });
            popupCashbookSelect.disabled = false;
            popupStartBtn.disabled = false;
        } else {
            popupCashbookSelect.innerHTML = '<option value="">⚠️ No cashbooks found</option>';
            popupCashbookSelect.disabled = true;
            popupStartBtn.disabled = true;
            showPopupError(
                'No cashbooks found for this client.\n' +
                'Make sure you have uploaded data containing CPV/CRV vouchers.\n' +
                'The column names must be exactly: "Voucher ID", "Account Name", "Debit", "Credit".'
            );
        }
    } catch (e) {
        console.error('Error loading cashbooks:', e);
        popupCashbookSelect.innerHTML = '<option value="">⚠️ Error loading</option>';
        popupCashbookSelect.disabled = true;
        showPopupError('Failed to load cashbooks: ' + e.message);
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
    selectedCashbook = cashbook;
    popupOverlay.style.display = 'none';
    dashboardContent.style.display = 'none';
    sessionContent.style.display = 'none';
    cashbookContent.style.display = 'block';
    cashbookNameDisplay.textContent = cashbook;
    const now = new Date();
    const firstDay = new Date(now.getFullYear(), now.getMonth(), 1);
    const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    cashbookStartDate.value = formatDateInput(firstDay);
    cashbookEndDate.value = formatDateInput(lastDay);
    await fetchCashbookData();
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
            created_by: 'Analyst'
        });
        const res = await fetch(`${API_BASE}/api/verification/start_session?${params}`, {
            method: 'POST'
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to start session');
        }
            const data = await res.json();
               currentSessionId = data.session_id;
        totalTransactions = data.total_transactions || 0;
        cashbookContent.style.display = 'none';
        sessionContent.style.display = 'block';
        // Remove padding from main container when verification session is active
        const container = document.getElementById('verificationContainer');
        if (container) container.classList.add('verification-active');
        if (typeof setNavStats === 'function') {
            setNavStats(`Session ${currentSessionId} · Cashbook: ${selectedCashbook} · 0 / ${totalTransactions} verified`);
        }
        // Load transaction list (without showing popover) to get indices and total
        await loadTransactionList(false);
        await loadNextTransaction();
        await updateProgress();
    } catch (error) {
        console.error('Error starting session:', error);
        showStatus('❌ Error starting verification: ' + error.message, 'error');
    } finally {
        startVerificationBtn.disabled = false;
        startVerificationBtn.textContent = '✅ Verification';
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
        await updateProgress();
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
// Renders the voucher-level chrome above the voucher lines list: a primary row with text
// Prev/Next voucher buttons flanking a centered title ("Voucher {id}") + subtitle
// (date · line count · Amount total · position counter), and a secondary slim toolbar row for
// Save Progress / Cashbook return / view-all. Matches the approved prototype layout — see
// C:\Users\LENOVO\.claude\plans\melodic-dazzling-feather.md. The per-line collapsed/expand rows
// (renderVoucherLines()) own everything else below this header.
function renderVoucherHeader(voucherId, voucherDate, lines) {
    const bar = document.getElementById('voucherHeaderBar');
    if (!bar) return;
    const lineCount = lines ? lines.length : 0;
    const total = computeVoucherTotal(lines);
    bar.innerHTML = `
        <div class="verification-header-row voucher-nav-row">
            <button class="btn-voucher-nav" id="prevArrow" ${currentTransactionIndex <= 0 ? 'disabled' : ''} title="Previous transaction">← Prev voucher</button>
            <div class="voucher-title-block">
                <div class="voucher-title">Voucher ${voucherId || '—'}</div>
                <div class="voucher-subtitle">${voucherDate || '—'} · ${lineCount} line${lineCount === 1 ? '' : 's'} · Amount ${total}<span id="voucherPositionCounter"></span></div>
            </div>
            <button class="btn-voucher-nav" id="nextArrow" ${currentTransactionIndex >= sessionTransactions.length - 1 ? 'disabled' : ''} title="Next transaction">Next voucher →</button>
        </div>
        <div class="verification-header-row voucher-toolbar-row">
            <span class="vh-icon" id="transactionListIcon" title="View all vouchers" style="cursor:pointer;">
                <span class="vh-icon-symbol">🔍</span>
            </span>
            <button class="btn-save-header" id="saveBtn"><span class="btn-icon">💾</span> Save Progress</button>
            <button class="btn-cashbook" id="cashbookReturnBtn"><span class="btn-icon">📒</span> Cashbook</button>
        </div>
    `;

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
    document.getElementById('saveBtn')?.addEventListener('click', function() {
        if (typeof saveAllVoucherLines === 'function') saveAllVoucherLines();
    });
    document.getElementById('cashbookReturnBtn')?.addEventListener('click', function() {
        if (typeof exitSession === 'function') exitSession();
    });
}

// Clear the voucher header bar (used when a session completes or exits).
function clearVoucherHeader() {
    const bar = document.getElementById('voucherHeaderBar');
    if (bar) bar.innerHTML = '';
}

// ---------- VOUCHER LINE ALIAS HELPERS (Phase 3) ----------
// Small, shared alias lookups reused by renderVoucherLines()/buildLineDetailHtml() — mirrors the
// alias arrays already used inside renderTransaction(), kept local to avoid touching that
// function (left untouched this phase per the confirmed plan).
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
// Renders a column-title row, one collapsed/expand row per line, and a total-line footer into
// #voucherLinesContainer. Segment 1 is not repeated here (its fields — account code/name/
// description/amount — are covered by the compact summary row); Segment 2/3 render inside each
// row's detail panel via buildLineDetailHtml(), independently per line.
function renderVoucherLines(lines) {
    const container = document.getElementById('voucherLinesContainer');
    if (!container) return;
    if (!lines || lines.length === 0) {
        container.innerHTML = '';
        return;
    }

    // Column-title row, matching the cashbook's own column names — aligned with the summary
    // row's grid via the shared .voucher-line-summary-grid class.
    let html = `
        <div class="voucher-line-header-row voucher-line-summary-grid">
            <span>Account Code</span>
            <span>Account Name</span>
            <span class="voucher-line-amount-col">Amount</span>
            <span>Status</span>
            <span></span>
        </div>
    `;

    lines.forEach(line => {
        const uid = line.detail_id;
        const row = line.row_data || {};
        const accountCode = getVoucherLineAlias(row, VOUCHER_LINE_ACCOUNT_CODE_ALIASES);
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
                    <span class="voucher-line-name-cell">
                        <span class="voucher-line-name">${accountName}${changeIconHtml}${deletedBadgeHtml}</span>
                        <span class="voucher-line-desc">${description}</span>
                    </span>
                    <span class="voucher-line-amount">${amount}</span>
                    <span class="voucher-line-status status-${validationStatus}" data-validation-status="${validationStatus}">${getVoucherLineStatusIcon(line.validation_status)} ${validationStatus}</span>
                    <span class="voucher-line-chevron">▾</span>
                </div>
                <div class="voucher-line-detail" id="voucherLineDetail-${uid}">
                    ${buildLineDetailHtml(line, uid)}
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
            <span class="voucher-line-name-cell">
                <span class="voucher-line-name">Total</span>
                <span class="voucher-line-desc">${cashbookName}</span>
            </span>
            <span class="voucher-line-amount">${total}</span>
            <span></span>
            <span></span>
        </div>
    `;

    container.innerHTML = html;

    // Sync each line's type dropdown with its seeded state.
    container.querySelectorAll('.voucher-line-type-dropdown').forEach(select => {
        const uid = select.dataset.uid;
        const state = voucherLineState[uid];
        if (state && state.transactionType) select.value = state.transactionType;
    });

    // Phase 4: attach each line's change-alert icon (only present for lines flagged 'updated').
    lines.forEach(line => {
        const uid = line.detail_id;
        setupVoucherChangeIcon(uid);
    });

    // ----- Event wiring (rebound after every innerHTML write, matching this file's convention) -----

    // Summary bar click toggles that row's expanded state — independent per line.
    container.querySelectorAll('.voucher-line-summary').forEach(bar => {
        bar.addEventListener('click', function() {
            const detailEl = document.getElementById(`voucherLineDetail-${this.dataset.detailId}`);
            const row = this.closest('.voucher-line-row');
            if (!detailEl || !row) return;
            const expanded = detailEl.classList.toggle('expanded');
            row.classList.toggle('expanded', expanded);
        });
    });

    // Transaction type dropdown, scoped per line.
    container.querySelectorAll('.voucher-line-type-dropdown').forEach(select => {
        select.addEventListener('click', function(e) { e.stopPropagation(); });
        select.addEventListener('change', function() {
            const uid = this.dataset.uid;
            const type = this.value;
            if (!voucherLineState[uid]) voucherLineState[uid] = { transactionType: '', procedureData: {} };
            voucherLineState[uid].transactionType = type;
            voucherLineState[uid].procedureData.transaction_type = type;
        });
    });

    // Audit Guidance button — opens a popup with the audit guidance content for this line's type.
    container.querySelectorAll('.btn-audit-guidance').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            const uid = this.dataset.uid;
            openAuditGuidancePopup(uid);
        });
    });

    // Supporting button — opens a popup with the five procedure cards for this line.
    container.querySelectorAll('.btn-supporting').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            const uid = this.dataset.uid;
            openSupportingPopup(uid);
        });
    });
}

// ---------- BUILD LINE DETAIL HTML (Phase 4) ----------
// Expanded panel: horizontal bar with Audit Guidance/Supporting buttons and the transaction type
// dropdown. The Particulars/Value table was removed — Date/Account Code/Account Name/Description
// are already shown in the line's own summary row (renderVoucherLines()), so it was a duplicate.
function buildLineDetailHtml(line, uid) {
    const state = voucherLineState[uid] || { transactionType: '', procedureData: {} };

    let html = '';

    // Deleted-state banner, shown inside this line's own expanded panel.
    if (line.change_detection_status === 'deleted') {
        html += `
        <div class="change-detection-banner" style="border-left-color:#dc2626; background:#fef2f2;">
            <span class="banner-icon">🗑️</span>
            <div class="banner-content">
                <div class="banner-title" style="color:#991b1b;">Transaction flagged as deleted</div>
                <div class="banner-subtitle" style="color:#b91c1c;">This transaction was present in previous uploads but is missing from the current re-upload. Awaiting auditor review.</div>
            </div>
        </div>
        `;
    }

    // Horizontal bar: Audit Guidance + Supporting buttons grouped on the left, transaction type
    // dropdown on the extreme right (two flex items so space-between splits them apart).
    html += `
        <div class="expanded-bar">
            <div class="expanded-bar-actions">
                <button class="btn-audit-guidance" data-uid="${uid}" title="View audit guidance for this transaction type">📋 Audit Guidance</button>
                <button class="btn-supporting" data-uid="${uid}" title="View and confirm supporting documents">📎 Supporting</button>
            </div>
            <div class="expanded-bar-type">
                <label class="vh-type-label">Type</label>
                <select class="vh-type-dropdown voucher-line-type-dropdown" data-uid="${uid}">
                    <option value="">-- Select --</option>
                    <option value="Material Purchased">Material Purchased</option>
                    <option value="Repair & Maintenance (In-house)">Repair & Maintenance (In-house)</option>
                    <option value="Fuel Expense">Fuel Expense</option>
                    <option value="Short Term Advance against Salary">Short Term Advance against Salary</option>
                    <option value="Payment to Contractors">Payment to Contractors</option>
                    <option value="Fixed Asset Purchased">Fixed Asset Purchased</option>
                    <option value="Kitchen Expenses">Kitchen Expenses</option>
                </select>
            </div>
        </div>
    `;
    return html;
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

function buildAuditGuidanceTable(data) {
    const toItems = (text) => (text || '')
        .split(';')
        .map(s => s.trim())
        .filter(Boolean)
        .map(s => `<li>${escapeHtml(s)}</li>`)
        .join('');
    return `
        <div class="audit-guidance-columns">
            <div class="audit-guidance-col">
                <div class="audit-guidance-col-header">Management Controls</div>
                <ul>${toItems(data.management_controls)}</ul>
            </div>
            <div class="audit-guidance-col">
                <div class="audit-guidance-col-header">Audit Risks</div>
                <ul>${toItems(data.audit_risks)}</ul>
            </div>
            <div class="audit-guidance-col">
                <div class="audit-guidance-col-header">Audit Procedures</div>
                <ul>${toItems(data.audit_procedures)}</ul>
            </div>
        </div>
    `;
}

// buildAuditGuidanceTable()/escapeHtml() are now consumed only by openAuditGuidancePopup().

// ---------- UPDATE PROGRESS ----------
// ---------- UPDATE PROGRESS (lightweight) ----------
async function updateProgress() {
    if (!currentSessionId) return;
    try {
        // Use the lightweight progress endpoint
        const res = await fetch(`${API_BASE}/api/verification/session/${currentSessionId}/progress`);
        if (!res.ok) throw new Error('Failed to fetch progress');
        const data = await res.json();
        if (typeof setNavStats === 'function') {
            // We need the cashbook name; we can store it globally or retrieve from session data
            // We'll store it in a variable when session starts.
            const cashbook = window._currentCashbook || '';
            setNavStats(`Session ${currentSessionId} · Cashbook: ${cashbook} · ${data.verified_count || 0} / ${data.total_transactions || 0} verified`);
        }
    } catch (e) {
        console.error('Error updating progress:', e);
    }
}

// Update the voucher position counter (small "· voucher n/total" suffix on the header subtitle,
// written by renderVoucherHeader()) and navbar stats. Cashbook name is already shown in the
// navbar stats below, so it's not repeated here.
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

    // Update navbar stats
    if (!currentSessionId) return;
    try {
        const res = await fetch(`${API_BASE}/api/verification/session/${currentSessionId}/progress`);
        if (!res.ok) throw new Error('Failed to fetch progress');
        const data = await res.json();
        const total = data.total_transactions || 0;
        const verified = data.verified_count || 0;
        if (typeof setNavStats === 'function') {
            const cashbook = window._currentCashbook || '';
            setNavStats(`Session ${currentSessionId} · Cashbook: ${cashbook} · ${verified} / ${total} verified`);
        }
    } catch (e) {
        console.error('Error updating progress:', e);
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

// ==============================================
// PHASE 2 – TRANSACTION LIST POPOVER
// ==============================================

let sessionTransactions = [];
let currentTransactionIndex = -1;

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

function renderTransactionPopover() {
    const popover = document.getElementById('transactionListPopover');
    const tbody = document.getElementById('transactionListBody');
    const totalSpan = document.getElementById('popoverTotal');
    if (!popover || !tbody) return;

    const count = sessionTransactions.length;

    if (count === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; color:#94a3b8;">No transactions found.</td></tr>`;
        if (totalSpan) totalSpan.textContent = '';
        popover.style.display = 'block';
        return;
    }

    // Show total number of transactions
    if (totalSpan) {
        totalSpan.textContent = `Total: ${count} transaction${count > 1 ? 's' : ''}`;
    }

    let html = '';
    sessionTransactions.forEach((t, index) => {
        const rowData = t.row_data || {};
        const accountName = rowData['Account Name'] || rowData['Account'] || rowData['Ledger'] || '—';
        const amount = t.extract_amount !== undefined && t.extract_amount !== null ? t.extract_amount : '—';
        const status = t.validation_status || 'Pending';
        let statusIcon = '';
        if (status === 'Passed') {
            statusIcon = '✅';
        } else if (status === 'Pending') {
            statusIcon = '⏳';
        } else {
            statusIcon = '❌';
        }
        const isCurrent = (t.detail_id === currentDetailId);
        html += `<tr data-detail-id="${t.detail_id}" style="${isCurrent ? 'background:#e0f2fe;' : ''}">
            <td style="text-align:center;">${index + 1}</td>
            <td>${t.voucher_id || '—'}</td>
            <td>${accountName}</td>
            <td style="text-align:right;">${amount}</td>
            <td style="text-align:center;"><span class="status-icon ${status.toLowerCase()}">${statusIcon}</span></td>
        </tr>`;
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

// Check if current transaction validation is Passed (or Verified)
function isCurrentTransactionValidated() {
    if (!sessionTransactions.length || currentTransactionIndex < 0) return false;
    const current = sessionTransactions[currentTransactionIndex];
    if (!current) return false;
    const status = (current.validation_status || '').toLowerCase();
    return status === 'passed' || status === 'verified';
}

// Go to previous transaction – with confirmation if not validated
async function goToPreviousTransaction() {
    if (!sessionTransactions.length || currentTransactionIndex <= 0) {
        // No message – silently ignore
        return;
    }
    // Save current state first (always)
    await saveCurrentTransaction();
    // If not validated, ask for confirmation
    if (!isCurrentTransactionValidated()) {
        const confirmed = await showNavigationConfirm();
        if (!confirmed) {
            // User cancelled – no message
            return;
        }
    }
    const prev = sessionTransactions[currentTransactionIndex - 1];
    if (prev) {
        navigateToTransaction(prev.detail_id);
    } else {
        // Silently ignore if not found
    }
}

// Go to next transaction – with confirmation if not validated
async function goToNextTransaction() {
    if (!sessionTransactions.length || currentTransactionIndex >= sessionTransactions.length - 1) {
        showStatus('You are at the last transaction.', 'info');
        return;
    }
    // Save current state
    await saveCurrentTransaction();
    // If not validated, ask for confirmation
    if (!isCurrentTransactionValidated()) {
        const confirmed = await showNavigationConfirm();
        if (!confirmed) {
            showStatus('Navigation cancelled.', 'info');
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
        const next = sessionTransactions[currentTransactionIndex + 1];
        if (next) {
            navigateToTransaction(next.detail_id);
        } else {
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
// globals, which remain #transactionCard's own separate concern (its Segment 2/3 duplicate of
// the active line, per Phase 3's decision — not reconciled with voucherLineState this phase).
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
// "Comments save per line ... called once per line instead of once per screen": saves every line
// currently loaded in the voucher via the existing save_line_items endpoint, one call per line,
// instead of a single call for just the active line. Wired to the Save Progress button and to the
// pre-navigation auto-save so edits on any expanded line (not just the active one) aren't lost.
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
        await updateProgress();
    } catch (e) {
        console.error('Error completing:', e);
        // Do not show status bar error – only log
    }
}

// ---------- EXIT SESSION ----------
function exitSession() {
    if (confirm('Are you sure you want to exit this verification session? Progress will be saved.')) {
        saveAllVoucherLines(); // Phase 4: saves every line, not just the active one
                sessionContent.style.display = 'none';
        cashbookContent.style.display = 'block';
        // Restore padding when session ends
        const container = document.getElementById('verificationContainer');
        if (container) container.classList.remove('verification-active');
        clearVoucherHeader();
        showStatus('Session exited. Returning to cashbook.', 'info');
        currentSessionId = null;
        currentDetailId = null;
        fetchCashbookData();
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

// ---------- EVENT LISTENERS ----------
const cashBtn = document.getElementById('cashVerificationBtn');
if (cashBtn) {
    cashBtn.addEventListener('click', function() {
        popupOverlay.style.display = 'flex';
        loadCashbooks();
    });
}

if (popupStartBtn) popupStartBtn.addEventListener('click', openCashbook);
if (popupCancelBtn) {
    popupCancelBtn.addEventListener('click', function() {
        popupOverlay.style.display = 'none';
    });
}

// Cashbook page events
if (cashbookFetchBtn) cashbookFetchBtn.addEventListener('click', fetchCashbookData);
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
if (exitCashbookLink) {
    exitCashbookLink.addEventListener('click', function(e) {
        e.preventDefault();
        cashbookContent.style.display = 'none';
        dashboardContent.style.display = 'block';
        selectedCashbook = null;
        cashbookTableWrapper.innerHTML = '';
    });
}

// Phase 2: standalone transaction view removed — prevBtn/nextBtn/verifyBtn/saveProgressBtn/exitBtn
// are no longer static DOM elements (nav controls are dynamically rendered in #voucherHeaderBar).
// Navigation event wiring is handled by renderVoucherHeader() which binds #prevArrow/#nextArrow/
// #saveBtn/#cashbookReturnBtn after each innerHTML write.

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

// ---------- Phase 4: Audit Guidance Popup ----------
// Opens a modal displaying audit guidance (management controls, risks, procedures) for
// the given line's transaction type. Relies on the same buildAuditGuidanceTable() / fetch
// logic that the removed AUDIT tab used, but rendered as a standalone popup triggered by
// the Audit Guidance button in the expanded bar.
async function openAuditGuidancePopup(uid) {
    const state = voucherLineState[uid];
    const type = state ? state.transactionType : '';
    // No transaction type picked yet for this line — do nothing (no toast). A clearer inline
    // signal near the button can be added later; the page-level warning toast looked out of
    // place here per user feedback.
    if (!type) return;
    // Reuse the procedure modal container, repurposing its content for audit guidance.
    // We swap the modal's title, hide the search/confirm UI, and insert guidance content.
    if (!procedureModal) return;
    if (procedureModalTitle) procedureModalTitle.textContent = '📋 Audit Guidance';
    // Hide search/confirm elements that belong to the procedure modal flow
    const searchRow = procedureModal.querySelector('.search-row');
    if (searchRow) searchRow.style.display = 'none';
    const confirmRow = procedureModal.querySelector('.modal-confirm-row');
    if (confirmRow) confirmRow.style.display = 'none';
    if (procedureStatus) procedureStatus.textContent = '';
    // Show loading
    if (procedureResults) procedureResults.innerHTML = '<div style="text-align:center; padding:20px;"><span class="spinner"></span> Loading audit guidance...</div>';
    procedureModal.style.display = 'flex';

    try {
        const res = await fetch(`${API_BASE}/api/verification/audit_guidance?transaction_type=${encodeURIComponent(type)}`);
        if (!voucherLineState[uid] || voucherLineState[uid].transactionType !== type) return;
        if (res.status === 404) {
            if (procedureResults) procedureResults.innerHTML = '<p style="color:#94a3b8; text-align:center; padding:30px;">No audit guidance available for this transaction type yet.</p>';
            return;
        }
        if (!res.ok) throw new Error('Failed to fetch audit guidance');
        const data = await res.json();
        if (procedureResults) procedureResults.innerHTML = buildAuditGuidanceTable(data);
    } catch (e) {
        if (!voucherLineState[uid] || voucherLineState[uid].transactionType !== type) return;
        console.error('Error fetching audit guidance:', e);
        if (procedureResults) procedureResults.innerHTML = '<p style="color:#dc2626; text-align:center; padding:30px;">Unable to load audit guidance.</p>';
    }
}

// ---------- Phase 4: Supporting Popup ----------
// Opens the procedure modal with all five supporting-document cards listed for the given
// line. The user can click any card to open the existing search/confirm flow (openProcedureModal)
// for that specific procedure. This replaces the removed Segment 3 procedure cards.
function openSupportingPopup(uid) {
    if (!procedureModal) return;
    currentProcedureTarget = uid;
    if (procedureModalTitle) procedureModalTitle.textContent = '📎 Supporting Documents';
    // Show search/confirm elements (they may have been hidden by audit guidance popup)
    const searchRow = procedureModal.querySelector('.search-row');
    if (searchRow) searchRow.style.display = '';
    const confirmRow = procedureModal.querySelector('.modal-confirm-row');
    if (confirmRow) confirmRow.style.display = '';
    if (procedureStatus) procedureStatus.textContent = '';

    // Render a list of five procedure cards, each clickable to open the search modal
    const procKeys = ['purchase_demand', 'purchase_order', 'gate_pass', 'store_receiving', 'vendor_bill'];
    const procLabels = {
        'purchase_demand': 'Purchase Demand',
        'purchase_order': 'Purchase Order',
        'gate_pass': 'Gate Pass',
        'store_receiving': 'Store Receiving',
        'vendor_bill': 'Vendor Bill'
    };
    const procIcons = {
        'purchase_demand': '📋',
        'purchase_order': '📄',
        'gate_pass': '🚪',
        'store_receiving': '📥',
        'vendor_bill': '🧾'
    };
    const templateMap = {
        'purchase_demand': 2,
        'purchase_order': 3,
        'gate_pass': 4,
        'store_receiving': 5,
        'vendor_bill': 6
    };

    // Build a compact grid of procedure cards
    let html = '<div style="display:flex; flex-wrap:wrap; gap:12px; padding:12px 0;">';
    procKeys.forEach(key => {
        const label = procLabels[key] || key;
        const icon = procIcons[key] || '📌';
        const templateId = templateMap[key] || 1;
        // Check if this line already has this procedure confirmed
        const state = voucherLineState[uid];
        const confirmed = state && state.procedureData && state.procedureData[key];
        html += `
            <div class="procedure-card-item supporting-proc-card" data-proc="${key}" data-template="${templateId}" data-uid="${uid}" style="flex:1 1 160px; max-width:200px; background:white; border:2px solid ${confirmed ? '#86efac' : '#e2e8f0'}; border-radius:12px; padding:16px; cursor:pointer; text-align:center; transition:all 0.2s;">
                <div style="font-size:32px; margin-bottom:6px;">${icon}</div>
                <div style="font-weight:600; font-size:14px; color:#1a1a2e;">${label}</div>
                ${confirmed ? '<div style="font-size:12px; color:#16a34a; margin-top:4px;">✅ Confirmed</div>' : '<div style="font-size:12px; color:#94a3b8; margin-top:4px;">Click to search</div>'}
            </div>
        `;
    });
    html += '</div>';
    // Also add a brief instruction
    html += '<p style="color:#64748b; font-size:13px; text-align:center; margin:8px 0 0 0;">Click a card to search for and confirm a supporting document.</p>';

    if (procedureResults) procedureResults.innerHTML = html;

    // Wire click listeners on the cards to open the procedure search modal
    procedureResults.querySelectorAll('.supporting-proc-card').forEach(card => {
        card.addEventListener('click', function() {
            const procKey = this.dataset.proc;
            const label = procLabels[procKey] || procKey;
            const templateId = parseInt(this.dataset.template);
            const lineUid = this.dataset.uid;
            // Close the supporting popup first, then open the procedure modal for this doc
            procedureModal.style.display = 'none';
            openProcedureModal(procKey, label, templateId, lineUid);
        });
    });

    procedureModal.style.display = 'flex';
}

// ==============================================
// AUDIT PROCEDURE POPOVER
// ==============================================

function openProcedureModal(procKey, procLabel, templateId, lineUid = null) {
    if (!procedureModal) return;
    currentProcedureKey = procKey;
    currentTemplateId = templateId;
    // Phase 3: when opened from a voucher line's collapsed/expand row, lineUid targets that
    // line's own voucherLineState instead of the global procedureData used by #transactionCard.
    currentProcedureTarget = lineUid;
    selectedDocId = null;
    selectedDocData = null;
    if (procedureModalTitle) procedureModalTitle.textContent = `📋 ${procLabel}`;
    if (procedureSearchInput) procedureSearchInput.value = '';
    if (procedureResults) procedureResults.innerHTML = '<div style="color:#94a3b8; text-align:center; padding:20px;">Enter a reference number and click Search.</div>';
    if (procedureStatus) procedureStatus.textContent = '';
    if (procedureConfirmBtn) procedureConfirmBtn.disabled = true;
    procedureModal.style.display = 'flex';
    setTimeout(() => {
        if (procedureSearchInput) procedureSearchInput.focus();
    }, 100);
}

function closeProcedureModal() {
    if (procedureModal) procedureModal.style.display = 'none';
    currentProcedureKey = null;
    currentTemplateId = null;
    currentProcedureTarget = null;
    selectedDocId = null;
    selectedDocData = null;
}

if (procedureModal) {
    procedureSearchBtn.addEventListener('click', async function() {
        if (!procedureSearchInput || !procedureResults || !procedureConfirmBtn) return;
        const searchTerm = procedureSearchInput.value.trim();
        if (!searchTerm) {
            procedureResults.innerHTML = '<div style="color:#f59e0b; text-align:center; padding:10px;">Please enter a reference number.</div>';
            return;
        }
        const clientId = getClientId();
        if (!clientId) {
            procedureResults.innerHTML = '<div style="color:#dc2626; text-align:center; padding:10px;">No client selected.</div>';
            return;
        }
        procedureResults.innerHTML = '<div style="text-align:center; padding:10px;"><span class="spinner"></span> Searching...</div>';
        procedureSearchBtn.disabled = true;
        try {
            const params = new URLSearchParams({
                client_id: clientId,
                search: searchTerm,
                template_id: currentTemplateId,
                limit: 20
            });
            const res = await fetch(`${API_BASE}/api/verification/supporting?${params}`);
            if (!res.ok) throw new Error('Failed to search');
            const data = await res.json();
            const docs = data.supporting_documents || [];
            if (docs.length === 0) {
                procedureResults.innerHTML = '<div style="color:#94a3b8; text-align:center; padding:10px;">No documents found with that reference.</div>';
                procedureConfirmBtn.disabled = true;
            } else {
                let html = '<div style="font-weight:600; font-size:13px; padding:4px 8px; background:#f8fafc; margin-bottom:6px;">Select a document:</div>';
                docs.forEach(doc => {
                    const row = doc.row_data || {};
                    const desc = row['Description'] || row['PO Number'] || row['GRN Number'] || row['Invoice Number'] || '';
                    const vendor = row['Vendor Name'] || row['Vendor'] || '—';
                    const unit = row['Unit'] || '—';
                    const qty = row['Qty'] || row['Quantity'] || '—';
                    const rate = row['Rate'] || '—';
                    const amount = doc.extract_amount || row['Amount'] || '—';
                    html += `
                        <div class="result-item" data-id="${doc.id}" style="padding:8px; border-bottom:1px solid #f0f2f5; cursor:pointer; display:flex; justify-content:space-between; align-items:center; transition:background 0.1s;">
                            <div style="flex:1;">
                                <div><strong>${desc}</strong></div>
                                <div style="font-size:12px; color:#64748b;">
                                    Vendor: ${vendor} | Unit: ${unit} | Qty: ${qty} | Rate: ${rate} | Amount: ${amount}
                                </div>
                            </div>
                            <button class="select-doc-btn" data-id="${doc.id}" data-row='${JSON.stringify(row)}' style="padding:4px 12px; background:#3b82f6; color:white; border:none; border-radius:4px; cursor:pointer; font-size:12px;">Select</button>
                        </div>
                    `;
                });
                procedureResults.innerHTML = html;
                procedureResults.querySelectorAll('.select-doc-btn').forEach(btn => {
                    btn.addEventListener('click', function(e) {
                        e.stopPropagation();
                        const id = this.dataset.id;
                        const rowData = JSON.parse(this.dataset.row);
                        selectedDocId = id;
                        selectedDocData = rowData;
                        procedureConfirmBtn.disabled = false;
                        procedureResults.querySelectorAll('.result-item').forEach(el => el.style.background = '');
                        this.closest('.result-item').style.background = '#e0f2fe';
                        if (procedureStatus) {
                            procedureStatus.textContent = '✅ Document selected. Click "Confirm Selection" to apply.';
                            procedureStatus.style.color = '#16a34a';
                        }
                    });
                });
                procedureResults.querySelectorAll('.result-item').forEach(el => {
                    el.addEventListener('click', function() {
                        const btn = this.querySelector('.select-doc-btn');
                        if (btn) btn.click();
                    });
                });
            }
        } catch (error) {
            console.error('Search error:', error);
            procedureResults.innerHTML = `<div style="color:#dc2626; text-align:center; padding:10px;">Error: ${error.message}</div>`;
        } finally {
            procedureSearchBtn.disabled = false;
        }
    });

    procedureConfirmBtn.addEventListener('click', function() {
        if (!selectedDocId || !currentProcedureKey) return;
        if (currentProcedureTarget) {
            // Phase 3: write into the target line's own state and refresh just its observation
            // text — no voucher reload, so other expanded lines/state are left untouched.
            const uid = currentProcedureTarget;
            if (!voucherLineState[uid]) voucherLineState[uid] = { transactionType: '', procedureData: {} };
            const target = voucherLineState[uid].procedureData;
            target[currentProcedureKey] = true;
            target[currentProcedureKey + '_ref'] = procedureSearchInput.value.trim();
            target[currentProcedureKey + '_doc_id'] = selectedDocId;
            target[currentProcedureKey + '_doc_data'] = selectedDocData;
            const placeholder = document.querySelector(`#voucherLineDetail-${uid} .proc-observation-placeholder[data-proc="${currentProcedureKey}"]`);
            if (placeholder) placeholder.textContent = procedureSearchInput.value.trim();
            closeProcedureModal();
            showStatus('✅ Procedure updated!', 'success');
            return;
        }
        procedureData[currentProcedureKey] = true;
        procedureData[currentProcedureKey + '_ref'] = procedureSearchInput.value.trim();
        procedureData[currentProcedureKey + '_doc_id'] = selectedDocId;
        procedureData[currentProcedureKey + '_doc_data'] = selectedDocData;
        closeProcedureModal();
        loadNextTransaction(currentDetailId);
        showStatus('✅ Procedure updated!', 'success');
    });

    procedureCancelBtn.addEventListener('click', closeProcedureModal);
    procedureModalClose.addEventListener('click', closeProcedureModal);
    procedureModal.addEventListener('click', function(e) {
        if (e.target === this) closeProcedureModal();
    });

    procedureSearchInput.addEventListener('keypress', function(e) {
        if (e.key === 'Enter') procedureSearchBtn.click();
    });
}

// ==============================================
// TRANSACTION REPORT VIEW
// ==============================================

function openTransactionReport() {
    try {
        if (!reportContent) return;
        if (dashboardContent) dashboardContent.style.display = 'none';
        if (sessionContent) sessionContent.style.display = 'none';
        if (cashbookContent) cashbookContent.style.display = 'none';
        reportContent.style.display = 'block';

        const now = new Date();
        const firstDay = new Date(now.getFullYear(), now.getMonth(), 1);
        const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0);
        const formatDate = (d) => {
            const y = d.getFullYear();
            const m = String(d.getMonth() + 1).padStart(2, '0');
            const day = String(d.getDate()).padStart(2, '0');
            return `${y}-${m}-${day}`;
        };
        if (reportStartDate) reportStartDate.value = formatDate(firstDay);
        if (reportEndDate) reportEndDate.value = formatDate(lastDay);

        const clientName = typeof window.getSelectedClientName === 'function'
            ? window.getSelectedClientName()
            : localStorage.getItem('selectedClientName') || 'Unknown Client';
        if (reportClientInfo) reportClientInfo.textContent = `Client: ${clientName}`;

        if (reportTableWrapper) {
            reportTableWrapper.innerHTML = '<div style="text-align:center; color:#94a3b8; padding:40px;">Select date range and click Fetch.</div>';
        }
        if (reportStatus) reportStatus.textContent = '';
        reportCurrentPage = 1;
        reportTotalRows = 0;
        updatePaginationControls();

        loadTemplateId();
    } catch (error) {
        console.error('Error opening transaction report:', error);
        if (reportStatus) {
            reportStatus.textContent = '❌ Error opening report: ' + error.message;
            reportStatus.style.color = '#dc2626';
        }
    }
}

async function loadTemplateId() {
    try {
        const res = await fetch(`${API_BASE}/api/templates/by-name/Transaction Report`);
        if (!res.ok) {
            if (res.status === 404) {
                throw new Error('Transaction Report template not found. Please restore it via Setup.');
            }
            throw new Error('Failed to fetch template details.');
        }
        const data = await res.json();
        reportTemplateId = data.id;
    } catch (error) {
        console.error('Error loading template:', error);
        if (reportStatus) {
            reportStatus.textContent = '❌ ' + error.message;
            reportStatus.style.color = '#dc2626';
        }
    }
}

async function fetchReportData() {
    if (!reportStartDate || !reportEndDate || !reportStatus || !reportFetchBtn || !reportTableWrapper) return;
    const start = reportStartDate.value;
    const end = reportEndDate.value;
    if (!start || !end) {
        reportStatus.textContent = '⚠️ Please select both dates.';
        reportStatus.style.color = '#f59e0b';
        return;
    }
    if (start > end) {
        reportStatus.textContent = '⚠️ Start must be ≤ end.';
        reportStatus.style.color = '#f59e0b';
        return;
    }

    const clientId = getClientId();
    if (!clientId) {
        reportStatus.textContent = '⚠️ No client selected.';
        reportStatus.style.color = '#dc2626';
        return;
    }

    if (!reportTemplateId) {
        reportStatus.textContent = '⏳ Loading template...';
        await loadTemplateId();
        if (!reportTemplateId) return;
    }

    reportStatus.textContent = '⏳ Loading... (large data may take a moment)';
    reportStatus.style.color = '#3b82f6';
    reportFetchBtn.disabled = true;
    reportTableWrapper.innerHTML = '<div style="text-align:center; padding:40px;"><div class="spinner"></div> Loading transactions...</div>';

    const params = new URLSearchParams({
        client_id: clientId,
        template_id: reportTemplateId,
        date_start: start,
        date_end: end,
        page: reportCurrentPage,
        limit: reportPageSize
    });

    try {
        const res = await fetch(`${API_BASE}/api/data/preview?${params}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch data');
        }
        const data = await res.json();
        reportTotalRows = data.total_rows || 0;
        const rows = data.rows || [];

        renderReportTable(rows, data);
        updatePaginationControls();

        if (rows.length === 0 && reportTotalRows === 0) {
            reportTableWrapper.innerHTML = '<div style="text-align:center; color:#94a3b8; padding:40px;">No transactions found for the selected period.</div>';
            if (reportRowCount) reportRowCount.textContent = '0 rows';
        } else {
            if (reportRowCount) reportRowCount.textContent = `${reportTotalRows} rows`;
        }
        reportStatus.textContent = `✅ ${reportTotalRows} rows`;
        reportStatus.style.color = '#16a34a';
    } catch (error) {
        console.error('Fetch error:', error);
        reportTableWrapper.innerHTML = `<div style="text-align:center; color:#dc2626; padding:40px;">❌ ${error.message}</div>`;
        reportStatus.textContent = '❌ Error';
        reportStatus.style.color = '#dc2626';
    } finally {
        reportFetchBtn.disabled = false;
    }
}

function renderReportTable(rows, data) {
    if (!reportTableWrapper) return;
    if (!rows || rows.length === 0) {
        reportTableWrapper.innerHTML = '<div style="text-align:center; color:#94a3b8; padding:40px;">No data to display.</div>';
        return;
    }

    const firstRow = rows[0].row_data;
    const headers = firstRow ? Object.keys(firstRow) : [];

    let html = `<table style="width:100%; border-collapse:collapse; font-size:13px;">
        <thead style="background:#f8fafc; position:sticky; top:0; z-index:1;">
            <tr>`;
    headers.forEach(h => {
        html += `<th style="padding:8px 12px; text-align:left; border-bottom:2px solid #e2e8f0; font-weight:600; white-space:nowrap;">${h}</th>`;
    });
    html += `</tr></thead><tbody>`;

    rows.forEach(row => {
        html += `<tr style="border-bottom:1px solid #f0f2f5;">`;
        headers.forEach(h => {
            const val = row.row_data[h] !== undefined ? row.row_data[h] : '';
            html += `<td style="padding:6px 12px;">${val}</td>`;
        });
        html += `</tr>`;
    });
    html += `</tbody></table>`;

    reportTableWrapper.innerHTML = html;
}

function updatePaginationControls() {
    const totalPages = Math.ceil(reportTotalRows / reportPageSize) || 1;
    if (reportPageInfo) reportPageInfo.textContent = `Page ${reportCurrentPage} of ${totalPages}`;
    if (reportPrevPage) reportPrevPage.disabled = reportCurrentPage <= 1;
    if (reportNextPage) reportNextPage.disabled = reportCurrentPage >= totalPages;
}

if (reportFetchBtn) {
    reportFetchBtn.addEventListener('click', function() {
        fetchReportData();
    });
}
if (reportPageSizeSelect) {
    reportPageSizeSelect.addEventListener('change', function() {
        reportPageSize = parseInt(this.value);
        reportCurrentPage = 1;
        fetchReportData();
    });
}
if (reportPrevPage) {
    reportPrevPage.addEventListener('click', function() {
        if (reportCurrentPage > 1) {
            reportCurrentPage--;
            fetchReportData();
        }
    });
}
if (reportNextPage) {
    reportNextPage.addEventListener('click', function() {
        const totalPages = Math.ceil(reportTotalRows / reportPageSize);
        if (reportCurrentPage < totalPages) {
            reportCurrentPage++;
            fetchReportData();
        }
    });
}
if (exitReportLink) {
    exitReportLink.addEventListener('click', function(e) {
        e.preventDefault();
        if (reportContent) reportContent.style.display = 'none';
        if (dashboardContent) dashboardContent.style.display = 'block';
        if (sessionContent) sessionContent.style.display = 'none';
        if (cashbookContent) cashbookContent.style.display = 'none';
        if (reportTableWrapper) reportTableWrapper.innerHTML = '';
        if (reportStatus) reportStatus.textContent = '';
    });
}
if (reportStartDate) {
    reportStartDate.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') fetchReportData();
    });
}
if (reportEndDate) {
    reportEndDate.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') fetchReportData();
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
    if (typeof setNavStats === 'function') {
        setNavStats('');
    }
    loadVerificationStats();
};

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