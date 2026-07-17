// ================================================================
// 📤 UPLOAD MODULE – FINAL
// ================================================================

// API_BASE is defined globally in client_context.js

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
const dateDelimiterDropdown = getElement('dateDelimiterDropdown');
const dateOrderDropdown = getElement('dateOrderDropdown');
const dateFormatGroup = getElement('dateFormatGroup');
const dateFormatTriggerBtn = getElement('dateFormatTriggerBtn');
const dateFormatTriggerLabel = getElement('dateFormatTriggerLabel');
const dateFormatPopover = getElement('dateFormatPopover');

const uploadArea = getElement('uploadArea');
const fileInput = getElement('fileInput');
if (!fileInput) {
    console.error('❌ File input element with id "fileInput" not found – upload will not work');
}
const fileInfo = getElement('fileInfo');
const fileName = getElement('fileName');
const fileSize = getElement('fileSize');
const clearBtn = getElement('clearBtn');

const mappingPanel = getElement('mappingPanel');
const mappingBody = getElement('mappingBody');
const mappingCount = getElement('mappingCount');

const modeActions = getElement('modeActions');

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

// ---------- DATE FORMAT OPTIONS ----------
// Order codes (DMY/MDY/YMD) match the backend's parse_date_flexible() `order` param
// (routers/import_export.py) so Phase 4 can wire these straight through.
const DATE_ORDER_OPTIONS = {
    '/': [['DMY', 'DD/MM/YYYY'], ['MDY', 'MM/DD/YYYY'], ['YMD', 'YYYY/MM/DD']],
    '-': [['DMY', 'DD-MM-YYYY'], ['MDY', 'MM-DD-YYYY'], ['YMD', 'YYYY-MM-DD']],
    '': [['DMY', 'DDMMYYYY'], ['MDY', 'MMDDYYYY'], ['YMD', 'YYYYMMDD']]
};

// ---------- STATE ----------
let selectedTemplateId = null;
let selectedDateDelimiter = '/';
let selectedDateOrder = 'DMY';
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

// Pagination state variables
let previewCurrentPage = 1;
let previewPageSize = 50;
let previewTotalRows = 0;
let previewAllRows = [];
let previewHeaders = [];
let previewHiddenCount = 0; // rows hidden from the preview because the user excluded them

// ---------- ROW SELECTION GRID STATE (Phase 2 of the row-exclusion requirement) ----------
// excludedRowNumbers holds Excel row numbers (header row=1, first data row=2 -- same
// scheme routers/import_export.py's excluded_rows/validation_errors[].row_number use)
// that the user has unchecked. Empty = nothing excluded (all rows included), matching
// the "checked by default" requirement.
let excludedRowNumbers = new Set();
let rowExclusionAllRows = [];   // current sheet's data rows (raw arrays), same shape as previewAllRows
let rowExclusionHeaders = [];
let rowExclusionCurrentPage = 1;
const ROW_EXCLUSION_PAGE_SIZE = 10; // fixed page size for this compact inline grid

// validationFailedRows (Phase 4): Excel row number -> that row's validation `errors` array
// (same {column, excel_column, error_type, message} shape showValidationErrors() renders).
// Distinct from excludedRowNumbers -- a validation failure never auto-excludes a row.
let validationFailedRows = new Map();
// ---------- STEP INDICATOR / SHEET-DOWNLOAD REVEAL (Phase 2 of the Upload header reorg) ----------
// Download Template + Sheet selector are gated together behind Template selection instead of
// Download always being visible (see #sheetDownloadGroup in upload.html). Single place both
// reveal and hide go through.
function setSheetDownloadGroupVisible(visible) {
    const group = document.getElementById('sheetDownloadGroup');
    if (group) group.style.display = visible ? 'flex' : 'none';
}

// Match badge + Preview button (Phase 4) -- revealed together once mapping resolves, same
// reveal timing as the Date format group/row exclusion grid.
function setMatchPreviewGroupVisible(visible) {
    const group = document.getElementById('matchPreviewGroup');
    if (group) group.style.display = visible ? 'flex' : 'none';
}

// Go to Data Bank (Phase 4) -- stays visually inert until Template -> Sheet -> mapping all
// resolve; navigation behavior in the click handler (window.onload, further down) is unchanged.
function updateGoToDataBankState(enabled) {
    const btn = document.getElementById('goToDataBankBtn');
    if (btn) btn.disabled = !enabled;
}

// Moves the step-indicator strip's .active class between step-items (data-step attribute,
// added in Phase 1's markup). Only steps 1/2 are used this phase -- steps 3/4 stay neutral,
// their activation is Phases 3-4.
function updateStepIndicator(step) {
    document.querySelectorAll('#uploadStepIndicator .step-item').forEach(item => {
        item.classList.toggle('active', item.dataset.step === String(step));
    });
}

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
        return;
    }
    statusMessage.innerHTML = msg;
    if (extra) {
        statusMessage.innerHTML += '<div style="font-size:12px; opacity:0.9; margin-top:2px;">' + extra + '</div>';
    }
    if (statusExtra) statusExtra.textContent = '';
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

// ---------- DATE FORMAT DROPDOWNS ----------
// Repopulates the Date Order dropdown based on the chosen delimiter, defaulting to DMY.
function populateDateOrderDropdown(delimiter) {
    if (!dateOrderDropdown) return;
    const options = DATE_ORDER_OPTIONS[delimiter] || DATE_ORDER_OPTIONS['/'];
    dateOrderDropdown.innerHTML = '';
    options.forEach(([value, label]) => {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = label;
        dateOrderDropdown.appendChild(opt);
    });
    selectedDateOrder = 'DMY';
    dateOrderDropdown.value = selectedDateOrder;
}

if (dateDelimiterDropdown) {
    dateDelimiterDropdown.addEventListener('change', function() {
        selectedDateDelimiter = this.value;
        populateDateOrderDropdown(selectedDateDelimiter);
        refreshDateFormatPreview();
        updateDateFormatTriggerLabel();
    });
}

if (dateOrderDropdown) {
    dateOrderDropdown.addEventListener('change', function() {
        selectedDateOrder = this.value;
        refreshDateFormatPreview();
        updateDateFormatTriggerLabel();
    });
}

// ---------- DATE FORMAT TRIGGER + POPOVER (Phase 3 of the Upload header reorg) ----------
// Collapses the Date Delimiter/Order dropdowns behind one trigger button + popover. The
// popover holds the real, listener-bound <select> elements (see upload.html) rather than
// JS-rebuilt content, so nothing here needs to touch the change listeners above.
let dateFormatPopoverOpen = false;

function setDateFormatGroupVisible(visible) {
    if (dateFormatGroup) dateFormatGroup.style.display = visible ? 'flex' : 'none';
}

// Reflects the current selectedDateDelimiter/selectedDateOrder on the trigger button, e.g.
// "Date format: DD-MM-YYYY", using the same friendly labels DATE_ORDER_OPTIONS already holds.
function updateDateFormatTriggerLabel() {
    if (!dateFormatTriggerLabel) return;
    const options = DATE_ORDER_OPTIONS[selectedDateDelimiter] || DATE_ORDER_OPTIONS['/'];
    const match = options.find(([code]) => code === selectedDateOrder);
    dateFormatTriggerLabel.textContent = match ? match[1] : '';
}

function openDateFormatPopover() {
    if (!dateFormatPopover) return;
    dateFormatPopover.style.display = 'flex';
    dateFormatPopoverOpen = true;
}

function closeDateFormatPopover() {
    if (!dateFormatPopover) return;
    dateFormatPopover.style.display = 'none';
    dateFormatPopoverOpen = false;
}

if (dateFormatTriggerBtn) {
    dateFormatTriggerBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        if (dateFormatPopoverOpen) {
            closeDateFormatPopover();
        } else {
            openDateFormatPopover();
        }
    });
}

// Close the popover on any click outside it (the trigger's own click is stopped from
// propagating above, so this only fires for clicks elsewhere) -- same pattern as
// verification.js's #voucherChangePopover outside-click listener.
document.addEventListener('click', function(e) {
    if (dateFormatPopoverOpen && dateFormatPopover && !dateFormatPopover.contains(e.target)) {
        closeDateFormatPopover();
    }
});

updateDateFormatTriggerLabel(); // Set the initial trigger label to match the defaults above

// ---------- DATE FORMAT LIVE PREVIEW (Phase 5) ----------
// Fetches 2-3 real sample dates from the uploaded file's mapped Date columns,
// parsed under the currently selected delimiter/order, so a wrong format
// choice is caught before running full validation/import. No-op until a
// file is uploaded and template mapping is known (Date columns identified).
async function refreshDateFormatPreview() {
    const container = getElement('dateFormatPreview');
    if (!container) return;

    if (!fileId || !currentMappings || currentMappings.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }

    const dateMappings = currentMappings
        .filter(m => m.data_type === 'Date' && m.excel_column)
        .map(m => ({ template_column_name: m.column_name, excel_column_name: m.excel_column }));

    if (dateMappings.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }

    try {
        const res = await fetch(API_BASE + '/api/upload/date-preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_id: fileId,
                sheet_name: currentSheet || 'Sheet1',
                date_mappings: dateMappings,
                date_delimiter: selectedDateDelimiter,
                date_order: selectedDateOrder
            })
        });
        if (!res.ok) throw new Error('Preview request failed');
        const data = await res.json();
        renderDateFormatPreview(data.samples || []);
    } catch (error) {
        console.error('Date preview error:', error);
        container.style.display = 'none';
        container.innerHTML = '';
    }
}

// Renders up to 3 sample lines as small text (not a table) -- a sanity check,
// not a data grid. Invalid samples render in red with a clear error message.
function renderDateFormatPreview(samples) {
    const container = getElement('dateFormatPreview');
    if (!container) return;

    if (!samples || samples.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }

    const shown = samples.slice(0, 3);
    let html = '<label>Preview</label>';
    shown.forEach(s => {
        if (s.valid) {
            html += `<span class="date-preview-line">${s.raw} → ${s.parsed}</span>`;
        } else {
            html += `<span class="date-preview-line error">Could not parse "${s.raw}" as ${selectedDateOrder}</span>`;
        }
    });
    container.innerHTML = html;
    container.style.display = 'flex';
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
            setSheetDownloadGroupVisible(false); // Template cleared -- hide Sheet/Download again
            setDateFormatGroupVisible(false); // ...and the Date format trigger/popover too
            closeDateFormatPopover();
            setMatchPreviewGroupVisible(false); // ...and the match badge/Preview group...
            updateGoToDataBankState(false); // ...and disable Go to Data Bank again
            updateStepIndicator(1);
            updatePreviewButtonState();
            return;
        }
        selectedTemplateId = parseInt(id);
        // A template is now chosen -- reveal Sheet/Download together and advance the step
        // strip, regardless of whether the column-fetch below succeeds (Download only needs
        // the template id, already known; Sheet is populated independently once a file loads).
        setSheetDownloadGroupVisible(true);
        updateStepIndicator(2);
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
    // Click to open file picker – with safety check
    uploadArea.addEventListener('click', function(e) {
        if (fileInput) {
            fileInput.click();
        } else {
            console.error('❌ fileInput element not found');
        }
    });
    
    // Drag and drop events
    uploadArea.addEventListener('dragover', function(e) {
        e.preventDefault();
        e.stopPropagation();
        this.classList.add('dragover');
    });
    
    uploadArea.addEventListener('dragleave', function(e) {
        e.preventDefault();
        e.stopPropagation();
        this.classList.remove('dragover');
    });
    
    uploadArea.addEventListener('drop', function(e) {
        e.preventDefault();
        e.stopPropagation();
        this.classList.remove('dragover');
        if (e.dataTransfer && e.dataTransfer.files.length) {
            handleFile(e.dataTransfer.files[0]);
        } else {
            console.warn('⚠️ No file dropped');
        }
    });
    
} else {
    console.error('❌ Upload area element not found');
}

if (fileInput) {
    fileInput.addEventListener('change', function(e) {
        if (this.files && this.files.length > 0) {
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
    setSheetDownloadGroupVisible(false); // Full reset -- collapse the Sheet/Download group too
    updateStepIndicator(1);
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

    // Clear the date format live preview (Phase 5) – no file/mapping left to sample from.
    const dateFormatPreviewEl = getElement('dateFormatPreview');
    if (dateFormatPreviewEl) {
        dateFormatPreviewEl.style.display = 'none';
        dateFormatPreviewEl.innerHTML = '';
    }
    // Hide the Date format trigger/popover too (Phase 3) -- no mapping left to base it on.
    setDateFormatGroupVisible(false);
    closeDateFormatPopover();

    // Hide the match badge/Preview group and disable Go to Data Bank again (Phase 4) --
    // no mapping left to base either on.
    setMatchPreviewGroupVisible(false);
    updateGoToDataBankState(false);

    // Hide the row selection grid and clear its state (Phase 2 of row-exclusion) –
    // no file/mapping left to build it from.
    const rowExclusionContainerEl = document.getElementById('rowExclusionContainer');
    if (rowExclusionContainerEl) rowExclusionContainerEl.style.display = 'none';
    excludedRowNumbers = new Set();
    validationFailedRows = new Map();
    rowExclusionAllRows = [];
    rowExclusionHeaders = [];
    rowExclusionCurrentPage = 1;

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
        // populateUniqueIdDropdown() removed – no longer needed

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

        // Date columns are now known via the mapping – reveal the Date format trigger/popover
        // and show the live sample-date preview inside it.
        setDateFormatGroupVisible(true);
        refreshDateFormatPreview();

        // Mapping is complete – show the row selection grid (Phase 2 of row-exclusion).
        renderRowExclusionGrid();

        // Mapping resolved (Phase 4) -- reveal the match badge/Preview group, enable Go to
        // Data Bank, and advance the step strip to "Map columns".
        setMatchPreviewGroupVisible(true);
        updateGoToDataBankState(true);
        updateStepIndicator(3);

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
            // Column mapping changed -- reset row selection grid checkboxes/flags (Phase 4
            // of the row-exclusion requirement), since the key column basis may have shifted.
            resetRowExclusionSelections();
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

// ---------- UNIQUE ID DROPDOWN (removed – upsert is automatic) ----------

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
        max-width: 560px; width: 90%; box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        font-family: 'Segoe UI', sans-serif;
        max-height: 90vh;
        overflow-y: auto;
    `;
    
    const total = result.total_rows || 0;
    const inserted = result.imported_count || 0;
    const updated = result.updated_count || 0;
    const errors = result.validation_errors || [];
    const errorCount = errors.length;
    
    // --- Change detection counts ---
    const cd = result.change_detection || {};
    const unchanged = cd.unchanged_count || 0;
    const changed = cd.changed_count || 0;
    const newRows = cd.new_count || 0;
    const deleted = cd.deleted_count || 0;
    
    let statusIcon = '✅';
    let statusColor = '#16a34a';
    let statusText = 'Import Successful';
    if (errorCount > 0) {
        statusIcon = '⚠️';
        statusColor = '#f59e0b';
        statusText = 'Import with Warnings';
    }

    // Build a plain, single-line outcome summary (no inline emoji, no run-on sentence).
    let detailMessage = '';
    if (inserted > 0 && updated > 0) {
        detailMessage = `${inserted} new row${inserted === 1 ? '' : 's'} inserted &middot; ${updated} existing row${updated === 1 ? '' : 's'} updated`;
    } else if (inserted > 0) {
        detailMessage = `${inserted} new row${inserted === 1 ? '' : 's'} inserted`;
    } else if (updated > 0) {
        detailMessage = `${updated} existing row${updated === 1 ? '' : 's'} updated`;
    } else {
        detailMessage = 'No changes were made';
    }

    // Change Detection Summary: labeled stat block matching the tile styling below, color-coded via left border instead of emoji.
    let changeSummary = '';
    if (unchanged > 0 || changed > 0 || newRows > 0 || deleted > 0) {
        changeSummary = `
            <div style="margin-top:20px;">
                <div style="font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:0.04em; color:#94a3b8; margin-bottom:8px;">Change Detection</div>
                <div style="display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:8px;">
                    <div style="background:#f8fafc; border-left:3px solid #94a3b8; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${unchanged}</div>
                        <div style="font-size:11px; color:#64748b;">Unchanged</div>
                    </div>
                    <div style="background:#f8fafc; border-left:3px solid #f59e0b; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${changed}</div>
                        <div style="font-size:11px; color:#64748b;">Changed</div>
                    </div>
                    <div style="background:#f8fafc; border-left:3px solid #16a34a; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${newRows}</div>
                        <div style="font-size:11px; color:#64748b;">New</div>
                    </div>
                    <div style="background:#f8fafc; border-left:3px solid #dc2626; border-radius:6px; padding:8px 10px;">
                        <div style="font-size:18px; font-weight:700; color:#334155;">${deleted}</div>
                        <div style="font-size:11px; color:#64748b;">Missing</div>
                    </div>
                </div>
                <div style="font-size:11px; color:#94a3b8; margin-top:8px;">
                    Changed rows have been reset to "Pending" and require re-verification.
                </div>
            </div>
        `;
    }

    // Explanatory note about debit/credit lines, styled as a plain muted info box.
    const note = `<div style="font-size:12px; color:#64748b; background:#f8fafc; padding:10px 12px; border-radius:6px; margin-top:16px;">
        <strong style="color:#475569;">Note:</strong> Each voucher may appear twice in the file (debit and credit).
        The system stores one transaction per voucher ID. Total records in the Data Bank reflect distinct vouchers.
    </div>`;

    modal.innerHTML = `
        <div style="text-align:center; margin-bottom:20px;">
            <div style="font-size:40px; line-height:1;">${statusIcon}</div>
            <h2 style="margin:10px 0 4px; color:#1a1a2e; font-size:20px;">${statusText}</h2>
            <p style="color:#64748b; font-size:14px; margin:0;">${detailMessage}</p>
        </div>
        <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px;">
            <div style="background:#f8fafc; padding:12px; border-radius:8px; text-align:center;">
                <div style="font-size:24px; font-weight:700; color:#3b82f6;">${total}</div>
                <div style="font-size:12px; color:#64748b;">Lines Processed</div>
            </div>
            <div style="background:#f8fafc; padding:12px; border-radius:8px; text-align:center;">
                <div style="font-size:24px; font-weight:700; color:#16a34a;">${inserted}</div>
                <div style="font-size:12px; color:#64748b;">New Rows</div>
            </div>
            <div style="background:#f8fafc; padding:12px; border-radius:8px; text-align:center;">
                <div style="font-size:24px; font-weight:700; color:#f59e0b;">${updated}</div>
                <div style="font-size:12px; color:#64748b;">Updated Rows</div>
            </div>
        </div>
        ${changeSummary}
        ${note}
        ${errorCount > 0 ? `
            <div style="background:#fef2f2; border:1px solid #fecaca; padding:12px; border-radius:8px; margin-top:16px; max-height:150px; overflow-y:auto;">
                <div style="font-weight:600; color:#991b1b; margin-bottom:4px; font-size:13px;">${errorCount} validation error${errorCount === 1 ? '' : 's'}</div>
                ${errors.slice(0,5).map(err => `
                    <div style="font-size:12px; color:#991b1b; padding:2px 0; border-bottom:1px solid #fecaca;">
                        Row ${err.row_number}: ${err.errors.map(e => `${e.column} - ${e.message}`).join('; ')}
                    </div>
                `).join('')}
                ${errors.length > 5 ? `<div style="font-size:12px; color:#991b1b; margin-top:2px;">...and ${errors.length - 5} more.</div>` : ''}
            </div>
        ` : ''}
        <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:20px; flex-wrap:wrap;">
            <a href="/static/databank.html" target="_blank" style="padding:8px 20px; background:#3b82f6; color:white; border:none; border-radius:6px; font-weight:600; cursor:pointer; text-decoration:none; display:inline-block; font-size:14px;">View in Data Bank</a>
            <button id="importSummaryCloseBtn" style="padding:8px 24px; background:#e2e8f0; color:#1a1a2e; border:none; border-radius:6px; font-weight:600; cursor:pointer; font-size:14px;">Close</button>
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
            mode: 'create',
            unique_column_id: null,
            sheet_name: currentSheet || 'Sheet1',
            // Selected date format (Phase 3 dropdowns) – drives Date validation/import parsing on the backend.
            date_delimiter: selectedDateDelimiter,
            date_order: selectedDateOrder,
            // Rows the user unchecked in the row selection grid (Phase 2 of row-exclusion) –
            // Excel row numbers, matches routers/import_export.py's excluded_rows contract.
            excluded_rows: Array.from(excludedRowNumbers)
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
                // Mark the failing rows in the row selection grid (Phase 4) -- distinct from
                // user-excluded rows, and does not auto-exclude them.
                setValidationFailedRows(result.errors);
                validationStatus = 'invalid';
                // Reset button state
                importBtn.innerHTML = 'Validate';
                importBtn.disabled = false;
                importBtn.style.background = '#22c55e'; // Keep green but maybe orange? We'll keep default.
                showStatus('❌ Validation failed. Please fix errors and try again.', 'error');
                setBadge('❌ Validation failed', 'error');
                return;
            }

                                    // Validation passed – clear any stale failed-row markers from a
            // prior attempt (Phase 4), then show simple success message
            setValidationFailedRows([]);
            const totalRows = result.total_rows || 0;
            const newRowsCount = result.new_rows_count || 0;
            const existingRowsCount = result.existing_rows_count || 0;
            validationStatus = 'valid';
            importBtn.innerHTML = '🚀 Upload';
            importBtn.style.background = '#16a34a';
            importBtn.style.color = 'white';
            importBtn.disabled = false;
            let statusMsg = `✅ Validation passed! ${totalRows} rows identified. Click "Upload" to import all rows.`;
            const extraMsg = `<span style="color: maroon; font-weight: bold;">${newRowsCount} new row(s) • ${existingRowsCount} already exists in client data</span>`;
            showStatus(statusMsg, 'success', extraMsg);
            setBadge('✅ Data valid, ready to upload');
            updatePreviewButtonState();
            // Validation succeeded -- this is the genuine "ready to import" milestone the step
            // strip's final step represents (confirmed with the user: mapping-resolved alone
            // isn't distinct enough, since it fires at the same instant Import becomes enabled).
            updateStepIndicator(4);

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
// ---------- PERFORM IMPORT (after validation) ----------
async function performImport(payload) {
    try {
        importBtn.disabled = true;
        importBtn.innerHTML = '<span class="spinner"></span> Processing...';
        showStatus('⏳ Importing data...', 'loading');
        setBadge('⏳ Importing...', 'loading');

        // --- FIX: Add skip_validation if validation already passed ---
        if (validationStatus === 'valid') {
            payload.skip_validation = true;
        }

                // No duplicate check – we always insert new rows (simple insert)
        let proceed = true;

        if (!proceed) return;

        // Perform the actual import
        const res = await fetch(API_BASE + '/api/upload/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.message || 'Import failed');

        // --- Handle overwrite progress (if modal is open) ---
        const modalRef = window._overwriteModal;
        if (modalRef && !modalRef.isResolved) {
            const updated = result.updated_count || 0;
            const total = modalRef.total || 1;
            const pct = Math.min(100, Math.round((updated / total) * 100));
            modalRef.progressBar.style.width = pct + '%';
            modalRef.progressText.textContent = `${updated} of ${total} rows overwritten`;

            if (updated >= total) {
                modalRef.confirmBtn.innerHTML = 'OK';
                modalRef.confirmBtn.style.background = '#16a34a';
                modalRef.confirmBtn.disabled = false;
                modalRef.progressBar.style.background = '#16a34a';
                modalRef.progressText.textContent = `✅ ${updated} of ${total} rows overwritten`;

                modalRef.confirmBtn.onclick = function() {
                    if (modalRef.overlay && modalRef.overlay.parentNode) {
                        document.body.removeChild(modalRef.overlay);
                    }
                    modalRef.resolve(true);
                    window._overwriteModal = null;
                    showImportSummary(result);
                };
            }
        }

        if (result.status === 'validation_failed') {
            showValidationErrors(result);
            // Mark the failing rows in the row selection grid (Phase 4), same as the
            // pre-import validate path -- does not auto-exclude them.
            setValidationFailedRows(result.errors);
            showStatus('❌ Import validation errors found.', 'error');
            setBadge('❌ Import failed', 'error');
        } else {
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
    }
}

function renderPreviewPage() {
    const previewHead = document.getElementById('previewSheetHead');
    const previewBody = document.getElementById('previewSheetBody');
    const previewInfo = document.getElementById('previewInfo');
    const previewRowRange = document.getElementById('previewRowRange');
    const previewPageInfo = document.getElementById('previewPageInfo');
    const previewPrevPage = document.getElementById('previewPrevPage');
    const previewNextPage = document.getElementById('previewNextPage');

    const headers = previewHeaders || [];
    const totalRows = previewAllRows.length;
    previewTotalRows = totalRows;

    // Calculate pagination details
    const totalPages = Math.ceil(totalRows / previewPageSize) || 1;
    if (previewCurrentPage > totalPages) {
        previewCurrentPage = totalPages;
    }
    if (previewCurrentPage < 1) {
        previewCurrentPage = 1;
    }

    const startIndex = (previewCurrentPage - 1) * previewPageSize;
    const endIndex = Math.min(startIndex + previewPageSize, totalRows);

    // Build headers
    previewHead.innerHTML = '';
    if (headers.length) {
        const tr = document.createElement('tr');
        
        // Add S.No header column as the first column with specified styling
        const thSNo = document.createElement('th');
        thSNo.textContent = 'S.No';
        thSNo.style.textAlign = 'center';
        thSNo.style.color = '#64748b';
        thSNo.style.width = '50px';
        thSNo.style.fontWeight = '500';
        tr.appendChild(thSNo);

        headers.forEach(h => {
            const th = document.createElement('th');
            th.textContent = h || '—';
            tr.appendChild(th);
        });
        previewHead.appendChild(tr);
    }

    // Build body for current page rows
    previewBody.innerHTML = '';
    const pageRows = previewAllRows.slice(startIndex, endIndex);

    pageRows.forEach((row, index) => {
        const tr = document.createElement('tr');
        const rowIndex = startIndex + index; // Actual 0-based index in full Excel sheet
        
        // Add S.No serial number cell (starting from 1) before all other cells
        const tdSNo = document.createElement('td');
        tdSNo.textContent = rowIndex + 1;
        tdSNo.style.textAlign = 'center';
        tdSNo.style.color = '#64748b';
        tdSNo.style.width = '50px';
        tdSNo.style.fontWeight = '500';
        tr.appendChild(tdSNo);

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

    // Update displays
    if (previewInfo) {
        previewInfo.textContent = `${totalRows} rows · ${headers.length} columns` +
            (previewHiddenCount > 0 ? ` (${previewHiddenCount} excluded)` : '');
    }
    if (previewRowRange) {
        if (totalRows === 0) {
            previewRowRange.textContent = 'Showing 0-0 of 0 rows';
        } else {
            previewRowRange.textContent = `Showing ${startIndex + 1}-${endIndex} of ${totalRows} rows`;
        }
    }
    if (previewPageInfo) {
        previewPageInfo.textContent = `Page ${previewCurrentPage} of ${totalPages}`;
    }

    // Disable buttons at boundaries
    if (previewPrevPage) {
        previewPrevPage.disabled = (previewCurrentPage === 1);
    }
    if (previewNextPage) {
        previewNextPage.disabled = (previewCurrentPage === totalPages);
    }
}

// ---------- ROW SELECTION GRID (Phase 2 of the row-exclusion requirement) ----------
// Reads the current sheet's raw rows the same way showPreview()/getFirstDataRow() do,
// and (re)builds the checkbox-per-row grid. Called once column mapping succeeds (including
// on a sheet change, via autoMapColumns()'s success path) -- a fresh excludedRowNumbers Set
// here is correct since the underlying rows genuinely differ. A per-column remap that
// doesn't change sheets uses the lighter resetRowExclusionSelections() instead (Phase 4).
function renderRowExclusionGrid() {
    const container = document.getElementById('rowExclusionContainer');
    if (!container || !workbookData) return;

    try {
        const workbook = XLSX.read(workbookData, { type: 'array' });
        const sheetName = currentSheet || workbook.SheetNames[0];
        const sheet = workbook.Sheets[sheetName];
        // defval: '' -- same fix as showPreview(): avoids holes in the row array so
        // renderRowExclusionPage()'s row.forEach() doesn't skip blank cells and shift
        // later columns left.
        const jsonData = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: '' });

        rowExclusionHeaders = jsonData[0] || [];
        rowExclusionAllRows = jsonData.slice(1);
        excludedRowNumbers = new Set();
        validationFailedRows = new Map(); // new sheet/mapping -- any prior validation result is stale
        rowExclusionCurrentPage = 1;

        container.style.display = 'block';
        renderRowExclusionPage();
    } catch (error) {
        console.error('Row selection grid error:', error);
        container.style.display = 'none';
    }
}

// ---------- AUTO-FLAG HEURISTIC (Phase 3 of the row-exclusion requirement) ----------
// A visual hint only -- never unchecks a row automatically, the user always decides.
// Key column = the first mapped template column (currentMappings[0]), mirroring the
// app-wide convention that the first template column is the document's unique key
// (see get_document_group_id() / the Conventions note in CLAUDE.md).
function getRowExclusionKeyColumnIndex() {
    if (!currentMappings || currentMappings.length === 0) return -1;
    const keyMapping = currentMappings[0];
    if (!keyMapping || !keyMapping.excel_column) return -1;
    return (rowExclusionHeaders || []).indexOf(keyMapping.excel_column);
}

// Flags a row when the key column is blank, or its text contains "total" (case-insensitive) --
// this single substring check also naturally catches "subtotal"/"grand total". Simple and
// transparent by design, no fuzzy matching.
function isRowExclusionFlagged(row, keyColIndex) {
    if (keyColIndex < 0) return false;
    const value = row[keyColIndex];
    if (value === undefined || value === null || String(value).trim() === '') return true;
    return String(value).toLowerCase().includes('total');
}

// ---------- LIVE "X of Y rows will be imported" COUNTER (Phase 3) ----------
// Always reflects the whole dataset (not just the visible page).
function updateRowExclusionCounter() {
    const counterEl = document.getElementById('rowExclusionCounter');
    if (!counterEl) return;
    const total = rowExclusionAllRows.length;
    const included = total - excludedRowNumbers.size;
    counterEl.textContent = `${included} of ${total} rows will be imported`;
}

// ---------- SELECT ALL / DESELECT ALL (Phase 3) ----------
// Operate on the full excludedRowNumbers Set, so they apply across all pages, not
// just the currently visible one; a single re-render refreshes the visible page + counter.
function rowExclusionSelectAll() {
    excludedRowNumbers.clear();
    renderRowExclusionPage();
}

function rowExclusionDeselectAll() {
    const total = rowExclusionAllRows.length;
    excludedRowNumbers = new Set();
    for (let i = 0; i < total; i++) {
        excludedRowNumbers.add(i + 2); // Excel row numbers: first data row = 2
    }
    renderRowExclusionPage();
}

// ---------- RESET ON MAPPING CHANGE (Phase 4 of the row-exclusion requirement) ----------
// Sheet changes already reset the grid correctly via renderRowExclusionGrid() (called from
// autoMapColumns()'s success path), since the underlying rows genuinely differ. A per-column
// remap in the mapping table doesn't change the sheet's rows -- only which column feeds the
// auto-flag heuristic/validation -- so this is a lighter reset: clear selections/flags and
// re-render, without re-reading the file.
function resetRowExclusionSelections() {
    if (!rowExclusionAllRows.length) return; // grid not built yet, nothing to reset
    excludedRowNumbers = new Set();
    validationFailedRows = new Map(); // mapping changed -- any prior validation result is stale
    rowExclusionCurrentPage = 1;
    renderRowExclusionPage();
}

// ---------- VALIDATION-FAILED ROWS (Phase 4) ----------
// Captures the validate/import response's errors[] (row_number + errors) so the grid can
// show which rows failed, distinct from which rows the user chose to exclude. Never touches
// excludedRowNumbers -- a validation failure must not auto-exclude a row.
function setValidationFailedRows(errorsList) {
    validationFailedRows = new Map();
    (errorsList || []).forEach(e => {
        validationFailedRows.set(e.row_number, e.errors || []);
    });
    if (document.getElementById('rowExclusionContainer') && rowExclusionAllRows.length) {
        renderRowExclusionPage();
    }
}

function renderRowExclusionPage() {
    const headRow = document.getElementById('rowExclusionHeadRow');
    const body = document.getElementById('rowExclusionBody');
    const rangeEl = document.getElementById('rowExclusionRange');
    const pageInfoEl = document.getElementById('rowExclusionPageInfo');
    const prevBtn = document.getElementById('rowExclusionPrevPage');
    const nextBtn = document.getElementById('rowExclusionNextPage');
    if (!headRow || !body) return;

    const headers = rowExclusionHeaders || [];
    const totalRows = rowExclusionAllRows.length;

    const totalPages = Math.ceil(totalRows / ROW_EXCLUSION_PAGE_SIZE) || 1;
    if (rowExclusionCurrentPage > totalPages) rowExclusionCurrentPage = totalPages;
    if (rowExclusionCurrentPage < 1) rowExclusionCurrentPage = 1;

    const startIndex = (rowExclusionCurrentPage - 1) * ROW_EXCLUSION_PAGE_SIZE;
    const endIndex = Math.min(startIndex + ROW_EXCLUSION_PAGE_SIZE, totalRows);

    // Auto-flag heuristic key column (Phase 3) -- computed once per page render
    const keyColIndex = getRowExclusionKeyColumnIndex();

    // Build header: checkbox col + Row col + one <th> per Excel column
    headRow.innerHTML = '';
    const thCheck = document.createElement('th');
    thCheck.className = 'row-exclusion-check-col';
    // "Mother" checkbox (Fix 1): checked = all rows included, unchecked = all excluded,
    // indeterminate = a partial selection -- standard select-all convention (e.g. Gmail).
    // Reflects the whole dataset, not just the visible page, same scope as
    // rowExclusionSelectAll()/rowExclusionDeselectAll() which it now triggers.
    const masterCheckbox = document.createElement('input');
    masterCheckbox.type = 'checkbox';
    const totalForMaster = rowExclusionAllRows.length;
    const excludedCount = excludedRowNumbers.size;
    masterCheckbox.checked = totalForMaster > 0 && excludedCount === 0;
    masterCheckbox.indeterminate = excludedCount > 0 && excludedCount < totalForMaster;
    masterCheckbox.title = 'Select all / deselect all rows';
    masterCheckbox.addEventListener('change', function() {
        if (this.checked) {
            rowExclusionSelectAll();
        } else {
            rowExclusionDeselectAll();
        }
    });
    thCheck.appendChild(masterCheckbox);
    headRow.appendChild(thCheck);
    const thRowNum = document.createElement('th');
    thRowNum.className = 'row-exclusion-rownum-col';
    thRowNum.textContent = 'Row';
    headRow.appendChild(thRowNum);
    headers.forEach(h => {
        const th = document.createElement('th');
        th.textContent = h || '—';
        headRow.appendChild(th);
    });

    // Build body for current page
    body.innerHTML = '';
    const pageRows = rowExclusionAllRows.slice(startIndex, endIndex);
    pageRows.forEach((row, index) => {
        // jsonData[0] is the header (Excel row 1), jsonData[1] is the first data row
        // (Excel row 2) -- so array index i (0-based, after slicing off the header) is
        // Excel row i + 2. Matches routers/import_export.py's excluded_rows scheme.
        const excelRowNumber = startIndex + index + 2;

        const tr = document.createElement('tr');
        if (excludedRowNumbers.has(excelRowNumber)) tr.classList.add('row-excluded');
        const flagged = isRowExclusionFlagged(row, keyColIndex);
        if (flagged) tr.classList.add('row-flagged');
        const failed = validationFailedRows.has(excelRowNumber);
        if (failed) tr.classList.add('row-validation-failed');

        const tdCheck = document.createElement('td');
        tdCheck.className = 'row-exclusion-check-col';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = !excludedRowNumbers.has(excelRowNumber);
        checkbox.addEventListener('change', function() {
            if (this.checked) {
                excludedRowNumbers.delete(excelRowNumber);
                tr.classList.remove('row-excluded');
            } else {
                excludedRowNumbers.add(excelRowNumber);
                tr.classList.add('row-excluded');
            }
            updateRowExclusionCounter();
        });
        tdCheck.appendChild(checkbox);
        tr.appendChild(tdCheck);

        const tdRowNum = document.createElement('td');
        tdRowNum.className = 'row-exclusion-rownum-col';
        // Prefixes concatenate rather than overwrite -- a row can be both auto-flagged and
        // validation-failed at once, and both signals should stay visible (e.g. "⚠️❌ 5").
        let prefix = (flagged ? '⚠️' : '') + (failed ? '❌' : '');
        tdRowNum.textContent = (prefix ? prefix + ' ' : '') + excelRowNumber;
        const titleParts = [];
        if (flagged) titleParts.push('Possible junk row: blank or "total"/"subtotal" value in the key column');
        if (failed) {
            const rowErrors = validationFailedRows.get(excelRowNumber) || [];
            const msg = rowErrors.map(e => `${e.column}: ${e.message}`).join('; ');
            titleParts.push('Failed validation' + (msg ? ': ' + msg : ''));
        }
        if (titleParts.length) tdRowNum.title = titleParts.join(' | ');
        tr.appendChild(tdRowNum);

        row.forEach((cell, idx) => {
            const td = document.createElement('td');
            td.textContent = cell !== undefined ? cell : '';
            tr.appendChild(td);
        });

        body.appendChild(tr);
    });

    if (rangeEl) {
        rangeEl.textContent = totalRows === 0
            ? 'Showing 0-0 of 0 rows'
            : `Showing ${startIndex + 1}-${endIndex} of ${totalRows} rows`;
    }
    if (pageInfoEl) {
        pageInfoEl.textContent = `Page ${rowExclusionCurrentPage} of ${totalPages}`;
    }
    if (prevBtn) prevBtn.disabled = (rowExclusionCurrentPage === 1);
    if (nextBtn) nextBtn.disabled = (rowExclusionCurrentPage === totalPages);

    updateRowExclusionCounter();
}

function showPreview() {
    const previewSheet = document.getElementById('previewSheet');
    const mainUploadPage = document.getElementById('mainUploadPage');

    if (!workbookData) {
        showStatus('⚠️ No file loaded to preview.', 'error');
        return;
    }

    try {
        const workbook = XLSX.read(workbookData, { type: 'array' });
        const sheetName = currentSheet || workbook.SheetNames[0];
        const sheet = workbook.Sheets[sheetName];
        // defval: '' forces every column slot to exist even when a cell is blank --
        // without it, SheetJS leaves a hole in the row array, which row.forEach() below
        // silently skips, shifting every later value one column to the left.
        const jsonData = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: '' });

        previewHeaders = jsonData[0] || [];
        const rawDataRows = jsonData.slice(1); // All rows, not limited
        // Show only rows the user hasn't excluded in the Row Selection grid, so the
        // preview reflects what will actually be imported. Same Excel row-number scheme
        // as excludedRowNumbers / routers/import_export.py's excluded_rows (header=1,
        // first data row=2). S.No is renumbered sequentially over the filtered set by
        // renderPreviewPage() (unchanged) since excluded rows are gone entirely, not
        // just hidden -- a clean list of what will be imported, not a gapped one.
        previewAllRows = rawDataRows.filter((_, idx) => !excludedRowNumbers.has(idx + 2));
        previewHiddenCount = rawDataRows.length - previewAllRows.length;
        previewCurrentPage = 1;

        // Render current page
        renderPreviewPage();

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
    populateDateOrderDropdown(selectedDateDelimiter);
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

    // Go to Data Bank shortcut (top bar)
    const goToDataBankBtn = document.getElementById('goToDataBankBtn');
    if (goToDataBankBtn) {
        goToDataBankBtn.addEventListener('click', function() {
            window.location.href = '/static/databank.html';
        });
    }

    // Pagination controls event listeners
    const previewPageSizeDropdown = document.getElementById('previewPageSize');
    if (previewPageSizeDropdown) {
        previewPageSizeDropdown.addEventListener('change', function() {
            previewPageSize = parseInt(this.value) || 50;
            previewCurrentPage = 1;
            renderPreviewPage();
        });
    }

    const previewPrevPageBtn = document.getElementById('previewPrevPage');
    if (previewPrevPageBtn) {
        previewPrevPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            if (previewCurrentPage > 1) {
                previewCurrentPage--;
                renderPreviewPage();
            }
        });
    }

    const previewNextPageBtn = document.getElementById('previewNextPage');
    if (previewNextPageBtn) {
        previewNextPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            const totalPages = Math.ceil(previewTotalRows / previewPageSize) || 1;
            if (previewCurrentPage < totalPages) {
                previewCurrentPage++;
                renderPreviewPage();
            }
        });
    }

    // Row selection grid pagination controls (Phase 2 of row-exclusion)
    const rowExclusionPrevPageBtn = document.getElementById('rowExclusionPrevPage');
    if (rowExclusionPrevPageBtn) {
        rowExclusionPrevPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            if (rowExclusionCurrentPage > 1) {
                rowExclusionCurrentPage--;
                renderRowExclusionPage();
            }
        });
    }

    const rowExclusionNextPageBtn = document.getElementById('rowExclusionNextPage');
    if (rowExclusionNextPageBtn) {
        rowExclusionNextPageBtn.addEventListener('click', function(e) {
            e.preventDefault();
            const totalPages = Math.ceil(rowExclusionAllRows.length / ROW_EXCLUSION_PAGE_SIZE) || 1;
            if (rowExclusionCurrentPage < totalPages) {
                rowExclusionCurrentPage++;
                renderRowExclusionPage();
            }
        });
    }

    // Select All / Deselect All are now a single "mother" checkbox in the grid's own
    // header row (built in renderRowExclusionPage()), not separate buttons -- no init
    // wiring needed here.
};
