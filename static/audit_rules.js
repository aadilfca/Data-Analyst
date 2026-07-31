// ==============================================
// 📋 AUDIT RULES — DOCUMENTS SHELL LOGIC (Phase 5 of 9)
// ==============================================
// Three-pane shell: rail (entity type) | list (Documents) | detail (edit form).
// Only the Documents entity type is wired this phase — Cross-checks/Categories rail
// items are visible but inert (see audit_rules.html), to be wired in Phases 6-7 reusing
// this same shell/pattern.
//
// API_BASE is defined globally in client_context.js.

// ---------- STATE ----------
// null while creating a new Document; set to the Document's id while editing an
// existing one (mirrors columns.js's editingId convention).
let editingId = null;

// Two independent flat string arrays edited by the chip editors below, sent as
// auth_roles/cross_check_fields on save. Reset on both selectDocument() and
// startNewDocument().
let rolesState = [];
let fieldsState = [];

// ---------- DOM REFS ----------
const arListBody = document.getElementById('arListBody');
const arListEmptyMessage = document.getElementById('arListEmptyMessage');
const arShowInactiveCheckbox = document.getElementById('arShowInactiveCheckbox');
const arAddNewBtn = document.getElementById('arAddNewBtn');

const arDetailEmpty = document.getElementById('arDetailEmpty');
const arDetailForm = document.getElementById('arDetailForm');
const arEditingIdInput = document.getElementById('arEditingId');
const arNameInput = document.getElementById('arNameInput');
const arKbNoteInput = document.getElementById('arKbNoteInput');
const arTemplateSelect = document.getElementById('arTemplateSelect');
const arRiskRatingSelect = document.getElementById('arRiskRatingSelect');
const arRolesChipList = document.getElementById('arRolesChipList');
const arRolesInput = document.getElementById('arRolesInput');
const arRolesAddBtn = document.getElementById('arRolesAddBtn');
const arFieldsChipList = document.getElementById('arFieldsChipList');
const arFieldsInput = document.getElementById('arFieldsInput');
const arFieldsAddBtn = document.getElementById('arFieldsAddBtn');
const arUsedByRow = document.getElementById('arUsedByRow');
const arUsedByCount = document.getElementById('arUsedByCount');
const arDetailStatus = document.getElementById('arDetailStatus');
const arSaveBtn = document.getElementById('arSaveBtn');
const arDeleteBtn = document.getElementById('arDeleteBtn');
const arRestoreBtn = document.getElementById('arRestoreBtn');

// ---------- STATUS MESSAGES ----------
// Detail-panel-scoped status (save/delete/restore results) — same timeout-based
// show/hide pattern as columns.js's showModalStatus().
function showDetailStatus(message, type) {
    arDetailStatus.textContent = message;
    arDetailStatus.className = 'upload-status ' + type;
    arDetailStatus.style.display = 'block';
    clearTimeout(window.arDetailStatusTimeout);
    window.arDetailStatusTimeout = setTimeout(() => {
        arDetailStatus.style.display = 'none';
    }, 5000);
}

// ---------- LOAD & RENDER LIST ----------
async function loadDocuments() {
    const includeInactive = arShowInactiveCheckbox.checked;
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/documents?include_inactive=' + includeInactive);
        if (!res.ok) throw new Error('Failed to load documents');
        const documents = await res.json();
        renderDocumentList(documents);
    } catch (error) {
        arListBody.innerHTML = '';
        arListEmptyMessage.textContent = '❌ Error loading documents: ' + error.message;
        arListEmptyMessage.style.display = 'block';
    }
}

function renderDocumentList(documents) {
    arListBody.innerHTML = '';

    if (documents.length === 0) {
        arListEmptyMessage.textContent = 'No documents found.';
        arListEmptyMessage.style.display = 'block';
        return;
    }
    arListEmptyMessage.style.display = 'none';

    documents.forEach(doc => {
        const row = document.createElement('div');
        row.className = 'ar-list-row' + (doc.is_active ? '' : ' ar-list-row-inactive');
        if (doc.id === editingId) row.classList.add('ar-list-row-selected');

        const nameSpan = document.createElement('span');
        nameSpan.className = 'ar-list-row-name';
        nameSpan.textContent = doc.name;
        row.appendChild(nameSpan);

        if (!doc.is_active) {
            const badge = document.createElement('span');
            badge.className = 'ar-list-row-badge';
            badge.textContent = 'Inactive';
            row.appendChild(badge);
        }

        row.addEventListener('click', () => selectDocument(doc.id));
        arListBody.appendChild(row);
    });
}

// ---------- CHIP EDITORS (Authorisation Roles / Cross-check-eligible Fields) ----------
// Same add/remove/render shape as client_settings.js's addPrefix/removePrefix/
// renderPrefixList, adapted to two independent flat arrays (no cross-list dedup
// validation needed — roles and fields are unrelated concepts here).
function renderChipList(container, items, onRemove) {
    container.innerHTML = '';
    if (items.length === 0) {
        const empty = document.createElement('span');
        empty.className = 'ar-chip-empty';
        empty.textContent = 'None added.';
        container.appendChild(empty);
        return;
    }
    items.forEach(item => {
        const chip = document.createElement('span');
        chip.className = 'ar-chip';

        const label = document.createElement('span');
        label.textContent = item;
        chip.appendChild(label);

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'ar-chip-remove';
        removeBtn.textContent = '✕';
        removeBtn.title = 'Remove';
        removeBtn.addEventListener('click', () => onRemove(item));
        chip.appendChild(removeBtn);

        container.appendChild(chip);
    });
}

function renderRoleChips() {
    renderChipList(arRolesChipList, rolesState, removeRole);
}
function renderFieldChips() {
    renderChipList(arFieldsChipList, fieldsState, removeField);
}

function addRole() {
    const value = arRolesInput.value.trim();
    if (!value) return;
    if (rolesState.some(r => r.toLowerCase() === value.toLowerCase())) {
        arRolesInput.value = '';
        return;
    }
    rolesState.push(value);
    arRolesInput.value = '';
    renderRoleChips();
}
function removeRole(value) {
    rolesState = rolesState.filter(r => r !== value);
    renderRoleChips();
}

function addField() {
    const value = arFieldsInput.value.trim();
    if (!value) return;
    if (fieldsState.some(f => f.toLowerCase() === value.toLowerCase())) {
        arFieldsInput.value = '';
        return;
    }
    fieldsState.push(value);
    arFieldsInput.value = '';
    renderFieldChips();
}
function removeField(value) {
    fieldsState = fieldsState.filter(f => f !== value);
    renderFieldChips();
}

// ---------- TEMPLATES (Phase 9: Document -> Data Bank template link) ----------
// Populated once at init (see INIT below), mirrors templates.js's own GET /api/templates
// fetch. Only active templates are offered, matching how the rest of the app treats
// is_active=0 templates as retired.
async function loadTemplatesForDocuments() {
    try {
        const res = await fetch(API_BASE + '/api/templates');
        if (!res.ok) throw new Error('Failed to fetch templates');
        const templates = await res.json();
        arTemplateSelect.innerHTML = '<option value="">-- None --</option>' +
            templates.filter(t => t.is_active).map(t => `<option value="${t.id}">${t.template_name}</option>`).join('');
    } catch (error) {
        // Non-fatal: the Template field just stays at its "-- None --" default if this fails.
        console.error('Failed to load templates for Documents:', error);
    }
}

// ---------- TRANSACTION LEVEL APPROVAL (new, confirmed requirement) ----------
// UI checkbox intentionally hidden (see CLAUDE.md Conventions) — this function is kept so
// reactivating the feature later is a pure markup+wiring change, no backend/schema work needed.
async function toggleDocumentTransactionLevelApproval(checked) {
    if (!editingId) return;

    try {
        const res = await fetch(
            API_BASE + '/api/audit-rules/documents/' + editingId + '/transaction-level-approval',
            {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ transaction_level_approval: checked })
            }
        );
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to update transaction level approval');

        showDetailStatus('✅ Transaction level approval updated.', 'success');
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

// ---------- SELECT / NEW / RESET DETAIL PANEL ----------
async function selectDocument(id) {
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/documents/' + id);
        if (!res.ok) throw new Error('Document not found');
        const doc = await res.json();

        editingId = doc.id;
        arEditingIdInput.value = doc.id;
        arNameInput.value = doc.name;
        arKbNoteInput.value = doc.kb_note || '';
        arTemplateSelect.value = doc.template_id || '';
        arRiskRatingSelect.value = doc.risk_rating || '';
        rolesState = (doc.auth_roles || []).slice();
        fieldsState = (doc.cross_check_fields || []).slice();
        renderRoleChips();
        renderFieldChips();

        arUsedByRow.style.display = 'block';
        arUsedByCount.textContent = doc.used_by_categories_count;

        if (doc.is_active) {
            arDeleteBtn.style.display = 'inline-block';
            arRestoreBtn.style.display = 'none';
        } else {
            arDeleteBtn.style.display = 'none';
            arRestoreBtn.style.display = 'inline-block';
        }

        arDetailEmpty.style.display = 'none';
        arDetailForm.style.display = 'block';

        // Re-render the list so the selected row highlight moves without a full re-fetch.
        loadDocuments();
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

function startNewDocument() {
    editingId = null;
    arEditingIdInput.value = '';
    arNameInput.value = '';
    arKbNoteInput.value = '';
    arTemplateSelect.value = '';
    arRiskRatingSelect.value = '';
    rolesState = [];
    fieldsState = [];
    renderRoleChips();
    renderFieldChips();

    arUsedByRow.style.display = 'none';
    arDeleteBtn.style.display = 'none';
    arRestoreBtn.style.display = 'none';

    arDetailEmpty.style.display = 'none';
    arDetailForm.style.display = 'block';
    arNameInput.focus();
}

// ---------- SAVE ----------
async function saveDocument() {
    const name = arNameInput.value.trim();
    if (!name) {
        showDetailStatus('❌ Name is required.', 'error');
        return;
    }

    const payload = {
        name: name,
        kb_note: arKbNoteInput.value.trim() || null,
        template_id: arTemplateSelect.value ? parseInt(arTemplateSelect.value) : null,
        risk_rating: arRiskRatingSelect.value || null,
        auth_roles: rolesState,
        cross_check_fields: fieldsState
    };

    try {
        let url = API_BASE + '/api/audit-rules/documents';
        let method = 'POST';
        if (editingId) {
            url += '/' + editingId;
            method = 'PUT';
        }

        const res = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to save document');

        showDetailStatus('✅ ' + result.message, 'success');

        // POST doesn't echo back the full row, so re-fetch the list, then re-select
        // (create: the new id; update: the same id) to refresh the detail panel's
        // is_active/used_by state from the server.
        const savedId = editingId || result.document_id;
        await loadDocuments();
        await selectDocument(savedId);
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

// ---------- DELETE / RESTORE ----------
async function deleteDocument() {
    if (!editingId) return;
    if (!confirm('Are you sure you want to delete "' + arNameInput.value + '"? This will soft-delete it.')) return;

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/documents/' + editingId, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to delete document');

        showDetailStatus('✅ ' + result.message, 'success');
        await loadDocuments();
        await selectDocument(editingId);
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

async function restoreDocument() {
    if (!editingId) return;

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/documents/' + editingId + '/restore', { method: 'POST' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to restore document');

        showDetailStatus('✅ ' + result.message, 'success');
        await loadDocuments();
        await selectDocument(editingId);
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

// ---------- WIRE UP EVENT LISTENERS ----------
arAddNewBtn.addEventListener('click', startNewDocument);
arShowInactiveCheckbox.addEventListener('change', loadDocuments);

arRolesAddBtn.addEventListener('click', addRole);
arRolesInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); addRole(); }
});

arFieldsAddBtn.addEventListener('click', addField);
arFieldsInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); addField(); }
});

arSaveBtn.addEventListener('click', saveDocument);
arDeleteBtn.addEventListener('click', deleteDocument);
arRestoreBtn.addEventListener('click', restoreDocument);

// Categories rail item is still an inert placeholder this phase (no listener), see
// audit_rules.html — wired up in Phase 7.

// ==============================================
// 🔗 AUDIT RULES — CROSS-CHECKS SHELL LOGIC (Phase 6 of 9)
// ==============================================
// Same rail → list → detail pattern as Documents above, but with its own fully separate
// markup/state/functions (see the Phase 6 Conventions note in CLAUDE.md for why: keeps
// this addition zero-risk to the existing Documents code, at the cost of some
// duplication — the same tradeoff Phase 5 already made when copying patterns from
// columns.js/client_settings.js rather than importing them).

// ---------- RAIL / VIEW SWITCHING ----------
// 'documents', 'cross-checks', 'categories', or 'signature-specimens' — which entity
// type's list/detail sections are visible.
let currentEntityType = 'documents';

const arRailDocuments = document.getElementById('arRailDocuments');
const arRailCrossChecks = document.getElementById('arRailCrossChecks');
const arRailCategories = document.getElementById('arRailCategories');
const arRailSignatureSpecimens = document.getElementById('arRailSignatureSpecimens');
const arDocumentsListSection = document.getElementById('arDocumentsListSection');
const arDocumentsDetailSection = document.getElementById('arDocumentsDetailSection');
const arCrossChecksListSection = document.getElementById('arCrossChecksListSection');
const arCrossChecksDetailSection = document.getElementById('arCrossChecksDetailSection');
const arCategoriesListSection = document.getElementById('arCategoriesListSection');
const arCategoriesDetailSection = document.getElementById('arCategoriesDetailSection');
const arSignatureSpecimensListSection = document.getElementById('arSignatureSpecimensListSection');
const arSignatureSpecimensDetailSection = document.getElementById('arSignatureSpecimensDetailSection');
const arRailVoucherChecks = document.getElementById('arRailVoucherChecks');
const arVoucherChecksListSection = document.getElementById('arVoucherChecksListSection');
const arVoucherChecksDetailSection = document.getElementById('arVoucherChecksDetailSection');

function switchEntityType(type) {
    currentEntityType = type;

    arRailDocuments.classList.toggle('active', type === 'documents');
    arRailCrossChecks.classList.toggle('active', type === 'cross-checks');
    arRailCategories.classList.toggle('active', type === 'categories');
    arRailSignatureSpecimens.classList.toggle('active', type === 'signature-specimens');
    arRailVoucherChecks.classList.toggle('active', type === 'voucher-checks');

    arDocumentsListSection.style.display = (type === 'documents') ? 'flex' : 'none';
    arDocumentsDetailSection.style.display = (type === 'documents') ? 'block' : 'none';
    arCrossChecksListSection.style.display = (type === 'cross-checks') ? 'flex' : 'none';
    arCrossChecksDetailSection.style.display = (type === 'cross-checks') ? 'block' : 'none';
    arCategoriesListSection.style.display = (type === 'categories') ? 'flex' : 'none';
    arCategoriesDetailSection.style.display = (type === 'categories') ? 'block' : 'none';
    arSignatureSpecimensListSection.style.display = (type === 'signature-specimens') ? 'flex' : 'none';
    arSignatureSpecimensDetailSection.style.display = (type === 'signature-specimens') ? 'block' : 'none';
    arVoucherChecksListSection.style.display = (type === 'voucher-checks') ? 'flex' : 'none';
    arVoucherChecksDetailSection.style.display = (type === 'voucher-checks') ? 'block' : 'none';

    if (type === 'cross-checks') {
        // Refresh the Documents cache every time this view is opened, so the "add
        // document" picker and field-eligibility logic reflect any Documents-side
        // edits made since the last visit.
        loadAllDocumentsForCrossChecks().then(loadCrossChecks);
    } else if (type === 'categories') {
        // Refresh both caches every time this view is opened, so the Documents/
        // Cross-checks pickers and the eligibility check reflect the latest state.
        Promise.all([loadAllDocumentsForCrossChecks(), loadAllCrossChecksForCategories()]).then(loadCategories);
    } else if (type === 'signature-specimens') {
        // Refresh the Documents cache (Document picker + per-document auth_roles for the
        // Role picker) every time this view is opened, same convention as above.
        loadAllDocumentsForCrossChecks().then(() => {
            populateSpecimenDocumentSelect();
            loadSignatureSpecimens();
        });
    } else if (type === 'voucher-checks') {
        // No cross-entity cache to refresh -- Voucher Checks has no picker dependent
        // on Documents/Cross-checks, unlike the three branches above.
        loadVoucherChecks();
    }
}

// ---------- DOM REFS ----------
const arCrossCheckListBody = document.getElementById('arCrossCheckListBody');
const arCrossCheckListEmptyMessage = document.getElementById('arCrossCheckListEmptyMessage');
const arCrossCheckShowInactiveCheckbox = document.getElementById('arCrossCheckShowInactiveCheckbox');
const arCrossCheckAddNewBtn = document.getElementById('arCrossCheckAddNewBtn');

const arCrossCheckDetailEmpty = document.getElementById('arCrossCheckDetailEmpty');
const arCrossCheckDetailForm = document.getElementById('arCrossCheckDetailForm');
const arCrossCheckEditingIdInput = document.getElementById('arCrossCheckEditingId');
const arCrossCheckNameInput = document.getElementById('arCrossCheckNameInput');
const arCrossCheckDocsList = document.getElementById('arCrossCheckDocsList');
const arAddDocumentSelect = document.getElementById('arAddDocumentSelect');
const arAddDocumentBtn = document.getElementById('arAddDocumentBtn');
const arCompareFieldSelect = document.getElementById('arCompareFieldSelect');
const arCompareFieldStaleWarning = document.getElementById('arCompareFieldStaleWarning');
const arComparisonTypeSelect = document.getElementById('arComparisonTypeSelect');
const arCompareToVoucherLineCheckbox = document.getElementById('arCompareToVoucherLineCheckbox');
const arCompareToVoucherLineHint = document.getElementById('arCompareToVoucherLineHint');
const arCrossCheckUsedByRow = document.getElementById('arCrossCheckUsedByRow');
const arCrossCheckUsedByCount = document.getElementById('arCrossCheckUsedByCount');
const arCrossCheckDetailStatus = document.getElementById('arCrossCheckDetailStatus');
const arCrossCheckSaveBtn = document.getElementById('arCrossCheckSaveBtn');
const arCrossCheckDeleteBtn = document.getElementById('arCrossCheckDeleteBtn');
const arCrossCheckRestoreBtn = document.getElementById('arCrossCheckRestoreBtn');

// ---------- STATE ----------
// null while creating a new Cross-check; set to its id while editing an existing one.
let editingCrossCheckId = null;
// Ordered array of Document ids currently attached to the cross-check being edited —
// drives both the "Documents" list UI and the Compare Field dropdown's eligible options.
let crossCheckDocIds = [];
// Cache of all active Documents ({id, name, cross_check_fields}), used to resolve names
// for crossCheckDocIds, populate the "add document" picker, and compute field
// eligibility. Refreshed each time the Cross-checks view is opened (switchEntityType()).
let allDocuments = [];

// ---------- STATUS MESSAGES ----------
// Standalone duplicate of showDetailStatus() above, scoped to the Cross-checks detail
// panel's own status div — kept separate rather than generalizing the Documents helper,
// so this addition never has to touch that function's existing call sites.
function showCrossCheckDetailStatus(message, type) {
    arCrossCheckDetailStatus.textContent = message;
    arCrossCheckDetailStatus.className = 'upload-status ' + type;
    arCrossCheckDetailStatus.style.display = 'block';
    clearTimeout(window.arCrossCheckDetailStatusTimeout);
    window.arCrossCheckDetailStatusTimeout = setTimeout(() => {
        arCrossCheckDetailStatus.style.display = 'none';
    }, 5000);
}

// ---------- DOCUMENT CACHE (for the add-document picker + field eligibility) ----------
async function loadAllDocumentsForCrossChecks() {
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/documents?include_inactive=false');
        if (!res.ok) throw new Error('Failed to load documents');
        allDocuments = await res.json();
    } catch (error) {
        allDocuments = [];
        showCrossCheckDetailStatus('❌ Error loading documents: ' + error.message, 'error');
    }
}

function getDocumentById(id) {
    return allDocuments.find(d => d.id === id);
}

// ---------- LOAD & RENDER LIST ----------
async function loadCrossChecks() {
    const includeInactive = arCrossCheckShowInactiveCheckbox.checked;
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/cross-checks?include_inactive=' + includeInactive);
        if (!res.ok) throw new Error('Failed to load cross-checks');
        const crossChecks = await res.json();
        renderCrossCheckList(crossChecks);
    } catch (error) {
        arCrossCheckListBody.innerHTML = '';
        arCrossCheckListEmptyMessage.textContent = '❌ Error loading cross-checks: ' + error.message;
        arCrossCheckListEmptyMessage.style.display = 'block';
    }
}

function renderCrossCheckList(crossChecks) {
    arCrossCheckListBody.innerHTML = '';

    if (crossChecks.length === 0) {
        arCrossCheckListEmptyMessage.textContent = 'No cross-checks found.';
        arCrossCheckListEmptyMessage.style.display = 'block';
        return;
    }
    arCrossCheckListEmptyMessage.style.display = 'none';

    crossChecks.forEach(cc => {
        const row = document.createElement('div');
        row.className = 'ar-list-row' + (cc.is_active ? '' : ' ar-list-row-inactive');
        if (cc.id === editingCrossCheckId) row.classList.add('ar-list-row-selected');

        const nameSpan = document.createElement('span');
        nameSpan.className = 'ar-list-row-name';
        nameSpan.textContent = cc.name;
        row.appendChild(nameSpan);

        if (!cc.is_active) {
            const badge = document.createElement('span');
            badge.className = 'ar-list-row-badge';
            badge.textContent = 'Inactive';
            row.appendChild(badge);
        }

        row.addEventListener('click', () => selectCrossCheck(cc.id));
        arCrossCheckListBody.appendChild(row);
    });
}

// ---------- ORDERED DOCUMENTS LIST (add / remove / reorder) ----------
function renderCrossCheckDocsList() {
    arCrossCheckDocsList.innerHTML = '';

    if (crossCheckDocIds.length === 0) {
        const empty = document.createElement('span');
        empty.className = 'ar-chip-empty';
        empty.textContent = 'No documents added.';
        arCrossCheckDocsList.appendChild(empty);
    } else {
        crossCheckDocIds.forEach((docId, index) => {
            const doc = getDocumentById(docId);
            const row = document.createElement('div');
            row.className = 'ar-ordered-doc-row';

            const nameSpan = document.createElement('span');
            nameSpan.textContent = doc ? doc.name : ('Document #' + docId);
            row.appendChild(nameSpan);

            const controls = document.createElement('span');

            const upBtn = document.createElement('button');
            upBtn.type = 'button';
            upBtn.className = 'ar-move-btn';
            upBtn.textContent = '▲';
            upBtn.title = 'Move up';
            upBtn.disabled = (index === 0);
            upBtn.addEventListener('click', () => moveDocUp(index));
            controls.appendChild(upBtn);

            const downBtn = document.createElement('button');
            downBtn.type = 'button';
            downBtn.className = 'ar-move-btn';
            downBtn.textContent = '▼';
            downBtn.title = 'Move down';
            downBtn.disabled = (index === crossCheckDocIds.length - 1);
            downBtn.addEventListener('click', () => moveDocDown(index));
            controls.appendChild(downBtn);

            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'ar-remove-doc-btn';
            removeBtn.textContent = '✕';
            removeBtn.title = 'Remove';
            removeBtn.addEventListener('click', () => removeDocumentFromCrossCheck(docId));
            controls.appendChild(removeBtn);

            row.appendChild(controls);
            arCrossCheckDocsList.appendChild(row);
        });
    }

    // Rebuild the "add document" picker to exclude documents already in the list.
    arAddDocumentSelect.innerHTML = '';
    const available = allDocuments.filter(d => !crossCheckDocIds.includes(d.id));
    if (available.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.disabled = true;
        opt.selected = true;
        opt.textContent = allDocuments.length === 0 ? 'No documents exist yet' : 'All documents added';
        arAddDocumentSelect.appendChild(opt);
        arAddDocumentBtn.disabled = true;
    } else {
        available.forEach(d => {
            const opt = document.createElement('option');
            opt.value = d.id;
            opt.textContent = d.name;
            arAddDocumentSelect.appendChild(opt);
        });
        arAddDocumentBtn.disabled = false;
    }
}

function addDocumentToCrossCheck() {
    const value = arAddDocumentSelect.value;
    if (!value) return;
    crossCheckDocIds.push(parseInt(value, 10));
    renderCrossCheckDocsList();
    renderCompareFieldDropdown();
    updateCompareToVoucherLineHint();
}

function removeDocumentFromCrossCheck(docId) {
    crossCheckDocIds = crossCheckDocIds.filter(id => id !== docId);
    renderCrossCheckDocsList();
    renderCompareFieldDropdown();
    updateCompareToVoucherLineHint();
}

function moveDocUp(index) {
    if (index <= 0) return;
    [crossCheckDocIds[index - 1], crossCheckDocIds[index]] = [crossCheckDocIds[index], crossCheckDocIds[index - 1]];
    renderCrossCheckDocsList();
    renderCompareFieldDropdown();
    updateCompareToVoucherLineHint();
}

function moveDocDown(index) {
    if (index >= crossCheckDocIds.length - 1) return;
    [crossCheckDocIds[index + 1], crossCheckDocIds[index]] = [crossCheckDocIds[index], crossCheckDocIds[index + 1]];
    renderCrossCheckDocsList();
    renderCompareFieldDropdown();
    updateCompareToVoucherLineHint();
}

// ---------- COMPARE FIELD DROPDOWN (dynamic, driven by crossCheckDocIds) ----------
// Eligible fields are the intersection of every selected document's own
// cross_check_fields — mirrors validate_cross_check_payload()'s server-side rule
// (routers/audit_rules.py) so the picker never offers a field the server would reject.
function computeEligibleFields() {
    if (crossCheckDocIds.length === 0) return [];
    let eligible = null;
    for (const docId of crossCheckDocIds) {
        const doc = getDocumentById(docId);
        const fields = doc ? (doc.cross_check_fields || []) : [];
        eligible = (eligible === null) ? fields.slice() : eligible.filter(f => fields.includes(f));
    }
    return eligible || [];
}

function hideCompareFieldStaleWarning() {
    arCompareFieldStaleWarning.style.display = 'none';
    arCompareFieldStaleWarning.textContent = '';
}

function showCompareFieldStaleWarning(value) {
    arCompareFieldStaleWarning.textContent =
        '⚠️ "' + value + '" is not available on every selected document — pick a different field, or add/remove documents until it matches.';
    arCompareFieldStaleWarning.style.display = 'block';
}

// preserveValue: the value to try to keep selected across a rebuild (e.g. a document
// add/remove, or the saved compare_field when a Cross-check is first opened). Defaults to
// the select's own current value, so in-session add/remove-document calls need no change.
// A preserveValue that isn't in the eligible set is never silently dropped — it's kept
// visible as a real (selected) option so an existing saved value is never hidden, per the
// confirmed requirement, with a persistent warning (not the auto-dismissing status toast).
function renderCompareFieldDropdown(preserveValue) {
    const eligibleFields = computeEligibleFields();
    const previousValue = (preserveValue !== undefined) ? preserveValue : arCompareFieldSelect.value;

    arCompareFieldSelect.innerHTML = '';

    if (eligibleFields.length === 0 && !previousValue) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.disabled = true;
        opt.selected = true;
        opt.textContent = (crossCheckDocIds.length === 0)
            ? 'Add a document first'
            : 'No field is common to all selected documents';
        arCompareFieldSelect.appendChild(opt);
        hideCompareFieldStaleWarning();
        return;
    }

    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.disabled = true;
    placeholder.textContent = 'Select a field';
    arCompareFieldSelect.appendChild(placeholder);

    eligibleFields.forEach(field => {
        const opt = document.createElement('option');
        opt.value = field;
        opt.textContent = field;
        arCompareFieldSelect.appendChild(opt);
    });

    if (eligibleFields.includes(previousValue)) {
        arCompareFieldSelect.value = previousValue;
        hideCompareFieldStaleWarning();
    } else if (previousValue) {
        // Saved/previously-chosen value doesn't match any current option — keep it visible
        // (as its own selected option) rather than blanking the dropdown, and show a
        // persistent warning explaining why.
        const staleOpt = document.createElement('option');
        staleOpt.value = previousValue;
        staleOpt.textContent = '⚠️ ' + previousValue + ' (not on every selected document)';
        staleOpt.selected = true;
        arCompareFieldSelect.appendChild(staleOpt);
        showCompareFieldStaleWarning(previousValue);
    } else {
        placeholder.selected = true;
        hideCompareFieldStaleWarning();
    }
}

// ---------- COMPARE TO VOUCHER LINE (Phase 5 of 10) ----------
// The anchor document is never picked in this UI - it's always the last document in
// crossCheckDocIds (the chain's own order), resolved live at evaluation time (Phase 6). This
// just surfaces today's anchor + field as a live-updating explanatory hint so the admin can
// see what "compare to voucher line" will actually mean once ticked, without configuring it.
function updateCompareToVoucherLineHint() {
    if (!arCompareToVoucherLineCheckbox.checked) {
        arCompareToVoucherLineHint.style.display = 'none';
        return;
    }

    if (crossCheckDocIds.length === 0) {
        arCompareToVoucherLineHint.textContent = 'Add at least one document above to set an anchor.';
        arCompareToVoucherLineHint.style.display = 'block';
        return;
    }

    const anchorDocId = crossCheckDocIds[crossCheckDocIds.length - 1];
    const anchorDoc = getDocumentById(anchorDocId);
    const anchorName = anchorDoc ? anchorDoc.name : ('Document #' + anchorDocId);
    const field = arCompareFieldSelect.value || '(field not yet selected)';
    arCompareToVoucherLineHint.textContent =
        'ℹ️ Will compare "' + anchorName + '"\'s ' + field + ' vs. this voucher line\'s amount. ' +
        '(Anchor = last document above; resolved live, not fixed here.)';
    arCompareToVoucherLineHint.style.display = 'block';
}

// ---------- SELECT / NEW / RESET DETAIL PANEL ----------
async function selectCrossCheck(id) {
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/cross-checks/' + id);
        if (!res.ok) throw new Error('Cross-check not found');
        const cc = await res.json();

        editingCrossCheckId = cc.id;
        arCrossCheckEditingIdInput.value = cc.id;
        arCrossCheckNameInput.value = cc.name;
        crossCheckDocIds = (cc.documents || []).map(d => d.document_id);
        renderCrossCheckDocsList();
        renderCompareFieldDropdown(cc.compare_field);
        arComparisonTypeSelect.value = cc.comparison_type;
        arCompareToVoucherLineCheckbox.checked = !!cc.compare_to_voucher_line;
        updateCompareToVoucherLineHint();

        arCrossCheckUsedByRow.style.display = 'block';
        arCrossCheckUsedByCount.textContent = cc.used_by_categories_count;

        if (cc.is_active) {
            arCrossCheckDeleteBtn.style.display = 'inline-block';
            arCrossCheckRestoreBtn.style.display = 'none';
        } else {
            arCrossCheckDeleteBtn.style.display = 'none';
            arCrossCheckRestoreBtn.style.display = 'inline-block';
        }

        arCrossCheckDetailEmpty.style.display = 'none';
        arCrossCheckDetailForm.style.display = 'block';

        // Re-render the list so the selected row highlight moves without a full re-fetch.
        loadCrossChecks();
    } catch (error) {
        showCrossCheckDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

function startNewCrossCheck() {
    editingCrossCheckId = null;
    arCrossCheckEditingIdInput.value = '';
    arCrossCheckNameInput.value = '';
    crossCheckDocIds = [];
    renderCrossCheckDocsList();
    renderCompareFieldDropdown('');
    arComparisonTypeSelect.value = '';
    arCompareToVoucherLineCheckbox.checked = false;
    updateCompareToVoucherLineHint();

    arCrossCheckUsedByRow.style.display = 'none';
    arCrossCheckDeleteBtn.style.display = 'none';
    arCrossCheckRestoreBtn.style.display = 'none';

    arCrossCheckDetailEmpty.style.display = 'none';
    arCrossCheckDetailForm.style.display = 'block';
    arCrossCheckNameInput.focus();
}

// ---------- SAVE ----------
async function saveCrossCheck() {
    const name = arCrossCheckNameInput.value.trim();
    const compareField = arCompareFieldSelect.value;
    const comparisonType = arComparisonTypeSelect.value;

    if (!name) {
        showCrossCheckDetailStatus('❌ Name is required.', 'error');
        return;
    }
    if (!compareField) {
        showCrossCheckDetailStatus('❌ A field to compare is required.', 'error');
        return;
    }
    if (!comparisonType) {
        showCrossCheckDetailStatus('❌ A comparison type is required.', 'error');
        return;
    }

    const payload = {
        name: name,
        compare_field: compareField,
        comparison_type: comparisonType,
        document_ids: crossCheckDocIds,
        compare_to_voucher_line: arCompareToVoucherLineCheckbox.checked
    };

    try {
        let url = API_BASE + '/api/audit-rules/cross-checks';
        let method = 'POST';
        if (editingCrossCheckId) {
            url += '/' + editingCrossCheckId;
            method = 'PUT';
        }

        const res = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to save cross-check');

        showCrossCheckDetailStatus('✅ ' + result.message, 'success');

        // POST doesn't echo back the full row, so re-fetch the list, then re-select
        // to refresh the detail panel's is_active/used_by state from the server.
        const savedId = editingCrossCheckId || result.cross_check_id;
        await loadCrossChecks();
        await selectCrossCheck(savedId);
    } catch (error) {
        showCrossCheckDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

// ---------- DELETE / RESTORE ----------
async function deleteCrossCheck() {
    if (!editingCrossCheckId) return;
    if (!confirm('Are you sure you want to delete "' + arCrossCheckNameInput.value + '"? This will soft-delete it.')) return;

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/cross-checks/' + editingCrossCheckId, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to delete cross-check');

        showCrossCheckDetailStatus('✅ ' + result.message, 'success');
        await loadCrossChecks();
        await selectCrossCheck(editingCrossCheckId);
    } catch (error) {
        showCrossCheckDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

async function restoreCrossCheck() {
    if (!editingCrossCheckId) return;

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/cross-checks/' + editingCrossCheckId + '/restore', { method: 'POST' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to restore cross-check');

        showCrossCheckDetailStatus('✅ ' + result.message, 'success');
        await loadCrossChecks();
        await selectCrossCheck(editingCrossCheckId);
    } catch (error) {
        showCrossCheckDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

// ---------- WIRE UP EVENT LISTENERS ----------
arRailDocuments.addEventListener('click', () => switchEntityType('documents'));
arRailCrossChecks.addEventListener('click', () => switchEntityType('cross-checks'));
arRailCategories.addEventListener('click', () => switchEntityType('categories'));
arRailSignatureSpecimens.addEventListener('click', () => switchEntityType('signature-specimens'));
arRailVoucherChecks.addEventListener('click', () => switchEntityType('voucher-checks'));

arCrossCheckAddNewBtn.addEventListener('click', startNewCrossCheck);
arCrossCheckShowInactiveCheckbox.addEventListener('change', loadCrossChecks);

arAddDocumentBtn.addEventListener('click', addDocumentToCrossCheck);
arCompareFieldSelect.addEventListener('change', updateCompareToVoucherLineHint);
arCompareToVoucherLineCheckbox.addEventListener('change', updateCompareToVoucherLineHint);

arCrossCheckSaveBtn.addEventListener('click', saveCrossCheck);
arCrossCheckDeleteBtn.addEventListener('click', deleteCrossCheck);
arCrossCheckRestoreBtn.addEventListener('click', restoreCrossCheck);

// ==============================================
// 🗂️ AUDIT RULES — CATEGORIES SHELL LOGIC (Phase 7 of 9, final entity view)
// ==============================================
// Same rail → list → detail pattern as Documents/Cross-checks above, with its own fully
// separate markup/state/functions (same "full duplication per entity type" convention
// documented in CLAUDE.md's Phase 6 note — this phase follows it, not generalizes it).
// Documents/Cross-checks tag lists here are flat (add/remove only, no reorder), unlike
// Cross-checks' own *ordered* document list — a Category's document/cross-check order has
// no meaning, per the confirmed brief ("tag-style list").

// ---------- DOM REFS ----------
const arCategoryListBody = document.getElementById('arCategoryListBody');
const arCategoryListEmptyMessage = document.getElementById('arCategoryListEmptyMessage');
const arCategoryShowInactiveCheckbox = document.getElementById('arCategoryShowInactiveCheckbox');
const arCategoryAddNewBtn = document.getElementById('arCategoryAddNewBtn');

const arCategoryDetailEmpty = document.getElementById('arCategoryDetailEmpty');
const arCategoryDetailForm = document.getElementById('arCategoryDetailForm');
const arCategoryEditingIdInput = document.getElementById('arCategoryEditingId');
const arCategoryNameInput = document.getElementById('arCategoryNameInput');
const arCategoryKbOverviewInput = document.getElementById('arCategoryKbOverviewInput');
const arCategoryDocsChipList = document.getElementById('arCategoryDocsChipList');
const arCategoryAddDocumentSelect = document.getElementById('arCategoryAddDocumentSelect');
const arCategoryAddDocumentBtn = document.getElementById('arCategoryAddDocumentBtn');
const arCategoryCrossChecksChipList = document.getElementById('arCategoryCrossChecksChipList');
const arCategoryAddCrossCheckSelect = document.getElementById('arCategoryAddCrossCheckSelect');
const arCategoryAddCrossCheckBtn = document.getElementById('arCategoryAddCrossCheckBtn');
const arCategoryCrossCheckChainPreview = document.getElementById('arCategoryCrossCheckChainPreview');
const arCategoryDocApprovalsList = document.getElementById('arCategoryDocApprovalsList');
const arCategoryMatrixVersion = document.getElementById('arCategoryMatrixVersion');
const arCategoryUsedByRow = document.getElementById('arCategoryUsedByRow');
const arCategoryUsedByCount = document.getElementById('arCategoryUsedByCount');
const arCategoryDetailStatus = document.getElementById('arCategoryDetailStatus');
const arCategorySaveBtn = document.getElementById('arCategorySaveBtn');
const arCategoryDeleteBtn = document.getElementById('arCategoryDeleteBtn');
const arCategoryRestoreBtn = document.getElementById('arCategoryRestoreBtn');

// ---------- STATE ----------
// null while creating a new Category; set to its id while editing an existing one.
let editingCategoryId = null;
// Flat arrays (insertion order only, no reorder) of the ids currently attached.
let categoryDocIds = [];
let categoryCrossCheckIds = [];
// Validation Rules (Phase 3 of 7): {document_id: approval_required (bool)}, populated from
// cat.documents on selectCategory() — a document's presence as a KEY here (not its value)
// is what "this pairing is actually persisted in audit_rule_category_documents" means, since
// a document just added locally (not yet saved) has no key at all.
let categoryDocApprovals = {};
// Automated cross-document validation engine (Phase 4 of 10): {document_id: is_required (bool)},
// same "key presence = persisted" convention as categoryDocApprovals above, populated
// alongside it.
let categoryDocRequired = {};
// Cache of all active Cross-checks ({id, name, documents: [{document_id, name}], ...}),
// mirroring Phase 6's allDocuments cache — used for the "add cross-check" picker's
// eligibility check and the document-removal warning. No reusable "all cross-checks"
// fetch existed before this phase (loadCrossChecks() is tied to the Cross-checks view's
// own show-inactive filter), so this is a new, Categories-scoped cache.
let allCrossChecks = [];

// ---------- STATUS MESSAGES ----------
// Standalone duplicate of showDetailStatus()/showCrossCheckDetailStatus() above, scoped
// to the Categories detail panel's own status div.
function showCategoryDetailStatus(message, type) {
    arCategoryDetailStatus.textContent = message;
    arCategoryDetailStatus.className = 'upload-status ' + type;
    arCategoryDetailStatus.style.display = 'block';
    clearTimeout(window.arCategoryDetailStatusTimeout);
    window.arCategoryDetailStatusTimeout = setTimeout(() => {
        arCategoryDetailStatus.style.display = 'none';
    }, 5000);
}

// ---------- CROSS-CHECK CACHE (for the add-cross-check picker + removal warning) ----------
async function loadAllCrossChecksForCategories() {
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/cross-checks?include_inactive=false');
        if (!res.ok) throw new Error('Failed to load cross-checks');
        allCrossChecks = await res.json();
    } catch (error) {
        allCrossChecks = [];
        showCategoryDetailStatus('❌ Error loading cross-checks: ' + error.message, 'error');
    }
}

function getCrossCheckById(id) {
    return allCrossChecks.find(cc => cc.id === id);
}

// ---------- LOAD & RENDER LIST ----------
async function loadCategories() {
    const includeInactive = arCategoryShowInactiveCheckbox.checked;
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/categories?include_inactive=' + includeInactive);
        if (!res.ok) throw new Error('Failed to load categories');
        const categories = await res.json();
        renderCategoryList(categories);
    } catch (error) {
        arCategoryListBody.innerHTML = '';
        arCategoryListEmptyMessage.textContent = '❌ Error loading categories: ' + error.message;
        arCategoryListEmptyMessage.style.display = 'block';
    }
}

function renderCategoryList(categories) {
    arCategoryListBody.innerHTML = '';

    if (categories.length === 0) {
        arCategoryListEmptyMessage.textContent = 'No categories found.';
        arCategoryListEmptyMessage.style.display = 'block';
        return;
    }
    arCategoryListEmptyMessage.style.display = 'none';

    categories.forEach(cat => {
        const row = document.createElement('div');
        row.className = 'ar-list-row' + (cat.is_active ? '' : ' ar-list-row-inactive');
        if (cat.id === editingCategoryId) row.classList.add('ar-list-row-selected');

        const nameSpan = document.createElement('span');
        nameSpan.className = 'ar-list-row-name';
        nameSpan.textContent = cat.name;
        row.appendChild(nameSpan);

        if (!cat.is_active) {
            const badge = document.createElement('span');
            badge.className = 'ar-list-row-badge';
            badge.textContent = 'Inactive';
            row.appendChild(badge);
        }

        row.addEventListener('click', () => selectCategory(cat.id));
        arCategoryListBody.appendChild(row);
    });
}

// ---------- DOCUMENTS TAG LIST (add / remove, with a removal warning) ----------
function renderCategoryDocChips() {
    arCategoryDocsChipList.innerHTML = '';

    if (categoryDocIds.length === 0) {
        const empty = document.createElement('span');
        empty.className = 'ar-chip-empty';
        empty.textContent = 'None added.';
        arCategoryDocsChipList.appendChild(empty);
    } else {
        categoryDocIds.forEach(docId => {
            const doc = getDocumentById(docId);
            const chip = document.createElement('span');
            chip.className = 'ar-chip';

            const label = document.createElement('span');
            label.textContent = doc ? doc.name : ('Document #' + docId);
            chip.appendChild(label);

            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'ar-chip-remove';
            removeBtn.textContent = '✕';
            removeBtn.title = 'Remove';
            removeBtn.addEventListener('click', () => removeCategoryDocument(docId));
            chip.appendChild(removeBtn);

            arCategoryDocsChipList.appendChild(chip);
        });
    }

    // Rebuild the "add document" picker to exclude documents already attached.
    arCategoryAddDocumentSelect.innerHTML = '';
    const available = allDocuments.filter(d => !categoryDocIds.includes(d.id));
    if (available.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.disabled = true;
        opt.selected = true;
        opt.textContent = allDocuments.length === 0 ? 'No documents exist yet' : 'All documents added';
        arCategoryAddDocumentSelect.appendChild(opt);
        arCategoryAddDocumentBtn.disabled = true;
    } else {
        available.forEach(d => {
            const opt = document.createElement('option');
            opt.value = d.id;
            opt.textContent = d.name;
            arCategoryAddDocumentSelect.appendChild(opt);
        });
        arCategoryAddDocumentBtn.disabled = false;
    }

    renderCategoryDocApprovalsList();
}

// ---------- REQUIRED / APPROVAL-REQUIRED PER DOCUMENT ----------
// (Validation Rules, Phase 3 of 7 added Approval Required; Automated cross-document
// validation engine, Phase 4 of 10 added Required, same row.) Sits alongside the Documents
// chip list above (not merged into each chip pill) — one row per currently-attached
// document, each with its own Required/Approved checkbox pair. A row's checkboxes are only
// enabled once its document is actually persisted in audit_rule_category_documents (i.e.
// present as a key in categoryDocApprovals/categoryDocRequired) — the PUT .../required and
// .../approval endpoints both 404 on a pairing that doesn't exist yet (a brand-new category,
// or a document just added locally but not yet saved). All styling is inline to override
// .ar-field label's block/bold styling, which would otherwise apply to every nested <label>
// including these.
function renderCategoryDocApprovalsList() {
    arCategoryDocApprovalsList.innerHTML = '';

    if (categoryDocIds.length === 0) {
        const empty = document.createElement('span');
        empty.className = 'ar-chip-empty';
        empty.textContent = 'No documents attached yet.';
        arCategoryDocApprovalsList.appendChild(empty);
        return;
    }

    categoryDocIds.forEach(docId => {
        const doc = getDocumentById(docId);
        const isPersisted = Object.prototype.hasOwnProperty.call(categoryDocApprovals, docId);

        const row = document.createElement('div');
        row.style.display = 'flex';
        row.style.alignItems = 'center';
        row.style.gap = '16px';
        row.style.fontSize = '13px';
        row.style.color = isPersisted ? '#1a1a2e' : '#94a3b8';
        row.style.padding = '4px 0';

        const nameSpan = document.createElement('span');
        nameSpan.style.flex = '1';
        nameSpan.textContent = (doc ? doc.name : ('Document #' + docId)) +
            (isPersisted ? '' : ' (save the category first)');
        row.appendChild(nameSpan);

        // Defaults to checked/required, matching audit_rule_category_documents.is_required's
        // own DB default (TRUE) — a document whose key isn't in categoryDocRequired yet
        // (not persisted) still shows checked, so nothing looks "unrequired" before save.
        const requiredLabel = document.createElement('label');
        requiredLabel.style.display = 'flex';
        requiredLabel.style.alignItems = 'center';
        requiredLabel.style.gap = '6px';
        requiredLabel.style.fontWeight = '400';
        requiredLabel.style.cursor = isPersisted ? 'pointer' : 'default';
        requiredLabel.style.marginBottom = '0';
        const requiredCheckbox = document.createElement('input');
        requiredCheckbox.type = 'checkbox';
        requiredCheckbox.checked = isPersisted ? !!categoryDocRequired[docId] : true;
        requiredCheckbox.disabled = !isPersisted;
        requiredCheckbox.addEventListener('change', () => toggleCategoryDocumentRequired(docId, requiredCheckbox.checked));
        requiredLabel.appendChild(requiredCheckbox);
        requiredLabel.appendChild(document.createTextNode('Required'));
        row.appendChild(requiredLabel);

        const approvedLabel = document.createElement('label');
        approvedLabel.style.display = 'flex';
        approvedLabel.style.alignItems = 'center';
        approvedLabel.style.gap = '6px';
        approvedLabel.style.fontWeight = '400';
        approvedLabel.style.cursor = isPersisted ? 'pointer' : 'default';
        approvedLabel.style.marginBottom = '0';
        const approvedCheckbox = document.createElement('input');
        approvedCheckbox.type = 'checkbox';
        approvedCheckbox.checked = !!categoryDocApprovals[docId];
        approvedCheckbox.disabled = !isPersisted;
        approvedCheckbox.addEventListener('change', () => toggleCategoryDocumentApproval(docId, approvedCheckbox.checked));
        approvedLabel.appendChild(approvedCheckbox);
        approvedLabel.appendChild(document.createTextNode('Approved'));
        row.appendChild(approvedLabel);

        arCategoryDocApprovalsList.appendChild(row);
    });
}

async function toggleCategoryDocumentApproval(docId, checked) {
    if (!editingCategoryId) return;

    try {
        const res = await fetch(
            API_BASE + '/api/audit-rules/categories/' + editingCategoryId + '/documents/' + docId + '/approval',
            {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ approval_required: checked })
            }
        );
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to update approval requirement');

        const changed = !!categoryDocApprovals[docId] !== checked;
        categoryDocApprovals[docId] = checked;
        if (changed) {
            // Mirrors the backend's own no-op-doesn't-bump rule (bump_category_matrix_version()
            // only fires on an actual value change) — no extra fetch needed to know this.
            arCategoryMatrixVersion.textContent = String(parseInt(arCategoryMatrixVersion.textContent, 10) + 1);
        }
        showCategoryDetailStatus('✅ Approval requirement updated.', 'success');
    } catch (error) {
        showCategoryDetailStatus('❌ Error: ' + error.message, 'error');
        renderCategoryDocApprovalsList();
    }
}

async function toggleCategoryDocumentRequired(docId, checked) {
    if (!editingCategoryId) return;

    try {
        const res = await fetch(
            API_BASE + '/api/audit-rules/categories/' + editingCategoryId + '/documents/' + docId + '/required',
            {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ is_required: checked })
            }
        );
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to update required flag');

        const changed = !!categoryDocRequired[docId] !== checked;
        categoryDocRequired[docId] = checked;
        if (changed) {
            // Mirrors the backend's own no-op-doesn't-bump rule, same as the approval toggle.
            arCategoryMatrixVersion.textContent = String(parseInt(arCategoryMatrixVersion.textContent, 10) + 1);
        }
        // Dimmed/optional pill state in the chain preview depends on categoryDocRequired,
        // which just changed here without going through renderCategoryCrossCheckChips().
        renderCategoryCrossCheckChainPreview();
        showCategoryDetailStatus('✅ Required flag updated.', 'success');
    } catch (error) {
        showCategoryDetailStatus('❌ Error: ' + error.message, 'error');
        renderCategoryDocApprovalsList();
    }
}

function addCategoryDocument() {
    const value = arCategoryAddDocumentSelect.value;
    if (!value) return;
    categoryDocIds.push(parseInt(value, 10));
    renderCategoryDocChips();
    // A newly-added document may make a previously-ineligible cross-check selectable.
    renderCategoryCrossCheckChips();
}

// Which currently-attached Cross-checks (by name) reference the given Document —
// used both by the removal warning below and could be reused if this check is ever
// needed elsewhere in this view.
function computeAffectedCrossChecks(docId) {
    return categoryCrossCheckIds
        .map(ccId => getCrossCheckById(ccId))
        .filter(cc => cc && (cc.documents || []).some(d => d.document_id === docId))
        .map(cc => cc.name);
}

function removeCategoryDocument(docId) {
    const doc = getDocumentById(docId);
    const affected = computeAffectedCrossChecks(docId);
    if (affected.length > 0) {
        const proceed = confirm(
            'Removing "' + (doc ? doc.name : 'this document') + '" will leave the following ' +
            'attached Cross-check(s) referencing a document no longer in this Category: ' +
            affected.join(', ') + '. Remove anyway?'
        );
        if (!proceed) return;
    }
    categoryDocIds = categoryDocIds.filter(id => id !== docId);
    renderCategoryDocChips();
    // Removing a document can make an already-attached cross-check ineligible again —
    // re-render so the picker's disabled/hint state (and the chip list) stay accurate.
    renderCategoryCrossCheckChips();
}

// ---------- LIVE DOCUMENT-CHAIN PREVIEW (Automated cross-document validation engine, Phase 10, FINAL) ----------
// Config-time visual only, no evaluation logic and no real voucher data — reads exactly the
// same client-side state already loaded for this Category (categoryCrossCheckIds,
// categoryDocRequired) plus each attached Cross-check's own ordered documents list, already
// present on the allCrossChecks cache via getCrossCheckById(). One row per attached
// Cross-check, since chain order is per-Cross-check, not a single combined view across a
// Category's Cross-checks (confirmed with the user). Called from renderCategoryCrossCheckChips()
// (covers all its own callers: selectCategory/startNewCategory/add-remove document/cross-check)
// and from toggleCategoryDocumentRequired() (the one place categoryDocRequired changes without
// going through renderCategoryCrossCheckChips()).
function renderCategoryCrossCheckChainPreview() {
    arCategoryCrossCheckChainPreview.innerHTML = '';

    if (categoryCrossCheckIds.length === 0) {
        const empty = document.createElement('span');
        empty.className = 'ar-chip-empty';
        empty.textContent = 'No cross-checks attached yet.';
        arCategoryCrossCheckChainPreview.appendChild(empty);
        return;
    }

    categoryCrossCheckIds.forEach(ccId => {
        const cc = getCrossCheckById(ccId);
        const row = document.createElement('div');
        row.className = 'ar-chain-row';

        if (!cc) {
            row.textContent = 'Cross-check #' + ccId + ' (inactive)';
            row.classList.add('ar-chain-row-unavailable');
            arCategoryCrossCheckChainPreview.appendChild(row);
            return;
        }

        const nameLabel = document.createElement('span');
        nameLabel.className = 'ar-chain-cc-name';
        nameLabel.textContent = cc.name;
        row.appendChild(nameLabel);

        const pillsWrap = document.createElement('div');
        pillsWrap.className = 'ar-chain-pills';

        const docs = cc.documents || [];
        if (docs.length === 0) {
            const empty = document.createElement('span');
            empty.className = 'ar-chip-empty';
            empty.textContent = 'No documents in this chain yet.';
            pillsWrap.appendChild(empty);
        } else {
            docs.forEach((d, index) => {
                const doc = getDocumentById(d.document_id);
                const pill = document.createElement('span');
                pill.className = 'ar-chain-pill';
                if (categoryDocRequired[d.document_id] === false) {
                    pill.classList.add('optional');
                }
                pill.textContent = doc ? doc.name : ('Document #' + d.document_id);

                if (index === docs.length - 1) {
                    const anchorTag = document.createElement('span');
                    anchorTag.className = 'ar-chain-anchor-tag';
                    anchorTag.textContent = ' (anchor)';
                    pill.appendChild(anchorTag);
                }

                pillsWrap.appendChild(pill);

                if (index < docs.length - 1) {
                    const arrow = document.createElement('span');
                    arrow.className = 'ar-chain-arrow';
                    arrow.textContent = '→';
                    pillsWrap.appendChild(arrow);
                }
            });
        }

        row.appendChild(pillsWrap);
        arCategoryCrossCheckChainPreview.appendChild(row);
    });
}

// ---------- CROSS-CHECKS TAG LIST (add / remove, eligibility-gated picker) ----------
function renderCategoryCrossCheckChips() {
    renderCategoryCrossCheckChainPreview();
    arCategoryCrossChecksChipList.innerHTML = '';

    if (categoryCrossCheckIds.length === 0) {
        const empty = document.createElement('span');
        empty.className = 'ar-chip-empty';
        empty.textContent = 'None added.';
        arCategoryCrossChecksChipList.appendChild(empty);
    } else {
        categoryCrossCheckIds.forEach(ccId => {
            const cc = getCrossCheckById(ccId);
            const chip = document.createElement('span');
            chip.className = 'ar-chip';

            const label = document.createElement('span');
            label.textContent = cc ? cc.name : ('Cross-check #' + ccId);
            chip.appendChild(label);

            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'ar-chip-remove';
            removeBtn.textContent = '✕';
            removeBtn.title = 'Remove';
            removeBtn.addEventListener('click', () => removeCategoryCrossCheck(ccId));
            chip.appendChild(removeBtn);

            arCategoryCrossChecksChipList.appendChild(chip);
        });
    }

    // Rebuild the "add cross-check" picker. A cross-check is only selectable once every
    // one of its own documents is already in this Category's Documents list (mirrors
    // validate_category_payload()'s server-side containment rule, routers/audit_rules.py)
    // — ineligible ones stay visible but disabled with a hint, rather than hidden, so the
    // user can see what's blocking them.
    arCategoryAddCrossCheckSelect.innerHTML = '';
    const available = allCrossChecks.filter(cc => !categoryCrossCheckIds.includes(cc.id));

    if (available.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.disabled = true;
        opt.selected = true;
        opt.textContent = allCrossChecks.length === 0 ? 'No cross-checks exist yet' : 'All cross-checks added';
        arCategoryAddCrossCheckSelect.appendChild(opt);
        arCategoryAddCrossCheckBtn.disabled = true;
        return;
    }

    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.disabled = true;
    placeholder.selected = true;
    placeholder.textContent = 'Select a cross-check';
    arCategoryAddCrossCheckSelect.appendChild(placeholder);

    available.forEach(cc => {
        const missingDocs = (cc.documents || [])
            .filter(d => !categoryDocIds.includes(d.document_id))
            .map(d => d.name);
        const opt = document.createElement('option');
        opt.value = cc.id;
        opt.disabled = missingDocs.length > 0;
        opt.textContent = missingDocs.length > 0
            ? cc.name + ' (needs: ' + missingDocs.join(', ') + ')'
            : cc.name;
        arCategoryAddCrossCheckSelect.appendChild(opt);
    });

    arCategoryAddCrossCheckBtn.disabled = false;
}

function addCategoryCrossCheck() {
    const value = arCategoryAddCrossCheckSelect.value;
    if (!value) return;
    categoryCrossCheckIds.push(parseInt(value, 10));
    renderCategoryCrossCheckChips();
}

function removeCategoryCrossCheck(ccId) {
    categoryCrossCheckIds = categoryCrossCheckIds.filter(id => id !== ccId);
    renderCategoryCrossCheckChips();
}

// ---------- SELECT / NEW / RESET DETAIL PANEL ----------
async function selectCategory(id) {
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/categories/' + id);
        if (!res.ok) throw new Error('Category not found');
        const cat = await res.json();

        editingCategoryId = cat.id;
        arCategoryEditingIdInput.value = cat.id;
        arCategoryNameInput.value = cat.name;
        arCategoryKbOverviewInput.value = cat.kb_overview || '';
        categoryDocIds = (cat.documents || []).map(d => d.document_id);
        categoryCrossCheckIds = (cat.cross_checks || []).map(c => c.cross_check_id);
        categoryDocApprovals = {};
        categoryDocRequired = {};
        (cat.documents || []).forEach(d => {
            categoryDocApprovals[d.document_id] = !!d.approval_required;
            categoryDocRequired[d.document_id] = !!d.is_required;
        });
        arCategoryMatrixVersion.textContent = String(cat.matrix_version || 1);
        renderCategoryDocChips();
        renderCategoryCrossCheckChips();

        arCategoryUsedByRow.style.display = 'block';
        arCategoryUsedByCount.textContent = cat.used_by_vouchers_count;

        if (cat.is_active) {
            arCategoryDeleteBtn.style.display = 'inline-block';
            arCategoryRestoreBtn.style.display = 'none';
        } else {
            arCategoryDeleteBtn.style.display = 'none';
            arCategoryRestoreBtn.style.display = 'inline-block';
        }

        arCategoryDetailEmpty.style.display = 'none';
        arCategoryDetailForm.style.display = 'block';

        // Re-render the list so the selected row highlight moves without a full re-fetch.
        loadCategories();
    } catch (error) {
        showCategoryDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

function startNewCategory() {
    editingCategoryId = null;
    arCategoryEditingIdInput.value = '';
    arCategoryNameInput.value = '';
    arCategoryKbOverviewInput.value = '';
    categoryDocIds = [];
    categoryCrossCheckIds = [];
    categoryDocApprovals = {};
    categoryDocRequired = {};
    arCategoryMatrixVersion.textContent = '1';
    renderCategoryDocChips();
    renderCategoryCrossCheckChips();

    arCategoryUsedByRow.style.display = 'none';
    arCategoryDeleteBtn.style.display = 'none';
    arCategoryRestoreBtn.style.display = 'none';

    arCategoryDetailEmpty.style.display = 'none';
    arCategoryDetailForm.style.display = 'block';
    arCategoryNameInput.focus();
}

// ---------- SAVE ----------
async function saveCategory() {
    const name = arCategoryNameInput.value.trim();
    if (!name) {
        showCategoryDetailStatus('❌ Name is required.', 'error');
        return;
    }

    const payload = {
        name: name,
        kb_overview: arCategoryKbOverviewInput.value.trim() || null,
        document_ids: categoryDocIds,
        cross_check_ids: categoryCrossCheckIds
    };

    try {
        let url = API_BASE + '/api/audit-rules/categories';
        let method = 'POST';
        if (editingCategoryId) {
            url += '/' + editingCategoryId;
            method = 'PUT';
        }

        const res = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const result = await res.json();
        // The server's validation error already names the specific conflicting
        // Cross-check and Document ID(s) (validate_category_payload(), routers/
        // audit_rules.py) — surfacing result.detail as-is already satisfies "a clear
        // inline error naming which Cross-check/Document conflicts", no extra parsing
        // needed here.
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to save category');

        showCategoryDetailStatus('✅ ' + result.message, 'success');

        // POST doesn't echo back the full row, so re-fetch the list, then re-select
        // to refresh the detail panel's is_active/used_by state from the server.
        const savedId = editingCategoryId || result.category_id;
        await loadCategories();
        await selectCategory(savedId);
    } catch (error) {
        showCategoryDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

// ---------- DELETE / RESTORE ----------
async function deleteCategory() {
    if (!editingCategoryId) return;
    if (!confirm('Are you sure you want to delete "' + arCategoryNameInput.value + '"? This will soft-delete it.')) return;

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/categories/' + editingCategoryId, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to delete category');

        showCategoryDetailStatus('✅ ' + result.message, 'success');
        await loadCategories();
        await selectCategory(editingCategoryId);
    } catch (error) {
        showCategoryDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

async function restoreCategory() {
    if (!editingCategoryId) return;

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/categories/' + editingCategoryId + '/restore', { method: 'POST' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to restore category');

        showCategoryDetailStatus('✅ ' + result.message, 'success');
        await loadCategories();
        await selectCategory(editingCategoryId);
    } catch (error) {
        showCategoryDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

// ---------- WIRE UP EVENT LISTENERS ----------
arCategoryAddNewBtn.addEventListener('click', startNewCategory);
arCategoryShowInactiveCheckbox.addEventListener('change', loadCategories);

arCategoryAddDocumentBtn.addEventListener('click', addCategoryDocument);
arCategoryAddCrossCheckBtn.addEventListener('click', addCategoryCrossCheck);

arCategorySaveBtn.addEventListener('click', saveCategory);
arCategoryDeleteBtn.addEventListener('click', deleteCategory);
arCategoryRestoreBtn.addEventListener('click', restoreCategory);

// ==============================================
// 🧮 AUDIT RULES — ASSIGN CATEGORIES (Section 2, Phase 5 of 5, FINAL)
// ==============================================
// Full-page view (not another rail entity type) listing the current client's latest
// parsed Trial Balance accounts, with a per-row Category <select> (smart-fill cascade
// on change) and a force-apply icon on parent rows only. Uses Phase 4's
// GET /api/trial-balance/latest-accounts and PUT /api/trial-balance/assign-category.
// Toggled via #arShellContainer <-> #arAssignCategoriesSection, mirroring
// trial_balance.js's #uploadViewContainer <-> #availableTrialBalancesSection pattern —
// a distinct, unrelated screen, so it swaps the whole shell rather than living inside
// the rail/list/detail three-pane layout.

// ---------- DOM REFS ----------
const arShellContainer = document.getElementById('arShellContainer');
const arAssignCategoriesBtn = document.getElementById('arAssignCategoriesBtn');
const arAssignCategoriesSection = document.getElementById('arAssignCategoriesSection');
const arAssignCategoriesBackBtn = document.getElementById('arAssignCategoriesBackBtn');
const arResetCategoriesBtn = document.getElementById('arResetCategoriesBtn');
const arAssignCategoriesSubtitle = document.getElementById('arAssignCategoriesSubtitle');
const arAssignCategoriesStatus = document.getElementById('arAssignCategoriesStatus');
const arAssignCategoriesEmpty = document.getElementById('arAssignCategoriesEmpty');
const arAssignCategoriesTable = document.getElementById('arAssignCategoriesTable');
const arAssignCategoriesTableBody = document.getElementById('arAssignCategoriesTableBody');

// ---------- STATE ----------
// Raw accounts from the latest-accounts response, refreshed every time the view opens
// (same "refetch on open, don't cache indefinitely" convention as Cross-checks'/
// Categories' own document/cross-check caches above).
let assignCategoriesAccounts = [];
// Active Categories ({id, name}) for the per-row dropdown options, refreshed alongside
// the accounts on every open so a Category added/renamed elsewhere is picked up.
let assignCategoriesCategoryOptions = [];

// ---------- STATUS MESSAGES ----------
// Same timeout-based show/hide pattern as showDetailStatus()/showCategoryDetailStatus().
function showAssignCategoriesStatus(message, type) {
    arAssignCategoriesStatus.textContent = message;
    arAssignCategoriesStatus.className = 'upload-status ' + type;
    arAssignCategoriesStatus.style.display = 'block';
    clearTimeout(window.arAssignCategoriesStatusTimeout);
    window.arAssignCategoriesStatusTimeout = setTimeout(() => {
        arAssignCategoriesStatus.style.display = 'none';
    }, 5000);
}

// ---------- OPEN / CLOSE (whole-shell swap) ----------
// async/await added for the Verification -> Assign Categories deep link (Phase 5), which needs
// to know once the accounts have actually loaded before it can locate/highlight a row. The
// existing in-app button click listener ignores the returned promise, so this is fully
// backward-compatible with that call site.
async function openAssignCategoriesView() {
    arShellContainer.style.display = 'none';
    arAssignCategoriesSection.style.display = 'block';
    await loadAssignCategoriesData();
}

function closeAssignCategoriesView() {
    arAssignCategoriesSection.style.display = 'none';
    arShellContainer.style.display = 'flex';
}

// ---------- LOAD ----------
async function loadAssignCategoriesData() {
    // Client selection reuses the global client context (client_context.js) — a client
    // is always already selected on this page, since enforceClientSelection() redirects
    // to the dashboard otherwise, same as every other module page.
    const clientId = getSelectedClientId();

    arAssignCategoriesTable.style.display = 'none';
    arAssignCategoriesEmpty.style.display = 'none';
    arAssignCategoriesSubtitle.textContent = '';

    try {
        const [categoriesRes, accountsRes] = await Promise.all([
            fetch(API_BASE + '/api/audit-rules/categories?include_inactive=false'),
            fetch(API_BASE + '/api/trial-balance/latest-accounts?client_id=' + clientId)
        ]);

        if (!categoriesRes.ok) throw new Error('Failed to load categories');
        const categories = await categoriesRes.json();
        assignCategoriesCategoryOptions = categories.map(c => ({ id: c.id, name: c.name }));

        if (accountsRes.status === 404) {
            // No parsed TB exists yet for this client — a clear empty state, not a
            // blank/broken table, per the confirmed brief.
            const result = await accountsRes.json();
            assignCategoriesAccounts = [];
            arAssignCategoriesEmpty.textContent = result.detail || 'No parsed trial balance found for this client.';
            arAssignCategoriesEmpty.style.display = 'block';
            return;
        }
        if (!accountsRes.ok) throw new Error('Failed to load trial balance accounts');

        const data = await accountsRes.json();
        assignCategoriesAccounts = data.accounts;

        const periodLabel = (data.period_start && data.period_end)
            ? (data.period_type ? data.period_type + ' ' : '') + data.period_start + ' to ' + data.period_end
            : 'As of ' + data.as_of_date;
        arAssignCategoriesSubtitle.textContent = periodLabel;

        if (assignCategoriesAccounts.length === 0) {
            arAssignCategoriesEmpty.textContent = 'This trial balance has no accounts.';
            arAssignCategoriesEmpty.style.display = 'block';
            return;
        }

        renderAssignCategoriesTable();
        arAssignCategoriesTable.style.display = 'table';

        // Confirmed fix: re-apply the deep-link highlight on every load of this view (in-app
        // button reopen, Reset), not just the initial arrival — highlightAssignCategoriesRow()
        // itself re-checks the account's current assignment status against this fresh data on
        // every call, so it only shows for as long as the account remains unassigned.
        if (deepLinkAccountCode) {
            highlightAssignCategoriesRow(deepLinkAccountCode);
        }
    } catch (error) {
        assignCategoriesAccounts = [];
        arAssignCategoriesEmpty.textContent = '❌ Error loading data: ' + error.message;
        arAssignCategoriesEmpty.style.display = 'block';
    }
}

// ---------- RENDER TABLE ----------
function renderAssignCategoriesTable() {
    arAssignCategoriesTableBody.innerHTML = '';
    assignCategoriesAccounts.forEach(account => {
        arAssignCategoriesTableBody.appendChild(buildAssignCategoriesRow(account));
    });
}

// Builds one <tr> for an account. createElement/textContent throughout (no innerHTML)
// since account codes/names come from an uploaded file, not this app's own control —
// same convention trial_balance.js's renderReviewTable() already follows.
function buildAssignCategoriesRow(account) {
    const tr = document.createElement('tr');
    tr.dataset.accountId = account.id;

    const tdCode = document.createElement('td');
    tdCode.textContent = account.account_code;
    tr.appendChild(tdCode);

    const tdName = document.createElement('td');
    tdName.textContent = account.account_name || '';
    tr.appendChild(tdName);

    const tdBadge = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = 'ar-tb-badge ' + (account.is_leaf ? 'leaf' : 'parent');
    badge.textContent = account.is_leaf ? 'Leaf' : 'Parent';
    tdBadge.appendChild(badge);
    tr.appendChild(tdBadge);

    const tdCategory = document.createElement('td');
    tdCategory.className = 'ar-tb-category-cell';

    const select = document.createElement('select');
    select.className = 'ar-tb-category-select';
    select.dataset.accountId = account.id;

    // Confirmed fix: "Not assigned" is now a real, selectable option (value="none"),
    // not a disabled placeholder — picking it clears the account's category the same
    // way picking a real category assigns one (see the change listener below).
    const notAssignedOption = document.createElement('option');
    notAssignedOption.value = 'none';
    notAssignedOption.textContent = '-- Not assigned --';
    if (!account.category_id) notAssignedOption.selected = true;
    select.appendChild(notAssignedOption);

    assignCategoriesCategoryOptions.forEach(cat => {
        const opt = document.createElement('option');
        opt.value = cat.id;
        opt.textContent = cat.name;
        if (account.category_id === cat.id) opt.selected = true;
        select.appendChild(opt);
    });
    tdCategory.appendChild(select);

    // Force-apply icon: parent rows only, omitted entirely (not just hidden/disabled)
    // for leaves, since force_apply is a documented no-op there.
    if (!account.is_leaf) {
        const forceBtn = document.createElement('button');
        forceBtn.type = 'button';
        forceBtn.className = 'ar-tb-force-apply-icon';
        forceBtn.dataset.accountId = account.id;
        forceBtn.textContent = '⤵';
        forceBtn.title = 'Force-apply the selected category to every descendant, overwriting any existing assignment';
        tdCategory.appendChild(forceBtn);
    }

    tr.appendChild(tdCategory);
    return tr;
}

// ---------- LOOKUPS ----------
function getAssignCategoriesAccountById(id) {
    return assignCategoriesAccounts.find(a => a.id === id);
}
function getAssignCategoriesCategoryName(id) {
    if (!id) return 'Not assigned';
    const cat = assignCategoriesCategoryOptions.find(c => c.id === id);
    return cat ? cat.name : null;
}

// ---------- ASSIGN (smart-fill default / force-apply explicit) ----------
async function assignCategory(accountId, categoryId, forceApply) {
    const targetAccount = getAssignCategoriesAccountById(accountId);
    const previousCategoryId = targetAccount ? targetAccount.category_id : null;

    try {
        const res = await fetch(API_BASE + '/api/trial-balance/assign-category', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ account_id: accountId, category_id: categoryId, force_apply: forceApply })
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to assign category');

        // Only the target account plus whatever the backend actually updated
        // (result.descendants_updated) get their displayed Category refreshed —
        // descendants_skipped are deliberately left alone, so the UI reflects the real
        // smart-fill outcome rather than assuming every descendant changed.
        // categoryId/categoryName are null when the user picked "Not assigned" —
        // getAssignCategoriesCategoryName() already returns the 'Not assigned' label
        // for a null/falsy id, so this reads correctly either way.
        const categoryName = getAssignCategoriesCategoryName(categoryId);
        const affectedIds = [accountId].concat(result.descendants_updated || []);
        applyCategoryToRows(affectedIds, categoryId, categoryId ? categoryName : null);

        const actionLabel = categoryId ? 'Category assigned' : 'Category cleared';
        if (result.is_leaf) {
            showAssignCategoriesStatus('✅ ' + actionLabel + '.', 'success');
        } else {
            showAssignCategoriesStatus(
                '✅ ' + actionLabel + (forceApply ? ' (force-applied)' : '') + '. ' +
                result.descendants_updated_count + ' descendant(s) updated, ' +
                result.descendants_skipped_count + ' left unchanged.',
                'success'
            );
        }
    } catch (error) {
        // Revert the target row's dropdown to its pre-change value — state was never
        // optimistically updated, so only the native <select>'s own visible value (which
        // the browser already changed on user selection) needs correcting back.
        const select = arAssignCategoriesTableBody.querySelector('select[data-account-id="' + accountId + '"]');
        if (select) select.value = previousCategoryId || 'none';
        showAssignCategoriesStatus('❌ Error: ' + error.message, 'error');
    }
}

// Updates both the in-memory account list and the visible <select> value for a set of
// account ids, after a successful assignment call. categoryId may be null/falsy (the
// account was cleared to "Not assigned") — the <select>'s "none" option value is used
// in that case, since a real <select> element has no concept of a null value.
function applyCategoryToRows(accountIds, categoryId, categoryName) {
    accountIds.forEach(id => {
        const account = getAssignCategoriesAccountById(id);
        if (account) {
            account.category_id = categoryId || null;
            account.category_name = categoryName;
        }
        const select = arAssignCategoriesTableBody.querySelector('select[data-account-id="' + id + '"]');
        if (select) select.value = categoryId || 'none';
    });
}

// Converts a <select>'s raw string value ("none" or a numeric category id string)
// into the value assignCategory()/the backend expect: null, or a real integer id.
function parseAssignCategoriesSelectValue(rawValue) {
    return (!rawValue || rawValue === 'none') ? null : parseInt(rawValue, 10);
}

// ---------- RESET (confirmed fix) ----------
// Bulk-clears every account's Category for the client's currently loaded (latest
// parsed) Trial Balance only — a hard-to-reverse action, so it requires a confirm()
// step first, matching this app's existing convention for destructive bulk actions.
async function resetAssignCategories() {
    const clientId = getSelectedClientId();
    const proceed = confirm(
        'Reset every account\'s Category to "Not assigned" for this client\'s current ' +
        'Trial Balance? This cannot be undone.'
    );
    if (!proceed) return;

    try {
        const res = await fetch(API_BASE + '/api/trial-balance/reset-categories?client_id=' + clientId, {
            method: 'POST'
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to reset categories');

        showAssignCategoriesStatus('✅ Reset ' + result.reset_count + ' account(s) to Not assigned.', 'success');
        // Full reload rather than a local state patch — simplest way to guarantee the
        // table matches the server exactly after a bulk change touching every row.
        await loadAssignCategoriesData();
    } catch (error) {
        showAssignCategoriesStatus('❌ Error: ' + error.message, 'error');
    }
}

// ---------- VERIFICATION DEEP LINK (Verification ↔ Assign Categories link, Phase 5, FINAL) ----------
// Verification's recycle-arrow icon (unmatched lines) opens this page as
// audit_rules.html?client_id=&account_code= in a new tab. Design decision: client_id is read and
// required only to recognize a deep link — it is NOT used to switch the globally-selected client
// (getSelectedClientId()/localStorage, which loadAssignCategoriesData() already sources from and
// which this phase leaves completely untouched). At the moment Verification's icon is clicked,
// its own getClientId() and this page's getSelectedClientId() read the exact same localStorage
// value, so they're identical by construction — the only way they could differ is an unrelated
// tab changing the active client in the brief window before this tab finishes loading, and in
// that rare case the account code simply won't be found below (a harmless no-op, not a crash).

// Confirmed fix: the account code carried by the URL, remembered for the entire life of this
// page load (not just the initial arrival) — so the highlight below can be re-evaluated and
// re-applied every time the Assign Categories view (re)loads (in-app button reopen, Reset), not
// only on the very first "just arrived via URL param" check.
let deepLinkAccountCode = null;

// Scrolls the row matching accountCode into view and outlines it in light red (cleared by the
// row's own Category-select change listener below) — but only while that account is still
// unassigned. Re-checked fresh against assignCategoriesAccounts (always just-reloaded by
// loadAssignCategoriesData()) on every call, so this naturally stops applying once the account
// is actually assigned, and naturally reapplies if a Reset clears it back to unassigned. A no-op
// if the code isn't in the currently loaded (client-scoped) account list — covers "no parsed TB
// for this client", "code not in the latest TB", and the rare client-mismatch edge case described
// above (see the earlier design-decision comment on client scoping).
function highlightAssignCategoriesRow(accountCode) {
    const account = assignCategoriesAccounts.find(a => a.account_code === accountCode);
    if (!account || account.category_id) return; // not found, or already assigned - nothing to flag
    const row = arAssignCategoriesTableBody.querySelector('tr[data-account-id="' + account.id + '"]');
    if (!row) return;
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    row.classList.add('ar-tb-deep-link-highlight');
}

// Reads client_id + account_code from the page URL; if both are present this is a deep link, so
// it remembers the account code (deepLinkAccountCode, above) and opens the Assign Categories view
// directly (bypassing the normal Documents-rail default) instead of the plain in-app entry.
// Returns false (no side effects) for a normal, non-deep-link page load, so window.onload's
// existing default entry stays unaffected. This is legitimately a one-time check (window.onload
// only fires once) — it only decides how the page is *entered*; the highlight itself is
// re-evaluated on every load inside loadAssignCategoriesData(), not here, which is what makes it
// reapply correctly on a later reopen within the same page life.
async function checkAssignCategoriesDeepLink() {
    const params = new URLSearchParams(window.location.search);
    const clientId = params.get('client_id');
    const accountCode = params.get('account_code');
    if (!clientId || !accountCode) return false;

    deepLinkAccountCode = accountCode;
    await openAssignCategoriesView();
    return true;
}

// ---------- WIRE UP EVENT LISTENERS ----------
arAssignCategoriesBtn.addEventListener('click', openAssignCategoriesView);
arAssignCategoriesBackBtn.addEventListener('click', closeAssignCategoriesView);
arResetCategoriesBtn.addEventListener('click', resetAssignCategories);

// Delegated listeners on the table body (rows are rebuilt from scratch on every load).
arAssignCategoriesTableBody.addEventListener('change', function(e) {
    if (!e.target.matches('select[data-account-id]')) return;
    // Verification deep link (Phase 5): the light-red highlight clears the moment a Category is
    // selected for this row — independent of whether the save below succeeds or fails, per the
    // confirmed spec. A row that was never highlighted simply has nothing to remove here.
    const row = e.target.closest('tr');
    if (row) row.classList.remove('ar-tb-deep-link-highlight');
    const categoryId = parseAssignCategoriesSelectValue(e.target.value);
    const accountId = parseInt(e.target.dataset.accountId, 10);
    assignCategory(accountId, categoryId, false);
});

arAssignCategoriesTableBody.addEventListener('click', function(e) {
    const btn = e.target.closest('.ar-tb-force-apply-icon');
    if (!btn) return;
    const accountId = parseInt(btn.dataset.accountId, 10);
    const select = arAssignCategoriesTableBody.querySelector('select[data-account-id="' + accountId + '"]');
    if (!select) return;
    // "Not assigned" is a real selectable value now, so force-apply works with it too
    // (clears the target and cascades that clearing to every descendant) — no "select
    // a category first" guard needed anymore.
    const categoryId = parseAssignCategoriesSelectValue(select.value);
    const account = getAssignCategoriesAccountById(accountId);
    const categoryName = getAssignCategoriesCategoryName(categoryId);
    const proceed = confirm(
        'Force-apply "' + categoryName + '" to every descendant of ' +
        (account ? account.account_code : accountId) +
        ', overwriting any category already assigned to them? This cannot be undone.'
    );
    if (!proceed) return;
    assignCategory(accountId, categoryId, true);
});

// ---------- INIT ----------
window.onload = async function() {
    // Verification deep link (Phase 5, FINAL): a ?client_id=&account_code= URL means Verification's
    // recycle-arrow icon opened this page — skip the normal Documents-rail default entirely and go
    // straight to the highlighted Assign Categories row. A plain/in-app page load (no params) is
    // completely unaffected, matching the confirmed brief.
    const isDeepLink = await checkAssignCategoriesDeepLink();
    if (!isDeepLink) {
        loadDocuments();
        loadTemplatesForDocuments();
    }
};

// ==============================================
// 🖊️ AUDIT RULES — SIGNATURE SPECIMENS (Validation Rules, Phase 3 of 7)
// ==============================================
// Same rail → list → detail duplication pattern as Documents/Cross-checks/Categories above
// (own markup ids, own state, own functions), per this module's documented "full duplication
// per entity type" convention. Always scoped to the globally-selected client
// (getSelectedClientId(), client_context.js) — client selection is enforced on every
// non-dashboard page, so it's always available here, same assumption Assign Categories above
// already relies on. No "Show inactive" toggle and no Restore button: signature_specimens has
// no is_active column (Phase 1 schema), so Delete is a real hard delete.

// ---------- DOM REFS ----------
const arSpecimenListPanelTitle = document.getElementById('arSpecimenListPanelTitle');
const arSpecimenListBody = document.getElementById('arSpecimenListBody');
const arSpecimenListEmptyMessage = document.getElementById('arSpecimenListEmptyMessage');
const arSpecimenAddNewBtn = document.getElementById('arSpecimenAddNewBtn');

const arSpecimenDetailEmpty = document.getElementById('arSpecimenDetailEmpty');
const arSpecimenDetailForm = document.getElementById('arSpecimenDetailForm');
const arSpecimenEditingIdInput = document.getElementById('arSpecimenEditingId');
const arSpecimenDocumentSelect = document.getElementById('arSpecimenDocumentSelect');
const arSpecimenRoleSelect = document.getElementById('arSpecimenRoleSelect');
const arSpecimenNameInput = document.getElementById('arSpecimenNameInput');
const arSpecimenDetailStatus = document.getElementById('arSpecimenDetailStatus');
const arSpecimenSaveBtn = document.getElementById('arSpecimenSaveBtn');
const arSpecimenDeleteBtn = document.getElementById('arSpecimenDeleteBtn');

// ---------- STATE ----------
// null while creating a new specimen; set to its id while editing an existing one.
let editingSpecimenId = null;
// Current client's specimens, refetched every time this view opens (same "refetch on open,
// don't cache indefinitely" convention as allDocuments/allCrossChecks above).
let allSpecimens = [];

// ---------- STATUS MESSAGES ----------
// Standalone duplicate of showDetailStatus()/showCrossCheckDetailStatus()/
// showCategoryDetailStatus() above, scoped to this entity's own status div.
function showSpecimenDetailStatus(message, type) {
    arSpecimenDetailStatus.textContent = message;
    arSpecimenDetailStatus.className = 'upload-status ' + type;
    arSpecimenDetailStatus.style.display = 'block';
    clearTimeout(window.arSpecimenDetailStatusTimeout);
    window.arSpecimenDetailStatusTimeout = setTimeout(() => {
        arSpecimenDetailStatus.style.display = 'none';
    }, 5000);
}

// ---------- DOCUMENT PICKER ----------
// Reuses the allDocuments cache (Cross-checks/Categories above) - already active-only, and
// already carries each Document's auth_roles, which the Role picker below needs.
function populateSpecimenDocumentSelect() {
    arSpecimenDocumentSelect.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.disabled = true;
    placeholder.selected = true;
    placeholder.textContent = allDocuments.length === 0 ? 'No documents exist yet' : 'Select a document';
    arSpecimenDocumentSelect.appendChild(placeholder);

    allDocuments.forEach(d => {
        const opt = document.createElement('option');
        opt.value = d.id;
        opt.textContent = d.name;
        arSpecimenDocumentSelect.appendChild(opt);
    });
}

// ---------- ROLE PICKER (dynamic, driven by the selected Document's auth_roles) ----------
// Mirrors renderCompareFieldDropdown()'s pattern above.
function renderSpecimenRoleOptions() {
    const docId = parseInt(arSpecimenDocumentSelect.value, 10);
    const doc = getDocumentById(docId);
    const roles = doc ? (doc.auth_roles || []) : [];
    const previousValue = arSpecimenRoleSelect.value;

    arSpecimenRoleSelect.innerHTML = '';

    if (roles.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.disabled = true;
        opt.selected = true;
        opt.textContent = doc ? 'This document has no authorisation roles configured' : 'Select a document first';
        arSpecimenRoleSelect.appendChild(opt);
        return;
    }

    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.disabled = true;
    placeholder.textContent = 'Select a role';
    arSpecimenRoleSelect.appendChild(placeholder);

    roles.forEach(role => {
        const opt = document.createElement('option');
        opt.value = role;
        opt.textContent = role;
        arSpecimenRoleSelect.appendChild(opt);
    });

    if (roles.includes(previousValue)) {
        arSpecimenRoleSelect.value = previousValue;
    } else {
        placeholder.selected = true;
    }
}

// ---------- LOAD & RENDER LIST ----------
async function loadSignatureSpecimens() {
    const clientId = getSelectedClientId();
    arSpecimenListPanelTitle.textContent = 'Signature Specimens — ' + (getSelectedClientName() || '');

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/signature-specimens?client_id=' + clientId);
        if (!res.ok) throw new Error('Failed to load signature specimens');
        allSpecimens = await res.json();
        renderSpecimenList(allSpecimens);
    } catch (error) {
        arSpecimenListBody.innerHTML = '';
        arSpecimenListEmptyMessage.textContent = '❌ Error loading signature specimens: ' + error.message;
        arSpecimenListEmptyMessage.style.display = 'block';
    }
}

function renderSpecimenList(specimens) {
    arSpecimenListBody.innerHTML = '';

    if (specimens.length === 0) {
        arSpecimenListEmptyMessage.textContent = 'No signature specimens found.';
        arSpecimenListEmptyMessage.style.display = 'block';
        return;
    }
    arSpecimenListEmptyMessage.style.display = 'none';

    specimens.forEach(s => {
        const row = document.createElement('div');
        row.className = 'ar-list-row';
        if (s.id === editingSpecimenId) row.classList.add('ar-list-row-selected');

        const nameSpan = document.createElement('span');
        nameSpan.className = 'ar-list-row-name';
        nameSpan.textContent = (s.document_name || ('Document #' + s.document_id)) +
            ' — ' + s.role_name + ': ' + s.expected_signatory_name;
        row.appendChild(nameSpan);

        row.addEventListener('click', () => selectSpecimen(s.id));
        arSpecimenListBody.appendChild(row);
    });
}

// ---------- SELECT / NEW / RESET DETAIL PANEL ----------
async function selectSpecimen(id) {
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/signature-specimens/' + id);
        if (!res.ok) throw new Error('Signature specimen not found');
        const s = await res.json();

        editingSpecimenId = s.id;
        arSpecimenEditingIdInput.value = s.id;
        arSpecimenDocumentSelect.value = s.document_id;
        renderSpecimenRoleOptions();
        arSpecimenRoleSelect.value = s.role_name;
        arSpecimenNameInput.value = s.expected_signatory_name;

        arSpecimenDeleteBtn.style.display = 'inline-block';

        arSpecimenDetailEmpty.style.display = 'none';
        arSpecimenDetailForm.style.display = 'block';

        // Re-render the list so the selected row highlight moves without a full re-fetch.
        loadSignatureSpecimens();
    } catch (error) {
        showSpecimenDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

function startNewSpecimen() {
    editingSpecimenId = null;
    arSpecimenEditingIdInput.value = '';
    arSpecimenDocumentSelect.value = '';
    renderSpecimenRoleOptions();
    arSpecimenNameInput.value = '';

    arSpecimenDeleteBtn.style.display = 'none';

    arSpecimenDetailEmpty.style.display = 'none';
    arSpecimenDetailForm.style.display = 'block';
    arSpecimenDocumentSelect.focus();
}

// ---------- SAVE ----------
async function saveSpecimen() {
    const clientId = getSelectedClientId();
    const documentId = arSpecimenDocumentSelect.value;
    const roleName = arSpecimenRoleSelect.value;
    const expectedName = arSpecimenNameInput.value.trim();

    if (!documentId) {
        showSpecimenDetailStatus('❌ A document is required.', 'error');
        return;
    }
    if (!roleName) {
        showSpecimenDetailStatus('❌ A role is required.', 'error');
        return;
    }
    if (!expectedName) {
        showSpecimenDetailStatus('❌ Expected signatory name is required.', 'error');
        return;
    }

    const payload = {
        client_id: parseInt(clientId, 10),
        document_id: parseInt(documentId, 10),
        role_name: roleName,
        expected_signatory_name: expectedName
    };

    try {
        let url = API_BASE + '/api/audit-rules/signature-specimens';
        let method = 'POST';
        if (editingSpecimenId) {
            url += '/' + editingSpecimenId;
            method = 'PUT';
        }

        const res = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to save signature specimen');

        showSpecimenDetailStatus('✅ ' + result.message, 'success');

        const savedId = editingSpecimenId || result.specimen_id;
        await loadSignatureSpecimens();
        await selectSpecimen(savedId);
    } catch (error) {
        showSpecimenDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

// ---------- DELETE (hard delete - no is_active/Restore for this entity) ----------
async function deleteSpecimen() {
    if (!editingSpecimenId) return;
    if (!confirm('Are you sure you want to permanently delete this signature specimen? This cannot be undone.')) return;

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/signature-specimens/' + editingSpecimenId, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to delete signature specimen');

        showSpecimenDetailStatus('✅ ' + result.message, 'success');
        // Unlike the soft-delete flows above, the row no longer exists - return the detail
        // panel to its empty state instead of re-selecting the now-deleted id.
        editingSpecimenId = null;
        arSpecimenDetailForm.style.display = 'none';
        arSpecimenDetailEmpty.style.display = 'block';
        await loadSignatureSpecimens();
    } catch (error) {
        showSpecimenDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

// ---------- WIRE UP EVENT LISTENERS ----------
arSpecimenAddNewBtn.addEventListener('click', startNewSpecimen);
arSpecimenDocumentSelect.addEventListener('change', renderSpecimenRoleOptions);
arSpecimenSaveBtn.addEventListener('click', saveSpecimen);

// =====================================================================================
// VOUCHER-LEVEL CHECKS (Generic Voucher-Level Checks, Phase 3 of 6)
// A 4th reusable entity type: audit_rule_voucher_checks. Ordered by sort_order (server-
// assigned), NOT alphabetical like Documents/Cross-checks/Categories. Soft-delete/restore
// like Documents (not hard-delete like Signature Specimens). Name-only (confirmed
// requirement) - no Authorisation Roles field; audit_rule_voucher_check_auth_roles is left
// in place but unread/unwritten by this entity going forward.
// Uses its own state (editingVoucherCheckId), distinct from Documents' editingId global,
// to avoid clobbering that section's state.
// =====================================================================================

// ---------- DOM REFS ----------
const arVoucherCheckListBody = document.getElementById('arVoucherCheckListBody');
const arVoucherCheckListEmptyMessage = document.getElementById('arVoucherCheckListEmptyMessage');
const arVoucherCheckShowInactiveCheckbox = document.getElementById('arVoucherCheckShowInactiveCheckbox');
const arVoucherCheckAddNewBtn = document.getElementById('arVoucherCheckAddNewBtn');
const arVoucherCheckReorderHint = document.getElementById('arVoucherCheckReorderHint');

const arVoucherCheckDetailEmpty = document.getElementById('arVoucherCheckDetailEmpty');
const arVoucherCheckDetailForm = document.getElementById('arVoucherCheckDetailForm');
const arVoucherCheckEditingIdInput = document.getElementById('arVoucherCheckEditingId');
const arVoucherCheckNameInput = document.getElementById('arVoucherCheckNameInput');
const arVoucherCheckDetailStatus = document.getElementById('arVoucherCheckDetailStatus');
const arVoucherCheckSaveBtn = document.getElementById('arVoucherCheckSaveBtn');
const arVoucherCheckDeleteBtn = document.getElementById('arVoucherCheckDeleteBtn');
const arVoucherCheckRestoreBtn = document.getElementById('arVoucherCheckRestoreBtn');

// ---------- STATE ----------
// null while creating a new Voucher-level check; set to its id while editing an existing one.
let editingVoucherCheckId = null;
// Sortable.js instance, created once (see initVoucherCheckSortable()) and toggled via
// .option('disabled', ...) rather than destroyed/recreated on every render.
let arVoucherCheckSortable = null;

// ---------- STATUS MESSAGE ----------
function showVoucherCheckDetailStatus(message, type) {
    arVoucherCheckDetailStatus.textContent = message;
    arVoucherCheckDetailStatus.className = 'upload-status ' + type;
    arVoucherCheckDetailStatus.style.display = 'block';
    clearTimeout(window.arVoucherCheckDetailStatusTimeout);
    window.arVoucherCheckDetailStatusTimeout = setTimeout(() => {
        arVoucherCheckDetailStatus.style.display = 'none';
    }, 5000);
}

// ---------- LOAD & RENDER LIST ----------
async function loadVoucherChecks() {
    initVoucherCheckSortable();
    const includeInactive = arVoucherCheckShowInactiveCheckbox.checked;
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/voucher-checks?include_inactive=' + includeInactive);
        if (!res.ok) throw new Error('Failed to load voucher-level checks');
        const checks = await res.json(); // already ORDER BY sort_order server-side -- do not re-sort here
        renderVoucherCheckList(checks, includeInactive);
    } catch (error) {
        arVoucherCheckListBody.innerHTML = '';
        arVoucherCheckListEmptyMessage.textContent = '❌ Error loading voucher-level checks: ' + error.message;
        arVoucherCheckListEmptyMessage.style.display = 'block';
    }
}

function renderVoucherCheckList(checks, includeInactive) {
    arVoucherCheckListBody.innerHTML = '';

    if (checks.length === 0) {
        arVoucherCheckListEmptyMessage.textContent = 'No voucher-level checks found.';
        arVoucherCheckListEmptyMessage.style.display = 'block';
    } else {
        arVoucherCheckListEmptyMessage.style.display = 'none';
        checks.forEach(check => {
            const row = document.createElement('div');
            row.className = 'ar-list-row' + (check.is_active ? '' : ' ar-list-row-inactive');
            row.dataset.id = check.id;
            if (check.id === editingVoucherCheckId) row.classList.add('ar-list-row-selected');

            const handle = document.createElement('span');
            handle.className = 'ar-drag-handle';
            handle.textContent = '::';
            handle.style.cursor = 'grab';
            handle.style.marginRight = '8px';
            handle.style.color = '#94a3b8';
            row.appendChild(handle);

            const nameSpan = document.createElement('span');
            nameSpan.className = 'ar-list-row-name';
            nameSpan.style.flex = '1';
            nameSpan.textContent = check.name;
            row.appendChild(nameSpan);

            if (!check.is_active) {
                const badge = document.createElement('span');
                badge.className = 'ar-list-row-badge';
                badge.textContent = 'Inactive';
                row.appendChild(badge);
            }

            // A plain click landing on the handle (no actual drag) must not trigger
            // selection -- only clicks elsewhere on the row select the check.
            row.addEventListener('click', (e) => {
                if (e.target.closest('.ar-drag-handle')) return;
                selectVoucherCheck(check.id);
            });
            arVoucherCheckListBody.appendChild(row);
        });
    }

    // Reorder is only safe when the full active-only set is shown, since the reorder
    // endpoint reassigns sort_order = enumerate(index) over exactly the ids sent --
    // mixing in hidden inactive rows (or omitting them) would corrupt/gap their sort_order.
    if (arVoucherCheckSortable) {
        arVoucherCheckSortable.option('disabled', includeInactive);
    }
    arVoucherCheckReorderHint.style.display = includeInactive ? 'block' : 'none';
}

// ---------- DRAG-TO-REORDER ----------
// Created once, lazily -- arVoucherCheckListBody (the container Sortable binds to) is
// never destroyed/recreated, only its children are replaced on each render, which is
// exactly what Sortable is designed to tolerate.
function initVoucherCheckSortable() {
    if (arVoucherCheckSortable) return;
    arVoucherCheckSortable = new Sortable(arVoucherCheckListBody, {
        handle: '.ar-drag-handle',
        animation: 150,
        ghostClass: 'sortable-ghost',
        disabled: arVoucherCheckShowInactiveCheckbox.checked,
        onEnd: async function () {
            const orderedIds = Array.from(arVoucherCheckListBody.children).map(row => parseInt(row.dataset.id, 10));
            try {
                const res = await fetch(API_BASE + '/api/audit-rules/voucher-checks/reorder', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(orderedIds) // bare array, not wrapped -- matches backend Body(...)
                });
                const result = await res.json();
                if (!res.ok) throw new Error(result.detail || result.message || 'Failed to reorder');
                showVoucherCheckDetailStatus('✅ Order updated.', 'success');
            } catch (error) {
                showVoucherCheckDetailStatus('❌ Error reordering: ' + error.message, 'error');
                // Re-fetch to restore the true server order after a failed persist, since
                // the DOM has already been reordered visually by Sortable itself.
                await loadVoucherChecks();
            }
        }
    });
}

// ---------- CHIP EDITOR (Authorisation Roles) ----------
// Reuses the existing generic renderChipList(container, items, onRemove) helper (defined
// above, shared across Documents/etc.) rather than duplicating it.
// ---------- SELECT / NEW ----------
async function selectVoucherCheck(id) {
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/voucher-checks/' + id);
        if (!res.ok) throw new Error('Voucher-level check not found');
        const check = await res.json();

        editingVoucherCheckId = check.id;
        arVoucherCheckEditingIdInput.value = check.id;
        arVoucherCheckNameInput.value = check.name;

        if (check.is_active) {
            arVoucherCheckDeleteBtn.style.display = 'inline-block';
            arVoucherCheckRestoreBtn.style.display = 'none';
        } else {
            arVoucherCheckDeleteBtn.style.display = 'none';
            arVoucherCheckRestoreBtn.style.display = 'inline-block';
        }

        arVoucherCheckDetailEmpty.style.display = 'none';
        arVoucherCheckDetailForm.style.display = 'block';

        loadVoucherChecks(); // refresh selected-row highlight, same pattern as Documents
    } catch (error) {
        showVoucherCheckDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

function startNewVoucherCheck() {
    editingVoucherCheckId = null;
    arVoucherCheckEditingIdInput.value = '';
    arVoucherCheckNameInput.value = '';

    arVoucherCheckDeleteBtn.style.display = 'none';
    arVoucherCheckRestoreBtn.style.display = 'none';

    arVoucherCheckDetailEmpty.style.display = 'none';
    arVoucherCheckDetailForm.style.display = 'block';
    arVoucherCheckNameInput.focus();
}

// ---------- SAVE / DELETE / RESTORE ----------
async function saveVoucherCheck() {
    const name = arVoucherCheckNameInput.value.trim();
    if (!name) {
        showVoucherCheckDetailStatus('⚠️ Name is required.', 'error');
        return;
    }

    const payload = { name: name };

    try {
        let url = API_BASE + '/api/audit-rules/voucher-checks';
        let method = 'POST';
        if (editingVoucherCheckId) {
            url += '/' + editingVoucherCheckId;
            method = 'PUT';
        }

        const res = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to save voucher-level check');

        showVoucherCheckDetailStatus('✅ ' + result.message, 'success');

        const savedId = editingVoucherCheckId || result.voucher_check_id;
        await loadVoucherChecks();
        await selectVoucherCheck(savedId);
    } catch (error) {
        showVoucherCheckDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

async function deleteVoucherCheck() {
    if (!editingVoucherCheckId) return;
    if (!confirm('Are you sure you want to delete "' + arVoucherCheckNameInput.value + '"? This will soft-delete it.')) return;

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/voucher-checks/' + editingVoucherCheckId, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to delete voucher-level check');

        showVoucherCheckDetailStatus('✅ ' + result.message, 'success');
        await loadVoucherChecks();
        await selectVoucherCheck(editingVoucherCheckId);
    } catch (error) {
        showVoucherCheckDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

async function restoreVoucherCheck() {
    if (!editingVoucherCheckId) return;

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/voucher-checks/' + editingVoucherCheckId + '/restore', { method: 'POST' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to restore voucher-level check');

        showVoucherCheckDetailStatus('✅ ' + result.message, 'success');
        await loadVoucherChecks();
        await selectVoucherCheck(editingVoucherCheckId);
    } catch (error) {
        showVoucherCheckDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

// ---------- WIRE UP EVENT LISTENERS ----------
arVoucherCheckAddNewBtn.addEventListener('click', startNewVoucherCheck);
arVoucherCheckShowInactiveCheckbox.addEventListener('change', loadVoucherChecks);

arVoucherCheckSaveBtn.addEventListener('click', saveVoucherCheck);
arVoucherCheckDeleteBtn.addEventListener('click', deleteVoucherCheck);
arVoucherCheckRestoreBtn.addEventListener('click', restoreVoucherCheck);
arSpecimenDeleteBtn.addEventListener('click', deleteSpecimen);
