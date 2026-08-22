// ==============================================
// 🗂️ CLIENT CARDS LANDING PAGE
// ==============================================
// Standalone page (no client_context.js/navbar — no client is selected yet), so this file
// defines its own minimal fetch call rather than relying on client_context.js's global API_BASE.
// Selection is written to localStorage under the same keys client_context.js already
// reads/writes (selectedClientId/selectedClientName/selectedClientCode) so the dashboard and
// every other client_context.js-driven page picks it up with no changes on their end. This is a
// temporary client-side handoff — true session-backed selection is a later phase.

const STORAGE_KEY = 'selectedClientId';
const STORAGE_NAME = 'selectedClientName';
const STORAGE_CODE = 'selectedClientCode';

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

function selectClient(client) {
    localStorage.setItem(STORAGE_KEY, client.id);
    localStorage.setItem(STORAGE_NAME, client.client_name);
    localStorage.setItem(STORAGE_CODE, client.client_code);
    window.location.href = '/static/index.html';
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

loadClients();
