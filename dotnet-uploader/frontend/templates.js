// ==============================================
// 📄 MANAGE TEMPLATES - PIVOT TABLE VERSION
// ==============================================

// API_BASE is defined globally in client_context.js
const nativeTenantTemplates = window.AppTenantTemplates?.enabled === true;
const canEditTenantTemplates = !nativeTenantTemplates || window.AppTenantTemplates.canEdit === true;
let templateConfigurationKey = null;
let nativeTemplateKind = null;
window.TemplateAccountingFields = (() => {
    const state = {current:{}, generation:0};
    const accountingFieldRoles = [
        ['amount', 'Amount'], ['quantity', 'Quantity'], ['rate', 'Rate'],
        ['account_code', 'Account Code'], ['account_name', 'Account Name'],
        ['debit', 'Debit'], ['credit', 'Credit'], ['description', 'Description'], ['line_id', 'Line ID (optional)']
    ];

    function accountingTypeMatches(role, column) {
        if (['amount', 'quantity', 'rate', 'debit', 'credit'].includes(role)) return column.type === 'Numeric';
        if (['account_code', 'line_id'].includes(role)) return column.type === 'Text';
        return true;
    }

    function accountingBindingError() {
        for (const [role, id] of Object.entries(state.current)) {
            const column = selectedColumns.find(c => c.id === id);
            if (!column || column.comparison_review_required || !accountingTypeMatches(role, column))
                return 'Review the accounting assignments. Each field must use a compatible selected column.';
        }
        const sides = ['account_code', 'debit', 'credit'].filter(role => state.current[role]);
        if (sides.length && sides.length !== 3) return 'Choose Account Code, Debit and Credit together.';
        if (state.current.rate && ['amount', 'quantity'].some(role => state.current[role] === state.current.rate))
            return 'Rate must use a different column from Amount and Quantity.';
        if (state.current.debit && state.current.debit === state.current.credit) return 'Debit and Credit must use different columns.';
        return '';
    }

    function renderAccountingBindings() {
        const section = document.getElementById('accountingBindingFields');
        section.hidden = !nativeTenantTemplates || ledgerPurposeSelect.value === 'account_ledger';
        if (section.hidden) return;
        const controls = document.getElementById('accountingBindingControls');
        controls.replaceChildren();
        accountingFieldRoles.forEach(([role, label]) => {
            const field = document.createElement('label'); field.textContent = label;
            const select = document.createElement('select'); select.id = 'accounting-field-' + role;
            select.setAttribute('aria-label', 'Accounting field: ' + label);
            select.style.cssText = 'display:block;width:100%;padding:8px;margin-top:4px;border:1px solid #cbd5e1;border-radius:6px;';
            select.appendChild(new Option('No column selected', ''));
            selectedColumns.filter(c => !c.comparison_review_required && accountingTypeMatches(role, c))
                .forEach(c => select.appendChild(new Option(c.name, String(c.id))));
            const chosen = state.current[role];
            if (chosen && !Array.from(select.options).some(o => o.value === String(chosen))) {
                const missing = new Option('Previous assignment needs review', String(chosen)); missing.disabled = true; select.appendChild(missing);
            }
            select.value = chosen ? String(chosen) : '';
            select.addEventListener('change', () => {
                if (select.value) state.current[role] = Number(select.value); else delete state.current[role];
                document.getElementById('accountingBindingStatus').textContent = accountingBindingError();
            });
            field.appendChild(select); controls.appendChild(field);
        });
        document.getElementById('accountingBindingStatus').textContent = accountingBindingError();
    }

    return {state, render:renderAccountingBindings, error:accountingBindingError};
})();

// DOM refs
const templatesBody = document.getElementById('templatesBody');
const noTemplatesMsg = document.getElementById('noTemplatesMessage');
const totalCountSpan = document.getElementById('totalCount');
const statusDiv = document.getElementById('templateStatus');

const modalOverlay = document.getElementById('templateModalOverlay');
const modal = document.getElementById('templateModal');
const modalTitle = document.getElementById('modalTitle');
const templateName = document.getElementById('templateName');
const rateSourceField = document.getElementById('rateSourceField');
const rateSourceSelect = document.getElementById('rateSourceSelect');
const hasSubtotalsCheckbox = document.getElementById('hasSubtotalsByDefaultCheckbox');
const searchInput = document.getElementById('columnSearchInput');
const clearSearchBtn = document.getElementById('clearSearchBtn');
const addColumnTriggerBtn = document.getElementById('addColumnTriggerBtn');
const closeMatchHint = document.getElementById('closeMatchHint');
const searchResults = document.getElementById('searchResults');
const noColumnsMsg = document.getElementById('noColumnsMessage');
const noSearchResults = document.getElementById('noSearchResults');
const selectedColumnsList = document.getElementById('selectedColumnsList');
const emptySelectedMsg = document.getElementById('emptySelectedMessage');
const selectedCountBadge = document.getElementById('selectedCountBadge');
const editId = document.getElementById('editTemplateId');
const cancelBtn = document.getElementById('cancelTemplateBtn');
const closeModalBtn = document.getElementById('closeModalBtn');
const modalStatus = document.getElementById('modalStatus');

// Quick Add Column modal (Phase 3) refs
const quickAddOverlay = document.getElementById('quickAddColumnOverlay');
const quickAddModal = document.getElementById('quickAddColumnModal');
const quickAddColName = document.getElementById('quickAddColName');
const quickAddColType = document.getElementById('quickAddColType');
const quickAddColCategory = document.getElementById('quickAddColCategory');
const quickAddColNotes = document.getElementById('quickAddColNotes');
const quickAddCategoryDropdown = document.getElementById('quickAddCategoryDropdown');
const quickAddModalStatus = document.getElementById('quickAddModalStatus');
const quickAddCancelBtn = document.getElementById('quickAddCancelBtn');
const quickAddSaveBtn = document.getElementById('quickAddSaveBtn');
const quickAddSaveAndNewBtn = document.getElementById('quickAddSaveAndNewBtn');

// Mirrors RATE_SOURCE_ELIGIBLE_TEMPLATES in Templates API -- keep in sync.
const RATE_SOURCE_ELIGIBLE_TEMPLATES = new Set(['purchase order', 'store receiving', 'vendor bill']);

function updateRateSourceVisibility() {
    const eligibleKind = nativeTenantTemplates && nativeTemplateKind !== null
        ? nativeTemplateKind !== 'standard' : RATE_SOURCE_ELIGIBLE_TEMPLATES.has(templateName.value.trim().toLowerCase());
    const eligible = ledgerPurposeSelect?.value !== 'account_ledger' && eligibleKind;
    rateSourceField.style.display = eligible ? 'block' : 'none';
}

templateName.addEventListener('input', updateRateSourceVisibility);

let editingId = null;
let allMasterColumns = [];
window.allMasterColumns = [];
let selectedColumns = [];
let closeMatchDebounceTimer = null;
let masterColumnsLoaded = false;
let masterColumnsInFlight = null;
let templatesCache = null;
let templateLoadRevision = 0;
const templateActionsInFlight = new Set();

function startTemplateLoading(target) {
    if (!window.LoadingUI || typeof window.LoadingUI.startGlobalLoading !== 'function') return null;
    return window.LoadingUI.startGlobalLoading(target);
}

function stopTemplateLoading(token) {
    if (token && window.LoadingUI && typeof window.LoadingUI.stopGlobalLoading === 'function') {
        window.LoadingUI.stopGlobalLoading(token);
    }
}

// Ledger setup is a draft until Save; missing master columns are created in
// the same server transaction as the template, never while opening the form.
const ledgerPurposeSelect = document.getElementById('templatePurpose');
const ledgerRoleDefinitions = [
    ['voucher', 'Voucher', 'Voucher ID', 'Text'], ['date', 'Date', 'Transaction Date', 'Date'],
    ['description', 'Description', 'Voucher Detail', 'Text'], ['debit', 'Debit', 'Debit Amount', 'Numeric'],
    ['credit', 'Credit', 'Credit Amount', 'Numeric'], ['balance', 'Balance', 'Balance', 'Text']
];
let ledgerColumnRoles = {};
let ledgerSetupDraft = null;
function ledgerRoleTypeMatches(role, kind) {
    return role === 'balance' ? ['Text', 'Numeric'].includes(kind)
        : ledgerRoleDefinitions.find(item => item[0] === role)[3] === kind;
}
function syncPreparedLedgerColumns() {
    if (!ledgerSetupDraft) return;
    const previous = new Map(selectedColumns.map(c => [c.id, c.compare_for_changes]));
    selectedColumns = ledgerRoleDefinitions.map(([role, label, name, kind], index) => {
        const id = ledgerColumnRoles[role];
        const existing = allMasterColumns.find(col => col.id === id);
        const column = existing ? {id, name: existing.column_name, type: existing.data_type,
            storage_key: existing.storage_key, definition_version: existing.definition_version}
            : {id: -(index + 1), name: name + ' (create on Save)', type: kind};
        if (typeof previous.get(column.id) === 'boolean') column.compare_for_changes = previous.get(column.id);
        return column;
    });
}
function renderLedgerRoleControls() {
    const ledgerMode = ledgerPurposeSelect.value === 'account_ledger';
    document.getElementById('ledgerRoleFields').hidden = !ledgerMode;
    ledgerPurposeSelect.disabled = Boolean(ledgerSetupDraft);
    document.getElementById('templateColumnSearch').hidden = Boolean(ledgerSetupDraft);
    document.getElementById('templateColumnOrderHint').textContent = ledgerSetupDraft
        ? 'Prepared columns follow the ledger role order. Review or change the role selections above.'
        : 'Drag ☰ to reorder. Click ✕ to remove a column.';
    const container = document.getElementById('ledgerRoleControls');
    container.replaceChildren();
    if (!ledgerMode) return;
    ledgerRoleDefinitions.forEach(([role, label, name, kind], index) => {
        const choices = ledgerSetupDraft
            ? allMasterColumns.map(c => ({id: c.id, name: c.column_name, type: c.data_type})) : selectedColumns;
        const field = document.createElement('label'); field.textContent = label;
        const select = document.createElement('select'); select.id = 'ledgerRole-' + role;
        select.style.cssText = 'display:block;width:100%;margin-top:4px;padding:8px;border:1px solid #cbd5e1;border-radius:6px;';
        const empty = new Option('Select column', ''); select.appendChild(empty);
        choices.filter(c => ledgerRoleTypeMatches(role, c.type)).forEach(c => select.appendChild(new Option(c.name, c.id)));
        if (ledgerSetupDraft && !ledgerSetupDraft.columns[index].column_id) {
            select.appendChild(new Option('Create ' + name + ' on Save', -(index + 1)));
        }
        if (!ledgerColumnRoles[role]) {
            const match = choices.find(c => c.name.trim().toLowerCase() === name.toLowerCase() && ledgerRoleTypeMatches(role, c.type));
            if (match) ledgerColumnRoles[role] = match.id;
        }
        select.value = String(ledgerColumnRoles[role] || '');
        if (!select.value) delete ledgerColumnRoles[role];
        select.addEventListener('change', () => {
            ledgerColumnRoles[role] = Number(select.value) || null;
            if (ledgerSetupDraft) { syncPreparedLedgerColumns(); renderSelectedColumns(); updateSelectedCount(); }
            else renderSelectedColumns();
        });
        field.appendChild(select); container.appendChild(field);
    });
    document.getElementById('ledgerSetupHint').textContent = ledgerSetupDraft
        ? 'Existing compatible columns will be reused. Missing columns are created only when you save this template.'
        : 'Column roles are saved with this template. Imported ledger data protects these settings from reassignment.';
}
ledgerPurposeSelect.addEventListener('change', () => {
    ledgerColumnRoles = {}; renderLedgerRoleControls(); updateRateSourceVisibility();
    window.TemplateAccountingFields.render();
    renderSelectedColumns();
});
document.getElementById('prepareLedgerBtn').addEventListener('click', async function() {
    if (!canEditTenantTemplates) return;
    if (templateActionsInFlight.has('ledger-prepare')) return;
    templateActionsInFlight.add('ledger-prepare');
    const token = startTemplateLoading(this);
    try {
        await loadTemplates();
        const existing = (templatesCache || []).find(t => t.template_name.trim().toLowerCase() === 'account ledger' && t.data_purpose === 'account_ledger');
        if (existing) { await openModal('Edit Account Ledger', existing, this); return; }
        const response = await fetch(API_BASE + '/api/templates/ledger-setup');
        const draft = await response.json();
        if (!response.ok) throw new Error(draft.detail || 'Ledger setup could not be prepared.');
        await openModal('Prepare Account Ledger', null, this);
        ledgerSetupDraft = draft;
        templateName.value = draft.template_name;
        ledgerPurposeSelect.value = 'account_ledger';
        ledgerColumnRoles = Object.fromEntries(draft.columns.map((c, i) => [c.role, c.column_id || -(i + 1)]));
        syncPreparedLedgerColumns(); renderSelectedColumns(); updateSelectedCount(); renderLedgerRoleControls();
    } catch (error) { showStatus('Ledger setup: ' + error.message, 'error'); }
    finally { stopTemplateLoading(token); templateActionsInFlight.delete('ledger-prepare'); }
});

// ---------- LOAD TEMPLATES ----------
// forceRefresh=true bypasses the cache after a create/edit/delete that changed server
// state (Fast Data Handling, Phase 8) -- the initial load leaves the cache empty so it
// fetches normally.
async function loadTemplates(forceRefresh = false, target = null) {
    if (templatesCache && !forceRefresh && !nativeTenantTemplates) {
        renderTemplates(templatesCache);
        totalCountSpan.textContent = templatesCache.length;
        return;
    }
    const requestRevision = ++templateLoadRevision;
    const loadingToken = startTemplateLoading(target);
    try {
        const res = await fetch(API_BASE + '/api/templates');
        if (!res.ok) throw new Error('Failed to fetch templates');
        const rows = await res.json();
        if (requestRevision !== templateLoadRevision) return;
        templatesCache = rows;
        renderTemplates(templatesCache);
        totalCountSpan.textContent = templatesCache.length;
    } catch (error) {
        if (requestRevision !== templateLoadRevision) return;
        showStatus('❌ Error loading templates: ' + error.message, 'error');
        templatesBody.innerHTML = '';
        noTemplatesMsg.style.display = 'block';
        totalCountSpan.textContent = '0';
    } finally {
        stopTemplateLoading(loadingToken);
    }
}

// ---------- RENDER TEMPLATES TABLE ----------
function renderTemplates(templates) {
    templatesBody.innerHTML = '';
    if (!templates || templates.length === 0) {
        noTemplatesMsg.style.display = 'block';
        return;
    }
    noTemplatesMsg.style.display = 'none';

    templates.forEach(tpl => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #e2e8f0';

        // ID
        const tdId = document.createElement('td');
        tdId.textContent = tpl.id;
        tdId.style.padding = '10px 16px';

        // Name
        const tdName = document.createElement('td');
        tdName.textContent = tpl.template_name;
        tdName.style.padding = '10px 16px';
        tdName.style.fontWeight = '500';

        // Columns – now using tpl.columns (from pivot table)
        const tdCols = document.createElement('td');
        tdCols.style.padding = '10px 16px';
        const cols = tpl.columns || [];
        let colDisplay = [];
        if (cols.length > 0) {
            colDisplay = cols.map((col, idx) => `${idx+1}. ${col.column_name} (ID: ${col.id})`);
        }
        tdCols.textContent = colDisplay.join(', ') || '—';
        tdCols.title = colDisplay.join(', ') || 'No columns';

        // Created By
        const tdBy = document.createElement('td');
        tdBy.textContent = tpl.created_by || 'Admin';
        tdBy.style.padding = '10px 16px';

        // Created Date
        const tdDate = document.createElement('td');
        tdDate.style.padding = '10px 16px';
        tdDate.textContent = window.formatDate(tpl.created_date);

        // Active
        const tdActive = document.createElement('td');
        tdActive.style.padding = '10px 16px';
        tdActive.style.textAlign = 'center';
        const activeSpan = document.createElement('span');
        activeSpan.textContent = tpl.is_active ? '✅' : '❌';
        activeSpan.style.fontSize = '16px';
        tdActive.appendChild(activeSpan);

        // Actions
        const tdActions = document.createElement('td');
        tdActions.style.padding = '10px 16px';
        tdActions.style.textAlign = 'center';

        // Preview (Eye)
        const previewBtn = document.createElement('button');
        previewBtn.textContent = '👁️';
        previewBtn.style.background = 'none';
        previewBtn.style.border = 'none';
        previewBtn.style.cursor = 'pointer';
        previewBtn.style.fontSize = '18px';
        previewBtn.title = 'Preview template';
        previewBtn.addEventListener('click', () => openPreviewModal(tpl, previewBtn));
        tdActions.appendChild(previewBtn);

        // Edit
        const editBtn = document.createElement('button');
        editBtn.textContent = '✏️';
        editBtn.style.background = 'none';
        editBtn.style.border = 'none';
        editBtn.style.cursor = 'pointer';
        editBtn.style.fontSize = '18px';
        editBtn.style.marginLeft = '8px';
        editBtn.title = 'Edit template';
        editBtn.disabled = !canEditTenantTemplates;
        editBtn.addEventListener('click', () => openEditModal(tpl, editBtn));
        tdActions.appendChild(editBtn);

        // Delete – hide for system templates
        if (!tpl.is_system) {
            const delBtn = document.createElement('button');
            delBtn.textContent = '🗑️';
            delBtn.style.background = 'none';
            delBtn.style.border = 'none';
            delBtn.style.cursor = 'pointer';
            delBtn.style.fontSize = '18px';
            delBtn.style.marginLeft = '8px';
            delBtn.title = 'Delete template';
            delBtn.disabled = !canEditTenantTemplates;
            delBtn.addEventListener('click', () => confirmDelete(tpl.id, tpl.template_name, delBtn));
            tdActions.appendChild(delBtn);
        } else {
            // Show a lock icon for system templates
            const lock = document.createElement('span');
            lock.textContent = '🔒';
            lock.style.marginLeft = '8px';
            lock.title = 'System template – cannot be deleted';
            lock.style.color = '#94a3b8';
            tdActions.appendChild(lock);
        }

        tr.appendChild(tdId);
        tr.appendChild(tdName);
        tr.appendChild(tdCols);
        tr.appendChild(tdBy);
        tr.appendChild(tdDate);
        tr.appendChild(tdActive);
        tr.appendChild(tdActions);
        templatesBody.appendChild(tr);
    });
}

// ---------- LOAD MASTER COLUMNS ----------
// Fetch-once cache (Fast Data Handling, Phase 8) -- this used to re-fetch on every
// Add/Edit modal open and every Preview open. No invalidation needed: Quick Add already
// patches allMasterColumns directly (see saveQuickAddColumn()), so the cache stays correct.
async function loadMasterColumns(target = null) {
    if (masterColumnsLoaded && !nativeTenantTemplates) return allMasterColumns;
    if (masterColumnsInFlight) return masterColumnsInFlight;

    const loadingToken = startTemplateLoading(target);
    masterColumnsInFlight = (async function () {
        try {
            const res = await fetch(API_BASE + '/api/columns');
            if (!res.ok) throw new Error('Failed to fetch columns');
            allMasterColumns = await res.json();
            window.allMasterColumns = allMasterColumns;
            masterColumnsLoaded = true;
            return allMasterColumns;
        } catch (error) {
            return [];
        } finally {
            masterColumnsInFlight = null;
            stopTemplateLoading(loadingToken);
        }
    })();
    return masterColumnsInFlight;
}

// ---------- STATUS HELPERS ----------
function showStatus(msg, type) {
    statusDiv.textContent = msg;
    statusDiv.className = 'upload-status ' + type;
    statusDiv.style.display = 'block';
    clearTimeout(window.statusTimeout);
    window.statusTimeout = setTimeout(() => { statusDiv.style.display = 'none'; }, 5000);
}

function showModalStatus(msg, type) {
    modalStatus.textContent = msg;
    modalStatus.style.display = 'block';
    modalStatus.style.background = type === 'error' ? '#ffebee' : '#e8f5e9';
    modalStatus.style.color = type === 'error' ? '#c62828' : '#2e7d32';
    modalStatus.style.border = '1px solid ' + (type === 'error' ? '#ef9a9a' : '#a5d6a7');
    setTimeout(() => { modalStatus.style.display = 'none'; }, 4000);
}

// ---------- NOTES / INTERPRETATION TOOLTIP HELPER ----------
// Looks up a master column's Notes/Interpretation text by id from the
// already-loaded allMasterColumns list (Rate-derivation requirement, Phase 4
// of 9). Returns '' when there's no column, no notes, or a blank/whitespace
// note -- callers use this to decide whether to render a tooltip icon at all.
function getColumnNotesById(columnId) {
    const col = allMasterColumns.find(c => c.id === columnId);
    const notes = col && col.notes ? col.notes.trim() : '';
    return notes;
}

// Minimal HTML-escape for interpolating free-text Notes into an innerHTML
// template (used only by the Preview modal, which builds markup via strings).
function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Appends a small "ℹ️" info icon (native title tooltip) after nameSpan when
// notes is non-blank. No-op otherwise -- blank/null Notes get no icon.
function appendNotesIcon(container, notes) {
    if (!notes) return;
    const icon = document.createElement('span');
    icon.textContent = 'ℹ️';
    icon.title = notes;
    icon.style.cssText = 'margin-left:6px; font-size:12px; cursor:help; color:#94a3b8;';
    container.appendChild(icon);
}

// ---------- RENDER SEARCH RESULTS ----------
function renderSearchResults(query) {
    const trimmed = query.trim().toLowerCase();
    searchResults.innerHTML = '';
    searchResults.style.display = 'none';
    noSearchResults.style.display = 'none';

    if (!trimmed) return;

    const filtered = allMasterColumns.filter(col =>
        col.column_name.toLowerCase().includes(trimmed) ||
        (col.category && col.category.toLowerCase().includes(trimmed))
    );

    if (filtered.length === 0) {
        noSearchResults.style.display = 'block';
        return;
    }

    searchResults.style.display = 'block';
    filtered.forEach(col => {
        const isSelected = selectedColumns.some(c => c.id === col.id);
        const div = document.createElement('div');
        div.style.cssText = 'display:flex; justify-content:space-between; align-items:center; padding:8px 12px; border-bottom:1px solid #f0f2f5;';

        const nameWrap = document.createElement('span');
        nameWrap.style.cssText = 'display:flex; align-items:center;';
        const nameSpan = document.createElement('span');
        nameSpan.textContent = col.column_name;
        nameSpan.style.fontSize = '14px';
        nameSpan.style.fontWeight = '500';
        nameWrap.appendChild(nameSpan);
        appendNotesIcon(nameWrap, col.notes ? col.notes.trim() : '');

        const actionBtn = document.createElement('button');
        actionBtn.type = 'button';
        if (isSelected) {
            actionBtn.textContent = '✅ Selected';
            actionBtn.style.cssText = 'background:#e8f5e9; color:#2e7d32; border:1px solid #a5d6a7; padding:4px 14px; border-radius:6px; cursor:pointer; font-size:12px; font-weight:600; display:flex; align-items:center; gap:4px;';
            const cross = document.createElement('span');
            cross.textContent = '✕';
            cross.style.cssText = 'margin-left:6px; font-size:14px; color:#c62828; cursor:pointer;';
            cross.addEventListener('click', function(e) {
                e.stopPropagation();
                removeFromSelected(col.id);
            });
            actionBtn.appendChild(cross);
        } else {
            actionBtn.textContent = 'Select';
            actionBtn.style.cssText = 'background:#3b82f6; color:white; border:none; padding:4px 14px; border-radius:6px; cursor:pointer; font-size:12px; font-weight:500;';
        }

        actionBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            if (selectedColumns.some(c => c.id === col.id)) {
                removeFromSelected(col.id);
            } else {
                addToSelected(col);
            }
        });

        div.appendChild(nameWrap);
        div.appendChild(actionBtn);
        searchResults.appendChild(div);
    });
}

// ---------- ADD COLUMN TRIGGER + CLOSE-MATCH HINT ----------
// Shows the green "+ Add Column" button when typed text has no exact
// (case-insensitive) match among master columns, and an advisory
// "Did you mean ...?" hint from GET /api/columns/close-matches (Phase 1).
function updateAddColumnUI(query) {
    const trimmed = query.trim();
    clearTimeout(closeMatchDebounceTimer);

    if (!trimmed) {
        addColumnTriggerBtn.style.display = 'none';
        closeMatchHint.style.display = 'none';
        return;
    }

    const lowerTrimmed = trimmed.toLowerCase();
    const hasExactMatch = allMasterColumns.some(col => col.column_name.toLowerCase() === lowerTrimmed);
    addColumnTriggerBtn.style.display = hasExactMatch ? 'none' : 'block';

    closeMatchDebounceTimer = setTimeout(async () => {
        try {
            const res = await fetch(API_BASE + '/api/columns/close-matches?name=' + encodeURIComponent(trimmed));
            if (!res.ok) throw new Error('Failed to fetch close matches');
            const matches = await res.json();
            // Guard against a fast typist having already changed the input
            // by the time this debounced fetch resolves.
            if (searchInput.value.trim() !== trimmed) return;
            if (matches.length > 0) {
                closeMatchHint.textContent = `Did you mean "${matches[0].column_name}"?`;
                closeMatchHint.style.display = 'block';
            } else {
                closeMatchHint.style.display = 'none';
            }
        } catch (error) {
            closeMatchHint.style.display = 'none';
        }
    }, 250);
}

// ---------- QUICK ADD COLUMN MODAL (Phase 3) ----------
// Mirrors the Add New Column modal/behavior on Manage Columns (static/columns.js)
// so a missing column can be created and simultaneously added to the template
// under construction, without leaving this screen.

function showQuickAddModalStatus(message, type) {
    quickAddModalStatus.textContent = message;
    quickAddModalStatus.style.display = 'block';
    quickAddModalStatus.style.background = type === 'error' ? '#ffebee' : '#e8f5e9';
    quickAddModalStatus.style.color = type === 'error' ? '#c62828' : '#2e7d32';
    quickAddModalStatus.style.border = '1px solid ' + (type === 'error' ? '#ef9a9a' : '#a5d6a7');
}

async function loadQuickAddCategoryDropdown(target = quickAddCategoryDropdown) {
    const loadingToken = startTemplateLoading(target);
    try {
        const res = await fetch(API_BASE + '/api/columns/categories');
        if (!res.ok) throw new Error('Failed to fetch categories');
        const categories = await res.json();
        quickAddCategoryDropdown.innerHTML = '<option value="">-- Select existing --</option>';
        categories.forEach(cat => {
            const opt = document.createElement('option');
            opt.value = cat.category;
            opt.textContent = cat.category;
            quickAddCategoryDropdown.appendChild(opt);
        });
    } catch (error) {
        // Advisory only -- leave the dropdown at its default option on failure.
    } finally {
        stopTemplateLoading(loadingToken);
    }
}

quickAddCategoryDropdown.addEventListener('change', function() {
    if (this.value) quickAddColCategory.value = this.value;
});

function openQuickAddColumnModal(prefillName) {
    quickAddColName.value = prefillName || '';
    quickAddColType.value = 'Text';
    quickAddColCategory.value = '';
    quickAddColNotes.value = '';
    quickAddCategoryDropdown.value = '';
    quickAddModalStatus.style.display = 'none';
    quickAddOverlay.style.display = 'block';
    quickAddModal.style.display = 'block';
    loadQuickAddCategoryDropdown();
    quickAddColName.focus();
}

function closeQuickAddColumnModal() {
    quickAddOverlay.style.display = 'none';
    quickAddModal.style.display = 'none';
    quickAddModalStatus.style.display = 'none';
}

// Creates the column in the master table and appends it to the template's
// selected-columns list in the same step. keepModalOpen === true is "Save and New".
async function saveQuickAddColumn(keepModalOpen, triggerBtn) {
    if (!canEditTenantTemplates) return;
    const name = quickAddColName.value.trim();
    const type = quickAddColType.value;
    const category = quickAddColCategory.value.trim();
    const notes = quickAddColNotes.value.trim();

    if (!name) {
        showQuickAddModalStatus('⚠️ Column name is required.', 'error');
        return;
    }

    if (templateActionsInFlight.has('quick-add-column')) return;
    templateActionsInFlight.add('quick-add-column');
    const loadingToken = startTemplateLoading(triggerBtn);
    const previousDisabled = triggerBtn ? triggerBtn.disabled : false;
    if (triggerBtn) triggerBtn.disabled = true;

    try {
        window.LoadingUI.showSpinner(triggerBtn, 'Saving');

        const res = await fetch(API_BASE + '/api/columns', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ column_name: name, data_type: type, category: category || 'General', notes: notes || null })
        });
        const result = await res.json();

        if (!res.ok) {
            // Duplicate-name (or other) error from the Phase 1 backend check -- shown here, save blocked.
            throw new Error(result.message || result.detail || 'Failed to create column');
        }

        // New column now exists in the master table -- reflect it locally so
        // search/exact-match logic sees it immediately, then add it to the template.
        const newCol = nativeTenantTemplates ? (await loadMasterColumns()).find(c => c.id === result.column_id)
            : { id: result.column_id, column_name: name, data_type: type, category: category || 'General', notes: notes || null };
        if (!newCol) throw new Error('Column created. Reload the editor to obtain its permanent binding.');
        if (!nativeTenantTemplates) allMasterColumns.push(newCol);
        window.allMasterColumns = allMasterColumns;
        addToSelected(newCol);

        showQuickAddModalStatus('✅ ' + result.message, 'success');

        if (keepModalOpen) {
            quickAddColName.value = '';
            quickAddColType.value = 'Text';
            quickAddColCategory.value = '';
            quickAddColNotes.value = '';
            quickAddCategoryDropdown.value = '';
            quickAddColName.focus();
        } else {
            setTimeout(closeQuickAddColumnModal, 800);
        }

        // Clear the search box now that the typed text has become a real, selected column.
        searchInput.value = '';
        clearSearchBtn.style.display = 'none';
        searchResults.style.display = 'none';
        noSearchResults.style.display = 'none';
        updateAddColumnUI('');

    } catch (error) {
        showQuickAddModalStatus('❌ Error: ' + error.message, 'error');
    } finally {
        window.LoadingUI.hideSpinner(triggerBtn);
        if (triggerBtn) triggerBtn.disabled = previousDisabled;
        stopTemplateLoading(loadingToken);
        templateActionsInFlight.delete('quick-add-column');
    }
}

document.getElementById('quickAddColumnForm').addEventListener('submit', function(e) {
    e.preventDefault();
});

quickAddCancelBtn.addEventListener('click', closeQuickAddColumnModal);
quickAddOverlay.addEventListener('click', closeQuickAddColumnModal);
quickAddSaveBtn.addEventListener('click', function(e) {
    e.preventDefault();
    saveQuickAddColumn(false, quickAddSaveBtn);
});
quickAddSaveAndNewBtn.addEventListener('click', function(e) {
    e.preventDefault();
    saveQuickAddColumn(true, quickAddSaveAndNewBtn);
});

// ---------- ADD / REMOVE SELECTED ----------
function addToSelected(col) {
    if (selectedColumns.some(c => c.id === col.id)) return;
    selectedColumns.push({ id: col.id, name: col.column_name, type: col.data_type, notes: col.notes || null,
        storage_key: col.storage_key, definition_version: col.definition_version });
    renderSelectedColumns();
    renderSearchResults(searchInput.value);
    updateSelectedCount();
}

function removeFromSelected(colId) {
    selectedColumns = selectedColumns.filter(c => c.id !== colId);
    renderSelectedColumns();
    renderSearchResults(searchInput.value);
    updateSelectedCount();
}

function templateColumnComparison(column) {
    if (typeof column.compare_for_changes === 'boolean') return column.compare_for_changes;
    return !(ledgerPurposeSelect.value === 'account_ledger' && Number(column.id) === Number(ledgerColumnRoles.balance));
}

function renderSelectedColumns() {
    window.TemplateAccountingFields.render();
    selectedColumnsList.innerHTML = '';
    const conflicts = selectedColumns.filter(c => c.comparison_review_required).map(c => c.name);
    const review = document.getElementById('templateComparisonReview');
    review.hidden = conflicts.length === 0;
    review.textContent = conflicts.length ? 'Previous client comparison choices differ for: ' + conflicts.join(', ') + '. Review the checkboxes and Save Template before uploading.' : '';
    if (selectedColumns.length === 0) {
        emptySelectedMsg.style.display = 'block';
        return;
    }
    emptySelectedMsg.style.display = 'none';

    selectedColumns.forEach((col, index) => {
        const div = document.createElement('div');
        div.className = 'selected-item';
        div.dataset.id = col.id;
        div.style.cssText = 'padding:8px 10px; border-bottom:1px solid #f0f2f5; background:white; border-radius:6px; margin-bottom:4px;';

        const handle = document.createElement('span');
        handle.textContent = ledgerSetupDraft ? '' : '☰';
        handle.style.cssText = 'cursor:grab; color:#94a3b8; font-size:16px; user-select:none; padding:0 4px;';
        div.appendChild(handle);

        const num = document.createElement('span');
        num.textContent = index + 1;
        num.style.cssText = 'font-weight:600; color:#64748b; min-width:24px; font-size:13px;';
        div.appendChild(num);

        const nameWrap = document.createElement('span');
        nameWrap.style.cssText = 'flex:1; display:flex; align-items:center;';
        const nameSpan = document.createElement('span');
        nameSpan.textContent = col.name;
        nameSpan.style.cssText = 'font-size:14px; font-weight:500; color:#1a1a2e;';
        nameWrap.appendChild(nameSpan);
        appendNotesIcon(nameWrap, col.notes ? col.notes.trim() : '');
        div.appendChild(nameWrap);

        // Roles belong to this selected mapping; the reusable master column is unchanged.
        if (!ledgerSetupDraft) {
            const roleSelect = document.createElement('select');
            roleSelect.className = 'template-reconciliation-role';
            roleSelect.setAttribute('aria-label', 'Reconciliation role for ' + col.name);
            const options = [['', 'Standard']];
            if (col.type === 'Text') options.push(['document_id', 'Document ID']);
            if (col.type === 'Date') options.push(['document_date', 'Document Date']);
            if (col.type === 'Numeric') options.push(['footing', 'Footing']);
            options.forEach(([value, label]) => {
                const option = document.createElement('option');
                option.value = value; option.textContent = label; roleSelect.appendChild(option);
            });
            roleSelect.value = col.reconciliation_role || '';
            roleSelect.disabled = options.length === 1;
            roleSelect.addEventListener('change', () => {
                if (['document_id', 'document_date'].includes(roleSelect.value)) {
                    selectedColumns.forEach(other => {
                        if (other.id !== col.id && other.reconciliation_role === roleSelect.value) other.reconciliation_role = null;
                    });
                }
                col.reconciliation_role = roleSelect.value || null;
                renderSelectedColumns();
            });
            div.appendChild(roleSelect);
        } else {
            div.appendChild(document.createElement('span'));
        }

        const compareCell = document.createElement('span');
        compareCell.className = 'template-compare-cell';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = templateColumnComparison(col);
        checkbox.setAttribute('aria-label', 'Compare for changes: ' + col.name);
        checkbox.addEventListener('change', () => { col.compare_for_changes = checkbox.checked; });
        compareCell.appendChild(checkbox);
        div.appendChild(compareCell);

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.textContent = '✕';
        removeBtn.style.cssText = 'background:none; border:none; color:#dc2626; cursor:pointer; font-size:16px; padding:0 6px;';
        removeBtn.addEventListener('click', function() { removeFromSelected(col.id); });
        if (!ledgerSetupDraft) div.appendChild(removeBtn);
        else div.appendChild(document.createElement('span'));

        selectedColumnsList.appendChild(div);
    });

    if (!ledgerSetupDraft && typeof Sortable !== 'undefined' && selectedColumns.length > 1) {
        new Sortable(selectedColumnsList, {
            handle: '.selected-item span:first-child',
            animation: 150,
            ghostClass: 'sortable-ghost',
            onEnd: function() {
                const items = selectedColumnsList.querySelectorAll('.selected-item');
                const newOrder = [];
                items.forEach(item => {
                    const id = parseInt(item.dataset.id);
                    const col = selectedColumns.find(c => c.id === id);
                    if (col) newOrder.push(col);
                });
                selectedColumns = newOrder;
                const nums = selectedColumnsList.querySelectorAll('.selected-item span:nth-child(2)');
                nums.forEach((num, idx) => { num.textContent = idx + 1; });
                updateSelectedCount();
            }
        });
    }
}

function updateSelectedCount() {
    renderLedgerRoleControls();
    window.TemplateAccountingFields.render();
    const count = selectedColumns.length;
    selectedCountBadge.textContent = count + ' selected';
    selectedCountBadge.style.background = count > 0 ? '#e8f5e9' : '#f1f3f4';
    selectedCountBadge.style.color = count > 0 ? '#2e7d32' : '#94a3b8';
}

// ---------- MODAL OPEN / CLOSE ----------
async function openModal(title, templateData = null, triggerBtn = null) {
    if (!canEditTenantTemplates) return;
    const generation = ++window.TemplateAccountingFields.state.generation;
    // A table may have been rendered before another administrator's save.
    // Start edits with a fresh server snapshot, then submit its revision.
    if (nativeTenantTemplates && templateData) {
        try {
            const response = await fetch(API_BASE + '/api/templates/' + templateData.id);
            if (!response.ok) throw new Error('Template is no longer available. Reload the list.');
            templateData = await response.json();
        } catch (error) { if (generation === window.TemplateAccountingFields.state.generation) showStatus(error.message, 'error'); return; }
    }
    if (generation !== window.TemplateAccountingFields.state.generation) return;
    templateConfigurationKey = templateData?.configuration_key || null;
    nativeTemplateKind = templateData?.template_kind ?? null;
    window.TemplateAccountingFields.state.current = nativeTenantTemplates ? {...(templateData?.upload_bindings || {})} : {};
    ledgerSetupDraft = null;
    ledgerColumnRoles = {};
    ledgerPurposeSelect.value = templateData?.data_purpose || 'standard';
    if (templateData?.ledger_columns) ledgerColumnRoles = typeof templateData.ledger_columns === 'string' ? JSON.parse(templateData.ledger_columns) : {...templateData.ledger_columns};

    selectedColumns = [];
    searchInput.value = '';
    searchResults.style.display = 'none';
    noSearchResults.style.display = 'none';
    modalStatus.style.display = 'none';
    updateAddColumnUI('');

    modalTitle.textContent = title;
    modalOverlay.style.display = 'block';
    modal.style.display = 'block';
    document.body.style.overflow = 'hidden';

    const columns = await loadMasterColumns(triggerBtn);
    if (generation !== window.TemplateAccountingFields.state.generation) return;
    if (columns.length === 0) {
        noColumnsMsg.style.display = 'block';
        searchInput.disabled = true;
    } else {
        noColumnsMsg.style.display = 'none';
        searchInput.disabled = false;
        searchInput.focus();
    }

    if (templateData) {
        editingId = templateData.id;
        templateName.value = templateData.template_name;
        editId.value = templateData.id;
        rateSourceSelect.value = templateData.rate_source || 'auto';
        hasSubtotalsCheckbox.checked = !!templateData.has_subtotals_by_default;
        // Use templateData.columns (pivot table) instead of selected_columns.
        // GET /api/templates(/{id}) doesn't select master_columns.notes, so
        // look it up from the just-loaded `columns` (GET /api/columns, which
        // does) by id -- keeps this phase frontend-only, no backend change.
        const cols = templateData.columns || [];
        cols.forEach(col => {
            const masterCol = columns.find(c => c.id === col.id);
            selectedColumns.push({ id: col.id, name: col.column_name, type: col.data_type,
                storage_key: col.storage_key, definition_version: col.definition_version,
                reconciliation_role: col.reconciliation_role || null,
                compare_for_changes: col.compare_for_changes, comparison_review_required: col.comparison_review_required,
                notes: masterCol ? masterCol.notes : null });
        });
        renderSelectedColumns();
        updateSelectedCount();
    } else {
        editingId = null;
        templateName.value = '';
        editId.value = '';
        rateSourceSelect.value = 'auto';
        hasSubtotalsCheckbox.checked = false;
        renderSelectedColumns();
        updateSelectedCount();
    }
    renderLedgerRoleControls();
    updateRateSourceVisibility();
}

function closeModal() {
    window.TemplateAccountingFields.state.generation += 1;
    modalOverlay.style.display = 'none';
    modal.style.display = 'none';
    document.body.style.overflow = '';
    editingId = null;
    modalStatus.style.display = 'none';
    searchInput.value = '';
    searchResults.style.display = 'none';
    noSearchResults.style.display = 'none';
    updateAddColumnUI('');
    closeQuickAddColumnModal();
}

// ---------- SEARCH INPUT ----------
searchInput.addEventListener('input', function() {
    const val = this.value;
    if (val.trim()) {
        clearSearchBtn.style.display = 'block';
        renderSearchResults(val);
    } else {
        clearSearchBtn.style.display = 'none';
        searchResults.style.display = 'none';
        noSearchResults.style.display = 'none';
    }
    updateAddColumnUI(val);
});

clearSearchBtn.addEventListener('click', function() {
    searchInput.value = '';
    searchInput.focus();
    this.style.display = 'none';
    searchResults.style.display = 'none';
    noSearchResults.style.display = 'none';
    updateAddColumnUI('');
});

// Opens the quick-add modal pre-filled with whatever the user typed in the search box.
addColumnTriggerBtn.addEventListener('click', function() {
    openQuickAddColumnModal(searchInput.value.trim());
});

// ---------- MODAL CONTROLS ----------
document.getElementById('addTemplateBtn').addEventListener('click', function () {
    openModal('Create New Template', null, this);
});

function openEditModal(template, triggerBtn = null) {
    openModal('Edit Template', template, triggerBtn);
}

cancelBtn.addEventListener('click', closeModal);
closeModalBtn.addEventListener('click', closeModal);

// ---------- FORM SUBMIT ----------
document.getElementById('templateForm').addEventListener('submit', async function(e) {
    e.preventDefault();
    if (!canEditTenantTemplates) return;

    const name = templateName.value.trim();
    if (!name) {
        showModalStatus('⚠️ Template name is required.', 'error');
        return;
    }
    if (selectedColumns.length === 0) {
        showModalStatus('⚠️ Please select at least one column.', 'error');
        return;
    }

    const ledgerMode = ledgerPurposeSelect.value === 'account_ledger';
    if (ledgerMode && (ledgerRoleDefinitions.some(([role]) => !ledgerColumnRoles[role])
        || new Set(Object.values(ledgerColumnRoles)).size !== 6 || selectedColumns[0]?.id !== ledgerColumnRoles.voucher)) {
        showModalStatus('Select six distinct ledger column roles and keep the Voucher column first.', 'error'); return;
    }
    if (nativeTenantTemplates && !ledgerSetupDraft && window.TemplateAccountingFields.error()) {
        showModalStatus(window.TemplateAccountingFields.error(), 'error'); return;
    }
    const selectedIds = selectedColumns.map(c => c.id);
    const saveTemplateBtn = document.getElementById('saveTemplateBtn');
    if (templateActionsInFlight.has('template-save')) return;
    templateActionsInFlight.add('template-save');
    const loadingToken = startTemplateLoading(saveTemplateBtn);
    const previousDisabled = saveTemplateBtn ? saveTemplateBtn.disabled : false;
    if (saveTemplateBtn) saveTemplateBtn.disabled = true;

    try {
        window.LoadingUI.showSpinner(saveTemplateBtn, 'Saving');

        let url = API_BASE + '/api/templates';
        let method = 'POST';
        let payload = { template_name: name, selected_columns: selectedIds, rate_source: ledgerMode ? null : rateSourceSelect.value,
            has_subtotals_by_default: hasSubtotalsCheckbox.checked, data_purpose: ledgerPurposeSelect.value,
            ledger_columns: ledgerMode ? ledgerColumnRoles : {},
            column_roles: Object.fromEntries(selectedColumns.map(c => [c.id, c.reconciliation_role || null])),
            column_comparisons: Object.fromEntries(selectedColumns.map(c => [c.id, templateColumnComparison(c)])) };
        if (ledgerSetupDraft) {
            url += '/ledger-setup';
            payload = {template_name: name, has_subtotals_by_default: hasSubtotalsCheckbox.checked,
                ledger_columns: Object.fromEntries(Object.entries(ledgerColumnRoles).map(([role, id]) => [role, id > 0 ? id : null])),
                column_comparisons: Object.fromEntries(ledgerRoleDefinitions.map(([role]) => [role,
                    templateColumnComparison(selectedColumns.find(c => c.id === ledgerColumnRoles[role]))]))};
        }

        if (editingId) {
            url += '/' + editingId;
            method = 'PUT';
        }

        if (nativeTenantTemplates && !ledgerSetupDraft) {
            payload.column_versions = Object.fromEntries(selectedColumns.map(c => [c.id, c.definition_version]));
            payload.upload_bindings = {...window.TemplateAccountingFields.state.current};
        }
        const headers = { 'Content-Type': 'application/json' };
        if (nativeTenantTemplates && editingId) headers['X-Template-Revision'] = templateConfigurationKey;

        const res = await fetch(url, {
            method: method,
            headers,
            body: JSON.stringify(payload)
        });

        const result = await res.json();

        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to save template');
        }

        if (ledgerSetupDraft) masterColumnsLoaded = false; // Newly created columns must be searchable in the next editor.
        showModalStatus('✅ ' + result.message, 'success');
        await new Promise(resolve => setTimeout(resolve, 800));
        closeModal();
        await loadTemplates(true);
        showStatus('✅ Template saved successfully!', 'success');

    } catch (error) {
        showModalStatus('❌ Error: ' + error.message, 'error');
    } finally {
        window.LoadingUI.hideSpinner(saveTemplateBtn);
        if (saveTemplateBtn) saveTemplateBtn.disabled = previousDisabled;
        stopTemplateLoading(loadingToken);
        templateActionsInFlight.delete('template-save');
    }
});

// ---------- CONFIRM DELETE ----------
async function confirmDelete(id, name, triggerBtn = null) {
    if (!canEditTenantTemplates) return;
    if (!confirm(`Are you sure you want to delete template "${name}"? This will soft-delete it.`)) {
        return;
    }
    const actionKey = 'template-delete:' + id;
    if (templateActionsInFlight.has(actionKey)) return;
    templateActionsInFlight.add(actionKey);
    const loadingToken = startTemplateLoading(triggerBtn);
    const previousDisabled = triggerBtn ? triggerBtn.disabled : false;
    if (triggerBtn) triggerBtn.disabled = true;
    try {
        const headers = {};
        if (nativeTenantTemplates) headers['X-Template-Revision'] = templatesCache?.find(t => t.id === id)?.configuration_key || '';
        const res = await fetch(API_BASE + '/api/templates/' + id, { method: 'DELETE', headers });
        const result = await res.json();
        if (!res.ok) throw new Error(result.message || result.detail || 'Failed to delete template');
        showStatus('✅ ' + result.message, 'success');
        await loadTemplates(true);
    } catch (error) {
        showStatus('❌ Error deleting template: ' + error.message, 'error');
    } finally {
        if (triggerBtn) triggerBtn.disabled = previousDisabled;
        stopTemplateLoading(loadingToken);
        templateActionsInFlight.delete(actionKey);
    }
}

// ==============================================
// PREVIEW MODAL (Eye Icon) - PIVOT TABLE VERSION
// ==============================================

async function openPreviewModal(tpl, triggerBtn = null) {
    if (nativeTenantTemplates) {
        const response = await fetch(API_BASE + '/api/templates/' + tpl.id);
        if (!response.ok) { showStatus('Template is no longer available. Reload the list.', 'error'); return; }
        tpl = await response.json();
    }
    // Use tpl.columns (from pivot table) instead of selected_columns.
    // GET /api/templates doesn't select master_columns.notes, so refresh
    // allMasterColumns (GET /api/columns, which does) and look notes up by
    // id -- keeps this phase frontend-only, no backend change.
    await loadMasterColumns(triggerBtn);
    const cols = tpl.columns || [];
    let columnData = [];
    if (cols.length > 0) {
        columnData = cols.map(col => ({
            id: col.id,
            name: col.column_name,
            type: col.data_type,
            notes: getColumnNotesById(col.id)
        }));
    }

    if (columnData.length === 0) {
        alert('No columns found in this template.');
        return;
    }

    // Build modal
    const overlay = document.createElement('div');
    overlay.id = 'previewOverlay';
    overlay.style.cssText = 'display:block; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:999;';

    const modal = document.createElement('div');
    modal.id = 'previewModal';
    modal.style.cssText = 'display:block; position:fixed; top:50%; left:50%; transform:translate(-50%, -50%); background:white; padding:30px; z-index:1000; min-width:650px; max-width:800px; max-height:80vh; overflow-y:auto; border-radius:12px; box-shadow:0 20px 60px rgba(0,0,0,0.3);';

    let tableRows = columnData.map((col, idx) => `
        <tr style="border-bottom:1px solid #e2e8f0;">
            <td style="padding:10px 16px; text-align:center; color:#94a3b8;">${idx + 1}</td>
            <td style="padding:10px 16px; text-align:center; color:#64748b;">${col.id}</td>
            <td style="padding:10px 16px; font-weight:500;">${escapeHtml(col.name)}${col.notes ? ` <span title="${escapeHtml(col.notes)}" style="margin-left:4px; font-size:12px; cursor:help; color:#94a3b8;">ℹ️</span>` : ''}</td>
            <td style="padding:10px 16px; text-align:center;"><span style="display:inline-block; padding:2px 12px; border-radius:12px; font-size:12px; font-weight:600; background:#f1f5f9; color:#475569;">${col.type}</span></td>
        </tr>
    `).join('');

    modal.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; border-bottom:2px solid #f0f2f5; padding-bottom:15px;">
            <div>
                <h2 style="margin:0; font-size:22px; color:#1a1a2e;">📋 ${escapeHtml(tpl.template_name)}</h2>
                <p style="margin:4px 0 0 0; color:#64748b; font-size:14px;">Created by: ${escapeHtml(tpl.created_by || 'Admin')} | Date: ${window.formatDate(tpl.created_date)} | <strong>${columnData.length}</strong> columns</p>
            </div>
            <button id="closePreviewBtn" style="background:none; border:none; font-size:28px; cursor:pointer; color:#94a3b8;">×</button>
        </div>
        <div style="overflow-x:auto; border:1px solid #e2e8f0; border-radius:8px;">
            <table style="width:100%; border-collapse:collapse; font-size:14px;">
                <thead style="background:#f1f5f9; border-bottom:2px solid #e2e8f0;">
                    <tr>
                        <th style="padding:12px 16px; text-align:center; font-weight:600; color:#475569;">#</th>
                        <th style="padding:12px 16px; text-align:center; font-weight:600; color:#475569;">Column ID</th>
                        <th style="padding:12px 16px; text-align:left; font-weight:600; color:#475569;">Column Name</th>
                        <th style="padding:12px 16px; text-align:center; font-weight:600; color:#475569;">Data Type</th>
                    </tr>
                </thead>
                <tbody>${tableRows}</tbody>
            </table>
        </div>
        <div style="margin-top:15px; text-align:right; color:#94a3b8; font-size:13px;">Total: ${columnData.length} column${columnData.length > 1 ? 's' : ''}</div>
    `;

    document.body.appendChild(overlay);
    document.body.appendChild(modal);

    function closePreview() {
        if (document.body.contains(overlay)) document.body.removeChild(overlay);
        if (document.body.contains(modal)) document.body.removeChild(modal);
    }
    document.getElementById('closePreviewBtn').addEventListener('click', closePreview);
    overlay.addEventListener('click', closePreview);
}

// ---------- ESC KEY ----------
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        if (quickAddModal && quickAddModal.style.display === 'block') {
            closeQuickAddColumnModal();
            return;
        }
        const previewModal = document.getElementById('previewModal');
        if (previewModal) {
            const closeBtn = document.getElementById('closePreviewBtn');
            if (closeBtn) closeBtn.click();
            return;
        }
    }
});

// ---------- INIT ----------
window.onload = async function() {
    if (window.clientContextReady) await window.clientContextReady;
    document.getElementById('templatesReadOnlyNotice').hidden = canEditTenantTemplates;
    document.getElementById('addTemplateBtn').disabled = !canEditTenantTemplates;
    document.getElementById('prepareLedgerBtn').disabled = !canEditTenantTemplates;
    await loadTemplates();
    if (nativeTenantTemplates && new URLSearchParams(location.search).get('feature') === 'accounting-fields') {
        document.getElementById('addTemplateBtn').focus();
        showStatus('Open a template to review its Accounting fields.', 'success');
    }
};
if (nativeTenantTemplates) window.addEventListener('focus', () => {
    // Never replace an unsaved form. Its revision check rejects stale writes.
    if (modal.style.display !== 'block') loadTemplates(true);
});
