// ==============================================
// 📋 ERROR DASHBOARD LOGIC
// ==============================================

// API_BASE and STORAGE_KEY are defined in client_context.js (loaded before this file)

let currentPage = 1;
let totalPages = 1;
let totalRecords = 0;
let currentFilters = {};

// DOM refs
const tableBody = document.getElementById('errorTableBody');
const noDataMessage = document.getElementById('noDataMessage');
const totalErrorsSpan = document.getElementById('totalErrors');
const pendingErrorsSpan = document.getElementById('pendingErrors');
const resolvedErrorsSpan = document.getElementById('resolvedErrors');
const categoryStatsDiv = document.getElementById('categoryStats');
const statusMessage = document.getElementById('statusMessage');
const prevPageBtn = document.getElementById('prevPageBtn');
const nextPageBtn = document.getElementById('nextPageBtn');
const pageInfo = document.getElementById('pageInfo');

// Filter inputs
const filterModule = document.getElementById('filterModule');
const filterCategory = document.getElementById('filterCategory');
const filterResolved = document.getElementById('filterResolved');
const filterSearch = document.getElementById('filterSearch');
const filterStartDate = document.getElementById('filterStartDate');
const filterEndDate = document.getElementById('filterEndDate');
const applyFiltersBtn = document.getElementById('applyFiltersBtn');
const clearFiltersBtn = document.getElementById('clearFiltersBtn');

// Modal
const detailModal = document.getElementById('detailModal');
const modalTitle = document.getElementById('modalTitle');
const modalBody = document.getElementById('modalBody');
const closeModalBtn = document.getElementById('closeModalBtn');

const limit = 20;

// ---------- HELPERS ----------
function errorDashboardEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function showStatus(msg, type = 'info') {
    statusMessage.textContent = msg;
    statusMessage.className = 'upload-status ' + type;
    statusMessage.style.display = 'block';
    clearTimeout(window.statusTimeout);
    window.statusTimeout = setTimeout(() => {
        statusMessage.style.display = 'none';
    }, 5000);
}

function formatDate(dtStr) {
    return dtStr || '—';
}

function getSeverityClass(severity) {
    if (!severity) return '';
    const map = {
        'Fatal': 'severity-fatal',
        'Error': 'severity-error',
        'Warning': 'severity-warning',
        'Info': 'severity-info'
    };
    return map[severity] || '';
}

function getStatusClass(isResolved) {
    return isResolved ? 'status-resolved' : 'status-pending';
}

function getStatusText(isResolved) {
    return isResolved ? '✅ Resolved' : '⏳ Pending';
}

// ---------- LOAD STATS ----------
async function loadStats() {
    try {
        const res = await fetch(`${API_BASE}/api/error-logs/stats`);
        if (!res.ok) throw new Error('Failed to fetch stats');
        const stats = await res.json();
        totalErrorsSpan.textContent = stats.total || 0;
        pendingErrorsSpan.textContent = stats.pending || 0;
        resolvedErrorsSpan.textContent = stats.resolved || 0;

        // Category breakdown
        const cat = stats.category_breakdown || {};
        let catHtml = '';
        for (const [key, count] of Object.entries(cat)) {
            catHtml += `<span style="font-size:12px; background:#f1f5f9; padding:2px 10px; border-radius:10px; margin:0 4px;">${errorDashboardEscape(key)}: ${errorDashboardEscape(count)}</span>`;
        }
        categoryStatsDiv.innerHTML = catHtml || 'No categories';
    } catch (error) {
        console.error('Error loading stats:', error);
    }
}

// ---------- LOAD ERRORS ----------
async function loadErrors() {
    // Build query parameters
    const params = new URLSearchParams();
    params.append('limit', limit);
    params.append('skip', (currentPage - 1) * limit);

    if (filterModule.value) params.append('module', filterModule.value);
    if (filterCategory.value) params.append('error_category', filterCategory.value);
    if (filterResolved.value !== '') params.append('is_resolved', filterResolved.value);
    if (filterSearch.value.trim()) params.append('search', filterSearch.value.trim());
    if (filterStartDate.value) params.append('start_date', filterStartDate.value);
    if (filterEndDate.value) params.append('end_date', filterEndDate.value);

    try {
        const res = await fetch(`${API_BASE}/api/error-logs?${params.toString()}`);
        if (!res.ok) throw new Error('Failed to fetch error logs');
        const data = await res.json();

        // Unfortunately our API doesn't return total count in the response body.
        // We'll use the number of rows returned to estimate pagination.
        // But we also have a `count` endpoint? We didn't implement a separate count endpoint.
        // We'll use the length as a hint: if length < limit, we are on the last page.
        // For simplicity, we'll keep a simple pagination: next/prev based on current data.
        // For better UX, we could fetch total count separately, but for now we'll just adjust.
        // However, we can use the fact that the API returns an array, not an object with total.
        // We need to handle pagination differently: keep a `total` variable manually.
        // Since we don't have total, we'll use the response length to determine next/prev.

        renderErrors(data);
        const countHeader = res.headers.get('X-Total-Count');
        if (countHeader !== null && /^\d+$/.test(countHeader)) {
            totalRecords = Number(countHeader);
            totalPages = Math.max(1, Math.ceil(totalRecords / limit));
            updatePagination();
            return;
        }
        // Update pagination based on length and known limit
        const count = data.length;
        // If we got less than limit, we're on last page
        const isLast = count < limit;
        totalPages = currentPage; // We'll just set to current page and assume we can go next if count == limit.
        // But we need to know if there is a next page. We'll set totalPages dynamically.
        if (count === limit) {
            totalPages = currentPage + 1; // there might be more
        } else {
            totalPages = currentPage;
        }
        updatePagination();

        if (data.length === 0) {
            noDataMessage.style.display = 'block';
            tableBody.innerHTML = '';
        } else {
            noDataMessage.style.display = 'none';
        }
    } catch (error) {
        showStatus('❌ Error loading errors: ' + error.message, 'error');
        tableBody.innerHTML = '';
        noDataMessage.style.display = 'block';
        noDataMessage.textContent = 'Failed to load errors. Please try again.';
    }
}

// ---------- RENDER ERRORS ----------
function renderErrors(errors) {
    tableBody.innerHTML = '';
    if (!errors || errors.length === 0) {
        noDataMessage.style.display = 'block';
        return;
    }
    noDataMessage.style.display = 'none';

    errors.forEach(err => {
        const tr = document.createElement('tr');

        // ID
        const tdId = document.createElement('td');
        tdId.textContent = err.id;
        tdId.style.fontWeight = '500';
        tr.appendChild(tdId);

        // Error Code
        const tdCode = document.createElement('td');
        tdCode.textContent = err.error_code || '—';
        tdCode.style.fontSize = '12px';
        tdCode.style.color = '#64748b';
        tr.appendChild(tdCode);

        // Message (truncated)
        const tdMsg = document.createElement('td');
        const msg = err.error_message || '';
        tdMsg.textContent = msg.length > 60 ? msg.substring(0, 60) + '...' : msg;
        tdMsg.title = msg;
        tr.appendChild(tdMsg);

        // Category
        const tdCat = document.createElement('td');
        const cat = err.error_category || 'Unknown';
        tdCat.textContent = cat;
        tdCat.style.fontSize = '12px';
        tr.appendChild(tdCat);

        // Module
        const tdModule = document.createElement('td');
        tdModule.textContent = err.module || '—';
        tdModule.style.fontSize = '12px';
        tr.appendChild(tdModule);

        // Status
        const tdStatus = document.createElement('td');
        const statusSpan = document.createElement('span');
        statusSpan.className = `status-badge ${getStatusClass(err.is_resolved)}`;
        statusSpan.textContent = getStatusText(err.is_resolved);
        tdStatus.appendChild(statusSpan);
        tr.appendChild(tdStatus);

        // Date
        const tdDate = document.createElement('td');
        tdDate.textContent = formatDate(err.created_at);
        tdDate.style.fontSize = '12px';
        tr.appendChild(tdDate);

        // Actions
        const tdActions = document.createElement('td');
        tdActions.style.textAlign = 'center';
        tdActions.style.whiteSpace = 'nowrap';

        // View
        const viewBtn = document.createElement('button');
        viewBtn.className = 'action-btn view';
        viewBtn.textContent = '👁️';
        viewBtn.title = 'View Details';
        viewBtn.addEventListener('click', () => openDetailModal(err.id));
        tdActions.appendChild(viewBtn);

        // Resolve/Unresolve toggle
        const resolveBtn = document.createElement('button');
        resolveBtn.className = 'action-btn resolve';
        resolveBtn.textContent = err.is_resolved ? '↩️' : '✅';
        resolveBtn.title = err.is_resolved ? 'Mark as Pending' : 'Mark as Resolved';
        resolveBtn.addEventListener('click', () => toggleResolve(err.id, err.is_resolved));
        tdActions.appendChild(resolveBtn);

        // Delete
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'action-btn delete';
        deleteBtn.textContent = '🗑️';
        deleteBtn.title = 'Delete Error';
        deleteBtn.addEventListener('click', () => confirmDelete(err.id));
        tdActions.appendChild(deleteBtn);

        tr.appendChild(tdActions);
        tableBody.appendChild(tr);
    });
}

// ---------- PAGINATION ----------
function updatePagination() {
    pageInfo.textContent = `Page ${currentPage} of ${totalPages}`;
    prevPageBtn.disabled = currentPage <= 1;
    nextPageBtn.disabled = currentPage >= totalPages;
}

prevPageBtn.addEventListener('click', () => {
    if (currentPage > 1) {
        currentPage--;
        loadErrors();
    }
});

nextPageBtn.addEventListener('click', () => {
    // We'll increment and load, but if result count < limit, we won't have next.
    // However we will rely on the fact that if we got limit rows, there might be more.
    // So we'll just increment and reload.
    currentPage++;
    loadErrors();
});

// ---------- FILTERS ----------
applyFiltersBtn.addEventListener('click', () => {
    currentPage = 1;
    loadErrors();
});

clearFiltersBtn.addEventListener('click', () => {
    filterModule.value = '';
    filterCategory.value = '';
    filterResolved.value = '';
    filterSearch.value = '';
    filterStartDate.value = '';
    filterEndDate.value = '';
    currentPage = 1;
    loadErrors();
});

// ---------- MODAL ----------
async function openDetailModal(errorId) {
    try {
        const res = await fetch(`${API_BASE}/api/error-logs/${errorId}`);
        if (!res.ok) throw new Error('Failed to fetch error details');
        const err = await res.json();

        modalTitle.textContent = `Error ${err.id} – ${err.error_code || 'Unknown'}`;
        modalBody.innerHTML = buildDetailHTML(err);
        detailModal.classList.add('active');
    } catch (error) {
        showStatus('❌ Error loading details: ' + error.message, 'error');
    }
}

function buildDetailHTML(err) {
    // Treat every stored string as text at this rendering boundary.
    err = Object.fromEntries(Object.entries(err).map(([key, value]) => [key,
        typeof value === 'string' ? errorDashboardEscape(value) : value]));
    return `
        <div class="detail-grid">
            <div class="label">Error Code</div>
            <div class="value">${err.error_code || '—'}</div>

            <div class="label">Category</div>
            <div class="value">${err.error_category || 'Unknown'}</div>

            <div class="label">Module</div>
            <div class="value">${err.module || '—'}</div>

            <div class="label">Endpoint</div>
            <div class="value">${err.endpoint || '—'}</div>

            <div class="label">Method</div>
            <div class="value">${err.method || '—'}</div>

            <div class="label">Status Code</div>
            <div class="value">${err.status_code || '—'}</div>

            <div class="label">User</div>
            <div class="value">${err.user_id || 'System'}</div>

            <div class="label">Client ID</div>
            <div class="value">${err.client_id || '—'}</div>

            <div class="label">Date</div>
            <div class="value">${formatDate(err.created_at)}</div>

            <div class="label">File</div>
            <div class="value">${err.file_name || '—'}</div>

            <div class="label">Line</div>
            <div class="value">${err.line_number || '—'}</div>

            <div class="label">Error Message</div>
            <div class="value" style="font-weight:500;color:#dc2626;">${err.error_message || '—'}</div>

            <div class="label">Suggested Fix</div>
            <div class="value" style="background:#e8f5e9; padding:4px 8px; border-radius:4px;">${err.suggested_fix || 'No suggestion available'}</div>

            ${err.stack_trace ? `
                <div class="label">Stack Trace</div>
                <div class="value code-block">${err.stack_trace}</div>
            ` : ''}

            ${err.code_context ? `
                <div class="label">Code Context</div>
                <div class="value code-block">${err.code_context}</div>
            ` : ''}

            ${err.input_data ? `
                <div class="label">Input Data</div>
                <div class="value code-block">${err.input_data}</div>
            ` : ''}

            <div class="label">Status</div>
            <div class="value"><span class="status-badge ${getStatusClass(err.is_resolved)}">${getStatusText(err.is_resolved)}</span></div>

            ${err.is_resolved ? `
                <div class="label">Resolved By</div>
                <div class="value">${err.resolved_by || '—'}</div>

                <div class="label">Resolved At</div>
                <div class="value">${formatDate(err.resolved_at)}</div>

                <div class="label">Resolution Notes</div>
                <div class="value">${err.resolution_notes || '—'}</div>
            ` : ''}
        </div>
        ${!err.is_resolved ? `
            <div class="resolve-section">
                <h4>✅ Resolve This Error</h4>
                <textarea id="resolutionNotes" placeholder="Add notes about how this error was fixed..."></textarea>
                <button id="resolveBtn">Mark as Resolved</button>
            </div>
        ` : ''}
    `;
}

// Event delegation for resolve button inside modal
document.addEventListener('click', function(e) {
    if (e.target && e.target.id === 'resolveBtn') {
        const notes = document.getElementById('resolutionNotes');
        if (notes) {
            // Find the error ID from the modal title or from a data attribute
            // We'll use the modal title to extract ID
            const titleText = modalTitle.textContent;
            const match = titleText.match(/Error (\d+)/);
            if (match) {
                const errorId = parseInt(match[1]);
                resolveError(errorId, notes.value);
            }
        }
    }
});

async function resolveError(errorId, notes) {
    try {
        const res = await fetch(`${API_BASE}/api/error-logs/${errorId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                is_resolved: true,
                resolution_notes: notes || 'Resolved via dashboard'
            })
        });
        if (!res.ok) throw new Error('Failed to resolve error');
        const result = await res.json();
        showStatus('✅ Error resolved successfully!', 'success');
        detailModal.classList.remove('active');
        loadErrors();
        loadStats();
    } catch (error) {
        showStatus('❌ Error resolving: ' + error.message, 'error');
    }
}

async function toggleResolve(errorId, currentStatus) {
    const newStatus = !currentStatus;
    try {
        const res = await fetch(`${API_BASE}/api/error-logs/${errorId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                is_resolved: newStatus,
                resolution_notes: newStatus ? 'Resolved via dashboard' : ''
            })
        });
        if (!res.ok) throw new Error('Failed to update');
        showStatus(`✅ Error marked as ${newStatus ? 'Resolved' : 'Pending'}`, 'success');
        loadErrors();
        loadStats();
    } catch (error) {
        showStatus('❌ Error updating status: ' + error.message, 'error');
    }
}

async function confirmDelete(errorId) {
    if (!confirm('Are you sure you want to permanently delete this error log?')) return;
    try {
        const res = await fetch(`${API_BASE}/api/error-logs/${errorId}`, { method: 'DELETE' });
        if (!res.ok) throw new Error('Failed to delete');
        showStatus('✅ Error log deleted', 'success');
        loadErrors();
        loadStats();
    } catch (error) {
        showStatus('❌ Error deleting: ' + error.message, 'error');
    }
}

// ---------- MODAL CLOSE ----------
closeModalBtn.addEventListener('click', () => {
    detailModal.classList.remove('active');
});
detailModal.addEventListener('click', function(e) {
    if (e.target === this) this.classList.remove('active');
});

// ---------- INIT ----------
window.onload = async function() {
    if (window.clientContextReady) await window.clientContextReady;
    loadStats();
    loadErrors();
};