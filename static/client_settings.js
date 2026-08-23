// ==============================================
// ⚙️ CLIENT SETTINGS — VOUCHER PREFIX CONFIG LOGIC
// ==============================================

// API_BASE is defined globally in client_context.js

// key   - category name exactly as stored/returned by the server (client_voucher_prefixes
//         .category / GET response keys), e.g. "Cash Payment". Contains spaces.
// field - Python-identifier-safe field name expected by VoucherPrefixesUpdate on PUT
//         (routers/clients.py), e.g. "cash_payment".
// id    - id-safe prefix used to build this category's DOM element ids, e.g. "cashPayment".
const CATEGORIES = [
    { key: 'Cash Payment', field: 'cash_payment', id: 'cashPayment' },
    { key: 'Cash Receipt', field: 'cash_receipt', id: 'cashReceipt' },
    { key: 'Bank Payment', field: 'bank_payment', id: 'bankPayment' },
    { key: 'Bank Receipt', field: 'bank_receipt', id: 'bankReceipt' },
    { key: 'Journal', field: 'journal', id: 'journal' },
];

// In-memory prefix state per category (keyed by category.key), populated by
// loadClientSettings() and mutated by addPrefix()/removePrefix() — sent to the
// server only on Save.
const prefixState = {};
CATEGORIES.forEach(category => { prefixState[category.key] = []; });

const clientNameHeading = document.getElementById('clientNameHeading');
const validationMsgDiv = document.getElementById('prefixValidationMessage');
const settingsStatusDiv = document.getElementById('settingsStatus');
const saveBtn = document.getElementById('saveSettingsBtn');

let clientId = null;

// ---------- DOM REFS PER CATEGORY (keyed by category.id, not category.key — ids can't
// contain the spaces that category.key does) ----------
function listContainer(id) {
    return document.getElementById(id + 'PrefixList');
}
function inputEl(id) {
    return document.getElementById(id + 'PrefixInput');
}
function addBtnEl(id) {
    return document.getElementById(id + 'AddBtn');
}

// ---------- STATUS MESSAGES ----------
function showSettingsStatus(message, type) {
    settingsStatusDiv.textContent = message;
    settingsStatusDiv.className = 'upload-status ' + type;
    settingsStatusDiv.style.display = 'block';
    clearTimeout(window.settingsStatusTimeout);
    window.settingsStatusTimeout = setTimeout(() => {
        settingsStatusDiv.style.display = 'none';
    }, 5000);
}

function showValidationMessage(message) {
    validationMsgDiv.textContent = message;
    validationMsgDiv.style.display = 'block';
}

function clearValidationMessage() {
    validationMsgDiv.style.display = 'none';
    validationMsgDiv.textContent = '';
}

// ---------- RENDER A CATEGORY'S CHIP LIST ----------
function renderPrefixList(category) {
    const container = listContainer(category.id);
    container.innerHTML = '';

    const prefixes = prefixState[category.key];
    if (prefixes.length === 0) {
        const empty = document.createElement('span');
        empty.className = 'prefix-chip-empty';
        empty.textContent = 'No prefixes configured.';
        container.appendChild(empty);
        return;
    }

    prefixes.forEach(prefix => {
        const chip = document.createElement('span');
        chip.className = 'prefix-chip';

        const label = document.createElement('span');
        label.textContent = prefix;
        chip.appendChild(label);

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'prefix-chip-remove';
        removeBtn.textContent = '✕';
        removeBtn.title = 'Remove prefix';
        removeBtn.addEventListener('click', () => removePrefix(category, prefix));
        chip.appendChild(removeBtn);

        container.appendChild(chip);
    });
}

function renderAllPrefixLists() {
    CATEGORIES.forEach(renderPrefixList);
}

// ---------- ADD / REMOVE ----------
function addPrefix(category) {
    const input = inputEl(category.id);
    const value = input.value.trim();
    if (!value) return;

    const lowerValue = value.toLowerCase();

    // Already present in this same category — nothing to do.
    if (prefixState[category.key].some(p => p.toLowerCase() === lowerValue)) {
        input.value = '';
        return;
    }

    // Cross-category duplicate check (server enforces this too — this is for
    // immediate feedback per the confirmed brief).
    const otherCategories = CATEGORIES.filter(c => c.key !== category.key);
    for (const otherCategory of otherCategories) {
        if (prefixState[otherCategory.key].some(p => p.toLowerCase() === lowerValue)) {
            showValidationMessage(
                `"${value}" is already used under ${otherCategory.key} — a prefix can only belong to one category.`
            );
            return;
        }
    }

    prefixState[category.key].push(value);
    input.value = '';
    clearValidationMessage();
    renderPrefixList(category);
}

function removePrefix(category, prefix) {
    prefixState[category.key] = prefixState[category.key].filter(p => p !== prefix);
    renderPrefixList(category);
}

// ---------- LOAD ----------
async function loadClientSettings() {
    const params = new URLSearchParams(window.location.search);
    const idParam = params.get('client_id');

    if (!idParam || isNaN(parseInt(idParam, 10))) {
        clientNameHeading.textContent = 'No client specified.';
        showSettingsStatus('❌ No client_id was provided in the page URL.', 'error');
        saveBtn.disabled = true;
        return;
    }
    clientId = parseInt(idParam, 10);

    try {
        const [clientRes, prefixesRes] = await Promise.all([
            fetch(API_BASE + '/api/clients/' + clientId),
            fetch(API_BASE + '/api/clients/' + clientId + '/voucher-prefixes')
        ]);

        if (!clientRes.ok) throw new Error('Client not found');
        if (!prefixesRes.ok) throw new Error('Failed to load voucher prefixes');

        const client = await clientRes.json();
        const prefixes = await prefixesRes.json();

        clientNameHeading.textContent = 'Voucher prefix configuration for ' + client.client_name;

        CATEGORIES.forEach(category => {
            prefixState[category.key] = prefixes[category.key] || [];
        });
        renderAllPrefixLists();
    } catch (error) {
        clientNameHeading.textContent = 'Unable to load client settings.';
        showSettingsStatus('❌ Error: ' + error.message, 'error');
        saveBtn.disabled = true;
    }
}

// ---------- SAVE ----------
async function saveSettings() {
    try {
        window.LoadingUI.showSpinner(saveBtn, 'Saving');

        // Server expects VoucherPrefixesUpdate field names (category.field, e.g.
        // "cash_payment"), not the display category names (category.key) prefixState is
        // keyed by — those contain spaces and aren't valid Pydantic field names.
        const payload = {};
        CATEGORIES.forEach(category => {
            payload[category.field] = prefixState[category.key];
        });

        const res = await fetch(API_BASE + '/api/clients/' + clientId + '/voucher-prefixes', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const result = await res.json();

        if (!res.ok) {
            throw new Error(result.detail || result.message || 'Failed to save voucher prefixes');
        }

        showSettingsStatus('✅ ' + result.message, 'success');
    } catch (error) {
        showSettingsStatus('❌ Error: ' + error.message, 'error');
    } finally {
        window.LoadingUI.hideSpinner(saveBtn);
    }
}

// ---------- WIRE UP EVENT LISTENERS ----------
CATEGORIES.forEach(category => {
    addBtnEl(category.id).addEventListener('click', () => addPrefix(category));
    inputEl(category.id).addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            addPrefix(category);
        }
    });
});

saveBtn.addEventListener('click', saveSettings);

// ---------- INIT ----------
window.onload = function() {
    loadClientSettings();
};
