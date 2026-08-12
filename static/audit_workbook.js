// Audit Workbook page — Phase 3 of 5 (wire page to endpoint: fetch, render, filter chips).
//
// Phase 2 built the static shell (placeholder rows, non-functional filter chips, a static
// total row). This phase reads ?session=<id> from the URL (same convention as
// template-viewer.js's ?template=), fetches Phase 1's single per-voucher rollup endpoint once,
// and does everything else — filtering, rendering, the running total — client-side from that
// one in-memory array. No client-side status rollup logic: `status` is used exactly as
// _rollup_voucher_status() (routers/verification.py) already computed it.
//
// API_BASE is a global already provided by client_context.js — deliberately not redeclared
// here (redeclaring a global in a second plain <script> has caused real bugs elsewhere in this
// app, see error_dashboard.js's old STORAGE_KEY crash).

// ---------- STATE ----------
let currentSessionId = null;
let allVouchers = [];
let currentFilterStatus = 'all';

// ---------- DOM REFS ----------
const awSubtitleEl = document.getElementById('awSubtitle');
const awTableBody = document.getElementById('awTableBody');
const awTotalAmountEl = document.getElementById('awTotalAmount');
const awFilterChips = [
    document.getElementById('awFilterAll'),
    document.getElementById('awFilterCleared'),
    document.getElementById('awFilterQuery'),
    document.getElementById('awFilterPending')
];
// Peer nav bar (Phase 5, FINAL) — see the matching comment in audit_workbook.html.
const awVerificationBtn = document.getElementById('awVerificationBtn');

// ---------- HELPERS ----------

// Local formatter, same shape as verification.js's own formatBillTotalDisplay() — Audit
// Workbook is a Verification-adjacent page, and no page in that module pulls in
// static/utils.js's app-wide settings-driven formatAmount(), so this stays consistent with
// its siblings rather than introducing a new dependency for one field.
function formatAwAmount(value) {
    const num = typeof value === 'number' ? value : parseFloat(value);
    if (isNaN(num)) return '—';
    return num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str === null || str === undefined ? '' : String(str);
    return div.innerHTML;
}

function setChipsDisabled(disabled) {
    awFilterChips.forEach(chip => { chip.disabled = disabled; });
}

// ---------- RENDER ----------
function renderAuditWorkbookRows(vouchers, emptyMessage) {
    awTableBody.innerHTML = '';

    if (!vouchers || vouchers.length === 0) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 5;
        td.textContent = emptyMessage;
        td.style.textAlign = 'center';
        td.style.color = '#94a3b8';
        tr.appendChild(td);
        awTableBody.appendChild(tr);
        return;
    }

    vouchers.forEach(v => {
        const tr = document.createElement('tr');
        tr.className = 'aw-row-clickable';
        tr.setAttribute('data-voucher-no', v.voucher_no || '');

        const statusClass = (v.status || '').toLowerCase();
        tr.innerHTML = `
            <td>${escapeHtml(v.voucher_no)}</td>
            <td>${escapeHtml(v.date || '—')}</td>
            <td>${escapeHtml(v.description || '—')}</td>
            <td>${formatAwAmount(v.amount)}</td>
            <td><span class="aw-status-badge ${statusClass}">${escapeHtml(v.status || '—')}</span></td>
        `;
        awTableBody.appendChild(tr);
    });
}

// Recomputes the visible set from the current filter and re-renders both the table and the
// running-total footer from that same set — the footer must always reflect what's on screen,
// not the full unfiltered fetch.
function applyFilterAndRender() {
    const visible = currentFilterStatus === 'all'
        ? allVouchers
        : allVouchers.filter(v => v.status === currentFilterStatus);

    const emptyMessage = allVouchers.length === 0
        ? 'No vouchers found for this session.'
        : 'No vouchers match this filter.';
    renderAuditWorkbookRows(visible, emptyMessage);

    const total = visible.reduce((sum, v) => sum + (typeof v.amount === 'number' ? v.amount : (parseFloat(v.amount) || 0)), 0);
    awTotalAmountEl.textContent = formatAwAmount(total);

    awFilterChips.forEach(chip => {
        chip.classList.toggle('active', chip.dataset.status === currentFilterStatus);
    });
}

// ---------- FETCH ----------
async function fetchAuditWorkbook(sessionId) {
    awSubtitleEl.textContent = 'Loading vouchers…';
    setChipsDisabled(true);
    renderAuditWorkbookRows([], 'Loading vouchers…');

    try {
        const res = await fetch(`${API_BASE}/api/verification/session/${sessionId}/audit_workbook`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch Audit Workbook data');
        }
        const data = await res.json();
        allVouchers = data.vouchers || [];
        awSubtitleEl.textContent = `Session #${sessionId} · ${data.total || 0} voucher(s)`;
        setChipsDisabled(false);
        currentFilterStatus = 'all';
        applyFilterAndRender();
    } catch (error) {
        console.error('Audit Workbook fetch error:', error);
        awSubtitleEl.textContent = '❌ Error: ' + error.message;
        allVouchers = [];
        setChipsDisabled(true);
        renderAuditWorkbookRows([], 'Error loading vouchers. Please try again.');
        awTotalAmountEl.textContent = formatAwAmount(0);
    }
}

// ---------- NAVIGATION (Phase 4) ----------
// Reuses verification.js's existing hard-refresh restore path (?state=session&id=&voucher=,
// see restoreSessionView()/activateSessionView()) rather than building a parallel mechanism —
// that path already resolves an arbitrary voucher_no to its detail_id client-side from the
// session's full transaction list, with no backend support needed for an arbitrary jump.
function navigateToVoucherInVerification(voucherNo) {
    if (!currentSessionId || !voucherNo) return;
    const params = new URLSearchParams({ state: 'session', id: currentSessionId, voucher: voucherNo });
    window.location.href = `/static/verification.html?${params}`;
}

// ---------- EVENT LISTENERS ----------
awFilterChips.forEach(chip => {
    chip.addEventListener('click', () => {
        currentFilterStatus = chip.dataset.status;
        applyFilterAndRender();
    });
});

// Delegated on the tbody (wired once) since renderAuditWorkbookRows() rebuilds every <tr> from
// scratch on each render — placeholder/loading/error/empty rows never carry data-voucher-no, so
// they're naturally excluded from this selector, no extra guard needed.
awTableBody.addEventListener('click', function (e) {
    const tr = e.target.closest('tr[data-voucher-no]');
    if (!tr) return;
    const voucherNo = tr.getAttribute('data-voucher-no');
    if (!voucherNo) return;
    navigateToVoucherInVerification(voucherNo);
});

// ---------- INIT ----------
window.onload = function () {
    const params = new URLSearchParams(window.location.search);
    const sessionParam = params.get('session');
    const sessionId = Number(sessionParam);
    const isValidSessionId = sessionParam && Number.isInteger(sessionId) && sessionId > 0;

    if (isValidSessionId) {
        currentSessionId = sessionId;
        // Reuses verification.js's own hard-refresh restore path (?state=session&id=) — lands
        // on the session's earliest-Pending voucher (no ?voucher= given, since this button has
        // no specific-voucher context to carry, unlike Phase 4's per-row jump).
        if (awVerificationBtn) {
            awVerificationBtn.disabled = false;
            awVerificationBtn.addEventListener('click', () => {
                window.location.href = `/static/verification.html?state=session&id=${currentSessionId}`;
            });
        }
        fetchAuditWorkbook(sessionId);
    } else {
        currentSessionId = null;
        allVouchers = [];
        if (awVerificationBtn) awVerificationBtn.disabled = true;
        awSubtitleEl.textContent = 'No session specified — open this page via an Audit Workbook link from Verification.';
        setChipsDisabled(true);
        renderAuditWorkbookRows([], 'No session specified.');
        awTotalAmountEl.textContent = formatAwAmount(0);
    }
};
