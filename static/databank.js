// ==============================================
// 📊 CLIENT DATA BANK - FINAL POLISHED VERSION
// ==============================================

// API_BASE and STORAGE_KEY are defined in client_context.js

// ---------- STATE ----------
let availabilityData = [];
let selectedTemplateId = null;
let dateStart = null;
let dateEnd = null;
let currentPage = 1;
let totalPages = 1;
let previewLimit = 50;
let previewTotalRows = 0;
let previewSundayCount = 0;
let previewTemplateId = null;
let previewTemplateName = '';
let isPreviewOpen = false;
let isPreviewLoading = false;
let isAvailabilityLoading = false;

// Coverage Calendar (Phase 4) state — independent of dateStart/dateEnd/
// filterTemplate above; this widget has its own template multi-select and
// its own month/year navigation.
let calendarYear = new Date().getFullYear();
let calendarMonth = new Date().getMonth() + 1; // 1-12

// Template picker (Redesign Phase 2 — chips + search) state. Chips (unlike
// <option> elements) don't carry selection in the DOM themselves, so the
// selection lives here explicitly. calendarTemplatesList is the full
// fetched set (for the search filter to work against); calendarSelectedTemplateIds
// is the actual selection, unaffected by what the search box currently hides.
let calendarTemplatesList = [];
let calendarSelectedTemplateIds = new Set();

// ---------- DOM REFS ----------
const filterTemplate = document.getElementById('templateFilter');
const filterDateStart = document.getElementById('dateStart');
const filterDateEnd = document.getElementById('dateEnd');
const checkBtn = document.getElementById('checkAvailabilityBtn');
const clearFiltersBtn = document.getElementById('clearFiltersBtn');
const refreshBtn = document.getElementById('refreshBtn');
const availabilityBody = document.getElementById('availabilityBody');
const noDataMessage = document.getElementById('noDataMessage');
const statusDiv = document.getElementById('statusMessage');

// Redesign Phase 1: header's Upload More button (moved off an inline onclick).
const uploadMoreBtn = document.getElementById('uploadMoreBtn');

// Coverage Calendar (Phase 4) DOM refs
// Redesign Phase 2: the old <select multiple> is replaced by a search input
// + a chip container (calendarTemplateSelect no longer exists in the DOM).
const calendarTemplateSearchInput = document.getElementById('calendarTemplateSearch');
const calendarTemplateChipsContainer = document.getElementById('calendarTemplateChips');
const calendarPrevMonthBtn = document.getElementById('calendarPrevMonthBtn');
const calendarNextMonthBtn = document.getElementById('calendarNextMonthBtn');
const calendarMonthLabel = document.getElementById('calendarMonthLabel');
const coverageCalendarsContainer = document.getElementById('coverageCalendarsContainer');

// Missing Dates Report (Phase 5) DOM ref
const missingDatesReportBody = document.getElementById('missingDatesReportBody');

const statsTotalRecords = document.getElementById('totalRecords');
const statsTotalTemplates = document.getElementById('totalTemplates');
const statsMissingTemplates = document.getElementById('missingTemplates');
const statsSundayRows = document.getElementById('totalSundayRows');

// Preview modal elements
const previewModal = document.getElementById('previewModal');
const previewTitle = document.getElementById('previewTitle');
const previewTableHead = document.getElementById('previewTableHead');
const previewTableBody = document.getElementById('previewTableBody');
const pageSizeSelect = document.getElementById('pageSizeSelect');
const sundayOnlyCheckbox = document.getElementById('sundayOnlyCheckbox');
const dataQualityFilter = document.getElementById('dataQualityFilter');
const sundayCountBadge = document.getElementById('sundayCountBadge');

// Shared date range popover elements (Phase 2 — Unified date range). One
// popover, two entry points: the row 👁️ icon (gate mode, before the preview
// opens) and this modal's own trigger button (live mode, while it's open).
const modalDateRangeBtn = document.getElementById('modalDateRangeBtn');
const sharedRangePopover = document.getElementById('sharedRangePopover');
const sharedRangeFromInput = document.getElementById('sharedRangeFrom');
const sharedRangeToInput = document.getElementById('sharedRangeTo');
const sharedRangeConfirmBtn = document.getElementById('sharedRangeConfirmBtn');
// Set only while the popover is in "gate" mode (opened from a row's preview
// icon, before the modal exists) — holds which template to open once the
// range is confirmed. Null in "live" mode (popover opened from inside an
// already-open modal), where confirming just refreshes in place.
let pendingPreviewOpen = null;
const searchInput = document.getElementById('searchInput');
const amountMin = document.getElementById('amountMin');
const amountMax = document.getElementById('amountMax');
const applyFiltersBtn = document.getElementById('applyFiltersBtn');
const clearPreviewFiltersBtn = document.getElementById('clearFiltersBtn');
const prevPageBtn = document.getElementById('prevPageBtn');
const nextPageBtn = document.getElementById('nextPageBtn');
const pageInfo = document.getElementById('pageInfo');
const previewCountInfo = document.getElementById('previewCountInfo');
const closePreviewBtn = document.getElementById('closePreviewBtn');

// Row revision history popover elements (Phase 3)
const rowHistoryPopover = document.getElementById('rowHistoryPopover');
const rowHistoryBody = document.getElementById('rowHistoryBody');
// Tracks which history icon (if any) currently has its popover open, so the
// click handler can toggle it closed and the outside-click handler knows
// what counts as "inside".
let activeHistoryIcon = null;

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

function getSelectedClientName() {
    return localStorage.getItem(STORAGE_NAME) || 'Unknown Client';
}

function formatDateForDisplay(dateStr) {
    if (!dateStr) return '—';
    try {
        const d = new Date(dateStr);
        return d.toLocaleDateString('en-GB', { day: '2-digit', month: '2-digit', year: 'numeric' });
    } catch {
        return dateStr;
    }
}

function getDateRangeFromFilter() {
    const start = filterDateStart.value;
    const end = filterDateEnd.value;
    if (start && end) {
        if (start <= end) {
            return { start, end };
        } else {
            showStatus('⚠️ Start date must be before or equal to end date.', 'error');
            return null;
        }
    }
    return null;
}

function isSunday(dateStr) {
    if (!dateStr) return false;
    try {
        const d = new Date(dateStr);
        return d.getDay() === 0;
    } catch {
        return false;
    }
}

function formatCellValue(value, columnName = '') {
    if (value === undefined || value === null || value === '') return '—';
    if (typeof value === 'number') {
        if (window.formatAmount) {
            return window.formatAmount(value);
        }
        return value.toString();
    }
    if (typeof value === 'string' && value.length > 100) {
        return value.substring(0, 100) + '…';
    }
    return value;
}

function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value === undefined || value === null ? '' : String(value);
    return div.innerHTML;
}

// ---------- ROW REVISION HISTORY POPOVER (Phase 3) ----------
// Click-triggered popover on the S.No column's history icon. Fetches from
// the Phase 1 endpoint and renders generically off the "changes" array,
// so it works for any template without hardcoding field names.
function hideRowHistoryPopover() {
    rowHistoryPopover.style.display = 'none';
    activeHistoryIcon = null;
}

function renderRowHistoryPopover(data) {
    if (!data.revisions || data.revisions.length === 0) {
        rowHistoryBody.innerHTML = '<div style="color:#94a3b8;">No revision history.</div>';
        return;
    }
    rowHistoryBody.innerHTML = data.revisions.map(rev => {
        const changeRows = (rev.changes || []).map(c => `
            <tr>
                <td class="field-name">${escapeHtml(c.field)}</td>
                <td class="value-change">
                    <span class="old-value">${escapeHtml(formatCellValue(c.old_value))}</span>
                    &rarr;
                    <span class="new-value">${escapeHtml(formatCellValue(c.new_value))}</span>
                </td>
            </tr>`).join('');
        return `
            <div class="revision-block">
                <div class="revision-meta">${escapeHtml(rev.action)} &middot; ${escapeHtml(rev.changed_at)} &middot; ${escapeHtml(rev.user)}</div>
                <table>${changeRows}</table>
            </div>`;
    }).join('');
}

async function showRowHistoryPopover(icon, clientDataId) {
    // Position near the icon, clamped so it stays within the viewport.
    const rect = icon.getBoundingClientRect();
    const popoverWidth = 340;
    let left = rect.left;
    if (left + popoverWidth > window.innerWidth) {
        left = Math.max(8, window.innerWidth - popoverWidth - 8);
    }
    rowHistoryPopover.style.left = left + 'px';

    // Flip above the icon when there isn't enough room below (matches the
    // popover's CSS max-height of 320px), so it's never cut off at the
    // bottom of the viewport. Anchoring with "bottom" when flipped lets it
    // grow upward without needing to know its rendered height in advance.
    const popoverMaxHeight = 320;
    const spaceBelow = window.innerHeight - rect.bottom;
    if (spaceBelow >= popoverMaxHeight + 6) {
        rowHistoryPopover.style.top = (rect.bottom + 6) + 'px';
        rowHistoryPopover.style.bottom = 'auto';
    } else {
        rowHistoryPopover.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
        rowHistoryPopover.style.top = 'auto';
    }

    rowHistoryBody.innerHTML = '<div style="color:#94a3b8;">Loading...</div>';
    rowHistoryPopover.style.display = 'block';
    activeHistoryIcon = icon;

    try {
        const res = await fetch(`${API_BASE}/api/data/rows/${clientDataId}/history`);
        if (!res.ok) throw new Error('Failed to fetch history');
        const data = await res.json();
        renderRowHistoryPopover(data);
    } catch (error) {
        rowHistoryBody.innerHTML = '<div style="color:#b91c1c;">Failed to load history.</div>';
    }
}

// ---------- SHARED DATE RANGE POPOVER (Phase 2 — Unified date range) ----------
// One popover backs both entry points; #dateStart/#dateEnd (the main sheet's
// own inputs) remain the single source of truth for the range — this popover
// only ever reads/writes those two fields, never a duplicated copy.

// Positions the popover near an anchor element, clamped to the viewport and
// flipped above the anchor when there isn't enough room below — same
// approach as showRowHistoryPopover()'s positioning, written separately
// here since that function is unrelated/already-working code.
function positionSharedRangePopover(anchorEl) {
    const rect = anchorEl.getBoundingClientRect();
    const popoverWidth = 220;
    let left = rect.left;
    if (left + popoverWidth > window.innerWidth) {
        left = Math.max(8, window.innerWidth - popoverWidth - 8);
    }
    sharedRangePopover.style.left = left + 'px';

    const popoverEstimatedHeight = 140;
    const spaceBelow = window.innerHeight - rect.bottom;
    if (spaceBelow >= popoverEstimatedHeight + 6) {
        sharedRangePopover.style.top = (rect.bottom + 6) + 'px';
        sharedRangePopover.style.bottom = 'auto';
    } else {
        sharedRangePopover.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
        sharedRangePopover.style.top = 'auto';
    }
}

// mode: 'gate' (opened from a row's 👁️ icon, before the preview modal
// exists — confirming will open it) or 'live' (opened from inside the
// already-open modal — confirming just refreshes in place).
function openSharedRangePopover(anchorEl, mode, templateId, templateName) {
    sharedRangeFromInput.value = filterDateStart.value || '';
    sharedRangeToInput.value = filterDateEnd.value || '';
    pendingPreviewOpen = (mode === 'gate') ? { templateId, templateName } : null;
    sharedRangeConfirmBtn.textContent = (mode === 'gate') ? 'Open Preview' : 'Apply';
    positionSharedRangePopover(anchorEl);
    sharedRangePopover.style.display = 'block';
}

function closeSharedRangePopover() {
    sharedRangePopover.style.display = 'none';
    pendingPreviewOpen = null;
}

// Validates and writes the popover's From/To into the shared #dateStart/
// #dateEnd inputs, then propagates the change to whichever views need it.
// fetchAvailability() (awaited) is the single point that also refreshes an
// already-open preview (see its own trailing isPreviewOpen check) — awaiting
// it here before opening a pending (gate-mode) preview means that check runs
// while isPreviewOpen is still false, so openPreview()'s own fetch is the
// only preview fetch that happens, avoiding a redundant duplicate call.
async function confirmSharedRangePopover() {
    const start = sharedRangeFromInput.value;
    const end = sharedRangeToInput.value;
    if (!start || !end) {
        showStatus('⚠️ Please select both a start and end date.', 'error');
        return;
    }
    if (start > end) {
        showStatus('⚠️ Start date must be before or equal to end date.', 'error');
        return;
    }

    filterDateStart.value = start;
    filterDateEnd.value = end;

    const gateTarget = pendingPreviewOpen;
    closeSharedRangePopover();

    if (getSelectedClientId()) {
        await fetchAvailability();
    }

    if (gateTarget) {
        openPreview(gateTarget.templateId, gateTarget.templateName);
    }
}

// ---------- LOAD TEMPLATES INTO DROPDOWN ----------
async function loadTemplateDropdown() {
    try {
        const res = await fetch(API_BASE + '/api/templates');
        if (!res.ok) throw new Error('Failed to fetch templates');
        const templates = await res.json();
        
        filterTemplate.innerHTML = '<option value="">All Templates</option>';
        templates.forEach(tpl => {
            const opt = document.createElement('option');
            opt.value = tpl.id;
            opt.textContent = tpl.template_name;
            filterTemplate.appendChild(opt);
        });
    } catch (error) {
        console.error('Error loading templates:', error);
        filterTemplate.innerHTML = '<option value="">⚠️ Failed to load</option>';
    }
}

// ---------- COVERAGE CALENDAR (Phase 4) ----------
// Independent widget — its own template multi-select and its own month/year
// state (calendarYear/calendarMonth above). Does not read or write
// #dateStart/#dateEnd/#templateFilter, which stay dedicated to the existing
// Availability Table/Preview/Export flow below, left untouched.

const CALENDAR_MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'];
// Redesign Phase 3: full weekday names (was 3-letter abbreviations), per the
// prototype — buildCalendarDayGrid() just iterates this array, so it needed
// no change itself to pick up the longer labels.
const CALENDAR_DAY_HEADERS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

// Populated via its own GET /api/templates call (deliberately not sharing
// loadTemplateDropdown()'s fetch/element, to avoid touching that existing,
// working code — same small-duplication-over-refactor-risk tradeoff already
// used elsewhere in this codebase, e.g. the Audit Rules Cross-checks/
// Categories views).
// Redesign Phase 2: renamed from loadCalendarTemplateOptions() (no <option>
// elements anymore) — stores the fetched list, then renders it as chips.
async function loadCalendarTemplates() {
    try {
        const res = await fetch(API_BASE + '/api/templates');
        if (!res.ok) throw new Error('Failed to fetch templates');
        calendarTemplatesList = await res.json();
        renderCalendarTemplateChips('');
    } catch (error) {
        console.error('Error loading calendar templates:', error);
        calendarTemplatesList = [];
        calendarTemplateChipsContainer.innerHTML = '<div class="calendar-chip-list-empty">⚠️ Failed to load templates</div>';
    }
}

// Rebuilds the chip list from calendarTemplatesList, filtered by a
// case-insensitive substring match on filterText — selection itself
// (calendarSelectedTemplateIds) is untouched by filtering, only which chips
// are currently visible. Built with document.createElement, matching this
// section's existing buildCalendarDayGrid()/buildCalendarLegend() convention.
function renderCalendarTemplateChips(filterText) {
    calendarTemplateChipsContainer.innerHTML = '';

    const needle = (filterText || '').trim().toLowerCase();
    const visibleTemplates = needle
        ? calendarTemplatesList.filter(tpl => tpl.template_name.toLowerCase().includes(needle))
        : calendarTemplatesList;

    if (visibleTemplates.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'calendar-chip-list-empty';
        empty.textContent = calendarTemplatesList.length === 0
            ? 'No templates available.'
            : 'No templates match your search.';
        calendarTemplateChipsContainer.appendChild(empty);
        return;
    }

    visibleTemplates.forEach(tpl => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'calendar-template-chip';
        chip.textContent = tpl.template_name;
        chip.dataset.templateId = tpl.id;
        if (calendarSelectedTemplateIds.has(tpl.id)) {
            chip.classList.add('selected');
        }
        // Toggle selection, reflect it immediately on this chip (no full
        // re-render needed), then feed the existing, unchanged calendar fetch —
        // same effect the old <select>'s "change" listener had.
        chip.addEventListener('click', () => {
            if (calendarSelectedTemplateIds.has(tpl.id)) {
                calendarSelectedTemplateIds.delete(tpl.id);
                chip.classList.remove('selected');
            } else {
                calendarSelectedTemplateIds.add(tpl.id);
                chip.classList.add('selected');
            }
            fetchCoverageCalendar();
        });
        calendarTemplateChipsContainer.appendChild(chip);
    });
}

// Same name/return shape as before this phase (array of numeric template
// ids) — fetchCoverageCalendar() and everything downstream of it needed no
// changes as a result.
function getCalendarSelectedTemplateIds() {
    return Array.from(calendarSelectedTemplateIds);
}

function updateCalendarMonthLabel() {
    calendarMonthLabel.textContent = `${CALENDAR_MONTH_NAMES[calendarMonth - 1]} ${calendarYear}`;
}

// Fetches Phase 3's per-day coverage endpoint for every currently-selected
// template in one call (template_ids sent as repeated query params) and
// renders one calendar block per template.
async function fetchCoverageCalendar() {
    updateCalendarMonthLabel();

    const clientId = getSelectedClientId();
    if (!clientId) {
        coverageCalendarsContainer.innerHTML = '<div class="calendar-empty-state">⚠️ No client selected. Please select a client from the Dashboard.</div>';
        return;
    }

    const templateIds = getCalendarSelectedTemplateIds();
    if (templateIds.length === 0) {
        coverageCalendarsContainer.innerHTML = '<div class="calendar-empty-state">Select one or more templates above to view their coverage calendar.</div>';
        return;
    }

    const params = new URLSearchParams({
        client_id: clientId,
        year: calendarYear,
        month: calendarMonth
    });
    templateIds.forEach(id => params.append('template_ids', id));

    coverageCalendarsContainer.innerHTML = '<div class="calendar-empty-state">⏳ Loading calendar...</div>';

    try {
        const res = await fetch(`${API_BASE}/api/data/availability/calendar?${params.toString()}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch coverage calendar');
        }
        const data = await res.json();
        renderCoverageCalendars(data);
    } catch (error) {
        console.error('Coverage calendar fetch error:', error);
        coverageCalendarsContainer.innerHTML = `<div class="calendar-empty-state">❌ Error loading calendar: ${escapeHtml(error.message)}</div>`;
    }
}

// Builds the 7-column day grid for one template's "days" array, with leading/
// trailing blank cells so the grid stays calendar-shaped. 0=Sunday matches
// both JS's Date.getDay() and the backend's own strftime('%w') convention.
function buildCalendarDayGrid(days, year, month) {
    const grid = document.createElement('div');
    grid.className = 'coverage-calendar-grid';

    CALENDAR_DAY_HEADERS.forEach(label => {
        const th = document.createElement('div');
        th.className = 'calendar-day-header';
        th.textContent = label;
        grid.appendChild(th);
    });

    const leadingBlanks = new Date(year, month - 1, 1).getDay();
    for (let i = 0; i < leadingBlanks; i++) {
        const blank = document.createElement('div');
        blank.className = 'calendar-day-cell empty';
        grid.appendChild(blank);
    }

    days.forEach(day => {
        const cell = document.createElement('div');
        cell.className = `calendar-day-cell ${day.status}`;
        cell.textContent = String(parseInt(day.date.slice(-2), 10));
        if (day.status === 'holiday' && day.label) {
            cell.title = day.label;
        }
        grid.appendChild(cell);
    });

    const totalCells = leadingBlanks + days.length;
    const trailingBlanks = (7 - (totalCells % 7)) % 7;
    for (let i = 0; i < trailingBlanks; i++) {
        const blank = document.createElement('div');
        blank.className = 'calendar-day-cell empty';
        grid.appendChild(blank);
    }

    return grid;
}

function buildCalendarLegend() {
    const legend = document.createElement('div');
    legend.className = 'calendar-legend';
    const items = [
        { cls: 'present', label: 'Present' },
        { cls: 'missing', label: 'Missing' },
        { cls: 'sunday', label: 'Sunday' },
        { cls: 'holiday', label: 'Holiday' }
    ];
    items.forEach(item => {
        const el = document.createElement('div');
        el.className = 'calendar-legend-item';
        const swatch = document.createElement('span');
        swatch.className = `calendar-legend-swatch ${item.cls}`;
        el.appendChild(swatch);
        el.appendChild(document.createTextNode(item.label));
        legend.appendChild(el);
    });
    return legend;
}

function renderCoverageCalendars(data) {
    coverageCalendarsContainer.innerHTML = '';
    if (!data.templates || data.templates.length === 0) {
        coverageCalendarsContainer.innerHTML = '<div class="calendar-empty-state">No templates found.</div>';
        renderMissingDatesReport(data);
        return;
    }

    data.templates.forEach(tpl => {
        const block = document.createElement('div');
        block.className = 'coverage-calendar-block';

        const heading = document.createElement('h3');
        heading.className = 'coverage-calendar-heading';
        heading.textContent = tpl.template_name;
        block.appendChild(heading);

        block.appendChild(buildCalendarLegend());
        block.appendChild(buildCalendarDayGrid(tpl.days, data.year, data.month));

        coverageCalendarsContainer.appendChild(block);
    });

    renderMissingDatesReport(data);
}

// ---------- MISSING DATES REPORT (Phase 5) ----------
// Grouped-by-template, plain-language summary of missing days — reuses the
// exact same "data" object renderCoverageCalendars() already received from
// fetchCoverageCalendar(), so no separate network call is made. Sundays and
// holidays are already excluded from "missing" by the Phase 3 endpoint, so
// every date rendered here is a genuine gap.

// "2026-07-12" -> "12 Jul". Reuses CALENDAR_MONTH_NAMES (already defined
// above for the calendar heading) sliced to 3 letters, rather than a second
// month-name list.
function formatShortDate(isoDateStr) {
    const parts = isoDateStr.split('-'); // [year, month, day]
    const day = parseInt(parts[2], 10);
    const monthName = CALENDAR_MONTH_NAMES[parseInt(parts[1], 10) - 1].slice(0, 3);
    return `${day} ${monthName}`;
}

function renderMissingDatesReport(data) {
    missingDatesReportBody.innerHTML = '';

    if (!data.templates || data.templates.length === 0) {
        missingDatesReportBody.innerHTML = '<div class="calendar-empty-state">Select one or more templates above to view their missing dates report.</div>';
        return;
    }

    data.templates.forEach(tpl => {
        const missingDays = tpl.days.filter(d => d.status === 'missing');
        const row = document.createElement('div');
        row.className = 'missing-dates-template-row';

        const nameSpan = document.createElement('span');
        nameSpan.className = 'missing-dates-template-name';
        nameSpan.textContent = tpl.template_name;
        row.appendChild(nameSpan);

        if (missingDays.length === 0) {
            const okSpan = document.createElement('span');
            okSpan.className = 'missing-dates-complete';
            okSpan.textContent = ': ✅ No missing days this month';
            row.appendChild(okSpan);
        } else {
            const dayWord = missingDays.length === 1 ? 'day' : 'days';
            const countSpan = document.createElement('span');
            countSpan.className = 'missing-dates-count';
            countSpan.textContent = `: missing ${missingDays.length} ${dayWord} — `;
            row.appendChild(countSpan);

            const listSpan = document.createElement('span');
            listSpan.className = 'missing-dates-list';
            listSpan.textContent = missingDays.map(d => formatShortDate(d.date)).join(', ');
            row.appendChild(listSpan);
        }

        missingDatesReportBody.appendChild(row);
    });
}

function goToPrevMonth() {
    calendarMonth--;
    if (calendarMonth < 1) {
        calendarMonth = 12;
        calendarYear--;
    }
    fetchCoverageCalendar();
}

function goToNextMonth() {
    calendarMonth++;
    if (calendarMonth > 12) {
        calendarMonth = 1;
        calendarYear++;
    }
    fetchCoverageCalendar();
}

// ---------- SET DEFAULT DATE RANGE (Current Month) ----------
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
    
    filterDateStart.value = formatDate(firstDay);
    filterDateEnd.value = formatDate(lastDay);
}

// ---------- CLEAR ALL FILTERS (Main Page) ----------
function clearAllFilters() {
    // 1. Reset filter fields to empty
    filterTemplate.value = '';
    filterDateStart.value = '';
    filterDateEnd.value = '';
    
    // 2. Clear the table and stats
    availabilityData = [];
    availabilityBody.innerHTML = '';
    noDataMessage.style.display = 'block';
    noDataMessage.innerHTML = 'Filters cleared. Select a new date range and click "Check Availability".';
    
    statsTotalRecords.textContent = '0';
    statsTotalTemplates.textContent = '0';
    statsMissingTemplates.textContent = '0';
    statsSundayRows.textContent = '0';
    
    // 3. Show status message
    showStatus('🧹 Filters cleared. Please select a new date range and click "Check Availability".', 'info');
}

// ---------- FETCH AVAILABILITY DATA ----------
async function fetchAvailability() {
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ Please select a client from the Dashboard first.', 'error');
        availabilityBody.innerHTML = '';
        noDataMessage.style.display = 'block';
        noDataMessage.innerHTML = '⚠️ No client selected. Please go to the Dashboard and select a client.';
        return;
    }
    
    const dateRange = getDateRangeFromFilter();
    if (!dateRange) {
        showStatus('⚠️ Please select a valid date range (start ≤ end).', 'error');
        return;
    }
    
    const templateId = filterTemplate.value || null;
    const params = new URLSearchParams({
        client_id: clientId,
        date_start: dateRange.start,
        date_end: dateRange.end
    });
    if (templateId) {
        params.append('template_id', templateId);
    }
    
    // Disable button and show loading
    isAvailabilityLoading = true;
    checkBtn.disabled = true;
    checkBtn.textContent = '⏳ Loading...';
    refreshBtn.disabled = true;
    showStatus('⏳ Fetching availability data...', 'loading');
    
    try {
        const res = await fetch(`${API_BASE}/api/data/availability?${params.toString()}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch availability');
        }
        const data = await res.json();
        availabilityData = data;
        
                // Update stats - use total_row_count (unfiltered) for Total Records
        const totalRows = data.reduce((sum, item) => sum + (item.total_row_count || 0), 0);
        const totalTemplates = data.length;
        const missing = data.filter(item => item.status === 'missing').length;
        const sundayRows = data.reduce((sum, item) => sum + (item.filtered_sunday_count || 0), 0);
        
        statsTotalRecords.textContent = totalRows;
        statsTotalTemplates.textContent = totalTemplates;
        statsMissingTemplates.textContent = missing;
        statsSundayRows.textContent = sundayRows;
        
        renderAvailabilityTable(data);

        if (totalTemplates === 0) {
            showStatus('ℹ️ No templates found for the selected criteria.', 'info');
        } else {
            showStatus(`✅ Found ${totalTemplates} template(s) with ${totalRows} total rows.`, 'success');
        }

        // Phase 2 (Unified date range): the shared range may have just changed
        // (this is the main-sheet side of the two-way sync) — keep an open
        // preview in step with it immediately, rather than only on next open.
        if (isPreviewOpen) {
            currentPage = 1;
            fetchPreviewData();
        }

    } catch (error) {
        console.error('Fetch availability error:', error);
        showStatus('❌ Error: ' + error.message, 'error');
        availabilityBody.innerHTML = '';
        noDataMessage.style.display = 'block';
        noDataMessage.innerHTML = '❌ Error loading data. Please try again.';
        statsTotalRecords.textContent = '0';
        statsTotalTemplates.textContent = '0';
        statsMissingTemplates.textContent = '0';
        statsSundayRows.textContent = '0';
    } finally {
        isAvailabilityLoading = false;
        checkBtn.disabled = false;
        checkBtn.textContent = '🔍 Check Availability';
        refreshBtn.disabled = false;
    }
}

// ---------- RENDER AVAILABILITY TABLE ----------
function renderAvailabilityTable(data) {
    availabilityBody.innerHTML = '';
    if (!data || data.length === 0) {
        noDataMessage.style.display = 'block';
        noDataMessage.innerHTML = 'No templates found for the selected criteria.';
        return;
    }
    noDataMessage.style.display = 'none';
    
    data.forEach((item, index) => {
        const tr = document.createElement('tr');
        
        // # (row number)
        const tdNum = document.createElement('td');
        tdNum.textContent = index + 1;
        tdNum.style.textAlign = 'center';
        tdNum.style.color = '#94a3b8';
        tr.appendChild(tdNum);
        
        // Template Name
        const tdName = document.createElement('td');
        tdName.textContent = item.template_name;
        tdName.style.fontWeight = '500';
        tr.appendChild(tdName);
        
        // Date Range - FULL range (unfiltered)
        const tdDate = document.createElement('td');
        if (item.date_min && item.date_max) {
            tdDate.textContent = `${formatDateForDisplay(item.date_min)} – ${formatDateForDisplay(item.date_max)}`;
        } else {
            tdDate.textContent = '—';
            tdDate.style.color = '#94a3b8';
        }
        // Add a small note if filtered range is narrower
        tr.appendChild(tdDate);
        
        // Total Rows (all data, unfiltered)
        const tdTotalRows = document.createElement('td');
        tdTotalRows.textContent = item.total_row_count || 0;
        tdTotalRows.style.textAlign = 'center';
        tdTotalRows.style.fontWeight = '600';
        tdTotalRows.setAttribute('data-tooltip', 'Total rows across all dates for this template');
        tr.appendChild(tdTotalRows);
        
        // Filtered Rows (matching the selected date range)
        const tdFilteredRows = document.createElement('td');
        tdFilteredRows.textContent = item.filtered_row_count || 0;
        tdFilteredRows.style.textAlign = 'center';
        tdFilteredRows.style.color = '#3b82f6';
        tdFilteredRows.style.fontWeight = '500';
        tdFilteredRows.setAttribute('data-tooltip', 'Rows matching the selected date filter');
        tr.appendChild(tdFilteredRows);
        
        // Sunday Rows (filtered)
        const tdSunday = document.createElement('td');
        tdSunday.textContent = item.filtered_sunday_count || 0;
        tdSunday.style.textAlign = 'center';
        if (item.filtered_sunday_count > 0) {
            tdSunday.style.color = '#f59e0b';
            tdSunday.style.fontWeight = '600';
        }
        tdSunday.setAttribute('data-tooltip', 'Sunday rows within the selected date range');
        tr.appendChild(tdSunday);
        
        // Status (based on total data)
        const tdStatus = document.createElement('td');
        tdStatus.style.textAlign = 'center';
        const statusSpan = document.createElement('span');
        // Use status from API ('complete' if total_row_count > 0, else 'missing')
        const status = item.status || 'missing';
        statusSpan.className = `status-badge ${status}`;
        const statusMap = {
            'complete': '✅ Complete',
            'missing': '❌ Missing',
            'partial': '⚠️ Partial'
        };
        statusSpan.textContent = statusMap[status] || status;
        tdStatus.appendChild(statusSpan);
        // Add a tooltip showing filtered status
        if (item.filtered_status === 'missing' && status === 'complete') {
            statusSpan.style.cursor = 'help';
            statusSpan.setAttribute('data-tooltip', 'Data exists but outside the selected date range');
        }
        tr.appendChild(tdStatus);
        
                // Actions
        const tdActions = document.createElement('td');
        tdActions.style.textAlign = 'center';
        tdActions.style.whiteSpace = 'nowrap';
        
        // Preview button - enabled if there's ANY data (total_row_count > 0)
        if (item.total_row_count > 0) {
            const previewBtn = document.createElement('button');
            previewBtn.className = 'action-btn preview';
            previewBtn.textContent = '👁️';
            previewBtn.title = 'Preview Data';
            previewBtn.setAttribute('data-tooltip', 'Preview data for this template');
            // Phase 2 (Unified date range): the 👁️ icon always opens the shared
            // date-range popover first (gate mode) — the preview itself only
            // opens once a range is confirmed there.
            previewBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                openSharedRangePopover(previewBtn, 'gate', item.template_id, item.template_name);
            });
            tdActions.appendChild(previewBtn);
            
            // Export button
            const exportBtn = document.createElement('button');
            exportBtn.className = 'action-btn export';
            exportBtn.textContent = '📥';
            exportBtn.title = 'Export to Excel';
            exportBtn.setAttribute('data-tooltip', 'Export all data for this template');
            exportBtn.style.marginLeft = '6px';
            exportBtn.addEventListener('click', () => {
                exportDataset(item.template_id, item.template_name);
            });
            tdActions.appendChild(exportBtn);

            // Delete button
            const deleteBtn = document.createElement('button');
            deleteBtn.className = 'action-btn delete';
            deleteBtn.textContent = '🗑️';
            deleteBtn.title = 'Delete all data for this template';
            deleteBtn.setAttribute('data-tooltip', 'Delete all imported data for this template');
            deleteBtn.style.marginLeft = '6px';
            deleteBtn.style.color = '#dc2626';
            deleteBtn.addEventListener('click', () => {
                deleteTemplateData(item.template_id, item.template_name, item.total_row_count);
            });
            tdActions.appendChild(deleteBtn);
        } else {
            tdActions.textContent = '—';
            tdActions.style.color = '#94a3b8';
        }
        
        tr.appendChild(tdActions);
        availabilityBody.appendChild(tr);
    });
    
    // Update stats using the new fields
    const totalAllRows = data.reduce((sum, item) => sum + (item.total_row_count || 0), 0);
    const totalFilteredRows = data.reduce((sum, item) => sum + (item.filtered_row_count || 0), 0);
    const totalTemplates = data.length;
    const missing = data.filter(item => item.status === 'missing').length;
    const sundayRows = data.reduce((sum, item) => sum + (item.filtered_sunday_count || 0), 0);
    
    statsTotalRecords.textContent = totalAllRows;  // Show total rows, not filtered
    statsTotalTemplates.textContent = totalTemplates;
    statsMissingTemplates.textContent = missing;
    statsSundayRows.textContent = sundayRows;
}
async function deleteTemplateData(templateId, templateName, rowCount) {
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected.', 'error');
        return;
    }
    
    // Confirm deletion
    const confirmMsg = `⚠️ Are you sure you want to delete ALL ${rowCount} rows for template "${templateName}"?\n\n` +
                       `This will permanently remove this data from the system (soft-delete).\n` +
                       `Templates and columns will NOT be affected.`;
    if (!confirm(confirmMsg)) {
        return;
    }
    
    try {
        showStatus('⏳ Deleting data...', 'loading');
        const res = await fetch(`${API_BASE}/api/data/template/${templateId}?client_id=${clientId}`, {
            method: 'DELETE'
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to delete data');
        }
        const result = await res.json();
        showStatus(`✅ ${result.message}`, 'success');
        // Refresh the availability table
        await fetchAvailability();
    } catch (error) {
        console.error('Delete error:', error);
        showStatus('❌ Error deleting data: ' + error.message, 'error');
    }
}
// ==============================================
// 📋 PREVIEW MODAL – FULL IMPLEMENTATION
// ==============================================

// ---------- OPEN PREVIEW ----------
function openPreview(templateId, templateName) {
    // The shared #dateStart/#dateEnd inputs (already validated by whichever
    // caller reached this point — confirmSharedRangePopover()) are the single
    // source of truth; this is a defensive fallback only, not a copy.
    const dateRange = getDateRangeFromFilter();
    if (!dateRange) {
        showStatus('⚠️ Please select a date range.', 'error');
        return;
    }

    // Check if client is still selected
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected. Please select a client from the Dashboard.', 'error');
        return;
    }

    previewTemplateId = templateId;
    previewTemplateName = templateName;
    previewLimit = parseInt(pageSizeSelect.value) || 50;
    currentPage = 1;
    isPreviewOpen = true;

    // Show modal
    previewModal.style.display = 'flex';
    previewTitle.textContent = `📋 ${templateName} – Preview (${getSelectedClientName()})`;
    sundayOnlyCheckbox.checked = false;
    searchInput.value = '';
    amountMin.value = '';
    amountMax.value = '';
    dataQualityFilter.value = '';
    sundayCountBadge.textContent = 'Sunday rows: 0';

    // Fetch data
    fetchPreviewData();
}

// ---------- FETCH PREVIEW DATA ----------
async function fetchPreviewData() {
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected.', 'error');
        return;
    }
    
    // Read the shared date range fresh on every call (rather than a cached
    // copy) — this is what lets a range change made elsewhere (main sheet
    // or the in-modal popover) take effect the next time data is fetched,
    // with no separate preview-only range to keep in sync.
    const dateRange = getDateRangeFromFilter();
    if (!previewTemplateId || !dateRange) {
        showStatus('⚠️ Missing preview parameters.', 'error');
        return;
    }

    isPreviewLoading = true;
    previewTableBody.innerHTML = `<tr><td colspan="100" style="text-align:center; padding:40px; color:#94a3b8;">
        <span class="spinner"></span> Loading data...
    </td></tr>`;
    previewTableHead.innerHTML = '';
    previewCountInfo.textContent = 'Loading...';

    const params = new URLSearchParams({
        client_id: clientId,
        template_id: previewTemplateId,
        date_start: dateRange.start,
        date_end: dateRange.end,
        page: currentPage,
        limit: previewLimit
    });

    // Add filters
    if (searchInput.value.trim()) {
        params.append('search', searchInput.value.trim());
    }
    if (amountMin.value) {
        params.append('amount_min', amountMin.value);
    }
    if (amountMax.value) {
        params.append('amount_max', amountMax.value);
    }
    if (sundayOnlyCheckbox.checked) {
        params.append('sunday_only', 'true');
    }
    if (dataQualityFilter.value) {
        params.append('data_quality', dataQualityFilter.value);
    }

    try {
        const res = await fetch(`${API_BASE}/api/data/preview?${params.toString()}`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Failed to fetch preview');
        }
        const data = await res.json();
        
        previewTotalRows = data.total_rows;
        previewSundayCount = data.sunday_count;
        const rows = data.rows || [];
        
        // Update Sunday badge
        sundayCountBadge.textContent = `Sunday rows: ${previewSundayCount}`;
        
        // Render table
        renderPreviewRows(rows);
        updatePagination();
        
        if (rows.length === 0 && previewTotalRows === 0) {
            previewTableBody.innerHTML = '<tr><td colspan="100" style="text-align:center; padding:40px; color:#94a3b8;">No data found for the selected criteria.</td></tr>';
            previewCountInfo.textContent = '0 rows';
        }
        
    } catch (error) {
        console.error('Preview fetch error:', error);
        showStatus('❌ Error loading preview: ' + error.message, 'error');
        previewTableBody.innerHTML = `<tr><td colspan="100" style="text-align:center; padding:40px; color:#dc2626;">Error loading data. Please try again.</td></tr>`;
        previewCountInfo.textContent = 'Error loading';
    } finally {
        isPreviewLoading = false;
    }
}

// ---------- RENDER PREVIEW ROWS ----------
function renderPreviewRows(rows) {
    hideRowHistoryPopover();
    if (!rows || rows.length === 0) {
        previewTableBody.innerHTML = '<tr><td colspan="100" style="text-align:center; padding:40px; color:#94a3b8;">No rows to display.</td></tr>';
        previewTableHead.innerHTML = '';
        previewCountInfo.textContent = '0 rows';
        return;
    }
    
    // Build headers from first row
    const firstRow = rows[0].row_data;
    const headers = firstRow ? Object.keys(firstRow) : [];
    previewTableHead.innerHTML = '';
    if (headers.length) {
        const tr = document.createElement('tr');
        
        // Add Serial Number column header
        const thSerial = document.createElement('th');
        thSerial.textContent = 'S.No';
        thSerial.style.padding = '8px 12px';
        thSerial.style.textAlign = 'center';
        thSerial.style.borderBottom = '2px solid #e2e8f0';
        thSerial.style.whiteSpace = 'nowrap';
        thSerial.style.position = 'sticky';
        thSerial.style.top = '0';
        thSerial.style.background = '#f8fafc';
        thSerial.style.width = '60px';
        tr.appendChild(thSerial);
        
        headers.forEach(h => {
            const th = document.createElement('th');
            th.textContent = h;
            th.style.padding = '8px 12px';
            th.style.textAlign = 'left';
            th.style.borderBottom = '2px solid #e2e8f0';
            th.style.whiteSpace = 'nowrap';
            th.style.position = 'sticky';
            th.style.top = '0';
            th.style.background = '#f8fafc';
            tr.appendChild(th);
        });
        previewTableHead.appendChild(tr);
    }
    
    // Render body rows with serial numbers
    previewTableBody.innerHTML = '';
    const startSerial = (currentPage - 1) * previewLimit + 1;
    rows.forEach((row, index) => {
        const tr = document.createElement('tr');
        // Check if this row's extract_date is Sunday
        const isSun = isSunday(row.extract_date);
        if (isSun) {
            tr.className = 'sunday-highlight';
        }
        
        // Serial number column
        const tdSerial = document.createElement('td');
        tdSerial.style.padding = '6px 12px';
        tdSerial.style.borderBottom = '1px solid #f0f2f5';
        tdSerial.style.textAlign = 'center';
        tdSerial.style.color = '#64748b';
        tdSerial.textContent = startSerial + index;

        // Revision history icon: only shown for rows overwritten during a re-upload.
        if (row.has_history) {
            const historyIcon = document.createElement('span');
            historyIcon.className = 'history-icon';
            historyIcon.textContent = '⚠️';
            historyIcon.title = `${row.revision_count} revision(s)`;
            // Click-to-open/toggle instead of hover: hover closed the popover
            // as soon as the mouse moved toward it, before it could be read.
            historyIcon.addEventListener('click', (e) => {
                e.stopPropagation();
                if (activeHistoryIcon === historyIcon) {
                    hideRowHistoryPopover();
                } else {
                    showRowHistoryPopover(historyIcon, row.id);
                }
            });
            tdSerial.appendChild(historyIcon);
        }

        // Data quality flag icon: only shown for rows force-imported with a
        // blank Document ID/Amount/Date (via the Upload page's override).
        if (row.data_quality_flag === 'incomplete') {
            const qualityIcon = document.createElement('span');
            qualityIcon.className = 'quality-flag-icon';
            qualityIcon.textContent = '🚩';
            qualityIcon.title = 'Imported with missing data (blank Document ID/Amount/Date)';
            tdSerial.appendChild(qualityIcon);
        }

        tr.appendChild(tdSerial);
        
        headers.forEach(h => {
            const td = document.createElement('td');
            td.style.padding = '6px 12px';
            td.style.borderBottom = '1px solid #f0f2f5';
            const value = row.row_data[h] !== undefined ? row.row_data[h] : '';
            td.textContent = formatCellValue(value, h);
            tr.appendChild(td);
        });
        previewTableBody.appendChild(tr);
    });
    
    previewCountInfo.textContent = `Showing ${rows.length} of ${previewTotalRows} rows`;
}

// ---------- UPDATE PAGINATION ----------
function updatePagination() {
    const totalPages = Math.ceil(previewTotalRows / previewLimit) || 1;
    pageInfo.textContent = `Page ${currentPage} of ${totalPages}`;
    prevPageBtn.disabled = currentPage <= 1;
    nextPageBtn.disabled = currentPage >= totalPages;
}

// ---------- APPLY PREVIEW FILTERS ----------
function applyPreviewFilters() {
    if (!isPreviewOpen) return;
    currentPage = 1;
    fetchPreviewData();
}

// ---------- CLEAR PREVIEW FILTERS ----------
function clearPreviewFilters() {
    if (!isPreviewOpen) return;
    searchInput.value = '';
    amountMin.value = '';
    amountMax.value = '';
    sundayOnlyCheckbox.checked = false;
    dataQualityFilter.value = '';
    currentPage = 1;
    fetchPreviewData();
}

// ---------- EXPORT DATASET ----------
async function exportDataset(templateId, templateName) {
    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ No client selected.', 'error');
        return;
    }
    
    const dateRange = getDateRangeFromFilter();
    if (!dateRange) {
        showStatus('⚠️ Please select a date range.', 'error');
        return;
    }
    
    showStatus('⏳ Preparing export...', 'loading');
    
    try {
        // Fetch all rows in batches
        let allRows = [];
        let page = 1;
        const limit = 500;
        let totalFetched = 0;
        const maxRows = 10000;
        
        const params = new URLSearchParams({
            client_id: clientId,
            template_id: templateId,
            date_start: dateRange.start,
            date_end: dateRange.end,
            limit: limit
        });
        
        do {
            params.set('page', page);
            const res = await fetch(`${API_BASE}/api/data/preview?${params.toString()}`);
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Export failed');
            }
            const data = await res.json();
            const rows = data.rows || [];
            if (rows.length === 0) break;
            
            allRows = allRows.concat(rows);
            totalFetched += rows.length;
            page++;
            
            if (totalFetched >= maxRows) {
                showStatus(`⚠️ Export limited to ${maxRows} rows. Use filters to reduce data.`, 'error');
                break;
            }
        } while (true);
        
        if (allRows.length === 0) {
            showStatus('⚠️ No data to export.', 'error');
            return;
        }
        
        // Build Excel data
        const firstRow = allRows[0];
        const headers = firstRow.row_data ? Object.keys(firstRow.row_data) : [];
        const excelData = [headers];
        allRows.forEach(row => {
            const rowData = headers.map(h => row.row_data[h] !== undefined ? row.row_data[h] : '');
            excelData.push(rowData);
        });
        
        // Create workbook
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

// ---------- COVERAGE CALENDAR EVENT LISTENERS (Phase 4) ----------
// Redesign Phase 2: search only re-filters which chips are visible (no
// fetch) — a chip's own click listener (see renderCalendarTemplateChips())
// is what now triggers fetchCoverageCalendar() on a selection change.
calendarTemplateSearchInput.addEventListener('input', () => {
    renderCalendarTemplateChips(calendarTemplateSearchInput.value);
});
calendarPrevMonthBtn.addEventListener('click', goToPrevMonth);
calendarNextMonthBtn.addEventListener('click', goToNextMonth);

// ---------- EVENT LISTENERS (Main) ----------
// Redesign Phase 1: same destination/behavior as the old inline onclick.
uploadMoreBtn.addEventListener('click', () => {
    window.location.href = '/static/upload.html';
});
checkBtn.addEventListener('click', fetchAvailability);
clearFiltersBtn.addEventListener('click', clearAllFilters);
refreshBtn.addEventListener('click', fetchAvailability);

filterDateStart.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchAvailability(); });
filterDateEnd.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchAvailability(); });
filterTemplate.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchAvailability(); });

// Preview modal events
closePreviewBtn.addEventListener('click', () => {
    previewModal.style.display = 'none';
    isPreviewOpen = false;
    hideRowHistoryPopover();
    closeSharedRangePopover();
});
previewModal.addEventListener('click', (e) => {
    if (e.target === previewModal) {
        previewModal.style.display = 'none';
        isPreviewOpen = false;
        hideRowHistoryPopover();
        closeSharedRangePopover();
    }
});

// Pagination
prevPageBtn.addEventListener('click', () => {
    if (currentPage > 1) {
        currentPage--;
        fetchPreviewData();
    }
});
nextPageBtn.addEventListener('click', () => {
    const totalPages = Math.ceil(previewTotalRows / previewLimit) || 1;
    if (currentPage < totalPages) {
        currentPage++;
        fetchPreviewData();
    }
});

// Page size change
pageSizeSelect.addEventListener('change', function() {
    previewLimit = parseInt(this.value) || 50;
    currentPage = 1;
    if (isPreviewOpen) {
        fetchPreviewData();
    }
});

// Preview filters
applyFiltersBtn.addEventListener('click', applyPreviewFilters);
// Modal clear button (using the updated ID from the HTML fix)
const modalClearBtn = document.getElementById('clearPreviewFiltersBtn');
if (modalClearBtn) {
    modalClearBtn.addEventListener('click', clearPreviewFilters);
}

// The main clear button is already handled.

// Sunday checkbox
sundayOnlyCheckbox.addEventListener('change', function() {
    if (isPreviewOpen) {
        currentPage = 1;
        fetchPreviewData();
    }
});

// Data quality filter (All / Complete / Incomplete)
dataQualityFilter.addEventListener('change', function() {
    if (isPreviewOpen) {
        currentPage = 1;
        fetchPreviewData();
    }
});

// Search with debounce
let searchTimeout;
searchInput.addEventListener('input', function() {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
        if (isPreviewOpen) {
            currentPage = 1;
            fetchPreviewData();
        }
    }, 400);
});

// Amount filters – apply on Enter key
amountMin.addEventListener('keydown', (e) => { if (e.key === 'Enter' && isPreviewOpen) applyPreviewFilters(); });
amountMax.addEventListener('keydown', (e) => { if (e.key === 'Enter' && isPreviewOpen) applyPreviewFilters(); });

// Shared date range popover (Phase 2 — Unified date range): the modal's own
// trigger opens it in "live" mode (no pending template, no anchor template
// info needed — confirming just re-fetches the currently open preview).
modalDateRangeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    openSharedRangePopover(modalDateRangeBtn, 'live');
});
sharedRangeConfirmBtn.addEventListener('click', confirmSharedRangePopover);
// Enter-to-confirm from either date field, same convention as amountMin/amountMax.
sharedRangeFromInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') confirmSharedRangePopover();
});
sharedRangeToInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') confirmSharedRangePopover();
});

// Close the row history popover when clicking anywhere outside it (the icon's
// own click is stopped from propagating here, so this only fires for clicks
// elsewhere on the page). Also closes the shared date range popover on the
// same outside-click check — both of its trigger buttons already stop
// propagation, so a click on either is never treated as "outside".
document.addEventListener('click', (e) => {
    if (rowHistoryPopover.style.display === 'block' && !rowHistoryPopover.contains(e.target)) {
        hideRowHistoryPopover();
    }
    if (sharedRangePopover.style.display === 'block' && !sharedRangePopover.contains(e.target)) {
        closeSharedRangePopover();
    }
});

// ESC key closes preview (or just the shared range popover, if that's open)
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sharedRangePopover.style.display === 'block') {
        closeSharedRangePopover();
        return;
    }
    if (e.key === 'Escape' && isPreviewOpen) {
        previewModal.style.display = 'none';
        isPreviewOpen = false;
    }
    // Ctrl+Enter triggers check availability
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        fetchAvailability();
    }
});

// ---------- CLIENT CHANGE HOOK (with debounce) ----------
let clientChangeTimeout;
window.onClientChange = function(clientId) {
    clearTimeout(clientChangeTimeout);
    clientChangeTimeout = setTimeout(() => {
        if (clientId) {
            if (filterDateStart.value && filterDateEnd.value) {
                fetchAvailability();
            } else {
                setDefaultDateRange();
                fetchAvailability();
            }
            // If preview is open, close it
            if (isPreviewOpen) {
                previewModal.style.display = 'none';
                isPreviewOpen = false;
            }
            // Coverage Calendar (Phase 4): refresh for the new client, same
            // template/month selection — presence data is per-client.
            fetchCoverageCalendar();
        }
    }, 300);
};

// ---------- INIT ----------
window.onload = async function() {
    // Load utilities first
    if (typeof initUtils === 'function') {
        await initUtils();
    }
    
    setDefaultDateRange();
    await loadTemplateDropdown();
    await loadCalendarTemplates();
    updateCalendarMonthLabel();

    const clientId = getSelectedClientId();
    if (!clientId) {
        showStatus('⚠️ Please select a client from the Dashboard first.', 'error');
        availabilityBody.innerHTML = '';
        noDataMessage.style.display = 'block';
        noDataMessage.innerHTML = '⚠️ No client selected. Please go to the Dashboard and select a client.';
        setTimeout(() => {
            window.location.href = '/static/index.html';
        }, 3000);
        return;
    }

    fetchAvailability();
};