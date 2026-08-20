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
// Physical Bill evidence type (Phase 4 of 8): the document_type as it stands ON THE SERVER for the
// Document currently open. Kept separate from arDocumentTypeSelect.value, which changes the moment
// the user touches the picker - so the picker can't serve as its own "what was it before?" baseline
// for the destructive-switch confirm in saveDocument(). null while creating a new Document.
let savedDocumentType = null;

// Two independent flat string arrays edited by the chip editors below, sent as
// auth_roles/cross_check_fields on save. Reset on both selectDocument() and
// startNewDocument().
let rolesState = [];
let fieldsState = [];

// Required Authority (new, confirmed requirement): role_name -> required authority text,
// only ever relevant for an "Approved by"-type role (see isApprovalRole()). Additive
// alongside rolesState/auth_roles - kept as a parallel map rather than changing rolesState's
// shape, so nothing else reading auth_roles as plain strings needs to change.
let requiredAuthorityState = {};

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
// Physical Bill evidence type (Phase 4 of 8): the type picker plus the three blocks it hides.
const arDocumentTypeSelect = document.getElementById('arDocumentTypeSelect');
const arDocumentTypeHint = document.getElementById('arDocumentTypeHint');
const arDocTemplateField = document.getElementById('arDocTemplateField');
const arDocFieldsField = document.getElementById('arDocFieldsField');
const arDocCrossChecksSectionTitle = document.getElementById('arDocCrossChecksSectionTitle');
const arRolesChipList = document.getElementById('arRolesChipList');
const arRolesInput = document.getElementById('arRolesInput');
const arRolesAddBtn = document.getElementById('arRolesAddBtn');
const arFieldsChipList = document.getElementById('arFieldsChipList');
const arFieldsSelect = document.getElementById('arFieldsSelect');
const arFieldsAddBtn = document.getElementById('arFieldsAddBtn');
const arDocSignatureSpecimensHint = document.getElementById('arDocSignatureSpecimensHint');
const arDocSignatureSpecimensContainer = document.getElementById('arDocSignatureSpecimensContainer');
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

// Matches an "Approved by"-type role by substring, not an exact string - mirrors
// routers/audit_rules.py's is_approval_role() (which itself mirrors verification.py's
// existing 'approv' in role_name.lower() convention). Kept defined but unused (non-destructive
// convention) now that renderRoleChips() no longer renders a per-role Required Authority input
// (confirmed fix) — requiredAuthorityState is still populated from/sent to the backend
// unchanged, just no longer editable from this section's UI.
function isApprovalRole(roleName) {
    return (roleName || '').toLowerCase().includes('approv');
}

// Plain chip render (Required Authority input removed, confirmed fix — it was a stray field
// duplicating the Signature Specimens section's own separate "Authority (optional)" input,
// which is untouched). No longer needs a dedicated implementation now that there's nothing
// beyond a chip to render per role — reuses the shared renderChipList() other chip lists use.
function renderRoleChips() {
    renderChipList(arRolesChipList, rolesState, removeRole);
}

// Cross-check-eligible Fields (ID-based Cross-check-eligible Fields, Phase 2 of 4): unlike
// the plain renderChipList() above, each chip is checked against the Document's *current*
// template's real columns (arTemplateColumnsCache) and rendered flagged if it doesn't
// match — this mirrors, client-side, the same case-insensitive/trimmed matching
// routers/audit_rules.py's resolve_field_name_to_master_column_id() does on save, so a
// flagged chip here is exactly one that would save with a NULL master_column_id.
function renderFieldChips() {
    arFieldsChipList.innerHTML = '';
    if (fieldsState.length === 0) {
        const empty = document.createElement('span');
        empty.className = 'ar-chip-empty';
        empty.textContent = 'None added.';
        arFieldsChipList.appendChild(empty);
        return;
    }

    const templateId = arTemplateSelect.value;
    const templateColumns = templateId ? (arTemplateColumnsCache[templateId] || []) : [];

    fieldsState.forEach(item => {
        const isResolved = templateColumns.some(c => c.column_name.toLowerCase() === item.toLowerCase());

        const chip = document.createElement('span');
        chip.className = 'ar-chip' + (isResolved ? '' : ' ar-chip-warning');
        if (!isResolved) {
            chip.title = 'Doesn\'t match the linked template\'s columns — remove and re-add.';
        }

        const label = document.createElement('span');
        label.textContent = (isResolved ? '' : '⚠ ') + item;
        chip.appendChild(label);

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'ar-chip-remove';
        removeBtn.textContent = '✕';
        removeBtn.title = 'Remove';
        removeBtn.addEventListener('click', () => removeField(item));
        chip.appendChild(removeBtn);

        arFieldsChipList.appendChild(chip);
    });
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
    delete requiredAuthorityState[value];
    renderRoleChips();
}

function addField() {
    const value = arFieldsSelect.value;
    if (!value) return;
    if (fieldsState.some(f => f.toLowerCase() === value.toLowerCase())) {
        return;
    }
    fieldsState.push(value);
    renderFieldChips();
    renderFieldsDropdown();
}
function removeField(value) {
    fieldsState = fieldsState.filter(f => f !== value);
    renderFieldChips();
    renderFieldsDropdown();
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

// ---------- CROSS-CHECK-ELIGIBLE FIELDS DROPDOWN (ID-based Cross-check-eligible Fields,
// Phase 2 of 4) ----------
// Fetches and caches a template's master columns (id + column_name), mirroring
// verification.js's fetchTemplateColumns() for its Linked Evidence table, but keeps
// `id` (the master_column_id) in the cached shape since it's directly relevant to this
// feature's naming, even though the actual save payload is still by column_name text —
// see saveDocument(), unchanged this phase.
let arTemplateColumnsCache = {};
async function fetchArTemplateColumns(templateId) {
    if (!templateId) return [];
    if (Object.prototype.hasOwnProperty.call(arTemplateColumnsCache, templateId)) {
        return arTemplateColumnsCache[templateId];
    }
    try {
        const res = await fetch(API_BASE + '/api/templates/' + templateId);
        if (!res.ok) { arTemplateColumnsCache[templateId] = []; return []; }
        const data = await res.json();
        const columns = (data.columns || []).map(c => ({ id: c.id, column_name: c.column_name }));
        arTemplateColumnsCache[templateId] = columns;
        return columns;
    } catch (error) {
        console.error('Failed to load template columns for Cross-check-eligible Fields:', error);
        arTemplateColumnsCache[templateId] = [];
        return [];
    }
}

// Populates arFieldsSelect from the Document's currently-selected template, excluding
// columns already present in fieldsState (case-insensitive) — same dedup rule addField()
// already enforced for free text. Called on template change and whenever fieldsState
// changes (add/remove), plus on selectDocument()/startNewDocument().
async function renderFieldsDropdown() {
    const templateId = arTemplateSelect.value;

    if (!templateId) {
        arFieldsSelect.innerHTML = '<option value="">Select a template first</option>';
        arFieldsSelect.disabled = true;
        arFieldsAddBtn.disabled = true;
        renderFieldChips();
        return;
    }

    const columns = await fetchArTemplateColumns(templateId);
    // Re-render chips now that arTemplateColumnsCache[templateId] is populated, so the
    // resolved/flagged state reflects real data rather than a transient empty cache.
    renderFieldChips();

    const eligible = columns.filter(c => !fieldsState.some(f => f.toLowerCase() === c.column_name.toLowerCase()));

    if (eligible.length === 0) {
        arFieldsSelect.innerHTML = '<option value="">No more fields available</option>';
        arFieldsSelect.disabled = true;
        arFieldsAddBtn.disabled = true;
        return;
    }

    arFieldsSelect.innerHTML = '<option value="">-- Select a field --</option>' +
        eligible.map(c => `<option value="${c.column_name}">${c.column_name}</option>`).join('');
    arFieldsSelect.disabled = false;
    arFieldsAddBtn.disabled = false;
}

// ---------- DOCUMENT TYPE (Physical Bill evidence type, Phase 4 of 8) ----------
function updateDocumentTypeVisibility() {
    // A Physical Bill has no linked Data Bank template and no configurable field list - its fields
    // (Bill No / Date / Amount / Vendor name) are fixed by type. The Cross-Checks section heading
    // is hidden alongside its only field so the form doesn't show an empty section header.
    const isPhysicalBill = arDocumentTypeSelect.value === 'physical_bill';
    const display = isPhysicalBill ? 'none' : '';

    arDocTemplateField.style.display = display;
    arDocFieldsField.style.display = display;
    arDocCrossChecksSectionTitle.style.display = display;
    arDocumentTypeHint.style.display = isPhysicalBill ? 'block' : 'none';
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
        // Physical Bill evidence type (Phase 4 of 8): reflect the stored type and record it as the
        // baseline for saveDocument()'s destructive-switch confirm.
        savedDocumentType = doc.document_type || 'linked_template';
        arDocumentTypeSelect.value = savedDocumentType;
        updateDocumentTypeVisibility();
        rolesState = (doc.auth_roles || []).slice();
        requiredAuthorityState = { ...(doc.auth_role_authorities || {}) };
        fieldsState = (doc.cross_check_fields || []).slice();
        renderRoleChips();
        renderFieldChips();
        renderFieldsDropdown();

        arUsedByRow.style.display = 'block';
        arUsedByCount.textContent = doc.used_by_categories_count;

        arDocSignatureSpecimensHint.style.display = 'none';
        loadDocumentSignatureSpecimens(doc);

        if (doc.is_active) {
            arDeleteBtn.style.display = 'inline-block';
            arRestoreBtn.style.display = 'none';
        } else {
            arDeleteBtn.style.display = 'none';
            arRestoreBtn.style.display = 'inline-block';
        }

        arDetailEmpty.style.display = 'none';
        arDetailForm.style.display = 'block';

        updateAuditRulesUrl();

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
    // Forced choice: a brand-new Document starts with no type selected and can't be saved until
    // one is picked (see saveDocument()). No saved baseline exists yet, so no switch confirm.
    savedDocumentType = null;
    arDocumentTypeSelect.value = '';
    updateDocumentTypeVisibility();
    rolesState = [];
    requiredAuthorityState = {};
    fieldsState = [];
    renderRoleChips();
    renderFieldChips();
    renderFieldsDropdown();

    arUsedByRow.style.display = 'none';
    arDeleteBtn.style.display = 'none';
    arRestoreBtn.style.display = 'none';

    currentDocumentSpecimens = [];
    currentSpecimensDoc = null;
    arDocSignatureSpecimensContainer.innerHTML = '';
    arDocSignatureSpecimensHint.style.display = 'block';

    arDetailEmpty.style.display = 'none';
    arDetailForm.style.display = 'block';
    arNameInput.focus();

    updateAuditRulesUrl();
}

// ---------- SAVE ----------
async function saveDocument() {
    const name = arNameInput.value.trim();
    if (!name) {
        showDetailStatus('❌ Name is required.', 'error');
        return;
    }

    // Physical Bill evidence type (Phase 4 of 8): forced choice, no default. The backend still
    // defaults a missing document_type to 'linked_template' (resolve_document_type(),
    // routers/audit_rules.py) - this UI is deliberately stricter, so the choice is always explicit.
    const documentType = arDocumentTypeSelect.value;
    if (!documentType) {
        showDetailStatus('❌ Document type is required.', 'error');
        return;
    }

    const isPhysicalBill = documentType === 'physical_bill';

    // Destructive-switch guard (the confirm step Phase 2 deferred to this phase): turning an
    // existing linked-template Document into a Physical Bill clears its template and its
    // cross-check-eligible fields server-side. Only asks when there's actually something to lose.
    // A native confirm(), matching this codebase's convention for a single flat destructive action
    // (Assign Categories' force-apply, Clear Saved Mapping) - custom modals here are reserved for
    // multi-step flows.
    if (editingId && savedDocumentType === 'linked_template' && isPhysicalBill) {
        const losing = [];
        if (arTemplateSelect.value) {
            const templateName = arTemplateSelect.options[arTemplateSelect.selectedIndex].text;
            losing.push('its linked template (' + templateName + ')');
        }
        if (fieldsState.length) {
            losing.push('its cross-check-eligible field(s): ' + fieldsState.join(', '));
        }
        if (losing.length) {
            const proceed = confirm(
                'Switching "' + name + '" to Physical Bill will permanently clear ' + losing.join(' and ') + '.\n\n' +
                'A Physical Bill has fixed fields (Bill No, Date, Amount, Vendor name), so it cannot keep either. ' +
                'Any Cross-check comparing a field from this document will stop validating.\n\n' +
                'Continue?'
            );
            if (!proceed) return;
        }
    }

    const payload = {
        name: name,
        kb_note: arKbNoteInput.value.trim() || null,
        // Send what will actually be stored rather than whatever the now-hidden controls still
        // hold. The backend forces both empty for a physical_bill anyway
        // (apply_document_type_constraints()), but sending the real outcome keeps the UI from
        // briefly implying a template/field survived the switch.
        template_id: (!isPhysicalBill && arTemplateSelect.value) ? parseInt(arTemplateSelect.value) : null,
        risk_rating: arRiskRatingSelect.value || null,
        auth_roles: rolesState,
        auth_role_authorities: requiredAuthorityState,
        cross_check_fields: isPhysicalBill ? [] : fieldsState,
        document_type: documentType
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

// ---------- SIGNATURE SPECIMENS (Soft-delete conversion, Phase 4 of 4, FINAL) ----------
// Moved in from the old standalone "Signature Specimens" rail entity — now a per-Role,
// multi-person list embedded directly in the selected Document's own detail view, with an
// active/inactive toggle (soft-delete/restore) plus a separate low-visibility hard-delete
// for genuine data-entry corrections. Backend (Phases 1-2) already fully supports this:
// GET .../signature-specimens accepts document_id + include_inactive, and the uniqueness
// constraint was relaxed so multiple concurrently-active people per role are allowed.

// Current client's specimens for the currently-selected Document (include_inactive=true —
// inactive rows stay visible, greyed, as a historical record, never hidden).
let currentDocumentSpecimens = [];
// The Document object last passed to loadDocumentSignatureSpecimens() — its own
// server-persisted auth_roles (not the live-editing rolesState chip array, which can be
// mid-edit/unsaved) is what drives the per-Role grouping, so add/toggle/hard-delete reloads
// reuse this instead of re-fetching the Document or trusting rolesState.
let currentSpecimensDoc = null;

async function loadDocumentSignatureSpecimens(doc) {
    currentSpecimensDoc = doc;
    try {
        const res = await fetch(
            API_BASE + '/api/audit-rules/signature-specimens?client_id=' + getSelectedClientId() +
            '&document_id=' + doc.id + '&include_inactive=true'
        );
        if (!res.ok) throw new Error('Failed to load signature specimens');
        currentDocumentSpecimens = await res.json();
        renderDocumentSignatureSpecimensSection(doc);
    } catch (error) {
        arDocSignatureSpecimensContainer.innerHTML =
            '<div class="ar-list-empty">❌ Error loading signature specimens: ' + error.message + '</div>';
    }
}

function renderDocumentSignatureSpecimensSection(doc) {
    arDocSignatureSpecimensContainer.innerHTML = '';
    const roles = doc.auth_roles || [];
    const rolesSeen = new Set(roles);

    roles.forEach(role => {
        const group = buildSpecimenRoleGroup(doc, role, currentDocumentSpecimens.filter(s => s.role_name === role), false);
        arDocSignatureSpecimensContainer.appendChild(group);
    });

    // Specimens whose role no longer matches any of this Document's current auth_roles (the
    // role was since removed/renamed) — kept visible under "(former role)" rather than
    // silently hidden, per the confirmed "historical record" requirement. No add-row here,
    // since the role no longer exists to add a new person against.
    const otherRoles = [...new Set(currentDocumentSpecimens.filter(s => !rolesSeen.has(s.role_name)).map(s => s.role_name))];
    otherRoles.forEach(role => {
        const group = buildSpecimenRoleGroup(doc, role, currentDocumentSpecimens.filter(s => s.role_name === role), true);
        arDocSignatureSpecimensContainer.appendChild(group);
    });

    if (roles.length === 0 && otherRoles.length === 0) {
        const empty = document.createElement('span');
        empty.className = 'ar-chip-empty';
        empty.textContent = 'Add an Authorisation Role above to record signature specimens for it.';
        arDocSignatureSpecimensContainer.appendChild(empty);
    }
}

function buildSpecimenRoleGroup(doc, role, specimens, isFormerRole) {
    const group = document.createElement('div');
    group.className = 'ar-doc-specimens-role-group';

    const title = document.createElement('div');
    title.className = 'ar-doc-specimens-role-title';
    title.textContent = role + (isFormerRole ? ' (former role)' : '');
    group.appendChild(title);

    // Active people first, then inactive — inactive ones stay visible (greyed), never hidden.
    specimens.slice()
        .sort((a, b) => (b.is_active ? 1 : 0) - (a.is_active ? 1 : 0))
        .forEach(s => group.appendChild(buildSpecimenPersonRow(s)));

    if (specimens.length === 0) {
        const empty = document.createElement('span');
        empty.className = 'ar-chip-empty';
        empty.textContent = 'No signatories recorded.';
        group.appendChild(empty);
    }

    if (!isFormerRole) {
        group.appendChild(buildSpecimenAddRow(doc, role));
    }

    return group;
}

function buildSpecimenPersonRow(s) {
    const row = document.createElement('div');
    row.className = 'ar-doc-specimens-person-row' + (s.is_active ? '' : ' inactive');

    const nameSpan = document.createElement('span');
    nameSpan.className = 'ar-doc-specimens-person-name';
    nameSpan.textContent = s.expected_signatory_name;
    row.appendChild(nameSpan);

    if (s.authority) {
        const authoritySpan = document.createElement('span');
        authoritySpan.className = 'ar-doc-specimens-person-authority';
        authoritySpan.textContent = s.authority;
        row.appendChild(authoritySpan);
    }

    const statusSpan = document.createElement('span');
    statusSpan.className = 'ar-doc-specimens-person-status';
    statusSpan.textContent = s.is_active ? 'Active' : 'Inactive';
    row.appendChild(statusSpan);

    const toggleBtn = document.createElement('button');
    toggleBtn.type = 'button';
    toggleBtn.className = 'ar-btn-chip-add';
    toggleBtn.textContent = s.is_active ? 'Deactivate' : 'Reactivate';
    toggleBtn.addEventListener('click', () => toggleSignatureSpecimenActive(s.id, !s.is_active));
    row.appendChild(toggleBtn);

    const hardDeleteLink = document.createElement('span');
    hardDeleteLink.className = 'ar-doc-specimens-hard-delete';
    hardDeleteLink.textContent = '🗑';
    hardDeleteLink.title = 'Permanently delete — data-entry correction only';
    hardDeleteLink.addEventListener('click', () => hardDeleteSignatureSpecimen(s.id, s.expected_signatory_name, s.role_name));
    row.appendChild(hardDeleteLink);

    return row;
}

function buildSpecimenAddRow(doc, role) {
    const row = document.createElement('div');
    row.className = 'ar-doc-specimens-add-row';

    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = '+ Add person for ' + role;
    input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); addSignatureSpecimenPerson(doc.id, role, input, authorityInput); }
    });

    const authorityInput = document.createElement('input');
    authorityInput.type = 'text';
    authorityInput.placeholder = 'Authority (optional)';
    authorityInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); addSignatureSpecimenPerson(doc.id, role, input, authorityInput); }
    });

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ar-btn-chip-add';
    btn.textContent = 'Add';
    btn.addEventListener('click', () => addSignatureSpecimenPerson(doc.id, role, input, authorityInput));

    row.appendChild(input);
    row.appendChild(authorityInput);
    row.appendChild(btn);
    return row;
}

async function addSignatureSpecimenPerson(documentId, roleName, inputEl, authorityInputEl) {
    const name = inputEl.value.trim();
    if (!name) return;
    const authority = authorityInputEl ? authorityInputEl.value.trim() : '';

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/signature-specimens', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                client_id: parseInt(getSelectedClientId(), 10),
                document_id: documentId,
                role_name: roleName,
                expected_signatory_name: name,
                authority: authority || null
            })
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to add signature specimen');

        showDetailStatus('✅ ' + result.message, 'success');
        inputEl.value = '';
        if (authorityInputEl) authorityInputEl.value = '';
        if (currentSpecimensDoc) await loadDocumentSignatureSpecimens(currentSpecimensDoc);
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

async function toggleSignatureSpecimenActive(specimenId, activate) {
    try {
        const url = API_BASE + '/api/audit-rules/signature-specimens/' + specimenId + (activate ? '/restore' : '');
        const res = await fetch(url, { method: activate ? 'POST' : 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to update signature specimen');

        showDetailStatus('✅ ' + result.message, 'success');
        if (currentSpecimensDoc) await loadDocumentSignatureSpecimens(currentSpecimensDoc);
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

async function hardDeleteSignatureSpecimen(specimenId, name, role) {
    if (!confirm('Permanently delete the signature specimen for "' + name + '" (' + role + ')? This cannot be undone — use Deactivate instead for a normal personnel change.')) return;

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/signature-specimens/' + specimenId + '/hard-delete', { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to permanently delete signature specimen');

        showDetailStatus('✅ ' + result.message, 'success');
        if (currentSpecimensDoc) await loadDocumentSignatureSpecimens(currentSpecimensDoc);
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

// Switching the linked Template mid-edit re-scopes the eligible-fields dropdown and
// re-flags any already-added field against the newly-selected template's columns
// (ID-based Cross-check-eligible Fields, Phase 2 of 4) — existing fields are never
// auto-removed on a template switch, only re-flagged.
arTemplateSelect.addEventListener('change', renderFieldsDropdown);
// Physical Bill evidence type (Phase 4 of 8): show/hide Template + Cross-check fields live as the
// type changes, so the form always reflects what the chosen type can actually carry.
arDocumentTypeSelect.addEventListener('change', updateDocumentTypeVisibility);

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
// 'documents', 'cross-checks', 'categories', or 'voucher-checks' — which entity type's
// list/detail sections are visible. Signature Specimens is no longer a switchable top-level
// entity (moved into Documents detail, see the SIGNATURE SPECIMENS section below Documents'
// own SAVE logic).
let currentEntityType = 'documents';

const arRailDocuments = document.getElementById('arRailDocuments');
const arRailCrossChecks = document.getElementById('arRailCrossChecks');
const arRailCategories = document.getElementById('arRailCategories');
const arDocumentsListSection = document.getElementById('arDocumentsListSection');
const arDocumentsDetailSection = document.getElementById('arDocumentsDetailSection');
const arCrossChecksListSection = document.getElementById('arCrossChecksListSection');
const arCrossChecksDetailSection = document.getElementById('arCrossChecksDetailSection');
const arCategoriesListSection = document.getElementById('arCategoriesListSection');
const arCategoriesDetailSection = document.getElementById('arCategoriesDetailSection');
const arRailVoucherChecks = document.getElementById('arRailVoucherChecks');
const arVoucherChecksListSection = document.getElementById('arVoucherChecksListSection');
const arVoucherChecksDetailSection = document.getElementById('arVoucherChecksDetailSection');

function switchEntityType(type) {
    currentEntityType = type;

    arRailDocuments.classList.toggle('active', type === 'documents');
    arRailCrossChecks.classList.toggle('active', type === 'cross-checks');
    arRailCategories.classList.toggle('active', type === 'categories');
    arRailVoucherChecks.classList.toggle('active', type === 'voucher-checks');

    arDocumentsListSection.style.display = (type === 'documents') ? 'flex' : 'none';
    arDocumentsDetailSection.style.display = (type === 'documents') ? 'block' : 'none';
    arCrossChecksListSection.style.display = (type === 'cross-checks') ? 'flex' : 'none';
    arCrossChecksDetailSection.style.display = (type === 'cross-checks') ? 'block' : 'none';
    arCategoriesListSection.style.display = (type === 'categories') ? 'flex' : 'none';
    arCategoriesDetailSection.style.display = (type === 'categories') ? 'block' : 'none';
    arVoucherChecksListSection.style.display = (type === 'voucher-checks') ? 'flex' : 'none';
    arVoucherChecksDetailSection.style.display = (type === 'voucher-checks') ? 'block' : 'none';

    // URL state persistence (confirmed fix): reflect the newly-active entity type (and
    // whatever item, if any, was last selected within it — editingCrossCheckId etc. are
    // untouched by a plain rail switch, so this naturally restores the right selection too).
    updateAuditRulesUrl();
    // Global navbar Back icon: records this rail switch as a chronological history entry — covers
    // both a real switch and window.onload's own URL-restore call, the latter a no-op via
    // pushInPageNavEntry()'s own dedup since the page's own load-time push already recorded this
    // exact URL.
    if (typeof window.pushInPageNavEntry === 'function') window.pushInPageNavEntry(null, type);

    if (type === 'cross-checks') {
        // Refresh the Documents cache every time this view is opened, so the "add
        // document" picker and field-eligibility logic reflect any Documents-side
        // edits made since the last visit. Returned (not just fired) so the URL-restore
        // path in window.onload can await these caches before selecting a specific item.
        return loadAllDocumentsForCrossChecks().then(loadCrossChecks);
    } else if (type === 'categories') {
        // Refresh all three caches every time this view is opened, so the Documents/
        // Cross-checks pickers, the eligibility check, and the rule-count badge (Phase 3 of 3)
        // all reflect the latest state. loadVoucherChecks() is chained here (not fired in
        // parallel/uncoordinated) so allVoucherChecksCache is populated before loadCategories()
        // renders the list -- otherwise the badge would race an independent fetch and could
        // read a stale/empty cache on first entry into this view. Voucher-level Checks is no
        // longer embedded in this view's own detail panel (restored to its own rail tab), but
        // this call stays -- it's what keeps the rule-count badge's voucher-check count
        // accurate on first entry, independent of whether the Voucher-level Checks tab has
        // been visited yet this session.
        return Promise.all([
            loadAllDocumentsForCrossChecks(),
            loadAllCrossChecksForCategories(),
            loadVoucherChecks()
        ]).then(loadCategories);
    } else if (type === 'voucher-checks') {
        return loadVoucherChecks();
    }
    return Promise.resolve();
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
        const noneHaveAnyFields = crossCheckDocIds.every(
            docId => ((getDocumentById(docId) || {}).cross_check_fields || []).length === 0
        );
        opt.textContent = (crossCheckDocIds.length === 0)
            ? 'Add a document first'
            : noneHaveAnyFields
                ? 'No eligible fields — add them on the Document'
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

        updateAuditRulesUrl();

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

    updateAuditRulesUrl();
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
// Physical Bill evidence type (Phase 4 of 8): forced choice, no default - see saveCategory().
const arCategoryEvidenceTypeSelect = document.getElementById('arCategoryEvidenceTypeSelect');
const arCategoryDocsList = document.getElementById('arCategoryDocsList');
const arCategoryAddDocumentSelect = document.getElementById('arCategoryAddDocumentSelect');
const arCategoryAddDocumentBtn = document.getElementById('arCategoryAddDocumentBtn');
const arCategoryCrossChecksChipList = document.getElementById('arCategoryCrossChecksChipList');
const arCategoryAddCrossCheckSelect = document.getElementById('arCategoryAddCrossCheckSelect');
const arCategoryAddCrossCheckBtn = document.getElementById('arCategoryAddCrossCheckBtn');
const arCategoryCrossCheckChainPreview = document.getElementById('arCategoryCrossCheckChainPreview');

// ---------- CATEGORY DETAIL: VALIDATION RULES SECTION SHELL (Phase 1 of 3) ----------
// Groups the Cross-checks field + its Document Chains preview into one visually distinct
// "Validation Rules" box. Per the confirmed scope, static/audit_rules.html is not touched —
// the Cross-checks chip list/Document Chains preview stay exactly as declared there; this
// function reparents those existing nodes (moving them, not cloning) into a new wrapper
// built here, so every id/listener/render function that already targets them
// (renderCategoryCrossCheckChips(), renderCategoryCrossCheckChainPreview(), etc.) keeps
// working untouched. Voucher-level Checks was briefly embedded here too (a since-reverted
// change) — it's back to being its own top-level rail entity (see the VOUCHER-LEVEL CHECKS
// section further down), so this box now only ever has one sub-section.
function buildCategoryValidationRulesSection() {
    const crossChecksField = arCategoryCrossChecksChipList.closest('.ar-field');
    const chainPreviewField = arCategoryCrossCheckChainPreview.closest('.ar-field');
    if (!crossChecksField || !chainPreviewField) return;

    const section = document.createElement('div');
    section.className = 'ar-validation-rules-section';
    section.id = 'arCategoryValidationRulesSection';

    const header = document.createElement('div');
    header.className = 'ar-validation-rules-header';
    header.textContent = '⚠️ Validation Rules';
    section.appendChild(header);

    // Cleared Requirements summary (new, confirmed requirement, Phase 2 of 3) — read-only,
    // no manual toggles, sits first/above the Cross-checks subsection per the confirmed scope.
    const clearedReqSubsection = document.createElement('div');
    clearedReqSubsection.className = 'ar-validation-subsection';
    clearedReqSubsection.id = 'arValidationSubsectionClearedRequirements';
    const clearedReqTitle = document.createElement('div');
    clearedReqTitle.className = 'ar-validation-subsection-title';
    clearedReqTitle.textContent = 'What Cleared Requires';
    clearedReqSubsection.appendChild(clearedReqTitle);
    const clearedReqList = document.createElement('div');
    clearedReqList.id = 'arCategoryClearedRequirementsList';
    clearedReqList.className = 'ar-cleared-requirements-list';
    clearedReqSubsection.appendChild(clearedReqList);
    section.appendChild(clearedReqSubsection);

    const crossChecksSubsection = document.createElement('div');
    crossChecksSubsection.className = 'ar-validation-subsection';
    crossChecksSubsection.id = 'arValidationSubsectionCrossChecks';
    const crossChecksTitle = document.createElement('div');
    crossChecksTitle.className = 'ar-validation-subsection-title';
    crossChecksTitle.textContent = 'Cross-checks';
    crossChecksSubsection.appendChild(crossChecksTitle);
    section.appendChild(crossChecksSubsection);

    // Insert the new section where the Cross-checks field currently sits, then move both
    // existing fields into the Cross-checks sub-section (appendChild on an existing node moves
    // it, it does not clone it).
    crossChecksField.parentNode.insertBefore(section, crossChecksField);
    crossChecksSubsection.appendChild(crossChecksField);
    crossChecksSubsection.appendChild(chainPreviewField);
}
buildCategoryValidationRulesSection();

const arCategoryClearedRequirementsList = document.getElementById('arCategoryClearedRequirementsList');

// ---------- CATEGORY DETAIL: CLEARED REQUIREMENTS SUMMARY (Phase 2 of 3) ----------
// Read-only "what does Cleared require for this Category" bullets, fetched fresh from the
// Phase 1 backend endpoint (routers/audit_rules.py) on every Category detail open — no manual
// toggles, no local/cached state, reflects live backend state exactly. A brand-new/unsaved
// Category has no id yet, so it shows a "save first" hint instead (same convention as the
// Signature Specimens section's "Save the document first" hint).
async function renderCategoryClearedRequirements(categoryId) {
    if (!arCategoryClearedRequirementsList) return;

    if (!categoryId) {
        arCategoryClearedRequirementsList.innerHTML = '';
        const hint = document.createElement('div');
        hint.className = 'ar-cleared-requirements-hint';
        hint.textContent = 'Save the category first to see what Cleared requires.';
        arCategoryClearedRequirementsList.appendChild(hint);
        return;
    }

    arCategoryClearedRequirementsList.innerHTML = '';
    const loading = document.createElement('div');
    loading.className = 'ar-cleared-requirements-hint';
    loading.textContent = 'Loading...';
    arCategoryClearedRequirementsList.appendChild(loading);

    try {
        const res = await fetch(API_BASE + '/api/audit-rules/categories/' + categoryId + '/cleared-requirements');
        if (!res.ok) throw new Error('Failed to load cleared-requirements summary');
        const data = await res.json();
        renderClearedRequirementsBullets(data);
    } catch (error) {
        arCategoryClearedRequirementsList.innerHTML = '';
        console.warn('Failed to load cleared-requirements summary:', error);
    }
}

function renderClearedRequirementsBullets(data) {
    arCategoryClearedRequirementsList.innerHTML = '';
    const ul = document.createElement('ul');
    ul.className = 'ar-cleared-requirements-bullets';

    const addBullet = (text) => {
        const li = document.createElement('li');
        li.className = 'ar-cleared-requirement-item';
        li.textContent = text;
        ul.appendChild(li);
    };

    // Physical Bill evidence type (Phase 4 of 8): surfaced first, since it's what decides which
    // kind of evidence the rest of the list means. "not set" is shown plainly rather than hidden —
    // an unclassified Category is exactly what Phase 6's gate won't be able to classify.
    const EVIDENCE_TYPE_LABELS = { billable: 'Billable', non_billable: 'Non-billable' };
    addBullet('Evidence type — ' + (EVIDENCE_TYPE_LABELS[data.evidence_type] || 'not set'));

    addBullet('Voucher-level checks — always required');
    addBullet('Account Head — always required');
    addBullet(
        data.cross_checks_required
            ? 'Cross-checks — required (active cross-check attached)'
            : 'Cross-checks — not required (no active cross-check attached)'
    );

    const docs = data.required_documents || [];
    if (docs.length === 0) {
        addBullet('Required documents — none');
    } else {
        // Each document is marked Physical Bill where applicable (Phase 4 of 8) alongside the
        // pre-existing inactive marker — the two compose, so a document can carry both.
        const names = docs.map(d => {
            const markers = [];
            if (d.document_type === 'physical_bill') markers.push('Physical Bill');
            if (!d.is_active) markers.push('inactive');
            return markers.length ? `${d.name} (${markers.join(', ')})` : d.name;
        }).join(', ');
        addBullet('Required documents — ' + names);
    }

    arCategoryClearedRequirementsList.appendChild(ul);
}

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
// Automated cross-document validation engine (Phase 4 of 10): {document_id: is_required (bool)},
// populated from cat.documents on selectCategory() — a document's presence as a KEY here (not
// its value) is what "this pairing is actually persisted in audit_rule_category_documents"
// means, since a document just added locally (not yet saved) has no key at all. (The former
// categoryDocApprovals/approval_required flag this once sat alongside was hard-removed -
// confirmed requirement, 2026-08-02.)
let categoryDocRequired = {};
// Inactive-document indicator (new, confirmed fix): {document_id: {name, is_active}},
// populated from cat.documents on selectCategory() alongside categoryDocIds/categoryDocRequired
// above - the authoritative per-attachment name/active-state, valid even once a document has
// been soft-deleted and so no longer appears in the active-only allDocuments cache below.
let categoryDocMeta = {};
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
        // ar-category-list-card gives this row a card surface (border/radius/padding) so the
        // two-line name + rule-count block reads as a discrete item — Categories only, the other
        // entity lists keep the plain .ar-list-row treatment.
        const row = document.createElement('div');
        row.className = 'ar-list-row ar-category-list-card' + (cat.is_active ? '' : ' ar-list-row-inactive');
        if (cat.id === editingCategoryId) row.classList.add('ar-list-row-selected');

        // Rule-count badge (Phase 3 of 3, FINAL). Cross-check count comes straight from this
        // same list response (cat.cross_checks, already returned by row_to_category() -- no
        // new endpoint needed). Voucher-level checks are global (not per-Category), so every
        // row shows the same active-check count, read from allVoucherChecksCache (populated by
        // loadVoucherChecks(), chained before loadCategories() in switchEntityType() above).
        // Always rendered, zero included -- same plain-count convention as .ar-used-by-label
        // ("Used by 0 vouchers"), no separate hidden/muted empty state.
        const infoWrap = document.createElement('div');
        infoWrap.className = 'ar-category-row-info';

        const nameSpan = document.createElement('span');
        nameSpan.className = 'ar-list-row-name';
        nameSpan.textContent = cat.name;
        infoWrap.appendChild(nameSpan);

        const crossCheckCount = (cat.cross_checks || []).length;
        const voucherCheckCount = allVoucherChecksCache.filter(c => c.is_active).length;
        const ruleBadge = document.createElement('span');
        ruleBadge.className = 'ar-category-rule-badge';
        ruleBadge.textContent = crossCheckCount + ' cross-check' + (crossCheckCount === 1 ? '' : 's') +
            ', ' + voucherCheckCount + ' voucher check' + (voucherCheckCount === 1 ? '' : 's');
        infoWrap.appendChild(ruleBadge);

        row.appendChild(infoWrap);

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

// ---------- DOCUMENTS LIST (merged: name + Required checkbox + remove, one row per document) ----------
// Merges what used to be two separate blocks (a chip list for add/remove, and a standalone
// Document Requirements list for the Required checkbox) into one row-based list — both acted
// on the exact same categoryDocIds array, so this is a pure UI merge, no new data source.
// A row's Required checkbox is only enabled once its document is actually persisted in
// audit_rule_category_documents (i.e. present as a key in categoryDocRequired) — the PUT
// .../required endpoint 404s on a pairing that doesn't exist yet (a brand-new category, or a
// document just added locally but not yet saved). (This row used to also carry an "Approved"
// checkbox for the now-removed approval_required flag - confirmed requirement, 2026-08-02 -
// approval requirement now lives solely at Document level via the "Approved by" role's
// Required Authority field.)
// Resolves {name, is_active} for an attached document, preferring categoryDocMeta (the
// authoritative per-attachment data from the last-loaded Category, correct even for a
// since-soft-deleted document) over the active-only allDocuments cache (covers a document
// just added in the current unsaved edit session, not yet round-tripped through
// categoryDocMeta) — falls back to a bare id label only if neither source has anything.
function getCategoryDocDisplayInfo(docId) {
    if (Object.prototype.hasOwnProperty.call(categoryDocMeta, docId)) {
        return categoryDocMeta[docId];
    }
    const doc = getDocumentById(docId);
    if (doc) {
        return { name: doc.name, is_active: true };
    }
    return { name: 'Document #' + docId, is_active: true };
}

function renderCategoryDocsList() {
    arCategoryDocsList.innerHTML = '';

    // Inactive attached documents are fully hidden from this list (confirmed fix) — never
    // rendered as a row (no name/Inactive badge/Required checkbox/✕ button), and no
    // "--No Document--" placeholder either. Still attached in categoryDocIds/on the server
    // either way — this is display-only, never auto-detached. Computed up front so an
    // all-inactive attachment set still shows the empty state rather than an empty table.
    const visibleDocIds = categoryDocIds.filter(docId => getCategoryDocDisplayInfo(docId).is_active);

    if (visibleDocIds.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'ar-category-empty-state';
        empty.textContent = 'No documents attached yet.';
        arCategoryDocsList.appendChild(empty);
    } else {
        const table = document.createElement('table');
        table.className = 'ar-category-doc-table';

        const thead = document.createElement('thead');
        const headRow = document.createElement('tr');
        ['Document', 'Required', ''].forEach(text => {
            const th = document.createElement('th');
            th.textContent = text;
            headRow.appendChild(th);
        });
        thead.appendChild(headRow);
        table.appendChild(thead);

        const tbody = document.createElement('tbody');

        visibleDocIds.forEach(docId => {
            const displayInfo = getCategoryDocDisplayInfo(docId);
            const isPersisted = Object.prototype.hasOwnProperty.call(categoryDocRequired, docId);

            const row = document.createElement('tr');
            if (!isPersisted) row.className = 'ar-category-doc-row-unsaved';

            const nameCell = document.createElement('td');
            nameCell.className = 'ar-category-doc-name';
            nameCell.textContent = displayInfo.name +
                (isPersisted ? '' : ' (save the category first)');
            row.appendChild(nameCell);

            // Defaults to checked/required, matching audit_rule_category_documents.is_required's
            // own DB default (TRUE) — a document whose key isn't in categoryDocRequired yet
            // (not persisted) still shows checked, so nothing looks "unrequired" before save.
            // Still a real checkbox (it drives a live PUT), just presented as a badge-style pill.
            const requiredCell = document.createElement('td');
            const requiredCheckbox = document.createElement('input');
            requiredCheckbox.type = 'checkbox';
            requiredCheckbox.checked = isPersisted ? !!categoryDocRequired[docId] : true;
            requiredCheckbox.disabled = !isPersisted;
            requiredCheckbox.addEventListener('change', () => toggleCategoryDocumentRequired(docId, requiredCheckbox.checked));

            const requiredLabel = document.createElement('label');
            requiredLabel.className = 'ar-category-doc-required' +
                (requiredCheckbox.checked ? ' checked' : '') +
                (isPersisted ? '' : ' disabled');
            requiredLabel.appendChild(requiredCheckbox);
            requiredLabel.appendChild(document.createTextNode('Required'));
            requiredCell.appendChild(requiredLabel);
            row.appendChild(requiredCell);

            const actionCell = document.createElement('td');
            actionCell.className = 'ar-category-doc-action';
            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'ar-category-doc-remove';
            removeBtn.textContent = '✕';
            removeBtn.title = 'Remove';
            removeBtn.addEventListener('click', () => removeCategoryDocument(docId));
            actionCell.appendChild(removeBtn);
            row.appendChild(actionCell);

            tbody.appendChild(row);
        });

        table.appendChild(tbody);
        arCategoryDocsList.appendChild(table);
    }

    // Rebuild the "add document" picker to exclude documents already attached (confirmed
    // fix): sourced from allDocuments, which is already active-only — an inactive
    // (soft-deleted) Document is fully excluded from this dropdown, not just disabled/
    // greyed. Always starts on a "No Document" placeholder; the Add button (hidden by
    // default here) only reappears once the change listener below sees a real option chosen.
    arCategoryAddDocumentSelect.innerHTML = '';
    const available = allDocuments.filter(d => !categoryDocIds.includes(d.id));

    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = allDocuments.length === 0 ? 'No documents exist yet' : 'No Document';
    placeholder.selected = true;
    arCategoryAddDocumentSelect.appendChild(placeholder);

    available.forEach(d => {
        const opt = document.createElement('option');
        opt.value = d.id;
        opt.textContent = d.name;
        arCategoryAddDocumentSelect.appendChild(opt);
    });

    arCategoryAddDocumentBtn.style.display = 'none';
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
            // Mirrors the backend's own no-op-doesn't-bump rule (bump_category_matrix_version()
            // only fires on an actual value change) — no extra fetch needed to know this.
            arCategoryMatrixVersion.textContent = String(parseInt(arCategoryMatrixVersion.textContent, 10) + 1);
        }
        // Dimmed/optional pill state in the chain preview depends on categoryDocRequired,
        // which just changed here without going through renderCategoryCrossCheckChips().
        renderCategoryCrossCheckChainPreview();
        showCategoryDetailStatus('✅ Required flag updated.', 'success');
    } catch (error) {
        showCategoryDetailStatus('❌ Error: ' + error.message, 'error');
        renderCategoryDocsList();
    }
}

function addCategoryDocument() {
    const value = arCategoryAddDocumentSelect.value;
    if (!value) return;
    categoryDocIds.push(parseInt(value, 10));
    renderCategoryDocsList();
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
    renderCategoryDocsList();
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
        const empty = document.createElement('div');
        empty.className = 'ar-category-empty-state';
        empty.textContent = 'No chains to preview — attach a cross-check above.';
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
        const empty = document.createElement('div');
        empty.className = 'ar-category-empty-state';
        empty.textContent = 'No cross-checks attached — this category has no cross-document validation rules.';
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
        // Physical Bill evidence type (Phase 4 of 8): the `|| ''` is the backfill mechanism - a
        // Category predating this field opens with the picker genuinely UNSET rather than
        // defaulted to either value, and saveCategory() then blocks until one is chosen.
        arCategoryEvidenceTypeSelect.value = cat.evidence_type || '';
        categoryDocIds = (cat.documents || []).map(d => d.document_id);
        categoryCrossCheckIds = (cat.cross_checks || []).map(c => c.cross_check_id);
        categoryDocRequired = {};
        categoryDocMeta = {};
        (cat.documents || []).forEach(d => {
            categoryDocRequired[d.document_id] = !!d.is_required;
            categoryDocMeta[d.document_id] = { name: d.name, is_active: !!d.is_active };
        });
        arCategoryMatrixVersion.textContent = String(cat.matrix_version || 1);
        renderCategoryDocsList();
        renderCategoryCrossCheckChips();
        renderCategoryClearedRequirements(cat.id);

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

        updateAuditRulesUrl();

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
    arCategoryEvidenceTypeSelect.value = '';
    categoryDocIds = [];
    categoryCrossCheckIds = [];
    categoryDocRequired = {};
    categoryDocMeta = {};
    arCategoryMatrixVersion.textContent = '1';
    renderCategoryDocsList();
    renderCategoryCrossCheckChips();
    renderCategoryClearedRequirements(null);

    arCategoryUsedByRow.style.display = 'none';
    arCategoryDeleteBtn.style.display = 'none';
    arCategoryRestoreBtn.style.display = 'none';

    arCategoryDetailEmpty.style.display = 'none';
    arCategoryDetailForm.style.display = 'block';
    arCategoryNameInput.focus();

    updateAuditRulesUrl();
}

// ---------- SAVE ----------
async function saveCategory() {
    const name = arCategoryNameInput.value.trim();
    if (!name) {
        showCategoryDetailStatus('❌ Name is required.', 'error');
        return;
    }

    // Physical Bill evidence type (Phase 4 of 8): mirrors the backend's own reject-if-missing rule
    // (validate_category_evidence_type(), routers/audit_rules.py) on both create AND edit, so the
    // user gets an inline message here rather than a bare 400 from the server. Sending this field
    // is also what un-breaks Category saving, which has been 400ing since Phase 3.
    const evidenceType = arCategoryEvidenceTypeSelect.value;
    if (!evidenceType) {
        showCategoryDetailStatus('❌ Evidence type is required — choose Billable or Non-billable.', 'error');
        return;
    }

    const payload = {
        name: name,
        kb_overview: arCategoryKbOverviewInput.value.trim() || null,
        document_ids: categoryDocIds,
        cross_check_ids: categoryCrossCheckIds,
        evidence_type: evidenceType
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
// Confirmed fix: Add button stays hidden until a real, selectable (non-placeholder,
// non-disabled/inactive) Document is chosen.
arCategoryAddDocumentSelect.addEventListener('change', () => {
    const selected = arCategoryAddDocumentSelect.selectedOptions[0];
    arCategoryAddDocumentBtn.style.display = (selected && selected.value && !selected.disabled) ? '' : 'none';
});
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
// Whether the whole-shell Assign Categories view is currently open — drives
// updateAuditRulesUrl()'s view=assign-categories vs entity/id branch below.
let assignCategoriesViewOpen = false;

// ---------- URL STATE PERSISTENCE (confirmed fix) ----------
// Mirrors verification.js's setVerificationUrl()/window.onload hard-refresh persistence:
// reflects the current view in the URL via history.replaceState() (no new history entries —
// this page manages its own navigation via the global navbar back-arrow, not the browser's
// native back button) so a hard refresh restores the same view instead of always dropping
// back to the Documents rail default. Only ever called after the whole script has run (event
// handlers / window.onload), so referencing editingCrossCheckId/editingCategoryId/
// editingVoucherCheckId here — declared with `let` further down the file — is safe, same
// forward-reference reasoning switchEntityType() below already relies on for
// allDocuments/allCrossChecks.
function updateAuditRulesUrl() {
    const url = new URL(window.location.href);
    if (assignCategoriesViewOpen) {
        url.searchParams.set('view', 'assign-categories');
        url.searchParams.delete('entity');
        url.searchParams.delete('id');
    } else {
        url.searchParams.delete('view');
        url.searchParams.set('entity', currentEntityType);
        const selectedId = {
            documents: editingId,
            'cross-checks': editingCrossCheckId,
            categories: editingCategoryId,
            'voucher-checks': editingVoucherCheckId
        }[currentEntityType];
        if (selectedId) {
            url.searchParams.set('id', String(selectedId));
        } else {
            url.searchParams.delete('id');
        }
    }
    history.replaceState(null, '', url);
}

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
    assignCategoriesViewOpen = true;
    updateAuditRulesUrl();
    // Global navbar Up icon: Assign Categories' structural parent is the entity rail it was
    // opened from — reuses the same function the in-page "⬅ Back" button already calls.
    if (typeof window.setUpOverride === 'function') window.setUpOverride(closeAssignCategoriesView);
    // Global navbar Back icon: records entering this view as a chronological history entry.
    if (typeof window.pushInPageNavEntry === 'function') window.pushInPageNavEntry(null, 'Assign Categories');
    if (assignCategoriesAccounts.length > 0) {
        // Already loaded once this page session — a full reload here would wipe+rebuild the
        // whole table and lose scroll position just to pick up a category added/renamed
        // elsewhere; only the dropdown options actually need refreshing (confirmed fix).
        await refreshAssignCategoriesCategoryOptions();
    } else {
        await loadAssignCategoriesData();
    }
}

function closeAssignCategoriesView() {
    arAssignCategoriesSection.style.display = 'none';
    arShellContainer.style.display = 'flex';
    assignCategoriesViewOpen = false;
    // Necessary side-fix: checkAssignCategoriesDeepLink() triggers purely on client_id+
    // account_code being present in the URL, with no expiry — left in place, they'd make
    // every future hard refresh keep reopening Assign Categories even after the user has
    // navigated elsewhere in this page. Only relevant to clear here (not on every
    // updateAuditRulesUrl() call) since this is the one place the user is done with them.
    const url = new URL(window.location.href);
    url.searchParams.delete('client_id');
    url.searchParams.delete('account_code');
    history.replaceState(null, '', url);
    updateAuditRulesUrl();
    // Now back on the entity rail — Up falls back to the static default (getUpTarget()). Not
    // pushed to the Back history stack: this is an Up-equivalent retreat, not new forward progress.
    if (typeof window.setUpOverride === 'function') window.setUpOverride(null);
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
            updateAssignCategoriesUnassignedBadge();
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
            updateAssignCategoriesUnassignedBadge();
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
        updateAssignCategoriesUnassignedBadge();
    }
}

// ---------- RENDER TABLE ----------
function renderAssignCategoriesTable() {
    arAssignCategoriesTableBody.innerHTML = '';
    assignCategoriesAccounts.forEach(account => {
        arAssignCategoriesTableBody.appendChild(buildAssignCategoriesRow(account));
    });
    updateAssignCategoriesUnassignedBadge();
    restoreLastEditedAssignCategoriesHighlight();
}

// Confirmed fix: reopening this view after it's already been loaded once this session must
// NOT re-fetch accounts / wipe+rebuild the whole table (loses scroll position) — only the
// Category dropdown options can meaningfully have changed (added/renamed/deactivated via the
// separate Categories pane), so just refresh assignCategoriesCategoryOptions and patch each
// already-rendered row's <select> in place via buildAssignCategoriesRow()+replaceWith(), same
// per-row-swap technique applyCategoryToRows() already uses — this never touches the table's
// scroll container itself, so scroll position is naturally preserved.
async function refreshAssignCategoriesCategoryOptions() {
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/categories?include_inactive=false');
        if (!res.ok) throw new Error('Failed to load categories');
        const categories = await res.json();
        assignCategoriesCategoryOptions = categories.map(c => ({ id: c.id, name: c.name }));
        assignCategoriesAccounts.forEach(account => {
            const oldRow = arAssignCategoriesTableBody.querySelector('tr[data-account-id="' + account.id + '"]');
            if (oldRow) oldRow.replaceWith(buildAssignCategoriesRow(account));
        });
        updateAssignCategoriesUnassignedBadge();
    } catch (error) {
        console.warn('Failed to refresh category options:', error);
    }
}

// ---------- UNASSIGNED SUMMARY BADGE (confirmed fix) ----------
// "X of Y unassigned" count in the section header, recomputed from the currently loaded
// assignCategoriesAccounts list — kept in sync by every place that mutates that list or
// rebuilds the table (renderAssignCategoriesTable(), applyCategoryToRows(), and the two
// early-return branches in loadAssignCategoriesData() below that never reach
// renderAssignCategoriesTable() at all). Lazily creates its own element once, inserted right
// after the existing subtitle span — no static/audit_rules.html change needed.
function updateAssignCategoriesUnassignedBadge() {
    let badge = document.getElementById('arAssignCategoriesUnassignedBadge');
    if (!badge) {
        badge = document.createElement('span');
        badge.id = 'arAssignCategoriesUnassignedBadge';
        badge.className = 'ar-assign-categories-unassigned-badge';
        arAssignCategoriesSubtitle.insertAdjacentElement('afterend', badge);
    }
    if (assignCategoriesAccounts.length === 0) {
        badge.style.display = 'none';
        return;
    }
    const unassignedCount = assignCategoriesAccounts.filter(a => !a.category_id).length;
    badge.textContent = unassignedCount + ' of ' + assignCategoriesAccounts.length + ' unassigned';
    badge.style.display = 'inline-block';
}

// Builds one <tr> for an account. createElement/textContent throughout (no innerHTML)
// since account codes/names come from an uploaded file, not this app's own control —
// same convention trial_balance.js's renderReviewTable() already follows.
function buildAssignCategoriesRow(account) {
    const tr = document.createElement('tr');
    tr.dataset.accountId = account.id;

    // Unassigned-flag requirement (confirmed fix): amber row tint for any account with no
    // category_id, driven purely by that one field — see also the placeholder option below
    // and updateAssignCategoriesUnassignedBadge()'s header count.
    const isUnassigned = !account.category_id;
    if (isUnassigned) {
        tr.classList.add('ar-tb-row-unassigned');
    }

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

    // Unassigned-flag requirement (confirmed fix): a disabled, selected placeholder is shown
    // first for a genuinely unassigned account, nudging the user to pick one. The real
    // "-- Not assigned --" option below it (still real/selectable, per the 2026-07-23 fix)
    // is unaffected — it's simply no longer the pre-selected option when there's no category,
    // since this placeholder takes that role instead.
    if (isUnassigned) {
        const placeholderOption = document.createElement('option');
        placeholderOption.value = '';
        placeholderOption.disabled = true;
        placeholderOption.selected = true;
        placeholderOption.textContent = '— Select category —';
        select.appendChild(placeholderOption);
    }

    // Confirmed fix: "Not assigned" is now a real, selectable option (value="none"),
    // not a disabled placeholder — picking it clears the account's category the same
    // way picking a real category assigns one (see the change listener below).
    const notAssignedOption = document.createElement('option');
    notAssignedOption.value = 'none';
    notAssignedOption.textContent = '-- Not assigned --';
    select.appendChild(notAssignedOption);

    assignCategoriesCategoryOptions.forEach(cat => {
        const opt = document.createElement('option');
        opt.value = cat.id;
        opt.textContent = cat.name;
        if (account.category_id === cat.id) opt.selected = true;
        select.appendChild(opt);
    });

    // Inactive-selection requirement, Phase 4: an account can already be assigned a
    // Category that's since been deactivated (assignCategoriesCategoryOptions is
    // active-only, per the "never select an inactive item" rule) — without this, no
    // real <option> above would ever match account.category_id, and the browser
    // would silently default the <select> to "-- Not assigned --", misrepresenting
    // a real (frozen) assignment. This extra option exists purely to display that
    // frozen assignment correctly; it's never added to assignCategoriesCategoryOptions
    // itself, so it can't be picked for a *different* row's fresh assignment.
    if (account.category_id && !assignCategoriesCategoryOptions.some(cat => cat.id === account.category_id)) {
        const inactiveOpt = document.createElement('option');
        inactiveOpt.value = account.category_id;
        inactiveOpt.textContent = (account.category_name || 'Unknown category') + ' (inactive)';
        inactiveOpt.selected = true;
        select.appendChild(inactiveOpt);
    }
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
    if (cat) return cat.name;
    // Inactive-selection requirement, Phase 4: id may point at a Category that's
    // since been deactivated (not in the active-only assignCategoriesCategoryOptions
    // list) — fall back to any account row that already carries this id, since the
    // backend returns category_name regardless of active state (a display value).
    const accountWithCategory = assignCategoriesAccounts.find(a => a.category_id === id);
    return accountWithCategory ? (accountWithCategory.category_name || 'Unknown category') : null;
}

// ---------- LAST-EDITED ROW: SCROLL/HIGHLIGHT RESTORE (confirmed fix) ----------
// sessionStorage (survives a hard browser refresh, unlike a plain JS variable) remembers the
// last account row the user edited a Category for, so that after any full table rebuild (first
// open, hard refresh, Reset) we can scroll back to it and briefly flag it — consumed once per
// restore so it doesn't keep re-highlighting on every later reopen.
const AR_ASSIGN_CATEGORIES_LAST_EDITED_KEY = 'arAssignCategoriesLastEditedAccountId';

function setLastEditedAssignCategoriesAccount(accountId) {
    try {
        sessionStorage.setItem(AR_ASSIGN_CATEGORIES_LAST_EDITED_KEY, String(accountId));
    } catch (e) { /* sessionStorage unavailable — purely a UX nicety, safe to skip */ }
}

function restoreLastEditedAssignCategoriesHighlight() {
    let storedId;
    try {
        storedId = sessionStorage.getItem(AR_ASSIGN_CATEGORIES_LAST_EDITED_KEY);
    } catch (e) { return; }
    if (!storedId) return;
    const row = arAssignCategoriesTableBody.querySelector('tr[data-account-id="' + storedId + '"]');
    if (row) {
        row.scrollIntoView({ behavior: 'smooth', block: 'center' });
        row.classList.add('ar-tb-restore-highlight');
        setTimeout(() => row.classList.remove('ar-tb-restore-highlight'), 2000);
    }
    try { sessionStorage.removeItem(AR_ASSIGN_CATEGORIES_LAST_EDITED_KEY); } catch (e) {}
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
        // Revert the target row to its pre-change appearance — account data was never
        // optimistically mutated (targetAccount.category_id still holds previousCategoryId),
        // so rebuilding from it restores select value, unassigned tint, and the placeholder
        // option all at once (see buildAssignCategoriesRow()) rather than only patching
        // select.value, which can't correctly add/remove the placeholder option itself.
        const oldRow = arAssignCategoriesTableBody.querySelector('tr[data-account-id="' + accountId + '"]');
        if (oldRow && targetAccount) {
            oldRow.replaceWith(buildAssignCategoriesRow(targetAccount));
        }
        showAssignCategoriesStatus('❌ Error: ' + error.message, 'error');
    }
}

// Updates the in-memory account list and rebuilds the affected <tr>(s) for a set of account
// ids, after a successful assignment call. categoryId may be null/falsy (the account was
// cleared to "Not assigned"). A full row rebuild (via buildAssignCategoriesRow(), rather than
// patching just select.value) is used so the unassigned-flag placeholder option and amber row
// tint stay correct too — arAssignCategoriesTableBody's change/click listeners are delegated
// on the container, not per-row, so swapping a row's DOM node is safe.
function applyCategoryToRows(accountIds, categoryId, categoryName) {
    accountIds.forEach(id => {
        const account = getAssignCategoriesAccountById(id);
        if (account) {
            account.category_id = categoryId || null;
            account.category_name = categoryName;
        }
        const oldRow = arAssignCategoriesTableBody.querySelector('tr[data-account-id="' + id + '"]');
        if (oldRow && account) {
            oldRow.replaceWith(buildAssignCategoriesRow(account));
        }
    });
    updateAssignCategoriesUnassignedBadge();
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
    setLastEditedAssignCategoriesAccount(accountId);
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
    setLastEditedAssignCategoriesAccount(accountId);
    assignCategory(accountId, categoryId, true);
});

// ---------- INIT ----------
// URL state persistence (confirmed fix): restores whichever view (entity type + selected
// item, or the Assign Categories full-page view) was showing before a hard refresh, using
// the same ?entity=&id=/?view= params updateAuditRulesUrl() writes throughout this file —
// mirrors verification.js's own hard-refresh restore pattern in window.onload.
const AR_VALID_ENTITY_TYPES = ['documents', 'cross-checks', 'categories', 'voucher-checks'];

// Global navbar Back icon: factored out of window.onload's own hard-refresh restore logic (below)
// so the same dispatch also serves as the Back history stack's in-page restore handler — reached
// only in the narrow edge case where the popped stack entry's URL exactly matches the current URL
// (Back's own "different page, do a real navigation" path already handles every other case via a
// full reload straight into this same ?view=/?entity=/?id= restore logic, since
// pushInPageNavEntry() always pushes AFTER updateAuditRulesUrl() has updated the URL to match the
// view being entered). Mirrors verification.js's restoreVerificationViewFromUrl().
async function restoreAuditRulesViewFromUrl() {
    const restoreParams = new URLSearchParams(window.location.search);
    const viewParam = restoreParams.get('view');

    if (viewParam === 'assign-categories') {
        await openAssignCategoriesView();
        return;
    }

    const entityParam = restoreParams.get('entity');
    const idParam = restoreParams.get('id');
    const restoredEntity = AR_VALID_ENTITY_TYPES.includes(entityParam) ? entityParam : 'documents';

    if (restoredEntity === 'documents') {
        await loadDocuments();
        if (idParam) await selectDocument(parseInt(idParam, 10));
    } else {
        // switchEntityType() now returns its cache-load promise (see the ENTITY TYPE SWITCH
        // section above) so the relevant select*() call below only fires once the cache it
        // needs (allDocuments/allCrossChecks/etc.) is actually populated.
        await switchEntityType(restoredEntity);
        if (idParam) {
            const id = parseInt(idParam, 10);
            if (restoredEntity === 'cross-checks') await selectCrossCheck(id);
            else if (restoredEntity === 'categories') await selectCategory(id);
            else if (restoredEntity === 'voucher-checks') await selectVoucherCheck(id);
        }
    }
}

window.onload = async function() {
    // Verification deep link (Phase 5, FINAL): a ?client_id=&account_code= URL means Verification's
    // recycle-arrow icon opened this page — skip the normal Documents-rail default entirely and go
    // straight to the highlighted Assign Categories row. A plain/in-app page load (no params) is
    // completely unaffected, matching the confirmed brief. Takes priority over the URL-restore
    // logic below.
    const isDeepLink = await checkAssignCategoriesDeepLink();
    if (isDeepLink) return;

    // The Documents form's Template dropdown is needed regardless of which entity/view ends
    // up restored below.
    loadTemplatesForDocuments();

    // Global navbar Back icon: lets the history stack restore this page's own in-page views (see
    // restoreAuditRulesViewFromUrl() above) for the rare same-URL edge case.
    if (typeof window.setInPageRestoreHandler === 'function') {
        window.setInPageRestoreHandler(restoreAuditRulesViewFromUrl);
    }

    await restoreAuditRulesViewFromUrl();
};

// =====================================================================================
// VOUCHER-LEVEL CHECKS (Generic Voucher-Level Checks, Phase 3 of 6)
// A 4th reusable entity type: audit_rule_voucher_checks. Ordered by sort_order (server-
// assigned), NOT alphabetical like Documents/Cross-checks/Categories. Soft-delete/restore
// like Documents. Name-only (confirmed
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
const arVoucherCheckAuthorityInput = document.getElementById('arVoucherCheckAuthorityInput');
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
// Rule-count badge (Phase 3 of 3, FINAL): every check fetched by the most recent
// loadVoucherChecks() call, regardless of that widget's own "Show inactive" toggle -- read by
// renderCategoryList() (filtered to is_active there) since voucher-level checks are global
// (no Category linkage in schema), so the same active count applies to every category row.
let allVoucherChecksCache = [];

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
        allVoucherChecksCache = checks;
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
        arVoucherCheckAuthorityInput.value = check.authority || '';

        if (check.is_active) {
            arVoucherCheckDeleteBtn.style.display = 'inline-block';
            arVoucherCheckRestoreBtn.style.display = 'none';
        } else {
            arVoucherCheckDeleteBtn.style.display = 'none';
            arVoucherCheckRestoreBtn.style.display = 'inline-block';
        }

        arVoucherCheckDetailEmpty.style.display = 'none';
        arVoucherCheckDetailForm.style.display = 'block';

        updateAuditRulesUrl();

        loadVoucherChecks(); // refresh selected-row highlight, same pattern as Documents
    } catch (error) {
        showVoucherCheckDetailStatus('❌ Error: ' + error.message, 'error');
    }
}

function startNewVoucherCheck() {
    editingVoucherCheckId = null;
    arVoucherCheckEditingIdInput.value = '';
    arVoucherCheckNameInput.value = '';
    arVoucherCheckAuthorityInput.value = '';

    arVoucherCheckDeleteBtn.style.display = 'none';
    arVoucherCheckRestoreBtn.style.display = 'none';

    arVoucherCheckDetailEmpty.style.display = 'none';
    arVoucherCheckDetailForm.style.display = 'block';
    arVoucherCheckNameInput.focus();

    updateAuditRulesUrl();
}

// ---------- SAVE / DELETE / RESTORE ----------
async function saveVoucherCheck() {
    const name = arVoucherCheckNameInput.value.trim();
    if (!name) {
        showVoucherCheckDetailStatus('⚠️ Name is required.', 'error');
        return;
    }

    const payload = { name: name, authority: arVoucherCheckAuthorityInput.value.trim() || null };

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
