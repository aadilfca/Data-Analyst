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

// Cross-checks/Categories rail items are inert placeholders this phase (no listener),
// see audit_rules.html — Documents' rail button needs no listener either since it's
// already .active by default and there's nothing else to switch to yet.

// ---------- INIT ----------
window.onload = function() {
    loadDocuments();
};
