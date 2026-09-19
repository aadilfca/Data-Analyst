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

// Authorisation roles remain editable on the Documents detail form.
let rolesState = [];

// Required Authority (new, confirmed requirement): role_name -> required authority text,
// only ever relevant for an "Approved by"-type role (see isApprovalRole()). Additive
// alongside rolesState/auth_roles - kept as a parallel map rather than changing rolesState's
// shape, so nothing else reading auth_roles as plain strings needs to change.
let requiredAuthorityState = {};

// Phase 2: live editor state for the Template-scoped verification projection. The server's
// detail response remains the source of truth after every save; this state only represents
// unsaved changes in the open Audit Rules form.
let verificationPolicyState = null;
let verificationPolicyTemplateId = null;
let verificationPolicyView = null;
let currentDocumentRecord = null;
let verificationTemplateLoadToken = 0;
let verificationTemplateLoading = false;
// Phase B: unsaved reusable Form field definitions for the open Document.
let formFieldsState = [];
let formTemplateColumns = [];

// ---------- DOM REFS ----------
const arListBody = document.getElementById('arListBody');
const arListEmptyMessage = document.getElementById('arListEmptyMessage');
const arShowInactiveCheckbox = document.getElementById('arShowInactiveCheckbox');
const arAddNewBtn = document.getElementById('arAddNewBtn');
const arLoadMoreBtn = document.getElementById('arLoadMoreBtn');

// Pagination (Fast Data Handling, Phase 10 of 13). PAGE_SIZE matches the backend's Query(..., le=200)
// ceiling with room to spare. documentsOffset/documentsTotal/documentItems track the accumulated
// "Load more" state for the currently-loaded filter (Show inactive toggle); loadDocuments() always
// resets these back to page 1, mirroring every other "fresh load" call site (toggle change, entity
// switch, post-save/delete/restore refresh).
const DOCUMENTS_PAGE_SIZE = 30;
let documentsOffset = 0;
let documentsTotal = 0;
let documentItems = [];

const arDetailEmpty = document.getElementById('arDetailEmpty');
const arDetailForm = document.getElementById('arDetailForm');
const arEditingIdInput = document.getElementById('arEditingId');
const arNameInput = document.getElementById('arNameInput');
const arKbNoteInput = document.getElementById('arKbNoteInput');
const arTemplateSelect = document.getElementById('arTemplateSelect');
const arRiskRatingSelect = document.getElementById('arRiskRatingSelect');
const arVerificationFieldsField = document.getElementById('arVerificationFieldsField');
const arVerificationPolicyStatus = document.getElementById('arVerificationPolicyStatus');
const arVerificationCoreFields = document.getElementById('arVerificationCoreFields');
const arVerificationOptionalFields = document.getElementById('arVerificationOptionalFields');
const arVerificationOptionalSelect = document.getElementById('arVerificationOptionalSelect');
const arVerificationOptionalRisk = document.getElementById('arVerificationOptionalRisk');
const arVerificationOptionalAddBtn = document.getElementById('arVerificationOptionalAddBtn');
const arVerificationPolicyHint = document.getElementById('arVerificationPolicyHint');
// Physical Bill evidence type (Phase 4 of 8): the type picker plus the blocks it hides.
const arDocumentTypeSelect = document.getElementById('arDocumentTypeSelect');
const arLegacyPhysicalBillOption = document.getElementById('arLegacyPhysicalBillOption');
const arDocumentTypeHint = document.getElementById('arDocumentTypeHint');
const arDocTemplateField = document.getElementById('arDocTemplateField');
const arDocTemplateLabel = document.getElementById('arDocTemplateLabel');
const arFormFieldsField = document.getElementById('arFormFieldsField');
const arFormFieldsBody = document.getElementById('arFormFieldsBody');
const arFormAddFieldBtn = document.getElementById('arFormAddFieldBtn');
const arFormFieldsHint = document.getElementById('arFormFieldsHint');
const arRolesChipList = document.getElementById('arRolesChipList');
const arRolesInput = document.getElementById('arRolesInput');
const arRolesAddBtn = document.getElementById('arRolesAddBtn');
const arDocSignatureSpecimensHint = document.getElementById('arDocSignatureSpecimensHint');
const arDocSignatureSpecimensContainer = document.getElementById('arDocSignatureSpecimensContainer');
const arUsedByRow = document.getElementById('arUsedByRow');
const arUsedByCount = document.getElementById('arUsedByCount');
const arDetailStatus = document.getElementById('arDetailStatus');
const arSaveBtn = document.getElementById('arSaveBtn');
const arDeleteBtn = document.getElementById('arDeleteBtn');
const arRestoreBtn = document.getElementById('arRestoreBtn');

// Shared Processing UI: Audit Rules can make several independent reads on a cold/free-tier
// Supabase instance. Keep one page-level progress bar alive while any of those reads is active,
// with a plain-language stage message beside it so a slow response is distinguishable from a
// stalled page. The counter prevents one parallel read from hiding the indicator for another.
const arProcessingStatus = document.getElementById('arProcessingStatus');
const arProcessingProgress = document.getElementById('arProcessingProgress');
const arProcessingMessage = document.getElementById('arProcessingMessage');
let arProcessingOperations = 0;
let arGlobalLoadingToken = null;

function beginAuditRulesProcessing(message) {
    if (!arProcessingStatus || !arProcessingProgress) return;
    arProcessingOperations += 1;
    arProcessingStatus.hidden = false;
    arProcessingStatus.classList.remove('is-error');
    if (arProcessingMessage) arProcessingMessage.textContent = message || 'Loading Audit Rules…';
    if (arProcessingOperations === 1 && window.LoadingUI) {
        // Keep one reference-counted cursor token for the whole Audit Rules operation group.
        // Nested refreshes therefore keep the cursor active until the originating action and
        // every server refresh it triggered have completed.
        arGlobalLoadingToken = window.LoadingUI.startGlobalLoading();
        window.LoadingUI.showProgressBar(arProcessingProgress, {
            mode: 'simulated',
            message: 'Working…'
        });
    }
}

function endAuditRulesProcessing() {
    if (!arProcessingStatus || !arProcessingProgress) return;
    arProcessingOperations = Math.max(0, arProcessingOperations - 1);
    if (arProcessingOperations > 0) return;
    if (window.LoadingUI) {
        window.LoadingUI.hideProgressBar(arProcessingProgress);
        window.LoadingUI.stopGlobalLoading(arGlobalLoadingToken);
    }
    arGlobalLoadingToken = null;
    arProcessingStatus.hidden = true;
}

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
// Fresh load (page 1) — called on toggle change, entity-switch, and every post-save/delete/
// restore refresh. Always resets documentsOffset/documentItems, same as every other entity's
// "fresh load" reset point below.
async function loadDocuments() {
    const includeInactive = arShowInactiveCheckbox.checked;
    beginAuditRulesProcessing('Loading Documents…');
    window.LoadingUI.showSkeleton(arListBody, 6);
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/documents?include_inactive=' + includeInactive + '&limit=' + DOCUMENTS_PAGE_SIZE + '&offset=0');
        if (!res.ok) throw new Error('Failed to load documents');
        documentItems = await res.json();
        documentsOffset = documentItems.length;
        documentsTotal = parseInt(res.headers.get('X-Total-Count'), 10) || documentItems.length;
        renderDocumentList(documentItems);
        updateDocumentsLoadMoreVisibility();
    } catch (error) {
        arListBody.innerHTML = '';
        arListEmptyMessage.textContent = '❌ Error loading documents: ' + error.message;
        arListEmptyMessage.style.display = 'block';
        arLoadMoreBtn.style.display = 'none';
    } finally {
        endAuditRulesProcessing();
    }
}

// Appends the next page onto documentItems/arListBody without touching already-rendered rows,
// so the currently-selected/highlighted row (if any) isn't disturbed.
async function loadMoreDocuments() {
    const includeInactive = arShowInactiveCheckbox.checked;
    beginAuditRulesProcessing('Loading more Documents...');
    window.LoadingUI.showSpinner(arLoadMoreBtn, 'Loading');
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/documents?include_inactive=' + includeInactive + '&limit=' + DOCUMENTS_PAGE_SIZE + '&offset=' + documentsOffset);
        if (!res.ok) throw new Error('Failed to load more documents');
        const nextPage = await res.json();
        documentItems = documentItems.concat(nextPage);
        documentsOffset = documentItems.length;
        documentsTotal = parseInt(res.headers.get('X-Total-Count'), 10) || documentsTotal;
        appendDocumentRows(nextPage);
        updateDocumentsLoadMoreVisibility();
    } catch (error) {
        showDetailStatus('❌ Error loading more documents: ' + error.message, 'error');
    } finally {
        window.LoadingUI.hideSpinner(arLoadMoreBtn);
        endAuditRulesProcessing();
    }
}

function updateDocumentsLoadMoreVisibility() {
    arLoadMoreBtn.style.display = (documentItems.length < documentsTotal) ? 'block' : 'none';
}

function renderDocumentList(documents) {
    arListBody.innerHTML = '';

    if (documents.length === 0) {
        arListEmptyMessage.textContent = 'No documents found.';
        arListEmptyMessage.style.display = 'block';
        return;
    }
    arListEmptyMessage.style.display = 'none';
    appendDocumentRows(documents);
}

function appendDocumentRows(documents) {
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

// ---------- CHIP EDITORS (Authorisation Roles) ----------
// Same add/remove/render shape as client_settings.js's addPrefix/removePrefix/renderPrefixList.
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
    renderAuthorisationTable();
}

function renderAuthorisationTable() {
    const body = document.getElementById('arAuthorisationTableBody');
    if (!body) return;
    body.innerHTML = '';
    const checks = rolesState;
    checks.forEach((role, index) => {
        const row = document.createElement('tr');
        const checkCell = document.createElement('td');
        const checkInput = document.createElement('input'); checkInput.type = 'text'; checkInput.value = role; checkInput.className = 'ar-authorisation-check-input';
        checkInput.addEventListener('input', () => {
            // Keep the in-memory list synchronized even when Save is clicked before the
            // input loses focus. Authority data remains keyed to the saved role name and is
            // intentionally not rewritten on every keystroke.
            rolesState[index] = checkInput.value.trim();
        });
        checkCell.appendChild(checkInput); row.appendChild(checkCell);
        const signerCell = document.createElement('td'); signerCell.className = 'ar-authorisation-signers';
        const signers = currentDocumentSpecimens.filter(item => item.role_name === role && item.is_active);
        if (editingId) signers.forEach(signer => {
            const chip = document.createElement('span');
            chip.className = 'ar-signatory-chip';
            const name = document.createElement('span');
            name.textContent = signer.expected_signatory_name;
            chip.appendChild(name);
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'ar-signatory-remove';
            remove.textContent = '×';
            remove.title = 'Remove signatory';
            remove.setAttribute('aria-label', 'Remove signatory ' + signer.expected_signatory_name);
            remove.addEventListener('click', () => toggleSignatureSpecimenActive(signer.id, false));
            chip.appendChild(remove);
            signerCell.appendChild(chip);
        });
        if (editingId) {
            const input = document.createElement('input'); input.type = 'text'; input.placeholder = 'Add signatory name'; input.className = 'ar-signatory-input';
            const add = document.createElement('button'); add.type = 'button'; add.className = 'ar-btn-chip-add'; add.textContent = 'Add';
            add.addEventListener('click', () => {
                syncAuthorisationTableState();
                const currentRole = checkInput.value.trim();
                if (currentRole) addSignatureSpecimenPerson(editingId, currentRole, input, null);
            });
            signerCell.appendChild(input); signerCell.appendChild(add);
        }
        else { const hint = document.createElement('span'); hint.className = 'ar-save-first-hint'; hint.textContent = 'Save the document first'; signerCell.appendChild(hint); }
        row.appendChild(signerCell);
        const actionCell = document.createElement('td'); const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'ar-row-remove'; remove.textContent = '×'; remove.title = 'Remove audit check'; remove.addEventListener('click', () => { rolesState.splice(index, 1); renderAuthorisationTable(); }); actionCell.appendChild(remove); row.appendChild(actionCell); body.appendChild(row);
    });
    const addRow = document.createElement('tr'); addRow.className = 'ar-authorisation-add-row'; addRow.innerHTML = '<td colspan="3"><button type="button" class="ar-btn-chip-add">+ Add audit check</button></td>'; addRow.querySelector('button').addEventListener('click', () => { rolesState.push(''); renderAuthorisationTable(); }); body.appendChild(addRow);
    const hint = document.getElementById('arAuthorisationSaveHint'); if (hint) hint.style.display = editingId ? 'none' : 'block';
}

function syncAuthorisationTableState() {
    const body = document.getElementById('arAuthorisationTableBody');
    if (!body) return;
    const nextRoles = [];
    const seen = new Set();
    body.querySelectorAll('.ar-authorisation-check-input').forEach(input => {
        const value = input.value.trim();
        const key = value.toLowerCase();
        if (value && !seen.has(key)) {
            nextRoles.push(value);
            seen.add(key);
        }
    });
    rolesState = nextRoles;
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

// ---------- TEMPLATES (Phase 9: Document -> Data Bank template link) ----------
// Populated once at init (see INIT below), mirrors templates.js's own GET /api/templates
// fetch. Only active templates are offered, matching how the rest of the app treats
// is_active=0 templates as retired.
async function loadTemplatesForDocuments() {
    beginAuditRulesProcessing('Loading Templates…');
    if (arTemplateSelect) window.LoadingUI.showFieldLoading(arTemplateSelect);
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/templates');
        if (!res.ok) throw new Error('Failed to fetch templates');
        const templates = await res.json();
        arTemplateSelect.innerHTML = '<option value="">-- None --</option>' +
            templates.filter(t => t.is_active).map(t => `<option value="${t.id}">${t.template_name}</option>`).join('');
    } catch (error) {
        // Non-fatal: the Template field just stays at its "-- None --" default if this fails.
        console.error('Failed to load templates for Documents:', error);
    } finally {
        if (arTemplateSelect) window.LoadingUI.hideFieldLoading(arTemplateSelect);
        endAuditRulesProcessing();
    }
}

async function loadVerificationFieldsForTemplate(templateId) {
    const requestToken = ++verificationTemplateLoadToken;
    const parsedTemplateId = Number(templateId);
    if (!Number.isInteger(parsedTemplateId) || parsedTemplateId <= 0) {
        verificationTemplateLoading = false;
        resetVerificationFieldPolicy();
        return;
    }

    beginAuditRulesProcessing('Loading Template fields...');

    verificationTemplateLoading = true;
    verificationPolicyState = null;
    verificationPolicyTemplateId = null;
    verificationPolicyView = null;
    const isFormDocument = arDocumentTypeSelect.value === 'form';
    arVerificationFieldsField.style.display = isFormDocument ? 'none' : '';
    if (!isFormDocument) {
        arVerificationPolicyStatus.textContent = 'Loading Template columns...';
        arVerificationPolicyStatus.className = 'ar-verification-policy-status default';
    }

    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/templates/' + parsedTemplateId + '/columns');
        if (!res.ok) throw new Error('Failed to load Template columns');
        const columns = await res.json();
        if (requestToken !== verificationTemplateLoadToken || String(arTemplateSelect.value) !== String(parsedTemplateId)) return;
        verificationTemplateLoading = false;

        // Forms use this request only to populate their optional Linked Column dropdown. They
        // do not use the separate Verification Data Fields policy that linked-template Documents
        // use, so do not pass Form columns through that policy renderer.
        if (arDocumentTypeSelect.value === 'form') {
            formTemplateColumns = columns;
            renderFormFields();
            return;
        }

        // A newly selected Template starts with every active mapped column selected. The
        // administrator can remove individual columns before saving the Document.
        renderVerificationFieldPolicy({
            version: 1,
            template_id: parsedTemplateId,
            configured: false,
            template_columns: columns,
            core_fields: [],
            optional_fields: [],
            selected_column_ids: []
        });
        formTemplateColumns = columns;
        renderFormFields();
    } catch (error) {
        if (requestToken !== verificationTemplateLoadToken) return;
        verificationTemplateLoading = false;
        if (isFormDocument) {
            // Form templates only supply Linked Column choices; keep the separate
            // linked-template verification policy panel hidden on load failure too.
            renderFormFields();
            return;
        }
        arVerificationPolicyStatus.textContent = error.message;
        arVerificationPolicyStatus.className = 'ar-verification-policy-status error';
        arVerificationOptionalFields.innerHTML = '';
        arVerificationOptionalSelect.innerHTML = '<option value="">Unable to load Template columns</option>';
    } finally {
        endAuditRulesProcessing();
    }
}


// ---------- VERIFICATION DATA FIELDS (Phase 2) ----------
function verificationPolicyStateFromServer(policy) {
    const state = { core: {}, optional: [], brief: [] };
    (policy && policy.core_fields || []).forEach(field => {
        if (field.column_id !== null && field.column_id !== undefined) {
            state.core[field.key] = Number(field.column_id);
        }
    });
    (policy && policy.optional_fields || []).forEach((field, index) => {
        state.optional.push({
            column_id: Number(field.column_id),
            risk_level: field.risk_level || 'Medium',
            sort_order: index
        });
    });
    const selectedIds = [
        ...Object.values(state.core),
        ...state.optional.map(field => field.column_id)
    ].map(Number);
    const savedBriefIds = Array.isArray(policy?.brief_column_ids)
        ? policy.brief_column_ids.map(Number).filter(columnId => selectedIds.includes(columnId))
        : selectedIds.slice(0, 5);
    state.brief = [...new Set(savedBriefIds)].slice(0, 5);
    return state;
}

function verificationPolicyPayload() {
    if (!verificationPolicyState) return null;
    const core = {};
    Object.keys(verificationPolicyState.core || {}).forEach(key => {
        const columnId = Number(verificationPolicyState.core[key]);
        if (Number.isInteger(columnId) && columnId > 0) core[key] = columnId;
    });
    return {
        version: 1,
        core: core,
        optional: (verificationPolicyState.optional || []).map((field, index) => ({
            column_id: Number(field.column_id),
            risk_level: field.risk_level || 'Medium',
            sort_order: index
        })),
        brief_column_ids: [...new Set((verificationPolicyState.brief || []).map(Number))]
            .filter(columnId => Number.isInteger(columnId) && columnId > 0)
            .slice(0, 5)
    };
}

function renderVerificationFieldPolicy(policy) {
    if (!arVerificationFieldsField) return;
    if (policy) verificationPolicyView = policy;
    const hasTemplate = !!(policy && policy.template_id);
    const isManualDocument = ['physical_bill', 'form'].includes(arDocumentTypeSelect.value);
    arVerificationFieldsField.style.display = hasTemplate && !isManualDocument ? '' : 'none';
    arVerificationCoreFields.innerHTML = ''; arVerificationOptionalFields.innerHTML = ''; arVerificationOptionalSelect.innerHTML = '';
    if (!hasTemplate || isManualDocument) { verificationPolicyState = null; verificationPolicyTemplateId = null; verificationPolicyView = null; return; }
    verificationPolicyTemplateId = Number(policy.template_id);
    const shouldSelectAll = !verificationPolicyState && !policy.configured;
    if (!verificationPolicyState) verificationPolicyState = verificationPolicyStateFromServer(policy);
    const columns = policy.template_columns || [];
    const used = () => new Set([...Object.values(verificationPolicyState.core || {}), ...(verificationPolicyState.optional || []).map(field => field.column_id)].map(Number));
    // Default to every active mapped column. Core aliases remain in the API payload, while the
    // editor presents one simple removable list to the administrator.
    const allSelected = used();
    if (shouldSelectAll) columns.forEach(column => { if (!allSelected.has(Number(column.column_id))) verificationPolicyState.optional.push({ column_id: Number(column.column_id), risk_level: 'Medium', sort_order: verificationPolicyState.optional.length }); });
    if (shouldSelectAll && !verificationPolicyState.brief.length) {
        verificationPolicyState.brief = columns.slice(0, 5).map(column => Number(column.column_id));
    }
    const brief = () => new Set((verificationPolicyState.brief || []).map(Number));
    // Keep the saved selection model unchanged while presenting it as a scannable configuration
    // table; the Procedure modal uses the same selected columns for its row-level preview.
    const selectedColumnsTable = document.createElement('div');
    selectedColumnsTable.className = 'ar-verification-selected-table';
    selectedColumnsTable.setAttribute('role', 'table');
    selectedColumnsTable.setAttribute('aria-label', 'Selected Data Table columns');
    const tableHeader = document.createElement('div');
    tableHeader.className = 'ar-verification-selected-row ar-verification-selected-header';
    tableHeader.setAttribute('role', 'row');
    tableHeader.innerHTML = '<span role="columnheader">Data Table column</span><span role="columnheader" class="ar-verification-window-header"><span>Procedure window</span><span class="ar-verification-brief-count">Brief ' + brief().size + '/5</span></span>';
    selectedColumnsTable.appendChild(tableHeader);
    columns.forEach(column => {
        const columnId = Number(column.column_id);
        const isSelected = used().has(columnId);
        const row = document.createElement('div');
        row.className = 'ar-verification-selected-row';
        row.setAttribute('role', 'row');
        const columnCell = document.createElement('div');
        columnCell.className = 'ar-verification-selected-column';
        columnCell.setAttribute('role', 'cell');
        const columnName = document.createElement('span');
        columnName.className = 'ar-verification-selected-name';
        columnName.textContent = column.column_name;
        columnCell.appendChild(columnName);
        if (column.data_type) {
            const type = document.createElement('span');
            type.className = 'ar-verification-selected-type';
            type.textContent = column.data_type;
            columnCell.appendChild(type);
        }
        const visibilityCell = document.createElement('div');
        visibilityCell.className = 'ar-verification-selected-visibility';
        visibilityCell.setAttribute('role', 'cell');
        const visibilityButton = document.createElement('button');
        visibilityButton.type = 'button';
        visibilityButton.className = `ar-verification-show-toggle${isSelected ? ' selected' : ''}`;
        visibilityButton.textContent = 'Show';
        visibilityButton.setAttribute('aria-pressed', String(isSelected));
        visibilityButton.setAttribute('aria-label', `${isSelected ? 'Hide' : 'Show'} ${column.column_name} in the Procedure Data Table`);
        visibilityButton.title = `${isSelected ? 'Hide' : 'Show'} this column in the Procedure Data Table`;
        visibilityButton.addEventListener('click', () => {
            Object.keys(verificationPolicyState.core).forEach(key => {
                if (Number(verificationPolicyState.core[key]) === columnId) delete verificationPolicyState.core[key];
            });
            verificationPolicyState.optional = verificationPolicyState.optional.filter(field => Number(field.column_id) !== columnId);
            verificationPolicyState.brief = (verificationPolicyState.brief || []).filter(id => Number(id) !== columnId);
            if (!isSelected) {
                verificationPolicyState.optional.push({
                    column_id: columnId,
                    risk_level: 'Medium',
                    sort_order: verificationPolicyState.optional.length
                });
            }
            renderVerificationFieldPolicy({ ...policy, configured: true });
        });
        visibilityCell.appendChild(visibilityButton);
        const briefButton = document.createElement('button');
        const isBrief = brief().has(columnId);
        briefButton.type = 'button';
        briefButton.className = `ar-verification-brief-toggle${isBrief ? ' selected' : ''}`;
        briefButton.textContent = 'Brief';
        briefButton.disabled = !isSelected;
        briefButton.setAttribute('aria-pressed', String(isBrief));
        briefButton.setAttribute('aria-label', `${isBrief ? 'Remove' : 'Add'} ${column.column_name} from the brief Procedure view`);
        briefButton.title = isSelected
            ? `${isBrief ? 'Remove from' : 'Add to'} the brief Procedure view`
            : 'Show this column first using the Show control';
        briefButton.addEventListener('click', () => {
            if (!isSelected) return;
            if (isBrief) {
                verificationPolicyState.brief = (verificationPolicyState.brief || []).filter(id => Number(id) !== columnId);
            } else {
                if ((verificationPolicyState.brief || []).length >= 5) {
                    arVerificationPolicyStatus.textContent = 'Brief view is limited to 5 columns. Remove one Brief selection before adding another.';
                    arVerificationPolicyStatus.className = 'ar-verification-policy-status warning';
                    return;
                }
                verificationPolicyState.brief = [...(verificationPolicyState.brief || []), columnId];
            }
            renderVerificationFieldPolicy({ ...policy, configured: true });
        });
        visibilityCell.appendChild(briefButton);
        row.append(columnCell, visibilityCell);
        selectedColumnsTable.appendChild(row);
    });
    arVerificationOptionalFields.appendChild(selectedColumnsTable);
    const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = columns.length ? '-- Add a Template column --' : '-- No active mapped columns --'; arVerificationOptionalSelect.appendChild(placeholder);
    columns.forEach(column => { if (!used().has(Number(column.column_id))) { const option = document.createElement('option'); option.value = column.column_id; option.textContent = column.column_name; arVerificationOptionalSelect.appendChild(option); } });
    arVerificationOptionalRisk.style.display = 'none';
    arVerificationPolicyStatus.textContent = '';
    arVerificationPolicyStatus.className = 'ar-verification-policy-status default';
    arVerificationPolicyHint.style.display = 'none';
}

// ---------- FORM DOCUMENT FIELDS (Phase B) ----------
function formFieldsFromServer(fields) {
    return (fields || []).map((field, index) => ({
        field_name: field.field_name || '', field_type: field.field_type || 'text',
        field_scope: field.field_scope || 'header', linked_column: field.linked_column || '',
        arithmetic_operands: Array.isArray(field.arithmetic_operands) ? field.arithmetic_operands.slice(0, 2) : [],
        sort_order: index
    }));
}

function formFieldsPayload() {
    return formFieldsState.map((field, index) => ({
        field_name: field.field_name.trim(), field_type: field.field_type,
        field_scope: field.field_scope, linked_column: field.linked_column || null,
        arithmetic_operands: field.field_type === 'arithmetic_multiply'
            ? (field.arithmetic_operands || []).slice(0, 2) : [],
        sort_order: index
    }));
}

function arithmeticOperandCandidates(field) {
    return formFieldsState.filter(candidate => candidate !== field
        && candidate.field_type === 'numeric'
        && candidate.field_scope === field.field_scope);
}

function appendArithmeticOperandSelect(cell, field, operandIndex, candidates) {
    const select = document.createElement('select');
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = operandIndex === 0 ? '-- First field --' : '-- Second field --';
    select.appendChild(placeholder);
    candidates.forEach(candidate => {
        const option = document.createElement('option');
        option.value = candidate.field_name;
        option.textContent = candidate.field_name;
        select.appendChild(option);
    });
    const savedOperand = (field.arithmetic_operands || [])[operandIndex] || '';
    if (savedOperand && !candidates.some(candidate =>
        candidate.field_name.toLowerCase() === savedOperand.toLowerCase())) {
        const stale = document.createElement('option');
        stale.value = savedOperand;
        stale.textContent = '⚠️ ' + savedOperand + ' (unavailable)';
        select.appendChild(stale);
    }
    select.value = savedOperand;
    select.addEventListener('change', () => {
        const selected = select.value;
        const otherIndex = operandIndex === 0 ? 1 : 0;
        const other = (field.arithmetic_operands || [])[otherIndex] || '';
        if (selected && other && selected.toLowerCase() === other.toLowerCase()) {
            select.value = '';
            field.arithmetic_operands[operandIndex] = '';
            arFormFieldsHint.textContent = 'Choose two different Numeric Item-row fields for each Arithmetic — Multiply field.';
            return;
        }
        field.arithmetic_operands[operandIndex] = selected;
        arFormFieldsHint.textContent = '';
    });
    cell.appendChild(select);
    if (operandIndex === 0) {
        const multiply = document.createElement('span');
        multiply.textContent = ' × ';
        cell.appendChild(multiply);
    }
}

function renderFormFields() {
    if (!arFormFieldsField || !arFormFieldsBody) return;
    const isForm = arDocumentTypeSelect.value === 'form';
    arFormFieldsField.style.display = isForm ? '' : 'none';
    if (arDocTemplateLabel) arDocTemplateLabel.textContent = isForm
        ? 'Linked Template (optional prefill source)' : 'Template (for Supporting search)';
    if (!isForm) return;
    arFormFieldsBody.innerHTML = '';
    formFieldsState.forEach((field, index) => {
        const row = document.createElement('tr');
        const nameCell = row.insertCell();
        const name = document.createElement('input'); name.type = 'text'; name.value = field.field_name;
        name.placeholder = 'e.g. Bill No'; name.className = 'ar-form-field-name';
        name.addEventListener('input', () => {
            const previousName = field.field_name;
            field.field_name = name.value;
            if (previousName.trim()) {
                formFieldsState.forEach(candidate => {
                    candidate.arithmetic_operands = (candidate.arithmetic_operands || []).map(operand =>
                        operand && operand.toLowerCase() === previousName.trim().toLowerCase()
                            ? field.field_name : operand
                    );
                });
            }
        }); nameCell.appendChild(name);

        const typeCell = row.insertCell();
        const type = document.createElement('select');
        [['text', 'Text'], ['date', 'Date'], ['numeric', 'Numeric'],
            ['arithmetic_multiply', 'Arithmetic — Multiply']].forEach(([value, label]) => {
            const option = document.createElement('option'); option.value = value; option.textContent = label; type.appendChild(option);
        });
        type.value = field.field_type; type.addEventListener('change', () => {
            field.field_type = type.value;
            field.arithmetic_operands = [];
            if (field.field_type === 'arithmetic_multiply') {
                field.field_scope = 'item';
                field.linked_column = '';
            }
            renderFormFields();
        }); typeCell.appendChild(type);

        const isArithmetic = field.field_type === 'arithmetic_multiply';
        const operandCell = row.insertCell();
        if (isArithmetic) {
            const candidates = arithmeticOperandCandidates(field);
            appendArithmeticOperandSelect(operandCell, field, 0, candidates);
            appendArithmeticOperandSelect(operandCell, field, 1, candidates);
            if (!candidates.length) {
                const note = document.createElement('small');
                note.textContent = 'Add Numeric Item fields first.';
                operandCell.appendChild(note);
            }
        } else {
            operandCell.textContent = '—';
        }

        const scopeCell = row.insertCell();
        const scope = document.createElement('select');
        [['header', 'Header'], ['item', 'Item row']].forEach(([value, label]) => {
            const option = document.createElement('option'); option.value = value; option.textContent = label; scope.appendChild(option);
        });
        scope.value = isArithmetic ? 'item' : field.field_scope;
        scope.disabled = isArithmetic;
        scope.addEventListener('change', () => { field.field_scope = scope.value; renderFormFields(); }); scopeCell.appendChild(scope);

        const linkCell = row.insertCell();
        const link = document.createElement('select');
        const empty = document.createElement('option'); empty.value = ''; empty.textContent = formTemplateColumns.length ? '-- None --' : '-- Select a Linked Template first --'; link.appendChild(empty);
        formTemplateColumns.forEach(column => { const option = document.createElement('option'); option.value = column.column_name; option.textContent = column.column_name; link.appendChild(option); });
        if (field.linked_column && !formTemplateColumns.some(column => column.column_name === field.linked_column)) {
            const stale = document.createElement('option'); stale.value = field.linked_column; stale.textContent = '⚠️ ' + field.linked_column + ' (not on template)'; link.appendChild(stale);
        }
        link.value = field.linked_column || '';
        link.disabled = isArithmetic;
        link.addEventListener('change', () => { field.linked_column = link.value; }); linkCell.appendChild(link);

        const actions = row.insertCell();
        const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'ar-btn-delete'; remove.textContent = 'Remove';
        remove.addEventListener('click', () => { formFieldsState.splice(index, 1); renderFormFields(); }); actions.appendChild(remove);
        const up = document.createElement('button'); up.type = 'button'; up.className = 'ar-move-btn'; up.textContent = '▲'; up.title = 'Move field up'; up.disabled = index === 0;
        up.addEventListener('click', () => { [formFieldsState[index - 1], formFieldsState[index]] = [formFieldsState[index], formFieldsState[index - 1]]; renderFormFields(); }); actions.appendChild(up);
        const down = document.createElement('button'); down.type = 'button'; down.className = 'ar-move-btn'; down.textContent = '▼'; down.title = 'Move field down'; down.disabled = index === formFieldsState.length - 1;
        down.addEventListener('click', () => { [formFieldsState[index], formFieldsState[index + 1]] = [formFieldsState[index + 1], formFieldsState[index]]; renderFormFields(); }); actions.appendChild(down);
        arFormFieldsBody.appendChild(row);
    });
    arFormFieldsHint.textContent = formFieldsState.length ? '' : 'Add at least one field before using this Form in Verification.';
}

function addFormField() {
    formFieldsState.push({ field_name: '', field_type: 'text', field_scope: 'header',
        linked_column: '', arithmetic_operands: [], sort_order: formFieldsState.length });
    renderFormFields();
    arFormFieldsBody.lastElementChild?.querySelector('.ar-form-field-name')?.focus();
}

function resetVerificationFieldPolicy() {
    verificationPolicyState = null;
    verificationPolicyTemplateId = null;
    verificationPolicyView = null;
    renderVerificationFieldPolicy(null);
}

function addVerificationOptionalField() {
    if (!verificationPolicyState || !arVerificationOptionalSelect.value) return;
    const columnId = Number(arVerificationOptionalSelect.value);
    if (!Number.isInteger(columnId)) return;
    verificationPolicyState.optional.push({
        column_id: columnId,
        risk_level: arVerificationOptionalRisk.value || 'Medium',
        sort_order: verificationPolicyState.optional.length
    });
    arVerificationOptionalSelect.value = '';
    // Preserve the current server read model while redrawing the local editor state.
    // The server read model is retained on the field panel for redraws without another fetch.
    renderVerificationFieldPolicy({ ...verificationPolicyView, configured: true });
}

// ---------- DOCUMENT TYPE (Physical Bill evidence type, Phase 4 of 8) ----------
function updateDocumentTypeVisibility() {
    // A Physical Bill has no linked Data Bank template - its fields (Bill No / Date / Amount /
    // Vendor name) are fixed by type - so the Template field is hidden for it.
    const isPhysicalBill = arDocumentTypeSelect.value === 'physical_bill';
    const isForm = arDocumentTypeSelect.value === 'form';
    const display = isPhysicalBill ? 'none' : '';

    arDocTemplateField.style.display = display;
    arDocumentTypeHint.style.display = isPhysicalBill ? 'block' : 'none';
    renderFormFields();
    if (isForm && arTemplateSelect.value) loadVerificationFieldsForTemplate(arTemplateSelect.value);
    if (isPhysicalBill) resetVerificationFieldPolicy();
}

function setLegacyPhysicalBillOptionVisible(visible) {
    if (!arLegacyPhysicalBillOption) return;
    arLegacyPhysicalBillOption.hidden = !visible;
    arLegacyPhysicalBillOption.disabled = !visible;
}

// ---------- TRANSACTION LEVEL APPROVAL (new, confirmed requirement) ----------
// UI checkbox intentionally hidden (see CLAUDE.md Conventions) — this function is kept so
// reactivating the feature later is a pure markup+wiring change, no backend/schema work needed.
async function toggleDocumentTransactionLevelApproval(checked) {
    if (!editingId) return;

    beginAuditRulesProcessing('Saving approval setting...');
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
    } finally {
        endAuditRulesProcessing();
    }
}

// ---------- SELECT / NEW / RESET DETAIL PANEL ----------
async function selectDocument(id) {
    beginAuditRulesProcessing('Loading Document details...');
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/documents/' + id);
        if (!res.ok) throw new Error('Document not found');
        const doc = await res.json();
        currentDocumentRecord = doc;

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
        setLegacyPhysicalBillOptionVisible(savedDocumentType === 'physical_bill');
        formFieldsState = formFieldsFromServer(doc.form_fields);
        formTemplateColumns = [];
        updateDocumentTypeVisibility();
        verificationPolicyState = null;
        verificationPolicyTemplateId = null;
        verificationPolicyView = null;
        verificationTemplateLoading = false;
        renderVerificationFieldPolicy(doc.verification_field_policy);
        if (savedDocumentType === 'form' && arTemplateSelect.value) loadVerificationFieldsForTemplate(arTemplateSelect.value);
        rolesState = [...new Set((doc.auth_roles || []).map(role => String(role).trim()).filter(Boolean))];
        requiredAuthorityState = { ...(doc.auth_role_authorities || {}) };
        currentDocumentSpecimens = [];
        renderRoleChips();

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
    } finally {
        endAuditRulesProcessing();
    }
}

function startNewDocument() {
    editingId = null;
    currentDocumentRecord = null;
    arEditingIdInput.value = '';
    arNameInput.value = '';
    arKbNoteInput.value = '';
    arTemplateSelect.value = '';
    arRiskRatingSelect.value = '';
    // Forced choice: a brand-new Document starts with no type selected and can't be saved until
    // one is picked (see saveDocument()). No saved baseline exists yet, so no switch confirm.
    savedDocumentType = null;
    arDocumentTypeSelect.value = '';
    setLegacyPhysicalBillOptionVisible(false);
    formFieldsState = [];
    formTemplateColumns = [];
    updateDocumentTypeVisibility();
    resetVerificationFieldPolicy();
    renderFormFields();
    rolesState = ['Prepared by', 'Checked by', 'Approved by'];
    requiredAuthorityState = {};
    renderRoleChips();

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
async function saveDocument(triggerBtn) {
    syncAuthorisationTableState();
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

    const usesVerificationPolicy = documentType === 'linked_template';
    if (usesVerificationPolicy && arTemplateSelect.value && verificationTemplateLoading) {
        showDetailStatus('Please wait for the Template columns to load.', 'error');
        return;
    }
    if (usesVerificationPolicy && arTemplateSelect.value && verificationPolicyTemplateId !== parseInt(arTemplateSelect.value, 10)) {
        showDetailStatus('Template columns could not be loaded. Please reselect the Template and try again.', 'error');
        return;
    }

    // Destructive-switch guard: turning an existing linked-template Document into a Physical Bill
    // clears its template link server-side. Only asks when there's actually a template to lose.
    // A native confirm(), matching this codebase's convention for a single flat destructive action
    // (Assign Categories' force-apply, Clear Saved Mapping) - custom modals here are reserved for
    // multi-step flows.
    if (editingId && savedDocumentType === 'linked_template' && isPhysicalBill && arTemplateSelect.value) {
        const templateName = arTemplateSelect.options[arTemplateSelect.selectedIndex].text;
        const proceed = confirm(
            'Switching "' + name + '" to Physical Bill will permanently clear its linked template (' + templateName + ').\n\n' +
            'A Physical Bill has fixed fields (Bill No, Date, Amount, Vendor name), so it cannot keep a template link. ' +
            'Any Cross-check sourcing a column from this document will stop validating.\n\n' +
            'Continue?'
        );
        if (!proceed) return;
    }

    beginAuditRulesProcessing('Saving Document...');

    const payload = {
        name: name,
        kb_note: arKbNoteInput.value.trim() || null,
        // Send what will actually be stored rather than whatever the now-hidden Template control
        // still holds. The backend forces template_id NULL for a physical_bill anyway
        // (apply_document_type_constraints()), but sending the real outcome keeps the UI from
        // briefly implying a template survived the switch. cross_check_fields is no longer sent -
        // it was a read-only Master Columns mirror; the backend model defaults it to [].
        template_id: (!isPhysicalBill && arTemplateSelect.value) ? parseInt(arTemplateSelect.value) : null,
        risk_rating: arRiskRatingSelect.value || null,
        auth_roles: rolesState,
        auth_role_authorities: requiredAuthorityState,
        document_type: documentType,
        form_fields: documentType === 'form' ? formFieldsPayload() : [],
        verification_field_policy: (usesVerificationPolicy && arTemplateSelect.value &&
            verificationPolicyTemplateId === parseInt(arTemplateSelect.value, 10))
            ? verificationPolicyPayload() : null
    };

    try {
        window.LoadingUI.showSpinner(triggerBtn, 'Saving');

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
        delete categoryDocumentPolicyCache[savedId];
        delete categoryDocumentAuthorisationCache[savedId];
        await loadDocuments();
        await selectDocument(savedId);
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        window.LoadingUI.hideSpinner(triggerBtn);
        endAuditRulesProcessing();
    }
}

// ---------- DELETE / RESTORE ----------
async function deleteDocument() {
    if (!editingId) return;
    if (!confirm('Are you sure you want to delete "' + arNameInput.value + '"? This will soft-delete it.')) return;

    beginAuditRulesProcessing('Deleting Document...');
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/documents/' + editingId, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to delete document');

        showDetailStatus('✅ ' + result.message, 'success');
        await loadDocuments();
        await selectDocument(editingId);
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
    }
}

async function restoreDocument() {
    if (!editingId) return;

    beginAuditRulesProcessing('Restoring Document...');
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/documents/' + editingId + '/restore', { method: 'POST' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to restore document');

        showDetailStatus('✅ ' + result.message, 'success');
        await loadDocuments();
        await selectDocument(editingId);
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
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
let signatureSpecimenLoadToken = 0;
// The Document object last passed to loadDocumentSignatureSpecimens() — its own
// server-persisted auth_roles (not the live-editing rolesState chip array, which can be
// mid-edit/unsaved) is what drives the per-Role grouping, so add/toggle/hard-delete reloads
// reuse this instead of re-fetching the Document or trusting rolesState.
let currentSpecimensDoc = null;

async function loadDocumentSignatureSpecimens(doc) {
    const requestToken = ++signatureSpecimenLoadToken;
    currentSpecimensDoc = doc;
    beginAuditRulesProcessing('Loading signature specimens...');
    try {
        const res = await window.ApiUtils.apiFetch(
            API_BASE + '/api/audit-rules/signature-specimens?client_id=' + getSelectedClientId() +
            '&document_id=' + doc.id + '&include_inactive=false'
        );
        if (!res.ok) throw new Error('Failed to load signature specimens');
        if (requestToken !== signatureSpecimenLoadToken || currentDocumentRecord?.id !== doc.id) return;
        currentDocumentSpecimens = await res.json();
        renderDocumentSignatureSpecimensSection(doc);
        renderAuthorisationTable();
    } catch (error) {
        if (requestToken !== signatureSpecimenLoadToken || currentDocumentRecord?.id !== doc.id) return;
        arDocSignatureSpecimensContainer.innerHTML =
            '<div class="ar-list-empty">❌ Error loading signature specimens: ' + error.message + '</div>';
    } finally {
        endAuditRulesProcessing();
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

async function ensureDocumentAuthorisationRole(documentId, roleName) {
    const existingRoles = currentDocumentRecord?.auth_roles || [];
    if (existingRoles.includes(roleName)) return;
    if (!currentDocumentRecord) throw new Error('Save the document first');
    const templateId = currentDocumentRecord.template_id;
    const payload = {
        name: currentDocumentRecord.name,
        kb_note: currentDocumentRecord.kb_note || null,
        template_id: templateId,
        risk_rating: currentDocumentRecord.risk_rating || null,
        auth_roles: [...existingRoles, roleName],
        auth_role_authorities: currentDocumentRecord.auth_role_authorities || {},
        document_type: currentDocumentRecord.document_type,
        // This endpoint updates the whole Document row. Carry the saved Form definition so
        // adding a signatory cannot clear it as an unrelated side effect.
        form_fields: currentDocumentRecord.document_type === 'form'
            ? (currentDocumentRecord.form_fields || []) : [],
        verification_field_policy: templateId ? verificationPolicyPayload() : null
    };
    const res = await fetch(API_BASE + '/api/audit-rules/documents/' + documentId, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const result = await res.json();
    if (!res.ok) throw new Error(result.detail || result.message || 'Failed to save authorisation role');
    currentDocumentRecord.auth_roles = payload.auth_roles;
    if (currentSpecimensDoc) currentSpecimensDoc.auth_roles = payload.auth_roles.slice();
    rolesState = payload.auth_roles.slice();
}

async function addSignatureSpecimenPerson(documentId, roleName, inputEl, authorityInputEl) {
    const name = inputEl.value.trim();
    if (!name) return;
    const authority = authorityInputEl ? authorityInputEl.value.trim() : '';

    beginAuditRulesProcessing('Saving signature specimen...');
    try {
        await ensureDocumentAuthorisationRole(documentId, roleName);
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
        delete categoryDocumentAuthorisationCache[documentId];
        if (currentSpecimensDoc) await loadDocumentSignatureSpecimens(currentSpecimensDoc);
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
    }
}

async function toggleSignatureSpecimenActive(specimenId, activate) {
    beginAuditRulesProcessing(activate ? 'Restoring signature specimen...' : 'Deactivating signature specimen...');
    try {
        const url = API_BASE + '/api/audit-rules/signature-specimens/' + specimenId + (activate ? '/restore' : '');
        const res = await fetch(url, { method: activate ? 'POST' : 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to update signature specimen');

        showDetailStatus('✅ ' + result.message, 'success');
        if (currentSpecimensDoc) delete categoryDocumentAuthorisationCache[currentSpecimensDoc.id];
        if (currentSpecimensDoc) await loadDocumentSignatureSpecimens(currentSpecimensDoc);
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
    }
}

async function hardDeleteSignatureSpecimen(specimenId, name, role) {
    if (!confirm('Permanently delete the signature specimen for "' + name + '" (' + role + ')? This cannot be undone — use Deactivate instead for a normal personnel change.')) return;

    beginAuditRulesProcessing('Deleting signature specimen...');
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/signature-specimens/' + specimenId + '/hard-delete', { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to permanently delete signature specimen');

        showDetailStatus('✅ ' + result.message, 'success');
        if (currentSpecimensDoc) delete categoryDocumentAuthorisationCache[currentSpecimensDoc.id];
        if (currentSpecimensDoc) await loadDocumentSignatureSpecimens(currentSpecimensDoc);
    } catch (error) {
        showDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
    }
}

// ---------- WIRE UP EVENT LISTENERS ----------
arAddNewBtn.addEventListener('click', startNewDocument);
arShowInactiveCheckbox.addEventListener('change', loadDocuments);
arLoadMoreBtn.addEventListener('click', loadMoreDocuments);

arRolesAddBtn.addEventListener('click', addRole);
arRolesInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); addRole(); }
});

// Physical Bill evidence type (Phase 4 of 8): show/hide the Template field live as the type
// changes, so the form always reflects what the chosen type can actually carry.
arDocumentTypeSelect.addEventListener('change', updateDocumentTypeVisibility);
arTemplateSelect.addEventListener('change', () => {
    formTemplateColumns = [];
    if (arTemplateSelect.value) loadVerificationFieldsForTemplate(arTemplateSelect.value);
    else renderFormFields();
});
arFormAddFieldBtn?.addEventListener('click', addFormField);
arVerificationOptionalAddBtn.addEventListener('click', addVerificationOptionalField);

arSaveBtn.addEventListener('click', () => saveDocument(arSaveBtn));
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

// A rail click can happen while window.onload is still waiting for the shared Template list.
// Remember that explicit user choice so the later URL-restore pass cannot switch the page back
// to the view that was present when the document first loaded. This is page-lifetime state only:
// a fresh load starts with no user choice and restores the URL normally.
let userSelectedEntityType = null;

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

function switchEntityType(type, options) {
    const userInitiated = Boolean(options && options.userInitiated);
    if (userInitiated) userSelectedEntityType = type;

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
        beginAuditRulesProcessing('Loading Cross-checks…');
        // Refresh the Documents cache every time this view is opened, so the "add
        // document" picker and field-eligibility logic reflect any Documents-side
        // edits made since the last visit. Returned (not just fired) so the URL-restore
        // path in window.onload can await these caches before selecting a specific item.
        return loadAllDocumentsForCrossChecks().then(loadCrossChecks).finally(endAuditRulesProcessing);
    } else if (type === 'categories') {
        beginAuditRulesProcessing('Loading Categories and supporting rules…');
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
        ]).then(loadCategories).finally(endAuditRulesProcessing);
    } else if (type === 'voucher-checks') {
        beginAuditRulesProcessing('Loading Voucher-level Checks…');
        return loadVoucherChecks().finally(endAuditRulesProcessing);
    }
    // Documents are the default view, so the initial URL restore loads them separately. A
    // user click can pre-empt that restore while page-start work is still pending; reload the
    // list here so choosing Documents first never leaves its panel empty.
    return userInitiated ? loadDocuments() : Promise.resolve();
}

// ---------- DOM REFS ----------
const arCrossCheckListBody = document.getElementById('arCrossCheckListBody');
const arCrossCheckListEmptyMessage = document.getElementById('arCrossCheckListEmptyMessage');
const arCrossCheckShowInactiveCheckbox = document.getElementById('arCrossCheckShowInactiveCheckbox');
const arCrossCheckAddNewBtn = document.getElementById('arCrossCheckAddNewBtn');
const arCrossCheckLoadMoreBtn = document.getElementById('arCrossCheckLoadMoreBtn');

// Pagination (Fast Data Handling, Phase 10 of 13) — same shape as Documents' above.
const CROSS_CHECKS_PAGE_SIZE = 30;
let crossChecksOffset = 0;
let crossChecksTotal = 0;
let crossCheckItems = [];

const arCrossCheckDetailEmpty = document.getElementById('arCrossCheckDetailEmpty');
const arCrossCheckDetailForm = document.getElementById('arCrossCheckDetailForm');
const arCrossCheckEditingIdInput = document.getElementById('arCrossCheckEditingId');
const arCrossCheckNameInput = document.getElementById('arCrossCheckNameInput');
const arMatrixAddColumnSelect = document.getElementById('arMatrixAddColumnSelect');
const arMatrixAddColumnBtn = document.getElementById('arMatrixAddColumnBtn');
const arMatrixTable = document.getElementById('arMatrixTable');
const arMatrixAddRowBtn = document.getElementById('arMatrixAddRowBtn');
const arMatrixWarnings = document.getElementById('arMatrixWarnings');
const arMatrixNeedsSaveHint = document.getElementById('arMatrixNeedsSaveHint');
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
// Cross-Check Matrix Redesign — the matrix editor's state, loaded from and saved to the backend
// (Phase 5). Each edit persists immediately through its own endpoint.
//   columns: ordered [{ documentId, name }] — the audit_rule_cross_check_documents axis.
//   rows: [{ id, cells: { <documentId>: <columnName> } }] — id IS the backend field_id (from
//         GET .../matrix or POST .../fields); cells omit not-applicable (null) entries.
let matrixState = { columns: [], rows: [] };
// Session cache: documentId -> [columnName, ...] from that document's linked template. Populated
// by fetchMatrixColumnsForDoc(); reused so adding several rows against the same document doesn't
// re-fetch. Never invalidated in-session (page reload only).
let matrixColumnsByDoc = {};
let matrixColumnOptionsByDoc = {};
// Cache of all active Documents ({id, name, ...}), used to resolve column names and populate the
// "Add document" picker. Refreshed each time the Cross-checks view is opened (switchEntityType()).
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
    beginAuditRulesProcessing('Loading Documents for Cross-checks…');
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/documents?include_inactive=false');
        if (!res.ok) throw new Error('Failed to load documents');
        allDocuments = await res.json();
    } catch (error) {
        allDocuments = [];
        showCrossCheckDetailStatus('❌ Error loading documents: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
    }
}

function getDocumentById(id) {
    return allDocuments.find(d => d.id === id);
}

// ---------- LOAD & RENDER LIST ----------
async function loadCrossChecks() {
    const includeInactive = arCrossCheckShowInactiveCheckbox.checked;
    beginAuditRulesProcessing('Loading Cross-checks…');
    window.LoadingUI.showSkeleton(arCrossCheckListBody, 6);
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/cross-checks?include_inactive=' + includeInactive + '&limit=' + CROSS_CHECKS_PAGE_SIZE + '&offset=0');
        if (!res.ok) throw new Error('Failed to load cross-checks');
        crossCheckItems = await res.json();
        crossChecksOffset = crossCheckItems.length;
        crossChecksTotal = parseInt(res.headers.get('X-Total-Count'), 10) || crossCheckItems.length;
        renderCrossCheckList(crossCheckItems);
        updateCrossChecksLoadMoreVisibility();
    } catch (error) {
        arCrossCheckListBody.innerHTML = '';
        arCrossCheckListEmptyMessage.textContent = '❌ Error loading cross-checks: ' + error.message;
        arCrossCheckListEmptyMessage.style.display = 'block';
        arCrossCheckLoadMoreBtn.style.display = 'none';
    } finally {
        endAuditRulesProcessing();
    }
}

async function loadMoreCrossChecks() {
    const includeInactive = arCrossCheckShowInactiveCheckbox.checked;
    beginAuditRulesProcessing('Loading more Cross-checks...');
    window.LoadingUI.showSpinner(arCrossCheckLoadMoreBtn, 'Loading');
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/cross-checks?include_inactive=' + includeInactive + '&limit=' + CROSS_CHECKS_PAGE_SIZE + '&offset=' + crossChecksOffset);
        if (!res.ok) throw new Error('Failed to load more cross-checks');
        const nextPage = await res.json();
        crossCheckItems = crossCheckItems.concat(nextPage);
        crossChecksOffset = crossCheckItems.length;
        crossChecksTotal = parseInt(res.headers.get('X-Total-Count'), 10) || crossChecksTotal;
        appendCrossCheckRows(nextPage);
        updateCrossChecksLoadMoreVisibility();
    } catch (error) {
        showCrossCheckDetailStatus('❌ Error loading more cross-checks: ' + error.message, 'error');
    } finally {
        window.LoadingUI.hideSpinner(arCrossCheckLoadMoreBtn);
        endAuditRulesProcessing();
    }
}

function updateCrossChecksLoadMoreVisibility() {
    arCrossCheckLoadMoreBtn.style.display = (crossCheckItems.length < crossChecksTotal) ? 'block' : 'none';
}

function renderCrossCheckList(crossChecks) {
    arCrossCheckListBody.innerHTML = '';

    if (crossChecks.length === 0) {
        arCrossCheckListEmptyMessage.textContent = 'No cross-checks found.';
        arCrossCheckListEmptyMessage.style.display = 'block';
        return;
    }
    arCrossCheckListEmptyMessage.style.display = 'none';
    appendCrossCheckRows(crossChecks);
}

function appendCrossCheckRows(crossChecks) {
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

// ---------- COMPARISON MATRIX (Cross-Check Matrix Redesign, Phase 4 of 5) ----------
// Documents are columns, field rows have no label (positional), each cell picks a column name
// from that document's linked template. In-memory / mock state only this phase — renderMatrix()
// draws matrixState; nothing here talks to the matrix endpoints yet (Phase 5).

// documentId -> Promise, while that document's column list is being fetched. Guards the fetch
// chokepoint so repeated renders (add column, add row, …) before the first fetch resolves reuse
// the one in-flight request rather than each spawning a duplicate.
let matrixColumnsInFlight = {};

// documentId -> [fieldName, ...] for the Document's linked-template or Form field contract.
// Form fields remain the saved evidence names; linked_column is display metadata only.
function fetchMatrixColumnsForDoc(documentId) {
    if (Object.prototype.hasOwnProperty.call(matrixColumnsByDoc, documentId)) {
        return Promise.resolve(matrixColumnsByDoc[documentId]);
    }
    if (matrixColumnsInFlight[documentId]) {
        return matrixColumnsInFlight[documentId];
    }
    const promise = (async () => {
        beginAuditRulesProcessing('Loading Cross-check fields...');
        try {
            const docRes = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/documents/' + documentId);
            if (!docRes.ok) throw new Error('document lookup failed');
            const doc = await docRes.json();
            if (doc.document_type === 'form') {
                const fields = (doc.cross_check_field_specs || doc.form_fields || []).map(field => ({
                    field_name: field.field_name,
                    label: field.linked_column
                        ? field.field_name + ' — linked: ' + field.linked_column
                        : field.field_name,
                }));
                matrixColumnOptionsByDoc[documentId] = fields;
                matrixColumnsByDoc[documentId] = fields.map(field => field.field_name);
                return matrixColumnsByDoc[documentId];
            }
            if (doc.document_type === 'physical_bill') {
                const fields = (doc.cross_check_field_specs || []).map(field => ({
                    field_name: field.field_name,
                    label: field.field_name,
                }));
                matrixColumnOptionsByDoc[documentId] = fields;
                matrixColumnsByDoc[documentId] = fields.map(field => field.field_name);
                return matrixColumnsByDoc[documentId];
            }
            if (!doc.template_id) {
                matrixColumnOptionsByDoc[documentId] = [];
                matrixColumnsByDoc[documentId] = [];
                return [];
            }
            const colRes = await window.ApiUtils.apiFetch(API_BASE + '/api/templates/' + doc.template_id + '/columns');
            if (!colRes.ok) throw new Error('template columns lookup failed');
            const cols = await colRes.json();
            matrixColumnOptionsByDoc[documentId] = cols.map(c => ({
                field_name: c.column_name,
                label: c.column_name,
            }));
            matrixColumnsByDoc[documentId] = matrixColumnOptionsByDoc[documentId].map(field => field.field_name);
        } catch (error) {
            console.error('Failed to load matrix columns for document ' + documentId + ':', error);
            matrixColumnOptionsByDoc[documentId] = [];
            matrixColumnsByDoc[documentId] = [];
        } finally {
            delete matrixColumnsInFlight[documentId];
            endAuditRulesProcessing();
        }
        return matrixColumnsByDoc[documentId];
    })();
    matrixColumnsInFlight[documentId] = promise;
    return promise;
}

// Kick off a fetch for any column whose list isn't cached yet, re-rendering once each resolves.
// fetchMatrixColumnsForDoc() dedupes, so calling this on every render is cheap.
function ensureMatrixColumnsLoaded() {
    matrixState.columns.forEach(col => {
        if (!Object.prototype.hasOwnProperty.call(matrixColumnsByDoc, col.documentId)) {
            fetchMatrixColumnsForDoc(col.documentId).then(renderMatrix);
        }
    });
}

function countRowSelections(row) {
    return matrixState.columns.reduce(
        (n, col) => n + (row.cells[col.documentId] ? 1 : 0), 0
    );
}

const MATRIX_OPERATOR_LABELS = {
    '=': 'Equal to',
    '>=': 'Greater than or equal to',
    '<=': 'Less than or equal to',
    '>': 'Greater than',
    '<': 'Less than',
    '!=': 'Does not equal'
};
const MATRIX_MISSING_LABELS = {
    not_configured: 'Not configured',
    not_linked: 'Not linked',
    missing: 'Missing data',
    shortfall: 'Shortfall'
};

function matrixSelect(options, selected, className) {
    const select = document.createElement('select');
    select.className = className || 'ar-matrix-condition-control';
    options.forEach(option => {
        const item = document.createElement('option');
        item.value = option.value;
        item.textContent = option.label;
        select.appendChild(item);
    });
    select.value = selected;
    return select;
}

function matrixConditionOptions(labels) {
    return Object.entries(labels).map(([value, label]) => ({ value, label }));
}

function renderMatrix() {
    arMatrixTable.innerHTML = '';
    arMatrixWarnings.innerHTML = '';

    // A brand-new Cross-check has no id yet, so its matrix rows/columns/cells have nothing to
    // persist against. Gate the whole editor until Name + Comparison Type are saved (which
    // creates the row) — see saveCrossCheck().
    if (!editingCrossCheckId) {
        arMatrixNeedsSaveHint.textContent = 'Save this Cross-check before building its comparison matrix.';
        arMatrixNeedsSaveHint.style.display = 'block';
        arMatrixAddColumnSelect.innerHTML = '';
        arMatrixAddColumnBtn.disabled = true;
        arMatrixAddRowBtn.disabled = true;
        return;
    }
    arMatrixNeedsSaveHint.style.display = 'none';
    renderMatrixAddColumnPicker();

    if (matrixState.columns.length === 0) {
        const tr = arMatrixTable.insertRow();
        const td = tr.insertCell();
        td.className = 'ar-matrix-empty';
        td.textContent = 'Add a document to start the matrix.';
        arMatrixAddRowBtn.disabled = true;
        return;
    }
    arMatrixAddRowBtn.disabled = false;

    // Header: corner + one cell per document column (name + remove-column button).
    const thead = arMatrixTable.createTHead();
    const headRow = thead.insertRow();
    const corner = document.createElement('th');
    corner.className = 'ar-matrix-corner';
    headRow.appendChild(corner);
    matrixState.columns.forEach(col => {
        const th = document.createElement('th');
        const nameSpan = document.createElement('span');
        nameSpan.textContent = col.name;
        th.appendChild(nameSpan);
        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'ar-remove-doc-btn';
        removeBtn.textContent = '✕';
        removeBtn.title = 'Remove this document column';
        removeBtn.addEventListener('click', () => removeMatrixColumn(col.documentId));
        th.appendChild(removeBtn);
        headRow.appendChild(th);
    });
    ['Operator', 'Tolerance', 'Missing data', 'Blocking', 'Risk', ''].forEach(label => {
        const th = document.createElement('th');
        th.textContent = label;
        th.className = 'ar-matrix-condition-heading';
        headRow.appendChild(th);
    });

    const tbody = arMatrixTable.createTBody();
    if (matrixState.rows.length === 0) {
        const tr = tbody.insertRow();
        const td = tr.insertCell();
        td.colSpan = matrixState.columns.length + 2;
        td.className = 'ar-matrix-empty';
        td.textContent = 'No field rows yet — click "+ Add field row".';
    }

    matrixState.rows.forEach((row, index) => {
        const tr = tbody.insertRow();

        // Lead cell: 1-based position + reorder controls (no row label, by design).
        const lead = tr.insertCell();
        lead.className = 'ar-matrix-row-ord';
        const ord = document.createElement('span');
        ord.textContent = String(index + 1);
        lead.appendChild(ord);
        const upBtn = document.createElement('button');
        upBtn.type = 'button';
        upBtn.className = 'ar-move-btn';
        upBtn.textContent = '▲';
        upBtn.title = 'Move row up';
        upBtn.disabled = (index === 0);
        upBtn.addEventListener('click', () => moveMatrixRow(row.id, -1));
        lead.appendChild(upBtn);
        const downBtn = document.createElement('button');
        downBtn.type = 'button';
        downBtn.className = 'ar-move-btn';
        downBtn.textContent = '▼';
        downBtn.title = 'Move row down';
        downBtn.disabled = (index === matrixState.rows.length - 1);
        downBtn.addEventListener('click', () => moveMatrixRow(row.id, 1));
        lead.appendChild(downBtn);

        // One cell per document column: a dropdown of that document's template columns.
        matrixState.columns.forEach(col => {
            const td = tr.insertCell();
            const select = document.createElement('select');
            select.className = 'ar-matrix-cell-select';

            const naOpt = document.createElement('option');
            naOpt.value = '';
            naOpt.textContent = '— not applicable —';
            select.appendChild(naOpt);

            const cached = matrixColumnsByDoc[col.documentId];
            const cachedOptions = matrixColumnOptionsByDoc[col.documentId] ||
                (cached || []).map(fieldName => ({field_name: fieldName, label: fieldName}));
            if (cached === undefined) {
                const loadingOpt = document.createElement('option');
                loadingOpt.textContent = 'Loading…';
                loadingOpt.disabled = true;
                select.appendChild(loadingOpt);
                select.disabled = true;
            } else if (cached.length === 0) {
                const noneOpt = document.createElement('option');
                noneOpt.textContent = '(document has no linked template)';
                noneOpt.disabled = true;
                select.appendChild(noneOpt);
            } else {
                cachedOptions.forEach(option => {
                    const opt = document.createElement('option');
                    opt.value = option.field_name;
                    opt.textContent = option.label;
                    select.appendChild(opt);
                });
            }

            const current = row.cells[col.documentId];
            if (current && cached && !cached.includes(current)) {
                // A stored value no longer on the template — keep it visible + flagged.
                const staleOpt = document.createElement('option');
                staleOpt.value = current;
                staleOpt.textContent = '⚠️ ' + current + ' (not on this template)';
                select.appendChild(staleOpt);
            }
            select.value = current || '';
            if (!current) td.classList.add('ar-matrix-cell-na');

            select.addEventListener('change', () => {
                persistMatrixCell(row.id, col.documentId, select.value,
                    reverted => { select.value = (reverted === undefined ? '' : reverted); });
            });
            td.appendChild(select);
        });

        const operatorCell = tr.insertCell();
        const operatorSelect = matrixSelect(
            matrixConditionOptions(MATRIX_OPERATOR_LABELS), row.operator || '=',
            'ar-matrix-condition-control'
        );
        operatorSelect.addEventListener('change', () => updateMatrixCondition(row.id, {
            operator: operatorSelect.value
        }));
        operatorCell.appendChild(operatorSelect);

        const toleranceCell = tr.insertCell();
        const toleranceInput = document.createElement('input');
        toleranceInput.type = 'number';
        toleranceInput.min = '0';
        toleranceInput.step = 'any';
        toleranceInput.value = row.tolerance == null ? '0' : row.tolerance;
        toleranceInput.className = 'ar-matrix-condition-control';
        toleranceInput.title = 'Allowed numeric difference';
        toleranceInput.addEventListener('change', () => updateMatrixCondition(row.id, {
            tolerance: Math.max(0, Number(toleranceInput.value) || 0)
        }));
        toleranceCell.appendChild(toleranceInput);

        const missingCell = tr.insertCell();
        const missingSelect = matrixSelect(
            matrixConditionOptions(MATRIX_MISSING_LABELS), row.missing_data_behavior || 'not_configured',
            'ar-matrix-condition-control'
        );
        missingSelect.addEventListener('change', () => updateMatrixCondition(row.id, {
            missing_data_behavior: missingSelect.value
        }));
        missingCell.appendChild(missingSelect);

        const blockingCell = tr.insertCell();
        const blockingLabel = document.createElement('label');
        blockingLabel.className = 'ar-matrix-blocking-control';
        const blockingInput = document.createElement('input');
        blockingInput.type = 'checkbox';
        blockingInput.checked = !!row.blocking;
        blockingInput.addEventListener('change', () => updateMatrixCondition(row.id, {
            blocking: blockingInput.checked
        }));
        blockingLabel.appendChild(blockingInput);
        blockingLabel.appendChild(document.createTextNode('Block'));
        blockingCell.appendChild(blockingLabel);

        const riskCell = tr.insertCell();
        const riskSelect = matrixSelect(
            matrixConditionOptions({ Low: 'Low', Medium: 'Medium', High: 'High' }),
            row.risk || 'Medium', 'ar-matrix-condition-control'
        );
        riskSelect.addEventListener('change', () => updateMatrixCondition(row.id, {
            risk: riskSelect.value
        }));
        riskCell.appendChild(riskSelect);

        // Trailing cell: remove this row.
        const removeCell = tr.insertCell();
        const removeRowBtn = document.createElement('button');
        removeRowBtn.type = 'button';
        removeRowBtn.className = 'ar-remove-doc-btn';
        removeRowBtn.textContent = '✕';
        removeRowBtn.title = 'Remove this field row';
        removeRowBtn.addEventListener('click', () => removeMatrixRow(row.id));
        removeCell.appendChild(removeRowBtn);

        // Non-blocking warning: a row that compares fewer than 2 documents validates nothing.
        if (countRowSelections(row) < 2) {
            const warn = document.createElement('div');
            warn.className = 'ar-field-warning';
            warn.textContent = 'Row ' + (index + 1) + ' compares fewer than 2 documents — it won’t validate anything.';
            arMatrixWarnings.appendChild(warn);
        }
    });

    ensureMatrixColumnsLoaded();
}

// Rebuild the "Add document" picker to exclude documents already used as columns.
function renderMatrixAddColumnPicker() {
    arMatrixAddColumnSelect.innerHTML = '';
    const usedIds = matrixState.columns.map(c => c.documentId);
    const available = allDocuments.filter(d => !usedIds.includes(d.id));
    if (available.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.disabled = true;
        opt.selected = true;
        opt.textContent = allDocuments.length === 0 ? 'No documents exist yet' : 'All documents added';
        arMatrixAddColumnSelect.appendChild(opt);
        arMatrixAddColumnBtn.disabled = true;
    } else {
        available.forEach(d => {
            const opt = document.createElement('option');
            opt.value = d.id;
            opt.textContent = d.name;
            arMatrixAddColumnSelect.appendChild(opt);
        });
        arMatrixAddColumnBtn.disabled = false;
    }
}

// Every matrix mutation persists immediately. Helper: run the write, keep the optimistic local
// change on success, roll it back (via a caller-supplied undo) + surface the error on failure.
async function matrixPersist(doRequest, undo) {
    beginAuditRulesProcessing('Saving Cross-check matrix change...');
    try {
        const res = await doRequest();
        const result = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(result.detail || result.message || 'Request failed');
        if (typeof categoryCrossCheckDetailCache !== 'undefined' && editingCrossCheckId) {
            delete categoryCrossCheckDetailCache[editingCrossCheckId];
        }
        return result;
    } catch (error) {
        if (undo) undo();
        renderMatrix();
        showCrossCheckDetailStatus('❌ ' + error.message, 'error');
        return null;
    } finally {
        endAuditRulesProcessing();
    }
}

function matrixDocumentIds() {
    return matrixState.columns.map(c => c.documentId);
}

async function addMatrixColumn() {
    const value = arMatrixAddColumnSelect.value;
    if (!value || !editingCrossCheckId) return;
    const documentId = parseInt(value, 10);
    const doc = getDocumentById(documentId);
    matrixState.columns.push({ documentId: documentId, name: doc ? doc.name : ('Document #' + documentId) });
    fetchMatrixColumnsForDoc(documentId).then(renderMatrix);
    renderMatrix();
    await matrixPersist(
        () => fetch(API_BASE + '/api/audit-rules/cross-checks/' + editingCrossCheckId + '/documents', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ document_ids: matrixDocumentIds() })
        }),
        () => { matrixState.columns = matrixState.columns.filter(c => c.documentId !== documentId); }
    );
}

async function removeMatrixColumn(documentId) {
    if (!editingCrossCheckId) return;
    const prevColumns = matrixState.columns.slice();
    const removedCells = matrixState.rows.map(row => row.cells[documentId]);
    matrixState.columns = matrixState.columns.filter(c => c.documentId !== documentId);
    matrixState.rows.forEach(row => { delete row.cells[documentId]; });
    renderMatrix();
    await matrixPersist(
        () => fetch(API_BASE + '/api/audit-rules/cross-checks/' + editingCrossCheckId + '/documents', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ document_ids: matrixDocumentIds() })
        }),
        () => {
            matrixState.columns = prevColumns;
            matrixState.rows.forEach((row, i) => { if (removedCells[i]) row.cells[documentId] = removedCells[i]; });
        }
    );
}

async function addMatrixRow() {
    if (!editingCrossCheckId) return;
    const result = await matrixPersist(
        () => fetch(API_BASE + '/api/audit-rules/cross-checks/' + editingCrossCheckId + '/fields', { method: 'POST' })
    );
    if (result && result.field_id != null) {
        matrixState.rows.push({
            id: result.field_id,
            cells: {},
            operator: result.operator || '=',
            tolerance: result.tolerance || 0,
            missing_data_behavior: result.missing_data_behavior || 'not_configured',
            blocking: !!result.blocking,
            risk: result.risk || 'Medium'
        });
        renderMatrix();
    }
}

async function removeMatrixRow(fieldId) {
    if (!editingCrossCheckId) return;
    const idx = matrixState.rows.findIndex(r => r.id === fieldId);
    if (idx < 0) return;
    const removed = matrixState.rows[idx];
    matrixState.rows.splice(idx, 1);
    renderMatrix();
    await matrixPersist(
        () => fetch(API_BASE + '/api/audit-rules/cross-check-fields/' + fieldId, { method: 'DELETE' }),
        () => { matrixState.rows.splice(idx, 0, removed); }
    );
}

async function moveMatrixRow(fieldId, direction) {
    if (!editingCrossCheckId) return;
    const i = matrixState.rows.findIndex(r => r.id === fieldId);
    const j = i + direction;
    if (i < 0 || j < 0 || j >= matrixState.rows.length) return;
    const prevOrder = matrixState.rows.slice();
    [matrixState.rows[i], matrixState.rows[j]] = [matrixState.rows[j], matrixState.rows[i]];
    renderMatrix();
    await matrixPersist(
        () => fetch(API_BASE + '/api/audit-rules/cross-checks/' + editingCrossCheckId + '/field-order', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ field_ids: matrixState.rows.map(r => r.id) })
        }),
        () => { matrixState.rows = prevOrder; }
    );
}

// Persist one cell (called from the <select> change handler). value '' clears it (not applicable).
async function persistMatrixCell(fieldId, documentId, value, revert) {
    const row = matrixState.rows.find(r => r.id === fieldId);
    if (!row) return;
    const prev = row.cells[documentId];
    if (value) { row.cells[documentId] = value; } else { delete row.cells[documentId]; }
    renderMatrix();
    await matrixPersist(
        () => fetch(API_BASE + '/api/audit-rules/cross-check-fields/' + fieldId + '/cells/' + documentId, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ column_name: value || null })
        }),
        () => {
            if (prev === undefined) { delete row.cells[documentId]; } else { row.cells[documentId] = prev; }
            if (revert) revert(prev);
        }
    );
}

async function updateMatrixCondition(fieldId, changes) {
    const row = matrixState.rows.find(item => item.id === fieldId);
    if (!row) return;
    const previous = {
        operator: row.operator || '=',
        tolerance: row.tolerance || 0,
        missing_data_behavior: row.missing_data_behavior || 'not_configured',
        blocking: !!row.blocking,
        risk: row.risk || 'Medium'
    };
    Object.assign(row, changes);
    renderMatrix();
    await matrixPersist(
        () => fetch(API_BASE + '/api/audit-rules/cross-check-fields/' + fieldId + '/condition', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                operator: row.operator,
                tolerance: row.tolerance,
                missing_data_behavior: row.missing_data_behavior,
                blocking: row.blocking,
                risk: row.risk
            })
        }),
        () => Object.assign(row, previous)
    );
}

// Load the full matrix for a Cross-check into matrixState (columns + field rows + cells).
function loadMatrixFromServer(matrix) {
    matrixState = {
        columns: (matrix.documents || []).map(d => ({
            documentId: d.document_id,
            name: d.name || ('Document #' + d.document_id)
        })),
        rows: (matrix.fields || []).map(f => {
            const cells = {};
            Object.entries(f.cells || {}).forEach(([docId, colName]) => {
                if (colName) cells[parseInt(docId, 10)] = colName;
            });
            return {
                id: f.id,
                cells: cells,
                operator: f.operator || '=',
                tolerance: f.tolerance || 0,
                missing_data_behavior: f.missing_data_behavior || 'not_configured',
                blocking: !!f.blocking,
                risk: f.risk || 'Medium'
            };
        })
    };
    matrixState.columns.forEach(c => fetchMatrixColumnsForDoc(c.documentId).then(renderMatrix));
    renderMatrix();
}

function resetMatrix() {
    matrixState = { columns: [], rows: [] };
    renderMatrix();
}


// ---------- COMPARE TO VOUCHER LINE (Phase 5 of 10) ----------
// The anchor document is never picked here - it's resolved live at evaluation time (Phase 6).
// Just a static explanatory hint shown while the box is ticked.
function updateCompareToVoucherLineHint() {
    if (!arCompareToVoucherLineCheckbox.checked) {
        arCompareToVoucherLineHint.style.display = 'none';
        return;
    }
    arCompareToVoucherLineHint.textContent =
        'ℹ️ Also compares the anchor document against this voucher line’s own amount. ' +
        'The anchor is resolved live at evaluation time, not configured here.';
    arCompareToVoucherLineHint.style.display = 'block';
}

// ---------- SELECT / NEW / RESET DETAIL PANEL ----------
async function selectCrossCheck(id) {
    beginAuditRulesProcessing('Loading Cross-check details...');
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/cross-checks/' + id);
        if (!res.ok) throw new Error('Cross-check not found');
        const cc = await res.json();

        editingCrossCheckId = cc.id;
        arCrossCheckEditingIdInput.value = cc.id;
        arCrossCheckNameInput.value = cc.name;
        arComparisonTypeSelect.value = cc.comparison_type;
        arCompareToVoucherLineCheckbox.checked = !!cc.compare_to_voucher_line;
        updateCompareToVoucherLineHint();

        // Load the full matrix (columns + field rows + cells) from the backend.
        const matrixRes = await window.ApiUtils.apiFetch(
            API_BASE + '/api/audit-rules/cross-checks/' + id + '/matrix');
        if (!matrixRes.ok) throw new Error('Failed to load matrix');
        loadMatrixFromServer(await matrixRes.json());

        arCrossCheckUsedByRow.style.display = 'block';
        arCrossCheckUsedByCount.textContent = cc.used_by_categories_count;

        arCrossCheckSaveBtn.style.display = 'inline-block';
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
    } finally {
        endAuditRulesProcessing();
    }
}

function startNewCrossCheck() {
    editingCrossCheckId = null;
    arCrossCheckEditingIdInput.value = '';
    arCrossCheckNameInput.value = '';
    resetMatrix();  // renders the matrix in its disabled "save this cross-check first" state
    // Comparison Type is retained only as a legacy compatibility value. New reusable
    // conditions are configured row-by-row in the matrix below.
    arComparisonTypeSelect.value = 'All equal';
    arCompareToVoucherLineCheckbox.checked = false;
    updateCompareToVoucherLineHint();

    arCrossCheckUsedByRow.style.display = 'none';
    arCrossCheckSaveBtn.style.display = 'inline-block';
    arCrossCheckDeleteBtn.style.display = 'none';
    arCrossCheckRestoreBtn.style.display = 'none';

    arCrossCheckDetailEmpty.style.display = 'none';
    arCrossCheckDetailForm.style.display = 'block';
    arCrossCheckNameInput.focus();

    updateAuditRulesUrl();
}

// ---------- SAVE ----------
// Cross-Check Matrix Redesign, Phase 5 of 5. The matrix (columns / rows / cells) persists itself
// immediately through its own endpoints. Save here only handles the scalar fields:
//   - new Cross-check: POST creates the row (a filler compare_field is sent so the deferred
//     flat-model validation passes with document_ids=[]), then selectCrossCheck() reopens it
//     with an editable, empty matrix.
//   - existing: PUT .../meta updates name / comparison type / voucher-line flag only.
async function saveCrossCheck(triggerBtn) {
    const name = arCrossCheckNameInput.value.trim();
    if (!name) {
        showCrossCheckDetailStatus('❌ Name is required.', 'error');
        return;
    }

    beginAuditRulesProcessing('Saving Cross-check...');
    try {
        window.LoadingUI.showSpinner(triggerBtn, 'Saving');
        let res, result;
        if (editingCrossCheckId) {
            res = await fetch(API_BASE + '/api/audit-rules/cross-checks/' + editingCrossCheckId + '/meta', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: name,
                    // Legacy field retained until Verification is migrated; the matrix rows
                    // now own the actual reusable comparison conditions.
                    comparison_type: 'All equal',
                    compare_to_voucher_line: arCompareToVoucherLineCheckbox.checked
                })
            });
            result = await res.json();
            if (!res.ok) throw new Error(result.detail || result.message || 'Failed to save cross-check');
            if (typeof categoryCrossCheckDetailCache !== 'undefined') {
                delete categoryCrossCheckDetailCache[editingCrossCheckId];
            }
            showCrossCheckDetailStatus('✅ ' + result.message, 'success');
            await loadCrossChecks();
        } else {
            res = await fetch(API_BASE + '/api/audit-rules/cross-checks', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: name,
                    comparison_type: 'All equal',
                    compare_to_voucher_line: arCompareToVoucherLineCheckbox.checked,
                    compare_field: '(matrix)',  // filler: deferred flat model still requires it
                    document_ids: [],
                    document_fields: {}
                })
            });
            result = await res.json();
            if (!res.ok) throw new Error(result.detail || result.message || 'Failed to create cross-check');
            showCrossCheckDetailStatus('✅ ' + result.message, 'success');
            await loadCrossChecks();
            await selectCrossCheck(result.cross_check_id);
        }
    } catch (error) {
        showCrossCheckDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        window.LoadingUI.hideSpinner(triggerBtn);
        endAuditRulesProcessing();
    }
}

// ---------- DELETE / RESTORE ----------
async function deleteCrossCheck() {
    if (!editingCrossCheckId) return;
    if (!confirm('Are you sure you want to delete "' + arCrossCheckNameInput.value + '"? This will soft-delete it.')) return;

    beginAuditRulesProcessing('Deleting Cross-check...');
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/cross-checks/' + editingCrossCheckId, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to delete cross-check');
        if (typeof categoryCrossCheckDetailCache !== 'undefined') {
            delete categoryCrossCheckDetailCache[editingCrossCheckId];
        }

        showCrossCheckDetailStatus('✅ ' + result.message, 'success');
        await loadCrossChecks();
        await selectCrossCheck(editingCrossCheckId);
    } catch (error) {
        showCrossCheckDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
    }
}

async function restoreCrossCheck() {
    if (!editingCrossCheckId) return;

    beginAuditRulesProcessing('Restoring Cross-check...');
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/cross-checks/' + editingCrossCheckId + '/restore', { method: 'POST' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to restore cross-check');
        if (typeof categoryCrossCheckDetailCache !== 'undefined') {
            delete categoryCrossCheckDetailCache[editingCrossCheckId];
        }

        showCrossCheckDetailStatus('✅ ' + result.message, 'success');
        await loadCrossChecks();
        await selectCrossCheck(editingCrossCheckId);
    } catch (error) {
        showCrossCheckDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
    }
}

// ---------- WIRE UP EVENT LISTENERS ----------
arRailDocuments.addEventListener('click', () => switchEntityType('documents', { userInitiated: true }));
arRailCrossChecks.addEventListener('click', () => switchEntityType('cross-checks', { userInitiated: true }));
arRailCategories.addEventListener('click', () => switchEntityType('categories', { userInitiated: true }));
arRailVoucherChecks.addEventListener('click', () => switchEntityType('voucher-checks', { userInitiated: true }));

arCrossCheckAddNewBtn.addEventListener('click', startNewCrossCheck);
arCrossCheckShowInactiveCheckbox.addEventListener('change', loadCrossChecks);
arCrossCheckLoadMoreBtn.addEventListener('click', loadMoreCrossChecks);

arMatrixAddColumnBtn.addEventListener('click', addMatrixColumn);
arMatrixAddRowBtn.addEventListener('click', addMatrixRow);
arCompareToVoucherLineCheckbox.addEventListener('change', updateCompareToVoucherLineHint);

arCrossCheckSaveBtn.addEventListener('click', () => saveCrossCheck(arCrossCheckSaveBtn));
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
const arCategoryLoadMoreBtn = document.getElementById('arCategoryLoadMoreBtn');
const CATEGORIES_PAGE_SIZE = 30;

const arCategoryDetailEmpty = document.getElementById('arCategoryDetailEmpty');
const arCategoryDetailForm = document.getElementById('arCategoryDetailForm');
const arCategoryEditingIdInput = document.getElementById('arCategoryEditingId');
const arCategoryNameInput = document.getElementById('arCategoryNameInput');
const arCategoryKbOverviewInput = document.getElementById('arCategoryKbOverviewInput');
const arCategoryDocsList = document.getElementById('arCategoryDocsList');
const arCategoryAddDocumentSelect = document.getElementById('arCategoryAddDocumentSelect');
const arCategoryAddDocumentBtn = document.getElementById('arCategoryAddDocumentBtn');
const arCategoryDetailMeta = document.querySelector('.ar-category-detail-meta');

// ---------- CATEGORY DETAIL: VALIDATION RULES SECTION SHELL ----------
// Procedure/Evidence remains Category-specific. Cross-document comparisons are assigned here
// by reference only; their reusable definitions and matrix conditions belong to Cross-checks.
function buildCategoryValidationRulesSection() {
    const section = document.createElement('div');
    section.className = 'ar-validation-rules-section';
    section.id = 'arCategoryValidationRulesSection';

    const header = document.createElement('div');
    header.className = 'ar-validation-rules-header';
    header.textContent = '⚠️ Validation Rules';
    section.appendChild(header);

    const procedure = document.createElement('div');
    procedure.className = 'ar-validation-subsection';
    procedure.innerHTML = '<div class="ar-validation-subsection-title">Procedure / Evidence</div>' +
        '<div class="ar-rule-table-wrap"><table class="ar-category-rule-table"><thead><tr><th>Document</th><th>Condition</th><th>Role</th><th>Risk</th><th></th></tr></thead><tbody id="arProcedureRulesBody"></tbody></table></div>' +
        '<div class="ar-validation-actions"><button type="button" class="ar-btn-chip-add" id="arAddProcedureRuleBtn">Add procedure check</button>' +
        '<button type="button" class="ar-btn-secondary" id="arImportDocumentProcedureRulesBtn">Import Document audit checks</button></div>';
    section.appendChild(procedure);

    const applied = document.createElement('div');
    applied.className = 'ar-validation-subsection';
    applied.innerHTML = '<div class="ar-validation-subsection-title">Applied Cross-checks</div>' +
        '<p class="ar-validation-help">Attach reusable rules from Cross-checks. Definitions stay read-only here.</p>' +
        '<div id="arAppliedCrossChecksList" class="ar-applied-cross-checks-list"></div>' +
        '<div class="ar-applied-cross-check-picker"><div class="ar-applied-cross-check-picker-copy"><span class="ar-applied-cross-check-picker-label">Reusable rule</span><span class="ar-applied-cross-check-picker-help">Choose a rule to apply to this Category.</span></div><div class="ar-applied-cross-check-picker-controls"><select id="arCategoryAddCrossCheckSelect"></select>' +
        '<button type="button" class="ar-btn-chip-add" id="arCategoryAddCrossCheckBtn">Add rule</button></div></div>' +
        '<div id="arCategoryLegacyMathNotice" class="ar-legacy-rule-notice" style="display:none;"></div>';
    section.appendChild(applied);
    const clearing = document.createElement('button');
    clearing.type = 'button'; clearing.className = 'ar-btn-secondary ar-clearing-rules-btn'; clearing.id = 'arClearingRulesBtn'; clearing.textContent = 'Clearing Rules';
    clearing.addEventListener('click', showClearingRules);
    section.appendChild(clearing);
    arCategoryDetailMeta.parentNode.insertBefore(section, arCategoryDetailMeta);
}
buildCategoryValidationRulesSection();

// Replace the original stacked Category section with the approved Control Center layout.
// This is a DOM-only presentation upgrade: the existing field ids, row builders, and save
// handlers remain the source of truth for all persisted Category data.
function upgradeCategoryControlCenter() {
    const section = document.getElementById('arCategoryValidationRulesSection');
    if (!section) return;
    section.className = 'ar-category-control-center';
    section.innerHTML = '';

    const moveField = (input, fallbackId) => {
        const element = input || document.getElementById(fallbackId);
        return element && (element.closest('.ar-field') || element.parentElement);
    };

    const hero = document.createElement('div');
    hero.className = 'ar-category-control-hero';
    hero.innerHTML = '<div><p class="ar-category-eyebrow">Category control center</p>' +
        '<h2>How this category affects voucher status</h2>' +
        '<p>Define the evidence, procedures, and comparison tests that determine whether a voucher line can be cleared.</p></div>' +
        '<div class="ar-category-hero-actions"><span class="ar-category-policy-pill"><span class="ar-category-policy-dot"></span>Policy configured</span>' +
        '<button type="button" class="ar-btn-secondary ar-category-status-logic-btn">View exact status logic</button></div>';
    hero.querySelector('.ar-category-status-logic-btn').addEventListener('click', showClearingRules);
    section.appendChild(hero);

    const identity = document.createElement('div');
    identity.className = 'ar-category-identity-card';
    identity.innerHTML = '<div class="ar-category-card-heading"><div><h3>Category identity</h3><p>Name this policy and keep its audit intent visible to the reviewer.</p></div></div>';
    const identityFields = document.createElement('div');
    identityFields.className = 'ar-category-identity-fields';
    [moveField(arCategoryNameInput, 'arCategoryNameInput'), moveField(arCategoryKbOverviewInput, 'arCategoryKbOverviewInput')].forEach(field => {
        if (field) identityFields.appendChild(field);
    });
    identity.appendChild(identityFields);
    section.appendChild(identity);

    const statusGrid = document.createElement('div');
    statusGrid.className = 'ar-category-status-grid';
    statusGrid.innerHTML = '<article class="ar-category-status-card ar-status-pending" data-status-card="pending"><div class="ar-category-status-title"><span class="ar-category-status-icon">1</span><strong>Pending</strong></div><p>Account code is missing or no active Category is assigned.</p><button type="button" class="ar-category-status-link">View conditions</button></article>' +
        '<article class="ar-category-status-card ar-status-query" data-status-card="query"><div class="ar-category-status-title"><span class="ar-category-status-icon">2</span><strong>Query</strong></div><p>Required evidence, active checks, exceptions, hard-fail Cross-checks, or Account Head notes remain unresolved.</p><button type="button" class="ar-category-status-link">View conditions</button></article>' +
        '<article class="ar-category-status-card ar-status-cleared" data-status-card="cleared"><div class="ar-category-status-title"><span class="ar-category-status-icon">3</span><strong>Cleared</strong></div><p>Category is resolved and every applicable Query condition has passed or is not applicable.</p><button type="button" class="ar-category-status-link">View conditions</button></article>';
    statusGrid.querySelectorAll('.ar-category-status-link').forEach(button => button.addEventListener('click', showClearingRules));
    section.appendChild(statusGrid);

    const metrics = document.createElement('div');
    metrics.className = 'ar-category-metrics';
    metrics.innerHTML = '<div class="ar-category-metric metric-blue"><strong id="arCategoryMetricDocuments">0</strong><span>Required documents</span></div>' +
        '<div class="ar-category-metric"><strong id="arCategoryMetricProcedures">0</strong><span>Procedure checks</span></div>' +
        '<div class="ar-category-metric metric-amber"><strong id="arCategoryMetricCrossChecks">0</strong><span>Applied Cross-checks</span></div>' +
        '<div class="ar-category-metric metric-green"><strong id="arCategoryMetricIssues">0</strong><span>Configuration issues</span></div>';
    section.appendChild(metrics);

    const tabs = document.createElement('nav');
    tabs.className = 'ar-category-tabs';
    tabs.setAttribute('aria-label', 'Category configuration sections');
    [['overview', 'Overview'], ['evidence', 'Evidence'], ['procedures', 'Procedures'], ['crosschecks', 'Cross-checks']].forEach(([key, label], index) => {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'ar-category-tab' + (index === 0 ? ' active' : '');
        button.dataset.categoryTab = key; button.textContent = label;
        button.addEventListener('click', () => selectCategoryControlTab(key));
        tabs.appendChild(button);
    });
    section.appendChild(tabs);

    const panel = (key, active = false) => {
        const value = document.createElement('section');
        value.className = 'ar-category-tab-panel' + (active ? ' active' : '');
        value.dataset.categoryPanel = key;
        return value;
    };

    const overview = panel('overview', true);
    overview.innerHTML = '<div class="ar-category-overview-grid"><article class="ar-category-info-card"><h3>What this policy controls</h3><p>Required evidence identifies what must be present. Procedure checks explain what the reviewer must complete. Cross-checks compare configured document fields.</p><div class="ar-category-callout"><span>!</span><div><strong>Status rule</strong>Only the conditions shown above can block a voucher line from becoming Cleared.</div></div></article><article class="ar-category-info-card"><h3>Audit focus</h3><ul><li>Prevent unauthorised or unsupported purchases.</li><li>Compare quantities and rates across the document chain.</li><li>Keep document selection explicit and visible.</li><li>Keep authority metadata separate from status gates.</li></ul></article></div>';
    const requirements = document.createElement('article');
    requirements.className = 'ar-category-policy-summary ar-category-info-card';
    requirements.innerHTML = '<h3>Live status requirements</h3><div id="arCategoryClearedRequirementsList"></div>';
    overview.appendChild(requirements);
    section.appendChild(overview);

    const evidence = panel('evidence');
    evidence.innerHTML = '<div class="ar-category-section-heading"><div><h3>Required evidence</h3><p>Only these documents can appear in this Category\'s Procedure/Evidence and Verification configuration.</p></div><span class="ar-category-section-badge">Blocks Cleared when incomplete</span></div>';
    const documentsField = document.getElementById('arCategoryDocumentsField') || moveField(arCategoryDocsList, 'arCategoryDocsList');
    if (documentsField) evidence.appendChild(documentsField);
    const ownership = document.createElement('div');
    ownership.className = 'ar-category-info-card ar-category-document-ownership';
    ownership.innerHTML = '<h3>Document ownership policy</h3><p>Category Applicable Documents is the single source of truth. A Cross-check dependency must already be selected here; it never creates a hidden document tab.</p>';
    evidence.appendChild(ownership);
    section.appendChild(evidence);

    const procedure = panel('procedures');
    procedure.innerHTML = '<div class="ar-category-section-heading"><div><h3>Procedure / Evidence</h3><p>Checks are grouped by document so the author can see exactly what the reviewer must complete.</p></div><span class="ar-category-section-badge">Incomplete -&gt; Query</span></div>' +
        '<div class="ar-rule-table-wrap"><table class="ar-category-rule-table"><thead><tr><th>Document</th><th>Condition</th><th>Role</th><th>Risk</th><th></th></tr></thead><tbody id="arProcedureRulesBody"></tbody></table></div>' +
        '<div class="ar-validation-actions"><button type="button" class="ar-btn-chip-add" id="arAddProcedureRuleBtn">Add procedure check</button>' +
        '<button type="button" class="ar-btn-secondary" id="arImportDocumentProcedureRulesBtn">Import Document audit checks</button></div>';
    section.appendChild(procedure);

    const applied = panel('crosschecks');
    applied.innerHTML = '<div class="ar-category-section-heading"><div><h3>Applied Cross-checks</h3><p>Reusable comparison rules stay defined centrally; this Category only selects which rules apply.</p></div><span class="ar-category-section-badge">Hard-fail only</span></div>' +
        '<div id="arAppliedCrossChecksList" class="ar-applied-cross-checks-list"></div>' +
        '<div class="ar-applied-cross-check-picker"><div class="ar-applied-cross-check-picker-copy"><span class="ar-applied-cross-check-picker-label">Reusable rule</span><span class="ar-applied-cross-check-picker-help">Choose a rule to apply to this Category.</span></div><div class="ar-applied-cross-check-picker-controls"><select id="arCategoryAddCrossCheckSelect"></select>' +
        '<button type="button" class="ar-btn-chip-add" id="arCategoryAddCrossCheckBtn">Add rule</button></div></div>' +
        '<div id="arCategoryLegacyMathNotice" class="ar-legacy-rule-notice" style="display:none;"></div>';
    const matrixHint = document.createElement('div');
    matrixHint.className = 'ar-category-crosscheck-policy';
    matrixHint.innerHTML = '<strong>Dependency policy</strong><span>Missing dependencies warn or block rule activation. They never silently create Procedure or Verification tabs.</span>';
    applied.appendChild(matrixHint);
    section.appendChild(applied);

    arCategoryDetailForm.classList.add('ar-category-control-form');
    arCategoryDetailMeta.parentNode.insertBefore(section, arCategoryDetailMeta);
}

function selectCategoryControlTab(name) {
    document.querySelectorAll('#arCategoryValidationRulesSection [data-category-tab]').forEach(button => {
        button.classList.toggle('active', button.dataset.categoryTab === name);
    });
    document.querySelectorAll('#arCategoryValidationRulesSection [data-category-panel]').forEach(panel => {
        panel.classList.toggle('active', panel.dataset.categoryPanel === name);
    });
}

function updateCategoryControlMetrics() {
    const setMetric = (id, value) => { const target = document.getElementById(id); if (target) target.textContent = String(value); };
    const procedureCount = document.querySelectorAll('#arProcedureRulesBody > tr').length;
    const issues = categoryDocIds.filter(id => {
        const info = getCategoryDocDisplayInfo(id);
        return info && info.is_active === false;
    }).length + (categoryLegacyMathRuleCount || 0);
    setMetric('arCategoryMetricDocuments', categoryDocIds.length);
    setMetric('arCategoryMetricProcedures', procedureCount);
    setMetric('arCategoryMetricCrossChecks', categoryCrossCheckIds.length);
    setMetric('arCategoryMetricIssues', issues);
}

upgradeCategoryControlCenter();
document.getElementById('arAddProcedureRuleBtn')?.addEventListener('click', () => addProcedureRuleRow());
document.getElementById('arImportDocumentProcedureRulesBtn')?.addEventListener('click', importDocumentProcedureChecks);
document.getElementById('arCategoryAddCrossCheckBtn')?.addEventListener('click', addCategoryCrossCheck);

function escapeHtml(value) {
    const div = document.createElement('div'); div.textContent = value == null ? '' : String(value); return div.innerHTML;
}

function templateDocuments() {
    // Procedure/Evidence may target only a Document already selected in this Category's
    // Applicable Documents list. Membership is deliberately not inferred from a rule row.
    const applicableDocumentIds = new Set(categoryDocIds);
    return allDocuments.filter(doc => doc.is_active !== false && (
        applicableDocumentIds.has(doc.id) && (
            ((doc.document_type || 'linked_template') === 'linked_template' && doc.template_id) ||
            doc.document_type === 'form' ||
            doc.document_type === 'physical_bill'
        )
    ));
}

function optionMarkup(items, selected, placeholder) {
    return '<option value="">' + placeholder + '</option>' + items.map(item => {
        const suffix = item.document_type === 'physical_bill' ? ' — Physical Vendor Bill' : '';
        return '<option value="' + item.id + '"' + (String(item.id) === String(selected) ? ' selected' : '') + '>' + escapeHtml(item.name + suffix) + '</option>';
    }).join('');
}

function fieldMarkup(documentId, selected, selectedFields = null) {
    // Do not fall back to the legacy Master Columns mirror here. Category rules must use
    // only the active columns mapped to the selected Document's own Template.
    const fields = selectedFields || [];
    return '<option value="">Select field</option>' + fields.map(field => '<option' + (field === selected ? ' selected' : '') + '>' + escapeHtml(field) + '</option>').join('');
}

function addProcedureRuleRow(rule = {}) {
    const body = document.getElementById('arProcedureRulesBody'); if (!body) return;
    const row = document.createElement('tr');
    row.innerHTML = '<td><select data-rule="document_id">' + optionMarkup(templateDocuments(), rule.document_id, 'Select document') + '</select></td><td><select data-rule="condition"><option value="required_document">Required document</option><option value="prepared_by">Prepared by</option><option value="checked_by">Checked by</option><option value="approved_by">Approved by</option><option value="signature">Signature</option></select></td><td><select data-rule="sub_value"><option value="">Select after choosing a document</option></select></td><td><select data-rule="risk"><option value="Low">Low</option><option value="Medium" selected>Medium</option><option value="High">High</option></select></td><td><button type="button" class="ar-btn-delete">Remove</button></td>';
    row.querySelector('[data-rule="condition"]').value = rule.condition || 'required_document';
    row.querySelector('[data-rule="risk"]').value = rule.risk || 'Medium';
    const docSelect = row.querySelector('[data-rule="document_id"]'); const condition = row.querySelector('[data-rule="condition"]'); const sub = row.querySelector('[data-rule="sub_value"]');
    let subValueRequestToken = 0;
    const renderSubValues = (values, selectedValue) => {
        const uniqueValues = [...new Set((values || []).filter(Boolean))];
        if (selectedValue && !uniqueValues.includes(selectedValue)) uniqueValues.unshift(selectedValue);
        sub.innerHTML = '<option value="">Select detail</option>' + uniqueValues.map(value => '<option value="' + escapeHtml(value) + '">' + escapeHtml(value) + '</option>').join('');
        sub.value = selectedValue || '';
    };
    const refreshSub = async (selectedValue = '') => {
        const requestToken = ++subValueRequestToken;
        if (condition.value === 'required_document') {
            renderSubValues(['Document must be linked'], selectedValue || rule.sub_value || '');
            return;
        }
        if (!docSelect.value) {
            renderSubValues([], selectedValue);
            return;
        }
        sub.innerHTML = '<option value="">Loading document values...</option>';
        try {
            const values = await getCategoryProcedureValues(docSelect.value, condition.value);
            if (requestToken !== subValueRequestToken) return;
            renderSubValues(values, selectedValue);
        } catch (error) {
            if (requestToken !== subValueRequestToken) return;
            console.warn('Failed to load Category procedure values:', error);
            renderSubValues([], selectedValue);
        }
    };
    docSelect.addEventListener('change', () => refreshSub(''));
    condition.addEventListener('change', () => refreshSub(''));
    refreshSub(rule.sub_value || '');
    row.querySelector('button').addEventListener('click', () => { row.remove(); updateCategoryControlMetrics(); }); body.appendChild(row);
    updateCategoryControlMetrics();
}

function procedureConditionForRole(roleName) {
    const role = String(roleName || '').toLowerCase();
    if (role.includes('prepar')) return 'prepared_by';
    if (role.includes('check')) return 'checked_by';
    if (role.includes('approv')) return 'approved_by';
    return 'signature';
}

async function importDocumentProcedureChecks() {
    const documentIds = [...new Set(categoryDocIds)];
    if (!documentIds.length) {
        showCategoryDetailStatus('Select at least one Applicable Document first.', 'error');
        return;
    }

    try {
        const documents = await Promise.all(documentIds.map(async documentId => {
            const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/documents/' + documentId);
            if (!res.ok) throw new Error('Failed to load Document authorisation checks');
            return await res.json();
        }));
        const existing = new Set(readCategoryValidationRules().procedure_evidence.checks.map(rule => [
            rule.document_id, rule.condition, rule.sub_value || rule.detail || rule.selected_detail || ''
        ].join('|')));
        let imported = 0;
        documents.forEach(document => {
            (document.auth_roles || []).forEach(roleName => {
                const condition = procedureConditionForRole(roleName);
                const key = [document.id, condition, roleName].join('|');
                if (existing.has(key)) return;
                existing.add(key);
                addProcedureRuleRow({
                    document_id: document.id,
                    condition: condition,
                    sub_value: roleName,
                    risk: 'Medium'
                });
                imported += 1;
            });
        });
        showCategoryDetailStatus(
            imported ? 'Imported ' + imported + ' Document audit check' + (imported === 1 ? '' : 's') + '. Review and save the Category.' : 'All Document audit checks are already configured.',
            'success'
        );
    } catch (error) {
        showCategoryDetailStatus('Error: ' + error.message, 'error');
    }
}

function readCategoryValidationRules() {
    const rows = selector => [...document.querySelectorAll(selector + ' tr')].map(row => Object.fromEntries([...row.querySelectorAll('[data-rule]')].map(el => [el.dataset.rule, el.type === 'checkbox' ? el.checked : (el.value || null)])));
    // Mathematical conditions are no longer Category-owned. They are persisted on Cross-check
    // matrix rows; omitting this key lets the backend preserve old legacy JSON safely.
    return { procedure_evidence: { checks: rows('#arProcedureRulesBody') } };
}

function showClearingRulesLegacy() {
    const rules = readCategoryValidationRules();
    const popover = document.createElement('div');
    popover.className = 'ar-clearing-rules-popover';
    const title = document.createElement('div'); title.className = 'ar-clearing-rules-popover-title'; title.textContent = 'Clearing Rules'; popover.appendChild(title);
    const addSection = (heading, points, empty) => { const section = document.createElement('section'); section.className = 'ar-clearing-rules-section'; const h = document.createElement('h4'); h.textContent = heading; section.appendChild(h); const ul = document.createElement('ul'); (points.length ? points : [empty]).forEach(point => { const li = document.createElement('li'); li.textContent = point; ul.appendChild(li); }); section.appendChild(ul); popover.appendChild(section); };
    const docName = id => { const doc = templateDocuments().find(item => String(item.id) === String(id)); return doc ? doc.name : 'Selected document'; };
    const procedurePoints = rules.procedure_evidence.checks.map(rule => [docName(rule.document_id), ': ', rule.condition || 'condition', rule.sub_value ? ' — ' + rule.sub_value : '', rule.risk ? ' (' + rule.risk + ' risk)' : ''].join(''));
    const mathPoints = categoryCrossCheckIds.map(ccId => {
        const cc = getCrossCheckById(ccId);
        return cc ? cc.name + ' — defined in Cross-checks tab' : 'Cross-check #' + ccId + ' — unavailable';
    });
    addSection('Procedure / Evidence', procedurePoints, 'No procedure or evidence conditions configured.');
    addSection('Applied Cross-checks', mathPoints, 'No reusable Cross-checks applied.');
    const close = document.createElement('button'); close.type = 'button'; close.className = 'ar-btn-secondary'; close.textContent = 'Close'; close.addEventListener('click', () => popover.remove()); popover.appendChild(close);
    document.body.appendChild(popover);
}

function showClearingRules() {
    const backdrop = document.createElement('div');
    backdrop.className = 'ar-category-modal-backdrop';
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');
    backdrop.innerHTML = '<div class="ar-category-modal" role="document"><div class="ar-category-modal-header"><div><h2>Clearing Rules</h2><p>The exact conditions that can leave a voucher line Pending, Query, or Cleared.</p></div><button type="button" class="ar-category-modal-close" aria-label="Close">&times;</button></div><div class="ar-category-modal-body"></div></div>';

    const body = backdrop.querySelector('.ar-category-modal-body');
    const addSection = (heading, points, empty, className) => {
        const section = document.createElement('section');
        section.className = 'ar-category-modal-section ' + (className || '');
        const title = document.createElement('h3'); title.textContent = heading; section.appendChild(title);
        const list = document.createElement('ul');
        (points.length ? points : [empty]).forEach(point => { const item = document.createElement('li'); item.textContent = point; list.appendChild(item); });
        section.appendChild(list); body.appendChild(section);
    };
    const policy = window.arCategoryStatusPolicy || {};
    addSection('Pending', policy.pending_conditions || ['Account code is missing', 'No active Category is assigned'], '', 'is-pending');
    addSection('Query', policy.query_conditions || ['A required Document is incomplete', 'An active voucher-level check is not marked done', 'Linked evidence has a signature exception', 'A Cross-check has a hard failure', 'A pending Account Head note exists'], '', 'is-query');
    addSection('Cleared', [policy.cleared_condition || 'The Category is resolved and every applicable Query condition passes.'], '', 'is-cleared');
    addSection('Informational metadata', [policy.authority_note || 'Document Required Authority and Signature Specimens are informational and do not block status.'], '', 'is-info');

    const procedurePoints = readCategoryValidationRules().procedure_evidence.checks.map(rule => {
        const doc = templateDocuments().find(item => String(item.id) === String(rule.document_id));
        return [doc ? doc.name : 'Selected document', ': ', rule.condition || 'condition', rule.sub_value ? ' - ' + rule.sub_value : '', rule.risk ? ' (' + rule.risk + ' risk)' : ''].join('');
    });
    const crossCheckPoints = categoryCrossCheckIds.map(ccId => {
        const cc = getCrossCheckById(ccId);
        return cc ? cc.name + ' - reusable definition from Cross-checks' : 'Cross-check #' + ccId + ' - unavailable';
    });
    addSection('Configured checks', procedurePoints.concat(crossCheckPoints), 'No procedure or evidence conditions configured.', 'is-configured');

    const close = () => { backdrop.remove(); document.body.style.overflow = ''; };
    backdrop.querySelector('.ar-category-modal-close').addEventListener('click', close);
    backdrop.addEventListener('click', event => { if (event.target === backdrop) close(); });
    document.body.appendChild(backdrop);
    document.body.style.overflow = 'hidden';
}

async function loadCategoryValidationRules(categoryId) {
    beginAuditRulesProcessing('Loading Category checks...');
    try {
    const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/categories/' + categoryId + '/validation-rules');
    if (!res.ok) throw new Error('Failed to load validation rules');
    const rules = await res.json(); const p = rules.procedure_evidence || {};
    document.getElementById('arProcedureRulesBody').innerHTML = ''; (p.checks || []).forEach(addProcedureRuleRow);
    categoryLegacyMathRuleCount = (rules.mathematical_cross_checks || []).length;
    updateCategoryControlMetrics();
    } finally {
        endAuditRulesProcessing();
    }
}

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
        window.arCategoryStatusPolicy = {};
        arCategoryClearedRequirementsList.innerHTML = '';
        const hint = document.createElement('div');
        hint.className = 'ar-cleared-requirements-hint';
        hint.textContent = 'Save the category first to see what Cleared requires.';
        arCategoryClearedRequirementsList.appendChild(hint);
        return;
    }

    beginAuditRulesProcessing('Loading cleared requirements...');
    arCategoryClearedRequirementsList.innerHTML = '';
    const loading = document.createElement('div');
    loading.className = 'ar-cleared-requirements-hint';
    loading.textContent = 'Loading...';
    arCategoryClearedRequirementsList.appendChild(loading);

    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/categories/' + categoryId + '/cleared-requirements');
        if (!res.ok) throw new Error('Failed to load cleared-requirements summary');
        const data = await res.json();
        renderClearedRequirementsBullets(data);
    } catch (error) {
        arCategoryClearedRequirementsList.innerHTML = '';
        console.warn('Failed to load cleared-requirements summary:', error);
    } finally {
        endAuditRulesProcessing();
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

    const policy = data.status_policy || {};
    window.arCategoryStatusPolicy = policy;
    const pendingConditions = policy.pending_conditions || [
        'Account code is missing',
        'No active Category is assigned'
    ];
    const queryConditions = policy.query_conditions || [
        'A required Document is incomplete',
        'An active voucher-level check is not marked done',
        'Linked evidence has a signature exception',
        'A Cross-check has a hard failure',
        'A pending Account Head note exists'
    ];
    addBullet('Pending — ' + pendingConditions.join('; '));
    addBullet('Query when any of these applies — ' + queryConditions.join('; '));
    addBullet(
        'Cleared — ' + (policy.cleared_condition ||
            'The Category is resolved and every applicable Query condition passes.')
    );
    addBullet(policy.authority_note ||
        'Document Required Authority and Signature Specimens are informational and do not block status.'
    );
    addBullet('Voucher-level checks — all active checks must be marked done');
    addBullet('Account Head — no pending Account Head note');
    addBullet(
        data.cross_checks_required
            ? 'Cross-checks — hard failures block; shortfalls and not-evaluated results do not'
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
let categoryLegacyMathRuleCount = 0;
const categoryCrossCheckDetailCache = {};
let appliedCrossChecksRenderToken = 0;
const categoryDocumentPolicyCache = {};
const categoryDocumentAuthorisationCache = {};

function procedureConditionMatchesRole(condition, roleName) {
    const role = String(roleName || '').toLowerCase();
    if (condition === 'prepared_by') return role.includes('prepar');
    if (condition === 'checked_by') return role.includes('check');
    if (condition === 'approved_by') return role.includes('approv');
    return true;
}

async function getCategoryDocumentAuthorisation(documentId) {
    if (categoryDocumentAuthorisationCache[documentId]) {
        return categoryDocumentAuthorisationCache[documentId];
    }
    const result = (async () => {
        beginAuditRulesProcessing('Loading Document authorisation...');
        try {
        const documentRes = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/documents/' + documentId);
        if (!documentRes.ok) throw new Error('Failed to load Document authorisation values');
        return {
            roles: (await documentRes.json()).auth_roles || []
        };
        } finally {
            endAuditRulesProcessing();
        }
    })();
    categoryDocumentAuthorisationCache[documentId] = result;
    try {
        return await result;
    } catch (error) {
        delete categoryDocumentAuthorisationCache[documentId];
        throw error;
    }
}

async function getCategoryProcedureValues(documentId, condition) {
    const authorisation = await getCategoryDocumentAuthorisation(documentId);
    const matchingRoles = authorisation.roles.filter(role => procedureConditionMatchesRole(condition, role));
    return [...new Set(matchingRoles.filter(Boolean))];
}

async function getCategorySelectedFields(documentId) {
    const cached = categoryDocumentPolicyCache[documentId];
    if (cached) return cached.fields || cached;
    beginAuditRulesProcessing('Loading Category document fields...');
    try {
    const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/documents/' + documentId);
    if (!res.ok) throw new Error('Failed to load Template fields');
    const doc = await res.json();
    if (!doc.template_id) {
        categoryDocumentPolicyCache[documentId] = { templateId: null, fields: [] };
        return [];
    }
    const columnsRes = await window.ApiUtils.apiFetch(API_BASE + '/api/templates/' + doc.template_id + '/columns');
    if (!columnsRes.ok) throw new Error('Failed to load Template fields');
    const columns = await columnsRes.json();
    const fields = columns.map(field => field.column_name).filter(Boolean);
    categoryDocumentPolicyCache[documentId] = { templateId: doc.template_id, fields };
    return fields;
    } finally {
        endAuditRulesProcessing();
    }
}

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
    beginAuditRulesProcessing('Loading Cross-check rules for Categories…');
    try {
        // Include inactive definitions so an existing Category can display and detach one
        // safely; the picker below still offers active definitions only.
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/cross-checks?include_inactive=true');
        if (!res.ok) throw new Error('Failed to load cross-checks');
        allCrossChecks = await res.json();
    } catch (error) {
        allCrossChecks = [];
        showCategoryDetailStatus('❌ Error loading cross-checks: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
    }
}

function getCrossCheckById(id) {
    return allCrossChecks.find(cc => cc.id === id);
}

function getCategoryCrossCheckDependencyIssues(crossCheck) {
    const categoryDocumentIds = new Set(categoryDocIds.map(Number));
    const missing = [];
    const inactive = [];
    (crossCheck.documents || []).forEach(document => {
        const documentId = Number(document.document_id);
        if (!categoryDocumentIds.has(documentId)) {
            missing.push(document.name || ('Document #' + documentId));
            return;
        }
        const categoryDocument = categoryDocMeta[documentId];
        if (document.is_active === false || (categoryDocument && categoryDocument.is_active === false)) {
            inactive.push(document.name || ('Document #' + documentId));
        }
    });
    return { missing, inactive, crossCheckInactive: crossCheck.is_active === false };
}

function categoryCrossCheckDependencyMessage(issues) {
    const parts = [];
    if (issues.crossCheckInactive) parts.push('Cross-check itself is inactive');
    if (issues.missing.length) parts.push('not in Applicable Documents: ' + issues.missing.join(', '));
    if (issues.inactive.length) parts.push('inactive: ' + issues.inactive.join(', '));
    return parts.join('; ');
}

// ---------- LOAD & RENDER LIST ----------
// Fetch-once-per-toggle-state cache (Fast Data Handling, Phase 9), now pagination-aware (Phase
// 10 of 13): each key holds {items, total} rather than a bare array -- items is everything
// loaded so far (page 1 plus any "Load more" pages), total comes from the backend's
// X-Total-Count header. switchEntityType('categories') re-fetches this same list every time the
// user switches back to this rail tab, and selectCategory() (below) calls loadCategories() on
// every row click just to move the highlight -- neither needs fresh server data, so both still
// serve from cache (now restoring however many pages were already loaded, not just page 1).
// invalidateCategoriesListCache() clears both keys on this page's own create/edit/delete/
// restore, which are the only actions that can change what this list should show -- the next
// loadCategories() call then re-fetches from page 1, the correct pagination reset point too.
let categoriesListCache = { true: null, false: null };

function invalidateCategoriesListCache() {
    categoriesListCache = { true: null, false: null };
}

async function loadCategories() {
    const includeInactive = arCategoryShowInactiveCheckbox.checked;
    const cached = categoriesListCache[includeInactive];
    if (cached) {
        renderCategoryList(cached.items);
        updateCategoriesLoadMoreVisibility();
        return;
    }
    beginAuditRulesProcessing('Loading Categories…');
    window.LoadingUI.showSkeleton(arCategoryListBody, 6);
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/categories?include_inactive=' + includeInactive + '&limit=' + CATEGORIES_PAGE_SIZE + '&offset=0');
        if (!res.ok) throw new Error('Failed to load categories');
        const categories = await res.json();
        const total = parseInt(res.headers.get('X-Total-Count'), 10) || categories.length;
        categoriesListCache[includeInactive] = { items: categories, total: total };
        renderCategoryList(categories);
        updateCategoriesLoadMoreVisibility();
    } catch (error) {
        arCategoryListBody.innerHTML = '';
        arCategoryListEmptyMessage.textContent = '❌ Error loading categories: ' + error.message;
        arCategoryListEmptyMessage.style.display = 'block';
        arCategoryLoadMoreBtn.style.display = 'none';
    } finally {
        endAuditRulesProcessing();
    }
}

async function loadMoreCategories() {
    const includeInactive = arCategoryShowInactiveCheckbox.checked;
    const cached = categoriesListCache[includeInactive];
    if (!cached) return;
    beginAuditRulesProcessing('Loading more Categories...');
    window.LoadingUI.showSpinner(arCategoryLoadMoreBtn, 'Loading');
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/categories?include_inactive=' + includeInactive + '&limit=' + CATEGORIES_PAGE_SIZE + '&offset=' + cached.items.length);
        if (!res.ok) throw new Error('Failed to load more categories');
        const nextPage = await res.json();
        cached.items = cached.items.concat(nextPage);
        cached.total = parseInt(res.headers.get('X-Total-Count'), 10) || cached.total;
        appendCategoryRows(nextPage);
        updateCategoriesLoadMoreVisibility();
    } catch (error) {
        showCategoryDetailStatus('❌ Error loading more categories: ' + error.message, 'error');
    } finally {
        window.LoadingUI.hideSpinner(arCategoryLoadMoreBtn);
        endAuditRulesProcessing();
    }
}

function updateCategoriesLoadMoreVisibility() {
    const includeInactive = arCategoryShowInactiveCheckbox.checked;
    const cached = categoriesListCache[includeInactive];
    arCategoryLoadMoreBtn.style.display = (cached && cached.items.length < cached.total) ? 'block' : 'none';
}

function renderCategoryList(categories) {
    arCategoryListBody.innerHTML = '';

    if (categories.length === 0) {
        arCategoryListEmptyMessage.textContent = 'No categories found.';
        arCategoryListEmptyMessage.style.display = 'block';
        return;
    }
    arCategoryListEmptyMessage.style.display = 'none';
    appendCategoryRows(categories);
}

function appendCategoryRows(categories) {
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
    // Keep inactive attached Documents visible so administrators can identify and remove
    // stale references; they remain excluded from the active add-picker and Verification.
    const visibleDocIds = categoryDocIds.slice();

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

            const isActive = displayInfo.is_active !== false;
            const row = document.createElement('tr');
            row.className = [
                !isPersisted ? 'ar-category-doc-row-unsaved' : '',
                !isActive ? 'ar-category-doc-row-inactive ar-list-row-inactive' : ''
            ].filter(Boolean).join(' ');

            const nameCell = document.createElement('td');
            nameCell.className = 'ar-category-doc-name';
            nameCell.textContent = displayInfo.name +
                (isActive ? '' : ' (Inactive — remove or restore)') +
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
            requiredCheckbox.disabled = !isPersisted || !isActive;
            requiredCheckbox.addEventListener('change', () => toggleCategoryDocumentRequired(docId, requiredCheckbox.checked));

            const requiredLabel = document.createElement('label');
            requiredLabel.className = 'ar-category-doc-required' +
                (requiredCheckbox.checked ? ' checked' : '') +
                (isPersisted && isActive ? '' : ' disabled');
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
    updateCategoryControlMetrics();
}

async function toggleCategoryDocumentRequired(docId, checked) {
    if (!editingCategoryId) return;

    beginAuditRulesProcessing('Saving required flag...');
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
    } finally {
        endAuditRulesProcessing();
    }
}

function addCategoryDocument() {
    const value = arCategoryAddDocumentSelect.value;
    if (!value) return;
    const documentId = parseInt(value, 10);
    if (!Number.isInteger(documentId) || categoryDocIds.includes(documentId)) return;
    categoryDocIds.push(documentId);
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

// ---------- APPLIED CROSS-CHECKS (Suggestion 2) ----------
// The Category owns only the assignment. The Cross-check detail and matrix are fetched for a
// read-only summary, so changing a reusable rule in the Cross-check tab is reflected everywhere.
async function getCategoryCrossCheckDefinition(ccId) {
    if (categoryCrossCheckDetailCache[ccId]) return categoryCrossCheckDetailCache[ccId];
    const promise = (async () => {
        beginAuditRulesProcessing('Loading Category Cross-check details...');
        try {
        const detailRes = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/cross-checks/' + ccId);
        if (!detailRes.ok) throw new Error('Cross-check not found');
        const detail = await detailRes.json();
        const matrixRes = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/cross-checks/' + ccId + '/matrix');
        if (!matrixRes.ok) throw new Error('Cross-check matrix not found');
        const result = { detail, matrix: await matrixRes.json() };
        return result;
        } finally {
            endAuditRulesProcessing();
        }
    })();
    categoryCrossCheckDetailCache[ccId] = promise;
    try {
        return await promise;
    } catch (error) {
        delete categoryCrossCheckDetailCache[ccId];
        throw error;
    }
}

function renderAppliedCrossCheckSummary(container, ccId, definition) {
    const card = document.createElement('article');
    card.className = 'ar-applied-cross-check-card';

    const header = document.createElement('div');
    header.className = 'ar-applied-cross-check-header';
    const headerMain = document.createElement('div');
    headerMain.className = 'ar-applied-cross-check-header-main';
    const kicker = document.createElement('span');
    kicker.className = 'ar-applied-cross-check-kicker';
    kicker.textContent = 'Reusable comparison rule';
    headerMain.appendChild(kicker);
    const title = document.createElement('strong');
    title.className = 'ar-applied-cross-check-title';
    title.textContent = definition.detail.name || ('Cross-check #' + ccId);
    headerMain.appendChild(title);
    header.appendChild(headerMain);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'ar-applied-cross-check-remove';
    remove.textContent = 'Remove';
    remove.title = 'Remove this Cross-check from the Category';
    remove.addEventListener('click', () => removeCategoryCrossCheck(ccId));
    header.appendChild(remove);
    card.appendChild(header);

    const matrix = definition.matrix || {};
    const documentNames = Object.fromEntries((matrix.documents || []).map(doc => [String(doc.document_id), doc.name]));
    const documentsSection = document.createElement('section');
    documentsSection.className = 'ar-applied-cross-check-section ar-applied-cross-check-documents';
    const documentsHeading = document.createElement('div');
    documentsHeading.className = 'ar-applied-cross-check-section-heading';
    documentsHeading.textContent = 'Document flow';
    documentsSection.appendChild(documentsHeading);
    const chain = document.createElement('div');
    chain.className = 'ar-applied-cross-check-chain';
    if ((matrix.documents || []).length) {
        (matrix.documents || []).forEach((doc, index) => {
            const pill = document.createElement('span');
            pill.className = 'ar-applied-cross-check-document-pill';
            pill.textContent = doc.name || ('Document #' + doc.document_id);
            chain.appendChild(pill);
            if (index < matrix.documents.length - 1) {
                const arrow = document.createElement('span');
                arrow.className = 'ar-applied-cross-check-chain-arrow';
                arrow.setAttribute('aria-hidden', 'true');
                arrow.textContent = '→';
                chain.appendChild(arrow);
            }
        });
    } else {
        const notConfigured = document.createElement('span');
        notConfigured.className = 'ar-applied-cross-check-not-configured';
        notConfigured.textContent = 'No document flow configured';
        chain.appendChild(notConfigured);
    }
    documentsSection.appendChild(chain);
    card.appendChild(documentsSection);

    const conditionsSection = document.createElement('section');
    conditionsSection.className = 'ar-applied-cross-check-section ar-applied-cross-check-comparisons';
    const conditionsHeading = document.createElement('div');
    conditionsHeading.className = 'ar-applied-cross-check-section-heading';
    const headingLabel = document.createElement('span');
    headingLabel.textContent = 'Comparison conditions';
    conditionsHeading.appendChild(headingLabel);
    const completeFields = (matrix.fields || []).filter(field => Object.values(field.cells || {}).filter(Boolean).length >= 2);
    const conditionCount = document.createElement('span');
    conditionCount.className = 'ar-applied-cross-check-count';
    conditionCount.textContent = completeFields.length + (completeFields.length === 1 ? ' condition' : ' conditions');
    conditionsHeading.appendChild(conditionCount);
    conditionsSection.appendChild(conditionsHeading);
    const conditions = document.createElement('div');
    conditions.className = 'ar-applied-cross-check-conditions';
    (matrix.fields || []).forEach((field, index) => {
        const operands = Object.entries(field.cells || {}).filter(([, value]) => value);
        if (operands.length < 2) return;
        const item = document.createElement('div');
        item.className = 'ar-applied-cross-check-condition';
        const conditionTop = document.createElement('div');
        conditionTop.className = 'ar-applied-cross-check-condition-top';
        const rowLabel = document.createElement('span');
        rowLabel.className = 'ar-applied-cross-check-row-label';
        rowLabel.textContent = 'Condition ' + (index + 1);
        conditionTop.appendChild(rowLabel);
        const state = document.createElement('span');
        state.className = 'ar-applied-cross-check-state ' + (field.blocking ? 'is-blocking' : 'is-informational');
        state.textContent = field.blocking ? 'Blocking' : 'Informational';
        conditionTop.appendChild(state);
        item.appendChild(conditionTop);

        const expression = document.createElement('div');
        expression.className = 'ar-applied-cross-check-expression';
        operands.forEach(([docId, value], operandIndex) => {
            const operand = document.createElement('span');
            operand.className = 'ar-applied-cross-check-operand';
            const operandDocument = document.createElement('span');
            operandDocument.className = 'ar-applied-cross-check-operand-document';
            operandDocument.textContent = documentNames[docId] || 'Document #' + docId;
            const operandField = document.createElement('span');
            operandField.className = 'ar-applied-cross-check-operand-field';
            operandField.textContent = value;
            operand.appendChild(operandDocument);
            operand.appendChild(operandField);
            expression.appendChild(operand);
            if (operandIndex < operands.length - 1) {
                const operator = document.createElement('span');
                operator.className = 'ar-applied-cross-check-operator';
                operator.textContent = MATRIX_OPERATOR_LABELS[field.operator] || field.operator || '=';
                expression.appendChild(operator);
            }
        });
        item.appendChild(expression);

        const metadata = document.createElement('div');
        metadata.className = 'ar-applied-cross-check-metadata';
        const tolerance = document.createElement('span');
        tolerance.className = 'ar-applied-cross-check-meta-badge';
        tolerance.textContent = 'Tolerance ' + (field.tolerance ?? 0);
        metadata.appendChild(tolerance);
        const missing = document.createElement('span');
        missing.className = 'ar-applied-cross-check-meta-badge';
        missing.textContent = MATRIX_MISSING_LABELS[field.missing_data_behavior] || field.missing_data_behavior || 'Not configured';
        metadata.appendChild(missing);
        const risk = document.createElement('span');
        const riskClass = String(field.risk || 'Medium').toLowerCase().replace(/[^a-z]+/g, '-');
        risk.className = 'ar-applied-cross-check-meta-badge ar-applied-cross-check-risk ar-risk-' + riskClass;
        risk.textContent = (field.risk || 'Medium') + ' risk';
        metadata.appendChild(risk);
        item.appendChild(metadata);
        conditions.appendChild(item);
    });
    if (!conditions.childElementCount) {
        const empty = document.createElement('div');
        empty.className = 'ar-applied-cross-check-empty';
        const emptyTitle = document.createElement('strong');
        emptyTitle.textContent = 'No comparison conditions yet';
        empty.appendChild(emptyTitle);
        const emptyText = document.createElement('span');
        emptyText.textContent = 'Complete at least two document fields in the Cross-checks tab to show the rule here.';
        empty.appendChild(emptyText);
        conditions.appendChild(empty);
    }
    conditionsSection.appendChild(conditions);
    card.appendChild(conditionsSection);
    container.appendChild(card);
}

async function renderAppliedCrossChecks() {
    const container = document.getElementById('arAppliedCrossChecksList');
    const picker = document.getElementById('arCategoryAddCrossCheckSelect');
    const addButton = document.getElementById('arCategoryAddCrossCheckBtn');
    const legacyNotice = document.getElementById('arCategoryLegacyMathNotice');
    if (!container || !picker || !addButton) return;
    updateCategoryControlMetrics();
    const renderToken = ++appliedCrossChecksRenderToken;

    container.innerHTML = '';
    picker.innerHTML = '';
    const available = allCrossChecks.filter(cc => cc.is_active !== false && !categoryCrossCheckIds.includes(cc.id));
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = available.length ? 'Select a reusable Cross-check' : 'No reusable Cross-checks available';
    placeholder.selected = true;
    picker.appendChild(placeholder);
    let selectableCount = 0;
    available.forEach(cc => {
        const issues = getCategoryCrossCheckDependencyIssues(cc);
        const option = document.createElement('option');
        option.value = cc.id;
        option.disabled = issues.missing.length > 0 || issues.inactive.length > 0;
        if (!option.disabled) selectableCount += 1;
        option.textContent = option.disabled
            ? cc.name + ' (blocked: ' + categoryCrossCheckDependencyMessage(issues) + ')'
            : cc.name;
        picker.appendChild(option);
    });
    addButton.disabled = selectableCount === 0;

    if (!categoryCrossCheckIds.length) {
        const empty = document.createElement('div');
        empty.className = 'ar-applied-cross-check-empty-state';
        const emptyTitle = document.createElement('strong');
        emptyTitle.textContent = 'No rules applied yet';
        empty.appendChild(emptyTitle);
        const emptyText = document.createElement('span');
        emptyText.textContent = 'Attach a reusable Cross-check below to include it in this Category.';
        empty.appendChild(emptyText);
        container.appendChild(empty);
    } else {
        categoryCrossCheckIds.forEach(ccId => {
            const known = getCrossCheckById(ccId);
            if (!known) {
                const unavailable = document.createElement('div');
                unavailable.className = 'ar-applied-cross-check-card ar-applied-cross-check-unavailable';
                unavailable.textContent = 'Cross-check #' + ccId + ' is inactive or unavailable. ';
                appendCategoryCrossCheckRemoveButton(unavailable, ccId);
                container.appendChild(unavailable);
                return;
            }
            const dependencyIssues = getCategoryCrossCheckDependencyIssues(known);
            if (dependencyIssues.crossCheckInactive || dependencyIssues.missing.length || dependencyIssues.inactive.length) {
                const blocked = document.createElement('div');
                blocked.className = 'ar-applied-cross-check-card ar-applied-cross-check-unavailable';
                blocked.textContent = known.name + ' — blocked: ' + categoryCrossCheckDependencyMessage(dependencyIssues) + '. ' +
                    (dependencyIssues.crossCheckInactive
                        ? 'Restore or remove this Cross-check.'
                        : 'Add the required active Documents before this rule can be activated.');
                appendCategoryCrossCheckRemoveButton(blocked, ccId);
                container.appendChild(blocked);
                return;
            }
            const loading = document.createElement('div');
            loading.className = 'ar-applied-cross-check-card ar-applied-cross-check-loading';
            loading.textContent = 'Loading ' + known.name + '…';
            container.appendChild(loading);
            getCategoryCrossCheckDefinition(ccId).then(definition => {
                if (renderToken !== appliedCrossChecksRenderToken || !container.contains(loading)) return;
                loading.remove();
                renderAppliedCrossCheckSummary(container, ccId, definition);
            }).catch(() => {
                if (renderToken === appliedCrossChecksRenderToken && container.contains(loading)) {
                    loading.textContent = known.name + ' — definition unavailable.';
                }
            });
        });
    }

    if (legacyNotice) {
        legacyNotice.style.display = categoryLegacyMathRuleCount ? 'block' : 'none';
        legacyNotice.textContent = categoryLegacyMathRuleCount
            ? categoryLegacyMathRuleCount + ' legacy Category comparison(s) are preserved but not editable. Recreate them as reusable Cross-checks above.'
            : '';
    }
    updateCategoryControlMetrics();
}

// Keep old internal callers harmless while ensuring there is only one visible Category
// Cross-check surface.
function renderCategoryCrossCheckChainPreview() { renderAppliedCrossChecks(); }
function renderCategoryCrossCheckChips() { renderAppliedCrossChecks(); }
function appendCategoryCrossCheckRemoveButton(container, ccId) {
    const removeButton = document.createElement('button');
    removeButton.type = 'button';
    removeButton.className = 'ar-btn-secondary';
    removeButton.textContent = 'Remove';
    removeButton.title = 'Remove this Cross-check from the Category';
    removeButton.addEventListener('click', () => removeCategoryCrossCheck(ccId));
    container.appendChild(removeButton);
}
function addCategoryCrossCheck() {
    const picker = document.getElementById('arCategoryAddCrossCheckSelect');
    if (!picker || !picker.value) return;
    const ccId = parseInt(picker.value, 10);
    if (!Number.isInteger(ccId) || categoryCrossCheckIds.includes(ccId)) return;
    const crossCheck = getCrossCheckById(ccId);
    if (!crossCheck || crossCheck.is_active === false) return;
    const dependencyIssues = getCategoryCrossCheckDependencyIssues(crossCheck);
    if (dependencyIssues.crossCheckInactive || dependencyIssues.missing.length || dependencyIssues.inactive.length) {
        showCategoryDetailStatus(
            'Cannot apply this Cross-check: ' + categoryCrossCheckDependencyMessage(dependencyIssues) + '.',
            'error'
        );
        return;
    }
    if (!categoryCrossCheckIds.includes(ccId)) categoryCrossCheckIds.push(ccId);
    renderAppliedCrossChecks();
}
function removeCategoryCrossCheck(ccId) {
    categoryCrossCheckIds = categoryCrossCheckIds.filter(id => id !== ccId);
    renderAppliedCrossChecks();
}

// ---------- SELECT / NEW / RESET DETAIL PANEL ----------
async function selectCategory(id) {
    beginAuditRulesProcessing('Loading Category details...');
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/categories/' + id);
        if (!res.ok) throw new Error('Category not found');
        const cat = await res.json();

        editingCategoryId = cat.id;
        arCategoryEditingIdInput.value = cat.id;
        arCategoryNameInput.value = cat.name;
        arCategoryKbOverviewInput.value = cat.kb_overview || '';
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
        await loadCategoryValidationRules(cat.id);
        await renderAppliedCrossChecks();

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
    } finally {
        endAuditRulesProcessing();
    }
}

function startNewCategory() {
    editingCategoryId = null;
    arCategoryEditingIdInput.value = '';
    arCategoryNameInput.value = '';
    arCategoryKbOverviewInput.value = '';
    categoryDocIds = [];
    categoryCrossCheckIds = [];
    categoryDocRequired = {};
    categoryDocMeta = {};
    arCategoryMatrixVersion.textContent = '1';
    renderCategoryDocsList();
    document.getElementById('arProcedureRulesBody').innerHTML = '';
    categoryLegacyMathRuleCount = 0;
    renderAppliedCrossChecks();

    arCategoryUsedByRow.style.display = 'none';
    arCategoryDeleteBtn.style.display = 'none';
    arCategoryRestoreBtn.style.display = 'none';

    arCategoryDetailEmpty.style.display = 'none';
    arCategoryDetailForm.style.display = 'block';
    arCategoryNameInput.focus();

    updateAuditRulesUrl();
}

// ---------- SAVE ----------
async function saveCategory(triggerBtn) {
    const name = arCategoryNameInput.value.trim();
    if (!name) {
        showCategoryDetailStatus('❌ Name is required.', 'error');
        return;
    }

    const validationRules = readCategoryValidationRules();
    /* D2: Category document membership is canonical; legacy derived-document expansion is retired.
    try {
        appliedDefinitions = [];
    } catch (error) {
        showCategoryDetailStatus('❌ Error: ' + error.message, 'error');
        return;
    }
    const appliedDocumentIds = appliedDefinitions.flatMap(definition =>
        (definition.matrix.documents || []).map(document => Number(document.document_id))
    );
    const ruleDocumentIds = [
        ...validationRules.procedure_evidence.checks.map(rule => Number(rule.document_id)),
        ...appliedDocumentIds
    ].filter(id => Number.isInteger(id) && id > 0); */
    const payload = {
        name: name,
        kb_overview: arCategoryKbOverviewInput.value.trim() || null,
        document_ids: [...new Set(categoryDocIds)],
        cross_check_ids: categoryCrossCheckIds
    };

    beginAuditRulesProcessing('Saving Category...');
    try {
        window.LoadingUI.showSpinner(triggerBtn, 'Saving');

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

        const savedId = editingCategoryId || result.category_id;
        if (savedId) {
            const rulesRes = await fetch(API_BASE + '/api/audit-rules/categories/' + savedId + '/validation-rules', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(validationRules) });
            const rulesResult = await rulesRes.json();
            if (!rulesRes.ok) throw new Error(rulesResult.detail || 'Failed to save validation rules');
        }

        showCategoryDetailStatus('✅ ' + result.message, 'success');

        // POST doesn't echo back the full row, so re-fetch the list, then re-select
        // to refresh the detail panel's is_active/used_by state from the server.
        invalidateCategoriesListCache();
        await loadCategories();
        await selectCategory(savedId);
    } catch (error) {
        showCategoryDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        window.LoadingUI.hideSpinner(triggerBtn);
        endAuditRulesProcessing();
    }
}

// ---------- DELETE / RESTORE ----------
async function deleteCategory() {
    if (!editingCategoryId) return;
    if (!confirm('Are you sure you want to delete "' + arCategoryNameInput.value + '"? This will soft-delete it.')) return;

    beginAuditRulesProcessing('Deleting Category...');
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/categories/' + editingCategoryId, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to delete category');

        showCategoryDetailStatus('✅ ' + result.message, 'success');
        invalidateCategoriesListCache();
        await loadCategories();
        await selectCategory(editingCategoryId);
    } catch (error) {
        showCategoryDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
    }
}

async function restoreCategory() {
    if (!editingCategoryId) return;

    beginAuditRulesProcessing('Restoring Category...');
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/categories/' + editingCategoryId + '/restore', { method: 'POST' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to restore category');

        showCategoryDetailStatus('✅ ' + result.message, 'success');
        invalidateCategoriesListCache();
        await loadCategories();
        await selectCategory(editingCategoryId);
    } catch (error) {
        showCategoryDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
    }
}

// ---------- WIRE UP EVENT LISTENERS ----------
arCategoryAddNewBtn.addEventListener('click', startNewCategory);
arCategoryShowInactiveCheckbox.addEventListener('change', loadCategories);
arCategoryLoadMoreBtn.addEventListener('click', loadMoreCategories);

arCategorySaveBtn.addEventListener('click', () => saveCategory(arCategorySaveBtn));
arCategoryAddDocumentBtn?.addEventListener('click', addCategoryDocument);
arCategoryAddDocumentSelect?.addEventListener('change', () => {
    arCategoryAddDocumentBtn.style.display = arCategoryAddDocumentSelect.value ? 'inline-block' : 'none';
});
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

// Momentary success toast (bottom-right, auto-dismisses) - same pattern as showVerifyToast() in
// procedure_modal.js, reusing its .voucher-verify-toast CSS. This page doesn't load
// procedure_modal.js (it self-installs a large unrelated modal component), so this is its own
// small local copy rather than a shared function. Success-only feedback, per that convention -
// errors still go through showAssignCategoriesStatus() above.
const arAssignCategoriesToast = document.getElementById('arAssignCategoriesToast');
let arAssignCategoriesToastTimeout = null;
function showAssignCategoriesToast(msg) {
    if (!arAssignCategoriesToast) return;
    arAssignCategoriesToast.textContent = msg;
    arAssignCategoriesToast.classList.add('show');
    clearTimeout(arAssignCategoriesToastTimeout);
    arAssignCategoriesToastTimeout = setTimeout(() => {
        arAssignCategoriesToast.classList.remove('show');
    }, 2000);
}

// ---------- OPEN / CLOSE (whole-shell swap) ----------
// async/await added for the Verification -> Assign Categories deep link (Phase 5), which needs
// to know once the accounts have actually loaded before it can locate/highlight a row. The
// existing in-app button click listener ignores the returned promise, so this is fully
// backward-compatible with that call site.
async function openAssignCategoriesView() {
    beginAuditRulesProcessing('Opening Assign Categories...');
    try {
    // Confirmed fix: window.onload firing doesn't guarantee client_context.js's async session
    // fetch has resolved yet - a fast click on the "Assign Categories" button right after page
    // load can fire before getSelectedClientId() has a real value, sending client_id=null to
    // the accounts API (422). Guarding here (not just the URL-restore caller below) covers
    // every entry path - button click, Verification deep link, and hard-refresh restore - with
    // one guard instead of needing it duplicated per caller.
    if (window.clientContextReady) await window.clientContextReady;

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
    } finally {
        endAuditRulesProcessing();
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

    beginAuditRulesProcessing('Loading Assign Categories data...');
    try {
        const [categoriesRes, accountsRes] = await Promise.all([
            window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/categories?include_inactive=false'),
            window.ApiUtils.apiFetch(API_BASE + '/api/trial-balance/latest-accounts?client_id=' + clientId)
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
    } finally {
        endAuditRulesProcessing();
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
    beginAuditRulesProcessing('Refreshing Category options...');
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/categories?include_inactive=false');
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
    } finally {
        endAuditRulesProcessing();
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

    beginAuditRulesProcessing('Saving Category assignment...');
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
            showAssignCategoriesToast('✅ ' + actionLabel + '.');
        } else {
            showAssignCategoriesToast(
                '✅ ' + actionLabel + (forceApply ? ' (force-applied)' : '') + '. ' +
                result.descendants_updated_count + ' descendant(s) updated, ' +
                result.descendants_skipped_count + ' left unchanged.'
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
    } finally {
        endAuditRulesProcessing();
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

    beginAuditRulesProcessing('Resetting Category assignments...');
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
    } finally {
        endAuditRulesProcessing();
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
    // If the user clicked a rail while page-start work was still pending, that click is the
    // authoritative view choice for this page life. The click handler has already started the
    // required list load, so replaying the URL here would only risk putting an older view back.
    if (userSelectedEntityType) return;

    const restoreParams = new URLSearchParams(window.location.search);
    const viewParam = restoreParams.get('view');

    if (viewParam === 'assign-categories') {
        // Confirmed fix: window.onload firing doesn't guarantee client_context.js's session
        // fetch has resolved yet, so getSelectedClientId() can still be null here on a hard
        // refresh, sending client_id=null to the accounts API (422). Wait for the session
        // fetch, then only enter this view if a client actually ended up selected — otherwise
        // fall through to the normal entity/id restore below (matches a non-deep-link load).
        if (window.clientContextReady) await window.clientContextReady;
        if (getSelectedClientId()) {
            await openAssignCategoriesView();
            return;
        }
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
        // The user may have clicked a different rail while the restore loads were in flight.
        // Do not select an item from the stale URL view after that newer choice took over.
        if (userSelectedEntityType && userSelectedEntityType !== restoredEntity) return;
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
    await loadTemplatesForDocuments();

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
let arVoucherCheckSortableWarningShown = false;
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
    beginAuditRulesProcessing('Loading Voucher-level Checks…');
    window.LoadingUI.showSkeleton(arVoucherCheckListBody, 6);
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/voucher-checks?include_inactive=' + includeInactive);
        if (!res.ok) throw new Error('Failed to load voucher-level checks');
        const checks = await res.json(); // already ORDER BY sort_order server-side -- do not re-sort here
        allVoucherChecksCache = checks;
        renderVoucherCheckList(checks, includeInactive);
    } catch (error) {
        arVoucherCheckListBody.innerHTML = '';
        arVoucherCheckListEmptyMessage.textContent = '❌ Error loading voucher-level checks: ' + error.message;
        arVoucherCheckListEmptyMessage.style.display = 'block';
    } finally {
        endAuditRulesProcessing();
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
    // Sortable.js is an optional CDN enhancement. Its absence must not reject
    // loadVoucherChecks(), because Categories and Assign Categories deliberately chain
    // that loader before their own data requests.
    if (typeof Sortable === 'undefined') {
        if (!arVoucherCheckSortableWarningShown) {
            console.warn('Sortable.js unavailable; drag-to-reorder is disabled.');
            arVoucherCheckSortableWarningShown = true;
        }
        return;
    }

    try {
        arVoucherCheckSortable = new Sortable(arVoucherCheckListBody, {
        handle: '.ar-drag-handle',
        animation: 150,
        ghostClass: 'sortable-ghost',
        disabled: arVoucherCheckShowInactiveCheckbox.checked,
        onEnd: async function () {
            const orderedIds = Array.from(arVoucherCheckListBody.children).map(row => parseInt(row.dataset.id, 10));
            beginAuditRulesProcessing('Saving Voucher-level Check order...');
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
            } finally {
                endAuditRulesProcessing();
            }
        }
        });
    } catch (error) {
        // A malformed/partially-loaded optional library is treated like a missing one.
        // The entity list itself remains usable; only drag-to-reorder is unavailable.
        console.warn('Sortable.js could not be initialized; drag-to-reorder is disabled.', error);
    }
}

// ---------- CHIP EDITOR (Authorisation Roles) ----------
// Reuses the existing generic renderChipList(container, items, onRemove) helper (defined
// above, shared across Documents/etc.) rather than duplicating it.
// ---------- SELECT / NEW ----------
async function selectVoucherCheck(id) {
    beginAuditRulesProcessing('Loading Voucher-level Check details...');
    try {
        const res = await window.ApiUtils.apiFetch(API_BASE + '/api/audit-rules/voucher-checks/' + id);
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
    } finally {
        endAuditRulesProcessing();
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
async function saveVoucherCheck(triggerBtn) {
    const name = arVoucherCheckNameInput.value.trim();
    if (!name) {
        showVoucherCheckDetailStatus('⚠️ Name is required.', 'error');
        return;
    }

    const payload = { name: name, authority: arVoucherCheckAuthorityInput.value.trim() || null };

    beginAuditRulesProcessing('Saving Voucher-level Check...');
    try {
        window.LoadingUI.showSpinner(triggerBtn, 'Saving');

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
    } finally {
        window.LoadingUI.hideSpinner(triggerBtn);
        endAuditRulesProcessing();
    }
}

async function deleteVoucherCheck() {
    if (!editingVoucherCheckId) return;
    if (!confirm('Are you sure you want to delete "' + arVoucherCheckNameInput.value + '"? This will soft-delete it.')) return;

    beginAuditRulesProcessing('Deleting Voucher-level Check...');
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/voucher-checks/' + editingVoucherCheckId, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to delete voucher-level check');

        showVoucherCheckDetailStatus('✅ ' + result.message, 'success');
        await loadVoucherChecks();
        await selectVoucherCheck(editingVoucherCheckId);
    } catch (error) {
        showVoucherCheckDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
    }
}

async function restoreVoucherCheck() {
    if (!editingVoucherCheckId) return;

    beginAuditRulesProcessing('Restoring Voucher-level Check...');
    try {
        const res = await fetch(API_BASE + '/api/audit-rules/voucher-checks/' + editingVoucherCheckId + '/restore', { method: 'POST' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || result.message || 'Failed to restore voucher-level check');

        showVoucherCheckDetailStatus('✅ ' + result.message, 'success');
        await loadVoucherChecks();
        await selectVoucherCheck(editingVoucherCheckId);
    } catch (error) {
        showVoucherCheckDetailStatus('❌ Error: ' + error.message, 'error');
    } finally {
        endAuditRulesProcessing();
    }
}

// ---------- WIRE UP EVENT LISTENERS ----------
arVoucherCheckAddNewBtn.addEventListener('click', startNewVoucherCheck);
arVoucherCheckShowInactiveCheckbox.addEventListener('change', loadVoucherChecks);

arVoucherCheckSaveBtn.addEventListener('click', () => saveVoucherCheck(arVoucherCheckSaveBtn));
arVoucherCheckDeleteBtn.addEventListener('click', deleteVoucherCheck);
arVoucherCheckRestoreBtn.addEventListener('click', restoreVoucherCheck);
