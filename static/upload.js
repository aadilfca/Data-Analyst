// ================================================================
// 📤 UPLOAD MODULE – FINAL, ERROR-FREE
// ================================================================

const API_BASE = 'http://localhost:5000';

// DOM refs
const templateDropdown = document.getElementById('templateDropdown');
const sheetSelector = document.getElementById('sheetSelector');
const sheetField = document.getElementById('sheetField');
const statusBadge = document.getElementById('statusBadge');

const uploadArea = document.getElementById('uploadArea');
const fileInput = document.getElementById('fileInput');
const fileInfo = document.getElementById('fileInfo');
const fileName = document.getElementById('fileName');
const fileSize = document.getElementById('fileSize');
const clearBtn = document.getElementById('clearBtn');

const mappingPanel = document.getElementById('mappingPanel');
const mappingBody = document.getElementById('mappingBody');
const mappingCount = document.getElementById('mappingCount');

const previewPanel = document.getElementById('previewPanel');
const previewHeader = document.getElementById('previewHeader');
const previewBody = document.getElementById('previewBody');
const previewCount = document.getElementById('previewCount');

const modeActions = document.getElementById('modeActions');
const uploadMode = document.getElementById('uploadMode');
const uniqueIdField = document.getElementById('uniqueIdField');
const uniqueIdColumn = document.getElementById('uniqueIdColumn');

const downloadBtn = document.getElementById('downloadBtn');
const importBtn = document.getElementById('importBtn');
const resetBtn = document.getElementById('resetBtn');

const statusBar = document.getElementById('statusBar');
const statusMessage = document.getElementById('statusMessage');
const statusExtra = document.getElementById('statusExtra');

const footerMeta = document.getElementById('footerMeta');
const footerFile = document.getElementById('footerFile');
const footerRows = document.getElementById('footerRows');
const footerDate = document.getElementById('footerDate');

// State
let selectedTemplateId = null;
let selectedFile = null;
let fileId = null;
let currentMappings = [];
let excelHeaders = [];
let templateColumns = [];
let sheetNames = [];
let currentSheet = null;
let workbookData = null;
let isProcessing = false;

// Helpers
function showStatus(msg, type = 'info', extra = '') {
    statusMessage.textContent = msg;
    statusExtra.textContent = extra;
    statusBar.className = 'status-bar ' + type;
    statusBar.style.display = 'flex';
}

function setBadge(text, type = '') {
    statusBadge.textContent = text;
    statusBadge.className = 'badge' + (type ? ' ' + type : '');
}

function showSpinner(show) {
    if (show) {
        importBtn.innerHTML = '<span class="spinner"></span> Processing...';
        importBtn.disabled = true;
    } else {
        importBtn.innerHTML = '🚀 Upload to Server';
        importBtn.disabled = false;
    }
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
}

function formatDate(d) {
    const date = new Date(d);
    return date.toLocaleDateString('en-GB') + ' ' + date.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
}

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
        setBadge('✅ ' + templates.length + ' templates loaded');
    } catch (error) {
        console.error('Error loading templates:', error);
        setBadge('❌ Failed to load templates', 'error');
        showStatus('❌ Failed to load templates: ' + error.message, 'error');
    }
}

// ---------- TEMPLATE SELECTION ----------
templateDropdown.addEventListener('change', async function() {
    const id = this.value;
    if (!id) {
        selectedTemplateId = null;
        mappingPanel.style.display = 'none';
        previewPanel.style.display = 'none';
        modeActions.style.display = 'none';
        sheetField.style.display = 'none';
        importBtn.disabled = true;
        setBadge('⏳ Select a template');
        showStatus('Select a template and upload an Excel file to begin.', 'info');
        return;
    }
    selectedTemplateId = parseInt(id);
    setBadge('⏳ Loading template...', 'loading');

    try {
        const res = await fetch(API_BASE + `/api/templates/${id}/upload-columns`);
        if (!res.ok) throw new Error('Failed to fetch template columns');
        const data = await res.json();
        templateColumns = data.columns;
        downloadBtn.dataset.templateId = id;
        downloadBtn.disabled = false;
        setBadge('✅ Template loaded: ' + data.total_columns + ' columns');

        if (fileId && currentSheet) {
            await autoMapColumns();
        } else if (fileId && !currentSheet) {
            showStatus('✅ Template loaded. Please select a sheet.', 'info');
        } else {
            showStatus('✅ Template loaded. Upload an Excel file to begin mapping.', 'success');
        }
    } catch (error) {
        console.error('Error loading template columns:', error);
        setBadge('❌ Failed to load columns', 'error');
        showStatus('❌ Failed to load template columns: ' + error.message, 'error');
    }
});

// ---------- FILE UPLOAD ----------
uploadArea.addEventListener('click', () => fileInput.click());

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
        this.value = '';
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
    fileSize.textContent = formatFileSize(file.size);
    fileInfo.style.display = 'flex';
    uploadArea.style.display = 'none';
    footerMeta.style.display = 'flex';
    footerFile.textContent = '📄 ' + file.name;

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

            populateSheetSelector(sheetNames);
            currentSheet = sheetNames[0];
            sheetSelector.value = currentSheet;
            sheetField.style.display = 'block';

            uploadFileToServer(file);

            showStatus('📄 File loaded. Sheet "' + currentSheet + '" selected.', 'info');
        } catch (err) {
            showStatus('❌ Error reading file: ' + err.message, 'error');
        }
    };
    reader.readAsArrayBuffer(file);
}

// ---------- SHEET SELECTOR ----------
function populateSheetSelector(sheets) {
    sheetSelector.innerHTML = '<option value="">-- Select Sheet --</option>';
    sheets.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        sheetSelector.appendChild(opt);
    });
    sheetField.style.display = 'block';
}

sheetSelector.addEventListener('change', function() {
    currentSheet = this.value;
    if (currentSheet && selectedTemplateId && fileId) {
        autoMapColumns();
        showStatus('📄 Sheet changed to "' + currentSheet + '". Re‑mapping...', 'info');
    } else if (currentSheet && selectedTemplateId && !fileId) {
        if (selectedFile) {
            uploadFileToServer(selectedFile);
        }
    }
});

// ---------- UPLOAD TO SERVER ----------
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
        footerRows.textContent = '📊 ' + result.record_count + ' rows';
        footerDate.textContent = '📁 Uploaded: ' + formatDate(new Date());

        showStatus('✅ File uploaded: ' + result.record_count + ' rows', 'success');

        if (selectedTemplateId && currentSheet) {
            await autoMapColumns();
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
    uploadArea.style.display = 'flex';
    fileInput.value = '';
    mappingPanel.style.display = 'none';
    previewPanel.style.display = 'none';
    modeActions.style.display = 'none';
    sheetField.style.display = 'none';
    sheetSelector.innerHTML = '';
    currentSheet = null;
    sheetNames = [];
    workbookData = null;
    footerMeta.style.display = 'none';
    importBtn.disabled = true;
    currentMappings = [];
    excelHeaders = [];
    templateColumns = [];
    showStatus('🔄 Reset complete. Select a template and upload a file.', 'info');
    setBadge('⏳ Select a template');
    mappingBody.innerHTML = '';
    previewHeader.innerHTML = '';
    previewBody.innerHTML = '';
}

// ---------- AUTO-MAP ----------
async function autoMapColumns() {
    if (!selectedTemplateId || !currentSheet || !fileId) {
        showStatus('⚠️ Please select a template, a sheet, and upload a file.', 'error');
        return;
    }

    try {
        showStatus('⏳ Auto‑mapping columns...', 'loading');
        setBadge('⏳ Mapping...', 'loading');

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
        populateUniqueIdDropdown();

        modeActions.style.display = 'block';
        mappingPanel.style.display = 'block';
        previewPanel.style.display = 'block';
        importBtn.disabled = false;

        const matched = data.mappings.filter(m => m.is_matched).length;
        mappingCount.textContent = matched + ' / ' + data.total_columns + ' matched';
        setBadge('✅ ' + matched + '/' + data.total_columns + ' matched');
        showStatus('✅ Auto‑mapped ' + matched + '/' + data.total_columns + ' columns', 'success');

    } catch (error) {
        console.error('Auto-map error:', error);
        setBadge('❌ Mapping failed', 'error');
        showStatus('❌ Auto‑mapping failed: ' + error.message, 'error');
    }
}

// ---------- RENDER MAPPING TABLE (ERROR-FREE) ----------
function renderMapping(mappings) {
    mappingBody.innerHTML = '';
    if (!mappings || mappings.length === 0) {
        mappingPanel.style.display = 'none';
        return;
    }

    mappings.forEach(m => {
        const tr = document.createElement('tr');

        // 1. Template Column
        const td1 = document.createElement('td');
        td1.textContent = m.column_name;
        td1.style.fontWeight = '500';
        td1.style.whiteSpace = 'nowrap';
        tr.appendChild(td1);

        // 2. Excel Column (dropdown)
        const td2 = document.createElement('td');
        const select = document.createElement('select');
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
        // Event listener – uses closure on m and tr
        select.addEventListener('change', function() {
            const mapping = currentMappings.find(mm => mm.column_id === m.column_id);
            if (mapping) {
                mapping.excel_column = this.value || null;
                mapping.is_matched = !!this.value;
                // Update status cell (the 4th td)
                const statusTd = tr.children[3]; // direct access, no querySelector
                if (statusTd) {
                    const dot = statusTd.querySelector('.status-dot');
                    const text = statusTd.querySelector('.status-text');
                    if (dot && text) {
                        if (mapping.is_matched) {
                            dot.className = 'status-dot green';
                            text.className = 'status-text green';
                            text.textContent = 'Matched';
                        } else {
                            dot.className = 'status-dot red';
                            text.className = 'status-text red';
                            text.textContent = 'Not mapped';
                        }
                        const matched = currentMappings.filter(mm => mm.is_matched).length;
                        mappingCount.textContent = matched + ' / ' + currentMappings.length + ' matched';
                        setBadge('✅ ' + matched + '/' + currentMappings.length + ' matched');
                    }
                }
            }
        });
        td2.appendChild(select);
        tr.appendChild(td2);

        // 3. Data Type
        const td3 = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = 'type-badge';
        if (m.data_type === 'Numeric') badge.classList.add('numeric');
        if (m.data_type === 'Date') badge.classList.add('date');
        badge.textContent = m.data_type;
        td3.appendChild(badge);
        tr.appendChild(td3);

        // 4. Status
        const td4 = document.createElement('td');
        const dot = document.createElement('span');
        dot.className = 'status-dot';
        const txt = document.createElement('span');
        txt.className = 'status-text';
        if (m.is_matched) {
            dot.className = 'status-dot green';
            txt.className = 'status-text green';
            txt.textContent = 'Matched';
        } else {
            dot.className = 'status-dot red';
            txt.className = 'status-text red';
            txt.textContent = 'Not mapped';
        }
        td4.appendChild(dot);
        td4.appendChild(txt);
        tr.appendChild(td4);

        mappingBody.appendChild(tr);
    });
}

// ---------- RENDER PREVIEW ----------
function renderPreviewFromData() {
    if (!workbookData) return;
    try {
        const workbook = XLSX.read(workbookData, { type: 'array' });
        const sheetName = currentSheet || workbook.SheetNames[0];
        const sheet = workbook.Sheets[sheetName];
        const jsonData = XLSX.utils.sheet_to_json(sheet, { header: 1 });
        const headers = jsonData[0] || [];
        const rows = jsonData.slice(1, 6);

        previewHeader.innerHTML = '';
        if (headers.length) {
            const headerRow = document.createElement('tr');
            headers.forEach(h => {
                const th = document.createElement('th');
                th.textContent = h || '—';
                headerRow.appendChild(th);
            });
            previewHeader.appendChild(headerRow);
        }

        previewBody.innerHTML = '';
        rows.forEach(row => {
            const tr = document.createElement('tr');
            row.forEach(cell => {
                const td = document.createElement('td');
                td.textContent = cell !== undefined ? cell : '';
                tr.appendChild(td);
            });
            previewBody.appendChild(tr);
        });

        previewCount.textContent = rows.length + ' rows · ' + headers.length + ' columns';
        previewPanel.style.display = 'block';

    } catch (error) {
        console.error('Preview error:', error);
    }
}

// ---------- UNIQUE ID DROPDOWN ----------
function populateUniqueIdDropdown() {
    uniqueIdColumn.innerHTML = '<option value="">— None —</option>';
    const mappedColumns = currentMappings.filter(m => m.is_matched);
    mappedColumns.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.column_id;
        opt.textContent = m.column_name;
        uniqueIdColumn.appendChild(opt);
    });
    toggleUniqueIdVisibility();
}

function toggleUniqueIdVisibility() {
    uniqueIdField.style.display = (uploadMode.value === 'update') ? 'block' : 'none';
}
uploadMode.addEventListener('change', toggleUniqueIdVisibility);

// ---------- DOWNLOAD TEMPLATE ----------
downloadBtn.addEventListener('click', function() {
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
    selectedTemplateId = null;
    setBadge('⏳ Select a template');
    showStatus('🔄 Reset complete. Select a template and upload a file.', 'info');
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
        isProcessing = true;
        showSpinner(true);
        importBtn.disabled = true;
        showStatus('⏳ Importing data...', 'loading');
        setBadge('⏳ Importing...', 'loading');

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
            setBadge('❌ Validation failed', 'error');
        } else {
            showStatus(`✅ ${result.message} (${result.imported_count} records)`, 'success');
            setBadge('✅ Import complete');
            setTimeout(resetUpload, 2000);
        }
    } catch (error) {
        showStatus('❌ Import error: ' + error.message, 'error');
        setBadge('❌ Import failed', 'error');
    } finally {
        isProcessing = false;
        showSpinner(false);
        importBtn.disabled = false;
    }
});

// ---------- INIT ----------
window.onload = function() {
    loadTemplates();
    console.log('📤 Upload module loaded');
    showStatus('Select a template and upload an Excel file to begin.', 'info');
};