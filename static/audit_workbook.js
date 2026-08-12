// Audit Workbook page — line-grain restructure (new, confirmed 3-phase follow-on requirement),
// Phase 2 of 3 (frontend: restructure table rendering).
//
// Phase 1 changed GET /api/verification/session/{session_id}/audit_workbook from one row per
// voucher to one row per accounting LINE grouped under its voucher — each voucher group is now
// {sr, voucher_no, date, line_count, amount, status, lines: [...]}, each line
// {detail_id, client_data_id, account_code, description, amount, status,
//  procedures: {done, total}, observations: {count, entries}}. This phase rewrites rendering to
// consume that shape: a voucher header band row (Sr shown once, not per line), unnumbered line
// rows with new Procedures/Observations columns, and a subtotal row for any voucher with more
// than one line. Styling polish is a separate, later phase — this file deliberately reuses only
// already-existing global CSS classes (no style.css changes this phase):
//   - .voucher-line-procedures-badge/.voucher-line-proc-ring(-fill) and
//     .voucher-line-obs-flag/.voucher-line-obs-none/.voucher-line-obs-expanded-list(-item/-doc/-text)
//     from static/verification.js's own Procedures/Observations column classes (defined globally
//     in style.css, not scoped to Verification's own container) — "matches Verification's
//     pattern" per the confirmed brief.
//   - .aw-status-badge/.aw-row-clickable, already defined for this page.
//
// Locked column mapping (confirmed this phase): Account = account_code, Reference = description —
// the only two per-line text fields the backend returns. Observations expand/collapse is a
// PER-ROW toggle (confirmed this phase) — clicking one line's own flag/expanded-list only affects
// that line, not a shared page-wide state like Verification's own columnsExpanded.
//
// API_BASE is a global already provided by client_context.js — deliberately not redeclared here
// (redeclaring a global in a second plain <script> has caused real bugs elsewhere in this app,
// see error_dashboard.js's old STORAGE_KEY crash).

// ---------- STATE ----------
let currentSessionId = null;
let allVouchers = [];           // voucher GROUPS (Phase 1's new shape), not flat rows
let currentFilterStatus = 'all';
// Per-row Observations expand state — a Set of line detail_ids currently expanded. Survives
// re-filtering/re-rendering (both just re-run applyFilterAndRender() over the same in-memory
// data); reset only on a fresh session fetch, mirroring allVouchers' own lifetime.
let expandedObservationDetailIds = new Set();

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
// Peer nav bar (Phase 5, FINAL of the original 5-phase build) — see the matching comment in
// audit_workbook.html.
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

// Verbatim local copy of static/verification.js's buildProcedureRingSvg() — small and
// self-contained, duplicated per this codebase's established "visually matched, physically
// duplicated" convention for a new page's own list (the same convention .aw-status-badge's
// palette already follows) rather than cross-importing between page scripts.
function buildProcedureRingSvg(pct, cls) {
    const r = 6, c = 2 * Math.PI * r;
    const off = c - (pct / 100) * c;
    return `<svg class="voucher-line-proc-ring" viewBox="0 0 16 16">
        <circle cx="8" cy="8" r="${r}" fill="none" stroke="#e2e8f0" stroke-width="2.5"/>
        <circle cx="8" cy="8" r="${r}" fill="none" class="voucher-line-proc-ring-fill ${cls}" stroke-width="2.5"
            stroke-dasharray="${c}" stroke-dashoffset="${off}" transform="rotate(-90 8 8)" stroke-linecap="round"/>
    </svg>`;
}

function getAwStatusBadgeHtml(status) {
    const statusClass = (status || '').toLowerCase();
    return `<span class="aw-status-badge ${statusClass}">${escapeHtml(status || '—')}</span>`;
}

// Procedures cell: static fraction + ring, matching Verification's own collapsed-badge look —
// never clickable here, since this page has no #procedureModal equivalent to open. {total: 0}
// (no resolved Category) renders a plain muted dash, same convention getAwObservationsCellHtml()
// uses for a zero-entry line.
function getAwProceduresCellHtml(procedures) {
    const done = procedures ? procedures.done : 0;
    const total = procedures ? procedures.total : 0;
    if (!total) {
        return '<span class="voucher-line-obs-none">—</span>';
    }
    const pct = Math.round((done / total) * 100);
    const cls = done === 0 ? 'pending' : (done === total ? 'done' : 'partial');
    return `
        <span class="voucher-line-procedures-badge ${cls}" title="${done} of ${total} documents complete">
            ${buildProcedureRingSvg(pct, cls)}${done}/${total}
        </span>
    `;
}

// Observations cell: ⚑ count flag (collapsed) or the full entries list (expanded), toggled
// per-row via expandedObservationDetailIds — both the flag and the expanded list carry
// data-detail-id so the delegated click handler below can flip membership and re-render.
function getAwObservationsCellHtml(line) {
    const count = line.observations ? line.observations.count : 0;
    const entries = (line.observations && line.observations.entries) || [];
    if (!count) {
        return '<span class="voucher-line-obs-none">—</span>';
    }
    if (expandedObservationDetailIds.has(line.detail_id)) {
        const itemsHtml = entries.map(entry => `
            <div class="voucher-line-obs-expanded-item">
                <span class="voucher-line-obs-expanded-doc">${escapeHtml(entry.document_name || 'Document')}:</span>
                <span class="voucher-line-obs-expanded-text">${escapeHtml(entry.text)}</span>
            </div>
        `).join('');
        return `<div class="voucher-line-obs-expanded-list" data-detail-id="${line.detail_id}">${itemsHtml}</div>`;
    }
    return `<span class="voucher-line-obs-flag" data-detail-id="${line.detail_id}" title="${count} observation${count === 1 ? '' : 's'} recorded">⚑ ${count}</span>`;
}

// ---------- RENDER ----------

function buildVoucherHeaderRowHtml(v) {
    // No group-level description field exists in the Phase 1 response (only per-line) — borrowed
    // from the voucher's own first line, per the confirmed design (no backend change needed).
    const firstDescription = (v.lines && v.lines[0] && v.lines[0].description) || '—';
    return `
        <tr class="aw-voucher-header-row aw-row-clickable" data-voucher-no="${escapeHtml(v.voucher_no)}">
            <td colspan="6">Sr ${v.sr} · <strong>${escapeHtml(v.voucher_no)}</strong> · ${escapeHtml(v.date || '—')} · ${escapeHtml(firstDescription)}</td>
        </tr>
    `;
}

function buildLineRowHtml(v, line) {
    return `
        <tr class="aw-line-row aw-row-clickable" data-voucher-no="${escapeHtml(v.voucher_no)}" data-detail-id="${line.detail_id}">
            <td>${escapeHtml(line.account_code || '—')}</td>
            <td>${escapeHtml(line.description || '—')}</td>
            <td>${formatAwAmount(line.amount)}</td>
            <td>${getAwProceduresCellHtml(line.procedures)}</td>
            <td>${getAwObservationsCellHtml(line)}</td>
            <td>${getAwStatusBadgeHtml(line.status)}</td>
        </tr>
    `;
}

// Only rendered when line_count > 1 — a single-line voucher's own line row already carries the
// voucher's whole amount/status, so a subtotal would just duplicate it.
function buildSubtotalRowHtml(v) {
    return `
        <tr class="aw-subtotal-row">
            <td colspan="2">Subtotal</td>
            <td>${formatAwAmount(v.amount)}</td>
            <td colspan="2"></td>
            <td>${getAwStatusBadgeHtml(v.status)}</td>
        </tr>
    `;
}

function renderAuditWorkbookRows(vouchers, emptyMessage) {
    if (!vouchers || vouchers.length === 0) {
        awTableBody.innerHTML = `
            <tr><td colspan="6" style="text-align:center; color:#94a3b8;">${escapeHtml(emptyMessage)}</td></tr>
        `;
        return;
    }

    const rowsHtml = vouchers.map(v => {
        const lineRowsHtml = (v.lines || []).map(line => buildLineRowHtml(v, line)).join('');
        const subtotalHtml = v.line_count > 1 ? buildSubtotalRowHtml(v) : '';
        return buildVoucherHeaderRowHtml(v) + lineRowsHtml + subtotalHtml;
    }).join('');

    awTableBody.innerHTML = rowsHtml;
}

// Recomputes the visible set from the current filter (still voucher-GROUP level — status is the
// group's own rollup) and re-renders both the table and the running-total footer from that same
// set — the footer must always reflect what's on screen, not the full unfiltered fetch.
function applyFilterAndRender() {
    const visible = currentFilterStatus === 'all'
        ? allVouchers
        : allVouchers.filter(v => v.status === currentFilterStatus);

    const emptyMessage = allVouchers.length === 0
        ? 'No vouchers found for this session.'
        : 'No vouchers match this filter.';
    renderAuditWorkbookRows(visible, emptyMessage);

    // Each group's own `amount` is already the sum of its lines (Phase 1) — no re-derivation
    // needed here, just sum across the currently-visible groups.
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
        expandedObservationDetailIds = new Set();
        awSubtitleEl.textContent = `Session #${sessionId} · ${data.total_vouchers || 0} voucher(s), ${data.total_lines || 0} line(s)`;
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

// ---------- NAVIGATION (Phase 4 of the original 5-phase build) ----------
// Reuses verification.js's existing hard-refresh restore path (?state=session&id=&voucher=,
// see restoreSessionView()/activateSessionView()) rather than building a parallel mechanism —
// that path already resolves an arbitrary voucher_no to its detail_id client-side from the
// session's full transaction list, with no backend support needed for an arbitrary jump. A click
// on any line within a voucher (or its header row) lands on that same voucher's first line —
// this restore path has no support for jumping to one specific line within a voucher.
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
// scratch on each render. Two concerns share this one listener: (1) the Observations per-row
// expand/collapse toggle, checked FIRST so a click on the flag/expanded-list never also triggers
// row navigation; (2) voucher header/line row click-to-navigate (unchanged from Phase 4) — a
// subtotal row carries neither data-detail-id nor data-voucher-no, so it's naturally excluded
// from both, and placeholder/loading/error/empty rows carry no data attributes either.
awTableBody.addEventListener('click', function (e) {
    const obsTarget = e.target.closest('.voucher-line-obs-flag, .voucher-line-obs-expanded-list');
    if (obsTarget) {
        e.stopPropagation();
        const detailId = Number(obsTarget.getAttribute('data-detail-id'));
        if (expandedObservationDetailIds.has(detailId)) {
            expandedObservationDetailIds.delete(detailId);
        } else {
            expandedObservationDetailIds.add(detailId);
        }
        applyFilterAndRender();
        return;
    }

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
        // no specific-voucher context to carry, unlike the per-row jump above).
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
