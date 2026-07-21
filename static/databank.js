// ==============================================
// 📊 CLIENT DATA BANK - FINAL POLISHED VERSION
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
let isAvailabilityLoading = false;

// ---------- DOM REFS ----------
const filterTemplate = document.getElementById('templateFilter');
const filterDateStart = document.getElementById('dateStart');
const filterDateEnd = document.getElementById('dateEnd');
const checkBtn = document.getElementById('checkAvailabilityBtn');
const clearFiltersBtn = document.getElementById('clearFiltersBtn');
const refreshBtn = document.getElementById('refreshBtn');
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
const dataQualityFilter = document.getElementById('dataQualityFilter');
const sundayCountBadge = document.getElementById('sundayCountBadge');

// Date range filter popover elements (Phase B2) — additive narrowing filter
// on top of the mandatory date_start/date_end range, same manual-entry
// pattern as amountMin/amountMax below.
const dateRangeFilterBtn = document.getElementById('dateRangeFilterBtn');
const dateRangeFilterPopover = document.getElementById('dateRangeFilterPopover');
const previewDateFromInput = document.getElementById('previewDateFrom');
const previewDateToInput = document.getElementById('previewDateTo');
const clearDateRangeFilterBtn = document.getElementById('clearDateRangeFilterBtn');
const searchInput = document.getElementById('searchInput');
const amountMin = document.getElementById('amountMin');
const amountMax = document.getElementById('amountMax');
const applyFiltersBtn = document.getElementById('applyFiltersBtn');
const clearPreviewFiltersBtn = document.getElementById('clearFiltersBtn');
const prevPageBtn = document.getElementById('prevPageBtn');
const nextPageBtn = document.getElementById('nextPageBtn');
const pageInfo = document.getElementById('pageInfo');
const previewCountInfo = document.getElementById('previewCountInfo');
const closePreviewBtn = document.getElementById('closePreviewBtn');

// Row revision history popover elements (Phase 3)
const rowHistoryPopover = document.getElementById('rowHistoryPopover');
const rowHistoryBody = document.getElementById('rowHistoryBody');
// Tracks which history icon (if any) currently has its popover open, so the
// click handler can toggle it closed and the outside-click handler knows
// what counts as "inside".
let activeHistoryIcon = null;

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

function getSelectedClientName() {
    return localStorage.getItem(STORAGE_NAME) || 'Unknown Client';
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
    if (start && end) {
        if (start <= end) {
            return { start, end };
        } else {
            showStatus('⚠️ Start date must be before or equal to end date.', 'error');
            return null;
        }
    }
    return null;
}

function isSunday(dateStr) {
    if (!dateStr) return false;
    try {
        const d = new Date(dateStr);
        return d.getDay() === 0;
    } catch {
        return false;
    }
}

function formatCellValue(value, columnName = '') {
    if (value === undefined || value === null || value === '') return '—';
    if (typeof value === 'number') {
        if (window.formatAmount) {
            return window.formatAmount(value);
        }
        return value.toString();
    }
    if (typeof value === 'string' && value.length > 100) {
        return value.substring(0, 100) + '…';
    }
    return value;
}

function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value === undefined || value === null ? '' : String(value);
    return div.innerHTML;
}

// ---------- ROW REVISION HISTORY POPOVER (Phase 3) ----------
// Click-triggered popover on the S.No column's history icon. Fetches from
// the Phase 1 endpoint and renders generically off the "changes" array,
// so it works for any template without hardcoding field names.
function hideRowHistoryPopover() {
    rowHistoryPopover.style.display = 'none';
    activeHistoryIcon = null;
}

function renderRowHistoryPopover(data) {
    if (!data.revisions || data.revisions.length === 0) {
        rowHistoryBody.innerHTML = '<div style="color:#94a3b8;">No revision history.</div>';
        return;
    }
    rowHistoryBody.innerHTML = data.revisions.map(rev => {
        const changeRows = (rev.changes || []).map(c => `
            <tr>
                <td class="field-name">${escapeHtml(c.field)}</td>
                <td class="value-change">
                    <span class="old-value">${escapeHtml(formatCellValue(c.old_value))}</span>
                    &rarr;
                    <span class="new-value">${escapeHtml(formatCellValue(c.new_value))}</span>
                </td>
            </tr>`).join('');
        return `
            <div class="revision-block">
                <div class="revision-meta">${escapeHtml(rev.action)} &middot; ${escapeHtml(rev.changed_at)} &middot; ${escapeHtml(rev.user)}</div>
                <table>${changeRows}</table>
            </div>`;
    }).join('');
}

async function showRowHistoryPopover(icon, clientDataId) {
    // Position near the icon, clamped so it stays within the viewport.
    const rect = icon.getBoundingClientRect();
    const popoverWidth = 340;
    let left = rect.left;
    if (left + popoverWidth > window.innerWidth) {
        left = Math.max(8, window.innerWidth - popoverWidth - 8);
    }
    rowHistoryPopover.style.left = left + 'px';

    // Flip above the icon when there isn't enough room below (matches the
    // popover's CSS max-height of 320px), so it's never cut off at the
    // bottom of the viewport. Anchoring with "bottom" when flipped lets it
    // grow upward without needing to know its rendered height in advance.
    const popoverMaxHeight = 320;
    const spaceBelow = window.innerHeight - rect.bottom;
    if (spaceBelow >= popoverMaxHeight + 6) {
        rowHistoryPopover.style.top = (rect.bottom + 6) + 'px';
        rowHistoryPopover.style.bottom = 'auto';
    } else {
        rowHistoryPopover.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
        rowHistoryPopover.style.top = 'auto';
    }

    rowHistoryBody.innerHTML = '<div style="color:#94a3b8;">Loading...</div>';
    rowHistoryPopover.style.display = 'block';
    activeHistoryIcon = icon;

    try {
        const res = await fetch(`${API_BASE}/api/data/rows/${clientDataId}/history`);
        if (!res.ok) throw new Error('Failed to fetch history');
        const data = await res.json();
        renderRowHistoryPopover(data);
    } catch (error) {
        rowHistoryBody.innerHTML = '<div style="color:#b91c1c;">Failed to load history.</div>';
    }
}

// ---------- DATE RANGE FILTER POPOVER (Phase B2) ----------
function openDateRangeFilterPopover() {
    dateRangeFilterPopover.style.display = 'block';
}

function closeDateRangeFilterPopover() {
    dateRangeFilterPopover.style.display = 'none';
}

function toggleDateRangeFilterPopover() {
    if (dateRangeFilterPopover.style.display === 'block') {
        closeDateRangeFilterPopover();
    } else {
        openDateRangeFilterPopover();
    }
}

// Reflects whether a From/To value is currently set on the trigger button,
// mirroring upload.js's active-state treatment for its Date Format popover.
function updateDateRangeButtonState() {
    const hasValue = !!(previewDateFromInput.value || previewDateToInput.value);
    dateRangeFilterBtn.classList.toggle('active', hasValue);
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

// ---------- CLEAR ALL FILTERS (Main Page) ----------
function clearAllFilters() {
    // 1. Reset filter fields to empty
    filterTemplate.value = '';
    filterDateStart.value = '';
    filterDateEnd.value = '';
    
    // 2. Clear the table and stats
    availabilityData = [];
    availabilityBody.innerHTML = '';
    noDataMessage.style.display = 'block';
    noDataMessage.innerHTML = 'Filters cleared. Select a new date range and click "Check Availability".';
    
    statsTotalRecords.textContent = '0';
    statsTotalTemplates.textContent = '0';
    statsMissingTemplates.textContent = '0';
    statsSundayRows.textContent = '0';
    
    // 3. Show status message
    showStatus('🧹 Filters cleared. Please select a new date range and click "Check Availability".', 'info');
}

// ---------- FETCH AVAILABILITY DATA ----------
async function fetchAvailability() {
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ Please select a client from the Dashboard first.', 'error');
        availabilityBody.innerHTML = '';
        noDataMessage.style.display = 'block';
        noDataMessage.innerHTML = '⚠️ No client selected. Please go to the Dashboard and select a client.';
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
    isAvailabilityLoading = true;
    checkBtn.disabled = true;
    checkBtn.textContent = '⏳ Loading...';
    refreshBtn.disabled = true;
    showStatus('⏳ Fetching availability data...', 'loading');
    
    try {
        const res = await fetch(`${API_BASE}/api/data/availability?${params.toString()}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch availability');
        }
        const data = await res.json();
        availabilityData = data;
        
                // Update stats - use total_row_count (unfiltered) for Total Records
        const totalRows = data.reduce((sum, item) => sum + (item.total_row_count || 0), 0);
        const totalTemplates = data.length;
        const missing = data.filter(item => item.status === 'missing').length;
        const sundayRows = data.reduce((sum, item) => sum + (item.filtered_sunday_count || 0), 0);
        
        statsTotalRecords.textContent = totalRows;
        statsTotalTemplates.textContent = totalTemplates;
        statsMissingTemplates.textContent = missing;
        statsSundayRows.textContent = sundayRows;
        
        renderAvailabilityTable(data);
        
        if (totalTemplates === 0) {
            showStatus('ℹ️ No templates found for the selected criteria.', 'info');
        } else {
            showStatus(`✅ Found ${totalTemplates} template(s) with ${totalRows} total rows.`, 'success');
        }
        
    } catch (error) {
        console.error('Fetch availability error:', error);
        showStatus('❌ Error: ' + error.message, 'error');
        availabilityBody.innerHTML = '';
        noDataMessage.style.display = 'block';
        noDataMessage.innerHTML = '❌ Error loading data. Please try again.';
        statsTotalRecords.textContent = '0';
        statsTotalTemplates.textContent = '0';
        statsMissingTemplates.textContent = '0';
        statsSundayRows.textContent = '0';
    } finally {
        isAvailabilityLoading = false;
        checkBtn.disabled = false;
        checkBtn.textContent = '🔍 Check Availability';
        refreshBtn.disabled = false;
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
        
        // Date Range - FULL range (unfiltered)
        const tdDate = document.createElement('td');
        if (item.date_min && item.date_max) {
            tdDate.textContent = `${formatDateForDisplay(item.date_min)} – ${formatDateForDisplay(item.date_max)}`;
        } else {
            tdDate.textContent = '—';
            tdDate.style.color = '#94a3b8';
        }
        // Add a small note if filtered range is narrower
        tr.appendChild(tdDate);
        
        // Total Rows (all data, unfiltered)
        const tdTotalRows = document.createElement('td');
        tdTotalRows.textContent = item.total_row_count || 0;
        tdTotalRows.style.textAlign = 'center';
        tdTotalRows.style.fontWeight = '600';
        tdTotalRows.setAttribute('data-tooltip', 'Total rows across all dates for this template');
        tr.appendChild(tdTotalRows);
        
        // Filtered Rows (matching the selected date range)
        const tdFilteredRows = document.createElement('td');
        tdFilteredRows.textContent = item.filtered_row_count || 0;
        tdFilteredRows.style.textAlign = 'center';
        tdFilteredRows.style.color = '#3b82f6';
        tdFilteredRows.style.fontWeight = '500';
        tdFilteredRows.setAttribute('data-tooltip', 'Rows matching the selected date filter');
        tr.appendChild(tdFilteredRows);
        
        // Sunday Rows (filtered)
        const tdSunday = document.createElement('td');
        tdSunday.textContent = item.filtered_sunday_count || 0;
        tdSunday.style.textAlign = 'center';
        if (item.filtered_sunday_count > 0) {
            tdSunday.style.color = '#f59e0b';
            tdSunday.style.fontWeight = '600';
        }
        tdSunday.setAttribute('data-tooltip', 'Sunday rows within the selected date range');
        tr.appendChild(tdSunday);
        
        // Status (based on total data)
        const tdStatus = document.createElement('td');
        tdStatus.style.textAlign = 'center';
        const statusSpan = document.createElement('span');
        // Use status from API ('complete' if total_row_count > 0, else 'missing')
        const status = item.status || 'missing';
        statusSpan.className = `status-badge ${status}`;
        const statusMap = {
            'complete': '✅ Complete',
            'missing': '❌ Missing',
            'partial': '⚠️ Partial'
        };
        statusSpan.textContent = statusMap[status] || status;
        tdStatus.appendChild(statusSpan);
        // Add a tooltip showing filtered status
        if (item.filtered_status === 'missing' && status === 'complete') {
            statusSpan.style.cursor = 'help';
            statusSpan.setAttribute('data-tooltip', 'Data exists but outside the selected date range');
        }
        tr.appendChild(tdStatus);
        
                // Actions
        const tdActions = document.createElement('td');
        tdActions.style.textAlign = 'center';
        tdActions.style.whiteSpace = 'nowrap';
        
        // Preview button - enabled if there's ANY data (total_row_count > 0)
        if (item.total_row_count > 0) {
            const previewBtn = document.createElement('button');
            previewBtn.className = 'action-btn preview';
            previewBtn.textContent = '👁️';
            previewBtn.title = 'Preview Data';
            previewBtn.setAttribute('data-tooltip', 'Preview data for this template');
            previewBtn.addEventListener('click', () => {
                openPreview(item.template_id, item.template_name);
            });
            tdActions.appendChild(previewBtn);
            
            // Export button
            const exportBtn = document.createElement('button');
            exportBtn.className = 'action-btn export';
            exportBtn.textContent = '📥';
            exportBtn.title = 'Export to Excel';
            exportBtn.setAttribute('data-tooltip', 'Export all data for this template');
            exportBtn.style.marginLeft = '6px';
            exportBtn.addEventListener('click', () => {
                exportDataset(item.template_id, item.template_name);
            });
            tdActions.appendChild(exportBtn);

            // Delete button
            const deleteBtn = document.createElement('button');
            deleteBtn.className = 'action-btn delete';
            deleteBtn.textContent = '🗑️';
            deleteBtn.title = 'Delete all data for this template';
            deleteBtn.setAttribute('data-tooltip', 'Delete all imported data for this template');
            deleteBtn.style.marginLeft = '6px';
            deleteBtn.style.color = '#dc2626';
            deleteBtn.addEventListener('click', () => {
                deleteTemplateData(item.template_id, item.template_name, item.total_row_count);
            });
            tdActions.appendChild(deleteBtn);
        } else {
            tdActions.textContent = '—';
            tdActions.style.color = '#94a3b8';
        }
        
        tr.appendChild(tdActions);
        availabilityBody.appendChild(tr);
    });
    
    // Update stats using the new fields
    const totalAllRows = data.reduce((sum, item) => sum + (item.total_row_count || 0), 0);
    const totalFilteredRows = data.reduce((sum, item) => sum + (item.filtered_row_count || 0), 0);
    const totalTemplates = data.length;
    const missing = data.filter(item => item.status === 'missing').length;
    const sundayRows = data.reduce((sum, item) => sum + (item.filtered_sunday_count || 0), 0);
    
    statsTotalRecords.textContent = totalAllRows;  // Show total rows, not filtered
    statsTotalTemplates.textContent = totalTemplates;
    statsMissingTemplates.textContent = missing;
    statsSundayRows.textContent = sundayRows;
}
async function deleteTemplateData(templateId, templateName, rowCount) {
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected.', 'error');
        return;
    }
    
    // Confirm deletion
    const confirmMsg = `⚠️ Are you sure you want to delete ALL ${rowCount} rows for template "${templateName}"?\n\n` +
                       `This will permanently remove this data from the system (soft-delete).\n` +
                       `Templates and columns will NOT be affected.`;
    if (!confirm(confirmMsg)) {
        return;
    }
    
    try {
        showStatus('⏳ Deleting data...', 'loading');
        const res = await fetch(`${API_BASE}/api/data/template/${templateId}?client_id=${clientId}`, {
            method: 'DELETE'
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to delete data');
        }
        const result = await res.json();
        showStatus(`✅ ${result.message}`, 'success');
        // Refresh the availability table
        await fetchAvailability();
    } catch (error) {
        console.error('Delete error:', error);
        showStatus('❌ Error deleting data: ' + error.message, 'error');
    }
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
    
    // Check if client is still selected
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected. Please select a client from the Dashboard.', 'error');
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
    previewTitle.textContent = `📋 ${templateName} – Preview (${getSelectedClientName()})`;
    sundayOnlyCheckbox.checked = false;
    searchInput.value = '';
    amountMin.value = '';
    amountMax.value = '';
    dataQualityFilter.value = '';
    previewDateFromInput.value = '';
    previewDateToInput.value = '';
    updateDateRangeButtonState();
    closeDateRangeFilterPopover();
    sundayCountBadge.textContent = 'Sunday rows: 0';
    
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
    previewCountInfo.textContent = 'Loading...';
    
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
    if (dataQualityFilter.value) {
        params.append('data_quality', dataQualityFilter.value);
    }
    // Date range filter (Phase B2): additive narrowing on top of the
    // mandatory date_start/date_end range already in params above.
    if (previewDateFromInput.value) {
        params.append('date_from', previewDateFromInput.value);
    }
    if (previewDateToInput.value) {
        params.append('date_to', previewDateToInput.value);
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
            previewCountInfo.textContent = '0 rows';
        }
        
    } catch (error) {
        console.error('Preview fetch error:', error);
        showStatus('❌ Error loading preview: ' + error.message, 'error');
        previewTableBody.innerHTML = `<tr><td colspan="100" style="text-align:center; padding:40px; color:#dc2626;">Error loading data. Please try again.</td></tr>`;
        previewCountInfo.textContent = 'Error loading';
    } finally {
        isPreviewLoading = false;
    }
}

// ---------- RENDER PREVIEW ROWS ----------
function renderPreviewRows(rows) {
    hideRowHistoryPopover();
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
        
        // Add Serial Number column header
        const thSerial = document.createElement('th');
        thSerial.textContent = 'S.No';
        thSerial.style.padding = '8px 12px';
        thSerial.style.textAlign = 'center';
        thSerial.style.borderBottom = '2px solid #e2e8f0';
        thSerial.style.whiteSpace = 'nowrap';
        thSerial.style.position = 'sticky';
        thSerial.style.top = '0';
        thSerial.style.background = '#f8fafc';
        thSerial.style.width = '60px';
        tr.appendChild(thSerial);
        
        headers.forEach(h => {
            const th = document.createElement('th');
            th.textContent = h;
            th.style.padding = '8px 12px';
            th.style.textAlign = 'left';
            th.style.borderBottom = '2px solid #e2e8f0';
            th.style.whiteSpace = 'nowrap';
            th.style.position = 'sticky';
            th.style.top = '0';
            th.style.background = '#f8fafc';
            tr.appendChild(th);
        });
        previewTableHead.appendChild(tr);
    }
    
    // Render body rows with serial numbers
    previewTableBody.innerHTML = '';
    const startSerial = (currentPage - 1) * previewLimit + 1;
    rows.forEach((row, index) => {
        const tr = document.createElement('tr');
        // Check if this row's extract_date is Sunday
        const isSun = isSunday(row.extract_date);
        if (isSun) {
            tr.className = 'sunday-highlight';
        }
        
        // Serial number column
        const tdSerial = document.createElement('td');
        tdSerial.style.padding = '6px 12px';
        tdSerial.style.borderBottom = '1px solid #f0f2f5';
        tdSerial.style.textAlign = 'center';
        tdSerial.style.color = '#64748b';
        tdSerial.textContent = startSerial + index;

        // Revision history icon: only shown for rows overwritten during a re-upload.
        if (row.has_history) {
            const historyIcon = document.createElement('span');
            historyIcon.className = 'history-icon';
            historyIcon.textContent = '⚠️';
            historyIcon.title = `${row.revision_count} revision(s)`;
            // Click-to-open/toggle instead of hover: hover closed the popover
            // as soon as the mouse moved toward it, before it could be read.
            historyIcon.addEventListener('click', (e) => {
                e.stopPropagation();
                if (activeHistoryIcon === historyIcon) {
                    hideRowHistoryPopover();
                } else {
                    showRowHistoryPopover(historyIcon, row.id);
                }
            });
            tdSerial.appendChild(historyIcon);
        }

        // Data quality flag icon: only shown for rows force-imported with a
        // blank Document ID/Amount/Date (via the Upload page's override).
        if (row.data_quality_flag === 'incomplete') {
            const qualityIcon = document.createElement('span');
            qualityIcon.className = 'quality-flag-icon';
            qualityIcon.textContent = '🚩';
            qualityIcon.title = 'Imported with missing data (blank Document ID/Amount/Date)';
            tdSerial.appendChild(qualityIcon);
        }

        tr.appendChild(tdSerial);
        
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
    if (!isPreviewOpen) return;
    // Client-side guard mirroring getDateRangeFromFilter()'s start<=end check
    // for the mandatory range — gives a clearer message than waiting on the
    // backend's own 400 for date_from > date_to.
    if (previewDateFromInput.value && previewDateToInput.value &&
        previewDateFromInput.value > previewDateToInput.value) {
        showStatus('⚠️ "From" date must be before or equal to "To" date.', 'error');
        return;
    }
    currentPage = 1;
    fetchPreviewData();
}

// ---------- CLEAR PREVIEW FILTERS ----------
function clearPreviewFilters() {
    if (!isPreviewOpen) return;
    searchInput.value = '';
    amountMin.value = '';
    amountMax.value = '';
    sundayOnlyCheckbox.checked = false;
    dataQualityFilter.value = '';
    previewDateFromInput.value = '';
    previewDateToInput.value = '';
    updateDateRangeButtonState();
    closeDateRangeFilterPopover();
    currentPage = 1;
    fetchPreviewData();
}

// ---------- EXPORT DATASET ----------
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
        const maxRows = 10000;
        
        const params = new URLSearchParams({
            client_id: clientId,
            template_id: templateId,
            date_start: dateRange.start,
            date_end: dateRange.end,
            limit: limit
        });
        
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
clearFiltersBtn.addEventListener('click', clearAllFilters);
refreshBtn.addEventListener('click', fetchAvailability);

filterDateStart.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchAvailability(); });
filterDateEnd.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchAvailability(); });
filterTemplate.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchAvailability(); });

// Preview modal events
closePreviewBtn.addEventListener('click', () => {
    previewModal.style.display = 'none';
    isPreviewOpen = false;
    hideRowHistoryPopover();
});
previewModal.addEventListener('click', (e) => {
    if (e.target === previewModal) {
        previewModal.style.display = 'none';
        isPreviewOpen = false;
        hideRowHistoryPopover();
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

// Preview filters
applyFiltersBtn.addEventListener('click', applyPreviewFilters);
// Modal clear button (using the updated ID from the HTML fix)
const modalClearBtn = document.getElementById('clearPreviewFiltersBtn');
if (modalClearBtn) {
    modalClearBtn.addEventListener('click', clearPreviewFilters);
}

// The main clear button is already handled.

// Sunday checkbox
sundayOnlyCheckbox.addEventListener('change', function() {
    if (isPreviewOpen) {
        currentPage = 1;
        fetchPreviewData();
    }
});

// Data quality filter (All / Complete / Incomplete)
dataQualityFilter.addEventListener('change', function() {
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

// Date range filter popover (Phase B2)
dateRangeFilterBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleDateRangeFilterPopover();
});
clearDateRangeFilterBtn.addEventListener('click', () => {
    previewDateFromInput.value = '';
    previewDateToInput.value = '';
    updateDateRangeButtonState();
    closeDateRangeFilterPopover();
    if (isPreviewOpen) {
        currentPage = 1;
        fetchPreviewData();
    }
});
// Enter-to-apply, same convention as amountMin/amountMax above.
previewDateFromInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && isPreviewOpen) {
        applyPreviewFilters();
        updateDateRangeButtonState();
        closeDateRangeFilterPopover();
    }
});
previewDateToInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && isPreviewOpen) {
        applyPreviewFilters();
        updateDateRangeButtonState();
        closeDateRangeFilterPopover();
    }
});
// Apply button also picks up date_from/date_to via fetchPreviewData(), so
// its own click should refresh the trigger's active-state styling too.
applyFiltersBtn.addEventListener('click', updateDateRangeButtonState);

// Close the row history popover when clicking anywhere outside it (the icon's
// own click is stopped from propagating here, so this only fires for clicks
// elsewhere on the page). Also closes the date-range filter popover (Phase B2)
// on the same outside-click check — its trigger button's own click already
// stops propagation, so a click on it is never treated as "outside".
document.addEventListener('click', (e) => {
    if (rowHistoryPopover.style.display === 'block' && !rowHistoryPopover.contains(e.target)) {
        hideRowHistoryPopover();
    }
    if (dateRangeFilterPopover.style.display === 'block' && !dateRangeFilterPopover.contains(e.target)) {
        closeDateRangeFilterPopover();
    }
});

// ESC key closes preview (or just the date-range popover, if that's open)
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && dateRangeFilterPopover.style.display === 'block') {
        closeDateRangeFilterPopover();
        return;
    }
    if (e.key === 'Escape' && isPreviewOpen) {
        previewModal.style.display = 'none';
        isPreviewOpen = false;
    }
    // Ctrl+Enter triggers check availability
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        fetchAvailability();
    }
});

// ---------- CLIENT CHANGE HOOK (with debounce) ----------
let clientChangeTimeout;
window.onClientChange = function(clientId) {
    clearTimeout(clientChangeTimeout);
    clientChangeTimeout = setTimeout(() => {
        if (clientId) {
            if (filterDateStart.value && filterDateEnd.value) {
                fetchAvailability();
            } else {
                setDefaultDateRange();
                fetchAvailability();
            }
            // If preview is open, close it
            if (isPreviewOpen) {
                previewModal.style.display = 'none';
                isPreviewOpen = false;
            }
        }
    }, 300);
};

// ---------- INIT ----------
window.onload = async function() {
    // Load utilities first
    if (typeof initUtils === 'function') {
        await initUtils();
    }
    
    setDefaultDateRange();
    await loadTemplateDropdown();
    
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ Please select a client from the Dashboard first.', 'error');
        availabilityBody.innerHTML = '';
        noDataMessage.style.display = 'block';
        noDataMessage.innerHTML = '⚠️ No client selected. Please go to the Dashboard and select a client.';
        setTimeout(() => {
            window.location.href = '/static/index.html';
        }, 3000);
        return;
    }
    
    fetchAvailability();
};