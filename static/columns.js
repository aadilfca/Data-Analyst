// ==============================================
// 📋 MANAGE COLUMNS LOGIC
// ==============================================

const API_BASE = 'http://localhost:5000';

// DOM References
const columnsBody = document.getElementById('columnsBody');
const noColumnsMsg = document.getElementById('noColumnsMessage');
const totalCountSpan = document.getElementById('totalCount');
const statusDiv = document.getElementById('columnStatus');

// Modal elements
const modalOverlay = document.getElementById('columnModalOverlay');
const modal = document.getElementById('columnModal');
const modalTitle = document.getElementById('modalTitle');
const colName = document.getElementById('colName');
const colType = document.getElementById('colType');
const colCategory = document.getElementById('colCategory');
const categoryDropdown = document.getElementById('categoryDropdown');
const editId = document.getElementById('editColumnId');
const cancelBtn = document.getElementById('cancelColumnBtn');
const saveBtn = document.getElementById('saveColumnBtn');
const modalStatus = document.getElementById('modalStatus');

let editingId = null; // null = add mode, number = edit mode

// ---------- LOAD COLUMNS ----------
async function loadColumns() {
    try {
        const res = await fetch(API_BASE + '/api/columns/all');
        if (!res.ok) throw new Error('Failed to fetch columns');
        const columns = await res.json();
        renderColumns(columns);
        // Update total count
        totalCountSpan.textContent = columns.length;
        // Also reload categories for dropdown
        loadCategoryDropdown();
    } catch (error) {
        showStatus('❌ Error loading columns: ' + error.message, 'error');
        columnsBody.innerHTML = '';
        noColumnsMsg.style.display = 'block';
        totalCountSpan.textContent = '0';
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

        // ID
        const tdId = document.createElement('td');
        tdId.textContent = col.id;
        tdId.style.padding = '10px 16px';

        // Name
        const tdName = document.createElement('td');
        tdName.textContent = col.column_name;
        tdName.style.padding = '10px 16px';
        tdName.style.fontWeight = '500';

        // Data Type
        const tdType = document.createElement('td');
        tdType.textContent = col.data_type;
        tdType.style.padding = '10px 16px';

        // Category
        const tdCategory = document.createElement('td');
        tdCategory.textContent = col.category || '—';
        tdCategory.style.padding = '10px 16px';

        // Active
        const tdActive = document.createElement('td');
        tdActive.style.padding = '10px 16px';
        tdActive.style.textAlign = 'center';
        const activeSpan = document.createElement('span');
        activeSpan.textContent = col.is_active ? '✅' : '❌';
        activeSpan.style.fontSize = '16px';
        tdActive.appendChild(activeSpan);

        // Actions
        const tdActions = document.createElement('td');
        tdActions.style.padding = '10px 16px';
        tdActions.style.textAlign = 'center';

        // Edit button
        const editBtn = document.createElement('button');
        editBtn.textContent = '✏️';
        editBtn.style.background = 'none';
        editBtn.style.border = 'none';
        editBtn.style.cursor = 'pointer';
        editBtn.style.fontSize = '18px';
        editBtn.title = 'Edit column';
        editBtn.addEventListener('click', () => openEditModal(col));

        // Delete button
        const delBtn = document.createElement('button');
        delBtn.textContent = '🗑️';
        delBtn.style.background = 'none';
        delBtn.style.border = 'none';
        delBtn.style.cursor = 'pointer';
        delBtn.style.fontSize = '18px';
        delBtn.style.marginLeft = '8px';
        delBtn.title = 'Delete column (soft)';
        delBtn.addEventListener('click', () => confirmDelete(col.id, col.column_name));

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
async function loadCategoryDropdown() {
    try {
        const res = await fetch(API_BASE + '/api/columns/categories');
        if (!res.ok) throw new Error('Failed to fetch categories');
        const categories = await res.json();
        // Keep the first option as placeholder
        categoryDropdown.innerHTML = '<option value="">-- Select existing --</option>';
        categories.forEach(cat => {
            const opt = document.createElement('option');
            opt.value = cat.category;
            opt.textContent = cat.category;
            categoryDropdown.appendChild(opt);
        });
        // When dropdown changes, fill the text input
        categoryDropdown.onchange = function() {
            if (this.value) {
                colCategory.value = this.value;
            }
        };
    } catch (error) {
        console.log('⚠️ Could not load categories:', error.message);
    }
}

// ---------- SHOW STATUS MESSAGE ----------
function showStatus(message, type) {
    statusDiv.textContent = message;
    statusDiv.className = 'upload-status ' + type;
    statusDiv.style.display = 'block';
    // Auto-hide after 5 seconds
    clearTimeout(window.statusTimeout);
    window.statusTimeout = setTimeout(() => {
        statusDiv.style.display = 'none';
    }, 5000);
}

// ---------- MODAL OPEN / CLOSE ----------
function openModal(title, columnData = null) {
    modalTitle.textContent = title;
    modalOverlay.style.display = 'block';
    modal.style.display = 'block';
    modalStatus.style.display = 'none';
    document.body.style.overflow = 'hidden';

    if (columnData) {
        // Edit mode
        editingId = columnData.id;
        colName.value = columnData.column_name;
        colType.value = columnData.data_type;
        colCategory.value = columnData.category || '';
        // Set dropdown to match
        categoryDropdown.value = columnData.category || '';
        editId.value = columnData.id;
    } else {
        // Add mode
        editingId = null;
        colName.value = '';
        colType.value = 'Text';
        colCategory.value = '';
        categoryDropdown.value = '';
        editId.value = '';
    }
}

function closeModal() {
    modalOverlay.style.display = 'none';
    modal.style.display = 'none';
    document.body.style.overflow = '';
    editingId = null;
    modalStatus.style.display = 'none';
}

// ---------- OPEN ADD MODAL ----------
document.getElementById('addColumnBtn').addEventListener('click', () => {
    openModal('Add New Column');
});

// ---------- OPEN EDIT MODAL ----------
function openEditModal(column) {
    openModal('Edit Column', column);
}

// ---------- CANCEL / CLOSE ----------
cancelBtn.addEventListener('click', closeModal);
modalOverlay.addEventListener('click', closeModal);

// ---------- FORM SUBMIT (SAVE) ----------
document.getElementById('columnForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    const name = colName.value.trim();
    const type = colType.value;
    const category = colCategory.value.trim();

    if (!name) {
        showModalStatus('⚠️ Column name is required.', 'error');
        return;
    }

    const payload = {
        column_name: name,
        data_type: type,
        category: category || 'General'
    };

    try {
        let url = API_BASE + '/api/columns';
        let method = 'POST';
        if (editingId) {
            url += '/' + editingId;
            method = 'PUT';
            // For PUT, we also need is_active (default 1) - we can include it
            payload.is_active = 1; // we always set active when editing
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
        // Close modal after success and reload table
        setTimeout(() => {
            closeModal();
            loadColumns();
            // Also reload categories
            loadCategoryDropdown();
            showStatus('✅ Column saved successfully!', 'success');
        }, 800);

    } catch (error) {
        showModalStatus('❌ Error: ' + error.message, 'error');
    }
});

// ---------- SHOW STATUS INSIDE MODAL ----------
function showModalStatus(message, type) {
    modalStatus.textContent = message;
    modalStatus.style.display = 'block';
    modalStatus.style.background = type === 'error' ? '#ffebee' : '#e8f5e9';
    modalStatus.style.color = type === 'error' ? '#c62828' : '#2e7d32';
    modalStatus.style.border = '1px solid ' + (type === 'error' ? '#ef9a9a' : '#a5d6a7');
}

// ---------- CONFIRM DELETE ----------
async function confirmDelete(id, name) {
    if (!confirm(`Are you sure you want to delete column "${name}"? This will soft-delete it (it will become inactive).`)) {
        return;
    }

    try {
        const res = await fetch(API_BASE + '/api/columns/' + id, {
            method: 'DELETE'
        });
        const result = await res.json();
        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to delete column');
        }
        showStatus('✅ ' + result.message, 'success');
        loadColumns(); // refresh table
        loadCategoryDropdown(); // refresh categories
    } catch (error) {
        showStatus('❌ Error deleting column: ' + error.message, 'error');
    }
}

// ---------- INITIAL LOAD ----------
// ---------- SORTING LOGIC ----------
let sortColumn = 'id';          // default sort column
let sortAscending = true;      // default ascending

function sortColumns(columns, column, ascending) {
    const sorted = [...columns];  // copy array
    sorted.sort((a, b) => {
        let valA = a[column];
        let valB = b[column];
        // Handle null/undefined values
        if (valA == null) valA = '';
        if (valB == null) valB = '';
        // For numeric sorting (if applicable)
        if (column === 'id' || column === 'is_active') {
            return ascending ? valA - valB : valB - valA;
        }
        // For strings (case-insensitive)
        if (typeof valA === 'string') valA = valA.toLowerCase();
        if (typeof valB === 'string') valB = valB.toLowerCase();
        if (ascending) {
            return valA < valB ? -1 : (valA > valB ? 1 : 0);
        } else {
            return valA > valB ? -1 : (valA < valB ? 1 : 0);
        }
    });
    return sorted;
}

// ---------- UPDATE SORT ARROWS ----------
function updateSortArrows(column, ascending) {
    document.querySelectorAll('.sortable .sort-arrow').forEach(span => {
        span.textContent = '';  // clear all arrows
    });
    // Find the header that has data-sort matching the column
    const header = document.querySelector(`.sortable[data-sort="${column}"]`);
    if (header) {
        const arrowSpan = header.querySelector('.sort-arrow');
        if (arrowSpan) {
            arrowSpan.textContent = ascending ? ' ▲' : ' ▼';
        }
    }
}

// ---------- HANDLE HEADER CLICK ----------
function setupSorting() {
    document.querySelectorAll('.sortable').forEach(header => {
        header.addEventListener('click', function() {
            const column = this.dataset.sort;
            // If same column, toggle direction; else sort ascending on new column
            if (sortColumn === column) {
                sortAscending = !sortAscending;
            } else {
                sortColumn = column;
                sortAscending = true;
            }
            // Re-sort and re-render the table
            const sortedData = sortColumns(window.columnData || [], sortColumn, sortAscending);
            renderColumns(sortedData);
            updateSortArrows(sortColumn, sortAscending);
        });
    });
}

// ---------- MODIFY loadColumns TO STORE DATA AND APPLY SORT ----------
window.columnData = [];  // global variable to store raw data

// Replace the existing loadColumns function (keep the same name)
async function loadColumns() {
    try {
        const res = await fetch(API_BASE + '/api/columns/all');
        if (!res.ok) throw new Error('Failed to fetch columns');
        const columns = await res.json();
        window.columnData = columns;  // store raw data
        // Sort by current sort column/direction and render
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
    }
}

// ---------- ON PAGE LOAD ----------
window.onload = function() {
    loadColumns();
    setupSorting();  // attach click listeners
    console.log('📋 Columns Manager loaded (with sorting)');
};