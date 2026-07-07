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
 * Render a list of columns as checkboxes with drag handles and category grouping.
 * @param {HTMLElement} container - The DOM element where checkboxes will be appended.
 * @param {Array} columns - Array of column objects (must have id, column_name, category).
 * @param {Array} selectedIds - Array of column IDs to be pre-checked.
 */
function renderColumnCheckboxes(container, columns, selectedIds = []) {
    container.innerHTML = '';
    if (!columns || columns.length === 0) {
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

    const sortedCategories = Object.keys(groups).sort();
    // Build items in category order
    const items = [];
    sortedCategories.forEach(cat => {
        groups[cat].forEach(col => {
            items.push({
                id: col.id,
                name: col.column_name,
                category: cat,
                checked: selectedIds.includes(col.id)
            });
        });
    });

    // Render each item with a drag handle
    items.forEach(item => {
        const div = document.createElement('div');
        div.className = 'checkbox-item';
        div.dataset.id = item.id;
        div.style.cssText = 'display:flex; align-items:center; gap:6px; padding:4px 0; border-bottom:1px solid #f0f2f5;';

        // Drag handle (☰)
        const handle = document.createElement('span');
        handle.className = 'drag-handle';
        handle.textContent = '☰';
        handle.style.cssText = 'cursor:grab; color:#94a3b8; user-select:none; font-size:16px; padding:0 4px;';
        div.appendChild(handle);

        // Label with checkbox
        const label = document.createElement('label');
        label.style.cssText = 'display:flex; align-items:center; gap:5px; font-size:14px; cursor:pointer; flex:1;';

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = item.id;
        cb.checked = item.checked;
        cb.style.cssText = 'width:16px; height:16px; cursor:pointer;';
        label.appendChild(cb);

        const nameSpan = document.createElement('span');
        nameSpan.textContent = `${item.id}. ${item.name}`;
        nameSpan.style.color = '#1a1a2e';
        label.appendChild(nameSpan);

        const catSpan = document.createElement('span');
        catSpan.textContent = item.category;
        catSpan.style.cssText = 'color:#94a3b8; font-size:11px; margin-left:auto;';
        label.appendChild(catSpan);

        div.appendChild(label);
        container.appendChild(div);
    });

    // Update order count
    updateOrderCount(container);

    // Add change listeners to checkboxes
    container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        cb.addEventListener('change', function() {
            updateOrderCount(container);
        });
    });

    // Initialize Sortable for drag-and-drop if available
    if (typeof Sortable !== 'undefined') {
        new Sortable(container, {
            handle: '.drag-handle',
            animation: 150,
            ghostClass: 'sortable-ghost',
            dragClass: 'sortable-drag',
            onEnd: function() {
                updateOrderCount(container);
            }
        });
    } else {
        console.warn('⚠️ SortableJS not loaded; drag-and-drop disabled.');
    }
}

/**
 * Update the order count display.
 */
function updateOrderCount(container) {
    const checked = container.querySelectorAll('input[type="checkbox"]:checked').length;
    const orderSpan = document.getElementById('orderCount');
    if (orderSpan) {
        orderSpan.textContent = checked;
    }
}

// Expose functions globally
window.formatDate = formatDate;
window.renderColumnCheckboxes = renderColumnCheckboxes;