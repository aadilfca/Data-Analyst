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
            console.log('✅ Dashboard stats loaded:', stats);
        } else {
            // Fallback
            document.getElementById('totalUploads').textContent = '0';
            document.getElementById('pendingVerifications').textContent = '0';
            document.getElementById('totalRecords').textContent = '0';
            const lastUploadEl = document.getElementById('lastUploadDate');
            if (lastUploadEl) lastUploadEl.textContent = 'Never';
        }
    } catch (error) {
        console.log('⚠️ Error loading stats:', error.message);
        document.getElementById('totalUploads').textContent = '⚠️';
        document.getElementById('pendingVerifications').textContent = '⚠️';
        document.getElementById('totalRecords').textContent = '⚠️';
        const lastUploadEl = document.getElementById('lastUploadDate');
        if (lastUploadEl) lastUploadEl.textContent = '⚠️';
    }
}

// ---------- ADD CLICK FEEDBACK TO MODULE CARDS ----------
document.querySelectorAll('.module-card').forEach(card => {
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
    console.log('🏢 System Analyst Dashboard loaded successfully!');
};

// ---------- KEYBOARD SHORTCUTS ----------
document.addEventListener('keydown', function(e) {
    if (e.key === 'r' || e.key === 'R') {
        loadDashboardStats();
        console.log('🔄 Stats refreshed');
    }
});

console.log('📊 Dashboard ready. Press "R" to refresh stats.');

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
console.log('🔑 ESC key handler enabled');