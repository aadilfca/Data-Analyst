// ==============================================
// 🏢 MANAGE CLIENTS LOGIC
// ==============================================

// API_BASE is defined globally in client_context.js

// DOM References
const clientsBody = document.getElementById('clientsBody');
const noClientsMsg = document.getElementById('noClientsMessage');
const totalCountSpan = document.getElementById('totalCount');
const statusDiv = document.getElementById('clientStatus');

const modalOverlay = document.getElementById('clientModalOverlay');
const modal = document.getElementById('clientModal');
const modalTitle = document.getElementById('modalTitle');
const clientCode = document.getElementById('clientCode');
const clientName = document.getElementById('clientName');
const clientIndustry = document.getElementById('clientIndustry');
const clientContact = document.getElementById('clientContact');
const clientEmail = document.getElementById('clientEmail');
const clientPhone = document.getElementById('clientPhone');
const editId = document.getElementById('editClientId');
const cancelBtn = document.getElementById('cancelClientBtn');
const modalStatus = document.getElementById('modalStatus');

// Button references
const saveBtn = document.getElementById('saveClientBtn');
const saveAndNewBtn = document.getElementById('saveAndNewBtn');
const addClientBtn = document.getElementById('addClientBtn');

let editingId = null;

// ---------- SORTING ----------
let sortColumn = 'id';
let sortAscending = true;
window.clientData = [];

function sortClients(clients, column, ascending) {
    const sorted = [...clients];
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
            const sorted = sortClients(window.clientData || [], sortColumn, sortAscending);
            renderClients(sorted);
            updateSortArrows(sortColumn, sortAscending);
        });
    });
}

// ---------- LOAD CLIENTS ----------
async function loadClients() {
    try {
        const res = await fetch(API_BASE + '/api/clients?include_inactive=true');
        if (!res.ok) throw new Error('Failed to fetch clients');
        const clients = await res.json();
        window.clientData = clients;
        const sorted = sortClients(clients, sortColumn, sortAscending);
        renderClients(sorted);
        updateSortArrows(sortColumn, sortAscending);
        totalCountSpan.textContent = clients.length;
    } catch (error) {
        showStatus('❌ Error loading clients: ' + error.message, 'error');
        clientsBody.innerHTML = '';
        noClientsMsg.style.display = 'block';
        totalCountSpan.textContent = '0';
    }
}

// ---------- RENDER CLIENTS TABLE ----------
function renderClients(clients) {
    clientsBody.innerHTML = '';
    if (!clients || clients.length === 0) {
        noClientsMsg.style.display = 'block';
        return;
    }
    noClientsMsg.style.display = 'none';

    clients.forEach(client => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #e2e8f0';

        const tdId = document.createElement('td');
        tdId.textContent = client.id;
        tdId.style.padding = '10px 16px';

        const tdCode = document.createElement('td');
        tdCode.textContent = client.client_code;
        tdCode.style.padding = '10px 16px';
        tdCode.style.fontWeight = '600';
        tdCode.style.color = '#2563eb';

        const tdName = document.createElement('td');
        tdName.textContent = client.client_name;
        tdName.style.padding = '10px 16px';
        tdName.style.fontWeight = '500';

        const tdIndustry = document.createElement('td');
        tdIndustry.textContent = client.industry || '—';
        tdIndustry.style.padding = '10px 16px';

        const tdContact = document.createElement('td');
        tdContact.textContent = client.contact_person || '—';
        tdContact.style.padding = '10px 16px';

        const tdActive = document.createElement('td');
        tdActive.style.padding = '10px 16px';
        tdActive.style.textAlign = 'center';
        const activeSpan = document.createElement('span');
        activeSpan.textContent = client.is_active ? '✅' : '❌';
        activeSpan.style.fontSize = '16px';
        tdActive.appendChild(activeSpan);

        const tdActions = document.createElement('td');
        tdActions.style.padding = '10px 16px';
        tdActions.style.textAlign = 'center';

        // Edit
        const editBtn = document.createElement('button');
        editBtn.textContent = '✏️';
        editBtn.style.background = 'none';
        editBtn.style.border = 'none';
        editBtn.style.cursor = 'pointer';
        editBtn.style.fontSize = '18px';
        editBtn.title = 'Edit client';
        editBtn.addEventListener('click', () => openEditModal(client));

        // Delete / Restore
        const delBtn = document.createElement('button');
        if (client.is_active) {
            delBtn.textContent = '🗑️';
            delBtn.style.color = '#dc2626';
            delBtn.title = 'Deactivate client';
            delBtn.addEventListener('click', () => confirmDelete(client.id, client.client_name));
        } else {
            delBtn.textContent = '↩️';
            delBtn.style.color = '#16a34a';
            delBtn.title = 'Restore client';
            delBtn.addEventListener('click', () => confirmRestore(client.id, client.client_name));
        }
        delBtn.style.background = 'none';
        delBtn.style.border = 'none';
        delBtn.style.cursor = 'pointer';
        delBtn.style.fontSize = '18px';
        delBtn.style.marginLeft = '8px';

        tdActions.appendChild(editBtn);
        tdActions.appendChild(delBtn);

        tr.appendChild(tdId);
        tr.appendChild(tdCode);
        tr.appendChild(tdName);
        tr.appendChild(tdIndustry);
        tr.appendChild(tdContact);
        tr.appendChild(tdActive);
        tr.appendChild(tdActions);
        clientsBody.appendChild(tr);
    });
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
    setTimeout(() => { modalStatus.style.display = 'none'; }, 4000);
}

// ---------- MODAL CONTROL ----------
function openModal(title, clientData = null) {
    modalTitle.textContent = title;
    modalOverlay.style.display = 'block';
    modal.style.display = 'block';
    modalStatus.style.display = 'none';
    document.body.style.overflow = 'hidden';

    if (clientData) {
        editingId = clientData.id;
        clientCode.value = clientData.client_code;
        clientName.value = clientData.client_name;
        clientIndustry.value = clientData.industry || '';
        clientContact.value = clientData.contact_person || '';
        clientEmail.value = clientData.email || '';
        clientPhone.value = clientData.phone || '';
        editId.value = clientData.id;
    } else {
        editingId = null;
        clientCode.value = '';
        clientName.value = '';
        clientIndustry.value = '';
        clientContact.value = '';
        clientEmail.value = '';
        clientPhone.value = '';
        editId.value = '';
        // Focus on first field
        setTimeout(() => clientCode.focus(), 100);
    }
}

function closeModal() {
    modalOverlay.style.display = 'none';
    modal.style.display = 'none';
    document.body.style.overflow = '';
    editingId = null;
    modalStatus.style.display = 'none';
}

addClientBtn.addEventListener('click', () => openModal('Add New Client'));
cancelBtn.addEventListener('click', closeModal);
modalOverlay.addEventListener('click', closeModal);

function openEditModal(client) { openModal('Edit Client', client); }

// ---------- CONFIRM DELETE (Soft-delete) ----------
async function confirmDelete(id, name) {
    if (!confirm(`Are you sure you want to deactivate client "${name}"? It will become inactive but data will be preserved.`)) {
        return;
    }
    try {
        const res = await fetch(API_BASE + '/api/clients/' + id, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to delete client');
        }
        showStatus('✅ ' + result.message, 'success');
        loadClients();
    } catch (error) {
        showStatus('❌ Error deactivating client: ' + error.message, 'error');
    }
}

// ---------- CONFIRM RESTORE ----------
async function confirmRestore(id, name) {
    if (!confirm(`Restore client "${name}"?`)) {
        return;
    }
    try {
        const res = await fetch(API_BASE + '/api/clients/' + id + '/restore', { method: 'POST' });
        const result = await res.json();
        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to restore client');
        }
        showStatus('✅ ' + result.message, 'success');
        loadClients();
    } catch (error) {
        showStatus('❌ Error restoring client: ' + error.message, 'error');
    }
}

// ---------- FILTER CLIENTS ----------
function filterClients(searchTerm) {
    if (!window.clientData) return;
    let filtered = window.clientData;
    if (searchTerm) {
        filtered = window.clientData.filter(client => {
            const code = (client.client_code || '').toLowerCase();
            const name = (client.client_name || '').toLowerCase();
            const industry = (client.industry || '').toLowerCase();
            const contact = (client.contact_person || '').toLowerCase();
            return code.includes(searchTerm) || name.includes(searchTerm) ||
                   industry.includes(searchTerm) || contact.includes(searchTerm);
        });
    }
    const sorted = sortClients(filtered, sortColumn, sortAscending);
    renderClients(sorted);
    updateSortArrows(sortColumn, sortAscending);
    totalCountSpan.textContent = filtered.length;
}

// ---------- SEARCH ----------
const searchInput = document.getElementById('clientSearchInput');
if (searchInput) {
    searchInput.addEventListener('input', function() {
        filterClients(this.value.trim().toLowerCase());
    });
}

// ---------- CLEAR FORM ----------
function clearForm() {
    clientCode.value = '';
    clientName.value = '';
    clientIndustry.value = '';
    clientContact.value = '';
    clientEmail.value = '';
    clientPhone.value = '';
    editingId = null;
    editId.value = '';
    modalStatus.style.display = 'none';
}

// ---------- SAVE FUNCTION ----------
async function saveClient(keepModalOpen) {
    const code = clientCode.value.trim();
    const name = clientName.value.trim();

    if (!code) {
        showModalStatus('⚠️ Client Code is required.', 'error');
        clientCode.focus();
        return false;
    }
    if (!name) {
        showModalStatus('⚠️ Client Name is required.', 'error');
        clientName.focus();
        return false;
    }

    const payload = {
        client_code: code,
        client_name: name,
        industry: clientIndustry.value.trim() || null,
        contact_person: clientContact.value.trim() || null,
        email: clientEmail.value.trim() || null,
        phone: clientPhone.value.trim() || null
    };

    try {
        let url = API_BASE + '/api/clients';
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

        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to save client');
        }

        showModalStatus('✅ ' + result.message, 'success');

        await loadClients();

        if (keepModalOpen) {
            clearForm();
            showModalStatus('✅ Saved! Ready for next entry.', 'success');
            modal.style.display = 'block';
            modalOverlay.style.display = 'block';
            setTimeout(() => clientCode.focus(), 100);
        } else {
            setTimeout(() => {
                closeModal();
                showStatus('✅ Client saved successfully!', 'success');
            }, 800);
        }
        return true;

    } catch (error) {
        showModalStatus('❌ Error: ' + error.message, 'error');
        return false;
    }
}

// Prevent accidental form submission
document.getElementById('clientForm').addEventListener('submit', function(e) {
    e.preventDefault();
});

// ---------- BUTTON HANDLERS ----------
saveBtn.addEventListener('click', function(e) {
    e.preventDefault();
    saveClient(false);
});

saveAndNewBtn.addEventListener('click', function(e) {
    e.preventDefault();
    saveClient(true);
});

// ---------- ESC KEY ----------
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        if (modal.style.display === 'block') {
            closeModal();
        }
    }
});

// ---------- INIT ----------
window.onload = function() {
    loadClients();
    setupSorting();
    console.log('🏢 Clients Manager loaded');
};