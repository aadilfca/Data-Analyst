// ==============================================
// 👥 MANAGE USERS LOGIC
// ==============================================

// API_BASE is defined globally in client_context.js

// DOM References
const usersBody = document.getElementById('usersBody');
const noUsersMsg = document.getElementById('noUsersMessage');
const totalCountSpan = document.getElementById('totalCount');
const statusDiv = document.getElementById('userStatus');

const modalOverlay = document.getElementById('userModalOverlay');
const modal = document.getElementById('userModal');
const modalTitle = document.getElementById('userModalTitle');
const userUsername = document.getElementById('userUsername');
const userPasswordGroup = document.getElementById('userPasswordGroup');
const userPassword = document.getElementById('userPassword');
const userRole = document.getElementById('userRole');
const editId = document.getElementById('editUserId');
const cancelBtn = document.getElementById('cancelUserBtn');
const modalStatus = document.getElementById('userModalStatus');

// Button references
const saveBtn = document.getElementById('saveUserBtn');
const saveAndNewBtn = document.getElementById('saveAndNewUserBtn');
const addUserBtn = document.getElementById('addUserBtn');

// Reset Password modal references
const resetPasswordOverlay = document.getElementById('resetPasswordOverlay');
const resetPasswordModal = document.getElementById('resetPasswordModal');
const resetPasswordInput = document.getElementById('resetPasswordInput');
const resetPasswordUserId = document.getElementById('resetPasswordUserId');
const resetPasswordStatus = document.getElementById('resetPasswordStatus');
const clientAssignmentOverlay = document.getElementById('clientAssignmentOverlay');
const clientAssignmentModal = document.getElementById('clientAssignmentModal');
const clientAssignmentList = document.getElementById('clientAssignmentList');
const clientAssignmentStatus = document.getElementById('clientAssignmentStatus');
const cancelClientAssignmentBtn = document.getElementById('cancelClientAssignmentBtn');
const saveClientAssignmentBtn = document.getElementById('saveClientAssignmentBtn');
let clientAssignmentUserId = null;
const cancelResetPasswordBtn = document.getElementById('cancelResetPasswordBtn');
const submitResetPasswordBtn = document.getElementById('submitResetPasswordBtn');

let editingId = null;

// ---------- SORTING ----------
let sortColumn = 'id';
let sortAscending = true;
window.userData = [];

function sortUsers(users, column, ascending) {
    const sorted = [...users];
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
            const sorted = sortUsers(window.userData || [], sortColumn, sortAscending);
            renderUsers(sorted);
            updateSortArrows(sortColumn, sortAscending);
        });
    });
}

// ---------- LOAD USERS ----------
async function loadUsers() {
    try {
        const res = await fetch(API_BASE + '/api/users?include_inactive=true');
        if (res.status === 403) {
            showStatus('⛔ Admin access required. Redirecting...', 'error');
            usersBody.innerHTML = '';
            noUsersMsg.style.display = 'none';
            // Bounces to the client picker, not index.html. This is a firm-level page reached from
            // client_select.html, so a non-admin who lands here has no client selected - dropping
            // them on the client-scoped dashboard was a dead end into client territory.
            setTimeout(() => { window.location.href = '/static/client_select.html'; }, 2000);
            return;
        }
        if (!res.ok) throw new Error('Failed to fetch users');
        const users = await res.json();
        window.userData = users;
        const sorted = sortUsers(users, sortColumn, sortAscending);
        renderUsers(sorted);
        updateSortArrows(sortColumn, sortAscending);
        totalCountSpan.textContent = users.length;
    } catch (error) {
        showStatus('❌ Error loading users: ' + error.message, 'error');
        usersBody.innerHTML = '';
        noUsersMsg.style.display = 'block';
        totalCountSpan.textContent = '0';
    }
}

// ---------- FORMAT HELPERS ----------
function formatLastLogin(value) {
    if (!value) return '—';
    const d = new Date(value);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleString();
}

function formatRole(role) {
    return role === 'admin' ? 'Admin' : 'Standard User';
}

// ---------- RENDER USERS TABLE ----------
function renderUsers(users) {
    usersBody.innerHTML = '';
    if (!users || users.length === 0) {
        noUsersMsg.style.display = 'block';
        return;
    }
    noUsersMsg.style.display = 'none';

    users.forEach(user => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #e2e8f0';

        const tdId = document.createElement('td');
        tdId.textContent = user.id;
        tdId.style.padding = '10px 16px';

        const tdUsername = document.createElement('td');
        tdUsername.textContent = user.username;
        tdUsername.style.padding = '10px 16px';
        tdUsername.style.fontWeight = '600';
        tdUsername.style.color = '#2563eb';

        const tdRole = document.createElement('td');
        tdRole.textContent = formatRole(user.role);
        tdRole.style.padding = '10px 16px';

        const tdActive = document.createElement('td');
        tdActive.style.padding = '10px 16px';
        tdActive.style.textAlign = 'center';
        const activeSpan = document.createElement('span');
        activeSpan.textContent = user.is_active ? '✅' : '❌';
        activeSpan.style.fontSize = '16px';
        tdActive.appendChild(activeSpan);

        const tdLastLogin = document.createElement('td');
        tdLastLogin.textContent = formatLastLogin(user.last_login);
        tdLastLogin.style.padding = '10px 16px';

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
        editBtn.title = 'Edit user';
        editBtn.addEventListener('click', () => openEditModal(user));

        // Tenant admins already have access to every client in their firm.
        if (user.role !== 'admin' && user.is_active) {
            const assignBtn = document.createElement('button');
            assignBtn.textContent = '🏢';
            assignBtn.style.background = 'none';
            assignBtn.style.border = 'none';
            assignBtn.style.cursor = 'pointer';
            assignBtn.style.fontSize = '18px';
            assignBtn.style.marginLeft = '8px';
            assignBtn.title = 'Assign clients';
            assignBtn.setAttribute('aria-label', 'Assign clients to ' + user.username);
            assignBtn.addEventListener('click', () => openClientAssignments(user));
            tdActions.appendChild(assignBtn);
        }

        // Deactivate / Reactivate
        const toggleBtn = document.createElement('button');
        if (user.is_active) {
            toggleBtn.textContent = '🗑️';
            toggleBtn.style.color = '#dc2626';
            toggleBtn.title = 'Deactivate user';
            toggleBtn.addEventListener('click', () => confirmDeactivate(user.id, user.username));
        } else {
            toggleBtn.textContent = '↩️';
            toggleBtn.style.color = '#16a34a';
            toggleBtn.title = 'Reactivate user';
            toggleBtn.addEventListener('click', () => confirmReactivate(user.id, user.username));
        }
        toggleBtn.style.background = 'none';
        toggleBtn.style.border = 'none';
        toggleBtn.style.cursor = 'pointer';
        toggleBtn.style.fontSize = '18px';
        toggleBtn.style.marginLeft = '8px';

        // Reset password
        const resetBtn = document.createElement('button');
        resetBtn.textContent = '🔑';
        resetBtn.style.background = 'none';
        resetBtn.style.border = 'none';
        resetBtn.style.cursor = 'pointer';
        resetBtn.style.fontSize = '18px';
        resetBtn.style.marginLeft = '8px';
        resetBtn.title = 'Reset password';
        resetBtn.addEventListener('click', () => openResetPasswordModal(user.id, user.username));

        tdActions.appendChild(editBtn);
        tdActions.appendChild(toggleBtn);
        tdActions.appendChild(resetBtn);

        tr.appendChild(tdId);
        tr.appendChild(tdUsername);
        tr.appendChild(tdRole);
        tr.appendChild(tdActive);
        tr.appendChild(tdLastLogin);
        tr.appendChild(tdActions);
        usersBody.appendChild(tr);
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

function showResetPasswordStatus(message, type) {
    resetPasswordStatus.textContent = message;
    resetPasswordStatus.style.display = 'block';
    resetPasswordStatus.style.background = type === 'error' ? '#ffebee' : '#e8f5e9';
    resetPasswordStatus.style.color = type === 'error' ? '#c62828' : '#2e7d32';
    resetPasswordStatus.style.border = '1px solid ' + (type === 'error' ? '#ef9a9a' : '#a5d6a7');
    setTimeout(() => { resetPasswordStatus.style.display = 'none'; }, 4000);
}

// ---------- MODAL CONTROL ----------
function openModal(title, userData = null) {
    modalTitle.textContent = title;
    modalOverlay.style.display = 'block';
    modal.style.display = 'block';
    modalStatus.style.display = 'none';
    document.body.style.overflow = 'hidden';

    if (userData) {
        editingId = userData.id;
        userUsername.value = userData.username;
        userRole.value = userData.role || '';
        editId.value = userData.id;
        // Password is only settable at creation - existing users go through Reset Password.
        userPasswordGroup.style.display = 'none';
        userPassword.required = false;
        userPassword.value = '';
    } else {
        editingId = null;
        userUsername.value = '';
        userPassword.value = '';
        userRole.value = '';
        editId.value = '';
        userPasswordGroup.style.display = 'block';
        userPassword.required = true;
        // Focus on first field
        setTimeout(() => userUsername.focus(), 100);
    }
}

function closeModal() {
    modalOverlay.style.display = 'none';
    modal.style.display = 'none';
    document.body.style.overflow = '';
    editingId = null;
    modalStatus.style.display = 'none';
}

addUserBtn.addEventListener('click', () => openModal('Add New User'));
cancelBtn.addEventListener('click', closeModal);
modalOverlay.addEventListener('click', closeModal);

function openEditModal(user) { openModal('Edit User', user); }

async function openClientAssignments(user) {
    clientAssignmentUserId = user.id;
    clientAssignmentModal.querySelector('#clientAssignmentTitle').textContent = 'Assign Clients — ' + user.username;
    clientAssignmentOverlay.style.display = 'block';
    clientAssignmentModal.style.display = 'block';
    clientAssignmentList.textContent = 'Loading clients…';
    clientAssignmentStatus.style.display = 'none';
    saveClientAssignmentBtn.disabled = true;
    document.body.style.overflow = 'hidden';
    try {
        const res = await fetch(API_BASE + '/api/users/' + user.id + '/client-assignments');
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || 'Failed to load client assignments');
        clientAssignmentList.replaceChildren();
        if (result.length === 0) {
            clientAssignmentList.textContent = 'There are no active clients in this firm yet.';
        }
        result.forEach(client => {
            const row = document.createElement('label');
            row.style.cssText = 'display:grid;grid-template-columns:minmax(0,1fr) 150px;align-items:center;gap:12px;padding:12px;border:1px solid #e2e8f0;border-radius:8px;';
            const choice = document.createElement('span');
            choice.style.cssText = 'display:flex;align-items:center;gap:10px;min-width:0;';
            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.checked = client.is_assigned;
            checkbox.setAttribute('aria-label', 'Assign ' + client.client_name);
            const label = document.createElement('span');
            label.textContent = client.client_name + ' (' + client.client_code + ')';
            label.style.cssText = 'overflow-wrap:anywhere;';
            choice.append(checkbox, label);
            const role = document.createElement('select');
            role.setAttribute('aria-label', 'Team role for ' + client.client_name);
            role.style.cssText = 'width:100%;padding:7px;border:1px solid #cbd5e1;border-radius:5px;';
            role.innerHTML = '<option value="manager">Manager / Senior</option><option value="internee">Internee / Junior</option>';
            role.value = client.team_role || 'internee';
            role.disabled = !checkbox.checked;
            checkbox.addEventListener('change', () => { role.disabled = !checkbox.checked; });
            row.append(choice, role);
            row.dataset.clientId = client.client_id;
            clientAssignmentList.appendChild(row);
        });
        saveClientAssignmentBtn.disabled = false;
    } catch (error) {
        clientAssignmentList.textContent = '';
        showClientAssignmentStatus('❌ ' + error.message, 'error');
    }
}

function closeClientAssignments() {
    clientAssignmentOverlay.style.display = 'none';
    clientAssignmentModal.style.display = 'none';
    document.body.style.overflow = '';
    clientAssignmentUserId = null;
}

function showClientAssignmentStatus(message, type) {
    clientAssignmentStatus.textContent = message;
    clientAssignmentStatus.style.display = 'block';
    clientAssignmentStatus.style.background = type === 'error' ? '#ffebee' : '#e8f5e9';
    clientAssignmentStatus.style.color = type === 'error' ? '#c62828' : '#2e7d32';
    clientAssignmentStatus.style.border = '1px solid ' + (type === 'error' ? '#ef9a9a' : '#a5d6a7');
}

cancelClientAssignmentBtn.addEventListener('click', closeClientAssignments);
clientAssignmentOverlay.addEventListener('click', closeClientAssignments);
saveClientAssignmentBtn.addEventListener('click', async function() {
    if (clientAssignmentUserId === null) return;
    const assignments = Array.from(clientAssignmentList.querySelectorAll('label[data-client-id]'))
        .filter(row => row.querySelector('input[type="checkbox"]').checked)
        .map(row => ({ client_id: Number(row.dataset.clientId), team_role: row.querySelector('select').value }));
    saveClientAssignmentBtn.disabled = true;
    try {
        const res = await fetch(API_BASE + '/api/users/' + clientAssignmentUserId + '/client-assignments', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ assignments })
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || 'Failed to save client assignments');
        showClientAssignmentStatus('✅ ' + result.message, 'success');
        setTimeout(closeClientAssignments, 700);
    } catch (error) {
        showClientAssignmentStatus('❌ ' + error.message, 'error');
        saveClientAssignmentBtn.disabled = false;
    }
});

// ---------- CONFIRM DEACTIVATE ----------
async function confirmDeactivate(id, username) {
    if (!confirm(`Are you sure you want to deactivate user "${username}"? They will no longer be able to log in.`)) {
        return;
    }
    try {
        const res = await fetch(API_BASE + '/api/users/' + id + '/deactivate', { method: 'POST' });
        const result = await res.json();
        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to deactivate user');
        }
        showStatus('✅ ' + result.message, 'success');
        loadUsers();
    } catch (error) {
        showStatus('❌ Error deactivating user: ' + error.message, 'error');
    }
}

// ---------- CONFIRM REACTIVATE ----------
async function confirmReactivate(id, username) {
    if (!confirm(`Reactivate user "${username}"?`)) {
        return;
    }
    try {
        const res = await fetch(API_BASE + '/api/users/' + id + '/reactivate', { method: 'POST' });
        const result = await res.json();
        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to reactivate user');
        }
        showStatus('✅ ' + result.message, 'success');
        loadUsers();
    } catch (error) {
        showStatus('❌ Error reactivating user: ' + error.message, 'error');
    }
}

// ---------- FILTER USERS ----------
function filterUsers(searchTerm) {
    if (!window.userData) return;
    let filtered = window.userData;
    if (searchTerm) {
        filtered = window.userData.filter(user => {
            const username = (user.username || '').toLowerCase();
            return username.includes(searchTerm);
        });
    }
    const sorted = sortUsers(filtered, sortColumn, sortAscending);
    renderUsers(sorted);
    updateSortArrows(sortColumn, sortAscending);
    totalCountSpan.textContent = filtered.length;
}

// ---------- SEARCH ----------
const searchInput = document.getElementById('userSearchInput');
if (searchInput) {
    searchInput.addEventListener('input', function() {
        filterUsers(this.value.trim().toLowerCase());
    });
}

// ---------- CLEAR FORM ----------
function clearForm() {
    userUsername.value = '';
    userPassword.value = '';
    userRole.value = '';
    editingId = null;
    editId.value = '';
    modalStatus.style.display = 'none';
}

// ---------- SAVE FUNCTION ----------
async function saveUser(keepModalOpen) {
    const username = userUsername.value.trim();
    const role = userRole.value || null;

    if (!username) {
        showModalStatus('⚠️ Username is required.', 'error');
        userUsername.focus();
        return false;
    }

    if (!editingId) {
        const password = userPassword.value;
        if (!password) {
            showModalStatus('⚠️ Password is required.', 'error');
            userPassword.focus();
            return false;
        }
    }

    try {
        let url = API_BASE + '/api/users';
        let method = 'POST';
        let payload = { username: username, role: role };

        if (editingId) {
            url += '/' + editingId;
            method = 'PUT';
        } else {
            payload.password = userPassword.value;
        }

        const res = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const result = await res.json();

        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to save user');
        }

        showModalStatus('✅ ' + result.message, 'success');

        await loadUsers();

        if (keepModalOpen) {
            clearForm();
            showModalStatus('✅ Saved! Ready for next entry.', 'success');
            modal.style.display = 'block';
            modalOverlay.style.display = 'block';
            setTimeout(() => userUsername.focus(), 100);
        } else {
            setTimeout(() => {
                closeModal();
                showStatus('✅ User saved successfully!', 'success');
            }, 800);
        }
        return true;

    } catch (error) {
        showModalStatus('❌ Error: ' + error.message, 'error');
        return false;
    }
}

// Prevent accidental form submission
document.getElementById('userForm').addEventListener('submit', function(e) {
    e.preventDefault();
});

// ---------- BUTTON HANDLERS ----------
saveBtn.addEventListener('click', function(e) {
    e.preventDefault();
    saveUser(false);
});

saveAndNewBtn.addEventListener('click', function(e) {
    e.preventDefault();
    // Save and New only makes sense for creating - editingId is null in that flow already.
    saveUser(true);
});

// ---------- RESET PASSWORD MODAL ----------
function openResetPasswordModal(userId, username) {
    document.getElementById('resetPasswordTitle').textContent = `Reset Password — ${username}`;
    resetPasswordUserId.value = userId;
    resetPasswordInput.value = '';
    resetPasswordStatus.style.display = 'none';
    resetPasswordOverlay.style.display = 'block';
    resetPasswordModal.style.display = 'block';
    document.body.style.overflow = 'hidden';
    setTimeout(() => resetPasswordInput.focus(), 100);
}

function closeResetPasswordModal() {
    resetPasswordOverlay.style.display = 'none';
    resetPasswordModal.style.display = 'none';
    document.body.style.overflow = '';
    resetPasswordUserId.value = '';
    resetPasswordInput.value = '';
}

cancelResetPasswordBtn.addEventListener('click', closeResetPasswordModal);
resetPasswordOverlay.addEventListener('click', closeResetPasswordModal);

document.getElementById('resetPasswordForm').addEventListener('submit', function(e) {
    e.preventDefault();
});

submitResetPasswordBtn.addEventListener('click', async function(e) {
    e.preventDefault();
    const userId = resetPasswordUserId.value;
    const password = resetPasswordInput.value;
    if (!password) {
        showResetPasswordStatus('⚠️ New password is required.', 'error');
        resetPasswordInput.focus();
        return;
    }
    try {
        const res = await fetch(API_BASE + '/api/users/' + userId + '/reset-password', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: password })
        });
        const result = await res.json();
        if (!res.ok) {
            throw new Error(result.message || result.detail || 'Failed to reset password');
        }
        showResetPasswordStatus('✅ ' + result.message, 'success');
        setTimeout(() => {
            closeResetPasswordModal();
            showStatus('✅ Password reset successfully!', 'success');
        }, 800);
    } catch (error) {
        showResetPasswordStatus('❌ Error: ' + error.message, 'error');
    }
});

// ---------- ESC KEY ----------
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        if (modal.style.display === 'block') {
            closeModal();
        }
        if (resetPasswordModal.style.display === 'block') {
            closeResetPasswordModal();
        }
        if (clientAssignmentModal.style.display === 'block') {
            closeClientAssignments();
        }
    }
});

// ---------- INIT ----------
window.onload = function() {
    loadUsers();
    setupSorting();
};
