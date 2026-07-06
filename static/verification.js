// ---------- LOAD VERIFICATION STATS ----------
async function loadVerificationStats() {
    try {
        // Try to fetch stats from the verification API
        const res = await fetch('/api/verification/stats');
        if (res.ok) {
            const stats = await res.json();
            document.getElementById('pendingCount').textContent = stats.pending || 0;
            document.getElementById('verifiedToday').textContent = stats.verified_today || 0;
            document.getElementById('totalVerified').textContent = stats.total_verified || 0;
            console.log('✅ Verification stats loaded:', stats);
        } else {
            // Fallback: show 0s
            document.getElementById('pendingCount').textContent = '0';
            document.getElementById('verifiedToday').textContent = '0';
            document.getElementById('totalVerified').textContent = '0';
        }
    } catch (error) {
        console.log('⚠️ Error loading verification stats:', error.message);
        // Show placeholder values
        document.getElementById('pendingCount').textContent = '0';
        document.getElementById('verifiedToday').textContent = '0';
        document.getElementById('totalVerified').textContent = '0';
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
    loadVerificationStats();
    console.log('✅ Verification Module loaded successfully!');
};

console.log('🔍 Verification Dashboard ready.');