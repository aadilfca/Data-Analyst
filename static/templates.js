// ==============================================
// 📄 MANAGE TEMPLATES LOGIC (with pivot table)
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

let editingId = null;
let allColumns = [];

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

        // Columns: display as "1. First Name, 2. Last Name"
        const tdCols = document.createElement('td');
        tdCols.style.padding = '10px 16px';
        const cols = tpl.columns || [];
        let colDisplay = cols.map((col, idx) => `${idx+1}. ${col.column_name}`).join(', ');
        tdCols.textContent = colDisplay || '—';
        tdCols.title = colDisplay || 'No columns';

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

        // Preview button (Eye)
        const previewBtn = document.createElement('button');
        previewBtn.textContent = '👁️';
        previewBtn.style.background = 'none';
        previewBtn.style.border = 'none';
        previewBtn.style.cursor = 'pointer';
        previewBtn.style.fontSize = '18px';
        previewBtn.title = 'Preview and edit columns';
        previewBtn.addEventListener('click', () => openPreviewModal(tpl));

        // Edit button
        const editBtn = document.createElement('button');
        editBtn.textContent = '✏️';
        editBtn.style.background = 'none';
        editBtn.style.border = 'none';
        editBtn.style.cursor = 'pointer';
        editBtn.style.fontSize = '18px';
        editBtn.style.marginLeft = '8px';
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

        tdActions.appendChild(previewBtn);
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
        allColumns = columns;
        return columns;
    } catch (error) {
        console.log('⚠️ Error loading columns:', error.message);
        return [];
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

// ---------- MODAL OPEN / CLOSE ----------
async function openModal(title, templateData = null) {
    modalTitle.textContent = title;
    modalOverlay.style.display = 'block';
    modal.style.display = 'block';
    modalStatus.style.display = 'none';
    document.body.style.overflow = 'hidden';

    const columns = await loadColumnsForCheckboxes();

    if (templateData) {
        editingId = templateData.id;
        templateName.value = templateData.template_name;
        editId.value = templateData.id;
        // Pre-select columns based on the template's columns (ordered)
        const selectedIds = (templateData.columns || []).map(c => c.id);
        window.renderColumnCheckboxes(columnCheckboxes, columns, selectedIds);
    } else {
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

document.getElementById('addTemplateBtn').addEventListener('click', () => {
    openModal('Create New Template');
});

function openEditModal(template) {
    openModal('Edit Template', template);
}

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

    // Get selected column IDs in the order they appear in the checkbox list (which is the display order)
    const checkboxItems = columnCheckboxes.querySelectorAll('.checkbox-item');
    const selectedIds = [];
    checkboxItems.forEach(item => {
        const cb = item.querySelector('input[type="checkbox"]');
        if (cb && cb.checked) {
            selectedIds.push(parseInt(cb.value));
        }
    });

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

// ==============================================
// PREVIEW MODAL (Interactive)
// ==============================================

function openPreviewModal(tpl) {
    // Fetch fresh column data for this template
    fetch(API_BASE + '/api/templates/' + tpl.id + '/columns')
        .then(res => {
            if (!res.ok) throw new Error('Failed to load template columns');
            return res.json();
        })
        .then(columns => {
            renderPreviewModal(tpl, columns);
        })
        .catch(err => {
            alert('Error loading template columns: ' + err.message);
        });
}

function renderPreviewModal(tpl, columns) {
    // Build the preview modal with interactive list
    const overlay = document.createElement('div');
    overlay.id = 'previewOverlay';
    overlay.style.cssText = 'display:block; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:999;';

    const modal = document.createElement('div');
    modal.id = 'previewModal';
    modal.style.cssText = 'display:block; position:fixed; top:50%; left:50%; transform:translate(-50%, -50%); background:white; padding:30px; z-index:1000; min-width:600px; max-width:800px; max-height:80vh; overflow-y:auto; border-radius:8px; box-shadow:0 10px 40px rgba(0,0,0,0.3);';

    // Build the list
    let listHtml = columns.map((col, index) => `
        <div class="preview-row" data-column-id="${col.column_id}" data-mapping-id="${col.mapping_id}" style="display:flex; align-items:center; gap:10px; padding:8px 0; border-bottom:1px solid #f0f2f5;">
            <span class="drag-handle" style="cursor:grab; color:#94a3b8; font-size:18px; user-select:none;">☰</span>
            <span style="font-weight:600; min-width:30px;">${index + 1}</span>
            <span style="flex:1;">${col.column_name}</span>
            <span style="color:#64748b; font-size:13px;">Master ID: ${col.column_id}</span>
            <button class="remove-column-btn" style="background:none; border:none; color:#dc2626; cursor:pointer; font-size:18px; padding:0 6px;" title="Remove column from template">✕</button>
        </div>
    `).join('');

    modal.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px;">
            <h2 style="margin:0;">👁️ Edit Template: ${tpl.template_name}</h2>
            <button id="closePreviewBtn" style="background:none; border:none; font-size:28px; cursor:pointer; color:#94a3b8;">×</button>
        </div>
        <p style="color:#64748b; margin-bottom:15px;">Drag ☰ to reorder. Click ✕ to remove a column. Changes are auto-saved.</p>
        <div id="previewList" style="max-height:300px; overflow-y:auto; border:1px solid #e2e8f0; border-radius:8px; padding:10px;">
            ${listHtml}
        </div>
        <div style="margin-top:15px; display:flex; justify-content:space-between; align-items:center;">
            <span id="previewCount" style="color:#94a3b8; font-size:14px;">${columns.length} columns</span>
            <button id="saveOrderBtn" style="padding:8px 20px; background:#3b82f6; color:white; border:none; border-radius:4px; cursor:pointer;">Save Order</button>
        </div>
        <div id="previewStatus" style="margin-top:10px; padding:8px; border-radius:4px; display:none;"></div>
    `;

    document.body.appendChild(overlay);
    document.body.appendChild(modal);

    // Close functions
    function closePreview() {
        if (document.body.contains(overlay)) document.body.removeChild(overlay);
        if (document.body.contains(modal)) document.body.removeChild(modal);
    }
    document.getElementById('closePreviewBtn').addEventListener('click', closePreview);
    overlay.addEventListener('click', closePreview);

    // Initialize SortableJS on the list
    const listContainer = document.getElementById('previewList');
    if (typeof Sortable !== 'undefined') {
        new Sortable(listContainer, {
            handle: '.drag-handle',
            animation: 150,
            ghostClass: 'sortable-ghost',
            onEnd: function() {
                // Update serial numbers
                updatePreviewSerialNumbers(listContainer);
                // Update count
                updatePreviewCount(listContainer);
            }
        });
    } else {
        console.warn('SortableJS not loaded; drag disabled.');
    }

    // Remove column button: delete mapping and remove row
    listContainer.querySelectorAll('.remove-column-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const row = this.closest('.preview-row');
            const columnId = row.dataset.columnId;
            const templateId = tpl.id;
            if (!confirm('Remove this column from the template?')) return;
            // Call API to delete mapping
            fetch(API_BASE + '/api/templates/' + templateId + '/columns/' + columnId, {
                method: 'DELETE'
            })
            .then(res => res.json())
            .then(result => {
                if (result.message) {
                    row.remove();
                    updatePreviewSerialNumbers(listContainer);
                    updatePreviewCount(listContainer);
                    showPreviewStatus('✅ Column removed', 'success');
                    // Reload templates to update the list view
                    loadTemplates();
                }
            })
            .catch(err => {
                showPreviewStatus('❌ Error: ' + err.message, 'error');
            });
        });
    });

    // Save Order button: send current order to server
    document.getElementById('saveOrderBtn').addEventListener('click', function() {
        const rows = listContainer.querySelectorAll('.preview-row');
        const columnIds = Array.from(rows).map(row => parseInt(row.dataset.columnId));
        if (columnIds.length === 0) {
            showPreviewStatus('⚠️ Cannot save empty order.', 'error');
            return;
        }
        fetch(API_BASE + '/api/templates/' + tpl.id + '/reorder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(columnIds)
        })
        .then(res => res.json())
        .then(result => {
            if (result.message) {
                showPreviewStatus('✅ Order saved successfully', 'success');
                // Reload templates to update the list view
                loadTemplates();
            }
        })
        .catch(err => {
            showPreviewStatus('❌ Error: ' + err.message, 'error');
        });
    });

    // Helper: update serial numbers
    function updatePreviewSerialNumbers(container) {
        const rows = container.querySelectorAll('.preview-row');
        rows.forEach((row, idx) => {
            const numSpan = row.querySelector('span:nth-child(2)');
            if (numSpan) numSpan.textContent = idx + 1;
        });
    }

    function updatePreviewCount(container) {
        const count = container.querySelectorAll('.preview-row').length;
        const countSpan = document.getElementById('previewCount');
        if (countSpan) countSpan.textContent = count + ' columns';
    }

    function showPreviewStatus(msg, type) {
        const statusDiv = document.getElementById('previewStatus');
        statusDiv.textContent = msg;
        statusDiv.style.display = 'block';
        statusDiv.style.background = type === 'error' ? '#ffebee' : '#e8f5e9';
        statusDiv.style.color = type === 'error' ? '#c62828' : '#2e7d32';
        statusDiv.style.border = '1px solid ' + (type === 'error' ? '#ef9a9a' : '#a5d6a7');
        setTimeout(() => { statusDiv.style.display = 'none'; }, 4000);
    }
}

// ---------- INITIAL LOAD ----------
window.onload = function() {
    loadTemplates();
    console.log('📄 Templates Manager loaded (pivot table)');
};
// ---------- ESC KEY TO CLOSE TEMPLATE & PREVIEW MODALS ----------
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        // Close template modal
        const templateModal = document.getElementById('templateModal');
        if (templateModal && templateModal.style.display === 'block') {
            closeModal();
            return;
        }
        // Close preview modal
        const previewModal = document.getElementById('previewModal');
        if (previewModal) {
            const closeBtn = document.getElementById('closePreviewBtn');
            if (closeBtn) closeBtn.click();
            return;
        }
    }
});