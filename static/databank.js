// ==============================================
// 📊 CLIENT DATA BANK - LOGIC (with Global Formatting)
// ==============================================

// API_BASE is defined in client_context.js
// STORAGE_KEY is defined globally in client_context.js

let currentDatasets = [];
let currentClientId = null;
let previewUploadId = null;
let previewPage = 1;
let previewTotalRows = 0;
let previewLimit = 50;

// ---------- DECIMAL OVERRIDE PER COLUMN (Spin Buttons) ----------
let columnDecimals = {}; // { columnName: decimalPlaces }

// DOM refs
const datasetsBody = document.getElementById('datasetsBody');
const noDataMessage = document.getElementById('noDataMessage');
const totalRecordsSpan = document.getElementById('totalRecords');
const totalUploadsSpan = document.getElementById('totalUploads');
const lastUploadSpan = document.getElementById('lastUpload');
const statusFilter = document.getElementById('statusFilter');
const uploadNewBtn = document.getElementById('uploadNewBtn');
const statusDiv = document.getElementById('statusMessage');

const previewModal = document.getElementById('previewModal');
const previewTitle = document.getElementById('previewTitle');
const previewTableHead = document.getElementById('previewTableHead');
const previewTableBody = document.getElementById('previewTableBody');
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

// ---------- FORMATTING HELPERS (using global utils) ----------
function formatCell(value, columnName = '') {
    if (value === undefined || value === null || value === '') return '—';
    if (typeof value === 'number') {
        // Check if column name suggests amount
        const amountKeywords = ['amount', 'total', 'debit', 'credit', 'balance', 'price', 'value', 'net', 'tax', 'fee', 'amt'];
        const isAmount = amountKeywords.some(kw => columnName.toLowerCase().includes(kw));
        if (isAmount) {
            // Check if we have a per-column decimal override
            const dec = columnDecimals[columnName] !== undefined ? columnDecimals[columnName] : null;
            return window.formatAmount ? window.formatAmount(value, dec) : value.toString();
        }
        return value;
    }
    return value;
}

// ---------- LOAD DATASETS ----------
async function loadDatasets() {
    const clientId = getSelectedClientId();
    if (!clientId) {
        datasetsBody.innerHTML = '';
        noDataMessage.style.display = 'block';
        noDataMessage.innerHTML = '⚠️ Please select a client from the Dashboard first.';
        totalRecordsSpan.textContent = '0';
        totalUploadsSpan.textContent = '0';
        lastUploadSpan.textContent = 'Never';
        return;
    }
    currentClientId = clientId;

    const filter = statusFilter.value;

    try {
        const res = await fetch(`${API_BASE}/api/data/datasets?client_id=${clientId}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to load datasets');
        }
        const data = await res.json();

        let filtered = data;
        if (filter !== 'all') {
            filtered = data.filter(d => d.status === filter);
        }
        currentDatasets = filtered;

        renderDatasets(filtered);
        updateStats(data);
    } catch (error) {
        showStatus('❌ Error loading datasets: ' + error.message, 'error');
        datasetsBody.innerHTML = '';
        noDataMessage.style.display = 'block';
        noDataMessage.innerHTML = 'Error loading data. Please try again.';
    }
}

// ---------- RENDER DATASETS ----------
function renderDatasets(datasets) {
    datasetsBody.innerHTML = '';
    if (!datasets || datasets.length === 0) {
        noDataMessage.style.display = 'block';
        noDataMessage.innerHTML = `
            No data sets found for this client.
            <button id="goUploadBtn" class="btn-upload" style="background:#3b82f6; padding:6px 18px; font-size:14px; margin-top:10px;">Upload Data</button>
        `;
        document.getElementById('goUploadBtn')?.addEventListener('click', () => {
            window.location.href = '/static/upload.html';
        });
        return;
    }
    noDataMessage.style.display = 'none';

    datasets.forEach((ds, index) => {
        const tr = document.createElement('tr');

        // # (row number)
        const tdNum = document.createElement('td');
        tdNum.textContent = index + 1;
        tdNum.style.textAlign = 'center';
        tr.appendChild(tdNum);

        // Template Name
        const tdTemplate = document.createElement('td');
        tdTemplate.textContent = ds.template_name || 'Unknown';
        tdTemplate.style.fontWeight = '500';
        tr.appendChild(tdTemplate);

        // File Name
        const tdFile = document.createElement('td');
        tdFile.textContent = ds.file_name || '—';
        tdFile.style.color = '#64748b';
        tr.appendChild(tdFile);

        // Upload Date (formatted)
        const tdDate = document.createElement('td');
        tdDate.textContent = window.formatDate ? window.formatDate(ds.upload_date) : ds.upload_date || '—';
        tr.appendChild(tdDate);

        // Rows
        const tdRows = document.createElement('td');
        tdRows.textContent = ds.record_count || 0;
        tdRows.style.textAlign = 'center';
        tr.appendChild(tdRows);

        // Status
        const tdStatus = document.createElement('td');
        const statusSpan = document.createElement('span');
        statusSpan.className = `status-badge ${ds.status.toLowerCase()}`;
        statusSpan.textContent = ds.status || 'Pending';
        tdStatus.appendChild(statusSpan);
        tr.appendChild(tdStatus);

        // Actions
        const tdActions = document.createElement('td');
        tdActions.style.textAlign = 'center';
        tdActions.style.whiteSpace = 'nowrap';

        if (ds.status === 'Imported' && ds.record_count > 0) {
            const viewBtn = document.createElement('button');
            viewBtn.className = 'action-btn view';
            viewBtn.textContent = '👁️';
            viewBtn.title = 'View Data';
            viewBtn.addEventListener('click', () => openPreview(ds.upload_id, ds.template_name));
            tdActions.appendChild(viewBtn);

            const exportBtn = document.createElement('button');
            exportBtn.className = 'action-btn export';
            exportBtn.textContent = '📥';
            exportBtn.title = 'Export to Excel';
            exportBtn.style.marginLeft = '6px';
            exportBtn.addEventListener('click', () => exportDataset(ds.upload_id, ds.template_name));
            tdActions.appendChild(exportBtn);
        }

        if (ds.status === 'Deleted') {
            const restoreBtn = document.createElement('button');
            restoreBtn.className = 'action-btn restore';
            restoreBtn.textContent = '↩️';
            restoreBtn.title = 'Restore Dataset';
            restoreBtn.addEventListener('click', () => confirmRestore(ds.upload_id, ds.file_name));
            tdActions.appendChild(restoreBtn);
        } else if (ds.status !== 'Pending') {
            const deleteBtn = document.createElement('button');
            deleteBtn.className = 'action-btn delete';
            deleteBtn.textContent = '🗑️';
            deleteBtn.title = 'Delete Dataset';
            deleteBtn.addEventListener('click', () => confirmDelete(ds.upload_id, ds.file_name));
            tdActions.appendChild(deleteBtn);
        }

        tr.appendChild(tdActions);
        datasetsBody.appendChild(tr);
    });

    const goUploadBtn = document.getElementById('goUploadBtn');
    if (goUploadBtn) {
        goUploadBtn.addEventListener('click', () => {
            window.location.href = '/static/upload.html';
        });
    }
}

// ---------- UPDATE STATS ----------
function updateStats(allDatasets) {
    const totalRecords = allDatasets.reduce((sum, ds) => sum + (ds.record_count || 0), 0);
    const totalUploads = allDatasets.length;
    let lastUpload = 'Never';
    if (allDatasets.length > 0) {
        const dates = allDatasets.map(d => d.upload_date).filter(d => d);
        if (dates.length > 0) {
            dates.sort((a, b) => new Date(b) - new Date(a));
            lastUpload = window.formatDate ? window.formatDate(dates[0]) : dates[0];
        }
    }
    totalRecordsSpan.textContent = totalRecords;
    totalUploadsSpan.textContent = totalUploads;
    lastUploadSpan.textContent = lastUpload || 'Never';
}

// ---------- PREVIEW MODAL ----------
async function openPreview(uploadId, templateName) {
    previewUploadId = uploadId;
    previewPage = 1;
    previewTitle.textContent = `📋 ${templateName || 'Dataset'} Preview`;
    previewModal.classList.add('active');
    await loadPreviewPage();
}

async function loadPreviewPage() {
    if (!previewUploadId) return;
    try {
        const searchVal = document.getElementById('searchInput')?.value || '';
        const amountMin = document.getElementById('amountMin')?.value || '';
        const amountMax = document.getElementById('amountMax')?.value || '';
        const dateStart = document.getElementById('dateStart')?.value || '';
        const dateEnd = document.getElementById('dateEnd')?.value || '';

        let url = `${API_BASE}/api/data/datasets/${previewUploadId}/rows?page=${previewPage}&limit=${previewLimit}`;
        if (searchVal) url += `&search=${encodeURIComponent(searchVal)}`;
        if (amountMin) url += `&amount_min=${encodeURIComponent(amountMin)}`;
        if (amountMax) url += `&amount_max=${encodeURIComponent(amountMax)}`;
        if (dateStart) url += `&date_start=${encodeURIComponent(dateStart)}`;
        if (dateEnd) url += `&date_end=${encodeURIComponent(dateEnd)}`;

        const res = await fetch(url);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch rows');
        }
        const data = await res.json();
        previewTotalRows = data.total_rows;
        const rows = data.rows || [];

        renderPreviewRows(rows);
        updatePagination();

        if (rows.length === 0 && previewTotalRows === 0) {
            previewTableBody.innerHTML = '<tr><td colspan="100" style="text-align:center; padding:40px; color:#94a3b8;">No data found.</td></tr>';
        }
    } catch (error) {
        showStatus('❌ Error loading preview: ' + error.message, 'error');
        previewTableBody.innerHTML = '<tr><td colspan="100" style="text-align:center; padding:40px; color:#dc2626;">Error loading data.</td></tr>';
    }
}

function renderPreviewRows(rows) {
    if (!rows || rows.length === 0) {
        previewTableBody.innerHTML = '<tr><td colspan="100" style="text-align:center; padding:40px; color:#94a3b8;">No rows to display.</td></tr>';
        previewTableHead.innerHTML = '';
        return;
    }

    // Build dynamic headers from first row's keys
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
            
            // Check if this is a numeric column (for spin buttons)
            const isNumeric = rows.some(row => typeof row.row_data[h] === 'number');
            if (isNumeric) {
                // Add spin buttons
                const btnContainer = document.createElement('span');
                btnContainer.style.marginLeft = '6px';
                btnContainer.style.display = 'inline-block';
                
                const upBtn = document.createElement('button');
                upBtn.textContent = '▲';
                upBtn.style.cssText = 'background:none; border:none; cursor:pointer; font-size:10px; color:#64748b; padding:0 2px; display:block;';
                upBtn.title = 'Increase decimals';
                upBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const current = columnDecimals[h] !== undefined ? columnDecimals[h] : 0;
                    columnDecimals[h] = Math.min(current + 1, 4);
                    loadPreviewPage();
                });
                
                const downBtn = document.createElement('button');
                downBtn.textContent = '▼';
                downBtn.style.cssText = 'background:none; border:none; cursor:pointer; font-size:10px; color:#64748b; padding:0 2px; display:block;';
                downBtn.title = 'Decrease decimals';
                downBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const current = columnDecimals[h] !== undefined ? columnDecimals[h] : 0;
                    columnDecimals[h] = Math.max(current - 1, 0);
                    loadPreviewPage();
                });
                
                btnContainer.appendChild(upBtn);
                btnContainer.appendChild(downBtn);
                th.appendChild(btnContainer);
                
                // Show current decimal count
                const decBadge = document.createElement('span');
                const currentDec = columnDecimals[h] !== undefined ? columnDecimals[h] : 0;
                decBadge.textContent = ` ${currentDec}`;
                decBadge.style.cssText = 'font-size:9px; color:#94a3b8; margin-left:2px; font-weight:400;';
                th.appendChild(decBadge);
            }
            
            tr.appendChild(th);
        });
        previewTableHead.appendChild(tr);
    }

    previewTableBody.innerHTML = '';
    rows.forEach(row => {
        const tr = document.createElement('tr');
        headers.forEach(h => {
            const td = document.createElement('td');
            td.style.padding = '6px 12px';
            td.style.borderBottom = '1px solid #f0f2f5';
            const value = row.row_data[h] !== undefined ? row.row_data[h] : '';
            td.textContent = formatCell(value, h);
            tr.appendChild(td);
        });
        previewTableBody.appendChild(tr);
    });

    previewCountInfo.textContent = `Showing ${rows.length} of ${previewTotalRows} rows`;
}

function updatePagination() {
    const totalPages = Math.ceil(previewTotalRows / previewLimit) || 1;
    pageInfo.textContent = `Page ${previewPage} of ${totalPages}`;
    prevPageBtn.disabled = previewPage <= 1;
    nextPageBtn.disabled = previewPage >= totalPages;
}

// ---------- PREVIEW PAGINATION ----------
prevPageBtn.addEventListener('click', () => {
    if (previewPage > 1) {
        previewPage--;
        loadPreviewPage();
    }
});

nextPageBtn.addEventListener('click', () => {
    const totalPages = Math.ceil(previewTotalRows / previewLimit);
    if (previewPage < totalPages) {
        previewPage++;
        loadPreviewPage();
    }
});

closePreviewBtn.addEventListener('click', () => {
    previewModal.classList.remove('active');
    // Reset column decimals when closing preview
    columnDecimals = {};
});

previewModal.addEventListener('click', function(e) {
    if (e.target === this) {
        this.classList.remove('active');
        columnDecimals = {};
    }
});

// ---------- FILTER EVENT HANDLERS ----------
const applyFiltersBtn = document.getElementById('applyFiltersBtn');
const clearFiltersBtn = document.getElementById('clearFiltersBtn');
if (applyFiltersBtn) {
    applyFiltersBtn.addEventListener('click', function() {
        previewPage = 1;
        loadPreviewPage();
    });
}
if (clearFiltersBtn) {
    clearFiltersBtn.addEventListener('click', function() {
        const searchInput = document.getElementById('searchInput');
        const amountMin = document.getElementById('amountMin');
        const amountMax = document.getElementById('amountMax');
        const dateStart = document.getElementById('dateStart');
        const dateEnd = document.getElementById('dateEnd');
        if (searchInput) searchInput.value = '';
        if (amountMin) amountMin.value = '';
        if (amountMax) amountMax.value = '';
        if (dateStart) dateStart.value = '';
        if (dateEnd) dateEnd.value = '';
        previewPage = 1;
        loadPreviewPage();
    });
}

// ---------- DELETE / RESTORE ----------
async function confirmDelete(uploadId, fileName) {
    if (!confirm(`Are you sure you want to delete dataset "${fileName}"? This action can be undone.`)) return;
    try {
        const res = await fetch(`${API_BASE}/api/data/datasets/${uploadId}`, { method: 'DELETE' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.message || 'Delete failed');
        showStatus('✅ ' + result.message, 'success');
        loadDatasets();
    } catch (error) {
        showStatus('❌ Error deleting: ' + error.message, 'error');
    }
}

async function confirmRestore(uploadId, fileName) {
    if (!confirm(`Restore dataset "${fileName}"?`)) return;
    try {
        const res = await fetch(`${API_BASE}/api/data/datasets/${uploadId}/restore`, { method: 'POST' });
        const result = await res.json();
        if (!res.ok) throw new Error(result.message || 'Restore failed');
        showStatus('✅ ' + result.message, 'success');
        loadDatasets();
    } catch (error) {
        showStatus('❌ Error restoring: ' + error.message, 'error');
    }
}

// ---------- EXPORT DATASET TO EXCEL ----------
async function exportDataset(uploadId, templateName) {
    try {
        showStatus('⏳ Preparing export...', 'loading');
        let allRows = [];
        let page = 1;
        let totalPages = 1;
        const limit = 500;

        do {
            const url = `${API_BASE}/api/data/datasets/${uploadId}/rows?page=${page}&limit=${limit}`;
            const res = await fetch(url);
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Failed to fetch data');
            }
            const data = await res.json();
            const rows = data.rows || [];
            allRows = allRows.concat(rows);
            totalPages = Math.ceil(data.total_rows / limit);
            page++;
        } while (page <= totalPages && allRows.length < 10000);

        if (allRows.length === 0) {
            showStatus('⚠️ No data to export.', 'error');
            return;
        }

        const firstRow = allRows[0];
        const headers = firstRow.row_data ? Object.keys(firstRow.row_data) : [];
        const excelData = [headers];
        allRows.forEach(row => {
            const rowData = headers.map(h => row.row_data[h] !== undefined ? row.row_data[h] : '');
            excelData.push(rowData);
        });

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

// ---------- FILTER CHANGE ----------
statusFilter.addEventListener('change', loadDatasets);

// ---------- UPLOAD NEW ----------
uploadNewBtn.addEventListener('click', () => {
    window.location.href = '/static/upload.html';
});

// ---------- CLIENT CHANGE HOOK ----------
window.onClientChange = function(clientId) {
    if (clientId) {
        loadDatasets();
    }
};

// ---------- INIT ----------
window.onload = function() {
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ Please select a client from the Dashboard.', 'error');
        setTimeout(() => {
            window.location.href = '/static/index.html';
        }, 2000);
        return;
    }
    loadDatasets();
    console.log('📊 Data Bank loaded');
};