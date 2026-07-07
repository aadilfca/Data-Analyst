// ==============================================
// 📋 COLUMN TEMPLATE MANAGER
// ==============================================

// ---------- API CONFIGURATION ----------
const API_BASE = 'http://localhost:5000';

const templateDropdown = document.getElementById('templateDropdown');
let rowCounter = 0;

// ---------- LOAD TEMPLATES ----------
async function loadTemplates() {
    try {
        const res = await fetch(API_BASE + '/api/templates');
        if (!res.ok) throw new Error('Failed to load templates');
        const templates = await res.json();
        
        templateDropdown.innerHTML = '';
        
        // ---------- ADD PLACEHOLDER (disabled, selected by default) ----------
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = '-- Select Template --';
        placeholder.disabled = true;
        placeholder.selected = true;
        templateDropdown.appendChild(placeholder);
        
        // ---------- ADD "Add New" OPTION ----------
        const addOption = document.createElement('option');
        addOption.value = 'new';
        addOption.textContent = '➕ Add New';
        templateDropdown.appendChild(addOption);
        
        // ---------- ADD SEPARATOR ----------
        const separator = document.createElement('option');
        separator.disabled = true;
        separator.textContent = '────────────────';
        templateDropdown.appendChild(separator);
        
        // ---------- ADD EXISTING TEMPLATES ----------
        templates.forEach(template => {
            const option = document.createElement('option');
            option.value = template.id;
            option.textContent = template.template_name;
            option.dataset.columns = template.columns;
            templateDropdown.appendChild(option);
        });
        
        console.log('✅ Templates loaded:', templates.length);
        
    } catch (error) {
        console.log('⚠️ Error loading templates:', error.message);
    }
}

// ---------- HANDLE TEMPLATE SELECTION ----------
templateDropdown.addEventListener('change', function() {
    const selectedValue = this.value;
    
    if (selectedValue === 'new') {
        // User selected "Add New" → open popup
        openPopup();
        // Reset dropdown to placeholder so they can select "Add New" again later
        this.value = '';
        return;
    }
    
    if (selectedValue === '') {
        // Placeholder selected (do nothing)
        return;
    }
    
    // User selected an existing template
    const selectedOption = this.options[this.selectedIndex];
    const templateName = selectedOption.textContent;
    const columns = JSON.parse(selectedOption.dataset.columns || '[]');
    
    console.log(`📋 Selected template: ${templateName}`, columns);
});

// ==============================================
// POPUP FUNCTIONS
// ==============================================

// ---------- OPEN POPUP ----------
function openPopup() {
    document.getElementById('popupOverlay').style.display = 'block';
    document.getElementById('popupWindow').style.display = 'block';
    
    document.getElementById('templateName').value = '';
    document.getElementById('columnsBody').innerHTML = '';
    document.getElementById('statusMessage').style.display = 'none';
    rowCounter = 0;
    
    addColumnRow();
    addColumnRow();
}

// ---------- CLOSE POPUP ----------
function closePopup() {
    document.getElementById('popupOverlay').style.display = 'none';
    document.getElementById('popupWindow').style.display = 'none';
}

// ---------- ADD COLUMN ROW ----------
function addColumnRow() {
    rowCounter++;
    const tbody = document.getElementById('columnsBody');
    const tr = document.createElement('tr');
    tr.id = 'row_' + rowCounter;

    // Sr No
    const tdSr = document.createElement('td');
    tdSr.textContent = rowCounter;
    tdSr.style.textAlign = 'center';

    // Column Name
    const tdName = document.createElement('td');
    const inputName = document.createElement('input');
    inputName.type = 'text';
    inputName.placeholder = 'Enter column name';
    inputName.className = 'column-name-input';
    inputName.id = 'colName_' + rowCounter;
    tdName.appendChild(inputName);

    // Type
    const tdType = document.createElement('td');
    const selectType = document.createElement('select');
    selectType.className = 'type-select';
    selectType.id = 'colType_' + rowCounter;
    
    const optText = document.createElement('option');
    optText.value = 'Text';
    optText.textContent = 'Text';
    selectType.appendChild(optText);
    
    const optNumeric = document.createElement('option');
    optNumeric.value = 'Numeric';
    optNumeric.textContent = 'Numeric';
    selectType.appendChild(optNumeric);
    
    const optDate = document.createElement('option');
    optDate.value = 'Date';
    optDate.textContent = 'Date [DD/MM/YYYY]';  // ← UPDATED HERE
    selectType.appendChild(optDate);
    
    tdType.appendChild(selectType);

    // Action (Delete button)
    const tdAction = document.createElement('td');
    tdAction.style.textAlign = 'center';
    const delBtn = document.createElement('button');
    delBtn.textContent = '✕';
    delBtn.className = 'delete-btn';
    delBtn.onclick = function() {
        if (document.querySelectorAll('#columnsBody tr').length > 1) {
            tr.remove();
            renumberRows();
        } else {
            alert('You must have at least one column.');
        }
    };
    tdAction.appendChild(delBtn);

    tr.appendChild(tdSr);
    tr.appendChild(tdName);
    tr.appendChild(tdType);
    tr.appendChild(tdAction);
    tbody.appendChild(tr);
}

// ---------- RENUMBER ROWS ----------
function renumberRows() {
    const rows = document.querySelectorAll('#columnsBody tr');
    let i = 1;
    rows.forEach(row => {
        row.cells[0].textContent = i;
        i++;
    });
    rowCounter = rows.length;
}

// ---------- SAVE TEMPLATE ----------
function saveTemplate() {
    const templateName = document.getElementById('templateName').value.trim();
    const rows = document.querySelectorAll('#columnsBody tr');
    
    const columns = [];
    let allValid = true;
    
    rows.forEach(row => {
        const nameInput = row.querySelector('input[type="text"]');
        const typeSelect = row.querySelector('select');
        if (nameInput && typeSelect) {
            const colName = nameInput.value.trim();
            const colType = typeSelect.value;
            if (!colName) {
                allValid = false;
                nameInput.style.border = '1px solid red';
            } else {
                nameInput.style.border = '1px solid #ccc';
                columns.push({ name: colName, type: colType });
            }
        }
    });

    if (!templateName) {
        showStatus('⚠️ Please enter a Template Name.', 'error');
        return;
    }

    if (!allValid || columns.length === 0) {
        showStatus('⚠️ Please enter valid Column Names for all rows.', 'error');
        return;
    }

    showStatus('✅ Template "' + templateName + '" created with ' + columns.length + ' columns!', 'success');
    console.log('TEMPLATE DATA:', {
        templateName: templateName,
        columns: columns
    });
    
    setTimeout(() => {
        closePopup();
    }, 2000);
}

// ---------- SHOW STATUS MESSAGE ----------
function showStatus(message, type) {
    const statusDiv = document.getElementById('statusMessage');
    statusDiv.textContent = message;
    statusDiv.style.display = 'block';
    statusDiv.style.background = type === 'error' ? '#ffebee' : '#e8f5e9';
    statusDiv.style.color = type === 'error' ? '#c62828' : '#2e7d32';
    statusDiv.style.border = '1px solid ' + (type === 'error' ? '#ef9a9a' : '#a5d6a7');
    statusDiv.style.padding = '10px';
    statusDiv.style.borderRadius = '4px';
}

// ---------- KEYBOARD SHORTCUT ----------
document.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && document.activeElement.id && document.activeElement.id.startsWith('colName_')) {
        addColumnRow();
        setTimeout(() => {
            const rows = document.querySelectorAll('#columnsBody tr');
            const lastRow = rows[rows.length - 1];
            if (lastRow) {
                const input = lastRow.querySelector('input[type="text"]');
                if (input) input.focus();
            }
        }, 50);
    }
});

// ---------- CLOSE POPUP ON OVERLAY CLICK ----------
document.addEventListener('DOMContentLoaded', function() {
    const overlay = document.getElementById('popupOverlay');
    if (overlay) {
        overlay.addEventListener('click', closePopup);
    }
});
// ---------- IMPORT FROM EXCEL ----------
function importFromExcel(event) {
    const file = event.target.files[0];
    if (!file) return;

    // Validate file type
    const validTypes = ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.ms-excel'];
    if (!validTypes.includes(file.type) && !file.name.endsWith('.xlsx') && !file.name.endsWith('.xls')) {
        alert('⚠️ Please select a valid Excel file (.xlsx or .xls)');
        event.target.value = '';
        return;
    }

    const reader = new FileReader();
    reader.onload = function(e) {
        try {
            const data = new Uint8Array(e.target.result);
            const workbook = XLSX.read(data, { type: 'array' });
            const firstSheet = workbook.Sheets[workbook.SheetNames[0]];
            const jsonData = XLSX.utils.sheet_to_json(firstSheet, { header: 1 });

            if (!jsonData || jsonData.length === 0) {
                alert('⚠️ The Excel file is empty.');
                return;
            }

            // Get first column from each row
            let importedCount = 0;
            jsonData.forEach(row => {
                if (row.length > 0 && row[0]) {
                    const colName = String(row[0]).trim();
                    if (colName) {
                        // Add column with default type "Text"
                        addColumnRowWithName(colName, 'Text');
                        importedCount++;
                    }
                }
            });

            if (importedCount === 0) {
                alert('⚠️ No column names found in the first column.');
            } else {
                showStatus('✅ Imported ' + importedCount + ' columns from Excel!', 'success');
                setTimeout(() => {
                    document.getElementById('statusMessage').style.display = 'none';
                }, 3000);
            }

        } catch (error) {
            alert('❌ Error reading Excel file: ' + error.message);
        }
    };
    reader.readAsArrayBuffer(file);

    // Reset file input
    event.target.value = '';
}

// ---------- ADD COLUMN ROW WITH SPECIFIC NAME AND TYPE ----------
function addColumnRowWithName(columnName, columnType) {
    rowCounter++;
    const tbody = document.getElementById('columnsBody');
    const tr = document.createElement('tr');
    tr.id = 'row_' + rowCounter;

    // Sr No
    const tdSr = document.createElement('td');
    tdSr.textContent = rowCounter;
    tdSr.style.textAlign = 'center';

    // Column Name (pre-filled)
    const tdName = document.createElement('td');
    const inputName = document.createElement('input');
    inputName.type = 'text';
    inputName.placeholder = 'Enter column name';
    inputName.className = 'column-name-input';
    inputName.id = 'colName_' + rowCounter;
    inputName.value = columnName;  // Pre-fill with imported name
    tdName.appendChild(inputName);

    // Type (pre-selected)
    const tdType = document.createElement('td');
    const selectType = document.createElement('select');
    selectType.className = 'type-select';
    selectType.id = 'colType_' + rowCounter;
    
    const optText = document.createElement('option');
    optText.value = 'Text';
    optText.textContent = 'Text';
    selectType.appendChild(optText);
    
    const optNumeric = document.createElement('option');
    optNumeric.value = 'Numeric';
    optNumeric.textContent = 'Numeric';
    selectType.appendChild(optNumeric);
    
    const optDate = document.createElement('option');
    optDate.value = 'Date';
    optDate.textContent = 'Date [DD/MM/YYYY]';
    selectType.appendChild(optDate);
    
    // Set the selected type
    if (columnType === 'Numeric') {
        selectType.value = 'Numeric';
    } else if (columnType === 'Date') {
        selectType.value = 'Date';
    } else {
        selectType.value = 'Text';
    }
    
    tdType.appendChild(selectType);

    // Action (Delete button)
    const tdAction = document.createElement('td');
    tdAction.style.textAlign = 'center';
    const delBtn = document.createElement('button');
    delBtn.textContent = '✕';
    delBtn.className = 'delete-btn';
    delBtn.onclick = function() {
        if (document.querySelectorAll('#columnsBody tr').length > 1) {
            tr.remove();
            renumberRows();
        } else {
            alert('You must have at least one column.');
        }
    };
    tdAction.appendChild(delBtn);

    tr.appendChild(tdSr);
    tr.appendChild(tdName);
    tr.appendChild(tdType);
    tr.appendChild(tdAction);
    tbody.appendChild(tr);
}
// ---------- LOAD ON PAGE OPEN ----------
window.onload = function() {
    loadTemplates();
    console.log('📋 Template Manager loaded');
};