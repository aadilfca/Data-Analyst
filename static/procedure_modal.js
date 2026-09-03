// ==============================================
// PROCEDURE MODAL — shared Supporting-Document/procedure modal component
// Originally extracted verbatim from static/verification.js (pure relocation, no behavior change)
// so it could be reused by other pages (e.g. Audit Workbook's Procedures pill) without duplicating
// this markup/logic. Self-installing: this classic (non-module) <script> injects its own markup
// (#procedureModal, #procedureBrowseModal, #voucherVerifyToast) into <body> and wires its own DOM
// refs/listeners the moment it loads — a consuming page needs nothing beyond
// <script src="/static/procedure_modal.js">.
//
// Phase 1b (adapter refactor): that first extraction still read its host page's globals directly
// (both files were classic <script> tags sharing one Global Environment Record) — workable for
// Verification, which always loads both files together, but not portable to a page like Audit
// Workbook that has no verification.js-shaped session/voucher-navigation state at all. This file
// now receives every one of those external dependencies through an explicit adapter object,
// stored in the module-level `hostAdapter` below, rather than reading bare globals.
//
// openProcedureModal(uid, initialDocId, adapter) is the sole place `hostAdapter` is set — its
// first statement is `if (adapter) hostAdapter = adapter;`. A host page (verification.js today;
// Audit Workbook's own equivalent in a later phase) builds a fresh adapter and passes it on every
// REAL open call. procedure_modal.js's own internal self-reopen call (line-to-line stepping
// inside verifyAndAdvanceVoucher()) omits the 3rd argument entirely — the guard above then simply
// leaves `hostAdapter` as whatever was last set, which is always already correct since the modal
// can only reach that internal call while it's already open. The adapter's live-state properties
// (hostAdapter.currentVoucherId/hostAdapter.currentSessionId/hostAdapter.currentVoucherLines/hostAdapter.voucherLineState/hostAdapter.procedureModalState) are
// `get` accessors, not plain values, so they always resolve the host page's CURRENT live value at
// read-time — safe regardless of how long ago the adapter object itself was constructed, since
// verification.js reassigns several of these elsewhere (voucher navigation, seedVoucherLineState())
// independent of this modal. See buildProcedureModalAdapter() in verification.js for the adapter's
// exact shape/construction and the reasoning behind each property (function ref vs. getter).
//
// `escapeHtml` is one of the adapted properties, not a locally-duplicated copy, specifically
// because a same-named top-level `function` declaration in two classic <script> files sharing one
// global scope doesn't throw (unlike `let`/`const`) — the later-loaded file's declaration would
// silently win and shadow this file's own, defeating a "local copy." `API_BASE` is NOT adapted —
// it's a page-level global from client_context.js that every host page already loads on its own,
// not a verification.js-specific dependency.
//
// `currentProcedureModalUid` and `voucherChecksCache`/`voucherChecksLoadingPromises` are this
// file's OWN module-local state (declared below), not part of the adapter — nothing outside this
// file ever reads or writes them, so there was nothing to decouple.
//
// In the other direction, verification.js's hostAdapter.saveLineItems() calls this file's
// flushProcedureObservations()/flushAccountHeadNoMatchNote() directly (unchanged, still resolved
// via shared classic-script scope, since those two functions' NAMES still need to be callable from
// verification.js) — both guard on `if (!hostAdapter) return;` as their first statement, since a
// line can be saved (and these get called) before the modal has ever been opened this page-load,
// when hostAdapter is still null. That guard is provably a no-op-for-a-no-op: whenever hostAdapter
// is null, the modal was never opened for any line, so hostAdapter.procedureModalState/accountHeadCache would
// have been empty anyway — the exact same no-op path these functions already take today.
// ==============================================

const PROCEDURE_MODAL_MARKUP = `
        <!-- SUPPORTING DOCUMENTS MODAL (left-side document rail)  -->
        <!-- Voucher Supporting-Document Linking Phases 3-6 built this as a top tab bar; the       -->
        <!-- Supporting Documents redesign's Phase 1 restructured it into a left rail instead -     -->
        <!-- one entry per document belonging to the voucher line's Category, listed vertically.   -->
        <!-- Rail (#procedureModalTabs) and panel (#procedureModalPanels) are still populated       -->
        <!-- entirely by JS in verification.js (renderProcedureModalTabs()/renderProcedureModalPanel()) -->
        <!-- - shell/CSS lives in the #procedureModal rules in style.css, per this file's documented -->
        <!-- per-modal-scoped .modal-content/.modal-header convention (see #supportingModal/         -->
        <!-- #dfdModal above). Each document's panel renders a search box/results dropdown/selected- -->
        <!-- lines table (templated documents), an Audit Procedures checklist, and an Observation    -->
        <!-- section - no per-document static markup here, all JS-rendered.                          -->
        <!-- ============================================= -->
        <div id="procedureModal">
            <div class="modal-content">
                <div class="modal-header">
                    <div class="procedure-modal-heading">
                        <h3 id="procedureModalTitle">📎 Supporting Documents</h3>
                        <div id="procedureModalMeta" class="procedure-modal-meta"></div>
                    </div>
                    <button type="button" class="btn-ghost procedure-validate-btn" id="procedureModalValidateBtn">🔍 Validate</button>
                    <button class="close-btn" id="procedureModalClose"
                        style="background:none; border:none; font-size:28px; cursor:pointer; color:#94a3b8;">&times;</button>
                </div>
                <!-- Persistent voucher-level checks bar (Generic Voucher-Level Checks cutover): shown
                     once per voucher, above the document rail — not duplicated per tab/line, since
                     these checks (audit_rule_voucher_checks/voucher_check_status) apply to the whole
                     voucher, not one line. Fully JS-rendered by renderVoucherChecksBar() in
                     verification.js, same "no static per-item markup" convention as the rail/panels
                     below. -->
                <!-- Confirmed fix: structural header freeze. Everything that can grow tall (voucher
                     checks bar, Validate panel, rail+panels) is wrapped in its own scrolling region
                     (.procedure-modal-scroll-body, style.css) so .modal-header/.procedure-modal-footer
                     never scroll with it and body content can never render above the header — see the
                     style.css comment on .modal-content for the full flex-column layout this enables.
                     Supersedes an earlier position:sticky attempt on .modal-header alone. -->
                <div class="procedure-modal-scroll-body">
                    <div id="procedureVoucherChecksBar" class="procedure-voucher-checks-bar"></div>
                    <!-- "Why is this line Cleared/Query?" inline panel (Validate button, above) — same
                         GET /api/verification/line_status_reason/{id} payload and gate-row rendering as
                         the Status-pill popover in the main voucher table (buildLineStatusReasonBody() in
                         verification.js, reused here so there is exactly one gate-list rendering path).
                         Collapsed by default; toggled open/closed by #procedureModalValidateBtn via
                         toggleValidatePanel(). Content is 100% JS-rendered. -->
                    <div id="procedureValidatePanel" class="procedure-validate-panel" style="display:none;"></div>
                    <div class="procedure-modal-body">
                        <div id="procedureModalTabs" class="procedure-doc-rail"></div>
                        <div id="procedureModalPanels" class="procedure-tab-panels"></div>
                    </div>
                </div>
                <!-- Footer (Supporting Documents modal redesign follow-up): saved-state indicator +
                     Back/Next to move between this voucher's OTHER lines without closing the modal -
                     distinct from the rail's tab switching, which moves between one line's Documents.
                     See navigateProcedureModalLine()/updateProcedureModalSavedNote() in verification.js. -->
                <div class="procedure-modal-footer">
                    <span id="procedureModalSavedNote" class="procedure-modal-saved-note"></span>
                    <div class="procedure-modal-footnav">
                        <button type="button" id="procedureModalBackBtn" class="btn-ghost">← Back</button>
                        <!-- Plain "Verify" (confirmed requirement): same save+verify action as Verify & Next
                             below, but never auto-advances — always closes back onto the grid at the current
                             voucher. Shares the same disabled/title gate as Verify & Next
                             (updateVerifyFooterButton() in verification.js), since both hit the same backend
                             precondition. -->
                        <button type="button" id="procedureModalVerifyOnlyBtn" class="btn-ghost">✅ Verify</button>
                        <!-- Voucher-level Checks simplified to name-only (Phase 3 of 4): replaces the old
                             per-line "Next →" nav. Label/disabled state are fully JS-driven
                             (updateVerifyFooterButton() in verification.js) — see that function for why. -->
                        <button type="button" id="procedureModalVerifyBtn" class="btn-verify">✅ Verify & Next</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- ============================================= -->
        <!-- SUPPORTING DOCUMENTS BROWSE MODAL             -->
        <!-- Voucher Supporting-Document Linking, Phase 4 of 6: nested on top of #procedureModal, -->
        <!-- opened by a tab's 👁️ preview icon when the analyst doesn't have a search term -      -->
        <!-- a paginated, read-only browse of GET /api/data/preview for that tab's document,      -->
        <!-- same select/grey-out row treatment as the search dropdown                            -->
        <!-- (buildProcedureResultRowHtml() in verification.js). Content is 100% JS-rendered;      -->
        <!-- Prev/Next reuse the app's shared .btn-pagination class (style.css).                   -->
        <!-- ============================================= -->
        <div id="procedureBrowseModal">
            <div class="modal-content">
                <div class="modal-header">
                    <h3 id="procedureBrowseModalTitle">👁️ Browse</h3>
                    <button class="close-btn" id="procedureBrowseModalClose"
                        style="background:none; border:none; font-size:28px; cursor:pointer; color:#94a3b8;">&times;</button>
                </div>
                <!-- Status filter + count badge (Phase 3 of 3, FINAL) — client-side only, filters
                     the already-loaded current page of #procedureBrowseTableContainer's rows by
                     the Status column Phase 1 added. No search box here (this modal has none of
                     its own; that's the separate tab-panel dropdown, Phase 2, untouched). -->
                <div class="procedure-browse-toolbar">
                    <label for="procedureBrowseStatusFilter">Status</label>
                    <select id="procedureBrowseStatusFilter" class="procedure-browse-status-select">
                        <option value="all" selected>All</option>
                        <option value="assigned">Assigned</option>
                        <option value="unassigned">Unassigned</option>
                    </select>
                    <span id="procedureBrowseStatusCount" class="procedure-browse-status-count"></span>
                </div>
                <div id="procedureBrowseTableContainer" class="procedure-browse-table-container"></div>
                <div class="procedure-browse-pagination">
                    <button type="button" id="procedureBrowsePrevBtn" class="btn-pagination" disabled>Previous</button>
                    <span id="procedureBrowsePageInfo" class="procedure-browse-page-info">Page 1 of 1</span>
                    <button type="button" id="procedureBrowseNextBtn" class="btn-pagination" disabled>Next</button>
                </div>
            </div>
        </div>
        <!-- Momentary success toast (bottom-right, auto-dismisses) - see showVerifyToast() in
             verification.js. Visibility is driven entirely by the .show class toggle, no inline
             display style. -->
        <div id="voucherVerifyToast" class="voucher-verify-toast"></div>
`;
document.body.insertAdjacentHTML('beforeend', PROCEDURE_MODAL_MARKUP);

// Transient toast (bottom-right overlay) for momentary positive feedback on an action itself -
// distinct from statusDiv above, which is the persistent page-level status bar used for
// everything else. See showVerifyToast() below.
const voucherVerifyToast = document.getElementById('voucherVerifyToast');
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
// Small auto-dismissing toast for momentary positive feedback on a single action (e.g. the
// plain "Verify" button) - unlike hostAdapter.showStatus() above, this doesn't write into the persistent
// page-level status bar, since the VERIFIED count and each line's Cleared/Query badge already
// cover the standing status. Kept on its own timer variable so it never clobbers/gets clobbered
// by hostAdapter.showStatus()'s window.statusTimeout.
let voucherVerifyToastTimeout = null;
function showVerifyToast(msg) {
    if (!voucherVerifyToast) return;
    voucherVerifyToast.textContent = msg;
    voucherVerifyToast.classList.add('show');
    clearTimeout(voucherVerifyToastTimeout);
    voucherVerifyToastTimeout = setTimeout(() => {
        voucherVerifyToast.classList.remove('show');
    }, 2000);
}
// Validation Rules Phase 5: synthetic id for the procedure modal's non-document "Voucher Line"
// tab (Prepared/Checked/Approved By) — never a real audit_rule_documents.id, so it can't collide.
const VOUCHER_LINE_TAB_ID = '__voucher_line__';

// Phase 1b: the host page's adapter object (see the top-of-file comment above and
// buildProcedureModalAdapter() in verification.js). Set only by openProcedureModal()'s own
// `if (adapter) hostAdapter = adapter;` guard — every other function in this file reads
// dependencies off this object instead of a bare global.
let hostAdapter = null;

// The voucher line uid currently shown in #procedureModal, or null when it's closed — lets
// closeProcedureModal() flush any unsaved observation text without needing a uid parameter, since
// its callers (the ✕ button and the outside-click listener) don't have one either. Moved here from
// verification.js (Phase 1b) — nothing outside this file ever read or wrote it.
let currentProcedureModalUid = null;

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

// Supplies the currently cached tick state to Verification's voucher-level atomic save.
// Modal observations and Physical Bills are deliberately excluded until 4B-2 serializes their
// pending editor state as part of the same transaction.
function getVoucherTickSaveState() {
    const procedureChecks = [];
    Object.entries(hostAdapter?.procedureModalState || {}).forEach(([uid, line]) => {
        Object.entries(line.tabs || {}).forEach(([docId, tab]) => {
            Object.entries(tab.checklist || {}).forEach(([roleName, check]) => {
                procedureChecks.push({ voucher_line_id: parseInt(uid), document_id: parseInt(docId),
                    role_name: roleName, is_checked: !!check.is_checked });
            });
        });
    });
    const transactionRef = hostAdapter?.currentVoucherId;
    const observations = [];
    const accountHeadNotes = [];
    const physicalBills = [];
    Object.entries(hostAdapter?.procedureModalState || {}).forEach(([uid, line]) => {
        Object.entries(line.tabs || {}).forEach(([docId, tab]) => {
            if (tab.observationDirty) observations.push({
                voucher_line_id: parseInt(uid), document_id: parseInt(docId),
                observation_text: tab.observation || ''
            });
            if (tab.physicalBillDirty) physicalBills.push({
                voucher_line_id: parseInt(uid), document_id: parseInt(docId),
                bills: (tab.physicalBills || []).map(bill => ({
                    bill_no: (bill.bill_no || '').trim(), bill_date: bill.bill_date || null,
                    vendor_name: (bill.vendor_name || '').trim(),
                    items: (bill.items || []).map(item => ({
                        item_name: (item.item_name || '').trim(),
                        quantity: item.quantity === '' ? null : item.quantity,
                        rate: item.rate === '' ? null : item.rate,
                        source_document_row_id: null, source_template: null
                    }))
                }))
            });
        });
        const accountHead = accountHeadCache[uid];
        if (accountHead && accountHead.mode === 'nomatch' && accountHead.noMatchDirty) {
            accountHeadNotes.push({ voucher_line_id: parseInt(uid), observation_text: accountHead.noMatchText || '' });
        }
    });
    // Keys here must match the snake_case Body(embed=True) param names on
    // save_and_verify_voucher_bundle exactly — FastAPI binds embedded Body params by exact JSON
    // key, so a camelCase key here silently falls through to that param's None default instead
    // of erroring, which is what caused the endpoint's unconditional 400 on verify (root-caused
    // 2026-08-28: voucher_checks always arrived empty, so every active check read as untouched).
    return { observations, account_head_notes: accountHeadNotes, physical_bills: physicalBills, procedure_checks: procedureChecks, voucher_checks: (voucherChecksCache[transactionRef] || []).map(check => ({
        check_id: check.id, state: check.state
    })) };
}
// Confirmed fix (2026-07-30): keyed by transaction_ref, holds the in-flight fetch Promise while
// one is outstanding for that voucher — see ensureVoucherChecksLoaded() below for why this exists.
let voucherChecksLoadingPromises = {};
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
// Trial Balance leaf accounts for the Account Head search-select — via GET /api/trial-balance/
// latest-accounts, same source Assign Categories already uses. That endpoint returns both parent
// and leaf accounts, so is_leaf filtering happens here, client-side; not pre-filtered server-side.
//
// Phase 3c: this is now warmed lazily (fire-and-forget from renderAccountHeadCard(), only when
// the "Voucher Line" tab actually renders in match mode) rather than on every modal open, and it
// is cached per client for the rest of the page session — the leaf-account list only changes on
// a Trial Balance re-upload, which can't happen mid-verification. latestTbAccountsCacheClientId
// guards the cache hit; latestTbAccountsInFlight dedups concurrent callers so a quick
// tab-switch-and-type can't fire the request twice.
let latestTbAccountsCache = null;
let latestTbAccountsCacheClientId = null;
let latestTbAccountsInFlight = null;
async function fetchLatestTbAccounts(clientId) {
    if (!clientId) {
        latestTbAccountsCache = [];
        latestTbAccountsCacheClientId = null;
        return latestTbAccountsCache;
    }
    if (latestTbAccountsCache !== null && latestTbAccountsCacheClientId === clientId) {
        return latestTbAccountsCache;
    }
    if (latestTbAccountsInFlight && latestTbAccountsInFlight.clientId === clientId) {
        return latestTbAccountsInFlight.promise;
    }
    const promise = (async () => {
        try {
            const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/trial-balance/latest-accounts?client_id=${encodeURIComponent(clientId)}`);
            if (!res.ok) {
                latestTbAccountsCache = [];
                latestTbAccountsCacheClientId = null;
                return latestTbAccountsCache;
            }
            const data = await res.json();
            latestTbAccountsCache = (data.accounts || []).filter(a => a.is_leaf);
            latestTbAccountsCacheClientId = clientId;
        } catch (e) {
            console.error('Failed to load Trial Balance leaf accounts:', e);
            latestTbAccountsCache = [];
            latestTbAccountsCacheClientId = null;
        }
        return latestTbAccountsCache;
    })();
    latestTbAccountsInFlight = { clientId, promise };
    try {
        return await promise;
    } finally {
        if (latestTbAccountsInFlight && latestTbAccountsInFlight.promise === promise) {
            latestTbAccountsInFlight = null;
        }
    }
}
// Confirmed fix (Linked Evidence table dynamic columns): each Document's evidence table needs
// its own template's actual configured columns (name + data_type, sort_order), not a one-size-
// fits-all set. Cached by template_id — several Documents can share the same template, and a
// Category's Documents don't change template mid-session, so no forceRefresh option is needed
// here (unlike auditGuidanceCache above). Fails soft to [] on error/404 so a lookup hiccup never
// blocks the modal from opening; buildSelectedLinesTable() falls back to deriving columns from
// the selections themselves when this returns empty.
let templateColumnsCache = {};
let templateMetadataRulebookVersion = null;

// GRN-to-Vendor-Bill copy: a sibling cache keyed the same way as templateColumnsCache, populated
// by the same fetchTemplateColumns() call (no extra network round trip) — lets
// buildSelectedLinesTable() tell a Store Receiving/GRN document's evidence table apart from any
// other template's without a new endpoint.
let templateNameCache = {};

async function fetchTemplateColumns(templateId) {
    const rulebook = hostAdapter && hostAdapter.sessionRulebook;
    const rulebookVersion = rulebook && rulebook.global_version != null
        ? String(rulebook.global_version)
        : null;
    if (rulebookVersion !== null && templateMetadataRulebookVersion !== rulebookVersion) {
        templateColumnsCache = {};
        templateNameCache = {};
        templateMetadataRulebookVersion = rulebookVersion;
    }
    if (Object.prototype.hasOwnProperty.call(templateColumnsCache, templateId)) {
        return templateColumnsCache[templateId];
    }
    const rulebookTemplate = rulebook && (rulebook.templates || []).find(template =>
        String(template.id) === String(templateId)
    );
    if (rulebookTemplate && Array.isArray(rulebookTemplate.columns)) {
        const columns = rulebookTemplate.columns.map(column => ({
            column_name: column.column_name,
            data_type: column.data_type
        }));
        templateColumnsCache[templateId] = columns;
        templateNameCache[templateId] = rulebookTemplate.name || '';
        return columns;
    }
    try {
        const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/templates/${templateId}`);
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
// "Show N/A (n)" toggle link. Reuses hostAdapter.renderGateRowHtml() — same row markup as the popover.
function buildValidatePanelBody(data) {
    if (data.reason) {
        return `<p class="lsr-reason">${hostAdapter.escapeHtml(data.reason)}</p>`;
    }
    const gates = data.gates || [];
    if (!gates.length) {
        return '<p class="lsr-reason">No gate breakdown available for this line.</p>';
    }
    if (data.working_paper_gates?.length && hostAdapter.renderWorkingPaperGatesHtml) {
        return hostAdapter.renderWorkingPaperGatesHtml(data, true);
    }
    const visible = gates.filter(g => g.status !== 'not_applicable');
    const naGates = gates.filter(g => g.status === 'not_applicable');

    let html = visible.length
        ? visible.map(hostAdapter.renderGateRowHtml).join('')
        : '<p class="lsr-reason">No applicable checks for this line.</p>';

    if (naGates.length) {
        if (validatePanelShowNA) {
            html += naGates.map(hostAdapter.renderGateRowHtml).join('');
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
            <span class="procedure-validate-status-pill ${pillClass}">${hostAdapter.escapeHtml(status)}</span>
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

    if (window.LoadingUI) {
        window.LoadingUI.showSpinner(panel, 'Loading');
    } else {
        panel.innerHTML = '<p class="lsr-reason">Loading…</p>';
    }
    panel.style.display = 'block';
    validatePanelLoadingUid = uid;

    try {
        const response = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/line_status_reason/${uid}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        // Discard a stale response if the panel was closed or the modal moved to a different
        // line while this fetch was in flight. Do NOT hideSpinner here - a newer request for a
        // different line may already have its own spinner active on this same singleton panel.
        if (validatePanelLoadingUid !== uid) return;
        if (window.LoadingUI) window.LoadingUI.hideSpinner(panel);
        validatePanelData = data;
        validatePanelShowNA = false;
        renderValidatePanel();
    } catch (e) {
        if (validatePanelLoadingUid !== uid) return;
        console.warn('Unable to load line status reason:', e);
        if (window.LoadingUI) window.LoadingUI.hideSpinner(panel);
        panel.innerHTML = '<p class="lsr-reason">Unable to load the status breakdown.</p>';
    }
}
// ==============================================
// SUPPORTING DOCUMENTS TABBED MODAL
// Voucher Supporting-Document Linking, Phase 3 of 6 (shell): one tab per document belonging to
// the voucher line's Category, generated dynamically from that Category's documents[] list (the
// same list already fetched for the Guide popover/Supporting cell via hostAdapter.fetchAuditGuidanceData()).
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
async function fetchProcedureRows(templateId, documentId, search, page, limit) {
    const clientId = hostAdapter.getClientId();
    if (!clientId) throw new Error('No client selected.');
    const params = new URLSearchParams({
        client_id: clientId,
        template_id: templateId,
        date_start: PROCEDURE_SEARCH_DATE_START,
        date_end: getProcedureSearchDateEnd(),
        page: page,
        limit: limit,
        document_id: documentId
    });
    if (search) params.set('search', search);
    const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/data/preview?${params}`);
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
// "uid" via hostAdapter.currentVoucherLines (already loaded for the current voucher), against the new
// assigned_to_line_client_data_id field routers/data.py's attach_assignment_flags() now returns.
function isSameUnderlyingLine(uid, row) {
    if (!row.is_assigned) return false;
    const currentLine = hostAdapter.currentVoucherLines.find(l => String(l.detail_id) === String(uid));
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
        lockNote = `<span class="procedure-result-locked">Linked to this voucher under "${hostAdapter.escapeHtml(row.assigned_to_document_name || 'another document')}"</span>`;
    } else if (isLocked) {
        lockNote = `<span class="procedure-result-locked">Assigned to voucher ${hostAdapter.escapeHtml(formatAssignedVoucherLabel(row.assigned_to_voucher_id, row.assigned_to_line_srno))}</span>`;
    }
    return `
        <div class="procedure-result-item${isLocked ? ' locked' : ''}" data-id="${row.id}"${isLocked ? '' : ' role="button" tabindex="0"'}>
            <div class="procedure-result-main">
                <span class="procedure-result-label">${hostAdapter.escapeHtml(String(label))}</span>
                <span class="procedure-result-meta">${hostAdapter.escapeHtml(date)} · ${hostAdapter.escapeHtml(String(amount))}</span>
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
    if (!hostAdapter.procedureModalState[uid]) hostAdapter.procedureModalState[uid] = { activeDocId: null, documents: [], assignedDocIds: new Set(), voucherLineAmount: 0, crossCheckResults: [], tabs: {} };
    if (!hostAdapter.procedureModalState[uid].tabs[docId]) {
        hostAdapter.procedureModalState[uid].tabs[docId] = { loaded: false, searchTerm: '', searchResults: [], docKeyColumn: null, selections: [], observation: '', observationDirty: false, suggestionApplied: false, lastSyncedText: null, checklist: {}, physicalBills: [], physicalBillSaved: false, physicalBillDirty: false };
    }
    return hostAdapter.procedureModalState[uid].tabs[docId];
}

// Opens the modal for one voucher line, showing a tab per document in that line's currently
// assigned Category. initialDocId (optional) pre-selects that document's tab; otherwise the
// first document's tab opens. No-ops if the line has no Category assigned yet (nothing to build
// tabs from) or its Category has no documents.
const procedureModalDataCache = new Map();
const procedureModalDataLoading = new Map();

function invalidateProcedureModalData(uid) {
    const key = String(uid);
    procedureModalDataCache.delete(key);
    console.debug('[procedure-cache] invalidated', { uid: key, reason: 'line-data-changed' });
}

// Drop the cached procedure-modal payload for EVERY line in the voucher the host currently has
// loaded. save_and_verify_voucher_bundle persists all of currentVoucherLines in one request, so
// reopening any of them must re-fetch rather than serve the pre-save cache (the "ticks show
// unticked on reopen" bug — openProcedureModal() rebuilds tabState.checklist from this payload
// verbatim). No-ops safely on an empty/absent line list.
function invalidateProcedureModalDataForVoucher() {
    ((hostAdapter && hostAdapter.currentVoucherLines) || []).forEach(line => {
        invalidateProcedureModalData(line.detail_id);
    });
}

function loadProcedureModalData(uid, perfSuffix) {
    const key = String(uid);
    if (procedureModalDataCache.has(key)) {
        console.debug('[procedure-cache] hit', { uid: key });
        return Promise.resolve(procedureModalDataCache.get(key));
    }
    if (procedureModalDataLoading.has(key)) {
        console.debug('[procedure-cache] in-flight reuse', { uid: key });
        return procedureModalDataLoading.get(key);
    }
    console.debug('[procedure-cache] miss/request', { uid: key });
    const promise = window.ApiUtils.apiFetch(`${API_BASE}/api/verification/procedure_modal_data/${uid}${perfSuffix}`)
        .then(async res => {
            if (!res.ok) return null;
            const body = await res.json();
            procedureModalDataCache.set(key, body);
            return body;
        }).catch(() => null).finally(() => procedureModalDataLoading.delete(key));
    procedureModalDataLoading.set(key, promise);
    return promise;
}

async function openProcedureModal(uid, initialDocId = null, adapter = null, triggerEl = null) {
    const perfStarted = performance.now();
    const perfComponents = {};
    if (adapter) hostAdapter = adapter;
    // Dirty tracking is a Verification-host optimization. Audit Workbook's lean adapter does not
    // own voucher-navigation saves, so these remain intentional no-ops there.
    if (typeof hostAdapter.markLineDirty !== 'function') hostAdapter.markLineDirty = () => {};
    if (typeof hostAdapter.markAllLinesDirty !== 'function') hostAdapter.markAllLinesDirty = () => {};
    if (!procedureModal) return;
    const lineState = hostAdapter.voucherLineState[uid];
    const type = lineState && lineState.transactionType;
    if (!type) return;

    // Spinner target: the clicked badge/pill on a fresh open (triggerEl, passed by the caller),
    // or the panel container itself on an internal re-navigation between already-open lines
    // (navigateProcedureModalLine/verifyAndAdvanceVoucher call this with no triggerEl, since
    // there's no click event to attach to and the modal is already visible).
    const spinnerTarget = triggerEl || document.getElementById('procedureModalPanels');
    // A triggerEl-driven open means the clicked element is a narrow Procedures/Observations table
    // cell badge/flag — LoadingUI's default 3s escalation to "— still working…" overflowed past
    // the column there (confirmed bug), so escalation is disabled for that case specifically. The
    // no-triggerEl re-navigation case targets the wide panels container, which has room, so it
    // keeps the default escalating message.
    if (window.LoadingUI) window.LoadingUI.showSpinner(spinnerTarget, 'Loading', { escalate: !triggerEl });

    try {
        // Session-level ref-data caching (Phase 2 of 4): forceRefresh=false — reuses
        // auditGuidanceCache[type] when already warm instead of re-fetching on every open.
        // In practice this Category's guidance is usually already cached by the time a modal
        // opens, since verification.js's Supporting-column badge/Guide popover rendering already
        // call fetchAuditGuidanceData(type) with no forceRefresh as each voucher's lines render.
        // Guidance now carries matrix_version (audit_rule_categories.matrix_version) alongside
        // the rest of the payload, so a cached entry's version is available if a later phase adds
        // a live staleness check - none exists yet, so this phase is a plain session-lifetime
        // cache, not a version-triggered one.
        // Phase 3c: kick off the batch modal-data fetch immediately. It needs only `uid`, so it
        // can run in parallel with the (usually cached) guidance fetch above and the
        // template-column warming below, instead of waiting for both. Its own try/catch +
        // fail-soft-to-null is kept here so a modal_data failure still degrades gracefully and
        // never surfaces as an unhandled rejection when an early `return` below abandons it.
        // modal_data_ms is timed inside the IIFE so it stays truthful even under overlap.
        const modalDataPromise = (async () => {
            const modalDataStarted = performance.now();
            try {
                const perfSuffix = window.VerificationPerf && window.VerificationPerf.enabled ? '?perf=true' : '';
                const body = await loadProcedureModalData(uid, perfSuffix);
                if (body) {
                    perfComponents.modal_data_ms = Math.round((performance.now() - modalDataStarted) * 10) / 10;
                    return body;
                }
            } catch (e) {
                console.error('Failed to load Supporting Documents modal data:', e);
            }
            perfComponents.modal_data_ms = Math.round((performance.now() - modalDataStarted) * 10) / 10;
            return null;
        })();

        let guidance = null;
        try {
            const guidanceStarted = performance.now();
            guidance = await hostAdapter.fetchAuditGuidanceData(type, false);
            perfComponents.guidance_ms = Math.round((performance.now() - guidanceStarted) * 10) / 10;
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
    // would mutate guidance.documents in place — that's the same object hostAdapter.fetchAuditGuidanceData()
    // just wrote into auditGuidanceCache, so mutating it would permanently pollute the shared
    // cache other callers (Supporting column, Guide popover) read from).
    const documents = [{ id: VOUCHER_LINE_TAB_ID, name: 'Voucher Line', __voucherTab: true }, ...guidanceDocuments];

    // Confirmed fix: warm templateColumnsCache for every template_id among this Category's
    // Documents before the tabs render, so buildSelectedLinesTable() can look up each template's
    // real column list synchronously — mirrors the assignedRecords/procedureChecks/
    // observationEntries pattern below (all awaited before rendering).
    const distinctTemplateIds = [...new Set(documents.filter(d => d.template_id).map(d => d.template_id))];

    // Batch modal-open endpoint (Fast Modal-Open Data, Phase 1 of 4): one call replaces what
    // used to be 6 separate round trips here (voucher_line_documents/procedure_checks/
    // line_observations/physical_bills/cross_check_results/voucher_checks) - each paid the same
    // Render<->Supabase cross-region latency floor separately. audit_guidance stays a separate
    // call above (it primes auditGuidanceCache, a cache other UI surfaces outside this modal
    // read from).
    //
    // Phase 3c: the template-column warming and the batch modal-data fetch (started before the
    // guidance await above) are independent, so they run together here instead of one after the
    // other. template_columns_ms therefore covers whichever of the two finishes last; the true
    // modal-data duration is captured separately as modal_data_ms inside its own IIFE.
    const templateColumnsStarted = performance.now();
    const [, modalDataResolved] = await Promise.all([
        Promise.all(distinctTemplateIds.map(fetchTemplateColumns)),
        modalDataPromise
    ]);
    perfComponents.template_columns_ms = Math.round((performance.now() - templateColumnsStarted) * 10) / 10;

    // Fail-soft to {} on the whole call, same convention as the per-call fail-soft this replaced.
    let modalData = modalDataResolved || {};

    // Full assignment records (not just IDs) for this line. Drives the tab completion dots AND
    // seeds each tab's selected-lines table, grouped by document_id. Re-fetched fresh on every
    // open (via the composite call above) so both can never drift from true server state.
    const assignedRecords = (modalData.linked_documents && modalData.linked_documents.documents) || [];
    const assignedDocIds = new Set(assignedRecords.map(d => d.document_id));

    // Every ticked/unticked audit-procedure role for this line, across all documents — re-fetched
    // fresh on every open (via the composite call above).
    const procedureChecks = (modalData.procedure_checks && modalData.procedure_checks.checks) || [];

    // Every saved observation entry for this line, across all documents — re-fetched fresh on
    // every open (via the composite call above) so a saved note is never silently stale.
    const observationEntries = (modalData.observations && modalData.observations.entries) || [];

    // Confirmed fix: tabs for Documents not applicable to this line's Category are fully hidden,
    // not rendered greyed-out — `documents` (built from guidanceDocuments above) already IS that
    // per-Category applicable list (audit_rule_category_documents, is_active=1-filtered server-
    // side), so no further filtering is needed here. This supersedes the earlier "synthesize a
    // locked tab for a Document deactivated after evidence was already recorded against it"
    // design — that tab is now simply omitted; the underlying voucher_line_documents/
    // voucher_line_procedure_checks/voucher_line_observations rows are left untouched (data is
    // not deleted, just no longer reachable from this rail).

    // Every saved Physical Bill entry for this line, across all documents - re-fetched fresh on
    // every open (via the composite procedure_modal_data call above). The out-of-modal
    // Procedures-badge path no longer needs its own physical-bill fetch either - it reads
    // hostAdapter.loadVoucherSupportData() instead.
    const physicalBillEntries = (modalData.physical_bills && modalData.physical_bills.bills) || [];

    // This line's Cross-check evaluation results (already-computed structure, zero new
    // evaluation logic), for the Data Table segment's collapsible pass/fail rollup - re-fetched
    // fresh on every open (via the composite call above).
    const crossCheckResults = (modalData.cross_check_results && modalData.cross_check_results.results) || [];

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

    // Phase 3c: the Trial Balance leaf-account list is no longer warmed here. It is used only by
    // the "Voucher Line" tab's Account Head search-select, which is usually not the tab that
    // opens, so paying its round trip on every modal open was pure waste. renderAccountHeadCard()
    // now warms it lazily (fire-and-forget) the first time that tab renders in match mode, and
    // fetchLatestTbAccounts() caches per client + dedups in-flight calls — see both below.

    // The persistent voucher-level checks bar's data — now included in the composite call above,
    // written straight into the same voucherChecksCache ensureVoucherChecksLoaded() and
    // hostAdapter.updateProceduresCellForLine() both read from, instead of a separate fetch.
    // ensureVoucherChecksLoaded() itself is left in place, still used by that other call site.
    if (hostAdapter.currentVoucherId) {
        voucherChecksCache[hostAdapter.currentVoucherId] = (modalData.voucher_checks || []).map(c => ({
            id: c.id,
            name: c.name,
            sort_order: c.sort_order,
            state: c.state || 'untouched'
        }));
    }

    // The voucher line's own amount (for the mismatch badge) — looked up from the already-loaded
    // voucher, no extra fetch needed. String()-normalized: uid arrives as a string whenever this
    // is called from a table click (dataset.uid is always a string), but hostAdapter.currentVoucherLines[].
    // detail_id is a number straight from the JSON response — a bare === here always missed on
    // that path (confirmed bug: caused the header below to show "Line 0 of 6 — —" and this amount
    // to silently default to 0). Object-key lookups elsewhere (hostAdapter.voucherLineState[uid]) don't have
    // this problem since plain-object keys auto-coerce to strings; only Array.find/findIndex do not.
    const lineIdx = hostAdapter.currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    const voucherLine = lineIdx !== -1 ? hostAdapter.currentVoucherLines[lineIdx] : null;
    const voucherLineAmount = voucherLine ? hostAdapter.parseVoucherAmount(hostAdapter.getVoucherLineAmount(voucherLine.row_data || {})) : 0;

    if (!hostAdapter.procedureModalState[uid]) hostAdapter.procedureModalState[uid] = { activeDocId: null, documents: [], assignedDocIds: new Set(), voucherLineAmount: 0, crossCheckResults: [], tabs: {} };
    hostAdapter.procedureModalState[uid].documents = documents;
    hostAdapter.procedureModalState[uid].assignedDocIds = assignedDocIds;
    hostAdapter.procedureModalState[uid].voucherLineAmount = voucherLineAmount;
    hostAdapter.procedureModalState[uid].crossCheckResults = crossCheckResults;
    hostAdapter.procedureModalState[uid].standaloneData = {
        assignedRecords,
        procedureChecks,
        physicalBillEntries,
        guidance,
        // Stable category profile from the shared Rulebook-compatible guidance cache. Fresh
        // voucher data above remains separate and is still reloaded on every modal open.
        rulebookProfile: guidance
    };

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

        // Multi-Bill/Multi-Line Physical Bill, Phase 3 of 6: server truth always wins on reopen,
        // same convention as observation/lastSyncedText above — never trust a stale local draft.
        // A document can now have several bills, each with several items (Phase 2's nested shape);
        // ids/timestamps/bill_total are display-only server fields, not needed client-side since
        // save is always full-replace, so only the editable fields are carried into tabState.
        if (doc.document_type === 'physical_bill') {
            const savedBills = physicalBillEntries.filter(b => b.document_id === doc.id);
            tabState.physicalBills = savedBills.map(b => ({
                bill_no: b.bill_no || '',
                bill_date: b.bill_date || '',
                vendor_name: b.vendor_name || '',
                items: (b.items || []).map(item => ({
                    item_name: item.item_name || '',
                    quantity: item.quantity != null ? String(item.quantity) : '',
                    rate: item.rate != null ? String(item.rate) : '',
                    amount: item.quantity != null && item.rate != null ? item.quantity * item.rate : null
                }))
            }));
            tabState.physicalBillSaved = savedBills.length > 0;
            tabState.physicalBillDirty = false;
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
    hostAdapter.procedureModalState[uid].activeDocId = activeDocId;

    currentProcedureModalUid = uid;

    // "Why is this line Cleared/Query?" panel always resets to collapsed on open (new line or
    // reopen) — stale gate data for a different line must never carry over.
    hideValidatePanel();

    // Header: line position/account name, type, amount, risk (Supporting Documents modal
    // redesign follow-up) — srNo/totalLines mirror the same order renderVoucherLines() numbers
    // the table with; risk reuses hostAdapter.computeLineRiskData() against the guidance already fetched
    // above, no extra request. lineIdx/voucherLine were already resolved above (String()-normalized).
    const srNo = lineIdx + 1;
    const totalLines = hostAdapter.currentVoucherLines.length;
    const accountName = voucherLine ? hostAdapter.getVoucherLineAlias(voucherLine.row_data || {}, hostAdapter.VOUCHER_LINE_ACCOUNT_NAME_ALIASES) : '—';
    const amountDisplay = voucherLine ? hostAdapter.getVoucherLineAmount(voucherLine.row_data || {}) : '';
    const riskData = hostAdapter.computeLineRiskData(guidance);
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
    } finally {
        if (window.VerificationPerf) {
            const lineState = hostAdapter && hostAdapter.procedureModalState[uid];
            window.VerificationPerf.log('procedure_modal_open', perfStarted, {
                detail_id: String(uid),
                document_count: lineState ? lineState.documents.filter(d => !d.__voucherTab).length : 0,
                components: perfComponents
            });
        }
        if (window.LoadingUI) window.LoadingUI.hideSpinner(spinnerTarget);
    }
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
    const lineState = hostAdapter.procedureModalState[uid];
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
    const idx = hostAdapter.currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    if (idx === -1) return;
    const newIdx = idx + delta;
    if (newIdx < 0 || newIdx >= hostAdapter.currentVoucherLines.length) return;
    const newUid = hostAdapter.currentVoucherLines[newIdx].detail_id;
    const newLineState = hostAdapter.voucherLineState[newUid];
    if (!newLineState || !newLineState.transactionType) {
        hostAdapter.showStatus('This line has no assigned category — supporting documents unavailable.', 'error');
        return;
    }
    // Validation Rules Phase 5: hostAdapter.saveLineItems() (not just flushProcedureObservations()) so
    // cleared_status recomputes and the badge refreshes every time the modal moves between
    // lines — the badge lives in the main table, hidden behind the modal while it's open, so
    // this is the natural point for it to catch up.
    // Navigation is independent from persistence: begin saving the line being left, then open
    // the next line immediately. The host's in-flight save guard keeps this background save safe;
    // dirty state is retained automatically if the request fails.
    const savePromise = (async () => {
        const physicalBillsSaved = await flushPhysicalBillEntries(uid);
        const result = await hostAdapter.saveLineItems(uid);
        if (!physicalBillsSaved) hostAdapter.markLineDirty(uid, 'physical bill save retry required');
        if (result && result.ok) {
            hostAdapter.showStatus('✅ Audit procedures saved.', 'success');
        } else {
            hostAdapter.showStatus('⚠️ Audit procedures could not be saved. Please retry.', 'error');
        }
    })().catch(() => {
        hostAdapter.showStatus('⚠️ Audit procedures could not be saved. Please retry.', 'error');
    });
    await openProcedureModal(newUid);
    await savePromise;
}

// Finds the next *unverified* line of the currently loaded voucher, searching forward only from
// fromIdx + 1. "Unverified" is cleared_status !== 'Cleared' — the Validation Rules verdict already
// rendered as the Cleared/Query badge on each table row, recomputed server-side on every
// hostAdapter.saveLineItems() call and mirrored straight back into hostAdapter.currentVoucherLines, so the value tested
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
    for (let i = fromIdx + 1; i < hostAdapter.currentVoucherLines.length; i++) {
        const line = hostAdapter.currentVoucherLines[i];
        if (line.cleared_status === 'Cleared') continue;
        const lineState = hostAdapter.voucherLineState[line.detail_id];
        if (!lineState || !lineState.transactionType) continue;
        return i;
    }
    return -1;
}

// Pure teardown — hides the modal(s) and clears the "currently open" marker, with no save of any
// kind. Factored out of closeProcedureModal() so the Verify & Next/Close button (which already
// explicitly saves via hostAdapter.saveLineItems() before calling verify_voucher_checks) can tear down the
// modal without triggering a redundant second save. hostAdapter.procedureModalState[uid] is deliberately left
// in place (not cleared here) — reopening the same line's modal within the same voucher should
// show tab state exactly as it was left; it's only reset on voucher navigation, in
// seedVoucherLineState().
//
// Confirmed fix: the main table's Procedures (and Observations) badges were never wired to
// refresh after anything done inside this modal (ticking a checklist role, linking/unlinking
// evidence, saving a Physical Bill, or pressing Verify) — they're computed once when the line's
// Category first resolves and otherwise only ever recomputed by a full page reload. Since every
// close/verify path (✕, outside-click, Verify & Next/Close, plain Verify) already funnels through
// this one shared teardown function, refreshing both cells here — for the line the modal is
// actually leaving, captured before currentProcedureModalUid is cleared below — covers all of
// them with one change. Fire-and-forget, matching the existing convention at this function's own
// two other call sites (applyCategoryToTypeCell/toggleColumnsExpanded never await it either).
function hideProcedureModalOnly(refreshBadges = true) {
    if (procedureModal) procedureModal.style.display = 'none';
    // Defensive — the browse modal is nested on top of this one, so it should never remain open
    // once the parent closes.
    closeProcedureBrowseModal();
    const uid = currentProcedureModalUid;
    currentProcedureModalUid = null;
    const type = uid && hostAdapter.voucherLineState[uid] && hostAdapter.voucherLineState[uid].transactionType;
    if (type && refreshBadges) {
        hostAdapter.updateProceduresCellForLine(uid, type);
        hostAdapter.updateObservationsCellForLine(uid, type);
    }
}

async function closeProcedureModal() {
    // Flush any unsaved observation text/voucher-approval edits before hiding — closing via ✕ or
    // outside-click persists the same way Prev/Next/Complete/Exit does, per the confirmed
    // requirement (there is no longer a Save button to rely on instead). hostAdapter.saveLineItems() also
    // recomputes cleared_status (Validation Rules Phase 5) so the main table's badge is caught up
    // by the time the modal is gone.
    if (currentProcedureModalUid) {
        if (window.LoadingUI) window.LoadingUI.showProgressBar(procedureModalClose, { mode: 'indeterminate', message: 'Saving…' });
        try {
            const physicalBillsSaved = await flushPhysicalBillEntries(currentProcedureModalUid);
            // Verification supplies the grouped save path. Audit Workbook deliberately has a
            // lean adapter, so close must use its ordinary line save instead of calling a method
            // that host does not provide. Both paths return the same {ok, data/error} contract.
            const result = typeof hostAdapter.saveAndVerifyVoucher === 'function'
                ? await hostAdapter.saveAndVerifyVoucher(currentProcedureModalUid, false)
                : await hostAdapter.saveLineItems(currentProcedureModalUid, { force: true });
            if (!result || !result.ok) {
                throw new Error(result?.error?.detail || result?.error?.message || 'Save failed.');
            }
            // The bundle just persisted every line in this voucher — drop each line's cached
            // modal payload so a later reopen re-fetches the saved ticks, not the pre-save copy.
            invalidateProcedureModalDataForVoucher();
            // The Procedures cells are rendered asynchronously and are not rebuilt when the
            // modal closes. Refresh every line because the grouped save includes voucher-wide
            // procedure state; this makes the count/ring/color reflect the just-saved checks.
            const refreshedLines = (hostAdapter.currentVoucherLines || []).map(line => {
                const lineState = hostAdapter.voucherLineState[line.detail_id];
                const type = lineState && lineState.transactionType;
                return type ? hostAdapter.updateProceduresCellForLine(line.detail_id, type) : null;
            }).filter(Boolean);
            await Promise.all(refreshedLines);
            if (!physicalBillsSaved) hostAdapter.markLineDirty(currentProcedureModalUid, 'physical bill save retry required');
        } finally {
            if (window.LoadingUI) window.LoadingUI.hideProgressBar(procedureModalClose);
        }
    }
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
    const lineIdx = hostAdapter.currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    const hasNextLine = lineIdx !== -1 && findNextUnverifiedLineIndex(lineIdx) !== -1;
    const pos = hostAdapter.getVoucherPosition();
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
    const checks = voucherChecksCache[hostAdapter.currentVoucherId] || [];
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
const inFlightVoucherVerifies = new Map();

async function performVerifyVoucherChecks(uid, triggerBtn) {
    const key = String(hostAdapter.currentVoucherId || uid);
    if (inFlightVoucherVerifies.has(key)) return inFlightVoucherVerifies.get(key);
    const sequence = performVerifyVoucherChecksImpl(uid, triggerBtn);
    inFlightVoucherVerifies.set(key, sequence);
    try {
        return await sequence;
    } finally {
        inFlightVoucherVerifies.delete(key);
    }
}

async function performVerifyVoucherChecksImpl(uid, triggerBtn) {
    const perfStarted = performance.now();
    const btn = document.getElementById('procedureModalVerifyBtn');
    const verifyOnlyBtn = document.getElementById('procedureModalVerifyOnlyBtn');
    if (btn) btn.disabled = true;
    if (verifyOnlyBtn) verifyOnlyBtn.disabled = true;
    const progressTarget = triggerBtn || btn;
    const previousBodyCursor = document.body ? document.body.style.cursor : '';
    const previousModalBusy = procedureModal ? procedureModal.getAttribute('aria-busy') : null;
    if (document.body) document.body.style.cursor = 'wait';
    if (procedureModal) procedureModal.setAttribute('aria-busy', 'true');
    if (window.LoadingUI) window.LoadingUI.showProgressBar(progressTarget, { mode: 'indeterminate', message: 'Saving…' });
    try {
        // Defensive re-check (fixes verify_voucher_checks 400s reaching the server for a
        // voucher the UI itself doesn't believe is ready): voucherChecksCache is fetched once
        // by ensureVoucherChecksLoaded() and cached forever per transaction_ref for the rest of
        // the page session, and both buttons' disabled state defaults to enabled until
        // updateVerifyFooterButton() has actually run for the voucher currently in view - a gap
        // proven to matter once already (a check added mid-session, e.g. via Audit Rules, would
        // go undetected by an already-warmed cache). Reuse the modal's fresh cache when present;
        // ensureVoucherChecksLoaded() still fetches when no cache exists.
        const freshChecks = await ensureVoucherChecksLoaded(hostAdapter.getClientId(), hostAdapter.currentVoucherId);
        if (!isVoucherChecksAllTouched(freshChecks || [])) {
            hostAdapter.showStatus('Mark every voucher-level check before verifying.', 'error');
            return null;
        }
        let combinedResult;
        if (typeof hostAdapter.saveAndVerifyVoucher === 'function') {
            combinedResult = await hostAdapter.saveAndVerifyVoucher(uid);
        } else {
            const saved = await hostAdapter.saveLineItems(uid, { force: true });
            if (!saved || !saved.ok) {
                combinedResult = { ok: false, error: {} };
            } else {
                const suffix = window.VerificationPerf && window.VerificationPerf.enabled ? '?perf=true' : '';
                const response = await window.ApiUtils.apiFetchMutate(`${API_BASE}/api/verification/verify_voucher_checks${suffix}`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ session_id: hostAdapter.currentSessionId, current_detail_id: uid })
                });
                combinedResult = response.ok ? { ok: true, data: await response.json() } : { ok: false, error: {} };
            }
        }
        if (!combinedResult.ok) throw new Error(combinedResult.error?.detail || 'Save and verify failed.');
        // Both branches above persist this voucher's lines (the bundle saves all of
        // currentVoucherLines; the fallback saves the current line) — invalidate every line's
        // cached modal payload so reopening any of them shows the saved ticks, not stale cache.
        invalidateProcedureModalDataForVoucher();
        const data = combinedResult.data;
        if (!data) return null;
        if (window.VerificationPerf) {
            window.VerificationPerf.log('verify_voucher_checks_sequence', perfStarted, {
                detail_id: String(uid),
                includes: ['fresh voucher-check read', 'atomic voucher-line/modal save', 'voucher verify'],
                backend: data && data._perf ? data._perf : null
            });
        }
        return data;
    } catch (e) {
        console.error('Error verifying voucher checks:', e);
        hostAdapter.showStatus(e && e.message ? e.message : 'Could not verify voucher-level checks.', 'error');
        return null;
    } finally {
        if (window.LoadingUI) window.LoadingUI.hideProgressBar(progressTarget);
        // Both footer actions are disabled together, so clear either button's visual progress
        // state even when the other button was the one clicked.
        if (window.LoadingUI) {
            window.LoadingUI.hideProgressBar(btn);
            window.LoadingUI.hideProgressBar(verifyOnlyBtn);
        }
        if (document.body) document.body.style.cursor = previousBodyCursor;
        if (procedureModal) {
            if (previousModalBusy === null) procedureModal.removeAttribute('aria-busy');
            else procedureModal.setAttribute('aria-busy', previousModalBusy);
        }
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
    const data = await performVerifyVoucherChecks(uid, btn);
    if (!data) return;

    // Line-first stepping: hostAdapter.saveLineItems() inside the helper above already refreshed this line's
    // cleared_status in hostAdapter.currentVoucherLines, so the search below sees this press's own result, not
    // a stale one. String()-normalized lookup for the same reason openProcedureModal() does it —
    // uid can be a string from a table click while detail_id is a number from JSON.
    const lineIdx = hostAdapter.currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    const nextLineIdx = lineIdx === -1 ? -1 : findNextUnverifiedLineIndex(lineIdx);
    if (nextLineIdx !== -1) {
        // Stay in the voucher: keep the modal open and re-run the full fetch/render pipeline
        // for the target line, same as navigateProcedureModalLine() does (which re-saves; we
        // already saved above, so openProcedureModal() is called directly instead).
        await openProcedureModal(hostAdapter.currentVoucherLines[nextLineIdx].detail_id);
        await hostAdapter.updateProgressDisplay();
        return;
    }

    // No unverified line left below this one — leave the voucher, unchanged behaviour.
    // Already saved above — hide only, don't re-save via closeProcedureModal().
    hideProcedureModalOnly(false);
    if (data.boundary) {
        hostAdapter.showStatus('Voucher verified. No more vouchers in this session.', 'success');
    } else {
        hostAdapter.applyVoucherNavRenderData(data);
    }
    await hostAdapter.updateProgressDisplay();
}

// Click handler for the plain footer "Verify" button (confirmed requirement): performs the exact
// same save+verify action as Verify & Next above, via the same shared helper, but never advances —
// it always just closes the modal back onto the grid at the current voucher, regardless of how
// many lines the voucher has or whether more remain unverified. The current line's Cleared/Query
// badge is already live in the grid by the time this returns (hostAdapter.saveLineItems(), inside the helper,
// already refreshes it), so there is nothing further to re-render here.
async function verifyAndStay() {
    const uid = currentProcedureModalUid;
    if (!uid) return;
    const verifyOnlyBtn = document.getElementById('procedureModalVerifyOnlyBtn');
    if (verifyOnlyBtn && verifyOnlyBtn.disabled) return;
    const data = await performVerifyVoucherChecks(uid, verifyOnlyBtn);
    if (!data) return;
    // Already saved above — hide only, don't re-save via closeProcedureModal().
    hideProcedureModalOnly(false);
    showVerifyToast('Voucher checks verified.');
    await hostAdapter.updateProgressDisplay();
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
// hostAdapter.updateProceduresCellForLine()'s standalone badge computation (below) can also warm the same
// cache for a transaction_level_approval-flagged Document, without duplicating this fetch.
async function ensureVoucherChecksLoaded(clientId, transactionRef) {
    if (!transactionRef) return null;
    if (voucherChecksCache[transactionRef]) {
        return voucherChecksCache[transactionRef];
    }
    // Confirmed fix (2026-07-30): this function is called from two independent sites for the
    // same voucher — openProcedureModal() and hostAdapter.updateProceduresCellForLine() (the latter fires
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
            const res = await window.ApiUtils.apiFetch(`${API_BASE}/api/verification/voucher_checks?${params}`);
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
    const transactionRef = hostAdapter.currentVoucherId;
    const checks = voucherChecksCache[transactionRef] || [];

    if (!checks.length) {
        container.innerHTML = '<p class="procedure-voucher-checks-empty">No voucher-level checks configured.</p>';
        updateVerifyFooterButton(uid);
        return;
    }

    const notDone = checks.filter(c => c.state === 'not_done');
    const warningHtml = notDone.length
        ? `<div class="procedure-voucher-checks-warning">⚠️ ${notDone.map(c => `<strong>${hostAdapter.escapeHtml(c.name)}</strong>`).join(', ')} marked not done — observation will auto-note on close.</div>`
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
            <span class="procedure-voucher-check-label">${hostAdapter.escapeHtml(check.name)}</span>
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
            hostAdapter.markAllLinesDirty('voucher-level check changed');
            renderVoucherChecksBar(uid);
            renderProcedureModalTabs(uid);
            // Verification keeps this change local until grouped Save -> Verify. Audit Workbook
            // supplies this method because it has no grouped voucher save path.
            if (typeof hostAdapter.saveVoucherCheck !== 'function') {
                invalidateProcedureModalData(uid);
                return;
            }
            try {
                const result = await hostAdapter.saveVoucherCheck({
                    transactionRef, checkId, state: newState, target: this
                });
                if (!result || !result.ok) {
                    throw new Error(result?.error?.detail || 'Failed to save voucher check.');
                }
                // Completion-dot/gate logic depends on check state, so a successful toggle must
                // refresh the rail's dots, the main table's Procedures badges, and the footer's
                // Verify button — mirrors toggleProcedureCheck()'s own renderProcedureModalTabs()
                // refresh, extended to this bar's own dependents.
                renderVoucherChecksBar(uid);
                renderProcedureModalTabs(uid);
                invalidateProcedureModalData(uid);
                hostAdapter.markAllLinesDirty('voucher-level check changed');
            } catch (e) {
                console.error('Failed to save voucher check:', e);
                check.state = previousState;
                hostAdapter.showStatus('Failed to save voucher-level check — please try again.', 'error');
                renderVoucherChecksBar(uid);
                renderProcedureModalTabs(uid);
            }
        });
    });

    updateVerifyFooterButton(uid);
}

// Bulk-toggles the roles bar's ✓ state — see renderVoucherChecksBar()'s selectAllTargets comment
// for the "never touch ✗" rule. Mirrors the per-check button's own optimistic-save/revert-on-
// failure shape, batched via Promise.all rather than one save per check.
async function toggleAllVoucherChecks(uid) {
    const transactionRef = hostAdapter.currentVoucherId;
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
    hostAdapter.markAllLinesDirty('voucher-level checks changed');
    renderProcedureModalTabs(uid);

    // Verification persists the complete voucher-check state in grouped Save -> Verify. Audit
    // Workbook provides the direct batch method used below.
    if (typeof hostAdapter.saveVoucherChecksBatch !== 'function') {
        invalidateProcedureModalData(uid);
        return;
    }

    // Re-queried after the render above (not the click's own captured element) since that render
    // just replaced the select-all button's DOM node - a spinner on the old reference would be
    // invisible. Shown once for the whole batch, not per-item (LoadingUI's show/hide state is a
    // single WeakMap slot per element - N concurrent calls on the same button would race).
    const selectAllBtn = document.getElementById('procedureVoucherChecksSelectAllBtn');
    if (window.LoadingUI && selectAllBtn) window.LoadingUI.showSpinner(selectAllBtn, 'Saving');

    try {
        const result = await hostAdapter.saveVoucherChecksBatch({
            transactionRef,
            checks: toChange.map(c => ({ check_id: c.id, state: newState }))
        });
        if (!result || !result.ok) {
            throw new Error(result?.error?.detail || 'Failed to save voucher checks.');
        }
        renderVoucherChecksBar(uid);
        renderProcedureModalTabs(uid);
        invalidateProcedureModalData(uid);
        hostAdapter.markAllLinesDirty('voucher-level checks changed');
    } catch (e) {
        console.error('Failed to save all voucher checks:', e);
        toChange.forEach(c => { c.state = previousStates[c.id]; });
        hostAdapter.showStatus('Failed to update all checks — please try again.', 'error');
        renderVoucherChecksBar(uid);
        renderProcedureModalTabs(uid);
    } finally {
        if (window.LoadingUI && selectAllBtn) window.LoadingUI.hideSpinner(selectAllBtn);
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
    // Physical Bill evidence type: checked BEFORE doc.template_id (always null for this doc
    // type) - mirrors compute_and_persist_cleared_status()'s own document_type-first branch
    // order, so a physical_bill Document never silently falls into the no-template role-tick
    // branch below. Reflects SAVED state only (not an unsaved draft edit) - same "dot reflects
    // committed truth" convention every other document type already follows: gated by
    // tabState.physicalBillSaved (must have been saved at least once this session, or loaded
    // with existing bills), with completeness itself computed over the current (possibly
    // since-edited) draft. Multi-Bill/Multi-Line Physical Bill, Phase 3 of 6: mirrors
    // _evaluate_physical_bill_completion() - every bill needs non-blank bill_no/bill_date/
    // vendor_name and at least one item, every item's qty*rate must resolve numeric, and the
    // grand total across every bill/item must equal this line's own amount within the same
    // epsilon the backend gate uses (CROSS_CHECK_FLOAT_EPSILON = 0.005).
    if (doc.document_type === 'physical_bill') {
        const tabState = getProcedureTabState(uid, doc.id);
        if (tabState.physicalBillSaved) {
            const lineState = hostAdapter.procedureModalState[uid];
            const voucherLineAmount = (lineState && lineState.voucherLineAmount) || 0;
            baseComplete = computePhysicalBillCompletion(tabState.physicalBills, voucherLineAmount);
        } else {
            baseComplete = false;
        }
    } else if (doc.template_id) {
        const lineState = hostAdapter.procedureModalState[uid];
        baseComplete = !!(lineState && lineState.assignedDocIds && lineState.assignedDocIds.has(doc.id));
    } else {
        const tabState = getProcedureTabState(uid, doc.id);
        const roles = doc.auth_roles || [];
        baseComplete = roles.length > 0 && roles.every(role => tabState.checklist[role] && tabState.checklist[role].is_checked);
    }
    if (!baseComplete) return false;
    if (doc.transaction_level_approval) {
        return isVoucherChecksComplete(voucherChecksCache[hostAdapter.currentVoucherId]);
    }
    return true;
}

// Renders the left-side document rail for one line's modal: one entry per document, name + a
// completion dot (see isProcedureDocumentComplete() above).
function renderProcedureModalTabs(uid) {
    const container = document.getElementById('procedureModalTabs');
    if (!container) return;
    const lineState = hostAdapter.procedureModalState[uid];
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
                        <span class="procedure-tab-label">${hostAdapter.escapeHtml(doc.name)}</span>
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
                    <span class="procedure-tab-label">${hostAdapter.escapeHtml(doc.name)}</span>
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
    const lineState = hostAdapter.procedureModalState[uid];
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
            toggleProcedureCheck(uid, docId, this.dataset.role, this.checked, this);
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
    // Prev/Next/Complete/Exit (via hostAdapter.saveLineItems()) and on modal close (via closeProcedureModal());
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
            hostAdapter.markLineDirty(uid, 'observation edited');
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
            hostAdapter.markLineDirty(uid, 'observation suggestion applied');
            if (observationInput) observationInput.value = tabState.observation;
            updateProcedureModalSavedNote(uid);
            refreshObservationButtons();
        });
    }

    if (copyBtn) {
        copyBtn.addEventListener('click', async function() {
            if (copyBtn.disabled) return;
            await saveLineObservation(uid, docId, copyBtn);
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
            hostAdapter.markLineDirty(uid, 'observation restored');
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
            hostAdapter.markLineDirty(uid, 'system observation appended');
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

    const lineIdx = hostAdapter.currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    const voucherLine = lineIdx !== -1 ? hostAdapter.currentVoucherLines[lineIdx] : null;
    const currentAccountName = voucherLine ? hostAdapter.getVoucherLineAlias(voucherLine.row_data || {}, hostAdapter.VOUCHER_LINE_ACCOUNT_NAME_ALIASES) : '—';

    container.innerHTML = `
        <div class="procedure-tab-panel" data-doc-id="${VOUCHER_LINE_TAB_ID}">
            <div class="procedure-segment procedure-segment-data">
                <h4 class="procedure-segment-header">🎯 Account Head Recommendation</h4>
                <div class="procedure-segment-body">
                    <div class="procedure-account-head-current">
                        <label>Current Account Name</label>
                        <span>${hostAdapter.escapeHtml(currentAccountName)}</span>
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

    // Phase 3c: warm the Trial Balance leaf-account list the first time this card renders (i.e.
    // the user actually opened the "Voucher Line" tab), fire-and-forget. Safe to not await: the
    // match-mode markup below reads nothing from latestTbAccountsCache — only
    // renderAccountHeadSearchDropdown() (on keystroke) and the Suggest/"Use" handlers do, all
    // strictly later. fetchLatestTbAccounts() caches per client + dedups, so repeat renders and
    // mode toggles cost nothing.
    fetchLatestTbAccounts(hostAdapter.getClientId());

    if (state.mode === 'nomatch') {
        bodyEl.innerHTML = `
            <div class="procedure-voucher-approval-field">
                <label for="procedureAccountHeadNoMatchInput">Finding — no suitable account in Trial Balance</label>
                <textarea id="procedureAccountHeadNoMatchInput" class="procedure-observation-textarea" rows="3"
                    placeholder="Describe the recommended new account head...">${hostAdapter.escapeHtml(state.noMatchText || '')}</textarea>
            </div>
            <button type="button" id="procedureAccountHeadToggleBtn" class="procedure-account-head-toggle-link">🔍 Search Trial Balance instead</button>
        `;
        const noMatchInput = document.getElementById('procedureAccountHeadNoMatchInput');
        if (noMatchInput) {
            noMatchInput.addEventListener('input', function() {
                state.noMatchText = this.value;
                state.noMatchDirty = true;
                hostAdapter.markLineDirty(uid, 'account-head finding edited');
            });
        }
    } else {
        bodyEl.innerHTML = `
            <div class="procedure-voucher-approval-field">
                <label for="procedureAccountHeadSearchInput">Recommended Account Head</label>
                <input type="text" id="procedureAccountHeadSearchInput" class="procedure-search-input"
                    placeholder="Search Trial Balance accounts..." autocomplete="off"
                    value="${hostAdapter.escapeHtml(state.selectedAccountName || state.searchTerm || '')}">
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
        <div class="procedure-result-item" data-account-id="${a.id}" data-account-name="${hostAdapter.escapeHtml(a.account_name)}">
            <div class="procedure-result-main">
                <span>${hostAdapter.escapeHtml(a.account_name)}</span>
                <span class="procedure-result-meta">${hostAdapter.escapeHtml(a.account_code || '')}</span>
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
        resultsEl.innerHTML = `<p class="procedure-account-head-no-match">${hostAdapter.escapeHtml(state.message || 'No match — new head likely needed')}</p>`;
        return;
    }

    resultsEl.innerHTML = state.suggestions.map((s, idx) => `
        <div class="procedure-account-head-suggestion">
            <div class="procedure-account-head-suggestion-row">
                <span class="procedure-account-head-suggestion-name">${hostAdapter.escapeHtml(s.account_name)}</span>
                <span class="procedure-account-head-suggestion-score">Score: ${hostAdapter.escapeHtml(String(s.score))}</span>
                <button type="button" class="procedure-account-head-use-btn" data-suggestion-idx="${idx}" title="Select this account in the dropdown above">📋 Use</button>
            </div>
            <ul class="procedure-account-head-reasons">
                ${(s.reasons || []).map(r => `<li>${hostAdapter.escapeHtml(r)}</li>`).join('')}
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
                hostAdapter.showStatus(`"${suggestion.account_name}" is no longer in the current Trial Balance — pick a leaf account manually if it still applies.`, 'error');
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
    const lineIdx = hostAdapter.currentVoucherLines.findIndex(l => String(l.detail_id) === String(uid));
    const voucherLine = lineIdx !== -1 ? hostAdapter.currentVoucherLines[lineIdx] : null;
    const rowData = voucherLine ? (voucherLine.row_data || {}) : {};
    const accountName = hostAdapter.getVoucherLineAlias(rowData, hostAdapter.VOUCHER_LINE_ACCOUNT_NAME_ALIASES);
    const rawDescription = hostAdapter.getVoucherLineAlias(rowData, hostAdapter.VOUCHER_LINE_DESCRIPTION_ALIASES);
    const description = (rawDescription && rawDescription !== '—') ? rawDescription : accountName;
    const category = (hostAdapter.voucherLineState[uid] && hostAdapter.voucherLineState[uid].transactionType) || '';
    const clientId = hostAdapter.getClientId();

    state.loading = true;
    renderAccountHeadResultsButtonState(uid);

    try {
        const params = new URLSearchParams({ client_id: clientId, description: description });
        if (category) params.set('category', category);
        const res = await window.ApiUtils.apiFetch(`/api/verification/suggest-account-head?${params.toString()}`);
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

// A not_evaluated rule can never leave that state no matter what gets linked when it has fewer
// than 2 configured Documents and no voucher-line comparison — there's nothing for a linked value
// to compare against (chain.length === 0 can't produce an anchor either way, so it's unconfigured
// regardless of compare_to_voucher_line; kept for a future non-per-Document reader even though
// today's per-Document-tab filtering, docCrossChecks below, means a chain can never actually be
// empty when this runs — the open Document is always a chain member). Distinguishes a genuine
// Audit Rules setup gap ("check not configured") from this voucher line simply not having enough
// evidence linked yet ("documents not linked yet") — see summarizeCrossCheckRule()/
// buildCrossCheckEmptyStateDetail() below, both empty/no-data redesign, Phase 2.
function isCrossCheckRuleUnconfigured(rule) {
    if (rule.chain.length === 0) return true;
    return rule.chain.length < 2 && !rule.compare_to_voucher_line;
}

// One compact line per rule. A shortfall gets its own distinct (amber) state — not folded into
// fail — since Phase 7/8 already treat it differently (doesn't gate Cleared, only observed).
function summarizeCrossCheckRule(rule) {
    const field = hostAdapter.escapeHtml(rule.compare_field);
    if (rule.status === 'fail') {
        return { icon: '✗', text: `${field} mismatch`, cssClass: 'cc-badge-fail' };
    }
    if (rule.status === 'shortfall') {
        return { icon: '⚠', text: `${field} shortfall`, cssClass: 'cc-badge-shortfall' };
    }
    if (rule.status === 'not_evaluated') {
        if (isCrossCheckRuleUnconfigured(rule)) {
            return { icon: '⚙', text: `${field}: check not configured`, cssClass: 'cc-badge-neutral' };
        }
        return { icon: '—', text: `${field}: documents not linked yet`, cssClass: 'cc-badge-neutral' };
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
// new lookups. not_evaluated is a dedicated redesign (buildCrossCheckEmptyStateDetail(), below) —
// pass/fail/shortfall keep this original raw chain-list rendering unchanged.
function buildCrossCheckRuleDetail(rule) {
    if (rule.status === 'not_evaluated') {
        return buildCrossCheckEmptyStateDetail(rule);
    }
    let html = '<ul class="cc-chain-list">';
    rule.chain.forEach(c => {
        const isAnchor = rule.anchor_document_id === c.document_id;
        const valueText = c.linked
            ? (c.value !== null ? hostAdapter.escapeHtml(String(c.value)) : '<span class="cc-chain-novalue">no value</span>')
            : '<span class="cc-chain-notlinked">not linked</span>';
        html += `<li class="cc-chain-item">
            <span class="cc-chain-doc">${hostAdapter.escapeHtml(c.document_name)}${isAnchor ? ' <span class="cc-anchor-tag">(anchor)</span>' : ''}</span>
            <span class="cc-chain-value">${valueText}</span>
        </li>`;
    });
    if (rule.voucher_line_comparison) {
        const vlc = rule.voucher_line_comparison;
        const matchIcon = vlc.match === true ? '✓' : (vlc.match === false ? '✗' : '—');
        html += `<li class="cc-chain-item cc-chain-voucher-row">
            <span class="cc-chain-doc">Voucher Line Amount</span>
            <span class="cc-chain-value">${matchIcon} ${vlc.voucher_amount !== null ? hostAdapter.escapeHtml(String(vlc.voucher_amount)) : '—'}</span>
        </li>`;
    }
    html += '</ul>';
    if (rule.message) {
        html += `<div class="cc-rule-message cc-rule-message-${rule.status}">${hostAdapter.escapeHtml(rule.message)}</div>`;
    }
    return html;
}

// Empty/no-data state redesign (Phase 2): replaces the old plain "<field> not enough data" text
// for a not_evaluated rule with a chip row (name + a ✓/✕ circle per configured Document, reusing
// the same tick/cross circle language as the voucher-level Checks bar's
// .procedure-voucher-check-btn, style.css) so an auditor can see at a glance exactly which
// Documents still need linking, a plain-English explainer that differs for the two causes
// (isCrossCheckRuleUnconfigured() above), a static info-icon tooltip (title attribute only, no
// popover — same convention as the column-notes ℹ️ icon in static/templates.js), and a footnote
// that reuses the engine's own already-computed rule.message verbatim (no new wording to keep in
// sync with the backend). Every value used here already exists on `rule` — no new fetch.
function buildCrossCheckEmptyStateDetail(rule) {
    const unconfigured = isCrossCheckRuleUnconfigured(rule);

    let chipsHtml = rule.chain.map(c => `
        <span class="cc-empty-chip">
            <span class="cc-empty-chip-circle${c.linked ? ' linked' : ''}">${c.linked ? '✓' : '✕'}</span>
            <span class="cc-empty-chip-name">${hostAdapter.escapeHtml(c.document_name)}</span>
        </span>
    `).join('');

    if (rule.compare_to_voucher_line) {
        const vlc = rule.voucher_line_comparison;
        const hasAnchorValue = !!(vlc && vlc.anchor_value !== null);
        chipsHtml += `
            <span class="cc-empty-chip">
                <span class="cc-empty-chip-circle${hasAnchorValue ? ' linked' : ''}">${hasAnchorValue ? '✓' : '✕'}</span>
                <span class="cc-empty-chip-name">Voucher Line Amount</span>
            </span>
        `;
    }

    const explainerText = unconfigured
        ? 'This check needs another document to compare against, or a voucher-line comparison, before it can run.'
        : `Link the document${rule.chain.length > 1 ? 's' : ''} above as evidence on this voucher line to run this check.`;

    const tooltipText = 'A Cross-check compares a value across two or more documents, or against ' +
        'the amount on this voucher line. It shows no result until enough of them are linked here ' +
        'with a usable value.';

    const footnoteHtml = rule.message
        ? `<p class="cc-empty-footnote">${hostAdapter.escapeHtml(rule.message)}</p>`
        : '';

    return `
        <div class="cc-empty-state">
            <div class="cc-empty-chips">${chipsHtml}</div>
            <p class="cc-empty-explainer">
                <span>${hostAdapter.escapeHtml(explainerText)}</span>
                <span class="cc-info-icon" title="${hostAdapter.escapeHtml(tooltipText)}">ℹ️</span>
            </p>
            ${footnoteHtml}
        </div>
    `;
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
    const lineState = hostAdapter.procedureModalState[uid];
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
                        placeholder="Search by reference, vendor, amount..." value="${hostAdapter.escapeHtml(tabState.searchTerm)}">
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
        <div class="procedure-segment procedure-segment-procedures ${allRolesChecked ? 'complete' : 'pending'}">
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
                unassignSupportingDocument(uid, docId, parseInt(this.dataset.assignmentId), this);
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
// per-role toggleProcedureCheck() loop — instead applies the optimistic update once in memory,
// re-renders, and marks the line dirty so the background per-line save persists it. Same
// local-first shape as toggleAllVoucherChecks() above.
async function toggleAllProcedureChecks(uid, docId) {
    const lineState = hostAdapter.procedureModalState[uid];
    const doc = lineState && (lineState.documents || []).find(d => d.id === docId);
    const roles = (doc && doc.auth_roles) || [];
    if (!roles.length) return;
    const tabState = getProcedureTabState(uid, docId);
    const allChecked = roles.every(r => tabState.checklist[r] && tabState.checklist[r].is_checked);
    const newChecked = !allChecked;
    const toChange = roles.filter(r => !!(tabState.checklist[r] && tabState.checklist[r].is_checked) !== newChecked);
    if (!toChange.length) return;

    // Local-first: apply the optimistic tick change in memory only. There is no network save
    // here anymore (the per-line save runs in the background off markLineDirty), so there is
    // nothing to roll back and no previous-state capture is needed.
    toChange.forEach(role => {
        if (!tabState.checklist[role]) tabState.checklist[role] = {};
        tabState.checklist[role].is_checked = newChecked;
    });
    renderProcedureModalPanel(uid, docId);
    renderProcedureModalTabs(uid);
    hostAdapter.markLineDirty(uid, 'procedure checks changed');
    // The in-memory checklist has now diverged from this line's cached modal payload, so that
    // cache entry is stale — drop it. A later reopen re-fetches once the background per-line
    // save has run.
    invalidateProcedureModalData(uid);
}

// ---------- PHYSICAL BILL ENTRY (evidence type; Multi-Bill/Multi-Line, Phase 3 of 6) ----------
// Small line-style SVG icons (stroke=currentColor, no fill) used throughout this form in place of
// emoji — purely decorative, sized/aligned via the shared .pb-icon wrapper class in style.css.
const PB_ICON_RECEIPT = '<svg class="pb-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h12v18l-3-2-3 2-3-2-3 2V3z"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="9" y1="12" x2="15" y2="12"/></svg>';
const PB_ICON_DETAILS = '<svg class="pb-icon" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>';
const PB_ICON_CHECK = '<svg class="pb-icon" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
const PB_ICON_PLUS = '<svg class="pb-icon" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
const PB_ICON_REMOVE = '<svg class="pb-icon" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
const PB_ICON_COPY = '<svg class="pb-icon" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';

// Float-safety epsilon matching CROSS_CHECK_FLOAT_EPSILON (routers/verification.py) — used both
// for the grand-total-vs-voucher-amount reconciliation pill and the completion-dot gate below, so
// neither can ever disagree with the backend's own _cross_check_values_equal()/
// _evaluate_physical_bill_completion() epsilon.
const PB_FLOAT_EPSILON = 0.005;

// Strips a Quantity/Rate input's typed value down to digits and at most one decimal point (confirmed
// fix, carried over) — used on every 'input' event so pasted/typed letters and symbols never survive,
// and a second '.' collapses away rather than producing an unparseable string like "1.2.3".
function pbSanitizeNumericInput(str) {
    let cleaned = str.replace(/[^0-9.]/g, '');
    const firstDot = cleaned.indexOf('.');
    if (firstDot !== -1) {
        cleaned = cleaned.slice(0, firstDot + 1) + cleaned.slice(firstDot + 1).replace(/\./g, '');
    }
    return cleaned;
}

// Formats a raw number for display (subtotal/grand total rows, item Amount cells) — display only,
// never fed back into what gets saved. Returns '—' for anything not a finite number.
function formatBillTotalDisplay(value) {
    const num = parseFloat(value);
    if (isNaN(num)) return '—';
    return num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// One empty bill card — items start empty; the user adds lines via the per-card "Add line" button.
function makeEmptyPhysicalBill() {
    return { bill_no: '', bill_date: '', vendor_name: '', items: [] };
}

function makeEmptyPhysicalBillItem() {
    return { item_name: '', quantity: '', rate: '', amount: null };
}

// Recomputes one item's derived Amount (qty*rate, or null if either isn't currently a valid
// number) and caches it on the item object — the single source both the item's own Amount cell
// and the bill/grand-total sums read from.
function recomputePhysicalBillItemAmount(item) {
    const qty = parseFloat(item.quantity);
    const rate = parseFloat(item.rate);
    const bothValid = item.quantity !== '' && item.rate !== '' && !isNaN(qty) && !isNaN(rate);
    item.amount = bothValid ? qty * rate : null;
    return item.amount;
}

// A bill's subtotal for display — sums every item's cached Amount, treating a not-yet-resolvable
// item (blank/invalid Qty or Rate) as 0 so the running total stays a useful live preview rather
// than going blank the moment one row is mid-edit. This is display-only; the actual Cleared/Query
// gate (_evaluate_physical_bill_completion(), mirrored by computePhysicalBillCompletion() below)
// requires every item to resolve numeric, with no such leniency.
function computePhysicalBillSubtotal(bill) {
    return (bill.items || []).reduce((sum, item) => sum + (item.amount != null ? item.amount : 0), 0);
}

function computePhysicalBillGrandTotal(bills) {
    return (bills || []).reduce((sum, bill) => sum + computePhysicalBillSubtotal(bill), 0);
}

// Client-side mirror of _evaluate_physical_bill_completion() (routers/verification.py) — every
// bill needs non-blank bill_no/bill_date/vendor_name and at least one item; every item's Qty*Rate
// must resolve to a number; the grand total across every bill/item must equal the voucher line's
// own amount within PB_FLOAT_EPSILON. No partial credit — one incomplete bill or item, or a
// mismatched total, fails the whole document. Used only for the rail's completion dot; the real
// gate is always evaluated server-side.
function computePhysicalBillCompletion(bills, voucherLineAmount) {
    if (!bills || !bills.length || voucherLineAmount == null) return false;
    let grandTotal = 0;
    for (const bill of bills) {
        if (!bill.bill_no || !bill.bill_no.trim()) return false;
        if (!bill.bill_date || !bill.bill_date.trim()) return false;
        if (!bill.vendor_name || !bill.vendor_name.trim()) return false;
        if (!bill.items || !bill.items.length) return false;
        for (const item of bill.items) {
            const amount = recomputePhysicalBillItemAmount(item);
            if (amount == null || isNaN(amount)) return false;
            grandTotal += amount;
        }
    }
    return Math.abs(grandTotal - voucherLineAmount) <= PB_FLOAT_EPSILON;
}

// Reconciliation pill shown beside the Grand Total — "Reconciled" once the grand total matches
// the voucher line's own amount within PB_FLOAT_EPSILON (same epsilon the backend gate uses, no
// wider tolerance), otherwise "Over by X" / "Short by X".
function buildPhysicalBillReconciliationStatus(grandTotal, voucherLineAmount) {
    const diff = grandTotal - voucherLineAmount;
    if (Math.abs(diff) <= PB_FLOAT_EPSILON) {
        return { cls: 'reconciled', label: `${PB_ICON_CHECK} Reconciled` };
    }
    if (diff > 0) {
        return { cls: 'over', label: `Over by ${formatBillTotalDisplay(diff)}` };
    }
    return { cls: 'short', label: `Short by ${formatBillTotalDisplay(-diff)}` };
}

// Mandatory-for-Cleared visual cue (confirmed requirement, purely visual — never blocks saving or
// Verify/Verify & Next). Bill No/Date/Vendor Name are the fields the Cleared/Query gate actually
// requires; a blank one gets a red border + tooltip. Evaluated only at panel render and after a
// save completes (savePhysicalBillEntry()) — never live as-you-type — same convention as before,
// now walking every currently-rendered bill card via data-bill-idx/data-field rather than the old
// design's fixed element ids (which assumed exactly one bill per document). No-ops safely if this
// document's panel isn't the one currently rendered.
function refreshPhysicalBillMandatoryIndicators(uid, docId) {
    const container = document.getElementById('procedureModalPanels');
    if (!container) return;
    const panel = container.querySelector(`.procedure-tab-panel[data-doc-id="${docId}"]`);
    if (!panel) return;
    const tabState = getProcedureTabState(uid, docId);
    (tabState.physicalBills || []).forEach((bill, billIdx) => {
        ['bill_no', 'bill_date', 'vendor_name'].forEach(field => {
            const el = panel.querySelector(`[data-bill-idx="${billIdx}"][data-field="${field}"]`);
            if (!el) return;
            const isBlank = !bill[field] || !String(bill[field]).trim();
            el.classList.toggle('procedure-bill-input-mandatory-missing', isBlank);
            el.title = isBlank ? 'Required for status to clear' : '';
        });
    });
}

// Recomputes and repaints one item row's Amount cell, its bill's Subtotal, and the Grand
// Total/reconciliation pill — the targeted (no full re-render) update path used on every Rate/Qty
// keystroke so typing never loses focus. Structural changes (Add/Remove Bill/line) go through a
// full renderPhysicalBillDocumentPanel() re-render instead (see the click handlers below).
function updatePhysicalBillTotals(uid, docId, billIdx) {
    const container = document.getElementById('procedureModalPanels');
    if (!container) return;
    const panel = container.querySelector(`.procedure-tab-panel[data-doc-id="${docId}"]`);
    if (!panel) return;
    const tabState = getProcedureTabState(uid, docId);
    const bill = tabState.physicalBills[billIdx];
    if (!bill) return;

    if (billIdx != null) {
        bill.items.forEach((item, itemIdx) => {
            const amountCell = panel.querySelector(`.procedure-bill-item-amount[data-bill-idx="${billIdx}"][data-item-idx="${itemIdx}"]`);
            if (amountCell) amountCell.textContent = formatBillTotalDisplay(item.amount);
        });
        const subtotalEl = panel.querySelector(`.procedure-bill-subtotal-value[data-bill-idx="${billIdx}"]`);
        if (subtotalEl) subtotalEl.textContent = formatBillTotalDisplay(computePhysicalBillSubtotal(bill));
    }

    const lineState = hostAdapter.procedureModalState[uid];
    const voucherLineAmount = (lineState && lineState.voucherLineAmount) || 0;
    const grandTotal = computePhysicalBillGrandTotal(tabState.physicalBills);
    const grandTotalEl = panel.querySelector('#procedureBillGrandTotalValue');
    if (grandTotalEl) grandTotalEl.textContent = formatBillTotalDisplay(grandTotal);
    const recon = buildPhysicalBillReconciliationStatus(grandTotal, voucherLineAmount);
    const reconEl = panel.querySelector('#procedureBillReconStatus');
    if (reconEl) {
        reconEl.className = `procedure-bill-recon-pill procedure-bill-recon-${recon.cls}`;
        reconEl.innerHTML = recon.label;
    }
}

// One bill card: header fields (Bill No/Vendor/Date + remove-bill), an items table (Item/Rate/
// Qty/Amount-derived/remove-line), an "Add line" button, and a dimmed subtotal row.
function renderPhysicalBillCard(bill, billIdx) {
    const subtotal = computePhysicalBillSubtotal(bill);
    const itemRows = (bill.items || []).map((item, itemIdx) => `
        <tr>
            <td><input type="text" class="procedure-bill-input" data-bill-idx="${billIdx}" data-item-idx="${itemIdx}" data-field="item_name" value="${hostAdapter.escapeHtml(item.item_name)}" placeholder="Item"></td>
            <td><input type="text" inputmode="decimal" class="procedure-bill-input" data-bill-idx="${billIdx}" data-item-idx="${itemIdx}" data-field="rate" value="${hostAdapter.escapeHtml(item.rate)}" placeholder="0"></td>
            <td><input type="text" inputmode="decimal" class="procedure-bill-input" data-bill-idx="${billIdx}" data-item-idx="${itemIdx}" data-field="quantity" value="${hostAdapter.escapeHtml(item.quantity)}" placeholder="0"></td>
            <td class="procedure-bill-item-amount" data-bill-idx="${billIdx}" data-item-idx="${itemIdx}">${formatBillTotalDisplay(item.amount)}</td>
            <td><button type="button" class="procedure-bill-remove-line-btn" data-bill-idx="${billIdx}" data-item-idx="${itemIdx}" title="Remove line">${PB_ICON_REMOVE}</button></td>
        </tr>
    `).join('');

    return `
        <div class="procedure-bill-card" data-bill-idx="${billIdx}">
            <div class="procedure-bill-card-header">
                <div class="procedure-bill-field">
                    <label>Bill No</label>
                    <input type="text" class="procedure-bill-input" data-bill-idx="${billIdx}" data-field="bill_no" value="${hostAdapter.escapeHtml(bill.bill_no)}">
                </div>
                <div class="procedure-bill-field">
                    <label>Vendor Name</label>
                    <input type="text" class="procedure-bill-input" data-bill-idx="${billIdx}" data-field="vendor_name" value="${hostAdapter.escapeHtml(bill.vendor_name)}">
                </div>
                <div class="procedure-bill-field">
                    <label>Date</label>
                    <input type="date" class="procedure-bill-input" data-bill-idx="${billIdx}" data-field="bill_date" value="${hostAdapter.escapeHtml(bill.bill_date)}">
                </div>
                <button type="button" class="procedure-bill-remove-bill-btn" data-bill-idx="${billIdx}" title="Remove bill">${PB_ICON_REMOVE} Remove Bill</button>
            </div>
            <table class="procedure-bill-items-table">
                <thead>
                    <tr><th>Item</th><th>Rate</th><th>Qty</th><th>Amount</th><th></th></tr>
                </thead>
                <tbody>${itemRows}</tbody>
            </table>
            <button type="button" class="procedure-bill-add-line-btn" data-bill-idx="${billIdx}">${PB_ICON_PLUS} Add line</button>
            <div class="procedure-bill-subtotal-row">
                <span class="procedure-bill-subtotal-label">Subtotal</span>
                <span class="procedure-bill-subtotal-value" data-bill-idx="${billIdx}">${formatBillTotalDisplay(subtotal)}</span>
            </div>
        </div>
    `;
}

// ---------- COPY FROM LINES (Phase 5 of 6 — rebuilt against the nested bills[].items[] shape) ----------
// Resolves a Document's own template to a Physical Bill "source type" ('grn'/'vendor_bill') purely
// from templateNameCache, same case-insensitive match resolve_physical_bill_fields_from_source()
// (routers/verification.py) uses via its own PHYSICAL_BILL_SOURCE_TEMPLATE_NAMES — reused here only
// to find eligible source rows, no server call made.
function resolvePhysicalBillSourceType(templateId) {
    const templateName = (templateNameCache[templateId] || '').trim().toLowerCase();
    if (templateName === 'store receiving') return 'grn';
    if (templateName === 'vendor bill') return 'vendor_bill';
    return null;
}

// Every GRN/Vendor Bill row currently linked as evidence anywhere on this line, across every other
// Document — no new fetch needed: openProcedureModal() already seeds every document's tabState.
// selections (with full row_data) from its batch assignedRecords fetch, and already warms
// templateNameCache for every document's template_id before any tab renders. Quantity/Rate are read
// case-insensitively via the same getRowFieldCaseInsensitive() the Linked Evidence totals row
// already uses. Item Name has no source column on either template (confirmed by inspecting both
// templates' actual configured columns) — left for the copied item's default blank value.
function getEligiblePhysicalBillCopySourceRows(uid) {
    const lineState = hostAdapter.procedureModalState[uid];
    if (!lineState) return [];
    const rows = [];
    (lineState.documents || []).forEach(otherDoc => {
        const sourceType = resolvePhysicalBillSourceType(otherDoc.template_id);
        if (!sourceType) return;
        const tabState = getProcedureTabState(uid, otherDoc.id);
        (tabState.selections || []).forEach(sel => {
            rows.push({
                quantity: getRowFieldCaseInsensitive(sel.row_data, 'Quantity'),
                rate: getRowFieldCaseInsensitive(sel.row_data, 'Rate')
            });
        });
    });
    return rows;
}

// One-time seed action (not a live sync): creates a single new bill, populated with one item per
// currently-eligible source row (Quantity/Rate carried across, Amount derived; Item Name/Bill No/
// Vendor Name/Date all left blank for manual entry — no reliable source for any of them). Only ever
// called from the empty-state's Copy from lines button, which itself only renders while this
// Document has zero bills (confirmed requirement) — so no separate "already has bills" guard is
// needed here beyond the eligible-rows check.
function copyPhysicalBillFromLines(uid, doc) {
    const tabState = getProcedureTabState(uid, doc.id);
    const eligibleRows = getEligiblePhysicalBillCopySourceRows(uid);
    if (!eligibleRows.length) return;
    const items = eligibleRows.map(row => {
        const item = { item_name: '', quantity: row.quantity, rate: row.rate, amount: null };
        recomputePhysicalBillItemAmount(item);
        return item;
    });
    tabState.physicalBills.push({ bill_no: '', bill_date: '', vendor_name: '', items });
    tabState.physicalBillDirty = true;
    hostAdapter.markLineDirty(uid, 'physical bill copied from lines');
    renderPhysicalBillDocumentPanel(uid, doc);
}

// Dedicated panel for a physical_bill Document — one card per bill, each with its own editable
// item-lines table, plus an "Add Bill" button and a Grand Total/reconciliation footer. Replaces
// the earlier one-bill-per-Document 4-field form (Phase 7 of 8) now that a Document can carry
// several bills, each with several line items (Phase 1-2 of this requirement). No Audit
// Procedures segment here (same as before — neither concept applies to this doc type). The
// Observation segment is unchanged, same as every other document type.
function renderPhysicalBillDocumentPanel(uid, doc) {
    const container = document.getElementById('procedureModalPanels');
    if (!container) return;
    const tabState = getProcedureTabState(uid, doc.id);
    const bills = tabState.physicalBills || [];

    const lineState = hostAdapter.procedureModalState[uid];
    const voucherLineAmount = (lineState && lineState.voucherLineAmount) || 0;
    const grandTotal = computePhysicalBillGrandTotal(bills);
    const recon = buildPhysicalBillReconciliationStatus(grandTotal, voucherLineAmount);

    // Copy from lines (Phase 5 of 6): only offered while this Document has zero bills yet — a
    // one-time seed action, not a repeatable append (confirmed requirement) — and only when there
    // is at least one eligible GRN/Vendor Bill row to seed from.
    const eligibleForCopy = bills.length === 0 ? getEligiblePhysicalBillCopySourceRows(uid) : [];
    const cardsHtml = bills.length
        ? bills.map((bill, billIdx) => renderPhysicalBillCard(bill, billIdx)).join('')
        : `<div class="procedure-bill-empty-state">
            No bills entered yet.
            ${eligibleForCopy.length ? `<button type="button" id="procedureBillCopyBtn" class="procedure-bill-copy-btn">${PB_ICON_COPY} Copy from lines (${eligibleForCopy.length})</button>` : ''}
           </div>`;

    container.innerHTML = `
        <div class="procedure-tab-panel" data-doc-id="${doc.id}">
            <div class="procedure-segment procedure-segment-data">
                <h4 class="procedure-segment-header">${PB_ICON_RECEIPT} Physical Bill</h4>
                <div class="procedure-segment-body">
                    <div class="procedure-bill-form" id="procedureBillForm">
                        ${cardsHtml}
                        <button type="button" id="procedureBillAddBillBtn" class="procedure-bill-add-bill-btn">${PB_ICON_PLUS} Add Bill</button>
                        <div class="procedure-bill-grand-total-row">
                            <span class="procedure-bill-grand-total-label">${PB_ICON_DETAILS} Grand Total</span>
                            <span id="procedureBillGrandTotalValue" class="procedure-bill-grand-total-value">${formatBillTotalDisplay(grandTotal)}</span>
                            <span id="procedureBillReconStatus" class="procedure-bill-recon-pill procedure-bill-recon-${recon.cls}">${recon.label}</span>
                        </div>
                    </div>
                </div>
            </div>
            <div class="procedure-segment procedure-segment-observation">
                <h4 class="procedure-segment-header">📝 Observation</h4>
                <div class="procedure-segment-body">${buildObservationSection(uid, doc.id)}</div>
            </div>
        </div>
    `;

    const form = document.getElementById('procedureBillForm');
    if (form) {
        // Text/date fields (bill header + item name) — write straight into tabState on every
        // keystroke, no DOM update needed since the input already shows what was typed.
        form.querySelectorAll('input[data-field="bill_no"], input[data-field="vendor_name"], input[data-field="bill_date"], input[data-field="item_name"]').forEach(el => {
            el.addEventListener('input', function() {
                const billIdx = parseInt(this.dataset.billIdx);
                const bill = tabState.physicalBills[billIdx];
                if (!bill) return;
                if (this.dataset.itemIdx != null && this.dataset.itemIdx !== '') {
                    const item = bill.items[parseInt(this.dataset.itemIdx)];
                    if (item) item[this.dataset.field] = this.value;
                } else {
                    bill[this.dataset.field] = this.value;
                }
                tabState.physicalBillDirty = true;
                hostAdapter.markLineDirty(uid, 'physical bill edited');
            });
        });

        // Rate/Qty inputs — sanitized on every input, written into tabState, then a targeted
        // (non-full-re-render) update of this row's Amount + its bill's Subtotal + the Grand
        // Total/reconciliation pill, so typing never loses focus.
        form.querySelectorAll('input[data-field="rate"], input[data-field="quantity"]').forEach(el => {
            el.addEventListener('input', function() {
                const cleaned = pbSanitizeNumericInput(this.value);
                if (cleaned !== this.value) this.value = cleaned;
                const billIdx = parseInt(this.dataset.billIdx);
                const itemIdx = parseInt(this.dataset.itemIdx);
                const bill = tabState.physicalBills[billIdx];
                const item = bill && bill.items[itemIdx];
                if (!item) return;
                item[this.dataset.field] = cleaned;
                recomputePhysicalBillItemAmount(item);
                tabState.physicalBillDirty = true;
                hostAdapter.markLineDirty(uid, 'physical bill amount edited');
                updatePhysicalBillTotals(uid, doc.id, billIdx);
            });
        });

        form.querySelectorAll('.procedure-bill-remove-line-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                const billIdx = parseInt(this.dataset.billIdx);
                const itemIdx = parseInt(this.dataset.itemIdx);
                const bill = tabState.physicalBills[billIdx];
                if (!bill) return;
                bill.items.splice(itemIdx, 1);
                tabState.physicalBillDirty = true;
                hostAdapter.markLineDirty(uid, 'physical bill line removed');
                renderPhysicalBillDocumentPanel(uid, doc);
            });
        });

        form.querySelectorAll('.procedure-bill-add-line-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                const billIdx = parseInt(this.dataset.billIdx);
                const bill = tabState.physicalBills[billIdx];
                if (!bill) return;
                bill.items.push(makeEmptyPhysicalBillItem());
                tabState.physicalBillDirty = true;
                hostAdapter.markLineDirty(uid, 'physical bill line added');
                renderPhysicalBillDocumentPanel(uid, doc);
            });
        });

        form.querySelectorAll('.procedure-bill-remove-bill-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                const billIdx = parseInt(this.dataset.billIdx);
                tabState.physicalBills.splice(billIdx, 1);
                tabState.physicalBillDirty = true;
                hostAdapter.markLineDirty(uid, 'physical bill removed');
                renderPhysicalBillDocumentPanel(uid, doc);
            });
        });

        const addBillBtn = document.getElementById('procedureBillAddBillBtn');
        if (addBillBtn) {
            addBillBtn.addEventListener('click', function() {
                tabState.physicalBills.push(makeEmptyPhysicalBill());
                tabState.physicalBillDirty = true;
                hostAdapter.markLineDirty(uid, 'physical bill added');
                renderPhysicalBillDocumentPanel(uid, doc);
            });
        }

        const copyBtn = document.getElementById('procedureBillCopyBtn');
        if (copyBtn) {
            copyBtn.addEventListener('click', function() {
                copyPhysicalBillFromLines(uid, doc);
            });
        }

        // Multi-Bill/Multi-Line Physical Bill, Phase 4 of 6: Enter-key left-to-right navigation
        // (Bill No -> Vendor Name -> Date -> each line's Item -> Rate -> Qty, card after card).
        // .procedure-bill-input is applied to exactly these fields (never the derived Amount
        // span), in exactly this order by HTML source order — so the flat DOM-order list below
        // *is* the navigation order, with no manual bill/item index arithmetic and no special
        // case needed to flow from one bill card into the next. Tab is left untouched (native
        // default) — this only intercepts Enter.
        form.querySelectorAll('.procedure-bill-input').forEach(el => {
            el.addEventListener('keydown', function(e) {
                if (e.key !== 'Enter') return;
                e.preventDefault();
                const inputs = Array.from(form.querySelectorAll('.procedure-bill-input'));
                const idx = inputs.indexOf(this);
                if (idx === -1) return;
                // Last field of the last line of the last bill — those two facts (last in DOM
                // order + a quantity field) are only simultaneously true for the last item row's
                // Qty cell of the last bill card, since Qty is always an item row's last column
                // and item rows always come last in a card. Appends to *this* input's own bill
                // (its own data-bill-idx), not an assumed "last bill index".
                if (idx === inputs.length - 1 && this.dataset.field === 'quantity') {
                    const billIdx = parseInt(this.dataset.billIdx);
                    const bill = tabState.physicalBills[billIdx];
                    if (!bill) return;
                    const newItemIdx = bill.items.length;
                    bill.items.push(makeEmptyPhysicalBillItem());
                    tabState.physicalBillDirty = true;
                    hostAdapter.markLineDirty(uid, 'physical bill line added');
                    renderPhysicalBillDocumentPanel(uid, doc);
                    const newInput = document.querySelector(
                        `#procedureBillForm input[data-field="item_name"][data-bill-idx="${billIdx}"][data-item-idx="${newItemIdx}"]`
                    );
                    if (newInput) newInput.focus();
                    return;
                }
                if (idx < inputs.length - 1) {
                    inputs[idx + 1].focus();
                }
            });
        });
    }

    refreshPhysicalBillMandatoryIndicators(uid, doc.id);
    wireProcedureChecklistAndObservation(container, uid, doc.id, tabState);
}

// Auto-saves every bill for one physical_bill Document (POST save_physical_bill, full-replace
// upsert) — called only from flushPhysicalBillEntries() below, from the modal's four exit
// chokepoints (✕/Back/Verify/Verify & Next), never from a UI click (there is no Save button — a
// blank/incomplete Physical Bill tab is an expected, non-error state at this point in a line's
// review). A completely empty bills array is still sent (the backend drops any all-blank bill
// server-side, so an empty array or an array of all-blank bills behave the same). A failure is
// logged only, never a blocking alert()/toast, since the user is already leaving this line.
async function savePhysicalBillEntry(uid, docId) {
    const tabState = getProcedureTabState(uid, docId);
    const bills = tabState.physicalBills || [];
    let saved = false;

    try {
        const res = await window.ApiUtils.apiFetchMutate(`${API_BASE}/api/verification/save_physical_bill`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                voucher_line_id: parseInt(uid),
                document_id: docId,
                bills: bills.map(bill => ({
                    bill_no: (bill.bill_no || '').trim(),
                    bill_date: bill.bill_date,
                    vendor_name: (bill.vendor_name || '').trim(),
                    items: (bill.items || []).map(item => ({
                        item_name: (item.item_name || '').trim(),
                        quantity: item.quantity === '' ? null : item.quantity,
                        rate: item.rate === '' ? null : item.rate,
                        source_document_row_id: null,
                        source_template: null
                    }))
                }))
            })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to save physical bill.');
        }
        tabState.physicalBillSaved = true;
        tabState.physicalBillDirty = false;
        saved = true;
    } catch (e) {
        console.error('Failed to auto-save physical bill:', e);
    }
    refreshPhysicalBillMandatoryIndicators(uid, docId);
    return saved;
}

// Persists every Physical Bill Document tab of this line — called from the same exit chokepoints as
// flushProcedureObservations() (closeProcedureModal(), navigateProcedureModalLine(), and
// performVerifyVoucherChecks(), shared by both Verify and Verify & Next). Every physical_bill
// Document tab always has tabState.physicalBills seeded by openProcedureModal() (whether or not that
// tab was ever actually opened/viewed this session), so this saves ALL of them, not just the active
// tab — mirrors flushProcedureObservations()'s across-every-tab convention.
async function flushPhysicalBillEntries(uid) {
    if (!hostAdapter) return true;
    const lineState = hostAdapter.procedureModalState[uid];
    if (!lineState || !lineState.documents) return true;
    let allSaved = true;
    for (const doc of lineState.documents) {
        if (doc.document_type !== 'physical_bill') continue;
        const tabState = getProcedureTabState(uid, doc.id);
        if (!tabState.physicalBillDirty) continue;
        if (!await savePhysicalBillEntry(uid, doc.id)) allSaved = false;
    }
    return allSaved;
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
    const lineState = hostAdapter.procedureModalState[uid];
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
        const data = await fetchProcedureRows(doc.template_id, doc.id, term.trim(), 1, 10);
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
            if (row) await selectProcedureDocument(uid, docId, row, this);
        });
    });
}

// ---------- SELECTING A ROW (Phase 1's assign_document endpoint) ----------
// Links a Data Bank row to this voucher line for the active tab's document, then reflects the
// result locally — no full modal reload needed, since GET /api/data/preview's search/browse rows
// already carry everything the selected-lines table needs (row_data/extract_date/extract_amount).
// Returns true on success, false on failure, so callers (the dropdown and the browse modal) can
// each decide what to do next (clear the dropdown vs. keep the browse modal open and refresh it).
// target (optional) is the clicked dropdown item/browse-table row, for a LoadingUI spinner.
async function selectProcedureDocument(uid, docId, row, target = null) {
    const lineState = hostAdapter.procedureModalState[uid];
    if (!lineState) return false;
    const tabState = getProcedureTabState(uid, docId);
    try {
        const res = await window.ApiUtils.apiFetchMutate(`${API_BASE}/api/verification/assign_document`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ voucher_line_id: parseInt(uid), client_data_id: row.id, document_id: docId })
        }, { target, message: 'Assigning' });
        if (!res.ok) {
            // A real race, not hypothetical — the row's own UNIQUE(client_data_id) constraint is
            // the actual enforcement, so a concurrent assignment elsewhere can land first.
            const err = await res.json().catch(() => ({}));
            hostAdapter.showStatus(err.detail || 'That row is no longer available — it was just assigned elsewhere.', 'error');
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
        hostAdapter.markLineDirty(uid, 'supporting document linked');
        // Clear the search box/dropdown for this tab now that the row is locked — it stays
        // selected in the table below, no longer meaningful as a search result.
        tabState.searchTerm = '';
        tabState.searchResults = [];
        renderProcedureModalTabs(uid);
        renderProcedureModalPanel(uid, docId);
        hostAdapter.showStatus('✅ Evidence linked.', 'success');
        return true;
    } catch (e) {
        console.error('Failed to assign supporting document:', e);
        hostAdapter.showStatus('Failed to link evidence — please try again.', 'error');
        return false;
    }
}

// ---------- UNASSIGNING A ROW (Unassign requirement) ----------
// Soft-deletes a link via POST /api/verification/unassign_document, then updates local state and
// re-renders — reusing the exact same buildSelectedLinesTable()/getSelectedLinesSummary() logic
// Phase 4 already built, so the running total and Matches/mismatch badge recalculate automatically
// with no separate recompute path. Observation text for the tab is deliberately left untouched.
async function unassignSupportingDocument(uid, docId, assignmentId, target = null) {
    if (!confirm('Unlink this evidence from the voucher line? The Data Bank row will become available to assign elsewhere.')) {
        return;
    }
    const lineState = hostAdapter.procedureModalState[uid];
    if (!lineState) return;
    const tabState = getProcedureTabState(uid, docId);

    try {
        const res = await window.ApiUtils.apiFetchMutate(`${API_BASE}/api/verification/unassign_document`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ assignment_id: assignmentId })
        }, { target, message: 'Unlinking' });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            hostAdapter.showStatus(err.detail || 'Failed to unlink evidence — please try again.', 'error');
            return;
        }

        tabState.selections = (tabState.selections || []).filter(sel => sel.id !== assignmentId);
        // If that was this document's last active link, its tab dot should un-fill too.
        if (!tabState.selections.length) {
            lineState.assignedDocIds.delete(docId);
        }
        hostAdapter.markLineDirty(uid, 'supporting document unlinked');
        renderProcedureModalTabs(uid);
        renderProcedureModalPanel(uid, docId);
        hostAdapter.showStatus('✅ Evidence unlinked.', 'success');
    } catch (e) {
        console.error('Failed to unassign supporting document:', e);
        hostAdapter.showStatus('Failed to unlink evidence — please try again.', 'error');
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
    const lineState = hostAdapter.procedureModalState[uid];
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
    const lineState = hostAdapter.procedureModalState[uid];
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
    columns.forEach(col => { html += `<th>${hostAdapter.escapeHtml(col.column_name)}</th>`; });
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
                html += `<td>${hostAdapter.escapeHtml(value)}${icon}</td>`;
            } else {
                html += `<td>${hostAdapter.escapeHtml(value)}</td>`;
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
// showAuditGuidancePopover()/hostAdapter.updateProceduresCellForLine() — no new fetch for the role list
// itself). Tick state comes from tabState.checklist (seeded in openProcedureModal() from
// GET /api/verification/procedure_checks/{uid}) — a role with no entry renders unchecked. No
// validation behind ticking a role — rules are explicitly deferred to a future phase.
function buildProcedureChecklist(uid, docId) {
    const lineState = hostAdapter.procedureModalState[uid];
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
                <input type="checkbox" class="procedure-checklist-checkbox" data-role="${hostAdapter.escapeHtml(role)}" ${checked ? 'checked' : ''}>
                <span>${hostAdapter.escapeHtml(role)}</span>
                <span class="procedure-checklist-source">sourced from Audit Rules</span>
            </label>
        `;
    }).join('');

    return `<div class="procedure-checklist-list">${items}</div>`;
}

// Persists one checkbox toggle. Updates local state from the server's response on success (no
// re-render needed — the checkbox already reflects the click); on failure, reverts the in-memory
// state and re-renders so the checkbox visually snaps back to its prior value.
async function toggleProcedureCheck(uid, docId, roleName, isChecked, target = null) {
    const tabState = getProcedureTabState(uid, docId);
    const previousState = tabState.checklist[roleName] ? { ...tabState.checklist[roleName] } : null;
    if (!tabState.checklist[roleName]) tabState.checklist[roleName] = {};
    tabState.checklist[roleName].is_checked = isChecked;
    hostAdapter.markLineDirty(uid, 'procedure check changed');
    renderProcedureModalTabs(uid);
    return;

    try {
        const res = await window.ApiUtils.apiFetchMutate(`${API_BASE}/api/verification/procedure_checks`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                voucher_line_id: parseInt(uid),
                document_id: docId,
                role_name: roleName,
                is_checked: isChecked
            })
        }, { target, message: 'Saving' });
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
        hostAdapter.markLineDirty(uid, 'procedure check changed');
        invalidateProcedureModalData(uid);
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
        hostAdapter.showStatus('Failed to save procedure tick — please try again.', 'error');
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
        const res = await window.ApiUtils.apiFetchMutate(`${API_BASE}/api/verification/set_signature_exception`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ assignment_id: assignmentId, signature_exception: isChecked })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to save signature exception.');
        }
        hostAdapter.markLineDirty(uid, 'signature exception changed');
    } catch (e) {
        console.error('Failed to save signature exception:', e);
        if (sel) sel.signature_exception = previousValue;
        hostAdapter.showStatus('Failed to save signature exception — please try again.', 'error');
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
    const lineState = hostAdapter.procedureModalState[uid];
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
        hostAdapter.markLineDirty(uid, 'automatic observation suggested');
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
            placeholder="Add an observation for this document...">${hostAdapter.escapeHtml(tabState.observation || '')}</textarea>
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
// Observation field the next time that line is saved (hostAdapter.saveLineItems(), via
// sync_line_observations_to_voucher() in routers/verification.py), and also immediately here since
// save_line_observation() itself calls that same sync function.
async function saveLineObservation(uid, docId, target = null) {
    const tabState = getProcedureTabState(uid, docId);
    try {
        const res = await window.ApiUtils.apiFetchMutate(`${API_BASE}/api/verification/save_line_observation`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                voucher_line_id: parseInt(uid),
                document_id: docId,
                observation_text: tabState.observation || ''
            })
        }, { target, message: 'Saving' });
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
        // reflect that here too, in the cached line data (hostAdapter.currentVoucherLines, so a later full
        // re-render of this voucher stays correct) and the live Observations cell's flag+count
        // (a fresh recount, not a plain-text write — this cell shows count/tooltip only, per the
        // v7 redesign).
        const cachedLine = hostAdapter.currentVoucherLines.find(l => l.detail_id === parseInt(uid));
        if (cachedLine) cachedLine.observations = data.voucher_observations;
        const lineType = hostAdapter.voucherLineState[uid] && hostAdapter.voucherLineState[uid].transactionType;
        if (lineType) hostAdapter.updateObservationsCellForLine(uid, lineType);
    } catch (e) {
        console.error('Failed to save line observation:', e);
        hostAdapter.showStatus('Failed to save observation — please try again.', 'error');
    }
}

// Account Head Recommendation follow-up: persists a dirty no-match finding — called from the same
// save chokepoint as flushProcedureObservations() (hostAdapter.saveLineItems()). No-op
// unless the card is currently in no-match mode with unsaved text. Reuses the existing
// save_line_observation endpoint under the reserved ACCOUNT_HEAD_OBSERVATION_DOC_ID sentinel — the
// backend wraps the raw text into the "[Account Head Recommendation]: New account head
// recommended: ... — no match in Trial Balance" finding at rollup time
// (sync_line_observations_to_voucher()), so only the raw note is sent here.
async function flushAccountHeadNoMatchNote(uid) {
    // hostAdapter can be null here — this is called from verification.js's saveLineItems() on
    // EVERY line save, including a save that happens before the modal has ever been opened this
    // page-load. Provably a no-op-for-a-no-op: if hostAdapter was never set, the modal was never
    // opened for any line, so accountHeadCache[uid] would still be its untouched default (mode
    // 'match', noMatchDirty false) — this guard reaches the exact same early-return the next line
    // would have taken anyway, just without needing hostAdapter to check it.
    if (!hostAdapter) return;
    const state = getAccountHeadState(uid);
    if (state.mode !== 'nomatch' || !state.noMatchDirty) return;

    try {
        const res = await window.ApiUtils.apiFetchMutate(`${API_BASE}/api/verification/save_line_observation`, {
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
        const cachedLine = hostAdapter.currentVoucherLines.find(l => l.detail_id === parseInt(uid));
        if (cachedLine) cachedLine.observations = data.voucher_observations;
        const lineType = hostAdapter.voucherLineState[uid] && hostAdapter.voucherLineState[uid].transactionType;
        if (lineType) hostAdapter.updateObservationsCellForLine(uid, lineType);
    } catch (e) {
        console.error('Failed to save account head no-match note:', e);
        hostAdapter.showStatus('Failed to save account head finding — please try again.', 'error');
    }
}

// Persists every dirty (unsaved) observation tab for one voucher line — called from the
// Prev/Next/Complete/Exit save chokepoint (hostAdapter.saveLineItems()) and from closeProcedureModal(), so an
// observation typed without an explicit Save action is never lost. No-op if the modal was never
// opened for this uid or nothing changed since the last save.
async function flushProcedureObservations(uid) {
    // See the matching comment in flushAccountHeadNoMatchNote() above — same reasoning applies
    // here: hostAdapter null means the modal was never opened for any line this page-load, so
    // procedureModalState[uid] would be undefined anyway and the next line would already no-op.
    if (!hostAdapter) return;
    const lineState = hostAdapter.procedureModalState[uid];
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
    const lineState = hostAdapter.procedureModalState[uid];
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
    const lineState = hostAdapter.procedureModalState[uid];
    const doc = lineState && (lineState.documents || []).find(d => d.id === docId);
    if (!doc) return;
    if (procedureBrowseTableContainer) {
        procedureBrowseTableContainer.innerHTML = '<div style="text-align:center; padding:20px;"><span class="spinner"></span> Loading...</div>';
    }
    try {
        const data = await fetchProcedureRows(doc.template_id, doc.id, '', page, PROCEDURE_BROWSE_PAGE_SIZE);
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
    const lineState = hostAdapter.procedureModalState[uid];
    const activeDoc = lineState && (lineState.documents || []).find(d => d.id === activeDocId);
    const templateColumns = (activeDoc && templateColumnsCache[activeDoc.template_id]) || [];
    const columns = templateColumns.length
        ? templateColumns.map(c => c.column_name)
        : Object.keys(rows[0].row_data || {});

    let html = '<table class="procedure-browse-table"><thead><tr>';
    columns.forEach(col => { html += `<th>${hostAdapter.escapeHtml(col)}</th>`; });
    html += '<th>Status</th><th>Voucher No.</th>';
    html += '</tr></thead><tbody>';

    rows.forEach(row => {
        const isAssigned = !!row.is_assigned;
        const rowClass = isAssigned ? ' class="procedure-browse-row-assigned"' : '';
        const interactiveAttrs = isAssigned ? '' : ' role="button" tabindex="0"';
        html += `<tr data-id="${row.id}"${rowClass}${interactiveAttrs}>`;
        columns.forEach(col => {
            html += `<td>${hostAdapter.escapeHtml(formatProcedureCellValue(row.row_data ? row.row_data[col] : ''))}</td>`;
        });
        const statusBadgeClass = isAssigned ? 'assigned' : 'unassigned';
        const statusLabel = isAssigned ? 'Assigned' : 'Unassigned';
        html += `<td><span class="procedure-browse-status-badge ${statusBadgeClass}">${statusLabel}</span></td>`;
        let voucherCell = '';
        if (isAssigned) {
            voucherCell = hostAdapter.escapeHtml(formatAssignedVoucherLabel(row.assigned_to_voucher_id, row.assigned_to_line_srno));
            const isOtherDocSameLine = isSameUnderlyingLine(uid, row)
                && activeDocId != null && String(row.assigned_to_document_id) !== String(activeDocId);
            if (isOtherDocSameLine) {
                voucherCell += ` <span class="procedure-browse-other-doc-note">(under "${hostAdapter.escapeHtml(row.assigned_to_document_name || 'another document')}")</span>`;
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
                const success = await selectProcedureDocument(uid, docId, row, this);
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
// Confirmed fix: no outside-click-to-close — the overlay click listener that called
// closeProcedureModal() when e.target === procedureModal was removed. The × button above
// remains the only way to close this modal.
document.getElementById('procedureModalBackBtn')?.addEventListener('click', function() {
    navigateProcedureModalLine(-1);
});
document.getElementById('procedureModalVerifyOnlyBtn')?.addEventListener('click', function() {
    verifyAndStay();
});
document.getElementById('procedureModalVerifyBtn')?.addEventListener('click', function() {
    verifyAndAdvanceVoucher();
});
