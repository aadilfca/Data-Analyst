// ==============================================
// 📊 DASHBOARD LOGIC
// ==============================================

// ---------- LOAD DASHBOARD STATS ----------
async function loadDashboardStats() {
    try {
        const res = await fetch('/api/dashboard/stats');
        if (res.ok) {
            const stats = await res.json();
            document.getElementById('totalUploads').textContent = stats.total_uploads || 0;
            document.getElementById('pendingVerifications').textContent = stats.pending_verifications || 0;
            document.getElementById('totalRecords').textContent = stats.total_records || 0;
                        const lastUpload = stats.last_upload ? formatDate(stats.last_upload) : 'Never';
            // Add a new element for last upload if not exists; we'll add it in index.html.
            // We'll handle it by updating an optional element.
            const lastUploadEl = document.getElementById('lastUploadDate');
            if (lastUploadEl) {
                lastUploadEl.textContent = lastUpload;
            }
        } else {
            // Fallback
            document.getElementById('totalUploads').textContent = '0';
            document.getElementById('pendingVerifications').textContent = '0';
            document.getElementById('totalRecords').textContent = '0';
            const lastUploadEl = document.getElementById('lastUploadDate');
            if (lastUploadEl) lastUploadEl.textContent = 'Never';
        }
    } catch (error) {
        document.getElementById('totalUploads').textContent = '⚠️';
        document.getElementById('pendingVerifications').textContent = '⚠️';
        document.getElementById('totalRecords').textContent = '⚠️';
        const lastUploadEl = document.getElementById('lastUploadDate');
        if (lastUploadEl) lastUploadEl.textContent = '⚠️';
    }
}

// ---------- MODULE CARD NAVIGATION ----------
// Every navigating dashboard card is wired here by id (no inline onclick in index.html).
// Verification deliberately has no ?type= param — the verification hub itself is the
// Cash-vs-Bank chooser. "Coming Soon" cards are inert by design: no entry in this map,
// no listener at all.
const MODULE_CARD_DESTINATIONS = {
    verificationCard: '/static/verification.html',
    uploadCard: '/static/upload.html',
    dataBankCard: '/static/databank.html',
    trialBalanceCard: '/static/trial_balance.html',
    setupCard: '/static/setup.html',
    manageClientsCard: '/static/clients.html',
    auditRulesCard: '/static/audit_rules.html'
};
Object.entries(MODULE_CARD_DESTINATIONS).forEach(([cardId, destination]) => {
    document.getElementById(cardId).addEventListener('click', function() {
        window.location.href = destination;
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
};

// ---------- KEYBOARD SHORTCUTS ----------
document.addEventListener('keydown', function(e) {
    if (e.key === 'r' || e.key === 'R') {
        loadDashboardStats();
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