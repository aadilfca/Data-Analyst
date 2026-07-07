// ================================================================
// 📤 UPLOAD MODULE – FIXED (safe null checks)
// ================================================================

const API_BASE = 'http://localhost:5000';

// DOM refs with fallback
function getElement(id) {
    const el = document.getElementById(id);
    if (!el) console.warn('⚠️ Element not found:', id);
    return el;
}

const templateDropdown = getElement('templateDropdown');
const sheetSelector = getElement('sheetSelector');
const sheetField = getElement('sheetField');
const statusBadge = getElement('statusBadge');

const uploadArea = getElement('uploadArea');
const fileInput = getElement('fileInput');
const fileInfo = getElement('fileInfo');
const fileName = getElement('fileName');
const fileSize = getElement('fileSize');
const clearBtn = getElement('clearBtn');

const mappingPanel = getElement('mappingPanel');
const mappingBody = getElement('mappingBody');
const mappingCount = getElement('mappingCount');

const previewPanel = getElement('previewPanel');
const previewHeader = getElement('previewHeader');
const previewBody = getElement('previewBody');
const previewCount = getElement('previewCount');

const modeActions = getElement('modeActions');
const uploadMode = getElement('uploadMode');
const uniqueIdField = getElement('uniqueIdField');
const uniqueIdColumn = getElement('uniqueIdColumn');

const downloadBtn = getElement('downloadBtn');
const importBtn = getElement('importBtn');
const resetBtn = getElement('resetBtn');

const statusBar = getElement('statusBar');
const statusMessage = getElement('statusMessage');
const statusExtra = getElement('statusExtra');

const footerMeta = getElement('footerMeta');
const footerFile = getElement('footerFile');
const footerRows = getElement('footerRows');
const footerDate = getElement('footerDate');

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
    if (!statusBar || !statusMessage) {
        console.log('📢 Status:', msg, type, extra);
        return;
    }
    statusMessage.textContent = msg;
    if (statusExtra) statusExtra.textContent = extra || '';
    statusBar.className = 'status-bar ' + type;
    statusBar.style.display = 'flex';
}

function setBadge(text, type = '') {
    if (!statusBadge) return;
    statusBadge.textContent = text;
    statusBadge.className = 'badge' + (type ? ' ' + type : '');
}

function showSpinner(show) {
    if (!importBtn) return;
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
        if (templateDropdown) {
            templateDropdown.innerHTML = '<option value="">-- Select Template --</option>';
            templates.forEach(tpl => {
                const opt = document.createElement('option');
                opt.value = tpl.id;
                opt.textContent = tpl.template_name;
                templateDropdown.appendChild(opt);
            });
        }
        setBadge('✅ ' + templates.length + ' templates loaded');
    } catch (error) {
        console.error('Error loading templates:', error);
        setBadge('❌ Failed to load templates', 'error');
        showStatus('❌ Failed to load templates: ' + error.message, 'error');
    }
}

// ---------- TEMPLATE SELECTION ----------
if (templateDropdown) {
    templateDropdown.addEventListener('change', async function() {
        const id = this.value;
        if (!id) {
            selectedTemplateId = null;
            if (mappingPanel) mappingPanel.style.display = 'none';
            if (previewPanel) previewPanel.style.display = 'none';
            if (modeActions) modeActions.style.display = 'none';
            if (sheetField) sheetField.style.display = 'none';
            if (importBtn) importBtn.disabled = true;
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
            if (downloadBtn) {
                downloadBtn.dataset.templateId = id;
                downloadBtn.disabled = false;
            }
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
}

// ---------- FILE UPLOAD ----------
if (uploadArea) {
    uploadArea.addEventListener('click', () => fileInput && fileInput.click());
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
}

if (fileInput) {
    fileInput.addEventListener('change', function(e) {
        if (this.files && this.files.length > 0) {
            handleFile(this.files[0]);
            this.value = '';
        }
    });
}

async function handleFile(file) {
    const validTypes = ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.ms-excel'];
    if (!validTypes.includes(file.type) && !file.name.endsWith('.xlsx') && !file.name.endsWith('.xls')) {
        showStatus('⚠️ Please select a valid Excel file (.xlsx or .xls)', 'error');
        return;
    }

    selectedFile = file;
    if (fileName) fileName.textContent = file.name;
    if (fileSize) fileSize.textContent = formatFileSize(file.size);
    if (fileInfo) fileInfo.style.display = 'flex';
    if (uploadArea) uploadArea.style.display = 'none';
    if (footerMeta) footerMeta.style.display = 'flex';
    if (footerFile) footerFile.textContent = '📄 ' + file.name;

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
            if (sheetSelector) sheetSelector.value = currentSheet;
            if (sheetField) sheetField.style.display = 'block';

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
    if (!sheetSelector) return;
    sheetSelector.innerHTML = '<option value="">-- Select Sheet --</option>';
    sheets.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        sheetSelector.appendChild(opt);
    });
    if (sheetField) sheetField.style.display = 'block';
}

if (sheetSelector) {
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
}

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
        if (footerRows) footerRows.textContent = '📊 ' + result.record_count + ' rows';
        if (footerDate) footerDate.textContent = '📁 Uploaded: ' + formatDate(new Date());

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
if (clearBtn) {
    clearBtn.addEventListener('click', resetUpload);
}

function resetUpload() {
    selectedFile = null;
    fileId = null;
    if (fileInfo) fileInfo.style.display = 'none';
    if (uploadArea) uploadArea.style.display = 'flex';
    if (fileInput) fileInput.value = '';
    if (mappingPanel) mappingPanel.style.display = 'none';
    if (previewPanel) previewPanel.style.display = 'none';
    if (modeActions) modeActions.style.display = 'none';
    if (sheetField) sheetField.style.display = 'none';
    if (sheetSelector) sheetSelector.innerHTML = '';
    currentSheet = null;
    sheetNames = [];
    workbookData = null;
    if (footerMeta) footerMeta.style.display = 'none';
    if (importBtn) importBtn.disabled = true;
    currentMappings = [];
    excelHeaders = [];
    templateColumns = [];
    showStatus('🔄 Reset complete. Select a template and upload a file.', 'info');
    setBadge('⏳ Select a template');
    if (mappingBody) mappingBody.innerHTML = '';
    if (previewHeader) previewHeader.innerHTML = '';
    if (previewBody) previewBody.innerHTML = '';
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

        if (modeActions) modeActions.style.display = 'block';
        if (mappingPanel) mappingPanel.style.display = 'block';
        if (previewPanel) previewPanel.style.display = 'flex';
        if (importBtn) importBtn.disabled = false;

        const matched = data.mappings.filter(m => m.is_matched).length;
        if (mappingCount) mappingCount.textContent = matched + ' / ' + data.total_columns + ' matched';
        setBadge('✅ ' + matched + '/' + data.total_columns + ' matched');
        showStatus('✅ Auto‑mapped ' + matched + '/' + data.total_columns + ' columns', 'success');

    } catch (error) {
        console.error('Auto-map error:', error);
        setBadge('❌ Mapping failed', 'error');
        showStatus('❌ Auto‑mapping failed: ' + error.message, 'error');
    }
}

// ---------- RENDER MAPPING TABLE ----------
function renderMapping(mappings) {
    if (!mappingBody) return;
    mappingBody.innerHTML = '';
    if (!mappings || mappings.length === 0) {
        if (mappingPanel) mappingPanel.style.display = 'none';
        return;
    }

    mappings.forEach(m => {
        const tr = document.createElement('tr');

        // 1. Template
        const td1 = document.createElement('td');
        td1.textContent = m.column_name;
        td1.style.fontWeight = '500';
        td1.style.whiteSpace = 'nowrap';
        tr.appendChild(td1);

        // 2. Excel Column
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
        select.addEventListener('change', function() {
            const mapping = currentMappings.find(mm => mm.column_id === m.column_id);
            if (mapping) {
                mapping.excel_column = this.value || null;
                mapping.is_matched = !!this.value;
                const statusTd = tr.children[3];
                if (statusTd) {
                    const icon = statusTd.querySelector('span');
                    if (icon) {
                        if (mapping.is_matched) {
                            icon.textContent = '✅';
                            icon.style.color = '#16a34a';
                        } else {
                            icon.textContent = '❌';
                            icon.style.color = '#dc2626';
                        }
                    }
                    const matched = currentMappings.filter(mm => mm.is_matched).length;
                    if (mappingCount) mappingCount.textContent = matched + ' / ' + currentMappings.length + ' matched';
                    setBadge('✅ ' + matched + '/' + currentMappings.length + ' matched');
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

        // 4. Status – only icon
        const td4 = document.createElement('td');
        const icon = document.createElement('span');
        icon.style.fontSize = '16px';
        if (m.is_matched) {
            icon.textContent = '✅';
            icon.style.color = '#16a34a';
        } else {
            icon.textContent = '❌';
            icon.style.color = '#dc2626';
        }
        td4.appendChild(icon);
        tr.appendChild(td4);

        mappingBody.appendChild(tr);
    });
}

// ---------- RENDER PREVIEW ----------
function renderPreviewFromData() {
    if (!workbookData || !previewHeader || !previewBody) return;
    try {
        const workbook = XLSX.read(workbookData, { type: 'array' });
        const sheetName = currentSheet || workbook.SheetNames[0];
        const sheet = workbook.Sheets[sheetName];
        const jsonData = XLSX.utils.sheet_to_json(sheet, { header: 1 });
        const headers = jsonData[0] || [];
        const rows = jsonData.slice(1, 21);

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
            row.forEach((cell, idx) => {
                const td = document.createElement('td');
                let displayValue = cell;
                const headerName = (headers[idx] || '').toLowerCase();
                if (typeof cell === 'number' && cell > 30000 && (headerName.includes('date') || headerName.includes('day') || headerName.includes('month'))) {
                    const date = new Date((cell - 25569) * 86400 * 1000);
                    if (!isNaN(date)) {
                        const d = String(date.getDate()).padStart(2, '0');
                        const m = String(date.getMonth() + 1).padStart(2, '0');
                        const y = date.getFullYear();
                        displayValue = d + '/' + m + '/' + y;
                    }
                } else if (typeof cell === 'string' && /^\d{1,2}\/\d{1,2}\/\d{2,4}$/.test(cell)) {
                    const parts = cell.split('/');
                    if (parts.length === 3) {
                        let day, month, year;
                        if (parseInt(parts[0]) > 12) {
                            day = parts[0];
                            month = parts[1];
                        } else {
                            month = parts[0];
                            day = parts[1];
                        }
                        year = parts[2];
                        if (year.length === 2) year = '20' + year;
                        displayValue = day.padStart(2, '0') + '/' + month.padStart(2, '0') + '/' + year;
                    }
                }
                td.textContent = displayValue !== undefined ? displayValue : '';
                tr.appendChild(td);
            });
            previewBody.appendChild(tr);
        });

        const totalRows = jsonData.length - 1;
        if (previewCount) previewCount.textContent = Math.min(totalRows, 20) + ' rows · ' + headers.length + ' columns';
        if (previewPanel) previewPanel.style.display = 'flex';

    } catch (error) {
        console.error('Preview error:', error);
    }
}

// ---------- UNIQUE ID DROPDOWN ----------
function populateUniqueIdDropdown() {
    if (!uniqueIdColumn) return;
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
    if (!uniqueIdField) return;
    uniqueIdField.style.display = (uploadMode && uploadMode.value === 'update') ? 'block' : 'none';
}
if (uploadMode) {
    uploadMode.addEventListener('change', toggleUniqueIdVisibility);
}

// ---------- DOWNLOAD TEMPLATE ----------
if (downloadBtn) {
    downloadBtn.addEventListener('click', function() {
        const templateId = this.dataset.templateId;
        if (!templateId) {
            showStatus('⚠️ Please select a template first', 'error');
            return;
        }
        window.open(API_BASE + `/api/upload/download-template/${templateId}`, '_blank');
    });
}

// ---------- RESET ----------
if (resetBtn) {
    resetBtn.addEventListener('click', function() {
        resetUpload();
        if (templateDropdown) templateDropdown.value = '';
        selectedTemplateId = null;
        setBadge('⏳ Select a template');
        showStatus('🔄 Reset complete. Select a template and upload a file.', 'info');
    });
}

// ---------- IMPORT ----------
if (importBtn) {
    importBtn.addEventListener('click', async function() {
        if (!fileId || !selectedTemplateId) {
            showStatus('⚠️ Please upload a file and select a template', 'error');
            return;
        }

        if (!mappingBody) return;
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
            mode: uploadMode ? uploadMode.value : 'create',
            unique_column_id: uniqueIdColumn && uniqueIdColumn.value ? parseInt(uniqueIdColumn.value) : null,
            sheet_name: currentSheet || 'Sheet1'
        };

        try {
            isProcessing = true;
            showSpinner(true);
            if (importBtn) importBtn.disabled = true;
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
            if (importBtn) importBtn.disabled = false;
        }
    });
}

// ---------- INIT ----------
window.onload = function() {
    loadTemplates();
    console.log('📤 Upload module loaded');
    showStatus('Select a template and upload an Excel file to begin.', 'info');
};