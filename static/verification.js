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
let selectedCashbook = null;
let cashbookCurrentPage = 1;
let cashbookPageSize = 50;
let cashbookTotalRows = 0;

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
const transactionCard = document.getElementById('transactionCard');
const prevBtn = document.getElementById('prevBtn');
const nextBtn = document.getElementById('nextBtn');
const saveProgressBtn = document.getElementById('saveProgressBtn');
const verifyBtn = document.getElementById('verifyBtn');
const exitBtn = document.getElementById('exitBtn');
const exitSessionLink = document.getElementById('exitSessionLink');

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
        cashbookContent.style.display = 'none';
        sessionContent.style.display = 'block';
        // Remove padding from main container when verification session is active
        const container = document.getElementById('verificationContainer');
        if (container) container.classList.add('verification-active');
        if (typeof setNavStats === 'function') {
            setNavStats(`Session ${currentSessionId} · Cashbook: ${selectedCashbook} · 0 / ${data.total_transactions || 0} verified`);
        }
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
                if (transactionCard) transactionCard.innerHTML = `<div style="padding:30px; text-align:center; color:#16a34a; font-size:18px;">🎉 All transactions verified!</div>`;
                if (prevBtn) prevBtn.disabled = true;
                if (nextBtn) nextBtn.disabled = true;
                if (verifyBtn) verifyBtn.disabled = true;
                // Update progress display to show completion
                await updateProgressDisplay();
                return;
            }
            throw new Error(err.detail || 'Failed to load transaction');
        }
        const data = await res.json();
        if (data.completed) {
            showStatus('✅ All transactions have been verified!', 'success');
            if (transactionCard) transactionCard.innerHTML = `<div style="padding:30px; text-align:center; color:#16a34a; font-size:18px;">🎉 All transactions verified!</div>`;
            if (prevBtn) prevBtn.disabled = true;
            if (nextBtn) nextBtn.disabled = true;
            if (verifyBtn) verifyBtn.disabled = true;
            await updateProgressDisplay();
            return;
        }
        currentDetailId = data.detail_id;
        currentClientDataId = data.client_data_id;
        currentVoucherId = data.voucher_id;
        currentTransactionType = data.transaction_type || '';
        lineItems = data.line_items || [];
        procedureData = data.procedure_data || {};
        renderTransaction(data);
        await updateProgress();
        await updateProgressDisplay(); // update the new progress element
        if (prevBtn) prevBtn.disabled = false;
        if (nextBtn) nextBtn.disabled = false;
        if (verifyBtn) verifyBtn.disabled = false;
        // Removed "Loaded transaction" message
    } catch (e) {
        console.error('Error loading transaction:', e);
        showStatus('Error loading transaction: ' + e.message, 'error');
    } finally {
        isNavigating = false;
    }
}

// ---------- RENDER TRANSACTION ----------
function renderTransaction(data) {
    if (!transactionCard) return;
    const row = data.row_data || {};

    // ----- Aliases for flexible column name matching (unchanged) -----
    const dateAliases = ['Date', 'Transaction Date', 'Invoice Date', 'Doc Date', 'Posting Date', 'Entry Date', 'Date'];
    const getDateValue = () => {
        for (let alias of dateAliases) {
            if (row[alias] !== undefined && row[alias] !== null && row[alias] !== '') return row[alias];
        }
        return data.extract_date || '—';
    };
    const accountCodeAliases = ['Account Code', 'Account No', 'Account Number', 'Code', 'Ledger Code', 'AccountID', 'Account Head'];
    const getAccountHead = () => {
        for (let alias of accountCodeAliases) {
            if (row[alias] !== undefined && row[alias] !== null && row[alias] !== '') return row[alias];
        }
        return '—';
    };
    const accountNameAliases = ['Account Name', 'Account', 'Account Detail', 'Ledger', 'Head'];
    const getAccountName = () => {
        for (let alias of accountNameAliases) {
            if (row[alias] !== undefined && row[alias] !== null && row[alias] !== '') return row[alias];
        }
        return '—';
    };
    const amountAliases = ['Amount', 'Total', 'Net Amount', 'Gross Amount', 'Transaction Amount', 'Value', 'Debit', 'Credit'];
    const getAmount = () => {
        for (let alias of amountAliases) {
            if (row[alias] !== undefined && row[alias] !== null && row[alias] !== '') {
                const val = parseFloat(row[alias]);
                if (!isNaN(val)) return val;
                return row[alias];
            }
        }
        return data.extract_amount || '—';
    };
    const descriptionAliases = ['Description', 'Voucher Detail', 'Narration', 'Remarks', 'Particulars'];
    const getDescription = () => {
        for (let alias of descriptionAliases) {
            if (row[alias] !== undefined && row[alias] !== null && row[alias] !== '') return row[alias];
        }
        return '—';
    };

    // ----- Prepare values for display -----
    const voucherId = data.voucher_id || '—';
    const dateVal = getDateValue();
    const accountHead = getAccountHead();
    const accountName = getAccountName();
    const description = getDescription();
    const amount = getAmount();

    // ----- Build the new layout (validation status moved up) -----
    let html = `
        <!-- Progress Indicator -->
        <div style="display:flex; align-items:center; gap:12px; margin-bottom:8px; flex-wrap:wrap;">
            <span class="progress-indicator" id="progressDisplay">Loading progress...</span>
        </div>

                <!-- Header row: TRANSACTION | mirror icon | Type dropdown -->
        <div class="verification-header-row" style="border-bottom: none; padding: 10px 20px 0 0;">
            <div class="vh-center" style="justify-content: flex-start; gap: 8px;">
                <span class="vh-title">TRANSACTION</span>
                <span class="vh-icon" id="transactionListIcon" title="View all transactions" style="cursor:pointer;">🔍</span>
                <span class="vh-separator">|</span>
                <span class="vh-type-label">Type</span>
                <select id="transactionTypeDropdown" class="vh-type-dropdown">
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
            <div class="vh-right">
                <button class="btn-cashbook" id="cashbookReturnBtn" style="font-size:13px;">📒 Cashbook</button>
            </div>
        </div>
        <hr class="header-divider">

        <!-- Transaction Details Table – wrapped in a sub‑container to shrink‑wrap -->
        <div class="transaction-details-wrapper">
            <table class="transaction-details-table">
                <tbody>
                    <tr class="voucher-row">
                        <td class="label">Voucher ID</td>
                        <td class="value">${voucherId}</td>
                    </tr>
                    <tr>
                        <td class="label">Date</td>
                        <td class="value">${dateVal}</td>
                    </tr>
                    <tr>
                        <td class="label">Account Code</td>
                        <td class="value">${accountHead}</td>
                    </tr>
                    <tr>
                        <td class="label">Account Name</td>
                        <td class="value">${accountName}</td>
                    </tr>
                    <tr>
                        <td class="label">Voucher Description</td>
                        <td class="value">${description}</td>
                    </tr>
                    <tr class="amount-row">
                        <td class="label">Amount</td>
                        <td class="value">${amount}</td>
                    </tr>
                </tbody>
            </table>
        </div>

        <!-- Validation Status Row (now placed directly below the transaction table) -->
        <div class="validation-status-row">
            <span class="label">Validation Status:</span>
            <span class="status-badge ${(data.validation_status || 'Pending').toLowerCase()}">${data.validation_status || 'Pending'}</span>
            ${data.validation_message ? `<span class="validation-message">${data.validation_message}</span>` : ''}
        </div>

                                <!-- Mandatory Procedures Tiles (placed below the transaction table) -->
        <div style="margin-top:16px;">
            <div style="display:flex; align-items:center; gap:8px;">
                <span class="vh-title" style="font-size:20px; font-weight:700; color:#1a1a2e;">PROCEDURES</span>
            </div>
            <hr class="header-divider">
            <div class="procedure-tiles" id="procedureTilesContainer">
    `;

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

    procKeys.forEach(key => {
        const isChecked = procedureData[key] === true;
        const icon = procIcons[key] || '📌';
        const label = procLabels[key] || key;
        html += `
                    <div class="procedure-tile clickable-proc" data-proc="${key}" style="${isChecked ? 'background:#dcfce7; border-color:#86efac;' : ''}">
                        <span class="proc-icon">${icon}</span>
                        <span class="proc-name">${label}</span>
                        <input type="checkbox" class="proc-checkbox" ${isChecked ? 'checked' : ''} title="Mark as done">
                    </div>
                `;
    });

    html += `
            </div>
        </div>

        <!-- Observations & Comments -->
        <div class="obs-comments-row">
            <div class="obs-wrapper">
                <label for="observationsInput">Observations</label>
                <textarea id="observationsInput" rows="2">${procedureData.observations || ''}</textarea>
                <div class="obs-dropdown-container">
                    <select id="observationsDropdown">
                        <option value="">-- Select frequent --</option>
                    </select>
                    <button class="btn-edit-obs" id="editObsBtn" title="Manage frequent observations">⚙️</button>
                </div>
            </div>
            <div class="comments-wrapper">
                <label for="commentsInput">Comments</label>
                <textarea id="commentsInput" rows="2">${procedureData.comments || ''}</textarea>
            </div>
        </div>

        <!-- Action Buttons -->
        <div class="action-buttons-row">
            <button class="btn-action btn-back" id="backPrevBtn">← Back</button>
            <button class="btn-action btn-next" id="nextSaveBtn">Next →</button>
        </div>
    `;

    transactionCard.innerHTML = html;

    // ----- Attach event listeners (dynamic) -----

    // Mirror icon – placeholder for Phase 2
    document.getElementById('transactionListIcon')?.addEventListener('click', function() {
        alert('Transaction list popover – coming soon in Phase 2');
    });

    // Cashbook return button
    document.getElementById('cashbookReturnBtn')?.addEventListener('click', function() {
        if (typeof exitSession === 'function') {
            exitSession();
        } else {
            console.warn('exitSession not defined');
        }
    });

    // Procedure tiles – click to open supporting document modal
    document.querySelectorAll('.clickable-proc').forEach(el => {
        el.addEventListener('click', function(e) {
            const procKey = this.dataset.proc;
            const labels = {
                'purchase_demand': 'Purchase Demand',
                'purchase_order': 'Purchase Order',
                'gate_pass': 'Gate Pass',
                'store_receiving': 'Store Receiving',
                'vendor_bill': 'Vendor Bill'
            };
            const templateMap = {
                'purchase_demand': 2,
                'purchase_order': 3,
                'gate_pass': 4,
                'store_receiving': 5,
                'vendor_bill': 6
            };
            const procLabel = labels[procKey] || procKey;
            const templateId = templateMap[procKey] || 1;
            if (typeof openProcedureModal === 'function') {
                openProcedureModal(procKey, procLabel, templateId);
            } else {
                console.warn('⚠️ openProcedureModal not defined yet');
            }
        });
    });

    // Procedure checkbox change – update procedureData and tile style
    document.querySelectorAll('.proc-checkbox').forEach(cb => {
        cb.addEventListener('change', function() {
            const tile = this.closest('.procedure-tile');
            if (tile) {
                const procKey = tile.dataset.proc;
                procedureData[procKey] = this.checked;
                if (this.checked) {
                    tile.style.background = '#dcfce7';
                    tile.style.borderColor = '#86efac';
                } else {
                    tile.style.background = '#f8fafc';
                    tile.style.borderColor = '#e2e8f0';
                }
            }
        });
    });

    // Observations dropdown – populate with saved list
    populateObservationsDropdown();
    document.getElementById('observationsDropdown')?.addEventListener('change', function() {
        const val = this.value;
        if (val) {
            document.getElementById('observationsInput').value = val;
            procedureData.observations = val;
        }
    });
    document.getElementById('editObsBtn')?.addEventListener('click', function() {
        alert('Manage frequent observations – to be implemented in Phase 4');
    });

    // Observations textarea – update data
    document.getElementById('observationsInput')?.addEventListener('input', function() {
        procedureData.observations = this.value;
    });
    document.getElementById('commentsInput')?.addEventListener('input', function() {
        procedureData.comments = this.value;
    });

    // Back button – placeholder for Phase 3
    document.getElementById('backPrevBtn')?.addEventListener('click', function() {
        console.log('Back to previous transaction (Phase 3)');
        showStatus('Back to previous transaction – coming soon', 'info');
    });

    // Next button – triggers validation and save (existing logic)
    document.getElementById('nextSaveBtn')?.addEventListener('click', function() {
        if (typeof completeTransaction === 'function') {
            completeTransaction();
        } else {
            console.warn('completeTransaction not defined');
        }
    });

    // Sync header dropdown with current type if any
    const headerDropdown = document.getElementById('transactionTypeDropdown');
    if (headerDropdown && currentTransactionType) {
        headerDropdown.value = currentTransactionType;
    }
    headerDropdown?.addEventListener('change', function() {
        currentTransactionType = this.value;
        procedureData.transaction_type = currentTransactionType;
    });

    // Update progress display
    updateProgressDisplay();
}

// Helper: populate observations dropdown from localStorage
function populateObservationsDropdown() {
    const dropdown = document.getElementById('observationsDropdown');
    if (!dropdown) return;
    let obsList = JSON.parse(localStorage.getItem('frequentObservations') || '[]');
    dropdown.innerHTML = '<option value="">-- Select --</option>';
    obsList.forEach(text => {
        const opt = document.createElement('option');
        opt.value = text;
        opt.textContent = text;
        dropdown.appendChild(opt);
    });
}

// Helper: populate observations dropdown from localStorage
function populateObservationsDropdown() {
    const dropdown = document.getElementById('observationsDropdown');
    if (!dropdown) return;
    let obsList = JSON.parse(localStorage.getItem('frequentObservations') || '[]');
    dropdown.innerHTML = '<option value="">-- Select --</option>';
    obsList.forEach(text => {
        const opt = document.createElement('option');
        opt.value = text;
        opt.textContent = text;
        dropdown.appendChild(opt);
    });
}

// ---------- RENDER LINE ITEMS ----------
function renderLineItems() {
    const tbody = document.getElementById('lineItemsBody');
    if (!tbody) return;
    tbody.innerHTML = '';
    if (!lineItems || lineItems.length === 0) {
        lineItems = [{ vendor: '', unit: '', qty: '', rate: '', calculated_amount: '' }];
    }
    lineItems.forEach((item, index) => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><input type="text" class="li-vendor" value="${item.vendor || ''}" data-index="${index}"></td>
            <td><input type="text" class="li-unit" value="${item.unit || ''}" data-index="${index}"></td>
            <td><input type="number" class="li-qty" value="${item.qty || ''}" step="any" data-index="${index}"></td>
            <td><input type="number" class="li-rate" value="${item.rate || ''}" step="any" data-index="${index}"></td>
            <td><input type="number" class="li-calc" value="${item.calculated_amount || ''}" step="any" data-index="${index}"></td>
            <td><button class="remove-btn" data-index="${index}">✕</button></td>
        `;
        tbody.appendChild(tr);
    });
    tbody.querySelectorAll('.remove-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const idx = parseInt(this.dataset.index);
            if (lineItems.length <= 1) {
                alert('At least one line item is required.');
                return;
            }
            lineItems.splice(idx, 1);
            renderLineItems();
        });
    });
    tbody.querySelectorAll('.li-qty, .li-rate').forEach(input => {
        input.addEventListener('input', function() {
            const idx = parseInt(this.dataset.index);
            const row = tbody.children[idx];
            const qty = parseFloat(row.querySelector('.li-qty').value) || 0;
            const rate = parseFloat(row.querySelector('.li-rate').value) || 0;
            const calc = qty * rate;
            row.querySelector('.li-calc').value = calc.toFixed(2);
            lineItems[idx].qty = qty;
            lineItems[idx].rate = rate;
            lineItems[idx].calculated_amount = calc;
        });
    });
    tbody.querySelectorAll('.li-vendor').forEach(inp => {
        inp.addEventListener('input', function() {
            const idx = parseInt(this.dataset.index);
            lineItems[idx].vendor = this.value;
        });
    });
    tbody.querySelectorAll('.li-unit').forEach(inp => {
        inp.addEventListener('input', function() {
            const idx = parseInt(this.dataset.index);
            lineItems[idx].unit = this.value;
        });
    });
    tbody.querySelectorAll('.li-calc').forEach(inp => {
        inp.addEventListener('input', function() {
            const idx = parseInt(this.dataset.index);
            lineItems[idx].calculated_amount = parseFloat(this.value) || 0;
        });
    });
}

function addLineItemRow() {
    lineItems.push({ vendor: '', unit: '', qty: '', rate: '', calculated_amount: '' });
    renderLineItems();
    const tbody = document.getElementById('lineItemsBody');
    if (tbody) tbody.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

// ---------- FETCH LIBRARY ----------
async function fetchLibrary(type) {
    const libraryData = {
        'Material Purchased': {
            controls: 'Authorized Purchase demand; Approved Purchase order; Signed Gate pass; Inspection note; Store receiving; Vendor bill',
            risks: 'Unauthorized purchases; Inflated quantities; Unapproved vendors; Missing GRN',
            procedures: 'Verify purchase demand authorization; Check PO approval; Inspect gate pass; Review inspection note; Match GRN with PO; Compare vendor bill with PO/GRN; Reconcile quantities and amounts'
        },
        'Repair & Maintenance (In-house)': {
            controls: 'Authorized Purchase demand; Signature of relevant supervisor on bill; Vendor bill',
            risks: 'Unapproved repairs; Overcharging; Duplicate bills',
            procedures: 'Check purchase demand authorization; Verify supervisor signature on bill; Review vendor bill for reasonableness; Compare with maintenance log if available'
        },
        'Fuel Expense': {
            controls: 'Vehicle log; Fuel receipts; Mileage records',
            risks: 'Unexplained fuel consumption; Theft; Unauthorized vehicles',
            procedures: 'Reconcile fuel receipts with vehicle log; Check mileage against fuel consumption; Verify authorized vehicles'
        },
        'Short Term Advance against Salary': {
            controls: 'Approved advance request; Employee acknowledgment; Payroll deduction schedule',
            risks: 'Unauthorized advances; Non-recovery; Duplicate advances',
            procedures: 'Verify approved advance request; Check employee acknowledgment; Review deduction schedule; Confirm with payroll records'
        },
        'Payment to Contractors': {
            controls: 'Signed contract; Work completion certificate; Tax deduction certificate; Vendor bill',
            risks: 'Fake contractors; Unauthorized work; Overpayment; Non-deduction of tax',
            procedures: 'Review contract; Inspect work completion certificate; Verify tax deduction; Match vendor bill with contract'
        },
        'Fixed Asset Purchased': {
            controls: 'Capital budget approval; Purchase order; Gate pass; Inspection note; Asset tagging',
            risks: 'Unauthorized purchase; Misclassification as expense; Incorrect valuation; Missing asset',
            procedures: 'Check capital budget approval; Verify PO; Inspect gate pass; Review inspection note; Ensure asset tagged and entered in fixed asset register'
        },
        'Kitchen Expenses': {
            controls: 'Daily consumption report; Purchase invoices; Inventory count',
            risks: 'Theft; Over-purchasing; Unauthorized items; Missing invoices',
            procedures: 'Reconcile purchase invoices with consumption report; Verify inventory count; Check for approved purchase; Review daily consumption logs'
        }
    };
    const data = libraryData[type];
    const content = document.getElementById('libraryContent');
    if (!data) {
        content.textContent = 'No guidance available for this transaction type.';
    } else {
        content.innerHTML = `
            <strong>Management Controls:</strong> ${data.controls}<br>
            <strong>Audit Risks:</strong> ${data.risks}<br>
            <strong>Audit Procedures:</strong> ${data.procedures}
        `;
    }
    document.getElementById('libraryPanel').style.display = 'block';
}

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

// Update the progress indicator in the transaction card
async function updateProgressDisplay() {
    const progressEl = document.getElementById('progressDisplay');
    if (!progressEl) return;
    if (!currentSessionId) {
        progressEl.textContent = 'No session active';
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/api/verification/session/${currentSessionId}/progress`);
        if (!res.ok) throw new Error('Failed to fetch progress');
        const data = await res.json();
        const total = data.total_transactions || 0;
        const verified = data.verified_count || 0;
        // Also update the navbar stats if function exists
        if (typeof setNavStats === 'function') {
            const cashbook = window._currentCashbook || '';
            setNavStats(`Session ${currentSessionId} · Cashbook: ${cashbook} · ${verified} / ${total} verified`);
        }
        // Update the progress element
        progressEl.textContent = `Transaction ${verified + 1} of ${total}`;
        if (verified === total && total > 0) {
            progressEl.textContent = `✅ All ${total} transactions verified!`;
        }
    } catch (e) {
        console.error('Error updating progress display:', e);
        progressEl.textContent = 'Progress unavailable';
    }
}

// ---------- SAVE LINE ITEMS ----------
async function saveLineItems() {
    if (!currentDetailId) return;
    const tbody = document.getElementById('lineItemsBody');
    if (tbody) {
        const rows = tbody.querySelectorAll('tr');
        lineItems = [];
        rows.forEach(row => {
            const vendor = row.querySelector('.li-vendor')?.value || '';
            const unit = row.querySelector('.li-unit')?.value || '';
            const qty = parseFloat(row.querySelector('.li-qty')?.value) || 0;
            const rate = parseFloat(row.querySelector('.li-rate')?.value) || 0;
            const calc = parseFloat(row.querySelector('.li-calc')?.value) || 0;
            lineItems.push({ vendor, unit, qty, rate, calculated_amount: calc });
        });
    }
    const procKeys = ['purchase_demand', 'purchase_order', 'gate_pass', 'store_receiving', 'vendor_bill'];
    procKeys.forEach(key => {
        const cb = document.getElementById(`proc_${key}`);
        if (cb) procedureData[key] = cb.checked;
        const refInput = document.querySelector(`input[data-proc="${key}"]`);
        if (refInput) procedureData[key + '_ref'] = refInput.value;
    });
    const obs = document.getElementById('observationsInput');
    const comm = document.getElementById('commentsInput');
    if (obs) procedureData.observations = obs.value;
    if (comm) procedureData.comments = comm.value;
    const typeSelect = document.getElementById('transactionTypeSelect');
    if (typeSelect) {
        currentTransactionType = typeSelect.value;
        procedureData.transaction_type = currentTransactionType;
    }

    try {
        const payload = {
            detail_id: currentDetailId,
            line_items: lineItems,
            transaction_type: currentTransactionType,
            procedure_data: procedureData
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
        showStatus('✅ Progress saved.', 'success');
    } catch (e) {
        console.error('Error saving:', e);
        showStatus('❌ Error saving: ' + e.message, 'error');
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
        showStatus(data.validation_message, data.passed ? 'success' : 'error');
        const badge = document.querySelector('.status-badge');
        if (badge) {
            badge.textContent = data.validation_status;
            badge.className = `status-badge ${data.validation_status.toLowerCase()}`;
        }
        if (data.passed) {
            if (verifyBtn) verifyBtn.disabled = false;
        } else {
            if (verifyBtn) verifyBtn.disabled = true;
        }
    } catch (e) {
        console.error('Validation error:', e);
        showStatus('❌ Validation error: ' + e.message, 'error');
    }
}

// ---------- COMPLETE TRANSACTION ----------
async function completeTransaction() {
    if (!currentDetailId) return;
    await saveLineItems();
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
        showStatus('✅ Transaction verified!', 'success');
        await loadNextTransaction();
        await updateProgress();
    } catch (e) {
        console.error('Error completing:', e);
        showStatus('❌ Error completing: ' + e.message, 'error');
    }
}

// ---------- NAVIGATION ----------
async function navigate(direction) {
    if (isNavigating) return;
    await saveLineItems();
    if (direction === 'next') {
        await loadNextTransaction();
    } else {
        showStatus('Previous not implemented yet.', 'info');
    }
}

// ---------- EXIT SESSION ----------
function exitSession() {
    if (confirm('Are you sure you want to exit this verification session? Progress will be saved.')) {
        saveLineItems();
                sessionContent.style.display = 'none';
        cashbookContent.style.display = 'block';
        // Restore padding when session ends
        const container = document.getElementById('verificationContainer');
        if (container) container.classList.remove('verification-active');
        if (transactionCard) transactionCard.innerHTML = '';
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
        console.log('💰 Cash Verification button clicked');
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

// Session navigation
if (prevBtn) prevBtn.addEventListener('click', () => navigate('prev'));
if (nextBtn) nextBtn.addEventListener('click', () => navigate('next'));
if (saveProgressBtn) saveProgressBtn.addEventListener('click', saveLineItems);
if (verifyBtn) verifyBtn.addEventListener('click', completeTransaction);
if (exitBtn) exitBtn.addEventListener('click', exitSession);
if (exitSessionLink) {
    exitSessionLink.addEventListener('click', function(e) {
        e.preventDefault();
        exitSession();
    });
}

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

// ==============================================
// AUDIT PROCEDURE POPOVER
// ==============================================

function openProcedureModal(procKey, procLabel, templateId) {
    if (!procedureModal) return;
    currentProcedureKey = procKey;
    currentTemplateId = templateId;
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
    console.log('📊 Transaction Report button clicked');
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
        console.log('✅ Transaction Report template ID loaded:', reportTemplateId);
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
        console.log('📊 Fetch button clicked');
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
    console.log('✅ Verification module loaded (cashbook + verification flow)');
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
        console.log('⚠️ Error loading verification stats:', error.message);
        document.getElementById('pendingCount').textContent = '0';
        document.getElementById('verifiedToday').textContent = '0';
        document.getElementById('totalVerified').textContent = '0';
    }
}