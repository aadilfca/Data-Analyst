// ==============================================
// ⚙️ APP SETTINGS LOGIC
// ==============================================

const API_BASE = 'http://localhost:5000';

const SETTINGS_DEF = {
    date_format: {
        label: 'Date Format',
        type: 'select',
        options: [
            { value: 'DD/MM/YYYY', label: 'DD/MM/YYYY (e.g., 15/01/2025)' },
            { value: 'MM/DD/YYYY', label: 'MM/DD/YYYY (e.g., 01/15/2025)' },
            { value: 'DD/MMM/YYYY', label: 'DD/MMM/YYYY (e.g., 15/Jan/2025)' }
        ],
        default: 'DD/MM/YYYY',
        hint: 'How dates are displayed throughout the app.'
    },
    amount_grouping: {
        label: 'Amount Grouping',
        type: 'select',
        options: [
            { value: '000', label: '000 (e.g., 1,000,000)' },
            { value: 'Lacs', label: 'Lacs (e.g., 10,00,000)' }
        ],
        default: '000',
        hint: 'How thousands are separated in numbers.'
    },
    decimal_precision: {
        label: 'Decimal Precision (Default)',
        type: 'select',
        options: [
            { value: '0', label: '0 (e.g., 1,000)' },
            { value: '2', label: '2 (e.g., 1,000.25)' },
            { value: '4', label: '4 (e.g., 1,000.2500)' }
        ],
        default: '0',
        hint: 'Default number of decimal places for amounts.'
    },
    default_date_range: {
        label: 'Default Date Range for Dashboard',
        type: 'select',
        options: [
            { value: 'Current Month', label: 'Current Month' },
            { value: 'Current Quarter', label: 'Current Quarter' },
            { value: 'Current Year', label: 'Current Year' }
        ],
        default: 'Current Month',
        hint: 'Preselected date range on the main dashboard.'
    },
    materiality_threshold: {
        label: 'Materiality Threshold',
        type: 'input',
        inputType: 'number',
        default: '50000',
        hint: 'Transactions above this amount are flagged for review.'
    }
};

let currentSettings = {};

async function loadSettings() {
    try {
        const res = await fetch(`${API_BASE}/api/settings`);
        if (!res.ok) throw new Error('Failed to load settings');
        const data = await res.json();
        currentSettings = data;
        renderSettings();
    } catch (error) {
        showStatus('❌ Error loading settings: ' + error.message, 'error');
    }
}

function renderSettings() {
    const container = document.getElementById('settingsContainer');
    if (!container) return;
    let html = '';
    for (const [key, def] of Object.entries(SETTINGS_DEF)) {
        const value = currentSettings[key] || def.default;
        html += `
            <div class="settings-group">
                <h3>${def.label}</h3>
                <div class="setting-row">
                    <label>${def.label}</label>
                    ${def.type === 'select' ? `
                        <select id="setting_${key}">
                            ${def.options.map(opt => `<option value="${opt.value}" ${opt.value === value ? 'selected' : ''}>${opt.label}</option>`).join('')}
                        </select>
                    ` : `
                        <input type="${def.inputType || 'text'}" id="setting_${key}" value="${value}" />
                    `}
                    <span class="hint">${def.hint}</span>
                </div>
            </div>
        `;
    }
    html += `
        <div style="text-align:right; margin-top:20px;">
            <button class="save-btn" id="saveSettingsBtn">💾 Save All Settings</button>
        </div>
    `;
    container.innerHTML = html;
    document.getElementById('saveSettingsBtn')?.addEventListener('click', saveSettings);
}

async function saveSettings() {
    const updates = [];
    for (const key of Object.keys(SETTINGS_DEF)) {
        const el = document.getElementById(`setting_${key}`);
        if (!el) continue;
        const newValue = el.value;
        if (newValue !== currentSettings[key]) {
            updates.push({ key, value: newValue });
        }
    }
    if (updates.length === 0) {
        showStatus('ℹ️ No changes to save.', 'info');
        return;
    }
    try {
        for (const update of updates) {
            const res = await fetch(`${API_BASE}/api/settings/${update.key}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ value: update.value })
            });
            if (!res.ok) throw new Error(`Failed to update ${update.key}`);
        }
        showStatus('✅ All settings saved successfully!', 'success');
        loadSettings(); // reload to reflect changes
    } catch (error) {
        showStatus('❌ Error saving settings: ' + error.message, 'error');
    }
}

function showStatus(msg, type) {
    const statusDiv = document.getElementById('statusMessage');
    statusDiv.textContent = msg;
    statusDiv.className = 'upload-status ' + type;
    statusDiv.style.display = 'block';
    setTimeout(() => {
        statusDiv.style.display = 'none';
    }, 5000);
}

window.onload = function() {
    loadSettings();
    console.log('⚙️ Settings module loaded');
};