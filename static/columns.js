// ==============================================
// 📋 MANAGE COLUMNS LOGIC - FINAL VERSION
// ==============================================

// API_BASE is defined globally in client_context.js

// DOM References
const columnsBody = document.getElementById('columnsBody');
const noColumnsMsg = document.getElementById('noColumnsMessage');
const totalCountSpan = document.getElementById('totalCount');
const statusDiv = document.getElementById('columnStatus');

const modalOverlay = document.getElementById('columnModalOverlay');
const modal = document.getElementById('columnModal');
const modalTitle = document.getElementById('modalTitle');
const colName = document.getElementById('colName');
const colType = document.getElementById('colType');
const colCategory = document.getElementById('colCategory');
const colNotes = document.getElementById('colNotes');
const categoryDropdown = document.getElementById('categoryDropdown');
const editId = document.getElementById('editColumnId');
const cancelBtn = document.getElementById('cancelColumnBtn');
const modalStatus = document.getElementById('modalStatus');
const columnUsagePreview = document.getElementById('columnUsagePreview');

// Button references
const saveBtn = document.getElementById('saveColumnBtn');
const saveAndNewBtn = document.getElementById('saveAndNewBtn');
const addColumnBtn = document.getElementById('addColumnBtn');

let editingId = null;
const columnActionsInFlight = new Set();

function startColumnsLoading(target) {
    if (!window.LoadingUI || typeof window.LoadingUI.startGlobalLoading !== 'function') return null;
    return window.LoadingUI.startGlobalLoading(target);
}

function stopColumnsLoading(token) {
    if (token && window.LoadingUI && typeof window.LoadingUI.stopGlobalLoading === 'function') {
        window.LoadingUI.stopGlobalLoading(token);
    }
}

// ---------- SORTING ----------
let sortColumn = 'id';
let sortAscending = true;
window.columnData = [];

function sortColumns(columns, column, ascending) {
    const sorted = [...columns];
    sorted.sort((a, b) => {
        let valA = a[column];
        let valB = b[column];
        if (valA == null) valA = '';
        if (valB == null) valB = '';
        if (column === 'id' || column === 'is_active') {
            return ascending ? valA - valB : valB - valA;
        }
        if (typeof valA === 'string') valA = valA.toLowerCase();
        if (typeof valB === 'string') valB = valB.toLowerCase();
        return ascending ? valA < valB ? -1 : (valA > valB ? 1 : 0) : valA > valB ? -1 : (valA < valB ? 1 : 0);
    });
    return sorted;
}

function updateSortArrows(column, ascending) {
    document.querySelectorAll('.sortable .sort-arrow').forEach(span => span.textContent = '');
    const header = document.querySelector(`.sortable[data-sort="${column}"]`);
    if (header) {
        const arrowSpan = header.querySelector('.sort-arrow');
        if (arrowSpan) arrowSpan.textContent = ascending ? ' ▲' : ' ▼';
    }
}

function setupSorting() {
    document.querySelectorAll('.sortable').forEach(header => {
        header.addEventListener('click', function() {
            const column = this.dataset.sort;
            if (sortColumn === column) {
                sortAscending = !sortAscending;
            } else {
                sortColumn = column;
                sortAscending = true;
            }
            const sorted = sortColumns(window.columnData || [], sortColumn, sortAscending);
            renderColumns(sorted);
            updateSortArrows(sortColumn, sortAscending);
        });
    });
}

// ---------- LOAD COLUMNS ----------
async function loadColumns(target = null) {
    const loadingToken = startColumnsLoading(target);
    try {
        const res = await fetch(API_BASE + '/api/columns/all');
        if (!res.ok) throw new Error('Failed to fetch columns');
        const columns = await res.json();
        window.columnData = columns;
        const sorted = sortColumns(columns, sortColumn, sortAscending);
        renderColumns(sorted);
        updateSortArrows(sortColumn, sortAscending);
        totalCountSpan.textContent = columns.length;
        loadCategoryDropdown();
    } catch (error) {
        showStatus('❌ Error loading columns: ' + error.message, 'error');
        columnsBody.innerHTML = '';
        noColumnsMsg.style.display = 'block';
        totalCountSpan.textContent = '0';
    } finally {
        stopColumnsLoading(loadingToken);
    }
}

// ---------- RENDER COLUMNS TABLE ----------
function renderColumns(columns) {
    columnsBody.innerHTML = '';
    if (!columns || columns.length === 0) {
        noColumnsMsg.style.display = 'block';
        return;
    }
    noColumnsMsg.style.display = 'none';

    columns.forEach(col => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #e2e8f0';

        const tdId = document.createElement('td');
        tdId.textContent = col.id;
        tdId.style.padding = '10px 16px';

        const tdName = document.createElement('td');
        tdName.textContent = col.column_name;
        tdName.style.padding = '10px 16px';
        tdName.style.fontWeight = '500';

        const tdType = document.createElement('td');
        tdType.textContent = col.data_type;
        tdType.style.padding = '10px 16px';

        const tdCategory = document.createElement('td');
        tdCategory.textContent = col.category || '—';
        tdCategory.style.padding = '10px 16px';

        const tdActive = document.createElement('td');
        tdActive.style.padding = '10px 16px';
        tdActive.style.textAlign = 'center';
        const activeSpan = document.createElement('span');
        activeSpan.textContent = col.is_active ? '✅' : '❌';
        activeSpan.style.fontSize = '16px';
        tdActive.appendChild(activeSpan);

        const tdActions = document.createElement('td');
        tdActions.style.padding = '10px 16px';
        tdActions.style.textAlign = 'center';

        const editBtn = document.createElement('button');
        editBtn.textContent = '✏️';
        editBtn.style.background = 'none';
        editBtn.style.border = 'none';
        editBtn.style.cursor = 'pointer';
        editBtn.style.fontSize = '18px';
        editBtn.title = 'Edit column';
        editBtn.addEventListener('click', () => openEditModal(col, editBtn));

        const delBtn = document.createElement('button');
        delBtn.textContent = '🗑️';
        delBtn.style.background = 'none';
        delBtn.style.border = 'none';
        delBtn.style.cursor = 'pointer';
        delBtn.style.fontSize = '18px';
        delBtn.style.marginLeft = '8px';
        delBtn.title = 'Delete column (soft)';
        delBtn.addEventListener('click', () => confirmDelete(col.id, col.column_name, delBtn));

        tdActions.appendChild(editBtn);
        tdActions.appendChild(delBtn);

        tr.appendChild(tdId);
        tr.appendChild(tdName);
        tr.appendChild(tdType);
        tr.appendChild(tdCategory);
        tr.appendChild(tdActive);
        tr.appendChild(tdActions);
        columnsBody.appendChild(tr);
    });
}

// ---------- LOAD CATEGORY DROPDOWN ----------
async function loadCategoryDropdown(target = null) {
    const loadingToken = startColumnsLoading(target);
    try {
        const res = await fetch(API_BASE + '/api/columns/categories');
        if (!res.ok) throw new Error('Failed to fetch categories');
        const categories = await res.json();
        categoryDropdown.innerHTML = '<option value="">-- Select existing --</option>';
        categories.forEach(cat => {
            const opt = document.createElement('option');
            opt.value = cat.category;
            opt.textContent = cat.category;
            categoryDropdown.appendChild(opt);
        });
        categoryDropdown.onchange = function() {
            if (this.value) colCategory.value = this.value;
        };
    } catch (error) {
    } finally {
        stopColumnsLoading(loadingToken);
    }
}

// ---------- STATUS MESSAGES ----------
function showStatus(message, type) {
    statusDiv.textContent = message;
    statusDiv.className = 'upload-status ' + type;
    statusDiv.style.display = 'block';
    clearTimeout(window.statusTimeout);
    window.statusTimeout = setTimeout(() => {
        statusDiv.style.display = 'none';
    }, 5000);
}

function showModalStatus(message, type) {
    modalStatus.textContent = message;
    modalStatus.style.display = 'block';
    modalStatus.style.background = type === 'error' ? '#ffebee' : '#e8f5e9';
    modalStatus.style.color = type === 'error' ? '#c62828' : '#2e7d32';
    modalStatus.style.border = '1px solid ' + (type === 'error' ? '#ef9a9a' : '#a5d6a7');
}

// ---------- USAGE PREVIEW ----------
async function loadColumnUsage(columnId, target = null) {
    const loadingToken = startColumnsLoading(target);
    try {
        const res = await fetch(API_BASE + '/api/columns/' + columnId + '/usage');
        if (!res.ok) throw new Error('Failed to fetch usage');
        return await res.json();
    } catch (error) {
        return null;
    } finally {
        stopColumnsLoading(loadingToken);
    }
}

function renderColumnUsage(usage) {
    if (!usage) {
        columnUsagePreview.style.display = 'none';
        columnUsagePreview.textContent = '';
        return;
    }
    columnUsagePreview.style.display = 'block';
    columnUsagePreview.textContent =
        `Used in ${usage.templates_count} template(s) · ` +
        `${usage.documents_count} document(s) (cross-check fields) · ` +
        `${usage.cross_checks_count} cross-check(s) (field to compare)`;
}

// ---------- MODAL CONTROL ----------
function openModal(title, columnData = null, triggerBtn = null) {
    modalTitle.textContent = title;
    modalOverlay.style.display = 'block';
    modal.style.display = 'block';
    modalStatus.style.display = 'none';
    document.body.style.overflow = 'hidden';

    if (columnData) {
        editingId = columnData.id;
        colName.value = columnData.column_name;
        colType.value = columnData.data_type;
        colCategory.value = columnData.category || '';
        colNotes.value = columnData.notes || '';
        categoryDropdown.value = columnData.category || '';
        editId.value = columnData.id;

        columnUsagePreview.style.display = 'block';
        columnUsagePreview.textContent = 'Loading usage...';
        loadColumnUsage(columnData.id, triggerBtn).then(usage => {
            // Guard against a stale response landing after the modal was closed/reopened for a different column
            if (editingId === columnData.id) {
                renderColumnUsage(usage);
            }
        });
    } else {
        editingId = null;
        colName.value = '';
        colType.value = 'Text';
        colCategory.value = '';
        colNotes.value = '';
        categoryDropdown.value = '';
        editId.value = '';
        columnUsagePreview.style.display = 'none';
        columnUsagePreview.textContent = '';
    }
}

function closeModal() {
    modalOverlay.style.display = 'none';
    modal.style.display = 'none';
    document.body.style.overflow = '';
    editingId = null;
    modalStatus.style.display = 'none';
    columnUsagePreview.style.display = 'none';
    columnUsagePreview.textContent = '';
}

addColumnBtn.addEventListener('click', function() { openModal('Add New Column', null, this); });
cancelBtn.addEventListener('click', closeModal);
modalOverlay.addEventListener('click', closeModal);

function openEditModal(column, triggerBtn = null) { openModal('Edit Column', column, triggerBtn); }

// ---------- CONFIRM DELETE ----------
async function confirmDelete(id, name, triggerBtn = null) {
    if (!confirm(`Are you sure you want to delete column "${name}"? This will soft-delete it.`)) {
        return;
    }

    const actionKey = 'column-delete:' + id;
    if (columnActionsInFlight.has(actionKey)) return;
    columnActionsInFlight.add(actionKey);
    const loadingToken = startColumnsLoading(triggerBtn);
    const previousDisabled = triggerBtn ? triggerBtn.disabled : false;
    if (triggerBtn) triggerBtn.disabled = true;
    try {
        const res = await fetch(API_BASE + '/api/columns/' + id, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to delete column');
        }
        showStatus('✅ ' + result.message, 'success');
        await loadColumns();
        await loadCategoryDropdown();
    } catch (error) {
        showStatus('❌ Error deleting column: ' + error.message, 'error');
    } finally {
        if (triggerBtn) triggerBtn.disabled = previousDisabled;
        stopColumnsLoading(loadingToken);
        columnActionsInFlight.delete(actionKey);
    }
}

// ---------- FILTER COLUMNS ----------
function filterColumns(searchTerm) {
    if (!window.columnData) return;
    
    let filtered = window.columnData;
    if (searchTerm) {
        filtered = window.columnData.filter(col => {
            const name = (col.column_name || '').toLowerCase();
            const type = (col.data_type || '').toLowerCase();
            const category = (col.category || '').toLowerCase();
            return name.includes(searchTerm) || type.includes(searchTerm) || category.includes(searchTerm);
        });
    }
    
    const sorted = sortColumns(filtered, sortColumn, sortAscending);
    renderColumns(sorted);
    updateSortArrows(sortColumn, sortAscending);
    totalCountSpan.textContent = filtered.length;
}

// ---------- CLEAR FORM ----------
function clearForm() {
    colName.value = '';
    colType.value = 'Text';
    colCategory.value = '';
    colNotes.value = '';
    categoryDropdown.value = '';
    editingId = null;
    editId.value = '';
    modalStatus.style.display = 'none';
}

// ---------- SAVE FUNCTION ----------
async function saveColumn(keepModalOpen, triggerBtn) {
    const name = colName.value.trim();
    const type = colType.value;
    const category = colCategory.value.trim();

    if (!name) {
        showModalStatus('⚠️ Column name is required.', 'error');
        return false;
    }

    const payload = {
        column_name: name,
        data_type: type,
        category: category || 'General',
        notes: colNotes.value.trim() || null
    };

    if (columnActionsInFlight.has('column-save')) return false;
    columnActionsInFlight.add('column-save');
    const loadingToken = startColumnsLoading(triggerBtn);
    const previousDisabled = triggerBtn ? triggerBtn.disabled : false;
    if (triggerBtn) triggerBtn.disabled = true;

    try {
        window.LoadingUI.showSpinner(triggerBtn, 'Saving');

        let url = API_BASE + '/api/columns';
        let method = 'POST';
        if (editingId) {
            url += '/' + editingId;
            method = 'PUT';
            payload.is_active = 1;
        }

        const res = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const result = await res.json();

        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to save column');
        }

        showModalStatus('✅ ' + result.message, 'success');

        // Reload table
        await loadColumns();
        await loadCategoryDropdown();

        if (keepModalOpen) {
            // Save and New: clear form, keep modal open
            clearForm();
            showModalStatus('✅ Saved! Ready for next entry.', 'success');
            // Force modal to stay open
            modal.style.display = 'block';
            modalOverlay.style.display = 'block';
        } else {
            // Normal Save: close modal after a short delay
            await new Promise(resolve => setTimeout(resolve, 800));
            closeModal();
            showStatus('✅ Column saved successfully!', 'success');
        }

        return true;

    } catch (error) {
        showModalStatus('❌ Error: ' + error.message, 'error');
        return false;
    } finally {
        window.LoadingUI.hideSpinner(triggerBtn);
        if (triggerBtn) triggerBtn.disabled = previousDisabled;
        stopColumnsLoading(loadingToken);
        columnActionsInFlight.delete('column-save');
    }
}

// Prevent accidental form submission (Enter key inside form)
document.getElementById('columnForm').addEventListener('submit', function(e) {
    e.preventDefault();
});

// ---------- BUTTON HANDLERS ----------
// Save button – close modal after save
saveBtn.addEventListener('click', function(e) {
    e.preventDefault();
    saveColumn(false, saveBtn);
});

// Save and New button – keep modal open after save
saveAndNewBtn.addEventListener('click', function(e) {
    e.preventDefault();
    saveColumn(true, saveAndNewBtn);
});

// ---------- SEARCH ----------
const searchInput = document.getElementById('columnSearchInput');
if (searchInput) {
    searchInput.addEventListener('input', function() {
        filterColumns(this.value.trim().toLowerCase());
    });
}

// ---------- ESC KEY ----------
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        const modalEl = document.getElementById('columnModal');
        if (modalEl && modalEl.style.display === 'block') {
            closeModal();
        }
    }
});

// ---------- INIT ----------
window.onload = function() {
    loadColumns();
    setupSorting();
};
