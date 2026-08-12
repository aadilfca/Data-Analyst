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
        // Inert hook for Phase 4's row-click — not wired to anything this phase.
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

// ---------- EVENT LISTENERS ----------
awFilterChips.forEach(chip => {
    chip.addEventListener('click', () => {
        currentFilterStatus = chip.dataset.status;
        applyFilterAndRender();
    });
});

// ---------- INIT ----------
window.onload = function () {
    const params = new URLSearchParams(window.location.search);
    const sessionParam = params.get('session');
    const sessionId = Number(sessionParam);
    const isValidSessionId = sessionParam && Number.isInteger(sessionId) && sessionId > 0;

    if (isValidSessionId) {
        currentSessionId = sessionId;
        fetchAuditWorkbook(sessionId);
    } else {
        currentSessionId = null;
        allVouchers = [];
        awSubtitleEl.textContent = 'No session specified — open this page via an Audit Workbook link from Verification.';
        setChipsDisabled(true);
        renderAuditWorkbookRows([], 'No session specified.');
        awTotalAmountEl.textContent = formatAwAmount(0);
    }
};
