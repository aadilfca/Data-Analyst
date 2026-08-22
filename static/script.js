// ==============================================
// 📊 DASHBOARD LOGIC
// ==============================================

// ---------- LOAD DASHBOARD STATS ----------
async function loadDashboardStats() {
    try {
        const res = await fetch('/api/dashboard/stats');
        if (res.ok) {
            const stats = await res.json();
            const lastUpload = stats.last_upload ? formatDate(stats.last_upload) : 'Never';
            const lastUploadEl = document.getElementById('lastUploadDate');
            if (lastUploadEl) {
                lastUploadEl.textContent = lastUpload;
            }
        } else {
            const lastUploadEl = document.getElementById('lastUploadDate');
            if (lastUploadEl) lastUploadEl.textContent = 'Never';
        }
    } catch (error) {
        const lastUploadEl = document.getElementById('lastUploadDate');
        if (lastUploadEl) lastUploadEl.textContent = '⚠️';
    }
}

// ---------- LOAD QUICK OVERVIEW KPI STATS (Phase B2) ----------
async function loadKpiStats() {
    try {
        const res = await fetch('/api/dashboard/kpi-stats');
        if (res.ok) {
            const data = await res.json();
            document.getElementById('openQueries').textContent = data.open_queries ?? 0;
            document.getElementById('verificationProgress').textContent = `${data.verification_progress_pct ?? 0}%`;
            document.getElementById('unresolvedErrors').textContent = data.unresolved_errors ?? 0;
        } else {
            document.getElementById('openQueries').textContent = '0';
            document.getElementById('verificationProgress').textContent = '0%';
            document.getElementById('unresolvedErrors').textContent = '0';
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
    if (overdueUploadsData.length === 0) {
        tbody.innerHTML = '';
        if (noData) noData.style.display = 'block';
        return;
    }
    if (noData) noData.style.display = 'none';
    tbody.innerHTML = overdueUploadsData.map(item => `
        <tr>
            <td style="padding:12px 16px; border-bottom:1px solid #f0f2f5;">${item.client_name}</td>
            <td style="padding:12px 16px; border-bottom:1px solid #f0f2f5;">${item.template_name}</td>
            <td style="padding:12px 16px; border-bottom:1px solid #f0f2f5;">${formatDate(item.last_upload_date)}</td>
            <td style="padding:12px 16px; border-bottom:1px solid #f0f2f5;">${item.frequency}</td>
            <td style="padding:12px 16px; border-bottom:1px solid #f0f2f5;">${getOverdueSeverityBadge(item.days_overdue)}</td>
        </tr>
    `).join('');
}

async function loadOverdueUploads() {
    try {
        const res = await fetch('/api/dashboard/overdue-uploads');
        if (res.ok) {
            const data = await res.json();
            overdueUploadsData = data.overdue || [];
        } else {
            overdueUploadsData = [];
        }
    } catch (error) {
        overdueUploadsData = [];
    }
    const countEl = document.getElementById('overdueUploadsCount');
    if (countEl) countEl.textContent = overdueUploadsData.length;
    renderOverdueUploadsTable();
}

function toggleOverdueUploadsPanel() {
    const panel = document.getElementById('overdueUploadsPanel');
    if (!panel) return;
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

// ---------- LOAD DATABASE SIZE (Database Size Visibility, Phase A2) ----------
async function loadDbSize() {
    try {
        const res = await fetch('/api/dashboard/db-size');
        if (res.ok) {
            const data = await res.json();
            document.getElementById('dbSize').textContent = data.formatted || '0 B';
        } else {
            document.getElementById('dbSize').textContent = '0 B';
        }
    } catch (error) {
        document.getElementById('dbSize').textContent = '⚠️';
    }
}

// ---------- MODULE CARD NAVIGATION ----------
// Every navigating dashboard card is wired here by id (no inline onclick in index.html).
// Verification deliberately has no ?type= param — the verification hub itself is the
// Cash-vs-Bank chooser. "Coming Soon" cards are inert by design: no entry in this map,
// no listener at all.
// A destination may be a plain URL string or a function returning one, for a card whose target can
// only be resolved at click time — Client Settings needs the session's ?client_id= appended, so it
// delegates to getClientSettingsUrl() (client_context.js), the same helper NAV_MANIFEST uses.
const MODULE_CARD_DESTINATIONS = {
    verificationCard: '/static/verification.html',
    uploadCard: '/static/upload.html',
    dataBankCard: '/static/databank.html',
    trialBalanceCard: '/static/trial_balance.html',
    setupCard: '/static/setup.html',
    auditRulesCard: '/static/audit_rules.html',
    clientSettingsCard: () => getClientSettingsUrl()
};
Object.entries(MODULE_CARD_DESTINATIONS).forEach(([cardId, destination]) => {
    document.getElementById(cardId).addEventListener('click', function() {
        window.location.href = typeof destination === 'function' ? destination() : destination;
    });
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