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

async function selectClient(client) {
    try {
        const res = await fetch('/api/auth/select-client', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ client_id: client.id }),
        });
        if (!res.ok) throw new Error('Failed to select client');
        window.location.href = '/static/index.html';
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

// This page has no navbar/client_context.js, so it needs its own session guard rather
// than inheriting loadSessionClient()'s check — no valid session, no client cards.
(async function guardSession() {
    try {
        const res = await fetch('/api/auth/current-client');
        if (!res.ok) {
            window.location.href = '/static/login.html';
            return;
        }
    } catch (err) {
        window.location.href = '/static/login.html';
        return;
    }
    loadClients();
})();
