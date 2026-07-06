// ==============================================
// 📄 MANAGE TEMPLATES LOGIC
// ==============================================

const API_BASE = 'http://localhost:5000';

// DOM References
const templatesBody = document.getElementById('templatesBody');
const noTemplatesMsg = document.getElementById('noTemplatesMessage');
const totalCountSpan = document.getElementById('totalCount');
const statusDiv = document.getElementById('templateStatus');

// Modal elements
const modalOverlay = document.getElementById('templateModalOverlay');
const modal = document.getElementById('templateModal');
const modalTitle = document.getElementById('modalTitle');
const templateName = document.getElementById('templateName');
const columnCheckboxes = document.getElementById('columnCheckboxes');
const noColumnsModalMsg = document.getElementById('noColumnsMessageModal');
const editId = document.getElementById('editTemplateId');
const cancelBtn = document.getElementById('cancelTemplateBtn');
const saveBtn = document.getElementById('saveTemplateBtn');
const modalStatus = document.getElementById('modalStatus');

let editingId = null; // null = add mode, number = edit mode
let allColumns = []; // store fetched columns for reuse

// ---------- LOAD TEMPLATES ----------
async function loadTemplates() {
    try {
        const res = await fetch(API_BASE + '/api/templates');
        if (!res.ok) throw new Error('Failed to fetch templates');
        const templates = await res.json();
        renderTemplates(templates);
        totalCountSpan.textContent = templates.length;
    } catch (error) {
        showStatus('❌ Error loading templates: ' + error.message, 'error');
        templatesBody.innerHTML = '';
        noTemplatesMsg.style.display = 'block';
        totalCountSpan.textContent = '0';
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

        // Columns (comma-separated names)
        const tdCols = document.createElement('td');
        tdCols.style.padding = '10px 16px';
        // tpl.selected_columns is an array of column IDs - we need to fetch column names?
        // The API returns selected_columns as array of IDs. We need to map to names.
        // But we can also get column names from the backend? The current API returns only IDs.
        // We'll fetch columns and map, or we can show IDs for now.
        // Better: we have the list of all columns; we can map IDs to names.
        // Since we have allColumns global, we can use it if loaded. But we might not have it.
        // Instead, we can call a separate endpoint to get column names for these IDs:
        // /api/templates/{id}/columns returns full column objects.
        // To avoid extra requests, we can map using the global allColumns if available.
        // Since we load columns in the modal, we might not have them here.
        // Let's use a placeholder and improve later.
        // For now, we'll show the IDs as a fallback.
        const colIds = tpl.selected_columns || [];
        let colNames = [];
        if (allColumns && allColumns.length > 0) {
            colNames = colIds.map(id => {
                const col = allColumns.find(c => c.id === id);
                return col ? col.column_name : 'ID:' + id;
            });
        } else {
            colNames = colIds.map(id => '#' + id);
        }
        tdCols.textContent = colNames.join(', ') || '—';
        tdCols.title = colNames.join(', ') || 'No columns';

        // Created By
        const tdBy = document.createElement('td');
        tdBy.textContent = tpl.created_by || 'Admin';
        tdBy.style.padding = '10px 16px';

        // Created Date (format DD/MM/YYYY)
        const tdDate = document.createElement('td');
        tdDate.style.padding = '10px 16px';
        if (tpl.created_date) {
            tdDate.textContent = formatDate(tpl.created_date);
        } else {
            tdDate.textContent = '—';
        }

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

        // Edit button
        const editBtn = document.createElement('button');
        editBtn.textContent = '✏️';
        editBtn.style.background = 'none';
        editBtn.style.border = 'none';
        editBtn.style.cursor = 'pointer';
        editBtn.style.fontSize = '18px';
        editBtn.title = 'Edit template';
        editBtn.addEventListener('click', () => openEditModal(tpl));

        // Delete button
        const delBtn = document.createElement('button');
        delBtn.textContent = '🗑️';
        delBtn.style.background = 'none';
        delBtn.style.border = 'none';
        delBtn.style.cursor = 'pointer';
        delBtn.style.fontSize = '18px';
        delBtn.style.marginLeft = '8px';
        delBtn.title = 'Delete template (soft)';
        delBtn.addEventListener('click', () => confirmDelete(tpl.id, tpl.template_name));

        tdActions.appendChild(editBtn);
        tdActions.appendChild(delBtn);

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

// ---------- DATE FORMATTING UTILITY ----------
function formatDate(dateString) {
    if (!dateString) return '—';
    try {
        const date = new Date(dateString);
        if (isNaN(date.getTime())) return dateString; // fallback
        // Format as DD/MM/YYYY
        const day = String(date.getDate()).padStart(2, '0');
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const year = date.getFullYear();
        return day + '/' + month + '/' + year;
    } catch (e) {
        return dateString;
    }
}

// ---------- LOAD ACTIVE COLUMNS FOR CHECKBOXES ----------
async function loadColumnsForCheckboxes() {
    try {
        const res = await fetch(API_BASE + '/api/columns');
        if (!res.ok) throw new Error('Failed to fetch columns');
        const columns = await res.json();
        allColumns = columns; // store globally
        return columns;
    } catch (error) {
        console.log('⚠️ Error loading columns:', error.message);
        return [];
    }
}

// ---------- RENDER CHECKBOXES GROUPED BY CATEGORY ----------
function renderCheckboxes(columns, selectedIds = []) {
    columnCheckboxes.innerHTML = '';
    if (!columns || columns.length === 0) {
        noColumnsModalMsg.style.display = 'block';
        return;
    }
    noColumnsModalMsg.style.display = 'none';

    // Group by category
    const groups = {};
    columns.forEach(col => {
        const cat = col.category || 'General';
        if (!groups[cat]) groups[cat] = [];
        groups[cat].push(col);
    });

    // Sort categories alphabetically
    const sortedCategories = Object.keys(groups).sort();

    sortedCategories.forEach(cat => {
        const groupDiv = document.createElement('div');
        groupDiv.style.marginBottom = '8px';
        groupDiv.style.borderBottom = '1px solid #e2e8f0';
        groupDiv.style.paddingBottom = '6px';

        const catLabel = document.createElement('div');
        catLabel.textContent = cat;
        catLabel.style.fontWeight = '600';
        catLabel.style.fontSize = '13px';
        catLabel.style.color = '#475569';
        catLabel.style.marginBottom = '4px';
        groupDiv.appendChild(catLabel);

        const checkboxContainer = document.createElement('div');
        checkboxContainer.style.display = 'flex';
        checkboxContainer.style.flexWrap = 'wrap';
        checkboxContainer.style.gap = '10px';

        groups[cat].forEach(col => {
            const label = document.createElement('label');
            label.style.display = 'flex';
            label.style.alignItems = 'center';
            label.style.gap = '5px';
            label.style.fontSize = '14px';
            label.style.cursor = 'pointer';

            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.value = col.id;
            cb.checked = selectedIds.includes(col.id);
            cb.style.width = '16px';
            cb.style.height = '16px';
            cb.style.cursor = 'pointer';

            const span = document.createElement('span');
            span.textContent = col.column_name;
            span.style.color = '#1a1a2e';

            label.appendChild(cb);
            label.appendChild(span);
            checkboxContainer.appendChild(label);
        });

        groupDiv.appendChild(checkboxContainer);
        columnCheckboxes.appendChild(groupDiv);
    });
}

// ---------- SHOW STATUS MESSAGE ----------
function showStatus(message, type) {
    statusDiv.textContent = message;
    statusDiv.className = 'upload-status ' + type;
    statusDiv.style.display = 'block';
    clearTimeout(window.statusTimeout);
    window.statusTimeout = setTimeout(() => {
        statusDiv.style.display = 'none';
    }, 5000);
}

// ---------- MODAL OPEN / CLOSE ----------
async function openModal(title, templateData = null) {
    modalTitle.textContent = title;
    modalOverlay.style.display = 'block';
    modal.style.display = 'block';
    modalStatus.style.display = 'none';
    document.body.style.overflow = 'hidden';

    // Load columns for checkboxes
    const columns = await loadColumnsForCheckboxes();

    if (templateData) {
        // Edit mode
        editingId = templateData.id;
        templateName.value = templateData.template_name;
        editId.value = templateData.id;
        // Pre-select checkboxes based on selected_columns array
        const selectedIds = templateData.selected_columns || [];
        renderCheckboxes(columns, selectedIds);
    } else {
        // Add mode
        editingId = null;
        templateName.value = '';
        editId.value = '';
        renderCheckboxes(columns, []);
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
document.getElementById('addTemplateBtn').addEventListener('click', () => {
    openModal('Create New Template');
});

// ---------- OPEN EDIT MODAL ----------
function openEditModal(template) {
    openModal('Edit Template', template);
}

// ---------- CANCEL / CLOSE ----------
cancelBtn.addEventListener('click', closeModal);
modalOverlay.addEventListener('click', closeModal);

// ---------- FORM SUBMIT (SAVE) ----------
document.getElementById('templateForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    const name = templateName.value.trim();
    if (!name) {
        showModalStatus('⚠️ Template name is required.', 'error');
        return;
    }

    // Get selected column IDs
    const checkboxes = columnCheckboxes.querySelectorAll('input[type="checkbox"]:checked');
    const selectedIds = Array.from(checkboxes).map(cb => parseInt(cb.value));
    if (selectedIds.length === 0) {
        showModalStatus('⚠️ Please select at least one column.', 'error');
        return;
    }

    try {
        let url = API_BASE + '/api/templates';
        let method = 'POST';
        const payload = {
            template_name: name,
            selected_columns: selectedIds
        };

        if (editingId) {
            url += '/' + editingId;
            method = 'PUT';
            // PUT expects same payload
        }

        const res = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const result = await res.json();

        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to save template');
        }

        showModalStatus('✅ ' + result.message, 'success');
        setTimeout(() => {
            closeModal();
            loadTemplates();
            showStatus('✅ Template saved successfully!', 'success');
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
    if (!confirm(`Are you sure you want to delete template "${name}"? This will soft-delete it.`)) {
        return;
    }

    try {
        const res = await fetch(API_BASE + '/api/templates/' + id, {
            method: 'DELETE'
        });
        const result = await res.json();
        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to delete template');
        }
        showStatus('✅ ' + result.message, 'success');
        loadTemplates(); // refresh table
    } catch (error) {
        showStatus('❌ Error deleting template: ' + error.message, 'error');
    }
}

// ---------- INITIAL LOAD ----------
window.onload = function() {
    loadTemplates();
    console.log('📄 Templates Manager loaded');
};