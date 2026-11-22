// ==============================================
// 📊 DASHBOARD LOGIC
// ==============================================

// Stored labels remain text when a table uses an HTML template.
function dashboardEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ---------- LOAD DASHBOARD STATS ----------
async function loadDashboardStats() {
    if (window.clientContextReady) await window.clientContextReady;
    try {
        const res = await window.ApiUtils.apiFetch('/api/dashboard/stats');
        if (res.ok) {
            const stats = await res.json();
            const lastUpload = stats.last_upload ? formatDate(stats.last_upload) : 'Never';
            const lastUploadEl = document.getElementById('lastUploadDate');
            if (lastUploadEl) {
                lastUploadEl.textContent = lastUpload;
            }
        } else {
            const lastUploadEl = document.getElementById('lastUploadDate');
            if (lastUploadEl) lastUploadEl.textContent = 'Unavailable';
        }
    } catch (error) {
        const lastUploadEl = document.getElementById('lastUploadDate');
        if (lastUploadEl) lastUploadEl.textContent = '⚠️';
    }
}

// ---------- LOAD QUICK OVERVIEW KPI STATS (Phase B2) ----------
async function loadKpiStats() {
    if (window.clientContextReady) await window.clientContextReady;
    try {
        const res = await window.ApiUtils.apiFetch('/api/dashboard/kpi-stats');
        if (res.ok) {
            const data = await res.json();
            document.getElementById('openQueries').textContent = data.open_queries ?? 0;
            document.getElementById('verificationProgress').textContent = `${data.verification_progress_pct ?? 0}%`;
            document.getElementById('unresolvedErrors').textContent = data.unresolved_errors ?? 'Unavailable';
        } else {
            document.getElementById('openQueries').textContent = 'Unavailable';
            document.getElementById('verificationProgress').textContent = 'Unavailable';
            document.getElementById('unresolvedErrors').textContent = 'Unavailable';
        }
    } catch (error) {
        document.getElementById('openQueries').textContent = '⚠️';
        document.getElementById('verificationProgress').textContent = '⚠️';
        document.getElementById('unresolvedErrors').textContent = '⚠️';
    }
}

// ---------- LOAD OVERDUE UPLOADS (Overdue Uploads, Phase D2, FINAL) ----------
// All-clients list from routers/data.py's compute_overdue_uploads(), already
// sorted most-overdue-first by the backend — no client-side re-sort needed.
// Row-click-to-jump-to-upload-page is deliberately deferred (not built this phase).
let overdueUploadsData = [];
// true = last fetch failed (status/network); distinct from a genuinely empty result,
// so the render function can show an error state instead of "No overdue uploads."
let overdueUploadsError = false;

function getOverdueSeverityBadge(daysOverdue) {
    // Exact thresholds left to implementation per the brief — a fixed cutoff
    // reusing the app's existing .status-badge.pending (amber) / .failed (red) pair.
    const cls = daysOverdue >= 5 ? 'failed' : 'pending';
    return `<span class="status-badge ${cls}">${daysOverdue}</span>`;
}

function renderOverdueUploadsTable() {
    const tbody = document.getElementById('overdueUploadsTableBody');
    const noData = document.getElementById('overdueUploadsNoData');
    if (!tbody) return;
    if (overdueUploadsError) {
        tbody.innerHTML = '';
        if (noData) {
            noData.style.display = 'block';
            noData.textContent = '⚠️ Couldn\'t load overdue uploads — try again.';
        }
        return;
    }
    if (overdueUploadsData.length === 0) {
        tbody.innerHTML = '';
        if (noData) {
            noData.style.display = 'block';
            noData.textContent = '✅ No overdue uploads.';
        }
        return;
    }
    if (noData) noData.style.display = 'none';
    tbody.innerHTML = overdueUploadsData.map(item => `
        <tr>
            <td style="padding:12px 16px; border-bottom:1px solid #f0f2f5;">${dashboardEscape(item.client_name)}</td>
            <td style="padding:12px 16px; border-bottom:1px solid #f0f2f5;">${dashboardEscape(item.template_name)}</td>
            <td style="padding:12px 16px; border-bottom:1px solid #f0f2f5;">${dashboardEscape(formatDate(item.last_upload_date))}</td>
            <td style="padding:12px 16px; border-bottom:1px solid #f0f2f5;">${dashboardEscape(item.frequency)}</td>
            <td style="padding:12px 16px; border-bottom:1px solid #f0f2f5;">${getOverdueSeverityBadge(item.days_overdue)}</td>
        </tr>
    `).join('');
}

async function loadOverdueUploads() {
    if (window.clientContextReady) await window.clientContextReady;
    overdueUploadsError = false;
    try {
        const res = await window.ApiUtils.apiFetch('/api/dashboard/overdue-uploads');
        if (res.ok) {
            const data = await res.json();
            overdueUploadsData = data.overdue || [];
        } else {
            overdueUploadsData = [];
            overdueUploadsError = true;
        }
    } catch (error) {
        overdueUploadsData = [];
        overdueUploadsError = true;
    }
    const countEl = document.getElementById('overdueUploadsCount');
    if (countEl) countEl.textContent = overdueUploadsError ? '⚠️' : overdueUploadsData.length;
    renderOverdueUploadsTable();
}

function toggleOverdueUploadsPanel() {
    const panel = document.getElementById('overdueUploadsPanel');
    if (!panel) return;
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

// ---------- LOAD DATABASE SIZE (Database Size Visibility, Phase A2) ----------
async function loadDbSize() {
    if (window.clientContextReady) await window.clientContextReady;
    const value = document.getElementById('dbSize');
    if (!value) return;
    value.title = 'Database files include current records, indexes, other modules and space available for reuse. Deleting records may not reduce this number immediately.';
    try {
        const res = await window.ApiUtils.apiFetch('/api/dashboard/db-size');
        if (res.ok) {
            const data = await res.json();
            value.textContent = data.formatted || 'Unavailable';
        } else {
            value.textContent = 'Unavailable';
        }
    } catch (error) {
        value.textContent = 'Unavailable';
    }
}

// ---------- LOAD SELECTED CLIENT'S DATA INTEGRITY STATUS ----------
let dataIntegritySummaryRequest = 0;
async function loadDataIntegritySummary() {
    const statusEl = document.getElementById('dataIntegrityStatus');
    if (!statusEl) return;
    const request = ++dataIntegritySummaryRequest;
    try {
        if (window.clientContextReady) await window.clientContextReady;
        if (request !== dataIntegritySummaryRequest) return;
        const clientId = typeof getSelectedClientId === 'function' ? getSelectedClientId() : null;
        if (!clientId) {
            statusEl.textContent = 'Select client';
            return;
        }
        const response = await window.ApiUtils.apiFetch(
            `/api/data-integrity/overview?client_id=${encodeURIComponent(clientId)}`
        );
        if (!response.ok) throw new Error('Data check unavailable');
        const data = await response.json();
        if (request !== dataIntegritySummaryRequest || Number(getSelectedClientId()) !== Number(clientId)) return;
        const status = String(data.overall_status || 'Not checked');
        statusEl.textContent = status.startsWith('Review') ? 'Review' : status;
        statusEl.title = `Data Integrity for ${data.client_name || 'selected client'}: ${status}`;
    } catch (error) {
        if (request !== dataIntegritySummaryRequest) return;
        statusEl.textContent = 'Unavailable';
        statusEl.title = 'Data Integrity check unavailable';
    }
}

// ---------- MODULE CARD NAVIGATION ----------
// Every navigating dashboard card is wired here by id (no inline onclick in index.html).
// Verification deliberately has no ?type= param — the verification hub itself is the
// Cash-vs-Bank chooser and Reports are wired here. Genuinely unavailable cards have no entry.
// A destination may be a plain URL string or a function returning one, for a card whose target can
// only be resolved at click time — Client Settings needs the session's ?client_id= appended, so it
// delegates to getClientSettingsUrl() (client_context.js), the same helper NAV_MANIFEST uses.
const MODULE_CARD_DESTINATIONS = {
    verificationCard: '/static/verification.html',
    reportsCard: '/static/reconciliation.html?view=reports',
    uploadCard: '/static/upload.html',
    dataBankCard: '/static/databank.html',
    trialBalanceCard: '/static/trial_balance.html',
    setupCard: '/static/setup.html',
    auditRulesCard: '/static/audit_rules.html',
    clientSettingsCard: () => getClientSettingsUrl()
};

let dashboardNavigationInFlight = false;
let dashboardNavigationToken = null;
let dashboardNavigationCard = null;
let dashboardNavigationOriginalCursor = '';

function releaseDashboardNavigation() {
    if (!dashboardNavigationInFlight) return;
    if (dashboardNavigationCard) {
        dashboardNavigationCard.style.cursor = dashboardNavigationOriginalCursor;
        dashboardNavigationCard.removeAttribute('aria-busy');
    }
    if (dashboardNavigationToken) {
        window.LoadingUI.stopGlobalLoading(dashboardNavigationToken);
    }
    dashboardNavigationCard = null;
    dashboardNavigationToken = null;
    dashboardNavigationOriginalCursor = '';
    dashboardNavigationInFlight = false;
}

Object.entries(MODULE_CARD_DESTINATIONS).forEach(([cardId, destination]) => {
    const card = document.getElementById(cardId);
    if (!card) return;
    card.addEventListener('click', function() {
        if (dashboardNavigationInFlight) return;

        dashboardNavigationInFlight = true;
        dashboardNavigationCard = this;
        dashboardNavigationOriginalCursor = this.style.cursor;
        this.style.cursor = 'progress';
        this.setAttribute('aria-busy', 'true');
        dashboardNavigationToken = window.LoadingUI.startGlobalLoading();

        try {
            window.location.href = typeof destination === 'function' ? destination() : destination;
        } catch (error) {
            releaseDashboardNavigation();
            throw error;
        }
    });
});

// A normal navigation replaces this document. These recovery paths cover the cases where
// the old dashboard survives instead (browser Stop/Escape or bfcache restoration).
document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape') releaseDashboardNavigation();
});
window.addEventListener('pageshow', function(event) {
    if (event.persisted) releaseDashboardNavigation();
});

// ---------- ADD CLICK FEEDBACK TO MODULE CARDS ----------
// Scoped to exclude .coming-soon cards — inert cards must not visibly react to clicks.
document.querySelectorAll('.module-card:not(.coming-soon)').forEach(card => {
    card.addEventListener('click', function() {
        this.style.transform = 'scale(0.95)';
        setTimeout(() => {
            this.style.transform = '';
        }, 150);
    });
});

// ---------- LOAD ON PAGE OPEN ----------
window.onload = function() {
    loadDashboardStats();
    loadDbSize();
    loadKpiStats();
    loadOverdueUploads();
    loadDataIntegritySummary();
    const overdueCard = document.getElementById('overdueUploadsCard');
    if (overdueCard) overdueCard.addEventListener('click', toggleOverdueUploadsPanel);
};

// ---------- KEYBOARD SHORTCUTS ----------
document.addEventListener('keydown', function(e) {
    if (e.key === 'r' || e.key === 'R') {
        loadDashboardStats();
        loadDbSize();
        loadKpiStats();
        loadOverdueUploads();
        loadDataIntegritySummary();
    }
});

// ---------- CLOSE MODALS WITH ESC KEY (Global) ----------
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        // Close Column Modal (if open)
        const columnModal = document.getElementById('columnModal');
        if (columnModal && columnModal.style.display === 'block') {
            const closeBtn = document.getElementById('cancelColumnBtn');
            if (closeBtn) closeBtn.click();
            return;
        }

        // Close Template Modal (if open)
        const templateModal = document.getElementById('templateModal');
        if (templateModal && templateModal.style.display === 'block') {
            const closeBtn = document.getElementById('cancelTemplateBtn');
            if (closeBtn) closeBtn.click();
            return;
        }

        // Close Preview Modal (if open – dynamic)
        const previewModal = document.getElementById('previewModal');
        if (previewModal) {
            const closeBtn = document.getElementById('closePreviewBtn');
            if (closeBtn) closeBtn.click();
            return;
        }

        // Close Upload Popup (if open)
        const popupWindow = document.getElementById('popupWindow');
        if (popupWindow && popupWindow.style.display === 'block') {
            const closeBtn = document.querySelector('#popupWindow button[onclick*="closePopup"]');
            if (closeBtn) closeBtn.click();
            return;
        }
    }
});
