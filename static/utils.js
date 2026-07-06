// ==============================================
// 📦 UTILITY FUNCTIONS (shared across modules)
// ==============================================

/**
 * Format a date string (ISO or any parseable format) to DD/MM/YYYY.
 * @param {string} dateString - The date string to format.
 * @returns {string} Formatted date as DD/MM/YYYY or the original if invalid.
 */
function formatDate(dateString) {
    if (!dateString) return '—';
    try {
        const date = new Date(dateString);
        if (isNaN(date.getTime())) return dateString; // fallback
        const day = String(date.getDate()).padStart(2, '0');
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const year = date.getFullYear();
        return day + '/' + month + '/' + year;
    } catch (e) {
        return dateString;
    }
}

/**
 * Render a list of columns as checkboxes grouped by category.
 * @param {HTMLElement} container - The DOM element where checkboxes will be appended.
 * @param {Array} columns - Array of column objects (must have id, column_name, category).
 * @param {Array} selectedIds - Array of column IDs to be pre-checked.
 */
function renderColumnCheckboxes(container, columns, selectedIds = []) {
    container.innerHTML = '';
    if (!columns || columns.length === 0) {
        // Show a message inside the container
        const msg = document.createElement('div');
        msg.textContent = 'No active master columns available. Please add columns first.';
        msg.style.color = '#94a3b8';
        msg.style.padding = '15px';
        msg.style.textAlign = 'center';
        container.appendChild(msg);
        return;
    }

    // Group by category
    const groups = {};
    columns.forEach(col => {
        const cat = col.category || 'General';
        if (!groups[cat]) groups[cat] = [];
        groups[cat].push(col);
    });

    // Sort categories alphabetically
    const sortedCategories = Object.keys(groups).sort();

    sortedCategories.forEach(cat => {
        const groupDiv = document.createElement('div');
        groupDiv.className = 'checkbox-group';
        groupDiv.style.marginBottom = '8px';
        groupDiv.style.borderBottom = '1px solid #e2e8f0';
        groupDiv.style.paddingBottom = '6px';

        const catLabel = document.createElement('div');
        catLabel.className = 'checkbox-group-label';
        catLabel.textContent = cat;
        catLabel.style.fontWeight = '600';
        catLabel.style.fontSize = '13px';
        catLabel.style.color = '#475569';
        catLabel.style.marginBottom = '4px';
        groupDiv.appendChild(catLabel);

        const checkboxContainer = document.createElement('div');
        checkboxContainer.className = 'checkbox-items';
        checkboxContainer.style.display = 'flex';
        checkboxContainer.style.flexWrap = 'wrap';
        checkboxContainer.style.gap = '10px';

        groups[cat].forEach(col => {
            const label = document.createElement('label');
            label.style.display = 'flex';
            label.style.alignItems = 'center';
            label.style.gap = '5px';
            label.style.fontSize = '14px';
            label.style.cursor = 'pointer';

            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.value = col.id;
            cb.checked = selectedIds.includes(col.id);
            cb.style.width = '16px';
            cb.style.height = '16px';
            cb.style.cursor = 'pointer';

            const span = document.createElement('span');
            span.textContent = col.column_name;
            span.style.color = '#1a1a2e';

            label.appendChild(cb);
            label.appendChild(span);
            checkboxContainer.appendChild(label);
        });

        groupDiv.appendChild(checkboxContainer);
        container.appendChild(groupDiv);
    });
}

// Expose functions globally (for use in non-module scripts)
window.formatDate = formatDate;
window.renderColumnCheckboxes = renderColumnCheckboxes;