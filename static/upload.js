// ================================================================
// 📤 UPLOAD MODULE – FINAL
// ================================================================

// API_BASE is defined globally in client_context.js
console.log('📤 Upload module loaded');

// ---------- DOM REFS ----------
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

// ---------- STATE ----------
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
let miniPreviewVisible = false; // Tracks whether mini preview is shown

// ---------- HELPERS ----------
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

// ---------- PREVIEW BUTTON STATE ----------
function updatePreviewButtonState() {
    const previewBtn = document.getElementById('previewBtn');
    if (!previewBtn) return;

    const isFileLoaded = workbookData !== null;
    const isSheetSelected = currentSheet !== null && currentSheet !== '';
    const isTemplateSelected = selectedTemplateId !== null;
    const isMappingDone = currentMappings.length > 0 && mappingPanel && mappingPanel.style.display !== 'none';

    const isReady = isFileLoaded && isSheetSelected && isTemplateSelected && isMappingDone;

    if (isReady) {
        previewBtn.style.display = 'inline-block';
        previewBtn.disabled = false;
    } else {
        previewBtn.style.display = 'inline-block'; // Keep visible but disabled
        previewBtn.disabled = true;
    }
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
        console.log('✅ Templates loaded:', templates.length);
    } catch (error) {
        console.error('Error loading templates:', error);
        setBadge('❌ Failed to load templates', 'error');
        showStatus('❌ Failed to load templates: ' + error.message, 'error');
    }
}

// ---------- TEMPLATE SELECTION ----------
if (templateDropdown) {
    templateDropdown.addEventListener('change', async function() {
        // Check if client is selected
        if (!window.isClientSelected || !window.isClientSelected()) {
            showStatus('⚠️ Please select a client first from the Dashboard.', 'error');
            this.value = '';
            setBadge('⛔ No client selected');
            if (importBtn) importBtn.disabled = true;
            updatePreviewButtonState();
            return;
        }
        const id = this.value;
        if (!id) {
            selectedTemplateId = null;
            if (mappingPanel) mappingPanel.style.display = 'none';
            if (modeActions) modeActions.style.display = 'none';
            if (sheetField) sheetField.style.display = 'none';
            if (importBtn) importBtn.disabled = true;
            setBadge('⏳ Select a template');
            showStatus('Select a template and upload an Excel file to begin.', 'info');
            updatePreviewButtonState();
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
        updatePreviewButtonState();
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
        console.log('📄 File input change event triggered');
        if (this.files && this.files.length > 0) {
            console.log('📄 File selected:', this.files[0].name);
            handleFile(this.files[0]);
            this.value = '';
        } else {
            console.warn('⚠️ No file selected');
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
            updatePreviewButtonState();
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
        updatePreviewButtonState();
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
    if (modeActions) modeActions.style.display = 'none';
    if (sheetField) sheetField.style.display = 'none';
    if (sheetSelector) sheetSelector.innerHTML = '';
    currentSheet = null;
    sheetNames = [];
    workbookData = null;
    currentMappings = [];
    excelHeaders = [];
    templateColumns = [];
    if (footerMeta) footerMeta.style.display = 'none';
    if (importBtn) importBtn.disabled = true;
    showStatus('🔄 Reset complete. Select a template and upload a file.', 'info');
    setBadge('⏳ Select a template');
    if (mappingBody) mappingBody.innerHTML = '';
    updatePreviewButtonState();
    // Hide mini preview and reset toggle
    const container = document.getElementById('miniPreviewContainer');
    const toggleBtn = document.getElementById('toggleMiniPreviewBtn');
    if (container) container.style.display = 'none';
    if (toggleBtn) {
        toggleBtn.textContent = '📋 Mini Preview';
        toggleBtn.style.display = 'none';
    }
    miniPreviewVisible = false;
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
        populateUniqueIdDropdown();

        if (modeActions) modeActions.style.display = 'block';
        if (mappingPanel) mappingPanel.style.display = 'block';
        if (importBtn) importBtn.disabled = false;

        const matched = data.mappings.filter(m => m.is_matched).length;
        if (mappingCount) mappingCount.textContent = matched + ' / ' + data.total_columns + ' matched';
        setBadge('✅ ' + matched + '/' + data.total_columns + ' matched');
        showStatus('✅ Auto‑mapped ' + matched + '/' + data.total_columns + ' columns', 'success');

        // Show mini preview toggle button
        const toggleBtn = document.getElementById('toggleMiniPreviewBtn');
        if (toggleBtn) toggleBtn.style.display = 'inline-block';

        updatePreviewButtonState();

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

    mappings.forEach((m, rowIndex) => {
        const tr = document.createElement('tr');

        const td1 = document.createElement('td');
        td1.textContent = m.column_name;
        td1.style.fontWeight = '500';
        td1.style.whiteSpace = 'nowrap';
        tr.appendChild(td1);

        const td2 = document.createElement('td');
        const select = document.createElement('select');
        select.dataset.rowIndex = rowIndex;
        select.dataset.columnId = m.column_id;

        function buildOptions() {
            const selectedValues = currentMappings
                .filter((_, idx) => idx !== rowIndex)
                .map(mm => mm.excel_column)
                .filter(v => v !== null && v !== '');
            select.innerHTML = '';
            const noneOpt = document.createElement('option');
            noneOpt.value = '';
            noneOpt.textContent = '— None —';
            select.appendChild(noneOpt);
            excelHeaders.forEach(h => {
                if (selectedValues.includes(h)) return;
                const opt = document.createElement('option');
                opt.value = h;
                opt.textContent = h;
                if (m.excel_column === h) {
                    opt.selected = true;
                }
                select.appendChild(opt);
            });
            if (m.excel_column && ![...select.options].some(o => o.value === m.excel_column)) {
                m.excel_column = null;
                m.is_matched = false;
                const statusTd = tr.children[3];
                if (statusTd) {
                    const icon = statusTd.querySelector('span');
                    if (icon) {
                        icon.textContent = '❌';
                        icon.style.color = '#dc2626';
                    }
                }
                updateCounts();
            } else {
                if (m.excel_column) {
                    const opt = select.querySelector(`option[value="${m.excel_column}"]`);
                    if (opt) opt.selected = true;
                }
            }
        }
        buildOptions();

                select.addEventListener('change', function() {
            const newVal = this.value || null;
            m.excel_column = newVal;
            m.is_matched = !!newVal;
            const statusTd = tr.children[3];
            if (statusTd) {
                const icon = statusTd.querySelector('span');
                if (icon) {
                    if (m.is_matched) {
                        icon.textContent = '✅';
                        icon.style.color = '#16a34a';
                    } else {
                        icon.textContent = '❌';
                        icon.style.color = '#dc2626';
                    }
                }
            }
            // Update mini preview immediately
            renderMiniPreview();
            requestAnimationFrame(() => {
                renderMapping(currentMappings);
            });
        });

        td2.appendChild(select);
        tr.appendChild(td2);

        const td3 = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = 'type-badge';
        if (m.data_type === 'Numeric') badge.classList.add('numeric');
        if (m.data_type === 'Date') badge.classList.add('date');
        badge.textContent = m.data_type;
        td3.appendChild(badge);
        tr.appendChild(td3);

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

     updateCounts();

    function updateCounts() {
        const matched = currentMappings.filter(m => m.is_matched).length;
        if (mappingCount) mappingCount.textContent = matched + ' / ' + currentMappings.length + ' matched';
        setBadge('✅ ' + matched + '/' + currentMappings.length + ' matched');
    }

    // Update mini preview
    renderMiniPreview();
}

// ---------- MINI PREVIEW ----------
function getFirstDataRow() {
    if (!workbookData) return [];
    try {
        const workbook = XLSX.read(workbookData, { type: 'array' });
        const sheetName = currentSheet || workbook.SheetNames[0];
        const sheet = workbook.Sheets[sheetName];
        const jsonData = XLSX.utils.sheet_to_json(sheet, { header: 1 });
        // Row 0 = headers, row 1 = first data row (row 2 in Excel)
        if (jsonData.length < 2) return [];
        return jsonData[1] || [];
    } catch (e) {
        return [];
    }
}

function renderMiniPreview() {
    const tbody = document.getElementById('miniPreviewBody');
    const container = document.getElementById('miniPreviewContainer');
    if (!tbody || !container) return;

    // If no mapping or no data, hide container
    if (!currentMappings || currentMappings.length === 0 || !workbookData) {
        container.style.display = 'none';
        return;
    }

    const firstDataRow = getFirstDataRow();
    if (!firstDataRow || firstDataRow.length === 0) {
        container.style.display = 'none';
        return;
    }

    // Build a map from excel column name to value
    const headers = excelHeaders || [];
    const valueMap = {};
    headers.forEach((h, idx) => {
        valueMap[h] = firstDataRow[idx] !== undefined ? firstDataRow[idx] : '';
    });

    // Build rows
    let html = '';
    currentMappings.forEach(m => {
        const templateCol = m.column_name || '';
        const excelCol = m.excel_column || '—';
        const value = excelCol && excelCol !== '—' ? (valueMap[excelCol] !== undefined ? valueMap[excelCol] : '') : '—';
        // Truncate long values
        const displayValue = typeof value === 'string' ? (value.length > 30 ? value.substring(0, 30) + '…' : value) : value;
        html += `<tr>
            <td style="font-weight:500;">${templateCol}</td>
            <td>${excelCol}</td>
            <td>${displayValue}</td>
        </tr>`;
    });

    tbody.innerHTML = html;
    // Only show container if mini preview is supposed to be visible
    if (miniPreviewVisible) {
        container.style.display = 'block';
    }
    // Show the toggle button
    const toggleBtn = document.getElementById('toggleMiniPreviewBtn');
    if (toggleBtn) toggleBtn.style.display = 'inline-block';
}

function toggleMiniPreview() {
    const container = document.getElementById('miniPreviewContainer');
    const toggleBtn = document.getElementById('toggleMiniPreviewBtn');
    if (!container || !toggleBtn) return;
    if (!miniPreviewVisible) {
        container.style.display = 'block';
        toggleBtn.textContent = '📋 Hide Preview';
        miniPreviewVisible = true;
        renderMiniPreview(); // ensure it's up to date
    } else {
        container.style.display = 'none';
        toggleBtn.textContent = '📋 Mini Preview';
        miniPreviewVisible = false;
    }
}

function closeMiniPreview() {
    const container = document.getElementById('miniPreviewContainer');
    const toggleBtn = document.getElementById('toggleMiniPreviewBtn');
    if (container) container.style.display = 'none';
    if (toggleBtn) toggleBtn.textContent = '📋 Mini Preview';
    miniPreviewVisible = false;
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

// ---------- IMPORT (UPLOAD TO SERVER) ----------
if (importBtn) {
    importBtn.addEventListener('click', async function() {
        // Ensure client is selected
        let clientId = null;
        if (typeof window.isClientSelected === 'function' && window.isClientSelected()) {
            clientId = window.getSelectedClientId();
        } else {
            // Fallback: try to read from localStorage directly
            clientId = localStorage.getItem('selectedClientId');
        }
        if (!clientId) {
            showStatus('⚠️ Please select a client first from the Dashboard.', 'error');
            return;
        }
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
            client_id: parseInt(clientId),
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

// ---------- PREVIEW FUNCTION ----------
function showPreview() {
    const previewSheet = document.getElementById('previewSheet');
    const mainUploadPage = document.getElementById('mainUploadPage');
    const previewHead = document.getElementById('previewSheetHead');
    const previewBody = document.getElementById('previewSheetBody');
    const previewInfo = document.getElementById('previewInfo');

    if (!workbookData) {
        showStatus('⚠️ No file loaded to preview.', 'error');
        return;
    }

    try {
        const workbook = XLSX.read(workbookData, { type: 'array' });
        const sheetName = currentSheet || workbook.SheetNames[0];
        const sheet = workbook.Sheets[sheetName];
        const jsonData = XLSX.utils.sheet_to_json(sheet, { header: 1 });
        
        const headers = jsonData[0] || [];
        const rows = jsonData.slice(1); // All rows, not limited

        // Build headers
        previewHead.innerHTML = '';
        if (headers.length) {
            const tr = document.createElement('tr');
            headers.forEach(h => {
                const th = document.createElement('th');
                th.textContent = h || '—';
                tr.appendChild(th);
            });
            previewHead.appendChild(tr);
        }

        // Build body (all rows)
        previewBody.innerHTML = '';
        rows.forEach(row => {
            const tr = document.createElement('tr');
            row.forEach((cell, idx) => {
                const td = document.createElement('td');
                let displayValue = cell;
                const headerName = (headers[idx] || '').toLowerCase();
                if (typeof cell === 'number' && cell > 30000 && 
                    (headerName.includes('date') || headerName.includes('day') || headerName.includes('month'))) {
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

        previewInfo.textContent = `${rows.length} rows · ${headers.length} columns`;

        // Update footer with client and user info
        const clientName = window.getSelectedClientName ? window.getSelectedClientName() : 'No Client';
        const clientCode = window.getSelectedClientCode ? window.getSelectedClientCode() : '';
        const userName = 'Analyst';
        const now = new Date();
        const timeStr = now.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
        const dateStr = now.toLocaleDateString('en-GB', { day: '2-digit', month: '2-digit', year: 'numeric' });

        document.getElementById('footerClientName').textContent = clientCode ? `${clientName} (${clientCode})` : clientName;
        document.getElementById('footerUserName').textContent = userName;
        document.getElementById('footerTimeValue').textContent = timeStr;
        document.getElementById('footerDateValue').textContent = dateStr;

        // Show preview sheet, hide main page
        previewSheet.classList.add('active');
        mainUploadPage.style.display = 'none';
        // Set container to fixed height
        const container = document.getElementById('uploadContainer');
        if (container) container.classList.add('preview-active');

    } catch (error) {
        showStatus('❌ Error previewing file: ' + error.message, 'error');
    }
}

// ---------- CLOSE PREVIEW ----------
const closePreviewBtn = document.getElementById('closePreviewBtn');
if (closePreviewBtn) {
    closePreviewBtn.addEventListener('click', function(e) {
        e.preventDefault();
        document.getElementById('previewSheet').classList.remove('active');
        document.getElementById('mainUploadPage').style.display = 'block';
        // Remove fixed height from container
        const container = document.getElementById('uploadContainer');
        if (container) container.classList.remove('preview-active');
        // Re-enable preview button after closing (state already managed)
        updatePreviewButtonState();
    });
}

// ---------- PREVIEW BUTTON ----------
const previewBtn = document.getElementById('previewBtn');
if (previewBtn) {
    previewBtn.addEventListener('click', function() {
        if (!workbookData) {
            showStatus('⚠️ Please upload a file first.', 'error');
            return;
        }
        if (!currentSheet) {
            showStatus('⚠️ Please select a sheet.', 'error');
            return;
        }
        if (!selectedTemplateId) {
            showStatus('⚠️ Please select a template.', 'error');
            return;
        }
        if (currentMappings.length === 0) {
            showStatus('⚠️ Please wait for auto-mapping to complete.', 'error');
            return;
        }
        // All conditions satisfied
        showPreview();
    });
}

// ---------- INIT ----------
window.onload = function() {
    loadTemplates();
    console.log('📤 Upload module fully loaded');
    showStatus('Select a template and upload an Excel file to begin.', 'info');
    updatePreviewButtonState();

    // Mini preview toggle button
    const toggleBtn = document.getElementById('toggleMiniPreviewBtn');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', toggleMiniPreview);
    }
    const closeBtn = document.getElementById('closeMiniPreviewBtn');
    if (closeBtn) {
        closeBtn.addEventListener('click', closeMiniPreview);
    }
};