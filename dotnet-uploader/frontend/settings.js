// ==============================================
// ⚙️ APP SETTINGS LOGIC
// ==============================================

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
    rate_precision: {
        label: 'Rate Precision',
        type: 'select',
        options: [
            { value: '0', label: '0 (e.g., 1,000)' },
            { value: '2', label: '2 (e.g., 1,000.25)' },
            { value: '4', label: '4 (e.g., 1,000.2500)' }
        ],
        default: '0',
        hint: 'Decimal places for the derived/imported Rate column, independent of Decimal Precision (Default).'
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
let currentCashflowDefaults = {};

// The .NET shell supplies this capability for its own page only. Python's
// existing flow is retained; the backend remains the authority on every write.
function canEditTenantSettings() {
    return !window.AppTenantSettings?.enabled || window.AppTenantSettings.canEdit === true;
}

function escapeSettingsHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[character]));
}

function applySettingsReadOnly() {
    if (canEditTenantSettings()) return;
    document.getElementById('settingsReadOnlyNotice').hidden = false;
    document.querySelectorAll('#settingsContainer input, #settingsContainer select, #settingsContainer button, '
        + '#cashflowDefaultsContainer input, #cashflowDefaultsContainer select, #cashflowDefaultsContainer button, '
        + '#holidayDateInput, #holidayLabelInput, #holidayRepeatsYearly, #addHolidayBtn, .delete-holiday-btn')
        .forEach(control => control.disabled = true);
}

function uploadPolicyMessage(required, saved = false) {
    const policy = required ? 'a recovery copy is required before replacement.' : 'uploads can proceed without a separate recovery copy.';
    return `${saved ? 'Saved.' : 'Current setting:'} ${policy}`;
}

async function loadUploadBackupSetting() {
    const panel = document.getElementById('uploadBackupsPanel');
    const toggle = document.getElementById('uploadBackupRequired');
    const save = document.getElementById('saveUploadBackupsBtn');
    const status = document.getElementById('uploadBackupsStatus');
    // Authorization is also checked by the server on every read and save.
    toggle.disabled = save.disabled = true;
    try {
        const response = await fetch(`${API_BASE}/api/settings/upload-backups`);
        if (response.status === 401 || response.status === 403) { panel.hidden = true; return; }
        panel.hidden = false;
        if (!response.ok) throw new Error('Could not load the backup setting. Reload Settings to try again.');
        const policy = await response.json();
        toggle.checked = policy.allow_without_backup !== true;
        toggle.disabled = save.disabled = false;
        status.textContent = uploadPolicyMessage(toggle.checked);
    } catch (error) { panel.hidden = false; status.textContent = error.message; }
}

async function saveUploadBackupSetting() {
    if (!canEditTenantSettings()) return;
    const toggle = document.getElementById('uploadBackupRequired');
    const save = document.getElementById('saveUploadBackupsBtn');
    const status = document.getElementById('uploadBackupsStatus');
    const selected = toggle.checked;
    toggle.disabled = save.disabled = true;
    status.textContent = 'Saving…';
    try {
        const response = await fetch(`${API_BASE}/api/settings/upload-backups`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({allow_without_backup: !selected})
        });
        if (!response.ok) throw new Error('Save was not confirmed. Reload Settings to check the saved choice.');
        const policy = await response.json();
        if (policy.allow_without_backup !== !selected) throw new Error('Save was not confirmed. Reload Settings to check the saved choice.');
        status.textContent = uploadPolicyMessage(selected, true);
    } catch (error) { status.textContent = error.message; }
    finally { toggle.disabled = save.disabled = false; }
}

async function loadCashflowDefaults() {
    try {
        const res = await fetch(`${API_BASE}/api/settings/cashflows-defaults`);
        if (!res.ok) throw new Error('Failed to load Cashflows defaults');
        currentCashflowDefaults = await res.json();
        renderCashflowDefaults();
    } catch (error) {
        showStatus('Error loading Cashflows defaults: ' + error.message, 'error');
    }
}

function renderCashflowDefaults() {
    const container = document.getElementById('cashflowDefaultsContainer');
    if (!container) return;
    const period = currentCashflowDefaults.default_period || 'ytd';
    const custom = period === 'custom';
    container.innerHTML = `
        <div class="settings-group">
            <h3>Reports -&gt; Defaults</h3>
            <div class="setting-row">
                <label for="cashflowFiscalYearStartMonth">Fiscal-year start month</label>
                <select id="cashflowFiscalYearStartMonth">
                    ${Array.from({ length: 12 }, (_, index) => `<option value="${index + 1}" ${Number(currentCashflowDefaults.fiscal_year_start_month || 1) === index + 1 ? 'selected' : ''}>${new Date(2000, index, 1).toLocaleString(undefined, { month: 'long' })}</option>`).join('')}
                </select>
                <span class="hint">Used for This Financial Year and Year to date.</span>
            </div>
            <div class="setting-row">
                <label for="cashflowDefaultPeriod">Default period</label>
                <select id="cashflowDefaultPeriod">
                    <option value="financial_year" ${period === 'financial_year' ? 'selected' : ''}>This financial year</option>
                    <option value="ytd" ${period === 'ytd' ? 'selected' : ''}>Year to date</option>
                    <option value="last_12_months" ${period === 'last_12_months' ? 'selected' : ''}>Last 12 months</option>
                    <option value="custom" ${period === 'custom' ? 'selected' : ''}>Custom period</option>
                </select>
                <span class="hint">Cashflows opens with this period for all users.</span>
            </div>
            <div class="setting-row">
                <label for="cashflowCustomStart">Custom start date</label>
                <input type="date" id="cashflowCustomStart" value="${escapeSettingsHtml(currentCashflowDefaults.custom_start)}" ${custom ? '' : 'disabled'}>
            </div>
            <div class="setting-row">
                <label for="cashflowCustomEnd">Custom end date</label>
                <input type="date" id="cashflowCustomEnd" value="${escapeSettingsHtml(currentCashflowDefaults.custom_end)}" ${custom ? '' : 'disabled'}>
            </div>
            <div style="text-align:right; margin-top:12px;">
                <button class="save-btn" id="saveCashflowDefaultsBtn">Save Report Defaults</button>
            </div>
        </div>
    `;
    document.getElementById('cashflowDefaultPeriod')?.addEventListener('change', toggleCashflowCustomDates);
    document.getElementById('saveCashflowDefaultsBtn')?.addEventListener('click', saveCashflowDefaults);
    applySettingsReadOnly();
}

function toggleCashflowCustomDates() {
    if (!canEditTenantSettings()) return;
    const enabled = document.getElementById('cashflowDefaultPeriod')?.value === 'custom';
    ['cashflowCustomStart', 'cashflowCustomEnd'].forEach(id => {
        const input = document.getElementById(id);
        if (input) input.disabled = !enabled;
    });
}

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
                        <input type="${def.inputType || 'text'}" id="setting_${key}" value="${escapeSettingsHtml(value)}" />
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
    applySettingsReadOnly();
}

async function saveSettings() {
    if (!canEditTenantSettings()) return;
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

async function saveCashflowDefaults() {
    if (!canEditTenantSettings()) return;
    const period = document.getElementById('cashflowDefaultPeriod')?.value || 'ytd';
    const customStart = document.getElementById('cashflowCustomStart')?.value || '';
    const customEnd = document.getElementById('cashflowCustomEnd')?.value || '';
    if (period === 'custom' && (!customStart || !customEnd)) {
        showStatus('Enter both custom start and end dates.', 'error');
        return;
    }
    if (period === 'custom' && customStart > customEnd) {
        showStatus('Custom start date cannot be after custom end date.', 'error');
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/api/settings/cashflows-defaults`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                fiscal_year_start_month: Number(document.getElementById('cashflowFiscalYearStartMonth')?.value || 1),
                default_period: period,
                custom_start: customStart || null,
                custom_end: customEnd || null
            })
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || 'Failed to save Cashflows defaults');
        currentCashflowDefaults = result;
        renderCashflowDefaults();
        showStatus('Cashflows report defaults saved.', 'success');
    } catch (error) {
        showStatus('Error saving Cashflows defaults: ' + error.message, 'error');
    }
}

// ==============================================
// 🗓️ FIRM-WIDE HOLIDAYS (Phase 2 of the Client Data Availability & Coverage Redesign)
// Uses Phase 1's /api/settings/holidays CRUD as-is. Kept fully separate from the
// key-value SETTINGS_DEF/renderSettings()/saveSettings() logic above, since holidays
// are a list of dated entries, not a flat setting.
// ==============================================

let currentHolidays = [];

async function loadHolidays() {
    try {
        const res = await fetch(`${API_BASE}/api/settings/holidays`);
        if (!res.ok) throw new Error('Failed to load holidays');
        currentHolidays = await res.json();
        renderHolidayList();
    } catch (error) {
        showStatus('❌ Error loading holidays: ' + error.message, 'error');
    }
}

function renderHolidayList() {
    const container = document.getElementById('holidayListContainer');
    if (!container) return;

    if (currentHolidays.length === 0) {
        container.innerHTML = '<div class="holiday-empty-hint">No holidays added yet.</div>';
        return;
    }

    container.innerHTML = currentHolidays.map(h => `
        <div class="holiday-row">
            <span class="holiday-date">${escapeSettingsHtml(h.date)}</span>
            <span class="holiday-label">${escapeSettingsHtml(h.label)}</span>
            ${h.repeats_yearly ? '<span class="holiday-yearly-badge">🔁 Yearly</span>' : ''}
            <button class="delete-holiday-btn" data-id="${escapeSettingsHtml(h.id)}" title="Delete holiday">🗑️</button>
        </div>
    `).join('');
    applySettingsReadOnly();
}

async function addHoliday() {
    if (!canEditTenantSettings()) return;
    const dateInput = document.getElementById('holidayDateInput');
    const labelInput = document.getElementById('holidayLabelInput');
    const repeatsYearlyInput = document.getElementById('holidayRepeatsYearly');

    const date = dateInput.value;
    const label = labelInput.value.trim();

    // Fast client-side check before hitting the network - the backend re-validates
    // the same rules regardless, this is just a friendlier first pass.
    if (!date || !label) {
        showStatus('⚠️ Please enter both a date and a label.', 'error');
        return;
    }

    try {
        const res = await fetch(`${API_BASE}/api/settings/holidays`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ date, label, repeats_yearly: repeatsYearlyInput.checked })
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || 'Failed to add holiday');

        // Clear label/checkbox only - date is kept, since holidays are often added
        // in date-adjacent batches (e.g. multiple Eid days in a row).
        labelInput.value = '';
        repeatsYearlyInput.checked = false;
        showStatus(`✅ Added holiday: ${result.label}`, 'success');
        loadHolidays();
    } catch (error) {
        showStatus('❌ Error adding holiday: ' + error.message, 'error');
    }
}

async function deleteHoliday(id) {
    if (!canEditTenantSettings()) return;
    if (!confirm('Delete this holiday?')) return;
    try {
        const res = await fetch(`${API_BASE}/api/settings/holidays/${id}`, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || 'Failed to delete holiday');
        showStatus('✅ Holiday deleted.', 'success');
        loadHolidays();
    } catch (error) {
        showStatus('❌ Error deleting holiday: ' + error.message, 'error');
    }
}

window.onload = async function() {
    if (window.clientContextReady) await window.clientContextReady;
    applySettingsReadOnly();
    loadUploadBackupSetting();
    document.getElementById('saveUploadBackupsBtn')?.addEventListener('click', saveUploadBackupSetting);
    loadSettings();
    loadHolidays();
    loadCashflowDefaults();
    document.getElementById('addHolidayBtn')?.addEventListener('click', addHoliday);
    // Event delegation for delete buttons - #holidayListContainer's rows are rebuilt
    // on every loadHolidays(), so a single listener here avoids rebinding per row.
    document.getElementById('holidayListContainer')?.addEventListener('click', (event) => {
        const btn = event.target.closest('.delete-holiday-btn');
        if (btn) deleteHoliday(btn.dataset.id);
    });
};

// The Admin tab strip's rendering, admin-only gating and click handling used to live here as
// checkAdminAndShowTabs()/setupSettingsTabs(). Both are gone: the strip is now shared across all
// three Admin pages, owned by renderFirmLevelTabs() in client_context.js, which this page reaches
// via its empty #firmTabs container. Nothing tab-related belongs in this file any more.
