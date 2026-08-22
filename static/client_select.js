// ==============================================
// 🗂️ CLIENT CARDS LANDING PAGE
// ==============================================
// Standalone page (no client_context.js/navbar — no client is selected yet), so this file
// defines its own minimal fetch call rather than relying on client_context.js's global API_BASE.
// Selection is session-backed: a card click PUTs to /api/auth/select-client, which writes
// sessions.selected_client_id server-side. client_context.js reads it back on every page load
// via GET /api/auth/current-client. Returning to this page never auto-selects the last-used
// client — the user must click a card again each time.

let allClients = [];

const clientCardGrid = document.getElementById('clientCardGrid');
const clientSelectEmpty = document.getElementById('clientSelectEmpty');
const clientSearchInput = document.getElementById('clientSearchInput');

async function loadClients() {
    try {
        const res = await fetch('/api/clients');
        if (!res.ok) throw new Error('Failed to load clients');
        allClients = await res.json();
        renderClients(allClients);
    } catch (err) {
        clientCardGrid.innerHTML = '';
        clientSelectEmpty.textContent = 'Could not load clients. Please try again.';
        clientSelectEmpty.style.display = 'block';
    }
}

function renderClients(clients) {
    clientCardGrid.innerHTML = '';

    if (clients.length === 0) {
        clientSelectEmpty.style.display = 'block';
        return;
    }
    clientSelectEmpty.style.display = 'none';

    clients.forEach(client => {
        const card = document.createElement('div');
        card.className = 'module-card';

        const initial = (client.client_name || '?').trim().charAt(0).toUpperCase();
        const industryHtml = client.industry
            ? `<p>${escapeHtml(client.industry)}</p>`
            : '';

        card.innerHTML = `
            <div class="client-card-avatar">${escapeHtml(initial)}</div>
            <div class="client-card-code">${escapeHtml(client.client_code || '')}</div>
            <h3>${escapeHtml(client.client_name || '')}</h3>
            ${industryHtml}
        `;

        card.addEventListener('click', () => selectClient(client));
        clientCardGrid.appendChild(card);
    });
}

// ---------- POST-SELECTION INTENT ("?next=") ----------
// A user who asks for a client-scoped page while no client is selected gets sent here first. Without
// this, picking a client dropped them on the dashboard and silently discarded what they actually
// asked for. The requesting page stamps ?next=<key> (currently only getClientSettingsUrl() in
// client_context.js) and this table turns that key back into a real destination.
//
// `from` travels with the destination rather than being hardcoded at the build site, so the arriving
// page's Up arrow/back-link resolve through client_context.js's NAV_ENTRY_SOURCES to the door the
// user actually came through — here the dashboard, since that is where both stamping entry points
// (the #clientSettingsCard and the ⊞ nav entry) live.
//
// SECURITY: this is a whitelist lookup on purpose, mirroring NAV_ENTRY_SOURCES. Never build a path
// by concatenating the raw param ('/static/' + nextParam) — that is an open redirect. An unknown or
// absent key resolves to null and the caller falls back to the dashboard.
//
// The table lives here, in the file that CONSUMES the param, rather than beside NAV_ENTRY_SOURCES in
// client_context.js: this is a standalone pre-session page that cannot load that file (see the <nav>
// comment in client_select.html). One copy at the point of use also can't drift the way two would.
const NEXT_DESTINATIONS = {
    client_settings: { file: 'client_settings.html', from: 'dashboard' },
};

// Returns a fully-built destination URL for the just-selected client, or null when there is no
// recognised ?next= to honour.
function resolveNextUrl(clientId) {
    const params = new URLSearchParams(window.location.search);
    const next = params.get('next');
    if (!next) return null;

    const destination = NEXT_DESTINATIONS[next];
    if (!destination) return null;

    return '/static/' + destination.file + '?client_id=' + clientId + '&from=' + destination.from;
}

async function selectClient(client) {
    try {
        const res = await fetch('/api/auth/select-client', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ client_id: client.id }),
        });
        if (!res.ok) throw new Error('Failed to select client');
        // With no (or an unrecognised) ?next=, this is exactly the previous behaviour.
        window.location.href = resolveNextUrl(client.id) || '/static/index.html';
    } catch (err) {
        clientCardGrid.innerHTML = '';
        clientSelectEmpty.textContent = 'Could not select client. Please try again.';
        clientSelectEmpty.style.display = 'block';
    }
}

function filterClients(searchTerm) {
    if (!searchTerm) {
        renderClients(allClients);
        return;
    }
    const filtered = allClients.filter(client => {
        const name = (client.client_name || '').toLowerCase();
        const code = (client.client_code || '').toLowerCase();
        const industry = (client.industry || '').toLowerCase();
        return name.includes(searchTerm) || code.includes(searchTerm) || industry.includes(searchTerm);
    });
    renderClients(filtered);
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

clientSearchInput.addEventListener('input', function () {
    filterClients(this.value.trim().toLowerCase());
});

// Admin-only Settings entry point (Admin area, Phase 7 of 8) - same GET /api/users 403-probe
// pattern reused across settings.js/clients.js. Standalone page, no client_context.js/API_BASE,
// so this follows the plain-relative-path fetch convention already used elsewhere in this file.
async function checkAdminAndShowGear() {
    try {
        const res = await fetch('/api/users');
        if (res.ok) {
            const btn = document.getElementById('adminSettingsBtn');
            if (btn) btn.style.display = '';
        }
    } catch (err) {
        // Fail closed - gear icon stays hidden, same as a 403.
    }
}

document.getElementById('adminSettingsBtn')?.addEventListener('click', () => {
    window.location.href = '/static/settings.html';
});

// ---------- LIGHTWEIGHT HEADER ----------
// Fills the #clientSelectNav container with the same markup/classes client_context.js's
// renderFirmLevelNav() produces, so the two headers stay visually identical with no CSS of this
// page's own. Rendered here rather than reusing that function because this page cannot load
// client_context.js at all — see the comment on the <nav> in client_select.html for why.
// Two deliberate differences from renderFirmLevelNav():
//   1. The brand is a plain <span>, not an <a> — on the Admin pages it links TO this page, so
//      here that would be a self-link to nowhere.
//   2. The area label reads "Module: Client", not "AREA: Administration" — this isn't the
//      Admin area, it's the client picker.
function renderClientSelectHeader(username) {
    const container = document.getElementById('clientSelectNav');
    if (!container) return;

    container.innerHTML = `
        <div class="firm-nav-left">
            <span class="firm-nav-brand">System Analyst</span>
            <span class="firm-nav-area">Module: Client</span>
        </div>
        <div class="firm-nav-right">
            <span class="nav-user-name">👤 Welcome, ${escapeHtml(username || 'User')}</span>
            <button type="button" class="nav-logout-btn" title="Log out" aria-label="Log out">🚪</button>
        </div>
    `;

    container.querySelector('.nav-logout-btn').addEventListener('click', logout);
}

// Local copy of client_context.js's logoutUser() — that file isn't loaded here, so neither is its
// document-level .nav-logout-btn listener. Same contract: clear the session server-side, then go to
// login regardless of the response (logout is idempotent and must never leave the user stuck).
async function logout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
    } catch (err) {
        console.warn('⚠️ Logout request failed:', err.message);
    }
    window.location.href = '/static/login.html';
}

// This page has no navbar/client_context.js, so it needs its own session guard rather
// than inheriting loadSessionClient()'s check — no valid session, no client cards.
// The response body is read (not just its status) because it now also carries `username`,
// which is the header greeting's only source.
(async function guardSession() {
    let session;
    try {
        const res = await fetch('/api/auth/current-client');
        if (!res.ok) {
            window.location.href = '/static/login.html';
            return;
        }
        session = await res.json();
    } catch (err) {
        window.location.href = '/static/login.html';
        return;
    }
    renderClientSelectHeader(session.username);
    loadClients();
    checkAdminAndShowGear();
})();
