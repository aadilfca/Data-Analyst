// ---------- DOM ELEMENTS ----------
const uploadArea = document.getElementById('uploadArea');
const fileInput = document.getElementById('fileInput');
const uploadBtn = document.getElementById('uploadBtn');
const fileInfo = document.getElementById('fileInfo');
const fileName = document.getElementById('fileName');
const fileSize = document.getElementById('fileSize');
const clearBtn = document.getElementById('clearBtn');
const submitBtn = document.getElementById('submitBtn');
const templateBtn = document.getElementById('templateBtn');
const uploadStatus = document.getElementById('uploadStatus');

let selectedFile = null;

// ---------- OPEN FILE SELECTOR ----------
uploadBtn.addEventListener('click', () => fileInput.click());

uploadArea.addEventListener('click', () => fileInput.click());

// ---------- DRAG AND DROP ----------
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

// ---------- FILE SELECTION ----------
fileInput.addEventListener('change', (e) => {
    if (e.target.files.length) {
        handleFile(e.target.files[0]);
    }
});

function handleFile(file) {
    // Check file type
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
    submitBtn.disabled = false;
    showStatus('', '');
}

// ---------- CLEAR FILE ----------
clearBtn.addEventListener('click', () => {
    selectedFile = null;
    fileInfo.style.display = 'none';
    uploadArea.style.display = 'flex';
    submitBtn.disabled = true;
    fileInput.value = '';
    showStatus('', '');
});

// ---------- SUBMIT UPLOAD ----------
submitBtn.addEventListener('click', async () => {
    if (!selectedFile) return;

    const formData = new FormData();
    formData.append('file', selectedFile);

    submitBtn.disabled = true;
    submitBtn.textContent = '⏳ Uploading...';
    showStatus('⏳ Uploading file to server...', 'loading');

    try {
        const res = await fetch('/api/import-excel', {
            method: 'POST',
            body: formData
        });

        const result = await res.json();

        if (result.error) {
            showStatus('❌ ' + result.error, 'error');
        } else {
            showStatus('✅ ' + result.message, 'success');
            // Clear the file after successful upload
            setTimeout(() => {
                clearBtn.click();
            }, 2000);
        }
    } catch (error) {
        showStatus('❌ Error: ' + error.message, 'error');
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Upload to Server';
    }
});

// ---------- DOWNLOAD TEMPLATE ----------
templateBtn.addEventListener('click', async () => {
    try {
        templateBtn.textContent = '⏳ Generating...';
        const res = await fetch('/api/download-template');
        const blob = await res.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'import_template.xlsx';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);
        showStatus('✅ Template downloaded successfully!', 'success');
    } catch (error) {
        showStatus('❌ Error downloading template: ' + error.message, 'error');
    } finally {
        templateBtn.textContent = '📥 Download Template';
    }
});

// ---------- SHOW STATUS ----------
function showStatus(message, type) {
    uploadStatus.textContent = message;
    uploadStatus.className = 'upload-status ' + type;
}