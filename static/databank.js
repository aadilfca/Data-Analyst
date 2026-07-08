// ==============================================
// 📊 CLIENT DATA BANK - AVAILABILITY + PREVIEW
// ==============================================

// API_BASE and STORAGE_KEY are defined in client_context.js

// ---------- STATE ----------
let availabilityData = [];
let selectedTemplateId = null;
let dateStart = null;
let dateEnd = null;
let currentPage = 1;
let totalPages = 1;
let previewLimit = 50;
let previewTotalRows = 0;
let previewSundayCount = 0;
let previewTemplateId = null;
let previewTemplateName = '';
let previewDateStart = null;
let previewDateEnd = null;
let isPreviewOpen = false;
let isPreviewLoading = false;

// ---------- DOM REFS ----------
const filterTemplate = document.getElementById('templateFilter');
const filterDateStart = document.getElementById('dateStart');
const filterDateEnd = document.getElementById('dateEnd');
const checkBtn = document.getElementById('checkAvailabilityBtn');
const availabilityBody = document.getElementById('availabilityBody');
const noDataMessage = document.getElementById('noDataMessage');
const statusDiv = document.getElementById('statusMessage');

const statsTotalRecords = document.getElementById('totalRecords');
const statsTotalTemplates = document.getElementById('totalTemplates');
const statsMissingTemplates = document.getElementById('missingTemplates');
const statsSundayRows = document.getElementById('totalSundayRows');

// Preview modal elements
const previewModal = document.getElementById('previewModal');
const previewTitle = document.getElementById('previewTitle');
const previewTableHead = document.getElementById('previewTableHead');
const previewTableBody = document.getElementById('previewTableBody');
const pageSizeSelect = document.getElementById('pageSizeSelect');
const sundayOnlyCheckbox = document.getElementById('sundayOnlyCheckbox');
const sundayCountBadge = document.getElementById('sundayCountBadge');
const searchInput = document.getElementById('searchInput');
const amountMin = document.getElementById('amountMin');
const amountMax = document.getElementById('amountMax');
const applyFiltersBtn = document.getElementById('applyFiltersBtn');
const clearFiltersBtn = document.getElementById('clearFiltersBtn');
const prevPageBtn = document.getElementById('prevPageBtn');
const nextPageBtn = document.getElementById('nextPageBtn');
const pageInfo = document.getElementById('pageInfo');
const previewCountInfo = document.getElementById('previewCountInfo');
const closePreviewBtn = document.getElementById('closePreviewBtn');

// ---------- HELPERS ----------
function showStatus(msg, type = 'info') {
    statusDiv.textContent = msg;
    statusDiv.className = 'upload-status ' + type;
    statusDiv.style.display = 'block';
    clearTimeout(window.statusTimeout);
    window.statusTimeout = setTimeout(() => {
        statusDiv.style.display = 'none';
    }, 5000);
}

function getSelectedClientId() {
    return localStorage.getItem(STORAGE_KEY) || null;
}

function formatDateForDisplay(dateStr) {
    if (!dateStr) return '—';
    try {
        const d = new Date(dateStr);
        return d.toLocaleDateString('en-GB', { day: '2-digit', month: '2-digit', year: 'numeric' });
    } catch {
        return dateStr;
    }
}

function getDateRangeFromFilter() {
    const start = filterDateStart.value;
    const end = filterDateEnd.value;
    if (start && end && start <= end) {
        return { start, end };
    }
    return null;
}

function isSunday(dateStr) {
    if (!dateStr) return false;
    try {
        const d = new Date(dateStr);
        return d.getDay() === 0; // 0 = Sunday
    } catch {
        return false;
    }
}

function formatCellValue(value, columnName = '') {
    if (value === undefined || value === null || value === '') return '—';
    if (typeof value === 'number') {
        // Use global formatAmount if available
        if (window.formatAmount) {
            return window.formatAmount(value);
        }
        return value.toString();
    }
    return value;
}

// ---------- LOAD TEMPLATES INTO DROPDOWN ----------
async function loadTemplateDropdown() {
    try {
        const res = await fetch(API_BASE + '/api/templates');
        if (!res.ok) throw new Error('Failed to fetch templates');
        const templates = await res.json();
        
        filterTemplate.innerHTML = '<option value="">All Templates</option>';
        templates.forEach(tpl => {
            const opt = document.createElement('option');
            opt.value = tpl.id;
            opt.textContent = tpl.template_name;
            filterTemplate.appendChild(opt);
        });
        console.log('✅ Template dropdown loaded with ' + templates.length + ' templates');
    } catch (error) {
        console.error('Error loading templates:', error);
        filterTemplate.innerHTML = '<option value="">⚠️ Failed to load</option>';
    }
}

// ---------- SET DEFAULT DATE RANGE (Current Month) ----------
function setDefaultDateRange() {
    const now = new Date();
    const firstDay = new Date(now.getFullYear(), now.getMonth(), 1);
    const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    
    const formatDate = (d) => {
        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${y}-${m}-${day}`;
    };
    
    filterDateStart.value = formatDate(firstDay);
    filterDateEnd.value = formatDate(lastDay);
}

// ---------- FETCH AVAILABILITY DATA ----------
async function fetchAvailability() {
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ Please select a client from the Dashboard first.', 'error');
        return;
    }
    
    const dateRange = getDateRangeFromFilter();
    if (!dateRange) {
        showStatus('⚠️ Please select a valid date range (start ≤ end).', 'error');
        return;
    }
    
    const templateId = filterTemplate.value || null;
    const params = new URLSearchParams({
        client_id: clientId,
        date_start: dateRange.start,
        date_end: dateRange.end
    });
    if (templateId) {
        params.append('template_id', templateId);
    }
    
    // Disable button and show loading
    checkBtn.disabled = true;
    checkBtn.textContent = '⏳ Loading...';
    showStatus('⏳ Fetching availability data...', 'loading');
    
    try {
        const res = await fetch(`${API_BASE}/api/data/availability?${params.toString()}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch availability');
        }
        const data = await res.json();
        availabilityData = data;
        
        // Update stats
        const totalRows = data.reduce((sum, item) => sum + item.row_count, 0);
        const totalTemplates = data.length;
        const missing = data.filter(item => item.status === 'missing').length;
        const sundayRows = data.reduce((sum, item) => sum + (item.sunday_count || 0), 0);
        
        statsTotalRecords.textContent = totalRows;
        statsTotalTemplates.textContent = totalTemplates;
        statsMissingTemplates.textContent = missing;
        statsSundayRows.textContent = sundayRows;
        
        renderAvailabilityTable(data);
        showStatus(`✅ Found ${totalTemplates} template(s) with ${totalRows} total rows.`, 'success');
        
    } catch (error) {
        console.error('Fetch availability error:', error);
        showStatus('❌ Error: ' + error.message, 'error');
        availabilityBody.innerHTML = '';
        noDataMessage.style.display = 'block';
        noDataMessage.innerHTML = 'Error loading data. Please try again.';
    } finally {
        checkBtn.disabled = false;
        checkBtn.textContent = '🔍 Check Availability';
    }
}

// ---------- RENDER AVAILABILITY TABLE ----------
function renderAvailabilityTable(data) {
    availabilityBody.innerHTML = '';
    if (!data || data.length === 0) {
        noDataMessage.style.display = 'block';
        noDataMessage.innerHTML = 'No templates found for the selected criteria.';
        return;
    }
    noDataMessage.style.display = 'none';
    
    data.forEach((item, index) => {
        const tr = document.createElement('tr');
        
        // # (row number)
        const tdNum = document.createElement('td');
        tdNum.textContent = index + 1;
        tdNum.style.textAlign = 'center';
        tdNum.style.color = '#94a3b8';
        tr.appendChild(tdNum);
        
        // Template Name
        const tdName = document.createElement('td');
        tdName.textContent = item.template_name;
        tdName.style.fontWeight = '500';
        tr.appendChild(tdName);
        
        // Date Range
        const tdDate = document.createElement('td');
        if (item.date_min && item.date_max) {
            tdDate.textContent = `${formatDateForDisplay(item.date_min)} – ${formatDateForDisplay(item.date_max)}`;
        } else {
            tdDate.textContent = '—';
            tdDate.style.color = '#94a3b8';
        }
        tr.appendChild(tdDate);
        
        // Row Count
        const tdRows = document.createElement('td');
        tdRows.textContent = item.row_count;
        tdRows.style.textAlign = 'center';
        tdRows.style.fontWeight = '600';
        tr.appendChild(tdRows);
        
        // Sunday Rows
        const tdSunday = document.createElement('td');
        tdSunday.textContent = item.sunday_count || 0;
        tdSunday.style.textAlign = 'center';
        if (item.sunday_count > 0) {
            tdSunday.style.color = '#f59e0b';
            tdSunday.style.fontWeight = '600';
        }
        tr.appendChild(tdSunday);
        
        // Status
        const tdStatus = document.createElement('td');
        tdStatus.style.textAlign = 'center';
        const statusSpan = document.createElement('span');
        statusSpan.className = `status-badge ${item.status}`;
        const statusMap = {
            'complete': '✅ Complete',
            'missing': '❌ Missing',
            'partial': '⚠️ Partial'
        };
        statusSpan.textContent = statusMap[item.status] || item.status;
        tdStatus.appendChild(statusSpan);
        tr.appendChild(tdStatus);
        
        // Actions
        const tdActions = document.createElement('td');
        tdActions.style.textAlign = 'center';
        tdActions.style.whiteSpace = 'nowrap';
        
        if (item.row_count > 0) {
            // Preview button
            const previewBtn = document.createElement('button');
            previewBtn.className = 'action-btn preview';
            previewBtn.textContent = '👁️';
            previewBtn.title = 'Preview Data';
            previewBtn.addEventListener('click', () => {
                openPreview(item.template_id, item.template_name);
            });
            tdActions.appendChild(previewBtn);
            
            // Export button
            const exportBtn = document.createElement('button');
            exportBtn.className = 'action-btn export';
            exportBtn.textContent = '📥';
            exportBtn.title = 'Export to Excel';
            exportBtn.style.marginLeft = '6px';
            exportBtn.addEventListener('click', () => {
                exportDataset(item.template_id, item.template_name);
            });
            tdActions.appendChild(exportBtn);
        } else {
            tdActions.textContent = '—';
            tdActions.style.color = '#94a3b8';
        }
        
        tr.appendChild(tdActions);
        availabilityBody.appendChild(tr);
    });
}

// ==============================================
// 📋 PREVIEW MODAL – FULL IMPLEMENTATION
// ==============================================

// ---------- OPEN PREVIEW ----------
function openPreview(templateId, templateName) {
    const dateRange = getDateRangeFromFilter();
    if (!dateRange) {
        showStatus('⚠️ Please select a date range.', 'error');
        return;
    }
    
    previewTemplateId = templateId;
    previewTemplateName = templateName;
    previewDateStart = dateRange.start;
    previewDateEnd = dateRange.end;
    previewLimit = parseInt(pageSizeSelect.value) || 50;
    currentPage = 1;
    isPreviewOpen = true;
    
    // Show modal
    previewModal.style.display = 'flex';
    previewTitle.textContent = `📋 ${templateName} – Preview`;
    sundayOnlyCheckbox.checked = false;
    searchInput.value = '';
    amountMin.value = '';
    amountMax.value = '';
    
    // Fetch data
    fetchPreviewData();
}

// ---------- FETCH PREVIEW DATA ----------
async function fetchPreviewData() {
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected.', 'error');
        return;
    }
    
    if (!previewTemplateId || !previewDateStart || !previewDateEnd) {
        showStatus('⚠️ Missing preview parameters.', 'error');
        return;
    }
    
    isPreviewLoading = true;
    previewTableBody.innerHTML = `<tr><td colspan="100" style="text-align:center; padding:40px; color:#94a3b8;">
        <span class="spinner"></span> Loading data...
    </td></tr>`;
    previewTableHead.innerHTML = '';
    
    const params = new URLSearchParams({
        client_id: clientId,
        template_id: previewTemplateId,
        date_start: previewDateStart,
        date_end: previewDateEnd,
        page: currentPage,
        limit: previewLimit
    });
    
    // Add filters
    if (searchInput.value.trim()) {
        params.append('search', searchInput.value.trim());
    }
    if (amountMin.value) {
        params.append('amount_min', amountMin.value);
    }
    if (amountMax.value) {
        params.append('amount_max', amountMax.value);
    }
    if (sundayOnlyCheckbox.checked) {
        params.append('sunday_only', 'true');
    }
    
    try {
        const res = await fetch(`${API_BASE}/api/data/preview?${params.toString()}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch preview');
        }
        const data = await res.json();
        
        previewTotalRows = data.total_rows;
        previewSundayCount = data.sunday_count;
        const rows = data.rows || [];
        
        // Update Sunday badge
        sundayCountBadge.textContent = `Sunday rows: ${previewSundayCount}`;
        
        // Render table
        renderPreviewRows(rows);
        updatePagination();
        
        if (rows.length === 0 && previewTotalRows === 0) {
            previewTableBody.innerHTML = '<tr><td colspan="100" style="text-align:center; padding:40px; color:#94a3b8;">No data found for the selected criteria.</td></tr>';
        }
        
    } catch (error) {
        console.error('Preview fetch error:', error);
        showStatus('❌ Error loading preview: ' + error.message, 'error');
        previewTableBody.innerHTML = `<tr><td colspan="100" style="text-align:center; padding:40px; color:#dc2626;">Error loading data. Please try again.</td></tr>`;
    } finally {
        isPreviewLoading = false;
    }
}

// ---------- RENDER PREVIEW ROWS ----------
function renderPreviewRows(rows) {
    if (!rows || rows.length === 0) {
        previewTableBody.innerHTML = '<tr><td colspan="100" style="text-align:center; padding:40px; color:#94a3b8;">No rows to display.</td></tr>';
        previewTableHead.innerHTML = '';
        previewCountInfo.textContent = '0 rows';
        return;
    }
    
    // Build headers from first row
    const firstRow = rows[0].row_data;
    const headers = firstRow ? Object.keys(firstRow) : [];
    previewTableHead.innerHTML = '';
    if (headers.length) {
        const tr = document.createElement('tr');
        headers.forEach(h => {
            const th = document.createElement('th');
            th.textContent = h;
            th.style.padding = '8px 12px';
            th.style.textAlign = 'left';
            th.style.borderBottom = '2px solid #e2e8f0';
            th.style.whiteSpace = 'nowrap';
            tr.appendChild(th);
        });
        previewTableHead.appendChild(tr);
    }
    
    // Render body rows
    previewTableBody.innerHTML = '';
    rows.forEach(row => {
        const tr = document.createElement('tr');
        // Check if this row's extract_date is Sunday
        const isSun = isSunday(row.extract_date);
        if (isSun) {
            tr.className = 'sunday-highlight';
        }
        
        headers.forEach(h => {
            const td = document.createElement('td');
            td.style.padding = '6px 12px';
            td.style.borderBottom = '1px solid #f0f2f5';
            const value = row.row_data[h] !== undefined ? row.row_data[h] : '';
            td.textContent = formatCellValue(value, h);
            tr.appendChild(td);
        });
        previewTableBody.appendChild(tr);
    });
    
    previewCountInfo.textContent = `Showing ${rows.length} of ${previewTotalRows} rows`;
}

// ---------- UPDATE PAGINATION ----------
function updatePagination() {
    const totalPages = Math.ceil(previewTotalRows / previewLimit) || 1;
    pageInfo.textContent = `Page ${currentPage} of ${totalPages}`;
    prevPageBtn.disabled = currentPage <= 1;
    nextPageBtn.disabled = currentPage >= totalPages;
}

// ---------- APPLY PREVIEW FILTERS ----------
function applyPreviewFilters() {
    currentPage = 1;
    fetchPreviewData();
}

// ---------- CLEAR PREVIEW FILTERS ----------
function clearPreviewFilters() {
    searchInput.value = '';
    amountMin.value = '';
    amountMax.value = '';
    sundayOnlyCheckbox.checked = false;
    currentPage = 1;
    fetchPreviewData();
}

// ---------- EXPORT DATASET (Full implementation) ----------
async function exportDataset(templateId, templateName) {
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected.', 'error');
        return;
    }
    
    const dateRange = getDateRangeFromFilter();
    if (!dateRange) {
        showStatus('⚠️ Please select a date range.', 'error');
        return;
    }
    
    showStatus('⏳ Preparing export...', 'loading');
    
    try {
        // Fetch all rows in batches
        let allRows = [];
        let page = 1;
        const limit = 500;
        let totalFetched = 0;
        const maxRows = 10000; // safety limit
        
        const params = new URLSearchParams({
            client_id: clientId,
            template_id: templateId,
            date_start: dateRange.start,
            date_end: dateRange.end,
            limit: limit
        });
        
        // If we have filters from the availability view? We'll use the same date range.
        // For export, we ignore the preview filters (search, amount, sunday) to export all.
        
        do {
            params.set('page', page);
            const res = await fetch(`${API_BASE}/api/data/preview?${params.toString()}`);
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Export failed');
            }
            const data = await res.json();
            const rows = data.rows || [];
            if (rows.length === 0) break;
            
            allRows = allRows.concat(rows);
            totalFetched += rows.length;
            page++;
            
            if (totalFetched >= maxRows) {
                showStatus(`⚠️ Export limited to ${maxRows} rows. Use filters to reduce data.`, 'error');
                break;
            }
        } while (true);
        
        if (allRows.length === 0) {
            showStatus('⚠️ No data to export.', 'error');
            return;
        }
        
        // Build Excel data
        const firstRow = allRows[0];
        const headers = firstRow.row_data ? Object.keys(firstRow.row_data) : [];
        const excelData = [headers];
        allRows.forEach(row => {
            const rowData = headers.map(h => row.row_data[h] !== undefined ? row.row_data[h] : '');
            excelData.push(rowData);
        });
        
        // Create workbook
        const ws = XLSX.utils.aoa_to_sheet(excelData);
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, 'Data');
        const wbout = XLSX.write(wb, { bookType: 'xlsx', type: 'array' });
        
        const blob = new Blob([wbout], { type: 'application/octet-stream' });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${templateName || 'dataset'}_${new Date().toISOString().slice(0,10)}.xlsx`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);
        
        showStatus(`✅ Exported ${allRows.length} rows to Excel`, 'success');
    } catch (error) {
        showStatus('❌ Export error: ' + error.message, 'error');
    }
}

// ---------- EVENT LISTENERS (Main) ----------
checkBtn.addEventListener('click', fetchAvailability);

filterDateStart.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchAvailability(); });
filterDateEnd.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchAvailability(); });
filterTemplate.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchAvailability(); });

// Preview modal events
closePreviewBtn.addEventListener('click', () => {
    previewModal.style.display = 'none';
    isPreviewOpen = false;
});
previewModal.addEventListener('click', (e) => {
    if (e.target === previewModal) {
        previewModal.style.display = 'none';
        isPreviewOpen = false;
    }
});

// Pagination
prevPageBtn.addEventListener('click', () => {
    if (currentPage > 1) {
        currentPage--;
        fetchPreviewData();
    }
});
nextPageBtn.addEventListener('click', () => {
    const totalPages = Math.ceil(previewTotalRows / previewLimit) || 1;
    if (currentPage < totalPages) {
        currentPage++;
        fetchPreviewData();
    }
});

// Page size change
pageSizeSelect.addEventListener('change', function() {
    previewLimit = parseInt(this.value) || 50;
    currentPage = 1;
    if (isPreviewOpen) {
        fetchPreviewData();
    }
});

// Filters
applyFiltersBtn.addEventListener('click', applyPreviewFilters);
clearFiltersBtn.addEventListener('click', clearPreviewFilters);

// Sunday checkbox
sundayOnlyCheckbox.addEventListener('change', function() {
    if (isPreviewOpen) {
        currentPage = 1;
        fetchPreviewData();
    }
});

// Search with debounce
let searchTimeout;
searchInput.addEventListener('input', function() {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
        if (isPreviewOpen) {
            currentPage = 1;
            fetchPreviewData();
        }
    }, 400);
});

// Amount filters – apply on Enter key
amountMin.addEventListener('keydown', (e) => { if (e.key === 'Enter' && isPreviewOpen) applyPreviewFilters(); });
amountMax.addEventListener('keydown', (e) => { if (e.key === 'Enter' && isPreviewOpen) applyPreviewFilters(); });

// ESC key closes preview
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isPreviewOpen) {
        previewModal.style.display = 'none';
        isPreviewOpen = false;
    }
});

// ---------- CLIENT CHANGE HOOK ----------
window.onClientChange = function(clientId) {
    if (clientId) {
        if (filterDateStart.value && filterDateEnd.value) {
            fetchAvailability();
        }
    }
};

// ---------- INIT ----------
window.onload = async function() {
    setDefaultDateRange();
    await loadTemplateDropdown();
    
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ Please select a client from the Dashboard first.', 'error');
        setTimeout(() => {
            window.location.href = '/static/index.html';
        }, 2000);
        return;
    }
    
    fetchAvailability();
    console.log('📊 Data Bank with full preview loaded');
};