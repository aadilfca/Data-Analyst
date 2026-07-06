// ==============================================
// 📋 COLUMN TEMPLATE MANAGER
// ==============================================

const templateDropdown = document.getElementById('templateDropdown');

// ---------- LOAD TEMPLATES ----------
async function loadTemplates() {
    try {
        const res = await fetch('/api/templates');
        if (!res.ok) throw new Error('Failed to load templates');
        const templates = await res.json();
        
        // Clear dropdown (keep "Add New" option)
        templateDropdown.innerHTML = '';
        
        // Add "Add New" option
        const addOption = document.createElement('option');
        addOption.value = 'new';
        addOption.textContent = '➕ Add New';
        templateDropdown.appendChild(addOption);
        
        // Add separator
        const separator = document.createElement('option');
        separator.disabled = true;
        separator.textContent = '────────────────';
        templateDropdown.appendChild(separator);
        
        // Add templates
        templates.forEach(template => {
            const option = document.createElement('option');
            option.value = template.id;
            option.textContent = template.template_name;
            option.dataset.columns = template.columns;
            templateDropdown.appendChild(option);
        });
        
        console.log('✅ Templates loaded:', templates.length);
        
    } catch (error) {
        console.log('⚠️ Error loading templates:', error.message);
    }
}

// ---------- HANDLE TEMPLATE SELECTION ----------
templateDropdown.addEventListener('change', function() {
    if (this.value === 'new') {
        // Open template editor (will implement next)
        alert('Template editor coming soon!');
        // Reset dropdown to previous selection or first template
        this.value = this.options[1]?.value || '';
        return;
    }
    
    // Get selected template
    const selectedOption = this.options[this.selectedIndex];
    const templateName = selectedOption.textContent;
    const columns = JSON.parse(selectedOption.dataset.columns || '[]');
    
    console.log(`📋 Selected template: ${templateName}`, columns);
    
    // TODO: Update upload form to show the selected columns
    // This will be implemented in the next phase
});

// ---------- LOAD ON PAGE OPEN ----------
window.onload = function() {
    loadTemplates();
    console.log('📋 Template Manager loaded');
};