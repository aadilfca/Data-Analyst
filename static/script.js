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
            console.log('✅ Dashboard stats loaded:', stats);
        } else {
            // Fallback: try test endpoint
            const testRes = await fetch('/api/test');
            if (testRes.ok) {
                document.getElementById('totalUploads').textContent = '0';
                document.getElementById('pendingVerifications').textContent = '0';
                console.log('✅ Server connected but no stats available');
            } else {
                document.getElementById('totalUploads').textContent = '⚠️';
                document.getElementById('pendingVerifications').textContent = '⚠️';
            }
        }
    } catch (error) {
        console.log('⚠️ Error loading stats:', error.message);
        document.getElementById('totalUploads').textContent = '⚠️';
        document.getElementById('pendingVerifications').textContent = '⚠️';
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