// ---------- LOAD DASHBOARD STATS ----------
async function loadDashboardStats() {
    try {
        // Try to fetch items count from the test endpoint
        const res = await fetch('/api/test');
        if (res.ok) {
            const data = await res.json();
            document.getElementById('totalItems').textContent = '0'; // Default for now
            document.getElementById('activeModules').textContent = '2';
            console.log('✅ Server connected:', data.status);
        }
    } catch (error) {
        console.log('⚠️ Server not connected:', error.message);
        document.getElementById('totalItems').textContent = '⚠️';
    }
}

// ---------- ADD CLICK FEEDBACK TO MODULE CARDS ----------
document.querySelectorAll('.module-card').forEach(card => {
    card.addEventListener('click', function() {
        // Visual feedback
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

// ---------- KEYBOARD SHORTCUTS (Optional) ----------
document.addEventListener('keydown', function(e) {
    // Press 'R' to refresh stats
    if (e.key === 'r' || e.key === 'R') {
        loadDashboardStats();
        console.log('🔄 Stats refreshed');
    }
});

console.log('📊 Dashboard ready. Press "R" to refresh stats.');