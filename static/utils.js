// ==============================================
// 📦 UTILITY FUNCTIONS (with global settings)
// ==============================================

const API_BASE = 'http://localhost:5000';

// Cache settings to avoid repeated API calls
let cachedSettings = null;

async function fetchSettings() {
    if (cachedSettings) return cachedSettings;
    try {
        const res = await fetch(`${API_BASE}/api/settings`);
        if (!res.ok) throw new Error('Failed to fetch settings');
        const data = await res.json();
        cachedSettings = data;
        return data;
    } catch (e) {
        console.warn('⚠️ Using default settings:', e.message);
        // Fallback defaults
        cachedSettings = {
            date_format: 'DD/MM/YYYY',
            amount_grouping: '000',
            decimal_precision: '0'
        };
        return cachedSettings;
    }
}

// ---------- DATE FORMAT ----------
function formatDate(dateString, formatOverride = null) {
    if (!dateString) return '—';
    try {
        const date = new Date(dateString);
        if (isNaN(date.getTime())) return dateString;
        const day = String(date.getDate()).padStart(2, '0');
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const monthShort = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][date.getMonth()];
        const year = date.getFullYear();
        const format = formatOverride || (cachedSettings ? cachedSettings.date_format : 'DD/MM/YYYY');
        switch (format) {
            case 'DD/MM/YYYY': return `${day}/${month}/${year}`;
            case 'MM/DD/YYYY': return `${month}/${day}/${year}`;
            case 'DD/MMM/YYYY': return `${day}/${monthShort}/${year}`;
            default: return `${day}/${month}/${year}`;
        }
    } catch (e) {
        return dateString;
    }
}

// ---------- AMOUNT FORMAT ----------
function formatAmount(value, decimals = null, grouping = null) {
    if (value === undefined || value === null || isNaN(value)) return '—';
    let num = typeof value === 'string' ? parseFloat(value) : value;
    if (isNaN(num)) return '—';

    const settings = cachedSettings || {};
    const dec = decimals !== null ? decimals : parseInt(settings.decimal_precision) || 0;
    const group = grouping || settings.amount_grouping || '000';

    // Round to the specified decimals
    const rounded = Number(num.toFixed(dec));
    const fixed = rounded.toFixed(dec);

    // Apply grouping
    let parts = fixed.split('.');
    let intPart = parts[0];
    let decPart = parts[1] !== undefined ? '.' + parts[1] : '';

    if (group === 'Lacs') {
        // Lacs grouping: 1,00,000 (Indian numbering)
        // Convert to string and apply lacs grouping: first group = 3 digits, then 2 digits
        let str = intPart;
        let result = '';
        let len = str.length;
        if (len <= 3) {
            result = str;
        } else {
            // last 3 digits
            result = str.slice(-3);
            let remaining = str.slice(0, len - 3);
            while (remaining.length > 0) {
                let groupLen = remaining.length > 2 ? 2 : remaining.length;
                result = remaining.slice(-groupLen) + ',' + result;
                remaining = remaining.slice(0, remaining.length - groupLen);
            }
        }
        return result + decPart;
    } else {
        // Thousands grouping (default)
        return Number(rounded).toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec });
    }
}

// ---------- LOAD SETTINGS ONCE ----------
// This function should be called on page load to pre-fetch settings
async function initUtils() {
    await fetchSettings();
}

window.formatDate = formatDate;
window.formatAmount = formatAmount;
window.initUtils = initUtils;

console.log('📦 Utility functions loaded');