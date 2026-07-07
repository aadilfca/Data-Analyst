// ==============================================
// 📄 MANAGE TEMPLATES - PIVOT TABLE VERSION
// ==============================================

// API_BASE is defined globally in client_context.js

// DOM refs
const templatesBody = document.getElementById('templatesBody');
const noTemplatesMsg = document.getElementById('noTemplatesMessage');
const totalCountSpan = document.getElementById('totalCount');
const statusDiv = document.getElementById('templateStatus');

const modalOverlay = document.getElementById('templateModalOverlay');
const modal = document.getElementById('templateModal');
const modalTitle = document.getElementById('modalTitle');
const templateName = document.getElementById('templateName');
const searchInput = document.getElementById('columnSearchInput');
const clearSearchBtn = document.getElementById('clearSearchBtn');
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

let editingId = null;
let allMasterColumns = [];
window.allMasterColumns = [];
let selectedColumns = [];

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
        previewBtn.addEventListener('click', () => openPreviewModal(tpl));

        // Edit
        const editBtn = document.createElement('button');
        editBtn.textContent = '✏️';
        editBtn.style.background = 'none';
        editBtn.style.border = 'none';
        editBtn.style.cursor = 'pointer';
        editBtn.style.fontSize = '18px';
        editBtn.style.marginLeft = '8px';
        editBtn.title = 'Edit template';
        editBtn.addEventListener('click', () => openEditModal(tpl));

        // Delete
        const delBtn = document.createElement('button');
        delBtn.textContent = '🗑️';
        delBtn.style.background = 'none';
        delBtn.style.border = 'none';
        delBtn.style.cursor = 'pointer';
        delBtn.style.fontSize = '18px';
        delBtn.style.marginLeft = '8px';
        delBtn.title = 'Delete template';
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

// ---------- LOAD MASTER COLUMNS ----------
async function loadMasterColumns() {
    try {
        const res = await fetch(API_BASE + '/api/columns');
        if (!res.ok) throw new Error('Failed to fetch columns');
        const columns = await res.json();
        allMasterColumns = columns;
        window.allMasterColumns = columns;
        return columns;
    } catch (error) {
        console.log('⚠️ Error loading columns:', error.message);
        return [];
    }
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

        const nameSpan = document.createElement('span');
        nameSpan.textContent = col.column_name;
        nameSpan.style.fontSize = '14px';
        nameSpan.style.fontWeight = '500';

        const actionBtn = document.createElement('button');
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

        div.appendChild(nameSpan);
        div.appendChild(actionBtn);
        searchResults.appendChild(div);
    });
}

// ---------- ADD / REMOVE SELECTED ----------
function addToSelected(col) {
    if (selectedColumns.some(c => c.id === col.id)) return;
    selectedColumns.push({ id: col.id, name: col.column_name, type: col.data_type });
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

function renderSelectedColumns() {
    selectedColumnsList.innerHTML = '';
    if (selectedColumns.length === 0) {
        emptySelectedMsg.style.display = 'block';
        return;
    }
    emptySelectedMsg.style.display = 'none';

    selectedColumns.forEach((col, index) => {
        const div = document.createElement('div');
        div.className = 'selected-item';
        div.dataset.id = col.id;
        div.style.cssText = 'display:flex; align-items:center; gap:10px; padding:8px 10px; border-bottom:1px solid #f0f2f5; background:white; border-radius:6px; margin-bottom:4px;';

        const handle = document.createElement('span');
        handle.textContent = '☰';
        handle.style.cssText = 'cursor:grab; color:#94a3b8; font-size:16px; user-select:none; padding:0 4px;';
        div.appendChild(handle);

        const num = document.createElement('span');
        num.textContent = index + 1;
        num.style.cssText = 'font-weight:600; color:#64748b; min-width:24px; font-size:13px;';
        div.appendChild(num);

        const nameSpan = document.createElement('span');
        nameSpan.textContent = col.name;
        nameSpan.style.cssText = 'flex:1; font-size:14px; font-weight:500; color:#1a1a2e;';
        div.appendChild(nameSpan);

        const removeBtn = document.createElement('button');
        removeBtn.textContent = '✕';
        removeBtn.style.cssText = 'background:none; border:none; color:#dc2626; cursor:pointer; font-size:16px; padding:0 6px;';
        removeBtn.addEventListener('click', function() { removeFromSelected(col.id); });
        div.appendChild(removeBtn);

        selectedColumnsList.appendChild(div);
    });

    if (typeof Sortable !== 'undefined' && selectedColumns.length > 1) {
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
    const count = selectedColumns.length;
    selectedCountBadge.textContent = count + ' selected';
    selectedCountBadge.style.background = count > 0 ? '#e8f5e9' : '#f1f3f4';
    selectedCountBadge.style.color = count > 0 ? '#2e7d32' : '#94a3b8';
}

// ---------- MODAL OPEN / CLOSE ----------
async function openModal(title, templateData = null) {
    selectedColumns = [];
    searchInput.value = '';
    searchResults.style.display = 'none';
    noSearchResults.style.display = 'none';
    modalStatus.style.display = 'none';

    modalTitle.textContent = title;
    modalOverlay.style.display = 'block';
    modal.style.display = 'block';
    document.body.style.overflow = 'hidden';

    const columns = await loadMasterColumns();
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
        // Use templateData.columns (pivot table) instead of selected_columns
        const cols = templateData.columns || [];
        cols.forEach(col => {
            selectedColumns.push({ id: col.id, name: col.column_name, type: col.data_type });
        });
        renderSelectedColumns();
        updateSelectedCount();
    } else {
        editingId = null;
        templateName.value = '';
        editId.value = '';
        renderSelectedColumns();
        updateSelectedCount();
    }
}

function closeModal() {
    modalOverlay.style.display = 'none';
    modal.style.display = 'none';
    document.body.style.overflow = '';
    editingId = null;
    modalStatus.style.display = 'none';
    searchInput.value = '';
    searchResults.style.display = 'none';
    noSearchResults.style.display = 'none';
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
});

clearSearchBtn.addEventListener('click', function() {
    searchInput.value = '';
    searchInput.focus();
    this.style.display = 'none';
    searchResults.style.display = 'none';
    noSearchResults.style.display = 'none';
});

// ---------- MODAL CONTROLS ----------
document.getElementById('addTemplateBtn').addEventListener('click', () => openModal('Create New Template'));

function openEditModal(template) {
    openModal('Edit Template', template);
}

cancelBtn.addEventListener('click', closeModal);
closeModalBtn.addEventListener('click', closeModal);
modalOverlay.addEventListener('click', closeModal);

// ---------- FORM SUBMIT ----------
document.getElementById('templateForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    const name = templateName.value.trim();
    if (!name) {
        showModalStatus('⚠️ Template name is required.', 'error');
        return;
    }
    if (selectedColumns.length === 0) {
        showModalStatus('⚠️ Please select at least one column.', 'error');
        return;
    }

    const selectedIds = selectedColumns.map(c => c.id);

    try {
        let url = API_BASE + '/api/templates';
        let method = 'POST';
        const payload = { template_name: name, selected_columns: selectedIds };

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
        const res = await fetch(API_BASE + '/api/templates/' + id, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.message || result.detail || 'Failed to delete template');
        showStatus('✅ ' + result.message, 'success');
        loadTemplates();
    } catch (error) {
        showStatus('❌ Error deleting template: ' + error.message, 'error');
    }
}

// ==============================================
// PREVIEW MODAL (Eye Icon) - PIVOT TABLE VERSION
// ==============================================

function openPreviewModal(tpl) {
    // Use tpl.columns (from pivot table) instead of selected_columns
    const cols = tpl.columns || [];
    let columnData = [];
    if (cols.length > 0) {
        columnData = cols.map(col => ({
            id: col.id,
            name: col.column_name,
            type: col.data_type
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
            <td style="padding:10px 16px; font-weight:500;">${col.name}</td>
            <td style="padding:10px 16px; text-align:center;"><span style="display:inline-block; padding:2px 12px; border-radius:12px; font-size:12px; font-weight:600; background:#f1f5f9; color:#475569;">${col.type}</span></td>
        </tr>
    `).join('');

    modal.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; border-bottom:2px solid #f0f2f5; padding-bottom:15px;">
            <div>
                <h2 style="margin:0; font-size:22px; color:#1a1a2e;">📋 ${tpl.template_name}</h2>
                <p style="margin:4px 0 0 0; color:#64748b; font-size:14px;">Created by: ${tpl.created_by || 'Admin'} | Date: ${window.formatDate(tpl.created_date)} | <strong>${columnData.length}</strong> columns</p>
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
        const templateModal = document.getElementById('templateModal');
        if (templateModal && templateModal.style.display === 'block') {
            closeModal();
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
window.onload = function() {
    loadTemplates();
    console.log('📄 Templates Manager loaded (pivot table version)');
};