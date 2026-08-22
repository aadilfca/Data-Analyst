// ==============================================
// 🌐 GLOBAL CLIENT CONTEXT – Centralised Switching
// ==============================================

const API_BASE = 'http://localhost:5000';

// Session-backed selected-client cache (replaces the old localStorage handoff). Populated once
// per page load by loadSessionClient() (called from initClientContext() before anything renders
// that depends on it) via GET /api/auth/current-client. Every getter below reads this in-memory
// object rather than localStorage, keeping the existing synchronous getter interface every
// dependent page already relies on.
let sessionClientCache = { id: null, name: null, code: null };

// Ready signal for page scripts. initClientContext() is async (it awaits a real network fetch),
// so window.onload is NOT a safe barrier for it — on a light page, onload fires long before the
// fetch resolves, and any page reading getSelectedClientId() at that moment sees null and wrongly
// concludes no client is selected. Every page's init must await this promise before reading the
// client. Created here at parse time (not inside initClientContext()) so it always exists by the
// time a page script's own onload handler runs, since client_context.js is loaded first everywhere.
// Always resolves, never rejects — loadSessionClient() swallows its own errors and leaves the cache
// null, so a rejection here would hang every page instead of letting it render its no-client state.
let resolveClientContextReady;
window.clientContextReady = new Promise(function (resolve) {
    resolveClientContextReady = resolve;
});

async function loadSessionClient() {
    try {
        const res = await fetch(API_BASE + '/api/auth/current-client');
        if (res.status === 401) {
            // No valid session - bounce to login rather than rendering a page the
            // user isn't allowed to see.
            window.location.href = '/static/login.html';
            return;
        }
        if (!res.ok) throw new Error('Failed to load session client');
        const data = await res.json();
        sessionClientCache = {
            id: data.client_id || null,
            name: data.client_name || null,
            code: data.client_code || null,
        };
    } catch (error) {
        console.warn('⚠️ Failed to load session client:', error.message);
        sessionClientCache = { id: null, name: null, code: null };
    }
}

/**
 * Log out: clear the session server-side, then redirect to login regardless of
 * response (logout is designed to always succeed / be idempotent).
 */
async function logoutUser() {
    try {
        await fetch(API_BASE + '/api/auth/logout', { method: 'POST' });
    } catch (error) {
        console.warn('⚠️ Logout request failed:', error.message);
    }
    window.location.href = '/static/login.html';
}

// ---------- GLOBAL QUICK-NAVIGATION MANIFEST ----------
// Hand-maintained tree describing every navigable destination, grouped exactly like the
// dashboard's three sections (static/index.html). Not a live scrape of the DOM — whoever
// edits either side must mirror changes into the other manually. Plain data (no rendering
// logic) so the search field can flatten/filter this same array directly.
// `module` (when present) is matched against window._appNav.module to highlight the
// current page as "you are here" (see getUpTarget()/data-nav-module below).
// `children` mirror a module's own internal card/hub grid or in-page feature list one level
// deep (no further nesting supported by renderNavMenuPanel()/renderNavSearchResults()) — used
// both for real sub-pages (Verification's Voucher/Process verification tiles, Setup's own
// 5-card grid) and, since the Phase D nav-data merge, for a page's ground-level in-page
// features that have a real visible entry point but no page of their own (Audit Rules' rail
// entities + Assign Categories/Signature Specimens, deep-linked via `?feature=<slug>` — see
// "Global Header Nav Rebuild" in CHANGELOG_ARCHIVE.md for the merge history; this replaces the
// old standalone FEATURE_SEARCH_INDEX/static/search_index.js, deleted in that same phase). A
// child MAY carry `keywords: string[]` (lowercase synonym phrases) — flattenNavManifest()
// carries them through and renderNavSearchResults() matches them by substring alongside label,
// exactly like FEATURE_SEARCH_INDEX's old matching did; a child with no keywords is matched by
// label only, same as before this phase.
// Verification's Cash vouchers/Bank vouchers children use the same ?type=cash/?type=bank URLs as
// verification.html's own hub tiles (#cashVoucherTile/#bankVoucherTile in verification.js) — an
// earlier session added `?view=cashbook`/`?view=bankbook` as separate "Cashbook"/"Bank book"
// entries before realising both resolve to the exact same currentType/openSessionPopup() call as
// ?type=; consolidated down to one entry per destination, matching the hub's own wording exactly.
// (window.onload in verification.js still recognises ?view= too — harmless unused code path now
// that nothing links to it, left as-is since removing it isn't needed for this manifest change.)
const NAV_MANIFEST = [
    {
        title: 'Audit work',
        items: [
            {
                label: 'Verification', url: '/static/verification.html', state: 'ready', module: 'Verification',
                children: [
                    { label: 'Cash vouchers', url: '/static/verification.html?type=cash', state: 'ready' },
                    { label: 'Bank vouchers', url: '/static/verification.html?type=bank', state: 'ready' },
                    { label: 'Local vendor', url: null, state: 'coming-soon' },
                    { label: 'Import vendor', url: null, state: 'coming-soon' },
                    { label: 'Export customer', url: null, state: 'coming-soon' },
                    { label: 'Payroll', url: null, state: 'coming-soon' },
                    { label: 'Contractor', url: null, state: 'coming-soon' },
                    { label: 'Store receiving vs vendor JV', url: null, state: 'coming-soon' },
                    { label: 'PO vs gate receiving vs store receiving', url: null, state: 'coming-soon' },
                    { label: 'Store issuance vs article manufactured', url: null, state: 'coming-soon' },
                ]
            },
            { label: 'Reports', url: null, state: 'coming-soon' },
            { label: 'Analysis', url: null, state: 'coming-soon' },
        ],
    },
    {
        title: 'Client Data',
        items: [
            { label: 'Upload Client Data', url: '/static/upload.html', state: 'ready', module: 'Upload Data' },
            { label: 'Client Data Bank', url: '/static/databank.html', state: 'ready', module: 'Data Bank' },
            { label: 'Trial Balance', url: '/static/trial_balance.html', state: 'ready', module: 'Trial Balance' },
        ],
    },
    {
        title: 'Administration',
        items: [
            {
                label: 'Setup', url: '/static/setup.html', state: 'ready', module: 'Setup',
                children: [
                    { label: 'Manage Columns', url: '/static/columns.html', state: 'ready' },
                    { label: 'Manage Templates', url: '/static/templates.html', state: 'ready' },
                    { label: 'Error Dashboard', url: '/static/error_dashboard.html', state: 'ready' },
                    { label: 'App Settings', url: '/static/settings.html', state: 'ready' },
                ]
            },
            { label: 'Manage Clients', url: '/static/clients.html', state: 'ready', module: 'Manage Clients' },
            {
                label: 'Audit Rules', url: '/static/audit_rules.html', state: 'ready', module: 'Audit Rules',
                children: [
                    {
                        label: 'Documents', url: '/static/audit_rules.html?feature=documents', state: 'ready',
                        keywords: ['supporting document', 'audit document', 'risk rating', 'authorisation roles'],
                    },
                    {
                        label: 'Cross-checks', url: '/static/audit_rules.html?feature=cross-checks', state: 'ready',
                        keywords: ['cross check', 'crosscheck', 'compare field', 'comparison', 'document chain'],
                    },
                    {
                        label: 'Categories', url: '/static/audit_rules.html?feature=categories', state: 'ready',
                        keywords: ['category', 'transaction type', 'validation rules', 'cleared requirements'],
                    },
                    {
                        label: 'Voucher-level Checks', url: '/static/audit_rules.html?feature=voucher-checks', state: 'ready',
                        keywords: ['voucher check', 'prepared by', 'checked by', 'approved by'],
                    },
                    {
                        label: 'Assign Categories', url: '/static/audit_rules.html?feature=assign-categories', state: 'ready',
                        keywords: ['assign category', 'account category', 'trial balance accounts', 'account head'],
                    },
                    {
                        label: 'Signature Specimens', url: '/static/audit_rules.html?feature=signature-specimens', state: 'ready',
                        keywords: ['signature specimen', 'specimen', 'signatory', 'authority'],
                    },
                ]
            },
            { label: 'Settings', url: null, state: 'coming-soon' },
            { label: 'Users', url: null, state: 'coming-soon' },
            { label: 'Audit Log', url: null, state: 'coming-soon' },
        ],
    },
];

/**
 * Flatten NAV_MANIFEST into a single ordered array for search filtering. Pure data
 * transform (no DOM) — recomputed fresh on every keystroke since the manifest is tiny.
 * Each entry carries groupTitle/isChild so renderNavSearchResults() can re-group matches
 * in the manifest's own order without a second, drift-prone copy of the group structure.
 */
function flattenNavManifest() {
    const flat = [];
    NAV_MANIFEST.forEach(group => {
        group.items.forEach(item => {
            flat.push({ label: item.label, url: item.url, state: item.state, groupTitle: group.title, isChild: false });
            (item.children || []).forEach(child => {
                flat.push({ label: child.label, url: child.url, state: child.state, groupTitle: group.title, isChild: true, keywords: child.keywords || [] });
            });
        });
    });
    return flat;
}

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

        // Restore selection from the session-backed cache (loaded before this runs)
        const savedId = sessionClientCache.id;
        if (savedId && clientList.some(c => c.id == savedId)) {
            select.value = savedId;
        } else if (currentVal && clientList.some(c => c.id == currentVal)) {
            select.value = currentVal;
        }
        await updateSelectedClient();
    } catch (error) {
        console.error('⚠️ Error loading clients:', error.message);
        select.innerHTML = '<option value="">⚠️ Failed to load clients</option>';
    }
}

// ---------- UPDATE SELECTED CLIENT (from dropdown) ----------
async function updateSelectedClient() {
    const select = getClientSelect();
    if (!select) return;
    const id = select.value || null;
    selectedClientId = id;
    if (id) {
        const client = clientList.find(c => c.id == id);
        try {
            const res = await fetch(API_BASE + '/api/auth/select-client', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ client_id: Number(id) }),
            });
            if (!res.ok) throw new Error('Failed to select client');
            sessionClientCache = {
                id,
                name: client ? client.client_name : null,
                code: client ? client.client_code : null,
            };
        } catch (error) {
            console.error('⚠️ Failed to select client:', error.message);
        }
    } else {
        // No "unselect" endpoint exists (not requested by this phase) — just reflect the
        // cleared dropdown locally; the session's own selected_client_id is left as-is.
        sessionClientCache = { id: null, name: null, code: null };
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

// ---------- GETTERS (session-backed via sessionClientCache, see loadSessionClient() above) ----------
function getSelectedClientId() {
    return sessionClientCache.id;
}

function getSelectedClientName() {
    return sessionClientCache.name;
}

function getSelectedClientCode() {
    return sessionClientCache.code;
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
// backOverride: LEGACY, pending removal in Phase B. No longer consulted by the Back-arrow click
// handler (Back is now the chronological history stack below) — kept defined only so existing
// callers (e.g. verification.js's setBackOverride(exitSession)) don't throw. Its intended meaning
// ("go to a sensible parent view") is superseded by upOverride, which Phase B will migrate callers
// onto.
// upOverride: null (default, static getUpTarget() resolution) or a function invoked instead of
// navigating — for single-page/multi-view modules (e.g. Verification) whose "up" destination
// depends on in-page view state rather than a fixed URL.
// inPageRestoreHandler: null, or a function(viewKey) a multi-view page registers so the Back
// history stack can restore one of its in-page views instead of navigating (see
// setInPageRestoreHandler()/pushInPageNavEntry() below).
window._appNav = window._appNav || { module: 'Dashboard', stats: '', backOverride: null, upOverride: null, inPageRestoreHandler: null };

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

/**
 * LEGACY, pending removal in Phase B — no longer consulted by the Back-arrow click handler.
 * Kept only so existing callers (verification.js) don't throw. Use setUpOverride() instead.
 * @param {Function|null} fn
 */
window.setBackOverride = function(fn) {
    window._appNav.backOverride = (typeof fn === 'function') ? fn : null;
};

/**
 * Override the Up arrow's behavior for pages with in-page view state (e.g. Verification's
 * Cashbook-selection vs active-session views), where "up" isn't a fixed URL.
 * Pass a function to intercept Up-arrow clicks (called instead of navigating); pass null
 * (or omit) to restore the page's static data-nav-parent/NAV_PARENT_MAP/index.html default.
 * @param {Function|null} fn
 */
window.setUpOverride = function(fn) {
    window._appNav.upOverride = (typeof fn === 'function') ? fn : null;
};

/**
 * Register a handler the Back history stack calls to restore one of this page's in-page views
 * (e.g. Verification's cashbook/session views) instead of navigating, when the stack's target
 * entry shares this page's URL but carries a viewKey. Pass null to unregister.
 * @param {Function|null} fn - called as fn(viewKey)
 */
window.setInPageRestoreHandler = function(fn) {
    window._appNav.inPageRestoreHandler = (typeof fn === 'function') ? fn : null;
};

// ---------- UP-ARROW PARENT-TARGET MECHANISM ----------
// Page-filename -> parent-filename config map. Left empty in this foundation phase;
// later phases populate real entries as each page is wired (e.g. 'client_settings.html': 'clients.html').
const NAV_PARENT_MAP = {
    // 'somepage.html': 'parentpage.html',
};

/**
 * Resolve the Up arrow's destination for the current page.
 * Priority: <body data-nav-parent="..."> override, then NAV_PARENT_MAP, then index.html.
 */
function getUpTarget() {
    const override = document.body.getAttribute('data-nav-parent');
    if (override) return '/static/' + override;

    const currentFile = window.location.pathname.split('/').pop() || 'index.html';
    const mapped = NAV_PARENT_MAP[currentFile];
    if (mapped) return '/static/' + mapped;

    return '/static/index.html';
}

// ---------- BACK: CHRONOLOGICAL HISTORY STACK ----------
// sessionStorage-backed (not a plain JS variable — a real page navigation, e.g. Verification to
// Audit Workbook, reloads this whole module) array of {url, viewKey, label}, capped at 20 entries,
// oldest trimmed first. url is pathname+search; viewKey is null for a plain page load or an opaque
// string a page's own setInPageRestoreHandler() knows how to interpret for an in-page view.
// Follows this project's established try/catch + warn-only + clear-on-parse-error sessionStorage
// convention (see e.g. VERIFICATION_DATE_RANGE_STORAGE_KEY in verification.js).
const NAV_HISTORY_STORAGE_KEY = 'navHistoryStack';
const NAV_PENDING_RESTORE_KEY = 'navPendingRestore';
const NAV_HISTORY_MAX = 20;

function readNavHistory() {
    try {
        const raw = sessionStorage.getItem(NAV_HISTORY_STORAGE_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? parsed : [];
    } catch (error) {
        console.warn('⚠️ Failed to read nav history, resetting:', error.message);
        sessionStorage.removeItem(NAV_HISTORY_STORAGE_KEY);
        return [];
    }
}

function writeNavHistory(stack) {
    try {
        const trimmed = stack.slice(-NAV_HISTORY_MAX);
        sessionStorage.setItem(NAV_HISTORY_STORAGE_KEY, JSON.stringify(trimmed));
    } catch (error) {
        console.warn('⚠️ Failed to write nav history:', error.message);
    }
}

function currentPageUrl() {
    return window.location.pathname + window.location.search;
}

/**
 * Push the current page's entry onto the Back history stack, skipping a duplicate push if it
 * already matches the current top (e.g. a plain refresh). Called once per page load from
 * initClientContext(), after any pending in-page restore has been consumed.
 */
function pushCurrentPageToHistory() {
    const stack = readNavHistory();
    const entry = { url: currentPageUrl(), viewKey: null, label: window._appNav.module };
    const top = stack[stack.length - 1];
    if (top && top.url === entry.url && top.viewKey === entry.viewKey) return;
    stack.push(entry);
    writeNavHistory(stack);
}

/**
 * Page-facing hook (used by multi-view pages, wired in a later phase): push a new history entry
 * for an in-page view change (no navigation happens — the URL stays the same, only viewKey/label
 * differ), so Back can later restore it via setInPageRestoreHandler().
 * @param {string} viewKey
 * @param {string} label
 */
window.pushInPageNavEntry = function(viewKey, label) {
    const stack = readNavHistory();
    const entry = { url: currentPageUrl(), viewKey: viewKey || null, label: label || window._appNav.module };
    const top = stack[stack.length - 1];
    if (top && top.url === entry.url && top.viewKey === entry.viewKey) return;
    stack.push(entry);
    writeNavHistory(stack);
};

/**
 * On page load, consume a pending in-page restore left behind by a cross-page Back click (see
 * handleBackArrowClick() below). Runs before pushCurrentPageToHistory() so the just-restored entry
 * (already the stack's top from the Back click that navigated here) isn't duplicated.
 */
function consumePendingNavRestore() {
    let pending;
    try {
        const raw = sessionStorage.getItem(NAV_PENDING_RESTORE_KEY);
        if (!raw) return;
        pending = JSON.parse(raw);
    } catch (error) {
        console.warn('⚠️ Failed to read pending nav restore, discarding:', error.message);
        sessionStorage.removeItem(NAV_PENDING_RESTORE_KEY);
        return;
    }
    sessionStorage.removeItem(NAV_PENDING_RESTORE_KEY);
    if (pending && pending.viewKey && typeof window._appNav.inPageRestoreHandler === 'function') {
        window._appNav.inPageRestoreHandler(pending.viewKey);
    }
}

/**
 * Back-arrow click: pop the current entry, then either restore an in-page view on this same page
 * (via inPageRestoreHandler) or navigate to a different page (stashing its viewKey, if any, for
 * that page to consume on load via consumePendingNavRestore()).
 */
function handleBackArrowClick() {
    const stack = readNavHistory();
    if (stack.length <= 1) return; // nothing to go back to; button is disabled in this state anyway

    stack.pop(); // discard "current" entry
    const newTop = stack[stack.length - 1];
    writeNavHistory(stack);

    if (newTop.url === currentPageUrl()) {
        if (typeof window._appNav.inPageRestoreHandler === 'function') {
            window._appNav.inPageRestoreHandler(newTop.viewKey);
        }
        return;
    }

    if (newTop.viewKey) {
        try {
            sessionStorage.setItem(NAV_PENDING_RESTORE_KEY, JSON.stringify({ viewKey: newTop.viewKey }));
        } catch (error) {
            console.warn('⚠️ Failed to stash pending nav restore:', error.message);
        }
    }
    window.location.href = newTop.url;
}

// ---------- RENDER LEFT SECTION (brand + home icon + back arrow) ----------
function renderNavLeft() {
    const container = document.getElementById('navLeft');
    if (!container) return;

    const isDash = isDashboardPage();
    const homeIcon = `<a href="/static/index.html" class="nav-home-icon" title="Go to Dashboard">🏢</a>`;
    const logoutButton = `<button type="button" class="nav-logout-btn" title="Log out">🚪 Logout</button>`;
    // Back: chronological history stack (see handleBackArrowClick()) — always intercepted, so a
    // <button> rather than an anchor with a static href. Disabled when there's nowhere to go back
    // to (stack has 0 or 1 entries, i.e. this is the first page visited this session).
    const backDisabled = readNavHistory().length <= 1;
    const backArrow = `<button type="button" class="nav-back-arrow" title="Back — go to the page you just came from"${backDisabled ? ' disabled' : ''}>←</button>`;
    // Up: hierarchical parent (getUpTarget()/upOverride) — also always intercepted, since its
    // destination can depend on a page-registered override rather than a fixed href.
    const upArrow = `<button type="button" class="nav-up-arrow" title="Up — go to the parent screen">↑</button>`;
    // Quick-navigation icon + panel, rendered immediately after the brand text on every page.
    const navMenu = `
        <button type="button" id="navMenuToggle" class="nav-menu-toggle" aria-haspopup="true" aria-expanded="false" title="Quick navigation">⊞</button>
        <div id="navMenuPanel" class="nav-menu-panel" role="menu" hidden></div>
    `;
    // Phase 2: live-search field beside the ⊞ icon, same NAV_MANIFEST data source. Uses the
    // ARIA combobox/listbox pattern — the input keeps real DOM focus the whole time; the
    // "highlighted" result is tracked via aria-activedescendant, not by moving focus onto
    // result rows (see initNavSearch()/renderNavSearchResults() below).
    const navSearch = `
        <div id="navSearchWrapper" class="nav-search-wrapper">
            <input type="text" id="navSearchInput" class="nav-search-input"
                   role="combobox" aria-expanded="false" aria-autocomplete="list"
                   aria-controls="navSearchResults" aria-haspopup="listbox"
                   placeholder="Search... ( / )" title="Search all pages and features" autocomplete="off">
            <div id="navSearchResults" class="nav-menu-panel" role="listbox" hidden></div>
        </div>
    `;

    if (isDash) {
        // Dashboard: home icon + brand, no back arrow (nothing above the dashboard)
        container.innerHTML = `
            ${homeIcon}
            <span style="font-weight:700; font-size:24px; letter-spacing:1px; color:white; margin-left:8px;">System Analyst</span>
            ${navMenu}
            ${navSearch}
            ${logoutButton}
        `;
    } else {
        // Non-dashboard: back arrow, up arrow, then home icon, then brand
        container.innerHTML = `
            ${backArrow}
            ${upArrow}
            ${homeIcon}
            <span style="font-weight:700; font-size:24px; letter-spacing:1px; color:white; margin-left:8px;">System Analyst</span>
            ${navMenu}
            ${navSearch}
            ${logoutButton}
        `;
    }

    initNavMenu();
    initNavSearch();
}

// ---------- QUICK NAVIGATION MENU (⊞ icon + dropdown) ----------
// Re-initialised on every renderNavLeft() call since the toggle/panel are rebuilt via
// innerHTML each time (mirrors how renderClientControl() re-binds clientSelectElement
// after its own innerHTML rebuild, above). Only the toggle/panel's own listeners are
// (re)bound here, since those elements themselves are freshly created each call; the
// document-level outside-click/Escape listeners are bound exactly once at module load
// (below) and always look up the current toggle/panel fresh, so they never duplicate
// across repeated renders (module switches, client changes, etc.).
function initNavMenu() {
    const toggle = document.getElementById('navMenuToggle');
    const panel = document.getElementById('navMenuPanel');
    if (!toggle || !panel) return;

    panel.innerHTML = renderNavMenuPanel();

    toggle.addEventListener('click', function (e) {
        e.stopPropagation();
        if (panel.hidden) {
            openNavMenu(toggle, panel);
        } else {
            closeNavMenu(toggle, panel, false);
        }
    });
}

// Bound once at module load (not inside initNavMenu()) so repeated renderNavLeft() calls
// never stack duplicate document-level listeners. Both look up the live toggle/panel by
// id each time, since those elements are replaced on every render.
document.addEventListener('click', function (e) {
    const toggle = document.getElementById('navMenuToggle');
    const panel = document.getElementById('navMenuPanel');
    if (!toggle || !panel || panel.hidden) return;
    if (!panel.contains(e.target) && e.target !== toggle) {
        closeNavMenu(toggle, panel, false);
    }
});

document.addEventListener('keydown', function (e) {
    const toggle = document.getElementById('navMenuToggle');
    const panel = document.getElementById('navMenuPanel');
    if (!toggle || !panel || panel.hidden) return;

    const items = getNavMenuFocusableItems(panel);
    const currentIndex = items.indexOf(document.activeElement);

    if (e.key === 'Escape') {
        e.preventDefault();
        closeNavMenu(toggle, panel, true);
    } else if (e.key === 'ArrowDown' && panel.contains(document.activeElement)) {
        e.preventDefault();
        const next = items[(currentIndex + 1) % items.length];
        if (next) next.focus();
    } else if (e.key === 'ArrowUp' && panel.contains(document.activeElement)) {
        e.preventDefault();
        const prev = items[(currentIndex - 1 + items.length) % items.length];
        if (prev) prev.focus();
    }
});

/**
 * Build the dropdown's inner HTML from NAV_MANIFEST: one labelled group per section,
 * one row per item (nested one indent level for Verification's Cashbook/Bank book
 * children). Ready items render as real links; coming-soon items (and all sub-view
 * children this phase) render as inert, non-focusable-via-click spans.
 */
function renderNavMenuPanel() {
    const currentModule = window._appNav?.module || null;

    function renderItem(item, isChild) {
        const isCurrent = item.state === 'ready' && item.module && item.module === currentModule;
        const classes = [
            'nav-menu-item',
            item.state === 'coming-soon' ? 'coming-soon' : '',
            isCurrent ? 'current' : '',
            isChild ? 'nav-menu-child' : '',
        ].filter(Boolean).join(' ');

        let html;
        if (item.state === 'ready') {
            html = `<a href="${item.url}" role="menuitem" class="${classes}">${item.label}</a>`;
        } else {
            html = `<span role="menuitem" aria-disabled="true" class="${classes}">${item.label}</span>`;
        }

        if (item.children && item.children.length) {
            html += item.children.map(child => renderItem(child, true)).join('');
        }
        return html;
    }

    return NAV_MANIFEST.map(group => `
        <div class="nav-menu-group">
            <div class="nav-menu-group-title">${group.title}</div>
            ${group.items.map(item => renderItem(item, false)).join('')}
        </div>
    `).join('');
}

function getNavMenuFocusableItems(panel) {
    return Array.from(panel.querySelectorAll('[role="menuitem"]:not([aria-disabled])'));
}

// ---------- VIEWPORT-AWARE PANEL POSITIONING (shared by the Phase 1 dropdown and Phase 2 search
// results — both use the .nav-menu-panel class/CSS, but anchor to different positioned ancestors:
// #navLeft for the dropdown, #navSearchWrapper for search. Both call sites (openNavMenu() below,
// renderNavSearchResults() further down) pass their own anchor into this one function so the fix
// lives in exactly one place. ----------
// Bug fixed (horizontal): the panel's CSS position (left:0, no width bound) let it render
// partially/fully off-screen whenever its anchor sat close enough to the right edge that a
// min-width:240px panel wouldn't fit. Fixed by measuring the anchor's real position via
// getBoundingClientRect() against window.innerWidth at the moment the panel opens, then choosing
// left- vs right-alignment and clamping width.
// Bug fixed (vertical, follow-up): the panel always opened downward (top:100%, fixed in CSS) with
// no max-height/overflow-y at all, so a tall item list (e.g. verification.html's full manifest)
// could extend below the visible browser window/taskbar. Fixed the same way: measure against
// window.innerHeight, flip to open upward when that has more room, and — per the user's confirmed
// choice — clamp height and let the panel scroll internally if neither direction has enough room,
// matching the existing max-height+overflow-y convention already used by .transaction-list-popover
// and .voucher-change-popover/.audit-guidance-popover elsewhere in this app (verification.js/
// style.css), rather than inventing a new pattern.
// Neither axis re-runs on window resize while a panel is already open (not part of the confirmed
// scope for either fix) — closing and reopening re-measures correctly.
function positionNavPanel(anchorEl, panel) {
    if (!anchorEl || !panel) return;

    // Clear any inline overrides from a previous open (e.g. under a different viewport size)
    // before measuring, so stale values from an earlier open can't leak into this one.
    panel.style.left = '';
    panel.style.right = '';
    panel.style.maxWidth = '';
    panel.style.minWidth = '';
    panel.style.top = '';
    panel.style.bottom = '';
    panel.style.marginTop = '';
    panel.style.marginBottom = '';
    panel.style.maxHeight = '';
    panel.style.overflowY = '';

    const anchorRect = anchorEl.getBoundingClientRect();
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const edgeMargin = 12; // small breathing room from the viewport edge, not flush against it
    // Real rendered size now that panel.hidden is false and inline overrides were just cleared
    // (must be called after unhiding — an element with display:none, which [hidden] sets, always
    // measures 0 — and before re-applying max-height, or this would measure the clamped size
    // instead of the panel's true natural size).
    const naturalWidth = panel.offsetWidth;
    const naturalHeight = panel.offsetHeight;

    // ---- Horizontal (left/right) ----
    const spaceRight = viewportWidth - anchorRect.left - edgeMargin;
    const spaceLeft = anchorRect.right - edgeMargin;

    let availableWidth;
    if (naturalWidth <= spaceRight) {
        // Default left-aligned anchor (CSS left:0) already fits — keep it, just cap max-width
        // to the real available space as a safeguard against a future wider panel.
        availableWidth = spaceRight;
    } else if (naturalWidth <= spaceLeft) {
        // Doesn't fit opening rightward from the anchor's left edge, but does fit opening
        // leftward from the anchor's right edge — flip to right-aligned.
        panel.style.left = 'auto';
        panel.style.right = '0';
        availableWidth = spaceLeft;
    } else {
        // Neither side has room for the panel's natural width (e.g. a very narrow viewport) —
        // pick whichever side has more space and clamp hard so it still can't overflow.
        if (spaceRight >= spaceLeft) {
            availableWidth = spaceRight;
        } else {
            panel.style.left = 'auto';
            panel.style.right = '0';
            availableWidth = spaceLeft;
        }
    }

    // Floor at a small usable width so an extreme viewport never collapses the panel to nothing.
    const clampedWidth = Math.max(160, availableWidth);
    panel.style.maxWidth = clampedWidth + 'px';
    // The CSS min-width:240px would otherwise win over max-width when they conflict (min-width
    // takes priority), forcing overflow right back in on a tight viewport — override it down to
    // match whenever the clamp is tighter than that default.
    panel.style.minWidth = Math.min(240, clampedWidth) + 'px';

    // ---- Vertical (top/bottom) ----
    // margin-top:8px on the default (downward) direction comes from CSS; mirrored manually as
    // margin-bottom when flipped upward, so the same 8px gap from the anchor applies either way.
    const spaceBelow = viewportHeight - anchorRect.bottom - edgeMargin - 8;
    const spaceAbove = anchorRect.top - edgeMargin - 8;

    let availableHeight;
    if (naturalHeight <= spaceBelow) {
        // Default downward open (CSS top:100%) already fits — keep it, just cap max-height as a
        // safeguard against a future taller panel.
        availableHeight = spaceBelow;
    } else if (naturalHeight <= spaceAbove) {
        // Doesn't fit opening downward, but does fit opening upward — flip above the anchor.
        panel.style.top = 'auto';
        panel.style.bottom = '100%';
        panel.style.marginTop = '0';
        panel.style.marginBottom = '8px';
        availableHeight = spaceAbove;
    } else {
        // Neither direction has room for the panel's natural height (a tall item list on a short
        // window) — open on whichever side has more space and let the panel scroll internally for
        // the rest, per the confirmed choice and the existing popover convention in this app.
        if (spaceBelow >= spaceAbove) {
            availableHeight = spaceBelow;
        } else {
            panel.style.top = 'auto';
            panel.style.bottom = '100%';
            panel.style.marginTop = '0';
            panel.style.marginBottom = '8px';
            availableHeight = spaceAbove;
        }
    }

    // Floor at a small usable height so an extreme viewport never collapses the panel to nothing;
    // overflow-y is always set (not just in the "still doesn't fit" branch) so it's a harmless
    // no-op when there's room and a real scroll fallback the moment there isn't.
    const clampedHeight = Math.max(120, availableHeight);
    panel.style.maxHeight = clampedHeight + 'px';
    panel.style.overflowY = 'auto';
}

function openNavMenu(toggle, panel) {
    panel.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    positionNavPanel(document.getElementById('navLeft'), panel);
    const items = getNavMenuFocusableItems(panel);
    if (items.length) items[0].focus();
}

function closeNavMenu(toggle, panel, returnFocus) {
    panel.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    if (returnFocus) toggle.focus();
}

// ---------- QUICK NAVIGATION SEARCH (Phase 2) ----------
// Index of the currently keyboard-highlighted result within the ready-only result set
// (see getSearchFocusableResults()); -1 means "nothing explicitly highlighted yet", in
// which case Enter falls back to the first ready result, per the confirmed spec.
let searchHighlightIndex = -1;

/**
 * Re-bound on every renderNavLeft() call since the input/panel are rebuilt via innerHTML
 * each time (same reasoning as initNavMenu()). Only the input's own listeners are (re)bound
 * here; the outside-click listener is bound once at module load, below.
 */
function initNavSearch() {
    const input = document.getElementById('navSearchInput');
    const panel = document.getElementById('navSearchResults');
    if (!input || !panel) return;

    input.addEventListener('input', function () {
        renderNavSearchResults(input.value);
    });

    input.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            e.preventDefault();
            input.value = '';
            closeNavSearch(input, panel);
        } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            if (panel.hidden) return;
            e.preventDefault();
            const results = getSearchFocusableResults(panel);
            if (!results.length) return;
            if (e.key === 'ArrowDown') {
                searchHighlightIndex = (searchHighlightIndex + 1) % results.length;
            } else {
                searchHighlightIndex = (searchHighlightIndex - 1 + results.length) % results.length;
            }
            applySearchHighlight(input, results);
        } else if (e.key === 'Enter') {
            if (panel.hidden) return;
            e.preventDefault();
            const results = getSearchFocusableResults(panel);
            const target = results[searchHighlightIndex] || results[0];
            if (target) {
                window.location.href = target.getAttribute('href');
            }
        }
    });
}

// Bound once at module load (mirrors the nav-menu outside-click listener's reasoning above)
// so repeated renderNavLeft() calls never stack duplicate document-level listeners. Closes
// the results panel without clearing the typed text — deliberately different from Escape.
document.addEventListener('click', function (e) {
    const wrapper = document.getElementById('navSearchWrapper');
    const panel = document.getElementById('navSearchResults');
    if (!wrapper || !panel || panel.hidden) return;
    if (!wrapper.contains(e.target)) {
        panel.hidden = true;
        const input = document.getElementById('navSearchInput');
        if (input) input.setAttribute('aria-expanded', 'false');
    }
});

/**
 * Filter NAV_MANIFEST (via flattenNavManifest()) by a case-insensitive substring match
 * against each entry's label, and render the matches grouped exactly like Phase 1's
 * dropdown (group order/titles come straight from NAV_MANIFEST, never duplicated).
 * Reuses the .nav-menu-panel/.nav-menu-group/.nav-menu-item classes Phase 1 already
 * built, so the two mechanisms share one visual language.
 */
function renderNavSearchResults(query) {
    const input = document.getElementById('navSearchInput');
    const panel = document.getElementById('navSearchResults');
    if (!input || !panel) return;

    const trimmed = query.trim();
    searchHighlightIndex = -1;

    if (!trimmed) {
        panel.hidden = true;
        panel.innerHTML = '';
        input.setAttribute('aria-expanded', 'false');
        input.removeAttribute('aria-activedescendant');
        return;
    }

    const needle = trimmed.toLowerCase();
    const matches = flattenNavManifest().filter(entry =>
        entry.label.toLowerCase().includes(needle) ||
        (entry.keywords || []).some(kw => kw.includes(needle))
    );

    if (!matches.length) {
        panel.innerHTML = '<div class="nav-search-empty">No matches found.</div>';
    } else {
        let optionIndex = 0;
        const groupsHtml = NAV_MANIFEST.map(group => {
            const groupMatches = matches.filter(m => m.groupTitle === group.title);
            if (!groupMatches.length) return '';
            const rowsHtml = groupMatches.map(entry => {
                const classes = [
                    'nav-menu-item',
                    entry.state === 'coming-soon' ? 'coming-soon' : '',
                    entry.isChild ? 'nav-menu-child' : '',
                ].filter(Boolean).join(' ');
                if (entry.state === 'ready') {
                    const id = 'navSearchOpt-' + optionIndex;
                    optionIndex += 1;
                    return `<a href="${entry.url}" role="option" id="${id}" class="${classes}">${entry.label}</a>`;
                }
                return `<span role="option" aria-disabled="true" class="${classes}">${entry.label}</span>`;
            }).join('');
            return `<div class="nav-menu-group"><div class="nav-menu-group-title">${group.title}</div>${rowsHtml}</div>`;
        }).join('');
        panel.innerHTML = groupsHtml;
    }

    panel.hidden = false;
    // Shared with the Phase 1 dropdown (see positionNavPanel() above) — anchored to
    // #navSearchWrapper (the search input's own box) rather than #navLeft, since that's this
    // panel's actual positioned ancestor.
    positionNavPanel(document.getElementById('navSearchWrapper'), panel);
    input.setAttribute('aria-expanded', 'true');
}

// Only ready (actionable) rows participate in keyboard highlighting/Enter — coming-soon
// rows are visible but never targetable, matching Phase 1's dropdown convention exactly.
function getSearchFocusableResults(panel) {
    return Array.from(panel.querySelectorAll('a[role="option"]'));
}

function applySearchHighlight(input, results) {
    results.forEach((el, i) => {
        el.classList.toggle('highlighted', i === searchHighlightIndex);
    });
    const active = results[searchHighlightIndex];
    if (active) {
        input.setAttribute('aria-activedescendant', active.id);
    } else {
        input.removeAttribute('aria-activedescendant');
    }
}

function closeNavSearch(input, panel) {
    panel.hidden = true;
    panel.innerHTML = '';
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    searchHighlightIndex = -1;
    input.focus();
}

// ---------- GLOBAL KEYBOARD SHORTCUT: "/" FOCUSES SEARCH ----------
// Bound once at module load (same reasoning as the other document-level listeners in this file —
// #navSearchInput is rebuilt via innerHTML on every renderNavLeft() call, so this looks it up fresh
// by id each time rather than caching a reference). Guards on the currently focused element so
// typing a literal "/" into any other input/textarea/contenteditable field anywhere in the app is
// never intercepted — the app's existing global keydown handlers (e.g. script.js's "R"-to-refresh)
// have no such guard, which is a gap this shortcut deliberately avoids repeating.
document.addEventListener('keydown', function (e) {
    if (e.key !== '/') return;
    const active = document.activeElement;
    const isEditable = active && (
        active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable
    );
    if (isEditable) return;
    const input = document.getElementById('navSearchInput');
    if (!input) return;
    e.preventDefault();
    input.focus();
    input.select();
});

// ---------- GLOBAL CLICK HANDLER: BACK ARROW (chronological history stack) ----------
document.addEventListener('click', function (e) {
    const backArrow = e.target.closest('.nav-back-arrow');
    if (backArrow && !backArrow.disabled) {
        handleBackArrowClick();
    }
});

// ---------- GLOBAL CLICK HANDLER: UP ARROW (hierarchical parent) ----------
// When a page has registered a dynamic override via setUpOverride() (e.g. Verification's in-page
// view switching), run that instead of navigating to the static getUpTarget() URL.
document.addEventListener('click', function (e) {
    const upArrow = e.target.closest('.nav-up-arrow');
    if (!upArrow) return;
    if (typeof window._appNav.upOverride === 'function') {
        window._appNav.upOverride();
    } else {
        window.location.href = getUpTarget();
    }
});

// ---------- GLOBAL CLICK HANDLER: LOGOUT ----------
document.addEventListener('click', function (e) {
    const logoutBtn = e.target.closest('.nav-logout-btn');
    if (logoutBtn) {
        logoutUser();
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
async function initClientContext() {
    // Data-attribute source for the Area label (mirrors getUpTarget()'s data-nav-parent read) -
    // lets a self-contained page declare its module name via <body data-nav-module="..."> instead
    // of needing its own setNavModule() call. Read before the first render so it's correct on
    // first paint; setNavModule() can still override it later (e.g. verification.js at runtime).
    const moduleAttr = document.body.getAttribute('data-nav-module');
    if (moduleAttr) {
        window._appNav.module = moduleAttr;
    }
    // Consume any pending in-page restore left by a cross-page Back click before pushing this
    // page's own history entry, so the restored entry isn't duplicated (see
    // consumePendingNavRestore()/pushCurrentPageToHistory() above).
    consumePendingNavRestore();
    pushCurrentPageToHistory();
    // Populate the session-backed client cache before anything renders that depends on it
    // (the client badge/dropdown). This is a real network fetch, and window.onload waiting on
    // subresources says nothing about an in-flight fetch() — on a light page onload fires first.
    // Page scripts must therefore await window.clientContextReady (resolved below) rather than
    // assuming their own window.onload runs after this.
    await loadSessionClient();
    renderClientControl();
    renderNavLeft();
    // Resolved only once the client cache is populated AND the navbar is rendered, so a page
    // awaiting this gets both.
    resolveClientContextReady();
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