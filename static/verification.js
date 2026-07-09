// ==============================================
// ✅ VERIFICATION MODULE – RESTORED DASHBOARD + POPUP
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

// DOM refs
const dashboardContent = document.getElementById('dashboardContent');
const sessionContent = document.getElementById('verificationSessionContent');
const popupOverlay = document.getElementById('sessionPopupOverlay');
const popupCashbookSelect = document.getElementById('popupCashbookSelect');
const popupPeriodStart = document.getElementById('popupPeriodStart');
const popupPeriodEnd = document.getElementById('popupPeriodEnd');
const popupStartBtn = document.getElementById('popupStartBtn');
const popupCancelBtn = document.getElementById('popupCancelBtn');
const popupError = document.getElementById('popupError');

const sessionIdDisplay = document.getElementById('sessionIdDisplay');
const cashbookDisplay = document.getElementById('cashbookDisplay');
const periodDisplay = document.getElementById('periodDisplay');
const progressDisplay = document.getElementById('progressDisplay');
const statusDiv = document.getElementById('verificationStatus');
const transactionCard = document.getElementById('transactionCard');
const prevBtn = document.getElementById('prevBtn');
const nextBtn = document.getElementById('nextBtn');
const saveProgressBtn = document.getElementById('saveProgressBtn');
const verifyBtn = document.getElementById('verifyBtn');
const exitBtn = document.getElementById('exitBtn');
const exitSessionLink = document.getElementById('exitSessionLink');

const supportingModal = document.getElementById('supportingModal');
const supportingSearchInput = document.getElementById('supportingSearchInput');
const supportingSearchBtn = document.getElementById('supportingSearchBtn');
const supportingResults = document.getElementById('supportingResults');
const supportingModalClose = document.getElementById('supportingModalClose');

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

function showPopupError(msg) {
    popupError.textContent = msg;
    popupError.style.display = 'block';
}

function hidePopupError() {
    popupError.style.display = 'none';
}

function getClientId() {
    if (typeof window.getSelectedClientId === 'function') {
        return window.getSelectedClientId();
    }
    return localStorage.getItem('selectedClientId');
}

// ---------- POPUP: LOAD CASHBOOKS ----------
async function loadCashbooks() {
    const clientId = getClientId();
    const start = popupPeriodStart.value;
    const end = popupPeriodEnd.value;
    if (!clientId || !start || !end) {
        popupCashbookSelect.innerHTML = '<option value="">-- Select date range --</option>';
        popupCashbookSelect.disabled = true;
        popupStartBtn.disabled = true;
        return;
    }
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
        popupCashbookSelect.innerHTML = '<option value="">-- Select Cashbook --</option>';
        data.cashbooks.forEach(cb => {
            const opt = document.createElement('option');
            opt.value = cb;
            opt.textContent = cb;
            popupCashbookSelect.appendChild(opt);
        });
        popupCashbookSelect.disabled = false;
        popupStartBtn.disabled = !popupCashbookSelect.value;
    } catch (e) {
        console.error('Error loading cashbooks:', e);
        popupCashbookSelect.innerHTML = '<option value="">⚠️ Error loading</option>';
        popupCashbookSelect.disabled = true;
        showPopupError('Failed to load cashbooks: ' + e.message);
    }
}

// ---------- POPUP: START SESSION ----------
async function startSession() {
    const clientId = getClientId();
    const cashbook = popupCashbookSelect.value;
    const start = popupPeriodStart.value;
    const end = popupPeriodEnd.value;
    if (!clientId || !cashbook || !start || !end) {
        showPopupError('Please fill all fields.');
        return;
    }
    hidePopupError();
    popupStartBtn.disabled = true;
    popupStartBtn.innerHTML = '<span class="spinner"></span> Starting...';
    try {
        const params = new URLSearchParams({
            client_id: clientId,
            cashbook_name: cashbook,
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

        // Close popup
        popupOverlay.style.display = 'none';

        // Switch to session content
        dashboardContent.style.display = 'none';
        sessionContent.style.display = 'block';

        // Update header
        sessionIdDisplay.textContent = currentSessionId;
        cashbookDisplay.textContent = cashbook;
        periodDisplay.textContent = start + ' to ' + end;

        // Load first transaction
        await loadNextTransaction();
        await updateProgress();
    } catch (e) {
        console.error('Error starting session:', e);
        showPopupError('Error: ' + e.message);
    } finally {
        popupStartBtn.disabled = false;
        popupStartBtn.innerHTML = '🚀 Start Session';
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
                transactionCard.innerHTML = `<div style="padding:30px; text-align:center; color:#16a34a; font-size:18px;">🎉 All transactions verified!</div>`;
                prevBtn.disabled = true;
                nextBtn.disabled = true;
                verifyBtn.disabled = true;
                return;
            }
            throw new Error(err.detail || 'Failed to load transaction');
        }
        const data = await res.json();
        if (data.completed) {
            showStatus('✅ All transactions have been verified!', 'success');
            transactionCard.innerHTML = `<div style="padding:30px; text-align:center; color:#16a34a; font-size:18px;">🎉 All transactions verified!</div>`;
            prevBtn.disabled = true;
            nextBtn.disabled = true;
            verifyBtn.disabled = true;
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
        prevBtn.disabled = false;
        nextBtn.disabled = false;
        verifyBtn.disabled = false;
        showStatus('Loaded transaction ' + currentVoucherId, 'info');
    } catch (e) {
        console.error('Error loading transaction:', e);
        showStatus('Error loading transaction: ' + e.message, 'error');
    } finally {
        isNavigating = false;
    }
}

// ---------- RENDER TRANSACTION ----------
function renderTransaction(data) {
    const row = data.row_data || {};
    let html = `
        <div class="row">
            <div><span class="label">Voucher ID:</span> <span class="value">${data.voucher_id || '—'}</span></div>
            <div><span class="label">Date:</span> <span class="value">${row['Date'] || row['Invoice Date'] || data.extract_date || '—'}</span></div>
            <div><span class="label">Account Code:</span> <span class="value">${row['Account Code'] || '—'}</span></div>
            <div><span class="label">Account Name:</span> <span class="value">${row['Account Name'] || '—'}</span></div>
            <div><span class="label">Amount:</span> <span class="value">${row['Amount'] || row['Debit'] || row['Credit'] || data.extract_amount || '—'}</span></div>
            <div><span class="label">Description:</span> <span class="value">${row['Description'] || '—'}</span></div>
        </div>
        <div style="margin-top:10px;">
            <span class="label">Validation Status:</span>
            <span class="status-badge ${data.validation_status.toLowerCase()}">${data.validation_status}</span>
            ${data.validation_message ? `<span style="font-size:13px; color:#64748b; margin-left:10px;">${data.validation_message}</span>` : ''}
        </div>
    `;

    // Line items grid
    html += `<div class="line-items-section">
        <div class="section-title" style="font-weight:600; font-size:16px; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center;">
            <span>📋 Line Items</span>
            <button class="add-btn" id="addLineItemBtn" style="background:#3b82f6; color:white; border:none; border-radius:4px; padding:4px 12px; cursor:pointer; font-size:13px;">+ Add Row</button>
        </div>
        <table class="line-items-grid">
            <thead>
                <tr>
                    <th style="width:20%;">Vendor</th>
                    <th style="width:15%;">Unit</th>
                    <th style="width:12%;">Qty</th>
                    <th style="width:15%;">Rate</th>
                    <th style="width:18%;">Calculated Amount</th>
                    <th style="width:10%;">Action</th>
                </tr>
            </thead>
            <tbody id="lineItemsBody">
            </tbody>
        </table>
    </div>`;

    // Procedure ticks
    const procKeys = ['purchase_demand', 'purchase_order', 'gate_pass', 'store_receiving', 'vendor_bill'];
    html += `<div class="procedure-section" style="margin-top:16px; border-top:1px solid #e2e8f0; padding-top:16px;">
        <div style="font-weight:600; margin-bottom:6px;">✅ Audit Procedures</div>
        <div class="procedure-grid" id="procedureGrid">`;
    procKeys.forEach(key => {
        const label = key.replace('_', ' ').toUpperCase();
        const checked = procedureData[key] ? 'checked' : '';
        html += `
            <div class="procedure-item">
                <input type="checkbox" id="proc_${key}" ${checked}>
                <label for="proc_${key}">${label}</label>
                <input type="text" placeholder="Ref #" value="${procedureData[key + '_ref'] || ''}" data-proc="${key}">
            </div>
        `;
    });
    html += `</div></div>`;

    // Transaction type dropdown & library panel
    html += `<div class="procedure-section" style="margin-top:12px; border-top:1px solid #e2e8f0; padding-top:12px;">
        <div style="display:flex; gap:12px; align-items:center; flex-wrap:wrap;">
            <label style="font-weight:600;">Transaction Type:</label>
            <select id="transactionTypeSelect" style="padding:4px 10px; border:2px solid #e2e8f0; border-radius:6px;">
                <option value="">-- Select --</option>
                <option value="Material Purchased" ${currentTransactionType === 'Material Purchased' ? 'selected' : ''}>Material Purchased</option>
                <option value="Repair & Maintenance (In-house)" ${currentTransactionType === 'Repair & Maintenance (In-house)' ? 'selected' : ''}>Repair & Maintenance (In-house)</option>
                <option value="Fuel Expense" ${currentTransactionType === 'Fuel Expense' ? 'selected' : ''}>Fuel Expense</option>
                <option value="Short Term Advance against Salary" ${currentTransactionType === 'Short Term Advance against Salary' ? 'selected' : ''}>Short Term Advance against Salary</option>
                <option value="Payment to Contractors" ${currentTransactionType === 'Payment to Contractors' ? 'selected' : ''}>Payment to Contractors</option>
                <option value="Fixed Asset Purchased" ${currentTransactionType === 'Fixed Asset Purchased' ? 'selected' : ''}>Fixed Asset Purchased</option>
                <option value="Kitchen Expenses" ${currentTransactionType === 'Kitchen Expenses' ? 'selected' : ''}>Kitchen Expenses</option>
            </select>
            <button class="btn-outline" id="fetchLibraryBtn" style="padding:4px 16px;">📚 Show Guidance</button>
        </div>
        <div id="libraryPanel" class="library-panel" style="display:none; margin-top:8px;">
            <h4 style="margin:0 0 4px 0; font-size:14px; color:#1a1a2e;">📖 Audit Guidance</h4>
            <div id="libraryContent" class="content" style="font-size:13px; color:#475569; white-space:pre-wrap;"></div>
        </div>
    </div>`;

    // Observations and Comments
    html += `<div style="margin-top:12px; display:flex; gap:16px; flex-wrap:wrap;">
        <div style="flex:1; min-width:200px;">
            <label style="font-weight:600; display:block;">Observations</label>
            <textarea id="observationsInput" rows="2" style="width:100%; padding:6px 10px; border:2px solid #e2e8f0; border-radius:6px;">${procedureData.observations || ''}</textarea>
        </div>
        <div style="flex:1; min-width:200px;">
            <label style="font-weight:600; display:block;">Comments</label>
            <textarea id="commentsInput" rows="2" style="width:100%; padding:6px 10px; border:2px solid #e2e8f0; border-radius:6px;">${procedureData.comments || ''}</textarea>
        </div>
    </div>`;

    // Supporting docs buttons
    html += `<div style="margin-top:12px; display:flex; gap:8px; flex-wrap:wrap;" class="no-print">
        <button class="btn-outline" data-template="purchase_orders" data-label="Purchase Orders">📦 Search PO</button>
        <button class="btn-outline" data-template="gate_passes" data-label="Gate Passes">🚪 Search Gate Pass</button>
        <button class="btn-outline" data-template="store_receiving" data-label="Store Receiving">📥 Search GRN</button>
        <button class="btn-outline" data-template="vendor_bills" data-label="Vendor Bills">🧾 Search Vendor Bill</button>
    </div>`;

    transactionCard.innerHTML = html;

    // Attach event listeners for line items
    renderLineItems();
    document.getElementById('addLineItemBtn')?.addEventListener('click', addLineItemRow);

    // Procedure checkboxes
    procKeys.forEach(key => {
        const cb = document.getElementById(`proc_${key}`);
        if (cb) {
            cb.addEventListener('change', function() {
                procedureData[key] = this.checked;
                const refInput = this.parentElement.querySelector('input[type="text"]');
                if (refInput) {
                    procedureData[key + '_ref'] = refInput.value;
                }
            });
        }
        const refInput = document.querySelector(`input[data-proc="${key}"]`);
        if (refInput) {
            refInput.addEventListener('input', function() {
                procedureData[key + '_ref'] = this.value;
            });
        }
    });

    // Transaction type change
    document.getElementById('transactionTypeSelect')?.addEventListener('change', function() {
        currentTransactionType = this.value;
        document.getElementById('libraryPanel').style.display = 'none';
    });

    // Fetch library button
    document.getElementById('fetchLibraryBtn')?.addEventListener('click', async function() {
        const type = document.getElementById('transactionTypeSelect').value;
        if (!type) {
            alert('Please select a transaction type first.');
            return;
        }
        await fetchLibrary(type);
    });

    // Observations & comments
    document.getElementById('observationsInput')?.addEventListener('input', function() {
        procedureData.observations = this.value;
    });
    document.getElementById('commentsInput')?.addEventListener('input', function() {
        procedureData.comments = this.value;
    });

    // Supporting docs buttons
    transactionCard.querySelectorAll('[data-template]').forEach(btn => {
        btn.addEventListener('click', function() {
            const templateLabel = this.dataset.template;
            const label = this.dataset.label;
            openSupportingModal(templateLabel, label);
        });
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
async function updateProgress() {
    if (!currentSessionId) return;
    try {
        const res = await fetch(`${API_BASE}/api/verification/audited_cashbook/${currentSessionId}`);
        if (!res.ok) throw new Error('Failed to fetch progress');
        const data = await res.json();
        const session = data.session;
        progressDisplay.textContent = `${session.verified_count || 0} / ${session.total_transactions || 0} verified`;
    } catch (e) {
        console.error('Error updating progress:', e);
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
            verifyBtn.disabled = false;
        } else {
            verifyBtn.disabled = true;
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
        // Hide session content and show dashboard
        sessionContent.style.display = 'none';
        dashboardContent.style.display = 'block';
        transactionCard.innerHTML = '';
        showStatus('Session exited.', 'info');
        currentSessionId = null;
        currentDetailId = null;
    }
}

// ---------- SUPPORTING DOCUMENTS MODAL ----------
function openSupportingModal(templateKey, label) {
    supportingModal.style.display = 'flex';
    supportingResults.innerHTML = '<div style="color:#94a3b8; text-align:center; padding:20px;">Enter a search term and click Search.</div>';
    supportingModal.dataset.template = templateKey;
    supportingModal.dataset.label = label;
    supportingSearchInput.focus();
}

async function searchSupportingDocuments() {
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
// Cash Verification button (from the card)
document.getElementById('cashVerificationBtn')?.addEventListener('click', function() {
    popupOverlay.style.display = 'flex';
    // Set default date range to current month
    const now = new Date();
    const firstDay = new Date(now.getFullYear(), now.getMonth(), 1);
    const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    const formatDate = (d) => {
        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${y}-${m}-${day}`;
    };
    popupPeriodStart.value = formatDate(firstDay);
    popupPeriodEnd.value = formatDate(lastDay);
    // Load cashbooks based on current client and dates
    loadCashbooks();
});

// Popup events
popupPeriodStart.addEventListener('change', loadCashbooks);
popupPeriodEnd.addEventListener('change', loadCashbooks);
popupCashbookSelect.addEventListener('change', function() {
    popupStartBtn.disabled = !this.value;
});
popupStartBtn.addEventListener('click', startSession);
popupCancelBtn.addEventListener('click', function() {
    popupOverlay.style.display = 'none';
});

// Session navigation
prevBtn.addEventListener('click', () => navigate('prev'));
nextBtn.addEventListener('click', () => navigate('next'));
saveProgressBtn.addEventListener('click', saveLineItems);
verifyBtn.addEventListener('click', completeTransaction);
exitBtn.addEventListener('click', exitSession);
exitSessionLink.addEventListener('click', function(e) {
    e.preventDefault();
    exitSession();
});

// Supporting modal
supportingSearchBtn.addEventListener('click', searchSupportingDocuments);
supportingSearchInput.addEventListener('keypress', function(e) {
    if (e.key === 'Enter') searchSupportingDocuments();
});
supportingModalClose.addEventListener('click', function() {
    supportingModal.style.display = 'none';
});
supportingModal.addEventListener('click', function(e) {
    if (e.target === this) this.style.display = 'none';
});
// ==============================================
// TRANSACTION REPORT MODAL
// ==============================================

const reportModalOverlay = document.getElementById('reportModalOverlay');
const reportModal = document.getElementById('reportModal');
const reportStartDate = document.getElementById('reportStartDate');
const reportEndDate = document.getElementById('reportEndDate');
const reportGenerateBtn = document.getElementById('reportGenerateBtn');
const reportModalClose = document.getElementById('reportModalClose');
const reportResults = document.getElementById('reportResults');
const reportStatus = document.getElementById('reportStatus');

// Open modal
document.getElementById('transactionReportBtn')?.addEventListener('click', function() {
    reportModalOverlay.style.display = 'flex';
    // Set default date range to current month
    const now = new Date();
    const firstDay = new Date(now.getFullYear(), now.getMonth(), 1);
    const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    const formatDate = (d) => {
        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${y}-${m}-${day}`;
    };
    reportStartDate.value = formatDate(firstDay);
    reportEndDate.value = formatDate(lastDay);
    reportResults.innerHTML = '<div style="text-align:center; color:#94a3b8; padding:40px;">Select date range and click Generate.</div>';
    reportStatus.textContent = '';
});

// Close modal
function closeReportModal() {
    reportModalOverlay.style.display = 'none';
}
reportModalClose.addEventListener('click', closeReportModal);
reportModalOverlay.addEventListener('click', function(e) {
    if (e.target === this) closeReportModal();
});

// Generate report
reportGenerateBtn.addEventListener('click', async function() {
    const start = reportStartDate.value;
    const end = reportEndDate.value;
    if (!start || !end) {
        reportStatus.textContent = '⚠️ Please select both dates.';
        reportStatus.style.color = '#f59e0b';
        return;
    }
    if (start > end) {
        reportStatus.textContent = '⚠️ Start date must be before or equal to end date.';
        reportStatus.style.color = '#f59e0b';
        return;
    }
    reportStatus.textContent = '⏳ Loading...';
    reportStatus.style.color = '#3b82f6';
    reportGenerateBtn.disabled = true;

    const clientId = getClientId();
    if (!clientId) {
        reportStatus.textContent = '⚠️ No client selected.';
        reportStatus.style.color = '#dc2626';
        reportGenerateBtn.disabled = false;
        return;
    }

    try {
        // 1. Look up Transaction Report template by name
        const tplRes = await fetch(`${API_BASE}/api/templates/by-name/Transaction Report`);
        if (!tplRes.ok) {
            if (tplRes.status === 404) {
                throw new Error('Transaction Report template not found. Please restore it via Setup.');
            }
            throw new Error('Failed to fetch template details.');
        }
        const tplData = await tplRes.json();
        const templateId = tplData.id;

        // 2. Fetch data using /api/data/preview
        const params = new URLSearchParams({
            client_id: clientId,
            template_id: templateId,
            date_start: start,
            date_end: end,
            limit: 500  // we'll fetch up to 500 rows
        });

        const dataRes = await fetch(`${API_BASE}/api/data/preview?${params}`);
        if (!dataRes.ok) {
            const err = await dataRes.json();
            throw new Error(err.detail || 'Failed to fetch data');
        }
        const data = await dataRes.json();
        const rows = data.rows || [];
        const totalRows = data.total_rows || 0;

        // Render table
        if (rows.length === 0) {
            reportResults.innerHTML = `<div style="text-align:center; color:#94a3b8; padding:40px;">No transactions found for the selected period.</div>`;
            reportStatus.textContent = '✅ 0 rows';
            reportStatus.style.color = '#16a34a';
            reportGenerateBtn.disabled = false;
            return;
        }

        // Build table
        const firstRow = rows[0].row_data;
        const headers = firstRow ? Object.keys(firstRow) : [];

        let tableHtml = `<table style="width:100%; border-collapse:collapse; font-size:13px;">
            <thead style="background:#f8fafc; position:sticky; top:0; z-index:1;">
                <tr>`;
        headers.forEach(h => {
            tableHtml += `<th style="padding:8px 12px; text-align:left; border-bottom:2px solid #e2e8f0; font-weight:600;">${h}</th>`;
        });
        tableHtml += `</tr></thead><tbody>`;

        rows.forEach(row => {
            tableHtml += `<tr style="border-bottom:1px solid #f0f2f5;">`;
            headers.forEach(h => {
                const val = row.row_data[h] !== undefined ? row.row_data[h] : '';
                tableHtml += `<td style="padding:8px 12px;">${val}</td>`;
            });
            tableHtml += `</tr>`;
        });
        tableHtml += `</tbody></table>`;

        reportResults.innerHTML = tableHtml;
        reportStatus.textContent = `✅ ${totalRows} rows`;
        reportStatus.style.color = '#16a34a';

    } catch (error) {
        console.error('Report error:', error);
        reportResults.innerHTML = `<div style="text-align:center; color:#dc2626; padding:40px;">❌ ${error.message}</div>`;
        reportStatus.textContent = '❌ Error';
        reportStatus.style.color = '#dc2626';
    } finally {
        reportGenerateBtn.disabled = false;
    }
});
// ---------- INIT ----------
window.onload = function() {
    // Ensure dashboard is visible, session content hidden
    dashboardContent.style.display = 'block';
    sessionContent.style.display = 'none';
    // Load verification stats
    loadVerificationStats();
    console.log('✅ Verification module loaded (restored dashboard)');
};

// ---------- LOAD VERIFICATION STATS ----------
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