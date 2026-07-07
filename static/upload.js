// ==============================================
// 📤 UPLOAD MODULE - COMPLETE LOGIC
// ==============================================

const API_BASE = 'http://localhost:5000';

// DOM Elements
const templateDropdown = document.getElementById('templateDropdown');
const columnListDiv = document.getElementById('columnList');
const totalColumnsSpan = document.getElementById('totalColumnsCount');
const templateColumnsPanel = document.getElementById('templateColumnsPanel');

const uploadArea = document.getElementById('uploadArea');
const fileInput = document.getElementById('fileInput');
const uploadBtn = document.getElementById('uploadBtn');
const fileInfo = document.getElementById('fileInfo');
const fileName = document.getElementById('fileName');
const fileSize = document.getElementById('fileSize');
const clearBtn = document.getElementById('clearBtn');

const mappingPanel = document.getElementById('mappingPanel');
const mappingBody = document.getElementById('mappingBody');

const previewPanel = document.getElementById('previewPanel');
const previewHeader = document.getElementById('previewHeader');
const previewBody = document.getElementById('previewBody');

const downloadTemplateBtn = document.getElementById('downloadTemplateBtn');
const importBtn = document.getElementById('importBtn');
const resetBtn = document.getElementById('resetBtn');
const uploadStatus = document.getElementById('uploadStatus');

const uploadMode = document.getElementById('uploadMode');
const uniqueIdContainer = document.getElementById('uniqueIdContainer');
const uniqueIdColumn = document.getElementById('uniqueIdColumn');
const modePanel = document.getElementById('modePanel');

const sheetSelector = document.getElementById('sheetSelector');
const sheetSelectorContainer = document.getElementById('sheetSelectorContainer');

// State
let selectedTemplateId = null;
let selectedFile = null;
let fileId = null;
let currentMappings = [];
let excelHeaders = [];
let excelData = [];
let templateColumns = [];
let sheetNames = [];
let currentSheet = null;
let workbookData = null;

// ---------- LOAD TEMPLATES ----------
async function loadTemplates() {
    try {
        const res = await fetch(API_BASE + '/api/templates');
        if (!res.ok) throw new Error('Failed to load templates');
        const templates = await res.json();
        templateDropdown.innerHTML = '<option value="">-- Select Template --</option>';
        templates.forEach(tpl => {
            const opt = document.createElement('option');
            opt.value = tpl.id;
            opt.textContent = tpl.template_name;
            templateDropdown.appendChild(opt);
        });
    } catch (error) {
        console.error('Error loading templates:', error);
        showStatus('❌ Failed to load templates', 'error');
    }
}

// ---------- TEMPLATE SELECTION ----------
templateDropdown.addEventListener('change', async function() {
    const id = this.value;
    if (!id) {
        templateColumnsPanel.style.display = 'none';
        sheetSelectorContainer.style.display = 'none';
        selectedTemplateId = null;
        resetUpload();
        return;
    }
    selectedTemplateId = parseInt(id);
    try {
        const res = await fetch(API_BASE + `/api/templates/${id}/upload-columns`);
        if (!res.ok) throw new Error('Failed to fetch template columns');
        const data = await res.json();
        templateColumns = data.columns;
        renderTemplateColumns(data.columns);
        templateColumnsPanel.style.display = 'block';
        downloadTemplateBtn.dataset.templateId = id;
        downloadTemplateBtn.disabled = false;
        if (fileId && currentSheet) {
            await autoMapColumns(fileId);
        }
    } catch (error) {
        console.error('Error loading template columns:', error);
        showStatus('❌ Failed to load template columns', 'error');
    }
});

// ---------- RENDER TEMPLATE COLUMNS ----------
function renderTemplateColumns(columns) {
    columnListDiv.innerHTML = '';
    if (!columns || columns.length === 0) {
        columnListDiv.innerHTML = '<div style="color:#94a3b8; padding:10px;">No columns in this template.</div>';
        totalColumnsSpan.textContent = '0';
        return;
    }
    columns.forEach((col, idx) => {
        const div = document.createElement('div');
        div.style.cssText = 'display:flex; align-items:center; gap:8px; padding:6px 0; border-bottom:1px solid #f0f2f5;';
        const numSpan = document.createElement('span');
        numSpan.textContent = idx + 1;
        numSpan.style.cssText = 'font-weight:600; color:#64748b; min-width:24px;';
        const nameSpan = document.createElement('span');
        nameSpan.textContent = col.column_name;
        nameSpan.style.cssText = 'flex:1; font-size:14px;';
        const typeSpan = document.createElement('span');
        typeSpan.textContent = `(${col.data_type})`;
        typeSpan.style.cssText = 'color:#94a3b8; font-size:12px;';
        div.appendChild(numSpan);
        div.appendChild(nameSpan);
        div.appendChild(typeSpan);
        columnListDiv.appendChild(div);
    });
    totalColumnsSpan.textContent = columns.length;
}

// ---------- FILE UPLOAD ----------
uploadArea.addEventListener('click', () => fileInput.click());
uploadBtn.addEventListener('click', () => fileInput.click());

uploadArea.addEventListener('dragover', (e) => {
    e.preventDefault();
    uploadArea.classList.add('dragover');
});
uploadArea.addEventListener('dragleave', () => {
    uploadArea.classList.remove('dragover');
});
uploadArea.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadArea.classList.remove('dragover');
    if (e.dataTransfer.files.length) {
        handleFile(e.dataTransfer.files[0]);
    }
});

fileInput.addEventListener('change', function(e) {
    if (this.files && this.files.length > 0) {
        handleFile(this.files[0]);
        this.value = ''; // allow re-selection
    }
});

async function handleFile(file) {
    const validTypes = ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.ms-excel'];
    if (!validTypes.includes(file.type) && !file.name.endsWith('.xlsx') && !file.name.endsWith('.xls')) {
        showStatus('⚠️ Please select a valid Excel file (.xlsx or .xls)', 'error');
        return;
    }

    selectedFile = file;
    fileName.textContent = file.name;
    fileSize.textContent = (file.size / 1024).toFixed(1) + ' KB';
    fileInfo.style.display = 'flex';
    uploadArea.style.display = 'none';

    // Read the file to get sheet names
    const reader = new FileReader();
    reader.onload = function(e) {
        try {
            const data = new Uint8Array(e.target.result);
            workbookData = data;
            const workbook = XLSX.read(data, { type: 'array' });
            sheetNames = workbook.SheetNames;

            if (sheetNames.length === 0) {
                showStatus('❌ No sheets found in the file.', 'error');
                return;
            }

            // Populate sheet selector
            populateSheetSelector(sheetNames);

            // Auto-select first sheet
            currentSheet = sheetNames[0];
            sheetSelector.value = currentSheet;

            // Upload file to server
            uploadFileToServer(file);

            // Show sheet selector in left panel
            sheetSelectorContainer.style.display = 'block';

            showStatus('📄 File loaded. Sheet "' + currentSheet + '" selected.', 'info');
        } catch (err) {
            showStatus('❌ Error reading file: ' + err.message, 'error');
        }
    };
    reader.readAsArrayBuffer(file);
}

// ---------- POPULATE SHEET SELECTOR ----------
function populateSheetSelector(sheets) {
    sheetSelector.innerHTML = '<option value="">-- Select Sheet --</option>';
    sheets.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        sheetSelector.appendChild(opt);
    });
    sheetSelectorContainer.style.display = 'block';
}

// ---------- SHEET SELECTION CHANGE ----------
sheetSelector.addEventListener('change', function() {
    currentSheet = this.value;
    if (currentSheet && selectedTemplateId && fileId) {
        autoMapColumns(fileId);
        showStatus('📄 Sheet changed to "' + currentSheet + '". Re‑mapping...', 'info');
    } else if (currentSheet && selectedTemplateId && !fileId) {
        if (selectedFile) {
            uploadFileToServer(selectedFile);
        }
    }
});

// ---------- UPLOAD FILE TO SERVER ----------
async function uploadFileToServer(file) {
    const formData = new FormData();
    formData.append('file', file);
    try {
        const res = await fetch('/api/import-excel', {
            method: 'POST',
            body: formData
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.message || 'Upload failed');
        fileId = result.file_id;
        showStatus(`✅ File uploaded: ${result.record_count} rows`, 'success');

        if (selectedTemplateId && currentSheet) {
            await autoMapColumns(fileId);
        } else if (selectedTemplateId && !currentSheet) {
            showStatus('✅ File uploaded. Please select a sheet.', 'info');
        } else {
            showStatus('✅ File uploaded. Please select a template.', 'info');
        }
    } catch (error) {
        showStatus('❌ Upload error: ' + error.message, 'error');
        resetUpload();
    }
}

// ---------- CLEAR FILE ----------
clearBtn.addEventListener('click', resetUpload);

function resetUpload() {
    selectedFile = null;
    fileId = null;
    fileInfo.style.display = 'none';
    uploadArea.style.display = 'block';
    fileInput.value = '';
    mappingPanel.style.display = 'none';
    previewPanel.style.display = 'none';
    modePanel.style.display = 'none';
    sheetSelectorContainer.style.display = 'none';
    sheetSelector.innerHTML = '';
    currentSheet = null;
    sheetNames = [];
    workbookData = null;
    importBtn.disabled = true;
    currentMappings = [];
    excelHeaders = [];
    excelData = [];
    templateColumns = [];
    showStatus('', '');
}

// ---------- AUTO-MAP ----------
async function autoMapColumns(fileId) {
    if (!selectedTemplateId || !currentSheet) {
        showStatus('⚠️ Please select a template and a sheet.', 'error');
        return;
    }

    try {
        showStatus('⏳ Auto‑mapping columns...', 'loading');

        const res = await fetch(API_BASE + '/api/upload/auto-map', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_id: fileId,
                template_id: selectedTemplateId,
                sheet_name: currentSheet
            })
        });

        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Auto‑mapping failed');
        }

        const data = await res.json();
        currentMappings = data.mappings;
        excelHeaders = data.excel_headers;

        renderMapping(data.mappings);
        renderPreviewFromData();

        modePanel.style.display = 'block';
        importBtn.disabled = false;
        populateUniqueIdDropdown();

        showStatus(`✅ Auto‑mapped ${data.auto_mapped_count}/${data.total_columns} columns`, 'success');
    } catch (error) {
        console.error('Auto-map error:', error);
        showStatus('❌ Auto‑mapping failed: ' + error.message, 'error');
    }
}

// ---------- RENDER PREVIEW FROM WORKBOOK DATA ----------
function renderPreviewFromData() {
    if (!workbookData) return;
    try {
        const workbook = XLSX.read(workbookData, { type: 'array' });
        const sheetName = currentSheet || workbook.SheetNames[0];
        const sheet = workbook.Sheets[sheetName];
        const jsonData = XLSX.utils.sheet_to_json(sheet, { header: 1 });
        const headers = jsonData[0] || [];
        const rows = jsonData.slice(1, 6);
        renderPreviewTable(headers, rows);
    } catch (error) {
        console.error('Preview error:', error);
    }
}

// ---------- RENDER PREVIEW TABLE ----------
function renderPreviewTable(headers, rows) {
    previewPanel.style.display = 'block';
    previewHeader.innerHTML = '';
    headers.forEach(h => {
        const th = document.createElement('th');
        th.textContent = h || '—';
        th.style.padding = '6px 10px';
        th.style.textAlign = 'left';
        th.style.borderBottom = '1px solid #e2e8f0';
        previewHeader.appendChild(th);
    });
    previewBody.innerHTML = '';
    rows.forEach(row => {
        const tr = document.createElement('tr');
        row.forEach(cell => {
            const td = document.createElement('td');
            td.textContent = cell !== undefined ? cell : '';
            td.style.padding = '6px 10px';
            td.style.borderBottom = '1px solid #f0f2f5';
            tr.appendChild(td);
        });
        previewBody.appendChild(tr);
    });
}

// ---------- RENDER MAPPING TABLE ----------
function renderMapping(mappings) {
    mappingBody.innerHTML = '';
    if (!mappings || mappings.length === 0) {
        mappingPanel.style.display = 'none';
        return;
    }
    mappingPanel.style.display = 'block';
    mappings.forEach(m => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #f0f2f5';
        const td1 = document.createElement('td');
        td1.textContent = m.column_name;
        td1.style.padding = '8px 10px';
        td1.style.fontWeight = '500';
        const td2 = document.createElement('td');
        const select = document.createElement('select');
        select.style.cssText = 'padding:4px 8px; border:1px solid #ccc; border-radius:4px; width:100%;';
        const noOpt = document.createElement('option');
        noOpt.value = '';
        noOpt.textContent = '— None —';
        select.appendChild(noOpt);
        excelHeaders.forEach(h => {
            const opt = document.createElement('option');
            opt.value = h;
            opt.textContent = h;
            if (h === m.excel_column) opt.selected = true;
            select.appendChild(opt);
        });
        select.addEventListener('change', function() {
            const mapping = currentMappings.find(mm => mm.column_id === m.column_id);
            if (mapping) {
                mapping.excel_column = this.value || null;
                mapping.is_matched = !!this.value;
                const statusTd = tr.querySelector('td:last-child');
                if (statusTd) {
                    statusTd.textContent = mapping.is_matched ? '✅' : '❌';
                    statusTd.style.color = mapping.is_matched ? '#16a34a' : '#dc2626';
                }
            }
        });
        td2.appendChild(select);
        const td3 = document.createElement('td');
        td3.textContent = m.is_matched ? '✅' : '❌';
        td3.style.textAlign = 'center';
        td3.style.color = m.is_matched ? '#16a34a' : '#dc2626';
        tr.appendChild(td1);
        tr.appendChild(td2);
        tr.appendChild(td3);
        mappingBody.appendChild(tr);
    });
}

// ---------- POPULATE UNIQUE ID DROPDOWN ----------
function populateUniqueIdDropdown() {
    uniqueIdColumn.innerHTML = '<option value="">-- Select --</option>';
    const mappedColumns = currentMappings.filter(m => m.is_matched);
    mappedColumns.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.column_id;
        opt.textContent = m.column_name;
        uniqueIdColumn.appendChild(opt);
    });
    toggleUniqueIdVisibility();
}

// ---------- TOGGLE UNIQUE ID VISIBILITY ----------
function toggleUniqueIdVisibility() {
    if (uploadMode.value === 'update') {
        uniqueIdContainer.style.display = 'block';
    } else {
        uniqueIdContainer.style.display = 'none';
    }
}
uploadMode.addEventListener('change', toggleUniqueIdVisibility);

// ---------- DOWNLOAD TEMPLATE ----------
downloadTemplateBtn.addEventListener('click', function() {
    const templateId = this.dataset.templateId;
    if (!templateId) {
        showStatus('⚠️ Please select a template first', 'error');
        return;
    }
    window.open(API_BASE + `/api/upload/download-template/${templateId}`, '_blank');
});

// ---------- RESET ----------
resetBtn.addEventListener('click', function() {
    resetUpload();
    templateDropdown.value = '';
    templateColumnsPanel.style.display = 'none';
    selectedTemplateId = null;
    showStatus('🔄 Reset complete', 'success');
});

// ---------- IMPORT ----------
importBtn.addEventListener('click', async function() {
    if (!fileId || !selectedTemplateId) {
        showStatus('⚠️ Please upload a file and select a template', 'error');
        return;
    }

    const rows = mappingBody.querySelectorAll('tr');
    const finalMappings = [];
    rows.forEach(tr => {
        const select = tr.querySelector('select');
        const templateColName = tr.children[0].textContent;
        const mapping = currentMappings.find(m => m.column_name === templateColName);
        if (mapping) {
            finalMappings.push({
                template_column_id: mapping.column_id,
                template_column_name: mapping.column_name,
                excel_column_name: select.value || null,
                matched: !!select.value,
                mapping_status: select.value ? 'matched' : 'unmatched'
            });
        }
    });

    const payload = {
        template_id: selectedTemplateId,
        file_id: fileId,
        mappings: finalMappings,
        mode: uploadMode.value,
        unique_column_id: uniqueIdColumn.value ? parseInt(uniqueIdColumn.value) : null,
        sheet_name: currentSheet || 'Sheet1'
    };

    try {
        importBtn.disabled = true;
        importBtn.textContent = '⏳ Importing...';
        const res = await fetch(API_BASE + '/api/upload/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.message || 'Import failed');
        if (result.status === 'validation_failed') {
            let errorMsg = '❌ Validation errors:\n';
            result.errors.forEach(err => {
                errorMsg += `Row ${err.row_number}: `;
                err.errors.forEach(e => {
                    errorMsg += `${e.column} - ${e.message}; `;
                });
                errorMsg += '\n';
            });
            showStatus(errorMsg, 'error');
        } else {
            showStatus(`✅ ${result.message} (${result.imported_count} records)`, 'success');
            setTimeout(resetUpload, 3000);
        }
    } catch (error) {
        showStatus('❌ Import error: ' + error.message, 'error');
    } finally {
        importBtn.disabled = false;
        importBtn.textContent = '🚀 Upload to Server';
    }
});

// ---------- STATUS HELPER ----------
function showStatus(message, type) {
    uploadStatus.textContent = message;
    uploadStatus.className = 'upload-status ' + type;
    uploadStatus.style.display = message ? 'block' : 'none';
    if (type === 'success' || type === 'error') {
        setTimeout(() => {
            uploadStatus.style.display = 'none';
        }, 6000);
    }
}

// ---------- INIT ----------
window.onload = function() {
    loadTemplates();
    console.log('📤 Upload module loaded');
};