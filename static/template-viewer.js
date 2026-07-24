// Template Viewer page shell — Phase 3 of 5 (per-template config + data fetch).
//
// Phase 2 only read `?template=` for a placeholder title. This phase adds a small
// template-ID -> display config lookup and wires the date-range/Fetch toolbar to the
// same backend endpoint Data Bank's own preview already uses (GET /api/data/preview),
// reusing that endpoint as-is rather than building new data logic. Table columns are
// derived at render time from the fetched row_data (same convention databank.js's own
// renderPreviewRows() already uses), not hardcoded here — that's what keeps this view
// from drifting out of sync with the real template definition. Search stays fully inert
// this phase — no listener is attached to #tvSearchInput; that is Phase 4's job.

// getSelectedClientId()/API_BASE are globals already provided by client_context.js —
// deliberately not redeclared here (redeclaring a global in a second plain <script> has
// caused real bugs elsewhere in this app, see error_dashboard.js's old STORAGE_KEY crash).

// ---------- TEMPLATE CONFIG ----------
// Template ID -> display label/icon/endpoint. Label/icon confirmed against the Client
// Data Links card strip in verification.html (the cards this viewer will eventually
// replace, Phase 5). `endpoint` is included per the brief even though every template
// currently reuses the same one — a placeholder for a future template that might need a
// different data source, without having to restructure this lookup later.
const TEMPLATE_CONFIG = {
    1: { label: 'Transaction Report', icon: '📊', endpoint: '/api/data/preview' },
    2: { label: 'Purchase Demand', icon: '📋', endpoint: '/api/data/preview' },
    3: { label: 'Purchase Order', icon: '📄', endpoint: '/api/data/preview' },
    4: { label: 'Gate Receiving', icon: '🚪', endpoint: '/api/data/preview' },
    5: { label: 'Store Receiving', icon: '📥', endpoint: '/api/data/preview' }
};

// This phase's fixed page size — the Phase 2 shell has no pagination controls yet, so a
// single page (at the endpoint's own max limit) is fetched; see setStatusMessage()'s
// truncation note when a template has more rows than this.
const TEMPLATE_VIEWER_PAGE_LIMIT = 200;

// ---------- STATE ----------
let currentTemplateId = null;
let currentTemplateConfig = null;

// ---------- DOM REFS ----------
const titleEl = document.getElementById('templateViewerTitle');
const subtitleEl = document.getElementById('templateViewerSubtitle');
const tvStartDateInput = document.getElementById('tvStartDate');
const tvEndDateInput = document.getElementById('tvEndDate');
const tvSearchInput = document.getElementById('tvSearchInput');
const tvFetchBtn = document.getElementById('tvFetchBtn');
const tvStatus = document.getElementById('tvStatus');
const tvEmptyState = document.getElementById('tvEmptyState');
const tvLoadingState = document.getElementById('tvLoadingState');
const tvDataTableWrapper = document.getElementById('tvDataTableWrapper');
const tvDataTable = document.getElementById('tvDataTable');

// ---------- HELPERS ----------

// Same "first/last day of the current month" default this app already uses in
// databank.js's setDefaultDateRange() — gives the page a sensible starting range so
// Fetch works immediately without the user having to pick dates first.
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
    tvStartDateInput.value = formatDate(firstDay);
    tvEndDateInput.value = formatDate(lastDay);
}

function setStatusMessage(msg) {
    tvStatus.textContent = msg || '';
}

// Shows exactly one of the three table-area states (empty / loading / populated table),
// mirroring this app's established single-state-visible modal/panel convention.
function setViewerState(state) {
    tvEmptyState.style.display = state === 'empty' ? 'flex' : 'none';
    tvLoadingState.style.display = state === 'loading' ? 'flex' : 'none';
    tvDataTableWrapper.style.display = state === 'data' ? 'block' : 'none';
}

// ---------- RENDER ----------
// Builds a plain header row + one row per record from row_data's own keys — no S.No
// column, document grouping, or subtotal rows (that's Data Bank preview-specific polish,
// out of scope for this viewer).
function renderTemplateData(rows) {
    const thead = tvDataTable.querySelector('thead');
    const tbody = tvDataTable.querySelector('tbody');
    thead.innerHTML = '';
    tbody.innerHTML = '';

    if (!rows || rows.length === 0) {
        setViewerState('empty');
        tvEmptyState.textContent = 'No data found for the selected date range.';
        return;
    }

    const headers = Object.keys(rows[0].row_data || {});
    const headerRow = document.createElement('tr');
    headers.forEach(h => {
        const th = document.createElement('th');
        th.textContent = h;
        headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);

    rows.forEach(row => {
        const tr = document.createElement('tr');
        headers.forEach(h => {
            const td = document.createElement('td');
            const value = row.row_data[h];
            td.textContent = (value === undefined || value === null || value === '') ? '—' : value;
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });

    setViewerState('data');
}

// ---------- FETCH ----------
async function fetchTemplateData() {
    if (!currentTemplateConfig) {
        // No valid template — the toolbar is already inert in this state (see
        // window.onload), so this should not be reachable, but guard anyway.
        return;
    }

    const clientId = window.getSelectedClientId ? window.getSelectedClientId() : null;
    if (!clientId) {
        setStatusMessage('⚠️ No client selected.');
        setViewerState('empty');
        tvEmptyState.textContent = 'No client selected — please select a client from the Dashboard.';
        return;
    }

    const start = tvStartDateInput.value;
    const end = tvEndDateInput.value;
    if (!start || !end) {
        setStatusMessage('⚠️ Please select both a start and end date.');
        return;
    }
    if (start > end) {
        setStatusMessage('⚠️ Start date must be before or equal to end date.');
        return;
    }

    setStatusMessage('');
    setViewerState('loading');

    const params = new URLSearchParams({
        client_id: clientId,
        template_id: currentTemplateId,
        date_start: start,
        date_end: end,
        page: 1,
        limit: TEMPLATE_VIEWER_PAGE_LIMIT
    });

    try {
        const res = await fetch(`${API_BASE}${currentTemplateConfig.endpoint}?${params.toString()}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch template data');
        }
        const data = await res.json();
        renderTemplateData(data.rows || []);

        // No pagination UI this phase — make a partial result visible rather than
        // silently truncating at the endpoint's own page-size cap.
        if (data.total_rows > (data.rows || []).length) {
            setStatusMessage(`Showing ${data.rows.length} of ${data.total_rows} rows — narrow the date range to see more.`);
        } else {
            setStatusMessage(`${data.total_rows} row(s).`);
        }
    } catch (error) {
        console.error('Template Viewer fetch error:', error);
        setStatusMessage('❌ Error: ' + error.message);
        setViewerState('empty');
        tvEmptyState.textContent = 'Error loading data. Please try again.';
    }
}

// ---------- EVENT LISTENERS ----------
tvFetchBtn.addEventListener('click', fetchTemplateData);
tvStartDateInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchTemplateData(); });
tvEndDateInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchTemplateData(); });
// tvSearchInput deliberately has no listener this phase — stays inert until Phase 4.

// ---------- INIT ----------
window.onload = function () {
    const params = new URLSearchParams(window.location.search);
    const templateParam = params.get('template');
    const templateId = Number(templateParam);
    const config = TEMPLATE_CONFIG[templateId];

    if (templateParam && Number.isInteger(templateId) && templateId > 0 && config) {
        currentTemplateId = templateId;
        currentTemplateConfig = config;

        titleEl.textContent = `${config.icon} Template Viewer — ${config.label}`;
        subtitleEl.textContent = `Template #${templateId} · ${config.label}`;

        tvFetchBtn.disabled = false;
        tvStartDateInput.disabled = false;
        tvEndDateInput.disabled = false;

        setDefaultDateRange();
        fetchTemplateData();
    } else {
        // Minimal guard against a missing/invalid/unsupported template id — same
        // fallback message as Phase 2, plus keeping the toolbar inert so Fetch can't be
        // clicked with nothing valid to fetch.
        currentTemplateId = null;
        currentTemplateConfig = null;

        titleEl.textContent = '📄 Template Viewer';
        subtitleEl.textContent = 'No template specified — open this page via a template link from Verification.';

        tvFetchBtn.disabled = true;
        tvStartDateInput.disabled = true;
        tvEndDateInput.disabled = true;
        setViewerState('empty');
    }
};
