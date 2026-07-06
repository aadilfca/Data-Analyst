// ==============================================
// 🔧 SETUP MODULE LOGIC
// ==============================================

const API_BASE = 'http://localhost:5000';

// ---------- LOAD STATS ----------
async function loadSetupStats() {
    try {
        // Get column count
        const colRes = await fetch(API_BASE + '/api/columns');
        const columns = await colRes.json();
        document.getElementById('totalColumns').textContent = columns.length || 0;
        
        // Get template count
        const tempRes = await fetch(API_BASE + '/api/templates');
        const templates = await tempRes.json();
        document.getElementById('totalTemplates').textContent = templates.length || 0;
        
        // Count active templates
        const active = templates.filter(t => t.is_active !== 0).length;
        document.getElementById('activeTemplates').textContent = active || 0;
        
        console.log('✅ Setup stats loaded');
    } catch (error) {
        console.log('⚠️ Error loading setup stats:', error.message);
        document.getElementById('totalColumns').textContent = '⚠️';
        document.getElementById('totalTemplates').textContent = '⚠️';
        document.getElementById('activeTemplates').textContent = '⚠️';
    }
}

// ---------- ADD CLICK FEEDBACK ----------
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
    loadSetupStats();
    console.log('🔧 Setup Module loaded successfully!');
};