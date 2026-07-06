// ==============================================
// 📄 MANAGE TEMPLATES LOGIC (using utilities)
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

        // Created Date (using global formatDate)
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
        // Use global renderColumnCheckboxes
        window.renderColumnCheckboxes(columnCheckboxes, columns, templateData.selected_columns || []);
    } else {
        // Add mode
        editingId = null;
        templateName.value = '';
        editId.value = '';
        window.renderColumnCheckboxes(columnCheckboxes, columns, []);
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
        loadTemplates();
    } catch (error) {
        showStatus('❌ Error deleting template: ' + error.message, 'error');
    }
}

// ---------- INITIAL LOAD ----------
window.onload = function() {
    loadTemplates();
    console.log('📄 Templates Manager loaded (using utils)');
};