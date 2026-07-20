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
// 'documents' or 'cross-checks' — which entity type's list/detail sections are visible.
let currentEntityType = 'documents';

const arRailDocuments = document.getElementById('arRailDocuments');
const arRailCrossChecks = document.getElementById('arRailCrossChecks');
const arDocumentsListSection = document.getElementById('arDocumentsListSection');
const arDocumentsDetailSection = document.getElementById('arDocumentsDetailSection');
const arCrossChecksListSection = document.getElementById('arCrossChecksListSection');
const arCrossChecksDetailSection = document.getElementById('arCrossChecksDetailSection');

function switchEntityType(type) {
    currentEntityType = type;

    arRailDocuments.classList.toggle('active', type === 'documents');
    arRailCrossChecks.classList.toggle('active', type === 'cross-checks');

    arDocumentsListSection.style.display = (type === 'documents') ? 'flex' : 'none';
    arDocumentsDetailSection.style.display = (type === 'documents') ? 'block' : 'none';
    arCrossChecksListSection.style.display = (type === 'cross-checks') ? 'flex' : 'none';
    arCrossChecksDetailSection.style.display = (type === 'cross-checks') ? 'block' : 'none';

    if (type === 'cross-checks') {
        // Refresh the Documents cache every time this view is opened, so the "add
        // document" picker and field-eligibility logic reflect any Documents-side
        // edits made since the last visit.
        loadAllDocumentsForCrossChecks().then(loadCrossChecks);
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
const arComparisonTypeSelect = document.getElementById('arComparisonTypeSelect');
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
}

function removeDocumentFromCrossCheck(docId) {
    crossCheckDocIds = crossCheckDocIds.filter(id => id !== docId);
    renderCrossCheckDocsList();
    renderCompareFieldDropdown();
}

function moveDocUp(index) {
    if (index <= 0) return;
    [crossCheckDocIds[index - 1], crossCheckDocIds[index]] = [crossCheckDocIds[index], crossCheckDocIds[index - 1]];
    renderCrossCheckDocsList();
    renderCompareFieldDropdown();
}

function moveDocDown(index) {
    if (index >= crossCheckDocIds.length - 1) return;
    [crossCheckDocIds[index + 1], crossCheckDocIds[index]] = [crossCheckDocIds[index], crossCheckDocIds[index + 1]];
    renderCrossCheckDocsList();
    renderCompareFieldDropdown();
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

function renderCompareFieldDropdown() {
    const eligibleFields = computeEligibleFields();
    const previousValue = arCompareFieldSelect.value;

    arCompareFieldSelect.innerHTML = '';

    if (eligibleFields.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.disabled = true;
        opt.selected = true;
        opt.textContent = 'Add a document first';
        arCompareFieldSelect.appendChild(opt);
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
    } else {
        placeholder.selected = true;
        if (previousValue) {
            // The field the user had chosen is no longer eligible after a document
            // add/remove — surface this rather than silently clearing it.
            showCrossCheckDetailStatus(
                '⚠️ "' + previousValue + '" is no longer available on every selected document — please choose a field again.',
                'error'
            );
        }
    }
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
        renderCompareFieldDropdown();
        arCompareFieldSelect.value = cc.compare_field;
        arComparisonTypeSelect.value = cc.comparison_type;

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
    renderCompareFieldDropdown();
    arComparisonTypeSelect.value = '';

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
        document_ids: crossCheckDocIds
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

arCrossCheckAddNewBtn.addEventListener('click', startNewCrossCheck);
arCrossCheckShowInactiveCheckbox.addEventListener('change', loadCrossChecks);

arAddDocumentBtn.addEventListener('click', addDocumentToCrossCheck);

arCrossCheckSaveBtn.addEventListener('click', saveCrossCheck);
arCrossCheckDeleteBtn.addEventListener('click', deleteCrossCheck);
arCrossCheckRestoreBtn.addEventListener('click', restoreCrossCheck);

// ---------- INIT ----------
window.onload = function() {
    loadDocuments();
};
