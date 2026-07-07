// ==============================================
// 🌐 GLOBAL CLIENT CONTEXT – Centralised Switching
// ==============================================

const API_BASE = 'http://localhost:5000';
const STORAGE_KEY = 'selectedClientId';
const STORAGE_NAME = 'selectedClientName';
const STORAGE_CODE = 'selectedClientCode';

// ---------- STATE ----------
let clientList = [];
let selectedClientId = null;
let clientSelectElement = null;

// ---------- DOM REFS ----------
function getClientSelect() {
    if (!clientSelectElement) {
        clientSelectElement = document.getElementById('globalClientSelect');
    }
    return clientSelectElement;
}

// ---------- LOAD CLIENTS (for dropdown) ----------
async function loadClientDropdown() {
    const select = getClientSelect();
    if (!select) return;

    try {
        const res = await fetch(API_BASE + '/api/clients');
        if (!res.ok) throw new Error('Failed to fetch clients');
        clientList = await res.json();

        const currentVal = select.value;
        select.innerHTML = '<option value="">-- Select Client --</option>';
        clientList.forEach(client => {
            const opt = document.createElement('option');
            opt.value = client.id;
            opt.textContent = client.client_code + ' - ' + client.client_name;
            select.appendChild(opt);
        });

        // Restore selection from localStorage
        const savedId = localStorage.getItem(STORAGE_KEY);
        if (savedId && clientList.some(c => c.id == savedId)) {
            select.value = savedId;
        } else if (currentVal && clientList.some(c => c.id == currentVal)) {
            select.value = currentVal;
        }
        updateSelectedClient();

        console.log('✅ Client dropdown loaded with ' + clientList.length + ' clients');
    } catch (error) {
        console.error('⚠️ Error loading clients:', error.message);
        select.innerHTML = '<option value="">⚠️ Failed to load clients</option>';
    }
}

// ---------- UPDATE SELECTED CLIENT (from dropdown) ----------
function updateSelectedClient() {
    const select = getClientSelect();
    if (!select) return;
    const id = select.value || null;
    selectedClientId = id;
    if (id) {
        const client = clientList.find(c => c.id == id);
        localStorage.setItem(STORAGE_KEY, id);
        if (client) {
            localStorage.setItem(STORAGE_NAME, client.client_name);
            localStorage.setItem(STORAGE_CODE, client.client_code);
        }
    } else {
        localStorage.removeItem(STORAGE_KEY);
        localStorage.removeItem(STORAGE_NAME);
        localStorage.removeItem(STORAGE_CODE);
    }
    updateClientDependentUI();
    // If we are on a non-dashboard page, re-render the badge
    if (!isDashboardPage()) {
        renderClientBadge();
    }
}

// ---------- UI UPDATES (for upload page) ----------
function updateClientDependentUI() {
    const hasClient = !!getSelectedClientId();
    const uploadBtn = document.getElementById('importBtn');
    const warningEl = document.getElementById('clientSelectionWarning');

    if (warningEl) {
        warningEl.style.display = hasClient ? 'none' : 'flex';
    }
    if (uploadBtn) {
        const templateSelected = document.getElementById('templateDropdown') &&
            document.getElementById('templateDropdown').value;
        uploadBtn.disabled = !hasClient || !templateSelected;
    }
}

// ---------- GETTERS (using localStorage for non-dashboard pages) ----------
function getSelectedClientId() {
    return localStorage.getItem(STORAGE_KEY) || null;
}

function getSelectedClientName() {
    return localStorage.getItem(STORAGE_NAME) || null;
}

function getSelectedClientCode() {
    return localStorage.getItem(STORAGE_CODE) || null;
}

function isClientSelected() {
    return !!getSelectedClientId();
}

// ---------- PAGE DETECTION ----------
function isDashboardPage() {
    const path = window.location.pathname;
    return path.endsWith('index.html') || path === '/' || path === '/static/' || path === '/static/index.html';
}

// ---------- ENFORCE CLIENT SELECTION ----------
function enforceClientSelection() {
    if (!isClientSelected()) {
        // Only enforce on non-dashboard pages
        if (!isDashboardPage()) {
            // Show a friendly alert and redirect to dashboard
            alert('⚠️ Please select a client from the Dashboard first.');
            window.location.href = '/static/index.html';
            return false;
        }
        return false;
    }
    return true;
}

// ---------- RENDER CLIENT CONTROL (dropdown or badge) ----------
function renderClientControl() {
    const container = document.getElementById('clientControl');
    if (!container) return;

    if (isDashboardPage()) {
        // Dashboard: show dropdown
        container.innerHTML = `
            <label for="globalClientSelect">🏢 Client</label>
            <select id="globalClientSelect">
                <option value="">-- Select --</option>
            </select>
        `;
        // Re-bind the select element and load clients
        clientSelectElement = document.getElementById('globalClientSelect');
        loadClientDropdown();
        // Add change listener
        clientSelectElement.addEventListener('change', updateSelectedClient);
    } else {
        // Non-dashboard: show read-only badge with switch icon
        renderClientBadge();
        // Enforce client selection on all non-dashboard pages
        enforceClientSelection();
    }
}

// ---------- RENDER CLIENT BADGE (read-only) ----------
function renderClientBadge() {
    const container = document.getElementById('clientControl');
    if (!container) return;

    const clientId = getSelectedClientId();
    const clientName = getSelectedClientName();
    const clientCode = getSelectedClientCode();

    if (clientId && clientName) {
        container.innerHTML = `
            <span class="client-badge">
                🏢 ${clientName} (${clientCode})
                <button class="client-switch-btn" title="Switch Client" onclick="confirmSwitchClient()">🔄</button>
            </span>
        `;
    } else {
        container.innerHTML = `
            <span class="client-badge client-none">
                ⚠️ No client selected
                <button class="client-switch-btn" title="Go to Dashboard to select client" onclick="goToDashboard()">🏠</button>
            </span>
        `;
    }
}

// ---------- SWITCH CLIENT FLOW ----------
function confirmSwitchClient() {
    if (confirm('Do you want to switch client? This will return you to the Dashboard.')) {
        alert('All client-related data visible on current pages will be cleared. Your data is safely stored in the database.');
        window.location.href = '/static/index.html';
    }
}

function goToDashboard() {
    window.location.href = '/static/index.html';
}

// ---------- INIT ----------
function initClientContext() {
    renderClientControl();
    console.log('🌐 Client context initialized');
}

// ---------- EXPOSE GLOBALLY ----------
window.getSelectedClientId = getSelectedClientId;
window.getSelectedClientName = getSelectedClientName;
window.getSelectedClientCode = getSelectedClientCode;
window.isClientSelected = isClientSelected;
window.confirmSwitchClient = confirmSwitchClient;
window.goToDashboard = goToDashboard;
window.renderClientBadge = renderClientBadge;
window.enforceClientSelection = enforceClientSelection;
window.onClientChange = null; // Hook for page-specific logic

// ---------- AUTO-INIT ON DOM READY ----------
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initClientContext);
} else {
    initClientContext();
}

console.log('📦 Client context module loaded (centralised switching)');