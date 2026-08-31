// Audit Workbook page — two overlapping change sets share this file's history:
// (1) the earlier 2-phase "restyle" (Sr/Voucher No. columns replacing the old banner row,
//     then lighter styling in style.css — see the aw-group-even/aw-group-odd note below).
// (2) the current 3-phase "restructure" (Phase 1: backend account_name lookup, done;
//     Phase 2, THIS PHASE: real HTML rowspan on Sr/Voucher No./Account instead of the old
//     blank-cell/repeated-text fake grouping, plus Account merged into a code+name stacked
//     cell; Phase 3: grouping visual treatment in CSS).
//
// renderAuditWorkbookRows() stamps each voucher group's line/subtotal rows with a shared
// aw-group-even/aw-group-odd class, a pure CSS hook for zebra striping (variable-sized groups
// can't be alternated in plain CSS with no marker) — Phase 3's job, unused by CSS today.
//
// GET /api/verification/session/{session_id}/audit_workbook returns one voucher group per
// voucher — {sr, voucher_no, date, line_count, amount, status, lines: [...]}, each line
// {detail_id, client_data_id, account_code, account_name, description, amount, status,
//  procedures: {done, total}, observations: {count, entries}}. Sr/Voucher No./Date are each
// rendered ONCE per voucher group via a real <td rowspan="line_count">, on the group's first
// line row only — not a blank placeholder cell repeated down the group, and not extended to
// cover the subtotal row (see buildSubtotalRowHtml()'s own comment for why). Account is a
// genuinely per-line field (confirmed fix — previously wrongly rowspan'd off the group's
// first line, repeating one account down every line of a multi-account voucher), rendered in
// its own always-present <td> per line, same as Reference. Its cell merges account_code +
// account_name (Phase 1's field) stacked, falling back to the code alone when account_name is
// null. Further styling polish (hairline borders, zebra striping,
// lighter subtotal row, any dedicated Account cell styling) is Phase 3 — this file deliberately
// reuses only already-existing global CSS classes plus two new unstyled hook classes
// (aw-account-code/aw-account-name, plain block-level <div>s, no style.css changes this phase):
//   - .voucher-line-procedures-badge/.voucher-line-proc-ring(-fill) and
//     .voucher-line-obs-flag/.voucher-line-obs-none/.voucher-line-obs-expanded-list(-item/-doc/-text)
//     from static/verification.js's own Procedures/Observations column classes (defined globally
//     in style.css, not scoped to Verification's own container) — "matches Verification's
//     pattern" per the confirmed brief.
//   - .aw-status-badge/.aw-row-clickable, already defined for this page.
//
// Locked column mapping: Account = account_code + account_name (merged), Reference =
// description. Observations expand/collapse is a PER-ROW toggle — clicking one line's own
// flag/expanded-list only affects that line, not a shared page-wide state like Verification's
// own columnsExpanded.
//
// Date column (confirmed single-phase fix, frontend-only — no backend change, the Phase 1
// endpoint already returns `date` per voucher group): inserted after Voucher No., same
// rowspan-once-per-voucher-group treatment as Sr/Voucher No./Account (v.date is a group-level
// field, not per-line, so it's read from `v` directly rather than the line object). Formatted
// via utils.js's formatDate() (now loaded in audit_workbook.html) rather than a raw ISO string.
//
// API_BASE is a global already provided by client_context.js — deliberately not redeclared here
// (redeclaring a global in a second plain <script> has caused real bugs elsewhere in this app,
// see error_dashboard.js's old STORAGE_KEY crash).
//
// Land-on-voucher (Voucher No. -> Audit Workbook, confirmed requirement — Voucher No. on
// verification.js's voucher header is a clickable link that navigates here with
// ?session=&voucher=<voucher_no>, see navigateToAuditWorkbook() there): window.onload reads the
// optional ?voucher= param and, once the fetch resolves, positions on that voucher's row.
//
// Spinner-overlay gating (confirmed acceptance criterion): the spinner must stay visible until
// BOTH the data has loaded AND the target row has already been scrolled into view — not just
// "no visible flash," a stricter requirement than relying on the browser never painting between
// two synchronous statements. #awSpinnerOverlay (style.css/audit_workbook.html) is a full-cover
// overlay INSIDE .aw-table-wrapper, not a spinner row swapped into #awTableBody — so
// applyFilterAndRender()'s row-insertion and positionOnTargetVoucher()'s instant scroll both
// happen completely hidden underneath it, and hideSpinnerOverlay() is the very last call in
// fetchAuditWorkbook()'s success path. The scroll itself is an instant jump (scrollIntoView
// behavior:'auto', not 'smooth') plus a brief highlight flash reusing the exact fade pattern
// already established for Assign Categories' .ar-tb-restore-highlight (static/style.css),
// duplicated here under Audit Workbook's own class name per this file's established "visually
// matched, physically duplicated" convention. A voucher_no with no match in this session fails
// soft (page just loads at the top, no error) — the overlay still hides correctly since
// positionOnTargetVoucher() no-ops but hideSpinnerOverlay() still runs right after it.

// ---------- STATE ----------
let currentSessionId = null;
let allVouchers = [];           // voucher GROUPS (Phase 1's new shape), not flat rows
let currentFilterStatus = 'all';
// Per-row Observations expand state — a Set of line detail_ids currently expanded. Survives
// re-filtering/re-rendering (both just re-run applyFilterAndRender() over the same in-memory
// data); reset only on a fresh session fetch, mirroring allVouchers' own lifetime.
let expandedObservationDetailIds = new Set();

// ---------- PROCEDURES-PILL WIRING STATE ----------
// This session's client_id (GET .../audit_workbook now returns it) — the modal's own
// client-scoped calls (Trial Balance leaf-account lookup, Data Bank search/browse) must use the
// session's real client, not whatever the global navbar client picker happens to have selected.
let sessionClientId = null;
// {[detail_id]: {transactionType: category_name}} — the direct analog of verification.js's own
// voucherLineState, populated once per fetchAuditWorkbook() from the endpoint's new per-line
// category_name field. openProcedureModal() (procedure_modal.js) hard-gates on
// hostAdapter.voucherLineState[uid].transactionType being truthy before it will open at all.
let awVoucherLineState = {};
// {[detail_id]: {...}} — the modal's own per-line tab scratch state (analog of verification.js's
// procedureModalState), written into directly by procedure_modal.js via
// hostAdapter.procedureModalState[uid] = {...}. Persistent across modal opens within this page's
// lifetime, reset only alongside a fresh session fetch (matches awVoucherLineState's own lifetime).
let awProcedureModalState = {};
// The Audit Workbook uses the same session-scoped compiled Rulebook as Verification. It is
// loaded lazily on the first guidance request, then reused for every Category on this page.
// The backend endpoint enforces the session's captured Rulebook version.
let awSessionRulebook = null;
let awSessionRulebookPromise = null;

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
// Fetch-in-flight spinner overlay (Voucher No. -> Audit Workbook deep-link fix) — see the
// matching comment on #awSpinnerOverlay in audit_workbook.html/style.css for why this is a
// full-cover overlay rather than a spinner row inside #awTableBody.
const awSpinnerOverlay = document.getElementById('awSpinnerOverlay');
// Status feedback for the shared procedure modal's adapter (Procedures-pill wiring phase) — same
// .upload-status convention as #verificationStatus in verification.html.
const awStatusEl = document.getElementById('awStatus');

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

// Account cell content: code + name stacked (Phase 2's merged "Option B" format), using
// Phase 1's account_name field. null falls back to showing just the code, same '—' convention
// the rest of this file already uses for a missing value.
function getAwAccountCellHtml(line) {
    const code = escapeHtml(line.account_code || '—');
    if (!line.account_name) {
        return code;
    }
    return `<div class="aw-account-code">${code}</div><div class="aw-account-name">${escapeHtml(line.account_name)}</div>`;
}

function getAwStatusBadgeHtml(status) {
    const statusClass = (status || '').toLowerCase();
    return `<span class="aw-status-badge ${statusClass}">${escapeHtml(status || '—')}</span>`;
}

// Procedures cell: static fraction + ring, matching Verification's own collapsed-badge look.
// Procedures-pill wiring phase: clickable — carries data-detail-id so the delegated click
// listener below can open the shared procedure modal via this page's own adapter. {total: 0}
// (no resolved Category) renders a plain muted dash with no badge at all, same convention
// getAwObservationsCellHtml() uses for a zero-entry line — naturally non-clickable, no extra
// guard needed.
function getAwProceduresCellHtml(line) {
    const procedures = line.procedures;
    const done = procedures ? procedures.done : 0;
    const total = procedures ? procedures.total : 0;
    if (!total) {
        return '<span class="voucher-line-obs-none">—</span>';
    }
    const pct = Math.round((done / total) * 100);
    const cls = done === 0 ? 'pending' : (done === total ? 'done' : 'partial');
    return `
        <span class="voucher-line-procedures-badge ${cls}" data-detail-id="${line.detail_id}" title="${done} of ${total} documents complete — click to open">
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

// Sr, Voucher No., and Date are each rendered ONCE per voucher group, via a real
// <td rowspan="line_count">, emitted only on the group's first line row (isFirstLine) — not a
// blank placeholder repeated down the group. Account (confirmed fix: was previously rowspan'd
// off the group's first line only, which wrongly repeated line 0's account down every line of
// a multi-account voucher — e.g. CPV-0126-110) is a genuinely PER-LINE field on the backend
// response (each line carries its own account_code/account_name), so it's rendered in its own
// always-present <td> instead, the same way aw-col-reference already is. The Sr/Voucher
// No./Date rowspan deliberately covers only the line_count line rows, not the subtotal row
// that may follow — see buildSubtotalRowHtml()'s own comment for why that row needs its own
// leading cells instead of being covered by this span. groupParityClass ('aw-group-even'/
// 'aw-group-odd') is a pure CSS hook — style.css's
// zebra striping needs it since variable-sized voucher groups can't be alternated in plain CSS
// with no marker. The group-start class (confirmed fix) is a second such hook, stamped only
// when isFirstLine is true: the row's own first-line-vs-continuation position used to be
// inferred in CSS via :has(td:first-child:not(:empty)) (blank-Sr-cell-on-continuation-rows was
// true under the old pre-rowspan markup), but omitting the leading <td>s entirely on
// continuation rows (rather than rendering them blank) left every continuation row's true
// first-child as the Reference cell — which is never empty (falls back to '—') — so that
// selector was firing on every line of a multi-line voucher, not just the true first one. An
// explicit class removes the guesswork the same way aw-group-even/aw-group-odd already do.
function buildLineRowHtml(v, line, isFirstLine, groupParityClass) {
    const leadingCellsHtml = isFirstLine ? `
        <td rowspan="${v.line_count}">${escapeHtml(String(v.sr))}</td>
        <td rowspan="${v.line_count}">${escapeHtml(v.voucher_no)}</td>
        <td rowspan="${v.line_count}">${formatDate(v.date)}</td>
    ` : '';
    const groupStartClass = isFirstLine ? ' group-start' : '';
    return `
        <tr class="aw-line-row aw-row-clickable ${groupParityClass}${groupStartClass}" data-voucher-no="${escapeHtml(v.voucher_no)}" data-detail-id="${line.detail_id}">
            ${leadingCellsHtml}
            <td class="aw-col-account">${getAwAccountCellHtml(line)}</td>
            <td class="aw-col-reference">${escapeHtml(line.description || '—')}</td>
            <td class="aw-col-amount">${formatAwAmount(line.amount)}</td>
            <td class="aw-col-procedures">${getAwProceduresCellHtml(line)}</td>
            <td>${getAwObservationsCellHtml(line)}</td>
            <td class="aw-col-status">${getAwStatusBadgeHtml(line.status)}</td>
        </tr>
    `;
}

// Only rendered when line_count > 1 — a single-line voucher's own line row already carries the
// voucher's whole amount/status, so a subtotal would just duplicate it. This row is NOT covered
// by the line rows' rowspan above (that span ends exactly at line_count rows, right before this
// one) - so it must independently supply its own cells for the same 5 leading logical columns
// (Sr+Voucher No.+Date+Account+Reference) via colspan="5" — a bare, unlabelled cell (per the
// confirmed v7-prototype fix removing the old "Subtotal" text) that still reads as coloured band
// since .aw-subtotal-row td's background rule applies to every <td> in the row including this
// one, not just blank/bare space. Carries the same groupParityClass as its group's line rows for
// consistency, though style.css's zebra rule is scoped to .aw-line-row only, so it has no visual
// effect here — the subtotal keeps its own distinct #dbeafe tint regardless of parity.
function buildSubtotalRowHtml(v, groupParityClass) {
    return `
        <tr class="aw-subtotal-row ${groupParityClass}">
            <td colspan="5"></td>
            <td class="aw-col-amount">${formatAwAmount(v.amount)}</td>
            <td colspan="2"></td>
            <td class="aw-col-status">${getAwStatusBadgeHtml(v.status)}</td>
        </tr>
    `;
}

function renderAuditWorkbookRows(vouchers, emptyMessage) {
    if (!vouchers || vouchers.length === 0) {
        awTableBody.innerHTML = `
            <tr><td colspan="9" style="text-align:center; color:#94a3b8;">${escapeHtml(emptyMessage)}</td></tr>
        `;
        return;
    }

    const rowsHtml = vouchers.map((v, groupIdx) => {
        const groupParityClass = groupIdx % 2 === 0 ? 'aw-group-even' : 'aw-group-odd';
        const lineRowsHtml = (v.lines || []).map((line, idx) => buildLineRowHtml(v, line, idx === 0, groupParityClass)).join('');
        const subtotalHtml = v.line_count > 1 ? buildSubtotalRowHtml(v, groupParityClass) : '';
        return lineRowsHtml + subtotalHtml;
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

// Spinner overlay show/hide. The overlay is visible by default in the HTML (covers page paint
// before the fetch even starts) — showSpinnerOverlay() here is a defensive re-show in case a
// future caller re-fetches after an earlier hide; hideSpinnerOverlay() is the one call that
// matters, and it must only ever be reached AFTER rows are inserted and positioning is complete
// (see fetchAuditWorkbook() below) — that ordering, not this function itself, is what satisfies
// the acceptance criterion.
function showSpinnerOverlay() {
    if (awSpinnerOverlay) awSpinnerOverlay.classList.remove('hidden');
}
function hideSpinnerOverlay() {
    if (awSpinnerOverlay) awSpinnerOverlay.classList.add('hidden');
}

async function fetchAuditWorkbook(sessionId, targetVoucherNo) {
    awSubtitleEl.textContent = 'Loading vouchers…';
    setChipsDisabled(true);
    showSpinnerOverlay();

    try {
        const res = await fetch(`${API_BASE}/api/verification/session/${sessionId}/audit_workbook`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch Audit Workbook data');
        }
        const data = await res.json();
        allVouchers = data.vouchers || [];
        expandedObservationDetailIds = new Set();
        // Procedures-pill wiring phase: session client + per-line resolved-Category state, built
        // fresh from this fetch — awVoucherLineState/awProcedureModalState mirror this response's
        // own lifetime (a fresh session fetch replaces both, same as allVouchers).
        sessionClientId = data.client_id || null;
        // A fresh session fetch starts a fresh guidance/Rulebook lifetime. This also keeps a
        // future in-place refresh from carrying a prior session's compiled rules forward.
        awSessionRulebook = null;
        awSessionRulebookPromise = null;
        awAuditGuidanceCache = {};
        awVoucherLineState = {};
        allVouchers.forEach(v => {
            (v.lines || []).forEach(line => {
                awVoucherLineState[line.detail_id] = { transactionType: line.category_name || '' };
            });
        });
        awProcedureModalState = {};
        awSubtitleEl.textContent = `${data.total_vouchers || 0} voucher(s), ${data.total_lines || 0} line(s)`;
        setChipsDisabled(false);
        currentFilterStatus = 'all';
        // Rows are inserted, and the target row (if any) is instantly scrolled into view + flash-
        // highlighted, all while the spinner overlay still fully covers the table — the user's
        // first frame without the overlay is only reached at hideSpinnerOverlay() below, once both
        // are already done (confirmed acceptance criterion). No await/setTimeout between any of
        // these three calls.
        applyFilterAndRender();
        positionOnTargetVoucher(targetVoucherNo);
        hideSpinnerOverlay();
    } catch (error) {
        console.error('Audit Workbook fetch error:', error);
        awSubtitleEl.textContent = '❌ Error: ' + error.message;
        allVouchers = [];
        setChipsDisabled(true);
        renderAuditWorkbookRows([], 'Error loading vouchers. Please try again.');
        awTotalAmountEl.textContent = formatAwAmount(0);
        hideSpinnerOverlay();
    }
}

// ---------- VOUCHER JUMP TARGET (Phase 2 of "Voucher No. → Audit Workbook") ----------

// Finds the target voucher's first line row (the one holding the Sr/Voucher No./Date/Account
// rowspan cells) by exact attribute comparison rather than a CSS attribute-selector string, to
// avoid needing to escape special characters that might appear in a real voucher number.
// Subtotal rows carry no data-voucher-no and line rows are in DOM order (first line before any
// continuation lines), so the first match is always the group's first row.
function findVoucherRow(voucherNo) {
    if (!voucherNo) return null;
    const rows = awTableBody.querySelectorAll('tr[data-voucher-no]');
    for (const row of rows) {
        if (row.getAttribute('data-voucher-no') === voucherNo) return row;
    }
    return null;
}

// Instant jump + highlight flash. behavior:'auto' (not 'smooth') per the confirmed requirement —
// an instant jump, not an animated scroll. A voucher_no with no match in this session (stale
// link, wrong session) fails soft: the page just stays at the top, no error shown.
function positionOnTargetVoucher(voucherNo) {
    if (!voucherNo) return;
    const row = findVoucherRow(voucherNo);
    if (!row) return;
    row.scrollIntoView({ behavior: 'auto', block: 'center' });
    row.classList.add('aw-voucher-jump-highlight');
    setTimeout(() => row.classList.remove('aw-voucher-jump-highlight'), 2000);
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

// ---------- PROCEDURE MODAL ADAPTER (Procedures-pill wiring phase) ----------
// Wires a line's Procedures pill to the shared static/procedure_modal.js component (the same
// modal Verification uses) via this page's OWN buildProcedureModalAdapter()-equivalent, per
// procedure_modal.js's Phase 1b adapter contract — see that file's own header comment for the
// full property list/reasoning. This does NOT reuse verification.js's adapter or its
// saveLineItems() (that function also does Verification-page-specific UI work — cashbook
// preview, sessionTransactions patching — that doesn't apply here); everything below is this
// page's own lean, direct-to-backend equivalent. Saving here has no effect on Verification's own
// grid/state; the reverse is also true (Verification never reads any of this page's state).

// Status feedback, same shape as verification.js's own showStatus() — see #awStatus in
// audit_workbook.html.
function awShowStatus(msg, type = 'info') {
    if (!awStatusEl) return;
    awStatusEl.textContent = msg;
    awStatusEl.className = 'upload-status ' + type;
    awStatusEl.style.display = 'block';
    clearTimeout(window.awStatusTimeout);
    window.awStatusTimeout = setTimeout(() => {
        if (awStatusEl) awStatusEl.style.display = 'none';
    }, 5000);
}

// Finds a line + its owning voucher group anywhere in allVouchers, by detail_id (String-compared,
// same convention procedure_modal.js itself uses, since a uid arriving from a DOM dataset is
// always a string while detail_id is a number straight from JSON).
function awFindLineByDetailId(uid) {
    for (const v of allVouchers) {
        const line = (v.lines || []).find(l => String(l.detail_id) === String(uid));
        if (line) return { voucher: v, line };
    }
    return null;
}

// Mirrors the backend's own _rollup_voucher_status() rule exactly (routers/verification.py,
// documented in this project's CLAUDE.md Architecture notes): all lines Cleared -> Cleared; else
// any line Query -> Query; else Pending (covers null/unclassified) — used to keep a voucher
// group's own rollup status in sync client-side after a single line's status changes, without a
// full session refetch.
function awRollupVoucherStatus(statuses) {
    if (!statuses.length) return 'Pending';
    if (statuses.every(s => s === 'Cleared')) return 'Cleared';
    if (statuses.some(s => s === 'Query')) return 'Query';
    return 'Pending';
}

// Small local duplicates of the pure (no-Verification-state) helpers procedure_modal.js's adapter
// contract requires — same "visually matched, physically duplicated" convention this file already
// follows for buildProcedureRingSvg() above, since verification.js itself isn't loaded on this page.

function getAwLineAlias(row, aliases) {
    if (!row) return '—';
    for (const alias of aliases) {
        if (row[alias] !== undefined && row[alias] !== null && row[alias] !== '') return row[alias];
    }
    return '—';
}

const AW_ACCOUNT_NAME_ALIASES = ['Account Name'];
const AW_DESCRIPTION_ALIASES = ['Description'];

// Reads the synthesized row_data's single Amount key (see buildAuditWorkbookAdapter()'s
// currentVoucherLines below) — this page's lines are already resolved to one plain amount
// (client_data.extract_amount), so there's no real Debit/Credit alias set to match, unlike
// verification.js's own getVoucherLineAmount().
function getAwVoucherLineAmount(row) {
    if (!row) return '';
    return (row['Debit Amount'] !== undefined && row['Debit Amount'] !== null && row['Debit Amount'] !== '')
        ? row['Debit Amount'] : '';
}

function parseAwVoucherAmount(value) {
    if (value === '' || value === undefined || value === null) return 0;
    const n = parseFloat(String(value).replace(/,/g, ''));
    return isNaN(n) ? 0 : n;
}

const AW_RISK_RATING_ORDER = { Low: 1, Medium: 2, High: 3 };
// Verbatim logic copy of verification.js's computeLineRiskData() — used by the modal's header
// meta line (Line N of M — account — Type · Amount · Risk: ...).
function awComputeLineRiskData(auditGuidanceData) {
    const documents = (auditGuidanceData && auditGuidanceData.documents) || [];
    let risk = null;
    documents.forEach(doc => {
        if (doc.risk_rating && (!risk || AW_RISK_RATING_ORDER[doc.risk_rating] > AW_RISK_RATING_ORDER[risk])) {
            risk = doc.risk_rating;
        }
    });
    const tooltip = documents
        .filter(doc => (doc.auth_roles || []).length)
        .map(doc => `${doc.name}: ${doc.auth_roles.join(', ')}`)
        .join('\n');
    return { risk, tooltip };
}

const AW_LSR_GATE_ICON = { pass: '✔', fail: '✘', not_applicable: '–' };
// Verbatim logic copy of verification.js's renderGateRowHtml() — the Validate panel inside the
// modal (procedure_modal.js's toggleValidatePanel()) calls hostAdapter.renderGateRowHtml per gate.
function awRenderGateRowHtml(g) {
    return `
        <div class="lsr-gate ${g.status}">
            <div class="lsr-gate-label"><span class="lsr-gate-icon">${AW_LSR_GATE_ICON[g.status] || '–'}</span>${escapeHtml(g.label)}</div>
            ${g.detail ? `<div class="lsr-gate-detail">${escapeHtml(g.detail)}</div>` : ''}
        </div>
    `;
}

// Category guidance cache, same shape/contract as verification.js's auditGuidanceCache —
// forceRefresh (reserved by the shared adapter contract) bypasses the category cache, while the
// session Rulebook itself remains one request per Audit Workbook session.
let awAuditGuidanceCache = {};

async function awLoadSessionRulebook() {
    if (awSessionRulebook) return awSessionRulebook;
    if (awSessionRulebookPromise) return awSessionRulebookPromise;
    if (!currentSessionId) throw new Error('Audit Workbook session is not available.');

    const sessionIdAtStart = currentSessionId;
    awSessionRulebookPromise = window.ApiUtils.apiFetch(
        `${API_BASE}/api/verification/session/${sessionIdAtStart}/rulebook`
    ).then(async res => {
        if (!res.ok) {
            let detail = 'Unable to load the session Rulebook.';
            try {
                const error = await res.json();
                detail = error.detail || detail;
            } catch (_) {
                // Keep the stable user-facing error when the response is not JSON.
            }
            throw new Error(detail);
        }
        const payload = await res.json();
        const rulebook = payload && payload.rulebook;
        if (!rulebook || !Array.isArray(rulebook.categories)) {
            throw new Error('The session Rulebook response is invalid.');
        }
        if (currentSessionId === sessionIdAtStart) awSessionRulebook = rulebook;
        return rulebook;
    }).finally(() => {
        awSessionRulebookPromise = null;
    });
    return awSessionRulebookPromise;
}

// The compiled Rulebook deliberately uses a richer, normalized shape than the historical
// audit_guidance endpoint. Convert only at this adapter boundary so procedure_modal.js and the
// existing Audit Workbook badge logic continue to receive their established contract.
function awRulebookCategoryToGuidance(profile) {
    const documents = (profile.documents || []).map(document => ({
        ...document,
        id: document.document_id,
        auth_roles: (document.authority_roles || []).map(role => role.role_name || role),
        cross_check_fields: (document.fields || []).map(field => field.field_name || field)
    }));
    const documentNameById = {};
    documents.forEach(document => { documentNameById[document.id] = document.name; });
    return {
        transaction_type: profile.name,
        category_id: profile.category_id,
        category_name: profile.name,
        kb_overview: profile.kb_overview,
        matrix_version: profile.matrix_version,
        documents,
        cross_checks: (profile.cross_checks || []).map(check => ({
            id: check.cross_check_id,
            name: check.name,
            compare_field: check.compare_field,
            comparison_type: check.comparison_type,
            documents: (check.document_ids || []).map(id => documentNameById[id]).filter(Boolean)
        }))
    };
}

async function awFetchAuditGuidanceData(type, forceRefresh = false) {
    if (!forceRefresh && Object.prototype.hasOwnProperty.call(awAuditGuidanceCache, type)) {
        return awAuditGuidanceCache[type];
    }
    const rulebook = await awLoadSessionRulebook();
    const profile = rulebook.categories.find(category => category.name === type);
    const data = profile ? awRulebookCategoryToGuidance(profile) : null;
    awAuditGuidanceCache[type] = data;
    return data;
}

// Voucher-level batched supporting-data cache — the direct analog of verification.js's
// voucherSupportCache / voucherSupportLoading / invalidateVoucherSupportCache(). One
// GET /api/verification/voucher_support_data/{anchor} (anchor = the voucher group's first
// line's detail_id) returns { lines: { "<detail_id>": { linked_documents, procedure_checks,
// observations, physical_bills } } } for every line in that voucher — replacing the four
// uncached per-line fetches this file used to make on every modal close. The endpoint already
// resolves sibling-session ids and shapes physical bills identically to the standalone routes
// (see Phase 2a discovery), so callers read the same arrays they did before.
//
// Keyed by anchor detail_id: navigating to a different voucher naturally misses and re-fetches.
// awInvalidateVoucherSupportCache() MUST run after any successful save (see awSaveLineItems())
// — otherwise hideProcedureModalOnly()'s post-save badge refresh serves the pre-save copy and
// the Procedures/Observations badges show stale state until the page is reloaded.
let awVoucherSupportCache = null;         // { voucher_line_id, transaction_ref, lines: {...} } | null
let awVoucherSupportLoading = null;       // in-flight Promise<lines-map> | null
let awVoucherSupportLoadingAnchor = null; // anchor detail_id the in-flight fetch is for

// Returns the { "<detail_id>": {...} } map for the voucher whose first line is anchorUid.
// Fail-soft to {} so a fetch hiccup never breaks a badge cell. The in-flight guard only reuses
// a pending promise when it's for the SAME anchor — unlike verification.js (one voucher on
// screen at a time), this page can refresh lines from different vouchers, so a blind reuse
// could hand back another voucher's data.
async function awLoadVoucherSupportData(anchorUid) {
    if (!anchorUid) return {};
    if (awVoucherSupportCache && String(awVoucherSupportCache.voucher_line_id) === String(anchorUid)) {
        return awVoucherSupportCache.lines || {};
    }
    if (awVoucherSupportLoading && String(awVoucherSupportLoadingAnchor) === String(anchorUid)) {
        return awVoucherSupportLoading;
    }
    awVoucherSupportLoadingAnchor = anchorUid;
    awVoucherSupportLoading = window.ApiUtils.apiFetch(`${API_BASE}/api/verification/voucher_support_data/${anchorUid}`)
        .then(async res => {
            if (!res.ok) return {};
            const data = await res.json();
            awVoucherSupportCache = data;
            return data.lines || {};
        })
        .catch(() => ({}))
        .finally(() => { awVoucherSupportLoading = null; awVoucherSupportLoadingAnchor = null; });
    return awVoucherSupportLoading;
}

function awInvalidateVoucherSupportCache() {
    awVoucherSupportCache = null;
    awVoucherSupportLoading = null;
    awVoucherSupportLoadingAnchor = null;
}

// The direct-backend-call replacement for verification.js's saveLineItems() — flushes any dirty
// observation/no-match-note text (flushProcedureObservations()/flushAccountHeadNoMatchNote() are
// plain globals already declared in procedure_modal.js, reachable by name since it loads before
// this file — same classic-script shared-scope convention procedure_modal.js's own header comment
// documents), then POSTs the same save_line_items endpoint verification.js ultimately uses. On
// success, patches this line's status + its voucher group's rollup status in allVouchers and
// re-renders — the Status-column half of "row/pill refreshes after save".
async function awSaveLineItems(uid) {
    if (!uid) return { ok: false, error: { detail: 'Missing line identifier.' } };
    await flushProcedureObservations(uid);
    await flushAccountHeadNoMatchNote(uid);
    try {
        const payload = {
            detail_id: uid,
            line_items: [],
            transaction_type: (awVoucherLineState[uid] && awVoucherLineState[uid].transactionType) || null,
            procedure_data: {}
        };
        const res = await fetch(`${API_BASE}/api/verification/save_line_items`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to save');
        }
        const data = await res.json();
        if ('cleared_status' in data) {
            const found = awFindLineByDetailId(uid);
            if (found) {
                found.line.status = data.cleared_status || 'Pending';
                found.voucher.status = awRollupVoucherStatus((found.voucher.lines || []).map(l => l.status));
                applyFilterAndRender();
            }
        }
        // The line (incl. its flushed observation text) is now persisted — drop the voucher-wide
        // support cache so the post-save badge refresh (hideProcedureModalOnly() →
        // awRefreshLineProcedures/Observations) re-reads saved state instead of the pre-save copy.
        // Mirrors invalidateVoucherSupportCache() in verification.js. Single chokepoint — covers
        // the adapter's saveLineItems / saveAllVoucherLines and the Verify fallback save.
        awInvalidateVoucherSupportCache();
        return { ok: true, data };
    } catch (e) {
        console.error('Error saving line (Audit Workbook):', e);
        awShowStatus('Could not save changes for this line — please try again.', 'error');
        return { ok: false, error: { detail: e.message || 'Failed to save line.' } };
    }
}

// The "row/pill refreshes after save" mechanism — called by procedure_modal.js's
// hideProcedureModalOnly() unconditionally on every close path (✕, Verify & Next/Close, plain
// Verify). Duplicates the same isDocDone()-style completion rule verification.js's own
// updateProceduresCellForLine() applies (physical_bill exact-amount match / template-linked
// evidence presence / no-template role ticks, gated on voucher-level checks when
// transaction_level_approval is set) — the exact rule the backend's own _compute_line_procedures()
// documents itself as matching, so this refresh can never disagree with a later full page reload.
// Patches allVouchers in place and re-renders from that already-patched in-memory data (no network
// re-fetch of the whole session) — a full local re-render rather than a surgical single-row DOM
// patch, since Sr/Voucher No./Date/Account are real rowspan cells that make a targeted patch
// fragile; the actual DATA change stays scoped to the one line, satisfying "no effect on
// Verification's grid" (nothing here ever touches Verification's own state/DOM).
async function awRefreshLineProcedures(uid, type) {
    const found = awFindLineByDetailId(uid);
    if (!found) return;
    const { voucher, line } = found;
    const category = type || (awVoucherLineState[uid] && awVoucherLineState[uid].transactionType);
    if (!category) {
        line.procedures = { done: 0, total: 0 };
        applyFilterAndRender();
        return;
    }
    let guidance = null;
    try {
        guidance = await awFetchAuditGuidanceData(category);
    } catch (e) {
        console.error('Failed to refresh Procedures cell:', e);
        return;
    }
    const docs = (guidance && guidance.documents) || [];
    if (!docs.length) {
        line.procedures = { done: 0, total: 0 };
        applyFilterAndRender();
        return;
    }
    const needsVoucherChecks = docs.some(doc => doc.transaction_level_approval);
    // One voucher-level batched fetch replaces the former per-line
    // awFetchAssignedDocumentRecords / awFetchProcedureChecksForLine / awFetchPhysicalBillsForLine —
    // anchor is the voucher group's first line, result indexed by this line's own detail_id.
    // Physical bills come back unconditionally (one request regardless), so there's no
    // needsPhysicalBills gate anymore.
    const anchorUid = (voucher.lines && voucher.lines[0] && voucher.lines[0].detail_id) || uid;
    const [supportLines, checks] = await Promise.all([
        awLoadVoucherSupportData(anchorUid),
        // ensureVoucherChecksLoaded() is a plain global already declared in procedure_modal.js
        // (pure fetch+cache, no hostAdapter dependency of its own) — reused directly by name.
        needsVoucherChecks ? ensureVoucherChecksLoaded(sessionClientId, voucher.voucher_no) : Promise.resolve(null)
    ]);
    const lineSupport = supportLines[String(uid)] || {};
    const assignedRecords = (lineSupport.linked_documents && lineSupport.linked_documents.documents) || [];
    const procedureChecks = (lineSupport.procedure_checks && lineSupport.procedure_checks.checks) || [];
    const physicalBills = (lineSupport.physical_bills && lineSupport.physical_bills.bills) || [];
    const assignedDocIds = new Set(assignedRecords.map(r => r.document_id));
    const voucherLineAmount = parseAwVoucherAmount(line.amount);
    const isDocDone = doc => {
        let base;
        if (doc.document_type === 'physical_bill') {
            // Multi-Bill/Multi-Line Physical Bill: a Document can have several bills, each with
            // several items — sum every matching bill's server-computed bill_total (null when a
            // bill has no items or any item amount isn't numeric), matching verification.js's
            // updateProceduresCellForLine() exactly. The old single-bill .find()/bill.amount
            // check always failed here (the shaped bill has no `amount` field) — see KNOWN_ISSUES.
            const docBills = physicalBills.filter(b => b.document_id === doc.id);
            const headersComplete = docBills.length > 0 && docBills.every(bill =>
                (bill.bill_no || '').trim() && (bill.bill_date || '').trim()
                && (bill.vendor_name || '').trim() && bill.bill_total != null
            );
            base = headersComplete && Math.abs(docBills.reduce((sum, bill) => sum + bill.bill_total, 0) - voucherLineAmount) <= 0.005;
        } else if (doc.template_id) {
            base = assignedDocIds.has(doc.id);
        } else {
            const roles = doc.auth_roles || [];
            base = roles.length > 0 && roles.every(role => procedureChecks.some(c => c.document_id === doc.id && c.role_name === role && c.is_checked));
        }
        if (!base) return false;
        // isVoucherChecksComplete() is a plain global already declared in procedure_modal.js.
        if (doc.transaction_level_approval) return isVoucherChecksComplete(checks);
        return true;
    };
    const done = docs.filter(isDocDone).length;
    line.procedures = { done, total: docs.length };
    applyFilterAndRender();
}

// Companion to awRefreshLineProcedures() above, same call/patch/re-render shape — mirrors
// verification.js's own updateObservationsCellForLine() nonEmpty filter and document-name
// resolution (names aren't on the observation entry itself, resolved from the same cached
// guidance fetch).
async function awRefreshLineObservations(uid, type) {
    const found = awFindLineByDetailId(uid);
    if (!found) return;
    const { voucher, line } = found;
    // Same voucher-level batched fetch as awRefreshLineProcedures() — the endpoint returns each
    // line's observation entries alongside its documents/checks/bills, so no separate call.
    const anchorUid = (voucher.lines && voucher.lines[0] && voucher.lines[0].detail_id) || uid;
    const supportLines = await awLoadVoucherSupportData(anchorUid);
    const lineSupport = supportLines[String(uid)] || {};
    const entries = (lineSupport.observations && lineSupport.observations.entries) || [];
    const nonEmpty = entries.filter(e => (e.observation_text || '').trim());
    line.observations = {
        count: nonEmpty.length,
        entries: nonEmpty.map(e => ({ document_id: e.document_id, document_name: null, text: e.observation_text }))
    };
    const category = type || (awVoucherLineState[uid] && awVoucherLineState[uid].transactionType);
    if (category && nonEmpty.length) {
        try {
            const guidance = await awFetchAuditGuidanceData(category);
            const nameById = {};
            ((guidance && guidance.documents) || []).forEach(doc => { nameById[doc.id] = doc.name; });
            line.observations.entries.forEach(e => { e.document_name = nameById[e.document_id] || null; });
        } catch (e) {
            console.error('Failed to resolve document names for Observations refresh:', e);
        }
    }
    applyFilterAndRender();
}

// Builds a fresh adapter closing over the specific voucher group the clicked line belongs to —
// mirrors verification.js's buildProcedureModalAdapter()'s own "fresh per real open" convention.
// See procedure_modal.js's header comment for the full contract each property below satisfies.
function buildAuditWorkbookAdapter(voucherGroup) {
    return {
        getClientId: () => sessionClientId,
        // Cosmetic only (drives the footer button's "Verify & Next" vs "Verify & Close" label) —
        // position within the whole session's voucher list, not line position within a voucher.
        getVoucherPosition: () => {
            const idx = allVouchers.indexOf(voucherGroup);
            return { index: idx === -1 ? 0 : idx + 1, total: allVouchers.length };
        },
        showStatus: awShowStatus,
        saveLineItems: awSaveLineItems,
        saveAllVoucherLines: async () => {
            for (const line of (voucherGroup.lines || [])) {
                await awSaveLineItems(line.detail_id);
            }
        },
        escapeHtml,
        getVoucherLineAlias: getAwLineAlias,
        getVoucherLineAmount: getAwVoucherLineAmount,
        renderGateRowHtml: awRenderGateRowHtml,
        fetchAuditGuidanceData: awFetchAuditGuidanceData,
        updateProceduresCellForLine: awRefreshLineProcedures,
        updateObservationsCellForLine: awRefreshLineObservations,
        parseVoucherAmount: parseAwVoucherAmount,
        computeLineRiskData: awComputeLineRiskData,
        // Audit Workbook has no session-progress display to refresh.
        updateProgressDisplay: async () => {},
        // Audit Workbook already shows every voucher in one table — there is no separate
        // "current voucher" page view to re-render into on the rare "advance past this voucher"
        // branch (verifyAndAdvanceVoucher() in procedure_modal.js). The line that was just
        // verified is already refreshed via updateProceduresCellForLine/updateObservationsCellForLine
        // above regardless of this branch, so a no-op here loses nothing.
        applyVoucherNavRenderData: () => {},
        get VOUCHER_LINE_ACCOUNT_NAME_ALIASES() { return AW_ACCOUNT_NAME_ALIASES; },
        get VOUCHER_LINE_DESCRIPTION_ALIASES() { return AW_DESCRIPTION_ALIASES; },
        get currentVoucherId() { return voucherGroup.voucher_no; },
        get currentSessionId() { return currentSessionId; },
        // Synthesizes a row_data shape satisfying getVoucherLineAlias()/getVoucherLineAmount()'s
        // alias lookups from this page's already-resolved line fields — there is no real Excel
        // row_data here (unlike verification.js's currentVoucherLines, which comes straight from
        // client_data).
        get currentVoucherLines() {
            return (voucherGroup.lines || []).map(line => ({
                detail_id: line.detail_id,
                client_data_id: line.client_data_id,
                row_data: {
                    'Account Name': line.account_name || line.account_code || '',
                    'Description': line.description || '',
                    'Debit Amount': line.amount
                }
            }));
        },
        get voucherLineState() { return awVoucherLineState; },
        get procedureModalState() { return awProcedureModalState; }
    };
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
    // Procedures-pill wiring phase: checked first so a click on the badge opens the shared
    // procedure modal instead of falling through to the Observations toggle or row-navigate
    // branches below.
    const procTarget = e.target.closest('.voucher-line-procedures-badge');
    if (procTarget) {
        e.stopPropagation();
        const detailId = procTarget.getAttribute('data-detail-id');
        const found = awFindLineByDetailId(detailId);
        if (found) {
            openProcedureModal(String(found.line.detail_id), null, buildAuditWorkbookAdapter(found.voucher), procTarget);
        }
        return;
    }

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
    // Optional companion param (Phase 2) from the Verification-side link — the specific voucher
    // to land on once the fetch resolves. See positionOnTargetVoucher() above.
    const targetVoucherNo = params.get('voucher');

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
        // Global navbar Up icon: this page's structural parent is the Verification session it was
        // opened from — same target as the peer-nav button above. No in-page views to distinguish
        // here (one table, one screen), so pushInPageNavEntry()/setInPageRestoreHandler() aren't
        // wired on this page — there's nothing for them to do.
        if (typeof window.setUpOverride === 'function') {
            window.setUpOverride(() => {
                window.location.href = `/static/verification.html?state=session&id=${currentSessionId}`;
            });
        }
        fetchAuditWorkbook(sessionId, targetVoucherNo);
    } else {
        currentSessionId = null;
        allVouchers = [];
        sessionClientId = null;
        awSessionRulebook = null;
        awSessionRulebookPromise = null;
        awAuditGuidanceCache = {};
        awVoucherLineState = {};
        awProcedureModalState = {};
        if (awVerificationBtn) awVerificationBtn.disabled = true;
        awSubtitleEl.textContent = 'No session specified — open this page via an Audit Workbook link from Verification.';
        setChipsDisabled(true);
        renderAuditWorkbookRows([], 'No session specified.');
        awTotalAmountEl.textContent = formatAwAmount(0);
        // No fetch happens on this branch — the overlay (visible by default) must be hidden here,
        // otherwise it would stay stuck covering the table forever.
        hideSpinnerOverlay();
    }
};
