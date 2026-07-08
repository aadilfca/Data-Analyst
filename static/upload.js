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
let validationStatus = 'idle'; // 'idle' | 'validating' | 'valid' | 'invalid'
let miniPreviewVisible = false; // Tracks whether mini preview is shown
// ---------- RESET VALIDATION STATE ----------
function resetValidationState() {
    validationStatus = 'idle';
    if (importBtn) {
        importBtn.innerHTML = 'Validate';
        importBtn.style.background = '';
        importBtn.style.color = '';
        importBtn.disabled = false;
    }
    updatePreviewButtonState();
}

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
    const isValidated = validationStatus === 'valid';

    const isReady = isFileLoaded && isSheetSelected && isTemplateSelected && isMappingDone && isValidated;

    if (isReady) {
        previewBtn.style.display = 'inline-block';
        previewBtn.disabled = false;
    } else {
        previewBtn.style.display = 'inline-block';
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
            resetValidationState(); // Reset validation when template is cleared
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
        resetValidationState(); // New template means new data – reset validation
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
            resetValidationState(); // Reset validation for new file
                    updatePreviewButtonState();
        resetValidationState(); // New mapping – reset validation
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
        resetValidationState(); // Reset validation on sheet change
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
    
    // Reset validation status
    validationStatus = 'idle';
    if (importBtn) {
        importBtn.innerHTML = 'Validate';
        importBtn.style.background = '';
        importBtn.style.color = '';
        importBtn.disabled = false;
    }
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
// ---------- IMPORT SUMMARY POPUP ----------
function showImportSummary(result) {
    // Create overlay
    const overlay = document.createElement('div');
    overlay.id = 'importSummaryOverlay';
    overlay.style.cssText = `
        position: fixed; top:0; left:0; width:100%; height:100%;
        background: rgba(0,0,0,0.5); z-index:2000;
        display: flex; justify-content:center; align-items:center;
    `;
    
    // Create modal
    const modal = document.createElement('div');
    modal.style.cssText = `
        background: white; border-radius: 12px; padding: 30px;
        max-width: 500px; width: 90%; box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        font-family: 'Segoe UI', sans-serif;
    `;
    
    const total = result.total_rows || 0;
    const inserted = result.imported_count || 0;
    const updated = result.updated_count || 0;
    const errors = result.validation_errors || [];
    const errorCount = errors.length;
    
    // Determine status and message
    let statusIcon = '✅';
    let statusColor = '#16a34a';
    let statusText = 'Import Successful';
    let summaryMessage = '';
    
    if (errorCount > 0) {
        statusIcon = '⚠️';
        statusColor = '#f59e0b';
        statusText = 'Import with Warnings';
        summaryMessage = `Imported with ${errorCount} warning(s). Please check the errors below.`;
    } else if (total === 0) {
        statusIcon = 'ℹ️';
        statusColor = '#3b82f6';
        statusText = 'No Data to Import';
        summaryMessage = 'The file contained no data rows, or all rows were already up to date.';
    } else if (inserted === 0 && updated === 0) {
        statusIcon = 'ℹ️';
        statusColor = '#3b82f6';
        statusText = 'No Changes Detected';
        summaryMessage = 'All rows already exist and no changes were made.';
    } else {
        // Build a descriptive message
        let parts = [];
        if (inserted > 0) parts.push(`${inserted} new row${inserted > 1 ? 's' : ''} inserted`);
        if (updated > 0) parts.push(`${updated} existing row${updated > 1 ? 's' : ''} updated`);
        summaryMessage = parts.join(' and ') + ' successfully.';
    }
    
    // Use backend message as fallback if available
    if (!summaryMessage && result.message) {
        summaryMessage = result.message;
    }
    
    modal.innerHTML = `
        <div style="text-align:center; margin-bottom:16px;">
            <div style="font-size:48px;">${statusIcon}</div>
            <h2 style="margin:8px 0 4px; color:#1a1a2e;">${statusText}</h2>
            <p style="color:#475569; font-size:15px; font-weight:500; margin-top:4px;">${summaryMessage || 'Data import completed.'}</p>
        </div>
        <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; margin:16px 0;">
            <div style="background:#f8fafc; padding:12px; border-radius:8px; text-align:center;">
                <div style="font-size:24px; font-weight:700; color:#3b82f6;">${total}</div>
                <div style="font-size:12px; color:#64748b;">Total Rows</div>
            </div>
            <div style="background:#dcfce7; padding:12px; border-radius:8px; text-align:center;">
                <div style="font-size:24px; font-weight:700; color:#16a34a;">${inserted}</div>
                <div style="font-size:12px; color:#64748b;">New Rows</div>
            </div>
            <div style="background:#fef3c7; padding:12px; border-radius:8px; text-align:center;">
                <div style="font-size:24px; font-weight:700; color:#f59e0b;">${updated}</div>
                <div style="font-size:12px; color:#64748b;">Updated Rows</div>
            </div>
        </div>
        ${errorCount > 0 ? `
            <div style="background:#fee2e2; padding:12px; border-radius:8px; margin-bottom:12px; max-height:150px; overflow-y:auto;">
                <div style="font-weight:600; color:#991b1b; margin-bottom:4px;">${errorCount} validation error(s):</div>
                ${errors.slice(0,5).map(err => `
                    <div style="font-size:12px; color:#991b1b; padding:2px 0; border-bottom:1px solid #fecaca;">
                        Row ${err.row_number}: ${err.errors.map(e => `${e.column} - ${e.message}`).join('; ')}
                    </div>
                `).join('')}
                ${errors.length > 5 ? `<div style="font-size:12px; color:#991b1b;">... and ${errors.length - 5} more.</div>` : ''}
            </div>
        ` : ''}
        <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:16px;">
            <button id="importSummaryCloseBtn" style="padding:8px 24px; background:#3b82f6; color:white; border:none; border-radius:6px; font-weight:600; cursor:pointer;">Close</button>
        </div>
    `;
    
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    
    // Close on button click
    overlay.querySelector('#importSummaryCloseBtn').addEventListener('click', function() {
        document.body.removeChild(overlay);
        resetUpload();
    });
    // Close on overlay click (background)
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) {
            document.body.removeChild(overlay);
            resetUpload();
        }
    });
}
// ---------- SHOW VALIDATION ERRORS ----------
function showValidationErrors(result) {
    const overlay = document.createElement('div');
    overlay.id = 'validationErrorOverlay';
    overlay.style.cssText = `
        position: fixed; top:0; left:0; width:100%; height:100%;
        background: rgba(0,0,0,0.5); z-index:2000;
        display: flex; justify-content:center; align-items:center;
        font-family: 'Segoe UI', sans-serif;
    `;

    const modal = document.createElement('div');
    modal.style.cssText = `
        background: white; border-radius: 12px; padding: 24px 30px;
        max-width: 700px; width: 95%; max-height: 80vh; overflow: auto;
        box-shadow: 0 20px 60px rgba(0,0,0,0.3);
    `;

    let errorRows = '';
    const errors = result.errors || [];
    errors.forEach(err => {
        const rowNum = err.row_number;
        const errMsgs = err.errors.map(e => `${e.column}: ${e.message}`).join('; ');
        errorRows += `<tr style="border-bottom:1px solid #f0f2f5;">
            <td style="padding:6px 10px; font-weight:600;">${rowNum}</td>
            <td style="padding:6px 10px; color:#dc2626;">${errMsgs}</td>
        </tr>`;
    });

    modal.innerHTML = `
        <h2 style="margin:0 0 4px; color:#dc2626;">❌ Validation Errors</h2>
        <p style="color:#64748b; margin-bottom:12px;">
            <strong>${result.error_count}</strong> error(s) found in <strong>${result.total_rows}</strong> rows.
            Please fix the errors in your Excel file and try again.
        </p>
        <div style="overflow:auto; max-height:300px; border:1px solid #e2e8f0; border-radius:6px; margin-bottom:12px;">
            <table style="width:100%; border-collapse:collapse; font-size:13px;">
                <thead style="position:sticky; top:0; background:#f1f5f9;">
                    <tr>
                        <th style="padding:6px 10px; text-align:left; border-bottom:2px solid #e2e8f0;">Row</th>
                        <th style="padding:6px 10px; text-align:left; border-bottom:2px solid #e2e8f0;">Error</th>
                    </tr>
                </thead>
                <tbody>${errorRows}</tbody>
            </table>
        </div>
        <div style="display:flex; gap:10px; justify-content:flex-end;">
            <button id="validationErrorCloseBtn" style="padding:8px 24px; background:#3b82f6; color:white; border:none; border-radius:6px; font-weight:600; cursor:pointer;">Close</button>
        </div>
    `;

    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    overlay.querySelector('#validationErrorCloseBtn').addEventListener('click', function() {
        document.body.removeChild(overlay);
    });
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) {
            document.body.removeChild(overlay);
        }
    });
}
// ---------- OVERWRITE CONFIRMATION DIALOG (Enhanced) ----------
function showOverwriteConfirmation(duplicates, count) {
    return new Promise((resolve) => {
        // Create overlay
        const overlay = document.createElement('div');
        overlay.id = 'overwriteConfirmOverlay';
        overlay.style.cssText = `
            position: fixed; top:0; left:0; width:100%; height:100%;
            background: rgba(0,0,0,0.6); z-index:3000;
            display: flex; justify-content:center; align-items:center;
            font-family: 'Segoe UI', sans-serif;
        `;

        // Determine all columns from the first duplicate
        let allColumns = [];
        if (duplicates.length > 0 && duplicates[0].columns) {
            allColumns = duplicates[0].columns;
        } else if (duplicates.length > 0 && duplicates[0].old_data) {
            allColumns = Object.keys(duplicates[0].old_data);
        }

        // Build full table
        let tableRows = '';
        const maxShow = Math.min(duplicates.length, 50); // Show up to 50 rows
        for (let i = 0; i < maxShow; i++) {
            const dup = duplicates[i];
            const oldData = dup.old_data || {};
            const newData = dup.new_data || {};

            // Row header with unique value and row number
            let rowHtml = `<tr style="border-bottom:1px solid #e2e8f0; background:${i % 2 === 0 ? '#fafcff' : '#ffffff'};">
                <td style="padding:6px 10px; font-weight:600; font-size:12px; position:sticky; left:0; background:${i % 2 === 0 ? '#fafcff' : '#ffffff'}; z-index:1;">#${i+1}<br><span style="font-weight:400; color:#64748b;">${dup.unique_value}</span></td>`;

            // For each column, show old → new
            allColumns.forEach(col => {
                const oldVal = oldData[col] !== undefined && oldData[col] !== null ? oldData[col] : '—';
                const newVal = newData[col] !== undefined && newData[col] !== null ? newData[col] : '—';
                const isChanged = String(oldVal) !== String(newVal);
                rowHtml += `<td style="padding:4px 8px; font-size:12px; border-left:1px solid #f0f2f5; min-width:80px;">
                    <div style="color:#dc2626; font-size:11px; ${isChanged ? '' : 'opacity:0.5;'}">${oldVal}</div>
                    <div style="color:#16a34a; font-size:11px; ${isChanged ? 'font-weight:500;' : 'opacity:0.5;'}">${isChanged ? '→ ' : '= '}${newVal}</div>
                </td>`;
            });

            rowHtml += `</tr>`;
            tableRows += rowHtml;
        }

        const modal = document.createElement('div');
        modal.style.cssText = `
            background: white; border-radius: 12px; padding: 20px 24px;
            max-width: 95vw; width: 95%; max-height: 90vh; overflow: auto;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            font-family: 'Segoe UI', sans-serif;
        `;

        // Create the modal content with progress bar area
        modal.innerHTML = `
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                <h2 style="margin:0; color:#f59e0b; font-size:20px;">⚠️ Overwrite Confirmation</h2>
                <span style="font-size:13px; color:#64748b;">${count} duplicate row(s)</span>
            </div>
            <p style="color:#64748b; margin-bottom:12px; font-size:14px;">
                The following <strong>${count}</strong> row(s) will be <strong>overwritten</strong> with new data.
                Changes are logged in the audit trail.
            </p>
            <div style="overflow:auto; max-height:45vh; border:1px solid #e2e8f0; border-radius:6px; margin-bottom:12px;">
                <table style="width:100%; border-collapse:collapse; font-size:12px; min-width:${Math.max(400, allColumns.length * 80)}px;">
                    <thead style="position:sticky; top:0; background:#f1f5f9; z-index:2;">
                        <tr>
                            <th style="padding:6px 10px; text-align:left; border-bottom:2px solid #e2e8f0; position:sticky; left:0; background:#f1f5f9; z-index:3; min-width:80px;">Row / ID</th>
                            ${allColumns.map(col => `<th style="padding:6px 10px; text-align:left; border-bottom:2px solid #e2e8f0; white-space:nowrap; font-size:11px;">${col}</th>`).join('')}
                        </tr>
                    </thead>
                    <tbody>${tableRows}</tbody>
                </table>
                ${duplicates.length > 50 ? `<div style="padding:8px; text-align:center; color:#94a3b8; font-size:12px;">... and ${duplicates.length - 50} more</div>` : ''}
            </div>

            <!-- Progress Bar Area (hidden initially) -->
            <div id="overwriteProgressArea" style="display:none; margin:8px 0 12px 0;">
                <div style="background:#f1f5f9; border-radius:4px; height:6px; overflow:hidden;">
                    <div id="overwriteProgressBar" style="background:#f59e0b; height:6px; width:0%; transition:width 0.3s ease;"></div>
                </div>
                <div id="overwriteProgressText" style="font-size:13px; color:#64748b; margin-top:4px;">0 of ${count} rows overwritten</div>
            </div>

            <!-- Button Row -->
            <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:12px; border-top:1px solid #e2e8f0; padding-top:12px;">
                <button id="overwriteCancelBtn" style="padding:8px 24px; background:#e2e8f0; color:#1a1a2e; border:none; border-radius:6px; font-weight:600; cursor:pointer;">Cancel</button>
                <button id="overwriteConfirmBtn" style="padding:8px 24px; background:#f59e0b; color:white; border:none; border-radius:6px; font-weight:600; cursor:pointer; min-width:140px;">
                    ✅ Overwrite ${count} Rows
                </button>
            </div>
        `;

        overlay.appendChild(modal);
        document.body.appendChild(overlay);

        let isResolved = false;

        // ---------- CANCEL ----------
        document.getElementById('overwriteCancelBtn').addEventListener('click', function() {
            if (isResolved) return;
            isResolved = true;
            document.body.removeChild(overlay);
            resolve(false);
        });

        // ---------- CONFIRM (with progress) ----------
        document.getElementById('overwriteConfirmBtn').addEventListener('click', function() {
            if (isResolved) return;

            const confirmBtn = this;
            const progressArea = document.getElementById('overwriteProgressArea');
            const progressBar = document.getElementById('overwriteProgressBar');
            const progressText = document.getElementById('overwriteProgressText');

            // Show progress area
            progressArea.style.display = 'block';
            confirmBtn.disabled = true;
            confirmBtn.innerHTML = '<span class="spinner"></span> Overwriting...';
            confirmBtn.style.background = '#94a3b8';

            // Simulate progress (will be updated by the calling function)
            // We'll resolve after the import is done, but we keep the modal open
            // The progress will be updated from the import function

            // Store reference to the modal elements for the import function
            window._overwriteModal = {
                overlay: overlay,
                confirmBtn: confirmBtn,
                progressBar: progressBar,
                progressText: progressText,
                progressArea: progressArea,
                total: count,
                resolve: resolve,
                isResolved: isResolved
            };

            // Resolve with true – the import will handle the rest
            isResolved = true;
            resolve(true);
        });

        // Close on overlay click
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay && !isResolved) {
                isResolved = true;
                document.body.removeChild(overlay);
                resolve(false);
            }
        });
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
            clientId = localStorage.getItem('selectedClientId');
        }
        if (!clientId) {
            showStatus('⚠️ Please select a client first from the Dashboard.', 'error');
            return;
        }
        if (!fileId || !selectedTemplateId) {
            showStatus('⚠️ Please upload a file and select a template.', 'error');
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

        // ---- NEW LOGIC: Validation Flow ----
        // If validation status is already 'valid', proceed with import
        if (validationStatus === 'valid') {
            // Perform the actual import
            await performImport(payload);
            return;
        }

        // Otherwise, run validation first
        try {
            // Disable button and show spinner
            importBtn.disabled = true;
            importBtn.innerHTML = '<span class="spinner"></span> Validating...';
            showStatus('⏳ Validating data...', 'loading');
            setBadge('⏳ Validating...', 'loading');

            const res = await fetch(API_BASE + '/api/upload/validate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const result = await res.json();
            if (!res.ok) throw new Error(result.message || 'Validation failed');

            if (result.has_errors) {
                // Show validation errors
                showValidationErrors(result);
                validationStatus = 'invalid';
                // Reset button state
                importBtn.innerHTML = 'Validate';
                importBtn.disabled = false;
                importBtn.style.background = '#22c55e'; // Keep green but maybe orange? We'll keep default.
                showStatus('❌ Validation failed. Please fix errors and try again.', 'error');
                setBadge('❌ Validation failed', 'error');
                return;
            }

            // Validation passed
            validationStatus = 'valid';
            // Change button to green "Upload"
            importBtn.innerHTML = '🚀 Upload';
            importBtn.style.background = '#16a34a';
            importBtn.style.color = 'white';
            importBtn.disabled = false;
            showStatus('✅ Validation passed! Click "Upload" to import data.', 'success');
            setBadge('✅ Data valid, ready to upload');
            // Update preview button state (enable preview)
            updatePreviewButtonState();

        } catch (error) {
            showStatus('❌ Validation error: ' + error.message, 'error');
            setBadge('❌ Validation failed', 'error');
            importBtn.innerHTML = 'Validate';
            importBtn.disabled = false;
            importBtn.style.background = '';
            validationStatus = 'invalid';
        }
    });
}
// ---------- PERFORM IMPORT (after validation) ----------
async function performImport(payload) {
    try {
        importBtn.disabled = true;
        importBtn.innerHTML = '<span class="spinner"></span> Processing...';
        showStatus('⏳ Importing data...', 'loading');
        setBadge('⏳ Importing...', 'loading');

        // Check for duplicates (if mode is update)
        let proceed = true;
        if (payload.mode === 'update' && payload.unique_column_id) {
            const checkRes = await fetch(API_BASE + '/api/upload/check-duplicates', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const checkResult = await checkRes.json();
            if (checkRes.ok && checkResult.has_duplicates) {
                // Show overwrite confirmation
                proceed = await showOverwriteConfirmation(checkResult.duplicates, checkResult.count);
                if (!proceed) {
                    showStatus('⛔ Import cancelled by user.', 'info');
                    setBadge('⛔ Cancelled');
                    importBtn.innerHTML = '🚀 Upload';
                    importBtn.disabled = false;
                    return;
                }
            }
        }

        if (!proceed) return;

                // Perform the actual import
        const res = await fetch(API_BASE + '/api/upload/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.message || 'Import failed');

        // Update overwrite progress if the modal is open
        const modalRef = window._overwriteModal;
        if (modalRef && !modalRef.isResolved) {
            // Update progress bar
            const updated = result.updated_count || 0;
            const total = modalRef.total || 1;
            const pct = Math.min(100, Math.round((updated / total) * 100));
            modalRef.progressBar.style.width = pct + '%';
            modalRef.progressText.textContent = `${updated} of ${total} rows overwritten`;

            if (updated >= total) {
                // All done!
                modalRef.confirmBtn.innerHTML = 'OK';
                modalRef.confirmBtn.style.background = '#16a34a';
                modalRef.confirmBtn.disabled = false;
                modalRef.progressBar.style.background = '#16a34a';
                modalRef.progressText.textContent = `✅ ${updated} of ${total} rows overwritten`;

                // Wait for OK click
                modalRef.confirmBtn.onclick = function() {
                    if (modalRef.overlay && modalRef.overlay.parentNode) {
                        document.body.removeChild(modalRef.overlay);
                    }
                    modalRef.resolve(true);
                    window._overwriteModal = null;
                    // Show import summary
                    showImportSummary(result);
                };
            }
            // Continue – the modal stays open until all rows are processed
            // The import is already complete, so we should update the button
            // Actually, since the import is a single API call, all rows are processed at once.
            // So we can immediately show completion.
        }

        if (result.status === 'validation_failed') {
            // This should not happen if validation passed, but handle it
            showValidationErrors(result);
            showStatus('❌ Import validation errors found.', 'error');
            setBadge('❌ Import failed', 'error');
        } else {
            // If modal was not open, show summary popup
            if (!modalRef || modalRef.isResolved) {
                showImportSummary(result);
            }
            setBadge('✅ Import complete');
        }
    } catch (error) {
        showStatus('❌ Import error: ' + error.message, 'error');
        setBadge('❌ Import failed', 'error');
    } finally {
        importBtn.disabled = false;
        importBtn.innerHTML = '🚀 Upload';
        // Reset validation status after import (whether success or failure)
        // But we want to keep the button as "Upload" for the next file? We'll reset on file/template change.
    }
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
        // --- NEW: Check validation ---
        if (validationStatus !== 'valid') {
            showStatus('⚠️ Please validate the data first (click "Validate" button).', 'error');
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

    // Set initial button text
    if (importBtn) {
        importBtn.innerHTML = 'Validate';
        importBtn.style.background = '';
        importBtn.style.color = '';
    }

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