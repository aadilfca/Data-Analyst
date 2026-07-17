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
    // If we are on a non-dashboard page, update the client name in the navbar
    if (!isDashboardPage()) {
        const name = getSelectedClientName() || 'No Client';
        updateNavClientName(name);
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

    // Determine if we are on the dashboard
    const isDash = isDashboardPage();

    if (isDash) {
        // Dashboard: show dropdown for client selection, no back arrow
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
        // Non-dashboard: show back arrow, client badge, AREA, stats
        const clientName = getSelectedClientName() || 'No Client';
        const moduleName = window._appNav?.module || 'Dashboard';
        const statsText = window._appNav?.stats || '';

                container.innerHTML = `
            <span class="nav-client-name" style="font-weight:500; font-size:13px; color:#f1f5f9; background:#1e293b; padding:2px 12px; border-radius:16px; border:1px solid #334155; white-space:nowrap;">${clientName}</span>
            <span class="nav-area" style="font-weight:600; font-size:14px; color:#e0f2fe; letter-spacing:0.3px; white-space:nowrap;">AREA: ${moduleName}</span>
            <span id="navStats" style="font-size:13px; color:#94a3b8; white-space:nowrap;">${statsText}</span>
        `;
        // Enforce client selection on all non-dashboard pages
        enforceClientSelection();
    }
}

// ---------- GLOBAL NAVBAR UPDATE FUNCTIONS ----------
window._appNav = window._appNav || { module: 'Dashboard', stats: '' };

/**
 * Set the module name displayed in the navbar (e.g., "Verification", "Upload")
 * @param {string} name - The module name (e.g., "Verification")
 */
window.setNavModule = function(name) {
    window._appNav.module = name || 'Dashboard';
    // Re-render the entire navbar to reflect the change
    renderClientControl();
};

/**
 * Set the statistics text displayed in the navbar (e.g., "Session 14 · Cashbook: ...")
 * @param {string} stats - The stats text to display (empty string to hide)
 */
window.setNavStats = function(stats) {
    window._appNav.stats = stats || '';
    // Re-render the entire navbar to reflect the change
    renderClientControl();
};

/**
 * Update the client name displayed in the navbar (used after client change)
 * @param {string} name - The client name (e.g., "ACME")
 */
window.updateNavClientName = function(name) {
    const clientSpan = document.querySelector('.nav-client-name');
    if (clientSpan) {
        clientSpan.textContent = name || 'No Client';
    }
};
// ---------- BACK-ARROW PARENT-TARGET MECHANISM ----------
// Page-filename -> parent-filename config map. Left empty in this foundation phase;
// Phases 2-5 populate real entries as each page is wired (e.g. 'client_settings.html': 'clients.html').
const NAV_PARENT_MAP = {
    // 'somepage.html': 'parentpage.html',
};

/**
 * Resolve the back arrow's destination for the current page.
 * Priority: <body data-nav-parent="..."> override, then NAV_PARENT_MAP, then index.html.
 */
function getBackTarget() {
    const override = document.body.getAttribute('data-nav-parent');
    if (override) return '/static/' + override;

    const currentFile = window.location.pathname.split('/').pop() || 'index.html';
    const mapped = NAV_PARENT_MAP[currentFile];
    if (mapped) return '/static/' + mapped;

    return '/static/index.html';
}

// ---------- RENDER LEFT SECTION (brand + home icon + back arrow) ----------
function renderNavLeft() {
    const container = document.getElementById('navLeft');
    if (!container) return;

    const isDash = isDashboardPage();
    const homeIcon = `<a href="/static/index.html" class="nav-home-icon" title="Go to Dashboard">🏢</a>`;

    if (isDash) {
        // Dashboard: home icon + brand, no back arrow (nothing above the dashboard)
        container.innerHTML = `
            ${homeIcon}
            <span style="font-weight:700; font-size:24px; letter-spacing:1px; color:white; margin-left:8px;">System Analyst</span>
        `;
    } else {
        // Non-dashboard: back arrow, then home icon, then brand
        container.innerHTML = `
            <a href="${getBackTarget()}" class="nav-back-arrow">←</a>
            ${homeIcon}
            <span style="font-weight:700; font-size:24px; letter-spacing:1px; color:white; margin-left:8px;">System Analyst</span>
        `;
    }
}

// ---------- GLOBAL CLICK HANDLER: HOME ICON ----------
// Delegated at document level so any current or future element carrying the
// .nav-home-icon class navigates to the dashboard, even if a page doesn't wire its own href.
document.addEventListener('click', function (e) {
    const homeIcon = e.target.closest('.nav-home-icon');
    if (homeIcon) {
        window.location.href = '/static/index.html';
    }
});
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
    renderNavLeft();
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