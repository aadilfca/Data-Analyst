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

// ---------- BACKUP EXPORT ----------
async function exportBackup() {
    const statusSpan = document.getElementById('backupStatus');
    try {
        statusSpan.style.display = 'inline';
        statusSpan.textContent = '⏳ Exporting...';
        statusSpan.style.color = '#92400e';

        const res = await fetch(API_BASE + '/api/backup/export');
        if (!res.ok) throw new Error('Export failed');
        const data = await res.json();

        // Create download
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `setup_backup_${new Date().toISOString().slice(0,10)}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);

        statusSpan.textContent = '✅ Export successful!';
        statusSpan.style.color = '#166534';
        setTimeout(() => {
            statusSpan.style.display = 'none';
        }, 4000);
    } catch (error) {
        statusSpan.textContent = '❌ Export error: ' + error.message;
        statusSpan.style.color = '#991b1b';
        console.error(error);
    }
}

// ---------- BACKUP IMPORT ----------
async function handleImportFile(event) {
    const file = event.target.files[0];
    if (!file) return;

    const statusSpan = document.getElementById('backupStatus');
    try {
        const reader = new FileReader();
        reader.onload = async function(e) {
            try {
                const jsonData = JSON.parse(e.target.result);
                // Validate structure
                if (!jsonData.columns || !jsonData.templates) {
                    throw new Error('Invalid backup file: missing "columns" or "templates" keys.');
                }

                const confirmMsg = `This will REPLACE all existing columns and templates with the data from the backup file.\n\n` +
                    `Columns: ${jsonData.columns.length}\nTemplates: ${jsonData.templates.length}\n\nContinue?`;
                if (!confirm(confirmMsg)) {
                    statusSpan.style.display = 'inline';
                    statusSpan.textContent = '⛔ Import cancelled.';
                    statusSpan.style.color = '#92400e';
                    setTimeout(() => {
                        statusSpan.style.display = 'none';
                    }, 3000);
                    return;
                }

                statusSpan.style.display = 'inline';
                statusSpan.textContent = '⏳ Importing...';
                statusSpan.style.color = '#92400e';

                const res = await fetch(API_BASE + '/api/backup/import', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(jsonData)
                });
                const result = await res.json();
                if (!res.ok) {
                    throw new Error(result.detail || result.message || 'Import failed');
                }

                statusSpan.textContent = '✅ ' + result.message;
                statusSpan.style.color = '#166534';
                // Reload stats
                loadSetupStats();
                setTimeout(() => {
                    statusSpan.style.display = 'none';
                }, 5000);
            } catch (error) {
                statusSpan.textContent = '❌ Import error: ' + error.message;
                statusSpan.style.color = '#991b1b';
                console.error(error);
            }
        };
        reader.readAsText(file);
    } catch (error) {
        statusSpan.textContent = '❌ File read error: ' + error.message;
        statusSpan.style.color = '#991b1b';
        console.error(error);
    } finally {
        // Reset file input so same file can be re-uploaded
        event.target.value = '';
    }
}

// ---------- LOAD ON PAGE OPEN ----------
window.onload = function() {
    loadSetupStats();
    console.log('🔧 Setup Module loaded successfully!');

    // Backup export button
    document.getElementById('exportBackupBtn').addEventListener('click', exportBackup);
    // Backup import button triggers file input
    document.getElementById('importBackupBtn').addEventListener('click', function() {
        document.getElementById('importFileInput').click();
    });
    document.getElementById('importFileInput').addEventListener('change', handleImportFile);
};

// Reload stats when page becomes visible again (e.g., after navigating back from columns/templates)
window.addEventListener('pageshow', function(event) {
    loadSetupStats();
    console.log('🔄 Setup stats refreshed on pageshow');
});